"""票据 03 的样例台：六组原型样例的输入与跑法，供验收测试与对照脚本共用。

与 `.scratch/multi-machine-collection/prototype/dump_plans.py` 的样例表一一对应
（`samples.md` 的六组场景）。期望值不在这里——在 `fixtures/plan_parity_expected.json`
（原型产物）与 `fixtures/plan_md_expected.md`（原型 .md 形态）里。
"""
from __future__ import annotations

import datetime as dt

from bestseller_monitor import weekly_plan
from bestseller_monitor.config import Shop

MACHINES = ["m1", "m2", "m3"]
START_MONDAY = dt.date(2026, 9, 21)
FIXED_CLOCK = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))

# 与原型样例同一份输入：config/shops.csv 的 12 家店 + 示例新店 A13（见 samples.md）。
SHOPS = [
    Shop("A01", "义乌市中茂箱包有限公司", "https://a01.example/", pages=23),
    Shop("A02", "义乌市奔此日用品有限公司", "https://a02.example/", pages=8),
    Shop("A03", "义乌市中茂专业设计有限公司", "https://a03.example/", pages=14),
    Shop("A04", "义乌市快茂日用品有限公司", "https://a04.example/", pages=7),
    Shop("A05", "义乌市中蓝箱包有限公司", "https://a05.example/", pages=15),
    Shop("A06", "义乌市快勤日用品有限公司", "https://a06.example/", pages=5),
    Shop("A07", "义乌市暖宏纺织品有限公司", "https://a07.example/", pages=16),
    Shop("A08", "义乌市创柔文化用品有限公司", "https://a08.example/", pages=16),
    Shop("A09", "义乌市启弘纺织品有限公司", "https://a09.example/", pages=15),
    Shop("A10", "义乌市中腾箱包有限公司", "https://a10.example/", pages=10),
    Shop("A11", "义乌市通伟塑料制品有限公司", "https://a11.example/", pages=8),
    Shop("A12", "义乌市远强不锈钢制品有限公司", "https://a12.example/", pages=14),
]
A13 = Shop("A13", "新店（示例）", "https://a13.example/", pages=9)

# (名称, 店铺, 机器, 约束窗口, 平衡窗口, 步骤)；与原型 dump_plans.py 的六组场景一致
SCENARIOS = [
    ("s1_full_c1_b4", SHOPS, MACHINES, 1, 4, ["week"] * 4),
    ("s2_full_c1_b1", SHOPS, MACHINES, 1, 1, ["week"] * 4),
    ("s3_roster_c1", SHOPS, MACHINES, 1, 4, ["week", "week", "add", "week", "remove", "week"]),
    ("s4_roster_c2", SHOPS, MACHINES, 2, 4, ["week", "week", "add", "week", "remove", "week"]),
    ("s5_two_shops", [s for s in SHOPS if s.key in ("A01", "A06")], MACHINES, 1, 4, ["week"] * 3),
    ("s6_two_machines_c2", SHOPS[:3], ["m1", "m2"], 2, 4, ["week"] * 3),
]


def run_scenario(shops, machines, constraint_weeks, balance_weeks, steps):
    """按周推进一组样例：每周用此前生成的计划文件当历史，逐周产出计划文件。"""
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
            week=week, generated_by="m1", generated_at=FIXED_CLOCK,
            constraint_weeks=constraint_weeks, balance_weeks=balance_weeks,
        )
        plans.append(doc)
        history.append(doc)
    return plans


def parity_view(doc):
    """与原型 dump_plans.py 相同的投影：只留可逐格对照的指派与核对信息。"""
    return {
        "week": doc["week"],
        "assignments": sorted(doc["assignments"].items()),
        "relaxed": sorted(doc["checks"]["relaxed"].items()),
        "eligible": sorted(doc["checks"]["eligible_counts"].items()),
    }
