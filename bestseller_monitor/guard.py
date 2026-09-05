"""滑块 / 登录墙等人工干预页识别。"""
from __future__ import annotations

import logging
import time

from . import sound

log = logging.getLogger(__name__)

SLIDER_MARKERS = (
    "向右滑动验证",
    "请完成验证",
    "滑块验证",
    "拖动滑块",
    "安全验证",
)

LOGIN_MARKERS = (
    "登录后查看",
    "请登录",
    "扫码登录",
    "登录 1688",
)


def detect(page) -> str | None:
    """返回 '滑块' / '登录墙' / None。page 为 Playwright Page。"""
    try:
        url = page.url.lower()
    except Exception:
        url = ""
    if "login.1688.com" in url or "login.taobao.com" in url:
        return "登录墙"
    try:
        body = page.locator("body").inner_text(timeout=3000) or ""
    except Exception:
        body = ""
    for m in SLIDER_MARKERS:
        if m in body:
            return "滑块"
    for m in LOGIN_MARKERS:
        if m in body and len(body) < 3000:
            return "登录墙"
    try:
        nc = page.locator("#nc_1_n1z, .nc_iconfont, [id^='nc_1_']")
        if nc.count() > 0 and nc.first.is_visible():
            return "滑块"
    except Exception:
        pass
    return None


def wait_for_human(page, kind: str, minutes: int) -> None:
    """出现滑块/登录墙时，暂停并等待人工处理。"""
    log.warning(
        "检测到【%s】。请在打开的浏览器窗口内人工处理（扫码/滑块），会持续响铃直到解决。", kind,
    )
    deadline = time.time() + minutes * 60
    while True:
        try:
            if detect(page) is None:
                log.info("人工介入已解决，停止响铃。")
                return
        except Exception:
            pass
        if time.time() > deadline:
            raise RuntimeError(f"人工处理超时（{kind}），请稍后重新运行续跑。")
        sound.play_alarm(count=1)
        time.sleep(3)
