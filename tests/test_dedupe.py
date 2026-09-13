"""同日去重与补采 module 的行为：详情机会与尝试额度。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor import dedupe, rounds
from bestseller_monitor.db import Database, DetailBudgetExhausted, connect
from bestseller_monitor.rounds import TerminalReason
from helpers import new_round


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.db = Database(self.conn)
        self.rid = new_round(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def used(self) -> int:
        return self.db.detail_opportunity_total(self.rid)

    def test_retrying_same_offer_takes_one_slot(self):
        dedupe.claim_slot(self.db, self.rid, "A01", "111", 2)
        dedupe.claim_slot(self.db, self.rid, "A01", "111", 2)
        self.assertEqual(self.used(), 1, "同一商品的补采复用同一次详情机会")

    def test_budget_exhaustion_blocks_new_offer_but_allows_retry(self):
        dedupe.claim_slot(self.db, self.rid, "A01", "111", 1)
        with self.assertRaises(DetailBudgetExhausted):
            dedupe.claim_slot(self.db, self.rid, "A01", "222", 1)
        # 已占过机会的商品只是重试，不该被预算耗尽挡住
        dedupe.claim_slot(self.db, self.rid, "A01", "111", 1)
        self.assertEqual(self.used(), 1)

    def test_budget_limit_binds_to_round_not_to_later_config(self):
        dedupe.claim_slot(self.db, self.rid, "A01", "111", 1)
        # 续跑时即使配置放宽，仍沿用轮次已记录的上限
        with self.assertRaises(DetailBudgetExhausted):
            dedupe.claim_slot(self.db, self.rid, "A01", "222", 5)

    def test_budget_survives_reopening_the_database(self):
        """重启不能绕过单轮预算：重新打开同一个库后，已用尽的预算仍然用尽。"""
        path = Path(self.tmp.name) / "restart.db"
        first = connect(path)
        try:
            db = Database(first)
            rid = new_round(db)
            dedupe.claim_slot(db, rid, "A01", "111", 1)
        finally:
            first.close()

        second = connect(path)
        try:
            db = Database(second)
            self.assertEqual(db.detail_opportunity_total(rid), 1)
            with self.assertRaises(DetailBudgetExhausted):
                dedupe.claim_slot(db, rid, "A01", "222", 1)
            # 已经占过机会的商品只是重试，不该被预算耗尽挡住
            dedupe.claim_slot(db, rid, "A01", "111", 1)
            self.assertEqual(db.detail_opportunity_total(rid), 1)
        finally:
            second.close()

    def test_binding_merges_when_offer_already_holds_a_slot(self):
        dedupe.claim_slot(self.db, self.rid, "A01", "33", 5)
        dedupe.claim_slot(self.db, self.rid, "A01", "card:p2:i1", 5)
        self.assertEqual(self.used(), 2)
        dedupe.bind_card_to_offer(self.db, self.rid, "A01", "card:p2:i1", "33")
        self.assertEqual(self.used(), 1, "同一商品的两份机会合并成一次")

    def test_rescanning_a_bound_card_does_not_take_a_second_slot(self):
        """重扫同一张卡片（补抓/排序重排）不该重复占用预算。"""
        dedupe.claim_slot(self.db, self.rid, "A01", "card:p1:i0", 1)
        dedupe.bind_card_to_offer(self.db, self.rid, "A01", "card:p1:i0", "22")
        dedupe.claim_slot(self.db, self.rid, "A01", "card:p1:i0", 1)
        dedupe.claim_slot(self.db, self.rid, "A01", "22", 1)
        self.assertEqual(self.used(), 1)

    def test_released_slot_frees_budget_for_other_offers(self):
        """同日跳过要退还机会，否则会白白吃掉别的商品的预算。"""
        dedupe.claim_slot(self.db, self.rid, "A01", "card:p1:i0", 1)
        dedupe.bind_card_to_offer(self.db, self.rid, "A01", "card:p1:i0", "22")
        dedupe.release_slot(self.db, self.rid, "A01", "22")
        self.assertEqual(self.used(), 0)
        dedupe.claim_slot(self.db, self.rid, "A01", "33", 1)
        self.assertEqual(self.used(), 1)

    def test_next_attempt_continues_from_earlier_retry_in_same_round(self):
        self.assertEqual(dedupe.next_attempt(self.db, self.rid, "A01", "111"), 1)
        self.db.mark_failure(
            self.rid, "A01", "111", 1, "解析失败",
            shop_url="https://a.example/", shop_name="店铺A",
            product_url="https://detail.1688.com/offer/111.html",
        )
        self.assertEqual(dedupe.next_attempt(self.db, self.rid, "A01", "111"), 2)

    def test_next_attempt_starts_over_in_a_new_round(self):
        self.db.mark_failure(
            self.rid, "A01", "111", 2, "解析失败",
            shop_url="https://a.example/", shop_name="店铺A",
            product_url="https://detail.1688.com/offer/111.html",
        )
        rounds.finish(self.db, rounds.load(self.db, self.rid), TerminalReason.ABANDONED)
        next_round = new_round(self.db)
        self.assertEqual(dedupe.next_attempt(self.db, next_round, "A01", "111"), 1)

    def test_connect_migrates_legacy_rounds_and_opportunity_tables(self):
        # 模拟旧库：rounds 没有 detail_budget_limit，detail_opportunities 还是 attempts 列
        old = Path(tempfile.gettempdir()) / f"bestseller_budget_{id(self)}.db"
        raw = sqlite3.connect(str(old))
        raw.execute(
            "CREATE TABLE rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "started_at TEXT NOT NULL, finished_at TEXT, "
            "status TEXT NOT NULL DEFAULT '进行中', phase TEXT NOT NULL DEFAULT 'listing', note TEXT)"
        )
        raw.execute(
            "CREATE TABLE detail_opportunities (round_id INTEGER NOT NULL, "
            "shop_key TEXT NOT NULL, identity TEXT NOT NULL, "
            "attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
            "PRIMARY KEY (round_id, shop_key, identity))"
        )
        raw.execute("INSERT INTO rounds(id, started_at) VALUES (1, '2026-09-04T00:00:00+00:00')")
        raw.commit()
        raw.close()

        conn = connect(old)
        try:
            round_cols = [row[1] for row in conn.execute('PRAGMA table_info("rounds")').fetchall()]
            self.assertIn("detail_budget_limit", round_cols)
            self.assertIn("terminal_reason", round_cols)
            self.assertNotIn("status", round_cols, "过渡用的中文状态列应当在迁移时删掉")
            opp_cols = [
                row[1] for row in
                conn.execute('PRAGMA table_info("detail_opportunities")').fetchall()
            ]
            self.assertIn("offer_id", opp_cols)
            dedupe.claim_slot(Database(conn), 1, "A01", "111", 1)
        finally:
            conn.close()
            old.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
