"""等待原语：条件 · 上限 · 轮询间隔 · 取消钩子（ADR-0042）。

这一层只钉原语本身（判据 A01／A02）：四处循环各自的语义——签名、返回值、异常文案——
留在它们自己的用例里，这里管的是它们共用的那部分。需要精密计时的用例整块替换
`waiting.time`（`FakeClock`），其余用真时钟配小数值（test_listing 的先例）。
"""
import unittest
from unittest.mock import patch

from bestseller_monitor import stop_request, waiting
from bestseller_monitor.delay import Humanizer
from helpers import crawler_cfg


class FakeClock:
    """`waiting.time` 的整块替身：虚拟钟只在睡眠时前进，`sleep` 不真睡。

    给了 `trace` 就把每片睡眠记进那条轨迹——「次序」（探针 → 问取消 → 响铃 → 睡）
    只有把睡眠也放进同一条时间线上才断言得了。
    """

    def __init__(self, trace: list[str] | None = None):
        self.now = 0.0
        self.sleeps: list[float] = []
        self._trace = trace

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self._trace is not None:
            self._trace.append("睡")


def _answers(*values):
    """按顺序给答案，用完后一直给最后一个。"""
    queue = list(values)

    def take():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return take


def _slices(clock: "FakeClock") -> list[float]:
    """睡过的每片时长，按 1e-9 取整：虚拟钟是浮点累加，末片总带一截尾数。"""
    return [round(piece, 9) for piece in clock.sleeps]


class UntilContractTests(unittest.TestCase):
    """A01：真值原样返回、超时记一行、零窗口、必填参数。"""

    def test_the_first_truth_is_returned_as_it_is(self):
        token = object()
        calls = []

        def probe():
            calls.append(1)
            return token

        self.assertIs(waiting.until(probe, timeout_sec=0.5, poll_sec=0.01), token)
        self.assertEqual(calls, [1], "真值一到手就结束")

    def test_it_keeps_polling_until_the_probe_speaks(self):
        answers = _answers(None, False, "", "好了")
        calls = []

        def probe():
            calls.append(1)
            return answers()

        self.assertEqual(waiting.until(probe, timeout_sec=1.0, poll_sec=0.01), "好了")
        self.assertEqual(len(calls), 4, "假值一路等下去，第 4 次才结束")

    def test_a_timeout_records_one_line_and_returns_none(self):
        with self.assertLogs("bestseller_monitor.waiting", level="INFO") as logs:
            result = waiting.until(lambda: False, timeout_sec=0.05, poll_sec=0.01,
                                   describe="商品卡片出现")

        self.assertIsNone(result, "超时不抛异常：按当前状态继续")
        self.assertEqual(len(logs.output), 1, "超时只记一行")
        self.assertEqual(logs.records[0].getMessage(),
                         "等待「商品卡片出现」超时(0s)，按当前状态继续。")

    def test_without_a_describe_nothing_is_recorded(self):
        with self.assertNoLogs("bestseller_monitor.waiting", level="INFO"):
            result = waiting.until(lambda: False, timeout_sec=0.05, poll_sec=0.01)

        self.assertIsNone(result)

    def test_a_zero_window_neither_probes_nor_asks_to_cancel(self):
        probes, cancels = [], []

        with self.assertLogs("bestseller_monitor.waiting", level="INFO") as logs:
            result = waiting.until(lambda: probes.append(1) or True, timeout_sec=0,
                                   poll_sec=0.01, describe="端口就绪",
                                   cancel=lambda: cancels.append(1))
            self.assertIsNone(waiting.until(lambda: probes.append(1) or True, timeout_sec=-1,
                                            poll_sec=0.01))

        self.assertIsNone(result, "窗口 0 也要走一次出口：按超时返回")
        self.assertEqual(probes, [], "窗口 0 不探测")
        self.assertEqual(cancels, [], "窗口 0 不问取消")
        self.assertEqual(len(logs.output), 1, "describe 给了就照记一行（榜单页的零窗口行为）")

    def test_poll_sec_and_timeout_sec_are_required(self):
        with self.assertRaises(TypeError):
            waiting.until(lambda: True, timeout_sec=1)
        with self.assertRaises(TypeError):
            waiting.until(lambda: True, poll_sec=0.1)


class CancelTests(unittest.TestCase):
    """A02：取消在入口与每次睡前被问；抛出的异常原样上抛；没装钩子时零副作用。"""

    def setUp(self):
        self.addCleanup(stop_request.uninstall)

    def test_cancel_is_asked_at_the_entry_and_before_every_sleep(self):
        clock = FakeClock()
        probes, cancels = [], []

        with patch.object(waiting, "time", clock):
            result = waiting.until(lambda: probes.append(1) and False,
                                   timeout_sec=0.5, poll_sec=0.1,
                                   cancel=lambda: cancels.append(1))

        self.assertIsNone(result)
        self.assertEqual(_slices(clock), [0.1] * 5)
        self.assertEqual(len(cancels), len(clock.sleeps) + 1, "入口一次 + 每次睡前一次")
        self.assertEqual(len(probes), len(clock.sleeps) + 1, "超时那一轮先探测、后判到点")

    def test_a_cancel_exception_goes_up_as_it_is(self):
        boom = stop_request.StopRequested("收到界面暂停请求")
        probes = []

        with self.assertRaises(stop_request.StopRequested) as caught:
            waiting.until(lambda: probes.append(1) or True, timeout_sec=1.0, poll_sec=0.01,
                          cancel=lambda: (_ for _ in ()).throw(boom))

        self.assertIs(caught.exception, boom, "原样上抛：不换类型也不包一层")
        self.assertEqual(probes, [], "入口那次取消在第一次探测之前")

    def test_a_stop_between_rounds_is_claimed_before_the_next_sleep(self):
        clock = FakeClock()
        cancels, probes = [], []

        def cancel():
            cancels.append(1)
            if len(cancels) >= 2:
                raise stop_request.StopRequested("停")

        with patch.object(waiting, "time", clock):
            with self.assertRaises(stop_request.StopRequested):
                waiting.until(lambda: probes.append(1) and False, timeout_sec=10.0,
                              poll_sec=0.1, cancel=cancel)

        self.assertEqual(probes, [1], "第一轮探测过了")
        self.assertEqual(clock.sleeps, [], "第二次问（第一片的睡前）就抛出：一片都没睡")

    def test_the_default_cancel_is_the_installed_stop_hook(self):
        stop_request.install(
            lambda: (_ for _ in ()).throw(stop_request.StopRequested("停")))

        with self.assertRaises(stop_request.StopRequested):
            waiting.until(lambda: False, timeout_sec=5.0, poll_sec=0.01)

    def test_without_a_hook_the_wait_is_untouched(self):
        stop_request.uninstall()

        self.assertEqual(waiting.until(lambda: "就绪", timeout_sec=1.0, poll_sec=0.01),
                         "就绪")


class WaitShapeTests(unittest.TestCase):
    """A02：`on_wait` 每轮一次且在睡前；纯等待只受上限与取消约束。"""

    def test_on_wait_runs_once_per_round_before_the_sleep(self):
        trace: list[str] = []
        clock = FakeClock(trace=trace)

        with patch.object(waiting, "time", clock):
            waiting.until(lambda: trace.append("探针") and False, timeout_sec=0.2,
                          poll_sec=0.1, cancel=lambda: trace.append("问取消"),
                          on_wait=lambda: trace.append("响铃"))

        self.assertEqual(trace, ["问取消",
                                 "探针", "问取消", "响铃", "睡",
                                 "探针", "问取消", "响铃", "睡",
                                 "探针"],
                         "入口一次取消；每轮探针 → 问取消 → 响铃 → 睡；到点那轮不响铃")

    def test_a_plain_wait_only_obeys_the_deadline_and_cancel(self):
        clock = FakeClock()
        cancels = []

        with patch.object(waiting, "time", clock):
            result = waiting.until(None, timeout_sec=0.2, poll_sec=0.1,
                                   cancel=lambda: cancels.append(1))

        self.assertIsNone(result, "探针为 None：条件永不成立，等满上限即返回")
        self.assertEqual(_slices(clock), [0.1, 0.1])
        self.assertEqual(len(cancels), 3, "入口一次 + 两片各一次")

    def test_the_last_slice_never_crosses_the_deadline(self):
        clock = FakeClock()

        with patch.object(waiting, "time", clock):
            waiting.until(None, timeout_sec=0.25, poll_sec=0.1)

        self.assertEqual(_slices(clock), [0.1, 0.1, 0.05],
                         "末片切到上限为止：一场等待的总时长不超过上限")

    def test_a_probe_exception_goes_up_as_it_is(self):
        boom = ValueError("页面没了")
        calls = []

        def probe():
            calls.append(1)
            if len(calls) == 2:
                raise boom
            return False

        with self.assertRaises(ValueError) as caught:
            waiting.until(probe, timeout_sec=5.0, poll_sec=0.01)

        self.assertIs(caught.exception, boom, "吞异常是探针闭包自己的事，原语不上手")


class ExtendSentinelTests(unittest.TestCase):
    """`EXTEND`：真值哨兵——原语不认得它，真值原样返回的契约不为它开口子。"""

    def test_extend_is_truthy_and_comes_out_as_it_is(self):
        self.assertTrue(waiting.EXTEND, "哨兵必须是真值，探针才能拿它结束本次窗口")

        self.assertIs(waiting.until(lambda: waiting.EXTEND, timeout_sec=1.0, poll_sec=0.01),
                      waiting.EXTEND)
        self.assertEqual(repr(waiting.EXTEND), "EXTEND")


class SleepMigrationTests(unittest.TestCase):
    """A03：`Humanizer.sleep` 迁到原语之后，切片语义逐字保留。"""

    def setUp(self):
        self.addCleanup(stop_request.uninstall)
        stop_request.uninstall()

    def test_sleep_keeps_half_second_slices_and_its_total(self):
        human = Humanizer(crawler_cfg(long_pause_interval=(12, 20)))
        clock = FakeClock()

        with patch.object(waiting, "time", clock):
            human.sleep(1.2)

        self.assertEqual(_slices(clock), [0.5, 0.5, 0.2],
                         "0.5 秒一片、末片切到剩余量：1.2 秒就是 1.2 秒")
