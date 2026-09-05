"""一次性工具：把某店 inventory 里的 product_name 刷新为“列表页卡片标题”。

修复早期版本库存里存的是“详情页标题”、与卡片标题不一致导致的按名跳过失配。
只更新今天的 inventory，不新增轮次/快照。

用法：python tools/sync_list_titles.py [--shop A02] [--pages N]
"""
from __future__ import annotations

import sys
import re
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bestseller_monitor.config import Config, load_shops
from bestseller_monitor.db import Database, connect, cst_date
from bestseller_monitor.delay import Humanizer
from bestseller_monitor import browser_pw


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv
    shop_key = "A02"
    if "--shop" in argv:
        shop_key = argv[argv.index("--shop") + 1]
    cfg = Config.from_file(REPO / "config/config.toml", root=REPO)
    shops = {s.key: s for s in load_shops(cfg.shop_csv)}
    shop = shops[shop_key]
    conn = connect(cfg.db_file)
    db = Database(conn)
    today = cst_date()

    pw, br, page, ctx = browser_pw.open_session(cfg)
    human = Humanizer(cfg)
    try:
        page.goto(shop.url, wait_until="domcontentloaded")
        time.sleep(4)
        browser_pw._click_text_in_frames(page, "销量")
        time.sleep(3)
        max_pages = int(shop.pages) if shop.pages else int(cfg.max_pages_per_shop)
        mapping: dict[str, str] = {}
        for pg in range(1, max_pages + 1):
            human.before_list_page()
            prev = -1
            for _ in range(12):
                try:
                    page.mouse.wheel(0, 6000)
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(1.2)
                except Exception:
                    break
                cur = page.locator(browser_pw._PRODUCT_IMG_SEL).count()
                if cur == prev:
                    break
                prev = cur
            n = page.locator(browser_pw._PRODUCT_IMG_SEL).count()
            for i in range(n):
                title = browser_pw._read_card_title(page, i)
                img = page.locator(browser_pw._PRODUCT_IMG_SEL).nth(i)
                detail_page, popup = browser_pw._click_one_product(
                    page, img, cfg, [False], lambda r: None
                )
                if detail_page is None:
                    continue
                m = re.search(r"/(?:offer|item)/(\d+)\.html", detail_page.url)
                if not m:
                    browser_pw._close_popup_or_back(detail_page, popup, page)
                    continue
                oid = m.group(1)
                mapping.setdefault(oid, title or "")
                browser_pw._close_popup_or_back(detail_page, popup, page)
            if pg >= max_pages:
                break
            if not browser_pw._click_text_in_frames(page, "下一页"):
                if not browser_pw._click_text_in_frames(page, "加载更多"):
                    break
            page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
            time.sleep(2)

        upd = 0
        mapped_titled = 0
        for oid, title in mapping.items():
            if not title:
                continue
            mapped_titled += 1
            cur = conn.execute(
                "UPDATE inventory SET product_name=? "
                "WHERE shop_key=? AND offer_id=? AND date=?",
                (title, shop.key, oid, today),
            )
            upd += cur.rowcount
        conn.commit()
        print(f"shop={shop_key}  点开并拿到卡片标题的 offer 数={mapped_titled} "
              f"（共映射 {len(mapping)}）  成功更新 inventory 行数={upd}  (date={today})")
        print(f"未匹配到卡片标题的 offer，仍保留旧标题，将走 offer_id 兜底。")
    finally:
        browser_pw.close_session(pw, br)
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
