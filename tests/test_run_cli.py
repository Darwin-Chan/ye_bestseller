"""命令行覆盖的接线：`--pages-per-shop` 必须留下「显式覆盖」标记（IS-35）。

只测 `run.apply_overrides()` 这一个 seam：参数怎么解析、怎么落到配置，都是外部
行为（命令行页数优先于 shops.csv 的 pages）；不碰浏览器与数据库。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run
from bestseller_monitor.config import Config, Shop, effective_pages_limit


MINIMAL_CONFIG = """
[run]
human_pause_minutes = 1
max_pages_per_shop = 30
max_detail_opportunities_per_round = 1000
max_attempts_per_page = 2
fail_rate_limit = 0.1
shuffle_within_shop = true

[human]
detail_delay_sec = [0.0, 0.0]
long_pause_interval = [1, 1]
long_pause_sec = [0.0, 0.0]
batch_size = 1
batch_rest_sec = [0.0, 0.0]
list_delay_sec = [0.0, 0.0]
action_delay_sec = [0.0, 0.0]
read_delay_sec = [0.0, 0.0]
retry_base_sec = 0.0
retry_jitter_sec = 0.0

[browser]
profile_dir = "profile"
user_data_path = "profile"
headless = false
slow_mo_ms = 0
timeout_ms = 1000

[paths]
shop_csv = "shops.csv"
db_file = "bestseller.db"
data_dir = "data"
logs_dir = "logs"
screenshot_dir = "screenshots"
raw_page_dir = "raw_pages"
"""

# shops.csv 里配了 pages 的店：它是命令行覆盖要压过去的那一层
SHOP_WITH_PAGES = Shop("A01", "店一", "https://a.1688.com/", pages=3)


class RunCliTests(unittest.TestCase):
    def _parsed(self, argv: list[str]):
        with patch.object(sys, "argv", ["run.py", *argv]):
            return run.parse_args()

    def _cfg(self, tmp: str) -> Config:
        path = Path(tmp) / "config.toml"
        path.write_text(MINIMAL_CONFIG, encoding="utf-8")
        return Config.from_file(path, root=Path(tmp))

    def test_pages_per_shop_flag_overrides_shop_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = run.apply_overrides(self._cfg(tmp), self._parsed(["--pages-per-shop", "1"]))

        self.assertEqual(cfg.max_pages_per_shop, 1, "运行时快照记有效上限")
        self.assertEqual(cfg.pages_per_shop_override, 1, "显式覆盖要单独留标记")
        self.assertEqual(effective_pages_limit(SHOP_WITH_PAGES, cfg), 1)

    def test_without_flag_shop_pages_still_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = run.apply_overrides(self._cfg(tmp), self._parsed([]))

        self.assertIsNone(cfg.pages_per_shop_override)
        self.assertEqual(cfg.max_pages_per_shop, 30)
        self.assertEqual(effective_pages_limit(SHOP_WITH_PAGES, cfg), 3)

    def test_other_flags_still_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = run.apply_overrides(
                self._cfg(tmp),
                self._parsed(["--max-detail", "20", "--no-shuffle"]),
            )

        self.assertEqual(cfg.max_detail_opportunities_per_round, 20)
        self.assertFalse(cfg.shuffle_within_shop)
        self.assertIsNone(cfg.pages_per_shop_override, "别的参数不该动页数覆盖")


if __name__ == "__main__":
    unittest.main()
