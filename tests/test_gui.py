import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gui
from bestseller_monitor import browser_proc, rounds
from bestseller_monitor.config import Shop
from bestseller_monitor.db import CST, Database, connect, cst_date, DETAIL_BUDGET_NOTE
from bestseller_monitor.rounds import RoundRequest, ShopScope, TerminalReason
from gui import Api


class GuiWindowHeightTests(unittest.TestCase):
    def test_uses_preferred_height_on_tall_screen(self):
        with patch("gui._screen_work_height", return_value=2160):
            self.assertEqual(gui._default_window_height(), 1354)

    def test_shrinks_to_fit_short_screen(self):
        with patch("gui._screen_work_height", return_value=1000):
            self.assertEqual(gui._default_window_height(), 940)

    def test_falls_back_to_preferred_height_without_screen_info(self):
        with patch("gui._screen_work_height", return_value=None):
            self.assertEqual(gui._default_window_height(), 1354)

    def test_stays_at_min_height_on_tiny_screen(self):
        with patch("gui._screen_work_height", return_value=600):
            self.assertEqual(gui._default_window_height(), 720)


class GuiResultTests(unittest.TestCase):
    def test_detail_budget_exhausted_reports_its_own_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            try:
                db = Database(conn)
                round_id = db.start_or_resume()
                db.finish_round(round_id, status="详情预算耗尽", note=DETAIL_BUDGET_NOTE)
                api = Api.__new__(Api)
                api._lock = RLock()
                api.round_id = round_id
                api._open_conn = lambda: conn

                result = api.get_result()

                self.assertEqual(result["reason"], "DETAIL_BUDGET_EXHAUSTED")
                self.assertEqual(result["tag"], "预算耗尽")
                self.assertIn("详情预算", result["note"])
                self.assertNotIn("deny", result["note"])
            finally:
                conn.close()

    def test_resumable_interruption_uses_current_elapsed_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            try:
                round_id = Database(conn).start_or_resume()
                api = Api.__new__(Api)
                api._lock = RLock()
                api.round_id = round_id
                api._current_elapsed = MagicMock(return_value=125.0)
                api._open_conn = lambda: conn

                result = api.get_result()

                self.assertIsNone(result["reason"])
                self.assertEqual(result["tag"], "进行中")
                self.assertEqual(result["duration_text"], "2 分")
                api._current_elapsed.assert_called_once_with()
            finally:
                conn.close()

    def test_every_terminal_reason_has_result_text(self):
        for reason in TerminalReason:
            with self.subTest(reason=reason):
                tag, note = gui._terminal_text(reason)
                self.assertTrue(tag)
                self.assertTrue(note)

        self.assertEqual(gui._terminal_text(None), ("进行中", "本轮仍在进行；未抓取店铺见下方。"))


class GuiBrowserCleanupTests(unittest.TestCase):
    """暂停 / 中止后收尾本任务启动的浏览器：抓取进程被强杀不会执行它的 finally（IS-43）。"""

    @staticmethod
    def _api(start_browser=True, attach_port=9222):
        api = Api.__new__(Api)
        api._lock = RLock()
        api.proc = None
        api.round_id = None
        api.user_paused = False
        api._elapsed_base = 0.0
        api._run_start_ts = None
        api.cfg = SimpleNamespace(start_browser=start_browser, attach_port=attach_port)
        return api

    def test_pause_closes_browser_launched_by_the_task(self):
        api = self._api()
        with patch.object(Api, "_kill_proc") as kill_proc, \
                patch.object(browser_proc, "close_browser", return_value=6104) as close:
            api.pause_run()

        kill_proc.assert_called_once_with()
        close.assert_called_once_with(9222, launched_by_us=True)

    def test_abort_closes_browser_launched_by_the_task(self):
        api = self._api()
        with patch.object(Api, "_kill_proc"), \
                patch.object(browser_proc, "close_browser", return_value=6104) as close:
            api.abort_run()

        close.assert_called_once_with(9222, launched_by_us=True)

    def test_pause_keeps_user_browser_when_attaching(self):
        api = self._api(start_browser=False)
        with patch.object(Api, "_kill_proc"), \
                patch.object(browser_proc, "close_browser") as close:
            api.pause_run()

        close.assert_not_called()

    def test_browser_cleanup_waits_for_a_browser_that_starts_late(self):
        """刚启动就被暂停时端口还没监听，收尾要短暂重试。"""
        api = self._api()
        with patch.object(gui, "_BROWSER_CLOSE_RETRY_SEC", 5.0), \
                patch.object(gui.time, "sleep") as sleep, \
                patch.object(browser_proc, "close_browser",
                             side_effect=[None, 4242]) as close:
            pid = api._kill_browser()

        self.assertEqual(pid, 4242)
        self.assertEqual(close.call_count, 2)
        sleep.assert_called_once()


class GuiLoggingTests(unittest.TestCase):
    def test_gui_records_its_own_actions_to_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(logs_dir=Path(tmp))
            handler = gui._configure_gui_logging(cfg)
            try:
                logging.getLogger("bestseller_monitor.browser_proc").warning("暂停后收尾浏览器")
                text = (Path(tmp) / "gui.log").read_text(encoding="utf-8")
            finally:
                logging.getLogger().removeHandler(handler)
                handler.close()

        self.assertIn("暂停后收尾浏览器", text)


class GuiRoundScopeTests(unittest.TestCase):
    """界面勾选店铺与命令行 --limit-shops 是同一套范围语义（工单 02）。"""

    @staticmethod
    def _api(db_path):
        api = Api.__new__(Api)
        api._lock = RLock()
        api.proc = None
        api.round_id = None
        api.start_ts = None
        api.user_paused = False
        api._elapsed_base = 0.0
        api._run_start_ts = None
        api.shops = [
            Shop("A01", "店铺A", "https://A01.example/"),
            Shop("A02", "店铺B", "https://A02.example/"),
        ]
        api.cfg = SimpleNamespace(max_pages_per_shop=3)
        # start_run 会关掉自己开的连接，所以每次都要给一条新的。
        api._open_conn = lambda: connect(db_path)
        return api

    @staticmethod
    def _scope(db_path, round_id):
        conn = connect(db_path)
        try:
            return sorted(
                row["shop_key"] for row in conn.execute(
                    "SELECT shop_key FROM shop_rounds WHERE round_id=?", (round_id,)
                )
            )
        finally:
            conn.close()

    @staticmethod
    def _row(db_path, round_id) -> dict:
        conn = connect(db_path)
        try:
            return dict(conn.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone())
        finally:
            conn.close()

    @staticmethod
    def _stale_round(db_path, keys) -> int:
        """昨天开始、还在「进行中」的轮次：跨日之后它不该再被续跑。"""
        yesterday = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")
        conn = connect(db_path)
        try:
            return rounds.open(Database(conn), RoundRequest(
                yesterday,
                tuple(ShopScope(k, f"https://{k}.example/", f"店铺{k}") for k in keys),
            )).round.id
        finally:
            conn.close()

    def test_start_run_creates_round_with_checked_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)

            with patch.object(Api, "_spawn_crawler") as spawn:
                result = api.start_run(["A02"])

            self.assertTrue(result["ok"])
            # 轮次由采集子进程创建：界面只传范围，启动失败不会留下空轮次挡住下一次。
            conn = connect(db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 0)
            finally:
                conn.close()
            spawn.assert_called_once_with(["A02"])

    def test_start_run_reports_scope_mismatch_instead_of_merging(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)
            conn = connect(db_path)
            try:
                rid = rounds.open(Database(conn), RoundRequest(
                    cst_date(), (ShopScope("A01", "https://A01.example/", "店铺A"),),
                )).round.id
            finally:
                conn.close()

            with patch.object(Api, "_spawn_crawler") as spawn:
                self.assertTrue(api.start_run(["A01"])["ok"])
                result = api.start_run(["A02"])

            self.assertFalse(result["ok"])
            self.assertIn("范围", result["error"])
            spawn.assert_called_once_with(["A01"])
            conn = connect(db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 1)
                self.assertEqual(self._scope(db_path, rid), ["A01"])
            finally:
                conn.close()

    def test_start_page_reports_a_stale_round_without_touching_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)
            stale = self._stale_round(db_path, ["A01"])

            start = api.get_start()

            self.assertIn("跨天", start["start_hint"])
            self.assertIn(f"#{stale}", start["start_hint"])
            self.assertEqual(self._row(db_path, stale)["terminal_reason"], None)  # 浏览不改数据
            self.assertEqual(self._row(db_path, stale)["status"], "进行中")

    def test_resume_refuses_a_round_from_a_previous_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)
            self._stale_round(db_path, ["A01"])

            with patch.object(Api, "_spawn_crawler") as spawn:
                result = api.resume_run()

            self.assertFalse(result["ok"])
            self.assertIn("跨天", result["error"])
            spawn.assert_not_called()

    def test_resume_continues_todays_round_without_restating_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)
            conn = connect(db_path)
            try:
                rid = rounds.open(Database(conn), RoundRequest(
                    cst_date(), (ShopScope("A01", "https://A01.example/", "店铺A"),),
                )).round.id
            finally:
                conn.close()

            with patch.object(Api, "_spawn_crawler") as spawn:
                result = api.resume_run()

            self.assertTrue(result["ok"])
            self.assertEqual(result["round_id"], rid)
            spawn.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
