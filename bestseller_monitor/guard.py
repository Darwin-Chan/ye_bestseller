"""验证/拦截页识别、deny 账目与人工介入（统一口径，供多个驱动/路径复用）。

目标：所有「是否滑块 / 是否登录墙 / 是否 deny 限流 / 是否真 punish 页」的判定，
「打开一个详情页之后怎么安顿它」，以及人工介入的「确认窗口 + 刷新兜底 + 持续响铃」等待逻辑，
都收敛到本模块。采集的两条路径（点击式列表、逐店补采）与诊断工具都从这一处取用
（工具经 browser_pw 的兼容别名转发），避免各处口径不一致。
"""
from __future__ import annotations

import logging
import time

from . import sound
from . import stop_request
from .config import Config

log = logging.getLogger(__name__)


class RoundPauseRequired(RuntimeError):
    """轮次必须暂停并保留进度，等待后续人工或环境恢复。"""


class InterventionTimeout(RoundPauseRequired):
    """人工验证或扫码未在配置时限内解决。"""


class ShopDenyExceeded(Exception):
    """某店滚动窗口内 deny 数达到阈值，跳过该店。"""


class RoundDenyExceeded(RoundPauseRequired):
    """整轮滚动窗口内 deny 数达到阈值，中止本轮。"""


class DenyTracker:
    """滚动窗口内的 deny 计数（按店 + 整轮）。

    点击式列表与逐店补采共用同一个实例：deny 是「这台机器正在被限流」的同一个信号，
    不能因为走哪条路而两种待遇（候选 04）。
    """

    def __init__(self, window_sec: float):
        self.window_sec = window_sec
        self.events: list[tuple[float, str]] = []

    def _prune(self, now: float) -> None:
        self.events = [(t, s) for t, s in self.events if now - t <= self.window_sec]

    def record(self, shop_key: str) -> None:
        now = time.time()
        self.events.append((now, shop_key))
        self._prune(now)

    def shop_count(self, shop_key: str) -> int:
        self._prune(time.time())
        return sum(1 for t, s in self.events if s == shop_key)

    def round_count(self) -> int:
        self._prune(time.time())
        return len(self.events)


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


def ready_detail_page(page, cfg: Config, *, emit=None, punished: bool = False,
                      deny_tracker: DenyTracker | None = None,
                      shop_key: str | None = None) -> bool:
    """一个刚打开的详情页能不能用：先认 deny（记账 + 阈值），不是 deny 才判人工介入并等人解决。

    两条详情访问（点击式列表的弹窗、逐店补采的详情页）打开页面之后都先走这里，「deny 该不该
    退避、滑块要不要响铃等人」只判一次（候选 04）。顺序是有意的：deny 页是自动限流，该退避，
    不该当成滑块去响铃等人（`is_deny_url` 的注释就是这么写的）。

    交回 `True` 表示这次落在 deny 页上（记账已经做完；阈值越界会抛下面两个异常），
    `False` 表示页面可用——需要人工介入的话，已经等人解决完了。
    """
    if is_deny_url(page.url or ""):
        if deny_tracker is not None and shop_key is not None:
            deny_tracker.record(shop_key)
            shop_denies = deny_tracker.shop_count(shop_key)
            log.warning("店铺 %s 命中 deny 页（窗口内第 %s 次，整轮第 %s 次）",
                        shop_key, shop_denies, deny_tracker.round_count())
            if deny_tracker.round_count() >= cfg.deny_round_limit:
                raise RoundDenyExceeded(
                    f"整轮 {cfg.deny_window_minutes} 分钟内"
                    f" deny≥{cfg.deny_round_limit}")
            if shop_denies >= cfg.deny_shop_limit:
                raise ShopDenyExceeded(
                    f"店铺 {shop_key} {cfg.deny_window_minutes} 分钟内"
                    f" deny≥{cfg.deny_shop_limit}")
        return True
    kind = intervention_kind(page, punished)
    if kind:
        wait_for_resolution(page, cfg.human_pause_minutes, emit=emit,
                            verification_type=vtype(kind),
                            confirm_sec=cfg.intervention_confirmation_sec)
    return False


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
