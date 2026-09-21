"""分析入口（analyze.py）：运行互斥、劝退与提示路径（票 14）。

同一台机器同一时刻至多一次分析运行——窗口与 `--serve` 共用一把 `ANALYSIS_LOCK`；
抢不到就落日志、前置已有窗口、弹中文提示、退出 0（让分析壳保持安静，照 ADR-0008）。
真实锁的用例自起一把内核对象：名字按用例隔离，不跟本机真实运行的分析抢同一把
（与 tests/helpers.py 的 isolated_locks 同规）。
"""
import io
import subprocess
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import analyze
from bestseller_monitor import single_instance

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


if __name__ == "__main__":
    unittest.main()
