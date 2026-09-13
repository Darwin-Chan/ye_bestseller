"""榜单页 module：列表身份（IS-36）与「推进到下一批」的确认规则。

点击「下一页 / 加载更多」后是 AJAX 局部更新：旧卡片会先留在原地，所以
「页面已加载」和「至少有一张卡片」两个条件旧页本身就满足。这里锁的行为是：
翻页后用「商品卡片身份序列」判断列表是否真的变了；变了才继续读，始终没变就
如实报列表失败（店铺留待续跑），而不是把旧页再读一遍当成新页。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import listing
from bestseller_monitor.listing import ListingLoadFailed


def _cfg(**overrides):
    values = {
        "timeout_ms": 1,
        "max_pages_per_shop": 2,
        "max_detail_opportunities_per_round": 1000,
        "human_pause_minutes": 1,
        "intervention_confirmation_sec": 0,
        "deny_backoff_sec": 0.0,
        "deny_retry2_backoff_sec": 0.0,
        "deny_window_minutes": 10,
        "deny_shop_limit": 7,
        "deny_round_limit": 10,
        "list_delay_sec": (0.0, 0.0),
        "action_delay_sec": (0.0, 0.0),
        "read_delay_sec": (0.0, 0.0),
        "detail_delay_sec": (0.0, 0.0),
        "long_pause_interval": (1, 1),
        "long_pause_sec": (0.0, 0.0),
        "batch_size": 1,
        "batch_rest_sec": (0.0, 0.0),
        "retry_base_sec": 0.0,
        "retry_jitter_sec": 0.0,
        "shuffle_within_shop": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _sequences(values):
    """按顺序取值，用完后一直返回最后一个值。"""
    seq = list(values)

    def take(*_args, **_kwargs):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return take


class IdentityTests(unittest.TestCase):
    """列表身份的取法：跨 frame 汇总，读不到时给空元组（由调用方决定怎么处理）。"""

    def test_playwright_identity_merges_frames_and_skips_broken_ones(self):
        main, broken, sub = MagicMock(), MagicMock(), MagicMock()
        main.evaluate.return_value = ["card-a"]
        broken.evaluate.side_effect = RuntimeError("frame detached")
        sub.evaluate.return_value = ["card-b"]
        page = MagicMock()
        page.frames = [main, broken, sub]

        self.assertEqual(listing.list_identity(page), ("card-a", "card-b"))

    def test_wait_for_change_sees_a_late_update(self):
        # 异步更新延迟：前两次轮询还是旧列表，第三次才换过来。
        observe = _sequences([("p1",), ("p1",), ("p2",)])
        with patch.object(listing, "_POLL_SEC", 0.0):
            self.assertTrue(
                listing.wait_for_change(lambda: observe(), ("p1",), "第二页", 5.0))

    def test_wait_for_change_times_out_when_the_list_never_changes(self):
        with patch.object(listing, "_POLL_SEC", 0.0):
            self.assertFalse(
                listing.wait_for_change(lambda: ("p1",), ("p1",), "第二页", 0.05))

    def test_wait_for_change_lets_the_caller_continue_without_a_before_state(self):
        # 翻页前读不到列表身份：无从判断变化，放行由旧逻辑兜底，绝不因此误报失败。
        def observe():   # pragma: no cover - 不该被调用
            raise AssertionError("没有翻页前身份时不该轮询")

        self.assertTrue(listing.wait_for_change(observe, (), "第二页", 0.05))


class PrepareTests(unittest.TestCase):
    """prepare：打开发榜页并准备好——等首屏卡片 → 人工介入 → 点「销量」排序。"""

    def setUp(self):
        self.page = MagicMock()
        self.human = MagicMock()
        self.cfg = _cfg()
        self.calls: list[str] = []
        self.events: list[str] = []

    def _prepare(self, *, cards_ready: bool = True, kind: str | None = None,
                 can_sort: bool = True):
        def step(name: str, result):
            self.calls.append(name)
            return result

        with patch.object(listing, "wait_cards",
                          side_effect=lambda *a, **k: step("等卡片", cards_ready)) as wait_cards, \
             patch.object(listing, "intervention_kind", return_value=kind), \
             patch.object(listing, "wait_for_resolution",
                          side_effect=lambda *a, **k: step("人工介入", None)) as resolve, \
             patch.object(listing, "click_text_in_frames",
                          side_effect=lambda *a, **k: step("排序", can_sort)):
            listing.prepare(self.page, "https://shop.example/", self.cfg, self.human,
                            describe="店铺 A01 首屏", punished=False,
                            emit=lambda event, **kw: self.events.append(event))
        return wait_cards, resolve

    def test_opens_the_page_waits_for_cards_then_sorts(self):
        self._prepare()

        self.page.goto.assert_called_once_with("https://shop.example/",
                                              wait_until="domcontentloaded")
        self.human.after_load.assert_called_once_with()
        self.human.before_action.assert_called_once_with()
        self.assertEqual(self.events, ["list_load", "list_sort"])

    def test_verification_is_handled_before_the_sort_click(self):
        self._prepare(kind="滑块")

        self.assertLess(self.calls.index("人工介入"), self.calls.index("排序"),
                        "验证挡着的时候「销量」点不着，先处理人工介入")

    def test_missing_first_screen_cards_fail_instead_of_reading_an_empty_list(self):
        with self.assertRaises(ListingLoadFailed) as ctx:
            self._prepare(cards_ready=False)

        self.assertIn("未加载商品卡片", str(ctx.exception))
        self.assertNotIn("排序", self.calls, "没卡片就不该点排序")
        self.assertEqual(self.events, [], "没准备好不算 list_load")


class AdvanceTests(unittest.TestCase):
    """advance：推进到下一批，并确认列表真的换了内容（IS-36）。

    旧卡片暂留不算换页；点了翻页而列表始终没变，要如实报榜单失败——
    返回 False 只表示「这一页后面没有下一批」。
    """

    def setUp(self):
        self.page = MagicMock()
        self.human = MagicMock()
        self.cfg = _cfg()

    def _advance(self, *, identities, click=None):
        with patch.object(listing, "_POLL_SEC", 0.0), \
             patch.object(listing, "WAIT_NEXT_SEC", 0.05), \
             patch.object(listing, "list_identity", side_effect=identities), \
             patch.object(listing, "wait_cards", return_value=True), \
             patch.object(listing, "click_text_in_frames",
                          side_effect=click or (lambda _page, _label: True)) as clicker:
            advanced = listing.advance(self.page, self.human, self.cfg, "店铺 A01 第 1 页")
        return advanced, clicker

    def test_next_page_is_confirmed_before_the_caller_reads_on(self):
        # 旧卡片暂留：翻页前的身份要连着出现几次，之后才换成第二页。
        advanced, _ = self._advance(
            identities=_sequences([("p1",), ("p1",), ("p1",), ("p2",)]))

        self.assertTrue(advanced, "列表真的换了才算推进成功")
        self.human.before_action.assert_called_once_with()

    def test_load_more_counts_as_advancing_when_cards_are_appended(self):
        # 「加载更多」是追加：旧卡片还在，序列变长也算换了内容。
        advanced, clicker = self._advance(
            identities=_sequences([("p1a", "p1b"), ("p1a", "p1b"), ("p1a", "p1b", "p2a")]),
            click=lambda _page, label: label == "加载更多")

        self.assertTrue(advanced)
        self.assertEqual([call.args[1] for call in clicker.call_args_list],
                         ["下一页", "加载更多"], "先试下一页，再回退加载更多")

    def test_unchanged_list_fails_instead_of_returning_false(self):
        with self.assertRaises(ListingLoadFailed) as ctx:
            self._advance(identities=_sequences([("p1",)]))

        self.assertIn("翻页后未确认新一页加载", str(ctx.exception))

    def test_no_next_batch_returns_false_without_failing(self):
        advanced, _ = self._advance(identities=_sequences([("p1",)]),
                                    click=lambda _page, _label: False)

        self.assertFalse(advanced, "没有下一批不是失败")

    def test_unreadable_list_identity_keeps_the_old_flow(self):
        # 读不到列表身份时不做变化判断，按原来的「等卡片出现」继续，不误报失败。
        advanced, _ = self._advance(identities=_sequences([()]))

        self.assertTrue(advanced)


if __name__ == "__main__":
    unittest.main()
