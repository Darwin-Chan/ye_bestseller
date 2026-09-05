import tempfile
import unittest
from pathlib import Path

from bestseller_monitor.db import Database, connect, cst_date


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.db = Database(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _add_shop(self, round_id):
        self.db.add_shop(round_id, "A01", "https://a.example/", "店铺A")

    def test_resume_same_round_and_shop_complete_preserved(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        self.db.save_shop_offers(
            rid, "A01", "https://a.example/", "店铺A",
            [(1, "111", "https://detail.1688.com/offer/111.html", "商品", "")],
            1,
        )
        rid2 = self.db.start_or_resume()
        self.assertEqual(rid, rid2)
        self._add_shop(rid2)
        remaining = self.db.shops_to_list(rid2)
        self.assertEqual(len(remaining), 0)  # 已完成店铺不会被重置

    def test_pending_offers_and_attempts(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        self.db.save_shop_offers(
            rid, "A01", "https://a.example/", "店铺A",
            [
                (1, "111", "https://detail.1688.com/offer/111.html", "商品1", ""),
                (2, "222", "https://detail.1688.com/offer/222.html", "商品2", ""),
            ],
            1,
        )
        pending = list(self.db.pending_offers(rid, max_attempts=2))
        self.assertEqual(len(pending), 2)

        self.db.mark_failure(rid, "A01", "111", attempt=1, note="测试失败")
        pending = list(self.db.pending_offers(rid, max_attempts=2))
        self.assertEqual([r["offer_id"] for r in pending], ["111", "222"])

        self.db.mark_failure(rid, "A01", "111", attempt=2, note="第二次失败")
        pending = list(self.db.pending_offers(rid, max_attempts=2))
        self.assertEqual([r["offer_id"] for r in pending], ["222"])

    def test_success_clears_failure_and_counts(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        self.db.save_shop_offers(
            rid, "A01", "https://a.example/", "店铺A",
            [(1, "111", "https://detail.1688.com/offer/111.html", "商品", "")],
            1,
        )
        self.db.mark_failure(rid, "A01", "111", 1, "先失败")
        self.db.clear_failures(rid, "A01", "111")
        self.db.save_snapshot_rows(rid, "A01", [
            {
                "round_id": rid, "shop_key": "A01",
                "shop_url": "https://a.example/", "shop_name": "店铺A",
                "offer_id": "111", "product_url": "https://detail.1688.com/offer/111.html",
                "product_name": "商品", "sku_id": "111:1", "sku_name": "小号",
                "sku_price": 1.0, "sku_stock": 100, "collected_at": "2026-09-04T00:00:00+00:00",
                "page_status": "成功", "attempt": 2,
            }
        ])
        total, ok = self.db.offer_counts(rid)
        self.assertEqual((total, ok), (1, 1))
        self.assertEqual(len(self.db.failed_rows(rid)), 0)

    def test_skus_master_only_upsert_never_delete(self):
        self.db.upsert_skus([{"offer_id": "11", "sku_name": "S", "sku_id": "a"}])
        self.db.upsert_skus([{"offer_id": "11", "sku_name": "S2", "sku_id": "a"}])  # 同(offer,sku_id)更新名称
        self.db.upsert_skus([{"offer_id": "12", "sku_name": "T", "sku_id": "b"}])  # 新增offer
        rows = self.db.conn.execute(
            "SELECT offer_id, sku_name, sku_id FROM skus ORDER BY offer_id, sku_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(dict(rows[0]), {"offer_id": "11", "sku_name": "S2", "sku_id": "a"})
        self.assertEqual(dict(rows[1]), {"offer_id": "12", "sku_name": "T", "sku_id": "b"})
        # 即便后续只写了 offer 12，offer 11 仍保留（不删除）
        self.db.upsert_skus([{"offer_id": "12", "sku_name": "T2", "sku_id": "b"}])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM skus").fetchone()[0], 2)
        self.assertEqual(
            dict(self.db.conn.execute(
                "SELECT offer_id, sku_name, sku_id FROM skus WHERE offer_id='12'"
            ).fetchone()),
            {"offer_id": "12", "sku_name": "T2", "sku_id": "b"},
        )

    def test_products_upsert_update_and_never_delete(self):
        self.db.upsert_product("11", "https://a/offer/11.html", "旧名")
        first = self.db.conn.execute(
            "SELECT first_seen_at FROM products WHERE offer_id='11'"
        ).fetchone()[0]
        self.db.upsert_product("11", "https://a/offer/11_new.html", "新名")
        row = self.db.conn.execute(
            "SELECT product_url, product_name, first_seen_at, last_seen_at FROM products WHERE offer_id='11'"
        ).fetchone()
        self.assertEqual(dict(row)["product_url"], "https://a/offer/11_new.html")
        self.assertEqual(dict(row)["product_name"], "新名")
        self.assertEqual(dict(row)["first_seen_at"], first)  # 保留首次时间
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) FROM products WHERE offer_id='11'").fetchone()[0],
            1,
        )

    def test_inventory_upsert_and_diff(self):
        base = {
            "shop_key": "A", "offer_id": "11", "sku_id": "s1", "sku_name": "S",
            "shop_name": "店铺A", "product_name": "商品", "sku_price": 1.0,
        }
        self.db.upsert_inventory([{**base, "sku_stock": 200, "collected_at": "2026-09-04T02:00:00+00:00"}])
        r = self.db.conn.execute(
            "SELECT stock, diff, date FROM inventory WHERE shop_key='A' AND date='2026-09-04'"
        ).fetchone()
        self.assertEqual((r["stock"], r["diff"], r["date"]), (200, None, "2026-09-04"))

        self.db.upsert_inventory([{**base, "sku_stock": 160, "collected_at": "2026-09-05T02:00:00+00:00"}])
        r2 = self.db.conn.execute(
            "SELECT stock, diff FROM inventory WHERE shop_key='A' AND date='2026-09-05'"
        ).fetchone()
        self.assertEqual(r2["diff"], -40)

        # 同一天再次抓取：覆盖 stock，diff 仍对比前一日(200)
        self.db.upsert_inventory([{**base, "sku_stock": 180, "collected_at": "2026-09-05T02:00:00+00:00"}])
        r3 = self.db.conn.execute(
            "SELECT stock, diff FROM inventory WHERE shop_key='A' AND date='2026-09-05'"
        ).fetchone()
        self.assertEqual(r3["stock"], 180)
        self.assertEqual(r3["diff"], -20)

    def test_event_log_append_only_and_interval_per_channel(self):
        rid = self.db.start_or_resume()
        self.db.append_event(rid, "list_load", shop_key="A01", phase="listing")
        # 同 (round, shop) 上一条，interval 应非空
        self.db.append_event(rid, "product_open", shop_key="A01", phase="listing")
        # 跨店铺：shop_key 不同，interval 应为空
        self.db.append_event(rid, "popup_open", shop_key="A02", offer_id="111", phase="listing")
        rows = self.db.conn.execute(
            "SELECT event, shop_key, interval_ms FROM event_log ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[0]["interval_ms"])       # 首条
        self.assertIsNotNone(rows[1]["interval_ms"])    # 同渠道第二条
        self.assertIsNone(rows[2]["interval_ms"])       # 跨渠道重置
        ts_values = [r["event"] for r in rows]
        self.assertEqual(ts_values, ["list_load", "product_open", "popup_open"])
        # 只追加：再写一条，总数增加，旧值不变
        self.db.append_event(rid, "popup_close", shop_key="A01")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0], 4)

    def test_record_params_dedup(self):
        rid = self.db.start_or_resume()
        # 用最小配置对象记录参数（只依赖配置键）
        class FakeCfg:
            detail_delay_sec = (0.8, 2.0)
            list_delay_sec = (1.5, 3.0)
            long_pause_interval = (12, 20)
            long_pause_sec = (10.0, 20.0)
            batch_size = 300
            batch_rest_sec = (20.0, 40.0)
            action_delay_sec = (0.6, 1.5)
            read_delay_sec = (0.8, 1.5)
            retry_base_sec = 5.0
            retry_jitter_sec = 2.0
            max_pages_per_shop = 10
            human_pause_minutes = 10
            alarm_on_intervention = True

        h1 = self.db.record_params(FakeCfg())
        h2 = self.db.record_params(FakeCfg())
        self.assertEqual(h1, h2)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM run_params").fetchone()[0], 1)
        row = self.db.conn.execute(
            "SELECT params_hash, config_json FROM run_params LIMIT 1"
        ).fetchone()
        self.assertEqual(row["params_hash"], h1)
        self.assertIn("detail_delay_sec", row["config_json"])

    def test_inventory_exists_by_shop_offer_date(self):
        base = {
            "shop_key": "A", "offer_id": "11", "sku_id": "s1", "sku_name": "S",
            "shop_name": "店铺A", "product_name": "商品", "sku_price": 1.0,
        }
        self.db.upsert_inventory([{**base, "sku_stock": 200,
                                   "collected_at": "2026-09-05T02:00:00+00:00"}])
        self.assertTrue(self.db.inventory_exists("A", "11", "2026-09-05"))
        self.assertFalse(self.db.inventory_exists("A", "11", "2026-09-06"))   # 其它日期
        self.assertFalse(self.db.inventory_exists("A", "99", "2026-09-05"))   # 其它商品
        self.assertFalse(self.db.inventory_exists("B", "11", "2026-09-05"))   # 其它店铺

    def test_inventory_exists_by_name(self):
        base = {
            "shop_key": "A", "offer_id": "11", "sku_id": "s1", "sku_name": "S",
            "shop_name": "店铺A", "product_name": "厨房清洁膏", "sku_price": 1.0,
        }
        self.db.upsert_inventory([{**base, "sku_stock": 200,
                                   "collected_at": "2026-09-05T02:00:00+00:00"}])
        self.assertTrue(self.db.inventory_exists_by_name("A", "厨房清洁膏", "2026-09-05"))
        self.assertFalse(self.db.inventory_exists_by_name("A", "厨房清洁膏", "2026-09-06"))  # 其它日期
        self.assertFalse(self.db.inventory_exists_by_name("A", "别的商品", "2026-09-05"))   # 其它名称
        self.assertFalse(self.db.inventory_exists_by_name("B", "厨房清洁膏", "2026-09-05"))  # 其它店铺
        self.assertFalse(self.db.inventory_exists_by_name("A", "", "2026-09-05"))           # 空名不查

    def test_cst_date_default_now(self):
        # 回归：cst_date() 应可不带参调用，返回北京时间当天
        self.assertRegex(cst_date(), r"^\d{4}-\d{2}-\d{2}$")

    def test_mark_skipped_success_and_idempotent(self):
        rid = self.db.start_or_resume()
        self.db.mark_skipped(
            rid, "A", "https://a.example/", "店铺A", "111",
            "https://detail.1688.com/offer/111.html", "商品",
        )
        rows = self.db.conn.execute(
            "SELECT offer_id, page_status, detail_note FROM snapshots "
            "WHERE round_id=? AND shop_key='A'", (rid,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["page_status"], "成功")
        self.assertIn("跳过", rows[0]["detail_note"])
        # 重复调用不重复写入
        self.db.mark_skipped(
            rid, "A", "https://a.example/", "店铺A", "111",
            "https://detail.1688.com/offer/111.html", "商品",
        )
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE round_id=?", (rid,)
        ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
