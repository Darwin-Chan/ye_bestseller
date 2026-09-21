"""分析壳（票 14 / ADR-0007）：`dist/bestseller_analysis.exe` 就是本文件打出来的。

壳的正文在 `launcher_core.py`；本文件填分析目标的差异：拉 `<项目根>\\analyze.py`（缺省即开窗，
不带参数）、项目根标记是 `analyze.py` + `bestseller_monitor`、弹窗政策「非零都弹」（与采集壳
一致：分析脚本没有「非零即正常」的退出码语义；「分析已经打开」的第二个实例由脚本自己提示并以
退出码 0 收场，不走壳的弹窗）。

名实分裂是有意的：目标键、spec、薄入口、日志、`--target` 都叫 `analysis`，而被拉的脚本、
项目根标记、`project_modules` 叫 `analyze`（脚本名是 `analyze.py`）。写错会当场失败
（标记写 `analysis.py` 判「不是项目根」）。

**壳不向子进程转发参数**：`--config`（换整份分析配置）与 `--serve`（只起服务不开窗）是脚本的
参数，壳收到就明确拒绝并提示走脚本（双击入口没有参数可传，防的是命令行误用）。
"""
from __future__ import annotations

import os
import sys

from launcher_core import LauncherSpec, refuse
from launcher_core import main as run_shell

SPEC = LauncherSpec(
    target="analysis",
    noun="分析",
    markers=("analyze.py", "bestseller_monitor"),
    script="analyze.py",
    log_name="analysis_launcher.log",
    dialog_title="畅销品分析 · 无法启动",
    quiet_exit_codes=frozenset(),
    project_modules=("bestseller_monitor", "analyze"),
)

# 壳认得的参数：--check 系列是壳自己的开关。
KNOWN_ARGS = ("--check", "--check-report")


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
        f"分析壳不认识这些参数：{' '.join(unknown)}。\n"
        "壳只负责开窗（等价于 python analyze.py）；要换配置或只起本地服务，"
        "请在项目根目录运行脚本，例如：\n"
        "  python analyze.py --config config/analysis.toml"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    refusal = shell_refusal(argv)
    if refusal is not None:
        return refuse(refusal, env=os.environ, spec=SPEC)
    return run_shell(argv, spec=SPEC)


if __name__ == "__main__":
    sys.exit(main())
