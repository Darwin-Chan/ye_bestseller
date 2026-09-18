"""榜单页：准备、推进、列表身份与失败表示。

采集驱动只有一条（Playwright 连接接管 + 点击式列表，见 ADR-0010）。这一个 module 管
「这一页榜单怎么算加载好了、怎么换到下一批、换不了怎么办」；浏览器会话、逐卡点击与
详情落库在 browser_pw.py。

调用方只需要知道两道 interface——`prepare()`（打开并准备好榜单页）与 `advance()`
（推进到下一批），以及确认不了时抛出的 `ListingLoadFailed`。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .config import Config
from .guard import intervention_kind, vtype, wait_for_resolution

log = logging.getLogger(__name__)

# 单次翻页等新内容的上限（秒）：调用方省略时用这个默认值
LIST_CHANGE_TIMEOUT_SEC = 10.0
_POLL_SEC = 0.4


class ListingLoadFailed(Exception):
    """列表无法确认可用，不能把空结果写成完成榜单。"""

    def __init__(self, message: str, html: str = ""):
        super().__init__(message)
        self.html = html


def save_raw_listing_page(cfg: Config, round_id: int, shop_key: str, html: str) -> Path:
    """存档失败列表页，供选择器或页面结构校准。"""
    d = cfg.raw_page_dir / f"round_{round_id}"
    d.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", shop_key)
    path = d / f"listing_{safe_key}.html"
    path.write_text(html, encoding="utf-8")
    return path


# ---------- 商品卡片与条件等待 ----------

# 只认商品图。曾经把 img.hover-trigger 也算进来，但那是店铺头部的 48×48 图标
# （imgextra/...-tps-48-48.png，渲染成 12×12，不在商品网格内），排在所有商品图之前，
# 导致下标 0 恒为它、点击必然没有弹窗，还会让「等商品卡片出现」在商品图渲染前就提前通过。
# 真实列表页 30 张商品图全部是 img.main-picture。
PRODUCT_IMG_SEL = "img.main-picture"

# 条件等待的上限兜底（秒）——优先「等条件满足」，超时才继续，替代固定 sleep。
WAIT_UI_SEC = 18.0        # 列表页等商品卡片出现
WAIT_SORT_SEC = 10.0      # 点「销量」排序后等列表刷新
WAIT_NEXT_SEC = 10.0      # 翻页/加载更多后等列表刷新


def wait_until(describe: str, predicate, timeout_sec: float, poll: float = 0.4) -> bool:
    """条件等待 + 上限兜底：条件满足返回 True；超时记日志并返回 False（不中断流程）。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(poll)
    log.info("等待「%s」超时(%.0fs)，按当前状态继续。", describe, timeout_sec)
    return False


def wait_cards(page, min_count: int = 1, timeout_sec: float | None = None,
               describe: str = "商品卡片出现"):
    """等列表页出现至少 min_count 张商品卡片（条件等待，超时兜底）。

    timeout_sec 省略时取 WAIT_UI_SEC（调用时解析，方便按驱动调整）。
    """
    return wait_until(describe,
                      lambda: page.locator(PRODUCT_IMG_SEL).count() >= min_count,
                      WAIT_UI_SEC if timeout_sec is None else timeout_sec)


def click_text_in_frames(page, label: str) -> bool:
    """点页面上第一个文字匹配的元素（先在顶层找，再逐个 frame 找）；点不到返回 False。"""
    try:
        loc = page.get_by_text(label, exact=False)
        if loc.count() > 0:
            loc.first.click(timeout=6000)
            return True
    except Exception:
        pass
    for fr in page.frames:
        try:
            loc = fr.get_by_text(label, exact=False)
            if loc.count() > 0:
                loc.first.click(timeout=6000)
                return True
        except Exception:
            continue
    return False


def page_html(page) -> str:
    try:
        return page.content()
    except Exception:
        return ""


def listing_load_failed(page, reason: str) -> ListingLoadFailed:
    """把「榜单没拿到」表示成一个可捕获的异常，并带上出错页面的存档。"""
    try:
        cards = page.locator(PRODUCT_IMG_SEL).count()
    except Exception:
        cards = "unknown"
    return ListingLoadFailed(
        f"{reason}（current_url={getattr(page, 'url', '')}，cards={cards}）",
        html=page_html(page),
    )


# ---------- 列表身份：确认「新一批榜单内容」真的加载出来（IS-36） ----------
#
# 点击「下一页 / 加载更多」之后页面是 AJAX 局部更新：旧卡片会先留在原地，所以
# 「页面已加载（domcontentloaded）」和「至少有一张商品卡片」这两个条件旧页本身就满足。
# 慢请求下按这两个条件判定「新页已就绪」，只会重复读旧页或漏页。
#
# 这里改用「商品卡片身份序列」判断列表是否真的变了：
#
# - 「下一页」整页替换，序列改变；
# - 「加载更多」在旧卡片后面追加，序列改变（变长）。
#
# 身份 = 每张卡片的图片地址 + 卡片内首行文字（拿不到卡片图时退化为商品链接）。
# 任一维度变化都能反映「换了一批商品」，不依赖分页控件的具体结构。

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
                           LIST_CHANGE_TIMEOUT_SEC if timeout_sec is None else timeout_sec)


# ---------- 推进：换到下一批 ----------

def prepare(page, url: str, cfg, human, *, describe: str, emit=None) -> None:
    """打开发榜页并把它准备好：等首屏卡片 → 人工介入 → 点「销量」排序。

    `describe` 是这次准备的上下文（如「店铺 A01 首屏」），用于日志与失败文案；
    `emit` 给了就记 `list_load` / `list_sort` 与人工介入事件（调用方决定这一遍要不要记）。

    首屏拿不到商品卡片 → 抛 `ListingLoadFailed`：不能把空结果写成完成榜单。
    """
    page.goto(url, wait_until="domcontentloaded")
    human.after_load()          # read_delay_sec：页面加载后、读取数据前的拟人化延迟
    cards_ready = wait_cards(page, min_count=1, describe=f"{describe}商品卡片")
    kind = intervention_kind(page)
    if kind:
        wait_for_resolution(page, cfg.human_pause_minutes, emit=emit,
                            verification_type=vtype(kind),
                            confirm_sec=cfg.intervention_confirmation_sec)
    if not cards_ready and not wait_cards(page, min_count=1,
                                          describe=f"{describe}（验证后）商品卡片"):
        raise listing_load_failed(page, f"{describe}未加载商品卡片：{url}")
    if emit is not None:
        emit("list_load", note=url)
    human.before_action()       # action_delay_sec：点击排序前的拟人化延迟
    if click_text_in_frames(page, "销量"):
        wait_cards(page, min_count=1, timeout_sec=WAIT_SORT_SEC,
                   describe=f"{describe}（销量排序后）商品卡片")
        log.info("已点击「销量」排序")
        if emit is not None:
            emit("list_sort")


def advance(page, human, cfg, describe: str) -> bool:
    """推进到下一批：点「下一页」（点不到就回退「加载更多」）并确认列表真的换了内容。

    `describe` 是这次推进的上下文（如「店铺 A01 第 3 页」），用于日志与失败文案。
    返回 False = 这一页后面没有下一批（两个控件都点不到）；点过而列表身份始终没变 →
    抛 `ListingLoadFailed`——旧卡片暂留不算换页（IS-36），再读一遍只会重复旧页、漏掉新页。
    """
    human.before_action()          # 翻页/加载更多前的拟人化延迟
    before = list_identity(page)   # 翻页前的列表身份，用来确认新页真的换了
    if not (click_text_in_frames(page, "下一页")
            or click_text_in_frames(page, "加载更多")):
        log.info("%s 后无下一页/加载更多，提前结束", describe)
        return False
    page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
    if not wait_for_list_change(page, before, describe, WAIT_NEXT_SEC):
        raise listing_load_failed(page, f"翻页后未确认新一页加载：{describe}")
    wait_cards(page, min_count=1, timeout_sec=WAIT_NEXT_SEC,
               describe=f"翻页后商品卡片：{describe}")   # 条件等待，替代固定 2s
    return True
