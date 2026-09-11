import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_dp, browser_pw, pipeline
from bestseller_monitor.config import Shop
from bestseller_monitor.db import (
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
            "max_attempts_per_page": 2,
            "max_detail_pages_per_round": 1000,
            "shuffle_within_shop": False,
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

    def test_capture_one_success_uses_submitted_payload_rows_for_logging(self):
        round_id = self.db.start_or_resume()
        offer = {
            "shop_key": "A01",
            "shop_url": "https://shop.example/",
            "shop_name": "店铺A",
            "offer_id": "111",
            "product_url": "https://detail.1688.com/offer/111.html",
            "list_title": "榜单标题",
        }
        payload = {
            "html": "<html></html>",
            "product_name": "详情标题",
            "rows": [{"sku_id": "red", "sku_name": "红色", "sku_price": 10, "sku_stock": 3}],
        }
        with patch.object(pipeline, "capture_detail_payload", return_value=payload), \
             patch.object(pipeline, "extract_main_image", return_value=None):
            pipeline._capture_one(
                self.db, self._cfg(), MagicMock(), round_id, offer, MagicMock(),
            )

        row = self.conn.execute(
            "SELECT sku_id, sku_stock FROM snapshots WHERE round_id=? AND page_status='成功'",
            (round_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("red", 3))

    def test_failed_offer_with_many_skus_triggers_failure_rate_pause(self):
        round_id = self.db.start_or_resume()
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

    def test_click_path_stops_when_detail_budget_exhausted(self):
        """点击式列表也必须遵守单轮详情预算。"""
        page = MagicMock()
        locator = MagicMock()
        locator.count.return_value = 1
        page.locator.return_value = locator
        human = MagicMock()
        db = MagicMock()
        db.inventory_exists_by_name.return_value = False
        db.claim_detail_opportunity.return_value = SimpleNamespace(
            granted=False, budget_exhausted=True, reused=False,
        )
        shop = Shop("A01", "店铺A", "https://shop.example/")

        with patch.object(browser_pw, "_wait_cards", return_value=True), \
             patch.object(browser_pw, "_scroll_cards_until_stable", return_value=1), \
             patch.object(browser_pw, "intervention_kind", return_value=None), \
             patch.object(browser_pw, "_click_text_in_frames", return_value=False), \
             patch.object(browser_pw, "_read_card_title", return_value="商品1"), \
             patch.object(browser_pw, "_capture_card") as capture:
            with self.assertRaises(pipeline.DetailBudgetExhausted):
                browser_pw.crawl_store_by_click(
                    page, shop, self._cfg(max_pages_per_shop=1), human, db=db, round_id=1,
                )

        capture.assert_not_called()

    def test_ingest_detail_binds_card_opportunity_to_known_offer(self):
        """卡片机会在学到商品编号后绑定过去，补采重试不再重复占预算。"""
        round_id = self.db.start_or_resume()
        shop = Shop("A01", "店铺A", "https://shop.example/")
        self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        self.assertTrue(self.db.claim_detail_opportunity(
            round_id, shop.key, "card:p1:i0", budget_limit=1,
        ).granted)

        detail_page = MagicMock()
        detail_page.url = "https://detail.1688.com/offer/22.html"
        detail_page.content.return_value = "<html></html>"
        offers, seen = [], set()
        cfg = self._cfg()
        with patch.object(browser_pw, "parse_detail_html",
                          return_value=self._detail_payload()):
            browser_pw._ingest_detail(
                MagicMock(), detail_page, MagicMock(), "厨房清洁膏", cfg, [False],
                MagicMock(), lambda *a, **k: None, self.db, round_id, shop, offers, seen,
                "page=1&idx=0", card_ref="card:p1:i0",
            )

        grant = self.db.claim_detail_opportunity(round_id, shop.key, "22", budget_limit=1)
        self.assertTrue(grant.granted)
        self.assertTrue(grant.reused, "卡片机会应已绑定到商品编号")
        self.assertEqual(self.db.detail_budget_used(round_id), 1)

    def _seed_same_name_failed_offer(self):
        """商品 11 今日已完整观测；同名商品 22 只有失败记录。返回 22 的待补采行。"""
        round_id = self.db.start_or_resume()
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
    def _detail_payload():
        return {
            "product_name": "厨房清洁膏",
            "rows": [{"sku_id": "s2", "sku_name": "标准", "sku_price": 9.9, "sku_stock": 150}],
            "html": "",
        }

    def test_same_name_inventory_does_not_block_failed_offer_detail(self):
        round_id, offer = self._seed_same_name_failed_offer()
        cfg = self._cfg()
        with patch.object(browser_pw, "capture_detail",
                          return_value=self._detail_payload()) as capture:
            pipeline._capture_one_pw(self.db, cfg, Humanizer(cfg), round_id, offer, MagicMock())

        self.assertEqual(capture.call_count, 1, "同名库存不应阻止已知 offer_id 的补采")

    def test_same_name_inventory_does_not_block_drission_detail(self):
        round_id, offer = self._seed_same_name_failed_offer()
        cfg = self._cfg()
        with patch.object(pipeline, "capture_detail_payload",
                          return_value=self._detail_payload()) as capture:
            pipeline._capture_one(self.db, cfg, Humanizer(cfg), round_id, offer, MagicMock())

        self.assertEqual(capture.call_count, 1, "同名库存不应阻止已知 offer_id 的补采")

    def test_same_name_inventory_does_not_block_drission_page_detail(self):
        round_id, offer = self._seed_same_name_failed_offer()
        cfg = self._cfg()
        with patch.object(browser_dp, "capture_detail_payload",
                          return_value=self._detail_payload()) as capture:
            pipeline._capture_one_dp(self.db, cfg, Humanizer(cfg), round_id, offer, MagicMock())

        self.assertEqual(capture.call_count, 1, "同名库存不应阻止已知 offer_id 的补采")

    def test_retry_stops_and_signals_when_detail_budget_exhausted(self):
        round_id = self.db.start_or_resume()
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
        cfg = self._cfg(max_detail_pages_per_round=1)

        with patch.object(browser_pw, "capture_detail",
                          return_value=self._detail_payload()) as capture:
            with self.assertRaises(pipeline.DetailBudgetExhausted):
                pipeline._retry_shop_pending_pw(
                    self.db, cfg, round_id, shop, MagicMock(), Humanizer(cfg),
                )

        self.assertEqual(capture.call_count, 1, "预算耗尽后不再访问详情")
        self.assertEqual(self.db.detail_budget_used(round_id), 1)

    def test_detail_budget_exhaustion_finishes_round_with_terminal_note(self):
        db_path = Path(self.tmp.name) / "budget-terminal.db"
        cfg = self._cfg(db_file=db_path, driver="pw_cdp", ensure_dirs=MagicMock())
        with patch.object(pipeline, "_run_pwcdp_round",
                          side_effect=pipeline.DetailBudgetExhausted("预算耗尽")):
            pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://shop.example/")])

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT status, phase, finished_at, note FROM rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row["status"], "详情预算耗尽")
            self.assertEqual(row["phase"], "done")
            self.assertIsNotNone(row["finished_at"])
            self.assertEqual(row["note"], pipeline.DETAIL_BUDGET_NOTE)
        finally:
            conn.close()

    def test_dp_detail_phase_stops_when_budget_exhausted(self):
        round_id = self.db.start_or_resume()
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
        cfg = self._cfg(max_detail_pages_per_round=1)

        with patch.object(pipeline, "capture_detail_payload",
                          return_value=self._detail_payload()) as capture:
            with self.assertRaises(pipeline.DetailBudgetExhausted):
                pipeline._run_detail_phase(self.db, cfg, round_id, MagicMock())

        self.assertEqual(capture.call_count, 1, "drission 详情阶段也应停在同一上限")
        self.assertEqual(self.db.detail_budget_used(round_id), 1)

    def test_skip_does_not_consume_detail_budget(self):
        """当日已完整观测的商品只跳过，不占用详情预算。"""
        round_id = self.db.start_or_resume()
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
        cfg = self._cfg(max_detail_pages_per_round=1)

        with patch.object(browser_pw, "capture_detail") as capture:
            pipeline._capture_one_pw(self.db, cfg, Humanizer(cfg), round_id, offer, MagicMock())

        capture.assert_not_called()
        self.assertEqual(self.db.detail_budget_used(round_id), 0, "跳过不消耗详情预算")

    def test_retry_shares_attempt_budget_with_first_visit(self):
        """初次访问已用掉的尝试次数要从补采的额度里扣掉。"""
        round_id, offer = self._seed_same_name_failed_offer()   # 商品 22 已有 attempt=1 的失败记录
        cfg = self._cfg(max_attempts_per_page=2)

        with patch.object(browser_pw, "capture_detail",
                          return_value=self._detail_payload()) as capture:
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
        cfg = self._cfg(max_attempts_per_page=2)

        with patch.object(browser_pw, "capture_detail") as capture:
            pipeline._capture_one_pw(self.db, cfg, Humanizer(cfg), round_id, offer, MagicMock())

        capture.assert_not_called()

    def test_retry_fallback_failure_records_shared_attempt_number(self):
        """兜底异常也要接着已用掉的尝试次数，而不是写回第 1 次。"""
        round_id, offer = self._seed_same_name_failed_offer()   # 商品 22 已有 attempt=1
        shop = Shop("A01", "店铺A", "https://shop.example/")
        cfg = self._cfg()

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

    def test_click_detail_closes_popup_before_stopping_at_day_boundary(self):
        round_id = self.db.start_or_resume()
        page = MagicMock()
        popup = MagicMock()
        detail_page = MagicMock()
        detail_page.url = "https://detail.1688.com/offer/11.html"
        detail_page.content.return_value = (
            '<script>{"skuInfoMap":{"A":{"skuId":1,"canBookCount":1}}}</script>'
        )
        shop = Shop("A01", "店铺A", "https://shop.example/")
        with patch("bestseller_monitor.db.past_day_cutoff", return_value=True):
            with self.assertRaises(DayBoundaryReached):
                browser_pw._ingest_detail(
                    page, detail_page, popup, "商品", self._cfg(), [False], MagicMock(),
                    MagicMock(), self.db, round_id, shop, [], set(), "page=1&idx=0",
                )

        popup.close.assert_called_once_with()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 1)

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
        round_id = self.db.start_or_resume()
        shops = [
            Shop("A01", "店A", "https://shop-a.example/"),
            Shop("A02", "店B", "https://shop-b.example/"),
        ]
        for shop in shops:
            self.db.add_shop(round_id, shop.key, shop.url, shop.name)
        cfg = self._cfg()

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

        def fake_capture(db, cfg_, human, rid, offer, page, emit=None):
            events.append(("retry", offer["shop_key"]))

        with patch.object(browser_pw, "crawl_store_by_click", side_effect=fake_crawl), \
             patch.object(pipeline, "_capture_one_pw", side_effect=fake_capture):
            pipeline._run_listing_pw(self.db, cfg, round_id, shops, MagicMock())

        self.assertEqual(events, [("list", "A01"), ("retry", "A01"), ("list", "A02")])


if __name__ == "__main__":
    unittest.main()
