"""问题记录包：采集到的事实、脱敏、REPORT.md 模板与打包清单。

规范与读法在 docs/ops/问题记录与打包.md；这里钉 tools/issue_bundle.py 的行为——
整份采集指到临时目录（create_bundle 的 `root` 参数），不碰真机、不碰真库：
配置副本过脱敏、日志按尾截、库只读（缺表只记错）、缺项如实进 missing、
REPORT.md 的待填判定、zip 与 MANIFEST.json 逐件一致。
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools import issue_bundle

STAMP = ("2026-09-22T20:33:41+08:00", "2026-09-22T20:33:41+08:00")
SECRET = "SHOULD-BE-REDACTED"


def _example_config_text() -> str:
    return (issue_bundle.ROOT / "config" / "config.example.toml").read_text(encoding="utf-8")


def _write_config(root: Path, *, machine_id: str = "m9", with_secret: bool = True) -> Path:
    """把仓库里的示例配置当底稿：改机器编号、塞一个假密钥；相对路径落在这棵临时树下。"""
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    text = _example_config_text()
    patched = text.replace('machine_id = "m1"', f'machine_id = "{machine_id}"')
    assert patched != text, "示例配置里的 machine_id 占位改了？测试要跟着改"
    if with_secret:
        patched = patched.replace('cos_bucket = ', f'api_secret = "{SECRET}"\ncos_bucket = ', 1)
    path = config_dir / "config.toml"
    path.write_text(patched, encoding="utf-8")
    return path


def _make_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE rounds (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,
            note TEXT, detail_budget_limit INTEGER, run_date TEXT, terminal_reason TEXT);
        CREATE TABLE shops (shop_key TEXT PRIMARY KEY, shop_name TEXT);
        INSERT INTO rounds (id, started_at, run_date, terminal_reason)
            VALUES (1, '2026-09-22T09:00:00+08:00', '2026-09-22', '完成');
        INSERT INTO shops (shop_key, shop_name) VALUES ('A01', '示例店铺');
        """
    )
    conn.commit()
    conn.close()


def _fake_root(tmp: Path, *, machine_id: str = "m9", config: bool = True) -> Path:
    """一棵够采集跑完的临时项目根：配置、日志、库、截图、原始页面、交换区。"""
    root = tmp / "proj"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "gui.log").write_text("一行\n二行\n三行\n", encoding="utf-8")
    if config:
        _write_config(root, machine_id=machine_id)
        (root / "config" / "shops.csv").write_text(
            "shop_key,shop_name,shop_url,pages,active,offer_list_url\n"
            "A01,示例店铺,https://example.1688.com/,3,1,\n", encoding="utf-8")
        _make_db(root / "data" / "bestseller.db")
        shots = root / "data" / "screenshots"
        shots.mkdir(parents=True)
        (shots / "介入-1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 32)
        (shots / "介入-2.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"b" * 32)
        pages = root / "data" / "raw_pages" / "round_1"
        pages.mkdir(parents=True)
        (pages / "111.html").write_text("<html>一</html>", encoding="utf-8")
        (pages / "222.html").write_text("<html>二</html>", encoding="utf-8")
        diag = root / "data" / "diag"
        diag.mkdir()
        (diag / "fail5.json").write_text("{}\n", encoding="utf-8")
        exchange = root / "exchange"
        (exchange / "raw-m9" / "data" / "2026").mkdir(parents=True)
        (exchange / "raw-m9" / "data" / "2026" / "W39-m9.db.gz").write_bytes(b"gz")
        (exchange / "plan" / "plan").mkdir(parents=True)
        (exchange / "plan" / "machines.json").write_text('["m9"]', encoding="utf-8")
        (exchange / "报告").mkdir(parents=True)
        (exchange / "报告" / "2026-W39.md").write_text("# 周报\n缺口 2 条\n", encoding="utf-8")
    return root


def _walk_text(root: Path) -> str:
    """把一棵树里的文本文件全读出来拼一起——用来验「密钥没有进包」。"""
    chunks = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


class SlugTests(unittest.TestCase):
    def test_replaces_windows_illegal_characters(self):
        self.assertEqual(issue_bundle.slugify('交换台报错: raw-m4/push "被拒"'),
                         "交换台报错-raw-m4-push-被拒")

    def test_empty_title_falls_back(self):
        self.assertEqual(issue_bundle.slugify("   "), "未命名")

    def test_caps_the_length(self):
        self.assertEqual(len(issue_bundle.slugify("长" * 100)), 40)


class RedactTests(unittest.TestCase):
    def test_masks_secret_keys_and_keeps_key_env(self):
        text = ('api_secret = "abc123"\n'
                'key_env = "DEEPSEEK_API_KEY"\n'
                'machine_id = "m4"\n'
                'access_key = "AKIDxx"   # 行尾注释\n')
        out = issue_bundle.redact(text)
        self.assertIn('api_secret = "<已脱敏>"', out)
        self.assertIn('access_key = "<已脱敏>"', out)
        self.assertIn('key_env = "DEEPSEEK_API_KEY"', out)   # 值是变量名，不是密钥
        self.assertIn('machine_id = "m4"', out)
        self.assertIn("# 行尾注释", out)                      # 注释留着，后续排查要看


class TailTests(unittest.TestCase):
    def test_keeps_the_last_lines_and_flags_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "gui.log"
            log.write_text("".join(f"第 {i} 行\n" for i in range(1, 101)), encoding="utf-8")

            info = issue_bundle.tail_lines(log, limit=10)

            self.assertTrue(info["truncated"])
            self.assertEqual(info["lines_total"], 100)
            self.assertEqual(info["lines_kept"], 10)
            self.assertNotIn("第 90 行", info["text"])
            self.assertTrue(info["text"].startswith("第 91 行"))

    def test_small_file_comes_through_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text("只有一行\n", encoding="utf-8")
            info = issue_bundle.tail_lines(log, limit=10)
            self.assertFalse(info["truncated"])
            self.assertEqual(info["text"], "只有一行\n")


class DirSizeTests(unittest.TestCase):
    def test_counts_files_bytes_and_free_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            (data / "sub").mkdir(parents=True)
            (data / "a.db").write_bytes(b"x" * 100)
            (data / "sub" / "b.db").write_bytes(b"x" * 50)

            sizes = issue_bundle.collect_dir_sizes({"data_dir": data, "none": None})

            self.assertEqual(sizes["data_dir"]["files"], 2)
            self.assertEqual(sizes["data_dir"]["bytes"], 150)
            self.assertTrue(sizes["data_dir"]["complete"])
            self.assertIn("free", sizes["data_dir"]["disk"])
            self.assertFalse(sizes["none"]["exists"])

    def test_missing_dir_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            sizes = issue_bundle.collect_dir_sizes({"data_dir": Path(tmp) / "没有这个"})
            self.assertFalse(sizes["data_dir"]["exists"])

    def test_summary_renders_sizes_and_disk_line(self):
        facts = {"dir_sizes": {"data_dir": {"path": "x", "exists": True, "files": 1,
                                            "bytes": 2048, "complete": True,
                                            "disk": {"free": 1024 ** 3, "total": 2 * 1024 ** 3}}}}
        text = issue_bundle._size_summary(facts)
        self.assertIn("data_dir 2.0 KB", text)
        self.assertIn("所在盘剩余 1.0 GB / 2.0 GB", text)


class CollectLogsTests(unittest.TestCase):
    def test_missing_dir_is_recorded_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = issue_bundle.collect_logs(
                [("runtime", Path(tmp) / "没有这个目录")], Path(tmp) / "out")
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0]["missing"])

    def test_rotation_backups_are_collected_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            (logs / "gui.log").write_text("新\n", encoding="utf-8")
            (logs / "gui.log.1").write_text("旧\n", encoding="utf-8")

            entries = issue_bundle.collect_logs([("repo", logs)], Path(tmp) / "out")

            self.assertEqual(sorted(entry["name"] for entry in entries), ["gui.log", "gui.log.1"])
            self.assertEqual((Path(tmp) / "out" / "repo" / "gui.log.1").read_text(encoding="utf-8"), "旧\n")


class DbStateTests(unittest.TestCase):
    def test_counts_rows_and_runs_queries_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bestseller.db"
            _make_db(db)

            state = issue_bundle.db_state(db, issue_bundle.INVENTORY_QUERIES)

            self.assertTrue(state["exists"])
            self.assertEqual(state["row_counts"]["rounds"], 1)
            self.assertEqual(state["row_counts"]["shops"], 1)
            self.assertEqual(state["queries"]["最近轮次"]["rows"][0]["terminal_reason"], "完成")
            # 临时库没有别的表：缺表只记进这条查询，不塌
            self.assertIn("error", state["queries"]["本周计划"])

    def test_read_only_connection_leaves_no_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "没有这个库.db"
            state = issue_bundle.db_state(missing)
            self.assertFalse(state["exists"])
            self.assertFalse(missing.exists())   # 只读连接不许把库「顺手建出来」

    def test_integrity_check_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "b.db"
            _make_db(db)
            self.assertEqual(issue_bundle.db_state(db)["integrity_check"], "ok")


class CreateBundleTests(unittest.TestCase):
    def _create(self, tmp: Path, title="交换台报错：raw-m9 push 被拒", **kwargs):
        root = _fake_root(tmp, **kwargs)
        out_root = tmp / "issues"
        bundle = issue_bundle.create_bundle(
            title, root / "config" / "config.toml", out_root=out_root, root=root, now=STAMP)
        return root, bundle, json.loads((bundle / "facts.json").read_text(encoding="utf-8"))

    def test_names_the_bundle_after_time_machine_and_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, bundle, facts = self._create(Path(tmp))
            self.assertEqual(bundle.name, "2026-09-22-2033-m9-交换台报错：raw-m9-push-被拒")
            self.assertEqual(facts["machine_id"], "m9")
            self.assertEqual(facts["role"], "collector")

    def test_collects_logs_without_duplicating_the_shared_log_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, bundle, facts = self._create(Path(tmp))
            names = sorted(entry["name"] for entry in facts["logs"])
            self.assertEqual(names, ["gui.log"])          # 配置的 logs 与项目根 logs 是同一个目录
            self.assertEqual((bundle / "logs" / "runtime" / "gui.log").read_text(encoding="utf-8"),
                             "一行\n二行\n三行\n")

    def test_state_files_capture_db_and_exchange(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, bundle, facts = self._create(Path(tmp))
            db_state = json.loads((bundle / "state" / "db-state.json").read_text(encoding="utf-8"))
            self.assertEqual(db_state["库存库"]["row_counts"]["shops"], 1)
            exchange = json.loads((bundle / "state" / "exchange-state.json").read_text(encoding="utf-8"))
            self.assertEqual(exchange["roster"], ["m9"])
            self.assertEqual([item["name"] for item in exchange["reports"]], ["2026-W39.md"])
            self.assertTrue((bundle / "state" / "reports" / "2026-W39.md").is_file())
            self.assertEqual(facts["exchange_state_file"], "state/exchange-state.json")
            self.assertEqual(facts["dir_sizes"]["data_dir"]["files"], 6)   # 库 + 2 截图 + 2 原始页面 + 1 diag

    def test_copies_screenshots_raw_pages_and_diag(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, bundle, facts = self._create(Path(tmp))
            self.assertEqual(len([e for e in facts["screenshots"] if e["copied"]]), 2)
            self.assertEqual(len([e for e in facts["raw_pages"] if e["copied"]]), 2)
            self.assertEqual(len([e for e in facts["diag_files"] if e["copied"]]), 1)
            self.assertTrue((bundle / "screenshots" / "介入-1.png").is_file())
            self.assertTrue((bundle / "raw-pages" / "111.html").is_file())
            self.assertTrue((bundle / "diag" / "fail5.json").is_file())

    def test_config_copy_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, bundle, facts = self._create(Path(tmp))
            copied = (bundle / "config" / "config.toml").read_text(encoding="utf-8")
            self.assertIn("<已脱敏>", copied)
            self.assertNotIn(SECRET, copied)
            self.assertNotIn(SECRET, _walk_text(bundle))   # 密钥整包都不许出现
            self.assertEqual([c["name"] for c in facts["config_copies"]["copies"]],
                             ["config.toml", "shops.csv"])

    def test_report_template_lists_the_seven_must_fill_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, bundle, _ = self._create(Path(tmp))
            text = (bundle / issue_bundle.REPORT_NAME).read_text(encoding="utf-8")
            unfilled = issue_bundle.unfilled_items(text)
            self.assertEqual(len(unfilled), 7)
            self.assertIn("1. 现象", unfilled)
            self.assertIn("7. 还能复现吗", unfilled)
            self.assertIn("docs/ops/问题记录与打包.md", text)

    def test_two_bundles_in_the_same_minute_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(Path(tmp))
            out_root = Path(tmp) / "issues"
            first = issue_bundle.create_bundle("同名", root / "config" / "config.toml",
                                               out_root=out_root, root=root, now=STAMP)
            second = issue_bundle.create_bundle("同名", root / "config" / "config.toml",
                                                out_root=out_root, root=root, now=STAMP)
            self.assertNotEqual(first, second)
            self.assertTrue(second.name.endswith("-2"))

    def test_broken_or_missing_config_still_yields_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(Path(tmp), config=False)
            bundle = issue_bundle.create_bundle(
                "配置都没了", root / "config" / "config.toml",
                out_root=Path(tmp) / "issues", root=root, now=STAMP)
            facts = json.loads((bundle / "facts.json").read_text(encoding="utf-8"))
            self.assertFalse(facts["config"]["loaded"])
            self.assertFalse(facts["config"]["exists"])
            self.assertEqual(facts["machine_id"], "unknown")
            self.assertTrue(any("db_file" in note for note in facts["warnings"]))
            # 项目根那份 logs/ 照收（配置没了也还有它）
            self.assertEqual([entry["name"] for entry in facts["logs"]], ["gui.log"])
            self.assertTrue((bundle / issue_bundle.REPORT_NAME).is_file())


class PackTests(unittest.TestCase):
    def test_pack_writes_manifest_and_zip_with_matching_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(Path(tmp))
            bundle = issue_bundle.create_bundle(
                "打包自检", root / "config" / "config.toml",
                out_root=Path(tmp) / "issues", root=root, now=STAMP)

            zip_path, manifest = issue_bundle.pack_bundle(bundle)

            self.assertEqual(zip_path, issue_bundle.zip_path_for(bundle))
            self.assertTrue(zip_path.is_file())
            paths = [entry["path"] for entry in manifest["files"]]
            self.assertIn("REPORT.md", paths)
            self.assertIn("facts.json", paths)
            self.assertNotIn(issue_bundle.MANIFEST_NAME, paths)   # 清单不把自己算进去
            for entry in manifest["files"]:
                file_path = bundle / entry["path"]
                self.assertEqual(issue_bundle.sha256_of(file_path), entry["sha256"])
            with zipfile.ZipFile(zip_path) as archive:
                names = archive.namelist()
                self.assertIn(f"{bundle.name}/REPORT.md", names)
                self.assertIn(f"{bundle.name}/{issue_bundle.MANIFEST_NAME}", names)
                self.assertEqual(archive.read(f"{bundle.name}/REPORT.md"),
                                 (bundle / "REPORT.md").read_bytes())

    def test_filling_the_report_clears_the_placeholder_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(Path(tmp))
            bundle = issue_bundle.create_bundle(
                "填好了", root / "config" / "config.toml",
                out_root=Path(tmp) / "issues", root=root, now=STAMP)
            report = bundle / issue_bundle.REPORT_NAME
            report.write_text(report.read_text(encoding="utf-8").replace(
                issue_bundle.PLACEHOLDER, "已经写清楚了"), encoding="utf-8")

            self.assertEqual(issue_bundle.unfilled_items(report.read_text(encoding="utf-8")), [])

    def test_a_dotted_title_does_not_lose_its_zip_name(self):
        """点题带小数点（如「报错 v1.2」）时 zip 也要跟目录同名。

        审查抓到的窄缺陷：`Path.with_suffix` 会把最后一个小数点后面当后缀换掉——zip 成了
        `…-v1.zip`，`--list` 按同一条错规则找文件、于是还说「还没打包」。
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_root = tmp_path / "issues"
            root = _fake_root(tmp_path)
            bundle = issue_bundle.create_bundle(
                "报错 v1.2", root / "config" / "config.toml",
                out_root=out_root, root=root, now=STAMP)
            self.assertTrue(bundle.name.endswith("报错-v1.2"))

            zip_path, _ = issue_bundle.pack_bundle(bundle)

            self.assertEqual(zip_path.name, bundle.name + ".zip")
            self.assertEqual(zip_path, issue_bundle.zip_path_for(bundle))
            self.assertTrue(zip_path.is_file())
            entry = issue_bundle.list_bundles(out_root)[0]           # --list 也要认出它已打包
            self.assertEqual(entry["zip"], str(zip_path))
            # 解包落地的目录名同样不能少一截（包内顶层目录名才是权威）
            unpacked, _ = issue_bundle.unpack_bundle(zip_path, tmp_path / "inbox")
            self.assertEqual(unpacked.name, bundle.name)


class UnpackTests(unittest.TestCase):
    def _packed(self, tmp: Path) -> Path:
        root = _fake_root(tmp)
        bundle = issue_bundle.create_bundle("换台机器的问题", root / "config" / "config.toml",
                                            out_root=tmp / "issues", root=root, now=STAMP)
        return issue_bundle.pack_bundle(bundle)[0]

    @staticmethod
    def _hand_made_zip(tmp: Path, bundle_name: str, encoding: str = "gbk") -> Path:
        """别的工具打的包的样子：名字按 `encoding` 写、不打 UTF-8 标记——资源管理器
        「压缩为 zip」用 GBK，PowerShell `Compress-Archive` 用 UTF-8 却忘标记。"""
        def encode_without_the_flag(info):
            return info.filename.encode(encoding), info.flag_bits & ~0x800

        zip_path = tmp / "手压的包.zip"
        with mock.patch.object(zipfile.ZipInfo, "_encodeFilenameFlags",
                               encode_without_the_flag):
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr(f"{bundle_name}/REPORT.md",
                                 "# 手压包自检\n\n## 一、问题描述（人填写）\n\n"
                                 "**1. 现象**：手压的包解出来要能读。\n")
                archive.writestr(f"{bundle_name}/facts.json", "{}")
                archive.writestr(f"{bundle_name}/evidence/说明.txt", "证据\n")
        return zip_path

    def test_a_hand_made_zip_with_gbk_names_is_readable(self):
        """没打 UTF-8 标记的包名要按本地代码页还原。

        工具自己打的包（`--pack`）名字带 UTF-8 标记、照收；别的工具不爱标记——资源
        管理器「压缩为 zip」按 GBK 写、PowerShell `Compress-Archive` 写 UTF-8 也不
        标记——zipfile 读到这类名字会按 cp437 解成乱码。收包是人工通道，落到别的
        机器上可能就是这么来的。
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_name = "2026-09-23-1139-m3-全量测试发现两处缺陷"
            zip_path = self._hand_made_zip(tmp_path, bundle_name)

            # 先钉住「包在磁盘上确实是乱码形态」：按标记读出来不等于原名。
            with zipfile.ZipFile(zip_path) as archive:
                self.assertNotEqual(archive.namelist()[0].split("/")[0], bundle_name)

            bundle_dir, summary = issue_bundle.unpack_bundle(zip_path, tmp_path / "inbox")

            self.assertEqual(bundle_dir.name, bundle_name)
            self.assertTrue((bundle_dir / "REPORT.md").is_file())
            self.assertTrue((bundle_dir / "evidence" / "说明.txt").is_file(),
                            "包里每一条中文名都要还原，不只是顶层目录")
            self.assertIn("一、问题描述", summary)

    def test_a_hand_made_zip_with_unflagged_utf8_names_is_readable(self):
        """不打标记但写 UTF-8 的（`Compress-Archive` 就这样）同样要还原。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_name = "2026-09-23-1139-m3-压缩归档示例"
            zip_path = self._hand_made_zip(tmp_path, bundle_name, encoding="utf-8")

            bundle_dir, _ = issue_bundle.unpack_bundle(zip_path, tmp_path / "inbox")

            self.assertEqual(bundle_dir.name, bundle_name)
            self.assertTrue((bundle_dir / "evidence" / "说明.txt").is_file())

    def test_unpack_lands_under_the_inbox_and_summarizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = self._packed(tmp_path)

            bundle_dir, summary = issue_bundle.unpack_bundle(zip_path, tmp_path / "inbox")

            self.assertEqual(bundle_dir.name, zip_path.name.removesuffix(".zip"))
            self.assertTrue((bundle_dir / "facts.json").is_file())
            self.assertTrue((bundle_dir / "logs" / "runtime" / "gui.log").is_file())
            self.assertIn("一、问题描述", summary)
            self.assertIn("还有没填的项", summary)      # 没填的七项要点名
            self.assertIn("m9", summary)
            self.assertIn("深挖顺序", summary)

    def test_unpack_names_the_still_empty_report_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = self._packed(tmp_path)
            _, summary = issue_bundle.unpack_bundle(zip_path, tmp_path / "inbox")
            self.assertIn("1. 现象", summary)

    def test_a_plain_zip_is_not_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            stray = Path(tmp) / "随手打的.zip"
            with zipfile.ZipFile(stray, "w") as archive:
                archive.writestr("a.txt", "x")
                archive.writestr("b.txt", "y")

            with self.assertRaises(ValueError):
                issue_bundle.unpack_bundle(stray, Path(tmp) / "inbox")

    def test_member_escaping_the_bundle_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stray = tmp_path / "坏包.zip"
            with zipfile.ZipFile(stray, "w") as archive:
                archive.writestr("bundle/ok.txt", "x")
                archive.writestr("bundle/../../escaped.txt", "x")

            with self.assertRaises(ValueError):
                issue_bundle.unpack_bundle(stray, tmp_path / "inbox")

            self.assertFalse((tmp_path / "escaped.txt").exists())
            self.assertFalse((tmp_path.parent / "escaped.txt").exists())

    def test_cli_unpack_prints_the_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = self._packed(tmp_path)
            out = io.StringIO()
            with redirect_stdout(out):
                code = issue_bundle.main(["--unpack", str(zip_path),
                                          "--into", str(tmp_path / "inbox")])
            self.assertEqual(code, 0)
            self.assertIn("一、问题描述", out.getvalue())
            self.assertTrue((tmp_path / "inbox" / zip_path.name.removesuffix(".zip") / "REPORT.md").is_file())


class CliTests(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = issue_bundle.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_list_on_empty_out_root_teaches_how_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self._run(["--list", "--out", tmp])
            self.assertEqual(code, 0)
            self.assertIn("还没有记录", out)

    def test_pack_without_any_bundle_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self._run(["--pack", "", "--out", tmp])

    def test_newest_prints_the_bundle_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(Path(tmp))
            bundle = issue_bundle.create_bundle(
                "最新一份", root / "config" / "config.toml",
                out_root=Path(tmp) / "issues", root=root, now=STAMP)
            code, out, _ = self._run(["--newest", "--out", str(Path(tmp) / "issues")])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), str(bundle))

    def test_missing_bundle_does_not_crash_the_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp) / "issues"
            root = _fake_root(Path(tmp))
            # REPORT.md 不在（目录是空的）：照打包，只是点名提醒
            bundle = out_root / "2026-09-22-2033-m9-手滑删了"
            bundle.mkdir(parents=True)
            (bundle / "facts.json").write_text("{}", encoding="utf-8")
            code, out, _ = self._run(["--pack", str(bundle)])
            self.assertEqual(code, 0)
            self.assertIn("没有 REPORT.md", out)
            self.assertTrue(issue_bundle.zip_path_for(bundle).is_file())


class InteractiveTests(unittest.TestCase):
    """双击流程（.cmd 调 --prompt）：问点题 → 采集 → 填完回车 → 打包。"""

    def _run(self, root: Path, out_root: Path, answers: list):
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.dict(os.environ, {"BESTSELLER_NO_DIALOG": "1"}), \
                mock.patch("builtins.input", side_effect=answers):
            code = issue_bundle.interactive(root=root, out_root=out_root)
        return code, out.getvalue()

    def test_title_then_enter_packs_the_new_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = _fake_root(tmp_path)
            out_root = tmp_path / "issues"

            code, text = self._run(root, out_root, ["交互自检", ""])

            self.assertEqual(code, 0)
            self.assertIn("记录目录", text)
            zips = list(out_root.glob("*.zip"))
            self.assertEqual(len(zips), 1)
            self.assertEqual([d.name for d in out_root.iterdir() if d.is_dir()],
                             [zips[0].stem])

    def test_empty_title_packs_the_newest_without_creating(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = _fake_root(tmp_path)
            out_root = tmp_path / "issues"
            first = issue_bundle.create_bundle("早先的一份", root / "config" / "config.toml",
                                               out_root=out_root, root=root, now=STAMP)

            code, _ = self._run(root, out_root, [""])

            self.assertEqual(code, 0)
            self.assertEqual([d for d in out_root.iterdir() if d.is_dir()], [first])
            self.assertTrue(issue_bundle.zip_path_for(first).is_file())

    def test_ctrl_c_at_the_title_leaves_everything_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = _fake_root(tmp_path)
            out_root = tmp_path / "issues"

            with redirect_stdout(io.StringIO()), \
                    mock.patch("builtins.input", side_effect=KeyboardInterrupt):
                code = issue_bundle.interactive(root=root, out_root=out_root)

            self.assertEqual(code, 1)
            self.assertFalse(out_root.exists())


if __name__ == "__main__":
    unittest.main()
