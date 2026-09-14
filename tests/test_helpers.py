"""测试夹具本身：采集配置替身要跟得上真配置（候选 06）。"""
import unittest

from bestseller_monitor.db import PARAMS_KEYS
from helpers import crawler_cfg


class CrawlerCfgTests(unittest.TestCase):
    def test_the_stub_covers_every_key_a_round_records(self):
        """`record_params()` 会把 `PARAMS_KEYS` 逐键取出来记进 run_params：
        替身缺一个键，跑一整轮的那类用例就会在 `getattr` 上炸。"""
        missing = sorted(set(PARAMS_KEYS) - set(vars(crawler_cfg())))

        self.assertEqual(missing, [], f"crawler_cfg 缺这些键：{missing}")

    def test_overrides_win(self):
        cfg = crawler_cfg(max_pages_per_shop=7, raw_page_dir="/tmp/x")

        self.assertEqual(cfg.max_pages_per_shop, 7)
        self.assertEqual(cfg.raw_page_dir, "/tmp/x")
        self.assertEqual(cfg.batch_size, 1, "没覆盖的键照旧")


if __name__ == "__main__":
    unittest.main()
