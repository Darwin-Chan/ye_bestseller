"""核对已存档详情页的单规格解析结果（只读，不写任何数据）。

用法：
    python tools/check_single_spec.py           # 默认读 config 里的 raw_page_dir
    python tools/check_single_spec.py <目录>     # 指定原始页目录

按页面标记分组统计：单规格商品应各自解析出一条带库存的默认 SKU 行，多规格商品
应仍按 SKU 明细解析，反爬拦截页不应产出任何库存行。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bestseller_monitor.parse import (  # noqa: E402
    DEFAULT_SKU_ID,
    extract_skus_from_html,
    is_single_spec_offer,
)


def classify(html: str) -> str:
    rows = extract_skus_from_html(html)
    if '"isSkuOffer"' not in html:
        return "反爬拦截页"
    if is_single_spec_offer(html):
        first = rows[0] if rows else {}
        return ("单规格已解析" if first.get("sku_id") == DEFAULT_SKU_ID
                and first.get("sku_stock") is not None else "单规格未解析")
    return "多规格已解析" if rows else "多规格未解析"


def main() -> int:
    if len(sys.argv) > 1:
        root = Path(sys.argv[1])
    else:
        from bestseller_monitor.config import Config
        root = Config.from_file(ROOT / "config" / "config.toml", root=ROOT).raw_page_dir
    if not root.is_dir():
        print(f"原始页目录不存在：{root}")
        return 2

    buckets: dict[str, list[str]] = {}
    for path in sorted(root.glob("round_*/*.html")):
        tag = f"{path.parent.name}/{path.stem}"
        buckets.setdefault(classify(path.read_text(encoding="utf-8", errors="ignore")), []).append(tag)

    for label in sorted(buckets):
        print(f"{label}: {len(buckets[label])}")

    unresolved = buckets.get("单规格未解析", []) + buckets.get("多规格未解析", [])
    if unresolved:
        print("\n未按预期解析的页面：")
        for tag in unresolved:
            print("  ", tag)
        return 1
    print(f"\n全部页面符合预期（目录：{root}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
