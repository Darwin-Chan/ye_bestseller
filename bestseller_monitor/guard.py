"""验证/拦截页识别与人工介入（统一口径，供多个驱动/路径复用）。

目标：所有「是否滑块 / 是否登录墙 / 是否 deny 限流 / 是否真 punish 页」的判定，
以及人工介入的「确认窗口 + 刷新兜底 + 持续响铃」等待逻辑，都收敛到本模块。
browser_pw / browser_dp / listing / detail 调用同一套常量与判定，避免各处口径不一致。
"""
from __future__ import annotations

import logging
import time

from . import sound
from . import stop_request

log = logging.getLogger(__name__)


class RoundPauseRequired(RuntimeError):
    """轮次必须暂停并保留进度，等待后续人工或环境恢复。"""


class InterventionTimeout(RoundPauseRequired):
    """人工验证或扫码未在配置时限内解决。"""


# 滑块/验证文案（含 punish，用于页面正文命中）
SLIDER_MARKERS = ("向右滑动验证", "请完成验证", "滑块验证", "拖动滑块", "安全验证", "punish")
# 登录相关文案
LOGIN_MARKERS = ("登录后查看", "请登录", "扫码登录", "确认登录", "快速进入")
# deny / 反爬拦截页关键词
DENY_KEYWORDS = ("bsop-punish", "deny_pc")


# ---------- URL 判定（纯字符串，各驱动可复用） ----------
def is_login_url(url: str) -> bool:
    u = (url or "").lower()
    return "login.taobao" in u or "login.1688" in u


def is_punish_url(url: str) -> bool:
    """真实验证页/验证请求：punish 页或 punishTextFetch。普通 tmd report/x5sec 上报不算。"""
    u = (url or "").lower()
    # 站点会在正常详情 URL 后追加 /_____tmd_____/punish?x5secdata=... 的上报装饰，不算真验证
    if "_____tmd_____" in u:
        return False
    return "punishtextfetch" in u or "/punish?" in u or "/punish/" in u


def is_deny_url(url: str) -> bool:
    """淘宝 deny/验证拦截页（bsop-punish / deny_pc），连续高频访问触发的反爬限流。
    这类不该响铃等人扫码，而应自动降速退避。"""
    u = (url or "").lower()
    return any(k in u for k in DENY_KEYWORDS)


def vtype(kind: str | None) -> str:
    """把『滑块/登录墙』这类文案映射为相对稳定的验证类型。"""
    if kind == "登录墙":
        return "login"
    if kind in ("滑块", "物品识别", "图片验证"):
        return "slider"
    return "none"


# ---------- Playwright 页面读取 ----------
def body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000) or ""
    except Exception:
        return ""


def captcha_visible(page) -> bool:
    # 只针对特征明确的验证容器/iframe，避免普通元素误判
    sel = ("iframe[src*='captcha' i], iframe[src*='nocaptcha' i], iframe[src*='secaptcha' i], "
           "#nc_1_wrapper, #nc_1_container, .nc-container, .nc_scale, #nc_1_n1z, "
           "#baxia-dialog-content, .baxia-dialog, [class*='baxia-dialog'], "
           "#nocaptcha, [class*='verify_'], [class*='captcha']")
    try:
        loc = page.locator(sel)
        n = loc.count()
        for i in range(min(n, 40)):
            try:
                if loc.nth(i).is_visible():
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def intervention_kind(page, punished: bool = False) -> str | None:
    """判定是否需要人工介入。仅看验证据信号，绝不因“没有商品”误判。"""
    url = (page.url or "").lower()
    if is_login_url(url):
        return "登录墙"
    if is_punish_url(url):
        return "滑块"
    body = body_text(page)
    for m in SLIDER_MARKERS:
        if m in body:
            return "滑块"
    if captcha_visible(page):
        # 可见验证容器：只有当页面确实带验证文案，或不是“点我反馈”这种纯反爬拦截页时，才算滑块
        if any(m in body for m in SLIDER_MARKERS) or ("点我反馈" not in body):
            return "滑块"
    for m in LOGIN_MARKERS:
        if m in body and len(body) < 3000:
            return "登录墙"
    # 仅当 URL 是真 punish 页且页面确实“像验证”时，才兜底判滑块（避免裸 URL 误报）
    if punished and is_punish_url(url):
        return "滑块"
    return None


def resolved(page) -> bool:
    """解决判定：验证弹窗/iframe 不再可见，且不处于登录墙，即认为已解决。"""
    try:
        url = (page.url or "").lower()
        if is_login_url(url):
            return False
        if is_punish_url(url):
            return False
        return not captcha_visible(page)
    except Exception:
        return False


def deny_resolved(page) -> bool:
    """deny 界面是否已解除：URL 不再是 deny 页即可认为解除（用户扫码后页面会离开 deny）。"""
    return not is_deny_url(page.url or "")


def wait_for_resolution(page, minutes: int, emit=None, verification_type: str | None = None,
                        confirm_sec: float = 2.0) -> None:
    """需要人工介入时：先过确认窗口过滤瞬时报错信号，再持续响铃直到解决。

    每一轮都问一次「该不该停」（ADR-0009）：这时用户最可能去按界面上的暂停，
    而最长可等 human_pause_minutes 分钟，不打断就会把停止拖成十分钟。
    """
    vtype_name = verification_type or "slider"
    # 确认窗口：短暂出现又自行消失的信号（如 tmd/x5sec 上报）不算真正的人工介入
    confirm_deadline = time.time() + max(0.0, confirm_sec)
    while time.time() < confirm_deadline:
        stop_request.check()
        if resolved(page):
            log.debug("人工介入信号瞬时就消失，判定为误报，忽略")
            return
        time.sleep(0.3)
    # 超过确认窗口仍未解决 => 确认为真正需要人工介入
    # 先尝试一次刷新：反爬拦截页/瞬时 block 常可通过刷新解除，刷新后恢复则不响铃
    try:
        page.reload(wait_until="domcontentloaded")
        time.sleep(1.5)
    except Exception:
        pass
    if resolved(page):
        log.info("刷新后已恢复，忽略（原为瞬时报错/反爬拦截）")
        return
    log.warning("检测到需要人工介入，请在浏览器窗口处理（持续响铃直到解决）……最长 %s 分钟", minutes)
    if emit:
        emit("verification_appear", kind="verification", verification_type=vtype_name,
             note=f"type={vtype_name}")
    appear_ts = time.time()
    deadline = time.time() + minutes * 60
    while True:
        stop_request.check()
        if resolved(page):
            if emit:
                emit("verification_solved", kind="verification", verification_type=vtype_name,
                     note=f"resolution_seconds={time.time() - appear_ts:.1f}")
            log.info("人工介入已解决，停止响铃，继续。")
            return
        if time.time() > deadline:
            raise InterventionTimeout("人工介入超时")
        sound.play_alarm(count=1)   # 每次约 1 秒，循环播放
        time.sleep(3)


# ---------- 兼容旧接口（listing / detail / browser_dp 用） ----------
def detect(page) -> str | None:
    return intervention_kind(page)


def wait_for_human(page, kind, minutes, confirm_sec: float = 2.0) -> None:
    wait_for_resolution(page, minutes, verification_type=vtype(kind), confirm_sec=confirm_sec)
