"""浏览器进程的归属判定与收尾（与驱动解耦，爬虫与 GUI 共用）。

收尾必须按「归属」关，不能只认自己 Popen 出来的那个 PID：同一 profile 已有
实例时，新启动的 msedge.exe 会把命令交给旧实例后立刻退出，那时只有 CDP 或
调试端口占用者还能指出真正在跑的浏览器进程（IS-43）。

本模块只依赖标准库，GUI（打包时不带 playwright）也能直接调用。
"""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

# 端口占用者只在这些镜像名下才当作浏览器关闭，避免误杀占用同一端口的其它程序。
BROWSER_IMAGES = ("msedge.exe", "msedge_proxy.exe", "chrome.exe", "chromium.exe")


def image_name_for_pid(tasklist_csv: str, pid: int) -> str:
    """从 `tasklist /FO CSV` 输出里取指定 PID 的镜像名（小写）；找不到返回空串。"""
    for line in tasklist_csv.splitlines():
        fields = [field.strip().strip('"') for field in line.split(",")]
        if len(fields) >= 2 and fields[1].isdigit() and int(fields[1]) == pid:
            return fields[0].lower()
    return ""


def process_image_name(pid: int) -> str:
    """进程镜像名（小写）；查询不可用或查不到返回空串。"""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
    except Exception as exc:  # noqa: BLE001
        log.debug("查询进程镜像名失败：%s", exc)
        return ""
    return image_name_for_pid(out, pid)


def listen_port_owner(port: int) -> int | None:
    """端口的 LISTENING 占用者 PID；没有监听返回 None。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    except Exception as exc:  # noqa: BLE001
        log.debug("查询端口占用失败：%s", exc)
        return None
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


def close_browser(port: int, *, launched_by_us: bool, browser_pid: int | None = None,
                  own_pid: int | None = None) -> int | None:
    """按归属关闭本任务启动的浏览器，返回被关闭的 PID。

    归属判定：本次由本程序启动过浏览器（launched_by_us）才关；接管既有实例
    （start_browser=false）时不动用户的浏览器。自己启动的进程已退出（交接给
    同一 profile 的旧实例）时，改用 CDP 报告的 browser PID，再回退到调试端口
    占用者；用端口占用者兜底时先核对镜像名，避免误杀别的程序。
    """
    if not launched_by_us:
        log.info("本次未启动浏览器（接管既有实例），跳过关闭（端口 %s）。", port)
        return None
    pid = browser_pid or own_pid or listen_port_owner(port)
    if pid is None:
        log.info("调试端口 %s 上没有浏览器进程，无需关闭。", port)
        return None
    if own_pid is None or pid != own_pid:
        image = process_image_name(pid)
        if image not in BROWSER_IMAGES:
            log.warning("调试端口 %s 的占用者 PID %s（%s）不是浏览器，跳过关闭。",
                        port, pid, image or "未知镜像")
            return None
    if terminate_process_tree(pid):
        log.info("已关闭本次启动的浏览器进程（PID %s）。", pid)
        return pid
    return None
