"""Trial history cannot enter formal analysis or actual local Git/image outputs."""
import contextlib
import gzip
import sqlite3
import unittest
from unittest.mock import patch

from bestseller_monitor import export
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.collection_context import for_collection, prepare_trial_storage
from bestseller_monitor.config import Config, ROLE_MERGE_ONLY
from bestseller_monitor.db import connect, Database
from bestseller_monitor.image_store import image_key
from helpers import submit_offer, product_picture
from test_run_cli import MINIMAL_CONFIG
from test_exchange import ConsoleWorld, DAYS, WEEK


class LocalTrialExchangeTests(unittest.TestCase):
    def seed_trial(self, world):
        """在本机落一份试采数据，供下方两条「正式侧看不见试采」的断言使用。

        试采收窄为纯汇总机之后（ADR-0049 修订段），采集机已经进不了试采入口，
        所以这里的试采现场按库层口径直接构造：用例要的是「磁盘上存在试采库与试采
        资产」这一个事实，好让导出与汇总继续证明它们不会把试采资产带出去。
        """
        root = world.box.tmp
        config = root / "config/config.toml"
        config.parent.mkdir(exist_ok=True)
        config.write_text(MINIMAL_CONFIG + f'role="{world.role}"\n', encoding="utf-8")
        cfg = Config.from_file(config, root=root).replace(
            db_file=world.db_path, exchange_root=world.root,
            analysis_config=world.analysis_path)
        cfg.shop_csv.write_text("shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                                "TRIAL,测试店,https://trial.example/,3,1,\n", encoding="utf-8")
        # 试采路径由 for_collection 一处算出（纯汇总机口径），不在测试里另抄一份布局。
        trial = for_collection(cfg.replace(role=ROLE_MERGE_ONLY), local_trial=True, config_file=config)
        prepare_trial_storage(trial)
        self.assertEqual(trial.db_file, for_collection(
            cfg.replace(role=ROLE_MERGE_ONLY), local_trial=True, config_file=config).db_file)
        with contextlib.closing(connect(trial.db_file)) as conn:
            db = Database(conn)
            for day, stock in [("2026-09-14", 9), ("2026-09-15", 8)]:
                submit_offer(db, "99001", day, stock, name="trial-only", shop_key="TRIAL", color="green")
        return trial

    def test_merge_only_still_merges_and_shares_formal_judgments_with_trial_history(self):
        world = ConsoleWorld(self, machine_id="m4", role=ROLE_MERGE_ONLY)
        world.plan(("A02", "m2", 3), ("A03", "m3", 3))
        world.publish("m2", [("A02", day) for day in DAYS])
        world.publish("m3", [("A03", day) for day in DAYS])
        world.judged("m4", "m2", "m3")
        world.analysis_config()
        world.judgments_local()
        world.judged_publish("m2", rows=(("formal-remote", world.contract),))
        trial = self.seed_trial(world)
        with contextlib.closing(connect(trial.db_file)) as conn:
            before = "\n".join(conn.iterdump())
        with patch("bestseller_monitor.matching.urlopen", side_effect=AssertionError("不得调用真模型")):
            outcome = world.run()
            service = AnalysisService(AnalysisConfig.from_file(world.analysis_path), running=lambda: False)
            analysis = service.start("2026-09-14", "2026-09-15")
        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        self.assertIsNotNone(world.judged_local_bytes("m4"))
        self.assertIsNotNone(world.judged_local_bytes("m2"))
        with contextlib.closing(sqlite3.connect(world.cache)) as cache:
            self.assertEqual(
                sorted(cache.execute("SELECT machine_id FROM judgments").fetchall()),
                [("m2",), ("m4",)], "正式线两边的判断都在（本机的 + 收进 m2 的）")
        self.assertEqual(world.conn.execute("SELECT DISTINCT shop_key FROM inventory ORDER BY shop_key").fetchall()[0][0], "A02")
        self.assertEqual(world.conn.execute("SELECT count(*) FROM inventory WHERE offer_id='99001'").fetchone()[0], 0)
        self.assertNotIn("TRIAL", {row["shop_key"] for row in service.coverage("2026-09-14")})
        self.assertEqual("sku", analysis["member_kind"])
        self.assertNotIn("99001", {row["offer_id"] for row in analysis["members"]})
        self.assertNotIn("TRIAL", {row["shop_key"] for row in analysis["members"]})
        self.assertTrue(analysis["members"])
        self.assertFalse((world.root / "raw-m4").exists())
        self.assertEqual(world.store.uploaded, {})
        with contextlib.closing(connect(trial.db_file)) as conn:
            self.assertEqual("\n".join(conn.iterdump()), before)

    def test_formal_export_actual_git_package_and_image_upload_exclude_trial_assets(self):
        world = ConsoleWorld(self)
        db = Database(world.conn)
        submit_offer(db, "11001", "2026-09-14", 10, name="formal", color="red")
        trial = self.seed_trial(world)
        result = export.export(world.cfg(), week=WEEK, store=world.store)
        self.assertTrue(result.published, result.failure)
        raw = world.published_bytes("raw-m1", result.package_rel)
        self.assertIsNotNone(raw)
        package = world.box.tmp / "published-check.db"
        package.write_bytes(gzip.decompress(raw))
        with contextlib.closing(sqlite3.connect(package)) as conn:
            self.assertEqual(conn.execute("SELECT DISTINCT offer_id FROM inventory").fetchall(), [("11001",)])
            self.assertEqual(conn.execute("SELECT DISTINCT offer_id FROM product_information_versions").fetchall(), [("11001",)])
        red = product_picture("red")
        green = product_picture("green")
        self.assertEqual(set(world.store.uploaded), {image_key(red["hash"], red["mime"])})
        self.assertNotIn(image_key(green["hash"], green["mime"]), world.store.uploaded)
        self.assertEqual(world.conn.execute("SELECT count(*) FROM inventory").fetchone()[0], 1)
        self.assertTrue(trial.db_file.exists())
