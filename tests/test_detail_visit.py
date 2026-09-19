import threading
import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

from bestseller_monitor import detail, detail_visit, guard
from helpers import FakePage, GuardClock, crawler_cfg


DETAIL_HTML = ('<script>{"skuInfoMap":{"红色":{"skuId":"red","name":"红色",'
               '"price":10,"canBookCount":3}}}</script>')
PRODUCT_URL = "https://detail.1688.com/offer/11.html"


class DetailVisitTests(unittest.TestCase):
    def setUp(self):
        self.cfg = crawler_cfg()

    def begin(self, page, *, reraise=()):
        return detail_visit.begin_detail_visit(
            lambda: detail_visit.OpenedDetail(page), self.cfg, reraise=reraise,
        )

    def test_verification_html_is_not_observed_before_the_page_becomes_readable(self):
        page = MagicMock()
        page.url = PRODUCT_URL
        page.content.side_effect = ["<html>登录墙</html>", DETAIL_HTML]

        with patch.object(detail_visit, "ready_detail_page", return_value=False), \
             patch.object(detail_visit, "intervention_kind", return_value=None), \
             patch.object(detail_visit.time, "sleep"):
            visit = self.begin(page)
            observation = visit.observe(PRODUCT_URL)

        self.assertIsInstance(visit, detail_visit.ReadyDetailVisit)
        self.assertTrue(observation.ok)
        self.assertEqual(observation.sku_count, 1)
        self.assertEqual(page.content.call_count, 2)

    def test_the_visit_capability_carries_only_the_page(self):
        """`OpenedDetail` 只交页面：判据的证据是页面上的地址、正文与可见验证容器（候选 02）。"""
        with self.assertRaises(TypeError):
            detail_visit.OpenedDetail(MagicMock(), punished=True)

    def test_acquisition_failure_becomes_a_read_failed_visit(self):
        error = PlaywrightError("导航失败")

        result = detail_visit.begin_detail_visit(
            lambda: (_ for _ in ()).throw(error), self.cfg,
        )

        self.assertIsInstance(result, detail_visit.ReadFailedVisit)
        self.assertIs(result.error, error)
        self.assertEqual(result.observation.kind, detail.FailureKind.READ)
        self.assertIn("导航失败", result.observation.failure)

    def test_registered_stop_exception_is_rethrown_as_the_same_object(self):
        class Stop(Exception):
            pass

        error = Stop("暂停")
        with self.assertRaises(Stop) as raised:
            detail_visit.begin_detail_visit(
                lambda: (_ for _ in ()).throw(error), self.cfg,
                reraise=(Stop,),
            )

        self.assertIs(raised.exception, error)

    def test_all_registered_stop_outcomes_keep_identity_across_the_visit_stages(self):
        from bestseller_monitor import pipeline

        errors = [
            pipeline.StopRequested("停"),
            pipeline.RoundDenyExceeded("整轮"),
            pipeline.DayBoundaryReached(),
            pipeline.DetailBudgetExhausted(),
            pipeline.RoundPauseRequired("人工"),
            pipeline.ShopDenyExceeded("店铺"),
        ]
        for error in errors:
            with self.subTest(stage="acquire", error=type(error).__name__):
                with self.assertRaises(type(error)) as raised:
                    detail_visit.begin_detail_visit(
                        lambda error=error: (_ for _ in ()).throw(error),
                        self.cfg, reraise=pipeline.STOP_WITH_OUTCOME,
                    )
                self.assertIs(raised.exception, error)

            page = MagicMock()
            page.url = PRODUCT_URL
            with patch.object(detail_visit, "ready_detail_page",
                              side_effect=error):
                with self.subTest(stage="guard", error=type(error).__name__):
                    with self.assertRaises(type(error)) as raised:
                        detail_visit.begin_detail_visit(
                            lambda page=page: detail_visit.OpenedDetail(page),
                            self.cfg, reraise=pipeline.STOP_WITH_OUTCOME,
                        )
                    self.assertIs(raised.exception, error)

            page = MagicMock()
            page.url = PRODUCT_URL
            page.content.side_effect = error
            with patch.object(detail_visit, "ready_detail_page", return_value=False), \
                 patch.object(detail_visit, "intervention_kind", return_value=None), \
                 patch.object(detail_visit, "is_deny_url", return_value=False):
                visit = detail_visit.begin_detail_visit(
                    lambda page=page: detail_visit.OpenedDetail(page), self.cfg,
                    reraise=pipeline.STOP_WITH_OUTCOME,
                )
                with self.subTest(stage="content", error=type(error).__name__):
                    with self.assertRaises(type(error)) as raised:
                        visit.observe(PRODUCT_URL)
                    self.assertIs(raised.exception, error)

    def test_an_adapter_program_error_is_not_masqueraded_as_read_failure(self):
        with self.assertRaises(TypeError):
            detail_visit.begin_detail_visit(
                lambda: (_ for _ in ()).throw(TypeError("adapter bug")), self.cfg,
            )

    def test_guard_browser_io_becomes_a_read_failed_visit(self):
        error = PlaywrightError("guard 读取失败")
        page = MagicMock()
        page.url = PRODUCT_URL
        with patch.object(detail_visit, "ready_detail_page", side_effect=error):
            result = self.begin(page)

        self.assertIsInstance(result, detail_visit.ReadFailedVisit)
        self.assertIs(result.error, error)
        self.assertEqual(result.observation.kind, detail.FailureKind.READ)

    def test_guard_program_error_is_not_masqueraded_as_read_failure(self):
        page = MagicMock()
        page.url = PRODUCT_URL
        with patch.object(detail_visit, "ready_detail_page",
                          side_effect=TypeError("guard bug")):
            with self.assertRaises(TypeError):
                self.begin(page)

    def test_confirmed_deny_with_a_failed_raw_read_keeps_an_empty_raw_page(self):
        page = MagicMock()
        page.url = "https://s.1688.com/bsop-punish?x=1"
        page.content.side_effect = PlaywrightError("页面已关闭")

        with patch.object(detail_visit, "ready_detail_page", return_value=True):
            result = self.begin(page)

        self.assertIsInstance(result, detail_visit.DeniedVisit)
        self.assertEqual(result.raw_html, "")

    def test_late_deny_is_returned_after_guard_records_it(self):
        page = MagicMock()
        page.url = PRODUCT_URL
        page.content.return_value = "<html>deny</html>"
        guard_calls = []

        def guard(page_, cfg, **kwargs):
            guard_calls.append(kwargs)
            return len(guard_calls) == 2

        with patch.object(detail_visit, "ready_detail_page", side_effect=guard), \
             patch.object(detail_visit, "is_deny_url",
                          side_effect=[False, True]):
            visit = self.begin(page)
            result = visit.observe(PRODUCT_URL)

        self.assertIsInstance(result, detail_visit.DeniedVisit)
        self.assertEqual(result.raw_html, "<html>deny</html>")
        self.assertEqual(len(guard_calls), 2)

    def test_late_intervention_reenters_guard_and_reads_after_resolution(self):
        page = MagicMock()
        page.url = PRODUCT_URL
        page.content.side_effect = ["<html>仍在渲染</html>", DETAIL_HTML]
        guard_calls = []

        with patch.object(detail_visit, "ready_detail_page",
                          side_effect=lambda *args, **kwargs: guard_calls.append(kwargs) or False), \
             patch.object(detail_visit, "intervention_kind",
                          side_effect=[None, "滑块", None]), \
             patch.object(detail_visit, "is_deny_url", return_value=False), \
             patch.object(detail_visit.time, "sleep"):
            visit = self.begin(page)
            observation = visit.observe(PRODUCT_URL)

        self.assertTrue(observation.ok)
        self.assertEqual(len(guard_calls), 2, "初次 guard 后，晚到人工介入要再走一次 guard")
        self.assertEqual(page.content.call_count, 2)

    def test_late_intervention_resets_the_full_readiness_window(self):
        page = MagicMock()
        page.url = PRODUCT_URL
        page.content.side_effect = ["<html>仍在渲染</html>",
                                     "<html>仍在渲染</html>", DETAIL_HTML]
        clock = iter([0.0, 1.0, 5.0, 12.0])

        with patch.object(detail_visit, "ready_detail_page", return_value=False), \
             patch.object(detail_visit, "intervention_kind",
                          side_effect=[None, "滑块", None, None]), \
             patch.object(detail_visit, "is_deny_url", return_value=False), \
             patch.object(detail_visit.time, "time", side_effect=clock), \
             patch.object(detail_visit.time, "sleep"):
            visit = self.begin(page)
            observation = visit.observe(PRODUCT_URL)

        self.assertTrue(observation.ok, "重置后的窗口内应继续等到商品页可读")
        self.assertEqual(page.content.call_count, 3)

    def test_observe_is_a_single_use_capability(self):
        page = MagicMock()
        page.url = PRODUCT_URL
        page.content.return_value = DETAIL_HTML

        with patch.object(detail_visit, "ready_detail_page", return_value=False), \
             patch.object(detail_visit, "intervention_kind", return_value=None), \
             patch.object(detail_visit, "is_deny_url", return_value=False):
            visit = self.begin(page)
            self.assertTrue(visit.observe(PRODUCT_URL).ok)
            with self.assertRaises(RuntimeError):
                visit.observe(PRODUCT_URL)

    def test_a_late_body_only_intervention_cannot_spin_the_readiness_loop(self):
        """晚到的正文级介入也要有界：等到时限就暂停，而不是无限空转（2026-09-19 候选 01）。

        改前 `resolved` 看不见正文：确认窗口当场返回 → `_guard()` 说页面可用 → 重置可读窗口
        → `continue` → 判据仍然为真 → **不返回**（真仓 HEAD 上实跑 40 秒，约 43 万圈/秒）。
        """
        page = FakePage(PRODUCT_URL, body="正常的商品详情内容", html=DETAIL_HTML)
        visit = self.begin(page)
        self.assertIsInstance(visit, detail_visit.ReadyDetailVisit)

        page.become_wall("请登录后查看商品详情")   # 弹窗打开之后才出现的登录墙
        with patch.object(guard, "time", GuardClock(step=10.0)), \
             patch.object(guard.sound, "play_alarm"), \
             patch.object(detail_visit.time, "sleep"):
            with self.assertRaises(guard.InterventionTimeout):
                self.assert_returns_within(lambda: visit.observe(PRODUCT_URL), 3.0,
                                           release=page.solve)

    def assert_returns_within(self, call, seconds: float, *, release):
        """跑 `call`；`seconds` 内没返回就判定「这条分支没有出口」，并把那条线程放出来。

        这条分支的空转不带 `sleep`（每圈都去问一次页面），所以超时之后要靠 `release` 让页面
        进入已解决状态，那条线程下一圈才走得出去，不会在后面的用例里继续烧 CPU。
        """
        box: dict = {}

        def target():
            try:
                box["value"] = call()
            except BaseException as exc:   # noqa: BLE001 - 原样带回主线程
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(seconds)
        if thread.is_alive():
            release()
            thread.join(5.0)
            self.fail(f"{seconds} 秒内没有返回：这条分支没有出口")
        if "error" in box:
            raise box["error"]
        return box.get("value")


if __name__ == "__main__":
    unittest.main()
