"""测试共用件：建轮只走轮次模块这一条路；锁名字按用例隔离。"""
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from bestseller_monitor import single_instance
from bestseller_monitor import rounds
from bestseller_monitor.db import cst_date
from bestseller_monitor.rounds import RoundRequest, ShopScope


@contextmanager
def isolated_locks():
    """给本用例一套自己的界面锁/采集锁名字。

    真实运行时的锁是机器级的：不隔离的话，本机正在跑的界面或采集会让测试莫名其妙地
    「已有任务在运行」。锁本身照常走内核对象，只是换个名字。
    """
    suffix = uuid4().hex
    with patch.object(single_instance, "GUI_LOCK", rf"Local\bestseller_test_gui_{suffix}"), \
            patch.object(single_instance, "CRAWLER_LOCK", rf"Local\bestseller_test_crawler_{suffix}"):
        yield


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
