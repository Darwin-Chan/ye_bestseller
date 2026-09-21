"""启动壳的正文（launcher_core）：项目根推导、解释器选择、失败原因、按目标参数化。

壳是 exe 里唯一的自有代码，所以它只从 exe 位置与 PATH 这两件事实出发做判断，
测试也照这两件事实构造场景。采集壳、交换台壳与分析壳的差异都收在 LauncherSpec 里，
这份用例对三种目标各钉一遍（与鼠标无关的部分）；三入口各自的参数守卫在
各自的文件里（test_exchange_launcher.py / test_analyze_launcher.py）。

原 `test_gui_launcher.py` 的用例改指 core（票 14 壳正文抽芯），断言不动。
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shells"))  # noqa: E402

from analysis_launcher import SPEC as ANALYSIS_SPEC
from exchange_launcher import SPEC as EXCHANGE_SPEC
from gui_launcher import SPEC as GUI_SPEC
from launcher_core import (
    LauncherError,
    bundle_has_project_code,
    child_command,
    failure_hint,
    log_path_for,
    resolve_project_root,
    resolve_python,
    should_notify,
)


def _usable_root(tmp: str, *markers: str) -> Path:
    """造一个「像项目根」的目录：有目标的标记文件，也有 bestseller_monitor。"""
    root = Path(tmp)
    root.mkdir(parents=True, exist_ok=True)
    for marker in markers or ("gui.py",):
        (root / marker).write_text("# marker\n", encoding="utf-8")
    (root / "bestseller_monitor").mkdir(exist_ok=True)
    return root


def _exe_in_dist(root: Path, name: str = "bestseller_gui.exe") -> Path:
    exe = root / "dist" / name
    exe.parent.mkdir(exist_ok=True)
    exe.write_bytes(b"")
    return exe


class ResolveProjectRootTests(unittest.TestCase):
    def test_derives_root_from_exe_living_in_dist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _usable_root(tmp)

            self.assertEqual(
                resolve_project_root(_exe_in_dist(root), env={}, frozen=True,
                                     spec=GUI_SPEC), root)

    def test_derives_root_when_the_shell_runs_from_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _usable_root(tmp)
            launcher = root / "gui_launcher.py"
            launcher.write_text("# shell\n", encoding="utf-8")

            self.assertEqual(
                resolve_project_root(launcher, env={}, frozen=False, spec=GUI_SPEC), root)

    def test_derives_root_when_the_shell_runs_from_shells(self):
        """源码布局（2026-09-21 起）：壳在 <项目根>\\shells\\，上一级才是项目根。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _usable_root(tmp)
            shells = root / "shells"
            shells.mkdir()
            launcher = shells / "gui_launcher.py"
            launcher.write_text("# shell\n", encoding="utf-8")

            self.assertEqual(
                resolve_project_root(launcher, env={}, frozen=False, spec=GUI_SPEC), root)

    def test_refuses_unknown_location_even_with_two_levels(self):
        """两级都不像项目根时照旧拒绝：向上多认一级不等于放宽标记。"""
        with tempfile.TemporaryDirectory() as tmp:
            shells = Path(tmp) / "somewhere" / "shells"
            shells.mkdir(parents=True)
            launcher = shells / "gui_launcher.py"
            launcher.write_text("# shell\n", encoding="utf-8")

            with self.assertRaises(LauncherError):
                resolve_project_root(launcher, env={}, frozen=False, spec=GUI_SPEC)

    def test_env_override_wins_over_exe_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _usable_root(str(Path(tmp) / "elsewhere"))
            exe = Path(tmp) / "downloads" / "bestseller_gui.exe"
            exe.parent.mkdir()
            exe.write_bytes(b"")

            got = resolve_project_root(
                exe, env={"BESTSELLER_PROJECT": str(root)}, frozen=True, spec=GUI_SPEC)

            self.assertEqual(got, root)

    def test_misplaced_exe_says_where_it_belongs(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "downloads" / "bestseller_gui.exe"
            exe.parent.mkdir()
            exe.write_bytes(b"")

            with self.assertRaises(LauncherError) as caught:
                resolve_project_root(exe, env={}, frozen=True, spec=GUI_SPEC)

            self.assertIn("gui.py", str(caught.exception))
            self.assertIn("dist", str(caught.exception))

    def test_env_pointing_at_a_non_project_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            exe = _exe_in_dist(_usable_root(str(Path(tmp) / "root")))

            with self.assertRaises(LauncherError) as caught:
                resolve_project_root(
                    exe, env={"BESTSELLER_PROJECT": str(empty)}, frozen=True, spec=GUI_SPEC)

            self.assertIn("BESTSELLER_PROJECT", str(caught.exception))

    def test_each_shell_wants_its_own_entry_script(self):
        """项目根标记按目标定：有 exchange.py 没 gui.py 的目录，交换台壳认、采集壳不认；
        分析壳认的是 analyze.py（键叫 analysis、标记叫 analyze，写错会当场失败）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _usable_root(tmp, "exchange.py")
            exe = _exe_in_dist(root, "bestseller_exchange.exe")

            self.assertEqual(
                resolve_project_root(exe, env={}, frozen=True, spec=EXCHANGE_SPEC), root)
            with self.assertRaises(LauncherError) as caught:
                resolve_project_root(exe, env={}, frozen=True, spec=GUI_SPEC)
            self.assertIn("gui.py", str(caught.exception))
            with self.assertRaises(LauncherError) as caught:
                resolve_project_root(exe, env={}, frozen=True, spec=ANALYSIS_SPEC)
            self.assertIn("analyze.py", str(caught.exception))

    def test_the_analysis_shell_needs_analyze_py_not_analysis_py(self):
        """名实分裂：项目根标记写 analysis.py 就判「不是项目根」，壳当场失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _usable_root(tmp, "analysis.py")
            exe = _exe_in_dist(root, "bestseller_analysis.exe")

            with self.assertRaises(LauncherError) as caught:
                resolve_project_root(exe, env={}, frozen=True, spec=ANALYSIS_SPEC)

            self.assertIn("analyze.py", str(caught.exception))


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
    """子进程起不来时，壳要替用户把「怎么办」说出来。"""

    def test_missing_webview_points_at_requirements(self):
        tail = (
            "Traceback (most recent call last):\n"
            '  File "F:\\AI\\projects\\bestseller\\gui.py", line 26, in <module>\n'
            "    import webview\n"
            "ModuleNotFoundError: No module named 'webview'\n"
        )

        hint = failure_hint(tail, spec=GUI_SPEC)

        self.assertIn("webview", hint)
        self.assertIn("pip install -r requirements.txt", hint)

    def test_the_hint_speaks_for_its_own_target(self):
        tail = "ModuleNotFoundError: No module named 'webview'\n"

        self.assertIn("交换台", failure_hint(tail, spec=EXCHANGE_SPEC))

    def test_other_crash_has_nothing_to_add(self):
        tail = "ValueError: 榜单数据不完整\n"

        self.assertEqual(failure_hint(tail, spec=GUI_SPEC), "")


class BundleCheckTests(unittest.TestCase):
    """`--check` 报告里要能看出 exe 里有没有夹带项目代码。"""

    def test_shell_bundle_carries_no_project_code(self):
        self.assertFalse(bundle_has_project_code(GUI_SPEC, find_spec=lambda name: None))

    def test_bundled_package_is_reported(self):
        def fake_find_spec(name: str):
            return object() if name == "bestseller_monitor" else None

        self.assertTrue(bundle_has_project_code(GUI_SPEC, find_spec=fake_find_spec))

    def test_the_exchange_shell_watches_its_own_script(self):
        """交换台壳夹带了 exchange.py 也是半套形态：目标各自的名单不同。"""

        def fake_find_spec(name: str):
            return object() if name == "exchange" else None

        self.assertTrue(bundle_has_project_code(EXCHANGE_SPEC, find_spec=fake_find_spec))
        self.assertFalse(bundle_has_project_code(GUI_SPEC, find_spec=fake_find_spec))

    def test_the_analysis_shell_watches_its_own_script(self):
        """分析壳认的脚本名是 analyze（键叫 analysis），名单按脚本名钉。"""

        def fake_find_spec(name: str):
            return object() if name == "analyze" else None

        self.assertTrue(bundle_has_project_code(ANALYSIS_SPEC, find_spec=fake_find_spec))
        self.assertFalse(bundle_has_project_code(GUI_SPEC, find_spec=fake_find_spec))


class ChildCommandTests(unittest.TestCase):
    """壳不向子进程转发参数：拉什么、带什么固定参数，按目标写死。"""

    def test_gui_shell_launches_the_gui_script(self):
        root = Path(r"F:\AI\projects\bestseller")

        self.assertEqual(child_command(GUI_SPEC, root, r"D:\Python\pythonw.exe"),
                         [r"D:\Python\pythonw.exe", str(root / "gui.py")])

    def test_exchange_shell_launches_the_window(self):
        root = Path(r"F:\AI\projects\bestseller")

        self.assertEqual(child_command(EXCHANGE_SPEC, root, r"D:\Python\pythonw.exe"),
                         [r"D:\Python\pythonw.exe", str(root / "exchange.py"), "--window"])

    def test_analysis_shell_launches_the_script_with_no_arguments(self):
        """分析缺省即开窗：壳不传参数（也刻意没有恒真的 --window）。"""
        root = Path(r"F:\AI\projects\bestseller")

        self.assertEqual(child_command(ANALYSIS_SPEC, root, r"D:\Python\pythonw.exe"),
                         [r"D:\Python\pythonw.exe", str(root / "analyze.py")])


class NotifyPolicyTests(unittest.TestCase):
    """弹窗政策按目标区分：采集壳与分析壳「非零都弹」；交换台壳「0/1 静默，2 才弹」。"""

    def test_gui_shell_pops_for_every_nonzero_code(self):
        self.assertFalse(should_notify(GUI_SPEC, 0))
        self.assertTrue(should_notify(GUI_SPEC, 1))
        self.assertTrue(should_notify(GUI_SPEC, 2))

    def test_analysis_shell_pops_for_every_nonzero_code_too(self):
        # 分析脚本没有「非零即正常」的退出码语义，政策与采集壳一致。
        self.assertFalse(should_notify(ANALYSIS_SPEC, 0))
        self.assertTrue(should_notify(ANALYSIS_SPEC, 1))
        self.assertTrue(should_notify(ANALYSIS_SPEC, 2))

    def test_exchange_shell_stays_quiet_on_the_one_that_means_look(self):
        # 退出码 1 是正常结局（「需要看一眼」）；只有 2（本机没做成事）才值得弹。
        self.assertFalse(should_notify(EXCHANGE_SPEC, 0))
        self.assertFalse(should_notify(EXCHANGE_SPEC, 1))
        self.assertTrue(should_notify(EXCHANGE_SPEC, 2))


class LogPathTests(unittest.TestCase):
    def test_each_target_logs_under_its_own_name(self):
        root = Path(r"F:\AI\projects\bestseller")

        self.assertEqual(log_path_for(root, GUI_SPEC).name, "gui_launcher.log")
        self.assertEqual(log_path_for(root, EXCHANGE_SPEC).name, "exchange_launcher.log")
        self.assertEqual(log_path_for(root, ANALYSIS_SPEC).name, "analysis_launcher.log")


class ImportPurityTests(unittest.TestCase):
    """壳不许把项目代码拉进 exe——这正是 IS-52 的根因，锁死它。"""

    def test_importing_a_launcher_does_not_load_project_modules(self):
        probe = (
            "import sys; import {module}; "
            "loaded = [m for m in ('gui', 'exchange', 'analyze', 'bestseller_monitor') "
            "if m in sys.modules]; "
            "print(','.join(loaded))"
        )
        for module in ("gui_launcher", "exchange_launcher", "analysis_launcher",
                       "launcher_core"):
            done = subprocess.run(
                [sys.executable, "-c", probe.format(module=module)],
                cwd=Path(__file__).resolve().parent.parent / "shells",
                capture_output=True, text=True,
            )

            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), "", f"{module} 拉进了项目代码")


if __name__ == "__main__":
    unittest.main()
