"""启动壳的正文（IS-52 / ADR-0007）：三只壳共用这一份，差异全在 `LauncherSpec` 里。

exe 里不放项目代码：壳只做三件事——推导项目根、找到本机 python、拉起源码目录里的
目标脚本并守着它。采集壳（`dist/bestseller_gui.exe`，入口 `gui_launcher.py`）拉
`gui.py`；交换台壳（`dist/bestseller_exchange.exe`，入口 `exchange_launcher.py`）拉
`exchange.py --window`；分析壳（`dist/bestseller_analysis.exe`，入口 `analysis_launcher.py`）
拉 `analyze.py`（缺省即开窗，不带参数）。

行为约定（对三只壳一致）：
  - 项目根：`BESTSELLER_PROJECT` > exe 所在目录的上一级（exe 待在 `<项目根>\\dist\\`），
    两者都要求目录里有目标的项目根标记（见 `LauncherSpec.markers`）。
  - 解释器：`BESTSELLER_PYTHON` > PATH 上的 `pythonw` > PATH 上的 `python`。
  - 壳守着子进程：stderr 收管道写进 `<项目根>\\logs\\<目标日志名>`；子进程退出时按目标的
    弹窗政策决定要不要弹中文 MessageBox（`should_notify`）。
  - `BESTSELLER_NO_DIALOG=1` 时只落日志、不弹窗，供自动化验证用。
  - `--check [--check-report <路径>]`：只做推导与校验，把结果写成 JSON 后退出，不开子进程。

**壳不向子进程转发参数**：目标脚本自己的命令行参数（`--week`、`--only`、`--config`、
`--serve`）一律走源码目录的脚本。交换台壳与分析壳对这类参数明确拒绝
（见 `exchange_launcher.shell_refusal` / `analysis_launcher.shell_refusal`）。
"""
from __future__ import annotations

import dataclasses
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
from collections.abc import Callable, Mapping
from pathlib import Path

# `ModuleNotFoundError: No module named 'webview'` —— 认得出就给出「怎么办」。
MISSING_MODULE_RE = re.compile(r"No module named '([^']+)'")

# 先用 pythonw（界面不该带控制台窗口），退到 python。
PYTHON_CANDIDATES = ("pythonw", "python", "python3")

TAIL_LINES = 12

# 壳自己拒绝启动（参数不对之类）时的退出码：与服务端约定里「没做成事」的 2 对齐。
REFUSE_EXIT_CODE = 2

_MB_ICONERROR = 0x10
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000


@dataclasses.dataclass(frozen=True)
class LauncherSpec:
    """一只壳的目标差异：拉谁、认什么项目根、日志与弹窗怎么说话。

    `quiet_exit_codes` 是「除了 0 之外，还当正常结局、不弹窗」的退出码——采集壳是空的
    （非零都弹）；交换台壳是 `{0, 1}`（1 = 有需要人看一眼的地方，是正常结局）。
    """

    target: str                                  # "gui" / "exchange" / "analysis"：--check 报告里自我说明
    noun: str                                    # 文案主语：「界面」/「交换台」/「分析」
    markers: tuple[str, ...]                     # 项目根标记：缺一就不是项目根
    script: str                                  # 子进程入口：<项目根>\<script>
    script_args: tuple[str, ...] = ()            # 子进程的固定参数（用户的参数不转发）
    log_name: str = "launcher.log"               # <项目根>\logs\ 下的日志名
    dialog_title: str = "1688 畅销品监控 · 无法启动"
    quiet_exit_codes: frozenset[int] = frozenset()
    project_modules: tuple[str, ...] = ()


class LauncherError(Exception):
    """启动壳的可读失败原因：文案直接给用户看。"""


def _looks_like_project_root(path: Path, spec: LauncherSpec) -> bool:
    return all((path / marker).exists() for marker in spec.markers)


def _markers_text(spec: LauncherSpec) -> str:
    return " 或 ".join(spec.markers)


def resolve_project_root(launcher_path: Path, env: Mapping[str, str], *, frozen: bool,
                         spec: LauncherSpec) -> Path:
    """项目根：`BESTSELLER_PROJECT` > 壳自己的位置。

    打包成 exe 时壳待在 `<项目根>\\dist\\`（上一级才是项目根）；直接跑源码时壳就躺在项目根里。
    """
    override = (env.get("BESTSELLER_PROJECT") or "").strip()
    if override:
        root = Path(override)
        if not _looks_like_project_root(root, spec):
            raise LauncherError(
                f"BESTSELLER_PROJECT 指向的目录不像项目根：{root} 下缺少 {_markers_text(spec)}。"
            )
        return root

    path = Path(launcher_path).resolve()
    root = path.parent.parent if frozen else path.parent
    if not _looks_like_project_root(root, spec):
        where = " exe 的位置" if frozen else " 启动壳的位置"
        raise LauncherError(
            f"从{where}推不出项目根：{root} 下缺少 {_markers_text(spec)}。\n"
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


def failure_hint(stderr_tail: str, spec: LauncherSpec) -> str:
    """从子进程的报错里认出一句「怎么办」；认不出来就什么都不说。"""
    missing = MISSING_MODULE_RE.search(stderr_tail)
    if missing:
        return (
            f"{spec.noun}缺少依赖 {missing.group(1)}：请在项目根目录运行 "
            "python -m pip install -r requirements.txt。"
        )
    return ""


def bundle_has_project_code(
    spec: LauncherSpec,
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
) -> bool:
    """exe 里有没有夹带项目代码；夹带了就说明打包退回了 IS-52 的半套形态。"""
    return any(find_spec(name) is not None for name in spec.project_modules)


def should_notify(spec: LauncherSpec, exit_code: int) -> bool:
    """这个退出码要不要弹窗：非零才弹，除非目标把某些非零码声明成正常结局。"""
    return exit_code != 0 and exit_code not in spec.quiet_exit_codes


def child_command(spec: LauncherSpec, root: Path, python: str) -> list[str]:
    """壳会拉起的那条命令（固定形态：脚本 + 目标的固定参数，不转发用户参数）。"""
    return [python, str(root / spec.script), *spec.script_args]


def log_path_for(root: Path | None, spec: LauncherSpec) -> Path:
    """日志落点：认得项目根就写它的 `logs/`，认不得就先写临时目录（路径会出现在弹窗里）。"""
    return (root or Path(tempfile.gettempdir())) / "logs" / spec.log_name


class LauncherLog:
    """壳自己的动作与子进程的 stderr 都进同一个文件，事后有据可查。"""

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


def notify(reason: str, log: LauncherLog, env: Mapping[str, str], spec: LauncherSpec) -> None:
    """失败了要让用户看见：先落日志，再弹窗（`BESTSELLER_NO_DIALOG=1` 时只落日志）。"""
    log.write(f"{_timestamp()} 启动失败：{reason}")
    if (env.get("BESTSELLER_NO_DIALOG") or "").strip() == "1":
        return
    _message_box(f"{reason}\n\n日志：{log.path}", spec)


def _message_box(text: str, spec: LauncherSpec) -> None:
    if os.name != "nt":
        return
    import ctypes

    flags = _MB_ICONERROR | _MB_SETFOREGROUND | _MB_TOPMOST
    ctypes.windll.user32.MessageBoxW(None, text, spec.dialog_title, flags)


def _launcher_location() -> Path:
    """壳自己的位置：打包时是 exe，跑源码时是 launcher_core.py。"""
    return Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)


def _maybe_root(env: Mapping[str, str], spec: LauncherSpec) -> Path | None:
    """尽力认项目根（只为挑日志落点）；认不出就 None。"""
    try:
        return resolve_project_root(_launcher_location(), env,
                                    frozen=bool(getattr(sys, "frozen", False)), spec=spec)
    except LauncherError:
        return None


def refuse(reason: str, *, env: Mapping[str, str], spec: LauncherSpec) -> int:
    """壳拒绝启动（参数不对之类）：说清楚、按「没做成事」退出。

    日志尽量落在项目根的 `logs/` 下（认得出来时）——用户照弹窗里的路径就能找到。
    """
    log = LauncherLog(log_path_for(_maybe_root(env, spec), spec))
    notify(reason, log, env, spec)
    return REFUSE_EXIT_CODE


def _pump_stderr(proc: subprocess.Popen, log: LauncherLog) -> None:
    """子进程的 stderr 一路抄进日志；进程没了管道也会关，循环自然会退出。"""
    if proc.stderr is None:
        return
    for line in proc.stderr:
        log.write(line.rstrip("\n"))


def launch(root: Path, python: str, env: Mapping[str, str], log: LauncherLog,
           spec: LauncherSpec) -> int:
    """拉起 `<项目根>\\<目标脚本>` 并守着它；返回子进程的退出码。"""
    command = child_command(spec, root, python)
    log.write(f"{_timestamp()} 拉起{spec.noun}：{' '.join(command)}")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            command,
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
        notify(f"调不起本机 python（{python}）：{exc}", log, env, spec)
        return 1

    pump = threading.Thread(target=_pump_stderr, args=(proc, log), daemon=True)
    pump.start()
    code = proc.wait()
    pump.join(timeout=2)
    log.write(f"{_timestamp()} {spec.noun}退出，退出码 {code}")
    if should_notify(spec, code):
        parts = [f"{spec.noun}没能正常退出（退出码 {code}）。", failure_hint(log.tail(), spec)]
        if log.tail():
            parts.append("最后几行：\n" + log.tail())
        notify("\n\n".join(part for part in parts if part), log, env, spec)
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


def main(argv: list[str] | None = None, *, spec: LauncherSpec) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    env = os.environ
    checking = "--check" in argv
    report_path = _arg_value(argv, "--check-report")

    root: Path | None = None
    python: str | None = None
    failure = ""
    frozen = bool(getattr(sys, "frozen", False))
    try:
        root = resolve_project_root(_launcher_location(), env, frozen=frozen, spec=spec)
        python = resolve_python(env)
    except LauncherError as exc:
        failure = str(exc)

    log = LauncherLog(log_path_for(root, spec))
    report = {
        "ok": not failure,
        "target": spec.target,
        "frozen": frozen,
        "project_root": str(root) if root else None,
        "python": python,
        "log": str(log.path),
        "bundle_has_project_code": bundle_has_project_code(spec),
        "error": failure,
    }
    if checking:
        _write_report(report_path, report)
        return 0 if not failure else 1
    if failure:
        notify(failure, log, env, spec)
        return 1
    return launch(root, python, env, log, spec)
