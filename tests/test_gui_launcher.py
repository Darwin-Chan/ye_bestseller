"""启动壳：项目根推导、解释器选择、失败原因（IS-52 / ADR-0007）。

壳是 exe 里唯一的自有代码，所以它只从 exe 位置与 PATH 这两件事实出发做判断，
测试也照这两件事实构造场景。
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gui_launcher import (
    LauncherError,
    bundle_has_project_code,
    failure_hint,
    resolve_project_root,
    resolve_python,
)


def _usable_root(tmp: str) -> Path:
    """造一个「像项目根」的目录：有 gui.py，也有 bestseller_monitor。"""
    root = Path(tmp)
    root.mkdir(parents=True, exist_ok=True)
    (root / "gui.py").write_text("# gui\n", encoding="utf-8")
    (root / "bestseller_monitor").mkdir()
    return root


def _exe_in_dist(root: Path) -> Path:
    exe = root / "dist" / "bestseller_gui.exe"
    exe.parent.mkdir(exist_ok=True)
    exe.write_bytes(b"")
    return exe


class ResolveProjectRootTests(unittest.TestCase):
    def test_derives_root_from_exe_living_in_dist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _usable_root(tmp)

            self.assertEqual(
                resolve_project_root(_exe_in_dist(root), env={}, frozen=True), root)

    def test_derives_root_when_the_shell_runs_from_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _usable_root(tmp)
            launcher = root / "gui_launcher.py"
            launcher.write_text("# shell\n", encoding="utf-8")

            self.assertEqual(
                resolve_project_root(launcher, env={}, frozen=False), root)

    def test_env_override_wins_over_exe_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _usable_root(str(Path(tmp) / "elsewhere"))
            exe = Path(tmp) / "downloads" / "bestseller_gui.exe"
            exe.parent.mkdir()
            exe.write_bytes(b"")

            got = resolve_project_root(
                exe, env={"BESTSELLER_PROJECT": str(root)}, frozen=True)

            self.assertEqual(got, root)

    def test_misplaced_exe_says_where_it_belongs(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "downloads" / "bestseller_gui.exe"
            exe.parent.mkdir()
            exe.write_bytes(b"")

            with self.assertRaises(LauncherError) as caught:
                resolve_project_root(exe, env={}, frozen=True)

            self.assertIn("gui.py", str(caught.exception))
            self.assertIn("dist", str(caught.exception))

    def test_env_pointing_at_a_non_project_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            exe = _exe_in_dist(_usable_root(str(Path(tmp) / "root")))

            with self.assertRaises(LauncherError) as caught:
                resolve_project_root(
                    exe, env={"BESTSELLER_PROJECT": str(empty)}, frozen=True)

            self.assertIn("BESTSELLER_PROJECT", str(caught.exception))


class ResolvePythonTests(unittest.TestCase):
    def test_prefers_the_windowless_python(self):
        on_path = {"pythonw": r"D:\Python\pythonw.exe", "python": r"D:\Python\python.exe"}

        self.assertEqual(resolve_python(env={}, which=on_path.get), on_path["pythonw"])

    def test_falls_back_to_console_python_when_no_pythonw(self):
        on_path = {"python": r"D:\Python\python.exe"}

        self.assertEqual(resolve_python(env={}, which=on_path.get), on_path["python"])

    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "python.exe"
            real.write_bytes(b"")

            got = resolve_python(
                env={"BESTSELLER_PYTHON": str(real)},
                which={"python": r"D:\Python\python.exe"}.get,
            )

            self.assertEqual(got, str(real))

    def test_override_pointing_at_nothing_says_so(self):
        with self.assertRaises(LauncherError) as caught:
            resolve_python(env={"BESTSELLER_PYTHON": r"E:\nope\python.exe"}, which={}.get)

        self.assertIn("BESTSELLER_PYTHON", str(caught.exception))

    def test_no_python_at_all_says_how_to_fix(self):
        with self.assertRaises(LauncherError) as caught:
            resolve_python(env={}, which={}.get)

        self.assertIn("BESTSELLER_PYTHON", str(caught.exception))


class FailureHintTests(unittest.TestCase):
    """界面起不来时，壳要替用户把「怎么办」说出来。"""

    def test_missing_webview_points_at_requirements(self):
        tail = (
            "Traceback (most recent call last):\n"
            '  File "F:\\AI\\projects\\bestseller\\gui.py", line 26, in <module>\n'
            "    import webview\n"
            "ModuleNotFoundError: No module named 'webview'\n"
        )

        hint = failure_hint(tail)

        self.assertIn("webview", hint)
        self.assertIn("pip install -r requirements.txt", hint)

    def test_other_crash_has_nothing_to_add(self):
        tail = "ValueError: 榜单数据不完整\n"

        self.assertEqual(failure_hint(tail), "")


class BundleCheckTests(unittest.TestCase):
    """`--check` 报告里要能看出 exe 里有没有夹带项目代码。"""

    def test_shell_bundle_carries_no_project_code(self):
        self.assertFalse(bundle_has_project_code(find_spec=lambda name: None))

    def test_bundled_package_is_reported(self):
        def fake_find_spec(name: str):
            return object() if name == "bestseller_monitor" else None

        self.assertTrue(bundle_has_project_code(find_spec=fake_find_spec))


class ImportPurityTests(unittest.TestCase):
    """壳不许把项目代码拉进 exe——这正是 IS-52 的根因，锁死它。"""

    def test_importing_launcher_does_not_load_project_modules(self):
        probe = (
            "import sys; import gui_launcher; "
            "loaded = [m for m in ('gui', 'bestseller_monitor') if m in sys.modules]; "
            "print(','.join(loaded))"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True,
        )

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "")
