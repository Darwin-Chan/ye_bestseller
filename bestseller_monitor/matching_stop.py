"""Per-run matching stop signal and its observed request reason (ADR-0044)."""
from __future__ import annotations

import threading

BUTTON_STOP = "用户主动停止匹配"
WINDOW_CLOSE = "用户关闭窗口"


class MatchingStop:
    """Callable checkpoint shared by product and SKU work; first request wins."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = "未记录"

    def request(self, reason=None):
        with self._lock:
            if not self._event.is_set():
                self._reason = reason or "未记录"
                self._event.set()

    @property
    def reason(self):
        with self._lock:
            return self._reason

    def __call__(self):
        return self._event.is_set()


def stop_reason(stop):
    """Old boolean callbacks have no recorded request source."""
    return getattr(stop, "reason", None) or "未记录"
