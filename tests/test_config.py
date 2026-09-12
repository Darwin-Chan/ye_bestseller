import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bestseller_monitor.config import Config, Shop, effective_pages_limit, load_shops


ROOT = Path(__file__).resolve().parents[1]

# 最小可用配置：路径写成相对值，由 root 参数解析，避免 Windows 反斜杠的 TOML 转义问题
MINIMAL_CONFIG = """
[run]
human_pause_minutes = 1
max_pages_per_shop = 3
max_detail_opportunities_per_round = 10
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
"""


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

    @staticmethod
    def _write_config(tmp: str) -> Path:
        path = Path(tmp) / "config.toml"
        path.write_text(MINIMAL_CONFIG, encoding="utf-8")
        return path

    @staticmethod
    def _write_config_with_driver(tmp: str, value: str) -> Path:
        """在同一份最小配置里加一行 driver，其余键保持不动。"""
        text = MINIMAL_CONFIG.replace("[browser]\n", f'[browser]\ndriver = "{value}"\n', 1)
        path = Path(tmp) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_config_without_driver_key_uses_the_click_driver(self):
        """driver 缺键不说谎，按唯一的驱动跑（IS-23 / ADR-0010）。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_file(self._write_config(tmp), root=Path(tmp))

            self.assertEqual(cfg.driver, "pw_cpd")

    def test_config_accepts_the_click_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_file(self._write_config_with_driver(tmp, "pw_cpd"), root=Path(tmp))

            self.assertEqual(cfg.driver, "pw_cpd")

    def test_config_rejects_a_retired_driver(self):
        """配置里写着已下线的驱动就报错，不静默换一条路径跑（IS-23 / ADR-0010）。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                Config.from_file(self._write_config_with_driver(tmp, "drission"), root=Path(tmp))

            message = str(ctx.exception)
            self.assertIn("drission", message)
            self.assertIn("pw_cpd", message)

    def test_config_without_export_dir_still_loads(self):
        """同步导出链已删除，配置里不再有导出目录这个概念。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_file(self._write_config(tmp), root=Path(tmp))

            self.assertFalse(hasattr(cfg, "output_dir"), "导出目录已随同步导出链删除")
            self.assertEqual(cfg.data_dir, (Path(tmp) / "data").resolve())
            self.assertEqual(cfg.logs_dir, (Path(tmp) / "logs").resolve())

    def test_ensure_dirs_creates_runtime_dirs_only(self):
        """启动只准备运行期目录，不再额外创建导出目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_file(self._write_config(tmp), root=Path(tmp))

            cfg.ensure_dirs()

            created = {p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*") if p.is_dir()}
            self.assertEqual(
                created,
                {"data", "logs", "screenshots", "raw_pages", "profile"},
            )

    def test_requirements_list_the_engines_we_actually_use(self):
        """导出库和已下线的驱动都不在依赖里；测试用的 CSS 引擎显式声明。"""
        lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        names = {
            line.split(">=")[0].split("==")[0].strip().lower()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        }

        self.assertNotIn("openpyxl", names)
        self.assertNotIn("drissionpage", names, "DrissionPage 那条路径已删除（IS-23 / ADR-0010）")
        self.assertIn("playwright", names)
        self.assertIn("pywebview", names)
        self.assertIn("lxml", names)
        self.assertIn("cssselect", names)

    def test_effective_pages_limit_priority(self):
        """翻页上限优先级：命令行覆盖 > 店铺配置 > 全局默认（IS-35）。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_file(self._write_config(tmp), root=Path(tmp))  # 全局 3 页
        configured = Shop("A01", "店一", "https://a.1688.com/", pages=9)
        unconfigured = Shop("A02", "店二", "https://b.1688.com/")

        self.assertIsNone(cfg.pages_per_shop_override, "没有命令行覆盖时不留标记")
        self.assertEqual(effective_pages_limit(configured, cfg), 9, "店铺配置压过全局默认")
        self.assertEqual(effective_pages_limit(unconfigured, cfg), 3, "没配 pages 落回全局默认")

        overridden = cfg.replace(pages_per_shop_override=1)
        self.assertEqual(effective_pages_limit(configured, overridden), 1, "命令行压过店铺配置")
        self.assertEqual(effective_pages_limit(unconfigured, overridden), 1)
        self.assertEqual(effective_pages_limit(None, overridden), 1)

        # 测试与旧调用方常传 SimpleNamespace：没有该字段就按「没有命令行覆盖」处理
        self.assertEqual(
            effective_pages_limit(configured, SimpleNamespace(max_pages_per_shop=3)), 9,
        )


if __name__ == "__main__":
    unittest.main()
