"""界面取数 module：三个页面（开始 / 过程 / 结果）的取数与渲染文案。

界面每约 2 秒刷新一次，取数入口只有这一个 module；`gui.Api` 持有界面会话事实
（`UiState`）与连接生命周期，取数本身在这里。返回的是页面直接要的原始 dict——
键就是 `bestseller_monitor/pages/ui_live.html` 读的那些，也是测试断言的那些。

两条输入约定：

- 「现在」由调用方给（`now`，UTC ISO 时刻）：今天的北京日期一律 `cst_date(now)` 折算，
  这个 module 不读挂钟，测试与基准因此能固定日期。
- 连接由调用方给：这里只接 `conn`，不负责开合。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import click_events, plan_step, rounds, weekly_plan
from .config import effective_pages_limit, is_merge_only
from .crawler_identity import CrawlerProcess
from .db import CST, Database, RoundTally, cst_date


@dataclass(frozen=True)
class UiState:
    """界面会话事实：界面自己记着的，既不是数据层事实，也不是采集进程身份。

    采集进程身份（库里那行 pid/轮次）不在这里——它由会话锁与身份行回答，
    见 crawler_identity（`current()` / `is_running()`）。
    """

    round_id: int | None = None
    crawler_running: bool = False
    manually_paused: bool = False
    stopping: str | None = None
    stop_grace_sec: float = 0.0
    elapsed_sec: float = 0.0
    start_error: str | None = None
    # 本界面这一次、属于本周的开轮前准备（ADR-0035）：闸门与「未能确认最新」的事实源
    # 是它，不是库里的计划表——库里的表回答「本地有没有」，它回答「这次确认到没有」。
    prep: plan_step.PrepResult | None = None
    # 准备正在跑、且还在 30 秒预算里（超时兜底开了窗，或刚点了「重试准备」）：
    # 页面锁住勾选与开始。预算用完（`plan_waiting` 还在）就不再拦着人。
    plan_confirming: bool = False
    # 准备还在跑（不限预算）：页面接着轮询，落定后自动更新。
    plan_waiting: bool = False


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


@dataclass(frozen=True)
class _ShopMark:
    """一家店的计划标注：pill 上的短文案、种类（页面据此上色）与悬浮说明。

    `kind` 三种：`mine`（本机份额，绿）/ `other`（归别机，琥珀）/ `out`（计划没说
    到，灰）；空串 = 不挂标（没有计划）。`label` 是长理由（越权那句，与记账同源）。
    """

    text: str = ""
    kind: str = ""
    label: str = ""


def _plan_mark(shop_key: str, plan: plan_step.StartPlan,
               deviation: plan_step.PlanDeviation | None) -> _ShopMark:
    """这家店的计划标注（ADR-0035）：本机 `本机`、归别机 `归 m2`、计划没说到 `计划外`。

    没有计划（闸门 / 命令行逃生口）不挂标——那层话由横幅说。标注是标记不是锁：
    越权店照旧可勾；它的长理由随 `label` 一起交出，由页面挂成悬浮说明。
    """
    if plan.local is None:
        return _ShopMark()
    if shop_key in plan.my_shops:
        return _ShopMark("本机", "mine")
    if deviation is not None and deviation.kind is plan_step.DeviationKind.OVERREACH:
        return _ShopMark(f"归 {deviation.planned_machine}", "other", deviation.reason)
    return _ShopMark("计划外", "out")


def _start_shops(conn: sqlite3.Connection, shops, cfg, today: str, *,
                 plan: plan_step.StartPlan) -> list[dict]:
    out = []
    plan_pages = plan.local.pages() if plan.local is not None else None
    # 越权店（店在计划内、本周归别人）：点名它归谁（票据 07）——判定与文案跟记账同源
    # （plan_step），界面只负责把它挂进标注的悬浮说明。
    deviations = {d.shop_key: d
                  for d in plan_step.plan_deviations(plan.local, plan.machine,
                                                     [shop.key for shop in shops])}
    for shop in shops:
        products, skus = _inventory_counts(conn, today, shop.key)
        # 该店本轮实际翻页上限：四层优先取第一个有值的（命令行覆盖 > 计划快照 >
        # 店铺 pages > 全局默认），与抓取逻辑同一处口径
        pages = effective_pages_limit(shop, cfg, plan_pages=plan_pages)
        mark = _plan_mark(shop.key, plan, deviations.get(shop.key))
        out.append({
            "key": shop.key,
            "name": shop.name,
            "products": products,
            "skus": skus,
            "pages": pages,
            # 默认勾选 = 本机份额（本周计划里归本机的店，票据 06）；越权店默认不勾、
            # 可显式勾上
            "default_checked": shop.key in plan.my_shops,
            "plan_label": mark.label,
            "plan_mark": mark.text,
            "plan_mark_kind": mark.kind,
        })
    return out


def _crawler_hint(running: CrawlerProcess) -> str:
    """启动页在「已经有采集在跑」时说什么：谁在跑，以及点开始会被拒。"""
    parts = [
        f"轮次 #{running.round_id}" if running.round_id is not None else None,
        f"PID {running.pid}" if running.pid else None,
        f"{_fmt_hhmm(running.started_at)} 起" if running.started_at else None,
    ]
    who = "，".join(part for part in parts if part) or "身份未知"
    return (f"采集进程正在跑（{who}）：同一时刻只能有一个，现在点开始会被拒绝；"
            "要停它就用下面的「中止」按钮。")


def start_view(conn: sqlite3.Connection, *, cfg, shops, state: UiState,
               crawler: CrawlerProcess | None, now: str) -> dict:
    """开始页取数：今日大盘、每店今日进度、以及「点开始会发生什么」的提示。

    计划情况（闸门、默认勾选、标注、页数）读 **plan_step.start_plan** 那一份
    （ADR-0035，与拦开轮是同一处判定）：本地没有本周计划就停在闸门——不勾不开，
    给理由与「重试准备」；本地有那份而这次没确认到最新，就标「未能确认最新」、
    照常可采。本机本周没店（空手）是合法正常态：照常给出店铺表，另附一句说明。
    纯汇总机照常给这一页，但第一句就说清它不做采集（票据 11）。
    """
    today = cst_date(now)
    db = Database(conn)
    plan = plan_step.start_plan(db, cfg, weekly_plan.week_label(today), state.prep)
    idle = plan.local is not None and not plan.my_shops
    notes = []
    if state.plan_confirming:
        notes.append("正在确认本周计划…（拉计划库 → 同步清单 → 确认计划）："
                     "确认完这一页会自动更新。")
    if is_merge_only(cfg):
        # 采集入口在这台机器上是被拒的（判定与命令行同源，见 plan_step.MERGE_ONLY_REFUSAL）：
        # 把话说在点「开始抓取」之前，而不是等人点了才知道。
        notes.append(plan_step.MERGE_ONLY_REFUSAL)
    if plan.week_changed is not None:
        notes.append(f"本周已变：这个界面确认的是 {plan.week_changed} 那一周的计划，"
                     "不是当前这一周——要按新周计划开轮，重新打开界面"
                     "（窗口只在打开时确认一次计划）。")
    if plan.blocked:
        notes.append(plan.reason)
    if idle:
        notes.append(f"本周计划（{plan.week}）里没有归本机的店（空手）——合法状态，不用开轮。")
    if plan.stale:
        notes.append("未能确认最新：这次没从计划库确认到本周计划，用的是本地已落库的那份"
                     "（发布后本周不重算，本地即权威）。")
    ov_products, ov_skus = _inventory_counts(conn, today)
    current = rounds.active_round(db, today)
    stale = None if current is not None else rounds.active_round(db)
    # 今天已有进行中的轮次时，点「开始抓取」是续跑——范围以轮次自身为准（与 run.py
    # 同一条规则），与闸门无关；所以闸门只锁「新建一轮」。
    locked = state.plan_confirming or (plan.blocked and current is None)
    if is_merge_only(cfg):
        # 本机不做采集：页首那句已经说清，这里不再给「点开始抓取会…」这类承诺
        # （hint 只谈采集；库里留着进行中的轮次也不改这条）。
        hint = ""
    elif crawler is not None:
        hint = _crawler_hint(crawler)
    elif current is not None:
        hint = (f"轮次 #{current.id} 正在进行（{current.run_date}），"
                "点「开始抓取」会按它的店铺范围续跑。")
    elif stale is not None:
        hint = (f"轮次 #{stale.id}（{stale.run_date}）已经跨天，不会再续跑；"
                + ("本周计划还没确认，先新建不了新一轮。" if locked
                   else "点「开始抓取」会新建一轮。"))
    else:
        hint = ""
    return {
        "ov": {"products": ov_products, "skus": ov_skus},
        "summary": _start_summary(conn, today),
        "shops": _start_shops(conn, shops, cfg, today, plan=plan),
        "total_shops": len(shops),
        "start_hint": hint,
        "plan_idle": idle,
        "plan_note": "\n".join(notes),
        "plan_gate": plan.blocked,
        "plan_locked": locked,
        "plan_confirming": state.plan_confirming,
        "plan_waiting": state.plan_waiting,
        "plan_retry": plan.blocked or plan.stale,
        # 页面按 `d.crawler.round_id` 说话，跨 pywebview 那一步走 JSON：给回普通 dict。
        "crawler": crawler.to_payload() if crawler is not None else None,
        "stopping": state.stopping,
        "stop_grace_sec": state.stop_grace_sec,
    }


# ---------- 过程页与结果页共用 ----------

def _shop_span(conn: sqlite3.Connection, round_id: int,
               shop_key: str) -> tuple[int, float | None]:
    """该店本轮的事件跨度：（deny 数、耗时秒）。

    deny 数是**事件条数**不是卡片数：同一张卡在 deny 阶梯上的第 1/2/3 次各发一条
    （见 `click_events.denied`），界面要看的正是这家店吃了多少 deny 压力。
    失败率那边按卡片去重，两个单位刻意不同（见 `db.click_card_failures`）。
    """
    deny = conn.execute(
        "SELECT COUNT(*) c FROM event_log WHERE round_id=? AND shop_key=? AND event=?",
        (round_id, shop_key, click_events.ClickOutcome.DENIED.value),
    ).fetchone()["c"]
    span = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM event_log WHERE round_id=? AND shop_key=?",
        (round_id, shop_key),
    ).fetchone()
    return int(deny), (_duration_seconds(span[0], span[1]) if span else None)


def _shop_breakdown(conn: sqlite3.Connection, round_id: int,
                    tally: RoundTally) -> tuple[list[dict], list[dict]]:
    """本轮各店的（已完成明细、未完成清单），按店铺编号排序。

    每店的商品数/SKU 行数从整轮 tally 里切片取（`RoundTally.shop()`）：口径在
    数据层一处定义，且不给每家店各跑一遍查询（IS-38 的护栏）。
    """
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
        one = tally.shop(shop_key)
        deny, duration = _shop_span(conn, round_id, shop_key)
        done.append({
            "key": shop_key,
            "name": row["shop_name"],
            "products": one.success_offers,
            "skus": one.success_skus,
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
    # deny 按事件条数计，与 `_shop_span` 同一口径（不是卡片数）。
    deny = conn.execute(
        "SELECT COUNT(*) c FROM event_log WHERE round_id=? AND event=?",
        (round_id, click_events.ClickOutcome.DENIED.value),
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
    tally = Database(conn).round_tally(round_id)
    done, todo = _shop_breakdown(conn, round_id, tally)
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
    tally = db.round_tally(round_id)
    done, todo = _shop_breakdown(conn, round_id, tally)
    total, deny = _round_counts(conn, round_id)
    products_total, skus_total = tally.success_offers, tally.success_skus
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
