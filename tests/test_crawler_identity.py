"""采集进程身份：现在是谁在跑、他算不算在跑（候选 05）。

两个来源合成一条判据——会话锁（「在不在跑」的权威）与库里那行身份（「是谁」）；
清残留也在这条判据里，所以不必构造一个 `gui.Api` 就能测它。
"""
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor import crawler_identity, single_instance, stop_request
from bestseller_monitor.crawler_identity import CrawlerProcess
from bestseller_monitor.db import Database, connect
from helpers import isolated_locks, new_round


class CrawlerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_locks())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = connect(Path(tmp.name) / "identity.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.round_id = new_round(self.db, "A01")

    def row(self):
        return self.conn.execute("SELECT * FROM crawler_process WHERE id=1").fetchone()

    # ---------- 在不在跑 ----------
    def test_the_session_lock_is_the_authority(self):
        holding = single_instance.acquire(single_instance.CRAWLER_LOCK)
        try:
            self.assertTrue(crawler_identity.is_running())
        finally:
            holding.release()
        self.assertFalse(crawler_identity.is_running())

    def test_the_interfaces_own_child_counts_before_it_holds_the_lock(self):
        """刚 Popen 到子进程抢到锁之间那一小段：界面自己的子进程说了算。"""
        self.assertFalse(crawler_identity.is_running())
        self.assertTrue(crawler_identity.is_running(own_alive=True))

    # ---------- 采集端：纯读那行身份 ----------
    def test_registered_reads_the_row_without_cleaning_it(self):
        self.db.record_crawler_process(pid=1234, round_id=self.round_id, note="run.py")

        who = crawler_identity.registered(self.conn)

        self.assertEqual((who.pid, who.round_id, who.note),
                         (1234, self.round_id, "run.py"))
        self.assertTrue(who.started_at, "停止请求按 (pid, 启动时刻) 认领")
        self.assertIsNotNone(self.row(), "registered 是纯读：锁不在也不许清")

    def test_registered_is_none_without_a_row(self):
        self.assertIsNone(crawler_identity.registered(self.conn))

    # ---------- 界面：谁在跑 ----------
    def test_current_reports_the_registered_process_while_the_lock_is_held(self):
        self.db.record_crawler_process(pid=4321, round_id=self.round_id, note="run.py")

        holding = single_instance.acquire(single_instance.CRAWLER_LOCK)
        try:
            who = crawler_identity.current(self.conn)
        finally:
            holding.release()

        self.assertEqual((who.pid, who.round_id, who.note), (4321, self.round_id, "run.py"))

    def test_current_clears_a_stale_row_and_reports_nobody(self):
        """被强杀的采集会留下身份行：锁不在就不该报「有任务在跑」，顺手清掉。"""
        self.db.record_crawler_process(pid=4321, round_id=self.round_id, note="run.py")

        self.assertIsNone(crawler_identity.current(self.conn))

        self.assertIsNone(self.row(), "残留身份行要清掉")

    def test_current_falls_back_to_the_interfaces_own_child(self):
        """锁刚拿到、身份行还没写：界面报得出自己那个 pid。"""
        holding = single_instance.acquire(single_instance.CRAWLER_LOCK)
        try:
            who = crawler_identity.current(self.conn, own_alive=True, own_pid=999,
                                           own_round_id=self.round_id)
        finally:
            holding.release()

        self.assertEqual((who.pid, who.round_id, who.started_at), (999, self.round_id, None))

    def test_current_reports_an_unknown_identity_when_someone_else_holds_the_lock(self):
        """别的会话起的采集：有人在跑，但界面拿不到它的 pid（开始页会说「身份未知」）。"""
        holding = single_instance.acquire(single_instance.CRAWLER_LOCK)
        try:
            who = crawler_identity.current(self.conn, own_alive=False, own_pid=999)
        finally:
            holding.release()

        self.assertIsNotNone(who, "有人在跑")
        self.assertIsNone(who.pid, "不是本界面的子进程，就没它的 pid")

    def test_current_reports_nobody_when_nothing_runs(self):
        self.assertIsNone(crawler_identity.current(self.conn, own_pid=999))

    # ---------- 界面的 payload ----------
    def test_the_payload_keeps_the_json_shape_the_page_reads(self):
        """`docs/ui_live.html` 读 `d.crawler.round_id`，所以跨 pywebview 那一步还是 dict。"""
        who = CrawlerProcess(pid=1, round_id=2, started_at="t")

        self.assertEqual(who.to_payload(),
                         {"pid": 1, "round_id": 2, "started_at": "t", "note": None})

    # ---------- 身份 → 停止目标 ----------
    def test_a_stop_target_needs_both_a_pid_and_a_started_at(self):
        """停止请求按 (PID, 启动时刻) 认人：占位身份（还没有启动时刻）成不了目标。"""
        row = self.db.record_crawler_process(pid=4321, round_id=self.round_id, note="run.py")
        placeholder = CrawlerProcess(pid=999, round_id=self.round_id)

        self.assertEqual(stop_request.StopTarget.of(crawler_identity.registered(self.conn)),
                         stop_request.StopTarget(pid=4321, started_at=row))
        self.assertIsNone(stop_request.StopTarget.of(placeholder))
        self.assertIsNone(stop_request.StopTarget.of(None))


if __name__ == "__main__":
    unittest.main()
