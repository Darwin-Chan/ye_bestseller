"""Ticket 04: shipped start-page events, real API/config/views/SQLite, OS seam only."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gui
from bestseller_monitor.config import Config
from bestseller_monitor.db import connect
from helpers import isolated_locks


class BrowserOS:
    def __init__(self):
        self.state = 'free'
        self.starts = []
    def config_error(self, settings):
        from bestseller_monitor.dedicated_browser import BrowserRuntime
        return BrowserRuntime().config_error(settings)
    def observe(self, settings):
        return SimpleNamespace(state=self.state, message='测试 OS 事实：'+self.state)
    def launch(self, settings):
        self.starts.append(settings)
        self.state = 'occupied'
        return SimpleNamespace(pid=123, poll=lambda: 0)


class ManualEdgeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_locks())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())) / '迁移 部署目录'
        self.tmp.mkdir()
        (self.tmp/'config').mkdir()
        (self.tmp/'Edge 浏览器.exe').touch()
        text = (Path(gui.PROJECT_ROOT)/'config/config.example.toml').read_text('utf-8')
        text = text.replace('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'Edge 浏览器.exe')
        text = text.replace('alarm_on_intervention = true', 'alarm_on_intervention = false')
        self.config = self.tmp/'config/config.toml'
        self.config.write_text(text, 'utf-8')
        self.cfg = Config.from_file(self.config, root=self.tmp)
        self.cfg.shop_csv.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.shop_csv.write_text('shop_key,shop_name,shop_url,pages,active\nA,测试,https://a.example/,1,1\n', 'utf-8')
        self.os = BrowserOS()
        self.api = gui.Api(config_file=self.config, browser_runtime=self.os, now=lambda: '2026-10-09T04:00:00+00:00')
    def page(self):
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start(); self.addCleanup(pw.stop)
        browser = pw.chromium.launch(channel='msedge', headless=True); self.addCleanup(browser.close)
        ctx = browser.new_context(); self.addCleanup(ctx.close)
        ctx.route('**/*', lambda r: r.continue_() if r.request.url.startswith('file:') else r.abort())
        page = ctx.new_page()
        page.expose_function('ticketApi', lambda name,args: getattr(self.api,name)(*args))
        page.add_init_script("window.pywebview={api:new Proxy({}, {get:(_,n)=>(...a)=>window.ticketApi(n,a)})};window.alert=()=>{};")
        page.goto((Path(gui.PROJECT_ROOT)/'bestseller_monitor/pages/ui_live.html').as_uri())
        page.locator('#loginBrowserBtn').wait_for()
        page.wait_for_function("document.querySelector('#browserFacts').textContent.length > 0")
        return page
    def test_page_login_does_not_collect_or_create_round(self):
        page = self.page()
        page.click('#loginBrowserBtn')
        page.wait_for_function("document.querySelector('#browserFacts').textContent.includes('占用')")
        self.assertEqual(len(self.os.starts), 1)
        self.assertIsNone(self.api.proc)
        view = self.api.get_start()
        self.assertEqual(view['summary']['rounds'], 0)
        self.assertEqual(view['browser']['profile'], str(self.tmp/'profiles/account1_edge'))
        self.assertNotIn('账号已登录', page.locator('#browserFacts').inner_text())
        page.click('#refreshBrowserBtn')
        self.assertEqual(len(self.os.starts), 1)
    def store_plan(self):
        from contextlib import closing
        from bestseller_monitor.db import Database
        from bestseller_monitor import weekly_plan
        from helpers import store_weekly_plan
        with closing(connect(self.cfg.db_file)) as conn:
            store_weekly_plan(Database(conn), weekly_plan.week_label('2026-10-09'), ('A','m1',1))
    def test_closed_launcher_with_background_profile_blocks_start(self):
        self.store_plan()
        self.assertTrue(self.api.open_login_browser()['ok'])
        self.assertFalse(self.api.get_start()['start_allowed'])
        with patch('gui.subprocess.Popen') as spawn:
            result = self.api.start_run(['A'])
            self.assertFalse(result['ok'])
            self.assertIn('关闭', result['error'])
            spawn.assert_not_called()
        self.os.state = 'free'
        self.assertTrue(self.api.get_start()['start_allowed'])
        with patch('gui.subprocess.Popen') as spawn:
            self.assertTrue(self.api.start_run(['A'])['ok'])
            args, options = spawn.call_args
            self.assertEqual(args[0][args[0].index('--config')+1], str(self.config))
            self.assertEqual(options['cwd'], str(self.tmp))
            self.assertIn('--gui-browser-settings', args[0])
    def test_os_profile_and_unrelated_port_are_not_taken_over(self):
        import json
        from bestseller_monitor.dedicated_browser import BrowserRuntime
        runtime = BrowserRuntime()
        api = gui.Api(config_file=self.config, browser_runtime=runtime)
        evidence = {'processes':[{'name':'msedge.exe','pid':22,'parent':1,
                    'cmd': 'msedge.exe "--user-data-dir='+str(self.cfg.user_data_path)+'"'}], 'ports':[]}
        with patch('bestseller_monitor.dedicated_browser.subprocess.run', return_value=SimpleNamespace(returncode=0,stdout=json.dumps(evidence).encode('utf-8'))), patch('bestseller_monitor.dedicated_browser.subprocess.Popen') as spawn:
            result = api.open_login_browser()
            self.assertTrue(result['ok'])
            self.assertEqual(result['browser']['state'], 'occupied')
            spawn.assert_not_called()
        evidence['processes'] = []
        evidence['ports'] = [{'port':9222,'pid':99}]
        with patch('bestseller_monitor.dedicated_browser.subprocess.run', return_value=SimpleNamespace(returncode=0,stdout=json.dumps(evidence).encode('utf-8'))), patch('bestseller_monitor.dedicated_browser.subprocess.Popen') as spawn:
            self.assertFalse(api.open_login_browser()['ok'])
            self.assertEqual(api.get_start()['browser']['state'], 'port_busy')
            spawn.assert_not_called()
    def test_unconfirmed_creation_does_not_allow_second_launch_or_collection(self):
        self.store_plan()
        self.os.launch = lambda settings: self.os.starts.append(settings)
        self.assertTrue(self.api.open_login_browser()['ok'])
        self.assertFalse(self.api.get_start()['start_allowed'])
        self.api.open_login_browser()
        self.assertEqual(len(self.os.starts), 1)
        with patch('gui.subprocess.Popen') as spawn:
            self.assertFalse(self.api.start_run(['A'])['ok'])
            spawn.assert_not_called()
    def test_cli_rechecks_frozen_config_without_creating_round(self):
        import run
        from bestseller_monitor.dedicated_browser import Settings
        snapshot = Settings.of(self.cfg, self.config).to_json()
        self.config.write_text(self.config.read_text('utf-8').replace('attach_port = 9222','attach_port = 9333'),'utf-8')
        with patch('sys.argv', ['run.py','--config',str(self.config),'--gui-browser-settings',snapshot]), patch('bestseller_monitor.dedicated_browser.BrowserRuntime.observe', return_value=SimpleNamespace(state='free',message='free')):
            self.assertEqual(run.main(), 2)
        self.assertEqual(self.api.get_start()['summary']['rounds'], 0)
        self.assertIn('配置', self.api.open_login_browser()['error'])
    def test_repeated_concurrent_open_and_read_only_refresh(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.api.open_login_browser())) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(len(self.os.starts), 1)
        for _ in range(3): self.api.get_start()
        self.assertEqual(len(self.os.starts), 1)
        reopened = gui.Api(config_file=self.config, browser_runtime=self.os)
        self.assertTrue(reopened.open_login_browser()['reused'])
        self.assertEqual(len(self.os.starts), 1)
    def test_unknown_permission_and_missing_edge_refuse_api_and_page(self):
        self.store_plan()
        self.os.state = 'unknown'
        page = self.page()
        self.assertTrue(page.locator('#loginBrowserBtn').is_disabled())
        self.assertTrue(page.locator('#startBtn').is_disabled())
        self.assertFalse(self.api.open_login_browser()['ok'])
        with patch('gui.subprocess.Popen') as spawn:
            self.assertFalse(self.api.start_run(['A'])['ok'])
            spawn.assert_not_called()
        self.os.state = 'free'
        (self.tmp/'Edge 浏览器.exe').unlink()
        self.assertIn('未找到 Edge', self.api.open_login_browser()['error'])
        self.assertEqual(len(self.os.starts), 0)
    def test_false_mode_and_invalid_config_have_accurate_errors(self):
        self.store_plan()
        self.config.write_text(self.config.read_text('utf-8').replace('start_browser = true','start_browser = false'),'utf-8')
        api = gui.Api(config_file=self.config, browser_runtime=self.os)
        self.assertIn('start_browser=true', api.open_login_browser()['error'])
        self.assertIn('start_browser=true', api.start_run(['A'])['error'])
        self.config.write_text('invalid = [', 'utf-8')
        self.assertIn('配置错误', api.open_login_browser()['error'])
        self.assertFalse(api.get_start()['browser']['can_start'])
    def test_os_failure_or_unreadable_process_or_directory_is_unknown(self):
        import json
        from bestseller_monitor.dedicated_browser import BrowserRuntime
        runtime = BrowserRuntime()
        api = gui.Api(config_file=self.config, browser_runtime=runtime)
        for response in [SimpleNamespace(returncode=1,stdout=b''),
                SimpleNamespace(returncode=0,stdout=json.dumps({'processes':[{'name':'msedge.exe','pid':1,'parent':0,'cmd':None}], 'ports':[]}).encode())]:
            with patch('bestseller_monitor.dedicated_browser.subprocess.run',return_value=response):
                self.assertEqual(api.get_start()['browser']['state'], 'unknown')
                self.assertFalse(api.open_login_browser()['ok'])
        self.cfg.user_data_path.mkdir(parents=True)
        (self.cfg.user_data_path/'SingletonLock').touch()
        with patch('bestseller_monitor.dedicated_browser.subprocess.run') as query:
            self.assertFalse(api.open_login_browser()['ok'])
            query.assert_not_called()
    def test_gui_close_does_not_close_manual_window(self):
        self.api.open_login_browser()
        snapshot = self.api.close_snapshot()
        self.assertEqual(snapshot.kind, 'idle')
        self.assertTrue(self.api.begin_close(snapshot)['ok'])
        self.assertTrue(self.api.close_ready(snapshot))
        self.assertEqual(self.os.state, 'occupied')
        self.assertFalse(self.api.open_login_browser()['ok'])
        self.assertFalse(self.api.start_run(['A'])['ok'])
        page = self.page()
        self.assertTrue(page.locator('#loginBrowserBtn').is_disabled())
        self.assertTrue(page.locator('#startBtn').is_disabled())
    def test_role_disallows_entry_api_and_page(self):
        self.config.write_text(self.config.read_text('utf-8').replace('role = "collector"','role = "merge_only"'),'utf-8')
        self.api = gui.Api(config_file=self.config, browser_runtime=self.os)
        page = self.page()
        self.assertTrue(page.locator('#loginBrowserBtn').is_disabled())
        self.assertTrue(page.locator('#startBtn').is_disabled())
        self.assertIn('纯汇总机', self.api.open_login_browser()['error'])
        self.assertFalse(self.api.start_run(['A'])['ok'])
    def test_collecting_and_stop_cleanup_reject_api_and_page(self):
        from contextlib import closing
        from bestseller_monitor.db import Database
        from bestseller_monitor import rounds, single_instance
        from test_gui import StopClock
        self.store_plan()
        with closing(connect(self.cfg.db_file)) as conn:
            rid = rounds.open(Database(conn), rounds.RoundRequest('2026-10-09', (rounds.ShopScope('A','https://a.example/','测试'),))).round.id
            Database(conn).record_crawler_process(pid=4321,round_id=rid,process_os_started='proof',browser_state='NOT_STARTED')
        clock = StopClock()
        self.api = gui.Api(config_file=self.config,browser_runtime=self.os,stop_clock=clock,now=lambda:'2026-10-09T04:00:00+00:00')
        from unittest.mock import MagicMock
        proc = MagicMock(pid=4321); proc.poll.return_value = None
        self.api.proc = proc
        self.api.round_id = rid
        page = self.page()
        for label in ('collecting',):
            page.evaluate('refreshStart()')
            self.assertTrue(page.locator('#loginBrowserBtn').is_disabled(), label)
            self.assertFalse(self.api.open_login_browser()['ok'])
            self.assertFalse(self.api.start_run(['A'])['ok'])
        self.assertTrue(self.api.pause_run()['ok'])
        page.evaluate('refreshStart()')
        self.assertTrue(page.locator('#loginBrowserBtn').is_disabled())
        self.assertFalse(self.api.open_login_browser()['ok'])
        self.assertFalse(self.api.start_run(['A'])['ok'])
        self.assertEqual(len(self.os.starts),0)
        proc.poll.return_value = 0
        self.api.get_run()
        self.assertFalse(self.api.open_login_browser()['ok'])  # ticket 05 deliberately absent
    def test_moved_root_and_changed_cwd_use_one_resolved_contract(self):
        import os
        from bestseller_monitor.dedicated_browser import Settings
        old = Path.cwd()
        os.chdir(self.tmp.parent)
        try:
            api = gui.Api(config_file=self.config,browser_runtime=self.os)
            self.assertTrue(api.open_login_browser()['ok'])
            self.assertEqual(self.os.starts[0].executable, str(self.tmp/'Edge 浏览器.exe'))
            self.assertEqual(self.os.starts[0].profile, str(self.tmp/'profiles/account1_edge'))
            self.assertEqual(self.os.starts[0].port,9222)
            self.assertEqual(self.os.starts[0].root,str(self.tmp))
            self.assertEqual(Settings.of(Config.from_file(self.config),self.config), self.os.starts[0])
        finally:
            os.chdir(old)
    def test_profile_changed_after_gui_precheck_is_rejected_under_crawler_lock(self):
        from bestseller_monitor import pipeline, rounds
        from bestseller_monitor.dedicated_browser import Settings
        self.store_plan()
        snapshot = Settings.of(self.cfg,self.config).to_json()
        cfg = self.cfg.replace(gui_browser_settings=snapshot)
        self.os.state = 'occupied'
        with patch('bestseller_monitor.dedicated_browser.BrowserRuntime.observe',side_effect=self.os.observe):
            with self.assertRaisesRegex(ValueError,'关闭'):
                pipeline.run_round(cfg, self.api.shops)
        self.assertEqual(self.api.get_start()['summary']['rounds'],0)


    def test_opening_is_observable_and_concurrent_clicks_do_not_launch_twice(self):
        entered = threading.Event(); release = threading.Event()
        def launch(settings):
            self.os.starts.append(settings)
            entered.set(); release.wait(3)
            self.os.state = 'occupied'
        self.os.launch = launch
        thread = threading.Thread(target=self.api.open_login_browser)
        thread.start(); entered.wait(2)
        done = threading.Event(); views = []
        observer = threading.Thread(target=lambda: (views.append(self.api.get_start()),done.set()))
        observer.start()
        try:
            self.assertTrue(done.wait(.5), '状态读取不得等待启动命令完成')
            self.assertEqual(views[0]['browser']['state'], 'opening')
            self.assertTrue(self.api.open_login_browser()['reused'])
        finally:
            release.set(); thread.join(); observer.join()
        self.assertEqual(len(self.os.starts),1)

    def test_daily_edge_profile_is_rejected_without_query_or_launch(self):
        import os
        daily = self.tmp/'日常 AppData'/'Microsoft'/'Edge'/'User Data'
        text = self.config.read_text('utf-8').replace('profiles/account1_edge',daily.as_posix())
        self.config.write_text(text,'utf-8')
        with patch.dict(os.environ, {'LOCALAPPDATA':str(self.tmp/'日常 AppData')}):
            from bestseller_monitor.dedicated_browser import BrowserRuntime
            api = gui.Api(config_file=self.config,browser_runtime=BrowserRuntime())
            with patch('bestseller_monitor.dedicated_browser.BrowserRuntime.observe') as observe:
                self.assertIn('日常', api.open_login_browser()['error'])
                self.assertFalse(api.start_run(['A'])['ok'])
                observe.assert_not_called()
            self.assertEqual(self.os.starts, [])

    def test_gui_cdp_creation_mismatch_never_uses_or_closes_foreign_browser(self):
        from bestseller_monitor import browser_pw
        from bestseller_monitor.dedicated_browser import Settings
        from test_browser_session_lifecycle import BrowserSessionEstablishmentTests, FakeProcess
        cfg = self.cfg.replace(gui_browser_settings=Settings.of(self.cfg,self.config).to_json())
        factory, pw, br, ctx, page = BrowserSessionEstablishmentTests().stack(browser_pid=99999)
        proc = FakeProcess(20303)
        def terminate(pid):
            self.assertEqual(pid,20303)
            proc.alive = False
            return True
        with patch('bestseller_monitor.dedicated_browser.BrowserRuntime.observe',side_effect=self.os.observe), \
                patch('playwright.sync_api.sync_playwright',return_value=factory), \
                patch.object(browser_pw.subprocess,'Popen',return_value=proc), \
                patch.object(browser_pw.browser_proc,'process_creation_proof',return_value='proof'), \
                patch.object(browser_pw.browser_proc,'terminate_process_tree',side_effect=terminate):
            with self.assertRaisesRegex(RuntimeError,'归属'):
                browser_pw.open_session(cfg)
        ctx.new_page.assert_not_called()
        br.close.assert_not_called()
        pw.stop.assert_called_once()
        self.assertFalse(proc.alive)

    def test_exe_location_resolves_migrated_deployment_before_page_event(self):
        import sys
        sys.path.insert(0,str(Path(gui.PROJECT_ROOT)/'shells'))
        from launcher_core import resolve_project_root, child_command
        from gui_launcher import SPEC
        (self.tmp/'gui.py').touch()
        (self.tmp/'bestseller_monitor').mkdir()
        (self.tmp/'dist').mkdir()
        exe = self.tmp/'dist'/'inventory_fetch.exe'
        exe.touch()
        root = resolve_project_root(exe,env={},frozen=True,spec=SPEC)
        self.assertEqual(root,self.tmp)
        self.assertEqual(child_command(SPEC,root,'pythonw.exe'),['pythonw.exe',str(self.tmp/'gui.py')])
        self.api = gui.Api(config_file=root/'config/config.toml',browser_runtime=self.os)
        page = self.page()
        page.click('#loginBrowserBtn')
        page.wait_for_function("document.querySelector('#browserFacts').textContent.includes('占用')")
        self.assertEqual(self.os.starts[0].root,str(root))
        self.assertEqual(self.api.get_start()['summary']['rounds'],0)

    def test_unknown_browser_with_same_profile_blocks_public_open_and_start(self):
        import json
        from bestseller_monitor.dedicated_browser import BrowserRuntime
        self.store_plan()
        processes=[{'name':'unfamiliar-chromium.exe','pid':77,'parent':0,
                    'cmd':f'"C:/Other Browser/unfamiliar-chromium.exe" "--user-data-dir={self.cfg.user_data_path}"'}]
        response=SimpleNamespace(returncode=0,stdout=json.dumps({'processes':processes,'ports':[]}).encode('utf-8'))
        api=gui.Api(config_file=self.config,browser_runtime=BrowserRuntime(),now=lambda:'2026-10-09T04:00:00+00:00')
        with patch('bestseller_monitor.dedicated_browser.subprocess.run',return_value=response), \
                patch('bestseller_monitor.dedicated_browser.subprocess.Popen') as create:
            self.assertEqual(api.get_start()['browser']['state'],'occupied')
            self.assertTrue(api.open_login_browser()['reused'])
            self.assertFalse(api.start_run(['A'])['ok'])
            create.assert_not_called()
            self.assertEqual(api.get_start()['summary']['rounds'],0)
