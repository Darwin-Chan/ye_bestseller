import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from bestseller_monitor.db import (
    CST,
    DAY_BOUNDARY_NOTE,
    DETAIL_BUDGET_NOTE,
    LEGACY_STATUS_REASONS,
    LEGACY_UNKNOWN_REASON,
    Database,
    connect,
)
from bestseller_monitor.rounds import (
    RoundAlreadyFinished,
    RoundRequest,
    ScopeMismatch,
    ShopScope,
    TerminalReason,
    finish,
    open,
)

LEGACY_ROUNDS_DDL = (
    "CREATE TABLE rounds ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " started_at TEXT NOT NULL,"
    " finished_at TEXT,"
    " status TEXT NOT NULL DEFAULT '进行中',"
    " phase TEXT NOT NULL DEFAULT 'listing',"
    " note TEXT,"
    " detail_budget_limit INTEGER)"
)


def _shops(*keys: str) -> tuple[ShopScope, ...]:
    return tuple(
        ShopScope(key=key, url=f"https://{key}.example/", name=f"店铺{key}")
        for key in keys
    )


class RoundMigrationTests(unittest.TestCase):
    """打开旧库时补上轮次日期与终态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "legacy.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _legacy_db(self, rows):
        conn = sqlite3.connect(str(self.path))
        conn.execute(LEGACY_ROUNDS_DDL)
        for started_at, status, note in rows:
            conn.execute(
                "INSERT INTO rounds(started_at, status, phase, note) VALUES (?, ?, 'listing', ?)",
                (started_at, status, note),
            )
        conn.commit()
        conn.close()

    def test_backfills_date_and_terminal_reason(self):
        self._legacy_db([
            ("2026-09-08T15:13:11+00:00", "意外中止",
             "本轮因整轮 deny 超过阈值而意外中止：整轮 10 分钟内 deny≥10；已抓取数据已保留，不可续跑"),
            ("2026-09-11T15:53:25+00:00", "意外中止", DAY_BOUNDARY_NOTE),
            ("2026-09-09T01:35:18+00:00", "完成", None),
            ("2026-09-07T02:00:00+00:00", "已放弃", "GUI 人工中止（放弃）"),
            ("2026-09-06T02:00:00+00:00", "需人工-失败率超限", "失败率 80% 超过阈值 50%"),
            ("2026-09-05T02:00:00+00:00", "详情预算耗尽", DETAIL_BUDGET_NOTE),
            ("2026-09-12T02:00:00+00:00", "进行中", None),
            ("2026-09-04T02:00:00+00:00", "某个没见过的状态", None),
        ])

        conn = connect(self.path)
        try:
            rows = {
                row["id"]: dict(row)
                for row in conn.execute("SELECT * FROM rounds ORDER BY id")
            }
        finally:
            conn.close()

        self.assertEqual(rows[1]["run_date"], "2026-09-08")
        self.assertEqual(rows[1]["terminal_reason"], "DENY_EXCEEDED")
        self.assertEqual(rows[2]["run_date"], "2026-09-11")
        self.assertEqual(rows[2]["terminal_reason"], "DAY_BOUNDARY")
        self.assertEqual(rows[3]["run_date"], "2026-09-09")
        self.assertEqual(rows[3]["terminal_reason"], "COMPLETED")
        self.assertEqual(rows[4]["terminal_reason"], "ABANDONED")
        self.assertEqual(rows[5]["terminal_reason"], "FAIL_RATE_EXCEEDED")
        self.assertEqual(rows[6]["terminal_reason"], "DETAIL_BUDGET_EXHAUSTED")
        self.assertEqual(rows[7]["run_date"], "2026-09-12")
        self.assertIsNone(rows[7]["terminal_reason"])  # 「进行中」没有终态
        self.assertEqual(rows[8]["terminal_reason"], LEGACY_UNKNOWN_REASON)

    def test_migration_is_repeatable(self):
        self._legacy_db([("2026-09-08T15:13:11+00:00", "完成", None)])
        first = connect(self.path)
        before = dict(first.execute("SELECT * FROM rounds WHERE id=1").fetchone())
        first.close()

        second = connect(self.path)
        try:
            after = dict(second.execute("SELECT * FROM rounds WHERE id=1").fetchone())
        finally:
            second.close()

        self.assertEqual(before, after)
        self.assertEqual(after["run_date"], "2026-09-08")
        self.assertEqual(after["terminal_reason"], "COMPLETED")

    def test_legacy_status_mapping_stays_within_terminal_reason(self):
        known = {reason.value for reason in TerminalReason}
        self.assertIn(LEGACY_UNKNOWN_REASON, known)
        self.assertIn(TerminalReason.DAY_BOUNDARY.value, known)
        for status, reason in LEGACY_STATUS_REASONS.items():
            with self.subTest(status=status):
                self.assertIn(reason, known)


class RoundModuleTests(unittest.TestCase):
    """轮次接口：身份、续跑资格与终态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.db = Database(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _round_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]

    def _row(self, round_id: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone())

    def test_open_creates_round_with_date_and_scope(self):
        result = open(self.db, RoundRequest("2026-09-12", _shops("A01", "A02")))

        self.assertTrue(result.created)
        self.assertEqual(result.superseded, ())
        self.assertEqual(result.round.run_date, "2026-09-12")
        self.assertEqual(result.round.shop_keys, ("A01", "A02"))
        self.assertTrue(result.round.in_progress)
        self.assertIsNone(result.round.reason)

        row = self._row(result.round.id)
        self.assertEqual(row["run_date"], "2026-09-12")
        self.assertEqual(row["status"], "进行中")
        self.assertIsNone(row["terminal_reason"])
        scope = {
            r["shop_key"]: r for r in self.conn.execute(
                "SELECT * FROM shop_rounds WHERE round_id=?", (result.round.id,)
            )
        }
        self.assertEqual(set(scope), {"A01", "A02"})
        self.assertEqual(scope["A01"]["shop_name"], "店铺A01")
        self.assertEqual(scope["A01"]["shop_url"], "https://A01.example/")

    def test_open_resumes_same_date_and_scope(self):
        first = open(self.db, RoundRequest("2026-09-12", _shops("A01")))
        second = open(self.db, RoundRequest("2026-09-12", _shops("A01")))

        self.assertFalse(second.created)
        self.assertEqual(second.round.id, first.round.id)
        self.assertEqual(second.superseded, ())
        self.assertEqual(self._round_count(), 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM shop_rounds").fetchone()[0], 1
        )

    def test_open_supersedes_previous_day(self):
        first = open(self.db, RoundRequest("2026-09-11", _shops("A01")))
        second = open(self.db, RoundRequest("2026-09-12", _shops("A02")))

        self.assertTrue(second.created)
        self.assertNotEqual(second.round.id, first.round.id)
        self.assertEqual([r.id for r in second.superseded], [first.round.id])

        stale = self._row(first.round.id)
        self.assertEqual(stale["terminal_reason"], "DAY_BOUNDARY")
        self.assertEqual(stale["status"], "意外中止")
        self.assertEqual(stale["phase"], "done")
        self.assertIsNotNone(stale["finished_at"])
        self.assertEqual(self._round_count(), 2)

    def test_open_rejects_scope_change_on_the_same_day(self):
        first = open(self.db, RoundRequest("2026-09-12", _shops("A01")))

        with self.assertRaises(ScopeMismatch):
            open(self.db, RoundRequest("2026-09-12", _shops("A01", "A02")))

        self.assertEqual(self._round_count(), 1)
        self.assertIsNone(self._row(first.round.id)["terminal_reason"])

    def test_open_creates_new_round_after_finish(self):
        first = open(self.db, RoundRequest("2026-09-12", _shops("A01")))
        finish(self.db, first.round, TerminalReason.COMPLETED)

        second = open(self.db, RoundRequest("2026-09-12", _shops("A01")))

        self.assertTrue(second.created)
        self.assertNotEqual(second.round.id, first.round.id)
        self.assertEqual(second.superseded, ())

    def test_finish_writes_terminal_state_and_is_idempotent(self):
        opened = open(self.db, RoundRequest("2026-09-12", _shops("A01")))
        done = finish(self.db, opened.round, TerminalReason.COMPLETED, note="本轮正常完成")

        self.assertEqual(done.reason, TerminalReason.COMPLETED)
        self.assertFalse(done.in_progress)
        row = self._row(opened.round.id)
        self.assertEqual(row["terminal_reason"], "COMPLETED")
        self.assertEqual(row["status"], "完成")
        self.assertEqual(row["phase"], "done")
        self.assertEqual(row["note"], "本轮正常完成")
        self.assertIsNotNone(row["finished_at"])

        finish(self.db, opened.round, TerminalReason.COMPLETED)  # 同因重复收尾可以忽略
        self.assertEqual(self._row(opened.round.id)["note"], "本轮正常完成")

        with self.assertRaises(RoundAlreadyFinished):
            finish(self.db, opened.round, TerminalReason.ABANDONED)
        self.assertEqual(self._row(opened.round.id)["terminal_reason"], "COMPLETED")

    def test_finish_rejects_legacy_unknown(self):
        opened = open(self.db, RoundRequest("2026-09-12", _shops("A01")))

        with self.assertRaises(ValueError):
            finish(self.db, opened.round, TerminalReason.LEGACY_UNKNOWN)

        self.assertIsNone(self._row(opened.round.id)["terminal_reason"])

    def test_legacy_finish_rejects_unknown_status(self):
        """「历史未分类」只能由迁移写入，旧收尾路径也不得产生它。"""
        started = self.db.start_or_resume()

        with self.assertRaises(ValueError):
            self.db.finish_round(started, status="某个没见过的状态")

        row = self._row(started)
        self.assertEqual(row["status"], "进行中")
        self.assertIsNone(row["terminal_reason"])
        self.assertEqual(row["run_date"], datetime.now(CST).strftime("%Y-%m-%d"))

    def test_resumable_and_stops_work_boundaries(self):
        opened = open(self.db, RoundRequest("2026-09-12", _shops("A01")))
        run = opened.round

        before_cutoff = datetime(2026, 9, 12, 23, 54, tzinfo=CST)
        at_cutoff = datetime(2026, 9, 12, 23, 55, tzinfo=CST)
        midnight = datetime(2026, 9, 13, 0, 0, tzinfo=CST)
        next_noon = datetime(2026, 9, 13, 12, 0, tzinfo=CST)

        self.assertTrue(run.resumable_on(before_cutoff))
        self.assertFalse(run.stops_work(before_cutoff))
        self.assertTrue(run.resumable_on(at_cutoff))
        self.assertTrue(run.stops_work(at_cutoff))
        self.assertFalse(run.resumable_on(midnight))
        self.assertTrue(run.stops_work(midnight))
        self.assertFalse(run.resumable_on(next_noon))
        self.assertTrue(run.stops_work(next_noon))

        # ISO 字符串和无时区（按 UTC 解释）都接受
        self.assertTrue(run.stops_work("2026-09-12T15:55:00+00:00"))
        self.assertFalse(run.stops_work(datetime(2026, 9, 12, 15, 54)))

        done = finish(self.db, run, TerminalReason.COMPLETED)
        self.assertFalse(done.resumable_on(before_cutoff))
        self.assertTrue(done.stops_work(before_cutoff))
