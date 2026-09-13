"""同日去重与补采 module：详情机会与尝试额度的唯一实现。

调用方包括点击式列表（还不知道商品编号，先用卡片位置占位）和逐店补采
（已知商品编号）。两条路径必须共用这里的规则，否则预算和尝试次数会各自为政。

一层只认两个概念：**详情机会**（进入一次商品详情流程的名额，同一商品重试复用
同一个名额）和**尝试额度**（同一商品在本轮允许进详情的次数）。
"""
from __future__ import annotations

from .db import Database, DetailBudgetExhausted, DETAIL_BUDGET_NOTE


def next_attempt(db: Database, round_id: int, shop_key: str, offer_id: str) -> int:
    """本轮该商品的下一次尝试序号。

    初次访问与补采共享同一份额度：次数接着本轮已用掉的次数继续编号。
    """
    return db.detail_attempts_used(round_id, shop_key, offer_id) + 1


def claim_slot(db: Database, round_id: int, shop_key: str, key: str,
               budget_limit: int) -> None:
    """占一次机会，`key` 是这个名额的标识。

    标识可能是商品编号（编号已知时）或卡片位置（点击式列表在打开卡片前还不知道编号）；
    对账本来说两者一样，上面的两个名字只是把这两种情形说出来。
    """
    _claim(db, round_id, shop_key, key, budget_limit)


def bind_card_to_offer(db: Database, round_id: int, shop_key: str, card_ref: str,
                       offer_id: str) -> None:
    """把卡片占用的机会绑定到它打开的商品编号。

    绑定后，同一商品在补采路径上的重试会复用这次机会。若该商品本轮已经由别处
    占过机会（例如按编号的补采先到），只保留一次，删掉卡片这一份。
    """
    if not card_ref or not offer_id or card_ref == offer_id:
        return
    card = _find(db, round_id, shop_key, card_ref, on_identity=True)
    if card is None:
        return
    if _find(db, round_id, shop_key, offer_id, on_identity=True, on_offer=True) is not None:
        db.remove_detail_opportunity(round_id, shop_key, card["identity"])
        return
    db.bind_detail_opportunity(round_id, shop_key, card["identity"], offer_id)


def release_slot(db: Database, round_id: int, shop_key: str, key: str) -> None:
    """退还一次机会：同日跳过不消耗预算。

    key 传商品编号或卡片标识都能命中，因为机会可能还挂在卡片位置上。
    """
    for row in db.detail_opportunities(round_id, shop_key):
        if key in (row["identity"], row["offer_id"]):
            db.remove_detail_opportunity(round_id, shop_key, row["identity"])


def _claim(db: Database, round_id: int, shop_key: str, key: str, budget_limit: int) -> None:
    """占一次机会：已占过的标识只算重试，否则占用新名额或结束本轮。"""
    if _find(db, round_id, shop_key, key, on_identity=True, on_offer=True) is not None:
        return
    if db.detail_opportunity_total(round_id) >= db.detail_budget_limit(round_id, budget_limit):
        raise DetailBudgetExhausted(DETAIL_BUDGET_NOTE)
    db.add_detail_opportunity(round_id, shop_key, key)


def _find(db: Database, round_id: int, shop_key: str, key: str, *,
          on_identity: bool = False, on_offer: bool = False):
    """按标识找本轮该店铺已占用的机会。"""
    for row in db.detail_opportunities(round_id, shop_key):
        if (on_identity and row["identity"] == key) or (on_offer and row["offer_id"] == key):
            return row
    return None
