"""测试共用件：建轮只走轮次模块这一条路；锁名字按用例隔离。"""
from contextlib import contextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from bestseller_monitor import single_instance
from bestseller_monitor import rounds
from bestseller_monitor.db import cst_date
from bestseller_monitor.listing import ListingLoadFailed
from bestseller_monitor.rounds import RoundRequest, ShopScope

# 采集配置替身存档原始页的地方（失败路径真会写文件；用例要断言就覆盖成本地 tmp）。
_RAW_PAGE_DIR = Path(tempfile.gettempdir()) / "bestseller-test-raw"


def crawler_cfg(**overrides):
    """一个采集配置包含什么：全套默认值只在这里写一遍，用例只覆盖自己关心的键（候选 06）。

    值都取「不拖慢测试」的那一档：超时 1 毫秒、各种延迟 0、deny 阈值按 `config.toml` 的量级。
    与采集过程无关的键（`db_file` / `shop_csv` / `driver` / `ensure_dirs`）给中性默认，要用的
    用例自己覆盖。

    **这是采集配置的替身，不是 `Config` 的替身**：真读配置文件的那类用例
    （`test_config.py` / `test_run_cli.py`）照样走 `Config.from_file`。
    """
    values = {
        "max_pages_per_shop": 2,
        "timeout_ms": 1,
        "fail_rate_limit": 0.1,
        "max_attempts_per_page": 2,
        "max_detail_opportunities_per_round": 1000,
        "deny_window_minutes": 10,
        "deny_shop_limit": 7,
        "deny_round_limit": 10,
        "deny_backoff_sec": 0.0,
        "deny_retry2_backoff_sec": 0.0,
        "human_pause_minutes": 1,
        "intervention_confirmation_sec": 0,
        "shuffle_within_shop": False,
        "list_delay_sec": (0.0, 0.0),
        "action_delay_sec": (0.0, 0.0),
        "read_delay_sec": (0.0, 0.0),
        "detail_delay_sec": (0.0, 0.0),
        "long_pause_interval": (1, 1),
        "long_pause_sec": (0.0, 0.0),
        "batch_size": 1,
        "batch_rest_sec": (0.0, 0.0),
        "retry_base_sec": 0.0,
        "retry_jitter_sec": 0.0,
        # 存档目录给一个共享临时目录（与改前 test_click_listing 的默认一致）：
        # 失败路径真的会往里写原始页，默认 None 会让它崩。要断言的用例自己覆盖成本地 tmp。
        "raw_page_dir": _RAW_PAGE_DIR,
        "alarm_on_intervention": False,
        "db_file": None,
        "shop_csv": None,
        "driver": "pw_cdp",
        "ensure_dirs": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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

    遍历只看这几件事（`title` / `open` / `opened` / `page` / `url` / `offer_id` / `read` /
    `close`）；`page` 是给 `guard.ready_detail_page()` 看的假页面句柄，deny 判定看它的 url。
    """

    DENY_URL = "https://s.1688.com/bsop-punish?x=1"

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

    @property
    def page(self):
        """这次的详情页句柄：deny 的那种给 deny 地址（`is_deny_url` 认 bsop-punish）。"""
        return SimpleNamespace(url=self.DENY_URL if self._denied else self.url)

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
