"""SQLite 数据层：轮次、店铺榜单、SKU 快照、每日库存。"""
from __future__ import annotations

import logging
import sqlite3
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import click_events
from .parse import DEFAULT_SKU_ID

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_image_assets (
    content_hash TEXT PRIMARY KEY, mime TEXT NOT NULL, content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS product_information_versions (
    id INTEGER PRIMARY KEY, shop_key TEXT NOT NULL, offer_id TEXT NOT NULL,
    observed_at TEXT NOT NULL, observed_date TEXT NOT NULL, product_name TEXT,
    image_url TEXT, content_hash TEXT REFERENCES product_image_assets(content_hash),
    image_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_product_information_date
ON product_information_versions(shop_key, offer_id, observed_date, observed_at);
-- SKU 图流水（SKU 图线 spec §2/§3）：每次成功观测每个 SKU 一行（含空图与失败），字节复用
-- product_image_assets 池（内容寻址，同图不重复存）。观测表口径与版本行同规——观测时刻
-- UTC + 北京日期；观测事实写定就不回头改，下一观测自会有新行。来源三态见 SKU_IMAGE_*。
CREATE TABLE IF NOT EXISTS sku_image_versions (
    id INTEGER PRIMARY KEY, shop_key TEXT NOT NULL, offer_id TEXT NOT NULL,
    sku_id TEXT NOT NULL, observed_at TEXT NOT NULL, observed_date TEXT NOT NULL,
    image_url TEXT, content_hash TEXT REFERENCES product_image_assets(content_hash),
    image_error TEXT, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sku_image_date
ON sku_image_versions(shop_key, offer_id, sku_id, observed_date, observed_at);
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    note TEXT,
    detail_budget_limit INTEGER,
    run_date TEXT,
    terminal_reason TEXT
);

CREATE TABLE IF NOT EXISTS shops (
    shop_key TEXT PRIMARY KEY,
    shop_name TEXT,
    shop_url TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS shop_rounds (
    round_id INTEGER NOT NULL,
    shop_key TEXT NOT NULL,
    shop_url TEXT NOT NULL,
    shop_name TEXT NOT NULL,
    list_status TEXT NOT NULL DEFAULT '待处理',
    list_pages_read INTEGER DEFAULT 0,
    offer_count INTEGER DEFAULT 0,
    list_note TEXT,
    PRIMARY KEY (round_id, shop_key)
);

CREATE TABLE IF NOT EXISTS shop_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    shop_key TEXT NOT NULL,
    shop_url TEXT NOT NULL,
    shop_name TEXT NOT NULL,
    rank INTEGER NOT NULL,
    offer_id TEXT NOT NULL,
    product_url TEXT NOT NULL,
    list_title TEXT,
    list_price TEXT
);
CREATE INDEX IF NOT EXISTS idx_shop_offers_round ON shop_offers(round_id, shop_key);
-- 本轮计数要按店铺数「榜单里发现过哪些商品」，孤儿也是按这个三元组去认：
-- 少了这条，每次界面刷新都得为整轮榜单行回表（IS-38 同类问题）。
CREATE INDEX IF NOT EXISTS idx_shop_offers_key ON shop_offers(round_id, shop_key, offer_id);

CREATE TABLE IF NOT EXISTS products (
    offer_id TEXT PRIMARY KEY,
    product_url TEXT,
    product_name TEXT,
    main_image_url TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS skus (
    offer_id TEXT NOT NULL,
    sku_name TEXT,
    sku_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT,
    PRIMARY KEY (offer_id, sku_id)
);

CREATE TABLE IF NOT EXISTS inventory (
    shop_key TEXT NOT NULL,
    offer_id TEXT NOT NULL,
    sku_id TEXT NOT NULL,
    date TEXT NOT NULL,
    stock INTEGER,
    price REAL,
    shop_name TEXT,
    product_name TEXT,
    sku_name TEXT,
    PRIMARY KEY (shop_key, offer_id, sku_id, date)
);
CREATE INDEX IF NOT EXISTS idx_inventory_date ON inventory(date);
CREATE INDEX IF NOT EXISTS idx_inventory_name
    ON inventory(shop_key, product_name, date);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    shop_key TEXT NOT NULL,
    shop_url TEXT NOT NULL,
    shop_name TEXT NOT NULL,
    offer_id TEXT NOT NULL,
    product_url TEXT NOT NULL,
    product_name TEXT,
    sku_id TEXT,
    sku_name TEXT,
    sku_price REAL,
    sku_stock INTEGER,
    collected_at TEXT NOT NULL,
    page_status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    detail_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_round ON snapshots(round_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_key
    ON snapshots(shop_key, offer_id, sku_id, round_id);

CREATE TABLE IF NOT EXISTS detail_opportunities (
    round_id INTEGER NOT NULL,
    shop_key TEXT NOT NULL,
    identity TEXT NOT NULL,
    offer_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (round_id, shop_key, identity)
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    shop_key TEXT,
    offer_id TEXT,
    sku_id TEXT,
    phase TEXT,
    event TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'work',
    ts TEXT NOT NULL,
    prev_ts TEXT,
    interval_ms INTEGER,
    verification_type TEXT NOT NULL DEFAULT 'none',
    attempt INTEGER,
    config_hash TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_log_round ON event_log(round_id);
CREATE INDEX IF NOT EXISTS idx_event_log_shop_ts ON event_log(shop_key, ts);
CREATE INDEX IF NOT EXISTS idx_event_log_event ON event_log(event);
CREATE INDEX IF NOT EXISTS idx_event_log_verification ON event_log(verification_type);
-- 过程页刷新按「轮次 + 店铺」问这两件事：deny 计数、该店的时间跨度。只按 round_id
-- 索引的话，12 家店要各扫一遍本轮全部事件（IS-38 实测：30 万行一次刷新 0.80 → 0.117 秒；
-- 现在一次刷新约 0.5 秒，大头是 round_tally，见 tools/bench_refresh.py）。
CREATE INDEX IF NOT EXISTS idx_event_log_round_shop_event
    ON event_log(round_id, shop_key, event);
CREATE INDEX IF NOT EXISTS idx_event_log_round_shop_ts
    ON event_log(round_id, shop_key, ts);

CREATE TABLE IF NOT EXISTS run_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    params_hash TEXT NOT NULL UNIQUE,
    captured_at TEXT NOT NULL,
    config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawler_process (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pid INTEGER NOT NULL,
    round_id INTEGER,
    started_at TEXT NOT NULL,
    note TEXT,
    process_os_started TEXT,
    browser_state TEXT NOT NULL DEFAULT 'UNKNOWN',
    browser_port INTEGER,
    browser_pid INTEGER,
    browser_os_started TEXT
);

CREATE TABLE IF NOT EXISTS stop_requests (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    round_id INTEGER,
    kind TEXT NOT NULL,
    target_pid INTEGER NOT NULL,
    target_started_at TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    ack_at TEXT,
    note TEXT
);

-- 本机计划表（spec §6）：本周计划的整周指派，界面与命令行读的同一份落库计划。
-- 不入交换集——每台机器各存各的；存整周的行（不只本机那几行），账目与报告要对照
-- 「计划说了什么」。来源记 拉取/本地缓存/生成，见 plan_step.PlanSource。
CREATE TABLE IF NOT EXISTS weekly_plan (
    week TEXT NOT NULL,
    shop_key TEXT NOT NULL,
    shop_name TEXT,
    machine_id TEXT NOT NULL,
    pages INTEGER NOT NULL,
    source TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    PRIMARY KEY (week, shop_key)
);

-- 汇总导入账（spec §9；全部是本机账，不入交换集，实现见 merge.py）。

-- 包哈希幂等账 + 每次导入的结果计数：同哈希再来直接跳过（重跑汇总是无害的）。
CREATE TABLE IF NOT EXISTS import_packages (
    package_sha256 TEXT PRIMARY KEY,
    package_name TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    week TEXT,
    generated_at TEXT,
    format_version TEXT,
    imported_at TEXT NOT NULL,
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_replaced INTEGER NOT NULL DEFAULT 0,
    conflicts INTEGER NOT NULL DEFAULT 0,
    images_pulled INTEGER NOT NULL DEFAULT 0,
    images_skipped INTEGER NOT NULL DEFAULT 0,
    images_missing INTEGER NOT NULL DEFAULT 0
);

-- 冲突明细：同一 (日期, 店铺) 上两个不同机器标识的 claim（越权、降级、换周交接不清）。
-- 「谁赢」由判据定、各台一致；「我见过这个冲突吗」取决于该机导入过哪些包，所以这是
-- 本机视角的流水：每次导入碰到冲突追加一行，败方被覆盖与保留的行数在行上。
CREATE TABLE IF NOT EXISTS import_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    package_sha256 TEXT NOT NULL,
    observed_date TEXT NOT NULL,
    shop_key TEXT NOT NULL,
    winner_side TEXT NOT NULL,
    winner_machine TEXT NOT NULL,
    winner_at TEXT NOT NULL,
    loser_machine TEXT NOT NULL,
    loser_at TEXT NOT NULL,
    loser_rows_replaced INTEGER NOT NULL,
    loser_rows_kept INTEGER NOT NULL
);

-- 取胜方账（观测表）：每个 (店铺, 日期) 当前取胜 claim 的 (时刻, 机器)。时刻本身仍由
-- 版本行推导（不单独存），这张账只为记住取胜方是哪台机器——平局按 machine_id 定胜时，
-- 合并必须知道库里这一组行是谁写的，否则同一批包换个导入顺序会得出不同的库。
CREATE TABLE IF NOT EXISTS merge_claims (
    shop_key TEXT NOT NULL,
    observed_date TEXT NOT NULL,
    claim_at TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    PRIMARY KEY (shop_key, observed_date)
);

-- 取胜方账（身份表）：(表, 主键) → 描述列当前取胜方的 (last_seen_at, 机器)。同理由：
-- 描述列按最近一次观测取胜，增量合并要记得住"最近一次"是谁的。
CREATE TABLE IF NOT EXISTS merge_seen (
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    PRIMARY KEY (table_name, row_key)
);

-- 越权与计划外账（spec §6）：本机在计划之外采过的店。越权 = 店在本周计划里、但归别台机器
-- （放行并上报）；计划外 = 逃生口自由采集（拉不到计划库且本地没有本周计划）或计划没说到
-- 这家店。不入交换集；汇总侧拿它给冲突补「计划外多采」这层说明（票据 10）。只记偏离——
-- "实际采了哪些"仍是 rounds.run_date × shop_rounds 的现成答案，不在这里重记。
CREATE TABLE IF NOT EXISTS plan_deviations (
    round_id INTEGER NOT NULL,
    run_date TEXT NOT NULL,
    week TEXT NOT NULL,
    shop_key TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    planned_machine TEXT,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (round_id, shop_key)
);
"""

# 同一轮、店铺、商品和 SKU 至多一条成功快照：靠唯一索引保证（见 connect() 的迁移）。
SNAPSHOT_SUCCESS_INDEX = "idx_snapshots_success_key"

# 本轮计数按店铺分组时还要看状态与 sku：这条覆盖索引让两条聚合各扫一遍索引就够
# （30 万行实测 0.19 / 0.20 秒），不用为每一行回表取 page_status/sku_id（+0.25 秒），
# 更不用把两张表 JOIN 起来逐行核对（1.5 秒）。它引用后加的两列，所以只能由迁移建
# ——SCHEMA 比迁移先跑，老库的 snapshots 可能还没有这两列。
ROUND_TALLY_INDEX = "idx_snapshots_round_counts"

# 汇总导入的去重键（spec §9 / ADR-0032）：一次观测 = (店铺, 商品, 时刻, 图片结果)。
# 不能写成 UNIQUE(..., content_hash)——content_hash 与 image_error 可能为 NULL，而 SQLite
# 唯一索引里 NULL 互不相等，会漏掉所有图片失败的行；两列的空值用 COALESCE 折进表达式。
# 列的表达式只在这里写一遍：建索引与写路径的回查共用（导入侧，票据 09，也用它）。
# 索引由迁移建、不写进 SCHEMA：老库版本表若有重复键，UNIQUE 建不上——迁移可以跳过、
# 库照开；SCHEMA 里失败则整个 executescript 中断，库打不开。
VERSION_DEDUPE_KEY = ("shop_key", "offer_id", "observed_at",
                      "COALESCE(content_hash,'')", "COALESCE(image_error,'')")
VERSION_DEDUPE_INDEX = "idx_product_information_dedupe"

# SKU 图来源三态（`sku_image_versions.source`）：每行必居其一，库里可断言。空图（页面
# 没给这个 SKU 配图）不是失败，用当次观测的商品主图代填；有地址但下载/校验败才算失败行。
SKU_IMAGE_OWN = "专属图"
SKU_IMAGE_FILLED = "主图代填"
SKU_IMAGE_NONE = "无图"

# SKU 图流水的去重键：照版本表的写法加 sku_id（一次观测 = 店铺、商品、SKU、时刻、图片
# 结果）。NULL 折叠同理——无图行两列都是 NULL，不折进表达式就漏掉它们。索引由迁移建、
# 不写进 SCHEMA，理由与版本表那条相同（见 _sku_image_dedupe_index）。
SKU_IMAGE_DEDUPE_KEY = ("shop_key", "offer_id", "sku_id", "observed_at",
                        "COALESCE(content_hash,'')", "COALESCE(image_error,'')")
SKU_IMAGE_DEDUPE_INDEX = "idx_sku_image_dedupe"

# 过程页刷新的两条索引：逐店 deny 计数、逐店时间跨度（名字要与上面 SCHEMA 里的
# 两条 CREATE INDEX 一致，tests/test_db.py 有用例守着）。
EVENT_REFRESH_INDEXES = (
    "idx_event_log_round_shop_event",
    "idx_event_log_round_shop_ts",
)


def _has_index(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone() is not None


def _has_snapshot_success_index(conn: sqlite3.Connection) -> bool:
    """唯一索引在不在——在，就说明这个库已经过了那次整表去重。"""
    return _has_index(conn, SNAPSHOT_SUCCESS_INDEX)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utcnow_us() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


CST = timezone(timedelta(hours=8))


# 延迟/反爬相关配置键：用于生成 run_params 快照与 params_hash
PARAMS_KEYS = (
    "detail_delay_sec", "list_delay_sec", "long_pause_interval", "long_pause_sec",
    "pause_every_detail_visits", "pause_sec",
    "batch_size", "batch_rest_sec", "action_delay_sec", "read_delay_sec",
    "retry_base_sec", "retry_jitter_sec", "max_pages_per_shop",
    "human_pause_minutes", "alarm_on_intervention",
)


def params_snapshot(cfg) -> dict:
    return {k: getattr(cfg, k) for k in PARAMS_KEYS}


def params_hash(cfg) -> str:
    payload = json.dumps(params_snapshot(cfg), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def cst_date(iso_utc: str | None = None) -> str:
    """把 UTC 时间转成北京时间的日期 YYYY-MM-DD。"""
    if not iso_utc:
        return datetime.now(CST).strftime("%Y-%m-%d")
    try:
        dt = datetime.fromisoformat(iso_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%Y-%m-%d")
    except ValueError:
        return datetime.now(CST).strftime("%Y-%m-%d")


DAY_CUTOFF = (23, 55)
DAY_BOUNDARY_NOTE = "库存数据即将跨天，请0点后继续抓取"
DETAIL_BUDGET_NOTE = "本轮详情预算已用尽，剩余商品留待下一轮"

# 只用于迁移：把旧库的中文状态串折算成轮次终态标识。状态列删掉之后，
# 这里不再有写入方，只剩打开旧库时的一次性折算。
LEGACY_STATUS_REASONS = {
    "完成": "COMPLETED",
    "需人工-失败率超限": "FAIL_RATE_EXCEEDED",
    "详情预算耗尽": "DETAIL_BUDGET_EXHAUSTED",
    "已放弃": "ABANDONED",
    "意外中止": "DENY_EXCEEDED",
}
LEGACY_UNKNOWN_REASON = "LEGACY_UNKNOWN"


def legacy_terminal_reason(status: str | None, note: str | None) -> str | None:
    """把旧状态串与说明折算成轮次终态标识；「进行中」返回 None。"""
    if not status or status == "进行中":
        return None
    if status == "意外中止" and note == DAY_BOUNDARY_NOTE:
        return "DAY_BOUNDARY"
    return LEGACY_STATUS_REASONS.get(status, LEGACY_UNKNOWN_REASON)


class DayBoundaryReached(RuntimeError):
    """库存数据即将跨天（北京时间 ≥ 23:55），当前轮次需中止，0 点后继续。"""


class DetailBudgetExhausted(RuntimeError):
    """本轮详情预算已用尽但仍有待处理商品；轮次进入终态，剩余留待下一轮。

    已发现的商品在抓取途中就已落库，不需要随这个异常携带出来。
    """

    def __init__(self, note: str = DETAIL_BUDGET_NOTE) -> None:
        super().__init__(note)


# 点击事件里「这张卡算成功 / 算失败」两个集合，只在这里写一遍。
# `UNREADABLE`（拿到了商品编号、但详情读不出来）两个集合都不进：那张卡的失败已经由
# `detail.capture_observation` 写成失败快照，并经 `failed_offers` 计入了失败率，
# 在这里再算一次就是双计。事件名与位置编码都取自 `click_events`。
_CARD_OK_OUTCOMES = (click_events.ClickOutcome.SUBMITTED, click_events.ClickOutcome.SKIPPED)
# 卡片口径的失败就是「没走到认出商品编号那一步」——由结果种类自己的属性给出，
# 将来新增种类时这里不会漏掉。`UNREADABLE` 恰好有编号，所以自动落在两个集合之外。
_CARD_FAILED_OUTCOMES = tuple(outcome for outcome in click_events.ClickOutcome
                              if not outcome.has_offer_id)
# SQL 白名单必须与下面分类用的两个集合严格一致：分类写的是两个显式分支、不是 `else`，
# 这样将来新增的种类要么两处都在、要么两处都不在，不会一边取到了行、另一边判不出。
_CLICK_CARD_EVENTS = tuple(outcome.value
                           for outcome in _CARD_OK_OUTCOMES + _CARD_FAILED_OUTCOMES)


def _migrate_round_columns(conn: sqlite3.Connection) -> bool:
    """给旧轮次补上日期与终态，然后丢掉过渡用的中文状态列；可重复执行。

    返回这次有没有动手（新库上这一趟什么都不用做）。
    """
    columns = {row[1] for row in conn.execute('PRAGMA table_info("rounds")').fetchall()}
    if not columns:
        return False
    has_reason = "terminal_reason" in columns
    has_status = "status" in columns
    touched = False
    selected = ["id", "started_at", "run_date"]
    if has_reason:
        selected.append("terminal_reason")
    if has_status:
        selected += ["status", "note"]
    rows = conn.execute(f"SELECT {', '.join(selected)} FROM rounds").fetchall()
    for row in rows:
        if row["run_date"] is None:
            conn.execute(
                "UPDATE rounds SET run_date=? WHERE id=?",
                (cst_date(row["started_at"]), row["id"]),
            )
            touched = True
        if has_reason and has_status and row["terminal_reason"] is None:
            reason = legacy_terminal_reason(row["status"], row["note"])
            if reason is not None:
                conn.execute(
                    "UPDATE rounds SET terminal_reason=? WHERE id=?", (reason, row["id"])
                )
    if has_status:
        conn.execute("ALTER TABLE rounds DROP COLUMN status")
        touched = True
        log.info("已删除过渡用的 rounds.status 列（「进行中」改由没有终态表达）")
    return touched


# ---------- 连接即迁移：动作排成清单，做过什么写进报告 ----------

@dataclass(frozen=True)
class MigrationReport:
    """这次开库做过哪些迁移。

    `applied` 是真正动过手的段名（不需要动手的段不出现）；整表动作另有明细：
    `deduped_snapshot_rows`（None = 这次没跑整表去重）与 `created_indexes`。
    """

    applied: tuple[str, ...] = ()
    deduped_snapshot_rows: int | None = None
    created_indexes: tuple[str, ...] = ()


class _ReportBuilder:
    """迁移过程中攒报告；最后交出去的是一份不可变快照。"""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.deduped_snapshot_rows: int | None = None
        self.created_indexes: list[str] = []

    def did(self, name: str) -> None:
        self.applied.append(name)

    def build(self) -> MigrationReport:
        return MigrationReport(tuple(self.applied), self.deduped_snapshot_rows,
                               tuple(self.created_indexes))


@dataclass(frozen=True)
class _Migration:
    name: str
    apply: Callable[[sqlite3.Connection, "_ReportBuilder"], bool]


def _add_column(table: str, column: str, ddl: str) -> _Migration:
    """「旧库缺这一列就补上」：已经加过就什么都不做。"""
    name = f"{table}_{column}"

    def apply(conn: sqlite3.Connection, out: _ReportBuilder) -> bool:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError:
            return False
        log.info("迁移 %s：已补上 %s.%s", name, table, column)
        return True

    return _Migration(name, apply)


def _drop_column(table: str, column: str) -> _Migration:
    """「旧库还留着这一列就删掉」。"""
    name = f"drop_{table}_{column}"

    def apply(conn: sqlite3.Connection, out: _ReportBuilder) -> bool:
        try:
            info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        except sqlite3.OperationalError:
            return False
        if not any(row[1] == column for row in info):
            return False
        try:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        except sqlite3.OperationalError as exc:
            log.debug("删除 %s.%s 失败（可能已删除或版本不支持）：%s", table, column, exc)
            return False
        conn.commit()
        log.info("迁移 %s：已删除 %s.%s", name, table, column)
        return True

    return _Migration(name, apply)


def _drop_shops_active(conn: sqlite3.Connection, out: _ReportBuilder) -> bool:
    """迁移：删除 shops.active 列（旧库）。"""
    try:
        info = conn.execute('PRAGMA table_info("shops")').fetchall()
    except sqlite3.OperationalError:
        return False
    if not any(row[1] == "active" for row in info):
        return False
    try:
        conn.execute("ALTER TABLE shops DROP COLUMN active")
    except sqlite3.OperationalError:
        return False
    conn.commit()
    log.info("迁移 drop_shops_active：已删除 shops.active 列")
    return True


def _skus_primary_key_on_sku_id(conn: sqlite3.Connection, out: _ReportBuilder) -> bool:
    """迁移：旧 skus 主键为 (offer_id, sku_name)，统一改为 (offer_id, sku_id)。

    整段包在 try 里：这一段改不动就不改（旧代码同样静默跳过），
    别让一次迁移把库卡在打不开的状态。
    """
    try:
        info = conn.execute('PRAGMA table_info("skus")').fetchall()
        pk_cols = [row[1] for row in info if row[5] > 0]
        if not pk_cols or "sku_name" not in pk_cols:
            return False
        conn.execute("ALTER TABLE skus RENAME TO skus_old")
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS skus ("
            " offer_id TEXT NOT NULL, sku_name TEXT, sku_id TEXT NOT NULL, "
            " first_seen_at TEXT NOT NULL, last_seen_at TEXT, "
            " PRIMARY KEY (offer_id, sku_id));"
        )
        conn.execute(
            "INSERT OR IGNORE INTO skus(offer_id, sku_name, sku_id, first_seen_at, last_seen_at) "
            "SELECT offer_id, MAX(sku_name), sku_id, MIN(first_seen_at), MAX(last_seen_at) "
            "FROM skus_old WHERE sku_id IS NOT NULL GROUP BY offer_id, sku_id"
        )
        conn.execute("DROP TABLE skus_old")
        conn.commit()
    except sqlite3.OperationalError as exc:
        log.debug("skus 主键迁移跳过（改不动就不改）：%s", exc)
        return False
    log.info("迁移 skus_primary_key_on_sku_id：skus 主键已改为 (offer_id, sku_id)")
    return True


def _rounds_legacy_status_columns(conn: sqlite3.Connection, out: _ReportBuilder) -> bool:
    """迁移：旧轮次的日期与终态折算，以及丢弃过渡用的中文状态列。"""
    try:
        changed = _migrate_round_columns(conn)
        conn.commit()
    except sqlite3.OperationalError as exc:
        log.debug("轮次列迁移失败：%s", exc)
        return False
    return changed


def _snapshot_success_index(conn: sqlite3.Connection, out: _ReportBuilder) -> bool:
    """迁移：同一轮、店铺、商品和 SKU 只留最新成功快照，再建唯一索引。

    失败记录与无 SKU 的跳过记录不参与该约束。去重是整表扫描，而界面每次刷新都走一次
    connect()，所以它只在「唯一索引还不在」时跑：索引一旦建好，重复行不可能再写进来，
    后面的开库不该再付这笔随快照总量线性增长的成本（IS-38 实测：WAL 库 30 万行上
    这次去重 + 建索引约 0.42 秒；索引已在时 connect() 约 1.6 毫秒）。
    """
    snapshot_cols = {
        row[1] for row in conn.execute('PRAGMA table_info("snapshots")').fetchall()
    }
    if not ({"id", "round_id", "shop_key", "offer_id", "sku_id", "page_status"} <= snapshot_cols):
        return False
    if _has_snapshot_success_index(conn):
        return False
    removed = conn.execute(
        "DELETE FROM snapshots WHERE id IN ("
        "SELECT older.id FROM snapshots older "
        "JOIN snapshots newer ON newer.round_id=older.round_id "
        "AND newer.shop_key=older.shop_key AND newer.offer_id=older.offer_id "
        "AND newer.sku_id=older.sku_id AND newer.page_status='成功' "
        "AND newer.sku_id IS NOT NULL AND newer.id > older.id "
        "WHERE older.page_status='成功' AND older.sku_id IS NOT NULL"
        ")"
    ).rowcount
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {SNAPSHOT_SUCCESS_INDEX} "
        "ON snapshots(round_id, shop_key, offer_id, sku_id) "
        "WHERE page_status='成功' AND sku_id IS NOT NULL"
    )
    out.deduped_snapshot_rows = removed
    out.created_indexes.append(SNAPSHOT_SUCCESS_INDEX)
    log.info("快照去重迁移：删除 %d 行重复成功记录并建立唯一索引 %s",
             removed, SNAPSHOT_SUCCESS_INDEX)
    return True


def _round_tally_index(conn: sqlite3.Connection, out: _ReportBuilder) -> bool:
    """迁移：补上本轮计数的覆盖索引（老库第一次开连接时建一次）。

    索引引用 page_status/sku_id，而老库的 snapshots 可能还没有这两列（SCHEMA 先跑、
    迁移后跑），所以不能直接写进 SCHEMA；建好之后 `_has_index()` 就不再动手，界面每次
    刷新不会重复付建索引的钱（IS-38）。
    """
    columns = {row[1] for row in conn.execute('PRAGMA table_info("snapshots")').fetchall()}
    if not {"round_id", "shop_key", "offer_id", "page_status", "sku_id"} <= columns:
        return False
    if _has_index(conn, ROUND_TALLY_INDEX):
        return False
    conn.execute(
        f"CREATE INDEX {ROUND_TALLY_INDEX} ON snapshots"
        "(round_id, shop_key, offer_id, page_status, sku_id)"
    )
    out.created_indexes.append(ROUND_TALLY_INDEX)
    log.info("本轮计数迁移：建立覆盖索引 %s", ROUND_TALLY_INDEX)
    return True


def _version_dedupe_index(conn: sqlite3.Connection, out: _ReportBuilder) -> bool:
    """迁移：给版本表建汇总导入的去重索引（老库第一次开连接时建一次）。

    建不动就跳过（老库版本表真有重复键时 UNIQUE 建不上）——与 `_drop_column` 同款：
    一次迁移不该把库卡在打不开的状态；没建起来，下次开库会再试。
    """
    columns = {row[1] for row in
               conn.execute('PRAGMA table_info("product_information_versions")').fetchall()}
    if not {"shop_key", "offer_id", "observed_at", "content_hash", "image_error"} <= columns:
        return False
    if _has_index(conn, VERSION_DEDUPE_INDEX):
        return False
    try:
        conn.execute(
            f"CREATE UNIQUE INDEX {VERSION_DEDUPE_INDEX} ON product_information_versions"
            f"({', '.join(VERSION_DEDUPE_KEY)})"
        )
    except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
        # 重复键报的是 IntegrityError（不是 OperationalError），两条都要接住。
        log.warning("版本表去重索引建不上，本次跳过（下次开库再试）：%s", exc)
        return False
    out.created_indexes.append(VERSION_DEDUPE_INDEX)
    log.info("版本表去重索引迁移：建立 %s", VERSION_DEDUPE_INDEX)
    return True


def _sku_image_dedupe_index(conn: sqlite3.Connection, out: _ReportBuilder) -> bool:
    """迁移：给 SKU 图流水建去重索引（老库第一次开连接时建一次）。

    与版本表那条同款：建不动就跳过（流水表真有重复键时 UNIQUE 建不上），下次开库再试——
    一次迁移不该把库卡在打不开的状态。老库的表由 SCHEMA 在这次开库里补上，建索引接在它后面。
    """
    columns = {row[1] for row in
               conn.execute('PRAGMA table_info("sku_image_versions")').fetchall()}
    if not {"shop_key", "offer_id", "sku_id", "observed_at",
            "content_hash", "image_error"} <= columns:
        return False
    if _has_index(conn, SKU_IMAGE_DEDUPE_INDEX):
        return False
    try:
        conn.execute(
            f"CREATE UNIQUE INDEX {SKU_IMAGE_DEDUPE_INDEX} ON sku_image_versions"
            f"({', '.join(SKU_IMAGE_DEDUPE_KEY)})"
        )
    except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
        log.warning("SKU 图去重索引建不上，本次跳过（下次开库再试）：%s", exc)
        return False
    out.created_indexes.append(SKU_IMAGE_DEDUPE_INDEX)
    log.info("SKU 图去重索引迁移：建立 %s", SKU_IMAGE_DEDUPE_INDEX)
    return True


_MIGRATIONS: tuple[_Migration, ...] = (
    _Migration("drop_shops_active", _drop_shops_active),
    _Migration("skus_primary_key_on_sku_id", _skus_primary_key_on_sku_id),
    _add_column("products", "last_seen_at", "TEXT"),
    _add_column("products", "main_image_url", "TEXT"),
    _add_column("shop_rounds", "list_note", "TEXT"),
    _add_column("rounds", "detail_budget_limit", "INTEGER"),
    _add_column("rounds", "run_date", "TEXT"),
    _add_column("rounds", "terminal_reason", "TEXT"),
    _Migration("rounds_legacy_status_columns", _rounds_legacy_status_columns),
    _drop_column("rounds", "phase"),
    _add_column("detail_opportunities", "offer_id", "TEXT"),
    _drop_column("skus", "main_image_url"),
    _drop_column("snapshots", "stock_delta"),
    _drop_column("inventory", "diff"),
    _Migration("snapshot_success_index", _snapshot_success_index),
    _Migration("round_tally_index", _round_tally_index),
    _Migration("version_dedupe_index", _version_dedupe_index),
    _Migration("sku_image_dedupe_index", _sku_image_dedupe_index),
    _add_column("crawler_process", "process_os_started", "TEXT"),
    _add_column("crawler_process", "browser_state", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
    _add_column("crawler_process", "browser_port", "INTEGER"),
    _add_column("crawler_process", "browser_pid", "INTEGER"),
    _add_column("crawler_process", "browser_os_started", "TEXT"),
)


def migrate(conn: sqlite3.Connection) -> MigrationReport:
    """按序跑完所有迁移，交回这次开库做过什么。"""
    out = _ReportBuilder()
    for migration in _MIGRATIONS:
        if migration.apply(conn, out):
            out.did(migration.name)
    conn.commit()
    return out.build()


def open(db_path: Path) -> sqlite3.Connection:
    """开一条连接并建好表（不跑迁移）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def connect(db_path: Path) -> sqlite3.Connection:
    """界面、命令行与工具都走这里：连接即迁移（见 CONTEXT.md）。"""
    conn = open(db_path)
    migrate(conn)
    return conn


# 「已处理」「成功库存快照」两个判据只在这一处写下来；下面两条计数查询都拼它（表别名一律 `s`）。
_TALLY_HANDLED_WHEN = "s.page_status IN ('成功', '跳过')"
_TALLY_SUCCESS_WHEN = "s.page_status='成功' AND s.sku_id IS NOT NULL"


def _tally_columns() -> str:
    """本轮计数的三列：已处理、成功商品、成功 SKU 行。"""
    return ("COUNT(DISTINCT CASE WHEN "
            f"{_TALLY_HANDLED_WHEN} THEN s.offer_id END) AS handled, "
            "COUNT(DISTINCT CASE WHEN "
            f"{_TALLY_SUCCESS_WHEN} THEN s.offer_id END) AS success_offers, "
            f"SUM(CASE WHEN {_TALLY_SUCCESS_WHEN} THEN 1 ELSE 0 END) AS success_skus")


# 榜单侧：这一轮榜单行里发现过哪些商品（`idx_shop_offers_key` 覆盖）。
_DISCOVERED_TALLY_SQL = (
    "SELECT shop_key, COUNT(DISTINCT offer_id) AS discovered FROM shop_offers "
    "WHERE round_id=? GROUP BY shop_key")
# 快照侧：整轮里「已处理」「成功商品」「成功 SKU 行」三个数（`idx_snapshots_round_counts` 覆盖）。
_SNAPSHOT_TALLY_SQL = (
    f"SELECT s.shop_key, {_tally_columns()} "
    "FROM snapshots s WHERE s.round_id=? GROUP BY s.shop_key")
# 孤儿侧：同一组数，但只看「有快照、无榜单行」的商品（IS-49；通常是零行）。
_ORPHAN_TALLY_SQL = (
    f"SELECT s.shop_key, {_tally_columns()}, "
    "COUNT(DISTINCT s.offer_id) AS orphans "
    "FROM snapshots s WHERE s.round_id=? AND NOT EXISTS ("
    "SELECT 1 FROM shop_offers so WHERE so.round_id=s.round_id "
    "AND so.shop_key=s.shop_key AND so.offer_id=s.offer_id) "
    "GROUP BY s.shop_key")


@dataclass(frozen=True)
class ShopTally:
    """一组本轮计数：整轮合计，或一家店的。**商品数一律按榜单行去重**（IS-49 判定
    「有快照、无榜单行」是异常）。

    - `shop_key`：哪一家店；整轮合计是 `None`（它不属于任何一家店）
    - `discovered`：榜单里发现过的商品（榜单行去重）
    - `handled`：其中有成功或跳过快照的——跳过算已处理、不算失败（ADR-0002）
    - `failed_offers`：`discovered - handled`，含「只有榜单行、本轮没抓到」的商品
    - `success_offers` / `success_skus`：成功快照的商品数与 SKU 行数，只数榜单行里有的商品
    - `orphans`：有快照、没有榜单行的商品数——单列出来，不算成功商品（IS-49；
      `tools/check_orphans.py` 查同一件事）
    """

    shop_key: str | None = None
    discovered: int = 0
    handled: int = 0
    success_offers: int = 0
    success_skus: int = 0
    orphans: int = 0

    @property
    def failed_offers(self) -> int:
        return self.discovered - self.handled


@dataclass(frozen=True)
class RoundTally:
    """一轮的计数：整轮合计 + 按店铺切片 + 点击未得卡片。

    整轮一次算全（榜单行、快照、孤儿各一条 GROUP BY shop_key 的聚合），页面按店铺切片
    取自己那家——界面每约 2 秒刷新一次，不能为每家店各跑一遍（IS-38 的护栏：实测逐店
    算 12 家要 1.3 秒，超了 1 秒上限）。

    失败率那两条算式不在这里：它是 CONTEXT.md 定义的领域词，计数只是它的输入。
    """

    total: ShopTally
    per_shop: tuple[ShopTally, ...] = ()
    click_card_failures: int = 0

    @property
    def discovered(self) -> int:
        return self.total.discovered

    @property
    def handled(self) -> int:
        return self.total.handled

    @property
    def failed_offers(self) -> int:
        return self.total.failed_offers

    @property
    def success_offers(self) -> int:
        return self.total.success_offers

    @property
    def success_skus(self) -> int:
        return self.total.success_skus

    @property
    def orphans(self) -> int:
        return self.total.orphans

    def shop(self, shop_key: str) -> ShopTally:
        """按店铺切片；本轮一行都没出现过的店铺给零计数（不返回 None，省得调用方补默认）。"""
        for one in self.per_shop:
            if one.shop_key == shop_key:
                return one
        return ShopTally(shop_key=shop_key)


@dataclass(frozen=True)
class WeeklyPlanRow:
    """本机计划表的一行：某店在某周归哪台机器、页数预算是多少（见 plan_step）。"""

    shop_key: str
    shop_name: str
    machine_id: str
    pages: int


@dataclass(frozen=True)
class PlanDeviationRow:
    """计划外账的一行：某轮里一家越权/计划外采过的店（判定见 plan_step）。

    `kind` 取 `plan_step.DeviationKind`；`planned_machine` 只在越权时有值（计划归谁），
    逃生口自由采集时为 None。
    """

    round_id: int
    run_date: str
    week: str
    shop_key: str
    machine_id: str
    kind: str
    planned_machine: str | None
    reason: str


class Database:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def completed_listing_keys(self, round_id: int) -> set[str]:
        """本轮榜单已完成的店铺编号：三条驱动路径靠它决定续跑时跳过谁。

        榜单阶段只问「谁已完成」然后跳过已完成的那家，不拿未完成店铺集合
        反向筛出已完成项——后者永远筛不出东西，是 IS-33 的成因。
        未完成集合另见 incomplete_listings()，它服务于轮次收尾。
        """
        cur = self.conn.execute(
            "SELECT shop_key FROM shop_rounds WHERE round_id=? AND list_status='完成'",
            (round_id,),
        )
        return {str(row["shop_key"]) for row in cur.fetchall()}

    def add_shop(self, round_id: int, shop_key: str, shop_url: str, shop_name: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO shop_rounds(round_id, shop_key, shop_url, shop_name) "
            "VALUES (?, ?, ?, ?)",
            (round_id, shop_key, shop_url, shop_name),
        )
        self.conn.commit()

    def upsert_shops(self, shops: list) -> None:
        """把 shops.csv 的店铺写入 shops 表；只 upsert，不因 csv 删除而删除。"""
        now = utcnow()
        self.conn.executemany(
            "INSERT INTO shops(shop_key, shop_name, shop_url, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(shop_key) DO UPDATE SET "
            "shop_name=excluded.shop_name, shop_url=excluded.shop_url, last_seen_at=excluded.last_seen_at",
            [(s.key, s.name, s.url, now, now) for s in shops],
        )
        self.conn.commit()

    def replace_weekly_plan(self, week: str, rows, *, source: str, plan_sha256: str,
                            stored_at: str) -> bool:
        """把本周计划的整周指派写进本机计划表（每店一行），返回这次是否真的落了库。

        `rows` 是 `WeeklyPlanRow` 序列；整周替换——新一份计划里没有的店不会留下旧行。
        内容、来源与哈希都和库里那份一致时不动（重跑幂等：连 `stored_at` 也不刷新，
        它是这份计划落库的时刻，不是重跑的时刻）。
        """
        new_rows = sorted((r.shop_key, r.shop_name, r.machine_id, r.pages) for r in rows)
        current = self.conn.execute(
            "SELECT shop_key, shop_name, machine_id, pages, source, plan_sha256 "
            "FROM weekly_plan WHERE week=? ORDER BY shop_key", (week,)).fetchall()
        unchanged = all(r["source"] == source and r["plan_sha256"] == plan_sha256
                        for r in current) and \
            [(r["shop_key"], r["shop_name"], r["machine_id"], r["pages"]) for r in current] == new_rows
        if unchanged:
            return False
        with self.conn:                     # 整周一个事务：删旧行与插新行一起生效
            self.conn.execute("DELETE FROM weekly_plan WHERE week=?", (week,))
            self.conn.executemany(
                "INSERT INTO weekly_plan(week, shop_key, shop_name, machine_id, pages, "
                "source, stored_at, plan_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(week, key, name, machine, pages, source, stored_at, plan_sha256)
                 for key, name, machine, pages in new_rows],
            )
        return True

    def weekly_plan(self, week: str):
        """本机计划表里这一周的整周指派，按店铺编号排序；没落过库就是空列表。"""
        return self.conn.execute(
            "SELECT * FROM weekly_plan WHERE week=? ORDER BY shop_key", (week,)).fetchall()

    def record_plan_deviations(self, rows, *, recorded_at: str) -> None:
        """把一轮里越权/计划外采过的店写进计划外账（本机账，不入交换集）。

        `rows` 是 `PlanDeviationRow` 序列。同一轮重跑（续跑）重复记账不产生重复行——
        主键是 (轮次, 店铺)。
        """
        self.conn.executemany(
            "INSERT OR REPLACE INTO plan_deviations(round_id, run_date, week, shop_key, "
            "machine_id, kind, planned_machine, reason, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(r.round_id, r.run_date, r.week, r.shop_key, r.machine_id, r.kind,
              r.planned_machine, r.reason, recorded_at) for r in rows],
        )
        self.conn.commit()

    def recorded_deviations(self, *, round_id: int | None = None):
        """计划外账里已经记下的行（可按轮次过滤），按日期、店铺排序；没记过就是空列表。"""
        where, params = (" WHERE round_id=?", (round_id,)) if round_id is not None else ("", ())
        return self.conn.execute(
            f"SELECT * FROM plan_deviations{where} ORDER BY run_date, shop_key", params,
        ).fetchall()

    def record_crawler_process(self, pid: int, round_id: int | None,
                               note: str | None = None, *,
                               process_os_started: str | None = None,
                               browser_state: str = "NOT_STARTED",
                               browser_port: int | None = None,
                               browser_pid: int | None = None,
                               browser_os_started: str | None = None) -> str:
        """登记正在跑的采集进程（单行），返回这行的启动时刻。

        这行只服务于「界面显示谁在跑、能不能中止它」；是不是真的有进程在跑，
        以会话锁为准（见 single_instance）。被强杀的进程会留下这行，读到的人负责清。
        停止请求按 (pid, 启动时刻) 认领（ADR-0009），所以调用方要留住这个时刻：
        精度取到微秒，好让「PID 被复用」也撞不上同一次运行。
        """
        started_at = utcnow_us()
        self.conn.execute(
            "INSERT OR REPLACE INTO crawler_process"
            "(id, pid, round_id, started_at, note, process_os_started, browser_state, "
            "browser_port, browser_pid, browser_os_started) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(pid), round_id, started_at, note, process_os_started, browser_state,
             browser_port, browser_pid, browser_os_started),
        )
        self.conn.commit()
        return started_at

    def crawler_process(self):
        """正在跑的采集进程身份；没有登记时返回 None。"""
        return self.conn.execute("SELECT * FROM crawler_process WHERE id=1").fetchone()

    def clear_crawler_process(self, *, target_pid: int | None = None,
                              target_started_at: str | None = None) -> int:
        """条件删除身份行；不给目标只保留迁移/人工工具的全删能力。"""
        if target_pid is None:
            cur = self.conn.execute("DELETE FROM crawler_process WHERE id=1")
        else:
            cur = self.conn.execute(
                "DELETE FROM crawler_process WHERE id=1 AND pid=? AND started_at=?",
                (int(target_pid), target_started_at),
            )
        self.conn.commit()
        return cur.rowcount

    def publish_browser_state_if_current(self, *, target_pid: int,
                                         target_started_at: str,
                                         browser_state: str,
                                         browser_port: int | None = None,
                                         browser_pid: int | None = None,
                                         browser_os_started: str | None = None) -> bool:
        """只给仍属于 target 的身份行发布/补全浏览器事实。"""
        cur = self.conn.execute(
            "UPDATE crawler_process SET browser_state=?, browser_port=?, browser_pid=?, "
            "browser_os_started=? WHERE id=1 AND pid=? AND started_at=?",
            (browser_state, browser_port, browser_pid, browser_os_started,
             int(target_pid), target_started_at),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def clear_browser_facts_if_current(self, *, target_pid: int,
                                       target_started_at: str) -> bool:
        return self.publish_browser_state_if_current(
            target_pid=target_pid, target_started_at=target_started_at,
            browser_state="CLOSED", browser_port=None, browser_pid=None,
            browser_os_started=None,
        )

    def request_stop(self, *, round_id: int | None, kind: str, target_pid: int,
                     target_started_at: str, note: str | None = None) -> None:
        """写一条停止请求（单行）：只对目标进程有效。

        后写的覆盖先写的——同一时刻至多一个采集进程，所以表里留一行就够。
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO stop_requests"
            "(id, round_id, kind, target_pid, target_started_at, requested_at, ack_at, note) "
            "VALUES (1, ?, ?, ?, ?, ?, NULL, ?)",
            (round_id, kind, int(target_pid), target_started_at, utcnow(), note),
        )
        self.conn.commit()

    def request_stop_if_current(self, *, round_id: int | None, kind: str,
                                target_pid: int, target_started_at: str,
                                note: str | None = None) -> bool:
        """写暂停请求前再次确认身份仍是 target，避免覆盖 B 的请求。"""
        cur = self.conn.execute(
            "INSERT OR REPLACE INTO stop_requests"
            "(id, round_id, kind, target_pid, target_started_at, requested_at, ack_at, note) "
            "SELECT 1, ?, ?, pid, started_at, ?, NULL, ? FROM crawler_process "
            "WHERE id=1 AND pid=? AND started_at=?",
            (round_id, kind, utcnow(), note, int(target_pid), target_started_at),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def stop_request(self):
        """当前挂着的停止请求；没有就返回 None。"""
        return self.conn.execute("SELECT * FROM stop_requests WHERE id=1").fetchone()

    def ack_stop_request(self, *, target_pid: int, target_started_at: str) -> bool:
        """回执：采集进程收到请求了。只回执给对得上的那一行，返回是否真的写进去。

        只有第一次回执算数，界面据此起算「正在收尾」的窗口。
        """
        cur = self.conn.execute(
            "UPDATE stop_requests SET ack_at=? WHERE id=1 AND target_pid=? "
            "AND target_started_at=? AND ack_at IS NULL",
            (utcnow(), int(target_pid), target_started_at),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def clear_stop_request(self, *, target_pid: int | None = None,
                           target_started_at: str | None = None) -> int:
        """删掉停止请求；给了目标就只删指向它的那条。返回删掉的行数。"""
        if target_pid is None:
            cur = self.conn.execute("DELETE FROM stop_requests WHERE id=1")
        else:
            cur = self.conn.execute(
                "DELETE FROM stop_requests WHERE id=1 AND target_pid=? AND target_started_at=?",
                (int(target_pid), target_started_at),
            )
        self.conn.commit()
        return cur.rowcount

    def _upsert_offer_row(
        self,
        round_id: int,
        shop_key: str,
        shop_url: str,
        shop_name: str,
        offer: tuple[int, str, str, str, str],
    ) -> None:
        """写入或更新一条榜单行；同一轮同店同商品只留一行。不提交事务。"""
        rank, offer_id, product_url, list_title, list_price = offer
        existing = self.conn.execute(
            "SELECT id FROM shop_offers WHERE round_id=? AND shop_key=? AND offer_id=? LIMIT 1",
            (round_id, shop_key, offer_id),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO shop_offers(round_id, shop_key, shop_url, shop_name, rank, offer_id, "
                "product_url, list_title, list_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (round_id, shop_key, shop_url, shop_name, rank, offer_id,
                 product_url, list_title, list_price),
            )
        else:
            self.conn.execute(
                "UPDATE shop_offers SET rank=?, product_url=?, list_title=?, list_price=? WHERE id=?",
                (rank, product_url, list_title, list_price, existing["id"]),
            )

    def _offer_count_in_db(self, round_id: int, shop_key: str) -> int:
        """该店本轮已发现商品数：榜单行里不同商品编号的个数。"""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT offer_id) AS c FROM shop_offers "
            "WHERE round_id=? AND shop_key=?",
            (round_id, shop_key),
        ).fetchone()
        return int(row["c"])

    def remember_shop_offer(
        self,
        round_id: int,
        shop_key: str,
        shop_url: str,
        shop_name: str,
        offer: tuple[int, str, str, str, str],
    ) -> None:
        """记下一个刚发现的商品，并即时刷新该店本轮已发现商品数。

        商品在列表遍历途中一旦被发现就落行，中断发生在遍历中途时，
        已发现的商品与已写入的快照始终有榜单行可对应。

        幂等：同一轮同店同商品重复发现只更新该行。只增不减：这里从不删除榜单行，
        因此续跑再次中断也不会抹掉先前发现的商品。
        """
        self._upsert_offer_row(round_id, shop_key, shop_url, shop_name, offer)
        self.conn.execute(
            "UPDATE shop_rounds SET offer_count=? WHERE round_id=? AND shop_key=?",
            (self._offer_count_in_db(round_id, shop_key), round_id, shop_key),
        )
        self.conn.commit()

    def save_shop_offers(
        self,
        round_id: int,
        shop_key: str,
        shop_url: str,
        shop_name: str,
        offers: list[tuple[int, str, str, str, str]],  # rank, offer_id, url, title, price
        pages_read: int,
        *,
        confirmed_empty: bool = False,
    ) -> None:
        """把一次完整榜单遍历的结果落库：整体替换该店本轮的行，rank 反映传入顺序。

        中断时保住已发现的商品是 remember_shop_offer() 的职责——那些行不属于完整
        榜单，由店铺状态与备注说明。
        """
        if not offers and not confirmed_empty:
            raise ValueError("未确认的空榜单不能标记为完成")
        self.conn.execute(
            "DELETE FROM shop_offers WHERE round_id=? AND shop_key=?", (round_id, shop_key)
        )
        rows = [
            (round_id, shop_key, shop_url, shop_name, rank, oid, url, title, price)
            for rank, oid, url, title, price in offers
        ]
        self.conn.executemany(
            "INSERT INTO shop_offers(round_id, shop_key, shop_url, shop_name, rank, offer_id, "
            "product_url, list_title, list_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.execute(
            "UPDATE shop_rounds SET list_status='完成', list_pages_read=?, offer_count=?, "
            "list_note=NULL WHERE round_id=? AND shop_key=?",
            (pages_read, len(rows), round_id, shop_key),
        )
        self.conn.commit()
        log.info("店铺 %s 榜单入库 %s 个商品（读了 %s 页）", shop_key, len(rows), pages_read)

    def mark_listing_failure(self, round_id: int, shop_key: str, note: str) -> None:
        """记录列表页失败；保留店铺为未完成状态，供同轮续跑。"""
        self.conn.execute(
            "UPDATE shop_rounds SET list_status='失败', list_note=? WHERE round_id=? AND shop_key=?",
            (note, round_id, shop_key),
        )
        self.conn.commit()

    def incomplete_listings(self, round_id: int) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM shop_rounds WHERE round_id=? AND list_status!='完成' ORDER BY shop_key",
            (round_id,),
        )
        return cur.fetchall()

    def pending_offers(self, round_id: int, max_attempts: int,
                       shop_key: str | None = None) -> Iterator[sqlite3.Row]:
        sql = """
        SELECT so.* FROM shop_offers so
        WHERE so.round_id = :rid
          AND NOT EXISTS (
            SELECT 1 FROM snapshots s
            WHERE s.round_id = so.round_id AND s.shop_key = so.shop_key
              AND s.offer_id = so.offer_id AND s.page_status IN ('成功', '跳过')
          )
          AND COALESCE((
            SELECT MAX(s2.attempt) FROM snapshots s2
            WHERE s2.round_id = so.round_id AND s2.shop_key = so.shop_key
              AND s2.offer_id = so.offer_id
          ), 0) < :max_attempts
        """
        params = {"rid": round_id, "max_attempts": max_attempts}
        if shop_key is not None:
            sql += "          AND so.shop_key = :shop_key\n"
            params["shop_key"] = shop_key
        sql += "        ORDER BY so.id"
        yield from self.conn.execute(sql, params)

    def detail_opportunities(self, round_id: int, shop_key: str) -> list[sqlite3.Row]:
        """该店铺本轮已占用的详情机会：identity 是申请时的标识，offer_id 是绑定到的商品。"""
        cur = self.conn.execute(
            "SELECT identity, offer_id FROM detail_opportunities "
            "WHERE round_id=? AND shop_key=? ORDER BY identity",
            (round_id, shop_key),
        )
        return cur.fetchall()

    def detail_opportunity_total(self, round_id: int) -> int:
        """本轮已占用的详情机会总数（重试复用同一次机会）。"""
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM detail_opportunities WHERE round_id=?",
            (round_id,),
        ).fetchone()
        return int(row["c"])

    def detail_budget_limit(self, round_id: int, default_limit: int) -> int:
        """本轮详情预算上限；首次读取时把配置写进轮次，续跑沿用，配置变化不再放宽。"""
        row = self.conn.execute(
            "SELECT detail_budget_limit FROM rounds WHERE id=?", (round_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"轮次不存在：{round_id}")
        limit = row["detail_budget_limit"]
        if limit is None:
            limit = int(default_limit)
            self.conn.execute(
                "UPDATE rounds SET detail_budget_limit=? WHERE id=?", (limit, round_id),
            )
            self.conn.commit()
        return int(limit)

    def add_detail_opportunity(self, round_id: int, shop_key: str, identity: str) -> None:
        """占用一次详情机会；同一标识重复申请由主键忽略。"""
        self.conn.execute(
            "INSERT OR IGNORE INTO detail_opportunities(round_id, shop_key, identity, created_at) "
            "VALUES (?, ?, ?, ?)",
            (round_id, shop_key, identity, utcnow()),
        )
        self.conn.commit()

    def bind_detail_opportunity(self, round_id: int, shop_key: str, identity: str,
                                offer_id: str) -> None:
        """把一次详情机会记录到它最终打开的商品编号上。"""
        self.conn.execute(
            "UPDATE detail_opportunities SET offer_id=? WHERE round_id=? AND shop_key=? "
            "AND identity=?",
            (offer_id, round_id, shop_key, identity),
        )
        self.conn.commit()

    def remove_detail_opportunity(self, round_id: int, shop_key: str, identity: str) -> None:
        """退还一次详情机会（同日跳过不消耗预算）。"""
        self.conn.execute(
            "DELETE FROM detail_opportunities WHERE round_id=? AND shop_key=? AND identity=?",
            (round_id, shop_key, identity),
        )
        self.conn.commit()

    def detail_attempts_used(self, round_id: int, shop_key: str, offer_id: str) -> int:
        """本轮该商品已经用掉的详情尝试次数（初次访问与补采共享同一份额度）。"""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) AS a FROM snapshots "
            "WHERE round_id=? AND shop_key=? AND offer_id=?",
            (round_id, shop_key, offer_id),
        ).fetchone()
        return int(row["a"])

    def mark_failure(
        self,
        round_id: int,
        shop_key: str,
        offer_id: str,
        attempt: int,
        note: str,
        *,
        shop_url: str | None = None,
        shop_name: str | None = None,
        product_url: str | None = None,
        product_name: str | None = None,
    ) -> None:
        row = self.conn.execute(
            "SELECT shop_url, shop_name, product_url, list_title AS product_name "
            "FROM shop_offers "
            "WHERE round_id=? AND shop_key=? AND offer_id=?",
            (round_id, shop_key, offer_id),
        ).fetchone()
        if row is None:
            if shop_url is None or shop_name is None or product_url is None:
                raise ValueError(f"缺少失败商品元数据：{shop_key}/{offer_id}")
            row = {
                "shop_url": shop_url,
                "shop_name": shop_name,
                "product_url": product_url,
                "product_name": product_name,
            }
        self.conn.execute(
            "INSERT INTO snapshots(round_id, shop_key, shop_url, shop_name, offer_id, product_url, "
            "product_name, collected_at, page_status, attempt, detail_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '失败', ?, ?)",
            (
                round_id, shop_key, row["shop_url"], row["shop_name"], offer_id,
                row["product_url"], row["product_name"], utcnow(), attempt, note,
            ),
        )
        self.conn.commit()

    def _clear_failures(self, round_id: int, shop_key: str, offer_id: str) -> None:
        self.conn.execute(
            "DELETE FROM snapshots WHERE round_id=? AND shop_key=? AND offer_id=? "
            "AND page_status!='成功'",
            (round_id, shop_key, offer_id),
        )

    def submit_inventory_snapshot(
        self,
        *,
        round_id: int,
        shop_key: str,
        shop_url: str,
        shop_name: str,
        offer_id: str,
        product_url: str,
        list_title: str | None,
        detail_title: str | None,
        main_image_url: str | None,
        sku_rows: list[dict],
        collected_at: str,
        attempt: int,
        image_evidence: dict | None = None,
    ) -> None:
        """原子提交一次完整商品库存快照。

        调用方只交出解析后的商品结果。相同轮次、店铺、商品和 SKU 的成功
        快照按 SKU 替换；本次未出现的旧 SKU 保留。是否停止本轮由调用方按
        轮次日期与当前时刻判定，提交本身不改变轮次状态。
        """
        if not sku_rows:
            raise ValueError("成功快照不能为空")
        collected_at = str(collected_at).strip() if collected_at is not None else ""
        if not collected_at:
            raise ValueError("成功快照必须有采集时间")
        offer_id = str(offer_id).strip() if offer_id is not None else ""
        shop_key = str(shop_key).strip() if shop_key is not None else ""
        product_url = str(product_url).strip() if product_url is not None else ""
        if not offer_id or not product_url or not shop_key:
            raise ValueError("成功快照缺少商品或店铺标识")

        list_name = str(list_title).strip() if list_title else ""
        detail_name = str(detail_title).strip() if detail_title else ""
        image_url = str(main_image_url).strip() if main_image_url else ""
        snapshot_name = list_name or detail_name or None
        normalized: list[dict] = []
        seen_sku_ids: set[str] = set()
        for sku in sku_rows:
            if not isinstance(sku, dict):
                raise ValueError("成功快照的每个 SKU 必须是结构化记录")
            sku_name = str(sku.get("sku_name") or "").strip()
            stock = sku.get("sku_stock")
            if not sku_name or stock is None or isinstance(stock, bool) or not isinstance(stock, int):
                raise ValueError("成功快照的每个 SKU 都必须有名称和整数库存")
            sku_id = sku.get("sku_id")
            if sku_id is None or str(sku_id).strip() == "":
                sku_id = hashlib.sha1(
                    f"{offer_id}|{sku_name}".encode("utf-8")
                ).hexdigest()[:16]
            else:
                sku_id = str(sku_id).strip()
            if sku_id in seen_sku_ids:
                raise ValueError(f"成功快照包含重复 SKU 编号：{sku_id}")
            seen_sku_ids.add(sku_id)
            # 图证据在事务前校验：坏证据要在这里当场报错，不能落进写库阶段把整单打回
            # （流水行 source 非空、带字节的行哈希与类型齐备，都是写路径的硬条件）。
            sku_image = sku.get("sku_image_evidence")
            if sku_image is not None:
                if not isinstance(sku_image, dict) or not str(sku_image.get("source") or "").strip():
                    raise ValueError("SKU 图证据必须带来源")
                if "content" in sku_image and not (sku_image.get("hash") and sku_image.get("mime")):
                    raise ValueError("SKU 图证据带字节时必须带哈希与图片类型")
            normalized.append({
                "round_id": round_id,
                "shop_key": shop_key,
                "shop_url": shop_url,
                "shop_name": shop_name,
                "offer_id": offer_id,
                "product_url": product_url,
                "product_name": snapshot_name,
                "sku_id": sku_id,
                "sku_name": sku_name,
                "sku_price": sku.get("sku_price"),
                "sku_stock": stock,
                "collected_at": collected_at,
                "page_status": "成功",
                "attempt": attempt,
                "sku_image_evidence": sku_image,
            })

        try:
            self.conn.execute("BEGIN")
            self._upsert_product(
                offer_id, product_url, detail_name or None, image_url or None,
            )
            self._upsert_skus(normalized)
            image = image_evidence or {'error': '未取得历史图片'}
            if 'content' in image:
                from .product_images import evidence
                try:
                    image = evidence(image['content'])
                except ValueError as exc:
                    image = {'error': str(exc)}
                if 'content' in image:
                    self.conn.execute('INSERT OR IGNORE INTO product_image_assets VALUES (?, ?, ?)',
                                      (image['hash'], image['mime'], image['content']))
            self._write_sku_image_versions(normalized, collected_at,
                                           fill_hash=image.get('hash'))
            # 同一秒里的同一次观测只留一条（VERSION_DEDUPE_INDEX）：重复提交不整单回滚。
            self.conn.execute(
                'INSERT OR IGNORE INTO product_information_versions '
                '(shop_key,offer_id,observed_at,observed_date,product_name,image_url,content_hash,image_error) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (shop_key, offer_id, collected_at, cst_date(collected_at),
                 detail_name or snapshot_name, image_url, image.get('hash'), image.get('error')))
            # 粒度由本次提交的行决定：只有默认行是商品级观测，其余是 SKU 级观测。
            self._clear_other_granularity(
                round_id, shop_key, offer_id, cst_date(collected_at),
                offer_level=all(row["sku_id"] == DEFAULT_SKU_ID for row in normalized),
            )
            for row in normalized:
                self.conn.execute(
                    "DELETE FROM snapshots WHERE round_id=? AND shop_key=? "
                    "AND offer_id=? AND sku_id=? AND page_status='成功'",
                    (round_id, shop_key, offer_id, row["sku_id"]),
                )
            self.conn.executemany(
                "INSERT INTO snapshots(round_id, shop_key, shop_url, shop_name, offer_id, product_url, "
                "product_name, sku_id, sku_name, sku_price, sku_stock, collected_at, page_status, "
                "attempt) VALUES (:round_id, :shop_key, :shop_url, :shop_name, :offer_id, "
                ":product_url, :product_name, :sku_id, :sku_name, :sku_price, :sku_stock, "
                ":collected_at, :page_status, :attempt)",
                normalized,
            )
            self._upsert_inventory(normalized)
            self._clear_failures(round_id, shop_key, offer_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _write_sku_image_versions(self, rows: list[dict], collected_at: str, *,
                                  fill_hash: str | None) -> None:
        """把每个 SKU 这次观测的图证据写成流水行（含空图与失败）；没带证据的行不写。

        字节先入内容寻址池（同哈希不重复存），行再落流水——流水行的哈希必须指向池里
        有的字节（外键）。同一秒里的重复提交按去重键折叠，不整单回滚（INSERT OR IGNORE）。

        代填行的哈希以 `fill_hash`（这次真正落池的主图）为准：主图这次没落池（没地址或
        校验没过）就没有可代填的字节，那一行如实记成无图。
        """
        day = cst_date(collected_at)
        ledger = []
        for row in rows:
            evidence = row.get("sku_image_evidence")
            if not evidence:
                continue
            if "content" in evidence:
                self.conn.execute('INSERT OR IGNORE INTO product_image_assets VALUES (?, ?, ?)',
                                  (evidence['hash'], evidence['mime'], evidence['content']))
            source = evidence.get("source")
            content_hash, error = evidence.get("hash"), evidence.get("error")
            if source == SKU_IMAGE_FILLED:
                content_hash, error = fill_hash, None
                if content_hash is None:
                    source = SKU_IMAGE_NONE
            ledger.append((row["shop_key"], row["offer_id"], row["sku_id"], collected_at, day,
                           evidence.get("url"), content_hash, error, source))
        if not ledger:
            return
        self.conn.executemany(
            'INSERT OR IGNORE INTO sku_image_versions (shop_key,offer_id,sku_id,observed_at,'
            'observed_date,image_url,content_hash,image_error,source) VALUES (?,?,?,?,?,?,?,?,?)',
            ledger)

    def _clear_other_granularity(self, round_id: int, shop_key: str, offer_id: str,
                                 day: str, *, offer_level: bool) -> None:
        """商品在单规格与多规格之间切换时，清掉相反粒度的记录。

        offer_level 为真表示本次是商品级观测（单规格商品的一条默认行），要清掉 SKU 级
        记录；反之清掉默认行。快照按本轮清，库存按当天清；更早日期的历史观测保留，
        因为分析侧按 SKU 从 stock 序列自算销量，跨粒度的历史行不会落进同一条序列。
        """
        # 只有这一个取反操作随粒度变化，取值来自下面两个字面量，不来自外部输入。
        other = "<>" if offer_level else "="
        self.conn.execute(
            "DELETE FROM snapshots WHERE round_id=? AND shop_key=? AND offer_id=? "
            f"AND page_status='成功' AND sku_id IS NOT NULL AND sku_id{other}?",
            (round_id, shop_key, offer_id, DEFAULT_SKU_ID),
        )
        self.conn.execute(
            f"DELETE FROM inventory WHERE shop_key=? AND offer_id=? AND date=? AND sku_id{other}?",
            (shop_key, offer_id, day, DEFAULT_SKU_ID),
        )

    def retry_product_image(self, version_id: int) -> dict:
        """Retry a failed latest observation without bypassing inventory deduplication.

        Acquired content is new evidence dated now, never backdated over the failure.
        """
        from .product_images import acquire
        version = self.conn.execute('SELECT * FROM product_information_versions WHERE id=?', (version_id,)).fetchone()
        if version is None or not version['image_error']:
            raise ValueError('只能重试失败的图片版本')
        image = acquire(version['image_url'])
        try:
            self.conn.execute('BEGIN IMMEDIATE')
            latest = self.conn.execute('SELECT id FROM product_information_versions WHERE shop_key=? AND offer_id=? ORDER BY observed_date DESC, observed_at DESC,id DESC LIMIT 1',
                                       (version['shop_key'], version['offer_id'])).fetchone()
            if latest['id'] != version_id:
                raise ValueError('已有更新商品版本，请重试最新失败版本')
            if 'content' in image:
                self.conn.execute('INSERT OR IGNORE INTO product_image_assets VALUES (?,?,?)',
                                  (image['hash'], image['mime'], image['content']))
            now = utcnow()
            # 同一秒里、同一图片结果的重试只留一条（VERSION_DEDUPE_INDEX）：被忽略时
            # 那次观测已在案，按去重键把已有那一行的 id 交回去。
            self.conn.execute(
                'INSERT OR IGNORE INTO product_information_versions '
                '(shop_key,offer_id,observed_at,observed_date,product_name,image_url,content_hash,image_error) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (version['shop_key'], version['offer_id'], now, cst_date(now), None,
                 version['image_url'], image.get('hash'), image.get('error')))
            new_version = self.conn.execute(
                "SELECT id FROM product_information_versions WHERE "
                + " AND ".join(f"{column}=?" for column in VERSION_DEDUPE_KEY)
                + " ORDER BY id DESC LIMIT 1",
                (version['shop_key'], version['offer_id'], now,
                 image.get('hash') or '', image.get('error') or ''),
            ).fetchone()['id']
            self.conn.commit()
            return {'retried_version': version_id, 'new_version': new_version,
                    'image_error': image.get('error')}
        except Exception:
            self.conn.rollback()
            raise

    def _upsert_skus(self, rows: list[dict]) -> None:
        now = utcnow()
        sku_rows = [
            (r["offer_id"], r.get("sku_name"), r.get("sku_id"), now, now)
            for r in rows
            if r.get("sku_id")
        ]
        if not sku_rows:
            return
        self.conn.executemany(
            "INSERT INTO skus(offer_id, sku_name, sku_id, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(offer_id, sku_id) DO UPDATE SET "
            "sku_name=excluded.sku_name, last_seen_at=excluded.last_seen_at",
            sku_rows,
        )

    def _upsert_inventory(self, rows: list[dict]) -> None:
        """每次抓到 SKU 库存写一条每日库存；同键覆盖本次观测值。

        纯 upsert：不再为算差分逐行回查本表（ADR-0032，`inventory.diff` 已退役）。
        """
        inventory_rows = [
            (r["shop_key"], r["offer_id"], r["sku_id"], cst_date(r.get("collected_at")),
             r.get("sku_stock"), r.get("sku_price"), r.get("shop_name"),
             r.get("product_name"), r.get("sku_name"))
            for r in rows
            if r.get("sku_id")
        ]
        if not inventory_rows:
            return
        self.conn.executemany(
            "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, price, "
            "shop_name, product_name, sku_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(shop_key, offer_id, sku_id, date) DO UPDATE SET "
            "stock=excluded.stock, price=excluded.price, "
            "shop_name=excluded.shop_name, product_name=excluded.product_name, "
            "sku_name=excluded.sku_name",
            inventory_rows,
        )

    def inventory_exists(self, shop_key: str, offer_id: str, date: str) -> bool:
        """某 (shop_key, offer_id, date) 是否已有完整库存记录。"""
        row = self.conn.execute(
            "SELECT 1 FROM inventory WHERE shop_key=? AND offer_id=? AND date=? "
            "AND stock IS NOT NULL LIMIT 1",
            (shop_key, offer_id, date),
        ).fetchone()
        return row is not None

    def inventory_exists_by_name(self, shop_key: str, product_name: str, date: str) -> bool:
        """某 (shop_key, product_name, date) 是否已有完整库存记录（按列表页商品名匹配）。"""
        if not product_name:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM inventory WHERE shop_key=? AND product_name=? AND date=? "
            "AND stock IS NOT NULL LIMIT 1",
            (shop_key, product_name, date),
        ).fetchone()
        return row is not None

    def find_offer_id_by_name(self, shop_key: str, product_name: str, date: str) -> str | None:
        """按 (shop_key, product_name, date) 找唯一 offer_id。

        用于「按名暂缓」时补记录该商品：只在能确定唯一 offer_id 时返回，避免同名多品时猜错。
        同名多品（多个不同 offer_id）返回 None。
        """
        if not product_name:
            return None
        rows = self.conn.execute(
            "SELECT DISTINCT offer_id FROM inventory "
            "WHERE shop_key=? AND product_name=? AND date=? AND stock IS NOT NULL",
            (shop_key, product_name, date),
        ).fetchall()
        ids = [r["offer_id"] for r in rows]
        return ids[0] if len(ids) == 1 else None

    def mark_skipped(self, round_id: int, shop_key: str, shop_url: str, shop_name: str,
                     offer_id: str, product_url: str, product_name: str | None = None,
                     note: str = "今日已有库存，跳过") -> None:
        """为已跳过的商品写一条 page_status='跳过' 的快照。

        跳过表示「本轮不访问详情」，不是库存观测：它让商品不再被待处理，
        但不会成为同日去重证据，也不算失败。
        """
        existing = self.conn.execute(
            "SELECT 1 FROM snapshots WHERE round_id=? AND shop_key=? AND offer_id=? "
            "AND page_status IN ('成功', '跳过') LIMIT 1",
            (round_id, shop_key, offer_id),
        ).fetchone()
        if existing is not None:
            return
        self.conn.execute(
            "INSERT INTO snapshots(round_id, shop_key, shop_url, shop_name, offer_id, "
            "product_url, product_name, collected_at, page_status, attempt, detail_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '跳过', 1, ?)",
            (round_id, shop_key, shop_url, shop_name, offer_id, product_url,
             product_name, utcnow(), note),
        )
        self.conn.commit()

    def _upsert_product(self, offer_id: str, product_url: str, product_name: str | None,
                        main_image_url: str | None = None) -> None:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO products(offer_id, product_url, product_name, main_image_url, "
            "first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(offer_id) DO UPDATE SET "
            "product_url=COALESCE(NULLIF(excluded.product_url, ''), products.product_url), "
            "product_name=COALESCE(NULLIF(excluded.product_name, ''), products.product_name), "
            "main_image_url=COALESCE(NULLIF(excluded.main_image_url, ''), products.main_image_url), "
            "last_seen_at=excluded.last_seen_at",
            (offer_id, product_url, product_name, main_image_url, now, now),
        )

    def round_tally(self, round_id: int) -> RoundTally:
        """一轮的计数：整轮合计 + 按店铺切片。

        「成功库存快照」「已处理」「孤儿」这些判据只在这一个 implementation 里定义；
        失败率、三个页面与摘要工具都从这里读同一份口径。

        三条 GROUP BY shop_key 的聚合（榜单行、快照、孤儿各一条）整轮一次算全；要某
        一家店时用 `RoundTally.shop()` 切片——界面每约 2 秒刷新一次，不能为每家店各跑
        一遍（IS-38 的护栏：实测逐店算 12 家要 1.3 秒，超了 1 秒上限）。三条都走覆盖
        索引，30 万行的合成库上整轮 0.46 秒；把两张表 JOIN 起来逐行核对是 1.5 秒，写
        成相关 EXISTS 更慢（46 秒）。
        """
        discovered = self._counts_by_shop(round_id, _DISCOVERED_TALLY_SQL)
        snapped = self._counts_by_shop(round_id, _SNAPSHOT_TALLY_SQL)
        orphaned = self._counts_by_shop(round_id, _ORPHAN_TALLY_SQL)

        per_shop = tuple(
            self._shop_tally(key, discovered, snapped, orphaned)
            for key in sorted(set(discovered) | set(snapped) | set(orphaned))
        )
        # 整轮合计就是各店相加：店铺编号互不重叠，没有跨店的商品。
        return RoundTally(
            total=ShopTally(
                shop_key=None,
                discovered=sum(one.discovered for one in per_shop),
                handled=sum(one.handled for one in per_shop),
                success_offers=sum(one.success_offers for one in per_shop),
                success_skus=sum(one.success_skus for one in per_shop),
                orphans=sum(one.orphans for one in per_shop),
            ),
            per_shop=per_shop,
            click_card_failures=self.click_card_failures(round_id),
        )

    def _counts_by_shop(self, round_id: int,
                        sql: str) -> dict[str, dict[str, int]]:
        """跑一条按店铺编号分组的聚合，交回 {店铺编号: {列名: 计数}}。

        列名取自查询自己的 `AS` 别名；没抓到的列（COUNT 遇不到行、SUM 全是 NULL）补 0。
        """
        cursor = self.conn.execute(sql, (round_id,))
        columns = [column[0] for column in cursor.description][1:]
        return {
            str(row[0]): {name: int(value or 0) for name, value in zip(columns, row[1:])}
            for row in cursor.fetchall()
        }

    @staticmethod
    def _shop_tally(shop_key: str, discovered, snapped, orphaned) -> ShopTally:
        """一家店的计数：榜单行给商品数，快照侧减去孤儿那一份就是榜单口径。

        「快照里有、榜单行里没有」的商品不算榜单里的商品（IS-49），所以减掉它之后
        剩下的正好是「榜单行里的商品」那份数——不需要把两张表 JOIN 起来逐行核对。
        """
        snapshot_side = snapped.get(shop_key, {})
        orphan_side = orphaned.get(shop_key, {})

        def listed(column: str) -> int:
            return snapshot_side.get(column, 0) - orphan_side.get(column, 0)

        return ShopTally(
            shop_key=shop_key,
            discovered=discovered.get(shop_key, {}).get("discovered", 0),
            handled=listed("handled"),
            success_offers=listed("success_offers"),
            success_skus=listed("success_skus"),
            orphans=orphan_side.get("orphans", 0),
        )

    def click_card_failures(self, round_id: int) -> int:
        """统计「点击后没拿到商品编号」的失败卡片数（按店 + 卡片位置去重）。

        判定：
          - 成功：该卡片出现过 click_ok（抓到 SKU）或 click_skipped（成功跳过）。
          - 失败：该卡片仅出现过 click_no_popup / click_url_notoffer / click_deny。
          - 不参与：click_parse_error（含旧名 click_parse_empty）——那张卡拿到了商品编号，
            它的失败由失败快照经 `failed_offers` 计入，这里再算一次就是双计。
        用于把「点击失败但没拿到 offer_id」的卡片也计入整轮失败率，避免被静默丢弃、
        导致成功率被高估、『失败率>阈值即暂停』失效。
        """
        rows = self.conn.execute(
            "SELECT shop_key, event, note FROM event_log "
            f"WHERE round_id=? AND event IN ({','.join('?' * len(_CLICK_CARD_EVENTS))})",
            (round_id, *_CLICK_CARD_EVENTS),
        ).fetchall()
        ok_keys: set = set()
        fail_keys: set = set()
        for r in rows:
            got = click_events.classify(r["event"], r["note"])
            if got is None:
                continue
            # 位置读不出来时退化成整条 note 作键（历史行如此；生产侧每条都带位置）。
            key = (r["shop_key"], got.card if got.card is not None else r["note"])
            if got.outcome in _CARD_OK_OUTCOMES:
                ok_keys.add(key)
            elif got.outcome in _CARD_FAILED_OUTCOMES:
                fail_keys.add(key)
        return len(fail_keys - ok_keys)

    def append_event(
        self,
        round_id: int,
        event: str,
        *,
        shop_key: str | None = None,
        offer_id: str | None = None,
        sku_id: str | None = None,
        phase: str | None = None,
        kind: str = "work",
        verification_type: str = "none",
        attempt: int | None = None,
        config_hash: str | None = None,
        note: str | None = None,
    ) -> None:
        """追加一条事件日志（只增不删）。interval_ms 相对同一 (round, shop) 上一条事件。"""
        ts = utcnow_us()
        prev = self.conn.execute(
            "SELECT ts FROM event_log WHERE round_id=? AND shop_key IS ? "
            "ORDER BY id DESC LIMIT 1",
            (round_id, shop_key),
        ).fetchone()
        prev_ts = prev["ts"] if prev else None
        interval_ms = None
        if prev_ts:
            try:
                interval_ms = int(
                    (datetime.fromisoformat(ts) - datetime.fromisoformat(prev_ts)).total_seconds() * 1000
                )
            except ValueError:
                interval_ms = None

        self.conn.execute(
            "INSERT INTO event_log(round_id, shop_key, offer_id, sku_id, phase, event, kind, "
            "ts, prev_ts, interval_ms, verification_type, attempt, config_hash, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                round_id, shop_key, offer_id, sku_id, phase, event, kind, ts, prev_ts,
                interval_ms, verification_type, attempt, config_hash, note,
            ),
        )
        self.conn.commit()

    def record_params(self, cfg) -> str:
        """把本次生效的延迟/反爬配置写入 run_params（幂等去重），返回 params_hash。"""
        h = params_hash(cfg)
        payload = json.dumps(params_snapshot(cfg), sort_keys=True, ensure_ascii=False, default=str)
        self.conn.execute(
            "INSERT OR IGNORE INTO run_params(params_hash, captured_at, config_json) "
            "VALUES (?, ?, ?)",
            (h, utcnow(), payload),
        )
        self.conn.commit()
        return h

    def event_logger(self, round_id: int, config_hash: str | None = None):
        """返回一个 emit 回调，绑定 round_id/config_hash，供浏览器埋点使用。
        记录失败只写日志，绝不中断抓取主流程。"""
        def emit(event: str, **kw: object) -> None:
            try:
                self.append_event(round_id, event, config_hash=config_hash, **kw)
            except Exception as exc:  # noqa: BLE001
                log.debug("事件记录失败 %s：%s", event, exc)
        return emit

    def commit(self) -> None:
        self.conn.commit()
