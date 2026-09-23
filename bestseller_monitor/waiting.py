"""等一件事：条件 · 上限 · 轮询间隔 · 取消钩子（ADR-0042）。

四个「等页面」的循环——榜单页的条件等待、详情页的可读轮询、浏览器调试端口的连接重试、
人工介入的确认窗口与响铃循环——都往这里搬：搬完之后「等多久、多久看一眼、能不能被打断」
只有这一处知道（迁移是票 02–05 的事，票 01 落的是原语本身与 `Humanizer.sleep` 的睡眠
分片）。取消钩子默认接 `stop_request.check()`，与长睡眠的分片同一个判据（ADR-0009）：
等待因此和睡眠一样落在协作停止的检查点上。

一轮 = 探针 → 不成 → 问一次取消 → `on_wait`（如有）→ 睡一片到上限为止；每轮先探测后判
到点（条件恰在窗口末尾成立时仍算成立）。检查点在入口与每次睡前各一次。探针与取消抛出的
异常都原样上抛——吞异常是探针闭包自己的事（榜单页的谓词今天吞全部异常，那是它的调用点
知识）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from . import stop_request

log = logging.getLogger(__name__)


class _Extend:
    """`EXTEND` 的真身：探针返回它表示「本次窗口到此，外层重开一窗」。

    详情页的可读轮询是唯一带窗口重置的循环（人工介入解决后重开一个完整可读窗口）。重置
    不进原语：探针把 `EXTEND` 当普通真值返回，`until()` 原样交出去，由调用方的外层循环
    再开一窗。
    """

    def __repr__(self) -> str:
        return "EXTEND"


EXTEND = _Extend()


def until(probe: Callable[[], object] | None, *, timeout_sec: float, poll_sec: float,
          describe: str | None = None, cancel: Callable[[], None] | None = None,
          on_wait: Callable[[], None] | None = None) -> object | None:
    """轮询 `probe()`：返回它给出的第一个真值；超时记一行（describe 非空时）并返回 `None`。

    - `probe=None` 是纯等待（条件永不成立）：只受上限与取消约束，`Humanizer.sleep` 的退化式。
    - `timeout_sec <= 0` 既不探测也不问取消，记一行后按超时返回——窗口是 0 也要走一次出口。
    - 每轮先探测、后判到点：条件恰在窗口末尾成立时算成立（响铃循环的既有次序即如此）。
    - 每轮睡到上限为止：末片取 `min(poll_sec, 剩余)`，一场等待的总时长不超过上限。
    - `on_wait` 是「还没等到时每轮要做的事」（人工介入的响铃），在睡前调用一次。
    """
    if cancel is None:
        cancel = stop_request.check
    if timeout_sec <= 0:
        _note_timeout(describe, timeout_sec)
        return None
    deadline = time.monotonic() + timeout_sec
    cancel()
    while True:
        if probe is not None:
            value = probe()
            if value:
                return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _note_timeout(describe, timeout_sec)
            return None
        cancel()
        if on_wait is not None:
            on_wait()
        piece = min(poll_sec, deadline - time.monotonic())
        time.sleep(max(0.0, piece))   # on_wait（响铃）可能已经用掉了剩下的窗口


def _note_timeout(describe: str | None, timeout_sec: float) -> None:
    if not describe:
        return
    log.info("等待「%s」超时(%.0fs)，按当前状态继续。", describe, timeout_sec)
