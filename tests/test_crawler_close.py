"""Normal closing event integration: real Api/database, fake native/process boundaries."""
import tempfile
import sqlite3
import threading
from unittest.mock import patch, MagicMock
import unittest
from pathlib import Path
from types import SimpleNamespace

import gui
from bestseller_monitor import rounds, stop_request, single_instance
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

    def test_paused_replacement_without_original_cleanup_proof_stays_blocked(self):
        self.running()
        runtime = stop_request.RecordingStopRuntime(alive=True)
        self.api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        self.assertTrue(self.api.pause_run()["ok"])
        confirmations = 0
        def confirm(text):
            nonlocal confirmations
            confirmations += 1
            if confirmations == 1:
                conn = connect(self.path)
                try:
                    Database(conn).record_crawler_process(
                        pid=9999, round_id=self.rid, process_os_started="replacement-proof")
                finally:
                    conn.close()
            return True
        def wait(seconds):
            runtime.alive = False
            self.process.alive = False
        self.install(answer=confirm, wait=wait)
        self.assertFalse(self.window.close())
        self.assertIsNone(self.terminal(), "old authorization cannot abandon the replacement")
        while self.tasks:
            self.tasks.pop()()
        self.assertFalse(self.window.destroyed)
        self.assertFalse(self.window.close())
        self.assertEqual(confirmations, 1, "unknown original cleanup cannot be discarded for a new target")
        self.assertFalse(self.window.destroyed)
        self.assertIsNone(self.terminal())
        conn = connect(self.path)
        try:
            self.assertEqual(Database(conn).crawler_process()["pid"], 9999)
        finally:
            conn.close()
        self.assertNotIn("terminate", [action for action, _ in runtime.actions])
        self.assertNotIn("close_browser", [action for action, _ in runtime.actions])

    def test_changed_os_start_proof_during_wait_preserves_replacement_and_browser(self):
        self.running()
        browser = stop_request.BoundBrowser(9500, 9222, "new-browser-proof")
        runtime = stop_request.RecordingStopRuntime(
            alive=True, browser=browser, browser_state="OWNED")
        self.api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        waits = 0
        def wait(seconds):
            nonlocal waits
            waits += 1
            if waits > 1:
                raise RuntimeError("test watchdog: replacement was not recognized")
            conn = connect(self.path)
            try:
                # Keep PID and registration timestamp: OS creation proof alone
                # is sufficient to reject authorization for a different process.
                conn.execute("UPDATE crawler_process SET process_os_started=? WHERE id=1",
                             ("replacement-os-proof",))
                conn.commit()
            finally:
                conn.close()
            self.clock.advance(20)
        self.install(answer=True, wait=wait)
        self.window.close()
        self.tasks.pop()()
        self.assertFalse(self.window.destroyed)
        self.assertTrue(runtime.alive)
        self.assertIn("重新确认", self.messages[-1])
        self.assertFalse(any(action in ("terminate", "close_browser")
                             for action, target in runtime.actions))
        conn = connect(self.path)
        try:
            self.assertEqual(Database(conn).crawler_process()["process_os_started"],
                             "replacement-os-proof")
        finally:
            conn.close()

    def external_running(self, entry):
        conn = connect(self.path)
        try:
            old = new_round(Database(conn), "OLD", run_date="2026-10-07")
        finally:
            conn.close()
        self.running()
        self.api.proc = None
        self.api.round_id = old if entry == "previous_gui" else None
        lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.assertIsNotNone(lock)
        self.addCleanup(lock.release)
        runtime = stop_request.RecordingStopRuntime(alive=True, browser_state="BORROWED")
        self.api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        return runtime, lock, old

    def test_cli_target_without_gui_memory_is_aborted_and_borrowed_browser_preserved(self):
        runtime, lock, old = self.external_running("cli")
        def wait(seconds):
            self.clock.advance(20)
            if not runtime.alive:
                lock.release()
        self.install(answer=True, wait=wait)
        self.assertFalse(self.window.close())
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertFalse(any(action == "close_browser" for action, target in runtime.actions))

    def test_prior_gui_actual_round_wins_over_stale_gui_round(self):
        runtime, lock, old = self.external_running("previous_gui")
        def wait(seconds):
            runtime.alive = False
            lock.release()
        self.install(answer=True, wait=wait)
        self.window.close()
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        conn = connect(self.path)
        try:
            self.assertEqual(rounds.load(Database(conn), old).reason, rounds.TerminalReason.DAY_BOUNDARY)
        finally:
            conn.close()

    def test_natural_completion_during_confirmation_preserves_completed_terminal(self):
        self.running()
        def confirm(text):
            conn = connect(self.path)
            try:
                rounds.finish_if_open(Database(conn), rounds.load(Database(conn), self.rid),
                                      rounds.TerminalReason.COMPLETED)
            finally:
                conn.close()
            self.process.alive = False
            return True
        self.install(answer=confirm)
        self.window.close()
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.COMPLETED)

    def test_natural_completion_during_confirmation_waits_for_original_live_cleanup(self):
        self.running()
        def confirm(text):
            conn = connect(self.path)
            try:
                rounds.finish_if_open(Database(conn), rounds.load(Database(conn), self.rid),
                                      rounds.TerminalReason.COMPLETED)
            finally:
                conn.close()
            return True
        self.install(answer=confirm)
        self.window.close()
        self.assertFalse(self.window.destroyed)
        self.assertTrue(self.process.alive)
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.COMPLETED)

    def test_pid_reuse_during_confirmation_requires_fresh_confirmation(self):
        self.running()
        def confirm(text):
            conn = connect(self.path)
            try:
                Database(conn).record_crawler_process(
                    pid=self.process.pid, round_id=self.rid,
                    process_os_started="reused-pid-proof")
            finally:
                conn.close()
            return True
        self.install(answer=confirm)
        self.assertFalse(self.window.close())
        self.assertFalse(self.window.destroyed)
        self.assertIsNone(self.terminal())
        self.assertIn("重新确认", self.messages[-1])
        self.assertEqual(self.tasks, [])

    def test_replacement_during_wait_requires_fresh_confirmation_and_keeps_new_browser(self):
        self.running()
        runtime = stop_request.RecordingStopRuntime(
            alive=True, browser=stop_request.BoundBrowser(9501, 9222, "new-browser"),
            browser_state="OWNED")
        self.api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        def wait(seconds):
            conn = connect(self.path)
            try:
                self.new_rid = new_round(Database(conn), "B02")
                Database(conn).record_crawler_process(
                    pid=9999, round_id=self.new_rid, process_os_started="new-proof")
            finally:
                conn.close()
            self.clock.advance(20)
        self.install(answer=True, wait=wait)
        self.window.close()
        self.tasks.pop()()
        self.assertFalse(self.window.destroyed)
        self.assertIn("重新确认", self.messages[-1])
        self.assertTrue(runtime.alive)
        self.assertFalse(any(action in ("terminate", "close_browser")
                             for action, target in runtime.actions))
        conn = connect(self.path)
        try:
            self.assertIsNone(rounds.load(Database(conn), self.new_rid).reason)
            self.assertEqual(Database(conn).crawler_process()["pid"], 9999)
        finally:
            conn.close()
        self.assertFalse(self.api.start_run([])["ok"])
        self.assertFalse(self.api.resume_run()["ok"])

    def test_unverifiable_process_wait_is_visible_and_retries_original_binding(self):
        self.running()
        runtime = stop_request.RecordingStopRuntime(alive=None)
        self.api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        waits = 0
        def wait(seconds):
            nonlocal waits
            waits += 1
            self.assertFalse(self.window.destroyed)
            self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
            self.assertIn("重试", self.window.title)
            self.assertFalse(any(action == "terminate" for action, target in runtime.actions))
            runtime.alive = False
            self.process.alive = False
            if waits > 1:
                self.fail("original binding must finish after verification recovers")
        self.install(answer=True, wait=wait)
        self.window.close()
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)

    def test_paused_confirmed_wait_replacement_keeps_new_round_open(self):
        self.running()
        runtime = stop_request.RecordingStopRuntime(alive=True)
        self.api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        self.api.pause_run()
        def wait(seconds):
            conn = connect(self.path)
            try:
                self.new_rid = new_round(Database(conn), "B02")
                Database(conn).record_crawler_process(
                    pid=self.process.pid, round_id=self.new_rid,
                    process_os_started="reused-pid-proof")
            finally:
                conn.close()
            self.clock.advance(20)
        self.install(answer=True, wait=wait)
        self.window.close()
        self.tasks.pop()()
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.assertFalse(self.window.destroyed)
        self.assertIn("重新确认", self.messages[-1])
        self.assertTrue(runtime.alive)
        conn = connect(self.path)
        try:
            self.assertIsNone(rounds.load(Database(conn), self.new_rid).reason)
        finally:
            conn.close()

    def test_cross_entry_owned_browser_cleanup_remains_pending_until_exact_browser_finishes(self):
        runtime, lock, old = self.external_running("cli")
        browser = stop_request.BoundBrowser(9501, 9222, "owned-proof")
        runtime.browser, runtime.browser_state = browser, "OWNED"
        runtime.fail_close = True
        waits = 0
        def wait(seconds):
            nonlocal waits
            waits += 1
            if waits == 1:
                self.clock.advance(20)
            elif waits == 2:
                self.assertFalse(self.window.destroyed)
                self.assertEqual(runtime.browser, browser)
                self.assertIn("浏览器收尾", self.window.title)
                lock.release()
                runtime.fail_close = False
            else:
                self.fail("must finish original bounded cleanup")
        self.install(answer=True, wait=wait)
        self.window.close()
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.assertIsNone(runtime.browser)

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

    def test_pause_cancel_continues_original_stop_without_page_polling(self):
        self.running()
        self.api.pause_run()
        self.install(answer=False)
        self.assertFalse(self.window.close())
        self.assertIn("已收到用户暂停请求，采集进程正在停止", self.messages[0])
        self.assertIsNone(self.terminal())
        self.assertFalse(self.window.destroyed)
        self.assertEqual(len(self.tasks), 1, "independent driver must continue original pause")
        self.tasks.pop()()
        self.assertFalse(self.process.alive)
        self.assertFalse(self.window.destroyed)
        self.assertIsNone(self.terminal())
        self.assertTrue(self.api.get_run()["manually_paused"])

    def test_fully_paused_with_lost_gui_memory_uses_confirmed_pause_association(self):
        self.running()
        self.api.pause_run()
        self.process.alive = False
        self.api.get_start()
        self.api.round_id = None  # abort_run historically returns no-op success here
        self.install(answer=True)
        self.assertFalse(self.window.close())
        self.assertIn("本轮已由用户主动暂停", self.messages[0])
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)

    def test_pause_can_finish_during_confirmation_without_losing_round_authorization(self):
        self.running()
        self.api.pause_run()
        def confirm(text):
            self.process.alive = False
            self.api.get_start()
            return True
        self.install(answer=confirm)
        self.assertFalse(self.window.close())
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)

    def paused_cleanup(self):
        self.running()
        runtime = stop_request.RecordingStopRuntime(
            alive=True, browser=stop_request.BoundBrowser(9201, 9222, "browser-proof"),
            browser_state="OWNED")
        runtime.fail_close = True
        self.api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        self.assertTrue(self.api.pause_run()["ok"])
        self.clock.advance(20)
        self.api.get_start()
        self.process.alive = False
        return runtime

    def test_pause_cleanup_confirm_keeps_exact_browser_and_original_deadline(self):
        runtime = self.paused_cleanup()
        self.api.round_id = None  # the original command is authoritative
        before = list(runtime.actions)
        def wait(seconds):
            self.assertFalse(self.window.destroyed)
            self.clock.advance(9)
            runtime.fail_close = False
        self.install(answer=True, wait=wait)
        self.assertFalse(self.window.close())
        self.assertIn("用户暂停请求正在收尾", self.messages[0])
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(sum(a[0] == "bind" for a in runtime.actions), 1)
        self.assertEqual(sum(a[0] == "terminate" for a in runtime.actions), 1)
        browsers = [a[1] for a in runtime.actions if a[0] == "close_browser"]
        self.assertTrue(browsers)
        self.assertTrue(all(b == stop_request.BoundBrowser(9201, 9222, "browser-proof")
                            for b in browsers))
        self.assertEqual(before[0], runtime.actions[0])

    def check_pause_declined(self, stage, *, dialog_failure=False):
        runtime = None
        if stage == "cleanup":
            runtime = self.paused_cleanup()
        else:
            self.running()
            self.api.pause_run()
            if stage == "paused":
                self.process.alive = False
                self.api.get_start()
        conn = connect(self.path)
        try:
            insert_inventory_rows(conn, [("A01", "offer1", "2026-10-08")])
        finally:
            conn.close()
        def broken(text):
            raise OSError("dialog failed")
        self.install(answer=broken if dialog_failure else False,
                     wait=(lambda seconds: setattr(runtime, "fail_close", False))
                     if runtime else None)
        self.assertFalse(self.window.close())
        headings = {"stopping": "已收到用户暂停请求，采集进程正在停止",
                    "cleanup": "用户暂停请求正在收尾", "paused": "本轮已由用户主动暂停"}
        self.assertIn(headings[stage], self.messages[0])
        self.assertIn("不可续跑", self.messages[0])
        self.assertFalse(self.window.destroyed)
        self.assertIsNone(self.terminal())
        self.assertEqual(len(self.tasks), 0 if stage == "paused" else 1)
        while self.tasks:
            self.tasks.pop()()
        self.assertIsNone(self.terminal())
        self.assertFalse(self.window.destroyed)
        self.assertTrue(self.api.get_run()["manually_paused"])
        self.assertFalse(self.process.alive)
        conn = connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT stock FROM inventory").fetchone()[0], 3)
            self.assertIsNone(Database(conn).stop_request())
        finally:
            conn.close()
        if runtime:
            self.assertEqual(sum(a[0] == "bind" for a in runtime.actions), 1)
            self.assertEqual(sum(a[0] == "terminate" for a in runtime.actions), 1)

    def test_pause_stopping_dialog_failure_continues_original_pause(self):
        self.check_pause_declined("stopping", dialog_failure=True)

    def test_pause_cleanup_cancel_continues_original_cleanup(self):
        self.check_pause_declined("cleanup")

    def test_pause_cleanup_dialog_failure_continues_original_cleanup(self):
        self.check_pause_declined("cleanup", dialog_failure=True)

    def test_fully_paused_cancel_keeps_round_and_data(self):
        self.check_pause_declined("paused")

    def test_fully_paused_dialog_failure_keeps_round_and_data(self):
        self.check_pause_declined("paused", dialog_failure=True)

    def test_unknown_pause_cannot_abandon_arbitrary_unfinished_round(self):
        self.running()
        self.process.alive = False
        # A pause without any owned live crawler cannot establish association.
        self.api.round_id = None
        self.assertTrue(self.api.pause_run()["ok"])
        self.install(answer=True)
        self.assertFalse(self.window.close())
        self.assertIsNone(self.terminal())
        self.assertFalse(self.window.destroyed)
        self.assertEqual(self.tasks, [])
        self.assertIn("无法确认", self.messages[0])

    def test_pause_without_live_owned_execution_does_not_authorize_gui_remembered_round(self):
        self.running()
        self.process.alive = False
        self.assertTrue(self.api.pause_run()["ok"])
        self.install(answer=True)
        self.assertFalse(self.window.close())
        self.assertFalse(self.window.destroyed)
        self.assertIsNone(self.terminal())
        self.assertEqual(self.tasks, [])

    def test_fully_paused_terminal_write_failure_keeps_pause_and_allows_retry(self):
        self.running()
        denied = True
        def open_conn():
            conn = connect(self.path)
            def authorize(action, table, column, dbname, source):
                return (sqlite3.SQLITE_DENY if denied and action == sqlite3.SQLITE_UPDATE
                        and table == "rounds" else sqlite3.SQLITE_OK)
            conn.set_authorizer(authorize)
            return conn
        self.api = gui_api(open_conn=open_conn, stop_clock=self.clock)
        self.api.proc, self.api.round_id = self.process, self.rid
        self.api.pause_run()
        self.process.alive = False
        self.api.get_start()
        self.install(answer=True)
        self.assertFalse(self.window.close())
        self.assertIsNone(self.terminal())
        self.assertFalse(self.window.destroyed)
        self.assertEqual(self.tasks, [])
        self.assertTrue(self.api.get_run()["manually_paused"])
        self.assertIn("重试", self.messages[-1])
        denied = False
        self.window.close()
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)

    def test_paused_terminal_changes_during_confirmation_are_not_rewritten(self):
        self.running()
        self.api.pause_run()
        self.process.alive = False
        self.api.get_start()
        def confirm(text):
            conn = connect(self.path)
            try:
                rounds.finish_if_open(Database(conn), rounds.load(Database(conn), self.rid),
                                      rounds.TerminalReason.COMPLETED)
            finally:
                conn.close()
            return True
        self.install(answer=confirm)
        self.assertFalse(self.window.close())
        self.assertFalse(self.window.destroyed)
        self.assertEqual(self.tasks, [])
        self.assertEqual(self.terminal(), rounds.TerminalReason.COMPLETED)
        self.assertIn("重新确认", self.messages[-1])

    def test_pause_confirm_keeps_committed_inventory(self):
        self.running()
        self.api.pause_run()
        conn = connect(self.path)
        try:
            insert_inventory_rows(conn, [("A01", "offer1", "2026-10-08")])
        finally:
            conn.close()
        self.install(answer=True)
        self.window.close()
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        conn = connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT stock FROM inventory").fetchone()[0], 3)
        finally:
            conn.close()

    def test_pause_upgrade_force_failure_never_rolls_back_to_resumable(self):
        self.running()
        self.api.pause_run()
        self.process.fail_terminate = True
        waits = 0
        def wait(seconds):
            nonlocal waits
            waits += 1
            self.clock.advance(20)
            self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
            self.assertFalse(self.api.get_run()["manually_paused"])
            self.assertFalse(self.api.resume_run()["ok"])
            if waits == 2:
                self.assertFalse(self.window.destroyed)
                self.process.fail_terminate = False
            if waits > 3:
                self.fail("must retry original stop")
        self.install(answer=True, wait=wait)
        self.window.close()
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)

    def test_paused_target_replacement_during_confirmation_is_not_abandoned(self):
        self.running()
        self.api.pause_run()
        def confirm(text):
            conn = connect(self.path)
            try:
                self.new_rid = self.rid  # a replacement may share the same round
                Database(conn).record_crawler_process(pid=9999, round_id=self.new_rid,
                                                      process_os_started="other-proof")
            finally:
                conn.close()
            return True
        self.install(answer=confirm)
        self.assertFalse(self.window.close())
        self.assertIsNone(self.terminal())
        conn = connect(self.path)
        try:
            self.assertIsNone(rounds.load(Database(conn), self.new_rid).reason)
        finally:
            conn.close()
        self.assertFalse(self.window.destroyed)
        while self.tasks:
            self.tasks.pop()()
        self.assertTrue(self.process.alive)

    def check_committed_abort_with_failed_pause_signal_cleanup(self, stage):
        runtime = self.paused_cleanup() if stage == "cleanup" else None
        if runtime is None:
            self.running()
            self.api.pause_run()
            self.clock.advance(7)
        conn = connect(self.path)
        try:
            insert_inventory_rows(conn, [("A01", "offer1", "2026-10-08")])
        finally:
            conn.close()
        denied = True
        def open_conn():
            conn = connect(self.path)
            def authorize(action, table, column, dbname, source):
                return (sqlite3.SQLITE_DENY if denied and action == sqlite3.SQLITE_DELETE
                        and table == "stop_requests" else sqlite3.SQLITE_OK)
            conn.set_authorizer(authorize)
            return conn
        self.api._open_conn = open_conn  # real SQLite failure at the DB boundary
        waits = 0
        def wait(seconds):
            nonlocal waits, denied
            waits += 1
            self.assertFalse(self.window.destroyed)
            self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
            self.assertFalse(self.api.resume_run()["ok"])
            if waits == 2:
                denied = False
                self.clock.advance(2)
                if runtime:
                    runtime.fail_close = False
            if waits > 3:
                self.fail("must recover without another close or page polling")
        self.install(answer=True, wait=wait)
        self.assertFalse(self.window.close())
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.assertFalse(self.window.destroyed)
        self.assertEqual(len(self.tasks), 1)
        self.tasks.pop()()  # only independent driver; no page polling/second close
        self.assertTrue(self.window.destroyed)
        self.assertFalse(self.process.alive)
        self.assertEqual(waits, 2, "must keep retrying before DB recovery")
        conn = connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT stock FROM inventory").fetchone()[0], 3)
            self.assertIsNone(Database(conn).stop_request())
        finally:
            conn.close()
        if runtime:
            self.assertEqual(sum(a[0] == "bind" for a in runtime.actions), 1)
            self.assertEqual(sum(a[0] == "terminate" for a in runtime.actions), 1)
            self.assertTrue(all(a[1] == stop_request.BoundBrowser(9201, 9222, "browser-proof")
                                for a in runtime.actions if a[0] == "close_browser"))

    def test_pause_stopping_abandoned_then_db_failure_keeps_independent_driver(self):
        self.check_committed_abort_with_failed_pause_signal_cleanup("stopping")

    def test_pause_cleanup_abandoned_then_db_failure_keeps_independent_driver(self):
        self.check_committed_abort_with_failed_pause_signal_cleanup("cleanup")

    def test_pause_stopping_confirm_reuses_original_stop_window(self):
        self.running()
        self.assertTrue(self.api.pause_run()["ok"])
        self.clock.advance(7)
        waits = 0
        def wait(seconds):
            nonlocal waits
            waits += 1
            if waits > 1:
                self.fail("pause upgrade restarted the original deadline")
            self.clock.advance(2)
        self.install(answer=True, wait=wait)
        self.assertFalse(self.window.close())
        self.assertIn("已收到用户暂停请求，采集进程正在停止", self.messages[0])
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.assertIn("正在中止", self.window.title)
        conn = connect(self.path)
        try:
            self.assertIsNone(Database(conn).stop_request())
        finally:
            conn.close()
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)
        self.assertFalse(self.process.alive)

    def test_fully_paused_confirm_writes_abandonment_without_process(self):
        self.running()
        self.api.pause_run()
        self.process.alive = False
        self.api.get_start()
        self.install(answer=True)
        self.assertFalse(self.window.close())
        self.assertIn("本轮已由用户主动暂停", self.messages[0])
        self.assertEqual(self.terminal(), rounds.TerminalReason.ABANDONED)
        self.assertFalse(self.api.resume_run()["ok"])
        self.tasks.pop()()
        self.assertTrue(self.window.destroyed)

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
