"""判断集的打包与搬运（票 03、票 04）：把本机判断集打成包，经 `judged-<机器>` 库发布与收取。

**包是什么**（spec §2／§3）：

- 一个小 SQLite（`.db` 形态；发布时压成 `.db.gz`），落在 `judged-<机器>` 库的
  `data/judgments.db.gz`——一本一包、**覆盖同名路径**，旧版留在 git 历史里（`git show
  HEAD~1:data/judgments.db.gz` 取得回来，不在工作树里堆批次文件）。
- 包内 = 判断集：判断缓存的三类表（模型判断 `judgments`、视觉描述 `visual_evidence`、
  判断证据索引 `evidence`）＋人工决定账本的三张表（`manual_relations`、
  `manual_standalone`、`manual_exclusions`），外加一张键值元数据表 `judgment_meta`。
  判断缓存与账本是两个库（缓存 `matching.sqlite`、账本在分析草稿库里），打包时各读各的。
  `evidence.image_data` 是只写不读的图片字节，**不进包**（图片证据仍走对象存储那条
  通道）；账本的 `saved_at` 是「这一版账本什么时候保存的」、写入后从不读回，也不进包
  （与 `image_data` 同理）。`recommendations` 只写不读，也不搬。
- **账本只搬决定**：已确认的关系、单独成组、排除对进包；未确认的中间态（撤回过的组）
  不外发——撤回是本机的中间态，搬过去只会制造伪冲突（票 04、spec §6）。
- **身份 = 内容摘要**（`package_digest`）：逐表、行按列排序、含格式版本号，不含生成时刻
  与打包机器——同一份内容在谁那里重打都是同一个摘要（口径与周包同规，行的规范编码共用
  `export.row_bytes`）。「同内容不重复发布」「重复收取幂等」都靠它：身份是内容本身，
  所以一台机器收到两台内容相同的判断集时，第二份如实算「已经收过」。

**发布**（`publish`）：打包 → 与 `judged-<机器>` 库 HEAD 里那份比内容摘要——同内容不写
文件、不提交、不推送（空操作）；有新内容则覆盖 `data/judgments.db.gz` 再提交推送。本机
判断集**六类表全空**时不发布：全空这件事多半是文件被删了，拿空包盖掉已发布那份是净损失
（旧版虽在 git 历史里，没必要制造这一步）。推送失败把克隆退回远端状态、如实报错；本地
缓存与账本一个字不动。

**收取**（`collect`）：对交换区里别的机器的 `judged-<机器>` 库各 pull 一次，读它 HEAD 里
那份包，按内容摘要幂等导入：**先并人工决定**（不冲突即生效、冲突写进本机冲突账，票 04），
**后导判断**（三类表逐行 `INSERT OR IGNORE`）+ **导入账**（`judgment_imports`，就在缓存
库里——账与行同一个文件，「收过」与「收进了什么」不可能说法不一）。导入账只在决定与判断
都成功之后记下：账里有的包，一定连决定都并过了；中途失败重跑等价于首次（并决定逐行幂等、
冲突账按内容去重）。判断集这条通道因此只碰自己的两个库与自己的 git 库，与采集包那条
互不影响。

**顺序**：两条通道互不设先后——判断集先到就先收下、闲置待用（引用的商品/版本本机还没有
也不报错，键是内容摘要，本机算出来的对得上就命中），数据到齐后自然生效。谁还没建库、
还没发布过判断集、库没 clone 下来，都只给可读提示，不挡别家的收取。
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

from bestseller_monitor import matching
from bestseller_monitor.analysis_store import Conflict, DraftStore
from bestseller_monitor.db import CST
from bestseller_monitor.export import pull_fresh, restore_remote, row_bytes
from bestseller_monitor.git_channel import ChannelError, GitChannel
from bestseller_monitor.merge import open_package

# 判断集包格式的口径版本：包内表、列或筛选口径变化时进一位（与周包的 EXPORT_FORMAT_VERSION 同规）。
# v2（票 04）：包内多了人工决定账本的三张表。
FORMAT_VERSION = "v2"
# 包在 judged-<机器> 库里的相对路径：一本一包、覆盖同名路径（spec §3）。
PACKAGE_REL = "data/judgments.db.gz"
# 交换区里判断库的目录名前缀：judged-<机器>，与 raw-<机器>、plan 并列。
REPO_PREFIX = "judged-"
META_TABLE = "judgment_meta"

# 表来自哪个库（`_read_rows` 按它分组读）：缓存与账本是两个文件，各自一个只读事务。
# （与下面的 LEDGER_TABLE 区分：那是导入账的表名，这里是表的来源库。）
CACHE_SOURCE = "cache"       # 判断缓存（matching.sqlite）
LEDGER_SOURCE = "ledger"     # 人工决定账本（分析草稿库里的三张 manual_* 表）


@dataclasses.dataclass(frozen=True)
class _PackageTable:
    """包内一张判断集表：列（列名, 声明）按包里的列序、主键、来源库、行筛选。

    包内 DDL、写包的列序、读包的投影与内容摘要都从 `columns` 这一处生成——三份各写一遍
    就会悄悄错列（周包那边的教训）。列集对应本地同名表的 DDL，唯一的差别是 `evidence`
    不带只写不读的 `image_data`、账本三张表不带写入后从不读回的 `saved_at`（spec §2）。
    `where` 是「账本只搬决定」的落点：撤回过的关系（未确认）不进包。
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    primary_key: tuple[str, ...] = ()
    source: str = CACHE_SOURCE
    where: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)

    @property
    def ddl(self) -> str:
        parts = [f"{name} {decl}" for name, decl in self.columns]
        if self.primary_key:
            parts.append(f"PRIMARY KEY ({', '.join(self.primary_key)})")
        return f"CREATE TABLE {self.name} ({', '.join(parts)});"


PACKAGE_TABLES: tuple[_PackageTable, ...] = (
    _PackageTable(
        "judgments",
        (("pair", "TEXT NOT NULL"), ("signature", "TEXT NOT NULL"),
         ("machine_id", "TEXT NOT NULL DEFAULT ''"), ("evidence_a", "TEXT"),
         ("evidence_b", "TEXT"), ("result", "TEXT")),
        primary_key=("pair", "signature"),
    ),
    _PackageTable(
        "visual_evidence",
        (("image_hash", "TEXT"), ("description", "TEXT"), ("model", "TEXT")),
        primary_key=("image_hash",),
    ),
    _PackageTable(
        "evidence",
        (("identity", "TEXT"), ("version", "TEXT"), ("name", "TEXT"),
         ("image_hash", "TEXT"), ("origin", "TEXT")),
        primary_key=("identity", "version"),
    ),
    _PackageTable(
        "manual_relations",
        (("members", "TEXT PRIMARY KEY"), ("confirmed", "INTEGER NOT NULL"),
         ("machine_id", "TEXT NOT NULL DEFAULT ''")),
        source=LEDGER_SOURCE, where="confirmed=1",
    ),
    _PackageTable(
        "manual_standalone",
        (("identity", "TEXT PRIMARY KEY"), ("version", "TEXT NOT NULL"),
         ("machine_id", "TEXT NOT NULL DEFAULT ''")),
        source=LEDGER_SOURCE,
    ),
    _PackageTable(
        "manual_exclusions",
        (("pair_a", "TEXT NOT NULL"), ("pair_b", "TEXT NOT NULL"),
         ("machine_id", "TEXT NOT NULL DEFAULT ''")),
        primary_key=("pair_a", "pair_b"), source=LEDGER_SOURCE,
    ),
)

# 账本三张表的表名：表集只从 PACKAGE_TABLES 推，认「哪些表是决定」也只此一份。
LEDGER_TABLES = tuple(table.name for table in PACKAGE_TABLES if table.source == LEDGER_SOURCE)

_PACKAGE_SCHEMA = ("\n".join(table.ddl for table in PACKAGE_TABLES)
                   + f"\nCREATE TABLE {META_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL);\n")


def _connect_ro(path: pathlib.Path) -> sqlite3.Connection:
    """只读打开源库：打包不该写源库，也不该把它建出来。"""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def table_counts(cache: pathlib.Path, store: pathlib.Path) -> dict[str, int]:
    """判断集六类表各有多少行；库还没有（一次分析都没跑过 / 还没保存过草稿）时算 0。"""
    return {name: len(rows) for name, rows in _read_rows(cache, store).items()}


def build_package(cache: pathlib.Path, package_path: pathlib.Path, *, machine_id: str,
                  generated_at: dt.datetime, store: pathlib.Path) -> None:
    """把本机判断集（缓存三类表 + 账本三张表）写成一个包文件（SQLite，`.db` 形态）。

    两个源库各自只读打开、单个事务里读完（WAL 下不锁正在跑的分析，也看不见未提交的
    半截判断），再往包库里写。包里的行按显式列投影——源库多的列不读、少的列不补。
    账本路径必给：判断集的定义就含人工决定账本，替调用方省掉它等于悄悄发出半个判断集。
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
        conn.executescript(_PACKAGE_SCHEMA)
        for table in PACKAGE_TABLES:
            if rows[table.name]:
                conn.executemany(
                    f"INSERT INTO {table.name} VALUES ({', '.join('?' * len(table.columns))})",
                    rows[table.name])
        conn.executemany(f"INSERT INTO {META_TABLE}(key, value) VALUES (?, ?)",
                         list(_meta_rows(machine_id=machine_id, generated_at=generated_at,
                                         counts={name: len(rows[name]) for name in rows})))
        conn.commit()
    finally:
        conn.close()


def _read_rows(cache: pathlib.Path, store: pathlib.Path) -> dict[str, list[tuple]]:
    """按来源库分组读完六类表：每个库一个只读事务（一致快照，看不见半截批次）。

    库不在、表还没建（一次分析都没跑过 / 还没保存过草稿 / 文件被谁空建了）都算「没有行」；
    列对不上的读错误照常抛出——那是源库形状不对，不是空。
    """
    sources = {CACHE_SOURCE: pathlib.Path(cache), LEDGER_SOURCE: pathlib.Path(store)}
    rows: dict[str, list[tuple]] = {}
    for source, path in sources.items():
        tables = [table for table in PACKAGE_TABLES if table.source == source]
        if not path.exists():
            rows.update({table.name: [] for table in tables})
            continue
        conn = _connect_ro(path)
        try:
            conn.execute("BEGIN")
            for table in tables:
                where = f" WHERE {table.where}" if table.where else ""
                try:
                    rows[table.name] = [tuple(row) for row in conn.execute(
                        f"SELECT {', '.join(table.names)} FROM {table.name}{where}")]
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
        ("format_version", FORMAT_VERSION),
    ]
    rows += [(f"rows_{table.name}", str(counts[table.name])) for table in PACKAGE_TABLES]
    return rows


def read_package_meta(conn: sqlite3.Connection) -> dict:
    """把 judgment_meta 折成调用方要的形状：机器、时刻、口径版本、各表行数。"""
    raw = {key: value for key, value in
           conn.execute(f"SELECT key, value FROM {META_TABLE}").fetchall()}
    return {
        "machine_id": raw["machine_id"],
        "generated_at": raw["generated_at"],
        "format_version": raw["format_version"],
        "rows": {table.name: int(raw[f"rows_{table.name}"]) for table in PACKAGE_TABLES},
    }


def package_digest(conn: sqlite3.Connection) -> str:
    """包的内容摘要：六类表逐行的规范摘要 + 口径版本；这就是包的身份。

    不含生成时刻与打包机器（元数据表整个不进摘要）——身份是内容本身：同一份内容在
    别处重打、在另一台机器上重打都是同一个摘要，行序按列排序因此也与 SQLite 的
    物理布局无关。摘要口径与周包同规（`export.row_bytes` 是共用的行编码）。
    """
    digest = hashlib.sha256()
    digest.update(f"{FORMAT_VERSION}\n".encode("utf-8"))
    for table in PACKAGE_TABLES:
        digest.update(f"== {table.name}\n".encode("utf-8"))
        cols = ", ".join(table.names)
        count = 0
        for row in conn.execute(f"SELECT {cols} FROM {table.name} ORDER BY {cols}"):
            count += 1
            digest.update(row_bytes(row))
        digest.update(f"-- {count}\n".encode("utf-8"))
    return digest.hexdigest()


def _pack_gzip(data: bytes) -> bytes:
    """统一形态的 gzip：不嵌构建时刻（与周包同规，同内容压出来一样）。"""
    return gzip.compress(data, mtime=0)


@contextlib.contextmanager
def _open_bytes(data: bytes) -> Iterator[sqlite3.Connection]:
    """把发布形态（`.db.gz`）的字节落在临时文件上，按包的读口打开（只读）。

    读的是 **HEAD 里那份**（远端的事实）：判「同不同内容」与导入都走这里，工作区里
    可能躺着的残迹不算数。
    """
    with tempfile.TemporaryDirectory(prefix="bestseller-judged-") as tmp:
        path = pathlib.Path(tmp) / "published.db.gz"
        path.write_bytes(data)
        with open_package(path) as conn:
            yield conn


def _published_digest(data: bytes) -> str | None:
    """已发布那份（`.db.gz` 的字节）的内容摘要；解不动、表不齐时回 None。

    回 None 的后果是「与本地不同内容」→ 照常覆盖发布：远端那份既然读不成判断集，
    用本机的真内容盖掉它正是自愈，而不是停下报错（与周包那半同一口径）。
    """
    try:
        with _open_bytes(data) as conn:
            return package_digest(conn)
    except (sqlite3.Error, OSError, EOFError, KeyError, ValueError, TypeError):
        return None


@dataclasses.dataclass(frozen=True)
class PublishResult:
    """跑一次发布做了什么（周报与演示从这里读）。

    `unchanged` 为真表示与已发布那份同内容：没写文件、没提交、没推送。
    `note` 是「本机还没判过、没东西可发」这类可读说明——不是失败。
    """

    machine_id: str
    package_rel: str                     # 交换区相对路径（judged-<机器>/data/judgments.db.gz）
    rows: dict[str, int]                 # 本机判断集六类表各多少行
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
            store: str | pathlib.Path, *, now: dt.datetime | None = None) -> PublishResult:
    """把本机判断集发布到 `judged-<机器>` 库（spec §3）。

    打包 → 与库里 HEAD 那份比内容摘要：同内容不写文件、不提交、**不推送**（票面口径；
    与周包那半不同——那边空操作还会补一次推送，因为它的本地提交可能还挂在克隆里，而这里
    推送失败就把克隆退回远端了，没有待补的东西）。有新内容覆盖 `data/judgments.db.gz`
    再提交推送；HEAD 那份读不成判断集也照常覆盖（自愈）。本机判断集六类表**全空**时不
    发布：全空这件事多半是文件被删了，拿空包盖掉已发布那份是净损失（旧版虽在 git 历史里，
    没必要制造这一步）；有任何内容就照发。推送失败把克隆退回远端状态、如实报错——本地
    缓存与账本一个字不动。`now` 是包上记的生成时刻（北京时间），缺省取现在。
    """
    root = pathlib.Path(exchange_root)
    repo = root / f"{REPO_PREFIX}{machine_id}"
    rel = f"{repo.name}/{PACKAGE_REL}"
    rows = table_counts(cache, store)
    if not any(rows.values()):
        return PublishResult(machine_id, rel, rows, note=(
            f"本机还没有判断集可发布（判断缓存与人工决定账本都是空的）："
            "跑过一次带模型匹配的分析、或在分析页面上确认过同款之后再来。"))
    if not (repo / ".git").exists():
        return PublishResult(machine_id, rel, rows, failure=(
            f"{repo.name} 还没 clone 到 {repo}：按上机清单把本机这条 judged 库建好、clone 下来"
            "（自己那本可写、其余只读），clone 好重跑即可发布。"))

    channel = GitChannel(repo)
    try:
        pull_fresh(channel)
    except ChannelError as exc:
        return PublishResult(machine_id, rel, rows, failure=(
            f"拉不到 {repo.name}：{exc}\n本地缓存一个字没动，通道修好后重跑即可发布。"))

    with tempfile.TemporaryDirectory(prefix="bestseller-judged-") as tmp:
        package = pathlib.Path(tmp) / "judgments.db"
        try:
            build_package(cache, package, machine_id=machine_id,
                          generated_at=now or dt.datetime.now(CST), store=store)
        except sqlite3.Error as exc:
            # 源库形状不对（列缺了、库坏了）如实报错，不发半个包出去。
            return PublishResult(machine_id, rel, rows, failure=(
                f"本机判断集读不出来（{pathlib.Path(cache).name}／{pathlib.Path(store).name}）："
                f"{exc}\n本机缓存与账本一个字没动，修好后重跑即可发布。"))
        conn = sqlite3.connect(package)
        try:
            digest = package_digest(conn)
        finally:
            conn.close()
        package_gz = _pack_gzip(package.read_bytes())

    published_gz = channel.read_path(PACKAGE_REL)
    if published_gz is not None and _published_digest(published_gz) == digest:
        return PublishResult(machine_id, rel, rows, digest=digest, published=True,
                             unchanged=True, commit=channel.head())

    target = repo / PACKAGE_REL
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(package_gz)
        channel.commit(f"judged publish {machine_id}", [target])
        channel.push()
    except (ChannelError, OSError) as exc:
        note = restore_remote(channel)
        tail = f"\n（克隆没能退回远端状态：{note}）" if note else ""
        return PublishResult(machine_id, rel, rows, digest=digest, failure=(
            f"推送没成功：{exc}\n本机缓存一个字没动，通道修好后重跑即可发布。{tail}"))
    return PublishResult(machine_id, rel, rows, digest=digest, published=True,
                         commit=channel.head())


# 导入账（本机账，就在缓存库里）：包身份 → 这次收进来时各表有多少行、新插进去多少行。
# 账与行同一个文件，「收过」与「收进了什么」不可能说法不一（缓存被删时账随它一起去，
# 重收一遍就是了）；重复收取按内容摘要命中这里，计数回放，什么都不写。
LEDGER_TABLE = "judgment_imports"

_LEDGER_DDL = f"""CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    digest TEXT PRIMARY KEY,
    source_machine TEXT NOT NULL,
    package_rel TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_added INTEGER NOT NULL DEFAULT 0,
    rows_detail TEXT NOT NULL DEFAULT '{{}}')"""


@dataclasses.dataclass(frozen=True)
class CollectResult:
    """从一台机器的 judged 库收判断集的结果。

    `skipped` 为真表示同内容已收过：这次什么都没做，行数与新增数从账里回放（冲突不复现，
    冲突账是累计的、读它就好）。`conflicts` 是这次核对到的冲突（`analysis_store.Conflict`，
    `recorded` 说有没有新记一笔）。`note` 是「还没建库/没发布过」这类可读说明——不是失败，
    别家照收。
    """

    machine: str
    package_rel: str
    digest: str = ""
    skipped: bool = False
    rows: dict[str, int] = dataclasses.field(default_factory=dict)    # 包里各表多少行
    added: dict[str, int] = dataclasses.field(default_factory=dict)   # 这次新收进来多少行
    conflicts: tuple[Conflict, ...] = ()                              # 这次核对到的冲突
    imported_at: str = ""
    failure: str | None = None
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
class CollectOutcome:
    """跑一次收取做了什么：逐个来源的结果 + 「没有可收的」这类整趟说明。"""

    results: tuple[CollectResult, ...]
    notes: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return any(result.failed for result in self.results)


def judged_repos(exchange_root: str | pathlib.Path,
                 *, exclude: str = "") -> tuple[tuple[str, pathlib.Path], ...]:
    """交换区里实际存在的 `judged-<机器>` 库——判断集的名单口径（spec §9）。

    名单与采集名册（`machines.json`）解耦：判断的生产者集合 ≠ 采集机集合，所以名单就是
    「交换区里实际存在的库」——建了库、clone 下来，就进了名单。`exclude` 排掉本机自己那本
    （本机缓存就是那份，收自己没有意义）。
    """
    root = pathlib.Path(exchange_root)
    found = []
    for path in sorted(root.glob(f"{REPO_PREFIX}*")):
        machine = path.name[len(REPO_PREFIX):]
        if path.is_dir() and machine and machine != exclude:
            found.append((machine, path))
    return tuple(sorted(found))


def collect(exchange_root: str | pathlib.Path, machine_id: str, cache: str | pathlib.Path,
            store: str | pathlib.Path, *, now: dt.datetime | None = None) -> CollectOutcome:
    """把交换区里别的机器的判断集收进本机（spec §3、§7）：决定进账本，判断进缓存。

    对名单里每本 `judged-<机器>` pull 一次，读它 HEAD 里那份包，按内容摘要幂等导入：
    **先并人工决定**（不冲突即生效、冲突写进本机冲突账，票 04），**后导判断**（三类表
    逐行 `INSERT OR IGNORE`，本机已有的行不动）+ 导入账，各自一个事务、失败全回滚。
    一家拉不动、没发布过、包坏了都只记那一家，别家照收。`now` 是账上的收到时刻（UTC），
    缺省取现在。
    """
    root = pathlib.Path(exchange_root)
    cache = pathlib.Path(cache)
    store = pathlib.Path(store)
    sources = judged_repos(root, exclude=machine_id)
    if not sources:
        return CollectOutcome((), ((
            f"交换区里没有别的机器的判断库（{REPO_PREFIX}*）可收：别的机器建库、"
            f"clone 到 {root}、发布判断集之后重跑即可。"),))
    try:
        conn = _connect_cache(cache, machine_id)
    except sqlite3.Error as exc:
        return CollectOutcome((), ((
            f"判断缓存打不开（{cache}）：{exc}\n"
            "分析程序正在跑的时候收不了判断集（它占着缓存的写口），等它跑完再重跑收取。"),))
    try:
        return CollectOutcome(tuple(
            _collect_one(conn, path, machine=machine, machine_id=machine_id, store=store, now=now)
            for machine, path in sources))
    finally:
        conn.close()


def _connect_cache(cache: pathlib.Path, machine_id: str) -> sqlite3.Connection:
    """读写打开缓存：三类表按当前形状备好（`matching.prepare_cache`），再加导入账。"""
    conn = sqlite3.connect(cache)
    conn.row_factory = sqlite3.Row
    matching.prepare_cache(conn, machine_id)
    conn.execute(_LEDGER_DDL)
    return conn


def _incoming_decisions(sliced: dict) -> dict:
    """包内三张决定表折成与 `DraftStore.ledger()` 同形的账本字典（来源机器随行）。

    按 `_read_package_rows` 的口径认列：包缺少的列按空值算（来源机器缺了就是空串——
    并入时按通道来源记），结构列缺了当场报错，这本按「读不出来」处理。
    """
    def rows_of(name):
        return [dict(zip(sliced[name][0], row)) for row in sliced[name][1]]

    return {
        'relations': [{'members': json.loads(row['members']),
                       'confirmed': bool(row['confirmed']),
                       'machine_id': row.get('machine_id') or ''}
                      for row in rows_of('manual_relations')],
        'standalone': [[row['identity'], row['version'], row.get('machine_id') or '']
                       for row in rows_of('manual_standalone')],
        'excluded': [[row['pair_a'], row['pair_b'], row.get('machine_id') or '']
                     for row in rows_of('manual_exclusions')],
    }


def _collect_one(conn: sqlite3.Connection, repo: pathlib.Path, *,
                 machine: str, machine_id: str, store: pathlib.Path,
                 now: dt.datetime | None) -> CollectResult:
    rel = f"{repo.name}/{PACKAGE_REL}"
    if not (repo / ".git").exists():
        return CollectResult(machine, rel, note=(
            f"{repo.name} 还不是 git 克隆：按上机清单把这条 judged 库 clone 到 {repo}；"
            "这本的判断集这次没收。"))
    channel = GitChannel(repo)
    try:
        channel.pull()
    except ChannelError as exc:
        return CollectResult(machine, rel, failure=f"拉不到 {repo.name}，这本这次没收：{exc}")
    published = channel.read_path(PACKAGE_REL)
    if published is None:
        return CollectResult(machine, rel, note=(
            f"{repo.name} 还没发布过判断集（HEAD 里没有 {PACKAGE_REL}）：那台机器发布之后"
            "重跑即可收到。"))

    try:
        with _open_bytes(published) as package:
            digest = package_digest(package)
            sliced = _read_package_rows(package)
        decisions = _incoming_decisions(sliced)          # 形状不对的包：算这本读不出来
    except (sqlite3.Error, OSError, KeyError, ValueError, TypeError) as exc:
        return CollectResult(machine, rel, failure=(
            f"判断集读不出来（{rel} 不是判断集的包，这本这次没收）：{exc}"))

    rows = {name: len(rows_) for name, (_, rows_) in sliced.items()}
    # 账本一枚一枚现查（不预先取快照）：同一趟里两家内容相同时，第二家要看见第一家
    # 刚写下的那行、如实算「已经收过」，而不是撞主键报一个假的导入失败。
    known = conn.execute(f"SELECT * FROM {LEDGER_TABLE} WHERE digest=?",
                          (digest,)).fetchone()
    if known is not None:
        # 同内容已收过：什么都不写，计数从账里回放（幂等）。
        detail = json.loads(known["rows_detail"])
        return CollectResult(machine, rel, digest=digest, skipped=True,
                             rows={name: counts[0] for name, counts in detail.items()},
                             added={name: counts[1] for name, counts in detail.items()},
                             imported_at=known["imported_at"])

    imported_at = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    # 先并人工决定（本机账本），后导判断与记导入账：并决定逐行幂等、冲突账按内容去重，
    # 所以后一步失败时重跑等价于首次；而账里记下的包，一定连决定都并过了——不会被
    # 「已经收过」挡住一份没并进去的账本。
    try:
        merge = DraftStore(store, machine_id).merge_incoming(
            decisions, source=machine, seen_at=imported_at)
    except sqlite3.Error as exc:
        return CollectResult(machine, rel, digest=digest, failure=(
            f"人工决定没能并进本机账本（{store}）：{exc}\n"
            "判断也还没导入（这本这次整个没收），修好后重跑等价于首次。"))
    added: dict[str, int] = {}
    try:
        with conn:
            for table in PACKAGE_TABLES:
                if table.source != CACHE_SOURCE:     # 决定表进的是账本，不是缓存
                    continue
                names, rows_ = sliced[table.name]
                cursor = conn.executemany(
                    f"INSERT OR IGNORE INTO {table.name} ({', '.join(names)}) "
                    f"VALUES ({', '.join('?' * len(names))})", rows_)
                added[table.name] = cursor.rowcount
            added.update(merge.adopted)
            conn.execute(
                f"INSERT INTO {LEDGER_TABLE}(digest, source_machine, package_rel, imported_at, "
                "rows_total, rows_added, rows_detail) VALUES (?,?,?,?,?,?,?)",
                (digest, machine, rel, imported_at, sum(rows.values()), sum(added.values()),
                 json.dumps({name: [rows[name], added[name]] for name in rows},
                            ensure_ascii=False)))
    except sqlite3.Error as exc:
        conn.rollback()
        return CollectResult(machine, rel, digest=digest, failure=(
            f"整包没有导入（已全部回滚）：{exc}"))
    return CollectResult(machine, rel, digest=digest, rows=rows, added=added,
                         conflicts=merge.conflicts, imported_at=imported_at)


def _read_package_rows(package: sqlite3.Connection) -> dict[str, tuple[tuple[str, ...], list[tuple]]]:
    """按显式投影列读包内六类表：包缺少的列不读，表缺了就当场报错。"""
    sliced: dict[str, tuple[tuple[str, ...], list[tuple]]] = {}
    for table in PACKAGE_TABLES:
        present = {row[1] for row in package.execute(f'PRAGMA table_info("{table.name}")')}
        if not present:
            raise ValueError(f"包里没有 {table.name} 表")
        names = tuple(name for name in table.names if name in present)
        sliced[table.name] = (names, [tuple(row) for row in
                                      package.execute(f'SELECT {", ".join(names)} '
                                                      f'FROM "{table.name}"')])
    return sliced
