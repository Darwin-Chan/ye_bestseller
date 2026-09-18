"""停止请求：界面请求正在跑的采集进程停下，采集进程在检查点上认领（ADR-0009）。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_proc, crawler_identity, guard, rounds, stop_request
from bestseller_monitor.db import Database, DayBoundaryReached, connect, utcnow
from bestseller_monitor.delay import Humanizer
from bestseller_monitor.rounds import TerminalReason
from helpers import crawler_cfg, new_round


class StopRequestStoreTests(unittest.TestCase):
    def setUp(self):
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


class StopWatchTests(unittest.TestCase):
    """界面端的停止编排：窗口、回执、强杀与收尾（不碰进程、也不碰浏览器）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / "watch.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.rid = new_round(self.db, "A01")
        self.started_at = self.db.record_crawler_process(
            pid=os.getpid(), round_id=self.rid, note="run.py")
        self.now = 1000.0
        self.killed: list[str] = []
        self.running = True
        self.watch = stop_request.StopWatch(
            # 「强杀」真的生效：内核释放会话锁，进程随之不在。
            kill_child=self._kill_child,
            stop_foreign=lambda identity: self.killed.append(f"foreign:{identity.pid}"),
            close_browser=lambda: self.killed.append("browser"),
            is_running=lambda: self.running,
            identity_of=lambda conn: crawler_identity.registered(conn),
            now=lambda: self.now,
        )

    def _kill_child(self) -> None:
        self.killed.append("child")
        self.running = False

    def begin(self, kind: str = stop_request.PAUSE) -> None:
        self.watch.begin(kind, stop_request.StopTarget(pid=os.getpid(),
                                                       started_at=self.started_at))

    def test_nothing_happens_inside_the_window(self):
        self.begin()

        self.watch.tick(self.conn)

        self.assertEqual(self.killed, [])
        self.assertEqual(self.watch.state, "stopping")

    def test_the_window_deadline_forces_the_stop_and_cleans_up(self):
        self.begin()
        self.db.request_stop(round_id=self.rid, kind=stop_request.PAUSE,
                             target_pid=os.getpid(), target_started_at=self.started_at)

        self.now += 8.0
        self.watch.tick(self.conn)

        self.assertEqual(self.killed, ["child", "browser"])
        self.assertIsNone(self.watch.state)
        self.assertIsNone(self.db.stop_request(), "收尾之后不该留着停止请求")
        self.assertIsNone(self.db.crawler_process(), "身份行也要清掉")

    def test_an_acknowledgement_widens_the_window(self):
        self.begin()
        self.db.request_stop(round_id=self.rid, kind=stop_request.PAUSE,
                             target_pid=os.getpid(), target_started_at=self.started_at)
        self.db.ack_stop_request(target_pid=os.getpid(), target_started_at=self.started_at)

        self.now += 8.0                       # 原窗口到点
        self.watch.tick(self.conn)

        self.assertEqual(self.killed, [], "回执之后是在收尾，不是在等响应")
        self.assertEqual(self.watch.state, "closing")
        self.now += 10.0                      # 收尾窗口也到点
        self.watch.tick(self.conn)
        self.assertEqual(self.killed, ["child", "browser"])

    def test_a_crawler_that_already_stopped_is_just_cleaned_up(self):
        self.begin()
        self.db.request_stop(round_id=self.rid, kind=stop_request.PAUSE,
                             target_pid=os.getpid(), target_started_at=self.started_at)
        self.running = False

        self.watch.tick(self.conn)

        self.assertEqual(self.killed, [], "进程已经走了就不必再杀")
        self.assertIsNone(self.watch.state)
        self.assertIsNone(self.db.stop_request())

    def test_a_foreign_crawler_is_only_stopped_on_abort(self):
        self.db.clear_crawler_process()
        target_started_at = self.db.record_crawler_process(
            pid=4321, round_id=self.rid, note="别处起的")
        self.watch.begin(stop_request.ABORT,
                             stop_request.StopTarget(pid=4321, started_at=target_started_at))

        self.now += 8.0
        self.watch.tick(self.conn)

        self.assertEqual(self.killed, ["child", "foreign:4321", "browser"],
                         "中止要停的可能是别处起的采集")

    def test_a_kill_that_did_not_take_leaves_the_request_for_the_crawler(self):
        """强杀没落到实处：请求留着，采集进程在下一个检查点仍会自己停下。"""
        self.begin()
        self.db.request_stop(round_id=self.rid, kind=stop_request.PAUSE,
                             target_pid=os.getpid(), target_started_at=self.started_at)
        self.watch = stop_request.StopWatch(
            kill_child=lambda: self.killed.append("child"),   # 杀了但进程还在
            stop_foreign=lambda identity: None,
            close_browser=lambda: self.killed.append("browser"),
            is_running=lambda: True,
            identity_of=lambda conn: crawler_identity.registered(conn),
            now=lambda: self.now,
        )
        self.begin()

        self.now += 8.0
        self.watch.tick(self.conn)

        self.assertIsNotNone(self.db.stop_request(), "请求留着等它自己认领")
        self.assertIsNotNone(self.db.crawler_process(), "身份行也留着")
        self.assertIsNone(self.watch.state)

    def test_forget_drops_the_stop_in_flight(self):
        self.begin()

        self.watch.forget()

        self.assertIsNone(self.watch.state)


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
