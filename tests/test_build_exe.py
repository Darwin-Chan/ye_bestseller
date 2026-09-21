"""打包自检：产物里不许夹带项目代码（IS-52 / ADR-0007）；构建入口按目标泛化（票 14）。

壳的 TOC 里只该有它自己的模块与第三方库；一旦出现 `gui`/`exchange`/`analyze` 或
`bestseller_monitor`，exe 就又变回「入口脚本冻结、抓取包走源码目录」的半套形态了。

原 `test_build_gui_exe.py` 的用例改指 `tools.build_exe`（构建入口从「只管采集壳」泛化成
`--target gui|exchange|analysis`），断言不动。
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tools import build_exe

DIRTY_TOC = r"""('Analysis',
 (['F:\AI\projects\bestseller\gui.py'],
  ['F:\AI\projects\bestseller'],
  [('gui', 'F:\AI\projects\bestseller\gui.py', 'PYMODULE'),
   ('bestseller_monitor.db', 'F:\AI\projects\bestseller\bestseller_monitor\db.py', 'PYMODULE'),
   ('webview', 'D:\Python\Lib\site-packages\webview\__init__.py', 'PYMODULE')]))"""

CLEAN_TOC = r"""('Analysis',
 (['F:\AI\projects\bestseller\gui_launcher.py'],
  ['F:\AI\projects\bestseller'],
  ['gui_launcher', 'launcher_core', 'os', 'subprocess', 'webview', 'zlib']))"""


class ProjectCodeEntryTests(unittest.TestCase):
    def test_flags_bundled_project_modules(self):
        self.assertEqual(
            build_exe.project_code_entries(DIRTY_TOC), ["bestseller_monitor.db", "gui"])

    def test_paths_mentioning_the_project_are_not_entries(self):
        self.assertEqual(build_exe.project_code_entries(CLEAN_TOC), [])

    def test_the_exchange_script_counts_as_project_code(self):
        toc = ("('Analysis', (['x'], ['y'], "
               "[('exchange', 'F:/p/exchange.py', 'PYMODULE')]))")

        self.assertEqual(build_exe.project_code_entries(toc), ["exchange"])

    def test_the_analyze_script_counts_as_project_code(self):
        """键叫 analysis，被拉的脚本叫 analyze.py——夹带的是后者，名单按脚本名钉。"""
        toc = ("('Analysis', (['x'], ['y'], "
               "[('analyze', 'F:/p/analyze.py', 'PYMODULE')]))")

        self.assertEqual(build_exe.project_code_entries(toc), ["analyze"])


class VerifyArchiveTests(unittest.TestCase):
    def test_clean_artifact_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            toc = Path(tmp) / "Analysis-00.toc"
            toc.write_text(CLEAN_TOC, encoding="utf-8")

            self.assertIsNone(build_exe.assert_no_project_code(toc))

    def test_dirty_artifact_fails_with_the_offending_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            toc = Path(tmp) / "Analysis-00.toc"
            toc.write_text(DIRTY_TOC, encoding="utf-8")

            with self.assertRaises(build_exe.BuildCheckError) as caught:
                build_exe.assert_no_project_code(toc)

            self.assertIn("bestseller_monitor.db", str(caught.exception))
            self.assertIn("gui", str(caught.exception))


class TargetTests(unittest.TestCase):
    """三个目标各自的构建坐标：spec 是仓库里真有的文件，产物落 dist/，自检读 build/。"""

    def test_every_target_points_at_a_spec_that_exists(self):
        self.assertEqual(sorted(build_exe.TARGETS), ["analysis", "exchange", "gui"])
        for key, target in build_exe.TARGETS.items():
            with self.subTest(target=key):
                self.assertTrue(target.spec.is_file(), f"{key} 的 spec 不在：{target.spec}")
                self.assertEqual(target.exe.parent.name, "dist")
                self.assertEqual(target.toc.parts[-3:],
                                 ("build", target.spec.stem, "Analysis-00.toc"))

    def test_each_spec_names_the_artifact_the_target_expects(self):
        """spec 里的 `name=` 才是 PyInstaller 用的产物名：它与文件名必须成对，否则半套改名会
        悄悄把 exe 装成另一个名字（verify_exe 的锚）。"""
        for key, target in build_exe.TARGETS.items():
            with self.subTest(target=key):
                spec_text = target.spec.read_text(encoding="utf-8")
                self.assertIn(f"name='{target.exe.stem}'", spec_text)

    def test_the_gui_target_builds_the_fetch_shell(self):
        self.assertEqual(build_exe.TARGETS["gui"].spec.name, "inventory_fetch.spec")
        self.assertEqual(build_exe.TARGETS["gui"].exe.name, "inventory_fetch.exe")

    def test_the_exchange_target_builds_the_exchange_shell(self):
        self.assertEqual(build_exe.TARGETS["exchange"].spec.name, "inventory_exchange.spec")
        self.assertEqual(build_exe.TARGETS["exchange"].exe.name, "inventory_exchange.exe")

    def test_the_analysis_target_builds_the_analysis_shell(self):
        """键取 analysis（跟产品名走），产物名逐目标显式断言，不从键推导。"""
        self.assertEqual(build_exe.TARGETS["analysis"].spec.name, "bestseller_analysis.spec")
        self.assertEqual(build_exe.TARGETS["analysis"].exe.name, "bestseller_analysis.exe")


class CommandLineTests(unittest.TestCase):
    def test_target_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit):
            build_exe.main([])

        self.assertIn("--target", err.getvalue())

    def test_an_unknown_target_is_rejected_before_any_build(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_exe.main(["--target", "analyze"])


if __name__ == "__main__":
    unittest.main()
