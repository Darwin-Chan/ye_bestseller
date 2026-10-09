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

from contextlib import closing
from pathlib import Path
import sqlite3
from dataclasses import dataclass

from . import browser_proc, single_instance
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


def browser_cleanup_complete(state: str | None) -> bool:
    """Positive evidence that this identity has no owned-browser cleanup debt."""
    return state in ("NOT_STARTED", "BORROWED", "CLOSED")


def settle_dead_registration(conn, who: CrawlerProcess) -> bool:
    """结清一条已无进程的残留身份行（显式中止遗留轮的必要收尾）。

    只在原进程**确已不在**时才动手：`process_alive` 为 False，或创建证明与登记值
    分歧（PID 已被另一个创建身份接管）。核不实时一律保留欠账，返回 False——
    「无法核验」不是许可。OWNED 浏览器只按行里的精确绑定关（pid + 创建证明），
    关不掉且浏览器仍在的同样保留；BORROWED 一类的完整状态直接清行。

    返回 True 表示身份行（及其停止请求）已删除。进程本身从不被终止：这里没有
    任何终止动作，处置的只有「登记事实」与「本次启动的浏览器」。
    """
    alive = browser_proc.process_alive(who.pid)
    if alive is not False:
        proof = browser_proc.process_creation_proof(who.pid)
        taken_over = (proof is not None and who.process_os_started is not None
                      and proof != who.process_os_started)
        if not taken_over:
            return False
    if not browser_cleanup_complete(who.browser_state):
        if (who.browser_state != "OWNED" or not who.browser_pid
                or not who.browser_os_started):
            return False
        closed = browser_proc.close_browser(
            who.browser_port, launched_by_us=True,
            browser_pid=who.browser_pid, browser_os_started=who.browser_os_started)
        if closed is None:
            b_alive = browser_proc.process_alive(who.browser_pid)
            if b_alive is not False:
                b_proof = browser_proc.process_creation_proof(who.browser_pid)
                taken_over = (b_proof is not None
                              and b_proof != who.browser_os_started)
                if not taken_over:
                    return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = registered(conn)
        if (current is None or current.pid != who.pid
                or current.started_at != who.started_at):
            conn.rollback()
            return False
        conn.execute("DELETE FROM crawler_process WHERE id=1 AND pid=? AND started_at=?",
                     (who.pid, who.started_at))
        conn.execute("DELETE FROM stop_requests WHERE id=1 AND target_pid=? "
                     "AND target_started_at=?", (who.pid, who.started_at))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


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
            own_round_id: int | None = None, other_space: Path | None = None) -> CrawlerProcess | None:
    """现在是谁在跑：没人在跑返回 None；有身份行就给那一行；只有锁、没有行就用界面自己的子进程凑一个。

    `own_*` 是界面会话自己的事实（它拉起的那个子进程、它认领的轮次），只在 `own_alive`
    为真时参与——不是本界面起的进程，界面拿不到它的 pid。
    """
    who = registered(conn)
    if not is_running(own_alive=own_alive):
        if who is not None and browser_cleanup_complete(who.browser_state):
            Database(conn).clear_crawler_process(
                target_pid=who.pid, target_started_at=who.started_at)
        return None
    identity = who if who is not None else CrawlerProcess(
        pid=own_pid if own_alive else None, round_id=own_round_id)
    return _verified(identity, other_space, own_alive=own_alive) if other_space is not None else identity


def registered_in_file(path: Path) -> CrawlerProcess | None:
    """Read another collection space without creating or migrating its database.

    A missing database/identity table means no registration, not permission to stop.
    Invalid or inaccessible databases remain errors so callers fail closed.
    """
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crawler_process'").fetchone()
        if table is None:
            return None
        return registered(conn)


def _registration_may_be_live(identity: CrawlerProcess | None) -> bool:
    if identity is None:
        return False
    if browser_proc.process_alive(identity.pid) is False:
        return False
    actual = browser_proc.process_creation_proof(identity.pid)
    # A different known creation proof means the original process is gone
    # (PID reuse). Missing proof is unknown, not permission to ignore it.
    return not (actual is not None and identity.process_os_started is not None
                and actual != identity.process_os_started)


def _verified(identity: CrawlerProcess | None, other_space: Path, *, own_alive=False) -> CrawlerProcess:
    try:
        if _registration_may_be_live(registered_in_file(other_space)):
            return CrawlerProcess(note="其他模式有采集身份，无法安全确认目标")
    except (OSError, sqlite3.Error):
        return CrawlerProcess(note="无法核验其他模式的进程身份")
    if identity is None:
        return CrawlerProcess(note="采集进程身份未知")
    if own_alive:
        return identity
    if (identity.pid is None or not identity.process_os_started
            or browser_proc.process_alive(identity.pid) is not True
            or browser_proc.process_creation_proof(identity.pid) != identity.process_os_started):
        return CrawlerProcess(note="无法核验采集进程身份")
    return identity


def verified_in_file(path: Path, *, other_space: Path) -> CrawlerProcess:
    """Resolve a requested running space without modifying either database.

    Caller has already observed the machine lock; a known, matching OS proof
    and no live/unknown competing registration are needed to attach.
    """
    return _verified(registered_in_file(path), other_space)
