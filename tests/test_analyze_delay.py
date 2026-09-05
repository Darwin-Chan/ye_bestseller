import sqlite3
import unittest

from tools import analyze_delay as ad


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE event_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER, shop_key TEXT,
            offer_id TEXT, sku_id TEXT, phase TEXT, event TEXT, kind TEXT,
            ts TEXT, prev_ts TEXT, interval_ms INTEGER, verification_type TEXT,
            attempt INTEGER, config_hash TEXT, note TEXT
        );
        """
    )
    return conn


def _add(conn, rid, shop, ev, ms, phase="listing", k="work", vt="none"):
    conn.execute(
        "INSERT INTO event_log(round_id, shop_key, event, interval_ms, phase, kind, "
        "verification_type, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, shop, ev, ms, phase, k, vt, "2026-09-04T00:00:00+00:00"),
    )


class AnalyzeTests(unittest.TestCase):
    def test_bucket_rate_and_corr(self):
        conn = _mem_conn()
        rid = 1
        _add(conn, rid, "A", "popup_open", 400)
        _add(conn, rid, "A", "verification_appear", 350, k="verification", vt="slider")
        _add(conn, rid, "A", "verification_solved", 2500, k="verification", vt="slider")
        _add(conn, rid, "A", "popup_open", 1000)
        _add(conn, rid, "A", "detail_parse", 1200)
        _add(conn, rid, "A", "popup_close", 1500)
        _add(conn, rid, "A", "popup_open", 450)
        _add(conn, rid, "A", "verification_appear", 400, k="verification", vt="slider")
        _add(conn, rid, "B", "popup_open", 2000)
        _add(conn, rid, "B", "detail_parse", 2200)

        res = ad.analyze(conn, rid)
        conn.close()
        # 短间隔(<0.5s)的步骤事件下一步全是验证
        bin0 = next(s for s in res["buckets"] if s["bin"] == "<0.5s")
        self.assertEqual(bin0["events"], 2)
        self.assertEqual(bin0["triggers"], 2)
        self.assertAlmostEqual(bin0["rate"], 1.0)
        # 1-2s 桶：3 个步骤事件，0 个触发
        bin12 = next(s for s in res["buckets"] if s["bin"] == "1-2s")
        self.assertEqual(bin12["events"], 3)
        self.assertEqual(bin12["triggers"], 0)
        # 总验证出现数（A 店两次）
        self.assertEqual(res["total_verif"], 2)
        # 间隔越大越不容易触发 → 相关系数应为负
        self.assertLess(res["corr_interval"]["pearson"], 0)
        self.assertLess(res["corr_interval"]["spearman"], 0)

    def test_empty_stream(self):
        conn = _mem_conn()
        res = ad.analyze(conn, 1)
        conn.close()
        self.assertEqual(res["total_events"], 0)
        self.assertEqual(res["total_verif"], 0)
        self.assertIsNone(res["corr_interval"]["pearson"])


if __name__ == "__main__":
    unittest.main()
