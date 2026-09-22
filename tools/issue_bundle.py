"""运行问题记录与打包：把一台机器上出的问题收成一份可以整个拷走的记录包。

用法：
    python tools/issue_bundle.py --title "交换台报错：raw-m1 push 被拒"
        # 新建一份记录：自动采集机器事实、日志、库状态、截图，写好 REPORT.md 模板
    python tools/issue_bundle.py --pack
        # 在记录目录里填好 REPORT.md 后跑这条：算出 MANIFEST.json 并打成同名 .zip
    python tools/issue_bundle.py --list
        # 列出本机已有的记录与打包情况
    python tools/issue_bundle.py --unpack <记录包.zip>
        # 收到别台机器的记录包：解到 .scratch/inbox/ 并打印摘要（问题描述 + 缺项）

落点：`<项目根>/.scratch/tool-output/issues/<日期-时刻>-<机器>-<标题>/`，同名 `.zip` 就放在它旁边；
收到的包解到 `<项目根>/.scratch/inbox/<包名>/`。
规范与「收到包怎么读」：docs/ops/问题记录与打包.md。Windows 双击等价物：`tools\\issue_bundle.cmd`。

包里不含任何密钥：配置副本过一遍脱敏（只保留键与档位），环境变量只记「有没有设」，
`~/.cos.yaml`、`secrets/`、`.env` 一律不读。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import urllib.parse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # noqa: E402

ISSUES_SUBDIR = Path(".scratch") / "tool-output" / "issues"
INBOX_SUBDIR = Path(".scratch") / "inbox"
REPORT_NAME = "REPORT.md"
MANIFEST_NAME = "MANIFEST.json"
PLACEHOLDER = "（待填写）"
CST = datetime.timezone(datetime.timedelta(hours=8))

LOG_TAIL_LINES = 2000               # 每份日志保留的最后行数
LOG_FULL_LINE_COUNT_MAX = 20 * 1024 * 1024   # 超过这个字节数就不数总行数，直接取尾
MAX_SCREENSHOTS = 8
MAX_SCREENSHOT_BYTES = 3 * 1024 * 1024
MAX_RAW_PAGES = 3
MAX_RAW_PAGE_BYTES = 4 * 1024 * 1024
MAX_DIAG_FILES = 5
PROCESS_LIST_LIMIT = 60

# 库状态要带的表查询：键是 JSON 里的查询名，值是 (说明, SQL)。表可能在任何一条上缺失
# （老库、别的角色的库），每条各查各的、失败只记进 errors，不影响其余。
INVENTORY_QUERIES: dict[str, tuple[str, str]] = {
    "最近轮次": (
        "最近 10 轮的起止、日期与终态",
        "SELECT id, run_date, started_at, finished_at, terminal_reason, detail_budget_limit, note"
        " FROM rounds ORDER BY id DESC LIMIT 10",
    ),
    "采集身份": (
        "crawler_process 那一行：现在算不算有采集进程在跑",
        "SELECT * FROM crawler_process WHERE id = 1",
    ),
    "停止请求": (
        "stop_requests 那一行：有没有还没被认领的暂停请求",
        "SELECT * FROM stop_requests WHERE id = 1",
    ),
    "本周计划": (
        "weekly_plan 里最新一周的全部指派",
        "SELECT * FROM weekly_plan WHERE week = (SELECT MAX(week) FROM weekly_plan)"
        " ORDER BY machine_id, shop_key LIMIT 200",
    ),
    "导入口账": (
        "import_packages 最近 20 行：收过哪些包、插了多少行",
        "SELECT package_name, machine_id, week, imported_at, rows_total, rows_inserted,"
        " rows_replaced, conflicts FROM import_packages ORDER BY imported_at DESC LIMIT 20",
    ),
    "导入冲突": (
        "import_conflicts 最近 20 行：本机视角见过的取胜/败方",
        "SELECT imported_at, observed_date, shop_key, winner_machine, loser_machine,"
        " loser_rows_replaced, loser_rows_kept FROM import_conflicts"
        " ORDER BY imported_at DESC LIMIT 20",
    ),
    "计划外采集": (
        "plan_deviations 最近 20 行：越权/计划外采集",
        "SELECT run_date, week, shop_key, machine_id, kind, planned_machine, reason"
        " FROM plan_deviations ORDER BY recorded_at DESC LIMIT 20",
    ),
    "最近事件": (
        "event_log 最后 30 条：最后发生了什么",
        "SELECT round_id, ts, shop_key, offer_id, phase, event, kind, verification_type, note"
        " FROM event_log ORDER BY id DESC LIMIT 30",
    ),
}

# 配置副本的脱敏：键名带这些词的（key_env 这类「值就是变量名」的除外）值一律打码。
_SECRET_KEY_RE = re.compile(r"(?i)(secret|token|password|passwd|api[_-]?key|access[_-]?key)")
_ENV_KEEP_SUFFIX = "_env"


def slugify(title: str, limit: int = 40) -> str:
    """标题 → 文件名安全的一段：去掉 Windows 非法字符，空白并成 '-'，可空则给个兜底名。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", title).strip()
    cleaned = re.sub(r"[\s-]+", "-", cleaned).strip("-.")
    return cleaned[:limit] or "未命名"


def now_stamps() -> tuple[str, str]:
    """(本机时间, 北京时间) 两个 ISO 字符串——记录里两个都给，跨时区对不上时好认。"""
    local = datetime.datetime.now().astimezone()
    cst = datetime.datetime.now(CST)
    return local.isoformat(timespec="seconds"), cst.isoformat(timespec="seconds")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text(path: Path, text: str) -> None:
    """统一 utf-8 + '\\n'：这个仓库在 Windows 上被 CRLF 坑过（见 .gitattributes 与记忆）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_json(path: Path, data) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")


def tail_lines(path: Path, limit: int = LOG_TAIL_LINES) -> dict:
    """取文件末尾 limit 行。返回 {text, bytes, lines_total, lines_kept, truncated}。

    日志一律 utf-8（各 handler 都显式声明了 encoding）；坏字节用 replace 收下，
    比整个文件读不出来强——它就是来给人找线索的。
    """
    size = path.stat().st_size
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    kept = lines[-limit:] if limit > 0 else lines
    return {
        "text": "\n".join(kept) + ("\n" if kept else ""),
        "bytes": size,
        "lines_total": len(lines) if size <= LOG_FULL_LINE_COUNT_MAX else None,
        "lines_kept": len(kept),
        "truncated": len(lines) > len(kept),
    }


def redact(text: str) -> str:
    """配置副本的脱敏：敏感键的值换成 `<已脱敏>`，行尾注释留着（排查要看那些说明）。

    `key_env` 这类键的值是变量名，不是密钥，原样保留。"""
    out = []
    for line in text.splitlines():
        match = re.match(r"(\s*[A-Za-z_][\w.-]*\s*=\s*)(.*)$", line)
        if match:
            name = match.group(1).split("=")[0].strip()
            if _SECRET_KEY_RE.search(name) and not name.lower().endswith(_ENV_KEEP_SUFFIX):
                rest = match.group(2)
                quoted = re.match(r'\s*(["\'])(.*?)\1(.*)$', rest, re.S)
                if quoted:
                    tail = quoted.group(3)
                else:
                    tail = rest[rest.find("#"):] if "#" in rest else ""
                line = f'{match.group(1)}"<已脱敏>"{tail}'
        out.append(line)
    return "\n".join(out) + "\n"


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 15.0) -> dict:
    """跑一条只读命令并收下结果；任何失败都记进返回值，不抛——采集要尽量全。"""
    try:
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        return {"cmd": cmd, "returncode": done.returncode,
                "stdout": done.stdout.strip(), "stderr": done.stderr.strip()}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"cmd": cmd, "returncode": None, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}


def git_info(cwd: Path, log_count: int = 10) -> dict:
    """一个 git 目录的版本事实：分支、提交、工作区脏不脏、远端、最近几条提交。"""
    info = {
        "dir": str(cwd),
        "is_repo": (cwd / ".git").exists(),
    }
    if not info["is_repo"]:
        return info
    info["branch"] = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)["stdout"]
    info["commit"] = _run(["git", "rev-parse", "HEAD"], cwd)["stdout"]
    info["commit_short"] = info["commit"][:12]
    status = _run(["git", "status", "--porcelain", "--branch"], cwd)["stdout"]
    lines = [line for line in status.splitlines() if line.strip()]
    info["status_head"] = lines[0] if lines else ""
    info["changed_files"] = lines[1:51]
    info["changed_count"] = max(0, len(lines) - 1)
    info["remotes"] = _run(["git", "remote", "-v"], cwd)["stdout"].splitlines()
    info["log"] = _run(["git", "log", f"-{log_count}", "--oneline", "--date=short"], cwd)["stdout"].splitlines()
    return info


def _ro_connect(path: Path, timeout: float = 2.0) -> sqlite3.Connection:
    """只读连接（`mode=ro`）：采集事实不许碰运行中的库——不建文件、不写 WAL。"""
    uri = "file:" + urllib.parse.quote(path.as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    return [dict(row) for row in conn.execute(sql).fetchall()]


def db_state(path: Path, queries: dict[str, tuple[str, str]] | None = None) -> dict:
    """一个 SQLite 库的状态快照：表与行数、指定查询、以及失败项的清单。"""
    state: dict = {"path": str(path), "exists": path.is_file(), "errors": []}
    if not state["exists"]:
        state["errors"].append("文件不存在")
        return state
    state["bytes"] = path.stat().st_size
    try:
        conn = _ro_connect(path)
    except sqlite3.Error as exc:
        state["errors"].append(f"打不开（只读）：{exc}")
        return state
    try:
        tables = [row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name")]
        state["tables"] = tables
        counts = {}
        for name in tables:
            quoted = name.replace('"', '""')
            try:
                counts[name] = conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
            except sqlite3.Error as exc:
                state["errors"].append(f"{name} 行数：{exc}")
        state["row_counts"] = counts
        results = {}
        for key, (note, sql) in (queries or {}).items():
            try:
                results[key] = {"note": note, "rows": _rows(conn, sql)}
            except sqlite3.Error as exc:
                results[key] = {"note": note, "error": str(exc)}
        state["queries"] = results
        try:
            state["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as exc:
            state["errors"].append(f"integrity_check：{exc}")
    finally:
        conn.close()
    return state


def collect_logs(dirs: list[tuple[str, Path]], dest: Path) -> list[dict]:
    """把各日志目录里的日志尾部收进 dest/<标签>/；返回逐文件的采集记录。"""
    collected = []
    for label, directory in dirs:
        if not directory.is_dir():
            collected.append({"label": label, "dir": str(directory), "missing": True})
            continue
        for path in sorted(directory.glob("*.log*")):
            if not path.is_file():
                continue
            try:
                info = tail_lines(path)
            except OSError as exc:
                collected.append({"label": label, "source": str(path), "error": str(exc)})
                continue
            _write_text(dest / label / path.name, info.pop("text"))
            info.update({"label": label, "source": str(path), "name": path.name})
            collected.append(info)
    return collected


def collect_files(sources: list[Path], dest: Path, limit: int, max_bytes: int) -> list[dict]:
    """把一组文件里最新的几个收进 dest；返回逐文件的采集记录（超限的记 size 不拷）。"""
    collected = []
    existing = [p for p in sources if p.is_file()]
    existing.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for index, path in enumerate(existing):
        size = path.stat().st_size
        if index < limit and size <= max_bytes:
            target = dest / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            collected.append({"source": str(path), "bytes": size, "copied": True})
        else:
            collected.append({"source": str(path), "bytes": size, "copied": False,
                              "why": "超出份数上限" if index >= limit else "超出单件大小上限"})
    return collected


def collect_config(config_path: Path, dest: Path) -> dict:
    """配置副本（脱敏）收进 dest；同时把真配置原文拿来读键——脱敏只作用于副本。"""
    info: dict = {"path": str(config_path), "exists": config_path.is_file(), "copies": []}
    if not info["exists"]:
        return info
    raw = config_path.read_text(encoding="utf-8", errors="replace")
    _write_text(dest / config_path.name, redact(raw))
    info["copies"].append({"source": str(config_path), "name": config_path.name})
    for sibling in ("analysis.toml", "shops.csv"):
        candidate = config_path.parent / sibling
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            _write_text(dest / sibling, redact(text) if sibling.endswith(".toml") else text)
            info["copies"].append({"source": str(candidate), "name": sibling})
    return info


def collect_environment() -> dict:
    """环境事实：Python、关键依赖版本、与本项目有关的环境变量**是否设置**（值不记）。"""
    env: dict = {
        "os": f"{os.name} {sys.platform}",
        "python": sys.version.replace("\n", " "),
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "env_present": {name: bool(os.environ.get(name))
                        for name in ("BESTSELLER_PYTHON", "BESTSELLER_PROJECT",
                                     "BESTSELLER_NO_DIALOG", "DEEPSEEK_API_KEY", "VISION_API_KEY")},
        "packages": {},
    }
    try:
        from importlib import metadata
        for name in ("playwright", "pywebview", "pillow", "lxml", "cssselect", "pyinstaller"):
            try:
                env["packages"][name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                env["packages"][name] = None
    except Exception as exc:  # noqa: BLE001 - 采集尽力而为
        env["packages_error"] = f"{type(exc).__name__}: {exc}"
    return env


def collect_processes() -> list[str]:
    """本机 python / Edge 进程清单（tasklist 只读）；拿不到就返回空表。"""
    if os.name != "nt":
        return []
    done = _run(["tasklist", "/FO", "CSV", "/NH"], timeout=10.0)
    lines = []
    for line in done["stdout"].splitlines():
        low = line.lower()
        if any(word in low for word in ('"python', '"msedge', '"py.exe', '"pythonw')):
            lines.append(line.strip())
    return lines[:PROCESS_LIST_LIMIT]


def collect_project(root: Path) -> dict:
    """项目自身的版本事实：git、三只壳的 sha256、requirements。"""
    info = {"git": git_info(root)}
    dist = root / "dist"
    shells = []
    if dist.is_dir():
        for exe in sorted(dist.glob("*.exe")):
            shells.append({"name": exe.name, "bytes": exe.stat().st_size, "sha256": sha256_of(exe)})
    info["shells"] = shells
    req = root / "requirements.txt"
    if req.is_file():
        info["requirements_md5"] = hashlib.md5(req.read_bytes()).hexdigest()
    return info


def collect_dir_sizes(dirs: dict[str, Path | None], file_limit: int = 20000) -> dict:
    """各目录的体积：文件数与总字节；顺带给出所在盘的剩余空间——「写不进去」类的问题常在这里。

    只统计元数据不读内容；超过 file_limit 个文件就停手，记 actual=False（数不完）。
    """
    sizes: dict = {}
    for label, directory in dirs.items():
        if directory is None:
            sizes[label] = {"path": "", "exists": False}
            continue
        entry: dict = {"path": str(directory), "exists": directory.is_dir()}
        if entry["exists"]:
            files = 0
            total = 0
            for path in directory.rglob("*"):
                if path.is_file():
                    files += 1
                    try:
                        total += path.stat().st_size
                    except OSError:
                        pass
                    if files >= file_limit:
                        break
            entry.update({"files": files, "bytes": total, "complete": files < file_limit})
            try:
                usage = shutil.disk_usage(directory)
                entry["disk"] = {"total": usage.total, "free": usage.free}
            except OSError as exc:
                entry["disk_error"] = str(exc)
        sizes[label] = entry
    return sizes


def collect_exchange(exchange_root: Path | None, machine_id: str, dest: Path) -> dict:
    """交换区事实：各 raw-* 库与 plan 库的 git 状态、已发布的包、最近周报、名册。"""
    info: dict = {"root": str(exchange_root) if exchange_root else "", "missing": []}
    if exchange_root is None or not exchange_root.is_dir():
        info["missing"].append("交换区根目录不在（machine.exchange_root 没配或还没 clone）")
        return info

    repos = {}
    for child in sorted(exchange_root.iterdir()):
        if child.is_dir() and (child.name.startswith("raw-") or child.name == "plan"):
            repos[child.name] = git_info(child)
    info["repos"] = repos

    packages = []
    for path in sorted(exchange_root.glob("raw-*/data/*/*.gz")) + \
            sorted(exchange_root.glob("raw-*/data/*/*.json*")):
        if path.is_file():
            packages.append({"file": str(path.relative_to(exchange_root)),
                             "bytes": path.stat().st_size})
    info["packages"] = packages[-40:]

    reports = sorted((exchange_root / "报告").glob("*.md")) if (exchange_root / "报告").is_dir() else []
    info["reports"] = [{"name": path.name, "mtime": datetime.datetime.fromtimestamp(
        path.stat().st_mtime).isoformat(timespec="seconds")} for path in reports]
    recent = sorted(reports, key=lambda p: p.stat().st_mtime, reverse=True)[:2]
    for path in recent:
        try:
            _write_text(dest / "reports" / path.name,
                        path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            info["missing"].append(f"周报 {path.name} 读不出来：{exc}")

    roster = exchange_root / "plan" / "machines.json"
    if roster.is_file():
        try:
            info["roster"] = json.loads(roster.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            info["roster_error"] = str(exc)
    plan_dir = exchange_root / "plan" / "plan"
    plan_files = sorted((path for path in plan_dir.glob("*") if path.is_file()),
                        key=lambda p: p.stat().st_mtime, reverse=True) if plan_dir.is_dir() else []
    info["plan_files"] = [path.name for path in plan_files[:8]]
    for path in plan_files[:2]:                       # 最新一两份计划原文（周计划问题的输入证据）
        try:
            _write_text(dest / "plan" / path.name, path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            info["missing"].append(f"计划文件 {path.name} 读不出来：{exc}")
    return info


def newest_round_dir(raw_page_dir: Path | None) -> Path | None:
    if raw_page_dir is None or not raw_page_dir.is_dir():
        return None
    rounds = [d for d in raw_page_dir.glob("round_*") if d.is_dir()]
    if not rounds:
        return raw_page_dir
    return max(rounds, key=lambda d: d.stat().st_mtime)


def create_bundle(title: str, config_path: Path, out_root: Path | None = None,
                  root: Path | None = None, now=None) -> Path:
    """新建一份记录目录；返回它的路径。采集失败只记进 facts.json，不中断。

    `root` 是项目根（缺省本文件推出来的那个）；用例拿它把整份采集指到临时目录上。
    """
    root = ROOT if root is None else Path(root)
    local_iso, cst_iso = now_stamps() if now is None else now
    facts: dict = {
        "title": title,
        "collected_at_local": local_iso,
        "collected_at_cst": cst_iso,
        "project_root": str(root),
        "missing": [],
        "warnings": [],
    }

    # —— 配置：能走 Config 就走（它认得全、也校验机器身份），走不通退回原始 toml 取路径。
    raw_toml: dict = {}
    try:
        if config_path.is_file():
            with config_path.open("rb") as handle:
                raw_toml = tomllib.load(handle)
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"读 config.toml 原文失败：{type(exc).__name__}: {exc}")

    cfg = None
    cfg_error = ""
    try:
        from bestseller_monitor.config import Config
        cfg = Config.from_file(config_path, root=root)
    except Exception as exc:  # noqa: BLE001 - 配置坏了正是要记录的事
        cfg_error = f"{type(exc).__name__}: {exc}"

    machine = raw_toml.get("machine") if isinstance(raw_toml.get("machine"), dict) else {}
    paths = raw_toml.get("paths") if isinstance(raw_toml.get("paths"), dict) else {}
    machine_id = (getattr(cfg, "machine_id", "") or machine.get("machine_id") or "unknown")
    role = getattr(cfg, "role", "") or machine.get("role") or ""

    def _resolve(raw) -> Path:
        path = Path(str(raw))
        return path if path.is_absolute() else (root / path)

    def _path(attr: str, key: str, default: Path | None) -> Path | None:
        value = getattr(cfg, attr, None)
        if value:
            return Path(value)
        raw = paths.get(key)
        return _resolve(raw) if raw else default

    data_dir = _path("data_dir", "data_dir", None)
    logs_dirs = []
    if getattr(cfg, "logs_dir", None):
        logs_dirs.append(("runtime", Path(cfg.logs_dir)))
    elif paths.get("logs_dir"):
        logs_dirs.append(("runtime", _resolve(paths["logs_dir"])))
    else:
        facts["warnings"].append("配置里没有 logs_dir：只收项目根 logs/ 那一份")
    if (root / "logs").resolve() not in {d.resolve() for _, d in logs_dirs}:
        logs_dirs.append(("repo", root / "logs"))

    exchange_root = None
    if getattr(cfg, "exchange_root", None):
        exchange_root = Path(cfg.exchange_root)
    elif machine.get("exchange_root"):
        exchange_root = _resolve(machine["exchange_root"])

    # —— 记录目录：机器编号已知后再定名（标题里用户自己也会写机器，重了无妨）。
    stamp = datetime.datetime.fromisoformat(cst_iso).strftime("%Y-%m-%d-%H%M")
    name = f"{stamp}-{machine_id}-{slugify(title)}"
    out_root = out_root or (root / ISSUES_SUBDIR)
    bundle_dir = out_root / name
    suffix = 2
    while bundle_dir.exists():
        bundle_dir = out_root / f"{name}-{suffix}"
        suffix += 1
    bundle_dir.mkdir(parents=True)

    facts.update({
        "bundle": bundle_dir.name,
        "machine_id": str(machine_id),
        "role": str(role),
        "config": {"path": str(config_path), "exists": config_path.is_file(),
                   "loaded": cfg is not None, "error": cfg_error},
        "log_dirs": [{"label": label, "dir": str(directory)} for label, directory in logs_dirs],
    })

    # —— 逐样采集。每样一个 try：任何一样塌了，记录照发，塌的那条进 warnings。
    try:
        facts["config_copies"] = collect_config(config_path, bundle_dir / "config")
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"配置副本采集失败：{type(exc).__name__}: {exc}")
    try:
        facts["logs"] = collect_logs(logs_dirs, bundle_dir / "logs")
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"日志采集失败：{type(exc).__name__}: {exc}")
    try:
        facts["environment"] = collect_environment()
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"环境采集失败：{type(exc).__name__}: {exc}")
    try:
        facts["processes"] = collect_processes()
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"进程清单采集失败：{type(exc).__name__}: {exc}")
    try:
        facts["project"] = collect_project(root)
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"项目版本采集失败：{type(exc).__name__}: {exc}")

    db_file = _path("db_file", "db_file", data_dir / "bestseller.db" if data_dir else None)
    db_states = {}
    if db_file is not None:
        try:
            db_states["库存库"] = db_state(db_file, INVENTORY_QUERIES)
        except Exception as exc:  # noqa: BLE001
            facts["warnings"].append(f"库存库状态采集失败：{type(exc).__name__}: {exc}")
    else:
        facts["warnings"].append("配置里没有 db_file：库存库状态没采")
    analysis_cfg = config_path.parent / "analysis.toml"
    analysis_raw = {}
    try:
        if analysis_cfg.is_file():
            with analysis_cfg.open("rb") as handle:
                analysis_raw = tomllib.load(handle)
    except Exception:  # noqa: BLE001 - 分析配置坏了也照收其余事实
        pass
    analysis_section = analysis_raw.get("analysis") if isinstance(analysis_raw.get("analysis"), dict) else {}
    matching_section = analysis_raw.get("matching") if isinstance(analysis_raw.get("matching"), dict) else {}
    for label, value in (("分析草稿库", analysis_section.get("store")),
                         ("匹配缓存库", matching_section.get("cache"))):
        if not value:
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = (analysis_cfg.parent / candidate)
        try:
            db_states[label] = db_state(candidate)
        except Exception as exc:  # noqa: BLE001
            facts["warnings"].append(f"{label}状态采集失败：{type(exc).__name__}: {exc}")
    if db_states:
        _write_json(bundle_dir / "state" / "db-state.json", db_states)
    facts["db_state_file"] = "state/db-state.json" if db_states else ""

    try:
        exchange_state = collect_exchange(exchange_root, str(machine_id), bundle_dir / "state")
        _write_json(bundle_dir / "state" / "exchange-state.json", exchange_state)
        facts["exchange"] = {key: value for key, value in exchange_state.items()
                             if key not in ("repos",)}
        facts["exchange_state_file"] = "state/exchange-state.json"
        facts["missing"].extend(exchange_state.get("missing", []))
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"交换区状态采集失败：{type(exc).__name__}: {exc}")

    screenshot_dir = _path("screenshot_dir", "screenshot_dir", None)
    try:
        facts["dir_sizes"] = collect_dir_sizes({
            "data_dir": data_dir,
            "logs_dir": logs_dirs[0][1] if logs_dirs else None,
            "raw_page_dir": _path("raw_page_dir", "raw_page_dir", None),
            "screenshot_dir": screenshot_dir,
            "exchange_root": exchange_root,
        })
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"目录体积采集失败：{type(exc).__name__}: {exc}")
    try:
        shots = [p for p in screenshot_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")] \
            if screenshot_dir and screenshot_dir.is_dir() else []
        facts["screenshots"] = collect_files(shots, bundle_dir / "screenshots",
                                             MAX_SCREENSHOTS, MAX_SCREENSHOT_BYTES)
        if not facts["screenshots"]:
            facts["missing"].append("没有可收的截图（截图目录为空或没配）")
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"截图采集失败：{type(exc).__name__}: {exc}")

    try:
        raw_dir = _path("raw_page_dir", "raw_page_dir", None)
        round_dir = newest_round_dir(raw_dir)
        pages = [p for p in round_dir.glob("*.html")] if round_dir else []
        facts["raw_pages"] = collect_files(pages, bundle_dir / "raw-pages",
                                           MAX_RAW_PAGES, MAX_RAW_PAGE_BYTES)
        if round_dir:
            facts["raw_pages_round"] = str(round_dir)
        if not facts["raw_pages"]:
            facts["missing"].append("没有可收的原始页面（没解析失败过、或页面目录没配）")
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"原始页面采集失败：{type(exc).__name__}: {exc}")

    try:
        diag_dir = data_dir / "diag" if data_dir else None
        diag_files = [p for p in diag_dir.iterdir() if p.is_file()] if diag_dir and diag_dir.is_dir() else []
        facts["diag_files"] = collect_files(diag_files, bundle_dir / "diag",
                                            MAX_DIAG_FILES, MAX_SCREENSHOT_BYTES)
    except Exception as exc:  # noqa: BLE001
        facts["warnings"].append(f"diag 目录采集失败：{type(exc).__name__}: {exc}")

    _write_json(bundle_dir / "facts.json", facts)
    _write_text(bundle_dir / REPORT_NAME, render_report(facts))
    return bundle_dir


def render_report(facts: dict) -> str:
    """REPORT.md：上半段给人填、下半段是自动事实的摘要。"""
    git = facts.get("project", {}).get("git", {})
    commit = git.get("commit_short") or "（取不到）"
    branch = git.get("branch") or "（取不到）"
    changed = git.get("changed_count", 0)
    dirty = f"有 {changed} 处未提交改动" if changed else "干净"
    copies = [item["name"] for item in facts.get("config_copies", {}).get("copies", [])]
    lines = [
        f"# {facts.get('title', '未命名')}",
        "",
        "> 运行问题记录。第一节由人填写（必填），第二节是工具自动采集的事实。",
        "> 规范、读法与怎么交出去：docs/ops/问题记录与打包.md。",
        "",
        f"- 记录编号：{facts.get('bundle', '')}",
        f"- 机器编号：{facts.get('machine_id', '')}（角色：{facts.get('role') or '未写'}）",
        f"- 采集时间：{facts.get('collected_at_cst', '')}（北京时间）/ "
        f"{facts.get('collected_at_local', '')}（本机时间）",
        f"- 程序版本：{commit}（分支 {branch}；工作区{dirty}）",
        f"- 自动采集的完整事实：facts.json；库状态：{facts.get('db_state_file') or '（没采到）'}；"
        f"交换区状态：{facts.get('exchange_state_file') or '（没采到）'}",
        "",
        "## 一、问题描述（人填写）",
        "",
        f"**1. 现象**：{PLACEHOLDER}",
        "（看到了什么？弹窗、终端或界面上的原文请原样抄一句。）",
        "",
        f"**2. 期望**：{PLACEHOLDER}",
        "（本来应该怎样。）",
        "",
        f"**3. 复现步骤**：{PLACEHOLDER}",
        "（从打开哪个入口开始，按了哪些按钮、敲了哪条命令。）",
        "",
        f"**4. 发生时间**：{PLACEHOLDER}",
        "（大概几点；是第几次运行。）",
        "",
        f"**5. 当时在做什么**：{PLACEHOLDER}",
        "（例：刚点「导出&汇总」；采集跑到第 3 家店。）",
        "",
        f"**6. 最近改过什么**：{PLACEHOLDER}",
        "（配置、代码、账号、网络；没改就写「没改」。）",
        "",
        f"**7. 还能复现吗**：{PLACEHOLDER}",
        "（每次都出 / 偶发 / 只出过一次；现在程序还在跑吗。）",
        "",
        "## 二、自动采集的事实（工具填的，不用改）",
        "",
        f"- 配置副本（已脱敏）：{'、'.join(copies) if copies else '（没采到 config.toml）'}",
        f"- 日志：{_log_summary(facts.get('logs', []))}",
        f"- 截图：{_count_summary(facts.get('screenshots', []))}；"
        f"原始页面：{_count_summary(facts.get('raw_pages', []))}",
        f"- 体量与空间：{_size_summary(facts)}",
        f"- 没采到的部分：{'；'.join(facts.get('missing', [])) or '无'}",
        f"- 采集时的异常：{'；'.join(facts.get('warnings', [])) or '无'}",
        "",
        "采集到的文件与逐项说明见 facts.json；数据库与交换区的明细在 state/ 下。",
        "",
    ]
    return "\n".join(lines)


def _log_summary(entries: list[dict]) -> str:
    parts = []
    for entry in entries:
        if entry.get("missing"):
            parts.append(f"{entry['label']} 目录不在")
        elif entry.get("error"):
            parts.append(f"{entry.get('name', entry.get('source'))} 读不出来")
        else:
            tail_note = "（截尾）" if entry.get("truncated") else ""
            parts.append(f"{entry['label']}/{entry['name']} {entry.get('lines_kept')} 行{tail_note}")
    return "；".join(parts) or "（没有日志文件）"


def _count_summary(entries: list[dict]) -> str:
    copied = [entry for entry in entries if entry.get("copied")]
    if not copied:
        return "没采到"
    return f"{len(copied)} 个（{'、'.join(Path(e['source']).name for e in copied)}）"


def _human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _size_summary(facts: dict) -> str:
    """体量与磁盘剩余——「写不进去」类的问题先看这一行。"""
    sizes = facts.get("dir_sizes") or {}
    parts = []
    for label in ("data_dir", "logs_dir", "raw_page_dir", "screenshot_dir", "exchange_root"):
        entry = sizes.get(label) or {}
        if not entry.get("exists"):
            continue
        note = "" if entry.get("complete", True) else "（数不完：文件太多）"
        parts.append(f"{label} {_human_bytes(entry.get('bytes', 0))}{note}")
    disk = next((entry.get("disk") for entry in sizes.values() if entry.get("disk")), None)
    if disk:
        parts.append(f"所在盘剩余 {_human_bytes(disk['free'])} / {_human_bytes(disk['total'])}")
    return "；".join(parts) or "（没采到）"


def unfilled_items(report_text: str) -> list[str]:
    """REPORT.md 里还没填的必填项（拿 `**N. 标题**：（待填写）` 判）。"""
    return [match.group(1).strip() for match in
            re.finditer(rf"\*\*(.+?)\*\*：{re.escape(PLACEHOLDER)}", report_text)]


def pack_bundle(bundle_dir: Path) -> tuple[Path, dict]:
    """算出 MANIFEST.json 并打成同名 .zip；返回 (zip 路径, 清单)。"""
    manifest_entries = []
    for path in sorted(bundle_dir.rglob("*")):
        if path.is_file() and path.name != MANIFEST_NAME:
            manifest_entries.append({
                "path": path.relative_to(bundle_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_of(path),
            })
    manifest = {"bundle": bundle_dir.name, "packed_at": now_stamps()[1],
                "files": manifest_entries}
    _write_json(bundle_dir / MANIFEST_NAME, manifest)

    zip_path = bundle_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=f"{bundle_dir.name}/{path.relative_to(bundle_dir).as_posix()}")
    return zip_path, manifest


def unpack_bundle(zip_path: Path, into: Path) -> tuple[Path, str]:
    """把收到的记录包解到 `<into>/<包名>/`，返回 (包目录, 给接收方看的摘要)。

    摘要 = REPORT.md 的「一、问题描述」那一段 + facts.json 的 missing/warnings——
    接收的人（或 agent）先看这段就能决定往哪深挖。
    """
    with zipfile.ZipFile(zip_path) as archive:
        infos = [info for info in archive.infolist() if info.filename.strip("/")]
        roots = {info.filename.split("/")[0] for info in infos}
        if len(roots) != 1:
            raise ValueError(f"{zip_path.name} 不是一份记录包（顶层不是唯一目录）：{sorted(roots)[:3]}")
        bundle_name = roots.pop()
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            if Path(name).is_absolute() or re.match(r"^[A-Za-z]:", name) or ".." in Path(name).parts:
                raise ValueError(f"包里有不安全的路径，拒绝解包：{name}")
            target = into / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as dest_file:
                shutil.copyfileobj(source, dest_file)
    bundle_dir = into / bundle_name

    parts = [f"包：{bundle_dir}"]
    report = bundle_dir / REPORT_NAME
    if report.is_file():
        text = report.read_text(encoding="utf-8", errors="replace")
        section = re.search(r"## 一、问题描述.*?(?=\n## |\Z)", text, re.S)
        if section:
            body = "\n".join(section.group(0).splitlines()[:40])
            parts.append("—— REPORT.md 的「问题描述」——\n" + body)
        unfilled = unfilled_items(text)
        if unfilled:
            parts.append(f"注意：这份 REPORT.md 还有没填的项：{'、'.join(unfilled)}")
    else:
        parts.append("注意：包里没有 REPORT.md（人的描述缺失）。")
    facts_path = bundle_dir / "facts.json"
    if facts_path.is_file():
        try:
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            if facts.get("missing"):
                parts.append(f"没采到的部分：{'；'.join(facts['missing'])}")
            if facts.get("warnings"):
                parts.append(f"采集时的异常：{'；'.join(facts['warnings'])}")
            parts.append(f"机器 {facts.get('machine_id', '?')}（{facts.get('role') or '角色未写'}），"
                         f"采集于 {facts.get('collected_at_cst', '?')}")
        except (OSError, json.JSONDecodeError) as exc:
            parts.append(f"facts.json 读不出来：{exc}")
    parts.append("深挖顺序：facts.json → logs/ → state/ → 证据（screenshots/、raw-pages/、diag/）")
    return bundle_dir, "\n".join(parts)


def list_bundles(out_root: Path | None = None) -> list[dict]:
    """列出已有记录：名字、是否填过 REPORT.md、是否打过包。"""
    root = out_root or (ROOT / ISSUES_SUBDIR)
    entries = []
    if not root.is_dir():
        return entries
    for directory in sorted([d for d in root.iterdir() if d.is_dir()]):
        report = directory / REPORT_NAME
        filled = report.is_file() and not unfilled_items(report.read_text(encoding="utf-8", errors="replace"))
        zip_path = directory.with_suffix(".zip")
        entries.append({
            "dir": str(directory),
            "name": directory.name,
            "report_filled": filled,
            "zip": str(zip_path) if zip_path.is_file() else "",
        })
    return entries


def _newest_bundle(out_root: Path | None = None) -> Path | None:
    root = out_root or (ROOT / ISSUES_SUBDIR)
    dirs = [d for d in root.iterdir() if d.is_dir()] if root.is_dir() else []
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def _force_utf8_output() -> None:
    """管道/重定向时把输出对齐到 UTF-8：重定向的 stdout 默认走本机 ANSI 代码页（GBK），
    中文会乱码——别人（或 agent）捕获这条命令的输出就什么都读不出来。控制台上本就是
    UTF-8，这一步等于没动。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass


def interactive(root: Path | None = None, out_root: Path | None = None) -> int:
    """双击流程：问一句点题 → 采集 → 打开 REPORT.md → 等人填完 → 打包。

    字面都留在 Python 这边：.cmd 是 ASCII-only 的（cmd.exe 按 OEM 代码页读它，中文会烂），
    所以中文提示一律由这里打印。`BESTSELLER_NO_DIALOG=1` 只跳过开记事本那一步（自动化用）。
    `root` / `out_root` 给用例指到临时目录；缺省就是本机的项目根与标准落点。
    """
    root = ROOT if root is None else Path(root)
    tail = ["--out", str(out_root)] if out_root is not None else []
    print("记录一个运行问题：自动收集机器事实、日志、库状态与截图，收成一份可以整个拷走的记录包。")
    print("（直接回车 = 不新建，打包最新一份；Ctrl+C 退出）")
    try:
        title = input("一句话点题（例：交换台报错：raw-m1 push 被拒）: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return 1
    if not title:
        return main(["--pack", *tail])

    bundle_dir = create_bundle(title, root / "config" / "config.toml", out_root, root=root)
    print(f"\nOK：记录目录 {bundle_dir}")
    report = bundle_dir / REPORT_NAME
    if os.name == "nt" and os.environ.get("BESTSELLER_NO_DIALOG", "").strip() != "1":
        try:
            subprocess.Popen(["notepad", str(report)])
            print("已打开 REPORT.md（记事本）：把「一、问题描述」七个空填上，存盘。")
        except OSError as exc:
            print(f"（没打开记事本：{exc}）请自己打开：{report}")
    else:
        print(f"请打开并填写：{report}")
    try:
        input("\n填好存盘后按回车打包 ...")
    except (EOFError, KeyboardInterrupt):
        print("\n没打包。之后想打就再跑一次：python tools/issue_bundle.py --pack")
        return 1
    return main(["--pack", str(bundle_dir), *tail])


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        description="运行问题记录与打包：采集机器事实 + 日志 + 库状态，收成可拷走的记录包。")
    parser.add_argument("--title", default="", help="一句话点题（进目录名与 REPORT.md 标题）")
    parser.add_argument("--prompt", action="store_true",
                        help="双击流程：问点题 → 采集 → 开 REPORT.md → 填完回车打包（.cmd 用）")
    parser.add_argument("--pack", nargs="?", const="", default=None, metavar="记录目录",
                        help="打包；不给目录就打最新一份（填好 REPORT.md 后跑这条）")
    parser.add_argument("--list", action="store_true", help="列出本机已有的记录与打包情况")
    parser.add_argument("--newest", action="store_true",
                        help="只打印最新一份记录的目录（给 .cmd 双击流程取路径用）")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "config.toml",
                        help="采集配置路径（缺省 config/config.toml）")
    parser.add_argument("--out", type=Path, default=None,
                        help="记录落点（缺省 .scratch/tool-output/issues/）")
    parser.add_argument("--unpack", type=Path, default=None, metavar="记录包.zip",
                        help="把收到的记录包解到 .scratch/inbox/ 并打印摘要")
    parser.add_argument("--into", type=Path, default=None,
                        help="--unpack 的落点（缺省 .scratch/inbox/）")
    args = parser.parse_args(argv)

    if args.unpack is not None:
        into = args.into or (ROOT / INBOX_SUBDIR)
        try:
            bundle_dir, summary = unpack_bundle(args.unpack, into)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise SystemExit(f"解包失败：{exc}")
        print(summary)
        return 0

    if args.prompt:
        return interactive(out_root=args.out)

    if args.list:
        entries = list_bundles(args.out)
        if not entries:
            print("还没有记录。新建一份：python tools/issue_bundle.py --title \"一句话点题\"")
            return 0
        for entry in entries:
            state = f"REPORT.md {'已填' if entry['report_filled'] else '待填'}"
            zip_note = f"；已打包 {Path(entry['zip']).name}" if entry["zip"] else "；还没打包"
            print(f"{entry['name']}  {state}{zip_note}")
        return 0

    if args.newest:
        newest = _newest_bundle(args.out)
        if newest is None:
            print("还没有记录。", file=sys.stderr)
            return 1
        print(newest)
        return 0

    if args.pack is not None:
        bundle_dir = Path(args.pack) if args.pack else _newest_bundle(args.out)
        if bundle_dir is None or not bundle_dir.is_dir():
            raise SystemExit("没有可打包的记录：先跑一次 python tools/issue_bundle.py --title \"…\"")
        report = bundle_dir / REPORT_NAME
        if report.is_file():
            missing = unfilled_items(report.read_text(encoding="utf-8", errors="replace"))
            if missing:
                print(f"注意：REPORT.md 里还有没填的项：{'、'.join(missing)}（照打包，内容自己看）")
        else:
            print(f"注意：{bundle_dir} 里没有 {REPORT_NAME}——包里将没有人的描述。")
        zip_path, manifest = pack_bundle(bundle_dir)
        print(f"OK：{zip_path}（{len(manifest['files'])} 个文件，"
              f"{zip_path.stat().st_size // 1024} KB）")
        print(f"把这个 zip 拷走发出去即可；目录本身留在 {bundle_dir}")
        return 0

    if not args.title:
        raise SystemExit("要一句标题：python tools/issue_bundle.py --title \"交换台报错：…\"")
    bundle_dir = create_bundle(args.title, args.config, args.out)
    print(f"OK：记录目录 {bundle_dir}")
    print("下一步：打开 REPORT.md，把「一、问题描述」七个空填上；")
    print("然后跑下面这条打成 zip（Windows 双击 tools\\issue_bundle.cmd 走同样两步）：")
    print(f'  python tools/issue_bundle.py --pack "{bundle_dir}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
