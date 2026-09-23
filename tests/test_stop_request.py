"""停止请求：界面请求正在跑的采集进程停下，采集进程在检查点上认领（ADR-0009）。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_proc, guard, rounds, stop_request
from bestseller_monitor.db import Database, DayBoundaryReached, connect, utcnow
from bestseller_monitor.delay import Humanizer
from bestseller_monitor.rounds import TerminalReason
from frozen_clock import frozen_clock
from helpers import crawler_cfg, new_round


class StopRequestStoreTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(frozen_clock())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.rid = new_round(self.db, "A01")
        self.started_at = self.db.record_crawler_process(
            pid=os.getpid(), round_id=self.rid, note="run.py")
        self.addCleanup(stop_request.uninstall)

    def _request(self, pid: int | None = None, started_at: str | None = None) -> None:
        self.db.request_stop(
            round_id=self.rid,
            kind=stop_request.PAUSE,
            target_pid=os.getpid() if pid is None else pid,
            target_started_at=self.started_at if started_at is None else started_at,
        )

    def test_written_request_can_be_read_acknowledged_and_cleared(self):
        self.assertIsNone(self.db.stop_request(), "没写过就什么都没有")

        self._request()
        request = self.db.stop_request()
        self.assertEqual(request["target_pid"], os.getpid())
        self.assertEqual(request["target_started_at"], self.started_at)
        self.assertIsNone(request["ack_at"], "还没人回执")

        self.assertTrue(self.db.ack_stop_request(
            target_pid=os.getpid(), target_started_at=self.started_at))
        first_ack = self.db.stop_request()["ack_at"]
        self.assertIsNotNone(first_ack)
        self.assertFalse(self.db.ack_stop_request(
            target_pid=os.getpid(), target_started_at=self.started_at),
            "回执只写第一次，界面据此起算收尾窗口")
        self.assertEqual(self.db.stop_request()["ack_at"], first_ack)

        self.assertEqual(self.db.clear_stop_request(
            target_pid=os.getpid(), target_started_at=self.started_at), 1)
        self.assertIsNone(self.db.stop_request())

    def test_clear_leaves_a_request_for_another_process_alone(self):
        self._request(pid=os.getpid() + 1)

        self.assertEqual(self.db.clear_stop_request(
            target_pid=os.getpid(), target_started_at=self.started_at), 0)
        self.assertIsNotNone(self.db.stop_request(), "不是给我的请求不该被我删掉")

    def test_a_request_targets_only_the_process_it_names(self):
        self.assertFalse(stop_request.targets_me(self.db), "没有请求就没人被指着")

        self._request()
        self.assertTrue(stop_request.targets_me(self.db))

        self._request(started_at="2026-09-12T00:00:00+00:00")
        self.assertFalse(stop_request.targets_me(self.db), "启动时刻对不上：不是这一次运行")

        self._request(pid=os.getpid() + 1)
        self.assertFalse(stop_request.targets_me(self.db), "PID 对不上：不是这个进程")

        self._request()
        self.db.clear_crawler_process()
        self.assertFalse(stop_request.targets_me(self.db), "没有身份行就不认领")

    def test_ensure_workable_acknowledges_the_request_that_stops_the_round(self):
        self._request()

        with self.assertRaises(stop_request.StopRequested):
            rounds.ensure_workable(self.db, self.rid, utcnow())

        self.assertIsNotNone(self.db.stop_request()["ack_at"], "认领时回执，界面据此放宽窗口")

    def test_ensure_workable_ignores_a_request_for_another_process(self):
        self._request(pid=os.getpid() + 1)

        rounds.ensure_workable(self.db, self.rid, utcnow())

        self.assertIsNone(self.db.stop_request()["ack_at"], "不是给我的请求不许回执")

    def test_a_terminal_round_stops_before_the_request_is_claimed(self):
        """中止把轮次写进终态，终态本身就是停止信号：不许把它说成「暂停」（ADR-0009）。"""
        self._request()
        rounds.finish(self.db, rounds.load(self.db, self.rid), TerminalReason.ABANDONED)

        with self.assertRaises(DayBoundaryReached):
            rounds.ensure_workable(self.db, self.rid, utcnow())

        self.assertIsNone(self.db.stop_request()["ack_at"],
                          "轮次已经停了，这条请求不该被当成暂停认领")


class StopRequestHookTests(unittest.TestCase):
    """长睡眠上的检查：delay/guard 不必知道轮次，问的是装进来的那个钩子。"""

    def setUp(self):
        self.addCleanup(stop_request.uninstall)

    def test_check_does_nothing_without_a_hook(self):
        stop_request.check()      # 工具与单测路径：没有钩子就不该有任何副作用

    def test_check_calls_the_installed_hook_until_it_is_uninstalled(self):
        hook = MagicMock()
        stop_request.install(hook)

        stop_request.check()
        stop_request.uninstall()
        stop_request.check()

        hook.assert_called_once_with()

    def test_sleep_stops_within_one_slice_of_the_request(self):
        cfg = crawler_cfg(long_pause_interval=(12, 20), long_pause_sec=(10, 20))
        human = Humanizer(cfg)
        stop_request.install(lambda: (_ for _ in ()).throw(stop_request.StopRequested("停")))

        started = time.time()
        with self.assertRaises(stop_request.StopRequested):
            human.sleep(5)

        self.assertLess(time.time() - started, 1.0, "检查在每一片之前，长睡眠不该睡完再停")

    def test_sleep_wakes_up_between_slices_when_the_request_arrives_late(self):
        cfg = crawler_cfg(long_pause_interval=(12, 20), long_pause_sec=(10, 20))
        human = Humanizer(cfg)
        seen = []

        def hook():
            seen.append(time.time())
            if len(seen) >= 2:
                raise stop_request.StopRequested("停")

        stop_request.install(hook)
        started = time.time()
        with self.assertRaises(stop_request.StopRequested):
            human.sleep(30)

        self.assertLess(time.time() - started, stop_request.SLICE_SEC * 3,
                        "请求在睡眠途中到达时，最多等一片")

    def test_waiting_for_a_human_can_be_interrupted(self):
        page = MagicMock()
        stop_request.install(lambda: (_ for _ in ()).throw(stop_request.StopRequested("停")))

        with patch.object(guard, "resolved", return_value=False), \
                patch.object(guard.time, "sleep"), \
                patch.object(guard.sound, "play_alarm"):
            with self.assertRaises(stop_request.StopRequested):
                guard.wait_for_resolution(page, 10, confirm_sec=0.01)


class ProcessStopRuntimeTests(unittest.TestCase):
    """生产 adapter 的处置契约：只认绑定上的 facts（2026-09-18 审查候选 01）。

    这一组替代原来钉在 `gui.Api._close_bound_browser` 上的那条用例。处置搬进 adapter 之后，
    契约在 adapter 上钉一次就够了——在调用方再钉一次，就是在保住那份重复的实现。
    """

    def test_close_uses_the_binding_facts_and_ignores_caller_config(self):
        runtime = stop_request.ProcessStopRuntime(own_process=lambda: None)

        with patch.object(browser_proc, "close_browser", return_value=4242) as close:
            effect = runtime.close_browser(
                stop_request.BoundBrowser(4242, 9222, "browser-proof"))

        self.assertTrue(effect.ok)
        close.assert_called_once_with(9222, launched_by_us=True, browser_pid=4242,
                                      browser_os_started="browser-proof")

    def test_a_binding_without_a_recorded_port_is_still_attempted(self):
        """端口只用于日志，不拿它的有无把门（`.scratch/stop-target/spec.md` 的挂账）。"""
        runtime = stop_request.ProcessStopRuntime(own_process=lambda: None)

        with patch.object(browser_proc, "close_browser", return_value=4242) as close:
            runtime.close_browser(stop_request.BoundBrowser(4242, None, "browser-proof"))

        close.assert_called_once_with(None, launched_by_us=True, browser_pid=4242,
                                      browser_os_started="browser-proof")

    def test_a_binding_without_a_proof_is_handed_over_untouched(self):
        """没有 creation proof 的绑定照样交下去：拒绝是 `browser_proc` 的判断，不是这里的。

        这条是原来钉在 `gui.Api._close_bound_browser` 上的那条用例搬过来的——它验的是
        「不拿端口猜归属」，而「猜」这件事发生在 `browser_proc.close_browser` 里：那边缺
        pid 或 proof 一律拒绝。adapter 不该在这里替它提前判一次。
        """
        runtime = stop_request.ProcessStopRuntime(own_process=lambda: None)

        with patch.object(browser_proc, "close_browser", return_value=None) as close:
            effect = runtime.close_browser(stop_request.BoundBrowser(4242, 9222, None))

        close.assert_called_once_with(9222, launched_by_us=True, browser_pid=4242,
                                      browser_os_started=None)
        self.assertFalse(effect.ok, "对方拒绝关闭时如实上报，窗口据此进 cleanup_pending")

    def test_a_pid_taken_by_another_process_is_refused_and_the_handle_returned(self):
        """身份行记的创建证明与此刻同一 PID 上读到的不符：目标已被顶替，拒绝绑定。

        PID 会复用，所以「能开句柄」不等于「就是那个进程」（ADR-0024 冻结的是身份，
        不是 PID）。句柄当场还掉：不还就是漏一个句柄，还会让这个 PID 一直处在
        「能开句柄」的状态里。
        """
        target = stop_request.StopTarget(4242, "2026-09-23T00:00:00",
                                         process_os_started="crawler-proof")
        impostor = browser_proc.ProcessCapability(4242, 91, "somebody-elses-proof")

        with patch.object(browser_proc, "bind_process",
                          return_value=impostor) as bind, \
                patch.object(browser_proc, "release_process_capability") as release:
            with self.assertRaises(RuntimeError) as caught:
                stop_request.ProcessStopRuntime().bind(target)

        self.assertEqual(str(caught.exception), "process_identity_mismatch")
        bind.assert_called_once_with(4242)
        release.assert_called_once_with(impostor)

    def test_the_only_injected_input_is_the_own_child(self):
        """调用方交事实，不交动作、也不交否决权。"""
        with self.assertRaises(TypeError):
            stop_request.ProcessStopRuntime(own_process=None, browser_enabled=False)
        with self.assertRaises(TypeError):
            stop_request.ProcessStopRuntime(own_process=None,
                                            terminate=lambda bound: True)

    def test_a_failed_terminate_is_reported_not_swallowed(self):
        capability = MagicMock()
        capability.terminate.side_effect = OSError("拒绝访问")
        bound = stop_request.BoundTarget(
            stop_request.StopTarget(4242, "2026-09-19T00:00:00"), capability)

        effect = stop_request.ProcessStopRuntime().terminate(bound)

        self.assertFalse(effect.ok)
        self.assertEqual(effect.code, "terminate_failed")
        self.assertTrue(effect.retryable, "留在核验里重试，而不是报成功收口")

    def test_release_never_hands_a_process_double_to_ctypes(self):
        """子进程替身（MagicMock 什么属性都有，包括 handle）不得进 ctypes。

        崩过一次：判据曾是 hasattr(capability, "handle")，对 mock 恒真；ctypes 参数转换
        去摸 mock 属性时无限递归，Python 3.12/3.13 栈溢出杀掉整个测试进程。
        observe/terminate 都先认 poll，release 现在与它们对齐。
        """
        target = stop_request.StopTarget(4242, "2026-09-23T00:00:00")
        bound = stop_request.BoundTarget(target, MagicMock())

        with patch.object(browser_proc, "release_process_capability") as release:
            stop_request.ProcessStopRuntime().release(bound)

        release.assert_not_called()

    def test_release_hands_the_bound_capability_to_the_os(self):
        target = stop_request.StopTarget(4242, "2026-09-23T00:00:00")
        capability = browser_proc.ProcessCapability(4242, 91, "proof")
        bound = stop_request.BoundTarget(target, capability)

        with patch.object(browser_proc, "release_process_capability") as release:
            stop_request.ProcessStopRuntime().release(bound)

        release.assert_called_once_with(capability)

    def test_release_without_a_bound_handle_is_quiet(self):
        target = stop_request.StopTarget(4242, "2026-09-23T00:00:00")
        bound = stop_request.BoundTarget(target, None)

        with patch.object(browser_proc, "release_process_capability") as release:
            stop_request.ProcessStopRuntime().release(bound)

        release.assert_not_called()
