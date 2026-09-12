import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import pipeline, rounds
from bestseller_monitor.config import Shop
from bestseller_monitor.db import CST, Database, connect, cst_date
from bestseller_monitor.rounds import RoundRequest, ScopeMismatch, ShopScope
from helpers import isolated_locks

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

    def _cfg(self, **overrides):
        values = {
            "db_file": self.db_path,
            "shop_csv": self.shop_csv,
            "driver": "pw_cdp",
            "ensure_dirs": MagicMock(),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

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

        shops = pipeline.requested_round_shops(self._cfg(), None)

        self.assertEqual([shop.key for shop in shops], ["A01", "A02"])
        self.assertEqual(shops[1].url, "https://A02.example/")

    def test_bare_run_without_active_round_uses_config_shops(self):
        shops = pipeline.requested_round_shops(self._cfg(), None)

        self.assertEqual([shop.key for shop in shops], ["A01", "A02"])

    def test_limit_keys_is_an_explicit_scope_request(self):
        shops = pipeline.requested_round_shops(self._cfg(), {"A02"})

        self.assertEqual([shop.key for shop in shops], ["A02"])
