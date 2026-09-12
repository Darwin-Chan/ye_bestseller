"""1688 畅销品监控 · GUI 启动器/进度/结果（pywebview）。

行为约定：
  - GUI 只做启动器/监控，抓取仍由本机 `python run.py --limit-shops=...` 子进程执行。
  - GUI 读 data/bestseller.db 展示“今日/各店”数据，并每约 2 秒轮询数据库刷新过程页。
  - “暂停”＝终止子进程，轮次保留为“进行中”（可续跑）。
  - “中止（放弃）”＝终止子进程 + 把轮次标记为“已放弃”（数据保留、不再续跑、下次开新轮）。
  - “暂停/中止”后连带收尾本任务启动的浏览器（start_browser=true 时），避免残留 Edge（IS-43）。
  - 依赖：本机已登录 Edge + Playwright + pywebview 环境；打包的 exe 只是启动壳，
    界面与采集都来自源码目录（见 ADR-0007），所以改本文件不需要重新打包。
"""
from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import webview

from bestseller_monitor.config import Config, effective_pages_limit, load_shops
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


CST = timezone(timedelta(hours=8))

WINDOW_TITLE = "1688 畅销品监控 · 每日库存抓取"

log = logging.getLogger(__name__)

# 暂停/中止后收尾浏览器：抓取进程被强杀时端口可能还没监听，短暂重试几次（IS-43）。
_BROWSER_CLOSE_RETRY_SEC = 3.0
_BROWSER_CLOSE_RETRY_INTERVAL = 0.7

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


def _fmt_hhmm(iso_utc: str | None) -> str:
    if not iso_utc:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%H:%M")
    except ValueError:
        return "—"


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "进行中"
    s = int(seconds)
    if s < 60:
        return f"{s} 秒"
    m = s // 60
    if m < 60:
        return f"{m} 分"
    h, m = divmod(m, 60)
    return f"{h} 时 {m} 分"


def _fmt_minutes(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    return f"{round(seconds / 60, 1)} 分"


_TERMINAL_TEXT = {
    TerminalReason.COMPLETED: ("正常完成", "本轮正常完成。"),
    TerminalReason.DAY_BOUNDARY: (
        "跨天中止",
        "本轮因库存数据即将跨天而中止，已抓取数据已保留；请0点后启动新的抓取轮次。",
    ),
    TerminalReason.DENY_EXCEEDED: (
        "意外中止",
        "本轮因整轮 deny 达到阈值而意外中止，已抓取数据已保留；本轮不可续跑，请启动新的抓取轮次。",
    ),
    TerminalReason.FAIL_RATE_EXCEEDED: (
        "暂停待处理",
        "本轮因失败率超过阈值而暂停，需人工决策；未抓取店铺见下方。",
    ),
    TerminalReason.DETAIL_BUDGET_EXHAUSTED: (
        "预算耗尽",
        "本轮详情预算已用尽，已抓取数据已保留；剩余商品留待下一轮重新发现。",
    ),
    TerminalReason.ABANDONED: (
        "人工中止",
        "本轮由人工中止（放弃），已抓取数据已保留、不再续跑；未抓取店铺见下方。",
    ),
}


def _terminal_text(reason) -> tuple[str, str]:
    """轮次终态 → 结果页的标签与说明。改文案不影响任何判定。"""
    if reason is None:
        return "进行中", "本轮仍在进行；未抓取店铺见下方。"
    return _TERMINAL_TEXT.get(reason, ("意外中止", "本轮非正常结束，已抓取数据已保留；未抓取店铺见下方。"))


def _terminal_suffix(reason) -> str:
    """开始页摘要里的一句短注；进行中的轮次不加注。"""
    return "" if reason is None else "，" + _terminal_text(reason)[0]


def _duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        return (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
    except ValueError:
        return None


class Api:
    """暴露给 pywebview 前端的方法。返回 JSON 可序列化的基本类型。"""

    def __init__(self):
        self.cfg = Config.from_file(PROJECT_ROOT / "config" / "config.toml", root=PROJECT_ROOT)
        self.shops = [s for s in load_shops(self.cfg.shop_csv) if s.active]
        self._lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.round_id: int | None = None
        self.start_ts: float | None = None
        self.user_paused = False
        self._elapsed_base = 0.0
        self._run_start_ts: float | None = None

    # ---------- 基础 ----------
    def _open_conn(self) -> sqlite3.Connection:
        """走数据层的连接入口：它会建目录、建表并执行迁移。

        界面自己开连接就会绕过迁移，对着旧结构的库报 `no such column`（IS-37）。
        """
        return connect(self.cfg.db_file)

    @staticmethod
    def _today() -> str:
        return cst_date()

    def _current_elapsed(self) -> float:
        """当前已抓时长：运行中 = 累计段 + 当前段；暂停 = 仅累计段（冻结）。"""
        if self._run_start_ts is not None:
            return self._elapsed_base + (time.time() - self._run_start_ts)
        return self._elapsed_base

    # ---------- 开始页 ----------
    def _start_summary(self, conn) -> dict:
        today = self._today()
        today_rounds = rounds.on_date(Database(conn), today)
        if not today_rounds:
            return {
                "started": False,
                "rounds": 0,
                "text": "今天尚未开始",
            }

        lines = []
        for run in today_rounds:
            dur = _duration_seconds(run.started_at, run.finished_at)
            lines.append(
                f"{_fmt_hhmm(run.started_at)} 开始 · 跑约 {_fmt_dur(dur)}"
                f"{_terminal_suffix(run.reason)}"
            )
        return {
            "started": True,
            "rounds": len(today_rounds),
            "text": "\n".join(lines),
        }

    def _start_shops(self, conn) -> list[dict]:
        today = self._today()
        out = []
        for s in self.shops:
            p = conn.execute(
                "SELECT COUNT(DISTINCT offer_id) c FROM inventory WHERE shop_key=? AND date=?",
                (s.key, today),
            ).fetchone()["c"]
            k = conn.execute(
                "SELECT COUNT(*) c FROM inventory WHERE shop_key=? AND date=?",
                (s.key, today),
            ).fetchone()["c"]
            # 该店实际翻页上限：店铺未单独配置时回落到全局默认（与抓取逻辑一致）
            pages = effective_pages_limit(s, self.cfg)
            out.append({
                "key": s.key,
                "name": s.name,
                "products": p,
                "skus": k,
                "pages": pages,
                "default_checked": (p < pages * 30),
            })
        return out

    def get_start(self) -> dict:
        with self._lock:
            conn = self._open_conn()
            try:
                today = self._today()
                ov_products = conn.execute(
                    "SELECT COUNT(DISTINCT offer_id) c FROM inventory WHERE date=?", (today,)
                ).fetchone()["c"]
                ov_skus = conn.execute(
                    "SELECT COUNT(*) c FROM inventory WHERE date=?", (today,)
                ).fetchone()["c"]
                db = Database(conn)
                running = self.crawler_identity(conn)
                current = rounds.active_round(db, today)
                stale = None if current is not None else rounds.active_round(db)
                if running is not None:
                    hint = self._crawler_hint(running)
                elif current is not None:
                    hint = (f"轮次 #{current.id} 正在进行（{current.run_date}），"
                            "点「开始抓取」会按它的店铺范围续跑。")
                elif stale is not None:
                    hint = (f"轮次 #{stale.id}（{stale.run_date}）已经跨天，不会再续跑；"
                            "点「开始抓取」会新建一轮。")
                else:
                    hint = ""
                return {
                    "ov": {
                        "products": ov_products,
                        "skus": ov_skus,
                    },
                    "summary": self._start_summary(conn),
                    "shops": self._start_shops(conn),
                    "total_shops": len(self.shops),
                    "start_hint": hint,
                    "crawler": running,
                }
            finally:
                conn.close()

    # ---------- 过程页 ----------
    def _shop_metrics(self, conn, round_id: int, shop_key: str) -> tuple[int, int, int, float | None]:
        prod = conn.execute(
            "SELECT COUNT(DISTINCT offer_id) c FROM snapshots "
            "WHERE round_id=? AND shop_key=? AND page_status='成功' AND sku_id IS NOT NULL",
            (round_id, shop_key),
        ).fetchone()["c"]
        sku = conn.execute(
            "SELECT COUNT(*) c FROM snapshots WHERE round_id=? AND shop_key=? "
            "AND page_status='成功' AND sku_id IS NOT NULL",
            (round_id, shop_key),
        ).fetchone()["c"]
        deny = conn.execute(
            "SELECT COUNT(*) c FROM event_log WHERE round_id=? AND shop_key=? AND event='click_deny'",
            (round_id, shop_key),
        ).fetchone()["c"]
        ts = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM event_log WHERE round_id=? AND shop_key=?",
            (round_id, shop_key),
        ).fetchone()
        dur = None
        if ts and ts[0] and ts[1]:
            try:
                dur = (datetime.fromisoformat(ts[1]) - datetime.fromisoformat(ts[0])).total_seconds()
            except ValueError:
                dur = None
        return prod, sku, deny, dur

    def get_run(self) -> dict:
        with self._lock:
            if (self.proc is not None
                    and self.proc.poll() == single_instance.CRAWLER_BUSY_EXIT_CODE):
                return self._refused_start()
            running = self._own_crawler_alive()
            conn = self._open_conn()
            try:
                active = rounds.active_round(Database(conn), self._today())
                if active is None:
                    return {"running": running, "manually_paused": self.user_paused, "has_round": False}
                rid = active.id
                # 轮次由采集子进程创建，界面在这里随轮询认领它。
                self.round_id = rid
                started_at = active.started_at
                elapsed = self._current_elapsed()

                deny = conn.execute(
                    "SELECT COUNT(*) c FROM event_log WHERE round_id=? AND event='click_deny'",
                    (rid,),
                ).fetchone()["c"]
                done_rows = conn.execute(
                    "SELECT * FROM shop_rounds WHERE round_id=? AND list_status='完成' ORDER BY shop_key",
                    (rid,),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) c FROM shop_rounds WHERE round_id=?", (rid,)
                ).fetchone()["c"]
                todo_rows = conn.execute(
                    "SELECT * FROM shop_rounds WHERE round_id=? AND list_status!='完成' ORDER BY shop_key",
                    (rid,),
                ).fetchall()

                done = []
                for r in done_rows:
                    p, k, d, dur = self._shop_metrics(conn, rid, r["shop_key"])
                    done.append({
                        "key": r["shop_key"],
                        "name": r["shop_name"],
                        "products": p,
                        "skus": k,
                        "duration": _fmt_minutes(dur),
                        "deny": d,
                    })
                todo_names = [{"key": r["shop_key"], "name": r["shop_name"]} for r in todo_rows]

                # 当前处理中的店：未完成里最近有事件的
                current = None
                if todo_rows:
                    cur = conn.execute(
                        "SELECT shop_key FROM event_log WHERE round_id=? AND shop_key IN (%s) "
                        "ORDER BY id DESC LIMIT 1" % ",".join("?" for _ in todo_rows),
                        (rid, *(r["shop_key"] for r in todo_rows)),
                    ).fetchone()
                    if cur:
                        current = cur["shop_key"]

                return {
                    "running": running,
                    "manually_paused": self.user_paused,
                    "has_round": True,
                    "round_id": rid,
                    "started_hhmm": _fmt_hhmm(started_at),
                    "elapsed_sec": max(elapsed, 0),
                    "deny": deny,
                    "done_count": len(done),
                    "total_count": total,
                    "progress": (len(done) / total) if total else 0.0,
                    "current_shop": current,
                    "done": done,
                    "todo": todo_names,
                }
            finally:
                conn.close()

    # ---------- 控制 ----------
    @staticmethod
    def _refused_start() -> dict:
        """子进程因为「已有采集在跑」被拒绝：说清原因，别把它当成一轮跑完。"""
        return {
            "running": False,
            "manually_paused": False,
            "has_round": False,
            "start_error": "已有采集进程在运行：本次启动被拒绝了，等它跑完再试。",
        }

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

    @staticmethod
    def _crawler_hint(running: dict) -> str:
        """启动页在「已经有采集在跑」时说什么：谁在跑，以及点开始会被拒。"""
        parts = [
            f"轮次 #{running['round_id']}" if running.get("round_id") is not None else None,
            f"PID {running['pid']}" if running.get("pid") else None,
            f"{_fmt_hhmm(running['started_at'])} 起" if running.get("started_at") else None,
        ]
        who = "，".join(part for part in parts if part) or "身份未知"
        return (f"采集进程正在跑（{who}）：同一时刻只能有一个，现在点开始会被拒绝；"
                "要停它就用下面的「中止」按钮。")

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
        with self._lock:
            self.user_paused = True
            self._kill_proc()
            self._kill_browser()
            if self._run_start_ts is not None:
                self._elapsed_base += time.time() - self._run_start_ts
                self._run_start_ts = None
            return {"ok": True}

    def abort_run(self) -> dict:
        """中止：先让采集进程停下，再把轮次收尾为「人工放弃」。

        停的可能是本界面拉起的子进程，也可能是别处起的（命令行、上一次界面留下的）——
        顺序必须是先停进程、再写终态：反过来会撞上「不同终态不得覆盖」，那一轮就收不了尾。
        """
        with self._lock:
            self.user_paused = False
            self._kill_proc()
            self._kill_browser()
            if self.round_id is None and not self.any_crawler_running():
                return {"ok": True}  # 没起过任务、也没有采集在跑：不必连库
            conn = self._open_conn()
            try:
                db = Database(conn)
                identity = self.crawler_identity(conn)
                self._stop_crawler_process(identity)
                rid = self._round_to_abandon(db, identity)
                if rid is not None:
                    rounds.finish_if_open(db, rounds.load(db, rid), TerminalReason.ABANDONED,
                                          note="GUI 人工中止（放弃）")
                db.clear_crawler_process()
            finally:
                conn.close()
            return {"ok": True, "round_id": rid}

    def _round_to_abandon(self, db, identity: dict | None) -> int | None:
        """该收尾哪一轮：本界面认领过的 > 身份行里的 > 今天进行中的。"""
        if self.round_id is not None:
            return self.round_id
        if identity is not None and identity.get("round_id") is not None:
            return identity["round_id"]
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

    # ---------- 结果页 ----------
    def get_result(self) -> dict:
        with self._lock:
            conn = self._open_conn()
            try:
                db = Database(conn)
                run = (rounds.load(db, self.round_id) if self.round_id is not None
                       else rounds.latest(db, finished=True))
                if run is None:
                    return {"has_round": False}
                rid = run.id
                dur = _duration_seconds(run.started_at, run.finished_at)
                if dur is None and rid == self.round_id:
                    # 榜单未完成时轮次保持进行中、没有 finished_at，
                    # 即便抓取进程已经停下也仍可续跑，所以用当前已抓时长。
                    dur = self._current_elapsed()
                deny = conn.execute(
                    "SELECT COUNT(*) c FROM event_log WHERE round_id=? AND event='click_deny'",
                    (rid,),
                ).fetchone()["c"]
                prod_total = conn.execute(
                    "SELECT COUNT(DISTINCT offer_id) c FROM snapshots "
                    "WHERE round_id=? AND page_status='成功' AND sku_id IS NOT NULL",
                    (rid,),
                ).fetchone()["c"]
                sku_total = conn.execute(
                    "SELECT COUNT(*) c FROM snapshots WHERE round_id=? "
                    "AND page_status='成功' AND sku_id IS NOT NULL",
                    (rid,),
                ).fetchone()["c"]
                done_rows = conn.execute(
                    "SELECT * FROM shop_rounds WHERE round_id=? AND list_status='完成' ORDER BY shop_key",
                    (rid,),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) c FROM shop_rounds WHERE round_id=?", (rid,)
                ).fetchone()["c"]
                todo_rows = conn.execute(
                    "SELECT * FROM shop_rounds WHERE round_id=? AND list_status!='完成' ORDER BY shop_key",
                    (rid,),
                ).fetchall()
                done = []
                for r in done_rows:
                    p, k, d, dur_s = self._shop_metrics(conn, rid, r["shop_key"])
                    done.append({
                        "key": r["shop_key"],
                        "name": r["shop_name"],
                        "products": p,
                        "skus": k,
                        "duration": _fmt_minutes(dur_s),
                        "deny": d,
                    })
                todo = [{"key": r["shop_key"], "name": r["shop_name"]} for r in todo_rows]
                tag, note = _terminal_text(run.reason)
                return {
                    "has_round": True,
                    "round_id": rid,
                    "reason": run.reason.value if run.reason is not None else None,
                    "started_hhmm": _fmt_hhmm(run.started_at),
                    "finished_hhmm": _fmt_hhmm(run.finished_at),
                    "duration_text": _fmt_dur(dur),
                    "deny": deny,
                    "done_count": len(done),
                    "total_count": total,
                    "products_total": prod_total,
                    "skus_total": sku_total,
                    "done": done,
                    "todo": todo,
                    "note": note,
                    "tag": tag,
                }
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
