"""验证/拦截页识别、deny 账目与人工介入（统一口径，供多个驱动/路径复用）。

目标：所有「是否滑块 / 是否登录墙 / 是否 deny 限流 / 是否真 punish 页」的判定，
「打开一个详情页之后怎么安顿它」，以及人工介入的「确认窗口 + 刷新兜底 + 持续响铃」等待逻辑，
都收敛到本模块。采集的两条路径（点击式列表、逐店补采）与诊断工具都从这一处取用
（工具经 browser_pw 的兼容别名转发），避免各处口径不一致。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import sound
from . import waiting
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


# 滑块/验证文案（含 punish，用于页面正文命中）。
# 末两词按 2026-09 起存档的「验证码拦截」页样本补入（53 份，见 ADR-0036）：该页顶着正常
# 详情 URL，正文只有这两句拖动指引，原表无一命中，判定给 None，页面被静默记成解析失败。
SLIDER_MARKERS = ("向右滑动验证", "请完成验证", "滑块验证", "拖动滑块", "安全验证",
                  "请按住滑块", "拖动下方滑块", "punish")
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


@dataclass(frozen=True)
class _PageEvidence:
    """页面上的三样证据：地址、正文、可见验证容器。"""

    url: str
    body: str
    captcha: bool


def _page_evidence(page) -> _PageEvidence:
    """读一次页面上的三样证据。

    判据与它的解除判定都从这一份读起（ADR-0030）。各读各的就会给出互相矛盾的答案，
    而「要人工介入」与「介入已经解除」本来就是同一件事的两种读法。
    """
    return _PageEvidence(page.url or "", body_text(page), captcha_visible(page))


def _intervention_of(evidence: _PageEvidence) -> str | None:
    """三样证据 → 需不需要人工介入。纯函数，判据只有这一份。"""
    url = evidence.url.lower()
    if is_login_url(url):
        return "登录墙"
    if is_punish_url(url):
        return "滑块"
    for m in SLIDER_MARKERS:
        if m in evidence.body:
            return "滑块"
    if evidence.captcha:
        # 可见验证容器：带文案的页上面正文支就已经判走；这一支只对「有容器、无表内文案」的页
        # 做「点我反馈」豁免——有它（纯反爬拦截页的记号）不算滑块。正文支先于本支的次序由
        # test_guard 的 captcha=True 用例钉着（ADR-0036）。
        if "点我反馈" not in evidence.body:
            return "滑块"
    for m in LOGIN_MARKERS:
        if m in evidence.body and len(evidence.body) < 3000:
            return "登录墙"
    return None


def intervention_kind(page) -> str | None:
    """判定是否需要人工介入。仅看验证据信号，绝不因“没有商品”误判。

    证据只有页面上的三样：地址、正文、可见验证容器。响应流里那份 punish 信号曾经也占一个
    形参，但读它的那行与地址判定是同一条谓词，对任何输入都到不了，已整个撤掉（ADR-0029）。

    判据只有这一份：`resolved` 是它的另一种读法，不再自己认一遍证据（ADR-0030）。
    """
    return _intervention_of(_page_evidence(page))


def resolved(page) -> bool:
    """人工介入是否已经解除：同一次判定不再认得这个页面上的任何信号。

    改前这是一份独立的判据，只读地址与可见验证容器、看不见正文。于是「只由正文命中」的
    那一幕里，它与 `intervention_kind` 同时给出「要介入」和「已解决」两个矛盾答案：详情读取
    循环的每一步都在等一个永远不为假的判据，而确认窗口拿这个「已解决」把自己判成误报
    （ADR-0030）。

    读**不出**证据时回 `False`（还没解除、继续等）——与 `intervention_kind` 上抛是有意的
    不同：那是判据，异常交给调用方；这是轮询谓词，「读不到」只说明还不能停。注意这说的是
    **读证据出错**：`body_text` / `captcha_visible` 自己把异常吞成空证据，那种情况判据给
    `None`、这里便是 `True`，与「页面确实没有信号」不可分。这层区分先于本条存在（改前的
    `resolved` 也这样），记在 ADR-0030 的挂账里。
    """
    try:
        return _intervention_of(_page_evidence(page)) is None
    except Exception:
        return False


def ready_detail_page(page, cfg: Config, *, emit=None,
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
    kind = intervention_kind(page)
    if kind:
        wait_for_resolution(page, cfg.human_pause_minutes, emit=emit,
                            verification_type=vtype(kind),
                            confirm_sec=cfg.intervention_confirmation_sec)
    return False


def wait_for_resolution(page, minutes: int, emit=None, verification_type: str | None = None,
                        confirm_sec: float = 2.0) -> None:
    """需要人工介入时：先过确认窗口过滤瞬时报错信号，再持续响铃直到解决。

    两段等待都走 `waiting.until`：每一轮都问一次「该不该停」（ADR-0009）——这时用户最
    可能去按界面上的暂停，而最长可等 human_pause_minutes 分钟，不打断就会把停止拖成十分钟。
    取消钩子由原语默认接上（与睡眠分片同一个判据），本模块不再手写检查点。
    """
    vtype_name = verification_type or "slider"
    # 确认窗口：短暂出现又自行消失的信号（如 tmd/x5sec 上报）不算真正的人工介入
    if waiting.until(lambda: resolved(page), timeout_sec=confirm_sec, poll_sec=0.3):
        log.debug("人工介入信号瞬时就消失，判定为误报，忽略")
        return
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

    def ring() -> None:
        sound.play_alarm(count=1)   # 每次约 1 秒，循环播放

    if waiting.until(lambda: resolved(page), timeout_sec=minutes * 60, poll_sec=3,
                     on_wait=ring) is None:
        raise InterventionTimeout("人工介入超时")
    if emit:
        emit("verification_solved", kind="verification", verification_type=vtype_name,
             note=f"resolution_seconds={time.time() - appear_ts:.1f}")
    log.info("人工介入已解决，停止响铃，继续。")
