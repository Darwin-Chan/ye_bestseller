"""轮次采集时长：只通过公开生命周期与持久化读取验证。"""
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path

from bestseller_monitor import elapsed, views, rounds, pipeline, browser_pw, stop_request, click_listing, single_instance
from bestseller_monitor.db import Database, connect, utcnow
from bestseller_monitor.config import Shop
from helpers import new_round, crawler_cfg, isolated_locks, FakeCard, ScriptedListing

from test_gui import StopClock as Clock


class ElapsedTests(unittest.TestCase):
    def test_single_segment_saves_checkpoints_and_freezes_final_value_on_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round.db"
            conn = connect(path)
            clock = Clock()
            round_id = new_round(Database(conn))
            clock.advance(60)  # 准备尚未进入有效执行，不计入。
            segment = elapsed.begin(conn, round_id, process_started="execution-one",
                                    new_round=True, clock=clock)
            clock.advance(12)
            self.assertEqual(elapsed.read(conn, round_id).seconds, 0)
            segment.checkpoint()
            self.assertEqual(elapsed.read(conn, round_id).seconds, 12)
            segment.settle()
            clock.advance(80)
            segment.settle()
            segment.checkpoint()
            conn.close()
            conn = connect(path)
            try:
                fact = elapsed.read(conn, round_id)
                self.assertEqual((fact.seconds, fact.quality), (12, "complete"))
            finally:
                conn.close()

    def test_both_pages_use_saved_collection_time_not_round_wall_span(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "round.db")
            try:
                db = Database(conn)
                rid = new_round(db)
                clock = Clock()
                segment = elapsed.begin(conn, rid, process_started="one",
                                        new_round=True, clock=clock)
                clock.advance(12)
                segment.settle()
                state = views.UiState(round_id=rid)
                progress = views.run_view(conn, state=state, now=utcnow())
                result = views.result_view(conn, state=state, now=utcnow())
                self.assertEqual(progress["elapsed_sec"], 12)
                self.assertEqual(result["duration_text"], "12 秒")
                rounds.finish(db, rounds.load(db, rid), rounds.TerminalReason.COMPLETED)
                clock.advance(100)
                self.assertEqual(views.result_view(conn, state=state, now=utcnow())["duration_text"],
                                 "12 秒")
            finally:
                conn.close()

    def test_common_collection_entry_excludes_preparation_and_gui_reload_uses_final_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round.db"
            csv = Path(tmp) / "shops.csv"
            csv.write_text("shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                           "A01,店铺A,https://A01.example/,1,1,\n",
                           encoding="utf-8")
            clock = Clock()
            cfg = crawler_cfg(db_file=path, shop_csv=csv,
                              ensure_dirs=lambda: clock.advance(60))

            def open_browser(*args, **kwargs):
                clock.advance(12)  # 浏览器连接属于有效执行。
                return (None, None, None, None)

            with isolated_locks(), patch.object(browser_pw, "open_session", open_browser), \
                    patch.object(browser_pw, "close_session",
                                 side_effect=lambda *a, **kw: clock.advance(30)), \
                    patch.object(click_listing, "PlaywrightListing",
                                 return_value=ScriptedListing([[FakeCard("商品1", offer_id="11")]])):
                pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://A01.example/")],
                                   elapsed_clock=clock)
            conn = connect(path)
            try:
                rid = rounds.latest(Database(conn), finished=True).id
                self.assertEqual(elapsed.read(conn, rid).seconds, 12)
            finally:
                conn.close()
            from gui import Api
            clock.advance(80)
            api = Api(cfg=cfg, shops=[], open_conn=lambda: connect(path))
            self.assertEqual(api.get_result()["duration_text"], "12 秒")
            self.assertEqual(api.get_result()["elapsed_sec"], 12)

    def test_live_checkpoint_is_saved_at_collection_check_not_gui_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round.db"
            csv = Path(tmp) / "shops.csv"
            csv.write_text("shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                           "A01,店铺A,https://A01.example/,1,1,\n", encoding="utf-8")
            clock = Clock()
            cfg = crawler_cfg(db_file=path, shop_csv=csv, ensure_dirs=lambda: None)

            def open_browser(*args, **kwargs):
                clock.advance(5)
                stop_request.check()
                reader = connect(path)
                try:
                    rid = rounds.active_round(Database(reader)).id
                    state = views.UiState(round_id=rid)
                    self.assertEqual(views.run_view(reader, state=state, now=utcnow())["elapsed_sec"], 5)
                    clock.advance(7)
                    # 读取不能自己外推，未保存的新 7 秒尚不可见。
                    self.assertEqual(elapsed.read(reader, rid).seconds, 5)
                finally:
                    reader.close()
                return (None, None, None, None)

            with isolated_locks(), patch.object(browser_pw, "open_session", open_browser), \
                    patch.object(browser_pw, "close_session"), \
                    patch.object(click_listing, "PlaywrightListing",
                                 return_value=ScriptedListing([[FakeCard("商品1", offer_id="11")]])):
                pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://A01.example/")],
                                   elapsed_clock=clock)
            conn = connect(path)
            try:
                rid = rounds.latest(Database(conn), finished=True).id
                self.assertEqual(elapsed.read(conn, rid).seconds, 12)
            finally:
                conn.close()

    def test_legacy_database_upgrade_does_not_invent_time_from_round_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = connect(path)
            rid = new_round(Database(conn))
            conn.executescript("DROP TABLE elapsed_segments; DROP TABLE elapsed_rounds;")
            conn.close()
            conn = connect(path)
            try:
                fact = elapsed.read(conn, rid)
                self.assertEqual((fact.seconds, fact.text, fact.quality),
                                 (None, "未记录", "unrecorded"))
                ticks = iter((300.0, 312.0))
                segment = elapsed.begin(conn, rid, process_started="new-process",
                                        new_round=False, clock=lambda: next(ticks))
                segment.settle()
                fact = elapsed.read(conn, rid)
                self.assertEqual((fact.seconds, fact.text, fact.quality),
                                 (12, "至少 12 秒", "lower_bound"))
            finally:
                conn.close()

    def test_new_round_is_isolated_and_repeated_checkpoints_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "round.db")
            try:
                db = Database(conn)
                first = new_round(db)
                clock = Clock()
                segment = elapsed.begin(conn, first, process_started="one",
                                        new_round=True, clock=clock)
                clock.advance(12)
                segment.checkpoint()
                segment.checkpoint()
                self.assertEqual(elapsed.read(conn, first).seconds, 12)
                segment.settle()
                rounds.finish(db, rounds.load(db, first), rounds.TerminalReason.COMPLETED)
                second = new_round(db)
                self.assertNotEqual(first, second)
                self.assertIsNone(elapsed.read(conn, second).seconds)
                fresh = elapsed.begin(conn, second, process_started="two",
                                      new_round=True, clock=clock)
                clock.advance(3)
                fresh.settle()
                self.assertEqual(elapsed.read(conn, second).seconds, 3)
                self.assertEqual(elapsed.read(conn, first).seconds, 12)
            finally:
                conn.close()

    def test_busy_entry_never_starts_or_resets_a_collection_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round.db"
            conn = connect(path)
            rid = new_round(Database(conn))
            clock = Clock()
            segment = elapsed.begin(conn, rid, process_started="one",
                                    new_round=True, clock=clock)
            clock.advance(12)
            segment.settle()
            cfg = crawler_cfg(db_file=path, ensure_dirs=lambda: self.fail("busy must refuse first"))
            with isolated_locks():
                lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
                try:
                    with self.assertRaises(pipeline.CrawlerAlreadyRunning):
                        pipeline.run_round(cfg, [], elapsed_clock=clock)
                finally:
                    lock.release()
            self.assertEqual(elapsed.read(conn, rid).seconds, 12)
            conn.close()

    def test_timing_storage_failure_does_not_replace_collection_or_stop_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round.db"
            csv = Path(tmp) / "shops.csv"
            csv.write_text("shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                           "A01,店铺A,https://A01.example/,1,1,\n", encoding="utf-8")
            cfg = crawler_cfg(db_file=path, shop_csv=csv, ensure_dirs=lambda: None)
            clock = Clock()

            def storage(path):
                conn = connect(path)
                conn.set_authorizer(lambda action, table, *rest:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE
                    and table == "elapsed_segments" else sqlite3.SQLITE_OK)
                return conn

            def open_browser(*args, **kwargs):
                clock.advance(12)
                stop_request.check()
                return (None, None, None, None)

            with isolated_locks(), patch.object(pipeline, "connect", storage), \
                    patch.object(browser_pw, "open_session", open_browser), \
                    patch.object(browser_pw, "close_session"), \
                    patch.object(click_listing, "PlaywrightListing",
                                 return_value=ScriptedListing([[FakeCard("商品1", offer_id="11")]])):
                pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://A01.example/")],
                                   elapsed_clock=clock)
            conn = connect(path)
            try:
                run = rounds.latest(Database(conn), finished=True)
                self.assertEqual(run.reason, rounds.TerminalReason.COMPLETED)
                fact = elapsed.read(conn, run.id)
                self.assertEqual(fact.seconds, 0)
                self.assertEqual((fact.quality, fact.text), ("lower_bound", "至少 0 秒"))
            finally:
                conn.close()

    def test_collection_continues_when_only_initial_timing_insert_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round.db"
            csv = Path(tmp) / "shops.csv"
            csv.write_text("shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                           "A01,店铺A,https://A01.example/,1,1,\n", encoding="utf-8")
            cfg = crawler_cfg(db_file=path, shop_csv=csv, ensure_dirs=lambda: None)
            clock = Clock()

            def storage(path):
                conn = connect(path)
                conn.set_authorizer(lambda action, table, *rest:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT
                    and table == "elapsed_segments" else sqlite3.SQLITE_OK)
                return conn

            def open_browser(*args, **kwargs):
                clock.advance(12)
                stop_request.check()
                return (None, None, None, None)

            with isolated_locks(), patch.object(pipeline, "connect", storage), \
                    patch.object(browser_pw, "open_session", open_browser), \
                    patch.object(browser_pw, "close_session"), \
                    patch.object(click_listing, "PlaywrightListing",
                                 return_value=ScriptedListing([[FakeCard("商品1", offer_id="11")]])):
                pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://A01.example/")],
                                   elapsed_clock=clock)
            conn = connect(path)
            try:
                run = rounds.latest(Database(conn), finished=True)
                self.assertEqual(run.reason, rounds.TerminalReason.COMPLETED)
                fact = elapsed.read(conn, run.id)
                self.assertEqual((fact.seconds, fact.text), (None, "未记录"))
            finally:
                conn.close()
