"""开轮前的一次准备（spec §6）：拉计划库 → 同步清单 → 确认本周计划 → 落库 → 缺口告警。

界面打开时、命令行开跑前走的是同一步（`prepare_week`）；接线归票据 06/07，本模块只交
机制与判定：

- **确认**：本周计划在计划库里就只读它（发布后本周不重算）；不在就生成并发布——
  JSON 与 .md 同一次提交，另写 `published/<本机>.md`（各机各写各的，零冲突）。三台
  同时首次生成靠「先到先发布」：推送被拒（或没成功）的一方把克隆退回远端状态、重读
  已有计划。生成的输入只取**已发布的事实**（计划库里的共享清单、机器名册、历史计划
  文件）——这是三台各自算出同一份计划的前提。
- **落库**：整周指派写进本机计划表（`db.weekly_plan`，不入交换集），带来源与计划文件
  哈希（按归一化文本算——BOM/CRLF 差异不算另一份计划）；同周重跑幂等。
- **降级**（spec §6 表逐行，判定都在这里，界面/命令行入口见票据 07）：拉不到计划库 +
  本地已落库 → 用本地那份（`READY_FROM_CACHE`，即「未能确认最新」）；拉不到 + 本地没有
  → 默认拒绝开轮（`REFUSED`）并留显式逃生口；生成失败（pages 空缺点名、名册读不到）落到
  同样两行；计划文件在但读不动不猜也不覆盖，同样走降级；纯汇总机不检查、不生成、不发布；
  本机本周没店是合法空态（`idle`）。
- **缺口检查**：只读交换区（本机已有的包，不拉取），比对本机库缺哪些店哪些日，
  只告警不拦、不自动合并——汇总始终保持手工触发。

两个边界口径（本实现选定，spec 只说到这一步）：

- 落库只在「确认过一份发布物」时发生：发布没成功、远端也没有本周计划的那些情形不落库，
  否则下次离线重开会把一份谁都没发布的计划当成「本地已落库」的既成事实。
- 来源取 生成 / 拉取 由计划文件的 `generated_by` 判定（本机生成的就是「生成」，无论
  这一轮是刚生成还是重读到自己的发布物），「本地缓存」只属于降级到本机计划表的那一轮——
  同周重跑因此逐行幂等。

整周没有任何店铺的空计划在本机计划表里留不下行（表按「周 × 店铺」记）；这种周离线重开
会被当作「本地没有」拒绝——空计划本就没有可采的店，走逃生口也只是不采任何店。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import gzip
import json
import os
import pathlib
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from typing import Any

from bestseller_monitor import shops_sync, weekly_plan
from bestseller_monitor.canonical_text import normalized_text, text_digest
from bestseller_monitor.config import ROLE_MERGE_ONLY, load_shops
from bestseller_monitor.db import CST, Database, WeeklyPlanRow
from bestseller_monitor.git_channel import ChannelError, GitChannel
from bestseller_monitor.weekly_plan import PlanError

PLAN_REPO_DIR = "plan"          # 计划库克隆在交换区根下的目录名
PLAN_DIR = "plan"               # 计划文件在计划库里的目录（plan/<年>-W<周>.json）
PUBLISHED_DIR = "published"     # 各机各写各的发布记录
PACKAGE_SUFFIX = ".db.gz"       # raw 库里的周包（票据 08 的导出写这种名字，见 _package_week）

# 包文件名里的周号：`W39-m2.db.gz` 为准，spec 字面的裸周号 `39-m2.db.gz` 也认
# （年份由 data/<年> 目录钉住，机器标识是文件名末尾的后缀）。
_PACKAGE_WEEK = re.compile(r"^W?(\d{1,2})-")
# published/<本机>.md 的一行：本机自己写的表格，读回来只为「同周重发布时替换旧行」
_PUBLISHED_ROW = re.compile(r"^\|\s*(\d{4}-W\d{2})\s*\|\s*(\S+)\s*\|\s*\S+\s*\|\s*(\S*)\s*\|$")


class PlanSource(str, enum.Enum):
    """本周这份计划是怎么来的：拉取 / 本地缓存 / 生成（spec §6 的计划表字段）。"""

    PULLED = "pulled"
    LOCAL_CACHE = "local_cache"
    GENERATED = "generated"


class PrepStatus(str, enum.Enum):
    """开轮前准备的结局。"""

    READY = "ready"                          # 计划就位（读到了或刚生成发布）
    READY_FROM_CACHE = "ready_from_cache"    # 拉不到计划库，用本地已落库那份（未能确认最新）
    REFUSED = "refused"                      # 默认拒绝开轮，留显式逃生口
    SKIPPED_MERGE_ONLY = "skipped_merge_only"  # 纯汇总机：不检查、不生成、不发布


@dataclasses.dataclass(frozen=True)
class GapEntry:
    """本机缺的一个（店铺 × 日期）：交换区里已经有，本机库里没有。"""

    shop_key: str
    date: str
    machines: tuple[str, ...]     # 哪些机器的包覆盖了它（按机器标识排序）


@dataclasses.dataclass(frozen=True)
class GapScan:
    """一次缺口检查的结果；`missing` 非空时 `warning_message` 是要给人看的那段话。"""

    week: str
    packages: tuple[str, ...]     # 读进来的包（相对交换区根的路径）
    missing: tuple[GapEntry, ...]
    notes: tuple[str, ...]        # 有问题的注记（读不动的包等）；「还没有包」不算问题

    @property
    def warning_message(self) -> str | None:
        """缺口告警文本；没有缺口时为 None（只告警，从不拦开轮）。"""
        if not self.missing:
            return None
        lines = [f"缺口检查：本机库里还缺 {len(self.missing)} 个（店铺 × 日期），"
                 "交换区里已经有了——"]
        shown = self.missing[:20]
        lines += [f"- {e.shop_key} {e.date}（来自 {'、'.join(e.machines)} 的包）"
                  for e in shown]
        if len(self.missing) > len(shown):
            lines.append(f"…还有 {len(self.missing) - len(shown)} 个。")
        lines.append("跑一次数据交换台（汇总）可以补齐；本检查只告警，不拦开轮、不自动合并。")
        return "\n".join(lines)


@dataclasses.dataclass(frozen=True)
class PrepResult:
    """开轮前准备的结局与账目。

    `can_start` 就是开轮判定的答案；`REFUSED` 时 `escape_hatch_available` 为真——
    走逃生口（命令行 `--ignore-plan` / 界面确认）之后按「自由采集 + 记账为计划外」运行。
    """

    status: PrepStatus
    week: str
    machine: str
    source: PlanSource | None = None
    plan_sha256: str | None = None
    my_shops: tuple[str, ...] = ()          # 本周计划里归本机的店（按编号排序）
    idle: bool = False                      # 本机本周没店：合法正常态，不是报错
    warnings: tuple[str, ...] = ()
    reason: str | None = None               # REFUSED 的判定解释（也是给逃生口决策看的）
    shops_sync: shops_sync.SyncResult | None = None
    gaps: GapScan | None = None

    @property
    def can_start(self) -> bool:
        return self.status in (PrepStatus.READY, PrepStatus.READY_FROM_CACHE)

    @property
    def stale(self) -> bool:
        """拉不到计划库、用了本地缓存：界面要标注「未能确认最新」。"""
        return self.status is PrepStatus.READY_FROM_CACHE

    @property
    def escape_hatch_available(self) -> bool:
        return self.status is PrepStatus.REFUSED


@dataclasses.dataclass(frozen=True)
class _PlanOutcome:
    """确认本周计划的结果：拿到了哪份计划，或者为什么没拿到。"""

    document: Mapping[str, Any] | None
    source: PlanSource | None
    sha256: str | None
    note: str | None = None     # 没拿到时的原因；拿到但过程不顺时的说明


def _plan_text(path: pathlib.Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def _checked_plan(document: object, week: str, path: pathlib.Path) -> Mapping[str, Any]:
    """校验一份计划文件是能用的发布物；结构坏了点名拒绝，不猜。"""
    if not isinstance(document, Mapping):
        raise PlanError(f"计划文件 {path} 不是一个对象：{type(document).__name__}")
    if document.get("week") != week:
        raise PlanError(f"计划文件 {path} 的 week 是 {document.get('week')!r}，不是 {week}")
    snapshot: dict[str, tuple[str, int]] = {}
    for entry in document.get("shops") or ():
        key = entry.get("key") if isinstance(entry, Mapping) else None
        pages = entry.get("pages") if isinstance(entry, Mapping) else None
        name = entry.get("name") if isinstance(entry, Mapping) else None
        if not isinstance(key, str) or not key:
            raise PlanError(f"计划文件 {path} 的 shops 快照缺 key：{entry!r}")
        if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
            raise PlanError(f"计划文件 {path} 的 shops 快照里 {key} 的 pages 非法：{pages!r}")
        if key in snapshot:
            raise PlanError(f"计划文件 {path} 的 shops 快照里 {key} 出现两次")
        snapshot[key] = ("" if name is None else str(name), pages)
    assignments = document.get("assignments")
    if not isinstance(assignments, Mapping):
        raise PlanError(f"计划文件 {path} 缺少 assignments")
    for key, machine in assignments.items():
        if not isinstance(key, str) or not isinstance(machine, str) or not machine:
            raise PlanError(f"计划文件 {path} 的指派非法：{key!r} → {machine!r}")
        if key not in snapshot:
            raise PlanError(f"计划文件 {path}：assignments 里的 {key} 不在 shops 快照里")
    return document


def _read_plan_file(path: pathlib.Path, week: str) -> Mapping[str, Any] | None:
    """读一份已发布的计划文件；不存在返回 None，存在但读不动就点名报错。"""
    if not path.exists():
        return None
    try:
        text = _plan_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanError(f"计划文件读不动：{path}（{exc}）") from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise PlanError(f"计划文件不是合法 JSON：{path}（{exc}）") from exc
    return _checked_plan(document, week, path)


def _plan_rows(document: Mapping[str, Any]) -> list[WeeklyPlanRow]:
    """计划文件 → 本机计划表的整周行（店铺编号、名称、归谁、页数预算）。"""
    snapshot = {str(s["key"]): s for s in document["shops"]}
    return [WeeklyPlanRow(str(key), str(snapshot[key].get("name") or ""), str(machine),
                          int(snapshot[key]["pages"]))
            for key, machine in document["assignments"].items()]


def _source_of(document: Mapping[str, Any], machine_id: str) -> PlanSource:
    return (PlanSource.GENERATED if document.get("generated_by") == machine_id
            else PlanSource.PULLED)


def _read_roster(path: pathlib.Path) -> list[str]:
    """机器名册（machines.json）：顺序即平局顺序。读不到就点名拒绝。"""
    if not path.exists():
        raise PlanError(f"计划库里还没有机器名册 {path}：按上机清单第 11 步写一份 "
                        f'（如 ["m1","m2","m3"]）并推送，再开程序。')
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PlanError(f"机器名册读不动：{path}（{exc}）") from exc
    if not isinstance(data, list) or not all(isinstance(m, str) and m.strip() for m in data):
        raise PlanError(f"机器名册格式不对：{path}——应是一串机器标识，如 [\"m1\",\"m2\",\"m3\"]")
    return [m.strip() for m in data]


def _history_documents(plan_dir: pathlib.Path) -> list[Mapping[str, Any]]:
    """计划库里此前的全部计划文件（各周自包含），供分配算法读历史。"""
    documents: list[Mapping[str, Any]] = []
    for path in sorted(plan_dir.glob("*.json")) if plan_dir.is_dir() else ():
        try:
            document = json.loads(path.read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise PlanError(f"历史计划读不动：{path}（{exc}）") from exc
        documents.append(document)
    return documents


def _generate_documents(clone: pathlib.Path, machine_id: str, week: str,
                        now: dt.datetime) -> tuple[dict[str, Any], str, list[Mapping[str, Any]]]:
    """生成本周计划与它的 .md（输入只取计划库里已发布的事实）。"""
    shops_path = clone / shops_sync.SHARED_FILE_NAME
    if not shops_path.exists():
        raise PlanError(f"计划库里还没有共享店铺清单 {shops_path}：先让清单同步成功"
                        "（或按上机清单第 11 步把它推上去），再开程序。")
    shops = load_shops(shops_path)
    roster = _read_roster(clone / "machines.json")
    history = _history_documents(clone / PLAN_DIR)
    document = weekly_plan.generate_plan(
        shops, roster, history, week=week, generated_by=machine_id, generated_at=now)
    return document, weekly_plan.render_plan_md(document, history), history


def _read_published(path: pathlib.Path) -> dict[str, tuple[str, str]]:
    """读回本机自己的发布记录（这个文件只由本机写）：{周: (发布时间, 哈希前12位)}。"""
    if not path.exists():
        return {}
    entries: dict[str, tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _PUBLISHED_ROW.match(line.strip())
        if match:
            entries[match.group(1)] = (match.group(2), match.group(3))
    return entries


def _render_published(machine_id: str, entries: Mapping[str, tuple[str, str]]) -> str:
    lines = [f"# {machine_id} 发布的计划", "",
             "| 周 | 发布时间 | 计划文件 | 计划文件哈希 |", "|---|---|---|---|"]
    lines += [f"| {week} | {published_at} | plan/{week}.json | {sha[:12]} |"
              for week, (published_at, sha) in sorted(entries.items())]
    return "\n".join(lines) + "\n"


def _reread_after_failed_publish(channel: GitChannel, json_path: pathlib.Path,
                                 week: str, why: str) -> _PlanOutcome:
    """推送没成功后：退回远端状态、重读已有计划——先到先发布里我们就是后到的那一方。

    `clean=True` 是必须的：推送失败时本机刚写下的那三个文件可能还没提交（未跟踪），
    只 reset 清不掉——留着会被下面的重读当成「远端那份」，让一台机器拿一份谁都没发布的
    计划开工，正是本设计要避免的。
    """
    try:
        channel.reset_to_upstream(clean=True)
        channel.pull()
    except ChannelError as exc:
        return _PlanOutcome(None, None, None,
                            f"发布没成功（{why}），克隆也没能退回并刷新到远端状态（{exc}）——"
                            "现在读不到一份可确认的本周计划。")
    try:
        existing = _read_plan_file(json_path, week)
    except PlanError as exc:
        return _PlanOutcome(None, None, None, str(exc))
    if existing is None:
        return _PlanOutcome(None, None, None,
                            f"发布没成功（{why}），远端也还没有本周计划——"
                            "先修通道（git 凭据/网络），再开程序。")
    # 本机那笔提交已经退回、没了；远端现在这份只可能是别人先发布的
    return _PlanOutcome(existing, PlanSource.PULLED, text_digest(_plan_text(json_path)),
                        f"发布没成功（{why}）；已退回远端状态并重读已有计划，按它开工。")


def _publish(clone: pathlib.Path, machine_id: str, week: str, document: Mapping[str, Any],
             md_text: str, now: dt.datetime) -> _PlanOutcome:
    """写 JSON + .md + published/<本机>.md 三个文件，一次提交推上去。"""
    json_path = clone / PLAN_DIR / f"{week}.json"
    md_path = clone / PLAN_DIR / f"{week}.md"
    published_path = clone / PUBLISHED_DIR / f"{machine_id}.md"
    text = weekly_plan.plan_json(document)
    entries = _read_published(published_path)
    entries[week] = (now.isoformat(timespec="seconds"), text_digest(text))
    published_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_bytes(text.encode("utf-8"))
        md_path.write_bytes(md_text.encode("utf-8"))
        published_path.write_bytes(_render_published(machine_id, entries).encode("utf-8"))
    except OSError as exc:
        return _PlanOutcome(None, None, None, f"计划写不进计划库工作区：{exc}")
    channel = GitChannel(clone)
    try:
        channel.commit(f"publish plan {week}", [json_path, md_path, published_path])
        channel.push()
    except ChannelError as exc:
        return _reread_after_failed_publish(channel, json_path, week, str(exc))
    return _PlanOutcome(document, PlanSource.GENERATED, text_digest(text))


def _confirm_plan(clone: pathlib.Path, machine_id: str, week: str,
                  now: dt.datetime) -> _PlanOutcome:
    """确认本周计划：库里有就只读；没有就生成、发布（先到先发布）。"""
    json_path = clone / PLAN_DIR / f"{week}.json"
    try:
        existing = _read_plan_file(json_path, week)
    except PlanError as exc:
        return _PlanOutcome(None, None, None, f"本周计划在计划库里但读不动，不猜也不覆盖：{exc}")
    if existing is not None:
        return _PlanOutcome(existing, _source_of(existing, machine_id),
                            text_digest(_plan_text(json_path)))
    try:
        document, md_text, _history = _generate_documents(clone, machine_id, week, now)
    except PlanError as exc:
        return _PlanOutcome(None, None, None, f"生成本周计划失败：{exc}")
    return _publish(clone, machine_id, week, document, md_text, now)


def _preserve_debris(clone: pathlib.Path, sidecar: pathlib.Path) -> str | None:
    """把克隆工作区（除 .git）整棵拷到旁路目录；拷不动就返回原因。

    残迹里可能有人的东西（比如照上机清单在克隆里改了 machines.json 还没提交），
    所以收拾前先留一份：全拷比逐个挑改动便宜，计划库本来就不大。
    """
    try:
        sidecar.mkdir(parents=True)
        for item in clone.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir():
                shutil.copytree(item, sidecar / item.name)
            else:
                shutil.copy2(item, sidecar / item.name)
    except OSError as exc:
        return str(exc)
    return None


def _heal_dirty_clone(clone: pathlib.Path, warnings: list[str]) -> None:
    """克隆里有没提交完的残迹时退回远端状态（收拾前先整棵旁路留存）。

    准备串里的 pull 在有未提交改动时直接失败；上一次发布中途崩掉就会留下这种克隆，
    不收拾的话这台机器每次开程序都卡在「拉不动」。克隆只由本机程序写（写进去的都
    立刻提交、失败就退回），未跟踪的也是崩在半途的残迹；但残迹里可能有人的东西，
    所以先复制到旁路目录再退——留存里没有要留的，人自己把那个目录删掉即可。
    """
    if not (clone / ".git").exists():
        return
    channel = GitChannel(clone)
    try:
        changes = channel.local_changes()
    except ChannelError as exc:
        warnings.append(f"计划库克隆的残迹自检没做成：{exc}")
        return
    if not changes:
        return
    shown = "、".join(changes[:5]) + ("…" if len(changes) > 5 else "")
    sidecar = clone.parent / f"plan-残迹-{dt.datetime.now(CST).strftime('%Y%m%d-%H%M%S')}"
    note = _preserve_debris(clone, sidecar)
    if note:
        warnings.append(f"计划库克隆里有没提交完的残迹（{shown}），没能先整棵存到旁路"
                        f"（{note}）——这次不动它，先自己看一眼再开程序。")
        return
    try:
        channel.reset_to_upstream(clean=True)
    except ChannelError as exc:
        warnings.append(f"计划库克隆里有没提交完的残迹（{shown}），没能退回远端状态：{exc}\n"
                        f"残迹已整棵存到 {sidecar}。")
        return
    warnings.append(f"计划库克隆里有上次没提交完的残迹（可能是发布中途崩过）：{shown}。\n"
                    f"已整棵复制到 {sidecar} 后退回远端状态；留存里没有要留的就把它删掉。")


def _package_week(name: str) -> int | None:
    """包文件名里的周号（`W39-m2.db.gz`，spec 字面的裸周号也认）；认不出返回 None。"""
    if not name.endswith(PACKAGE_SUFFIX):
        return None
    match = _PACKAGE_WEEK.match(name[:-len(PACKAGE_SUFFIX)])
    return int(match.group(1)) if match else None


def _package_pairs(path: pathlib.Path, start: dt.date, end: dt.date) -> list[tuple[str, str]]:
    """一个包覆盖的（店铺 × 日期）集合：解到临时文件，只读查询。"""
    handle, tmp_name = tempfile.mkstemp(suffix=".db", prefix="bestseller-package-")
    os.close(handle)
    try:
        with gzip.open(path, "rb") as src, open(tmp_name, "wb") as dst:
            shutil.copyfileobj(src, dst)
        conn = sqlite3.connect(tmp_name)
        try:
            rows = conn.execute(
                "SELECT DISTINCT shop_key, date FROM inventory WHERE date>=? AND date<=?",
                (start.isoformat(), end.isoformat())).fetchall()
        finally:
            conn.close()
        return [(str(key), str(day)) for key, day in rows]
    finally:
        pathlib.Path(tmp_name).unlink(missing_ok=True)


def scan_exchange_gaps(exchange_root: str | pathlib.Path, local: sqlite3.Connection,
                       week: str) -> GapScan:
    """只读交换区，比对本机缺哪些店哪些日：包里有、本机库里没有的，就是本机落后的部分。

    只看本机交换区里**已经有**的包（`raw-<机器>/data/<年>/W<周>-<机器>.db.gz`，不拉取），
    以及本周的整周日期范围；读不动的包记进 `notes` 跳过，不抛错——这个检查只告警。
    """
    monday, sunday = weekly_plan.week_window(week)
    iso = monday.isocalendar()
    # 年取周编号里的那个 ISO 年：2026-W01 的周一落在 2025-12-29，目录仍是 data/2026/
    # （与票据 08 的导出口径一致）。
    year_dir = str(iso.year)
    week_no = iso.week

    packages: list[str] = []
    covered: dict[tuple[str, str], set[str]] = {}
    notes: list[str] = []
    for repo in sorted(pathlib.Path(exchange_root).glob("raw-*")):
        machine = repo.name[len("raw-"):]
        if not repo.is_dir() or not machine:
            continue
        for path in sorted((repo / "data" / year_dir).glob(f"*{PACKAGE_SUFFIX}")):
            rel = f"{repo.name}/data/{year_dir}/{path.name}"
            if _package_week(path.name) != week_no or \
                    not path.name[:-len(PACKAGE_SUFFIX)].endswith(f"-{machine}"):
                continue
            try:
                pairs = _package_pairs(path, monday, sunday)
            except (OSError, EOFError, sqlite3.Error) as exc:
                notes.append(f"跳过读不动的包 {rel}：{exc}")
                continue
            packages.append(rel)
            for pair in pairs:
                covered.setdefault(pair, set()).add(machine)

    local_pairs = {(str(row["shop_key"]), str(row["date"])) for row in local.execute(
        "SELECT DISTINCT shop_key, date FROM inventory WHERE date>=? AND date<=?",
        (monday.isoformat(), sunday.isoformat()))}
    missing = tuple(GapEntry(shop_key=key, date=day, machines=tuple(sorted(machines)))
                    for (key, day), machines in sorted(covered.items())
                    if (key, day) not in local_pairs)
    return GapScan(week=week, packages=tuple(packages), missing=missing, notes=tuple(notes))


def _fallback_reason(outcome: _PlanOutcome | None, sync: shops_sync.SyncResult | None,
                     sync_error: str | None, clone: pathlib.Path, week: str) -> str:
    """没确认到本周计划的原因（降级说明与拒绝理由共用）。"""
    if outcome is not None and outcome.note:
        return outcome.note
    if sync_error:
        return sync_error
    if sync is not None:
        return sync.message
    return f"计划库还没有 clone 到 {clone}（上机清单第 9 步）：本周（{week}）确认不了。"


def prepare_week(cfg, db: Database, *, now: dt.datetime | None = None,
                 state_path: str | pathlib.Path | None = None) -> PrepResult:
    """开轮前的一次准备：走完「拉计划库 → 同步清单 → 确认计划 → 落库 → 缺口告警」，
    返回结局与判定（开不开轮由调用方照 `PrepResult` 决定，逃生口入口见票据 07）。

    `cfg` 取 `machine_id` / `role` / `shop_csv` / `exchange_root`（其余键不读）。
    """
    now = now or dt.datetime.now(CST)
    week = weekly_plan.iso_week_label(now.date())
    machine = str(cfg.machine_id)
    if cfg.role == ROLE_MERGE_ONLY:
        return PrepResult(status=PrepStatus.SKIPPED_MERGE_ONLY, week=week, machine=machine)

    clone = pathlib.Path(cfg.exchange_root) / PLAN_REPO_DIR
    warnings: list[str] = []
    _heal_dirty_clone(clone, warnings)

    sync: shops_sync.SyncResult | None = None
    sync_error: str | None = None
    try:
        sync = shops_sync.sync_shared_shops(cfg.shop_csv, clone, state_path=state_path)
    except shops_sync.ShopsSyncError as exc:
        sync_error = str(exc)
    if sync is not None and sync.action is not shops_sync.SyncAction.IN_SYNC:
        warnings.append(sync.message)
    if sync_error:
        warnings.append(sync_error)

    repo_ok = sync is not None and sync.action is not shops_sync.SyncAction.UNREACHABLE
    outcome = _confirm_plan(clone, machine, week, now) if repo_ok else None

    source: PlanSource | None = None
    reason: str | None = None
    if outcome is not None and outcome.document is not None:
        if outcome.note:
            warnings.append(outcome.note)
        source = outcome.source
        db.replace_weekly_plan(week, _plan_rows(outcome.document), source=source.value,
                               plan_sha256=outcome.sha256,
                               stored_at=now.isoformat(timespec="seconds"))
        status = PrepStatus.READY
    else:
        stored = db.weekly_plan(week)
        why = _fallback_reason(outcome, sync, sync_error, clone, week)
        if stored:
            status = PrepStatus.READY_FROM_CACHE
            source = PlanSource.LOCAL_CACHE
            db.replace_weekly_plan(week,
                                   [WeeklyPlanRow(r["shop_key"], r["shop_name"],
                                                  r["machine_id"], r["pages"]) for r in stored],
                                   source=source.value, plan_sha256=stored[0]["plan_sha256"],
                                   stored_at=now.isoformat(timespec="seconds"))
            warnings.append(f"用本地已落库的本周（{week}）计划继续——发布后本周不重算，"
                            f"本地即权威；未能确认最新。原因：{why}")
        else:
            status = PrepStatus.REFUSED
            reason = (
                f"{why}\n本机也没有本周（{week}）已落库的计划：默认拒绝开轮——三台各自"
                "脱网时都自由采集，会把同一批店三台各打一遍，正是分片设计要避免的。\n"
                "要照常开轮，走显式逃生口（命令行 --ignore-plan / 界面确认），按"
                "「自由采集 + 记账为计划外」运行。")

    try:
        gaps = scan_exchange_gaps(cfg.exchange_root, db.conn, week)
    except Exception as exc:            # 只告警不拦：缺口检查坏了也不该挡住开轮
        warnings.append(f"缺口检查没做成（不影响开轮）：{exc}")
        gaps = None
    else:
        if gaps.warning_message:
            warnings.append(gaps.warning_message)
        if gaps.notes:
            warnings.append("缺口检查注记：" + "；".join(gaps.notes))

    rows = db.weekly_plan(week)
    my_shops = tuple(r["shop_key"] for r in rows if r["machine_id"] == machine)
    return PrepResult(
        status=status, week=week, machine=machine, source=source,
        plan_sha256=rows[0]["plan_sha256"] if rows else None,
        my_shops=my_shops,
        idle=status in (PrepStatus.READY, PrepStatus.READY_FROM_CACHE) and not my_shops,
        warnings=tuple(warnings), reason=reason, shops_sync=sync, gaps=gaps,
    )
