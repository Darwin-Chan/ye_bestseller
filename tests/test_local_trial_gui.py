"""Local trial through the public GUI API and the actual CLI boundary."""
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gui
from bestseller_monitor.config import Config
from bestseller_monitor import db
from bestseller_monitor.db import connect
from helpers import isolated_locks
from test_run_cli import MINIMAL_CONFIG
from frozen_clock import frozen_clock


class LocalTrialGuiTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(frozen_clock())
        self.enterContext(isolated_locks())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "config.toml"
        self.config.write_text(MINIMAL_CONFIG + 'role = "merge_only"\n', encoding="utf-8")
        (self.root / "shops.csv").write_text(
            "shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
            "A01,店一,https://a.example/,3,1,\n"
            "A02,店二,https://b.example/,4,1,\n", encoding="utf-8")
        self.cfg = Config.from_file(self.config, root=self.root)
        self.api = gui.Api(cfg=self.cfg, config_file=self.config, now=db.utcnow)

    def test_explicit_trial_has_one_isolated_context_on_all_three_pages(self):
        before = self.config.read_bytes()
        self.assertFalse(self.api.start_run(["A01"])["ok"])
        with patch("bestseller_monitor.plan_step.prepare_week") as prepare:
            self.assertTrue(self.api.set_local_trial(True)["ok"])
            self.api.prepare()
            self.assertTrue(self.api.retry_prepare()["ok"])
            self.assertFalse(self.api.prepare_running())
            start = self.api.get_start()
            run = self.api.get_run()
            result = self.api.get_result()
            prepare.assert_not_called()
        for page in [start, run, result]:
            self.assertTrue(page["local_trial"])
            self.assertEqual(page["collection_label"], "本机试采，不共享")
            self.assertEqual(page["database"], str(self.root / "local-trial/data/bestseller.db"))
        self.assertFalse(start["plan_gate"])
        self.assertEqual([s["key"] for s in start["shops"]], ["A01", "A02"])
        self.assertEqual([s["pages"] for s in start["shops"]], [3, 4])
        self.assertEqual((self.api.cfg.machine_id, self.api.cfg.role), ("m1", "merge_only"))
        self.assertEqual(self.api.cfg.user_data_path, self.cfg.user_data_path)
        self.assertEqual(self.config.read_bytes(), before)

    def test_launcher_runs_real_trial_cli_and_reopens_the_same_round(self):
        import contextlib
        import io
        import sys
        import run
        from bestseller_monitor import browser_pw, guard
        from types import SimpleNamespace
        sessions = []
        commands = []

        def pause(cfg, **kwargs):
            sessions.append(cfg)
            raise guard.RoundPauseRequired("测试暂停")

        def launch(command, **kwargs):
            commands.append(command)
            with patch.object(run, "ROOT", self.root), patch.object(sys, "argv", command[1:]), \
                    patch.object(browser_pw, "open_session", side_effect=pause), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run.main(), 0)
            return SimpleNamespace(pid=1234, poll=lambda: 0)

        self.assertTrue(self.api.set_local_trial(True)["ok"])
        with patch.object(gui.subprocess, "Popen", side_effect=launch):
            self.assertTrue(self.api.start_run(["A01"])["ok"])
            first = self.api.get_run()
            self.assertTrue(first["has_round"])
            self.assertFalse(self.api.start_run(["A02"])["ok"])
            # Runtime settings remain reloadable; saved scope survives disabled CSV rows.
            (self.root / "shops.csv").write_text(
                "shop_key,shop_name,shop_url,pages,active,offer_list_url\n", encoding="utf-8")
            reopened = gui.Api(cfg=self.cfg, config_file=self.config, now=db.utcnow)
            self.assertTrue(reopened.set_local_trial(True)["ok"])
            self.assertTrue(reopened.resume_run()["ok"])
            self.assertEqual(reopened.get_run()["round_id"], first["round_id"])
        self.assertIn("--local-trial", commands[0])
        self.assertEqual(commands[0][commands[0].index("--config") + 1], str(self.config))
        self.assertNotIn("--limit-shops", commands[1])
        self.assertEqual([cfg.db_file for cfg in sessions], [self.root / "local-trial/data/bestseller.db"] * 2)
        self.assertEqual([cfg.role for cfg in sessions], ["merge_only"] * 2)

    def test_empty_or_stale_enabled_selection_never_launches(self):
        self.assertTrue(self.api.set_local_trial(True)["ok"])
        with patch.object(gui.subprocess, "Popen") as launch:
            self.assertFalse(self.api.start_run([])["ok"])
            self.assertFalse(self.api.start_run(["unknown"])["ok"])
            (self.root / "shops.csv").write_text(
                "shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                "A01,店一,https://a.example/,3,0,\n", encoding="utf-8")
            self.assertFalse(self.api.start_run(["A01"])["ok"])
            launch.assert_not_called()

    def test_cli_identity_is_visible_and_pause_targets_only_trial_database(self):
        from bestseller_monitor import single_instance, browser_proc
        from bestseller_monitor.db import Database
        from helpers import new_round
        trial_path = self.root / "local-trial/data/bestseller.db"
        conn = connect(trial_path)
        rid = new_round(Database(conn), "A01")
        started = Database(conn).record_crawler_process(
            4321, rid, note="run.py --local-trial", process_os_started="trial-proof")
        conn.close()
        lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(lock.release)
        cap = browser_proc.ProcessCapability(4321, 91, "trial-proof")
        with patch.object(browser_proc, "bind_process", return_value=cap), \
                patch.object(browser_proc, "process_creation_proof", return_value="trial-proof"), \
                patch.object(browser_proc, "process_capability_alive", return_value=True):
            self.assertTrue(self.api.set_local_trial(True)["ok"])
            self.assertEqual(self.api.get_start()["crawler"]["pid"], 4321)
            self.assertTrue(self.api.get_run()["running"])
            self.assertFalse(self.api.set_local_trial(False)["ok"])
            self.assertFalse(self.api.start_run(["A01"])["ok"])
            paused = self.api.pause_run()
            self.assertTrue(paused["ok"])
            with closing(connect(trial_path)) as check:
                request = Database(check).stop_request()
                self.assertIsNotNone(request)
                self.assertEqual((request["target_pid"], request["target_started_at"]), (4321, started))
                self.assertIsNone(check.execute("SELECT terminal_reason FROM rounds").fetchone()[0])
            with closing(connect(self.cfg.db_file)) as formal:
                self.assertIsNone(Database(formal).stop_request())
                self.assertEqual(formal.execute("SELECT count(*) FROM rounds").fetchone()[0], 0)

    def test_page_has_explicit_entry_and_persistent_mode_banner(self):
        html = (gui.PROJECT_ROOT / "bestseller_monitor/pages/ui_live.html").read_text(encoding="utf-8")
        self.assertIn('id="collectionMode"', html)
        self.assertIn('id="trialModeBtn"', html)
        self.assertIn('callApi("set_local_trial"', html)

    def test_external_abort_finishes_trial_round_then_stops_exact_identity(self):
        from bestseller_monitor import single_instance, browser_proc
        from bestseller_monitor.db import Database
        from helpers import new_round
        from test_gui import StopClock
        clock = StopClock()
        self.api = gui.Api(cfg=self.cfg, config_file=self.config, now=db.utcnow, stop_clock=clock)
        with closing(connect(self.cfg.db_file)) as formal:
            formal_id = new_round(Database(formal), "A01")
        self.assertTrue(self.api.set_local_trial(True)["ok"])
        path = self.api.cfg.db_file
        with closing(connect(path)) as trial:
            trial_id = new_round(Database(trial), "A02")
            Database(trial).record_crawler_process(4321, trial_id, process_os_started="trial-proof")
        lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(lock.release)
        cap = browser_proc.ProcessCapability(4321, 91, "trial-proof")
        observed = []

        def stop(capability):
            with closing(connect(path)) as trial:
                observed.append((capability.pid, trial.execute("SELECT terminal_reason FROM rounds WHERE id=?", (trial_id,)).fetchone()[0]))
            lock.release()
            return True

        with patch.object(browser_proc, "process_creation_proof", return_value="trial-proof"), \
                patch.object(browser_proc, "bind_process", return_value=cap), \
                patch.object(browser_proc, "process_capability_alive", side_effect=lambda cap: bool(single_instance.is_held(single_instance.CRAWLER_LOCK))), \
                patch.object(browser_proc, "terminate_process_capability", side_effect=stop), \
                patch.object(browser_proc, "release_process_capability"):
            self.assertEqual(self.api.get_run()["round_id"], trial_id)
            self.assertTrue(self.api.abort_run()["ok"])
            clock.advance(20)
            self.api.get_start()
            self.assertEqual(observed, [(4321, "ABANDONED")])
            self.assertEqual(self.api.get_result()["reason"], "ABANDONED")
        with closing(connect(self.cfg.db_file)) as formal:
            self.assertIsNone(formal.execute("SELECT terminal_reason FROM rounds WHERE id=?", (formal_id,)).fetchone()[0])
            self.assertIsNone(Database(formal).stop_request())

    def test_foreign_formal_process_is_never_paused_or_aborted_from_trial(self):
        from bestseller_monitor import single_instance, browser_proc
        from bestseller_monitor.db import Database
        from helpers import new_round
        self.assertTrue(self.api.set_local_trial(True)["ok"])
        with closing(connect(self.api.cfg.db_file)) as trial:
            trial_id = new_round(Database(trial), "A01")
        self.api.get_run()  # remember a trial round with the same numeric ID as formal
        with closing(connect(self.cfg.db_file)) as formal:
            formal_id = new_round(Database(formal), "A02")
            Database(formal).record_crawler_process(9876, formal_id, process_os_started="formal-proof")
        lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(lock.release)
        with patch.object(browser_proc, "terminate_process_capability") as kill:
            self.assertFalse(self.api.pause_run()["ok"])
            self.assertEqual(self.api.close_snapshot().kind, "unknown")
            self.assertFalse(self.api.abort_run()["ok"])
            self.assertFalse(self.api.resume_run()["ok"])
            kill.assert_not_called()
        for path, rid in [(self.cfg.db_file, formal_id), (self.api.cfg.db_file, trial_id)]:
            with closing(connect(path)) as conn:
                self.assertIsNone(conn.execute("SELECT terminal_reason FROM rounds WHERE id=?", (rid,)).fetchone()[0])
                self.assertIsNone(Database(conn).stop_request())

    def test_stale_trial_pid_cannot_authorize_switch_during_other_startup(self):
        from bestseller_monitor import single_instance, browser_proc
        from bestseller_monitor.db import Database
        from helpers import new_round
        path = self.root / "local-trial/data/bestseller.db"
        with closing(connect(path)) as trial:
            rid = new_round(Database(trial), "A01")
            Database(trial).record_crawler_process(4321, rid, process_os_started="old-proof")
        lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(lock.release)
        with patch.object(browser_proc, "process_creation_proof", return_value="new-proof"):
            self.assertFalse(self.api.set_local_trial(True)["ok"])
        self.assertFalse(self.api.cfg.local_trial)

    def test_missing_shop_list_and_unsafe_space_refuse_without_fallback(self):
        (self.root / "shops.csv").unlink()
        missing = self.api.set_local_trial(True)
        self.assertFalse(missing["ok"])
        self.assertIn("不会回退", missing["error"])
        self.assertFalse(self.api.cfg.local_trial)
        (self.root / "shops.csv").write_text(
            "shop_key,shop_name,shop_url,pages,active,offer_list_url\n", encoding="utf-8")
        (self.root / "local-trial").write_text("not a directory", encoding="utf-8")
        self.assertFalse(self.api.set_local_trial(True)["ok"])
        self.assertFalse(self.api.cfg.local_trial)

    def test_trial_migration_and_control_logs_do_not_reach_formal_handler(self):
        import logging
        handler = logging.FileHandler(self.root / "formal.log", encoding="utf-8")
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        try:
            self.assertTrue(self.api.set_local_trial(True)["ok"])
            self.api.get_start()
            self.api.get_run()
            self.api.get_result()
            handler.flush()
            self.assertEqual((self.root / "formal.log").read_text(encoding="utf-8"), "")
            self.assertIn(handler, root.handlers)
            self.assertIn("迁移", (self.root / "local-trial/logs/gui.log").read_text(encoding="utf-8"))
        finally:
            root.removeHandler(handler)
            handler.close()
            root.setLevel(previous_level)

    def test_own_live_child_prevents_mode_change_before_identity_registration(self):
        from types import SimpleNamespace
        self.assertTrue(self.api.set_local_trial(True)["ok"])
        with patch.object(gui.subprocess, "Popen", return_value=SimpleNamespace(pid=1234, poll=lambda: None)):
            self.assertTrue(self.api.start_run(["A01"])["ok"])
            self.assertFalse(self.api.set_local_trial(False)["ok"])
            self.assertTrue(self.api.get_run()["running"])

    def test_reopened_trial_exposes_saved_scope_as_awaiting_resume(self):
        from bestseller_monitor.db import Database
        from helpers import new_round
        path = self.root / "local-trial/data/bestseller.db"
        with closing(connect(path)) as conn:
            rid = new_round(Database(conn), "A01")
        self.assertTrue(self.api.set_local_trial(True)["ok"])
        view = self.api.get_run()
        self.assertEqual(view["round_id"], rid)
        self.assertTrue(view["awaiting_resume"])
        self.assertEqual([shop["key"] for shop in view["todo"]], ["A01"])

    def test_attaching_to_cli_trial_does_not_create_a_formal_database(self):
        from bestseller_monitor import single_instance, browser_proc
        from bestseller_monitor.db import Database
        from helpers import new_round
        path = self.root / "local-trial/data/bestseller.db"
        with closing(connect(path)) as trial:
            rid = new_round(Database(trial), "A01")
            Database(trial).record_crawler_process(4321, rid, process_os_started="trial-proof")
        lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(lock.release)
        with patch.object(browser_proc, "process_creation_proof", return_value="trial-proof"):
            self.assertTrue(self.api.set_local_trial(True)["ok"])
        self.assertFalse(self.cfg.db_file.exists())

    def test_gui_collection_stores_real_trial_inventory_images_and_dedupes_only_trial(self):
        import contextlib
        import io
        import sys
        from types import SimpleNamespace
        import run
        from bestseller_monitor import browser_pw, click_listing, product_images
        from bestseller_monitor.db import Database, cst_date
        from helpers import FakeCard, ScriptedListing, submit_offer, product_picture
        with closing(connect(self.cfg.db_file)) as formal:
            submit_offer(Database(formal), "11", cst_date(), 99, name="formal", color="red")
            before = "\n".join(formal.iterdump())
        self.assertTrue(self.api.set_local_trial(True)["ok"])
        self.assertEqual(self.api.get_start()["ov"]["products"], 0)
        cards = []

        def launch(command, **kwargs):
            card = FakeCard("trial-only", offer_id="11", content_values=[
                '<meta property="og:image" content="https://img.example/trial.png">'
                '<script>{"skuInfoMap":{"A":{"skuId":"A","name":"默认",'
                '"price":1,"canBookCount":3}}}</script>'])
            cards.append(card)
            with patch.object(run, "ROOT", self.root), patch.object(sys, "argv", command[1:]), \
                    patch.object(browser_pw, "open_session", return_value=(None, None, object(), None)), \
                    patch.object(browser_pw, "close_session"), \
                    patch.object(click_listing, "PlaywrightListing", side_effect=lambda *a: ScriptedListing([[card]])), \
                    patch.object(product_images, "urlopen", side_effect=lambda *a, **k: io.BytesIO(product_picture("green")["content"])), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run.main(), 0)
            return SimpleNamespace(pid=1234, poll=lambda: 0)

        with patch.object(gui.subprocess, "Popen", side_effect=launch), \
                patch("bestseller_monitor.matching.urlopen", side_effect=AssertionError("禁止真模型")), \
                patch("bestseller_monitor.git_channel.subprocess.run", side_effect=AssertionError("禁止共享动作")):
            self.assertTrue(self.api.start_run(["A01"])["ok"])
            self.assertEqual(self.api.get_start()["ov"]["products"], 1)
            self.assertEqual(self.api.get_result()["products_total"], 1)
            self.assertEqual(self.api.get_result()["reason"], "COMPLETED")
            self.assertTrue(self.api.start_run(["A01"])["ok"])
        with closing(connect(self.api.cfg.db_file)) as trial:
            self.assertEqual(trial.execute("SELECT stock FROM inventory").fetchone()[0], 3)
            self.assertEqual(trial.execute("SELECT count(*) FROM inventory").fetchone()[0], 1)
            self.assertEqual(trial.execute("SELECT count(*) FROM product_image_assets").fetchone()[0], 1)
            self.assertEqual(trial.execute("SELECT count(*) FROM sku_image_versions").fetchone()[0], 1)
            self.assertEqual(trial.execute("SELECT count(*) FROM snapshots WHERE page_status='跳过'").fetchone()[0], 1)
            self.assertEqual(trial.execute("SELECT count(*) FROM plan_deviations").fetchone()[0], 0)
            self.assertEqual(trial.execute("SELECT count(*) FROM weekly_plan").fetchone()[0], 0)
        with closing(connect(self.cfg.db_file)) as formal:
            self.assertEqual("\n".join(formal.iterdump()), before)
        self.assertFalse(self.cfg.exchange_root.exists())
        self.assertTrue((self.root / "local-trial/logs/run.log").exists())

    def test_cross_day_resume_refuses_without_launching_or_changing_formal_round(self):
        from bestseller_monitor.db import Database
        from helpers import new_round
        self.assertTrue(self.api.set_local_trial(True)["ok"])
        with closing(connect(self.api.cfg.db_file)) as conn:
            new_round(Database(conn), "A01", run_date="2026-09-13")
        with patch.object(gui.subprocess, "Popen") as launch:
            result = self.api.resume_run()
            self.assertFalse(result["ok"])
            self.assertIn("跨天", result["error"])
            launch.assert_not_called()

    def test_trial_child_refusal_does_not_misreport_a_weekly_plan_problem(self):
        from types import SimpleNamespace
        self.assertTrue(self.api.set_local_trial(True)["ok"])
        for code, expected in [(2, "试采"), (3, "范围")]:
            with patch.object(gui.subprocess, "Popen", return_value=SimpleNamespace(pid=1234, poll=lambda: code)):
                self.assertTrue(self.api.start_run(["A01"])["ok"])
                error = self.api.get_run()["start_error"]
                self.assertIn(expected, error)
                self.assertNotIn("本周计划", error)
