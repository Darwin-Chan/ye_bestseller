"""停止请求：界面请求正在跑的采集进程停下（ADR-0009）。

「暂停」由界面往库里写一条请求，采集进程在自己的检查点上认领它——按目标进程
（PID + 启动时刻）识别，所以新起的进程与命令行续跑不会被表里的旧请求影响。
「中止」不走这个通道：轮次终态本身就是停止信号（`Round.stops_work()`）。

本模块只认数据层的两个事实（谁在跑、有没有针对它的请求），不碰轮次的判定。
长睡眠上要问的那个问题由 pipeline 装进来的钩子回答（`install()`），
这样 delay / guard 不必知道轮次，也就不可能在两处写出不同的停止判据。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable

from .db import Database

log = logging.getLogger(__name__)

PAUSE = "pause"

# 长睡眠的切片长度：协作停止的响应时间约等于一片加上一次检查（ADR-0009）。
SLICE_SEC = 0.5

_hook: Callable[[], None] | None = None


class StopRequested(RuntimeError):
    """收到指向本进程的停止请求：本轮暂停，保留进度、可以续跑。"""


def install(hook: Callable[[], None]) -> None:
    """装上「现在该不该停」的检查：pipeline 在一轮开始时装、结束时摘。"""
    global _hook
    _hook = hook


def uninstall() -> None:
    """摘掉检查；没装过也算成功。"""
    global _hook
    _hook = None


def check() -> None:
    """在长睡眠的切片上问一次该不该停。

    没装钩子（工具、单测、非采集路径）时什么都不做。
    """
    hook = _hook
    if hook is not None:
        hook()


def _mine(db: Database, *, pid: int | None = None):
    """表里那条请求如果指向本进程就返回它，否则返回 None。"""
    request = db.stop_request()
    if request is None:
        return None
    identity = db.crawler_process()
    if identity is None or int(identity["pid"]) != (os.getpid() if pid is None else pid):
        return None
    if (int(request["target_pid"]) != int(identity["pid"])
            or request["target_started_at"] != identity["started_at"]):
        return None
    return request


def targets_me(db: Database, *, pid: int | None = None) -> bool:
    """这条停止请求是不是指向本进程。"""
    return _mine(db, pid=pid) is not None


def consume(db: Database, *, pid: int | None = None) -> None:
    """有指向本进程的请求就回执并抛出；没有就返回。

    回执让界面分得清「还没响应」与「正在收尾」，并据此放宽超时窗口。
    """
    request = _mine(db, pid=pid)
    if request is None:
        return
    if db.ack_stop_request(target_pid=request["target_pid"],
                           target_started_at=request["target_started_at"]):
        # 回执让界面分得清「还没响应」与「正在收尾」，并据此放宽收尾窗口。
        log.info("已认领界面暂停请求：回执已写，界面据此放宽收尾窗口。")
    raise StopRequested("收到界面暂停请求")
