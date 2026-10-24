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
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import elapsed, pacing, plan_step, preparation, rounds, shop_progress, weekly_plan
from .config import CARDS_PER_PAGE, effective_pages_limit, is_merge_only
from .crawler_identity import CrawlerProcess, browser_cleanup_complete
from .db import CST, Database, RoundTally, cst_date
from .stop_request import StopReason


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
    start_error: str | None = None
    # 本界面这一次开轮前准备的一份不可变快照（ADR-0035）：闸门、「未能确认最新」、
    # 「正在确认」（锁住勾选与开始）与「接着轮询」都从它读，不再拼零散字段。
    prep: preparation.PreparationSnapshot = field(
        default_factory=preparation.PreparationSnapshot)


def stop_reason_text(reason: StopReason | None) -> str:
    return {
        StopReason.USER_PAUSE: "用户请求暂停",
        StopReason.USER_ABORT: "用户确认中止",
        StopReason.WINDOW_CLOSE: "用户确认关闭采集窗口",
    }.get(reason, "未记录")


def stop_failure_text(code: str) -> str:
    """Stable StopWatch protocol codes become user-facing reasons only here."""
    return {
        "terminate_failed": "无法强制停止采集，请检查进程权限",
        "target_replaced": "原采集进程已被替换，暂不处置新进程",
        "target_unverifiable": "暂时无法核验原采集身份",
        "database_unavailable": "数据库暂不可用",
        "observation_failed": "暂时无法读取采集进程状态",
        "browser_binding_unavailable": "暂时无法核验采集浏览器归属",
        "browser_binding_changed": "采集浏览器绑定已变化，暂不处置",
        "browser_close_failed": "采集已停止，浏览器收尾暂未完成",
        "process_still_running": "原采集进程尚未停止",
    }.get(code, "停止状态暂不可确认")


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


def _fmt_countdown(seconds: float | None) -> str:
    """倒计时文本（mm:ss；超过一小时给 h:mm:ss）——主动停顿的「还要等多久」。"""
    if seconds is None or seconds < 0:
        return "—"
    s = int(round(seconds))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _pacing_state(conn: sqlite3.Connection, round_id: int, *, now: str,
                  running: bool) -> tuple[int | None, float]:
    """主动停顿的（剩余秒数、本轮累计秒数）；没有停顿记录就是 (None, 0.0)。

    累计只算真的等过的：睡满的按 resume 事件的秒数加，正在进行的那次按已过时长加
    （进程半路没了也只算等过的那一段）。剩余只在采集进程此刻还在跑时才给——
    进程已经不在了，倒计时就是一条旧数据，不该继续走（ADR-0037）。
    """
    rows = conn.execute(
        "SELECT event, ts, note FROM event_log WHERE round_id=? AND event IN (?, ?) "
        "ORDER BY id",
        (round_id, pacing.PAUSE_EVENT, pacing.RESUME_EVENT),
    ).fetchall()
    completed = 0.0
    open_pause: tuple[str, float] | None = None
    for row in rows:
        seconds = pacing.note_seconds(row["note"]) or 0.0
        if row["event"] == pacing.RESUME_EVENT:
            completed += seconds
            open_pause = None
        else:
            open_pause = (row["ts"], seconds)
    if open_pause is None:
        return None, completed
    elapsed = _duration_seconds(open_pause[0], now)
    elapsed = elapsed if elapsed is not None and elapsed > 0 else 0.0
    planned = open_pause[1]
    ongoing = min(elapsed, planned) if planned > 0 else elapsed
    remaining = max(0, int(round(planned - elapsed))) if running else None
    return remaining, completed + ongoing


_TERMINAL_TEXT = {
    rounds.TerminalReason.IDENTITY_VERIFICATION_TIMEOUT: (
        "身份验证超时中止", "身份验证等待超时，本轮自动中止；已抓取数据已保留、不可续跑。请完成安全收尾后明确开始新轮。",
    ),
    rounds.TerminalReason.STARTUP_FAILED: (
        "启动失败", "本次新建在启动或初始化时失败，未进入采集；失败原因：{cause}；请核实收尾与配置后明确开始新轮。",
    ),
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
        "失败率超限中止",
        "本轮因失败率超过阈值而中止，已抓取数据已保留；本轮不可续跑，请完成收尾后再开始新轮；未抓取店铺见下方。",
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


def _terminal_text(reason, detail=None) -> tuple[str, str]:
    """轮次终态 → 结果页的标签与说明。改文案不影响任何判定。"""
    if reason is None:
        return "进行中", "本轮仍在进行；未抓取店铺见下方。"
    cause = detail or "未记录"
    tag, text = _TERMINAL_TEXT.get(
        reason, ("非正常结束", "本轮非正常结束；停止原因：{cause}；"
                            "已抓取数据已保留；未抓取店铺见下方。"))
    return tag, text.format(cause=cause)


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


def _round_can_resume(state: UiState, round_: rounds.Round, now: str) -> bool:
    """这一轮在该界面此刻算不算「明确暂停、可继续」——开始页与过程页共用一条。

    仅同界面明确暂停的原轮、同日、停止收尾完成且无采集进程才算数；数据库无终态、
    进程消失或别的界面留下的记录都不授予继续资格（票 03/06）。
    """
    return (state.manually_paused and state.round_id == round_.id
            and not state.crawler_running and not state.stopping
            and round_.resumable_on(now))


def _interruption_reason(conn: sqlite3.Connection, round_id: int) -> str:
    """只认本次执行保存的原因；新一次开始后，旧原因不再适用。"""
    row = conn.execute(
        "SELECT event, note FROM event_log WHERE round_id=? "
        "AND event IN ('collection_started', 'collection_interrupted') "
        "ORDER BY id DESC LIMIT 1", (round_id,),
    ).fetchone()
    if row is not None and row["event"] == "collection_interrupted" and row["note"]:
        return row["note"]
    return "未记录"


def _unfinished_text(reason: str, *, cross_day: bool = False) -> str:
    head = ("采集已停止且已跨天，本轮未正常结束、不能继续" if cross_day
            else "采集已停止，本轮未正常结束")
    return (f"{head}；停止原因：{reason}；"
            "请确认中止并完成收尾后，再开始新轮。")


def _pending_round_text(row: rounds.Round, *, today: str, resumable: bool,
                        reason: str) -> str:
    keys = "、".join(row.shop_keys) or "（无店铺）"
    head = f"轮次 #{row.id}（{row.run_date}，{keys}）"
    if resumable:
        return (head + "：已由用户主动暂停，可到过程页继续；如需换范围或新建，请先确认中止。")
    return head + "：" + _unfinished_text(reason, cross_day=row.run_date != today)


def _pending_payload(conn: sqlite3.Connection, pending: tuple[rounds.Round, ...], state: UiState,
                     now: str, today: str) -> list[dict]:
    rows = []
    for row in pending:
        resumable = _round_can_resume(state, row, now)
        rows.append({
            "id": row.id,
            "run_date": row.run_date,
            "shops": list(row.shop_keys),
            "cross_day": row.run_date != today,
            "resumable": resumable,
            "text": _pending_round_text(row, today=today, resumable=resumable,
                                        reason=_interruption_reason(conn, row.id)),
        })
    return rows


def _pending_hint(conn: sqlite3.Connection, pending: tuple[rounds.Round, ...], state: UiState,
                  now: str, today: str) -> str:
    if len(pending) == 1 and _round_can_resume(state, pending[0], now):
        row = pending[0]
        return (f"轮次 #{row.id}（{row.run_date}）已由用户主动暂停：请到过程页继续；"
                "如需新建，请先确认中止本轮。")
    heads = []
    if any(row.run_date == today and not _round_can_resume(state, row, now)
           for row in pending):
        heads.append("采集已停止，本轮未正常结束")
    if any(row.run_date != today for row in pending):
        heads.append("有轮次已跨天，不能继续")
    resumed = [row for row in pending if _round_can_resume(state, row, now)]
    if resumed:
        heads.append("、".join(f"轮次 #{row.id} 已由用户主动暂停" for row in resumed))
    ids = "、".join(f"#{row.id}（{row.run_date}）" for row in pending)
    causes = "；".join(f"#{row.id}：{_interruption_reason(conn, row.id)}"
                       for row in pending if not _round_can_resume(state, row, now))
    if causes:
        heads.append("停止原因：" + causes)
    return (f"未结束的轮次：{ids}。" + "；".join(heads)
            + "。请在下方逐个确认中止并等待收尾完成，再开始新轮。")


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
            # 默认勾选 = 本机份额（票据 06）里**今天还没采够**的（票据 17）：今日已采
            # 商品数 ≥ 页数×每页张数判「采够」，默认不勾、可手工勾上重采——同日去重
            # 只挡详情页，采够的店再来一遍只是白走列表页。越权店默认不勾、可显式勾上。
            "default_checked": (getattr(cfg, "local_trial", False) or shop.key in plan.my_shops)
            and products < pages * CARDS_PER_PAGE,
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
    照常可采。确认期内（还在 30 秒预算里）与后端共用同一处判定，锁住勾选与开始；
    本机本周没店（空手）是合法正常态：照常给出店铺表，另附一句说明。
    纯汇总机照常给这一页，但第一句就说清它不做采集（票据 11）。
    """
    today = cst_date(now)
    db = Database(conn)
    trial = getattr(cfg, "local_trial", False)
    plan = (plan_step.StartPlan(weekly_plan.week_label(today), cfg.machine_id) if trial
            else plan_step.start_plan(db, cfg, weekly_plan.week_label(today),
                                      state.prep.result, history=state.prep.history,
                                      confirming=state.prep.confirming))
    idle = plan.local is not None and not plan.my_shops
    notes = []
    if plan.confirming:
        notes.append(plan_step.CONFIRMING_GATE_REASON)
    if is_merge_only(cfg) and not trial:
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
    pending = rounds.pending_rounds(db)
    # 已终态轮次留下的收尾欠账（浏览器/身份未核实）：没有待处置轮次时，
    # 这里同样是新建被拦的原因，页面据此给出「重试收尾核验」入口。
    identity = db.crawler_process()
    residue = None
    if (crawler is None and identity is not None
            and not browser_cleanup_complete(identity["browser_state"])):
        residue = {"round_id": identity["round_id"],
                   "state": identity["browser_state"] or "UNKNOWN"}
    # 开始页仅表达新建；无终态旧轮不是「可暂停续跑」的证明。
    locked = plan.locked
    start_allowed = not locked and not (is_merge_only(cfg) and not trial)
    if state.stopping:
        hint = "上一次停止仍在核验或收尾，请等待收尾完成再明确开始新轮。"
        start_allowed = False
    elif crawler is not None:
        hint = _crawler_hint(crawler)
        start_allowed = False
    elif pending:
        hint = _pending_hint(conn, pending, state, now, today)
        start_allowed = False
    else:
        hint = ""
        try:
            rounds.check_new(db, rounds.RoundRequest(today, tuple(
                rounds.ShopScope(s.key, s.url, s.name) for s in shops)))
        except rounds.ScopeMismatch as exc:
            hint = str(exc)
            start_allowed = False
    return {
        "ov": {"products": ov_products, "skus": ov_skus},
        "summary": _start_summary(conn, today),
        "shops": _start_shops(conn, shops, cfg, today, plan=plan),
        "total_shops": len(shops),
        "start_hint": hint,
        "start_allowed": start_allowed,
        "pending": ({"rounds": _pending_payload(conn, pending, state, now, today)}
                    if pending else None),
        "residue": residue,
        "plan_idle": idle,
        "plan_note": "\n".join(notes),
        "plan_gate": plan.blocked,
        "plan_locked": locked,
        "plan_confirming": plan.confirming,
        "plan_waiting": state.prep.running,
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

    deny 按发生次数计，不按卡片去重；降温前的 deny_hit 是新版本的事实来源，
    旧版 click_deny 的兼容边界由 db.deny_count 统一处理（ADR-0063）。
    失败率那边按卡片去重，两个单位不同（见 db.click_card_failures）。
    """
    deny = Database(conn).deny_count(round_id, shop_key)
    span = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM event_log WHERE round_id=? AND shop_key=?",
        (round_id, shop_key),
    ).fetchone()
    return int(deny), (_duration_seconds(span[0], span[1]) if span else None)


def _shop_breakdown(conn: sqlite3.Connection, round_id: int,
                    tally: RoundTally, max_attempts: int = shop_progress.DEFAULT_MAX_ATTEMPTS
                    ) -> tuple[list[dict], list[dict], list[dict]]:
    """本轮各店的（完成明细、部分采集明细、待处理清单），共享店铺阶段投影。

    完成＝无剩余工作；部分采集＝已抓到商品（成功商品数 > 0）但仍有剩余工作——明细同
    完成行、另带 `partial` 事实，结果页据此加「未完成」标；待处理＝所有仍有剩余工作的店
    （含部分采集店），形状与从前一致，过程页药丸照旧消费它。「完成」与「已抓取」在这里
    分家：完成看剩余工作，已抓取看成功商品数（见 CONTEXT.md「部分采集店铺」）。
    """
    db = Database(conn)
    done = []
    partial = []
    todo = []
    for progress in shop_progress.all_shops(db, round_id, max_attempts):
        shop_key = progress.shop_key
        one = tally.shop(shop_key)
        deny, duration = _shop_span(conn, round_id, shop_key)
        base = {
            "key": shop_key,
            "name": progress.shop_name,
            **shop_progress.display(progress),
        }
        counts = {
            "products": one.success_offers,
            "skus": one.success_skus,
            "duration": _fmt_minutes(duration),
            "deny": deny,
        }
        if not progress.needs_work:
            done.append({**base, **counts})
            continue
        todo.append(base)
        if one.success_offers > 0:
            partial.append({**base, **counts, "partial": True})
    return done, partial, todo


def _pill_row(row: dict) -> dict:
    """药丸/次要清单沿旧形状：店铺标识与阶段字段，不出现计数与未完成标。"""
    return {k: v for k, v in row.items()
            if k not in ("products", "skus", "duration", "deny", "partial")}


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
    deny = Database(conn).deny_count(round_id)
    return total, deny


def _identity_wait(conn, round_id: int, state: UiState) -> dict | None:
    """Only the running round's latest verification event can represent a wait."""
    if not state.crawler_running or state.stopping or state.manually_paused:
        return None
    row = conn.execute(
        "SELECT event, verification_type, note FROM event_log "
        "WHERE round_id=? AND kind='verification' ORDER BY id DESC LIMIT 1",
        (round_id,),
    ).fetchone()
    if not row or row["event"] != "verification_appear" or row["verification_type"] != "identity":
        return None
    values = dict(part.split("=", 1) for part in (row["note"] or "").split(";") if "=" in part)
    try:
        minutes = float(values["wait_minutes"])
    except (KeyError, ValueError):
        return None
    return {"type": "identity", "wait_minutes": minutes,
            "note": f"需要人工完成身份验证；本次最长等待 {minutes:g} 分钟，超时自动中止。"}


def run_view(conn: sqlite3.Connection | None, *, state: UiState, now: str,
             max_attempts: int = shop_progress.DEFAULT_MAX_ATTEMPTS) -> dict:
    """过程页取数：本轮进度、各店明细、当前处理中的店。

    `state.start_error` 已置位（子进程被拒）时不读数据库，`conn` 可以传 None。
    跨天的轮次只有「本界面明确暂停的那一轮」还留在这一页（界面保留到次日）：
    继续被拒，说明跨天，用户可以从这里显式中止；其余历史遗留归开始页逐个处置。
    """
    if state.start_error is not None:
        return _refused_start(state.start_error)
    today = cst_date(now)
    db = Database(conn)
    active = rounds.active_round(db, today)
    cross_day = False
    if active is None and state.manually_paused and state.round_id is not None:
        # 认的就是本界面暂停的那一轮：库里可能有别的更新遗留，不能按最新一条猜。
        try:
            claimed = rounds.load(db, state.round_id)
        except ValueError:
            claimed = None
        if claimed is not None and claimed.reason is None:
            active, cross_day = claimed, True
    if active is None:
        return {
            "running": state.crawler_running,
            "manually_paused": state.manually_paused,
            "has_round": False,
            "can_resume": False,
            "can_abort": False,
            "stopping": state.stopping,
            "stop_grace_sec": state.stop_grace_sec,
        }
    round_id = active.id
    tally = db.round_tally(round_id)
    # 过程页不展示部分采集桶：完成表 + 药丸（待处理清单里含部分采集店）即旧观感，一字不动。
    done, _partial, todo = _shop_breakdown(conn, round_id, tally, max_attempts)
    total, deny = _round_counts(conn, round_id)
    timing = elapsed.read(conn, round_id)
    pacing_remaining, pacing_paused = _pacing_state(
        conn, round_id, now=now, running=state.crawler_running)
    return {
        "running": state.crawler_running,
        "manually_paused": state.manually_paused,
        "stopping": state.stopping,
        "stop_grace_sec": state.stop_grace_sec,
        "has_round": True,
        "round_id": round_id,
        "cross_day": cross_day,
        "note": (_unfinished_text(_interruption_reason(conn, round_id), cross_day=cross_day)
                 if cross_day or (not state.crawler_running and not state.stopping
                                  and not _round_can_resume(state, active, now)) else None),
        "verification": _identity_wait(conn, round_id, state),
        "can_resume": _round_can_resume(state, active, now),
        "can_abort": True,
        "started_hhmm": _fmt_hhmm(active.started_at),
        "elapsed_sec": timing.seconds,
        "elapsed_text": timing.text,
        "elapsed_quality": timing.quality,
        "elapsed_note": timing.note,
        "deny": deny,
        "pacing_remaining_sec": pacing_remaining,
        "pacing_remaining_text": _fmt_countdown(pacing_remaining),
        "pacing_paused_sec": pacing_paused,
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

def result_view(conn: sqlite3.Connection, *, state: UiState, now: str,
                max_attempts: int = shop_progress.DEFAULT_MAX_ATTEMPTS) -> dict:
    """结果页取数：最近一轮（或本界面认领的那一轮）的终态、总量与已抓取/未抓取店铺。

    「已抓取店铺」（done 键）＝抓到过商品的店——完成与部分采集按编号混排，部分行带
    partial 事实；「未抓取店铺」（todo 键）＝一件商品都没抓到的店（含完成但空手的）。
    """
    db = Database(conn)
    run = (rounds.load(db, state.round_id) if state.round_id is not None
           else rounds.latest(db, finished=True))
    if run is None:
        return {
            "has_round": False,
            "can_resume": False,
            "can_abort": False,
            "stopping": state.stopping,
            "stop_grace_sec": state.stop_grace_sec,
        }
    round_id = run.id
    timing = elapsed.read(conn, round_id)
    tally = db.round_tally(round_id)
    done, partial, todo = _shop_breakdown(conn, round_id, tally, max_attempts)
    total, deny = _round_counts(conn, round_id)
    # 「已抓取店铺」表＝抓到过商品的店（完成与部分采集按编号混排，部分行带 partial 事实）；
    # 药丸＝一件商品都没抓到的店（含完成但空手的）。done_count 仍是「完成店铺」——
    # 只数无剩余工作的店；空手的完成店计入它、但不进表。
    partial_keys = {row["key"] for row in partial}
    table = sorted(partial + [row for row in done if row["products"]],
                   key=lambda row: row["key"])
    pills = sorted([_pill_row(row) for row in done if not row["products"]]
                   + [row for row in todo if row["key"] not in partial_keys],
                   key=lambda row: row["key"])
    products_total, skus_total = tally.success_offers, tally.success_skus
    tag, note = _terminal_text(run.reason, rounds.ending_detail(Database(conn), run.id))
    return {
        "has_round": True,
        "round_id": round_id,
        "stopping": state.stopping,
        "stop_grace_sec": state.stop_grace_sec,
        "reason": run.reason.value if run.reason is not None else None,
        "started_hhmm": _fmt_hhmm(run.started_at),
        "finished_hhmm": _fmt_hhmm(run.finished_at),
        "duration_text": timing.text,
        "elapsed_sec": timing.seconds,
        "elapsed_quality": timing.quality,
        "elapsed_note": timing.note,
        "deny": deny,
        "done_count": len(done),
        "total_count": total,
        "products_total": products_total,
        "skus_total": skus_total,
        "done": table,
        "todo": pills,
        "note": note,
        "tag": tag,
    }
