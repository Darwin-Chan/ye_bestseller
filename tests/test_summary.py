import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bestseller_monitor import rounds
from bestseller_monitor.db import Database, connect, cst_date
from bestseller_monitor.rounds import RoundRequest, ShopScope, TerminalReason
from test_config import MINIMAL_CONFIG
from test_rounds import LEGACY_ROUNDS_DDL
from tools import summary
from helpers import new_round

def _repo_with_config(tmp: Path) -> Path:
    """搭一个临时仓库根：config/config.toml 的 db_file 是相对根解析的。"""
    (tmp / "config").mkdir()
    (tmp / "config" / "config.toml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    return tmp


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
            with contextlib.redirect_stdout(buf):
                summary.main(["--db", str(db_path)])

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
            with contextlib.redirect_stdout(buf):
                summary.main(["--db", str(db_path)])

            text = buf.getvalue()
            self.assertIn(f"round={rid}", text)
            self.assertIn("reason=DAY_BOUNDARY", text)

    def test_default_db_comes_from_project_config(self):
        """不传 --db 时读配置里的 db_file，并把实际读的路径说出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo_with_config(Path(tmp))
            db_path = repo / "bestseller.db"
            conn = connect(db_path)
            try:
                rid = new_round(Database(conn))
            finally:
                conn.close()

            buf = io.StringIO()
            with patch.object(summary, "REPO", repo), contextlib.redirect_stdout(buf):
                code = summary.main([])

            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn(str(db_path), text)
            self.assertIn(f"round={rid}", text)

    def test_db_flag_overrides_the_configured_path(self):
        """显式 --db 优先于配置：即便配置指向另一个（空的）库。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo_with_config(Path(tmp))
            connect(repo / "bestseller.db").close()      # 配置指向它，里面没有轮次
            other = repo / "副本.db"
            conn = connect(other)
            try:
                rid = new_round(Database(conn))
            finally:
                conn.close()

            buf = io.StringIO()
            with patch.object(summary, "REPO", repo), contextlib.redirect_stdout(buf):
                code = summary.main(["--db", str(other)])

            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn(str(other), text)
            self.assertIn(f"round={rid}", text)

    def test_missing_db_reports_the_path_it_looked_for(self):
        """库不存在时要说清找的是哪个路径，而不是只打印 no db。"""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "还没有这个库.db"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = summary.main(["--db", str(missing)])

            text = buf.getvalue()
            self.assertNotEqual(code, 0)
            self.assertIn(str(missing), text)
            self.assertNotIn("no db", text)

    def test_legacy_db_reports_old_structure_instead_of_sql_error(self):
        """旧结构库（有 status、没有 run_date）要给可读提示，不抛 SQL 错误。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            con = sqlite3.connect(str(db_path))
            try:
                con.execute(LEGACY_ROUNDS_DDL)
                con.commit()
            finally:
                con.close()

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = summary.main(["--db", str(db_path)])

            text = buf.getvalue()
            self.assertNotEqual(code, 0)
            self.assertIn(str(db_path), text)
            self.assertIn("旧结构", text)
            self.assertIn("迁移", text)


if __name__ == "__main__":
    unittest.main()
