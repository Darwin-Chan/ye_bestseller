"""手动验证：停止请求能不能让真进程自己停下（ADR-0009）。

起一个真进程跑一轮采集的骨架（检查点 + 长睡眠切片，不起浏览器、不碰线上库），
再像界面那样往库里写一条停止请求，看它是不是在窗口内自己收尾退出。

用法：
    python tools/diag_stop_request.py
退出码 0 = 两组都绿。红的一般意味着：请求没被认领、进程没自己停下、
或者表里的旧请求把新起的进程毒停了。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHILD_ENV_DB = "DIAG_STOP_DB"
CHILD_ENV_LOCK = "DIAG_STOP_LOCK"

# 进程自己停下的上限：真采集要靠检查点与睡眠切片，这里给足余量再判红。
SELF_STOP_SEC = 3.0


def mode_child() -> int:
    """跑一轮采集的骨架：只有检查点和长睡眠，走的是真实的停止通道。"""
    from unittest.mock import patch

    from bestseller_monitor import pipeline, rounds, single_instance
    from bestseller_monitor.config import Shop
    from bestseller_monitor.delay import Humanizer
    from bestseller_monitor.db import utcnow

    # 日志当成证据用：父进程从这一行里读「请求被认领了」（真采集写进 run.log）。
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(levelname)s %(message)s", force=True)

    cfg = SimpleNamespace(
        db_file=Path(os.environ[CHILD_ENV_DB]),
        driver="pw_cdp",
        shop_csv=None,
        ensure_dirs=lambda: None,
        long_pause_interval=(1, 1000),   # 不插长停顿
        long_pause_sec=(0.0, 0.0),
        detail_delay_sec=(0.5, 0.5),
        list_delay_sec=(0.0, 0.0),
        action_delay_sec=(0.0, 0.0),
        read_delay_sec=(0.0, 0.0),
        batch_size=1_000_000,
        batch_rest_sec=(0.0, 0.0),
        retry_base_sec=0.0,
        retry_jitter_sec=0.0,
    )
    shops = [Shop("A01", "店铺A", "https://A01.example/")]

    def skeletal_round(db, cfg_, round_id, _shops):
        human = Humanizer(cfg_)
        while True:
            rounds.ensure_workable(db, round_id, utcnow())   # 检查点
            human.sleep(10)                                  # 切成 0.5 秒的片

    # 只换掉「怎么抓」这一段：轮次、身份行、停止通道都走真实代码。
    with patch.object(single_instance, "CRAWLER_LOCK", os.environ[CHILD_ENV_LOCK]), \
            patch.object(pipeline, "_run_pwcdp_round", side_effect=skeletal_round):
        pipeline.run_round(cfg, shops)
    return 0


def _row(db_path: Path, sql: str):
    from bestseller_monitor.db import connect

    conn = connect(db_path)
    try:
        return conn.execute(sql).fetchone()
    finally:
        conn.close()


def _identity(db_path: Path):
    return _row(db_path, "SELECT * FROM crawler_process WHERE id=1")


def _stop_request(db_path: Path):
    return _row(db_path, "SELECT * FROM stop_requests WHERE id=1")


def spawn_child(db_path: Path, lock: str) -> subprocess.Popen:
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               **{CHILD_ENV_DB: str(db_path), CHILD_ENV_LOCK: lock})
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "child"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )


def wait_for_identity(db_path: Path, timeout: float = 20.0):
    """等采集进程登记身份行——界面也是从这一行拿目标进程的。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = _identity(db_path)
        if row is not None:
            return row
        time.sleep(0.05)
    return None


def wait_for(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _say(text: str) -> None:
    """按当前控制台编码打印：编不出来的字符换成问号，别让诊断脚本自己崩在这里。"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(enc, "replace").decode(enc, "replace"))


def _write_request(db_path: Path, row, *, pid: int) -> None:
    from bestseller_monitor import stop_request
    from bestseller_monitor.db import Database, connect

    conn = connect(db_path)
    try:
        Database(conn).request_stop(
            round_id=row["round_id"], kind=stop_request.PAUSE,
            target_pid=pid, target_started_at=row["started_at"],
        )
    finally:
        conn.close()


def scenario_self_stop(workdir: Path, lock: str) -> tuple[bool, list[str]]:
    """界面写请求 → 采集进程在检查点自己停下，轮次保持进行中。"""
    from bestseller_monitor import single_instance

    log: list[str] = []
    db_path = workdir / "self_stop.db"
    child = spawn_child(db_path, lock)
    try:
        row = wait_for_identity(db_path)
        if row is None:
            log.append("红：采集进程没有登记身份行")
            return False, log
        log.append(f"采集进程 PID={row['pid']} 轮次 #{row['round_id']} 起跑")
        if not single_instance.is_held(lock):
            log.append("红：会话锁没被持有，这一轮不是真在跑")
            return False, log

        # 界面那一跳：写请求（读的就是身份行里的目标）。
        started = time.time()
        _write_request(db_path, row, pid=int(row["pid"]))
        stopped = wait_for(lambda: not single_instance.is_held(lock), SELF_STOP_SEC)
        elapsed = time.time() - started
        child.wait(timeout=10)
        out = child.stdout.read() if child.stdout is not None else ""
        # 回执是界面用来放宽窗口的凭据；它能被认领才会写下这一行（真采集写进 run.log）。
        ack_seen = "已认领界面暂停请求" in out
        log.append(f"请求发出 {elapsed:.2f} 秒后进程停下（认领={'有' if ack_seen else '无'}，"
                   f"退出码={child.returncode}）")

        round_row = _row(db_path, "SELECT terminal_reason FROM rounds ORDER BY id DESC LIMIT 1")
        green = (stopped and ack_seen and child.returncode == 0 and elapsed < SELF_STOP_SEC
                 and round_row is not None and round_row["terminal_reason"] is None
                 and _stop_request(db_path) is None)
        log.append(f"轮次终态={round_row['terminal_reason'] if round_row else '（没有轮次）'}"
                   f" 停止请求={'已清掉' if _stop_request(db_path) is None else '还在'}")
        if out.strip():
            log.append("采集进程输出：" + " / ".join(
                line.strip() for line in out.splitlines() if line.strip()))
        return green, log
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


def scenario_stale_request(workdir: Path, lock: str) -> tuple[bool, list[str]]:
    """表里留着指向别人的旧请求时，新起的采集进程不该被它停掉。"""
    from bestseller_monitor import single_instance

    log: list[str] = []
    db_path = workdir / "stale.db"
    child = spawn_child(db_path, lock)
    try:
        row = wait_for_identity(db_path)
        if row is None:
            log.append("红：采集进程没有登记身份行")
            return False, log
        _write_request(db_path, row, pid=int(row["pid"]) + 9999)
        time.sleep(2.0)
        still_running = child.poll() is None and single_instance.is_held(lock)
        log.append(f"旧请求指着别的 PID，等 2 秒后采集进程"
                   f"{'仍在跑（对）' if still_running else '已经停了（错）'}")
        return still_running, log
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


def main() -> int:
    lock = rf"Local\bestseller_diag_stop_{os.getpid()}"
    workdir = Path(tempfile.mkdtemp(prefix="diag_stop_request_"))
    _say(f"临时工作目录：{workdir}")
    green1 = green2 = False
    try:
        _say("\n[1/2] 界面请求暂停：采集进程在检查点自己停下（期望绿）")
        green1, log1 = scenario_self_stop(workdir, lock)
        for line in log1:
            _say("    " + line)

        _say("\n[2/2] 表里的旧请求不毒化新起的采集进程（期望绿）")
        green2, log2 = scenario_stale_request(workdir, lock)
        for line in log2:
            _say("    " + line)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    _say(f"\n结果：[1]={'绿' if green1 else '红'} [2]={'绿' if green2 else '红'}")
    return 0 if (green1 and green2) else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        raise SystemExit(mode_child())
    raise SystemExit(main())
