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

# Keep the alarm pattern deliberately fixed.  It is a product behaviour rather
# than a per-machine tuning knob: the longer tones are needed when the operator
# is away from the screen or there is ordinary background noise.
_ALARM_TONES = ((1200, 800), (650, 800), (1200, 800))


def configure(enabled: bool) -> None:
    global _enabled
    _enabled = bool(enabled)


def play_alarm(count: int = 3) -> bool | None:
    """播放近似火警的高低音。

    返回 ``None`` 表示警报未启用，``True`` 表示所有音调都已提交播放，
    ``False`` 表示播放失败。失败仍然不阻塞主流程；调用方可据此记录现场
    是否至少尝试过播放。
    """
    if not _enabled:
        return None
    try:
        import winsound

        for _ in range(count):
            for frequency, duration_ms in _ALARM_TONES:
                winsound.Beep(frequency, duration_ms)
            time.sleep(0.08)
    except Exception as exc:  # pragma: no cover - 无声环境
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass
        log.debug("无法播放警报音：%s", exc)
        return False
    return True
