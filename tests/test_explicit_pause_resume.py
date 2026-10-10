"""Ticket 03: explicit page pause, same-round resume, real scheduling."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from contextlib import closing
import gui
from bestseller_monitor import rounds, pipeline, single_instance
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, connect, cst_date
from frozen_clock import frozen_clock
from helpers import crawler_cfg, isolated_locks
from test_gui import gui_api, store_today_plan, StopClock

class PauseResumeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(frozen_clock())
        self.enterContext(isolated_locks())
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.temp / 'round.db'
        self.shops = [Shop(k, 'original-'+k, f'https://{k}.example/') for k in 'ABCD']
        store_today_plan(self.path, *((k, 'm-test', 1) for k in 'ABCD'))
        self.clock = StopClock()
        self.api = gui_api(shops=self.shops, db_path=self.path, stop_clock=self.clock)
        with closing(connect(self.path)) as conn:
            db = Database(conn)
            self.rid = rounds.open(db, rounds.RoundRequest(cst_date(), tuple(
                rounds.ShopScope(s.key,s.url,s.name) for s in self.shops))).round.id
            db.record_crawler_process(4321,self.rid,process_os_started='test-proof',browser_state='NOT_STARTED')
        self.proc = MagicMock(pid=4321)
        self.proc.poll.return_value = None
        self.api.proc = self.proc
        self.api.round_id = self.rid
        self.lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(self.lock.release)
    def settle(self):
        self.proc.poll.return_value = 0
        self.lock.release()
        self.assertTrue(self.api.get_run()['can_resume'])
    def page(self,browser=None):
        from playwright.sync_api import sync_playwright
        if browser is None:
            pw=sync_playwright().start(); self.addCleanup(pw.stop)
            browser=pw.chromium.launch(channel='msedge',headless=True); self.addCleanup(browser.close)
        context=browser.new_context(); self.addCleanup(context.close)
        context.route('https://**/*',lambda route: route.abort())
        context.route('http://**/*',lambda route: route.abort())
        page=context.new_page()
        page.expose_function('ticketApi',lambda name,args: getattr(self.api,name)(*args))
        page.add_init_script("""window.pywebview={api:new Proxy({}, {get:(o,n)=>n in o ? o[n] : (...a)=>window.ticketApi(n,a)})};window.alert=()=>{};""")
        # file navigation has no network; all HTTP(S) requests are aborted.
        page.goto((Path(gui.PROJECT_ROOT)/'bestseller_monitor/pages/ui_live.html').as_uri())
        page.locator('input[data-i]').first.wait_for()
        page.evaluate("switchTab('run'); tickRun()")
        return page
    def command_intent(self, spawn):
        cmd=spawn.call_args.args[0]
        self.assertIn('--resume-round-intent',cmd)
        return rounds.ResumeRoundIntent.from_json(cmd[cmd.index('--resume-round-intent')+1])
    def test_page_pause_cleanup_then_continue_carries_frozen_complete_round(self):
        from playwright.sync_api import expect
        page=self.page()
        page.locator('#pauseBtn').click()
        self.assertFalse(self.api.resume_run()['ok'])
        self.assertFalse(self.api.get_run()['can_resume'])
        self.settle()
        page.evaluate('tickRun()')
        expect(page.locator('#resumeBtn')).to_be_visible()
        with patch.object(gui.subprocess,'Popen') as spawn:
            page.locator('#resumeBtn').click()
            page.wait_for_timeout(100)
        intent=self.command_intent(spawn)
        self.assertEqual(intent.round_id,self.rid)
        self.assertEqual(intent.request.shop_keys,('A','B','C','D'))
        self.assertNotIn('--new-round-intent',spawn.call_args.args[0])



    def test_public_resume_rechecks_terminal_after_preflight_under_collection_lock(self):
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        with patch.object(gui.subprocess,'Popen') as spawn:
            self.assertTrue(self.api.resume_run()['ok'])
        intent=self.command_intent(spawn)
        def finish_at_lock():
            self.assertTrue(single_instance.is_held(single_instance.CRAWLER_LOCK))
            with closing(connect(self.path)) as conn:
                db=Database(conn)
                rounds.finish(db,rounds.load(db,self.rid),rounds.TerminalReason.IDENTITY_VERIFICATION_TIMEOUT)
        cfg=crawler_cfg(db_file=self.path,ensure_dirs=finish_at_lock)
        with patch('bestseller_monitor.browser_pw.open_session') as browser:
            with self.assertRaises(rounds.ScopeMismatch):
                pipeline.run_round(cfg,self.shops,resume_intent=intent)
            browser.assert_not_called()
        with closing(connect(self.path)) as conn:
            db=Database(conn)
            self.assertEqual(rounds.latest(db).id,self.rid)
            self.assertEqual(rounds.load(db,self.rid).reason,rounds.TerminalReason.IDENTITY_VERIFICATION_TIMEOUT)
            self.assertIsNone(db.crawler_process())

    def test_failed_resume_spawn_keeps_real_pause_retryable_and_does_not_create(self):
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        with patch.object(gui.subprocess,'Popen',side_effect=OSError('offline launch failed')):
            self.assertFalse(self.api.resume_run()['ok'])
        self.assertTrue(self.api.get_run()['can_resume'])
        with patch.object(gui.subprocess,'Popen') as spawn:
            self.assertTrue(self.api.resume_run()['ok'])
        self.assertEqual(self.command_intent(spawn).round_id,self.rid)
        with closing(connect(self.path)) as conn:
            self.assertEqual(rounds.latest(Database(conn)).id,self.rid)

    def test_child_browser_start_failure_keeps_original_pause_for_explicit_retry(self):
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        cfg=crawler_cfg(db_file=self.path,ensure_dirs=lambda:None)
        failed=MagicMock(pid=5432); failed.poll.return_value=2
        def child(cmd,**kw):
            intent=rounds.ResumeRoundIntent.from_json(cmd[cmd.index('--resume-round-intent')+1])
            with patch('bestseller_monitor.browser_pw.open_session',side_effect=OSError('offline browser unavailable')):
                with self.assertRaises(OSError):
                    pipeline.run_round(cfg,self.shops,resume_intent=intent)
            return failed
        with patch.object(gui.subprocess,'Popen',side_effect=child):
            self.assertTrue(self.api.resume_run()['ok'])
        self.assertTrue(self.api.get_run()['can_resume'])
        with closing(connect(self.path)) as conn:
            db=Database(conn)
            self.assertIsNone(rounds.load(db,self.rid).reason)
            self.assertIsNone(db.crawler_process())

    def test_four_shop_real_identity_pause_then_default_D_resumes_only_remaining_task(self):
        import os
        from playwright.sync_api import sync_playwright
        from bestseller_monitor import guard, waiting, browser_pw
        from helpers import insert_inventory_rows
        from test_identity_lifecycle import Clock, BUSINESS, FIXTURE
        with closing(connect(self.path)) as conn:
            db=Database(conn)
            insert_inventory_rows(conn,[(k,str(i),cst_date()) for k in 'ABC' for i in range(30)])
            for shop in self.shops:
                ids=['501','502'] if shop.key=='D' else [str(i) for i in range(30)]
                db.save_shop_offers(self.rid,shop.key,shop.url,shop.name,
                    [(i+1,offer,f'https://detail.1688.com/offer/{offer}.html','saved','1') for i,offer in enumerate(ids)],1)
        self.lock.release()
        self.proc.pid=os.getpid()
        pw=sync_playwright().start(); self.addCleanup(pw.stop)
        browser=pw.chromium.launch(channel='msedge',headless=True); self.addCleanup(browser.close)
        ctx=browser.new_context(java_script_enabled=False); self.addCleanup(ctx.close)
        detail_page=ctx.new_page()
        html=BUSINESS.replace('</body>','<script>{"skuInfoMap":{"red":{"skuId":"red","name":"red","price":10,"canBookCount":3}}}</script></body>')
        identity=FIXTURE.read_text(encoding='utf-8-sig')
        restored=[]; visits=[]
        def route(route):
            if not route.request.is_navigation_request(): route.abort(); return
            visits.append(route.request.url)
            route.fulfill(content_type='text/html; charset=utf-8',body=(identity if route.request.url.endswith('/502.html') and not restored else html))
        ctx.route('**/*',route)
        clock=Clock()
        self.enterContext(patch.object(guard,'time',clock)); self.enterContext(patch.object(waiting,'time',clock))
        def session(cfg,*,publish_browser):
            publish_browser('BORROWED',9222,None,None)
            return None,None,detail_page,ctx
        self.enterContext(patch.object(browser_pw,'open_session',side_effect=session))
        self.enterContext(patch.object(browser_pw,'close_session'))
        cfg=crawler_cfg(db_file=self.path,root=self.temp,data_dir=self.temp/'data',logs_dir=self.temp/'logs',
            raw_page_dir=self.temp/'raw',screenshot_dir=self.temp/'shots',user_data_path=self.temp/'profile',
            exchange_root=self.temp/'exchange',ensure_dirs=lambda:None,start_browser=False,
            human_pause_minutes=0.1,intervention_confirmation_sec=2)
        ui=self.page(browser); acted=[]
        def click_pause():
            if clock.now>=3 and not acted:
                ui.locator('#pauseBtn').click()
                acted.append(True)
                restored.append(True)
                detail_page.set_content(html)
        clock.on_sleep=click_pause
        pipeline.run_round(cfg,self.shops,elapsed_clock=clock.monotonic)
        self.assertEqual(acted,[True], 'new reproduction must really click pause')
        self.proc.poll.return_value=0
        self.assertTrue(self.api.get_run()['can_resume'])
        before=self.api.get_start()
        self.assertEqual([s['key'] for s in before['shops'] if s['default_checked']],['D'])
        self.assertEqual(visits,['https://detail.1688.com/offer/501.html','https://detail.1688.com/offer/502.html'])
        ui.evaluate('tickRun()')
        with patch.object(gui.subprocess,'Popen') as spawn:
            ui.locator('#resumeBtn').click(); ui.wait_for_timeout(100)
        intent=self.command_intent(spawn)
        self.assertEqual(intent.request.shop_keys,('A','B','C','D'))
        paused_visits = list(visits)
        # Remove A/B/C and replace D's configuration before real resumed scheduling.
        csv = self.temp / 'changed-shops.csv'
        csv.write_text('shop_key,shop_name,shop_url,pages,active,home_url,offer_list_url\n'
                       'D,replaced,https://new.example/,1,1,https://new.example/home,https://new.example/list\n', encoding='utf-8')
        cfg.shop_csv = csv
        visits.clear(); clock.on_sleep=lambda:None
        pipeline.run_round(cfg,[Shop(s.key,s.name,s.url) for s in intent.request.shops],resume_intent=intent)
        self.assertEqual(visits,['https://detail.1688.com/offer/502.html'])
        with closing(connect(self.path)) as conn:
            db=Database(conn)
            self.assertEqual(rounds.latest(db).id,self.rid)
            self.assertEqual(rounds.load(db,self.rid).shop_keys,('A','B','C','D'))
            self.assertEqual(rounds.load(db,self.rid).reason,rounds.TerminalReason.COMPLETED)
            self.assertEqual(db.round_tally(self.rid).handled,92)
        self.assertFalse(self.api.get_run()['can_resume'])
        import json
        print('TICKET03_REPRO ' + json.dumps({
            'explicit_pause_clicked': acted == [True], 'default_checked': ['D'],
            'round_id_before_and_after': self.rid, 'scope': list(intent.request.shop_keys),
            'before_pause_navigation': paused_visits, 'resumed_navigation': visits,
            'config_after_pause': csv.read_text(encoding='utf-8'),
            'handled': 92, 'reason': 'COMPLETED', 'offline': True}, ensure_ascii=False))

    def test_deleted_and_renamed_configuration_cannot_replace_original_round_addresses(self):
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        csv=self.temp/'shops.csv'
        csv.write_text('shop_key,shop_name,shop_url,pages,active,home_url,offer_list_url\n'
            'D,replaced,https://new.example/,1,1,https://new.example/home,https://new.example/list\n',encoding='utf-8')
        cfg=crawler_cfg(db_file=self.path,shop_csv=csv,ensure_dirs=lambda:None)
        with closing(connect(self.path)) as conn:
            shops=pipeline.round_shops(Database(conn),self.rid,cfg)
        self.assertEqual([(s.key,s.name,s.url) for s in shops],
            [(k,'original-'+k,f'https://{k}.example/') for k in 'ABCD'])
        self.assertIsNone(shops[-1].home_url, 'new configuration must not redirect original shop')
        self.assertIsNone(shops[-1].offer_list_url)

    def test_real_page_abort_cancel_is_inert_confirm_waits_then_explicit_new(self):
        # 票 04 删「改范围」后：唯一中止动线＝「中止本轮」→（等收尾）→ 结果页
        # 「返回开始」→ 改勾选 → 明确开始；不再有自动跳回开始页的机制。
        from playwright.sync_api import expect
        page=self.page()
        self.assertEqual(page.locator('#changeScopeBtn').count(),0, '「改范围」按钮与整链已删（票 04）')
        with patch.object(gui.subprocess,'Popen') as spawn:
            page.locator('#abortBtn').click()
            expect(page.locator('#confirmSub')).to_contain_text('不能继续')
            page.locator('#confirmCancel').click()
            with closing(connect(self.path)) as conn:
                self.assertIsNone(rounds.load(Database(conn),self.rid).reason)
            self.assertIsNone(self.api.get_run()['stopping'])
            spawn.assert_not_called()
            page.locator('#abortBtn').click(); page.locator('#confirmOk').click()
            expect(page.locator('#panel-result')).to_have_class('panel active')
            self.assertFalse(self.api.start_run(['D'])['ok'])
            self.assertFalse(self.api.resume_run()['ok'])
            spawn.assert_not_called()
            self.proc.poll.return_value=0; self.lock.release()
            page.evaluate('refreshResultStopping()')
            expect(page.locator('#resStopping')).to_be_hidden()
            # 收尾结束后留在结果页，不再自动跳回开始页（票 04 替代动线）
            expect(page.locator('#panel-result')).to_have_class('panel active')
            page.locator('#backBtn').click()
            expect(page.locator('#panel-start')).to_have_class('panel active')
            spawn.assert_not_called()
            with closing(connect(self.path)) as conn:
                self.assertEqual(rounds.load(Database(conn),self.rid).reason,rounds.TerminalReason.ABANDONED)
            expect(page.locator('#startBtn')).to_be_enabled()
            page.locator('#selNoneBtn').click()
            page.locator('input[data-i="3"]').check()
            page.locator('#startBtn').click(); page.wait_for_timeout(100)
            self.assertIsNotNone(spawn.call_args, self.api.get_start())
            cmd=spawn.call_args.args[0]
            intent=rounds.NewRoundIntent.from_json(cmd[cmd.index('--new-round-intent')+1])
            self.assertEqual(intent.request.shop_keys,('D',))
            self.assertNotIn('--resume-round-intent',cmd)


    def test_second_explicit_pause_is_not_consumed_by_previous_resume_ack(self):
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertTrue(self.api.resume_run()['ok'])
        intent = self.command_intent(spawn)
        child = spawn.return_value
        child.pid = 5432
        child.poll.return_value = None
        with closing(connect(self.path)) as conn:
            db = Database(conn)
            db.append_event(self.rid, 'resume_started', kind='run', note=intent.attempt)
            db.record_crawler_process(5432, self.rid, process_os_started='second', browser_state='NOT_STARTED')
        self.lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(self.lock.release)
        # No polling between child's execution acknowledgment and the next pause.
        self.assertTrue(self.api.pause_run()['ok'])
        child.poll.return_value = 0
        self.lock.release()
        self.assertTrue(self.api.get_run()['can_resume'])

    def test_resume_without_explicit_pause_and_after_terminal_is_refused(self):
        self.proc.poll.return_value = 0; self.lock.release()
        self.assertFalse(self.api.resume_run()['ok'])
        with closing(connect(self.path)) as conn:
            db = Database(conn)
            rounds.finish(db, rounds.load(db, self.rid), rounds.TerminalReason.IDENTITY_VERIFICATION_TIMEOUT)
        self.api.user_paused = True
        self.api._paused_round_id = self.rid
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertFalse(self.api.resume_run()['ok'])
            spawn.assert_not_called()

    def test_public_resume_rejects_changed_scope_cross_day_and_live_cleanup(self):
        from dataclasses import replace
        from datetime import datetime, timedelta
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        with closing(connect(self.path)) as conn:
            intent = rounds.plan_resume(Database(conn), self.rid, now=self.api._now())
        cfg = crawler_cfg(db_file=self.path, ensure_dirs=lambda: None)
        wrong = replace(intent, request=replace(intent.request, shops=intent.request.shops[-1:]))
        with patch('bestseller_monitor.browser_pw.open_session') as browser:
            with self.assertRaises(rounds.ScopeMismatch):
                pipeline.run_round(cfg, self.shops[-1:], resume_intent=wrong)
            with patch.object(pipeline, "utcnow", return_value=(datetime.fromisoformat(self.api._now()) + timedelta(days=1)).isoformat()):
                with self.assertRaises(rounds.ScopeMismatch):
                    pipeline.run_round(cfg, self.shops, resume_intent=intent)
            with closing(connect(self.path)) as conn:
                Database(conn).record_crawler_process(7654, self.rid, process_os_started='unfinished', browser_state='NOT_STARTED')
            with self.assertRaises(rounds.ScopeMismatch):
                pipeline.run_round(cfg, self.shops, resume_intent=intent)
            browser.assert_not_called()

    def test_child_rejection_is_visible_on_pause_page_and_retry_is_explicit(self):
        from playwright.sync_api import expect
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        page = self.page()
        child = MagicMock(pid=5432)
        child.poll.return_value = 3
        with patch.object(gui.subprocess, 'Popen', return_value=child) as spawn:
            page.locator('#resumeBtn').click()
            page.evaluate('tickRun()')
            expect(page.locator('#panel-run')).to_have_class('panel active')
            expect(page.locator('#rPaused')).to_contain_text('启动')
            self.assertTrue(self.api.get_run()['can_resume'])
            page.wait_for_timeout(100)
            self.assertEqual(spawn.call_count, 1, 'polling does not automatically retry or create')

    def test_consumed_resume_attempt_cannot_be_replayed(self):
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        with closing(connect(self.path)) as conn:
            db = Database(conn)
            intent = rounds.plan_resume(db, self.rid, now=self.api._now())
            db.append_event(self.rid, 'resume_started', kind='run', note=intent.attempt)
        cfg = crawler_cfg(db_file=self.path, ensure_dirs=lambda: None)
        with patch('bestseller_monitor.browser_pw.open_session') as browser:
            with self.assertRaises(rounds.ScopeMismatch):
                pipeline.run_round(cfg, self.shops, resume_intent=intent)
            browser.assert_not_called()

    def test_public_cli_resume_carries_snapshot_and_refuses_terminal_without_fallback(self):
        import sys
        import run
        from test_run_cli import MINIMAL_CONFIG
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertTrue(self.api.resume_run()['ok'])
        intent = self.command_intent(spawn)
        config = self.temp / 'config.toml'
        config.write_text(MINIMAL_CONFIG.replace('bestseller.db', 'round.db').replace('"m1"', '"m-test"'), encoding='utf-8')
        (self.temp / 'shops.csv').write_text(
            'shop_key,shop_name,shop_url,pages,active,offer_list_url\n'
            'D,replaced,https://new.example/,1,1,\n', encoding='utf-8')
        argv = ['run.py', '--config', str(config), '--ignore-plan', '--resume-round-intent', intent.to_json()]
        with patch.object(run, 'ROOT', self.temp), patch.object(sys, 'argv', argv):
            with patch('bestseller_monitor.browser_pw.open_session', side_effect=OSError('offline unavailable')) as browser:
                with self.assertRaises(OSError):
                    run.main()
                browser.assert_called_once()
        with closing(connect(self.path)) as conn:
            db = Database(conn)
            self.assertEqual(rounds.scope_shops(db, self.rid), intent.request.shops)
            rounds.finish(db, rounds.load(db, self.rid), rounds.TerminalReason.IDENTITY_VERIFICATION_TIMEOUT)
        with patch.object(run, 'ROOT', self.temp), patch.object(sys, 'argv', argv):
            with patch('bestseller_monitor.browser_pw.open_session') as browser:
                self.assertNotEqual(run.main(), 0)
                browser.assert_not_called()
        with closing(connect(self.path)) as conn:
            self.assertEqual(rounds.latest(Database(conn)).id, self.rid)

    def test_actual_execution_consumes_pause_even_without_intermediate_poll(self):
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        child = MagicMock(pid=5432)
        child.poll.return_value = 0
        with patch.object(gui.subprocess, 'Popen', return_value=child) as spawn:
            self.assertTrue(self.api.resume_run()['ok'])
        intent = self.command_intent(spawn)
        with closing(connect(self.path)) as conn:
            Database(conn).append_event(self.rid, 'resume_started', kind='run', note=intent.attempt)
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertFalse(self.api.resume_run()['ok'])
            spawn.assert_not_called()

    def test_locked_resume_domain_uses_callers_clock_not_wall_clock(self):
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        with closing(connect(self.path)) as conn:
            intent = rounds.plan_resume(Database(conn), self.rid, now=self.api._now())
        cfg = crawler_cfg(db_file=self.path, ensure_dirs=lambda: None)
        with patch.object(rounds, 'utcnow', side_effect=AssertionError('domain reads wall clock')):
            with patch('bestseller_monitor.browser_pw.open_session', side_effect=OSError('offline unavailable')):
                with self.assertRaises(OSError):
                    pipeline.run_round(cfg, self.shops, resume_intent=intent)

    def test_locked_resume_refuses_replacement_round_after_gui_preflight(self):
        self.assertTrue(self.api.pause_run()['ok']); self.settle()
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertTrue(self.api.resume_run()['ok'])
        intent = self.command_intent(spawn)
        replacement = []
        def replace_at_lock():
            self.assertTrue(single_instance.is_held(single_instance.CRAWLER_LOCK))
            with closing(connect(self.path)) as conn:
                db = Database(conn)
                rounds.finish(db, rounds.load(db, self.rid), rounds.TerminalReason.ABANDONED)
                replacement.append(rounds.open(db, intent.request).round.id)
        cfg = crawler_cfg(db_file=self.path, ensure_dirs=replace_at_lock)
        with patch('bestseller_monitor.browser_pw.open_session') as browser:
            with self.assertRaises(rounds.ScopeMismatch):
                pipeline.run_round(cfg, self.shops, resume_intent=intent)
            browser.assert_not_called()
        with closing(connect(self.path)) as conn:
            db = Database(conn)
            self.assertEqual(rounds.latest(db).id, replacement[0])
            self.assertIsNone(rounds.load(db, replacement[0]).reason)
            self.assertEqual(rounds.load(db, self.rid).reason, rounds.TerminalReason.ABANDONED)
