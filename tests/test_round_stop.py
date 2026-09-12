import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw, pipeline, rounds, stop_request
from bestseller_monitor.config import Shop
from bestseller_monitor.db import CST, Database, DayBoundaryReached, connect, cst_date
from bestseller_monitor.rounds import RoundRequest, ShopScope


def _cst(y: int, m: int, d: int, hh: int, mm: int) -> str:
    return datetime(y, m, d, hh, mm, tzinfo=CST).isoformat()


def _yesterday() -> str:
    return (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")


class RoundStopRuleTests(unittest.TestCase):
    """工单 03：停止判定比较轮次日期与当前北京日期，不再只看「现在几点」。"""

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
        fetch = MagicMock(return_value=self._payload())

        with self.assertRaises(DayBoundaryReached):
            pipeline._capture_offer_detail(
                self.db, self._cfg(), MagicMock(), stale.id, self._offer(), fetch,
            )

        fetch.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 0)

    def test_after_commit_stops_but_keeps_the_data(self):
        run = self._open("2026-09-12", "A01")
        fetch = MagicMock(return_value=self._payload())
        # utcnow 的三次调用：进详情前（23:54，不触发）、写入时间戳、提交之后（23:56，触发）
        moments = [
            _cst(2026, 9, 12, 23, 54),
            _cst(2026, 9, 12, 23, 54),
            _cst(2026, 9, 12, 23, 56),
        ]
        with patch.object(pipeline, "utcnow", side_effect=moments), \
             patch.object(pipeline, "extract_main_image", return_value=None):
            with self.assertRaises(DayBoundaryReached):
                pipeline._capture_offer_detail(
                    self.db, self._cfg(max_attempts_per_page=1), MagicMock(),
                    run.id, self._offer(), fetch,
                )

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM snapshots WHERE page_status='成功'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 1)

    def test_listing_stops_before_touching_a_shop(self):
        stale = self._open(_yesterday(), "A01")
        shop = Shop("A01", "店铺A01", "https://A01.example/")

        with patch.object(browser_pw, "crawl_store_by_click") as crawl:
            with self.assertRaises(DayBoundaryReached):
                pipeline._run_listing_pw(self.db, self._cfg(), stale.id, [shop], MagicMock())

        crawl.assert_not_called()
        row = self.conn.execute(
            "SELECT list_status FROM shop_rounds WHERE round_id=?", (stale.id,)
        ).fetchone()
        self.assertEqual(row["list_status"], "待处理")

    def test_click_path_stops_before_opening_a_card(self):
        stale = self._open(_yesterday(), "A01")
        shop = Shop("A01", "店铺A01", "https://A01.example/")

        with patch.object(browser_pw, "_click_one_product") as click:
            with self.assertRaises(DayBoundaryReached):
                browser_pw._capture_card(
                    MagicMock(), MagicMock(), "商品", self._cfg(), [False], MagicMock(),
                    MagicMock(), self.db, stale.id, shop, [], set(),
                )

        click.assert_not_called()

    def _request_pause(self, round_id: int) -> None:
        """模拟界面那一跳：按身份行里的目标进程写一条暂停请求（ADR-0009）。"""
        identity = self.db.crawler_process()
        started_at = (identity["started_at"] if identity is not None
                      else self.db.record_crawler_process(
                          pid=os.getpid(), round_id=round_id, note="test"))
        self.db.request_stop(round_id=round_id, kind=stop_request.PAUSE,
                             target_pid=os.getpid(), target_started_at=started_at)

    def test_click_path_stops_when_the_interface_asks_for_a_pause(self):
        """暂停与跨天走同一条检查点，但抛的是停止请求：轮次保持进行中、可续跑。"""
        run = self._open(cst_date(), "A01")
        shop = Shop("A01", "店铺A01", "https://A01.example/")
        self._request_pause(run.id)

        with patch.object(browser_pw, "_click_one_product") as click:
            with self.assertRaises(stop_request.StopRequested):
                browser_pw._capture_card(
                    MagicMock(), MagicMock(), "商品", self._cfg(), [False], MagicMock(),
                    MagicMock(), self.db, run.id, shop, [], set(),
                )

        click.assert_not_called()
        self.assertIsNotNone(self.db.stop_request()["ack_at"], "认领时回执")

    def test_pause_marks_the_shop_it_interrupted(self):
        """停在一家店的中途：这家店记为未完成，原因写「用户暂停」而不是人工介入。"""
        run = self._open(cst_date(), "A01")
        shop = Shop("A01", "店铺A01", "https://A01.example/")

        def crawl(*_args, **_kwargs):
            self._request_pause(run.id)                      # 界面那一跳
            rounds.ensure_workable(self.db, run.id, cst_date())   # 采集进程的检查点

        with patch.object(browser_pw, "crawl_store_by_click", side_effect=crawl):
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
            pipeline._capture_offer_detail(self.db, self._cfg(), MagicMock(), run.id,
                                           _offer, MagicMock())

        with self.assertRaises(stop_request.StopRequested):
            pipeline._capture_pending_offers(self.db, self._cfg(), MagicMock(), run.id,
                                             offers, capture)

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0,
            "暂停不该留下失败记录")

    def test_pause_inside_a_detail_fetch_is_not_recorded_as_a_failure(self):
        """暂停落在长睡眠的切片里（fetch 途中）：同样要原样上抛，不许当成访问异常。"""
        run = self._open(cst_date(), "A01")

        with self.assertRaises(stop_request.StopRequested):
            pipeline._capture_offer_detail(
                self.db, self._cfg(), MagicMock(), run.id, self._offer(),
                MagicMock(side_effect=stop_request.StopRequested("停")),
            )

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0,
            "暂停不该留下失败记录")
