"""分析入口（analyze.py）：运行互斥、劝退、提示路径（票 14）与关窗＝真的停下（票 03）。

同一台机器同一时刻至多一次分析运行——窗口与 `--serve` 共用一把 `ANALYSIS_LOCK`；
抢不到就落日志、前置已有窗口、弹中文提示、退出 0（让分析壳保持安静，照 ADR-0008）。
匹配在跑时关窗先弹原生确认框，「关闭并停止」等这次运行彻底停写（`wait_terminal`
返回）才关窗、才放锁（ADR-0044 决策 4/5）。
真实锁的用例自起一把内核对象：名字按用例隔离，不跟本机真实运行的分析抢同一把
（与 tests/helpers.py 的 isolated_locks 同规）。
"""
import ctypes
import io
import os
import subprocess
import sys
import threading
import time
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import analyze
from bestseller_monitor import single_instance
from bestseller_monitor.analysis import PHASES

ROOT = Path(__file__).resolve().parent.parent


class FakeServer:
    """只带 main() 会用到的四件：端口、serve_forever、关停、关服务。"""

    def __init__(self):
        self.server_port = 43210
        self.served = False
        self.shut = False
        self.closed = False

    def serve_forever(self):
        self.served = True

    def shutdown(self):
        self.shut = True

    def server_close(self):
        self.closed = True


def unique_lock_name() -> str:
    """每个用例一把自己的锁名：不跟本机真实运行的分析抢同一把。"""
    return rf"Local\bestseller_test_analysis_{uuid.uuid4().hex}"


def fake_webview() -> SimpleNamespace:
    return SimpleNamespace(create_window=MagicMock(), start=MagicMock())


# ---- 关窗那一层的假件（票 03）：形状照真 pywebview 的 winforms 平台 ----
# 真语义（webview/platforms/winforms.py 的 on_closing 与 webview/event.py 的 Event）：
# closing 处理器同步跑，任一个返回 False 就 args.Cancel＝True（这次关闭被吞）；
# window.destroy() 走 Form.Close()，同样再过一次 closing。


def probe_second_instance(lock_name: str) -> str:
    """另一个进程抢同一把锁——第二个分析实例要做的事。'ACQUIRED' 或 'REFUSED'。"""
    code = ("import sys; sys.path.insert(0, %r); "
            "from bestseller_monitor import single_instance as s; "
            "print('ACQUIRED' if s.acquire(%r) else 'REFUSED')" % (str(ROOT), lock_name))
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    return done.stdout.strip()


def windowed_webview(window) -> SimpleNamespace:
    """假 webview：start() 像真的一样开到窗口关上（消息循环随窗口关闭而结束）。"""
    return SimpleNamespace(create_window=MagicMock(return_value=window),
                           start=MagicMock(side_effect=window.closed.wait))


class FakeClosingEvent:
    """假 closing 事件：处理器同步跑，返回 False＝吞掉这次关闭（与真 Event 同规）。"""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def set(self):
        return any(handler() is False for handler in list(self.handlers))


class CloseableWindow:
    """假窗口：点 × 与 destroy() 都会过一次 closing，被吞掉就关不上。"""

    def __init__(self):
        self.events = SimpleNamespace(closing=FakeClosingEvent())
        self.closed = threading.Event()

    def click_close(self) -> bool:
        """点窗口的 ×：走一次 closing。返回这次点击是否被吞掉（没吞＝真的关）。"""
        if self.events.closing.set():
            return True
        self.closed.set()
        return False

    def destroy(self) -> None:
        """window.destroy()：真 pywebview 走 Close()，同样再过一次 closing。"""
        if not self.events.closing.set():
            self.closed.set()


def reading(analysis_id='a1', *, phase_index=3, judged=120, todo=317, eta_text='8 分钟') -> dict:
    """一趟在跑的运行的读数（真 `job_state` 的那些键里关窗那一层用得上的）。"""
    return {'id': analysis_id, 'state': 'matching', 'phases': list(PHASES),
            'phase_index': phase_index, 'judged': judged, 'todo': todo,
            'eta_text': eta_text}


class FakeRun:
    """一趟在跑的运行：读数由用例给，终态由用例放行（`wait_terminal` 等它）。"""

    def __init__(self, analysis_id='a1', **fields):
        self.analysis_id = analysis_id
        self.reading = reading(analysis_id, **fields)
        self.finished = threading.Event()

    def done(self) -> bool:
        return self.finished.is_set()


class FakeAnalysis:
    """关窗那一层要的三件（票 03 的接口）：谁在跑、请求停下、等它停完。"""

    def __init__(self, runs=(), *, race_after_first_look=None):
        self._runs = list(runs)
        self._race = race_after_first_look   # 关窗检查放行之后才登记进来的那趟（夹缝）
        self._looks = 0
        self.stopped, self.waited = [], []
        self.calls = []                      # 收尾里的次序：先请求停下、再等它停完

    def in_flight_job(self):
        self._looks += 1
        if self._looks > 1 and self._race is not None:
            self._runs.append(self._race)
            self._race = None
        for run in self._runs:
            if not run.done():
                return dict(run.reading)
        return None

    def request_stop(self, analysis_id):
        self.stopped.append(analysis_id)
        self.calls.append(('stop', analysis_id))

    def wait_terminal(self, analysis_id, timeout=None):
        self.waited.append(analysis_id)
        self.calls.append(('wait', analysis_id))
        run = next(run for run in self._runs if run.analysis_id == analysis_id)
        run.finished.wait(timeout)
        return dict(run.reading)


class AnalysisMutexTests(unittest.TestCase):
    def test_second_instance_announces_and_exits_zero(self):
        """分析已经开着时：不建第二个窗口，提示后退出 0（让分析壳保持安静）。"""
        name = unique_lock_name()
        holder = single_instance.acquire(name)
        try:
            with patch.object(single_instance, "ANALYSIS_LOCK", name), \
                    patch.object(analyze, "_announce_already_open") as announce, \
                    patch.object(analyze, "create_server") as create:
                code = analyze.main(["--serve"])

            self.assertEqual(code, 0, "第二个实例要安静退出，别让分析壳弹错误框")
            announce.assert_called_once_with()
            create.assert_not_called()
        finally:
            holder.release()

    def test_window_and_serve_share_the_same_lock(self):
        """窗口与 --serve 都先抢同一把锁（ADR-0008 的「命令行与界面共用同一把」）。"""
        for argv in ([], ["--serve"]):
            with self.subTest(argv=argv):
                lock = MagicMock()
                webview = fake_webview()
                with patch.object(single_instance, "acquire", return_value=lock) as acquire, \
                        patch.object(analyze, "create_server", return_value=FakeServer()), \
                        patch.dict(sys.modules, {"webview": webview}), \
                        redirect_stdout(io.StringIO()):
                    code = analyze.main(argv)

                self.assertEqual(code, 0)
                acquire.assert_called_once_with(single_instance.ANALYSIS_LOCK)
                lock.release.assert_called_once_with()

    def test_the_lock_is_released_when_the_config_is_missing(self):
        """配置缺失照旧炸给用户看（traceback → 壳的失败面），但锁要先放掉。"""
        lock = MagicMock()
        config = MagicMock()
        config.from_file.side_effect = FileNotFoundError("找不到分析配置：config/analysis.toml")
        with patch.object(single_instance, "acquire", return_value=lock), \
                patch.object(analyze, "AnalysisConfig", config), \
                self.assertRaises(FileNotFoundError):
            analyze.main(["--serve"])

        lock.release.assert_called_once_with()


class AlreadyOpenNoticeTests(unittest.TestCase):
    def test_already_open_notice_points_at_the_existing_window(self):
        with patch.object(analyze, "_focus_existing_window", return_value=False) as focus, \
                patch.object(analyze, "_notify") as notify:
            analyze._announce_already_open()

        focus.assert_called_once_with(analyze.WINDOW_TITLE)
        self.assertIn("已经打开", notify.call_args.args[0])


class EntryPathTests(unittest.TestCase):
    """锁拿得到时两条路照旧：--serve 打印地址；缺省开窗。"""

    def test_serve_still_prints_the_url_and_returns_zero(self):
        server = FakeServer()
        out = io.StringIO()
        with patch.object(single_instance, "acquire", return_value=MagicMock()), \
                patch.object(analyze, "create_server", return_value=server), \
                redirect_stdout(out):
            code = analyze.main(["--serve"])

        self.assertEqual(code, 0)
        self.assertIn("http://127.0.0.1:43210", out.getvalue())
        self.assertTrue(server.closed, "服务要关干净")

    def test_window_mode_opens_the_analysis_window_at_the_served_url(self):
        server = FakeServer()
        webview = fake_webview()
        with patch.object(single_instance, "acquire", return_value=MagicMock()), \
                patch.object(analyze, "create_server", return_value=server), \
                patch.dict(sys.modules, {"webview": webview}):
            code = analyze.main([])

        self.assertEqual(code, 0)
        title, url = webview.create_window.call_args.args
        self.assertEqual(title, "1688 畅销品监控 · 销量分析")
        self.assertEqual(url, "http://127.0.0.1:43210")
        self.assertTrue(server.shut and server.closed, "关窗后服务要关干净")

    def test_config_argument_still_reaches_the_analysis_config(self):
        config = MagicMock()
        with patch.object(single_instance, "acquire", return_value=MagicMock()), \
                patch.object(analyze, "AnalysisConfig", config), \
                patch.object(analyze, "create_server", return_value=FakeServer()):
            code = analyze.main(["--serve", "--config", "D:/tmp/other.toml"])

        self.assertEqual(code, 0)
        config.from_file.assert_called_once_with(Path("D:/tmp/other.toml"))


class LoggingEncodingTests(unittest.TestCase):
    """壳正文按 UTF-8 读子进程 stderr：分析入口往重定向的 stderr 写中文不能走本机代码页。

    不修的话，壳日志与失败弹窗里的中文提示、traceback 尾巴全是乱码（本机实测过）。
    """

    def test_chinese_notices_reach_a_pipe_as_utf8(self):
        probe = (
            "import sys, analyze; analyze._configure_logging(); "
            "sys.stderr.write('\\u4e2d\\u6587\\u7f16\\u7801\\n'); sys.stderr.flush()"
        )
        done = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                              capture_output=True)

        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        self.assertIn("中文编码", done.stderr.decode("utf-8"))


class CloseWindowTests(unittest.TestCase):
    """关窗＝真的停下（票 03，ADR-0044 决策 4/5）：确认框、等收尾、放锁排在最后。

    缝在 analyze.py：假服务（谁在跑／请求停下／等它停完）、假窗口（closing 同真
    pywebview：返回 False 吞掉关闭）、假 webview（start() 开到窗口关上）。真锁
    （每例一把自己的名字）＋子进程探针，钉住"收尾期间第二个实例必被拒"。
    """

    def setUp(self):
        self.window = CloseableWindow()
        self.webview = windowed_webview(self.window)
        self.lock_name = unique_lock_name()

    def wait_for(self, predicate, what, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f'等不到：{what}')

    def run_analysis_window(self, service):
        """在后台线程里跑一次窗口模式的 main()：真锁、假服务、假 webview。"""
        self.enterContext(patch.object(single_instance, "ANALYSIS_LOCK", self.lock_name))
        self.enterContext(patch.object(analyze, "AnalysisService", return_value=service))
        self.enterContext(patch.object(analyze, "create_server", return_value=FakeServer()))
        self.enterContext(patch.dict(sys.modules, {"webview": self.webview}))
        result = {}
        thread = threading.Thread(target=lambda: result.update(code=analyze.main([])), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.wait_for(lambda: self.window.events.closing.handlers, '关窗钩子挂上')
        return thread, result

    def test_closing_without_a_run_still_exits_quietly(self):
        """没有匹配在跑：无确认框、窗口直接关、退出 0、锁照常释放（票 03 第 6 条）。"""
        confirm = self.enterContext(patch.object(analyze, "_confirm_close", return_value=True))
        thread, result = self.run_analysis_window(FakeAnalysis())

        self.assertFalse(self.window.click_close(), '没有运行在跑：这次点击不该被吞')
        thread.join(10)
        self.assertEqual(result, {'code': 0})
        confirm.assert_not_called()
        self.assertEqual(probe_second_instance(self.lock_name), 'ACQUIRED',
                         '进程退出之后锁要回到系统手里')

    def test_cancel_keeps_matching_and_the_window(self):
        """点 × 选「取消」：关闭被吞、运行照跑；跑完再点 × 就直接关（不再弹框）。"""
        run = FakeRun()
        service = FakeAnalysis([run])
        confirm = self.enterContext(patch.object(analyze, "_confirm_close", return_value=False))
        thread, result = self.run_analysis_window(service)

        self.assertTrue(self.window.click_close(), '「取消」＝吞掉这次关闭')
        self.assertEqual(service.stopped, [], '取消不该请求停止')
        self.assertFalse(self.window.closed.is_set(), '窗口还在')
        self.assertNotIn('code', result, '进程还没退出')
        confirm.assert_called_once()

        run.finished.set()               # 运行自己跑完，回到没有匹配在跑的样子
        self.assertFalse(self.window.click_close(), '这次没有运行在跑了：不再拦')
        thread.join(10)
        self.assertEqual(result, {'code': 0})
        confirm.assert_called_once()

    def test_close_and_stop_waits_for_teardown_before_the_lock_is_released(self):
        """「关闭并停止」：停在收尾里——窗口不关、锁不放；收尾之后才一起走。

        复现脚本（.scratch/mutex-diag/repro_lock_short_window.py）转正的那条：关窗后
        半秒第二个实例就能拿锁是现状 RED；现在探针必须被拒，而且拒到这次运行停写为止。
        """
        run = FakeRun()
        service = FakeAnalysis([run])
        confirm = self.enterContext(patch.object(analyze, "_confirm_close", return_value=True))
        thread, result = self.run_analysis_window(service)

        self.assertTrue(self.window.click_close(), '确认之后这次点击仍被吞：关窗改由收尾完成时执行')
        self.wait_for(lambda: service.waited, '收尾进入 wait_terminal')
        self.assertEqual(service.calls, [('stop', 'a1'), ('wait', 'a1')],
                         '「关闭并停止」走与「停止匹配」同一个停止入口，先请求停下再等它停完')
        text = confirm.call_args.args[0]
        self.assertIn('正在匹配同款：已完成 120/317 对，预计还需约 8 分钟。', text)
        self.assertIn('但这一趟的分析结果会没。', text)
        self.assertFalse(self.window.closed.is_set(), '收尾完成前窗口不消失')
        self.assertNotIn('code', result, '收尾没完，进程不退出')
        self.assertEqual(probe_second_instance(self.lock_name), 'REFUSED',
                         '收尾期间第二个实例探同一把锁必被拒')

        # 收尾期间再点 ×：不再弹第二个框，也不改变什么
        self.assertTrue(self.window.click_close(), '收尾进行中的点击照旧被吞')
        confirm.assert_called_once()

        run.finished.set()               # 在途判断收完，这次运行到终态
        thread.join(10)
        self.assertEqual(result, {'code': 0}, '收尾之后才退出 0')
        self.assertTrue(self.window.closed.is_set(), '收尾之后窗口才关')
        self.assertEqual(probe_second_instance(self.lock_name), 'ACQUIRED',
                         '放锁排在 wait_terminal 之后')

    def test_a_second_x_while_the_confirm_box_is_up_does_not_stack_boxes(self):
        """原生框的嵌套消息泵会把窗口的消息放进来：框还开着时再点 × 会重入钩子。

        那一刻只吞掉点击、不再弹第二个框（假 `click_close` 在确认回调里同步重入，
        与真机上 GUI 线程被 MessageBox 的泵带着走同形）。
        """
        run = FakeRun()
        service = FakeAnalysis([run])
        confirm = self.enterContext(patch.object(analyze, "_confirm_close"))
        swallowed = []

        def answer(_text):
            swallowed.append(self.window.click_close())   # 框还开着：用户又点了一次 ×
            return True

        confirm.side_effect = answer
        thread, result = self.run_analysis_window(service)

        self.assertTrue(self.window.click_close())
        self.assertEqual(swallowed, [True], '重入的那次点击被吞掉，窗口不关')
        self.assertEqual(confirm.call_count, 1, '确认框只弹了一次')
        self.wait_for(lambda: service.waited, '收尾照常走起来')
        run.finished.set()
        thread.join(10)
        self.assertEqual(result, {'code': 0})
        self.assertTrue(self.window.closed.is_set())

    def test_a_run_slipping_in_while_closing_is_still_stopped_before_the_lock_is_released(self):
        """夹缝里抢进来的运行（点 × 的同一瞬页面才发出开始分析）：窗口可以关，锁要等它停。

        关窗检查那一刻还没有运行——确认框都不弹；等它登记进来时窗口已在关。兜底把它
        停下、等它停写，进程才退出：放锁永远排在最后一次写入之后。
        """
        racing = FakeRun('late')
        service = FakeAnalysis([], race_after_first_look=racing)
        confirm = self.enterContext(patch.object(analyze, "_confirm_close", return_value=True))
        thread, result = self.run_analysis_window(service)

        self.assertFalse(self.window.click_close(), '那一刻没有运行在跑：这次点击不被吞')
        confirm.assert_not_called()
        self.wait_for(lambda: service.waited, '兜底把抢进来的运行等起来')
        self.assertEqual(service.stopped, ['late'])
        self.assertNotIn('code', result, '运行没停完，进程不退出')
        self.assertEqual(probe_second_instance(self.lock_name), 'REFUSED',
                         '兜底期间第二个实例同样进不来')

        racing.finished.set()
        thread.join(10)
        self.assertEqual(result, {'code': 0})
        self.assertEqual(probe_second_instance(self.lock_name), 'ACQUIRED')


class CloseConfirmTextTests(unittest.TestCase):
    """关窗确认框的文案：两段照原型「关窗确认」抄，只加了按钮图例（MB_OKCANCEL 的按钮是系统标签）。"""

    def test_judging_text_counts_pairs_and_the_eta_from_the_prototype(self):
        text = analyze._close_confirm_text(reading())
        self.assertIn('正在匹配同款：已完成 120/317 对，预计还需约 8 分钟。', text)
        self.assertIn('关闭会停在这里：已判断的会保存，重新开始会接着算，不会重复花钱；'
                      '但这一趟的分析结果会没。', text)
        self.assertIn('按「确定」＝关闭并停止匹配；按「取消」＝继续匹配。', text)

    def test_no_eta_yet_says_it_is_still_estimating(self):
        text = analyze._close_confirm_text(reading(eta_text=''))
        self.assertIn('已完成 120/317 对，预计时长正在估算。', text)

    def test_before_the_judging_phase_names_the_phase_instead_of_counts(self):
        text = analyze._close_confirm_text(reading(phase_index=0, judged=0, todo=0))
        self.assertIn('正在匹配同款：当前在「冻结库存数据」。', text)

    def test_after_the_last_pair_is_judged_names_the_assembly_phase(self):
        text = analyze._close_confirm_text(reading(phase_index=4, judged=317, todo=317))
        self.assertIn('正在匹配同款：当前在「装配同款组」。', text)

    def test_no_dialog_convention_closes_and_stops_without_a_box(self):
        with patch.dict(os.environ, {'BESTSELLER_NO_DIALOG': '1'}), \
                patch.object(ctypes.windll.user32, 'MessageBoxW') as box:
            self.assertTrue(analyze._confirm_close('文案（自动化场合没人可点）'))
        box.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', '原生框只在 Windows 上')
    def test_box_answers_map_to_the_two_exits(self):
        """确定（IDOK）＝关闭并停止；取消（IDCANCEL）＝不关；框弹不出来也按不关算。"""
        with patch.dict(os.environ, {'BESTSELLER_NO_DIALOG': '0'}):
            with patch.object(ctypes.windll.user32, 'MessageBoxW', return_value=1) as box:
                self.assertTrue(analyze._confirm_close('文案'))
            with patch.object(ctypes.windll.user32, 'MessageBoxW', return_value=2):
                self.assertFalse(analyze._confirm_close('文案'), '「取消」＝不关')
            with patch.object(ctypes.windll.user32, 'MessageBoxW',
                              side_effect=OSError('弹不出来')):
                self.assertFalse(analyze._confirm_close('文案'), '没问成就别关')

        self.assertEqual(box.call_args.args[2], '1688 畅销品监控 · 销量分析（正在匹配）')
        flags = box.call_args.args[3]
        self.assertTrue(flags & analyze._MB_OKCANCEL, '两个出口：确定／取消')
        self.assertTrue(flags & analyze._MB_DEFBUTTON2, '缺省焦点给「取消」')


if __name__ == "__main__":
    unittest.main()
