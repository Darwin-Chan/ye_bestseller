"""票 03：判断集的打包与搬运（模型判断侧）。

接缝（都走公开口）：

- **打包与包身份**：`judgment_set.build_package` / `package_digest` / `read_package_meta`
  ——临时缓存库进、包文件出，不碰 git；视觉描述行按规格「预置缓存行」直接写进库。
- **发布与收取**：`judgment_set.publish` / `collect`——真 git（本地裸库当远端、克隆当机器，
  `tests/git_repos` 的样例台）、真缓存库、真导入账。
- **消费侧**：收进来的判断直接命中（零模型调用）走 `MatchingService.suggest` 这个既有
  服务层口，外部只替换模型传输（`test_matching` 的 `ModelTransport`）。
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bestseller_monitor import export, merge
from bestseller_monitor.db import CST, Database, connect
from bestseller_monitor.judgment_set import (FORMAT_VERSION, META_TABLE, PACKAGE_REL,
                                             build_package, collect, package_digest, publish,
                                             read_package_meta)
from bestseller_monitor.matching import (STATUS_CACHE, STATUS_MODEL, MatchingConfig,
                                         MatchingService, ModelConfig, prepare_cache)
from tests.git_repos import GitSandbox
from helpers import submit_offer
from test_exchange import FakeImageStore
from test_matching import ModelTransport, product, singles


def git_bytes(args, **kwargs) -> bytes:
    """真跑一次 git 并拿**字节**（包的二进制内容经 `must()` 的文本解码会坏掉）。"""
    done = subprocess.run(["git", *args], capture_output=True,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C"), **kwargs)
    if done.returncode != 0:
        raise AssertionError(f"样例台 git 失败：git {' '.join(args)}\n{done.stderr!r}")
    return done.stdout


# 固定时刻：包上的生成时刻不进身份，怎么取都不该影响摘要（取值本身只求可复现）。
STAMP = dt.datetime(2026, 9, 22, 21, 0, tzinfo=CST)
# 采集侧的世界：观测落在 W38 第一天（导出与汇总那两条用例要用周窗口）。
DAY = "2026-09-14"
WEEK = "2026-W38"


class JudgmentSetCase(unittest.TestCase):
    """共用夹具：真缓存库（判断行由真服务产生）+ 真模型传输替身。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "matching.sqlite"
        self.transport = ModelTransport()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret-value"}).start()
        patch("bestseller_monitor.matching.urlopen", side_effect=self.transport).start()

    def judge_products(self, *, cache=None, machine_id="m1", model="deepseek-chat", same=True,
                       colors=("red", "blue")):
        """用真服务判一批商品：缓存里落下判断行与成员证据行（返回这批商品）。"""
        products = [product(number, color, "月牙杯")
                    for number, color in enumerate(colors, start=1)]
        self.transport.decisions[("月牙杯", "月牙杯")] = same
        config = MatchingConfig(cache or self.cache, ModelConfig(model=model), mode="direct")
        MatchingService(config, machine_id=machine_id).suggest(products, singles(products))
        return products

    def seed_visual_evidence(self, image_hash, description, model="vision-model", *, cache=None):
        """视觉描述缓存行：真从 caption 模式产生要另配视觉服务，这里按规格预置行。"""
        with closing(sqlite3.connect(cache or self.cache)) as conn, conn:
            prepare_cache(conn, "m4")           # 缓存库可能还没建（一次分析都没跑过）
            conn.execute("INSERT OR REPLACE INTO visual_evidence VALUES (?,?,?)",
                         (image_hash, description, model))


class PackageTests(JudgmentSetCase):
    """打包：三类表、剥掉图片字节、身份是内容不是时刻与打包者。"""

    def build(self, package_name="judgments.db", *, cache=None, machine_id="m1",
              generated_at=STAMP):
        package = self.root / package_name
        build_package(cache or self.cache, package, machine_id=machine_id,
                      generated_at=generated_at)
        return package

    def test_the_package_carries_the_three_judgment_tables_and_leaves_the_rest_out(self):
        self.judge_products()
        self.seed_visual_evidence("hash-1", "一只月牙形的杯子")

        package = self.build()

        with closing(sqlite3.connect(package)) as conn:
            tables = {row[0] for row in
                      conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"judgments", "visual_evidence", "evidence", META_TABLE})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM visual_evidence").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT description FROM visual_evidence").fetchone()[0],
                "一只月牙形的杯子")
            # 判断行原样带走：署名与来源都在里面（消费规则的一部分）。
            self.assertEqual(
                conn.execute("SELECT machine_id FROM judgments").fetchone()[0], "m1")
        with closing(sqlite3.connect(self.cache)) as conn:
            recommendations = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
        self.assertGreater(recommendations, 0)      # 本机缓存里有它——只写不读，不搬

    def test_the_package_table_shapes_match_the_local_cache(self):
        """包内表的形状跟着本地缓存走：证据索引只少一列图片字节，其余逐列一致。

        规格 §2：「表结构与本地同名表一致（DDL 同一来源）」——这里是那份一致性的守门用例：
        本地缓存改列而包没跟上时，这一条先红。
        """
        self.judge_products()

        package = self.build()

        with closing(sqlite3.connect(package)) as conn:
            packed = {name: [(row[1], row[2], row[3], row[5]) for row in
                             conn.execute(f"PRAGMA table_info({name})")]
                      for name in ("judgments", "visual_evidence", "evidence")}
        with closing(sqlite3.connect(self.cache)) as conn:
            local = {name: [(row[1], row[2], row[3], row[5]) for row in
                            conn.execute(f"PRAGMA table_info({name})")]
                     for name in ("judgments", "visual_evidence", "evidence")}
        self.assertEqual(packed["judgments"], local["judgments"])
        self.assertEqual(packed["visual_evidence"], local["visual_evidence"])
        self.assertEqual(packed["evidence"],
                         [column for column in local["evidence"] if column[0] != "image_data"])

    def test_meta_records_who_packed_it_when_and_what_is_inside(self):
        self.judge_products()

        package = self.build()

        with closing(sqlite3.connect(package)) as conn:
            meta = read_package_meta(conn)
        self.assertEqual(meta["machine_id"], "m1")
        self.assertEqual(meta["generated_at"], STAMP.isoformat(timespec="seconds"))
        self.assertEqual(meta["format_version"], FORMAT_VERSION)
        self.assertEqual(meta["rows"],
                         {"judgments": 1, "visual_evidence": 0, "evidence": 2})

    def test_the_identity_is_the_content_not_the_packer_the_moment_nor_the_layout(self):
        self.judge_products()
        first = self.build("one.db", machine_id="m1", generated_at=STAMP)

        # 同一份内容在别处重打（换打包机器、换时刻、换库里行的写入次序）得到同一摘要——
        # 身份是内容：摘要把行按列排序后再算，重打包与另一台机器都复现得出来。
        elsewhere = self.rewritten_copy_of_cache()
        second = self.build("two.db", cache=elsewhere, machine_id="m9",
                            generated_at=STAMP + dt.timedelta(hours=3))

        self.assertEqual(self.digest_of(first), self.digest_of(second))

    def test_new_content_changes_the_identity(self):
        self.judge_products()
        before = self.build("before.db")

        # 再判一件新商品（多两条判断）：内容变了，身份跟着变。
        self.transport.decisions[("月牙杯", "月牙杯")] = False
        self.judge_products(cache=self.cache, colors=("red", "blue", "green"))
        after = self.build("after.db")

        self.assertNotEqual(self.digest_of(before), self.digest_of(after))

    def digest_of(self, package: Path) -> str:
        with closing(sqlite3.connect(package)) as conn:
            return package_digest(conn)

    def rewritten_copy_of_cache(self) -> Path:
        """同一份内容、不同写入次序的缓存：三张表的行各自反序重写一遍。"""
        copy = self.root / "rewritten.sqlite"
        copy.write_bytes(self.cache.read_bytes())
        with closing(sqlite3.connect(copy)) as conn:
            for table in ("judgments", "evidence", "visual_evidence"):
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                rows = conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
                conn.execute(f"DELETE FROM {table}")
                conn.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(columns))})",
                                 list(reversed(rows)))
            conn.commit()
        return copy



class PublishTests(JudgmentSetCase):
    """发布：判过 → 发到 `judged-<机器>`；同内容重发是空操作；新版覆盖同名路径。"""

    def setUp(self):
        super().setUp()
        self.box = GitSandbox(self, prefix="bestseller-judged-")
        self.exchange = self.box.tmp / "exchange"
        self.exchange.mkdir()
        self.remote = self.box.new_remote("judged-m4.git")
        self.repo = self.exchange / "judged-m4"
        self.box.must("clone", str(self.remote), str(self.repo))

    def publish(self, **kwargs):
        kwargs.setdefault("machine_id", "m4")
        kwargs.setdefault("cache", self.cache)
        return publish(self.exchange, now=STAMP, **kwargs)

    def read_published(self, rel=PACKAGE_REL, rev="HEAD"):
        """从远端（经一个干净克隆）取回包并打开它；`rev` 取历史某一版。"""
        check = self.box.clone(self.remote, f"check-{len(list(self.box.tmp.glob('check-*')))}")
        path = self.box.tmp / f"published-{rev.replace('~', '-')}.db"
        path.write_bytes(gzip.decompress(git_bytes(
            ["-C", str(check), "show", f"{rev}:{rel}"])))
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        return conn

    def commits(self) -> int:
        check = self.box.clone(self.remote, f"counter-{len(list(self.box.tmp.glob('counter-*')))}")
        return int(self.box.must("rev-list", "--count", "HEAD", cwd=check).strip())

    def test_publish_lands_the_package_in_the_machine_repo(self):
        self.judge_products(machine_id="m4")

        result = self.publish()

        self.assertTrue(result.published, result.failure)
        self.assertFalse(result.unchanged)
        self.assertFalse(result.failed)
        self.assertEqual(result.machine_id, "m4")
        self.assertEqual(result.package_rel, "judged-m4/data/judgments.db.gz")
        self.assertEqual(result.rows["judgments"], 1)
        package = self.read_published()
        self.assertEqual(read_package_meta(package)["machine_id"], "m4")
        self.assertEqual(package.execute("SELECT COUNT(*) FROM judgments").fetchone()[0], 1)
        self.assertEqual(package_digest(package), result.digest)
        check = self.box.clone(self.remote, "one-commit")
        self.assertEqual(result.commit,
                         self.box.must("rev-parse", "--short", "HEAD", cwd=check).strip(),
                         "结果里记的提交就是远端收到的那笔")
        self.assertEqual(self.commits(), 1)

    def test_publishing_the_same_content_again_makes_no_extra_commit(self):
        self.judge_products(machine_id="m4")
        first = self.publish()

        second = self.publish()

        self.assertTrue(second.published, "包仍在远端（这次只是确认了一遍）")
        self.assertTrue(second.unchanged, "同内容重发：不写文件、不提交、不推送")
        self.assertEqual(second.commit, first.commit, "还是那一笔提交")
        self.assertFalse(second.failed)
        self.assertEqual(self.commits(), 1)

    def test_new_judgments_land_as_a_new_commit_and_the_old_version_stays(self):
        self.judge_products(machine_id="m4")
        first = self.publish()

        self.transport.decisions[("月牙杯", "月牙杯")] = False
        self.judge_products(cache=self.cache, colors=("red", "blue", "green"), machine_id="m4")
        second = self.publish()

        self.assertTrue(second.published, second.failure)
        self.assertFalse(second.unchanged)
        self.assertNotEqual(second.digest, first.digest)
        self.assertEqual(self.commits(), 2)
        self.assertEqual(package_digest(self.read_published()), second.digest)
        self.assertEqual(package_digest(self.read_published(rev="HEAD~1")), first.digest,
                         "旧版留在 git 历史里，不在工作树里堆批次文件")

    def test_an_unreadable_published_package_is_overwritten(self):
        """远端那份读不成判断集（谁手工塞了个别的文件）：照常覆盖发布，不是停下来报错。"""
        self.judge_products(machine_id="m4")
        self.box.commit_push(self.repo, {PACKAGE_REL: b"not a judgment set at all"})

        result = self.publish()

        self.assertTrue(result.published, result.failure)
        self.assertFalse(result.unchanged)
        self.assertEqual(read_package_meta(self.read_published())["rows"]["judgments"], 1)

    def test_an_uncloned_judged_repo_names_the_step_to_take(self):
        self.judge_products(machine_id="m4")
        shutil.rmtree(self.repo)

        result = self.publish()

        self.assertTrue(result.failed)
        self.assertFalse(result.published)
        self.assertIn(str(self.repo), result.failure)
        self.assertIn("judged", result.failure)

    def test_a_machine_with_an_empty_cache_publishes_nothing(self):
        result = self.publish(cache=self.root / "never-ran.sqlite")

        self.assertFalse(result.failed, "缓存全空不是失败，是可读的一句说明")
        self.assertFalse(result.published)
        self.assertIn("空", result.note)

    def test_a_cache_with_captions_but_no_judgments_still_publishes(self):
        """发布的门槛是「三类表全空」：只有视觉描述与证据索引也照发（caption 模式省得下）。"""
        self.seed_visual_evidence("hash-1", "一只月牙形的杯子")

        result = self.publish()

        self.assertTrue(result.published, result.note or result.failure)
        self.assertEqual(read_package_meta(self.read_published())["rows"],
                         {"judgments": 0, "visual_evidence": 1, "evidence": 0})

    def test_a_rejected_push_cleans_the_clone_and_the_next_run_heals(self):
        self.judge_products(machine_id="m4")
        counter = self.box.tmp / "hook-runs"
        self.box.install_declining_hook(self.repo, counter)

        first = self.publish()

        self.assertTrue(first.failed)
        self.assertFalse(first.published)
        self.assertEqual(self.box.must("status", "--porcelain", cwd=self.repo).strip(), "",
                         "推送失败后克隆要退回远端状态（没有半截改动）")

        self.box.remove_pre_push(self.repo)
        second = self.publish()

        self.assertTrue(second.published, second.failure)
        self.assertTrue((self.exchange / "judged-m4" / PACKAGE_REL).exists())


class CollectTests(JudgmentSetCase):
    """收取：把别的机器的判断集收进本机缓存；幂等；缺库/没发布只给可读说明。"""

    def setUp(self):
        super().setUp()
        self.box = GitSandbox(self, prefix="bestseller-judged-")
        self.exchange = self.box.tmp / "exchange"
        self.exchange.mkdir()

    def publish_from(self, machine, *, cache=None, products=("red", "blue"), same=True):
        """另一台机器判一批商品并发布：真仓库（裸库当远端 + 一个克隆）、真打包。"""
        remote = self.box.new_remote(f"judged-{machine}.git")
        repo = self.exchange / f"judged-{machine}"
        self.box.must("clone", str(remote), str(repo))
        cache = cache or (self.root / f"{machine}-cache.sqlite")
        self.transport.decisions[("月牙杯", "月牙杯")] = same
        self.judge_products(cache=cache, machine_id=machine, colors=products)
        result = publish(self.exchange, machine, cache, now=STAMP)
        self.assertTrue(result.published, result.failure)
        return result

    def collect(self, machine_id="m4", *, cache=None):
        return collect(self.exchange, machine_id, cache or self.cache)

    def dump(self, cache=None):
        """缓存里三类表 + 导入账的全部行：幂等断言比这个（比计数严）。"""
        with closing(sqlite3.connect(cache or self.cache)) as conn:
            return {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1, 2").fetchall()
                    for name in ("judgments", "evidence", "visual_evidence", "judgment_imports")}

    def test_collect_imports_the_other_machines_judgments_and_records_the_ledger(self):
        published = self.publish_from("m1")

        outcome = self.collect()

        self.assertEqual([r.machine for r in outcome.results], ["m1"])
        result = outcome.results[0]
        self.assertFalse(result.failed, result.failure)
        self.assertFalse(result.skipped)
        self.assertEqual(result.package_rel, "judged-m1/data/judgments.db.gz")
        self.assertEqual(result.digest, published.digest)
        self.assertEqual(result.rows, {"judgments": 1, "visual_evidence": 0, "evidence": 2})
        self.assertEqual(result.added, {"judgments": 1, "visual_evidence": 0, "evidence": 2})
        with closing(sqlite3.connect(self.cache)) as conn:
            self.assertEqual(conn.execute("SELECT machine_id FROM judgments").fetchone()[0],
                             "m1", "收进来的判断带来源机器（只作显示，不参与键）")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 2)
            ledger = conn.execute(
                "SELECT digest, source_machine FROM judgment_imports").fetchall()
        self.assertEqual(ledger, [(published.digest, "m1")])

    def test_collecting_the_same_content_again_changes_nothing(self):
        published = self.publish_from("m1")
        first = self.collect()
        before = self.dump()

        second = self.collect()

        self.assertEqual(self.dump(), before, "重复收取：账本、缓存一个字不变")
        replay = second.results[0]
        self.assertTrue(replay.skipped, "同内容已收过：这次什么都没做")
        self.assertFalse(replay.failed)
        self.assertEqual(replay.digest, published.digest)
        self.assertEqual(replay.rows, first.results[0].rows, "计数从账里回放")
        self.assertEqual(replay.added, first.results[0].added)

    def test_rows_the_machine_already_has_keep_their_own_source(self):
        self.judge_products(machine_id="m4")                     # 本机自己先判过这一对
        self.publish_from("m1", cache=self.root / "m1-cache.sqlite")

        self.collect()

        with closing(sqlite3.connect(self.cache)) as conn:
            sources = [row[0] for row in conn.execute("SELECT machine_id FROM judgments")]
        self.assertEqual(sources, ["m4"], "先到的那条留着：导入不覆盖本机已有的判断")

    def test_collected_judgments_hit_the_cache_and_only_new_products_are_judged(self):
        self.publish_from("m1")
        self.collect()
        config = MatchingConfig(self.cache, ModelConfig(model="deepseek-chat"), mode="direct")
        shared = [product(1, "red", "月牙杯"), product(2, "blue", "月牙杯")]

        matches = len(self.transport.calls)
        groups = MatchingService(config, machine_id="m4").suggest(shared, singles(shared))

        self.assertEqual(self.comparisons_since(matches), 0, "同批证据版本零调用命中")
        self.assertEqual([p["matching_status"] for p in shared], [STATUS_CACHE, STATUS_CACHE])
        self.assertEqual(sorted(len(g["members"]) for g in groups), [2],
                         "收进来的判断照常驱动本机分组")

        fresh = shared + [product(3, "green", "月牙杯")]           # 本机新增的商品照常判
        self.transport.decisions[("月牙杯", "月牙杯")] = False      # 本机模型判它不同款

        matches = len(self.transport.calls)
        groups = MatchingService(config, machine_id="m4").suggest(fresh, singles(fresh))

        self.assertEqual(self.comparisons_since(matches), 2,
                         "新增商品的两个新对判了；共享的那对照旧零调用")
        self.assertEqual(fresh[2]["matching_status"], STATUS_MODEL)
        self.assertEqual(sorted(len(g["members"]) for g in groups), [1, 2])

    def comparisons_since(self, index) -> int:
        return len([call for call in self.transport.calls[index:]
                    if "Compare the same" in call["messages"][0]["content"]])

    def test_two_sources_with_the_same_content_are_collected_once(self):
        """同一趟里两家内容相同（纯汇总机收下再原样转发）：第二家如实算「已经收过」。"""
        self.publish_from("m1")
        relay = self.root / "m2-cache.sqlite"
        collect(self.exchange, "m2", relay)                     # m2 先收下 m1 那份
        remote = self.box.new_remote("judged-m2.git")
        self.box.must("clone", str(remote), str(self.exchange / "judged-m2"))
        republished = publish(self.exchange, "m2", relay, now=STAMP)
        self.assertTrue(republished.published, republished.failure)

        outcome = self.collect()

        by_machine = {result.machine: result for result in outcome.results}
        self.assertEqual(sorted(by_machine), ["m1", "m2"])
        self.assertFalse(by_machine["m1"].skipped, "第一份照常导进来")
        self.assertTrue(by_machine["m2"].skipped, "同内容：第二家不算一次新导入")
        self.assertFalse(outcome.failed, [r.failure for r in outcome.results])
        self.assertEqual(by_machine["m2"].digest, by_machine["m1"].digest)
        with closing(sqlite3.connect(self.cache)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM judgment_imports").fetchone()[0], 1,
                "两个来源、一份内容：导入账只有一行")

    def test_the_status_lines_match_the_machine_that_published(self):
        """A 判过的那批：B 收下来跑，与 A 自己重跑逐字段一致（状态行与分组都算）。"""
        self.publish_from("m1")
        self.collect()

        def config_of(cache):
            return MatchingConfig(cache, ModelConfig(model="deepseek-chat"), mode="direct")

        shared_b = [product(1, "red", "月牙杯"), product(2, "blue", "月牙杯")]
        shared_a = [product(1, "red", "月牙杯"), product(2, "blue", "月牙杯")]
        groups_b = MatchingService(config_of(self.cache), machine_id="m4").suggest(
            shared_b, singles(shared_b))
        groups_a = MatchingService(config_of(self.root / "m1-cache.sqlite"), machine_id="m1").suggest(
            shared_a, singles(shared_a))

        self.assertEqual([(p["offer_id"], p["matching_status"], p["matching_state"]) for p in shared_b],
                         [(p["offer_id"], p["matching_status"], p["matching_state"]) for p in shared_a])
        self.assertEqual(sorted(sorted(m["offer_id"] for m in g["members"]) for g in groups_b),
                         sorted(sorted(m["offer_id"] for m in g["members"]) for g in groups_a))

    def test_a_source_that_has_not_published_anything_is_a_readable_note(self):
        remote = self.box.new_remote("judged-m2.git")
        self.box.must("clone", str(remote), str(self.exchange / "judged-m2"))

        outcome = self.collect()

        result = outcome.results[0]
        self.assertFalse(result.failed)
        self.assertIn("还没发布", result.note)
        self.assertEqual(self.dump()["judgment_imports"], [])

    def test_a_source_whose_repo_is_not_cloned_is_a_readable_note(self):
        (self.exchange / "judged-m3").mkdir()

        outcome = self.collect()

        result = outcome.results[0]
        self.assertFalse(result.failed)
        self.assertIn("clone", result.note)

    def test_no_sources_at_all_says_so_readably(self):
        outcome = self.collect()

        self.assertEqual(outcome.results, ())
        self.assertTrue(outcome.notes)
        self.assertIn("judged", outcome.notes[0])

    def test_the_machines_own_judged_repo_is_not_collected_for_itself(self):
        self.publish_from("m4", cache=self.cache)                # 本机自己发布的那本

        outcome = self.collect()

        self.assertEqual([r.machine for r in outcome.results], [], "自己那本不收：本机缓存就是那份")

    def test_a_broken_source_does_not_stop_the_others(self):
        self.publish_from("m1")
        broken = self.exchange / "judged-m2"
        self.box.must("clone", str(self.box.new_remote("judged-m2.git")), str(broken))
        self.box.must("remote", "set-url", "origin", str(self.box.tmp / "gone.git"), cwd=broken)

        outcome = self.collect()

        by_machine = {r.machine: r for r in outcome.results}
        self.assertTrue(by_machine["m2"].failed)
        self.assertIn("拉不到", by_machine["m2"].failure)
        self.assertFalse(by_machine["m1"].failed)
        self.assertFalse(by_machine["m1"].skipped)
        with closing(sqlite3.connect(self.cache)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0], 1,
                             "坏的那家不挡别家：m1 的判断照常收进来")

    def test_a_corrupt_package_leaves_the_cache_untouched(self):
        remote = self.box.new_remote("judged-m1.git")
        work = self.box.clone(remote, "work-m1")
        self.box.commit_push(work, {PACKAGE_REL: b"not a package at all"})
        self.box.must("clone", str(remote), str(self.exchange / "judged-m1"))

        outcome = self.collect()

        result = outcome.results[0]
        self.assertTrue(result.failed)
        self.assertIn("judgments.db.gz", result.failure)
        with closing(sqlite3.connect(self.cache)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM judgment_imports").fetchone()[0], 0)

    def test_collect_creates_the_cache_on_a_machine_that_never_ran_an_analysis(self):
        published = self.publish_from("m1")
        fresh = self.root / "never-ran.sqlite"

        outcome = self.collect(cache=fresh)

        self.assertFalse(outcome.results[0].failed, outcome.results[0].failure)
        with closing(sqlite3.connect(fresh)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT digest FROM judgment_imports").fetchone()[0],
                             published.digest)


class ChannelIndependenceTests(JudgmentSetCase):
    """两条通道各自独立：判断集的发布收取只碰自己的库与缓存，采集包那半一个字节不动。"""

    def test_the_judgment_channel_leaves_the_collection_side_alone(self):
        box = GitSandbox(self, prefix="bestseller-judged-")
        exchange = box.tmp / "exchange"
        exchange.mkdir()
        # 采集侧在场的东西：本机采集库、别的机器的 raw 克隆（里面躺着一份周包）、
        # plan 库的内容、outbox 里的残迹。
        collection_db = self.root / "bestseller.db"
        collection_db.write_bytes(b"collection-db-bytes")
        package = exchange / "raw-m1" / "data" / "2026" / "W38-m1.db.gz"
        package.parent.mkdir(parents=True)
        package.write_bytes(b"weekly-package-bytes")
        (exchange / "plan").mkdir()
        (exchange / "plan" / "machines.json").write_text('["m1", "m4"]\n', encoding="utf-8")
        (exchange / "outbox").mkdir()
        (exchange / "outbox" / "W38-m1.db.gz").write_bytes(b"outbox-residue")
        watched = [collection_db, exchange / "raw-m1", exchange / "plan", exchange / "outbox"]
        before = self.fingerprint(watched)

        remote = box.new_remote("judged-m1.git")
        box.must("clone", str(remote), str(exchange / "judged-m1"))
        # 另一台机器（m1）判一件并发布；本机（m4）收取——两件都只该动判断集那半。
        other = self.root / "m1-cache.sqlite"
        self.transport.decisions[("月牙杯", "月牙杯")] = True
        self.judge_products(cache=other, machine_id="m1", colors=("green", "yellow"))
        published = publish(exchange, "m1", other, now=STAMP)
        self.assertTrue(published.published, published.failure)
        outcome = collect(exchange, "m4", self.cache)

        self.assertFalse(outcome.failed, outcome.results)
        self.assertEqual(self.fingerprint(watched), before,
                         "判断集的发布收取不碰采集包那半（库、raw-*、plan、outbox）")

    def test_the_collection_channel_leaves_the_judgment_side_alone(self):
        """反之亦然：导出汇总那半（真 export／merge）不碰判断集——缓存与本机 judged 库。"""
        box = GitSandbox(self, prefix="bestseller-judged-")
        exchange = box.tmp / "exchange"
        exchange.mkdir()
        for repo in ("judged-m4", "raw-m1"):
            remote = box.new_remote(f"{repo}.git")
            box.must("clone", str(remote), str(exchange / repo))
        self.judge_products(machine_id="m4")
        published = publish(exchange, "m4", self.cache, now=STAMP)
        self.assertTrue(published.published, published.failure)
        other_db = self.root / "m1.db"
        other = connect(other_db)
        self.addCleanup(other.close)
        for conn in (other,):
            conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES ('A01','店铺1')")
            conn.commit()
        submit_offer(Database(other), "21", DAY, 50, name="月牙杯", color="blue")
        mine = connect(self.root / "bestseller.db")
        self.addCleanup(mine.close)
        watched = [self.cache, exchange / "judged-m4"]
        before = self.fingerprint(watched)

        store = FakeImageStore()
        cfg_m1 = SimpleNamespace(db_file=other_db, exchange_root=exchange, machine_id="m1",
                                 role="collector", cos_bucket="demo-bucket")
        exported = export.export(cfg_m1, week=WEEK, store=store)
        imported = merge.import_package(mine, exchange / "raw-m1" / exported.package_rel,
                                        machine_id="m4", store=store)

        self.assertFalse(exported.failed, exported.failure)
        self.assertFalse(imported.failed, imported.failure)
        self.assertEqual(self.fingerprint(watched), before,
                         "导出与汇总不碰判断集（缓存与本机 judged 库）")

    def fingerprint(self, paths) -> dict:
        """一组文件/目录的内容指纹：路径 → （字节数, 内容哈希）。"""
        prints = {}
        for path in paths:
            files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
            for file in files:
                prints[str(file)] = (file.stat().st_size,
                                     hashlib.sha256(file.read_bytes()).hexdigest())
        return prints


if __name__ == "__main__":
    unittest.main()
