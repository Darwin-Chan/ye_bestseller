"""诊断：店铺商品列表页的商品卡片 DOM 结构（临时工具）。

浏览器会话走 `browser_pw.open_session` / `close_session`（候选 03 / ADR-0019）。
"""
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from bestseller_monitor import browser_pw
from bestseller_monitor.config import Config

URL = "https://yipihuo8.1688.com/page/offerlist.htm"

_cfg = Config.from_file(_REPO / "config" / "config.toml", root=_REPO)


def main():
    pw, br, page, ctx = browser_pw.open_session(_cfg)
    try:
        _diagnose(page)
    finally:
        browser_pw.close_session(pw, br)


def _diagnose(page) -> None:
    pun = [False]

    def onr(resp):
        try:
            if resp.request.resource_type in ("xhr", "fetch") and any(
                k in resp.url.lower() for k in ("punish", "x5sec", "nocaptcha", "secaptcha")
            ):
                pun[0] = True
        except Exception:
            pass

    page.on("response", onr)
    page.goto(URL, wait_until="domcontentloaded")
    time.sleep(6)
    print("punished?", pun[0])
    print(">>> 若有滑块请在 Edge 窗口滑动；等待 40 秒……")
    time.sleep(40)
    try:
        page.reload(wait_until="domcontentloaded")
        time.sleep(5)
    except Exception as e:
        print("reload err", e)

    info = page.evaluate(
        """() => {
          const imgs=[...document.querySelectorAll('img')];
          const brands=[...document.querySelectorAll('[class*="offer"],[class*="card"],[class*="item"],[class*="goods"],[class*="product"]')];
          const t=[];
          imgs.slice(0,30).forEach(im=>{
            let el=im, chain=[];
            for(let i=0;i<5&&el;i++){chain.push(el.tagName+'.'+String(el.className).slice(0,40)); el=el.parentElement;}
            const a=im.closest('a'); const data=Object.keys(im.dataset||{});
            t.push({alt:(im.alt||'').slice(0,30), hasA: !!a, aHref: a?(a.href||'').slice(0,60):'', data});
          });
          return {imgCount: imgs.length, brandCount: brands.length, sample: t.slice(0,12)};
        }"""
    )
    print(json.dumps(info, ensure_ascii=False)[:2500])


if __name__ == "__main__":
    main()
