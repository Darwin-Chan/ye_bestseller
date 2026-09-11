import tempfile
import unittest
from pathlib import Path

from bestseller_monitor.db import Database, connect
from tools import summary


class SummaryTests(unittest.TestCase):
    def test_skipped_offer_counts_as_handled_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            try:
                db = Database(conn)
                rid = db.start_or_resume()
                db.save_shop_offers(
                    rid, "A", "https://a.example/", "店铺A",
                    [(1, "111", "https://detail.1688.com/offer/111.html", "商品", "")], 1,
                )
                db.mark_skipped(
                    rid, "A", "https://a.example/", "店铺A", "111",
                    "https://detail.1688.com/offer/111.html", "商品",
                )

                counts = summary.summarize(conn, rid)

                self.assertEqual(counts["ok_offers"], 1, "跳过属于已处理")
                self.assertEqual(counts["fail_offers"], 0, "跳过不是失败")
                self.assertEqual(counts["shop_offers"], 1)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
