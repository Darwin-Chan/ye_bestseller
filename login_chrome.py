"""在「正常、非自动化」的 Edge 窗口中手动登录 1688，保存登录态。

用法：
    python login_chrome.py

运行后会弹出独立的 Edge 窗口；用你的 1688/Taobao 账号扫码或账号登录一次，
登录成功并看到店铺/主页后，直接关闭该窗口即可。之后运行 MVP 会自动复用这份登录态。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bestseller_monitor.config import Config  # noqa: E402


def main() -> int:
    cfg = Config.from_file(ROOT / "config" / "config.toml", root=ROOT)
    chrome = cfg.chrome_path or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not Path(chrome).exists():
        print(f"未找到 Edge：{chrome}")
        return 1
    profile = cfg.user_data_path
    profile.mkdir(parents=True, exist_ok=True)
    url = "https://www.1688.com/"
    print("正在打开独立 Edge 窗口，请登录 1688 后关闭该窗口……")
    subprocess.Popen([
        chrome,
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ])
    print("已启动。请在弹出的 Edge 中完成登录，登录成功后直接关闭窗口即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
