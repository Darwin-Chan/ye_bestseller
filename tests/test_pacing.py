"""主动停顿：配额账本、惰性触发与界面读数（ADR-0037）。

单位换算（一页 = 30 个详情）在 `pacing.DETAILS_PER_PAGE` 一处定义，这里钉的是它的用法：
列表阶段按页记、补采阶段按详情折算、到点只在「下一次要开始工作」时停。
"""
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from bestseller_monitor import click_listing, pacing, pipeline, stop_request, views
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, DayBoundaryReached, connect
from bestseller_monitor.delay import Humanizer
from frozen_clock import frozen_clock
from helpers import crawler_cfg, FakeCard, ScriptedListing, new_round


class PacingTests(unittest.TestCase):
    """配额账本本身：不碰页面、不碰数据库。"""

    def setUp(self):
        self.human = MagicMock()
        self.events: list[tuple[str, dict]] = []
        self.addCleanup(stop_request.uninstall)

    def make(self, **overrides):
        values = {"pause_every_pages": 2, "pause_sec": (30.0, 30.0)}
        values.update(overrides)
        return pacing.Pacing(crawler_cfg(**values), self.human, emit=self.emit)

    def emit(self, event: str, **kw: object) -> None:
        self.events.append((event, kw))

    def sleeps(self) -> list[float]:
        return [call.args[0] for call in self.human.sleep.call_args_list]

    def test_the_quota_counts_pages_and_pauses_before_the_next_one(self):
        p = self.make()
        for _ in range(5):
            p.page_started(shop_key="A01")

        # 每 2 页停一次：第 3、第 5 页开始之前各停一次（第 1、2 页只记账）。
        self.assertEqual(self.sleeps(), [30.0, 30.0])

    def test_the_pause_is_lazy_so_finishing_on_the_quota_never_waits(self):
        p = self.make()
        for _ in range(2):
            p.page_started(shop_key="A01")

        self.assertEqual(self.sleeps(), [], "凑满配额但后面没有工作了，不该白停")

    def test_backfill_details_count_as_a_thirtieth_of_a_page(self):
        p = self.make()
        for _ in range(pacing.DETAILS_PER_PAGE * 2):
            p.detail_started(shop_key="A01")

        # 2 页 = 60 个详情：第 60 个之后、第 61 个之前停——这里刚满 60，还没到下一次。
        self.assertEqual(self.sleeps(), [])
        p.detail_started(shop_key="A01")
        self.assertEqual(self.sleeps(), [30.0])

    def test_zero_pages_turns_the_rule_off(self):
        p = self.make(pause_every_pages=0)
        for _ in range(pacing.DETAILS_PER_PAGE * 10):
            p.detail_started(shop_key="A01")
            p.page_started(shop_key="A01")

        self.assertEqual(self.sleeps(), [])

    def test_the_pause_events_carry_the_planned_seconds(self):
        p = self.make()
        for _ in range(3):
            p.page_started(shop_key="A01")

        names = [event for event, _ in self.events]
        self.assertEqual(names, [pacing.PAUSE_EVENT, pacing.RESUME_EVENT])
        self.assertEqual(pacing.note_seconds(self.events[0][1]["note"]), 30.0)
        self.assertEqual(self.events[0][1]["shop_key"], "A01")
        self.assertEqual(pacing.note_seconds(self.events[1][1]["note"]), 30.0)

    def test_the_pause_is_interruptible_within_a_slice(self):
        cfg = crawler_cfg(pause_every_pages=1, pause_sec=(600.0, 600.0))
        p = pacing.Pacing(cfg, Humanizer(cfg), emit=None)
        stop_request.install(
            lambda: (_ for _ in ()).throw(DayBoundaryReached("跨天")))
        p.page_started(shop_key="A01")   # 记满一页的配额

        started = time.time()
        with self.assertRaises(DayBoundaryReached):
            p.page_started(shop_key="A01")   # 到点该停了：跨天要在第一片内打断
        self.assertLess(time.time() - started, 1.0)


class PacingWalkTests(unittest.TestCase):
    """接进点击式遍历：停顿发生在两页之间，页面还没翻、下一批请求还没发出。"""

    def setUp(self):
        self.enterContext(frozen_clock())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = connect(Path(tmp.name) / "pacing.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.round_id = new_round(self.db, "A01")
        self.shop = Shop("A01", "店铺A", "https://A01.example/")
        self.human = MagicMock()
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **kw: object) -> None:
        self.events.append((event, kw))

    def run_walk(self, pages, cfg):
        page = ScriptedListing(pages)
        walk = click_listing.ShopWalk(
            page, self.shop, cfg, self.human, db=self.db, round_id=self.round_id,
            emit=self.emit, pacing=pacing.Pacing(cfg, self.human, emit=self.emit))
        return page, walk.run()

    def test_the_walk_pauses_between_pages_not_before_the_first_one(self):
        cfg = crawler_cfg(pause_every_pages=2, pause_sec=(30.0, 30.0), max_pages_per_shop=3)
        pages = [[FakeCard("商品1", offer_id="11")],
                 [FakeCard("商品2", offer_id="12")],
                 [FakeCard("商品3", offer_id="13")]]

        page, (offers, pages_read) = self.run_walk(pages, cfg)

        self.assertEqual(pages_read, 3)
        self.assertEqual(len(offers), 3)
        self.assertEqual([call.args[0] for call in self.human.sleep.call_args_list],
                         [30.0], "第 3 页开始之前停一次")
        order = [event for event, _ in self.events
                 if event in ("list_page", "pacing_pause")]
        self.assertEqual(order, ["list_page", "list_page", "pacing_pause", "list_page"],
                         "停顿夹在第 2 页与第 3 页之间")

    def test_the_walk_does_not_pause_when_the_last_page_fills_the_quota(self):
        cfg = crawler_cfg(pause_every_pages=2, pause_sec=(30.0, 30.0))
        pages = [[FakeCard("商品1", offer_id="11")],
                 [FakeCard("商品2", offer_id="12")]]

        page, (offers, pages_read) = self.run_walk(pages, cfg)

        self.assertEqual(pages_read, 2)
        self.assertEqual(self.human.sleep.call_args_list, [], "最后一页之后没有工作，不停")

    def test_the_backfill_path_shares_the_same_quota(self):
        """补采按「30 个详情 = 1 页」折算：1 页的配额下，第 31 个补采详情之前停一次。"""
        cfg = crawler_cfg(pause_every_pages=1, pause_sec=(30.0, 30.0))
        offers = [{"shop_key": "A01", "offer_id": str(i),
                   "product_url": f"https://detail.1688.com/offer/{i}.html"}
                  for i in range(pacing.DETAILS_PER_PAGE + 1)]
        captured: list[dict] = []

        pipeline._capture_pending_offers(
            self.db, cfg, self.human, self.round_id, offers, captured.append,
            pacing=pacing.Pacing(cfg, self.human, emit=self.emit))

        self.assertEqual(len(captured), pacing.DETAILS_PER_PAGE + 1, "停顿不该吞掉任何一条")
        self.assertEqual([call.args[0] for call in self.human.sleep.call_args_list], [30.0])


NOW = "2026-09-13T04:00:00+00:00"


class PacingViewTests(unittest.TestCase):
    """界面读数：倒计时只在采集进程还在跑时给；累计只算真的等过的。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = connect(Path(tmp.name) / "pacing-view.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.round_id = new_round(self.db, "A01", run_date="2026-09-13")

    def add_event(self, event: str, seconds: float, *, ago: float) -> None:
        """往事件表里塞一条停顿事件：`ago` 是它相对 NOW 发生在多少秒之前。"""
        ts = (datetime.fromisoformat(NOW) - timedelta(seconds=ago)).isoformat()
        self.conn.execute(
            "INSERT INTO event_log(round_id, shop_key, event, kind, phase, ts, note) "
            "VALUES (?, 'A01', ?, 'work', 'pacing', ?, ?)",
            (self.round_id, event, ts, pacing.pause_note(seconds)))
        self.conn.commit()

    def view(self, *, running: bool):
        return views.run_view(self.conn, state=views.UiState(crawler_running=running), now=NOW)

    def test_the_countdown_runs_while_the_process_is_alive(self):
        self.add_event(pacing.PAUSE_EVENT, 1800, ago=300)

        view = self.view(running=True)

        self.assertEqual(view["pacing_remaining_sec"], 1500)
        self.assertEqual(view["pacing_remaining_text"], "25:00")
        self.assertEqual(view["pacing_paused_sec"], 300.0)

    def test_a_finished_pause_counts_in_full_and_leaves_no_countdown(self):
        self.add_event(pacing.PAUSE_EVENT, 1800, ago=3000)
        self.add_event(pacing.RESUME_EVENT, 1800, ago=1200)

        view = self.view(running=True)

        self.assertIsNone(view["pacing_remaining_sec"])
        self.assertEqual(view["pacing_paused_sec"], 1800.0)

    def test_no_countdown_when_the_process_is_gone(self):
        self.add_event(pacing.PAUSE_EVENT, 1800, ago=300)

        view = self.view(running=False)

        self.assertIsNone(view["pacing_remaining_sec"],
                          "进程不在了，倒计时就是一条旧数据")
        self.assertEqual(view["pacing_paused_sec"], 300.0, "等过的那一段照算")
