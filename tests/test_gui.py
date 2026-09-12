import logging
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gui
from bestseller_monitor import browser_proc, rounds, single_instance
from bestseller_monitor.config import Shop
from bestseller_monitor.db import CST, Database, connect, cst_date, DETAIL_BUDGET_NOTE
from bestseller_monitor.rounds import RoundRequest, ShopScope, TerminalReason
from gui import Api
from helpers import SNAPSHOT_DEDUPE_MARK, isolated_locks, new_round, traced_connections
from tools import bench_refresh


class GuiWindowHeightTests(unittest.TestCase):
    def test_uses_preferred_height_on_tall_screen(self):
        with patch("gui._screen_work_height", return_value=2160):
            self.assertEqual(gui._default_window_height(), 1083)

    def test_shrinks_to_fit_short_screen(self):
        with patch("gui._screen_work_height", return_value=1000):
            self.assertEqual(gui._default_window_height(), 940)

    def test_falls_back_to_preferred_height_without_screen_info(self):
        with patch("gui._screen_work_height", return_value=None):
            self.assertEqual(gui._default_window_height(), 1083)

    def test_stays_at_min_height_on_tiny_screen(self):
        with patch("gui._screen_work_height", return_value=600):
            self.assertEqual(gui._default_window_height(), 720)


class GuiCrawlerPythonTests(unittest.TestCase):
    """界面由 pythonw 拉起时，采集子进程仍要用带控制台的 python（IS-52 / ADR-0007）。"""

    def test_swaps_pythonw_for_the_python_beside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            pythonw = Path(tmp) / "pythonw.exe"
            pythonw.write_bytes(b"")
            console = Path(tmp) / "python.exe"
            console.write_bytes(b"")

            self.assertEqual(gui._crawler_python(str(pythonw)), str(console))

    def test_keeps_the_interpreter_when_no_console_twin_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            pythonw = Path(tmp) / "pythonw.exe"
            pythonw.write_bytes(b"")

            self.assertEqual(gui._crawler_python(str(pythonw)), str(pythonw))

    def test_plain_python_is_left_alone(self):
        self.assertEqual(
            gui._crawler_python(r"D:\Python\python.exe"), r"D:\Python\python.exe")


class GuiResultTests(unittest.TestCase):
    def test_detail_budget_exhausted_reports_its_own_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            try:
                db = Database(conn)
                round_id = new_round(db)
                rounds.finish(db, rounds.load(db, round_id),
                              TerminalReason.DETAIL_BUDGET_EXHAUSTED, note=DETAIL_BUDGET_NOTE)
                api = Api.__new__(Api)
                api._lock = RLock()
                api._stop = None
                api.round_id = round_id
                api._open_conn = lambda: conn

                result = api.get_result()

                self.assertEqual(result["reason"], "DETAIL_BUDGET_EXHAUSTED")
                self.assertEqual(result["tag"], "预算耗尽")
                self.assertIn("详情预算", result["note"])
                self.assertNotIn("deny", result["note"])
            finally:
                conn.close()

    def test_resumable_interruption_uses_current_elapsed_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            try:
                round_id = new_round(Database(conn))
                api = Api.__new__(Api)
                api._lock = RLock()
                api._stop = None
                api.round_id = round_id
                api._current_elapsed = MagicMock(return_value=125.0)
                api._open_conn = lambda: conn

                result = api.get_result()

                self.assertIsNone(result["reason"])
                self.assertEqual(result["tag"], "进行中")
                self.assertEqual(result["duration_text"], "2 分")
                api._current_elapsed.assert_called_once_with()
            finally:
                conn.close()

    def test_every_terminal_reason_has_result_text(self):
        for reason in TerminalReason:
            with self.subTest(reason=reason):
                tag, note = gui._terminal_text(reason)
                self.assertTrue(tag)
                self.assertTrue(note)

        self.assertEqual(gui._terminal_text(None), ("进行中", "本轮仍在进行；未抓取店铺见下方。"))


class GuiStopRequestTests(unittest.TestCase):
    """暂停／中止都先请求协作停止，窗口内没停下才强杀（ADR-0009）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        # 锁名字按用例隔离：这些用例要看「有没有采集在跑」，那是机器级锁——本机真跑着
        # 界面/采集时，用例会莫名其妙地连库、撞上「已有任务在运行」。
        self.enterContext(isolated_locks())
        self.api = self._api()

    def _api(self, start_browser=True, attach_port=9222):
        api = Api.__new__(Api)
        api._lock = RLock()
        api.proc = None
        api.round_id = None
        api.user_paused = False
        api._elapsed_base = 0.0
        api._run_start_ts = None
        api._stop = None
        api.shops = [Shop("A01", "店铺A", "https://A01.example/")]
        api.cfg = SimpleNamespace(max_pages_per_shop=3, start_browser=start_browser,
                                  attach_port=attach_port)
        api._open_conn = lambda: connect(self.db_path)
        return api

    def _running_crawler(self, pid: int = 6104) -> tuple[int, str]:
        """造出「本界面拉起的采集进程在跑」：占住会话锁 + 身份行 + 活着的子进程句柄。"""
        conn = connect(self.db_path)
        try:
            db = Database(conn)
            rid = new_round(db, "A01")
            started_at = db.record_crawler_process(pid=pid, round_id=rid, note="run.py")
        finally:
            conn.close()
        self.lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(self.lock.release)
        proc = MagicMock()
        proc.poll.return_value = None
        self.api.proc = proc
        self.api.round_id = rid
        return rid, started_at

    def _dies(self):
        """「强杀」真的生效：内核释放会话锁，子进程变成已退出。"""
        def kill():
            self.lock.release()
            self.api.proc.poll.return_value = 1
        return kill

    def _request_row(self):
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM stop_requests WHERE id=1").fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def _acknowledge(self) -> None:
        conn = connect(self.db_path)
        try:
            db = Database(conn)
            request = db.stop_request()
            db.ack_stop_request(target_pid=request["target_pid"],
                                target_started_at=request["target_started_at"])
        finally:
            conn.close()

    def test_pause_writes_a_stop_request_instead_of_killing(self):
        rid, started_at = self._running_crawler()

        with patch.object(Api, "_kill_proc") as kill_proc, \
                patch.object(browser_proc, "close_browser") as close:
            result = self.api.pause_run()

        kill_proc.assert_not_called()
        close.assert_not_called()
        self.assertEqual(result["stopping"], "stopping")
        request = self._request_row()
        self.assertEqual(request["kind"], "pause")
        self.assertEqual(request["round_id"], rid)
        self.assertEqual(request["target_pid"], 6104)
        self.assertEqual(request["target_started_at"], started_at,
                         "目标身份是 (PID, 启动时刻)：新起的进程不该被旧请求影响")
        self.assertIsNone(request["ack_at"], "还没人认领")
        self.assertTrue(self.api.user_paused)

    def test_pause_deadline_forces_the_stop_and_closes_the_browser(self):
        self._running_crawler()

        with patch.object(Api, "_kill_proc", side_effect=self._dies()) as kill_proc, \
                patch.object(browser_proc, "close_browser", return_value=6104) as close:
            self.api.pause_run()
            self.api._stop["deadline"] = time.time() - 1
            self.api.get_run()      # 轮询驱动兜底，不新增后台线程

        kill_proc.assert_called_once_with()
        close.assert_called_once_with(9222, launched_by_us=True)
        self.assertIsNone(self.api._stop)
        self.assertIsNone(self._request_row(), "停下之后不该留着停止请求")

    def test_pause_acknowledgement_widens_the_window_and_reports_closing(self):
        self._running_crawler()

        with patch.object(Api, "_kill_proc") as kill_proc, \
                patch.object(browser_proc, "close_browser"):
            self.api.pause_run()
            self._acknowledge()
            self.api._stop["deadline"] = time.time() - 1   # 原窗口已经到点
            data = self.api.get_run()

        kill_proc.assert_not_called()
        self.assertEqual(data["stopping"], "closing", "回执之后是在收尾，不是在等响应")

    def test_pause_keeps_user_browser_when_attaching(self):
        self.api = self._api(start_browser=False)
        self._running_crawler()

        with patch.object(Api, "_kill_proc", side_effect=self._dies()), \
                patch.object(browser_proc, "close_browser") as close:
            self.api.pause_run()
            self.api._stop["deadline"] = time.time() - 1
            self.api.get_run()

        close.assert_not_called()

    def test_pause_without_an_identity_ends_the_process_directly(self):
        """子进程刚拉起、身份行还没登记：没有可认领的目标，只能直接结束它。"""
        proc = MagicMock()
        proc.poll.return_value = None
        self.api.proc = proc

        with patch.object(Api, "_kill_proc") as kill_proc, \
                patch.object(browser_proc, "close_browser", return_value=1) as close:
            result = self.api.pause_run()

        kill_proc.assert_called_once_with()
        close.assert_called_once_with(9222, launched_by_us=True)
        self.assertIsNone(self._request_row())
        self.assertNotIn("stopping", result)

    def test_a_leftover_stop_request_does_not_bite_the_next_run(self):
        """旧请求只对写它时那个进程有效：新起的一轮不该被它停掉，也不必专门清它。"""
        rid, started_at = self._running_crawler()
        self.api.proc = None
        self.lock.release()          # 采集进程已经走了，只剩表里那条请求
        conn = connect(self.db_path)
        try:
            Database(conn).request_stop(round_id=rid, kind=gui._STOP_PAUSE,
                                        target_pid=6104, target_started_at=started_at)
        finally:
            conn.close()
        self.api._stop = None

        with patch.object(Api, "_spawn_crawler"):
            result = self.api.start_run(["A01"])

        self.assertTrue(result["ok"], result.get("error"))
        self.assertIsNone(self.api._stop, "新的一轮不该继承上一次的停止状态")
        self.assertIsNotNone(self._request_row(),
                             "惰性请求可以留着：认领按目标进程匹配，撞不上新进程")

    def test_browser_cleanup_waits_for_a_browser_that_starts_late(self):
        """刚启动就被暂停时端口还没监听，收尾要短暂重试。"""
        with patch.object(gui, "_BROWSER_CLOSE_RETRY_SEC", 5.0), \
                patch.object(gui.time, "sleep") as sleep, \
                patch.object(browser_proc, "close_browser",
                             side_effect=[None, 4242]) as close:
            pid = self.api._kill_browser()

        self.assertEqual(pid, 4242)
        self.assertEqual(close.call_count, 2)
        sleep.assert_called_once()


class GuiLoggingTests(unittest.TestCase):
    def test_gui_records_its_own_actions_to_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(logs_dir=Path(tmp))
            handler = gui._configure_gui_logging(cfg)
            try:
                logging.getLogger("bestseller_monitor.browser_proc").warning("暂停后收尾浏览器")
                text = (Path(tmp) / "gui.log").read_text(encoding="utf-8")
            finally:
                logging.getLogger().removeHandler(handler)
                handler.close()

        self.assertIn("暂停后收尾浏览器", text)


class GuiRoundScopeTests(unittest.TestCase):
    """界面勾选店铺与命令行 --limit-shops 是同一套范围语义（工单 02）。"""

    def _api(self, db_path):
        # 锁名字按用例隔离：本机正在跑的界面/采集不该让这些用例莫名其妙地被拒。
        self.enterContext(isolated_locks())
        api = Api.__new__(Api)
        api._lock = RLock()
        api._stop = None
        api.proc = None
        api.round_id = None
        api.start_ts = None
        api.user_paused = False
        api._elapsed_base = 0.0
        api._run_start_ts = None
        api.shops = [
            Shop("A01", "店铺A", "https://A01.example/"),
            Shop("A02", "店铺B", "https://A02.example/"),
        ]
        api.cfg = SimpleNamespace(max_pages_per_shop=3)
        # start_run 会关掉自己开的连接，所以每次都要给一条新的。
        api._open_conn = lambda: connect(db_path)
        return api

    @staticmethod
    def _scope(db_path, round_id):
        conn = connect(db_path)
        try:
            return sorted(
                row["shop_key"] for row in conn.execute(
                    "SELECT shop_key FROM shop_rounds WHERE round_id=?", (round_id,)
                )
            )
        finally:
            conn.close()

    @staticmethod
    def _row(db_path, round_id) -> dict:
        conn = connect(db_path)
        try:
            return dict(conn.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone())
        finally:
            conn.close()

    @staticmethod
    def _stale_round(db_path, keys) -> int:
        """昨天开始、还在「进行中」的轮次：跨日之后它不该再被续跑。"""
        yesterday = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")
        conn = connect(db_path)
        try:
            return rounds.open(Database(conn), RoundRequest(
                yesterday,
                tuple(ShopScope(k, f"https://{k}.example/", f"店铺{k}") for k in keys),
            )).round.id
        finally:
            conn.close()

    def test_start_run_creates_round_with_checked_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)

            with patch.object(Api, "_spawn_crawler") as spawn:
                result = api.start_run(["A02"])

            self.assertTrue(result["ok"])
            # 轮次由采集子进程创建：界面只传范围，启动失败不会留下空轮次挡住下一次。
            conn = connect(db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 0)
            finally:
                conn.close()
            spawn.assert_called_once_with(["A02"])

    def test_start_run_reports_scope_mismatch_instead_of_merging(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)
            conn = connect(db_path)
            try:
                rid = rounds.open(Database(conn), RoundRequest(
                    cst_date(), (ShopScope("A01", "https://A01.example/", "店铺A"),),
                )).round.id
            finally:
                conn.close()

            with patch.object(Api, "_spawn_crawler") as spawn:
                self.assertTrue(api.start_run(["A01"])["ok"])
                result = api.start_run(["A02"])

            self.assertFalse(result["ok"])
            self.assertIn("范围", result["error"])
            spawn.assert_called_once_with(["A01"])
            conn = connect(db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 1)
                self.assertEqual(self._scope(db_path, rid), ["A01"])
            finally:
                conn.close()

    def test_start_page_reports_a_stale_round_without_touching_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)
            stale = self._stale_round(db_path, ["A01"])

            start = api.get_start()

            self.assertIn("跨天", start["start_hint"])
            self.assertIn(f"#{stale}", start["start_hint"])
            self.assertEqual(self._row(db_path, stale)["terminal_reason"], None)  # 浏览不改数据

    def test_resume_refuses_a_round_from_a_previous_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)
            self._stale_round(db_path, ["A01"])

            with patch.object(Api, "_spawn_crawler") as spawn:
                result = api.resume_run()

            self.assertFalse(result["ok"])
            self.assertIn("跨天", result["error"])
            spawn.assert_not_called()

    def test_resume_continues_todays_round_without_restating_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)
            conn = connect(db_path)
            try:
                rid = rounds.open(Database(conn), RoundRequest(
                    cst_date(), (ShopScope("A01", "https://A01.example/", "店铺A"),),
                )).round.id
            finally:
                conn.close()

            with patch.object(Api, "_spawn_crawler") as spawn:
                result = api.resume_run()

            self.assertTrue(result["ok"])
            self.assertEqual(result["round_id"], rid)
            spawn.assert_called_once_with()


class _FakeWindow:
    """pywebview 窗口的最小替身：只为拿到关窗回调。"""

    def __init__(self):
        self.closing_handlers = []

    @property
    def events(self):
        return SimpleNamespace(closing=self)

    def __iadd__(self, handler):
        self.closing_handlers.append(handler)
        return self


class GuiSingleInstanceTests(unittest.TestCase):
    """界面单实例：已经开着一个时不再建第二个窗口（工单 01）。"""

    def test_second_launch_does_not_open_a_window(self):
        with patch.object(single_instance, "acquire", return_value=None) as acquire, \
                patch.object(gui, "Api"), \
                patch.object(gui.webview, "create_window") as create, \
                patch.object(gui, "_announce_already_open") as announce:
            code = gui.main()

        acquire.assert_called_once_with(single_instance.GUI_LOCK)
        create.assert_not_called()
        announce.assert_called_once_with()
        self.assertEqual(code, 0, "第二个实例要安静退出，别让启动壳弹错误框")

    def test_first_launch_holds_the_interface_lock_and_releases_it(self):
        lock = MagicMock()
        window = _FakeWindow()
        with patch.object(single_instance, "acquire", return_value=lock), \
                patch.object(gui, "Api"), \
                patch.object(gui, "_configure_gui_logging"), \
                patch.object(gui.webview, "create_window", return_value=window), \
                patch.object(gui.webview, "start"):
            code = gui.main()

        self.assertEqual(code, 0)
        lock.release.assert_called_once_with()
        self.assertEqual(len(window.closing_handlers), 1, "关窗要接上告知回调")

    def test_already_open_notice_points_at_the_existing_window(self):
        with patch.object(gui, "_focus_existing_window", return_value=False) as focus, \
                patch.object(gui, "_notify") as notify:
            gui._announce_already_open()

        focus.assert_called_once_with(gui.WINDOW_TITLE)
        self.assertIn("已经打开", notify.call_args.args[0])

    def test_closing_warns_that_the_crawler_keeps_running(self):
        api = SimpleNamespace(any_crawler_running=lambda: True)
        with patch.object(gui, "_notify") as notify:
            allowed = gui._warn_crawler_keeps_running(api)

        self.assertTrue(allowed, "告知归告知，关窗不该被阻断")
        self.assertIn("继续", notify.call_args.args[0])

    def test_closing_says_nothing_when_nothing_is_running(self):
        api = SimpleNamespace(any_crawler_running=lambda: False)
        with patch.object(gui, "_notify") as notify, \
                self.assertLogs("gui", level="INFO") as logged:
            allowed = gui._warn_crawler_keeps_running(api)

        self.assertTrue(allowed)
        notify.assert_not_called()
        self.assertIn("没有采集在跑", "\n".join(logged.output), "关窗动作要留底")


class GuiCrawlerMutexTests(unittest.TestCase):
    """界面与命令行共用一把采集锁：别处有采集在跑时，这里不许再起一个（工单 02）。"""

    def _api(self, db_path):
        self.enterContext(isolated_locks())
        api = Api.__new__(Api)
        api._lock = RLock()
        api._stop = None
        api.proc = None
        api.round_id = None
        api.start_ts = None
        api.user_paused = False
        api._elapsed_base = 0.0
        api._run_start_ts = None
        api.shops = [Shop("A01", "店铺A", "https://A01.example/")]
        api.cfg = SimpleNamespace(max_pages_per_shop=3)
        api._open_conn = lambda: connect(db_path)
        return api

    @staticmethod
    def _identity_row(db_path):
        conn = connect(db_path)
        try:
            row = conn.execute("SELECT * FROM crawler_process WHERE id=1").fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def test_start_run_is_refused_while_another_crawler_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = self._api(Path(tmp) / "test.db")
            holding = single_instance.acquire(single_instance.CRAWLER_LOCK)
            try:
                with patch.object(Api, "_spawn_crawler") as spawn:
                    result = api.start_run(["A01"])
            finally:
                holding.release()

            self.assertFalse(result["ok"])
            self.assertIn("已有抓取任务在运行", result["error"])
            spawn.assert_not_called()

    def test_start_page_names_the_running_crawler(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)
            conn = connect(db_path)
            try:
                rid = new_round(Database(conn))
                Database(conn).record_crawler_process(pid=4321, round_id=rid, note="run.py")
            finally:
                conn.close()

            holding = single_instance.acquire(single_instance.CRAWLER_LOCK)
            try:
                hint = api.get_start()["start_hint"]
            finally:
                holding.release()

            self.assertIn(f"#{rid}", hint, "要说清跑的是哪一轮")
            self.assertIn("4321", hint, "要说清是哪个进程")

    def test_a_stale_identity_row_is_ignored_and_cleaned(self):
        """被强杀的采集会留下身份行：锁不在就不该报「有任务在跑」，顺手清掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            api = self._api(db_path)
            conn = connect(db_path)
            try:
                rid = new_round(Database(conn))
                Database(conn).record_crawler_process(pid=4321, round_id=rid, note="run.py")
            finally:
                conn.close()

            hint = api.get_start()["start_hint"]

            self.assertNotIn("4321", hint)
            self.assertNotIn("采集进程正在跑", hint)
            self.assertIsNone(self._identity_row(db_path), "残留身份行要清掉")

    def test_a_refused_child_reports_its_reason_instead_of_a_result_page(self):
        api = Api.__new__(Api)
        api._lock = RLock()
        api._stop = None
        api.proc = SimpleNamespace(poll=lambda: single_instance.CRAWLER_BUSY_EXIT_CODE)
        api.round_id = None
        api.user_paused = False

        data = api.get_run()

        self.assertFalse(data["has_round"], "被拒绝的启动不是一轮结束")
        self.assertIn("已有采集进程在运行", data["start_error"])


class GuiCrossSessionAbortTests(unittest.TestCase):
    """跨会话中止：命令行起的、或上一次界面留下的采集，也能从这里停下来（工单 03）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.enterContext(isolated_locks())
        self.api = Api.__new__(Api)
        self.api._lock = RLock()
        self.api.proc = None
        self.api.round_id = None
        self.api.user_paused = False
        self.api._stop = None
        self.api.shops = [Shop("A01", "店铺A", "https://A01.example/")]
        # start_browser=False：中止只收尾采集进程，不去动用户的浏览器（那一路由 IS-43 的测试盯）。
        self.api.cfg = SimpleNamespace(max_pages_per_shop=3, start_browser=False)
        self.api._open_conn = lambda: connect(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _running_crawler(self, pid: int = 4321) -> int:
        """造出「别处有一个采集进程在跑」：抢住采集锁 + 写身份行。"""
        conn = connect(self.db_path)
        try:
            db = Database(conn)
            rid = new_round(db)
            db.record_crawler_process(pid=pid, round_id=rid, note="run.py")
        finally:
            conn.close()
        self.lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
        self.addCleanup(self.lock.release)
        return rid

    def _terminal_of(self, round_id: int):
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT terminal_reason FROM rounds WHERE id=?",
                               (round_id,)).fetchone()
            return row[0] if row is not None else None
        finally:
            conn.close()

    def _row(self, column: str, table: str = "rounds"):
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                f"SELECT {column} FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
            return row[0] if row is not None else None
        finally:
            conn.close()

    def test_abort_writes_the_terminal_state_and_then_stops_the_process(self):
        rid = self._running_crawler()
        seen = {}

        def kill(pid):
            seen["reason_at_kill_time"] = self._row("terminal_reason")
            seen["pid"] = pid
            self.lock.release()   # 进程被杀掉 → 内核释放会话锁
            return True

        with patch.object(browser_proc, "process_image_name", return_value="python.exe"), \
                patch.object(browser_proc, "terminate_process_tree", side_effect=kill):
            result = self.api.abort_run()
            self.assertEqual(self._row("terminal_reason"), "ABANDONED",
                             "先写终态：它本身就是停止信号（ADR-0009）")
            self.assertEqual(seen, {}, "窗口还没到，不许先杀")
            self.api._stop["deadline"] = time.time() - 1
            self.api.get_start()      # 轮询驱动兜底

        self.assertTrue(result["ok"])
        self.assertEqual(result["stopping"], "stopping")
        self.assertEqual(seen["pid"], 4321, "停下来的是身份行里那个进程")
        self.assertEqual(seen["reason_at_kill_time"], "ABANDONED",
                         "强杀发生在终态写好之后")
        self.assertEqual(self._row("terminal_reason"), "ABANDONED")
        self.assertEqual(self._row("note"), "GUI 人工中止（放弃）")
        self.assertIsNone(self._row("pid", "crawler_process"), "身份行要清掉")

    def test_abort_without_a_usable_pid_only_writes_the_terminal_state(self):
        """PID 已经不在了：只写终态，采集进程会在下一个检查点自己停下。"""
        self._running_crawler()

        with patch.object(browser_proc, "process_image_name", return_value=""), \
                patch.object(browser_proc, "terminate_process_tree") as kill:
            result = self.api.abort_run()
            self.api._stop["deadline"] = time.time() - 1
            self.api.get_start()

        self.assertTrue(result["ok"])
        kill.assert_not_called()
        self.assertEqual(self._row("terminal_reason"), "ABANDONED")

    def test_abort_keeps_the_identity_while_the_crawler_may_still_be_alive(self):
        """停不掉（拿不到可用 PID）时身份行要留着：下次还得知道是谁在跑。"""
        self._running_crawler()

        with patch.object(browser_proc, "process_image_name", return_value=""), \
                patch.object(browser_proc, "terminate_process_tree") as kill:
            self.api.abort_run()
            self.api._stop["deadline"] = time.time() - 1
            self.api.get_start()

        kill.assert_not_called()
        self.assertEqual(self._row("pid", "crawler_process"), 4321,
                         "进程可能还在跑，身份行不能先清掉")

    def test_abort_never_kills_a_pid_that_is_no_longer_our_crawler(self):
        """PID 会被系统回收：镜像名不是 python 就不许动它。"""
        self._running_crawler()

        with patch.object(browser_proc, "process_image_name", return_value="msedge.exe"), \
                patch.object(browser_proc, "terminate_process_tree") as kill:
            result = self.api.abort_run()
            self.api._stop["deadline"] = time.time() - 1
            self.api.get_start()

        self.assertTrue(result["ok"])
        kill.assert_not_called()
        self.assertEqual(self._row("terminal_reason"), "ABANDONED")

    def test_abort_keeps_an_earlier_terminal_state(self):
        """轮次刚好已经结束了：不改写终态，也不报错。"""
        rid = self._running_crawler()
        conn = connect(self.db_path)
        try:
            db = Database(conn)
            rounds.finish(db, rounds.load(db, rid), TerminalReason.COMPLETED)
        finally:
            conn.close()

        with patch.object(browser_proc, "process_image_name", return_value="python.exe"), \
                patch.object(browser_proc, "terminate_process_tree", return_value=True):
            result = self.api.abort_run()

        self.assertTrue(result["ok"])
        self.assertEqual(self._row("terminal_reason"), "COMPLETED")

    def test_start_page_exposes_the_running_crawler_for_the_abort_entry(self):
        rid = self._running_crawler()

        start = self.api.get_start()

        self.assertEqual(start["crawler"]["pid"], 4321)
        self.assertEqual(start["crawler"]["round_id"], rid)

    def test_abort_prefers_the_round_that_is_actually_running(self):
        """界面记着的轮次可能早就结束了：中止要收尾正在跑的那一轮，不能按旧编号写。"""
        conn = connect(self.db_path)
        try:
            db = Database(conn)
            old = new_round(db)
            rounds.finish(db, rounds.load(db, old), TerminalReason.COMPLETED)
        finally:
            conn.close()
        self.api.round_id = old                 # 界面还记着已经完结的那一轮
        running = self._running_crawler()       # 别处正在跑的是另一轮

        with patch.object(browser_proc, "process_image_name", return_value="python.exe"), \
                patch.object(browser_proc, "terminate_process_tree", return_value=True):
            self.api.abort_run()

        self.assertNotEqual(running, old)
        self.assertEqual(self._terminal_of(running), "ABANDONED",
                         "正在跑的那一轮才该被收尾")
        self.assertEqual(self._terminal_of(old), "COMPLETED", "已经完结的轮次不许改写")

    def test_start_page_has_no_abort_entry_when_nothing_runs(self):
        start = self.api.get_start()

        self.assertIsNone(start["crawler"])


class GuiConnectionTests(unittest.TestCase):
    """界面自己的连接也要走迁移，否则旧结构的库会让三个页面一起报错（IS-37）。"""

    @staticmethod
    def _legacy_db(db_path):
        raw = sqlite3.connect(str(db_path))
        raw.execute(
            "CREATE TABLE rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "started_at TEXT NOT NULL, finished_at TEXT, "
            "status TEXT NOT NULL DEFAULT '进行中', phase TEXT NOT NULL DEFAULT 'listing', note TEXT)"
        )
        raw.execute(
            "INSERT INTO rounds(started_at, status, phase, note) "
            "VALUES ('2026-09-08T15:13:11+00:00', '完成', 'done', NULL)"
        )
        raw.commit()
        raw.close()

    def test_opens_a_legacy_database_and_migrates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            self._legacy_db(db_path)
            api = Api.__new__(Api)
            api._lock = RLock()
            api._stop = None
            api.proc = None
            api.round_id = None
            api.user_paused = False
            api._elapsed_base = 0.0
            api._run_start_ts = None
            api.shops = [Shop("A01", "店铺A01", "https://A01.example/")]
            api.cfg = SimpleNamespace(db_file=db_path, max_pages_per_shop=3)

            # 三个页面都不再抛 no such column
            start = api.get_start()
            self.assertFalse(start["summary"]["started"])  # 那一轮是 9/8 的
            self.assertFalse(api.get_run()["has_round"])
            self.assertTrue(api.get_result()["has_round"])

            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                columns = {r[1] for r in conn.execute('PRAGMA table_info("rounds")')}
                round_row = conn.execute("SELECT * FROM rounds WHERE id=1").fetchone()
            finally:
                conn.close()
            self.assertNotIn("status", columns)
            self.assertEqual(dict(round_row)["run_date"], "2026-09-08")
            self.assertEqual(dict(round_row)["terminal_reason"], "COMPLETED")


class GuiRefreshCostTests(unittest.TestCase):
    """IS-38：界面每 2 秒刷新一次，刷新路径不能付「随库增长」的全表成本。

    合成 12 家店的大盘库，走真实 cfg + 真实 `_open_conn`（整表去重就在 connect() 里），
    断言两件事：这次刷新没有整表语句，且耗时不至于离谱。更大规模的耗时复现在
    `tools/bench_refresh.py`（默认 30 万行，可 `--rows-per-shop` 继续放大）。

    耗时上界只是护栏，不是性能目标：这台机器、30 万行、WAL 库上一次刷新实测约 0.09 秒
    （补事件索引之前是 0.80 秒），所以 1 秒的界有十倍余量。
    """

    SHOPS = 12
    ROWS_PER_SHOP = 25_000      # 12 店 × 2.5 万 = 30 万快照 + 30 万事件
    REFRESH_LIMIT_SEC = 1.0

    def test_refresh_on_a_large_database_does_not_scan_the_snapshot_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.db"
            conn = connect(path)
            try:
                rid, _ = bench_refresh.build_dataset(conn, self.SHOPS, self.ROWS_PER_SHOP)
            finally:
                conn.close()

            api = Api.__new__(Api)
            api._lock = RLock()
            api._stop = None
            api.proc = None
            api.round_id = None
            api.user_paused = False
            api._elapsed_base = 0.0
            api._run_start_ts = None
            api.cfg = SimpleNamespace(db_file=path)

            seen: list[str] = []
            with traced_connections(seen):
                started = time.perf_counter()
                run = api.get_run()
                elapsed = time.perf_counter() - started

            self.assertEqual(run["round_id"], rid)
            self.assertEqual(run["done_count"], self.SHOPS, "12 家店的指标都要算出来")
            self.assertGreater(run["done"][0]["skus"], 0)
            self.assertEqual(
                [sql for sql in seen if SNAPSHOT_DEDUPE_MARK in sql], [],
                "刷新一次不该扫整表：唯一索引已在，重复行不可能写进来",
            )
            self.assertLess(elapsed, self.REFRESH_LIMIT_SEC,
                            f"一次刷新耗时离谱，实测 {elapsed:.3f} 秒")


if __name__ == "__main__":
    unittest.main()
