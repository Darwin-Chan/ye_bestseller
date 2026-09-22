"""导出：一致性快照 → 周包 → 推入本机 raw 库；图片按内容寻址只传新增（spec §2/§3/§7）。

**包是什么**（与票据 09 的汇总侧对接的口径，写死在这里）：

- 一个 SQLite 文件（提交前压成 `.db.gz`），落在 `raw-<机器>` 的
  `data/<年>/<周>-<机器>.db.gz`（如 `data/2026/W38-m1.db.gz`）。
- 包内 = 交换集六表 + 一张元数据表 `exchange_meta`。列集**显式**列出、不 `SELECT *`
  （包与本地库的列可以不同；汇总侧同样按显式投影读）。
- **周窗口**：包是「这一周的切片」——`inventory` 按 `date`、`product_information_versions`
  按 `observed_date` 取窗口内（北京日期，ISO 周周一至周日）；`product_image_assets` 只收
  被本周版本行引用的（只带元数据，字节不进包，spec §3）；身份表（`shops`/`products`/`skus`）
  收本周观测碰到的那些行。每周一片，全部周包的并集 = 全量——补历史（W36/W37）与冷启动重放
  都靠这一条。
- **只发「自己那份」**（票据 10 的报告口径逼出来的第一条，spec §9 的交换集不含来源列）：
  观测表按 (店铺, 日期) 组过滤——组归取胜 claim 的机器（`merge_claims`，票据 09），本机采的
  组照发、账说归别人的不发；身份表按 `merge_seen` 的最近写者同理。不滤的话，导进来的行会
  以本机名义再发一遍，别的机器收回去就成了同一批数据两个 claim（幻影冲突、来源换手），
  而且合并之后再导出永远「有新数据」，原型样例二（同周第二次：无新提交）对不上。
- **缺图的行**：汇总来的版本行可能带 `content_hash` 而本机没有对应资产行（图还没拉到）。
  这种行不进清单——key 的扩展名无从得知；有字节的那台机器的包列着它，别人从那份清单取。
- 元数据表 `exchange_meta` 是键值对：`machine_id`、`week`、`generated_at`、`format_version`
  （导出程序口径版本）、`crawl_in_progress`，以及每表一行计数 `rows_<表名>`。
- **版本行的 `id` 不进包**：它是本机 rowid，导入侧会重排（spec §9 的已知约束），
  去重键是 `(shop_key, offer_id, observed_at, content_hash, image_error)`，不靠 id。

**一致性快照**：源库只读打开、单个 `BEGIN` 事务里把所有行读完（WAL 下不锁正在跑的
采集，也看不见任何未提交的半截事务），再往包库里写。源库自始至终只读，export 不改本机库。

**同周重跑不产生多余提交**：拿新包的**内容摘要**（`package_digest`，六表逐行的规范摘要，
不含生成时刻这类每次都会变的东西）与 raw 库里已发布那份比——比的是 **HEAD 里那份**
（远端的事实），工作区里躺着的残迹不算数；一样就不写文件、不提交，只把推送再试一次
（上次推失败留下的那笔在这里补上，真的是空的推送是个空动作）。
内容摘要可以跨重跑、跨机器复现（行序按列排序，与 SQLite 文件布局无关）。

**发布**：包与图片清单（`<周>-<机器>.manifest.json.gz`，列出包引用的图片 key 集）同一次
提交推送；push 失败就把克隆退回远端状态（包已在 outbox 里，下次重跑重发），本机库不动。
图片上传在包推送之后，且**每次导出都跑**（同内容重跑会把上次没传上去的图补上）：
把清单里的 key 与桶内已有 key 比对，只传新增；本机没有字节的 key 如实记进结果
（对方汇总时按缺图记缺）。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import gzip
import hashlib
import json
import pathlib
import re
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping

from bestseller_monitor import crawler_identity
from bestseller_monitor.cos_store import CosCliImageStore
from bestseller_monitor.db import CST, cst_date
from bestseller_monitor.git_channel import ChannelError, GitChannel
from bestseller_monitor.identity_key import identity_row_key_sql
from bestseller_monitor.image_store import ImageStore, ImageStoreError, image_key
from bestseller_monitor.weekly_plan import iso_week_label, week_monday
from bestseller_monitor.weekly_plan import week_window as plan_week_window

# 导出程序口径版本：包格式（表、列、筛选口径）变化时进一位——发布侧据此重发，
# 汇总侧据此认出旧包。
EXPORT_FORMAT_VERSION = "v1"

META_TABLE = "exchange_meta"
PACKAGE_SUFFIX = ".db.gz"
MANIFEST_SUFFIX = ".manifest.json.gz"

# 身份表收「本周观测碰到的那些行」：offer/店铺出现在本周的库存行或版本行里。
_WEEK_OFFERS = (
    "SELECT offer_id FROM inventory WHERE date BETWEEN :start AND :end "
    "UNION SELECT offer_id FROM product_information_versions "
    "WHERE observed_date BETWEEN :start AND :end"
)
_WEEK_SHOPS = (
    "SELECT shop_key FROM inventory WHERE date BETWEEN :start AND :end "
    "UNION SELECT shop_key FROM product_information_versions "
    "WHERE observed_date BETWEEN :start AND :end"
)


@dataclasses.dataclass(frozen=True)
class _PackageTable:
    """包内一张交换集表：列（列名, 声明）按包里的列序、主键、按周窗口取数的 SQL。

    DDL、INSERT 的列序、内容摘要都从 `columns` 这一处生成——三份各写一遍就会悄悄错列。
    `owned` 是「这一行归本机」的过滤条件（见 `_OWNED_*`）：观测表按 (店铺, 日期) 组的
    取胜 claim、身份表按最近一次观测的写者。切片 SQL 用 `{owned}` 占位。
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    primary_key: tuple[str, ...] = ()
    slice_sql: str = ""                   # 用 {cols} / {owned} 占位
    owned: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)

    @property
    def ddl(self) -> str:
        parts = [f"{name} {decl}" for name, decl in self.columns]
        if self.primary_key:
            parts.append(f"PRIMARY KEY ({', '.join(self.primary_key)})")
        return f"CREATE TABLE {self.name} ({', '.join(parts)});"


# 观测表只发「自己那份」：一个 (店铺, 日期) 组的行归取胜 claim 的机器（`merge_claims`，
# 票据 09）。本机采的组不在账里（含本机取胜的组）→ 照发；账说归别人 → 不发——不然
# 导入回来的行会以本机名义再发一遍：别的机器收回去就是同一批数据两个 claim（幻影冲突、
# 来源换手），「同周重跑不产生多余提交」也不再成立（票据 10 的报告要求第二次运行仍是
# 「无新提交」，三份原型样例都按这条画）。
#
# 认账要连**时刻**一起对（与 `merge._local_claim` 同一课）：时刻对不上说明本机导入之后
# 又采过这一组（越权补采、逃生口自由采集、周中交接、晚间补采），库里已是本机的新 claim、
# 账还没刷新——那种组必须照发，否则本机与全世界分叉、真冲突也不再浮出来。
_OWNED_OBSERVED = (
    "NOT EXISTS (SELECT 1 FROM merge_claims c WHERE c.shop_key = {table}.shop_key "
    "AND c.observed_date = {table}.{date} AND c.machine_id <> :machine "
    "AND c.claim_at = (SELECT MAX(v.observed_at) FROM product_information_versions v "
    "WHERE v.shop_key = {table}.shop_key AND v.observed_date = {table}.{date}))")
# 身份表同理，按 `merge_seen`（描述列最近一次观测的写者）与同一套时刻判据
# （`merge._seen_machine`：账里的 seen_at 等于本机现读到的 last_seen_at 才认账）；
# 行键的拼法在 `identity_key` 一处（SQL 版由分隔符码点现推）。
_OWNED_IDENTITY = (
    "NOT EXISTS (SELECT 1 FROM merge_seen s WHERE s.table_name = '{name}' "
    "AND s.row_key = {key} AND s.machine_id <> :machine "
    "AND s.seen_at = COALESCE({table}.last_seen_at, ''))")


def _owned_observed(table: str, date: str) -> str:
    return _OWNED_OBSERVED.format(table=table, date=date)


def _owned_identity(name: str, primary_key: tuple[str, ...]) -> str:
    key = identity_row_key_sql(f"{name}.{column}" for column in primary_key)
    return _OWNED_IDENTITY.format(name=name, key=key, table=name)


EXCHANGE_TABLES: tuple[_PackageTable, ...] = (
    _PackageTable(
        "shops",
        (("shop_key", "TEXT"), ("shop_name", "TEXT"), ("shop_url", "TEXT"),
         ("first_seen_at", "TEXT"), ("last_seen_at", "TEXT")),
        primary_key=("shop_key",),
        slice_sql=f"SELECT {{cols}} FROM shops WHERE shop_key IN ({_WEEK_SHOPS}) "
                  "AND {owned} ORDER BY {cols}",
        owned=_owned_identity("shops", ("shop_key",)),
    ),
    _PackageTable(
        "products",
        (("offer_id", "TEXT"), ("product_url", "TEXT"), ("product_name", "TEXT"),
         ("main_image_url", "TEXT"), ("first_seen_at", "TEXT NOT NULL"),
         ("last_seen_at", "TEXT")),
        primary_key=("offer_id",),
        slice_sql=f"SELECT {{cols}} FROM products WHERE offer_id IN ({_WEEK_OFFERS}) "
                  "AND {owned} ORDER BY {cols}",
        owned=_owned_identity("products", ("offer_id",)),
    ),
    _PackageTable(
        "skus",
        (("offer_id", "TEXT NOT NULL"), ("sku_name", "TEXT"), ("sku_id", "TEXT NOT NULL"),
         ("first_seen_at", "TEXT NOT NULL"), ("last_seen_at", "TEXT")),
        primary_key=("offer_id", "sku_id"),
        slice_sql=f"SELECT {{cols}} FROM skus WHERE offer_id IN ({_WEEK_OFFERS}) "
                  "AND {owned} ORDER BY {cols}",
        owned=_owned_identity("skus", ("offer_id", "sku_id")),
    ),
    _PackageTable(
        "inventory",
        (("shop_key", "TEXT NOT NULL"), ("offer_id", "TEXT NOT NULL"),
         ("sku_id", "TEXT NOT NULL"), ("date", "TEXT NOT NULL"),
         ("stock", "INTEGER"), ("price", "REAL"), ("shop_name", "TEXT"),
         ("product_name", "TEXT"), ("sku_name", "TEXT")),
        primary_key=("shop_key", "offer_id", "sku_id", "date"),
        slice_sql="SELECT {cols} FROM inventory WHERE date BETWEEN :start AND :end "
                  "AND {owned} ORDER BY {cols}",
        owned=_owned_observed("inventory", "date"),
    ),
    _PackageTable(
        "product_information_versions",
        (("shop_key", "TEXT NOT NULL"), ("offer_id", "TEXT NOT NULL"),
         ("observed_at", "TEXT NOT NULL"), ("observed_date", "TEXT NOT NULL"),
         ("product_name", "TEXT"), ("image_url", "TEXT"),
         ("content_hash", "TEXT"), ("image_error", "TEXT")),
        slice_sql="SELECT {cols} FROM product_information_versions "
                  "WHERE observed_date BETWEEN :start AND :end "
                  "AND {owned} ORDER BY {cols}",
        owned=_owned_observed("product_information_versions", "observed_date"),
    ),
    _PackageTable(
        "product_image_assets",
        (("content_hash", "TEXT"), ("mime", "TEXT NOT NULL")),
        primary_key=("content_hash",),
        slice_sql="SELECT {cols} FROM product_image_assets WHERE content_hash IN ("
                  "SELECT content_hash FROM product_information_versions "
                  "WHERE observed_date BETWEEN :start AND :end AND content_hash IS NOT NULL "
                  "AND {owned}) ORDER BY {cols}",
        owned=_owned_observed("product_information_versions", "observed_date"),
    ),
)

_PACKAGE_SCHEMA = ("\n".join(table.ddl for table in EXCHANGE_TABLES)
                   + f"\nCREATE TABLE {META_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL);\n")


def week_window(week: str) -> tuple[str, str]:
    """ISO 周编号（如 2026-W37）的北京日期窗口：周一与周日。

    口径在 `weekly_plan.week_window` 一处；这里只把日期折成取数 SQL 用的字符串。
    """
    monday, sunday = plan_week_window(week)
    return monday.isoformat(), sunday.isoformat()


def current_week() -> str:
    """现在的北京日期落在哪个 ISO 周。"""
    return iso_week_label(dt.date.fromisoformat(cst_date()))


def package_rel_path(week: str, machine_id: str) -> str:
    """包在 raw 库里的相对路径：data/<年>/<周>-<机器>.db.gz（spec §2 的布局）。

    年取周编号里那个（2026-W01 即使周一落在 2025 年，也归 2026——与计划文件的命名同一口径）。
    """
    week_monday(week)                     # 非法周编号在这里点名拒绝
    year, number = week.split("-W")
    return f"data/{year}/W{number}-{machine_id}{PACKAGE_SUFFIX}"


# 包文件名里的周号：`W39-m2.db.gz` 为准，spec 字面的裸周号 `39-m2.db.gz` 也认
# （年份由 data/<年> 目录钉住，机器标识是文件名末尾的后缀）。
_PACKAGE_WEEK = re.compile(r"^W?(\d{1,2})-")


def package_week(name: str) -> int | None:
    """包文件名里的周号；不是包、认不出周号时返回 None。"""
    if not name.endswith(PACKAGE_SUFFIX):
        return None
    match = _PACKAGE_WEEK.match(name[: -len(PACKAGE_SUFFIX)])
    return int(match.group(1)) if match else None


@dataclasses.dataclass(frozen=True)
class PackageRef:
    """交换区里的一个周包：哪台机器发的、包文件在哪、相对交换区根的路径。

    `machine` 与 `rel` 都从路径本身读出来（`raw-<机器>/data/<年>/<周>-<机器>.db.gz`），
    找包的人不必自己再解析一遍文件名。
    """

    machine: str
    path: pathlib.Path
    rel: str

    @property
    def week(self) -> str | None:
        """包名与年目录折出的周编号（`2026-W38`）；认不出时 None。"""
        number = package_week(self.path.name)
        year = self.path.parent.name
        return f"{year}-W{number:02d}" if number is not None and year.isdigit() else None


def _package_ref(repo: pathlib.Path, path: pathlib.Path,
                 exchange_root: pathlib.Path) -> PackageRef | None:
    """把一个文件认成包：名字里认得出周号、机器后缀与所在 `raw-*` 目录对得上。

    认不出的不是包（周号乱写、放错目录的都算）——两个找包的读口共用这一处判定。
    """
    machine = repo.name[len("raw-"):]
    if not machine or package_week(path.name) is None or \
            not path.name[: -len(PACKAGE_SUFFIX)].endswith(f"-{machine}"):
        return None
    return PackageRef(machine=machine, path=path,
                      rel=path.relative_to(exchange_root).as_posix())


def week_packages(exchange_root: str | pathlib.Path, week: str) -> tuple[PackageRef, ...]:
    """交换区里某一周的包，按机器与文件名排序。

    只读本机交换区**已经有**的克隆（不拉取），一个包都不解压——读包内容是调用方的事
    （`merge.package_shop_days` / `merge.import_package`）。年目录取周编号里的 ISO 年：
    2026-W01 的周一落在 2025-12-29，目录仍是 data/2026/（与 `package_rel_path` 同一口径）。
    """
    root = pathlib.Path(exchange_root)
    iso = week_monday(week).isocalendar()
    year_dir, week_no = str(iso.year), iso.week
    found: list[PackageRef] = []
    for repo in sorted(root.glob("raw-*")):
        if not repo.is_dir():
            continue
        for path in sorted((repo / "data" / year_dir).glob(f"*{PACKAGE_SUFFIX}")):
            if package_week(path.name) != week_no:
                continue
            ref = _package_ref(repo, path, root)
            if ref is not None:
                found.append(ref)
    return tuple(found)


def all_packages(exchange_root: str | pathlib.Path) -> tuple[PackageRef, ...]:
    """交换区里全部周的包，按周、机器、文件名排序——冷启动重放读它（spec §11）。

    同 `week_packages` 的口径：只读本机**已经有**的克隆、一个包都不解压；认不出周号
    或机器对不上文件名的文件不算包。
    """
    root = pathlib.Path(exchange_root)
    found: list[PackageRef] = []
    for repo in sorted(root.glob("raw-*")):
        if not repo.is_dir():
            continue
        for path in sorted(repo.glob(f"data/*/*{PACKAGE_SUFFIX}")):
            ref = _package_ref(repo, path, root)
            if ref is not None:
                found.append(ref)
    return tuple(sorted(found, key=lambda ref: (ref.week or "", ref.machine, ref.path.name)))


def read_package_meta(conn: sqlite3.Connection) -> dict:
    """把 exchange_meta 折成调用方要的形状：机器、周、时刻、口径版本、是否在采、各表行数。"""
    raw = {key: value for key, value in
           conn.execute(f"SELECT key, value FROM {META_TABLE}").fetchall()}
    return {
        "machine_id": raw["machine_id"],
        "week": raw["week"],
        "generated_at": raw["generated_at"],
        "format_version": raw["format_version"],
        "crawl_in_progress": raw["crawl_in_progress"] == "1",
        "rows": {table.name: int(raw[f"rows_{table.name}"]) for table in EXCHANGE_TABLES},
    }


def _read_week_slice(source: pathlib.Path, week: str, machine_id: str) -> dict[str, list[tuple]]:
    """在源库的一个只读事务里读完本周切片：一致快照，且看不见未提交的事务。

    只读连接（`mode=ro`）不跑迁移、不写源库；WAL 下读事务与正在跑的采集互不打架。
    `machine_id` 供「只发自己那份」的过滤（见 `_OWNED_OBSERVED` / `_OWNED_IDENTITY`）。
    """
    uri = f"{source.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("BEGIN")
        params = dict(zip(("start", "end"), week_window(week)), machine=machine_id)
        slice_: dict[str, list[tuple]] = {}
        for table in EXCHANGE_TABLES:
            sql = table.slice_sql.format(cols=", ".join(table.names), owned=table.owned)
            slice_[table.name] = [tuple(row) for row in conn.execute(sql, params)]
        conn.rollback()
        return slice_
    finally:
        conn.close()


def build_package(source: pathlib.Path, package_path: pathlib.Path, *, week: str,
                  machine_id: str, crawl_in_progress: bool,
                  generated_at: dt.datetime) -> None:
    """把源库的本周切片写成一个包文件（SQLite）。

    带 `generated_at`（北京时刻）与 `crawl_in_progress`——它们是这一份包的元数据，
    不影响内容摘要（同数据重跑仍是同一份内容）。
    """
    slice_ = _read_week_slice(pathlib.Path(source), week, machine_id)
    package_path = pathlib.Path(package_path)
    package_path.parent.mkdir(parents=True, exist_ok=True)
    if package_path.exists():
        package_path.unlink()
    conn = sqlite3.connect(package_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")     # 版本行可能引用本机缺字节的资产
        conn.executescript(_PACKAGE_SCHEMA)
        for table in EXCHANGE_TABLES:
            rows = slice_[table.name]
            if not rows:
                continue
            conn.executemany(
                f"INSERT INTO {table.name} VALUES ({', '.join('?' * len(table.columns))})",
                rows)
        conn.executemany(
            f"INSERT INTO {META_TABLE}(key, value) VALUES (?, ?)",
            [(key, value) for key, value in _meta_rows(
                week=week, machine_id=machine_id, crawl_in_progress=crawl_in_progress,
                generated_at=generated_at,
                counts={table.name: len(slice_[table.name]) for table in EXCHANGE_TABLES})])
        conn.commit()
    finally:
        conn.close()


def _meta_rows(*, week: str, machine_id: str, crawl_in_progress: bool,
               generated_at: dt.datetime, counts: Mapping[str, int]) -> list[tuple[str, str]]:
    rows = [
        ("machine_id", machine_id),
        ("week", week),
        ("generated_at", generated_at.isoformat(timespec="seconds")),
        ("format_version", EXPORT_FORMAT_VERSION),
        ("crawl_in_progress", "1" if crawl_in_progress else "0"),
    ]
    rows += [(f"rows_{table.name}", str(counts[table.name])) for table in EXCHANGE_TABLES]
    return rows


def package_digest(conn: sqlite3.Connection) -> str:
    """包的内容摘要：六表逐行的规范摘要，+ 口径版本与机器/周。

    不含生成时刻、是否在采这类每次都可能变的东西——「同周无新数据重跑」靠它判。
    行序按列排序（重跑与跨机器都可复现），与 SQLite 文件的物理布局无关。
    """
    digest = hashlib.sha256()
    digest.update(f"{EXPORT_FORMAT_VERSION}\n".encode("utf-8"))
    meta = read_package_meta(conn)
    digest.update(f"{meta['machine_id']}\n{meta['week']}\n".encode("utf-8"))
    for table in EXCHANGE_TABLES:
        digest.update(f"== {table.name}\n".encode("utf-8"))
        cols = ", ".join(table.names)
        count = 0
        for row in conn.execute(f"SELECT {cols} FROM {table.name} ORDER BY {cols}"):
            count += 1
            digest.update(_row_bytes(row))
        digest.update(f"-- {count}\n".encode("utf-8"))
    return digest.hexdigest()


def _row_bytes(row: Iterable) -> bytes:
    """一行的规范字节：空值、数字、文本各给可区分的标记，列间不可能混淆。"""
    parts = []
    for value in row:
        if value is None:
            parts.append(b"\x00")
        elif isinstance(value, bytes):
            parts.append(b"\x02" + value)
        elif isinstance(value, (int, float)):
            parts.append(b"\x03" + repr(value).encode("ascii"))
        else:
            parts.append(b"\x01" + str(value).encode("utf-8"))
    return b"\x1f".join(parts) + b"\n"


@dataclasses.dataclass(frozen=True)
class ImageRef:
    """包引用的一个图片：内容寻址的 key 与它对应的内容哈希。"""

    key: str
    content_hash: str


def package_image_keys(conn: sqlite3.Connection) -> tuple[ImageRef, ...]:
    """包引用的图片，按 key 排序。

    引用 = 包内资产表的行。包里的资产表恰好收「本周版本行引用到的那些」（见取数 SQL），
    所以清单恰等于包引用的 key 集；mime 也在资产表里，key 的扩展名由它定。
    """
    return tuple(sorted(
        (ImageRef(image_key(content_hash, mime), content_hash)
         for content_hash, mime in
         conn.execute("SELECT content_hash, mime FROM product_image_assets")),
        key=lambda ref: ref.key))


@dataclasses.dataclass(frozen=True)
class ImageUploadResult:
    """图片通道这一趟做了什么。

    `missing` 是包引用了、本机没有字节的 key——如实记，不挡事（对方汇总时按缺图记缺）。
    `failure` 非空表示这趟图片没传成（包可能已经发出去了）：原因供报告引用。
    """

    uploaded: int = 0
    uploaded_bytes: int = 0
    skipped: int = 0
    missing: tuple[str, ...] = ()
    failure: str | None = None


@dataclasses.dataclass(frozen=True)
class ExportResult:
    """跑一次导出的结果（票据 10 的报告从这里长出来）。"""

    week: str
    machine_id: str
    package_rel: str                     # 包在 raw 库里的相对路径
    manifest_rel: str
    rows: dict[str, int]
    crawl_in_progress: bool
    package_size: int                    # 包（.db.gz）的字节大小
    published: bool                      # 这次跑完包在远端上（同内容重跑的空推确认也算）
    unchanged: bool                      # 与已发布那份同内容：没有新提交
    commit: str | None = None            # 包在远端的提交（短哈希）；没发成时是 None
    failure: str | None = None           # 没做成事时的原因（拉不动 / 推不动）
    images: ImageUploadResult | None = None    # 没发成事的那两条路上是 None（图片这趟没做）

    @property
    def failed(self) -> bool:
        return self.failure is not None


def _pack_gzip(data: bytes) -> bytes:
    """统一形态的 gzip：不嵌构建时刻（同内容压出来一样，git 才认「没有变化」）。"""
    return gzip.compress(data, mtime=0)


def _manifest_bytes(package_name: str, images: tuple[ImageRef, ...]) -> bytes:
    manifest = {"package": package_name, "keys": [ref.key for ref in images]}
    return _pack_gzip(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))


def _published_digest(published_gz: bytes) -> str | None:
    """已发布那份包（.db.gz 的字节）的内容摘要；不是我们的包时回 None（当「不一样」重发）。"""
    try:
        data = gzip.decompress(published_gz)
    except (OSError, EOFError):
        return None
    with tempfile.TemporaryDirectory(prefix="bestseller-export-") as tmp:
        tmp_db = pathlib.Path(tmp) / "published.db"
        tmp_db.write_bytes(data)
        conn = sqlite3.connect(tmp_db)
        try:
            return package_digest(conn)
        except (sqlite3.Error, KeyError, ValueError, TypeError):
            return None
        finally:
            conn.close()


def _pull_fresh(channel: GitChannel) -> None:
    """拉取远端。上次导出中断留下的脏工作区会让 pull --rebase 拒绝：先把克隆退回
    远端状态（连未跟踪残迹一起清）再拉一次——raw 库是纯输出通道，本地没有不可重生的东西。"""
    try:
        channel.pull()
    except ChannelError as first:
        try:
            channel.reset_to_upstream(clean=True)
        except ChannelError:
            raise first from None
        channel.pull()


def _restore_remote(channel: GitChannel) -> str | None:
    """推送失败后把克隆退回远端状态；回退不了就把原因带出去（包已在 outbox）。"""
    try:
        channel.reset_to_upstream(clean=True)
    except ChannelError as exc:
        return str(exc)
    return None


def upload_new_images(source: pathlib.Path, images: tuple[ImageRef, ...],
                      store: ImageStore) -> ImageUploadResult:
    """清单里的图片：桶里已有的跳过，缺的传字节；本机取不到字节的如实记进 missing。"""
    if not images:
        return ImageUploadResult()
    try:
        existing = store.existing_keys()
    except ImageStoreError as exc:
        return ImageUploadResult(failure=f"图片库连不上：{exc}")
    uri = f"{pathlib.Path(source).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        uploaded = uploaded_bytes = skipped = 0
        missing: list[str] = []
        for ref in images:
            if ref.key in existing:
                skipped += 1
                continue
            row = conn.execute(
                "SELECT content FROM product_image_assets WHERE content_hash=?",
                (ref.content_hash,)).fetchone()
            data = row[0] if row else None
            if not data:
                missing.append(ref.key)
                continue
            try:
                store.upload(ref.key, bytes(data))
            except ImageStoreError as exc:
                return ImageUploadResult(
                    uploaded=uploaded, uploaded_bytes=uploaded_bytes, skipped=skipped,
                    missing=tuple(missing),
                    failure=f"图片传了一半就失败（已传 {uploaded} 张）：{exc}")
            uploaded += 1
            uploaded_bytes += len(data)
        return ImageUploadResult(uploaded, uploaded_bytes, skipped, tuple(missing))
    finally:
        conn.close()


@dataclasses.dataclass(frozen=True)
class _BuiltPackage:
    """打好在 outbox 里的一份包，连同它的清单与内容摘要。"""

    base: str                            # 如 W38-m1
    package_gz: bytes                    # .db.gz 的字节
    manifest_gz: bytes
    rows: dict[str, int]
    digest: str
    images: tuple[ImageRef, ...]


def _build(source: pathlib.Path, outbox: pathlib.Path, package_rel: str, *,
           week: str, machine_id: str, crawl_in_progress: bool) -> _BuiltPackage:
    """打包 → 摘要 → 清单 → 落 outbox（`.db` 与 `.db.gz` 都留着：失败时这就是「包留在本机」）。"""
    package_name = pathlib.Path(package_rel).name
    base = package_name[: -len(PACKAGE_SUFFIX)]
    package_path = outbox / f"{base}.db"
    build_package(source, package_path, week=week, machine_id=machine_id,
                  crawl_in_progress=crawl_in_progress,
                  generated_at=dt.datetime.now(CST))
    conn = sqlite3.connect(package_path)
    try:
        rows = read_package_meta(conn)["rows"]
        digest = package_digest(conn)
        images = package_image_keys(conn)
    finally:
        conn.close()
    package_gz = _pack_gzip(package_path.read_bytes())
    (outbox / f"{base}.db.gz").write_bytes(package_gz)
    return _BuiltPackage(base=base, package_gz=package_gz,
                         manifest_gz=_manifest_bytes(package_name, images),
                         rows=rows, digest=digest, images=images)


@dataclasses.dataclass(frozen=True)
class _PublishOutcome:
    published: bool
    unchanged: bool
    commit: str | None = None
    failure: str | None = None


def _publish(channel: GitChannel, raw_dir: pathlib.Path, package_rel: str,
             manifest_rel: str, built: _BuiltPackage) -> _PublishOutcome:
    """与已发布那份比对后决定发不发；发就写文件 + 包与清单同一次提交推上去。

    比的是 **HEAD 里那份**（远端的事实），不是工作区文件——崩溃在「写完、提交前」
    留下的残迹不能冒充已发布。
    """
    published_gz = channel.read_path(package_rel)
    if published_gz is not None and _published_digest(published_gz) == built.digest:
        # 没有新内容：不写文件、不产生提交。推送仍试一次——上次推失败（比如第一次
        # 发布撞上断网）留下的那笔在这里补上；确实是空的推送是个空动作。
        try:
            channel.push()
        except ChannelError as exc:
            return _PublishOutcome(published=False, unchanged=True, failure=(
                f"推送没成功（本次没有新内容要发布）：{exc}\n等通道恢复后重跑一次即可确认。"))
        return _PublishOutcome(published=True, unchanged=True, commit=channel.head())

    published_package = raw_dir / package_rel
    published_manifest = raw_dir / manifest_rel
    try:
        published_package.parent.mkdir(parents=True, exist_ok=True)
        published_package.write_bytes(built.package_gz)
        published_manifest.write_bytes(built.manifest_gz)
        channel.commit(f"export {built.base}", [published_package, published_manifest])
        channel.push()
    except (ChannelError, OSError) as exc:
        note = _restore_remote(channel)
        tail = f"\n（克隆没能退回远端状态：{note}）" if note else ""
        return _PublishOutcome(published=False, unchanged=False, failure=(
            f"推送没成功：{exc}\n包已经在 outbox 里打好，通道修好后重跑即可发布。{tail}"))
    return _PublishOutcome(published=True, unchanged=False, commit=channel.head())


def default_store(cfg) -> ImageStore | None:
    """按配置搭图片库；没配桶时回 None（导出/导入两侧都按「图片这半没做」如实报告）。"""
    bucket = str(getattr(cfg, "cos_bucket", "") or "").strip()
    return CosCliImageStore(bucket) if bucket else None


def export(cfg, *, week: str | None = None, store: ImageStore | None = None) -> ExportResult:
    """导出本周（或指定周）的包：打包 → 推入 `raw-<机器>` → 图片只传新增。

    与采集并行安全：快照走源库的只读事务（WAL 下不锁采集），包里记 `crawl_in_progress`。
    推送失败不下架任何东西：包留在 outbox，克隆退回远端状态，结果里带回原因。
    """
    week = week or current_week()
    machine_id = str(cfg.machine_id)
    source = pathlib.Path(cfg.db_file)
    exchange_root = pathlib.Path(cfg.exchange_root)
    raw_dir = exchange_root / f"raw-{machine_id}"
    package_rel = package_rel_path(week, machine_id)
    manifest_rel = package_rel[: -len(PACKAGE_SUFFIX)] + MANIFEST_SUFFIX
    outbox = exchange_root / "outbox"

    crawl_in_progress = crawler_identity.is_running()
    built = _build(source, outbox, package_rel, week=week, machine_id=machine_id,
                   crawl_in_progress=crawl_in_progress)
    result = dict(week=week, machine_id=machine_id, package_rel=package_rel,
                  manifest_rel=manifest_rel, rows=built.rows,
                  crawl_in_progress=crawl_in_progress,
                  package_size=len(built.package_gz))
    if not (raw_dir / ".git").exists():
        return ExportResult(**result, published=False, unchanged=False, failure=(
            f"raw 库还没 clone 到 {raw_dir}：按上机清单第 9 步 clone 本机那条 raw 库"
            f"（与 plan 库），包已经在 {outbox} 里打好，clone 好重跑即可发布。"))

    channel = GitChannel(raw_dir)
    try:
        _pull_fresh(channel)
    except ChannelError as exc:
        return ExportResult(**result, published=False, unchanged=False,
                            failure=f"拉不到 raw 库：{exc}\n包已经在 {outbox} 里打好，"
                                    "通道修好后重跑即可发布。")

    outcome = _publish(channel, raw_dir, package_rel, manifest_rel, built)
    if outcome.failure is not None:
        return ExportResult(**result, published=False, unchanged=outcome.unchanged,
                            failure=outcome.failure)

    if store is None:
        store = default_store(cfg)
    images = (upload_new_images(source, built.images, store) if store is not None else
              ImageUploadResult(failure=(
                  "配置缺 machine.cos_bucket：图片通道没有桶可用，包里的图这次没传。"
                  "照 config/config.example.toml 的 [machine] 一节补上桶名后重跑。")))
    return ExportResult(**result, published=True, unchanged=outcome.unchanged,
                        commit=outcome.commit, images=images)


def export_weeks(cfg, weeks: Iterable[str], *,
                 store: ImageStore | None = None) -> list[ExportResult]:
    """按周窗口导出：补历史（W36/W37 这类）一周一个包，各自独立发布。"""
    return [export(cfg, week=week, store=store) for week in weeks]
