"""配置与店铺清单加载。"""
from __future__ import annotations

import csv
import dataclasses
import io
import pathlib
import tomllib
from dataclasses import dataclass, replace


def _tuple2(name: str, v: object) -> tuple[float, float]:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ValueError(f"配置 {name} 需要两个数值：[最小值, 最大值]")
    a, b = float(v[0]), float(v[1])
    if a < 0 or b < a:
        raise ValueError(f"配置 {name} 区间非法：{a}-{b}")
    return (a, b)


@dataclass
class Config:
    root: pathlib.Path
    shop_csv: pathlib.Path
    db_file: pathlib.Path
    data_dir: pathlib.Path
    output_dir: pathlib.Path
    logs_dir: pathlib.Path
    screenshot_dir: pathlib.Path
    raw_page_dir: pathlib.Path
    profile_dir: pathlib.Path
    user_data_path: pathlib.Path
    chrome_path: str
    driver: str
    use_system_profile: bool
    start_browser: bool
    attach_port: int
    browser_channel: str
    headless: bool
    slow_mo_ms: int
    timeout_ms: int
    base_url: str
    human_pause_minutes: int
    intervention_confirmation_sec: float
    deny_backoff_sec: float
    deny_retry2_backoff_sec: float
    deny_window_minutes: int
    deny_shop_limit: int
    deny_round_limit: int
    max_pages_per_shop: int
    max_detail_pages_per_round: int
    max_attempts_per_page: int
    fail_rate_limit: float
    shuffle_within_shop: bool
    alarm_on_intervention: bool
    detail_delay_sec: tuple[float, float]
    long_pause_interval: tuple[int, int]
    long_pause_sec: tuple[float, float]
    batch_size: int
    batch_rest_sec: tuple[float, float]
    list_delay_sec: tuple[float, float]
    action_delay_sec: tuple[float, float]
    read_delay_sec: tuple[float, float]
    retry_base_sec: float
    retry_jitter_sec: float

    @classmethod
    def from_file(cls, path: pathlib.Path, root: pathlib.Path | None = None) -> "Config":
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        root = root or pathlib.Path(path).resolve().parent.parent

        def p(*keys: str) -> pathlib.Path:
            cur: object = raw
            for k in keys:
                cur = cur[k]  # type: ignore[index]
            return (root / str(cur)).resolve()

        run = raw["run"]
        human = raw["human"]
        browser = raw["browser"]
        paths = raw["paths"]

        return cls(
            root=root,
            shop_csv=p("paths", "shop_csv"),
            db_file=p("paths", "db_file"),
            data_dir=p("paths", "data_dir"),
            output_dir=p("paths", "output_dir"),
            logs_dir=p("paths", "logs_dir"),
            screenshot_dir=p("paths", "screenshot_dir"),
            raw_page_dir=p("paths", "raw_page_dir"),
            profile_dir=p("browser", "profile_dir"),
            user_data_path=p("browser", "user_data_path"),
            chrome_path=str(browser.get("chrome_path", "")),
            driver=str(browser.get("driver", "drission")),
            use_system_profile=bool(browser.get("use_system_profile", False)),
            start_browser=bool(browser.get("start_browser", True)),
            attach_port=int(browser.get("attach_port", 9222)),
            browser_channel=str(browser.get("channel", "msedge")),
            headless=bool(browser["headless"]),
            slow_mo_ms=int(browser["slow_mo_ms"]),
            timeout_ms=int(browser["timeout_ms"]),
            base_url=str(browser.get("base_url", "https://www.1688.com/")),
            human_pause_minutes=int(run["human_pause_minutes"]),
            intervention_confirmation_sec=float(run.get("intervention_confirmation_sec", 2.0)),
            deny_backoff_sec=float(run.get("deny_backoff_sec", 30.0)),
            deny_retry2_backoff_sec=float(run.get("deny_retry2_backoff_sec", 60.0)),
            deny_window_minutes=int(run.get("deny_window_minutes", 10)),
            deny_shop_limit=int(run.get("deny_shop_limit", 7)),
            deny_round_limit=int(run.get("deny_round_limit", 10)),
            max_pages_per_shop=int(run["max_pages_per_shop"]),
            max_detail_pages_per_round=int(run["max_detail_pages_per_round"]),
            max_attempts_per_page=int(run["max_attempts_per_page"]),
            fail_rate_limit=float(run["fail_rate_limit"]),
            shuffle_within_shop=bool(run["shuffle_within_shop"]),
            alarm_on_intervention=bool(run.get("alarm_on_intervention", True)),
            detail_delay_sec=_tuple2("human.detail_delay_sec", human["detail_delay_sec"]),
            long_pause_interval=(
                int(human["long_pause_interval"][0]),
                int(human["long_pause_interval"][1]),
            ),
            long_pause_sec=_tuple2("human.long_pause_sec", human["long_pause_sec"]),
            batch_size=int(human["batch_size"]),
            batch_rest_sec=_tuple2("human.batch_rest_sec", human["batch_rest_sec"]),
            list_delay_sec=_tuple2("human.list_delay_sec", human["list_delay_sec"]),
            action_delay_sec=_tuple2("human.action_delay_sec", human["action_delay_sec"]),
            read_delay_sec=_tuple2("human.read_delay_sec", human["read_delay_sec"]),
            retry_base_sec=float(human["retry_base_sec"]),
            retry_jitter_sec=float(human["retry_jitter_sec"]),
        )

    def replace(self, **kwargs: object) -> "Config":
        return replace(self, **kwargs)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.output_dir, self.logs_dir,
                  self.screenshot_dir, self.raw_page_dir, self.profile_dir):
            d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Shop:
    key: str
    name: str
    url: str                       # 实际抓取用的商品列表页 URL（显式 offer_list_url 优先，否则自动补）
    home_url: str | None = None    # 店铺首页 URL
    offer_list_url: str | None = None
    pages: int | None = None
    active: bool = True


def load_shops(path: pathlib.Path) -> list[Shop]:
    """读取 shops.csv；跳过空行与 # 注释行；url 去空格。"""
    if not path.exists():
        raise FileNotFoundError(
            f"找不到店铺清单：{path}\n请复制 config/shops.example.csv 为 config/shops.csv 并填入真实店铺。"
        )
    shops: list[Shop] = []
    seen: set[str] = set()
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        key = (row.get("shop_key") or "").strip()
        name = (row.get("shop_name") or "").strip()
        home = (row.get("shop_url") or "").strip()
        if not key and not home:
            continue
        if not key or not home:
            raise ValueError(f"shops.csv 行不完整：{row}")
        if key in seen:
            raise ValueError(f"shop_key 重复：{key}")
        seen.add(key)
        if not home.startswith("http"):
            raise ValueError(f"店铺 URL 非法：{home}")
        # 显式 offer_list_url 优先；否则自动补成商品列表页
        offer_list = (row.get("offer_list_url") or "").strip()
        if offer_list:
            eff = offer_list
        elif "offerlist" in home:
            eff = home
        else:
            eff = home.rstrip("/") + "/page/offerlist.htm"
        pages_raw = (row.get("pages") or "").strip()
        pages = int(pages_raw) if pages_raw.isdigit() else None
        active_raw = (row.get("active") or "").strip()
        active = (active_raw == "1")   # 仅值为 1 才有效；空/其它一律无效
        shops.append(
            Shop(key=key, name=name, url=eff, home_url=home,
                 offer_list_url=offer_list or None, pages=pages, active=active)
        )
    if not shops:
        raise ValueError("shops.csv 中没有店铺。")
    return shops
