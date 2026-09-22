"""主动停顿：按「详情访问」配额给站点降温（ADR-0038）。

一轮里**真正打开了商品详情页**的访问每凑满配额就停一次（默认 180 次 → 25–35 分钟）。
翻列表页不记账：跳过密集的一天里，一页 30 张卡可能一张详情都没开（round_36 实测 180 张卡
里 171 张是同名暂缓），为没发生的工作付停顿是改前的毛病。

记账分两步，因为「这一次访问会不会真的打开页面」只有打开之后才知道：

- `before_detail_visit()`：要开详情之前问一次——配额到点就停在这里，请求还没发出；
- `detail_visit_opened()`：页面真的打开之后再记 1。

两步在同一条路径上相邻发生（点击路径在 `capture()` 的重试循环里，补采路径在 `observe()` 里），
于是停顿仍然落在两次详情访问之间。触发是**惰性**的：凑满之后没有下一次详情访问，就永远不白停，
也不会因此把一轮完成拖成跨天中止。

停顿本身借 `Humanizer.sleep` 的分片：停止请求、跨天边界、当日截止线都能在一片（0.5 秒）内
打断。停顿事件的编码也由本模块一处持有——界面按同一份口径算倒计时与累计。
"""
from __future__ import annotations

import logging
import random

log = logging.getLogger(__name__)

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
    """一轮的那一个配额账本：两条路径共用，跨店累计（ADR-0038）。"""

    def __init__(self, cfg, human, emit=None):
        self.cfg = cfg
        self.human = human
        self.emit = emit
        self._since_pause = 0   # 距上次停顿累计的详情访问次数
        if self.quota <= 0:
            log.info("主动停顿已关闭（pause_every_detail_visits = 0）")
        else:
            log.info("主动停顿：每 %s 次详情访问停一次，每次 %.0f–%.0f 秒",
                     self.quota, cfg.pause_sec[0], cfg.pause_sec[1])

    @property
    def quota(self) -> int:
        """一次停顿要凑满的详情访问次数；0 = 这条规则关闭。"""
        return self.cfg.pause_every_detail_visits

    def before_detail_visit(self, *, shop_key: str | None = None) -> None:
        """一次详情访问就要开始：配额到点就先停在这里——页面还没打开，请求也就还没发出。"""
        if self.quota <= 0 or self._since_pause < self.quota:
            return
        seconds = random.uniform(*self.cfg.pause_sec)
        self._since_pause = 0
        self._record(PAUSE_EVENT, seconds, shop_key)
        log.info("主动停顿 %.1f 秒（已凑满 %s 次详情访问配额）", seconds, self.quota)
        self.human.sleep(seconds)
        self._record(RESUME_EVENT, seconds, shop_key)
        log.info("主动停顿结束，继续")

    def detail_visit_opened(self) -> None:
        """详情页真的到手了：这一次访问记进配额（命中 deny 的拦截页也算一次访问）。"""
        if self.quota <= 0:
            return
        self._since_pause += 1

    def _record(self, event: str, seconds: float, shop_key: str | None) -> None:
        if self.emit is None:
            return
        self.emit(event, shop_key=shop_key, phase=_PACING_PHASE,
                  note=pause_note(seconds))
