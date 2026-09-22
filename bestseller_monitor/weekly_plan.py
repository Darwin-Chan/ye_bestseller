"""周计划生成：给定店铺清单、机器名册与已发布的历史计划文件，算出本周计划。

口径（spec §4/§5）：

- 输入只取**已发布的事实**（共享店铺清单、历史计划文件、机器名册），不读任何一台
  机器的本地采集结果——这是三台各自独立算出同一份计划的前提。
- 硬约束：同一店铺相邻周不由同一台机器采，按计划文件的**指派**判定；首周与回归店
  （上一条历史计划里没有这家店）不设约束。
- 软目标：大件优先贪心——店铺按页数降序、店号升序处理；给某店挑机器时比分 = 该机
  「近 N-1 周被指派页数之和 + 本周已分页数」，平局比本周已分页数，再平局按名册顺序。
- 平衡用的历史页数取各周计划里的**旧值**，不因后来改页数而重算；本周预算以本计划
  快照为准。
- 无解兜底：候选为空时丢最老的一条约束并记「放宽」；约束 1 周 + 至少 2 台机器时
  永不触发。空手（店少于机器）合法。
- 同一输入重跑逐格一致（`generated_at` / `generated_by` 是发布事实，不参与逐格对照）。

发布物两份：JSON 计划文件（机器读，为准）与同名 .md（人核对）。
机制全文见 `docs/ops/多机分片采集机制.md`；逐格对照基准钉在
`tests/fixtures/plan_parity_expected.json` 与 `tests/test_weekly_plan.py`
（设计期的原型样例在开发机的 `.scratch/multi-machine-collection/prototype/`，机器本地、不随仓库走）。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from bestseller_monitor.config import Shop
from bestseller_monitor.db import cst_date

PLAN_ALGO_VERSION = "v1"
DEFAULT_CONSTRAINT_WEEKS = 1
DEFAULT_BALANCE_WEEKS = 4

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


class PlanError(Exception):
    """生成计划被拒绝（页数空缺/名册为空/历史文件坏了等），消息点名具体对象。"""


def _parse_week(week: object) -> tuple[int, int]:
    match = _WEEK_RE.match(week.strip()) if isinstance(week, str) else None
    if not match:
        raise PlanError(f"周编号格式非法：{week!r}（应形如 2026-W39）")
    year, number = int(match.group(1)), int(match.group(2))
    if not 1 <= number <= 53:
        raise PlanError(f"周编号非法：{week!r}（周序号超范围）")
    return year, number


def week_monday(week: str) -> dt.date:
    """周编号（如 2026-W39）对应的周一日期；非法就点名拒绝。"""
    year, number = _parse_week(week)
    try:
        return dt.date.fromisocalendar(year, number, 1)
    except ValueError as exc:      # 该年没有第 53 周之类
        raise PlanError(f"周编号非法：{week!r}（{exc}）") from exc


def iso_week_label(day: dt.date) -> str:
    """北京日期所在 ISO 周的周编号。"""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_label(iso_date: str | None = None) -> str:
    """「本周」的周编号：给北京日期串（YYYY-MM-DD）就折算它所在的 ISO 周，不给就取现在。

    界面、命令行与导出判断「哪一周」都经这里——同一件事不再各处拼一遍
    （`iso_week_label(date.fromisoformat(...))` 的写法人手一份迟早分家）。
    """
    return iso_week_label(dt.date.fromisoformat(iso_date or cst_date()))


def week_window(week: str) -> tuple[dt.date, dt.date]:
    """ISO 周编号（如 2026-W39）对应的周一与周日（北京日期）。

    周界的唯一算法：`.md` 抬头（`week_span`）与导出的周窗口（票据 08）都从这里取。
    """
    monday = week_monday(week)
    return monday, monday + dt.timedelta(days=6)


def week_span(week: str) -> str:
    """周编号的日期范围文本（如 9/21–9/27），供 .md 抬头用。"""
    monday, sunday = week_window(week)
    return f"{monday.month}/{monday.day}–{sunday.month}/{sunday.day}"


@dataclasses.dataclass(frozen=True)
class PlanHistory:
    """已发布历史计划的只读视图，只含目标周之前的周，最近的在前。"""

    weeks: tuple[str, ...]
    documents: dict[str, Mapping[str, Any]]           # week -> 计划文件（原样保留）
    assignments: dict[str, dict[str, str]]            # week -> {shop_key: machine}
    pages: dict[str, dict[str, int]]                  # week -> {shop_key: pages 快照}

    @property
    def previous_document(self) -> Mapping[str, Any] | None:
        """紧挨着目标周之前的那一份计划文件（首周为 None）。"""
        return self.documents[self.weeks[0]] if self.weeks else None

    def machine_by_week(self, shop_key: str) -> dict[str, str]:
        """这家店在每个历史周被指派给哪台机器（只含采过的周）。"""
        return {w: self.assignments[w][shop_key]
                for w in self.weeks if shop_key in self.assignments[w]}

    def assigned_load(self, machines: Iterable[str], weeks: int) -> dict[str, int]:
        """近 weeks 份已发布计划里各机器被指派的页数之和（取各周计划里的旧值）。"""
        load = dict.fromkeys(machines, 0)
        for w in self.weeks[: max(weeks, 0)]:
            week_pages = self.pages[w]
            for key, machine in self.assignments[w].items():
                if machine in load:
                    load[machine] += week_pages[key]
        return load


def read_history(documents: Iterable[Mapping[str, Any]], *, before_week: str) -> PlanHistory:
    """把已发布的历史计划文件整理成算法与 .md 都要的视图。

    只收 week 严格早于 before_week 的。历史是发布物：结构缺项、同一周两份、快照与
    指派对不上，都直接报错点名——坏了不能猜。
    """
    before = _parse_week(before_week)
    seen: dict[str, Mapping[str, Any]] = {}
    for doc in documents:
        if not isinstance(doc, Mapping):
            raise PlanError(f"历史计划不是一份计划文件：{doc!r}")
        week = doc.get("week")
        if _parse_week(week) >= before:
            continue
        if week in seen:
            raise PlanError(f"历史计划里同一周出现两份：{week}")
        seen[week] = doc

    assignments: dict[str, dict[str, str]] = {}
    pages: dict[str, dict[str, int]] = {}
    for week, doc in seen.items():
        raw_assignments = doc.get("assignments")
        if not isinstance(raw_assignments, Mapping):
            raise PlanError(f"历史计划 {week} 缺少 assignments")
        week_pages: dict[str, int] = {}
        for entry in doc.get("shops") or ():
            key = entry.get("key") if isinstance(entry, Mapping) else None
            value = entry.get("pages") if isinstance(entry, Mapping) else None
            if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool):
                raise PlanError(f"历史计划 {week} 的 shops 快照缺 key/pages：{entry!r}")
            week_pages[key] = value
        for key, machine in raw_assignments.items():
            if key not in week_pages:
                raise PlanError(f"历史计划 {week}：assignments 里的 {key} 不在 shops 快照里")
            if not isinstance(machine, str):
                raise PlanError(f"历史计划 {week}：{key} 的机器标识不是字符串：{machine!r}")
            assignments.setdefault(week, {})[key] = machine
        pages[week] = week_pages

    weeks = tuple(sorted(seen, key=_parse_week, reverse=True))
    return PlanHistory(weeks=weeks, documents=seen, assignments=assignments, pages=pages)


def _clean_roster(machines: Iterable[str]) -> list[str]:
    roster = [str(m).strip() for m in machines]
    if not roster:
        raise PlanError("机器名册为空：machines.json 里至少要有一台机器")
    if any(not m for m in roster):
        raise PlanError("机器名册有空项：machines.json 里有空名字")
    seen: set[str] = set()
    for m in roster:
        if m in seen:
            raise PlanError(f"机器名册重复：{m}")
        seen.add(m)
    return roster


def _active_shops_with_pages(shops: Iterable[Shop]) -> list[Shop]:
    """进计划的店＝active=1；页数空缺或非法就点名拒绝，不猜。"""
    active = [s for s in shops if s.active]
    bad = [s for s in active
           if not isinstance(s.pages, int) or isinstance(s.pages, bool) or s.pages < 0]
    if bad:
        names = "、".join(f"{s.key}（{s.name}）" if s.name else s.key for s in bad)
        raise PlanError(f"这些店铺的 pages 空缺或非法，先修好清单再生成计划：{names}")
    return active


def generate_plan(
    shops: Iterable[Shop],
    machines: Iterable[str],
    history_documents: Iterable[Mapping[str, Any]] = (),
    *,
    week: str,
    generated_by: str,
    generated_at: dt.datetime,
    constraint_weeks: int = DEFAULT_CONSTRAINT_WEEKS,
    balance_weeks: int = DEFAULT_BALANCE_WEEKS,
) -> dict[str, Any]:
    """算出本周计划，返回计划文件（JSON 为准的那份）。"""
    week_monday(week)                                       # 周编号非法当场拒绝
    roster = _clean_roster(machines)
    active = _active_shops_with_pages(shops)
    history = read_history(history_documents, before_week=week)

    # 平衡窗口：近 balance_weeks-1 周的被指派页数，取各周计划里的旧值
    hist_load = history.assigned_load(roster, balance_weeks - 1)

    load = dict.fromkeys(roster, 0)
    assignments: dict[str, str] = {}
    eligible_counts: dict[str, int] = {}
    relaxed: dict[str, int] = {}

    for shop in sorted(active, key=lambda s: (-s.pages, s.key)):
        weeks_of_shop = history.machine_by_week(shop.key)
        forbidden: list[str] = []
        for i in range(constraint_weeks):
            if i >= len(history.weeks):
                break
            machine = weeks_of_shop.get(history.weeks[i])
            if machine is None:                              # 回归店：不设约束
                break
            forbidden.append(machine)

        dropped = 0
        eligible = [m for m in roster if m not in forbidden]
        while not eligible and forbidden:
            forbidden.pop()                                  # 丢最老的一条约束
            dropped += 1
            eligible = [m for m in roster if m not in forbidden]

        pick = min(eligible, key=lambda m: (hist_load[m] + load[m], load[m]))
        assignments[shop.key] = pick
        eligible_counts[shop.key] = len(eligible)
        if dropped:
            relaxed[shop.key] = dropped
        load[pick] += shop.pages

    return {
        "week": week,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "generated_by": generated_by,
        "algo_version": PLAN_ALGO_VERSION,
        "params": {"constraint_weeks": constraint_weeks, "balance_weeks": balance_weeks},
        "shops": [{"key": s.key, "name": s.name, "pages": s.pages}
                  for s in sorted(active, key=lambda s: s.key)],
        "machines": roster,
        "assignments": {k: assignments[k] for k in sorted(assignments)},
        "checks": {
            "relaxed": {k: relaxed[k] for k in sorted(relaxed)},
            "eligible_counts": {k: eligible_counts[k] for k in sorted(eligible_counts)},
            "idle": [m for m in roster if load[m] == 0],
        },
    }


def plan_json(document: Mapping[str, Any]) -> str:
    """计划文件的磁盘形态：UTF-8、保留中文、缩进两格、末尾一个换行。"""
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _violation_count(history: PlanHistory, week: str, assignments: Mapping[str, str]) -> int:
    """相邻周同机器违例数：把历史与本周并成时间线，逐店数相邻两次同机器。

    回归店（中间周没采）跳过缺席周，不把隔周的两次算成相邻。
    """
    timeline = sorted(history.weeks, key=_parse_week) + [week]
    shops = set(assignments)
    for week_assignments in history.assignments.values():
        shops.update(week_assignments)
    violations = 0
    for key in shops:
        machines = []
        for w in timeline:
            machine = assignments.get(key) if w == week else history.assignments[w].get(key)
            if machine is not None:
                machines.append(machine)
        violations += sum(1 for a, b in zip(machines, machines[1:]) if a == b)
    return violations


def render_plan_md(document: Mapping[str, Any],
                   history_documents: Iterable[Mapping[str, Any]] = ()) -> str:
    """把计划文件渲染成人核对的 .md（形态见原型 plan-md-sample.md）。"""
    week = str(document["week"])
    history = read_history(history_documents, before_week=week)
    machines = list(document["machines"])
    snapshot = {str(s["key"]): s for s in document["shops"]}
    assignments = dict(document["assignments"])
    checks = document["checks"]

    totals = dict.fromkeys(machines, 0)
    for key, machine in assignments.items():
        totals[machine] += snapshot[key]["pages"]
    total = sum(totals.values())

    prev = history.previous_document
    prev_assignments = dict(prev["assignments"]) if prev else {}
    if prev is None:
        delta = "—（首周）"
    else:
        added = sorted(set(assignments) - set(prev_assignments))
        removed = sorted(set(prev_assignments) - set(assignments))
        delta = "、".join([f"+{k}" for k in added] + [f"−{k}" for k in removed]) or "无变化"

    relaxed = "、".join(f"{k}×{n}" for k, n in sorted(checks["relaxed"].items())) or "0 家"
    idle = "、".join(checks["idle"]) or "—"
    min_eligible = min(checks["eligible_counts"].values(), default=None)

    params = document["params"]
    lines = [
        f"# {week} 周计划（{week_span(week)}）",
        "",
        f"口径：约束 {params['constraint_weeks']} 周 · 平衡 {params['balance_weeks']} 周 · "
        f"算法 {document['algo_version']} ｜ {len(assignments)} 家店 · {total} 页 · "
        f"理想 {total / len(machines):.1f} 页/台",
        f"较上周：{delta}",
        "",
    ]
    for machine in machines:
        shops_of = sorted(
            ((key, snapshot[key]["pages"]) for key, m in assignments.items() if m == machine),
            key=lambda kv: (-kv[1], kv[0]),
        )
        lines += [f"## {machine} · {totals[machine]} 页（{len(shops_of)} 家）", "",
                  "| 店铺 | 名称 | 页数 | 上周 | 轮换 |", "|---|---|---|---|---|"]
        for key, pages in shops_of:
            previous = prev_assignments.get(key)
            if prev is None:
                mark = "首周"
            elif previous == machine:
                mark = "**同机器 ✗**"
            else:
                mark = "轮换 ✓"
            lines.append(f"| {key} | {snapshot[key]['name']} | {pages} | {previous or '—'} | {mark} |")
        lines.append("")
    lines.append(
        f"合计 {total} 页 · 极差 {max(totals.values()) - min(totals.values())} 页 ｜ 核对："
        f"相邻周同机器 {_violation_count(history, week, assignments)} 处 · "
        f"每店可选机器数最少 {min_eligible if min_eligible is not None else '—'} · "
        f"放宽 {relaxed} · 空手 {idle}"
    )
    return "\n".join(lines) + "\n"
