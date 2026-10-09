"""票08：SKU 判断集的跨机共享与版本隔离（打包、发布、收取、决定并入、回放）。

接缝（都走公开口）：

- **打包与包身份**：`sku_sharing.build_package` / `package_digest` / `read_package_meta`
  ——临时缓存库与临时 SKU 账本库进、包文件出，不碰 git；缓存行按「预置行」直接写库
  （真产出走分析服务，见端到端用例）。
- **发布与收取**：`sku_sharing.publish` / `collect`——真 git（本地裸库当远端、克隆当
  机器，`tests/git_repos` 的样例台）、真缓存库、真账本库、真导入账。世界模板前缀
  `sku-share-*`，与 test_judgment_set 的 `judgment-*` 不撞。
- **决定并入**：`analysis_store.DraftStore` 的 SKU 账本口（write_sku / sku_ledger /
  merge_incoming_sku / sku_conflicts）。
- **消费侧**：收进来的判断与推荐直接命中（零模型调用）走真 AnalysisService；
  回放语义（人工排除／拆组／混码／未确认／证据变化）走公开分析服务终态。
"""
from __future__ import annotations

import copy
import datetime as dt
import gzip
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import test_sku_recommendations as sku_fixtures

from bestseller_monitor import sku_sharing
from bestseller_monitor.analysis_store import DraftStore
from bestseller_monitor.db import CST
from bestseller_monitor.matching import ModelFailure as _ModelFailure  # noqa: F401  (夹具兼容)
from bestseller_monitor.sku_grouping import identity as sku_identity
from bestseller_monitor.sku_matching import evidence_version as sku_evidence_version
from tests.git_repos import GitSandbox, WorldTemplate
from helpers import product_picture


def git_bytes(args, **kwargs) -> bytes:
    """真跑一次 git 并拿**字节**（包的二进制内容经文本解码会坏掉）。"""
    done = subprocess.run(["git", *args], capture_output=True,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C"), **kwargs)
    if done.returncode != 0:
        raise AssertionError(f"样例台 git 失败：git {' '.join(args)}\n{done.stderr!r}")
    return done.stdout


# 固定时刻：包上的生成时刻不进身份（取值本身只求可复现）。
STAMP = dt.datetime(2026, 10, 9, 21, 0, tzinfo=CST)
STAMP_ISO = STAMP.isoformat(timespec="seconds")


def _world_paths(repo: str) -> dict[str, str]:
    """交换区里某个库的「一台机器的世界」三件东西的相对路径：裸远端、交换区里的克隆、旁路工作克隆。"""
    return {"remote": f"{repo}.git", "exchange": f"exchange/{repo}", "work": f"{repo}-work"}


def _world_template(repo: str) -> WorldTemplate:
    """取（必要时搭一次）一个库的世界模板：空裸库 + 未出生克隆，每测试进程只搭一次。"""
    names = _world_paths(repo)
    world = (names["remote"], names["exchange"], names["work"])
    return WorldTemplate.obtain(f"sku-share-{repo}", lambda site: site.world(*world))


def _take_world(box: GitSandbox, repo: str) -> dict[str, Path]:
    """给这个沙盒取一份（复制）`repo` 的独立世界，返回三件的绝对路径。"""
    _world_template(repo).take(box)
    return {key: box.tmp / rel for key, rel in _world_paths(repo).items()}


class SkuSharingCase(unittest.TestCase):
    """共用夹具：真缓存库（SKU 表按生产 DDL 备好）+ 真 SKU 账本库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "matching.sqlite"
        self.store = self.root / "drafts.sqlite"

    # ---- 预置行（与真产出同一条生产 DDL 口径） ----

    def seed_judgment(self, pair, signature, result, *, machine_id="m1", cache=None):
        from bestseller_monitor.sku_lifecycle import ensure_tables
        conn = sqlite3.connect(cache or self.cache)
        try:
            with conn:
                ensure_tables(conn, machine_id)
                conn.execute(
                    "INSERT OR REPLACE INTO sku_judgments(pair,signature,machine_id,result)"
                    " VALUES (?,?,?,?)",
                    (pair, signature, machine_id, json.dumps(result, ensure_ascii=False)))
        finally:
            conn.close()

    def seed_recommendation(self, context, signature, result, *, machine_id="m1", cache=None):
        from bestseller_monitor.sku_lifecycle import ensure_tables
        conn = sqlite3.connect(cache or self.cache)
        try:
            with conn:
                ensure_tables(conn, machine_id)
                conn.execute(
                    "INSERT OR REPLACE INTO sku_recommendations(context,signature,machine_id,result)"
                    " VALUES (?,?,?,?)",
                    (context, signature, machine_id, json.dumps(result, ensure_ascii=False)))
        finally:
            conn.close()

    def seed_decisions(self, *, groups=(), excluded_skus=(), excluded_pairs=(),
                       machine="m1", store=None, seen_at=STAMP_ISO):
        """往 SKU 账本写一组决定（走并入公开口：空账本上即原样收下）。"""
        DraftStore(store or self.store, machine).merge_incoming_sku(
            {"groups": [dict(row) for row in groups],
             "excluded_skus": [list(row) for row in excluded_skus],
             "excluded_pairs": [list(row) for row in excluded_pairs]},
            source=machine, seen_at=seen_at)

    def ledger(self, *, store=None):
        return DraftStore(store or self.store, "m-test").sku_ledger()

    @staticmethod
    def group_row(members, *, confirmed=False, excluded=False, group_id="sku-manual-111111111111",
                  machine_id="", versions=None):
        """账本形状的一行人工组：members=[[身份, 证据摘要], …]（与 `sku_ledger()` 同形）。"""
        pairs = sorted([member, (versions or {}).get(member, "v-%s" % len(member))]
                       for member in members)
        return {"members": pairs, "confirmed": confirmed, "excluded": excluded,
                "group_id": group_id, "machine_id": machine_id}


class PackageTests(SkuSharingCase):
    """打包：五类表、内容身份、元数据。"""

    def build(self, package_name="sku.db", *, cache=None, store=None, machine_id="m1",
              generated_at=STAMP):
        package = self.root / package_name
        sku_sharing.build_package(cache or self.cache, store or self.store, package,
                                  machine_id=machine_id, generated_at=generated_at)
        return package

    def test_the_package_carries_the_sku_tables_and_leaves_the_rest_out(self):
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        self.seed_recommendation("ctx-1", "sig-2", {"group_id": "sku-auto-a", "reason": "同款"})
        self.seed_decisions(groups=[self.group_row(["s1", "s2"], confirmed=True)],
                            excluded_skus=[["s9"]], excluded_pairs=[["s3", "s4"]])
        # 缓存里加一张无关的表：进不了包（投影由包内表清单决定）。
        with closing(sqlite3.connect(self.cache)) as conn:
            conn.execute("CREATE TABLE unrelated (x TEXT)")
            conn.commit()
        package = self.build()
        with closing(sqlite3.connect(package)) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(
                {"sku_judgments", "sku_recommendations", "sku_manual_groups",
                 "sku_excluded_skus", "sku_excluded_pairs", "sku_meta"}, names)
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM sku_judgments").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM sku_recommendations").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM sku_manual_groups").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM sku_excluded_skus").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM sku_excluded_pairs").fetchone()[0])

    def test_missing_source_databases_are_an_empty_package_not_an_error(self):
        package = self.build(cache=self.root / "absent.sqlite", store=self.root / "absent2.sqlite")
        with closing(sqlite3.connect(package)) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM sku_judgments").fetchone()[0])
            meta = sku_sharing.read_package_meta(conn)
            self.assertTrue(all(count == 0 for count in meta["rows"].values()))

    def test_meta_records_who_packed_it_when_and_what_is_inside(self):
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        self.seed_decisions(groups=[self.group_row(["s1", "s2"])])
        package = self.build(machine_id="m4")
        with closing(sqlite3.connect(package)) as conn:
            meta = sku_sharing.read_package_meta(conn)
        self.assertEqual("m4", meta["machine_id"])
        self.assertEqual(STAMP_ISO, meta["generated_at"])
        self.assertEqual(sku_sharing.SKU_FORMAT_VERSION, meta["format_version"])
        self.assertEqual({"sku_judgments": 1, "sku_recommendations": 0, "sku_manual_groups": 1,
                          "sku_excluded_skus": 0, "sku_excluded_pairs": 0}, meta["rows"])

    def test_the_identity_is_the_content_not_the_packer_the_moment_nor_the_layout(self):
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        first = self.digest_of(self.build("a.db", machine_id="m1", generated_at=STAMP))
        second = self.digest_of(self.build("b.db", machine_id="m9",
                                           generated_at=STAMP.replace(hour=22)))
        self.assertEqual(first, second)
        self.seed_judgment("pair-2", "sig-1", {"same": False, "confidence": .9})
        third = self.digest_of(self.build("c.db"))
        self.assertNotEqual(first, third)

    def digest_of(self, package: Path) -> str:
        with closing(sqlite3.connect(package)) as conn:
            return sku_sharing.package_digest(conn)


class PublishTests(SkuSharingCase):
    """发布：新路径、内容身份幂等、全空不发布、失败回退与版本闸门。"""

    def setUp(self):
        super().setUp()
        self.box = GitSandbox(self)
        self.world = _take_world(self.box, "judged-m1")
        self.repo = self.world["exchange"]
        self.exchange = self.repo.parent

    def publish(self, **kwargs):
        kwargs.setdefault("cache", self.cache)
        kwargs.setdefault("store", self.store)
        kwargs.setdefault("now", STAMP)
        return sku_sharing.publish(self.exchange, "m1", **kwargs)

    def read_published(self, rev="HEAD", rel=sku_sharing.SKU_PACKAGE_REL):
        data = git_bytes(["show", f"{rev}:{rel}"], cwd=self.repo)
        path = self.root / f"published-{rev.replace('~', '-')}.db"
        path.write_bytes(gzip.decompress(data))
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        return conn

    def commits(self) -> int:
        return int(self.box.must("rev-list", "--count", "HEAD", cwd=self.repo).strip())

    def test_publish_lands_the_package_in_the_machine_repo(self):
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        result = self.publish()
        self.assertTrue(result.published, result.failure)
        self.assertFalse(result.unchanged)
        self.assertEqual("m1", result.machine_id)
        self.assertEqual("judged-m1/data/sku-judgments.db.gz", result.package_rel)
        self.assertEqual(1, result.rows["sku_judgments"])
        package = self.read_published()
        self.assertEqual("m1", sku_sharing.read_package_meta(package)["machine_id"])
        self.assertEqual(1, package.execute("SELECT COUNT(*) FROM sku_judgments").fetchone()[0])
        self.assertEqual(sku_sharing.package_digest(package), result.digest)
        self.assertEqual(1, self.commits())

    def test_publishing_the_same_content_again_makes_no_extra_commit(self):
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        first = self.publish()
        second = self.publish()
        self.assertTrue(second.published)
        self.assertTrue(second.unchanged)
        self.assertEqual(first.commit, second.commit)
        self.assertEqual(1, self.commits())

    def test_new_rows_land_as_a_new_commit_and_the_old_version_stays(self):
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        first = self.publish()
        self.seed_judgment("pair-2", "sig-1", {"same": False, "confidence": .9})
        second = self.publish()
        self.assertTrue(second.published, second.failure)
        self.assertNotEqual(second.digest, first.digest)
        self.assertEqual(2, self.commits())
        self.assertEqual(sku_sharing.package_digest(self.read_published(rev="HEAD~1")), first.digest,
                         "旧版留在 git 历史里，不在工作树里堆批次文件")

    def test_the_legacy_package_path_is_untouched(self):
        """SKU 包与旧商品包同库不同文件：发布 SKU 不动旧路径一个字节。"""
        self.box.commit_push(self.repo, {"data/judgments.db.gz": b"legacy-bytes"})
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        self.publish()
        self.assertEqual(b"legacy-bytes",
                         git_bytes(["show", "HEAD:data/judgments.db.gz"], cwd=self.repo))

    def test_an_empty_machine_publishes_nothing(self):
        result = self.publish(cache=self.root / "never-ran.sqlite",
                              store=self.root / "never-saved.sqlite")
        self.assertFalse(result.failed, "全空不是失败，是可读的一句说明")
        self.assertFalse(result.published)
        self.assertIn("空", result.note)

    def test_an_uncloned_judged_repo_names_the_step_to_take(self):
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        result = sku_sharing.publish(self.root / "elsewhere", "m1",
                                     cache=self.cache, store=self.store, now=STAMP)
        self.assertTrue(result.failed)
        self.assertFalse(result.published)
        self.assertIn("judged-m1", result.failure)

    def test_a_rejected_push_cleans_the_clone_and_the_next_run_heals(self):
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        counter = self.box.tmp / "hook-runs"
        self.box.install_declining_hook(self.repo, counter)
        first = self.publish()
        self.assertTrue(first.failed)
        self.assertFalse(first.published)
        self.assertEqual("", self.box.must("status", "--porcelain", cwd=self.repo).strip(),
                         "推送失败后克隆要退回远端状态（没有半截改动）")
        self.box.remove_pre_push(self.repo)
        second = self.publish()
        self.assertTrue(second.published, second.failure)

    def test_an_unreadable_published_package_is_overwritten(self):
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        self.box.commit_push(self.repo, {sku_sharing.SKU_PACKAGE_REL: b"not an sku package"})
        result = self.publish()
        self.assertTrue(result.published, result.failure)
        self.assertEqual(1, sku_sharing.read_package_meta(
            self.read_published())["rows"]["sku_judgments"])

    def unknown_version_package(self) -> bytes:
        """先打一份真包，把口径版本改成未来版本（本机不认识的新格式）。"""
        from bestseller_monitor.export import row_bytes  # noqa: F401  （仅为保持与实现同源提示）
        import gzip as _gzip
        package = self.root / "future.db"
        sku_sharing.build_package(self.cache, self.store, package,
                                  machine_id="m9", generated_at=STAMP)
        with closing(sqlite3.connect(package)) as conn:
            conn.execute("UPDATE sku_meta SET value='sku-v2' WHERE key='format_version'")
            conn.commit()
        return _gzip.compress(package.read_bytes(), mtime=0)

    def test_an_unknown_format_version_is_refused_not_overwritten(self):
        """远端那份是更新的 SKU 格式：明确拒绝，绝不覆盖（覆盖会毁掉未来机器的数据）。"""
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95})
        future = self.unknown_version_package()
        self.box.commit_push(self.repo, {sku_sharing.SKU_PACKAGE_REL: future})
        before = self.commits()
        result = self.publish()
        self.assertTrue(result.failed)
        self.assertIn("sku-v2", result.failure)
        self.assertIn("升级", result.failure)
        self.assertEqual(before, self.commits(), "没有新提交，远端那份一个字节没动")
        self.assertEqual(future, git_bytes(["show", f"HEAD:{sku_sharing.SKU_PACKAGE_REL}"],
                                           cwd=self.repo))


class CollectTests(SkuSharingCase):
    """收取：幂等导入、本机行保留、坏包隔离、版本拒绝与可读说明。"""

    def setUp(self):
        super().setUp()
        self.box = GitSandbox(self)
        self.machines = {}

    def world_for(self, machine):
        if machine not in self.machines:
            self.machines[machine] = _take_world(self.box, f"judged-{machine}")
        return self.machines[machine]

    def exchange(self):
        return self.world_for("m1")["exchange"].parent

    def machine_cache(self, machine):
        return self.root / f"{machine}.sqlite"

    def publish_from(self, machine="m1", *, judgments=(), recommendations=(), groups=(),
                     excluded_skus=(), excluded_pairs=()):
        self.world_for(machine)
        cache, store = self.machine_cache(machine), self.root / f"{machine}-drafts.sqlite"
        for pair, signature, result in judgments:
            self.seed_judgment(pair, signature, result, machine_id=machine, cache=cache)
        for context, signature, result in recommendations:
            self.seed_recommendation(context, signature, result, machine_id=machine, cache=cache)
        if groups or excluded_skus or excluded_pairs:
            self.seed_decisions(groups=groups, excluded_skus=excluded_skus,
                                excluded_pairs=excluded_pairs, machine=machine, store=store)
        return sku_sharing.publish(self.exchange(), machine, cache=cache, store=store, now=STAMP)

    def collect(self, machine_id="m4", *, cache=None, store=None):
        return sku_sharing.collect(self.exchange(), machine_id,
                                   cache=cache if cache is not None else self.machine_cache(machine_id),
                                   store=store if store is not None else self.root / f"{machine_id}-drafts.sqlite",
                                   now=STAMP)

    def cache_rows(self, table, machine="m4"):
        with closing(sqlite3.connect(self.machine_cache(machine))) as conn:
            return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()

    def test_collect_imports_rows_and_decisions_and_records_the_ledger(self):
        self.publish_from(judgments=[("pair-1", "sig-1", {"same": True, "confidence": .95})],
                          recommendations=[("ctx-1", "sig-2", {"group_id": "sku-auto-a", "reason": "同款"})],
                          groups=[self.group_row(["s1", "s2"], confirmed=True)],
                          excluded_skus=[["s9"]], excluded_pairs=[["s3", "s4"]])
        outcome = self.collect()
        self.assertFalse(outcome.failed)
        (result,) = outcome.results
        self.assertEqual("m1", result.machine)
        self.assertFalse(result.skipped)
        self.assertEqual({"sku_judgments": 1, "sku_recommendations": 1, "sku_manual_groups": 1,
                          "sku_excluded_skus": 1, "sku_excluded_pairs": 1}, result.rows)
        self.assertEqual(5, sum(result.added.values()))
        self.assertEqual(3, result.adopted_total, "三张决定表的行都并进了账本")
        self.assertEqual(1, len(self.cache_rows("sku_judgments")))
        self.assertEqual(1, len(self.cache_rows("sku_recommendations")))
        ledger = self.ledger(store=self.root / "m4-drafts.sqlite")
        self.assertEqual(1, len(ledger["groups"]))
        self.assertEqual("m1", ledger["groups"][0]["machine_id"])
        self.assertEqual(1, len(ledger["excluded_skus"]))
        self.assertEqual(1, len(ledger["excluded_pairs"]))

    def test_collecting_the_same_content_again_changes_nothing(self):
        self.publish_from(judgments=[("pair-1", "sig-1", {"same": True, "confidence": .95})])
        first = self.collect()
        second = self.collect()
        (result,) = second.results
        self.assertTrue(result.skipped)
        self.assertEqual(1, result.rows["sku_judgments"], "计数从导入账回放")
        self.assertEqual(1, len(self.cache_rows("sku_judgments")))
        with closing(sqlite3.connect(self.machine_cache("m4"))) as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM sku_imports").fetchone()[0])

    def test_rows_the_machine_already_has_keep_their_own_source(self):
        self.seed_judgment("pair-1", "sig-1", {"same": False, "confidence": .9},
                           machine_id="m4", cache=self.machine_cache("m4"))
        self.publish_from(judgments=[("pair-1", "sig-1", {"same": True, "confidence": .95})])
        self.collect()
        (row,) = self.cache_rows("sku_judgments")
        self.assertEqual("m4", row[2], "先到者保留：本机已有的行不动")
        self.assertIn('"same": false', row[3])

    def test_collect_creates_the_cache_on_a_machine_that_never_ran_an_analysis(self):
        self.publish_from(judgments=[("pair-1", "sig-1", {"same": True, "confidence": .95})])
        outcome = self.collect()
        self.assertFalse(outcome.failed)
        self.assertEqual(1, len(self.cache_rows("sku_judgments")))

    def test_an_unknown_format_version_is_refused(self):
        cache = self.machine_cache("m1")
        self.seed_judgment("pair-1", "sig-1", {"same": True, "confidence": .95}, cache=cache)
        future = PublishTests.unknown_version_package(self)  # 借同一助手造一份未来版本包
        self.box.commit_push(self.world_for("m1")["exchange"],
                             {sku_sharing.SKU_PACKAGE_REL: future})
        outcome = self.collect()
        (result,) = outcome.results
        self.assertTrue(result.failed)
        self.assertIn("sku-v2", result.failure)
        self.assertIn("升级", result.failure)
        self.assertEqual([], self.cache_rows("sku_judgments"), "明确拒绝：一行都不导入")

    def test_a_corrupt_package_leaves_the_cache_untouched(self):
        self.box.commit_push(self.world_for("m1")["exchange"],
                             {sku_sharing.SKU_PACKAGE_REL: b"not an sku package"})
        outcome = self.collect()
        (result,) = outcome.results
        self.assertTrue(result.failed)
        self.assertIn("读不出来", result.failure)
        self.assertEqual([], self.cache_rows("sku_judgments"))

    def test_a_source_that_has_not_published_anything_is_a_readable_note(self):
        outcome = self.collect()
        (result,) = outcome.results
        self.assertFalse(result.failed)
        self.assertIn("还没发布过 SKU 判断集", result.note)

    def test_no_sources_at_all_says_so_readably(self):
        outcome = sku_sharing.collect(self.root / "empty-exchange", "m4",
                                      cache=self.machine_cache("m4"),
                                      store=self.root / "m4-drafts.sqlite", now=STAMP)
        self.assertFalse(outcome.failed)
        self.assertIn("没有别的机器", outcome.notes[0])

    def test_a_broken_source_does_not_stop_the_others(self):
        self.publish_from("m2", judgments=[("pair-2", "sig-1", {"same": True, "confidence": .95})])
        (self.world_for("m1")["exchange"] / ".git").rename(
            self.world_for("m1")["exchange"] / ".git-away")
        outcome = self.collect()
        self.assertFalse(outcome.failed)
        self.assertEqual(1, len(self.cache_rows("sku_judgments")))
        by_machine = {result.machine: result for result in outcome.results}
        self.assertIn("还不是 git 克隆", by_machine["m1"].note)
        self.assertFalse(by_machine["m2"].failed)


class DecisionMergeTests(SkuSharingCase):
    """SKU 账本并入语义：同一条跳过、同源让位、异源矛盾记冲突（两边都不动）、幂等。"""

    def merge(self, machine, *, groups=(), excluded_skus=(), excluded_pairs=()):
        return DraftStore(self.store, machine).merge_incoming_sku(
            {"groups": [dict(row) for row in groups],
             "excluded_skus": [list(row) for row in excluded_skus],
             "excluded_pairs": [list(row) for row in excluded_pairs]},
            source=machine, seen_at=STAMP_ISO)

    def groups(self):
        return self.ledger()["groups"]

    def conflicts(self):
        return DraftStore(self.store, "m-test").sku_conflicts()

    def test_same_decision_from_another_machine_is_skipped(self):
        row = self.group_row(["s1", "s2"], confirmed=True)
        self.merge("m1", groups=[row])
        result = self.merge("m2", groups=[row])
        self.assertEqual(0, result.adopted["sku_manual_groups"])
        self.assertEqual(1, len(self.groups()))
        self.assertEqual("m1", self.groups()[0]["machine_id"], "先到者保留")

    def test_the_same_machine_updating_itself_supersedes(self):
        self.merge("m1", groups=[self.group_row(["s1", "s2"], group_id="g-1")])
        result = self.merge("m1", groups=[self.group_row(["s1", "s2", "s3"], group_id="g-1")])
        self.assertEqual(1, result.adopted["sku_manual_groups"])
        rows = self.groups()
        self.assertEqual(1, len(rows), "旧话让位，不是并存")
        self.assertEqual(["s1", "s2", "s3"], sorted(member for member, _ in rows[0]["members"]))

    def test_a_contradicting_group_from_another_machine_is_a_conflict(self):
        self.merge("m1", groups=[self.group_row(["s1", "s2"], group_id="g-1")])
        result = self.merge("m2", groups=[self.group_row(["s1", "s3"], group_id="g-2",
                                                         machine_id="m2")])
        self.assertEqual(0, result.adopted["sku_manual_groups"])
        (row,) = self.groups()
        self.assertEqual(["s1", "s2"], sorted(member for member, _ in row["members"]),
                         "本机决定原样保留")
        (conflict,) = self.conflicts()
        self.assertEqual("different_groups", conflict.kind)
        self.assertEqual("m2", conflict.incoming_machine)
        self.assertEqual(("s1", "s2", "s3"), conflict.members, "冲突记的是两侧牵涉的身份并集")

    def test_an_exclusion_conflicts_with_a_foreign_group(self):
        self.merge("m1", groups=[self.group_row(["s1", "s2"], group_id="g-1")])
        result = self.merge("m2", excluded_skus=[["s1", "m2"]])
        self.assertEqual(0, result.adopted["sku_excluded_skus"])
        self.assertEqual([], self.ledger()["excluded_skus"])
        (conflict,) = self.conflicts()
        self.assertEqual("group_vs_exclusion", conflict.kind)
        self.assertEqual(("s1", "s2"), conflict.members)

    def test_a_group_conflicts_with_a_local_exclusion_pair(self):
        self.merge("m1", excluded_pairs=[["s1", "s2"]])
        result = self.merge("m2", groups=[self.group_row(["s1", "s2"], group_id="g-2",
                                                         machine_id="m2")])
        self.assertEqual(0, result.adopted["sku_manual_groups"])
        self.assertEqual(1, len(self.ledger()["excluded_pairs"]), "本机排除原样保留")
        (conflict,) = self.conflicts()
        self.assertEqual("group_vs_exclusion", conflict.kind)
        self.assertEqual("m2", conflict.incoming_machine)

    def test_same_source_updates_of_exclusions_supersede(self):
        self.merge("m1", excluded_skus=[["s1"]])
        result = self.merge("m1", groups=[self.group_row(["s1", "s2"], group_id="g-1")])
        # 两行都没显式带来源：按通道来源记 m1，因此后一条算「同一台机器在更新自己」。
        self.assertEqual(1, result.adopted["sku_manual_groups"])
        self.assertEqual([], self.ledger()["excluded_skus"], "同一台机器的新话覆盖旧话")
        self.assertEqual(1, len(self.groups()))

    def test_a_clashing_self_update_retires_nothing(self):
        """审查 B1：同源同集让位只能在确认不矛盾之后——矛盾时先删会让共享行永久丢失。"""
        first = sku_identity({"shop_key": "S", "offer_id": "1", "sku_id": "s1"})
        second = sku_identity({"shop_key": "S", "offer_id": "1", "sku_id": "s2"})
        third = sku_identity({"shop_key": "S", "offer_id": "1", "sku_id": "s3"})
        self.merge("m1", groups=[self.group_row([first, second, third], group_id="g-1")])
        # m2 的局部保存行（保存侧是本地权威、静默写入）：账本成为 {s1,s2,s3}(m1)+{s1,s2}(m2)。
        DraftStore(self.store, "m2").write_sku(
            {"id": "d1", "saved_at": "2026-10-09T00:00:00+00:00",
             "start": "2026-09-07", "end": "2026-09-14",
             "members": [{"shop_key": "S", "offer_id": "1", "sku_id": "s1"},
                         {"shop_key": "S", "offer_id": "1", "sku_id": "s2"}]},
            decisions={"groups": [{"members": sorted([[first, "v1"], [second, "v1"]]),
                                   "confirmed": False, "excluded": False,
                                   "group_id": "g-1", "machine_id": ""}],
                       "excluded_skus": [], "excluded_pairs": []})
        before = sorted(json.dumps(row["members"], ensure_ascii=False)
                        for row in self.groups())
        self.merge("m1", groups=[self.group_row([first, second, third], group_id="g-1")])
        after = sorted(json.dumps(row["members"], ensure_ascii=False)
                       for row in self.groups())
        self.assertEqual(before, after, "矛盾时两边都不动：同源同集行不许先被删")
        self.assertTrue(self.conflicts(), "矛盾照记")

    def test_a_clashing_exclusion_self_update_retires_nothing(self):
        """B1 同规：排除身份行的同源让位也不许抢在冲突判定之前执行。

        正常流程里这两行不会共存（并入互斥、保存会撤在场身份行），按「预置行」直写
        造出共存态：同源排除行（m1）＋异源组（m2）同时在账。旧码上这条用例会红：
        让位先删掉 m1 的排除行，冲突判定随后返回，行再也补不回来。
        """
        first = sku_identity({"shop_key": "S", "offer_id": "1", "sku_id": "s1"})
        second = sku_identity({"shop_key": "S", "offer_id": "1", "sku_id": "s2"})
        DraftStore(self.store, "seed").merge_incoming_sku(
            {"groups": [], "excluded_skus": [[first, "m1"]], "excluded_pairs": []},
            source="m1", seen_at=STAMP_ISO)
        with closing(sqlite3.connect(self.store)) as conn:
            conn.execute("INSERT OR IGNORE INTO sku_manual_groups"
                         "(members,confirmed,excluded,group_id,saved_at,machine_id)"
                         " VALUES(?,?,?,?,?,?)",
                         (json.dumps(sorted([[first, "v1"], [second, "v1"]]), ensure_ascii=False),
                          0, 0, "g-2", STAMP_ISO, "m2"))
            conn.commit()
        before = self.ledger()["excluded_skus"]
        self.merge("m1", excluded_skus=[[first, "m1"]])
        self.assertEqual(before, self.ledger()["excluded_skus"], "矛盾时先不动本机一侧")
        self.assertTrue(self.conflicts(), "矛盾照记")

    def test_two_element_exclusion_rows_stamp_the_channel_source(self):
        """审查发现的边界：只带两端的排除行（[身份]／[a, b]）不能把第二端当来源机器。"""
        self.merge("m1", excluded_pairs=[["s1", "s2"]], excluded_skus=[["s3"]])
        self.assertEqual([["s1", "s2", "m1"]], self.ledger()["excluded_pairs"])
        self.assertEqual([["s3", "m1"]], self.ledger()["excluded_skus"])

    def test_conflicts_are_recorded_once(self):
        self.merge("m1", groups=[self.group_row(["s1", "s2"], group_id="g-1")])
        clash = self.group_row(["s1", "s3"], group_id="g-2", machine_id="m2")
        first = self.merge("m2", groups=[clash])
        second = self.merge("m2", groups=[clash])
        self.assertTrue(first.conflicts[0].recorded)
        self.assertFalse(second.conflicts[0].recorded, "同一条冲突重复遇到只核对不重记")
        self.assertEqual(1, len(self.conflicts()))


class LedgerWriteTests(unittest.TestCase):
    """保存 → SKU 账本：真服务上整理并保存，账本里出现同一决定（版本、组 ID、来源随行）。"""

    setUp = sku_fixtures.SkuRecommendationTests.setUp
    observe = sku_fixtures.SkuRecommendationTests.observe
    member = staticmethod(sku_fixtures.SkuRecommendationTests.member)
    group_for = classmethod(sku_fixtures.SkuRecommendationTests.group_for.__func__)
    enable_model = sku_fixtures.SkuRecommendationTests.enable_model
    seed = sku_fixtures.SkuRecommendationTests.seed
    analyze = sku_fixtures.SkuRecommendationTests.analyze
    evidence = sku_fixtures.SkuRecommendationTests.evidence

    def store_path(self):
        return self.service.config.store

    def ledger(self):
        return DraftStore(self.store_path(), "m-test").sku_ledger()

    def two_members(self):
        self.result = dict(same=False, confidence=.95, reason="不同主体")   # 不让图文自动归组，人工来
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        return a, b

    def test_saving_a_manual_move_writes_the_group_to_the_ledger(self):
        a, b = self.two_members()
        first = self.analyze()
        target = self.group_for(first, a)["id"]
        self.service.edit_group(first["id"], "move", self.group_for(first, b)["id"], b, target)
        self.service.save_draft(first["id"])
        (row,) = self.ledger()["groups"]
        self.assertEqual(sorted([sku_identity(a), sku_identity(b)]),
                         sorted(member for member, _ in row["members"]))
        self.assertFalse(row["confirmed"])
        self.assertFalse(row["excluded"])
        self.assertEqual(target, row["group_id"], "组 ID 随行（跨机推荐语境要用）")
        self.assertEqual(self.service.config.machine, row["machine_id"])
        versions = dict((member, version) for member, version in row["members"])
        self.assertEqual(sku_evidence_version(self.evidence(first, b)), versions[sku_identity(b)])

    def test_saving_an_exclusion_writes_the_exclusion_to_the_ledger(self):
        a, b = self.two_members()
        first = self.analyze()
        self.service.edit_group(first["id"], "exclude", self.group_for(first, b)["id"], b)
        self.service.save_draft(first["id"])
        ledger = self.ledger()
        self.assertEqual([[sku_identity(b), self.service.config.machine]],
                         [[entry[0], entry[1]] for entry in ledger["excluded_skus"]])
        excluded = [row for row in ledger["groups"] if row["excluded"]]
        self.assertEqual(1, len(excluded), "人工排除的独立组也进账本（带排除旗标）")

    def test_undoing_an_exclusion_removes_its_row(self):
        a, b = self.two_members()
        first = self.analyze()
        self.service.edit_group(first["id"], "exclude", self.group_for(first, b)["id"], b)
        self.service.save_draft(first["id"])
        self.assertEqual(1, len(self.ledger()["excluded_skus"]))
        excluded_group = self.group_for(self.service.get(first["id"]), b)["id"]
        self.service.edit_group(first["id"], "undo_exclude", excluded_group, b)
        self.service.save_draft(first["id"])
        self.assertEqual([], self.ledger()["excluded_skus"], "撤掉的排除从账本里撤下")

    def test_resaving_after_evidence_change_refreshes_versions(self):
        a, b = self.two_members()
        first = self.analyze()
        self.service.edit_group(first["id"], "move", self.group_for(first, b)["id"], b,
                                self.group_for(first, a)["id"])
        self.service.save_draft(first["id"])
        before = dict((member, version) for member, version in self.ledger()["groups"][0]["members"])
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="新原始无编码名", sku_stock=90)],
                     shop="B01", image=product_picture("blue"))
        again = self.service.retry_matching(first["id"])
        self.service.save_draft(first["id"])
        after = dict((member, version) for member, version in self.ledger()["groups"][0]["members"])
        self.assertNotEqual(before[sku_identity(b)], after[sku_identity(b)])
        self.assertEqual(sku_evidence_version(self.evidence(again, b)), after[sku_identity(b)],
                         "版本刷新为本次保存时的证据摘要")

    def test_local_save_replaces_an_adopted_foreign_row_and_keeps_its_source(self):
        a, b = self.two_members()
        first = self.analyze()
        target = self.group_for(first, a)["id"]
        self.service.edit_group(first["id"], "move", self.group_for(first, b)["id"], b, target)
        DraftStore(self.store_path(), "m1").merge_incoming_sku(
            {"groups": [{"members": sorted([[sku_identity(a), "m1-v"], [sku_identity(b), "m1-v"]]),
                         "confirmed": False, "excluded": False,
                         "group_id": "sku-manual-foreign00001", "machine_id": "m1"}],
             "excluded_skus": [], "excluded_pairs": []},
            source="m1", seen_at=STAMP_ISO)
        self.service.save_draft(first["id"])
        (row,) = self.ledger()["groups"]
        self.assertEqual("m1", row["machine_id"], "来源沿用旧行：收进来的决定往返一次不被改写")
        self.assertEqual(target, row["group_id"], "组 ID 取本次保存的组")
        versions = dict((member, version) for member, version in row["members"])
        self.assertEqual(sku_evidence_version(self.evidence(first, b)), versions[sku_identity(b)],
                         "版本刷新为本次保存时的证据摘要")


class CrossMachineCase(unittest.TestCase):
    """跨机共用的机器夹具：m1 夹具服务 + judged-m1 世界 + 机器切换/发布/收取助手。"""

    def setUp(self):
        sku_fixtures.SkuRecommendationTests.setUp(self)
        from dataclasses import replace
        from bestseller_monitor.analysis import AnalysisService
        self.service = AnalysisService(replace(self.service.config, machine="m1"),
                                       running=lambda: False)
        self.box = GitSandbox(self)
        self.world = _take_world(self.box, "judged-m1")
        self.exchange = self.world["exchange"].parent

    observe = sku_fixtures.SkuRecommendationTests.observe
    member = staticmethod(sku_fixtures.SkuRecommendationTests.member)
    group_for = classmethod(sku_fixtures.SkuRecommendationTests.group_for.__func__)
    enable_model = sku_fixtures.SkuRecommendationTests.enable_model
    seed = sku_fixtures.SkuRecommendationTests.seed
    analyze = sku_fixtures.SkuRecommendationTests.analyze
    evidence = sku_fixtures.SkuRecommendationTests.evidence

    def second_machine(self, name="m2"):
        """同夹具口径的又一台机器：自己的库存库、草稿库、缓存与模型配置。"""
        from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
        from bestseller_monitor.db import connect, Database
        from bestseller_monitor.matching import MatchingConfig, ModelConfig
        from types import SimpleNamespace
        root = Path(self.tmp.name) / name
        root.mkdir()
        path = root / "inventory.db"
        conn = connect(path)
        self.addCleanup(conn.close)
        config = AnalysisConfig(path, member_kind="sku", machine=name,
                                store=root / "drafts.sqlite",
                                matching=MatchingConfig(root / "matching.sqlite",
                                    ModelConfig(endpoint="http://127.0.0.1:9/v1/chat/completions",
                                                key_env="SKU04_TEST_KEY"),
                                    mode="direct", min_score=0))
        return SimpleNamespace(path=path, conn=conn, db=Database(conn),
                               service=AnalysisService(config, running=lambda: False))

    def on(self, machine):
        """把夹具的观察与操作口切到另一台机器（seed/observe/analyze 都走 self.*）。"""
        import contextlib

        @contextlib.contextmanager
        def switch():
            saved = (self.db, self.service, self.path)
            self.db, self.service, self.path = machine.db, machine.service, machine.path
            try:
                yield machine
            finally:
                self.db, self.service, self.path = saved
        return switch()

    def publish(self, machine_id):
        return sku_sharing.publish(self.exchange, machine_id,
                                   cache=self.service.config.matching.cache,
                                   store=self.service.config.store, now=STAMP)

    def collect(self, machine_id):
        return sku_sharing.collect(self.exchange, machine_id,
                                   cache=self.service.config.matching.cache,
                                   store=self.service.config.store, now=STAMP)


class CrossMachineTests(CrossMachineCase):
    """端到端：复用、人工决定回放、局部保护、拆组混码、证据变化与同族补员。"""

    def test_second_machine_reuses_judgments_and_uncertain_recommendation(self):
        self.seed("#A#", image=product_picture("red"))
        self.seed("#C#", shop="C01", image=product_picture("blue"))
        b = self.seed("无码", shop="B01", image=product_picture("green"))
        self.recommend_result = dict(group_id=None, reason="两组图文证据均不足以唯一归属")
        first = self.analyze()
        self.assertEqual(2, len(self.calls))
        self.assertEqual(1, len(self.recommend_calls))
        first_group = self.group_for(first, b)["id"]
        self.service.save_draft(first["id"])
        self.assertTrue(self.publish("m1").published)

        m2 = self.second_machine()
        with self.on(m2):
            self.seed("#A#", image=product_picture("red"))
            self.seed("#C#", shop="C01", image=product_picture("blue"))
            self.seed("无码", shop="B01", image=product_picture("green"))
            self.calls, self.recommend_calls = [], []
            outcome = self.collect("m2")
            self.assertFalse(outcome.failed, outcome.results[0].failure)
            result = self.analyze()
            self.assertEqual(0, len(self.calls), "共享的判断直接命中，不重复付费")
            self.assertEqual(0, len(self.recommend_calls), "成功不确定的推荐同样复用")
            self.assertEqual(first_group, self.group_for(result, b)["id"])
            member = self.evidence(result, b)
            self.assertEqual("pending_candidates", member["matching_state"])
            self.assertEqual("两组图文证据均不足以唯一归属", member["recommendation"]["reason"])
            self.assertTrue(member["recommendation"]["cached"])
            self.assertIn("多个候选", member["matching_note"])
            again = self.collect("m2")
            self.assertTrue(again.results[0].skipped, "重复收取幂等")
            self.analyze()
            self.assertEqual(0, len(self.calls))

    def test_second_machine_reuses_unique_recommendation_and_join(self):
        a = self.seed("#A#", image=product_picture("red"))
        self.seed("#C#", shop="C01", image=product_picture("blue"))
        b = self.seed("无码", shop="B01", image=product_picture("green"))
        first = self.analyze()
        self.assertEqual(1, len(self.recommend_calls))
        target = self.group_for(first, a)["id"]
        self.assertEqual(target, self.group_for(first, b)["id"])
        self.service.save_draft(first["id"])
        self.publish("m1")

        m2 = self.second_machine()
        with self.on(m2):
            self.seed("#A#", image=product_picture("red"))
            self.seed("#C#", shop="C01", image=product_picture("blue"))
            self.seed("无码", shop="B01", image=product_picture("green"))
            self.calls, self.recommend_calls = [], []
            self.collect("m2")
            result = self.analyze()
            self.assertEqual(0, len(self.calls))
            self.assertEqual(0, len(self.recommend_calls))
            self.assertEqual(target, self.group_for(result, b)["id"])
            member = self.evidence(result, b)
            self.assertEqual("recommended", member["matching_state"])
            self.assertFalse(self.group_for(result, b)["confirmed"], "推荐不是人工确认")

    def test_manual_decisions_replay_confirmation_and_membership(self):
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        self.result = dict(same=False, confidence=.95, reason="不同主体")
        first = self.analyze()
        target = self.group_for(first, a)["id"]
        self.service.edit_group(first["id"], "move", self.group_for(first, b)["id"], b, target)
        self.service.confirm_groups(first["id"], [target])
        self.service.save_draft(first["id"])
        self.publish("m1")

        m2 = self.second_machine()
        with self.on(m2):
            self.seed("#A#", image=product_picture("red"))
            self.seed("无码", shop="B01", image=product_picture("blue"))
            self.calls, self.recommend_calls = [], []
            self.collect("m2")
            result = self.analyze()
            group = self.group_for(result, b)
            self.assertEqual(target, group["id"], "组 ID 跨机随行")
            self.assertEqual(self.group_for(result, a)["id"], group["id"])
            self.assertTrue(group["confirmed"], "共享的人工确认随行")
            member = self.evidence(result, b)
            self.assertEqual("manual", member["matching_state"])
            self.assertEqual(0, len(self.calls))
            self.assertTrue(DraftStore(self.service.config.store, "m-test").sku_conflicts() == [])

    def test_local_exclusion_beats_incoming_group_and_records_conflict(self):
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        self.result = dict(same=False, confidence=.95, reason="不同主体")
        first = self.analyze()
        self.service.edit_group(first["id"], "move", self.group_for(first, b)["id"], b,
                                self.group_for(first, a)["id"])
        self.service.save_draft(first["id"])
        self.publish("m1")

        m2 = self.second_machine()
        with self.on(m2):
            self.seed("#A#", image=product_picture("red"))
            self.seed("无码", shop="B01", image=product_picture("blue"))
            local = self.analyze()
            self.service.edit_group(local["id"], "exclude", self.group_for(local, b)["id"], b)
            self.service.save_draft(local["id"])          # 本机决定先落账本
            outcome = self.collect("m2")
            (collected,) = outcome.results
            self.assertEqual(1, len(collected.conflicts), "外来组与本机排除矛盾：收取时记冲突")
            self.assertEqual(0, collected.adopted_total, "矛盾行不并进本机账本")
            ledger = DraftStore(self.service.config.store, "m-test").sku_ledger()
            self.assertEqual([], [row for row in ledger["groups"]
                                  if {sku_identity(a), sku_identity(b)}
                                  <= {member for member, _ in row["members"]}],
                             "外来组没有并进本机账本")
            again = self.service.retry_matching(local["id"])
            self.assertTrue(self.group_for(again, b)["excluded_group"], "本机排除原样保留")
            self.assertNotEqual(self.group_for(again, a)["id"], self.group_for(again, b)["id"])
            conflicts = DraftStore(self.service.config.store, "m-test").sku_conflicts()
            self.assertEqual("group_vs_exclusion", conflicts[0].kind)

    def test_replay_protects_legacy_saved_draft_against_shared_row(self):
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        self.result = dict(same=False, confidence=.95, reason="不同主体")
        first = self.analyze()
        self.service.edit_group(first["id"], "move", self.group_for(first, b)["id"], b,
                                self.group_for(first, a)["id"])
        self.service.save_draft(first["id"])
        self.publish("m1")

        m2 = self.second_machine()
        with self.on(m2):
            self.seed("#A#", image=product_picture("red"))
            self.seed("无码", shop="B01", image=product_picture("blue"))
            local = self.analyze()
            drafted = self.service.edit_group(local["id"], "exclude",
                                              self.group_for(local, b)["id"], b)
            # 旧口径草稿：决定只在草稿里、不在账本里（升级前保存的草稿就是这样）。
            payload = copy.deepcopy(drafted)
            payload.update(dirty=False, saved_at="2026-10-09T00:00:00+00:00")
            self.service.store.write_sku(payload)
            self.assertEqual([], DraftStore(self.service.config.store, "m-test").sku_ledger()["groups"])
            self.collect("m2")                             # 外来组进账本（本机账本还没有反话）
            result = self.service.start("2026-09-07", "2026-09-14")
            self.assertTrue(self.group_for(result, b)["excluded_group"],
                            "本机草稿的排除优先，外来组不套上")
            self.assertNotEqual(self.group_for(result, a)["id"], self.group_for(result, b)["id"])
            conflicts = DraftStore(self.service.config.store, "m-test").sku_conflicts()
            self.assertEqual("group_vs_exclusion", conflicts[0].kind, "回放期矛盾落到冲突账")

    def test_split_and_mixed_code_decisions_replay_across_machines(self):
        left = self.seed("#A#")
        right = self.seed("#A#", shop="B01")
        first = self.analyze()
        self.service.edit_group(first["id"], "remove", self.group_for(first, right)["id"], right)
        self.service.save_draft(first["id"])
        other = self.seed("#B#", shop="C01")
        second = self.analyze()
        self.service.edit_group(second["id"], "move", self.group_for(second, other)["id"], other,
                                self.group_for(second, left)["id"])
        self.service.save_draft(second["id"])
        self.publish("m1")
        left_group = self.group_for(second, left)["id"]

        m2 = self.second_machine()
        with self.on(m2):
            self.seed("#A#")
            self.seed("#A#", shop="B01")
            self.seed("#B#", shop="C01")
            self.collect("m2")
            result = self.analyze()
            self.assertEqual(left_group, self.group_for(result, left)["id"])
            self.assertEqual(self.group_for(result, left)["id"],
                             self.group_for(result, other)["id"], "混合编码人工组随行")
            self.assertNotEqual(self.group_for(result, right)["id"],
                                self.group_for(result, left)["id"], "人工拆组不被同码规则合回")
            self.assertEqual({"A", "B"},
                             set(self.group_for(result, left)["merchant_codes"]))

    def test_a_partially_applied_shared_group_extends_when_members_arrive(self):
        """审查 F1：同一共享行先按在场子集应用、成员后到齐——补员进同一组，不记假冲突。"""
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        self.result = dict(same=False, confidence=.95, reason="不同主体")
        first = self.analyze()
        target = self.group_for(first, a)["id"]
        self.service.edit_group(first["id"], "move", self.group_for(first, b)["id"], b, target)
        self.service.save_draft(first["id"])
        c = self.seed("无码二", shop="C01", image=product_picture("green"))
        second = self.analyze()
        self.service.edit_group(second["id"], "move", self.group_for(second, c)["id"], c,
                                self.group_for(second, a)["id"])
        self.service.save_draft(second["id"])
        self.publish("m1")

        m2 = self.second_machine()
        with self.on(m2):
            self.seed("#A#", image=product_picture("red"))
            self.seed("无码", shop="B01", image=product_picture("blue"))
            self.collect("m2")
            partial = self.analyze()                    # c 还没到：按在场子集先应用
            self.assertEqual(self.group_for(partial, a)["id"], self.group_for(partial, b)["id"])
            self.service.save_draft(partial["id"])
            self.seed("无码二", shop="C01", image=product_picture("green"))
            result = self.analyze()                     # c 到齐：补进同一组
            self.assertEqual(target, self.group_for(result, c)["id"], "同族扩员：组 ID 不变")
            self.assertEqual(self.group_for(result, a)["id"], self.group_for(result, c)["id"])
            self.assertEqual([], DraftStore(self.service.config.store, "m-test").sku_conflicts(),
                             "同族补员不是矛盾，不记冲突")

    def test_shared_group_with_changed_evidence_keeps_membership_unconfirmed(self):
        """共享行证据对不上：保留原归属、整组退回未确认并标注（票面第 4 条后半跨机适用）。"""
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        self.result = dict(same=False, confidence=.95, reason="不同主体")
        first = self.analyze()
        target = self.group_for(first, a)["id"]
        self.service.edit_group(first["id"], "move", self.group_for(first, b)["id"], b, target)
        self.service.confirm_groups(first["id"], [target])
        self.service.save_draft(first["id"])
        self.publish("m1")

        m2 = self.second_machine()
        with self.on(m2):
            self.seed("#A#", image=product_picture("red"))
            self.seed("改过名字的无码", shop="B01", image=product_picture("blue"))
            self.collect("m2")
            result = self.analyze()
            group = self.group_for(result, b)
            self.assertEqual(self.group_for(result, a)["id"], group["id"], "保留原归属")
            self.assertFalse(group["confirmed"], "证据对不上：整组退回未确认")
            self.assertIn("与共享决定时的证据不一致",
                          self.evidence(result, b)["evidence_changes"])
            self.assertEqual([], DraftStore(self.service.config.store, "m-test").sku_conflicts())

    def test_evidence_change_is_not_reused_and_only_completed_rows_are_shared(self):
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("green"))
        def unavailable(content):
            raise _ModelFailure("模型请求失败")
        self.result = unavailable
        failed = self.analyze()
        self.assertEqual(1, len(self.calls))
        self.service.save_draft(failed["id"])
        empty = self.publish("m1")
        self.assertFalse(empty.published, "失败的调用不进包")
        self.assertIn("空", empty.note)
        self.result = dict(same=True, confidence=.95, reason="主体一致")
        retried = self.service.retry_matching(failed["id"])
        self.service.save_draft(failed["id"])
        published = self.publish("m1")
        self.assertTrue(published.published, published.failure)
        self.assertEqual(1, published.rows["sku_judgments"], "只有已完成的结果进包")

        m2 = self.second_machine()
        with self.on(m2):
            self.seed("#A#", image=product_picture("red"))
            self.seed("改过名字的无码", shop="B01", image=product_picture("green"))
            self.calls, self.recommend_calls = [], []
            self.collect("m2")
            self.analyze()
            self.assertEqual(1, len(self.calls), "证据变了不命中共享判断：重新判一次")


class CrossMachineBrowserTests(CrossMachineCase):
    """真实页面验收（票 08）：经交换台收取后，SKU 确认页看到归属、推荐理由、多候选与确认状态。"""

    def test_page_after_collect_shows_grouping_reason_alternatives_and_state(self):
        import os
        from playwright.sync_api import expect, sync_playwright
        from test_analysis import serve
        self.seed("#A#", image=product_picture("red"))
        self.seed("#C#", shop="C01", image=product_picture("blue"))
        b = self.seed("无码", shop="B01", image=product_picture("green"))
        self.recommend_result = lambda context: dict(
            group_id=context["candidates"][0]["id"], reason="共享来的推荐理由：杯口与底座证据更完整")
        first = self.analyze()
        first_group = self.group_for(first, b)["id"]
        self.service.save_draft(first["id"])
        self.assertTrue(sku_sharing.publish(self.exchange, "m1",
                                            cache=self.service.config.matching.cache,
                                            store=self.service.config.store, now=STAMP).published)

        m2 = self.second_machine()
        with self.on(m2):
            self.seed("#A#", image=product_picture("red"))
            self.seed("#C#", shop="C01", image=product_picture("blue"))
            self.seed("无码", shop="B01", image=product_picture("green"))
            self.calls, self.recommend_calls = [], []
            outcome = sku_sharing.collect(self.exchange, "m2",
                                          cache=self.service.config.matching.cache,
                                          store=self.service.config.store, now=STAMP)
            self.assertFalse(outcome.failed)
            self.server, self.thread = serve(self.service)
            self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(),
                                     self.thread.join()))
            pw = sync_playwright().start()
            self.addCleanup(pw.stop)
            browser = pw.chromium.launch(channel="msedge", headless=True)
            self.addCleanup(browser.close)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{self.server.server_port}")
            page.locator("#start").fill("2026-09-07")
            page.locator("#end").fill("2026-09-14")
            page.locator("#begin").click()
            page.locator("#skuQuery").fill("B01")
            card = page.locator(".sku-evidence-card").filter(has_text="原始 SKU 名称：无码")
            expect(card).to_contain_text("共享来的推荐理由：杯口与底座证据更完整")
            expect(card).to_contain_text("其他候选")
            expect(card).to_contain_text("多候选推荐")
            expect(page.locator(".group-status").first).to_have_text("待确认")
            self.assertEqual(0, len(self.calls), "共享的判断直接命中，页面不再付费")
            self.assertEqual(0, len(self.recommend_calls))
            target = os.environ.get("SKU08_ARTIFACTS")
            if target:
                path = Path(target)
                path.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(path / "collected-recommendation-on-page.png"),
                                full_page=True)
            again = self.analyze()
            self.assertEqual(first_group, self.group_for(again, b)["id"],
                             "页面背后的公开快照与发布时同组")
            self.assertEqual(0, len(self.calls), "页面与再次分析都不再付费")


class LegacyCoexistenceTests(SkuSharingCase):
    """与已部署旧版本共存（实测真实旧代码）：旧通道对新文件不读不写、不静默误用。"""

    def setUp(self):
        super().setUp()
        self.box = GitSandbox(self)
        self.world = _take_world(self.box, "judged-m1")
        self.repo = self.world["exchange"]
        self.exchange = self.repo.parent

    def seed_legacy_cache(self, cache, machine="m1"):
        """按规格预置一行旧判断（够发布器有内容可发；本测试不消费它）。

        `pair` 要按程序的口径铸（两端证据版本的摘要）——issue 06 票 04 的自洽门禁
        会逐行验它，拿假串当 pair 的包属于「坏包」，整包会被拒。
        """
        from bestseller_monitor.matching import digest, prepare_cache
        conn = sqlite3.connect(cache)
        try:
            with conn:
                prepare_cache(conn, machine)
                conn.execute("INSERT OR REPLACE INTO judgments(pair,signature,machine_id,"
                             "evidence_a,evidence_b,result) VALUES(?,?,?,?,?,?)",
                             (digest(sorted(["ev-a", "ev-b"])), "sig-legacy", machine,
                              "ev-a", "ev-b",
                              json.dumps({"same": True, "confidence": .9}, ensure_ascii=False)))
        finally:
            conn.close()

    def test_the_legacy_channel_code_is_bit_identical_to_main(self):
        """旧版本实测用的就是真实旧代码：旧通道文件与 main（已部署）逐字节一致。"""
        import bestseller_monitor
        root = Path(bestseller_monitor.__file__).resolve().parents[1]
        # 这一步有意读到沙箱外、对本检出（真实仓库）跑 git：沙箱屏蔽的本机 git 配置
        # 里正有它成立靠的两样——系统层的 core.autocrlf（CRLF 工作树按它归一到 LF）
        # 与全局层的 safe.directory（属主校验，主仓 .git 属沙箱账号）；命令级只放行
        # 这两项，另把宿主可能泄漏的 GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE 滤掉（否则
        # 比较会被引到别的仓库/索引），其余仍走沙箱口径。
        env = {key: value for key, value in os.environ.items()
               if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        for ref in ("main", "origin/main"):     # 功能树里有本地 main；隔离副本克隆里是 origin/main
            done = subprocess.run(["git", "-C", str(root), "-c", "safe.directory=*",
                                   "-c", "core.autocrlf=true",
                                   "diff", "--quiet", ref, "--",
                                   "bestseller_monitor/judgment_set.py"],
                                  capture_output=True, env=env)
            if done.returncode in (0, 1):
                break
        # 两条失败消息都带上实际比过的 ref、rc 与 git 的输出：rc=1（真差异，--quiet
        # 无输出）与访问错误（stderr 有话说）由此分辨得出。
        git_output = done.stderr.decode("utf-8", "replace").strip() or "无"
        self.assertIn(done.returncode, (0, 1),
                      f"找不到 main/origin/main 作比较基准（最后试 {ref}：rc={done.returncode}，"
                      f"git 输出：{git_output}）")
        self.assertEqual(0, done.returncode,
                         f"judgment_set.py 与已部署（{ref}）有差异，先确认部署版本再谈旧版本实测"
                         f"（rc={done.returncode}，git 输出：{git_output}）")

    def test_legacy_publish_and_collect_leave_the_new_file_untouched(self):
        from bestseller_monitor import judgment_set
        legacy_cache = self.root / "legacy-cache.sqlite"
        legacy_store = self.root / "legacy-drafts.sqlite"
        self.seed_legacy_cache(legacy_cache)
        sku_cache, sku_store = self.root / "sku-cache.sqlite", self.root / "sku-drafts.sqlite"
        self.seed_judgment("pair-sku", "sig-sku", {"same": True, "confidence": .95},
                           machine_id="m1", cache=sku_cache)
        sku = sku_sharing.publish(self.exchange, "m1", cache=sku_cache, store=sku_store, now=STAMP)
        self.assertTrue(sku.published, sku.failure)
        legacy = judgment_set.publish(self.exchange, "m1", cache=legacy_cache,
                                      store=legacy_store, now=STAMP)
        self.assertTrue(legacy.published, legacy.failure)
        sku_bytes = git_bytes(["show", f"HEAD:{sku_sharing.SKU_PACKAGE_REL}"], cwd=self.repo)

        # 旧版本程序再发布一次（有新内容）：只动它自己的文件，新文件一个字节不改。
        self.seed_legacy_cache(legacy_cache, machine="m1")
        from bestseller_monitor.matching import digest as _digest
        with closing(sqlite3.connect(legacy_cache)) as conn:
            conn.execute("INSERT OR REPLACE INTO judgments(pair,signature,machine_id,"
                         "evidence_a,evidence_b,result) VALUES(?,?,?,?,?,?)",
                         (_digest(sorted(["ev-c", "ev-d"])), "sig-legacy", "m1", "ev-c", "ev-d",
                          json.dumps({"same": False, "confidence": .9}, ensure_ascii=False)))
            conn.commit()
        republish = judgment_set.publish(self.exchange, "m1", cache=legacy_cache,
                                         store=legacy_store, now=STAMP)
        self.assertTrue(republish.published and not republish.unchanged, republish.failure)
        self.assertEqual(sku_bytes, git_bytes(["show", f"HEAD:{sku_sharing.SKU_PACKAGE_REL}"],
                                              cwd=self.repo),
                         "旧版本发布整文件覆盖的只是它自己那条路径")

        # 旧版本的另一台机器来收：只收旧判断；不会触碰（也读不懂）新文件。
        m2_cache, m2_store = self.root / "m2-cache.sqlite", self.root / "m2-drafts.sqlite"
        outcome = judgment_set.collect(self.exchange, "m2", cache=m2_cache, store=m2_store)
        self.assertFalse(outcome.failed,
                         [(r.machine, r.failure) for r in outcome.results] or outcome.failure)
        (collected,) = outcome.results
        self.assertFalse(collected.failed, collected.failure)
        self.assertGreaterEqual(collected.added["judgments"], 1)
        with closing(sqlite3.connect(m2_cache)) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("sku_judgments", tables, "旧收取不建也不碰 SKU 表")
        self.assertNotIn("sku_imports", tables)
        if m2_store.exists():
            ledger = DraftStore(m2_store, "m-test")
            self.assertEqual([], ledger.sku_ledger()["groups"])
            self.assertEqual([], ledger.sku_conflicts())

