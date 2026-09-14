"""续跑只重新抓未完成店铺（IS-33）。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw, click_listing, pipeline
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, connect
from helpers import crawler_cfg, new_round

# 本轮已完成店铺（A01）的榜单行：续跑不该动它。
COMPLETED_OFFERS = [(1, "11", "https://detail.1688.com/offer/11.html", "旧榜单标题", "")]
# 未完成店铺（A02）这次抓到的行。
UNFINISHED_OFFERS = [(1, "99", "https://detail.1688.com/offer/99.html", "新榜单标题", "")]


class ListingResumeTests(unittest.TestCase):
    """IS-33：一条已完成、一条未完成的店铺范围，续跑只抓未完成的那条。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.db = Database(self.conn)
        self.round_id = new_round(self.db)
        self.a01 = Shop("A01", "店A", "https://shop-a.example/")
        self.a02 = Shop("A02", "店B", "https://shop-b.example/")
        self.shops = [self.a01, self.a02]
        for shop in self.shops:
            self.db.add_shop(self.round_id, shop.key, shop.url, shop.name)
        # A01 本轮榜单已经完成；A02 还没轮到，仍是「待处理」。
        self.db.save_shop_offers(
            self.round_id, self.a01.key, self.a01.url, self.a01.name, COMPLETED_OFFERS, 1,
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _crawled_keys(self, crawl) -> list[str]:
        return [call.args[1].key for call in crawl.call_args_list]

    def _assert_only_the_unfinished_shop_was_crawled(self, crawl):
        rows = self.conn.execute(
            "SELECT offer_id, list_title FROM shop_offers "
            "WHERE round_id=? AND shop_key='A01' ORDER BY rank",
            (self.round_id,),
        ).fetchall()
        self.assertEqual(self._crawled_keys(crawl), ["A02"], "已完成店铺不再抓，未完成店铺继续抓")
        self.assertEqual(
            [tuple(row) for row in rows], [("11", "旧榜单标题")], "本轮已完成的榜单行不被重写",
        )
        statuses = {row["shop_key"]: row["list_status"] for row in self.conn.execute(
            "SELECT shop_key, list_status FROM shop_rounds WHERE round_id=?",
            (self.round_id,),
        )}
        self.assertEqual(statuses, {"A01": "完成", "A02": "完成"}, "未完成店铺这一轮被补完")

    def test_cdp_click_path_skips_the_completed_shop(self):
        with patch.object(click_listing, "crawl_store_by_click",
                          return_value=(UNFINISHED_OFFERS, 1)) as crawl, \
             patch.object(pipeline, "_retry_shop_pending_pw"):
            pipeline._run_listing_pw(
                self.db, crawler_cfg(), self.round_id, self.shops, MagicMock(),
            )

        self._assert_only_the_unfinished_shop_was_crawled(crawl)


if __name__ == "__main__":
    unittest.main()
