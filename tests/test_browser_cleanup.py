import os
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_proc, browser_pw

LOG = "bestseller_monitor"


class CloseSessionTests(unittest.TestCase):
    """轮次收尾必须关掉本任务启动的浏览器（IS-43）。"""

    def test_still_closes_browser_when_launched_process_already_exited(self):
        """同一 profile 已有实例时，本轮 msedge.exe 交接后立刻退出；
        真正在跑的是旧实例（CDP browser PID 指向它），收尾仍须关掉。"""
        handed_off = SimpleNamespace(pid=22008, poll=lambda: 0)
        with patch.object(browser_pw, "_launched_proc", handed_off), \
                patch.object(browser_pw, "_launched_port", 9222), \
                patch.object(browser_pw, "cdp_browser_pid", return_value=6104), \
                patch.object(browser_proc, "process_image_name", return_value="msedge.exe"), \
                patch.object(browser_proc, "terminate_process_tree") as kill, \
                self.assertLogs(LOG, level="WARNING") as logs:
            browser_pw.close_session(MagicMock(), MagicMock())

        kill.assert_called_once_with(6104)
        self.assertIn("交接", "\n".join(logs.output))

    def test_closes_own_process_while_it_is_still_alive(self):
        """常规情形：本轮启动的浏览器还在，按自己的 PID 关掉。"""
        alive = SimpleNamespace(pid=10500, poll=lambda: None)
        with patch.object(browser_pw, "_launched_proc", alive), \
                patch.object(browser_pw, "_launched_port", 9222), \
                patch.object(browser_pw, "cdp_browser_pid", return_value=10500), \
                patch.object(browser_proc, "terminate_process_tree") as kill, \
                self.assertLogs(LOG, level="INFO"):
            browser_pw.close_session(MagicMock(), MagicMock())

        kill.assert_called_once_with(10500)

    def test_attached_session_does_not_close_user_browser(self):
        """start_browser=false 接管既有实例时不动用户的浏览器，但要留下可见记录。"""
        with patch.object(browser_pw, "_launched_proc", None), \
                patch.object(browser_pw, "_launched_port", 9222), \
                patch.object(browser_pw, "cdp_browser_pid", return_value=None), \
                patch.object(browser_proc, "terminate_process_tree") as kill, \
                self.assertLogs(LOG, level="INFO") as logs:
            browser_pw.close_session(MagicMock(), MagicMock())

        kill.assert_not_called()
        self.assertIn("跳过关闭", "\n".join(logs.output))

    def test_falls_back_to_port_owner_when_cdp_has_no_answer(self):
        """CDP 取不到 browser PID 时，用调试端口占用者兜底。"""
        handed_off = SimpleNamespace(pid=22008, poll=lambda: 0)
        with patch.object(browser_pw, "_launched_proc", handed_off), \
                patch.object(browser_pw, "_launched_port", 9222), \
                patch.object(browser_pw, "cdp_browser_pid", return_value=None), \
                patch.object(browser_proc, "listen_port_owner", return_value=777), \
                patch.object(browser_proc, "process_image_name", return_value="msedge.exe"), \
                patch.object(browser_proc, "terminate_process_tree") as kill, \
                self.assertLogs(LOG, level="WARNING"):
            browser_pw.close_session(MagicMock(), MagicMock())

        kill.assert_called_once_with(777)

    def test_refuses_to_kill_non_browser_port_owner(self):
        """端口被非浏览器进程占用时不误杀，并明确告警。"""
        handed_off = SimpleNamespace(pid=22008, poll=lambda: 0)
        with patch.object(browser_pw, "_launched_proc", handed_off), \
                patch.object(browser_pw, "_launched_port", 9222), \
                patch.object(browser_pw, "cdp_browser_pid", return_value=None), \
                patch.object(browser_proc, "listen_port_owner", return_value=999), \
                patch.object(browser_proc, "process_image_name", return_value="nginx.exe"), \
                patch.object(browser_proc, "terminate_process_tree") as kill, \
                self.assertLogs(LOG, level="WARNING") as logs:
            browser_pw.close_session(MagicMock(), MagicMock())

        kill.assert_not_called()
        self.assertIn("不是浏览器", "\n".join(logs.output))

    def test_no_browser_on_port_is_reported_not_silent(self):
        """端口上确实没有浏览器时不报错，但要留下可见记录。"""
        handed_off = SimpleNamespace(pid=22008, poll=lambda: 0)
        with patch.object(browser_pw, "_launched_proc", handed_off), \
                patch.object(browser_pw, "_launched_port", 9222), \
                patch.object(browser_pw, "cdp_browser_pid", return_value=None), \
                patch.object(browser_proc, "listen_port_owner", return_value=None), \
                patch.object(browser_proc, "terminate_process_tree") as kill, \
                self.assertLogs(LOG, level="INFO") as logs:
            browser_pw.close_session(MagicMock(), MagicMock())

        kill.assert_not_called()
        self.assertIn("无需关闭", "\n".join(logs.output))


class CloseBrowserTests(unittest.TestCase):
    """close_browser 的归属判定（爬虫与 GUI 共用的入口）。"""

    def test_attach_only_reports_skip(self):
        with patch.object(browser_proc, "terminate_process_tree") as kill, \
                self.assertLogs(LOG, level="INFO") as logs:
            self.assertIsNone(browser_proc.close_browser(9222, launched_by_us=False))

        kill.assert_not_called()
        self.assertIn("跳过关闭", "\n".join(logs.output))

    def test_kills_own_process_without_image_lookup(self):
        with patch.object(browser_proc, "process_image_name") as image, \
                patch.object(browser_proc, "terminate_process_tree", return_value=True) as kill:
            self.assertEqual(
                browser_proc.close_browser(9222, launched_by_us=True, own_pid=10500), 10500)

        kill.assert_called_once_with(10500)
        image.assert_not_called()

    def test_returns_none_when_taskkill_fails(self):
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


class AbandonLaunchedBrowserTests(unittest.TestCase):
    """会话没建起来时，别把本任务拉起的浏览器丢在后台（ADR-0019 的收尾只关自己那份）。"""

    def test_terminates_our_own_still_alive_process(self):
        alive = SimpleNamespace(pid=20301, poll=lambda: None)
        with patch.object(browser_pw, "_launched_proc", alive), \
                patch.object(browser_pw, "_launched_port", 9222), \
                patch.object(browser_proc, "terminate_process_tree") as kill:
            browser_pw._abandon_launched_browser()

        kill.assert_called_once_with(20301)
        self.assertIsNone(browser_pw._launched_proc)
        self.assertIsNone(browser_pw._launched_port)

    def test_leaves_the_port_alone_when_our_process_already_exited(self):
        """同一 profile 有实例时，本次 msedge.exe 交接后退出；端口上那个是用户自己的。"""
        handed_off = SimpleNamespace(pid=22008, poll=lambda: 0)
        with patch.object(browser_pw, "_launched_proc", handed_off), \
                patch.object(browser_pw, "_launched_port", 9222), \
                patch.object(browser_proc, "terminate_process_tree") as kill, \
                self.assertLogs(LOG, level="INFO"):
            browser_pw._abandon_launched_browser()

        kill.assert_not_called()

    def test_noop_when_we_launched_nothing(self):
        with patch.object(browser_pw, "_launched_proc", None), \
                patch.object(browser_proc, "terminate_process_tree") as kill:
            browser_pw._abandon_launched_browser()

        kill.assert_not_called()

    def test_open_session_cleans_up_when_the_debug_port_never_answers(self):
        """端到端那一条：连不上就收掉自己拉起的浏览器，再上抛。"""
        cfg = SimpleNamespace(attach_port=9222, timeout_ms=45000,
                              user_data_path="ignored", chrome_path=os.sys.executable,
                              start_browser=True)
        fake_pw = MagicMock()
        fake_pw.start.return_value = fake_pw
        fake_pw.chromium.connect_over_cdp.side_effect = RuntimeError("拒绝连接")
        alive = SimpleNamespace(pid=20302, poll=lambda: None)
        # open_session 会写这两个模块级全局：不管成不成，跑完都擦干净，别污染别的用例。
        self.addCleanup(setattr, browser_pw, "_launched_proc", None)
        self.addCleanup(setattr, browser_pw, "_launched_port", None)
        with patch("playwright.sync_api.sync_playwright", return_value=fake_pw), \
                patch.object(browser_pw, "_WAIT_LAUNCH_SEC", 0.05), \
                patch.object(browser_pw.time, "sleep"), \
                patch.object(browser_pw.subprocess, "Popen", return_value=alive), \
                patch.object(browser_proc, "terminate_process_tree") as kill:
            with self.assertRaises(RuntimeError):
                browser_pw.open_session(cfg)

        kill.assert_called_once_with(20302)


class ProcessLookupTests(unittest.TestCase):
    """解析函数对真实系统输出有效（不 mock OS）。"""

    # 真实 tasklist /FO CSV /NH 输出样本（本机实测）
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
