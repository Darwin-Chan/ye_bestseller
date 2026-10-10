"""1688 畅销品监控 · GUI 启动器/进度/结果（pywebview）。

行为约定：
  - GUI 只做启动器/监控，抓取仍由本机 `python run.py --limit-shops=...` 子进程执行。
  - 界面打开时先走一次「开轮前的一次准备」（spec §6，`plan_step.prepare_week`）：
    拉计划库 → 同步清单 → 确认/生成/发布本周计划 → 落库。**窗口等它落定再建**
    （上限 30 秒，ADR-0035）——开始页画出来时默认勾选与页数就已经是本机份额那份（默认
    勾选再筛掉今天已采够的，票据 17）。
    等不到就按「本地有没有本周计划」分岔：有则用本地那份、标「未能确认最新」；
    没有则停在闸门（给理由与「重试准备」，界面不开轮）。「重试准备」在闸门与
    「未能确认最新」两态都可点。
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

import contextlib
import dataclasses
import functools
import ctypes
import logging
import logging.handlers
import os
import sqlite3
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview

from bestseller_monitor.crawler_close import CloseSnapshot, install_close_guard

from bestseller_monitor.collection_context import for_collection, prepare_trial_storage
from bestseller_monitor.config import Config, Shop, is_merge_only, load_shops
from bestseller_monitor.db import Database, connect, cst_date, utcnow
from bestseller_monitor.rounds import (
    RoundRequest,
    ScopeMismatch,
    ShopScope,
    TerminalReason,
)
from bestseller_monitor import plan_step, preparation, rounds, weekly_plan
from bestseller_monitor import crawler_identity
from bestseller_monitor import single_instance
from bestseller_monitor import shop_progress
from bestseller_monitor import stop_request
from bestseller_monitor import views
from bestseller_monitor.dedicated_browser import DedicatedBrowser


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


WINDOW_TITLE = "1688 畅销品监控 · 库存数据抓取"

log = logging.getLogger(__name__)

# 一句提示的 MessageBox 旗标：信息图标 + 抢到前台 + 置顶。
_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000
_SW_RESTORE = 9

# 「已经有采集在跑」的拒绝文案：三个入口共用一句，免得改了半处。
_BUSY_ERROR = "已有抓取任务在运行，请先暂停或中止。"


def _local_shops(cfg) -> list[Shop]:
    """本机采集清单里启用的店。

    纯汇总机上没有这份清单是常态（不采 1688，上机清单也不建它）：界面照常打开、
    开始页说明白，而不是拿「找不到店铺清单」把人挡在门外。
    """
    try:
        return [shop for shop in load_shops(cfg.shop_csv, allow_empty=is_merge_only(cfg)) if shop.active]
    except FileNotFoundError:
        if not is_merge_only(cfg):
            raise
        log.info("纯汇总机：本机没有采集清单（%s），开始页照常打开。", cfg.shop_csv)
        return []


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


@contextlib.contextmanager
def _trial_gui_logging(cfg):
    """Keep trial migration/control logs out of previously installed formal handlers."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    for handler in handlers:
        root.removeHandler(handler)
    trial_handler = None
    try:
        trial_handler = _configure_gui_logging(cfg)
        yield
    finally:
        if trial_handler is not None:
            root.removeHandler(trial_handler)
            trial_handler.close()
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)


def _collection_action(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            if self.cfg.local_trial:
                with _trial_gui_logging(self.cfg):
                    return method(self, *args, **kwargs)
            return method(self, *args, **kwargs)
    return call


CLOSING_REFUSAL = "正在关闭程序，不能开始、续跑或暂停。"

# 手动入口在这些浏览器状态下不提供：启动/观察失败、占用核不实、调试端口被无关实例占着、
# 正在打开。「已打开/占用」放行——引导用户用已有窗口，不另开标签也不接管。
_BROWSER_ENTRY_BLOCKED = ("failed", "unknown", "port_busy", "opening")


class Api:
    """暴露给 pywebview 前端的方法。返回 JSON 可序列化的基本类型。

    三个页面的取数不在这里：那部分见 `bestseller_monitor.views`。本类持有界面会话事实
    （`_ui_state()` 折出来的 `views.UiState`）与连接生命周期，取数与渲染文案交给那个 module。
    """

    def __init__(self, cfg=None, *, now=utcnow, open_conn=None, shops=None,
                 stop_clock=None, config_file=None, browser_runtime=None, prep_runtime=None):
        """构造 interface：配置、时刻、连接与店铺表都可以注入。

        `main()` 仍写 `Api()`——那时配置按项目默认位置读，「现在」取本机时间，
        连接走数据层入口（`connect()` 会建目录、建表并执行迁移，见 IS-37）。
        测试与基准注入固定时刻与自己的库，于是「今天」可判定、也不用手工塞私有属性。
        `stop_clock` 是停止窗口用的时钟（默认挂钟）：用例给它一个能推进的时钟，
        就能把 8 秒 / 10 秒那两段窗口走到点，而不必去改私有状态。
        `prep_runtime` 是准备专用运行 adapter（单调读时 / 安排后台执行 / 等待通知，
        默认走生产线程与真实时钟）：用例用它控制预算推进与派发/唤醒，不去改私有字段。
        """
        source = Path(config_file) if config_file is not None else PROJECT_ROOT / "config/config.toml"
        self.cfg = cfg if cfg is not None else Config.from_file(source)
        self._formal_cfg = self.cfg
        self.config_file = (Path(config_file) if config_file is not None else PROJECT_ROOT / "config/config.toml").resolve()
        self._browser = DedicatedBrowser(self.cfg, self.config_file, runtime=browser_runtime)
        self._formal_open_conn = open_conn
        self._now = now
        self._open_conn = open_conn if open_conn is not None else (
            lambda: connect(self.cfg.db_file))
        self._shops_injected = shops is not None
        self.shops = (list(shops) if shops is not None else _local_shops(self.cfg))
        self._lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.round_id: int | None = None
        self.user_paused = False
        self._paused_round_id: int | None = None
        self._resume_intent: rounds.ResumeRoundIntent | None = None
        self._closing_requested = False
        self._abort_confirmation: tuple[str, CloseSnapshot] | None = None
        # 停止编排（窗口、回执、超时强杀）在 stop_request 里；**冻结目标之后的处置也在那里**，
        # 界面只交一件事实：本界面拉起的那个子进程。晚绑定 lambda 让 begin 那一刻读一次
        # `self.proc` 并冻进绑定，此后窗口不再读这个可变字段（ADR-0024）。
        # 窗口：请求发出后 8 秒、采集进程回执之后再 10 秒（ADR-0009，数值在 StopWatch 里）。
        runtime = stop_request.ProcessStopRuntime(own_process=lambda: self.proc)
        self._stop_watch = stop_request.StopWatch(
            runtime, now=stop_clock or time.time,
        )
        # 开轮前准备（spec §6 / ADR-0035）的接纳、预算、等待、落定与扫描编排都在
        # 持有者里（preparation.Preparation）；这里只负责选owner与接线。正式上下文
        # 一个实例；进入试采换成绑定试采配置的 bypass 实例；回到原正式配置时恢复
        # 同一个正式实例（原事实还在）。连接能力按构造时的配置冻结——持有者不回读
        # 可变的 self.cfg / self._open_conn（切换试采也换不掉发起时那次运行的连接）。
        formal_cfg = self.cfg
        formal_conn = open_conn if open_conn is not None else (
            lambda: connect(formal_cfg.db_file))
        self._prep_runtime = prep_runtime if prep_runtime is not None else (
            preparation.ProductionPrepRuntime())
        self._formal_prep_owner = preparation.Preparation(
            cfg=formal_cfg, open_conn=formal_conn, now=self._now,
            runtime=self._prep_runtime)
        self._prep_owner = self._formal_prep_owner

    def set_local_trial(self, enabled: bool) -> dict:
        """Explicit storage selection; never change machine identity or formal tools."""
        with self._lock:
            if self._closing_requested or self.prepare_running():
                return {"ok": False, "error": "正在关闭或准备，暂不能切换采集模式。"}
            if self._own_crawler_alive() or self._stop_watch.status.phase is not stop_request.StopPhase.IDLE:
                return {"ok": False, "error": "采集或停止仍在进行，不能切换模式。"}
            if bool(enabled) == self.cfg.local_trial:
                return {"ok": True}
            try:
                target = for_collection(self._formal_cfg, local_trial=bool(enabled),
                                        config_file=self.config_file)
                if self.any_crawler_running():
                    # Only attach to an already running process in the requested space.
                    # Missing/ambiguous identity cannot authorize a context change.
                    if not target.db_file.exists():
                        return {"ok": False, "error": "其他模式的采集正在运行，不能切换模式。"}
                    identity = crawler_identity.verified_in_file(
                        target.db_file, other_space=self.cfg.db_file)
                    if identity.pid is None:
                        return {"ok": False, "error": "采集目标不明或其他模式正在运行，不能切换模式。"}
                if enabled:
                    configured = load_shops(target.shop_csv, allow_empty=True)
                    prepare_trial_storage(target)
                    shops = [shop for shop in configured if shop.active]
                else:
                    shops = _local_shops(target)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                return {"ok": False, "error": f"不能进入本机试采，不会回退正式采集：{exc}"}
            self.cfg = target
            self._open_conn = (lambda: connect(self.cfg.db_file)) if enabled or self._formal_open_conn is None else self._formal_open_conn
            # 选owner：进试采换成绑定试采配置的 bypass 实例（不启动也不消费正式准备），
            # 回正式恢复**同一个**正式实例——原先的事实还在，不自动重新准备。
            self._prep_owner = (preparation.Preparation(
                cfg=target, open_conn=(lambda: connect(target.db_file)), now=self._now,
                runtime=self._prep_runtime) if enabled else self._formal_prep_owner)
            self.shops = shops
            self.proc = None
            self.round_id = None
            self.user_paused = False
            self._paused_round_id = None
            self._resume_intent = None
            return {"ok": True}

    def _collection_payload(self, payload: dict) -> dict:
        return {**payload, "local_trial": self.cfg.local_trial,
                "collection_label": "本机试采，不共享" if self.cfg.local_trial else "正式采集",
                "database": str(self.cfg.db_file),
                "mode_switch_locked": self._own_crawler_alive() or (self.cfg.local_trial and self.any_crawler_running())
                or self._stop_watch.status.phase is not stop_request.StopPhase.IDLE}

    # ---------- 基础 ----------
    def _today(self) -> str:
        """今天的北京日期；「现在」来自构造时注入的时刻，不读挂钟。"""
        return cst_date(self._now())

    def _refused_start_message(self) -> str | None:
        """本界面拉起的采集子进程被拒时，要显示的那句话（按子进程的退出码分流）。"""
        if self.proc is None:
            return None
        code = self.proc.poll()
        if code == single_instance.CRAWLER_BUSY_EXIT_CODE:
            return "已有采集进程在运行：本次启动被拒绝了，等它跑完再试。"
        if code == 3:
            return "新建或继续被拒绝：轮次预检事实或范围已变化，请检查旧轮并重新确认。"
        if self.cfg.local_trial:
            if code == 2:
                return "本机试采启动被拒绝：请检查本机清单、选店与隔离路径，查看试采 logs/run.log。"
        if code == plan_step.PLAN_REFUSED_EXIT_CODE:
            return ("开轮前准备没通过：本次启动被拒绝了——本机没有可用的本周计划"
                    "（拉不到计划库、本地也没落库）。")
        return None

    def _ui_state(self) -> views.UiState:
        """界面会话事实：取数要读、又不属于数据层的那几件。"""
        return views.UiState(
            round_id=self.round_id,
            crawler_running=self._own_crawler_alive(),
            manually_paused=(self.user_paused and self._paused_round_id == self.round_id),
            stopping=self._stop_watch.state,
            stop_grace_sec=self._stop_watch.grace_sec,
            start_error=self._refused_start_message(),
            # 这一次准备的一份不可变快照原样交给取数：闸门、「未能确认最新」与确认期
            # 锁定都从它读（跨周的那次不算本周的确认，但也正是「窗口跨周了」的判据，
            # plan_step.start_plan 自己按周折）。
            prep=self._prep_owner.snapshot(),
        )

    # ---------- 开始页 ----------
    def _current_week(self) -> str:
        """今天（北京时间）所在的周编号；计划表与准备步骤都按它对齐。"""
        return weekly_plan.week_label(self._today())

    def prepare(self) -> plan_step.PrepResult | None:
        """开轮前的一次准备（spec §6）：与命令行开跑前同一步（ADR-0035）。

        经持有者接纳，等它**连同缺口扫描**结束（保持同步 prepare 语义）：落定先发布，
        扫描是只告警的慢活、不占开窗的等待（开窗走 `prepare_for_window`）。未落定时
        重复请求被拒绝——记录理由并返回 None，不隐式等待、不重跑（一次只接纳一个）。
        试采绕过正式准备（返回 None）；失败只记日志，界面照常可用，闸门判定在
        `_plan_block_reason`。返回结局供测试与日志用；异常或派发失败返回 None。
        """
        with self._lock:
            owner = self._prep_owner
            decision = owner.request()
        if decision.outcome is preparation.RequestOutcome.IN_FLIGHT:
            log.info("重复的开轮前准备被拒绝（已有未落定的运行）：返回 None，不等待、不重跑。")
            return None
        if decision.run is None:                        # 试采绕过
            return None
        return owner.wait_for_finish(decision.run)

    def prepare_for_window(self) -> preparation.PreparationSnapshot:
        """开窗准备（`main()` 用）：请求一次准备，或**明确等待已在跑的那一次**。

        只等指定运行落定或它的剩余预算——不含缺口扫描，也不接受另给一份预算；
        返回该次运行的快照供日志与测试看事实。试采直接返回绕过快照。
        """
        with self._lock:
            owner = self._prep_owner
            decision = owner.request()
        if decision.run is None:                        # 试采绕过
            return owner.snapshot()
        return owner.wait_for_open(decision.run)

    def confirming_now(self) -> bool:
        """「正在确认」：准备在跑、且还在 30 秒预算里——页面据此锁住勾选与开始。

        预算之外不再拦着人（超时兜底：本地有那份就照常用、没有就停在闸门），但准备
        还在跑（`prepare_running()`）时页面继续轮询，落定即自动更新。判断都在持有者
        的快照里（单调截止点），这里是纯委托。
        """
        with self._lock:
            return self._prep_owner.snapshot().confirming

    def prepare_running(self) -> bool:
        """准备还在跑吗（不限预算）：页面据此决定要不要接着轮询。"""
        with self._lock:
            return self._prep_owner.snapshot().running

    def retry_prepare(self) -> dict:
        """「重试准备」入口：后台重跑一次开轮前准备（闸门与「未能确认最新」两态）。

        接纳成功立即放行——紧接着的那次取数就能从快照看到「正在确认」，页面据此
        接着轮询、落定后自动更新。未落定时重复请求返回 error；派发失败返回 error。
        """
        with self._lock:
            decision = self._prep_owner.request()
        if decision.outcome is preparation.RequestOutcome.TRIAL_BYPASS:
            return {"ok": True}
        if decision.outcome is preparation.RequestOutcome.IN_FLIGHT:
            return {"ok": False, "error": "正在确认中：等这一次跑完。"}
        if decision.outcome is preparation.RequestOutcome.DISPATCH_FAILED:
            return {"ok": False, "error": "准备任务派发失败，请稍后重试。"}
        return {"ok": True}

    def _plan_block_reason(self, db: Database) -> str | None:
        """闸门判定（ADR-0035）：本地没有本周计划就不开轮——与开始页同一处判定。

        纯汇总机先判角色。开始页只表达新建，不借数据库旧轮跳过计划门禁；
        子进程 run.py 开跑前还会准备并核验一次。普通 CLI 的续跑能力保持独立。
        """
        if self.cfg.local_trial:
            return None
        if is_merge_only(self.cfg):
            return plan_step.MERGE_ONLY_REFUSAL
        plan = plan_step.start_plan(db, self.cfg, self._current_week(),
                                    self._prep_owner.snapshot().result)
        return plan.reason if plan.blocked else None

    def _refresh_shops(self, conn) -> None:
        """店铺全量的最新读法：本机启用的店 + 本周计划说到的店。

        计划落库后，开始页的清单会跟着变（停用但仍在计划里的店要能勾上）；
        测试与基准注入了 shops= 就完全按注入的那份来。纯汇总机不采集：清单不是它的
        取数来源（没有那份文件也是正常态），构造时读到什么就留什么。
        """
        if self.cfg.local_trial:
            self.shops = [shop for shop in load_shops(self.cfg.shop_csv, allow_empty=True) if shop.active]
            return
        if self._shops_injected or self.cfg.shop_csv is None:
            return
        if is_merge_only(self.cfg):
            return
        plan = plan_step.stored_plan(Database(conn), self._current_week())
        self.shops = plan_step.visible_shops(self.cfg, plan)

    @_collection_action
    def get_start(self) -> dict:
        with self._lock:
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
                self._refresh_shops(conn)
                self._consume_resume_ack(Database(conn))
                view = self._collection_payload(views.start_view(
                    conn, cfg=self.cfg, shops=self.shops, state=self._ui_state(),
                    crawler=self.current_crawler(conn), now=self._now()))
                browser, refusal, gate = self._browser_entry(conn)
                view["browser"] = browser
                if refusal is not None:
                    # 开始页按钮禁用时给出原因（与过程页同一处 login_refusal）。
                    view["login_refusal"] = refusal
                if gate:
                    view["start_allowed"] = False
                    view["browser_error"] = gate
                    if not view.get("start_hint"):
                        view["start_hint"] = gate
                return view
            finally:
                conn.close()

    def _browser_entry(self, conn):
        """手动入口的事实、可用性与拦停原因：开始页与「明确暂停且收尾完成」的过程页同一判据。

        返回 `(浏览器事实, 入口门禁原因, 拦开轮/拦继续的原因)`。占用态入口仍可点
        （引导用已有窗口），但 `gate` 非空——采集不许借它启动，这与 `can_open` 是两件事。
        """
        refusal = self._login_refusal(conn)
        browser = self._browser.status()
        error = self._browser.configuration_error()
        browser["can_open"] = (refusal is None and error is None
                               and browser["state"] not in _BROWSER_ENTRY_BLOCKED)
        gate = error or (None if browser["can_start"]
                         else browser["message"] + browser["retry"])
        return browser, refusal, gate

    def _login_refusal(self, conn):
        """同一专用浏览器服务的入口门禁：开始页与明确暂停后的过程页共用这一条。

        停止/收尾未完成、采集或身份验证等待进行中一律拒绝。本界面明确暂停且停止
        收尾已完成（停止窗口回到 idle、没有采集进程）时，过程页入口照常放行——
        这是票 05 接入的那一格。纯汇总机正式模式拒绝，但**本机试采照常放行**
        （票 08）：试采用同一份专用 profile，登录准备与正式采集同样需要。
        前端禁用只是引导，真正的授权在这里。
        """
        if self._closing_requested:
            return CLOSING_REFUSAL
        if is_merge_only(self.cfg) and not self.cfg.local_trial:
            return "纯汇总机不允许打开采集专用 Edge。"
        if self._stop_watch.status.phase is not stop_request.StopPhase.IDLE:
            return "停止或收尾仍在进行，请稍后刷新重试。"
        if self.current_crawler(conn) is not None:
            return "采集或身份验证等待中，不能另开手动浏览器。"
        debt = crawler_identity.registered(conn)
        if debt is not None and not crawler_identity.browser_cleanup_complete(debt.browser_state):
            # 已有终态但停止/浏览器收尾未结清（含界面重开后的残留欠账）：先完成收尾核验。
            return "上一轮的停止/浏览器收尾仍未完成；请先在开始页完成收尾核验，再打开手动浏览器。"
        return None

    def open_login_browser(self):
        with self._lock:
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
                reason = self._login_refusal(conn)
                if reason:
                    return {"ok": False, "error": reason}
                # Reserve while the API gate is locked; OS creation itself must
                # not block refresh, close or a duplicate click.
                reserved = self._browser.reserve_open()
            finally:
                conn.close()
        if reserved is not None:
            return reserved
        return self._browser.finish_open()

    # ---------- 过程页 ----------
    @_collection_action
    def get_run(self) -> dict:
        with self._lock:
            state = self._ui_state()
            max_attempts = getattr(self.cfg, "max_attempts_per_page",
                                   shop_progress.DEFAULT_MAX_ATTEMPTS)
            if state.start_error is not None and self._resume_intent is None:
                # 子进程因「已有采集在跑」被拒：不碰数据库，直接说清原因。
                return self._collection_payload(views.run_view(None, state=state, now=self._now(),
                                     max_attempts=max_attempts))
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
                self._consume_resume_ack(Database(conn))
                state = self._ui_state()
                resume_error = state.start_error if self._resume_intent is not None else None
                if self._resume_intent is not None:
                    state = dataclasses.replace(state, start_error=None)
                if self.cfg.local_trial:
                    identity = self.current_crawler(conn)
                    if identity is not None and identity.round_id is not None:
                        self.round_id = identity.round_id
                    state = dataclasses.replace(state,
                                                crawler_running=identity is not None and identity.pid is not None)
                view = views.run_view(conn, state=state, now=self._now(),
                                    max_attempts=max_attempts)
                if resume_error is not None:
                    view["resume_error"] = resume_error
                if view.get("has_round"):
                    # 轮次由采集子进程创建，界面在这里随轮询认领它。
                    self.round_id = view["round_id"]
                # 手动入口与继续门禁只在「本界面明确暂停且停止收尾完成」这一格才有意义
                # （票 05）：其余状态一律禁用并给出原因。浏览器事实要跑一次进程枚举
                # （秒级），而过程页每 2 秒轮询一次——不当暂停时不去查它。
                if view.get("can_resume"):
                    browser, refusal, gate = self._browser_entry(conn)
                    view["browser"] = browser
                    view["continue_gate"] = gate
                    if refusal is not None:
                        view["login_refusal"] = refusal
                else:
                    refusal = self._login_refusal(conn)
                    if refusal is not None:
                        view["login_refusal"] = refusal
                if self.cfg.local_trial:
                    view["awaiting_resume"] = view.get("can_resume", False)
                return self._collection_payload(view)
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
            other_space=self._formal_cfg.db_file if self.cfg.local_trial else None,
        )

    def _spawn_crawler(self, keys: list[str] | None = None, *, new_intent=None, resume_intent=None):
        """拉起采集子进程；新建和继续均携带明确意图，继续冻结原轮完整范围。

        界面不发 `--ignore-plan`：逃生口只在命令行（ADR-0035）。
        """
        cmd = [_crawler_python(), str(PROJECT_ROOT / "run.py"), "--config", str(self.config_file)]
        if new_intent is not None:
            cmd += ["--gui-browser-settings", self._browser.settings.to_json()]
        if new_intent is not None:
            cmd += ["--new-round-intent", new_intent.to_json()]
        if resume_intent is not None:
            cmd += ["--resume-round-intent", resume_intent.to_json()]
        if self.cfg.local_trial:
            cmd += ["--local-trial"]
        limit = ",".join(k for k in (keys or []) if k)
        if limit:
            cmd += ["--limit-shops", limit]
        # 子进程静默运行：不弹控制台窗口，stdout/stderr 丢弃（详细日志仍写入 run.log）
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd,
            cwd=self._browser.settings.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    @_collection_action
    def start_run(self, keys: list[str]) -> dict:
        """开始一轮（按勾选的店铺）；闸门没开或角色不许时不发子进程（ADR-0035）。"""
        with self._lock:
            if self._closing_requested:
                return {"ok": False, "error": CLOSING_REFUSAL}
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
                db = Database(conn)
                reason = self._plan_block_reason(db)
                if reason is not None:
                    return {"ok": False, "error": reason}
                if self.cfg.local_trial:
                    self._refresh_shops(conn)
                    if not keys:
                        return {"ok": False, "error": "试采范围为空，请手动选择启用的店铺。"}
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
                # 只读新建预检：任何未结束轮都须明确中止，不隐式收养或结束它。
                # 轮次本身由采集子进程创建，启动失败不会留下空的「进行中」轮次。
                new_intent = rounds.plan_new(db, request)
            except ScopeMismatch as exc:
                return {"ok": False, "error": str(exc)}
            finally:
                conn.close()
            reason = self._browser.start_error()
            if reason:
                return {"ok": False, "error": reason, "retryable": True}
            self.round_id = None
            self.user_paused = False
            self._paused_round_id = None
            self._resume_intent = None
            try:
                self._spawn_crawler(keys, new_intent=new_intent)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"启动抓取失败：{exc}"}
            return {"ok": True}

    @_collection_action
    def resume_run(self) -> dict:
        """在“过程”页暂停后点击“继续”：重新拉起抓取，续跑本轮未完成店铺，并停留在过程页。"""
        with self._lock:
            if self._closing_requested:
                return {"ok": False, "error": CLOSING_REFUSAL}
            if is_merge_only(self.cfg) and not self.cfg.local_trial:
                # 续跑也是开采集（库里可能留着换角色 / 拷库带来的进行中轮次）：与开始页同一条拒绝。
                return {"ok": False, "error": plan_step.MERGE_ONLY_REFUSAL}
            if self.any_crawler_running():
                return {"ok": False, "error": _BUSY_ERROR}
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
                if self._stop_watch.status.phase is not stop_request.StopPhase.IDLE:
                    return {"ok": False, "error": "上一次停止仍在核验或收尾，请稍后重试。",
                            "retryable": True}
                db = Database(conn)
                self._consume_resume_ack(db)
                current = rounds.active_round(db, self._today())
                if current is None:
                    stale = rounds.active_round(db)
                    if stale is not None:
                        return {"ok": False, "error": (
                            f"轮次 #{stale.id}（{stale.run_date}）已经跨天，不能再续跑；"
                            "请先明确中止旧轮并等待收尾，再开始新轮。"
                        )}
                    return {"ok": False, "error": "没有进行中的轮次可继续。"}
                if not self.user_paused or self._paused_round_id != current.id:
                    return {"ok": False, "error": "没有本界面明确暂停并完成收尾的轮次可继续；"
                            "数据库尚未结束不等于已暂停，请先明确中止旧轮。"}
                if not current.resumable_on(self._now()):
                    return {"ok": False, "error": (
                        f"轮次 #{current.id}（{current.run_date}）现在不可续跑；"
                        "请点「开始抓取」新建一轮。"
                    )}
                rid = current.id
                resume_intent = rounds.plan_resume(db, rid, now=self._now())
            finally:
                conn.close()
            # 续跑与新建同一道浏览器门禁（票 05）：手动窗口没关、专用目录仍被
            # 占用或释不释放核不实，都不许继续——否则会借错误登录态或撞同一目录。
            # 关键配置在这次启动后变化也一样拒绝，不静默换目录。
            reason = self._browser.start_error()
            if reason:
                return {"ok": False, "error": reason, "retryable": True}
            self.round_id = rid
            try:
                # 子进程持锁复核预期轮次与完整快照，不采用开始页默认子集。
                self._spawn_crawler(resume_intent=resume_intent)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"继续抓取失败：{exc}"}
            # Popen success is not evidence that the driver entered execution.
            # The exact attempt is consumed only by the child's committed event.
            self._resume_intent = resume_intent
            return {"ok": True, "round_id": rid}

    def _consume_resume_ack(self, db: Database) -> None:
        """Only committed entry into execution consumes this GUI's pause."""
        if (self._resume_intent is not None
                and rounds.resume_execution_started(db, self._resume_intent)):
            self.user_paused = False
            self._paused_round_id = None
            self._resume_intent = None

    def _note_paused(self) -> None:
        self.user_paused = True

    @_collection_action
    def pause_run(self) -> dict:
        """暂停：写下停止请求，采集进程在下一个检查点自己停下（ADR-0009）。

        这一跳不阻塞、也不杀进程：请求写完就返回，倒计时与超时兜底交给轮询。
        """
        with self._lock:
            if self._closing_requested:
                return {"ok": False, "error": CLOSING_REFUSAL}
            if not self.cfg.local_trial:
                # Preserve the existing unknown-pause close refusal; only the
                # frozen round ID below grants an actual resume qualification.
                self.user_paused = True
            conn = self._open_conn()
            try:
                # Reconcile any older target before selecting the current crawler.
                # A replacement must settle the old watch without touching the new one.
                self._stop_watch.tick(conn)
                db = Database(conn)
                identity = self.current_crawler(conn) if self.cfg.local_trial else crawler_identity.registered(conn)
                if not self._own_crawler_alive() and not self.cfg.local_trial:
                    return {"ok": True}   # 本界面没有在跑的采集进程，没什么可暂停的
                if self.cfg.local_trial and identity is None:
                    return {"ok": True}
                target = stop_request.StopTarget.of(identity)
                if target is None:
                    if self._own_crawler_alive():
                        # 身份登记前没有浏览器副作用；只结束入口时冻结的 child。
                        log.info("暂停：身份尚未登记，结束入口时冻结的子进程。")
                        self._kill_proc()
                        if self.cfg.local_trial:
                            self._note_paused()
                        return {"ok": True}
                    return {"ok": False, "error": "暂时无法确认停止目标，请稍后重试。",
                            "retryable": True}
                try:
                    self._stop_watch.begin(
                        conn, stop_request.StopCommand(
                            stop_request.StopKind.PAUSE, target,
                            identity.round_id))
                    self._paused_round_id = self._stop_watch.status.round_id
                    self.user_paused = True
                    # A new explicit pause supersedes any previous start acknowledgment.
                    self._resume_intent = None
                except RuntimeError as exc:
                    return {"ok": False, "error": "暂时无法确认停止目标，请稍后重试。",
                            "retryable": True, "code": str(exc)}
                if self.cfg.local_trial:
                    self._note_paused()
                log.info("暂停：已写下停止请求（目标 PID %s），等它自己停下。", target.pid)
                return {"ok": True, "stopping": self._stop_watch.state}
            finally:
                conn.close()

    @_collection_action
    def prepare_abort(self, round_id: int | None = None) -> dict:
        """Freeze the round and target before the page displays its confirmation.

        指定 round_id 时冻结的是「没有活动采集的遗留轮」（开始页逐个显式处置）；
        不给编号时沿用关窗快照：运行中 / 暂停中照旧，另外把「本界面认领过、但已无
        进程且无终态」的那一轮也按遗留冻结（异常退出后仍停在过程页的那条路）。
        """
        with self._lock:
            if self._closing_requested:
                return {"ok": False, "error": CLOSING_REFUSAL}
            if round_id is not None:
                if not isinstance(round_id, int) or isinstance(round_id, bool):
                    return {"ok": False, "error": "轮次编号无效，请刷新后重试。"}
                return self._freeze_pending_abort(int(round_id))
            snapshot = self.close_snapshot()
            if (snapshot.kind in ("running", "paused", "aborting")
                    and snapshot.round_id is not None):
                token = secrets.token_urlsafe(24)
                self._abort_confirmation = (token, snapshot)
                return {"ok": True, "confirmation": token, "round_id": snapshot.round_id}
            if snapshot.kind == "idle" and snapshot.round_id is not None:
                return self._freeze_pending_abort(snapshot.round_id)
            return {"ok": False, "error": "当前轮次或采集目标不可核实，请重新确认。"}

    def _freeze_pending_abort(self, round_id: int) -> dict:
        """没有活动采集时，按指定轮次冻结一次「中止此轮」授权。"""
        conn = self._open_conn()
        try:
            self._stop_watch.tick(conn)
            if self._stop_watch.status.phase is not stop_request.StopPhase.IDLE:
                return {"ok": False, "error": "停止或收尾仍在进行，请稍后重试。",
                        "retryable": True}
            if self.any_crawler_running():
                return {"ok": False, "error": _BUSY_ERROR}
            db = Database(conn)
            try:
                target = rounds.load(db, round_id)
            except ValueError:
                return {"ok": False, "error": f"轮次 #{round_id} 不存在，请刷新后重试。"}
            if target.reason is not None:
                return {"ok": False, "error": f"轮次 #{round_id} 已经结束，无需中止。"}
            identity = crawler_identity.registered(conn)
            snapshot = CloseSnapshot("pending", stop_request.StopTarget.of(identity),
                                     target.id)
        finally:
            conn.close()
        token = secrets.token_urlsafe(24)
        self._abort_confirmation = (token, snapshot)
        return {"ok": True, "confirmation": token, "round_id": target.id,
                "run_date": target.run_date, "shops": list(target.shop_keys),
                "cross_day": target.run_date != self._today()}

    def _confirm_pending_abort(self, expected: CloseSnapshot) -> dict:
        """执行一次遗留轮中止：重核冻结事实，写终态，再结清可核实的收尾欠账。

        授权只覆盖冻结的那一轮与当时可见的目标：执行前重核；出现新目标、新采集或
        写库失败都拒绝或说明重试，不把旧确认套到后来出现的事实上。
        """
        conn = self._open_conn()
        try:
            self._stop_watch.tick(conn)
            if self._stop_watch.status.phase is not stop_request.StopPhase.IDLE:
                return {"ok": False, "error": "停止或收尾仍在进行，请稍后重试。",
                        "retryable": True}
            if self.any_crawler_running():
                return {"ok": False, "error": "已有采集在运行，不能按本次确认处置；"
                        "请先处置正在跑的这一轮。"}
            db = Database(conn)
            identity = crawler_identity.registered(conn)
            current_target = stop_request.StopTarget.of(identity)
            if current_target is not None and current_target != expected.target:
                return {"ok": False, "error": "采集目标已变化，请重新确认中止。"}
            try:
                target = rounds.load(db, expected.round_id)
            except ValueError:
                return {"ok": False, "error": "轮次已不存在，请重新确认中止。"}
            try:
                settled = rounds.finish_if_open(db, target, TerminalReason.ABANDONED,
                                                note="GUI 人工中止（放弃）")
            except (sqlite3.Error, ValueError) as exc:
                return {"ok": False, "error": f"中止或收尾尚未完成：{exc}",
                        "retryable": True}
            if self._paused_round_id == settled.id:
                self.user_paused = False
                self._paused_round_id = None
            result = {"ok": True, "round_id": settled.id, "reason": settled.reason.value}
            notes = []
            if settled.reason is not TerminalReason.ABANDONED:
                notes.append(f"轮次 #{settled.id} 已按其它方式结束，保留原终态，"
                             "未改为人工中止。")
            if identity is not None:
                # 处置旧轮的这一刻顺手结清它的残留登记：进程确已不在时删行，
                # OWNED 浏览器按精确绑定关；核不实就保留欠账，新轮继续被拦。
                try:
                    done = crawler_identity.settle_dead_registration(conn, identity)
                except (OSError, sqlite3.Error) as exc:
                    log.warning("遗留轮中止后身份收尾暂不可核验：%s", exc)
                    done = False
                if not done:
                    if crawler_identity.browser_cleanup_complete(identity.browser_state):
                        notes.append("原采集进程身份暂不可核验，残留登记已保留；"
                                     "不影响开始新轮。")
                    else:
                        result["cleanup"] = "pending"
                        notes.append("原采集浏览器或身份收尾暂未核清，"
                                     "新轮在收尾完成前仍被拦；"
                                     "可点「重试收尾核验」稍后再试。")
            if notes:
                result["note"] = "".join(notes)
            return result
        finally:
            conn.close()

    @_collection_action
    def prepare_cleanup_retry(self) -> dict:
        """冻结一次「重试收尾核验」：结清已无进程的残留登记与本次浏览器欠账。

        轮次已终态、待处置卡消失之后，这是产品内唯一的重试入口（11-A12 的
        「解释重试」）：没有终态不回写，只按原采集的精确绑定核验与清理。
        """
        with self._lock:
            if self._closing_requested:
                return {"ok": False, "error": CLOSING_REFUSAL}
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
                if self._stop_watch.status.phase is not stop_request.StopPhase.IDLE:
                    return {"ok": False, "error": "停止或收尾仍在进行，请稍后重试。",
                            "retryable": True}
                if self.any_crawler_running():
                    return {"ok": False, "error": _BUSY_ERROR}
                identity = crawler_identity.registered(conn)
                if identity is None:
                    return {"ok": False, "error": "没有待完成的收尾。"}
                snapshot = CloseSnapshot("cleanup", stop_request.StopTarget.of(identity),
                                         identity.round_id)
            finally:
                conn.close()
            token = secrets.token_urlsafe(24)
            self._abort_confirmation = (token, snapshot)
            return {"ok": True, "confirmation": token, "state": identity.browser_state}

    def _confirm_cleanup_retry(self, expected: CloseSnapshot) -> dict:
        """执行一次收尾重试：重核冻结目标，只清可核实的欠账。"""
        conn = self._open_conn()
        try:
            self._stop_watch.tick(conn)
            if self._stop_watch.status.phase is not stop_request.StopPhase.IDLE:
                return {"ok": False, "error": "停止或收尾仍在进行，请稍后重试。",
                        "retryable": True}
            if self.any_crawler_running():
                return {"ok": False, "error": "已有采集在运行，不能按本次确认处置；"
                        "请先处置正在跑的这一轮。"}
            identity = crawler_identity.registered(conn)
            current_target = stop_request.StopTarget.of(identity)
            if current_target is None:
                return {"ok": True, "cleanup": "settled",
                        "note": "残留登记已不存在，无需再核验。"}
            if current_target != expected.target:
                return {"ok": False, "error": "采集目标已变化，请重新确认重试。"}
            try:
                done = crawler_identity.settle_dead_registration(conn, identity)
            except (OSError, sqlite3.Error) as exc:
                log.warning("收尾重试暂不可核验：%s", exc)
                done = False
            if done:
                return {"ok": True, "cleanup": "settled"}
            return {"ok": True, "cleanup": "pending",
                    "note": "收尾暂未核清，欠账保留；新轮仍被拦，可稍后再试。"}
        finally:
            conn.close()

    @_collection_action
    def confirm_abort(self, confirmation: str) -> dict:
        """A page confirmation can only authorize its original round/target."""
        with self._lock:
            frozen = self._abort_confirmation
            if (frozen is None or not isinstance(confirmation, str)
                    or not secrets.compare_digest(confirmation, frozen[0])):
                return {"ok": False, "error": "中止确认已失效，请重新确认。"}
            expected = frozen[1]
            if expected.kind == "pending":
                return self._confirm_pending_abort(expected)
            if expected.kind == "cleanup":
                return self._confirm_cleanup_retry(expected)
            current = self.close_snapshot()
            if current.round_id != expected.round_id or current.kind == "unknown":
                return {"ok": False, "error": "轮次或采集目标已变化，请重新确认中止。"}
            if current.kind == "idle":
                conn = self._open_conn()
                try:
                    settled = rounds.load(Database(conn), expected.round_id)
                    if settled.reason is not None:
                        return {"ok": True, "round_id": settled.id, "reason": settled.reason.value}
                finally:
                    conn.close()
                return {"ok": False, "error": "原采集已退出但轮次未结束，请核实后重试。"}
            if (current.target != expected.target
                    and not (current.target is None and current.kind == "paused"
                             and current.pause_phase == "paused")):
                return {"ok": False, "error": "采集目标已变化，请重新确认中止。"}
            return self.abort_run(expected_target=current.target,
                                  expected_round_id=expected.round_id)

    def _lock_abort_confirmation(self, conn, expected_target) -> bool:
        """Serialize the frozen identity (including absence) and terminal write."""
        conn.execute("BEGIN IMMEDIATE")
        registered = crawler_identity.registered(conn)
        if ((registered is not None and stop_request.StopTarget.of(registered) != expected_target)
                or (expected_target is None and self.any_crawler_running())):
            conn.rollback()
            return False
        return True

    @_collection_action
    def abort_run(self, *, expected_target=None, expected_round_id=None) -> dict:
        """中止：先把轮次收尾为「人工放弃」，再等采集进程自己停下（ADR-0009）。

        顺序是「先写终态、再停止进程」：终态本身就是停止信号，采集进程在下一个
        检查点自己收尾（关浏览器、清身份行、释放采集锁）；先杀进程反而让这条通道
        永远走不到，事后也没人知道它停在哪一步。窗口内没停下才强杀。
        停的可能是本界面拉起的子进程，也可能是别处起的（命令行、上一次界面留下的）。
        """
        with self._lock:
            conn = self._open_conn()
            try:
                db = Database(conn)
                if expected_round_id is not None:
                    # Confirmation binds even an absent target. Serialize its final
                    # identity check and terminal write against CLI publications.
                    if not self._lock_abort_confirmation(conn, expected_target):
                        return {"ok": False, "error": "采集目标已变化，请重新确认中止。"}
                status = self._stop_watch.status
                if status.phase is not stop_request.StopPhase.IDLE:
                    # Promotion is authorized for the original frozen target only,
                    # including when that process has exited and cleanup remains.
                    if (status.round_id is None or (expected_round_id is not None and
                            (status.target != expected_target
                             or status.round_id != expected_round_id))):
                        return {"ok": False, "error": "停止目标已变化，请重新确认。"}
                    settled = rounds.finish_if_open(
                        db, rounds.load(db, status.round_id), TerminalReason.ABANDONED,
                        note="GUI 人工中止（放弃）")
                    self.user_paused = False
                    self._stop_watch.begin(conn, stop_request.StopCommand(
                        stop_request.StopKind.ABORT, status.target, status.round_id))
                    return {"ok": True, "round_id": settled.id,
                            "reason": settled.reason.value,
                            "stopping": self._stop_watch.state}
                self._stop_watch.tick(conn)
                # 这一处清理不是卫生，是语义：中止压过暂停——留下的暂停请求会让
                # 采集进程把这次停止认领成「暂停」，日志与店铺备注就写错了原因。
                identity = self.current_crawler(conn)
                if expected_round_id is not None and (
                        stop_request.StopTarget.of(identity) != expected_target
                        or (identity is not None and identity.round_id != expected_round_id)):
                    return {"ok": False, "error": "采集目标已变化，请重新确认关闭。"}
                if self.round_id is None and identity is None:
                    return {"ok": True}  # 没起过任务、也没有采集在跑：不必连库
                if (identity is None or identity.pid is None) and self.any_crawler_running():
                    return {"ok": False, "error": "暂时无法确认停止目标，请稍后重试。",
                            "retryable": True}
                rid = self._round_to_abandon(db, identity)
                if expected_round_id is not None and rid != expected_round_id:
                    return {"ok": False, "error": "轮次已变化，请重新确认中止。"}
                if rid is not None:
                    rounds.finish_if_open(db, rounds.load(db, rid), TerminalReason.ABANDONED,
                                          note="GUI 人工中止（放弃）")
                self.user_paused = False
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
            except (sqlite3.Error, RuntimeError, ValueError) as exc:
                return {"ok": False, "error": f"中止或收尾尚未完成：{exc}", "retryable": True}
            finally:
                conn.close()

    @_collection_action
    def close_snapshot(self) -> CloseSnapshot:
        """Actual close state; never infer pause solely from process disappearance."""
        with self._lock:
            conn = self._open_conn()
            try:
                self._consume_resume_ack(Database(conn))
                status = self._stop_watch.status
                promotion_error = None
                if (status.kind is stop_request.StopKind.PAUSE
                        and status.round_id is not None
                        and rounds.load(Database(conn), status.round_id).reason
                        is TerminalReason.ABANDONED):
                    try:
                        self._stop_watch.begin(conn, stop_request.StopCommand(
                            stop_request.StopKind.ABORT, status.target, status.round_id))
                    except Exception as exc:
                        # Terminal is committed. Retry promotion on the same watch,
                        # without consuming pause ACKs or resetting its deadlines.
                        log.warning("人工放弃已提交，原暂停升级待重试：%s", exc)
                        promotion_error = "database_unavailable"
                status = self._stop_watch.status if promotion_error else self._stop_watch.tick(conn)
                identity = self.current_crawler(conn)
                target = stop_request.StopTarget.of(identity)
                rid = (identity.round_id if identity is not None else
                       self._paused_round_id if self.user_paused
                       and self._paused_round_id is not None else self.round_id)
                if status.phase is not stop_request.StopPhase.IDLE:
                    if (status.round_id is None
                            or (target is not None and target != status.target)):
                        return CloseSnapshot("unknown", target, status.round_id, status.code)
                    stop_reason = rounds.load(Database(conn), status.round_id).reason
                    kind = ("paused" if status.kind is stop_request.StopKind.PAUSE
                            and stop_reason is None else "aborting")
                    phase = ("stopping" if self.any_crawler_running()
                             and status.phase is not stop_request.StopPhase.CLEANUP_PENDING
                             else "cleanup") if kind == "paused" else None
                    return CloseSnapshot(kind, status.target, status.round_id,
                                         promotion_error or status.code, phase)
                reason = rounds.load(Database(conn), rid).reason if rid is not None else None
                # An external replacement is running, not this GUI's old pause.
                if self.any_crawler_running():
                    if target is None or rid is None:
                        return CloseSnapshot("unknown", target, rid)
                    kind = "aborting" if reason is TerminalReason.ABANDONED else "running"
                    return CloseSnapshot(kind, target, rid)
                if self.user_paused and reason is None:
                    if (self._paused_round_id is None or rid != self._paused_round_id
                            or target is not None):
                        return CloseSnapshot("unknown", target, rid)
                    return CloseSnapshot("paused", round_id=rid, pause_phase="paused")
                return CloseSnapshot("idle", round_id=rid)
            finally:
                conn.close()

    @_collection_action
    def begin_close(self, expected: CloseSnapshot) -> dict:
        """Freeze authorization, block GUI starts, reuse the existing abort entry."""
        with self._lock:
            current = self.close_snapshot()
            if expected.kind == "paused":
                # Only natural progression of this explicitly paused round is
                # allowed; a replacement or a new terminal needs fresh review.
                if (current.kind != "paused" or current.round_id != expected.round_id
                        or (current.target != expected.target
                            and not (current.target is None
                                     and current.pause_phase == "paused"))):
                    return {"ok": False, "error": "暂停轮次或采集目标已变化，请重新确认关闭。"}
            elif current.kind == "idle" and expected.target is not None:
                conn = self._open_conn()
                try:
                    if rounds.load(Database(conn), expected.round_id).reason is None:
                        return {"ok": False, "error": "原采集已退出但轮次未结束，请核实后重试。"}
                finally:
                    conn.close()
            elif (current.target != expected.target or current.round_id != expected.round_id
                  or current.kind != expected.kind):
                return {"ok": False, "error": "采集状态已变化，请重新确认关闭。"}
            if current.kind == "paused":
                conn = self._open_conn()
                try:
                    if not self._lock_abort_confirmation(conn, current.target):
                        return {"ok": False, "error": "采集目标已变化，请重新确认关闭。"}
                    db = Database(conn)
                    rounds.finish_if_open(db, rounds.load(db, expected.round_id),
                                          TerminalReason.ABANDONED,
                                          note="GUI 关窗人工中止（放弃）")
                    if rounds.load(db, expected.round_id).reason is not TerminalReason.ABANDONED:
                        return {"ok": False, "error": "轮次已结束，不能改写终态；请重新确认关闭。"}
                    self.user_paused = False
                    self._closing_requested = True
                    status = self._stop_watch.status
                    if status.phase is not stop_request.StopPhase.IDLE:
                        try:
                            self._stop_watch.begin(conn, stop_request.StopCommand(
                                stop_request.StopKind.ABORT, status.target, expected.round_id))
                        except Exception as exc:
                            # Request accepted, not teardown completed. The independent
                            # close driver retries this exact promotion via snapshots.
                            log.warning("人工放弃已核实，停止请求清理待重试：%s", exc)
                    return {"ok": True}
                finally:
                    conn.close()
            if current.kind == "unknown":
                return {"ok": False, "error": "暂时不能安全关闭，窗口保持打开。"}
            self._closing_requested = True
            if current.kind == "idle":
                return {"ok": True}
            if (current.kind == "aborting"
                    and self._stop_watch.status.phase is not stop_request.StopPhase.IDLE):
                # Snapshot reconciliation also retries a committed pause promotion.
                return {"ok": True}
            return self.abort_run(expected_target=expected.target,
                                  expected_round_id=expected.round_id)

    @_collection_action
    def close_ready(self, expected: CloseSnapshot) -> bool:
        """A request succeeding is not completion; recheck just before destroy."""
        with self._lock:
            if self.close_snapshot().kind != "idle":
                return False
            if expected.target is not None or expected.kind == "paused":
                conn = self._open_conn()
                try:
                    reason = rounds.load(Database(conn), expected.round_id).reason
                    return (reason is TerminalReason.ABANDONED if expected.kind == "paused"
                            else reason is not None)
                finally:
                    conn.close()
            return True

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
    @_collection_action
    def get_result(self) -> dict:
        with self._lock:
            conn = self._open_conn()
            try:
                self._stop_watch.tick(conn)
                max_attempts = getattr(self.cfg, "max_attempts_per_page",
                                       shop_progress.DEFAULT_MAX_ATTEMPTS)
                return self._collection_payload(views.result_view(conn, state=self._ui_state(), now=self._now(),
                                        max_attempts=max_attempts))
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


def _notify(text: str, title: str = WINDOW_TITLE) -> None:
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


def _window_exists(title: str) -> bool:
    """按标题问一句窗口在不在（第二个实例据此区分「已经在跑」与「还在启动」）。

    非 Windows 上无从查证：当它在——照旧说「已经打开」。
    """
    if os.name != "nt":
        return True
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        return bool(user32.FindWindowW(None, title))
    except OSError:
        return True


def _announce_already_open() -> None:
    """第二个实例不建窗口：界面已经开着就说它在哪；窗口还没出现就说它在启动。

    ADR-0035 起窗口要等「开轮前准备」落定才建，等待期的第二次双击会撞上「锁在、
    窗口不在」——那时说「请看已打开的窗口」会指向一个尚不存在的窗口。
    """
    log.info("界面已经打开，本次启动不建立第二个窗口。")
    if _window_exists(WINDOW_TITLE):
        if not _focus_existing_window(WINDOW_TITLE):
            log.info("没能把已有窗口前置，只做提示。")
        _notify("界面已经打开，请看已打开的那个窗口。", title=f"{WINDOW_TITLE}（已经在运行）")
        return
    log.info("界面锁在，但窗口还没建起来（开轮前准备未落定）：提示它在启动。")
    _notify("界面正在启动，请稍候——窗口会在确认完本周计划之后出现。",
            title=f"{WINDOW_TITLE}（正在启动）")


def _confirm_crawler_close(text: str) -> bool:
    """Cancellation is the safe default, including unattended/dialog-failure cases."""
    if os.name != "nt" or os.environ.get("BESTSELLER_NO_DIALOG", "").strip() == "1":
        log.warning("无法交互确认关窗，按取消处理。")
        return False
    try:
        # OK/Cancel, warning, default button 2, foreground/topmost.
        answer = ctypes.windll.user32.MessageBoxW(
            None, text, f"{WINDOW_TITLE}（确认中止并关闭）", 0x1 | 0x30 | 0x100 | 0x10000 | 0x40000)
        return answer == 1
    except Exception:
        log.exception("关窗确认框无法显示，按取消处理")
        return False


def _install_close_guard(window, api, *, confirm=None, notify=None, launch=None,
                         wait=time.sleep):
    install_close_guard(window, api, confirm=confirm or _confirm_crawler_close,
                        notify=notify or _notify, title=WINDOW_TITLE,
                        launch=launch, wait=wait)


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
        try:
            api = Api()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _notify(f"配置错误，不能打开采集界面：{exc}\n请修正本机配置后重新打开。")
            return 2
        _configure_gui_logging(api.cfg)
        # 窗口等「开轮前准备」的公开入口返回再建（ADR-0035）：入口只等落定或它的
        # 30 秒剩余预算，准备与预算的编排都在持有者里，main 不另起线程、不自己计时。
        # 落定后的缺口检查这类慢活在后台继续跑，不占开窗；开始页画出来时默认勾选
        # 就已经是本机份额那份（份额里今天已采够的默认不勾，票据 17）。
        api.prepare_for_window()
        html = (PROJECT_ROOT / "bestseller_monitor" / "pages" / "ui_live.html").read_text(encoding="utf-8")
        window = webview.create_window(
            WINDOW_TITLE,
            html=html,
            js_api=api,
            width=1120,
            height=_default_window_height(),
            min_size=(960, 720),
        )
        _install_close_guard(window, api)
        webview.start(debug=False)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
