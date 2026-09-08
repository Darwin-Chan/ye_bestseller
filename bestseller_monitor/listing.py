"""店铺商品列表抓取：销量排序 + 每店最多前 N 页。"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

from .config import Config, Shop
from .delay import Humanizer
from .guard import detect, wait_for_human
from .parse import extract_offer_links

log = logging.getLogger(__name__)

Offer = tuple[int, str, str, str, str]  # rank, offer_id, product_url, list_title, list_price


class ListingLoadFailed(Exception):
    """列表无法确认可用，不能把空结果写成完成榜单。"""

    def __init__(self, message: str, html: str = ""):
        super().__init__(message)
        self.html = html


class ListingCalibrationRequired(ListingLoadFailed):
    """列表页结构与预期不符，需要人工校准。"""


def _try_click_sales_sort(page) -> bool:
    """尽力点击「销量」排序；找不到时返回 False 并继续用默认排序。"""
    for label in ("按销量", "销量", "成交"):
        try:
            loc = page.get_by_text(label, exact=True)
            if loc.count() > 0:
                cand = loc.first
                if cand.is_visible():
                    cand.click(timeout=5000)
                    log.info("已点击排序入口：%s", label)
                    return True
        except Exception:
            continue
    log.warning("未找到「销量」排序入口，将按页面默认顺序抓取（首轮请人工确认）。")
    return False


def _next_page_available(page) -> bool:
    try:
        cand = page.get_by_text("下一页", exact=True)
        if cand.count() == 0:
            return False
        nxt = cand.first
        if not nxt.is_visible():
            return False
        disabled = nxt.get_attribute("disabled")
        return disabled is None
    except Exception:
        return False


def save_raw_listing_page(cfg: Config, round_id: int, shop_key: str, html: str) -> Path:
    """存档失败列表页，供选择器或页面结构校准。"""
    d = cfg.raw_page_dir / f"round_{round_id}"
    d.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", shop_key)
    path = d / f"listing_{safe_key}.html"
    path.write_text(html, encoding="utf-8")
    return path


def crawl_shop_listing(
    page,
    shop: Shop,
    cfg: Config,
    human: Humanizer,
    check_guard: Callable = detect,
) -> tuple[list[Offer], int]:
    """抓取一家店铺的前 max_pages 页商品，返回 (offers, pages_read)。"""
    log.info("开始抓取店铺 %s（%s）", shop.key, shop.url)
    page.goto(shop.url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
    human.after_load()
    kind = check_guard(page)
    if kind:
        wait_for_human(page, kind, cfg.human_pause_minutes)

    _try_click_sales_sort(page)

    offers: list[Offer] = []
    seen: set[str] = set()
    pages_read = 0

    for _ in range(cfg.max_pages_per_shop):
        pages_read += 1
        human.before_list_page()
        kind = check_guard(page)
        if kind:
            wait_for_human(page, kind, cfg.human_pause_minutes)

        try:
            items = page.evaluate(
                """() => Array.from(
                    document.querySelectorAll("a[href*='/offer/'], a[href*='/item/']")
                ).map(a => ({
                    href: a.href,
                    text: (a.innerText || a.textContent || '').trim().slice(0, 240)
                })).filter(x => /\\/offer\\/\\d+\\.html/.test(x.href) || /\\/item\\/\\d+\\.html/.test(x.href))"""
            )
        except Exception as exc:  # pragma: no cover - 只在真机触发
            html = page.content()
            items = [
                {"href": url, "text": ""}
                for _oid, url in extract_offer_links(html)
            ]
            if not items:
                raise ListingCalibrationRequired(f"店铺列表无法解析：{shop.url}（{exc}）", html=html)

        added = 0
        for item in items:
            href = str(item.get("href") or "")
            m = re.search(r"/(?:offer|item)/(\d+)\.html", href)
            if not m:
                continue
            oid = m.group(1)
            if oid in seen:
                continue
            seen.add(oid)
            offers.append((len(offers) + 1, oid, href, str(item.get("text") or ""), ""))
            added += 1
        log.info("店铺 %s 第 %s 页新增 %s 个商品，累计 %s 个", shop.key, pages_read, added, len(offers))

        if pages_read >= cfg.max_pages_per_shop:
            break
        if not _next_page_available(page):
            log.info("店铺 %s 已无下一页，提前结束（共 %s 页）", shop.key, pages_read)
            break
        try:
            page.get_by_text("下一页", exact=True).first.click(timeout=cfg.timeout_ms)
            page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
        except Exception as exc:
            log.warning("点击下一页失败：%s", exc)
            break

    if not offers:
        try:
            html = page.content()
        except Exception:
            html = ""
        raise ListingLoadFailed(
            f"店铺列表未解析到商品：{shop.url}（current_url={getattr(page, 'url', '')}）",
            html=html,
        )
    return offers, pages_read
