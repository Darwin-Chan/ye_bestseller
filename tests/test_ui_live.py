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
      shops: SHOPS, total_shops: 2, start_hint: "",
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
  abort_run: async () => ({ok: true}),
}};
"""

ERROR_BOX = "#apiError"


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
                  start_error: str | None = None):
        """加载真实页面，注入一个会按需抛错、按需拒绝启动的 pywebview 桥。"""
        page = self.browser.new_page(viewport={"width": 1100, "height": 1000})
        if fail_on:
            page.add_init_script(f"window.__fail = {fail_on!r};")
        if message:
            page.add_init_script(f"window.__failMessage = {message!r};")
        if start_error:
            page.add_init_script(f"window.__startError = {start_error!r};")
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


if __name__ == "__main__":
    unittest.main()
