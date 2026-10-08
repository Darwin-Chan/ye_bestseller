"""Normal closing event integration: real Api/database, fake native/process boundaries."""
import tempfile
import sqlite3
import threading
from unittest.mock import patch, MagicMock
import unittest
from pathlib import Path
from types import SimpleNamespace

import gui
from bestseller_monitor import rounds, stop_request
from bestseller_monitor.db import Database, connect
from helpers import isolated_locks, new_round, insert_inventory_rows
from test_gui import gui_api, StopClock


class Window:
    def __init__(self):
        self.events = SimpleNamespace(closing=self)
        self.handlers = []
        self.destroyed = False
        self.title = ""

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def close(self):
        allowed = all(handler() is not False for handler in self.handlers)
        if allowed:
            self.destroyed = True
        return allowed

    def destroy(self):
        self.close()

    def set_title(self, title):
        self.title = title


class Process:
    pid = 4321

    def __init__(self):
        self.alive = True
        self.fail_terminate = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        if self.fail_terminate:
            raise OSError("denied")
        self.alive = False


class CrawlerCloseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(isolated_locks())
        self.path = Path(tmp.name) / "test.db"
        self.clock = StopClock()
        self.api = gui_api(db_path=self.path, stop_clock=self.clock)
        self.window = Window()
        self.process = Process()
        self.tasks = []
        self.messages = []

    def running(self):
        conn = connect(self.path)
        try:
            self.rid = new_round(Database(conn), "A01")
            Database(conn).record_crawler_process(
                pid=self.process.pid, round_id=self.rid,
                process_os_started="proof")
        finally:
            conn.close()
        self.api.proc = self.process
        self.api.round_id = self.rid

    def install(self, answer=False, wait=None):
        def confirm(text):
            self.messages.append(text)
            return answer(text) if callable(answer) else answer
        gui._install_close_guard(
            self.window, self.api, confirm=confirm,
            notify=self.messages.append, launch=self.tasks.append,
            wait=wait or (lambda seconds: setattr(self.process, "alive", False)))

    def terminal(self):
        conn = connect(self.path)
        try:
            return rounds.load(Database(conn), self.rid).reason
        finally:
            conn.close()

    def test_cancel_keeps_window_and_collection_running(self):
        self.running()
        self.install(answer=False)
        self.assertFalse(self.window.close())
        self.assertFalse(self.window.destroyed)
        self.assertTrue(self.process.alive)
        self.assertIsNone(self.terminal())
        self.assertEqual(self.tasks, [])
        self.assertIn("不可续跑", self.messages[0])

    def test_confirm_abandons_round_then_waits_before_closing(self):
        self.running()
        self.install(answer=True)
        self.assertFalse(self.window.close())
        self.assertFalse(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.assertTrue(self.process.alive)
        self.assertIn("正在中止", self.window.title)
        self.assertFalse(self.api.start_run([])["ok"])
        self.assertFalse(self.api.resume_run()["ok"])
        self.assertEqual(len(self.tasks), 1)
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertFalse(self.process.alive)

    def test_unattended_confirmation_never_automatically_accepts(self):
        with patch.dict(gui.os.environ, {"BESTSELLER_NO_DIALOG": "1"}):
            self.assertFalse(gui._confirm_crawler_close("test"))

    def test_native_dialog_defaults_to_cancel_and_only_accepts_ok(self):
        user32 = MagicMock()
        with patch.object(gui.os, "name", "nt"), patch.dict(gui.os.environ, {"BESTSELLER_NO_DIALOG": "0"}), \
                patch.object(gui.ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            for answer in [2, 0, 1]:
                user32.MessageBoxW.return_value = answer
                self.assertEqual(gui._confirm_crawler_close("test"), answer == 1)
            flags = user32.MessageBoxW.call_args.args[-1]
            self.assertTrue(flags & 0x100, "default native button must be Cancel")
            user32.MessageBoxW.side_effect = OSError("no dialog")
            self.assertFalse(gui._confirm_crawler_close("test"))

    def test_confirmation_exception_leaves_window_and_round_unchanged(self):
        self.running()
        def broken(text):
            raise OSError("dialog failed")
        self.install(answer=broken)
        self.assertFalse(self.window.close())
        self.assertTrue(self.process.alive)
        self.assertIsNone(self.terminal())
        self.assertEqual(self.tasks, [])
        self.assertFalse(self.window.destroyed)

    def test_repeated_close_does_not_stack_dialogs_or_stop_workers(self):
        self.running()
        def confirm(text):
            self.assertFalse(self.window.close(), "nested modal close must be swallowed")
            return True
        self.install(answer=confirm)
        self.window.close()
        self.window.close()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(len(self.tasks), 1)
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)

    def test_empty_window_closes_without_confirmation(self):
        self.install(answer=False)
        self.assertTrue(self.window.close())
        self.assertEqual(self.messages, [])
        self.assertEqual(self.tasks, [])

    def test_abandoned_terminal_and_committed_inventory_are_preserved(self):
        self.running()
        conn = connect(self.path)
        try:
            insert_inventory_rows(conn, [("A01", "offer1", "2026-10-08")])
            rounds.finish_if_open(Database(conn), rounds.load(Database(conn), self.rid),
                                  rounds.TerminalReason.ABANDONED)
        finally:
            conn.close()
        self.install(answer=True)
        self.window.close()
        self.assertEqual(self.messages, [], "already abandoned needs no new confirmation")
        self.tasks.pop()()
        conn = connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT stock FROM inventory").fetchone()[0], 3)
        finally:
            conn.close()
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.assertTrue(self.window.destroyed)

    def test_existing_abort_waits_without_another_confirmation(self):
        self.running()
        self.assertTrue(self.api.abort_run()["ok"])
        self.install(answer=False)
        self.window.close()
        self.assertEqual(self.messages, [])
        self.assertFalse(self.window.destroyed)
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)

    def test_pause_is_kept_open_until_ticket_02(self):
        self.running()
        self.api.pause_run()
        self.install(answer=True)
        self.assertFalse(self.window.close())
        self.assertIsNone(self.terminal())
        self.assertEqual(self.tasks, [])
        self.assertFalse(self.window.destroyed)

    def test_fully_paused_round_is_not_treated_as_empty(self):
        self.running()
        self.api.pause_run()
        self.process.alive = False
        self.api.get_start()
        self.install(answer=True)
        self.assertFalse(self.window.close())
        self.assertIsNone(self.terminal())
        self.assertFalse(self.window.destroyed)

    def test_identity_registration_gap_is_not_authorized_for_force_stop(self):
        self.api.proc = self.process
        self.install(answer=True)
        self.assertFalse(self.window.close())
        self.assertTrue(self.process.alive)
        self.assertEqual(self.tasks, [])
        self.assertFalse(self.window.destroyed)

    def test_target_replacement_during_confirmation_is_not_aborted(self):
        self.running()
        def change_target(text):
            conn = connect(self.path)
            try:
                Database(conn).record_crawler_process(pid=9999, round_id=self.rid,
                                                      process_os_started="other-proof")
            finally:
                conn.close()
            return True
        self.install(answer=change_target)
        self.assertFalse(self.window.close())
        self.assertIsNone(self.terminal())
        self.assertTrue(self.process.alive)
        self.assertEqual(self.tasks, [])
        self.assertFalse(self.window.destroyed)

    def test_abrupt_disappearance_without_terminal_does_not_complete_authorized_close(self):
        self.running()
        def disappear(text):
            self.process.alive = False
            return True
        self.install(answer=disappear)
        self.assertFalse(self.window.close())
        self.assertIsNone(self.terminal())
        self.assertFalse(self.window.destroyed)

    def test_terminal_write_failure_keeps_window_and_allows_retry(self):
        self.running()
        denied = True
        def open_conn():
            conn = connect(self.path)
            def authorize(action, table, column, dbname, source):
                if denied and action == sqlite3.SQLITE_UPDATE and table == "rounds":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            return conn
        self.api = gui_api(open_conn=open_conn, stop_clock=self.clock)
        self.api.proc, self.api.round_id = self.process, self.rid
        self.install(answer=True)
        self.assertFalse(self.window.close())
        self.assertIsNone(self.terminal())
        self.assertFalse(self.window.destroyed)
        self.assertEqual(self.tasks, [])
        denied = False
        self.window.close()
        self.tasks.pop()()
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.assertTrue(self.window.destroyed)

    def test_force_stop_failure_keeps_window_then_retries_same_watch(self):
        self.running()
        self.process.fail_terminate = True
        self.clock.advance(0)
        waits = 0
        def wait(seconds):
            nonlocal waits
            waits += 1
            self.clock.advance(20)
            if waits == 2:
                self.assertFalse(self.window.destroyed)
                self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
                self.assertIn("重试", self.window.title)
                self.assertIn("无法强制停止采集", self.window.title)
                self.assertNotIn("terminate_failed", self.window.title)
                self.process.fail_terminate = False
            if waits > 4:
                self.fail("same target should eventually stop")
        self.install(answer=True, wait=wait)
        self.window.close()
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)


    def test_existing_cleanup_without_gui_round_still_closes_original_round(self):
        self.running()
        runtime = stop_request.RecordingStopRuntime(
            alive=True, browser=stop_request.BoundBrowser(9201, 9222, "browser-proof"),
            browser_state="OWNED")
        runtime.fail_close = True
        self.api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        self.assertTrue(self.api.abort_run()["ok"])
        self.clock.advance(20)
        self.api.get_start()
        self.process.alive = False
        self.api.round_id = None  # another GUI did not start/remember this round
        self.install(answer=False, wait=lambda seconds: setattr(runtime, "fail_close", False))
        self.assertFalse(self.window.close())
        self.assertFalse(self.window.destroyed)
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)


    def test_naturally_completed_round_closes_without_rewriting_terminal(self):
        self.running()
        conn = connect(self.path)
        try:
            rounds.finish_if_open(Database(conn), rounds.load(Database(conn), self.rid),
                                  rounds.TerminalReason.COMPLETED)
        finally:
            conn.close()
        self.process.alive = False
        self.install(answer=False)
        self.assertTrue(self.window.close())
        self.assertEqual(self.messages, [])
        self.assertEqual(self.terminal(), rounds.TerminalReason.COMPLETED)

    def test_background_wait_keeps_close_handler_responsive(self):
        self.running()
        waiting, release, done = threading.Event(), threading.Event(), threading.Event()
        def wait(seconds):
            waiting.set()
            if not release.wait(5):
                raise TimeoutError("test release missing")
            self.process.alive = False
        def notify(message):
            self.messages.append(message)
            done.set()
        original_destroy = self.window.destroy
        def destroy():
            original_destroy()
            done.set()
        self.window.destroy = destroy
        gui._install_close_guard(self.window, self.api, confirm=lambda text: True,
                                 notify=notify, wait=wait)
        try:
            self.assertFalse(self.window.close())
            self.assertTrue(waiting.wait(5))
            self.assertFalse(self.window.destroyed)
            self.assertFalse(self.window.close(), "repeated close stays responsive")
            self.assertFalse(self.api.start_run(["A01"])["ok"])
        finally:
            release.set()
            self.assertTrue(done.wait(5), "worker must finish before temp DB cleanup")
        self.assertTrue(self.window.destroyed)

    def test_new_target_at_final_destroy_keeps_window_and_does_not_stop_it(self):
        self.running()
        self.install(answer=True)
        self.window.close()
        original_destroy = self.window.destroy
        def replaced_destroy():
            self.process = Process()
            self.process.pid = 9876
            self.api.proc = self.process
            conn = connect(self.path)
            try:
                self.rid = new_round(Database(conn), "A01")
                self.api.round_id = self.rid
                Database(conn).record_crawler_process(
                    pid=self.process.pid, round_id=self.rid, process_os_started="new-proof")
            finally:
                conn.close()
            original_destroy()
        self.window.destroy = replaced_destroy
        self.tasks.pop()()
        self.assertFalse(self.window.destroyed)
        self.assertTrue(self.process.alive)
        self.assertIsNone(self.terminal())
        self.assertIn("重新确认", self.messages[-1])


    def test_transient_database_read_failure_keeps_independent_teardown_running(self):
        self.running()
        unavailable = False
        def open_conn():
            nonlocal unavailable
            if unavailable:
                unavailable = False
                raise sqlite3.OperationalError("database temporarily unavailable")
            return connect(self.path)
        self.api = gui_api(open_conn=open_conn, stop_clock=self.clock)
        self.api.proc, self.api.round_id = self.process, self.rid
        self.install(answer=True)
        self.window.close()
        unavailable = True
        self.tasks.pop()()  # no page polling and no second close click
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)


    def test_retry_title_failure_does_not_stop_independent_teardown(self):
        self.running()
        self.process.fail_terminate = True
        original_set_title = self.window.set_title
        def set_title(title):
            if "正在重试" in title or "正在中止采集" in title:
                raise OSError("native title unavailable")
            original_set_title(title)
        self.window.set_title = set_title
        waits = 0
        def wait(seconds):
            nonlocal waits
            waits += 1
            self.clock.advance(20)
            if waits == 2:
                self.assertFalse(self.window.destroyed)
                self.process.fail_terminate = False
            if waits > 4:
                self.fail("title display must not end teardown")
        self.install(answer=True, wait=wait)
        self.window.close()
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
