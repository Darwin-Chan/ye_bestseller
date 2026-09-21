"""三机验收口径的机械化核对（spec「三机验收口径」；票据 13）。

第一期真实运行结束后，把交换区与各机库的产物丢给这个工具，五条口径逐条给判定——
真机上两台库在两台电脑里，人眼对不了「同一 (日期, 店铺) 的取胜结果一致」。

用法（在任一台上，先把要对照的库文件拷到一起）：

    python tools/acceptance_check.py --week 2026-W40 \\
        --exchange-root F:/AI/bestseller_runtime/exchange \\
        --machine m1=F:/AI/bestseller_runtime/data/bestseller.db \\
        --machine m2=D:/验收/m2.db --machine m3=D:/验收/m3.db \\
        --merge-only m4=D:/验收/m4.db

五条口径（票面原文 → 这一版的可执行读法）：

1. **计划核对行**：读计划库克隆里已发布的 `plan/<周>.md`（人核对的那份），
   「相邻周同机器 N 处」必须为 0。计划没发布就没得验收，直接不过。
2. **本机份额 × 轮次对照**：对每台给了库的采集机，拿已发布的计划文件里归它的
   （店铺 × 日期），对照本机库的轮次事实（`rounds.run_date × shop_rounds`，票面
   口径；库里有行数并列展示）——缺的那些就是缺口，逐条带当天轮次终态当说明
   （与周报同一条推导）。唯一的不通过：缺口那天轮次还挂着没终态（半截的一天）。
3. **周包与「还没来的包」**：计划名册里的每台采集机，本周包都得在交换区里；
   缺席的必须在本机的周报里被点名（「还没收到的包」那一行）——报告都没跑就没有
   验收可言。报告是本机视角的：这条核的是**跑工具这台**的那份周报；想要三台的
   报告都被核，就在三台各跑一次这个命令（或把别机的报告拷到对应交换区根的
   `报告/` 下再跑）。
4. **任取两台：合并收敛一致**：拿到的库两两对照——交换集里承载取胜结果的五张表
   逐表逐行比对（版本表除 `id`：它是本机 rowid，导入会重排，spec §9 的已知约束；
   图片资产表与各机的账不参与，见 `_COMPARE_TABLES` 的注释）。数据逐行一致 ⇒
   同一份数据上分析确定性地产出同一份结果，这条不重跑分析——验收时人工在任两台
   各跑一次 `analyze.py` 对一眼即可。
5. **纯汇总机冷启动重放**：`--merge-only` 的那台库里，幂等账要覆盖交换区里的
   全部历史包（「收进来的包」= 交换区全部）；交换集各表有数；图片按清单补齐——
   版本行引用到的每个内容哈希，本机资产表里都得有字节（缺的列出来；若是源头
   本机就没字节的（各机周报里有记录），人工确认后按缺图记账）。

输入给不全的条目记「跳过」并在末尾写明缺什么；退出码 0 = 没有不过的（含跳过），
1 = 有不过的，2 = 用法或输入错（比如 --week 非法、`编号=路径` 形态不对）。
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import pathlib
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bestseller_monitor import exchange, export, merge, weekly_plan  # noqa: E402
from bestseller_monitor.db import Database, cst_date  # noqa: E402
from bestseller_monitor.weekly_plan import PlanError  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

_STATUS_GLOSS = {PASS: "通过", FAIL: "不过", SKIP: "跳过"}

# 计划 .md 末尾核对行里的那个数（`render_plan_md` 写的形态：「相邻周同机器 0 处」）
_VIOLATIONS = re.compile(r"相邻周同机器\s*(\d+)\s*处")


@dataclasses.dataclass(frozen=True)
class Criterion:
    """一条口径的判定：状态 + 给人看的明细行。"""

    title: str
    status: str
    lines: tuple[str, ...] = ()


def _plan_md_path(plan_clone: pathlib.Path, week: str) -> pathlib.Path:
    return pathlib.Path(plan_clone) / "plan" / f"{week}.md"


def check_plan_line(plan_clone: pathlib.Path, week: str) -> Criterion:
    """口径一：计划核对行「相邻周同机器 0 处」（读计划库里已发布的 .md）。"""
    title = "计划核对行"
    md_path = _plan_md_path(plan_clone, week)
    if not md_path.exists():
        return Criterion(title, FAIL, (
            f"计划库里没有 {week} 已发布的计划（找不到 {md_path}）："
            "先让采集这边生成并发布本周计划（上机清单第 12 步），再来验收。",))
    try:
        text = md_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return Criterion(title, FAIL, (f"计划 .md 读不动：{md_path}（{exc}）",))
    match = _VIOLATIONS.search(text)
    if match is None:
        return Criterion(title, FAIL, (
            f"{md_path} 里找不到核对行（「相邻周同机器 N 处」）："
            "这不是一份认识的计划 .md，人工看一眼。",))
    count = int(match.group(1))
    line = match.group(0)
    if count == 0:
        return Criterion(title, PASS, (f"核对行：{line}",))
    return Criterion(title, FAIL, (
        f"核对行：{line} —— 口径要求 0 处；0 处的意思是三台按同一份已发布计划"
        "各自完成本机份额，没有一家店被同一台机器连着采（风险分散约束）。",))


def _read_plan_document(plan_clone: pathlib.Path, week: str) -> tuple[dict | None, str | None]:
    """读计划库里这一周已发布的计划文件；返回 (文档, 没读到时的说明)。"""
    path = pathlib.Path(plan_clone) / "plan" / f"{week}.json"
    if not path.exists():
        return None, (f"计划库里没有 {week} 已发布的计划（找不到 {path}）："
                      "先让采集这边生成并发布本周计划（上机清单第 12 步），再来验收。")
    try:
        document = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"计划文件读不动：{path}（{exc}）"
    if not isinstance(document, dict) or document.get("week") != week:
        return None, f"计划文件不是一份 {week} 的计划：{path}"
    return document, None


def _open_read_only(db_path: pathlib.Path) -> sqlite3.Connection:
    """只读打开一份库文件（验收核对不写任何库；正在采集的库也能安全读）。"""
    uri = f"{pathlib.Path(db_path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def check_share(plan_clone: pathlib.Path, week: str, machine_id: str, db_path: pathlib.Path,
                *, as_of: str | None = None) -> Criterion:
    """口径二：本机份额 × 轮次对照（缺口逐条有说明）。

    份额取已发布计划文件里归这台机器的店；「采到没有」对照本机库的轮次事实
    （`rounds.run_date × shop_rounds`）；库里有行数并列展示。缺口带当天轮次终态
    当说明（与周报同一处推导，`exchange.gap_reason`）；哪天有轮次还挂着没终态
    （半截的一天）就不算完成，这条不过。
    """
    title = "本机份额 × 轮次对照"
    document, why = _read_plan_document(plan_clone, week)
    if document is None:
        return Criterion(title, FAIL, (why,))
    assignments = document.get("assignments")
    if not isinstance(assignments, dict):
        return Criterion(title, FAIL, (f"计划文件缺 assignments：{plan_clone}",))

    mine = sorted(str(key) for key, machine in assignments.items() if machine == machine_id)
    as_of_date = dt.date.fromisoformat(as_of or cst_date())
    days = tuple(day.isoformat() for day in exchange.week_days(week, as_of_date))
    roster = [str(machine) for machine in document.get("machines") or ()]
    lines = [f"{machine_id} 本周份额：{len(mine)} 家店 × {len(days)} 天 "
             f"= {len(mine) * len(days)} 个（店铺 × 日期）"
             + ("" if machine_id in roster else "（注意：这台不在本周计划的名册里）")]
    if not mine:
        lines.append("本周空手：计划里没有归本机的店（合法状态，不是错误）")
        return Criterion(title, PASS, tuple(lines))
    if not days:
        lines.append(f"本周还没有该采到的天（截止 {as_of or cst_date()}）：无从对照")
        return Criterion(title, PASS, tuple(lines))

    try:
        conn = _open_read_only(db_path)
    except sqlite3.Error as exc:
        return Criterion(title, FAIL, (*lines, f"库文件打不开：{db_path}（{exc}）"))
    try:
        db = Database(conn)
        span = (days[0], days[-1])
        covered: set[tuple[str, str]] = set()
        open_days: set[str] = set()
        notes: list[str] = []
        for row in conn.execute(
                "SELECT r.id, r.run_date, r.terminal_reason, s.shop_key, s.list_status, "
                "s.list_note FROM rounds r JOIN shop_rounds s ON s.round_id = r.id "
                "WHERE r.run_date BETWEEN ? AND ? ORDER BY r.id, s.shop_key", span):
            key = (str(row["shop_key"]), str(row["run_date"]))
            if key[0] not in mine:
                continue
            if row["terminal_reason"] is None:
                open_days.add(key[1])
            covered.add(key)
            if str(row["list_status"]) != "完成":
                note = f"；{row['list_note']}" if row["list_note"] else ""
                notes.append(f"注记：{key[0]} {key[1][5:]} 轮次 #{row['id']} 里该店榜单"
                             f"状态={row['list_status']}{note}")
        rows_count = {(str(row["shop_key"]), str(row["date"])): int(row["n"])
                      for row in conn.execute(
                          "SELECT shop_key, date, COUNT(*) AS n FROM inventory "
                          "WHERE date BETWEEN ? AND ? GROUP BY shop_key, date", span)}

        cells = [(shop, day) for shop in mine for day in days]
        gaps = [(shop, day) for shop, day in cells if (shop, day) not in covered]
        lines.append(f"轮次覆盖 {len(cells) - len(gaps)}/{len(cells)}：缺口 {len(gaps)} 条")
        for shop, day in gaps[:20]:
            lines.append(f"缺口：{shop} {day[5:]} —— {exchange.gap_reason(db, day)}"
                         f"（库里有 {rows_count.get((shop, day), 0)} 行）")
        if len(gaps) > 20:
            lines.append(f"…还有 {len(gaps) - 20} 条缺口（同类形态）。")
        lines += notes[:20]
        if len(notes) > 20:
            lines.append(f"…还有 {len(notes) - 20} 条注记。")
    finally:
        conn.close()

    if open_days:
        lines.append("不通过：这些天有轮次还挂着没终态（半截的一天），验收请在一周跑完"
                     "、轮次都收尾之后再做：" + "、".join(sorted(open_days)))
        return Criterion(title, FAIL, tuple(lines))
    return Criterion(title, PASS, tuple(lines))


def check_packages(exchange_root: pathlib.Path, week: str,
                   plan_clone: pathlib.Path | None = None) -> Criterion:
    """口径三：周包与「还没来的包」。

    计划名册里的每台采集机本周都该有自己的包；缺席的必须在本机周报里被点名
    （「还没收到的包」那一节）——「为空或已有说明」两种都算过，但本机这周的周报
    必须在（三台各自至少跑一次交换台是这条的前提）。包名与包内容（元数据里的
    机器、周）对不上的当场不过：那种文件比缺席更危险。
    """
    title = "周包与「还没来的包」"
    if plan_clone is None:
        return Criterion(title, SKIP, (
            "没给 --plan：名册里该有哪些机器无从得知（这条要对照已发布计划的名册）。",))
    document, why = _read_plan_document(plan_clone, week)
    if document is None:
        return Criterion(title, FAIL, (why,))
    roster = [str(machine) for machine in document.get("machines") or ()]
    if not roster:
        return Criterion(title, FAIL, ("计划文件里没有 machines 名册。",))

    root = pathlib.Path(exchange_root)
    refs = export.week_packages(root, week)
    by_machine = {ref.machine: ref for ref in refs}
    lines: list[str] = []
    problems: list[str] = []
    for ref in refs:
        try:
            with merge.open_package(ref.path) as pkg:
                meta = export.read_package_meta(pkg)
        except (OSError, EOFError, sqlite3.Error, KeyError, ValueError) as exc:
            problems.append(f"{ref.rel} 读不动：{exc}")
            continue
        if str(meta["machine_id"]) != ref.machine or str(meta["week"]) != week:
            problems.append(f"{ref.rel} 的包内容对不上包名：包里写着 "
                            f"{meta['machine_id']} 的 {meta['week']}")
    missing = [machine for machine in roster if machine not in by_machine]
    for machine in sorted(set(by_machine) - set(roster)):
        lines.append(f"注记：raw-{machine} 发了 {week} 的包，但不在本周计划的名册里。")
    lines.append(f"计划名册 {len(roster)} 台：本周包在交换区里 {len(roster) - len(missing)}/"
                 f"{len(roster)}" + ("（" + "、".join(f"raw-{m}" for m in missing) + " 缺席）"
                                     if missing else ""))

    report_path = root / exchange.REPORT_DIR / f"{week}.md"
    if not report_path.exists():
        return Criterion(title, FAIL, (*lines, *problems,
            f"本机这周还没有周报（{report_path}）：三台各自至少跑一次交换台"
            "是这条的前提，跑完再来验收。"))
    text = report_path.read_text(encoding="utf-8", errors="replace")
    # 「已点名」只认「还没收到的包」那一行（周报里唯一的出处；整个报告做子串匹配会把
    # 「收进来的包」表里的来源列 raw-xx 也算上，那就把「没说明」误判成「已说明」了）
    named = "".join(line for line in text.splitlines()
                    if line.strip().startswith("- 还没收到的包："))
    for machine in missing:
        if f"raw-{machine}" in named:
            lines.append(f"raw-{machine} 的包缺席——周报里已点名（「还没收到的包」），"
                         "按「已有说明」过。")
        else:
            problems.append(f"raw-{machine} 的包缺席，周报里也没点名："
                            "重跑一次本机交换台（汇总）再看这周的周报。")
    if problems:
        return Criterion(title, FAIL, (*lines, *problems))
    return Criterion(title, PASS, tuple(lines))


# 收敛对照的表集：交换集里承载「取胜结果」的五张。两张表故意不在这里——版本表的 `id`
# 列（本机 rowid，导入会重排，spec §9 的已知约束）、图片资产表（行有无反映的是图片
# 拉到没拉到，归口径五与各机周报的「缺图」记录看）。本机的账（取胜方账 / 幂等账 /
# 冲突账）不参与对照：本机自己采的组不进取胜方账，两台的账本来就长得不一样。
_COMPARE_TABLES = (
    ("shops", (), ("shop_key",)),
    ("products", (), ("offer_id",)),
    ("skus", (), ("offer_id", "sku_id")),
    ("inventory", (), ("shop_key", "date")),
    ("product_information_versions", ("id",), ("shop_key", "observed_date")),
)


@dataclasses.dataclass(frozen=True)
class _Dump:
    """一份库一张表的规范形态：列名 + 逐行元组（已按列排序）。"""

    label: str
    table: str
    cols: tuple[str, ...]
    rows: tuple[tuple, ...]


def _dump_table(conn: sqlite3.Connection, label: str, table: str,
                excluded: tuple[str, ...]) -> _Dump:
    """读一张表成规范形态；缺表当场报错——那是「不是这份库」的问题，不是数据差异。

    列按**列名排序**取：老库与新库的列顺序可以不同（真库的产品表就与新库不同序），
    按名字对齐两边才可比；行序也由同一列序定。
    """
    info = list(conn.execute(f'PRAGMA table_info("{table}")'))
    if not info:
        raise LookupError(f"{label} 的库里没有 {table} 表")
    cols = tuple(sorted(row["name"] for row in info if row["name"] not in excluded))
    order = ", ".join(f'"{col}"' for col in cols)
    rows = tuple(tuple(row) for row in
                 conn.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}'))
    return _Dump(label=label, table=table, cols=cols, rows=rows)


def _diff_table(left: _Dump, right: _Dump, group_cols: tuple[str, ...]) -> list[str]:
    """两台库一张表的差异，按分组列（店铺 × 日期这类）逐组说明；一致返回空。"""
    if set(left.cols) != set(right.cols):
        only_left = "、".join(sorted(set(left.cols) - set(right.cols))) or "（无）"
        only_right = "、".join(sorted(set(right.cols) - set(left.cols))) or "（无）"
        return [f"{left.table}：两台库的列集不同——{left.label} 多 {only_left}；"
                f"{right.label} 多 {only_right}（程序与库要一起升）。"]
    index = [left.cols.index(col) for col in group_cols]
    groups: dict[tuple, list[list[tuple]]] = {}
    for dump, slot in ((left, 0), (right, 1)):
        for row in dump.rows:
            groups.setdefault(tuple(row[i] for i in index), [[], []])[slot].append(row)
    out: list[str] = []
    for key in sorted(groups, key=lambda k: tuple(str(v) for v in k)):
        mine, theirs = groups[key]
        if Counter(mine) == Counter(theirs):
            continue
        shown = "、".join(str(v) for v in key)
        out.append(f"{left.table}：{shown} 上的行不一致（{left.label} {len(mine)} 行 / "
                   f"{right.label} {len(theirs)} 行）")
    return out


def _compare_pair(label_a: str, path_a: pathlib.Path, label_b: str,
                  path_b: pathlib.Path) -> tuple[list[str], list[str]]:
    """两台库逐表对照；返回 (差异行, 小计行)。"""
    conns: list[sqlite3.Connection] = []
    try:
        for path in (path_a, path_b):
            conns.append(_open_read_only(path))
    except sqlite3.Error as exc:
        for conn in conns:
            conn.close()
        return ([f"{label_a} × {label_b}：库文件打不开（{exc}）"], [])
    diffs: list[str] = []
    tallies: list[str] = []
    try:
        for table, excluded, group_cols in _COMPARE_TABLES:
            try:
                left = _dump_table(conns[0], label_a, table, excluded)
                right = _dump_table(conns[1], label_b, table, excluded)
            except LookupError as exc:
                diffs.append(str(exc))
                continue
            table_diffs = _diff_table(left, right, group_cols)
            if table_diffs:
                diffs += table_diffs[:10]
                if len(table_diffs) > 10:
                    diffs.append(f"{table}：…还有 {len(table_diffs) - 10} 处不一致。")
            else:
                tallies.append(f"{table} {len(left.rows)} 行一致")
    finally:
        for conn in conns:
            conn.close()
    return diffs, tallies


def check_convergence(dbs: Mapping[str, pathlib.Path]) -> Criterion:
    """口径四：任取两台，同一批包合并后的取胜结果一致。

    `dbs` 是 标签 → 库文件路径 的映射，两两对照：交换集里承载取胜结果的五张表
    逐表逐行比对（版本表除 `id`）。数据逐行一致 ⇒ 同一份数据上分析确定性地产出
    同一份结果。
    """
    title = "任取两台：合并收敛一致"
    items = list(dbs.items())
    if len(items) < 2:
        return Criterion(title, SKIP, (
            "只给了不到两台库：把要对照的库文件（含拷来的）都带上，再来对照。",))
    lines: list[str] = []
    problems: list[str] = []
    for index, (label_a, path_a) in enumerate(items):
        for label_b, path_b in items[index + 1:]:
            diffs, tallies = _compare_pair(label_a, path_a, label_b, path_b)
            if diffs:
                problems.append(f"{label_a} × {label_b}：取胜结果不一致——")
                problems += ["  " + line for line in diffs]
            else:
                lines.append(f"{label_a} × {label_b}：逐表一致（{'；'.join(tallies)}）")
    if problems:
        return Criterion(title, FAIL, (*lines, *problems))
    lines.append("数据逐行一致 ⇒ 两台跑分析得到同一份畅销品结果（验收时各跑一次 "
                 "analyze.py 对一眼即可）。")
    return Criterion(title, PASS, tuple(lines))


# 冷启动「各表有数」的两组：五张核心表必须非空；图片资产表（有字节的）只报数。
_CORE_TABLES = ("shops", "products", "skus", "inventory", "product_information_versions")


def check_cold_start(db_path: pathlib.Path, exchange_root: pathlib.Path) -> Criterion:
    """口径五：纯汇总机冷启动重放。

    幂等账要覆盖交换区里的**全部历史包**（「收进来的包」= 交换区全部）；交换集各表
    有数；图片按清单补齐——版本行引用到的每个内容哈希，本机资产表里都得有字节。
    缺图的可能是源头本机就没有字节的那几张（各机周报里记着缺图），列出来人工确认，
    这条先按不过算。
    """
    title = "纯汇总机冷启动重放"
    root = pathlib.Path(exchange_root)
    packages = export.all_packages(root)          # 与交换台同一个「什么算包」的口径
    try:
        conn = _open_read_only(db_path)
    except sqlite3.Error as exc:
        return Criterion(title, FAIL, (f"库文件打不开：{db_path}（{exc}）",))
    try:
        known = {str(row["package_sha256"])
                 for row in conn.execute("SELECT package_sha256 FROM import_packages")}
        counts = {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                  for table in _CORE_TABLES}
        span = conn.execute("SELECT MIN(date), MAX(date) FROM inventory").fetchone()
        with_bytes = int(conn.execute(
            "SELECT COUNT(*) FROM product_image_assets WHERE content IS NOT NULL").fetchone()[0])
        referenced = {str(row["content_hash"]) for row in conn.execute(
            "SELECT DISTINCT content_hash FROM product_information_versions "
            "WHERE content_hash IS NOT NULL")}
        have = {str(row["content_hash"]) for row in conn.execute(
            "SELECT content_hash FROM product_image_assets WHERE content IS NOT NULL")}
    except sqlite3.Error as exc:
        return Criterion(title, FAIL, (f"库里读不动：{exc}",))
    finally:
        conn.close()

    lines: list[str] = []
    problems: list[str] = []
    missing_packages: list[export.PackageRef] = []
    for ref in packages:
        try:
            sha = merge.package_sha256(ref.path)
        except OSError as exc:                    # 包读不动也是「没收进来」，如实报
            missing_packages.append(ref)
            problems.append(f"读不动的包：{ref.rel}（{exc}）")
            continue
        if sha not in known:
            missing_packages.append(ref)
    lines.append(f"交换区里的历史包 {len(packages)} 个：幂等账收进来 "
                 f"{len(packages) - len(missing_packages)}/{len(packages)}")
    for ref in missing_packages[:10]:
        problems.append(f"没收进来的包：{ref.rel}"
                        "（重跑一次本机的交换台（汇总）即可补齐）")
    if len(missing_packages) > 10:
        problems.append(f"…还有 {len(missing_packages) - 10} 个包没收进来。")

    empty = [table for table, count in counts.items() if count == 0]
    span_text = f"（{span[0]}..{span[1]}）" if span and span[0] else "（空）"
    lines.append("交换集行数：" + " · ".join(f"{table} {count}" for table, count in counts.items())
                 + f" · 图片资产（有字节）{with_bytes}")
    lines.append(f"inventory 覆盖：{span_text}——对一眼起点（m1 补发的历史应从接入时点起）")
    for table in empty:
        problems.append(f"交换集表 {table} 是空的：「各表有数」不过（重放没重放全，"
                        "或交换区里确实还没有数据）。")
    missing_images = sorted(referenced - have)
    lines.append(f"图片：版本行引用 {len(referenced)} 个内容哈希，本机有字节 "
                 f"{len(referenced) - len(missing_images)} 个")
    if missing_images:
        shown = "、".join(hash[:12] for hash in missing_images[:5])
        problems.append(f"缺图 {len(missing_images)} 张（{shown}…）：「图片按清单补齐」不过。"
                        "可能是源头本机就没有字节的那几张（各机周报的「缺图」里有记录），"
                        "人工确认后再算数。")
    if problems:
        return Criterion(title, FAIL, (*lines, *problems))
    return Criterion(title, PASS, tuple(lines))


@dataclasses.dataclass(frozen=True)
class Acceptance:
    """一次验收核对的判定表。"""

    week: str
    criteria: tuple[Criterion, ...]

    @property
    def failed(self) -> bool:
        return any(criterion.status == FAIL for criterion in self.criteria)


def run_checks(*, week: str, plan_clone: pathlib.Path | None = None,
               exchange_root: pathlib.Path | None = None, machines=(),
               merge_only: tuple[str, pathlib.Path] | None = None,
               as_of: str | None = None) -> Acceptance:
    """给什么跑什么：输入缺的那条记「跳过」并写明缺什么（见模块 docstring）。"""
    criteria: list[Criterion] = []
    if plan_clone is not None:
        criteria.append(check_plan_line(plan_clone, week))
    else:
        criteria.append(Criterion("计划核对行", SKIP, (
            "没给 --plan：读不到计划库克隆里已发布的计划。",)))
    for machine_id, db_path in machines:
        criteria.append(check_share(plan_clone, week, machine_id, db_path, as_of=as_of)
                        if plan_clone is not None else
                        Criterion(f"{machine_id} 本机份额 × 轮次对照", SKIP, (
                            "没给 --plan：份额对照不了（这条要对照已发布计划的指派）。",)))
    if exchange_root is not None:
        criteria.append(check_packages(exchange_root, week, plan_clone))
    else:
        criteria.append(Criterion("周包与「还没来的包」", SKIP, (
            "没给 --exchange-root：看不到交换区里的周包与周报。",)))
    dbs = dict(machines)
    if merge_only is not None:
        dbs[merge_only[0]] = merge_only[1]
    criteria.append(check_convergence(dbs) if dbs else
                    Criterion("任取两台：合并收敛一致", SKIP, (
                        "没给任何库（--machine / --merge-only）：没有可对照的对象。",)))
    if merge_only is not None and exchange_root is not None:
        criteria.append(check_cold_start(merge_only[1], exchange_root))
    else:
        missing = "；".join(part for part, given in
                            (("--merge-only", merge_only is not None),
                             ("--exchange-root", exchange_root is not None)) if not given)
        criteria.append(Criterion("纯汇总机冷启动重放", SKIP, (
            f"缺 {missing}：这条要纯汇总机的库文件 + 它那边的交换区。",)))
    return Acceptance(week=week, criteria=tuple(criteria))


def render(acceptance: Acceptance) -> str:
    """判定表的可读形态（报告风格：结果行 + 逐条）。"""
    counts = {status: sum(1 for c in acceptance.criteria if c.status == status)
              for status in (PASS, FAIL, SKIP)}
    lines = [
        f"# 三机验收核对 · {acceptance.week}",
        "",
        f"**结果**：{'不通过' if acceptance.failed else '通过'} · "
        f"跑 {len(acceptance.criteria)} 项：过 {counts[PASS]} / 没过 {counts[FAIL]} / "
        f"跳过 {counts[SKIP]}",
        "",
    ]
    for index, criterion in enumerate(acceptance.criteria, start=1):
        lines.append(f"## {index}. {criterion.title} —— {_STATUS_GLOSS[criterion.status]}")
        lines += [f"- {line}" for line in criterion.lines] or ["- （没有明细）"]
        lines.append("")
    return "\n".join(lines)


def _split_spec(text: str, *, what: str) -> tuple[str, pathlib.Path]:
    """拆 `编号=路径` 形式的参数；形态不对抛 ValueError（由 parse_args 转成用法错）。"""
    label, sep, raw = text.partition("=")
    if not sep or not label.strip() or not raw.strip():
        raise ValueError(f"{what} 要写成 编号=库文件路径（现在是 {text!r}）")
    path = pathlib.Path(raw.strip())
    if not path.exists():
        raise ValueError(f"{what} 的库文件不存在：{path}")
    return label.strip(), path


def parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="三机验收口径的机械化核对（spec「三机验收口径」；票据 13）。"
                    "五条口径见 tools/acceptance_check.py 的模块 docstring。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--week", required=True, help="验收周，如 2026-W40")
    parser.add_argument("--plan", type=pathlib.Path, default=None,
                        help="计划库克隆目录（读 plan/<周>.md/.json）")
    parser.add_argument("--exchange-root", type=pathlib.Path, default=None,
                        help="交换区根目录（周包、报告、名册都从这里读）")
    parser.add_argument("--machine", action="append", default=[], metavar="编号=库文件",
                        help="一台采集机的库文件（可给多次；拷贝过来的库文件都行）")
    parser.add_argument("--merge-only", default=None, metavar="编号=库文件",
                        help="纯汇总机的库文件（口径五用它）")
    parser.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                        help="「该采到」的截止日（默认今天，北京日期）")
    args = parser.parse_args(argv)
    try:
        weekly_plan.week_monday(args.week)
    except PlanError as exc:                       # 周编号非法：当场按用法错退出
        parser.error(str(exc))
    if args.plan is not None and not args.plan.exists():
        parser.error(f"--plan 的目录不存在：{args.plan}")
    if args.exchange_root is not None and not args.exchange_root.exists():
        parser.error(f"--exchange-root 的目录不存在：{args.exchange_root}")
    if not (args.plan or args.exchange_root or args.machine or args.merge_only):
        parser.error("至少给一样输入：--plan / --exchange-root / --machine / --merge-only")
    try:
        args.machines = [_split_spec(text, what="--machine") for text in args.machine]
        args.merge_only_spec = (_split_spec(args.merge_only, what="--merge-only")
                                if args.merge_only else None)
    except ValueError as exc:
        parser.error(str(exc))
    labels = [label for label, _ in args.machines] + \
             ([args.merge_only_spec[0]] if args.merge_only_spec else [])
    if len(labels) != len(set(labels)):
        parser.error("机器编号重复了：" + "、".join(sorted(labels)))
    return args


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    acceptance = run_checks(week=args.week, plan_clone=args.plan,
                            exchange_root=args.exchange_root, machines=args.machines,
                            merge_only=args.merge_only_spec, as_of=args.as_of)
    print(render(acceptance), end="")
    return 1 if acceptance.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
