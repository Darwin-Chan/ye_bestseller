"""Local trial behavior through the CLI and real collection orchestration."""
import contextlib
import io
import logging
import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run
from bestseller_monitor import browser_pw, guard, single_instance, click_listing, product_images, rounds, stop_request
from bestseller_monitor.config import Config
from bestseller_monitor.db import connect, Database, cst_date
from bestseller_monitor.collection_context import for_collection, TrialRoleRefused, TrialUnavailable
from helpers import isolated_locks, FakeCard, ScriptedListing, product_picture, submit_offer
from test_run_cli import MINIMAL_CONFIG
from frozen_clock import frozen_clock


class LocalTrialCliTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(frozen_clock())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text(MINIMAL_CONFIG + 'role = "merge_only"\n', encoding="utf-8")
        self.shops = self.root / "shops.csv"
        self.shops.write_text("shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                              "A01,店一,https://a.example/,3,1,\n"
                              "A02,店二,https://b.example/,4,1,\n", encoding="utf-8")
        self.sessions = []

    def browser_pause(self, cfg, **kwargs):
        self.sessions.append(cfg)
        raise guard.RoundPauseRequired("测试浏览器等待人工介入")

    def cli(self, *argv, browser=None):
        out = io.StringIO()
        with patch.object(run, "ROOT", self.root), \
                patch.object(sys, "argv", ["run.py", "--config", str(self.config), *argv]), \
                isolated_locks(), \
                patch.object(browser_pw, "open_session", side_effect=browser or self.browser_pause), \
                contextlib.redirect_stdout(out):
            code = run.main()
        return code, out.getvalue()

    def test_explicit_trial_uses_real_pipeline_without_formal_db_or_role_change(self):
        before = self.config.read_bytes()
        code, out = self.cli("--local-trial", "--limit-shops", "A01")
        self.assertEqual(code, 0)
        self.assertIn("本机试采", out)
        self.assertIn("不共享", out)
        self.assertFalse((self.root / "bestseller.db").exists())
        self.assertFalse((self.root / "exchange").exists())
        self.assertEqual(self.config.read_bytes(), before)
        cfg = self.sessions[0]
        self.assertEqual((cfg.machine_id, cfg.role), ("m1", "merge_only"))
        self.assertEqual(cfg.user_data_path, self.root / "profile")
        conn = connect(self.root / "local-trial/data/bestseller.db")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT count(*) FROM rounds").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT shop_key FROM shop_rounds").fetchone()[0], "A01")
        self.assertIsNone(conn.execute("SELECT terminal_reason FROM rounds").fetchone()[0])
        self.assertEqual(conn.execute("SELECT count(*) FROM plan_deviations").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM weekly_plan").fetchone()[0], 0)
        self.assertTrue((self.root / "local-trial/logs/run.log").exists())

    def test_trial_is_only_available_on_a_merge_only_machine(self):
        """采集机没有本机试采：库层先拒绝，命令行说清原因，且不碰任何存储。"""
        collector = self.root / "collector.toml"
        collector.write_text(MINIMAL_CONFIG, encoding="utf-8")  # 省略 role 即采集机
        formal = Config.from_file(collector, root=self.root)
        self.assertEqual(formal.role, "collector")
        with self.assertRaises(TrialRoleRefused):
            for_collection(formal, local_trial=True)
        self.assertIs(for_collection(formal, local_trial=False), formal)
        out = io.StringIO()
        with patch.object(run, "ROOT", self.root), \
                patch.object(sys, "argv", ["run.py", "--config", str(collector), "--local-trial"]), \
                isolated_locks(), \
                contextlib.redirect_stdout(out):
            code = run.main()
        text = out.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("只在纯汇总机提供", text)
        self.assertIn("采集机", text)
        self.assertNotIn("请检查本机清单与隔离空间", text)
        self.assertEqual(self.sessions, [])
        self.assertFalse((self.root / "local-trial").exists())
        self.assertFalse((self.root / "bestseller.db").exists())

    def test_default_merge_only_and_ignore_plan_still_refuse_without_storage(self):
        for args in [(), ("--ignore-plan",)]:
            code, _ = self.cli(*args)
            self.assertEqual(code, 2)
        self.assertEqual(self.sessions, [])
        self.assertFalse((self.root / "local-trial").exists())
        self.assertFalse((self.root / "bestseller.db").exists())

    def test_trial_refuses_overlap_with_formal_targets_before_browser(self):
        formal = Config.from_file(self.config, root=self.root)
        home = self.root / "local-trial"
        for field, path in [("db_file", home / "data/bestseller.db"),
                            ("logs_dir", home / "logs"),
                            ("data_dir", home),
                            ("exchange_root", home / "exchange"),
                            ("user_data_path", home / "profile"),
                            ("shop_csv", home / "shops.csv")]:
            with self.subTest(field=field), self.assertRaises(TrialUnavailable):
                for_collection(formal.replace(**{field: path}), local_trial=True)
        self.assertFalse(home.exists())

    def test_trial_refuses_nested_link_or_hardlink_to_formal_data(self):
        formal = Config.from_file(self.config, root=self.root)
        home = self.root / "local-trial"
        (home / "data").mkdir(parents=True)
        formal.db_file.write_bytes(b"formal sentinel")
        (home / "data/bestseller.db").hardlink_to(formal.db_file)
        with self.assertRaises(TrialUnavailable):
            for_collection(formal, local_trial=True)
        self.assertEqual(formal.db_file.read_bytes(), b"formal sentinel")
        (home / "data/bestseller.db").unlink()
        # Directory symlinks need privilege on some Windows hosts. Hardlink case is mandatory.
        try:
            (home / "logs").symlink_to(self.root / "logs", target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(TrialUnavailable):
            for_collection(formal, local_trial=True)

    def test_trial_refuses_link_when_soft_predicates_are_blind(self):
        """m2 现象回归（2026-10-12）：主机让 is_symlink/is_junction/resolve 对真实链接
        静默失效（对链接的 lstat/readlink 报 WinError 1921，被吞成 False/原样路径）时，
        守卫仍要凭原始事实拒绝——「查不出来」不是「没问题」。"""
        formal = Config.from_file(self.config, root=self.root)
        home = self.root / "local-trial"
        (home / "data").mkdir(parents=True)
        try:
            (home / "logs").symlink_to(self.root / "logs", target_is_directory=True)
        except OSError:
            return  # 目录符号链接需要特权，与上一用例同口径
        with patch.object(Path, "is_symlink", return_value=False), \
                patch.object(Path, "is_junction", return_value=False), \
                patch.object(Path, "resolve", new=lambda self, *args, **kwargs: self):
            with self.assertRaises(TrialUnavailable):
                for_collection(formal, local_trial=True)

    def test_trial_refuses_entry_that_cannot_be_verified(self):
        """同一课的另一半：连 lstat 都问不出来的条目（m2 上即此形态）按拒绝处理，
        错误信息点名路径——不把「核实不了」当「安全」。"""
        formal = Config.from_file(self.config, root=self.root)
        home = self.root / "local-trial"
        (home / "data").mkdir(parents=True)
        try:
            link = home / "logs"
            link.symlink_to(self.root / "logs", target_is_directory=True)
        except OSError:
            return
        real_lstat = os.lstat

        def blind_lstat(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(link):
                raise OSError(22, "模拟 m2：ERROR_CANT_RESOLVE_FILENAME", os.fspath(path), 1921)
            return real_lstat(path, *args, **kwargs)

        with patch("os.lstat", new=blind_lstat):
            with self.assertRaises(TrialUnavailable) as caught:
                for_collection(formal, local_trial=True)
        self.assertIn(os.fspath(link), str(caught.exception))

    def test_trial_refuses_analysis_database_inside_trial_namespace(self):
        (self.root / "analysis.toml").write_text(
            '[analysis]\ndatabase="local-trial/data/bestseller.db"\n', encoding="utf-8")
        code, out = self.cli("--local-trial")
        self.assertEqual(code, 2)
        self.assertIn("不会回退", out)
        self.assertEqual(self.sessions, [])
        self.assertFalse((self.root / "local-trial").exists())

    def test_unavailable_space_and_invalid_local_selection_are_clear_refusals(self):
        for keys in ["", "UNKNOWN", "A01,UNKNOWN"]:
            code, out = self.cli("--local-trial", "--limit-shops", keys)
            self.assertEqual(code, 2)
            self.assertIn("不会回退", out)
        self.shops.unlink()
        code, _ = self.cli("--local-trial")
        self.assertEqual(code, 2)
        self.assertEqual(self.sessions, [])
        self.assertFalse((self.root / "bestseller.db").exists())

    def test_trial_context_cannot_be_sent_to_exchange_or_export(self):
        from bestseller_monitor import exchange, export
        formal = Config.from_file(self.config, root=self.root)
        trial = for_collection(formal, local_trial=True)
        for action in [exchange.run_once, export.export]:
            with self.subTest(action=action.__name__), self.assertRaises(ValueError):
                action(trial)
        self.assertFalse(trial.db_file.exists())
        self.assertFalse(formal.exchange_root.exists())

    def trial_db(self):
        conn = connect(self.root / "local-trial/data/bestseller.db")
        self.addCleanup(conn.close)
        return Database(conn)

    def scripted_collection(self, *args, cards=None, pages=None):
        cards = cards or [FakeCard("trial-only", offer_id="11", content_values=[
            '<meta property="og:image" content="https://img.example/trial.png">'
            '<script>{"skuInfoMap":{"A":{"skuId":"A","name":"默认",'
            '"price":1,"canBookCount":3}}}</script>'])]
        self.last_cards = cards
        self.listings = []
        def listing(page, shop, cfg, human):
            adapter = ScriptedListing(pages if pages is not None else [cards])
            self.listings.append(adapter)
            return adapter
        def browser(cfg, **kwargs):
            self.sessions.append(cfg)
            return None, None, object(), None
        # 进程边界两道门都关上：run 挡 coscli（cos_store 走 run），Popen 挡 git 通道
        # （票 01 起 git 走 Popen——只堵 run 会让 git 悄悄漏过去）。
        with patch.object(browser_pw, "close_session"), \
                patch.object(click_listing, "PlaywrightListing", side_effect=listing), \
                patch.object(product_images, "urlopen", side_effect=lambda *a, **k: io.BytesIO(product_picture("green")["content"])), \
                patch("bestseller_monitor.git_channel.subprocess.run", side_effect=AssertionError("试采不得运行Git/COS")) as external_process, \
                patch("bestseller_monitor.git_channel.subprocess.Popen", side_effect=AssertionError("试采不得运行Git/COS")) as external_spawn, \
                patch("bestseller_monitor.matching.urlopen", side_effect=AssertionError("试采不得调用模型")) as model_request:
            result = self.cli("--local-trial", *args, browser=browser)
            external_process.assert_not_called()
            external_spawn.assert_not_called()
            model_request.assert_not_called()
            return result

    def test_real_inventory_and_image_capture_ignores_formal_success_then_dedupes_trial(self):
        formal = Database(connect(self.root / "bestseller.db"))
        self.addCleanup(formal.conn.close)
        submit_offer(formal, "11", cst_date(), 99, name="formal", color="red")
        formal_before = "\n".join(formal.conn.iterdump())
        code, out = self.scripted_collection("--limit-shops", "A01", "--no-shuffle")
        self.assertEqual(code, 0, out)
        trial = self.trial_db()
        self.assertEqual(trial.conn.execute("SELECT stock FROM inventory").fetchone()[0], 3)
        self.assertEqual(trial.conn.execute("SELECT count(*) FROM product_image_assets").fetchone()[0], 1)
        self.assertEqual(trial.conn.execute("SELECT count(*) FROM sku_image_versions").fetchone()[0], 1)
        self.assertEqual(trial.conn.execute("SELECT terminal_reason FROM rounds").fetchone()[0], "COMPLETED")
        self.assertGreater(trial.conn.execute("SELECT count(*) FROM event_log").fetchone()[0], 0)
        code, out = self.scripted_collection("--limit-shops", "A01")
        self.assertEqual(code, 0, out)
        self.assertEqual(trial.conn.execute("SELECT count(*) FROM rounds").fetchone()[0], 2)
        self.assertEqual(trial.conn.execute("SELECT count(*) FROM inventory").fetchone()[0], 1)
        self.assertEqual(trial.conn.execute("SELECT count(*) FROM product_information_versions").fetchone()[0], 1)
        self.assertEqual(trial.conn.execute("SELECT count(*) FROM snapshots WHERE page_status='跳过'").fetchone()[0], 1)
        self.assertEqual("\n".join(formal.conn.iterdump()), formal_before)
        self.assertFalse((self.root / "exchange").exists())

    def test_same_scope_resume_changed_scope_refusal_and_manual_abandon(self):
        self.assertEqual(self.cli("--local-trial", "--limit-shops", "A01")[0], 0)
        self.assertEqual(self.cli("--local-trial")[0], 0)
        db = self.trial_db()
        self.assertEqual(db.conn.execute("SELECT count(*) FROM rounds").fetchone()[0], 1)
        code, out = self.cli("--local-trial", "--limit-shops", "A02")
        self.assertEqual(code, 3, out)
        self.assertEqual(len(self.sessions), 2)
        current = rounds.active_round(db, cst_date())
        rounds.finish(db, current, rounds.TerminalReason.ABANDONED)
        self.assertEqual(self.cli("--local-trial", "--limit-shops", "A02")[0], 0)
        self.assertEqual(db.conn.execute("SELECT count(*) FROM rounds").fetchone()[0], 2)
        self.assertEqual(rounds.active_round(db).shop_keys, ("A02",))

    def test_bare_resume_preserves_round_after_all_local_shops_disabled(self):
        self.assertEqual(self.cli("--local-trial", "--limit-shops", "A01")[0], 0)
        db = self.trial_db()
        original = rounds.active_round(db).id
        self.shops.write_text(self.shops.read_text(encoding="utf-8").replace(",1,", ",0,"), encoding="utf-8")
        code, out = self.cli("--local-trial")
        self.assertEqual(code, 0, out)
        self.assertEqual(rounds.active_round(db).id, original)
        self.assertEqual(rounds.active_round(db).shop_keys, ("A01",))
        self.assertEqual(len(self.sessions), 2)
        rounds.finish(db, rounds.active_round(db), rounds.TerminalReason.ABANDONED)
        self.assertEqual(self.cli("--local-trial")[0], 2)

    def test_bare_resume_preserves_round_after_local_shop_rows_removed(self):
        self.assertEqual(self.cli("--local-trial", "--limit-shops", "A01")[0], 0)
        db = self.trial_db()
        original = rounds.active_round(db).id
        self.shops.write_text("shop_key,shop_name,shop_url,pages,active,offer_list_url\n", encoding="utf-8")
        code, out = self.cli("--local-trial")
        self.assertEqual(code, 0, out)
        self.assertEqual(rounds.active_round(db).id, original)
        self.assertEqual(rounds.active_round(db).shop_keys, ("A01",))
        self.assertEqual(len(self.sessions), 2)
        rounds.finish(db, rounds.active_round(db), rounds.TerminalReason.ABANDONED)
        self.assertEqual(self.cli("--local-trial")[0], 2)

    def test_cross_day_supersedes_trial_only_and_preserves_formal_round(self):
        self.cli("--local-trial", "--limit-shops", "A01")
        db = self.trial_db()
        yesterday = (dt.date.fromisoformat(cst_date()) - dt.timedelta(days=1)).isoformat()
        db.conn.execute("UPDATE rounds SET run_date=?", (yesterday,))
        db.conn.commit()
        code, out = self.cli("--local-trial", "--limit-shops", "A02")
        self.assertEqual(code, 0, out)
        self.assertEqual(db.conn.execute("SELECT terminal_reason FROM rounds WHERE id=1").fetchone()[0], "DAY_BOUNDARY")
        self.assertEqual(rounds.active_round(db).shop_keys, ("A02",))
        self.assertFalse((self.root / "bestseller.db").exists())

    def test_pages_budget_profile_and_credentials_are_retained_without_plan_layer(self):
        code, _ = self.cli("--local-trial", "--pages-per-shop", "2", "--max-detail", "1", "--ignore-plan")
        self.assertEqual(code, 0)
        cfg = self.sessions[0]
        self.assertEqual((cfg.pages_per_shop_override, cfg.max_detail_opportunities_per_round), (2, 1))
        self.assertIsNone(cfg.plan_pages)
        self.assertEqual((cfg.machine_id, cfg.role, cfg.user_data_path), ("m1", "merge_only", self.root / "profile"))
        self.assertEqual(cfg.git_access, Config.from_file(self.config, root=self.root).git_access)

    def test_same_machine_mutex_rejects_trial_without_opening_browser_or_round(self):
        with isolated_locks():
            lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
            self.assertIsNotNone(lock)
            try:
                # cli's helper normally isolates locks; share this test's existing lock instead.
                with patch(__name__ + ".isolated_locks", contextlib.nullcontext):
                    code, out = self.cli("--local-trial")
            finally:
                lock.release()
        self.assertEqual(code, single_instance.CRAWLER_BUSY_EXIT_CODE, out)
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.trial_db().conn.execute("SELECT count(*) FROM rounds").fetchone()[0], 0)

    def test_stop_request_and_crawler_identity_are_trial_only(self):
        def stop(cfg, **kwargs):
            with contextlib.closing(connect(cfg.db_file)) as conn:
                db = Database(conn)
                identity = db.crawler_process()
                self.assertEqual(identity["pid"], os.getpid())
                self.assertIn("--local-trial", identity["note"])
                db.request_stop(round_id=identity["round_id"], kind="pause", target_pid=identity["pid"], target_started_at=identity["started_at"])
                stop_request.consume(db)
        code, out = self.cli("--local-trial", browser=stop)
        self.assertEqual(code, 0, out)
        db = self.trial_db()
        self.assertIsNone(db.crawler_process())
        self.assertIsNone(db.stop_request())
        self.assertIsNotNone(rounds.active_round(db))
        self.assertFalse((self.root / "bestseller.db").exists())

    def test_repeated_cli_logging_never_writes_formal_handler(self):
        handler = logging.FileHandler(self.root / "formal.log", encoding="utf-8")
        logger = logging.getLogger()
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            self.cli("--local-trial")
            self.cli("--local-trial")
            self.assertIn(handler, logger.handlers)
            handler.flush()
            self.assertEqual((self.root / "formal.log").read_text(encoding="utf-8"), "")
            self.assertIn("续跑", (self.root / "local-trial/logs/run.log").read_text(encoding="utf-8"))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
            handler.close()

    def test_existing_non_directory_trial_root_refuses_without_fallback(self):
        (self.root / "local-trial").write_text("blocked", encoding="utf-8")
        code, out = self.cli("--local-trial")
        self.assertEqual(code, 2)
        self.assertIn("不会回退", out)
        self.assertFalse((self.root / "bestseller.db").exists())
        self.assertEqual(self.sessions, [])

    def test_disabled_or_empty_shop_list_cannot_create_a_round(self):
        for csv in ["shop_key,shop_name,shop_url,pages,active\nA01,停用,https://a.example,1,0\n",
                    "shop_key,shop_name,shop_url,pages,active\n"]:
            self.shops.write_text(csv, encoding="utf-8")
            code, out = self.cli("--local-trial", "--limit-shops", "A01")
            self.assertEqual(code, 2, out)
        self.assertEqual(self.sessions, [])

    def test_real_detail_budget_stops_trial_without_formal_effects(self):
        code, out = self.scripted_collection("--limit-shops", "A01", "--max-detail", "1", "--no-shuffle",
            cards=[FakeCard("one", offer_id="11"), FakeCard("two", offer_id="22")])
        self.assertEqual(code, 0, out)
        db = self.trial_db()
        self.assertEqual(db.conn.execute("SELECT terminal_reason FROM rounds").fetchone()[0], "DETAIL_BUDGET_EXHAUSTED")
        self.assertEqual(db.conn.execute("SELECT count(*) FROM inventory").fetchone()[0], 1)
        self.assertFalse((self.root / "bestseller.db").exists())

    def test_parse_failure_preserves_raw_page_in_trial_space(self):
        html = '<script>{"skuInfoMap":{"A":{"skuId":"A","name":"默认","price":1}}}</script>'
        card = FakeCard("broken", offer_id="11", content_values=[html, html])
        code, out = self.scripted_collection("--limit-shops", "A01", "--max-detail", "1", "--no-shuffle", cards=[card])
        self.assertEqual(code, 0, out)
        archived = list((self.root / "local-trial/data/raw_pages").rglob("*.html"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(encoding="utf-8"), html)
        self.assertTrue((self.root / "local-trial/data/screenshots").is_dir())
        self.assertFalse((self.root / "raw_pages").exists())
        self.assertFalse((self.root / "screenshots").exists())
        self.assertEqual(self.trial_db().conn.execute("SELECT count(*) FROM inventory").fetchone()[0], 0)

    def test_trial_does_not_modify_formal_analysis_or_judgment_stores(self):
        cache = self.root / "matching.sqlite"
        store = self.root / "drafts.sqlite"
        cache.write_bytes(b"formal matching sentinel")
        store.write_bytes(b"formal judgment sentinel")
        (self.root / "analysis.toml").write_text(
            '[analysis]\ndatabase="bestseller.db"\nstore="drafts.sqlite"\n'
            '[matching]\ncache="matching.sqlite"\n', encoding="utf-8")
        before = (cache.read_bytes(), store.read_bytes())
        code, out = self.scripted_collection("--limit-shops", "A01")
        self.assertEqual(code, 0, out)
        self.assertEqual((cache.read_bytes(), store.read_bytes()), before)
        self.assertFalse((self.root / "bestseller.db").exists())

    def test_analysis_judgment_and_report_paths_are_also_protected(self):
        for section in ['store="local-trial/drafts.sqlite"\n',
                        'output="local-trial/reports"\n',
                        '[matching]\ncache="local-trial/matching.sqlite"\n']:
            (self.root / "analysis.toml").write_text(
                '[analysis]\ndatabase="bestseller.db"\n' + section, encoding="utf-8")
            code, out = self.cli("--local-trial")
            self.assertEqual(code, 2, out)
            self.assertEqual(self.sessions, [])
        self.assertFalse((self.root / "local-trial").exists())

    def test_page_limit_applies_to_real_trial_listing_without_plan(self):
        code, out = self.scripted_collection("--limit-shops", "A01", "--pages-per-shop", "1", "--no-shuffle",
            pages=[[FakeCard("one", offer_id="11")], [FakeCard("two", offer_id="22")]])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.trial_db().conn.execute("SELECT offer_id FROM inventory").fetchone()[0], "11")
        self.assertEqual(self.listings[0].advanced, [])

    def test_existing_daily_cutoff_stops_trial_without_success_or_formal_write(self):
        from bestseller_monitor import pipeline, detail
        boundary = cst_date() + "T15:56:00+00:00"
        with patch.object(pipeline, "utcnow", return_value=boundary), \
                patch.object(detail, "utcnow", return_value=boundary), \
                patch.object(rounds, "utcnow", return_value=boundary), \
                patch.object(click_listing, "utcnow", return_value=boundary):
            code, out = self.scripted_collection("--limit-shops", "A01")
        self.assertEqual(code, 0, out)
        db = self.trial_db()
        self.assertEqual(db.conn.execute("SELECT terminal_reason FROM rounds").fetchone()[0], "DAY_BOUNDARY")
        self.assertEqual(db.conn.execute("SELECT count(*) FROM inventory").fetchone()[0], 0)
        self.assertFalse((self.root / "bestseller.db").exists())
