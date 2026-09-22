"""主动停顿：配额账本、惰性触发与界面读数（ADR-0038）。

配额的单位是**详情访问**：一次真正打开了商品详情页的访问记 1，翻列表页不记——跳过密集的
一天里，一页 30 张卡可能一张详情都没开（round_36），为没发生的工作付停顿是改前的毛病。
这里钉的是它的用法：页面真的打开之后记账、到点只在「下一次要开详情」时停。
"""
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# 补采那一组用例走 `test_p1` 的真 HTML 夹具（共用一个模块，不 import 它的测试类）。
import test_p1
from bestseller_monitor import (browser_pw, click_listing, detail, detail_visit, pacing,
                                pipeline, stop_request, views)
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, DayBoundaryReached, connect, cst_date
from bestseller_monitor.delay import Humanizer
from frozen_clock import frozen_clock
from helpers import crawler_cfg, FakeCard, ScriptedListing, new_round, submit_offer


class PacingTests(unittest.TestCase):
    """配额账本本身：不碰页面、不碰数据库。"""

    def setUp(self):
        self.human = MagicMock()
        self.events: list[tuple[str, dict]] = []
        self.addCleanup(stop_request.uninstall)

    def make(self, **overrides):
        values = {"pause_every_detail_visits": 2, "pause_sec": (30.0, 30.0)}
        values.update(overrides)
        return pacing.Pacing(crawler_cfg(**values), self.human, emit=self.emit)

    def visit(self, p) -> None:
        """一次详情访问的生产顺序：要开详情之前先问停顿，页面真的打开之后再记账。"""
        p.before_detail_visit(shop_key="A01")
        p.detail_visit_opened()

    def emit(self, event: str, **kw: object) -> None:
        self.events.append((event, kw))

    def sleeps(self) -> list[float]:
        return [call.args[0] for call in self.human.sleep.call_args_list]

    def test_the_quota_counts_detail_visits_and_pauses_before_the_next_one(self):
        p = self.make()
        for _ in range(5):
            self.visit(p)

        # 每 2 次详情访问停一次：第 3、第 5 次开始之前各停一次（前两次只记账）。
        self.assertEqual(self.sleeps(), [30.0, 30.0])

    def test_the_pause_is_lazy_so_finishing_on_the_quota_never_waits(self):
        p = self.make()
        for _ in range(2):
            self.visit(p)

        self.assertEqual(self.sleeps(), [], "凑满配额但后面没有工作了，不该白停")

    def test_zero_detail_visits_turns_the_rule_off(self):
        p = self.make(pause_every_detail_visits=0)
        for _ in range(50):
            self.visit(p)

        self.assertEqual(self.sleeps(), [])

    def test_the_pause_events_carry_the_planned_seconds(self):
        p = self.make()
        for _ in range(3):
            self.visit(p)

        names = [event for event, _ in self.events]
        self.assertEqual(names, [pacing.PAUSE_EVENT, pacing.RESUME_EVENT])
        self.assertEqual(pacing.note_seconds(self.events[0][1]["note"]), 30.0)
        self.assertEqual(self.events[0][1]["shop_key"], "A01")
        self.assertEqual(pacing.note_seconds(self.events[1][1]["note"]), 30.0)

    def test_the_pause_is_interruptible_within_a_slice(self):
        cfg = crawler_cfg(pause_every_detail_visits=1, pause_sec=(600.0, 600.0))
        p = pacing.Pacing(cfg, Humanizer(cfg), emit=None)
        stop_request.install(
            lambda: (_ for _ in ()).throw(DayBoundaryReached("跨天")))
        self.visit(p)   # 记满一次的配额

        started = time.time()
        with self.assertRaises(DayBoundaryReached):
            p.before_detail_visit(shop_key="A01")   # 到点该停了：跨天要在第一片内打断
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

    def test_the_walk_pauses_between_details_not_before_the_first_one(self):
        cfg = crawler_cfg(pause_every_detail_visits=2, pause_sec=(30.0, 30.0),
                          max_pages_per_shop=3)
        cards = [FakeCard("商品1", offer_id="11"),
                 FakeCard("商品2", offer_id="12"),
                 FakeCard("商品3", offer_id="13")]
        opens_at_pause: list[int] = []
        self.human.sleep.side_effect = lambda *_: opens_at_pause.append(cards[2].opens)

        page, (offers, pages_read) = self.run_walk([[card] for card in cards], cfg)

        self.assertEqual(pages_read, 3)
        self.assertEqual(len(offers), 3)
        self.assertEqual([call.args[0] for call in self.human.sleep.call_args_list], [30.0],
                         "两次详情访问之后、第三次开始之前停一次")
        self.assertEqual(opens_at_pause, [0], "停顿时第 3 张卡还没点开，请求也就还没发出")
        order = [event for event, _ in self.events
                 if event in ("popup_open", "pacing_pause")]
        self.assertEqual(order, ["popup_open", "popup_open", "pacing_pause", "popup_open"],
                         "停顿夹在第 2 次与第 3 次详情访问之间")

    def test_the_walk_never_pauses_when_every_card_is_already_collected(self):
        """round_36 的毛病：整页整页的同名暂缓，一张详情都没开，就不该占配额。"""
        cfg = crawler_cfg(pause_every_detail_visits=1, pause_sec=(30.0, 30.0),
                          max_pages_per_shop=3)
        for i in range(1, 4):
            submit_offer(self.db, f"{i}00", cst_date(), 3, name=f"商品{i}")
        pages = [[FakeCard(f"商品{i}", offer_id=f"{i}00")] for i in range(1, 4)]

        page, (offers, pages_read) = self.run_walk(pages, cfg)

        self.assertEqual(pages_read, 3, "页照读——只是不再为它记账")
        self.assertEqual([event for event, _ in self.events if event == "popup_open"], [],
                         "这些卡一个详情弹窗都没打开")
        self.assertEqual(self.human.sleep.call_args_list, [], "没开过详情，配额一次都没动")

    def test_the_walk_does_not_pause_when_the_last_detail_fills_the_quota(self):
        cfg = crawler_cfg(pause_every_detail_visits=2, pause_sec=(30.0, 30.0))
        pages = [[FakeCard("商品1", offer_id="11")],
                 [FakeCard("商品2", offer_id="12")]]

        page, (offers, pages_read) = self.run_walk(pages, cfg)

        self.assertEqual(pages_read, 2)
        self.assertEqual(self.human.sleep.call_args_list, [], "最后一页之后没有工作，不停")

    def test_a_card_that_never_opened_does_not_charge(self):
        """点了但弹窗没打开：页面没打开，就不算一次详情访问。

        配额 2 之下两张没打开的卡都没记账——记了的话，后面第一次真访问就得先停 30 秒。
        """
        cfg = crawler_cfg(pause_every_detail_visits=2, pause_sec=(30.0, 30.0),
                          max_pages_per_shop=4)
        pages = [[FakeCard("商品1", offer_id="11", opened=False)],
                 [FakeCard("商品2", offer_id="12", opened=False)],
                 [FakeCard("商品3", offer_id="13")],
                 [FakeCard("商品4", offer_id="14")]]

        page, (offers, pages_read) = self.run_walk(pages, cfg)

        self.assertEqual(pages_read, 4)
        self.assertEqual(len(offers), 2, "只有真打开的两张卡拿到了商品")
        self.assertEqual(self.human.sleep.call_args_list, [])


class PacingBackfillTests(unittest.TestCase):
    """接进逐店补采（走 `_capture_one_pw` 的真接缝）：页面真的打开才记账。"""

    def setUp(self):
        self.enterContext(frozen_clock())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = connect(Path(tmp.name) / "pacing-backfill.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.round_id = new_round(self.db, "A01")
        self.human = MagicMock()
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **kw: object) -> None:
        self.events.append((event, kw))

    def offer(self, offer_id: str) -> dict:
        return {"shop_key": "A01", "shop_url": "https://A01.example/", "shop_name": "店铺A",
                "offer_id": offer_id, "list_title": f"商品{offer_id}",
                "product_url": f"https://detail.1688.com/offer/{offer_id}.html"}

    def capture(self, cfg, offer, ledger):
        """跑一次补采（页面走真 `detail_visit`，导航换成剧本）：返回导航桩。"""
        page = MagicMock()
        page.url = offer["product_url"]
        page.content.return_value = test_p1.DETAIL_HTML
        with patch.object(browser_pw, "navigate_detail",
                          return_value=detail_visit.OpenedDetail(page)) as navigate, \
             patch.object(detail_visit, "ready_detail_page", return_value=False), \
             patch.object(detail_visit, "intervention_kind", return_value=None), \
             patch.object(detail_visit, "is_deny_url", return_value=False), \
             patch.object(detail, "extract_main_image", return_value=None):
            pipeline._capture_one_pw(self.db, cfg, self.human, self.round_id, offer,
                                     MagicMock(), emit=self.emit, pacing=ledger)
        return navigate

    def test_a_detail_that_was_already_collected_today_does_not_charge(self):
        """「今天采过」这条省事的路不打开页面，也就不占配额（改前它先记了 1）。"""
        cfg = crawler_cfg(pause_every_detail_visits=1, pause_sec=(30.0, 30.0))
        ledger = pacing.Pacing(cfg, self.human, emit=self.emit)
        submit_offer(self.db, "111", cst_date(), 3, name="商品111")

        skipped = self.capture(cfg, self.offer("111"), ledger)
        fresh = self.capture(cfg, self.offer("222"), ledger)

        self.assertEqual(skipped.call_count, 0, "今天采过的商品不打开详情页")
        self.assertEqual(fresh.call_count, 1)
        self.assertEqual(self.human.sleep.call_args_list, [],
                         "没打开的那一条不该占配额——占了它，这一次真访问就得先停 30 秒")

    def test_the_pause_lands_between_two_details(self):
        cfg = crawler_cfg(pause_every_detail_visits=1, pause_sec=(30.0, 30.0))
        ledger = pacing.Pacing(cfg, self.human, emit=self.emit)

        first = self.capture(cfg, self.offer("111"), ledger)
        second = self.capture(cfg, self.offer("222"), ledger)

        self.assertEqual((first.call_count, second.call_count), (1, 1),
                         "停顿不该吞掉任何一条")
        self.assertEqual([call.args[0] for call in self.human.sleep.call_args_list], [30.0])
        order = [event for event, _ in self.events
                 if event in ("detail_parse", "pacing_pause")]
        self.assertEqual(order, ["detail_parse", "pacing_pause", "detail_parse"],
                         "停在第 1 次与第 2 次详情访问之间")


class PacingDetailVisitSeamTests(unittest.TestCase):
    """共享 seam（`begin_detail_visit`）上的记账：页面到手才记。

    配额 1 之下，记账了就一定能在下一次「要开详情」的前置检查上停 30 秒——这几条用例
    都拿这个当探针，不去读账本内部。
    """

    def setUp(self):
        self.human = MagicMock()
        self.cfg = crawler_cfg(pause_every_detail_visits=1, pause_sec=(30.0, 30.0))
        self.ledger = pacing.Pacing(self.cfg, self.human, emit=None)

    def open_page(self):
        page = MagicMock()
        page.url = "https://detail.1688.com/offer/11.html"
        return detail_visit.OpenedDetail(page)

    def charged(self) -> bool:
        """探针：配额 1，再要开一次详情就该停 30 秒。"""
        self.ledger.before_detail_visit(shop_key="A01")
        return bool(self.human.sleep.call_args_list)

    def test_a_failed_acquire_does_not_charge_the_quota(self):
        """取得就失败（弹窗没打开 / 导航报错）：页面没到手，这次不算详情访问。"""
        def boom():
            raise OSError("点不开")

        visit = detail_visit.begin_detail_visit(boom, self.cfg, pacing=self.ledger)

        self.assertIsInstance(visit, detail_visit.ReadFailedVisit)
        self.assertFalse(self.charged())

    def test_the_page_being_handed_over_charges_before_the_guard_runs(self):
        """guard 判出 deny 也算一次访问——它一样是打到详情接口的请求。"""
        with patch.object(detail_visit, "ready_detail_page", return_value=True):
            visit = detail_visit.begin_detail_visit(
                self.open_page, self.cfg, pacing=self.ledger)

        self.assertIsInstance(visit, detail_visit.DeniedVisit)
        self.assertTrue(self.charged())

    def test_the_deny_page_that_trips_the_ladder_still_counts(self):
        """阶梯越界时 guard 直接抛：那一次请求已经发出，也要记。"""
        from bestseller_monitor.guard import ShopDenyExceeded

        with patch.object(detail_visit, "ready_detail_page",
                          side_effect=ShopDenyExceeded("店铺 deny 越界")):
            with self.assertRaises(ShopDenyExceeded):
                detail_visit.begin_detail_visit(self.open_page, self.cfg, pacing=self.ledger)

        self.assertTrue(self.charged())


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
