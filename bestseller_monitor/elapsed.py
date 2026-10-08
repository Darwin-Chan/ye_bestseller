"""本机轮次的采集段计时权威。

只有执行进程持有 Segment（单调基点只在内存）；所有读者只看成功保存的 Fact。
检查点覆盖本段累计，结账冻结本段；不从轮次挂钟或 GUI 观察时刻补猜历史。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from . import browser_proc, crawler_identity


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fact:
    seconds: float | None
    quality: str  # unrecorded / recorded（活跃快照）/ complete / lower_bound

    @property
    def text(self) -> str:
        if self.seconds is None:
            return "未记录"
        seconds = int(self.seconds)
        if seconds < 60:
            value = f"{seconds} 秒"
        elif seconds < 3600:
            value = f"{seconds // 60} 分"
        else:
            hours, minutes = divmod(seconds // 60, 60)
            value = f"{hours} 时 {minutes} 分"
        return f"至少 {value}" if self.quality == "lower_bound" else value

    @property
    def note(self) -> str:
        if self.quality == "unrecorded":
            return "没有已保存的采集时长记录，不以轮次起止跨度补算。"
        if self.quality == "lower_bound":
            return "存在未完整结账或无法证明仍活跃的段、或未知历史；仅显示已保存的已知下界。"
        if self.quality == "recorded":
            return "显示最近成功保存的采集时长快照。"
        return "有效采集执行段累计；不含开轮前准备和暂停间隔。"


class Segment:
    """一次执行的窄生命周期 interface；不暴露可被调用方拼装的计时字段。"""

    def __init__(self, conn: sqlite3.Connection, round_id: int, *,
                 process_started: str, new_round: bool, clock: Callable[[], float]):
        self._conn = conn
        self._execution_id = uuid4().hex
        self._round_id = round_id
        self._process_started = process_started
        self._process_pid = os.getpid()
        self._new_round = new_round
        self._reserved = False
        self._registered = False
        self._clock = clock
        self._started = clock()
        self._next_checkpoint = self._started + 1.0
        self._settled = False
        self._final_seconds: float | None = None

    def _register(self) -> bool:
        if self._registered:
            return True
        try:
            if not self._reserved:
                # 先持久化登记缺口，再登记段。失败不能让同轮之前的完整段掩盖新缺失。
                # 只有一个采集执行持会话锁；下一执行接替未完成登记时，旧缺口成为历史未知。
                with self._conn:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO elapsed_rounds(round_id, history_known) VALUES (?, ?)",
                        (self._round_id, int(self._new_round)))
                    self._conn.execute(
                        "UPDATE elapsed_rounds SET history_known=CASE "
                        "WHEN pending_execution IS NOT NULL AND pending_execution!=? THEN 0 "
                        "ELSE history_known END, pending_execution=? WHERE round_id=?",
                        (self._execution_id, self._execution_id, self._round_id))
                self._reserved = True
            with self._conn:
                self._conn.execute(
                    "INSERT INTO elapsed_segments(execution_id, round_id, process_started, process_pid) "
                    "VALUES (?, ?, ?, ?)",
                    (self._execution_id, self._round_id, self._process_started, self._process_pid))
                self._conn.execute(
                    "UPDATE elapsed_rounds SET pending_execution=NULL "
                    "WHERE round_id=? AND pending_execution=?", (self._round_id, self._execution_id))
        except sqlite3.Error:
            log.warning("采集段登记失败；不阻断采集，不伪造已保存时长", exc_info=True)
            return False
        self._registered = True
        return True

    def tick(self) -> bool:
        """现有协作检查承载约一秒节拍；阻塞期间不外推、不承诺硬延迟。"""
        if self._settled:
            return True
        now = self._clock()
        if now < self._next_checkpoint:
            return True
        self._next_checkpoint = now + 1.0
        return self.checkpoint()

    def checkpoint(self) -> bool:
        return self._save(final=False)

    def settle(self) -> bool:
        return self._save(final=True)

    def _save(self, *, final: bool) -> bool:
        if self._settled:
            return True
        if final and self._final_seconds is None:
            self._final_seconds = max(0.0, self._clock() - self._started)
        final = final or self._final_seconds is not None
        seconds = (self._final_seconds if final else
                   max(0.0, self._clock() - self._started))
        if not self._register():
            return False
        try:
            with self._conn:
                self._conn.execute(
                    "UPDATE elapsed_segments SET seconds=MAX(seconds, ?), settled=? "
                    "WHERE execution_id=? AND settled=0",
                    (seconds, int(final), self._execution_id))
        except sqlite3.Error:
            # 保存失败不冒充完整，也不能替换采集异常或阻断原停止检查。
            log.warning("采集时长保存失败；保留最近成功的快照", exc_info=True)
            return False
        if final:
            self._settled = True
        return True


def begin(conn: sqlite3.Connection, round_id: int, *, process_started: str,
          new_round: bool, clock: Callable[[], float] | None = None) -> Segment:
    """准备及轮次确定之后调用；已有轮缺失的历史不会被新段洗成完整。"""
    segment = Segment(conn, round_id, process_started=process_started,
                      new_round=new_round, clock=clock if clock is not None else time.monotonic)
    segment._register()
    return segment


def read(conn: sqlite3.Connection, round_id: int) -> Fact:
    """只读成功持久化的事实；既不累计也不外推，不复用任何单调基点。"""
    rows = conn.execute(
        "SELECT r.history_known, r.pending_execution, s.seconds, s.settled, s.process_started, s.process_pid, "
        "original.finished_at "
        "FROM elapsed_rounds r JOIN elapsed_segments s ON s.round_id=r.round_id "
        "JOIN rounds original ON original.id=r.round_id WHERE r.round_id=?",
        (round_id,)).fetchall()
    if not rows:
        return Fact(None, "unrecorded")
    unfinished = [row for row in rows if not row["settled"]]
    incomplete = not rows[0]["history_known"] or rows[0]["pending_execution"] is not None
    if unfinished:
        # 只解释已保存的段；不清进程身份、不发现退出就猜终点或改写终态。
        # 会话锁 + (pid, started_at, round_id) 才能证明这是当前执行的快照。
        identity = crawler_identity.registered(conn)
        # 锁可能刚被新执行取得（尚未换库中身份），也可能属于另一采集空间。
        # 只读核验旧目标的OS创建证明；未知不能把旧下界重新解释成活跃快照。
        live = (identity is not None and crawler_identity.is_running()
                and identity.process_os_started is not None
                and browser_proc.process_alive(identity.pid) is True
                and browser_proc.process_creation_proof(identity.pid) == identity.process_os_started)
        incomplete = incomplete or rows[0]["finished_at"] is not None or not live
        if live:
            incomplete = incomplete or any(
                row["process_pid"] != identity.pid or
                row["process_started"] != identity.started_at or identity.round_id != round_id
                for row in unfinished)
    quality = "lower_bound" if incomplete else "recorded" if unfinished else "complete"
    return Fact(sum(float(row["seconds"]) for row in rows), quality)
