"""交换区里文本文件的内容口径：行尾归一与内容哈希。

同一份文件经 Excel、git 检出（core.autocrlf）或编辑器碰过之后，BOM 与 CRLF 会来回变。
「这份文件变没变」在本仓库统一按**归一化文本**判定：行尾一律 \n、去掉末尾空行，
再取 sha256。共享店铺清单（`shops_sync`）与周计划文件（`plan_step`）共用这一处口径——
两边各写一份的话，口径一改就会各改各的。

（字节层的读法见 `config.decode_shops_bytes`：UTF-8 带不带 BOM 都行、失败退 GBK。）
"""
from __future__ import annotations

import hashlib


def normalized_text(text: str) -> str:
    """文本口径的内容：行尾归一、去尾空行。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def text_digest(text: str) -> str:
    """归一化文本的 sha256（小写十六进制）——交换区判定「变没变」的统一口径。"""
    return hashlib.sha256(normalized_text(text).encode("utf-8")).hexdigest()
