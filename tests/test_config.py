import tempfile
import unittest
from pathlib import Path

from bestseller_monitor.config import load_shops


class ConfigTests(unittest.TestCase):
    def test_load_shops_and_skip_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "shops.csv"
            p.write_text(
                "shop_key,shop_name,shop_url\n"
                "A01,店一,https://a.1688.com/\n"
                "\n"
                "A02,店二,https://b.1688.com/\n",
                encoding="utf-8",
            )
            shops = load_shops(p)
            self.assertEqual(len(shops), 2)
            self.assertEqual(shops[0].key, "A01")

    def test_duplicate_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "shops.csv"
            p.write_text(
                "shop_key,shop_name,shop_url\n"
                "A01,店一,https://a.1688.com/\n"
                "A01,店二,https://b.1688.com/\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_shops(p)


if __name__ == "__main__":
    unittest.main()
