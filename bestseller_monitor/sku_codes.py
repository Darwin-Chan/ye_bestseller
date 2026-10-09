"""Merchant codes are frozen SKU evidence, never member identities."""
from __future__ import annotations

import re

RULE_VERSION = "sku-merchant-codes-v1"
_FRAGMENTS = re.compile(r"[#＃]([^#＃]*)[#＃]")


def _normalize(value: str) -> str:
    # Only English letter case is equivalent; do not fold other characters.
    return value.strip().translate(str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))


def code_evidence(member: dict) -> dict:
    raw = member.get("raw_sku_name") or ""
    real = bool(raw.strip()) and member.get("name_source") != "system_default"
    source = "raw_sku_name"
    text = raw
    if member.get("single_sku") is True and not real:
        source, text = "product_name", member.get("product_name") or ""
    values = sorted({_normalize(match.group(1)) for match in _FRAGMENTS.finditer(text)} - {""})
    return dict(merchant_code=values[0] if len(values) == 1 else None,
                merchant_codes=values, code_ambiguous=len(values) > 1,
                code_source=source, evidence_rule=RULE_VERSION)


def _semantic_name(text: str) -> str:
    return _FRAGMENTS.sub(lambda match: "#" + _normalize(match.group(1)) + "#" if _normalize(match.group(1)) else match.group(0), text or "")


def matching_evidence(member: dict) -> dict:
    """Material evidence only: time, stock and spelling-equivalent codes are not changes."""
    codes = code_evidence(member)
    return {"商品名称": _semantic_name(member.get("product_name")),
            "SKU 名称": _semantic_name(member.get("raw_sku_name")),
            "商家编码": (codes["merchant_codes"], codes["code_source"]),
            "SKU 图片": member.get("image_hash"),
            "图片来源": member.get("image_source"),
            "单 SKU 匹配字段": member.get("single_sku"),
            "名称来源": member.get("name_source")}


def changed_evidence(before: dict, after: dict) -> list[str]:
    previous, current = matching_evidence(before), matching_evidence(after)
    return [field for field in current if current[field] != previous[field]]
