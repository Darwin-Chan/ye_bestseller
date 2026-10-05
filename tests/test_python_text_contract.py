"""Python 管道编码契约：通过真实标准流与壳的失败可见性验证。"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shells"))

import launcher_core
from analysis_launcher import SPEC as ANALYSIS_SPEC
from exchange_launcher import SPEC as EXCHANGE_SPEC
from gui_launcher import SPEC as GUI_SPEC


class PythonTextEnvTests(unittest.TestCase):
    def test_conflicting_parent_encoding_still_round_trips_both_streams(self):
        parent = {**os.environ, "PYTHONIOENCODING": "gbk",
                  "ENCODING_PROBE": "保留其它变量"}
        before = dict(parent)
        child_env = launcher_core.python_text_env(MappingProxyType(parent))
        code = ("import os, sys; "
                "print('中文输出：库存交换'); "
                "print('中文失败：无法启动', file=sys.stderr); "
                "print(os.environ['ENCODING_PROBE'])")
        done = subprocess.run([sys.executable, "-c", code], env=child_env,
                              capture_output=True, timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.decode("utf-8").splitlines(),
                         ["中文输出：库存交换", "保留其它变量"])
        self.assertEqual(done.stderr.decode("utf-8").strip(), "中文失败：无法启动")
        self.assertEqual(parent, before)
        self.assertIsNot(child_env, parent)

    @unittest.skipUnless(shutil.which("pythonw"), "本机没有 pythonw")
    def test_windowless_python_also_round_trips_both_streams(self):
        code = "import sys; print('无窗口中文输出'); print('无窗口中文错误', file=sys.stderr)"
        done = subprocess.run(
            [shutil.which("pythonw"), "-c", code], capture_output=True,
            env=launcher_core.python_text_env({**os.environ, "PYTHONIOENCODING": "gbk"}),
            timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.decode("utf-8").strip(), "无窗口中文输出")
        self.assertEqual(done.stderr.decode("utf-8").strip(), "无窗口中文错误")


class ToolOutputTests(unittest.TestCase):
    def test_importing_contract_and_tools_does_not_reconfigure_standard_streams(self):
        names = ["acceptance_check", "bench_refresh", "check_orphans", "diag_card_urls",
                 "diag_offer_id_presolve", "diag_verify_state", "dump_plan_parity",
                 "issue_bundle", "replay_groups", "run_full_suite", "summary",
                 "sync_list_titles"]
        code = (
            "import importlib, sys; "
            "before = (sys.stdout.encoding, sys.stdout.errors, sys.stderr.encoding, sys.stderr.errors); "
            "import launcher_core; "
            f"[importlib.import_module('tools.' + name) for name in {names!r}]; "
            "assert (sys.stdout.encoding, sys.stdout.errors, sys.stderr.encoding, sys.stderr.errors) == before"
        )
        done = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(launcher_core.__file__).parent.parent,
            env={**os.environ, "PYTHONIOENCODING": "gbk",
                 "PYTHONPATH": str(Path(launcher_core.__file__).parent)},
            capture_output=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_output_adapter_tolerates_missing_and_non_reconfigurable_streams(self):
        output = io.StringIO()
        with mock.patch.object(sys, "stdout", output), mock.patch.object(sys, "stderr", None):
            launcher_core.configure_tool_output()
            launcher_core.configure_tool_output(errors=None)
            print("仍可写入中文")
        self.assertEqual(output.getvalue(), "仍可写入中文\n")

    def test_tool_output_error_policy_is_separate_from_pipe_decoding(self):
        for errors, expected in (("replace", "replace"), (None, "strict")):
            with self.subTest(errors=errors):
                with io.TextIOWrapper(io.BytesIO(), encoding="gbk",
                                      errors="backslashreplace") as stdout, \
                        io.TextIOWrapper(io.BytesIO(), encoding="gbk",
                                           errors="backslashreplace") as stderr:
                    with mock.patch.object(sys, "stdout", stdout), \
                            mock.patch.object(sys, "stderr", stderr):
                        launcher_core.configure_tool_output(errors=errors)
                    self.assertEqual(stdout.encoding, "utf-8")
                    self.assertEqual(stderr.encoding, "utf-8")
                    self.assertEqual(stdout.errors, expected)
                    self.assertEqual(stderr.errors, expected)

    def test_direct_cli_help_is_utf8_without_using_the_parent_env_helper(self):
        root = Path(launcher_core.__file__).parent.parent
        for name in ("acceptance_check", "bench_refresh", "check_orphans", "issue_bundle",
                     "replay_groups", "run_full_suite", "summary"):
            with self.subTest(tool=name):
                done = subprocess.run(
                    [sys.executable, str(root / "tools" / (name + ".py")), "--help"],
                    env={**os.environ, "PYTHONIOENCODING": "gbk"},
                    capture_output=True, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr)
                text = done.stdout.decode("utf-8")
                self.assertIn("usage:", text)
                self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in text))
                self.assertNotIn("\ufffd", text)

    def test_direct_tool_output_is_utf8_even_with_a_conflicting_environment(self):
        code = (
            "import sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "from launcher_core import configure_tool_output; "
            "configure_tool_output(); "
            "print('工具输出：中文'); print('工具错误：中文', file=sys.stderr)"
        )
        done = subprocess.run(
            [sys.executable, "-c", code, str(Path(launcher_core.__file__).parent)],
            env={**os.environ, "PYTHONIOENCODING": "gbk"},
            capture_output=True, timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.decode("utf-8").strip(), "工具输出：中文")
        self.assertEqual(done.stderr.decode("utf-8").strip(), "工具错误：中文")


class LauncherTextTests(unittest.TestCase):
    def test_all_targets_capture_startup_failure_using_the_supplied_environment(self):
        for spec in (GUI_SPEC, EXCHANGE_SPEC, ANALYSIS_SPEC):
            with self.subTest(target=spec.target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / spec.script).write_text(
                    "import os, sys\n"
                    "print('中文失败：导入入口前', file=sys.stderr)\n"
                    "print(os.environ.get('ENCODING_PROBE', 'missing'), file=sys.stderr)\n"
                    "raise RuntimeError('中文启动异常')\n", encoding="utf-8")
                env = {**os.environ, "BESTSELLER_NO_DIALOG": "1",
                       "PYTHONIOENCODING": "gbk", "ENCODING_PROBE": "传入的环境"}
                log = launcher_core.LauncherLog(root / spec.log_name)
                with mock.patch.dict(os.environ, {"PYTHONIOENCODING": "gbk"}):
                    code = launcher_core.launch(root, sys.executable, env, log, spec)
                text = log.path.read_text(encoding="utf-8")
                self.assertEqual(code, 1)
                self.assertIn("中文失败：导入入口前", text)
                self.assertIn("传入的环境", text)
                self.assertIn("RuntimeError: 中文启动异常", text)
                self.assertNotIn("\ufffd", text)

    def test_invalid_bytes_do_not_interrupt_later_chinese_failure_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / GUI_SPEC.script).write_text(
                "import os, sys\n"
                "os.write(2, bytes([255]) + b'\\n')\n"
                "print('坏字节之后仍有中文原因', file=sys.stderr)\n"
                "sys.exit(2)\n", encoding="utf-8")
            log = launcher_core.LauncherLog(root / GUI_SPEC.log_name)
            env = dict(os.environ)
            env.pop("BESTSELLER_NO_DIALOG", None)
            with mock.patch.object(launcher_core, "_message_box") as dialog:
                code = launcher_core.launch(root, sys.executable, env, log, GUI_SPEC)
            self.assertEqual(code, 2)
            self.assertIn("\ufffd", log.tail())
            self.assertIn("坏字节之后仍有中文原因", log.tail())
            self.assertIn("坏字节之后仍有中文原因", dialog.call_args.args[0])

    def test_actual_failure_notifications_preserve_each_targets_exit_policy(self):
        for spec, notifying_codes in ((GUI_SPEC, {1, 2}), (EXCHANGE_SPEC, {2}),
                                     (ANALYSIS_SPEC, {1, 2})):
            for exit_code in (0, 1, 2):
                with self.subTest(target=spec.target, code=exit_code), \
                        tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / spec.script).write_text(
                        "import sys\n"
                        "print('中文退出原因：请检查配置', file=sys.stderr)\n"
                        f"sys.exit({exit_code})\n", encoding="utf-8")
                    log = launcher_core.LauncherLog(root / spec.log_name)
                    env = {**os.environ, "PYTHONIOENCODING": "gbk"}
                    env.pop("BESTSELLER_NO_DIALOG", None)
                    with mock.patch.object(launcher_core, "_message_box") as dialog:
                        code = launcher_core.launch(root, sys.executable, env, log, spec)
                    self.assertEqual(code, exit_code)
                    self.assertEqual(dialog.called, exit_code in notifying_codes)
                    if dialog.called:
                        text = dialog.call_args.args[0]
                        self.assertIn("中文退出原因：请检查配置", text)
                        self.assertNotIn("\ufffd", text)


if __name__ == "__main__":
    unittest.main()
