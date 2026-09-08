import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw, pipeline
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, connect, DayBoundaryReached, DAY_BOUNDARY_NOTE
from bestseller_monitor.detail import DetailParseFailed, parse_detail_html
from bestseller_monitor.guard import InterventionTimeout, RoundPauseRequired
from bestseller_monitor.listing import ListingLoadFailed


class P1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.db = Database(self.conn)
        self._day_patcher = patch("bestseller_monitor.db.past_day_cutoff", return_value=False)
        self._day_patcher.start()

    def tearDown(self):
        self._day_patcher.stop()
        self.conn.close()
        self.tmp.cleanup()

    @staticmethod
    def _cfg(**overrides):
        values = {
            "timeout_ms": 1,
            "human_pause_minutes": 1,
            "intervention_confirmation_sec": 0,
            "fail_rate_limit": 0.1,
            "long_pause_interval": (1, 1),
            "detail_delay_sec": (0.0, 0.0),
            "long_pause_sec": (0.0, 0.0),
            "batch_size": 1,
            "batch_rest_sec": (0.0, 0.0),
            "list_delay_sec": (0.0, 0.0),
            "action_delay_sec": (0.0, 0.0),
            "read_delay_sec": (0.0, 0.0),
            "retry_base_sec": 0.0,
            "retry_jitter_sec": 0.0,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

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

    def test_failed_offer_with_many_skus_triggers_failure_rate_pause(self):
        round_id = self.db.start_or_resume()
        self.db.add_shop(round_id, "A01", "https://shop.example/", "店铺A")
        offers = [
            (index, str(index), f"https://detail.1688.com/offer/{index}.html", f"商品{index}", "")
            for index in range(1, 11)
        ]
        self.db.save_shop_offers(round_id, "A01", "https://shop.example/", "店铺A", offers, 1)
        rows = [{
            "round_id": round_id, "shop_key": "A01", "shop_url": "https://shop.example/",
            "shop_name": "店铺A", "offer_id": "1", "product_url": "https://detail.1688.com/offer/1.html",
            "product_name": "商品1", "sku_id": f"1:{index}", "sku_name": f"规格{index}",
            "sku_price": 1.0, "sku_stock": 10, "collected_at": "2026-09-08T00:00:00+00:00",
            "page_status": "成功", "attempt": 1,
        } for index in range(10)]
        self.db.save_snapshot_rows(round_id, "A01", rows)
        for offer_id in map(str, range(2, 11)):
            self.db.mark_failure(round_id, "A01", offer_id, 1, "解析失败")

        pipeline._finalize_round(self.db, self._cfg(), round_id)

        row = self.conn.execute("SELECT status, note FROM rounds WHERE id=?", (round_id,)).fetchone()
        self.assertEqual(row["status"], "需人工-失败率超限")
        self.assertIn("90.0%", row["note"])

    def test_incomplete_listing_keeps_round_resumable(self):
        round_id = self.db.start_or_resume()
        self.db.add_shop(round_id, "A01", "https://shop.example/", "店铺A")
        self.db.mark_listing_failure(round_id, "A01", "首屏无商品卡片")

        with self.assertRaises(RoundPauseRequired):
            pipeline._finalize_round(self.db, self._cfg(), round_id)

        row = self.conn.execute("SELECT status, list_status, list_note FROM rounds JOIN shop_rounds "
                                "ON rounds.id=shop_rounds.round_id WHERE rounds.id=?", (round_id,)).fetchone()
        self.assertEqual(row["status"], "进行中")
        self.assertEqual(row["list_status"], "失败")
        self.assertIn("首屏", row["list_note"])

    def test_round_deny_exceeded_finishes_round_and_forces_new_round(self):
        db_path = Path(self.tmp.name) / "deny.db"
        cfg = SimpleNamespace(
            db_file=db_path,
            driver="pw_cdp",
            ensure_dirs=MagicMock(),
        )
        exc = browser_pw.RoundDenyExceeded("整轮 10 分钟内 deny≥10")

        with patch.object(pipeline, "_run_pwcdp_round", side_effect=exc):
            pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://shop.example/")])

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT id, status, phase, finished_at, note FROM rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row["status"], "意外中止")
            self.assertEqual(row["phase"], "done")
            self.assertIsNotNone(row["finished_at"])
            self.assertIn("不可续跑", row["note"])
            self.assertEqual(Database(conn).start_or_resume(), row["id"] + 1)
        finally:
            conn.close()

    def test_day_boundary_finishes_round_and_forces_new_round(self):
        db_path = Path(self.tmp.name) / "day.db"
        cfg = SimpleNamespace(
            db_file=db_path,
            driver="pw_cdp",
            ensure_dirs=MagicMock(),
        )
        with patch.object(pipeline, "_run_pwcdp_round", side_effect=DayBoundaryReached()):
            pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://shop.example/")])

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT id, status, phase, finished_at, note FROM rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row["status"], "意外中止")
            self.assertEqual(row["phase"], "done")
            self.assertIsNotNone(row["finished_at"])
            self.assertEqual(row["note"], DAY_BOUNDARY_NOTE)
            self.assertEqual(Database(conn).start_or_resume(), row["id"] + 1)
        finally:
            conn.close()

    def test_unconfirmed_empty_listing_is_rejected(self):
        round_id = self.db.start_or_resume()
        self.db.add_shop(round_id, "A01", "https://shop.example/", "店铺A")
        with self.assertRaisesRegex(ValueError, "空榜单"):
            self.db.save_shop_offers(round_id, "A01", "https://shop.example/", "店铺A", [], 1)

    def test_click_popup_intervention_timeout_propagates(self):
        page = MagicMock()
        popup = MagicMock()
        page.expect_popup.return_value.__enter__.return_value = SimpleNamespace(value=popup)
        with patch.object(browser_pw, "intervention_kind", return_value="滑块"), \
             patch.object(browser_pw, "wait_for_resolution", side_effect=InterventionTimeout("超时")):
            with self.assertRaises(InterventionTimeout):
                browser_pw._click_one_product(page, MagicMock(), self._cfg(), [False], MagicMock())
        self.assertEqual(
            page.expect_popup.call_args.kwargs["timeout"], browser_pw._WAIT_POPUP_MS,
        )

    def test_existing_name_is_skipped_before_detail_wait(self):
        page = MagicMock()
        locator = MagicMock()
        locator.count.return_value = 1
        page.locator.return_value = locator
        human = MagicMock()
        db = MagicMock()
        db.inventory_exists_by_name.return_value = True
        db.find_offer_id_by_name.return_value = "123"
        shop = Shop("A01", "店铺A", "https://shop.example/")

        with patch.object(browser_pw, "_wait_cards", return_value=True), \
             patch.object(browser_pw, "_scroll_cards_until_stable", return_value=1), \
             patch.object(browser_pw, "intervention_kind", return_value=None), \
             patch.object(browser_pw, "_click_text_in_frames", return_value=False), \
             patch.object(browser_pw, "_capture_card") as capture:
            offers, pages = browser_pw.crawl_store_by_click(
                page, shop, self._cfg(max_pages_per_shop=1), human,
                db=db, round_id=1,
            )

        self.assertEqual((len(offers), pages), (1, 1))
        human.before_detail.assert_not_called()
        capture.assert_not_called()
        db.mark_skipped.assert_called_once()

    def test_scroll_stops_after_first_no_change_window(self):
        page = MagicMock()
        locator = MagicMock()
        locator.count.return_value = 31
        page.locator.return_value = locator
        with patch.object(browser_pw, "_wait_until", return_value=False) as wait:
            self.assertEqual(browser_pw._scroll_cards_until_stable(page), 31)

        wait.assert_called_once()
        page.mouse.wheel.assert_called_once_with(0, 6000)

    def test_rescue_only_scrolls_pages_with_ambiguous_names(self):
        page = MagicMock()
        locator = MagicMock()
        locator.count.return_value = 1
        page.locator.return_value = locator
        human = MagicMock()
        db = MagicMock()
        db.inventory_exists_by_name.return_value = False
        shop = Shop("A01", "店铺A", "https://shop.example/")
        titles = iter(["唯一商品", "重复商品", "重复商品", "重复商品", "重复商品"])
        scroll_counts = iter([1, 2, 2])

        def capture(*args, **kwargs):
            args[10].append((len(args[10]) + 1, "offer", "url", "name", ""))

        with patch.object(browser_pw, "_wait_cards", return_value=True), \
             patch.object(browser_pw, "_scroll_cards_until_stable",
                          side_effect=lambda *args: next(scroll_counts)) as scroll, \
             patch.object(browser_pw, "_read_card_title",
                          side_effect=lambda *_args: next(titles)), \
             patch.object(browser_pw, "intervention_kind", return_value=None), \
             patch.object(browser_pw, "_click_text_in_frames", return_value=True), \
             patch.object(browser_pw, "_capture_card", side_effect=capture):
            offers, pages = browser_pw.crawl_store_by_click(
                page, shop, self._cfg(max_pages_per_shop=2), human,
                db=db, round_id=1,
            )

        self.assertEqual((len(offers), pages), (5, 2))
        # Initial pages 1/2 plus rescue page 2; rescue page 1 has no ambiguous name.
        self.assertEqual(scroll.call_count, 3)

    def test_click_detail_parse_exception_is_isolated_and_archived(self):
        round_id = self.db.start_or_resume()
        page = MagicMock()
        popup = MagicMock()
        detail_page = MagicMock()
        detail_page.url = "https://detail.1688.com/offer/11.html"
        detail_page.content.return_value = (
            '<script>{"skuInfoMap":{"A":{"skuId":1,"canBookCount":1}}}</script>'
        )
        shop = Shop("A01", "店铺A", "https://shop.example/")
        cfg = self._cfg(raw_page_dir=Path(self.tmp.name) / "raw")
        with patch.object(browser_pw, "extract_main_image", side_effect=ValueError("图片字段异常")):
            result = browser_pw._ingest_detail(
                page, detail_page, popup, "商品", cfg, [False], MagicMock(), MagicMock(), self.db,
                round_id, shop, [], set(), "page=1&idx=0",
            )

        self.assertIsNone(result)
        row = self.conn.execute(
            "SELECT page_status, detail_note FROM snapshots WHERE round_id=?", (round_id,)
        ).fetchone()
        self.assertEqual(row["page_status"], "失败")
        self.assertIn("图片字段异常", row["detail_note"])
        self.assertTrue((Path(self.tmp.name) / "raw" / f"round_{round_id}" / "11.html").exists())

    def test_click_listing_rejects_unconfirmed_zero_cards(self):
        page = MagicMock()
        page.content.return_value = "<html></html>"
        shop = Shop("A01", "店铺A", "https://shop.example/")
        with patch.object(browser_pw, "_wait_cards", return_value=False), \
             patch.object(browser_pw, "intervention_kind", return_value=None):
            with self.assertRaises(ListingLoadFailed):
                browser_pw.crawl_store_by_click(
                    page, shop, self._cfg(max_pages_per_shop=1), MagicMock(),
                )

    def test_listing_failure_keeps_that_shop_pending_and_continues(self):
        round_id = self.db.start_or_resume()
        shops = [
            Shop("A01", "失败店", "https://shop-a.example/"),
            Shop("A02", "成功店", "https://shop-b.example/"),
        ]
        for shop in shops:
            self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        cfg = self._cfg(raw_page_dir=Path(self.tmp.name) / "raw")
        ok_offers = [(1, "22", "https://detail.1688.com/offer/22.html", "商品", "")]
        with patch.object(
            browser_pw,
            "crawl_store_by_click",
            side_effect=[ListingLoadFailed("首屏无卡片", "<html>failed</html>"), (ok_offers, 1)],
        ):
            pipeline._run_listing_pw(self.db, cfg, round_id, shops, MagicMock())

        rows = self.conn.execute(
            "SELECT shop_key, list_status FROM shop_rounds WHERE round_id=? ORDER BY shop_key", (round_id,)
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("A01", "失败"), ("A02", "完成")])
        self.assertTrue((Path(self.tmp.name) / "raw" / f"round_{round_id}" / "listing_A01.html").exists())


if __name__ == "__main__":
    unittest.main()
