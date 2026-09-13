"""界面刷新基准：每次刷新要付多少时间（连接迁移 + 每店指标聚合）。工单 IS-38。

界面每约 2 秒刷新一次，所以这里的每一毫秒都是「每 2 秒付一次」的成本。脚本建自己的
临时库（不碰 F:/AI/bestseller_runtime 下的真机数据），分别量三件事：

  - connect()：开一次连接（含迁移）。界面每个 API 入口都会走它。
  - get_run()：一次刷新的全部查询（12 家店 × 4 条指标）。
  - --legacy：缺唯一索引的老库上，那次整表去重要多久（只在补索引时付一次）。

用法：
  python tools/bench_refresh.py
  python tools/bench_refresh.py --shops 12 --rows-per-shop 25000 --repeats 3 --legacy
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from bestseller_monitor import db  # noqa: E402
from bestseller_monitor.db import (  # noqa: E402
    Database,
    EVENT_REFRESH_INDEXES,
    SNAPSHOT_SUCCESS_INDEX,
    connect,
)
from bestseller_monitor import rounds  # noqa: E402
from bestseller_monitor.rounds import RoundRequest, ShopScope  # noqa: E402

# 合成轮次的日期与对应的「现在」：基准不读挂钟，量的是刷新本身（候选 04）。
RUN_DATE = "2026-09-12"
NOW = "2026-09-12T04:00:00+00:00"      # 北京时间同日 12:00


def refresh_api(db_file: Path, *, now: str = NOW, open_conn=None):
    """给基准一个真界面对象：量的是 `get_run()` 本身，不是复刻它的 SQL。

    `gui` 只在用到时才导入——它拖着 pywebview 那一套，别的 tools 不依赖。
    """
    from gui import Api

    return Api(cfg=SimpleNamespace(db_file=db_file, max_pages_per_shop=3),
               shops=[], now=lambda: now, open_conn=open_conn)


def build_dataset(conn: sqlite3.Connection, shops: int, rows_per_shop: int,
                  run_date: str = RUN_DATE) -> tuple[int, int]:
    """建一轮「已完成」的合成数据，返回 (轮次号, 写入的快照行数)。"""
    keys = [f"S{i:02d}" for i in range(1, shops + 1)]
    scopes = tuple(ShopScope(k, f"https://{k}.example/", f"店铺{k}") for k in keys)
    db = Database(conn)
    rid = rounds.open(db, RoundRequest(run_date, scopes)).round.id
    base = datetime.fromisoformat(run_date + "T01:00:00")
    snapshots, events, listed = [], [], []
    for key in keys:
        db.add_shop(rid, key, f"https://{key}.example/", f"店铺{key}")
        for i in range(rows_per_shop):
            ts = (base + timedelta(seconds=i)).isoformat()
            listed.append(
                (rid, key, f"https://{key}.example/", f"店铺{key}", i,
                 f"offer-{key}-{i}", "https://detail.example/x", "商品", "")
            )
            snapshots.append(
                (rid, key, f"https://{key}.example/", f"店铺{key}", f"offer-{key}-{i}",
                 "https://detail.example/x", "商品", f"sku-{i}", "规格", 1.0, 5, ts, "成功", 1)
            )
            events.append(
                (rid, key, f"offer-{key}-{i}", "detail",
                 "click_deny" if i % 500 == 0 else "detail_ok", ts)
            )
    conn.executemany(
        "INSERT INTO shop_offers(round_id, shop_key, shop_url, shop_name, rank, "
        "offer_id, product_url, list_title, list_price) VALUES (?,?,?,?,?,?,?,?,?)",
        listed,
    )
    conn.executemany(
        "INSERT INTO snapshots (round_id, shop_key, shop_url, shop_name, offer_id, "
        "product_url, product_name, sku_id, sku_name, sku_price, sku_stock, collected_at, "
        "page_status, attempt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        snapshots,
    )
    conn.executemany(
        "INSERT INTO event_log (round_id, shop_key, offer_id, phase, event, ts) "
        "VALUES (?,?,?,?,?,?)",
        events,
    )
    conn.execute("UPDATE shop_rounds SET list_status='完成' WHERE round_id=?", (rid,))
    conn.commit()
    return rid, len(snapshots)


def _best(fn, repeats: int) -> float:
    best = None
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return best


def measure(*, shops: int, rows_per_shop: int, repeats: int, legacy: bool = False) -> dict:
    """在临时库里量一次自己的成本；返回各项耗时（秒）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bench.db"
        conn = connect(path)
        try:
            rid, rows = build_dataset(conn, shops, rows_per_shop)
        finally:
            conn.close()

        def open_close():
            opened = connect(path)
            opened.close()

        connect_sec = _best(open_close, repeats)

        api = refresh_api(path)
        run = api.get_run()
        refresh_sec = _best(api.get_run, repeats)

        # 对照：同一个 get_run()，把两条事件索引摘掉再量一次。得绕开 connect()——它每次
        # 开连接都会把索引建回来，所以这里让界面用裸连接（摘掉的索引因此真的不在了）。
        raw = sqlite3.connect(path)
        try:
            for name in EVENT_REFRESH_INDEXES:
                raw.execute(f"DROP INDEX IF EXISTS {name}")
            raw.commit()
        finally:
            raw.close()

        def open_raw():
            opened = sqlite3.connect(path)
            opened.row_factory = sqlite3.Row
            return opened

        without_index_sec = _best(refresh_api(path, open_conn=open_raw).get_run, repeats)

        result = {
            "shops": shops,
            "rows": rows,
            "round_id": rid,
            "done_count": run["done_count"],
            "connect_sec": connect_sec,
            "refresh_sec": refresh_sec,
            "refresh_without_event_index_sec": without_index_sec,
            "legacy_sec": None,
            "legacy_removed": None,
        }
        if legacy:
            # 老库的样子：把唯一索引摘掉，看那次「去重 + 重建索引」要多久。
            copy_path = Path(tmp) / "legacy.db"
            shutil.copyfile(path, copy_path)
            raw = sqlite3.connect(copy_path)
            try:
                raw.execute(f"DROP INDEX IF EXISTS {SNAPSHOT_SUCCESS_INDEX}")
                raw.commit()
            finally:
                raw.close()

            # 报告直接回答「这次开库删了几行、建了哪个索引」，不用前后数行数。
            migrated: list = []

            def reopen():
                opened = db.open(copy_path)
                migrated.append(db.migrate(opened))
                opened.close()

            result["legacy_sec"] = _best(reopen, 1)
            result["legacy_removed"] = migrated[0].deduped_snapshot_rows
            result["legacy_index_built"] = migrated[0].created_indexes
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IS-38：界面刷新成本基准")
    parser.add_argument("--shops", type=int, default=12, help="店铺数（默认 12）")
    parser.add_argument("--rows-per-shop", type=int, default=25_000,
                        help="每家店写入的快照行数（默认 2.5 万 → 30 万行）")
    parser.add_argument("--repeats", type=int, default=3, help="取最好成绩的重复次数")
    parser.add_argument("--legacy", action="store_true",
                        help="额外量一次「缺唯一索引的老库」的整表去重成本")
    args = parser.parse_args(argv)

    result = measure(shops=args.shops, rows_per_shop=args.rows_per_shop,
                     repeats=args.repeats, legacy=args.legacy)
    print(f"规模：{result['shops']} 店 · 快照 {result['rows']:,} 行 · "
          f"轮次 #{result['round_id']}（{result['done_count']} 家已完成）")
    print(f"connect()（每次开连接） : {result['connect_sec'] * 1000:8.2f} ms")
    print(f"get_run()（一次刷新）   : {result['refresh_sec'] * 1000:8.2f} ms")
    print(f"  摘掉两条事件索引对照   : "
          f"{result['refresh_without_event_index_sec'] * 1000:8.2f} ms")
    if result["legacy_sec"] is not None:
        print(f"老库首次去重（仅一次）  : {result['legacy_sec'] * 1000:8.2f} ms"
              f"（删除 {result['legacy_removed']} 行，建索引 "
              f"{','.join(result['legacy_index_built']) or '无'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
