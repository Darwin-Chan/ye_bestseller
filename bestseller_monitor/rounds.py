"""轮次 module：轮次身份（北京日期 + 店铺范围）、续跑资格与终态规则。

界面、命令行、采集调度与摘要工具都从这里读轮次事实，不再各自拼查询。
日期与「现在」由调用方传入，判定本身不读挂钟。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .db import (
    CST,
    DAY_BOUNDARY_NOTE,
    DAY_CUTOFF,
    Database,
    DayBoundaryReached,
    utcnow,
)

_ROUND_COLUMNS = "id, run_date, terminal_reason, started_at, finished_at"


class TerminalReason(str, Enum):
    """轮次终态；「进行中」由没有终态表达。"""

    COMPLETED = "COMPLETED"
    FAIL_RATE_EXCEEDED = "FAIL_RATE_EXCEEDED"
    DETAIL_BUDGET_EXHAUSTED = "DETAIL_BUDGET_EXHAUSTED"
    DAY_BOUNDARY = "DAY_BOUNDARY"
    DENY_EXCEEDED = "DENY_EXCEEDED"
    ABANDONED = "ABANDONED"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"


class ScopeMismatch(RuntimeError):
    """同一日期已经有一轮在进行，但店铺范围与本次不同。

    店铺范围是轮次身份的一部分，所以这种情况既不并入、也不自动换轮。
    """


class RoundAlreadyFinished(RuntimeError):
    """轮次已落入别的终态，不允许改写。"""


@dataclass(frozen=True)
class ShopScope:
    """轮次范围内的一个店铺：名称与地址随轮次一起固定下来。"""

    key: str
    url: str
    name: str


@dataclass(frozen=True)
class RoundRequest:
    """一次启动意图：哪一天、哪些店铺。"""

    run_date: str
    shops: tuple[ShopScope, ...]

    @property
    def shop_keys(self) -> tuple[str, ...]:
        return tuple(sorted({shop.key for shop in self.shops}))


@dataclass(frozen=True)
class Round:
    id: int
    run_date: str
    shop_keys: tuple[str, ...]
    reason: TerminalReason | None
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def in_progress(self) -> bool:
        return self.reason is None

    def resumable_on(self, now) -> bool:
        """在 now 这个时刻还能不能续跑：仍进行中，且轮次日期就是 now 的北京日期。

        截止线不影响续跑资格——它管的是「要不要开始新的详情」，见 stops_work()。
        """
        return self.reason is None and _cst_moment(now).strftime("%Y-%m-%d") == self.run_date

    def stops_work(self, now) -> bool:
        """在 now 这个时刻要不要停：已终态、已跨日，或已到当日截止线。"""
        if self.reason is not None:
            return True
        moment = _cst_moment(now)
        if moment.strftime("%Y-%m-%d") != self.run_date:
            return True
        return (moment.hour, moment.minute) >= DAY_CUTOFF


@dataclass(frozen=True)
class OpenResult:
    round: Round
    created: bool
    superseded: tuple[Round, ...] = ()


def open(db: Database, request: RoundRequest, *, now=None) -> OpenResult:
    """打开或续跑一轮。

    同日期、同店铺范围：复用进行中的那一轮。
    跨日：把过期的进行中轮次按「跨天中止」收尾，再新建一轮（收尾的在 superseded 里）。
    同日期但店铺范围不同：抛 ScopeMismatch。
    """
    active = _active_rounds(db)
    same_day = [row for row in active if row.run_date == request.run_date]
    if same_day:
        current = same_day[0]
        _require_same_scope(current, request)
        return OpenResult(round=current, created=False)
    superseded = tuple(active)
    for stale in superseded:
        finish(db, stale, TerminalReason.DAY_BOUNDARY, note=DAY_BOUNDARY_NOTE)
    created = _create_round(db, request, _utc_iso(now))
    return OpenResult(round=created, created=True, superseded=superseded)


def finish(db: Database, round: Round, reason: TerminalReason, *,
           note: str | None = None, now=None) -> Round:
    """把轮次写入终态。同一终态重复收尾可以忽略，不同终态报错。"""
    if reason is TerminalReason.LEGACY_UNKNOWN:
        raise ValueError("「历史未分类」只能由迁移写入，不能作为轮次终态提交")
    row = db.conn.execute(
        "SELECT terminal_reason FROM rounds WHERE id=?", (round.id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"轮次不存在：{round.id}")
    current = row["terminal_reason"]
    if current is not None:
        if current == reason.value:
            return round
        raise RoundAlreadyFinished(
            f"轮次 #{round.id} 已经是 {current}，不能改写成 {reason.value}"
        )
    phase = "abandoned" if reason is TerminalReason.ABANDONED else "done"
    finished_at = _utc_iso(now)
    db.conn.execute(
        "UPDATE rounds SET terminal_reason=?, phase=?, finished_at=?, note=? WHERE id=?",
        (reason.value, phase, finished_at, note, round.id),
    )
    db.conn.commit()
    return Round(id=round.id, run_date=round.run_date,
                 shop_keys=round.shop_keys, reason=reason,
                 started_at=round.started_at, finished_at=finished_at)


def active_round(db: Database, run_date: str | None = None) -> Round | None:
    """进行中的轮次；给了日期就只要那一天的。"""
    for row in _active_rounds(db):
        if run_date is None or row.run_date == run_date:
            return row
    return None


def on_date(db: Database, run_date: str) -> tuple[Round, ...]:
    """某一天的轮次，旧的在前。"""
    rows = db.conn.execute(
        f"SELECT {_ROUND_COLUMNS} FROM rounds WHERE run_date=? ORDER BY id", (run_date,)
    ).fetchall()
    return tuple(_load_round(db, row) for row in rows)


def latest(db: Database, *, finished: bool = False) -> Round | None:
    """最近一轮；finished=True 时只要已经终态的（结果页用）。"""
    where = " WHERE terminal_reason IS NOT NULL" if finished else ""
    row = db.conn.execute(
        f"SELECT {_ROUND_COLUMNS} FROM rounds{where} ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _load_round(db, row) if row is not None else None


def check_scope(db: Database, request: RoundRequest) -> None:
    """只读地问一句：这次启动会不会因为范围不同被拒绝。

    界面在拉起采集子进程之前用它给出可读的拒绝理由，浏览本身不改任何数据。
    """
    current = active_round(db, request.run_date)
    if current is not None:
        _require_same_scope(current, request)


def _require_same_scope(current: Round, request: RoundRequest) -> None:
    if current.shop_keys == request.shop_keys:
        return
    raise ScopeMismatch(
        f"轮次 #{current.id}（{current.run_date}）已在进行中，店铺范围是"
        f"{_keys_text(current.shop_keys)}；本次是{_keys_text(request.shop_keys)}。"
        "要换店铺范围，请先中止本轮。"
    )


def load(db: Database, round_id: int) -> Round:
    """按 id 读一轮的当前事实；只有轮次编号的调用方用它问判据。"""
    row = db.conn.execute(
        f"SELECT {_ROUND_COLUMNS} FROM rounds WHERE id=?", (round_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"轮次不存在：{round_id}")
    return _load_round(db, row)


def ensure_workable(db: Database, round_id: int, now) -> None:
    """开始一件新的采集工作之前问一次轮次；该停就抛 DayBoundaryReached。

    判据本身是 Round.stops_work()，这里只是给只有轮次编号的调用方一个入口。
    """
    if load(db, round_id).stops_work(now):
        raise DayBoundaryReached()


def scope_shops(db: Database, round_id: int) -> tuple[ShopScope, ...]:
    """轮次自身的店铺范围（含名称与地址）。续跑以它为准，不看当前配置。"""
    rows = db.conn.execute(
        "SELECT shop_key, shop_url, shop_name FROM shop_rounds "
        "WHERE round_id=? ORDER BY shop_key",
        (round_id,),
    ).fetchall()
    return tuple(
        ShopScope(key=row["shop_key"], url=row["shop_url"], name=row["shop_name"])
        for row in rows
    )


def _active_rounds(db: Database) -> list[Round]:
    """进行中的轮次，新的在前；「进行中」就是没有终态。"""
    rows = db.conn.execute(
        f"SELECT {_ROUND_COLUMNS} FROM rounds "
        "WHERE terminal_reason IS NULL ORDER BY id DESC"
    ).fetchall()
    return [_load_round(db, row) for row in rows]


def _load_round(db: Database, row) -> Round:
    keys = tuple(sorted({
        r["shop_key"] for r in db.conn.execute(
            "SELECT shop_key FROM shop_rounds WHERE round_id=?", (row["id"],)
        )
    }))
    raw = row["terminal_reason"]
    return Round(
        id=int(row["id"]),
        run_date=row["run_date"],
        shop_keys=keys,
        reason=TerminalReason(raw) if raw else None,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _create_round(db: Database, request: RoundRequest, started_at: str) -> Round:
    cur = db.conn.execute(
        "INSERT INTO rounds(started_at, phase, run_date) VALUES (?, 'listing', ?)",
        (started_at, request.run_date),
    )
    round_id = int(cur.lastrowid)
    for shop in request.shops:
        db.add_shop(round_id, shop.key, shop.url, shop.name)
    db.conn.commit()
    return Round(id=round_id, run_date=request.run_date,
                 shop_keys=request.shop_keys, reason=None, started_at=started_at)


def _cst_moment(now) -> datetime:
    """把 datetime 或 ISO 字符串折算成北京时间；没有时区的按 UTC 解释。"""
    if isinstance(now, str):
        moment = datetime.fromisoformat(now)
    elif isinstance(now, datetime):
        moment = now
    else:
        raise TypeError("now 必须是 datetime 或 ISO 字符串")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CST)


def _utc_iso(now) -> str:
    """调用方给的时间戳折算成 UTC ISO 字符串；没给就用当前时刻。"""
    if now is None:
        return utcnow()
    moment = now if isinstance(now, datetime) else datetime.fromisoformat(now)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _keys_text(keys) -> str:
    return "、".join(keys) if keys else "（空）"
