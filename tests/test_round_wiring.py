import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw, guard, pipeline, rounds, weekly_plan
from bestseller_monitor.config import Shop
from bestseller_monitor.db import CST, Database, connect, cst_date
from bestseller_monitor.rounds import RoundRequest, ScopeMismatch, ShopScope
from helpers import crawler_cfg, isolated_locks, new_round, store_weekly_plan

SHOP_CSV_HEADER = "shop_key,shop_name,shop_url,pages,active,offer_list_url"


class RoundScopeWiringTests(unittest.TestCase):
    """工单 02：店铺范围成为轮次自己的事实，续跑不再读配置增删店铺。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.shop_csv = Path(self.tmp.name) / "shops.csv"
        self._write_shops("A01", "A02")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_shops(self, *keys):
        rows = [SHOP_CSV_HEADER]
        for key in keys:
            rows.append(f"{key},店铺{key},https://{key}.example/,3,1,")
        self.shop_csv.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def _write_shops_inactive(self, key):
        """该店在本机清单里标了停用（active=0）：行与地址都还在。"""
        self.shop_csv.write_text(
            f"{SHOP_CSV_HEADER}\n{key},店铺{key},https://{key}.example/,3,0,\n",
            encoding="utf-8")

    def _store_plan(self, *assignments):
        """把本周计划落进本机计划表；assignments 是 (shop_key, machine_id)。"""
        conn = connect(self.db_path)
        try:
            store_weekly_plan(Database(conn), weekly_plan.week_label(),
                              *[(key, machine, 3) for key, machine in assignments])
        finally:
            conn.close()

    def _cfg(self, **overrides):
        """这一层要的那几件（采集配置的默认值见 helpers.crawler_cfg）。"""
        return crawler_cfg(db_file=self.db_path, shop_csv=self.shop_csv,
                           driver="pw_cdp", ensure_dirs=MagicMock(), **overrides)

    @staticmethod
    def _shop(key):
        return Shop(key, f"店铺{key}", f"https://{key}.example/")

    @staticmethod
    def _yesterday() -> str:
        return (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")

    def _run(self, shops, cfg=None):
        cfg = cfg or self._cfg()
        with isolated_locks(), patch.object(pipeline, "_run_pwcdp_round") as driver:
            pipeline.run_round(cfg, shops)
        return driver

    def _round_rows(self):
        conn = connect(self.db_path)
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM rounds ORDER BY id")]
        finally:
            conn.close()

    def _scope(self, round_id):
        conn = connect(self.db_path)
        try:
            return sorted(
                row["shop_key"] for row in conn.execute(
                    "SELECT shop_key FROM shop_rounds WHERE round_id=?", (round_id,)
                )
            )
        finally:
            conn.close()

    def _open_round(self, run_date, *keys) -> int:
        conn = connect(self.db_path)
        try:
            opened = rounds.open(Database(conn), RoundRequest(
                run_date,
                tuple(ShopScope(k, f"https://{k}.example/", f"店铺{k}") for k in keys),
            ))
            return opened.round.id
        finally:
            conn.close()

    def test_run_round_creates_round_with_requested_scope(self):
        self._run([self._shop("A01"), self._shop("A02")])

        row = self._round_rows()[-1]
        self.assertEqual(row["run_date"], cst_date())
        self.assertIsNone(row["terminal_reason"])
        self.assertEqual(self._scope(row["id"]), ["A01", "A02"])

    def test_run_round_resumes_same_scope_on_the_same_day(self):
        self._run([self._shop("A01")])
        self._run([self._shop("A01")])

        rows = self._round_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self._scope(rows[0]["id"]), ["A01"])

    def test_run_round_rejects_scope_change_on_the_same_day(self):
        self._run([self._shop("A01")])

        with self.assertRaises(ScopeMismatch):
            self._run([self._shop("A01"), self._shop("A02")])

        rows = self._round_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["terminal_reason"])
        self.assertEqual(self._scope(rows[0]["id"]), ["A01"])

    def test_run_round_supersedes_previous_day(self):
        stale = self._open_round(self._yesterday(), "A01")

        self._run([self._shop("A02")])

        rows = {row["id"]: row for row in self._round_rows()}
        self.assertEqual(rows[stale]["terminal_reason"], "DAY_BOUNDARY")
        self.assertEqual(len(rows), 2)
        fresh_id = [rid for rid in rows if rid != stale][0]
        self.assertEqual(rows[fresh_id]["run_date"], cst_date())
        self.assertEqual(self._scope(fresh_id), ["A02"])

    def test_run_round_processes_the_round_scope_not_the_config(self):
        self._open_round(cst_date(), "A01", "A02")
        self._write_shops("A01")  # 配置里删掉 A02

        driver = self._run([self._shop("A01"), self._shop("A02")])

        processed = driver.call_args.args[3]
        self.assertEqual([shop.key for shop in processed], ["A01", "A02"])
        self.assertEqual(processed[1].url, "https://A02.example/")
        self.assertIsNone(processed[1].pages)  # 配置里已没有它，回落全局默认

    def test_bare_run_keeps_the_round_scope_when_config_shrinks(self):
        self._open_round(cst_date(), "A01", "A02")
        self._write_shops("A01")
        self._store_plan(("A01", "m-test"))    # 本周计划是另一份事实：续跑仍以轮次为准

        shops = pipeline.requested_round_shops(self._cfg(), None)

        self.assertEqual([shop.key for shop in shops], ["A01", "A02"])
        self.assertEqual(shops[1].url, "https://A02.example/")

    def test_bare_run_without_active_round_uses_the_stored_plan(self):
        """票据 06：范围从落库计划取——归本机的店才采，不再从 active 全取。"""
        self._store_plan(("A01", "m-test"), ("A02", "m3"))

        shops = pipeline.requested_round_shops(self._cfg(), None)

        self.assertEqual([shop.key for shop in shops], ["A01"],
                         "A02 本周归 m3：不进本机的轮次范围（越权要显式勾选）")

    def test_bare_run_without_a_stored_plan_has_nothing_to_crawl(self):
        shops = pipeline.requested_round_shops(self._cfg(), None)

        self.assertEqual(shops, [], "没有落库计划就没有可采的店（开轮前的准备先于这一步）")

    def test_a_planned_shop_deactivated_midweek_still_counts(self):
        """周中把店标停用不改变本周：计划已发布，本周仍按计划采它。"""
        self._write_shops_inactive("A01")
        self._store_plan(("A01", "m-test"))

        shops = pipeline.requested_round_shops(self._cfg(), None)

        self.assertEqual([shop.key for shop in shops], ["A01"])
        self.assertEqual(shops[0].url, "https://A01.example/page/offerlist.htm")

    def test_limit_keys_is_an_explicit_scope_request(self):
        shops = pipeline.requested_round_shops(self._cfg(), {"A02"})

        self.assertEqual([shop.key for shop in shops], ["A02"])

    def test_limit_keys_can_name_a_planned_shop_deactivated_locally(self):
        """界面勾选送的是这里的 limit_keys：与界面陈列的全量取同一份（含停用的计划店）。"""
        self._write_shops_inactive("A01")
        self._store_plan(("A01", "m-test"))

        shops = pipeline.requested_round_shops(self._cfg(), {"A01"})

        self.assertEqual([shop.key for shop in shops], ["A01"])


class PwCdpRoundAssemblyTests(unittest.TestCase):
    """IS-23 / ADR-0010：删掉旧的直连路径之后，这是整轮采集唯一的装配入口。

    会话的打开与收尾、事件埋点与 deny 追踪都从这一处挂到轮次上，
    所以这几件事在这里锁住，而不是散在已经不存在的旧路径里。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.db = Database(connect(self.db_path))
        self.round_id = new_round(self.db, "A01")
        self.shops = [Shop("A01", "店铺A01", "https://A01.example/")]

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def _cfg(self):
        """这一层要的那几件（采集配置的默认值见 helpers.crawler_cfg）。"""
        return crawler_cfg(db_file=self.db_path, driver="pw_cdp", max_pages_per_shop=3,
                           human_pause_minutes=1)

    def _run(self, listing):
        session = ("pw", "br", "page", "ctx")
        with patch.object(browser_pw, "open_session", return_value=session) as opened, \
                patch.object(browser_pw, "close_session") as closed, \
                patch.object(pipeline, "_run_listing_pw", side_effect=listing):
            pipeline._run_pwcdp_round(self.db, self._cfg(), self.round_id, self.shops)
        return opened, closed

    def test_session_is_closed_even_when_the_listing_phase_fails(self):
        session = ("pw", "br", "page", "ctx")
        with patch.object(browser_pw, "open_session", return_value=session), \
                patch.object(browser_pw, "close_session") as closed, \
                patch.object(pipeline, "_run_listing_pw", side_effect=RuntimeError("榜单炸了")):
            with self.assertRaises(RuntimeError):
                pipeline._run_pwcdp_round(self.db, self._cfg(), self.round_id, self.shops)

        self.assertEqual(closed.call_args.args, session[:2], "异常也不能漏掉浏览器收尾")

    def test_listing_phase_records_events_and_deny_tracking_for_this_round(self):
        captured = {}

        def fake_listing(db, cfg, round_id, shops, page, emit=None, deny_tracker=None):
            captured["deny_tracker"] = deny_tracker
            emit("click_deny", shop_key="A01", phase="listing")

        self._run(fake_listing)

        events = [tuple(row) for row in self.db.conn.execute(
            "SELECT round_id, event, shop_key FROM event_log"
        )]
        self.assertEqual(events, [(self.round_id, "click_deny", "A01")],
                         "埋点要落在本轮的事件表里，界面过程页读的就是它")
        self.assertIsInstance(captured["deny_tracker"], guard.DenyTracker)
        params = self.db.conn.execute("SELECT COUNT(*) c FROM run_params").fetchone()["c"]
        self.assertEqual(params, 1, "本轮生效的参数要留档")
