"""分析草稿的持久化：一次保存就是一个完整版本，读回不依赖旧进程的内存。

草稿库是独立文件：直接写采集库会越过「分析只读库存」的边界，
也让采集端的迁移与分析的保存周期互相牵制。

人工决策账本（确认的同款关系、独立确认、排除关系）与草稿同一事务落盘：
「已成功保存的人工判断」是它们唯一的持久来源，新日期区间据此复用（票 09）。

账本三张表各带 machine_id（来源机器，票 02）：本机写的记本机编号、收进来的记来源编号，
只作显示与冲突说明、不参与键；写入时行上带了就照它写，没带就记本机。

外来决定经判断集收取并进来（票 04、`merge_incoming`）：不冲突即生效、冲突写进冲突账
`decision_conflicts`（两侧来源机器与内容、记下的时刻），只增不自动消解——人在页面上
裁决后本机账本按现有编辑动作变化、下一次发布带走，冲突行留着作历史。
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

from .matching import digest as _digest
from .sku_grouping import identity as _sku_identity

SCHEMA_VERSION = 1
SKU_SCHEMA_VERSION = 3

# 冲突账（票 04）：冲突是「没能并进来的那些决定」——与本机账本同一个文件，
# 同生同死说得清；只增不自动消解，人在页面上裁决的是账本，不是它。
CONFLICT_TABLE = 'decision_conflicts'
# 冲突的三类（spec §6）：一边并组一边排除、同一成员分进不同组、确认对撤回相对。
CONFLICT_GROUP_VS_EXCLUSION = 'group_vs_exclusion'
CONFLICT_DIFFERENT_GROUPS = 'different_groups'
CONFLICT_CONFIRM_VS_WITHDRAW = 'confirm_vs_withdraw'

_CONFLICT_DDL = f"""CREATE TABLE IF NOT EXISTS {CONFLICT_TABLE} (
    digest TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    members TEXT NOT NULL,
    incoming_machine TEXT NOT NULL,
    incoming TEXT NOT NULL,
    local_machines TEXT NOT NULL,
    local TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '')"""

# 冲突里「一侧说了什么」的三种形态（`_relation_content` 们铸、页面与周报照它渲染）。
SIDE_RELATION = 'relation'
SIDE_STANDALONE = 'standalone'
SIDE_EXCLUSION = 'exclusion'

# 账本三张表：关系按成员清单整行存（带确认状态），独立确认与排除按主键逐行存。
# 保存时整版覆盖：一次保存就是一个完整的人工决策版本。
_DECISION_TABLES = {
    'manual_relations': """CREATE TABLE IF NOT EXISTS manual_relations (
        members TEXT PRIMARY KEY, confirmed INTEGER NOT NULL, saved_at TEXT NOT NULL,
        machine_id TEXT NOT NULL DEFAULT '')""",
    'manual_standalone': """CREATE TABLE IF NOT EXISTS manual_standalone (
        identity TEXT PRIMARY KEY, version TEXT NOT NULL, saved_at TEXT NOT NULL,
        machine_id TEXT NOT NULL DEFAULT '')""",
    'manual_exclusions': """CREATE TABLE IF NOT EXISTS manual_exclusions (
        pair_a TEXT NOT NULL, pair_b TEXT NOT NULL, saved_at TEXT NOT NULL,
        machine_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(pair_a, pair_b))""",
}

# SKU 人工决定的账本（票 08）：与商品账本同层但分表——商品保存整版覆盖商品三表，SKU
# 保存只动这三张；两套决定互不掺和。行身份＝成员集合（成员身份+证据版本，与包内一致）；
# 组 ID 随行保存，跨机回放后推荐语境摘要才能对上。冲突账独立一张（sku_decision_conflicts），
# 内容形状与商品冲突账同规（kind/members/incoming/local），只是成员是 SKU 身份。
SKU_DECISION_TABLES = {
    'sku_manual_groups': """CREATE TABLE IF NOT EXISTS sku_manual_groups (
        members TEXT PRIMARY KEY, confirmed INTEGER NOT NULL, excluded INTEGER NOT NULL,
        group_id TEXT NOT NULL, saved_at TEXT NOT NULL, machine_id TEXT NOT NULL DEFAULT '')""",
    'sku_excluded_skus': """CREATE TABLE IF NOT EXISTS sku_excluded_skus (
        identity TEXT PRIMARY KEY, saved_at TEXT NOT NULL, machine_id TEXT NOT NULL DEFAULT '')""",
    'sku_excluded_pairs': """CREATE TABLE IF NOT EXISTS sku_excluded_pairs (
        pair_a TEXT NOT NULL, pair_b TEXT NOT NULL, saved_at TEXT NOT NULL,
        machine_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(pair_a, pair_b))""",
}
SKU_CONFLICT_TABLE = 'sku_decision_conflicts'
_SKU_CONFLICT_DDL = f"""CREATE TABLE IF NOT EXISTS {SKU_CONFLICT_TABLE} (
    digest TEXT PRIMARY KEY, kind TEXT NOT NULL, members TEXT NOT NULL,
    incoming_machine TEXT NOT NULL, incoming TEXT NOT NULL,
    local_machines TEXT NOT NULL, local TEXT NOT NULL, seen_at TEXT NOT NULL)"""


def source_of(entry, default):
    """一行决定的来源机器：行上显式带了就用它（导入的行原样保留），没带就记本机。

    关系是带键的行（dict），独立确认与排除是按位置的行（list，第三位是来源）。
    """
    if isinstance(entry, dict):
        return entry.get('machine_id') or default
    return entry[2] if len(entry) > 2 and entry[2] else default


class StoredDraft(NamedTuple):
    payload: dict
    saved_at: str


@dataclasses.dataclass(frozen=True)
class Conflict:
    """一条冲突：两侧各自的来源机器与内容，以及记下的时刻（票 04）。

    `recorded` 说这次收取有没有把它新记进冲突账：同一条冲突重复遇到（对方又发布了
    含它的新包）是 False——账不变，只是又核对了一遍。`local` 是列表：本机一侧可能
    由多行折成（多次收取叠出来的组）。

    `resolved_at` 是人裁决过的时刻（票 07）：页面上用现有编辑动作裁决后、下一次保存
    记下它；空串＝还没裁决。行照旧只增，裁决只标时刻。
    """

    digest: str
    kind: str
    members: tuple[str, ...]          # 两侧牵涉到的商品身份（页面按它找组与商品）
    incoming_machine: str
    incoming: dict                    # 外来一侧的决定内容
    local_machines: tuple[str, ...]
    local: tuple[dict, ...]           # 本机一侧的决定内容（行上带各自的来源）
    seen_at: str
    recorded: bool = True
    resolved_at: str = ''


@dataclasses.dataclass(frozen=True)
class MergeResult:
    """一次并入做了什么：三张决定表各并进多少行、遇到哪些冲突。"""

    adopted: dict[str, int]                       # 表名 → 这次并进本机账本的行数
    conflicts: tuple[Conflict, ...] = ()


class DraftStore:
    def __init__(self, path: Path, machine: str = ''):
        self.path = Path(path)
        self.machine = machine        # 本机编号：账本的来源列（没带来源的行记它）

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        # 与同款缓存同款的做法：每次打开都带条件建表，已有库只付一次判断。
        conn.execute("""CREATE TABLE IF NOT EXISTS analysis_drafts (
            id TEXT PRIMARY KEY,
            saved_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            start TEXT NOT NULL,
            end TEXT NOT NULL,
            payload TEXT NOT NULL)""")
        for table, statement in _DECISION_TABLES.items():
            conn.execute(statement)
            self._add_machine_column(conn, table)
        for statement in SKU_DECISION_TABLES.values():
            conn.execute(statement)
        conn.execute(_SKU_CONFLICT_DDL)
        conn.execute("""CREATE TABLE IF NOT EXISTS analysis_draft_save_order (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id TEXT NOT NULL UNIQUE)""")
        conn.execute(_CONFLICT_DDL)
        self._add_resolved_column(conn)
        return conn

    def _add_resolved_column(self, conn):
        """老库补上裁决时刻列（一次性，票 07）：补列前的冲突都没裁决过，空串即未裁决。"""
        if 'resolved_at' in {row[1] for row in conn.execute(f'PRAGMA table_info({CONFLICT_TABLE})')}:
            return
        with conn:
            conn.execute(f'ALTER TABLE {CONFLICT_TABLE}'
                         " ADD COLUMN resolved_at TEXT NOT NULL DEFAULT ''")

    def _add_machine_column(self, conn, table):
        """老库补上来源机器列（一次性，票 02）：补列前的行只有本机能写，记本机编号。"""
        if 'machine_id' in {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}:
            return
        with conn:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN machine_id TEXT NOT NULL DEFAULT ''")
            conn.execute(f"UPDATE {table} SET machine_id=? WHERE machine_id=''", (self.machine,))

    def write(self, analysis_id, start, end, saved_at, payload, ledger, *, resolved=()):
        """保存一版草稿与账本；`resolved` 里是这次保存认定的、人已裁决过的冲突摘要。

        裁决不属于账本（账本按现有编辑动作变化），但它跟保存一起生效：没保存就丢掉
        的裁决不落账，保存下来的才写进冲突行的 `resolved_at`（票 07）。行只增，
        这一步只标时刻。
        """
        document = json.dumps(payload, ensure_ascii=False)
        conn = self._connect()
        try:
            # 单事务：草稿版本与决策账本一起生效；中途失败整体回滚，
            # 上一个成功版本（含账本）原样可读。
            with conn:
                conn.execute("""INSERT INTO analysis_drafts(id,saved_at,schema_version,start,end,payload)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET saved_at=excluded.saved_at,
                    schema_version=excluded.schema_version, start=excluded.start,
                    end=excluded.end, payload=excluded.payload""",
                    (analysis_id, saved_at, SCHEMA_VERSION, start, end, document))
                self._mark_saved(conn, analysis_id)
                for table in _DECISION_TABLES:
                    conn.execute(f'DELETE FROM {table}')
                conn.executemany('INSERT INTO manual_relations(members,confirmed,saved_at,machine_id)'
                                 ' VALUES(?,?,?,?)',
                    [(json.dumps([[member, member_version] for member, member_version in relation['members']],
                                 ensure_ascii=False), relation['confirmed'], saved_at,
                      source_of(relation, self.machine))
                     for relation in ledger['relations']])
                conn.executemany('INSERT INTO manual_standalone(identity,version,saved_at,machine_id)'
                                 ' VALUES(?,?,?,?)',
                    [(entry[0], entry[1], saved_at, source_of(entry, self.machine))
                     for entry in ledger['standalone']])
                conn.executemany('INSERT INTO manual_exclusions(pair_a,pair_b,saved_at,machine_id)'
                                 ' VALUES(?,?,?,?)',
                    [(pair[0], pair[1], saved_at, source_of(pair, self.machine))
                     for pair in ledger['excluded']])
                conn.executemany(f'UPDATE {CONFLICT_TABLE} SET resolved_at=? WHERE digest=?',
                                 [(saved_at, digest) for digest in resolved])
        finally:
            conn.close()

    def ledger(self):
        """已保存的人工决策账本；从未保存过时为空。

        打开走 `_connect`：老库在这里补上来源机器列（票 02），与保存是同一条打开路径。
        只有「文件不存在」能当作没保存过：保存会整版覆盖账本，把读错误当成空白会把
        历史判断抹掉，所以其余读取错误照常抛出。
        """
        if not self.path.exists():
            return {'relations': [], 'standalone': [], 'excluded': []}
        conn = self._connect()
        try:
            return _read_ledger(conn)
        finally:
            conn.close()

    def merge_incoming(self, decisions, *, source, seen_at):
        """把外来账本并进本机账本（票 04、spec §6）：不冲突即生效，冲突写进冲突账。

        `decisions` 与 `ledger()` 同形（关系是带键的行、独立确认与排除对是按位置的行）；
        包里只有已确认的关系（未确认的中间态不外发）。逐条对着**收取前那一刻**的本机
        账本判：

        - 已经是同一条（成员与版本一一对应）→ 什么都不做（重复收取幂等）。
        - 与本机决定相对 → **两边都不动**：这一行不并、本机一行不改，冲突写进冲突账
          （两侧来源机器与内容、记下的时刻）；只增不自动消解。
        - 整段出自同一台机器的旧话（它先前发布过、本机收下过的那几行）→ 让位：删掉旧的
          再并新的——同一台机器在给自己更新（组扩大了、缩小了、改成了单独成组、或把排除
          对翻成了成组），不是两台机器的分歧。掺着别的话（含本机自己的决定）时就不让位。
        - 其余 → 原样并入（版本与来源随行带走），缺席成员照旧认领、不报错。

        相对的判法（spec §6 三类）：

        - **并组 vs 排除**：外来关系里有本机排除过的对；或外来排除对的两端已在本机同一
          个已确认组里。
        - **分入不同组**：外来关系（或单独成组）与本机已确认组共享成员，而两边的成员集
          不同——同一条决定才算成员集相同。单独成组是只含一人的组，「独自成组」与「与谁
          同组」因此都是完整的两句话，必然相对。
        - **确认 vs 撤回**：外来关系里有本机撤回过的那条关系里的对——本机说「不成组」，
          对方说「成组」。

        来源：行上带了就用行的（转发的决定仍点名原来那台机器），没带记 `source`。
        让位与冲突都以行的来源为界：转发来的别人的话，仍算别人的。
        `seen_at` 是记下的时刻（收取时刻，UTC ISO）。
        """
        conn = self._connect()
        try:
            merge = _LedgerMerge(conn, source=source, seen_at=seen_at)
            with conn:
                merge.run(decisions)
        finally:
            conn.close()
        return MergeResult(adopted=merge.adopted, conflicts=tuple(merge.conflicts))

    def conflicts(self):
        """本机的冲突账（票 04）：只增不自动消解——页面上的裁决不删它（裁决改的是账本）。

        按记下的先后返回；形状与并入结果里的 `Conflict` 一致（`recorded` 恒为真——
        记下来的都已经在账上）。人裁决过的带 `resolved_at`（票 07）：页面按它把
        已裁决的冲突从展示里摘掉，行本身照旧留着作历史。
        """
        if not self.path.exists():
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                f'SELECT digest,kind,members,incoming_machine,incoming,'
                f'local_machines,local,seen_at,resolved_at FROM {CONFLICT_TABLE} ORDER BY rowid').fetchall()
        finally:
            conn.close()
        return [Conflict(digest=row[0], kind=row[1], members=tuple(json.loads(row[2])),
                         incoming_machine=row[3], incoming=json.loads(row[4]),
                         local_machines=tuple(json.loads(row[5])),
                         local=tuple(json.loads(row[6])), seen_at=row[7], resolved_at=row[8])
                for row in rows]

    # ---- SKU 人工决定的账本（票 08）：读取、并入与冲突账 ----

    def sku_ledger(self):
        """已保存与已收取的 SKU 人工决定；从未有决定时为空（形状见 `_read_sku_ledger`）。"""
        if not self.path.exists():
            return {'groups': [], 'excluded_skus': [], 'excluded_pairs': []}
        conn = self._connect()
        try:
            return _read_sku_ledger(conn)
        finally:
            conn.close()

    def merge_incoming_sku(self, decisions, *, source, seen_at):
        """把外来的 SKU 人工决定并进本机 SKU 账本（票 08）。

        判法与商品账本同规（见 `_SkuLedgerMerge`）：同一条决定跳过（幂等）、同一台机器
        的旧话让位（它在给自己更新）、异源矛盾记 `sku_decision_conflicts` 两边都不动、
        其余原样并入。`decisions` 与 `sku_ledger()` 同形。
        """
        conn = self._connect()
        try:
            merge = _SkuLedgerMerge(conn, source=source, seen_at=seen_at)
            with conn:
                merge.run(decisions)
        finally:
            conn.close()
        return MergeResult(adopted=merge.adopted, conflicts=tuple(merge.conflicts))

    def sku_conflicts(self):
        """本机的 SKU 冲突账（票 08）：只增不自动消解，形状与 `conflicts()` 同规。"""
        if not self.path.exists():
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                f'SELECT digest,kind,members,incoming_machine,incoming,'
                f'local_machines,local,seen_at FROM {SKU_CONFLICT_TABLE} ORDER BY rowid').fetchall()
        finally:
            conn.close()
        return [Conflict(digest=row[0], kind=row[1], members=tuple(json.loads(row[2])),
                         incoming_machine=row[3], incoming=json.loads(row[4]),
                         local_machines=tuple(json.loads(row[5])),
                         local=tuple(json.loads(row[6])), seen_at=row[7])
                for row in rows]

    def record_sku_replay_conflicts(self, conflicts, *, seen_at):
        """记一批回放期冲突（票 08）：与并入期冲突同一个账（`sku_decision_conflicts`）。

        `conflicts` 是 `apply_shared` 交来的行（`{kind, incoming, local}`，行形状见那里）；
        这里统一成冲突内容、算内容摘要——同一条（摘要相同）只记一次，重复分析只核对不重记。
        返回这次新记下的条数。
        """
        if not conflicts:
            return 0
        conn = self._connect()
        try:
            known = {row[0] for row in conn.execute(f'SELECT digest FROM {SKU_CONFLICT_TABLE}')}
            recorded = 0
            with conn:
                for entry in conflicts:
                    incoming = _format_sku_entry(entry["incoming"])
                    local_contents = sorted((_format_sku_entry(item) for item in entry["local"]),
                                            key=lambda content: json.dumps(content, sort_keys=True,
                                                                           ensure_ascii=False))
                    _, fresh = _insert_sku_conflict(conn, known, kind=entry["kind"],
                                                    incoming=incoming, local_contents=local_contents,
                                                    seen_at=seen_at)
                    recorded += fresh
            return recorded
        finally:
            conn.close()

    @staticmethod
    def _mark_saved(conn, analysis_id):
        # 两代本地草稿共用保存次序；同秒保存/再次保存不能按随机 UUID 猜最新。
        conn.execute('INSERT OR REPLACE INTO analysis_draft_save_order(draft_id) VALUES (?)',
                     (analysis_id,))

    def write_sku(self, payload, decisions=None):
        """单独本地格式，不改旧草稿或商品人工账本，旧程序也不会读成商品。

        `decisions` 给了就同一事务把本机 SKU 人工决定并进 SKU 账本（票 08）：
        草稿版本与账本一起生效，中途失败整体回滚（与商品草稿保存同规）。
        """
        conn = self._connect()
        try:
            with conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS sku_analysis_drafts (
                    id TEXT PRIMARY KEY, saved_at TEXT NOT NULL, schema_version INTEGER NOT NULL,
                    start TEXT NOT NULL, end TEXT NOT NULL, payload TEXT NOT NULL)""")
                conn.execute("""INSERT INTO sku_analysis_drafts VALUES (?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET saved_at=excluded.saved_at,
                    schema_version=excluded.schema_version,start=excluded.start,
                    end=excluded.end,payload=excluded.payload""",
                    (payload['id'], payload['saved_at'], SKU_SCHEMA_VERSION, payload['start'], payload['end'],
                     json.dumps(payload, ensure_ascii=False)))
                self._mark_saved(conn, payload['id'])
                if decisions is not None:
                    present = {_sku_identity(member) for member in payload.get('members', [])}
                    _write_sku_ledger(conn, present, decisions, machine=self.machine,
                                      saved_at=payload['saved_at'])
        finally:
            conn.close()

    def latest(self):
        if not self.path.exists():
            return None
        conn = self._connect()
        try:
            rows = list(conn.execute('SELECT rowid AS legacy_order,id,start,end,saved_at,schema_version FROM analysis_drafts'))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sku_analysis_drafts'").fetchone():
                rows += list(conn.execute('SELECT rowid AS legacy_order,id,start,end,saved_at,schema_version FROM sku_analysis_drafts'))
            order = dict(conn.execute('SELECT draft_id,sequence FROM analysis_draft_save_order'))
            row = max(rows, key=lambda r: (order.get(r['id'], 0), r['saved_at'], r['legacy_order'])) if rows else None
        finally:
            conn.close()
        return {key: row[key] for key in ('id', 'start', 'end', 'saved_at', 'schema_version')} if row else None

    def read(self, analysis_id):
        if not self.path.exists():
            return None
        conn = self._connect()
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sku_analysis_drafts'").fetchone():
                sku = conn.execute('SELECT * FROM sku_analysis_drafts WHERE id=?', (analysis_id,)).fetchone()
                if sku:
                    if sku['schema_version'] not in (2, SKU_SCHEMA_VERSION):
                        raise ValueError('SKU 草稿版本不兼容，未做任何修改')
                    return StoredDraft(json.loads(sku['payload']), sku['saved_at'])
            row = conn.execute("SELECT * FROM analysis_drafts WHERE id=?", (analysis_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        if row['schema_version'] != SCHEMA_VERSION:
            # 报错而不是清空：旧草稿由用户决定怎么处理，程序不代它丢数据。
            raise ValueError("上次保存的分析草稿来自不兼容的版本，未做任何修改；请升级程序后再打开")
        return StoredDraft(json.loads(row['payload']), row['saved_at'])


# ---- 外来决定的并入（票 04）：判法在上面 `merge_incoming`，这里是把判法写出来的那几件 ----


def _read_ledger(conn):
    """账本三张表读成 `ledger()` 那份形状（关系的成员解出 JSON、其余按位置）。

    保存、读回与并入（票 04）都走这一口读——行与列的口径只此一份。
    """
    return {
        'relations': [{'members': json.loads(row[0]), 'confirmed': bool(row[1]),
                       'machine_id': row[2]}
                      for row in conn.execute('SELECT members,confirmed,machine_id FROM manual_relations')],
        'standalone': [[row[0], row[1], row[2]]
                       for row in conn.execute('SELECT identity,version,machine_id FROM manual_standalone')],
        'excluded': [[row[0], row[1], row[2]]
                     for row in conn.execute('SELECT pair_a,pair_b,machine_id FROM manual_exclusions')],
    }


def _ledger_state(conn):
    """并入判法要用的本机账本形状：`ledger()` 那份 + 两个查重索引。"""
    ledger = _read_ledger(conn)
    ledger['standalone_by_id'] = {entry[0]: entry for entry in ledger['standalone']}
    ledger['exclusion_pairs'] = {frozenset((pair[0], pair[1])) for pair in ledger['excluded']}
    return ledger


# 一行本机账本在判法里的两种形态：关系是带键的行（dict，带确认与来源），
# 单独成组与排除对是按位置的行（list）。
def _row_kind(row) -> str:
    """一行本机账本是关系还是单独成组——让位时按它选删除哪张表。"""
    return 'relation' if isinstance(row, dict) else 'standalone'


def _content_of(row) -> dict:
    """一行本机账本折成冲突说明里的「内容」（两侧同一形状，页面与周报照它渲染）。"""
    return (_relation_content(row['members'], row['confirmed'], row['machine_id'])
            if isinstance(row, dict) else _standalone_content(row))


@dataclasses.dataclass(frozen=True)
class _Clash:
    """一条外来决定与本机账本的关系：相对的那些（记冲突）与该让位的那些（删掉）。"""

    opponents: list          # 本机侧内容（`_relation_content`／`_standalone_content`）
    superseded: list         # 该删的本机行：(表类, 行)


# 冲突账里的「内容」：一侧说了什么——成员（带版本）、来源机器；关系多一个确认与否，
# 排除对没有版本（排除本来就只按身份）。两侧同一形状，页面与周报都照它渲染。
def _relation_content(members, confirmed, machine):
    return {'kind': SIDE_RELATION, 'confirmed': bool(confirmed),
            'members': [[member, member_version] for member, member_version in members],
            'machine': machine or ''}


def _standalone_content(entry):
    return {'kind': SIDE_STANDALONE, 'members': [[entry[0], entry[1]]],
            'machine': entry[2] if len(entry) > 2 else ''}


def _exclusion_content(pair):
    return {'kind': SIDE_EXCLUSION, 'members': [[pair[0], None], [pair[1], None]],
            'machine': pair[2] if len(pair) > 2 else ''}


def _same_members(left, right):
    """两组（成员, 版本）是不是同一条关系：顺序无关。"""
    return (sorted((member, member_version) for member, member_version in left)
            == sorted((member, member_version) for member, member_version in right))


def _shared_pair(left, right):
    """两条关系有没有同一对成员（按身份）：撤回过的那条被外来关系重新认领。"""
    identities = {member for member, _ in right}
    return any({first, second} <= identities
               for first, second in itertools.combinations((m for m, _ in left), 2))


class _ConfirmedGroups:
    """本机账本「已确认决定」的成员闭包：身份 → 组，顺带记住形成每个组的行。

    撤回过的关系不是决定（本机说「不成组」，没有组可言），不进闭包；单独成组是只含
    一个成员的组，它的行就是那一条。组的「行」与「来源机器」给并入判法用：页面上要说
    「谁的组」，就得说得出是账本里的哪些行；「整段出自同一台机器」才让位，也看这些行。
    """

    def __init__(self, relations, standalone):
        self._parent = {}
        self._rows = []
        for relation in relations:
            if not relation['confirmed']:
                continue
            identities = [member for member, _ in relation['members']]
            for identity in identities:
                self._parent.setdefault(identity, identity)
            for identity in identities[1:]:
                self._union(identities[0], identity)
            self._rows.append(relation)
        self._standalone = {entry[0]: entry for entry in standalone}
        self._components = {}
        for identity in self._parent:
            self._components.setdefault(self._find(identity), set()).add(identity)
        self._component_rows = {}
        for relation in self._rows:
            self._component_rows.setdefault(self._find(relation['members'][0][0]), []).append(relation)

    def _find(self, identity):
        while self._parent[identity] != identity:
            self._parent[identity] = self._parent[self._parent[identity]]
            identity = self._parent[identity]
        return identity

    def _union(self, left, right):
        left, right = self._find(left), self._find(right)
        if left != right:
            self._parent[right] = left

    def in_group(self, identity) -> bool:
        return identity in self._parent

    def same_group(self, left, right) -> bool:
        return (left in self._parent and right in self._parent
                and self._find(left) == self._find(right))

    def group_of(self, identity):
        """成员所在的组：`(行列表, 成员身份集, 来源机器集)`；没在关系组里就是 None。

        单独成组不在这里（它的行只有一条、版本要单独比），由并入判法另判。
        """
        if identity not in self._parent:
            return None
        root = self._find(identity)
        rows = self._component_rows.get(root, [])
        machines = {row['machine_id'] or '' for row in rows}
        return rows, self._components[root], machines

    def joined_by(self, members, left, right) -> bool:
        """把 `members` 这条关系并进来之后，left、right 会不会落在同一个组里。"""
        identities = {member for member, _ in members}
        roots = {self._find(member) for member in identities if member in self._parent}
        if left in identities or right in identities:
            other = right if left in identities else left
            return other in identities or (other in self._parent and self._find(other) in roots)
        return (left in self._parent and right in self._parent
                and self._find(left) in roots and self._find(right) in roots)


class _LedgerMerge:
    """一次收取里的账本并入（票 04）：外来三张表的行逐条判、按判法落库。

    每条都对着收取前那一刻的本机账本判，同一条包里的行不互相判——来源那台自己的账本
    已经把它的闭包定死了（同一个组的行可能不止一条），逐行再判只会把行序变成一个因素。
    """

    def __init__(self, conn, *, source, seen_at):
        self.conn = conn
        self.source = source
        self.seen_at = seen_at
        self.local = _ledger_state(conn)
        self.groups = _ConfirmedGroups(self.local['relations'], self.local['standalone'])
        self.known = {row[0] for row in conn.execute(f'SELECT digest FROM {CONFLICT_TABLE}')}
        self.adopted = {table: 0 for table in _DECISION_TABLES}
        self.conflicts = []

    def run(self, decisions):
        for row in decisions['relations']:
            if row['confirmed']:                        # 未确认的中间态不外发，也不并
                self.relation(row)
        for entry in decisions['standalone']:
            self.standalone(entry)
        for pair in decisions['excluded']:
            self.exclusion(pair)

    def relation(self, row):
        """一条外来关系：同一条跳过、相对记冲突、同一台机器的旧话让位、其余原样并入。"""
        members = [(member, member_version) for member, member_version in row['members']]
        source = source_of(row, self.source)
        content = _relation_content(members, True, source)
        if any(_same_members(other['members'], members)
               for other in self.local['relations'] if other['confirmed']):
            return
        withdrawn = [other for other in self.local['relations']
                     if not other['confirmed'] and _shared_pair(other['members'], members)]
        if withdrawn:
            self._conflict(CONFLICT_CONFIRM_VS_WITHDRAW, content,
                           [_relation_content(other['members'], other['confirmed'],
                                              other['machine_id']) for other in withdrawn])
            return
        broken = self._broken_exclusions(members, source)
        if broken.opponents:
            self._conflict(CONFLICT_GROUP_VS_EXCLUSION, content, broken.opponents)
            return
        clash = self._clash(members, source)
        if clash.opponents:
            self._conflict(CONFLICT_DIFFERENT_GROUPS, content, clash.opponents)
            return
        self._retire(broken.superseded + clash.superseded)
        self._adopt('manual_relations',
                    'INSERT OR IGNORE INTO manual_relations(members,confirmed,saved_at,machine_id)'
                    ' VALUES(?,?,?,?)',
                    (json.dumps([[member, member_version] for member, member_version in members],
                                ensure_ascii=False), 1, self.seen_at, source))

    def standalone(self, entry):
        """一条外来单独成组：同一条跳过；同一台机器的旧话让位；相对记冲突；其余并入。"""
        member, member_version = entry[0], entry[1]
        source = source_of(entry, self.source)
        content = _standalone_content([member, member_version, source])
        known = self.local['standalone_by_id'].get(member)
        if known is not None:
            if known[1] == member_version:
                return                                  # 同一条：成员与版本都对上
            if source_of(known, '') == source:
                self._retire([('standalone', known)])   # 同一台机器把版本更新了
            else:
                self._conflict(CONFLICT_DIFFERENT_GROUPS, content,
                               [_standalone_content(known)])
                return
        clash = self._clash([(member, member_version)], source)
        if clash.opponents:
            self._conflict(CONFLICT_DIFFERENT_GROUPS, content, clash.opponents)
            return
        self._retire(clash.superseded)
        self._adopt('manual_standalone',
                    'INSERT OR IGNORE INTO manual_standalone(identity,version,saved_at,machine_id)'
                    ' VALUES(?,?,?,?)', (member, member_version, self.seen_at, source))

    def exclusion(self, pair):
        """一条外来排除对：本机排除过同一对就跳过；两端在本机同一个组里才可能相对。"""
        left, right = pair[0], pair[1]
        source = source_of(pair, self.source)
        if frozenset((left, right)) in self.local['exclusion_pairs']:
            return
        if self.groups.same_group(left, right):
            rows, _, machines = self.groups.group_of(left)
            if machines == {source}:
                self._retire([(_row_kind(row), row) for row in rows])
            else:
                self._conflict(CONFLICT_GROUP_VS_EXCLUSION,
                               _exclusion_content([left, right, source]),
                               [_relation_content(other['members'], other['confirmed'],
                                                  other['machine_id']) for other in rows])
                return
        self._adopt('manual_exclusions',
                    'INSERT OR IGNORE INTO manual_exclusions(pair_a,pair_b,saved_at,machine_id)'
                    ' VALUES(?,?,?,?)', (left, right, self.seen_at, source))

    def _clash(self, members, source):
        """外来这条决定与本机的组「分入不同组」的那些：谁是相对的、谁该让位。

        成员集相同 → 同一条决定（相容）；成员集不同 → 整组都出自外来那台机器的让位
        （它在给自己更新），否则相对。单独成组按只含一人的组比：与任何含它的关系都不同。
        """
        identities = {member for member, _ in members}
        opponents, superseded, seen = [], [], set()
        for member in identities:
            group = self.groups.group_of(member)
            if group is None:
                entry = self.local['standalone_by_id'].get(member)
                if entry is None:
                    continue
                rows, group_ids, machines = [entry], {member}, {source_of(entry, '')}
            else:
                rows, group_ids, machines = group
            key = frozenset(group_ids)
            if key in seen:
                continue
            seen.add(key)
            if group_ids == identities:
                continue                                # 成员集相同：同一条决定
            if machines == {source}:
                superseded.extend((_row_kind(row), row) for row in rows)
            else:
                opponents.extend(_content_of(row) for row in rows)
        return _Clash(opponents=opponents, superseded=superseded)

    def _broken_exclusions(self, members, source):
        """外来关系会把哪些本机排除对关进同一个组：同一台机器的让位，其余相对。"""
        opponents, superseded = [], []
        for pair in self.local['excluded']:
            if self.groups.same_group(pair[0], pair[1]):
                continue                    # 本机账本自己就矛盾的一对：不算在这条关系头上
            if not self.groups.joined_by(members, pair[0], pair[1]):
                continue
            if source_of(pair, '') == source:
                superseded.append(('exclusion', [pair[0], pair[1]]))
            else:
                opponents.append(_exclusion_content(pair))
        return _Clash(opponents=opponents, superseded=superseded)

    def _retire(self, stale):
        """让位：删掉同一台机器先前的话（旧关系、旧独立成组或旧排除对）。"""
        for kind, row in stale:
            if kind == 'relation':
                self.conn.execute('DELETE FROM manual_relations WHERE members=?',
                                  (json.dumps(row['members'], ensure_ascii=False),))
            elif kind == 'standalone':
                self.conn.execute('DELETE FROM manual_standalone WHERE identity=?', (row[0],))
            else:
                self.conn.execute('DELETE FROM manual_exclusions WHERE pair_a=? AND pair_b=?',
                                  (row[0], row[1]))

    def _adopt(self, table, statement, values):
        self.adopted[table] += self.conn.execute(statement, values).rowcount

    def _conflict(self, kind, incoming, local_contents):
        """记一条冲突：本机一行不动、外来那一行也不并；同一条重复遇到只核对不重记。"""
        local_contents = sorted(local_contents,
                                key=lambda content: json.dumps(content, sort_keys=True,
                                                               ensure_ascii=False))
        conflict_digest = _digest({'kind': kind, 'incoming': incoming, 'local': local_contents})
        members = sorted({member for content in [incoming, *local_contents]
                          for member, _ in content['members']})
        machines = sorted({content['machine'] for content in local_contents})
        recorded = conflict_digest not in self.known
        if recorded:
            self.known.add(conflict_digest)
            self.conn.execute(
                f'INSERT INTO {CONFLICT_TABLE}(digest,kind,members,incoming_machine,incoming,'
                'local_machines,local,seen_at) VALUES(?,?,?,?,?,?,?,?)',
                (conflict_digest, kind, json.dumps(members, ensure_ascii=False),
                 incoming['machine'], json.dumps(incoming, ensure_ascii=False),
                 json.dumps(machines, ensure_ascii=False),
                 json.dumps(local_contents, ensure_ascii=False), self.seen_at))
        self.conflicts.append(Conflict(
            digest=conflict_digest, kind=kind, members=tuple(members),
            incoming_machine=incoming['machine'], incoming=incoming,
            local_machines=tuple(machines), local=tuple(local_contents),
            seen_at=self.seen_at, recorded=recorded))


# ---- 外来 SKU 决定的并入（票 08）：判法在 `_SkuLedgerMerge`，这里是把判法写出来的那几件 ----

def _read_sku_ledger(conn):
    """SKU 账本三张表读成 `sku_ledger()` 那份形状：组解出成员对、其余按位置带来源。"""
    return {
        'groups': [{'members': json.loads(row[0]), 'confirmed': bool(row[1]),
                    'excluded': bool(row[2]), 'group_id': row[3], 'machine_id': row[4]}
                   for row in conn.execute(
                       'SELECT members,confirmed,excluded,group_id,machine_id FROM sku_manual_groups')],
        'excluded_skus': [[row[0], row[1]] for row in conn.execute(
            'SELECT identity,machine_id FROM sku_excluded_skus')],
        'excluded_pairs': [[row[0], row[1], row[2]] for row in conn.execute(
            'SELECT pair_a,pair_b,machine_id FROM sku_excluded_pairs')],
    }


def _sku_group_key(members):
    """一组 SKU 成员的规范行键（身份+证据版本、排序后 JSON）：与表主键同一口径。"""
    return json.dumps(sorted((member, member_version) for member, member_version in members),
                      ensure_ascii=False)


def _sku_group_content(members, row, machine):
    return {'kind': SIDE_RELATION, 'confirmed': bool(row['confirmed']),
            'excluded': bool(row.get('excluded')),
            'members': [[member, member_version] for member, member_version in members],
            'machine': machine or ''}


def _sku_exclusion_content(entry):
    return {'kind': SIDE_STANDALONE, 'members': [[entry[0], None]],
            'machine': entry[1] if len(entry) > 1 else ''}


def _sku_pair_content(pair):
    return {'kind': SIDE_EXCLUSION, 'members': [[pair[0], None], [pair[1], None]],
            'machine': pair[2] if len(pair) > 2 else ''}


def _insert_sku_conflict(conn, known, *, kind, incoming, local_contents, seen_at):
    """把一条 SKU 冲突写进 `sku_decision_conflicts`（票 08）：并入期与回放期共用这一件。

    `incoming` 与 `local_contents` 都是格式化后的内容；摘要按内容算，`known` 是调用方
    已读到的既有摘要集合（新记的同步补进，同一批里重复也不再算新记）。返回
    `(摘要, 这次是否新记)`——同一条重复遇到只核对不重记。
    """
    conflict_digest = _digest({'kind': kind, 'incoming': incoming, 'local': local_contents})
    if conflict_digest in known:
        return conflict_digest, False
    known.add(conflict_digest)
    members = sorted({member for content in [incoming, *local_contents]
                      for member, _ in content['members']})
    machines = sorted({content['machine'] for content in local_contents})
    conn.execute(
        f'INSERT INTO {SKU_CONFLICT_TABLE}(digest,kind,members,incoming_machine,incoming,'
        'local_machines,local,seen_at) VALUES(?,?,?,?,?,?,?,?)',
        (conflict_digest, kind, json.dumps(members, ensure_ascii=False),
         incoming['machine'], json.dumps(incoming, ensure_ascii=False),
         json.dumps(machines, ensure_ascii=False),
         json.dumps(local_contents, ensure_ascii=False), seen_at))
    return conflict_digest, True


def _format_sku_entry(entry):
    """回放期冲突行的统一格式（票 08）：`apply_shared` 交来的 `{side, …}` 折成与
    并入期冲突同一份内容形状（组／排除身份／排除对三种 `side`）。"""
    side = entry.get('side') or 'group'
    if side == 'sku':
        return _sku_exclusion_content([entry['members'][0][0], entry.get('machine') or ''])
    if side == 'pair':
        return _sku_pair_content([entry['members'][0][0], entry['members'][1][0],
                                  entry.get('machine') or ''])
    return _sku_group_content([(member, version) for member, version in entry['members']],
                              entry, entry.get('machine') or '')


def _sku_exclusion_source(entry, default):
    """排除身份行 [身份, 来源?] 的来源：第 2 位是来源；没带记通道来源 `default`。

    与商品账本的 `source_of` 分开的点就在这：SKU 的排除身份行没有版本位，
    按 `source_of` 的位置读会把来源读空。
    """
    return entry[1] if len(entry) > 1 and entry[1] else default


def _sku_pair_source(pair, default):
    """排除对 [a, b, 来源?] 的来源：第 3 位才是来源（第 2 位是另一半身份，
    按末位读会在只带两端的行上把身份当来源）。没带记通道来源 `default`。
    """
    return pair[2] if len(pair) > 2 and pair[2] else default


def _write_sku_ledger(conn, present, decisions, *, machine, saved_at):
    """保存时把本机 SKU 决定并进账本（票 08）：产品 `merge_decisions` 的 SKU 版。

    判法（三张表同一条，由 `_rewrite_sku_ledger_rows` 落）：

    - 旧行成员全在场（`present`＝本次快照的成员身份）：新决定里有同一成员集 → 按新行
      改写（版本／确认／排除刷新），来源沿用旧行（收进来的决定往返一次不被改写）；
      没有 → 删掉旧行（本机视图是权威：人改过的组、撤掉的排除都以这次保存为准）。
    - 旧行有缺席成员：原样保留——保存局部区间不能抹掉全局历史。
    - 排除身份、排除对同规（排除对两端都在场才可改写）。
    """
    old = _read_sku_ledger(conn)
    new_groups = {frozenset(member for member, _ in row['members']): row
                  for row in decisions['groups']}
    _rewrite_sku_ledger_rows(
        old['groups'], new_groups, machine=machine,
        key_of=lambda row: frozenset(member for member, _ in row['members']),
        present_ok=lambda row: {member for member, _ in row['members']} <= present,
        source_of_old=lambda row: source_of(row, machine),
        delete=lambda row: conn.execute('DELETE FROM sku_manual_groups WHERE members=?',
                                        (_sku_group_key(row['members']),)),
        insert=lambda row, source: _insert_sku_group(conn, row, source=source, saved_at=saved_at))
    new_skus = {entry[0]: entry for entry in decisions['excluded_skus']}
    _rewrite_sku_ledger_rows(
        old['excluded_skus'], new_skus, machine=machine,
        key_of=lambda entry: entry[0],
        present_ok=lambda entry: entry[0] in present,
        source_of_old=lambda entry: _sku_exclusion_source(entry, machine),
        delete=lambda entry: conn.execute('DELETE FROM sku_excluded_skus WHERE identity=?',
                                          (entry[0],)),
        insert=lambda entry, source: conn.execute(
            'INSERT OR IGNORE INTO sku_excluded_skus(identity,saved_at,machine_id) VALUES(?,?,?)',
            (entry[0], saved_at, source)))
    new_pairs = {frozenset((row[0], row[1])): row for row in decisions['excluded_pairs']}
    _rewrite_sku_ledger_rows(
        old['excluded_pairs'], new_pairs, machine=machine,
        key_of=lambda pair: frozenset((pair[0], pair[1])),
        present_ok=lambda pair: {pair[0], pair[1]} <= present,
        source_of_old=lambda pair: _sku_pair_source(pair, machine),
        delete=lambda pair: conn.execute('DELETE FROM sku_excluded_pairs WHERE pair_a=? AND pair_b=?',
                                         (pair[0], pair[1])),
        insert=lambda pair, source: conn.execute(
            'INSERT OR IGNORE INTO sku_excluded_pairs(pair_a,pair_b,saved_at,machine_id)'
            ' VALUES(?,?,?,?)', (pair[0], pair[1], saved_at, source)))


def _rewrite_sku_ledger_rows(old_rows, new_by_key, *, machine, key_of, present_ok,
                             source_of_old, delete, insert):
    """一张 SKU 账本表的保存侧改写：在场旧行删掉后按新视图重写（同键沿用原来源），
    缺席旧行原样保留；新视图里没被旧行接上的按本机来源补插。三张表共用这一条判法。
    """
    handled = set()
    for old_row in old_rows:
        if not present_ok(old_row):
            continue                                    # 有缺席成员：原样保留
        delete(old_row)
        key = key_of(old_row)
        if key not in new_by_key:
            continue                                    # 本机视图里这条决定没了：撤下
        handled.add(key)
        insert(new_by_key[key], source_of_old(old_row))
    for key, row in new_by_key.items():
        if key not in handled:
            insert(row, machine)


def _insert_sku_group(conn, row, *, source, saved_at):
    conn.execute(
        'INSERT OR IGNORE INTO sku_manual_groups(members,confirmed,excluded,group_id,saved_at,machine_id)'
        ' VALUES(?,?,?,?,?,?)',
        (_sku_group_key(row['members']), int(bool(row['confirmed'])), int(bool(row.get('excluded'))),
         row['group_id'], saved_at, source))


class _SkuLedgerMerge:
    """一次收取（或种子写入）里的 SKU 账本并入（票 08）。

    每条都对着收取前那一刻的本机账本判，同一条包里的行不互相判——来源那台自己的账本
    已经把它的决定定死了，逐行再判只会把行序变成一个因素（与商品账本同规）。
    同源（同一台机器）的旧话让位：它先前发布的组/排除被新的替换，不是两台机器的分歧；
    掺着别家的话就不让位，记冲突、两边都不动。行上带了来源就用行的（转发的决定仍点名
    原来那台机器），没带记 `source`。
    """

    def __init__(self, conn, *, source, seen_at):
        self.conn = conn
        self.source = source
        self.seen_at = seen_at
        self.local = _read_sku_ledger(conn)
        self.known = {row[0] for row in conn.execute(f'SELECT digest FROM {SKU_CONFLICT_TABLE}')}
        self.adopted = {table: 0 for table in SKU_DECISION_TABLES}
        self.conflicts = []

    def run(self, decisions):
        for row in decisions['groups']:
            self.group(row)
        for entry in decisions['excluded_skus']:
            self.excluded_sku(entry)
        for pair in decisions['excluded_pairs']:
            self.excluded_pair(pair)

    def group(self, row):
        """一条外来人工组：同一条跳过或同源更新、矛盾记冲突、其余并入（含状态更新）。"""
        members = [(member, member_version) for member, member_version in row['members']]
        idents = {member for member, _ in members}
        source = source_of(row, self.source)
        content = _sku_group_content(members, row, source)
        # 同一成员集就是同一条决定：版本只是内容刷新，不作第二行并存（否则重复行会越积越多）。
        same = [other for other in self.local['groups']
                if {member for member, _ in other['members']} == idents]
        if same:
            if any(source_of(other, '') != source for other in same):
                return                             # 别的机器已记过同一决定：先到者保留，幂等跳过
            self._retire_groups(same)              # 同一台机器在更新自己（版本/确认/排除）
        opponents, superseded = [], []
        for other in self.local['groups']:
            other_idents = {member for member, _ in other['members']}
            if other_idents == idents or not (other_idents & idents):
                continue                           # 同一成员集在上面判过；不相交与这条无关
            if source_of(other, '') == source:
                superseded.append(('group', other))
            else:
                kind = (CONFLICT_GROUP_VS_EXCLUSION if other.get('excluded')
                        else CONFLICT_DIFFERENT_GROUPS)
                opponents.append((kind,
                                  _sku_group_content(other['members'], other, source_of(other, ''))))
        for pair in self.local['excluded_pairs']:
            if not {pair[0], pair[1]} <= idents:
                continue
            if _sku_pair_source(pair, '') == source:
                superseded.append(('pair', pair))
            else:
                opponents.append((CONFLICT_GROUP_VS_EXCLUSION, _sku_pair_content(pair)))
        for entry in self.local['excluded_skus']:
            if entry[0] not in idents:
                continue
            if _sku_exclusion_source(entry, '') == source:
                superseded.append(('sku', entry))
            else:
                opponents.append((CONFLICT_GROUP_VS_EXCLUSION, _sku_exclusion_content(entry)))
        if opponents:
            self._conflict_kinds(opponents, content)
            return
        self._retire(superseded)
        self._adopt('sku_manual_groups',
                    'INSERT OR IGNORE INTO sku_manual_groups'
                    '(members,confirmed,excluded,group_id,saved_at,machine_id) VALUES(?,?,?,?,?,?)',
                    (_sku_group_key(members), int(bool(row['confirmed'])), int(bool(row.get('excluded'))),
                     row['group_id'], self.seen_at, source))

    def excluded_sku(self, entry):
        """一条外来排除身份：同一条跳过或同源更新、与本机组矛盾记冲突、其余并入。"""
        ident = entry[0]
        source = _sku_exclusion_source(entry, self.source)
        content = _sku_exclusion_content([ident, source])
        current = [other for other in self.local['excluded_skus'] if other[0] == ident]
        if current:
            if all(_sku_exclusion_source(other, '') == source for other in current):
                self._retire([('sku', other) for other in current])
            else:
                return
        rows = [row for row in self.local['groups']
                if ident in {member for member, _ in row['members']}]
        opponents = [(CONFLICT_GROUP_VS_EXCLUSION,
                      _sku_group_content(row['members'], row, source_of(row, '')))
                     for row in rows if source_of(row, '') != source]
        if opponents:
            self._conflict_kinds(opponents, content)
            return
        self._retire([('group', row) for row in rows])
        self._adopt('sku_excluded_skus',
                    'INSERT OR IGNORE INTO sku_excluded_skus(identity,saved_at,machine_id)'
                    ' VALUES(?,?,?)',
                    (ident, self.seen_at, source))

    def excluded_pair(self, pair):
        """一条外来排除对：同一条跳过或同源更新、两端同组矛盾记冲突、其余并入。"""
        left, right = pair[0], pair[1]
        source = _sku_pair_source(pair, self.source)
        content = _sku_pair_content([left, right, source])
        current = [other for other in self.local['excluded_pairs']
                   if {other[0], other[1]} == {left, right}]
        if current:
            if all(_sku_pair_source(other, '') == source for other in current):
                self._retire([('pair', other) for other in current])
            else:
                return
        rows = [row for row in self.local['groups']
                if {left, right} <= {member for member, _ in row['members']}]
        opponents = [(CONFLICT_GROUP_VS_EXCLUSION,
                      _sku_group_content(row['members'], row, source_of(row, '')))
                     for row in rows if source_of(row, '') != source]
        if opponents:
            self._conflict_kinds(opponents, content)
            return
        self._retire([('group', row) for row in rows])
        self._adopt('sku_excluded_pairs',
                    'INSERT OR IGNORE INTO sku_excluded_pairs(pair_a,pair_b,saved_at,machine_id)'
                    ' VALUES(?,?,?,?)',
                    (left, right, self.seen_at, source))

    def _retire_groups(self, rows):
        self._retire([('group', row) for row in rows])

    def _retire(self, stale):
        """让位：删掉同一台机器先前的话（旧组、旧排除身份或旧排除对）。"""
        for kind, row in stale:
            if kind == 'group':
                self.conn.execute('DELETE FROM sku_manual_groups WHERE members=?',
                                  (_sku_group_key(row['members']),))
            elif kind == 'sku':
                self.conn.execute('DELETE FROM sku_excluded_skus WHERE identity=?', (row[0],))
            else:
                self.conn.execute('DELETE FROM sku_excluded_pairs WHERE pair_a=? AND pair_b=?',
                                  (row[0], row[1]))

    def _adopt(self, table, statement, values):
        self.adopted[table] += self.conn.execute(statement, values).rowcount

    def _conflict_kinds(self, opponents, incoming):
        """按冲突类分组各记一条；同一条重复遇到只核对不重记。"""
        for kind in dict.fromkeys(kind for kind, _ in opponents):
            local_contents = sorted((content for own_kind, content in opponents if own_kind == kind),
                                    key=lambda content: json.dumps(content, sort_keys=True,
                                                                   ensure_ascii=False))
            self._conflict(kind, incoming, local_contents)

    def _conflict(self, kind, incoming, local_contents):
        conflict_digest, recorded = _insert_sku_conflict(
            self.conn, self.known, kind=kind, incoming=incoming,
            local_contents=local_contents, seen_at=self.seen_at)
        self.conflicts.append(Conflict(
            digest=conflict_digest, kind=kind,
            members=tuple(sorted({member for content in [incoming, *local_contents]
                                  for member, _ in content['members']})),
            incoming_machine=incoming['machine'], incoming=incoming,
            local_machines=tuple(sorted({content['machine'] for content in local_contents})),
            local=tuple(local_contents), seen_at=self.seen_at, recorded=recorded))
