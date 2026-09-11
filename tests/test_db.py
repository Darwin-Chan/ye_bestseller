import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from bestseller_monitor.db import (
    Database,
    SCHEMA,
    connect,
    cst_date,
    past_day_cutoff,
)


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.db = Database(self.conn)
        # 测试期间禁用“23:55 跨天中止”，避免在真实窗口内运行测试时误触发。
        self._day_patcher = patch("bestseller_monitor.db.past_day_cutoff", return_value=False)
        self._day_patcher.start()

    def tearDown(self):
        self._day_patcher.stop()
        self.conn.close()
        self.tmp.cleanup()

    def _add_shop(self, round_id):
        self.db.add_shop(round_id, "A01", "https://a.example/", "店铺A")

    def test_submit_inventory_snapshot_persists_complete_result(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)

        result = self.db.submit_inventory_snapshot(
            round_id=rid,
            shop_key="A01",
            shop_url="https://a.example/",
            shop_name="店铺A",
            offer_id="111",
            product_url="https://detail.1688.com/offer/111.html",
            list_title="榜单标题",
            detail_title="详情标题",
            main_image_url="https://img.example/111.jpg",
            sku_rows=[{
                "sku_id": None,
                "sku_name": "红色 / M",
                "sku_price": 12.5,
                "sku_stock": 0,
            }],
            collected_at="2026-09-04T02:00:00+00:00",
            attempt=2,
        )

        self.assertFalse(result.stop_round)
        product = self.conn.execute(
            "SELECT product_name, main_image_url FROM products WHERE offer_id='111'"
        ).fetchone()
        self.assertEqual(tuple(product), ("详情标题", "https://img.example/111.jpg"))
        snapshot = self.conn.execute(
            "SELECT product_name, sku_name, sku_stock, attempt FROM snapshots "
            "WHERE round_id=? AND offer_id='111' AND page_status='成功'",
            (rid,),
        ).fetchone()
        self.assertEqual(tuple(snapshot), ("榜单标题", "红色 / M", 0, 2))
        inventory = self.conn.execute(
            "SELECT product_name, stock FROM inventory "
            "WHERE shop_key='A01' AND offer_id='111'"
        ).fetchone()
        self.assertEqual(tuple(inventory), ("榜单标题", 0))

    def test_submit_inventory_snapshot_replaces_same_sku_and_preserves_absent_sku(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        base = dict(
            round_id=rid,
            shop_key="A01",
            shop_url="https://a.example/",
            shop_name="店铺A",
            offer_id="111",
            product_url="https://detail.1688.com/offer/111.html",
            list_title="榜单标题",
            detail_title="详情标题",
            main_image_url="https://img.example/111.jpg",
            collected_at="2026-09-04T02:00:00+00:00",
            attempt=1,
        )
        self.db.submit_inventory_snapshot(
            **base,
            sku_rows=[
                {"sku_id": "red", "sku_name": "红色", "sku_price": 10, "sku_stock": 10},
                {"sku_id": "blue", "sku_name": "蓝色", "sku_price": 11, "sku_stock": 11},
            ],
        )
        self.db.submit_inventory_snapshot(
            **{**base, "detail_title": "", "main_image_url": "", "attempt": 2},
            sku_rows=[
                {"sku_id": "red", "sku_name": "红色新名", "sku_price": 12, "sku_stock": 2},
                {"sku_id": "green", "sku_name": "绿色", "sku_price": 13, "sku_stock": 13},
            ],
        )

        rows = self.conn.execute(
            "SELECT sku_id, sku_name, sku_stock, attempt FROM snapshots "
            "WHERE round_id=? AND shop_key='A01' AND offer_id='111' AND page_status='成功' "
            "ORDER BY sku_id",
            (rid,),
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("blue", "蓝色", 11, 1),
            ("green", "绿色", 13, 2),
            ("red", "红色新名", 2, 2),
        ])
        product = self.conn.execute(
            "SELECT product_name, main_image_url FROM products WHERE offer_id='111'"
        ).fetchone()
        self.assertEqual(tuple(product), ("详情标题", "https://img.example/111.jpg"))

    def test_submit_inventory_snapshot_rolls_back_all_data_and_keeps_failure(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        self.db.mark_failure(
            rid,
            "A01",
            "111",
            1,
            "上一次解析失败",
            shop_url="https://a.example/",
            shop_name="店铺A",
            product_url="https://detail.1688.com/offer/111.html",
            product_name="榜单标题",
        )

        with patch.object(self.db, "_upsert_inventory", side_effect=sqlite3.OperationalError("写入失败")):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.submit_inventory_snapshot(
                    round_id=rid,
                    shop_key="A01",
                    shop_url="https://a.example/",
                    shop_name="店铺A",
                    offer_id="111",
                    product_url="https://detail.1688.com/offer/111.html",
                    list_title="榜单标题",
                    detail_title="详情标题",
                    main_image_url="https://img.example/111.jpg",
                    sku_rows=[{
                        "sku_id": "red",
                        "sku_name": "红色",
                        "sku_price": 10,
                        "sku_stock": 10,
                    }],
                    collected_at="2026-09-04T02:00:00+00:00",
                    attempt=2,
                )

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM skus").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE page_status!='成功' AND round_id=?", (rid,)
        ).fetchone()[0], 1)

    def test_submit_inventory_snapshot_rejects_duplicate_generated_sku_before_transaction(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        with self.assertRaisesRegex(ValueError, "重复 SKU 编号"):
            self.db.submit_inventory_snapshot(
                round_id=rid,
                shop_key="A01",
                shop_url="https://a.example/",
                shop_name="店铺A",
                offer_id="111",
                product_url="https://detail.1688.com/offer/111.html",
                list_title="榜单标题",
                detail_title="详情标题",
                main_image_url=None,
                sku_rows=[
                    {"sku_id": None, "sku_name": "红色", "sku_price": 10, "sku_stock": 10},
                    {"sku_id": None, "sku_name": "红色", "sku_price": 11, "sku_stock": 11},
                ],
                collected_at="2026-09-04T02:00:00+00:00",
                attempt=1,
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)

    def test_submit_inventory_snapshot_rejects_blank_product_url_before_transaction(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        with self.assertRaisesRegex(ValueError, "缺少商品或店铺标识"):
            self.db.submit_inventory_snapshot(
                round_id=rid,
                shop_key="A01",
                shop_url="https://a.example/",
                shop_name="店铺A",
                offer_id="111",
                product_url="   ",
                list_title="榜单标题",
                detail_title="详情标题",
                main_image_url=None,
                sku_rows=[{"sku_id": "red", "sku_name": "红色", "sku_price": 10, "sku_stock": 10}],
                collected_at="2026-09-04T02:00:00+00:00",
                attempt=1,
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)

    def test_submit_inventory_snapshot_rejects_blank_time_and_malformed_sku_before_transaction(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        base = dict(
            round_id=rid,
            shop_key="A01",
            shop_url="https://a.example/",
            shop_name="店铺A",
            offer_id="111",
            product_url="https://detail.1688.com/offer/111.html",
            list_title="榜单标题",
            detail_title="详情标题",
            main_image_url=None,
            collected_at="2026-09-04T02:00:00+00:00",
            attempt=1,
        )
        with self.assertRaisesRegex(ValueError, "必须有采集时间"):
            self.db.submit_inventory_snapshot(
                **{**base, "collected_at": "   "},
                sku_rows=[{"sku_id": "red", "sku_name": "红色", "sku_price": 10, "sku_stock": 10}],
            )
        with self.assertRaisesRegex(ValueError, "必须是结构化记录"):
            self.db.submit_inventory_snapshot(**base, sku_rows=["not-a-sku"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)

    def test_submit_inventory_snapshot_returns_stop_after_commit(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        with patch("bestseller_monitor.db.past_day_cutoff", return_value=True):
            result = self.db.submit_inventory_snapshot(
                round_id=rid,
                shop_key="A01",
                shop_url="https://a.example/",
                shop_name="店铺A",
                offer_id="111",
                product_url="https://detail.1688.com/offer/111.html",
                list_title="榜单标题",
                detail_title="详情标题",
                main_image_url=None,
                sku_rows=[{"sku_id": "red", "sku_name": "红色", "sku_price": 10, "sku_stock": 10}],
                collected_at="2026-09-09T15:54:00+00:00",
                attempt=1,
            )
        self.assertTrue(result.stop_round)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 1)

    def test_connect_deduplicates_success_snapshots_before_unique_index(self):
        legacy_tmp = tempfile.TemporaryDirectory()
        db_path = Path(legacy_tmp.name) / "legacy-duplicates.db"
        raw = sqlite3.connect(db_path)
        raw.executescript(SCHEMA)
        raw.execute("INSERT INTO rounds(id, started_at) VALUES (1, '2026-09-04T00:00:00+00:00')")
        raw.executemany(
            "INSERT INTO snapshots(round_id, shop_key, shop_url, shop_name, offer_id, product_url, "
            "product_name, sku_id, sku_name, sku_price, sku_stock, collected_at, page_status, attempt) "
            "VALUES (1, 'A01', 'https://a.example/', '店铺A', '111', 'https://detail/111', "
            "'榜单标题', 'red', '红色', ?, ?, ?, '成功', ?)",
            [(10, 10, "2026-09-04T02:00:00+00:00", 1),
             (12, 2, "2026-09-04T03:00:00+00:00", 2)],
        )
        raw.commit()
        raw.close()

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT sku_stock, attempt FROM snapshots WHERE round_id=1 AND shop_key='A01' "
                "AND offer_id='111' AND sku_id='red' AND page_status='成功'"
            ).fetchone()
            self.assertEqual(tuple(row), (2, 2))
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE round_id=1 AND shop_key='A01' "
                "AND offer_id='111' AND sku_id='red' AND page_status='成功'"
            ).fetchone()[0], 1)
            indexes = conn.execute("PRAGMA index_list('snapshots')").fetchall()
            self.assertTrue(any("success" in row[1] for row in indexes))
        finally:
            conn.close()
            legacy_tmp.cleanup()

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
        self.db.submit_inventory_snapshot(
            round_id=rid,
            shop_key="A01",
            shop_url="https://a.example/",
            shop_name="店铺A",
            offer_id="111",
            product_url="https://detail.1688.com/offer/111.html",
            list_title="商品",
            detail_title="商品详情",
            main_image_url=None,
            sku_rows=[{"sku_id": "111:1", "sku_name": "小号", "sku_price": 1.0, "sku_stock": 100}],
            collected_at="2026-09-04T00:00:00+00:00",
            attempt=2,
        )
        total, ok = self.db.offer_counts(rid)
        self.assertEqual((total, ok), (1, 1))
        self.assertEqual(len(self.db.failed_rows(rid)), 0)

    def test_offer_counts_are_per_offer_not_per_sku(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        offers = [
            (i, str(i), f"https://detail.1688.com/offer/{i}.html", f"商品{i}", "")
            for i in range(1, 11)
        ]
        self.db.save_shop_offers(rid, "A01", "https://a.example/", "店铺A", offers, 1)
        self.db.submit_inventory_snapshot(
            round_id=rid,
            shop_key="A01",
            shop_url="https://a.example/",
            shop_name="店铺A",
            offer_id="1",
            product_url="https://detail.1688.com/offer/1.html",
            list_title="商品1",
            detail_title="商品1详情",
            main_image_url=None,
            sku_rows=[
                {"sku_id": f"1:{i}", "sku_name": f"规格{i}", "sku_price": 1.0, "sku_stock": 100}
                for i in range(10)
            ],
            collected_at="2026-09-04T00:00:00+00:00",
            attempt=1,
        )
        for offer_id in map(str, range(2, 11)):
            self.db.mark_failure(rid, "A01", offer_id, 1, "解析失败")
        self.assertEqual(self.db.offer_counts(rid), (10, 1))

    def test_success_snapshot_requires_stock_and_id(self):
        rid = self.db.start_or_resume()
        with self.assertRaisesRegex(ValueError, "名称和整数库存"):
            self.db.submit_inventory_snapshot(
                round_id=rid,
                shop_key="A01",
                shop_url="https://a.example/",
                shop_name="店铺A",
                offer_id="1",
                product_url="https://detail.1688.com/offer/1.html",
                list_title="商品",
                detail_title="商品详情",
                main_image_url=None,
                sku_rows=[{"sku_id": "1:1", "sku_name": "规格", "sku_price": 1.0, "sku_stock": None}],
                collected_at="2026-09-04T00:00:00+00:00",
                attempt=1,
            )

    def test_past_day_cutoff_uses_beijing_time(self):
        self.assertFalse(past_day_cutoff("2026-09-09T15:54:00+00:00"))  # 北京 23:54
        self.assertTrue(past_day_cutoff("2026-09-09T15:55:00+00:00"))   # 北京 23:55
        self.assertTrue(past_day_cutoff("2026-09-09T15:59:59+00:00"))   # 北京 23:59:59
        self.assertFalse(past_day_cutoff("2026-09-09T16:00:00+00:00"))  # 北京次日 00:00

    def test_submit_inventory_snapshot_returns_stop_after_commit_at_day_boundary(self):
        rid = self.db.start_or_resume()
        with patch("bestseller_monitor.db.past_day_cutoff", return_value=True):
            result = self.db.submit_inventory_snapshot(
                round_id=rid,
                shop_key="A01",
                shop_url="https://a.example/",
                shop_name="店铺A",
                offer_id="1",
                product_url="https://detail.1688.com/offer/1.html",
                list_title="商品",
                detail_title="商品详情",
                main_image_url=None,
                sku_rows=[{"sku_id": "1:1", "sku_name": "规格", "sku_price": 1.0, "sku_stock": 100}],
                collected_at="2026-09-09T15:54:00+00:00",
                attempt=1,
            )

        self.assertTrue(result.stop_round)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE round_id=?", (rid,)
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM skus").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 1)

    def test_incomplete_inventory_does_not_trigger_same_day_dedupe(self):
        self.db.conn.execute(
            "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, product_name) "
            "VALUES ('A', '11', 's1', '2026-09-05', NULL, '厨房清洁膏')"
        )
        self.db.conn.commit()
        self.assertFalse(self.db.inventory_exists("A", "11", "2026-09-05"))
        self.assertFalse(self.db.inventory_exists_by_name("A", "厨房清洁膏", "2026-09-05"))
        self.assertIsNone(self.db.find_offer_id_by_name("A", "厨房清洁膏", "2026-09-05"))

    def _submit_offer(self, round_id, collected_at, sku_rows):
        self.db.submit_inventory_snapshot(
            round_id=round_id,
            shop_key="A01",
            shop_url="https://a.example/",
            shop_name="店铺A",
            offer_id="11",
            product_url="https://detail.1688.com/offer/11.html",
            list_title="商品",
            detail_title="商品详情",
            main_image_url=None,
            sku_rows=sku_rows,
            collected_at=collected_at,
            attempt=1,
        )

    def _same_day_rows(self):
        snapshots = [
            r[0] for r in self.conn.execute(
                "SELECT sku_id FROM snapshots WHERE shop_key='A01' AND offer_id='11' "
                "AND page_status='成功' ORDER BY sku_id"
            )
        ]
        inventory = [
            tuple(r) for r in self.conn.execute(
                "SELECT sku_id, stock FROM inventory WHERE shop_key='A01' AND offer_id='11' "
                "AND date='2026-09-05' ORDER BY sku_id"
            )
        ]
        return snapshots, inventory

    def test_single_spec_submit_replaces_sku_level_rows_for_same_day(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        self._submit_offer(rid, "2026-09-05T02:00:00+00:00", [
            {"sku_id": "a", "sku_name": "小号", "sku_price": 1.0, "sku_stock": 100},
            {"sku_id": "b", "sku_name": "大号", "sku_price": 2.0, "sku_stock": 200},
        ])

        self._submit_offer(rid, "2026-09-05T03:00:00+00:00", [
            {"sku_id": "default", "sku_name": "默认(单规格)", "sku_price": 1.0, "sku_stock": 300},
        ])

        snapshots, inventory = self._same_day_rows()
        self.assertEqual(snapshots, ["default"])
        self.assertEqual(inventory, [("default", 300)])

    def test_sku_level_submit_replaces_single_spec_rows_for_same_day(self):
        rid = self.db.start_or_resume()
        self._add_shop(rid)
        self._submit_offer(rid, "2026-09-05T02:00:00+00:00", [
            {"sku_id": "default", "sku_name": "默认(单规格)", "sku_price": 1.0, "sku_stock": 300},
        ])

        self._submit_offer(rid, "2026-09-05T03:00:00+00:00", [
            {"sku_id": "a", "sku_name": "小号", "sku_price": 1.0, "sku_stock": 100},
        ])

        snapshots, inventory = self._same_day_rows()
        self.assertEqual(snapshots, ["a"])
        self.assertEqual(inventory, [("a", 100)])

    def test_failure_with_explicit_metadata_is_recorded_before_listing_write(self):
        rid = self.db.start_or_resume()
        self.db.mark_failure(
            rid, "A01", "111", 1, "详情页缺少库存",
            shop_url="https://a.example/", shop_name="店铺A",
            product_url="https://detail.1688.com/offer/111.html", product_name="商品",
        )
        row = self.db.conn.execute(
            "SELECT shop_name, product_name, page_status FROM snapshots WHERE round_id=?", (rid,)
        ).fetchone()
        self.assertEqual(dict(row), {"shop_name": "店铺A", "product_name": "商品", "page_status": "失败"})

    def test_submit_updates_sku_master_and_preserves_product_first_seen(self):
        rid = self.db.start_or_resume()
        base = dict(
            round_id=rid,
            shop_key="A",
            shop_url="https://a.example/",
            shop_name="店铺A",
            offer_id="11",
            product_url="https://a/offer/11.html",
            list_title="榜单商品",
            detail_title="详情商品",
            main_image_url="https://img/a.jpg",
            collected_at="2026-09-04T02:00:00+00:00",
            attempt=1,
        )
        self.db.submit_inventory_snapshot(
            **base,
            sku_rows=[{"sku_id": "a", "sku_name": "S", "sku_price": 1.0, "sku_stock": 100}],
        )
        first_seen = self.conn.execute(
            "SELECT first_seen_at FROM products WHERE offer_id='11'"
        ).fetchone()[0]
        self.db.submit_inventory_snapshot(
            **{**base, "product_url": "https://a/offer/11_new.html", "detail_title": "新详情名"},
            sku_rows=[{"sku_id": "a", "sku_name": "S2", "sku_price": 1.0, "sku_stock": 90}],
        )
        product = self.conn.execute(
            "SELECT product_url, product_name, first_seen_at FROM products WHERE offer_id='11'"
        ).fetchone()
        self.assertEqual(tuple(product), ("https://a/offer/11_new.html", "新详情名", first_seen))
        sku = self.conn.execute(
            "SELECT offer_id, sku_name, sku_id FROM skus WHERE offer_id='11'"
        ).fetchone()
        self.assertEqual(tuple(sku), ("11", "S2", "a"))

    def test_submit_inventory_diff_uses_previous_date_and_updates_same_day(self):
        rid = self.db.start_or_resume()
        base = dict(
            round_id=rid,
            shop_key="A",
            shop_url="https://a.example/",
            shop_name="店铺A",
            offer_id="11",
            product_url="https://a/offer/11.html",
            list_title="商品",
            detail_title="商品详情",
            main_image_url=None,
            attempt=1,
        )
        for collected_at, stock in [
            ("2026-09-04T02:00:00+00:00", 200),
            ("2026-09-05T02:00:00+00:00", 160),
            ("2026-09-05T03:00:00+00:00", 180),
        ]:
            self.db.submit_inventory_snapshot(
                **base,
                collected_at=collected_at,
                sku_rows=[{"sku_id": "s1", "sku_name": "S", "sku_price": 1.0, "sku_stock": stock}],
            )
        row = self.conn.execute(
            "SELECT stock, diff, date FROM inventory WHERE shop_key='A' AND offer_id='11' "
            "AND date='2026-09-05'"
        ).fetchone()
        self.assertEqual(tuple(row), (180, -20, "2026-09-05"))

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
        rid = self.db.start_or_resume()
        self.db.submit_inventory_snapshot(
            round_id=rid, shop_key="A", shop_url="https://a.example/", shop_name="店铺A",
            offer_id="11", product_url="https://a/offer/11.html", list_title="商品",
            detail_title="详情商品", main_image_url=None,
            sku_rows=[{"sku_id": "s1", "sku_name": "S", "sku_price": 1.0, "sku_stock": 200}],
            collected_at="2026-09-05T02:00:00+00:00", attempt=1,
        )
        self.assertTrue(self.db.inventory_exists("A", "11", "2026-09-05"))
        self.assertFalse(self.db.inventory_exists("A", "11", "2026-09-06"))   # 其它日期
        self.assertFalse(self.db.inventory_exists("A", "99", "2026-09-05"))   # 其它商品
        self.assertFalse(self.db.inventory_exists("B", "11", "2026-09-05"))   # 其它店铺

    def test_inventory_exists_by_name(self):
        rid = self.db.start_or_resume()
        self.db.submit_inventory_snapshot(
            round_id=rid, shop_key="A", shop_url="https://a.example/", shop_name="店铺A",
            offer_id="11", product_url="https://a/offer/11.html", list_title="厨房清洁膏",
            detail_title="详情商品", main_image_url=None,
            sku_rows=[{"sku_id": "s1", "sku_name": "S", "sku_price": 1.0, "sku_stock": 200}],
            collected_at="2026-09-05T02:00:00+00:00", attempt=1,
        )
        self.assertTrue(self.db.inventory_exists_by_name("A", "厨房清洁膏", "2026-09-05"))
        self.assertFalse(self.db.inventory_exists_by_name("A", "厨房清洁膏", "2026-09-06"))  # 其它日期
        self.assertFalse(self.db.inventory_exists_by_name("A", "别的商品", "2026-09-05"))   # 其它名称
        self.assertFalse(self.db.inventory_exists_by_name("B", "厨房清洁膏", "2026-09-05"))  # 其它店铺
        self.assertFalse(self.db.inventory_exists_by_name("A", "", "2026-09-05"))           # 空名不查

    def test_cst_date_default_now(self):
        # 回归：cst_date() 应可不带参调用，返回北京时间当天
        self.assertRegex(cst_date(), r"^\d{4}-\d{2}-\d{2}$")

    def test_skip_is_recorded_as_skip_not_success_and_idempotent(self):
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
        # 跳过表示「本轮不访问详情」，不表示产生了库存观测
        self.assertEqual(rows[0]["page_status"], "跳过")
        self.assertIn("跳过", rows[0]["detail_note"])
        # 重复调用不重复写入
        self.db.mark_skipped(
            rid, "A", "https://a.example/", "店铺A", "111",
            "https://detail.1688.com/offer/111.html", "商品",
        )
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE round_id=?", (rid,)
        ).fetchone()[0], 1)

    def test_skipped_offer_counts_as_handled_and_is_not_a_failure(self):
        rid = self.db.start_or_resume()
        self.db.save_shop_offers(
            rid, "A", "https://a.example/", "店铺A",
            [(1, "111", "https://detail.1688.com/offer/111.html", "商品", "")], 1,
        )
        self.db.mark_skipped(
            rid, "A", "https://a.example/", "店铺A", "111",
            "https://detail.1688.com/offer/111.html", "商品",
        )
        # 跳过是已处理结果：计入成功、不算失败
        self.assertEqual(self.db.offer_counts(rid), (1, 1))
        self.assertEqual(self.db.failed_rows(rid), [])

    def test_skip_is_not_inventory_evidence(self):
        rid = self.db.start_or_resume()
        self.db.mark_skipped(
            rid, "A", "https://a.example/", "店铺A", "111",
            "https://detail.1688.com/offer/111.html", "商品",
        )
        # 跳过不产生库存观测，不能成为同日去重证据
        self.assertFalse(self.db.inventory_exists("A", "111", cst_date()))
        self.assertFalse(self.db.inventory_exists_by_name("A", "商品", cst_date()))

    def test_failed_observation_is_not_dedupe_evidence(self):
        rid = self.db.start_or_resume()
        self.db.save_shop_offers(
            rid, "A", "https://a.example/", "店铺A",
            [(1, "111", "https://detail.1688.com/offer/111.html", "商品", "")], 1,
        )
        self.db.mark_failure(rid, "A", "111", 1, "解析失败")
        # 失败不产生库存观测，既不触发同日去重，也仍是待补采商品
        self.assertFalse(self.db.inventory_exists("A", "111", cst_date()))
        self.assertFalse(self.db.inventory_exists_by_name("A", "商品", cst_date()))
        self.assertEqual(
            [row["offer_id"] for row in self.db.pending_offers(rid, max_attempts=2)],
            ["111"],
        )

    def test_find_offer_id_by_name_unique_vs_ambiguous(self):
        rid = self.db.start_or_resume()
        self.db.submit_inventory_snapshot(
            round_id=rid, shop_key="A", shop_url="https://a.example/", shop_name="店铺A",
            offer_id="11", product_url="https://a/offer/11.html", list_title="厨房清洁膏",
            detail_title="详情商品", main_image_url=None,
            sku_rows=[{"sku_id": "s1", "sku_name": "S", "sku_price": 1.0, "sku_stock": 200}],
            collected_at="2026-09-05T02:00:00+00:00", attempt=1,
        )
        # 唯一 offer_id：返回它
        self.assertEqual(self.db.find_offer_id_by_name("A", "厨房清洁膏", "2026-09-05"), "11")
        # 其它日期 / 店铺 / 名称拿不到
        self.assertIsNone(self.db.find_offer_id_by_name("A", "厨房清洁膏", "2026-09-06"))
        self.assertIsNone(self.db.find_offer_id_by_name("B", "厨房清洁膏", "2026-09-05"))
        self.assertIsNone(self.db.find_offer_id_by_name("A", "别的商品", "2026-09-05"))
        # 同名多品（两个不同 offer_id）→ 不猜，返回 None
        self.db.submit_inventory_snapshot(
            round_id=rid, shop_key="A", shop_url="https://a.example/", shop_name="店铺A",
            offer_id="22", product_url="https://a/offer/22.html", list_title="厨房清洁膏",
            detail_title="详情商品", main_image_url=None,
            sku_rows=[{"sku_id": "s2", "sku_name": "S", "sku_price": 1.0, "sku_stock": 100}],
            collected_at="2026-09-05T02:00:00+00:00", attempt=1,
        )
        self.assertIsNone(self.db.find_offer_id_by_name("A", "厨房清洁膏", "2026-09-05"))

    def test_click_card_failures_dedup_by_card(self):
        rid = self.db.start_or_resume()
        evs = [
            ("A", "click_ok", "page=1&idx=0&offer_id=1&sku=3"),
            ("A", "click_no_popup", "page=1&idx=1"),
            ("A", "click_url_notoffer", "page=1&idx=2"),
            ("A", "click_ok", "page=2&idx=1&offer_id=2&sku=2"),
            ("B", "click_url_notoffer", "page=1&idx=0"),
            ("B", "click_ok", "page=1&idx=0&offer_id=3&sku=1"),
            ("B", "click_no_popup", "page=2&idx=0"),
            ("C", "click_deny", "page=1&idx=0&n=1"),
            ("C", "click_ok", "page=1&idx=0&offer_id=9&sku=2"),
            ("C", "click_deny", "page=2&idx=0&n=3&scan"),
        ]
        for shop, ev, note in evs:
            self.db.conn.execute(
                "INSERT INTO event_log(round_id, shop_key, event, ts, note, kind) "
                "VALUES (?, ?, ?, ?, ?, 'work')",
                (rid, shop, ev, "2026-09-05T00:00:00+00:00", note),
            )
        self.db.conn.commit()
        # 失败卡片：A(1,1)、A(1,2)、B(2,0)、C(2,0)；B(1,0)/C(1,0) 曾失败但最终 click_ok → 不算
        self.assertEqual(self.db.click_card_failures(rid), 4)

    def test_connect_migration_drops_stock_delta(self):
        # 模拟旧库：snapshots 含 stock_delta 列
        old = Path(tempfile.gettempdir()) / f"bestseller_old_{id(self)}.db"
        c = sqlite3.connect(str(old))
        c.execute(
            "CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "round_id INTEGER, shop_key TEXT, offer_id TEXT, sku_id TEXT, "
            "stock_delta INTEGER, collected_at TEXT)"
        )
        c.commit()
        c.close()
        # connect() 应执行迁移删除 stock_delta
        conn = connect(old)
        cols = [r[1] for r in conn.execute('PRAGMA table_info("snapshots")').fetchall()]
        self.assertNotIn("stock_delta", cols)
        conn.close()
        old.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
