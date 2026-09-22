"""壳发布包：目录布局、清单内容与校验值（不跑 PyInstaller，用假 exe 打桩）。

真构建链（spec → exe → TOC 自检 → --check）在 test_build_exe.py 与
tools/build_exe.py 一侧；这里只钉「收包」这半步：复制的字节与清单一致、
说明里写着放置位置与来源提交、重跑会把旧文件清掉。
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools import build_exe, release_shells


def _fake_sources(tmp: Path) -> dict[str, Path]:
    """三只「exe」：内容各异，便于钉 sha256。"""
    sources = {}
    for key in ("gui", "exchange", "analysis"):
        exe = tmp / f"{key}.exe"
        exe.write_bytes(b"MZ" + key.encode("utf-8") * 8)
        sources[key] = exe
    return sources


class WriteBundleTests(unittest.TestCase):
    def test_manifest_lists_every_exe_with_matching_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = _fake_sources(root)
            out = root / "release" / "shells-2026-09-22"

            entries = release_shells.write_bundle(
                out, sources, {"git_commit": "abc123", "shells_clean": True})

            manifest = json.loads((out / release_shells.MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["git_commit"], "abc123")
            self.assertTrue(manifest["shells_clean"])
            self.assertEqual([e["exe"] for e in manifest["files"]],
                             ["analysis.exe", "exchange.exe", "gui.exe"])
            self.assertEqual(manifest["files"], entries)
            for entry in manifest["files"]:
                copied = (out / entry["exe"]).read_bytes()
                self.assertEqual(hashlib.sha256(copied).hexdigest(), entry["sha256"])
                self.assertEqual(len(copied), entry["size"])

    def test_note_tells_where_to_put_the_exes_and_where_they_came_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "release"
            release_shells.write_bundle(
                out, _fake_sources(root), {"git_commit": "deadbeef", "shells_clean": False})

            note = (out / release_shells.NOTE_NAME).read_text(encoding="utf-8")
            self.assertIn("<项目根>\\dist\\", note)
            self.assertIn("不需要重新打包", note)
            self.assertIn("deadbeef", note)
            self.assertIn("未提交改动", note)  # shells 不干净时如实说

    def test_rerunning_clears_stale_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "release"
            release_shells.write_bundle(out, _fake_sources(root), {})
            (out / "旧文件.txt").write_text("stale", encoding="utf-8")

            release_shells.write_bundle(out, _fake_sources(root), {})

            self.assertFalse((out / "旧文件.txt").exists())
            self.assertTrue((out / release_shells.MANIFEST_NAME).is_file())


class TargetsWiringTests(unittest.TestCase):
    def test_release_uses_the_same_three_targets_as_the_builder(self):
        self.assertEqual(sorted(build_exe.TARGETS), ["analysis", "exchange", "gui"])
        for target in build_exe.TARGETS.values():
            self.assertEqual(target.exe.parent.name, "dist")


if __name__ == "__main__":
    unittest.main()
