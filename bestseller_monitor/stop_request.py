"""停止请求：界面请求正在跑的采集进程停下（ADR-0009）。

「暂停」由界面往库里写一条请求，采集进程在自己的检查点上认领它——按目标进程
（PID + 启动时刻）识别，所以新起的进程与命令行续跑不会被表里的旧请求影响。
「中止」不走这个通道：轮次终态本身就是停止信号（`Round.stops_work()`）。

本模块只认数据层的两个事实（谁在跑、有没有针对它的请求），不碰轮次的判定。
长睡眠上要问的那个问题由 pipeline 装进来的钩子回答（`install()`），
这样 delay / guard 不必知道轮次，也就不可能在两处写出不同的停止判据。

ADR-0009 的协议有两端，都在这个文件里：采集端是 `install/check/consume`，
界面端是 `StopWatch`（窗口、回执、超时强杀与收尾）。
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from .db import Database

log = logging.getLogger(__name__)

PAUSE = "pause"
# 「中止」：先把轮次收尾为人工放弃，再等采集进程自己停下；不用请求行。
ABORT = "abort"

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


def targets(request, *, pid: int, started_at: str | None) -> bool:
    """这条停止请求是不是指向 (PID, 启动时刻) 那个进程。

    协议两端都按这一条判断：采集端认领的是「指向本进程」的请求，
    界面端放宽窗口时看的是「采集进程认领了它自己那条请求」。
    """
    if request is None:
        return False
    return (int(request["target_pid"]) == int(pid)
            and request["target_started_at"] == started_at)


def _mine(db: Database, *, pid: int | None = None):
    """表里那条请求如果指向本进程就返回它，否则返回 None。"""
    request = db.stop_request()
    if request is None:
        return None
    identity = db.crawler_process()
    if identity is None:
        return None
    if int(identity["pid"]) != (os.getpid() if pid is None else pid):
        return None
    if not targets(request, pid=identity["pid"], started_at=identity["started_at"]):
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


# ---------- 界面端：把「正在停止」这件事编排完 ----------

@dataclass(frozen=True)
class StopInFlight:
    """界面侧正在进行的停止：等采集进程自己停下，窗口到点才强杀。"""

    kind: str
    target: dict | None
    deadline: float
    acked: bool = False

    @property
    def state(self) -> str:
        """给界面看的阶段：回执之前是「正在停止」，回执之后是「正在收尾」。"""
        return "closing" if self.acked else "stopping"


class StopWatch:
    """界面侧的停止编排：窗口、回执、超时强杀与收尾（ADR-0009）。

    时间与副作用都从构造进来——所以整条协议可以在没有进程、没有浏览器的情况下
    走一遍：用例换个时钟就能把窗口拨到点，不必去改什么私有状态。

    驱动的仍然是既有的约 2 秒轮询（三个取数入口各调一次 `tick()`），不新增线程。
    """

    def __init__(self, *, kill_child: Callable[[], None], stop_foreign: Callable,
                 close_browser: Callable[[], None], is_running: Callable[[], bool],
                 identity_of: Callable, now=time.time, grace_sec: float = 8.0,
                 ack_grace_sec: float = 10.0):
        # 五个口子都必填：漏接线时要当场报错，不是静默什么都不做。
        self._kill_child = kill_child
        self._stop_foreign = stop_foreign
        self._close_browser = close_browser
        self._is_running = is_running
        self._identity_of = identity_of
        self._now = now
        self._grace_sec = grace_sec
        self._ack_grace_sec = ack_grace_sec
        self._in_flight: StopInFlight | None = None

    # ---------- 界面用得到的 ----------
    @property
    def grace_sec(self) -> float:
        return self._grace_sec

    @property
    def state(self) -> str | None:
        """没有停止在进行时是 None。"""
        return self._in_flight.state if self._in_flight is not None else None

    def forget(self) -> None:
        """丢掉在跑的停止：没起过任务、也没有采集在跑时用。"""
        self._in_flight = None

    def begin(self, kind: str, target: dict | None) -> StopInFlight:
        """记下「正在停止」：不阻塞，倒计时与超时兜底交给轮询。"""
        self._in_flight = StopInFlight(kind=kind, target=target,
                                       deadline=self._now() + self._grace_sec)
        return self._in_flight

    # ---------- 轮询驱动的那一跳 ----------
    def tick(self, conn) -> None:
        """到点还没停下就强制结束；已经停下就把状态收干净。"""
        stop = self._in_flight
        if stop is None:
            return
        db = Database(conn)
        stop = self._note_ack(db, stop)
        if not self._is_running():
            log.info("采集进程已停下（%s）。", stop.kind)
            db.clear_stop_request()
            self._in_flight = None
            return
        if self._now() < stop.deadline:
            return
        self._force(conn, db, stop)

    def _note_ack(self, db: Database, stop: StopInFlight) -> StopInFlight:
        """采集进程回执了就放宽窗口：它在收尾，而不是没响应。"""
        if stop.acked or stop.target is None:
            return stop
        request = db.stop_request()
        if request is None:
            return stop
        if not targets(request, pid=stop.target["pid"],
                       started_at=stop.target["started_at"]):
            return stop
        if not request["ack_at"]:
            return stop
        log.info("采集进程已回执停止请求，再等 %.0f 秒收尾。", self._ack_grace_sec)
        self._in_flight = replace(stop, acked=True,
                                  deadline=self._now() + self._ack_grace_sec)
        return self._in_flight

    def _force(self, conn, db: Database, stop: StopInFlight) -> None:
        """强制停止：强杀进程 → 按归属收尾浏览器 → 清停止请求 → 收尾状态。"""
        log.warning("停止窗口内没停下（%s），强制结束采集进程。", stop.kind)
        self._kill_child()
        identity = self._identity_of(conn)
        if stop.kind == ABORT and identity is not None:
            # 中止要停的可能是别处起的采集：按身份行里的 PID 停，镜像名先核过。
            self._stop_foreign(identity)
        self._close_browser()
        if self._is_running():
            # 强杀没落到实处：请求留着，采集进程在下一个检查点仍会自己停下。
            log.warning("强制结束之后采集进程仍在跑，停止请求留在库里等它自己认领。")
        else:
            db.clear_crawler_process()
            db.clear_stop_request()
        self._in_flight = None
