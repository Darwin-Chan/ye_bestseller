"""独立分析入口；不启动或控制库存采集。

**运行互斥**（票 14）：同一台机器同一时刻至多一次分析运行——窗口与 `--serve` 同规，
锁在这里取（不放启动壳里，改脚本不用重新打包）。抢不到锁的第二个实例落日志、把已有的
「1688 畅销品监控 · 销量分析」窗口叫到前面、弹中文提示，然后**退出 0**（照 ADR-0008
的约定让分析壳保持安静）。分析没有恒真的 `--window`：缺省即开窗，`--serve` 只起本地服务。

**提示路径**：本文件自带一份 `_notify`（落日志 → 非 `BESTSELLER_NO_DIALOG=1` 时弹
MessageBoxW），与 `gui.py` 里那份刻意分开写：入口各自独立，不抽公共件。日志走 stderr
——分析壳把子进程的 stderr 抄进 `logs/analysis_launcher.log`，命令行直接跑时终端也看得到。
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import threading
from pathlib import Path

from bestseller_monitor import single_instance
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.analysis_http import create_server

PROJECT_ROOT = Path(__file__).resolve().parent
WINDOW_TITLE = "1688 畅销品监控 · 销量分析"

log = logging.getLogger(__name__)

_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000
_SW_RESTORE = 9


def _configure_logging() -> None:
    """分析自己的动作也留日志：落 stderr——分析壳把子进程 stderr 抄进壳日志，
    命令行直接跑时在终端看得到。

    stderr 是管道（壳收着它）时对齐到 UTF-8：壳正文按 UTF-8 读子进程 stderr
    （`launcher_core` 的约定），而重定向的 stderr 默认走本机 ANSI 代码页（GBK），
    中文会变成乱码——提示与 traceback 尾巴在壳日志、失败弹窗里都读不出来。
    控制台上 Python 本就用 UTF-8 写，这一步等于没动。
    """
    if sys.stderr is not None:
        try:
            sys.stderr.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _notify(text: str, title: str = WINDOW_TITLE) -> None:
    """一句给人看的提示；弹不出来也只落日志，不算错。

    与 gui.py 里那份刻意分开写：入口各自独立，不抽公共件。
    `BESTSELLER_NO_DIALOG=1` 只落日志不弹窗，与启动壳同一约定，供自动化验证用。
    """
    log.info(text)
    if os.name != "nt" or (os.environ.get("BESTSELLER_NO_DIALOG") or "").strip() == "1":
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            None, text, title, _MB_ICONINFORMATION | _MB_SETFOREGROUND | _MB_TOPMOST)
    except OSError as exc:  # noqa: BLE001
        log.warning("弹窗失败（%s）：%s", exc, text)


def _focus_existing_window(title: str) -> bool:
    """按标题把已有窗口叫到前面；做不到就返回 False（调用方只提示，不报错）。

    与 gui.py 里那份刻意分开写（同一份实现在入口各留一份，不抽公共件）：
    改这里时那边也看一眼，别只修一边。已知的坑：只按标题找可能先命中资源管理器的
    `TabProxyWindow`（同名窗口的任务栏代理，演练查窗口时亲眼见过）——前置不一定命中
    对的那扇窗；先例（gui.py）同源，没真见过错置前就不收紧。
    """
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # 句柄是 64 位指针：不声明类型的话默认 c_int，会把 HWND 截断成错的窗口。
        user32.FindWindowW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        handle = user32.FindWindowW(None, title)
        if not handle:
            return False
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, _SW_RESTORE)
        return bool(user32.SetForegroundWindow(handle))
    except OSError as exc:  # noqa: BLE001
        log.debug("前置已有窗口失败：%s", exc)
        return False


def _announce_already_open() -> None:
    """第二个实例不建窗口：告诉用户分析已经开着，并尽量把那个窗口叫到前面。"""
    log.info("分析已经打开，本次启动不建立第二个窗口。")
    if not _focus_existing_window(WINDOW_TITLE):
        log.info("没能把已有窗口前置，只做提示。")
    _notify("分析已经打开，请看已打开的那个窗口。", title=f"{WINDOW_TITLE}（已经在运行）")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="独立销量分析")
    parser.add_argument("--config", type=Path,
                        default=PROJECT_ROOT / "config" / "analysis.toml")
    parser.add_argument("--serve", action="store_true",
                        help="仅启动本地 HTTP 服务，不打开桌面窗口")
    return parser.parse_args(argv)


def _serve(server) -> None:
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _open_window(server) -> None:
    import webview

    url = f"http://127.0.0.1:{server.server_port}"
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        webview.create_window(WINDOW_TITLE, url, width=1280, height=900)
        webview.start()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def main(argv: list[str] | None = None) -> int:
    """分析入口：同一台机器同一时刻至多一次分析（窗口与 --serve 共用一把锁）。

    抢不到锁就提示已有窗口并**退出 0**——退出码 0 让分析壳保持安静（ADR-0008 的
    「第二个实例自己说话、启动壳不弹」）。
    """
    args = _parse_args(argv)
    _configure_logging()
    lock = single_instance.acquire(single_instance.ANALYSIS_LOCK)
    if lock is None:
        _announce_already_open()
        return 0
    try:
        server = create_server(AnalysisService(AnalysisConfig.from_file(args.config)))
        if args.serve:
            _serve(server)
        else:
            _open_window(server)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
