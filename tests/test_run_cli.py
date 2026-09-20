"""命令行覆盖的接线：`--pages-per-shop` 必须留下「显式覆盖」标记（IS-35）。

`run.apply_overrides()` 这一个 seam 单独测：参数怎么解析、怎么落到配置（命令行页数
优先于 shops.csv 的 pages）；`main()` 那一层另测开轮前准备与落库计划的读法（票据 06）。
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run
from bestseller_monitor import plan_step, single_instance, weekly_plan
from bestseller_monitor.config import Config, Shop, effective_pages_limit
from bestseller_monitor.db import Database, connect
from helpers import store_weekly_plan


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
user_data_path = "profile"
timeout_ms = 1000

[paths]
shop_csv = "shops.csv"
db_file = "bestseller.db"
data_dir = "data"
logs_dir = "logs"
screenshot_dir = "screenshots"
raw_page_dir = "raw_pages"

[machine]
machine_id = "m1"
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


def seed_plan(tmp_path: Path, *assignments) -> None:
    """把本周计划落进本机计划表；assignments 是 (shop_key, machine_id, pages)。"""
    conn = connect(tmp_path / "bestseller.db")
    try:
        store_weekly_plan(Database(conn), weekly_plan.week_label(), *assignments)
    finally:
        conn.close()


class RunCliBusyTests(unittest.TestCase):
    """抢不到采集锁：命令行给可读原因 + 专用退出码，界面靠这个码提示原因（工单 02）。"""

    def test_busy_crawler_reports_a_readable_reason_and_its_own_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "config.toml"
            cfg_path.write_text(MINIMAL_CONFIG, encoding="utf-8")
            (tmp_path / "shops.csv").write_text(
                "shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                "A01,店一,https://a.1688.com/,3,1,\n",
                encoding="utf-8",
            )
            seed_plan(tmp_path, ("A01", "m1", 3))   # 先备好本周计划，才走到抢锁
            out = io.StringIO()
            busy = run.CrawlerAlreadyRunning("已有采集进程在运行：同一时刻只能跑一轮")

            with patch.object(run, "ROOT", tmp_path), \
                    patch.object(sys, "argv", ["run.py", "--config", str(cfg_path)]), \
                    patch.object(run.logging, "basicConfig"), \
                    patch.object(run.logging.handlers, "RotatingFileHandler"), \
                    patch.object(run, "run_round", side_effect=busy):
                with contextlib.redirect_stdout(out):
                    code = run.main()

        self.assertEqual(code, single_instance.CRAWLER_BUSY_EXIT_CODE)
        self.assertIn("已有采集进程在运行", out.getvalue(), "命令行要给出可读原因")


class RunCliPlanTests(unittest.TestCase):
    """开轮前准备与落库计划的接线（票据 06）：范围与页数都从本机计划表读。"""

    SHOPS_CSV = ("shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                 "A01,店一,https://a.1688.com/,3,1,\n"
                 "A02,店二,https://b.1688.com/,5,1,\n")

    def _env(self, tmp: str, *, shops: str | None = None, plan=()) -> Path:
        """建好配置、本机清单与（可选的）本周落库计划，返回配置文件路径。

        交换区根缺省指向 `<tmp>/exchange`，里面没有计划库克隆：拉不到计划库的降级
        在这里就是「本地已落库 → 用本地那份」，正好驱动票据 06 的读法。
        """
        tmp_path = Path(tmp)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(MINIMAL_CONFIG, encoding="utf-8")
        (tmp_path / "shops.csv").write_text(shops or self.SHOPS_CSV, encoding="utf-8")
        if plan:
            seed_plan(tmp_path, *plan)
        return cfg_path

    def _main(self, cfg_path: Path, *argv: str, round_error: Exception | None = None):
        out = io.StringIO()
        with patch.object(run, "ROOT", cfg_path.parent), \
                patch.object(sys, "argv", ["run.py", "--config", str(cfg_path), *argv]), \
                patch.object(run.logging, "basicConfig"), \
                patch.object(run.logging.handlers, "RotatingFileHandler"), \
                patch.object(run, "run_round", side_effect=round_error) as round_call:
            with contextlib.redirect_stdout(out):
                code = run.main()
        return code, out.getvalue(), round_call

    def test_cli_scopes_the_round_to_the_plan_and_feeds_it_the_page_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = self._env(tmp, plan=(("A01", "m1", 23), ("A02", "m2", 5)))

            code, out, round_call = self._main(cfg_path)

        self.assertEqual(code, 0)
        cfg, shops = round_call.call_args.args
        self.assertEqual([shop.key for shop in shops], ["A01"],
                         "A02 本周计划归 m2：裸跑的范围取计划里归本机的店")
        self.assertEqual(effective_pages_limit(shops[0], cfg), 23,
                         "计划快照（23）压过 shops.csv 的 pages（3）")
        self.assertEqual(cfg.plan_pages, {"A01": 23, "A02": 5}, "整周快照都挂上：越权补采也看本周预算")
        self.assertIsNone(cfg.pages_per_shop_override, "命令行没发 --pages-per-shop 就不留标记")
        self.assertIn("未能确认最新", out, "用本地那份继续的告警要在命令行看得见")

    def test_cli_still_takes_an_explicit_limit_shops_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = self._env(tmp, plan=(("A01", "m1", 23), ("A02", "m2", 5)))

            code, out, round_call = self._main(cfg_path, "--limit-shops", "A02")

        self.assertEqual(code, 0)
        cfg, shops = round_call.call_args.args
        self.assertEqual([shop.key for shop in shops], ["A02"],
                         "显式点名别机的店 = 越权补采：放行（处置与留痕见票据 07）")
        self.assertEqual(effective_pages_limit(shops[0], cfg), 5)

    def test_pages_per_shop_flag_still_beats_the_plan_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = self._env(tmp, plan=(("A01", "m1", 23),))

            code, out, round_call = self._main(cfg_path, "--pages-per-shop", "1")

        self.assertEqual(code, 0)
        cfg, shops = round_call.call_args.args
        self.assertEqual(effective_pages_limit(shops[0], cfg), 1)

    def test_cli_refuses_to_start_without_a_usable_plan(self):
        """拉不到计划库、本地也没有本周计划：默认拒绝开轮，给专用退出码（逃生口见票据 07）。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = self._env(tmp)

            code, out, round_call = self._main(cfg_path)

        self.assertEqual(code, plan_step.PLAN_REFUSED_EXIT_CODE)
        round_call.assert_not_called()
        self.assertIn("拒绝开轮", out)
        self.assertIn("clone", out, "要说清是计划库没建起来还是别的")

    def test_cli_reports_idle_as_a_legal_state(self):
        """本机本周没店（空手）：说清楚，不当错误，不开轮。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = self._env(tmp, plan=(("A01", "m2", 23), ("A02", "m3", 5)))

            code, out, round_call = self._main(cfg_path)

        self.assertEqual(code, 0)
        round_call.assert_not_called()
        self.assertIn("空手", out)

    def test_cli_names_plan_shops_missing_from_the_local_list(self):
        """计划里归本机、但本机清单里没有那个编号（周中删行）：点名，不说成「没有有效店铺」。"""
        with tempfile.TemporaryDirectory() as tmp:
            only_other = ("shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                          "B01,别家,https://b.1688.com/,3,1,\n")
            cfg_path = self._env(tmp, shops=only_other, plan=(("A01", "m1", 23),))

            code, out, round_call = self._main(cfg_path)

        self.assertEqual(code, 2)
        round_call.assert_not_called()
        self.assertIn("A01", out)
        self.assertIn("清单", out)

    def test_cli_refuses_when_the_lock_is_taken_even_with_a_plan(self):
        """准备过了也不越权：抢不到锁仍走「已有采集在跑」那条路（保持工单 02 的口径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = self._env(tmp, plan=(("A01", "m1", 23),))

            code, out, _ = self._main(
                cfg_path, round_error=run.CrawlerAlreadyRunning("占用中"))

        self.assertEqual(code, single_instance.CRAWLER_BUSY_EXIT_CODE)


if __name__ == "__main__":
    unittest.main()
