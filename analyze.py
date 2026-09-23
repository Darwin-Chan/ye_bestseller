"""独立分析入口；不启动或控制库存采集。

**运行互斥**（票 14）：同一台机器同一时刻至多一次分析运行——窗口与 `--serve` 同规，
锁在这里取（不放启动壳里，改脚本不用重新打包）。抢不到锁的第二个实例落日志、把已有的
「1688 畅销品监控 · 销量分析」窗口叫到前面、弹中文提示，然后**退出 0**（照 ADR-0008
的约定让分析壳保持安静）。分析没有恒真的 `--window`：缺省即开窗，`--serve` 只起本地服务。

**关窗＝真的停下**（票 03，ADR-0044 决策 4/5）：匹配在跑时点窗口关闭按钮先弹原生
确认框——「取消」吞掉这次关闭、匹配照常；「关闭并停止」走与遮罩上「停止匹配」同一个
停止入口，窗口留在「正在停止匹配…」态等这次运行**彻底停写**（`wait_terminal` 返回）
才关，锁也因此放到那之后（锁的寿命＝运行的寿命，不随窗口）。没有匹配在跑时关窗行为
与从前一样：无确认框、直接退出 0。

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
_MB_ICONWARNING = 0x30
_MB_OKCANCEL = 0x1
_MB_DEFBUTTON2 = 0x100        # 缺省焦点给「取消」：关窗丢的是这一趟结果，别让回车顺手关掉
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000
_IDOK = 1
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


def _close_confirm_text(reading: dict) -> str:
    """关窗确认框的正文：前两段照原型「关窗确认」抄，对数与预计照读数说（票 03）。

    对数只在还有没判的对时说；判完（或还没开始判）就报当前阶段。末行是按钮图例：
    原生 MessageBox 的按钮是系统标签（确定/取消），不写清两个出口，「确定」读起来两种意思。
    """
    remaining = reading['todo'] - reading['judged']
    if remaining > 0:
        line = f"正在匹配同款：已完成 {reading['judged']}/{reading['todo']} 对"
        line += (f"，预计还需约 {reading['eta_text']}。" if reading['eta_text']
                 else "，预计时长正在估算。")
    else:
        line = f"正在匹配同款：当前在「{reading['phases'][reading['phase_index']]}」。"
    return (line + "\n\n"
            "关闭会停在这里：已判断的会保存，重新开始会接着算，不会重复花钱；"
            "但这一趟的分析结果会没。\n\n"
            "按「确定」＝关闭并停止匹配；按「取消」＝继续匹配。")


def _confirm_close(text: str) -> bool:
    """关窗确认框：True＝「关闭并停止」。

    `BESTSELLER_NO_DIALOG=1`（与 `_notify` 同一约定，自动化场合没人可点）不弹窗、按
    「关闭并停止」算；弹不出来（极端场合）按取消算——没问成就别关。
    """
    if os.name != "nt" or (os.environ.get("BESTSELLER_NO_DIALOG") or "").strip() == "1":
        log.info("关窗确认（未弹窗）：%s", text)
        return True
    try:
        answer = ctypes.windll.user32.MessageBoxW(
            None, text, f"{WINDOW_TITLE}（正在匹配）",
            _MB_OKCANCEL | _MB_ICONWARNING | _MB_DEFBUTTON2 | _MB_SETFOREGROUND | _MB_TOPMOST)
    except OSError as exc:  # noqa: BLE001
        log.warning("关窗确认框弹不出来（%s）；按取消算，窗口保持打开", exc)
        return False
    return answer == _IDOK


def _stop_and_wait(service, analysis_id: str) -> None:
    """请求停下这次运行、等它彻底停写——放锁排在这一对之后（ADR-0044 决策 5 的硬语义）。"""
    service.request_stop(analysis_id)
    service.wait_terminal(analysis_id)


def _install_close_guard(window, service) -> None:
    """关窗钩子：匹配在跑先拦、确认后等收尾、收尾完自己关窗（票 03，ADR-0044 决策 4）。

    pywebview 的 closing 处理器返回 False 即吞掉这次关闭（winforms 平台在 GUI 线程上
    同步调它，见 webview/platforms/winforms.py 的 on_closing）。处理器里不长等——收尾
    （`wait_terminal`）交给后台线程，窗口因此留在「正在停止匹配…」态、页面照常轮询，
    而不是整窗假死。收尾完成后 `window.destroy()` 自己走一遍 closing，这次的关由
    `allow_close` 放行。
    """
    tearing_down = False
    allow_close = False
    confirming = False

    def finish_stop_then_close(analysis_id):
        nonlocal tearing_down, allow_close
        try:
            _stop_and_wait(service, analysis_id)
        except Exception:  # noqa: BLE001 —— 等不到终态就不能关窗走人：放锁要排在收尾之后
            log.exception("关窗收尾没能等到这次运行停下，窗口保持打开")
            tearing_down = False              # 回到可拦截：再点 × 还能重来
            return
        allow_close = True
        window.destroy()

    def on_closing():
        nonlocal tearing_down, confirming
        if allow_close:
            return True                       # 收尾完了，这次关闭是我们自己的
        if tearing_down or confirming:
            # 收尾进行中，或确认框还开着（原生框的嵌套消息泵会把窗口的消息放进来：
            # 框在时再点 × 会重入这里）——都只是吞掉这次点击
            return False
        job = service.in_flight_job()
        if job is None:
            return True                       # 没有匹配在跑：关窗与从前一样
        confirming = True
        try:
            answer = _confirm_close(_close_confirm_text(job))
        finally:
            confirming = False
        if not answer:
            return False                      # 「取消」：吞掉这次关闭，匹配照常
        tearing_down = True
        threading.Thread(target=finish_stop_then_close, args=(job['id'],), daemon=True,
                         name='analysis-close-teardown').start()
        return False                          # 先吞一次；收尾完成后由 destroy 真关

    window.events.closing += on_closing


def _stop_slipped_in_runs(service) -> None:
    """关窗那一瞬才受理进来的运行：停它、等它停写之后才轮到放锁（票 03 的兜底）。

    正常路径关窗钩子已经等过了；这里兜的是"点 × 的同一瞬页面正好发出开始分析"这种
    夹缝。调用点排在 `server_close()` 之后——它 join 完处理线程，不会再有新运行登记，
    所以查到谁就等谁。窗口这一刻已经不在了：这趟的结果会没（关窗的代价，与从前一致），
    但进程不再带着没停的写入退出。
    """
    while True:
        job = service.in_flight_job()
        if job is None:
            return
        _stop_and_wait(service, job['id'])


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


def _open_window(server, service) -> None:
    import webview

    url = f"http://127.0.0.1:{server.server_port}"
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        window = webview.create_window(WINDOW_TITLE, url, width=1280, height=900)
        _install_close_guard(window, service)
        webview.start()
    finally:
        server.shutdown()
        # server_close 会 join 处理线程：这之后不会再有新的分析被受理。
        server.server_close()
        worker.join()
        _stop_slipped_in_runs(service)


def main(argv: list[str] | None = None) -> int:
    """分析入口：同一台机器同一时刻至多一次分析（窗口与 --serve 共用一把锁）。

    抢不到锁就提示已有窗口并**退出 0**——退出码 0 让分析壳保持安静（ADR-0008 的
    「第二个实例自己说话、启动壳不弹」）。放锁排在 `_open_window` 返回之后，而它要等
    关窗收尾（`wait_terminal`）走完才返回：锁的寿命因此＝这次分析的寿命，不随窗口。
    """
    args = _parse_args(argv)
    _configure_logging()
    lock = single_instance.acquire(single_instance.ANALYSIS_LOCK)
    if lock is None:
        _announce_already_open()
        return 0
    try:
        service = AnalysisService(AnalysisConfig.from_file(args.config))
        server = create_server(service)
        if args.serve:
            _serve(server)
        else:
            _open_window(server, service)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
