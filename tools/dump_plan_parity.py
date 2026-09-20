"""票据 03 的实现侧对照导出：把原型六组样例跑过 `bestseller_monitor.weekly_plan`，
打印与原型 dump_plans.py 同形的 JSON，供 check_plan_parity.mjs 与原型 JS 模块逐格对照。

用法：python tools/dump_plan_parity.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bestseller_monitor import weekly_plan
from bestseller_monitor.config import Shop

# 与 .scratch/multi-machine-collection/prototype/dump_plans.py 同一份样例输入
SHOPS = [
    Shop("A01", "店A01", "https://a01.example/", pages=23),
    Shop("A02", "店A02", "https://a02.example/", pages=8),
    Shop("A03", "店A03", "https://a03.example/", pages=14),
    Shop("A04", "店A04", "https://a04.example/", pages=7),
    Shop("A05", "店A05", "https://a05.example/", pages=15),
    Shop("A06", "店A06", "https://a06.example/", pages=5),
    Shop("A07", "店A07", "https://a07.example/", pages=16),
    Shop("A08", "店A08", "https://a08.example/", pages=16),
    Shop("A09", "店A09", "https://a09.example/", pages=15),
    Shop("A10", "店A10", "https://a10.example/", pages=10),
    Shop("A11", "店A11", "https://a11.example/", pages=8),
    Shop("A12", "店A12", "https://a12.example/", pages=14),
]
A13 = Shop("A13", "新店（示例）", "https://a13.example/", pages=9)
START_MONDAY = dt.date(2026, 9, 21)
GENERATED_AT = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))

# (名称, 店铺, 机器, 约束窗口, 平衡窗口, 步骤)
SCENARIOS = [
    ("s1_full_c1_b4", SHOPS, ["m1", "m2", "m3"], 1, 4, ["week"] * 4),
    ("s2_full_c1_b1", SHOPS, ["m1", "m2", "m3"], 1, 1, ["week"] * 4),
    ("s3_roster_c1", SHOPS, ["m1", "m2", "m3"], 1, 4,
     ["week", "week", "add", "week", "remove", "week"]),
    ("s4_roster_c2", SHOPS, ["m1", "m2", "m3"], 2, 4,
     ["week", "week", "add", "week", "remove", "week"]),
    ("s5_two_shops", [s for s in SHOPS if s.key in ("A01", "A06")], ["m1", "m2", "m3"], 1, 4,
     ["week"] * 3),
    ("s6_two_machines_c2", SHOPS[:3], ["m1", "m2"], 2, 4, ["week"] * 3),
]


def run_scenario(shops, machines, constraint_weeks, balance_weeks, steps):
    current = list(shops)
    history: list[dict] = []
    plans: list[dict] = []
    for step in steps:
        if step == "add":
            current.append(A13)
            continue
        if step == "remove":
            current = [s for s in current if s.key != "A02"]
            continue
        week = weekly_plan.iso_week_label(START_MONDAY + dt.timedelta(weeks=len(plans)))
        doc = weekly_plan.generate_plan(
            current, machines, history,
            week=week, generated_by="m1", generated_at=GENERATED_AT,
            constraint_weeks=constraint_weeks, balance_weeks=balance_weeks,
        )
        plans.append(doc)
        history.append(doc)
    return [
        {
            "week": doc["week"],
            "assignments": sorted(doc["assignments"].items()),
            "relaxed": sorted(doc["checks"]["relaxed"].items()),
            "eligible": sorted(doc["checks"]["eligible_counts"].items()),
        }
        for doc in plans
    ]


def main() -> None:
    out = {
        name: run_scenario(shops, machines, c, b, steps)
        for name, shops, machines, c, b, steps in SCENARIOS
    }
    print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=1))


if __name__ == "__main__":
    main()
