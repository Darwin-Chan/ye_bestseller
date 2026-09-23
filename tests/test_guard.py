import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import guard, stop_request, waiting
from bestseller_monitor.guard import (
    DenyTracker,
    InterventionTimeout,
    RoundDenyExceeded,
    ShopDenyExceeded,
    is_deny_url,
    is_login_url,
    is_punish_url,
    ready_detail_page,
    vtype,
)
from helpers import FakePage, GuardClock, crawler_cfg


class GuardTests(unittest.TestCase):
    def test_punish_url_excludes_tmd_decoration(self):
        # 站点会在正常详情 URL 后追加 _____tmd_____/punish?x5secdata=... 上报装饰，不算真验证
        self.assertFalse(
            is_punish_url("https://detail.1688.com/offer/1.html/_____tmd_____/punish?x5secdata=xxx")
        )
        self.assertTrue(is_punish_url("https://x/punish?x5secdata=1"))
        self.assertTrue(is_punish_url("https://x/punishTextFetch"))
        self.assertTrue(is_punish_url("https://x/punish/1"))
        self.assertFalse(is_punish_url("https://detail.1688.com/offer/1.html"))

    def test_deny_and_login(self):
        self.assertTrue(is_deny_url("https://x/bsop-punish-test-webapp/deny_pc.html"))
        self.assertTrue(is_deny_url("https://x/deny_pc"))
        self.assertFalse(is_deny_url("https://x/punish"))   # punish 不算 deny
        self.assertTrue(is_login_url("https://login.1688.com/"))
        self.assertTrue(is_login_url("https://login.taobao.com/"))
        self.assertFalse(is_login_url("https://shop.1688.com/"))

    def test_vtype(self):
        self.assertEqual(vtype("登录墙"), "login")
        self.assertEqual(vtype("滑块"), "slider")
        self.assertEqual(vtype(None), "none")


class ReadyDetailPageTests(unittest.TestCase):
    """详情页打开之后怎么安顿：deny 记账优先、滑块等人次之（候选 04，两条路共用一份）。"""

    CFG = crawler_cfg()
    DENY = "https://s.1688.com/bsop-punish?x=1"

    @staticmethod
    def page(url: str):
        return SimpleNamespace(url=url)

    def test_a_deny_page_is_recorded_and_reported(self):
        tracker = DenyTracker(600)

        denied = ready_detail_page(self.page(self.DENY), self.CFG, deny_tracker=tracker,
                                   shop_key="A01")

        self.assertTrue(denied, "落在 deny 页上")
        self.assertEqual((tracker.shop_count("A01"), tracker.round_count()), (1, 1))

    def test_a_deny_page_is_not_treated_as_a_slider(self):
        """先认 deny：deny 是自动限流，不该走人工介入（改前会去响铃等人，候选 04）。"""
        with patch.object(guard, "wait_for_resolution") as wait:
            ready_detail_page(self.page(self.DENY), self.CFG, deny_tracker=DenyTracker(600),
                              shop_key="A01")

        wait.assert_not_called()

    def test_the_shop_limit_raises_after_recording(self):
        tracker = DenyTracker(600)
        cfg = crawler_cfg(deny_shop_limit=2)

        ready_detail_page(self.page(self.DENY), cfg, deny_tracker=tracker, shop_key="A01")
        with self.assertRaises(ShopDenyExceeded):
            ready_detail_page(self.page(self.DENY), cfg, deny_tracker=tracker, shop_key="A01")

        self.assertEqual(tracker.shop_count("A01"), 2, "越界的这一次也记进账目")

    def test_the_round_limit_raises_before_the_shop_limit(self):
        tracker = DenyTracker(600)
        cfg = crawler_cfg(deny_shop_limit=3, deny_round_limit=2)

        ready_detail_page(self.page(self.DENY), cfg, deny_tracker=tracker, shop_key="A01")
        with self.assertRaises(RoundDenyExceeded):
            ready_detail_page(self.page(self.DENY), cfg, deny_tracker=tracker, shop_key="A02")
        self.assertEqual(tracker.shop_count("A02"), 1, "另一家店的计数远没到店铺阈值")

    def test_a_slider_page_waits_for_the_human(self):
        with patch.object(guard, "intervention_kind", return_value="滑块"), \
             patch.object(guard, "wait_for_resolution") as wait:
            denied = ready_detail_page(self.page("https://detail.1688.com/offer/11.html"),
                                       self.CFG, emit=MagicMock(), shop_key="A01")

        self.assertFalse(denied, "滑块页不是 deny，等完人照样往下走")
        self.assertEqual(wait.call_args.args[0].url,
                         "https://detail.1688.com/offer/11.html")
        self.assertEqual(wait.call_args.kwargs["verification_type"], "slider")

    def test_an_intervention_timeout_passes_through(self):
        with patch.object(guard, "intervention_kind", return_value="滑块"), \
             patch.object(guard, "wait_for_resolution",
                          side_effect=InterventionTimeout("超时")):
            with self.assertRaises(InterventionTimeout):
                ready_detail_page(self.page("https://detail.1688.com/offer/11.html"),
                                  self.CFG)

    def test_without_a_tracker_a_deny_page_is_still_denied(self):
        """点击路径没配账目时（用例、诊断工具）：deny 照样认得出来，由调用方退避。"""
        self.assertTrue(ready_detail_page(self.page(self.DENY), self.CFG))


class InterventionEvidenceTests(unittest.TestCase):
    """验证判据的输入面只有页面本身：响应流信号已从五个 interface 上撤掉（候选 02）。

    信号原本被当成形参从 `click_listing` 一路转到 `intervention_kind`，而判据读它的那一行
    与早退行是同一条谓词，对任何输入都到不了。撤掉它买到的是行为不变。
    """

    CFG = crawler_cfg()
    # 正文与验证容器都读不到（没有 locator）——只剩地址一条证据
    PAGE = SimpleNamespace(url="https://detail.1688.com/offer/11.html")

    def test_a_normal_address_alone_is_not_an_intervention(self):
        """地址正常、正文与验证容器都读不到的页面：判据给 None。

        这是 ADR-0020 记下的那个「响应里出现 punish 而后台地址正常」的场景，也是 ADR-0029
        记的盲区所在：改前那个形参换不来别的返回值（差分核过），改后也没有第二条路认它。
        所以本条钉的是「删掉等于行为不变」——盲区本身不在这里修。
        """
        self.assertIsNone(guard.intervention_kind(self.PAGE))

    def test_the_judgment_admits_only_the_page(self):
        with self.assertRaises(TypeError):
            guard.intervention_kind(self.PAGE, True)

    def test_the_settling_entry_does_not_take_a_punish_signal(self):
        with self.assertRaises(TypeError):
            ready_detail_page(self.PAGE, self.CFG, punished=True)


class InterventionResolutionTests(unittest.TestCase):
    """「要不要介入」与「解除没有」是同一份证据的两种读法（2026-09-19 审查候选 01）。

    改前 `resolved` 只读地址与可见验证容器，看不见正文。于是「只由正文命中」的那一幕里，
    同一次判定同时说「要人介入」与「已经解决」：详情读取循环因此没有出口（真仓 HEAD 上实跑
    40 秒不返回、约 43 万圈/秒空转），确认窗口当场把一条落在 DOM 上的信号判成误报。
    """

    CFG = crawler_cfg()
    PRODUCT = "https://detail.1688.com/offer/11.html"
    LOGIN_WALL = "请登录后查看商品详情"

    def test_a_page_that_needs_intervention_is_never_reported_as_resolved(self):
        """判据说要介入的时候，不能说同一页已经解除。

        改前红在两条正文命中的形状——而正文正是这两个标记表唯一真正命中过的证据：
        生产存档里命中的 7 个 offer 全是「地址正常、正文是登录墙」。

        这条改完之后不可能再失败，它是给「有人把 `resolved` 又写回成一份独立判据」准备的
        回归护栏；行为证明在下面三条。
        """
        shapes = {
            "地址是登录页": FakePage("https://login.1688.com/"),
            "正文是登录墙": FakePage(self.PRODUCT, body=self.LOGIN_WALL),
            "地址是 punish 页": FakePage("https://x/punish?x5secdata=1"),
            "正文是滑块": FakePage(self.PRODUCT, body="请完成验证后继续访问"),
            "验证容器可见": FakePage(self.PRODUCT, captcha=True),
        }

        for name, page in shapes.items():
            with self.subTest(页面=name):
                self.assertIsNotNone(guard.intervention_kind(page))
                self.assertFalse(guard.resolved(page))

    def test_a_page_with_no_evidence_at_all_is_resolved(self):
        page = FakePage(self.PRODUCT, body="这是一个正常的商品详情页")

        self.assertIsNone(guard.intervention_kind(page))
        self.assertTrue(guard.resolved(page))

    def test_the_judgment_is_a_pure_function_of_the_evidence(self):
        """判定只吃三样证据、不碰页面对象——这是「读一次、判一次」的兑现处。

        判据能脱离页面被直接检验，才说得上「两种读法出自同一份证据」。下面三行里第三行
        还把一条**当前行为**钉住了：淘宝那种「亲，访问被拒绝」拦截页（正文里挂着导航的
        「亲，请登录」与「点我反馈」）今天判成「登录墙」，于是会响铃等人。容器那一支里那句
        「点我反馈」的豁免管不着它 —— 豁免要真管用，得是一条在判定开头就早退的规则，
        而那需要真登录墙样本做对照（见 ADR-0030 挂账）。改这条规则时，这里会红。
        """
        from bestseller_monitor.guard import _PageEvidence, _intervention_of

        product = "https://detail.1688.com/offer/11.html"
        # 那一页的真实正文取自 round_10/693147504013.html 的渲染文本
        block_page = ("淘宝网 - 淘！我喜欢 亲，请登录 免费注册 消息 手机逛淘宝 淘宝网首页 "
                      "我的淘宝 已买到的宝贝 我的足迹 购物车 0 收藏夹 亲，访问被拒绝 "
                      "可能因为：请检查是否使用了代理软件或 VPN 哦~ 了解更多原因 寻找答案 "
                      "点我反馈 阿里巴巴集团|淘宝网|天猫|1688")

        self.assertIsNone(_intervention_of(_PageEvidence(product, "正常的商品详情页", False)))
        self.assertEqual(_intervention_of(_PageEvidence(product, "请登录后查看商品详情", False)),
                         "登录墙")
        self.assertEqual(_intervention_of(_PageEvidence(product, block_page, False)),
                         "登录墙")

    def test_the_drag_captcha_page_copy_is_recognized_as_a_slider(self):
        """round_35 现场的「验证码拦截」页：正文只有拖动式滑块文案，必须判「滑块」。

        2026-09-22 18:27 生产实况：页面顶着正常详情 URL，正文是这两句拖动指引加一条
        「点我反馈」页脚。改前原表无一命中，容器那一支又被「点我反馈」豁免否决，判据给
        None——不等待、不响铃，页面被当成读不出 SKU 的详情页静默记失败。真实正文取自
        round_35/1053682048.html 的渲染文本；两句再各自单独钉一遍——抓取时机不同，页面上
        只渲染出其中一句是常态（存档 53 份里「拖动下方滑块」53/53、「请按住滑块」22/53），
        谁都不是冗余。captcha=True 那一遍还钉住「正文支先于容器支」的次序（ADR-0036）。
        """
        from bestseller_monitor.guard import _PageEvidence, _intervention_of

        product = "https://detail.1688.com/offer/11.html"
        page_body = ("亲，请拖动下方滑块完成验证\n通过验证以确保正常访问\n"
                     "请按住滑块，拖动到最右边\n点我反馈 >\n"
                     "© 1999-2026 Alibaba.com. All rights reserved.")

        for captcha in (False, True):
            with self.subTest(captcha_visible=captcha):
                self.assertEqual(
                    _intervention_of(_PageEvidence(product, page_body, captcha)),
                    "滑块",
                )
        for phrase in ("拖动下方滑块", "请按住滑块"):
            with self.subTest(只渲染出=phrase):
                self.assertEqual(
                    _intervention_of(_PageEvidence(product, phrase, False)),
                    "滑块",
                )

    def test_the_feedback_marker_vetoes_only_a_copy_less_container(self):
        """「点我反馈」豁免的两头（ADR-0036 挂账）：容器可见、正文带反馈记号又无表内文案
        → 仍判无介入；同一容器少掉这条记号就判「滑块」。两头都钉住，免得将来只改一半。
        """
        from bestseller_monitor.guard import _PageEvidence, _intervention_of

        product = "https://detail.1688.com/offer/11.html"

        self.assertIsNone(_intervention_of(_PageEvidence(product, "点我反馈 >", True)))
        self.assertEqual(_intervention_of(_PageEvidence(product, "", True)), "滑块")

    def test_a_body_only_signal_survives_the_confirmation_window(self):
        """落在 DOM 上的信号不该被确认窗口当成「没有落点的瞬时报错」丢掉。

        改前红：窗口第一句 `if resolved(page)` 恒真，于是从不刷新、从不发
        `verification_appear`、从不响铃——登录墙就这么被静默跳过。
        """
        page = FakePage(self.PRODUCT, body=self.LOGIN_WALL)   # 刷新也救不回来
        emit = MagicMock()

        clock = GuardClock(step=1.0)
        with patch.object(guard, "time", clock), patch.object(waiting, "time", clock), \
             patch.object(guard.sound, "play_alarm") as alarm, \
             self.assertRaises(InterventionTimeout):
            guard.wait_for_resolution(page, 1, emit=emit,
                                      verification_type="login", confirm_sec=2.0)

        self.assertEqual(page.reloads, 1, "过了确认窗口要先试一次刷新")
        self.assertEqual(emit.call_args.args[0], "verification_appear")
        self.assertEqual(emit.call_args.kwargs["verification_type"], "login")
        alarm.assert_called()

    def test_a_signal_that_a_reload_clears_is_still_a_false_alarm(self):
        """确认窗口的老职责不变：刷新后恢复的仍旧忽略，不发事件、不响铃。"""
        page = FakePage(self.PRODUCT, body=self.LOGIN_WALL, body_after_reload="")
        emit = MagicMock()

        clock = GuardClock(step=1.0)
        with patch.object(guard, "time", clock), patch.object(waiting, "time", clock), \
             patch.object(guard.sound, "play_alarm") as alarm:
            guard.wait_for_resolution(page, 0, emit=emit, confirm_sec=2.0)

        self.assertEqual(page.reloads, 1)
        emit.assert_not_called()
        alarm.assert_not_called()

    def test_a_signal_that_vanishes_inside_the_confirmation_window_is_ignored(self):
        """窗口里自己解除的信号判误报早退：不刷新、不响铃、不发事件。

        这段早退此前没有用例钉着——变异自证里把窗口改成不探测，全部用例仍绿
        （票 05 的 M2 盲区）。判据打桩为「已解除」，钉的是接线本身：窗口探到真值
        就早退，连刷新都不试；`resolved` 的读数次数兼钉「窗口确实探测过」。
        """
        page = FakePage(self.PRODUCT, body=self.LOGIN_WALL)
        emit = MagicMock()

        with patch.object(guard, "resolved", return_value=True) as resolved, \
             patch.object(guard.sound, "play_alarm") as alarm:
            guard.wait_for_resolution(page, 1, emit=emit, confirm_sec=2.0)

        self.assertGreaterEqual(resolved.call_count, 1, "确认窗口确实探测过")
        self.assertEqual(page.reloads, 0, "窗口内已解除，连刷新都不该试")
        emit.assert_not_called()
        alarm.assert_not_called()

    def test_the_ring_loop_is_asked_to_stop_every_round(self):
        """响铃循环每轮问一次「该不该停」，抛出即原样穿出（A11）。

        这段检查此前没有任何用例钉着。确认窗口取 0（零窗口既不探测也不问取消），数到的
        三次就都是响铃循环自己的检查点：入口放行 → 第一轮探测、响铃、睡下 → 第二轮睡前
        抛出。停在等待中间，而不是等满上限走成 InterventionTimeout。
        """
        page = FakePage(self.PRODUCT, body=self.LOGIN_WALL)   # 刷新也救不回来
        clock = GuardClock(step=1.0)
        checks = []

        def hook():
            checks.append(1)
            if len(checks) > 2:      # 入口与第一轮睡前放行；停在第二轮睡前那次
                raise stop_request.StopRequested("停")

        with patch.object(guard, "time", clock), patch.object(waiting, "time", clock), \
             patch.object(guard.sound, "play_alarm") as alarm:
            stop_request.install(hook)
            self.addCleanup(stop_request.uninstall)
            with self.assertRaises(stop_request.StopRequested):
                guard.wait_for_resolution(page, 1, confirm_sec=0)

        self.assertEqual(len(checks), 3, "入口一次、每轮睡前一次：停在第二轮")
        self.assertEqual(alarm.call_count, 1, "抛出之前已经完整响过一轮")
        self.assertEqual(page.reloads, 1, "确认窗口之后照旧刷新一次才进响铃循环")

    def test_the_ring_loop_reports_the_resolution_before_returning(self):
        """解决路径：停止响铃，先发 `verification_solved` 再返回（A10 的事件面）。

        这一段今天零断言，而本票正好把它挪到了 `until` 之后。铃响过一轮之后「人工解决」：
        页面在下一轮探针变可读，事件带上 resolution_seconds，函数正常返回——不是等满上限
        走成 InterventionTimeout（那条由上面「正文信号活过确认窗口」的用例钉着）。
        """
        page = FakePage(self.PRODUCT, body=self.LOGIN_WALL)   # 刷新也救不回来
        emit = MagicMock()
        clock = GuardClock(step=1.0)

        def solve_after_ringing(count=1):
            """铃响过一轮，人工把页面解决了——下一轮探针就该看到已解除。"""
            page.solve()

        with patch.object(guard, "time", clock), patch.object(waiting, "time", clock), \
             patch.object(guard.sound, "play_alarm", side_effect=solve_after_ringing) as alarm:
            guard.wait_for_resolution(page, 1, emit=emit, verification_type="login",
                                      confirm_sec=0)

        self.assertEqual(alarm.call_count, 1, "响过一轮之后页面才被解决")
        self.assertEqual([call.args[0] for call in emit.call_args_list],
                         ["verification_appear", "verification_solved"],
                         "先报出现、再报解决")
        solved = emit.call_args_list[-1].kwargs
        self.assertEqual(solved["verification_type"], "login")
        self.assertRegex(solved["note"], r"^resolution_seconds=\d+\.\d$")

    def test_the_settling_entry_only_reports_a_page_once_the_signal_is_gone(self):
        """`ready_detail_page` 说「页面可用」之后，判据不能再认得这个页面上的信号。

        这是 `detail_visit` 那个读取循环唯一的推进条件：`_guard()` 返回假值之后它就重置
        可读窗口再 `continue`，下一圈必须走到读页面那一步。改前不成立——正文命中的介入被
        确认窗口判成误报，函数返回「可用」，而判据原地仍为真，循环于是永远转下去。
        """
        page = FakePage(self.PRODUCT, body=self.LOGIN_WALL, body_after_reload="")
        cfg = crawler_cfg(intervention_confirmation_sec=2.0)

        clock = GuardClock(step=1.0)
        with patch.object(guard, "time", clock), patch.object(waiting, "time", clock), \
             patch.object(guard.sound, "play_alarm"):
            denied = ready_detail_page(page, cfg)

        self.assertFalse(denied, "登录墙不是 deny")
        self.assertIsNone(guard.intervention_kind(page),
                          "判据说页面可用了，就不该再认得这个页面")


if __name__ == "__main__":
    unittest.main()
