"""1688 畅销品监控 · 启动壳（IS-52 / ADR-0007）。

`dist/bestseller_gui.exe` 就是这个文件打出来的。exe 里不放项目代码，它只做三件事：
推导项目根、找到本机 python、拉起 `<项目根>\\gui.py` 并守着它。

行为约定：
  - 项目根：`BESTSELLER_PROJECT` > exe 所在目录的上一级（exe 待在 `<项目根>\\dist\\`），
    两者都要求目录里同时有 `gui.py` 与 `bestseller_monitor`。
  - 解释器：`BESTSELLER_PYTHON` > PATH 上的 `pythonw` > PATH 上的 `python`。
  - 壳守着界面进程：stderr 收管道写进 `<项目根>\\logs\\gui_launcher.log`；界面非零退出时
    弹一个中文 MessageBox（原因 + 报错尾部 + 日志路径），用户正常关窗（退出码 0）什么都不弹。
  - `BESTSELLER_NO_DIALOG=1` 时只落日志、不弹窗，供自动化验证用。
  - `--check [--check-report <路径>]`：只做推导与校验，把结果写成 JSON 后退出，不开界面。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

# 一个目录要同时有这两样，才算得上项目根（缺一就是 exe 放错了地方或指错了目录）。
PROJECT_MARKERS = ("gui.py", "bestseller_monitor")

# 先用 pythonw（界面不该带控制台窗口），退到 python。
PYTHON_CANDIDATES = ("pythonw", "python", "python3")

# `ModuleNotFoundError: No module named 'webview'` —— 认得出就给出「怎么办」。
MISSING_MODULE_RE = re.compile(r"No module named '([^']+)'")

# exe 里出现这几个名字，就说明它又变回 IS-52 那种半套代码的形态了。
PROJECT_MODULES = ("bestseller_monitor", "gui")

LOG_NAME = "gui_launcher.log"
TAIL_LINES = 12

_MB_ICONERROR = 0x10
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000


class LauncherError(Exception):
    """启动壳的可读失败原因：文案直接给用户看。"""


def _looks_like_project_root(path: Path) -> bool:
    return all((path / marker).exists() for marker in PROJECT_MARKERS)


def resolve_project_root(launcher_path: Path, env: Mapping[str, str], *, frozen: bool) -> Path:
    """项目根：`BESTSELLER_PROJECT` > 壳自己的位置。

    打包成 exe 时壳待在 `<项目根>\\dist\\`（上一级才是项目根）；直接跑源码时壳就躺在项目根里。
    """
    override = (env.get("BESTSELLER_PROJECT") or "").strip()
    if override:
        root = Path(override)
        if not _looks_like_project_root(root):
            raise LauncherError(
                f"BESTSELLER_PROJECT 指向的目录不像项目根：{root} 下缺少 gui.py 或 bestseller_monitor。"
            )
        return root

    path = Path(launcher_path).resolve()
    root = path.parent.parent if frozen else path.parent
    if not _looks_like_project_root(root):
        where = " exe 的位置" if frozen else " 启动壳的位置"
        raise LauncherError(
            f"从{where}推不出项目根：{root} 下缺少 gui.py 或 bestseller_monitor。\n"
            "exe 需要待在 <项目根>\\dist\\，或用环境变量 BESTSELLER_PROJECT 指定项目根。"
        )
    return root


def resolve_python(env: Mapping[str, str], which: Callable[[str], str | None] = shutil.which) -> str:
    """解释器：`BESTSELLER_PYTHON` > PATH 上的 pythonw > PATH 上的 python。"""
    override = (env.get("BESTSELLER_PYTHON") or "").strip()
    if override:
        if not Path(override).is_file():
            raise LauncherError(f"BESTSELLER_PYTHON 指的不是一个存在的解释器：{override}")
        return override

    for name in PYTHON_CANDIDATES:
        found = which(name)
        if found:
            return found
    raise LauncherError(
        "找不到本机 python：请装好 Python 并让它进 PATH，或用 BESTSELLER_PYTHON 指定解释器路径。"
    )


def failure_hint(stderr_tail: str) -> str:
    """从子进程的报错里认出一句「怎么办」；认不出来就什么都不说。"""
    missing = MISSING_MODULE_RE.search(stderr_tail)
    if missing:
        return (
            f"界面缺少依赖 {missing.group(1)}：请在项目根目录运行 "
            "python -m pip install -r requirements.txt。"
        )
    return ""


def bundle_has_project_code(
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
) -> bool:
    """exe 里有没有夹带项目代码；夹带了就说明打包退回了 IS-52 的半套形态。"""
    return any(find_spec(name) is not None for name in PROJECT_MODULES)


def log_path_for(root: Path | None) -> Path:
    """日志落点：认得项目根就写它的 `logs/`，认不得就先写临时目录（路径会出现在弹窗里）。"""
    return (root or Path(tempfile.gettempdir())) / "logs" / LOG_NAME


class LauncherLog:
    """壳自己的动作与界面的 stderr 都进同一个文件，事后有据可查。"""

    def __init__(self, path: Path, tail_lines: int = TAIL_LINES):
        self.path = path
        self._tail: deque[str] = deque(maxlen=tail_lines)
        self._lock = threading.Lock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def write(self, text: str) -> None:
        for line in text.splitlines():
            with self._lock:
                self._tail.append(line)
            try:
                with self.path.open("a", encoding="utf-8", errors="replace") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    def tail(self) -> str:
        with self._lock:
            return "\n".join(self._tail)


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def notify(reason: str, log: LauncherLog, env: Mapping[str, str]) -> None:
    """失败了要让用户看见：先落日志，再弹窗（`BESTSELLER_NO_DIALOG=1` 时只落日志）。"""
    log.write(f"{_timestamp()} 启动失败：{reason}")
    if (env.get("BESTSELLER_NO_DIALOG") or "").strip() == "1":
        return
    _message_box(f"{reason}\n\n日志：{log.path}")


def _message_box(text: str, title: str = "1688 畅销品监控 · 无法启动") -> None:
    if os.name != "nt":
        return
    import ctypes

    flags = _MB_ICONERROR | _MB_SETFOREGROUND | _MB_TOPMOST
    ctypes.windll.user32.MessageBoxW(None, text, title, flags)


def _pump_stderr(proc: subprocess.Popen, log: LauncherLog) -> None:
    """界面的 stderr 一路抄进日志；进程没了管道也会关，循环自然会退出。"""
    if proc.stderr is None:
        return
    for line in proc.stderr:
        log.write(line.rstrip("\n"))


def launch(root: Path, python: str, env: Mapping[str, str], log: LauncherLog) -> int:
    """拉起 `<项目根>\\gui.py` 并守着它；返回界面的退出码。"""
    gui_py = root / "gui.py"
    log.write(f"{_timestamp()} 拉起界面：{python} {gui_py}")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            [python, str(gui_py)],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=flags,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        notify(f"调不起本机 python（{python}）：{exc}", log, env)
        return 1

    pump = threading.Thread(target=_pump_stderr, args=(proc, log), daemon=True)
    pump.start()
    code = proc.wait()
    pump.join(timeout=2)
    log.write(f"{_timestamp()} 界面退出，退出码 {code}")
    if code:
        parts = [f"界面没能正常退出（退出码 {code}）。", failure_hint(log.tail())]
        if log.tail():
            parts.append("最后几行：\n" + log.tail())
        notify("\n\n".join(part for part in parts if part), log, env)
    return code


def _arg_value(argv: list[str], name: str) -> str | None:
    prefix = f"{name}="
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(prefix):
            return item[len(prefix):]
    return None


def _write_report(path: str | None, report: dict) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if path:
        Path(path).write_text(text + "\n", encoding="utf-8")
    if sys.stdout is not None:
        print(text)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    env = os.environ
    checking = "--check" in argv
    report_path = _arg_value(argv, "--check-report")

    root: Path | None = None
    python: str | None = None
    failure = ""
    frozen = bool(getattr(sys, "frozen", False))
    try:
        root = resolve_project_root(
            Path(sys.executable) if frozen else Path(__file__), env, frozen=frozen)
        python = resolve_python(env)
    except LauncherError as exc:
        failure = str(exc)

    log = LauncherLog(log_path_for(root))
    report = {
        "ok": not failure,
        "frozen": frozen,
        "project_root": str(root) if root else None,
        "python": python,
        "log": str(log.path),
        "bundle_has_project_code": bundle_has_project_code(),
        "error": failure,
    }
    if checking:
        _write_report(report_path, report)
        return 0 if not failure else 1
    if failure:
        notify(failure, log, env)
        return 1
    return launch(root, python, env, log)


if __name__ == "__main__":
    sys.exit(main())
