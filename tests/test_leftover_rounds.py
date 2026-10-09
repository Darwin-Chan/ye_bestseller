"""Ticket 06: 异常及跨天遗留轮次明确处置，不冒充暂停。

Public seam: the shipped start/run page events over the real Api, real temporary
SQLite rounds, real stop-watch/idle facts; only OS process/browser facts are
controlled adapters (browser_proc patches), matching tickets 01/03 fixtures.
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import gui
from bestseller_monitor import rounds, single_instance
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, connect
from frozen_clock import FROZEN_DATE, frozen_clock
from helpers import isolated_locks
from test_gui import StopClock, gui_api, pause_for_resume, store_today_plan


class LeftoverRoundTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(frozen_clock())
        self.enterContext(isolated_locks())
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.db_path = Path(self.tmp) / 'test.db'
        self.shops = [Shop(k, k, f'https://{k}.example/') for k in ('A', 'B', 'C', 'D')]
        store_today_plan(self.db_path, *((k, 'm-test', 3) for k in ('A', 'B', 'C', 'D')))
        self.clock = StopClock()
        self.api = gui_api(shops=self.shops, db_path=self.db_path, stop_clock=self.clock)

    # ---------- fixtures ----------
    def open_round(self, keys=('A', 'B', 'C', 'D'), *, day=None):
        """建一轮并保持「未结束」。

        `rounds.open` 仍带 CLI 的跨天收尾政策（建 09-13 的轮会把 09-14 的先收成
        跨天终态）。界面显式新建走的 open_new 不会这样；要模型「库里同时留着多个
        未结束轮次」这个历史现场，夹具把**本次调用顺带收尾的那几行**显式重开。
        """
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            request = rounds.RoundRequest(day or FROZEN_DATE, tuple(
                rounds.ShopScope(k, f'https://{k}.example/', f'店铺{k}') for k in keys))
            opened = rounds.open(db, request)
            for superseded in opened.superseded:
                db.conn.execute("UPDATE rounds SET terminal_reason=NULL, finished_at=NULL "
                                "WHERE id=?", (superseded.id,))
            db.conn.commit()
            return opened.round.id

    def load(self, rid):
        with closing(connect(self.db_path)) as conn:
            return rounds.load(Database(conn), rid)

    def identity_row(self):
        with closing(connect(self.db_path)) as conn:
            return Database(conn).crawler_process()

    def record_dead_identity(self, rid, *, state="NOT_STARTED", browser_pid=None,
                             browser_os_started=None, port=None,
                             pid=4321, process_os_started="crawler-proof"):
        """A residual identity row of a collector that is no longer running."""
        with closing(connect(self.db_path)) as conn:
            Database(conn).record_crawler_process(
                pid=pid, round_id=rid, process_os_started=process_os_started,
                browser_state=state, browser_port=port,
                browser_pid=browser_pid, browser_os_started=browser_os_started)

    def gone(self, *dead_pids):
        """OS liveness adapter: listed pids are gone, everything else is alive."""
        def alive(pid):
            return False if pid in dead_pids else True
        return patch("bestseller_monitor.browser_proc.process_alive", side_effect=alive)

    def proof(self, mapping):
        """OS creation-proof adapter keyed by pid."""
        return patch("bestseller_monitor.browser_proc.process_creation_proof",
                     side_effect=lambda pid: mapping.get(pid))

    def running(self, *, owned=False):
        """Claimed live collector (in this UI) over a real open round."""
        rid = self.open_round()
        with closing(connect(self.db_path)) as conn:
            Database(conn).record_crawler_process(
                pid=4321, round_id=rid, process_os_started='crawler-proof',
                browser_state='OWNED' if owned else 'NOT_STARTED',
                browser_pid=9201 if owned else None,
                browser_os_started='browser-proof' if owned else None,
                browser_port=9222 if owned else None)
        lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(lock.release)
        process = MagicMock(pid=4321)
        process.poll.return_value = None

        def death():
            process.poll.return_value = 0
            lock.release()
        process.terminate.side_effect = death
        self.api.proc = process
        self.api.round_id = rid
        return rid, process, death

    def page(self):
        """The shipped HTML with a real API bridge, offline and silent."""
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        self.addCleanup(pw.stop)
        browser = pw.chromium.launch(channel="msedge", headless=True)
        self.addCleanup(browser.close)
        context = browser.new_context()
        self.addCleanup(context.close)
        context.route("https://**/*", lambda route: route.abort())
        context.route("http://**/*", lambda route: route.abort())
        page = context.new_page()
        page.expose_function("ticketApi", lambda name, args: getattr(self.api, name)(*args))
        page.add_init_script("""
          window.pywebview = {api: new Proxy({}, {
            get: (target, name) => name in target ? target[name] : (...args) => window.ticketApi(name, args)
          })};
          window.alert = () => {};
        """)
        page.goto((Path(gui.PROJECT_ROOT) / "bestseller_monitor/pages/ui_live.html").as_uri())
        page.locator("input[data-i]").first.wait_for()
        return page

    # ---------- 11-A05 / 11-A06 / read-only safety ----------
    def test_read_only_views_never_end_or_start_anything_for_leftovers(self):
        rid_today = self.open_round()
        rid_stale = self.open_round(day="2026-09-13")
        with patch.object(gui.subprocess, "Popen") as spawn, \
                patch("bestseller_monitor.browser_proc.close_browser") as close, \
                patch("bestseller_monitor.browser_proc.terminate_process_capability") as kill:
            for _ in range(2):
                start = self.api.get_start()
                run = self.api.get_run()
                self.api.get_result()
        self.assertFalse(start["start_allowed"])
        self.assertTrue(run["has_round"])
        self.assertFalse(run.get("can_resume", False))
        self.assertTrue(self.load(rid_today).in_progress)
        self.assertTrue(self.load(rid_stale).in_progress)
        spawn.assert_not_called()
        close.assert_not_called()
        kill.assert_not_called()

    def test_start_view_lists_each_unfinished_round_with_its_real_state(self):
        rid_today = self.open_round()
        rid_stale = self.open_round(day="2026-09-13")

        start = self.api.get_start()

        rows = (start.get("pending") or {}).get("rounds") or []
        self.assertEqual({r["id"] for r in rows}, {rid_today, rid_stale})
        by_id = {r["id"]: r for r in rows}
        self.assertFalse(by_id[rid_today]["cross_day"])
        self.assertTrue(by_id[rid_stale]["cross_day"])
        self.assertIn("采集已停止，本轮未正常结束", by_id[rid_today]["text"])
        self.assertIn("已跨天", by_id[rid_stale]["text"])
        self.assertIn("采集已停止，本轮未正常结束", start["start_hint"])
        self.assertIn(f"#{rid_stale}", start["start_hint"])
        self.assertFalse(start["start_allowed"])

    def test_run_page_never_treats_a_leftover_as_an_explicit_pause(self):
        rid = self.open_round()

        run = self.api.get_run()

        self.assertTrue(run["has_round"])
        self.assertEqual(run["round_id"], rid)
        self.assertFalse(run.get("manually_paused", False))
        self.assertFalse(run.get("can_resume", False))
        self.assertFalse(self.api.resume_run()["ok"])

    def test_auto_retained_exits_keep_their_real_terminal_not_user_pause(self):
        # 其他自动保留轮次的真实终态照原样呈现，不改写成用户暂停/中止
        rid = self.open_round()
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            rounds.finish(db, rounds.load(db, rid), rounds.TerminalReason.FAIL_RATE_EXCEEDED)
        result = self.api.get_result()
        self.assertEqual(result["tag"], "暂停待处理")
        self.assertNotIn("人工中止", result["tag"])
        self.assertFalse(self.api.resume_run()["ok"])
        # 历史无终态（如旧验证超时保留的轮）按未正常结束呈现，不冒充已暂停
        legacy = self.open_round(keys=("D",), day="2026-09-13")
        run = self.api.get_run()
        self.assertFalse(run.get("has_round", False))
        self.assertFalse(run.get("can_resume", False))
        start = self.api.get_start()
        row = next(r for r in start["pending"]["rounds"] if r["id"] == legacy)
        self.assertFalse(row.get("resumable"))
        self.assertIn("未正常结束", row["text"])
        self.assertNotIn("已暂停", row["text"])

    # ---------- 11-A11 / confirmation contract ----------
    def test_confirmed_leftover_abort_writes_the_target_round(self):
        rid = self.open_round()

        prepared = self.api.prepare_abort(rid)

        self.assertTrue(prepared["ok"], prepared)
        self.assertEqual(prepared["round_id"], rid)
        self.assertEqual(prepared["run_date"], FROZEN_DATE)
        self.assertEqual(set(prepared["shops"]), {"A", "B", "C", "D"})
        self.assertFalse(prepared["cross_day"])
        self.assertTrue(self.load(rid).in_progress, "确认之前不许写终态")
        with patch.object(gui.subprocess, "Popen") as spawn:
            result = self.api.confirm_abort(prepared["confirmation"])
            self.assertTrue(result["ok"], result)
            spawn.assert_not_called()
        self.assertEqual(result["round_id"], rid)
        self.assertEqual(self.load(rid).reason, rounds.TerminalReason.ABANDONED)
        first = self.load(rid)
        # 幂等：重复确认保留先提交的终态，不重复创建也不报假成功
        repeated = self.api.confirm_abort(prepared["confirmation"])
        self.assertTrue(repeated["ok"])
        self.assertEqual(repeated["reason"], "ABANDONED")
        self.assertEqual(self.load(rid), first)
        # 已结束的轮次不再提供中止靶子
        refused = self.api.prepare_abort(rid)
        self.assertFalse(refused["ok"])
        # 收尾干净后，显式新建不再被旧轮阻挡
        with patch.object(gui.subprocess, "Popen"):
            self.assertTrue(self.api.start_run(["D"])["ok"])

    def test_leftover_abort_confirmation_survives_only_unchanged_facts(self):
        # 新目标出现（同轮的新登记）→ 拒绝，旧授权不适用
        rid = self.open_round()
        prepared = self.api.prepare_abort(rid)
        self.record_dead_identity(rid, pid=5432, process_os_started="new-proof")
        result = self.api.confirm_abort(prepared["confirmation"])
        self.assertFalse(result["ok"], "同轮后来登记的执行不能被旧确认处置")
        self.assertIn("变化", result["error"])
        self.assertTrue(self.load(rid).in_progress)
        with closing(connect(self.db_path)) as conn:
            row = Database(conn).crawler_process()
            if row is not None:
                Database(conn).clear_crawler_process(
                    target_pid=row["pid"], target_started_at=row["started_at"])

    def test_leftover_abort_confirmation_refuses_while_a_collection_runs(self):
        rid = self.open_round()
        prepared = self.api.prepare_abort(rid)
        lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(lock.release)
        with patch.object(gui.subprocess, "Popen") as spawn:
            result = self.api.confirm_abort(prepared["confirmation"])
            spawn.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertTrue(self.load(rid).in_progress)

    def test_leftover_abort_keeps_a_terminal_that_landed_after_confirmation(self):
        rid = self.open_round()
        prepared = self.api.prepare_abort(rid)
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            done = rounds.finish(db, rounds.load(db, rid), rounds.TerminalReason.COMPLETED)
        result = self.api.confirm_abort(prepared["confirmation"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "COMPLETED")
        self.assertEqual(self.load(rid), done)

    def test_leftover_abort_write_failure_is_retryable_and_blocks_new(self):
        fail = [False]

        def factory():
            conn = connect(self.db_path)
            if fail[0]:
                conn.set_authorizer(lambda action, table, *rest:
                                    sqlite3.SQLITE_DENY
                                    if action == sqlite3.SQLITE_UPDATE and table == "rounds"
                                    else sqlite3.SQLITE_OK)
            return conn

        self.api = gui_api(shops=self.shops, open_conn=factory, stop_clock=self.clock)
        rid = self.open_round()
        prepared = self.api.prepare_abort(rid)
        fail[0] = True
        with patch.object(gui.subprocess, "Popen") as spawn:
            result = self.api.confirm_abort(prepared["confirmation"])
            spawn.assert_not_called()
            self.assertFalse(result["ok"])
            self.assertTrue(result["retryable"])
            self.assertTrue(self.load(rid).in_progress)
            self.assertFalse(self.api.start_run(["D"])["ok"])
        fail[0] = False
        self.assertTrue(self.api.confirm_abort(prepared["confirmation"])["ok"])
        self.assertEqual(self.load(rid).reason, rounds.TerminalReason.ABANDONED)

    # ---------- 11-A12 leftover cleanup settlement ----------
    def test_leftover_abort_settles_dead_owned_browser_with_exact_proof(self):
        rid = self.open_round()
        self.record_dead_identity(rid, state="OWNED", browser_pid=9201,
                                  browser_os_started="browser-proof", port=9222)
        prepared = self.api.prepare_abort(rid)
        self.assertTrue(prepared["ok"], prepared)
        with self.gone(4321, 9201), \
                patch("bestseller_monitor.browser_proc.close_browser", return_value=9201) as close, \
                patch("bestseller_monitor.browser_proc.terminate_process_capability") as kill:
            result = self.api.confirm_abort(prepared["confirmation"])
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result.get("cleanup"), "pending")
        self.assertEqual(self.load(rid).reason, rounds.TerminalReason.ABANDONED)
        self.assertIsNone(self.identity_row(), "核得实的旧欠账要结清")
        close.assert_called_once()
        self.assertEqual(close.call_args.args[0], 9222)
        self.assertEqual(close.call_args.kwargs.get("browser_pid"), 9201)
        self.assertEqual(close.call_args.kwargs.get("browser_os_started"), "browser-proof")
        kill.assert_not_called()
        self.assertTrue(self.api.get_start()["start_allowed"])

    def test_leftover_abort_never_clears_unverifiable_browser_debt(self):
        rid = self.open_round()
        self.record_dead_identity(rid, state="UNKNOWN")
        prepared = self.api.prepare_abort(rid)
        with self.gone(4321), \
                patch("bestseller_monitor.browser_proc.close_browser") as close:
            result = self.api.confirm_abort(prepared["confirmation"])
            close.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result.get("cleanup"), "pending")
        self.assertIn("收尾", result.get("note", ""))
        self.assertEqual(self.load(rid).reason, rounds.TerminalReason.ABANDONED)
        self.assertIsNotNone(self.identity_row(), "核不实的欠账不抹除")
        self.assertFalse(self.api.get_start()["start_allowed"])

    def test_leftover_abort_clears_a_complete_row_without_touching_browsers(self):
        rid = self.open_round()
        self.record_dead_identity(rid, state="NOT_STARTED")
        prepared = self.api.prepare_abort(rid)
        with self.gone(4321), \
                patch("bestseller_monitor.browser_proc.close_browser") as close:
            result = self.api.confirm_abort(prepared["confirmation"])
            close.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertIsNone(self.identity_row())
        self.assertTrue(self.api.get_start()["start_allowed"])

    def test_leftover_abort_retries_a_failed_browser_close_until_os_proof_settles(self):
        rid = self.open_round()
        self.record_dead_identity(rid, state="OWNED", browser_pid=9201,
                                  browser_os_started="browser-proof", port=9222)
        prepared = self.api.prepare_abort(rid)
        with self.gone(4321), \
                self.proof({9201: "browser-proof"}), \
                patch("bestseller_monitor.browser_proc.close_browser", return_value=None) as close:
            result = self.api.confirm_abort(prepared["confirmation"])
            self.assertTrue(result["ok"])
            self.assertEqual(result.get("cleanup"), "pending")
            self.assertIsNotNone(self.identity_row(), "关不掉的原浏览器保留欠账")
            self.assertFalse(self.api.get_start()["start_allowed"])
            close.assert_called()
        # PID 已被另一个创建身份接管：原浏览器确已不在，允许安全结账
        with self.gone(4321), \
                self.proof({9201: "unrelated-new-browser"}), \
                patch("bestseller_monitor.browser_proc.close_browser", return_value=None):
            retried = self.api.confirm_abort(prepared["confirmation"])
        self.assertTrue(retried["ok"], retried)
        self.assertIsNone(self.identity_row())
        self.assertTrue(self.api.get_start()["start_allowed"])

    # ---------- 多个遗留逐个处置 ----------
    def test_multiple_leftovers_are_disposed_of_one_at_a_time(self):
        rid_today = self.open_round()
        rid_stale = self.open_round(day="2026-09-13")

        prepared = self.api.prepare_abort(rid_stale)
        self.assertTrue(prepared["cross_day"])
        self.assertTrue(self.api.confirm_abort(prepared["confirmation"])["ok"])
        self.assertEqual(self.load(rid_stale).reason, rounds.TerminalReason.ABANDONED)
        self.assertTrue(self.load(rid_today).in_progress, "一次授权不许扩大为全部旧轮")
        start = self.api.get_start()
        ids = {r["id"] for r in (start.get("pending") or {}).get("rounds") or []}
        self.assertEqual(ids, {rid_today})
        self.assertFalse(start["start_allowed"])
        with patch.object(gui.subprocess, "Popen") as spawn:
            blocked = self.api.start_run(["D"])
            self.assertFalse(blocked["ok"])
            self.assertIn("中止", blocked["error"])
            spawn.assert_not_called()
        prepared = self.api.prepare_abort(rid_today)
        self.assertTrue(self.api.confirm_abort(prepared["confirmation"])["ok"])
        with patch.object(gui.subprocess, "Popen"):
            self.assertTrue(self.api.start_run(["D"])["ok"])

    # ---------- 跨天暂停：不能继续，显式中止 ----------
    def test_cross_day_paused_round_cannot_resume_and_is_shown(self):
        rid = self.open_round(day="2026-09-13")
        self.api.round_id = rid
        self.api.user_paused = True
        self.api._paused_round_id = rid

        run = self.api.get_run()

        self.assertTrue(run["has_round"])
        self.assertEqual(run["round_id"], rid)
        self.assertTrue(run.get("cross_day"))
        self.assertFalse(run.get("can_resume"))
        refused = self.api.resume_run()
        self.assertFalse(refused["ok"])
        self.assertIn("跨天", refused["error"])
        self.assertTrue(self.load(rid).in_progress)
        # 开始页仍把它当作需要显式处置的遗留，而不是普通暂停续跑
        start = self.api.get_start()
        row = next(r for r in start["pending"]["rounds"] if r["id"] == rid)
        self.assertTrue(row["cross_day"])
        self.assertFalse(row.get("resumable"))
        self.assertFalse(start["start_allowed"])

    def test_run_page_keeps_an_unfinished_round_explicit_instead_of_a_result(self):
        from playwright.sync_api import expect
        handles = {}

        def spawn(*args, **kwargs):
            rid, process, death = self.running()
            handles.update(death=death)
            return process

        with patch.object(gui.subprocess, "Popen", side_effect=spawn):
            page = self.page()
            page.locator("#startBtn").click()
            expect(page.locator("#panel-run")).to_have_class("panel active")
            handles["death"]()
            page.evaluate("tickRun()")
            expect(page.locator("#rUnfinished")).to_be_visible(timeout=8000)
            expect(page.locator("#rUnfinished")).to_contain_text("采集已停止，本轮未正常结束")
            expect(page.locator("#resumeBtn")).to_be_hidden()
            expect(page.locator("#pauseBtn")).to_be_hidden()
            expect(page.locator("#abortBtn")).to_be_enabled()
            page.locator("#abortBtn").click()
            expect(page.locator("#confirmSub")).to_contain_text("不能继续")
            page.locator("#confirmOk").click()
            expect(page.locator("#resTag")).to_have_text("人工中止")

    def test_start_page_cancel_of_leftover_abort_changes_nothing(self):
        from playwright.sync_api import expect
        rid = self.open_round()
        page = self.page()

        expect(page.locator("#startBtn")).to_be_disabled()
        expect(page.locator("#activeRoundBanner")).to_contain_text("采集已停止，本轮未正常结束")
        with patch.object(gui.subprocess, "Popen") as spawn:
            page.locator("#pendingRows button").click()
            expect(page.locator("#confirmSub")).to_contain_text("不能继续")
            expect(page.locator("#confirmSub")).to_contain_text("保留")
            expect(page.locator("#confirmSub")).to_contain_text(f"#{rid}")
            page.locator("#confirmCancel").click()
            expect(page.locator("#confirmModal")).not_to_have_class("show")
            self.assertTrue(self.load(rid).in_progress, "取消不写终态")
            page.locator("#pendingRows button").click()
            page.locator("#confirmOk").click()
            expect(page.locator("#startBtn")).to_be_enabled(timeout=8000)
            spawn.assert_not_called()
        self.assertEqual(self.load(rid).reason, rounds.TerminalReason.ABANDONED)

    # ---------- 11-A15 / 重开路径 ----------
    def test_normal_close_reopen_vs_abnormal_exit_reopen(self):
        from bestseller_monitor.crawler_close import install_close_guard
        from test_crawler_close import Window
        rid, process, _ = self.running(owned=True)
        window, tasks, notices = Window(), [], []
        with patch("bestseller_monitor.browser_proc.close_browser", return_value=9201):
            install_close_guard(window, self.api, confirm=lambda text: True,
                                notify=notices.append, title="test",
                                launch=tasks.append,
                                wait=lambda sec: self.clock.advance(9))
            self.assertFalse(window.close())
            tasks.pop()()
        self.assertTrue(window.destroyed)
        self.assertEqual(self.load(rid).reason, rounds.TerminalReason.ABANDONED)
        reopened = gui_api(shops=self.shops, db_path=self.db_path, stop_clock=self.clock)
        self.assertFalse(reopened.get_run().get("can_resume", False))
        self.assertFalse(reopened.resume_run()["ok"])
        self.assertTrue(reopened.get_start()["start_allowed"],
                        "正常关窗确认中止、收尾完成后重开不再被旧轮阻挡")

        # 异常退出：进程没了、没有终态、也没有本界面暂停依据
        crashed = self.open_round(keys=("D",))
        self.record_dead_identity(crashed, state="NOT_STARTED")
        reopened2 = gui_api(shops=self.shops, db_path=self.db_path, stop_clock=self.clock)
        start = reopened2.get_start()
        rows = {r["id"]: r for r in start["pending"]["rounds"]}
        self.assertIn(crashed, rows)
        self.assertIn("采集已停止，本轮未正常结束", rows[crashed]["text"])
        self.assertFalse(start["start_allowed"])
        self.assertFalse(reopened2.resume_run()["ok"])
        prepared = reopened2.prepare_abort(crashed)
        with self.gone(4321), \
                patch("bestseller_monitor.browser_proc.close_browser") as close:
            self.assertTrue(reopened2.confirm_abort(prepared["confirmation"])["ok"])
            close.assert_not_called()
        self.assertEqual(self.load(crashed).reason, rounds.TerminalReason.ABANDONED)
        with patch.object(gui.subprocess, "Popen"):
            self.assertTrue(reopened2.start_run(["D"])["ok"])

    def test_cancel_close_keeps_a_real_pause_intact(self):
        from bestseller_monitor.crawler_close import install_close_guard
        from test_crawler_close import Window
        rid = self.open_round()
        pause_for_resume(self.api, self.db_path, rid)
        self.assertTrue(self.api.get_run()["can_resume"])

        window, tasks = Window(), []
        install_close_guard(window, self.api, confirm=lambda text: False,
                            notify=lambda text: None, title="test",
                            launch=tasks.append, wait=lambda sec: None)

        self.assertFalse(window.close())
        self.assertFalse(window.destroyed)
        self.assertTrue(self.load(rid).in_progress)
        self.assertTrue(self.api.get_run()["can_resume"], "取消关窗维持原暂停状态")
        with patch.object(gui.subprocess, "Popen"):
            self.assertTrue(self.api.resume_run()["ok"])


    # ---------- 11-A12 复审整改：收尾欠账保留后必须仍有重试入口 ----------
    def test_kept_cleanup_debt_exposes_retry_entry_and_settles(self):
        rid = self.open_round()
        self.record_dead_identity(rid, state="OWNED", browser_pid=9201,
                                  browser_os_started="browser-proof", port=9222)
        prepared = self.api.prepare_abort(rid)
        with self.gone(4321), \
                self.proof({9201: "browser-proof"}), \
                patch("bestseller_monitor.browser_proc.close_browser", return_value=None):
            first = self.api.confirm_abort(prepared["confirmation"])
        self.assertTrue(first["ok"])
        self.assertEqual(first["cleanup"], "pending")
        start = self.api.get_start()
        self.assertFalse(start["start_allowed"], "欠账未结清不放行新建")
        residue = start.get("residue") or {}
        self.assertEqual(residue.get("round_id"), rid)
        self.assertEqual(residue.get("state"), "OWNED")
        # 轮次已有终态，pending 卡消失后仍有产品内重试入口
        retry = self.api.prepare_cleanup_retry()
        self.assertTrue(retry["ok"], retry)
        with self.gone(4321), \
                self.proof({9201: "unrelated-new-browser"}), \
                patch("bestseller_monitor.browser_proc.close_browser", return_value=None):
            done = self.api.confirm_abort(retry["confirmation"])
        self.assertTrue(done["ok"], done)
        self.assertNotEqual(done.get("cleanup"), "pending")
        self.assertIsNone(self.identity_row())
        self.assertTrue(self.api.get_start()["start_allowed"])
        self.assertIsNone(self.api.get_start().get("residue"))

    def test_unsettled_cleanup_debt_keeps_the_manual_login_entry_disabled(self):
        # spec 可用性表：已有终态但停止/浏览器收尾未完成时，手动登录入口禁用。
        rid = self.open_round()
        self.record_dead_identity(rid, state="OWNED", browser_pid=9201,
                                  browser_os_started="browser-proof", port=9222)
        prepared = self.api.prepare_abort(rid)
        with self.gone(4321), \
                self.proof({9201: "browser-proof"}), \
                patch("bestseller_monitor.browser_proc.close_browser", return_value=None):
            first = self.api.confirm_abort(prepared["confirmation"])
        self.assertTrue(first["ok"])
        self.assertEqual(first["cleanup"], "pending")
        start = self.api.get_start()
        self.assertEqual((start.get("residue") or {}).get("round_id"), rid)
        self.assertFalse(start["browser"]["can_open"], "收尾欠账未结清不提供手动入口")
        refused = self.api.open_login_browser()
        self.assertFalse(refused["ok"])
        self.assertIn("收尾", refused["error"])
        page = self.page()
        page.evaluate("refreshStart()")
        self.assertTrue(page.locator("#loginBrowserBtn").is_disabled())
        # 结清（重试收尾核验）后入口恢复可用
        retry = self.api.prepare_cleanup_retry()
        self.assertTrue(retry["ok"], retry)
        with self.gone(4321), \
                self.proof({9201: "unrelated-new-browser"}), \
                patch("bestseller_monitor.browser_proc.close_browser", return_value=None):
            done = self.api.confirm_abort(retry["confirmation"])
        self.assertNotEqual(done.get("cleanup"), "pending")
        self.assertIsNone(self.api.get_start().get("residue"))
        self.assertTrue(self.api.get_start()["browser"]["can_open"])

    def test_cleanup_retry_without_residue_refuses_and_cancel_writes_nothing(self):
        rid = self.open_round()
        self.assertFalse(self.api.prepare_cleanup_retry()["ok"])
        # 取消路径：冻结之后不确认，什么都不发生
        self.record_dead_identity(rid, state="OWNED", browser_pid=9201,
                                  browser_os_started="browser-proof", port=9222)
        prepared = self.api.prepare_cleanup_retry()
        self.assertTrue(prepared["ok"])
        self.assertIsNotNone(self.identity_row())
        self.assertTrue(self.load(rid).in_progress)

    def test_leftover_abort_keeps_real_terminal_and_cleanup_notes_together(self):
        rid = self.open_round()
        self.record_dead_identity(rid, state="OWNED", browser_pid=9201,
                                  browser_os_started="browser-proof", port=9222)
        prepared = self.api.prepare_abort(rid)
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            rounds.finish(db, rounds.load(db, rid), rounds.TerminalReason.COMPLETED)
        with self.gone(4321), \
                self.proof({9201: "browser-proof"}), \
                patch("bestseller_monitor.browser_proc.close_browser", return_value=None):
            result = self.api.confirm_abort(prepared["confirmation"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "COMPLETED")
        self.assertEqual(result["cleanup"], "pending")
        note = result.get("note", "")
        self.assertIn("保留原终态", note, "真实终态的事实不能被收尾说明吞掉")
        self.assertIn("收尾", note)
        self.assertIn("重试收尾核验", note)

    def test_unverifiable_process_with_complete_row_is_explained(self):
        rid = self.open_round()
        self.record_dead_identity(rid, state="NOT_STARTED")
        prepared = self.api.prepare_abort(rid)
        with patch("bestseller_monitor.browser_proc.process_alive", return_value=None), \
                patch("bestseller_monitor.browser_proc.process_creation_proof", return_value=None):
            result = self.api.confirm_abort(prepared["confirmation"])
        self.assertTrue(result["ok"], result)
        self.assertNotIn("cleanup", result, "完整状态不构成收尾欠账")
        self.assertIn("残留登记", result.get("note", ""), "核不实要解释，不静默成功")

    def test_cross_day_paused_shown_even_when_another_leftover_is_newer(self):
        rid_paused = self.open_round(keys=("A",), day="2026-09-13")
        self.open_round(keys=("D",), day="2026-09-12")  # 更旧的遗留行，但不是本界面暂停的那一轮
        self.api.round_id = rid_paused
        self.api.user_paused = True
        self.api._paused_round_id = rid_paused

        run = self.api.get_run()

        self.assertTrue(run["has_round"], "跨天暂停轮不是最新遗留也要能显示")
        self.assertEqual(run["round_id"], rid_paused)
        self.assertTrue(run["cross_day"])
        self.assertFalse(run["can_resume"])

    def test_start_page_retry_settles_residue_debt(self):
        from playwright.sync_api import expect
        rid = self.open_round()
        with closing(connect(self.db_path)) as conn:
            db = Database(conn)
            rounds.finish(db, rounds.load(db, rid), rounds.TerminalReason.ABANDONED)
        self.record_dead_identity(rid, state="OWNED", browser_pid=9201,
                                  browser_os_started="browser-proof", port=9222)
        page = self.page()

        expect(page.locator("#startBtn")).to_be_disabled()
        expect(page.locator("#residueCard")).to_be_visible()
        expect(page.locator("#residueInfo")).to_contain_text("收尾")
        with self.gone(4321), \
                self.proof({9201: "browser-proof"}), \
                patch("bestseller_monitor.browser_proc.close_browser", return_value=None):
            page.locator("#residueBtn").click()
            expect(page.locator("#confirmSub")).to_contain_text("不会误关日常 Edge")
            page.locator("#confirmOk").click()
            expect(page.locator("#apiError")).to_be_visible(timeout=8000)
            expect(page.locator("#residueCard")).to_be_visible()
        with self.gone(4321), \
                self.proof({9201: "unrelated-new-browser"}), \
                patch("bestseller_monitor.browser_proc.close_browser", return_value=None):
            page.locator("#residueBtn").click()
            page.locator("#confirmOk").click()
            expect(page.locator("#residueCard")).to_be_hidden(timeout=8000)
            expect(page.locator("#startBtn")).to_be_enabled()


if __name__ == "__main__":
    unittest.main()
