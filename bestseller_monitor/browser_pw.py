"""Playwright 连接接管驱动：会话可信 + 可稳定拦截接口。

与 browser_dp 的区别：这里用 Playwright 连接已启动的浏览器(connect_over_cdp)，
新建标签页后 network 事件可靠，能拦截 getShopOfferList / mtop 等 XHR。
"""
from __future__ import annotations

import logging
import hashlib
import os
import re
import subprocess
import time

from .config import Config, Shop
from .db import utcnow, cst_date
from .delay import Humanizer
from .detail import DetailParseFailed
from .parse import extract_skus_from_html, extract_title, extract_main_image
from . import sound
from .guard import (
    body_text, captcha_visible, deny_resolved, detect, intervention_kind,
    is_deny_url, is_login_url, is_punish_url, resolved, vtype, wait_for_resolution,
)

log = logging.getLogger(__name__)

# 记录本次由 open_session 启动的浏览器进程，收尾只结束它，绝不波及用户其它 Edge 窗口。
_launched_proc = None

# 兼容旧私有名/旧名（本文件内部与诊断工具仍引用）
_body_text = body_text
_captcha_visible = captcha_visible
_deny_resolved = deny_resolved
_is_punish_url = is_punish_url
_is_deny_url = is_deny_url
_resolved = resolved
_vtype = vtype

# 条件等待的上限兜底（秒）——优先“等条件满足”，超时才继续，替代固定 sleep。
_WAIT_LAUNCH_SEC = 25.0    # 等浏览器调试端口可连接
_WAIT_UI_SEC = 18.0        # 列表页等商品卡片出现
_WAIT_SORT_SEC = 10.0      # 点「销量」排序后等列表刷新
_WAIT_SCROLL_SEC = 3.0     # 每次滚动后等新一批卡片
_WAIT_NEXT_SEC = 10.0      # 翻页/加载更多后等列表刷新
_WAIT_BACK_SEC = 8.0       # 返回上一页后等就绪


def _wait_until(page, describe: str, predicate, timeout_sec: float, poll: float = 0.4) -> bool:
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


def _wait_cards(page, min_count: int = 1, timeout_sec: float = _WAIT_UI_SEC,
                describe: str = "商品卡片出现"):
    """等列表页出现至少 min_count 张商品卡片（条件等待，超时兜底）。"""
    return _wait_until(page, describe,
                       lambda: page.locator(_PRODUCT_IMG_SEL).count() >= min_count,
                       timeout_sec)


def open_session(cfg: Config):
    global _launched_proc
    from playwright.sync_api import sync_playwright

    edge = cfg.chrome_path or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    proc = None
    if getattr(cfg, "start_browser", True) and os.path.exists(edge):
        proc = subprocess.Popen([
            edge,
            f"--remote-debugging-port={cfg.attach_port}",
            f"--user-data-dir={cfg.user_data_path}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ])
        log.info("已用普通进程启动浏览器（调试端口 %s，PID %s）。", cfg.attach_port, proc.pid)
    _launched_proc = proc

    pw = sync_playwright().start()
    # 用「能连上调试端口」作为浏览器就绪条件，替代固定 8 秒（超时兜底）
    br = None
    last_exc: Exception | None = None
    deadline = time.time() + _WAIT_LAUNCH_SEC
    while time.time() < deadline:
        try:
            br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cfg.attach_port}")
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(0.8)
    if br is None:
        try:
            pw.stop()
        except Exception:
            pass
        raise RuntimeError(f"无法连接浏览器调试端口 {cfg.attach_port}（{last_exc}）")
    ctx = br.contexts[0]
    page = ctx.new_page()
    page.set_default_timeout(cfg.timeout_ms)
    return pw, br, page, ctx


def close_session(pw, br) -> None:
    global _launched_proc
    try:
        br.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
    # 只关闭本次启动的那个浏览器进程树，避免 taskkill /IM msedge.exe 强杀用户的其它 Edge。
    proc = _launched_proc
    _launched_proc = None
    if proc is not None and proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            log.info("已关闭本次启动的浏览器进程（PID %s）。", proc.pid)
        except Exception as exc:
            log.debug("关闭浏览器进程失败：%s", exc)


class ShopDenyExceeded(Exception):
    """某店滚动窗口内 deny 数达到阈值，跳过该店。"""


class RoundDenyExceeded(Exception):
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


_ANCHOR_SEL = "a[href*='/offer/'], a[href*='/item/']"
_PRODUCT_IMG_SEL = "img.main-picture, img.hover-trigger"


def _frame_anchors(frame) -> list[dict]:
    try:
        return frame.evaluate(
            """() => Array.from(document.querySelectorAll('%s'))
               .map(e => ({href: e.href, text: (e.innerText || '').trim().slice(0,240)}))""" % _ANCHOR_SEL
        )
    except Exception:
        return []


def _extract_anchors(page) -> list[dict]:
    out: list[dict] = []
    try:
        out += page.eval_on_selector_all(
            _ANCHOR_SEL,
            "els => els.map(e => ({href: e.href, text: (e.innerText || '').trim().slice(0,240)}))",
        )
    except Exception:
        pass
    for fr in page.frames:
        if fr == page.main_frame:
            continue
        out += _frame_anchors(fr)
    return out


def _click_text_in_frames(page, label: str) -> bool:
    # 先在顶层找，再逐个 frame 找
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


def crawl_store_listing(page, shop: Shop, cfg: Config, human: Humanizer):
    """店铺商品列表：销量排序 + 最多 N 页。返回 (offers, pages)。"""
    log.info("开始抓取店铺 %s（%s）", shop.key, shop.url)
    offers: list[tuple[int, str, str, str, str]] = []
    seen: set[str] = set()
    captured_oids: set[str] = set()
    punished = [False]
    pages_read = 0

    def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            url = resp.url.lower()
            if _is_punish_url(url):
                punished[0] = True
            text = resp.text()
            body_oids = re.findall(r"/offer/(\d+)\.html", text)
            body_oids += re.findall(r'["\']offerId["\']\s*:\s*(\d+)', text)
            body_oids += re.findall(r'offerId["\':=\s]+(\d+)', text)
            for oid in body_oids:
                if oid not in captured_oids:
                    captured_oids.add(oid)
        except Exception:
            pass

    page.on("response", on_response)

    page.goto(shop.url, wait_until="domcontentloaded")
    time.sleep(4)

    if _click_text_in_frames(page, "销量"):
        time.sleep(3)
        log.info("已点击「销量」排序")

    # 仅当捕捉到真实验证信号才进入介入流程（持续响铃直到解决）
    kind = intervention_kind(page, punished[0])
    if kind:
        wait_for_resolution(page, cfg.human_pause_minutes,
                            confirm_sec=cfg.intervention_confirmation_sec)
        try:
            page.reload(wait_until="domcontentloaded")
            time.sleep(4)
        except Exception as exc:
            log.warning("刷新失败：%s", exc)
    else:
        time.sleep(5)  # 无验证：给一点时间让商品加载
        if not _extract_anchors(page):
            try:
                page.mouse.wheel(0, 2000)
            except Exception:
                pass
            time.sleep(3)

    while pages_read < cfg.max_pages_per_shop:
        pages_read += 1
        human.before_list_page()
        time.sleep(1)
        kind = intervention_kind(page, punished[0])
        if kind:
            wait_for_resolution(page, cfg.human_pause_minutes,
                                confirm_sec=cfg.intervention_confirmation_sec)

        added = 0
        for item in _extract_anchors(page):
            href = item.get("href") or ""
            m = re.search(r"/(?:offer|item)/(\d+)\.html", href)
            if not m:
                continue
            oid = m.group(1)
            if oid in seen:
                continue
            seen.add(oid)
            offers.append((len(offers) + 1, oid, href, item.get("text") or "", ""))
            added += 1
        # 接口兜底：把拦截到的 offerId 也补进来（无法拿标题时给空）
        for oid in list(captured_oids):
            if oid not in seen:
                seen.add(oid)
                offers.append((len(offers) + 1, oid, f"https://detail.1688.com/offer/{oid}.html", "", ""))
                added += 1
        log.info("店铺 %s 第 %s 页新增 %s，累计 %s", shop.key, pages_read, added, len(offers))

        if pages_read >= cfg.max_pages_per_shop:
            break
        if not _click_text_in_frames(page, "下一页"):
            log.info("店铺 %s 无下一页，提前结束", shop.key)
            break
        page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
        time.sleep(2)

    return offers, pages_read


def capture_detail(page, product_url: str, cfg: Config, human: Humanizer, emit=None) -> dict:
    if emit:
        m = re.search(r"/(?:offer|item)/(\d+)\.html", product_url)
        emit("detail_nav", offer_id=m.group(1) if m else None, phase="detail")
    page.goto(product_url, wait_until="domcontentloaded")
    time.sleep(2)
    kind = intervention_kind(page, False)
    if kind:
        wait_for_resolution(page, cfg.human_pause_minutes, emit=emit,
                            verification_type=_vtype(kind),
                            confirm_sec=cfg.intervention_confirmation_sec)
    html = page.content()
    rows = extract_skus_from_html(html)
    if not rows:
        raise DetailParseFailed(f"详情页未解析到 SKU：{product_url}", html=html)
    if emit:
        emit("detail_parse", phase="detail", note=f"sku_count={len(rows)}")
    return {"product_name": extract_title(html) or "", "html": html, "rows": rows}


def crawl_store_by_click(page, shop: Shop, cfg: Config, human: Humanizer,
                         db=None, round_id=None, emit=None, deny_tracker=None):
    """商品列表用「点击商品图进详情」的方式收集商品，绕开拿不到URL的问题。"""
    log.info("开始点击式抓取店铺 %s（%s）", shop.key, shop.url)
    max_pages = int(shop.pages) if shop.pages else int(cfg.max_pages_per_shop)
    offers: list[tuple[int, str, str, str, str]] = []
    seen: set[str] = set()
    name_counter: dict[str, int] = {}
    punished = [False]
    pages_read = 0

    def se(event: str, **kw: object) -> None:
        """shop 作用域事件：统一注入 shop_key；phase 默认 listing，可按调用单独覆盖。"""
        if emit is not None:
            emit(event, shop_key=shop.key, phase=kw.pop("phase", "listing"), **kw)

    def on_response(resp):
        try:
            if resp.request.resource_type in ("xhr", "fetch") and _is_punish_url(resp.url):
                punished[0] = True
        except Exception:
            pass

    page.on("response", on_response)
    page.goto(shop.url, wait_until="domcontentloaded")
    human.after_load()          # read_delay_sec：页面加载后、读取数据前的拟人化延迟
    _wait_cards(page, min_count=1, describe="店铺首屏商品卡片")   # 条件等待，替代固定 4s
    se("list_load", note=shop.url)
    human.before_action()       # action_delay_sec：点击排序前的拟人化延迟
    if _click_text_in_frames(page, "销量"):
        _wait_cards(page, min_count=1, timeout_sec=_WAIT_SORT_SEC,
                    describe="销量排序后商品卡片")   # 条件等待，替代固定 3s
        log.info("已点击「销量」排序")
        se("list_sort")
    kind = intervention_kind(page, punished[0])
    if kind:
        wait_for_resolution(page, cfg.human_pause_minutes, emit=se,
                            verification_type=_vtype(kind),
                            confirm_sec=cfg.intervention_confirmation_sec)

    while pages_read < max_pages:
        pages_read += 1
        human.before_list_page()
        se("list_page", note=f"page={pages_read}")
        # 滚动到底部触发懒加载，直到图片数量不再增加
        prev = -1
        for _ in range(12):
            baseline = page.locator(_PRODUCT_IMG_SEL).count()
            try:
                page.mouse.wheel(0, 6000)
                page.wait_for_load_state("domcontentloaded")
            except Exception:
                break
            # 等新一批卡片出现（条件等待，替代固定 1.2s；无新增则很快超时并停止滚动）
            _wait_until(page, "滚动加载新卡片",
                        lambda: page.locator(_PRODUCT_IMG_SEL).count() > baseline,
                        _WAIT_SCROLL_SEC)
            cur = page.locator(_PRODUCT_IMG_SEL).count()
            if cur == prev:
                break
            prev = cur
        n = page.locator(_PRODUCT_IMG_SEL).count()
        log.info("店铺 %s 第 %s 页图片总数 %s（滚动后）", shop.key, pages_read, n)

        for i in range(n):
            human.before_detail()   # 加大并随机化商品详情访问间隔
            se("product_open")
            list_title = _read_card_title(page, i)   # 点前读列表页商品名
            if list_title:
                name_counter[list_title] = name_counter.get(list_title, 0) + 1
                c = name_counter[list_title]
                if c >= 2:
                    # 同店（同页/跨页）重复商品名异常检测（第 2 次及以上出现）
                    log.warning("异常：店铺 %s 出现重复商品名「%s」（第 %s 个商品）",
                                shop.key, list_title[:60], i)
                    se("duplicate_name", note=f"name={list_title[:60]} @idx={i}")
                # 计数=1 且今天已有库存 → 暂缓（只记位置事件），若该名字最终计数>1 则第二遍补抓
                if db and round_id and c == 1 and db.inventory_exists_by_name(
                        shop.key, list_title, cst_date()):
                    log.info("店铺 %s 商品「%s」今日已有库存，暂缓（计数 %s）",
                             shop.key, list_title[:40], c)
                    se("defer_samename",
                       note=f"name={list_title[:40]} @page={pages_read} @idx={i}")
                    # 按名暂缓：若（店铺, 商品名, 当日）能确定唯一 offer_id，则把该商品计入
                    # 本轮榜单并补写一条“成功/跳过”快照，避免整轮商品数被低估；
                    # 同名多品拿不准就不写（只记事件），防止误配。
                    def_oid = db.find_offer_id_by_name(shop.key, list_title, cst_date())
                    if def_oid:
                        def_url = f"https://detail.1688.com/offer/{def_oid}.html"
                        if def_oid not in seen:
                            seen.add(def_oid)
                            offers.append((len(offers) + 1, def_oid, def_url, list_title, ""))
                        db.mark_skipped(round_id, shop.key, shop.url, shop.name, def_oid,
                                        def_url, list_title, note="今日已有同名库存，跳过")
                    continue
            img = page.locator(_PRODUCT_IMG_SEL).nth(i)
            _capture_card(page, img, list_title, cfg, punished, on_response, se, db,
                          round_id, shop, offers, seen, idx=i, page_no=pages_read,
                          human=human, deny_tracker=deny_tracker)

        if pages_read >= max_pages:
            break
        human.before_action()   # 翻页/加载更多前的拟人化延迟
        advanced = _click_text_in_frames(page, "下一页")
        if not advanced:
            advanced = _click_text_in_frames(page, "加载更多")
        if not advanced:
            log.info("店铺 %s 第 %s 页后无下一页/加载更多，提前结束", shop.key, pages_read)
            break
        page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
        _wait_cards(page, min_count=1, timeout_sec=_WAIT_NEXT_SEC,
                    describe="翻页后商品卡片")   # 条件等待，替代固定 2s

    # 第二遍：计数>1 的同名商品，按名找回并补抓（offer_id 去重，避免重复/遗漏）
    ambiguous = {n for n, c in name_counter.items() if c > 1}
    if ambiguous:
        log.info("店铺 %s 发现 %s 个同名商品名，回头补抓", shop.key, len(ambiguous))
        page.goto(shop.url, wait_until="domcontentloaded")
        human.after_load()
        _wait_cards(page, min_count=1, describe="补抓店铺首屏商品卡片")
        human.before_action()
        if _click_text_in_frames(page, "销量"):
            _wait_cards(page, min_count=1, timeout_sec=_WAIT_SORT_SEC,
                        describe="补抓排序后商品卡片")
        rkind = intervention_kind(page, punished[0])
        if rkind:
            wait_for_resolution(page, cfg.human_pause_minutes, emit=se,
                                verification_type=_vtype(rkind),
                                confirm_sec=cfg.intervention_confirmation_sec)
        for rpg in range(1, max_pages + 1):
            human.before_list_page()
            se("list_page", note=f"rescue_page={rpg}")
            prev = -1
            for _ in range(12):
                baseline = page.locator(_PRODUCT_IMG_SEL).count()
                try:
                    page.mouse.wheel(0, 6000)
                    page.wait_for_load_state("domcontentloaded")
                except Exception:
                    break
                _wait_until(page, "补抓滚动加载新卡片",
                            lambda: page.locator(_PRODUCT_IMG_SEL).count() > baseline,
                            _WAIT_SCROLL_SEC)
                cur = page.locator(_PRODUCT_IMG_SEL).count()
                if cur == prev:
                    break
                prev = cur
            n = page.locator(_PRODUCT_IMG_SEL).count()
            for i in range(n):
                name = _read_card_title(page, i)
                if name and name in ambiguous:
                    human.before_detail()
                    se("product_open")
                    img = page.locator(_PRODUCT_IMG_SEL).nth(i)
                    _capture_card(page, img, name, cfg, punished, on_response, se, db,
                                  round_id, shop, offers, seen, idx=i, page_no=rpg,
                                  human=human, deny_tracker=deny_tracker)
            if rpg >= max_pages:
                break
            human.before_action()
            advanced = _click_text_in_frames(page, "下一页")
            if not advanced:
                advanced = _click_text_in_frames(page, "加载更多")
            if not advanced:
                log.info("店铺 %s 补抓在第 %s 页后无下一页，提前结束", shop.key, rpg)
                break
            page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
            _wait_cards(page, min_count=1, timeout_sec=_WAIT_NEXT_SEC,
                        describe="补抓翻页后商品卡片")
    return offers, pages_read


def _click_one_product(page, img, cfg, punished, on_response, emit=None):
    """点击单个商品图的“可点击父元素”；返回 (detail_page, popup)。"""
    detail_page = None
    popup = None
    try:
        with page.expect_popup(timeout=4000) as pi:
            img.evaluate(
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
        popup = pi.value
        if popup:
            popup.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
            detail_page = popup
            popup.on("response", on_response)
            pk = intervention_kind(popup, punished[0])
            if pk:
                wait_for_resolution(popup, cfg.human_pause_minutes, emit=emit,
                                    verification_type=_vtype(pk),
                                    confirm_sec=cfg.intervention_confirmation_sec)
    except Exception:
        if "detail.1688.com/offer/" in (page.url or ""):
            detail_page = page
            page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
            pk = intervention_kind(page, punished[0])
            if pk:
                wait_for_resolution(page, cfg.human_pause_minutes, emit=emit,
                                    verification_type=_vtype(pk),
                                    confirm_sec=cfg.intervention_confirmation_sec)
    return detail_page, popup


def _read_card_title(page, idx: int) -> str:
    """读取第 idx 张商品卡（与 _PRODUCT_IMG_SEL 同序）在列表页展示的商品名。

    用「仅含一张商品图的最小祖先容器」定位卡片，取其 innerText 首行作为商品名；
    对卡片版式不敏感（不依赖 已售/¥ 等特定文案）。取不到（如店铺 logo 卡）返回空串。
    """
    try:
        loc = page.locator(_PRODUCT_IMG_SEL).nth(idx)
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


def _capture_card(page, img, list_title, cfg, punished, on_response, se, db, round_id, shop,
                  offers, seen, idx: int = 0, page_no: int = 0, human=None,
                  deny_tracker=None) -> str | None:
    """点开卡片弹出、offer_id 去重、读 SKU、入库；成功返回 offer_id，否则返回 None。
    deny 处理：第1/2次退避重试；第3次响铃提醒扫码、解除后重抓，30s 未成功视为失败。
    可能抛 ShopDenyExceeded / RoundDenyExceeded。"""
    cnote = f"page={page_no}&idx={idx}"
    per_product_denies = 0
    for _ in range(3):
        detail_page, popup = _click_one_product(page, img, cfg, punished, on_response, emit=se)
        if detail_page is None:
            se("click_no_popup", note=cnote)
            return None
        url = detail_page.url
        if _is_deny_url(url):
            per_product_denies += 1
            if deny_tracker:
                deny_tracker.record(shop.key)
                if deny_tracker.round_count() >= cfg.deny_round_limit:
                    se("click_deny", phase="detail", note=cnote + f"&n={per_product_denies}&round_abort")
                    raise RoundDenyExceeded(
                        f"整轮 {cfg.deny_window_minutes} 分钟内 deny≥{cfg.deny_round_limit}")
                if deny_tracker.shop_count(shop.key) >= cfg.deny_shop_limit:
                    se("click_deny", phase="detail", note=cnote + f"&n={per_product_denies}&shop_skip")
                    raise ShopDenyExceeded(
                        f"店铺 {shop.key} {cfg.deny_window_minutes} 分钟内 deny≥{cfg.deny_shop_limit}")
            log.warning("店铺 %s 商品命中 deny（该商品第 %s 次）", shop.key, per_product_denies)
            if per_product_denies == 1:
                se("click_deny", phase="detail", note=cnote + "&n=1")
                _close_popup_or_back(detail_page, popup, page)
                if human is not None:
                    human.sleep(cfg.deny_backoff_sec)
                continue
            if per_product_denies == 2:
                se("click_deny", phase="detail", note=cnote + "&n=2")
                _close_popup_or_back(detail_page, popup, page)
                if human is not None:
                    human.sleep(cfg.deny_retry2_backoff_sec)
                continue
            return _handle_deny_scan(page, detail_page, popup, img, cfg, punished, on_response,
                                     se, db, round_id, shop, offers, seen, list_title, cnote, human)
        return _ingest_detail(page, detail_page, popup, list_title, cfg, punished, on_response,
                              se, db, round_id, shop, offers, seen, cnote)
    return None


def _handle_deny_scan(page, detail_page, popup, img, cfg, punished, on_response, se, db,
                      round_id, shop, offers, seen, list_title, cnote, human) -> str | None:
    """第 3 次 deny：保留 deny 弹窗供扫码，响铃直到其 URL 离开 deny，再重新抓取。"""
    se("click_deny", phase="detail", note=cnote + "&n=3&scan")
    log.warning("店铺 %s 商品被 deny 第 3 次，请在 Edge 窗口扫码解除（响铃直到解除）", shop.key)
    appear_ts = time.time()
    while not _deny_resolved(detail_page):
        if time.time() - appear_ts > cfg.human_pause_minutes * 60:
            raise RuntimeError("人工介入(deny 扫码)超时")
        sound.play_alarm(count=1)
        time.sleep(3)
    log.info("店铺 %s 的 deny 界面已解除，停止响铃", shop.key)
    if re.search(r"/(?:offer|item)/(\d+)\.html", detail_page.url or ""):
        oid = _ingest_detail(page, detail_page, popup, list_title, cfg, punished, on_response,
                             se, db, round_id, shop, offers, seen, cnote)
        return oid
    _close_popup_or_back(detail_page, popup, page)
    return _retry_recapture(page, img, cfg, punished, on_response, se, db, round_id, shop,
                            offers, seen, list_title, cnote, human)


def _retry_recapture(page, img, cfg, punished, on_response, se, db, round_id, shop,
                     offers, seen, list_title, cnote, human) -> str | None:
    """扫码解除后限时重抓：deny_scan_wait_sec 秒内抓到即返回，否则视为失败。"""
    deadline = time.time() + cfg.deny_scan_wait_sec
    while time.time() < deadline:
        detail_page, popup = _click_one_product(page, img, cfg, punished, on_response, emit=se)
        if detail_page is not None and not _is_deny_url(detail_page.url or ""):
            oid = _ingest_detail(page, detail_page, popup, list_title, cfg, punished,
                                 on_response, se, db, round_id, shop, offers, seen, cnote)
            if oid:
                return oid
        if detail_page is not None:
            _close_popup_or_back(detail_page, popup, page)
        time.sleep(1)
    log.warning("店铺 %s 商品扫码后 %.0f 秒内仍未抓取成功，标记失败",
                shop.key, cfg.deny_scan_wait_sec)
    return None


def _ingest_detail(page, detail_page, popup, list_title, cfg, punished, on_response, se, db,
                   round_id, shop, offers, seen, cnote) -> str | None:
    """对非 deny 的详情弹窗做 offer_id 去重、读 SKU、入库；成功返回 offer_id。"""
    # IS-18：详情弹窗内的事件应记为 detail 阶段（传入的 se 默认标为 listing）
    _shop_emit = se
    def se(event: str, **kw: object) -> None:
        _shop_emit(event, phase="detail", **kw)
    url = detail_page.url
    m = re.search(r"/(?:offer|item)/(\d+)\.html", url)
    if not m:
        se("click_url_notoffer", note=cnote)
        _close_popup_or_back(detail_page, popup, page)
        return None
    oid = m.group(1)
    se("popup_open", offer_id=oid)
    first_time = oid not in seen
    if first_time:
        seen.add(oid)
        offers.append((len(offers) + 1, oid, url, list_title or "", ""))
        log.info("命中商品 %s（累计 %s）", oid, len(offers))
    if db and round_id and db.inventory_exists(shop.key, oid, cst_date()):
        # 今天已采过：仍把该商品计入本轮榜单，并补写一条“成功/跳过”快照，
        # 避免轮次商品数被低估、或误计为失败/待处理（PRD 口径）。
        if first_time:
            db.mark_skipped(round_id, shop.key, shop.url, shop.name, oid, url,
                            list_title or "", note="今日已有库存，跳过")
        se("click_skipped", offer_id=oid, note=cnote + "&offer_id=" + oid)
        se("skip_existing", offer_id=oid, note="inventory_exists_today")
        _close_popup_or_back(detail_page, popup, page)
        return oid
    if db and round_id and first_time:
        try:
            html = detail_page.content()
        except Exception:
            se("click_no_popup", offer_id=oid, note=cnote + "&offer_id=" + oid)
            se("popup_close", offer_id=oid)
            _close_popup_or_back(detail_page, popup, page)
            return None
        title = extract_title(html) or ""
        rows = extract_skus_from_html(html)
        if rows:
            img_url = extract_main_image(html)
            db.upsert_product(oid, url, title, img_url)
            se("detail_parse", offer_id=oid, note=f"sku_count={len(rows)}")
            snap_rows = [
                {
                    "round_id": round_id, "shop_key": shop.key,
                    "shop_url": shop.url, "shop_name": shop.name,
                    "offer_id": oid, "product_url": url,
                    "product_name": list_title or title,
                    "sku_id": sku.get("sku_id") or hashlib.sha1(
                        f"{oid}|{sku['sku_name']}".encode("utf-8")
                    ).hexdigest()[:16],
                    "sku_name": sku["sku_name"], "sku_price": sku["sku_price"],
                    "sku_stock": sku["sku_stock"], "collected_at": utcnow(),
                    "main_image_url": img_url, "page_status": "成功", "attempt": 1,
                }
                for sku in rows
            ]
            db.clear_failures(round_id, shop.key, oid)
            db.save_snapshot_rows(round_id, shop.key, snap_rows)
            se("click_ok", offer_id=oid, note=cnote + "&offer_id=" + oid + f"&sku={len(rows)}")
        else:
            db.mark_failure(round_id, shop.key, oid, 1, "popup未解析到SKU")
            se("click_parse_empty", offer_id=oid, note=cnote + "&offer_id=" + oid)
            se("detail_parse", offer_id=oid, note="sku_count=0")
    elif db and round_id:
        se("click_skipped", offer_id=oid, note=cnote + "&offer_id=" + oid + "&dup=1")
    se("popup_close", offer_id=oid)
    _close_popup_or_back(detail_page, popup, page)
    return oid


def _close_popup_or_back(detail_page, popup, page):
    if popup:
        try:
            popup.close()
        except Exception:
            pass
    elif detail_page is page:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            _wait_cards(page, min_count=1, timeout_sec=_WAIT_BACK_SEC,
                        describe="返回列表页商品卡片")   # 条件等待，替代固定 1s
        except Exception:
            pass
