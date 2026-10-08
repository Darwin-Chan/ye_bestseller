"""Shop-stage progress facts.

This module is the semantic seam between persisted shop-round rows, the crawl
pipeline, and UI projections.  Listing completeness and detail work are
intentionally separate facts; callers should use this projection instead of
combining raw columns or notes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


LISTING_PENDING = "待处理"
LISTING_FAILED = "失败"
LISTING_COMPLETE = "完成"

DETAIL_NOT_STARTED = "未开始"
DETAIL_IN_PROGRESS = "进行中"
DETAIL_INTERRUPTED = "中断"
DETAIL_COMPLETE = "完成"

NEXT_LISTING = "listing"
NEXT_DETAIL = "detail"
NEXT_NONE = "none"

DEFAULT_MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class ShopProgress:
    shop_key: str
    shop_name: str
    listing_status: str
    listing_complete: bool
    detail_status: str
    remaining_work: int
    interruption_reason: str | None
    next_action: str
    note: str | None

    @property
    def needs_work(self) -> bool:
        return self.next_action != NEXT_NONE


def _row(db, round_id: int, shop_key: str):
    row = db.conn.execute(
        "SELECT * FROM shop_rounds WHERE round_id=? AND shop_key=?",
        (round_id, shop_key),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown shop in round: {round_id}/{shop_key}")
    return row


def _pending_count(db, round_id: int, shop_key: str, max_attempts: int) -> int:
    return sum(1 for _ in db.pending_offers(round_id, max_attempts, shop_key=shop_key))


def project(db, round_id: int, shop_key: str,
            max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> ShopProgress:
    """Return the shop-stage projection for one shop.

    Legacy rows have ``detail_status='未开始'``.  Their effective detail fact is
    derived conservatively from the current pending-offer set: no pending work
    is completion; pending work remains unstarted unless a newer interruption
    was explicitly recorded.  No historical reason or timestamp is invented.
    """
    row = _row(db, round_id, shop_key)
    listing_status = str(row["list_status"])
    listing_complete = listing_status == LISTING_COMPLETE
    remaining = _pending_count(db, round_id, shop_key, max_attempts) if listing_complete else 0
    stored_detail = row["detail_status"] or DETAIL_NOT_STARTED
    interruption_reason = row["detail_note"]

    if not listing_complete:
        detail_status = stored_detail if stored_detail != DETAIL_COMPLETE else DETAIL_NOT_STARTED
        next_action = NEXT_LISTING
        note = row["list_note"]
    elif remaining == 0:
        detail_status = DETAIL_COMPLETE
        next_action = NEXT_NONE
        note = None
        interruption_reason = None
    else:
        detail_status = stored_detail
        if detail_status == DETAIL_COMPLETE:
            detail_status = DETAIL_NOT_STARTED
        next_action = NEXT_DETAIL
        note = interruption_reason

    if not note:
        note = row["list_note"] if not listing_complete else interruption_reason
    return ShopProgress(
        shop_key=shop_key,
        shop_name=row["shop_name"],
        listing_status=listing_status,
        listing_complete=listing_complete,
        detail_status=detail_status,
        remaining_work=remaining,
        interruption_reason=interruption_reason,
        next_action=next_action,
        note=note,
    )


def all_shops(db, round_id: int,
              max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[ShopProgress]:
    rows = db.conn.execute(
        "SELECT shop_key FROM shop_rounds WHERE round_id=? ORDER BY shop_key", (round_id,)
    ).fetchall()
    return [project(db, round_id, row["shop_key"], max_attempts) for row in rows]


def listing_complete_keys(db, round_id: int) -> set[str]:
    """The only resume query for listing traversal."""
    return {progress.shop_key for progress in all_shops(db, round_id)
            if progress.listing_complete}


def mark_detail_started(db, round_id: int, shop_key: str) -> None:
    row = _row(db, round_id, shop_key)
    if row["list_status"] != LISTING_COMPLETE:
        raise ValueError("detail work cannot start before the listing is complete")
    db.conn.execute(
        "UPDATE shop_rounds SET detail_status=?, detail_note=NULL "
        "WHERE round_id=? AND shop_key=?",
        (DETAIL_IN_PROGRESS, round_id, shop_key),
    )
    db.conn.commit()


def mark_detail_interrupted(db, round_id: int, shop_key: str, note: str) -> None:
    row = _row(db, round_id, shop_key)
    if row["list_status"] != LISTING_COMPLETE:
        raise ValueError("detail interruption requires a complete listing")
    db.conn.execute(
        "UPDATE shop_rounds SET detail_status=?, detail_note=? "
        "WHERE round_id=? AND shop_key=?",
        (DETAIL_INTERRUPTED, note, round_id, shop_key),
    )
    db.conn.commit()


def mark_detail_complete(db, round_id: int, shop_key: str) -> None:
    row = _row(db, round_id, shop_key)
    if row["list_status"] != LISTING_COMPLETE:
        return
    db.conn.execute(
        "UPDATE shop_rounds SET detail_status=?, detail_note=NULL "
        "WHERE round_id=? AND shop_key=?",
        (DETAIL_COMPLETE, round_id, shop_key),
    )
    db.conn.commit()


def sync_detail_after_listing(db, round_id: int, shop_key: str, max_attempts: int) -> ShopProgress:
    """Start detail only when pending work exists; otherwise record completion."""
    progress = project(db, round_id, shop_key, max_attempts)
    if not progress.listing_complete:
        return progress
    if progress.remaining_work:
        mark_detail_started(db, round_id, shop_key)
    else:
        mark_detail_complete(db, round_id, shop_key)
    return project(db, round_id, shop_key, max_attempts)


def shops_needing_work(db, round_id: int, max_attempts: int) -> list[ShopProgress]:
    return [progress for progress in all_shops(db, round_id, max_attempts)
            if progress.needs_work]


def display(progress: ShopProgress) -> dict[str, Any]:
    """Stable JSON-ready fields for views."""
    return {
        "listing_complete": progress.listing_complete,
        "detail_status": progress.detail_status,
        "remaining_work": progress.remaining_work,
        "next_action": progress.next_action,
        "note": progress.note,
    }
