"""只读对账：报出「有快照、无榜单行」的孤儿商品与完成态计数不符的店铺。

用法：python tools/check_orphans.py [--db PATH] [--round N]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bestseller_monitor.config import Config


def audit(con: sqlite3.Connection, round_id: int | None = None) -> dict:
    """返回不一致清单；只读，不写库。"""
    sql = """
    SELECT DISTINCT s.round_id, s.shop_key, s.offer_id
    FROM snapshots s
    WHERE NOT EXISTS (
      SELECT 1 FROM shop_offers so
      WHERE so.round_id = s.round_id AND so.shop_key = s.shop_key
        AND so.offer_id = s.offer_id
    )
    """
    params: tuple = ()
    if round_id is not None:
        sql += " AND s.round_id = ?"
        params = (round_id,)
    sql += " ORDER BY s.round_id, s.shop_key, s.offer_id"
    orphans = [
        {"round_id": row[0], "shop_key": row[1], "offer_id": row[2]}
        for row in con.execute(sql, params)
    ]

    mismatch_sql = """
    SELECT r.round_id, r.shop_key, r.offer_count, COUNT(so.id) AS listed
    FROM shop_rounds r
    LEFT JOIN shop_offers so
      ON so.round_id = r.round_id AND so.shop_key = r.shop_key
    WHERE r.list_status = '完成'
    """
    if round_id is not None:
        mismatch_sql += " AND r.round_id = ?"
    mismatch_sql += """
    GROUP BY r.round_id, r.shop_key, r.offer_count
    HAVING COUNT(so.id) != r.offer_count
    ORDER BY r.round_id, r.shop_key
    """
    count_mismatch = [
        {"round_id": row[0], "shop_key": row[1], "offer_count": row[2], "listed": row[3]}
        for row in con.execute(mismatch_sql, params)
    ]
    return {"orphans": orphans, "count_mismatch": count_mismatch}


def default_db_path() -> Path:
    """按项目配置解析数据库位置，不写死仓库内相对路径。"""
    return Config.from_file(REPO / "config" / "config.toml", root=REPO).db_file


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def render(db_path: Path, report: dict) -> str:
    lines = [f"对账：{db_path}（只读）"]
    for orphan in report["orphans"]:
        lines.append(
            f"轮次 #{orphan['round_id']} · 店铺 {orphan['shop_key']} · "
            f"商品 {orphan['offer_id']}：有快照、无榜单行"
        )
    for row in report["count_mismatch"]:
        lines.append(
            f"轮次 #{row['round_id']} · 店铺 {row['shop_key']}："
            f"声称完成但榜单行数 {row['listed']} ≠ 已发现商品数 {row['offer_count']}"
        )
    if len(lines) == 1:
        lines.append("未发现不一致")
    else:
        lines.append(
            f"合计：孤儿商品 {len(report['orphans'])} 个，"
            f"计数不符店铺 {len(report['count_mismatch'])} 家"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读对账：孤儿商品与计数不符的店铺")
    parser.add_argument("--db", help="数据库文件；缺省时取项目配置里的位置")
    parser.add_argument("--round", type=int, dest="round_id", help="只检查某一轮")
    args = parser.parse_args(argv)
    db_path = Path(args.db) if args.db else default_db_path()
    con = _open_readonly(db_path)
    try:
        report = audit(con, args.round_id)
    finally:
        con.close()
    print(render(db_path, report))
    return 1 if report["orphans"] or report["count_mismatch"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
