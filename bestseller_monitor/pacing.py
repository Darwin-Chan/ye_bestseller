"""主动停顿：按「页」配额给站点降温（ADR-0037）。

一轮里的详情访问凑满配额就停一次（默认 6 页 → 25–35 分钟）。配额按**详情当量**记账，
两条路径都折进同一个整数计数器：

- **列表/补抓阶段**：每开始一页记 `DETAILS_PER_PAGE`（一页约 30 张卡，round_35 实测）；
- **补采阶段**：每开始一个详情记 1。

所以「一页 = 30 个详情」只在这一处定义，两条路径的节奏才可比。

触发是**惰性**的：到点只挂标记，在下一次「开始新的一页/新的详情」时才停——最后一批之后
没有后续工作就不停，也就不会白等，更不会因此把一轮完成拖成跨天中止。

停顿本身借 `Humanizer.sleep` 的分片：停止请求、跨天边界、当日截止线都能在一片（0.5 秒）
内打断。停顿事件的编码也由本模块一处持有——界面按同一份口径算倒计时与累计。
"""
from __future__ import annotations

import logging
import random

log = logging.getLogger(__name__)

# 一页商品卡片的折算数：round_35 实测每页 30 张（A01 第 2–6 页各 30 张卡）。
DETAILS_PER_PAGE = 30

# 停顿事件的编码（界面读同一份口径）：
#   pacing_pause  note=sec=<计划停顿秒数>；pacing_resume  note=sec=<实际停顿秒数>
PAUSE_EVENT = "pacing_pause"
RESUME_EVENT = "pacing_resume"
_PACING_PHASE = "pacing"


def pause_note(seconds: float) -> str:
    return f"sec={int(round(seconds))}"


def note_seconds(note: str | None) -> float | None:
    """从停顿事件备注里读回秒数；读不出来给 None（界面据此放弃倒计时，不猜）。"""
    for part in (note or "").split("&"):
        key, _, value = part.partition("=")
        if key == "sec":
            try:
                return float(value)
            except ValueError:
                return None
    return None


class Pacing:
    """一轮的那一个配额账本：两条路径共用，跨店累计（ADR-0037）。"""

    def __init__(self, cfg, human, emit=None):
        self.cfg = cfg
        self.human = human
        self.emit = emit
        self._since_pause = 0   # 距上次停顿累计的「详情当量」
        if self.quota <= 0:
            log.info("主动停顿已关闭（pause_every_pages = 0）")
        else:
            log.info("主动停顿：每 %s 页停一次，每次 %.0f–%.0f 秒",
                     cfg.pause_every_pages, cfg.pause_sec[0], cfg.pause_sec[1])

    @property
    def quota(self) -> int:
        """一次停顿要凑满的详情当量；0 = 这条规则关闭。"""
        return self.cfg.pause_every_pages * DETAILS_PER_PAGE

    def page_started(self, *, shop_key: str | None = None) -> None:
        """列表/补抓阶段：新的一页就要开始（先查停顿，再把这一页记进配额）。"""
        self._maybe_pause(shop_key=shop_key)
        self._since_pause += DETAILS_PER_PAGE

    def detail_started(self, *, shop_key: str | None = None) -> None:
        """补采阶段：一个详情就要开始。"""
        self._maybe_pause(shop_key=shop_key)
        self._since_pause += 1

    def _maybe_pause(self, *, shop_key: str | None) -> None:
        if self.quota <= 0 or self._since_pause < self.quota:
            return
        seconds = random.uniform(*self.cfg.pause_sec)
        self._since_pause = 0
        self._record(PAUSE_EVENT, seconds, shop_key)
        log.info("主动停顿 %.1f 秒（已凑满 %s 页配额）", seconds,
                 self.cfg.pause_every_pages)
        self.human.sleep(seconds)
        self._record(RESUME_EVENT, seconds, shop_key)
        log.info("主动停顿结束，继续")

    def _record(self, event: str, seconds: float, shop_key: str | None) -> None:
        if self.emit is None:
            return
        self.emit(event, shop_key=shop_key, phase=_PACING_PHASE,
                  note=pause_note(seconds))
