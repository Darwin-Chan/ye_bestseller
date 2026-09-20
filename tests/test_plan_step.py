"""票据 05：开轮前准备（确认与发布本周计划、落库、降级、缺口告警）的验收测试。

两个接缝：

- `plan_step.prepare_week`：真 git 计划库（本地裸库当远端，样例台在 `tests/git_repos.py`）
  加真库，走完「拉计划库 → 同步清单 → 确认/生成本周计划 → 落库 → 缺口告警」整串。
- `plan_step.scan_exchange_gaps`：交换区里已有的包（本文件自己造的样例包）对比本地库。

期望值都是手写的清单、名册、计划与包内容，不重算实现；「远端现在长什么样」经全新克隆看，
与另一台机器会看到的完全一样。降级矩阵（spec §6）逐行都有对应用例。
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import pathlib
import shutil
import sqlite3
import tempfile
import unittest

from bestseller_monitor import db as dbm
from bestseller_monitor import plan_step, rounds, shops_sync
from bestseller_monitor.config import ROLE_MERGE_ONLY
from bestseller_monitor.db import Database, WeeklyPlanRow
from bestseller_monitor.rounds import RoundRequest, ShopScope
from tests.git_repos import GitSandbox
from tests.helpers import crawler_cfg

# 样例世界：两家店、两台机器（手算：A01 页多 → m1，A02 → m2；见票 03 的算法口径）
SHOPS_CSV = ("shop_key,shop_name,shop_url,pages,active\n"
             "A01,店一,https://a01.example/,23,1\n"
             "A02,店二,https://a02.example/,8,1\n")
ONE_SHOP_CSV = ("shop_key,shop_name,shop_url,pages,active\n"
                "A01,店一,https://a01.example/,23,1\n")
NO_PAGES_CSV = SHOPS_CSV.replace("A02,店二,https://a02.example/,8,1",
                                 "A02,店二,https://a02.example/,,1")
ROSTER_JSON = '["m1", "m2"]\n'
WEEK = "2026-W39"                      # 2026-09-21（周一）起
MONDAY = "2026-09-21"
NOW = "2026-09-21T08:00:00+08:00"
NEXT_RUN = "2026-09-21T09:30:00+08:00"

# 手写的另一份计划（模拟「另一台机器先发布了」）：指派与样例世界不同，且可被完整校验
OTHER_PLAN = {
    "week": WEEK,
    "generated_at": "2026-09-21T07:00:00+08:00",
    "generated_by": "m1",
    "algo_version": "v1",
    "params": {"constraint_weeks": 1, "balance_weeks": 4},
    "shops": [{"key": "A01", "name": "店一", "pages": 23},
              {"key": "A02", "name": "店二", "pages": 8}],
    "machines": ["m1", "m2"],
    "assignments": {"A01": "m2", "A02": "m1"},
    "checks": {"relaxed": {}, "eligible_counts": {"A01": 2, "A02": 2}, "idle": []},
}


def plan_text(document: dict) -> str:
    """计划文件的手写形态：与实现无关的独立期望值来源。"""
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def write_package(path: pathlib.Path, rows: list[tuple[str, str]]) -> None:
    """造一个样例周的包（`(shop_key, date)` 行），与票据 08 的包同形：gz 包的 SQLite。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = path.with_suffix("")
    conn = sqlite3.connect(raw)
    conn.execute("CREATE TABLE inventory (shop_key TEXT, offer_id TEXT, sku_id TEXT, "
                 "date TEXT, stock INTEGER, price REAL, PRIMARY KEY (shop_key, offer_id, "
                 "sku_id, date))")
    conn.executemany("INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock) "
                     "VALUES (?, ?, ?, ?, 1)",
                     [(shop, f"{shop}-1", "s", day) for shop, day in rows])
    conn.commit()
    conn.close()
    with open(raw, "rb") as src, gzip.open(path, "wb") as dst:
        dst.write(src.read())
    raw.unlink()


class Machine:
    """一台机器的小世界：自己的交换区根、本机清单副本、计划库克隆与本机库。"""

    def __init__(self, box: GitSandbox, remote: pathlib.Path, machine_id: str):
        self.machine_id = machine_id
        self.base = box.tmp / machine_id
        self.clone = box.clone(remote, f"{machine_id}/exchange/plan")
        self.cfg = crawler_cfg(
            machine_id=machine_id,
            shop_csv=self.base / "config" / "shops.csv",
            exchange_root=self.base / "exchange",
        )
        self.conn = dbm.connect(self.base / "data" / "crawler.db")
        self.db = Database(self.conn)

    def prepare(self, *, now: str = NOW) -> plan_step.PrepResult:
        return plan_step.prepare_week(self.cfg, self.db, now=dt.datetime.fromisoformat(now))

    def stored_rows(self) -> list[tuple]:
        return [(r["shop_key"], r["shop_name"], r["machine_id"], r["pages"], r["source"],
                 r["plan_sha256"], r["stored_at"]) for r in self.db.weekly_plan(WEEK)]


class PrepWorldTestCase(unittest.TestCase):
    """样例台：一个裸库当计划库远端，外加按需克隆的若干「机器」。"""

    def setUp(self):
        self.box = GitSandbox(self)
        self.remote = self.box.new_remote("plan.git")
        self._copies = 0
        self._machines: list[Machine] = []

    def tearDown(self):
        for world in self._machines:
            world.conn.close()

    # ---- 世界搭建 ----

    def seed_repo(self, *, shops: str = SHOPS_CSV, roster: str | None = ROSTER_JSON) -> None:
        files = {"shops.csv": shops.encode("utf-8")}
        if roster is not None:
            files["machines.json"] = roster.encode("utf-8")
        self.box.seed(self.remote, files)

    def machine(self, machine_id: str, *, local_shops: str | None = SHOPS_CSV) -> Machine:
        world = Machine(self.box, self.remote, machine_id)
        self._machines.append(world)
        if local_shops is not None:
            world.cfg.shop_csv.parent.mkdir(parents=True, exist_ok=True)
            world.cfg.shop_csv.write_text(local_shops, encoding="utf-8")
        return world

    def break_remote(self, machine: Machine) -> None:
        """把克隆的远端地址改成不存在的路径——制造「拉不到计划库」。"""
        self.box.must("remote", "set-url", "origin",
                      str(self.box.tmp / "gone.git"), cwd=machine.clone)

    def edit_remote(self, files: dict[str, str | bytes], *, message: str = "edit",
                    remove: tuple[str, ...] = ()) -> None:
        """另一台机器（或人手）在计划库里改了/删了文件并推送。"""
        self._copies += 1
        editor = self.box.clone(self.remote, f"editor-{self._copies}")
        if remove:
            self.box.must("rm", "--", *remove, cwd=editor)
        if files:
            self.box.write_files(editor, files)
            self.box.must("add", "--", *files, cwd=editor)
        self.box.must("commit", "-m", message, cwd=editor)
        self.box.must("push", cwd=editor)

    def fresh_clone(self) -> pathlib.Path:
        """一份全新克隆——另一台机器现在会看到的远端内容。"""
        self._copies += 1
        return self.box.clone(self.remote, f"check-{self._copies}")

    def remote_plans(self) -> list[str]:
        clone = self.fresh_clone()
        return sorted(p.name for p in (clone / "plan").glob("*.json")) \
            if (clone / "plan").exists() else []

    def remote_published(self) -> list[str]:
        clone = self.fresh_clone()
        return sorted(p.name for p in (clone / "published").glob("*.md")) \
            if (clone / "published").exists() else []

    def other_publishes_plan(self, document: dict, machine_id: str = "m1") -> None:
        """另一台机器先发布了本周计划（经一个临时克隆走正常发布路径）。"""
        self._copies += 1
        other = self.box.clone(self.remote, f"other-{self._copies}")
        self.box.commit_push(other, {
            f"plan/{document['week']}.json": plan_text(document),
            f"published/{machine_id}.md": f"# {machine_id} 发布的计划\n",
        }, message=f"publish plan {document['week']}")


class WeeklyPlanTableTests(unittest.TestCase):
    """本机计划表（不入交换集）：整周落库、同周重跑幂等。"""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="bestseller-plan-table-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.database = Database(dbm.connect(self.dir / "crawler.db"))
        self.addCleanup(self.database.conn.close)
        self.rows = [WeeklyPlanRow("A01", "店一", "m1", 23),
                     WeeklyPlanRow("A02", "店二", "m2", 8)]

    def store(self, rows=None, *, source: str = "generated", sha: str = "ab" * 32,
              stored_at: str = NOW) -> bool:
        return self.database.replace_weekly_plan(
            WEEK, self.rows if rows is None else rows,
            source=source, plan_sha256=sha, stored_at=stored_at)

    def read(self) -> list[tuple]:
        return [(r["shop_key"], r["shop_name"], r["machine_id"], r["pages"], r["source"],
                 r["plan_sha256"], r["stored_at"]) for r in self.database.weekly_plan(WEEK)]

    def test_stores_the_whole_week_and_reads_it_back(self):
        self.assertTrue(self.store())

        self.assertEqual(self.read(), [
            ("A01", "店一", "m1", 23, "generated", "ab" * 32, NOW),
            ("A02", "店二", "m2", 8, "generated", "ab" * 32, NOW),
        ])

    def test_rerun_with_same_content_leaves_every_row_identical(self):
        self.store()

        self.assertFalse(self.store(stored_at=NEXT_RUN), "同内容重跑不该再写一遍")

        self.assertEqual(self.read(), [
            ("A01", "店一", "m1", 23, "generated", "ab" * 32, NOW),
            ("A02", "店二", "m2", 8, "generated", "ab" * 32, NOW),
        ], "落库时刻也不该被重跑刷新")

    def test_changed_plan_replaces_the_whole_week(self):
        self.store()

        self.assertTrue(self.store(rows=[WeeklyPlanRow("A02", "店二", "m1", 30)],
                                   source="pulled", sha="cd" * 32, stored_at=NEXT_RUN))

        self.assertEqual(self.read(), [
            ("A02", "店二", "m1", 30, "pulled", "cd" * 32, NEXT_RUN),
        ], "换了一份计划：整周替换，A01 不再留下")


class StoredPlanReadTests(unittest.TestCase):
    """落库计划的读口（spec §6）：界面与命令行读同一份，都不解析计划文件。"""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="bestseller-plan-read-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.database = Database(dbm.connect(self.dir / "crawler.db"))
        self.addCleanup(self.database.conn.close)

    def store(self, *rows: WeeklyPlanRow) -> None:
        self.database.replace_weekly_plan(WEEK, rows, source="pulled",
                                          plan_sha256="ab" * 32, stored_at=NOW)

    def test_a_week_without_rows_reads_as_none(self):
        self.assertIsNone(plan_step.stored_plan(self.database, WEEK), "没落过库：没有这份计划")
        self.assertIsNone(plan_step.stored_plan(self.database, "2026-W38"))

    def test_reads_the_whole_week_with_machine_keys_and_page_snapshot(self):
        self.store(WeeklyPlanRow("A02", "店二", "m2", 8),
                   WeeklyPlanRow("A01", "店一", "m1", 23),
                   WeeklyPlanRow("A03", "店三", "m1", 5))

        plan = plan_step.stored_plan(self.database, WEEK)

        self.assertEqual(plan.week, WEEK)
        self.assertEqual([row.shop_key for row in plan.rows], ["A01", "A02", "A03"],
                         "整周的行都在（不只本机那几行），按店铺编号排序")
        self.assertEqual(plan.machine_keys("m1"), ("A01", "A03"))
        self.assertEqual(plan.machine_keys("m3"), (), "本机本周没店 = 空手，不是错误")
        self.assertEqual(plan.pages(), {"A01": 23, "A02": 8, "A03": 5})


class VisibleShopsTests(unittest.TestCase):
    """开始页陈列与命令行共用的店铺全量：本机启用的店，加上本周计划说到的店。"""

    LOCAL_CSV = ("shop_key,shop_name,shop_url,pages,active\n"
                 "A01,店一,https://a01.example/,23,1\n"
                 "A02,店二,https://a02.example/,8,1\n"
                 "A03,店三,https://a03.example/,5,0\n")   # 周中停用，但本周计划仍说到了它

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="bestseller-visible-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.shop_csv = self.dir / "shops.csv"
        self.shop_csv.write_text(self.LOCAL_CSV, encoding="utf-8")
        self.cfg = crawler_cfg(machine_id="m1", shop_csv=self.shop_csv)

    def plan(self, *rows: WeeklyPlanRow) -> plan_step.StoredPlan:
        return plan_step.StoredPlan(week=WEEK, rows=rows, plan_sha256="ab" * 32)

    def test_without_a_plan_the_active_shops_are_the_universe(self):
        shops = plan_step.visible_shops(self.cfg, None)

        self.assertEqual([shop.key for shop in shops], ["A01", "A02"])

    def test_a_planned_shop_stays_visible_even_after_it_is_deactivated(self):
        shops = plan_step.visible_shops(
            self.cfg, self.plan(WeeklyPlanRow("A03", "店三", "m1", 5)))

        self.assertEqual([shop.key for shop in shops], ["A01", "A02", "A03"],
                         "计划已发布：本周按计划走，周中停用不把它从本周摘掉")
        self.assertEqual(shops[2].url, "https://a03.example/page/offerlist.htm",
                         "地址与名称仍从本机清单取")

    def test_a_planned_key_missing_from_the_local_list_is_skipped(self):
        shops = plan_step.visible_shops(
            self.cfg, self.plan(WeeklyPlanRow("A09", "已删店", "m1", 5)))

        self.assertEqual([shop.key for shop in shops], ["A01", "A02"],
                         "本机清单里没有的行（被删了）：没有地址，采不了")


class PlanDeviationTests(unittest.TestCase):
    """这次要采的店里哪些在计划之外（spec §6）：越权只剩「店在计划内、本周归别人」一种。"""

    def plan(self, *rows: WeeklyPlanRow) -> plan_step.StoredPlan:
        return plan_step.StoredPlan(week=WEEK, rows=rows, plan_sha256="ab" * 32)

    def test_a_shop_planned_for_another_machine_is_overreach_and_names_its_machine(self):
        plan = self.plan(WeeklyPlanRow("A01", "店一", "m1", 23),
                         WeeklyPlanRow("A02", "店二", "m2", 8))

        deviations = plan_step.plan_deviations(plan, "m1", ["A01", "A02"])

        self.assertEqual([d.shop_key for d in deviations], ["A02"], "归本机的那家不是偏离")
        one = deviations[0]
        self.assertEqual(one.kind, plan_step.DeviationKind.OVERREACH)
        self.assertEqual(one.planned_machine, "m2")
        self.assertEqual(one.reason, "本周计划归 m2", "文案要点名这家店本周归谁")

    def test_a_shop_the_plan_never_mentions_is_out_of_plan(self):
        plan = self.plan(WeeklyPlanRow("A01", "店一", "m1", 23))

        deviations = plan_step.plan_deviations(plan, "m1", ["A01", "B07"])

        self.assertEqual([(d.shop_key, d.kind, d.planned_machine) for d in deviations],
                         [("B07", plan_step.DeviationKind.OUT_OF_PLAN, None)])
        self.assertIn("没说到", deviations[0].reason)

    def test_without_a_plan_every_shop_is_out_of_plan_for_the_escape_hatch(self):
        """逃生口放行的自由采集：没有计划可对照，这一轮的店都记计划外。"""
        deviations = plan_step.plan_deviations(None, "m1", ["A01", "A02"])

        self.assertEqual([d.shop_key for d in deviations], ["A01", "A02"])
        self.assertEqual([d.kind for d in deviations],
                         [plan_step.DeviationKind.OUT_OF_PLAN] * 2)
        self.assertIn("逃生口", deviations[0].reason, "理由就是降级表里「拉不到 + 本地没有」那一行")

    def test_a_shop_nobody_asked_about_is_not_a_deviation(self):
        plan = self.plan(WeeklyPlanRow("A01", "店一", "m1", 23),
                         WeeklyPlanRow("A02", "店二", "m2", 8))

        self.assertEqual(plan_step.plan_deviations(plan, "m1", ["A01"]), ())

    def test_the_note_names_each_shop_and_what_kind_of_deviation_it_is(self):
        plan = self.plan(WeeklyPlanRow("A01", "店一", "m1", 23),
                         WeeklyPlanRow("A02", "店二", "m2", 8))

        note = plan_step.deviation_note(
            plan_step.plan_deviations(plan, "m1", ["A02", "B07"]))

        self.assertIn("越权补采", note)
        self.assertIn("A02（本周计划归 m2）", note)
        self.assertIn("计划外采集", note)
        self.assertIn("B07", note)

    def test_the_overreach_note_names_the_constraint_it_breaks(self):
        """spec §6：文案要让人看见破坏的是哪一条，而不只是「越权」两个字。"""
        plan = self.plan(WeeklyPlanRow("A01", "店一", "m1", 23),
                         WeeklyPlanRow("A02", "店二", "m2", 8))

        note = plan_step.deviation_note(plan_step.plan_deviations(plan, "m1", ["A02"]))

        self.assertIn("相邻周不同机器", note)

    def test_the_escape_hatch_note_does_not_claim_a_broken_constraint(self):
        """逃生口自由采集没有计划可违：别把「相邻周不同机器」这条约束算到它头上。"""
        note = plan_step.deviation_note(plan_step.plan_deviations(None, "m1", ["A01"]))

        self.assertNotIn("相邻周不同机器", note)


class DeviationLedgerTests(unittest.TestCase):
    """放行之后要留痕（spec §6）：轮次备注 + 本机计划外账（不入交换集，供汇总侧加说明）。"""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="bestseller-deviations-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.conn = dbm.connect(self.dir / "crawler.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.round = rounds.open(self.db, RoundRequest(
            "2026-09-21", (ShopScope("A02", "https://a02.example/", "店二"),))).round
        self.deviations = (plan_step.PlanDeviation(
            "A02", plan_step.DeviationKind.OVERREACH, "m2", "本周计划归 m2"),)

    def test_records_every_field_the_summary_side_needs(self):
        """账目字段齐全：日期、店铺、本机、种类、计划归谁、理由、时刻，一样不少。"""
        plan_step.record_deviations(self.db, self.round, "m1", self.deviations,
                                    now=dt.datetime.fromisoformat(NOW))

        rows = self.db.recorded_deviations()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row["round_id"], row["run_date"], row["week"], row["shop_key"],
             row["machine_id"], row["kind"], row["planned_machine"], row["reason"],
             row["recorded_at"]),
            (self.round.id, "2026-09-21", WEEK, "A02", "m1", "overreach", "m2",
             "本周计划归 m2", NOW))

    def test_writes_the_round_note_too_and_the_terminal_note_does_not_eat_it(self):
        plan_step.record_deviations(self.db, self.round, "m1", self.deviations,
                                    now=dt.datetime.fromisoformat(NOW))

        note = self.conn.execute(
            "SELECT note FROM rounds WHERE id=?", (self.round.id,)).fetchone()["note"]
        self.assertIn("A02", note, "轮次备注要点名是哪家店")
        self.assertIn("m2", note, "以及本周它归谁")

        rounds.finish(self.db, self.round, rounds.TerminalReason.COMPLETED, note="本轮正常完成")
        combined = self.conn.execute(
            "SELECT note FROM rounds WHERE id=?", (self.round.id,)).fetchone()["note"]
        self.assertIn("A02", combined, "收尾备注追加在后面，不吞掉开轮时写下的留痕")
        self.assertIn("本轮正常完成", combined)

    def test_recording_the_same_round_again_is_idempotent(self):
        """同一轮重跑（续跑）重复记账：账上仍是一行，不留重复。"""
        plan_step.record_deviations(self.db, self.round, "m1", self.deviations,
                                    now=dt.datetime.fromisoformat(NOW))
        plan_step.record_deviations(self.db, self.round, "m1", self.deviations,
                                    now=dt.datetime.fromisoformat(NEXT_RUN))

        self.assertEqual(len(self.db.recorded_deviations()), 1)

    def test_the_first_record_wins_a_later_resume_does_not_rewrite_it(self):
        """偏离是开轮那一刻的事实：计划后来变没变都不改写已经记下的那一行。"""
        plan_step.record_deviations(self.db, self.round, "m1", self.deviations,
                                    now=dt.datetime.fromisoformat(NOW))
        later = (plan_step.PlanDeviation(
            "A02", plan_step.DeviationKind.OUT_OF_PLAN, None,
            "拉不到计划库、本地也没有本周计划（逃生口放行）"),)

        plan_step.record_deviations(self.db, self.round, "m1", later,
                                    now=dt.datetime.fromisoformat(NEXT_RUN))

        rows = self.db.recorded_deviations(round_id=self.round.id)
        self.assertEqual([(r["kind"], r["planned_machine"]) for r in rows],
                         [("overreach", "m2")], "第一次记账为准")

    def test_nothing_recorded_when_the_scope_has_no_deviations(self):
        plan_step.record_deviations(self.db, self.round, "m1", ())

        self.assertEqual(self.db.recorded_deviations(), [])
        self.assertIsNone(self.conn.execute(
            "SELECT note FROM rounds WHERE id=?", (self.round.id,)).fetchone()["note"])


class FirstPublishTests(PrepWorldTestCase):
    """pull 成功 + 本周计划不在：生成、发布（JSON 与 .md 同一次提交）、整周落库。"""

    def test_first_machine_generates_publishes_and_stores_the_whole_week(self):
        self.seed_repo()
        m1 = self.machine("m1")

        result = m1.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.READY)
        self.assertEqual(result.source, plan_step.PlanSource.GENERATED)
        self.assertTrue(result.can_start)
        self.assertFalse(result.stale)
        self.assertFalse(result.idle)
        self.assertEqual(result.my_shops, ("A01",))

        self.assertEqual(self.remote_plans(), [f"{WEEK}.json"])
        self.assertEqual(self.remote_published(), ["m1.md"], "发布记录各机各写各的")
        clone = self.fresh_clone()
        plan_file = clone / "plan" / f"{WEEK}.json"
        document = json.loads(plan_file.read_text(encoding="utf-8"))
        self.assertEqual(document["week"], WEEK)
        self.assertEqual(document["generated_by"], "m1")
        self.assertEqual(document["assignments"], {"A01": "m1", "A02": "m2"},
                         "样例世界手算：A01 页多归 m1、A02 归 m2")
        self.assertEqual({s["key"]: s["pages"] for s in document["shops"]},
                         {"A01": 23, "A02": 8}, "快照页数预算来自共享清单")
        self.assertTrue((clone / "plan" / f"{WEEK}.md").exists(), "同名 .md 与 JSON 同一次提交")
        published = (clone / "published" / "m1.md").read_text(encoding="utf-8")
        self.assertIn(WEEK, published)
        self.assertIn("plan/" + WEEK + ".json", published)

        text = plan_file.read_text(encoding="utf-8")
        expected_sha = hashlib.sha256(
            text.replace("\r\n", "\n").rstrip("\n").encode("utf-8")).hexdigest()
        self.assertEqual(m1.stored_rows(), [
            ("A01", "店一", "m1", 23, "generated", expected_sha, NOW),
            ("A02", "店二", "m2", 8, "generated", expected_sha, NOW),
        ], "整周落库：指派 + 快照页数 + 来源 + 落库时刻 + 计划文件哈希")
        self.assertEqual(result.plan_sha256, expected_sha)

    def test_rerunning_the_same_week_changes_nothing(self):
        self.seed_repo()
        m1 = self.machine("m1")
        m1.prepare()
        rows_before = m1.stored_rows()
        commits_before = self.box.must("log", "--oneline", cwd=self.remote).count("\n")

        again = m1.prepare(now=NEXT_RUN)

        self.assertEqual(again.status, plan_step.PrepStatus.READY)
        self.assertEqual(again.source, plan_step.PlanSource.GENERATED, "generated_by 就是本机")
        self.assertEqual(m1.stored_rows(), rows_before, "同周重跑逐行幂等，连落库时刻也不刷新")
        self.assertEqual(self.box.must("log", "--oneline", cwd=self.remote).count("\n"),
                         commits_before, "发布后本周不重算：没有新提交")
        self.assertEqual(self.remote_plans(), [f"{WEEK}.json"])


class SecondMachineTests(PrepWorldTestCase):
    """pull 成功 + 本周计划在：只读它，不重算也不重发。"""

    def test_a_second_machine_reads_the_published_plan(self):
        self.seed_repo()
        m1 = self.machine("m1")
        m1.prepare()
        m2 = self.machine("m2")

        result = m2.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.READY)
        self.assertEqual(result.source, plan_step.PlanSource.PULLED)
        self.assertEqual(result.my_shops, ("A02",))
        m1_rows, m2_rows = m1.stored_rows(), m2.stored_rows()
        self.assertEqual([r[:5] for r in m2_rows], [
            ("A01", "店一", "m1", 23, "pulled"),
            ("A02", "店二", "m2", 8, "pulled"),
        ])
        self.assertEqual(m1_rows[0][5], m2_rows[0][5], "同一份计划：哈希逐机一致")
        self.assertEqual(result.plan_sha256, m1_rows[0][5])
        self.assertEqual(self.remote_plans(), [f"{WEEK}.json"], "m2 没有另发一份")
        self.assertEqual(self.remote_published(), ["m1.md"])

    def test_simultaneous_first_generation_publishes_only_one_plan(self):
        """先到先发布：m2 生成完之后、推送落地之前，m1 的抢先在远端出现（真 git 交错）。"""
        self.seed_repo()
        m1 = self.machine("m1")
        m2 = self.machine("m2")          # 赶在 m1 发布前就已克隆好
        m3 = self.machine("m3")          # 第三台同样在发布前克隆好
        racer = self.box.clone(self.remote, "racer")
        self.box.write_files(racer, {
            f"plan/{WEEK}.json": plan_text(OTHER_PLAN),
            f"plan/{WEEK}.md": f"# {WEEK} 周计划\n",
            "published/m1.md": "# m1 发布的计划\n",
        })
        names = [f"plan/{WEEK}.json", f"plan/{WEEK}.md", "published/m1.md"]
        self.box.must("add", "--", *names, cwd=racer)
        self.box.must("commit", "-m", f"publish plan {WEEK}", "--", *names, cwd=racer)
        self.box.install_racing_hook(m2.clone, racer)

        result = m2.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.READY)
        self.assertEqual(result.source, plan_step.PlanSource.PULLED, "后到的一方重读已有计划")
        self.assertEqual(result.my_shops, ("A01",), "按 m1 那份计划：A01 归 m2")
        self.assertTrue(any("发布没成功" in w for w in result.warnings),
                        "发布没成功这件事要说出来")
        self.assertEqual(self.remote_plans(), [f"{WEEK}.json"], "远端只有一份计划")
        self.assertEqual(self.remote_published(), ["m1.md"], "m2 那份发布记录没上去")
        clone = self.fresh_clone()
        document = json.loads((clone / "plan" / f"{WEEK}.json").read_text(encoding="utf-8"))
        self.assertEqual(document["generated_by"], "m1", "留下的是先到的 m1 那份")
        self.assertEqual(m2.stored_rows(), [
            ("A01", "店一", "m2", 23, "pulled", m2.stored_rows()[0][5], NOW),
            ("A02", "店二", "m1", 8, "pulled", m2.stored_rows()[0][5], NOW),
        ], "落库的是 m1 先发布的那份指派")
        self.assertEqual(self.box.must("status", "--porcelain", cwd=m2.clone), "",
                         "被拒之后克隆退回干净状态")
        self.assertFalse((m2.clone / "published" / "m2.md").exists(), "没推上去的发布记录也退回了")

        third = m3.prepare()                       # 第三台：读到的是同一份，不另发

        self.assertEqual(third.source, plan_step.PlanSource.PULLED)
        self.assertEqual(third.my_shops, (), "两份计划里 A01/A02 都不归 m3：空手是合法态")
        self.assertTrue(third.idle)
        self.assertEqual([r[:6] for r in m3.stored_rows()], [r[:6] for r in m2.stored_rows()],
                         "三台读到同一份计划：逐行一致")
        self.assertEqual(self.remote_plans(), [f"{WEEK}.json"])
        self.assertEqual(self.remote_published(), ["m1.md"])


class OfflineTests(PrepWorldTestCase):
    """pull 失败的两行：本地已落库 → 用它；本地没有 → 默认拒绝开轮 + 逃生口。"""

    def test_offline_with_a_stored_plan_uses_the_local_copy(self):
        self.seed_repo()
        m1 = self.machine("m1")
        m1.prepare()
        sha = m1.stored_rows()[0][5]
        self.break_remote(m1)

        result = m1.prepare(now=NEXT_RUN)

        self.assertEqual(result.status, plan_step.PrepStatus.READY_FROM_CACHE)
        self.assertTrue(result.stale, "界面要据此标注「未能确认最新」")
        self.assertTrue(result.can_start)
        self.assertEqual(result.source, plan_step.PlanSource.LOCAL_CACHE)
        self.assertEqual(result.my_shops, ("A01",))
        self.assertEqual(m1.stored_rows(), [
            ("A01", "店一", "m1", 23, "local_cache", sha, NEXT_RUN),
            ("A02", "店二", "m2", 8, "local_cache", sha, NEXT_RUN),
        ], "同一份计划，来源改记为本地缓存、落库时刻记这一轮")
        self.assertTrue(any("未能确认最新" in w for w in result.warnings))

        m1.prepare(now=NEXT_RUN)

        self.assertEqual(m1.stored_rows()[0][6], NEXT_RUN, "连着离线重跑：已经记过就不再写")

    def test_offline_without_a_stored_plan_refuses_to_start(self):
        self.seed_repo()
        m1 = self.machine("m1")
        self.break_remote(m1)

        result = m1.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.REFUSED)
        self.assertFalse(result.can_start)
        self.assertTrue(result.escape_hatch_available)
        self.assertIn("--ignore-plan", result.reason, "拒绝理由里给出显式逃生口")
        self.assertIn("自由采集", result.reason)
        self.assertEqual(m1.stored_rows(), [], "没确认到计划：不落库")
        self.assertEqual(result.my_shops, ())


class GenerationFailureTests(PrepWorldTestCase):
    """生成失败（pages 空缺点名、名册读不到）：落到「本地有就用、没有就拒绝」两行。"""

    def test_missing_pages_refuses_and_names_the_shop(self):
        self.seed_repo(shops=NO_PAGES_CSV)
        m1 = self.machine("m1", local_shops=NO_PAGES_CSV)

        result = m1.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.REFUSED)
        self.assertIn("A02", result.reason, "点名缺哪家店")
        self.assertEqual(self.remote_plans(), [], "生成失败：没有半份计划被发布")
        self.assertEqual(m1.stored_rows(), [])

    def test_missing_roster_refuses_and_names_it(self):
        self.seed_repo(roster=None)
        m1 = self.machine("m1")

        result = m1.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.REFUSED)
        self.assertIn("machines.json", result.reason)
        self.assertEqual(self.remote_plans(), [])

    def test_generation_failure_falls_back_to_the_local_copy(self):
        self.seed_repo()
        m1 = self.machine("m1")
        m1.prepare()
        sha = m1.stored_rows()[0][5]
        self.edit_remote({"shops.csv": NO_PAGES_CSV.encode("utf-8")},
                         message="把共享清单的页数改坏",
                         remove=(f"plan/{WEEK}.json", f"plan/{WEEK}.md"))

        result = m1.prepare(now=NEXT_RUN)

        self.assertEqual(result.status, plan_step.PrepStatus.READY_FROM_CACHE)
        self.assertTrue(result.stale)
        self.assertTrue(result.can_start)
        self.assertTrue(any("未能确认最新" in w for w in result.warnings))
        self.assertTrue(any("A02" in w for w in result.warnings), "降级原因要点到具体店铺")
        self.assertEqual(m1.stored_rows(), [
            ("A01", "店一", "m1", 23, "local_cache", sha, NEXT_RUN),
            ("A02", "店二", "m2", 8, "local_cache", sha, NEXT_RUN),
        ])

    def test_a_broken_plan_file_is_not_guessed_or_overwritten(self):
        self.seed_repo()
        self.edit_remote({f"plan/{WEEK}.json": "{ 半截的 JSON"},
                         message=f"publish plan {WEEK}")
        m1 = self.machine("m1")

        result = m1.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.REFUSED)
        self.assertIn("读不动", result.reason)
        self.assertEqual(m1.stored_rows(), [])
        clone = self.fresh_clone()
        self.assertEqual((clone / "plan" / f"{WEEK}.json").read_text(encoding="utf-8"),
                         "{ 半截的 JSON", "不猜也不覆盖：远端那份原样留着")


class PublishFailureTests(PrepWorldTestCase):
    """推不动（凭据被拒、网络断这类非「被拒重试」的失败）：不落库、不拿自己的草稿冒充发布物。"""

    def test_a_failed_publish_without_a_stored_plan_refuses_and_leaves_no_debris(self):
        self.seed_repo()
        m1 = self.machine("m1")
        runs = self.box.tmp / "hook-runs"
        self.box.install_declining_commit_hook(m1.clone, runs)

        result = m1.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.REFUSED,
                         "提交都没成：不拿本机草稿冒充「远端那份」")
        self.assertIn("发布没成功", result.reason)
        self.assertEqual(runs.read_text(encoding="utf-8").split(), ["ran"])
        self.assertEqual(self.remote_plans(), [], "没发布成功：远端没有半份计划")
        self.assertEqual(m1.stored_rows(), [], "没确认到计划：不落库")
        self.assertEqual(self.box.must("status", "--porcelain", cwd=m1.clone), "",
                         "刚写的三个文件退回时一并清掉，不留成假的重读对象")

    def test_a_failed_publish_with_a_stored_plan_uses_the_local_copy(self):
        self.seed_repo()
        m1 = self.machine("m1")
        m1.prepare()
        sha = m1.stored_rows()[0][5]
        self.edit_remote({}, message="误删本周计划",
                         remove=(f"plan/{WEEK}.json", f"plan/{WEEK}.md"))
        self.box.install_declining_hook(m1.clone, self.box.tmp / "hook-runs")

        result = m1.prepare(now=NEXT_RUN)

        self.assertEqual(result.status, plan_step.PrepStatus.READY_FROM_CACHE)
        self.assertTrue(result.stale)
        self.assertTrue(any("发布没成功" in w for w in result.warnings))
        self.assertEqual(self.remote_plans(), [], "发布还是没成功：远端仍没有本周计划")
        self.assertEqual(m1.stored_rows(), [
            ("A01", "店一", "m1", 23, "local_cache", sha, NEXT_RUN),
            ("A02", "店二", "m2", 8, "local_cache", sha, NEXT_RUN),
        ])


class RoleAndIdleTests(PrepWorldTestCase):
    def test_merge_only_machine_skips_checking_generating_and_publishing(self):
        self.seed_repo()
        m = self.machine("m1")
        m.cfg.role = ROLE_MERGE_ONLY

        result = m.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.SKIPPED_MERGE_ONLY)
        self.assertFalse(result.can_start)
        self.assertFalse(result.escape_hatch_available)
        self.assertIsNone(result.shops_sync, "连清单都不同步（只读凭据）")
        self.assertIsNone(result.gaps, "不检查")
        self.assertEqual(m.stored_rows(), [])
        self.assertEqual(self.remote_plans(), [])
        self.assertEqual(self.box.must("status", "--porcelain", cwd=m.clone), "")

    def test_a_machine_with_no_shops_this_week_is_a_legal_empty_hand(self):
        self.seed_repo(shops=ONE_SHOP_CSV)
        m1 = self.machine("m1", local_shops=ONE_SHOP_CSV)
        m1.prepare()
        m2 = self.machine("m2", local_shops=ONE_SHOP_CSV)

        result = m2.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.READY)
        self.assertTrue(result.idle, "本机本周没店：合法正常态")
        self.assertTrue(result.can_start, "空手不拦开轮")
        self.assertEqual(result.my_shops, ())
        self.assertEqual([(r["shop_key"], r["machine_id"]) for r in m2.db.weekly_plan(WEEK)],
                         [("A01", "m1")], "整周仍落库，虽然本机一行没有")


class HealTests(PrepWorldTestCase):
    def test_debris_from_a_crashed_publish_is_preserved_aside_then_reset(self):
        self.seed_repo()
        m1 = self.machine("m1")
        debris = m1.clone / "plan" / f"{WEEK}.json"
        debris.parent.mkdir(parents=True, exist_ok=True)
        debris.write_text("{ 半截的 JSON", encoding="utf-8")     # 崩在提交之前的残迹

        result = m1.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.READY)
        self.assertTrue(any("残迹" in w for w in result.warnings))
        sidecars = sorted(m1.cfg.exchange_root.glob("plan-残迹-*"))
        self.assertEqual(len(sidecars), 1, "收拾之前先旁路留存一整棵")
        self.assertEqual((sidecars[0] / "plan" / f"{WEEK}.json").read_text(encoding="utf-8"),
                         "{ 半截的 JSON", "残迹内容留得住")
        clone = self.fresh_clone()
        document = json.loads((clone / "plan" / f"{WEEK}.json").read_text(encoding="utf-8"))
        self.assertEqual(document["generated_by"], "m1", "发布的是新生成的计划，不是残迹")
        self.assertEqual(self.box.must("status", "--porcelain", cwd=m1.clone), "")


class GapScanTests(unittest.TestCase):
    """缺口检查：只读交换区里已有的包，比对本机缺哪些店哪些日。"""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="bestseller-gaps-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.exchange = self.dir / "exchange"
        self.conn = dbm.connect(self.dir / "crawler.db")
        self.addCleanup(self.conn.close)

    def package(self, machine: str, name: str, rows: list[tuple[str, str]]) -> pathlib.Path:
        path = self.exchange / f"raw-{machine}" / "data" / "2026" / name
        write_package(path, rows)
        return path

    def local_rows(self, rows: list[tuple[str, str]]) -> None:
        self.conn.executemany("INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock) "
                              "VALUES (?, ?, 's', ?, 1)",
                              [(shop, f"{shop}-1", day) for shop, day in rows])
        self.conn.commit()

    def scan(self) -> plan_step.GapScan:
        return plan_step.scan_exchange_gaps(self.exchange, self.conn, WEEK)

    def test_missing_pairs_are_reported_with_the_machines_that_have_them(self):
        self.package("m2", "W39-m2.db.gz", [("A01", MONDAY), ("A02", "2026-09-22")])
        self.package("m3", "W39-m3.db.gz", [("A02", "2026-09-22")])
        self.local_rows([("A01", MONDAY)])

        scan = self.scan()

        self.assertEqual(scan.missing,
                         (plan_step.GapEntry("A02", "2026-09-22", ("m2", "m3")),))
        self.assertEqual(scan.packages, ("raw-m2/data/2026/W39-m2.db.gz",
                                         "raw-m3/data/2026/W39-m3.db.gz"))
        self.assertIn("A02 2026-09-22", scan.warning_message)
        self.assertIn("m2、m3", scan.warning_message)
        self.assertIn("只告警", scan.warning_message)

    def test_a_covered_pair_is_not_a_gap(self):
        self.package("m2", "W39-m2.db.gz", [("A01", MONDAY)])
        self.local_rows([("A01", MONDAY)])

        scan = self.scan()

        self.assertEqual(scan.missing, ())
        self.assertIsNone(scan.warning_message, "没有缺口就什么都不说")

    def test_other_weeks_are_not_read_and_out_of_range_dates_do_not_count(self):
        self.package("m2", "W38-m2.db.gz", [("A01", "2026-09-14")])
        self.package("m2", "39-m2.db.gz", [("A01", "2026-09-30")])

        scan = self.scan()

        self.assertEqual(scan.packages, ("raw-m2/data/2026/39-m2.db.gz",),
                         "上周的包不读；spec 字面的裸周号写法也认")
        self.assertEqual(scan.missing, (), "周范围外的日期不算缺口")

    def test_an_unreadable_package_is_skipped_with_a_note(self):
        path = self.package("m2", "W39-m2.db.gz", [("A01", MONDAY)])
        path.write_bytes(b"not a gzip at all")

        scan = self.scan()

        self.assertEqual(scan.packages, ())
        self.assertEqual(scan.missing, ())
        self.assertTrue(any("W39-m2.db.gz" in note for note in scan.notes))

    def test_no_packages_means_no_gaps_and_no_warning(self):
        scan = self.scan()

        self.assertEqual(scan, plan_step.GapScan(week=WEEK, packages=(), missing=(), notes=()))

    def test_the_year_boundary_week_reads_the_iso_year_directory(self):
        # 2026-W01 的周一落在 2025-12-29：目录按周编号里的 ISO 年（data/2026/），与导出口径一致
        self.package("m2", "W01-m2.db.gz", [("A01", "2025-12-29")])

        scan = plan_step.scan_exchange_gaps(self.exchange, self.conn, "2026-W01")

        self.assertEqual(scan.packages, ("raw-m2/data/2026/W01-m2.db.gz",))
        self.assertEqual(scan.missing,
                         (plan_step.GapEntry("A01", "2025-12-29", ("m2",)),))


class PrepareGapWarningTests(PrepWorldTestCase):
    def test_gaps_warn_but_do_not_block_starting(self):
        self.seed_repo()
        m1 = self.machine("m1")
        write_package(m1.cfg.exchange_root / "raw-m2" / "data" / "2026" / "W39-m2.db.gz",
                      [("A01", MONDAY)])

        result = m1.prepare()

        self.assertEqual(result.status, plan_step.PrepStatus.READY)
        self.assertTrue(result.can_start, "缺口只告警，不拦开轮")
        self.assertEqual(result.gaps.missing,
                         (plan_step.GapEntry("A01", MONDAY, ("m2",)),))
        self.assertTrue(any("缺口检查" in w for w in result.warnings))


class ShopsSyncToleranceTests(PrepWorldTestCase):
    """降级矩阵的「清单同步」一行：推不动 / 两边都变，都不拦计划确认与开轮。"""

    def test_a_shops_push_failure_does_not_block_confirming_the_plan(self):
        self.seed_repo()
        m1 = self.machine("m1")
        m1.prepare()                              # 第一次：确认（生成）发布并建立同步基线
        m1.cfg.shop_csv.write_text(SHOPS_CSV + "A03,店三,https://a03.example/,5,1\n",
                                   encoding="utf-8")     # 只本机变 → 方向是推
        self.box.install_declining_hook(m1.clone, self.box.tmp / "hook-runs")

        result = m1.prepare(now=NEXT_RUN)

        self.assertEqual(result.shops_sync.action, shops_sync.SyncAction.PUSH_FAILED)
        self.assertEqual(result.status, plan_step.PrepStatus.READY, "推不动不拦计划确认")
        self.assertTrue(any("推送没成功" in w for w in result.warnings))
        self.assertIn("A03", m1.cfg.shop_csv.read_text(encoding="utf-8"), "改动留着，下次再推")

    def test_a_shops_conflict_stops_the_sync_but_not_the_plan(self):
        self.seed_repo()
        m1 = self.machine("m1")
        m1.prepare()
        m1.cfg.shop_csv.write_text(SHOPS_CSV.replace("23", "30"), encoding="utf-8")
        self.edit_remote({"shops.csv": SHOPS_CSV.replace(",8,1", ",9,1").encode("utf-8")},
                         message="另一台机器改共享清单")

        result = m1.prepare(now=NEXT_RUN)

        self.assertEqual(result.shops_sync.action, shops_sync.SyncAction.CONFLICT)
        self.assertEqual(result.status, plan_step.PrepStatus.READY,
                         "两边都变只停下清单同步，不拦计划确认")
        self.assertTrue(any("两边都变" in w for w in result.warnings))


if __name__ == "__main__":
    unittest.main()

