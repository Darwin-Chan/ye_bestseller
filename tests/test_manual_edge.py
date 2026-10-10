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
        # 桩代理与真实 pywebview 对齐：页面自存的属性（api.__shops）原样读写，
        # 其余名字才派发到 Python（票 02 的点击流会在核对重绘后读回勾选快照）。
        page.add_init_script("window.pywebview={api:new Proxy({}, {get: (t,n)=> (n in t) ? t[n] : (...a)=>window.ticketApi(n,a)})};window.alert=()=>{};")
        page.goto((Path(gui.PROJECT_ROOT)/'bestseller_monitor/pages/ui_live.html').as_uri())
        page.locator('#loginBrowserBtn').wait_for()
        # 就绪判据（票 04 改动三）：事实卡已删，进页取数完成的信号改成按钮悬浮提示有值。
        page.wait_for_function("document.querySelector('#loginBrowserBtn').title.length > 0",
                               timeout=10000)
        return page
    def test_page_login_does_not_collect_or_create_round(self):
        page = self.page()
        page.click('#loginBrowserBtn')
        page.wait_for_function(
            "document.querySelector('#loginBrowserBtn').title.includes('占用')", timeout=5000)
        self.assertEqual(len(self.os.starts), 1)
        self.assertIsNone(self.api.proc)
        view = self.api.get_start()
        self.assertEqual(view['summary']['rounds'], 0)
        self.assertEqual(view['browser']['profile'], str(self.tmp/'profiles/account1_edge'))
        self.assertNotIn('账号已登录', page.locator('#loginBrowserBtn').get_attribute('title'))
        # 「刷新浏览器状态」按钮已删（票 01）：等价的读一次数直接走 refreshStart()。
        page.evaluate('refreshStart()')
        self.assertEqual(len(self.os.starts), 1)

    # ---------- 票 01：入口收拢（删刷新按钮、两页改名、上移页头） ----------
    def test_start_tab_header_carries_the_renamed_entry_left_of_the_trial_switch(self):
        page = self.page()
        self.assertEqual(page.eval_on_selector_all('header button', 'els => els.map(e => e.id)'),
                         ['loginBrowserBtn', 'runLoginBrowserBtn', 'trialModeBtn'],
                         '两枚「打开专用 Edge」（票 04）同槽并列，都在「进入本机试采」左侧')
        self.assertEqual(page.locator('#loginBrowserBtn').inner_text().strip(), '打开专用 Edge')
        self.assertTrue(page.locator('#loginBrowserBtn').is_visible())
        self.assertFalse(page.locator('#runLoginBrowserBtn').is_visible(), '开始页签不显过程版')

    def test_start_card_keeps_only_facts_and_copy_without_any_refresh_control(self):
        page = self.page()
        self.assertTrue(page.evaluate("document.getElementById('refreshBrowserBtn') === null"),
                        '「刷新浏览器状态」按钮的 HTML 已随票 01 删除')
        self.assertTrue(page.evaluate(
            "document.querySelector('#panel-start #loginBrowserBtn') === null"),
            '登录入口已上移页头：开始卡内不再有登录入口按钮')

    def test_header_login_entries_share_one_slot_across_tabs(self):
        # 票 04：两枚登录入口同槽互斥——开始页签显开始版；过程页签有轮次才显过程版
        # （本夹具没有轮次，因此两个页签都看不到过程版）；结果页签两版都不显。
        page = self.page()
        self.assertTrue(page.evaluate("document.querySelector('header #loginBrowserBtn') !== null"),
                        '入口应挂在页头（只随页签显隐，不再随开始卡一起消失）')
        self.assertTrue(page.locator('#loginBrowserBtn').is_visible())
        page.evaluate("showTab('run')")
        self.assertFalse(page.locator('#loginBrowserBtn').is_visible(), '过程页签不显示页头开始版')
        self.assertFalse(page.locator('#runLoginBrowserBtn').is_visible(),
                         '过程页签无轮次时不显示过程版（has_round 才显）')
        page.evaluate("showTab('result')")
        self.assertFalse(page.locator('#loginBrowserBtn').is_visible(), '结果页签不显示页头开始版')
        self.assertFalse(page.locator('#runLoginBrowserBtn').is_visible(), '结果页签不显示过程版')
        page.evaluate("showTab('start')")
        self.assertTrue(page.locator('#loginBrowserBtn').is_visible(), '回到开始页签恢复显示')

    def test_facts_and_copy_switch_to_automatic_recheck_wording(self):
        from bestseller_monitor.dedicated_browser import RETRY
        self.assertNotIn('刷新', RETRY, 'retry 文案不再指向已删除的刷新按钮')
        self.assertIn('自动核对', RETRY, 'retry 文案改为自动核对口径（开始页与过程页同源显示）')
        # 票 04 改动三：事实卡已删——retry 只活在点击信息框里，不进悬浮提示。
        page = self.page()
        self.os.state = 'occupied'
        page.evaluate('refreshStart()')
        page.wait_for_function(
            "document.querySelector('#loginBrowserBtn').title.includes('占用')", timeout=5000)
        title = page.locator('#loginBrowserBtn').get_attribute('title')
        self.assertNotIn('刷新', title)
        self.assertNotIn('请自行关闭', title, 'retry 尾句不进悬浮提示')
        page.click('#loginBrowserBtn')
        page.wait_for_selector('#confirmModal.show', timeout=5000)
        sub = page.locator('#confirmSub').inner_text()
        self.assertIn('自动核对', sub, '「已有窗口」信息框带着新的自动核对文案')
        self.assertNotIn('刷新', sub)
        self.assertNotIn('刷新', page.locator('#panel-start').inner_text(),
                         '开始卡说明句已随卡片删除（票 04 改动三）')

    def test_fact_rechecks_stay_automatic_on_tab_return_and_trial_switch(self):
        # 删按钮/删卡不减少核对（票 01/04）：进页取一次事实之外，切回开始页签、
        # 切换模式仍是自动取数时点。观测面从事实行换成取数计数＋按钮悬浮提示。
        # 合并适配（2026-10-11，local-trial-merge-only）：试采入口已按 ADR-0049 修订段
        # 收窄为纯汇总机功能（trialModeBtn 只在 merge_only 显示），且纯汇总机正式态
        # 入口自带置灰（另一契约，见 test_role_disallows_entry_api_and_page）；本用例在
        # 试采态起步观测「切回正式仍自动取数」，三个核对时点断言不变。
        self.config.write_text(self.config.read_text('utf-8').replace('role = "collector"','role = "merge_only"'),'utf-8')
        self.api = gui.Api(config_file=self.config, browser_runtime=self.os)
        self.assertTrue(self.api.set_local_trial(True)['ok'])
        calls = []
        real = self.api.get_start
        self.api.get_start = lambda *a: (calls.append(1), real(*a))[1]
        page = self.page()
        self.assertEqual(len(calls), 1, '进页自动取一次')
        self.assertIn('专用目录未占用', page.locator('#loginBrowserBtn').get_attribute('title'))
        page.evaluate("showTab('run')")
        self.os.state = 'occupied'
        before = len(calls)
        page.evaluate("switchTab('start')")
        page.wait_for_function(
            "document.querySelector('#loginBrowserBtn').title.includes('占用')", timeout=5000)
        self.assertEqual(len(calls), before + 1, '切回开始页签自动取一次')
        self.os.state = 'free'
        before = len(calls)
        page.click('#trialModeBtn')
        page.wait_for_function(
            "document.querySelector('#loginBrowserBtn').title.includes('纯汇总机')", timeout=5000)
        self.assertEqual(len(calls), before + 1, '切换模式自动取一次')
        self.assertIn('正式采集', page.locator('#collectionMode').inner_text())

    # ---------- 票 02：检测/配置类不预置灰，点击报因（页面内单按钮信息框） ----------
    def test_occupied_buttons_stay_lit_and_the_open_click_reports_the_existing_window(self):
        # 占用态（票 02）：两按钮亮；点「打开专用 Edge」弹「已有窗口」信息框——
        # 已在运行、请用原窗口、关闭后再开始采集；绝不假称「已关闭」，也不重复开窗。
        self.store_plan()
        self.assertTrue(self.api.open_login_browser()['ok'])
        page = self.page()
        self.assertFalse(page.locator('#loginBrowserBtn').is_disabled(), '检测类不再预置灰')
        self.assertFalse(page.locator('#startBtn').is_disabled())
        page.click('#loginBrowserBtn')
        page.wait_for_selector('#confirmModal.show', timeout=5000)
        self.assertIn('已在运行', page.locator('#confirmTitle').inner_text())
        sub = page.locator('#confirmSub').inner_text()
        self.assertIn('原窗口', sub)
        self.assertIn('关闭', sub)
        self.assertNotIn('已关闭', sub, '绝不显示假的「已关闭」')
        self.assertEqual(page.locator('#confirmOk').inner_text().strip(), '知道了')
        self.assertFalse(page.locator('#confirmCancel').is_visible(), '信息框是单按钮变体')
        self.assertEqual(len(self.os.starts), 1, '占用态不重复开窗')
        page.click('#confirmOk')
        page.wait_for_selector('#confirmModal', state='hidden', timeout=5000)

    def test_occupied_start_click_reports_the_reason_without_spawning(self):
        # 占用态点「开始抓取」（票 02）：先自动核对取最新事实，弹同源原因的信息框；
        # 不开子进程、不建轮次。
        self.store_plan()
        self.assertTrue(self.api.open_login_browser()['ok'])
        page = self.page()
        with patch('gui.subprocess.Popen') as spawn:
            page.click('#startBtn')
            page.wait_for_selector('#confirmModal.show', timeout=5000)
            spawn.assert_not_called()
        sub = page.locator('#confirmSub').inner_text()
        self.assertIn('专用 Edge', sub)
        self.assertIn('关闭', sub)
        self.assertEqual(page.locator('#confirmOk').inner_text().strip(), '知道了')
        self.assertEqual(self.api.get_start()['summary']['rounds'], 0)

    def test_config_error_click_reports_the_reason_from_both_buttons(self):
        # 配置错误类（缺 Edge，票 02）：两按钮亮；点击各弹同源原因的信息框；
        # 不接管无关实例（起点计数为零）、不假称「已关闭」。
        self.store_plan()
        (self.tmp/'Edge 浏览器.exe').unlink()
        page = self.page()
        self.assertFalse(page.locator('#loginBrowserBtn').is_disabled())
        self.assertFalse(page.locator('#startBtn').is_disabled())
        for target in ('#loginBrowserBtn', '#startBtn'):
            page.click(target)
            page.wait_for_selector('#confirmModal.show', timeout=5000)
            sub = page.locator('#confirmSub').inner_text()
            self.assertIn('未找到 Edge', sub, target)
            self.assertNotIn('已关闭', sub, target)
            page.click('#confirmOk')
            page.wait_for_selector('#confirmModal', state='hidden', timeout=5000)
        self.assertEqual(len(self.os.starts), 0, '不接管无关实例')
        self.assertEqual(self.api.get_start()['summary']['rounds'], 0)

    def test_opening_click_reports_without_a_second_launch(self):
        # 「正在打开」瞬时态（票 02）：不再置灰；点按弹对应信息框（reused），
        # 启动计数保持 1——防双开靠服务端去重与 reused 返回。
        entered = threading.Event(); release = threading.Event()
        def launch(settings):
            self.os.starts.append(settings); entered.set(); release.wait(3)
            self.os.state = 'occupied'
        self.os.launch = launch
        thread = threading.Thread(target=self.api.open_login_browser)
        thread.start(); entered.wait(2)
        try:
            page = self.page()
            self.assertFalse(page.locator('#loginBrowserBtn').is_disabled(), '正在打开不再置灰')
            page.click('#loginBrowserBtn')
            page.wait_for_selector('#confirmModal.show', timeout=5000)
            self.assertIn('正在打开', page.locator('#confirmTitle').inner_text())
            sub = page.locator('#confirmSub').inner_text()
            self.assertIn('即将出现', sub)
            self.assertNotIn('已关闭', sub)
            self.assertEqual(len(self.os.starts), 1, '不双开')
        finally:
            release.set(); thread.join()

    def test_start_click_recheck_keeps_the_current_selection(self):
        # 点开始先自动核对（票 02）：清单重绘后当前勾选按店原样保留，不丢选择。
        self.cfg.shop_csv.write_text(
            'shop_key,shop_name,shop_url,pages,active\n'
            'A,测试,https://a.example/,1,1\n'
            'B,乙店,https://b.example/,1,1\n', 'utf-8')
        self.store_plan()
        page = self.page()
        page.wait_for_selector('#rows input')
        self.assertEqual(page.eval_on_selector_all('#rows input', 'els => els.map(e => e.checked)'),
                         [True, False], '默认：本机的 A 勾、计划外的 B 不勾')
        page.evaluate("document.querySelectorAll('#rows input')[1].click()")
        self.assertEqual(page.locator('#selCnt').inner_text(), '已选 2 / 2')
        self.os.state = 'occupied'
        page.click('#startBtn')
        page.wait_for_selector('#confirmModal.show', timeout=5000)
        self.assertIn('关闭', page.locator('#confirmSub').inner_text(), '核对撞上占用门禁')
        self.assertEqual(page.locator('#selCnt').inner_text(), '已选 2 / 2', '核对重绘不丢勾选')
        self.assertEqual(page.eval_on_selector_all('#rows input', 'els => els.map(e => e.checked)'),
                         [True, True])

    def test_start_click_with_nothing_checked_uses_the_info_box(self):
        # 「未勾选店铺」也走信息框（票 02），不再用系统 alert。
        self.store_plan()
        page = self.page()
        # 表达式以数字 0 收尾：完成值若是函数，Playwright 的求值工具会把它当谓词调用一次。
        page.evaluate("window.__alertCalls = 0; window.alert = () => { window.__alertCalls++; }; 0")
        page.evaluate("document.querySelector('#rows input').click()")
        self.assertEqual(page.locator('#selCnt').inner_text(), '已选 0 / 1')
        page.click('#startBtn')
        page.wait_for_selector('#confirmModal.show', timeout=5000)
        self.assertIn('至少勾选一个店铺', page.locator('#confirmSub').inner_text())
        self.assertEqual(page.locator('#confirmOk').inner_text().strip(), '知道了')
        self.assertFalse(page.locator('#confirmCancel').is_visible())
        self.assertEqual(page.evaluate('window.__alertCalls'), 0, '不再用系统 alert')
        self.assertEqual(self.api.get_start()['summary']['rounds'], 0)

    def test_start_click_reports_a_server_refusal_in_the_info_box(self):
        # 核对没发现门禁、调用开始 API 才被拒（用「正在关闭」制造真实的服务端拒绝，票 02）：
        # 失败原因同样走页面内信息框，不是系统 alert。
        self.store_plan()
        page = self.page()
        # 表达式以数字 0 收尾：完成值若是函数，Playwright 的求值工具会把它当谓词调用一次。
        page.evaluate("window.__alertCalls = 0; window.alert = () => { window.__alertCalls++; }; 0")
        self.assertTrue(self.api.begin_close(self.api.close_snapshot())['ok'])
        page.click('#startBtn')
        page.wait_for_selector('#confirmModal.show', timeout=5000)
        self.assertIn('关闭程序', page.locator('#confirmSub').inner_text())
        self.assertEqual(page.evaluate('window.__alertCalls'), 0)
        self.assertEqual(self.api.get_start()['summary']['rounds'], 0)

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
        # 票 02：占用属检测类，不再折入 start_allowed（那半边只表达计划/忙/遗留/欠账）；
        # 核对原因留在 browser_error 供点击报因，服务端 start_run 的拒绝面照旧（下面两条）。
        view = self.api.get_start()
        self.assertTrue(view['start_allowed'])
        self.assertIn('关闭', view['browser_error'])
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
        # 票 02：创建归属无法确认为检测类，不再预置灰；原因在 browser_error，服务端照拒。
        view = self.api.get_start()
        self.assertTrue(view['start_allowed'])
        self.assertIn('无法确认', view['browser_error'])
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
        # 票 02：检测/配置类不再预置灰——两按钮亮，点击弹同源原因的信息框；
        # 服务端拒绝照旧（下面的 API 断言保持不动）。
        self.assertFalse(page.locator('#loginBrowserBtn').is_disabled())
        self.assertFalse(page.locator('#startBtn').is_disabled())
        expected = self.api.open_login_browser()['error']
        page.click('#loginBrowserBtn')
        page.wait_for_selector('#confirmModal.show', timeout=5000)
        self.assertEqual(page.locator('#confirmSub').inner_text(), expected, '原样报因')
        page.click('#confirmOk')
        page.wait_for_selector('#confirmModal', state='hidden', timeout=5000)
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
    def test_merge_only_trial_allows_the_login_entry_and_returns_to_refusal(self):
        # 纯汇总机的本机试采同样要登录专用 profile（票 08）：试采下入口放行，
        # 打开的是同一解析目录；切回正式模式恢复拒绝。正式拒绝文案不变。
        self.config.write_text(self.config.read_text('utf-8').replace('role = "collector"','role = "merge_only"'),'utf-8')
        self.api = gui.Api(config_file=self.config, browser_runtime=self.os)
        self.assertIn('纯汇总机', self.api.open_login_browser()['error'])
        self.assertTrue(self.api.set_local_trial(True)['ok'])
        page = self.page()
        page.evaluate('refreshStart()')
        self.assertFalse(page.locator('#loginBrowserBtn').is_disabled())
        result = self.api.open_login_browser()
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.os.starts[-1].profile, str(self.tmp/'profiles/account1_edge'))
        self.assertTrue(self.api.set_local_trial(False)['ok'])
        page.evaluate('refreshStart()')
        self.assertTrue(page.locator('#loginBrowserBtn').is_disabled())
        self.assertIn('纯汇总机', self.api.open_login_browser()['error'])
    def test_start_page_shows_why_the_login_entry_is_disabled(self):
        # 开始页按钮禁用时要给出原因（与过程页同一处 login_refusal）；票 04 改动三后
        # 原因、配置来源与专用目录一起挂在按钮悬浮提示上（拒绝句；配置：…；专用目录：…），
        # 且不再附带状态句（那半只在常态提示里）。
        self.config.write_text(self.config.read_text('utf-8').replace('role = "collector"','role = "merge_only"'),'utf-8')
        self.api = gui.Api(config_file=self.config, browser_runtime=self.os)
        page = self.page()
        page.evaluate('refreshStart()')
        page.wait_for_function(
            "document.querySelector('#loginBrowserBtn').title.includes('纯汇总机')", timeout=5000)
        self.assertTrue(page.locator('#loginBrowserBtn').is_disabled())
        title = page.locator('#loginBrowserBtn').get_attribute('title')
        self.assertIn('配置：', title)
        self.assertIn('专用目录：', title)
        self.assertNotIn('专用目录未占用', title, '置灰态提示不含状态句（两形之分）')

    # ---------- 票 04：浏览器事实面收整（删卡、删「改范围」、入口上移） ----------
    def test_start_page_has_no_live_browser_facts_surface(self):
        # 票 04 改动三：开始页第一张卡整卡删除（事实行元素与其写入、说明文字均不在）；
        # 浏览器事实退到「打开专用 Edge」悬浮提示：常态＝配置：…；专用目录：…；状态句；
        # retry 尾句不进提示；开始按钮不另加提示（其置灰原因另有横幅/处置卡的面）。
        page = self.page()
        self.assertTrue(page.evaluate("document.getElementById('browserFacts') === null"),
                        '事实行元素已随卡片删除')
        start_text = page.locator('#panel-start').inner_text()
        self.assertNotIn('专用数据目录独立于日常 Edge', start_text, '说明文字随卡删净')
        self.assertNotIn('浏览器打开不代表账号已登录', start_text)
        b = self.api.get_start()['browser']
        self.assertEqual(page.locator('#loginBrowserBtn').get_attribute('title'),
                         "配置：" + b['source'] + "；专用目录：" + b['profile'] + "；" + b['message'],
                         '常态悬浮提示＝配置：…；专用目录：…；状态句')
        self.assertNotIn('请自行关闭', page.locator('#loginBrowserBtn').get_attribute('title'),
                         'retry 尾句只活在点击信息框')
        self.assertFalse(page.locator('#startBtn').get_attribute('title'),
                         '开始按钮不加悬浮提示')

    def test_change_scope_chain_and_run_login_card_are_gone(self):
        # 票 04：删「改范围（中止后重选）」前端整链与「暂停后处理登录」整卡；
        # 唯一中止入口是「中止本轮」，替代动线＝中止 → 结果页「返回开始」→ 重选 → 明确开始。
        page = self.page()
        self.assertTrue(page.evaluate("document.getElementById('changeScopeBtn') === null"))
        self.assertTrue(page.evaluate("typeof changeScope === 'undefined'"))
        self.assertTrue(page.evaluate("typeof rangeChangeRound === 'undefined'"))
        self.assertTrue(page.evaluate("document.getElementById('runLoginCard') === null"))
        self.assertTrue(page.evaluate("document.getElementById('runBrowserFacts') === null"))
        self.assertTrue(page.evaluate("document.getElementById('abortBtn') !== null"),
                        '「中止本轮」保留')
        self.assertTrue(page.evaluate("document.getElementById('backBtn') !== null"),
                        '结果页「返回开始」是替代动线的回程')
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
        # 票 05 起，明确暂停且停止收尾完成后，过程页的手动入口与开始页同一服务放行；
        # 采集与停止/收尾中的拒绝（上面两条）仍是这条门禁的保护意图。
        self.assertTrue(self.api.open_login_browser()['ok'])
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
        page.wait_for_function(
            "document.querySelector('#loginBrowserBtn').title.includes('占用')", timeout=5000)
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
