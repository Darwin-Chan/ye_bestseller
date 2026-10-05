"""三只真 exe 的隔离编码探针（ADR-0046），不启动业务界面、不读真实配置。

缺省验证本机 python / pythonw；证据只落 .scratch/tool-output/encoding/。
每只壳按自身位置推导隔离项目根，运行一个在业务导入前输出中文的临时入口。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # noqa: E402
sys.path.insert(0, str(ROOT / "shells"))  # noqa: E402

from launcher_core import configure_tool_output  # noqa: E402
from tools import build_exe  # noqa: E402
from tools.release_shells import sha256_of  # noqa: E402

# 独立的验收口径，不从 should_notify 反算期待值。
_CASES = {
    "gui": ("gui.py", "gui_launcher.log", [], {1, 2}),
    "exchange": ("exchange.py", "exchange_launcher.log", ["--window"], {2}),
    "analysis": ("analyze.py", "analysis_launcher.log", [], {1, 2}),
}
_STDERR = "中文标准错误：入口导入前"
_STDOUT = "STDOUT_ONLY: 中文标准输出"
_TOKEN = "保留其它环境变量"


def default_interpreters() -> list[Path]:
    console = Path(sys.executable).with_name("python.exe")
    if not console.is_file():
        console = Path(sys.executable)
    interpreters = [console]
    windowless = console.with_name("pythonw.exe")
    if windowless.is_file():
        interpreters.append(windowless)
    return interpreters


def verify_shell_encoding(out_dir: Path, interpreters: list[Path]) -> dict:
    """保存 3 目标 × 解释器 × 退出码 0/1/2 的真壳证据；失败直接抛出。"""
    out_dir = out_dir.resolve()
    if not interpreters:
        raise ValueError("至少指定一个解释器")
    for python in interpreters:
        if not python.is_file():
            raise ValueError(f"解释器不存在：{python}")
    for target in build_exe.TARGETS.values():
        if not target.exe.is_file():
            raise ValueError(f"缺少壳：{target.exe}（先重建）")
    # 不覆盖或删除已有证据。正式入口与配置始终留在真实项目根，探针只写新目录。
    out_dir.mkdir(parents=True, exist_ok=False)
    cases = []
    for key, target in sorted(build_exe.TARGETS.items()):
        script_name, log_name, expected_args, notifying_codes = _CASES[key]
        for index, python in enumerate(interpreters, start=1):
            root = (out_dir / key / f"{index:02d}-{python.stem}").resolve()
            (root / "dist").mkdir(parents=True)
            (root / "bestseller_monitor").mkdir()
            exe = root / "dist" / target.exe.name
            shutil.copy2(target.exe, exe)
            digest = sha256_of(exe)
            probe = (
                "import json, os, sys\n"
                f"print({_STDOUT!r}, flush=True)\n"
                f"print({_STDERR!r}, file=sys.stderr, flush=True)\n"
                "print('PROBE=' + json.dumps({\n"
                "    'stdout_encoding': sys.stdout.encoding,\n"
                "    'stderr_encoding': sys.stderr.encoding,\n"
                "    'token': os.environ.get('ENCODING_PROBE_TOKEN'),\n"
                "    'args': sys.argv[1:]}, ensure_ascii=False), file=sys.stderr, flush=True)\n"
                "sys.exit(int(os.environ['ENCODING_PROBE_EXIT']))\n"
            )
            (root / script_name).write_text(probe, encoding="utf-8")
            env = {**os.environ, "BESTSELLER_PYTHON": str(python.resolve()),
                   "BESTSELLER_NO_DIALOG": "1", "PYTHONIOENCODING": "gbk",
                   "ENCODING_PROBE_TOKEN": _TOKEN}
            env.pop("BESTSELLER_PROJECT", None)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            check_path = root / "check.json"
            check = subprocess.run(
                [str(exe), "--check", "--check-report", str(check_path)],
                cwd=root, env=env, creationflags=flags, timeout=30)
            report = json.loads(check_path.read_text(encoding="utf-8"))
            if (check.returncode != 0 or not report.get("ok") or not report.get("frozen")
                    or report.get("target") != key or report.get("bundle_has_project_code")
                    or Path(report["project_root"]).resolve() != root
                    or Path(report["python"]).resolve() != python.resolve()):
                raise AssertionError(f"{key}/{python.name} 冻结壳自检失败：{report}")
            log_path = root / "logs" / log_name
            for exit_code in (0, 1, 2):
                offset = log_path.stat().st_size if log_path.exists() else 0
                done = subprocess.run(
                    [str(exe)], cwd=root,
                    env={**env, "ENCODING_PROBE_EXIT": str(exit_code)},
                    creationflags=flags, timeout=30)
                raw = log_path.read_bytes()[offset:]
                text = raw.decode("utf-8")
                evidence = root / f"exit-{exit_code}.log"
                evidence.write_bytes(raw)
                if done.returncode != exit_code:
                    raise AssertionError(f"{key}/{python.name} 退出码 {done.returncode} != {exit_code}")
                if _STDERR not in text or "\ufffd" in text or _STDOUT in text:
                    raise AssertionError(f"{key}/{python.name} 标准流不符合契约：{evidence}")
                lines = [line for line in text.splitlines() if line.startswith("PROBE=")]
                if not lines:
                    raise AssertionError(f"缺少子进程事实：{evidence}")
                facts = json.loads(lines[0].removeprefix("PROBE="))
                if facts != {"stdout_encoding": "utf-8", "stderr_encoding": "utf-8",
                             "token": _TOKEN, "args": expected_args}:
                    raise AssertionError(f"子进程事实不符合契约：{facts}")
                if ("启动失败：" in text) != (exit_code in notifying_codes):
                    raise AssertionError(f"{key} 的失败通知政策不符：{evidence}")
                cases.append({"target": key, "python": str(python.resolve()),
                              "exit_code": exit_code, "sha256": digest,
                              "evidence": str(evidence.relative_to(out_dir.resolve()))})
    summary = {"ok": True, "cases": cases, "count": len(cases),
               "note": "真 exe + 隔离入口；未启动业务界面、未读取真实配置。"}
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真壳隔离编码探针：不启动采集、浏览器或交换任务。")
    parser.add_argument("--out-dir", type=Path,
                        help="新的证据目录（不得已存在）；缺省 .scratch/tool-output/encoding/<时间戳>")
    parser.add_argument("--python", type=Path, action="append",
                        help="点名解释器，可重复；缺省验证本机 python 与 pythonw")
    args = parser.parse_args(argv)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out_dir = (args.out_dir or ROOT / ".scratch" / "tool-output" / "encoding" / stamp).resolve()
    result = verify_shell_encoding(out_dir, args.python or default_interpreters())
    print(f"OK：{result['count']} 个真壳用例；证据 {out_dir}")
    return 0


if __name__ == "__main__":
    configure_tool_output()
    raise SystemExit(main())
