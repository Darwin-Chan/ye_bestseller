"""SQLite 数据层：轮次、店铺榜单、SKU 快照、差分更新。"""
from __future__ import annotations

import logging
import sqlite3
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .parse import DEFAULT_SKU_ID

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    phase TEXT NOT NULL DEFAULT 'listing',
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
    diff INTEGER,
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
-- 索引的话，12 家店要各扫一遍本轮全部事件（IS-38：30 万行一次刷新 0.80 → 0.09 秒）。
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
    note TEXT
);
"""

# 同一轮、店铺、商品和 SKU 至多一条成功快照：靠唯一索引保证（见 connect() 的迁移）。
SNAPSHOT_SUCCESS_INDEX = "idx_snapshots_success_key"

# 过程页刷新的两条索引：逐店 deny 计数、逐店时间跨度（名字要与上面 SCHEMA 里的
# 两条 CREATE INDEX 一致，tests/test_db.py 有用例守着）。
EVENT_REFRESH_INDEXES = (
    "idx_event_log_round_shop_event",
    "idx_event_log_round_shop_ts",
)


def _has_snapshot_success_index(conn: sqlite3.Connection) -> bool:
    """唯一索引在不在——在，就说明这个库已经过了那次整表去重。"""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (SNAPSHOT_SUCCESS_INDEX,),
    ).fetchone() is not None


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utcnow_us() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


CST = timezone(timedelta(hours=8))


# 延迟/反爬相关配置键：用于生成 run_params 快照与 params_hash
PARAMS_KEYS = (
    "detail_delay_sec", "list_delay_sec", "long_pause_interval", "long_pause_sec",
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


_CARD_POS_RE = re.compile(r"page=(\d+)&idx=(\d+)")


def _card_pos(note: str | None) -> tuple[int, int] | None:
    """从事件 note 中解析卡片位置 (page, idx)，用于按卡片去重。"""
    if not note:
        return None
    m = _CARD_POS_RE.search(note)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _migrate_round_columns(conn: sqlite3.Connection) -> None:
    """给旧轮次补上日期与终态，然后丢掉过渡用的中文状态列；可重复执行。"""
    columns = {row[1] for row in conn.execute('PRAGMA table_info("rounds")').fetchall()}
    if not columns:
        return
    has_reason = "terminal_reason" in columns
    has_status = "status" in columns
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
        if has_reason and has_status and row["terminal_reason"] is None:
            reason = legacy_terminal_reason(row["status"], row["note"])
            if reason is not None:
                conn.execute(
                    "UPDATE rounds SET terminal_reason=? WHERE id=?", (reason, row["id"])
                )
    if has_status:
        conn.execute("ALTER TABLE rounds DROP COLUMN status")
        log.info("已删除过渡用的 rounds.status 列（「进行中」改由没有终态表达）")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # 迁移：删除 shops.active 列（旧库）
    try:
        info = conn.execute('PRAGMA table_info("shops")').fetchall()
        if any(r[1] == "active" for r in info):
            conn.execute("ALTER TABLE shops DROP COLUMN active")
            conn.commit()
    except sqlite3.OperationalError:
        pass
    # 迁移：旧 skus 主键为 (offer_id, sku_name)，统一改为 (offer_id, sku_id)，保留数据并去重
    try:
        info = conn.execute('PRAGMA table_info("skus")').fetchall()
        pk_cols = [r[1] for r in info if r[5] > 0]
        if pk_cols and "sku_name" in pk_cols:
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
    except sqlite3.OperationalError:
        pass
    # 兼容旧库：补 products.last_seen_at 列（已存在则忽略）
    try:
        conn.execute("ALTER TABLE products ADD COLUMN last_seen_at TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE products ADD COLUMN main_image_url TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE shop_rounds ADD COLUMN list_note TEXT")
    except sqlite3.OperationalError:
        pass
    # 兼容旧库：轮次记录的详情预算上限（NULL 表示尚未绑定，首次申请时写入）
    try:
        conn.execute("ALTER TABLE rounds ADD COLUMN detail_budget_limit INTEGER")
    except sqlite3.OperationalError:
        pass
    # 兼容旧库：轮次身份（北京日期）与终态标识（稳定标识取代中文状态串的判定作用）
    try:
        conn.execute("ALTER TABLE rounds ADD COLUMN run_date TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE rounds ADD COLUMN terminal_reason TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        _migrate_round_columns(conn)
        conn.commit()
    except sqlite3.OperationalError as exc:
        log.debug("轮次列迁移失败：%s", exc)
    # 兼容旧库：详情机会记录上绑定的商品编号（旧库残留的 attempts 列不再使用）
    try:
        conn.execute("ALTER TABLE detail_opportunities ADD COLUMN offer_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        info = conn.execute('PRAGMA table_info("skus")').fetchall()
        if any(r[1] == "main_image_url" for r in info):
            conn.execute("ALTER TABLE skus DROP COLUMN main_image_url")
            conn.commit()
    except sqlite3.OperationalError:
        pass
    # 迁移：物理删除 snapshots.stock_delta（旧报表口径，SKU 差分改用 inventory.diff）
    try:
        info = conn.execute('PRAGMA table_info("snapshots")').fetchall()
        if any(r[1] == "stock_delta" for r in info):
            conn.execute("ALTER TABLE snapshots DROP COLUMN stock_delta")
            conn.commit()
            log.info("已物理删除 snapshots.stock_delta 列")
    except sqlite3.OperationalError as exc:
        log.debug("删除 snapshots.stock_delta 列失败（可能已删除或版本不支持）：%s", exc)
    # 迁移：同一轮、店铺、商品和 SKU 只保留最新成功快照，再建立最终唯一约束。
    # 失败记录与无 SKU 的跳过记录不参与该约束。极旧库可能还没有 page_status，
    # 先完成其列迁移，等新写入路径可用时再建立约束。
    #
    # 去重是整表扫描，而界面每次刷新都走一次 connect()。所以它只在「唯一索引还不在」
    # 时跑：索引一旦建好，重复行不可能再写进来，后面的开库不该再付这笔随快照总量线性
    # 增长的成本（IS-38 实测：WAL 库 30 万行上，这次去重 + 建索引约 0.42 秒；索引已在
    # 时 connect() 约 1.6 毫秒）。
    snapshot_cols = {
        row[1] for row in conn.execute('PRAGMA table_info("snapshots")').fetchall()
    }
    if ({"id", "round_id", "shop_key", "offer_id", "sku_id", "page_status"} <= snapshot_cols
            and not _has_snapshot_success_index(conn)):
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
        log.info("快照去重迁移：删除 %d 行重复成功记录并建立唯一索引 %s",
                 removed, SNAPSHOT_SUCCESS_INDEX)
    conn.commit()
    return conn


class Database:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def set_phase(self, round_id: int, phase: str) -> None:
        self.conn.execute("UPDATE rounds SET phase=? WHERE id=?", (phase, round_id))
        self.conn.commit()

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

    def record_crawler_process(self, pid: int, round_id: int | None, note: str | None = None) -> None:
        """登记正在跑的采集进程（单行）。

        这行只服务于「界面显示谁在跑、能不能中止它」；是不是真的有进程在跑，
        以会话锁为准（见 single_instance）。被强杀的进程会留下这行，读到的人负责清。
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO crawler_process(id, pid, round_id, started_at, note) "
            "VALUES (1, ?, ?, ?, ?)",
            (int(pid), round_id, utcnow(), note),
        )
        self.conn.commit()

    def crawler_process(self):
        """正在跑的采集进程身份；没有登记时返回 None。"""
        return self.conn.execute("SELECT * FROM crawler_process WHERE id=1").fetchone()

    def clear_crawler_process(self) -> None:
        self.conn.execute("DELETE FROM crawler_process WHERE id=1")
        self.conn.commit()

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
            })

        try:
            self.conn.execute("BEGIN")
            self._upsert_product(
                offer_id, product_url, detail_name or None, image_url or None,
            )
            self._upsert_skus(normalized)
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

    def _clear_other_granularity(self, round_id: int, shop_key: str, offer_id: str,
                                 day: str, *, offer_level: bool) -> None:
        """商品在单规格与多规格之间切换时，清掉相反粒度的记录。

        offer_level 为真表示本次是商品级观测（单规格商品的一条默认行），要清掉 SKU 级
        记录；反之清掉默认行。快照按本轮清，库存按当天清；更早日期的历史观测保留，
        因为差分基准只取同一 SKU，不会跨粒度比较。
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
        """每次抓到 SKU 库存写一条每日库存；diff = 今日 stock − 最近一个更早日期 stock。"""
        for r in rows:
            sku_id = r.get("sku_id")
            if not sku_id:
                continue
            day = cst_date(r.get("collected_at"))
            stock = r.get("sku_stock")
            price = r.get("sku_price")
            prev = self.conn.execute(
                "SELECT stock FROM inventory WHERE shop_key=? AND offer_id=? AND sku_id=? "
                "AND date < ? ORDER BY date DESC LIMIT 1",
                (r["shop_key"], r["offer_id"], sku_id, day),
            ).fetchone()
            diff = None
            if prev is not None and stock is not None and prev["stock"] is not None:
                diff = int(stock) - int(prev["stock"])
            self.conn.execute(
                "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, diff, price, "
                "shop_name, product_name, sku_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(shop_key, offer_id, sku_id, date) DO UPDATE SET "
                "stock=excluded.stock, diff=excluded.diff, price=excluded.price, "
                "shop_name=excluded.shop_name, product_name=excluded.product_name, "
                "sku_name=excluded.sku_name",
                (
                    r["shop_key"], r["offer_id"], sku_id, day, stock, diff, price,
                    r.get("shop_name"), r.get("product_name"), r.get("sku_name"),
                ),
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

    def success_rows(self, round_id: int) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM snapshots WHERE round_id=? AND page_status='成功' "
            "AND sku_id IS NOT NULL",
            (round_id,),
        )
        return cur.fetchall()

    def failed_rows(self, round_id: int) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM snapshots WHERE round_id=? AND page_status NOT IN ('成功', '跳过')",
            (round_id,),
        )
        return cur.fetchall()

    def offer_counts(self, round_id: int) -> tuple[int, int]:
        total = self.conn.execute(
            "SELECT COUNT(*) AS c FROM ("
            "SELECT shop_key, offer_id FROM shop_offers WHERE round_id=? "
            "GROUP BY shop_key, offer_id)",
            (round_id,),
        ).fetchone()["c"]
        ok = self.conn.execute(
            "SELECT COUNT(*) AS c FROM ("
            "SELECT so.shop_key, so.offer_id FROM shop_offers so "
            "WHERE so.round_id=? AND EXISTS ("
            "SELECT 1 FROM snapshots s WHERE s.round_id=so.round_id "
            "AND s.shop_key=so.shop_key AND s.offer_id=so.offer_id "
            "AND s.page_status IN ('成功', '跳过')) "
            "GROUP BY so.shop_key, so.offer_id)",
            (round_id,),
        ).fetchone()["c"]
        return int(total), int(ok)

    def click_card_failures(self, round_id: int) -> int:
        """统计「点击后从未抓到任何 SKU」的失败卡片数（按 shop+page+idx 去重）。

        判定：
          - 成功：该卡片出现过 click_ok（抓到 SKU）或 click_skipped（成功跳过）。
          - 失败：该卡片仅出现过 click_no_popup / click_url_notoffer / click_deny。
        用于把「点击失败但没拿到 offer_id」的卡片也计入整轮失败率，避免被静默丢弃、
        导致成功率被高估、『失败率>阈值即暂停』失效。
        """
        rows = self.conn.execute(
            "SELECT shop_key, event, note FROM event_log "
            "WHERE round_id=? AND event IN "
            "('click_ok','click_skipped','click_no_popup','click_url_notoffer','click_deny')",
            (round_id,),
        ).fetchall()
        ok_keys: set[tuple] = set()
        fail_keys: set[tuple] = set()
        for r in rows:
            pos = _card_pos(r["note"])
            key = (r["shop_key"], pos) if pos else (r["shop_key"], r["note"])
            if r["event"] in ("click_ok", "click_skipped"):
                ok_keys.add(key)
            else:
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
