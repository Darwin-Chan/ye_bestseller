"""身份表取胜方账（`merge_seen`）的行键格式：一处写死，写入侧与过滤侧共用。

`merge_seen.row_key` 把身份表的主键拼成 "a\x1fb" 形状（单列主键就是它自己）。
两个消费者必须同源，否则静默错滤：

- 写入侧：`merge._merge_identity` 导入时记「这条描述列是谁写的」；
- 过滤侧：`export` 的「只发自己那份」按它认账（账说归别人的行不进本机周包）。

SQL 表达式是给过滤侧用的（`char(31)` 由分隔符码点现推，不手抄）；写入侧的 Python
拼法走 `identity_row_key`。主键列都 NOT NULL，所以 SQL 里 NULL 传染的差别不会发生。
（同款先例：`canonical_text`——为两处共用一处口径而生。）
"""
from __future__ import annotations

from collections.abc import Iterable

IDENTITY_ROW_KEY_SEP = "\x1f"


def identity_row_key(values: Iterable) -> str:
    """身份表主键 → 账里的行键（与 `identity_row_key_sql` 同源）。"""
    return IDENTITY_ROW_KEY_SEP.join(str(value) for value in values)


def identity_row_key_sql(columns: Iterable[str]) -> str:
    """行键的 SQL 表达式（给 SQL 侧的过滤用）；`columns` 是带表别名的列名。"""
    separator = f" || char({ord(IDENTITY_ROW_KEY_SEP)}) || "
    return separator.join(columns)
