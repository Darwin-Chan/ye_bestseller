"""Target-bound cooperative stop protocol (ADR-0009, ADR-0024)."""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol

from . import crawler_identity
from .crawler_identity import CrawlerProcess
from .db import Database

log = logging.getLogger(__name__)

PAUSE = "pause"
ABORT = "abort"
SLICE_SEC = 0.5
_hook: Callable[[], None] | None = None


class StopRequested(RuntimeError):
    """收到指向本进程的停止请求。"""


def install(hook: Callable[[], None]) -> None:
    global _hook
    _hook = hook


def uninstall() -> None:
    global _hook
    _hook = None


def check() -> None:
    hook = _hook
    if hook is not None:
        hook()


def targets(request, *, pid: int, started_at: str | None) -> bool:
    if request is None:
        return False
    return (int(request["target_pid"]) == int(pid)
            and request["target_started_at"] == started_at)


def _mine(db: Database, *, pid: int | None = None):
    request = db.stop_request()
    if request is None:
        return None
    identity = crawler_identity.registered(db.conn)
    if identity is None:
        return None
    if int(identity.pid) != (os.getpid() if pid is None else pid):
        return None
    if not targets(request, pid=identity.pid, started_at=identity.started_at):
        return None
    return request


def targets_me(db: Database, *, pid: int | None = None) -> bool:
    return _mine(db, pid=pid) is not None


def consume(db: Database, *, pid: int | None = None) -> None:
    request = _mine(db, pid=pid)
    if request is None:
        return
    if db.ack_stop_request(target_pid=request["target_pid"],
                           target_started_at=request["target_started_at"]):
        log.info("已认领界面暂停请求：回执已写，界面据此放宽收尾窗口。")
    raise StopRequested("收到界面暂停请求")


class StopKind(str, Enum):
    PAUSE = PAUSE
    ABORT = ABORT


class StopPhase(str, Enum):
    IDLE = "idle"
    STOPPING = "stopping"
    CLOSING = "closing"
    VERIFYING = "verifying"
    CLEANUP_PENDING = "cleanup_pending"


class StopRelation(str, Enum):
    EXACT = "exact"
    GONE = "gone"
    REPLACED = "replaced"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class StopTarget:
    pid: int
    started_at: str
    process_os_started: str | None = None

    def __post_init__(self):
        if not self.pid or not self.started_at:
            raise ValueError("stop target requires pid and started_at")
        object.__setattr__(self, "pid", int(self.pid))

    @classmethod
    def of(cls, who: CrawlerProcess | None) -> "StopTarget | None":
        if who is None or not who.pid or not who.started_at:
            return None
        return cls(int(who.pid), who.started_at, who.process_os_started)


@dataclass(frozen=True)
class StopCommand:
    kind: StopKind | str
    target: StopTarget
    round_id: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "kind", StopKind(self.kind))
        if self.target is None:
            raise ValueError("stop target is required")


@dataclass(frozen=True)
class StopStatus:
    phase: StopPhase
    kind: StopKind | None = None
    target: StopTarget | None = None
    code: str | None = None
    retryable: bool = False

    @property
    def state(self) -> str | None:
        return None if self.phase is StopPhase.IDLE else self.phase.value


@dataclass(frozen=True)
class BoundBrowser:
    pid: int
    port: int | None = None
    os_started: str | None = None


@dataclass(frozen=True)
class BoundTarget:
    target: StopTarget
    capability: Any = None


@dataclass(frozen=True)
class RuntimeFacts:
    identity: CrawlerProcess | None
    process_alive: bool | None
    browser: BoundBrowser | None = None
    browser_state: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class EffectResult:
    ok: bool
    code: str | None = None
    retryable: bool = True


class StopRuntime(Protocol):
    def bind(self, target: StopTarget) -> BoundTarget: ...
    def observe(self, conn, bound: BoundTarget) -> RuntimeFacts: ...
    def terminate(self, bound: BoundTarget) -> EffectResult: ...
    def close_browser(self, browser: BoundBrowser) -> EffectResult: ...
    def release(self, bound: BoundTarget) -> None: ...


class RecordingStopRuntime:
    """可脚本化的 adapter，用于 StopWatch seam 测试。"""

    def __init__(self, *, alive: bool | None = True,
                 browser: BoundBrowser | None = None,
                 browser_state: str | None = None,
                 survives_terminate: bool = False):
        self.alive = alive
        self.browser = browser
        self.browser_state = browser_state
        # 「杀了没杀掉」：terminate 报成功，但进程仍然活着——与 fail_terminate
        # （根本没杀成）走的是 _force 里两条不同的分支。
        self.survives_terminate = survives_terminate
        self.actions: list[tuple[str, object]] = []
        self.bound: BoundTarget | None = None
        self.fail_terminate = False
        self.fail_close = False

    def bind(self, target: StopTarget) -> BoundTarget:
        self.bound = BoundTarget(target, target)
        self.actions.append(("bind", target))
        return self.bound

    def observe(self, conn, bound: BoundTarget) -> RuntimeFacts:
        return RuntimeFacts(crawler_identity.registered(conn), self.alive,
                            self.browser, self.browser_state)

    def terminate(self, bound: BoundTarget) -> EffectResult:
        self.actions.append(("terminate", bound.target))
        if self.fail_terminate:
            return EffectResult(False, "terminate_failed")
        if not self.survives_terminate:
            self.alive = False
        return EffectResult(True)

    def close_browser(self, browser: BoundBrowser) -> EffectResult:
        self.actions.append(("close_browser", browser))
        if self.fail_close:
            return EffectResult(False, "browser_close_failed")
        self.browser = None
        return EffectResult(True)

    def release(self, bound: BoundTarget) -> None:
        self.actions.append(("release", bound.target))


class ProcessStopRuntime:
    """Windows process adapter.  It binds handles at begin and never reselects a PID.

    冻结目标之后**怎么处置**是这一个 module 的事：调用方只交事实（本界面拉起的那个子进程
    句柄），不交动作。动作留在外面时，规则会有第二份实现，而「动的是冻结的那个绑定还是调用方
    此刻的可变字段」就取决于接线而不是取决于这条不变量（ADR-0024）。
    """

    def __init__(self, *, own_process=None):
        self.own_process = own_process

    def bind(self, target: StopTarget) -> BoundTarget:
        process = self.own_process() if callable(self.own_process) else self.own_process
        process_pid = getattr(process, "pid", None) if process is not None else None
        # Test doubles may not expose a numeric pid; a real process is still frozen
        # at begin and its identity is checked by StopWatch before any action.
        handle = process if process is not None and \
            (process_pid == target.pid or not isinstance(process_pid, int)) else None
        if handle is not None:
            return BoundTarget(target, handle)
        if target.process_os_started is None:
            return BoundTarget(target, None)
        from . import browser_proc
        capability = browser_proc.bind_process(target.pid)
        if capability is None:
            return BoundTarget(target, None)
        if (target.process_os_started is not None
                and capability.created != target.process_os_started):
            browser_proc.release_process_capability(capability)
            raise RuntimeError("process_identity_mismatch")
        return BoundTarget(target, capability)

    def rebind(self, bound: BoundTarget) -> BoundTarget:
        """Retry opening the frozen target handle after a transient OS denial."""
        return self.bind(bound.target)

    def observe(self, conn, bound: BoundTarget) -> RuntimeFacts:
        identity = crawler_identity.registered(conn)
        if identity is None:
            handle = bound.capability
            if hasattr(handle, "poll"):
                alive = handle.poll() is None
            else:
                from . import browser_proc
                alive = browser_proc.process_capability_alive(handle)
            return RuntimeFacts(None, alive,
                                error="identity_missing" if alive is not False else None)
        if identity.pid != bound.target.pid or identity.started_at != bound.target.started_at:
            return RuntimeFacts(identity, False)
        handle = bound.capability
        if hasattr(handle, "poll"):
            alive = handle.poll() is None
        else:
            from . import browser_proc
            alive = browser_proc.process_capability_alive(handle)
        browser = None
        state = identity.browser_state or "UNKNOWN"
        if state == "OWNED" and identity.browser_pid and identity.browser_os_started:
            browser = BoundBrowser(int(identity.browser_pid), identity.browser_port,
                                   identity.browser_os_started)
        return RuntimeFacts(identity, alive, browser, state)

    def terminate(self, bound: BoundTarget) -> EffectResult:
        """结束绑定的那个目标；只碰传进来的句柄，不看调用方此刻的任何状态。"""
        handle = bound.capability
        if hasattr(handle, "poll"):
            try:
                handle.terminate()
                return EffectResult(True)
            except OSError:
                return EffectResult(False, "terminate_failed")
        from . import browser_proc
        if browser_proc.terminate_process_capability(handle):
            return EffectResult(True)
        return EffectResult(False, "terminate_failed")

    def close_browser(self, browser: BoundBrowser) -> EffectResult:
        """关闭绑定在目标上的浏览器。

        `launched_by_us=True` 不是调用方的声明，是 facts 推出来的：`observe()` 只在身份的
        `browser_state == "OWNED"` 时才给出 `BoundBrowser`，`BORROWED` / `NOT_STARTED` 根本没有
        浏览器可传。真正的授权检查在 `browser_proc.close_browser` 里（缺 pid 或 proof 一律拒绝），
        端口只用于日志，因此这里不拿端口的有无把门，也不看调用方当前的配置。
        """
        from . import browser_proc
        closed = browser_proc.close_browser(
            browser.port, launched_by_us=True, browser_pid=browser.pid,
            browser_os_started=browser.os_started)
        return EffectResult(True) if closed is not None else EffectResult(False, "browser_close_failed")

    def release(self, bound: BoundTarget) -> None:
        capability = bound.capability
        if hasattr(capability, "handle"):
            from . import browser_proc
            browser_proc.release_process_capability(capability)


@dataclass(frozen=True)
class StopInFlight:
    command: StopCommand
    bound: BoundTarget
    deadline: float
    acked: bool = False
    phase: StopPhase = StopPhase.STOPPING
    code: str | None = None
    # 关失败、还欠着的那一个浏览器：在失败那一刻冻结，重试只碰它，绝不按当前事实重选。
    pending_browser: BoundBrowser | None = None

    @property
    def kind(self):
        return self.command.kind.value

    @property
    def target(self):
        return self.command.target

    @property
    def state(self):
        return self.phase.value


class StopWatch:
    """目标贯穿整个停止窗口的停止编排。"""

    def __init__(self, runtime: StopRuntime, *, now=time.time,
                 grace_sec: float = 8.0, ack_grace_sec: float = 10.0) -> None:
        if runtime is None:
            raise TypeError("StopWatch requires runtime")
        self._runtime = runtime
        self._now = now
        self._grace_sec = grace_sec
        self._ack_grace_sec = ack_grace_sec
        self._in_flight: StopInFlight | None = None

    @property
    def grace_sec(self) -> float:
        return self._grace_sec

    @property
    def status(self) -> StopStatus:
        if self._in_flight is None:
            return StopStatus(StopPhase.IDLE)
        stop = self._in_flight
        return StopStatus(stop.phase, stop.command.kind, stop.command.target,
                          stop.code, stop.phase in (StopPhase.VERIFYING,
                                                    StopPhase.CLEANUP_PENDING))

    @property
    def state(self) -> str | None:
        return self.status.state

    def begin(self, conn, command: StopCommand) -> StopStatus:
        """开一个停止窗口：先确认目标仍是登记的那个身份，再冻结绑定、写下请求。

        `conn` 与 `command` 都是必须的。没有连接就没有「目标仍是当前身份」这道确认，而
        那条确认正是 ADR-0024 要求的开窗前提——旧签名用 `conn=None` 跳过它，那个口子随
        旧形一起删（ADR-0024 的删除清单）。
        """
        if command is None or not isinstance(command, StopCommand):
            raise TypeError("begin requires StopCommand")
        if conn is None:
            raise TypeError("begin requires a connection to the identity row")
        if self._in_flight is not None:
            active = self._in_flight.command
            if active.target == command.target and active.kind == command.kind:
                return self.status
            if active.target != command.target:
                raise RuntimeError("stop already targets another crawler")
            if command.kind is not StopKind.ABORT:
                raise RuntimeError("stop already active")
        current = crawler_identity.registered(conn)
        target = command.target
        if (current is None or current.pid != target.pid
                or current.started_at != target.started_at):
            raise RuntimeError("target_identity_unavailable")
        db = Database(conn)
        if command.kind is StopKind.PAUSE:
            ok = db.request_stop_if_current(
                round_id=command.round_id, kind=command.kind.value,
                target_pid=target.pid, target_started_at=target.started_at)
            if not ok:
                raise RuntimeError("target_identity_unavailable")
        else:
            db.clear_stop_request(target_pid=target.pid,
                                  target_started_at=target.started_at)
        bound = self._runtime.bind(command.target)
        self._in_flight = StopInFlight(command, bound,
                                       self._now() + self._grace_sec)
        return self.status

    def tick(self, conn) -> StopStatus:
        stop = self._in_flight
        if stop is None:
            return self.status
        db = Database(conn)
        if stop.bound.capability is None and hasattr(self._runtime, "rebind"):
            try:
                rebound = self._runtime.rebind(stop.bound)
            except Exception:
                rebound = None
            if rebound is not None and rebound.capability is not None:
                stop = replace(stop, bound=rebound)
                self._in_flight = stop
        try:
            stop = self._note_ack(db, stop)
        except Exception as exc:  # noqa: BLE001
            return self._verify(stop, "database_unavailable", str(exc))
        try:
            facts = self._runtime.observe(conn, stop.bound)
        except Exception as exc:  # noqa: BLE001
            return self._verify(stop, "observation_failed", str(exc))
        relation = self._relation(stop.command.target, facts)
        if stop.pending_browser is not None:
            return self._retry_cleanup(db, stop, facts.browser, relation)
        if relation in (StopRelation.REPLACED, StopRelation.GONE):
            return self._settle(db, stop)
        if relation is StopRelation.UNVERIFIABLE:
            return self._verify(stop, "target_unverifiable")
        if self._now() < stop.deadline:
            return self.status
        return self._force(conn, db, stop)

    @staticmethod
    def _relation(target, facts) -> StopRelation:
        who = facts.identity
        if who is not None and (who.pid != target.pid or who.started_at != target.started_at):
            return StopRelation.REPLACED
        if facts.error or facts.process_alive is None:
            return StopRelation.UNVERIFIABLE
        if not facts.process_alive:
            return StopRelation.GONE
        if who is None:
            return StopRelation.UNVERIFIABLE
        return StopRelation.EXACT

    def _note_ack(self, db, stop):
        if stop.acked or stop.command.kind is not StopKind.PAUSE:
            return stop
        request = db.stop_request()
        target = stop.command.target
        if request is None or not targets(request, pid=target.pid,
                                          started_at=target.started_at):
            return stop
        if not request["ack_at"]:
            return stop
        log.info("采集进程已回执停止请求，再等 %.0f 秒收尾。", self._ack_grace_sec)
        self._in_flight = replace(stop, acked=True,
                                  deadline=self._now() + self._ack_grace_sec,
                                  phase=StopPhase.CLOSING)
        return self._in_flight

    def _verify(self, stop, code, detail=None):
        if detail:
            log.warning("停止目标暂不可核验（%s）：%s", code, detail)
        self._in_flight = replace(stop, phase=StopPhase.VERIFYING, code=code)
        return self.status

    def _force(self, conn, db, stop):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:  # noqa: BLE001
            return self._verify(stop, "database_unavailable", str(exc))
        try:
            # Re-read A while the write lock is held. No new identity/browser
            # publication can interleave the process and browser actions.
            current = crawler_identity.registered(conn)
            target = stop.command.target
            if (current is not None
                    and (current.pid != target.pid or current.started_at != target.started_at)):
                self._delete_target_in_transaction(conn, target)
                conn.commit()
                self._finish(stop)
                return self.status
            current_facts = self._runtime.observe(conn, stop.bound)
            browser = current_facts.browser
            if (current_facts.browser_state in ("STARTING", "UNKNOWN", "OWNED")
                    and browser is None):
                conn.rollback()
                return self._verify(stop, "browser_binding_unavailable")
            effect = self._runtime.terminate(stop.bound)
            if not effect.ok:
                conn.rollback()
                return self._verify(stop, effect.code or "terminate_failed")
            after = self._runtime.observe(conn, stop.bound)
            if (after.identity is not None
                    and (after.identity.pid != target.pid
                         or after.identity.started_at != target.started_at)):
                self._delete_target_in_transaction(conn, target)
                conn.commit()
                self._finish(stop)
                return self.status
            if after.process_alive is not False:
                conn.rollback()
                return self._verify(stop, "process_still_running")
            if browser is not None:
                closed = self._runtime.close_browser(browser)
                if not closed.ok:
                    conn.rollback()
                    return self._pending_cleanup(stop, browser, closed.code)
            self._delete_target_in_transaction(conn, target)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:
                pass
            self._in_flight = replace(stop, phase=StopPhase.CLEANUP_PENDING,
                                      code="database_unavailable")
            log.warning("停止目标清理暂不可用：%s", exc)
            return self.status
        self._finish(stop)
        return self.status

    def _pending_cleanup(self, stop, browser, code):
        """停在这个相：还欠着一个没关掉的浏览器。

        上限**只在进入这个相时开一次**（判据是此刻还没欠着任何浏览器）——每次重试都续期的话，
        那个上限就形同虚设。
        """
        entering = stop.pending_browser is None
        self._in_flight = replace(
            stop, phase=StopPhase.CLEANUP_PENDING,
            code=code or "browser_close_failed", pending_browser=browser,
            deadline=self._now() + self._grace_sec if entering else stop.deadline)
        return self.status

    def _retry_cleanup(self, db, stop, browser, relation):
        """`CLEANUP_PENDING` 的重试主体：目标已经死透，只剩浏览器没关掉。

        `_force` 关浏览器失败时进程已被证明退出，所以下一次 tick 必然判出 `GONE` —— 而 `tick`
        里 `GONE` 的短路在 deadline 之前。不在这里重试的话，`close_browser` 每个窗口只会被调
        一次：一次瞬时失败就留下一个真在跑的孤儿浏览器占着 profile 与调试端口（IS-43），
        而条件清理还会把唯一能定位它的那份精确绑定一起抹掉。

        重试只认冻结下来的那一个 binding，绝不按当前事实重选（ADR-0024）。但**它必须有上限**：
        `browser_proc.close_browser` 要先 `bind_process`，PID 已经不存在时它永远回 `None`
        （实跑核过），不设上限窗口就永远停着，而界面在未收口的相里拒绝启动新的采集。
        """
        if relation is StopRelation.REPLACED:
            # B 接手了：结束旧重试，只留一句警告，不碰 B 的实例。
            log.warning("停止目标已被替换，结束旧目标未完成的浏览器收尾。")
            return self._settle(db, replace(stop, pending_browser=None))
        if self._now() >= stop.deadline:
            log.warning("浏览器收尾重试到窗口上限仍未成功，放弃这一个（PID %s，端口 %s）——"
                        "绑定随事实一起清掉，那个进程可能还在。", browser.pid, browser.port)
            return self._settle(db, replace(stop, pending_browser=None))
        if relation is StopRelation.UNVERIFIABLE:
            self._in_flight = replace(stop, code="target_unverifiable")
            return self.status
        if browser is not None and browser != stop.pending_browser:
            # 事实里的浏览器换了一个：原 binding 不再精确，那不是我们的目标。不动手，也不收口
            # ——收口会把没关掉的那个浏览器的绑定抹掉，正是要避免的伤害。等 B 出现或窗口到顶。
            self._in_flight = replace(stop, code="browser_binding_changed")
            return self.status
        closed = self._runtime.close_browser(stop.pending_browser)
        if not closed.ok:
            return self._pending_cleanup(stop, stop.pending_browser, closed.code)
        return self._settle(db, replace(stop, pending_browser=None))

    def _settle(self, db, stop):
        """条件清理 A 的三处事实并结束窗口；清理供不上库就留在核验里重试。"""
        try:
            self._cleanup_target(db, stop.command.target)
        except Exception as exc:  # noqa: BLE001
            return self._verify(stop, "database_unavailable", str(exc))
        self._finish(stop)
        return self.status

    @staticmethod
    def _cleanup_target(db, target):
        db.clear_browser_facts_if_current(target_pid=target.pid,
                                          target_started_at=target.started_at)
        db.clear_crawler_process(target_pid=target.pid,
                                 target_started_at=target.started_at)
        db.clear_stop_request(target_pid=target.pid,
                              target_started_at=target.started_at)

    @staticmethod
    def _delete_target_in_transaction(conn, target):
        conn.execute("DELETE FROM crawler_process WHERE id=1 AND pid=? AND started_at=?",
                     (target.pid, target.started_at))
        conn.execute("DELETE FROM stop_requests WHERE id=1 AND target_pid=? "
                     "AND target_started_at=?", (target.pid, target.started_at))

    def _finish(self, stop):
        self._runtime.release(stop.bound)
        self._in_flight = None
