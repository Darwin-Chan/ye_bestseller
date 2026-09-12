"""停止请求：界面请求正在跑的采集进程停下，采集进程在检查点上认领（ADR-0009）。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import guard, rounds, stop_request
from bestseller_monitor.db import Database, DayBoundaryReached, connect, utcnow
from bestseller_monitor.delay import Humanizer
from bestseller_monitor.rounds import TerminalReason
from helpers import new_round


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
        cfg = SimpleNamespace(long_pause_interval=(12, 20), long_pause_sec=(10, 20))
        human = Humanizer(cfg)
        stop_request.install(lambda: (_ for _ in ()).throw(stop_request.StopRequested("停")))

        started = time.time()
        with self.assertRaises(stop_request.StopRequested):
            human.sleep(5)

        self.assertLess(time.time() - started, 1.0, "检查在每一片之前，长睡眠不该睡完再停")

    def test_sleep_wakes_up_between_slices_when_the_request_arrives_late(self):
        cfg = SimpleNamespace(long_pause_interval=(12, 20), long_pause_sec=(10, 20))
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
