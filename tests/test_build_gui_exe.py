"""打包自检：产物里不许夹带项目代码（IS-52 / ADR-0007）。

壳的 TOC 里只该有它自己的模块与第三方库；一旦出现 `gui` 或 `bestseller_monitor`，
exe 就又变回「入口脚本冻结、抓取包走源码目录」的半套形态了。
"""
import tempfile
import unittest
from pathlib import Path

from tools import build_gui_exe

DIRTY_TOC = r"""('Analysis',
 (['F:\AI\projects\bestseller\gui.py'],
  ['F:\AI\projects\bestseller'],
  [('gui', 'F:\AI\projects\bestseller\gui.py', 'PYMODULE'),
   ('bestseller_monitor.db', 'F:\AI\projects\bestseller\bestseller_monitor\db.py', 'PYMODULE'),
   ('webview', 'D:\Python\Lib\site-packages\webview\__init__.py', 'PYMODULE')]))"""

CLEAN_TOC = r"""('Analysis',
 (['F:\AI\projects\bestseller\gui_launcher.py'],
  ['F:\AI\projects\bestseller'],
  ['gui_launcher', 'os', 'subprocess', 'webview', 'zlib']))"""


class ProjectCodeEntryTests(unittest.TestCase):
    def test_flags_bundled_project_modules(self):
        self.assertEqual(
            build_gui_exe.project_code_entries(DIRTY_TOC), ["bestseller_monitor.db", "gui"])

    def test_paths_mentioning_the_project_are_not_entries(self):
        self.assertEqual(build_gui_exe.project_code_entries(CLEAN_TOC), [])


class VerifyArchiveTests(unittest.TestCase):
    def test_clean_artifact_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            toc = Path(tmp) / "Analysis-00.toc"
            toc.write_text(CLEAN_TOC, encoding="utf-8")

            self.assertIsNone(build_gui_exe.assert_no_project_code(toc))

    def test_dirty_artifact_fails_with_the_offending_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            toc = Path(tmp) / "Analysis-00.toc"
            toc.write_text(DIRTY_TOC, encoding="utf-8")

            with self.assertRaises(build_gui_exe.BuildCheckError) as caught:
                build_gui_exe.assert_no_project_code(toc)

            self.assertIn("bestseller_monitor.db", str(caught.exception))
            self.assertIn("gui", str(caught.exception))
