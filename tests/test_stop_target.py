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


if __name__ == "__main__":
    unittest.main()
