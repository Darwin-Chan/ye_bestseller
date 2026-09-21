"""交换台壳的薄入口：参数守卫、--check 报告（票 14）。

壳的通用正文（项目根推导、解释器、弹窗政策）在 test_launcher_core.py；这里只测交换台
目标自己加的那点东西：不向子进程转发脚本参数，收到就明确拒绝并提示走脚本。
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shells"))  # noqa: E402

import exchange_launcher
import launcher_core


class ShellRefusalTests(unittest.TestCase):
    def test_script_arguments_are_refused_with_a_pointer_to_the_script(self):
        reason = exchange_launcher.shell_refusal(["--week", "2026-W37"])

        self.assertIn("--week", reason)
        self.assertIn("python exchange.py", reason)

    def test_only_arguments_are_refused_too(self):
        self.assertIn("--only", exchange_launcher.shell_refusal(["--only", "export"]))

    def test_the_shells_own_switches_pass(self):
        self.assertIsNone(exchange_launcher.shell_refusal([]))
        self.assertIsNone(exchange_launcher.shell_refusal(
            ["--check", "--check-report", "C:/tmp/x.json", "--window"]))
        self.assertIsNone(exchange_launcher.shell_refusal(["--check-report=C:/tmp/x.json"]))


class RefusalEntryTests(unittest.TestCase):
    def test_main_refuses_loudly_and_returns_two(self):
        """拒绝要弹窗说明（自动化验证时设 BESTSELLER_NO_DIALOG=1 只落日志）；
        退出码 2 = 本机没做成事。这里把弹窗层换成记录器、日志换到临时目录，
        只验接线与文案。"""
        told: list[str] = []
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(launcher_core, "log_path_for",
                             return_value=Path(tmp) / "refuse.log"), \
                patch.object(launcher_core, "notify",
                             side_effect=lambda reason, log, env, spec: told.append(reason)):
            code = exchange_launcher.main(["--week", "2026-W37"])

        self.assertEqual(code, 2)
        self.assertEqual(len(told), 1)
        self.assertIn("--week", told[0])
        self.assertIn("python exchange.py", told[0])

    def test_refusal_tries_to_log_beside_the_project_it_can_find(self):
        """认得项目根就把日志落在它的 logs/ 下（弹窗里的路径要能找得到）。"""
        roots: list = []
        with tempfile.TemporaryDirectory() as tmp:
            def spy(root, spec):
                roots.append(root)
                return Path(tmp) / spec.log_name       # 落到临时目录，用例不留痕

            with patch.object(launcher_core, "log_path_for", side_effect=spy), \
                    patch.object(launcher_core, "notify"):
                exchange_launcher.main(["--week", "2026-W37"])

        self.assertEqual(len(roots), 1)
        self.assertTrue((roots[0] / "exchange.py").is_file(),
                        f"日志落点该是认出来的项目根：{roots[0]}")


class CheckReportTests(unittest.TestCase):
    def test_check_reports_the_exchange_target_and_does_not_launch_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "check.json"
            out = io.StringIO()
            with redirect_stdout(out):
                code = exchange_launcher.main(
                    ["--check", "--check-report", str(report_path)])

            self.assertEqual(code, 0, out.getvalue())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertEqual(report["target"], "exchange")
            self.assertEqual(Path(report["project_root"]).name, "bestseller")
            self.assertEqual(Path(report["log"]).name, "exchange_launcher.log")


if __name__ == "__main__":
    unittest.main()
