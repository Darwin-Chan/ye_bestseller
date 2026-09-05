"""纯解析函数：数字清洗、店铺商品链接、SKU 可见文本解析。

注：1688 页面结构会变化，解析失败时上层会把原始 HTML 存档并标记「需人工」，
据此校准本文件的规则即可，无需改其他模块。
"""
from __future__ import annotations

import html as _html
import json
import hashlib
import re

OFFER_HREF_RE = re.compile(r"""href=["']([^"']*?(?:/offer/|/item/)(\d+)\.html[^"']*)["']""", re.I)
PRICE_RE = re.compile(r"([¥￥]\s*\d+(?:\.\d+)?)")

# 匹配形如： 小号#C0615# ¥0.18 库存 6486515个 / 大号 ¥0.03 库存:747155
SKU_ROW_RE = re.compile(
    r"(?P<name>[^¥￥\n\r]{1,160}?)[ \t]+[¥￥][ \t]*(?P<price>\d+(?:\.\d+)?)"
    r"[ \t]*(?:库存|现货|库存量|stock)[:：]?[ \t]*(?P<stock>\d[\d,，]*(?:\s*万|\s*亿)?)",
    re.I,
)

TITLE_RE = re.compile(r"<title[^>]*>\s*(.*?)\s*</title>", re.I | re.S)
OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', re.I
)


def strip_tags(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(div|p|li|tr|span|h\d)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return _html.unescape(text)


def extract_offer_links(html_text: str) -> list[tuple[str, str]]:
    """从 HTML 中按出现顺序提取 (offer_id, url)，保持先后顺序并去重。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in OFFER_HREF_RE.finditer(html_text):
        oid = m.group(2)
        if oid in seen:
            continue
        seen.add(oid)
        url = _html.unescape(m.group(1))
        out.append((oid, url))
    return out


def extract_title(html_text: str) -> str | None:
    for pat in (OG_TITLE_RE, TITLE_RE):
        m = pat.search(html_text)
        if m:
            t = strip_tags(m.group(1)).strip()
            if t:
                return t[:300]
    return None


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", "").replace("，", ""))
    return float(m.group(0)) if m else None


def parse_stock(text: str | None) -> int | None:
    """把 '6486515个' / '64.8万' / '1,234' 转成整数；失败返回 None。"""
    if not text:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    t = text.replace(",", "").replace("，", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|亿)?", t)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "万":
        val *= 10_000
    elif unit == "亿":
        val *= 100_000_000
    return int(val)


def extract_sku_rows(html_text: str) -> list[dict]:
    """从页面可见文本中尽量提取 SKU 名称/价格/库存。返回空表说明需要校准。"""
    text = strip_tags(html_text)
    rows: list[dict] = []
    seen: set[tuple[str, float, int]] = set()
    for m in SKU_ROW_RE.finditer(text):
        name = m.group("name").strip()
        price = parse_price(m.group("price"))
        stock = parse_stock(m.group("stock"))
        if not name or price is None or stock is None:
            continue
        key = (name, price, stock)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"sku_name": name, "sku_price": price, "sku_stock": stock})
    return rows


SKU_MAP_RE = re.compile(r'"skuInfoMap"\s*:\s*(\{)')

_DEFAULT_PRICE_PATS = (
    r'"price"\s*:\s*"?(\d+(?:\.\d+)?)',
    r'"discountPrice"\s*:\s*"?(\d+(?:\.\d+)?)',
    r'"salePrice"\s*:\s*"?(\d+(?:\.\d+)?)',
    r'"minPrice"\s*:\s*"?(\d+(?:\.\d+)?)',
)
# 只认明确的“可售库存”字段，避免把 orderAmount/起订量 误当库存
_DEFAULT_STOCK_PATS = (
    r'"canBookCount"\s*:\s*"?(\d+)',
    r'"availableStock"\s*:\s*"?(\d+)',
    r'"quantity"\s*:\s*"?(\d+)',
    r'"inventory"\s*:\s*"?(\d+)',
)


def _extract_default_sku(html_text: str) -> list[dict]:
    """单规格/无 skuInfoMap 时，从商品级字段取一条默认价格/库存。"""
    price = None
    stock = None
    for pat in _DEFAULT_PRICE_PATS:
        m = re.search(pat, html_text, re.I)
        if m:
            price = parse_price(m.group(1))
            break
    for pat in _DEFAULT_STOCK_PATS:
        m = re.search(pat, html_text, re.I)
        if m:
            stock = parse_stock(m.group(1))
            break
    if price is None and stock is None:
        return []
    return [{
        "sku_id": "default",
        "sku_name": "默认(单规格)",
        "sku_price": price,
        "sku_stock": stock,
    }]


def _extract_sku_map_json(html_text: str) -> dict | None:
    m = SKU_MAP_RE.search(html_text)
    if not m:
        return None
    body = html_text[m.start(1):]
    depth = 0
    end = 0
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        obj = json.loads(body[:end] if end else body)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def extract_skus_from_html(html_text: str) -> list[dict]:
    """优先解析内嵌 JSON skuInfoMap（含 canBookCount 真实可售库存）；失败再走可见文本。
    返回 [{sku_id, sku_name, sku_price, sku_stock}]。
    """
    rows: list[dict] = []
    obj = _extract_sku_map_json(html_text)
    if obj:
        for key, val in obj.items():
            if not isinstance(val, dict):
                continue
            name = str(val.get("specAttrs") or key).strip() or str(key).strip()
            price = parse_price(val.get("discountPrice") or val.get("price"))
            stock = val.get("canBookCount")
            if stock is None:
                stock = val.get("quantity")
            stock = parse_stock(stock) if stock is not None else None
            sku_id = val.get("skuId")
            if sku_id is None:
                sku_id = hashlib.sha1(f"{name}".encode("utf-8")).hexdigest()[:16]
            rows.append({
                "sku_id": str(sku_id),
                "sku_name": name,
                "sku_price": price,
                "sku_stock": stock,
            })
    if rows:
        return rows
    # 兜底：可见文本 / DOM 结构
    for r in extract_sku_rows(html_text):
        r["sku_id"] = hashlib.sha1(r["sku_name"].encode("utf-8")).hexdigest()[:16]
        rows.append(r)
    if not rows:
        # 再兜底：单规格/无 skuInfoMap 时取商品级默认价格/库存
        rows = _extract_default_sku(html_text)
    return rows


def extract_main_image(html_text: str) -> str | None:
    """提取商品首图 URL：优先 imageList[0].fullPathImageURI，再 mainImage、og:image。"""
    pats = (
        r'"imageList"\s*:\s*\[\s*\{\s*"fullPathImageURI"\s*:\s*"([^"]+)"',
        r'"imageList"\s*:\s*\[\s*\{\s*"url"\s*:\s*"([^"]+)"',
        r'"mainImage"\s*:\s*\[\s*"([^"]+)"',
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    )
    for pat in pats:
        m = re.search(pat, html_text, re.I)
        if m:
            return m.group(1).replace("\\/", "/")
    return None
