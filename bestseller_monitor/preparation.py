"""开轮前准备的运行态持有者：接纳、预算、等待与落定编排（ADR-0035 的闸门与 30 秒兜底）。

一次准备是一条运行：`request()` 原子接纳并安排后台执行，返回不透明运行凭据；
后台工作者跑真实的 `plan_step.prepare_week`（阻塞段），**先落定**（结果或失败发布、
唤醒等待者），再做缺口扫描这类只告警的慢活——扫描失败不撤销已落定的结果，
也不碰后来接纳的新运行。

预算：接纳那一刻建立 30 秒**单调**截止点（派发延迟计入），`snapshot()` 每次按截止点
推导「预算内 / 耗尽」，不靠定时器维护第二份状态；墙钟变化与它无关。超时不等于结束、
取消或可重跑——工作者继续跑，快照只是不再说「正在确认」。

运行身份：每次接纳是一条独立运行，落定/完成各有自己的通知；等待只对**指定那一次**
生效（`wait_for_open` 等它落定或它的剩余预算；`wait_for_finish` 等它连同扫描结束），
陌生持有者的凭据被拒绝，不隐式退化成等当前那次。

环境能力经运行 adapter 注入（单调读时、安排后台执行、按秒等待通知）；准备及扫描
固定构造时的配置与连接能力，不回读调用方的可变状态。规则不下放给 adapter。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import functools
import logging
import threading
import time

from . import plan_step
from .db import CST, Database

log = logging.getLogger(__name__)

# 开窗等「准备落定」的预算（秒，ADR-0035）：接纳那一刻起算，30 秒整即耗尽。
PREPARE_WAIT_SEC = 30.0


class ProductionPrepRuntime:
    """生产运行 adapter：真单调钟、daemon 线程、Event 等待（三件环境能力）。"""

    def monotonic(self) -> float:
        return time.monotonic()

    def start(self, work) -> None:
        threading.Thread(target=work, name="plan-prepare", daemon=True).start()

    def wait(self, signal: threading.Event, timeout: float | None) -> bool:
        return signal.wait(timeout)


class PrepPhase(str, enum.Enum):
    """准备运行态的五种对外解释；「预算内 / 耗尽」每次按截止点推导。"""

    NOT_STARTED = "not_started"
    CONFIRMING = "confirming"        # 运行中、还在预算里
    EXPIRED = "expired"              # 运行中、预算已经走完（不是结束、取消或可重跑）
    SETTLED = "settled"              # 已落定：结果或明确失败
    TRIAL_BYPASS = "trial_bypass"    # 本机试采：绕过正式准备，不启动也不消费结果


class RequestOutcome(str, enum.Enum):
    """`request()` 的接纳决定。"""

    ACCEPTED = "accepted"
    IN_FLIGHT = "in_flight"          # 未落定的准备在跑：拒绝（携带其凭据供显式等待）
    TRIAL_BYPASS = "trial_bypass"
    DISPATCH_FAILED = "dispatch_failed"   # 执行 adapter 派发失败：明确失败、可再试


class PrepFailure(str, enum.Enum):
    """本次准备的失败类别（有结果时为空）。"""

    EXCEPTION = "exception"
    DISPATCH_FAILED = "dispatch_failed"


@dataclasses.dataclass(frozen=True)
class PreparationSnapshot:
    """一次读取当前不可变事实：阶段、当前结果、失败类别与最近一份可用历史。

    「有 result」不等于「可以开轮」——READY/REFUSED 等类别仍由计划业务解释；
    新尝试开始时当前 result 清空，前一份保留为 history，失败重试不抹掉它。
    """

    phase: PrepPhase = PrepPhase.NOT_STARTED
    result: plan_step.PrepResult | None = None
    failure: PrepFailure | None = None
    history: plan_step.PrepResult | None = None

    @property
    def running(self) -> bool:
        """准备还在跑吗（不限预算）：页面据此决定要不要接着轮询。"""
        return self.phase in (PrepPhase.CONFIRMING, PrepPhase.EXPIRED)

    @property
    def confirming(self) -> bool:
        """还在 30 秒预算里吗：页面据此锁住勾选与开始。"""
        return self.phase is PrepPhase.CONFIRMING


class PreparationRun:
    """不透明运行凭据：调用方只负责把它原样传回 `wait_*`，不读里面的字段。"""

    def __init__(self, owner: "Preparation", deadline: float):
        self._owner = owner
        self._deadline = deadline
        self._settle = threading.Event()
        self._finish = threading.Event()
        self._settled = False
        self._finished = False
        self._result: plan_step.PrepResult | None = None
        self._failure: PrepFailure | None = None


@dataclasses.dataclass(frozen=True)
class RequestDecision:
    """`request()` 的返回：明确决定 + 可能的运行凭据（拒绝时指已在跑的那次）。"""

    outcome: RequestOutcome
    run: PreparationRun | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is RequestOutcome.ACCEPTED


class Preparation:
    """固定一个上下文（配置、连接能力、时刻来源）的准备持有者。

    调用方（Api）负责选owner：正式上下文一个实例、试采一个 bypass 实例；
    一个实例不自行按路径开库，也不检查调用方给的可调用对象内部换没换库。
    """

    def __init__(self, *, cfg, open_conn, now, runtime):
        self._cfg = cfg
        self._open_conn = open_conn
        self._now = now
        self._runtime = runtime
        self._bypass = bool(getattr(cfg, "local_trial", False))
        self._lock = threading.RLock()
        self._current: PreparationRun | None = None
        self._history: plan_step.PrepResult | None = None

    # ---------- 接纳 ----------

    def request(self) -> RequestDecision:
        """原子接纳一次准备并安排后台执行；未落定的运行在跑时拒绝，不排队、不重跑。"""
        with self._lock:
            if self._bypass:
                return RequestDecision(RequestOutcome.TRIAL_BYPASS)
            current = self._current
            if current is not None and not current._settled:
                return RequestDecision(RequestOutcome.IN_FLIGHT, run=current)
            if current is not None and current._result is not None:
                self._history = current._result
            run = PreparationRun(self, self._runtime.monotonic() + PREPARE_WAIT_SEC)
            self._current = run
            try:
                self._runtime.start(functools.partial(self._work, run))
            except BaseException as exc:                # noqa: BLE001
                # 派发失败也发布终态并唤醒——包括 BaseException 落在派发点的情形，
                # 否则这条运行永远不落定、后续请求全被拒；清理后仍保留传播语义。
                self._publish_settle(run, failure=PrepFailure.DISPATCH_FAILED)
                self._publish_finish(run)
                if not isinstance(exc, Exception):
                    raise
                log.warning("开轮前准备派发失败（界面照常可用，可再试）：%s", exc)
                return RequestDecision(RequestOutcome.DISPATCH_FAILED, run=run)
            return RequestDecision(RequestOutcome.ACCEPTED, run=run)

    def snapshot(self) -> PreparationSnapshot:
        """一次读取当前运行的不可变事实；不向调用方暴露事件或线程。"""
        with self._lock:
            return self._snapshot_of(self._current)

    # ---------- 等待 ----------

    def wait_for_open(self, run: PreparationRun) -> PreparationSnapshot:
        """开窗用：等**指定运行**落定或它的剩余预算，返回该次快照。

        不含缺口扫描，也不接受调用方另给一份预算；等待期间不持状态锁，
        唤醒后重读这次运行并重算剩余预算。已落定的事实优先于过时的超时判断。
        """
        self._check_owned(run)
        while True:
            with self._lock:
                if run._settled:
                    return self._snapshot_of(run)
                remaining = run._deadline - self._runtime.monotonic()
                if remaining <= 0:
                    return self._snapshot_of(run)
            self._runtime.wait(run._settle, remaining)

    def wait_for_finish(self, run: PreparationRun) -> plan_step.PrepResult | None:
        """同步 prepare 语义：等**指定运行**的后台工作连同缺口扫描结束。

        返回该次 result（失败或未产出时为 None）；不用于开窗，不受 30 秒限制，
        不重新安排任务。
        """
        self._check_owned(run)
        while True:
            with self._lock:
                if run._finished:
                    return run._result
            self._runtime.wait(run._finish, None)

    def _check_owned(self, run: PreparationRun) -> None:
        """陌生持有者的运行凭据被拒绝，不自动退化成等当前那次。"""
        if not isinstance(run, PreparationRun) or run._owner is not self:
            raise ValueError("不是这个准备持有者的运行凭据")

    # ---------- 后台工作 ----------

    def _work(self, run: PreparationRun) -> None:
        """一条运行的后台工作：阻塞段 → 落定 → 缺口扫描；完成通知在 finally 发布。"""
        try:
            result = self._prepare_once()
        except BaseException as exc:                    # noqa: BLE001
            # 先做可执行的落定/唤醒清理，再保留传播语义（BaseException 不伪装成功）。
            self._publish_settle(run, failure=PrepFailure.EXCEPTION)
            if not isinstance(exc, Exception):
                raise
            log.warning("开轮前准备没做成（界面照常可用，子进程开轮前还会再准备一次）：%s", exc)
        else:
            self._publish_settle(run, result=result)
            log.info("开轮前准备：%s（本周 %s，本机 %s 家店）",
                     result.status.value, result.week, len(result.my_shops))
            for warning in result.warnings:
                log.warning("开轮前准备：%s", warning)
            self._scan_gaps(result)
        finally:
            self._publish_finish(run)

    def _prepare_once(self) -> plan_step.PrepResult:
        """跑一次准备串的阻塞段（拉计划库 → 同步清单 → 确认/发布 → 落库）。"""
        conn = self._open_conn()
        try:
            # prepare_week 按北京日期算周：注入的时刻先折算到 CST，免得跨夜差一周
            moment = dt.datetime.fromisoformat(self._now()).astimezone(CST)
            return plan_step.prepare_week(self._cfg, Database(conn), now=moment, gap_scan=False)
        finally:
            conn.close()

    def _scan_gaps(self, result: plan_step.PrepResult) -> None:
        """缺口检查（只告警不拦）：落定之后再单独扫一遍，坏了也只记日志、不撤销结果。

        它属于采集机开轮前的准备（提醒本机落后了、去跑交换台）；纯汇总机没有开轮
        这一步，跳过——这条判据跟着落定后的扫描走，与 prepare_week 内那条一致。
        """
        if result.status is plan_step.PrepStatus.SKIPPED_MERGE_ONLY:
            return
        try:
            conn = self._open_conn()
            try:
                gaps = plan_step.scan_exchange_gaps(self._cfg.exchange_root, conn, result.week)
            finally:
                conn.close()
            for warning in plan_step.gap_warnings(gaps):
                log.warning("开轮前准备：%s", warning)
        except Exception as exc:                        # noqa: BLE001
            log.warning("缺口检查没做成（不影响开轮）：%s", exc)

    # ---------- 状态发布 ----------

    def _publish_settle(self, run: PreparationRun, *, result=None, failure=None) -> None:
        """落定与通知在同一同步保护下发布；等待者拿锁后重读的必是这一份。"""
        with self._lock:
            run._result = result
            run._failure = failure
            run._settled = True
            run._settle.set()

    def _publish_finish(self, run: PreparationRun) -> None:
        with self._lock:
            run._finished = True
            run._finish.set()

    def _snapshot_of(self, run: PreparationRun | None) -> PreparationSnapshot:
        if self._bypass:
            return PreparationSnapshot(phase=PrepPhase.TRIAL_BYPASS)
        if run is None:
            return PreparationSnapshot(history=self._history)
        if run._settled:
            phase = PrepPhase.SETTLED
        elif self._runtime.monotonic() < run._deadline:
            phase = PrepPhase.CONFIRMING
        else:
            phase = PrepPhase.EXPIRED
        return PreparationSnapshot(phase=phase, result=run._result,
                                   failure=run._failure, history=self._history)
