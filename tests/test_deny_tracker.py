import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw
from bestseller_monitor.browser_pw import (
    DenyTracker, ShopDenyExceeded, RoundDenyExceeded,
)
from bestseller_monitor.config import Shop


class DenyTrackerTests(unittest.TestCase):
    def test_counts_and_rolling_window(self):
        t = DenyTracker(window_sec=600)
        t.record("A")
        t.record("A")
        t.record("A")
        t.record("B")
        self.assertEqual(t.shop_count("A"), 3)
        self.assertEqual(t.shop_count("B"), 1)
        self.assertEqual(t.round_count(), 4)

    def test_exceptions_importable(self):
        self.assertTrue(issubclass(ShopDenyExceeded, Exception))
        self.assertTrue(issubclass(RoundDenyExceeded, Exception))

    def test_third_deny_closes_detail_and_skips_product(self):
        page = MagicMock()
        detail_page = MagicMock()
        detail_page.url = "https://example.invalid/deny_pc"
        popup = MagicMock()
        cfg = SimpleNamespace(
            deny_backoff_sec=0.0,
            deny_retry2_backoff_sec=0.0,
            deny_round_limit=10,
            deny_shop_limit=7,
        )
        shop = Shop("A01", "店铺A", "https://shop.example/")
        emit = MagicMock()
        human = MagicMock()

        with patch.object(browser_pw, "_click_one_product", return_value=(detail_page, popup)) as click, \
             patch.object(browser_pw, "_close_popup_or_back") as close:
            result = browser_pw._capture_card(
                page, MagicMock(), "商品", cfg, [False], MagicMock(), emit, None, None,
                shop, [], set(), human=human, deny_tracker=DenyTracker(600),
            )

        self.assertIsNone(result)
        self.assertEqual(click.call_count, 3)
        self.assertEqual(close.call_count, 3)
        self.assertEqual(human.sleep.call_count, 2)
        self.assertTrue(any("&n=3&skip" in call.kwargs["note"] for call in emit.call_args_list))


if __name__ == "__main__":
    unittest.main()
