import tempfile
import unittest
from pathlib import Path
from threading import RLock
from unittest.mock import MagicMock

from bestseller_monitor.db import Database, connect
from gui import Api


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
