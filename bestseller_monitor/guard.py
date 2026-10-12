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
from urllib.parse import urlsplit

from . import sound
from . import waiting, stop_request
from .config import Config

log = logging.getLogger(__name__)

# 等待期自己的两个节拍：响铃循环的轮询间隔（秒），以及每几轮写一行进度。
# 3 秒一轮、上限 10 分钟——每轮一行会给一段等待留下两百行，抽成 5 约四十行。
WAIT_POLL_SEC = 3.0
WAIT_PROGRESS_EVERY = 5


class RoundPauseRequired(RuntimeError):
    """轮次必须暂停并保留进度，等待后续人工或环境恢复。"""


class PageEvidenceUnavailable(RoundPauseRequired):
    """页面读取不可靠；不能据此继续采集或宣称解除。"""


class InterventionTimeout(RoundPauseRequired):
    """人工验证或扫码未在配置时限内解决。"""


class IdentityVerificationTimeout(InterventionTimeout):
    """身份验证达到固定等待上限；原轮必须自动中止。"""


class ShopDenyExceeded(Exception):
    """某店滚动窗口内 deny 数达到阈值，跳过该店。"""


class RoundDenyExceeded(RoundPauseRequired):
    """整轮滚动窗口内 deny 数达到阈值，中止本轮。"""


# deny 事件编码（账本的落点；`db.deny_seed` 按同一份口径回读——续跑接着数，issue 13）
DENY_HIT_EVENT = "deny_hit"
DENY_COOLDOWN_EVENT = "deny_cooldown"


def emit_deny_cooldown(emit, shop_key: str, *, reason: str, seconds: float) -> None:
    """写一条整轮降温事件：首次 deny 与跳店共用同一份编码（ADR-0054）。"""
    if emit is None:
        return
    emit(DENY_COOLDOWN_EVENT, shop_key=shop_key, kind="deny",
         note=f"reason={reason}&sec={int(round(seconds))}")


class DenyTracker:
    """滚动窗口内的 deny 计数（按店 + 整轮）。

    点击式列表与逐店补采共用同一个实例：deny 是「这台机器正在被限流」的同一个信号，
    不能因为走哪条路而两种待遇（候选 04）。

    账本按轮次累计、跨续跑延续（ADR-0054）：构造时可给 `seed`（窗口内既有 deny 的
    `(时刻, 店铺)`，续跑入口从事件回读）与 `cooldown_fired`（本轮是否已发过首次降温）；
    窗口过期自然清零，不另设停机规则。`emit` 给事件面（`deny_hit` / `deny_cooldown`），
    `cooldown` 是可打断的睡眠（`Humanizer.sleep`），`None` = 不降温。
    """

    def __init__(self, window_sec: float, *, seed=(), cooldown_fired: bool = False,
                 emit=None, cooldown=None):
        self.window_sec = window_sec
        self.events: list[tuple[float, str]] = list(seed)
        self._cooldown_fired = bool(cooldown_fired)
        self._emit = emit
        self._cooldown = cooldown
        self._prune(time.time())

    def _prune(self, now: float) -> None:
        self.events = [(t, s) for t, s in self.events if now - t <= self.window_sec]

    def record(self, shop_key: str) -> None:
        now = time.time()
        self.events.append((now, shop_key))
        self._prune(now)
        if self._emit is not None:
            self._emit(DENY_HIT_EVENT, shop_key=shop_key, kind="deny",
                       note=f"shop_n={self.shop_count(shop_key)}&round_n={self.round_count()}")
        self._maybe_cool_down(shop_key)

    def _maybe_cool_down(self, shop_key: str) -> None:
        """整轮首次 deny 后停一段给站点降温；以事件为记忆（先写事件后睡），续跑不重复。"""
        if self._cooldown is None or self._cooldown_fired:
            return
        self._cooldown_fired = True
        seconds = float(self.window_sec)
        emit_deny_cooldown(self._emit, shop_key, reason="first_deny", seconds=seconds)
        log.info("整轮首次 deny：停 %.0f 分钟给站点降温（窗口随后重起）", seconds / 60)
        self._cooldown(seconds)

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
    if kind == "身份验证":
        return "identity"
    if kind == "登录墙":
        return "login"
    if kind in ("滑块", "物品识别", "图片验证"):
        return "slider"
    return "none"


# ---------- Playwright 页面读取 ----------
def body_text(page, *, strict: bool = False) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000) or ""
    except Exception:
        if strict:
            raise
        return ""


def captcha_visible(page, *, strict: bool = False) -> bool:
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
                if strict:
                    raise
                continue
        return False
    except Exception:
        if strict:
            raise
        return False


@dataclass(frozen=True)
class _PageEvidence:
    """页面上的三样证据：地址、正文、可见验证容器。"""

    url: str
    body: str
    captcha: bool
    identity_page: bool = False
    business_page: bool = False


def _page_evidence(page) -> _PageEvidence:
    """读一次页面上的三样证据。

    判据与它的解除判定都从这一份读起（ADR-0030）。各读各的就会给出互相矛盾的答案，
    而「要人工介入」与「介入已经解除」本来就是同一件事的两种读法。

    业务页标记表前段是老前端的类名（兼容老页面与归档夹具），后段是 2026-10-11 真机取样
    验明的当前前端模块标记——旧表在真机详情页 0 命中（`.sku-selection` 在 air.1688.com
    的 iframe 里、顶层看不见），正常详情页曾被判「页面上下文未知」停轮；回归见
    `tests/fixtures/offer-detail-gallery.html`。
    """
    try:
        context = page.evaluate(r"""() => {
            const visible = e => !!e && e.getClientRects().length > 0;
            const anyVisible = sel => [...document.querySelectorAll(sel)].some(visible);
            return {
                loaded: document.readyState !== 'loading' && !!document.body,
                identity: /^(?:阿里巴巴集团\s*[|｜]\s*)?(?:身份验证|登录验证)$/.test(document.title.trim()) ||
                    anyVisible('form[name="validateParams"]'),
                business: anyVisible('#offer-detail, .offer-detail, #mod-detail-title, '
                    + '.mod-detail-title, .detail-gallery, .sku-selection, img.main-picture, '
                    + '.sm-offer-list, .offer-list, .no-result, .empty-result, '
                    + '#gallery, [data-module="od_picture_gallery"], .module-od-picture-gallery')
            };
        }""")
        # 先看文档在不在：不在就是还没到，不必再读正文与验证容器——无 body 时
        # `inner_text` 会挂起等挂载（至多 3 秒），就绪窗口里每个未就绪探针都会白付这笔等待。
        if not context["loaded"]:
            raise PageEvidenceUnavailable("页面尚未可靠加载，不能确认业务状态")
        text = body_text(page, strict=True)
        if not text.strip():
            raise PageEvidenceUnavailable("页面尚未可靠加载，不能确认业务状态")
        captcha = captcha_visible(page, strict=True)
        return _PageEvidence(page.url or "", text, captcha,
                             context["identity"], context["business"])
    except PageEvidenceUnavailable:
        raise
    except Exception as exc:
        raise PageEvidenceUnavailable("页面证据读取失败，不能确认业务状态") from exc


@dataclass(frozen=True)
class Intervention:
    """一次人工介入判定：是什么，以及判据是从哪一支来的。

    `criterion` 是事后能核对的那一半。现场只留下一句「滑块」时，分不出是地址撞上 punish、
    正文命中了哪个词、还是只看到了验证容器——三种情况的后续处置不同（issue 12）。
    """

    kind: str
    criterion: str


def _intervention_detail(evidence: _PageEvidence) -> Intervention | None:
    """三样证据 → 要不要人工介入，以及判据来源。纯函数，判据只有这一份。"""
    url = evidence.url.lower()
    if (evidence.identity_page and
            "为确认是你本人操作，请完成以下验证" in evidence.body):
        return Intervention("身份验证", "identity-page")
    if is_login_url(url):
        return Intervention("登录墙", "url:login")
    if is_punish_url(url):
        return Intervention("滑块", "url:punish")
    for m in SLIDER_MARKERS:
        if m in evidence.body:
            return Intervention("滑块", f"slider-body:{m}")
    if evidence.captcha:
        # 可见验证容器：带文案的页上面正文支就已经判走；这一支只对「有容器、无表内文案」的页
        # 做「点我反馈」豁免——有它（纯反爬拦截页的记号）不算滑块。正文支先于本支的次序由
        # test_guard 的 captcha=True 用例钉着（ADR-0036）。
        if "点我反馈" not in evidence.body:
            return Intervention("滑块", "captcha-container")
    for m in LOGIN_MARKERS:
        if m in evidence.body and len(evidence.body) < 3000:
            return Intervention("登录墙", f"login-body:{m}")
    return None


def _intervention_of(evidence: _PageEvidence) -> str | None:
    """三样证据 → 需不需要人工介入。判定的薄壳：判据在 `_intervention_detail` 里，只有那一份。"""
    found = _intervention_detail(evidence)
    return found.kind if found else None


def intervention_detail(page) -> Intervention | None:
    """判定 + 判据来源：与 `intervention_kind` 同一份证据、同一次判读。

    要「事后能核对」的调用方走这一条——`kind` 与 `criterion` 一起交出来；只要类型的调用方
    走 `intervention_kind`。同一页不许两个入口各来一次：那就是把同一页读两遍，也正是
    ADR-0030 要收掉的那种「两套读法」。
    """
    evidence = _page_evidence(page)
    found = _intervention_detail(evidence)
    if found is None and (evidence.identity_page or not evidence.business_page):
        raise PageEvidenceUnavailable("页面上下文未知，不能确认业务页面可用")
    return found


def intervention_kind(page) -> str | None:
    """判定是否需要人工介入。仅看验证据信号，绝不因“没有商品”误判。

    证据只有页面上的三样：地址、正文、可见验证容器。响应流里那份 punish 信号曾经也占一个
    形参，但读它的那行与地址判定是同一条谓词，对任何输入都到不了，已整个撤掉（ADR-0029）。

    判据只有这一份：`resolved` 是它的另一种读法，不再自己认一遍证据（ADR-0030）。
    需要判据来源的调用方用 `intervention_detail`，不要两个都调。
    """
    found = intervention_detail(page)
    return found.kind if found else None


def resolved(page, *, target_url: str | None = None) -> bool:
    """读取成功、全部介入消失并有可用业务页证据，才算真正解除。"""
    try:
        evidence = _page_evidence(page)
        if target_url is not None:
            target = urlsplit(target_url)
            current = urlsplit(evidence.url)
            if (target.hostname, target.path.split('/_____tmd_____')[0].rstrip('/')) != (
                    current.hostname, current.path.split('/_____tmd_____')[0].rstrip('/')):
                return False
        return (evidence.business_page and not evidence.identity_page
                and not is_deny_url(evidence.url)
                and _intervention_of(evidence) is None)
    except PageEvidenceUnavailable:
        return False


def ready_detail_page(page, cfg: Config, *, emit=None,
                      deny_tracker: DenyTracker | None = None,
                      shop_key: str | None = None,
                      target_url: str | None = None) -> bool:
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
    found = intervention_detail(page)
    if found:
        wait_for_resolution(page, cfg.human_pause_minutes, emit=emit,
                            verification_type=vtype(found.kind),
                            confirm_sec=cfg.intervention_confirmation_sec,
                            target_url=target_url, criterion=found.criterion)
    return False


def wait_for_resolution(page, minutes: int, emit=None, verification_type: str | None = None,
                        confirm_sec: float = 2.0, *, target_url: str | None = None,
                        criterion: str | None = None) -> None:
    """需要人工介入时：先过确认窗口过滤瞬时报错信号，再持续响铃直到解决。

    两段等待都走 `waiting.until`：每一轮都问一次「该不该停」（ADR-0009）——这时用户最
    可能去按界面上的暂停，而最长可等 human_pause_minutes 分钟，不打断就会把停止拖成十分钟。
    取消钩子由原语默认接上（与睡眠分片同一个判据），本模块不再手写检查点。

    `criterion` 是这一支判据的来源（`intervention_detail` 交出来的那一半）。它和页面地址一起
    进事件备注与进度行：现场那 2 分 20 秒事后只剩一句「滑块」，等的是哪一页、为什么判成介入
    都查不到（issue 12）。
    """
    vtype_name = verification_type or "slider"
    target_url = (target_url or page.url) if vtype_name == "identity" else None

    def where() -> str:
        """写下这一刻页面在哪个地址。

        每次读、不缓存：等待期间页面会被刷新、也可能被站点换掉，而「程序在等的页」与
        「人正在操作的页」是不是同一份，正是现场要靠地址对齐的那件事（issue 12）。

        读不到就给空串：这段只喂日志与事件，**不许把等待弄断**——观测的活儿不配改变控制流。
        """
        try:
            return page.url or ""
        except Exception:
            return ""

    deadline = None
    def probe():
        if vtype_name == "identity":
            stop_request.check()  # A user stop wins before even a now-readable page.
            if deadline is not None and time.monotonic() > deadline:
                return False
            usable = resolved(page, target_url=target_url)
            return usable and (deadline is None or time.monotonic() <= deadline)
        return resolved(page)
    # 确认窗口：短暂出现又自行消失的信号（如 tmd/x5sec 上报）不算真正的人工介入
    if waiting.until(probe, timeout_sec=confirm_sec, poll_sec=0.3):
        log.debug("人工介入信号瞬时就消失，判定为误报，忽略")
        return
    # 超过确认窗口仍未解决 => 确认为真正需要人工介入
    # 先尝试一次刷新：反爬拦截页/瞬时 block 常可通过刷新解除，刷新后恢复则不响铃
    if vtype_name != "identity":
        refreshed = True
        try:
            page.reload(wait_until="domcontentloaded")
            time.sleep(1.5)
        except Exception as exc:
            refreshed = False
            # 刷新只是「试一试」：没做成照旧进等待，但这一步不能无声无息（issue 12 票 02）。
            log.debug("等待前的刷新没做成（%s）：%s", exc, where())
        if resolved(page):
            log.info("刷新后已恢复，忽略（原为瞬时报错/反爬拦截）：%s", where())
            return
        if refreshed:
            log.info("刷新未解除，转入等待人工处理：%s", where())
    deadline = time.monotonic() + minutes * 60
    appear_ts = time.time()
    log.warning("检测到需要人工介入，请在浏览器窗口处理（持续响铃直到解决）……最长 %s 分钟", minutes)
    if emit:
        emit("verification_appear", kind="verification", verification_type=vtype_name,
             note=_wait_note(f"type={vtype_name};wait_minutes={minutes}", where(), criterion))
    ticks = 0
    def ring() -> None:
        nonlocal ticks
        ticks += 1
        if ticks % WAIT_PROGRESS_EVERY == 0:
            log.info("仍在等待人工处理（已等 %.0f 秒）：%s", time.time() - appear_ts, where())
        sound.play_alarm(count=1)   # 每次约 1 秒，循环播放

    timeout_sec = (max(0.0, deadline - time.monotonic())
                   if vtype_name == "identity" else minutes * 60)
    if waiting.until(probe, timeout_sec=timeout_sec, poll_sec=WAIT_POLL_SEC,
                     on_wait=ring) is None:
        if vtype_name == "identity":
            stop_request.check()
            if emit:
                emit("verification_timeout", kind="verification", verification_type="identity",
                     note=_wait_note("reason=IDENTITY_VERIFICATION_TIMEOUT", where(), criterion))
            raise IdentityVerificationTimeout("身份验证等待超时，本轮自动中止，已保存数据保留")
        raise InterventionTimeout("人工介入超时")
    if emit:
        emit("verification_solved", kind="verification", verification_type=vtype_name,
             note=_wait_note(f"resolution_seconds={time.time() - appear_ts:.1f}",
                             where(), criterion))
    log.info("人工介入已解决，停止响铃，继续。")


def _wait_note(base: str, page_url: str, criterion: str | None) -> str:
    """等待事件的备注：既有字段在前，页面地址与判据来源追加在后。

    备注的既有读法按 `;` 分片、`=` 取值（界面的身份验证倒计时就这么读），所以追加的这段
    不能改变前面那段的位置与写法。

    地址与判据都按原样写入，不做转义：地址里真出现 `;`（矩阵参数那种）时，切出来的碎片
    大多不含 `=`，会被读法直接丢掉；含 `=` 的碎片会落成一个谁都不读的键。既不覆盖既有键，
    也不改变既有键的位置。
    """
    note = f"{base};url={page_url}"
    if criterion:
        note = f"{note};criterion={criterion}"
    return note
