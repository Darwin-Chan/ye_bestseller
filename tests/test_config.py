import tempfile
import unittest
from pathlib import Path

from bestseller_monitor.config import Config, load_shops


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

    def test_export_dependency_removed_from_requirements(self):
        """导出库不再属于运行依赖，其余依赖保持不变。"""
        lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        names = {
            line.split(">=")[0].split("==")[0].strip().lower()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        }

        self.assertNotIn("openpyxl", names)
        self.assertIn("playwright", names)
        self.assertIn("drissionpage", names)


if __name__ == "__main__":
    unittest.main()
