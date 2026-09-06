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

log = logging.getLogger(__name__)

SLIDER_MARKERS = ("向右滑动验证", "请完成验证", "滑块验证", "拖动滑块", "安全验证", "punish")
LOGIN_MARKERS = ("登录后查看", "请登录", "扫码登录", "确认登录", "快速进入")


def open_session(cfg: Config):
    from playwright.sync_api import sync_playwright

    edge = cfg.chrome_path or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if getattr(cfg, "start_browser", True) and os.path.exists(edge):
        subprocess.Popen([
            edge,
            f"--remote-debugging-port={cfg.attach_port}",
            f"--user-data-dir={cfg.user_data_path}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ])
        log.info("已用普通进程启动浏览器（调试端口 %s）。", cfg.attach_port)
        time.sleep(8)

    pw = sync_playwright().start()
    br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cfg.attach_port}")
    ctx = br.contexts[0]
    page = ctx.new_page()
    page.set_default_timeout(cfg.timeout_ms)
    return pw, br, page, ctx


def close_session(pw, br) -> None:
    try:
        br.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
    try:
        subprocess.run(["taskkill", "/IM", "msedge.exe", "/F"], capture_output=True)
    except Exception:
        pass


def _body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000) or ""
    except Exception:
        return ""


def detect(page) -> str | None:
    url = (page.url or "").lower()
    if "login.taobao" in url or "login.1688" in url:
        return "登录墙"
    if _is_deny_url(url):
        return None          # 淘宝 deny/限流页：交给 _capture_card 退避，不响铃等扫码
    if _is_punish_url(url):
        return "滑块"
    body = _body_text(page)
    for m in SLIDER_MARKERS:
        if m in body:
            return "滑块"
    for m in LOGIN_MARKERS:
        if m in body and len(body) < 3000:
            return "登录墙"
    return None


def _captcha_visible(page) -> bool:
    # 只针对特征明确的验证容器/iframe，避免普通元素误判
    sel = ("iframe[src*='captcha' i], iframe[src*='nocaptcha' i], iframe[src*='secaptcha' i], "
           "#nc_1_wrapper, #nc_1_container, .nc-container, .nc_scale, #nc_1_n1z, "
           "#baxia-dialog-content, .baxia-dialog, [class*='baxia-dialog'], "
           "#nocaptcha, [class*='verify_'], [class*='captcha']")
    try:
        loc = page.locator(sel)
        n = loc.count()
        for i in range(min(n, 40)):
            try:
                if loc.nth(i).is_visible():
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _is_punish_url(url: str) -> bool:
    """真实验证页/验证请求：punish 页或 punishTextFetch。普通的 tmd report/x5sec 上报不算。"""
    u = (url or "").lower()
    # 站点会在正常详情 URL 后追加 /_____tmd_____/punish?x5secdata=... 的上报装饰，不算真验证
    if "_____tmd_____" in u:
        return False
    return "punishtextfetch" in u or "/punish?" in u or "/punish/" in u


def _is_deny_url(url: str) -> bool:
    """淘宝 deny/验证拦截页（bsop-punish/deny_pc，通常由连续高频访问触发的反爬限流）。
    这类不该响铃等人扫码，而应自动降速退避。"""
    u = (url or "").lower()
    return "bsop-punish" in u or "deny_pc" in u


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


def _deny_resolved(page) -> bool:
    """deny 界面是否已解除：URL 不再是 deny 页即可认为解除（用户扫码后页面会离开 deny）。"""
    return not _is_deny_url(page.url or "")


def intervention_kind(page, punished: bool) -> str | None:
    """判定是否需要人工介入。仅看验证据信号，绝不因“没有商品”误判。"""
    url = (page.url or "").lower()
    if "login.taobao" in url or "login.1688" in url:
        return "登录墙"
    if _is_punish_url(url):
        return "滑块"
    body = _body_text(page)
    for m in SLIDER_MARKERS:
        if m in body:
            return "滑块"
    if _captcha_visible(page):
        # 可见验证容器：只有当页面确实带验证文案，或不是“点我反馈”这种纯反爬拦截页时，才算滑块
        if any(m in body for m in SLIDER_MARKERS) or ("点我反馈" not in body):
            return "滑块"
    for m in LOGIN_MARKERS:
        if m in body and len(body) < 3000:
            return "登录墙"
    # 仅当 URL 是真 punish 页且页面确实“像验证”时，才兜底判滑块（避免裸 URL 误报）
    if punished and _is_punish_url(url):
        return "滑块"
    return None


def _vtype(kind: str | None) -> str:
    """把 intervention_kind 的返回文案映射为相对稳定的验证类型。"""
    if kind == "登录墙":
        return "login"
    if kind in ("滑块", "物品识别", "图片验证"):
        return "slider"
    return "none"


def _resolved(page) -> bool:
    """解决判定：验证弹窗/iframe 不再可见，且不处于登录墙，即认为已解决。"""
    try:
        url = (page.url or "").lower()
        if "login.taobao" in url or "login.1688" in url:
            return False
        if _is_punish_url(url):
            return False
        return not _captcha_visible(page)
    except Exception:
        return False


def wait_for_resolution(page, minutes: int, emit=None, verification_type: str | None = None,
                        confirm_sec: float = 2.0) -> None:
    """需要人工介入时：先过确认窗口过滤瞬时报错信号，再持续响铃直到解决。"""
    vtype = verification_type or "slider"
    # 确认窗口：短暂出现又自行消失的信号（如 tmd/x5sec 上报）不算真正的人工介入
    confirm_deadline = time.time() + max(0.0, confirm_sec)
    while time.time() < confirm_deadline:
        if _resolved(page):
            log.debug("人工介入信号瞬时就消失，判定为误报，忽略")
            return
        time.sleep(0.3)
    # 超过确认窗口仍未解决 => 确认为真正需要人工介入
    # 先尝试一次刷新：反爬拦截页/瞬时 block 常可通过刷新解除，刷新后恢复则不响铃
    try:
        page.reload(wait_until="domcontentloaded")
        time.sleep(1.5)
    except Exception:
        pass
    if _resolved(page):
        log.info("刷新后已恢复，忽略（原为瞬时报错/反爬拦截）")
        return
    log.warning("检测到需要人工介入，请在 Edge 窗口处理（持续响铃直到解决）……最长 %s 分钟", minutes)
    if emit:
        emit("verification_appear", kind="verification", verification_type=vtype,
             note=f"type={vtype}")
    appear_ts = time.time()
    deadline = time.time() + minutes * 60
    while True:
        if _resolved(page):
            if emit:
                emit("verification_solved", kind="verification", verification_type=vtype,
                     note=f"resolution_seconds={time.time() - appear_ts:.1f}")
            log.info("人工介入已解决，停止响铃，继续。")
            return
        if time.time() > deadline:
            raise RuntimeError("人工介入超时")
        sound.play_alarm(count=1)   # 每次约 1 秒，循环播放
        time.sleep(3)


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
        """shop 作用域事件：统一注入 shop_key + phase；传入的 offer_id/note 等保留。"""
        if emit is not None:
            emit(event, shop_key=shop.key, phase="listing", **kw)

    def on_response(resp):
        try:
            if resp.request.resource_type in ("xhr", "fetch") and _is_punish_url(resp.url):
                punished[0] = True
        except Exception:
            pass

    page.on("response", on_response)
    page.goto(shop.url, wait_until="domcontentloaded")
    time.sleep(4)
    se("list_load", note=shop.url)
    if _click_text_in_frames(page, "销量"):
        time.sleep(3)
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
            try:
                page.mouse.wheel(0, 6000)
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.2)
            except Exception:
                break
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
                    continue
            img = page.locator(_PRODUCT_IMG_SEL).nth(i)
            _capture_card(page, img, list_title, cfg, punished, on_response, se, db,
                          round_id, shop, offers, seen, idx=i, page_no=pages_read,
                          human=human, deny_tracker=deny_tracker)

        if pages_read >= max_pages:
            break
        advanced = _click_text_in_frames(page, "下一页")
        if not advanced:
            advanced = _click_text_in_frames(page, "加载更多")
        if not advanced:
            log.info("店铺 %s 第 %s 页后无下一页/加载更多，提前结束", shop.key, pages_read)
            break
        page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
        time.sleep(2)

    # 第二遍：计数>1 的同名商品，按名找回并补抓（offer_id 去重，避免重复/遗漏）
    ambiguous = {n for n, c in name_counter.items() if c > 1}
    if ambiguous:
        log.info("店铺 %s 发现 %s 个同名商品名，回头补抓", shop.key, len(ambiguous))
        page.goto(shop.url, wait_until="domcontentloaded")
        time.sleep(4)
        if _click_text_in_frames(page, "销量"):
            time.sleep(3)
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
                try:
                    page.mouse.wheel(0, 6000)
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(1.2)
                except Exception:
                    break
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
            advanced = _click_text_in_frames(page, "下一页")
            if not advanced:
                advanced = _click_text_in_frames(page, "加载更多")
            if not advanced:
                log.info("店铺 %s 补抓在第 %s 页后无下一页，提前结束", shop.key, rpg)
                break
            page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
            time.sleep(2)
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
                    se("click_deny", note=cnote + f"&n={per_product_denies}&round_abort")
                    raise RoundDenyExceeded(
                        f"整轮 {cfg.deny_window_minutes} 分钟内 deny≥{cfg.deny_round_limit}")
                if deny_tracker.shop_count(shop.key) >= cfg.deny_shop_limit:
                    se("click_deny", note=cnote + f"&n={per_product_denies}&shop_skip")
                    raise ShopDenyExceeded(
                        f"店铺 {shop.key} {cfg.deny_window_minutes} 分钟内 deny≥{cfg.deny_shop_limit}")
            log.warning("店铺 %s 商品命中 deny（该商品第 %s 次）", shop.key, per_product_denies)
            if per_product_denies == 1:
                se("click_deny", note=cnote + "&n=1")
                _close_popup_or_back(detail_page, popup, page)
                if human is not None:
                    human.sleep(cfg.deny_backoff_sec)
                continue
            if per_product_denies == 2:
                se("click_deny", note=cnote + "&n=2")
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
    se("click_deny", note=cnote + "&n=3&scan")
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
    url = detail_page.url
    m = re.search(r"/(?:offer|item)/(\d+)\.html", url)
    if not m:
        se("click_url_notoffer", note=cnote)
        _close_popup_or_back(detail_page, popup, page)
        return None
    oid = m.group(1)
    se("popup_open", offer_id=oid)
    if db and round_id and db.inventory_exists(shop.key, oid, cst_date()):
        se("click_skipped", offer_id=oid, note=cnote + "&offer_id=" + oid)
        se("skip_existing", offer_id=oid, note="inventory_exists_today")
        _close_popup_or_back(detail_page, popup, page)
        return None
    first_time = oid not in seen
    if first_time:
        seen.add(oid)
        offers.append((len(offers) + 1, oid, url, list_title or "", ""))
        log.info("命中商品 %s（累计 %s）", oid, len(offers))
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
            time.sleep(1)
        except Exception:
            pass
