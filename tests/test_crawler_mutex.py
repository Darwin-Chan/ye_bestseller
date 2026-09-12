"""采集进程互斥：同一时刻至多一个，界面与命令行共用同一把锁（工单 02）。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import pipeline, single_instance
from bestseller_monitor.config import Shop
from bestseller_monitor.db import connect
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

if __name__ == "__main__":
    unittest.main()
