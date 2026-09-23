import os
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

    def test_matching_proof_keeps_probing_until_the_bound_target_is_gone(self):
        """终止命令返回时进程可能还没走完：同一 capability 探到退出才算数。"""
        capability = browser_proc.ProcessCapability(6104, 94, "proof")
        with patch.object(browser_proc, "bind_process", return_value=capability), \
                patch.object(browser_proc, "process_capability_alive",
                             side_effect=[True, True, False]) as alive, \
                patch.object(browser_proc.time, "sleep") as sleep, \
                patch.object(browser_proc, "terminate_process_capability",
                             return_value=True) as terminate, \
                patch.object(browser_proc, "release_process_capability"):
            self.assertEqual(browser_proc.close_browser(
                9222, launched_by_us=True, browser_pid=6104,
                browser_os_started="proof"), 6104)

        terminate.assert_called_once_with(capability)
        self.assertEqual(alive.call_count, 3)
        sleep.assert_called_once()

    def test_unverifiable_exit_is_retained_without_retrying(self):
        """核验动作拿不到答案是「不可核验」，保留待清理项，不当成已退出，也不重探。"""
        capability = browser_proc.ProcessCapability(6104, 95, "proof")
        with patch.object(browser_proc, "bind_process", return_value=capability), \
                patch.object(browser_proc, "process_capability_alive",
                             side_effect=[True, None]) as alive, \
                patch.object(browser_proc.time, "sleep") as sleep, \
                patch.object(browser_proc, "terminate_process_capability", return_value=True), \
                patch.object(browser_proc, "release_process_capability"), \
                self.assertLogs(LOG, level="WARNING"):
            self.assertIsNone(browser_proc.close_browser(
                9222, launched_by_us=True, browser_pid=6104,
                browser_os_started="proof"))

        self.assertEqual(alive.call_count, 2)
        sleep.assert_not_called()

    def test_target_that_remains_alive_is_retained_as_a_cleanup_failure(self):
        capability = browser_proc.ProcessCapability(6104, 93, "proof")
        with patch.object(browser_proc, "_VERIFY_EXIT_TIMEOUT_SEC", 0.0), \
                patch.object(browser_proc, "bind_process", return_value=capability), \
                patch.object(browser_proc, "process_capability_alive", return_value=True), \
                patch.object(browser_proc, "terminate_process_capability", return_value=True), \
                patch.object(browser_proc, "release_process_capability"):
            self.assertIsNone(browser_proc.close_browser(
                9222, launched_by_us=True, browser_pid=6104,
                browser_os_started="proof"))

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

    def test_netstat_gbk_output_is_parsed_regardless_of_interpreter_encoding(self):
        """netstat 的文本按 OEM 码页输出（中文 Windows 是 GBK），不能拿解释器默认编码去猜。

        崩过一次：UTF-8 模式（PYTHONUTF8=1；Python 3.15 起是默认）下 text=True 用 UTF-8
        解 GBK，读取线程抛 UnicodeDecodeError，stdout 静默变 None。
        """
        sample = ("\r\n活动连接\r\n\r\n  协议  本地地址          外部地址        状态           PID\r\n"
                  "  TCP    127.0.0.1:50123        0.0.0.0:0              LISTENING       4242\r\n"
                  "  TCP    127.0.0.1:50124        0.0.0.0:0              ESTABLISHED     4243\r\n")
        done = SimpleNamespace(returncode=0, stdout=sample.encode("gbk"))
        with patch.object(browser_proc.subprocess, "run", return_value=done):
            self.assertEqual(browser_proc.listen_port_owner(50123), 4242)

    def test_unreadable_netstat_output_is_reported_as_unavailable(self):
        """解码失败时 subprocess 会把 stdout 留成 None：那时要当「查不到」，不是崩。"""
        done = SimpleNamespace(returncode=0, stdout=None)
        with patch.object(browser_proc.subprocess, "run", return_value=done):
            self.assertIsNone(browser_proc.listen_port_owner(50123))


class ReleaseCapabilityTests(unittest.TestCase):
    """释放句柄只碰真能力句柄：别的对象绝不进 ctypes。

    崩过一次：测试把 MagicMock 当句柄喂进来，ctypes 转换去摸 mock 属性时无限递归，
    Python 3.12/3.13 直接栈溢出杀掉测试进程（3.14 只把它变成可捕获的 RecursionError）。
    """

    def test_a_non_capability_never_reaches_ctypes(self):
        with patch.object(browser_proc, "ctypes") as ct:
            browser_proc.release_process_capability(MagicMock())
        ct.windll.kernel32.CloseHandle.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "ctypes 能力句柄是 Windows 机制")
    def test_a_real_capability_is_closed(self):
        capability = browser_proc.ProcessCapability(6104, 97, "proof")
        with patch.object(browser_proc, "ctypes") as ct:
            browser_proc.release_process_capability(capability)
        ct.windll.kernel32.CloseHandle.assert_called_once_with(97)


if __name__ == "__main__":
    unittest.main()
