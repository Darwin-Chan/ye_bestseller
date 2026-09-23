"""分段跑全量测试：重段（长尾文件）各自一个进程，其余段一次 discover，跑完对账。

用法（在仓库根目录；收尾口径仍是隔离副本里跑、补过 config/*.toml 与 shops.csv）：

    python tools/run_full_suite.py

分段只改执行编排、不改用例集合：段并集 == 全量 discover。每次运行先按「加载口径」统计
每段预期条数，跑完核对「段计数和 == 预期总数」，不等即退出码 1。背景、实测与合规对账见
.scratch/test-suite-segmentation/全量测试分段研究-2026-09-23.md（不进版本库）。
"""
from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 重段：实测最慢、且以「真子进程 / 真打包 / 真文件 IO」为主的一批（各自一个进程）。
# 它们在负载下会放大 2–3 倍，所以先跑、单独成段。刷新方法见研究 §4.4：全量加
# --durations 0，把逐用例耗时按模块聚合，从大到小取。
HEAVY_MODULES = [
    "test_exchange",
    "test_judgment_set",
    "test_plan_step",
    "test_export",
    "test_git_channel",
    "test_shops_sync",
]

_RAN_RE = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.MULTILINE)
_FAILED_RE = re.compile(r"^FAILED \((.*)\)", re.MULTILINE)
_OK_RE = re.compile(r"^OK(?: \(.*\))?$", re.MULTILINE)


def all_test_modules() -> list[str]:
    return sorted(p.stem for p in TESTS.glob("test_*.py"))


def expected_counts(modules: list[str]) -> dict[str, int]:
    """加载口径：逐模块 import 后只加载不运行，得到每段预期条数（对账基准）。"""
    for path in (REPO, TESTS):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    loader = unittest.TestLoader()
    counts: dict[str, int] = {}
    for name in modules:
        module = importlib.import_module(name)
        counts[name] = loader.loadTestsFromModule(module).countTestCases()
    return counts


def _parse_segment(text: str) -> dict:
    """从一段 unittest -v 日志里取 Ran / OK / FAILED。"""
    ran = _RAN_RE.search(text)
    failed = _FAILED_RE.search(text)
    return {
        "ran": int(ran.group(1)) if ran else None,
        "seconds": float(ran.group(2)) if ran else None,
        "ok": failed is None and _OK_RE.search(text) is not None,
        "detail": failed.group(1) if failed else "",
    }


def run_segment(label: str, patterns: list[str], log_path: Path,
                durations: int, timeout: float) -> dict:
    cmd = [sys.executable, "-X", "utf8", "-m", "unittest", "discover",
           "-s", "tests", "-t", "tests", "-v"]
    if durations >= 0:
        cmd += ["--durations", str(durations)]
    for pattern in patterns:
        cmd += ["-k", pattern]

    started = time.perf_counter()
    timed_out = False
    with open(log_path, "wb") as log_file:
        log_file.write(("# 命令：" + " ".join(cmd) + "\n\n").encode("utf-8"))
        log_file.flush()
        try:
            proc = subprocess.run(cmd, cwd=REPO, stdout=log_file,
                                  stderr=subprocess.STDOUT,
                                  timeout=timeout or None)
            code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out, code = True, None
    elapsed = time.perf_counter() - started

    text = log_path.read_text(encoding="utf-8", errors="replace")
    result = _parse_segment(text)
    if timed_out:
        result["ok"], result["detail"] = False, "超时"
    result.update(label=label, wall=elapsed, log=log_path)
    return result


def _fmt_result(res: dict) -> str:
    ran = "?" if res["ran"] is None else str(res["ran"])
    state = "OK" if res["ok"] else "FAILED"
    if res["detail"]:
        state += f"（{res['detail']}）"
    return f"{ran} 项，{state}，{res['wall']:.1f}s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="分段跑全量测试（重段逐文件 + 其余段一次 discover，跑完对账）")
    parser.add_argument("--log-dir", default=None,
                        help="日志目录（默认 .scratch/logs/full-suite/<时间戳>）")
    parser.add_argument("--durations", type=int, default=0,
                        help="透传 unittest --durations N（默认 0，供刷新长尾名单）")
    parser.add_argument("--timeout", type=float, default=0,
                        help="单段墙钟上限秒数（默认不设；超时按该段失败记）")
    parser.add_argument("--heavy", default=None,
                        help="重段模块名，空格分隔（默认内置名单）")
    parser.add_argument("--rest", default=None,
                        help="其余段模块名，空格分隔（默认＝全部减去重段）")
    args = parser.parse_args(argv)

    durations = args.durations
    if sys.version_info < (3, 12) and durations >= 0:
        print("Python < 3.12 没有 unittest --durations，本次不传该参数。")
        durations = -1

    heavy = args.heavy.split() if args.heavy else list(HEAVY_MODULES)
    all_modules = all_test_modules()
    rest = (args.rest.split() if args.rest is not None
            else [m for m in all_modules if m not in heavy])

    problems = []
    for name in heavy + rest:
        if name not in all_modules:
            problems.append(f"tests/{name}.py 不存在")
    for name in set(heavy) & set(rest):
        problems.append(f"{name} 同时出现在重段与其余段")
    if problems:
        print("配置有问题：" + "；".join(problems), file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = (Path(args.log_dir) if args.log_dir
               else REPO / ".scratch" / "logs" / "full-suite" / stamp)
    log_dir.mkdir(parents=True, exist_ok=True)

    print(f"分段全量测试  仓库：{REPO}")
    print(f"日志目录：{log_dir}")
    print(f"重段 {len(heavy)} 个文件、其余段 {len(rest)} 个文件；加载口径核对中…")

    try:
        expected = expected_counts(heavy + rest)
    except Exception as exc:
        print(f"加载测试失败（先修 import 再跑）：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    empty = [m for m, n in expected.items() if n == 0]
    if empty:
        print("这些模块加载不到用例：" + "、".join(empty), file=sys.stderr)
        return 2
    expected_total = sum(expected.values())
    expected_rest = sum(expected[m] for m in rest)
    print(f"预期合计 {expected_total} 项"
          f"（重段 {expected_total - expected_rest} + 其余段 {expected_rest}）\n")

    results: list[dict] = []
    segments: list[tuple[str, list[str], int]] = [
        (name, [f"{name}.*"], expected[name]) for name in heavy]
    if rest:
        segments.append((f"其余段（{len(rest)} 文件）",
                         [f"{m}.*" for m in rest], expected_rest))

    for index, (label, patterns, want) in enumerate(segments, start=1):
        safe = label.replace("（", "(").replace("）", ")").replace(" ", "_")
        log_path = log_dir / f"seg-{index:02d}-{safe}.log"
        print(f"[{index}/{len(segments)}] {label}（预期 {want} 项）开始…", flush=True)
        res = run_segment(label, patterns, log_path, durations, args.timeout)
        res["expected"] = want
        results.append(res)
        print(f"    → {_fmt_result(res)}；日志 {log_path.name}", flush=True)

    total_ran = sum(r["ran"] or 0 for r in results)
    total_wall = sum(r["wall"] for r in results)
    mismatches = [r for r in results
                  if r["ran"] != r["expected"] or not r["ok"]]
    reconciled = total_ran == expected_total and not mismatches
    conclusion = "OK" if reconciled else "FAILED"

    lines = [f"分段全量测试 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             f"仓库：{REPO}",
             f"传参：python tools/run_full_suite.py"
             f" --log-dir {log_dir}"
             + (f" --heavy \"{' '.join(heavy)}\"" if args.heavy else "")
             + (f" --rest \"{' '.join(rest)}\"" if args.rest is not None else ""),
             ""]
    for res in results:
        expected_note = ("" if res["ran"] == res["expected"]
                         else f"，预期 {res['expected']}")
        lines.append(f"{res['label']}：{_fmt_result(res)}{expected_note}"
                     f"；日志 {res['log'].name}")
    lines += ["",
              f"合计 {total_ran} 项（预期 {expected_total}），总耗时 {total_wall:.1f}s，"
              f"对账{'一致' if total_ran == expected_total and not mismatches else '不一致'}，"
              f"结论 {conclusion}"]
    summary = "\n".join(lines) + "\n"
    (log_dir / "summary.txt").write_text(summary, encoding="utf-8", newline="\n")

    print()
    print(summary, end="")
    return 0 if reconciled else 1


if __name__ == "__main__":
    raise SystemExit(main())
