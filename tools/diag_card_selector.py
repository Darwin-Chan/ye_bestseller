"""诊断：店铺商品列表页可点击卡片元素的真实 tag/class（临时工具）。"""
import json
import os
import subprocess
import time

from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = os.path.abspath("profiles/account1_edge")
PORT = 9222
URL = "https://yipihuo8.1688.com/page/offerlist.htm"


def main():
    subprocess.Popen([
        EDGE, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ])
    time.sleep(9)
    pw = sync_playwright().start()
    br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = br.contexts[0]
    page = ctx.new_page()
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
    br.close()
    pw.stop()
    subprocess.run(["taskkill", "/IM", "msedge.exe", "/F"], capture_output=True)


if __name__ == "__main__":
    main()
