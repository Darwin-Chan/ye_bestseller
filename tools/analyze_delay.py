"""只读分析：处理间隔 × 反爬验证触发率的关联。

用法：
    python tools/analyze_delay.py                    # 分析全部事件
    python tools/analyze_delay.py --round 5          # 只看第 5 轮
    python tools/analyze_delay.py --chart            # 生成柱状图（需 matplotlib）

核心口径：
    - “桶内触发率”按“上一步事件间隔”分桶：桶内事件数 = 该间隔区间内、非验证类步骤事件的个数；
      桶内触发数 = 这些步骤事件中“下一步紧跟着 verification_appear”的个数；触发率 = 触发数/事件数。
    - 相关系数：Pearson 与 Spearman，针对 interval_ms（以及渠道内累计序号）与“下一步是否验证”。
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from bestseller_monitor.db import CST

BINS_SEC = [0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, float("inf")]
BIN_LABELS = ["<0.5s", "0.5-1s", "1-2s", "2-3s", "3-5s", "5-10s", ">10s"]
VERIFY_EVENTS = {"verification_appear", "verification_solved"}


def bin_of(interval_ms: int | None) -> int | None:
    if interval_ms is None:
        return None
    s = interval_ms / 1000.0
    for i in range(len(BINS_SEC) - 1):
        if s < BINS_SEC[i + 1]:
            return i
    return len(BIN_LABELS) - 1


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    if dx <= 0 or dy <= 0:
        return None
    return num / (dx * dy) ** 0.5


def ranks(vals: list[float]) -> list[float]:
    """平均秩（处理并列）。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    return pearson(ranks(xs), ranks(ys))


def load_events(conn: sqlite3.Connection, round_id: int | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT id, round_id, shop_key, offer_id, sku_id, phase, event, kind, ts, interval_ms, "
        "verification_type, attempt FROM event_log "
    )
    if round_id is not None:
        sql += "WHERE round_id=? "
        sql += "ORDER BY id"
        return conn.execute(sql, (round_id,)).fetchall()
    sql += "ORDER BY id"
    return conn.execute(sql).fetchall()


def _channel_key(row: sqlite3.Row) -> tuple:
    return (row["round_id"], row["shop_key"] or "<none>")


def build_pairs(rows: list[sqlite3.Row]) -> list[dict]:
    """把事件按渠道排序，找出“(非验证步骤事件, 下一步) 且该步骤有 interval”的所有对。"""
    channels: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        channels[_channel_key(r)].append(r)

    pairs: list[dict] = []
    for ch in channels.values():
        ch.sort(key=lambda r: r["id"])
        for idx, a in enumerate(ch):
            if a["event"] in VERIFY_EVENTS:
                continue
            if a["interval_ms"] is None:
                continue
            b = ch[idx + 1] if idx + 1 < len(ch) else None
            pairs.append({
                "interval_ms": a["interval_ms"],
                "next_is_verify": 1 if (b is not None and b["event"] == "verification_appear") else 0,
                "seq": idx,
                "round_id": a["round_id"],
                "shop_key": a["shop_key"] or "<none>",
                "phase": a["phase"] or "-",
            })
    return pairs


def bucket_stats(pairs: list[dict]) -> list[dict]:
    stats = [{"bin": BIN_LABELS[i], "events": 0, "triggers": 0, "rate": None}
             for i in range(len(BIN_LABELS))]
    for p in pairs:
        b = bin_of(p["interval_ms"])
        if b is None:
            continue
        stats[b]["events"] += 1
        stats[b]["triggers"] += p["next_is_verify"]
    for s in stats:
        if s["events"]:
            s["rate"] = s["triggers"] / s["events"]
    return stats


def verif_summary(rows: list[sqlite3.Row]) -> list[dict]:
    agg: dict[tuple, int] = defaultdict(int)
    by_hour: dict[tuple, int] = defaultdict(int)
    for r in rows:
        if r["event"] != "verification_appear":
            continue
        agg[(r["round_id"], r["shop_key"] or "<none>", r["phase"] or "-",
             r["verification_type"] or "unknown")] += 1
        try:
            hour = datetime.fromisoformat(r["ts"]).astimezone(CST).hour
        except Exception:
            hour = -1
        by_hour[(r["round_id"], hour)] += 1
    out = sorted(({"round_id": k[0], "shop_key": k[1], "phase": k[2],
                   "verification_type": k[3], "count": v} for k, v in agg.items()),
                 key=lambda d: (d["round_id"], d["shop_key"]))
    return out, sorted(({"round_id": k[0], "hour": k[1], "count": v}
                        for k, v in by_hour.items()),
                       key=lambda d: (d["round_id"], d["hour"]))


def analyze(conn: sqlite3.Connection, round_id: int | None = None) -> dict:
    rows = load_events(conn, round_id)
    pairs = build_pairs(rows)
    buckets = bucket_stats(pairs)
    verif, verif_hours = verif_summary(rows)

    xs = [p["interval_ms"] for p in pairs]
    ys = [p["next_is_verify"] for p in pairs]
    corr_interval = {"pearson": pearson(xs, ys), "spearman": spearman(xs, ys)}
    xs_seq = [p["seq"] for p in pairs]
    corr_seq = {"pearson": pearson(xs_seq, ys), "spearman": spearman(xs_seq, ys)}

    total_events = len(rows)
    total_verif = sum(v["count"] for v in verif)
    return {
        "buckets": buckets, "pairs": pairs, "verif": verif, "verif_hours": verif_hours,
        "corr_interval": corr_interval, "corr_seq": corr_seq,
        "total_events": total_events, "total_verif": total_verif,
    }


def _fmt_rate(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else "—"


def render_report(res: dict) -> str:
    lines = []
    lines.append("1688 处理延迟 × 反爬验证 关联分析")
    lines.append("=" * 48)
    lines.append(f"事件总数：{res['total_events']}   验证出现(v_appear)：{res['total_verif']}")
    if res["total_events"]:
        lines.append(f"整体触发率：{res['total_verif']/res['total_events']:.1%}")
    lines.append("")

    lines.append("【按“上一步事件间隔”分桶的触发率】")
    lines.append(f"{'间隔':<10}{'事件数':>8}{'触发数':>8}{'触发率':>9}")
    for s in res["buckets"]:
        lines.append(f"{s['bin']:<10}{s['events']:>8}{s['triggers']:>8}"
                     f"{_fmt_rate(s['rate']):>9}")

    lines.append("")
    lines.append(f"【间隔 vs 下一步是否验证】"
                 f"Pearson={_fmt(res['corr_interval']['pearson'])}  "
                 f"Spearman={_fmt(res['corr_interval']['spearman'])}")
    lines.append(f"【渠道内累计序号 vs 下一步是否验证】"
                 f"Pearson={_fmt(res['corr_seq']['pearson'])}  "
                 f"Spearman={_fmt(res['corr_seq']['spearman'])}")

    lines.append("")
    lines.append("【每渠道验证出现次数（round / shop / phase / type）】")
    if res["verif"]:
        lines.append(f"{'round':>6}{'shop':<10}{'phase':<10}{'type':<12}{'count':>8}")
        for v in res["verif"]:
            lines.append(f"{v['round_id']:>6}{v['shop_key']:<10}{v['phase']:<10}"
                         f"{v['verification_type']:<12}{v['count']:>8}")
    else:
        lines.append("（无验证出现）")

    lines.append("")
    lines.append("【按轮次×北京时间小时分布】")
    if res["verif_hours"]:
        lines.append(f"{'round':>6}{'hour':>6}{'count':>8}")
        for h in res["verif_hours"]:
            lines.append(f"{h['round_id']:>6}{h['hour']:>6}{h['count']:>8}")
    else:
        lines.append("（无验证出现）")
    return "\n".join(lines)


def _fmt(v: float | None) -> str:
    return f"{v:+.3f}" if v is not None else "—"


def write_csv(path: Path, res: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["type", "key", "value"])
        for s in res["buckets"]:
            w.writerow(["bin", s["bin"], f"{s['triggers']}/{s['events']} "
                        f"({_fmt_rate(s['rate'])})"])
        w.writerow(["corr_interval_pearson", "", _fmt(res["corr_interval"]["pearson"])])
        w.writerow(["corr_interval_spearman", "", _fmt(res["corr_interval"]["spearman"])])
        w.writerow(["corr_seq_pearson", "", _fmt(res["corr_seq"]["pearson"])])
        w.writerow(["corr_seq_spearman", "", _fmt(res["corr_seq"]["spearman"])])
        for v in res["verif"]:
            w.writerow(["verif", f"round={v['round_id']} shop={v['shop_key']} "
                        f"{v['phase']} {v['verification_type']}", v["count"]])


def maybe_chart(res: dict, path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    labs = [s["bin"] for s in res["buckets"]]
    ev = [s["events"] for s in res["buckets"]]
    trig = [s["triggers"] for s in res["buckets"]]
    rates = [(s["rate"] or 0.0) * 100 for s in res["buckets"]]
    fig, ax1 = plt.subplots(figsize=(9, 5))
    ax1.bar(labs, ev, color="#cfe3f7", label="事件数")
    ax1.bar(labs, trig, color="#f5a0a0", label="触发数")
    ax1.set_ylabel("事件 / 触发")
    ax1.legend(loc="upper left")
    ax2 = ax1.twinx()
    ax2.plot(labs, rates, "o-", color="#2a6fbb", label="触发率 %")
    ax2.set_ylabel("触发率 %")
    ax2.set_ylim(0, max(max(rates) * 1.2, 5))
    ax2.legend(loc="upper right")
    plt.title("按间隔分桶的验证触发率")
    plt.xticks(rotation=30)
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close(fig)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=None, help="数据库路径，默认 data/bestseller.db")
    ap.add_argument("--round", type=int, default=None, help="只分析指定 round_id")
    ap.add_argument("--out", type=Path, default=None, help="输出目录，默认 output")
    ap.add_argument("--chart", action="store_true", help="生成柱状图（需 matplotlib）")
    args = ap.parse_args(argv)

    db_path = args.db or (Path(__file__).resolve().parents[1] / "data" / "bestseller.db")
    out_dir = args.out or (Path(__file__).resolve().parents[1] / "output")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        res = analyze(conn, args.round)
    finally:
        conn.close()

    report = render_report(res)
    print(report)
    txt_path = out_dir / f"延迟分析_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    csv_path = out_dir / f"延迟分析_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text(report + "\n", encoding="utf-8")
    write_csv(csv_path, res)
    print(f"\n已保存：{txt_path}\n已保存：{csv_path}")
    if args.chart:
        chart_path = out_dir / f"延迟分析_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        if maybe_chart(res, chart_path):
            print(f"已保存图：{chart_path}")
        else:
            print("未生成图：需要 matplotlib（pip install matplotlib）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
