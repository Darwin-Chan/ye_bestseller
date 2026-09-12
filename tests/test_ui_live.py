"""界面错误反馈：API 调用失败必须在页面上看得见（IS-37）。

pywebview 把 Python 异常变成 rejected promise，界面原先对 `await api.xxx()` 不设防，
异常就此消失在页面里——按钮看起来像"点了没反应"（2026-09-12 运行库实例：
exe 的入口脚本调用已删除的 Database.start_or_resume，点击后界面毫无动静）。

这里用 mock 的 pywebview 桥驱动真实的 docs/ui_live.html，锁住三件事：
失败要显示、失败后可重试、成功路径不受影响。
"""
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

HTML_URI = (Path(__file__).resolve().parent.parent / "docs" / "ui_live.html").as_uri()

MOCK_API = r"""
window.__calls = [];
const SHOPS = [
  {key:"A01", name:"店铺A", products:180, skus:1038, pages:23, default_checked:true},
  {key:"A02", name:"店铺B", products:0, skus:0, pages:2, default_checked:true},
];
function boom(name) {
  return new Error(window.__failMessage || (name + " 失败"));
}
window.pywebview = { platform: "edgechromium", api: {
  get_start: async () => {
    window.__calls.push("get_start");
    if (window.__fail === "get_start") throw boom("get_start");
    return {
      ov: {products: 180, skus: 1038},
      summary: {started: true, rounds: 1, text: "00:29 开始 · 跑约 20 分"},
      shops: SHOPS, total_shops: 2, start_hint: window.__crawler ? "采集进程正在跑" : "",
      crawler: window.__crawler || null,
    };
  },
  get_run: async () => {
    window.__calls.push("get_run");
    if (window.__fail === "get_run") throw boom("get_run");
    if (window.__startError) {
      return {running: false, manually_paused: false, has_round: false,
              start_error: window.__startError};
    }
    return {running: false, manually_paused: false, has_round: false};
  },
  get_result: async () => {
    window.__calls.push("get_result");
    if (window.__fail === "get_result") throw boom("get_result");
    return {has_round: false};
  },
  start_run: async (keys) => {
    window.__calls.push(["start_run", keys]);
    if (window.__fail === "start_run") throw boom("start_run");
    return {ok: true};
  },
  pause_run: async () => ({ok: true}),
  resume_run: async () => ({ok: true}),
  abort_run: async () => { window.__calls.push("abort_run"); return {ok: true}; },
}};
"""

ERROR_BOX = "#apiError"

# 轮询用桥：get_run 卡住直到测试放行，用来模拟「查询变慢」；返回值按调用那一刻的
# 状态快照构造，这样「复用在途结果」与「重新取一次」在页面上看得出区别。
POLL_MOCK_API = r"""
window.__calls = [];
window.__gate = false;
window.__paused = false;
window.__hold = null;
window.__release = () => { const r = window.__hold; window.__hold = null; if (r) r(); };
window.pywebview = { platform: "edgechromium", api: {
  get_start: async () => {
    window.__calls.push("get_start");
    return {
      ov: {products: 0, skus: 0},
      summary: {started: false, rounds: 0, text: ""},
      shops: [{key: "A01", name: "店铺A", products: 0, skus: 0, pages: 1,
               default_checked: true}],
      total_shops: 1, start_hint: "", crawler: null,
    };
  },
  get_run: async () => {
    window.__calls.push("get_run");
    const pausedAtCall = window.__paused;
    if (window.__gate) { await new Promise(resolve => { window.__hold = resolve; }); }
    return {
      running: true, manually_paused: pausedAtCall, has_round: true, round_id: 7,
      started_hhmm: "10:00", elapsed_sec: 5, deny: 0, done_count: 0, total_count: 1,
      progress: 0, current_shop: "A01", done: [], todo: [{key: "A01", name: "店铺A"}],
    };
  },
  get_result: async () => { window.__calls.push("get_result"); return {has_round: false}; },
  start_run: async (keys) => { window.__calls.push("start_run"); return {ok: true}; },
  pause_run: async () => { window.__calls.push("pause_run"); window.__paused = true;
                           return {ok: true}; },
  resume_run: async () => { window.__calls.push("resume_run"); return {ok: true}; },
  abort_run: async () => { window.__calls.push("abort_run"); return {ok: true}; },
}};
"""


class UiLiveErrorFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        try:
            cls.browser = cls._pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:  # noqa: BLE001
            cls._pw.stop()
            cls._pw = None
            raise unittest.SkipTest(f"没有可用的 Edge/Playwright 浏览器：{exc}")

    @classmethod
    def tearDownClass(cls):
        if cls._pw is not None:
            cls.browser.close()
            cls._pw.stop()

    def open_page(self, fail_on: str | None = None, message: str | None = None,
                  start_error: str | None = None, crawler: dict | None = None):
        """加载真实页面，注入一个会按需抛错、按需拒绝启动的 pywebview 桥。"""
        page = self.browser.new_page(viewport={"width": 1100, "height": 1000})
        if fail_on:
            page.add_init_script(f"window.__fail = {fail_on!r};")
        if message:
            page.add_init_script(f"window.__failMessage = {message!r};")
        if start_error:
            page.add_init_script(f"window.__startError = {start_error!r};")
        if crawler is not None:
            page.add_init_script(f"window.__crawler = {crawler!r};")
        page.add_init_script(MOCK_API)
        page.goto(HTML_URI)
        return page

    def set_fail(self, page, fail_on: str | None):
        page.evaluate("v => { window.__fail = v; }", fail_on)

    def active_tab(self, page) -> str:
        return page.eval_on_selector(".step.active .lbl", "el => el.textContent")

    def test_start_page_query_failure_is_shown_on_the_page(self):
        """首页查询失败：用户要看得到原因，而不是一堆 0 和空表。"""
        page = self.open_page(fail_on="get_start", message="no such column: run_date")
        try:
            self.assertEqual(page.locator(ERROR_BOX).count(), 1, "页面缺少错误提示容器")
            page.wait_for_selector(ERROR_BOX, state="visible", timeout=5000)
            self.assertIn("no such column: run_date", page.inner_text(ERROR_BOX))
        finally:
            page.close()

    def test_start_run_failure_is_shown_instead_of_doing_nothing(self):
        """点「开始抓取」失败：这是本次事故的现场，不能再静默。"""
        page = self.open_page(
            fail_on="start_run",
            message="'Database' object has no attribute 'start_or_resume'",
        )
        try:
            page.click("#startBtn")
            page.wait_for_selector(ERROR_BOX, state="visible", timeout=5000)

            self.assertEqual(
                page.evaluate("() => window.__calls.filter(c => Array.isArray(c)).length"), 1,
                "start_run 应该被调用一次")
            self.assertIn("start_or_resume", page.inner_text(ERROR_BOX))
            self.assertEqual(self.active_tab(page), "开始", "失败时不该跳到过程页")
        finally:
            page.close()

    def test_failure_can_be_retried_and_the_message_clears(self):
        page = self.open_page(fail_on="start_run", message="临时失败")
        try:
            page.click("#startBtn")
            page.wait_for_selector(ERROR_BOX, state="visible", timeout=5000)

            self.set_fail(page, None)
            page.click("#startBtn")
            page.wait_for_selector('.step.active[data-tab="run"]', timeout=5000)
            page.wait_for_selector(ERROR_BOX, state="hidden", timeout=5000)
            self.assertFalse(page.is_visible(ERROR_BOX), "成功之后错误提示要收起来")
        finally:
            page.close()

    def test_polling_failure_is_shown_and_recovers(self):
        """过程页轮询失败同样要露面，并且恢复后自动收起来。"""
        page = self.open_page()
        try:
            page.click("#startBtn")
            page.wait_for_selector('.step.active[data-tab="run"]', timeout=5000)

            self.set_fail(page, "get_run")
            page.evaluate("() => tickRun()")
            page.wait_for_selector(ERROR_BOX, state="visible", timeout=5000)

            self.set_fail(page, None)
            page.evaluate("() => tickRun()")
            page.wait_for_selector(ERROR_BOX, state="hidden", timeout=5000)
        finally:
            page.close()

    def test_successful_start_page_has_no_error(self):
        page = self.open_page()
        try:
            page.wait_for_selector("#rows input", timeout=5000)
            self.assertFalse(page.is_visible(ERROR_BOX))
            self.assertEqual(page.eval_on_selector_all("#rows input", "els => els.length"), 2)
        finally:
            page.close()

    def test_start_page_offers_the_abort_entry_for_a_running_crawler(self):
        """有采集在跑（可能是命令行起的）：启动页给中止入口，点了真的调 abort_run（工单 03）。"""
        page = self.open_page(crawler={"pid": 4321, "round_id": 7,
                                       "started_at": "2026-09-12T01:12:00+00:00"})
        try:
            page.wait_for_selector("#abortForeignBtn", state="visible", timeout=5000)
            self.assertIn("#7", page.inner_text("#abortForeignBtn"), "要说清中止的是哪一轮")

            page.click("#abortForeignBtn")
            page.wait_for_selector("#confirmModal.show", timeout=5000)
            page.click("#confirmOk")
            page.wait_for_function(
                "() => window.__calls.some(c => c === 'abort_run')", timeout=5000)
        finally:
            page.close()

    def test_start_page_hides_the_abort_entry_when_nothing_runs(self):
        page = self.open_page()
        try:
            page.wait_for_selector("#rows input", timeout=5000)
            self.assertFalse(page.is_visible("#abortForeignBtn"))
        finally:
            page.close()

    def test_a_refused_start_shows_the_reason_instead_of_the_result_page(self):
        """子进程被「已有采集在跑」拒绝：页面要说原因，别假装这一轮跑完了（工单 02）。"""
        page = self.open_page(start_error="已有采集进程在运行：本次启动被拒绝了")
        try:
            page.click("#startBtn")
            page.wait_for_selector(ERROR_BOX, state="visible", timeout=5000)

            self.assertIn("已有采集进程在运行", page.inner_text(ERROR_BOX))
            self.assertEqual(self.active_tab(page), "开始", "被拒绝时不该停在结果页")
        finally:
            page.close()


class UiLivePollingTests(unittest.TestCase):
    """界面轮询要防重入（IS-38）。

    原先每 2 秒无条件发一次异步 get_run：查询一慢，请求就堆在控制锁上，暂停/中止要
    排在它们后面。这里锁住两件事——一次刷新没回来之前不再发第二条；手动触发的刷新
    排在在途那条之后、取到的是新数据。
    """

    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        try:
            cls.browser = cls._pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:  # noqa: BLE001
            cls._pw.stop()
            cls._pw = None
            raise unittest.SkipTest(f"没有可用的 Edge/Playwright 浏览器：{exc}")

    @classmethod
    def tearDownClass(cls):
        if cls._pw is not None:
            cls.browser.close()
            cls._pw.stop()

    def open_page(self):
        """加载真实页面，注入一个可卡住 get_run 的桥，并把节拍压到 50 毫秒。"""
        page = self.browser.new_page(viewport={"width": 1100, "height": 1000})
        page.add_init_script(POLL_MOCK_API)
        page.goto(HTML_URI)
        self.assertEqual(page.evaluate("POLL_MS"), 2000, "运行时默认节拍仍是 2 秒")
        page.evaluate("POLL_MS = 50")
        return page

    def calls_of(self, page, name: str) -> int:
        return page.evaluate("n => window.__calls.filter(c => c === n).length", name)

    def start_a_run(self, page):
        page.click("#startBtn")
        page.wait_for_function("window.__calls.includes('get_run')", timeout=5000)

    def test_a_slow_refresh_is_not_started_again_while_it_is_still_running(self):
        page = self.open_page()
        try:
            page.evaluate("window.__gate = true")
            self.start_a_run(page)

            page.wait_for_timeout(500)   # 50 毫秒节拍 × 10：固定节拍会在这里堆 10 条

            self.assertEqual(self.calls_of(page, "get_run"), 1,
                             "上一次刷新还没回来，就不该再发下一条")

            page.evaluate("window.__release()")
            page.wait_for_function(
                "window.__calls.filter(c => c === 'get_run').length === 2", timeout=5000)
        finally:
            page.close()

    def test_a_manual_refresh_waits_then_fetches_new_data_instead_of_reusing_the_snapshot(self):
        page = self.open_page()
        try:
            page.evaluate("window.__gate = true")
            self.start_a_run(page)
            page.click("#pauseBtn")
            page.wait_for_function("window.__calls.includes('pause_run')", timeout=5000)

            self.assertEqual(self.calls_of(page, "get_run"), 1, "在途那条还在跑，先不重入")

            page.evaluate("window.__release()")   # 放行暂停前发出的那条
            page.wait_for_function(
                "window.__calls.filter(c => c === 'get_run').length === 2", timeout=5000)

            page.evaluate("window.__release()")   # 放行排到后面的那条新请求
            # 第 2 条取到的是暂停之后的状态：页面必须显示暂停，而不是复用暂停前的快照
            page.wait_for_selector("#rPaused", state="visible", timeout=5000)
            self.assertEqual(page.evaluate("window.__calls.filter(c => c === 'get_run').length"), 2)
        finally:
            page.close()

    def test_switching_back_to_the_start_page_also_waits_for_the_refresh_in_flight(self):
        """切页签也是手动刷新：在途那条没回来之前，开始页的取数要排队（CONTEXT：在途刷新至多一条）。"""
        page = self.open_page()
        try:
            page.evaluate("window.__gate = true")
            self.start_a_run(page)
            self.assertEqual(self.calls_of(page, "get_start"), 1, "开页面时已经取过一次")

            # 取数要排队时 switchTab() 返回的 promise 会挂到在途那条之后，这里不等它，
            # 只要求「页签切过去了、取数没开第二条」。
            page.evaluate("() => { switchTab('start'); }")
            page.wait_for_timeout(200)

            self.assertEqual(self.calls_of(page, "get_start"), 1,
                             "在途刷新还没回来，切页签不该再开一条取数")
            page.evaluate("window.__release()")
            page.wait_for_function(
                "window.__calls.filter(c => c === 'get_start').length === 2", timeout=5000)
        finally:
            page.close()


# 停止途中：界面要如实说明在等什么，并且停止途中不许再点（ADR-0009）。
STOP_MOCK_API = r"""
window.__calls = [];
window.__stopping = null;
window.__paused = false;
window.__result = {has_round: false};
window.pywebview = { platform: "edgechromium", api: {
  get_start: async () => {
    window.__calls.push("get_start");
    return {
      ov: {products: 0, skus: 0},
      summary: {started: false, rounds: 0, text: ""},
      shops: [{key: "A01", name: "店铺A", products: 0, skus: 0, pages: 1,
               default_checked: true}],
      total_shops: 1, start_hint: "采集进程正在跑", crawler: {round_id: 7, pid: 4321},
      stopping: window.__stopping,
    };
  },
  get_run: async () => {
    window.__calls.push("get_run");
    return {
      running: true, manually_paused: window.__paused, stopping: window.__stopping,
      has_round: true,
      round_id: 7, started_hhmm: "10:00", elapsed_sec: 5, deny: 0, done_count: 0,
      total_count: 1, progress: 0, current_shop: "A01", done: [],
      todo: [{key: "A01", name: "店铺A"}],
    };
  },
  get_result: async () => {
    window.__calls.push("get_result");
    return window.__result;
  },
  start_run: async () => ({ok: true}),
  pause_run: async () => {
    window.__paused = true; window.__stopping = "stopping";
    return {ok: true, stopping: "stopping"};
  },
  resume_run: async () => ({ok: true}),
  abort_run: async () => { window.__calls.push("abort_run"); return {ok: true}; },
}};
"""


class UiLiveStoppingTests(unittest.TestCase):
    """按下暂停之后：横幅说明在等什么，停止途中不给再点（ADR-0009）。"""

    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        try:
            cls.browser = cls._pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:  # noqa: BLE001
            cls._pw.stop()
            cls._pw = None
            raise unittest.SkipTest(f"没有可用的 Edge/Playwright 浏览器：{exc}")

    @classmethod
    def tearDownClass(cls):
        if cls._pw is not None:
            cls.browser.close()
            cls._pw.stop()

    def open_page(self):
        page = self.browser.new_page(viewport={"width": 1100, "height": 1000})
        page.add_init_script(STOP_MOCK_API)
        page.goto(HTML_URI)
        return page

    def start_a_run(self, page):
        page.click("#startBtn")
        page.wait_for_selector('.step.active[data-tab="run"]', timeout=5000)

    def test_pause_shows_what_it_is_waiting_for_and_locks_the_buttons(self):
        page = self.open_page()
        try:
            self.start_a_run(page)
            page.click("#pauseBtn")

            page.wait_for_selector("#rStopping", state="visible", timeout=5000)
            self.assertIn("正在停止", page.inner_text("#rStopping"))
            self.assertFalse(page.is_visible("#rPaused"), "停止途中不该说「已暂停」")
            self.assertFalse(page.is_visible("#pauseBtn"))
            self.assertFalse(page.is_visible("#resumeBtn"))
            self.assertTrue(page.eval_on_selector("#abortBtn", "el => el.disabled"))

            # 采集进程回执之后：同一块横幅变成「正在收尾」，而不是从零重来
            page.evaluate("() => { window.__stopping = 'closing'; }")
            page.evaluate("() => tickRun()")
            page.wait_for_function(
                "() => document.getElementById('rStopping').textContent.includes('收尾')",
                timeout=5000)
        finally:
            page.close()

    def test_result_page_keeps_asking_until_the_crawler_is_gone(self):
        """中止写完终态就回结果页，但采集进程可能还在收尾：接着问，直到它真的走了。"""
        page = self.open_page()
        try:
            page.evaluate("""() => {
              window.__result = {has_round: true, round_id: 7, reason: "ABANDONED",
                started_hhmm: "10:00", finished_hhmm: "10:05", duration_text: "5 分",
                deny: 0, done_count: 0, total_count: 1, products_total: 0, skus_total: 0,
                done: [], todo: [{key: "A01", name: "店铺A"}],
                tag: "人工中止", note: "本轮由人工中止（放弃），已抓取数据已保留。",
                stopping: "stopping"};
              renderResult(window.__result); switchTab("result");
            }""")

            page.wait_for_selector("#resStopping", state="visible", timeout=5000)
            self.assertIn("正在停止", page.inner_text("#resStopping"))
            before = page.evaluate("window.__calls.filter(c => c === 'get_result').length")

            page.evaluate("""() => { window.__result = Object.assign({}, window.__result,
              {stopping: null}); }""")
            page.wait_for_function(
                "() => document.getElementById('resStopping').style.display === 'none'",
                timeout=8000)
            self.assertGreater(page.evaluate(
                "window.__calls.filter(c => c === 'get_result').length"), before,
                "结果页要自己接着问，不能停在这一屏")
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
