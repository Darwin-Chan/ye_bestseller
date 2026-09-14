"""测试夹具本身：采集配置替身要跟得上真配置（候选 06）。"""
import dataclasses
import unittest

from bestseller_monitor.config import Config
from bestseller_monitor.db import PARAMS_KEYS
from helpers import crawler_cfg


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


if __name__ == "__main__":
    unittest.main()
