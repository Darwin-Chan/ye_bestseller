"""榜单失败：异常与原始页存档。

采集驱动只有一条（Playwright 连接接管 + 点击式列表，见 ADR-0010），
列表逻辑本身在 browser_pw.py。这里只剩两件事：
把「榜单没拿到」表示成一个可捕获的异常，以及把出错的页面存下来供校准。
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import Config


class ListingLoadFailed(Exception):
    """列表无法确认可用，不能把空结果写成完成榜单。"""

    def __init__(self, message: str, html: str = ""):
        super().__init__(message)
        self.html = html


def save_raw_listing_page(cfg: Config, round_id: int, shop_key: str, html: str) -> Path:
    """存档失败列表页，供选择器或页面结构校准。"""
    d = cfg.raw_page_dir / f"round_{round_id}"
    d.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", shop_key)
    path = d / f"listing_{safe_key}.html"
    path.write_text(html, encoding="utf-8")
    return path
