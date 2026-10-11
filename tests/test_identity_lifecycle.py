"""Ticket 02: real offline DOM, real waits and public round lifecycle."""
from __future__ import annotations
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from bestseller_monitor import guard, waiting

PRODUCT = 'https://detail.1688.com/offer/521844920352.html'
FIXTURE = Path(__file__).with_name('fixtures') / 'identity-verification.html'
BUSINESS = '<html><head><title>商品详情</title></head><body><div id="offer-detail"><h1>测试商品</h1></div></body></html>'

class Clock:
    def __init__(self):
        self.now = 0.0
        self.on_sleep = lambda: None
    def monotonic(self): return self.now
    def time(self): return self.now
    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()

class IdentityLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
    def setUp(self):
        self.context = self.browser.new_context(java_script_enabled=False)
        self.addCleanup(self.context.close)
        self.html = Path(os.environ.get('IDENTITY_SOURCE', FIXTURE)).read_text(encoding='utf-8-sig')
        self.requests = []
        def route(r):
            self.requests.append(r.request.url)
            if r.request.is_navigation_request() and r.request.resource_type == 'document':
                r.fulfill(status=200, content_type='text/html; charset=utf-8', body=self.html)
            else: r.abort()
        self.context.route('**/*', route)
        self.page = self.context.new_page()
        self.page.goto(PRODUCT, wait_until='domcontentloaded')
        self.clock = Clock()
        self.enterContext(patch.object(guard, 'time', self.clock))
        self.enterContext(patch.object(waiting, 'time', self.clock))
        self.alarm = self.enterContext(patch.object(guard.sound, 'play_alarm'))
        self.events = []
    def emit(self, event, **fields): self.events.append((event, fields))
    def test_archived_identity_at_product_url_without_captcha_is_not_resolved(self):
        self.assertIn('为确认是你本人操作，请完成以下验证', guard.body_text(self.page))
        self.assertFalse(guard.captcha_visible(self.page))
        self.assertEqual(guard.intervention_kind(self.page), '身份验证')
        self.assertEqual(guard.vtype(guard.intervention_kind(self.page)), 'identity')
        self.assertFalse(guard.resolved(self.page))
    def test_unknown_or_closed_page_never_proves_resolution(self):
        self.page.set_content('<html><head><title>加载中</title></head><body></body></html>')
        self.assertFalse(guard.resolved(self.page), 'no business proof is not resolution')
        self.page.close()
        self.assertFalse(guard.resolved(self.page))
        with self.assertRaises(guard.RoundPauseRequired):
            guard.intervention_kind(self.page)
    def test_a_rendering_detail_popup_settles_instead_of_pausing_the_round(self):
        """票 01（回归）：刚打开的详情页还是空壳（complete、正文空）时，访问就绪窗口把它
        等出来，而不是把整轮停掉——改前红：PageEvidenceUnavailable 当场穿透 begin_detail_visit。"""
        from bestseller_monitor import detail_visit
        from helpers import crawler_cfg
        self.page.set_content('<html><head><title>加载中</title></head><body></body></html>')

        def render():
            if self.clock.now >= 1:
                self.page.set_content(BUSINESS)
                self.clock.on_sleep = lambda: None
        self.clock.on_sleep = render

        visit = detail_visit.begin_detail_visit(
            lambda: detail_visit.OpenedDetail(self.page), crawler_cfg())

        self.assertIsInstance(visit, detail_visit.ReadyDetailVisit)
        self.assertGreaterEqual(self.clock.now, 1.0, '空壳期间在等，渲染后才判读')
        self.alarm.assert_not_called()
    def test_confirmed_identity_waits_without_reload_then_emits_solution(self):
        def change_page():
            if self.clock.now >= 5:
                self.page.set_content(BUSINESS)
        self.clock.on_sleep = change_page
        guard.wait_for_resolution(self.page, 0.1, emit=self.emit,
                                  verification_type='identity', confirm_sec=2)
        self.assertEqual(self.requests, [PRODUCT], 'identity must never reload')
        self.assertEqual([e for e, _ in self.events], ['verification_appear', 'verification_solved'])
        self.assertEqual(self.events[0][1]['verification_type'], 'identity')
        self.assertIn('wait_minutes=0.1', self.events[0][1]['note'])
        self.assertGreaterEqual(self.clock.now, 5)
    def test_type_changes_and_unknown_keep_one_deadline_including_notice_cost(self):
        def change():
            if self.clock.now < 5:
                self.page.set_content('<body>请登录后查看</body>')
            elif self.clock.now < 7:
                self.page.set_content('<body>拖动下方滑块</body>')
            else:
                self.page.set_content('<body></body>')
        def emit(event, **fields):
            self.emit(event, **fields)
            if event == 'verification_appear':
                self.clock.sleep(2)  # UI/event/alert overhead belongs to the same deadline.
                self.clock.on_sleep = change
        with self.assertRaises(guard.InterventionTimeout) as timeout:
            guard.wait_for_resolution(self.page, 0.1, emit=emit,
                                      verification_type='identity', confirm_sec=2)
        self.assertEqual(self.clock.now, 8, '2 sec confirmation + fixed 6 sec formal wait')
        self.assertEqual(type(timeout.exception).__name__, 'IdentityVerificationTimeout')
        self.assertEqual([e for e, _ in self.events], ['verification_appear', 'verification_timeout'])
        self.assertEqual(self.requests, [PRODUCT])
    def prepare_round(self):
        from helpers import crawler_cfg, isolated_locks
        from bestseller_monitor import browser_pw, pipeline, rounds
        from bestseller_monitor.config import Shop
        from bestseller_monitor.db import Database, connect, cst_date
        self.enterContext(isolated_locks())
        temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db_path = temp / 'round.db'
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.shops = [Shop(k, k, f'https://offline.example/{k}') for k in ('A', 'B')]
        opened = rounds.open(self.db, rounds.RoundRequest(cst_date(), tuple(
            rounds.ShopScope(s.key, s.url, s.name) for s in self.shops)))
        self.rid = opened.round.id
        for shop, offers in zip(self.shops, [[(1, '521844920352', PRODUCT, 'one', '1'),
                    (2, '222', 'https://detail.1688.com/offer/222.html', 'two', '1')],
                    [(1, '333', 'https://detail.1688.com/offer/333.html', 'three', '1')]]):
            self.db.save_shop_offers(self.rid, shop.key, shop.url, shop.name, offers, 1)
        self.cfg = crawler_cfg(db_file=self.db_path, root=temp, data_dir=temp/'data',
            raw_page_dir=temp/'raw', screenshot_dir=temp/'screenshots',
            logs_dir=temp/'logs', exchange_root=temp/'exchange', user_data_path=temp/'profile',
            ensure_dirs=lambda: None, start_browser=False, human_pause_minutes=0.1,
            intervention_confirmation_sec=2)
        def open_session(cfg, *, publish_browser):
            publish_browser('BORROWED', 9222, None, None)
            return None, None, self.page, self.context
        self.enterContext(patch.object(browser_pw, 'open_session', side_effect=open_session))
        self.enterContext(patch.object(browser_pw, 'close_session'))
        self.requests.clear()
        return pipeline, rounds

    def test_real_wait_timeout_ends_round_preserves_data_and_never_visits_next_task(self):
        pipeline, rounds = self.prepare_round()
        pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        run = rounds.load(self.db, self.rid)
        self.assertIsNotNone(run.reason, 'timeout must end rather than pause the round')
        self.assertEqual(run.reason.value, 'IDENTITY_VERIFICATION_TIMEOUT')
        self.assertFalse(run.resumable_on(run.started_at))
        self.assertEqual(rounds.latest(self.db).id, self.rid)
        self.assertEqual(self.requests, [PRODUCT])
        self.assertEqual(self.db.round_tally(self.rid).discovered, 3)
        events = self.conn.execute('SELECT event, verification_type FROM event_log WHERE round_id=? AND kind=? ORDER BY id', (self.rid, 'verification')).fetchall()
        self.assertEqual([tuple(e) for e in events], [('verification_appear', 'identity'), ('verification_timeout', 'identity')])
        self.assertEqual(self.clock.now, 8)
    def test_business_page_for_another_target_does_not_release_identity_wait(self):
        changed = []
        def change():
            if self.clock.now >= 8:
                self.html = BUSINESS
                self.page.goto(PRODUCT, wait_until='domcontentloaded')
            elif self.clock.now >= 3 and not changed:
                changed.append(True)
                self.html = BUSINESS
                self.page.goto('https://detail.1688.com/offer/222.html', wait_until='domcontentloaded')
        self.clock.on_sleep = change
        guard.wait_for_resolution(self.page, 0.2, emit=self.emit,
                                  verification_type='identity', confirm_sec=2)
        self.assertGreaterEqual(self.clock.now, 8, 'only current target business restoration solves')
        self.assertEqual(self.page.url, PRODUCT)
    def test_user_pause_during_wait_wins_over_later_business_restoration(self):
        import os
        from types import SimpleNamespace
        from test_gui import gui_api
        pipeline, rounds = self.prepare_round()
        api = gui_api(shops=self.shops, db_path=self.db_path)
        api.round_id = self.rid
        api.proc = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        acted = []
        def pause():
            if self.clock.now >= 3 and not acted:
                acted.append(api.pause_run())
                self.page.set_content(BUSINESS)
        self.clock.on_sleep = pause
        pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        self.assertEqual([r['ok'] for r in acted], [True])
        self.assertIsNone(rounds.load(self.db, self.rid).reason)
        events = self.conn.execute('SELECT event FROM event_log WHERE round_id=? AND kind=?', (self.rid, 'verification')).fetchall()
        self.assertEqual([e[0] for e in events], ['verification_appear'], 'user pause cannot emit solved')
        self.assertEqual(self.requests, [PRODUCT])

    def test_normal_business_quote_and_decorated_url_are_not_identity(self):
        self.html = BUSINESS.replace('商品详情', '商品—身份验证说明').replace('测试商品', '为确认是你本人操作，请完成以下验证')
        self.page.goto(PRODUCT + '/_____tmd_____/punish?x5secdata=test', wait_until='domcontentloaded')
        self.assertIsNone(guard.intervention_kind(self.page))
        self.assertTrue(guard.resolved(self.page, target_url=PRODUCT))
    def test_browser_cleanup_failure_cannot_hide_real_timeout_or_release_gate(self):
        from bestseller_monitor import browser_pw, views
        from bestseller_monitor.db import utcnow
        from test_gui import gui_api
        pipeline, rounds = self.prepare_round()
        def fail_close(pw, br, *, publish_browser):
            publish_browser('UNKNOWN', 9222, None, None)
            raise OSError('offline close boundary denied')
        with patch.object(browser_pw, 'close_session', side_effect=fail_close):
            pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        run = rounds.load(self.db, self.rid)
        self.assertEqual(run.reason.value, 'IDENTITY_VERIFICATION_TIMEOUT')
        self.assertEqual(self.db.crawler_process()['browser_state'], 'UNKNOWN')
        result = views.result_view(self.conn, state=views.UiState(round_id=self.rid, stopping='verifying'), now=utcnow())
        self.assertTrue(result['stopping'])
        api = gui_api(shops=self.shops, db_path=self.db_path)
        with patch('gui.subprocess.Popen') as spawn:
            self.assertFalse(api.start_run(['A', 'B'])['ok'])
            spawn.assert_not_called()
    def test_public_views_expose_actual_identity_wait_limit_and_automatic_end_reason(self):
        from bestseller_monitor import views
        from bestseller_monitor.db import utcnow
        pipeline, rounds = self.prepare_round()
        seen = []
        def observe():
            if self.clock.now >= 3:
                seen.append(views.run_view(self.conn, state=views.UiState(round_id=self.rid, crawler_running=True), now=utcnow()))
        self.clock.on_sleep = observe
        pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        self.assertTrue(seen)
        self.assertEqual(seen[0].get('verification', {}).get('type'), 'identity')
        self.assertEqual(seen[0]['verification']['wait_minutes'], 0.1)
        self.assertIn('身份验证', seen[0]['verification']['note'])
        result = views.result_view(self.conn, state=views.UiState(round_id=self.rid), now=utcnow())
        self.assertEqual(result['tag'], '身份验证超时中止')
        self.assertIn('自动中止', result['note'])
        self.assertFalse(views.run_view(self.conn, state=views.UiState(round_id=self.rid), now=utcnow()).get('verification'))

    def test_owned_cleanup_failure_retains_exact_process_binding(self):
        from bestseller_monitor import browser_pw
        pipeline, rounds = self.prepare_round()
        def owned(cfg, *, publish_browser):
            publish_browser('OWNED', 9222, 123456, 'offline-creation-proof')
            return None, None, self.page, self.context
        def fail(*args, **kwargs):
            raise OSError('cannot close owned test boundary')
        with patch.object(browser_pw, 'open_session', side_effect=owned), patch.object(browser_pw, 'close_session', side_effect=fail):
            pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        identity = self.db.crawler_process()
        self.assertEqual((identity['browser_state'], identity['browser_pid'], identity['browser_os_started']),
                         ('UNKNOWN', 123456, 'offline-creation-proof'))
        self.assertEqual(rounds.load(self.db, self.rid).reason.value, 'IDENTITY_VERIFICATION_TIMEOUT')

    def test_shipped_page_shows_actual_identity_wait_and_user_pause_works(self):
        import os
        from types import SimpleNamespace
        from test_gui import gui_api
        from playwright.sync_api import expect
        pipeline, rounds = self.prepare_round()
        api = gui_api(shops=self.shops, db_path=self.db_path)
        api.round_id = self.rid
        api.proc = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=(Path(__file__).resolve().parents[1] / 'bestseller_monitor/pages/ui_live.html').read_text(encoding='utf-8')) if route.request.is_navigation_request() else route.abort())
        ui = context.new_page()
        ui.expose_function('ticketApi', lambda name, args: getattr(api, name)(*args))
        ui.add_init_script("window.pywebview={api:new Proxy({}, {get:(_, name)=>(...args)=>window.ticketApi(name,args)})};window.alert=()=>{};")
        ui.goto((Path(__file__).resolve().parents[1] / 'bestseller_monitor/pages/ui_live.html').as_uri())
        acted = []
        def observe():
            if self.clock.now >= 3 and not acted:
                ui.evaluate("showTab('run'); tickRun()")
                expect(ui.locator('#rProgressLine')).to_contain_text('身份验证')
                expect(ui.locator('#rProgressLine')).to_contain_text('0.1 分钟')
                ui.evaluate('refreshStart()')
                expect(ui.locator('#loginBrowserBtn')).to_be_disabled()
                expect(ui.locator('#startBtn')).to_be_disabled()
                self.assertFalse(api.open_login_browser()['ok'])
                self.assertFalse(api.start_run(['A', 'B'])['ok'])
                ui.locator('#pauseBtn').click()
                ui.wait_for_function("document.querySelector('#rStopping').style.display !== 'none'")
                acted.append(True)
        self.clock.on_sleep = observe
        pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        self.assertEqual(acted, [True])
        self.assertIsNone(rounds.load(self.db, self.rid).reason)
        self.assertEqual(self.requests, [PRODUCT])

    def test_reliable_restore_finishes_remaining_tasks_in_original_round(self):
        pipeline, rounds = self.prepare_round()
        html = BUSINESS.replace('</body>', '<script>{"skuInfoMap":{"red":{"skuId":"red","name":"red","price":10,"canBookCount":3}}}</script></body>')
        def restore():
            if self.clock.now >= 5:
                self.html = html
                self.page.set_content(html)
        self.clock.on_sleep = restore
        pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        self.assertEqual(rounds.load(self.db, self.rid).reason, rounds.TerminalReason.COMPLETED)
        self.assertEqual(rounds.latest(self.db).id, self.rid)
        self.assertEqual(self.db.round_tally(self.rid).handled, 3)
        self.assertEqual(self.requests, [PRODUCT, 'https://detail.1688.com/offer/222.html', 'https://detail.1688.com/offer/333.html'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event_log WHERE event='verification_solved'").fetchone()[0], 1)

    def test_redirected_identity_restores_original_target_and_finishes_round(self):
        pipeline, rounds = self.prepare_round()
        verification = 'https://detail.1688.com/identity'
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        restored = []
        sku_html = BUSINESS.replace('</body>', '<script>{"skuInfoMap":{"red":{"skuId":"red","name":"red","price":10,"canBookCount":3}}}</script></body>')
        self.context.unroute('**/*')
        def route(r):
            self.requests.append(r.request.url)
            if not r.request.is_navigation_request():
                r.abort()
            elif r.request.url == PRODUCT and not restored:
                r.fulfill(content_type='text/html; charset=utf-8', body=self.html + '<script>history.replaceState(null, \"\", \"/identity\")</script>')
            else:
                r.fulfill(content_type='text/html; charset=utf-8', body=sku_html if restored else self.html)
        self.context.route('**/*', route)
        def restore():
            if self.clock.now >= 5 and not restored:
                self.assertEqual(self.page.url, verification)
                restored.append(True)
                self.page.goto(PRODUCT, wait_until='domcontentloaded')
        self.clock.on_sleep = restore
        pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        self.assertEqual(rounds.load(self.db, self.rid).reason, rounds.TerminalReason.COMPLETED, str(self.events) + str(self.requests))
        self.assertEqual(rounds.latest(self.db).id, self.rid)
        self.assertEqual(self.db.round_tally(self.rid).handled, 3)
        self.assertEqual(self.requests, [PRODUCT, PRODUCT,
            'https://detail.1688.com/offer/222.html', 'https://detail.1688.com/offer/333.html'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event_log WHERE event='verification_solved'").fetchone()[0], 1)

    def test_redirected_listing_restores_requested_shop_page(self):
        from types import SimpleNamespace
        from bestseller_monitor import listing
        self.prepare_round()
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        shop_url = 'https://shop.offline.example/sales'
        restored = []
        business = '<body><div class="offer-list"><img class="main-picture" width="40" height="40">测试商品</div></body>'
        self.context.route('**/*', lambda r: r.fulfill(content_type='text/html; charset=utf-8', body=(business if restored else self.html + '<script>history.replaceState(null, "", "/identity")</script>')) if r.request.is_navigation_request() else r.abort())
        appeared = []
        def emit(event, **fields):
            self.emit(event, **fields)
            if event == 'verification_appear': appeared.append(self.clock.now)
        def restore():
            if appeared and self.clock.now >= appeared[0] + 3 and not restored:
                self.assertEqual(self.page.url, 'https://shop.offline.example/identity')
                restored.append(True)
                self.page.goto(shop_url, wait_until='domcontentloaded')
        self.clock.on_sleep = restore
        human = SimpleNamespace(after_load=lambda: None, before_action=lambda: None)
        listing.prepare(self.page, shop_url, self.cfg, human, describe='offline shop', emit=emit)
        self.assertEqual(self.page.url, shop_url)
        self.assertEqual([e for e, _ in self.events], ['verification_appear', 'verification_solved', 'list_load'])

    def test_redirected_popup_restores_clicked_product_identity(self):
        self.redirected_card_restoration(new_tab=True)

    def test_redirected_in_place_click_restores_clicked_product_identity(self):
        self.redirected_card_restoration(new_tab=False)

    def redirected_card_restoration(self, *, new_tab):
        from types import SimpleNamespace
        from bestseller_monitor import click_listing, detail_visit
        self.prepare_round()
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        restored = []
        def route(r):
            if not r.request.is_navigation_request(): r.abort()
            elif r.request.url == 'https://shop.offline.example/sales':
                r.fulfill(content_type='text/html; charset=utf-8', body=f'<a href="{PRODUCT}" target="{"_blank" if new_tab else "_self"}"><img class="main-picture" width="40" height="40"></a>')
            else:
                r.fulfill(content_type='text/html; charset=utf-8', body=BUSINESS if restored else self.html + '<script>history.replaceState(null, "", "/identity")</script>')
        self.context.route('**/*', route)
        self.page.goto('https://shop.offline.example/sales', wait_until='domcontentloaded')
        self.cfg.timeout_ms = 3000  # Actual offline popup, not the 1ms legacy fake default.
        owner = click_listing.PlaywrightListing(self.page, self.shops[0], self.cfg, SimpleNamespace())
        owner.page_no = 1
        card = owner.card(0)
        appeared = []
        def emit(event, **fields):
            self.emit(event, **fields)
            if event == 'verification_appear': appeared.append(self.clock.now)
        def restore():
            if appeared and self.clock.now >= appeared[0] + 3 and not restored:
                popup = self.context.pages[-1]
                self.assertEqual(popup.url, 'https://detail.1688.com/identity')
                restored.append(True)
                popup.goto(PRODUCT, wait_until='domcontentloaded')
        self.clock.on_sleep = restore
        visit = detail_visit.begin_detail_visit(card.acquire, self.cfg, emit=emit)
        self.assertIsInstance(visit, detail_visit.ReadyDetailVisit)
        self.assertEqual(card.url, PRODUCT)
        self.assertEqual(card.offer_id, '521844920352')
        self.assertEqual([e for e, _ in self.events], ['verification_appear', 'verification_solved'])
        card.close()

    def test_public_export_gap_explains_automatic_identity_timeout(self):
        from bestseller_monitor import exchange
        pipeline, rounds = self.prepare_round()
        pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        text = exchange.gap_reason(self.db, rounds.load(self.db, self.rid).run_date)
        self.assertIn('身份验证', text)
        self.assertIn('自动中止', text)

    def test_elapsed_cleanup_error_cannot_replace_identity_timeout(self):
        from bestseller_monitor import elapsed
        pipeline, rounds = self.prepare_round()
        with patch.object(elapsed.Segment, 'settle', side_effect=OSError('test elapsed cleanup failure')):
            pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        self.assertEqual(rounds.load(self.db, self.rid).reason.value, 'IDENTITY_VERIFICATION_TIMEOUT')

    def test_first_committed_terminal_wins_timeout_race(self):
        from bestseller_monitor.db import Database
        pipeline, rounds = self.prepare_round()
        original = Database.event_logger
        def logger(db, *args, **kwargs):
            emit = original(db, *args, **kwargs)
            def raced(event, **fields):
                if event == 'verification_timeout':
                    rounds.finish(self.db, rounds.load(self.db, self.rid), rounds.TerminalReason.ABANDONED)
                emit(event, **fields)
            return raced
        with patch.object(Database, 'event_logger', logger):
            pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        self.assertEqual(rounds.load(self.db, self.rid).reason, rounds.TerminalReason.ABANDONED)
        self.assertEqual(self.requests, [PRODUCT])

    def test_failed_timeout_terminal_write_never_claims_success_or_starts_new(self):
        from test_gui import gui_api
        pipeline, rounds = self.prepare_round()
        with patch.object(rounds, 'finish_if_open', side_effect=OSError('test terminal write failure')):
            with self.assertRaises(OSError):
                pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        self.assertIsNone(rounds.load(self.db, self.rid).reason)
        api = gui_api(shops=self.shops, db_path=self.db_path)
        with patch('gui.subprocess.Popen') as spawn:
            self.assertFalse(api.start_run(['A', 'B'])['ok'])
            spawn.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT event FROM event_log ORDER BY id DESC LIMIT 1").fetchone()[0], 'verification_timeout')

    def test_business_empty_result_transient_signal_and_read_failures(self):
        from types import SimpleNamespace
        self.page.set_content('<body><div class="empty-result">没有符合条件的商品</div></body>')
        self.assertIsNone(guard.intervention_kind(self.page))
        self.assertTrue(guard.resolved(self.page, target_url=PRODUCT))
        for boundary in ('evaluate', 'locator'):
            proxy = SimpleNamespace(url=self.page.url, evaluate=self.page.evaluate, locator=self.page.locator)
            setattr(proxy, boundary, lambda *a, **k: (_ for _ in ()).throw(OSError('test DOM read failure')))
            with self.subTest(boundary=boundary):
                self.assertFalse(guard.resolved(proxy))
                with self.assertRaises(guard.PageEvidenceUnavailable):
                    guard.intervention_kind(proxy)
        self.page.set_content(self.html)
        self.clock.on_sleep = lambda: self.page.set_content(BUSINESS)
        guard.wait_for_resolution(self.page, .1, emit=self.emit, verification_type='identity', confirm_sec=2)
        self.assertEqual(self.events, [], 'reliable transient recovery precedes formal wait')

    def test_alarm_overhead_cannot_solve_after_frozen_deadline(self):
        def delayed_alarm(count=1):
            self.clock.sleep(10)
            self.page.set_content(BUSINESS)
        with patch.object(guard.sound, 'play_alarm', side_effect=delayed_alarm):
            with self.assertRaises(guard.IdentityVerificationTimeout):
                guard.wait_for_resolution(self.page, .1, emit=self.emit, verification_type='identity', confirm_sec=2)
        self.assertEqual([e for e, _ in self.events], ['verification_appear', 'verification_timeout'])

    def test_confirmed_close_during_identity_wait_abandons_not_solves(self):
        import os, gui
        from types import SimpleNamespace
        from test_gui import gui_api
        from test_crawler_close import Window
        pipeline, rounds = self.prepare_round()
        api = gui_api(shops=self.shops, db_path=self.db_path)
        api.round_id = self.rid
        api.proc = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        window, tasks, messages = Window(), [], []
        gui._install_close_guard(window, api, confirm=lambda text: True,
                                 notify=messages.append, launch=tasks.append)
        acted = []
        def close():
            if self.clock.now >= 3 and not acted:
                acted.append(window.close())
                self.page.set_content(BUSINESS)
        self.clock.on_sleep = close
        pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        self.assertEqual(acted, [False])
        self.assertFalse(window.destroyed, 'native close waits for actual cleanup')
        self.assertEqual(rounds.load(self.db, self.rid).reason, rounds.TerminalReason.ABANDONED)
        self.assertEqual(self.conn.execute("SELECT event FROM event_log WHERE kind='verification'").fetchall()[0][0], 'verification_appear')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event_log WHERE event='verification_solved'").fetchone()[0], 0)
        self.assertEqual(self.requests, [PRODUCT])

    def test_cancelled_close_during_identity_wait_keeps_original_wait(self):
        import os, gui
        from types import SimpleNamespace
        from test_gui import gui_api
        from test_crawler_close import Window
        pipeline, rounds = self.prepare_round()
        api = gui_api(shops=self.shops, db_path=self.db_path)
        api.round_id = self.rid
        api.proc = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        window, tasks = Window(), []
        gui._install_close_guard(window, api, confirm=lambda text: False,
                                 notify=lambda text: None, launch=tasks.append)
        acted = []
        def close():
            if self.clock.now >= 3 and not acted:
                acted.append(window.close())
        self.clock.on_sleep = close
        pipeline.run_round(self.cfg, self.shops, elapsed_clock=self.clock.monotonic)
        self.assertEqual(acted, [False])
        self.assertFalse(window.destroyed)
        self.assertEqual(tasks, [])
        self.assertEqual(rounds.load(self.db, self.rid).reason.value, 'IDENTITY_VERIFICATION_TIMEOUT')
        self.assertEqual(self.clock.now, 8)

    def test_feedback_and_deny_do_not_become_identity_or_resolved(self):
        self.page.set_content('<body><div class="captcha">点我反馈</div></body>')
        with self.assertRaises(guard.PageEvidenceUnavailable):
            guard.intervention_kind(self.page)
        self.assertFalse(guard.resolved(self.page))
        self.page.set_content('<body><div class="captcha">拖动下方滑块 点我反馈</div></body>')
        self.assertEqual(guard.intervention_kind(self.page), '滑块')
        self.html = Path(FIXTURE).read_text(encoding='utf-8')
        self.page.goto('https://s.1688.com/bsop-punish?x=1', wait_until='domcontentloaded')
        from helpers import crawler_cfg
        self.assertTrue(guard.ready_detail_page(self.page, crawler_cfg()))
        self.assertFalse(guard.resolved(self.page))
        self.alarm.assert_not_called()

    def test_nonempty_unknown_page_is_not_initially_usable(self):
        self.page.set_content('<body>正在准备内容，请稍后</body>')
        self.assertFalse(guard.resolved(self.page))
        with self.assertRaises(guard.PageEvidenceUnavailable):
            guard.intervention_kind(self.page)

    def test_identity_form_cannot_release_even_if_prompt_disappears(self):
        self.page.set_content(BUSINESS.replace('</body>', '<form name="validateParams">等待验证码</form></body>'))
        self.assertFalse(guard.resolved(self.page, target_url=PRODUCT))
