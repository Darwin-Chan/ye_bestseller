"""点击式列表：页面 adapter + 一家店的遍历。

这一侧原来长在 browser_pw.py 里，页面动作与遍历规则混在一起，测试只能打四五个私有函数的桩。
现在分成三块：

- **页面 adapter**（`PlaywrightListing`）：把页面动作翻译成遍历要的几件事——打开并准备、
  滚动取卡片数、拿第 i 张卡的句柄、推进到下一批。测试注一个脚本化实现就能跑完整条遍历。
- **卡片句柄**：`title` / `open` / `opened` / `denied` / `url` / `offer_id` / `read` / `close`。
  `read` 就是 `detail.capture_observation` 要的那道 adapter（候选 02 立的接缝），
  弹窗谁开谁关。
- **遍历上下文**（`ShopWalk`）：这家店这次遍历的账本、事件与每张卡的处理。

浏览器会话与逐卡详情机制仍留在 browser_pw.py；榜单页本身的行为（准备、推进、列表身份）
在 listing.py。
"""
from __future__ import annotations

import logging
import re
import time

from playwright.sync_api import Error as PlaywrightError

from . import dedupe, detail, listing, rounds
from .config import Config, Shop, effective_pages_limit
from .db import cst_date, utcnow
from .delay import Humanizer
from .guard import (RoundPauseRequired, intervention_kind, is_deny_url, is_punish_url,
                    vtype, wait_for_resolution)

log = logging.getLogger(__name__)

# 条件等待的上限兜底（秒）
WAIT_POPUP_MS = 2500       # 点击后等待新标签页；有效弹窗通常在 2 秒内出现
WAIT_SCROLL_SEC = 3.0      # 每次滚动后等新一批卡片
WAIT_BACK_SEC = 8.0        # 从详情返回列表页后等就绪


def crawl_store_by_click(listing_page, shop: Shop, cfg: Config, human: Humanizer, *,
                         db, round_id: int, emit=None, deny_tracker=None):
    """点击商品图进详情，收集这家店本轮的榜单。

    `listing_page` 是页面 adapter（生产用 `PlaywrightListing`，测试给脚本化实现）。
    规则在 ShopWalk 里：主遍历 + 同名商品的第二遍补抓。
    """
    walk = ShopWalk(listing_page, shop, cfg, human,
                    db=db, round_id=round_id, emit=emit, deny_tracker=deny_tracker)
    return walk.run()


# ---------- deny：被反爬拦下的计数与两个异常 ----------

class ShopDenyExceeded(Exception):
    """某店滚动窗口内 deny 数达到阈值，跳过该店。"""


class RoundDenyExceeded(RoundPauseRequired):
    """整轮滚动窗口内 deny 数达到阈值，中止本轮。"""


class DenyTracker:
    """滚动窗口内的 deny 计数（按店 + 整轮）。"""

    def __init__(self, window_sec: float):
        self.window_sec = window_sec
        self.events: list[tuple[float, str]] = []

    def _prune(self, now: float) -> None:
        self.events = [(t, s) for t, s in self.events if now - t <= self.window_sec]

    def record(self, shop_key: str) -> None:
        now = time.time()
        self.events.append((now, shop_key))
        self._prune(now)

    def shop_count(self, shop_key: str) -> int:
        self._prune(time.time())
        return sum(1 for t, s in self.events if s == shop_key)

    def round_count(self) -> int:
        self._prune(time.time())
        return len(self.events)


class ShopWalk:
    """一家店这一次点击式遍历：账本、事件与每张卡的处理。"""

    def __init__(self, listing_page, shop: Shop, cfg: Config, human: Humanizer, *,
                 db, round_id: int, emit=None, deny_tracker=None):
        self.listing_page = listing_page
        self.shop = shop
        self.cfg = cfg
        self.human = human
        self.db = db
        self.round_id = round_id
        self.deny_tracker = deny_tracker
        self.offers: list[tuple[int, str, str, str, str]] = []
        self.seen: set[str] = set()
        self.name_counter: dict[str, int] = {}
        self.name_pages: dict[str, set[int]] = {}
        self.pages_read = 0
        self._emit = emit

    # ---------- 事件 ----------
    def emit(self, event: str, **kw: object) -> None:
        """店铺作用域事件：统一注入 shop_key；phase 默认 listing，可按调用单独覆盖。"""
        if self._emit is not None:
            self._emit(event, shop_key=self.shop.key, phase=kw.pop("phase", "listing"), **kw)

    @staticmethod
    def _card_note(card, offer_id: str | None = None, suffix: str = "") -> str:
        """事件备注：卡片位置 +(可选)商品编号 +(可选)后缀——改前遍历手拼的那一套。"""
        note = card.note
        if offer_id is not None:
            note += "&offer_id=" + offer_id
        return note + suffix

    # ---------- 遍历 ----------
    def run(self):
        shop = self.shop
        log.info("开始点击式抓取店铺 %s（%s）", shop.key, shop.url)
        max_pages = effective_pages_limit(shop, self.cfg)
        self.listing_page.prepare(f"店铺 {shop.key} 首屏", emit=self.emit)
        while self.pages_read < max_pages:
            self.pages_read += 1
            self.human.before_list_page()
            self.emit("list_page", note=f"page={self.pages_read}")
            count = self.listing_page.scroll_to_load("滚动加载新卡片")
            log.info("店铺 %s 第 %s 页图片总数 %s（滚动后）", shop.key, self.pages_read, count)
            for index in range(count):
                self._visit_card(index)
            if self.pages_read >= max_pages:
                break
            if not self.listing_page.advance(f"店铺 {shop.key} 第 {self.pages_read} 页"):
                break

        self._rescue_same_named()

        if not self.offers:
            self.listing_page.load_failed(f"店铺列表未解析到商品：{shop.url}")
        return self.offers, self.pages_read

    def _visit_card(self, index: int) -> None:
        """列表页上第 index 张卡：读名字、按名暂缓、进详情。"""
        self.emit("product_open")
        card = self.listing_page.card(index)
        list_title = card.title()
        if list_title:
            self.name_counter[list_title] = self.name_counter.get(list_title, 0) + 1
            seen_times = self.name_counter[list_title]
            self.name_pages.setdefault(list_title, set()).add(self.pages_read)
            if seen_times >= 2:
                # 同店（同页/跨页）重复商品名异常检测（第 2 次及以上出现）
                log.warning("异常：店铺 %s 出现重复商品名「%s」（第 %s 个商品）",
                            self.shop.key, list_title[:60], index)
                self.emit("duplicate_name", note=f"name={list_title[:60]} @idx={index}")
            # 计数=1 且今天已有库存 → 暂缓（只记位置事件），若该名字最终计数>1 则第二遍补抓
            if (seen_times == 1
                    and self.db.inventory_exists_by_name(self.shop.key, list_title, cst_date())):
                self._defer_same_named(card, list_title, index)
                return
        self._enter_detail(card, list_title)

    def _defer_same_named(self, card, list_title: str, index: int) -> None:
        """同名暂缓：按名能找到唯一商品编号就把它计入榜单并写一条跳过，否则只记事件。"""
        shop = self.shop
        log.info("店铺 %s 商品「%s」今日已有库存，暂缓（计数 1）", shop.key, list_title[:40])
        self.emit("defer_samename",
                note=f"name={list_title[:40]} @page={self.pages_read} @idx={index}")
        known_offer_id = self.db.find_offer_id_by_name(shop.key, list_title, cst_date())
        if not known_offer_id:
            return
        product_url = f"https://detail.1688.com/offer/{known_offer_id}.html"
        if known_offer_id not in self.seen:
            self.seen.add(known_offer_id)
            remember_discovery(self.db, self.round_id, shop, self.offers,
                               known_offer_id, product_url, list_title)
        self.db.mark_skipped(self.round_id, shop.key, shop.url, shop.name, known_offer_id,
                             product_url, list_title, note="今日已有同名库存，跳过")

    def _enter_detail(self, card, list_title: str) -> None:
        """进详情前先申请机会：预算就是用来限制详情访问的，所以要赶在点开卡片之前。"""
        dedupe.claim_slot(self.db, self.round_id, self.shop.key, card.ref,
                          self.cfg.max_detail_opportunities_per_round)
        self.human.before_detail()
        self.capture(card, list_title)

    # ---------- 一张卡 ----------
    def capture(self, card, list_title: str) -> str | None:
        """点开一张卡、取一次详情观测、按结果记事件；成功返回商品编号。

        deny 处理：第 1/2 次退避重试，第 3 次关掉详情跳过当前商品。
        可能抛 ShopDenyExceeded / RoundDenyExceeded。
        """
        note = card.note
        per_product_denies = 0
        for _ in range(3):
            # 打开卡片就是进详情：先问轮次，跨天或已过截止线就不再开始。
            rounds.ensure_workable(self.db, self.round_id, utcnow())
            card.open()
            if not card.opened():
                self.emit("click_no_popup", note=note)
                return None
            if card.denied():
                per_product_denies += 1
                if self.deny_tracker is not None:
                    self.deny_tracker.record(self.shop.key)
                    if self.deny_tracker.round_count() >= self.cfg.deny_round_limit:
                        self.emit("click_deny", phase="detail",
                                note=note + f"&n={per_product_denies}&round_abort")
                        raise RoundDenyExceeded(
                            f"整轮 {self.cfg.deny_window_minutes} 分钟内"
                            f" deny≥{self.cfg.deny_round_limit}")
                    if self.deny_tracker.shop_count(self.shop.key) >= self.cfg.deny_shop_limit:
                        self.emit("click_deny", phase="detail",
                                note=note + f"&n={per_product_denies}&shop_skip")
                        raise ShopDenyExceeded(
                            f"店铺 {self.shop.key} {self.cfg.deny_window_minutes} 分钟内"
                            f" deny≥{self.cfg.deny_shop_limit}")
                log.warning("店铺 %s 商品命中 deny（该商品第 %s 次）",
                            self.shop.key, per_product_denies)
                if per_product_denies == 1:
                    self.emit("click_deny", phase="detail", note=note + "&n=1")
                    card.close()
                    self.human.sleep(self.cfg.deny_backoff_sec)
                    continue
                if per_product_denies == 2:
                    self.emit("click_deny", phase="detail", note=note + "&n=2")
                    card.close()
                    self.human.sleep(self.cfg.deny_retry2_backoff_sec)
                    continue
                self.emit("click_deny", phase="detail", note=note + "&n=3&skip")
                log.warning("店铺 %s 商品第 3 次命中 deny，跳过当前商品", self.shop.key)
                card.close()
                return None
            return self._ingest(card, list_title)
        return None

    def _ingest(self, card, list_title: str) -> str | None:
        """一张卡的详情观测：认领商品、按结果记事件、把卡片关掉。"""
        offer_id = card.offer_id
        if offer_id is None:
            self.emit("click_url_notoffer", note=card.note)
            card.close()
            return None
        self.emit("popup_open", offer_id=offer_id)
        first_time = offer_id not in self.seen
        if first_time:
            self.seen.add(offer_id)
            remember_discovery(self.db, self.round_id, self.shop, self.offers,
                               offer_id, card.url, list_title or "")
            log.info("命中商品 %s（累计 %s）", offer_id, len(self.offers))
        try:
            result = detail.capture_observation(
                self.db, self.cfg, self.human, self.round_id,
                detail.DetailTarget(
                    shop_key=self.shop.key, shop_url=self.shop.url,
                    shop_name=self.shop.name, product_url=card.url,
                    slot_key=card.ref, offer_id=offer_id, list_title=list_title,
                    duplicate=not first_time),
                lambda: self._read(card, offer_id), attempts=1)
        except BaseException:
            # 停止判定（跨天／暂停／预算）从规则里抛出来：弹窗照常关掉再上抛。
            self.emit("popup_close", offer_id=offer_id)
            card.close()
            raise

        if result.outcome is detail.Outcome.SKIPPED_TODAY:
            self.emit("click_skipped", offer_id=offer_id,
                    note=card.note + "&offer_id=" + offer_id)
            self.emit("skip_existing", offer_id=offer_id, note="inventory_exists_today")
        elif result.outcome is detail.Outcome.SUBMITTED:
            skus = result.sku_count if result.sku_count is not None else 0
            self.emit("click_ok", offer_id=offer_id,
                    note=self._card_note(card, offer_id, f"&sku={skus}"))
        elif result.outcome is detail.Outcome.DUPLICATE:
            self.emit("click_skipped", offer_id=offer_id,
                    note=self._card_note(card, offer_id, "&dup=1"))
        self.emit("popup_close", offer_id=offer_id)
        card.close()
        # 失败与「没读到编号」一样：对调用方来说这张卡没有拿到商品（只留了失败行）。
        return None if result.outcome is detail.Outcome.FAILED else result.offer_id

    def _read(self, card, offer_id: str) -> detail.Observation:
        """读一次观测，并按「这次读成什么样」记事件（事件顺序与改前一致）。"""
        observation = card.read()
        if observation.ok:
            self.emit("detail_parse", offer_id=offer_id,
                    note=f"sku_count={observation.sku_count}")
            return observation
        self.emit("click_parse_error", offer_id=offer_id,
                note=self._card_note(card, offer_id))
        if observation.kind is detail.FailureKind.PARSE:
            self.emit("detail_parse", offer_id=offer_id, note="sku_count=0")
        return observation

    # ---------- 同名商品第二遍 ----------
    def _rescue_same_named(self) -> None:
        """计数>1 的同名商品：回头按名补抓（offer_id 去重，避免重复/遗漏）。"""
        shop = self.shop
        ambiguous = {name for name, count in self.name_counter.items() if count > 1}
        if not ambiguous:
            return
        ambiguous_pages = set().union(*(self.name_pages[name] for name in ambiguous))
        rescue_last_page = max(ambiguous_pages)
        log.info("店铺 %s 发现 %s 个同名商品名，回头补抓第 %s 页（共 %s 页）",
                 shop.key, len(ambiguous), ",".join(map(str, sorted(ambiguous_pages))),
                 rescue_last_page)
        # 补抓第二遍与主页走同一份准备：顺序、兜底、事件都不再各写一遍。
        self.listing_page.prepare(f"店铺 {shop.key} 补抓首屏", emit=self.emit)
        for rescue_page in range(1, rescue_last_page + 1):
            self.human.before_list_page()
            self.emit("list_page", note=f"rescue_page={rescue_page}")
            if rescue_page in ambiguous_pages:
                count = self.listing_page.scroll_to_load("补抓滚动加载新卡片")
                for index in range(count):
                    card = self.listing_page.card(index)
                    name = card.title()
                    if name and name in ambiguous:
                        self.emit("product_open")
                        # 与主页一样：先申请机会（预算限制详情访问），再点开。
                        self._enter_detail(card, name)
            if rescue_page >= rescue_last_page:
                break
            if not self.listing_page.advance(f"店铺 {shop.key} 补抓第 {rescue_page} 页"):
                break


def remember_discovery(db, round_id: int, shop: Shop, offers: list,
                       offer_id: str, product_url: str, list_title: str) -> None:
    """把刚发现的商品计入本轮榜单：内存清单 + 立即落一条榜单行。

    商品在列表遍历途中一经发现就落库，因此无论以哪种方式中途离开榜单阶段，
    已写入的快照都有榜单行可对应，不会变成孤儿。
    """
    offers.append((len(offers) + 1, offer_id, product_url, list_title, ""))
    if db is not None and round_id is not None:
        db.remember_shop_offer(round_id, shop.key, shop.url, shop.name, offers[-1])


# ---------- 生产侧：Playwright adapter ----------

class PlaywrightListing:
    """点击式列表的 Playwright adapter。

    一个实例对应一次页面级的「从这家店捞一遍」：`prepare` 打开发榜页并准备好，
    `card(i)` 给卡片句柄，`advance` 换下一批。页号由它自己记（卡片事件备注与失败文案都用它）。
    """

    def __init__(self, page, shop: Shop, cfg: Config, human: Humanizer):
        self.page = page
        self.shop = shop
        self.cfg = cfg
        self.human = human
        self.page_no = 0
        self.punished = False
        self._emit = None
        page.on("response", self._on_punish_response)

    def _on_punish_response(self, response) -> None:
        try:
            if (response.request.resource_type in ("xhr", "fetch")
                    and is_punish_url(response.url)):
                self.punished = True
        except Exception:
            pass

    def prepare(self, describe: str, *, emit=None) -> None:
        self.page_no = 1
        self._emit = emit
        listing.prepare(self.page, self.shop.url, self.cfg, self.human,
                        describe=describe, punished=self.punished, emit=emit)

    def scroll_to_load(self, describe: str) -> int:
        return scroll_cards_until_stable(self.page, describe)

    def card(self, index: int) -> "PlaywrightCard":
        return PlaywrightCard(self, index)

    def advance(self, describe: str) -> bool:
        if not listing.advance(self.page, self.human, self.cfg, describe):
            return False
        self.page_no += 1
        return True

    def load_failed(self, reason: str) -> None:
        raise listing.listing_load_failed(self.page, reason)


class PlaywrightCard:
    """Playwright 里的一张商品卡：点开、读它打开的那个页面、关掉。"""

    def __init__(self, owner: PlaywrightListing, index: int):
        self._owner = owner
        self.index = index
        self._detail_page = None
        self._popup = None
        self.url = ""
        self.offer_id: str | None = None

    @property
    def note(self) -> str:
        """事件备注里的卡片位置：改前由遍历拼，现在卡片自己知道。"""
        return f"page={self._owner.page_no}&idx={self.index}"

    @property
    def ref(self) -> str:
        """详情机会账本上的标识：同一轮内重复命中同一张卡也认得出。"""
        return f"card:p{self._owner.page_no}:i{self.index}"

    def title(self) -> str:
        return read_card_title(self._owner.page, self.index)

    def open(self) -> None:
        owner = self._owner
        image = owner.page.locator(listing.PRODUCT_IMG_SEL).nth(self.index)
        self._detail_page, self._popup = click_card(
            owner.page, image, owner.cfg, owner.punished, owner._on_punish_response,
            emit=owner._emit)
        if self._detail_page is None:
            self.url = ""
            self.offer_id = None
            return
        self.url = self._detail_page.url or ""
        found = re.search(r"/(?:offer|item)/(\d+)\.html", self.url)
        self.offer_id = found.group(1) if found else None

    def opened(self) -> bool:
        return self._detail_page is not None

    def denied(self) -> bool:
        return self._detail_page is not None and is_deny_url(self.url)

    def read(self) -> detail.Observation:
        """读一次详情观测：怎么拿到 html 归弹窗，读到什么算失败归 `detail.observe_page`。"""
        return detail.observe_page(self._detail_page.content, self.url)

    def close(self) -> None:
        close_popup_or_back(self._detail_page, self._popup, self._owner.page)


def scroll_cards_until_stable(page, describe: str = "滚动加载新卡片") -> int:
    """滚动加载卡片，首次无新增即停止，返回当前卡片数。

    每次滚动已经有条件等待；再次对同一无新增状态等待没有信息增益，
    只会给每页额外增加一个完整的超时窗口。
    """
    for _ in range(12):
        baseline = page.locator(listing.PRODUCT_IMG_SEL).count()
        try:
            page.mouse.wheel(0, 6000)
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            break
        loaded = listing.wait_until(
            describe,
            lambda: page.locator(listing.PRODUCT_IMG_SEL).count() > baseline,
            WAIT_SCROLL_SEC,
        )
        current = page.locator(listing.PRODUCT_IMG_SEL).count()
        if not loaded or current <= baseline:
            break
    return page.locator(listing.PRODUCT_IMG_SEL).count()


def read_card_title(page, idx: int) -> str:
    """读取第 idx 张商品卡（与 listing.PRODUCT_IMG_SEL 同序）在列表页展示的商品名。

    用「仅含一张商品图的最小祖先容器」定位卡片，取其 innerText 首行作为商品名；
    对卡片版式不敏感（不依赖 已售/¥ 等特定文案）。取不到（如店铺 logo 卡）返回空串。
    """
    try:
        loc = page.locator(listing.PRODUCT_IMG_SEL).nth(idx)
        return (loc.evaluate(
            """el => {
                let e = el;
                for (let k=0;k<16&&e;k++){
                  let imgs = 0;
                  try { imgs = e.querySelectorAll ? e.querySelectorAll('img.main-picture').length : 0; } catch(_) {}
                  if (imgs === 1) {
                    const t = (e.innerText||'').trim();
                    if (t) return (t.split(/\\n/).map(s=>s.trim()).filter(Boolean)[0]||'').slice(0,200);
                  }
                  e=e.parentElement;
                }
                return '';
            }"""
        ) or "").strip()
    except Exception:
        return ""


def click_card(page, image, cfg: Config, punished: bool, on_response, emit=None):
    """点商品图的「可点击父元素」并等详情打开；返回 (详情页, 弹窗)。

    弹窗没出现时，页面可能就地跳到了详情；两种情况都可能需要人工介入。
    """
    popup = None
    try:
        with page.expect_popup(timeout=WAIT_POPUP_MS) as pending:
            image.evaluate(
                """el => {
                    let t = el;
                    for (let i = 0; i < 6 && t; i++) {
                      const st = window.getComputedStyle(t);
                      if (t.tagName === 'A' || t.onclick || t.getAttribute('href')
                          || st.cursor === 'pointer') {
                        t.click();
                        return true;
                      }
                      t = t.parentNode;
                    }
                    el.click();
                    return true;
                }"""
            )
        popup = pending.value
    except PlaywrightError:
        # 无新标签页时，商品可能在当前页面跳转；其余点击失败按无弹窗处理。
        popup = None

    if popup is not None:
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
        except PlaywrightError:
            try:
                popup.close()
            except PlaywrightError:
                pass
            return None, None
        popup.on("response", on_response)
        kind = intervention_kind(popup, punished)
        if kind:
            wait_for_resolution(popup, cfg.human_pause_minutes, emit=emit,
                                verification_type=vtype(kind),
                                confirm_sec=cfg.intervention_confirmation_sec)
        return popup, popup

    if "detail.1688.com/offer/" not in (page.url or ""):
        return None, None
    try:
        page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
    except PlaywrightError:
        return None, None
    kind = intervention_kind(page, punished)
    if kind:
        wait_for_resolution(page, cfg.human_pause_minutes, emit=emit,
                            verification_type=vtype(kind),
                            confirm_sec=cfg.intervention_confirmation_sec)
    return page, None


def close_popup_or_back(detail_page, popup, page) -> None:
    """关掉详情弹窗；没有弹窗（就地跳转）时返回列表页并等卡片就绪。"""
    if popup:
        try:
            popup.close()
        except Exception:
            pass
    elif detail_page is page:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            listing.wait_cards(page, min_count=1, timeout_sec=WAIT_BACK_SEC,
                               describe="返回列表页商品卡片")   # 条件等待，替代固定 1s
        except Exception:
            pass
