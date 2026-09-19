import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor.config import Shop
from bestseller_monitor.guard import DenyTracker, RoundDenyExceeded, ShopDenyExceeded


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
