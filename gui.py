"""1688 畅销品监控 · GUI 启动器/进度/结果（pywebview）。

行为约定：
  - GUI 只做启动器/监控，抓取仍由本机 `python run.py --limit-shops=...` 子进程执行。
  - 界面打开时在后台走一次「开轮前的一次准备」（spec §6，`plan_step.prepare_week`）：
    拉计划库 → 同步清单 → 确认/生成/发布本周计划 → 落库。开始页的默认勾选与页数
    都读落库计划（与命令行同一份）；准备没通过则默认拒绝开轮——拉不到计划库、本地也
    没有时点「开始」会先给确认（逃生口，按「自由采集 + 记账为计划外」跑）；用本地
    那份降级开轮时页面标注「未能确认最新」。
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
import datetime as dt
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
from bestseller_monitor.db import CST, Database, connect, cst_date, utcnow
from bestseller_monitor.rounds import (
    RoundRequest,
    ScopeMismatch,
    ShopScope,
    TerminalReason,
)
from bestseller_monitor import plan_step, rounds, weekly_plan
from bestseller_monitor import crawler_identity
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

    def __init__(self, cfg=None, *, now=utcnow, open_conn=None, shops=None,
                 stop_clock=None):
        """构造 interface：配置、时刻、连接与店铺表都可以注入。

        `main()` 仍写 `Api()`——那时配置按项目默认位置读，「现在」取本机时间，
        连接走数据层入口（`connect()` 会建目录、建表并执行迁移，见 IS-37）。
        测试与基准注入固定时刻与自己的库，于是「今天」可判定、也不用手工塞私有属性。
        `stop_clock` 是停止窗口用的时钟（默认挂钟）：用例给它一个能推进的时钟，
        就能把 8 秒 / 10 秒那两段窗口走到点，而不必去改私有状态。
        """
        self.cfg = cfg if cfg is not None else Config.from_file(
            PROJECT_ROOT / "config" / "config.toml", root=PROJECT_ROOT)
        self._now = now
        self._open_conn = open_conn if open_conn is not None else (
            lambda: connect(self.cfg.db_file))
        self._shops_injected = shops is not None
        self.shops = (list(shops) if shops is not None else
                      [s for s in load_shops(self.cfg.shop_csv) if s.active])
        # 开轮前准备（spec §6）的结局：`prepare()` 落在这里，`start_run` 拿它做
        # 「默认拒绝开轮」的判定；没跑过就是 None（子进程开轮前还会再准备一次）。
        self._prep: plan_step.PrepResult | None = None
        self._lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.round_id: int | None = None
        self.start_ts: float | None = None
        self.user_paused = False
        self._elapsed_base = 0.0
        self._run_start_ts: float | None = None
        # 停止编排（窗口、回执、超时强杀）在 stop_request 里；**冻结目标之后的处置也在那里**，
        # 界面只交一件事实：本界面拉起的那个子进程。晚绑定 lambda 让 begin 那一刻读一次
        # `self.proc` 并冻进绑定，此后窗口不再读这个可变字段（ADR-0024）。
        # 窗口：请求发出后 8 秒、采集进程回执之后再 10 秒（ADR-0009，数值在 StopWatch 里）。
        runtime = stop_request.ProcessStopRuntime(own_process=lambda: self.proc)
        self._stop_watch = stop_request.StopWatch(
            runtime, now=stop_clock or time.time,
        )

    # ---------- 基础 ----------
    def _today(self) -> str:
        """今天的北京日期；「现在」来自构造时注入的时刻，不读挂钟。"""
        return cst_date(self._now())

    def _current_elapsed(self) -> float:
        """当前已抓时长：运行中 = 累计段 + 当前段；暂停 = 仅累计段（冻结）。"""
        if self._run_start_ts is not None:
            return self._elapsed_base + (time.time() - self._run_start_ts)
        return self._elapsed_base

    def _refused_start_message(self) -> str | None:
        """本界面拉起的采集子进程被拒时，要显示的那句话（按子进程的退出码分流）。"""
        if self.proc is None:
            return None
        code = self.proc.poll()
        if code == single_instance.CRAWLER_BUSY_EXIT_CODE:
            return "已有采集进程在运行：本次启动被拒绝了，等它跑完再试。"
        if code == plan_step.PLAN_REFUSED_EXIT_CODE:
            return ("开轮前准备没通过：本次启动被拒绝了——本机没有可用的本周计划"
                    "（拉不到计划库、本地也没落库）。")
        return None

    def _ui_state(self) -> views.UiState:
        """界面会话事实：取数要读、又不属于数据层的那几件。"""
        return views.UiState(
            round_id=self.round_id,
            crawler_running=self._own_crawler_alive(),
            manually_paused=self.user_paused,
            stopping=self._stop_watch.state,
            stop_grace_sec=self._stop_watch.grace_sec,
            elapsed_sec=self._current_elapsed(),
            start_error=self._refused_start_message(),
            plan_stale=self._prep_is_stale(),
        )

    # ---------- 开始页 ----------
    def _current_week(self) -> str:
        """今天（北京时间）所在的周编号；计划表与准备步骤都按它对齐。"""
        return weekly_plan.week_label(self._today())

    def prepare(self) -> plan_step.PrepResult | None:
        """界面打开时的一次（开轮前）准备（spec §6）：与命令行开跑前同一步。

        `main()` 在后台线程里跑它——拉计划库与确认计划通常要几秒，不该挡着窗口打开；
        落库后开始页的下一次轮询就按本周计划给默认勾选与页数。失败只记日志：界面
        照常可用，「默认拒绝开轮」的判定在 `_plan_block_reason`，逃生口入口见票据
        07。返回结局供测试与日志用；意外异常返回 None。
        """
        try:
            conn = self._open_conn()
            try:
                # prepare_week 按北京日期算周：注入的时刻先折算到 CST，免得跨夜差一周
                moment = dt.datetime.fromisoformat(self._now()).astimezone(CST)
                result = plan_step.prepare_week(self.cfg, Database(conn), now=moment)
            finally:
                conn.close()
        except Exception as exc:                       # noqa: BLE001
            log.warning("开轮前准备没做成（界面照常可用，子进程开轮前还会再准备一次）：%s", exc)
            return None
        with self._lock:                # 只锁这一次赋值：准备本身（含 git）不占锁
            self._prep = result
        log.info("开轮前准备：%s（本周 %s，本机 %s 家店）",
                 result.status.value, result.week, len(result.my_shops))
        for warning in result.warnings:
            log.warning("开轮前准备：%s", warning)
        return result

    def _plan_block_reason(self, db: Database | None = None) -> str | None:
        """准备没通过就默认拒绝开轮（spec §6 降级表；逃生口入口见票据 07）。

        只认本界面刚做过、且是本周的那次准备：没跑过或跨了周就不拦——子进程 run.py
        开跑前自己会做准备，那里有同样的判定与同一条退出码（它的拒绝在这里也有文案，
        见 `_refused_start_message`）。今天已有进行中的轮次同样不拦：那是续跑，范围
        以轮次自身为准（与 run.py 同一条规则，逃生口开出来的那一轮也才续得下去）。
        """
        prep = self._prep_this_week()
        if prep is None or prep.can_start:
            return None
        if prep.status is plan_step.PrepStatus.SKIPPED_MERGE_ONLY:
            return "本机是纯汇总机（machine.role = merge_only）：不做采集。"
        if db is not None and rounds.active_round(db, self._today()) is not None:
            return None
        return "开轮前准备没通过，本次不开轮：\n" + (prep.reason or "拉不到计划库，且本地没有本周计划。")

    def _prep_this_week(self) -> plan_step.PrepResult | None:
        """本界面刚做过、且属于本周的那次准备；没跑过或跨周就是 None。"""
        prep = self._prep
        return prep if prep is not None and prep.week == self._current_week() else None

    def _escape_hatch_available(self) -> bool:
        """这次拒绝有没有逃生口：只有「拉不到计划库、本地也没有」那一行有（纯汇总机没有）。"""
        prep = self._prep_this_week()
        return prep is not None and prep.escape_hatch_available

    def _prep_is_stale(self) -> bool:
        """这次准备是不是降级来的（用本地那份、未能确认最新）：页面要标注（票据 07）。"""
        prep = self._prep_this_week()
        return prep is not None and prep.stale

    def _refresh_shops(self, conn) -> None:
        """店铺全量的最新读法：本机启用的店 + 本周计划说到的店。

        计划落库后，开始页的清单会跟着变（停用但仍在计划里的店要能勾上）；
        测试与基准注入了 shops= 就完全按注入的那份来。
        """
        if self._shops_injected or self.cfg.shop_csv is None:
            return
        plan = plan_step.stored_plan(Database(conn), self._current_week())
        self.shops = plan_step.visible_shops(self.cfg, plan)

    def get_start(self) -> dict:
        with self._lock:
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
                self._refresh_shops(conn)
                return views.start_view(
                    conn, cfg=self.cfg, shops=self.shops, state=self._ui_state(),
                    crawler=self.current_crawler(conn), now=self._now())
            finally:
                conn.close()

    # ---------- 过程页 ----------
    def get_run(self) -> dict:
        with self._lock:
            state = self._ui_state()
            if state.start_error is not None:
                # 子进程因「已有采集在跑」被拒：不碰数据库，直接说清原因。
                return views.run_view(None, state=state, now=self._now())
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
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
        """有采集进程在跑吗（判据在 crawler_identity，这里只接线）。"""
        return crawler_identity.is_running(own_alive=self._own_crawler_alive())

    def current_crawler(self, conn):
        """正在跑的采集进程身份；没有就返回 None（判据 + 顺手清残留在 crawler_identity）。"""
        return crawler_identity.current(
            conn,
            own_alive=self._own_crawler_alive(),
            own_pid=self.proc.pid if self.proc is not None else None,
            own_round_id=self.round_id,
        )

    def _spawn_crawler(self, keys: list[str] | None = None, *,
                       ignore_plan: bool = False):
        """拉起采集子进程；keys 为空表示「开始或续跑」，范围由子进程按轮次决定。

        `ignore_plan=True` 是逃生口（界面确认后）：子进程在「拉不到计划库、本地也没有」
        时照开轮，按「自由采集 + 记账为计划外」运行（run.py 的 `--ignore-plan`）。
        """
        cmd = [_crawler_python(), str(PROJECT_ROOT / "run.py")]
        limit = ",".join(k for k in (keys or []) if k)
        if limit:
            cmd += ["--limit-shops", limit]
        if ignore_plan:
            cmd += ["--ignore-plan"]
        # 子进程静默运行：不弹控制台窗口，stdout/stderr 丢弃（详细日志仍写入 run.log）
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def start_run(self, keys: list[str], ignore_plan: bool = False) -> dict:
        """开始一轮；`ignore_plan=True` 只有界面确认过逃生口才会传（否则默认拒绝开轮）。"""
        with self._lock:
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
                db = Database(conn)
                reason = self._plan_block_reason(db)
                if reason is not None:
                    escape = self._escape_hatch_available()
                    if not (ignore_plan and escape):
                        return {"ok": False, "error": reason, "escape_hatch": escape}
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
                if self._stop_watch.status.phase is not stop_request.StopPhase.IDLE:
                    return {"ok": False, "error": "上一次停止仍在核验或收尾，请稍后重试。",
                            "retryable": True}
                # 有采集进程在跑就不许再起一个：界面与命令行共用同一把会话锁，
                # 判据是环境事实，不是「本界面记不记得自己拉过子进程」。
                if self.current_crawler(conn) is not None:
                    return {"ok": False, "error": _BUSY_ERROR}
                # 只读地问一句会不会被拒：今天已有轮次但范围不同就给出可读理由。
                # 轮次本身由采集子进程创建，启动失败不会留下空的「进行中」轮次。
                rounds.check_scope(db, request)
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
                self._spawn_crawler(keys, ignore_plan=ignore_plan)
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
                self._stop_watch.tick(conn)
                if self._stop_watch.status.phase is not stop_request.StopPhase.IDLE:
                    return {"ok": False, "error": "上一次停止仍在核验或收尾，请稍后重试。",
                            "retryable": True}
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
                if not current.resumable_on(self._now()):
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
                # Reconcile any older target before selecting the current crawler.
                # A replacement must settle the old watch without touching the new one.
                self._stop_watch.tick(conn)
                db = Database(conn)
                if not self._own_crawler_alive():
                    return {"ok": True}   # 本界面没有在跑的采集进程，没什么可暂停的
                target = stop_request.StopTarget.of(crawler_identity.registered(conn))
                if target is None:
                    if self._own_crawler_alive():
                        # 身份登记前没有浏览器副作用；只结束入口时冻结的 child。
                        log.info("暂停：身份尚未登记，结束入口时冻结的子进程。")
                        self._kill_proc()
                        return {"ok": True}
                    return {"ok": False, "error": "暂时无法确认停止目标，请稍后重试。",
                            "retryable": True}
                try:
                    self._stop_watch.begin(
                        conn, stop_request.StopCommand(
                            stop_request.StopKind.PAUSE, target, self.round_id))
                except RuntimeError as exc:
                    return {"ok": False, "error": "暂时无法确认停止目标，请稍后重试。",
                            "retryable": True, "code": str(exc)}
                log.info("暂停：已写下停止请求（目标 PID %s），等它自己停下。", target.pid)
                return {"ok": True, "stopping": self._stop_watch.state}
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
                self._stop_watch.tick(conn)
                db = Database(conn)
                # 这一处清理不是卫生，是语义：中止压过暂停——留下的暂停请求会让
                # 采集进程把这次停止认领成「暂停」，日志与店铺备注就写错了原因。
                identity = self.current_crawler(conn)
                if self.round_id is None and identity is None:
                    return {"ok": True}  # 没起过任务、也没有采集在跑：不必连库
                if identity is None and self.any_crawler_running():
                    return {"ok": False, "error": "暂时无法确认停止目标，请稍后重试。",
                            "retryable": True}
                rid = self._round_to_abandon(db, identity)
                if rid is not None:
                    rounds.finish_if_open(db, rounds.load(db, rid), TerminalReason.ABANDONED,
                                          note="GUI 人工中止（放弃）")
                if identity is None:
                    return {"ok": True, "round_id": rid}   # 采集进程已经不在了
                # 同一条身份行只读这一次：它是「正在跑的是谁」，也是这次停止的目标。
                target = stop_request.StopTarget.of(identity)
                if target is None:
                    return {"ok": False, "error": "暂时无法确认停止目标，请稍后重试。",
                            "retryable": True}
                try:
                    self._stop_watch.begin(
                        conn, stop_request.StopCommand(stop_request.StopKind.ABORT,
                                                       target, rid))
                except RuntimeError as exc:
                    return {"ok": False, "error": "暂时无法确认停止目标，请稍后重试。",
                            "retryable": True, "code": str(exc)}
                log.info("中止：轮次 #%s 已收尾为人工放弃，等采集进程自己停下。", rid)
                return {"ok": True, "round_id": rid, "stopping": self._stop_watch.state}
            finally:
                conn.close()

    def _round_to_abandon(self, db, identity) -> int | None:
        """该收尾哪一轮：身份行里正在跑的 > 本界面记着的 > 今天进行中的。

        身份行优先，是因为界面记着的编号不会随轮次结束清零：那条路跑完一轮之后，
        别处又起了一轮的话，按旧编号收尾就会「杀了新进程、却把终态写给旧轮次」，
        真正在跑的那一轮于是永远留在「进行中」。
        """
        if identity is not None and identity.round_id is not None:
            return identity.round_id
        if self.round_id is not None:
            return self.round_id
        current = rounds.active_round(db, self._today())
        return current.id if current is not None else None

    def _kill_proc(self):
        """结束本界面拉起的那个子进程。

        唯一调用点是「暂停」的启动竞态：子进程刚拉起、身份行还没登记，没有可认领的停止目标，
        也建不起停止窗口（ADR-0024 留的唯一窄例外）。窗口内的强制停止不走这里——那条路只对
        begin 冻结的绑定动手，处置在 `ProcessStopRuntime` 里。
        """
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    # ---------- 停止：先请求，超时才强杀（ADR-0009） ----------
    # ---------- 结果页 ----------
    def get_result(self) -> dict:
        with self._lock:
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
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
        # 界面打开时的一次（开轮前）准备（spec §6）：后台跑，落库后开始页的下一次轮询
        # 就按本周计划给默认勾选与页数；失败只记日志，开轮时子进程还会再准备一次。
        threading.Thread(target=api.prepare, name="plan-prepare", daemon=True).start()
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
