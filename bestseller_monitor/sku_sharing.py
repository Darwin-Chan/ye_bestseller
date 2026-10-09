"""SKU 判断集的打包与搬运（票 08）：SKU 图文判断、候选推荐与 SKU 人工决定经
`judged-<机器>` 库的 `data/sku-judgments.db.gz` 发布与收取。

**为什么是新路径不是同包加表**：旧商品级判断集（`data/judgments.db.gz`，v2）的发布是
**整文件覆盖**——旧版本程序一发布就会把同路径里它不认识的表整包抹掉（只余 git 历史），
新旧混跑的机队里 SKU 数据会被反复清空。SKU 包走新文件：旧程序对它不读不写（结构性
隔离），新程序两条通道都走、旧通道一字不改地继续与未升级机器互通。

**包内五表**：两张缓存表（`sku_judgments`、`sku_recommendations`，只含已完成结果——
未完成与失败从来不落缓存）＋三张人工决定表（`sku_manual_groups`、`sku_excluded_skus`、
`sku_excluded_pairs`，只搬人工绑定的组与排除，不含自动中间态）。人工组的行身份＝成员集合
（身份＋证据版本），组 ID 随行——跨机回放保留组 ID，推荐语境摘要才能对上。

**身份＝内容摘要**（`package_digest`）：逐表、行按列排序、含格式版本号，不含生成时刻与
打包机器（与商品判断集同规，行编码共用 `export.row_bytes`）。「同内容不重复发布」与
「重复收取幂等」都靠它。

**版本闸门**：只认识 `sku-v1`。读到来路不明的版本号时**明确拒绝**（如实报错、点名升级），
不猜、不静默跳过——新格式的消费者必须能对未知版本说不。
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import gzip
import hashlib
import json
import pathlib
import sqlite3
import tempfile
from collections.abc import Iterator

from bestseller_monitor.db import CST
from bestseller_monitor.export import pull_fresh, restore_remote, row_bytes
from bestseller_monitor.git_channel import ChannelError, GitChannel
from bestseller_monitor.merge import open_package

# SKU 包格式的口径版本：包内表、列或筛选口径变化时进一位（各自独立于商品集的 v2）。
SKU_FORMAT_VERSION = "sku-v1"
# 包在 judged-<机器> 库里的相对路径：与旧商品包同库、不同文件（原因见模块文档）。
SKU_PACKAGE_REL = "data/sku-judgments.db.gz"
# 交换区里判断库的目录名前缀：与 judgment_set.REPO_PREFIX 同为 judged-<机器>。
REPO_PREFIX = "judged-"
SKU_META_TABLE = "sku_meta"

# 表来自哪个库（`_read_rows` 按它分组读）：缓存库与草稿库各自一个只读事务。
CACHE_SOURCE = "cache"       # SKU 判断与推荐缓存（matching.sqlite）
LEDGER_SOURCE = "ledger"     # SKU 人工决定账本（分析草稿库里的三张 sku_* 表）


@dataclasses.dataclass(frozen=True)
class _PackageTable:
    """包内一张 SKU 表：列（列名, 声明）按包里的列序、主键、来源库。

    包内 DDL、写包列序、读包投影与内容摘要都从 `columns` 这一处生成。列集对应本地同名
    表（`sku_lifecycle.SKU_*_DDL` 与 `analysis_store.SKU_DECISION_TABLES`），唯一的差别
    是账本三张表不带写入后从不读回的 `saved_at`。
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    primary_key: tuple[str, ...] = ()
    source: str = CACHE_SOURCE

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)

    @property
    def ddl(self) -> str:
        parts = [f"{name} {decl}" for name, decl in self.columns]
        if self.primary_key:
            parts.append(f"PRIMARY KEY ({', '.join(self.primary_key)})")
        return f"CREATE TABLE {self.name} ({', '.join(parts)});"


SKU_PACKAGE_TABLES: tuple[_PackageTable, ...] = (
    _PackageTable(
        "sku_judgments",
        (("pair", "TEXT NOT NULL"), ("signature", "TEXT NOT NULL"),
         ("machine_id", "TEXT NOT NULL DEFAULT ''"), ("result", "TEXT NOT NULL")),
        primary_key=("pair", "signature"),
    ),
    _PackageTable(
        "sku_recommendations",
        (("context", "TEXT NOT NULL"), ("signature", "TEXT NOT NULL"),
         ("machine_id", "TEXT NOT NULL DEFAULT ''"), ("result", "TEXT NOT NULL")),
        primary_key=("context", "signature"),
    ),
    _PackageTable(
        "sku_manual_groups",
        (("members", "TEXT NOT NULL"), ("confirmed", "INTEGER NOT NULL"),
         ("excluded", "INTEGER NOT NULL"), ("group_id", "TEXT NOT NULL"),
         ("machine_id", "TEXT NOT NULL DEFAULT ''")),
        primary_key=("members",), source=LEDGER_SOURCE,
    ),
    _PackageTable(
        "sku_excluded_skus",
        (("identity", "TEXT NOT NULL"), ("machine_id", "TEXT NOT NULL DEFAULT ''")),
        primary_key=("identity",), source=LEDGER_SOURCE,
    ),
    _PackageTable(
        "sku_excluded_pairs",
        (("pair_a", "TEXT NOT NULL"), ("pair_b", "TEXT NOT NULL"),
         ("machine_id", "TEXT NOT NULL DEFAULT ''")),
        primary_key=("pair_a", "pair_b"), source=LEDGER_SOURCE,
    ),
)

_SKU_PACKAGE_SCHEMA = ("\n".join(table.ddl for table in SKU_PACKAGE_TABLES)
                       + f"\nCREATE TABLE {SKU_META_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL);\n")


def build_package(cache: pathlib.Path, store: pathlib.Path, package_path: pathlib.Path, *,
                  machine_id: str, generated_at: dt.datetime) -> None:
    """把本机 SKU 判断集（缓存两张表 + SKU 账本三张表）写成一个包文件（SQLite，`.db` 形态）。

    两个源库各自只读打开、单个事务里读完（WAL 下不锁正在跑的分析，也看不见未提交的
    半截判断），再往包库里写；源库缺文件或表还没建都算「没有行」。包里的行按显式列
    投影——源库多的列（如 `saved_at`）不读。
    """
    cache = pathlib.Path(cache)
    store = pathlib.Path(store)
    package_path = pathlib.Path(package_path)
    package_path.parent.mkdir(parents=True, exist_ok=True)
    if package_path.exists():
        package_path.unlink()
    rows = _read_rows(cache, store)
    conn = sqlite3.connect(package_path)
    try:
        conn.executescript(_SKU_PACKAGE_SCHEMA)
        for table in SKU_PACKAGE_TABLES:
            if rows[table.name]:
                conn.executemany(
                    f"INSERT INTO {table.name} VALUES ({', '.join('?' * len(table.columns))})",
                    rows[table.name])
        conn.executemany(f"INSERT INTO {SKU_META_TABLE}(key, value) VALUES (?, ?)",
                         list(_meta_rows(machine_id=machine_id, generated_at=generated_at,
                                         counts={name: len(rows[name]) for name in rows})))
        conn.commit()
    finally:
        conn.close()


def _connect_ro(path: pathlib.Path) -> sqlite3.Connection:
    """只读打开源库：打包不该写源库，也不该把它建出来。"""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _read_rows(cache: pathlib.Path, store: pathlib.Path) -> dict[str, list[tuple]]:
    """按来源库分组读完五类表：每个库一个只读事务；缺文件、表还没建都算「没有行」。

    列对不上的读错误照常抛出——那是源库形状不对，不是空（与商品集同规）。
    """
    sources = {CACHE_SOURCE: pathlib.Path(cache), LEDGER_SOURCE: pathlib.Path(store)}
    rows: dict[str, list[tuple]] = {}
    for source, path in sources.items():
        tables = [table for table in SKU_PACKAGE_TABLES if table.source == source]
        if not path.exists():
            rows.update({table.name: [] for table in tables})
            continue
        conn = _connect_ro(path)
        try:
            conn.execute("BEGIN")
            for table in tables:
                try:
                    rows[table.name] = [tuple(row) for row in conn.execute(
                        f"SELECT {', '.join(table.names)} FROM {table.name}")]
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc):
                        raise
                    rows[table.name] = []
            conn.rollback()
        finally:
            conn.close()
    return rows


def _meta_rows(*, machine_id: str, generated_at: dt.datetime,
               counts: dict[str, int]) -> list[tuple[str, str]]:
    rows = [
        ("machine_id", machine_id),
        ("generated_at", generated_at.isoformat(timespec="seconds")),
        ("format_version", SKU_FORMAT_VERSION),
    ]
    rows += [(f"rows_{table.name}", str(counts[table.name])) for table in SKU_PACKAGE_TABLES]
    return rows


def read_package_meta(conn: sqlite3.Connection) -> dict:
    """把 sku_meta 折成调用方要的形状：机器、时刻、口径版本、各表行数。"""
    raw = {key: value for key, value in
           conn.execute(f"SELECT key, value FROM {SKU_META_TABLE}").fetchall()}
    return {
        "machine_id": raw["machine_id"],
        "generated_at": raw["generated_at"],
        "format_version": raw["format_version"],
        "rows": {table.name: int(raw[f"rows_{table.name}"]) for table in SKU_PACKAGE_TABLES},
    }


def package_digest(conn: sqlite3.Connection) -> str:
    """包的内容摘要：五类表逐行的规范摘要 + 口径版本；这就是包的身份。

    不含生成时刻与打包机器（元数据表整个不进摘要）——同一份内容在别处重打、在另一台
    机器上重打都是同一个摘要（与商品集同规，行编码共用 `export.row_bytes`）。
    """
    digest = hashlib.sha256()
    digest.update(f"{SKU_FORMAT_VERSION}\n".encode("utf-8"))
    for table in SKU_PACKAGE_TABLES:
        digest.update(f"== {table.name}\n".encode("utf-8"))
        cols = ", ".join(table.names)
        count = 0
        for row in conn.execute(f"SELECT {cols} FROM {table.name} ORDER BY {cols}"):
            count += 1
            digest.update(row_bytes(row))
        digest.update(f"-- {count}\n".encode("utf-8"))
    return digest.hexdigest()


def table_counts(cache: pathlib.Path, store: pathlib.Path) -> dict[str, int]:
    """五类表各有多少行；源库缺文件、表还没建时算 0。"""
    return {name: len(rows) for name, rows in _read_rows(cache, store).items()}


def _pack_gzip(data: bytes) -> bytes:
    """统一形态的 gzip：不嵌构建时刻（与周包、商品判断集同规，同内容压出来一样）。"""
    return gzip.compress(data, mtime=0)


@contextlib.contextmanager
def _open_bytes(data: bytes) -> Iterator[sqlite3.Connection]:
    """把发布形态（`.db.gz`）的字节落在临时文件上，按包的读口打开（只读）。

    读的是 **HEAD 里那份**（远端的事实）：判「同不同内容」与导入都走这里。
    """
    with tempfile.TemporaryDirectory(prefix="bestseller-judged-sku-") as tmp:
        path = pathlib.Path(tmp) / "published.db.gz"
        path.write_bytes(data)
        with open_package(path) as conn:
            yield conn


def _probe_published(data: bytes) -> tuple[str, str | None]:
    """探已发布那份的字节：`(状态, 内容摘要)`。

    - `("ok", digest)`：是本机认识的口径版本、可读，摘要即内容身份。
    - `("unknown", None)`：读得出元数据、但版本号不是本机认识的（未来版本）——
      调用方必须**明确拒绝**，绝不覆盖（覆盖会毁掉新机器发布的数据）。
    - `("corrupt", None)`：解不动、表不齐——照常按「与本地不同内容」处理，
      用本机的真内容覆盖它正是自愈（与商品集同规）。
    """
    try:
        with _open_bytes(data) as conn:
            version = read_package_meta(conn)["format_version"]
            if version != SKU_FORMAT_VERSION:
                return "unknown", version
            return "ok", package_digest(conn)
    except (sqlite3.Error, OSError, EOFError, KeyError, ValueError, TypeError):
        return "corrupt", None


@dataclasses.dataclass(frozen=True)
class SkuPublishResult:
    """跑一次 SKU 发布做了什么（周报与演示从这里读）。

    `unchanged` 为真表示与已发布那份同内容：没写文件、没提交、没推送。
    `note` 是「本机还没判过、没东西可发」这类可读说明——不是失败。
    """

    machine_id: str
    package_rel: str                     # 交换区相对路径（judged-<机器>/data/sku-judgments.db.gz）
    rows: dict[str, int]                 # 本机五类表各多少行
    digest: str = ""
    published: bool = False              # 包在远端上（含「确认了一遍、同内容」）
    unchanged: bool = False
    commit: str | None = None            # 远端那一笔提交（短哈希）
    failure: str | None = None
    note: str | None = None

    @property
    def failed(self) -> bool:
        return self.failure is not None


def publish(exchange_root: str | pathlib.Path, machine_id: str, cache: str | pathlib.Path,
            store: str | pathlib.Path, *, now: dt.datetime | None = None) -> SkuPublishResult:
    """把本机 SKU 判断集发布到 `judged-<机器>` 库的 `data/sku-judgments.db.gz`（票 08）。

    打包 → 与库里 HEAD 那份比内容摘要：同内容不写文件、不提交、不推送；新内容覆盖再
    提交推送；HEAD 那份读不成包照常覆盖（自愈），但 HEAD 那份**是更新的格式版本**时
    明确拒绝——覆盖会毁掉新机器发布的数据。五类表**全空**时不发布（多半是文件被删）。
    推送失败把克隆退回远端状态、如实报错——本地缓存与账本一个字不动。
    """
    root = pathlib.Path(exchange_root)
    repo = root / f"{REPO_PREFIX}{machine_id}"
    rel = f"{repo.name}/{SKU_PACKAGE_REL}"
    rows = table_counts(cache, store)
    if not any(rows.values()):
        return SkuPublishResult(machine_id, rel, rows, note=(
            "本机还没有 SKU 判断集可发布（SKU 判断缓存与 SKU 人工决定账本都是空的）："
            "跑过一次带模型的 SKU 分析、或在 SKU 页面上整理过分组之后再来。"))
    if not (repo / ".git").exists():
        return SkuPublishResult(machine_id, rel, rows, failure=(
            f"{repo.name} 还没 clone 到 {repo}：按上机清单把本机这条 judged 库建好、clone 下来"
            "（自己那本可写、其余只读），clone 好重跑即可发布。"))

    channel = GitChannel(repo)
    try:
        pull_fresh(channel)
    except ChannelError as exc:
        return SkuPublishResult(machine_id, rel, rows, failure=(
            f"拉不到 {repo.name}：{exc}\n本地缓存一个字没动，通道修好后重跑即可发布。"))

    with tempfile.TemporaryDirectory(prefix="bestseller-judged-sku-") as tmp:
        package = pathlib.Path(tmp) / "sku-judgments.db"
        try:
            build_package(cache, store, package, machine_id=machine_id,
                          generated_at=now or dt.datetime.now(CST))
        except sqlite3.Error as exc:
            # 源库形状不对（列缺了、库坏了）如实报错，不发半个包出去。
            return SkuPublishResult(machine_id, rel, rows, failure=(
                f"本机 SKU 判断集读不出来（{pathlib.Path(cache).name}／{pathlib.Path(store).name}）："
                f"{exc}\n本机缓存与账本一个字没动，修好后重跑即可发布。"))
        conn = sqlite3.connect(package)
        try:
            digest = package_digest(conn)
        finally:
            conn.close()
        package_gz = _pack_gzip(package.read_bytes())

    published_gz = channel.read_path(SKU_PACKAGE_REL)
    if published_gz is not None:
        state, published_digest = _probe_published(published_gz)
        if state == "unknown":
            return SkuPublishResult(machine_id, rel, rows, failure=(
                f"远端那份 SKU 判断集是更新的格式版本（{published_digest}，本机只认识 "
                f"{SKU_FORMAT_VERSION}）：明确不覆盖——先升级本机程序再发布。"
                "本机缓存与账本一个字没动。"))
        if state == "ok" and published_digest == digest:
            return SkuPublishResult(machine_id, rel, rows, digest=digest, published=True,
                                    unchanged=True, commit=channel.head())

    target = repo / SKU_PACKAGE_REL
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(package_gz)
        channel.commit(f"judged publish sku {machine_id}", [target])
        channel.push()
    except (ChannelError, OSError) as exc:
        note = restore_remote(channel)
        tail = f"\n（克隆没能退回远端状态：{note}）" if note else ""
        return SkuPublishResult(machine_id, rel, rows, digest=digest, failure=(
            f"推送没成功：{exc}\n本机缓存一个字没动，通道修好后重跑即可发布。{tail}"))
    return SkuPublishResult(machine_id, rel, rows, digest=digest, published=True,
                            commit=channel.head())


# 导入账（本机账，就在缓存库里）：包身份 → 这次收进来时各表有多少行、新插进去多少行。
# 与商品判断集的 `judgment_imports` 同一口径：「收过」与「收进了什么」不可能说法不一。
IMPORT_LEDGER_TABLE = "sku_imports"

_IMPORT_LEDGER_DDL = f"""CREATE TABLE IF NOT EXISTS {IMPORT_LEDGER_TABLE} (
    digest TEXT PRIMARY KEY,
    source_machine TEXT NOT NULL,
    package_rel TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_added INTEGER NOT NULL DEFAULT 0,
    rows_detail TEXT NOT NULL DEFAULT '{{}}')"""

# 决定表（进账本，不进缓存）：表集只从 SKU_PACKAGE_TABLES 推，认「哪些表是决定」只此一份。
LEDGER_TABLES = tuple(table.name for table in SKU_PACKAGE_TABLES if table.source == LEDGER_SOURCE)


@dataclasses.dataclass(frozen=True)
class SkuCollectResult:
    """从一台机器的 judged 库收 SKU 判断集的结果（与商品集的 CollectResult 同规）。"""

    machine: str
    package_rel: str
    digest: str = ""
    skipped: bool = False
    rows: dict[str, int] = dataclasses.field(default_factory=dict)    # 包里各表多少行
    added: dict[str, int] = dataclasses.field(default_factory=dict)   # 这次新收进来多少行
    conflicts: tuple = ()                                            # 这次核对到的冲突（Conflict）
    imported_at: str = ""
    failure: str | None = None
    unreachable: bool = False
    note: str | None = None

    @property
    def failed(self) -> bool:
        return self.failure is not None

    @property
    def adopted(self) -> dict[str, int]:
        """这次并进本机账本的决定行数（按表）——`added` 里决定表那部分。"""
        return {name: self.added.get(name, 0) for name in LEDGER_TABLES}

    @property
    def adopted_total(self) -> int:
        """这次并进本机账本的决定行数合计（周报的「采纳了几条」）。"""
        return sum(self.adopted.values())


@dataclasses.dataclass(frozen=True)
class SkuCollectOutcome:
    """跑一次 SKU 收取做了什么：逐个来源的结果 + 「没有可收的」这类整趟说明。"""

    results: tuple[SkuCollectResult, ...]
    notes: tuple[str, ...] = ()
    failure: str | None = None

    @property
    def failed(self) -> bool:
        return self.failure is not None or any(result.failed for result in self.results)

    @property
    def conflicts(self) -> tuple:
        return tuple(conflict for result in self.results for conflict in result.conflicts)


def collect(exchange_root: str | pathlib.Path, machine_id: str, cache: str | pathlib.Path,
            store: str | pathlib.Path, *, now: dt.datetime | None = None) -> SkuCollectOutcome:
    """把交换区里别的机器的 SKU 判断集收进本机（票 08）。

    名单与商品集同一份（交换区里实际存在的 `judged-*` 库）。对每本 pull 一次，读它 HEAD
    里的 SKU 包，按内容摘要幂等导入：**先并人工决定**（进 SKU 账本，票 08 判法），**后导
    判断与推荐**（逐行 `INSERT OR IGNORE`，本机已有的行不动）+ 导入账，各自一个事务、失败
    全回滚。版本号不是本机认识的**明确拒绝**、一行都不导；坏包、拉不动、没发布过都只记
    那一家，别家照收。
    """
    root = pathlib.Path(exchange_root)
    cache = pathlib.Path(cache)
    store = pathlib.Path(store)
    sources = judged_repos(root, exclude=machine_id)
    if not sources:
        return SkuCollectOutcome((), ((
            f"交换区里没有别的机器的判断库（{REPO_PREFIX}*）可收：别的机器建库、"
            f"clone 到 {root}、发布 SKU 判断集之后重跑即可。"),))
    try:
        conn = _connect_cache(cache, machine_id)
    except sqlite3.Error as exc:
        return SkuCollectOutcome((), failure=(
            f"判断缓存打不开（{cache}）：{exc}\n"
            "分析程序正在跑的时候收不了 SKU 判断集（它占着缓存的写口），等它跑完再重跑收取。"))
    try:
        return SkuCollectOutcome(tuple(
            _collect_one(conn, path, machine=machine, machine_id=machine_id, store=store, now=now)
            for machine, path in sources))
    finally:
        conn.close()


def judged_repos(exchange_root: str | pathlib.Path,
                 *, exclude: str = "") -> tuple[tuple[str, pathlib.Path], ...]:
    """交换区里实际存在的 `judged-<机器>` 库——名单口径与商品集完全相同（spec §9）。"""
    from bestseller_monitor.judgment_set import judged_repos as _judged_repos
    return _judged_repos(exchange_root, exclude=exclude)


def _connect_cache(cache: pathlib.Path, machine_id: str) -> sqlite3.Connection:
    """读写打开缓存：两张 SKU 表按当前形状备好（`sku_lifecycle.ensure_tables`），再加导入账。"""
    from bestseller_monitor.sku_lifecycle import ensure_tables
    conn = sqlite3.connect(cache)
    conn.row_factory = sqlite3.Row
    ensure_tables(conn, machine_id)
    conn.execute(_IMPORT_LEDGER_DDL)
    return conn


def _incoming_decisions(sliced: dict) -> dict:
    """包内三张决定表折成与 `DraftStore.sku_ledger()` 同形的账本字典（来源机器随行）。

    包缺少的列按空值算（来源机器缺了就是空串——并入时按通道来源记），结构列缺了当场报错，
    这本按「读不出来」处理（与商品集同规）。
    """
    def rows_of(name):
        return [dict(zip(sliced[name][0], row)) for row in sliced[name][1]]

    return {
        'groups': [{'members': json.loads(row['members']), 'confirmed': bool(row['confirmed']),
                    'excluded': bool(row['excluded']), 'group_id': row['group_id'],
                    'machine_id': row.get('machine_id') or ''}
                   for row in rows_of('sku_manual_groups')],
        'excluded_skus': [[row['identity'], row.get('machine_id') or '']
                          for row in rows_of('sku_excluded_skus')],
        'excluded_pairs': [[row['pair_a'], row['pair_b'], row.get('machine_id') or '']
                           for row in rows_of('sku_excluded_pairs')],
    }


def _read_package_rows(package: sqlite3.Connection) -> dict[str, tuple[tuple[str, ...], list[tuple]]]:
    """按显式投影列读包内五类表：包缺少的列不读，表缺了就当场报错（与商品集同规）。"""
    sliced: dict[str, tuple[tuple[str, ...], list[tuple]]] = {}
    for table in SKU_PACKAGE_TABLES:
        present = {row[1] for row in package.execute(f'PRAGMA table_info("{table.name}")')}
        if not present:
            raise ValueError(f"包里没有 {table.name} 表")
        names = tuple(name for name in table.names if name in present)
        sliced[table.name] = (names, [tuple(row) for row in
                                      package.execute(f'SELECT {", ".join(names)} '
                                                      f'FROM "{table.name}"')])
    return sliced


def _collect_one(conn: sqlite3.Connection, repo: pathlib.Path, *,
                 machine: str, machine_id: str, store: pathlib.Path,
                 now: dt.datetime | None) -> SkuCollectResult:
    rel = f"{repo.name}/{SKU_PACKAGE_REL}"
    if not (repo / ".git").exists():
        return SkuCollectResult(machine, rel, note=(
            f"{repo.name} 还不是 git 克隆：按上机清单把这条 judged 库 clone 到 {repo}；"
            "这本的 SKU 判断集这次没收。"))
    channel = GitChannel(repo)
    try:
        channel.pull()
    except ChannelError as exc:
        return SkuCollectResult(machine, rel, unreachable=True,
                                failure=f"拉不到 {repo.name}，这本这次没收：{exc}")
    published = channel.read_path(SKU_PACKAGE_REL)
    if published is None:
        return SkuCollectResult(machine, rel, note=(
            f"{repo.name} 还没发布过 SKU 判断集（HEAD 里没有 {SKU_PACKAGE_REL}）："
            "那台机器发布之后重跑即可收到。"))

    try:
        with _open_bytes(published) as package:
            version = read_package_meta(package)["format_version"]
            if version != SKU_FORMAT_VERSION:
                return SkuCollectResult(machine, rel, failure=(
                    f"{repo.name} 的 SKU 判断集是 {version} 格式（本机只认识 "
                    f"{SKU_FORMAT_VERSION}）：明确拒绝，这本一行都没导入——请先升级本机程序。"))
            digest = package_digest(package)
            sliced = _read_package_rows(package)
        decisions = _incoming_decisions(sliced)          # 形状不对的包：算这本读不出来
    except (sqlite3.Error, OSError, KeyError, ValueError, TypeError) as exc:
        return SkuCollectResult(machine, rel, failure=(
            f"SKU 判断集读不出来（{rel} 不是 SKU 判断集的包，这本这次没收）：{exc}"))

    rows = {name: len(rows_) for name, (_, rows_) in sliced.items()}
    known = conn.execute(f"SELECT * FROM {IMPORT_LEDGER_TABLE} WHERE digest=?",
                         (digest,)).fetchone()
    if known is not None:
        # 同内容已收过：什么都不写，计数从账里回放（幂等）。
        detail = json.loads(known["rows_detail"])
        return SkuCollectResult(machine, rel, digest=digest, skipped=True,
                                rows={name: counts[0] for name, counts in detail.items()},
                                added={name: counts[1] for name, counts in detail.items()},
                                imported_at=known["imported_at"])

    imported_at = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    # 先并人工决定（本机账本），后导判断与记导入账：并决定逐行幂等、冲突账按内容去重，
    # 所以后一步失败时重跑等价于首次（与商品集同规）。
    try:
        from bestseller_monitor.analysis_store import DraftStore
        merge = DraftStore(store, machine_id).merge_incoming_sku(
            decisions, source=machine, seen_at=imported_at)
    except sqlite3.Error as exc:
        return SkuCollectResult(machine, rel, digest=digest, failure=(
            f"SKU 人工决定没能并进本机账本（{store}）：{exc}\n"
            "判断也还没导入（这本这次整个没收），修好后重跑等价于首次。"))
    added: dict[str, int] = {}
    try:
        with conn:
            for table in SKU_PACKAGE_TABLES:
                if table.source != CACHE_SOURCE:     # 决定表进的是账本，不是缓存
                    continue
                names, rows_ = sliced[table.name]
                cursor = conn.executemany(
                    f"INSERT OR IGNORE INTO {table.name} ({', '.join(names)}) "
                    f"VALUES ({', '.join('?' * len(names))})", rows_)
                added[table.name] = cursor.rowcount
            added.update(merge.adopted)
            conn.execute(
                f"INSERT INTO {IMPORT_LEDGER_TABLE}(digest, source_machine, package_rel, imported_at, "
                "rows_total, rows_added, rows_detail) VALUES (?,?,?,?,?,?,?)",
                (digest, machine, rel, imported_at, sum(rows.values()), sum(added.values()),
                 json.dumps({name: [rows[name], added[name]] for name in rows},
                            ensure_ascii=False)))
    except sqlite3.Error as exc:
        conn.rollback()
        return SkuCollectResult(machine, rel, digest=digest, failure=(
            f"整包没有导入（已全部回滚）：{exc}"))
    return SkuCollectResult(machine, rel, digest=digest, rows=rows, added=added,
                            conflicts=merge.conflicts, imported_at=imported_at)
