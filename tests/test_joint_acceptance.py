"""票 07：联合验收链 J-A01–J-A04（登录准备→开始→身份验证→暂停/中止→手动处理→继续/新建）。

复用两组已确认的高层接缝，不另造第三套：真实页面守卫→等待→公共 `pipeline.run_round`；
实际 HTML 事件（开始页/过程页按钮）→真实 `gui.Api`、配置解析、临时 SQLite 与停止收尾。
只适配浏览器/OS 副作用（专用浏览器启动与进程事实）、时钟与发声。

离线环境里没有真实采集子进程与浏览器官网：建轮、身份登记与榜单保存按票 03/05 真实
调度用例的同一手法由测试代班（`seed_round`），其余检测/等待/调度/门控全部真实运行。
真实身份验证页默认用脱敏 fixture（`tests/fixtures/identity-verification.html`）；
发布验收时给 `IDENTITY_SOURCE` 指现场原件的绝对路径回放同一链。
"""
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import gui
from bestseller_monitor import browser_pw, guard, pipeline, rounds, waiting
from bestseller_monitor.config import Config, Shop
from bestseller_monitor.db import Database, connect
from frozen_clock import FROZEN_DATE, FROZEN_NOW, frozen_clock
from helpers import crawler_cfg, insert_inventory_rows, isolated_locks
from test_gui import StopClock, store_today_plan
from test_identity_lifecycle import BUSINESS, FIXTURE, Clock
from test_paused_manual_login import PausedBrowserOS


def identity_html() -> str:
    """现场身份验证原件（发布验收经 `IDENTITY_SOURCE` 指入）；默认脱敏 fixture。"""
    return Path(os.environ.get('IDENTITY_SOURCE', FIXTURE)).read_text(encoding='utf-8-sig')


BUSINESS_SKU = BUSINESS.replace(
    '</body>', '<script>{"skuInfoMap":{"red":{"skuId":"red","name":"red",'
               '"price":10,"canBookCount":3}}}</script></body>')


class JointAcceptanceTests(unittest.TestCase):
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
        self.text = self.text.replace('machine_id = "m1"', 'machine_id = "m-test"')
        self.config = self.tmp / 'config/config.toml'
        self.config.write_text(self.text, 'utf-8')
        self.cfg = Config.from_file(self.config, root=self.tmp)
        self.cfg.shop_csv.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.shop_csv.write_text(
            'shop_key,shop_name,shop_url,pages,active\n'
            + ''.join(f'{k},店{k},https://{k}.example/,1,1\n' for k in 'ABCD'), 'utf-8')
        store_today_plan(self.cfg.db_file, *((k, 'm-test', 1) for k in 'ABCD'))
        self.shops = [Shop(k, f'店{k}', f'https://{k}.example/') for k in 'ABCD']
        self.os = PausedBrowserOS()
        self.clock = StopClock()
        self.api = gui.Api(config_file=self.config, browser_runtime=self.os,
                           stop_clock=self.clock, now=lambda: FROZEN_NOW)
        self.child = None

    # ---------- 夹具 ----------
    def load(self, rid):
        with closing(connect(self.cfg.db_file)) as conn:
            return rounds.load(Database(conn), rid)

    def seed_round(self, *, shops=None, offers=None, today_rows=(), day=None):
        """代替采集子进程完成建轮、身份登记与榜单保存（票 03/05 同一手法）。"""
        offers = offers or {}
        shops = shops or self.shops
        with closing(connect(self.cfg.db_file)) as conn:
            db = Database(conn)
            opened = rounds.open(db, rounds.RoundRequest(day or FROZEN_DATE, tuple(
                rounds.ShopScope(s.key, s.url, s.name) for s in shops)))
            for superseded in opened.superseded:
                conn.execute("UPDATE rounds SET terminal_reason=NULL, finished_at=NULL WHERE id=?",
                             (superseded.id,))
            conn.commit()
            rid = opened.round.id
            db.record_crawler_process(pid=4321, round_id=rid, process_os_started='crawler-proof',
                                      browser_state='NOT_STARTED')
            for shop in shops:
                ids = offers.get(shop.key, [])
                if not ids:
                    continue
                db.save_shop_offers(rid, shop.key, shop.url, shop.name,
                                    [(i + 1, offer,
                                      f'https://detail.1688.com/offer/{offer}.html',
                                      'saved', '1') for i, offer in enumerate(ids)], 1)
            insert_inventory_rows(conn, list(today_rows))
        return rid

    def run_cfg(self, **overrides):
        base = dict(db_file=self.cfg.db_file, root=self.tmp, data_dir=self.tmp / 'data',
                    logs_dir=self.tmp / 'logs', raw_page_dir=self.tmp / 'raw',
                    screenshot_dir=self.tmp / 'shots', user_data_path=self.cfg.user_data_path,
                    exchange_root=self.tmp / 'exchange', ensure_dirs=lambda: None,
                    start_browser=False, max_pages_per_shop=3,
                    human_pause_minutes=0.1, intervention_confirmation_sec=2)
        base.update(overrides)
        return crawler_cfg(**base)

    def browser(self):
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        self.addCleanup(pw.stop)
        browser = pw.chromium.launch(channel='msedge', headless=True)
        self.addCleanup(browser.close)
        return browser

    def ui_page(self, browser):
        ctx = browser.new_context()
        self.addCleanup(ctx.close)
        ctx.route('https://**/*', lambda route: route.abort())
        ctx.route('http://**/*', lambda route: route.abort())
        page = ctx.new_page()
        page.expose_function('ticketApi', lambda name, args: getattr(self.api, name)(*args))
        page.add_init_script(
            "window.pywebview={api:new Proxy({}, {get:(o,n)=>n in o ? o[n] : (...a)=>window.ticketApi(n,a)})};"
            "window.alert=()=>{};")
        page.goto((Path(gui.PROJECT_ROOT) / 'bestseller_monitor/pages/ui_live.html').as_uri())
        page.locator('input[data-i]').first.wait_for()
        return page

    def detail_stack(self, browser, router, *, close_session=None):
        """采集浏览器替身：禁用脚本的离线页面，文档导航按 `router(url)` 供页。"""
        ctx = browser.new_context(java_script_enabled=False)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        visits = []

        def route(request):
            if not request.request.is_navigation_request():
                request.abort()
                return
            visits.append(request.request.url)
            request.fulfill(status=200, content_type='text/html; charset=utf-8',
                            body=router(request.request.url))

        ctx.route('**/*', route)

        def session(cfg, *, publish_browser):
            publish_browser('BORROWED', 9222, None, None)
            return None, None, page, ctx

        self.enterContext(patch.object(browser_pw, 'open_session', side_effect=session))
        if close_session is None:
            self.enterContext(patch.object(browser_pw, 'close_session'))
        else:
            self.enterContext(patch.object(browser_pw, 'close_session', side_effect=close_session))
        return page, visits

    def attach_child(self, rid, *, pid=None, alive=True):
        """界面在子进程启动后的接线：认领本轮与自己的采集进程。

        pid 必须与身份行里记录的采集进程一致（真实运行即本测试进程）：停止窗口在
        begin 时按「界面自己的那个句柄」绑定，pid 不一致会退化成不可核验的 OS 句柄。
        """
        self.api.round_id = rid
        self.child = MagicMock(pid=pid if pid is not None else os.getpid())
        self.child.poll.return_value = None if alive else 0
        self.api.proc = self.child
        return self.child

    def wait_clock(self):
        clock = Clock()
        self.enterContext(patch.object(guard, 'time', clock))
        self.enterContext(patch.object(waiting, 'time', clock))
        return clock

    def spawn_intent(self, spawn, flag):
        cmd = spawn.call_args.args[0]
        self.assertIn(flag, cmd)
        return cmd, cmd[cmd.index(flag) + 1]

    def ui_pause(self, ui):
        """过程页真实暂停：等停止/收尾指示出现，证明暂停请求确已落地。"""
        ui.locator('#pauseBtn').click()
        ui.wait_for_function(
            "document.querySelector('#rStopping') && "
            "document.querySelector('#rStopping').style.display !== 'none'")

    def start_via_page(self, ui):
        """真实开始按钮：等门禁放开再点，返回启动命令与解析出的新轮意图。"""
        ui.evaluate('refreshStart()')
        ui.wait_for_function("!document.querySelector('#startBtn').disabled")
        with patch.object(gui.subprocess, 'Popen') as spawn:
            ui.click('#startBtn')
            ui.wait_for_timeout(100)
        cmd, raw = self.spawn_intent(spawn, '--new-round-intent')
        return cmd, rounds.NewRoundIntent.from_json(raw)

    # ---------- J-A01 ----------
    def test_ja01_login_prep_then_identity_pause_manual_login_and_explicit_resume(self):
        from playwright.sync_api import expect
        identity = identity_html()
        state = {'handled': False}

        def router(url):
            if url.endswith('/502.html') and not state['handled']:
                return identity
            return BUSINESS_SKU

        browser = self.browser()
        ui = self.ui_page(browser)
        detail_page, visits = self.detail_stack(browser, router)
        clock = self.wait_clock()
        profile = str(self.cfg.user_data_path)

        # 开始页手动入口：只打开同一配置解析出的专用 Edge，不建轮、不采集
        ui.click('#loginBrowserBtn')
        ui.wait_for_function("document.querySelector('#browserFacts').textContent.includes('占用')")
        self.assertEqual(len(self.os.starts), 1)
        self.assertEqual(self.os.starts[0].profile, profile)
        self.assertNotIn('账号已登录', ui.locator('#browserFacts').inner_text())
        with closing(connect(self.cfg.db_file)) as conn:
            self.assertEqual(rounds.pending_rounds(Database(conn)), ())
            self.assertIsNone(Database(conn).crawler_process())
        # 用户自行关闭专用窗口后，开始门禁放开
        self.os.state = 'free'

        # 开始：真实按钮携带明确新轮意图与同一专用目录快照
        cmd, start_intent = self.start_via_page(ui)
        self.assertEqual(start_intent.request.shop_keys, ('A', 'B', 'C', 'D'))
        settings = json.loads(cmd[cmd.index('--gui-browser-settings') + 1])
        self.assertEqual(settings['profile'], profile)

        # 采集（测试代班建轮/榜单）：A/B/C 各两件正常完成，501 完成后 502 是真实身份验证页
        rid = self.seed_round(
            offers={'A': ['A1', 'A2'], 'B': ['B1', 'B2'], 'C': ['C1', 'C2'],
                    'D': ['501', '502', '503']})
        self.attach_child(rid)
        acted = []

        def click_pause():
            if clock.now >= 3 and not acted:
                ui.evaluate("showTab('run'); tickRun()")
                expect(ui.locator('#rProgressLine')).to_contain_text('身份验证')
                expect(ui.locator('#rProgressLine')).to_contain_text('0.1 分钟')
                self.ui_pause(ui)
                acted.append(True)

        clock.on_sleep = click_pause
        pipeline.run_round(self.run_cfg(), self.shops, elapsed_clock=clock.monotonic)
        self.assertEqual(acted, [True], '真实身份验证等待中必须真正点击暂停')
        def detail_url(offer):
            return f'https://detail.1688.com/offer/{offer}.html'
        self.assertEqual(visits, [detail_url(o) for o in
                                  ('A1', 'A2', 'B1', 'B2', 'C1', 'C2', '501', '502')])
        paused_visits = list(visits)
        run = self.load(rid)
        self.assertIsNone(run.reason, '暂停保留本轮，不写终态')
        events = self.verification_events(rid)
        self.assertEqual([e[0] for e in events], ['verification_appear'],
                         '用户暂停先于页面变化，不发解决事件')
        self.child.poll.return_value = 0
        self.assertTrue(self.api.get_run()['can_resume'])

        # 过程页手动入口：只开窗口；开着时继续被拒且没有任何自动采集
        ui.evaluate('tickRun()')
        expect(ui.locator('#runLoginBrowserBtn')).to_be_enabled()
        frozen_visits = list(visits)
        ui.click('#runLoginBrowserBtn')
        ui.wait_for_function("document.querySelector('#runBrowserFacts').textContent.includes('占用')")
        self.assertEqual(len(self.os.starts), 2)
        self.assertEqual(self.os.starts[1].profile, profile)
        self.assertNotIn('账号已登录', ui.locator('#runBrowserFacts').inner_text())
        self.assertIn('占用', self.api.get_run()['continue_gate'])
        ui.evaluate('tickRun()')
        self.assertTrue(ui.locator('#resumeBtn').is_disabled())
        self.assertEqual(list(visits), frozen_visits, '手动操作不自动采集')
        # 用户关闭手动窗口：也不会自动继续
        self.os.state = 'free'
        ui.evaluate('tickRun()')
        self.assertIsNone(self.api.get_run()['continue_gate'])
        self.assertEqual(list(visits), frozen_visits)

        # 明确继续：原轮 ID 与完整范围，真实调度只执行剩余任务
        state['handled'] = True   # 用户在手动窗口里处理完身份验证
        with patch.object(gui.subprocess, 'Popen') as spawn:
            ui.locator('#resumeBtn').click()
            ui.wait_for_timeout(100)
        cmd, raw = self.spawn_intent(spawn, '--resume-round-intent')
        self.assertNotIn('--new-round-intent', cmd)
        intent = rounds.ResumeRoundIntent.from_json(raw)
        self.assertEqual(intent.round_id, rid)
        self.assertEqual(intent.request.shop_keys, ('A', 'B', 'C', 'D'))
        visits.clear()
        clock.on_sleep = lambda: None
        pipeline.run_round(self.run_cfg(), [Shop(s.key, s.name, s.url) for s in intent.request.shops],
                           resume_intent=intent)
        self.assertEqual(visits, [detail_url('502'), detail_url('503')],
                         '前三店与已完成的 501 不重复执行，只访问剩余任务')
        run = self.load(rid)
        self.assertEqual(run.reason, rounds.TerminalReason.COMPLETED)
        self.assertEqual(run.shop_keys, ('A', 'B', 'C', 'D'))
        self.assertEqual(self.api.get_run()['can_resume'], False)
        print('TICKET07_JA01_CHAIN ' + json.dumps({
            'def_profiles': sorted({s.profile for s in self.os.starts}),
            'manual_starts': len(self.os.starts),
            'start_scope': list(start_intent.request.shop_keys),
            'paused_round': rid,
            'pre_pause_navigation': [v.rsplit('/', 1)[-1] for v in paused_visits],
            'resume_scope': list(intent.request.shop_keys),
            'resumed_navigation': [v.rsplit('/', 1)[-1] for v in visits],
            'members': list(run.shop_keys),
            'reason': run.reason.value, 'offline': True}, ensure_ascii=False))

    # ---------- J-A02 ----------
    def test_ja02_identity_timeout_ends_round_and_new_run_waits_for_explicit_start(self):
        identity = identity_html()
        browser = self.browser()
        ui = self.ui_page(browser)
        detail_page, visits = self.detail_stack(browser, lambda url: identity)
        clock = self.wait_clock()

        cmd, intent = self.start_via_page(ui)
        self.assertEqual(intent.request.shop_keys, ('A', 'B', 'C', 'D'))
        rid = self.seed_round(offers={'A': ['501', '502'], 'B': ['601'],
                                      'C': ['C1'], 'D': ['D1']})
        self.attach_child(rid)

        # 配置时限（0.1 分钟）到点：真实等待超时自动中止，不访问下一任务/店铺
        pipeline.run_round(self.run_cfg(), self.shops, elapsed_clock=clock.monotonic)
        self.assertEqual(visits, ['https://detail.1688.com/offer/501.html'])
        run = self.load(rid)
        self.assertEqual(run.reason.value, 'IDENTITY_VERIFICATION_TIMEOUT')
        self.assertFalse(run.resumable_on(run.started_at))
        events = self.verification_events(rid)
        self.assertEqual([e[0] for e in events], ['verification_appear', 'verification_timeout'])
        self.assertEqual([e[1] for e in events], ['identity', 'identity'])
        # 数据保留
        self.assertEqual(self.dbconn().round_tally(rid).discovered, 5)
        # 界面：自动中止准确呈现；超时轮不能恢复；收尾未结清前拒绝新建
        result = self.api.get_result()
        self.assertEqual(result['tag'], '身份验证超时中止')
        self.assertIn('自动中止', result['note'])
        self.assertFalse(self.api.get_run().get('can_resume', False))
        self.assertFalse(self.api.resume_run()['ok'])
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertFalse(self.api.start_run(['A', 'B', 'C', 'D'])['ok'])
            spawn.assert_not_called()
        self.assertEqual(rounds.latest(self.dbconn()).id, rid, '没有自动下一轮')
        # 子进程退出、收尾结清后：仍需用户明确新建，且携带明确意图
        self.child.poll.return_value = 0
        self.assertTrue(self.api.get_start()['start_allowed'])
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertTrue(self.api.start_run(['A', 'B'])['ok'])
        cmd, raw = self.spawn_intent(spawn, '--new-round-intent')
        self.assertEqual(rounds.NewRoundIntent.from_json(raw).request.shop_keys, ('A', 'B'))
        print('TICKET07_JA02_TIMEOUT ' + json.dumps({
            'timed_out_round': rid, 'reason': 'IDENTITY_VERIFICATION_TIMEOUT',
            'wait_limit_minutes': 0.1,
            'visited_before_stop': [v.rsplit('/', 1)[-1] for v in visits],
            'resumable': False, 'new_run_scope': 'A/B', 'offline': True}, ensure_ascii=False))

    # ---------- J-A03 ----------
    def test_ja03_confirmed_close_versus_abnormal_exit_reopen(self):
        from bestseller_monitor.crawler_close import install_close_guard
        from test_crawler_close import Window

        # 明确暂停 → 确认正常关窗 → 重开：没有旧轮续跑入口，可合法新建
        rid = self.seed_round(offers={'A': ['501'], 'B': ['601']})
        self.attach_child(rid, pid=4321)   # 本段没有真实采集：身份行与界面句柄同用假 PID
        ui = self.ui_page(self.browser())
        ui.evaluate("showTab('run'); tickRun()")
        self.ui_pause(ui)
        self.child.poll.return_value = 0
        self.assertTrue(self.api.get_run()['can_resume'])
        window, tasks = Window(), []
        install_close_guard(window, self.api, confirm=lambda text: True,
                            notify=lambda text: None, title='test',
                            launch=tasks.append, wait=lambda sec: self.clock.advance(9))
        self.assertFalse(window.close())
        tasks.pop()()
        self.assertTrue(window.destroyed)
        self.assertEqual(self.load(rid).reason, rounds.TerminalReason.ABANDONED,
                         '确认关窗结束原轮，不把正常关窗当暂停保留')
        reopened = gui.Api(config_file=self.config, browser_runtime=PausedBrowserOS(),
                           stop_clock=StopClock(), now=lambda: FROZEN_NOW)
        self.assertFalse(reopened.get_run().get('can_resume', False))
        self.assertFalse(reopened.resume_run()['ok'])
        self.assertTrue(reopened.get_start()['start_allowed'])
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertTrue(reopened.start_run(['A'])['ok'])

        # 异常退出 → 重开：按「未正常结束」呈现，显式中止旧轮后才能新建
        crashed = self.seed_round(offers={'C': ['C1']})
        reopened2 = gui.Api(config_file=self.config, browser_runtime=PausedBrowserOS(),
                            stop_clock=StopClock(), now=lambda: FROZEN_NOW)
        dead = patch('bestseller_monitor.browser_proc.process_alive',
                     side_effect=lambda pid: pid != 4321)
        with dead:
            start = reopened2.get_start()
            run = reopened2.get_run()
            resolved = reopened2.resume_run()
        rows = {r['id']: r for r in start['pending']['rounds']}
        self.assertIn(crashed, rows)
        self.assertIn('采集已停止，本轮未正常结束', rows[crashed]['text'])
        self.assertNotIn('已暂停', rows[crashed]['text'])
        self.assertFalse(start['start_allowed'])
        self.assertFalse(run.get('manually_paused', False))
        self.assertFalse(run.get('can_resume', False))
        self.assertFalse(resolved['ok'], '异常退出不给普通恢复')
        self.assertIsNone(self.load(crashed).reason, '读取本身不写终态，也不冒充用户中止')
        with dead:
            prepared = reopened2.prepare_abort(crashed)
            with patch('bestseller_monitor.browser_proc.close_browser') as close:
                done = reopened2.confirm_abort(prepared['confirmation'])
                close.assert_not_called()
        self.assertTrue(done['ok'], done)
        self.assertEqual(self.load(crashed).reason, rounds.TerminalReason.ABANDONED)
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertTrue(reopened2.start_run(['C'])['ok'])
        print('TICKET07_JA03_REOPEN ' + json.dumps({
            'confirmed_close': {'round': rid, 'reason': 'ABANDONED', 'resume_offered': False},
            'abnormal_exit': {'round': crashed, 'shown_as': '采集已停止，本轮未正常结束',
                              'reason_written_by': 'explicit confirm only', 'resume_offered': False},
            'offline': True}, ensure_ascii=False))

    # ---------- J-A04 ----------
    def test_ja04_four_shop_original_scope_resumes_and_unproven_history_is_disposed(self):
        # 原始条件：A/B/C/D 原轮；A/B/C 今天已采满（各 30 件），开始页默认只剩 D
        rid = self.seed_round(
            offers={**{k: [str(i) for i in range(30)] for k in 'ABC'}, 'D': ['501', '502']},
            today_rows=[(k, str(i), FROZEN_DATE) for k in 'ABC' for i in range(30)])
        identity = identity_html()
        state = {'restored': False}

        def router(url):
            if url.endswith('/502.html') and not state['restored']:
                return identity
            return BUSINESS_SKU

        browser = self.browser()
        ui = self.ui_page(browser)
        detail_page, visits = self.detail_stack(browser, router)
        clock = self.wait_clock()
        self.attach_child(rid)

        # 开始页直接请求旧轮（默认只剩 D 的勾选）不作为普通续跑：拒绝并给出处置路径
        before = self.api.get_start()
        self.assertEqual([s['key'] for s in before['shops'] if s['default_checked']], ['D'])
        self.assertFalse(before['start_allowed'])
        with patch.object(gui.subprocess, 'Popen') as spawn:
            refused = self.api.start_run(['D'])
            spawn.assert_not_called()
        self.assertFalse(refused['ok'])

        # 用户的正确路径：过程页明确暂停（真实点击，落在 502 的身份验证等待中）
        def click_pause():
            if clock.now >= 3 and not acted:
                ui.evaluate("showTab('run'); tickRun()")
                self.ui_pause(ui)
                state['restored'] = True
                detail_page.set_content(BUSINESS_SKU)
                acted.append(True)

        acted = []
        clock.on_sleep = click_pause
        pipeline.run_round(self.run_cfg(), self.shops, elapsed_clock=clock.monotonic)
        self.assertEqual(acted, [True], '新流程必须真正点击暂停')
        self.assertEqual(visits, ['https://detail.1688.com/offer/501.html',
                                  'https://detail.1688.com/offer/502.html'])
        self.child.poll.return_value = 0
        self.assertTrue(self.api.get_run()['can_resume'])

        # 暂停后开始页仍不提供换范围直接开始；正确入口是过程页继续
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertFalse(self.api.start_run(['D'])['ok'])
            spawn.assert_not_called()
        with patch.object(gui.subprocess, 'Popen') as spawn:
            ui.evaluate('tickRun()')
            ui.locator('#resumeBtn').click()
            ui.wait_for_timeout(100)
        cmd, raw = self.spawn_intent(spawn, '--resume-round-intent')
        intent = rounds.ResumeRoundIntent.from_json(raw)
        self.assertEqual(intent.round_id, rid)
        self.assertEqual(intent.request.shop_keys, ('A', 'B', 'C', 'D'),
                         '续跑携带原轮完整范围，不携带默认 D 子集')

        # 真实调度：只执行剩余任务，前三店与已完成任务不重复
        visits.clear()
        clock.on_sleep = lambda: None
        pipeline.run_round(self.run_cfg(), [Shop(s.key, s.name, s.url) for s in intent.request.shops],
                           resume_intent=intent)
        self.assertEqual(visits, ['https://detail.1688.com/offer/502.html'])
        run = self.load(rid)
        self.assertEqual(run.reason, rounds.TerminalReason.COMPLETED)
        self.assertEqual(run.shop_keys, ('A', 'B', 'C', 'D'))
        self.assertEqual(rounds.latest(self.dbconn()).id, rid)
        self.assertEqual(self.dbconn().round_tally(rid).handled, 92)   # 前三店 90 + D 2
        print('TICKET07_JA04_FOUR_SHOP ' + json.dumps({
            'original_conditions': {'round_scope': ['A', 'B', 'C', 'D'],
                                    'satisfied_today': ['A', 'B', 'C'],
                                    'default_checked': ['D']},
            'old_start_path': 'refused with disposal explanation',
            'resume_scope': list(intent.request.shop_keys),
            'resumed_navigation': [v.rsplit('/', 1)[-1] for v in visits],
            'handled': 92, 'members': list(run.shop_keys), 'reason': run.reason.value,
            'offline': True}, ensure_ascii=False))

        # 无暂停依据的历史记录：跨天遗留显式处置，不伪造历史暂停
        legacy = self.seed_round(offers={'D': ['701']}, day='2026-09-13')
        reopened = gui.Api(config_file=self.config, browser_runtime=PausedBrowserOS(),
                           stop_clock=StopClock(), now=lambda: FROZEN_NOW)
        dead = patch('bestseller_monitor.browser_proc.process_alive',
                     side_effect=lambda pid: pid != 4321)
        with dead:
            start = reopened.get_start()
            run = reopened.get_run()
            rows = {r['id']: r for r in start['pending']['rounds']}
            self.assertIn(legacy, rows)
            self.assertTrue(rows[legacy]['cross_day'])
            self.assertIn('已跨天', rows[legacy]['text'])
            self.assertNotIn('已暂停', rows[legacy]['text'])
            self.assertFalse(start['start_allowed'])
            self.assertFalse(run.get('manually_paused', False))
            self.assertFalse(reopened.resume_run()['ok'])
            self.assertIsNone(self.load(legacy).reason)
            prepared = reopened.prepare_abort(legacy)
            with patch('bestseller_monitor.browser_proc.close_browser') as close:
                done = reopened.confirm_abort(prepared['confirmation'])
                close.assert_not_called()
        self.assertTrue(done['ok'], done)
        self.assertEqual(self.load(legacy).reason, rounds.TerminalReason.ABANDONED)
        with patch.object(gui.subprocess, 'Popen') as spawn:
            self.assertTrue(reopened.start_run(['D'])['ok'])

    # ---------- 小工具 ----------
    def dbconn(self):
        conn = connect(self.cfg.db_file)
        self.addCleanup(conn.close)
        return Database(conn)

    def verification_events(self, rid):
        with closing(connect(self.cfg.db_file)) as conn:
            return conn.execute(
                'SELECT event, verification_type FROM event_log '
                'WHERE round_id=? AND kind=? ORDER BY id', (rid, 'verification')).fetchall()
