"""诊断：店铺商品列表页可点击卡片元素的真实 tag/class（临时工具）。

浏览器会话走 `browser_pw.open_session` / `close_session`（候选 03 / ADR-0019）：
端口、用户数据目录、Edge 路径、就绪等待与收尾归属只在那里定义。
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
    page.goto(URL, wait_until="domcontentloaded")
    time.sleep(7)
    print(">>> 若有滑块请在 Edge 窗口滑动；等待 40 秒……")
    time.sleep(40)
    try:
        page.reload(wait_until="domcontentloaded")
        time.sleep(5)
    except Exception as e:
        print("reload err", e)
    for _ in range(6):
        try:
            page.mouse.wheel(0, 5000)
            time.sleep(1.0)
        except Exception:
            break

    info = page.evaluate(
        """() => {
          const imgs=[...document.querySelectorAll('img')];
          const chains={}; const classSet={};
          imgs.slice(0,40).forEach((im,i)=>{
            let el=im, chain=[];
            for(let k=0;k<8&&el;k++){
              const st=window.getComputedStyle(el);
              const c=String(el.className||'').slice(0,70);
              if(c) classSet[c]=(classSet[c]||0)+1;
              if(el.tagName==='A'||el.onclick||el.getAttribute('href')||st.cursor==='pointer'){
                chain.push(el.tagName+'.'+c);
                break;
              }
              el=el.parentNode;
            }
            chains[i]=chain;
          });
          return {chains, classSet};
        }"""
    )
    print(json.dumps(info, ensure_ascii=False)[:2500])


if __name__ == "__main__":
    main()
