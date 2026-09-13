"""测试共用件：建轮只走轮次模块这一条路；锁名字按用例隔离。"""
import sqlite3
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from bestseller_monitor import single_instance
from bestseller_monitor import rounds
from bestseller_monitor.db import cst_date
from bestseller_monitor.listing import ListingLoadFailed
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


class FakeCard:
    """脚本化的一张商品卡：标题、打开后读到什么、是不是 deny。

    遍历只看这四件事（`title` / `open` / `opened` / `denied` / `read` / `close`），
    所以它不需要任何页面对象。
    """

    def __init__(self, title: str = "", *, offer_id: str | None = "11",
                 observation=None, denied: bool = False, opened: bool = True,
                 read_error: Exception | None = None):
        from bestseller_monitor import detail

        self._title = title
        self.offer_id = offer_id
        self.url = (f"https://detail.1688.com/offer/{offer_id}.html"
                    if offer_id else "https://detail.1688.com/")
        self.observation = observation or detail.Observation(
            payload={"product_name": title or "商品", "html": "<html></html>",
                     "rows": [{"sku_name": "默认(单规格)", "sku_stock": 3}]})
        self._denied = denied
        self._opened = opened
        self._read_error = read_error
        self.note = ""          # 由 adapter 按当前页号填
        self.ref = ""
        self.opens = 0
        self.closes = 0

    def title(self) -> str:
        return self._title

    def open(self) -> None:
        self.opens += 1

    def opened(self) -> bool:
        return self._opened

    def denied(self) -> bool:
        return self._denied

    def read(self):
        if self._read_error is not None:
            raise self._read_error
        return self.observation

    def close(self) -> None:
        self.closes += 1


class ScriptedListing:
    """脚本化的点击式列表：这家店有哪几页、每页有哪些卡。

    遍历要的四件事都在这里：准备、滚动取卡片数、拿第 i 张卡、推进到下一批。
    没有下一页时 `advance` 返回 False（就是「点不到下一页/加载更多」那条路）。
    """

    def __init__(self, pages, *, punished: bool = False):
        self.pages = [list(page) for page in pages]
        self.punished = punished
        self.page_no = 0
        self.prepared: list[str] = []
        self.scrolled: list[str] = []
        self.advanced: list[str] = []

    def prepare(self, describe: str, *, emit=None) -> None:
        self.page_no = 1
        self.prepared.append(describe)

    def scroll_to_load(self, describe: str) -> int:
        self.scrolled.append(describe)
        return len(self.pages[self.page_no - 1]) if self.pages else 0

    def card(self, index: int):
        card = self.pages[self.page_no - 1][index]
        card.note = f"page={self.page_no}&idx={index}"
        card.ref = f"card:p{self.page_no}:i{index}"
        return card

    def advance(self, describe: str) -> bool:
        if self.page_no >= len(self.pages):
            return False
        self.advanced.append(describe)
        self.page_no += 1
        return True

    def load_failed(self, reason: str):
        raise ListingLoadFailed(reason)
