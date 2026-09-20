"""汇总导入：把别人的包按确定性语义合进本机库（spec §9、ADR-0031）。

**一次导入**（`import_package`，数据交换台与测试都走这一个口）：

1. **包身份** = 包文件（发布形态是 `.db.gz`）内容的 SHA-256。幂等账（`import_packages`）
   里见过这个哈希就不做任何事；包被拉图、被合并都只发生一次。
2. **先拉图、再插行**：包里引用的图片 key 在本机还缺字节的，先从图片库取回、落进本机
   资产表（图片是独立通道，这一步在导入事务之外、独立提交——合并事务回滚不回滚图片）。
   取不到、没配桶、内容对不上哈希的都如实记缺，不挡导入（spec §9「仍缺的照插」）。
3. **整包一个事务**：六表合并 + 三本账（幂等账 / 冲突账 / 取胜方账）一起生效，
   失败全回滚，重跑等价于首次导入。

**观测表**（`inventory` / `product_information_versions`）——取胜单位是「一个 (日期, 店铺)
的一次采集」：

- 判据 = `(该店该日最后一次成功采集时刻, machine_id)` 的全序（取最大者）。时刻不单独存，
  由该 (店铺, 日期) 的版本行推导——包侧与本机侧跑**同一段推导代码**（`_claim_from`），
  所以本机 10:00 采的不会被别人 09:00 的包覆盖；平局按 machine_id 字典序。包级
  `generated_at`（导出时刻）不作判据。
- 同一键的行由取胜方整行替换（不逐列拼、不非空覆盖）；键不相同的行一律保留（并集）——
  多规格 SKU 集合不同时新 SKU 追加、本次未出现的旧 SKU 保留，是这条的自然结果。
- 相反粒度（单规格 / 多规格）在同一 (店铺, 商品, 日期) 上以取胜方那一种**整组取胜**：
  行级并集会把两种粒度留在同一天，分析侧遇到同日混合形态直接报错（与 `_clear_other_granularity`
  同一条规则）。
- **显式投影列**（spec §9）：包的列集与本地不必相同，读进来时缺列补 None、多列不读。

**身份表**（`shops` / `products` / `skus`）——交换律合成：`first_seen_at` 取 MIN、
`last_seen_at` 取 MAX；描述列按**最近一次观测**取胜——由该行的 `(last_seen_at, machine_id)`
定序，平局按 machine_id 字典序。导入口自己算，不沿用 `_upsert_product` 的"非空覆盖"。

**取胜方账**（`merge_claims` / `merge_seen`，本机账）：判据里「平局按 machine_id」要求合并
知道库里那一组行 / 那一条描述列的最后写者是谁——增量合并记不住它，同批包换个导入顺序
就会得出不同的库。这两张表就记这件事：观测表按 (店铺, 日期) 记取胜 claim 的 (时刻, 机器)，
身份表按 (表, 主键) 记描述列的 (last_seen_at, 机器)。本机采集写下的行不在账里，按本机
machine_id 参与定序（那些行确实是本机写的）。

**冲突**：同一 (日期, 店铺) 上两个不同 machine_id 的 claim（越权、降级逃生口、换周交接
不清）记进 `import_conflicts`（本机账，不入交换区）。判据确定，所以「谁赢」在各台一致；
「我见过这个冲突吗」取决于该机导入过哪些包——报告因此是本机视角的。

**已知约束**（spec §9 记录在案）：导入会重排 `product_information_versions.id`，分析侧拿
`id` 当「信息版本」对外标识，该标识不跨导入稳定——分析侧的事，本模块只记录。
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import gzip
import hashlib
import logging
import pathlib
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator

from bestseller_monitor import export
from bestseller_monitor.db import VERSION_DEDUPE_KEY, utcnow
from bestseller_monitor.image_store import ImageStore, ImageStoreError, image_key
from bestseller_monitor.parse import DEFAULT_SKU_ID

log = logging.getLogger(__name__)

# 包内六表列集的唯一来源是 export 的包格式定义（DRY：不在这里再抄一份列名）。
_BY_NAME = {table.name: table for table in export.EXCHANGE_TABLES}

# 观测表：表名 → 分组用的日期列。
_OBSERVATION_DATE = {
    "inventory": "date",
    "product_information_versions": "observed_date",
}

# 版本表同键的判别式与去重键同源（db.VERSION_DEDUPE_KEY 的表达式逐列绑定；那个注释
# 里点名「导入侧也用它」）。键相同时由取胜方替换的是全部非键列——observed_date 虽是
# observed_at 的派生值，也照取胜方整行落，口径不靠"本地旧值"兜底。
_VERSION_MATCH = " AND ".join(f"{column}=?" for column in VERSION_DEDUPE_KEY)
_VERSION_PAYLOAD = ("observed_date", "product_name", "image_url")


@dataclasses.dataclass(frozen=True)
class _Identity:
    """身份表（交换律合成的三张表）：表名与「按最近一次观测取胜」的描述列。

    主键与全部列名都取自 `export.EXCHANGE_TABLES` 的同一份定义，不再抄一遍。
    """

    table: str
    description: tuple[str, ...]


_IDENTITY_TABLES: tuple[_Identity, ...] = (
    _Identity("shops", ("shop_name", "shop_url")),
    _Identity("products", ("product_url", "product_name", "main_image_url")),
    _Identity("skus", ("sku_name",)),
)


@dataclasses.dataclass(frozen=True)
class _Claim:
    """一个 (店铺, 日期) 的一次采集主张：最后一次成功采集时刻 + 是哪台机器采的。"""

    at: str
    machine_id: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.at, self.machine_id)


@dataclasses.dataclass
class _Group:
    """一个 (店铺, 日期) 的裁决与败方行数统计（两表合计）。"""

    winner: str                       # "package" / "local"
    package_claim: _Claim | None
    local_claim: _Claim | None
    local_before: int = 0             # 合并前本机在这个组里的行数（两表合计）
    package_replaced: int = 0         # 包侧被覆盖的行数（同键替换 + 相反粒度整组丢掉）
    package_kept: int = 0             # 包侧保留的行数（键不相同、照插的部分）
    local_replaced: int = 0           # 本机侧被覆盖的行数（同键被替换 + 相反粒度被清掉）


@dataclasses.dataclass
class _Counts:
    inserted: int = 0
    replaced: int = 0


@dataclasses.dataclass(frozen=True)
class _ImagePull:
    """图片这一趟做了什么（在导入事务之外）。"""

    pulled: int = 0
    pulled_bytes: int = 0
    skipped: int = 0
    missing: tuple[str, ...] = ()
    note: str | None = None


@dataclasses.dataclass(frozen=True)
class ImportResult:
    """一次导入做了什么（数据交换台的报告从这里长出来）。

    `skipped` 为真表示同哈希已导过：这次什么都没做，计数回放幂等账里那份。
    `failure` 非空表示这次整包没导成（事务已全部回滚），重跑等价于首次导入。
    """

    package: str
    sha256: str
    machine_id: str = ""              # 包侧机器（从包元数据读）
    week: str = ""
    skipped: bool = False
    rows_total: int = 0
    rows_inserted: int = 0
    rows_replaced: int = 0
    conflicts: int = 0
    images_pulled: int = 0
    images_bytes: int = 0             # 这次拉回来的图片字节（报告里「68（6.8 MB）」那半）
    images_skipped: int = 0
    images_missing: int = 0
    images_note: str | None = None
    failure: str | None = None

    @property
    def failed(self) -> bool:
        return self.failure is not None


def package_sha256(package_path: pathlib.Path) -> str:
    """包身份：包文件内容的 SHA-256（发布形态是 `.db.gz`；同内容重打包字节一致）。"""
    return hashlib.sha256(pathlib.Path(package_path).read_bytes()).hexdigest()


def package_shop_days(package_path: pathlib.Path, start: dt.date,
                      end: dt.date) -> frozenset[tuple[str, str]]:
    """包覆盖的（店铺 × 日期）集合，只看窗口内的库存行。

    只读地开包（`.db` 与发布形态 `.db.gz` 都行）；包读不动时照常抛错，由调用方
    按「这个包怎么处理」决定（跳过记一笔，或按导入失败报）。
    """
    with open_package(package_path) as pkg:
        return frozenset(
            (str(shop_key), str(day)) for shop_key, day in pkg.execute(
                "SELECT DISTINCT shop_key, date FROM inventory "
                "WHERE date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())))


@contextlib.contextmanager
def open_package(package_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """打开包文件（`.db` 或发布形态 `.db.gz`）当**只读**的 SQLite 库。

    只读连接（`mode=ro`）不碰源文件：`.db` 形态的包留在原地时也不会被写入或恢复。
    """
    package_path = pathlib.Path(package_path)
    with tempfile.TemporaryDirectory(prefix="bestseller-merge-") as tmp:
        db_path = package_path
        if package_path.suffix == ".gz":
            try:
                raw = gzip.decompress(package_path.read_bytes())
            except (OSError, EOFError) as exc:
                raise sqlite3.DatabaseError(f"包解压失败：{exc}") from exc
            db_path = pathlib.Path(tmp) / package_path.stem
            db_path.write_bytes(raw)
        uri = f"{pathlib.Path(db_path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def import_package(conn: sqlite3.Connection, package_path: pathlib.Path, *,
                   machine_id: str, store: ImageStore | None = None) -> ImportResult:
    """把一个包合进本机库；返回这次做了什么。

    `conn` 是本机连接（调用方负责机位与收尾；导入会先提交它上面挂着的改动，再开自己的
    事务）。`machine_id` 是本机编号——推导本机侧 claim 与身份表平局时代表本机。
    包侧机器从包元数据读，不靠调用方声明。
    """
    package_path = pathlib.Path(package_path)
    try:
        sha = package_sha256(package_path)
    except OSError as exc:
        return ImportResult(package_path.name, "", failure=f"读不到包文件：{exc}")

    known = conn.execute(
        "SELECT * FROM import_packages WHERE package_sha256=?", (sha,)).fetchone()
    if known is not None:
        return _result_from_ledger(known)

    try:
        with open_package(package_path) as pkg:
            meta = export.read_package_meta(pkg)
            rows = _read_package_rows(pkg)
    except (sqlite3.Error, OSError, KeyError, ValueError) as exc:
        return ImportResult(package_path.name, sha, failure=f"包读不出来：{exc}")

    images = _ImagePull()
    try:
        images = _pull_images(conn, _package_assets(rows), store)
        conn.commit()                            # 图片那半步先独立落库（先拉图、再插行）
        conn.execute("PRAGMA foreign_keys=OFF")  # 事务之外才有效：缺图的行照插（spec §9）
        try:
            with conn:
                return _merge_all(conn, rows, meta=meta, package_name=package_path.name,
                                  sha=sha, local_machine=machine_id, images=images)
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    except (sqlite3.Error, OSError, KeyError, ValueError) as exc:
        conn.rollback()
        return ImportResult(
            package_path.name, sha, machine_id=str(meta["machine_id"]), week=str(meta["week"]),
            images_pulled=images.pulled, images_bytes=images.pulled_bytes,
            images_skipped=images.skipped,
            images_missing=len(images.missing), images_note=images.note,
            failure=f"整包没有导入（已全部回滚）：{exc}")


def _result_from_ledger(row: sqlite3.Row) -> ImportResult:
    """同哈希跳过的回执：计数回放账里首次导入那份。"""
    return ImportResult(
        package=row["package_name"], sha256=row["package_sha256"],
        machine_id=row["machine_id"], week=row["week"] or "", skipped=True,
        rows_total=row["rows_total"], rows_inserted=row["rows_inserted"],
        rows_replaced=row["rows_replaced"], conflicts=row["conflicts"],
        images_pulled=row["images_pulled"], images_skipped=row["images_skipped"],
        images_missing=row["images_missing"])


# ---------- 读包（显式投影） ----------

def _read_package_rows(pkg: sqlite3.Connection) -> dict[str, list[dict]]:
    """按显式投影列读包内六表（spec §9：禁止 `SELECT *`）。

    包缺少的列不读、下游按 None 落库（旧包少一列也能导）；多出来的列（`inventory.diff`
    退役后的旧包）不读。包里没有交换集的某张表就当场报错——那不是交换集里的包。
    """
    rows_by_table: dict[str, list[dict]] = {}
    for table in export.EXCHANGE_TABLES:
        present = {row[1] for row in pkg.execute(f'PRAGMA table_info("{table.name}")')}
        if not present:
            raise ValueError(f"包里没有 {table.name} 表")
        cols = [name for name in table.names if name in present]
        rows_by_table[table.name] = [
            {name: row[name] for name in cols}
            for row in pkg.execute(f'SELECT {", ".join(cols)} FROM "{table.name}"')
        ]
    return rows_by_table


def _package_assets(rows: dict[str, list[dict]]) -> list[tuple[str, str]]:
    """包引用的图片：(内容哈希, mime)，哈希与 mime 都在的那些。"""
    return [(str(row["content_hash"]), str(row["mime"]))
            for row in rows.get("product_image_assets", [])
            if row.get("content_hash") and row.get("mime")]


# ---------- 图片：先拉图、再插行 ----------

def _pull_images(conn: sqlite3.Connection, assets: list[tuple[str, str]],
                 store: ImageStore | None) -> _ImagePull:
    """包里引用的图片：本机已有哈希的跳过；缺的从图片库取回、落本机资产表。

    这一步在导入事务之外、独立提交：图片是独立通道，本来就可能后到（spec §9）。
    取不到的按缺口如实记，不挡导入；内容对不上内容寻址哈希的不落库（按缺记）。
    """
    todo: list[tuple[str, str]] = []
    skipped = 0
    for content_hash, mime in assets:
        if conn.execute("SELECT 1 FROM product_image_assets WHERE content_hash=?",
                        (content_hash,)).fetchone() is not None:
            skipped += 1
        else:
            todo.append((content_hash, mime))
    if not todo:
        return _ImagePull(skipped=skipped)
    if store is None:
        return _ImagePull(skipped=skipped, missing=tuple(h for h, _ in todo), note=(
            "配置缺 machine.cos_bucket：图片通道没有桶可用，包里引用的图这次没拉。"
            "照 config/config.example.toml 的 [machine] 一节补上桶名后重跑。"))

    pulled = 0
    pulled_bytes = 0
    missing: list[str] = []
    note = None
    bad_bytes = 0
    for content_hash, mime in todo:
        if note is not None:                     # 图片库拉不动了：剩下的按缺口记
            missing.append(content_hash)
            continue
        try:
            data = store.fetch(image_key(content_hash, mime))
        except ImageStoreError as exc:
            note = f"图片没取完（已取 {pulled} 张）：{exc}"
            missing.append(content_hash)
            continue
        if hashlib.sha256(data).hexdigest() != content_hash:
            bad_bytes += 1                       # 取回的内容对不上内容寻址的哈希
            missing.append(content_hash)
            continue
        conn.execute("INSERT OR IGNORE INTO product_image_assets VALUES (?, ?, ?)",
                     (content_hash, mime, data))
        pulled += 1
        pulled_bytes += len(data)
    if bad_bytes:
        note = (note + "；" if note else "") + f"有 {bad_bytes} 张取回的内容对不上哈希，按缺记"
    conn.commit()                                # 图片独立落库：合并事务回滚不回滚它们
    return _ImagePull(pulled=pulled, pulled_bytes=pulled_bytes, skipped=skipped,
                      missing=tuple(missing), note=note)


# ---------- 合并 ----------

def _merge_all(conn: sqlite3.Connection, rows: dict[str, list[dict]], *, meta: dict,
               package_name: str, sha: str, local_machine: str,
               images: _ImagePull) -> ImportResult:
    """整包合并：裁决 → 逐表落库 → 三本账。调用方保证在事务里、外键已关。"""
    imported_at = utcnow()
    pkg_machine = str(meta["machine_id"])
    groups = _decide_claims(conn, rows, pkg_machine, local_machine)
    counts = _Counts()
    _merge_inventory(conn, rows["inventory"], groups, counts)
    _merge_versions(conn, rows["product_information_versions"], groups, counts)
    for spec in _IDENTITY_TABLES:
        _merge_identity(conn, spec, rows[spec.table], pkg_machine, local_machine, counts)
    conflicts = _record_conflicts(conn, groups, sha, imported_at)
    for key, group in groups.items():
        _write_claim(conn, key, group)

    rows_total = sum(len(table_rows) for table_rows in rows.values())
    conn.execute(
        "INSERT INTO import_packages(package_sha256, package_name, machine_id, week, "
        "generated_at, format_version, imported_at, rows_total, rows_inserted, rows_replaced, "
        "conflicts, images_pulled, images_skipped, images_missing) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sha, package_name, pkg_machine, str(meta["week"]), str(meta["generated_at"]),
         str(meta["format_version"]), imported_at, rows_total, counts.inserted,
         counts.replaced, conflicts, images.pulled, images.skipped, len(images.missing)))
    log.info("导入包 %s（%s，%s）：新增 %d 行、覆盖 %d 行、冲突 %d 处、拉图 %d 张（缺 %d）",
             package_name, pkg_machine, meta["week"], counts.inserted, counts.replaced,
             conflicts, images.pulled, len(images.missing))
    return ImportResult(
        package=package_name, sha256=sha, machine_id=pkg_machine, week=str(meta["week"]),
        rows_total=rows_total, rows_inserted=counts.inserted, rows_replaced=counts.replaced,
        conflicts=conflicts, images_pulled=images.pulled, images_bytes=images.pulled_bytes,
        images_skipped=images.skipped,
        images_missing=len(images.missing), images_note=images.note)


def _claim_from(times: Iterable[str | None], machine_id: str) -> _Claim | None:
    """由一组版本行的 observed_at 推导 claim；一行都没有时没有 claim。

    包侧（machine_id 取包元数据）与本机侧（取本机编号或取胜方账里的机器）跑的是这一段。
    """
    usable = [str(t) for t in times if t]
    if not usable:
        return None
    return _Claim(max(usable), machine_id)


def _decide_claims(conn: sqlite3.Connection, rows: dict[str, list[dict]], pkg_machine: str,
                   local_machine: str) -> dict[tuple, _Group]:
    """给包里碰到的每个 (店铺, 日期) 定胜负，并备好败方统计的基数。"""
    package_times: dict[tuple, list[str | None]] = {}
    for row in rows["product_information_versions"]:
        key = (row.get("shop_key"), row.get("observed_date"))
        package_times.setdefault(key, []).append(row.get("observed_at"))

    keys: set[tuple] = set()
    for name, date_col in _OBSERVATION_DATE.items():
        for row in rows[name]:
            keys.add((row.get("shop_key"), row.get(date_col)))

    groups: dict[tuple, _Group] = {}
    for key in keys:
        package_claim = _claim_from(package_times.get(key, []), pkg_machine)
        local_claim = _local_claim(conn, key, local_machine)
        # 没有 claim 的一侧按 (空时刻, 自己机器) 参与定序：全序仍然无歧义，且真实
        # claim（旧包/历史包根本没有版本行）永远赢过空时刻。
        package_key = package_claim.key if package_claim else ("", pkg_machine)
        local_key = local_claim.key if local_claim else ("", local_machine)
        groups[key] = _Group(
            winner="package" if package_key > local_key else "local",
            package_claim=package_claim, local_claim=local_claim)
    _count_local_rows(conn, groups)
    return groups


def _local_claim(conn: sqlite3.Connection, key: tuple, local_machine: str) -> _Claim | None:
    """本机侧 claim：跑与包侧同一段推导代码（`_claim_from`）；机器取自取胜方账。

    账里记的是上一个取胜方的 (时刻, 机器)。时刻对得上（就是这批行的最大 observed_at）
    才认账里的机器；对不上说明行是本机采集新写进去的，按本机。
    """
    shop_key, date = key
    times = [row["observed_at"] for row in conn.execute(
        "SELECT observed_at FROM product_information_versions "
        "WHERE shop_key=? AND observed_date=?", (shop_key, date))]
    tag = local_machine
    if times:
        ledger = conn.execute(
            "SELECT claim_at, machine_id FROM merge_claims WHERE shop_key=? AND observed_date=?",
            (shop_key, date)).fetchone()
        if ledger is not None and ledger["claim_at"] == max(times):
            tag = str(ledger["machine_id"])
    return _claim_from(times, tag)


def _count_local_rows(conn: sqlite3.Connection, groups: dict[tuple, _Group]) -> None:
    """合并前本机在两个观测表里各组的行数——败方「保留的行数」从它减出来。"""
    if not groups:
        return
    placeholders = ", ".join(["(?, ?)"] * len(groups))
    params = [value for key in groups for value in key]
    for name, date_col in _OBSERVATION_DATE.items():
        for row in conn.execute(
                f"SELECT shop_key, {date_col} AS day, COUNT(*) AS n FROM {name} "
                f"WHERE (shop_key, {date_col}) IN ({placeholders}) "
                f"GROUP BY shop_key, {date_col}", params):
            group = groups.get((row["shop_key"], row["day"]))
            if group is not None:
                group.local_before += row["n"]


def _write_claim(conn: sqlite3.Connection, key: tuple, group: _Group) -> None:
    """把这一组的取胜 claim 记进取胜方账；两侧都没有真实 claim 时无事可记。"""
    claim = group.package_claim if group.winner == "package" else group.local_claim
    if claim is None:
        return
    conn.execute(
        "INSERT INTO merge_claims(shop_key, observed_date, claim_at, machine_id) "
        "VALUES (?,?,?,?) ON CONFLICT(shop_key, observed_date) DO UPDATE SET "
        "claim_at=excluded.claim_at, machine_id=excluded.machine_id",
        (key[0], key[1], claim.at, claim.machine_id))


def _merge_inventory(conn: sqlite3.Connection, rows: list[dict],
                     groups: dict[tuple, _Group], counts: _Counts) -> None:
    cleared: set[tuple] = set()     # 每个 (店铺, 商品, 日期) 的相反粒度只清一次
    for row in rows:
        shop_key, offer_id = row.get("shop_key"), row.get("offer_id")
        date, sku_id = row.get("date"), row.get("sku_id")
        group = groups[(shop_key, date)]
        exists = conn.execute(
            "SELECT 1 FROM inventory WHERE shop_key=? AND offer_id=? AND sku_id=? AND date=?",
            (shop_key, offer_id, sku_id, date)).fetchone() is not None
        if group.winner == "package":
            offer_day = (shop_key, offer_id, date)
            if offer_day not in cleared:
                cleared.add(offer_day)
                group.local_replaced += _clear_opposite_granularity(
                    conn, shop_key, offer_id, date, sku_id)
            _upsert_inventory_row(conn, row)
            if exists:
                counts.replaced += 1
                group.local_replaced += 1
            else:
                counts.inserted += 1
        elif _opposite_form_locally(conn, shop_key, offer_id, date, sku_id):
            group.package_replaced += 1          # 相反粒度：败方这种形态整组丢掉
        elif exists:
            group.package_replaced += 1          # 键相同：取胜方是本机，包侧这一行不要了
        else:
            _upsert_inventory_row(conn, row)
            counts.inserted += 1
            group.package_kept += 1


def _other_form_predicate(sku_id) -> str:
    """相反粒度的 sku_id 判别式：本行是商品级（默认行）时找 SKU 级行，反之找默认行。"""
    return "<>" if str(sku_id) == DEFAULT_SKU_ID else "="


def _opposite_form_locally(conn: sqlite3.Connection, shop_key, offer_id, date, sku_id) -> bool:
    """本机在同一个 (店铺, 商品, 日期) 上已有相反粒度的行吗。"""
    return conn.execute(
        f"SELECT 1 FROM inventory WHERE shop_key=? AND offer_id=? AND date=? "
        f"AND sku_id{_other_form_predicate(sku_id)}? LIMIT 1",
        (shop_key, offer_id, date, DEFAULT_SKU_ID)).fetchone() is not None


def _clear_opposite_granularity(conn: sqlite3.Connection, shop_key, offer_id, date,
                                sku_id) -> int:
    """取胜方这一行是什么粒度，就清掉本机在同一 (店铺, 商品, 日期) 上的另一种粒度。"""
    return conn.execute(
        f"DELETE FROM inventory WHERE shop_key=? AND offer_id=? AND date=? "
        f"AND sku_id{_other_form_predicate(sku_id)}?",
        (shop_key, offer_id, date, DEFAULT_SKU_ID)).rowcount


def _upsert_inventory_row(conn: sqlite3.Connection, row: dict) -> None:
    """库存行整行落库：同键只可能在取胜方是包侧时走到这里，全部值列按包侧替换。"""
    table = _BY_NAME["inventory"]
    sets = ", ".join(f"{name}=excluded.{name}" for name in table.names
                     if name not in table.primary_key)
    conn.execute(
        f"INSERT INTO inventory({', '.join(table.names)}) "
        f"VALUES ({', '.join('?' * len(table.names))}) "
        f"ON CONFLICT({', '.join(table.primary_key)}) DO UPDATE SET {sets}",
        tuple(row.get(name) for name in table.names))


def _merge_versions(conn: sqlite3.Connection, rows: list[dict],
                    groups: dict[tuple, _Group], counts: _Counts) -> None:
    table = _BY_NAME["product_information_versions"]
    for row in rows:
        group = groups[(row.get("shop_key"), row.get("observed_date"))]
        # 同键的查找与替换都按 VERSION_DEDUPE_KEY 的表达式口径（两个可空列 COALESCE），
        # 与去重索引一致，且不依赖索引本身在不在（老库上它可能没建起来——那种库上
        # INSERT OR IGNORE 挡不住重复，这里的先查后写才挡得住）。
        key_params = (row.get("shop_key"), row.get("offer_id"), row.get("observed_at"),
                      row.get("content_hash") or "", row.get("image_error") or "")
        exists = conn.execute(
            f"SELECT 1 FROM product_information_versions WHERE {_VERSION_MATCH}", key_params
        ).fetchone() is not None
        if group.winner == "package" and exists:
            sets = ", ".join(f"{name}=?" for name in _VERSION_PAYLOAD)
            conn.execute(
                f"UPDATE product_information_versions SET {sets} WHERE {_VERSION_MATCH}",
                tuple(row.get(name) for name in _VERSION_PAYLOAD) + key_params)
            counts.replaced += 1
            group.local_replaced += 1
        elif group.winner != "package" and exists:
            group.package_replaced += 1
        else:
            conn.execute(
                f"INSERT INTO product_information_versions({', '.join(table.names)}) "
                f"VALUES ({', '.join('?' * len(table.names))})",
                tuple(row.get(name) for name in table.names))
            counts.inserted += 1
            if group.winner != "package":
                group.package_kept += 1


def _merge_identity(conn: sqlite3.Connection, spec: _Identity, rows: list[dict],
                    pkg_machine: str, local_machine: str, counts: _Counts) -> None:
    """身份表交换律合成：时间列 MIN/MAX，描述列按最近一次观测取胜。"""
    table = _BY_NAME[spec.table]
    names, pk = table.names, table.primary_key
    pk_where = " AND ".join(f"{name}=?" for name in pk)
    for row in rows:
        pk_values = tuple(row.get(name) for name in pk)
        row_key = "\x1f".join(str(value) for value in pk_values)
        existing = conn.execute(
            f"SELECT * FROM {spec.table} WHERE {pk_where}", pk_values).fetchone()
        package_seen = (row.get("last_seen_at") or "", pkg_machine)
        if existing is None:
            conn.execute(
                f"INSERT INTO {spec.table}({', '.join(names)}) "
                f"VALUES ({', '.join('?' * len(names))})",
                tuple(row.get(name) for name in names))
            _write_seen(conn, spec.table, row_key, package_seen)
            counts.inserted += 1
            continue

        local_seen = (existing["last_seen_at"] or "", _seen_machine(
            conn, spec.table, row_key, existing["last_seen_at"] or "", local_machine))
        # 不逐列拼：描述列整份取最近一次观测那一侧的值（取胜方不是"后到者"，是判据）；
        # 包缺的列按 None 落——与观测表各路径同一条投影口径（旧包少一列也能合）。
        merged = {
            "first_seen_at": _min_ts(existing["first_seen_at"], row.get("first_seen_at")),
            "last_seen_at": _max_ts(existing["last_seen_at"], row.get("last_seen_at")),
            **{name: (row.get(name) if package_seen > local_seen else existing[name])
               for name in spec.description},
        }
        if any(existing[name] != merged[name] for name in merged):
            conn.execute(
                f"UPDATE {spec.table} SET {', '.join(f'{name}=?' for name in merged)} "
                f"WHERE {pk_where}", (*merged.values(), *pk_values))
            counts.replaced += 1
        _write_seen(conn, spec.table, row_key,
                    package_seen if package_seen > local_seen else local_seen)


def _write_seen(conn: sqlite3.Connection, table_name: str, row_key: str,
                seen: tuple[str, str]) -> None:
    conn.execute(
        "INSERT INTO merge_seen(table_name, row_key, seen_at, machine_id) VALUES (?,?,?,?) "
        "ON CONFLICT(table_name, row_key) DO UPDATE SET "
        "seen_at=excluded.seen_at, machine_id=excluded.machine_id",
        (table_name, row_key, seen[0], seen[1]))


def _seen_machine(conn: sqlite3.Connection, table_name: str, row_key: str,
                  local_last_seen: str, local_machine: str) -> str:
    """本机这条身份行的描述列是谁写的：账里时刻对得上就用账里的机器，否则按本机。"""
    ledger = conn.execute(
        "SELECT seen_at, machine_id FROM merge_seen WHERE table_name=? AND row_key=?",
        (table_name, row_key)).fetchone()
    if ledger is not None and ledger["seen_at"] == local_last_seen:
        return str(ledger["machine_id"])
    return local_machine


def _min_ts(*values: str | None) -> str | None:
    usable = [str(value) for value in values if value]
    return min(usable) if usable else None


def _max_ts(*values: str | None) -> str | None:
    usable = [str(value) for value in values if value]
    return max(usable) if usable else None


def _record_conflicts(conn: sqlite3.Connection, groups: dict[tuple, _Group],
                      sha: str, imported_at: str) -> int:
    """冲突 = 同一 (日期, 店铺) 上两个不同机器标识的 claim；追加进本机冲突账。"""
    count = 0
    for (shop_key, date), group in sorted(
            groups.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
        if group.package_claim is None or group.local_claim is None:
            continue
        if group.package_claim.machine_id == group.local_claim.machine_id:
            continue
        if group.winner == "package":
            winner, loser = group.package_claim, group.local_claim
            replaced = group.local_replaced
            kept = max(group.local_before - group.local_replaced, 0)
        else:
            winner, loser = group.local_claim, group.package_claim
            replaced, kept = group.package_replaced, group.package_kept
        conn.execute(
            "INSERT INTO import_conflicts(imported_at, package_sha256, observed_date, "
            "shop_key, winner_side, winner_machine, winner_at, loser_machine, loser_at, "
            "loser_rows_replaced, loser_rows_kept) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (imported_at, sha, date, shop_key,
             "package" if group.winner == "package" else "local",
             winner.machine_id, winner.at, loser.machine_id, loser.at, replaced, kept))
        log.info("冲突：%s %s 上 %s（%s）赢过 %s（%s），败方被覆盖 %d 行、保留 %d 行",
                 shop_key, date, winner.machine_id, winner.at,
                 loser.machine_id, loser.at, replaced, kept)
        count += 1
    return count
