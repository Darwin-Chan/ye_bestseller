"""界面取数 module：三个页面的取数走同一道 interface（候选 04）。

断言的是页面真正拿到的东西——`views.start_view` / `run_view` / `result_view` 的返回 dict，
不是它们内部的查询。穿透同一道 interface 的调用方是 `gui.Api`。
"""
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor import rounds, views
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, connect
from bestseller_monitor.rounds import RoundRequest, ShopScope, TerminalReason
from helpers import crawler_cfg, new_round

# 固定时刻：北京时间 2026-09-13 12:00。三个页面的「今天」都由它决定。
NOW = "2026-09-13T04:00:00+00:00"
TODAY = "2026-09-13"


class ViewsTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = connect(Path(tmp.name) / "views.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.cfg = crawler_cfg(max_pages_per_shop=3)
        self.shops = [Shop("A01", "店铺A", "https://A01.example/")]
        self.state = views.UiState()

    def submit(self, round_id, offer_id, *skus, shop_key="A01"):
        """按库存快照提交的口径写一条成功观测（每项是 (sku_name, stock)）。"""
        self.db.submit_inventory_snapshot(
            round_id=round_id,
            shop_key=shop_key,
            shop_url=f"https://{shop_key}.example/",
            shop_name=f"店铺{shop_key}",
            offer_id=offer_id,
            product_url=f"https://detail.1688.com/offer/{offer_id}.html",
            list_title=f"商品{offer_id}",
            detail_title=f"商品{offer_id}",
            main_image_url="",
            sku_rows=[{"sku_name": name, "sku_stock": stock} for name, stock in skus],
            collected_at=NOW,
            attempt=1,
        )


class StartViewTests(ViewsTestCase):
    def test_start_page_counts_today_inventory_and_offers_shops_to_crawl(self):
        round_id = new_round(self.db, "A01", run_date=TODAY)
        self.submit(round_id, "11", ("红", 5), ("蓝", 7))
        self.submit(round_id, "22", ("默认(单规格)", 3))

        view = views.start_view(self.conn, cfg=self.cfg, shops=self.shops,
                                state=self.state, crawler=None, now=NOW)

        self.assertEqual(view["ov"], {"products": 2, "skus": 3})
        self.assertEqual(view["total_shops"], 1)
        shop = view["shops"][0]
        self.assertEqual((shop["key"], shop["name"]), ("A01", "店铺A"))
        self.assertEqual((shop["products"], shop["skus"]), (2, 3))
        self.assertEqual(shop["pages"], 3, "翻页上限沿用配置里的全局默认")
        self.assertTrue(shop["default_checked"], "今天没采满的店铺默认勾上")
        self.assertTrue(view["summary"]["started"])
        self.assertEqual(view["summary"]["rounds"], 1)


class RunViewTests(ViewsTestCase):
    def setUp(self):
        super().setUp()
        self.shops = [Shop("A01", "店铺A", "https://A01.example/"),
                      Shop("A02", "店铺B", "https://A02.example/")]

    def test_progress_page_reports_done_todo_and_current_shop(self):
        round_id = new_round(self.db, ("A01", "https://A01.example/", "店铺A"),
                             ("A02", "https://A02.example/", "店铺B"), run_date=TODAY)
        self.db.save_shop_offers(
            round_id, "A01", "https://A01.example/", "店铺A",
            [(1, "11", "https://detail.1688.com/offer/11.html", "商品11", "")], 1)
        self.submit(round_id, "11", ("红", 5), ("蓝", 7))
        self.db.append_event(round_id, "click_deny", shop_key="A01")
        self.db.append_event(round_id, "list_page", shop_key="A02")

        view = views.run_view(self.conn, state=self.state, now=NOW)

        self.assertEqual((view["done_count"], view["total_count"], view["progress"]), (1, 2, 0.5))
        self.assertEqual(view["todo"], [{"key": "A02", "name": "店铺B"}])
        self.assertEqual(view["current_shop"], "A02", "未完成店铺里最近有事件的那家")
        self.assertEqual(view["deny"], 1)
        done = view["done"][0]
        self.assertEqual((done["key"], done["name"], done["products"], done["skus"], done["deny"]),
                         ("A01", "店铺A", 1, 2, 1))

    def test_progress_page_carries_the_ui_session_facts(self):
        state = views.UiState(crawler_running=True, manually_paused=True,
                              stopping="stopping", stop_grace_sec=8.0, elapsed_sec=123.0)

        view = views.run_view(self.conn, state=state, now=NOW)

        self.assertFalse(view["has_round"], "今天还没有轮次")
        self.assertTrue(view["running"])
        self.assertTrue(view["manually_paused"])
        self.assertEqual(view["stopping"], "stopping")
        self.assertEqual(view["stop_grace_sec"], 8.0)


class ResultViewTests(ViewsTestCase):
    def test_result_page_reports_the_terminal_state_and_unfinished_shops(self):
        opened = rounds.open(self.db, RoundRequest(TODAY, (
            ShopScope("A01", "https://A01.example/", "店铺A"),
            ShopScope("A02", "https://A02.example/", "店铺B"),
        )), now=NOW)
        round_id = opened.round.id
        self.db.save_shop_offers(
            round_id, "A01", "https://A01.example/", "店铺A",
            [(1, "11", "https://detail.1688.com/offer/11.html", "商品11", "")], 1)
        self.submit(round_id, "11", ("红", 5), ("蓝", 7))
        rounds.finish(self.db, rounds.load(self.db, round_id), TerminalReason.COMPLETED,
                      now="2026-09-13T04:10:00+00:00")

        view = views.result_view(self.conn, state=views.UiState(round_id=round_id), now=NOW)

        self.assertEqual(view["reason"], "COMPLETED")
        self.assertEqual((view["tag"], view["note"]), ("正常完成", "本轮正常完成。"))
        self.assertEqual(view["duration_text"], "10 分")
        self.assertEqual((view["done_count"], view["total_count"]), (1, 2))
        self.assertEqual((view["products_total"], view["skus_total"]), (1, 2))
        self.assertEqual(view["todo"], [{"key": "A02", "name": "店铺B"}])
        self.assertEqual(view["done"][0]["key"], "A01")

    def test_result_page_uses_current_elapsed_time_while_the_round_is_still_open(self):
        round_id = new_round(self.db, run_date=TODAY)

        view = views.result_view(
            self.conn, state=views.UiState(round_id=round_id, elapsed_sec=123.0), now=NOW)

        self.assertEqual((view["tag"], view["duration_text"]), ("进行中", "2 分"))

    def test_every_terminal_reason_has_result_text(self):
        """终态文案表是 module 内部的一处接缝：新加终态时这里先红（LEGACY_UNKNOWN 不在轮次模块的写入面里）。"""
        for reason in TerminalReason:
            with self.subTest(reason=reason):
                tag, note = views._terminal_text(reason)
                self.assertTrue(tag)
                self.assertTrue(note)

        self.assertEqual(views._terminal_text(None),
                         ("进行中", "本轮仍在进行；未抓取店铺见下方。"))


class SharedMetricsTests(ViewsTestCase):
    def test_progress_and_result_pages_report_the_same_shop_metrics(self):
        """同一轮里两个页面对各店给出同一份指标——这正是从前两份实现要保证的事。"""
        round_id = new_round(self.db, ("A01", "https://A01.example/", "店铺A"),
                             ("A02", "https://A02.example/", "店铺B"), run_date=TODAY)
        self.db.save_shop_offers(
            round_id, "A01", "https://A01.example/", "店铺A",
            [(1, "11", "https://detail.1688.com/offer/11.html", "商品11", "")], 1)
        self.submit(round_id, "11", ("红", 5), ("蓝", 7))
        self.db.append_event(round_id, "click_deny", shop_key="A01")

        run = views.run_view(self.conn, state=self.state, now=NOW)
        result = views.result_view(
            self.conn, state=views.UiState(round_id=round_id), now=NOW)

        self.assertEqual(run["done"], result["done"])
        self.assertEqual(run["todo"], result["todo"])
        self.assertEqual((run["done_count"], run["total_count"], run["deny"]),
                         (result["done_count"], result["total_count"], result["deny"]))
        self.assertEqual((result["products_total"], result["skus_total"]), (1, 2))


class RefusedStartTests(ViewsTestCase):
    def test_refused_child_is_reported_instead_of_an_empty_result_page(self):
        new_round(self.db, run_date=TODAY)

        view = views.run_view(self.conn, state=views.UiState(
            start_error="已有采集进程在运行：本次启动被拒绝了，等它跑完再试。"), now=NOW)

        self.assertEqual(view["start_error"],
                         "已有采集进程在运行：本次启动被拒绝了，等它跑完再试。")
        self.assertFalse(view["has_round"], "被拒绝不是一轮跑完")
        self.assertFalse(view["running"])
        self.assertFalse(view["manually_paused"])
