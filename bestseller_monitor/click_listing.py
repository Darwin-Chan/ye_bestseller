"""点击式列表：页面 adapter + 一家店的遍历。

这一侧原来长在 browser_pw.py 里，页面动作与遍历规则混在一起，测试只能打四五个私有函数的桩。
现在分成三块：

- **页面 adapter**（`PlaywrightListing`）：把页面动作翻译成遍历要的几件事——打开并准备、
  滚动取卡片数、拿第 i 张卡的句柄、推进到下一批。测试注一个脚本化实现就能跑完整条遍历。
- **卡片句柄**：`title` / `acquire` / `url` / `offer_id` / `close`。
  `acquire` 交回详情访问 module 所需的页面句柄，弹窗谁开谁关；deny、人工介入、等待可读和
  HTML 翻译由 `detail_visit` 统一处理。
- **遍历上下文**（`ShopWalk`）：这家店这次遍历的账本、事件与每张卡的处理。

浏览器会话与逐卡详情机制仍留在 browser_pw.py；榜单页本身的行为（准备、推进、列表身份）
在 listing.py。
"""
from __future__ import annotations

import logging
import re

from playwright.sync_api import Error as PlaywrightError

from . import click_events, dedupe, detail, detail_visit, listing, rounds
from .config import Config, Shop, effective_pages_limit
from .db import cst_date, utcnow
from .delay import Humanizer
from .guard import RoundDenyExceeded, ShopDenyExceeded

log = logging.getLogger(__name__)


class _LateDenied(Exception):
    """详情已知后在可读等待中再次命中 deny，交回当前商品的 deny 阶梯。"""

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

    def record(self, spec: tuple[str, dict]) -> None:
        """发一条 `click_events` 交回的事件规格（事件名 + 载荷）。

        事件名与备注由协议模块产出，遍历只决定**什么时候**记——这里是这两件事的接缝。
        """
        self.emit(spec[0], **spec[1])

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

        访问顺序与晚到 deny 由 `detail_visit` 统一，命中 deny 仍按第 1/2 次退避重试、
        第 3 次关掉详情跳过当前商品。
        """
        ref = card.card_ref
        per_product_denies = 0
        retry_after_late_deny = False

        for _ in range(3):
            closed_for_deny = False
            # 打开卡片就是进详情：先问轮次，跨天或已过截止线就不再开始。
            rounds.ensure_workable(self.db, self.round_id, utcnow())
            try:
                visit = detail_visit.begin_detail_visit(
                    card.acquire, self.cfg, emit=self.emit,
                    deny_tracker=self.deny_tracker, shop_key=self.shop.key,
                )
            except (ShopDenyExceeded, RoundDenyExceeded) as exc:
                # 账目与判据都在 guard，这里只把它记成事件再上抛。
                flag = ("round_abort" if isinstance(exc, RoundDenyExceeded)
                        else "shop_skip")
                self.record(click_events.denied(ref, per_product_denies + 1, terminal=flag))
                raise

            if isinstance(visit, detail_visit.NotOpenedVisit):
                self.record(click_events.not_opened(ref))
                return None
            if isinstance(visit, detail_visit.ReadFailedVisit):
                # 取得或初次 guard 失败时，已打开的弹窗仍由点击调用方关闭。
                card.close()
                # 商品编号尚未可靠取得，点击路径没有商品可记；保留原异常。
                raise visit.error

            denied = isinstance(visit, detail_visit.DeniedVisit)
            if not denied:
                try:
                    return self._ingest(card, list_title, visit,
                                        retry=retry_after_late_deny)
                except (ShopDenyExceeded, RoundDenyExceeded) as exc:
                    flag = ("round_abort" if isinstance(exc, RoundDenyExceeded)
                            else "shop_skip")
                    self.record(click_events.denied(ref, per_product_denies + 1,
                                                   terminal=flag))
                    raise
                except _LateDenied:
                    self.emit("popup_close", offer_id=card.offer_id)
                    card.close()
                    closed_for_deny = True
                    retry_after_late_deny = True
                    denied = True

            if denied:
                per_product_denies += 1
                log.warning("店铺 %s 商品命中 deny（该商品第 %s 次）",
                            self.shop.key, per_product_denies)
                if per_product_denies == 1:
                    self.record(click_events.denied(ref, 1))
                    if not closed_for_deny:
                        card.close()
                    self.human.sleep(self.cfg.deny_backoff_sec)
                    continue
                if per_product_denies == 2:
                    self.record(click_events.denied(ref, 2))
                    if not closed_for_deny:
                        card.close()
                    self.human.sleep(self.cfg.deny_retry2_backoff_sec)
                    continue
                self.record(click_events.denied(ref, 3, terminal="skip"))
                log.warning("店铺 %s 商品第 3 次命中 deny，跳过当前商品", self.shop.key)
                if not closed_for_deny:
                    card.close()
                return None
        return None

    def _ingest(self, card, list_title: str,
                visit: detail_visit.ReadyDetailVisit, *, retry: bool = False) -> str | None:
        """一张卡的详情观测：认领商品、按结果记事件、把卡片关掉。"""
        offer_id = card.offer_id
        if offer_id is None:
            self.record(click_events.no_offer(card.card_ref))
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
                    duplicate=not first_time and not retry),
                lambda: self._read(visit, card, card.url, offer_id), attempts=1)
        except _LateDenied:
            raise
        except BaseException:
            # 停止判定（跨天／暂停／预算）从规则里抛出来：弹窗照常关掉再上抛。
            self.emit("popup_close", offer_id=offer_id)
            card.close()
            raise

        if result.outcome is detail.Outcome.SKIPPED_TODAY:
            self.record(click_events.skipped(card.card_ref, offer_id,
                                            click_events.SkipReason.TODAY))
            self.emit("skip_existing", offer_id=offer_id, note="inventory_exists_today")
        elif result.outcome is detail.Outcome.SUBMITTED:
            skus = result.sku_count if result.sku_count is not None else 0
            self.record(click_events.ok(card.card_ref, offer_id, skus))
        elif result.outcome is detail.Outcome.DUPLICATE:
            self.record(click_events.skipped(card.card_ref, offer_id,
                                            click_events.SkipReason.DUPLICATE))
        self.emit("popup_close", offer_id=offer_id)
        card.close()
        # 失败与「没读到编号」一样：对调用方来说这张卡没有拿到商品（只留了失败行）。
        return None if result.outcome is detail.Outcome.FAILED else result.offer_id

    def _read(self, visit: detail_visit.ReadyDetailVisit, card, product_url: str,
              offer_id: str) -> detail.Observation:
        """读取当前详情，并按「这次读成什么样」记事件。"""
        observation = visit.observe(product_url)
        if isinstance(observation, detail_visit.DeniedVisit):
            raise _LateDenied()
        if observation.ok:
            self.emit("detail_parse", offer_id=offer_id,
                    note=f"sku_count={observation.sku_count}")
            return observation
        self.record(click_events.unreadable(card.card_ref, offer_id))
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
        self._emit = None

    def prepare(self, describe: str, *, emit=None) -> None:
        self.page_no = 1
        self._emit = emit
        listing.prepare(self.page, self.shop.url, self.cfg, self.human,
                        describe=describe, emit=emit)

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
    """Playwright 里的一张商品卡：点开详情、交回页面、关掉。"""

    def __init__(self, owner: PlaywrightListing, index: int):
        self._owner = owner
        self.index = index
        self._detail_page = None
        self._popup = None
        self.url = ""
        self.offer_id: str | None = None

    @property
    def card_ref(self) -> click_events.CardRef:
        """这张卡的身份：页号由 adapter 记（推进时 +1），序号是它在本页的位置。"""
        return click_events.CardRef(page=self._owner.page_no, index=self.index)

    @property
    def ref(self) -> str:
        """详情机会账本上的标识：同一轮内重复命中同一张卡也认得出。"""
        return self.card_ref.ref

    def title(self) -> str:
        return read_card_title(self._owner.page, self.index)

    def acquire(self) -> detail_visit.OpenedDetail | None:
        owner = self._owner
        image = owner.page.locator(listing.PRODUCT_IMG_SEL).nth(self.index)
        self._detail_page, self._popup = click_card(
            owner.page, image, owner.cfg, emit=owner._emit)
        if self._detail_page is None:
            self.url = ""
            self.offer_id = None
            return None
        self.url = self._detail_page.url or ""
        found = re.search(r"/(?:offer|item)/(\d+)\.html", self.url)
        self.offer_id = found.group(1) if found else None
        return detail_visit.OpenedDetail(self._detail_page)

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


def click_card(page, image, cfg: Config, emit=None):
    """点商品图的「可点击父元素」并等详情打开；返回 (详情页, 弹窗)。

    弹窗没出现时，页面可能就地跳到了详情。这里只负责「把页面打开」；打开之后的 deny
    记账、人工介入、可读等待和 HTML 翻译由 `detail_visit` 统一负责——诊断工具因此拿到的
    是页面的原始状态，不再被隐式的等待挡住。
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
        return popup, popup

    if "detail.1688.com/offer/" not in (page.url or ""):
        return None, None
    try:
        page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
    except PlaywrightError:
        return None, None
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
