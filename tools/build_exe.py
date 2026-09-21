"""重建 `dist\\` 下的启动壳（采集壳 / 交换台壳 / 分析壳），并自检产物里没有项目代码（IS-52 / ADR-0007）。

壳只带自己的代码与第三方库。`gui`、`exchange`、`analyze`、`bestseller_monitor` 一旦出现在
产物里，就说明打包退回了「入口脚本冻结、抓取包走源码目录」的半套形态——自检在这里直接失败，
别把它交给用户。

用法：`python tools/build_exe.py --target gui|exchange|analysis`
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# exe 里出现这些名字，就是夹带了项目代码。分析壳的脚本名是 analyze.py（目标键叫 analysis）。
PROJECT_MODULES = ("gui", "exchange", "analyze", "bestseller_monitor")

# PyInstaller 的 TOC 是 repr 出来的结构：模块名是单引号里的点分标识符。
_QUOTED_NAME_RE = re.compile(r"'([A-Za-z_][A-Za-z0-9_.]*)'")


@dataclasses.dataclass(frozen=True)
class Target:
    """一只壳的构建坐标：打包入口 spec、产物 exe、构建后要读的 TOC。"""

    spec: Path
    exe: Path
    toc: Path


TARGETS = {
    "gui": Target(spec=ROOT / "bestseller_gui.spec",
                  exe=ROOT / "dist" / "bestseller_gui.exe",
                  toc=ROOT / "build" / "bestseller_gui" / "Analysis-00.toc"),
    "exchange": Target(spec=ROOT / "bestseller_exchange.spec",
                       exe=ROOT / "dist" / "bestseller_exchange.exe",
                       toc=ROOT / "build" / "bestseller_exchange" / "Analysis-00.toc"),
    "analysis": Target(spec=ROOT / "bestseller_analysis.spec",
                       exe=ROOT / "dist" / "bestseller_analysis.exe",
                       toc=ROOT / "build" / "bestseller_analysis" / "Analysis-00.toc"),
}


class BuildCheckError(RuntimeError):
    """产物不合规：自检失败，不发布这个 exe。"""


def project_code_entries(toc_text: str) -> list[str]:
    """TOC 文本里冒出来的项目模块名（空列表 = 产物干净）。

    只认「整段都是模块名」的字符串，所以路径里的 `...\\bestseller_monitor\\db.py`
    不会被误判。
    """
    names = set(_QUOTED_NAME_RE.findall(toc_text))
    return sorted(
        name for name in names
        if name in PROJECT_MODULES or name.startswith("bestseller_monitor.")
    )


def assert_no_project_code(toc_path: Path) -> None:
    """读打包留下的 TOC，夹带了项目代码就直接失败。"""
    toc_path = Path(toc_path)
    offenders = project_code_entries(toc_path.read_text(encoding="utf-8", errors="replace"))
    if offenders:
        raise BuildCheckError(
            f"{toc_path.name} 里有项目代码：{'、'.join(offenders)}。"
            "壳只该带自己的代码，出现这些名字说明打包入口又指回了项目模块。"
        )


def build(target: Target) -> None:
    """跑一遍 PyInstaller（spec 的入口是目标那只壳的薄入口）。"""
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", str(target.spec)],
        cwd=ROOT, check=True,
    )


def verify_exe(target: Target) -> dict:
    """让产物自己报一遍：它认得项目根、跑在冻结态、身上没有项目代码。"""
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "check.json"
        done = subprocess.run(
            [str(target.exe), "--check", "--check-report", str(report_path)],
            cwd=ROOT,
            env={**os.environ, "BESTSELLER_NO_DIALOG": "1"},
        )
        if not report_path.is_file():
            raise BuildCheckError(
                f"产物没写出自检报告（退出码 {done.returncode}）：{target.exe}")
        report = json.loads(report_path.read_text(encoding="utf-8"))

    if done.returncode != 0 or not report.get("ok"):
        raise BuildCheckError(f"产物的 --check 没过：{report.get('error') or done.returncode}")
    if not report.get("frozen"):
        raise BuildCheckError("自检跑的不是打包产物（frozen=false），这次检查不算数。")
    if report.get("bundle_has_project_code"):
        raise BuildCheckError("产物里夹带了项目代码（bundle_has_project_code=true）。")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="重建 dist 下的启动壳，并自检产物里没有项目代码（IS-52 / ADR-0007）。")
    parser.add_argument("--target", choices=sorted(TARGETS), required=True,
                        help="建哪只壳：gui = 采集壳（dist/bestseller_gui.exe）、"
                             "exchange = 交换台壳（dist/bestseller_exchange.exe）、"
                             "analysis = 分析壳（dist/bestseller_analysis.exe）")
    args = parser.parse_args(argv)
    target = TARGETS[args.target]
    build(target)
    assert_no_project_code(target.toc)
    report = verify_exe(target)
    size_kb = target.exe.stat().st_size // 1024
    print(f"OK：{target.exe}（{size_kb} KB），项目根 {report['project_root']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
