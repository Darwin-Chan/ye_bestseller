"""诊断：点进详情前能否拿到 offer_id？

目标：
  A. 列表接口 XHR 是否返回 offerId 数组（且顺序是否对应卡片）。
  B. 卡片 DOM（祖先 a.href / data-* / 整个页面里的 /offer/ 链接）是否暴露 offer_id。
结论用于判断能否把「同日去重跳过」前移到点卡片之前。

用法：python tools/diag_offer_id_presolve.py [店铺URL]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse, parse_qs, unquote

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from playwright.sync_api import sync_playwright

from bestseller_monitor.config import Config, load_shops

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = os.path.abspath("profiles/account1_edge")
PORT = 9222
ROOT = os.path.dirname(REPO)
cfg_timeout = 45000


def pick_url(arg: str | None) -> str:
    if arg:
        return arg
    cfg = Config.from_file(__import__("pathlib").Path(REPO) / "config/config.toml", root=__import__("pathlib").Path(REPO))
    shops = [s for s in load_shops(cfg.shop_csv) if s.active]
    return shops[0].url


def unwrap_json(o):
    if isinstance(o, str):
        try:
            return json.loads(o)
        except Exception:
            return o
    if isinstance(o, dict):
        return {k: unwrap_json(v) for k, v in o.items()}
    if isinstance(o, list):
        return [unwrap_json(v) for v in o]
    return o


def collect_offers(obj, out):
    if isinstance(obj, dict):
        if "id" in obj and ("subject" in obj or "title" in obj):
            out.append(obj)
        for v in obj.values():
            collect_offers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_offers(v, out)


def parse_shop_data(text: str) -> list[dict]:
    obj = text
    if obj.lstrip().startswith("-"):  # jsonp 包装
        m = re.search(r"\(\s*(\{.*\})\s*\)", text, re.S)
        obj = m.group(1) if m else text
    try:
        obj = json.loads(obj)
    except Exception:
        return []
    obj = unwrap_json(obj)
    out: list[dict] = []
    collect_offers(obj, out)
    return out


def main(argv: list[str] | None = None) -> int:
    url = pick_url(argv[1] if argv and len(argv) > 1 else None)
    print("=== 诊断店铺列表 URL ===")
    print(url)

    subprocess.Popen([
        EDGE, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ])
    time.sleep(9)
    pw = sync_playwright().start()
    br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = br.contexts[0]
    page = ctx.new_page()
    page.set_default_timeout(45000)

    offer_ids_xhr: list[dict] = []
    api_bodies: list[dict] = []
    xhr_urls: list[str] = []
    punished = [False]

    def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            u = (resp.url or "").lower()
            if any(k in u for k in ("punish", "x5sec", "nocaptcha", "secaptcha")):
                punished[0] = True
            if u not in xhr_urls:
                xhr_urls.append(u)
            text = resp.text()
            # 兼容 mtop 的转义/双层 JSON
            ids = re.findall(r'offerId[\\"\'"\s:=]+(\d+)', text)
            ids += re.findall(r'/(?:offer|item)/(\d+)', text)
            if ids:
                offer_ids_xhr.append({"url": resp.url[:140], "ids": ids})
            try:
                q = parse_qs(urlparse(resp.url).query)
                api_val = q.get("api", [""])[0]
                component = ""
                data = q.get("data", [""])[0]
                if data:
                    try:
                        component = json.loads(unquote(data)).get("componentkey", "")
                    except Exception:
                        pass
                for a in ("shop.data.get", "moduledata.get", "moduleasyncservice",
                          "offerlist", "offer/list"):
                    if a in api_val:
                        api_bodies.append({"key": a, "component": component,
                                           "url": resp.url[:120], "text": text})
                        break
            except Exception:
                pass
        except Exception:
            pass

    page.on("response", on_response)
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(6)
    if punished[0]:
        print(">>> 检测到滑块/验证，请在 Edge 窗口手动处理（等待 40 秒）……")
        time.sleep(40)
        try:
            page.reload(wait_until="domcontentloaded")
            time.sleep(5)
        except Exception:
            pass

    # 滚动触发懒加载
    prev = -1
    for _ in range(10):
        try:
            page.mouse.wheel(0, 6000)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1.2)
        except Exception:
            break
        cur = page.locator("img.main-picture, img.hover-trigger").count()
        if cur == prev:
            break
        prev = cur
    time.sleep(3)

    html = page.content()
    html_offer_ids = re.findall(r'offerId["\'\s:=]+(\d+)', html)
    html_offer_ids += re.findall(r'/(?:offer|item)/(\d+)', html)
    script_text = page.evaluate(
        """() => Array.from(document.querySelectorAll('script'))
           .map(s => s.textContent || '').join('\\n')"""
    )
    script_offer_ids = re.findall(r'offerId["\'\s:=]+(\d+)', script_text)
    script_offer_ids += re.findall(r'/(?:offer|item)/(\d+)', script_text)

    img_sel_count = page.locator("img.main-picture, img.hover-trigger").count()
    card_info = page.evaluate(
        """() => {
          const imgs=[...document.querySelectorAll('img.main-picture, img.hover-trigger')];
          return imgs.slice(0,30).map(im=>{
            let a=im.closest('a');
            const chain=[]; let el=im;
            for(let i=0;i<6&&el;i++){
              const c=String(el.className||'').slice(0,50);
              chain.push((el.tagName||'')+'.'+c);
              if(el.tagName==='A') break;
              el=el.parentElement;
            }
            let card = im.parentElement;
            let title = '';
            let titleSel = '[class*="title"],[class*="name"],[class*="subject"],[class*="Subject"],[class*="offer-title"],[class*="offerTitle"]';
            let cands = card ? [...card.querySelectorAll(titleSel)] : [];
            let best = cands.map(c=>(c.innerText||c.textContent||'').trim()).filter(Boolean)[0] || '';
            let parentText = (im.parentElement?.innerText||'').trim().slice(0,90);
            let parentText2 = (im.parentElement?.parentElement?.innerText||'').trim().slice(0,90);
            let ancestorTexts = [];
            let e = im;
            for (let i=0;i<10&&e;i++){
              const t = (e.innerText||'').trim();
              if (t) ancestorTexts.push((e.tagName||'')+'.'+(e.className||'').slice(0,30)+' :: '+t.slice(0,70));
              e = e.parentElement;
            }
            return {
              alt:(im.alt||'').slice(0,40),
              aHref: a?(a.getAttribute('href')||'').slice(0,90):'',
              data: {...im.dataset},
              chain: chain,
              title: best.slice(0,60),
              parentText: parentText,
              parentText2: parentText2,
              ancestorTexts: ancestorTexts,
              src:(im.currentSrc||im.src||'').slice(-80)
            };
          });
        }"""
    )
    # 更稳的商品名提取：取“含销量/价格”那个容器 innerText 的首行
    named = page.evaluate(
        """() => {
          const imgs=[...document.querySelectorAll('img.main-picture')];
          return imgs.slice(0,30).map((im,idx)=>{
            let e=im, title='';
            for(let k=0;k<16&&e;k++){
              let imgs=0;
              try{ imgs = e.querySelectorAll ? e.querySelectorAll('img.main-picture').length : 0; }catch(_){}
              if(imgs===1){
                const t=(e.innerText||'').trim();
                if(t){ title=(t.split(/\\n/).map(s=>s.trim()).filter(Boolean)[0]||'').slice(0,90); break; }
              }
              e=e.parentElement;
            }
            return {idx, title, src:(im.currentSrc||im.src||'').slice(-55)};
          });
        }"""
    )
    anchor_hits = page.evaluate(
        """() => {
          const hits=[];
          document.querySelectorAll('a[href*="/offer/"], a[href*="/item/"], a[offerId], [offerId], [data-offer-id], [data-id]').forEach(e=>{
            if(hits.length>=50) return;
            hits.push({
              tag:e.tagName,
              cls:String(e.className||'').slice(0,50),
              href:(e.getAttribute('href')||'').slice(0,90),
              offer:(e.getAttribute('offerId')||e.getAttribute('data-offer-id')||e.getAttribute('data-id')||'')
            });
          });
          return hits;
        }"""
    )

    print("\n=== A. 列表接口 XHR 返回的 offerId ===")
    total_ids = 0
    for x in offer_ids_xhr[:8]:
        print(f"  {x['url']}")
        print(f"    ids={x['ids'][:40]}")
        total_ids += len(x["ids"])
    print(f"  XHR 去重 offerId 总数：{len(set(i for x in offer_ids_xhr for i in x['ids']))}"
          f"（未去重 {total_ids}）")

    print("\n=== A3. 关键 mtop 接口 body 里的 offerId（转义兼容） ===")
    for b in api_bodies:
        text = b["text"]
        ids = re.findall(r'offerId[\\"\'"\s:=]+(\d+)', text)
        ids += re.findall(r'/(?:offer|item)/(\d+)', text)
        ordered = list(dict.fromkeys(ids))
        counts = {k: len(re.findall(re.escape(k), text))
                  for k in ("offerId", "itemId", "productId", "offer_id", '"id"', "offerIds",
                            "offerTitle", "subject")}
        big = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        print(f"  [{b['key']}][{b['component']}] len={len(text)} "
              f"offerId可解析={len(ordered)} id-like={big}")
        if ordered:
            print(f"      前40={ordered[:40]}")
        if b["key"] == "shop.data.get":
            offers = parse_shop_data(b["text"])
            print(f"      shop.data.get 解析出的 offer 对象数={len(offers)}")
            for o in offers[:12]:
                k = list(o.keys())
                img = str(o.get("image") or o.get("pic") or o.get("picUrl") or o.get("img") or "")
                print(f"        id={o.get('id')} subject={(o.get('subject') or o.get('title') or '')[:24]!r} "
                      f"img=...{img[-70:] if img else ''}")
                if len(k) and len(offers) <= 4:
                    print(f"          keys={k[:12]}")

    print("\n=== A2. 全部 XHR/Fetch URL（去重） ===")
    for u in xhr_urls[:30]:
        print(f"  {u}")

    print("\n=== B. 卡片 DOM ===")
    print(f"  商品图数量：{img_sel_count}")
    with_title = [n for n in named if n["title"]]
    print(f"  商品名可提取数：{len(with_title)} / {len(named)}")
    for n in named[:10]:
        print(f"    [{n['idx']}] {n['title']!r}  ...{n['src']}")
    for c in card_info[:8]:
        print(f"  alt={c['alt']!r} aHref={c['aHref']!r} data={c['data']}")
        print(f"    title={c['title']!r}")
        print(f"    parentText={c['parentText']!r}")
        print(f"    parentText2={c['parentText2']!r}")
        for at in c["ancestorTexts"][:6]:
            print(f"      anc:: {at}")
        print(f"    chain={c['chain']} src=...{c['src']}")

    print("\n=== C. 页面里 /offer|item 链接/offerId 属性 ===")
    print(f"  命中数：{len(anchor_hits)}")
    for a in anchor_hits[:30]:
        print(f"  {a}")

    print("\n=== D. 整页 HTML / 内联脚本里的 offerId ===")
    print(f"  HTML 去重 offerId：{len(set(html_offer_ids))}  示例={list(dict.fromkeys(html_offer_ids))[:12]}")
    print(f"  Script 去重 offerId：{len(set(script_offer_ids))}  示例={list(dict.fromkeys(script_offer_ids))[:12]}")
    print(f"  img 卡片数={img_sel_count}")
    # 在源码里定位真实卡片 offer id（以其中一个已点击到的 id 为例）
    probe_id = "541861357088"
    h_in = probe_id in html
    s_in = probe_id in script_text
    print(f"  probe id {probe_id} in html={h_in} in script={s_in}")
    idx = script_text.find(probe_id)
    if idx >= 0:
        print(f"    script 上下文：...{script_text[max(0,idx-260):idx+80]!r}")

    # 顺序核对：尝试点击前 3 张卡片，比较 popup 里的 offer id 与 shop.data.get 的顺序
    shop_ids = []
    for b in api_bodies:
        if b["key"] == "shop.data.get":
            offers = parse_shop_data(b["text"])
            shop_ids = list(dict.fromkeys([o.get("id") for o in offers if o.get("id")]))
            break
    print("\n=== E. ORDER CHECK (click first 3 cards vs shop.data.get order) ===")
    print(f"  shop.data.get ordered offerId count={len(shop_ids)} first={shop_ids[:8]}")
    got_ids = []
    try:
        cards = page.locator("img.main-picture, img.hover-trigger")
        for i in range(min(3, cards.count())):
            card = cards.nth(i)
            oid = ""
            try:
                with page.expect_popup(timeout=4000) as pi:
                    card.evaluate(
                        """el => { let t=el; for(let i=0;i<6&&t;i++){
                          const st=window.getComputedStyle(t);
                          if(t.tagName==='A'||t.onclick||t.getAttribute('href')||st.cursor==='pointer'){
                            t.click(); return true; } t=t.parentNode; } el.click(); return true; }"""
                    )
                pop = pi.value
                if pop:
                    pop.wait_for_load_state("domcontentloaded", timeout=cfg_timeout)
                    m = re.search(r"/(?:offer|item)/(\d+)", pop.url)
                    oid = m.group(1) if m else ""
                    try:
                        pop.close()
                    except Exception:
                        pass
            except Exception:
                pass
            got_ids.append(oid)
        print(f"  clicked {len(got_ids)} cards, popup offerId={got_ids}")
    except Exception as exc:
        print(f"  click check failed: {exc}")
    match = got_ids and shop_ids and [g for g in got_ids if g in shop_ids]
    print(f"  in shop.data.get? {match}")
    print(f"  exact order match={got_ids == shop_ids[:len(got_ids)]}")

    br.close()
    pw.stop()
    subprocess.run(["taskkill", "/IM", "msedge.exe", "/F"], capture_output=True)
    print("\n=== 诊断结束 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
