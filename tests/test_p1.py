import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw, click_listing, detail, guard, pipeline, rounds
from bestseller_monitor.config import Shop
from bestseller_monitor.db import (
    CST,
    Database,
    connect,
    utcnow,
    DayBoundaryReached,
    DAY_BOUNDARY_NOTE,
)
from bestseller_monitor.delay import Humanizer
from bestseller_monitor.detail import DetailParseFailed, parse_detail_html
from bestseller_monitor.guard import InterventionTimeout, RoundPauseRequired
from bestseller_monitor.listing import ListingLoadFailed
from bestseller_monitor.rounds import RoundRequest, ShopScope
from helpers import crawler_cfg, isolated_locks, new_round
from tools import check_orphans


# 一次「读到了页面」的补采：真 html，交给 detail.observe_page 翻译（候选 02 之后
# 补采 adapter 只交 html，不自己解析）。
DETAIL_HTML = ('<script>{"skuInfoMap":{"红色":{"skuId":"red","name":"红色",'
               '"price":10,"canBookCount":3}}}</script>')


def _yesterday() -> str:
    return (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")


class P1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.db = Database(self.conn)

    def test_open_detail_reads_a_real_page_and_announces_the_offer(self):
        """补采 adapter 的真实路径：导航、认商品编号、把页面读成 html。

        这条守的是「`open_detail` 本身还能跑」——候选 03 收口时它（当时叫 `capture_detail`）
        一度因为少了一个 import 每次都抛 NameError，而所有用例都把函数打了桩，谁也没发现。
        """
        page = MagicMock()
        page.content.return_value = (
            '<script>{"skuInfoMap":{"A":{"skuId":1,"canBookCount":2}}}</script>')
        events: list[tuple[str, dict]] = []

        # 页面内容一次就解析得出来，等可读那一步不会真的 sleep；打桩只为万一是等待。
        with patch.object(browser_pw.time, "sleep"):
            html = browser_pw.open_detail(
                page, "https://detail.1688.com/offer/111.html", crawler_cfg(),
                emit=lambda event, **kw: events.append((event, kw)))

        self.assertEqual([event for event, _ in events], ["detail_nav"],
                         "读成 html 就交回去，解析与事件交给 observe_page 那一侧")
        self.assertEqual(events[0][1]["offer_id"], "111")
        self.assertEqual(
            [row["sku_stock"] for row in parse_detail_html(html, "u")["rows"]], [2])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


    def test_detail_parser_rejects_missing_stock_and_accepts_zero(self):
        with self.assertRaisesRegex(DetailParseFailed, "未解析到 SKU"):
            parse_detail_html('<script>{"price":"1.25"}</script>', "https://detail.1688.com/offer/1.html")
        partial = (
            '<script>{"skuInfoMap":{"A":{"skuId":1,"canBookCount":1},'
            '"B":{"skuId":2,"price":"1.25"}}}</script>'
        )
        with self.assertRaisesRegex(DetailParseFailed, "缺失库存"):
            parse_detail_html(partial, "https://detail.1688.com/offer/1.html")
        zero = '<script>{"skuInfoMap":{"A":{"skuId":1,"canBookCount":0}}}</script>'
        payload = parse_detail_html(zero, "https://detail.1688.com/offer/1.html")
        self.assertEqual(payload["rows"][0]["sku_stock"], 0)

    def test_single_spec_page_without_bookable_amount_reports_incomplete_stock(self):
        # 单规格页缺商品级可售量：属于不完整库存观测，备注要与页面结构变化区分开。
        html = (
            '<script>var x={"offerSign":{"isSkuOffer":false},'
            '"skuModel":{"skuInfoMap":[]},'
            '"tradeModel":{"priceDisplay":"0.02"}};</script>'
        )
        with self.assertRaisesRegex(DetailParseFailed, "单规格商品缺少商品级可售量"):
            parse_detail_html(html, "https://detail.1688.com/offer/1.html")

    def test_capture_one_success_uses_submitted_payload_rows_for_logging(self):
        round_id = new_round(self.db)
        offer = {
            "shop_key": "A01",
            "shop_url": "https://shop.example/",
            "shop_name": "店铺A",
            "offer_id": "111",
            "product_url": "https://detail.1688.com/offer/111.html",
            "list_title": "榜单标题",
        }
        with patch.object(browser_pw, "open_detail", return_value=DETAIL_HTML), \
             patch.object(detail, "extract_main_image", return_value=None):
            pipeline._capture_one_pw(
                self.db, crawler_cfg(), MagicMock(), round_id, offer, MagicMock(),
            )

        row = self.conn.execute(
            "SELECT sku_id, sku_stock FROM snapshots WHERE round_id=? AND page_status='成功'",
            (round_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("red", 3))

    def test_a_main_image_crash_in_the_backfill_keeps_the_raw_page(self):
        """补采路径与点击路径共用同一份翻译：主图字段异常记解析崩溃、留原始页。

        改前它落成「访问异常」且不留原始页——同一个失败，两条路两种待遇（候选 02）。
        """
        round_id = new_round(self.db)
        offer = {
            "shop_key": "A01",
            "shop_url": "https://shop.example/",
            "shop_name": "店铺A",
            "offer_id": "111",
            "product_url": "https://detail.1688.com/offer/111.html",
            "list_title": "榜单标题",
        }
        with patch.object(browser_pw, "open_detail", return_value=DETAIL_HTML), \
             patch.object(detail, "extract_main_image",
                          side_effect=ValueError("图片字段异常")):
            pipeline._capture_one_pw(
                self.db, crawler_cfg(raw_page_dir=Path(self.tmp.name) / "raw"),
                MagicMock(), round_id, offer, MagicMock(),
            )

        note = self.conn.execute(
            "SELECT detail_note FROM snapshots WHERE round_id=? AND page_status='失败'",
            (round_id,),
        ).fetchone()["detail_note"]
        self.assertIn("详情页解析异常：图片字段异常", note)
        self.assertIn("原始页面：", note)
        self.assertTrue(Path(note.split("原始页面：")[1]).exists(), "原始页要存下来供校准")

    def test_the_backfill_event_sequence_is_unchanged(self):
        """补采一次成功的事件与改前一致：`detail_nav` → `detail_parse`（名字、顺序、归属）。"""
        round_id = new_round(self.db)
        offer = {
            "shop_key": "A01",
            "shop_url": "https://shop.example/",
            "shop_name": "店铺A",
            "offer_id": "111",
            "product_url": "https://detail.1688.com/offer/111.html",
            "list_title": "榜单标题",
        }
        events: list[str] = []
        page = MagicMock()
        page.content.return_value = DETAIL_HTML

        # 走真的 open_detail（只有页面动作与等待被打桩），事件才真是生产路径发出来的。
        with patch.object(browser_pw.time, "sleep"), \
             patch.object(browser_pw, "intervention_kind", return_value=None):
            pipeline._capture_one_pw(
                self.db, crawler_cfg(), MagicMock(), round_id, offer, page,
                emit=lambda event, **kw: events.append(event),
            )

        self.assertEqual(events, ["detail_nav", "detail_parse"])

    def test_a_deny_page_during_backfill_is_counted_and_named(self):
        """补采命中 deny：计入整轮账目（候选 04 的 Q1 决定），失败记录里写清是 deny。"""
        round_id = new_round(self.db)
        self.db.add_shop(round_id, "A01", "https://shop.example/", "店铺A")
        url = "https://detail.1688.com/offer/11.html"
        self.db.save_shop_offers(round_id, "A01", "https://shop.example/", "店铺A",
                                 [(1, "11", url, "商品11", "")], 1)
        self.db.mark_failure(round_id, "A01", "11", 1, "解析失败")
        offer = next(row for row in self.db.pending_offers(round_id, max_attempts=2)
                     if row["offer_id"] == "11")
        tracker = guard.DenyTracker(600)
        page = SimpleNamespace(url="https://s.1688.com/bsop-punish?x=1")

        with patch.object(browser_pw, "open_detail", return_value="<html>deny</html>"):
            pipeline._capture_one_pw(
                self.db, crawler_cfg(raw_page_dir=Path(self.tmp.name) / "raw"),
                MagicMock(), round_id, offer, page, deny_tracker=tracker)

        self.assertEqual((tracker.shop_count("A01"), tracker.round_count()), (1, 1),
                         "补采的 deny 与点击路径共用一个账目")
        note = self.conn.execute(
            "SELECT detail_note FROM snapshots WHERE round_id=? AND page_status='失败' "
            "ORDER BY id DESC LIMIT 1", (round_id,)).fetchone()["detail_note"]
        self.assertIn("反爬拦截（deny）", note, "失败理由要说清是 deny，不是这个商品的锅")
        self.assertIn("原始页面：", note, "deny 页的原文留着供校准")

    def test_the_backfill_shop_is_skipped_when_deny_hits_the_limit(self):
        """补采撞上店铺阈值：这家店记为未完成，后面的店接着跑（与点击路径同款收尾）。"""
        round_id = new_round(self.db)
        shops = [Shop("A01", "店A", "https://shop-a.example/"),
                 Shop("A02", "店B", "https://shop-b.example/")]
        for shop in shops:
            self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        # A01：榜单已完成，两个商品都待补采——补采时连续命中 deny 撞上店铺阈值。
        a01_offers = [(index, oid, f"https://detail.1688.com/offer/{oid}.html", f"商品{oid}", "")
                      for index, oid in enumerate(("11", "12"), start=1)]
        self.db.save_shop_offers(round_id, "A01", shops[0].url, shops[0].name, a01_offers, 1)
        for index, oid in enumerate(("11", "12"), start=1):
            self.db.mark_failure(round_id, "A01", oid, 1, "解析失败", shop_url=shops[0].url,
                                 shop_name=shops[0].name,
                                 product_url=f"https://detail.1688.com/offer/{oid}.html",
                                 product_name=f"商品{oid}")
        # A02：还没开始，用它验证「跳过 A01 之后这一轮接着跑下一家」。
        self.db.submit_inventory_snapshot(
            round_id=round_id, shop_key="A02", shop_url=shops[1].url, shop_name=shops[1].name,
            offer_id="22", product_url="https://detail.1688.com/offer/22.html",
            list_title="商品22", detail_title="商品22", main_image_url=None,
            sku_rows=[{"sku_id": "22:1", "sku_name": "默认", "sku_price": 1.0,
                       "sku_stock": 3}],
            collected_at=utcnow(), attempt=1)
        cfg = crawler_cfg(deny_shop_limit=2, raw_page_dir=Path(self.tmp.name) / "raw")
        page = MagicMock()
        page.url = "https://s.1688.com/bsop-punish?x=1"
        crawled: list[str] = []

        def fake_crawl(listing_page, shop, cfg_, human, *, db, round_id, emit=None,
                       deny_tracker=None):
            crawled.append(shop.key)
            oid = "11" if shop.key == "A01" else "22"
            return [(1, oid, f"https://detail.1688.com/offer/{oid}.html", f"商品{oid}", "")], 1

        tracker = guard.DenyTracker(600)
        with patch.object(click_listing, "crawl_store_by_click", side_effect=fake_crawl), \
             patch.object(browser_pw, "open_detail", return_value="<html>deny</html>"):
            pipeline._run_listing_pw(self.db, cfg, round_id, shops, page,
                                     deny_tracker=tracker)

        self.assertEqual(crawled, ["A02"])
        self.assertEqual(tracker.shop_count("A01"), 2, "越界的这一次也记进账目")
        rows = {row["shop_key"]: row["list_status"] for row in self.conn.execute(
            "SELECT shop_key, list_status FROM shop_rounds WHERE round_id=?", (round_id,))}
        self.assertEqual(rows["A01"], "失败",
                         "补采撞阈值的那家店记为未完成（续跑时会接着补它）")
        self.assertEqual(rows["A02"], "完成", "跳过 A01 之后这一轮接着跑 A02")
        note = self.conn.execute(
            "SELECT list_note FROM shop_rounds WHERE round_id=? AND shop_key='A01'",
            (round_id,)).fetchone()["list_note"]
        self.assertIn("deny", note, "原因写得出来，续跑的人知道为什么停")

    def test_failed_offer_with_many_skus_triggers_failure_rate_pause(self):
        round_id = new_round(self.db)
        self.db.add_shop(round_id, "A01", "https://shop.example/", "店铺A")
        offers = [
            (index, str(index), f"https://detail.1688.com/offer/{index}.html", f"商品{index}", "")
            for index in range(1, 11)
        ]
        self.db.save_shop_offers(round_id, "A01", "https://shop.example/", "店铺A", offers, 1)
        self.db.submit_inventory_snapshot(
            round_id=round_id,
            shop_key="A01",
            shop_url="https://shop.example/",
            shop_name="店铺A",
            offer_id="1",
            product_url="https://detail.1688.com/offer/1.html",
            list_title="商品1",
            detail_title="商品1详情",
            main_image_url=None,
            sku_rows=[
                {"sku_id": f"1:{index}", "sku_name": f"规格{index}", "sku_price": 1.0, "sku_stock": 10}
                for index in range(10)
            ],
            collected_at="2026-09-08T00:00:00+00:00",
            attempt=1,
        )
        for offer_id in map(str, range(2, 11)):
            self.db.mark_failure(round_id, "A01", offer_id, 1, "解析失败")

        pipeline._finalize_round(self.db, crawler_cfg(), rounds.load(self.db, round_id))

        row = self.conn.execute(
            "SELECT terminal_reason, note FROM rounds WHERE id=?", (round_id,)
        ).fetchone()
        self.assertEqual(row["terminal_reason"], "FAIL_RATE_EXCEEDED")
        self.assertIn("90.0%", row["note"])

    def test_incomplete_listing_keeps_round_resumable(self):
        round_id = new_round(self.db)
        self.db.add_shop(round_id, "A01", "https://shop.example/", "店铺A")
        self.db.mark_listing_failure(round_id, "A01", "首屏无商品卡片")

        with self.assertRaises(RoundPauseRequired):
            pipeline._finalize_round(self.db, crawler_cfg(), rounds.load(self.db, round_id))

        row = self.conn.execute(
            "SELECT terminal_reason, list_status, list_note FROM rounds JOIN shop_rounds "
            "ON rounds.id=shop_rounds.round_id WHERE rounds.id=?", (round_id,)
        ).fetchone()
        self.assertIsNone(row["terminal_reason"])
        self.assertEqual(row["list_status"], "失败")
        self.assertIn("首屏", row["list_note"])

    def test_round_deny_exceeded_finishes_round_and_forces_new_round(self):
        db_path = Path(self.tmp.name) / "deny.db"
        cfg = crawler_cfg(db_file=db_path, driver="pw_cdp", ensure_dirs=MagicMock())
        exc = browser_pw.RoundDenyExceeded("整轮 10 分钟内 deny≥10")

        with isolated_locks(), patch.object(pipeline, "_run_pwcdp_round", side_effect=exc):
            pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://shop.example/")])

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT id, terminal_reason, phase, finished_at, note FROM rounds "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row["terminal_reason"], "DENY_EXCEEDED")
            self.assertEqual(row["phase"], "done")
            self.assertIsNotNone(row["finished_at"])
            self.assertIn("不可续跑", row["note"])
            self.assertEqual(new_round(Database(conn)), row["id"] + 1)
        finally:
            conn.close()

    def test_day_boundary_finishes_round_and_forces_new_round(self):
        db_path = Path(self.tmp.name) / "day.db"
        cfg = crawler_cfg(db_file=db_path, driver="pw_cdp", ensure_dirs=MagicMock())
        with isolated_locks(), patch.object(pipeline, "_run_pwcdp_round",
                                            side_effect=DayBoundaryReached()):
            pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://shop.example/")])

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT id, terminal_reason, phase, finished_at, note FROM rounds "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row["terminal_reason"], "DAY_BOUNDARY")
            self.assertEqual(row["phase"], "done")
            self.assertIsNotNone(row["finished_at"])
            self.assertEqual(row["note"], DAY_BOUNDARY_NOTE)
            self.assertEqual(new_round(Database(conn)), row["id"] + 1)
        finally:
            conn.close()

    def test_unconfirmed_empty_listing_is_rejected(self):
        round_id = new_round(self.db)
        self.db.add_shop(round_id, "A01", "https://shop.example/", "店铺A")
        with self.assertRaisesRegex(ValueError, "空榜单"):
            self.db.save_shop_offers(round_id, "A01", "https://shop.example/", "店铺A", [], 1)

    def _seed_same_name_failed_offer(self):
        """商品 11 今日已完整观测；同名商品 22 只有失败记录。返回 22 的待补采行。"""
        round_id = new_round(self.db)
        shop = Shop("A01", "店铺A", "https://shop.example/")
        self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        url11 = "https://detail.1688.com/offer/11.html"
        url22 = "https://detail.1688.com/offer/22.html"
        self.db.save_shop_offers(
            round_id, shop.key, shop.url, shop.name,
            [(1, "11", url11, "厨房清洁膏", ""), (2, "22", url22, "厨房清洁膏", "")],
            1,
        )
        self.db.submit_inventory_snapshot(
            round_id=round_id, shop_key=shop.key, shop_url=shop.url, shop_name=shop.name,
            offer_id="11", product_url=url11, list_title="厨房清洁膏",
            detail_title="厨房清洁膏", main_image_url=None,
            sku_rows=[{"sku_id": "s1", "sku_name": "标准", "sku_price": 9.9, "sku_stock": 200}],
            collected_at=utcnow(), attempt=1,
        )
        self.db.mark_failure(round_id, shop.key, "22", 1, "解析失败")
        offer = next(
            row for row in self.db.pending_offers(round_id, max_attempts=2)
            if row["offer_id"] == "22"
        )
        return round_id, offer

    @staticmethod
    def _detail_html() -> str:
        """补采 adapter 交回的真 html（解析、取主图都在 `detail.observe_page` 里）。"""
        return DETAIL_HTML

    def test_same_name_inventory_does_not_block_failed_offer_detail(self):
        round_id, offer = self._seed_same_name_failed_offer()
        cfg = crawler_cfg()
        with patch.object(browser_pw, "open_detail",
                          return_value=self._detail_html()) as capture:
            pipeline._capture_one_pw(self.db, cfg, Humanizer(cfg), round_id, offer, MagicMock())

        self.assertEqual(capture.call_count, 1, "同名库存不应阻止已知 offer_id 的补采")

    def test_retry_stops_and_signals_when_detail_budget_exhausted(self):
        round_id = new_round(self.db)
        shop = Shop("A01", "店铺A", "https://shop.example/")
        self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        self.db.save_shop_offers(
            round_id, shop.key, shop.url, shop.name,
            [(1, "11", "https://detail.1688.com/offer/11.html", "商品1", ""),
             (2, "22", "https://detail.1688.com/offer/22.html", "商品2", "")],
            1,
        )
        self.db.mark_failure(round_id, shop.key, "11", 1, "解析失败")
        self.db.mark_failure(round_id, shop.key, "22", 1, "解析失败")
        cfg = crawler_cfg(max_detail_opportunities_per_round=1)

        with patch.object(browser_pw, "open_detail",
                          return_value=self._detail_html()) as capture:
            with self.assertRaises(pipeline.DetailBudgetExhausted):
                pipeline._retry_shop_pending_pw(
                    self.db, cfg, round_id, shop, MagicMock(), Humanizer(cfg),
                )

        self.assertEqual(capture.call_count, 1, "预算耗尽后不再访问详情")
        self.assertEqual(self.db.detail_opportunity_total(round_id), 1)

    def test_detail_budget_exhaustion_finishes_round_with_terminal_note(self):
        db_path = Path(self.tmp.name) / "budget-terminal.db"
        cfg = crawler_cfg(db_file=db_path, driver="pw_cdp", ensure_dirs=MagicMock())
        with isolated_locks(), patch.object(
                pipeline, "_run_pwcdp_round",
                side_effect=pipeline.DetailBudgetExhausted("预算耗尽")):
            pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://shop.example/")])

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT terminal_reason, phase, finished_at, note FROM rounds "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row["terminal_reason"], "DETAIL_BUDGET_EXHAUSTED")
            self.assertEqual(row["phase"], "done")
            self.assertIsNotNone(row["finished_at"])
            self.assertEqual(row["note"], pipeline.DETAIL_BUDGET_NOTE)
        finally:
            conn.close()

    def test_budget_exhaustion_keeps_the_listing_progress(self):
        """预算在抓榜单途中用尽：已发现的商品仍在库里，店铺记为未完成并说明原因。

        已发现商品由抓取途中的增量落库保住，不再靠异常携带它们出来。
        """
        round_id = new_round(self.db)
        shop = Shop("A01", "店铺A", "https://shop.example/")
        self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        url11 = "https://detail.1688.com/offer/11.html"

        def fake_crawl(*args, **kwargs):
            db, rid = kwargs["db"], kwargs["round_id"]
            db.remember_shop_offer(rid, shop.key, shop.url, shop.name,
                                   (1, "11", url11, "商品1", ""))
            raise pipeline.DetailBudgetExhausted(pipeline.DETAIL_BUDGET_NOTE)

        with patch.object(click_listing, "crawl_store_by_click", side_effect=fake_crawl):
            with self.assertRaises(pipeline.DetailBudgetExhausted):
                pipeline._run_listing_pw(
                    self.db, crawler_cfg(), round_id, [shop], MagicMock(),
                )

        offers = self.db.conn.execute(
            "SELECT offer_id FROM shop_offers WHERE round_id=? AND shop_key=?",
            (round_id, shop.key),
        ).fetchall()
        self.assertEqual([row["offer_id"] for row in offers], ["11"], "已发现商品要保存")
        round_row = self.db.conn.execute(
            "SELECT list_status, offer_count, list_note FROM shop_rounds "
            "WHERE round_id=? AND shop_key=?",
            (round_id, shop.key),
        ).fetchone()
        self.assertEqual(round_row["offer_count"], 1)
        self.assertEqual(round_row["list_status"], "失败", "尝试过但没拿到完整榜单")
        self.assertEqual(round_row["list_note"], pipeline.DETAIL_BUDGET_NOTE,
                         "中断原因要留在店铺备注里")

    def test_skip_does_not_consume_detail_budget(self):
        """当日已完整观测的商品只跳过，不占用详情预算。"""
        round_id = new_round(self.db)
        shop = Shop("A01", "店铺A", "https://shop.example/")
        self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        url11 = "https://detail.1688.com/offer/11.html"
        self.db.save_shop_offers(
            round_id, shop.key, shop.url, shop.name, [(1, "11", url11, "商品1", "")], 1,
        )
        self.db.submit_inventory_snapshot(
            round_id=round_id, shop_key=shop.key, shop_url=shop.url, shop_name=shop.name,
            offer_id="11", product_url=url11, list_title="商品1", detail_title="商品1",
            main_image_url=None,
            sku_rows=[{"sku_id": "s1", "sku_name": "标准", "sku_price": 1.0, "sku_stock": 5}],
            collected_at=utcnow(), attempt=1,
        )
        offer = self.db.conn.execute(
            "SELECT * FROM shop_offers WHERE round_id=? AND offer_id='11'", (round_id,)
        ).fetchone()
        cfg = crawler_cfg(max_detail_opportunities_per_round=1)

        with patch.object(browser_pw, "open_detail") as capture:
            pipeline._capture_one_pw(self.db, cfg, Humanizer(cfg), round_id, offer, MagicMock())

        capture.assert_not_called()
        self.assertEqual(self.db.detail_opportunity_total(round_id), 0, "跳过不消耗详情预算")

    def test_retry_shares_attempt_budget_with_first_visit(self):
        """初次访问已用掉的尝试次数要从补采的额度里扣掉。"""
        round_id, offer = self._seed_same_name_failed_offer()   # 商品 22 已有 attempt=1 的失败记录
        cfg = crawler_cfg(max_attempts_per_page=2)

        with patch.object(browser_pw, "open_detail",
                          return_value=self._detail_html()) as capture:
            pipeline._capture_one_pw(self.db, cfg, Humanizer(cfg), round_id, offer, MagicMock())

        self.assertEqual(capture.call_count, 1)
        row = self.db.conn.execute(
            "SELECT MAX(attempt) AS a FROM snapshots WHERE round_id=? AND shop_key=? "
            "AND offer_id='22'",
            (round_id, offer["shop_key"]),
        ).fetchone()
        self.assertEqual(row["a"], 2, "补采应接着第 2 次尝试，而不是重新从 1 开始")

    def test_retry_does_nothing_when_attempts_already_spent(self):
        """尝试次数已在初次访问用尽时，补采不再访问详情。"""
        round_id, offer = self._seed_same_name_failed_offer()
        self.db.mark_failure(round_id, offer["shop_key"], "22", 2, "第二次也失败")
        cfg = crawler_cfg(max_attempts_per_page=2)

        with patch.object(browser_pw, "open_detail") as capture:
            pipeline._capture_one_pw(self.db, cfg, Humanizer(cfg), round_id, offer, MagicMock())

        capture.assert_not_called()

    def test_retry_fallback_failure_records_shared_attempt_number(self):
        """兜底异常也要接着已用掉的尝试次数，而不是写回第 1 次。"""
        round_id, offer = self._seed_same_name_failed_offer()   # 商品 22 已有 attempt=1
        shop = Shop("A01", "店铺A", "https://shop.example/")
        cfg = crawler_cfg()

        with patch.object(pipeline, "_capture_one_pw", side_effect=RuntimeError("boom")):
            pipeline._retry_shop_pending_pw(
                self.db, cfg, round_id, shop, MagicMock(), Humanizer(cfg),
            )

        row = self.db.conn.execute(
            "SELECT MAX(attempt) AS a FROM snapshots WHERE round_id=? AND shop_key=? "
            "AND offer_id='22'",
            (round_id, offer["shop_key"]),
        ).fetchone()
        self.assertEqual(row["a"], 2, "兜底失败也应记为第 2 次尝试")

    def test_no_orphan_snapshots_whichever_way_the_listing_ends(self):
        """四种离开榜单阶段的方式，都不留下「有快照、无榜单行」的商品。"""
        cases = [
            ("店铺 deny 跳店", browser_pw.ShopDenyExceeded("店铺 A01 10 分钟内 deny≥7"), False),
            ("整轮 deny 中止", browser_pw.RoundDenyExceeded("整轮 10 分钟内 deny≥10"), True),
            ("人工验证超时", InterventionTimeout("人工验证超时"), True),
            ("详情预算耗尽", pipeline.DetailBudgetExhausted(pipeline.DETAIL_BUDGET_NOTE), True),
        ]
        for label, exc, raises in cases:
            with self.subTest(label):
                with tempfile.TemporaryDirectory() as tmp:
                    conn = connect(Path(tmp) / "test.db")
                    try:
                        db = Database(conn)
                        round_id = new_round(db)
                        shop = Shop("A01", "店铺A", "https://shop-a.example/")
                        db.add_shop(round_id, shop.key, shop.url, shop.name)
                        url11 = "https://detail.1688.com/offer/11.html"

                        def fake_crawl(*args, _exc=exc, **kwargs):
                            d, rid = kwargs["db"], kwargs["round_id"]
                            d.remember_shop_offer(rid, shop.key, shop.url, shop.name,
                                                  (1, "11", url11, "商品11", ""))
                            d.submit_inventory_snapshot(
                                round_id=rid, shop_key=shop.key, shop_url=shop.url,
                                shop_name=shop.name, offer_id="11", product_url=url11,
                                list_title="商品11", detail_title="商品11",
                                main_image_url=None,
                                sku_rows=[{"sku_id": "s1", "sku_name": "标准",
                                           "sku_price": 1.0, "sku_stock": 5}],
                                collected_at=utcnow(), attempt=1,
                            )
                            raise _exc

                        with patch.object(click_listing, "crawl_store_by_click",
                                          side_effect=fake_crawl):
                            if raises:
                                with self.assertRaises(type(exc)):
                                    pipeline._run_listing_pw(
                                        db, crawler_cfg(), round_id, [shop], MagicMock())
                            else:
                                pipeline._run_listing_pw(
                                    db, crawler_cfg(), round_id, [shop], MagicMock())

                        self.assertEqual(check_orphans.audit(conn, round_id)["orphans"], [])
                    finally:
                        conn.close()

    def test_unexpected_error_propagates_without_faking_the_shop_state(self):
        """未预期异常：不吞、不伪装店铺状态，已落库的榜单行不丢。"""
        round_id = new_round(self.db)
        shop = Shop("A01", "店A", "https://shop-a.example/")
        self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        url11 = "https://detail.1688.com/offer/11.html"

        def fake_crawl(*args, **kwargs):
            db, rid = kwargs["db"], kwargs["round_id"]
            db.remember_shop_offer(rid, shop.key, shop.url, shop.name,
                                   (1, "11", url11, "商品11", ""))
            raise RuntimeError("浏览器崩了")

        with patch.object(click_listing, "crawl_store_by_click", side_effect=fake_crawl):
            with self.assertRaises(RuntimeError):
                pipeline._run_listing_pw(self.db, crawler_cfg(), round_id, [shop], MagicMock())

        row = self.conn.execute(
            "SELECT list_status, list_note FROM shop_rounds WHERE round_id=? AND shop_key='A01'",
            (round_id,),
        ).fetchone()
        self.assertEqual(row["list_status"], "待处理", "进程崩了不该假装知道这家店的进度")
        self.assertIsNone(row["list_note"])
        offers = self.conn.execute(
            "SELECT offer_id FROM shop_offers WHERE round_id=? AND shop_key='A01'",
            (round_id,),
        ).fetchall()
        self.assertEqual([r["offer_id"] for r in offers], ["11"], "已发现商品不丢")

    def test_intervention_timeout_records_the_shop_and_keeps_the_round_resumable(self):
        """人工验证等不到结果：店铺记为未完成，轮次仍可续跑，异常不被吞掉。"""
        round_id = new_round(self.db)
        shop = Shop("A01", "店A", "https://shop-a.example/")
        self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        url11 = "https://detail.1688.com/offer/11.html"

        def fake_crawl(*args, **kwargs):
            db, rid = kwargs["db"], kwargs["round_id"]
            db.remember_shop_offer(rid, shop.key, shop.url, shop.name,
                                   (1, "11", url11, "商品11", ""))
            raise InterventionTimeout("人工验证超时")

        with patch.object(click_listing, "crawl_store_by_click", side_effect=fake_crawl):
            with self.assertRaises(InterventionTimeout):
                pipeline._run_listing_pw(self.db, crawler_cfg(), round_id, [shop], MagicMock())

        row = self.conn.execute(
            "SELECT list_status, list_note FROM shop_rounds WHERE round_id=? AND shop_key='A01'",
            (round_id,),
        ).fetchone()
        self.assertEqual(row["list_status"], "失败")
        self.assertIn("人工", row["list_note"])
        terminal = self.conn.execute(
            "SELECT terminal_reason FROM rounds WHERE id=?", (round_id,)
        ).fetchone()["terminal_reason"]
        self.assertIsNone(terminal, "人工介入只是暂停，轮次仍可续跑")
        offers = self.conn.execute(
            "SELECT offer_id FROM shop_offers WHERE round_id=? AND shop_key='A01'",
            (round_id,),
        ).fetchall()
        self.assertEqual([r["offer_id"] for r in offers], ["11"])

    def test_round_deny_abort_records_current_shop_and_leaves_others_untouched(self):
        """整轮 deny 中止：正在处理的那家店如实记录，还没轮到的店仍是「未开始」。"""
        round_id = new_round(self.db)
        shops = [
            Shop("A01", "店A", "https://shop-a.example/"),
            Shop("A02", "店B", "https://shop-b.example/"),
        ]
        for shop in shops:
            self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        url11 = "https://detail.1688.com/offer/11.html"

        def fake_crawl(*args, **kwargs):
            db, rid = kwargs["db"], kwargs["round_id"]
            db.remember_shop_offer(rid, "A01", shop.url, shop.name,
                                   (1, "11", url11, "商品11", ""))
            raise browser_pw.RoundDenyExceeded("整轮 10 分钟内 deny≥10")

        with patch.object(click_listing, "crawl_store_by_click", side_effect=fake_crawl):
            with self.assertRaises(browser_pw.RoundDenyExceeded):
                pipeline._run_listing_pw(self.db, crawler_cfg(), round_id, shops, MagicMock())

        rows = self.conn.execute(
            "SELECT shop_key, list_status, list_note FROM shop_rounds "
            "WHERE round_id=? ORDER BY shop_key",
            (round_id,),
        ).fetchall()
        self.assertEqual([row["shop_key"] for row in rows], ["A01", "A02"])
        self.assertEqual(rows[0]["list_status"], "失败", "处理到一半被打断")
        self.assertIn("deny", rows[0]["list_note"])
        self.assertEqual(rows[1]["list_status"], "待处理", "还没轮到的店不背这个状态")
        self.assertIsNone(rows[1]["list_note"])
        offers = self.conn.execute(
            "SELECT offer_id FROM shop_offers WHERE round_id=? AND shop_key='A01'",
            (round_id,),
        ).fetchall()
        self.assertEqual([r["offer_id"] for r in offers], ["11"])

    def test_shop_deny_skip_keeps_discovered_offers_and_records_the_reason(self):
        """店铺 deny 跳店：已发现的商品与榜单行都留着，店铺记为未完成并带原因。"""
        round_id = new_round(self.db)
        shop = Shop("A01", "店铺A", "https://shop-a.example/")
        self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        url11 = "https://detail.1688.com/offer/11.html"

        def fake_crawl(*args, **kwargs):
            db, rid = kwargs["db"], kwargs["round_id"]
            db.remember_shop_offer(rid, shop.key, shop.url, shop.name,
                                   (1, "11", url11, "商品11", ""))
            raise browser_pw.ShopDenyExceeded("店铺 A01 10 分钟内 deny≥7")

        with patch.object(click_listing, "crawl_store_by_click", side_effect=fake_crawl):
            pipeline._run_listing_pw(self.db, crawler_cfg(), round_id, [shop], MagicMock())

        row = self.conn.execute(
            "SELECT list_status, offer_count, list_note FROM shop_rounds "
            "WHERE round_id=? AND shop_key='A01'",
            (round_id,),
        ).fetchone()
        self.assertEqual(row["list_status"], "失败", "尝试过但没拿到完整榜单")
        self.assertIn("deny", row["list_note"])
        self.assertEqual(row["offer_count"], 1)
        offers = self.conn.execute(
            "SELECT offer_id FROM shop_offers WHERE round_id=? AND shop_key='A01'",
            (round_id,),
        ).fetchall()
        self.assertEqual([r["offer_id"] for r in offers], ["11"], "已发现商品留在榜单里")

    def test_listing_failure_keeps_that_shop_pending_and_continues(self):
        round_id = new_round(self.db)
        shops = [
            Shop("A01", "失败店", "https://shop-a.example/"),
            Shop("A02", "成功店", "https://shop-b.example/"),
        ]
        for shop in shops:
            self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        cfg = crawler_cfg(raw_page_dir=Path(self.tmp.name) / "raw")
        ok_offers = [(1, "22", "https://detail.1688.com/offer/22.html", "商品", "")]
        with patch.object(
            click_listing,
            "crawl_store_by_click",
            side_effect=[ListingLoadFailed("首屏无卡片", "<html>failed</html>"), (ok_offers, 1)],
        ), patch.object(pipeline, "_retry_shop_pending_pw") as retry:
            pipeline._run_listing_pw(self.db, cfg, round_id, shops, MagicMock())

        rows = self.conn.execute(
            "SELECT shop_key, list_status FROM shop_rounds WHERE round_id=? ORDER BY shop_key", (round_id,)
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("A01", "失败"), ("A02", "完成")])
        self.assertTrue((Path(self.tmp.name) / "raw" / f"round_{round_id}" / "listing_A01.html").exists())
        # 只有榜单成功的 A02 会立即进入补抓，A01 榜单失败则不会。
        retry.assert_called_once()
        self.assertEqual(retry.call_args[0][3].key, "A02")

    def test_failed_offers_retried_immediately_after_their_shop(self):
        round_id = new_round(self.db)
        shops = [
            Shop("A01", "店A", "https://shop-a.example/"),
            Shop("A02", "店B", "https://shop-b.example/"),
        ]
        for shop in shops:
            self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        cfg = crawler_cfg()

        self.db.mark_failure(
            round_id, "A01", "1", 1, "解析失败",
            shop_url="https://shop-a.example/", shop_name="店A",
            product_url="https://detail.1688.com/offer/1.html", product_name="商品1",
        )
        self.db.mark_skipped(round_id, "A02", "https://shop-b.example/", "店B", "2",
                             "https://detail.1688.com/offer/2.html", "商品2", "今日已有库存，跳过")

        events = []

        def fake_crawl(page, shop, cfg_, human, db=None, round_id=None, emit=None,
                       deny_tracker=None):
            events.append(("list", shop.key))
            oid = "1" if shop.key == "A01" else "2"
            return [(1, oid, f"https://detail.1688.com/offer/{oid}.html", f"商品{oid}", "")], 1

        def fake_capture(db, cfg_, human, rid, offer, page, emit=None, deny_tracker=None):
            events.append(("retry", offer["shop_key"]))

        with patch.object(click_listing, "crawl_store_by_click", side_effect=fake_crawl), \
             patch.object(pipeline, "_capture_one_pw", side_effect=fake_capture):
            pipeline._run_listing_pw(self.db, cfg, round_id, shops, MagicMock())

        self.assertEqual(events, [("list", "A01"), ("retry", "A01"), ("list", "A02")])


if __name__ == "__main__":
    unittest.main()
