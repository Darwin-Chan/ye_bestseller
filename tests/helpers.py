"""测试共用的轮次构造：建轮只走轮次模块这一条路。"""
from bestseller_monitor import rounds
from bestseller_monitor.db import cst_date
from bestseller_monitor.rounds import RoundRequest, ShopScope


def new_round(db, *shops, run_date: str | None = None) -> int:
    """建一轮（同一天同范围会复用已有轮次）并返回编号。

    shops 的元素可以是 shop_key 字符串，也可以是 (key, url, name)。
    """
    scopes = []
    for shop in shops:
        if isinstance(shop, str):
            scopes.append(ShopScope(shop, f"https://{shop}.example/", f"店铺{shop}"))
        else:
            key, url, name = shop
            scopes.append(ShopScope(key, url, name))
    opened = rounds.open(db, RoundRequest(run_date or cst_date(), tuple(scopes)))
    return opened.round.id
