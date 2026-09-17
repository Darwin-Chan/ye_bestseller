"""只读：按店统计「点击→弹窗」可靠性（基于 event_log 的 click_* 事件）。

用法：python tools/analyze_click.py [--round N] [--db data/bestseller.db] [--out output]
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bestseller_monitor.click_events import ALL_EVENT_NAMES, ClickOutcome, classify

# 事件名与结果种类的对应（含历史名）由 click_events 一处定义，这里只挑自己关心的那些。
# `click_skipped` 既不算成功也不算失败：它只进「点击」总数，不进成功率的分母。
SUCCESS = ClickOutcome.SUBMITTED
FAILURE_OUTCOMES = (ClickOutcome.UNREADABLE, ClickOutcome.NOT_OPENED,
                    ClickOutcome.NO_OFFER, ClickOutcome.DENIED)


def load_click_rows(conn: sqlite3.Connection, round_id: int | None = None):
    sql = ("SELECT shop_key, event, COUNT(*) AS c FROM event_log "
           f"WHERE event IN ({','.join('?' * len(ALL_EVENT_NAMES))})")
    params: list = list(ALL_EVENT_NAMES)
    if round_id is not None:
        sql += " AND round_id=?"
        params.append(round_id)
    sql += " GROUP BY shop_key, event"
    return conn.execute(sql, params).fetchall()


def compute(rows) -> list[dict]:
    agg: dict[str, dict[ClickOutcome, int]] = defaultdict(
        lambda: {outcome: 0 for outcome in ClickOutcome})
    for r in rows:
        got = classify(r["event"], None)
        if got is not None:
            agg[r["shop_key"]][got.outcome] += r["c"]
    out = []
    for sk, d in agg.items():
        ok = d[SUCCESS]
        attempted = ok + sum(d[outcome] for outcome in FAILURE_OUTCOMES)
        success_rate = (ok / attempted) if attempted else None
        no_popup_ratio = (d[ClickOutcome.NOT_OPENED] / attempted) if attempted else None
        out.append({
            "shop": sk,
            **{outcome.value: d[outcome] for outcome in ClickOutcome},
            "total": sum(d.values()), "attempted": attempted,
            "success_rate": success_rate, "no_popup_ratio": no_popup_ratio,
        })
    out.sort(key=lambda x: (x["success_rate"] is None, -(x["success_rate"] or 0.0)))
    return out


def _pct(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else "—"


def render(out: list[dict]) -> str:
    lines = ["1688 点击→弹窗可靠性（按店）",
             f"{'店铺':<6}{'点击':>6}{'成功':>6}{'无弹窗':>8}{'非offer':>8}"
             f"{'解析失败':>8}{'跳过':>6}{'deny':>6}{'成功率':>9}{'无弹窗占比':>10}"]
    for d in out:
        lines.append(
            f"{d['shop']:<6}{d['total']:>6}{d[SUCCESS.value]:>6}"
            f"{d[ClickOutcome.NOT_OPENED.value]:>8}{d[ClickOutcome.NO_OFFER.value]:>8}"
            f"{d[ClickOutcome.UNREADABLE.value]:>8}{d[ClickOutcome.SKIPPED.value]:>6}"
            f"{d[ClickOutcome.DENIED.value]:>6}{_pct(d['success_rate']):>9}"
            f"{_pct(d['no_popup_ratio']):>10}"
        )
    if not out:
        lines.append("（无 click_* 事件）")
    return "\n".join(lines)


def write_csv(path: Path, out: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["shop", "total", "ok", "no_popup", "url_notoffer",
                    "parse_error", "skipped", "deny", "attempted",
                    "success_rate", "no_popup_ratio"])
        for d in out:
            w.writerow([d["shop"], d["total"], d[SUCCESS.value],
                        d[ClickOutcome.NOT_OPENED.value],
                        d[ClickOutcome.NO_OFFER.value],
                        d[ClickOutcome.UNREADABLE.value],
                        d[ClickOutcome.SKIPPED.value],
                        d[ClickOutcome.DENIED.value], d["attempted"],
                        _pct(d["success_rate"]), _pct(d["no_popup_ratio"])])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--round", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    db_path = args.db or (Path(__file__).resolve().parents[1] / "data" / "bestseller.db")
    out_dir = args.out or (Path(__file__).resolve().parents[1] / "output")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        res = compute(load_click_rows(conn, args.round))
    finally:
        conn.close()
    text = render(res)
    print(text)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    txt_path = out_dir / f"点击可靠性_{stamp}.txt"
    csv_path = out_dir / f"点击可靠性_{stamp}.csv"
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text(text + "\n", encoding="utf-8")
    write_csv(csv_path, res)
    print(f"\n已保存：{txt_path}\n已保存：{csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
