"""读取最近一轮的抓取结果摘要（不打印日志）。"""
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "bestseller.db"


def main():
    if not DB.exists():
        print("no db")
        return
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        print("no rounds")
        return
    rid = r["id"]
    offers = con.execute("SELECT COUNT(*) FROM shop_offers WHERE round_id=?", (rid,)).fetchone()[0]
    success_offer = con.execute(
        "SELECT COUNT(DISTINCT offer_id) FROM snapshots WHERE round_id=? AND page_status='成功'", (rid,)
    ).fetchone()[0]
    fail_offer = con.execute(
        "SELECT COUNT(DISTINCT offer_id) FROM snapshots WHERE round_id=? AND page_status='失败'", (rid,)
    ).fetchone()[0]
    skus = con.execute(
        "SELECT COUNT(*) FROM snapshots WHERE round_id=? AND page_status='成功' AND sku_id IS NOT NULL", (rid,)
    ).fetchone()[0]
    print(f"round={rid} status={r['status']} shop_offers={offers} ok_offers={success_offer} "
          f"fail_offers={fail_offer} sku_rows={skus}")
    ex = sorted((ROOT / "output").glob("日报_*.xlsx"), key=lambda p: p.stat().st_mtime)
    if ex:
        print("latest_excel:", ex[-1])
    con.close()


if __name__ == "__main__":
    main()
