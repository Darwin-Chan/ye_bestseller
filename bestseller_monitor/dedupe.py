"""同日去重与补采 module：详情机会与尝试额度的唯一实现。

调用方包括点击式列表（还不知道商品编号，先用卡片位置占位）和逐店补采
（已知商品编号）。两条路径必须共用这里的规则，否则预算和尝试次数会各自为政。
"""
from __future__ import annotations

from .db import Database, DetailBudgetExhausted, DETAIL_BUDGET_NOTE


def next_attempt(db: Database, round_id: int, shop_key: str, offer_id: str) -> int:
    """本轮该商品的下一次尝试序号。

    初次访问与补采共享同一份额度：次数接着本轮已用掉的次数继续编号。
    """
    return db.detail_attempts_used(round_id, shop_key, offer_id) + 1


def claim_detail_slot(db: Database, round_id: int, shop_key: str, identity: str,
                      budget_limit: int) -> None:
    """申请一次详情机会，预算耗尽时结束本轮。

    identity 通常是商品编号；点击式列表在打开卡片前用卡片位置代替，学到编号后
    再绑定过去。同日跳过不该走到这里——跳过不消耗预算。
    """
    grant = db.claim_detail_opportunity(round_id, shop_key, identity, budget_limit)
    if not grant.granted:
        raise DetailBudgetExhausted(DETAIL_BUDGET_NOTE)
