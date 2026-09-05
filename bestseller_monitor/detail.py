"""商品详情页抓取。"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .delay import Humanizer
from .guard import detect, wait_for_human
from .parse import extract_skus_from_html, extract_title

log = logging.getLogger(__name__)


class DetailParseFailed(Exception):
    """详情页 SKU 结构无法解析，需要人工校准。"""

    def __init__(self, message: str, html: str = ""):
        super().__init__(message)
        self.html = html


def capture_detail_payload(page, product_url: str, cfg: Config, human: Humanizer) -> dict:
    """打开详情页并返回解析结果；解析失败抛出 DetailParseFailed。"""
    page.goto(product_url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
    human.after_load()
    kind = detect(page)
    if kind:
        wait_for_human(page, kind, cfg.human_pause_minutes)
    html = page.content()
    rows = extract_skus_from_html(html)
    if not rows:
        raise DetailParseFailed(f"详情页未解析到 SKU：{product_url}", html=html)
    return {
        "product_name": extract_title(html) or "",
        "html": html,
        "rows": rows,
    }


def save_raw_page(cfg: Config, round_id: int, offer_id: str, html: str) -> Path:
    d = cfg.raw_page_dir / f"round_{round_id}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{offer_id}.html"
    path.write_text(html, encoding="utf-8")
    return path
