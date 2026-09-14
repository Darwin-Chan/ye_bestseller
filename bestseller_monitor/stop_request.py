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
    compatibility: bool = False


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
                 browser_state: str | None = None):
        self.alive = alive
        self.browser = browser
        self.browser_state = browser_state
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
    """Windows process adapter.  It binds handles at begin and never reselects a PID."""

    def __init__(self, *, own_process=None, browser_enabled: bool = True,
                 terminate=None, close_browser=None, browser_port=None):
        self.own_process = own_process
        self.browser_enabled = browser_enabled
        self._terminate_callback = terminate
        self._close_callback = close_browser
        self._browser_port = browser_port

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
        elif (state == "NOT_STARTED" and handle is not None
              and not isinstance(getattr(handle, "pid", None), int)
              and self._browser_port is not None):
            # Compatibility for the pre-publication test double only. Real Popen
            # handles always have a numeric pid and therefore never use this path.
            port = self._browser_port() if callable(self._browser_port) else self._browser_port
            browser = BoundBrowser(bound.target.pid, port, compatibility=True)
        return RuntimeFacts(identity, alive, browser, state)

    def terminate(self, bound: BoundTarget) -> EffectResult:
        if self._terminate_callback is not None:
            result = self._terminate_callback(bound)
            return result if isinstance(result, EffectResult) else EffectResult(bool(result))
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
        if not self.browser_enabled:
            return EffectResult(True)
        if self._close_callback is not None:
            result = self._close_callback(browser)
            return result if isinstance(result, EffectResult) else EffectResult(bool(result))
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


class _LegacyRuntime:
    """旧版五回调的兼容 adapter；生产接线不再依赖它。"""

    def __init__(self, *, kill_child, stop_foreign, close_browser, is_running,
                 identity_of):
        self.kill_child = kill_child
        self.stop_foreign = stop_foreign
        self.close = close_browser
        self.running = is_running
        self.identity_of = identity_of
        self.kind = PAUSE
        self.conn = None

    def bind(self, target):
        return BoundTarget(target, target)

    def observe(self, conn, bound):
        self.conn = conn
        who = self.identity_of(conn)
        return RuntimeFacts(who, bool(self.running()), BoundBrowser(1), "OWNED")

    def terminate(self, bound):
        self.kill_child()
        if self.kind == ABORT:
            who = self.identity_of(self.conn)
            if who is not None:
                self.stop_foreign(who)
        return EffectResult(True)

    def close_browser(self, browser):
        self.close()
        return EffectResult(True)

    def release(self, bound):
        return None


@dataclass(frozen=True)
class StopInFlight:
    command: StopCommand
    bound: BoundTarget
    deadline: float
    acked: bool = False
    phase: StopPhase = StopPhase.STOPPING
    code: str | None = None

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

    def __init__(self, runtime: StopRuntime | None = None, *, now=time.time,
                 grace_sec: float = 8.0, ack_grace_sec: float = 10.0, **legacy):
        if runtime is None and legacy:
            required = {"kill_child", "stop_foreign", "close_browser", "is_running",
                        "identity_of"}
            if not required <= legacy.keys():
                raise TypeError("StopWatch requires runtime")
            runtime = _LegacyRuntime(**{key: legacy[key] for key in required})
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

    def forget(self) -> None:
        if self._in_flight is None:
            return
        if not isinstance(self._runtime, _LegacyRuntime):
            raise RuntimeError("active stop targets cannot be forgotten")
        if self._in_flight is not None:
            self._runtime.release(self._in_flight.bound)
        self._in_flight = None

    def begin(self, conn, command=None):
        """新接口 `begin(conn, StopCommand)`；兼容旧接口 `begin(kind, target)`。"""
        legacy_call = command is not None and isinstance(conn, (str, StopKind))
        if legacy_call:
            command = StopCommand(conn, command)
            conn = None
        if command is None or not isinstance(command, StopCommand):
            raise TypeError("begin requires StopCommand")
        if self._in_flight is not None:
            active = self._in_flight.command
            if active.target == command.target and active.kind == command.kind:
                return self.status
            if active.target != command.target:
                raise RuntimeError("stop already targets another crawler")
            if command.kind is not StopKind.ABORT:
                raise RuntimeError("stop already active")
        if conn is not None:
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
        if isinstance(self._runtime, _LegacyRuntime):
            self._runtime.kind = command.kind.value
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
        if relation in (StopRelation.REPLACED, StopRelation.GONE):
            try:
                self._cleanup_target(db, stop.command.target)
            except Exception as exc:  # noqa: BLE001
                return self._verify(stop, "database_unavailable", str(exc))
            self._finish(stop)
            return self.status
        if relation is StopRelation.UNVERIFIABLE:
            return self._verify(stop, "target_unverifiable")
        if self._now() < stop.deadline:
            return self.status
        return self._force(conn, db, stop, facts)

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

    def _force(self, conn, db, stop, facts):
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
                if isinstance(self._runtime, _LegacyRuntime):
                    self._in_flight = None
                    return self.status
                return self._verify(stop, "process_still_running")
            if browser is not None:
                closed = self._runtime.close_browser(browser)
                if not closed.ok:
                    conn.rollback()
                    self._in_flight = replace(stop, phase=StopPhase.CLEANUP_PENDING,
                                              code=closed.code or "browser_close_failed")
                    return self.status
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
