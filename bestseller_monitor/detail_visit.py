"""详情访问的共享 seam：取得、安顿并读取一次当前详情观测。"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError

from . import detail, raw_pages, waiting
from .config import Config
from .guard import (DenyTracker, PageEvidenceUnavailable, RoundDenyExceeded, ShopDenyExceeded,
                    intervention_kind, is_deny_url, ready_detail_page)

log = logging.getLogger(__name__)

DETAIL_READY_TIMEOUT_SEC = 10.0
DETAIL_READY_POLL_SEC = 0.25
BROWSER_IO_ERRORS = (PlaywrightError, TimeoutError, OSError, ConnectionError)


@dataclass(frozen=True)
class OpenedDetail:
    """一个 adapter 已取得的详情页；页面仍归取得它的调用方关闭。

    交页面与访问意图：adapter 不替判据交证据——判据读的是页面上的地址、正文与可见验证容器（ADR-0029）。
    """

    page: object
    target_url: str | None = None  # 访问意图，不是重定向后的页面证据。


@dataclass(frozen=True)
class NotOpenedVisit:
    """点击 adapter 没有打开弹窗。"""


@dataclass(frozen=True)
class ReadFailedVisit:
    """取得详情页时的普通浏览器 I/O 失败。"""

    error: Exception
    observation: detail.Observation


@dataclass(frozen=True)
class DeniedVisit:
    """详情页已确认是 deny；账目与阈值已由 guard 处理。

    `raw_path` 是这次命中留下的原始页面存档路径（相对 `raw_page_dir`；拿不到页面时为 `None`）——
    交回调用方，让它把路径写进事件备注（ADR-0057）。
    """

    raw_html: str = ""
    raw_path: str | None = None


@dataclass
class ReadyDetailVisit:
    """已通过初次 guard、可读取一次当前详情页的 capability。"""

    _page: object
    _cfg: Config
    _emit: object = None
    _deny_tracker: DenyTracker | None = None
    _shop_key: str | None = None
    _target_url: str | None = None
    _observed: bool = False
    #: 命中人工介入时把页面交回调用方留证：`(page, vtype, criterion) -> 存档相对路径 | None`。
    _on_intervention: object = None
    #: 命中 deny 时留证：`(html) -> 存档相对路径 | None`（来源与身份已由调用方闭包捕获）。
    _archive_deny: object = None

    def observe(self, product_url: str) -> detail.Observation | DeniedVisit:
        """等待 guard 后的页面可读，再翻译当前 HTML；同一 capability 只能调用一次。

        等待走 `waiting.until`（ADR-0042）：探针每轮做「读地址 / 介入判定 / 读正文」
        的全套，返回终值（观测或 deny 结果）或 `None`（还不可读，继续等）。人工介入
        被 guard 解决后探针交回 `waiting.EXTEND`——本次窗口到此，外层重开一个完整
        `DETAIL_READY_TIMEOUT_SEC` 新窗（重置不进原语，只在这里的外层循环里）。等待
        期间收到停止请求时异常原样上抛：不落 `read_failed`，也不给「没真的读完的那次」
        记账（ADR-0009；spec §5 的那处行为变化）。
        """
        if self._observed:
            raise RuntimeError("ReadyDetailVisit.observe() 只能调用一次")
        self._observed = True
        self._target_url = product_url

        def probe():
            try:
                denied = is_deny_url(self._page.url or "")
            except BROWSER_IO_ERRORS as exc:
                return detail.Observation.read_failed(exc)
            if denied:
                guarded = self._guard()
                if isinstance(guarded, detail.Observation):
                    return guarded
                if guarded:
                    return self._denied_visit()

            try:
                intervention = intervention_kind(self._page)
            except BROWSER_IO_ERRORS as exc:
                return detail.Observation.read_failed(exc)
            if intervention:
                guarded = self._guard()
                if isinstance(guarded, detail.Observation):
                    return guarded
                if guarded:
                    return self._denied_visit()
                # 人工介入结束后，给当前页面一个完整的可读窗口。
                return waiting.EXTEND

            html = self._read_html()
            if isinstance(html, detail.Observation):
                return html
            if detail.readable(html):
                return detail.observe_html(html, product_url)
            return None

        while True:
            outcome = waiting.until(probe, timeout_sec=DETAIL_READY_TIMEOUT_SEC,
                                    poll_sec=DETAIL_READY_POLL_SEC,
                                    describe="详情页可读")
            if outcome is waiting.EXTEND:
                continue
            if outcome is not None:
                return outcome
            # 超时出口：读一次当前 html 按现状翻译（至多多读一次 content()）。
            html = self._read_html()
            if isinstance(html, detail.Observation):
                return html
            return detail.observe_html(html, product_url)

    def _denied_visit(self) -> DeniedVisit:
        """把当前页读成一次 deny 结果，并顺手留一份原始页（ADR-0057）。"""
        html = _read_raw_html(self._page)
        return DeniedVisit(html, _archive(self._archive_deny, html))

    def _guard(self) -> bool | detail.Observation:
        try:
            return ready_detail_page(
                self._page, self._cfg, emit=self._emit,
                deny_tracker=self._deny_tracker, shop_key=self._shop_key,
                target_url=self._target_url, on_intervention=self._on_intervention,
            )
        except (ShopDenyExceeded, RoundDenyExceeded) as exc:
            _archive_threshold_deny(self._archive_deny, self._page, exc)
            raise
        except BROWSER_IO_ERRORS as exc:
            return detail.Observation.read_failed(exc)

    def _read_html(self) -> str | detail.Observation:
        try:
            return self._page.content()
        except BROWSER_IO_ERRORS as exc:
            return detail.Observation.read_failed(exc)


def _settle_detail_page(opened: OpenedDetail, cfg: Config, *, emit=None,
                        deny_tracker: DenyTracker | None = None,
                        shop_key: str | None = None,
                        on_intervention=None) -> bool:
    """刚打开的详情页先判读一次；「还没渲染出来」时给一段就绪窗口等待重判。

    「还没渲染出来」不是「无法验证」：弹窗可能先落在空白页或空壳文档上，交接的同一瞬间
    判据读到「页面尚未可靠加载 / 页面上下文未知」——那是页面还没到，不是判据的意见
    （2026-10-11 本机试采第一张卡即整轮暂停，回路确证）。**第一判读照旧先发生**（deny
    记账、阈值阶梯与弹窗归属的既有次序都不变），只有「尚未可靠加载」才进窗口继续轮询；
    窗口用尽再按原样判一次，把真正的无法验证原样上抛——整轮暂停的安全语义不变
    （issues-08-11 票 02/07）。deny 与人工介入各自即刻走自己的路径，不进本窗口；
    等待期间收到停止请求照旧穿透。窗口与详情页可读等待同一量级（`DETAIL_READY_TIMEOUT_SEC`）：
    先「可判读」后「可读」。
    """
    def judge() -> bool:
        return ready_detail_page(
            opened.page, cfg, emit=emit, deny_tracker=deny_tracker,
            shop_key=shop_key, target_url=opened.target_url,
            on_intervention=on_intervention)

    try:
        return judge()
    except PageEvidenceUnavailable:
        pass  # 还没渲染出来：进就绪窗口等它，不是判据的意见

    def probe():
        try:
            # 包一层真值：False=页面可用 也是定论，不许被原语当成「还没等到」。
            return (True, judge())
        except PageEvidenceUnavailable:
            return None

    settled = waiting.until(probe, timeout_sec=DETAIL_READY_TIMEOUT_SEC,
                            poll_sec=DETAIL_READY_POLL_SEC)
    if settled is None:
        log.warning("详情页等待就绪超时（%.0f 秒），仍未就绪，交原样判读定论",
                    DETAIL_READY_TIMEOUT_SEC)
        return judge()
    return settled[1]


def begin_detail_visit(
    acquire,
    cfg: Config,
    *,
    emit=None,
    deny_tracker: DenyTracker | None = None,
    shop_key: str | None = None,
    pacing=None,
    round_id: int | None = None,
    offer_id=None,
) -> NotOpenedVisit | ReadFailedVisit | DeniedVisit | ReadyDetailVisit:
    """取得详情页并完成初次 guard，交回一次性的后续读取 capability。

    初次判读走 `_settle_detail_page`：刚打开的弹窗上「还没渲染出来」不算无法验证，
    第一判读照旧先发生，读不到可靠文档才进就绪窗口等待——这是本 seam 的责任，
    两条取得路径（点击弹窗、逐店补采导航）都从这里过。

    主动停顿（ADR-0038）的两步也在这条共享 seam 上，只此一处持有：**取得之前**问一次
    （配额到点就停在这里，请求还没发出）、**adapter 真的交回页面之后**记一次。记账点放在
    取得之后，所以「点了没打开」与「取得时报错」都不占配额，而 guard 判出来的 deny 页、
    以及在 deny 页上越界抛出的那一次都算——配额量的是打到详情接口的请求。

    `round_id` 与 `offer_id` 用来组「命中拦截/介入时留一份原始页」的闭包（ADR-0057）：本 seam
    只把闭包塞进 `ReadyDetailVisit` 与两个 deny 出口，**判据层（guard）不认识磁盘**。`offer_id`
    可以是字符串或返回字符串的无参函数——点击路径点开之后才拿得到编号，所以按值取而不是按
    引用捕获；编号缺失（`None`）时这一路不留证，采集照常。
    """
    if pacing is not None:
        pacing.before_detail_visit(shop_key=shop_key)
    try:
        opened = acquire()
    except BROWSER_IO_ERRORS as exc:
        return ReadFailedVisit(exc, detail.Observation.read_failed(exc))

    if opened is None:
        return NotOpenedVisit()
    if not isinstance(opened, OpenedDetail):
        raise TypeError(f"详情 adapter 必须交回 OpenedDetail，收到 {type(opened)!r}")

    if pacing is not None:
        pacing.detail_visit_opened(shop_key=shop_key)

    resolved: str | None = None
    resolved_done = False

    def identity() -> str | None:
        """这次的存档身份：调用方给的编号（可按值取——点击路径点开后才拿得到）。只解析一次。"""
        nonlocal resolved, resolved_done
        if not resolved_done:
            resolved = _resolve_identity(offer_id)
            resolved_done = True
        return resolved

    def archive_deny(html: str) -> str | None:
        key = identity()
        if not html or key is None or round_id is None:
            return None
        return raw_pages.relpath(cfg, raw_pages.save_detail_page(cfg, round_id, key, html))

    def archive_intervention(page, vtype: str, criterion: str | None = None) -> str | None:
        key = identity()
        if key is None or round_id is None:
            return None
        html = _read_raw_html(page)
        if not html:
            return None
        return raw_pages.relpath(cfg, raw_pages.save_intervention_page(
            cfg, round_id, key, vtype, html))

    try:
        denied = _settle_detail_page(
            opened, cfg, emit=emit, deny_tracker=deny_tracker, shop_key=shop_key,
            on_intervention=archive_intervention)
    except (ShopDenyExceeded, RoundDenyExceeded) as exc:
        _archive_threshold_deny(archive_deny, opened.page, exc)
        raise
    except BROWSER_IO_ERRORS as exc:
        return ReadFailedVisit(exc, detail.Observation.read_failed(exc))
    if denied:
        html = _read_raw_html(opened.page)
        return DeniedVisit(html, _archive(archive_deny, html))
    return ReadyDetailVisit(
        _page=opened.page, _cfg=cfg, _emit=emit, _deny_tracker=deny_tracker,
        _shop_key=shop_key, _target_url=opened.target_url,
        _on_intervention=archive_intervention, _archive_deny=archive_deny,
    )


def _resolve_identity(value):
    """`offer_id` 实参可以是字符串或返回字符串的无参函数（点击路径点开后才拿得到编号）。"""
    return value() if callable(value) else value


def _archive_threshold_deny(archive_deny, page, exc) -> None:
    """触发店铺/整轮阈值的 deny 也要留证：它恰是「赔掉一家店 / 中止整轮」的那一页（ADR-0057）。

    两处出口（初次判读与可读等待）共用这一份，异常由调用方原样上抛。
    """
    exc.raw_path = _archive(archive_deny, _read_raw_html(page))


def _archive(archive_deny, html: str) -> str | None:
    """调用调用方给的 deny 留证闭包；没给、html 为空或写盘失败都只记日志、不影响控制流。"""
    if archive_deny is None or not html:
        return None
    try:
        return archive_deny(html)
    except Exception:
        log.debug("deny 存档失败，忽略", exc_info=True)
        return None


def _read_raw_html(page) -> str:
    try:
        return page.content()
    except BROWSER_IO_ERRORS:
        return ""
