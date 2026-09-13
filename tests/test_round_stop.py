import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw, click_listing, detail, pipeline, rounds, stop_request
from bestseller_monitor.click_listing import RoundDenyExceeded, ShopDenyExceeded
from bestseller_monitor.config import Shop
from bestseller_monitor.db import (CST, Database, DayBoundaryReached, DetailBudgetExhausted,
                                   connect, cst_date)
from bestseller_monitor.guard import InterventionTimeout, RoundPauseRequired
from bestseller_monitor.rounds import RoundRequest, ShopScope, TerminalReason
from bestseller_monitor.stop_request import StopRequested


def _cst(y: int, m: int, d: int, hh: int, mm: int) -> str:
    return datetime(y, m, d, hh, mm, tzinfo=CST).isoformat()


def _yesterday() -> str:
    return (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")


class RoundStopRuleTests(unittest.TestCase):
    """工单 03：停止判定比较轮次日期与当前北京日期，不再只看「现在几点」。"""

    def test_every_stop_exception_has_one_place_that_says_how_to_end_it(self):
        """停止分类只有一处定义：查得到、文案有、子类沿继承链落到父类。"""
        examples = [
            StopRequested("停"), RoundPauseRequired("人工"), InterventionTimeout("超时"),
            DayBoundaryReached(), DetailBudgetExhausted(), ShopDenyExceeded("店"),
            RoundDenyExceeded("整轮"),
        ]

        for exc in examples:
            with self.subTest(exc=type(exc).__name__):
                outcome = pipeline.stop_outcome(exc)
                self.assertTrue(outcome.shop_note, "每个停止异常都要能写本店备注")
                if outcome.scope is pipeline.StopScope.ROUND:
                    self.assertTrue(outcome.notice, "整轮级的停止要有一句给人看的提示")

    def test_inheritance_no_longer_decides_how_a_stop_ends(self):
        """RoundDenyExceeded 继承 RoundPauseRequired：两者收尾不同，谁也不许遮谁。"""
        denied = pipeline.stop_outcome(RoundDenyExceeded("整轮 deny"))
        paused = pipeline.stop_outcome(InterventionTimeout("人工介入超时"))

        self.assertIs(denied.round_end, TerminalReason.DENY_EXCEEDED)
        self.assertIsNone(paused.round_end, "人工介入超时保持可续跑")
        self.assertIn("人工介入未完成", paused.shop_note)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.db = Database(self.conn)
        self.addCleanup(stop_request.uninstall)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    @staticmethod
    def _cfg(**overrides):
        values = {
            "timeout_ms": 1,
            "human_pause_minutes": 1,
            "intervention_confirmation_sec": 0,
            "fail_rate_limit": 0.1,
            "max_attempts_per_page": 2,
            "max_detail_opportunities_per_round": 1000,
            "shuffle_within_shop": False,
            "long_pause_interval": (1, 1),
            "detail_delay_sec": (0.0, 0.0),
            "long_pause_sec": (0.0, 0.0),
            "batch_size": 1,
            "batch_rest_sec": (0.0, 0.0),
            "list_delay_sec": (0.0, 0.0),
            "action_delay_sec": (0.0, 0.0),
            "read_delay_sec": (0.0, 0.0),
            "retry_base_sec": 0.0,
            "retry_jitter_sec": 0.0,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _open(self, run_date: str, *keys: str):
        return rounds.open(self.db, RoundRequest(
            run_date,
            tuple(ShopScope(k, f"https://{k}.example/", f"店铺{k}") for k in keys),
        )).round

    @staticmethod
    def _offer():
        return {
            "shop_key": "A01",
            "shop_url": "https://A01.example/",
            "shop_name": "店铺A01",
            "offer_id": "111",
            "product_url": "https://detail.1688.com/offer/111.html",
            "list_title": "榜单标题",
        }

    def _target(self):
        """补采路径的 target：编号在取观测之前就已知。"""
        offer = self._offer()
        return detail.DetailTarget(
            shop_key=offer["shop_key"], shop_url=offer["shop_url"],
            shop_name=offer["shop_name"], product_url=offer["product_url"],
            slot_key=offer["offer_id"], list_title=offer["list_title"],
            offer_id=offer["offer_id"])

    def _capture(self, round_id, observe, **kwargs):
        """照补采路径的样子取一次观测：规则在 detail.capture_observation 里。"""
        return detail.capture_observation(
            self.db, self._cfg(), MagicMock(), round_id, self._target(), observe, **kwargs)

    def _observation(self):
        return detail.Observation(payload=self._payload())

    @staticmethod
    def _payload():
        return {
            "html": "<html></html>",
            "product_name": "详情标题",
            "rows": [{"sku_id": "red", "sku_name": "红色", "sku_price": 10, "sku_stock": 3}],
        }

    def test_boundaries_use_round_date_and_moment(self):
        run = self._open("2026-09-12", "A01")
        stops = lambda moment: rounds.load(self.db, run.id).stops_work(moment)

        self.assertFalse(stops(_cst(2026, 9, 12, 23, 54)))   # 同日截止线前一刻
        self.assertTrue(stops(_cst(2026, 9, 12, 23, 55)))    # 达到截止线
        self.assertTrue(stops(_cst(2026, 9, 13, 0, 0)))      # 次日零点
        self.assertTrue(stops(_cst(2026, 9, 13, 12, 0)))     # 次日中午仍然停

    def test_round_started_after_midnight_keeps_working(self):
        # 零点之后启动的是一轮新轮次，它的日期就是当天，照常采集。
        run = self._open("2026-09-13", "A01")

        self.assertFalse(rounds.load(self.db, run.id).stops_work(_cst(2026, 9, 13, 0, 5)))
        self.assertFalse(rounds.load(self.db, run.id).stops_work(_cst(2026, 9, 13, 15, 0)))

    def test_before_detail_stops_without_starting_work(self):
        stale = self._open(_yesterday(), "A01")
        visits: list[int] = []

        with self.assertRaises(DayBoundaryReached):
            self._capture(stale.id, lambda: visits.append(1) or self._observation())

        self.assertEqual(visits, [], "该停就不该打开详情")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 0)

    def test_after_commit_stops_but_keeps_the_data(self):
        run = self._open("2026-09-12", "A01")
        # utcnow 的三次调用：进详情前（23:54，不触发）、写入时间戳、提交之后（23:56，触发）
        moments = [
            _cst(2026, 9, 12, 23, 54),
            _cst(2026, 9, 12, 23, 54),
            _cst(2026, 9, 12, 23, 56),
        ]
        with patch.object(detail, "utcnow", side_effect=moments):
            with self.assertRaises(DayBoundaryReached):
                self._capture(run.id, lambda: self._observation(), attempts=1)

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM snapshots WHERE page_status='成功'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 1)

    def test_listing_stops_before_touching_a_shop(self):
        stale = self._open(_yesterday(), "A01")
        shop = Shop("A01", "店铺A01", "https://A01.example/")

        with patch.object(click_listing, "crawl_store_by_click") as crawl:
            with self.assertRaises(DayBoundaryReached):
                pipeline._run_listing_pw(self.db, self._cfg(), stale.id, [shop], MagicMock())

        crawl.assert_not_called()
        row = self.conn.execute(
            "SELECT list_status FROM shop_rounds WHERE round_id=?", (stale.id,)
        ).fetchone()
        self.assertEqual(row["list_status"], "待处理")

    def _request_pause(self, round_id: int) -> None:
        """模拟界面那一跳：按身份行里的目标进程写一条暂停请求（ADR-0009）。"""
        identity = self.db.crawler_process()
        started_at = (identity["started_at"] if identity is not None
                      else self.db.record_crawler_process(
                          pid=os.getpid(), round_id=round_id, note="test"))
        self.db.request_stop(round_id=round_id, kind=stop_request.PAUSE,
                             target_pid=os.getpid(), target_started_at=started_at)

    def test_pause_marks_the_shop_it_interrupted(self):
        """停在一家店的中途：这家店记为未完成，原因写「用户暂停」而不是人工介入。"""
        run = self._open(cst_date(), "A01")
        shop = Shop("A01", "店铺A01", "https://A01.example/")

        def crawl(*_args, **_kwargs):
            self._request_pause(run.id)                      # 界面那一跳
            rounds.ensure_workable(self.db, run.id, cst_date())   # 采集进程的检查点

        with patch.object(click_listing, "crawl_store_by_click", side_effect=crawl):
            with self.assertRaises(stop_request.StopRequested):
                pipeline._run_listing_pw(self.db, self._cfg(), run.id, [shop], MagicMock())

        row = self.conn.execute(
            "SELECT list_status, list_note FROM shop_rounds WHERE round_id=?", (run.id,)
        ).fetchone()
        self.assertEqual(row["list_status"], "失败")
        self.assertEqual(row["list_note"], pipeline.PAUSE_BY_USER_NOTE)

    def test_pause_between_details_does_not_record_a_failure(self):
        """暂停不是详情失败：异常要原样上抛，不许记成一次失败尝试。"""
        run = self._open(cst_date(), "A01")
        offers = [self._offer()]

        def capture(_offer):
            self._request_pause(run.id)
            self._capture(run.id, lambda: self._observation(), attempts=1)

        with self.assertRaises(stop_request.StopRequested):
            pipeline._capture_pending_offers(self.db, self._cfg(), MagicMock(), run.id,
                                             offers, capture)

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0,
            "暂停不该留下失败记录")

    def test_pause_inside_a_detail_fetch_is_not_recorded_as_a_failure(self):
        """暂停落在长睡眠的切片里（fetch 途中）：同样要原样上抛，不许当成访问异常。"""
        run = self._open(cst_date(), "A01")

        def observe():
            raise stop_request.StopRequested("停")

        with self.assertRaises(stop_request.StopRequested):
            self._capture(run.id, observe, attempts=1)

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0,
            "暂停不该留下失败记录")

    def test_a_stop_request_from_the_detail_adapter_passes_through(self):
        """补采 adapter 里的停止判定要原样穿过（ADR-0009），不许被记成访问异常。"""
        run = self._open(cst_date(), "A01")

        with patch.object(browser_pw, "open_detail",
                          side_effect=stop_request.StopRequested("停")):
            with self.assertRaises(stop_request.StopRequested):
                pipeline._capture_one_pw(self.db, self._cfg(), MagicMock(), run.id,
                                         self._offer(), MagicMock())

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0,
            "停止判定不该留下失败记录")
