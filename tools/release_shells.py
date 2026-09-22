"""出壳的发布包：三只 exe 收进一个带校验清单的目录，供拷到别的机器（不上传、不进 git）。

用法：
    python tools/release_shells.py                # 重建三只壳，发布包落 dist/release/shells-<日期>/
    python tools/release_shells.py --no-build     # 不重建，直接打包 dist/ 里现成的 exe（可能过期）
    python tools/release_shells.py --out <目录>   # 自定义发布包位置

包里有什么：三只 exe、`MANIFEST.json`（每个文件的 sha256/大小 + 来源提交）与
`发布说明.txt`（给人看的放置与校验步骤）。接受方把三只 exe 放进目标机器的
`<项目根>\\dist\\` 即可双击使用——壳与机器无关（ADR-0007），改程序**不需要重新打包**，
只有壳正文（`shells/` 下那些 launcher 与 spec）改了才需要重发。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # noqa: E402

from tools import build_exe  # noqa: E402

NOTE_NAME = "发布说明.txt"
MANIFEST_NAME = "MANIFEST.json"

NOTE_TEMPLATE = """壳的发布包（{date}）

里面是什么
  三只启动壳的 exe（{exe_names}）与 MANIFEST.json（每个文件的 sha256）。

怎么用
  把三只 exe 放进目标机器的 <项目根>\\dist\\（exe 必须待在 dist\\ 里——项目根按它的位置
  推导），然后双击 {gui_name} 即可；也可以带上 --check 自查（会打印它认到的项目根）。
  壳与本机无关：它只找本机 python、拉起源码目录里的脚本，不挑机器。

什么时候要重发
  程序改了不用重发（改 gui.py、页面、bestseller_monitor 都不需要重新打包）；只有壳正文
  改了才要（shells/ 目录）。本包构建自提交 {commit}（shells 目录{clean_text}）。

校验（可选）
  certutil -hashfile <文件> SHA256   的输出与 MANIFEST.json 里的 sha256 对照。
"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def source_state() -> dict:
    """发布包要记的来源：构建自哪个提交、shells/ 当时干不干净（只有壳正文改了才需重发）。"""
    return {
        "git_commit": git_output("rev-parse", "HEAD"),
        "shells_clean": git_output("status", "--porcelain", "--", "shells") == "",
    }


def write_bundle(out_dir: Path, sources: dict[str, Path], meta: dict) -> list[dict]:
    """把 {目标键: exe 路径} 收进 out_dir，写清单与说明；返回清单条目（同内容进 MANIFEST）。"""
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    entries = []
    for key in sorted(sources):
        exe = Path(sources[key])
        shutil.copy2(exe, out_dir / exe.name)
        entries.append({
            "target": key,
            "exe": exe.name,
            "size": exe.stat().st_size,
            "sha256": sha256_of(exe),
        })

    manifest = {"created_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "git_commit": meta.get("git_commit", ""),
                "shells_clean": bool(meta.get("shells_clean", True)),
                "note": "壳与机器无关；把三只 exe 放进 <项目根>\\dist\\ 使用；改程序不需要重新打包。",
                "files": entries}
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    note = NOTE_TEMPLATE.format(
        date=datetime.date.today().isoformat(),
        exe_names="、".join(entry["exe"] for entry in entries),
        gui_name=next((e["exe"] for e in entries if e["target"] == "gui"), entries[0]["exe"]),
        commit=manifest["git_commit"] or "（未知）",
        clean_text="干净" if manifest["shells_clean"] else "有未提交改动——本包只含已保存的壳正文",
    )
    (out_dir / NOTE_NAME).write_text(note, encoding="utf-8")
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="出壳的发布包：重建三只壳（或打包现成的），收成带 sha256 清单的目录。")
    parser.add_argument("--out", type=Path, default=None,
                        help="发布包目录；缺省 dist/release/shells-<日期>/")
    parser.add_argument("--no-build", action="store_true",
                        help="不重建，直接打包 dist/ 里现成的 exe（快；可能过期）")
    args = parser.parse_args(argv)

    if args.no_build:
        for key, target in build_exe.TARGETS.items():
            if not target.exe.is_file():
                raise SystemExit(f"dist/ 里没有 {target.exe.name}（先跑 python tools/build_exe.py --target {key}）")
    else:
        for key in sorted(build_exe.TARGETS):
            target = build_exe.TARGETS[key]
            print(f"重建 {key}（{target.exe.name}）…")
            build_exe.build(target)
            build_exe.assert_no_project_code(target.toc)
            build_exe.verify_exe(key, target)

    out_dir = args.out or ROOT / "dist" / "release" / f"shells-{datetime.date.today().isoformat()}"
    sources = {key: target.exe for key, target in build_exe.TARGETS.items()}
    entries = write_bundle(out_dir, sources, source_state())
    print(f"OK：发布包 {out_dir}（{len(entries)} 只壳）")
    for entry in entries:
        print(f"  {entry['exe']}  {entry['size'] // 1024} KB  sha256={entry['sha256'][:12]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
