"""浏览器会话与详情页读取（Playwright 连接接管，ADR-0010）。

点击式列表的遍历、逐卡逻辑与 deny 账本在 click_listing.py，榜单页行为在 listing.py，
详情观测规则在 detail.py；这里只剩：

- 用普通进程拉起浏览器、经调试端口接管、按归属收尾（IS-43）；
- 逐店补采的详情导航 adapter（交回 `detail_visit.OpenedDetail`，见 ADR-0023）。

`DenyTracker` 与那两个 deny 异常现在长在 guard.py（deny 判定的家），这里替沿用旧 import 的
调用方转出来；详情页「打开之后怎么安顿」也归 `guard.ready_detail_page()`（候选 04）。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from . import browser_proc, listing
from .click_listing import WAIT_POPUP_MS
from .config import Config
from .delay import Humanizer
from .detail_visit import OpenedDetail
from .guard import (DenyTracker, RoundDenyExceeded,  # noqa: F401  旧名兼容
                    ShopDenyExceeded)
# 诊断工具仍按旧私有名读取这两个 guard 谓词。
from .guard import is_deny_url, is_punish_url
# 诊断工具的兼容名：它们按旧名从 browser_pw 取这些，别删（见下面的 `_*` 别名与工具用例）。
from .guard import (  # noqa: F401
    body_text, captcha_visible, intervention_kind, resolved, vtype,
)

log = logging.getLogger(__name__)

@dataclass
class _SessionResources:
    """One session's exact handles, acquired in establishment order."""

    token: object
    port: int
    publisher: Callable[..., bool] | None
    start_browser: bool
    proc: Any = None
    launch_proof: str | None = None
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
# A small identity cache makes repeated close a no-op without letting an old A
# close a newer B. Closed Playwright handles are retained only for this bound.
_closed_handles: deque[tuple[Any, Any, Callable[..., bool] | None]] = deque(maxlen=16)

# 兼容旧私有名/旧名（本文件内部与诊断工具仍引用）
_body_text = body_text
_captcha_visible = captcha_visible
_is_punish_url = is_punish_url
_is_deny_url = is_deny_url
_resolved = resolved
_vtype = vtype

# 条件等待的上限兜底（秒）——优先“等条件满足”，超时才继续，替代固定 sleep。
_WAIT_LAUNCH_SEC = 25.0    # 等浏览器调试端口可连接

# 搬到 listing.py 的榜单页原语：诊断工具仍按旧私有名引用，这里留兼容别名（同 guard 那组）。
# 本文件内部一律走 listing.*，别名只给工具用——否则测试打桩 listing 的改动会静默失效
# （2026-09-13 被这条咬过：测试改打 listing，采集路径却用别名，于是真的走进人工介入等待）。
_WAIT_POPUP_MS = WAIT_POPUP_MS
_PRODUCT_IMG_SEL = listing.PRODUCT_IMG_SEL
_click_text_in_frames = listing.click_text_in_frames


def open_session(cfg: Config, *, publish_browser=None):
    global _session
    _clear_pending_before_open()
    from playwright.sync_api import sync_playwright

    resources = _SessionResources(
        object(), int(cfg.attach_port), publish_browser,
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
        deadline = time.monotonic() + _WAIT_LAUNCH_SEC
        while time.monotonic() < deadline:
            try:
                resources.br = resources.pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{resources.port}")
                break
            except Exception as exc:  # noqa: BLE001 - retry until the readiness deadline
                last_exc = exc
                time.sleep(0.8)
        if resources.br is None:
            raise RuntimeError(
                f"无法连接浏览器调试端口 {resources.port}（{last_exc}）")

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
        _cleanup(resources)
        if publish_browser is not None and not resources.publication_failed:
            try:
                publish_browser("UNKNOWN", resources.port, None, None)
            except Exception as exc:  # noqa: BLE001 - preserve the establishment error
                log.warning("发布浏览器 UNKNOWN 状态失败：%s", exc)
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
    if proc is None or proc.poll() is not None:
        return "BORROWED", None, None
    browser_pid = cdp_browser_pid(resources.br)
    current_proof = (browser_proc.process_creation_proof(browser_pid)
                     if browser_pid is not None else None)
    if (browser_pid == proc.pid and resources.launch_proof is not None
            and current_proof == resources.launch_proof):
        return "OWNED", browser_pid, resources.launch_proof
    return "UNKNOWN", None, None


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
            if proc.poll() is not None:
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

    clean = resources.page is None and resources.br is None \
        and resources.pw is None and resources.proc is None
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


def navigate_detail(page, product_url: str, cfg: Config, emit=None) -> OpenedDetail:
    """导航到详情页并交回页面句柄；等待、guard 与读取由 detail_visit 负责。"""
    if emit:
        m = re.search(r"/(?:offer|item)/(\d+)\.html", product_url)
        emit("detail_nav", offer_id=m.group(1) if m else None, phase="detail")
    page.goto(product_url, wait_until="domcontentloaded")
    # 补采没有点击路径的响应监听，沿用旧路径的地址级 punish 判定信号。
    return OpenedDetail(page, punished=True)
