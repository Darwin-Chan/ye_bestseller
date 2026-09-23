"""纯解析函数：数字清洗、店铺商品链接、SKU 可见文本解析。

注：1688 页面结构会变化，解析失败时上层会把原始 HTML 存档并标记「需人工」，
据此校准本文件的规则即可，无需改其他模块。
"""
from __future__ import annotations

import html as _html
import json
import hashlib
import math
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


def parse_price(value: object) -> float | None:
    """解析价格；保留合法的 0，拒绝负数、布尔值和非有限数。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    m = re.search(r"(?<![-\d.])\d+(?:\.\d+)?", text.replace(",", "").replace("，", ""))
    if not m:
        return None
    parsed = float(m.group(0))
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def parse_stock(value: object) -> int | None:
    """把 '6486515个' / '64.8万' / '1,234' 转成整数；失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        if not math.isfinite(parsed) or parsed < 0 or not parsed.is_integer():
            return None
        return int(parsed)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    t = text.replace(",", "").replace("，", "")
    m = re.search(r"(?<![-\d.])(\d+(?:\.\d+)?)\s*(万|亿)?", t)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "万":
        val *= 10_000
    elif unit == "亿":
        val *= 100_000_000
    return int(val) if math.isfinite(val) and val >= 0 else None


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


# 单规格商品的内部 SKU 标识：平台不分配 SKU 编号，整件商品记一条默认行。
DEFAULT_SKU_ID = "default"
DEFAULT_SKU_NAME = "默认(单规格)"

_SKU_MAP_EMPTY_RE = re.compile(r'"skuInfoMap"\s*:\s*\[\s*\]')
_IS_SKU_OFFER_RE = re.compile(r'"isSkuOffer"\s*:\s*(true|false)', re.I)
_SKU_TRADE_SUPPORTED_RE = re.compile(r'"skuTradeSupported"\s*:\s*(true|false)', re.I)


def _extract_json_object(html_text: str, key: str) -> dict | None:
    """取出页面内嵌 JSON 中 key 对应的对象；缺失、非对象或解析失败都返回 None。"""
    m = re.search(r'"%s"\s*:\s*(\{)' % re.escape(key), html_text)
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


def _extract_json_array(html_text: str, key: str) -> list | None:
    """取出页面内嵌 JSON 中 key 对应的数组；缺失、非数组或解析失败都返回 None。"""
    m = re.search(r'"%s"\s*:\s*(\[)' % re.escape(key), html_text)
    if not m:
        return None
    try:
        value = json.JSONDecoder().raw_decode(html_text, m.start(1))[0]
    except Exception:  # noqa: BLE001 —— 解析不了按「没有这段」处理
        return None
    return value if isinstance(value, list) else None


def _sku_image_map(html_text: str) -> dict[str, str]:
    """「规格值名 → 图地址」映射，取自内嵌 JSON skuProps 各规格值的 imageUrl。

    取页面首个 `"skuProps"`（实测页里同一份数组出现多次、内容相同，与 skuInfoMap 的
    取法一致）；若首个是空占位，映射为空、按空值处理，不报错。图是附加字段：单个规格
    结构畸形只跳过它，其余映射照常、也不上抛异常。
    """
    out: dict[str, str] = {}
    for prop in _extract_json_array(html_text, "skuProps") or []:
        if not isinstance(prop, dict):
            continue
        values = prop.get("value")
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            name = str(value.get("name") or "").strip()
            image = value.get("imageUrl")
            if name and isinstance(image, str) and image.strip():
                out[name] = image.strip()
    return out


def _sku_image_for(sku_name: str, image_map: dict[str, str]) -> str | None:
    """SKU 的图地址：specAttrs 按 `>` 分段（先解 HTML 转义），逐段与规格值名精确匹配。

    命中段的图地址即该 SKU 的图；规格值名自带 #C0WEK# 内部码，同名色靠它区分。
    """
    for seg in _html.unescape(sku_name).split(">"):
        seg = seg.strip()
        if seg and seg in image_map:
            return image_map[seg]
    return None


def is_single_spec_offer(html_text: str) -> bool:
    """页面是否表明这是不使用 SKU 交易的单规格商品。

    判定要求平台标记 isSkuOffer=false 且页面显式给出空的 skuInfoMap；
    skuTradeSupported=false 作佐证，平台自述支持 SKU 交易却给不出明细时按页面
    没渲染完处理。明细键完全缺失同样不算单规格——那属于页面结构变化，由上层
    记失败并留档。页面上 isSkuOffer 可能出现多次，取值不一致的页面按非单规格处理。
    """
    flags = {v.lower() for v in _IS_SKU_OFFER_RE.findall(html_text)}
    if flags != {"false"}:
        return False
    if _SKU_MAP_EMPTY_RE.search(html_text) is None:
        return False
    corroboration = {v.lower() for v in _SKU_TRADE_SUPPORTED_RE.findall(html_text)}
    return not (corroboration - {"false"})


def _single_spec_price(trade_model: dict) -> float | None:
    """单规格商品的价格：优先商品级到手价，再退到当前区间价的首档。"""
    price = parse_price(trade_model.get("priceDisplay"))
    if price is not None:
        return price
    price_model = trade_model.get("offerPriceModel")
    if isinstance(price_model, dict):
        current = price_model.get("currentPrices")
        if isinstance(current, list) and current and isinstance(current[0], dict):
            return parse_price(current[0].get("price"))
    return None


def _extract_default_sku(html_text: str) -> list[dict]:
    """单规格商品：整件商品按一条默认 SKU 行记录。

    库存取商品级可售量 canBookedAmount（不取 canBookedAmountOriginal），价格取
    商品级到手价，口径与多规格 SKU 取 discountPrice 一致。缺可售量时返回空表，
    由上层按不完整库存观测记失败，不写空库存的成功行。
    """
    if not is_single_spec_offer(html_text):
        return []
    trade_model = _extract_json_object(html_text, "tradeModel") or {}
    stock = parse_stock(trade_model.get("canBookedAmount"))
    if stock is None:
        return []
    return [{
        "sku_id": DEFAULT_SKU_ID,
        "sku_name": DEFAULT_SKU_NAME,
        "sku_price": _single_spec_price(trade_model),
        "sku_stock": stock,
        "sku_image_url": None,   # 单规格商品没有规格值可配图，空值交给上层代填
    }]


def extract_skus_from_html(html_text: str) -> list[dict]:
    """优先解析内嵌 JSON skuInfoMap（含 canBookCount 真实可售库存）；失败再走可见文本，
    最后对单规格商品取商品级可售量。返回 [{sku_id, sku_name, sku_price, sku_stock,
    sku_image_url}]；图地址是附加字段，取不到一律 None，不影响库存判定。
    """
    rows: list[dict] = []
    obj = _extract_json_object(html_text, "skuInfoMap")
    if obj:
        image_map = _sku_image_map(html_text)
        for key, val in obj.items():
            if not isinstance(val, dict):
                continue
            name = str(val.get("specAttrs") or key).strip() or str(key).strip()
            price_value = val.get("discountPrice")
            if price_value is None:
                price_value = val.get("price")
            price = parse_price(price_value)
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
                "sku_image_url": _sku_image_for(name, image_map),
            })
    if rows:
        return rows
    # 兜底：可见文本 / DOM 结构
    for r in extract_sku_rows(html_text):
        r["sku_id"] = hashlib.sha1(r["sku_name"].encode("utf-8")).hexdigest()[:16]
        r["sku_image_url"] = None   # 文本行没有规格值来源，图一律空
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
