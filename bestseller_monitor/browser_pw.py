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
import time

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

# 记录本次由 open_session 启动的浏览器进程与调试端口：
# 收尾只结束本任务启动的浏览器，绝不波及用户其它 Edge 窗口。
_launched_proc = None
_launched_port = None
_launched_os_started = None

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
    global _launched_proc, _launched_port, _launched_os_started
    from playwright.sync_api import sync_playwright

    edge = cfg.chrome_path or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    proc = None
    launch_proof = None
    if publish_browser is not None and getattr(cfg, "start_browser", True):
        publish_browser("STARTING", cfg.attach_port, None, None)
    if getattr(cfg, "start_browser", True) and os.path.exists(edge):
        proc = subprocess.Popen([
            edge,
            f"--remote-debugging-port={cfg.attach_port}",
            f"--user-data-dir={cfg.user_data_path}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ])
        launch_proof = browser_proc.process_creation_proof(proc.pid)
        log.info("已用普通进程启动浏览器（调试端口 %s，PID %s）。", cfg.attach_port, proc.pid)
    _launched_proc = proc
    _launched_port = cfg.attach_port
    _launched_os_started = launch_proof

    pw = sync_playwright().start()
    # 用「能连上调试端口」作为浏览器就绪条件，替代固定 8 秒（超时兜底）
    br = None
    last_exc: Exception | None = None
    deadline = time.time() + _WAIT_LAUNCH_SEC
    while time.time() < deadline:
        try:
            br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cfg.attach_port}")
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(0.8)
    if br is None:
        try:
            pw.stop()
        except Exception:
            pass
        _abandon_launched_browser()
        if publish_browser is not None:
            publish_browser("UNKNOWN", cfg.attach_port, None, None)
        raise RuntimeError(f"无法连接浏览器调试端口 {cfg.attach_port}（{last_exc}）")
    ctx = br.contexts[0]
    page = ctx.new_page()
    page.set_default_timeout(cfg.timeout_ms)
    if publish_browser is not None:
        browser_pid = cdp_browser_pid(br)
        if getattr(cfg, "start_browser", True) and browser_pid:
            launch_proof = launch_proof or browser_proc.process_creation_proof(browser_pid)
        if getattr(cfg, "start_browser", True) and browser_pid and launch_proof:
            publish_browser("OWNED", cfg.attach_port, browser_pid, launch_proof)
        elif not getattr(cfg, "start_browser", True):
            publish_browser("BORROWED", cfg.attach_port, None, None)
        else:
            publish_browser("UNKNOWN", cfg.attach_port, None, None)
    return pw, br, page, ctx


def _abandon_launched_browser() -> None:
    """连不上调试端口、会话没能建立时，收掉本次由我们拉起的浏览器进程。

    只结束「我们拉起、而且现在还活着」的那一个（`proc.poll() is None`）。同一 profile
    已经有实例时，本次 msedge.exe 交接后立刻退出、`poll()` 有值——这时端口上那个是用户
    自己的浏览器，绝不能按端口占用者去关它。
    """
    global _launched_proc, _launched_port, _launched_os_started
    proc = _launched_proc
    _launched_proc = None
    _launched_port = None
    _launched_os_started = None
    if proc is None:
        return
    if proc.poll() is None:
        browser_proc.terminate_process_tree(proc.pid)
    else:
        log.info("本次启动的浏览器进程（PID %s）已退出（交接给既有实例），无需收尾。", proc.pid)


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
    global _launched_proc, _launched_port, _launched_os_started
    # 先问 CDP，再断开：交接场景里这个 PID 才是真正在跑的浏览器。
    browser_pid = cdp_browser_pid(br)
    try:
        br.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
    proc = _launched_proc
    port = _launched_port
    launch_proof = _launched_os_started
    _launched_proc = None
    _launched_port = None
    _launched_os_started = None
    if proc is None:
        log.info("本次未启动浏览器（接管既有实例），跳过关闭。")
        if publish_browser is not None:
            publish_browser("CLOSED", port, browser_pid, launch_proof)
        return
    own_pid = proc.pid if proc.poll() is None else None
    if own_pid is None:
        log.warning("本次启动的浏览器进程（PID %s）已退出：同一 profile 已有实例时会交接给旧实例；"
                    "改按调试端口 %s 的归属关闭。", proc.pid, port)
    browser_proc.close_browser(port, launched_by_us=True,
                               browser_pid=browser_pid, own_pid=own_pid)
    if publish_browser is not None:
        publish_browser("CLOSED", port, browser_pid, launch_proof)


def navigate_detail(page, product_url: str, cfg: Config, emit=None) -> OpenedDetail:
    """导航到详情页并交回页面句柄；等待、guard 与读取由 detail_visit 负责。"""
    if emit:
        m = re.search(r"/(?:offer|item)/(\d+)\.html", product_url)
        emit("detail_nav", offer_id=m.group(1) if m else None, phase="detail")
    page.goto(product_url, wait_until="domcontentloaded")
    # 补采没有点击路径的响应监听，沿用旧路径的地址级 punish 判定信号。
    return OpenedDetail(page, punished=True)
