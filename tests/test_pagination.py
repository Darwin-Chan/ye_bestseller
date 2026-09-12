"""IS-36：翻页后必须确认列表真的换了新内容，才继续读。

点击「下一页 / 加载更多」后是 AJAX 局部更新：旧卡片会先留在原地，所以
「页面已加载」和「至少有一张卡片」两个条件旧页本身就满足。这里锁的行为是：
翻页后用「商品卡片身份序列」判断列表是否真的变了；变了才继续读，始终没变就
如实报列表失败（店铺留待续跑），而不是把旧页再读一遍当成新页。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw, pagination
from bestseller_monitor.config import Shop
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

        self.assertEqual(pagination.list_identity(page), ("card-a", "card-b"))

    def test_wait_for_change_sees_a_late_update(self):
        # 异步更新延迟：前两次轮询还是旧列表，第三次才换过来。
        observe = _sequences([("p1",), ("p1",), ("p2",)])
        with patch.object(pagination, "_POLL_SEC", 0.0):
            self.assertTrue(
                pagination.wait_for_change(lambda: observe(), ("p1",), "第二页", 5.0))

    def test_wait_for_change_times_out_when_the_list_never_changes(self):
        with patch.object(pagination, "_POLL_SEC", 0.0):
            self.assertFalse(
                pagination.wait_for_change(lambda: ("p1",), ("p1",), "第二页", 0.05))

    def test_wait_for_change_lets_the_caller_continue_without_a_before_state(self):
        # 翻页前读不到列表身份：无从判断变化，放行由旧逻辑兜底，绝不因此误报失败。
        def observe():   # pragma: no cover - 不该被调用
            raise AssertionError("没有翻页前身份时不该轮询")

        self.assertTrue(pagination.wait_for_change(observe, (), "第二页", 0.05))


class ClickPathPaginationTests(unittest.TestCase):
    """点击式主路径（pw_cdp 驱动）的翻页等待。"""

    def setUp(self):
        self.page = MagicMock()
        locator = MagicMock()
        locator.count.return_value = 1
        self.page.locator.return_value = locator
        self.shop = Shop("A01", "店铺A", "https://shop.example/")
        self.opened: list[int] = []
        titles = iter(f"商品{i}" for i in range(1, 21))
        self.titles = lambda *_: next(titles)

    def _capture(self, page_, img, list_title, cfg_, punished, on_response, se,
                 db, round_id, shop_, offers, seen, idx=0, page_no=1,
                 human=None, deny_tracker=None):
        self.opened.append(page_no)
        offers.append((len(offers) + 1, str(page_no),
                       f"https://detail.1688.com/offer/{page_no}.html", list_title, ""))

    def _crawl(self, *, identities, click=None, max_pages=2):
        with patch.object(pagination, "_POLL_SEC", 0.0), \
             patch.object(browser_pw, "_WAIT_NEXT_SEC", 0.05), \
             patch.object(pagination, "list_identity", side_effect=identities), \
             patch.object(browser_pw, "_wait_cards", return_value=True), \
             patch.object(browser_pw, "_scroll_cards_until_stable", return_value=1), \
             patch.object(browser_pw, "intervention_kind", return_value=None), \
             patch.object(browser_pw, "_click_text_in_frames",
                          side_effect=click or (lambda _p, _label: True)), \
             patch.object(browser_pw, "_read_card_title", side_effect=self.titles), \
             patch.object(browser_pw, "_capture_card", side_effect=self._capture):
            return browser_pw.crawl_store_by_click(
                self.page, self.shop, _cfg(max_pages_per_shop=max_pages), MagicMock())

    def test_next_page_is_read_only_after_the_list_changes(self):
        # 旧卡片暂留：翻页前的身份要连着出现几次，之后才换成第二页。
        offers, pages = self._crawl(
            identities=_sequences([("p1",), ("p1",), ("p1",), ("p2",)]))

        self.assertEqual(self.opened, [1, 2], "第二页在列表真的换了之后才读")
        self.assertEqual((len(offers), pages), (2, 2))

    def test_load_more_advances_when_cards_are_appended(self):
        # 「加载更多」是追加：旧卡片还在，序列变长也算换了内容。
        click = lambda _page, label: label == "加载更多"
        self._crawl(identities=_sequences([("p1a", "p1b"), ("p1a", "p1b"),
                                           ("p1a", "p1b", "p2a")]),
                    click=click)

        self.assertEqual(self.opened, [1, 2])

    def test_unchanged_list_fails_instead_of_rereading_the_old_page(self):
        with self.assertRaises(ListingLoadFailed) as ctx:
            self._crawl(identities=_sequences([("p1",)]))

        self.assertIn("翻页后未确认新一页加载", str(ctx.exception))
        self.assertEqual(self.opened, [1], "旧页的卡片不会被当成第二页再读一遍")

    def test_unreadable_list_identity_keeps_the_old_flow(self):
        # 读不到列表身份时不做变化判断，按原来的「等卡片出现」继续，不误报失败。
        self._crawl(identities=_sequences([()]))

        self.assertEqual(self.opened, [1, 2])


if __name__ == "__main__":
    unittest.main()
