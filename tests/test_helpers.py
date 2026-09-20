"""测试夹具本身：采集配置替身要跟得上真配置（候选 06）。"""
import dataclasses
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor import rounds
from bestseller_monitor.config import Config
from bestseller_monitor.db import Database, PARAMS_KEYS, connect, utcnow
from frozen_clock import FROZEN_DATE, FROZEN_NOW, frozen_clock
from helpers import crawler_cfg, cst_date, new_round


class CrawlerCfgTests(unittest.TestCase):
    def test_the_stub_covers_every_key_a_round_records(self):
        """`record_params()` 会把 `PARAMS_KEYS` 逐键取出来记进 run_params：
        替身缺一个键，跑一整轮的那类用例就会在 `getattr` 上炸。"""
        missing = sorted(set(PARAMS_KEYS) - set(vars(crawler_cfg())))

        self.assertEqual(missing, [], f"crawler_cfg 缺这些键：{missing}")

    def test_the_stub_has_the_same_keys_as_the_real_config(self):
        """替身与真 `Config` 同形：真配置加了字段而替身没跟上时，读它的用例会静默走
        `getattr(..., 默认)` 那条路——这条护栏让「跟上」变成一次红灯。"""
        real = {field.name for field in dataclasses.fields(Config)}
        stub = set(vars(crawler_cfg()))

        self.assertEqual(sorted(real - stub), [], "替身缺真配置的字段")
        # `ensure_dirs` 是 Config 的方法（替身里给个 MagicMock 顶替），不是字段——只放它一个进来。
        self.assertEqual(sorted(stub - real), ["ensure_dirs"],
                         "除这个方法替身之外，替身不该多出真配置没有的键")

    def test_overrides_win(self):
        cfg = crawler_cfg(max_pages_per_shop=7, raw_page_dir="/tmp/x")

        self.assertEqual(cfg.max_pages_per_shop, 7)
        self.assertEqual(cfg.raw_page_dir, "/tmp/x")
        self.assertEqual(cfg.batch_size, 1, "没覆盖的键照旧")

    def test_a_typo_in_an_override_is_loud(self):
        """打错键名不许静默失效——否则「我明明设了」会变成一次假绿。"""
        with self.assertRaisesRegex(TypeError, "max_page_per_shop"):
            crawler_cfg(max_page_per_shop=3)


class FrozenClockTests(unittest.TestCase):
    """frozen_clock 的护栏：钉住采集链读到的钟，用例不再看真实钟（2026-09-20 深夜假红事件）。"""

    def test_it_pins_the_clock_the_crawl_path_reads(self):
        """采集路径的钟在各模块自己的命名空间里（`from .db import utcnow` 会留副本），
        漏钉一个，23:55 之后跑套件还是会把「跨天」判给无辜的用例。"""
        from bestseller_monitor import click_listing, detail, pipeline

        with frozen_clock():
            self.assertEqual(click_listing.utcnow(), FROZEN_NOW)
            self.assertEqual(detail.utcnow(), FROZEN_NOW)
            self.assertEqual(pipeline.utcnow(), FROZEN_NOW)
            self.assertEqual(rounds.utcnow(), FROZEN_NOW)
            self.assertEqual(cst_date(), FROZEN_DATE, "夹具建轮的「今天」也要同源")

    def test_a_round_opened_under_the_frozen_clock_keeps_working(self):
        """夹具的日期与判停读到的「现在」出自同一时刻：轮次照常可干活，不判停。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = connect(Path(tmp.name) / "clock.db")
        self.addCleanup(conn.close)
        db = Database(conn)

        with frozen_clock():
            rid = new_round(db, "A01")
            self.assertEqual(
                conn.execute("SELECT run_date FROM rounds WHERE id=?", (rid,)).fetchone()[0],
                FROZEN_DATE)
            rounds.ensure_workable(db, rid, utcnow())

    def test_it_gives_the_real_clock_back_afterwards(self):
        """出上下文要还原——冻结漏给后面的用例，就成了一次跨用例的假绿。"""
        from bestseller_monitor import click_listing

        real = click_listing.utcnow
        with frozen_clock():
            self.assertIsNot(click_listing.utcnow, real)
        self.assertIs(click_listing.utcnow, real)


if __name__ == "__main__":
    unittest.main()
