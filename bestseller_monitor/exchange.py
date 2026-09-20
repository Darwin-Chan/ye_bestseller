"""数据交换台的一次运行与周报（spec §7/§8）。

**一次运行**（`run_once`，手工触发）：检查 → 导出 → 拉取 → 汇总 → 报告；
`--only export|merge` 只跑一半（导出这半 = 检查 + 导出，汇总这半 = 检查 + 拉取 + 汇总；
报告两半都写）。退出码：`0` 干净 / `1` 有需要人看一眼的（缺口、冲突、别人的包还没来、
通道没拉成、图片缺）/ `2` 本机没做成事（导出没成功、有包没导成、纯汇总机被要求只导出）。

- **检查**：配置与角色、交换区与图片仓库可达性、本机本周采到了哪些天。交换区根不在是
  致命的（本机没做成事），其余检查结果只记注记。
- **导出**：`export.export`（票据 08 的一致性快照 + 周包 + 图片只传新增）。纯汇总机跳过并
  明示（保留入口不藏）；导出失败不拦汇总这半，如实记进报告与退出码。
- **拉取**：对另外的每个交换库 pull（别的 `raw-*` 库 + `plan` 库——纯汇总机不跑采集
  准备串，plan 克隆靠这一趟保鲜）。拉不动记一笔（需要人看一眼），不拦汇总。
- **汇总**：交换区里本周别人发的包逐个 `merge.import_package`（票据 09 的确定性合并、
  同哈希幂等跳过）；本机自己的包不收。
- **报告**：`<交换区根>/报告/<年>-W<周>.md`——**一周一份、同周重跑重写同一份**，
  内容是**状态累积**（从计划表、本机库、交换区、幂等账与冲突账现读）而不是两次运行
  日志相加；开头「本次运行」行说明这一次具体做了什么。逐次流水在 `logs/exchange.log`。

报告固定五节（§8）：本机发布 / 收进来的包 / 缺口与还没来的 / 冲突 / 下次该做什么；
干净场景不出现「冲突」节（三份样例的形态）。两处口径写死在这里：

- **缺口** = 计划里该采到的（店铺 × 日期）在收到的包里找不到。本机份额对照本机库
  （「本机本周采到了哪些天」）；别机份额对照它的包（包还没来就记「还没来的包」，
  不把它的店全记成缺口）。只把已经过去（含今天）的天算作「该采到」；
  历史缺口补不了，如实记、不催办。
- **冲突** 从本机导入账（`import_conflicts`）读，**不入交换区**——「我见过这个冲突吗」
  取决于本机导入过哪些包，所以冲突一节是本机视角。说明里「计划外多采」由计划表
  推断（claim 的机器不是那家店本周的计划归属）。

报告是本机视角的；报告里的机器中文名（采集机 / 纯汇总机）与配置共用 `config.ROLE_GLOSS`。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import pathlib
import sqlite3
from collections.abc import Callable

from bestseller_monitor import export, merge, plan_step, rounds, weekly_plan
from bestseller_monitor.config import ROLE_GLOSS, ROLE_MERGE_ONLY
from bestseller_monitor.db import CST, Database, connect
from bestseller_monitor.git_channel import ChannelError, GitChannel
from bestseller_monitor.image_store import ImageStoreError

log = logging.getLogger(__name__)

REPORT_DIR = "报告"                 # 周报落在交换区根下的这个目录（本机所有，不进交换集）
ONLY_EXPORT = "export"
ONLY_MERGE = "merge"

EXIT_CLEAN = 0                      # 干净
EXIT_NEEDS_LOOK = 1                 # 有需要人看一眼的
EXIT_NOTHING_DONE = 2               # 本机没做成事（给以后挂计划任务留的判据）

# 退出码的人话（报告「结果」行与命令行收尾共用一处）：spec §7 的三个数各对应一句。
VERDICT = {EXIT_CLEAN: "干净", EXIT_NEEDS_LOOK: "有需要人看一眼的地方",
           EXIT_NOTHING_DONE: "本机没做成事"}

# 轮次终态 → 报告里那句人话（spec §8 的样例：「当天采集在详情预算耗尽后中止」）。
_REASON_GLOSS = {
    "COMPLETED": "当天采集完成了，但这家店没有数据",
    "DETAIL_BUDGET_EXHAUSTED": "当天采集在详情预算耗尽后中止",
    "FAIL_RATE_EXCEEDED": "当天采集因失败率超限中止",
    "DAY_BOUNDARY": "当天采集撞上当日截止线中止",
    "DENY_EXCEEDED": "当天采集因被拒次数超限中止",
    "ABANDONED": "当天采集被中止（放弃）",
    "LEGACY_UNKNOWN": "当天采集的终态不详（旧数据）",
}


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """检查步骤的结局：`fatal` 非空 = 连起点都没有（本机没做成事，不再往下走）。"""

    fatal: str | None
    notes: tuple[str, ...]
    crawled: tuple[tuple[str, tuple[str, ...]], ...]   # 本机负责的店 → 覆盖到的天


@dataclasses.dataclass(frozen=True)
class PullNote:
    """一个 raw 库的拉取结果。"""

    repo: str                       # raw-m2
    failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclasses.dataclass(frozen=True)
class PackageRow:
    """报告「收进来的包」表的一行；`None` 的单元格渲染成「—」。"""

    repo: str                       # raw-m2
    week_short: str                 # W38
    rows: int | None
    inserted: int | None
    replaced: int | None
    conflicts: int | None
    images: tuple[int, int] | None  # (张数, 字节)
    result: str


@dataclasses.dataclass(frozen=True)
class GapLine:
    """一个缺口：计划里该采到的（店铺 × 日期）在收到的包里找不到。"""

    machine: str                    # 这份份额归哪台机器（本机 = cfg.machine_id）
    shop_key: str
    date: str
    reason: str


@dataclasses.dataclass(frozen=True)
class ConflictEntry:
    """冲突账里的一条（同一 (日期, 店铺) 上两个不同机器标识的 claim）。"""

    shop_key: str
    date: str
    winner_machine: str
    winner_at: str                  # 取胜方那次采集的时刻（ISO）
    loser_machine: str
    loser_at: str
    replaced: int                   # 败方被覆盖的行数
    kept: int                       # 败方保留的行数
    packages: tuple[str, ...]       # 在哪几个包的导入里发现的（raw-<机器>）


@dataclasses.dataclass(frozen=True)
class RunOutcome:
    """一次运行的完整账：报告从它渲染，退出码由它算，命令行与窗口读它。"""

    week: str
    machine_id: str
    role: str
    ran_at: dt.datetime
    check: CheckResult
    export: export.ExportResult | None = None
    export_note: str | None = None          # 没跑导出时的说明（纯汇总机跳过 / --only merge）
    pulls: tuple[PullNote, ...] = ()
    imports: tuple[merge.ImportResult, ...] = ()
    merge_ran: bool = False
    packages: tuple[PackageRow, ...] = ()
    gaps: tuple[GapLine, ...] = ()
    conflicts: tuple[ConflictEntry, ...] = ()
    missing_machines: tuple[str, ...] = ()
    todo: tuple[str, ...] = ()
    summary: str = ""
    failures: tuple[str, ...] = ()          # 硬失败（退出码 2 的判据）
    exit_code: int = EXIT_CLEAN
    report_path: pathlib.Path | None = None
    self_shops: tuple[str, ...] = ()
    plan_known: bool = False
    plan_of_shop: dict[str, str] = dataclasses.field(default_factory=dict)


def _emitter(emit: Callable[[str], None] | None) -> Callable[[str], None]:
    """进度行同时进日志与调用方的回调（小窗口用回调；命令行直接 print）。"""

    def say(message: str) -> None:
        log.info("%s", message)
        if emit is not None:
            emit(message)

    return say


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else "（没有说明）"


def _mb(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB"


def _week_days(week: str, now: dt.datetime) -> tuple[dt.date, ...]:
    """这一周「该采到」的天：整周里已经过去（含今天）的那些；未来周一个都没有。"""
    monday, sunday = weekly_plan.week_window(week)
    last = min(sunday, now.date())
    return tuple(day for day in (monday + dt.timedelta(days=i) for i in range(7))
                 if day <= last)


def check_environment(cfg, *, db: Database, week: str, now: dt.datetime,
                      store=None) -> CheckResult:
    """检查（spec §7）：配置与角色、交换区与图片仓库可达性、本机本周采到了哪些天。

    只读，不改任何东西；`fatal` 非空 = 交换区根不在，连起点都没有（不再往下走）。
    """
    machine_id = str(cfg.machine_id)
    root = pathlib.Path(cfg.exchange_root)
    if not root.is_dir():
        return CheckResult(fatal=(
            f"交换区根目录不存在：{root}\n"
            "照上机清单第 4/9 步建好运行根与四个交换库的克隆（raw-m1、raw-m2、raw-m3、plan），"
            "再跑数据交换台。"), notes=(), crawled=())

    notes: list[str] = []
    repos = {path.name for path in root.glob("raw-*") if path.is_dir()}
    if cfg.role != ROLE_MERGE_ONLY and f"raw-{machine_id}" not in repos:
        notes.append(f"本机的 raw 库还没 clone 到 {root / f'raw-{machine_id}'}："
                     "导出这半会按「没做成事」报（上机清单第 9 步）。")
    if not [repo for repo in repos if repo != f"raw-{machine_id}"]:
        notes.append("交换区里没有别的 raw 库克隆：拉取与汇总这半没有可收的包。")

    store = store if store is not None else export.default_store(cfg)
    if store is None:
        notes.append("配置缺 machine.cos_bucket：图片通道没有桶可用（导出与汇总都会"
                     "按「图片这半没做」如实记）。")
    else:
        try:
            store.existing_keys()
        except ImageStoreError as exc:
            notes.append(f"图片库连不上：{exc}")

    return CheckResult(fatal=None, notes=tuple(notes),
                       crawled=_self_crawled_days(db, week, machine_id, now))


def _self_crawled_days(db: Database, week: str, machine_id: str,
                       now: dt.datetime) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """本机负责的店里，本周各覆盖到了哪些天（从本机库读）。没有落库计划就是空。"""
    plan = plan_step.stored_plan(db, week)
    if plan is None:
        return ()
    days = _week_days(week, now)
    if not days:
        return ()
    span = (days[0].isoformat(), days[-1].isoformat())
    out = []
    for shop_key in plan.machine_keys(machine_id):
        covered = {str(row[0]) for row in db.conn.execute(
            "SELECT DISTINCT date FROM inventory WHERE shop_key=? AND date BETWEEN ? AND ?",
            (shop_key, *span))}
        out.append((shop_key, tuple(day.isoformat() for day in days
                                    if day.isoformat() in covered)))
    return tuple(out)


def _pull_others(cfg, machine_id: str, say) -> tuple[PullNote, ...]:
    """对另外的每个交换库 pull：别的 raw 库 + `plan` 库（spec §7「对另外三个库 pull」）。

    plan 库跟着拉是纯汇总机那条线的要紧事：它不跑采集准备串，不拉就永远停在 clone
    时那份；采集机上重复拉一次无害（同内容是个空动作）。没 clone 的目录跳过——那是
    上机清单第 9 步的事，检查步骤会点名。
    """
    root = pathlib.Path(cfg.exchange_root)
    repos = {path for path in root.glob("raw-*") if path.is_dir()}
    repos.discard(root / f"raw-{machine_id}")
    plan_repo = root / plan_step.PLAN_REPO_DIR
    if plan_repo.is_dir():
        repos.add(plan_repo)
    notes: list[PullNote] = []
    for path in sorted(repos, key=lambda p: p.name):
        try:
            GitChannel(path).pull()
        except ChannelError as exc:
            notes.append(PullNote(repo=path.name, failure=str(exc)))
            say(f"拉取 {path.name}：没成（{_first_line(str(exc))}）")
        else:
            notes.append(PullNote(repo=path.name))
            say(f"拉取 {path.name}：完成")
    return tuple(notes)


def _merge_week(cfg, db: Database, *, week: str, machine_id: str, store,
                say) -> dict[pathlib.Path, merge.ImportResult]:
    """把交换区里本周别人发的包逐个收进本机库（同哈希自动跳过）。本机自己的包不收。"""
    root = pathlib.Path(cfg.exchange_root)
    imported: dict[pathlib.Path, merge.ImportResult] = {}
    for ref in export.week_packages(root, week):
        if ref.machine == machine_id:
            continue
        result = merge.import_package(db.conn, ref.path, machine_id=machine_id, store=store)
        imported[ref.path] = result
        if result.failed:
            say(f"导入 {ref.path.name}：没导成（{_first_line(result.failure)}）")
        elif result.skipped:
            say(f"跳过 {ref.path.name}：已导入过（同哈希）")
        else:
            say(f"导入 {ref.path.name}：新增 {result.rows_inserted:,} 行、"
                f"覆盖 {result.rows_replaced:,} 行、冲突 {result.conflicts} 处、"
                f"拉图 {result.images_pulled} 张")
    if not imported:
        say("交换区里没有别的机器发的本周包可收。")
    return imported


def _ledger_row(conn: sqlite3.Connection, path: pathlib.Path):
    """幂等账里这个包的那一行（按包内容哈希找）；哈希都算不出来时 None。"""
    try:
        sha = merge.package_sha256(path)
    except OSError:
        return None
    return conn.execute("SELECT * FROM import_packages WHERE package_sha256=?",
                        (sha,)).fetchone()


def run_once(cfg, *, only: str | None = None, week: str | None = None,
             now: dt.datetime | None = None, store=None,
             emit: Callable[[str], None] | None = None) -> RunOutcome:
    """一次运行：检查 → 导出 → 拉取 → 汇总 → 报告（`only` 指定只跑一半）。

    `only` 取 `ONLY_EXPORT` / `ONLY_MERGE`（None = 全链）；`week` 默认本周（补历史
    指定周窗口，导出与报告都按它走）；`store` 是图片库（None = 按配置现搭）；
    `emit` 收进度行（小窗口用）。退出码见模块 docstring。
    """
    now = now or dt.datetime.now(CST)
    week = week or export.current_week()
    machine_id = str(cfg.machine_id)
    role = str(cfg.role)
    say = _emitter(emit)

    conn = connect(cfg.db_file)
    try:
        db = Database(conn)
        check = check_environment(cfg, db=db, week=week, now=now, store=store)
        if check.fatal is not None:
            say(f"检查没通过：{check.fatal}")
            return RunOutcome(week=week, machine_id=machine_id, role=role, ran_at=now,
                              check=check, failures=(check.fatal,),
                              exit_code=EXIT_NOTHING_DONE)
        say(f"检查：本机 {machine_id}（{ROLE_GLOSS[role]}）")
        for note in check.notes:
            say(f"检查注记：{note}")
        if check.crawled:
            say("本机本周采到：" + "；".join(
                f"{shop_key} {len(days)} 天" for shop_key, days in check.crawled))

        # ---- 导出这半 ----
        export_result: export.ExportResult | None = None
        export_note: str | None = None
        if only == ONLY_MERGE:
            export_note = "本次只跑了汇总（--only merge），没做导出"
        elif role == ROLE_MERGE_ONLY:
            export_note = "本机是纯汇总机，跳过"
            say(f"导出：{export_note}（入口保留着，不藏）。")
        else:
            say(f"导出 {week} 的包…")
            export_result = export.export(cfg, week=week, store=store)
            say(_export_line(export_result))

        # ---- 拉取 + 汇总这半 ----
        pulls: tuple[PullNote, ...] = ()
        imported: dict[pathlib.Path, merge.ImportResult] = {}
        merge_ran = only != ONLY_EXPORT
        if merge_ran:
            pulls = _pull_others(cfg, machine_id, say)
            imported = _merge_week(cfg, db, week=week, machine_id=machine_id,
                                   store=store, say=say)

        outcome = _assemble(cfg, db, week=week, now=now, machine_id=machine_id, role=role,
                            check=check, export_result=export_result, export_note=export_note,
                            pulls=pulls, imported=imported, merge_ran=merge_ran, only=only)
        report_path = pathlib.Path(cfg.exchange_root) / REPORT_DIR / f"{week}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render(outcome), encoding="utf-8")
        say(f"报告已写到 {report_path}")
        say(f"本次运行：{outcome.summary}（退出码 {outcome.exit_code}）")
        for failure in outcome.failures:
            say(f"没做成事：{failure}")
        return dataclasses.replace(outcome, report_path=report_path)
    finally:
        conn.close()


def _export_line(result: export.ExportResult) -> str:
    name = pathlib.Path(result.package_rel).name
    if result.failed:
        return f"导出没成功：{_first_line(result.failure)}"
    if result.unchanged:
        return f"导出：{name} 与已发布那份同内容（无新提交）"
    return f"导出已发布：{name} → raw-{result.machine_id} @ {result.commit}"


def _assemble(cfg, db: Database, *, week, now, machine_id, role, check, export_result,
              export_note, pulls, imported, merge_ran, only) -> RunOutcome:
    """把这次运行与库里的现成事实折成报告模型（状态累积，不是运行日志相加）。"""
    root = pathlib.Path(cfg.exchange_root)
    plan = plan_step.stored_plan(db, week)
    days = _week_days(week, now)
    packages = export.week_packages(root, week)
    plan_of_shop = ({row.shop_key: row.machine_id for row in plan.rows}
                    if plan is not None else {})

    packages_rows = tuple(
        _package_row(db, ref, imported.get(ref.path))
        for ref in packages if ref.machine != machine_id)

    planned = set(plan_of_shop.values())
    arrived = {ref.machine for ref in packages}
    missing = tuple(sorted(m for m in planned if m != machine_id and m not in arrived))

    gaps = _gaps(db, plan, week=week, days=days, machine_id=machine_id, packages=packages,
                 missing=missing)
    conflicts = _conflicts(db, week=week, machine_id=machine_id, plan=plan)
    image_missing_total = sum(result.images_missing for result in imported.values())
    todo = _todo(machine_id=machine_id, week=week, plan_of_shop=plan_of_shop, gaps=gaps,
                 conflicts=conflicts, missing=missing, pulls=pulls,
                 export_result=export_result, imported=imported,
                 image_missing_total=image_missing_total)

    failures: list[str] = []
    if export_result is not None and export_result.failed:
        failures.append(f"导出没成功：{export_result.failure}")
    failures += [f"{result.package} 没导成：{result.failure}"
                 for result in imported.values() if result.failed]
    if only == ONLY_EXPORT and role == ROLE_MERGE_ONLY:
        failures.append("本机是纯汇总机：导出这半没有可做的事")
    soft = bool(gaps) or bool(conflicts) or bool(missing) or any(not p.ok for p in pulls) \
        or (export_result is not None and export_result.images is not None
            and export_result.images.failure is not None) or image_missing_total > 0

    return RunOutcome(
        week=week, machine_id=machine_id, role=role, ran_at=now, check=check,
        export=export_result, export_note=export_note, pulls=pulls,
        imports=tuple(imported.values()), merge_ran=merge_ran, packages=packages_rows,
        gaps=gaps, conflicts=conflicts, missing_machines=missing, todo=todo,
        summary=_summary(only=only, export_result=export_result, export_note=export_note,
                         imported=imported, merge_ran=merge_ran),
        failures=tuple(failures),
        exit_code=(EXIT_NOTHING_DONE if failures
                   else EXIT_NEEDS_LOOK if soft else EXIT_CLEAN),
        self_shops=plan.machine_keys(machine_id) if plan is not None else (),
        plan_known=plan is not None, plan_of_shop=plan_of_shop)


def _package_row(db: Database, ref: export.PackageRef,
                 result: merge.ImportResult | None) -> PackageRow:
    """一行：这次的导入结果优先；汇总没跑（或没碰它）时回落到幂等账里的状态。"""
    row = PackageRow(repo=f"raw-{ref.machine}",
                     week_short=f"W{export.package_week(ref.path.name)}",
                     rows=None, inserted=None, replaced=None, conflicts=None, images=None,
                     result="")
    if result is not None and result.failed:
        return dataclasses.replace(row, result=f"导入没成功：{_first_line(result.failure)}")
    if result is not None and result.skipped:
        return dataclasses.replace(row, rows=result.rows_total, result="已导入过 → 跳过")
    if result is not None:
        return dataclasses.replace(
            row, rows=result.rows_total, inserted=result.rows_inserted,
            replaced=result.rows_replaced, conflicts=result.conflicts,
            images=(result.images_pulled, result.images_bytes), result="已导入")
    known = _ledger_row(db.conn, ref.path)
    if known is not None:
        return dataclasses.replace(row, rows=known["rows_total"], result="已导入过")
    return dataclasses.replace(row, result="还没汇总")


def _gaps(db: Database, plan, *, week: str, days: tuple[dt.date, ...], machine_id: str,
          packages: tuple[export.PackageRef, ...], missing: tuple[str, ...]
          ) -> tuple[GapLine, ...]:
    """计划 × 收到的包：缺 = 计划里该采到的（店铺 × 日期）在哪都没有。

    覆盖的判据是「本机库 ∪ 交换区里全部包的覆盖」——口径是「在收到的包里找不到」
    （包是「自己那份」，一组 (店铺, 日期) 只在一台机器的包里；本机采的组在本机库里）。
    缺的展示归到计划里那台机器名下：本机份额的理由从轮次事实取，别机份额记「包缺这一天」；
    还没发布/没拉到的机器的份额不记缺口（那是「还没来的包」那一行的事）。
    """
    if plan is None or not days:
        return ()
    span = (days[0].isoformat(), days[-1].isoformat())
    local = {(str(row[0]), str(row[1])) for row in db.conn.execute(
        "SELECT DISTINCT shop_key, date FROM inventory WHERE date BETWEEN ? AND ?", span)}
    coverage: set[tuple[str, str]] = set()
    for ref in packages:
        try:
            coverage |= merge.package_shop_days(ref.path, days[0], days[-1])
        except (OSError, EOFError, sqlite3.Error) as exc:
            log.warning("缺口检查读不动包 %s：%s（该包的导入失败会另行报出）", ref.rel, exc)
    gaps: list[GapLine] = []
    for row in plan.rows:
        for day in days:
            pair = (row.shop_key, day.isoformat())
            if pair in local or pair in coverage:
                continue
            if row.machine_id == machine_id:
                gaps.append(GapLine(machine=machine_id, shop_key=row.shop_key,
                                    date=pair[1], reason=_self_gap_reason(db, pair[1])))
            elif row.machine_id in missing:
                continue                        # 「还没来的包」那一行说它，不记成缺口
            else:
                gaps.append(GapLine(machine=row.machine_id, shop_key=row.shop_key,
                                    date=pair[1],
                                    reason="计划里该采到，收到的包里没有这一天"))
    return tuple(gaps)


def _self_gap_reason(db: Database, day: str) -> str:
    """本机缺这一天，为什么：当天的轮次终态（进行中 = 半截的一天）。"""
    if rounds.active_round(db, day) is not None:
        return "当天采集还在跑（半截的一天）"
    on_day = rounds.on_date(db, day)
    if not on_day:
        return "当天没有采集记录"
    reason = on_day[-1].reason
    if reason is None:
        return "当天采集的终态不详"
    return _REASON_GLOSS.get(reason.value, f"当天采集的终态是 {reason.value}")


def _conflicts(db: Database, *, week: str, machine_id: str,
               plan) -> tuple[ConflictEntry, ...]:
    """本机导入账里的本周冲突，按日期、店铺排序。"""
    monday, sunday = weekly_plan.week_window(week)
    rows = db.conn.execute(
        "SELECT c.observed_date, c.shop_key, c.winner_machine, c.winner_at, c.loser_machine, "
        "c.loser_at, c.loser_rows_replaced, c.loser_rows_kept, "
        "p.machine_id AS package_machine "
        "FROM import_conflicts c LEFT JOIN import_packages p "
        "ON p.package_sha256 = c.package_sha256 "
        "WHERE c.observed_date BETWEEN ? AND ? ORDER BY c.observed_date, c.shop_key",
        (monday.isoformat(), sunday.isoformat())).fetchall()
    return tuple(ConflictEntry(
        shop_key=str(row["shop_key"]), date=str(row["observed_date"]),
        winner_machine=str(row["winner_machine"]), winner_at=str(row["winner_at"]),
        loser_machine=str(row["loser_machine"]), loser_at=str(row["loser_at"]),
        replaced=int(row["loser_rows_replaced"]), kept=int(row["loser_rows_kept"]),
        packages=((f"raw-{row['package_machine']}",) if row["package_machine"] else ()))
        for row in rows)


def _summary(*, only, export_result, export_note, imported, merge_ran) -> str:
    """报告开头的「本次运行」行：这一次具体做了什么。"""
    parts: list[str] = []
    if only == ONLY_EXPORT:
        parts.append("只跑了导出")
    elif only == ONLY_MERGE:
        parts.append("只跑了汇总")
    if export_result is not None:
        if export_result.failed:
            parts.append("导出没成功")
        elif export_result.unchanged:
            parts.append("导出：本周包上次已发布（无新提交）")
        else:
            parts.append("导出已发布")
    elif export_note is not None and only != ONLY_MERGE:
        parts.append(f"导出：{export_note}")
    if merge_ran:
        results = list(imported.values())
        fresh = [r for r in results if not r.skipped and not r.failed]
        skipped = [r for r in results if r.skipped]
        failed = [r for r in results if r.failed]
        if fresh:
            parts.append(f"新收 {len(fresh)} 个包（{sum(r.rows_total for r in fresh):,} 行）")
        if skipped:
            parts.append(f"跳过 {len(skipped)} 个已导入")
        if failed:
            parts.append(f"{len(failed)} 个包没导成")
        if not (fresh or skipped or failed):
            parts.append("没有新包")
    return " · ".join(parts)


def _day_count(count: int) -> str:
    return "一天" if count == 1 else f"{count} 天"


def _by_machine(gaps: tuple[GapLine, ...]) -> list[tuple[str, list[GapLine]]]:
    """按份额归属的机器分组（机器标识排序）——缺口节与待办节共用。"""
    return [(machine, [gap for gap in gaps if gap.machine == machine])
            for machine in sorted({gap.machine for gap in gaps})]


def _gap_detail(gaps: list[GapLine], *, reasons: bool) -> str:
    """一组缺口的行内明细；`reasons` 带上理由（本机份额才有人话理由可带）。"""
    return "；".join(f"{gap.shop_key} {gap.date[5:]}" + (f" —— {gap.reason}" if reasons else "")
                     for gap in gaps)


def _todo(*, machine_id, week, plan_of_shop, gaps, conflicts, missing, pulls, export_result,
          imported, image_missing_total) -> tuple[str, ...]:
    """「下次该做什么」：从这次运行的事实与账里长出来。"""
    week_short = week.split("-")[1]
    items: list[str] = []
    for machine in missing:
        items.append(f"raw-{machine} 的 {week_short} 还没发布或没拉到。它发布后重跑一次"
                     "汇总就能收进来（重复导入是安全的）")

    self_over: list[str] = []
    for entry in conflicts:
        planned = plan_of_shop.get(entry.shop_key)
        if planned is not None and planned != machine_id and entry.shop_key not in self_over \
                and machine_id in (entry.winner_machine, entry.loser_machine):
            self_over.append(entry.shop_key)
    if self_over:
        items.append(f"提醒本机操作者：不要手工勾选本期不归本机的店（{'、'.join(sorted(self_over))}），"
                     "多采只会变成汇总侧的一条冲突")
    unexplained = [c for c in conflicts if c.shop_key not in self_over]
    if unexplained:
        items.append(f"还有 {len(unexplained)} 处冲突不是本机多采造成的（见第四节）："
                     "确认是越权、降级逃生口还是换周交接不清")

    if mine := [gap for gap in gaps if gap.machine == machine_id]:
        items.append(f"本机缺{_day_count(len(mine))}（{_gap_detail(mine, reasons=True)}）"
                     "无法回填，不影响下周")
    others = [gap for gap in gaps if gap.machine != machine_id]
    for machine, group in _by_machine(tuple(others)):
        items.append(f"raw-{machine} 的包缺{_day_count(len(group))}"
                     f"（{_gap_detail(group, reasons=False)}）：历史日期补不了，如实记一笔")

    for pull in pulls:
        if not pull.ok:
            items.append(f"{pull.repo} 拉不动（{_first_line(pull.failure)}）：通道恢复后重跑一次")
    if export_result is not None:
        if export_result.failed:
            items.append("导出没成功（见第一节）：修好通道后重跑一次")
        elif export_result.images is not None and export_result.images.failure is not None:
            items.append("图片通道这趟没传成（见第一节）：通道恢复后重跑一次导出就能补上")
    for result in imported.values():
        if result.failed:
            items.append(f"{result.package} 没导成（见第二节）：修好后重跑一次"
                         "（重跑等价于首次导入，安全）")
    if image_missing_total:
        items.append(f"这次有 {image_missing_total} 张图没拉到（见第二节），如实记一笔")

    return tuple(items) if items else ("没有待办：本周干净",)


# ---------- 报告渲染（三份原型样例的形态） ----------

def _num(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _images_cell(images: tuple[int, int] | None) -> str:
    return "—" if images is None else f"{images[0]}（{_mb(images[1])}）"


def _hhmm(moment: str) -> str:
    try:
        return dt.datetime.fromisoformat(str(moment)).strftime("%H:%M")
    except ValueError:
        return str(moment)


def _side_text(outcome: RunOutcome, entry: ConflictEntry, machine: str, at: str) -> str:
    """冲突里一方的人话：本机 / 计划机（那家店本周的计划归属）/ 机器编号，计划外多采注明。"""
    planned = outcome.plan_of_shop.get(entry.shop_key)
    who = "本机" if machine == outcome.machine_id else (
        "计划机" if planned == machine else machine)
    note = "（计划外多采）" if planned is not None and planned != machine else ""
    return f"{who} {_hhmm(at)}{note}"


def _render(outcome: RunOutcome) -> str:
    monday, sunday = weekly_plan.week_window(outcome.week)
    span = f"{monday.month}月{monday.day}日 – {sunday.month}月{sunday.day}日"
    lines = [
        f"# 数据交换台周报 · {outcome.week}",
        "",
        f"本机 **{outcome.machine_id}**（{ROLE_GLOSS[outcome.role]}）· "
        f"{outcome.ran_at:%Y-%m-%d %H:%M} 运行 · 覆盖 {span}",
        "",
        f"**结果**：{VERDICT[outcome.exit_code]}（退出码 {outcome.exit_code}）· "
        f"缺口 {len(outcome.gaps)} · "
        f"冲突 {len(outcome.conflicts)} · 还没来的包 {len(outcome.missing_machines)}",
        f"**本次运行**：{outcome.summary}",
        "",
    ]
    lines += _publish_section(outcome)
    lines += _received_section(outcome)
    lines += _gaps_section(outcome)
    lines += _conflicts_section(outcome)
    lines += _todo_section(outcome)
    return "\n".join(lines) + "\n"


def _publish_section(outcome: RunOutcome) -> list[str]:
    out = ["## 一、本机发布"]
    result = outcome.export
    if result is None:
        # 没跑导出（纯汇总机跳过 / --only merge）：入口保留着的那句话 +
        # 本机负责的店照旧对照计划说清楚（没有导出结果，行数/图片这两行无从谈起）
        out.append(f"- 导出：{outcome.export_note}")
        out.append("- 本机负责：" + _self_shops_text(outcome))
        return out + [""]
    name = pathlib.Path(result.package_rel).name
    if result.failed:
        out.append(f"- 包：{name} 这次没发出去（{_first_line(result.failure)}）")
    else:
        out.append(f"- 包：{name}（{_mb(result.package_size)}）→ "
                   f"raw-{result.machine_id} @ {result.commit}")
    out.append("- 本机负责：" + _self_shops_text(outcome))
    rows = result.rows
    out.append("- 行数：inventory {:,} · products {:,} · skus {:,} · 版本 {:,}".format(
        rows.get("inventory", 0), rows.get("products", 0), rows.get("skus", 0),
        rows.get("product_information_versions", 0)))
    out.append("- 图片：" + _images_text(result.images))
    return out + [""]


def _self_shops_text(outcome: RunOutcome) -> str:
    if outcome.role == ROLE_MERGE_ONLY:
        return "无（纯汇总机，不参与计划）"
    if not outcome.plan_known:
        return f"本机没有本周（{outcome.week}）的落库计划，无从对照"
    if not outcome.self_shops:
        return "本周空手（计划里没有归本机的店）"
    return "、".join(outcome.self_shops)


def _images_text(images) -> str:
    if images is None:
        return "这趟没做（导出没成功）"
    if images.failure is not None:
        text = (f"没传完（{_first_line(images.failure)}）；已传 {images.uploaded} 张"
                f"（{_mb(images.uploaded_bytes)}）")
    else:
        text = f"新增 {images.uploaded} 张（{_mb(images.uploaded_bytes)}）"
    text += f"；已有 {images.skipped} 张跳过"
    if images.missing:
        text += f"；本机没有字节的 {len(images.missing)} 张（对方从图片库取）"
    return text


def _received_section(outcome: RunOutcome) -> list[str]:
    out = ["## 二、收进来的包"]
    if not outcome.packages:
        return out + ["- 没有收到别的机器的包", ""]
    out += ["| 来源 | 周 | 行数 | 新增 | 覆盖 | 冲突 | 图片 | 结果 |",
            "|---|---|---|---|---|---|---|---|"]
    for row in outcome.packages:
        out.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            row.repo, row.week_short, _num(row.rows), _num(row.inserted),
            _num(row.replaced), _num(row.conflicts), _images_cell(row.images), row.result))
    if outcome.merge_ran:
        missing_total = sum(result.images_missing for result in outcome.imports)
        out.append(f"- 图片：先拉图再插行，仍缺的如实记（本次 {missing_total} 张）")
    return out + [""]


def _gaps_section(outcome: RunOutcome) -> list[str]:
    out = ["## 三、缺口与还没来的"]
    wrote = False
    if not outcome.plan_known:
        out.append(f"- 本机没有本周（{outcome.week}）的落库计划：缺口与「还没来的包」"
                   "这次没法对照（先让采集这边的准备串落一次库，再来汇总）")
        wrote = True
    else:
        mine = [gap for gap in outcome.gaps if gap.machine == outcome.machine_id]
        if mine:
            first, *rest = mine
            out.append(f"- 本机缺{_day_count(len(mine))}：{first.shop_key} {first.date[5:]}"
                       f" —— {first.reason}（历史日期补不了，如实记一笔）")
            for gap in rest:
                out.append(f"- 本机还缺：{gap.shop_key} {gap.date[5:]} —— {gap.reason}"
                           "（历史日期补不了，如实记一笔）")
            wrote = True
        others = [gap for gap in outcome.gaps if gap.machine != outcome.machine_id]
        for machine, group in _by_machine(tuple(others)):
            out.append(f"- raw-{machine} 缺{_day_count(len(group))}："
                       f"{_gap_detail(group, reasons=False)} —— 计划里该采到，"
                       "收到的包里没有这一天（历史日期补不了，如实记一笔）")
            wrote = True
        if outcome.missing_machines:
            who = "、".join(f"raw-{machine}" for machine in outcome.missing_machines)
            out.append(f"- 还没收到的包：{who} 的 {outcome.week.split('-')[1]} "
                       "还没发布或没拉到")
            wrote = True
        if not wrote:
            out.append("- 没有缺口")
    if outcome.gaps:
        out.append("- 口径：缺口 = 计划里该采到的（店铺 × 日期），在收到的包里找不到")
    return out + [""]


def _conflicts_section(outcome: RunOutcome) -> list[str]:
    if not outcome.conflicts:
        return []                           # 干净场景不出现这一节（样例的形态）
    out = ["## 四、冲突"]
    for entry in outcome.conflicts:
        loser = _side_text(outcome, entry, entry.loser_machine, entry.loser_at)
        winner = _side_text(outcome, entry, entry.winner_machine, entry.winner_at)
        pick = (f"取后到者（{entry.winner_machine}）" if entry.winner_at > entry.loser_at
                else f"同时刻，取机器标识较大的（{entry.winner_machine}）")
        out.append(f"- 重复采集：{entry.shop_key} {entry.date[5:]}：{loser}与{winner} "
                   f"都采到；{pick}，覆盖 {entry.replaced} 行、保留 {entry.kept} 行")
    packages = sorted({name for entry in outcome.conflicts for name in entry.packages})
    out.append("- 口径：按（日期, 店铺）整组取胜，胜负由包内采集时刻定（同时刻比机器标识）"
               "——所以哪台机器先导入，算出来的库都一样")
    out.append(f"- 明细记在本机导入账（本机视角：导入 {'、'.join(packages) or '包'} 时发现）；"
               "冲突不入交换区")
    return out + [""]


def _todo_section(outcome: RunOutcome) -> list[str]:
    # 节号跟着「冲突」节的在不在走：干净场景没有第四节，待办就是「四」（样例一/二的样子）
    number = "五" if outcome.conflicts else "四"
    out = [f"## {number}、下次该做什么"]
    out += [f"{index}. {item}" for index, item in enumerate(outcome.todo, start=1)]
    return out
