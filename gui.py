"""1688 畅销品监控 · GUI 启动器/进度/结果（pywebview + PyInstaller 单文件 exe）。

行为约定：
  - GUI 只做启动器/监控，抓取仍由本机 `python run.py --limit-shops=...` 子进程执行。
  - GUI 读 data/bestseller.db 展示“今日/各店”数据，并每约 2 秒轮询数据库刷新过程页。
  - “暂停”＝终止子进程，轮次保留为“进行中”（可续跑）。
  - “中止（放弃）”＝终止子进程 + 把轮次标记为“已放弃”（数据保留、不再续跑、下次开新轮）。
  - 依赖：本机已登录 Edge + Playwright 环境；exe 不内嵌抓取与浏览器。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import webview


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _project_root() -> Path:
    if _is_frozen():
        return Path(os.environ.get("BESTSELLER_PROJECT", r"F:/AI/projects/bestseller"))
    return Path(__file__).resolve().parent


def _python_exe() -> str:
    if not _is_frozen():
        return sys.executable
    for cand in (os.environ.get("BESTSELLER_PYTHON"), shutil.which("python"), shutil.which("python3")):
        if cand:
            return cand
    return "python"


PROJECT_ROOT = _project_root()
sys.path.insert(0, str(PROJECT_ROOT))

from bestseller_monitor.config import Config, load_shops  # noqa: E402
from bestseller_monitor.db import Database, cst_date, DAY_BOUNDARY_NOTE  # noqa: E402

CST = timezone(timedelta(hours=8))


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
        conn = sqlite3.connect(str(self.cfg.db_file))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _today() -> str:
        return cst_date()

    def _active_round(self, conn):
        return conn.execute(
            "SELECT id, started_at, phase, status FROM rounds WHERE status='进行中' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def _current_elapsed(self) -> float:
        """当前已抓时长：运行中 = 累计段 + 当前段；暂停 = 仅累计段（冻结）。"""
        if self._run_start_ts is not None:
            return self._elapsed_base + (time.time() - self._run_start_ts)
        return self._elapsed_base

    # ---------- 开始页 ----------
    def _start_summary(self, conn) -> dict:
        today = self._today()
        rounds_today = conn.execute(
            "SELECT id, started_at, finished_at, status, note FROM rounds"
        ).fetchall()
        today_ids = []
        for r in rounds_today:
            dt = datetime.fromisoformat(r["started_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.astimezone(CST).strftime("%Y-%m-%d") == today:
                today_ids.append(dict(r))
        started = bool(today_ids)

        if not started:
            return {
                "started": False,
                "rounds": 0,
                "text": "今天尚未开始",
            }

        lines = []
        for r in today_ids:
            started_at = r["started_at"]
            finished_at = r.get("finished_at")
            dur = None
            if finished_at:
                try:
                    dur = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
                except ValueError:
                    dur = None
            status = r["status"]
            if status == "意外中止" and r.get("note") == DAY_BOUNDARY_NOTE:
                note = "，跨天中止"
            else:
                note = {
                    "完成": "",
                    "已放弃": "，人工放弃",
                    "意外中止": "，deny 超限意外中止",
                    "需人工-失败率超限": "，失败率超限暂停",
                }.get(status, "")
            lines.append(f"{_fmt_hhmm(started_at)} 开始 · 跑约 {_fmt_dur(dur)}{note}")
        return {
            "started": True,
            "rounds": len(today_ids),
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
            pages = s.pages or self.cfg.max_pages_per_shop
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
                active = self._active_round(conn)
                return {
                    "ov": {
                        "products": ov_products,
                        "skus": ov_skus,
                    },
                    "summary": self._start_summary(conn),
                    "shops": self._start_shops(conn),
                    "total_shops": len(self.shops),
                    "active_round_id": active["id"] if active else None,
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
            running = self.proc is not None and self.proc.poll() is None
            conn = self._open_conn()
            try:
                active = self._active_round(conn)
                if active is None:
                    return {"running": running, "manually_paused": self.user_paused, "has_round": False}
                rid = int(active["id"])
                started_at = active["started_at"]
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
                    "phase": active["phase"],
                    "done": done,
                    "todo": todo_names,
                }
            finally:
                conn.close()

    # ---------- 控制 ----------
    def _spawn_crawler(self, keys: list[str]):
        limit = ",".join(k for k in keys if k)
        cmd = [_python_exe(), str(PROJECT_ROOT / "run.py"), "--limit-shops", limit]
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
            if self.proc is not None and self.proc.poll() is None:
                return {"ok": False, "error": "已有抓取任务在运行，请先暂停或中止。"}
            conn = self._open_conn()
            try:
                db = Database(conn)
                rid = db.start_or_resume()
            finally:
                conn.close()
            self.round_id = rid
            self.start_ts = time.time()
            self.user_paused = False
            self._elapsed_base = 0.0
            self._run_start_ts = time.time()
            try:
                self._spawn_crawler(keys)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"启动抓取失败：{exc}"}
            return {"ok": True, "round_id": rid}

    def resume_run(self) -> dict:
        """在“过程”页暂停后点击“继续”：重新拉起抓取，续跑本轮未完成店铺，并停留在过程页。"""
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return {"ok": False, "error": "抓取已在运行，无需继续。"}
            conn = self._open_conn()
            try:
                active = self._active_round(conn)
                if active is None:
                    return {"ok": False, "error": "没有进行中的轮次可继续。"}
                rid = int(active["id"])
                rows = conn.execute(
                    "SELECT shop_key FROM shop_rounds WHERE round_id=?", (rid,)
                ).fetchall()
                keys = [r["shop_key"] for r in rows]
            finally:
                conn.close()
            if not keys:
                return {"ok": False, "error": "该轮次没有可继续的店铺。"}
            self.round_id = rid
            self.start_ts = time.time()
            self.user_paused = False
            self._run_start_ts = time.time()
            try:
                self._spawn_crawler(keys)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"继续抓取失败：{exc}"}
            return {"ok": True, "round_id": rid}

    def pause_run(self) -> dict:
        with self._lock:
            self.user_paused = True
            self._kill_proc()
            if self._run_start_ts is not None:
                self._elapsed_base += time.time() - self._run_start_ts
                self._run_start_ts = None
            return {"ok": True}

    def abort_run(self) -> dict:
        with self._lock:
            self.user_paused = False
            self._kill_proc()
            if self.round_id is not None:
                conn = self._open_conn()
                try:
                    db = Database(conn)
                    db.abandon_round(self.round_id, note="GUI 人工中止（放弃）")
                finally:
                    conn.close()
            return {"ok": True}

    def _kill_proc(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    # ---------- 结果页 ----------
    def get_result(self) -> dict:
        with self._lock:
            conn = self._open_conn()
            try:
                round_id = self.round_id
                if round_id is None:
                    row = conn.execute(
                        "SELECT id, started_at, finished_at, status, phase, note FROM rounds "
                        "WHERE status!='进行中' ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT id, started_at, finished_at, status, phase, note FROM rounds WHERE id=?",
                        (round_id,),
                    ).fetchone()
                if row is None:
                    return {"has_round": False}
                row = dict(row)
                rid = int(row["id"])
                started = row["started_at"]
                finished = row.get("finished_at")
                dur = None
                if finished:
                    try:
                        dur = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
                    except ValueError:
                        dur = None
                elif rid == self.round_id:
                    # RoundPauseRequired keeps the round resumable, so it has no
                    # finished_at even though the current crawler process stopped.
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
                status = row["status"]
                if status == "已放弃":
                    note = "本轮由人工中止（放弃），已抓取数据已保留、不再续跑；未抓取店铺见下方。"
                    tag = "人工中止"
                elif status == "需人工-失败率超限":
                    note = "本轮因失败率超过阈值而暂停，需人工决策；未抓取店铺见下方。"
                    tag = "暂停待处理"
                elif status == "完成":
                    note = "本轮正常完成。"
                    tag = "正常完成"
                elif status == "意外中止":
                    if row.get("note") == DAY_BOUNDARY_NOTE:
                        note = "本轮因库存数据即将跨天而中止，已抓取数据已保留；请0点后启动新的抓取轮次。"
                        tag = "跨天中止"
                    else:
                        note = "本轮因整轮 deny 达到阈值而意外中止，已抓取数据已保留；本轮不可续跑，请启动新的抓取轮次。"
                        tag = "意外中止"
                else:
                    note = "本轮非正常结束，已抓取数据已保留；未抓取店铺见下方。"
                    tag = "意外中止"
                return {
                    "has_round": True,
                    "round_id": rid,
                    "status": status,
                    "started_hhmm": _fmt_hhmm(started),
                    "finished_hhmm": _fmt_hhmm(finished),
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


# 默认窗口高度：设计基准 1128 × 1.2 ≈ 1354；屏幕放不下时收缩到可用区域内。
_PREFERRED_HEIGHT = 1354
_MIN_HEIGHT = 720
_SCREEN_MARGIN = 60
_SPI_GETWORKAREA = 0x0030


def _screen_work_height() -> int | None:
    """当前屏幕可用高度（Windows 下排除任务栏）；查询不到返回 None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
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


def main():
    api = Api()
    here = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    ui_path = here / "docs" / "ui_live.html"
    if not ui_path.exists():
        ui_path = PROJECT_ROOT / "docs" / "ui_live.html"
    html = ui_path.read_text(encoding="utf-8")
    webview.create_window(
        "1688 畅销品监控 · 每日库存抓取",
        html=html,
        js_api=api,
        width=1120,
        height=_default_window_height(),
        min_size=(960, 720),
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
