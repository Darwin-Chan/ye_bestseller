"""人工干预时播放警报音（火警式蜂鸣）。

默认**不响**：只有显式 `configure(True)` 之后才出声。采集入口 `run.py` 会按
`config.toml` 的 `alarm_on_intervention` 配置它；测试与一次性工具什么都不配，
于是任何测试路径撞进「人工介入等待」时都不会去吵用这台机器的人。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

_enabled = False


def configure(enabled: bool) -> None:
    global _enabled
    _enabled = bool(enabled)


def play_alarm(count: int = 3) -> None:
    """播放近似火警的急促高低音。失败时静默（不阻塞主流程）。"""
    if not _enabled:
        return
    try:
        import winsound

        for _ in range(count):
            winsound.Beep(1200, 320)   # 高音
            winsound.Beep(650, 260)    # 低音
            winsound.Beep(1200, 320)
            time.sleep(0.08)
    except Exception as exc:  # pragma: no cover - 无声环境
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass
        log.debug("无法播放警报音：%s", exc)
