import sqlite3
import unittest

from tools import analyze_click as ac


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


def _add(conn, shop, ev, rid=4):
    conn.execute(
        "INSERT INTO event_log(round_id, shop_key, event, ts) VALUES (?,?,?,?)",
        (rid, shop, ev, "2026-09-05T00:00:00+00:00"),
    )


class AnalyzeClickTests(unittest.TestCase):
    def test_compute_and_render(self):
        conn = _mem_conn()
        # A 店：10 ok, 5 no_popup, 3 url_notoffer, 1 parse_empty, 20 skipped
        for _ in range(10):
            _add(conn, "A", "click_ok")
        for _ in range(5):
            _add(conn, "A", "click_no_popup")
        for _ in range(3):
            _add(conn, "A", "click_url_notoffer")
        _add(conn, "A", "click_parse_empty")
        for _ in range(20):
            _add(conn, "A", "click_skipped")
        # B 店：全成功
        for _ in range(30):
            _add(conn, "B", "click_ok")
        rows = ac.load_click_rows(conn, round_id=4)
        res = ac.compute(rows)
        by_shop = {d["shop"]: d for d in res}
        a = by_shop["A"]
        self.assertEqual(a["click_ok"], 10)
        self.assertEqual(a["click_no_popup"], 5)
        self.assertEqual(a["click_skipped"], 20)
        self.assertEqual(a["attempted"], 10 + 5 + 3 + 1)  # 19
        self.assertAlmostEqual(a["success_rate"], 10 / 19)
        self.assertAlmostEqual(a["no_popup_ratio"], 5 / 19)
        b = by_shop["B"]
        self.assertEqual(b["success_rate"], 1.0)
        self.assertEqual(b["no_popup_ratio"], 0.0)
        # 排序：成功率高的在前（B > A）
        self.assertEqual(res[0]["shop"], "B")
        text = ac.render(res)
        self.assertIn("成功", text)
        conn.close()

    def test_empty(self):
        conn = _mem_conn()
        res = ac.compute(ac.load_click_rows(conn, round_id=1))
        self.assertEqual(res, [])
        self.assertIn("无 click_* 事件", ac.render(res))
        conn.close()


if __name__ == "__main__":
    unittest.main()
