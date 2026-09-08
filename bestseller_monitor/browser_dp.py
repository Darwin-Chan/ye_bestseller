"""DrissionPage 驱动：真实 Chrome 控制，社区验证可过 1688 登录。"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time

from .config import Config, Shop
from .delay import Humanizer
from .detail import parse_detail_html
from . import sound
from .guard import (
    InterventionTimeout,
    SLIDER_MARKERS,
    LOGIN_MARKERS,
    is_login_url,
    is_punish_url,
    is_deny_url,
)
from .listing import ListingLoadFailed

log = logging.getLogger(__name__)

# 记录本次由 create_page 启动的浏览器进程，收尾只结束它。
_proc = None

def create_page(cfg: Config):
    global _proc
    from DrissionPage import ChromiumOptions, ChromiumPage

    if getattr(cfg, "start_browser", True):
        # 用普通进程启动浏览器（不经过 DrissionPage），携带远程调试端口，
        # 让服务器认为这是“人启动”的浏览器，避免会话被降级。
        edge = cfg.chrome_path or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        if os.path.exists(edge):
            _proc = subprocess.Popen([
                edge,
                f"--remote-debugging-port={cfg.attach_port}",
                f"--user-data-dir={cfg.user_data_path}",
                "--no-first-run",
                "--no-default-browser-check",
                cfg.base_url,
            ])
            log.info("已用普通进程启动浏览器（调试端口 %s，PID %s）。", cfg.attach_port, _proc.pid)
        else:
            log.warning("未找到浏览器路径，尝试由 DrissionPage 拉取：%s", edge)

    co = ChromiumOptions()
    try:
        co.set_address(f"127.0.0.1:{cfg.attach_port}").existing_only(True)
    except Exception:
        co.auto_port()
    if not getattr(cfg, "start_browser", True) and cfg.chrome_path:
        try:
            co.set_browser_path(cfg.chrome_path)
        except Exception:
            log.warning("设置 Chrome 路径失败，使用默认浏览器。")
    if not getattr(cfg, "start_browser", True) and getattr(cfg, "use_system_profile", False):
        # 复用系统默认 Chrome 配置（通常已登录 1688），彻底避开登录墙
        try:
            co.use_system_user_path(True)
            log.info("已使用系统默认 Chrome 用户配置（请确保之前已完全退出 Chrome）。")
        except Exception:
            log.warning("无法使用系统配置，回退到独立配置目录。")
            co.set_user_data_path(str(cfg.user_data_path))
    elif not getattr(cfg, "start_browser", True):
        co.set_user_data_path(str(cfg.user_data_path))
    # 关键：不要注入 --disable-blink-features=AutomationControlled。
    # Chrome 会把它标为“不受支持的命令行标记”，反而触发阿里风控导致登录循环。
    co.remove_argument("--disable-blink-features=AutomationControlled")
    co.headless(cfg.headless)
    try:
        co.set_load_mode("normal")
    except Exception:
        pass
    page = ChromiumPage(co)
    return page


def stop_browser() -> None:
    global _proc
    proc = _proc
    _proc = None
    if proc is not None and proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            log.info("已关闭本次启动的浏览器进程（PID %s）。", proc.pid)
        except Exception as exc:
            log.debug("关闭浏览器进程失败：%s", exc)
    else:
        log.info("未由本程序启动的浏览器进程，跳过关闭。")


def _body_text(page) -> str:
    try:
        body = page.ele("tag:body")
        return (body.text or "") if body else ""
    except Exception:
        return ""


def _page_html(page) -> str:
    try:
        return page.html or ""
    except Exception:
        return ""


def detect(page) -> str | None:
    url = (getattr(page, "url", "") or "").lower()
    if is_login_url(url):
        return "登录墙"
    if is_deny_url(url):
        return None
    if is_punish_url(url):
        return "滑块"
    body = _body_text(page)
    for m in SLIDER_MARKERS:
        if m in body:
            return "滑块"
    for m in LOGIN_MARKERS:
        if m in body and len(body) < 3000:
            return "登录墙"
    return None


def wait_for_human(page, kind: str, minutes: int) -> None:
    log.warning("等待人工介入【%s】（持续响铃）……最长 %s 分钟", kind, minutes)
    deadline = time.time() + minutes * 60
    while True:
        try:
            if detect(page) is None:
                log.info("人工介入已解决，停止响铃。")
                return
        except Exception:
            pass
        if time.time() > deadline:
            raise InterventionTimeout(f"人工处理超时（{kind}），请稍后重新运行续跑。")
        sound.play_alarm(count=1)
        time.sleep(3)


def _get_offer_links(page) -> list[tuple[str, str]]:
    try:
        anchors = page.eles("tag:a")
    except Exception:
        anchors = []
    out: list[tuple[str, str]] = []
    for a in anchors:
        try:
            href = a.attr("href") or ""
        except Exception:
            continue
        m = re.search(r"/(?:offer|item)/(\d+)\.html", href)
        if m:
            out.append((m.group(1), href))
    return out


def wait_for_offers(page, seconds: int = 180) -> bool:
    """等待商品链接真正出现（通常需要人工在此窗口处理滑块验证）。"""
    # 不误报：仅在确认出现验证时才由 wait_for_human 响铃，这里静默等待商品
    deadline = time.time() + seconds
    print("\n>>> 请在刚才弹出的 Edge 窗口中完成验证/滑动；商品加载后程序会自动继续。\n")
    log.warning("等待店铺商品加载（请人工处理可能的滑块验证），最长 %s 秒……", seconds)
    while time.time() < deadline:
        time.sleep(5)
        try:
            page.scroll.to_bottom()
        except Exception:
            pass
        if _get_offer_links(page):
            log.info("已检测到商品链接，继续。")
            return True
    log.warning("超时仍未检测到商品。")
    return False


def crawl_shop_listing(
    page,
    shop: Shop,
    cfg: Config,
    human: Humanizer,
) -> tuple[list[tuple[int, str, str, str, str]], int]:
    """翻页抓店铺商品列表；返回 (offers, pages_read)。"""
    log.info("开始抓取店铺 %s（%s）", shop.key, shop.url)
    page.get(shop.url)
    page.wait.load_start()
    human.after_load()
    time.sleep(4)  # 等 x5sec 惩罚/滑块加载出来
    kind = detect(page)
    if kind:
        wait_for_human(page, kind, cfg.human_pause_minutes)
    if not _get_offer_links(page) and not wait_for_offers(page, cfg.human_pause_minutes * 60):
        raise ListingLoadFailed(
            f"店铺列表未加载出商品链接：{shop.url}（current_url={getattr(page, 'url', '')}）",
            html=_page_html(page),
        )

    try:
        btn = page.ele("text:销量", timeout=5)
        if btn:
            btn.click()
            log.info("已点击「销量」排序")
    except Exception:
        log.warning("未找到「销量」排序，沿用默认顺序（首轮请人工确认）。")

    offers: list[tuple[int, str, str, str, str]] = []
    seen: set[str] = set()
    pages_read = 0
    for _ in range(cfg.max_pages_per_shop):
        pages_read += 1
        human.before_list_page()
        kind = detect(page)
        if kind:
            wait_for_human(page, kind, cfg.human_pause_minutes)
        try:
            anchors = page.eles("tag:a")
        except Exception:
            anchors = []
        added = 0
        for a in anchors:
            try:
                href = a.attr("href") or ""
            except Exception:
                continue
            m = re.search(r"/(?:offer|item)/(\d+)\.html", href)
            if not m:
                continue
            oid = m.group(1)
            if oid in seen:
                continue
            seen.add(oid)
            try:
                text = (a.text or "").strip()[:240]
            except Exception:
                text = ""
            offers.append((len(offers) + 1, oid, href, text, ""))
            added += 1
        log.info("店铺 %s 第 %s 页新增 %s，累计 %s", shop.key, pages_read, added, len(offers))
        if pages_read >= cfg.max_pages_per_shop:
            break
        if added == 0:
            # 首页无商品且可能触发了验证：再等一次并检测
            time.sleep(3)
            kind = detect(page)
            if kind:
                wait_for_human(page, kind, cfg.human_pause_minutes)
                time.sleep(2)
        try:
            nxt = page.ele("text:下一页", timeout=4)
            if not nxt:
                log.info("店铺 %s 无下一页，提前结束", shop.key)
                break
            nxt.click()
            page.wait.load_start()
        except Exception:
            log.info("店铺 %s 翻页结束", shop.key)
            break
    if not offers:
        raise ListingLoadFailed(
            f"店铺列表未解析到商品：{shop.url}（current_url={getattr(page, 'url', '')}）",
            html=_page_html(page),
        )
    return offers, pages_read


def capture_detail_payload(page, product_url: str, cfg: Config, human: Humanizer) -> dict:
    """打开详情页并解析 SKU；失败抛 DetailParseFailed（含 html）。"""
    page.get(product_url)
    page.wait.load_start()
    human.after_load()
    kind = detect(page)
    if kind:
        wait_for_human(page, kind, cfg.human_pause_minutes)
    html = page.html
    return parse_detail_html(html, product_url)
