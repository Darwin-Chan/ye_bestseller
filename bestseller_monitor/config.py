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


# 唯一的采集驱动：Playwright 连接接管 + 点击式列表（ADR-0010）。
# 这个配置键保留作过渡闸——老配置里写着别的驱动时要报错，而不是静默换一条路跑。
ONLY_DRIVER = "pw_cdp"


def _driver(v: object) -> str:
    driver = str(v or ONLY_DRIVER)
    if driver != ONLY_DRIVER:
        raise ValueError(
            f"配置 browser.driver = {driver!r} 已下线：现在只有 {ONLY_DRIVER} 一条采集路径"
            f"（见 ADR-0010）。改写这行或删掉它，否则它只会在启动时报这一句，不会生效。"
        )
    return driver


# 机器角色档位（spec §10）：采集机 = 采集 + 导出 + 汇总；纯汇总机 = 只汇总与展示。
ROLE_COLLECTOR = "collector"
ROLE_MERGE_ONLY = "merge_only"
_ROLE_GLOSS = {ROLE_COLLECTOR: "采集机", ROLE_MERGE_ONLY: "纯汇总机"}

# 凭据档位（spec §11）：只记档位、不记密钥；密钥在仓库外（~/.ssh、secrets/ 或 coscli 配置）。
# readwrite = 采集机档（git 可写、COS 限 img/* 读写）；read_only = 纯汇总机档（push/上传被拒即档位正确）。
ACCESS_READWRITE = "readwrite"
ACCESS_READ_ONLY = "read_only"
_ACCESS_TIERS = (ACCESS_READWRITE, ACCESS_READ_ONLY)


def _machine_id(machine: dict, config_path: pathlib.Path) -> str:
    machine_id = str(machine.get("machine_id") or "").strip()
    if not machine_id:
        raise ValueError(
            "配置缺 machine.machine_id：本机在交换区里的唯一编号，"
            "三台采集机写 m1/m2/m3，纯汇总机自取（如 m4）。\n"
            f"照 {config_path.with_name('config.example.toml')} 的 [machine] 一节补齐。"
        )
    return machine_id


def _role(v: object) -> str:
    role = str(v or ROLE_COLLECTOR)
    if role not in _ROLE_GLOSS:
        legal = "、".join(f'"{name}"（{gloss}）' for name, gloss in _ROLE_GLOSS.items())
        raise ValueError(f"配置 machine.role = {role!r} 非法：只接受 {legal}。")
    return role


def _access(key: str, v: object) -> str:
    tier = str(v or ACCESS_READWRITE)
    if tier not in _ACCESS_TIERS:
        raise ValueError(
            f"配置 machine.{key} = {tier!r} 非法："
            f'只接受 "{ACCESS_READWRITE}" 或 "{ACCESS_READ_ONLY}"。'
        )
    return tier


@dataclass
class Config:
    root: pathlib.Path
    shop_csv: pathlib.Path
    db_file: pathlib.Path
    data_dir: pathlib.Path
    logs_dir: pathlib.Path
    screenshot_dir: pathlib.Path
    raw_page_dir: pathlib.Path
    user_data_path: pathlib.Path
    chrome_path: str
    driver: str  # 唯一采集驱动，只接受 ONLY_DRIVER（见 ADR-0010）
    start_browser: bool
    attach_port: int
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
    max_detail_opportunities_per_round: int
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
    # 机器身份与交换区（spec §10，配置里的 [machine] 一节）
    machine_id: str
    role: str                 # ROLE_COLLECTOR / ROLE_MERGE_ONLY
    exchange_root: pathlib.Path
    cos_bucket: str           # 图片通道的桶名（不记密钥；空 = 没配，导出时点名）
    git_access: str           # ACCESS_READWRITE / ACCESS_READ_ONLY（只记档位，不记密钥）
    cos_access: str
    # 命令行显式指定的翻页上限（None = 没有显式覆盖）；优先级见 effective_pages_limit()
    pages_per_shop_override: int | None = None

    @classmethod
    def from_file(cls, path: pathlib.Path, root: pathlib.Path | None = None) -> "Config":
        try:
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"找不到配置：{path}\n"
                f"请复制 {path.with_name('config.example.toml')} 为 {path}，"
                "再改成本机的值（machine.machine_id、运行目录与交换区根）。"
            ) from None
        root = root or pathlib.Path(path).resolve().parent.parent

        def p(*keys: str) -> pathlib.Path:
            cur: object = raw
            for k in keys:
                cur = cur[k]  # type: ignore[index]
            return (root / str(cur)).resolve()

        run = raw["run"]
        human = raw["human"]
        browser = raw["browser"]
        machine = raw.get("machine", {})
        if not isinstance(machine, dict):
            raise ValueError(
                f"配置的 machine 必须是一张 [machine] 表（现在是 {type(machine).__name__}）："
                "机器编号、角色、交换区根与凭据档位都写在 [machine] 一节里。"
            )
        data_dir = p("paths", "data_dir")
        exchange_raw = str(machine.get("exchange_root") or "").strip()
        exchange_root = ((root / exchange_raw).resolve() if exchange_raw
                         else data_dir.parent / "exchange")
        cos_bucket = str(machine.get("cos_bucket") or "").strip()

        return cls(
            root=root,
            shop_csv=p("paths", "shop_csv"),
            db_file=p("paths", "db_file"),
            data_dir=data_dir,
            logs_dir=p("paths", "logs_dir"),
            screenshot_dir=p("paths", "screenshot_dir"),
            raw_page_dir=p("paths", "raw_page_dir"),
            machine_id=_machine_id(machine, path),
            role=_role(machine.get("role")),
            exchange_root=exchange_root,
            cos_bucket=cos_bucket,
            git_access=_access("git_access", machine.get("git_access")),
            cos_access=_access("cos_access", machine.get("cos_access")),
            user_data_path=p("browser", "user_data_path"),
            chrome_path=str(browser.get("chrome_path", "")),
            driver=_driver(browser.get("driver")),
            start_browser=bool(browser.get("start_browser", True)),
            attach_port=int(browser.get("attach_port", 9222)),
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
            max_detail_opportunities_per_round=int(run["max_detail_opportunities_per_round"]),
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
        for d in (self.data_dir, self.logs_dir, self.screenshot_dir,
                  self.raw_page_dir, self.user_data_path):
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


def effective_pages_limit(shop: Shop | None, cfg: Config) -> int:
    """该店本轮实际翻页上限，三个来源按优先级取第一个有值的。

    命令行显式覆盖（`--pages-per-shop`）> 店铺配置（shops.csv 的 `pages`）>
    全局默认（config.toml 的 `max_pages_per_shop`）。

    显式覆盖必须单独保留：只改 `max_pages_per_shop` 的话，店铺自己配了 pages
    的店仍会按店铺值翻页，命令行的冒烟参数就失效（IS-35）。
    """
    override = getattr(cfg, "pages_per_shop_override", None)
    if override is not None:
        return int(override)
    if shop is not None and shop.pages:
        return int(shop.pages)
    return int(cfg.max_pages_per_shop)


def decode_shops_bytes(raw: bytes) -> str:
    """shops.csv 的字节读法：UTF-8（带不带 BOM 都行）优先，失败退 GBK。

    读取方（`load_shops`）与同步方（`shops_sync` 判内容哈希）用同一读法：
    两侧对「同一份清单」的文本口径必须一致，否则 Excel 之类引起的编码差异
    会被当成内容变化。
    """
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def load_shops(path: pathlib.Path) -> list[Shop]:
    """读取 shops.csv；跳过空行与 # 注释行；url 去空格。"""
    if not path.exists():
        raise FileNotFoundError(
            f"找不到店铺清单：{path}\n请复制 config/shops.example.csv 为 config/shops.csv 并填入真实店铺。"
        )
    shops: list[Shop] = []
    seen: set[str] = set()
    text = decode_shops_bytes(path.read_bytes())
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
