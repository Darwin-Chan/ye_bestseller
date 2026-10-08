"""Ticket 01: explicit abort, safe cleanup, then explicit new round."""
import json
from contextlib import closing, contextmanager
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import gui
from bestseller_monitor import rounds, pipeline, single_instance
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, connect, cst_date
from frozen_clock import frozen_clock
from helpers import crawler_cfg, isolated_locks, new_round
from test_gui import gui_api, store_today_plan, StopClock


class ExplicitNewRoundTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(frozen_clock())
        self.enterContext(isolated_locks())
        self.enterContext(patch("bestseller_monitor.browser_proc.process_alive", return_value=True))
        self.enterContext(patch("bestseller_monitor.browser_proc.process_creation_proof", return_value="browser-proof"))
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.db_path = Path(self.tmp) / 'test.db'
        self.shops = [Shop(k, k, f'https://{k}.example/') for k in ('A', 'B', 'C', 'D')]
        store_today_plan(self.db_path, *((k, 'm-test', 3) for k in ('A', 'B', 'C', 'D')))
        self.clock = StopClock()
        self.api = gui_api(shops=self.shops, db_path=self.db_path, stop_clock=self.clock)

    def create_round(self, *, day=None):
        conn = connect(self.db_path)
        try:
            return new_round(Database(conn), 'A', 'B', 'C', 'D', run_date=day)
        finally:
            conn.close()

    def load_round(self, rid):
        conn = connect(self.db_path)
        try:
            return rounds.load(Database(conn), rid)
        finally:
            conn.close()

    def test_explicit_new_never_adopts_or_ends_an_unfinished_round(self):
        rid = self.create_round()
        with patch.object(gui.subprocess, 'Popen') as spawn:
            for keys in (['D'], ['A', 'B', 'C', 'D']):
                with self.subTest(keys=keys):
                    result = self.api.start_run(keys)
                    self.assertFalse(result['ok'])
                    self.assertIn('中止', result['error'])
            spawn.assert_not_called()
        self.assertTrue(self.load_round(rid).in_progress)

    def test_unfinished_database_row_is_not_proof_of_this_windows_pause(self):
        rid = self.create_round()
        self.api.get_run()
        with patch.object(gui.subprocess, 'Popen') as spawn:
            result = self.api.resume_run()
            self.assertFalse(result['ok'])
            self.assertIn('暂停', result['error'])
            spawn.assert_not_called()
        self.assertTrue(self.load_round(rid).in_progress)

    def test_new_intent_is_rechecked_after_acquiring_the_real_collection_lock(self):
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertTrue(self.api.start_run(['D'])['ok'])
        cmd = spawn.call_args.args[0]
        self.assertIn('--new-round-intent', cmd)
        intent = rounds.NewRoundIntent.from_json(cmd[cmd.index('--new-round-intent') + 1])
        created = []
        def prepare_dirs():
            self.assertTrue(single_instance.is_held(single_instance.CRAWLER_LOCK))
            created.append(self.create_round())
        cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=prepare_dirs)
        with patch('bestseller_monitor.browser_pw.open_session') as browser:
            with self.assertRaises(rounds.ScopeMismatch):
                pipeline.run_round(cfg, [self.shops[-1]], new_intent=intent)
            browser.assert_not_called()
        self.assertTrue(self.load_round(created[0]).in_progress)
        self.assertFalse(single_instance.is_held(single_instance.CRAWLER_LOCK))

    def test_public_locked_new_round_opens_only_authorized_scope_and_cleans_identity(self):
        from bestseller_monitor import browser_pw
        from bestseller_monitor.db import cst_date
        conn = connect(self.db_path)
        try:
            intent = rounds.plan_new(Database(conn), rounds.RoundRequest(cst_date(), (
                rounds.ShopScope("D", self.shops[-1].url, "D"),)))
        finally:
            conn.close()
        observed = []
        def open_browser(cfg, *, publish_browser):
            self.assertTrue(single_instance.is_held(single_instance.CRAWLER_LOCK))
            with closing(connect(self.db_path)) as conn:
                db = Database(conn)
                current = rounds.active_round(db)
                observed.append(current.id)
                self.assertEqual(current.shop_keys, ("D",))
                # A completed empty listing fixture keeps this lock/DB test offline.
                db.save_shop_offers(current.id, "D", cfg.base_url or self.shops[-1].url, "D", [], 1, confirmed_empty=True)
            publish_browser("BORROWED", 9222, None, None)
            return None, None, object(), None
        cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=lambda: None, start_browser=False)
        with patch.object(browser_pw, "open_session", side_effect=open_browser), \
                patch.object(browser_pw, "close_session"):
            pipeline.run_round(cfg, [self.shops[-1]], new_intent=intent)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.load_round(observed[0]).reason, rounds.TerminalReason.COMPLETED)
        self.assertTrue(self.api.get_start()["start_allowed"])
        self.assertFalse(single_instance.is_held(single_instance.CRAWLER_LOCK))
        with closing(connect(self.db_path)) as conn:
            self.assertIsNone(Database(conn).crawler_process())

    def test_locked_new_intent_rejects_changed_date_metadata_and_finished_competitor(self):
        from bestseller_monitor.db import cst_date
        for change in ("date", "metadata", "finished_competitor"):
            with self.subTest(change=change):
                conn = connect(self.db_path)
                try:
                    db = Database(conn)
                    intent = rounds.plan_new(db, rounds.RoundRequest(cst_date(), (
                        rounds.ShopScope("D", self.shops[-1].url, "D"),)))
                    if change == "date":
                        intent = rounds.NewRoundIntent(rounds.RoundRequest("2026-09-13", intent.request.shops), intent.after_round)
                    if change == "finished_competitor":
                        competitor = rounds.open(db, intent.request).round
                        rounds.finish(db, competitor, rounds.TerminalReason.COMPLETED)
                    before = rounds.latest(db)
                finally:
                    conn.close()
                shops = [Shop("D", "changed-name", self.shops[-1].url)] if change == "metadata" else [self.shops[-1]]
                cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=lambda: None)
                with patch("bestseller_monitor.browser_pw.open_session") as browser:
                    with self.assertRaises(rounds.ScopeMismatch):
                        pipeline.run_round(cfg, shops, new_intent=intent)
                    browser.assert_not_called()
                with closing(connect(self.db_path)) as conn:
                    self.assertEqual(rounds.latest(Database(conn)), before)
                self.assertFalse(single_instance.is_held(single_instance.CRAWLER_LOCK))

    def test_new_intent_wire_format_rejects_ambiguous_scope_or_date(self):
        valid = {"date": "2026-09-14", "after_round": 0,
                 "shops": [["D", "https://D.example/", "D"]]}
        invalid = [dict(valid, shops=["abc"]), dict(valid, shops=valid["shops"] * 2),
                   dict(valid, date="not-a-date"), dict(valid, after_round=True)]
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    rounds.NewRoundIntent.from_json(json.dumps(data))

    def test_failed_os_launch_does_not_create_an_empty_round(self):
        with patch.object(gui.subprocess, "Popen", side_effect=OSError("test launch denied")):
            result = self.api.start_run(["D"])
        self.assertFalse(result["ok"])
        with closing(connect(self.db_path)) as conn:
            self.assertIsNone(rounds.latest(Database(conn)))
        self.assertTrue(self.api.get_start()["start_allowed"])

    def test_public_new_browser_start_failure_does_not_leave_an_open_empty_round(self):
        with closing(connect(self.db_path)) as conn:
            intent = rounds.plan_new(Database(conn), rounds.RoundRequest(cst_date(), (
                rounds.ShopScope('D', self.shops[-1].url, 'D'),)))
        cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=lambda: None)
        def failure(*args, **kwargs):
            self.assertTrue(single_instance.is_held(single_instance.CRAWLER_LOCK))
            raise FileNotFoundError("test Edge executable missing")
        with patch("bestseller_monitor.browser_pw.open_session", side_effect=failure):
            with self.assertRaises(FileNotFoundError):
                pipeline.run_round(cfg, [self.shops[-1]], new_intent=intent)
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            self.assertIsNone(rounds.active_round(db), "浏览器启动失败不能留下空进行中轮次")
            failed = rounds.latest(db)
            self.assertEqual(failed.reason.value, "STARTUP_FAILED")
            self.assertIsNone(db.crawler_process())
        self.assertTrue(self.api.get_start()["start_allowed"])
        self.assertFalse(single_instance.is_held(single_instance.CRAWLER_LOCK))

    def test_driver_preparation_failure_settles_new_round_before_any_browser_start(self):
        import sqlite3
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            intent = rounds.plan_new(db, rounds.RoundRequest(cst_date(), (
                rounds.ShopScope("D", self.shops[-1].url, "D"),)))
            conn.execute("CREATE TRIGGER fail_run_params BEFORE INSERT ON run_params "
                         "BEGIN SELECT RAISE(ABORT, 'test parameter storage unavailable'); END")
            conn.commit()
        cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=lambda: None)
        with patch("bestseller_monitor.browser_pw.subprocess.Popen") as spawn, \
                patch("playwright.sync_api.sync_playwright") as playwright:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "test parameter storage unavailable"):
                pipeline.run_round(cfg, [self.shops[-1]], new_intent=intent)
            spawn.assert_not_called()
            playwright.assert_not_called()
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            self.assertIsNone(rounds.active_round(db))
            self.assertEqual(rounds.latest(db).reason.value, "STARTUP_FAILED")
            self.assertIsNone(db.crawler_process())
        self.assertFalse(single_instance.is_held(single_instance.CRAWLER_LOCK))
        self.assertTrue(self.api.get_start()["start_allowed"])

    def test_new_round_initialization_failure_does_not_leave_an_unfinished_round(self):
        with closing(connect(self.db_path)) as conn:
            intent = rounds.plan_new(Database(conn), rounds.RoundRequest(cst_date(), (
                rounds.ShopScope("D", self.shops[-1].url, "D"),)))
        cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=lambda: None,
                          shop_csv=Path(self.tmp) / "removed-shops.csv")
        with patch("bestseller_monitor.browser_pw.subprocess.Popen") as spawn, \
                patch("playwright.sync_api.sync_playwright") as playwright:
            with self.assertRaises(FileNotFoundError):
                pipeline.run_round(cfg, [self.shops[-1]], new_intent=intent)
            spawn.assert_not_called()
            playwright.assert_not_called()
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            self.assertIsNone(rounds.active_round(db), "采集前初始化失败不能留下空进行中轮")
            self.assertEqual(rounds.latest(db).reason.value, "STARTUP_FAILED")
            self.assertIsNone(db.crawler_process())
        self.assertFalse(single_instance.is_held(single_instance.CRAWLER_LOCK))
        self.assertTrue(self.api.get_start()["start_allowed"])
        with patch.object(gui.subprocess, "Popen"):
            self.assertTrue(self.api.start_run(["D"])["ok"])

    def test_real_missing_browser_startup_releases_cleanup_gate_for_explicit_retry(self):
        with closing(connect(self.db_path)) as conn:
            intent = rounds.plan_new(Database(conn), rounds.RoundRequest(cst_date(), (
                rounds.ShopScope("D", self.shops[-1].url, "D"),)))
        cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=lambda: None,
                          start_browser=True, attach_port=9222,
                          chrome_path=Path(self.tmp) / "missing-edge.exe")
        with patch("bestseller_monitor.browser_pw.subprocess.Popen") as spawn, \
                patch("playwright.sync_api.sync_playwright") as playwright:
            with self.assertRaises(FileNotFoundError):
                pipeline.run_round(cfg, [self.shops[-1]], new_intent=intent)
            spawn.assert_not_called()
            playwright.assert_not_called()
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            failed = rounds.latest(db)
            self.assertEqual(failed.reason.value, "STARTUP_FAILED")
            self.assertIsNone(db.crawler_process(), "没有启动资源不能留下无法清偿的浏览器欠账")
        self.assertFalse(single_instance.is_held(single_instance.CRAWLER_LOCK))
        self.assertTrue(self.api.get_start()["start_allowed"])
        with patch.object(gui.subprocess, "Popen"):
            self.assertTrue(self.api.start_run(["D"])["ok"])
        self.assertEqual(self.load_round(failed.id).reason.value, "STARTUP_FAILED")

    def test_partial_browser_start_failure_keeps_owned_or_unknown_cleanup_gate(self):
        for state in ("OWNED", "UNKNOWN"):
            with self.subTest(state=state):
                with closing(connect(self.db_path)) as conn:
                    db = Database(conn)
                    previous = db.crawler_process()
                    if previous is not None:
                        # Explicit fixture disposal, not a production cleanup claim.
                        db.clear_crawler_process(target_pid=previous["pid"],
                                                 target_started_at=previous["started_at"])
                    intent = rounds.plan_new(db, rounds.RoundRequest(cst_date(), (
                        rounds.ShopScope("D", self.shops[-1].url, "D"),)))
                def failure(cfg, *, publish_browser):
                    self.assertTrue(single_instance.is_held(single_instance.CRAWLER_LOCK))
                    publish_browser(state, 9222, 9201 if state == "OWNED" else None,
                                    "browser-proof" if state == "OWNED" else None)
                    raise RuntimeError("test partial browser startup")
                cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=lambda: None)
                with patch("bestseller_monitor.browser_pw.open_session", side_effect=failure):
                    with self.assertRaises(RuntimeError):
                        pipeline.run_round(cfg, [self.shops[-1]], new_intent=intent)
                with closing(connect(self.db_path)) as conn:
                    db = Database(conn)
                    self.assertIsNone(rounds.active_round(db))
                    self.assertEqual(rounds.latest(db).reason.value, "STARTUP_FAILED")
                    self.assertEqual(db.crawler_process()["browser_state"], state)
                self.assertFalse(single_instance.is_held(single_instance.CRAWLER_LOCK))
                self.assertFalse(self.api.get_start()["start_allowed"])
                self.assertFalse(self.api.start_run(["D"])["ok"])

    def test_browser_start_failure_preserves_a_terminal_already_committed(self):
        with closing(connect(self.db_path)) as conn:
            intent = rounds.plan_new(Database(conn), rounds.RoundRequest(cst_date(), (
                rounds.ShopScope("D", self.shops[-1].url, "D"),)))
        def failure(cfg, *, publish_browser):
            with closing(connect(self.db_path)) as conn:
                db = Database(conn)
                rounds.finish(db, rounds.active_round(db), rounds.TerminalReason.COMPLETED)
            raise FileNotFoundError("test failed startup after competing terminal")
        cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=lambda: None)
        with patch("bestseller_monitor.browser_pw.open_session", side_effect=failure):
            with self.assertRaises(FileNotFoundError):
                pipeline.run_round(cfg, [self.shops[-1]], new_intent=intent)
        with closing(connect(self.db_path)) as conn:
            self.assertEqual(rounds.latest(Database(conn)).reason, rounds.TerminalReason.COMPLETED)
        self.assertTrue(self.api.get_start()["start_allowed"])

    @contextmanager
    def competing_execution_at_terminal_write(self, rid):
        """Inject only an external SQLite race; both public entrances stay real."""
        import sqlite3
        competitor = self.enterContext(closing(connect(self.db_path)))
        raced = []
        native_connect = sqlite3.connect
        def trace(sql):
            if (sql == "BEGIN IMMEDIATE" or sql.startswith("UPDATE rounds SET terminal_reason=")) and not raced:
                raced.append(True)
                Database(competitor).record_crawler_process(5432, rid,
                    process_os_started="process-b", browser_state="BORROWED")
                lock_b = single_instance.acquire(single_instance.CRAWLER_LOCK)
                self.addCleanup(lock_b.release)
        def traced_connect(*args, **kwargs):
            conn = native_connect(*args, **kwargs)
            conn.set_trace_callback(trace)
            return conn
        with patch.object(sqlite3, "connect", side_effect=traced_connect):
            yield raced, competitor

    def test_paused_confirmation_rechecks_absent_target_at_sqlite_write_boundary(self):
        rid, process, death = self.running()
        self.assertTrue(self.api.pause_run()["ok"])
        death()
        self.api.get_run()
        prepared = self.api.prepare_abort()
        self.assertTrue(prepared["ok"])
        with self.competing_execution_at_terminal_write(rid) as (raced, competitor):
            result = self.api.confirm_abort(prepared["confirmation"])
        self.assertTrue(raced)
        self.assertFalse(result["ok"], "暂停确认的空目标不能授权同轮后来登记的执行")
        self.assertTrue(self.load_round(rid).in_progress)
        self.assertEqual(Database(competitor).crawler_process()["pid"], 5432)
        process.terminate.assert_not_called()

    def test_native_paused_close_rechecks_target_at_sqlite_write_boundary(self):
        rid, process, death = self.running()
        self.assertTrue(self.api.pause_run()["ok"])
        death()
        self.api.get_run()
        expected = self.api.close_snapshot()
        self.assertEqual(expected.kind, "paused")
        with self.competing_execution_at_terminal_write(rid) as (raced, competitor):
            result = self.api.begin_close(expected)
        self.assertTrue(raced)
        self.assertFalse(result["ok"], "正常关窗不能结束同轮后来执行")
        self.assertTrue(self.load_round(rid).in_progress)
        self.assertFalse(self.api.close_ready(expected))
        self.assertEqual(Database(competitor).crawler_process()["pid"], 5432)
        process.terminate.assert_not_called()

    def test_first_committed_terminal_wins_a_real_sqlite_write_race(self):
        rid = self.create_round()
        conn = connect(self.db_path)
        self.addCleanup(conn.close)
        db = Database(conn)
        original = rounds.load(db, rid)
        raced = []
        def competitor(sql):
            if sql.startswith('UPDATE rounds SET terminal_reason=') and not raced:
                raced.append(True)
                other = connect(self.db_path)
                try:
                    rounds.finish(Database(other), original, rounds.TerminalReason.COMPLETED,
                                  note='自然完成先提交')
                finally:
                    other.close()
        conn.set_trace_callback(competitor)
        settled = rounds.finish_if_open(db, original, rounds.TerminalReason.ABANDONED,
                                        note='迟到人工中止')
        self.assertTrue(raced)
        self.assertEqual(settled.reason, rounds.TerminalReason.COMPLETED)
        self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.COMPLETED)
        self.assertEqual(self.api.get_result()['tag'], '正常完成')

    def running(self, *, owned=False):
        rid = self.create_round()
        conn = connect(self.db_path)
        try:
            Database(conn).record_crawler_process(
                pid=4321, round_id=rid, process_os_started='crawler-proof',
                browser_state='OWNED' if owned else 'NOT_STARTED',
                browser_pid=9201 if owned else None,
                browser_os_started='browser-proof' if owned else None,
                browser_port=9222 if owned else None)
        finally:
            conn.close()
        lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(lock.release)
        process = MagicMock(pid=4321)
        process.poll.return_value = None
        def death():
            process.poll.return_value = 0
            lock.release()
        process.terminate.side_effect = death
        self.api.proc = process
        self.api.round_id = rid
        return rid, process, death

    def test_force_transaction_identity_loss_keeps_frozen_browser_debt_and_gate(self):
        import sqlite3
        from bestseller_monitor import browser_proc
        rid, process, death = self.running(owned=True)
        self.assertTrue(self.api.abort_run()["ok"])
        self.clock.advance(20)
        competitor = self.enterContext(closing(connect(self.db_path)))
        original = Database(competitor).crawler_process()
        native_connect = sqlite3.connect
        raced = []
        def trace(sql):
            if sql == "BEGIN IMMEDIATE" and not raced:
                raced.append(True)
                Database(competitor).clear_crawler_process(
                    target_pid=original["pid"], target_started_at=original["started_at"])
        def traced_connect(*args, **kwargs):
            conn = native_connect(*args, **kwargs)
            conn.set_trace_callback(trace)
            return conn
        with patch.object(sqlite3, "connect", side_effect=traced_connect), \
                patch.object(browser_proc, "close_browser", return_value=None) as close:
            result = self.api.get_run()
            self.assertTrue(raced)
            self.assertEqual(result["stopping"], "verifying")
            process.terminate.assert_not_called()
            close.assert_not_called()
            self.assertFalse(self.api.get_start()["start_allowed"])
            self.assertFalse(self.api.start_run(["D"])["ok"])
            death()
            self.assertEqual(self.api.get_run()["stopping"], "cleanup_pending")
            self.assertFalse(self.api.get_start()["start_allowed"])
            self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)
        with patch.object(browser_proc, "close_browser", return_value=True):
            self.assertIsNone(self.api.get_run()["stopping"])
        self.assertTrue(self.api.get_start()["start_allowed"])
        self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)

    def test_abort_during_pause_cleanup_reuses_original_stop_and_target(self):
        from bestseller_monitor import stop_request
        rid, process, _ = self.running(owned=True)
        with patch('bestseller_monitor.browser_proc.close_browser', return_value=None):
            self.assertTrue(self.api.pause_run()['ok'])
            self.clock.advance(9)
            self.api.get_run()
            self.assertEqual(self.api.get_result()['stopping'], 'cleanup_pending')
            original = self.api.close_snapshot().target
            result = self.api.abort_run()
            self.assertTrue(result['ok'])
            self.assertEqual(self.api.close_snapshot().kind, "aborting")
            self.assertEqual(self.api.close_snapshot().target, original)
            self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)
            self.assertFalse(self.api.resume_run()['ok'])
        process.terminate.assert_called_once()

    def test_failed_browser_cleanup_never_becomes_success_at_a_deadline(self):
        rid, process, _ = self.running(owned=True)
        with patch('bestseller_monitor.browser_proc.close_browser', return_value=None):
            self.assertTrue(self.api.abort_run()['ok'])
            self.clock.advance(9)
            self.assertEqual(self.api.get_result()['stopping'], 'cleanup_pending')
            self.clock.advance(1000)
            with patch.object(gui.subprocess, 'Popen') as spawn:
                result = self.api.start_run(['D'])
                self.assertFalse(result['ok'])
                self.assertIn('收尾', result['error'])
                spawn.assert_not_called()
            self.assertEqual(self.api.get_result()['stopping'], 'cleanup_pending')
        self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)
        process.terminate.assert_called_once()

    def test_cooperative_process_exit_still_requires_owned_browser_cleanup(self):
        rid, process, death = self.running(owned=True)
        with patch('bestseller_monitor.browser_proc.close_browser', return_value=None) as close:
            self.assertTrue(self.api.abort_run()['ok'])
            death()
            result = self.api.get_result()
            self.assertEqual(result['stopping'], 'cleanup_pending')
            self.assertFalse(self.api.start_run(['D'])['ok'])
            self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)
            close.assert_called()
        process.terminate.assert_not_called()

    def test_no_lock_is_not_proof_that_the_last_owned_browser_was_cleaned(self):
        rid = self.create_round()
        conn = connect(self.db_path)
        try:
            db = Database(conn)
            rounds.finish(db, rounds.load(db, rid), rounds.TerminalReason.COMPLETED)
            intent = rounds.plan_new(db, rounds.RoundRequest(cst_date(), (
                rounds.ShopScope('D', 'https://D.example/', 'D'),)))
            db.record_crawler_process(pid=4321, round_id=rid, process_os_started='proof',
                                      browser_state='OWNED', browser_pid=9201,
                                      browser_os_started='browser-proof', browser_port=9222)
        finally:
            conn.close()
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertFalse(self.api.start_run(['D'])['ok'])
            spawn.assert_not_called()
        cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=lambda: None)
        with patch('bestseller_monitor.browser_pw.open_session') as browser:
            with self.assertRaises(rounds.ScopeMismatch):
                pipeline.run_round(cfg, [self.shops[-1]], new_intent=intent)
            browser.assert_not_called()
        conn = connect(self.db_path)
        try:
            self.assertEqual(Database(conn).crawler_process()['browser_pid'], 9201)
        finally:
            conn.close()

    def page(self):
        """The shipped HTML with a real API bridge, no network or user profile."""
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        self.addCleanup(pw.stop)
        browser = pw.chromium.launch(channel="msedge", headless=True)
        self.addCleanup(browser.close)
        context = browser.new_context()
        context.route("https://**/*", lambda route: route.abort())
        context.route("http://**/*", lambda route: route.abort())
        page = context.new_page()
        def bridge(name, args):
            result = getattr(self.api, name)(*args)
            return result
        page.expose_function("ticketApi", bridge)
        page.add_init_script("""
          window.pywebview = {api: new Proxy({}, {
            get: (target, name) => name in target ? target[name] : (...args) => window.ticketApi(name, args)
          })};
          window.alert = () => {};
        """)
        page.goto((Path(gui.PROJECT_ROOT) / "bestseller_monitor/pages/ui_live.html").as_uri())
        page.locator("input[data-i]").first.wait_for()
        return page

    def test_real_start_page_rejects_unfinished_round_before_click(self):
        from playwright.sync_api import expect
        rid = self.create_round()
        page = self.page()
        expect(page.locator("#startBtn")).to_be_disabled()
        expect(page.locator("#activeRoundBanner")).to_contain_text("中止")
        self.assertTrue(self.load_round(rid).in_progress)


    def test_real_page_old_abort_confirmation_cannot_stop_a_replacement(self):
        from playwright.sync_api import expect
        rid, process, death = self.running()
        page = self.page()
        page.locator("#abortForeignBtn").click()
        expect(page.locator("#confirmModal")).to_have_class("show")
        death()
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            rounds.finish(db, rounds.load(db, rid), rounds.TerminalReason.COMPLETED)
            replacement = new_round(db, 'A', 'B', 'C', 'D')
            db.record_crawler_process(pid=9876, round_id=replacement,
                                      process_os_started='replacement-proof',
                                      browser_state='NOT_STARTED')
        self.api.proc = None
        lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(lock.release)
        with patch("bestseller_monitor.browser_proc.bind_process") as bind:
            page.locator("#confirmOk").click()
            expect(page.locator("#confirmModal")).not_to_have_class("show")
            page.wait_for_function("document.querySelector('#apiError').style.display !== 'none' || document.querySelector('#panel-result').classList.contains('active')")
            self.assertTrue(self.load_round(replacement).in_progress,
                            "A 的确认不能提交 B 的终态")
            bind.assert_not_called()
        self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.COMPLETED)

    def test_real_page_can_promote_pause_cleanup_to_abort_without_restart(self):
        from playwright.sync_api import expect
        with patch.object(gui.subprocess, "Popen", side_effect=lambda *a, **kw: self.running(owned=True)[1]) as spawn:
            with patch("bestseller_monitor.browser_proc.close_browser", return_value=None):
                page = self.page()
                page.locator("#startBtn").click()
                expect(page.locator("#panel-run")).to_have_class("panel active")
                page.locator("#pauseBtn").click()
                self.clock.advance(9)
                expect(page.locator("#rStopping")).to_contain_text("重试收尾", timeout=8000)
                expect(page.locator("#abortBtn")).to_be_enabled()
                page.locator("#abortBtn").click()
                page.locator("#confirmOk").click()
                expect(page.locator("#resTag")).to_have_text("人工中止")
                expect(page.locator("#resStopping")).to_be_visible()
                self.assertFalse(self.api.resume_run()["ok"])
                page.locator("#backBtn").click()
                expect(page.locator("#startBtn")).to_be_disabled()
                self.assertEqual(spawn.call_count, 1)

    def test_verified_already_exited_browser_completes_cleanup_without_another_kill(self):
        rid, process, _ = self.running(owned=True)
        with patch("bestseller_monitor.browser_proc.close_browser", return_value=None):
            self.assertTrue(self.api.abort_run()["ok"])
            self.clock.advance(9)
            self.assertEqual(self.api.get_result()["stopping"], "cleanup_pending")
            with patch("bestseller_monitor.browser_proc.process_alive", return_value=False):
                self.assertIsNone(self.api.get_result()["stopping"])
                self.assertTrue(self.api.get_start()["start_allowed"])
                self.assertFalse(self.api.resume_run()["ok"])
        process.terminate.assert_called_once()
        self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)

    def test_repeating_terminal_commit_returns_real_fact_even_with_stale_round(self):
        rid = self.create_round()
        conn = connect(self.db_path)
        try:
            db = Database(conn)
            stale = rounds.load(db, rid)
            first = rounds.finish_if_open(db, stale, rounds.TerminalReason.ABANDONED)
            repeated = rounds.finish_if_open(db, stale, rounds.TerminalReason.ABANDONED)
            self.assertEqual(repeated, first)
        finally:
            conn.close()

    def test_real_page_cleanup_never_creates_next_round_until_explicit_start(self):
        from playwright.sync_api import expect
        from helpers import insert_inventory_rows
        from bestseller_monitor.db import cst_date
        conn = connect(self.db_path)
        try:
            insert_inventory_rows(conn, [("D", "saved-offer", cst_date())])
        finally:
            conn.close()
        with patch.object(gui.subprocess, "Popen", side_effect=lambda *a, **kw: self.running(owned=True)[1]) as spawn:
            with patch("bestseller_monitor.browser_proc.close_browser", return_value=9201):
                page = self.page()
                page.locator("#startBtn").click()
                expect(page.locator("#panel-run")).to_have_class("panel active")
                page.locator("#abortBtn").click()
                expect(page.locator("#confirmSub")).to_contain_text("不能继续")
                page.locator("#confirmOk").click()
                expect(page.locator("#resTag")).to_have_text("人工中止")
                self.clock.advance(9)
                expect(page.locator("#resStopping")).to_be_hidden(timeout=8000)
                self.assertEqual(spawn.call_count, 1)
                self.assertFalse(self.api.resume_run()["ok"])
                page.locator("#backBtn").click()
                expect(page.locator("#startBtn")).to_be_enabled()
                self.assertEqual(self.api.get_start()["ov"]["products"], 1)
                page.locator("#startBtn").click()
                expect(page.locator("#panel-run")).to_have_class("panel active")
                self.assertEqual(spawn.call_count, 2)

    def test_terminal_write_failure_does_not_stop_or_release_new_round_gate(self):
        import sqlite3
        fail = [False]
        def factory():
            conn = connect(self.db_path)
            if fail[0]:
                conn.set_authorizer(lambda action, table, *rest:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE and table == "rounds"
                    else sqlite3.SQLITE_OK)
            return conn
        self.api = gui_api(shops=self.shops, open_conn=factory, stop_clock=self.clock)
        rid, process, _ = self.running()
        fail[0] = True
        result = self.api.abort_run()
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertTrue(self.load_round(rid).in_progress)
        process.terminate.assert_not_called()
        self.assertFalse(self.api.start_run(["D"])["ok"])
        fail[0] = False
        self.assertTrue(self.api.abort_run()["ok"])
        self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)

    def test_native_close_cancel_keeps_each_running_or_paused_phase(self):
        from bestseller_monitor.crawler_close import install_close_guard
        from test_crawler_close import Window
        for phase in ("running", "stopping", "cleanup", "paused"):
            with self.subTest(phase=phase):
                self.api = gui_api(shops=self.shops, db_path=self.db_path, stop_clock=self.clock)
                rid, process, death = self.running(owned=True)
                with patch("bestseller_monitor.browser_proc.close_browser", return_value=None if phase == "cleanup" else 9201):
                    if phase != "running":
                        self.api.pause_run()
                    if phase == "cleanup":
                        self.clock.advance(9)
                        self.api.get_run()
                    if phase == "paused":
                        death()
                        self.api.get_run()
                    window, tasks, questions = Window(), [], []
                    def confirm(text):
                        questions.append(text)
                        return False
                    install_close_guard(window, self.api, confirm=confirm, notify=lambda text: None,
                                        title="test", launch=tasks.append, wait=lambda sec: None)
                    self.assertFalse(window.close())
                    self.assertFalse(window.destroyed)
                    self.assertEqual(len(questions), 1)
                    self.assertIn("本轮", questions[0])
                    self.assertTrue(self.load_round(rid).in_progress)
                    death()
                    self.api.abort_run()
                    # Drain the cancel's independent pause supervisor after cleanup succeeds.
                with patch("bestseller_monitor.browser_proc.close_browser", return_value=9201):
                    for task in tasks:
                        task()

    def test_native_confirmed_close_abandons_and_waits_through_failed_cleanup(self):
        from bestseller_monitor.crawler_close import install_close_guard
        from test_crawler_close import Window
        rid, process, _ = self.running(owned=True)
        window, tasks, notices = Window(), [], []
        attempts = []
        def wait(seconds):
            attempts.append(window.destroyed)
            self.assertFalse(self.api.start_run(["D"])["ok"])
            self.clock.advance(9)
            if len(attempts) > 3:
                raise AssertionError("close watchdog")
        with patch("bestseller_monitor.browser_proc.close_browser", side_effect=[None, None, 9201]):
            install_close_guard(window, self.api, confirm=lambda text: True, notify=notices.append,
                                title="test", launch=tasks.append, wait=wait)
            self.assertFalse(window.close())
            self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)
            self.assertFalse(window.destroyed)
            tasks.pop()()
            self.assertTrue(window.destroyed)
        self.assertTrue(attempts)
        self.assertFalse(any(attempts))
        reopened = gui_api(shops=self.shops, db_path=self.db_path, stop_clock=self.clock)
        self.assertFalse(reopened.resume_run()["ok"])
        self.assertFalse(reopened.get_run().get("can_resume", False))
        self.assertTrue(reopened.get_start()["start_allowed"])


    def test_native_confirm_in_each_pause_phase_ends_once_and_reopen_cannot_resume(self):
        from bestseller_monitor.crawler_close import install_close_guard
        from test_crawler_close import Window
        for phase in ("stopping", "cleanup", "paused"):
            with self.subTest(phase=phase):
                self.api = gui_api(shops=self.shops, db_path=self.db_path, stop_clock=self.clock)
                rid, process, death = self.running(owned=True)
                self.assertTrue(self.api.pause_run()["ok"])
                with patch("bestseller_monitor.browser_proc.close_browser", return_value=None if phase == "cleanup" else 9201):
                    if phase == "cleanup":
                        self.clock.advance(9)
                        self.assertEqual(self.api.get_run()["stopping"], "cleanup_pending")
                    if phase == "paused":
                        death()
                        self.assertTrue(self.api.get_run()["can_resume"])
                    window, tasks, questions = Window(), [], []
                    def confirm(text):
                        questions.append(text)
                        return True
                    install_close_guard(window, self.api, confirm=confirm, notify=lambda text: None,
                                        title="test", launch=tasks.append,
                                        wait=lambda sec: self.clock.advance(9))
                    self.assertFalse(window.close())
                    self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)
                    self.assertFalse(self.api.resume_run()["ok"])
                    self.assertEqual(len(questions), 1)
                with patch("bestseller_monitor.browser_proc.close_browser", return_value=9201):
                    for task in tasks:
                        task()
                self.assertTrue(window.destroyed)
                self.assertLessEqual(process.terminate.call_count, 1)
                reopened = gui_api(shops=self.shops, db_path=self.db_path, stop_clock=self.clock)
                self.assertFalse(reopened.get_run().get("can_resume", False))
                self.assertFalse(reopened.resume_run()["ok"])
                self.assertTrue(reopened.get_start()["start_allowed"])

    def test_fully_paused_abort_and_repeat_keep_data_and_first_terminal(self):
        from helpers import insert_inventory_rows
        rid, process, death = self.running()
        with closing(connect(self.db_path)) as conn:
            insert_inventory_rows(conn, [("D", "kept-paused", cst_date())])
        self.assertTrue(self.api.pause_run()["ok"])
        death()
        self.assertTrue(self.api.get_run()["can_resume"])
        self.assertTrue(self.api.abort_run()["ok"])
        first = self.load_round(rid)
        self.assertTrue(self.api.abort_run()["ok"])
        self.assertEqual(self.load_round(rid), first)
        self.assertEqual(first.reason, rounds.TerminalReason.ABANDONED)
        self.assertEqual(self.api.get_start()["ov"]["products"], 1)
        self.assertFalse(self.api.resume_run()["ok"])
        process.terminate.assert_not_called()

    def test_completed_before_abort_keeps_completion_and_refuses_continue(self):
        rid, process, _ = self.running()
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            finished = rounds.finish(db, rounds.load(db, rid), rounds.TerminalReason.COMPLETED)
        self.assertTrue(self.api.abort_run()["ok"])
        self.clock.advance(9)
        self.assertEqual(self.api.get_result()["tag"], "正常完成")
        self.assertEqual(self.load_round(rid), finished)
        self.assertFalse(self.api.resume_run()["ok"])
        self.assertTrue(self.api.get_start()["start_allowed"])
        process.terminate.assert_called_once()

    def test_unknown_browser_proof_never_closes_or_releases_new_round_gate(self):
        rid, process, death = self.running(owned=True)
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            row = db.crawler_process()
            db.publish_browser_state_if_current(target_pid=row["pid"], target_started_at=row["started_at"],
                browser_state="UNKNOWN", browser_port=9222, browser_pid=None, browser_os_started=None)
        with patch("bestseller_monitor.browser_proc.close_browser") as close:
            self.assertTrue(self.api.abort_run()["ok"])
            death()
            self.clock.advance(1000)
            self.assertEqual(self.api.get_result()["stopping"], "verifying")
            self.assertFalse(self.api.get_start()["start_allowed"])
            self.assertFalse(self.api.start_run(["D"])["ok"])
            close.assert_not_called()
        self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)
        process.terminate.assert_not_called()

    def test_reused_browser_pid_is_not_closed_and_os_proof_can_settle_old_cleanup(self):
        rid, process, _ = self.running(owned=True)
        with patch("bestseller_monitor.browser_proc.close_browser", return_value=None) as close:
            self.assertTrue(self.api.abort_run()["ok"])
            self.clock.advance(9)
            self.assertEqual(self.api.get_result()["stopping"], "cleanup_pending")
            with patch("bestseller_monitor.browser_proc.process_creation_proof", return_value="unrelated-new-browser"):
                self.assertIsNone(self.api.get_result()["stopping"])
        self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)
        self.assertTrue(self.api.get_start()["start_allowed"])
        self.assertTrue(all(call.kwargs.get("browser_os_started") == "browser-proof"
                            for call in close.call_args_list))
        process.terminate.assert_called_once()

    def test_public_pipeline_preserves_owned_browser_debt_when_close_fails(self):
        from bestseller_monitor import browser_pw
        with closing(connect(self.db_path)) as conn:
            intent = rounds.plan_new(Database(conn), rounds.RoundRequest(cst_date(), (
                rounds.ShopScope("D", self.shops[-1].url, "D"),)))
        observed = []
        def open_browser(cfg, *, publish_browser):
            publish_browser("OWNED", 9222, 9201, "browser-proof")
            with closing(connect(self.db_path)) as conn:
                db = Database(conn)
                current = rounds.active_round(db)
                observed.append(current.id)
                db.save_shop_offers(current.id, "D", self.shops[-1].url, "D", [], 1, confirmed_empty=True)
            return None, None, object(), None
        def failed_close(*args, **kwargs):
            with closing(connect(self.db_path)) as conn:
                db = Database(conn)
                rounds.finish(db, rounds.load(db, observed[0]), rounds.TerminalReason.ABANDONED)
            raise RuntimeError("test browser cleanup failed")
        cfg = crawler_cfg(db_file=self.db_path, ensure_dirs=lambda: None)
        with patch.object(browser_pw, "open_session", side_effect=open_browser), \
                patch.object(browser_pw, "close_session", side_effect=failed_close):
            with self.assertRaises(RuntimeError):
                pipeline.run_round(cfg, [self.shops[-1]], new_intent=intent)
        self.assertEqual(self.load_round(observed[0]).reason, rounds.TerminalReason.ABANDONED)
        self.assertFalse(single_instance.is_held(single_instance.CRAWLER_LOCK))
        self.assertFalse(self.api.get_start()["start_allowed"])
        self.assertFalse(self.api.start_run(["D"])["ok"])
        with closing(connect(self.db_path)) as conn:
            self.assertEqual(Database(conn).crawler_process()["browser_state"], "OWNED")


    def test_replacement_before_first_cleanup_keeps_original_browser_debt(self):
        rid, process_a, death = self.running(owned=True)
        self.assertTrue(self.api.abort_run()["ok"])
        death()  # A failed its own browser cleanup, before the GUI's first tick.
        b = self.create_round()
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            b_started = db.record_crawler_process(5432, b, process_os_started="process-b",
                                                 browser_state="BORROWED")
        lock_b = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(lock_b.release)
        process_b = MagicMock(pid=5432)
        process_b.poll.return_value = None
        self.api.proc = process_b
        with patch("bestseller_monitor.browser_proc.close_browser", return_value=None) as close:
            self.assertIsNotNone(self.api.get_result()["stopping"],
                                 "首次清理前替换身份也不能抹除 A 的 OWNED 欠账")
            close.assert_not_called()
            process_b.terminate.assert_not_called()
            with closing(connect(self.db_path)) as conn:
                db = Database(conn)
                self.assertEqual(db.crawler_process()["started_at"], b_started)
                rounds.finish(db, rounds.load(db, b), rounds.TerminalReason.COMPLETED)
                db.clear_crawler_process(target_pid=5432, target_started_at=b_started)
            process_b.poll.return_value = 0
            lock_b.release()
            self.assertFalse(self.api.get_start()["start_allowed"], "B 已结束也不能掩盖 A 尚未收尾")
        self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)

    def test_replacement_cannot_discard_known_old_browser_cleanup_debt(self):
        rid, process_a, _ = self.running(owned=True)
        with patch("bestseller_monitor.browser_proc.close_browser", return_value=None) as close:
            self.assertTrue(self.api.abort_run()["ok"])
            self.clock.advance(9)
            self.assertEqual(self.api.get_result()["stopping"], "cleanup_pending")
            b = self.create_round()
            with closing(connect(self.db_path)) as conn:
                Database(conn).record_crawler_process(5432, b, process_os_started="process-b", browser_state="BORROWED")
            lock_b = single_instance.acquire(single_instance.CRAWLER_LOCK)
            self.addCleanup(lock_b.release)
            process_b = MagicMock(pid=5432)
            process_b.poll.return_value = None
            self.api.proc = process_b
            before = close.call_count
            self.assertEqual(self.api.get_result()["stopping"], "cleanup_pending")
            self.assertFalse(self.api.start_run(["D"])["ok"])
            self.assertEqual(close.call_count, before)
            process_b.terminate.assert_not_called()
            self.assertTrue(self.load_round(b).in_progress)
        process_a.terminate.assert_called_once()

    def test_borrowed_user_browser_is_never_closed_by_abort_cleanup(self):
        rid, process, _ = self.running()
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            row = db.crawler_process()
            db.publish_browser_state_if_current(target_pid=row["pid"], target_started_at=row["started_at"],
                browser_state="BORROWED", browser_port=9222, browser_pid=None, browser_os_started=None)
        with patch("bestseller_monitor.browser_proc.close_browser") as close:
            self.assertTrue(self.api.abort_run()["ok"])
            self.clock.advance(9)
            self.assertIsNone(self.api.get_result()["stopping"])
            close.assert_not_called()
        self.assertEqual(self.load_round(rid).reason, rounds.TerminalReason.ABANDONED)
        self.assertTrue(self.api.get_start()["start_allowed"])
        process.terminate.assert_called_once()
