"""点击事件的协议：编码、分类与旧事件名的解释，只有这一处。

「一张商品卡的一次详情尝试」在生产侧留下一条 `click_*` 事件，在消费侧被三个地方读：
本轮失败率（按卡片数）、分析工具（按事件次数）、界面的 deny 计数（按事件次数）。
生产者与消费者必须认同一套语义，所以事件名、卡片身份的两套编码、结果种类的分类
以及历史名的解释全部收在这里，别处不再拼字符串、也不再各写一份事件名表。

模块是纯函数式的：不持状态、不做 I/O、不自己发事件。事件继续由调用方发——生产者拿
`(事件名, kwargs)` 交给自己的 `emit`，消费者拿 `classify()` 把库里读回来的行翻译成结果。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ClickOutcome(str, Enum):
    """一次点击的结果种类；取值就是写进 `event_log` 的事件名。

    `has_offer_id` 说明这次尝试有没有走到「认出了商品编号」那一步。它做成种类的属性而不是
    一条独立字段：生产侧这六种事件里有没有编号由种类唯一决定（`click_deny` 即使知道编号也
    不写 `offer_id` 列），留成独立字段只多一个可以互相矛盾的输入。
    """

    SUBMITTED = "click_ok"              # 详情提交成功
    SKIPPED = "click_skipped"           # 跳过（原因见 SkipReason）
    UNREADABLE = "click_parse_error"    # 拿到编号，但详情读不出来
    NOT_OPENED = "click_no_popup"       # 点击后既没弹窗也没跳详情
    NO_OFFER = "click_url_notoffer"     # 打开了，但 URL 不是商品页
    DENIED = "click_deny"               # 命中 deny 阶梯

    @property
    def has_offer_id(self) -> bool:
        return self in _REACHED_A_PRODUCT


_REACHED_A_PRODUCT = frozenset({
    ClickOutcome.SUBMITTED, ClickOutcome.SKIPPED, ClickOutcome.UNREADABLE,
})

# 历史名：`click_parse_empty` 在 `58c7b55`（2026-09-09）改成 `click_parse_error` 时，消费侧
# 没跟着改。库是只增不删的事实流水，轮次 10–12 那 9 条仍要用同一个种类读回来，不做数据迁移。
_LEGACY_EVENT_NAMES = {"click_parse_empty": ClickOutcome.UNREADABLE}

_OUTCOMES_BY_NAME = {outcome.value: outcome for outcome in ClickOutcome}

#: 协议认得的全部事件名，含历史名——消费侧的 SQL 白名单用它，别再各自列一遍。
ALL_EVENT_NAMES = tuple(_OUTCOMES_BY_NAME) + tuple(_LEGACY_EVENT_NAMES)


class SkipReason(str, Enum):
    """跳过的两种原因。目前两个消费点同等看待，这个区分是给「要分开统计时」留的显式位置。"""

    TODAY = "today"           # 同日已有库存
    DUPLICATE = "duplicate"   # 同一次遍历里已经观测过这个商品


@dataclass(frozen=True)
class ClickEvent:
    """把库里的一行读回来的结果。`card` 为 `None` 表示这条 note 里没有卡片位置。"""

    outcome: ClickOutcome
    card: CardRef | None
    offer_id: str | None
    skip_reason: SkipReason | None = None
    deny_step: int | None = None
    deny_terminal: str | None = None


def classify(event: str, note: str | None) -> ClickEvent | None:
    """把一条事件行翻译成结果；不认识的事件名交回 `None`，交调用方各自忽略。"""
    outcome = _OUTCOMES_BY_NAME.get(event) or _LEGACY_EVENT_NAMES.get(event)
    if outcome is None:
        return None
    return ClickEvent(
        outcome=outcome,
        card=CardRef.from_note(note),
        offer_id=_search(_OFFER_ID_RE, note),
        skip_reason=_skip_reason(outcome, note),
        deny_step=_deny_step(outcome, note),
        deny_terminal=_deny_terminal(outcome, note),
    )


def _search(pattern: re.Pattern[str], note: str | None) -> str | None:
    found = pattern.search(note) if note else None
    return found.group(1) if found else None


def _matches(pattern: re.Pattern[str], note: str | None) -> bool:
    return bool(note) and pattern.search(note) is not None


# ---------- 生产者侧：交回 `(事件名, kwargs)` 给调用方自己的 emit ----------

def ok(card: CardRef, offer_id: str, skus: int) -> tuple[str, dict]:
    """提交成功，带上这次观测到的 SKU 行数。"""
    return ClickOutcome.SUBMITTED.value, {
        "offer_id": offer_id, "note": _note(card, offer_id, f"&sku={skus}")}


def skipped(card: CardRef, offer_id: str, reason: SkipReason) -> tuple[str, dict]:
    """跳过：同日已有库存，或本轮已经观测过这个商品。"""
    suffix = "&dup=1" if reason is SkipReason.DUPLICATE else ""
    return ClickOutcome.SKIPPED.value, {
        "offer_id": offer_id, "note": _note(card, offer_id, suffix)}


def unreadable(card: CardRef, offer_id: str) -> tuple[str, dict]:
    """认出了商品编号，但详情页读不出来。"""
    return ClickOutcome.UNREADABLE.value, {
        "offer_id": offer_id, "note": _note(card, offer_id)}


def not_opened(card: CardRef) -> tuple[str, dict]:
    """点击后既没弹窗也没跳到详情：商品编号无从谈起。"""
    return ClickOutcome.NOT_OPENED.value, {"note": card.note}


def no_offer(card: CardRef) -> tuple[str, dict]:
    """详情打开了，但地址不是商品页。"""
    return ClickOutcome.NO_OFFER.value, {"note": card.note}


def denied(card: CardRef, step: int, terminal: str | None = None) -> tuple[str, dict]:
    """在 deny 阶梯上的第 `step` 次；`terminal` 是这一档的收场方式。

    只有 deny 走 `phase="detail"`，其余五种都在 listing 阶段——这条差别原来散在
    调用点的 `phase=` 实参里，现在跟着事件名一起收在这里。
    """
    suffix = f"&n={step}" + (f"&{terminal}" if terminal else "")
    return ClickOutcome.DENIED.value, {"phase": _DENY_PHASE, "note": card.note + suffix}


def _note(card: CardRef, offer_id: str | None = None, suffix: str = "") -> str:
    """事件备注：卡片位置 +（可选）商品编号 +（可选）后缀。"""
    note = card.note
    if offer_id is not None:
        note += "&offer_id=" + offer_id
    return note + suffix


def _skip_reason(outcome: ClickOutcome, note: str | None) -> SkipReason | None:
    if outcome is not ClickOutcome.SKIPPED:
        return None
    return SkipReason.DUPLICATE if _matches(_DUPLICATE_RE, note) else SkipReason.TODAY


def _deny_step(outcome: ClickOutcome, note: str | None) -> int | None:
    if outcome is not ClickOutcome.DENIED:
        return None
    step = _search(_DENY_STEP_RE, note)
    return int(step) if step else None


def _deny_terminal(outcome: ClickOutcome, note: str | None) -> str | None:
    if outcome is not ClickOutcome.DENIED:
        return None
    return _search(_DENY_TERMINAL_RE, note)


@dataclass(frozen=True)
class CardRef:
    """一张商品卡的身份：它在列表页上的位置。

    两套字符串编码都已经落进持久数据，都要保留：`note` 是事件备注（`event_log` 只增不删，
    历史行全是这个样子），`ref` 是详情机会账本上的标识（`detail_opportunities.identity`）。
    身份本身是同一个值对象，这两套只是它的投影。
    """

    page: int
    index: int

    @property
    def note(self) -> str:
        """事件备注里的位置；`&offer_id=` 等后缀由各事件自己接在后面。"""
        return f"page={self.page}&idx={self.index}"

    @property
    def ref(self) -> str:
        """详情机会账本上的标识：同一轮内重复命中同一张卡也认得出。"""
        return f"card:p{self.page}:i{self.index}"

    @classmethod
    def from_note(cls, note: str | None) -> "CardRef | None":
        """从事件备注里读回卡片身份；读不出来交回 `None`，不猜。

        `search` 而不是 `match`：位置在最前面，后缀接在它后面。别的 note 格式
        （例如同名暂缓的 `@page=.. @idx=..`）刻意匹配不上。
        """
        if not note:
            return None
        found = _CARD_POS_RE.search(note)
        if found is None:
            return None
        return cls(page=int(found.group(1)), index=int(found.group(2)))


_CARD_POS_RE = re.compile(r"page=(\d+)&idx=(\d+)")
_OFFER_ID_RE = re.compile(r"&offer_id=([^&]+)")
_DUPLICATE_RE = re.compile(r"&dup=1")
_DENY_STEP_RE = re.compile(r"&n=(\d+)")
_DENY_TERMINAL_RE = re.compile(r"&(round_abort|shop_skip|skip)")

#: 只有 deny 落在详情阶段，其余五种点击事件都在列表阶段。
_DENY_PHASE = "detail"
