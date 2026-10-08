"""OS read-only liveness boundary: unknown is never treated as gone."""
import os
import unittest
from unittest.mock import Mock, patch
from bestseller_monitor import browser_proc


@unittest.skipUnless(os.name == "nt", "Windows process adapter")
class ProcessPresenceTests(unittest.TestCase):
    def test_current_real_process_is_alive_and_impossible_pid_is_gone(self):
        self.assertIs(browser_proc.process_alive(os.getpid()), True)
        self.assertIs(browser_proc.process_alive(2147483647), False)

    def test_access_denied_and_other_os_errors_remain_unknown(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = None
        with patch.object(browser_proc.ctypes, "WinDLL", return_value=kernel):
            for error, expected in ((5, None), (6, None), (87, False)):
                with self.subTest(error=error), patch.object(browser_proc.ctypes, "get_last_error", return_value=error):
                    self.assertIs(browser_proc.process_alive(4321), expected)
        kernel.CloseHandle.assert_not_called()

    def test_exited_handle_is_closed_and_failed_exit_query_is_unknown(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 91
        kernel.GetExitCodeProcess.return_value = True
        with patch.object(browser_proc.ctypes, "WinDLL", return_value=kernel):
            self.assertIs(browser_proc.process_alive(4321), False)
            kernel.GetExitCodeProcess.return_value = False
            self.assertIsNone(browser_proc.process_alive(4321))
        self.assertEqual(kernel.CloseHandle.call_count, 2)
