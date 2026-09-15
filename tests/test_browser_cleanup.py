import os
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bestseller_monitor import browser_proc

LOG = "bestseller_monitor"


class CloseBrowserTests(unittest.TestCase):
    """Destructive cleanup requires an exact local or proof-bound target."""

    def test_attach_only_reports_skip(self):
        with patch.object(browser_proc, "terminate_process_tree") as kill, \
                self.assertLogs(LOG, level="INFO") as logs:
            self.assertIsNone(browser_proc.close_browser(9222, launched_by_us=False))

        kill.assert_not_called()
        self.assertIn("跳过关闭", "\n".join(logs.output))

    def test_local_owner_pid_never_needs_port_or_image_guessing(self):
        with patch.object(browser_proc, "listen_port_owner") as port_owner, \
                patch.object(browser_proc, "process_image_name") as image, \
                patch.object(browser_proc, "terminate_process_tree", return_value=True) as kill:
            self.assertEqual(
                browser_proc.close_browser(9222, launched_by_us=True, own_pid=10500),
                10500,
            )

        kill.assert_called_once_with(10500)
        port_owner.assert_not_called()
        image.assert_not_called()

    def test_browser_pid_without_creation_proof_is_refused_without_port_fallback(self):
        with patch.object(browser_proc, "bind_process") as bind, \
                patch.object(browser_proc, "listen_port_owner") as port_owner, \
                patch.object(browser_proc, "terminate_process_tree") as kill, \
                self.assertLogs(LOG, level="WARNING"):
            self.assertIsNone(browser_proc.close_browser(
                9222, launched_by_us=True, browser_pid=6104))

        bind.assert_not_called()
        port_owner.assert_not_called()
        kill.assert_not_called()

    def test_changed_creation_proof_is_refused_and_capability_is_released(self):
        capability = browser_proc.ProcessCapability(6104, 91, "different")
        with patch.object(browser_proc, "bind_process", return_value=capability), \
                patch.object(browser_proc, "terminate_process_capability") as terminate, \
                patch.object(browser_proc, "release_process_capability") as release, \
                self.assertLogs(LOG, level="WARNING"):
            self.assertIsNone(browser_proc.close_browser(
                9222, launched_by_us=True, browser_pid=6104,
                browser_os_started="expected"))

        terminate.assert_not_called()
        release.assert_called_once_with(capability)

    def test_matching_proof_terminates_and_verifies_the_bound_capability(self):
        capability = browser_proc.ProcessCapability(6104, 92, "proof")
        with patch.object(browser_proc, "bind_process", return_value=capability), \
                patch.object(browser_proc, "process_capability_alive",
                             side_effect=[True, False]) as alive, \
                patch.object(browser_proc, "terminate_process_capability",
                             return_value=True) as terminate, \
                patch.object(browser_proc, "release_process_capability") as release:
            self.assertEqual(browser_proc.close_browser(
                9222, launched_by_us=True, browser_pid=6104,
                browser_os_started="proof"), 6104)

        self.assertEqual(alive.call_count, 2)
        terminate.assert_called_once_with(capability)
        release.assert_called_once_with(capability)

    def test_target_that_remains_alive_is_retained_as_a_cleanup_failure(self):
        capability = browser_proc.ProcessCapability(6104, 93, "proof")
        with patch.object(browser_proc, "bind_process", return_value=capability), \
                patch.object(browser_proc, "process_capability_alive", return_value=True), \
                patch.object(browser_proc, "terminate_process_capability", return_value=True), \
                patch.object(browser_proc, "release_process_capability"):
            self.assertIsNone(browser_proc.close_browser(
                9222, launched_by_us=True, browser_pid=6104,
                browser_os_started="proof"))

    def test_returns_none_when_local_taskkill_fails(self):
        with patch.object(browser_proc, "terminate_process_tree", return_value=False):
            self.assertIsNone(
                browser_proc.close_browser(9222, launched_by_us=True, own_pid=10500))

    def test_taskkill_failure_is_warned(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="")
        with patch.object(browser_proc.subprocess, "run", return_value=failed), \
                self.assertLogs(LOG, level="WARNING"):
            self.assertFalse(browser_proc.terminate_process_tree(10500))

    def test_taskkill_uses_process_tree_of_given_pid(self):
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(browser_proc.subprocess, "run", return_value=ok) as run:
            self.assertTrue(browser_proc.terminate_process_tree(10500))

        self.assertEqual(run.call_args[0][0], ["taskkill", "/PID", "10500", "/T", "/F"])


class ProcessLookupTests(unittest.TestCase):
    """Lookup helpers remain available for non-destructive diagnostics."""

    TASKLIST_SAMPLE = (
        '"System Idle Process","0","Services","0","8 K"\n'
        '"System","4","Services","0","4,872 K"\n'
        '"pwsh.exe","12808","Console","2","84,112 K"\n'
        '"msedge.exe","15812","Console","2","205,680 K"\n'
        ""
    )

    def test_parses_image_name_from_tasklist_csv(self):
        self.assertEqual(browser_proc.image_name_for_pid(self.TASKLIST_SAMPLE, 12808), "pwsh.exe")
        self.assertEqual(browser_proc.image_name_for_pid(self.TASKLIST_SAMPLE, 15812), "msedge.exe")
        self.assertEqual(browser_proc.image_name_for_pid(self.TASKLIST_SAMPLE, 0),
                         "system idle process")
        self.assertEqual(browser_proc.image_name_for_pid(self.TASKLIST_SAMPLE, 4242), "")

    def test_process_image_name_reads_real_process(self):
        name = browser_proc.process_image_name(os.getpid())
        if not name:
            self.skipTest("tasklist 在本环境不可用（沙箱拒绝进程查询）")
        self.assertEqual(name, os.path.basename(os.sys.executable).lower())

    def test_listen_port_owner_finds_own_listening_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            owner = browser_proc.listen_port_owner(port)
            if owner is None:
                self.skipTest("netstat 在本环境不可用")
            self.assertEqual(owner, os.getpid())
        self.assertIsNone(browser_proc.listen_port_owner(port))


if __name__ == "__main__":
    unittest.main()
