"""读取最近一轮的抓取结果摘要（不打印日志）。"""
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "bestseller.db"


def summarize(con: sqlite3.Connection, round_id: int) -> dict:
    """汇总某一轮的抓取结果；跳过属于已处理，不计失败也不计入 SKU 行。"""
    offers = con.execute(
        "SELECT COUNT(*) FROM shop_offers WHERE round_id=?", (round_id,)
    ).fetchone()[0]
    ok_offers = con.execute(
        "SELECT COUNT(DISTINCT offer_id) FROM snapshots WHERE round_id=? "
        "AND page_status IN ('成功', '跳过')",
        (round_id,),
    ).fetchone()[0]
    fail_offers = con.execute(
        "SELECT COUNT(DISTINCT offer_id) FROM snapshots WHERE round_id=? AND page_status='失败'",
        (round_id,),
    ).fetchone()[0]
    sku_rows = con.execute(
        "SELECT COUNT(*) FROM snapshots WHERE round_id=? AND page_status='成功' AND sku_id IS NOT NULL",
        (round_id,),
    ).fetchone()[0]
    return {
        "shop_offers": offers,
        "ok_offers": ok_offers,
        "fail_offers": fail_offers,
        "sku_rows": sku_rows,
    }


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
    counts = summarize(con, rid)
    print(f"round={rid} status={r['status']} shop_offers={counts['shop_offers']} "
          f"ok_offers={counts['ok_offers']} fail_offers={counts['fail_offers']} "
          f"sku_rows={counts['sku_rows']}")
    ex = sorted((ROOT / "output").glob("日报_*.xlsx"), key=lambda p: p.stat().st_mtime)
    if ex:
        print("latest_excel:", ex[-1])
    con.close()


if __name__ == "__main__":
    main()
