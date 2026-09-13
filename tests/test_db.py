import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from bestseller_monitor.db import (
    Database,
    EVENT_REFRESH_INDEXES,
    ROUND_TALLY_INDEX,
    SCHEMA,
    SNAPSHOT_SUCCESS_INDEX,
    connect,
    cst_date,
)
from bestseller_monitor import db
from helpers import new_round
from bestseller_monitor.parse import DEFAULT_SKU_ID, DEFAULT_SKU_NAME


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

    def _remember(self, round_id, offer):
        self.db.remember_shop_offer(
            round_id, "A01", "https://a.example/", "店铺A", offer,
        )

    def test_remembering_the_same_offer_twice_keeps_one_row_and_one_count(self):
        """中断时已发现的商品立刻落榜单行：重复发现不新增行、不重复计数。"""
        rid = new_round(self.db)
        self._add_shop(rid)
        offer = (1, "11", "https://detail.1688.com/offer/11.html", "商品11", "")

        self._remember(rid, offer)
        self._remember(rid, offer)

        rows = self.conn.execute(
            "SELECT offer_id FROM shop_offers WHERE round_id=? AND shop_key='A01'",
            (rid,),
        ).fetchall()
        self.assertEqual([row["offer_id"] for row in rows], ["11"])
        count = self.conn.execute(
            "SELECT offer_count FROM shop_rounds WHERE round_id=? AND shop_key='A01'",
            (rid,),
        ).fetchone()["offer_count"]
        self.assertEqual(count, 1, "已发现商品数按商品编号计，不重复计数")

    def test_complete_listing_replaces_earlier_discoveries_and_keeps_order(self):
        """完整榜单是权威列表：覆盖增量发现的行，rank 反映传入顺序。"""
        rid = new_round(self.db)
        self._add_shop(rid)
        self._remember(rid, (1, "11", "https://detail.1688.com/offer/11.html", "商品11", ""))
        self._remember(rid, (2, "22", "https://detail.1688.com/offer/22.html", "商品22", ""))

        self.db.save_shop_offers(
            rid, "A01", "https://a.example/", "店铺A",
            [(1, "22", "https://detail.1688.com/offer/22.html", "商品22", ""),
             (2, "33", "https://detail.1688.com/offer/33.html", "商品33", "")],
            1,
        )

        rows = self.conn.execute(
            "SELECT offer_id, rank FROM shop_offers WHERE round_id=? AND shop_key='A01' "
            "ORDER BY rank",
            (rid,),
        ).fetchall()
        self.assertEqual([(row["offer_id"], row["rank"]) for row in rows], [("22", 1), ("33", 2)])
        shop = self.conn.execute(
            "SELECT list_status, offer_count FROM shop_rounds WHERE round_id=? AND shop_key='A01'",
            (rid,),
        ).fetchone()
        self.assertEqual(shop["list_status"], "完成")
        self.assertEqual(shop["offer_count"], 2)

    def test_remembering_offers_keeps_earlier_discoveries(self):
        """已发现集合只增不减：续跑再次中断，先前发现的商品不能被抹掉。"""
        rid = new_round(self.db)
        self._add_shop(rid)
        self._remember(rid, (1, "11", "https://detail.1688.com/offer/11.html", "商品11", ""))
        self._remember(rid, (2, "22", "https://detail.1688.com/offer/22.html", "商品22", ""))
        self._remember(rid, (1, "33", "https://detail.1688.com/offer/33.html", "商品33", ""))

        rows = self.conn.execute(
            "SELECT offer_id FROM shop_offers WHERE round_id=? AND shop_key='A01' ORDER BY offer_id",
            (rid,),
        ).fetchall()
        self.assertEqual([row["offer_id"] for row in rows], ["11", "22", "33"])
        shop = self.conn.execute(
            "SELECT list_status, offer_count FROM shop_rounds WHERE round_id=? AND shop_key='A01'",
            (rid,),
        ).fetchone()
        self.assertEqual(shop["list_status"], "待处理", "只发现商品不等于跑完榜单")
        self.assertEqual(shop["offer_count"], 3, "已发现商品数包含先前发现的行")

    def test_mark_listing_failure_records_the_callers_reason(self):
        """中断原因由调用方给出：数据层不写死任何一种中断原因的文案。"""
        rid = new_round(self.db)
        self._add_shop(rid)

        self.db.mark_listing_failure(
            rid, "A01", "榜单 deny 超过阈值：店铺 A01 10 分钟内 deny≥7",
        )

        row = self.conn.execute(
            "SELECT list_status, list_note FROM shop_rounds WHERE round_id=? AND shop_key='A01'",
            (rid,),
        ).fetchone()
        self.assertEqual(row["list_status"], "失败")
        self.assertEqual(row["list_note"], "榜单 deny 超过阈值：店铺 A01 10 分钟内 deny≥7")

    def test_remembering_tolerates_pre_existing_duplicate_rows(self):
        """不新增唯一约束：既有库里同商品多行时仍能继续写入，不需要人工迁移。"""
        rid = new_round(self.db)
        self._add_shop(rid)
        self.conn.executemany(
            "INSERT INTO shop_offers(round_id, shop_key, shop_url, shop_name, rank, offer_id, "
            "product_url, list_title, list_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(rid, "A01", "https://a.example/", "店铺A", rank, "11",
              "https://detail.1688.com/offer/11.html", "商品11", "")
             for rank in (1, 2)],
        )
        self.conn.commit()

        self._remember(rid, (1, "11", "https://detail.1688.com/offer/11.html", "商品11", ""))
        self._remember(rid, (2, "22", "https://detail.1688.com/offer/22.html", "商品22", ""))

        rows = self.conn.execute(
            "SELECT COUNT(*) FROM shop_offers WHERE round_id=? AND shop_key='A01'", (rid,)
        ).fetchone()[0]
        self.assertEqual(rows, 3, "历史重复行保留，新商品才新增行")
        count = self.conn.execute(
            "SELECT offer_count FROM shop_rounds WHERE round_id=? AND shop_key='A01'", (rid,)
        ).fetchone()["offer_count"]
        self.assertEqual(count, 2, "已发现商品数按不同商品编号计")

    def test_submit_inventory_snapshot_persists_complete_result(self):
        rid = new_round(self.db)
        self._add_shop(rid)

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
                "sku_id": None,
                "sku_name": "红色 / M",
                "sku_price": 12.5,
                "sku_stock": 0,
            }],
            collected_at="2026-09-04T02:00:00+00:00",
            attempt=2,
        )

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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db, ("A01", "https://a.example/", "店铺A"))
        self.db.save_shop_offers(
            rid, "A01", "https://a.example/", "店铺A",
            [(1, "111", "https://detail.1688.com/offer/111.html", "商品", "")],
            1,
        )
        # 同一天、同一店铺范围才复用同一轮次。
        rid2 = new_round(self.db, ("A01", "https://a.example/", "店铺A"))
        self.assertEqual(rid, rid2)
        self.assertEqual(
            self.db.completed_listing_keys(rid2), {"A01"},  # 已完成店铺不会被重置
        )

    def test_pending_offers_and_attempts(self):
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        tally = self.db.round_tally(rid)
        self.assertEqual((tally.discovered, tally.handled), (1, 1))
        self.assertEqual(tally.failed_offers, 0)

    def test_offer_counts_are_per_offer_not_per_sku(self):
        rid = new_round(self.db)
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
        tally = self.db.round_tally(rid)
        self.assertEqual((tally.discovered, tally.handled), (10, 1))


    def test_round_tally_counts_every_question_in_one_place(self):
        """一轮的计数口径只在一处：商品数按榜单行，成功/跳过算已处理，孤儿单列。"""
        rid = new_round(self.db)
        self._add_shop(rid)
        offers = [
            (i, offer_id, f"https://detail.1688.com/offer/{offer_id}.html", f"商品{offer_id}", "")
            for i, offer_id in enumerate(["11", "22", "33", "44"], start=1)
        ]
        self.db.save_shop_offers(rid, "A01", "https://a.example/", "店铺A", offers, 1)
        # 11 成功（两行 SKU）、22 跳过、33 失败、44 只有榜单行（本轮没抓到）
        self.db.submit_inventory_snapshot(
            round_id=rid, shop_key="A01", shop_url="https://a.example/", shop_name="店铺A",
            offer_id="11", product_url="https://detail.1688.com/offer/11.html",
            list_title="商品11", detail_title="商品11详情", main_image_url=None,
            sku_rows=[{"sku_id": "11:1", "sku_name": "小号", "sku_price": 1.0, "sku_stock": 5},
                      {"sku_id": "11:2", "sku_name": "大号", "sku_price": 2.0, "sku_stock": 7}],
            collected_at="2026-09-04T00:00:00+00:00", attempt=1,
        )
        self.db.mark_skipped(rid, "A01", "https://a.example/", "店铺A", "22",
                             "https://detail.1688.com/offer/22.html", "商品22")
        self.db.mark_failure(rid, "A01", "33", 1, "解析失败")
        # 孤儿：快照里有、榜单行里没有的商品
        self.db.submit_inventory_snapshot(
            round_id=rid, shop_key="A01", shop_url="https://a.example/", shop_name="店铺A",
            offer_id="99", product_url="https://detail.1688.com/offer/99.html",
            list_title="商品99", detail_title="商品99详情", main_image_url=None,
            sku_rows=[{"sku_id": "99:1", "sku_name": "默认", "sku_price": 1.0, "sku_stock": 3}],
            collected_at="2026-09-04T00:00:00+00:00", attempt=1,
        )

        tally = self.db.round_tally(rid)

        self.assertEqual(tally.discovered, 4, "商品数按榜单行去重")
        self.assertEqual(tally.handled, 2, "成功与跳过都算已处理")
        self.assertEqual(tally.failed_offers, 2, "失败 + 只有榜单行没抓到的")
        self.assertEqual(tally.success_offers, 1)
        self.assertEqual(tally.success_skus, 2, "只有榜单行里那个商品的 SKU 行")
        self.assertEqual(tally.orphans, 1, "有快照、无榜单行：单列出来，不算成功商品")

    def test_round_tally_can_be_asked_per_shop(self):
        rid = new_round(self.db, "A01", "A02")
        for key in ("A01", "A02"):
            self.db.add_shop(rid, key, f"https://{key}.example/", f"店铺{key}")
            self.db.save_shop_offers(
                rid, key, f"https://{key}.example/", f"店铺{key}",
                [(1, "11", f"https://detail.1688.com/offer/11.html", "商品", "")], 1)
        self.db.submit_inventory_snapshot(
            round_id=rid, shop_key="A02", shop_url="https://A02.example/", shop_name="店铺A02",
            offer_id="11", product_url="https://detail.1688.com/offer/11.html",
            list_title="商品", detail_title="商品详情", main_image_url=None,
            sku_rows=[{"sku_id": "11:1", "sku_name": "默认", "sku_price": 1.0, "sku_stock": 3}],
            collected_at="2026-09-04T00:00:00+00:00", attempt=1,
        )

        whole = self.db.round_tally(rid)
        first = whole.shop("A01")
        second = whole.shop("A02")

        self.assertEqual((whole.discovered, whole.success_offers), (2, 1))
        self.assertEqual((first.discovered, first.success_offers), (1, 0))
        self.assertEqual((second.discovered, second.success_offers), (1, 1))
        self.assertIsNone(whole.shop("A03"), "本轮没出现过的店铺没有这一片")

    def test_success_snapshot_requires_stock_and_id(self):
        rid = new_round(self.db)
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
        rid = new_round(self.db)
        self._add_shop(rid)
        self._submit_offer(rid, "2026-09-05T02:00:00+00:00", [
            {"sku_id": "a", "sku_name": "小号", "sku_price": 1.0, "sku_stock": 100},
            {"sku_id": "b", "sku_name": "大号", "sku_price": 2.0, "sku_stock": 200},
        ])

        self._submit_offer(rid, "2026-09-05T03:00:00+00:00", [
            {"sku_id": DEFAULT_SKU_ID, "sku_name": DEFAULT_SKU_NAME,
             "sku_price": 1.0, "sku_stock": 300},
        ])

        snapshots, inventory = self._same_day_rows()
        self.assertEqual(snapshots, [DEFAULT_SKU_ID])
        self.assertEqual(inventory, [(DEFAULT_SKU_ID, 300)])

    def test_sku_level_submit_replaces_single_spec_rows_for_same_day(self):
        rid = new_round(self.db)
        self._add_shop(rid)
        self._submit_offer(rid, "2026-09-05T02:00:00+00:00", [
            {"sku_id": DEFAULT_SKU_ID, "sku_name": DEFAULT_SKU_NAME,
             "sku_price": 1.0, "sku_stock": 300},
        ])

        self._submit_offer(rid, "2026-09-05T03:00:00+00:00", [
            {"sku_id": "a", "sku_name": "小号", "sku_price": 1.0, "sku_stock": 100},
        ])

        snapshots, inventory = self._same_day_rows()
        self.assertEqual(snapshots, ["a"])
        self.assertEqual(inventory, [("a", 100)])

    def test_granularity_cleanup_keeps_earlier_date_rows(self):
        # 粒度切换只清当天，更早日期的历史观测保留。
        rid = new_round(self.db)
        self._add_shop(rid)
        self._submit_offer(rid, "2026-09-04T02:00:00+00:00", [
            {"sku_id": "a", "sku_name": "小号", "sku_price": 1.0, "sku_stock": 100},
        ])

        self._submit_offer(rid, "2026-09-05T03:00:00+00:00", [
            {"sku_id": DEFAULT_SKU_ID, "sku_name": DEFAULT_SKU_NAME,
             "sku_price": 1.0, "sku_stock": 300},
        ])

        earlier = [
            tuple(r) for r in self.conn.execute(
                "SELECT sku_id, stock FROM inventory WHERE offer_id='11' AND date='2026-09-04' "
                "ORDER BY sku_id"
            )
        ]
        self.assertEqual(earlier, [("a", 100)])

    def test_failure_with_explicit_metadata_is_recorded_before_listing_write(self):
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
        self.db.save_shop_offers(
            rid, "A", "https://a.example/", "店铺A",
            [(1, "111", "https://detail.1688.com/offer/111.html", "商品", "")], 1,
        )
        self.db.mark_skipped(
            rid, "A", "https://a.example/", "店铺A", "111",
            "https://detail.1688.com/offer/111.html", "商品",
        )
        # 跳过是已处理结果：计入成功、不算失败
        tally = self.db.round_tally(rid)
        self.assertEqual((tally.discovered, tally.handled), (1, 1))
        self.assertEqual(tally.failed_offers, 0)

    def test_skip_is_not_inventory_evidence(self):
        rid = new_round(self.db)
        self.db.mark_skipped(
            rid, "A", "https://a.example/", "店铺A", "111",
            "https://detail.1688.com/offer/111.html", "商品",
        )
        # 跳过不产生库存观测，不能成为同日去重证据
        self.assertFalse(self.db.inventory_exists("A", "111", cst_date()))
        self.assertFalse(self.db.inventory_exists_by_name("A", "商品", cst_date()))

    def test_failed_observation_is_not_dedupe_evidence(self):
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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
        rid = new_round(self.db)
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


class EventIndexTests(unittest.TestCase):
    """事件表要有为过程页刷新建的索引（IS-38）。

    一次刷新里逐店的 deny 计数与时间跨度约占九成：这两条原先各扫一遍本轮全部事件
    （12 店就是 12 遍）。WAL 库 30 万行实测：一次刷新 0.80 秒 → 0.09 秒；代价是事件
    写入（采集热路径）2000 条 17.1 → 20.1 毫秒。
    """

    DENY_SQL = ("SELECT COUNT(*) FROM event_log WHERE round_id=? AND shop_key=? "
                "AND event='click_deny'")
    SPAN_SQL = "SELECT MIN(ts), MAX(ts) FROM event_log WHERE round_id=? AND shop_key=?"

    def _index_names(self, conn) -> set[str]:
        return {row[1] for row in conn.execute("PRAGMA index_list('event_log')")}

    def _insert_event(self, conn, shop_key: str, event: str, ts: str) -> None:
        conn.execute(
            "INSERT INTO event_log (round_id, shop_key, event, ts) VALUES (1, ?, ?, ?)",
            (shop_key, event, ts),
        )
        conn.commit()

    def test_a_fresh_database_has_the_event_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            try:
                self.assertTrue(set(EVENT_REFRESH_INDEXES) <= self._index_names(conn))
            finally:
                conn.close()

    def test_the_names_match_the_schema(self):
        for name in EVENT_REFRESH_INDEXES:
            with self.subTest(name=name):
                self.assertIn(name, SCHEMA, "常量与 SCHEMA 里的 CREATE INDEX 要对得上")

    def test_an_existing_database_gets_them_on_open(self):
        """老库没有这两条索引：下一次开连接要补上，别等人工迁。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            conn = connect(path)
            try:
                for name in EVENT_REFRESH_INDEXES:
                    conn.execute(f"DROP INDEX {name}")
                conn.commit()
            finally:
                conn.close()

            conn = connect(path)
            try:
                self.assertTrue(set(EVENT_REFRESH_INDEXES) <= self._index_names(conn))
            finally:
                conn.close()

    def test_the_refresh_queries_land_on_those_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            try:
                for i in range(50):
                    self._insert_event(
                        conn, "A01", "click_deny" if i % 5 == 0 else "detail_ok",
                        f"2026-09-12T01:00:{i:02d}+08:00",
                    )

                for sql in (self.DENY_SQL, self.SPAN_SQL):
                    with self.subTest(sql=sql):
                        plan = [row[3] for row in conn.execute(
                            "EXPLAIN QUERY PLAN " + sql, (1, "A01"))]
                        self.assertTrue(
                            any(name in step for step in plan for name in EVENT_REFRESH_INDEXES),
                            f"这两条查询要为它们建的索引服务，实际计划：{plan}",
                        )
            finally:
                conn.close()


class SnapshotDedupeMigrationTests(unittest.TestCase):
    """整表去重是给「还没有唯一索引的老库」准备的一次性迁移（IS-38）。

    索引一旦在，重复行不可能再写进来。原先每开一次连接都把整表扫一遍，等于界面每次
    刷新都付一遍全表成本：实测 6 万行 0.020 秒、15 万行 0.051 秒、30 万行 0.107 秒、
    60 万行 0.218 秒，随快照总量线性增长。
    """

    def _legacy_db(self, tmp: str) -> Path:
        """造一个「同一成功快照有重复行、还没有唯一索引」的老库。"""
        path = Path(tmp) / "legacy-duplicates.db"
        raw = sqlite3.connect(path)
        raw.executescript(SCHEMA)
        raw.execute("INSERT INTO rounds(id, started_at) VALUES (1, '2026-09-04T00:00:00+00:00')")
        raw.executemany(
            "INSERT INTO snapshots(round_id, shop_key, shop_url, shop_name, offer_id, "
            "product_url, product_name, sku_id, sku_name, sku_price, sku_stock, collected_at, "
            "page_status, attempt) "
            "VALUES (1, 'A01', 'https://a.example/', '店铺A', '111', 'https://detail/111', "
            "'榜单标题', 'red', '红色', ?, ?, ?, '成功', ?)",
            [(10, 10, "2026-09-04T02:00:00+00:00", 1),
             (12, 2, "2026-09-04T03:00:00+00:00", 2)],
        )
        raw.commit()
        raw.close()
        return path

    def _open_with_report(self, path: Path):
        """开库并拿回这次开库做了什么的报告。"""
        conn = db.open(path)
        return conn, db.migrate(conn)

    def test_first_open_of_a_legacy_database_dedupes_and_builds_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, report = self._open_with_report(self._legacy_db(tmp))
            try:
                self.assertEqual(report.deduped_snapshot_rows, 1,
                                 "缺索引的老库要靠这次去重才建得起唯一索引")
                self.assertEqual(
                    report.created_indexes, (SNAPSHOT_SUCCESS_INDEX, ROUND_TALLY_INDEX),
                    "去重建起唯一索引之后，本轮计数的覆盖索引也一并补上")
                self.assertIn("snapshot_success_index", report.applied)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE round_id=1 AND sku_id='red'"
                ).fetchone()[0], 1)
            finally:
                conn.close()

    def test_second_open_does_not_scan_the_snapshot_table_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._legacy_db(tmp)
            conn, _ = self._open_with_report(path)
            conn.close()

            conn, report = self._open_with_report(path)
            try:
                self.assertIsNone(report.deduped_snapshot_rows,
                                  "唯一索引已在，重复行不可能写进来：这次开库不该再扫整表")
                self.assertEqual(report.created_indexes, ())
                self.assertEqual(report.applied, (), "没有一段迁移需要动手")
                indexes = [row[1] for row in conn.execute("PRAGMA index_list('snapshots')")]
                self.assertIn(SNAPSHOT_SUCCESS_INDEX, indexes, "闸门不能把索引本身也带掉")
            finally:
                conn.close()

    def test_a_fresh_database_reports_the_index_it_built(self):
        """全新的库也要如实报告：去重跑了但一行没删，索引是这次建的。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn, report = self._open_with_report(Path(tmp) / "fresh.db")

            try:
                self.assertEqual(report.deduped_snapshot_rows, 0)
                self.assertEqual(
                    report.created_indexes, (SNAPSHOT_SUCCESS_INDEX, ROUND_TALLY_INDEX),
                    "新库也在这次开库里建起本轮计数的覆盖索引")
            finally:
                conn.close()


class SkusPrimaryKeyMigrationTests(unittest.TestCase):
    """旧 skus 主键 (offer_id, sku_name) 要迁到 (offer_id, sku_id)；迁不动时不许把库卡住。"""

    @staticmethod
    def _old_skus_db(tmp: str, *, leftover: bool = False) -> Path:
        path = Path(tmp) / "old-skus.db"
        raw = sqlite3.connect(path)
        raw.executescript(
            "CREATE TABLE skus ("
            " offer_id TEXT NOT NULL, sku_name TEXT, sku_id TEXT NOT NULL, "
            " first_seen_at TEXT NOT NULL, last_seen_at TEXT, "
            " PRIMARY KEY (offer_id, sku_name));"
        )
        raw.executemany(
            "INSERT INTO skus(offer_id, sku_name, sku_id, first_seen_at, last_seen_at) "
            "VALUES ('111', ?, 'red', '2026-09-01T00:00:00+00:00', '2026-09-02T00:00:00+00:00')",
            [("规格A",), ("规格B",)],
        )
        if leftover:
            # 上一次迁移半途留下的表：RENAME 到 skus_old 会撞名，这一段只能跳过。
            raw.executescript("CREATE TABLE skus_old (offer_id TEXT)")
        raw.commit()
        raw.close()
        return path

    def test_an_old_primary_key_is_rebuilt_and_the_rows_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(self._old_skus_db(tmp))
            try:
                pk = [row[1] for row in conn.execute('PRAGMA table_info("skus")') if row[5] > 0]
                self.assertEqual(pk, ["offer_id", "sku_id"])
                rows = conn.execute(
                    "SELECT sku_name FROM skus WHERE offer_id='111' AND sku_id='red'").fetchall()
                self.assertEqual([row[0] for row in rows], ["规格B"],
                                 "同一个 SKU 编号的多行按名字取最大合并成一行")
            finally:
                conn.close()

    def test_a_leftover_skus_old_table_does_not_break_opening(self):
        """迁不动就跳过：这一段失败不该让整个库打不开（改前就是静默跳过）。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(self._old_skus_db(tmp, leftover=True))

            try:
                pk = [row[1] for row in conn.execute('PRAGMA table_info("skus")') if row[5] > 0]
                self.assertEqual(pk, ["offer_id", "sku_name"], "这一段被跳过，表保持原样")
            finally:
                conn.close()


class CrawlerIdentityTests(unittest.TestCase):
    """采集进程的身份行：同一时刻至多一行，进程走了就该清掉（工单 02）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.db = Database(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _row_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM crawler_process").fetchone()[0]

    def test_nothing_is_recorded_before_a_crawler_starts(self):
        self.assertIsNone(self.db.crawler_process())

    def test_recording_twice_keeps_one_row_with_the_latest_pid(self):
        rid = new_round(self.db)
        self.db.record_crawler_process(pid=4101, round_id=rid, note="run.py")
        self.db.record_crawler_process(pid=4102, round_id=rid, note="run.py")

        row = self.db.crawler_process()
        self.assertEqual(row["pid"], 4102)
        self.assertEqual(row["round_id"], rid)
        self.assertEqual(self._row_count(), 1)
        self.assertTrue(row["started_at"], "身份行要记下起点，界面才能显示")

    def test_clearing_removes_the_identity(self):
        rid = new_round(self.db)
        self.db.record_crawler_process(pid=4101, round_id=rid)

        self.db.clear_crawler_process()

        self.assertIsNone(self.db.crawler_process())
        self.assertEqual(self._row_count(), 0)


if __name__ == "__main__":
    unittest.main()
