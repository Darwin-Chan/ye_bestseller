import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bestseller_monitor import rounds
from bestseller_monitor.db import Database, connect, cst_date
from bestseller_monitor.rounds import RoundRequest, ShopScope, TerminalReason
from tools import summary
from helpers import new_round


class SummaryTests(unittest.TestCase):
    def test_skipped_offer_counts_as_handled_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            try:
                db = Database(conn)
                rid = new_round(db)
                db.save_shop_offers(
                    rid, "A", "https://a.example/", "店铺A",
                    [(1, "111", "https://detail.1688.com/offer/111.html", "商品", "")], 1,
                )
                db.mark_skipped(
                    rid, "A", "https://a.example/", "店铺A", "111",
                    "https://detail.1688.com/offer/111.html", "商品",
                )

                counts = summary.summarize(conn, rid)

                self.assertEqual(counts["ok_offers"], 1, "跳过属于已处理")
                self.assertEqual(counts["fail_offers"], 0, "跳过不是失败")
                self.assertEqual(counts["shop_offers"], 1)
            finally:
                conn.close()

    def test_summary_reports_round_only(self):
        """摘要只报告轮次统计，不再查找已停用的导出文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "bestseller.db"
            conn = connect(db_path)
            try:
                rid = new_round(Database(conn))
            finally:
                conn.close()
            export_dir = tmp_path / "output"
            export_dir.mkdir()
            (export_dir / "日报_20260101_000000.xlsx").write_bytes(b"")

            buf = io.StringIO()
            with patch.object(summary, "ROOT", tmp_path), \
                    patch.object(summary, "DB", db_path), \
                    contextlib.redirect_stdout(buf):
                summary.main()

            text = buf.getvalue()
            self.assertIn(f"round={rid}", text)
            self.assertNotIn("latest_excel", text)

    def test_summary_reports_the_same_round_and_reason_as_the_module(self):
        """摘要工具的轮次与终态来自轮次模块，界面说的是同一件事。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "bestseller.db"
            conn = connect(db_path)
            try:
                db = Database(conn)
                rid = rounds.open(db, RoundRequest(
                    cst_date(), (ShopScope("A01", "https://a.example/", "店铺A"),),
                )).round.id
                rounds.finish(db, rounds.load(db, rid), TerminalReason.DAY_BOUNDARY)
            finally:
                conn.close()

            buf = io.StringIO()
            with patch.object(summary, "ROOT", tmp_path), \
                    patch.object(summary, "DB", db_path), \
                    contextlib.redirect_stdout(buf):
                summary.main()

            text = buf.getvalue()
            self.assertIn(f"round={rid}", text)
            self.assertIn("reason=DAY_BOUNDARY", text)


if __name__ == "__main__":
    unittest.main()
