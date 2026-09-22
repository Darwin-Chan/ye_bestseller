import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bestseller_monitor.config import Config, Shop, effective_pages_limit, load_shops


ROOT = Path(__file__).resolve().parents[1]

# 机器身份一节（spec §10）：跑得动的最小配置也必须声明本机编号
MACHINE_BLOCK = """
[machine]
machine_id = "m1"
"""

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
""" + MACHINE_BLOCK


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
    def _write_raw(tmp: str, text: str) -> Path:
        """把一段完整配置文本放进临时目录当 config.toml（缺键/非法值的用例自己拼）。"""
        path = Path(tmp) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    @classmethod
    def _write_config(cls, tmp: str) -> Path:
        return cls._write_raw(tmp, MINIMAL_CONFIG)

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

            self.assertEqual(cfg.driver, "pw_cdp")

    def test_config_accepts_the_click_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_file(self._write_config_with_driver(tmp, "pw_cdp"), root=Path(tmp))

            self.assertEqual(cfg.driver, "pw_cdp")

    def test_pacing_and_deny_defaults_when_the_keys_are_absent(self):
        """主动停顿（ADR-0038）与 deny 阶梯（ADR-0037）：老机器的 config.toml 里没有这些键，
        缺省也要落到出货的策略上，而不是旧值。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_file(self._write_config(tmp), root=Path(tmp))

            self.assertEqual(cfg.pause_every_detail_visits, 180)
            self.assertEqual(cfg.pause_sec, (1500.0, 2100.0))
            self.assertEqual((cfg.deny_shop_limit, cfg.deny_round_limit), (3, 6))

    def test_pacing_keys_are_read_from_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = MINIMAL_CONFIG.replace(
                "[human]\n",
                "[human]\npause_every_detail_visits = 0\npause_sec = [10, 20]\n", 1)
            path = Path(tmp) / "config.toml"
            path.write_text(text, encoding="utf-8")

            cfg = Config.from_file(path, root=Path(tmp))

            self.assertEqual(cfg.pause_every_detail_visits, 0, "0 = 关闭这条规则")
            self.assertEqual(cfg.pause_sec, (10.0, 20.0))

    def test_negative_pause_every_detail_visits_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = MINIMAL_CONFIG.replace(
                "[human]\n", "[human]\npause_every_detail_visits = -1\n", 1)
            path = Path(tmp) / "config.toml"
            path.write_text(text, encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                Config.from_file(path, root=Path(tmp))

            self.assertIn("pause_every_detail_visits", str(ctx.exception),
                          "报错要点名是哪个键（写错了与故意关掉要分得开）")

    def test_the_superseded_pages_quota_key_is_rejected_by_name(self):
        """旧键按详情当量折算过来恰好等于新默认（6 页 = 180 次），但写着 0（故意关掉）或
        别的值就完全不同——静默替机器猜意图是最糟的错法，所以见到旧键就报错（ADR-0038）。"""
        with tempfile.TemporaryDirectory() as tmp:
            text = MINIMAL_CONFIG.replace(
                "[human]\n", "[human]\npause_every_pages = 6\n", 1)
            path = Path(tmp) / "config.toml"
            path.write_text(text, encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                Config.from_file(path, root=Path(tmp))

            message = str(ctx.exception)
            self.assertIn("pause_every_pages", message, "要点名旧键")
            self.assertIn("pause_every_detail_visits", message, "也要给出改写成什么")

    def test_shipped_example_config_passes_its_own_validation(self):
        """出厂的是示例配置（真配置每台自建、不进仓库，spec §10）：示例含全部键、必须被自己的
        校验接受——合法驱动值只在代码里声明一处（IS-23 审查发现）。"""
        cfg = Config.from_file(ROOT / "config" / "config.example.toml", root=ROOT)

        self.assertEqual(cfg.driver, "pw_cdp")
        self.assertTrue(cfg.machine_id, "示例必须带一个可用的本机编号占位")
        self.assertTrue(cfg.cos_bucket, "示例必须带一个图片桶占位，图片通道才配得起来")

    def test_example_config_copied_as_config_toml_loads(self):
        """「本机从示例复制即可开跑」：复制品在任意位置都能加载，路径全部落在复制后的根里。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "config"
            cfg_dir.mkdir()
            copied = cfg_dir / "config.toml"
            copied.write_text(
                (ROOT / "config" / "config.example.toml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            cfg = Config.from_file(copied)

            self.assertTrue(cfg.db_file.is_relative_to(Path(tmp).resolve()),
                            "示例里的路径必须是相对路径，复制到新机器才成立")

    def test_config_rejects_a_retired_driver(self):
        """配置里写着已下线的驱动就报错，不静默换一条路径跑（IS-23 / ADR-0010）。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                Config.from_file(self._write_config_with_driver(tmp, "drission"), root=Path(tmp))

            message = str(ctx.exception)
            self.assertIn("drission", message)
            self.assertIn("pw_cdp", message)

    def test_machine_identity_is_read_with_defaults(self):
        """机器身份（spec §10）：machine_id 必填；角色/交换区根/凭据档位缺省 = 采集机口径。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_file(self._write_config(tmp), root=Path(tmp))

            self.assertEqual(cfg.machine_id, "m1")
            self.assertEqual(cfg.role, "collector")
            self.assertEqual(cfg.exchange_root, (Path(tmp) / "exchange").resolve())
            self.assertEqual(cfg.cos_bucket, "", "没配桶：导出照发包，图片这半如实报缺")
            self.assertEqual(cfg.git_access, "readwrite")
            self.assertEqual(cfg.cos_access, "readwrite")

    def test_machine_block_declares_role_exchange_root_and_tiers(self):
        """纯汇总机口径在配置里全量声明（spec §11）：只记档位、不记密钥。"""
        block = """
[machine]
machine_id = "m4"
role = "merge_only"
exchange_root = "run/exchange"
cos_bucket = "bestseller-exchange-1250000000"
git_access = "read_only"
cos_access = "read_only"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_raw(tmp, MINIMAL_CONFIG.replace(MACHINE_BLOCK, block))
            cfg = Config.from_file(path, root=Path(tmp))

            self.assertEqual(cfg.machine_id, "m4")
            self.assertEqual(cfg.role, "merge_only")
            self.assertEqual(cfg.exchange_root, (Path(tmp) / "run" / "exchange").resolve())
            self.assertEqual(cfg.cos_bucket, "bestseller-exchange-1250000000")
            self.assertEqual(cfg.git_access, "read_only")
            self.assertEqual(cfg.cos_access, "read_only")

    def test_missing_machine_id_is_named_at_startup(self):
        """缺 machine_id 启动报错点名，并指向示例（spec §10）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_raw(tmp, MINIMAL_CONFIG.replace(MACHINE_BLOCK, ""))

            with self.assertRaises(ValueError) as ctx:
                Config.from_file(path, root=Path(tmp))

            message = str(ctx.exception)
            self.assertIn("machine_id", message)
            self.assertIn("config.example.toml", message)

    def test_illegal_role_is_named(self):
        """角色档位写错了启动报错点名两个合法值，不静默按采集机跑。"""
        text = MINIMAL_CONFIG.replace(
            'machine_id = "m1"', 'machine_id = "m1"\nrole = "summary"')
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_raw(tmp, text)

            with self.assertRaises(ValueError) as ctx:
                Config.from_file(path, root=Path(tmp))

            message = str(ctx.exception)
            self.assertIn("summary", message)
            self.assertIn("collector", message)
            self.assertIn("merge_only", message)

    def test_illegal_access_tier_is_named(self):
        """凭据档位只认 readwrite / read_only（spec §11），写别的启动报错点名。"""
        text = MINIMAL_CONFIG.replace(
            'machine_id = "m1"', 'machine_id = "m1"\ncos_access = "upload"')
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_raw(tmp, text)

            with self.assertRaises(ValueError) as ctx:
                Config.from_file(path, root=Path(tmp))

            message = str(ctx.exception)
            self.assertIn("cos_access", message)
            self.assertIn("upload", message)
            self.assertIn("read_only", message)

    def test_non_table_machine_section_is_named(self):
        """machine 写成单值（不是 [machine] 表）也要点名报错，而不是抛 AttributeError。"""
        text = 'machine = "m1"\n' + MINIMAL_CONFIG.replace(MACHINE_BLOCK, "")
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_raw(tmp, text)

            with self.assertRaises(ValueError) as ctx:
                Config.from_file(path, root=Path(tmp))

            self.assertIn("machine", str(ctx.exception))

    def test_missing_real_config_points_at_the_example(self):
        """真配置缺失（新机器还没自建）时报错直接指向示例文件（spec §10）。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError) as ctx:
                Config.from_file(Path(tmp) / "config.toml", root=Path(tmp))

            self.assertIn("config.example.toml", str(ctx.exception))

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

    def test_effective_pages_limit_plan_snapshot_layer(self):
        """页数四层优先（spec §6）：命令行覆盖 > 计划快照 > 店铺 pages > 全局默认。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_file(self._write_config(tmp), root=Path(tmp))  # 全局 3 页
        configured = Shop("A01", "店一", "https://a.1688.com/", pages=9)
        unconfigured = Shop("A02", "店二", "https://b.1688.com/")

        planned = cfg.replace(plan_pages={"A01": 23, "A02": 2})
        self.assertEqual(effective_pages_limit(configured, planned), 23, "计划快照压过店铺 pages")
        self.assertEqual(effective_pages_limit(unconfigured, planned), 2, "计划快照压过全局默认")

        # 刚从落库计划读到它的调用方可以显式传入；显式传入压过挂在配置上的那份
        self.assertEqual(
            effective_pages_limit(configured, planned, plan_pages={"A01": 5}), 5)

        overridden = planned.replace(pages_per_shop_override=1)
        self.assertEqual(effective_pages_limit(configured, overridden), 1, "命令行压过计划快照")

        # 计划快照里没有的店（计划外／周中新增）回落到下面两层
        partial = cfg.replace(plan_pages={"A02": 2})
        self.assertEqual(effective_pages_limit(configured, partial), 9)

        # 计划快照在就按快照算（计划是本周预算的权威）：说 0 页就是 0 页
        zeros = cfg.replace(plan_pages={"A01": 0, "A02": 0})
        self.assertEqual(effective_pages_limit(configured, zeros), 0, "快照说 0 就 0")
        self.assertEqual(effective_pages_limit(unconfigured, zeros), 0)
        # 店铺那层的 0/空仍是「没配」，回落到全局默认（沿用 IS-35 的老口径）
        self.assertEqual(effective_pages_limit(Shop("A03", "店三", "https://c.1688.com/",
                                                    pages=0), cfg), 3)


if __name__ == "__main__":
    unittest.main()
