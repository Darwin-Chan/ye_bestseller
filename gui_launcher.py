"""采集壳（IS-52 / ADR-0007）：`dist/bestseller_gui.exe` 就是本文件打出来的。

壳的正文在 `launcher_core.py`（两只壳共用）；本文件只填采集目标的差异：拉
`<项目根>\\gui.py`、项目根标记是 `gui.py` + `bestseller_monitor`、弹窗政策是
「非零退出都弹」（行为与抽芯前一致）。采集中途的参数一个都不转发。
"""
from __future__ import annotations

import sys

from launcher_core import LauncherSpec
from launcher_core import main as run_shell

SPEC = LauncherSpec(
    target="gui",
    noun="界面",
    markers=("gui.py", "bestseller_monitor"),
    script="gui.py",
    log_name="gui_launcher.log",
    dialog_title="1688 畅销品监控 · 无法启动",
    project_modules=("bestseller_monitor", "gui"),
)


def main(argv: list[str] | None = None) -> int:
    return run_shell(argv, spec=SPEC)


if __name__ == "__main__":
    sys.exit(main())
