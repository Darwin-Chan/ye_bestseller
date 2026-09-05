"""诊断：查看详情弹窗被误判为“需要人工介入”时的真实状态（URL/punish/captcha/resolved/body）。"""
from __future__ import annotations

import sys
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
from bestseller_monitor import browser_pw, sound

sound.configure(False)


def click_card(page, img, cfg):
    with page.expect_popup(timeout=4000) as pi:
        img.evaluate(
            """el => { let t=el; for(let i=0;i<6&&t;i++){
              const st=window.getComputedStyle(t);
              if(t.tagName==='A'||t.onclick||t.getAttribute('href')||st.cursor==='pointer'){
                t.click(); return true; } t=t.parentNode; } el.click(); return true; }"""
        )
    return pi.value


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv
    shop_key = "A02"
    if "--shop" in argv:
        shop_key = argv[argv.index("--shop") + 1]
    cfg = Config.from_file(REPO / "config/config.toml", root=REPO)
    shops = {s.key: s for s in load_shops(cfg.shop_csv)}
    shop = shops[shop_key]
    pw, br, page, ctx = browser_pw.open_session(cfg)
    try:
        page.goto(shop.url, wait_until="domcontentloaded")
        time.sleep(4)
        browser_pw._click_text_in_frames(page, "销量")
        time.sleep(3)
        for _ in range(8):
            page.mouse.wheel(0, 6000)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1.0)
        n = page.locator(browser_pw._PRODUCT_IMG_SEL).count()
        print(f"cards={n}")
        for i in range(min(8, n)):
            img = page.locator(browser_pw._PRODUCT_IMG_SEL).nth(i)
            try:
                popup = click_card(page, img, cfg)
                if popup:
                    popup.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
                    url = popup.url or ""
                    print(f"[{i}] popup.url={url[:95]}")
                    print(f"      is_punish={browser_pw._is_punish_url(url)} "
                          f"captcha_visible={browser_pw._captcha_visible(popup)} "
                          f"resolved={browser_pw._resolved(popup)} "
                          f"intervention={browser_pw.intervention_kind(popup, False)}")
                    body = browser_pw._body_text(popup)
                    print(f"      body[:140]={body[:140]!r}")
                    try:
                        popup.close()
                    except Exception:
                        pass
            except Exception as exc:
                if "/offer/" in (page.url or ""):
                    print(f"[{i}] same-tab page.url={page.url[:95]} "
                          f"captcha_visible={browser_pw._captcha_visible(page)} "
                          f"resolved={browser_pw._resolved(page)}")
                    try:
                        page.go_back(wait_until="domcontentloaded", timeout=30000)
                        time.sleep(1)
                    except Exception:
                        pass
                else:
                    print(f"[{i}] click fail {type(exc).__name__} page.url={page.url[:95]}")
    finally:
        browser_pw.close_session(pw, br)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
