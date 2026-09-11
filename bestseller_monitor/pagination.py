"""翻页等待：确认「新一批榜单内容」真的加载出来（IS-36）。

点击「下一页 / 加载更多」之后页面是 AJAX 局部更新：旧卡片会先留在原地，
所以「页面已加载（domcontentloaded）」和「至少有一张商品卡片」这两个条件
旧页本身就满足。慢请求下按这两个条件判定「新页已就绪」，只会重复读旧页或漏页。

这里改用「商品卡片身份序列」判断列表是否真的变了：

- 「下一页」整页替换，序列改变；
- 「加载更多」在旧卡片后面追加，序列改变（变长）。

身份 = 每张卡片的图片地址 + 卡片内首行文字（拿不到卡片图时退化为商品链接）。
任一维度变化都能反映「换了一批商品」，不依赖分页控件的具体结构。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# 单次翻页等新内容的上限（秒）：按 browser_pw 的 _WAIT_NEXT_SEC 传入，这里只给默认值
LIST_CHANGE_TIMEOUT_SEC = 10.0
_POLL_SEC = 0.4

# 商品卡片身份：优先商品图（点击式列表的商品没有 <a href>），没有商品图时退回商品链接。
# 卡片名沿用 _read_card_title() 的口径——「只含一张商品图的最小祖先容器」的首行文字。
_CARD_IDENTITY_JS = """
() => {
  const cards = Array.from(document.querySelectorAll('img.main-picture'));
  if (cards.length) {
    return cards.map(im => {
      const src = im.currentSrc || im.getAttribute('src') || '';
      let name = '';
      for (let el = im, k = 0; el && k < 16; k++) {
        let n = 0;
        try { n = el.querySelectorAll ? el.querySelectorAll('img.main-picture').length : 0; } catch (_) {}
        if (n === 1) {
          const t = (el.innerText || '').trim();
          if (t) {
            name = (t.split(/\\n/).map(s => s.trim()).filter(Boolean)[0] || '').slice(0, 120);
            break;
          }
        }
        el = el.parentElement;
      }
      return (src + '|' + name).slice(0, 240);
    });
  }
  return Array.from(document.querySelectorAll("a[href*='/offer/'], a[href*='/item/']"))
              .map(a => a.href || '');
}
"""


def _frames(page) -> list:
    """页面 + 子 frame；拿不到 frame 列表时返回空（只读主页面）。"""
    try:
        return list(page.frames)
    except Exception:
        return []


def _identity_in(frame) -> list[str]:
    try:
        found = frame.evaluate(_CARD_IDENTITY_JS)
    except Exception:
        return []
    return [str(x) for x in found] if found else []


def list_identity(page) -> tuple[str, ...]:
    """当前列表页的商品卡片身份序列；跨 frame 汇总，读不到时返回空元组。

    空元组表示「这次读不到列表结构」（页面异常、frame 未就绪等），
    调用方据此不做变化判断，见 wait_for_list_change()。
    """
    out: list[str] = []
    for frame in _frames(page):
        out.extend(_identity_in(frame))
    return tuple(out)


def drission_list_identity(page) -> tuple[str, ...]:
    """DrissionPage 版列表身份：商品链接（offer/item）序列，读不到返回空元组。"""
    try:
        anchors = page.eles("tag:a")
    except Exception:
        return ()
    out: list[str] = []
    for a in anchors:
        try:
            href = a.attr("href") or ""
        except Exception:
            continue
        if "/offer/" in href or "/item/" in href:
            out.append(str(href))
    return tuple(out)


def wait_for_change(observe, before: tuple, describe: str, timeout_sec: float) -> bool:
    """轮询 observe()，等到结果与 before 不同为止。

    before 为空表示翻页前就没读到列表身份：无从判断变化，返回 True 放行，
    由调用方按旧逻辑（等卡片出现）兜底，避免把「读不到 DOM」误判成加载失败。
    """
    if not before:
        log.info("翻页前读不到列表身份，跳过「%s」的变化判断。", describe)
        return True
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            current = observe()
        except Exception:
            current = ()
        if current and current != before:
            return True
        time.sleep(_POLL_SEC)
    log.info("等待「%s」超时(%.0fs)：列表身份始终未变。", describe, timeout_sec)
    return False


def wait_for_list_change(page, before: tuple, describe: str,
                         timeout_sec: float | None = None) -> bool:
    """等 Playwright 列表相对翻页前的身份发生变化；详见 wait_for_change()。

    timeout_sec 省略时取 LIST_CHANGE_TIMEOUT_SEC（调用时解析，方便按驱动调整）。
    """
    return wait_for_change(lambda: list_identity(page), before, describe,
                           _resolve_timeout(timeout_sec))


def wait_for_drission_change(page, before: tuple, describe: str,
                             timeout_sec: float | None = None) -> bool:
    """等 DrissionPage 列表相对翻页前的身份发生变化；详见 wait_for_change()。"""
    return wait_for_change(lambda: drission_list_identity(page), before, describe,
                           _resolve_timeout(timeout_sec))


def _resolve_timeout(timeout_sec: float | None) -> float:
    return LIST_CHANGE_TIMEOUT_SEC if timeout_sec is None else timeout_sec
