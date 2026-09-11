"""对账脚本：报出「有快照、无榜单行」的孤儿与完成态计数不符的店铺。"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bestseller_monitor.db import Database, connect, utcnow
from tools import check_orphans


class CheckOrphansTests(unittest.TestCase):
    def test_reports_snapshot_without_listing_row(self):
        """中断后只留下快照的店铺，其商品要被报成孤儿。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            try:
                db = Database(conn)
                rid = db.start_or_resume()
                db.add_shop(rid, "A01", "https://a.example/", "店铺A")
                db.submit_inventory_snapshot(
                    round_id=rid,
                    shop_key="A01",
                    shop_url="https://a.example/",
                    shop_name="店铺A",
                    offer_id="11",
                    product_url="https://detail.1688.com/offer/11.html",
                    list_title="商品11",
                    detail_title="商品11",
                    main_image_url=None,
                    sku_rows=[
                        {"sku_id": "s1", "sku_name": "标准", "sku_price": 1.0, "sku_stock": 5}
                    ],
                    collected_at=utcnow(),
                    attempt=1,
                )

                report = check_orphans.audit(conn, rid)

                self.assertEqual(
                    [(o["shop_key"], o["offer_id"]) for o in report["orphans"]],
                    [("A01", "11")],
                )
            finally:
                conn.close()

    def test_reports_completed_shop_whose_offer_count_disagrees(self):
        """声称完整榜单、但已发现商品数与榜单行数不符的店铺要被报出，正常店铺不受影响。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            try:
                db = Database(conn)
                rid = db.start_or_resume()
                db.add_shop(rid, "A01", "https://a.example/", "店铺A")
                db.add_shop(rid, "A02", "https://b.example/", "店铺B")
                db.save_shop_offers(
                    rid, "A01", "https://a.example/", "店铺A",
                    [(1, "11", "https://detail.1688.com/offer/11.html", "商品11", "")], 1,
                )
                db.save_shop_offers(
                    rid, "A02", "https://b.example/", "店铺B",
                    [(1, "22", "https://detail.1688.com/offer/22.html", "商品22", "")], 1,
                )
                # 人工制造历史脏数据：店铺声称已发现 3 个商品，榜单却只有 1 行。
                conn.execute(
                    "UPDATE shop_rounds SET offer_count=3 WHERE round_id=? AND shop_key='A01'",
                    (rid,),
                )
                conn.commit()

                report = check_orphans.audit(conn, rid)

                self.assertEqual(
                    [(m["shop_key"], m["offer_count"], m["listed"])
                     for m in report["count_mismatch"]],
                    [("A01", 3, 1)],
                )
            finally:
                conn.close()

    def test_cli_reports_orphans_and_exits_nonzero(self):
        """命令行按店铺与商品报出孤儿，并以非零返回码表示发现不一致；只读不写库。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bestseller.db"
            conn = connect(db_path)
            try:
                db = Database(conn)
                rid = db.start_or_resume()
                db.add_shop(rid, "A01", "https://a.example/", "店铺A")
                db.submit_inventory_snapshot(
                    round_id=rid,
                    shop_key="A01",
                    shop_url="https://a.example/",
                    shop_name="店铺A",
                    offer_id="11",
                    product_url="https://detail.1688.com/offer/11.html",
                    list_title="商品11",
                    detail_title="商品11",
                    main_image_url=None,
                    sku_rows=[
                        {"sku_id": "s1", "sku_name": "标准", "sku_price": 1.0, "sku_stock": 5}
                    ],
                    collected_at=utcnow(),
                    attempt=1,
                )
            finally:
                conn.close()
            before = db_path.read_bytes()

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = check_orphans.main(["--db", str(db_path)])

            text = buf.getvalue()
            self.assertIn("A01", text)
            self.assertIn("11", text)
            self.assertEqual(code, 1, "发现不一致时返回非零")
            self.assertEqual(db_path.read_bytes(), before, "只读：运行不得写入数据库")

    def test_clean_database_is_quiet_and_defaults_to_project_config(self):
        """干净库不报不一致、返回 0；不带 --db 时用项目配置解析出的库位置。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bestseller.db"
            conn = connect(db_path)
            try:
                db = Database(conn)
                rid = db.start_or_resume()
                db.add_shop(rid, "A01", "https://a.example/", "店铺A")
                db.save_shop_offers(
                    rid, "A01", "https://a.example/", "店铺A",
                    [(1, "11", "https://detail.1688.com/offer/11.html", "商品11", "")], 1,
                )
                db.mark_skipped(
                    rid, "A01", "https://a.example/", "店铺A", "11",
                    "https://detail.1688.com/offer/11.html", "商品11",
                )
            finally:
                conn.close()

            buf = io.StringIO()
            with patch.object(check_orphans, "default_db_path", return_value=db_path), \
                    contextlib.redirect_stdout(buf):
                code = check_orphans.main([])

            self.assertEqual(code, 0, "无不一致时返回 0")
            self.assertIn("未发现不一致", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
