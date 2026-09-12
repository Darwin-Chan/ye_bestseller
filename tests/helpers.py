"""测试共用件：建轮只走轮次模块这一条路；锁名字按用例隔离。"""
import sqlite3
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from bestseller_monitor import single_instance
from bestseller_monitor import rounds
from bestseller_monitor.db import cst_date
from bestseller_monitor.rounds import RoundRequest, ShopScope

# 整表去重的识别标记：生产语句是私有常量，用例只能按语句形状认它。
SNAPSHOT_DEDUPE_MARK = "DELETE FROM snapshots"


@contextmanager
def traced_connections(seen: list[str]):
    """拦截 sqlite3.connect，把这条连接上执行过的语句记进 seen。

    迁移跑在 connect() 内部，测试拿不到那条连接的句柄，只能这样看它执行了什么。
    """
    real_connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    with patch("sqlite3.connect", traced):
        yield


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
