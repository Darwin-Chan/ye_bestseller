"""分析草稿的持久化：一次保存就是一个完整版本，读回不依赖旧进程的内存。

草稿库是独立文件：直接写采集库会越过「分析只读库存」的边界，
也让采集端的迁移与分析的保存周期互相牵制。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

SCHEMA_VERSION = 1


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
        return conn

    def write(self, analysis_id, start, end, saved_at, payload):
        document = json.dumps(payload, ensure_ascii=False)
        conn = self._connect()
        try:
            # 单事务：中途失败整体回滚，上一个成功版本原样可读。
            with conn:
                conn.execute("""INSERT INTO analysis_drafts(id,saved_at,schema_version,start,end,payload)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET saved_at=excluded.saved_at,
                    schema_version=excluded.schema_version, start=excluded.start,
                    end=excluded.end, payload=excluded.payload""",
                    (analysis_id, saved_at, SCHEMA_VERSION, start, end, document))
        finally:
            conn.close()

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
