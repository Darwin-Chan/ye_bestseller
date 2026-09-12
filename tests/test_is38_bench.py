"""IS-38 基准脚本：建得出合成大盘库，量得出刷新成本。"""
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor.db import Database, connect
from tools import is38_bench


class Is38BenchTests(unittest.TestCase):
    def test_builds_a_finished_round_with_the_requested_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "bench.db")
            try:
                rid, rows = is38_bench.build_dataset(conn, shops=2, rows_per_shop=30)

                self.assertEqual(rows, 60)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE round_id=?", (rid,)
                ).fetchone()[0], 60)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM event_log WHERE round_id=?", (rid,)
                ).fetchone()[0], 60)
                done = conn.execute(
                    "SELECT COUNT(*) FROM shop_rounds WHERE round_id=? AND list_status='完成'",
                    (rid,),
                ).fetchone()[0]
                self.assertEqual(done, 2, "合成库要让 12 家店的指标查询真的跑起来")
                self.assertEqual(
                    [row[0] for row in conn.execute(
                        "SELECT DISTINCT shop_key FROM shop_rounds WHERE round_id=? "
                        "ORDER BY shop_key", (rid,))],
                    ["S01", "S02"],
                )
            finally:
                conn.close()

    def test_measure_reports_refresh_and_legacy_costs(self):
        report = is38_bench.measure(shops=2, rows_per_shop=200, repeats=1, legacy=True)

        self.assertEqual(report["shops"], 2)
        self.assertEqual(report["rows"], 400)
        self.assertEqual(report["done_count"], 2)
        self.assertGreater(report["connect_sec"], 0)
        self.assertGreater(report["refresh_sec"], 0)
        self.assertIsNotNone(report["legacy_sec"], "缺索引的老库要单独量一次去重成本")
        self.assertEqual(report["legacy_removed"], 0, "唯一索引只防新重复，老库里本来就没有")


if __name__ == "__main__":
    unittest.main()
