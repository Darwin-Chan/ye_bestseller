"""手动验证：轮次收尾与 GUI 暂停/中止后，本任务启动的 Edge 是否真的被关掉（IS-43）。

只在 Windows + 本机 Edge 上跑，使用独立临时 profile 与专用端口（默认 9333），
不碰线上 9222，也不碰用户的其它 Edge 窗口。

用法：
    python tools/diag_edge_cleanup.py
退出码 0 = 三组都绿；非 0 = 出现"轮次结束了但 Edge 还占着调试端口"。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bestseller_monitor import browser_proc  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = int(os.environ.get("EDGE_CLEANUP_PROBE_PORT", "9333"))
PROFILE = Path(os.environ.get("TEMP", ".")) / "edge_cleanup_probe_profile"


def msedge_count() -> int:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/NH"],
                         capture_output=True, text=True).stdout
    return len([ln for ln in out.splitlines() if ln.lower().startswith("msedge.exe")])


def port_owner() -> int | None:
    return browser_proc.listen_port_owner(PORT)


def cfg() -> SimpleNamespace:
    return SimpleNamespace(
        chrome_path=EDGE,
        attach_port=PORT,
        user_data_path=str(PROFILE),
        start_browser=True,
        timeout_ms=45000,
    )


def cleanup() -> None:
    owner = port_owner()
    if owner:
        browser_proc.terminate_process_tree(owner)
    time.sleep(2)


def round_once() -> dict:
    """跑一轮：open_session → close_session，返回插桩。"""
    from bestseller_monitor import browser_pw

    out: dict = {"port_owner_before": port_owner()}
    session = browser_pw.open_session(cfg())
    proc = browser_pw._launched_proc
    out["popen_pid"] = proc.pid if proc else None
    time.sleep(3)
    out["popen_exited_at_3s"] = proc.poll() is not None if proc else None
    out["cdp_browser_pid"] = browser_pw.cdp_browser_pid(session[1])
    browser_pw.close_session(session[0], session[1])
    time.sleep(3)
    out["port_owner_after_close"] = port_owner()
    return out


def mode_hold() -> int:
    """开一轮后挂着，等父进程像 GUI「暂停」那样强杀（TerminateProcess，不跑 finally）。"""
    from bestseller_monitor import browser_pw

    session = browser_pw.open_session(cfg())
    proc = browser_pw._launched_proc
    print(json.dumps({"popen_pid": proc.pid if proc else None,
                      "port_owner": port_owner(),
                      "cdp_browser_pid": browser_pw.cdp_browser_pid(session[1])}),
          flush=True)
    time.sleep(600)
    return 0


def mode_round() -> int:
    print(json.dumps(round_once(), ensure_ascii=False), flush=True)
    return 0


def run_child(mode: str, wait_s: float = 180):
    proc = subprocess.Popen([sys.executable, str(Path(__file__)), mode],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=wait_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    info = None
    for line in (out or "").splitlines():
        if line.strip().startswith("{"):
            try:
                info = json.loads(line.strip())
            except json.JSONDecodeError:
                pass
    if info is None and err:
        print("    子进程 stderr:", err.strip().splitlines()[-1][:200])
    return proc.returncode, info


def paused_crawler() -> subprocess.Popen:
    """起一个"正在跑"的抓取进程，然后像 GUI 暂停那样强杀它，留下它启动的浏览器。"""
    holder = subprocess.Popen([sys.executable, str(Path(__file__)), "hold"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(12)
    print(f"    暂停前端口占用者={port_owner()}")
    holder.terminate()
    try:
        holder.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        holder.kill()
    time.sleep(2)
    print(f"    强杀抓取进程后端口占用者={port_owner()}（= 残留浏览器）")
    return holder


def main() -> int:
    PROFILE.mkdir(parents=True, exist_ok=True)
    cleanup()
    print(f"基线：msedge={msedge_count()} 端口{PORT}占用者={port_owner()}")

    print("\n[1/3] 干净环境跑一轮（期望绿）")
    _, control = run_child("round")
    print("   ", control)
    green1 = bool(control) and control.get("port_owner_after_close") is None

    print("\n[2/3] 上一轮被暂停强杀留下浏览器，本轮正常收尾（期望绿）")
    cleanup()
    paused_crawler()
    _, after = run_child("round")
    print("   ", after)
    green2 = bool(after) and after.get("port_owner_after_close") is None

    print("\n[3/3] 上一轮被暂停强杀留下浏览器，GUI 暂停路径收尾（期望绿）")
    cleanup()
    paused_crawler()
    closed = browser_proc.close_browser(PORT, launched_by_us=True)
    time.sleep(2)
    print(f"    GUI 收尾关掉的 PID={closed} 端口占用者={port_owner()}")
    green3 = port_owner() is None

    cleanup()
    print(f"\n清理后：msedge={msedge_count()} 端口{PORT}占用者={port_owner()}")
    print(f"结果：[1]={'绿' if green1 else '红'} [2]={'绿' if green2 else '红'} "
          f"[3]={'绿' if green3 else '红'}")
    return 0 if (green1 and green2 and green3) else 1


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "hold":
        raise SystemExit(mode_hold())
    if arg == "round":
        raise SystemExit(mode_round())
    raise SystemExit(main())
