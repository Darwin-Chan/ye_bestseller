"""采集进程互斥：同一时刻至多一个，界面与命令行共用同一把锁（工单 02）。"""
import os
import io
import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import pipeline, rounds, single_instance
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, DayBoundaryReached, connect
from bestseller_monitor.rounds import TerminalReason
from helpers import isolated_locks

SHOPS = [Shop("A01", "店铺A", "https://shop.example/")]


class CrawlerMutexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.cfg = SimpleNamespace(db_file=self.db_path, driver="pw_cdp",
                                   ensure_dirs=MagicMock())

    def tearDown(self):
        self.tmp.cleanup()

    def _identity_row(self):
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM crawler_process WHERE id=1").fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def _round_count(self) -> int:
        conn = connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        finally:
            conn.close()

    def test_a_second_crawler_is_refused_without_touching_the_database(self):
        with isolated_locks():
            holding = single_instance.acquire(single_instance.CRAWLER_LOCK)
            try:
                with self.assertRaises(pipeline.CrawlerAlreadyRunning):
                    pipeline.run_round(self.cfg, SHOPS)
            finally:
                holding.release()

        self.assertEqual(self._round_count(), 0, "被拒绝的采集不该留下轮次")
        self.assertIsNone(self._identity_row(), "被拒绝的采集不该登记身份")

    def test_the_running_crawler_registers_itself_and_clears_it_afterwards(self):
        seen = {}

        def driver(_db, _cfg, _round_id, _shops):
            seen.update(self._identity_row() or {})

        with isolated_locks():
            with patch.object(pipeline, "_run_pwcdp_round", side_effect=driver):
                pipeline.run_round(self.cfg, SHOPS)

            self.assertFalse(single_instance.is_held(single_instance.CRAWLER_LOCK),
                             "一轮跑完要放锁，否则续跑和下一轮都启动不了")

        self.assertEqual(seen["pid"], os.getpid())
        self.assertIsNotNone(seen["round_id"], "身份行要对得上正在跑的轮次")
        self.assertIsNone(self._identity_row(), "进程走了就不该留着身份行")

    def _abandon(self, round_id: int) -> None:
        """模拟界面那一刀：把轮次收尾为「人工放弃」。"""
        conn = connect(self.db_path)
        try:
            db = Database(conn)
            rounds.finish(db, rounds.load(db, round_id), TerminalReason.ABANDONED,
                          note="GUI 人工中止（放弃）")
        finally:
            conn.close()

    def _terminal(self):
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT terminal_reason, note FROM rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return (row["terminal_reason"], row["note"]) if row is not None else None
        finally:
            conn.close()

    def test_a_late_finish_does_not_overwrite_the_abandoned_round(self):
        """界面中止后，还在跑的采集进程到检查点停下：不崩，也不改回「跨天中止」。"""
        def abandon_then_stop(_db, _cfg, round_id, _shops):
            self._abandon(round_id)
            raise DayBoundaryReached()

        with isolated_locks():
            with patch.object(pipeline, "_run_pwcdp_round", side_effect=abandon_then_stop):
                pipeline.run_round(self.cfg, SHOPS)

        self.assertEqual(self._terminal(), ("ABANDONED", "GUI 人工中止（放弃）"))

    def test_finalizing_does_not_overwrite_the_abandoned_round(self):
        """采集跑完时轮次已经被中止：正常收尾也不该把它改写成「完成」。"""
        def finish_shop_then_abandon(db, _cfg, round_id, _shops):
            db.save_shop_offers(round_id, "A01", "https://shop.example/", "店铺A", [],
                                pages_read=1, confirmed_empty=True)
            self._abandon(round_id)

        with isolated_locks():
            with patch.object(pipeline, "_run_pwcdp_round", side_effect=finish_shop_then_abandon):
                pipeline.run_round(self.cfg, SHOPS)

        self.assertEqual(self._terminal(), ("ABANDONED", "GUI 人工中止（放弃）"))

    def test_a_late_stop_does_not_claim_the_original_reason(self):
        """被人工中止的采集到检查点停下：日志与输出不该说成「跨天中止」。"""
        def abandon_then_stop(_db, _cfg, round_id, _shops):
            self._abandon(round_id)
            raise DayBoundaryReached()

        out = io.StringIO()
        with isolated_locks():
            with patch.object(pipeline, "_run_pwcdp_round", side_effect=abandon_then_stop), \
                    self.assertLogs("bestseller_monitor.pipeline", level="INFO") as logged, \
                    contextlib.redirect_stdout(out):
                pipeline.run_round(self.cfg, SHOPS)

        text = "\n".join(logged.output) + out.getvalue()
        self.assertNotIn("库存数据即将跨天", text, "迟到停下不等于跨天")
        self.assertIn("ABANDONED", text, "要说清本轮已经是别的原因收的尾")

if __name__ == "__main__":
    unittest.main()
