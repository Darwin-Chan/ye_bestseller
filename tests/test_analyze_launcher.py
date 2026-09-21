"""分析壳的薄入口：参数守卫、--check 报告（票 14）。

壳的通用正文（项目根推导、解释器、弹窗政策）在 test_launcher_core.py；这里只测分析
目标自己加的那点东西：不向子进程转发脚本参数（--config / --serve），收到就明确拒绝并
提示走脚本。名实分裂（键 analysis、脚本 analyze.py）在用例里一并钉住。
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

import analysis_launcher
import launcher_core


class ShellRefusalTests(unittest.TestCase):
    def test_the_config_argument_is_refused_with_a_pointer_to_the_script(self):
        reason = analysis_launcher.shell_refusal(["--config", "config/analysis.toml"])

        self.assertIn("--config", reason)
        self.assertIn("python analyze.py", reason)

    def test_the_serve_switch_is_refused_too(self):
        self.assertIn("--serve", analysis_launcher.shell_refusal(["--serve"]))

    def test_the_shells_own_switches_pass(self):
        self.assertIsNone(analysis_launcher.shell_refusal([]))
        self.assertIsNone(analysis_launcher.shell_refusal(
            ["--check", "--check-report", "C:/tmp/x.json"]))
        self.assertIsNone(analysis_launcher.shell_refusal(["--check-report=C:/tmp/x.json"]))


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
            code = analysis_launcher.main(["--serve"])

        self.assertEqual(code, 2)
        self.assertEqual(len(told), 1)
        self.assertIn("--serve", told[0])
        self.assertIn("python analyze.py", told[0])

    def test_refusal_tries_to_log_beside_the_project_it_can_find(self):
        """认得项目根就把日志落在它的 logs/ 下（弹窗里的路径要能找得到）。"""
        roots: list = []
        with tempfile.TemporaryDirectory() as tmp:
            def spy(root, spec):
                roots.append(root)
                return Path(tmp) / spec.log_name       # 落到临时目录，用例不留痕

            with patch.object(launcher_core, "log_path_for", side_effect=spy), \
                    patch.object(launcher_core, "notify"):
                analysis_launcher.main(["--serve"])

        self.assertEqual(len(roots), 1)
        self.assertTrue((roots[0] / "analyze.py").is_file(),
                        f"日志落点该是认出来的项目根：{roots[0]}")


class CheckReportTests(unittest.TestCase):
    def test_check_reports_the_analysis_target_and_does_not_launch_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "check.json"
            out = io.StringIO()
            with redirect_stdout(out):
                code = analysis_launcher.main(
                    ["--check", "--check-report", str(report_path)])

            self.assertEqual(code, 0, out.getvalue())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertEqual(report["target"], "analysis")
            self.assertEqual(Path(report["project_root"]).name, "bestseller")
            self.assertEqual(Path(report["log"]).name, "analysis_launcher.log")


if __name__ == "__main__":
    unittest.main()
