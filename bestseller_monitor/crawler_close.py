"""Normal-close coordination; stopping remains owned by the existing Api/StopWatch."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from .stop_request import StopTarget
from .views import stop_failure_text

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloseSnapshot:
    kind: str
    target: StopTarget | None = None
    round_id: int | None = None
    error: str | None = None


CONFIRM_TEXT = ("采集正在进行中。\n\n"
                "关闭将中止本轮采集，效果与‘中止本轮（放弃）’相同。"
                "已提交的采集数据保留，本轮不可续跑。\n\n"
                "按‘确定’＝中止并关闭；按‘取消’＝保留窗口。")


def install_close_guard(window, api, *, confirm, notify, title,
                        launch=None, wait=time.sleep):
    """Install one native closing handler; injectable scheduling is an OS boundary."""
    if launch is None:
        def launch(work):
            threading.Thread(target=work, daemon=True,
                             name="crawler-close-teardown").start()
    lock = threading.RLock()
    confirming = False
    working = False
    allow_close = False
    authorized = None

    def report(message):
        log.warning("关闭未完成：%s", message)
        try:
            window.set_title(f"{title}（关闭未完成：{message}）")
            notify(message)
        except Exception:
            log.exception("无法显示关闭失败提示；窗口仍保留")

    def finish():
        nonlocal working, allow_close
        last_error = None

        def show_retry(message):
            # Error display must neither kill the driver nor block it on a modal.
            log.warning("关窗等待：%s；继续核验与收尾", message)
            try:
                window.set_title(f"{title}（关闭未完成：{message}；正在重试）")
                return True
            except Exception:
                log.exception("无法更新关闭等待标题；继续驱动收尾")
                return False

        try:
            while True:
                try:
                    # Drive the same watch independently of page/JS polling.
                    state = api.close_snapshot()
                    if state.target is not None and state.target != authorized.target:
                        report("采集目标已变化，请重新确认关闭。")
                        return
                    if state.error and state.error != last_error:
                        log.warning("停止监督状态码：%s", state.error)
                        if show_retry(stop_failure_text(state.error)):
                            last_error = state.error
                    if api.close_ready(authorized):
                        with lock:
                            allow_close = True
                        try:
                            window.destroy()
                        except Exception:
                            with lock:
                                allow_close = False
                            raise
                        return
                except Exception as exc:
                    # A transient database/read/UI failure is not teardown completion.
                    # Keep the frozen authorization and original watch alive.
                    show_retry(f"中止或收尾暂未完成：{exc}")
                wait(0.2)
        except Exception as exc:
            report(f"中止或收尾失败，请重试：{exc}")
        finally:
            with lock:
                working = False

    def on_closing():
        nonlocal confirming, working, allow_close, authorized
        with lock:
            if allow_close:
                try:
                    if api.close_ready(authorized):
                        return True
                except Exception:
                    log.exception("最终关窗核验失败")
                allow_close = False
                report("采集状态已变化或暂不可核验，请重新确认关闭。")
                return False
            if confirming or working:
                return False
            confirming = True
        try:
            state = api.close_snapshot()
            if state.kind == "paused":
                report("本轮处于暂停或暂停收尾状态，请先中止本轮后再关闭。")
                return False
            if state.kind == "unknown":
                report("暂时无法确认采集身份，请稍后重试。")
                return False
            if state.kind == "running" and not confirm(CONFIRM_TEXT):
                return False
            result = api.begin_close(state)
            if not result["ok"]:
                report(result["error"])
                return False
            authorized = state
            if state.kind == "idle":
                return api.close_ready(state)
            try:
                window.set_title(f"{title}（正在中止采集，请稍候…）")
            except Exception:
                log.exception("无法更新初始关闭等待标题；仍启动独立收尾")
            with lock:
                working = True
            try:
                launch(finish)
            except Exception:
                with lock:
                    working = False
                raise
            return False
        except Exception as exc:
            report(f"无法完成关窗确认或中止，请重试：{exc}")
            return False
        finally:
            with lock:
                confirming = False

    window.events.closing += on_closing
