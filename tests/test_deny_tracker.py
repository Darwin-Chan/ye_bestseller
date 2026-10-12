import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import guard
from bestseller_monitor.config import Shop
from bestseller_monitor.guard import DenyTracker, RoundDenyExceeded, ShopDenyExceeded


class DenyTrackerTests(unittest.TestCase):
    def test_counts_and_rolling_window(self):
        t = DenyTracker(window_sec=600)
        t.record("A")
        t.record("A")
        t.record("A")
        t.record("B")
        self.assertEqual(t.shop_count("A"), 3)
        self.assertEqual(t.shop_count("B"), 1)
        self.assertEqual(t.round_count(), 4)

    def test_exceptions_importable(self):
        self.assertTrue(issubclass(ShopDenyExceeded, Exception))
        self.assertTrue(issubclass(RoundDenyExceeded, Exception))


class DenyLedgerTests(unittest.TestCase):
    """issue 13/ADR-0054：账本跨续跑延续（播种）＋整轮首次 deny 的降温与它的记忆。"""

    def test_seed_counts_join_the_window(self):
        now = time.time()
        t = DenyTracker(600, seed=[(now, "A"), (now, "A"), (now, "B")])

        self.assertEqual(t.shop_count("A"), 2)
        self.assertEqual(t.round_count(), 3)

    def test_seed_entries_outside_the_window_are_pruned(self):
        t = DenyTracker(600, seed=[(time.time() - 700, "A")])

        self.assertEqual(t.shop_count("A"), 0, "窗口过期自然清零，不另设停机规则")

    def test_record_writes_a_deny_hit_event(self):
        emitted: list[tuple[str, dict]] = []
        t = DenyTracker(600, emit=lambda event, **kw: emitted.append((event, kw)))

        t.record("A")
        t.record("A")
        t.record("B")

        self.assertEqual([event for event, _ in emitted], [guard.DENY_HIT_EVENT] * 3)
        self.assertEqual(emitted[1][1]["shop_key"], "A")
        self.assertEqual(emitted[1][1]["note"], "shop_n=2&round_n=2")

    def test_the_first_deny_of_the_round_cools_down_once(self):
        slept: list[float] = []
        emitted: list[tuple[str, dict]] = []
        t = DenyTracker(600, emit=lambda event, **kw: emitted.append((event, kw)),
                        cooldown=slept.append)

        t.record("A")
        t.record("B")

        self.assertEqual(slept, [600.0], "整轮只降一次温")
        cooldowns = [(event, kw) for event, kw in emitted
                     if event == guard.DENY_COOLDOWN_EVENT]
        self.assertEqual(len(cooldowns), 1)
        self.assertEqual(cooldowns[0][1]["note"], "reason=first_deny&sec=600")

    def test_a_resume_that_already_cooled_down_does_not_cool_again(self):
        slept: list[float] = []
        t = DenyTracker(600, cooldown_fired=True, cooldown=slept.append)

        t.record("A")

        self.assertEqual(slept, [], "续跑读回「已降过温」的记忆，不重复罚停")

    def test_the_cooldown_event_is_written_before_the_sleep(self):
        """先写事件后睡：降温被打断也算已发过，续跑不会把它再来一遍。"""
        order: list[str] = []
        t = DenyTracker(600, emit=lambda event, **kw: order.append(event),
                        cooldown=lambda seconds: order.append("sleep"))

        t.record("A")

        self.assertEqual(order, [guard.DENY_HIT_EVENT, guard.DENY_COOLDOWN_EVENT, "sleep"])
