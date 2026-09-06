import unittest

from bestseller_monitor.browser_pw import (
    DenyTracker, ShopDenyExceeded, RoundDenyExceeded,
)


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


if __name__ == "__main__":
    unittest.main()
