import tempfile
import unittest
from pathlib import Path
from threading import RLock
from unittest.mock import MagicMock, patch

import gui
from bestseller_monitor.db import Database, connect
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


if __name__ == "__main__":
    unittest.main()
