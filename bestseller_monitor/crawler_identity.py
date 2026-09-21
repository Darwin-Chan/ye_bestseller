"""采集进程身份：现在是谁在跑、他算不算在跑（候选 05）。

两个来源合成这一条判据：

- **会话锁**（`single_instance.CRAWLER_LOCK`）是「在不在跑」的权威——采集进程活着就攥着它；
- **库里那行身份**（`crawler_process` 表，单行）回答「是谁、哪一轮、什么时候起」——
  停止请求按 (pid, 启动时刻) 认领（ADR-0009），开始页也照它说话。

界面另外还知道一件自己的事：**它自己拉起来的那个子进程**。锁刚拿到、身份行还没写的那一小段
（`Popen` 到子进程抢到锁之间），只有它能把 pid 报出来，所以它是同一条判据的第三个输入，
由调用方以 `own_*` 参数传进来——这个 module 因此不认识界面。

被强杀的采集进程会留下身份行：`current()` 在判定「没人在跑」时顺手清掉它（ADR-0008 的规则：
判据与顺手清是一件事）。采集端只该**纯读**那一行（它要认领指向自己的停止请求），
用的是 `registered()`。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import single_instance
from .db import Database


@dataclass(frozen=True)
class CrawlerProcess:
    """正在跑的那个采集进程。

    字段都可以缺：锁被别处持着而身份行还没写时，界面只知道「有人在跑」，不知道是谁
    （`pid` 为 None，开始页据此说「身份未知」）。
    """

    pid: int | None = None
    round_id: int | None = None
    started_at: str | None = None
    note: str | None = None
    process_os_started: str | None = None
    browser_state: str = "UNKNOWN"
    browser_port: int | None = None
    browser_pid: int | None = None
    browser_os_started: str | None = None

    def to_payload(self) -> dict:
        """界面 payload 要的形状：`bestseller_monitor/pages/ui_live.html` 读 `d.crawler.round_id`，
        跨 pywebview 那一步走 JSON，所以这里给回普通 dict。"""
        return {"pid": self.pid, "round_id": self.round_id,
                "started_at": self.started_at, "note": self.note}


def is_running(*, own_alive: bool = False) -> bool:
    """有采集进程在跑吗。

    与「轮次是否进行中」是两件事：暂停后的轮次仍在进行中，但已经没有采集进程。
    """
    return own_alive or single_instance.is_held(single_instance.CRAWLER_LOCK)


def registered(conn) -> CrawlerProcess | None:
    """身份行里登记的是谁；没有登记就是 None。纯读，不清残留（采集端用这一条）。

    行里可能是一条残留（进程已被强杀）：那是界面该判的事，见 `current()`。
    """
    row = Database(conn).crawler_process()
    if row is None:
        return None
    return CrawlerProcess(pid=int(row["pid"]), round_id=row["round_id"],
                          started_at=row["started_at"], note=row["note"],
                          process_os_started=row["process_os_started"],
                          browser_state=row["browser_state"] or "UNKNOWN",
                          browser_port=row["browser_port"],
                          browser_pid=row["browser_pid"],
                          browser_os_started=row["browser_os_started"])


def current(conn, *, own_alive: bool = False, own_pid: int | None = None,
            own_round_id: int | None = None) -> CrawlerProcess | None:
    """现在是谁在跑：没人在跑返回 None；有身份行就给那一行；只有锁、没有行就用界面自己的子进程凑一个。

    `own_*` 是界面会话自己的事实（它拉起的那个子进程、它认领的轮次），只在 `own_alive`
    为真时参与——不是本界面起的进程，界面拿不到它的 pid。
    """
    who = registered(conn)
    if not is_running(own_alive=own_alive):
        if who is not None:
            Database(conn).clear_crawler_process(
                target_pid=who.pid, target_started_at=who.started_at)
        return None
    if who is not None:
        return who
    return CrawlerProcess(pid=own_pid if own_alive else None, round_id=own_round_id)
