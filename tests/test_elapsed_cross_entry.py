"""elapsed03：公共入口、重开取数与已安装正常关窗事件的计时契约。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gui
import run
from bestseller_monitor import (browser_pw, click_listing, elapsed, guard, pipeline,
                                rounds, stop_request, views)
from bestseller_monitor.config import Config, Shop
from bestseller_monitor.db import Database, connect, utcnow
from helpers import FakeCard, ScriptedListing, crawler_cfg, isolated_locks, new_round
from test_crawler_close import Process, Window
from test_elapsed_lifecycle import live_os_identity
from test_gui import StopClock, gui_api
from test_run_cli import MINIMAL_CONFIG


class ElapsedCrossEntryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'round.db'
        self.clock = StopClock()
        self.enterContext(isolated_locks())
        self.enterContext(live_os_identity())
        self.csv = self.root / 'shops.csv'
        self.csv.write_text('shop_key,shop_name,shop_url,pages,active,offer_list_url\n'
                            'A01,店一,https://a.example/,1,1,\n', encoding='utf-8')
        self.cfg = crawler_cfg(db_file=self.path, shop_csv=self.csv,
                               ensure_dirs=lambda: self.clock.advance(60))

    def api(self, path=None):
        return gui_api(db_path=path or self.path, stop_clock=self.clock)

    def collect(self, work, cleanup=None):
        listing = ScriptedListing([[FakeCard('商品1', offer_id='11')]])
        prepare = listing.prepare

        def prepared(*args, **kwargs):
            prepare(*args, **kwargs)
            work()

        listing.prepare = prepared
        with patch.object(browser_pw, 'open_session', return_value=(None, None, None, None)), \
                patch.object(browser_pw, 'close_session', side_effect=cleanup or (lambda *a, **k: None)), \
                patch.object(click_listing, 'PlaywrightListing', return_value=listing):
            pipeline.run_round(self.cfg, [Shop('A01', '店一', 'https://a.example/')],
                               elapsed_clock=self.clock)

    def fact(self, rid, path=None):
        conn = connect(path or self.path)
        try:
            return elapsed.read(conn, rid)
        finally:
            conn.close()

    def test_reopened_gui_reads_external_execution_and_keeps_collecting_without_gui(self):
        rid = None

        def work():
            nonlocal rid
            self.clock.advance(5)
            stop_request.check()
            first = self.api()
            view = first.get_run()
            rid = view['round_id']
            self.assertEqual((view['elapsed_sec'], view['elapsed_quality']), (5, 'recorded'))
            self.assertEqual(first.get_result()['elapsed_sec'], 5)
            # 界面消失adapter：不调用正常关窗、不发停止。执行仍走真实采集侧。
            del first
            self.clock.advance(7)
            stop_request.check()
            reopened = self.api()
            for _ in range(2):
                self.assertEqual(reopened.get_run()['elapsed_sec'], 12)
                self.assertEqual(reopened.get_result()['duration_text'], '12 秒')
            self.assertEqual(self.fact(rid), elapsed.Fact(12, 'recorded'))

        self.collect(work)
        self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))
        self.assertEqual(self.api().get_result()['elapsed_sec'], 12)
    def test_confirmed_close_settles_execution_tail_but_waits_for_process_cleanup(self):
        api, window, tasks, messages = self.api(), Window(), [], []
        process = Process()
        process.pid = os.getpid()
        api.proc = process
        runtime = stop_request.RecordingStopRuntime(alive=True, browser_state='BORROWED')
        api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        gui._install_close_guard(window, api, confirm=lambda text: True,
                                 notify=messages.append, launch=tasks.append,
                                 wait=lambda seconds: self.fail('cleanup already finished'))
        rid = None

        def work():
            nonlocal rid
            self.clock.advance(5)
            stop_request.check()
            rid = api.get_run()['round_id']
            self.assertFalse(window.close())
            self.assertFalse(window.destroyed)
            self.assertEqual(api.get_result()['reason'], rounds.TerminalReason.ABANDONED.value)
            self.assertFalse(window.close())  # 不叠确认或第二个停止任务。
            self.assertEqual(len(tasks), 1)
            self.assertFalse(api.start_run([])['ok'])
            self.clock.advance(7)  # 中止请求不提前结账。
            stop_request.check()

        def cleanup(*args, **kwargs):
            self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))
            self.assertFalse(window.destroyed)  # 计时保存不等于归属清理完成。
            self.clock.advance(30)
            process.alive = runtime.alive = False

        self.collect(work, cleanup)
        tasks.pop()()
        self.assertTrue(window.destroyed)
        self.assertEqual(messages, [])
        self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))
        self.assertEqual(api.get_result()['duration_text'], '12 秒')

    def test_cancel_or_dismiss_close_keeps_collecting_and_repeated_refresh_does_not_settle(self):
        for answer in (False, None):
            with self.subTest(answer=answer):
                # 每个例子独立数据库，避免前一终态影响新轮选择。
                self.path = self.root / f'cancel-{answer}.db'
                self.cfg.db_file = self.path
                api, window, tasks = self.api(), Window(), []
                process = Process()
                process.pid = os.getpid()
                api.proc = process
                gui._install_close_guard(window, api, confirm=lambda text: answer,
                                         notify=lambda text: self.fail(text), launch=tasks.append)
                rid = None

                def work():
                    nonlocal rid
                    self.clock.advance(5)
                    stop_request.check()
                    rid = api.get_run()['round_id']
                    for _ in range(2):
                        self.assertFalse(window.close())
                        self.assertFalse(window.destroyed)
                        self.assertIsNone(api.get_result()['reason'])
                        self.assertEqual(self.fact(rid), elapsed.Fact(5, 'recorded'))
                    self.clock.advance(7)
                    stop_request.check()
                    self.assertEqual(api.get_run()['elapsed_sec'], 12)
                    self.assertEqual(tasks, [])

                self.collect(work)
                self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))
    def test_real_cli_main_and_gui_takeover_share_saved_round_without_starting_another_segment(self):
        config = self.root / 'config.toml'
        # 试采只对纯汇总机开放（ADR-0049 修订段）：这条跨入口用例按真机口径跑。
        config.write_text(MINIMAL_CONFIG + 'role = "merge_only"\n', encoding='utf-8')
        rid = None
        db_path = None

        def browser(cfg, **kwargs):
            nonlocal rid, db_path
            db_path = cfg.db_file
            self.clock.advance(5)
            stop_request.check()
            api = self.api(db_path)
            rid = api.get_run()['round_id']
            self.assertEqual(api.get_result()['elapsed_sec'], 5)
            self.assertEqual(self.fact(rid, db_path), elapsed.Fact(5, 'recorded'))
            self.clock.advance(7)
            stop_request.check()
            self.assertEqual(self.api(db_path).get_run()['elapsed_sec'], 12)
            raise guard.RoundPauseRequired('CLI adapter：人工介入后暂停')

        # 只替换计时module的OS时钟边界，不全局替换采集入口的time模块。
        from types import SimpleNamespace
        with patch.object(run, 'ROOT', self.root), \
                patch.object(sys, 'argv', ['run.py', '--config', str(config), '--local-trial',
                                         '--limit-shops', 'A01']), \
                patch.object(elapsed, 'time', SimpleNamespace(monotonic=self.clock)), \
                patch.object(browser_pw, 'open_session', side_effect=browser), \
                patch.object(browser_pw, 'close_session'):
            self.assertEqual(run.main(), 0)
        self.assertEqual(self.fact(rid, db_path), elapsed.Fact(12, 'complete'))
        reopened = self.api(db_path)
        self.assertEqual(reopened.get_run()['elapsed_sec'], 12)
        self.assertEqual(reopened.get_result()['duration_text'], '12 秒')
        self.assertFalse((self.root / 'bestseller.db').exists())

    def test_confirming_explicitly_paused_round_preserves_saved_duration_and_excludes_wait(self):
        api = self.api()
        process = Process()
        process.pid = os.getpid()
        api.proc = process
        runtime = stop_request.RecordingStopRuntime(alive=True, browser_state='BORROWED')
        api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        rid = None

        def work():
            nonlocal rid
            self.clock.advance(5)
            stop_request.check()
            rid = api.get_run()['round_id']
            self.assertTrue(api.pause_run()['ok'])
            self.clock.advance(7)
            stop_request.check()

        self.collect(work)
        process.alive = runtime.alive = False
        self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))
        self.clock.advance(600)
        self.assertEqual(api.close_snapshot().pause_phase, 'paused')
        window, tasks, prompts = Window(), [], []
        gui._install_close_guard(window, api, confirm=lambda text: prompts.append(text) or True,
                                 notify=lambda text: self.fail(text), launch=tasks.append,
                                 wait=lambda seconds: self.fail('already paused'))
        self.assertFalse(window.close())
        self.assertEqual(api.get_result()['reason'], rounds.TerminalReason.ABANDONED.value)
        tasks.pop()()
        self.assertTrue(window.destroyed)
        self.assertIn('本轮已暂停', prompts[0])
        self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))
    def test_pause_stopping_or_cleanup_close_uses_same_settlement_not_a_new_timer(self):
        for phase in ('stopping', 'cleanup'):
            with self.subTest(phase=phase):
                self.path = self.root / f'pause-{phase}.db'
                self.cfg.db_file = self.path
                api, window, tasks, prompts = self.api(), Window(), [], []
                process = Process()
                process.pid = os.getpid()
                api.proc = process
                runtime = stop_request.RecordingStopRuntime(alive=True, browser_state='BORROWED')
                api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
                gui._install_close_guard(window, api, confirm=lambda text: prompts.append(text) or True,
                                         notify=lambda text: self.fail(text), launch=tasks.append,
                                         wait=lambda seconds: self.fail('cleanup already finished'))
                rid = None

                def work():
                    nonlocal rid
                    self.clock.advance(5)
                    stop_request.check()
                    rid = api.get_run()['round_id']
                    self.assertTrue(api.pause_run()['ok'])
                    if phase == 'stopping':
                        self.assertFalse(window.close())
                    self.clock.advance(7)
                    stop_request.check()

                def cleanup(*args, **kwargs):
                    self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))
                    if phase == 'cleanup':
                        process.alive = False
                        runtime.fail_close = True
                        runtime.browser_state = 'OWNED'
                        runtime.browser = stop_request.BoundBrowser(9501, 9222, 'owned-proof')
                        api.close_snapshot()  # 消费暂停ACK，开始原有收尾窗口。
                        self.clock.advance(20)
                        self.assertEqual(api.close_snapshot().pause_phase, 'cleanup')
                        self.assertFalse(window.close())
                        runtime.fail_close = False
                    self.assertFalse(window.destroyed)
                    self.clock.advance(30)
                    process.alive = runtime.alive = False

                self.collect(work, cleanup)
                tasks.pop()()
                self.assertTrue(window.destroyed)
                self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))
                self.assertEqual(api.get_result()['reason'], rounds.TerminalReason.ABANDONED.value)
                self.assertIn('正在暂停' if phase == 'stopping' else '暂停收尾', prompts[0])

    def test_existing_abort_close_does_not_reconfirm_or_credit_another_segment(self):
        api, window, tasks = self.api(), Window(), []
        process = Process()
        process.pid = os.getpid()
        api.proc = process
        runtime = stop_request.RecordingStopRuntime(alive=True, browser_state='BORROWED')
        api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        gui._install_close_guard(window, api, confirm=lambda text: self.fail('already aborting'),
                                 notify=lambda text: self.fail(text), launch=tasks.append,
                                 wait=lambda seconds: self.fail('already stopped'))
        rid = None

        def work():
            nonlocal rid
            self.clock.advance(5)
            stop_request.check()
            rid = api.get_run()['round_id']
            self.assertTrue(api.abort_run()['ok'])
            self.assertFalse(window.close())
            self.clock.advance(7)
            stop_request.check()

        def cleanup(*args, **kwargs):
            process.alive = runtime.alive = False
            self.clock.advance(30)

        self.collect(work, cleanup)
        tasks.pop()()
        self.assertTrue(window.destroyed)
        self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))

    def test_replaced_close_target_and_late_old_settlement_do_not_mutate_new_round(self):
        api, window, tasks = self.api(), Window(), []
        old_rid, new_rid, replacement = None, None, None

        def replace_target(text):
            nonlocal new_rid, replacement
            conn = connect(self.path)
            try:
                db = Database(conn)
                rounds.finish_if_open(db, rounds.load(db, old_rid), rounds.TerminalReason.COMPLETED)
                new_rid = new_round(db, 'A01')
                started = db.record_crawler_process(os.getpid(), new_rid, process_os_started='adapter-proof')
                replacement = elapsed.begin(conn, new_rid, process_started=started,
                                            new_round=True, clock=self.clock)
                self.clock.advance(3)
                replacement.checkpoint()
                # 生产段还会保存，所以连接必须保留到最后。
                self.addCleanup(conn.close)
            except BaseException:
                conn.close()
                raise
            return True

        gui._install_close_guard(window, api, confirm=replace_target,
                                 notify=lambda text: None, launch=tasks.append)

        def work():
            nonlocal old_rid
            self.clock.advance(5)
            stop_request.check()
            old_rid = api.get_run()['round_id']
            self.assertFalse(window.close())
            self.assertFalse(window.destroyed)
            self.assertEqual(tasks, [])
            self.assertEqual(self.fact(new_rid), elapsed.Fact(3, 'recorded'))
            # 原执行后到的check/finally只能更新原段。
            stop_request.check()

        self.collect(work)
        self.assertEqual(self.fact(old_rid), elapsed.Fact(8, 'complete'))
        self.assertEqual(self.fact(new_rid).seconds, 3)
        replacement.settle()
        self.assertEqual(self.fact(new_rid), elapsed.Fact(3, 'complete'))
        for _ in range(2):
            self.assertEqual(api.get_run()['round_id'], new_rid)
            self.assertEqual(api.get_result()['elapsed_sec'], 3)
            self.assertIsNone(api.get_result()['reason'])

    def test_stale_gui_round_is_replaced_on_takeover_but_explicit_history_keeps_own_fact(self):
        conn = connect(self.path)
        try:
            db = Database(conn)
            old = new_round(db, 'A01', run_date='2026-10-07')
            segment = elapsed.begin(conn, old, process_started='historical', new_round=True, clock=self.clock)
            self.clock.advance(120)
            segment.settle()
            rounds.finish_if_open(db, rounds.load(db, old), rounds.TerminalReason.COMPLETED)
        finally:
            conn.close()
        api = self.api()
        api.round_id = old  # 沿用原GUI helper的会话事实设置，不是私有计时字段。
        current = None

        def work():
            nonlocal current
            self.clock.advance(5)
            stop_request.check()
            current = api.get_run()['round_id']
            self.assertNotEqual(current, old)
            self.assertEqual(api.get_result()['elapsed_sec'], 5)
            conn = connect(self.path)
            try:
                history = views.result_view(conn, state=views.UiState(round_id=old), now=utcnow())
                self.assertEqual((history['round_id'], history['duration_text']), (old, '2 分'))
            finally:
                conn.close()

        self.collect(work)
        self.assertEqual(self.fact(old), elapsed.Fact(120, 'complete'))
        self.assertEqual(self.fact(current), elapsed.Fact(5, 'complete'))
    def test_formal_cli_main_uses_same_elapsed_owner_and_reopened_gui_reads_its_round(self):
        config = self.root / 'config.toml'
        config.write_text(MINIMAL_CONFIG, encoding='utf-8')
        formal = Config.from_file(config, root=self.root)
        rid = None

        def browser(cfg, **kwargs):
            nonlocal rid
            self.assertEqual(cfg.db_file, formal.db_file)
            self.clock.advance(5)
            stop_request.check()
            api = self.api(formal.db_file)
            rid = api.get_run()['round_id']
            self.assertEqual(api.get_result()['elapsed_sec'], 5)
            self.clock.advance(7)
            stop_request.check()
            raise guard.RoundPauseRequired('CLI adapter：保留同轮续跑')

        from types import SimpleNamespace
        with patch.object(run, 'ROOT', self.root), \
                patch.object(sys, 'argv', ['run.py', '--config', str(config), '--ignore-plan',
                                         '--limit-shops', 'A01']), \
                patch.object(elapsed, 'time', SimpleNamespace(monotonic=self.clock)), \
                patch.object(browser_pw, 'open_session', side_effect=browser), \
                patch.object(browser_pw, 'close_session'):
            self.assertEqual(run.main(), 0)
        self.assertEqual(self.fact(rid, formal.db_file), elapsed.Fact(12, 'complete'))
        reopened = self.api(formal.db_file)
        self.assertEqual(reopened.get_run()['elapsed_sec'], 12)
        self.assertEqual(reopened.get_result()['duration_text'], '12 秒')
    def test_natural_completion_during_confirmation_preserves_real_terminal_and_duration(self):
        api, window, tasks = self.api(), Window(), []
        process = Process()
        process.pid = os.getpid()
        api.proc = process
        runtime = stop_request.RecordingStopRuntime(alive=True, browser_state='BORROWED')
        api._stop_watch = stop_request.StopWatch(runtime, now=self.clock)
        rid = None

        def confirm(text):
            self.clock.advance(7)
            conn = connect(self.path)
            try:
                rounds.finish_if_open(Database(conn), rounds.load(Database(conn), rid),
                                      rounds.TerminalReason.COMPLETED)
            finally:
                conn.close()
            return True

        gui._install_close_guard(window, api, confirm=confirm,
                                 notify=lambda text: self.fail(text), launch=tasks.append,
                                 wait=lambda seconds: self.fail('already stopped'))

        def work():
            nonlocal rid
            self.clock.advance(5)
            stop_request.check()
            rid = api.get_run()['round_id']
            self.assertFalse(window.close())
            self.assertFalse(window.destroyed)
            stop_request.check()

        def cleanup(*args, **kwargs):
            self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))
            self.clock.advance(30)
            process.alive = runtime.alive = False

        self.collect(work, cleanup)
        tasks.pop()()
        self.assertTrue(window.destroyed)
        self.assertEqual(api.get_result()['reason'], rounds.TerminalReason.COMPLETED.value)
        self.assertEqual(self.fact(rid), elapsed.Fact(12, 'complete'))

    def test_reopened_pages_agree_on_legacy_unknown_interruption_and_completed_facts(self):
        for scenario, expected in (('legacy', elapsed.Fact(None, 'unrecorded')),
                                   ('legacy-resumed', elapsed.Fact(5, 'lower_bound')),
                                   ('interrupted', elapsed.Fact(5, 'lower_bound')),
                                   ('completed', elapsed.Fact(5, 'complete'))):
            with self.subTest(scenario=scenario):
                path = self.root / f'{scenario}.db'
                conn = connect(path)
                try:
                    rid = new_round(Database(conn), 'A01')
                    if scenario != 'legacy':
                        segment = elapsed.begin(conn, rid, process_started=scenario,
                                                new_round=scenario != 'legacy-resumed', clock=self.clock)
                        self.clock.advance(5)
                        segment.checkpoint()
                        if scenario != 'interrupted':
                            segment.settle()
                        else:
                            self.clock.advance(80)  # 无finally adapter；观察延迟不补算。
                finally:
                    conn.close()
                api = self.api(path)
                for _ in range(2):
                    progress = api.get_run()
                    result = api.get_result()
                    self.assertEqual(self.fact(rid, path), expected)
                    self.assertEqual(progress['elapsed_sec'], expected.seconds)
                    self.assertEqual(progress['elapsed_text'], expected.text)
                    self.assertEqual(result['duration_text'], expected.text)
                    self.assertEqual(progress['elapsed_quality'], result['elapsed_quality'])
                    self.assertEqual(progress['elapsed_note'], result['elapsed_note'])