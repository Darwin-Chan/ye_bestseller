"""1688 畅销品监控 · GUI 启动器/进度/结果（pywebview）。

行为约定：
  - GUI 只做启动器/监控，抓取仍由本机 `python run.py --limit-shops=...` 子进程执行。
  - GUI 读 data/bestseller.db 展示“今日/各店”数据；过程页上一次刷新回来之后才排下一次
    （节拍约 2 秒），慢查询不会把请求堆起来，暂停/中止也不必排在它们后面（IS-38）。
  - “暂停”＝写下停止请求，轮次保留为“进行中”（可续跑）。
  - “中止（放弃）”＝先把轮次标记为“已放弃”，再停止进程（数据保留、不再续跑、下次开新轮）。
  - 两者都先请求协作停止：采集进程在检查点上自己收尾（关浏览器、清身份行、释放采集锁）。
    窗口内没停下（8 秒；收到回执后 10 秒）才强制结束并连带收尾本任务启动的浏览器
    （ADR-0009；归属判定见 IS-43）。
  - 依赖：本机已登录 Edge + Playwright + pywebview 环境；打包的 exe 只是启动壳，
    界面与采集都来自源码目录（见 ADR-0007），所以改本文件不需要重新打包。
"""
from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview

from bestseller_monitor.config import Config, load_shops
from bestseller_monitor.db import Database, connect, cst_date, utcnow
from bestseller_monitor.rounds import (
    RoundRequest,
    ScopeMismatch,
    ShopScope,
    TerminalReason,
)
from bestseller_monitor import rounds
from bestseller_monitor import browser_proc
from bestseller_monitor import single_instance
from bestseller_monitor import stop_request
from bestseller_monitor import views


PROJECT_ROOT = Path(__file__).resolve().parent


def _crawler_python(exe: str | None = None) -> str:
    """采集子进程用的解释器。

    界面可能由 `pythonw` 拉起（壳优先选它，免得弹控制台窗口），采集子进程仍用带控制台的
    `python.exe`：采集本来就靠 CREATE_NO_WINDOW 静默，不该顺手再换一种解释器。
    """
    exe = exe or sys.executable
    path = Path(exe)
    if path.stem.lower() == "pythonw":
        console = path.with_name("python.exe")
        if console.is_file():
            return str(console)
    return exe


WINDOW_TITLE = "1688 畅销品监控 · 每日库存抓取"

log = logging.getLogger(__name__)

# 暂停/中止后收尾浏览器：抓取进程被强杀时端口可能还没监听，短暂重试几次（IS-43）。
_BROWSER_CLOSE_RETRY_SEC = 3.0
_BROWSER_CLOSE_RETRY_INTERVAL = 0.7

# 协作停止的窗口（ADR-0009）：请求发出后 8 秒还没停下就强制结束；采集进程回执之后
# 再给 10 秒，让它把浏览器会话关干净。这一跳由轮询驱动，关掉界面就没有兜底了。
_STOP_GRACE_SEC = 8.0
_STOP_ACK_GRACE_SEC = 10.0

# 停止的种类。「暂停」写进 stop_requests 的 kind；「中止」不用请求行——轮次终态
# 本身就是停止信号，这里只是一个内存里的标记（ADR-0009）。
_STOP_PAUSE = stop_request.PAUSE
_STOP_ABORT = "abort"

# 一句提示的 MessageBox 旗标：信息图标 + 抢到前台 + 置顶。
_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000
_SW_RESTORE = 9

# 「已经有采集在跑」的拒绝文案：三个入口共用一句，免得改了半处。
_BUSY_ERROR = "已有抓取任务在运行，请先暂停或中止。"


def _configure_gui_logging(cfg=None) -> logging.Handler:
    """GUI 自己的动作也留日志——暂停/中止后的浏览器收尾否则事后无据可查（IS-43）。

    不传 cfg 时写项目默认的 `logs/`：第二个实例在构造 Api 之前就被劝退了，
    但「这次启动为什么没开窗口」同样要留底。
    """
    logs_dir = Path(getattr(cfg, "logs_dir", PROJECT_ROOT / "logs"))
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    handler = logging.handlers.RotatingFileHandler(
        logs_dir / "gui.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return handler


class Api:
    """暴露给 pywebview 前端的方法。返回 JSON 可序列化的基本类型。

    三个页面的取数不在这里：那部分见 `bestseller_monitor.views`。本类持有界面会话事实
    （`_ui_state()` 折出来的 `views.UiState`）与连接生命周期，取数与渲染文案交给那个 module。
    """

    def __init__(self, cfg=None, *, now=utcnow, open_conn=None, shops=None):
        """构造 interface：配置、时刻、连接与店铺表都可以注入。

        `main()` 仍写 `Api()`——那时配置按项目默认位置读，「现在」取本机时间，
        连接走数据层入口（`connect()` 会建目录、建表并执行迁移，见 IS-37）。
        测试与基准注入固定时刻与自己的库，于是「今天」可判定、也不用手工塞私有属性。
        """
        self.cfg = cfg if cfg is not None else Config.from_file(
            PROJECT_ROOT / "config" / "config.toml", root=PROJECT_ROOT)
        self._now = now
        self._open_conn = open_conn if open_conn is not None else (
            lambda: connect(self.cfg.db_file))
        self.shops = (list(shops) if shops is not None else
                      [s for s in load_shops(self.cfg.shop_csv) if s.active])
        self._lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.round_id: int | None = None
        self.start_ts: float | None = None
        self.user_paused = False
        self._elapsed_base = 0.0
        self._run_start_ts: float | None = None
        # 正在进行的停止：{"kind", "target", "deadline", "acked", "round_id"}（ADR-0009）
        self._stop: dict | None = None

    # ---------- 基础 ----------
    def _today(self) -> str:
        """今天的北京日期；「现在」来自构造时注入的时刻，不读挂钟。"""
        return cst_date(self._now())

    def _current_elapsed(self) -> float:
        """当前已抓时长：运行中 = 累计段 + 当前段；暂停 = 仅累计段（冻结）。"""
        if self._run_start_ts is not None:
            return self._elapsed_base + (time.time() - self._run_start_ts)
        return self._elapsed_base

    def _refused_start_error(self) -> str | None:
        """本界面拉起的采集子进程被「已有采集在跑」拒绝了吗（专用退出码）。"""
        if (self.proc is not None
                and self.proc.poll() == single_instance.CRAWLER_BUSY_EXIT_CODE):
            return "已有采集进程在运行：本次启动被拒绝了，等它跑完再试。"
        return None

    def _ui_state(self) -> views.UiState:
        """界面会话事实：取数要读、又不属于数据层的那几件。"""
        return views.UiState(
            round_id=self.round_id,
            crawler_running=self._own_crawler_alive(),
            manually_paused=self.user_paused,
            stopping=self._stop_state(),
            stop_grace_sec=_STOP_GRACE_SEC,
            elapsed_sec=self._current_elapsed(),
            start_error=self._refused_start_error(),
        )

    # ---------- 开始页 ----------
    def get_start(self) -> dict:
        with self._lock:
            conn = self._open_conn()
            try:
                self._enforce_stop_deadline(conn)
                return views.start_view(
                    conn, cfg=self.cfg, shops=self.shops, state=self._ui_state(),
                    crawler=self.crawler_identity(conn), now=self._now())
            finally:
                conn.close()

    # ---------- 过程页 ----------
    def get_run(self) -> dict:
        with self._lock:
            if self._refused_start_error() is not None:
                # 子进程因「已有采集在跑」被拒：不碰数据库，直接说清原因。
                return views.run_view(None, state=self._ui_state(), now=self._now())
            conn = self._open_conn()
            try:
                self._enforce_stop_deadline(conn)
                view = views.run_view(conn, state=self._ui_state(), now=self._now())
                if view.get("has_round"):
                    # 轮次由采集子进程创建，界面在这里随轮询认领它。
                    self.round_id = view["round_id"]
                return view
            finally:
                conn.close()

    # ---------- 控制 ----------
    def _own_crawler_alive(self) -> bool:
        """本界面拉起的采集子进程还活着吗。"""
        return self.proc is not None and self.proc.poll() is None

    def any_crawler_running(self) -> bool:
        """有采集进程在跑吗：会话锁是权威判据，自己的子进程兜住刚拉起那一小段窗口。

        与「轮次是否进行中」是两件事：暂停后的轮次仍在进行中，但已经没有采集进程。
        """
        if self._own_crawler_alive():
            return True
        return single_instance.is_held(single_instance.CRAWLER_LOCK)

    def crawler_identity(self, conn) -> dict | None:
        """正在跑的采集进程身份；没有就返回 None。

        锁不在而身份行还在，就是被强杀留下的残留——顺手清掉，免得启动页报一个
        早就不存在的进程。
        """
        db = Database(conn)
        row = db.crawler_process()
        if not self.any_crawler_running():
            if row is not None:
                db.clear_crawler_process()
            return None
        if row is not None:
            return dict(row)
        return {
            "pid": self.proc.pid if self._own_crawler_alive() else None,
            "round_id": self.round_id,
            "started_at": None,
            "note": None,
        }

    def _spawn_crawler(self, keys: list[str] | None = None):
        """拉起采集子进程；keys 为空表示「开始或续跑」，范围由子进程按轮次决定。"""
        cmd = [_crawler_python(), str(PROJECT_ROOT / "run.py")]
        limit = ",".join(k for k in (keys or []) if k)
        if limit:
            cmd += ["--limit-shops", limit]
        # 子进程静默运行：不弹控制台窗口，stdout/stderr 丢弃（详细日志仍写入 run.log）
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def start_run(self, keys: list[str]) -> dict:
        with self._lock:
            by_key = {shop.key: shop for shop in self.shops}
            missing = [key for key in keys if key not in by_key]
            if missing:
                return {"ok": False, "error": "勾选的店铺已不在配置中：" + "、".join(missing)}
            request = RoundRequest(
                run_date=self._today(),
                shops=tuple(
                    ShopScope(by_key[key].key, by_key[key].url, by_key[key].name)
                    for key in keys
                ),
            )
            conn = self._open_conn()
            try:
                # 有采集进程在跑就不许再起一个：界面与命令行共用同一把会话锁，
                # 判据是环境事实，不是「本界面记不记得自己拉过子进程」。
                if self.crawler_identity(conn) is not None:
                    return {"ok": False, "error": _BUSY_ERROR}
                # 只读地问一句会不会被拒：今天已有轮次但范围不同就给出可读理由。
                # 轮次本身由采集子进程创建，启动失败不会留下空的「进行中」轮次。
                rounds.check_scope(Database(conn), request)
            except ScopeMismatch as exc:
                return {"ok": False, "error": str(exc)}
            finally:
                conn.close()
            self.round_id = None
            self.start_ts = time.time()
            self.user_paused = False
            self._elapsed_base = 0.0
            self._run_start_ts = time.time()
            try:
                self._spawn_crawler(keys)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"启动抓取失败：{exc}"}
            return {"ok": True}

    def resume_run(self) -> dict:
        """在“过程”页暂停后点击“继续”：重新拉起抓取，续跑本轮未完成店铺，并停留在过程页。"""
        with self._lock:
            if self.any_crawler_running():
                return {"ok": False, "error": _BUSY_ERROR}
            conn = self._open_conn()
            try:
                db = Database(conn)
                current = rounds.active_round(db, self._today())
                if current is None:
                    stale = rounds.active_round(db)
                    if stale is not None:
                        return {"ok": False, "error": (
                            f"轮次 #{stale.id}（{stale.run_date}）已经跨天，不能再续跑；"
                            "请点「开始抓取」新建一轮。"
                        )}
                    return {"ok": False, "error": "没有进行中的轮次可继续。"}
                if not current.resumable_on(utcnow()):
                    return {"ok": False, "error": (
                        f"轮次 #{current.id}（{current.run_date}）现在不可续跑；"
                        "请点「开始抓取」新建一轮。"
                    )}
                rid = current.id
            finally:
                conn.close()
            self.round_id = rid
            self.start_ts = time.time()
            self.user_paused = False
            self._run_start_ts = time.time()
            try:
                # 不带范围：子进程按「开始或续跑」处理，范围以轮次自身为准，
                # 配置里删掉过的店铺也能继续跑完。
                self._spawn_crawler()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"继续抓取失败：{exc}"}
            return {"ok": True, "round_id": rid}

    def pause_run(self) -> dict:
        """暂停：写下停止请求，采集进程在下一个检查点自己停下（ADR-0009）。

        这一跳不阻塞、也不杀进程：请求写完就返回，倒计时与超时兜底交给轮询。
        """
        with self._lock:
            self.user_paused = True
            if self._run_start_ts is not None:
                self._elapsed_base += time.time() - self._run_start_ts
                self._run_start_ts = None
            conn = self._open_conn()
            try:
                db = Database(conn)
                if not self._own_crawler_alive():
                    return {"ok": True}   # 本界面没有在跑的采集进程，没什么可暂停的
                target = self._stop_target(conn)
                if target is None:
                    # 身份行还没登记（子进程刚拉起的那一小段）：退回强制结束，
                    # 此时它连浏览器都还没起，不会留下孤儿。
                    log.info("暂停：拿不到采集进程身份，直接结束子进程。")
                    self._kill_proc()
                    self._kill_browser()
                    return {"ok": True}
                db.request_stop(round_id=self.round_id, kind=_STOP_PAUSE,
                                target_pid=target["pid"],
                                target_started_at=target["started_at"])
                self._begin_stop(_STOP_PAUSE, target, self.round_id)
                log.info("暂停：已写下停止请求（目标 PID %s），等它自己停下。", target["pid"])
                return {"ok": True, "stopping": self._stop_state()}
            finally:
                conn.close()

    def abort_run(self) -> dict:
        """中止：先把轮次收尾为「人工放弃」，再等采集进程自己停下（ADR-0009）。

        顺序是「先写终态、再停止进程」：终态本身就是停止信号，采集进程在下一个
        检查点自己收尾（关浏览器、清身份行、释放采集锁）；先杀进程反而让这条通道
        永远走不到，事后也没人知道它停在哪一步。窗口内没停下才强杀。
        停的可能是本界面拉起的子进程，也可能是别处起的（命令行、上一次界面留下的）。
        """
        with self._lock:
            self.user_paused = False
            conn = self._open_conn()
            try:
                db = Database(conn)
                # 这一处清理不是卫生，是语义：中止压过暂停——留下的暂停请求会让
                # 采集进程把这次停止认领成「暂停」，日志与店铺备注就写错了原因。
                db.clear_stop_request()
                identity = self.crawler_identity(conn)
                if self.round_id is None and identity is None:
                    self._stop = None
                    return {"ok": True}  # 没起过任务、也没有采集在跑：不必连库
                rid = self._round_to_abandon(db, identity)
                if rid is not None:
                    rounds.finish_if_open(db, rounds.load(db, rid), TerminalReason.ABANDONED,
                                          note="GUI 人工中止（放弃）")
                if identity is None:
                    self._stop = None
                    return {"ok": True, "round_id": rid}   # 采集进程已经不在了
                self._begin_stop(_STOP_ABORT, self._stop_target(conn), rid)
                log.info("中止：轮次 #%s 已收尾为人工放弃，等采集进程自己停下。", rid)
                return {"ok": True, "round_id": rid, "stopping": self._stop_state()}
            finally:
                conn.close()

    def _round_to_abandon(self, db, identity: dict | None) -> int | None:
        """该收尾哪一轮：身份行里正在跑的 > 本界面记着的 > 今天进行中的。

        身份行优先，是因为界面记着的编号不会随轮次结束清零：那条路跑完一轮之后，
        别处又起了一轮的话，按旧编号收尾就会「杀了新进程、却把终态写给旧轮次」，
        真正在跑的那一轮于是永远留在「进行中」。
        """
        if identity is not None and identity.get("round_id") is not None:
            return identity["round_id"]
        if self.round_id is not None:
            return self.round_id
        current = rounds.active_round(db, self._today())
        return current.id if current is not None else None

    @staticmethod
    def _stop_crawler_process(identity: dict | None) -> int | None:
        """结束身份行里那个采集进程；拿不到可用 PID 就返回 None。

        PID 会被系统回收，所以先认镜像名：不是 python 就不动它。拿不到时调用方仍旧
        只写终态——采集进程会在下一个检查点自己停下（轮次已是终态，它也干不下去了）。
        """
        pid = (identity or {}).get("pid")
        if not pid:
            return None
        image = browser_proc.process_image_name(int(pid))
        if not image:
            log.info("身份行里的采集进程 PID %s 已经不在了。", pid)
            return None
        if not image.startswith("python"):
            log.warning("身份行里的 PID %s 现在是 %s，不是采集进程，不动它。", pid, image)
            return None
        if browser_proc.terminate_process_tree(int(pid)):
            log.info("已结束采集进程 PID %s。", pid)
            return int(pid)
        return None

    def _kill_proc(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def _kill_browser(self):
        """收尾本任务启动的浏览器：抓取进程被强杀时不会执行它的 finally（IS-43）。"""
        if not getattr(self.cfg, "start_browser", True):
            return  # start_browser=false：接管用户自己的浏览器，不动它
        deadline = time.time() + _BROWSER_CLOSE_RETRY_SEC
        while True:
            pid = browser_proc.close_browser(self.cfg.attach_port, launched_by_us=True)
            if pid is not None or time.time() >= deadline:
                return pid
            time.sleep(_BROWSER_CLOSE_RETRY_INTERVAL)

    # ---------- 停止：先请求，超时才强杀（ADR-0009） ----------
    @staticmethod
    def _stop_target(conn) -> dict | None:
        """这次停止针对哪个进程：库里那条身份行（谁在跑由它说话）。

        目标身份是 (PID, 启动时刻)，采集进程只认领对得上自己的请求——拿不到它
        就退化成没有回执的窗口，到点强杀。
        """
        row = conn.execute("SELECT pid, started_at FROM crawler_process WHERE id=1").fetchone()
        if row is None or not row["pid"] or not row["started_at"]:
            return None
        return {"pid": int(row["pid"]), "started_at": row["started_at"]}

    def _begin_stop(self, kind: str, target: dict | None, round_id: int | None) -> None:
        """记下「正在停止」，到点由轮询兜底（不新增后台线程，也不挡住取数链）。"""
        self._stop = {
            "kind": kind,
            "target": target,
            "round_id": round_id,
            "deadline": time.time() + _STOP_GRACE_SEC,
            "acked": False,
        }

    def _stop_state(self) -> str | None:
        """给界面看的停止阶段：没有停止在进行时是 None。"""
        if self._stop is None:
            return None
        return "closing" if self._stop["acked"] else "stopping"

    def _enforce_stop_deadline(self, conn) -> None:
        """停止窗口到点还没停下就强制结束；已经停下就把状态收干净。

        由三个取数入口的轮询驱动（IS-38 同一条链），所以关掉界面之后就没有这一半，
        只剩采集进程自己读停止请求那一半——见 ADR-0009 的代价一节。
        """
        stop = self._stop
        if stop is None:
            return
        db = Database(conn)
        self._note_stop_ack(db, stop)
        if not self.any_crawler_running():
            log.info("采集进程已停下（%s）。", stop["kind"])
            db.clear_stop_request()
            self._stop = None
            return
        if time.time() < stop["deadline"]:
            return
        self._force_stop(conn, db, stop)

    @staticmethod
    def _note_stop_ack(db, stop: dict) -> None:
        """采集进程回执了就放宽窗口：它在收尾，而不是没响应。"""
        if stop["acked"] or stop["target"] is None:
            return
        request = db.stop_request()
        if request is None:
            return
        if (int(request["target_pid"]) != stop["target"]["pid"]
                or request["target_started_at"] != stop["target"]["started_at"]):
            return
        if request["ack_at"]:
            stop["acked"] = True
            stop["deadline"] = time.time() + _STOP_ACK_GRACE_SEC
            log.info("采集进程已回执停止请求，再等 %.0f 秒收尾。", _STOP_ACK_GRACE_SEC)

    def _force_stop(self, conn, db, stop: dict) -> None:
        """强制停止：强杀进程 → 按归属收尾浏览器 → 清停止请求 → 收尾状态。"""
        log.warning("停止窗口内没停下（%s），强制结束采集进程。", stop["kind"])
        self._kill_proc()
        identity = self.crawler_identity(conn)
        if stop["kind"] == _STOP_ABORT and identity is not None:
            # 中止要停的可能是别处起的采集：按身份行里的 PID 停，镜像名先核过。
            self._stop_crawler_process(identity)
        self._kill_browser()
        if self.any_crawler_running():
            # 强杀没落到实处：请求留着，采集进程在下一个检查点仍会自己停下。
            log.warning("强制结束之后采集进程仍在跑，停止请求留在库里等它自己认领。")
        else:
            db.clear_crawler_process()
            db.clear_stop_request()
        self._stop = None

    # ---------- 结果页 ----------
    def get_result(self) -> dict:
        with self._lock:
            conn = self._open_conn()
            try:
                self._enforce_stop_deadline(conn)
                return views.result_view(conn, state=self._ui_state(), now=self._now())
            finally:
                conn.close()


# 默认窗口高度：设计基准 1128 先 +20% 再 -20% ≈ 1083；屏幕放不下时收缩到可用区域内。
_PREFERRED_HEIGHT = 1083
_MIN_HEIGHT = 720
_SCREEN_MARGIN = 60
_SPI_GETWORKAREA = 0x0030


def _screen_work_height() -> int | None:
    """当前屏幕可用高度（Windows 下排除任务栏）；查询不到返回 None。"""
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return int(rect.bottom - rect.top)
    except Exception:  # noqa: BLE001
        pass
    return None


def _default_window_height() -> int:
    """默认高度按屏幕自适应：够高用 1354，不够则收缩到可用高度内（不低于 720）。"""
    avail = _screen_work_height()
    if avail is None:
        return _PREFERRED_HEIGHT
    return max(_MIN_HEIGHT, min(_PREFERRED_HEIGHT, avail - _SCREEN_MARGIN))


def _notify(text: str, title: str = "1688 畅销品监控") -> None:
    """一句给人看的提示；弹不出来也只落日志，不算错。

    与 gui_launcher 里那份刻意分开写：启动壳不能 import 项目代码（它要能独立打包）。
    `BESTSELLER_NO_DIALOG=1` 只落日志不弹窗，与启动壳同一约定，供自动化验证用。
    """
    log.info(text)
    if os.name != "nt" or (os.environ.get("BESTSELLER_NO_DIALOG") or "").strip() == "1":
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            None, text, title, _MB_ICONINFORMATION | _MB_SETFOREGROUND | _MB_TOPMOST)
    except OSError as exc:  # noqa: BLE001
        log.warning("弹窗失败（%s）：%s", exc, text)


def _focus_existing_window(title: str) -> bool:
    """按标题把已有窗口叫到前面；做不到就返回 False（调用方只提示，不报错）。"""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # 句柄是 64 位指针：不声明类型的话默认 c_int，会把 HWND 截断成错的窗口。
        user32.FindWindowW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        handle = user32.FindWindowW(None, title)
        if not handle:
            return False
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, _SW_RESTORE)
        return bool(user32.SetForegroundWindow(handle))
    except OSError as exc:  # noqa: BLE001
        log.debug("前置已有窗口失败：%s", exc)
        return False


def _announce_already_open() -> None:
    """第二个实例不建窗口：告诉用户界面已经开着，并尽量把那个窗口叫到前面。"""
    log.info("界面已经打开，本次启动不建立第二个窗口。")
    if not _focus_existing_window(WINDOW_TITLE):
        log.info("没能把已有窗口前置，只做提示。")
    _notify("界面已经打开，请看已打开的那个窗口。")


def _warn_crawler_keeps_running(api) -> bool:
    """关窗时如果采集还在跑，如实告知；返回 True 表示照常关闭。"""
    if not api.any_crawler_running():
        log.info("关闭界面：没有采集在跑。")
        return True
    log.info("关闭界面：采集仍在后台继续，重新打开界面可以看到进度并中止它。")
    _notify("采集仍在后台继续。\n\n重新打开界面可以看到进度并中止它。",
            title="1688 畅销品监控 · 采集继续运行")
    return True


def main() -> int:
    """界面入口：同一时刻只有一个界面窗口。

    抢不到界面锁就提示已有窗口并**退出 0**——退出码 0 让 gui_launcher 保持安静，
    因此这一条不需要重新打包 exe（ADR-0007 的「改界面不用重打包」得以保留）。
    """
    lock = single_instance.acquire(single_instance.GUI_LOCK)
    if lock is None:
        _configure_gui_logging()
        _announce_already_open()
        return 0
    try:
        api = Api()
        _configure_gui_logging(api.cfg)
        html = (PROJECT_ROOT / "docs" / "ui_live.html").read_text(encoding="utf-8")
        window = webview.create_window(
            WINDOW_TITLE,
            html=html,
            js_api=api,
            width=1120,
            height=_default_window_height(),
            min_size=(960, 720),
        )
        window.events.closing += lambda: _warn_crawler_keeps_running(api)
        webview.start(debug=False)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
