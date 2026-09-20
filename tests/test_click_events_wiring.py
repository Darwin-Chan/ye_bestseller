"""跨模块：同一条点击事件流，两个消费点各取各的切片。

生产侧（遍历）把事件写进临时 SQLite，数据层与分析工具各自去读。这条用例钉住候选 04
真正要的结果：一份事件在两个指标里得到各自该得的数，且 `click_parse_error` 不被双计、
也不再从报表里消失。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import analyze_click as ac

from bestseller_monitor import click_listing, detail, detail_visit
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, connect
from frozen_clock import frozen_clock
from helpers import FakeCard, ScriptedListing, crawler_cfg, new_round


class ClickEventConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(frozen_clock())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = connect(Path(tmp.name) / "click.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.round_id = new_round(self.db, "A01")
        self.shop = Shop("A01", "店铺A", "https://A01.example/")

    def crawl(self, cards):
        """跑一次真遍历，事件经 `db.event_logger` 落进这个临时库。"""
        walk = click_listing.ShopWalk(
            ScriptedListing([cards]), self.shop, crawler_cfg(), MagicMock(),
            db=self.db, round_id=self.round_id,
            emit=self.db.event_logger(self.round_id))
        return walk.run()

    def test_the_two_consumers_take_their_own_slice_of_the_same_events(self):
        # 解析失败那张的 HTML 一直不可读；把可读等待的窗口收掉，用例不必真等十秒。
        with patch.object(detail_visit, "DETAIL_READY_TIMEOUT_SEC", 0.0):
            self.crawl([
                FakeCard("商品1", offer_id="11"),
                FakeCard("商品2", offer_id="22",
                         observation=detail.Observation.parse_failed(
                             RuntimeError("页面结构变了"), "<html></html>")),
                FakeCard("商品3", offer_id=None, opened=False),
            ])

        # 卡片口径：只有「没打开」那一张。解析失败那张拿到了商品编号，它的失败已经由
        # 失败快照计入 `failed_offers`，在卡片口径里再算一次就是双计。
        self.assertEqual(self.db.click_card_failures(self.round_id), 1)
        tally = self.db.round_tally(self.round_id)
        self.assertEqual(tally.total.failed_offers, 1, "解析失败由快照失败计入")

        # 事件口径：三次点击都算尝试，只有一次成功——改前解析失败整条不进分母。
        res = ac.compute(ac.load_click_rows(self.conn, round_id=self.round_id))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["attempted"], 3)
        self.assertAlmostEqual(res[0]["success_rate"], 1 / 3)
        self.assertEqual(res[0]["click_parse_error"], 1)
        self.assertEqual(res[0]["click_no_popup"], 1)


if __name__ == "__main__":
    unittest.main()
