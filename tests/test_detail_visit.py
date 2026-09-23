import dataclasses
import inspect
import threading
import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

from bestseller_monitor import detail, detail_visit, guard, stop_request, waiting
from helpers import FakePage, GuardClock, crawler_cfg


DETAIL_HTML = ('<script>{"skuInfoMap":{"红色":{"skuId":"red","name":"红色",'
               '"price":10,"canBookCount":3}}}</script>')
PRODUCT_URL = "https://detail.1688.com/offer/11.html"


class ScriptedClock:
    """`waiting.time` 的替身：单调钟按剧本取值，`sleep` 不真等。

    同 seam 上另有两份替身：test_waiting 的 `FakeClock`（虚拟钟只在睡眠时前进）与
    helpers 的 `GuardClock`（读时即前进、还没有 `monotonic`，票 05 补）。窗口重置要看的
    是「每个时刻钟走到哪」，前两份都表达不了，因此这里用剧本——每次读的值由用例写死，
    读超了剧本就报错（轮询的读表次数变了，用例该跟着改）。
    """

    def __init__(self, moments):
        self.moments = list(moments)
        self.reads: list[float] = []

    def monotonic(self) -> float:
        if not self.moments:
            raise AssertionError("钟的剧本走完了：轮询读表次数超出用例预期")
        self.reads.append(self.moments.pop(0))
        return self.reads[-1]

    def sleep(self, seconds: float) -> None:
        pass


class DetailVisitTests(unittest.TestCase):
    def setUp(self):
        self.cfg = crawler_cfg()

    def begin(self, page):
        return detail_visit.begin_detail_visit(
            lambda: detail_visit.OpenedDetail(page), self.cfg,
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
                        self.cfg,
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
                            self.cfg,
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
                )
                with self.subTest(stage="content", error=type(error).__name__):
                    with self.assertRaises(type(error)) as raised:
                        visit.observe(PRODUCT_URL)
                    self.assertIs(raised.exception, error)

    def test_the_visit_entry_admits_no_stop_exception_tuple(self):
        """`reraise` 那条线是惰性的，已整个撤掉（2026-09-19 审查候选 03）。

        它穿过的 7 句 `except <元组>: raise` 对任何输入都不改变结果：下面那句只捕
        `BROWSER_IO_ERRORS` 的四个类，而登记在 `_STOP_OUTCOMES` 里的六个停止异常一个都不是
        它的子类，本来就会原样上抛。删掉它顺带把 `click_listing` 为拿一个常量而写的延迟
        import（与它引出的那条 import 环）一并去掉。

        **这条非用签名不可**：多传一个位置实参会被静默绑到下一个参数上。加参数也要在这里
        露一面——`pacing` 就是 2026-09-22 按 ADR-0038 加进来的那一个（主动停顿的两步都收在
        这条 seam 上）。
        """
        self.assertEqual(list(inspect.signature(detail_visit.begin_detail_visit).parameters),
                         ["acquire", "cfg", "emit", "deny_tracker", "shop_key", "pacing"])
        self.assertNotIn("_reraise",
                         {field.name for field in dataclasses.fields(detail_visit.ReadyDetailVisit)})
        with self.assertRaises(TypeError):
            detail_visit.begin_detail_visit(lambda: None, self.cfg, reraise=())

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
        """介入解决后重开的是**完整**新窗：原窗烧掉一半照旧，完整的那个窗才等得到（A08）。

        钟按剧本走（每次读单调钟 = 一个时刻，`sleep` 不真等）：窗口一 t=0 开（到 10
        为止）、t=1.5 睡过一片，第二轮探针撞上「滑块」；介入在 t=5 被 guard 解决，
        原窗只剩一半——重开的新窗必须是**完整**的（到 15），t≥12.5 的那两轮才轮得到，
        第四次读取（t=12.9 之后）才拿到可读的正文。重置若只续剩余额度（新窗到 10），
        那次读取会走超时出口、拿到不可读的正文，本用例判红。
        """
        clock = ScriptedClock([0.0, 1.0, 1.5, 5.0, 12.0, 12.5, 12.8, 12.9])
        page = MagicMock()
        page.url = PRODUCT_URL
        read_moments = []

        def content():
            read_moments.append(clock.reads[-1])
            return DETAIL_HTML if len(read_moments) >= 4 else "<html>仍在渲染</html>"

        page.content.side_effect = content

        with patch.object(detail_visit, "ready_detail_page", return_value=False), \
             patch.object(detail_visit, "intervention_kind",
                          side_effect=[None, "滑块", None, None, None]), \
             patch.object(detail_visit, "is_deny_url", return_value=False), \
             patch.object(waiting, "time", clock):
            visit = self.begin(page)
            observation = visit.observe(PRODUCT_URL)

        self.assertTrue(observation.ok, "重置后的窗口内应继续等到商品页可读")
        self.assertEqual(page.content.call_count, 4)
        self.assertGreater(read_moments[-1], detail_visit.DETAIL_READY_TIMEOUT_SEC,
                           "成功那次读取在原窗口(10s)之外：只有完整新窗等得到它")

    def test_the_timeout_exit_observes_the_current_html(self):
        """窗口耗尽的出口照旧：读一次当前 html 交 `observe_html`（A08 后半）。

        页面始终不可读 → `until` 超时返回 `None` → 出口再读一次当前页面、按现状翻译。
        窗口打小到 0.05 秒（test_listing 的先例），配合 `sleep` 打桩，用例不真等。
        """
        page = MagicMock()
        page.url = PRODUCT_URL
        page.content.return_value = "<html>仍在渲染</html>"

        with patch.object(detail_visit, "DETAIL_READY_TIMEOUT_SEC", 0.05), \
             patch.object(detail_visit, "ready_detail_page", return_value=False), \
             patch.object(detail_visit, "intervention_kind", return_value=None), \
             patch.object(detail_visit, "is_deny_url", return_value=False), \
             patch.object(detail_visit.time, "sleep"):
            visit = self.begin(page)
            observation = visit.observe(PRODUCT_URL)

        self.assertEqual(observation.kind, detail.FailureKind.PARSE,
                         "出口把当前 html 交给 observe_html 翻译")
        self.assertEqual(observation.raw_html, "<html>仍在渲染</html>")
        self.assertGreaterEqual(page.content.call_count, 2, "探测过一轮之后，出口又读了一次")

    def test_a_stop_request_during_the_readiness_wait_passes_through(self):
        """等待期间收到停止请求：异常原样上抛，不落 read_failed（A09；先例 test_round_stop）。

        停止落在「已经探测过一轮、还在等」的时刻：入口那个检查点放行，睡前那次把
        StopRequested 抛出来。异常穿过 observe，而不是被翻译成一次读取失败——失败率
        不为「没真的读完的那次」记账（ADR-0009；spec §5 的那处行为变化）。
        """
        page = MagicMock()
        page.url = PRODUCT_URL
        page.content.return_value = "<html>仍在渲染</html>"     # 一直不可读 → 一直在等
        checks = []

        def hook():
            checks.append(1)
            if len(checks) > 1:      # 入口放行；停在等待中间的那次检查点
                raise stop_request.StopRequested("停")

        with patch.object(detail_visit, "ready_detail_page", return_value=False), \
             patch.object(detail_visit, "intervention_kind", return_value=None), \
             patch.object(detail_visit, "is_deny_url", return_value=False), \
             patch.object(detail_visit.time, "sleep"):
            visit = self.begin(page)
            stop_request.install(hook)
            self.addCleanup(stop_request.uninstall)
            with self.assertRaises(stop_request.StopRequested):
                visit.observe(PRODUCT_URL)

        self.assertEqual(len(checks), 2, "入口一次、睡前一次：停在等待中间")
        self.assertEqual(page.content.call_count, 1, "被认领之前已经探测过一轮")

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
             patch.object(guard.sound, "play_alarm"):
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
