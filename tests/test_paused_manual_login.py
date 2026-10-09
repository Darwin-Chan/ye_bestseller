"""Ticket 05: manual dedicated-Edge login after an explicit pause, then an explicit continue.

Real shipped page events (pause / open / resume) drive the real Api, config parsing,
rounds, progress and stop cleanup; only the dedicated-browser OS facts are adapter-
controlled.  The manual browser is the same service ticket 04 shipped for the start page.
"""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gui
from bestseller_monitor import rounds, single_instance
from bestseller_monitor.config import Config
from bestseller_monitor.db import Database, connect, cst_date
from frozen_clock import FROZEN_NOW, frozen_clock
from helpers import isolated_locks
from test_gui import StopClock, store_today_plan


class PausedBrowserOS:
    """Controllable OS facts; config validation stays real (BrowserRuntime)."""

    def __init__(self):
        self.state = 'free'
        self.starts = []
        self.observes = 0
        self.fail_launch = False

    def config_error(self, settings):
        from bestseller_monitor.dedicated_browser import BrowserRuntime
        return BrowserRuntime().config_error(settings)

    def observe(self, settings):
        self.observes += 1
        return SimpleNamespace(state=self.state, message='测试 OS 事实：' + self.state)

    def launch(self, settings):
        if self.fail_launch:
            raise OSError('offline launch failed')
        self.starts.append(settings)
        self.state = 'occupied'
        return SimpleNamespace(pid=123, poll=lambda: 0)


class PausedManualLoginTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(frozen_clock())
        self.enterContext(isolated_locks())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())) / '迁移 部署目录'
        self.tmp.mkdir()
        (self.tmp / 'config').mkdir()
        (self.tmp / 'Edge 浏览器.exe').touch()
        self.text = (Path(gui.PROJECT_ROOT) / 'config/config.example.toml').read_text('utf-8')
        self.text = self.text.replace(
            'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'Edge 浏览器.exe')
        self.text = self.text.replace('alarm_on_intervention = true', 'alarm_on_intervention = false')
        self.config = self.tmp / 'config/config.toml'
        self.config.write_text(self.text, 'utf-8')
        self.cfg = Config.from_file(self.config, root=self.tmp)
        self.cfg.shop_csv.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.shop_csv.write_text(
            'shop_key,shop_name,shop_url,pages,active\n'
            'A,店A,https://a.example/,1,1\n'
            'B,店B,https://b.example/,1,1\n', 'utf-8')
        store_today_plan(self.cfg.db_file, ('A', 'm-test', 1), ('B', 'm-test', 1))
        self.os = PausedBrowserOS()
        self.clock = StopClock()
        self.api = gui.Api(config_file=self.config, browser_runtime=self.os,
                           stop_clock=self.clock, now=lambda: FROZEN_NOW)
        self.rid = None
        self.proc = None

    # ---------- 夹具 ----------
    def start_round(self):
        with closing(connect(self.cfg.db_file)) as conn:
            db = Database(conn)
            self.rid = rounds.open(db, rounds.RoundRequest(cst_date(), (
                rounds.ShopScope('A', 'https://a.example/', '店A'),
                rounds.ShopScope('B', 'https://b.example/', '店B')))).round.id
            db.record_crawler_process(pid=4321, round_id=self.rid,
                                      process_os_started='test-proof',
                                      browser_state='NOT_STARTED')
        self.proc = MagicMock(pid=4321)
        self.proc.poll.return_value = None
        self.api.proc = self.proc
        self.api.round_id = self.rid
        return self.rid

    def pause_and_settle(self):
        self.assertTrue(self.api.pause_run()['ok'])
        self.proc.poll.return_value = 0
        self.assertTrue(self.api.get_run()['can_resume'])

    def login_available(self):
        """过程页手动入口能不能点：`browser.can_open`；没有浏览器事实（未查）即不可点。"""
        return self.api.get_run().get('browser', {}).get('can_open', False)

    def page(self, browser=None):
        from playwright.sync_api import sync_playwright
        if browser is None:
            pw = sync_playwright().start()
            self.addCleanup(pw.stop)
            browser = pw.chromium.launch(channel='msedge', headless=True)
            self.addCleanup(browser.close)
        ctx = browser.new_context()
        self.addCleanup(ctx.close)
        ctx.route('**/*', lambda r: r.continue_() if r.request.url.startswith('file:') else r.abort())
        page = ctx.new_page()
        page.expose_function('ticketApi', lambda name, args: getattr(self.api, name)(*args))
        page.add_init_script(
            "window.pywebview={api:new Proxy({}, {get:(_,n)=>(...a)=>window.ticketApi(n,a)})};"
            "window.alert=()=>{};")
        page.goto((Path(gui.PROJECT_ROOT) / 'bestseller_monitor/pages/ui_live.html').as_uri())
        page.evaluate("switchTab('run'); tickRun()")
        return page

    # ---------- 08-A09/A12：入口的可用时机 ----------
    def test_manual_login_entry_opens_only_after_an_explicit_pause_cleanup(self):
        from playwright.sync_api import expect
        self.start_round()
        page = self.page()
        # 采集中：入口禁用，API 拒绝，不建窗口
        self.assertFalse(self.login_available())
        self.assertFalse(self.api.open_login_browser()['ok'])
        self.assertEqual(self.os.starts, [])
        self.assertTrue(page.locator('#runLoginBrowserBtn').is_disabled())
        # 暂停停止/收尾中：仍禁用
        self.assertTrue(self.api.pause_run()['ok'])
        page.evaluate('tickRun()')
        self.assertFalse(self.login_available())
        self.assertTrue(page.locator('#runLoginBrowserBtn').is_disabled())
        self.assertFalse(self.api.open_login_browser()['ok'])
        self.assertEqual(self.os.starts, [])
        # 收尾完成：过程页出现入口，点击只开手动窗口，不采集、不另建轮
        self.proc.poll.return_value = 0
        self.assertTrue(self.api.get_run()['can_resume'])
        page.evaluate('tickRun()')
        expect(page.locator('#runLoginBrowserBtn')).to_be_enabled()
        self.assertTrue(self.login_available())
        page.click('#runLoginBrowserBtn')
        page.wait_for_function(
            "document.querySelector('#runBrowserFacts').textContent.includes('占用')")
        self.assertEqual(len(self.os.starts), 1)
        self.assertEqual(self.os.starts[0].profile, str(self.tmp / 'profiles/account1_edge'))
        self.assertNotIn('账号已登录', page.locator('#runBrowserFacts').inner_text())

    def test_login_entry_is_refused_without_an_explicit_pause_qualification(self):
        # 未暂停：过程页的入口不提供（票 05 只在明确暂停后接入同一服务）
        self.start_round()
        page = self.page()
        self.assertIn('采集', self.api.open_login_browser()['error'])
        self.assertEqual(self.os.starts, [])
        self.assertTrue(page.locator('#runLoginBrowserBtn').is_disabled())
        page.wait_for_function(
            "document.querySelector('#runBrowserFacts').textContent.includes('不能另开手动浏览器')")

    # ---------- 轮询成本：采集中不查浏览器 OS 事实 ----------
    def test_collecting_refresh_does_not_query_browser_os_facts(self):
        # 过程页每 2 秒轮询一次，而真实进程枚举是秒级的：暂停前不得每轮都查。
        self.start_round()
        self.api.get_run()
        self.assertEqual(self.os.observes, 0)
        for _ in range(3):
            self.assertFalse(self.login_available())
        self.assertEqual(self.os.observes, 0)
        self.pause_and_settle()
        self.assertEqual(self.os.observes, 1)      # 暂停态这一次刷新查了一次
        self.assertTrue(self.login_available())
        self.assertEqual(self.os.observes, 2)      # 每一次刷新只查一次，不是两次

    # ---------- 08-A11：手动窗口未关就能继续 ----------
    def test_continue_is_refused_while_the_manual_window_holds_the_profile(self):
        from playwright.sync_api import expect
        self.start_round()
        self.pause_and_settle()
        page = self.page()
        expect(page.locator('#resumeBtn')).to_be_visible()
        self.assertTrue(self.api.open_login_browser()['ok'])
        self.assertEqual(len(self.os.starts), 1)
        view = self.api.get_run()
        self.assertTrue(view['can_resume'])
        self.assertIn('占用', view['continue_gate'])
        page.evaluate('tickRun()')
        self.assertTrue(page.locator('#resumeBtn').is_disabled())
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertFalse(self.api.resume_run()['ok'])
            spawn.assert_not_called()
        # 重复点击不另开、不接管
        self.assertTrue(self.api.open_login_browser()['reused'])
        self.assertEqual(len(self.os.starts), 1)

    def test_unverifiable_release_refuses_continue(self):
        self.start_round()
        self.pause_and_settle()
        for state in ('unknown', 'port_busy'):
            self.os.state = state
            with self.subTest(state=state):
                view = self.api.get_run()
                self.assertTrue(view['can_resume'])
                self.assertTrue(view['continue_gate'])
                with patch.object(gui.subprocess, 'Popen') as spawn:
                    self.assertFalse(self.api.resume_run()['ok'])
                    spawn.assert_not_called()
        self.os.state = 'free'
        self.assertIsNone(self.api.get_run()['continue_gate'])

    # ---------- 11-A01：关窗不自动继续，显式继续才恢复 ----------
    def test_closing_the_manual_window_does_not_auto_resume(self):
        self.start_round()
        self.pause_and_settle()
        self.assertTrue(self.api.open_login_browser()['ok'])
        self.os.state = 'free'          # 用户关掉了手动窗口
        with patch.object(gui.subprocess, 'Popen') as spawn:
            for _ in range(3):          # 只读轮询不会自动续跑
                self.api.get_run()
            spawn.assert_not_called()
        view = self.api.get_run()
        self.assertTrue(view['can_resume'])
        self.assertIsNone(view['continue_gate'])

    def test_explicit_continue_after_release_restores_the_same_round(self):
        self.start_round()
        self.pause_and_settle()
        self.assertTrue(self.api.open_login_browser()['ok'])
        self.os.state = 'free'
        with patch.object(gui.subprocess, 'Popen') as spawn:
            result = self.api.resume_run()
        self.assertTrue(result['ok'])
        self.assertEqual(result['round_id'], self.rid)
        cmd = spawn.call_args.args[0]
        self.assertIn('--resume-round-intent', cmd)
        self.assertNotIn('--new-round-intent', cmd)
        intent = rounds.ResumeRoundIntent.from_json(cmd[cmd.index('--resume-round-intent') + 1])
        self.assertEqual(intent.round_id, self.rid)
        self.assertEqual(intent.request.shop_keys, ('A', 'B'))

    # ---------- 08-A10/A14：配置变动与不支持的模式 ----------
    def test_changed_browser_config_refuses_login_and_continue(self):
        self.start_round()
        self.pause_and_settle()
        self.config.write_text(self.text.replace('attach_port = 9222', 'attach_port = 9333'),
                               'utf-8')
        view = self.api.get_run()
        self.assertTrue(view['can_resume'])
        self.assertIn('配置', view['continue_gate'])
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertFalse(self.api.resume_run()['ok'])
            spawn.assert_not_called()
        self.assertIn('配置', self.api.open_login_browser()['error'])
        self.assertEqual(self.os.starts, [])

    def test_external_mode_and_invalid_config_refuse_login_and_continue(self):
        self.start_round()
        self.pause_and_settle()
        # 界面打开后 start_browser 被改成 false：按「配置已变化」拒绝，不静默换模式
        self.config.write_text(self.text.replace('start_browser = true', 'start_browser = false'),
                               'utf-8')
        self.assertIn('配置', self.api.get_run()['continue_gate'])
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertFalse(self.api.resume_run()['ok'])
            spawn.assert_not_called()
        self.assertIn('配置', self.api.open_login_browser()['error'])
        self.assertEqual(self.os.starts, [])
        # 直接以 start_browser=false 打开界面：不支持的外部连接模式被准确拒绝
        external = gui.Api(config_file=self.config, browser_runtime=self.os,
                           now=lambda: FROZEN_NOW)
        self.assertIn('start_browser=true', external.open_login_browser()['error'])
        self.assertEqual(external.get_start()['browser']['state'], 'failed')
        # 无效配置同样拒绝登录与继续
        self.config.write_text('invalid = [', 'utf-8')
        self.assertIn('配置错误', self.api.open_login_browser()['error'])
        self.assertFalse(self.api.resume_run()['ok'])
        self.assertEqual(self.os.starts, [])

    # ---------- 11-A13：正常关窗仍结束暂停原轮，不引入关窗后续跑 ----------
    def test_confirm_close_still_ends_the_paused_round(self):
        self.start_round()
        self.pause_and_settle()
        # 手动窗口开着也不改关窗语义：取消（不调 begin_close）保留暂停与原窗口
        self.assertTrue(self.api.open_login_browser()['ok'])
        self.assertEqual(self.api.close_snapshot().kind, 'paused')
        self.assertTrue(self.api.get_run()['can_resume'])
        snapshot = self.api.close_snapshot()
        self.assertTrue(self.api.begin_close(snapshot)['ok'])
        self.assertTrue(self.api.close_ready(snapshot))
        with closing(connect(self.cfg.db_file)) as conn:
            self.assertEqual(rounds.load(Database(conn), self.rid).reason,
                             rounds.TerminalReason.ABANDONED)
        self.assertFalse(self.api.resume_run()['ok'])

    # ---------- 11-A01：停止/收尾期间继续被拒；登录失败不动原轮 ----------
    def test_continue_is_refused_while_the_pause_is_still_stopping(self):
        self.start_round()
        self.assertTrue(self.api.pause_run()['ok'])
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertFalse(self.api.resume_run()['ok'])
            spawn.assert_not_called()
        self.assertFalse(self.api.get_run()['can_resume'])
        self.proc.poll.return_value = 0
        self.assertTrue(self.api.get_run()['can_resume'])

    def test_failed_manual_open_leaves_the_paused_round_untouched(self):
        self.start_round()
        self.pause_and_settle()
        self.os.fail_launch = True
        result = self.api.open_login_browser()
        self.assertFalse(result['ok'])
        self.assertIn('失败', result['error'])
        self.assertEqual(self.os.starts, [])
        with closing(connect(self.cfg.db_file)) as conn:      # 原轮没有终态、成员与进度不变
            self.assertIsNone(rounds.load(Database(conn), self.rid).reason)
        view = self.api.get_run()
        self.assertTrue(view['can_resume'])
        # 打开失败后状态是「失败」：与开始页同源，继续保守拒绝，不带着未知状态启动
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertFalse(self.api.resume_run()['ok'])
            spawn.assert_not_called()

    # ---------- 11-A01/A03：手动登录之后真实调度仍跳过已完成任务 ----------
    def test_manual_login_then_explicit_continue_reaches_real_scheduling(self):
        import os
        from playwright.sync_api import sync_playwright
        from bestseller_monitor import browser_pw, pipeline, waiting
        from bestseller_monitor.config import Shop
        from helpers import crawler_cfg, insert_inventory_rows

        self.shops = [Shop(k, 'original-' + k, f'https://{k}.example/') for k in 'ABCD']
        store_today_plan(self.cfg.db_file, *((k, 'm-test', 1) for k in 'ABCD'))
        with closing(connect(self.cfg.db_file)) as conn:
            db = Database(conn)
            self.rid = rounds.open(db, rounds.RoundRequest(cst_date(), tuple(
                rounds.ShopScope(s.key, s.url, s.name) for s in self.shops))).round.id
            db.record_crawler_process(pid=4321, round_id=self.rid,
                                      process_os_started='test-proof',
                                      browser_state='NOT_STARTED')
            # A/B/C 今天已采满：真实调度应直接跳过，D 只剩 501/502 两个任务
            insert_inventory_rows(conn, [(k, str(i), cst_date()) for k in 'ABC' for i in range(30)])
            for shop in self.shops:
                ids = ['501', '502'] if shop.key == 'D' else [str(i) for i in range(30)]
                db.save_shop_offers(self.rid, shop.key, shop.url, shop.name,
                                    [(i + 1, offer,
                                      f'https://detail.1688.com/offer/{offer}.html',
                                      'saved', '1') for i, offer in enumerate(ids)], 1)
        self.proc = MagicMock(pid=os.getpid())
        self.proc.poll.return_value = None
        self.api.proc = self.proc
        self.api.round_id = self.rid

        pw = sync_playwright().start()
        self.addCleanup(pw.stop)
        browser = pw.chromium.launch(channel='msedge', headless=True)
        self.addCleanup(browser.close)
        ctx = browser.new_context(java_script_enabled=False)
        self.addCleanup(ctx.close)
        detail_page = ctx.new_page()
        html = ('<html><head><title>商品详情</title></head><body>'
                '<div id="offer-detail"><h1>测试商品</h1></div>'
                '<script>{"skuInfoMap":{"red":{"skuId":"red","name":"red",'
                '"price":10,"canBookCount":3}}}</script></body></html>')
        visits = []

        def route(request):
            if not request.request.is_navigation_request():
                request.abort()
                return
            visits.append(request.request.url)
            request.fulfill(content_type='text/html; charset=utf-8', body=html)

        ctx.route('**/*', route)

        def session(cfg, *, publish_browser):
            publish_browser('BORROWED', 9222, None, None)
            return None, None, detail_page, ctx

        self.enterContext(patch.object(browser_pw, 'open_session', side_effect=session))
        self.enterContext(patch.object(browser_pw, 'close_session'))
        cfg = crawler_cfg(db_file=self.cfg.db_file, root=self.tmp, data_dir=self.tmp / 'data',
                          logs_dir=self.tmp / 'logs', raw_page_dir=self.tmp / 'raw',
                          screenshot_dir=self.tmp / 'shots',
                          user_data_path=self.tmp / 'profile',
                          exchange_root=self.tmp / 'exchange', ensure_dirs=lambda: None,
                          start_browser=False, max_pages_per_shop=3,
                          pause_every_detail_visits=1, pause_sec=(5.0, 5.0))
        clock = _SleepClock()
        self.enterContext(patch.object(waiting, 'time', clock))
        ui = self.page(browser)
        acted = []

        def click_pause():
            if clock.now >= 3 and not acted:
                ui.locator('#pauseBtn').click()
                acted.append(True)

        clock.on_sleep = click_pause
        pipeline.run_round(cfg, self.shops, elapsed_clock=clock.monotonic)
        self.assertEqual(acted, [True], '真实复现必须真的点了暂停')
        self.proc.poll.return_value = 0
        self.assertTrue(self.api.get_run()['can_resume'])
        self.assertEqual(visits, ['https://detail.1688.com/offer/501.html'])

        # 暂停后从过程页手动打开、再关闭专用 Edge：关窗不自动继续
        self.assertTrue(self.api.open_login_browser()['ok'])
        self.os.state = 'free'
        self.assertIsNone(self.api.get_run()['continue_gate'])
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertTrue(self.api.resume_run()['ok'])
        cmd = spawn.call_args.args[0]
        intent = rounds.ResumeRoundIntent.from_json(
            cmd[cmd.index('--resume-round-intent') + 1])
        self.assertEqual(intent.request.shop_keys, ('A', 'B', 'C', 'D'))

        visits.clear()
        clock.on_sleep = lambda: None
        pipeline.run_round(cfg, [Shop(s.key, s.name, s.url) for s in intent.request.shops],
                           resume_intent=intent)
        self.assertEqual(visits, ['https://detail.1688.com/offer/502.html'])
        with closing(connect(self.cfg.db_file)) as conn:
            db = Database(conn)
            self.assertEqual(rounds.latest(db).id, self.rid)
            self.assertEqual(rounds.load(db, self.rid).shop_keys, ('A', 'B', 'C', 'D'))
            self.assertEqual(rounds.load(db, self.rid).reason, rounds.TerminalReason.COMPLETED)


class _SleepClock:
    """只替代等待原语里的时钟：`on_sleep` 在每片睡眠后回调（真实调度里的暂停点击）。"""

    def __init__(self):
        self.now = 0.0
        self.on_sleep = lambda: None

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()

