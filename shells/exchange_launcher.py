"""交换台壳（票 14 / ADR-0007）：`dist/inventory_exchange.exe` 就是本文件打出来的。

壳的正文在 `launcher_core.py`；本文件填交换台目标的差异：拉
`<项目根>\\exchange.py --window`、项目根标记是 `exchange.py` + `bestseller_monitor`、
弹窗政策「0/1 静默、2 与启动失败才弹」——退出码 1 是正常结局（「有需要人看一眼的」），
照搬采集壳的「非零都弹」会把正常结果弹成报错。

**壳不向子进程转发参数**：`--week`（补历史）、`--only`（只跑一半）、`--config` 是脚本的
参数，壳收到就明确拒绝并提示走脚本（双击入口没有参数可传，防的是命令行误用）。
"""
from __future__ import annotations

import os
import sys

from launcher_core import LauncherSpec, refuse
from launcher_core import main as run_shell

SPEC = LauncherSpec(
    target="exchange",
    noun="交换台",
    markers=("exchange.py", "bestseller_monitor"),
    script="exchange.py",
    script_args=("--window",),
    log_name="exchange_launcher.log",
    dialog_title="1688 畅销品监控 · 库存数据交换（无法启动）",
    quiet_exit_codes=frozenset({0, 1}),
    project_modules=("bestseller_monitor", "exchange"),
)

# 壳认得的参数：--check 系列是壳自己的开关；--window 与生俱来，写了当没看见。
KNOWN_ARGS = ("--check", "--check-report", "--window")


def _unknown_args(argv: list[str]) -> list[str]:
    """壳不认识的参数；`--check-report` 后面跟的那个值不算参数。"""
    rest: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item in KNOWN_ARGS:
            skip_next = item == "--check-report"
            continue
        if item.startswith("--check-report="):
            continue
        rest.append(item)
    return rest


def shell_refusal(argv: list[str]) -> str | None:
    """壳不该管的参数 → 一句给人看的说明；没有问题就 None。"""
    unknown = _unknown_args(argv)
    if not unknown:
        return None
    return (
        f"交换台壳不认识这些参数：{' '.join(unknown)}。\n"
        "壳只负责开窗（等价于 python exchange.py --window）；要指定周窗口、只跑一半或换配置，"
        "请在项目根目录运行脚本，例如：\n"
        "  python exchange.py --week 2026-W37"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    refusal = shell_refusal(argv)
    if refusal is not None:
        return refuse(refusal, env=os.environ, spec=SPEC)
    return run_shell(argv, spec=SPEC)


if __name__ == "__main__":
    sys.exit(main())
