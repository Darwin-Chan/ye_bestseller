"""详情访问的共享 seam：取得、安顿并读取一次当前详情观测。"""
from __future__ import annotations

import time
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError

from . import detail
from .config import Config
from .guard import (DenyTracker, intervention_kind, is_deny_url,
                    ready_detail_page)

DETAIL_READY_TIMEOUT_SEC = 10.0
DETAIL_READY_POLL_SEC = 0.25
BROWSER_IO_ERRORS = (PlaywrightError, TimeoutError, OSError, ConnectionError)


@dataclass(frozen=True)
class OpenedDetail:
    """一个 adapter 已取得的详情页；页面仍归取得它的调用方关闭。

    只交页面：验证据号不是 adapter 的事实——判据读页面上的地址、正文与验证容器（ADR-0029）。
    """

    page: object


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
    """详情页已确认是 deny；账目与阈值已由 guard 处理。"""

    raw_html: str = ""


@dataclass
class ReadyDetailVisit:
    """已通过初次 guard、可读取一次当前详情页的 capability。"""

    _page: object
    _cfg: Config
    _emit: object = None
    _deny_tracker: DenyTracker | None = None
    _shop_key: str | None = None
    _reraise: tuple[type[BaseException], ...] = ()
    _observed: bool = False

    def observe(self, product_url: str) -> detail.Observation | DeniedVisit:
        """等待 guard 后的页面可读，再翻译当前 HTML；同一 capability 只能调用一次。"""
        if self._observed:
            raise RuntimeError("ReadyDetailVisit.observe() 只能调用一次")
        self._observed = True

        deadline = time.time() + DETAIL_READY_TIMEOUT_SEC
        while True:
            try:
                denied = is_deny_url(self._page.url or "")
            except self._reraise:
                raise
            except BROWSER_IO_ERRORS as exc:
                return detail.Observation.read_failed(exc)
            if denied:
                guarded = self._guard()
                if isinstance(guarded, detail.Observation):
                    return guarded
                if guarded:
                    return DeniedVisit(self._raw_html())

            try:
                intervention = intervention_kind(self._page)
            except self._reraise:
                raise
            except BROWSER_IO_ERRORS as exc:
                return detail.Observation.read_failed(exc)
            if intervention:
                guarded = self._guard()
                if isinstance(guarded, detail.Observation):
                    return guarded
                if guarded:
                    return DeniedVisit(self._raw_html())
                # 人工介入结束后，给当前页面一个完整的可读窗口。
                deadline = time.time() + DETAIL_READY_TIMEOUT_SEC
                continue

            html = self._read_html()
            if isinstance(html, detail.Observation):
                return html
            if detail.readable(html) or time.time() >= deadline:
                return detail.observe_html(html, product_url)
            time.sleep(DETAIL_READY_POLL_SEC)

    def _guard(self) -> bool | detail.Observation:
        try:
            return ready_detail_page(
                self._page, self._cfg, emit=self._emit,
                deny_tracker=self._deny_tracker, shop_key=self._shop_key,
            )
        except self._reraise:
            raise
        except BROWSER_IO_ERRORS as exc:
            return detail.Observation.read_failed(exc)

    def _read_html(self) -> str | detail.Observation:
        try:
            return self._page.content()
        except self._reraise:
            raise
        except BROWSER_IO_ERRORS as exc:
            return detail.Observation.read_failed(exc)

    def _raw_html(self) -> str:
        return _read_raw_html(self._page, self._reraise)


def begin_detail_visit(
    acquire,
    cfg: Config,
    *,
    emit=None,
    deny_tracker: DenyTracker | None = None,
    shop_key: str | None = None,
    reraise: tuple[type[BaseException], ...] = (),
) -> NotOpenedVisit | ReadFailedVisit | DeniedVisit | ReadyDetailVisit:
    """取得详情页并完成初次 guard，交回一次性的后续读取 capability。"""
    try:
        opened = acquire()
    except reraise:
        raise
    except BROWSER_IO_ERRORS as exc:
        return ReadFailedVisit(exc, detail.Observation.read_failed(exc))

    if opened is None:
        return NotOpenedVisit()
    if not isinstance(opened, OpenedDetail):
        raise TypeError(f"详情 adapter 必须交回 OpenedDetail，收到 {type(opened)!r}")

    try:
        denied = ready_detail_page(
            opened.page, cfg, emit=emit,
            deny_tracker=deny_tracker, shop_key=shop_key,
        )
    except reraise:
        raise
    except BROWSER_IO_ERRORS as exc:
        return ReadFailedVisit(exc, detail.Observation.read_failed(exc))
    if denied:
        return DeniedVisit(_read_raw_html(opened.page, reraise))
    return ReadyDetailVisit(
        _page=opened.page, _cfg=cfg, _emit=emit, _deny_tracker=deny_tracker,
        _shop_key=shop_key, _reraise=reraise,
    )


def _read_raw_html(page, reraise: tuple[type[BaseException], ...]) -> str:
    try:
        return page.content()
    except reraise:
        raise
    except BROWSER_IO_ERRORS:
        return ""
