import logging
import tempfile
import unittest
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gui
from bestseller_monitor import browser_proc
from bestseller_monitor.db import Database, connect, DETAIL_BUDGET_NOTE
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

                self.assertEqual(result["status"], "详情预算耗尽")
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

                self.assertEqual(result["status"], "进行中")
                self.assertEqual(result["duration_text"], "2 分")
                api._current_elapsed.assert_called_once_with()
            finally:
                conn.close()


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


if __name__ == "__main__":
    unittest.main()
