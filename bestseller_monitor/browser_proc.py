"""浏览器进程的归属判定与收尾（与驱动解耦，爬虫与 GUI 共用）。

收尾必须按精确归属关，不能从调试端口重新猜目标：同一 profile 已有实例时，
新启动的 msedge.exe 会把命令交给旧实例后立刻退出，旧实例属于外部借用，绝不终止。

本模块只依赖标准库，GUI（打包时不带 playwright）也能直接调用。
"""
from __future__ import annotations

import logging
import ctypes
import os
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# 终止命令发出后，仍用同一原始句柄确认目标真的退出；上限是模块内部值，不增加配置。
_VERIFY_EXIT_TIMEOUT_SEC = 2.0
_VERIFY_EXIT_POLL_SEC = 0.1

@dataclass
class ProcessCapability:
    """Opaque process handle plus the OS creation proof observed at bind time."""

    pid: int
    handle: int
    created: str


def process_creation_proof(pid: int) -> str | None:
    """Return the Windows creation FILETIME for pid, or None when unverifiable."""
    if os.name != "nt":
        return None
    try:
        kernel = ctypes.windll.kernel32
        access = 0x1000 | 0x0400  # QUERY_LIMITED_INFORMATION | QUERY_INFORMATION
        handle = kernel.OpenProcess(access, False, int(pid))
        if not handle:
            return None
        created = ctypes.c_ulonglong()
        exited = ctypes.c_ulonglong()
        kernel_time = ctypes.c_ulonglong()
        user_time = ctypes.c_ulonglong()
        ok = kernel.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel_time), ctypes.byref(user_time))
        kernel.CloseHandle(handle)
        return str(created.value) if ok else None
    except Exception as exc:  # noqa: BLE001
        log.debug("查询进程创建证明失败（PID %s）：%s", pid, exc)
        return None


def bind_process(pid: int) -> ProcessCapability | None:
    """Open a target process handle and freeze its creation proof."""
    if os.name != "nt":
        return None
    try:
        kernel = ctypes.windll.kernel32
        access = 0x1000 | 0x0001 | 0x0400
        handle = kernel.OpenProcess(access, False, int(pid))
        if not handle:
            return None
        created = ctypes.c_ulonglong()
        exited = ctypes.c_ulonglong()
        kernel_time = ctypes.c_ulonglong()
        user_time = ctypes.c_ulonglong()
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            kernel.CloseHandle(handle)
            return None
        return ProcessCapability(int(pid), int(handle), str(created.value))
    except Exception as exc:  # noqa: BLE001
        log.debug("绑定进程失败（PID %s）：%s", pid, exc)
        return None


def process_capability_alive(capability: ProcessCapability) -> bool | None:
    if capability is None:
        return None
    if os.name != "nt":
        return None
    try:
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
                capability.handle, ctypes.byref(code)):
            return None
        return int(code.value) == 259  # STILL_ACTIVE
    except Exception as exc:  # noqa: BLE001
        log.debug("查询绑定进程失败（PID %s）：%s", capability.pid, exc)
        return None


def terminate_process_capability(capability: ProcessCapability) -> bool:
    if capability is None:
        return False
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.kernel32.TerminateProcess(capability.handle, 1))
    except Exception as exc:  # noqa: BLE001
        log.warning("关闭绑定进程 PID %s 失败：%s", capability.pid, exc)
        return False


def release_process_capability(capability: ProcessCapability) -> None:
    if os.name != "nt":
        return
    if not isinstance(capability, ProcessCapability):
        # 只有真能力句柄能进 ctypes：替身/异物（mock 什么属性都有）会在参数转换里被
        # ctypes 摸属性，mock 自动生成子 mock → 无限递归 → 3.12/3.13 栈溢出杀进程。
        return
    try:
        ctypes.windll.kernel32.CloseHandle(capability.handle)
    except Exception:
        pass


def decode_console_output(data: bytes | None) -> str:
    """控制台程序（netstat / tasklist）的输出：按 OEM 码页解码，坏字节替换掉。

    这些程序的文本跟随控制台/OEM 码页（中文 Windows 是 GBK），而 `subprocess` 的
    `text=True` 用的是解释器默认编码——UTF-8 模式（PYTHONUTF8=1；Python 3.15 起是
    默认）下解 GBK 会在读取线程里抛 UnicodeDecodeError，stdout 静默变成 None。
    读它们只认 ASCII 片段（TCP / LISTENING / PID / 进程名），替换字符不影响判断。
    """
    return (data or b"").decode("oem" if os.name == "nt" else "utf-8",
                                errors="replace")


def listen_port_owner(port: int) -> int | None:
    """端口的 LISTENING 占用者 PID；没有监听或读不到时返回 None。"""
    try:
        done = subprocess.run(["netstat", "-ano"], capture_output=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("查询端口占用失败：%s", exc)
        return None
    out = decode_console_output(done.stdout)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].lower() == "tcp" and parts[-1].isdigit():
            if "LISTENING" in line and parts[1].endswith(f":{port}"):
                return int(parts[-1])
    return None


def terminate_process_tree(pid: int) -> bool:
    """结束指定 PID 的进程树；失败给出可见告警并返回 False。"""
    try:
        done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("关闭进程树 PID %s 失败：%s", pid, exc)
        return False
    if done.returncode != 0:
        log.warning("关闭进程树 PID %s 失败（taskkill 返回 %s）。", pid, done.returncode)
        return False
    return True


def wait_until_gone(probe, *, timeout: float | None = None, poll: float | None = None):
    """Probe an already-terminated target until it is gone, within a bound.

    ``probe`` reports ``True`` while the exact target is still alive, ``False``
    once it is gone, and ``None`` when that cannot be verified. The wait ends at
    the first ``False``/``None`` and returns it: a ``None`` is never taken as
    gone, and only a ``True`` keeps probing until ``timeout`` expires.
    """
    timeout = _VERIFY_EXIT_TIMEOUT_SEC if timeout is None else timeout
    poll = _VERIFY_EXIT_POLL_SEC if poll is None else poll
    deadline = time.monotonic() + timeout
    state = probe()
    while state is True and time.monotonic() < deadline:
        time.sleep(poll)
        state = probe()
    return state


def close_browser(port: int, *, launched_by_us: bool, browser_pid: int | None = None,
                  browser_os_started: str | None = None) -> int | None:
    """Close a browser process bound by an exact creation proof.

    ``port`` is retained for the public compatibility signature and logging only;
    it is never used to select a destructive target. A session's own Popen goes
    through ``terminate_process_tree`` instead of this function.
    """
    if not launched_by_us:
        log.info("本次未启动浏览器（接管既有实例），跳过关闭（端口 %s）。", port)
        return None

    if browser_pid is None or browser_os_started is None:
        log.warning("拒绝关闭未绑定精确归属的浏览器（端口 %s）。", port)
        return None
    capability = bind_process(browser_pid)
    if capability is None:
        log.warning("无法绑定浏览器 PID %s，拒绝关闭。", browser_pid)
        return None
    try:
        if capability.created != browser_os_started:
            log.warning("浏览器 PID %s 的创建证明不匹配，跳过关闭。", browser_pid)
            return None
        alive = process_capability_alive(capability)
        if alive is False:
            return browser_pid
        if alive is not True:
            log.warning("无法核验浏览器 PID %s 是否仍存活，拒绝关闭。", browser_pid)
            return None
        if not terminate_process_capability(capability):
            return None
        gone = wait_until_gone(lambda: process_capability_alive(capability))
        if gone is False:
            log.info("已关闭本次启动的浏览器进程（PID %s）。", browser_pid)
            return browser_pid
        log.warning("浏览器 PID %s 终止后仍未确认退出。", browser_pid)
        return None
    finally:
        release_process_capability(capability)
