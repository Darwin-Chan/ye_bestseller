"""拟人化随机延迟。"""
from __future__ import annotations

import logging
import random
import time

from .config import Config
from . import stop_request

log = logging.getLogger(__name__)


class Humanizer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._next_long_pause_at = random.randint(*cfg.long_pause_interval)
        self._pages_since_long = 0

    def _rand(self, rng: tuple[float, float]) -> float:
        return random.uniform(rng[0], rng[1])

    def sleep(self, seconds: float) -> None:
        """拟人化睡眠；按片问一次「该不该停」（ADR-0009）。

        长停顿 10–20 秒、批量休息 20–40 秒、deny 退避 30/60 秒本来会让协作停止
        白等到睡完，切片把响应时间压到一片以内。没装停止检查时照旧睡一整段。
        """
        remaining = max(0.0, seconds)
        while remaining > 0:
            stop_request.check()
            piece = min(stop_request.SLICE_SEC, remaining)
            time.sleep(piece)
            remaining -= piece

    def before_detail(self) -> None:
        self._pages_since_long += 1
        self.sleep(self._rand(self.cfg.detail_delay_sec))
        if self._pages_since_long >= self._next_long_pause_at:
            pause = self._rand(self.cfg.long_pause_sec)
            log.info("拟人化长停顿 %.1f 秒", pause)
            self.sleep(pause)
            self._pages_since_long = 0
            self._next_long_pause_at = random.randint(*self.cfg.long_pause_interval)

    def before_batch_rest(self) -> None:
        rest = self._rand(self.cfg.batch_rest_sec)
        log.info("批量休息 %.1f 秒", rest)
        self.sleep(rest)

    def before_list_page(self) -> None:
        self.sleep(self._rand(self.cfg.list_delay_sec))

    def before_action(self) -> None:
        self.sleep(self._rand(self.cfg.action_delay_sec))

    def after_load(self) -> None:
        self.sleep(self._rand(self.cfg.read_delay_sec))

    def retry_delay(self, attempt: int) -> float:
        base = self.cfg.retry_base_sec * (2 ** max(0, attempt - 1))
        jitter = random.uniform(0, self.cfg.retry_jitter_sec)
        return base + jitter
