from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor.matching import MatchingConfig, ModelConfig, digest, signature_parts
from bestseller_monitor.sku_lifecycle import cache_signature, ensure_tables
from bestseller_monitor.sku_matching import evidence_version
from tools import replay_sku_baseline


class ReplaySkuBaselineTests(unittest.TestCase):
    def _pack(self, root: Path, sets: list[dict], members: list[dict]) -> Path:
        pack = root / "pack"
        pack.mkdir()
        set_fields = ["set_id", "stratum", "target_size", "candidate_source", "source_key", "candidate_size",
                      "shop_count", "offer_count", "sku_image_count", "merchant_code_value_count",
                      "merchant_code_values", "current_program_group_status", "current_program_group_id",
                      "current_program_group_basis", "program_context_required", "candidate_is_truth", "notes"]
        member_fields = ["set_id", "member_order", "member_key", "shop_key", "offer_id", "sku_id", "shop_name",
                         "product_name", "sku_name", "merchant_code", "merchant_code_values", "merchant_code_source",
                         "merchant_code_ambiguous", "image_hash", "image_date", "image_source", "image_error",
                         "evidence_version",
                         "product_image_hash", "product_image_source", "latest_inventory_date",
                         "current_program_group_id", "current_program_group_basis", "current_program_group_status",
                         "program_displayed_to_annotator"]
        offer_counts = {}
        for member in members:
            key = (member["shop_key"], member["offer_id"])
            offer_counts[key] = offer_counts.get(key, 0) + 1
        for member in members:
            if offer_counts[(member["shop_key"], member["offer_id"])] == 1:
                normalized = replay_sku_baseline._normalise_member(member)
                normalized["matching_input"]["sku_name"] = ""
                member["evidence_version"] = evidence_version(normalized)
        for name, fields, rows in (("sets.csv", set_fields, sets), ("members.csv", member_fields, members)):
            with (pack / name).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
        return pack

    def _set(self, set_id: str, stratum: str, size: int, source: str = "same_offer") -> dict:
        return {
            "set_id": set_id, "stratum": stratum, "target_size": str(size), "candidate_source": source,
            "source_key": f"offer:A:{set_id}", "candidate_size": str(size), "shop_count": "1",
            "offer_count": "1", "sku_image_count": str(size), "merchant_code_value_count": "0",
            "merchant_code_values": "", "current_program_group_status": "待回放填充",
            "current_program_group_id": "", "current_program_group_basis": "", "program_context_required": "yes",
            "candidate_is_truth": "no", "notes": "candidate source is not truth",
        }

    def _member(self, set_id: str, order: int, offer: str, sku: str, image: str = "image") -> dict:
        member = {
            "set_id": set_id, "member_order": str(order), "member_key": f"A01|{offer}|{sku}", "shop_key": "A01",
            "offer_id": offer, "sku_id": sku, "shop_name": "shop", "product_name": "product",
            "sku_name": f"sku-{sku}", "merchant_code": "", "merchant_code_values": "",
            "merchant_code_source": "product_name", "merchant_code_ambiguous": "no", "image_hash": image,
            "image_date": "2026-10-12", "image_source": "sample", "image_error": "",
            "product_image_hash": image, "product_image_source": "sample", "latest_inventory_date": "2026-10-12",
            "current_program_group_id": "", "current_program_group_basis": "", "current_program_group_status": "待回放填充",
            "program_displayed_to_annotator": "yes",
        }
        member["evidence_version"] = evidence_version(replay_sku_baseline._normalise_member(member))
        return member

    def _cache(self, root: Path, *, judgment_rows=(), recommendation_rows=(), product_rows=False) -> Path:
        path = root / "matching.sqlite"
        conn = sqlite3.connect(path)
        ensure_tables(conn, "test-machine")
        conn.execute("CREATE TABLE sku_imports (digest TEXT PRIMARY KEY, source_machine TEXT, package_rel TEXT, imported_at TEXT, rows_total INTEGER, rows_added INTEGER, rows_detail TEXT)")
        conn.execute("CREATE TABLE judgments (pair TEXT, signature TEXT, machine_id TEXT, result TEXT)")
        conn.execute("CREATE TABLE recommendations (id INTEGER PRIMARY KEY, payload TEXT)")
        if product_rows:
            conn.execute("INSERT INTO judgments VALUES ('product', 'sig', 'm', '{}')")
            conn.execute("INSERT INTO recommendations VALUES (1, '{}')")
        for pair, sig, result in judgment_rows:
            conn.execute("INSERT INTO sku_judgments(pair, signature, result) VALUES (?, ?, ?)", (pair, sig, json.dumps(result)))
        for context, sig, result in recommendation_rows:
            conn.execute("INSERT INTO sku_recommendations(context, signature, result) VALUES (?, ?, ?)", (context, sig, json.dumps(result)))
        conn.commit()
        conn.close()
        return path

    def _config(self, root: Path, cache: Path) -> Path:
        path = root / "analysis.toml"
        database = (root / "db.sqlite").as_posix()
        cache_path = cache.as_posix()
        path.write_text(
            "[analysis]\n"
            f'database = "{database}"\n\n'
            "[matching]\n"
            f'cache = "{cache_path}"\n'
            'mode = "direct"\n\n'
            "[matching.model]\n"
            'endpoint = "https://api.example.test/v1/chat/completions"\n'
            'model = "test-model"\n',
            encoding="utf-8",
        )
        return path

    def test_empty_sku_cache_marks_all_members_without_falling_back_to_product_cache(self):
        with tempfile.TemporaryDirectory(dir=Path(".scratch")) as tmp:
            root = Path(tmp)
            sets = [self._set("SET-001", "1-10", 1), self._set("SET-002", "26-50", 2, "same_image_hash")]
            members = [self._member("SET-001", 1, "O1", "default"), self._member("SET-002", 1, "O1", "S1"),
                       self._member("SET-002", 2, "O2", "S2")]
            pack = self._pack(root, sets, members)
            cache = self._cache(root, product_rows=True)
            before = cache.read_bytes()
            report = replay_sku_baseline.replay(pack, cache, report_path=root / "report.json")
            self.assertEqual(report["input"]["candidate_sets"], 2)
            self.assertEqual(report["input"]["members"], 3)
            self.assertTrue(all(row["status"] == "no_sku_cache" for row in report["candidate_sets"]))
            self.assertEqual(report["policy"]["model_calls"], 0)
            self.assertFalse(report["policy"]["product_cache_fallback"])
            self.assertEqual(report["candidate_sets"][0]["candidate_edges_basis"], "sample_members_pairwise")
            self.assertEqual(report["candidate_sets"][0]["execution"]["model_calls"], 0)
            self.assertEqual(report["candidate_sets"][0]["execution"]["cost"], 0)
            self.assertEqual(cache.read_bytes(), before)
            with (pack / "program_context.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(not row["current_program_group_id"] for row in rows))
            self.assertTrue(all(row["current_program_group_status"] == "no_sku_cache" for row in rows))
            self.assertTrue(all(not row["program_judgment_state"] for row in rows))

    def test_recommendation_only_cache_is_not_reported_as_judgment_cache(self):
        with tempfile.TemporaryDirectory(dir=Path(".scratch")) as tmp:
            root = Path(tmp)
            sets = [self._set("SET-001", "1-10", 1)]
            members = [self._member("SET-001", 1, "O1", "S1")]
            pack = self._pack(root, sets, members)
            cache = self._cache(root, recommendation_rows=[("context", "signature", {"group_id": None})])
            config = self._config(root, cache)
            report = replay_sku_baseline.replay(pack, cache, config_path=config, start="2026-10-01", end="2026-10-12")
            record = report["candidate_sets"][0]
            self.assertEqual(record["status"], "candidate_set_cache_miss")
            self.assertEqual(record["judgment_cache_status"], "no_sku_judgment_cache")
            self.assertEqual(record["recommendation"]["status"], "unsafe_replay_missing_snapshot")
            self.assertEqual(report["cache"]["sku_judgment_rows"], 0)
            self.assertEqual(report["cache"]["sku_recommendation_rows"], 1)

    def test_valid_sku_judgment_edge_is_replayed_but_group_is_left_unassembled(self):
        with tempfile.TemporaryDirectory(dir=Path(".scratch")) as tmp:
            root = Path(tmp)
            sets = [self._set("SET-001", "1-10", 2), self._set("SET-002", "1-10", 1)]
            members = [self._member("SET-001", 1, "O1", "S1"), self._member("SET-001", 2, "O1", "S2"),
                       self._member("SET-002", 1, "O9", "S9")]
            pack = self._pack(root, sets, members)
            left = replay_sku_baseline._normalise_member(members[0])
            right = replay_sku_baseline._normalise_member(members[1])
            start, end = "2026-10-01", "2026-10-12"
            pair = digest({"evidence": sorted((evidence_version(left), evidence_version(right))), "analysis": [start, end]})
            matching = MatchingConfig(root / "unused.sqlite", ModelConfig(endpoint="https://api.example.test/v1/chat/completions", model="test-model"), mode="direct")
            signature = cache_signature(matching, replay_sku_baseline.JUDGMENT_RULE)
            cache = self._cache(root, judgment_rows=[(pair, signature, {"same": True, "confidence": 0.93, "reason": "same"})])
            config = self._config(root, cache)
            before = cache.read_bytes()
            report = replay_sku_baseline.replay(pack, cache, report_path=root / "report.json", config_path=config,
                                                start=start, end=end)
            first, second = report["candidate_sets"]
            self.assertEqual(first["status"], "cache_edges_replayed")
            self.assertEqual(first["validated_judgment_edges"], 1)
            self.assertEqual(first["judgment_edges"][0]["state"], "completed_positive")
            self.assertEqual(second["status"], "candidate_set_cache_miss")
            self.assertEqual(first["recommendation"]["status"], "no_sku_recommendation_cache")
            self.assertFalse(first["safe_program_group_assembly"])
            with (pack / "program_context.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["program_judgment_state"], "completed_positive")
            self.assertEqual(rows[0]["program_score_or_confidence"], "0.93")
            self.assertFalse(rows[0]["current_program_group_id"])
            self.assertEqual(cache.read_bytes(), before)

    def test_generic_product_signature_does_not_hit_sku_cache(self):
        with tempfile.TemporaryDirectory(dir=Path(".scratch")) as tmp:
            root = Path(tmp)
            sets = [self._set("SET-001", "1-10", 2)]
            members = [self._member("SET-001", 1, "O1", "S1"), self._member("SET-001", 2, "O1", "S2")]
            pack = self._pack(root, sets, members)
            left = replay_sku_baseline._normalise_member(members[0])
            right = replay_sku_baseline._normalise_member(members[1])
            start, end = "2026-10-01", "2026-10-12"
            pair = digest({"evidence": sorted((evidence_version(left), evidence_version(right))), "analysis": [start, end]})
            matching = MatchingConfig(root / "unused.sqlite", ModelConfig(endpoint="https://api.example.test/v1/chat/completions", model="test-model"), mode="direct")
            product_signature = digest(signature_parts(matching))
            cache = self._cache(root, judgment_rows=[(pair, product_signature, {"same": True, "confidence": 0.93})])
            config = self._config(root, cache)
            report = replay_sku_baseline.replay(pack, cache, config_path=config, start=start, end=end)
            self.assertEqual(report["candidate_sets"][0]["status"], "candidate_set_cache_miss")
            self.assertEqual(report["candidate_sets"][0]["validated_judgment_edges"], 0)

    def test_partial_missing_evidence_keeps_set_unavailable_but_preserves_valid_edge(self):
        with tempfile.TemporaryDirectory(dir=Path(".scratch")) as tmp:
            root = Path(tmp)
            sets = [self._set("SET-001", "1-10", 3)]
            members = [self._member("SET-001", 1, "O1", "S1", image=""),
                       self._member("SET-001", 2, "O1", "S2"), self._member("SET-001", 3, "O1", "S3")]
            pack = self._pack(root, sets, members)
            left = replay_sku_baseline._normalise_member(members[1])
            right = replay_sku_baseline._normalise_member(members[2])
            start, end = "2026-10-01", "2026-10-12"
            pair = digest({"evidence": sorted((evidence_version(left), evidence_version(right))), "analysis": [start, end]})
            matching = MatchingConfig(root / "unused.sqlite", ModelConfig(endpoint="https://api.example.test/v1/chat/completions", model="test-model"), mode="direct")
            signature = cache_signature(matching, replay_sku_baseline.JUDGMENT_RULE)
            cache = self._cache(root, judgment_rows=[(pair, signature, {"same": True, "confidence": 0.93})])
            config = self._config(root, cache)
            report = replay_sku_baseline.replay(pack, cache, config_path=config, start=start, end=end)
            record = report["candidate_sets"][0]
            self.assertEqual(record["status"], "evidence_unavailable")
            self.assertEqual(record["missing_evidence_count"], 1)
            self.assertEqual(record["missing_evidence_member_keys"], ["A01|O1|S1"])
            self.assertEqual(record["validated_judgment_edges"], 1)
            with (pack / "program_context.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["current_program_group_status"], "evidence_unavailable")
            self.assertEqual(rows[0]["program_judgment_state"], "")
            self.assertEqual(rows[1]["program_judgment_state"], "completed_positive")
            self.assertEqual(rows[1]["current_program_group_status"], "evidence_unavailable")

    def test_missing_evidence_takes_priority_when_sku_cache_is_empty(self):
        with tempfile.TemporaryDirectory(dir=Path(".scratch")) as tmp:
            root = Path(tmp)
            sets = [self._set("SET-001", "1-10", 2)]
            members = [self._member("SET-001", 1, "O1", "S1", image=""),
                       self._member("SET-001", 2, "O1", "S2")]
            pack = self._pack(root, sets, members)
            cache = self._cache(root)
            config = self._config(root, cache)
            report = replay_sku_baseline.replay(
                pack, cache, config_path=config, start="2026-10-01", end="2026-10-12"
            )
            record = report["candidate_sets"][0]
            self.assertEqual(record["status"], "evidence_unavailable")
            self.assertEqual(record["missing_evidence_member_keys"], ["A01|O1|S1"])

    def test_nonempty_cache_without_production_evidence_version_is_unavailable(self):
        with tempfile.TemporaryDirectory(dir=Path(".scratch")) as tmp:
            root = Path(tmp)
            sets = [self._set("SET-001", "1-10", 2)]
            members = [self._member("SET-001", 1, "O1", "S1"), self._member("SET-001", 2, "O1", "S2")]
            for member in members:
                member.pop("evidence_version")
            pack = self._pack(root, sets, members)
            cache = self._cache(root, judgment_rows=[("some-pair", "some-signature", {"same": True, "confidence": 0.9})])
            config = self._config(root, cache)
            report = replay_sku_baseline.replay(pack, cache, config_path=config, start="2026-10-01", end="2026-10-12")
            record = report["candidate_sets"][0]
            self.assertEqual(record["status"], "evidence_unavailable")
            self.assertEqual(record["unverified_evidence_count"], 2)
            self.assertEqual(record["validated_judgment_edges"], 0)

    def test_mismatched_explicit_evidence_version_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(".scratch")) as tmp:
            root = Path(tmp)
            sets = [self._set("SET-001", "1-10", 2)]
            members = [self._member("SET-001", 1, "O1", "S1"), self._member("SET-001", 2, "O1", "S2")]
            members[0]["evidence_version"] = "forged-version"
            pack = self._pack(root, sets, members)
            cache = self._cache(root, judgment_rows=[("some-pair", "some-signature", {"same": True, "confidence": 0.9})])
            config = self._config(root, cache)
            report = replay_sku_baseline.replay(pack, cache, config_path=config, start="2026-10-01", end="2026-10-12")
            record = report["candidate_sets"][0]
            self.assertEqual(record["status"], "evidence_unavailable")
            self.assertEqual(record["unverified_evidence_count"], 1)
            self.assertEqual(record["validated_judgment_edges"], 0)

    def test_invalid_cached_result_is_not_replayed_as_non_match(self):
        with tempfile.TemporaryDirectory(dir=Path(".scratch")) as tmp:
            root = Path(tmp)
            sets = [self._set("SET-001", "1-10", 2)]
            members = [self._member("SET-001", 1, "O1", "S1"), self._member("SET-001", 2, "O1", "S2")]
            pack = self._pack(root, sets, members)
            left = replay_sku_baseline._normalise_member(members[0])
            right = replay_sku_baseline._normalise_member(members[1])
            start, end = "2026-10-01", "2026-10-12"
            pair = digest({"evidence": sorted((evidence_version(left), evidence_version(right))), "analysis": [start, end]})
            matching = MatchingConfig(root / "unused.sqlite", ModelConfig(endpoint="https://api.example.test/v1/chat/completions", model="test-model"), mode="direct")
            signature = cache_signature(matching, replay_sku_baseline.JUDGMENT_RULE)
            cache = self._cache(root, judgment_rows=[(pair, signature, {"same": "yes", "confidence": 0.93})])
            config = self._config(root, cache)
            report = replay_sku_baseline.replay(pack, cache, config_path=config, start=start, end=end)
            record = report["candidate_sets"][0]
            self.assertEqual(record["status"], "candidate_set_cache_miss")
            self.assertEqual(record["validated_judgment_edges"], 0)
            self.assertEqual(record["invalid_judgment_cache_rows"], 1)
            with (pack / "program_context.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertTrue(all(not row["program_judgment_state"] for row in rows))

    def test_missing_sample_evidence_blocks_cache_replay(self):
        with tempfile.TemporaryDirectory(dir=Path(".scratch")) as tmp:
            root = Path(tmp)
            sets = [self._set("SET-001", "1-10", 2)]
            members = [self._member("SET-001", 1, "O1", "S1", image=""), self._member("SET-001", 2, "O1", "S2")]
            pack = self._pack(root, sets, members)
            cache = self._cache(root, judgment_rows=[("some-pair", "some-signature", {"same": True, "confidence": 0.9})])
            config = self._config(root, cache)
            report = replay_sku_baseline.replay(pack, cache, config_path=config, start="2026-10-01", end="2026-10-12")
            self.assertEqual(report["candidate_sets"][0]["status"], "evidence_unavailable")
            self.assertEqual(report["candidate_sets"][0]["validated_judgment_edges"], 0)
            self.assertTrue(report["candidate_sets"][0]["missing_reason"])


if __name__ == "__main__":
    unittest.main()
