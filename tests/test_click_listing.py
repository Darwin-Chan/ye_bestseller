"""点击式列表的遍历规则：脚本化 adapter，不碰页面对象。

遍历只看页面 adapter 的四件事（准备 / 滚动取卡片 / 拿第 i 张卡 / 推进），
所以这里用 helpers 里的 ScriptedListing 与 FakeCard 就能把「这家店有哪几页、
每页有哪些卡、每张卡读到什么」摆出来，断言 offers、页数、事件与库里的行。
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw, click_listing, detail, listing, rounds, stop_request
from bestseller_monitor.config import Shop
from bestseller_monitor.db import (Database, DayBoundaryReached, DetailBudgetExhausted,
                                   connect, utcnow)
from bestseller_monitor import guard
from bestseller_monitor.guard import InterventionTimeout
from bestseller_monitor.listing import ListingLoadFailed
from helpers import crawler_cfg, FakeCard, ScriptedListing, new_round


class ClickListingTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.conn = connect(Path(tmp.name) / "click.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        # 「今天已经采过」落在更早那一轮（库存行仍是今天的），本轮是今天这一轮。
        self.past_round = new_round(self.db, "A01", run_date="2026-01-01")
        self.round_id = new_round(self.db, "A01")
        self.shop = Shop("A01", "店铺A", "https://A01.example/")
        self.human = MagicMock()
        self.events: list[tuple[str, dict]] = []

    # ---------- 夹具 ----------
    def walk(self, listing_page, cfg=None, **kwargs):
        return click_listing.ShopWalk(
            listing_page, self.shop, cfg or crawler_cfg(), self.human,
            db=self.db, round_id=self.round_id, emit=self.emit, **kwargs)

    def emit(self, event: str, **kw: object) -> None:
        self.events.append((event, kw))

    def event_names(self) -> list[str]:
        return [event for event, _ in self.events]

    def crawl(self, pages, cfg=None, deny_tracker=None):
        page = ScriptedListing(pages)
        offers, pages_read = self.walk(page, cfg, deny_tracker=deny_tracker).run()
        return page, offers, pages_read

    def offer_rows(self):
        return self.conn.execute(
            "SELECT offer_id, rank, list_title FROM shop_offers WHERE round_id=? "
            "ORDER BY rank", (self.round_id,)).fetchall()

    def snapshots(self, page_status):
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE round_id=? AND page_status=?",
            (self.round_id, page_status)).fetchall()

    def collected_today(self, offer_id: str, title: str):
        """把某商品记成「今天已采过」：落在更早那一轮，库存行是今天的。"""
        self.db.submit_inventory_snapshot(
            round_id=self.past_round, shop_key="A01", shop_url="https://A01.example/",
            shop_name="店铺A",
            offer_id=offer_id,
            product_url=f"https://detail.1688.com/offer/{offer_id}.html",
            list_title=title, detail_title=title, main_image_url="",
            sku_rows=[{"sku_name": "默认(单规格)", "sku_stock": 1}],
            collected_at=utcnow(), attempt=1)


class WalkTests(ClickListingTestCase):
    def test_a_shop_with_no_cards_is_a_listing_failure(self):
        with self.assertRaises(ListingLoadFailed):
            self.crawl([[]])

    def test_each_page_is_read_once_and_the_walk_stops_when_there_is_no_next_batch(self):
        page, offers, pages_read = self.crawl([[FakeCard("商品1", offer_id="11")]])

        self.assertEqual((len(offers), pages_read), (1, 1))
        self.assertEqual(page.advanced, [], "没有下一页就不再推进")
        self.assertEqual([row["offer_id"] for row in self.offer_rows()], ["11"])

    def test_the_page_limit_from_the_config_stops_the_walk(self):
        page, offers, pages_read = self.crawl(
            [[FakeCard("商品1", offer_id="11")], [FakeCard("商品2", offer_id="22")]],
            cfg=crawler_cfg(max_pages_per_shop=1))

        self.assertEqual((len(offers), pages_read), (1, 1))
        self.assertEqual(page.scrolled, ["滚动加载新卡片"])

    def test_a_discovered_offer_is_written_to_the_listing_immediately(self):
        self.crawl([[FakeCard("商品1", offer_id="11")]])

        rows = self.offer_rows()
        self.assertEqual([(row["offer_id"], row["list_title"]) for row in rows],
                         [("11", "商品1")])

    def test_the_event_sequence_of_a_successful_card_is_unchanged(self):
        self.crawl([[FakeCard("商品1", offer_id="11")]])

        self.assertEqual(self.event_names(), [
            "list_page", "product_open", "popup_open", "detail_parse", "click_ok",
            "popup_close"])

    def test_the_event_sequence_of_a_parse_failure_is_unchanged(self):
        card = FakeCard("商品1", offer_id="11", observation=detail.Observation.parse_failed(
            RuntimeError("页面结构变了"), "<html></html>"))

        self.crawl([[card]])

        self.assertEqual(self.event_names(), [
            "list_page", "product_open", "popup_open", "click_parse_error", "detail_parse",
            "popup_close"])
        self.assertEqual(len(self.snapshots("失败")), 1)

    def test_the_event_sequence_of_a_read_failure_is_unchanged(self):
        """读不到页面只发 click_parse_error（没有 sku_count=0 那条）。"""
        card = FakeCard("商品1", offer_id="11",
                        observation=detail.Observation.read_failed(RuntimeError("页面没了")))

        self.crawl([[card]])

        self.assertEqual(self.event_names(), [
            "list_page", "product_open", "popup_open", "click_parse_error", "popup_close"])

    def test_a_card_without_an_offer_id_is_recorded_as_no_popup(self):
        card = FakeCard("商品1", offer_id=None, opened=False)

        result = self.walk(ScriptedListing([[card]])).capture(card, "商品1")

        self.assertIsNone(result)
        self.assertIn("click_no_popup", self.event_names())
        self.assertEqual(self.snapshots("失败"), [])


class SameNameTests(ClickListingTestCase):
    def test_a_same_named_product_is_deferred_without_opening_the_detail(self):
        self.collected_today("11", "商品1")
        card = FakeCard("商品1", offer_id="11")

        self.crawl([[card]])

        self.assertEqual(card.opens, 0, "同名暂缓不该打开详情")
        self.assertIn("defer_samename", self.event_names())
        self.assertEqual([row["offer_id"] for row in self.snapshots("跳过")], ["11"])

    def test_a_repeated_name_triggers_a_second_pass_that_reads_only_that_name(self):
        first, second = FakeCard("商品1", offer_id="11"), FakeCard("商品1", offer_id="12")
        other = FakeCard("别的商品", offer_id="99")

        page, offers, pages_read = self.crawl([[first, second, other]])

        self.assertEqual(page.prepared, ["店铺 A01 首屏", "店铺 A01 补抓首屏"],
                         "同名商品出现后回头补抓一遍")
        self.assertEqual(other.opens, 1, "补抓只读同名的那几张卡")
        self.assertEqual((first.opens, second.opens), (2, 2), "同名的两张都被补抓读过")
        self.assertEqual(len(offers), 3, "补抓不重复计入已发现的商品")

    def test_the_same_offer_seen_twice_keeps_one_listing_row(self):
        first, second = FakeCard("商品1", offer_id="11"), FakeCard("商品1", offer_id="11")

        self.crawl([[first, second]])

        self.assertEqual(len(self.offer_rows()), 1, "同一个商品只有一条榜单行")
        self.assertEqual(len(self.snapshots("成功")), 1, "重复看到不再提交")

    def test_a_deferred_name_still_gets_its_listing_row(self):
        """同名暂缓的商品也要落榜单行，否则它的跳过快照会成为孤儿。"""
        self.collected_today("11", "商品1")

        self.crawl([[FakeCard("商品1", offer_id="11")]])

        self.assertEqual([row["offer_id"] for row in self.offer_rows()], ["11"])
        self.human.before_detail.assert_not_called()

    def test_the_rescue_pass_claims_before_opening_the_card(self):
        """补抓那遍同样先申请机会：预算用尽时那张卡根本不该被点开。

        第一张同名卡在主遍历里被按名暂缓（没占机会），第二张进了详情（占掉唯一的机会）；
        补抓那遍回头读第一张时必须先申请、拿不到就报预算耗尽。
        """
        self.collected_today("11", "商品1")
        deferred = FakeCard("商品1", offer_id="11")
        second = FakeCard("商品1", offer_id="12")

        with self.assertRaises(DetailBudgetExhausted):
            self.crawl([[deferred, second]], cfg=crawler_cfg(max_detail_opportunities_per_round=1))

        self.assertEqual(deferred.opens, 0, "预算用尽时补抓也不该点开卡片")
        self.assertEqual(second.opens, 1)

    def test_the_rescue_pass_stops_when_the_next_page_never_loads(self):
        """补抓翻页也走同一道推进：没有下一批就收工，不再读第二页。"""
        first, second = FakeCard("商品1", offer_id="11"), FakeCard("商品1", offer_id="12")
        page, offers, pages_read = self.crawl([[first, second]])

        self.assertEqual(page.prepared, ["店铺 A01 首屏", "店铺 A01 补抓首屏"])
        self.assertEqual(page.advanced, [], "单页的店没有可推进的下一批")

    def test_the_rescue_pass_advances_with_its_own_describe(self):
        """同名商品分布在两页时，补抓那遍自己翻页（推进的文案与主页那遍分开）。"""
        first, second = FakeCard("商品1", offer_id="11"), FakeCard("商品1", offer_id="12")
        page, offers, pages_read = self.crawl([[first], [second]])

        self.assertEqual(pages_read, 2)
        self.assertEqual(page.advanced,
                         ["店铺 A01 第 1 页",              # 主遍历：第 1 页之后
                          "店铺 A01 补抓第 1 页"],         # 补抓：第 1 页之后
                         "补抓翻页走同一道推进，但文案是自己的")


class DenyTests(ClickListingTestCase):
    def test_the_third_deny_closes_the_card_and_skips_the_product(self):
        card = FakeCard("商品1", offer_id="11", denied=True)
        tracker = guard.DenyTracker(600)

        result = self.walk(ScriptedListing([[card]]),
                           deny_tracker=tracker).capture(card, "商品1")

        self.assertIsNone(result, "第 3 次 deny 跳过当前商品")
        self.assertEqual(card.opens, 3, "第 1/2 次退避之后还要再试一次")
        self.assertEqual((card.closes, self.human.sleep.call_count), (3, 2),
                         "每次 deny 关掉详情，前两次之间退避")
        self.assertTrue(any("&n=3&skip" in kw.get("note", "")
                            for event, kw in self.events if event == "click_deny"))
        self.assertIn("click_deny", self.event_names())
        self.assertEqual(self.snapshots("失败"), [], "deny 跳过不算失败")

    def test_the_shop_is_skipped_when_deny_hits_the_limit(self):
        card = FakeCard("商品1", offer_id="11", denied=True)
        walk = self.walk(ScriptedListing([[card]]), cfg=crawler_cfg(deny_shop_limit=1),
                         deny_tracker=guard.DenyTracker(600))

        with self.assertRaises(guard.ShopDenyExceeded):
            walk.capture(card, "商品1")


class StopAndBudgetTests(ClickListingTestCase):
    def test_a_same_day_skip_releases_the_slot_the_walk_reserved(self):
        """点击路径在打开卡片前就占了机会；发现今天采过时要把它退回去。"""
        self.collected_today("11", "旧标题")
        card = FakeCard("商品1", offer_id="11")

        self.crawl([[card]])

        self.assertEqual(self.db.detail_opportunity_total(self.round_id), 0,
                         "同日跳过不消耗详情预算")

    def test_the_card_slot_is_bound_to_the_offer_it_opened(self):
        card = FakeCard("商品1", offer_id="11")

        self.crawl([[card]])

        ledger = self.db.detail_opportunities(self.round_id, "A01")
        self.assertEqual([(row["identity"], row["offer_id"]) for row in ledger],
                         [(card.ref, "11")], "机会记在卡片位置上，再绑到它打开的商品")

    def test_a_stop_from_the_reading_path_closes_the_card_and_propagates(self):
        card = FakeCard("商品1", offer_id="11", read_error=DayBoundaryReached())

        with self.assertRaises(DayBoundaryReached):
            self.walk(ScriptedListing([[card]])).capture(card, "商品1")

        self.assertEqual(card.closes, 1, "上抛之前要把它开的详情关掉")
        self.assertIn("popup_close", self.event_names())

    def test_a_stale_round_stops_before_opening_a_card(self):
        stale = new_round(self.db, "A01", run_date="2026-01-01")
        card = FakeCard("商品1", offer_id="11")
        page = ScriptedListing([[card]])
        walk = click_listing.ShopWalk(page, self.shop, crawler_cfg(), self.human,
                                      db=self.db, round_id=stale, emit=self.emit)

        with self.assertRaises(DayBoundaryReached):
            walk.run()

        self.assertEqual(card.opens, 0, "该停就不该点开卡片")

    def test_a_pause_request_stops_at_the_next_card(self):
        started_at = self.db.record_crawler_process(pid=os.getpid(), round_id=self.round_id,
                                                    note="test")
        self.db.request_stop(round_id=self.round_id, kind=stop_request.PAUSE,
                             target_pid=os.getpid(), target_started_at=started_at)
        card = FakeCard("商品1", offer_id="11")
        page = ScriptedListing([[card]])

        with self.assertRaises(stop_request.StopRequested):
            self.walk(page).run()

        self.assertEqual(card.opens, 0)

    def test_the_detail_budget_stops_the_walk_before_opening_the_card(self):
        first, second = FakeCard("商品1", offer_id="11"), FakeCard("商品2", offer_id="22")

        with self.assertRaises(DetailBudgetExhausted):
            self.crawl([[first, second]],
                      cfg=crawler_cfg(max_detail_opportunities_per_round=1))

        self.assertEqual((first.opens, second.opens), (1, 0),
                         "预算用尽时第二张卡都不该点开")


class PlaywrightAdapterTests(ClickListingTestCase):
    """adapter 自己的翻译活儿：点卡片、读页面、滚动。

    「打开之后安顿页面」（滑块等人 / deny 记账）不在这儿了——它归 `guard.ready_detail_page()`，
    由遍历在点开卡片之后叫（候选 04），所以那条等待的用例移到 `test_guard`。
    """

    def test_opening_a_card_only_opens_the_page(self):
        """点卡片只负责把页面打开：诊断工具因此拿到的是原始状态，不被隐式等待挡住。"""
        page, popup = MagicMock(), MagicMock()
        page.expect_popup.return_value.__enter__.return_value = SimpleNamespace(value=popup)

        with patch.object(guard, "intervention_kind", return_value="滑块") as kind, \
             patch.object(guard, "wait_for_resolution",
                          side_effect=InterventionTimeout("超时")) as wait:
            detail_page, again = click_listing.click_card(
                page, MagicMock(), crawler_cfg(), False, MagicMock())

        self.assertIs(detail_page, popup)
        self.assertIs(again, popup)
        kind.assert_not_called()
        wait.assert_not_called()
        self.assertEqual(page.expect_popup.call_args.kwargs["timeout"],
                         browser_pw._WAIT_POPUP_MS)

    def test_scrolling_stops_after_the_first_no_change_window(self):
        page = MagicMock()
        page.locator.return_value.count.return_value = 31

        with patch.object(listing, "wait_until", return_value=False) as wait:
            self.assertEqual(click_listing.scroll_cards_until_stable(page), 31)

        wait.assert_called_once()
        page.mouse.wheel.assert_called_once_with(0, 6000)

    def test_a_page_is_read_into_an_observation_with_its_main_image(self):
        owner = click_listing.PlaywrightListing(MagicMock(), self.shop, crawler_cfg(), self.human)
        card = owner.card(0)
        detail_page = MagicMock()
        detail_page.url = "https://detail.1688.com/offer/11.html"
        detail_page.content.return_value = (
            '<script>{"skuInfoMap":{"A":{"skuId":1,"canBookCount":1}}}</script>')

        with patch.object(click_listing, "click_card", return_value=(detail_page, MagicMock())):
            card.open()
        with patch.object(detail, "extract_main_image", return_value="https://img/1.png"):
            observation = card.read()

        self.assertEqual(card.offer_id, "11")
        self.assertIn("main_image_url", observation.payload)

    def test_a_main_image_failure_is_recorded_as_a_parse_crash(self):
        """主图字段异常在改前也算解析异常：记失败、留原始页。"""
        owner = click_listing.PlaywrightListing(MagicMock(), self.shop, crawler_cfg(), self.human)
        card = owner.card(0)
        detail_page = MagicMock()
        detail_page.url = "https://detail.1688.com/offer/11.html"
        detail_page.content.return_value = (
            '<script>{"skuInfoMap":{"A":{"skuId":1,"canBookCount":1}}}</script>')

        with patch.object(click_listing, "click_card", return_value=(detail_page, MagicMock())):
            card.open()
        with patch.object(detail, "extract_main_image",
                          side_effect=ValueError("图片字段异常")):
            observation = card.read()

        self.assertEqual(observation.kind, detail.FailureKind.PARSE)
        self.assertIn("图片字段异常", observation.failure)
        self.assertTrue(observation.raw_html, "原始页要留着供校准")


if __name__ == "__main__":
    unittest.main()
