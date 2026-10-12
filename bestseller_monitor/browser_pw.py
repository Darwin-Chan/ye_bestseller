"""浏览器会话与详情页读取（Playwright 连接接管，ADR-0010）。

点击式列表的遍历、逐卡逻辑与 deny 账本在 click_listing.py，榜单页行为在 listing.py，
详情观测规则在 detail.py；这里只剩：

- 用普通进程拉起浏览器、经调试端口接管、按归属收尾（IS-43）；
- 逐店补采的详情导航 adapter（交回 `detail_visit.OpenedDetail`，见 ADR-0023）。

`DenyTracker`、两个 deny 异常与详情页「打开之后怎么安顿」（`guard.ready_detail_page()`）都长在
guard.py（deny 判定的家，候选 04）；这里不为它们转出旧名。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time  # 本文件不再直接计时；留给 patch.object(browser_pw.time, …)（与 waiting 同一个 time 模块）
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from . import browser_proc, listing, waiting
from .click_listing import WAIT_POPUP_MS
from .config import Config
from .delay import Humanizer
from .detail_visit import OpenedDetail
# 诊断工具仍从 browser_pw 取这几个名字（四个旧私有名，加 guard 的公开名
# `intervention_kind`）：调用点在 tools/，这条边界由 tests/test_browser_pw.py::
# CompatSurfaceTests 钉着。
from .guard import (  # noqa: F401
    body_text, captcha_visible, intervention_kind, is_punish_url, resolved,
)

log = logging.getLogger(__name__)

@dataclass
class _SessionResources:
    """One session's exact handles, acquired in establishment order."""

    port: int
    publisher: Callable[..., bool] | None
    start_browser: bool
    proc: Any = None
    launch_proof: str | None = None
    # 启动进程交棒给新进程时（作业对象逃逸、引导启动器），真浏览器不是本次
    # Popen 那个进程，按它的创建证明单独记账，收尾时照它关。
    owned_pid: int | None = None
    owned_proof: str | None = None
    pw: Any = None
    br: Any = None
    page: Any = None
    ctx: Any = None
    handle_pw: Any = None
    handle_br: Any = None
    delivered: bool = False
    phase: str = "opening"
    publication_failed: bool = False


_session_lock = threading.RLock()
_session: _SessionResources | None = None
# Session identity is the pw/br objects themselves: every establishment returns
# fresh ones, so an old A can never match a newer B. This cache only makes a
# repeat close of a recently closed session a no-op; the bound is memory, and
# past it a stale handle is indistinguishable from a bogus one, so close_session
# raises rather than silently succeeding.
_CLOSED_HANDLE_MEMORY = 16
_closed_handles: deque[tuple[Any, Any, Callable[..., bool] | None]] = deque(
    maxlen=_CLOSED_HANDLE_MEMORY)

# 诊断工具按旧私有名取这几个（本文件自己不用它们）
_body_text = body_text
_captcha_visible = captcha_visible
_is_punish_url = is_punish_url
_resolved = resolved

# 条件等待的上限兜底（秒）——优先“等条件满足”，超时才继续，替代固定 sleep。
_WAIT_LAUNCH_SEC = 25.0    # 等浏览器调试端口可连接

# 启动进程交棒给新浏览器进程时，两者创建时刻的允许间距（秒）。实测 msedge.exe
# 交棒只差 20~50 毫秒，收得很紧只是为了区分「刚出生的才可能是我们交棒的那只」；
# 比启动进程还老的浏览器一定不是本次启动的。
_HANDOFF_WINDOW_SEC = 10.0
_HANDOFF_WINDOW_TICKS = int(_HANDOFF_WINDOW_SEC * 10_000_000)  # FILETIME 100ns

# 搬到 listing.py 的榜单页原语：诊断工具仍按旧私有名引用，这里留兼容别名（同 guard 那组）。
# 本文件内部一律走 listing.*，别名只给工具用——否则测试打桩 listing 的改动会静默失效
# （2026-09-13 被这条咬过：测试改打 listing，采集路径却用别名，于是真的走进人工介入等待）。
_WAIT_POPUP_MS = WAIT_POPUP_MS
_PRODUCT_IMG_SEL = listing.PRODUCT_IMG_SEL
_click_text_in_frames = listing.click_text_in_frames


def open_session(cfg: Config, *, publish_browser=None):
    global _session
    from .dedicated_browser import verify_gui_settings
    verify_gui_settings(cfg)
    _clear_pending_before_open()
    from playwright.sync_api import sync_playwright

    resources = _SessionResources(
        int(cfg.attach_port), publish_browser,
        bool(getattr(cfg, "start_browser", True)),
    )
    with _session_lock:
        if _session is not None:
            raise RuntimeError("同一进程已有活动或待清理的浏览器会话")
        _session = resources

    stage = "launch"
    try:
        if resources.start_browser:
            _publish(resources, "STARTING", resources.port, None, None)
            edge = getattr(cfg, "chrome_path", None)
            if not edge or not os.path.isfile(edge):
                raise FileNotFoundError(f"浏览器路径不存在：{edge or '<empty>'}")
            resources.proc = subprocess.Popen([
                edge,
                f"--remote-debugging-port={resources.port}",
                f"--user-data-dir={cfg.user_data_path}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ])
            resources.launch_proof = browser_proc.process_creation_proof(resources.proc.pid)
            log.info("已用普通进程启动浏览器（调试端口 %s，PID %s）。",
                     resources.port, resources.proc.pid)

        stage = "playwright"
        resources.pw = sync_playwright().start()
        stage = "cdp"
        last_exc: Exception | None = None

        def probe() -> Any:
            nonlocal last_exc
            try:
                return resources.pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{resources.port}")
            except Exception as exc:  # noqa: BLE001 - retry until the readiness deadline
                last_exc = exc
                return None

        # 等待期间被停止请求打断时，取消异常从 until 原样穿出，走下面的清理与发布序列。
        resources.br = waiting.until(probe, timeout_sec=_WAIT_LAUNCH_SEC, poll_sec=0.8,
                                     describe=f"浏览器调试端口 {resources.port}")
        if resources.br is None:
            raise RuntimeError(
                f"无法连接浏览器调试端口 {resources.port}（{last_exc}）")

        if getattr(cfg, 'gui_browser_settings', None) is not None:
            stage = "ownership"
            state, pid, proof = _browser_publication(resources)
            if state != 'OWNED':
                log.warning("浏览器创建归属未核实（判定 %s，启动进程 %s，端口上的浏览器 %s）。",
                            state, getattr(resources.proc, 'pid', None), pid)
                resources.br = None
                # No context/page/navigation on a redirected or unknown instance.
                raise RuntimeError('浏览器创建归属无法核实，拒绝采集，不接管已有实例。')

        stage = "context"
        if not resources.br.contexts:
            raise RuntimeError("浏览器没有可复用的默认 context")
        resources.ctx = resources.br.contexts[0]
        stage = "page"
        resources.page = resources.ctx.new_page()
        stage = "configure"
        resources.page.set_default_timeout(cfg.timeout_ms)

        stage = "publish"
        state, pid, proof = _browser_publication(resources)
        _publish(resources, state, resources.port, pid, proof)
        resources.handle_pw = resources.pw
        resources.handle_br = resources.br
        resources.delivered = True
        resources.phase = "active"
        return resources.pw, resources.br, resources.page, resources.ctx
    except BaseException:
        log.warning("浏览器会话建立失败（阶段 %s，端口 %s）。", stage, resources.port)
        clean = _cleanup(resources)
        if publish_browser is not None and not resources.publication_failed:
            # Only exact resource cleanup proves that this invocation owes no
            # browser cleanup. A failed/unknown cleanup must remain blocked.
            state = "CLOSED" if clean else "UNKNOWN"
            try:
                publish_browser(state, None if clean else resources.port, None, None)
            except Exception as exc:  # noqa: BLE001 - preserve the establishment error
                log.warning("发布浏览器 %s 状态失败：%s", state, exc)
        _release_if_clean(resources)
        raise


def _publish(resources: _SessionResources, state: str, port: int | None,
             pid: int | None, proof: str | None) -> None:
    if resources.publisher is None:
        return
    try:
        accepted = resources.publisher(state, port, pid, proof)
    except BaseException:
        resources.publication_failed = True
        raise
    if accepted is not True:
        resources.publication_failed = True
        raise RuntimeError(f"浏览器状态发布被当前目标拒绝：{state}")


def _browser_publication(resources: _SessionResources) -> tuple[str, int | None, str | None]:
    if not resources.start_browser:
        return "BORROWED", None, None
    proc = resources.proc
    if proc is None:
        return "BORROWED", None, None
    browser_pid = cdp_browser_pid(resources.br)
    browser_proof = (browser_proc.process_creation_proof(browser_pid)
                     if browser_pid is not None else None)
    if proc.poll() is not None:
        # 启动进程已经退出。它不是交棒给旧实例（那是借用），就是自己把浏览器交给
        # 了刚出生的新进程：作业对象限制下 msedge.exe 会另起一个进程逃出去，引导
        # 启动器同理（2026-10-12 m1 现场）。出生在本次启动之后、间距很小的那个才
        # 可能是我们交的棒；比启动进程还老、或者核不实的，一律不认。
        if _born_from_our_launch(resources.launch_proof, browser_proof):
            resources.owned_pid = browser_pid
            resources.owned_proof = browser_proof
            return "OWNED", browser_pid, browser_proof
        return "BORROWED", None, None
    if (browser_pid == proc.pid and resources.launch_proof is not None
            and browser_proof == resources.launch_proof):
        return "OWNED", browser_pid, resources.launch_proof
    return "UNKNOWN", None, None


def _born_from_our_launch(launch_proof: str | None, browser_proof: str | None) -> bool:
    """端口上这只浏览器是不是本次启动进程交棒出来的（创建时刻的先后与间距）。"""
    try:
        gap = int(browser_proof) - int(launch_proof)
    except (TypeError, ValueError):
        return False
    return 0 < gap <= _HANDOFF_WINDOW_TICKS


def _clear_pending_before_open() -> None:
    with _session_lock:
        resources = _session
        if resources is None:
            return
        if resources.phase in {"opening", "active", "closing"}:
            raise RuntimeError("同一进程已有活动或正在关闭的浏览器会话")
        resources.phase = "closing"
    _cleanup(resources)
    if resources.phase != "clean":
        resources.phase = "pending"
        raise RuntimeError("上一个浏览器会话仍有资源待清理")
    _publish_closed(resources)
    _release_if_clean(resources)


def _cleanup(resources: _SessionResources) -> bool:
    resources.phase = "closing"
    if resources.page is not None:
        try:
            resources.page.close()
            resources.page = None
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup retains the handle
            log.warning("浏览器会话清理失败（资源 page，阶段 cleanup）：%s", exc)

    # A borrowed browser must keep its transport alive when the page cannot be
    # closed, otherwise a retry loses the only exact handle to that page.
    if resources.page is None and resources.br is not None:
        try:
            resources.br.close()
            resources.br = None
            resources.ctx = None
        except Exception as exc:  # noqa: BLE001
            log.warning("浏览器会话清理失败（资源 cdp，阶段 cleanup）：%s", exc)
    if resources.page is None and resources.br is None and resources.pw is not None:
        try:
            resources.pw.stop()
            resources.pw = None
        except Exception as exc:  # noqa: BLE001
            log.warning("浏览器会话清理失败（资源 playwright，阶段 cleanup）：%s", exc)

    proc = resources.proc
    proc_was_alive = False
    if proc is not None:
        proc_was_alive = proc.poll() is None
        if not proc_was_alive:
            resources.proc = None
        elif browser_proc.terminate_process_tree(proc.pid):
            if browser_proc.wait_until_gone(lambda: proc.poll() is None) is False:
                resources.proc = None
            else:
                log.warning("浏览器会话清理后进程仍存活（阶段 cleanup，PID %s）。", proc.pid)
        else:
            log.warning("浏览器会话进程清理失败（阶段 cleanup，PID %s）。", proc.pid)

    # Once our exact process is gone, its remote objects are gone as well. This
    # lets owned sessions recover even if a remote close call failed first.
    if proc_was_alive and resources.proc is None:
        resources.page = None
        resources.br = None
        resources.ctx = None
        if resources.pw is not None:
            try:
                resources.pw.stop()
                resources.pw = None
            except Exception as exc:  # noqa: BLE001
                log.warning("浏览器会话清理失败（资源 playwright，阶段 cleanup）：%s", exc)

    # 交棒出来的浏览器握在 Popen 句柄之外，只能按绑定的创建证明关；关不掉且它
    # 还在，就留着待清理，不把这次收尾算成干净。
    if resources.proc is None and resources.owned_pid is not None:
        closed = browser_proc.close_browser(
            resources.port, launched_by_us=True,
            browser_pid=resources.owned_pid, browser_os_started=resources.owned_proof)
        if closed is not None or browser_proc.process_alive(resources.owned_pid) is False:
            resources.owned_pid = None
            resources.owned_proof = None
        else:
            log.warning("交棒浏览器（PID %s）清理未完成，保留待清理。", resources.owned_pid)

    clean = resources.page is None and resources.br is None \
        and resources.pw is None and resources.proc is None \
        and resources.owned_pid is None
    resources.phase = "clean" if clean else "pending"
    return clean


def _release_if_clean(resources: _SessionResources) -> bool:
    global _session
    if resources.phase != "clean":
        return False
    with _session_lock:
        if _session is resources:
            _session = None
        if resources.delivered:
            _closed_handles.append(
                (resources.handle_pw, resources.handle_br, resources.publisher))
    return True


def _publish_closed(resources: _SessionResources) -> None:
    if not resources.delivered or resources.publisher is None:
        return
    try:
        accepted = resources.publisher("CLOSED", None, None, None)
        if accepted is not True:
            log.warning("发布浏览器 CLOSED 状态被当前目标拒绝。")
    except Exception as exc:  # noqa: BLE001 - close remains best effort
        log.warning("发布浏览器 CLOSED 状态失败：%s", exc)


def cdp_browser_pid(br) -> int | None:
    """问 CDP 要真正在跑的那个 browser 进程 PID；取不到返回 None。"""
    if br is None:
        return None
    try:
        session = br.new_browser_cdp_session()
        info = session.send("SystemInfo.getProcessInfo")
    except Exception as exc:  # noqa: BLE001
        log.debug("通过 CDP 查询浏览器进程失败：%s", exc)
        return None
    for proc in info.get("processInfo", []):
        if proc.get("type") == "browser" and proc.get("id"):
            return int(proc["id"])
    return None


def close_session(pw, br, *, publish_browser=None) -> None:
    with _session_lock:
        resources = _session
        if resources is None:
            if any(old_pw is pw and old_br is br and old_pub is publish_browser
                   for old_pw, old_br, old_pub in _closed_handles):
                return
            raise RuntimeError("浏览器会话句柄不是当前活动会话")
        if (resources.handle_pw is not pw or resources.handle_br is not br
                or resources.publisher is not publish_browser):
            raise RuntimeError("浏览器会话句柄或状态发布器与当前活动会话不匹配")
        if not resources.delivered:
            raise RuntimeError("浏览器会话尚未完成建立")
        if resources.phase not in {"active", "pending"}:
            raise RuntimeError("浏览器会话正在关闭")
        resources.phase = "closing"

    _cleanup(resources)
    if resources.phase != "clean":
        return
    _publish_closed(resources)
    _release_if_clean(resources)


def navigate_detail(page, product_url: str, emit=None) -> OpenedDetail:
    """导航到详情页并交回页面句柄；等待、guard 与读取由 detail_visit 负责。"""
    if emit:
        m = re.search(r"/(?:offer|item)/(\d+)\.html", product_url)
        emit("detail_nav", offer_id=m.group(1) if m else None, phase="detail")
    page.goto(product_url, wait_until="domcontentloaded")
    return OpenedDetail(page, target_url=product_url)
