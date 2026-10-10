"""准备持有者（preparation）：接纳、30 秒单调预算、等待与落定的公开 interface。

测的是这一个 module 的公开面：`request` / `wait_for_open` / `wait_for_finish` /
`snapshot`。运行 adapter 只替换三件环境能力——单调读时、安排后台执行、按秒等待通知；
规则（截止点、阶段、结果归属）不下放给它，也不靠 sleep 碰运气。
业务仍是真实的 `plan_step.prepare_week`（计划库不可达时它很快返回结局）。
"""
import tempfile
import threading
import unittest
from pathlib import Path

from bestseller_monitor import plan_step, preparation
from bestseller_monitor.db import connect
from helpers import crawler_cfg

NOW = "2026-09-21T04:00:00+00:00"       # 北京时间 2026-09-21（周一）12:00，2026-W39


class GatedConnection:
    """连接工厂：指定第几次调用进闸门（等用例放行）、第几次直接失败。

    连接能力就是调用方给准备持有者的那一件；闸在这里等于把「准备/扫描卡在开库处」
    做成语义明确的事件，而不是去改私有字段。没点名的调用照常开库。
    """

    def __init__(self, path, *, hold=(), fail_on=None):
        self.path = path
        self.hold = set(hold)
        self.fail_on = fail_on
        self.lock = threading.Lock()
        self.calls = 0
        self._entered: dict[int, threading.Event] = {}
        self._release: dict[int, threading.Event] = {}

    def _events(self, call):
        return (self._entered.setdefault(call, threading.Event()),
                self._release.setdefault(call, threading.Event()))

    def __call__(self):
        with self.lock:
            self.calls += 1
            call = self.calls
        entered, release = self._events(call)
        if call in self.hold:
            entered.set()
            if not release.wait(5):
                raise AssertionError(f"test gate for connection #{call} was not released")
        if call == self.fail_on:
            raise RuntimeError("injected connection failure (test)")
        return connect(self.path)

    def wait_entered(self, call=1, timeout=5) -> bool:
        return self._events(call)[0].wait(timeout)

    def release_call(self, call=1) -> None:
        self._events(call)[1].set()

    def hold_next(self) -> None:
        """把下一次调用临时挂进闸门（调用次数与页面取数交错的用例用它）。"""
        with self.lock:
            self.hold.add(self.calls + 1)


def start_waiting(call):
    """在真实线程里发起一次阻塞调用；返回（线程, 结果槽）。test_gui 也共用这一件。"""
    out = {}

    def run():
        try:
            out["value"] = call()
        except BaseException as exc:                    # noqa: BLE001
            out["error"] = exc

    thread = threading.Thread(target=run, name="prep-waiter", daemon=True)
    thread.start()
    return thread, out


class ControlledRuntime:
    """可控准备运行 adapter：单调时钟可推进；派发只入队或按开关抛异常；等待由用例选择落法。

    - `start` 只把后台工作记下来（用例显式 launch 到真实线程），或按 `dispatch_exc`
      抛一个指定的异常（含 BaseException 情形）；
    - `wait` 两档：默认虚拟档——信号未置位时把同一时钟走到超时点再返回；
      `real_wait=True` 档——真等信号（有 5 秒硬上限，仅供「等工作完成」的用例）。
    """

    def __init__(self, *, real_wait=False, dispatch_exc=None, auto_start=False):
        self.clock = 1000.0
        self.queued = []
        self.started = 0
        self.launched = []
        self.real_wait = real_wait
        self.dispatch_exc = dispatch_exc
        self.auto_start = auto_start
        self.parked = threading.Event()

    def monotonic(self) -> float:
        return self.clock

    def advance(self, seconds: float) -> None:
        self.clock += seconds

    def start(self, work) -> None:
        if self.dispatch_exc is not None:
            raise self.dispatch_exc
        self.started += 1
        if self.auto_start:
            thread = threading.Thread(target=work, name="prep-work", daemon=True)
            thread.start()
            self.launched.append(thread)
            return
        self.queued.append(work)

    def wait(self, signal, timeout):
        if signal.is_set():
            return True
        self.parked.set()
        if timeout is None or self.real_wait:
            return signal.wait(5.0 if timeout is None else min(timeout, 5.0))
        self.clock += max(0.0, timeout)
        return signal.is_set()

    def launch(self, test) -> threading.Thread:
        """把队首的后台工作放到真实线程里跑（daemon，用例退出前必收）。"""
        work = self.queued.pop(0)
        thread = threading.Thread(target=work, name="prep-work", daemon=True)
        thread.start()
        self.launched.append(thread)
        test.addCleanup(lambda: thread.join(5))
        return thread

    def join_all(self, timeout=5) -> None:
        for thread in list(self.launched):
            thread.join(timeout)


class PreparationTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.shop_csv = self.dir / "shops.csv"
        self.shop_csv.write_text(
            "shop_key,shop_name,shop_url,pages,active\n"
            "A01,店一,https://a01.example/,3,1\n", encoding="utf-8")
        self.db_path = self.dir / "prep.db"

    def cfg(self, **overrides):
        values = dict(shop_csv=self.shop_csv, db_file=self.db_path,
                      exchange_root=self.dir / "exchange")   # 不可达：准备走快路
        values.update(overrides)
        return crawler_cfg(**values)

    def owner(self, *, runtime=None, open_conn=None, cfg=None):
        return preparation.Preparation(
            cfg=cfg or self.cfg(), open_conn=open_conn or GatedConnection(self.db_path),
            now=lambda: NOW, runtime=runtime or ControlledRuntime())

    def wait_async(self, call):
        """在真实线程里发起一次等待；返回（线程, 结果槽）。"""
        return start_waiting(call)


class SnapshotTests(PreparationTestBase):
    def test_a_fresh_owner_reads_not_started(self):
        owner = self.owner()

        snapshot = owner.snapshot()

        self.assertIs(snapshot.phase, preparation.PrepPhase.NOT_STARTED)
        self.assertFalse(snapshot.running)
        self.assertFalse(snapshot.confirming)
        self.assertIsNone(snapshot.result)
        self.assertIsNone(snapshot.history)
        self.assertIsNone(snapshot.failure)


class AdmissionTests(PreparationTestBase):
    def test_request_admits_once_and_rejects_a_duplicate_while_unsettled(self):
        gate = GatedConnection(self.db_path, hold={1})
        runtime = ControlledRuntime()
        owner = self.owner(runtime=runtime, open_conn=gate)

        first = owner.request()
        self.assertTrue(first.accepted)
        self.assertEqual(len(runtime.queued), 1, "接纳即安排一次后台执行")
        self.assertIs(owner.snapshot().phase, preparation.PrepPhase.CONFIRMING)

        runtime.launch(self)                      # 工作者进闸门、卡在开库
        self.assertTrue(gate.wait_entered(1), "工作者进闸门、卡在开库")

        duplicate = owner.request()
        self.assertFalse(duplicate.accepted)
        self.assertIs(duplicate.outcome, preparation.RequestOutcome.IN_FLIGHT)
        self.assertIs(duplicate.run, first.run, "拒绝要带上已在跑的凭据，供显式等待")
        self.assertEqual(gate.calls, 1, "重复请求不许再进一次执行")
        self.assertEqual(len(runtime.queued), 0, "不排队、不隐式重跑")

        gate.release_call(1)


class BudgetTests(PreparationTestBase):
    def test_budget_is_a_monotonic_30_seconds_from_admission(self):
        runtime = ControlledRuntime()
        owner = self.owner(runtime=runtime,
                           open_conn=GatedConnection(self.db_path, hold={1}))
        owner.request()

        runtime.advance(29.999)
        self.assertIs(owner.snapshot().phase, preparation.PrepPhase.CONFIRMING)
        runtime.advance(0.001)
        self.assertIs(owner.snapshot().phase, preparation.PrepPhase.EXPIRED,
                      "30 秒整即耗尽")
        self.assertTrue(owner.snapshot().running, "超时不等于结束")
        runtime.advance(10.0)
        self.assertIs(owner.snapshot().phase, preparation.PrepPhase.EXPIRED)

        duplicate = owner.request()
        self.assertFalse(duplicate.accepted, "超时≠落定：仍拒绝，不排队、不重跑")
        self.assertIs(duplicate.outcome, preparation.RequestOutcome.IN_FLIGHT)
        self.assertIs(owner.snapshot().phase, preparation.PrepPhase.EXPIRED,
                      "截止点没有被重置（重置会把它带回预算内）")

    def test_the_deadline_is_set_at_admission_not_at_dispatch(self):
        gate = GatedConnection(self.db_path, hold={1})
        runtime = ControlledRuntime()
        owner = self.owner(runtime=runtime, open_conn=gate)
        owner.request()

        runtime.advance(29.0)
        runtime.launch(self)                      # 到 +29 秒才真派发
        self.assertTrue(gate.wait_entered(1))
        runtime.advance(1.0)

        self.assertIs(owner.snapshot().phase, preparation.PrepPhase.EXPIRED,
                      "预算自接纳起算：派发延迟不重置截止点")
        gate.release_call(1)

    def test_waiters_take_only_the_runs_remaining_budget(self):
        gate = GatedConnection(self.db_path, hold={1, 2})
        runtime = ControlledRuntime()
        owner = self.owner(runtime=runtime, open_conn=gate)
        decision = owner.request()
        runtime.launch(self)
        self.assertTrue(gate.wait_entered(1))
        runtime.advance(20.0)                     # 后加入者只剩 10 秒

        thread, out = self.wait_async(lambda: owner.wait_for_open(decision.run))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", out)
        self.assertIs(out["value"].phase, preparation.PrepPhase.EXPIRED)
        self.assertEqual(runtime.clock, 1030.0, "等待只消耗该运行的剩余预算，不重置")
        gate.release_call(1)
        gate.release_call(2)


class WaitTests(PreparationTestBase):
    def test_wait_for_open_returns_on_settlement_without_waiting_for_the_scan(self):
        gate = GatedConnection(self.db_path, hold={1, 2})
        runtime = ControlledRuntime(real_wait=True)
        owner = self.owner(runtime=runtime, open_conn=gate)
        decision = owner.request()
        runtime.launch(self)
        self.assertTrue(gate.wait_entered(1))

        thread, out = self.wait_async(lambda: owner.wait_for_open(decision.run))
        self.assertTrue(runtime.parked.wait(5), "等待者挂在这次运行的通知上")
        gate.release_call(1)                      # 放行阻塞段：落定即返回

        thread.join(5)
        self.assertFalse(thread.is_alive(), "wait_for_open 不等缺口扫描")
        snapshot = out["value"]
        self.assertIs(snapshot.phase, preparation.PrepPhase.SETTLED)
        self.assertIs(snapshot.result.status, plan_step.PrepStatus.REFUSED)
        self.assertTrue(gate.wait_entered(2, timeout=5), "落定之后扫描才开始")
        self.assertFalse(owner.snapshot().running, "落定即释放接纳限制（扫描不算）")
        gate.release_call(2)

    def test_wait_for_finish_waits_for_the_worker_and_its_scan(self):
        gate = GatedConnection(self.db_path, hold={1, 2})
        runtime = ControlledRuntime(real_wait=True)
        owner = self.owner(runtime=runtime, open_conn=gate)
        decision = owner.request()
        runtime.launch(self)
        self.assertTrue(gate.wait_entered(1))

        thread, out = self.wait_async(lambda: owner.wait_for_finish(decision.run))
        self.assertTrue(runtime.parked.wait(5))
        gate.release_call(1)                      # 落定，但扫描还卡着
        self.assertTrue(gate.wait_entered(2, timeout=5))
        self.assertTrue(thread.is_alive(), "扫描没结束，完成等待不能返回")

        gate.release_call(2)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIs(out["value"].status, plan_step.PrepStatus.REFUSED)

    def test_a_settled_run_reads_settled_even_past_its_budget(self):
        gate = GatedConnection(self.db_path, hold={1})
        runtime = ControlledRuntime()
        owner = self.owner(runtime=runtime, open_conn=gate)
        decision = owner.request()
        runtime.launch(self)
        self.assertTrue(gate.wait_entered(1))
        runtime.advance(31.0)
        self.assertIs(owner.snapshot().phase, preparation.PrepPhase.EXPIRED)

        gate.release_call(1)
        owner.wait_for_finish(decision.run)

        snapshot = owner.snapshot()
        self.assertIs(snapshot.phase, preparation.PrepPhase.SETTLED,
                      "已落定事实优先于过时的超时判断")
        self.assertIs(snapshot.result.status, plan_step.PrepStatus.REFUSED)
        runtime.parked.clear()
        self.assertIs(owner.wait_for_open(decision.run).phase,
                      preparation.PrepPhase.SETTLED)
        self.assertFalse(runtime.parked.is_set(), "已落定：等待直接返回，不再挂起")

    def test_a_foreign_runs_credential_is_rejected(self):
        owner = self.owner(open_conn=GatedConnection(self.db_path, hold={1}))
        other = self.owner(runtime=ControlledRuntime())
        decision = owner.request()

        with self.assertRaises(ValueError):
            other.wait_for_open(decision.run)
        with self.assertRaises(ValueError):
            other.wait_for_finish(decision.run)
        with self.assertRaises(ValueError):
            owner.wait_for_open(None)


class AttemptLifecycleTests(PreparationTestBase):
    def test_a_new_attempt_clears_the_current_result_and_keeps_history(self):
        gate = GatedConnection(self.db_path, hold={1})
        runtime = ControlledRuntime()
        owner = self.owner(runtime=runtime, open_conn=gate)
        first = owner.request()
        runtime.launch(self)
        self.assertTrue(gate.wait_entered(1))
        gate.release_call(1)
        first_result = owner.wait_for_finish(first.run)
        self.assertIs(first_result.status, plan_step.PrepStatus.REFUSED)
        self.assertIs(owner.snapshot().result, first_result)
        self.assertIsNone(owner.snapshot().history)

        second = owner.request()
        self.assertTrue(second.accepted, "落定之后可以明确再试")
        running = owner.snapshot()
        self.assertIsNone(running.result, "新尝试清空当前结果，不冒充本次成功")
        self.assertIs(running.history, first_result, "前一份结果保留为历史")
        self.assertIs(running.phase, preparation.PrepPhase.CONFIRMING)
        self.assertFalse(owner.request().accepted, "同一时刻只有一个未落定运行")

        runtime.launch(self)
        second_result = owner.wait_for_finish(second.run)
        self.assertIs(second_result.status, plan_step.PrepStatus.REFUSED)
        self.assertIs(owner.snapshot().result, second_result)
        self.assertIs(owner.snapshot().history, first_result)

    def test_dispatch_failure_settles_wakes_and_allows_retry(self):
        runtime = ControlledRuntime(dispatch_exc=RuntimeError("dispatch failed (test)"))
        owner = self.owner(runtime=runtime, open_conn=GatedConnection(self.db_path))
        decision = owner.request()

        self.assertIs(decision.outcome, preparation.RequestOutcome.DISPATCH_FAILED)
        self.assertFalse(decision.accepted)
        snapshot = owner.snapshot()
        self.assertIs(snapshot.phase, preparation.PrepPhase.SETTLED,
                      "派发失败立即落定，不装成在跑")
        self.assertIs(snapshot.failure, preparation.PrepFailure.DISPATCH_FAILED)
        self.assertIsNone(snapshot.result)
        self.assertFalse(snapshot.running)
        self.assertIs(owner.wait_for_open(decision.run).phase,
                      preparation.PrepPhase.SETTLED, "等待者被唤醒，不卡死")
        self.assertIsNone(owner.wait_for_finish(decision.run))

        runtime.dispatch_exc = None
        retry = owner.request()
        self.assertTrue(retry.accepted, "派发失败之后可以明确再试")
        runtime.launch(self)
        self.assertIs(owner.wait_for_finish(retry.run).status, plan_step.PrepStatus.REFUSED)

    def test_a_base_exception_at_dispatch_still_settles_without_self_lock(self):
        """BaseException 落在派发点：同样先落定/唤醒再传播，不把持有者锁死在「在跑」。"""
        runtime = ControlledRuntime(dispatch_exc=KeyboardInterrupt())
        owner = self.owner(runtime=runtime)

        with self.assertRaises(KeyboardInterrupt):
            owner.request()

        snapshot = owner.snapshot()
        self.assertIs(snapshot.phase, preparation.PrepPhase.SETTLED)
        self.assertIs(snapshot.failure, preparation.PrepFailure.DISPATCH_FAILED)
        runtime.dispatch_exc = None
        self.assertTrue(owner.request().accepted, "清理之后还能明确再试，不永久拒绝")


class FailureTests(PreparationTestBase):
    def test_an_exception_settles_wakes_waiters_and_allows_retry(self):
        gate = GatedConnection(self.db_path, hold={1}, fail_on=1)
        runtime = ControlledRuntime(real_wait=True)
        owner = self.owner(runtime=runtime, open_conn=gate)
        first = owner.request()
        runtime.launch(self)
        self.assertTrue(gate.wait_entered(1))

        thread, out = self.wait_async(lambda: owner.wait_for_open(first.run))
        self.assertTrue(runtime.parked.wait(5))
        gate.release_call(1)                      # 阻塞段里抛异常

        thread.join(5)
        self.assertFalse(thread.is_alive(), "异常同样唤醒等待者")
        snapshot = out["value"]
        self.assertIs(snapshot.phase, preparation.PrepPhase.SETTLED)
        self.assertIs(snapshot.failure, preparation.PrepFailure.EXCEPTION)
        self.assertIsNone(snapshot.result)
        self.assertFalse(snapshot.running)
        self.assertIsNone(owner.wait_for_finish(first.run), "异常运行完成等待返回 None")

        retry = owner.request()
        self.assertTrue(retry.accepted, "异常落定之后可以明确再试")
        runtime.launch(self)
        self.assertIs(owner.wait_for_finish(retry.run).status, plan_step.PrepStatus.REFUSED)

    def test_base_exception_settles_publishes_finish_then_propagates(self):
        def boom():
            raise KeyboardInterrupt

        runtime = ControlledRuntime()
        owner = self.owner(runtime=runtime, open_conn=boom)
        decision = owner.request()
        work = runtime.queued.pop(0)              # 延后派发：在本线程里跑，才能接住传播

        with self.assertRaises(KeyboardInterrupt):
            work()

        snapshot = owner.snapshot()
        self.assertIs(snapshot.phase, preparation.PrepPhase.SETTLED,
                      "BaseException 也先做落定清理")
        self.assertIs(snapshot.failure, preparation.PrepFailure.EXCEPTION)
        self.assertIsNone(owner.wait_for_finish(decision.run),
                          "finally 已发布完成通知，等待者不卡死")


class RunIdentityTests(PreparationTestBase):
    def test_a_late_scan_and_finish_stay_with_their_own_run(self):
        gate = GatedConnection(self.db_path, hold={1, 2})
        runtime = ControlledRuntime()
        owner = self.owner(runtime=runtime, open_conn=gate)
        a = owner.request()
        runtime.launch(self)
        self.assertTrue(gate.wait_entered(1))

        a_thread, a_out = self.wait_async(lambda: owner.wait_for_finish(a.run))
        self.assertTrue(runtime.parked.wait(5))
        gate.release_call(1)                      # A 落定；A 的扫描卡在 call 2
        self.assertTrue(gate.wait_entered(2, timeout=5))
        a_result = owner.wait_for_open(a.run).result
        self.assertIsNotNone(a_result)

        b = owner.request()                       # 落定即释放接纳：A 的扫描没完也能接纳 B
        self.assertTrue(b.accepted)
        self.assertIs(owner.snapshot().history, a_result)

        runtime.launch(self)                      # B 自己跑完（call 3/4 自由）
        b_result = owner.wait_for_finish(b.run)
        self.assertIs(owner.snapshot().result, b_result)
        self.assertTrue(a_thread.is_alive(), "A 的完成等待只等 A 的扫描")

        gate.release_call(2)                      # A 的迟到扫描收尾
        a_thread.join(5)
        self.assertFalse(a_thread.is_alive())
        self.assertIs(a_out["value"], a_result, "A 的完成等待返回 A 的结果，不是 B 的")
        self.assertIs(owner.snapshot().result, b_result,
                      "A 的迟到扫描/完成不改写 B 的运行状态")

    def test_a_scan_failure_does_not_revoke_the_settled_result(self):
        gate = GatedConnection(self.db_path, fail_on=2)   # 第 2 次连接＝缺口扫描
        runtime = ControlledRuntime()
        owner = self.owner(runtime=runtime, open_conn=gate)
        decision = owner.request()
        runtime.launch(self)

        result = owner.wait_for_finish(decision.run)

        self.assertIs(result.status, plan_step.PrepStatus.REFUSED)
        self.assertIs(owner.snapshot().result, result, "扫描失败不撤销已落定的结果")
        self.assertIs(owner.snapshot().phase, preparation.PrepPhase.SETTLED)
        self.assertIsNone(owner.snapshot().failure, "扫描失败不是准备失败")


class TrialBypassTests(PreparationTestBase):
    def test_a_bypass_owner_never_starts_a_formal_run(self):
        runtime = ControlledRuntime()
        owner = self.owner(runtime=runtime, cfg=self.cfg(local_trial=True))

        decision = owner.request()

        self.assertIs(decision.outcome, preparation.RequestOutcome.TRIAL_BYPASS)
        self.assertIsNone(decision.run)
        self.assertEqual(runtime.queued, [], "试采不安排正式准备")
        snapshot = owner.snapshot()
        self.assertIs(snapshot.phase, preparation.PrepPhase.TRIAL_BYPASS)
        self.assertFalse(snapshot.running)
        self.assertFalse(snapshot.confirming)
        self.assertIsNone(snapshot.result)


if __name__ == "__main__":
    unittest.main()
