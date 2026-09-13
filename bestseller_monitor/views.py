"""界面取数 module：三个页面（开始 / 过程 / 结果）的取数与渲染文案。

界面每约 2 秒刷新一次，取数入口只有这一个 module；`gui.Api` 持有界面会话事实
（`UiState`）与连接生命周期，取数本身在这里。返回的是页面直接要的原始 dict——
键就是 `docs/ui_live.html` 读的那些，也是测试断言的那些。

两条输入约定：

- 「现在」由调用方给（`now`，UTC ISO 时刻）：今天的北京日期一律 `cst_date(now)` 折算，
  这个 module 不读挂钟，测试与基准因此能固定日期。
- 连接由调用方给：这里只接 `conn`，不负责开合。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import rounds
from .config import effective_pages_limit
from .db import CST, Database, cst_date


@dataclass(frozen=True)
class UiState:
    """界面会话事实：界面自己记着的，既不是数据层事实，也不是采集进程身份。

    采集进程身份（库里那行 pid/轮次）不在这里——它由会话锁与身份行回答，
    见 single_instance 与 gui.Api.crawler_identity()。
    """

    round_id: int | None = None
    crawler_running: bool = False
    manually_paused: bool = False
    stopping: str | None = None
    stop_grace_sec: float = 0.0
    elapsed_sec: float = 0.0
    start_error: str | None = None


# ---------- 渲染：事实 → 页面上的字符串 ----------

def _fmt_hhmm(iso_utc: str | None) -> str:
    if not iso_utc:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%H:%M")
    except ValueError:
        return "—"


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "进行中"
    s = int(seconds)
    if s < 60:
        return f"{s} 秒"
    m = s // 60
    if m < 60:
        return f"{m} 分"
    h, m = divmod(m, 60)
    return f"{h} 时 {m} 分"


def _fmt_minutes(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    return f"{round(seconds / 60, 1)} 分"


_TERMINAL_TEXT = {
    rounds.TerminalReason.COMPLETED: ("正常完成", "本轮正常完成。"),
    rounds.TerminalReason.DAY_BOUNDARY: (
        "跨天中止",
        "本轮因库存数据即将跨天而中止，已抓取数据已保留；请0点后启动新的抓取轮次。",
    ),
    rounds.TerminalReason.DENY_EXCEEDED: (
        "意外中止",
        "本轮因整轮 deny 达到阈值而意外中止，已抓取数据已保留；本轮不可续跑，请启动新的抓取轮次。",
    ),
    rounds.TerminalReason.FAIL_RATE_EXCEEDED: (
        "暂停待处理",
        "本轮因失败率超过阈值而暂停，需人工决策；未抓取店铺见下方。",
    ),
    rounds.TerminalReason.DETAIL_BUDGET_EXHAUSTED: (
        "预算耗尽",
        "本轮详情预算已用尽，已抓取数据已保留；剩余商品留待下一轮重新发现。",
    ),
    rounds.TerminalReason.ABANDONED: (
        "人工中止",
        "本轮由人工中止（放弃），已抓取数据已保留、不再续跑；未抓取店铺见下方。",
    ),
}


def _terminal_text(reason) -> tuple[str, str]:
    """轮次终态 → 结果页的标签与说明。改文案不影响任何判定。"""
    if reason is None:
        return "进行中", "本轮仍在进行；未抓取店铺见下方。"
    return _TERMINAL_TEXT.get(
        reason, ("意外中止", "本轮非正常结束，已抓取数据已保留；未抓取店铺见下方。"))


def _terminal_suffix(reason) -> str:
    """开始页摘要里的一句短注；进行中的轮次不加注。"""
    return "" if reason is None else "，" + _terminal_text(reason)[0]


def _duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        return (datetime.fromisoformat(finished_at)
                - datetime.fromisoformat(started_at)).total_seconds()
    except ValueError:
        return None


# ---------- 观测计数：三个页面共用的两个口径 ----------

def _inventory_counts(conn: sqlite3.Connection, date: str,
                      shop_key: str | None = None) -> tuple[int, int]:
    """某日（可选某店）的（商品数、SKU 行数）。

    今日大盘与每店今日进度共用这一份;两个过滤片段都是字面量，不含外部输入。
    """
    where = "date=?"
    params: list = [date]
    if shop_key is not None:
        where += " AND shop_key=?"
        params.append(shop_key)
    row = conn.execute(
        f"SELECT COUNT(DISTINCT offer_id) products, COUNT(*) skus FROM inventory WHERE {where}",
        params,
    ).fetchone()
    return int(row["products"]), int(row["skus"])


def _snapshot_counts(conn: sqlite3.Connection, round_id: int,
                     shop_key: str | None = None) -> tuple[int, int]:
    """本轮（可选某店）的（成功商品数、成功 SKU 行数）；过程页与结果页共用。"""
    where = "round_id=? AND page_status='成功' AND sku_id IS NOT NULL"
    params: list = [round_id]
    if shop_key is not None:
        where += " AND shop_key=?"
        params.append(shop_key)
    row = conn.execute(
        f"SELECT COUNT(DISTINCT offer_id) products, COUNT(*) skus FROM snapshots WHERE {where}",
        params,
    ).fetchone()
    return int(row["products"]), int(row["skus"])


# ---------- 开始页 ----------

def _start_summary(conn: sqlite3.Connection, today: str) -> dict:
    today_rounds = rounds.on_date(Database(conn), today)
    if not today_rounds:
        return {"started": False, "rounds": 0, "text": "今天尚未开始"}
    lines = []
    for run in today_rounds:
        dur = _duration_seconds(run.started_at, run.finished_at)
        lines.append(
            f"{_fmt_hhmm(run.started_at)} 开始 · 跑约 {_fmt_dur(dur)}"
            f"{_terminal_suffix(run.reason)}"
        )
    return {"started": True, "rounds": len(today_rounds), "text": "\n".join(lines)}


def _start_shops(conn: sqlite3.Connection, shops, cfg, today: str) -> list[dict]:
    out = []
    for shop in shops:
        products, skus = _inventory_counts(conn, today, shop.key)
        # 该店实际翻页上限：店铺未单独配置时回落到全局默认（与抓取逻辑一致）
        pages = effective_pages_limit(shop, cfg)
        out.append({
            "key": shop.key,
            "name": shop.name,
            "products": products,
            "skus": skus,
            "pages": pages,
            "default_checked": (products < pages * 30),
        })
    return out


def _crawler_hint(running: dict) -> str:
    """启动页在「已经有采集在跑」时说什么：谁在跑，以及点开始会被拒。"""
    parts = [
        f"轮次 #{running['round_id']}" if running.get("round_id") is not None else None,
        f"PID {running['pid']}" if running.get("pid") else None,
        f"{_fmt_hhmm(running['started_at'])} 起" if running.get("started_at") else None,
    ]
    who = "，".join(part for part in parts if part) or "身份未知"
    return (f"采集进程正在跑（{who}）：同一时刻只能有一个，现在点开始会被拒绝；"
            "要停它就用下面的「中止」按钮。")


def start_view(conn: sqlite3.Connection, *, cfg, shops, state: UiState,
               crawler: dict | None, now: str) -> dict:
    """开始页取数：今日大盘、每店今日进度、以及「点开始会发生什么」的提示。"""
    today = cst_date(now)
    db = Database(conn)
    ov_products, ov_skus = _inventory_counts(conn, today)
    current = rounds.active_round(db, today)
    stale = None if current is not None else rounds.active_round(db)
    if crawler is not None:
        hint = _crawler_hint(crawler)
    elif current is not None:
        hint = (f"轮次 #{current.id} 正在进行（{current.run_date}），"
                "点「开始抓取」会按它的店铺范围续跑。")
    elif stale is not None:
        hint = (f"轮次 #{stale.id}（{stale.run_date}）已经跨天，不会再续跑；"
                "点「开始抓取」会新建一轮。")
    else:
        hint = ""
    return {
        "ov": {"products": ov_products, "skus": ov_skus},
        "summary": _start_summary(conn, today),
        "shops": _start_shops(conn, shops, cfg, today),
        "total_shops": len(shops),
        "start_hint": hint,
        "crawler": crawler,
        "stopping": state.stopping,
        "stop_grace_sec": state.stop_grace_sec,
    }


# ---------- 过程页与结果页共用 ----------

def _shop_span(conn: sqlite3.Connection, round_id: int,
               shop_key: str) -> tuple[int, float | None]:
    """该店本轮的事件跨度：（deny 数、耗时秒）。"""
    deny = conn.execute(
        "SELECT COUNT(*) c FROM event_log WHERE round_id=? AND shop_key=? AND event='click_deny'",
        (round_id, shop_key),
    ).fetchone()["c"]
    span = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM event_log WHERE round_id=? AND shop_key=?",
        (round_id, shop_key),
    ).fetchone()
    return int(deny), (_duration_seconds(span[0], span[1]) if span else None)


def _shop_breakdown(conn: sqlite3.Connection, round_id: int) -> tuple[list[dict], list[dict]]:
    """本轮各店的（已完成明细、未完成清单），按店铺编号排序。"""
    done_rows = conn.execute(
        "SELECT * FROM shop_rounds WHERE round_id=? AND list_status='完成' ORDER BY shop_key",
        (round_id,),
    ).fetchall()
    todo_rows = conn.execute(
        "SELECT * FROM shop_rounds WHERE round_id=? AND list_status!='完成' ORDER BY shop_key",
        (round_id,),
    ).fetchall()
    done = []
    for row in done_rows:
        shop_key = row["shop_key"]
        products, skus = _snapshot_counts(conn, round_id, shop_key)
        deny, duration = _shop_span(conn, round_id, shop_key)
        done.append({
            "key": shop_key,
            "name": row["shop_name"],
            "products": products,
            "skus": skus,
            "duration": _fmt_minutes(duration),
            "deny": deny,
        })
    todo = [{"key": row["shop_key"], "name": row["shop_name"]} for row in todo_rows]
    return done, todo


def _current_shop(conn: sqlite3.Connection, round_id: int, todo_keys: list[str]) -> str | None:
    """当前处理中的店：未完成里最近有事件的那家。"""
    if not todo_keys:
        return None
    row = conn.execute(
        "SELECT shop_key FROM event_log WHERE round_id=? AND shop_key IN (%s) "
        "ORDER BY id DESC LIMIT 1" % ",".join("?" for _ in todo_keys),
        (round_id, *todo_keys),
    ).fetchone()
    return row["shop_key"] if row else None


def _round_counts(conn: sqlite3.Connection, round_id: int) -> tuple[int, int]:
    total = conn.execute(
        "SELECT COUNT(*) c FROM shop_rounds WHERE round_id=?", (round_id,)
    ).fetchone()["c"]
    deny = conn.execute(
        "SELECT COUNT(*) c FROM event_log WHERE round_id=? AND event='click_deny'",
        (round_id,),
    ).fetchone()["c"]
    return total, deny


def run_view(conn: sqlite3.Connection | None, *, state: UiState, now: str) -> dict:
    """过程页取数：本轮进度、各店明细、当前处理中的店。

    `state.start_error` 已置位（子进程被拒）时不读数据库，`conn` 可以传 None。
    """
    if state.start_error is not None:
        return _refused_start(state.start_error)
    active = rounds.active_round(Database(conn), cst_date(now))
    if active is None:
        return {
            "running": state.crawler_running,
            "manually_paused": state.manually_paused,
            "has_round": False,
            "stopping": state.stopping,
            "stop_grace_sec": state.stop_grace_sec,
        }
    round_id = active.id
    done, todo = _shop_breakdown(conn, round_id)
    total, deny = _round_counts(conn, round_id)
    return {
        "running": state.crawler_running,
        "manually_paused": state.manually_paused,
        "stopping": state.stopping,
        "stop_grace_sec": state.stop_grace_sec,
        "has_round": True,
        "round_id": round_id,
        "started_hhmm": _fmt_hhmm(active.started_at),
        "elapsed_sec": max(state.elapsed_sec, 0),
        "deny": deny,
        "done_count": len(done),
        "total_count": total,
        "progress": (len(done) / total) if total else 0.0,
        "current_shop": _current_shop(conn, round_id, [row["key"] for row in todo]),
        "done": done,
        "todo": todo,
    }


def _refused_start(message: str) -> dict:
    """子进程因为「已有采集在跑」被拒绝：说清原因，别把它当成一轮跑完。"""
    return {
        "running": False,
        "manually_paused": False,
        "has_round": False,
        "start_error": message,
    }


# ---------- 结果页 ----------

def result_view(conn: sqlite3.Connection, *, state: UiState, now: str) -> dict:
    """结果页取数：最近一轮（或本界面认领的那一轮）的终态、总量与未完成店铺。"""
    db = Database(conn)
    run = (rounds.load(db, state.round_id) if state.round_id is not None
           else rounds.latest(db, finished=True))
    if run is None:
        return {
            "has_round": False,
            "stopping": state.stopping,
            "stop_grace_sec": state.stop_grace_sec,
        }
    round_id = run.id
    duration = _duration_seconds(run.started_at, run.finished_at)
    if duration is None and round_id == state.round_id:
        # 榜单未完成时轮次保持进行中、没有 finished_at，
        # 即便抓取进程已经停下也仍可续跑，所以用当前已抓时长。
        duration = state.elapsed_sec
    done, todo = _shop_breakdown(conn, round_id)
    total, deny = _round_counts(conn, round_id)
    products_total, skus_total = _snapshot_counts(conn, round_id)
    tag, note = _terminal_text(run.reason)
    return {
        "has_round": True,
        "round_id": round_id,
        "stopping": state.stopping,
        "stop_grace_sec": state.stop_grace_sec,
        "reason": run.reason.value if run.reason is not None else None,
        "started_hhmm": _fmt_hhmm(run.started_at),
        "finished_hhmm": _fmt_hhmm(run.finished_at),
        "duration_text": _fmt_dur(duration),
        "deny": deny,
        "done_count": len(done),
        "total_count": total,
        "products_total": products_total,
        "skus_total": skus_total,
        "done": done,
        "todo": todo,
        "note": note,
        "tag": tag,
    }
