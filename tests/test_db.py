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
    SKU_IMAGE_DEDUPE_INDEX,
    SKU_IMAGE_FILLED,
    SKU_IMAGE_NONE,
    SKU_IMAGE_OWN,
    SNAPSHOT_SUCCESS_INDEX,
    ShopTally,
    VERSION_DEDUPE_INDEX,
    connect,
    cst_date,
)
from bestseller_monitor import db
from helpers import new_round, product_picture
from bestseller_monitor.parse import DEFAULT_SKU_ID, DEFAULT_SKU_NAME


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        self.db = Database(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_a_fresh_db_has_no_round_phase_column(self):
        """建出来的库没有 `rounds.phase`：从没人读它，而 `terminal_reason` 严格更强。

        老库留着那一列，代码既不读也不写——两种库都跑得动（见规格的兼容核对）。
        """
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(rounds)")}

        self.assertNotIn("phase", columns)

    def test_a_fresh_db_has_no_inventory_diff_column(self):
        """建出来的库没有 `inventory.diff`（ADR-0032）：它没有任何生产读方，
        多机分片后基准也不再成立——口径随列一并退役。老库由迁移删列。"""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(inventory)")}

        self.assertNotIn("diff", columns)

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

    def test_submit_inventory_snapshot_rejects_malformed_image_evidence_before_transaction(self):
        """图证据不是带来源的字典：在事务前当场拒绝（同其它入参校验），不落到「写库时被
        NOT NULL 打回、整单库存回滚」那条路上——图是附加证据，坏证据不该带走一次观测。"""
        rid = new_round(self.db)
        self._add_shop(rid)
        base = dict(
            round_id=rid, shop_key="A01", shop_url="https://a.example/", shop_name="店铺A",
            offer_id="111", product_url="https://detail.1688.com/offer/111.html",
            list_title="榜单标题", detail_title="详情标题", main_image_url=None,
            sku_rows=[{"sku_id": "red", "sku_name": "红色", "sku_price": 10, "sku_stock": 10,
                       "sku_image_evidence": {"url": None}}],
            collected_at="2026-09-04T02:00:00+00:00", attempt=1,
        )
        with self.assertRaisesRegex(ValueError, "SKU 图证据必须带来源"):
            self.db.submit_inventory_snapshot(**base)
        base["sku_rows"] = [{"sku_id": "red", "sku_name": "红色", "sku_price": 10,
                             "sku_stock": 10, "sku_image_evidence": ["不是字典"]}]
        with self.assertRaisesRegex(ValueError, "SKU 图证据必须带来源"):
            self.db.submit_inventory_snapshot(**base)
        base["sku_rows"] = [{"sku_id": "red", "sku_name": "红色", "sku_price": 10,
                             "sku_stock": 10,
                             "sku_image_evidence": {"url": "https://img/x", "source": SKU_IMAGE_OWN,
                                                    "content": b"bytes"}}]
        with self.assertRaisesRegex(ValueError, "带字节时必须带哈希与图片类型"):
            self.db.submit_inventory_snapshot(**base)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM sku_image_versions").fetchone()[0], 0)

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
        self.assertEqual(whole.shop("A03"), ShopTally(shop_key="A03"),
                         "本轮没出现过的店铺给零计数，调用方不用自己补默认")

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

    def test_submit_inventory_upserts_previous_days_and_updates_same_day(self):
        """库存是纯 upsert：不同日期各一行、同日覆盖（值更新、不添行）。

        写路径不再为算差分回查本表——按实测 2479 行/天，每天少约 2500 次查询（ADR-0032）。
        """
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
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        try:
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
        finally:
            self.conn.set_trace_callback(None)
        rows = self.conn.execute(
            "SELECT date, stock FROM inventory WHERE shop_key='A' AND offer_id='11' ORDER BY date"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [("2026-09-04", 200), ("2026-09-05", 180)],
                         "同日第二次观测覆盖同一行；更早日期那一行不动")
        reads = [sql for sql in statements
                 if sql.lstrip().upper().startswith("SELECT") and "inventory" in sql.lower()]
        self.assertEqual(reads, [], "写路径不该再为差分逐行回查本表（ADR-0032）")

    def test_submit_twice_in_the_same_second_keeps_one_version_row(self):
        """同一秒里、同一图片结果的同一次观测只留一条版本行（去重键见 VERSION_DEDUPE_INDEX）。

        本机写路径与导入侧同走 INSERT OR IGNORE：重复提交不再撞唯一索引把整单回滚。
        """
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
            collected_at="2026-09-05T02:00:00+00:00",
            attempt=1,
        )
        self.db.submit_inventory_snapshot(
            **base,
            sku_rows=[{"sku_id": "s1", "sku_name": "S", "sku_price": 1.0, "sku_stock": 160}],
        )
        self.db.submit_inventory_snapshot(
            **{**base, "attempt": 2},
            sku_rows=[{"sku_id": "s1", "sku_name": "S", "sku_price": 1.0, "sku_stock": 180}],
        )

        versions = self.conn.execute(
            "SELECT observed_at, content_hash, image_error FROM product_information_versions "
            "WHERE shop_key='A' AND offer_id='11'"
        ).fetchall()
        self.assertEqual(len(versions), 1, "同一次观测只留一条版本行")
        inventory = self.conn.execute(
            "SELECT stock FROM inventory WHERE shop_key='A' AND offer_id='11' AND sku_id='s1'"
        ).fetchone()
        self.assertEqual(inventory["stock"], 180, "库存照常被后一次观测覆盖")

    def test_submit_twice_in_the_same_second_keeps_one_sku_image_row_per_sku(self):
        """SKU 图流水照版本行的折叠方式去重（票 02）：同一秒、同一图片结果的重复提交
        每 SKU 只留一行；无图行两列都是 NULL，靠去重键里的 COALESCE 折成一条。"""
        rid = new_round(self.db)
        blue, main = product_picture('blue'), product_picture('red')
        rows = [
            {"sku_id": "own", "sku_name": "蓝", "sku_stock": 1,
             "sku_image_evidence": {"url": "https://img.example/blue", "source": SKU_IMAGE_OWN,
                                    "hash": blue["hash"], "mime": blue["mime"],
                                    "content": blue["content"]}},
            {"sku_id": "filled", "sku_name": "素色", "sku_stock": 1,
             "sku_image_evidence": {"url": None, "source": SKU_IMAGE_FILLED}},
            {"sku_id": "blank", "sku_name": "随机", "sku_stock": 1,
             "sku_image_evidence": {"url": None, "source": SKU_IMAGE_NONE}},
        ]
        base = dict(
            round_id=rid, shop_key="A", shop_url="https://a.example/", shop_name="店铺A",
            offer_id="11", product_url="https://a/offer/11.html", list_title="商品",
            detail_title="商品详情", main_image_url="https://img.example/main",
            collected_at="2026-09-05T02:00:00+00:00", attempt=1, image_evidence=main,
        )
        self.db.submit_inventory_snapshot(**base, sku_rows=rows)
        self.db.submit_inventory_snapshot(**{**base, "attempt": 2}, sku_rows=rows)

        ledger = {row["sku_id"]: row for row in self.conn.execute(
            "SELECT * FROM sku_image_versions WHERE shop_key='A' AND offer_id='11'")}
        self.assertEqual(set(ledger), {"own", "filled", "blank"},
                         "重复提交每 SKU 折叠成一行")
        self.assertEqual(ledger["own"]["content_hash"], blue["hash"])
        self.assertEqual(ledger["own"]["observed_date"], "2026-09-05")
        self.assertEqual(ledger["filled"]["content_hash"], main["hash"],
                         "代填行的哈希就是当次真正落池的主图")
        self.assertIsNone(ledger["blank"]["content_hash"])
        self.assertIsNone(ledger["blank"]["image_error"])
        assets = {row[0] for row in
                  self.conn.execute("SELECT content_hash FROM product_image_assets")}
        self.assertEqual(assets, {main["hash"], blue["hash"]}, "同哈希不重复存")

    def test_a_fill_whose_main_image_did_not_reach_the_pool_is_recorded_as_no_image(self):
        """代填认的是真正落池的主图：这次主图校验没过（调用方给了坏内容）就没有可代填的
        字节，那一行如实记成无图——流水行的哈希必须指向池里有的字节（外键），
        且这一点绝不把库存提交整单带回去。"""
        rid = new_round(self.db)
        self.db.submit_inventory_snapshot(
            round_id=rid, shop_key="A", shop_url="https://a.example/", shop_name="店铺A",
            offer_id="11", product_url="https://a/offer/11.html", list_title="商品",
            detail_title="商品详情", main_image_url="https://img.example/main",
            sku_rows=[{"sku_id": "s1", "sku_name": "素色", "sku_stock": 1,
                       "sku_image_evidence": {"url": None, "source": SKU_IMAGE_FILLED}}],
            collected_at="2026-09-05T02:00:00+00:00", attempt=1,
            image_evidence={"content": b"not an image"},
        )

        row = self.conn.execute("SELECT * FROM sku_image_versions").fetchone()
        self.assertEqual(row["source"], SKU_IMAGE_NONE)
        self.assertIsNone(row["content_hash"])
        self.assertIsNone(row["image_error"])
        version = self.conn.execute(
            "SELECT * FROM product_information_versions").fetchone()
        self.assertIsNotNone(version["image_error"], "主图这次没落池的事实记在主图版本行上")
        stock = self.conn.execute(
            "SELECT stock FROM inventory WHERE shop_key='A' AND offer_id='11'").fetchone()
        self.assertEqual(stock["stock"], 1, "库存照常提交")

    def test_rows_without_image_evidence_write_no_ledger_row(self):
        """没带图证据的提交不写流水：流水记的是采集的图片观测，没观测过就不编造行
        （只有采集路径才逐 SKU 带证据）。"""
        rid = new_round(self.db)
        self.db.submit_inventory_snapshot(
            round_id=rid, shop_key="A", shop_url="https://a.example/", shop_name="店铺A",
            offer_id="11", product_url="https://a/offer/11.html", list_title="商品",
            detail_title="商品详情", main_image_url=None,
            sku_rows=[{"sku_id": "s1", "sku_name": "S", "sku_stock": 1}],
            collected_at="2026-09-05T02:00:00+00:00", attempt=1,
        )
        count = self.conn.execute("SELECT COUNT(*) FROM sku_image_versions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_retry_refuses_a_sku_image_row_that_is_not_the_latest(self):
        """守卫同主图规（票 03）：那条（店铺、商品、SKU）上已有更新的 SKU 图版本时，
        过期的失败行不被重试——当场拒绝，文案照主图「已有更新版本」的形状。"""
        rid = new_round(self.db)
        blue = product_picture('blue')
        failed = {"url": "https://img.example/blue", "source": SKU_IMAGE_OWN,
                  "error": "offline"}
        own = {"url": "https://img.example/blue", "source": SKU_IMAGE_OWN,
               "hash": blue["hash"], "mime": blue["mime"], "content": blue["content"]}
        self._submit_offer(rid, "2026-09-05T02:00:00+00:00", [
            {"sku_id": "s1", "sku_name": "蓝", "sku_stock": 1, "sku_image_evidence": failed}])
        failed_id = self.conn.execute(
            "SELECT MAX(id) FROM sku_image_versions").fetchone()[0]
        self._submit_offer(rid, "2026-09-06T02:00:00+00:00", [
            {"sku_id": "s1", "sku_name": "蓝", "sku_stock": 1, "sku_image_evidence": own}])

        with patch("bestseller_monitor.product_images.acquire", return_value=blue):
            with self.assertRaises(ValueError) as ctx:
                self.db.retry_sku_image(failed_id)
        self.assertEqual(str(ctx.exception), "已有更新 SKU 图版本，请重试最新失败版本")
        count = self.conn.execute("SELECT COUNT(*) FROM sku_image_versions").fetchone()[0]
        self.assertEqual(count, 2, "拒绝时不新开行")

    def test_retrying_the_latest_failed_sku_image_opens_a_version_at_retry_time(self):
        """重试成功是重试当下的新证据（票 03）：按重试时刻新开一行、字节进资产池、
        来源仍是专属图；原失败行原样保留——不回填、不覆盖那次失败的事实。下载走的是
        失败行里存的那个地址（替身只认它，换地址就记成失败）。"""
        rid = new_round(self.db)
        blue = product_picture('blue')
        failed = {"url": "https://img.example/blue", "source": SKU_IMAGE_OWN,
                  "error": "offline"}
        self._submit_offer(rid, "2026-09-05T02:00:00+00:00", [
            {"sku_id": "s1", "sku_name": "蓝", "sku_stock": 1, "sku_image_evidence": failed}])
        failed_id = self.conn.execute(
            "SELECT MAX(id) FROM sku_image_versions").fetchone()[0]

        with patch("bestseller_monitor.product_images.acquire",
                   side_effect=lambda url: blue if url == "https://img.example/blue"
                   else {"error": f"意外的地址：{url}"}), \
             patch("bestseller_monitor.db.utcnow",
                   return_value="2026-09-18T04:00:00+00:00"):
            retried = self.db.retry_sku_image(failed_id)

        rows = self.conn.execute("SELECT * FROM sku_image_versions ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2, "原失败行 + 重试新开的一行")
        new = rows[1]
        self.assertEqual(retried, {"retried_sku_image": failed_id,
                                   "new_version": new["id"], "image_error": None})
        self.assertEqual(new["observed_at"], "2026-09-18T04:00:00+00:00")
        self.assertEqual(new["observed_date"], "2026-09-18", "观测日期=重试当下，不回填")
        self.assertEqual(new["image_url"], "https://img.example/blue", "地址照抄原失败行")
        self.assertEqual(new["content_hash"], blue["hash"])
        self.assertIsNone(new["image_error"])
        self.assertEqual(new["source"], SKU_IMAGE_OWN)
        failed = rows[0]
        self.assertEqual(failed["image_error"], "offline", "原失败行保留")
        self.assertIsNone(failed["content_hash"])
        self.assertEqual(failed["observed_date"], "2026-09-05")
        assets = {row[0] for row in
                  self.conn.execute("SELECT content_hash FROM product_image_assets")}
        self.assertEqual(assets, {blue["hash"]}, "重试取到的字节进资产池")

    def test_retrying_a_sku_image_touches_nothing_but_the_image(self):
        """重试只碰图（票 03）：库存行、快照、图片版本行、轮次与详情机会账都原样不动。
        库存不重抓、历史日期不回填、失败率的输入不变、不占详情重试账。"""
        rid = new_round(self.db)
        blue = product_picture('blue')
        self._submit_offer(rid, "2026-09-05T02:00:00+00:00", [
            {"sku_id": "s1", "sku_name": "蓝", "sku_stock": 7,
             "sku_image_evidence": {"url": "https://img.example/blue",
                                    "source": SKU_IMAGE_OWN, "error": "offline"}}])
        self.db.add_detail_opportunity(rid, "A01", "identity-1")
        failed_id = self.conn.execute(
            "SELECT MAX(id) FROM sku_image_versions").fetchone()[0]
        tables = ("inventory", "snapshots", "product_information_versions",
                  "detail_opportunities", "rounds")
        before = {table: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table}")]
                  for table in tables}
        tally_before = self.db.round_tally(rid)

        with patch("bestseller_monitor.product_images.acquire", return_value=blue), \
             patch("bestseller_monitor.db.utcnow",
                   return_value="2026-09-18T04:00:00+00:00"):
            self.db.retry_sku_image(failed_id)

        for table in tables:
            self.assertEqual(
                [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table}")],
                before[table], f"重试不该动 {table}")
        self.assertEqual(self.db.round_tally(rid), tally_before, "失败率的输入不变")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM sku_image_versions").fetchone()[0], 2,
            "重试本身照常落一行——上面的「不动」不是因为它没跑")

    def test_only_failed_sku_image_rows_can_be_retried(self):
        """重试对象只能是失败行（票 03）：没失败过的行（这里是空图行）不重下——
        这条通道对着的永远是记账为失败的那些行。"""
        rid = new_round(self.db)
        self._submit_offer(rid, "2026-09-05T02:00:00+00:00", [
            {"sku_id": "s1", "sku_name": "随机", "sku_stock": 1,
             "sku_image_evidence": {"url": None, "source": SKU_IMAGE_NONE}}])
        row_id = self.conn.execute(
            "SELECT MAX(id) FROM sku_image_versions").fetchone()[0]

        with patch("bestseller_monitor.product_images.acquire",
                   side_effect=AssertionError("没失败过的行不该被重下")):
            with self.assertRaises(ValueError) as ctx:
                self.db.retry_sku_image(row_id)
        self.assertEqual(str(ctx.exception), "只能重试失败的 SKU 图版本")

    def test_retrying_a_sku_image_twice_in_a_second_reuses_the_row_already_recorded(self):
        """重试也是流水行写者（票 03，照主图先例）：同一秒里、同一图片结果的重试不撞键
        （SKU_IMAGE_DEDUPE_INDEX）——那次观测已在案，结果指回已有那一行。连败两次的
        重试如实记成失败行：不补库存、不改原失败行。"""
        rid = new_round(self.db)
        failed = {"url": "https://img.example/blue", "source": SKU_IMAGE_OWN,
                  "error": "offline"}
        self._submit_offer(rid, "2026-09-05T02:00:00+00:00", [
            {"sku_id": "s1", "sku_name": "蓝", "sku_stock": 1, "sku_image_evidence": failed}])
        failed_id = self.conn.execute(
            "SELECT MAX(id) FROM sku_image_versions").fetchone()[0]

        with patch("bestseller_monitor.product_images.acquire",
                   return_value={"error": "offline"}), \
             patch("bestseller_monitor.db.utcnow",
                   return_value="2026-09-18T04:00:00+00:00"):
            first = self.db.retry_sku_image(failed_id)
            second = self.db.retry_sku_image(first["new_version"])

        self.assertEqual(second["new_version"], first["new_version"],
                         "同一秒内的重复重试指回同一行")
        self.assertEqual(second["image_error"], "offline")
        rows = self.conn.execute("SELECT * FROM sku_image_versions ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2, "原始失败行 + 一次重试行")
        self.assertEqual(rows[1]["source"], SKU_IMAGE_OWN)
        self.assertEqual(rows[1]["image_error"], "offline", "又败一次也是失败行")
        self.assertIsNone(rows[1]["content_hash"])

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
            pause_every_detail_visits = 180
            pause_sec = (1500.0, 2100.0)
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
            ("C", "click_deny", "page=2&idx=0&n=3&skip"),
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

    def test_a_card_that_reached_a_product_is_not_counted_here(self):
        """拿到了商品编号但读不出来的卡：失败已由快照计入 failed_offers，这里不再算一次。"""
        rid = new_round(self.db)
        evs = [
            # 旧名，就是库里轮次 10–12 那 9 条的写法：读侧要认得它，但同样不算卡片失败。
            ("A", "click_parse_empty", "page=1&idx=0&offer_id=7"),
            ("A", "click_parse_error", "page=2&idx=0&offer_id=8"),
            # 同一张卡先没打开、后拿到编号却读不出来：卡片口径仍记它失败一次。
            ("B", "click_no_popup", "page=1&idx=0"),
            ("B", "click_parse_error", "page=1&idx=0&offer_id=5"),
        ]
        for shop, ev, note in evs:
            self.db.conn.execute(
                "INSERT INTO event_log(round_id, shop_key, event, ts, note, kind) "
                "VALUES (?, ?, ?, ?, ?, 'work')",
                (rid, shop, ev, "2026-09-05T00:00:00+00:00", note),
            )
        self.db.conn.commit()

        self.assertEqual(self.db.click_card_failures(rid), 1)

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

    一次刷新里逐店的 deny 计数与时间跨度原先各扫一遍本轮全部事件（12 店就是 12 遍）。
    WAL 库 30 万行实测：一次刷新 0.80 秒 → 0.117 秒；次日 55a51ce 把计数口径统一到
    round_tally 后刷新整体约 0.5 秒（大头是 tally，不是这两条索引）。代价是事件写入
    （采集热路径）2000 条 17.1 → 20.1 毫秒。
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
                    report.created_indexes,
                    (SNAPSHOT_SUCCESS_INDEX, ROUND_TALLY_INDEX, VERSION_DEDUPE_INDEX,
                     SKU_IMAGE_DEDUPE_INDEX),
                    "去重建起唯一索引之后，本轮计数的覆盖索引、版本表与 SKU 图去重索引也一并补上")
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
                    report.created_indexes,
                    (SNAPSHOT_SUCCESS_INDEX, ROUND_TALLY_INDEX, VERSION_DEDUPE_INDEX,
                     SKU_IMAGE_DEDUPE_INDEX),
                    "新库也在这次开库里建起本轮计数的覆盖索引、版本表与 SKU 图去重索引")
            finally:
                conn.close()


class InventoryDiffRetirementTests(unittest.TestCase):
    """票据 01：`inventory.diff` 退役 + 版本表汇总导入去重索引（ADR-0032、spec §9 §10）。

    每台机器升级后首次开库自动、幂等地做完这两件事；重开不再付成本。
    全新库只建表：没有 diff 列要删（那一段不出现），去重索引随这次开库建起
    ——与 snapshot_success_index 同款，新库也如实报告自己建了索引。
    """

    @staticmethod
    def _legacy_db(tmp: str) -> Path:
        """造一个「inventory 还带着 diff 列、版本表还没有去重索引」的老库。"""
        path = Path(tmp) / "legacy-diff.db"
        raw = sqlite3.connect(path)
        raw.executescript(SCHEMA)
        raw.execute("ALTER TABLE inventory ADD COLUMN diff INTEGER")
        raw.executemany(
            "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, diff, price, "
            "shop_name, product_name, sku_name) VALUES "
            "('A', '11', 's1', ?, ?, ?, 1.0, '店铺A', '商品', 'S')",
            [("2026-09-04", 200, None), ("2026-09-05", 180, -20)],
        )
        raw.commit()
        raw.close()
        return path

    @staticmethod
    def _open_with_report(path: Path):
        conn = db.open(path)
        return conn, db.migrate(conn)

    def test_first_open_drops_the_column_and_builds_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, report = self._open_with_report(self._legacy_db(tmp))
            try:
                columns = {row[1] for row in conn.execute('PRAGMA table_info("inventory")')}
                self.assertNotIn("diff", columns)
                rows = conn.execute("SELECT date, stock FROM inventory ORDER BY date").fetchall()
                self.assertEqual([tuple(row) for row in rows],
                                 [("2026-09-04", 200), ("2026-09-05", 180)],
                                 "删列只动列，不动行数据")
                self.assertIn("drop_inventory_diff", report.applied)
                self.assertIn("version_dedupe_index", report.applied)
                self.assertIn(VERSION_DEDUPE_INDEX, report.created_indexes)
                indexes = {row[1] for row in
                           conn.execute('PRAGMA index_list("product_information_versions")')}
                self.assertIn(VERSION_DEDUPE_INDEX, indexes)
            finally:
                conn.close()

    def test_second_open_pays_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._legacy_db(tmp)
            conn, _ = self._open_with_report(path)
            conn.close()

            conn, report = self._open_with_report(path)
            try:
                self.assertEqual(report.applied, (), "没有一段迁移需要动手")
                self.assertEqual(report.created_indexes, ())
            finally:
                conn.close()

    def test_a_fresh_library_only_builds_tables(self):
        """全新库只建表：没有 diff 列要删；去重索引随第一次开库建起。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn, report = self._open_with_report(Path(tmp) / "fresh.db")
            try:
                self.assertNotIn("drop_inventory_diff", report.applied,
                                 "全新库没有 diff 列，那一段不该动手")
                self.assertIn(VERSION_DEDUPE_INDEX, report.created_indexes,
                              "新库也如实报告自己建了这条索引")
                columns = {row[1] for row in conn.execute('PRAGMA table_info("inventory")')}
                self.assertNotIn("diff", columns, "建表即新形态")
            finally:
                conn.close()

    def test_the_dedupe_index_folds_null_image_columns(self):
        """去重键把 content_hash/image_error 的空值折进表达式：图片失败的行（两列都是
        NULL）也要能去重——只写 UNIQUE(..., content_hash) 会漏掉它们，
        SQLite 唯一索引里 NULL 互不相等（spec §9）。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "fresh.db")
            try:
                insert = (
                    "INSERT OR IGNORE INTO product_information_versions "
                    "(shop_key, offer_id, observed_at, observed_date, product_name, "
                    "image_url, content_hash, image_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                )
                failed_image = ("A", "11", "2026-09-05T02:00:00+00:00", "2026-09-05",
                                "商品", "https://img/11.jpg", None, None)
                conn.execute(insert, failed_image)
                conn.execute(insert, failed_image)
                conn.commit()
                count = conn.execute(
                    "SELECT COUNT(*) FROM product_information_versions").fetchone()[0]
                self.assertEqual(count, 1, "同一次观测的图片失败行只留一条")

                conn.execute(insert, ("A", "11", "2026-09-05T03:00:00+00:00",
                                      "2026-09-05", "商品", "https://img/11.jpg", None, None))
                conn.commit()
                count = conn.execute(
                    "SELECT COUNT(*) FROM product_information_versions").fetchone()[0]
                self.assertEqual(count, 2, "同一店铺商品的另一次观测是另一条")
            finally:
                conn.close()

    def test_a_legacy_library_with_duplicate_version_keys_still_opens(self):
        """老库版本表若已有重复键（正是本票前写路径能造出来的），UNIQUE 建不上：
        这一段跳过并告警，库照开——一次迁移不该把库卡在打不开的状态。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy-dup-versions.db"
            raw = sqlite3.connect(path)
            raw.executescript(SCHEMA)
            raw.executemany(
                "INSERT INTO product_information_versions(shop_key, offer_id, observed_at, "
                "observed_date, product_name, image_url, content_hash, image_error) "
                "VALUES ('A', '11', '2026-09-05T02:00:00+00:00', '2026-09-05', '商品', "
                "'https://img/11.jpg', NULL, 'offline')",
                [(), ()],
            )
            raw.commit()
            raw.close()

            conn, report = self._open_with_report(path)
            try:
                self.assertNotIn(VERSION_DEDUPE_INDEX, report.created_indexes)
                self.assertNotIn("version_dedupe_index", report.applied)
                indexes = {row[1] for row in
                           conn.execute('PRAGMA index_list("product_information_versions")')}
                self.assertNotIn(VERSION_DEDUPE_INDEX, indexes, "建不上就跳过，重开再试")
            finally:
                conn.close()

    def test_retry_in_the_same_second_reuses_the_observation_already_recorded(self):
        """重试也是版本行写者：同一秒、同一图片结果的重试不撞键——那次观测已在案，
        结果指回已有那一行（与提交路径同走 INSERT OR IGNORE）。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "fresh.db")
            database = Database(conn)
            try:
                rid = new_round(database)
                database.submit_inventory_snapshot(
                    round_id=rid, shop_key="A", shop_url="https://a.example/",
                    shop_name="店铺A", offer_id="11", product_url="https://a/offer/11.html",
                    list_title="商品", detail_title="商品详情",
                    main_image_url="https://img/11.jpg",
                    sku_rows=[{"sku_id": "s1", "sku_name": "S", "sku_price": 1.0,
                               "sku_stock": 5}],
                    collected_at="2026-09-05T02:00:00+00:00", attempt=1,
                    image_evidence={"error": "offline"},
                )
                failed_id = conn.execute(
                    "SELECT MAX(id) FROM product_information_versions").fetchone()[0]
                with patch("bestseller_monitor.product_images.acquire",
                           return_value={"error": "offline"}), \
                     patch("bestseller_monitor.db.utcnow",
                           return_value="2026-09-18T04:00:00+00:00"):
                    first = database.retry_product_image(failed_id)
                    second = database.retry_product_image(first["new_version"])
                self.assertEqual(second["new_version"], first["new_version"],
                                 "同一秒内的重复重试指回同一行")
                count = conn.execute(
                    "SELECT COUNT(*) FROM product_information_versions").fetchone()[0]
                self.assertEqual(count, 2, "原始失败行 + 一次重试行")
            finally:
                conn.close()

    def test_the_old_program_hard_fails_writing_the_new_library(self):
        """程序与库必须一起升（ADR-0032）：旧程序的 INSERT 里还写着 diff，打在新库上
        当场报 `table inventory has no column named diff`，不再静默降级。

        消息取证于 2026-09-20（SQLite 3.50.4 / Python 3.14.7，生产库迁移前的副本上复现）；
        这条用例是随代码库常驻的回归守卫。
        """
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "fresh.db")
            try:
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    conn.execute(
                        "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, diff, price, "
                        "shop_name, product_name, sku_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(shop_key, offer_id, sku_id, date) DO UPDATE SET "
                        "stock=excluded.stock, diff=excluded.diff, price=excluded.price, "
                        "shop_name=excluded.shop_name, product_name=excluded.product_name, "
                        "sku_name=excluded.sku_name",
                        ("A", "11", "s1", "2026-09-05", 180, -20, 1.0, "店铺A", "商品", "S"),
                    )
                self.assertEqual(str(ctx.exception),
                                 "table inventory has no column named diff")
            finally:
                conn.close()


class SkuImageLedgerMigrationTests(unittest.TestCase):
    """票 02：SKU 图流水走既有「连接即迁移」——老库开库自动获得空表与去重索引，
    不需要任何手工动作；索引建不动（老库已有重复键）就跳过，库照开、下次再试。"""

    @staticmethod
    def _open_with_report(path: Path):
        conn = db.open(path)
        return conn, db.migrate(conn)

    @staticmethod
    def _library_without_the_ledger(tmp: str) -> Path:
        """造一个本线之前的老库：建表（不跑迁移）后把流水表删掉，当作它从未存在过。"""
        path = Path(tmp) / "legacy-no-ledger.db"
        raw = sqlite3.connect(path)
        raw.executescript(SCHEMA)
        raw.execute("DROP TABLE sku_image_versions")
        raw.commit()
        raw.close()
        return path

    def test_an_old_library_gains_the_ledger_table_and_its_index_on_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, report = self._open_with_report(self._library_without_the_ledger(tmp))
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM sku_image_versions").fetchone()[0]
                self.assertEqual(count, 0, "老库开库自动获得空表")
                self.assertIn("sku_image_dedupe_index", report.applied)
                self.assertIn(SKU_IMAGE_DEDUPE_INDEX, report.created_indexes)
                indexes = {row[1] for row in
                           conn.execute('PRAGMA index_list("sku_image_versions")')}
                self.assertIn(SKU_IMAGE_DEDUPE_INDEX, indexes)
            finally:
                conn.close()

    def test_a_legacy_library_with_duplicate_ledger_keys_still_opens(self):
        """老库流水表若已有重复键（正是本票之前没有索引时的形态），UNIQUE 建不上：
        跳过并告警，库照开、行数据不动——一次迁移不该把库卡在打不开的状态。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy-dup-ledger.db"
            raw = sqlite3.connect(path)
            raw.executescript(SCHEMA)
            raw.executemany(
                "INSERT INTO sku_image_versions(shop_key, offer_id, sku_id, observed_at, "
                "observed_date, image_url, content_hash, image_error, source) "
                "VALUES ('A', '11', 's1', '2026-09-05T02:00:00+00:00', '2026-09-05', "
                "NULL, NULL, NULL, ?)",
                [(SKU_IMAGE_NONE,), (SKU_IMAGE_NONE,)],
            )
            raw.commit()
            raw.close()

            conn, report = self._open_with_report(path)
            try:
                self.assertNotIn("sku_image_dedupe_index", report.applied)
                indexes = {row[1] for row in
                           conn.execute('PRAGMA index_list("sku_image_versions")')}
                self.assertNotIn(SKU_IMAGE_DEDUPE_INDEX, indexes, "建不上就跳过，重开再试")
                count = conn.execute(
                    "SELECT COUNT(*) FROM sku_image_versions").fetchone()[0]
                self.assertEqual(count, 2, "跳过只意味着不建索引，行数据不动")
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
