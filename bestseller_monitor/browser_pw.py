"""采集驱动：Playwright 连接用普通进程拉起的浏览器（connect_over_cdp）。

浏览器被平台当成「人启动」的，新建标签页后 network 事件可靠，能拦截
getShopOfferList / mtop 等 XHR。这里是唯一的采集路径（ADR-0010）。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time

from playwright.sync_api import Error as PlaywrightError

from . import browser_proc, dedupe, detail, listing, rounds
from .config import Config, Shop, effective_pages_limit
from .db import cst_date, utcnow
from .delay import Humanizer
from .detail import DetailParseFailed, parse_detail_html
from .guard import (
    RoundPauseRequired,
    body_text, captcha_visible, intervention_kind,
    is_deny_url, is_punish_url, resolved, vtype, wait_for_resolution,
)

log = logging.getLogger(__name__)

# 记录本次由 open_session 启动的浏览器进程与调试端口：
# 收尾只结束本任务启动的浏览器，绝不波及用户其它 Edge 窗口。
_launched_proc = None
_launched_port = None

# 兼容旧私有名/旧名（本文件内部与诊断工具仍引用）
_body_text = body_text
_captcha_visible = captcha_visible
_is_punish_url = is_punish_url
_is_deny_url = is_deny_url
_resolved = resolved
_vtype = vtype

# 条件等待的上限兜底（秒）——优先“等条件满足”，超时才继续，替代固定 sleep。
_WAIT_LAUNCH_SEC = 25.0    # 等浏览器调试端口可连接
_WAIT_SCROLL_SEC = 3.0     # 每次滚动后等新一批卡片
_WAIT_BACK_SEC = 8.0       # 返回上一页后等就绪
_WAIT_POPUP_MS = 2500      # 点击后等待新标签页；有效弹窗通常在 2 秒内出现

# 搬到 listing.py 的榜单页原语：诊断工具仍按旧私有名引用，这里留兼容别名（同 guard 那组）。
# 本文件内部一律走 listing.*，别名只给工具用——否则测试打桩 listing 的改动会静默失效
# （2026-09-13 被这条咬过：测试改打 listing，采集路径却用别名，于是真的走进人工介入等待）。
_PRODUCT_IMG_SEL = listing.PRODUCT_IMG_SEL
_click_text_in_frames = listing.click_text_in_frames


def _scroll_cards_until_stable(page, describe: str = "滚动加载新卡片") -> int:
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
            _WAIT_SCROLL_SEC,
        )
        cur = page.locator(listing.PRODUCT_IMG_SEL).count()
        if not loaded or cur <= baseline:
            break
    return page.locator(listing.PRODUCT_IMG_SEL).count()


def open_session(cfg: Config):
    global _launched_proc, _launched_port
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
    _launched_port = cfg.attach_port

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


def cdp_browser_pid(br) -> int | None:
    """问 CDP 要真正在跑的那个 browser 进程 PID；取不到返回 None。"""
    if br is None:
        return None
    try:
        session = br.new_browser_cdp_session()
        info = session.send("SystemInfo.getProcessInfo")
    except Exception as exc:  # noqa: BLE001
        log.debug("通过 CDP 查询浏览器进程失败：%s", exc)
        return None
    for proc in info.get("processInfo", []):
        if proc.get("type") == "browser" and proc.get("id"):
            return int(proc["id"])
    return None


def close_session(pw, br) -> None:
    global _launched_proc, _launched_port
    # 先问 CDP，再断开：交接场景里这个 PID 才是真正在跑的浏览器。
    browser_pid = cdp_browser_pid(br)
    try:
        br.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
    proc = _launched_proc
    port = _launched_port
    _launched_proc = None
    _launched_port = None
    if proc is None:
        log.info("本次未启动浏览器（接管既有实例），跳过关闭。")
        return
    own_pid = proc.pid if proc.poll() is None else None
    if own_pid is None:
        log.warning("本次启动的浏览器进程（PID %s）已退出：同一 profile 已有实例时会交接给旧实例；"
                    "改按调试端口 %s 的归属关闭。", proc.pid, port)
    browser_proc.close_browser(port, launched_by_us=True,
                               browser_pid=browser_pid, own_pid=own_pid)


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
    payload = parse_detail_html(page.content(), product_url)
    if emit:
        emit("detail_parse", phase="detail", note=f"sku_count={len(payload['rows'])}")
    return payload


def _card_ref(page_no: int, idx: int) -> str:
    """点击式列表里一张卡片的稳定标识（同一轮内可重复命中同一张卡片）。"""
    return f"card:p{page_no}:i{idx}"


def _remember_discovery(db, round_id, shop, offers: list, offer_id: str,
                        product_url: str, list_title: str) -> None:
    """把刚发现的商品计入本轮榜单：内存清单 + 立即落一条榜单行。

    商品在列表遍历途中一经发现就落库，因此无论以哪种方式中途离开榜单阶段，
    已写入的快照都有榜单行可对应，不会变成孤儿。没有数据库句柄时只记内存。
    """
    offers.append((len(offers) + 1, offer_id, product_url, list_title, ""))
    if db is not None and round_id is not None:
        db.remember_shop_offer(round_id, shop.key, shop.url, shop.name, offers[-1])


def _claim_card_slot(db, round_id, shop: Shop, cfg: Config, card_ref: str) -> None:
    """向同日去重与补采 module 申请一次详情机会，预算耗尽时结束本轮。

    点击式列表在打开卡片前还拿不到商品编号，因此这里用卡片位置作为机会标识；
    同一轮里重复扫到同一张卡片只会复用机会，不重复占用预算。
    已发现的商品在发现时就已落库，预算耗尽只需结束本轮。
    """
    if db is None or round_id is None:
        return
    dedupe.claim_card_slot(db, round_id, shop.key, card_ref,
                           cfg.max_detail_opportunities_per_round)


def crawl_store_by_click(page, shop: Shop, cfg: Config, human: Humanizer,
                         db=None, round_id=None, emit=None, deny_tracker=None):
    """商品列表用「点击商品图进详情」的方式收集商品，绕开拿不到URL的问题。"""
    log.info("开始点击式抓取店铺 %s（%s）", shop.key, shop.url)
    max_pages = effective_pages_limit(shop, cfg)
    offers: list[tuple[int, str, str, str, str]] = []
    seen: set[str] = set()
    name_counter: dict[str, int] = {}
    name_pages: dict[str, set[int]] = {}
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
    # 打开并准备好榜单页：等首屏卡片 → 人工介入 → 点「销量」排序（榜单页的行为在 listing）。
    listing.prepare(page, shop.url, cfg, human, describe=f"店铺 {shop.key} 首屏",
                    punished=punished[0], emit=se)
    while pages_read < max_pages:
        pages_read += 1
        human.before_list_page()
        se("list_page", note=f"page={pages_read}")
        # 滚动到底部触发懒加载，直到图片数量不再增加
        n = _scroll_cards_until_stable(page)
        log.info("店铺 %s 第 %s 页图片总数 %s（滚动后）", shop.key, pages_read, n)

        for i in range(n):
            se("product_open")
            list_title = _read_card_title(page, i)   # 点前读列表页商品名
            if list_title:
                name_counter[list_title] = name_counter.get(list_title, 0) + 1
                c = name_counter[list_title]
                name_pages.setdefault(list_title, set()).add(pages_read)
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
                            _remember_discovery(db, round_id, shop, offers,
                                                def_oid, def_url, list_title)
                        db.mark_skipped(round_id, shop.key, shop.url, shop.name, def_oid,
                                        def_url, list_title, note="今日已有同名库存，跳过")
                    continue
            # 只有确认需要进入详情后才消耗详情间隔和长停顿预算。
            _claim_card_slot(db, round_id, shop, cfg, _card_ref(pages_read, i))
            human.before_detail()
            img = page.locator(listing.PRODUCT_IMG_SEL).nth(i)
            _capture_card(page, img, list_title, cfg, punished, on_response, se, db,
                          round_id, shop, offers, seen, idx=i, page_no=pages_read,
                          human=human, deny_tracker=deny_tracker)

        if pages_read >= max_pages:
            break
        # 推进到下一批：换了内容才继续读（IS-36）；没有下一批就收工。
        if not listing.advance(page, human, cfg, f"店铺 {shop.key} 第 {pages_read} 页"):
            break

    # 第二遍：计数>1 的同名商品，按名找回并补抓（offer_id 去重，避免重复/遗漏）
    ambiguous = {n for n, c in name_counter.items() if c > 1}
    if ambiguous:
        ambiguous_pages = set().union(*(name_pages[n] for n in ambiguous))
        rescue_last_page = max(ambiguous_pages)
        log.info("店铺 %s 发现 %s 个同名商品名，回头补抓第 %s 页（共 %s 页）",
                 shop.key, len(ambiguous), ",".join(map(str, sorted(ambiguous_pages))), rescue_last_page)
        # 补抓第二遍与主页走同一份准备：顺序、兜底、事件都不再各写一遍。
        listing.prepare(page, shop.url, cfg, human, describe=f"店铺 {shop.key} 补抓首屏",
                        punished=punished[0], emit=se)
        for rpg in range(1, rescue_last_page + 1):
            human.before_list_page()
            se("list_page", note=f"rescue_page={rpg}")
            if rpg in ambiguous_pages:
                n = _scroll_cards_until_stable(page, "补抓滚动加载新卡片")
                for i in range(n):
                    name = _read_card_title(page, i)
                    if name and name in ambiguous:
                        _claim_card_slot(db, round_id, shop, cfg, _card_ref(rpg, i))
                        human.before_detail()
                        se("product_open")
                        img = page.locator(listing.PRODUCT_IMG_SEL).nth(i)
                        _capture_card(page, img, name, cfg, punished, on_response, se, db,
                                      round_id, shop, offers, seen, idx=i, page_no=rpg,
                                      human=human, deny_tracker=deny_tracker)
            if rpg >= rescue_last_page:
                break
            # 补抓翻页走同一道推进，确认规则不另写一份（IS-36）。
            if not listing.advance(page, human, cfg, f"店铺 {shop.key} 补抓第 {rpg} 页"):
                break
    if not offers:
        raise listing.listing_load_failed(page, f"店铺列表未解析到商品：{shop.url}")
    return offers, pages_read


def _click_one_product(page, img, cfg, punished, on_response, emit=None):
    """点击单个商品图的“可点击父元素”；返回 (detail_page, popup)。"""
    popup = None
    try:
        with page.expect_popup(timeout=_WAIT_POPUP_MS) as pi:
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
        kind = intervention_kind(popup, punished[0])
        if kind:
            wait_for_resolution(popup, cfg.human_pause_minutes, emit=emit,
                                verification_type=_vtype(kind),
                                confirm_sec=cfg.intervention_confirmation_sec)
        return popup, popup

    if "detail.1688.com/offer/" not in (page.url or ""):
        return None, None
    try:
        page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
    except PlaywrightError:
        return None, None
    kind = intervention_kind(page, punished[0])
    if kind:
        wait_for_resolution(page, cfg.human_pause_minutes, emit=emit,
                            verification_type=_vtype(kind),
                            confirm_sec=cfg.intervention_confirmation_sec)
    return page, None


def _read_card_title(page, idx: int) -> str:
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


def _capture_card(page, img, list_title, cfg, punished, on_response, se, db, round_id, shop,
                  offers, seen, idx: int = 0, page_no: int = 0, human=None,
                  deny_tracker=None) -> str | None:
    """点开卡片弹出、offer_id 去重、读 SKU、入库；成功返回 offer_id，否则返回 None。
    deny 处理：第1/2次退避重试；第3次关闭详情并跳过当前商品。
    可能抛 ShopDenyExceeded / RoundDenyExceeded。"""
    cnote = f"page={page_no}&idx={idx}"
    per_product_denies = 0
    for _ in range(3):
        # 打开卡片就是进详情：先问轮次，跨天或已过截止线就不再开始。
        if db and round_id:
            rounds.ensure_workable(db, round_id, utcnow())
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
            se("click_deny", phase="detail", note=cnote + "&n=3&skip")
            log.warning("店铺 %s 商品第 3 次命中 deny，跳过当前商品", shop.key)
            _close_popup_or_back(detail_page, popup, page)
            return None
        return _ingest_detail(page, detail_page, popup, list_title, cfg, punished, on_response,
                              se, db, round_id, shop, offers, seen, cnote,
                              human=human, card_ref=_card_ref(page_no, idx))
    return None


def _ingest_detail(page, detail_page, popup, list_title, cfg, punished, on_response, se, db,
                   round_id, shop, offers, seen, cnote, human=None,
                   card_ref: str | None = None) -> str | None:
    """点击路径的详情 adapter 与事件：页面动作在这里，规则在 detail.capture_observation。

    - adapter 负责读弹窗内容并关掉它；规则（同日去重、机会与绑定、额度、提交、失败记录）
      与逐店补采共用一份实现（候选 02 / ADR-0013）。
    - 事件由这里按结果记：`event_log` 的内容与改前一致，工具的统计口径不变。
      唯一的例外是「提交后立刻跨天」那一瞬——采集进程会先抛终止，`click_ok` 不再补发。
    - 同一次遍历里的 `seen` 去重与「立刻落榜单行」仍在这里，那是遍历的事。
    """
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
        _remember_discovery(db, round_id, shop, offers, oid, url, list_title or "")
        log.info("命中商品 %s（累计 %s）", oid, len(offers))

    def read() -> detail.Observation:
        try:
            html = detail_page.content()
        except Exception as exc:
            return detail.Observation(failure=f"详情页读取失败：{exc}")
        try:
            payload = parse_detail_html(html, url)
        except DetailParseFailed as exc:
            return detail.Observation(failure=f"解析失败：{exc}", raw_html=exc.html)
        except Exception as exc:
            return detail.Observation(failure=f"详情页解析异常：{exc}", raw_html=html)
        se("detail_parse", offer_id=oid, note=f"sku_count={len(payload['rows'])}")
        return detail.Observation(payload=payload)

    def on_attempt_failed(note: str) -> None:
        se("click_parse_error", offer_id=oid, note=cnote + "&offer_id=" + oid)
        se("detail_parse", offer_id=oid, note="sku_count=0")

    try:
        result = detail.capture_observation(
            db, cfg, human, round_id,
            detail.DetailTarget(
                shop_key=shop.key, shop_url=shop.url, shop_name=shop.name,
                product_url=url, slot_key=card_ref or oid, offer_id=oid,
                list_title=list_title, duplicate=not first_time),
            read, attempts=1, on_attempt_failed=on_attempt_failed)
    finally:
        se("popup_close", offer_id=oid)
        _close_popup_or_back(detail_page, popup, page)

    if result.outcome is detail.Outcome.SKIPPED_TODAY:
        # 今天已采过：仍把该商品计入本轮榜单，跳过不快照成失败也不消耗详情预算。
        se("click_skipped", offer_id=oid, note=cnote + "&offer_id=" + oid)
        se("skip_existing", offer_id=oid, note="inventory_exists_today")
    elif result.outcome is detail.Outcome.SUBMITTED:
        skus = result.sku_count if result.sku_count is not None else 0
        se("click_ok", offer_id=oid, note=cnote + "&offer_id=" + oid + f"&sku={skus}")
    elif result.outcome is detail.Outcome.DUPLICATE:
        se("click_skipped", offer_id=oid, note=cnote + "&offer_id=" + oid + "&dup=1")
    # 失败与「没读到编号」一样：对调用方来说这张卡没有拿到商品（只留了失败行）。
    return None if result.outcome is detail.Outcome.FAILED else result.offer_id


def _close_popup_or_back(detail_page, popup, page):
    if popup:
        try:
            popup.close()
        except Exception:
            pass
    elif detail_page is page:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            listing.wait_cards(page, min_count=1, timeout_sec=_WAIT_BACK_SEC,
                               describe="返回列表页商品卡片")   # 条件等待，替代固定 1s
        except Exception:
            pass
