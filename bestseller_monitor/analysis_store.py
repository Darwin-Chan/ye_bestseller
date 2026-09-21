"""分析草稿的持久化：一次保存就是一个完整版本，读回不依赖旧进程的内存。

草稿库是独立文件：直接写采集库会越过「分析只读库存」的边界，
也让采集端的迁移与分析的保存周期互相牵制。

人工决策账本（确认的同款关系、独立确认、排除关系）与草稿同一事务落盘：
「已成功保存的人工判断」是它们唯一的持久来源，新日期区间据此复用（票 09）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

SCHEMA_VERSION = 1

# 账本三张表：关系按成员清单整行存（帶确认状态），独立确认与排除按主键逐行存。
# 保存时整版覆盖：一次保存就是一个完整的人工决策版本。
_DECISION_TABLES = {
    'manual_relations': """CREATE TABLE IF NOT EXISTS manual_relations (
        members TEXT PRIMARY KEY, confirmed INTEGER NOT NULL, saved_at TEXT NOT NULL)""",
    'manual_standalone': """CREATE TABLE IF NOT EXISTS manual_standalone (
        identity TEXT PRIMARY KEY, version TEXT NOT NULL, saved_at TEXT NOT NULL)""",
    'manual_exclusions': """CREATE TABLE IF NOT EXISTS manual_exclusions (
        pair_a TEXT NOT NULL, pair_b TEXT NOT NULL, saved_at TEXT NOT NULL,
        PRIMARY KEY(pair_a, pair_b))""",
}


class StoredDraft(NamedTuple):
    payload: dict
    saved_at: str


class DraftStore:
    def __init__(self, path: Path):
        self.path = Path(path)

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
        for statement in _DECISION_TABLES.values():
            conn.execute(statement)
        return conn

    def write(self, analysis_id, start, end, saved_at, payload, ledger):
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
                for table in _DECISION_TABLES:
                    conn.execute(f'DELETE FROM {table}')
                conn.executemany('INSERT INTO manual_relations(members,confirmed,saved_at) VALUES(?,?,?)',
                    [(json.dumps([[member, member_version] for member, member_version in relation['members']],
                                 ensure_ascii=False), relation['confirmed'], saved_at)
                     for relation in ledger['relations']])
                conn.executemany('INSERT INTO manual_standalone(identity,version,saved_at) VALUES(?,?,?)',
                    [(member, member_version, saved_at) for member, member_version in ledger['standalone']])
                conn.executemany('INSERT INTO manual_exclusions(pair_a,pair_b,saved_at) VALUES(?,?,?)',
                    [(pair[0], pair[1], saved_at) for pair in ledger['excluded']])
        finally:
            conn.close()

    def ledger(self):
        """已保存的人工决策账本；从未保存过时为空。

        只有「文件或表不存在」能当作没保存过：保存会整版覆盖账本，把读错误
        当成空白会把历史判断抹掉，所以其余读取错误照常抛出。
        """
        empty = {'relations': [], 'standalone': [], 'excluded': []}
        if not self.path.exists():
            return empty
        conn = sqlite3.connect(self.path)
        try:
            try:
                relations = [{'members': json.loads(row[0]), 'confirmed': bool(row[1])}
                             for row in conn.execute('SELECT members,confirmed FROM manual_relations')]
                standalone = [[row[0], row[1]] for row in conn.execute('SELECT identity,version FROM manual_standalone')]
                excluded = [[row[0], row[1]] for row in conn.execute('SELECT pair_a,pair_b FROM manual_exclusions')]
            except sqlite3.OperationalError as exc:
                if 'no such table' not in str(exc):
                    raise
                return empty
        finally:
            conn.close()
        return {'relations': relations, 'standalone': standalone, 'excluded': excluded}

    def latest(self):
        if not self.path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute("""SELECT id,start,end,saved_at,schema_version
                FROM analysis_drafts ORDER BY saved_at DESC, rowid DESC LIMIT 1""").fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def read(self, analysis_id):
        if not self.path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM analysis_drafts WHERE id=?", (analysis_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        if row['schema_version'] != SCHEMA_VERSION:
            # 报错而不是清空：旧草稿由用户决定怎么处理，程序不代它丢数据。
            raise ValueError("上次保存的分析草稿来自不兼容的版本，未做任何修改；请升级程序后再打开")
        return StoredDraft(json.loads(row['payload']), row['saved_at'])
