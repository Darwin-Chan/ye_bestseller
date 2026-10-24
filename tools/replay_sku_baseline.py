"""Read-only SKU program-baseline extraction and cache replay.

The label pack defines the complete input boundary. This tool only reads SKU
cache rows and never calls a model or opens the cache in write mode. Product
level caches are reported as audit facts and are never used to populate the
SKU program context.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bestseller_monitor.matching import digest
from bestseller_monitor.sku_grouping import identity
from bestseller_monitor.sku_lifecycle import cache_signature
from bestseller_monitor.sku_matching import RULE_VERSION as JUDGMENT_RULE, evidence_version
from bestseller_monitor.sku_recommendations import RULE_VERSION as RECOMMENDATION_RULE

CSV_FIELDS = (
    "set_id",
    "member_key",
    "current_program_group_id",
    "current_program_group_basis",
    "current_program_group_status",
    "program_judgment_state",
    "program_recommendation_id",
    "program_score_or_confidence",
    "program_context_note",
)
STRATA = ("1-10", "11-25", "26-50")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _code_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _code_source() -> dict:
    """Describe the source tree used when the replay report was generated."""
    try:
        head = _code_version()
        status = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=REPO,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        changed_files = [line[3:] for line in status.splitlines() if len(line) >= 4]
        source_files = {}
        for relative in (
            "tools/replay_sku_baseline.py",
            "bestseller_monitor/sku_lifecycle.py",
            "bestseller_monitor/sku_matching.py",
            "bestseller_monitor/sku_recommendations.py",
        ):
            path = REPO / relative
            if path.is_file():
                source_files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "head": head,
            "worktree_dirty": bool(changed_files),
            "changed_files": changed_files,
            "source_file_sha256": source_files,
        }
    except (OSError, subprocess.CalledProcessError):
        return {"head": _code_version(), "worktree_dirty": None, "changed_files": None}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _normalise_member(row: dict[str, str]) -> dict:
    """Map sample-pack fields to the production SKU member evidence shape."""
    merchant_values = [value for value in row.get("merchant_code_values", "").split(",") if value]
    merchant_code = row.get("merchant_code") or (merchant_values[0] if len(merchant_values) == 1 else None)
    raw_sku_name = row.get("sku_name", "")
    member = {
        "shop_key": row["shop_key"],
        "offer_id": row["offer_id"],
        "sku_id": row["sku_id"],
        "product_name": row.get("product_name", ""),
        "raw_sku_name": raw_sku_name,
        "matching_input": {"product_name": row.get("product_name", ""), "sku_name": raw_sku_name},
        "evidence_version": row.get("evidence_version", "") or None,
        "image_hash": row.get("image_hash", "") or None,
        "image_source": row.get("image_source", "") or None,
        "image_error": row.get("image_error", "") or None,
        "observed_at": row.get("image_date", "") or None,
        "observed_date": row.get("image_date", "") or None,
        "merchant_code": merchant_code,
        "merchant_codes": merchant_values,
        "code_ambiguous": row.get("merchant_code_ambiguous") == "yes",
        "single_sku": row.get("sku_id") == "default",
        "sample": dict(row),
    }
    return member


def load_label_pack(pack_dir: Path) -> tuple[list[dict], list[dict]]:
    sets_path = pack_dir / "sets.csv"
    members_path = pack_dir / "members.csv"
    sets = _read_csv(sets_path)
    rows = _read_csv(members_path)
    if not sets or not rows:
        raise ValueError("样本包缺少 sets.csv 或 members.csv 数据")
    expected_keys = {identity(_normalise_member(row)): row["member_key"] for row in rows}
    offer_counts = Counter((row["shop_key"], row["offer_id"]) for row in rows)
    if len(expected_keys) != len(rows):
        raise ValueError("members.csv 中存在重复 SKU 身份")
    by_set: dict[str, list[dict]] = defaultdict(list)
    set_ids = {row["set_id"] for row in sets}
    for row in rows:
        if row["set_id"] not in set_ids:
            raise ValueError(f"成员引用未知候选集：{row['set_id']}")
        member = _normalise_member(row)
        member["set_id"] = row["set_id"]
        member["member_key"] = row["member_key"]
        member["single_sku"] = offer_counts[(member["shop_key"], member["offer_id"])] == 1
        member["matching_input"]["sku_name"] = "" if member["single_sku"] else member["raw_sku_name"]
        if row["member_key"] != "|".join(member[key] for key in ("shop_key", "offer_id", "sku_id")):
            raise ValueError(f"member_key 与 SKU 身份不一致：{row['member_key']}")
        by_set[row["set_id"]].append(member)
    for row in sets:
        if int(row["candidate_size"]) != len(by_set[row["set_id"]]):
            raise ValueError(f"候选集规模不一致：{row['set_id']}")
    return sets, [member for row in sets for member in by_set[row["set_id"]]]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _table_count(conn: sqlite3.Connection, name: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]) if _table_exists(conn, name) else 0


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"缓存库不存在：{path}")
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    conn.execute("SELECT count(*) FROM sqlite_master")
    return conn


def _table_details(conn: sqlite3.Connection, name: str) -> dict:
    exists = _table_exists(conn, name)
    if not exists:
        return {"exists": False, "rows": 0, "columns": []}
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({name})")]
    return {"exists": True, "rows": _table_count(conn, name), "columns": columns}


def cache_summary(conn: sqlite3.Connection, path: Path, signature: str | None = None) -> dict:
    table_names = (
        "sku_judgments", "sku_recommendations", "sku_imports", "judgments", "recommendations",
        "evidence", "visual_evidence", "judgment_imports",
    )
    details = {name: _table_details(conn, name) for name in table_names}
    tables = {name: info["rows"] for name, info in details.items()}
    current_judgment_rows = 0
    current_recommendation_rows = 0
    if signature and details["sku_judgments"]["exists"]:
        current_judgment_rows = conn.execute(
            "SELECT count(*) FROM sku_judgments WHERE signature=?", (signature,)
        ).fetchone()[0]
    if signature and details["sku_recommendations"]["exists"]:
        current_recommendation_rows = conn.execute(
            "SELECT count(*) FROM sku_recommendations WHERE signature=?", (signature,)
        ).fetchone()[0]
    return {
        "path": str(path.resolve()),
        "read_only": True,
        "tables": tables,
        "table_details": details,
        "sku_judgment_rows": tables["sku_judgments"],
        "sku_recommendation_rows": tables["sku_recommendations"],
        "sku_judgment_rows_current_signature": current_judgment_rows,
        "sku_recommendation_rows_current_signature": current_recommendation_rows,
        "sku_cache_rows": tables["sku_judgments"] + tables["sku_recommendations"],
        "product_cache_rows": tables["judgments"] + tables["recommendations"],
        "sku_import_rows": tables["sku_imports"],
    }


def _config_info(config_path: Path | None) -> tuple[dict, str | None]:
    if config_path is None:
        return {"path": None, "signature": None, "version": "not-provided"}, None
    from bestseller_monitor.analysis import AnalysisConfig

    config = AnalysisConfig.from_file(config_path)
    matching = config.matching
    if matching is None or matching.mode == "disabled":
        return {"path": str(config_path.resolve()), "signature": None, "version": "matching-disabled"}, None
    # Match sku_lifecycle.ModelWork._signature(): the generic product rule
    # must be replaced by the SKU judgment rule before hashing.
    signature = cache_signature(matching, JUDGMENT_RULE)
    content_digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    return {
        "path": str(config_path.resolve()),
        "signature": signature,
        "version": content_digest,
        "matching": {
            "candidates": matching.candidates,
            "min_score": matching.min_score,
            "mode": matching.mode,
            "concurrency": matching.concurrency,
            "judgment_rule": JUDGMENT_RULE,
            "recommendation_rule": RECOMMENDATION_RULE,
        },
    }, signature



def _production_pair_key(left: dict, right: dict, start: str, end: str) -> str:
    # sku_matching.ModelWork receives `pair` as cache_key and wraps the context
    # in its own ModelWork key; the durable table stores the inner pair digest.
    return digest({
        "evidence": sorted((_member_evidence_version(left), _member_evidence_version(right))),
        "analysis": [start, end],
    })


def _evidence_available(member: dict) -> bool:
    return bool(member.get("matching_input")) and bool(member.get("image_hash")) and not member.get("image_error")


def _evidence_verifiable(member: dict) -> bool:
    """Require an explicit production digest that matches recomputed evidence."""
    supplied = member.get("evidence_version")
    return bool(supplied) and _evidence_available(member) and supplied == evidence_version(member)


def _member_evidence_version(member: dict) -> str:
    return member["evidence_version"] if member.get("evidence_version") else evidence_version(member)


def _result_state(result: dict) -> str:
    confidence = result.get("confidence")
    if not isinstance(confidence, (int, float)) or not isinstance(result.get("same"), bool):
        return "invalid_result"
    if confidence < 0.8:
        return "completed_uncertain"
    return "completed_positive" if result.get("same") else "completed_negative"


def _load_judgment_rows(conn: sqlite3.Connection, keys: Iterable[str], signature: str | None) -> dict[str, dict]:
    if not _table_exists(conn, "sku_judgments"):
        return {}
    wanted = set(keys)
    if not wanted:
        return {}
    rows = conn.execute("SELECT pair, signature, result FROM sku_judgments").fetchall()
    found = {}
    for pair, row_signature, raw in rows:
        if pair not in wanted or (signature is not None and row_signature != signature):
            continue
        try:
            result = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(result, dict):
            found[pair] = {"signature": row_signature, "result": result}
    return found


def _member_judgment_summary(member: dict, edges: list[dict]) -> tuple[str, str, str]:
    member_id = identity(member)
    own = [edge for edge in edges if member_id in (edge["left"], edge["right"])]
    if not own:
        return "", "", ""
    states = [edge["state"] for edge in own]
    if "completed_uncertain" in states:
        state = "completed_uncertain"
    elif "completed_positive" in states:
        state = "completed_positive"
    else:
        state = "completed_negative"
    confidences = [edge["result"].get("confidence") for edge in own if isinstance(edge["result"].get("confidence"), (int, float))]
    score = str(max(confidences)) if confidences else ""
    return state, score, f"已验证 {len(own)} 条 SKU 判断边；程序组仍需完整生产 snapshot 才可安全组装"


def _set_record(set_row: dict, members: list[dict], conn: sqlite3.Connection, signature: str | None,
                start: str | None, end: str | None, global_sku_judgment_rows: int,
                global_sku_recommendation_rows: int, global_sku_rows: int,
                product_cache_rows: int) -> dict:
    candidate_edges = len(members) * (len(members) - 1) // 2
    missing_evidence = [member for member in members if not _evidence_available(member)]
    unverified_evidence = [member for member in members if _evidence_available(member) and not _evidence_verifiable(member)]
    edges: list[dict] = []
    cache_hits = 0
    lookup_possible = bool(start and end and signature)
    lookup_keys: dict[str, tuple[dict, dict]] = {}
    if lookup_possible:
        ordered = sorted(members, key=identity)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                if not _evidence_verifiable(left) or not _evidence_verifiable(right):
                    continue
                key = _production_pair_key(left, right, start, end)
                lookup_keys[key] = (left, right)
    rows = _load_judgment_rows(conn, lookup_keys, signature) if lookup_possible else {}
    invalid_cache_rows = 0
    for key, payload in rows.items():
        left, right = lookup_keys[key]
        result = payload["result"]
        state = _result_state(result)
        if state == "invalid_result":
            invalid_cache_rows += 1
            continue
        cache_hits += 1
        edges.append({
            "left": identity(left),
            "right": identity(right),
            "cache_key": key,
            "signature": payload["signature"],
            "state": state,
            "result": result,
        })
    if missing_evidence:
        status = "evidence_unavailable"
        missing_keys = ", ".join(member["member_key"] for member in missing_evidence)
        reason = (
            f"候选集有 {len(missing_evidence)} 个成员缺少样本证据（{missing_keys}）；"
            "即使部分判断边命中，整体程序组也不可安全回放，未回退商品级缓存"
        )
    elif global_sku_rows == 0:
        status = "no_sku_cache"
        reason = "没有可用 SKU 判断或推荐缓存；未回退商品级缓存，程序字段保持空值"
    elif global_sku_judgment_rows == 0:
        status = "candidate_set_cache_miss"
        reason = "存在 SKU 推荐缓存但没有 SKU 判断缓存；不能形成判断边，未回退商品级缓存"
    elif unverified_evidence:
        status = "evidence_unavailable"
        unverified_keys = ", ".join(member["member_key"] for member in unverified_evidence)
        reason = (
            f"候选集有 {len(unverified_evidence)} 个成员缺少生产 evidence_version（{unverified_keys}）；"
            "无法证明样本字段可复现生产 SKU 缓存键，未回放判断边"
        )
    elif not lookup_possible:
        status = "evidence_unavailable"
        reason = "未提供完整分析窗口或当前配置署名，无法验证 SKU 缓存键；程序字段保持空值"
    elif not edges:
        status = "candidate_set_cache_miss"
        reason = "全局存在 SKU 缓存，但当前候选集没有命中；未解释为不同款"
    else:
        status = "cache_edges_replayed"
        reason = "仅回放已按生产缓存键和当前证据验证的 SKU 判断边；程序组未安全组装"
    member_records = []
    for member in members:
        member_missing = member in missing_evidence
        member_unverified = member in unverified_evidence
        if member_missing:
            state, score = "", ""
            note = "该成员缺少样本证据，不能验证 SKU 缓存键；程序字段保持空值"
        elif member_unverified and global_sku_rows:
            state, score = "", ""
            note = "该成员没有生产 evidence_version，不能验证 SKU 缓存键；程序字段保持空值"
        else:
            state, score, note = _member_judgment_summary(member, edges)
            if not note:
                note = reason
            if missing_evidence:
                note += "；同一候选集另有成员缺少证据，整体程序组不可安全组装"
        member_records.append({
            "set_id": set_row["set_id"],
            "member_key": member["member_key"],
            "program_judgment_state": state,
            "program_score_or_confidence": score,
            "program_recommendation_id": "",
            "current_program_group_id": "",
            "current_program_group_basis": "",
            "current_program_group_status": status,
            "program_context_note": note,
        })
    if global_sku_rows == 0:
        recommendation_status = "no_sku_cache"
    elif global_sku_recommendation_rows == 0:
        recommendation_status = "no_sku_recommendation_cache"
    else:
        recommendation_status = "unsafe_replay_missing_snapshot"
    cross_offer = len({(member["shop_key"], member["offer_id"]) for member in members}) > 1
    return {
        "set_id": set_row["set_id"],
        "stratum": set_row["stratum"],
        "candidate_source": set_row["candidate_source"],
        "source_key": set_row["source_key"],
        "candidate_size": int(set_row["candidate_size"]),
        "member_count": len(members),
        "offer_count": len({(member["shop_key"], member["offer_id"]) for member in members}),
        "cross_offer_constructed": bool(set_row["stratum"] == "26-50" and cross_offer),
        "status": status,
        "judgment_cache_status": "available" if global_sku_judgment_rows else "no_sku_judgment_cache",
        "missing_reason": reason,
        "missing_evidence_count": len(missing_evidence),
        "missing_evidence_member_keys": [member["member_key"] for member in missing_evidence],
        "unverified_evidence_count": len(unverified_evidence),
        "unverified_evidence_member_keys": [member["member_key"] for member in unverified_evidence],
        "candidate_edges": candidate_edges,
        "candidate_edges_basis": "sample_members_pairwise",
        "validated_judgment_edges": len(edges),
        "sku_cache_hits": cache_hits,
        "invalid_judgment_cache_rows": invalid_cache_rows,
        "program_groups": [],
        "safe_program_group_assembly": False,
        "recommendation": {
            "status": recommendation_status,
            "cache_rows_available": global_sku_recommendation_rows,
            "replayed": 0,
            "reason": "缺少完整生产 SKU snapshot、候选组语境和人工约束，未伪造推荐结果",
        },
        "members": member_records,
        "judgment_edges": edges,
        "product_cache_fallback": False,
        "product_cache_rows_observed": product_cache_rows,
        "execution": {
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_tokens": 0,
            "cost": 0,
            "elapsed_seconds": 0,
            "reason": "只读缓存回放；未执行模型调用",
        },
    }


def _write_context(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    temp_path.replace(path)


def _stratum_summary(records: list[dict]) -> dict:
    output = {}
    for stratum in STRATA:
        selected = [record for record in records if record["stratum"] == stratum]
        output[stratum] = {
            "candidate_sets": len(selected),
            "members": sum(record["member_count"] for record in selected),
            "cross_offer_constructed_sets": sum(record["cross_offer_constructed"] for record in selected),
            "statuses": dict(Counter(record["status"] for record in selected)),
            "candidate_edges": sum(record["candidate_edges"] for record in selected),
            "validated_judgment_edges": sum(record["validated_judgment_edges"] for record in selected),
            "sku_cache_hits": sum(record["sku_cache_hits"] for record in selected),
        }
    return output


def replay(pack_dir: Path, cache_path: Path, output_csv: Path | None = None,
           report_path: Path | None = None, config_path: Path | None = None,
           start: str | None = None, end: str | None = None) -> dict:
    started = time.monotonic()
    sets, members = load_label_pack(pack_dir)
    by_set: dict[str, list[dict]] = defaultdict(list)
    for member in members:
        by_set[member["set_id"]].append(member)
    config_info, signature = _config_info(config_path)
    conn = open_readonly(cache_path)
    try:
        cache = cache_summary(conn, cache_path, signature)
        global_sku_rows = cache["sku_cache_rows"]
        records = [
            _set_record(row, by_set[row["set_id"]], conn, signature, start, end,
                        cache["sku_judgment_rows"], cache["sku_recommendation_rows"],
                        global_sku_rows, cache["product_cache_rows"])
            for row in sets
        ]
    finally:
        conn.close()
    context_records = [member for record in records for member in record["members"]]
    if output_csv is None:
        output_csv = pack_dir / "program_context.csv"
    _write_context(output_csv, context_records)
    report = {
        "schema_version": 1,
        "baseline_kind": "sku_cache_program_baseline",
        "generated_at": _utc_now(),
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "code_version": _code_version(),
        "code_source": _code_source(),
        "config": config_info,
        "analysis_window": {"start": start, "end": end, "verified": bool(start and end and signature)},
        "input": {
            "pack_dir": str(pack_dir.resolve()),
            "sets_file": str((pack_dir / "sets.csv").resolve()),
            "members_file": str((pack_dir / "members.csv").resolve()),
            "candidate_boundary": "sets.csv + members.csv",
            "candidate_sets": len(sets),
            "members": len(members),
            "strata": dict(Counter(row["stratum"] for row in sets)),
        },
        "cache": cache,
        "policy": {
            "read_only": True,
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_tokens": 0,
            "cost": 0,
            "no_new_llm_calls": True,
            "product_cache_fallback": False,
            "program_group_assembly": "only-safe-production-snapshot",
            "judgment_rule": JUDGMENT_RULE,
            "recommendation_rule": RECOMMENDATION_RULE,
        },
        "candidate_sets": records,
        "stratum_summary": _stratum_summary(records),
        "outputs": {"program_context_csv": str(output_csv.resolve()), "report_json": str(report_path.resolve()) if report_path else None},
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读回放 SKU 程序基线缓存")
    parser.add_argument("--pack", type=Path, default=REPO / ".scratch/sku-grouping-research-update/label-pack")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--start", help="生产分析窗口起始日期；用于验证 SKU 判断缓存键")
    parser.add_argument("--end", help="生产分析窗口结束日期；用于验证 SKU 判断缓存键")
    parser.add_argument("--output-csv", type=Path)
    parser.add_argument("--json", dest="report_path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.start) != bool(args.end):
        print("--start 与 --end 必须同时提供", file=sys.stderr)
        return 2
    if args.report_path is None:
        args.report_path = REPO / ".scratch/tool-output/sku-baseline-replay-20261023.json"
    try:
        report = replay(args.pack, args.cache, args.output_csv, args.report_path,
                        args.config, args.start, args.end)
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError, KeyError) as exc:
        print(f"SKU 程序基线回放失败：{exc}", file=sys.stderr)
        return 1
    statuses = Counter(record["status"] for record in report["candidate_sets"])
    print(f"SKU 程序基线：{report['input']['candidate_sets']} 个候选集，{report['input']['members']} 个成员")
    print("状态：" + ", ".join(f"{key}={value}" for key, value in sorted(statuses.items())))
    print(f"成员表：{report['outputs']['program_context_csv']}")
    print(f"JSON：{report['outputs']['report_json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





