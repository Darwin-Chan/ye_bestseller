import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import guard
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
from helpers import crawler_cfg


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


if __name__ == "__main__":
    unittest.main()
