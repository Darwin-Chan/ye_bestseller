"""票据 03：周计划生成算法与发布物的验收测试。

外部行为接缝是 `weekly_plan` 的三个入口：`generate_plan`（算出计划文件）、
`render_plan_md`（人核对的 .md）、`read_history`（读已发布的历史计划文件）。
对照基准来自设计期的原型（原型的存放目录 `.scratch/multi-machine-collection/prototype/`
在开发机上、不进代码仓；期望值已固化进本仓库，跑测试不需要它）：

- 六组分配样例来自 `prototype/samples.md`；期望值固定在
  `fixtures/plan_parity_expected.json`（由原型 `dump_plans.py` 导出，
  并经 `prototype/check_parity.mjs` 与 HTML 里的 JS 模块对照 6/6 验证）。
- .md 形态基准 `fixtures/plan_md_expected.md` 是原型 `plan-md-sample.md` 的逐字节
  副本，仅把「算法原型 v0」换成实现自己的版本行。
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor import weekly_plan
from bestseller_monitor.config import Shop, load_shops
from bestseller_monitor.db import cst_date
from tests.plan_samples import (
    A13, FIXED_CLOCK, MACHINES, SCENARIOS, SHOPS, START_MONDAY, parity_view, run_scenario,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class WeekLabelTests(unittest.TestCase):
    def test_iso_week_label_and_span(self):
        self.assertEqual(weekly_plan.iso_week_label(dt.date(2026, 9, 21)), "2026-W39")
        self.assertEqual(weekly_plan.week_span("2026-W39"), "9/21–9/27")

    def test_week_label_takes_a_beijing_date_or_defaults_to_now(self):
        """界面/命令行/导出判断「哪一周」的同一处读法。"""
        self.assertEqual(weekly_plan.week_label("2026-09-21"), "2026-W39")
        self.assertEqual(weekly_plan.week_label(), weekly_plan.week_label(cst_date()))

    def test_week_span_at_year_boundary_uses_iso_year(self):
        """2026-01-01（周四）落在跨年的 ISO 2026-W01：周一是 2025-12-29。"""
        self.assertEqual(weekly_plan.week_span("2026-W01"), "12/29–1/4")
        self.assertEqual(weekly_plan.week_monday("2026-W01"), dt.date(2025, 12, 29))

    def test_week_window_is_monday_to_sunday(self):
        """周界的唯一算法（票据 08 的导出周窗口也取它）：周一与周日两个北京日期。"""
        self.assertEqual(weekly_plan.week_window("2026-W39"),
                         (dt.date(2026, 9, 21), dt.date(2026, 9, 27)))
        self.assertEqual(weekly_plan.week_window("2026-W01"),
                         (dt.date(2025, 12, 29), dt.date(2026, 1, 4)))

    def test_bad_week_label_is_rejected(self):
        for bad in ("2026W39", "2026-W3", "", "2026-W99"):
            with self.assertRaises(weekly_plan.PlanError, msg=bad):
                weekly_plan.week_monday(bad)


class ParityTests(unittest.TestCase):
    def test_six_prototype_scenarios_match_cell_by_cell(self):
        expected = json.loads((FIXTURES / "plan_parity_expected.json").read_text(encoding="utf-8"))
        for name, shops, machines, c, b, steps in SCENARIOS:
            with self.subTest(scenario=name):
                views = [parity_view(d) for d in run_scenario(shops, machines, c, b, steps)]
                # 经 JSON 往返消掉 tuple/list 之别，再与原型产物逐格比对
                self.assertEqual(json.loads(json.dumps(views)), expected[name])


class InputValidationTests(unittest.TestCase):
    """pages 空缺或非法、名册不可用、历史文件坏了，都要拒绝并点名。"""

    def test_missing_pages_refuses_and_names_shops(self):
        shops = [
            Shop("A01", "店一", "https://a01.example/", pages=3),
            Shop("A02", "店二", "https://a02.example/", pages=None),
            Shop("A09", "退场的店", "https://a09.example/", pages=None, active=False),
        ]
        with self.assertRaises(weekly_plan.PlanError) as ctx:
            weekly_plan.generate_plan(shops, MACHINES, [], week="2026-W39",
                                      generated_by="m1", generated_at=FIXED_CLOCK)

        message = str(ctx.exception)
        self.assertIn("A02（店二）", message, "要点名缺 pages 的店")
        self.assertNotIn("A09", message, "active=0 的店不进计划，pages 坏了也不该拦")

    def test_illegal_pages_from_csv_refuses_and_names(self):
        """走真实清单路径：空值与非法值都由 load_shops 归一成 None，同样点名。"""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        csv_path = tmp / "shops.csv"
        csv_path.write_text(
            "shop_key,shop_name,shop_url,pages,active\n"
            "A01,店一,https://a01.example/,3,1\n"
            "A02,店二,https://a02.example/, ,1\n"
            "A03,店三,https://a03.example/,abc,1\n"
            "A04,退场的店,https://a04.example/, ,0\n",
            encoding="utf-8",
        )
        shops = load_shops(csv_path)

        with self.assertRaises(weekly_plan.PlanError) as ctx:
            weekly_plan.generate_plan(shops, MACHINES, [], week="2026-W39",
                                      generated_by="m1", generated_at=FIXED_CLOCK)

        message = str(ctx.exception)
        self.assertIn("A02", message)
        self.assertIn("A03", message)
        self.assertNotIn("A04", message)

    def test_zero_pages_is_legal(self):
        """「空缺或非法」是拒绝条件；0 是合法整数，不在此列。"""
        shops = [Shop("A01", "店一", "https://a01.example/", pages=3),
                 Shop("A02", "店二", "https://a02.example/", pages=0)]
        doc = weekly_plan.generate_plan(shops, MACHINES, [], week="2026-W39",
                                        generated_by="m1", generated_at=FIXED_CLOCK)
        self.assertEqual(doc["shops"][1], {"key": "A02", "name": "店二", "pages": 0})

    def test_bad_roster_is_rejected(self):
        shops = [Shop("A01", "店一", "https://a01.example/", pages=3)]
        for machines, fragment in (([], "机器名册为空"),
                                   (["m1", "m1"], "机器名册重复"),
                                   (["m1", ""], "机器名册有空项")):
            with self.subTest(machines=machines):
                with self.assertRaises(weekly_plan.PlanError) as ctx:
                    weekly_plan.generate_plan(shops, machines, [], week="2026-W39",
                                              generated_by="m1", generated_at=FIXED_CLOCK)
                self.assertIn(fragment, str(ctx.exception))

    def test_broken_history_files_are_rejected(self):
        shops = [Shop("A01", "店一", "https://a01.example/", pages=3)]
        good = {"week": "2026-W38",
                "shops": [{"key": "A01", "name": "店一", "pages": 3}],
                "assignments": {"A01": "m1"}}

        def generate(history):
            return weekly_plan.generate_plan(shops, MACHINES, history, week="2026-W39",
                                             generated_by="m1", generated_at=FIXED_CLOCK)

        with self.assertRaises(weekly_plan.PlanError) as ctx:
            generate([good, good])
        self.assertIn("2026-W38", str(ctx.exception))

        for broken, fragment in (
            ({"week": "2026-W38"}, "assignments"),
            ({"week": "2026-W38", "assignments": {"A01": "m1"},
              "shops": [{"key": "A02", "name": "店二", "pages": 3}]}, "A01"),
            ({"week": "W38", "assignments": {}, "shops": []}, "W38"),
            ({"assignments": {}, "shops": []}, "None"),
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(weekly_plan.PlanError) as ctx:
                    generate([broken])
                self.assertIn(fragment, str(ctx.exception))


class HistorySemanticsTests(unittest.TestCase):
    def test_regression_shop_has_no_constraint(self):
        """上周没采的店（回归店）不设约束：可选机器数仍是全部名册。"""
        shops = [Shop("A", "店A", "https://a.example/", pages=10),
                 Shop("B", "店B", "https://b.example/", pages=10)]
        w39 = weekly_plan.generate_plan(shops, ["m1", "m2"], [], week="2026-W39",
                                        generated_by="m1", generated_at=FIXED_CLOCK)
        # 手核：A 先（同为 10 页、按店号）拿 m1；B 拿 m2（m1 已背 10 页）
        self.assertEqual(w39["assignments"], {"A": "m1", "B": "m2"})

        w40 = weekly_plan.generate_plan(
            [shops[1]], ["m1", "m2"], [w39], week="2026-W40",
            generated_by="m1", generated_at=FIXED_CLOCK)
        self.assertEqual(w40["assignments"], {"B": "m1"}, "B 上周是 m2，本周轮到 m1")

        w41 = weekly_plan.generate_plan(shops, ["m1", "m2"], [w39, w40], week="2026-W41",
                                        generated_by="m1", generated_at=FIXED_CLOCK)
        self.assertEqual(w41["checks"]["eligible_counts"]["A"], 2, "回归店可选全部机器")
        # 手核：历史负载 m1=10(W39:A) + 10(W40:B) = 20、m2=10(W39:B)；A 选 m2（20+0 对 10+0）
        self.assertEqual(w41["assignments"], {"A": "m2", "B": "m2"})

    def test_history_pages_come_from_the_old_plan_snapshots(self):
        """平衡用的历史页数取各周计划里的旧值，不因后来改页数而重算。"""
        machines = ["m1", "m2", "m3"]
        w39_shops = [Shop("A", "店A", "https://a.example/", pages=30),
                     Shop("B", "店B", "https://b.example/", pages=1)]
        w39 = weekly_plan.generate_plan(w39_shops, machines, [], week="2026-W39",
                                        generated_by="m1", generated_at=FIXED_CLOCK)
        self.assertEqual(w39["assignments"], {"A": "m1", "B": "m2"})

        # 次周 A 的 pages 改成 5；若按现值重算历史，B 会落到 m1，按旧值（30）则落 m3
        w40_shops = [Shop("A", "店A", "https://a.example/", pages=5),
                     Shop("B", "店B", "https://b.example/", pages=1),
                     Shop("C", "店C", "https://c.example/", pages=11)]
        w40 = weekly_plan.generate_plan(w40_shops, machines, [w39], week="2026-W40",
                                        generated_by="m1", generated_at=FIXED_CLOCK)

        self.assertEqual(w40["assignments"], {"A": "m2", "B": "m3", "C": "m3"})
        self.assertEqual(w40["shops"], [{"key": "A", "name": "店A", "pages": 5},
                                        {"key": "B", "name": "店B", "pages": 1},
                                        {"key": "C", "name": "店C", "pages": 11}])

    def test_history_document_order_does_not_matter(self):
        """历史是发布事实的集合：传入顺序不许影响结果。"""
        docs = run_scenario(SHOPS, MACHINES, 1, 4, ["week", "week", "add"])
        week = weekly_plan.iso_week_label(START_MONDAY + dt.timedelta(weeks=3))
        shops = SHOPS + [A13]

        def generate(history):
            return weekly_plan.generate_plan(shops, MACHINES, history, week=week,
                                             generated_by="m1", generated_at=FIXED_CLOCK)

        self.assertEqual(generate(docs), generate(list(reversed(docs))))

    def test_history_for_this_week_or_later_is_ignored(self):
        """目标周及以后的计划不参与历史（重算旧周时不许拿后一周当约束）。"""
        docs = run_scenario(SHOPS, MACHINES, 1, 4, ["week", "week"])
        again = weekly_plan.generate_plan(SHOPS, MACHINES, docs, week="2026-W40",
                                          generated_by="m1", generated_at=FIXED_CLOCK)
        fresh = weekly_plan.generate_plan(SHOPS, MACHINES, docs[:1], week="2026-W40",
                                          generated_by="m1", generated_at=FIXED_CLOCK)
        self.assertEqual(again, fresh)


class CheckLineTests(unittest.TestCase):
    """核对行三项（违例/空手/极差）的期望值手核自 prototype/samples.md，不重算实现。"""

    def test_no_adjacent_week_same_machine_anywhere(self):
        for name, shops, machines, c, b, steps in SCENARIOS:
            docs = run_scenario(shops, machines, c, b, steps)
            by_week = {doc["week"]: doc["assignments"] for doc in docs}
            weeks = [doc["week"] for doc in docs]
            ever = set().union(*(set(a) for a in by_week.values()))
            violations = 0
            for key in ever:
                seq = [by_week[w][key] for w in weeks if key in by_week[w]]
                violations += sum(1 for x, y in zip(seq, seq[1:]) if x == y)
            self.assertEqual(violations, 0, f"{name} 出现相邻周同机器")

    def test_idle_machines_are_reported_per_week(self):
        """样例五：两家店轮着空出一台机器（samples.md「空手机器：W39:m3、W40:m2、W41:m1」）。"""
        few = [s for s in SHOPS if s.key in ("A01", "A06")]
        docs = run_scenario(few, MACHINES, 1, 4, ["week"] * 3)
        self.assertEqual([d["checks"]["idle"] for d in docs], [["m3"], ["m2"], ["m1"]])

    def test_page_spread_matches_the_prototype_week_summaries(self):
        """极差＝当周最高与最低机器页数之差（samples.md 样例一：3、3、3、3）。"""
        docs = run_scenario(SHOPS, MACHINES, 1, 4, ["week"] * 4)
        spreads = []
        for doc in docs:
            pages = {s["key"]: s["pages"] for s in doc["shops"]}
            totals = {m: 0 for m in doc["machines"]}
            for key, m in doc["assignments"].items():
                totals[m] += pages[key]
            spreads.append(max(totals.values()) - min(totals.values()))
        self.assertEqual(spreads, [3, 3, 3, 3])


class DeterminismTests(unittest.TestCase):
    def test_same_input_rerun_is_cell_identical(self):
        docs1 = run_scenario(SHOPS, MACHINES, 1, 4, ["week"] * 4)
        docs2 = run_scenario(SHOPS, MACHINES, 1, 4, ["week"] * 4)
        self.assertEqual(
            [weekly_plan.plan_json(d) for d in docs1],
            [weekly_plan.plan_json(d) for d in docs2],
        )

    def test_only_the_publication_facts_vary_between_runs(self):
        """generated_at / generated_by 是发布事实；其余字段逐格一致。"""
        def generate(clock, by):
            return weekly_plan.generate_plan(SHOPS, MACHINES, [], week="2026-W39",
                                             generated_by=by, generated_at=clock)

        a = generate(FIXED_CLOCK, "m1")
        other_clock = dt.datetime(2026, 9, 21, 8, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        b = generate(other_clock, "m2")

        for doc in (a, b):
            doc.pop("generated_at")
            doc.pop("generated_by")
        self.assertEqual(a, b)


class PlanDocumentTests(unittest.TestCase):
    def test_document_shape_and_serialization(self):
        doc = weekly_plan.generate_plan(SHOPS, MACHINES, [], week="2026-W39",
                                        generated_by="m1", generated_at=FIXED_CLOCK)

        self.assertEqual(doc["week"], "2026-W39")
        self.assertEqual(doc["generated_at"], "2026-09-20T12:00:00+08:00")
        self.assertEqual(doc["generated_by"], "m1")
        self.assertEqual(doc["algo_version"], weekly_plan.PLAN_ALGO_VERSION)
        self.assertEqual(doc["params"], {"constraint_weeks": 1, "balance_weeks": 4})
        self.assertEqual(doc["machines"], MACHINES)
        self.assertEqual([s["key"] for s in doc["shops"]], sorted(s.key for s in SHOPS))
        self.assertEqual(sorted(doc["assignments"]), sorted(s.key for s in SHOPS))
        self.assertEqual(set(doc["checks"]), {"relaxed", "eligible_counts", "idle"})
        self.assertEqual(doc["checks"]["relaxed"], {})
        self.assertEqual(doc["checks"]["idle"], [])

        text = weekly_plan.plan_json(doc)
        self.assertTrue(text.endswith("\n"))
        self.assertIn("义乌市中茂箱包有限公司", text, "中文不转义，人可直读")
        self.assertEqual(json.loads(text), doc)

    def test_inactive_shops_stay_out_of_the_plan(self):
        shops = SHOPS[:2] + [Shop(s.key, s.name, s.url, pages=s.pages, active=False)
                             for s in SHOPS[2:]]
        doc = weekly_plan.generate_plan(shops, MACHINES, [], week="2026-W39",
                                        generated_by="m1", generated_at=FIXED_CLOCK)
        self.assertEqual(sorted(doc["assignments"]), ["A01", "A02"])


class PlanMdTests(unittest.TestCase):
    def test_md_matches_the_prototype_sample_form(self):
        """.md 形态与原型 plan-md-sample.md 逐字节一致（仅算法版本行不同）。"""
        expected = (FIXTURES / "plan_md_expected.md").read_text(encoding="utf-8")
        sim1 = run_scenario(SHOPS, MACHINES, 1, 4, ["week"] * 4)
        sim3 = run_scenario(SHOPS, MACHINES, 1, 4,
                            ["week", "week", "add", "week", "remove", "week"])
        weeks = [
            (sim1[0], []),
            (sim1[1], sim1[:1]),
            (sim3[2], sim3[:2]),
            (sim3[3], sim3[:3]),
        ]
        text = "\n---\n\n".join(weekly_plan.render_plan_md(doc, history)
                                for doc, history in weeks)
        self.assertEqual(text, expected)

    def test_md_marks_same_machine_and_counts_the_violation(self):
        """名册只剩一台机器时会放宽约束、出现同机器；.md 要如实标注并计数。"""
        shops = [Shop("A", "店A", "https://a.example/", pages=5)]
        w39 = weekly_plan.generate_plan(shops, ["m1"], [], week="2026-W39",
                                        generated_by="m1", generated_at=FIXED_CLOCK)
        w40 = weekly_plan.generate_plan(shops, ["m1"], [w39], week="2026-W40",
                                        generated_by="m1", generated_at=FIXED_CLOCK)

        self.assertEqual(w40["checks"]["relaxed"], {"A": 1})
        text = weekly_plan.render_plan_md(w40, [w39])
        self.assertIn("| A | 店A | 5 | m1 | **同机器 ✗** |", text)
        self.assertIn("相邻周同机器 1 处", text)
        self.assertIn("每店可选机器数最少 1", text)
        self.assertIn("放宽 A×1", text)

    def test_md_renders_an_empty_plan(self):
        """全店退场是合法空手：.md 照常出，不崩。"""
        shops = [Shop("A01", "店一", "https://a01.example/", pages=3, active=False)]
        doc = weekly_plan.generate_plan(shops, MACHINES, [], week="2026-W39",
                                        generated_by="m1", generated_at=FIXED_CLOCK)

        self.assertEqual(doc["assignments"], {})
        self.assertEqual(doc["checks"]["idle"], MACHINES)
        text = weekly_plan.render_plan_md(doc)
        self.assertIn("# 2026-W39 周计划（9/21–9/27）", text)
        self.assertIn("每店可选机器数最少 —", text)
        self.assertIn("空手 m1、m2、m3", text)


if __name__ == "__main__":
    unittest.main()
