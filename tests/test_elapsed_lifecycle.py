"""elapsed02：调度、多段与可证明的中断时长。"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import contextmanager

from bestseller_monitor import elapsed, rounds, pipeline, browser_pw, stop_request, click_listing, views, single_instance, waiting, browser_proc
from bestseller_monitor.db import Database, connect, utcnow
from bestseller_monitor.config import Shop
from helpers import new_round, crawler_cfg, isolated_locks, FakeCard, ScriptedListing
from test_gui import StopClock as Clock, gui_api
from test_crawler_close import Process


@contextmanager
def live_os_identity():
    """既有只读OS adapter提供稳定创建证明，不依赖测试进程的真实平台。"""
    with patch.object(browser_proc, 'process_alive', return_value=True), \
            patch.object(browser_proc, 'process_creation_proof', return_value='adapter-proof'):
        yield 'adapter-proof'


class ElapsedLifecycleTests(unittest.TestCase):
    def test_cooperative_ticks_save_about_once_per_second_without_reader_interpolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "round.db")
            try:
                rid = new_round(Database(conn))
                clock = Clock()
                segment = elapsed.begin(conn, rid, process_started="one", new_round=True, clock=clock)
                clock.advance(.4)
                segment.tick()
                self.assertEqual(elapsed.read(conn, rid).seconds, 0)
                clock.advance(.5)
                segment.tick()
                self.assertEqual(elapsed.read(conn, rid).seconds, 0)
                clock.advance(.1)
                segment.tick()
                self.assertEqual(elapsed.read(conn, rid).seconds, 1)
                clock.advance(1.6)
                self.assertEqual(elapsed.read(conn, rid).seconds, 1)
                segment.tick()
                self.assertAlmostEqual(elapsed.read(conn, rid).seconds, 2.6)
                segment.settle()
            finally:
                conn.close()

    def test_unfinished_segment_after_process_disappears_is_a_saved_lower_bound_in_both_pages(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_locks(), live_os_identity() as proof:
            path = Path(tmp) / "round.db"
            conn = connect(path)
            db = Database(conn)
            rid = new_round(db)
            started = db.record_crawler_process(pid=os.getpid(), round_id=rid, note="adapter", process_os_started=proof)
            clock = Clock()
            segment = elapsed.begin(conn, rid, process_started=started, new_round=True, clock=clock)
            lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
            try:
                clock.advance(5)
                segment.checkpoint()
                clock.advance(80)  # 未成功保存的尾段不能由读者补算。
                self.assertEqual(elapsed.read(conn, rid).quality, "recorded")
            finally:
                lock.release()  # 确定性退出 adapter：不执行 segment.settle。
            conn.close()
            conn = connect(path)
            try:
                for _ in range(2):
                    fact = elapsed.read(conn, rid)
                    self.assertEqual((fact.seconds, fact.quality, fact.text), (5, "lower_bound", "至少 5 秒"))
                    state = views.UiState(round_id=rid)
                    progress = views.run_view(conn, state=state, now=utcnow())
                    result = views.result_view(conn, state=state, now=utcnow())
                    self.assertEqual(progress["elapsed_text"], result["duration_text"])
                    self.assertEqual(result["duration_text"], "至少 5 秒")
                    self.assertEqual(progress["elapsed_note"], result["elapsed_note"])
            finally:
                conn.close()

    def config(self, tmp, clock):
        path = Path(tmp) / "round.db"
        csv = Path(tmp) / "shops.csv"
        csv.write_text("shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
                       "A01,店铺A,https://A01.example/,1,1,\n", encoding="utf-8")
        return crawler_cfg(db_file=path, shop_csv=csv, ensure_dirs=lambda: clock.advance(60))

    def collect(self, cfg, clock, work):
        listing = ScriptedListing([[FakeCard("商品1", offer_id="11")]])
        prepare = listing.prepare

        def prepared(*args, **kwargs):
            prepare(*args, **kwargs)
            work()

        listing.prepare = prepared
        with patch.object(browser_pw, "open_session", return_value=(None, None, None, None)), \
                patch.object(browser_pw, "close_session", side_effect=lambda *a, **kw: clock.advance(30)), \
                patch.object(click_listing, "PlaywrightListing", return_value=listing):
            pipeline.run_round(cfg, [Shop("A01", "店铺A", "https://A01.example/")], elapsed_clock=clock)

    def test_gui_pause_tail_then_same_round_resume_excludes_600_second_pause(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_locks():
            clock = Clock()
            cfg = self.config(tmp, clock)
            api = gui_api(db_path=cfg.db_file, stop_clock=Clock())
            process = Process()
            process.pid = os.getpid()
            api.proc = process

            def pause_after_request(before, tail, expected):
                def work():
                    clock.advance(before)
                    stop_request.check()
                    conn = connect(cfg.db_file)
                    try:
                        rid = rounds.active_round(Database(conn)).id
                        api.round_id = rid
                        self.assertTrue(api.pause_run()["ok"])
                        clock.advance(tail)
                        self.assertEqual(elapsed.read(conn, rid).seconds, expected - tail)
                        stop_request.check()
                    finally:
                        conn.close()
                return work

            self.collect(cfg, clock, pause_after_request(100, 20, 120))
            process.alive = False
            # 真实界面按约 2 秒轮询一次：先经公共接口观察到采集进程已退出、旧的暂停停止窗口
            # 结清，之后才可能有同轮续跑。issues-08-11 对 ADR-0024 的替代明确「目标被替换不能
            # 证明原目标已收尾」——跳过这次观察、直接让新执行顶掉身份行，界面就无法结清旧窗口
            # （那正是新政策要保留的可见欠账），此处模拟的是真实轮询这一步。
            self.assertIsNone(api.get_run()["stopping"])
            conn = connect(cfg.db_file)
            try:
                rid = rounds.active_round(Database(conn)).id
                self.assertEqual((elapsed.read(conn, rid).seconds, elapsed.read(conn, rid).quality),
                                 (120, "complete"))
            finally:
                conn.close()
            clock.advance(600)
            process.alive = True
            self.collect(cfg, clock, pause_after_request(160, 20, 300))
            process.alive = False
            conn = connect(cfg.db_file)
            try:
                self.assertEqual(rounds.active_round(Database(conn)).id, rid)
                fact = elapsed.read(conn, rid)
                self.assertEqual((fact.seconds, fact.quality, fact.text), (300, "complete", "5 分"))
                self.assertEqual(api.get_run()["elapsed_sec"], 300)
                self.assertEqual(api.get_result()["duration_text"], "5 分")
            finally:
                conn.close()

    def test_missing_new_segment_registration_cannot_make_old_completed_seconds_look_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round.db"
            conn = connect(path)
            rid = new_round(Database(conn))
            clock = Clock()
            first = elapsed.begin(conn, rid, process_started="first", new_round=True, clock=clock)
            clock.advance(5)
            first.settle()
            conn.set_authorizer(lambda action, table, *rest:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "elapsed_segments"
                else sqlite3.SQLITE_OK)
            second = elapsed.begin(conn, rid, process_started="lost", new_round=False, clock=clock)
            clock.advance(12)
            self.assertFalse(second.settle())
            conn.close()
            conn = connect(path)
            try:
                fact = elapsed.read(conn, rid)
                self.assertEqual((fact.seconds, fact.quality), (5, "lower_bound"))
                third = elapsed.begin(conn, rid, process_started="third", new_round=False, clock=clock)
                clock.advance(3)
                third.settle()
                self.assertEqual((elapsed.read(conn, rid).seconds, elapsed.read(conn, rid).quality),
                                 (8, "lower_bound"))
            finally:
                conn.close()

    def test_same_execution_recovers_failed_registration_without_inventing_cleanup_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / 'round.db')
            try:
                rid = new_round(Database(conn))
                clock = Clock()
                conn.set_authorizer(lambda action, table, *rest:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == 'elapsed_segments'
                    else sqlite3.SQLITE_OK)
                segment = elapsed.begin(conn, rid, process_started='recover', new_round=True, clock=clock)
                clock.advance(12)
                self.assertFalse(segment.settle())
                self.assertEqual(elapsed.read(conn, rid).text, '未记录')
                clock.advance(30)
                conn.set_authorizer(None)
                self.assertTrue(segment.settle())
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(12, 'complete'))
                segment.tick()
                segment.settle()
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(12, 'complete'))
            finally:
                conn.close()

    def test_failed_final_save_is_lower_bound_and_retry_keeps_original_end(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_locks():
            path = Path(tmp) / 'round.db'
            conn = connect(path)
            rid = new_round(Database(conn))
            clock = Clock()
            segment = elapsed.begin(conn, rid, process_started='save-failure', new_round=True, clock=clock)
            clock.advance(5)
            segment.checkpoint()
            conn.set_authorizer(lambda action, table, *rest:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE and table == 'elapsed_segments'
                else sqlite3.SQLITE_OK)
            clock.advance(7)
            self.assertFalse(segment.settle())
            reader = connect(path)
            try:
                self.assertEqual(elapsed.read(reader, rid), elapsed.Fact(5, 'lower_bound'))
            finally:
                reader.close()
            clock.advance(30)
            conn.set_authorizer(None)
            self.assertTrue(segment.checkpoint())  # 保存重试也不将结束后的清理算进来。
            self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(12, 'complete'))
            segment.settle()
            conn.close()

    def test_pipeline_waiting_slices_checkpoint_validation_backoff_and_pacing(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_locks():
            clock = Clock()
            cfg = self.config(tmp, clock)
            snapshots = []

            def work():
                conn = connect(cfg.db_file)
                try:
                    rid = rounds.active_round(Database(conn)).id
                    with patch.object(waiting, 'time', SimpleNamespace(monotonic=clock, sleep=clock.advance)):
                        for describe in ('人工验证', '退避', '自动节流'):
                            waiting.until(None, timeout_sec=3, poll_sec=.5, describe=describe)
                            snapshots.append(elapsed.read(conn, rid).seconds)
                finally:
                    conn.close()

            self.collect(cfg, clock, work)
            conn = connect(cfg.db_file)
            try:
                rid = rounds.latest(Database(conn)).id
                self.assertEqual(snapshots, [2, 5, 8])
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(9, 'complete'))
            finally:
                conn.close()

    def test_gui_abort_counts_active_tail_and_repeated_abort_refresh_does_not_grow(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_locks():
            clock = Clock()
            cfg = self.config(tmp, clock)
            api = gui_api(db_path=cfg.db_file, stop_clock=Clock())
            process = Process()
            process.pid = os.getpid()
            api.proc = process

            def work():
                clock.advance(5)
                stop_request.check()
                conn = connect(cfg.db_file)
                try:
                    api.round_id = rounds.active_round(Database(conn)).id
                    self.assertTrue(api.abort_run()['ok'])
                    clock.advance(7)
                    self.assertEqual(elapsed.read(conn, api.round_id).seconds, 5)
                    stop_request.check()
                finally:
                    conn.close()

            self.collect(cfg, clock, work)
            process.alive = False
            conn = connect(cfg.db_file)
            try:
                self.assertEqual(rounds.latest(Database(conn)).reason, rounds.TerminalReason.ABANDONED)
                self.assertEqual(elapsed.read(conn, api.round_id), elapsed.Fact(12, 'complete'))
                clock.advance(90)
                api.abort_run()
                for _ in range(3):
                    api.get_run()  # 中止后无活跃轮，刷新只观察，不结账。
                    self.assertEqual(api.get_result()['duration_text'], '12 秒')
                self.assertEqual(elapsed.read(conn, api.round_id), elapsed.Fact(12, 'complete'))
            finally:
                conn.close()

    def test_pipeline_finally_saves_exception_segment_without_changing_round_outcome(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_locks():
            clock = Clock()
            cfg = self.config(tmp, clock)

            def work():
                clock.advance(12)
                raise RuntimeError('external listing failed')

            with self.assertRaisesRegex(RuntimeError, 'external listing failed'):
                self.collect(cfg, clock, work)
            conn = connect(cfg.db_file)
            try:
                run = rounds.active_round(Database(conn))
                self.assertIsNotNone(run)  # 原异常不擅自将可续轮改成新终态。
                self.assertEqual(elapsed.read(conn, run.id), elapsed.Fact(12, 'complete'))
            finally:
                conn.close()

    def test_lost_old_segment_stays_lower_bound_on_resume_but_does_not_leak_to_new_round(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_locks():
            conn = connect(Path(tmp) / 'round.db')
            db = Database(conn)
            rid = new_round(db)
            clock = Clock()
            old = elapsed.begin(conn, rid, process_started='lost', new_round=True, clock=clock)
            clock.advance(5)
            old.checkpoint()  # 不执行收尾，未知尾段不会被接班段洗掉。
            reset = Clock()
            resumed = elapsed.begin(conn, rid, process_started='resumed', new_round=False, clock=reset)
            reset.advance(3)
            resumed.settle()
            self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(8, 'lower_bound'))
            rounds.finish(db, rounds.load(db, rid), rounds.TerminalReason.ABANDONED)
            other = new_round(db)
            current = elapsed.begin(conn, other, process_started='other', new_round=True, clock=Clock())
            current.settle()
            self.assertEqual(elapsed.read(conn, other), elapsed.Fact(0, 'complete'))
            # 迟到原段只更新原轮，绝不将当前轮结账或继承原轮时长。
            old.settle()
            self.assertEqual(elapsed.read(conn, other), elapsed.Fact(0, 'complete'))
            conn.close()

    def test_lock_alone_or_replaced_full_identity_cannot_prove_old_segment_active(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_locks(), live_os_identity() as proof:
            conn = connect(Path(tmp) / 'round.db')
            db = Database(conn)
            rid = new_round(db)
            started = db.record_crawler_process(os.getpid(), rid, process_os_started=proof)
            clock = Clock()
            segment = elapsed.begin(conn, rid, process_started=started, new_round=True, clock=clock)
            clock.advance(5)
            segment.checkpoint()
            lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
            try:
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(5, 'recorded'))
                # 相同pid的新启动登记也是另一次执行；仅锁仍活跃不能洗掉旧缺口。
                db.record_crawler_process(os.getpid(), rid, process_os_started=proof)
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(5, 'lower_bound'))
                db.clear_crawler_process()
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(5, 'lower_bound'))
            finally:
                lock.release()
                conn.close()

    def test_01_schema_migration_retains_saved_seconds_without_proving_unfinished_history(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_locks():
            path = Path(tmp) / 'round.db'
            conn = connect(path)
            db = Database(conn)
            rid = new_round(db)
            clock = Clock()
            first = elapsed.begin(conn, rid, process_started='settled', new_round=True, clock=clock)
            clock.advance(5)
            first.settle()
            second = elapsed.begin(conn, rid, process_started='unfinished', new_round=False, clock=clock)
            clock.advance(3)
            second.checkpoint()
            # 仅建立旧01 schema fixture；结果必须通过公开读取，而非SQL断言。
            conn.execute('ALTER TABLE elapsed_rounds DROP COLUMN pending_execution')
            conn.execute('ALTER TABLE elapsed_segments DROP COLUMN process_pid')
            conn.commit()
            conn.close()
            conn = connect(path)
            try:
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(8, 'lower_bound'))
                fresh = elapsed.begin(conn, rid, process_started='after-migration', new_round=False, clock=Clock())
                fresh.settle()
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(8, 'lower_bound'))
            finally:
                conn.close()

    def test_wall_clock_changes_and_reloaded_monotonic_epoch_do_not_change_sum(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'round.db'
            conn = connect(path)
            rid = new_round(Database(conn))
            clock = Clock()
            segment = elapsed.begin(conn, rid, process_started='first', new_round=True, clock=clock)
            with patch.object(elapsed.time, 'time', return_value=10**12):
                clock.advance(5)
                segment.checkpoint()
                self.assertEqual(elapsed.read(conn, rid).seconds, 5)
            with patch.object(elapsed.time, 'time', return_value=-10**12):
                clock.advance(7)
                segment.settle()
            conn.close()
            conn = connect(path)
            try:
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(12, 'complete'))
                reset_clock = Clock()
                resumed = elapsed.begin(conn, rid, process_started='new-epoch', new_round=False, clock=reset_clock)
                reset_clock.advance(3)
                resumed.settle()
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(15, 'complete'))
            finally:
                conn.close()


    def test_new_lock_holder_before_identity_registration_cannot_reanimate_dead_old_segment(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_locks():
            conn = connect(Path(tmp) / 'round.db')
            try:
                db = Database(conn)
                rid = new_round(db)
                started = db.record_crawler_process(os.getpid(), rid, process_os_started='old-os-proof')
                clock = Clock()
                segment = elapsed.begin(conn, rid, process_started=started, new_round=True, clock=clock)
                clock.advance(5)
                segment.checkpoint()
                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(5, 'lower_bound'))
                # 新执行持锁但还在准备；库中仍是旧身份。OS adapter明确旧目标已死。
                lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
                try:
                    for alive, created in ((False, None), (None, None), (True, 'different-os-proof'), (True, None)):
                        with self.subTest(alive=alive, created=created), \
                                patch.object(browser_proc, 'process_alive', return_value=alive), \
                                patch.object(browser_proc, 'process_creation_proof', return_value=created):
                            for _ in range(2):
                                self.assertEqual(elapsed.read(conn, rid), elapsed.Fact(5, 'lower_bound'))
                finally:
                    lock.release()
            finally:
                conn.close()
