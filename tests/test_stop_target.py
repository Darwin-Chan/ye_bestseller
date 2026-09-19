import tempfile
import unittest
from pathlib import Path

from bestseller_monitor import stop_request
from bestseller_monitor.db import Database, connect


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


class StopTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / "stop.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.clock = Clock()
        self.a_started = self.db.record_crawler_process(
            pid=7001, round_id=1, browser_state="NOT_STARTED")
        self.a = stop_request.StopTarget(7001, self.a_started)

    def _watch(self, runtime):
        return stop_request.StopWatch(runtime, now=self.clock)

    def test_replacement_never_terminates_or_cleans_b(self):
        runtime = stop_request.RecordingStopRuntime(alive=True)
        watch = self._watch(runtime)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.PAUSE, self.a, 1))
        b_started = self.db.record_crawler_process(pid=7001, round_id=2)
        self.clock.value += 20
        status = watch.tick(self.conn)

        self.assertEqual(status.phase, stop_request.StopPhase.IDLE)
        self.assertEqual([name for name, _ in runtime.actions], ["bind", "release"])
        row = self.db.crawler_process()
        self.assertEqual((row["pid"], row["started_at"]), (7001, b_started))

    def test_ack_window_is_still_target_bound(self):
        runtime = stop_request.RecordingStopRuntime(alive=True)
        watch = self._watch(runtime)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.PAUSE, self.a, 1))
        self.db.ack_stop_request(target_pid=self.a.pid,
                                 target_started_at=self.a.started_at)
        self.clock.value += 8
        self.assertEqual(watch.tick(self.conn).phase, stop_request.StopPhase.CLOSING)
        self.db.record_crawler_process(pid=9001, round_id=2)
        self.clock.value += 10
        self.assertEqual(watch.tick(self.conn).phase, stop_request.StopPhase.IDLE)
        self.assertEqual([name for name, _ in runtime.actions], ["bind", "release"])

    def test_same_pid_new_start_is_replaced(self):
        runtime = stop_request.RecordingStopRuntime(alive=True)
        watch = self._watch(runtime)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.ABORT, self.a, 1))
        self.db.record_crawler_process(pid=self.a.pid, round_id=2)
        self.clock.value += 20
        watch.tick(self.conn)
        self.assertNotIn("terminate", [name for name, _ in runtime.actions])

    def test_gone_target_is_conditionally_cleaned(self):
        runtime = stop_request.RecordingStopRuntime(alive=False)
        watch = self._watch(runtime)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.PAUSE, self.a, 1))
        self.assertEqual(watch.tick(self.conn).phase, stop_request.StopPhase.IDLE)
        self.assertIsNone(self.db.crawler_process())

    def test_probe_failure_stays_verifying_after_deadline(self):
        runtime = stop_request.RecordingStopRuntime(alive=None)
        watch = self._watch(runtime)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.ABORT, self.a, 1))
        self.clock.value += 20
        status = watch.tick(self.conn)
        self.assertEqual(status.phase, stop_request.StopPhase.VERIFYING)
        self.assertIsNotNone(self.db.crawler_process())
        self.assertNotIn("terminate", [name for name, _ in runtime.actions])

    def test_a_failed_force_leaves_the_target_verifying(self):
        """强杀没落到实处：窗口留在核验里，事实不清、浏览器不关（候选 01 验收 2）。

        「杀失败」与「杀完还活着」在 adapter 那层是两件事（`terminate_failed` /
        `process_still_running`），但在窗口这层收口相同：都没证明停下，都不许清事实。
        """
        runtime = stop_request.RecordingStopRuntime(
            alive=True, browser=stop_request.BoundBrowser(9201, 9222, "browser-proof"),
            browser_state="OWNED")
        runtime.fail_terminate = True
        watch = self._watch(runtime)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.ABORT, self.a, 1))
        self.clock.value += 20

        status = watch.tick(self.conn)

        self.assertEqual(status.phase, stop_request.StopPhase.VERIFYING)
        self.assertEqual(status.code, "terminate_failed")
        self.assertIsNotNone(self.db.crawler_process(), "没证明停下就不该清事实")
        self.assertNotIn("close_browser", [name for name, _ in runtime.actions])

    def test_browser_binding_failure_is_verifying_not_port_guess(self):
        runtime = stop_request.RecordingStopRuntime(alive=True,
                                                     browser_state="STARTING")
        watch = self._watch(runtime)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.ABORT, self.a, 1))
        self.clock.value += 20
        status = watch.tick(self.conn)
        self.assertEqual(status.code, "browser_binding_unavailable")
        self.assertNotIn("terminate", [name for name, _ in runtime.actions])

    def test_nothing_happens_inside_the_window(self):
        runtime = stop_request.RecordingStopRuntime(alive=True)
        watch = self._watch(runtime)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.PAUSE, self.a, 1))

        self.clock.value += 4        # 窗口是 8 秒
        status = watch.tick(self.conn)

        self.assertEqual(status.phase, stop_request.StopPhase.STOPPING)
        self.assertEqual([name for name, _ in runtime.actions], ["bind"],
                         "窗口里只观察，不动手")

    def test_the_deadline_forces_the_stop_and_clears_every_trace(self):
        runtime = stop_request.RecordingStopRuntime(
            alive=True, browser=stop_request.BoundBrowser(9201, 9222, "browser-proof"),
            browser_state="OWNED")
        watch = self._watch(runtime)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.PAUSE, self.a, 1))
        self.assertIsNotNone(self.db.stop_request(), "开窗时已经写下请求（CAS 到目标身份）")

        self.clock.value += 20
        status = watch.tick(self.conn)

        self.assertEqual(status.phase, stop_request.StopPhase.IDLE)
        self.assertEqual([name for name, _ in runtime.actions],
                         ["bind", "terminate", "close_browser", "release"])
        self.assertIsNone(self.db.stop_request(), "收尾之后请求不留")
        self.assertIsNone(self.db.crawler_process(), "身份行也要清掉")

    def test_an_abort_stops_the_registered_crawler_even_when_it_is_not_ours(self):
        """目标可能是别处起的采集：登记行是谁，目标就是谁（本界面只负责编排）。"""
        self.db.clear_crawler_process()
        started_at = self.db.record_crawler_process(pid=4321, round_id=1)
        target = stop_request.StopTarget(4321, started_at)
        runtime = stop_request.RecordingStopRuntime(alive=True)
        watch = self._watch(runtime)

        watch.begin(self.conn, stop_request.StopCommand(stop_request.ABORT, target, 1))
        self.clock.value += 20
        status = watch.tick(self.conn)

        self.assertEqual(status.phase, stop_request.StopPhase.IDLE)
        self.assertIn(("terminate", target), runtime.actions)

    def test_a_kill_that_did_not_take_stays_verifying(self):
        """杀了但进程还在：留在核验里重试，事实不清、浏览器不关。

        只有这一套语义了——旧 runtime 那份「静默丢弃 watch」随 `_LegacyRuntime` 一起删
        （ADR-0024 的删除清单）。
        """
        runtime = stop_request.RecordingStopRuntime(alive=True, survives_terminate=True)
        watch = self._watch(runtime)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.PAUSE, self.a, 1))

        self.clock.value += 20
        status = watch.tick(self.conn)

        self.assertEqual(status.phase, stop_request.StopPhase.VERIFYING)
        self.assertEqual(status.code, "process_still_running")
        self.assertIsNotNone(self.db.crawler_process(), "没证明停下就不清事实")
        self.assertIsNotNone(self.db.stop_request(), "请求留着，等它在检查点自己认领")
        self.assertNotIn("close_browser", [name for name, _ in runtime.actions])

    def test_begin_requires_a_connection(self):
        """开窗之前必须确认目标仍是登记的那个身份：没有连接就没有这道确认。"""
        watch = self._watch(stop_request.RecordingStopRuntime(alive=True))
        command = stop_request.StopCommand(stop_request.PAUSE, self.a, 1)

        with self.assertRaises(TypeError):
            watch.begin(None, command)
        with self.assertRaises(TypeError):
            watch.begin(self.conn)

        self.assertIsNone(watch.state, "两次都不该开窗")

    def test_the_five_callback_shape_is_gone(self):
        """旧版五回调构造不再存在：StopWatch 只接受一个 runtime（ADR-0024 的删除清单）。"""
        with self.assertRaises(TypeError):
            stop_request.StopWatch(
                kill_child=lambda: None, stop_foreign=lambda who: None,
                close_browser=lambda: None, is_running=lambda: True,
                identity_of=lambda conn: None)


class BrowserCleanupRetryTests(unittest.TestCase):
    """收尾失败声明的重试有落点（2026-09-19 审查候选 02）。

    `CLEANUP_PENDING` 是 `_force` 关浏览器失败时留下的相，而那时进程已经被证明退出 —— 所以
    下一次 `tick` 必然判出 `GONE`，偏偏 `GONE` 的短路在 deadline 之前。改前不在这里重试：
    `close_browser` 每个窗口只会被调一次，一次瞬时失败就留下一个真在跑的孤儿浏览器，
    而条件清理还会把它唯一还能被定位的那份绑定一起抹掉。
    """

    BROWSER = stop_request.BoundBrowser(9201, 9222, "browser-proof")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / "cleanup.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.clock = Clock()
        self.a_started = self.db.record_crawler_process(
            pid=7001, round_id=1, browser_state="NOT_STARTED")
        self.a = stop_request.StopTarget(7001, self.a_started)

    def _watch_at_the_deadline(self, *, fail_close: bool = True):
        """一个已经过了 deadline、目标绑着浏览器的窗口——推一次就进清理那条路。"""
        runtime = stop_request.RecordingStopRuntime(
            alive=True, browser=self.BROWSER, browser_state="OWNED")
        runtime.fail_close = fail_close
        watch = stop_request.StopWatch(runtime, now=self.clock)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.ABORT, self.a, 1))
        self.clock.value += 20
        return watch, runtime

    @staticmethod
    def _closes(runtime) -> list:
        return [name for name, _ in runtime.actions if name == "close_browser"]

    def test_a_failed_browser_close_is_retried_until_it_lands(self):
        watch, runtime = self._watch_at_the_deadline()

        first = watch.tick(self.conn)

        self.assertEqual(first.phase, stop_request.StopPhase.CLEANUP_PENDING)
        self.assertEqual(first.code, "browser_close_failed")
        self.assertEqual(len(self._closes(runtime)), 1)
        self.assertIsNotNone(self.db.crawler_process(), "还没关掉就不该清事实")

        runtime.fail_close = False
        second = watch.tick(self.conn)

        self.assertEqual(second.phase, stop_request.StopPhase.IDLE)
        self.assertEqual(len(self._closes(runtime)), 2, "关失败的那一次要重试，不是就此收工")
        self.assertIsNone(self.db.crawler_process(), "重试成功之后才清事实")

    def test_a_binding_that_is_no_longer_exact_is_left_alone(self):
        watch, runtime = self._watch_at_the_deadline()
        watch.tick(self.conn)

        runtime.browser = stop_request.BoundBrowser(9999, 9222, "another-proof")
        status = watch.tick(self.conn)

        self.assertEqual(status.phase, stop_request.StopPhase.CLEANUP_PENDING)
        self.assertEqual(status.code, "browser_binding_changed")
        self.assertEqual(len(self._closes(runtime)), 1, "认不出的浏览器不动手")
        self.assertIsNotNone(self.db.crawler_process(), "没收口就不该清事实")

    def test_a_replaced_target_ends_the_old_retry_and_leaves_b_alone(self):
        watch, runtime = self._watch_at_the_deadline()
        watch.tick(self.conn)

        b_started = self.db.record_crawler_process(pid=9201, round_id=2)
        status = watch.tick(self.conn)

        self.assertEqual(status.phase, stop_request.StopPhase.IDLE)
        self.assertEqual(len(self._closes(runtime)), 1, "B 接手之后不再重试")
        row = self.db.crawler_process()
        self.assertEqual((row["pid"], row["started_at"]), (9201, b_started))

    def test_the_database_failure_path_still_retries_through_force(self):
        """另一条进 `CLEANUP_PENDING` 的路（库不可用）不带 pending browser，照旧走 `_force`。"""
        runtime = stop_request.RecordingStopRuntime(alive=True)
        attempts = []

        def terminate(bound):
            attempts.append(bound)
            if len(attempts) == 1:
                raise RuntimeError("库忙")
            runtime.alive = False
            return stop_request.EffectResult(True)

        runtime.terminate = terminate
        watch = stop_request.StopWatch(runtime, now=self.clock)
        watch.begin(self.conn, stop_request.StopCommand(stop_request.ABORT, self.a, 1))
        self.clock.value += 20

        first = watch.tick(self.conn)
        self.assertEqual(first.phase, stop_request.StopPhase.CLEANUP_PENDING)
        self.assertEqual(first.code, "database_unavailable")

        self.assertEqual(watch.tick(self.conn).phase, stop_request.StopPhase.IDLE)
        self.assertEqual(len(attempts), 2, "那条路的重试今天是活的，不许被改掉")


if __name__ == "__main__":
    unittest.main()
