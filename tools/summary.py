"""读取最近一轮的抓取结果摘要（只读，不打印日志）。

用法：python tools/summary.py [--db PATH]
不传 --db 时按项目配置的 db_file 解析，与 tools/check_orphans.py 一致。
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

from bestseller_monitor import rounds  # noqa: E402
from bestseller_monitor.config import Config  # noqa: E402
from bestseller_monitor.db import Database  # noqa: E402


def default_db_path() -> Path:
    """按项目配置解析数据库位置，不写死仓库内相对路径。"""
    return Config.from_file(REPO / "config" / "config.toml", root=REPO).db_file


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


def _unreadable_db_hint(db_path: Path, exc: sqlite3.Error) -> str:
    """只读连接不走迁移，所以旧结构库要说清该由谁来升级。"""
    detail = str(exc)
    if "no such column" in detail:
        return (f"读不了这个库（{detail}）：{db_path} 还是旧结构，"
                "跑一次采集会自动迁移。")
    if "no such table" in detail:
        return (f"读不了这个库（{detail}）：{db_path} 里没有本项目的表，"
                "跑一次采集会自动建表。")
    return f"读不了这个库（{detail}）：{db_path}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读取最近一轮的抓取结果摘要（只读）")
    parser.add_argument("--db", help="数据库文件；缺省时取项目配置里的位置")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_db_path()
    if not db_path.exists():
        print(f"数据库不存在：{db_path}")
        print("请检查 config/config.toml 里的 db_file，或用 --db 指定数据库文件。")
        return 1

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        print(f"摘要：{db_path}（只读）")
        try:
            run = rounds.latest(Database(con))
        except sqlite3.OperationalError as exc:
            print(_unreadable_db_hint(db_path, exc))
            return 1
        if run is None:
            print("no rounds")
            return 0
        counts = summarize(con, run.id)
        reason = run.reason.value if run.reason is not None else "IN_PROGRESS"
        print(f"round={run.id} date={run.run_date} reason={reason} "
              f"shop_offers={counts['shop_offers']} ok_offers={counts['ok_offers']} "
              f"fail_offers={counts['fail_offers']} sku_rows={counts['sku_rows']}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
