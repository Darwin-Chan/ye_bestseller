"""票 02：接收侧按来源清单自动建立/更新本地 clone 并安全阻断坏来源（judged_sources.prepare_sources）。

接缝（都走公开口）：

- **来源接入**：`judged_sources.prepare_sources`——真 git（本地裸库当远端、`plan` 克隆当
  交换区的计划库那份）、快照用 01 自己的 `snapshot_text` 写（消费的就是真实格式）。
  世界每测试进程只搭一次模板、用例复制取独立副本（同 test_exchange / test_judgment_set /
  test_judged_sources）。
- **整趟接线**：`exchange.run_once` 的收取那半——按来源清单补齐克隆后再走既有的
  `judgment_set.collect`；被阻断的来源不进收取名单（不读它的包）。

隔离判据都在这一层：清单不能注入任意本地路径、只许快进、远端要核对、空库不是失败、
单来源失败只阻断它自己、没有任何可用来源要报「接收通道未就绪」。不碰真实凭据与网络。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import unittest

import exchange
from bestseller_monitor import judged_sources
from bestseller_monitor.db import CST
from bestseller_monitor.judgment_set import PACKAGE_REL
from tests.git_repos import GitSandbox, WorldTemplate
from test_exchange import ConsoleWorld, todo_of

OWNER = "yeshuzhen"
STAMP = dt.datetime(2026, 10, 9, 21, 0, tzinfo=CST)
SNAPSHOT_REL = judged_sources.SNAPSHOT_REL

_PLAN_PATHS = {"remote": "plan.git", "exchange": "exchange/plan", "work": "plan-work"}


def _plan_template() -> WorldTemplate:
    """计划库那一件世界模板：裸远端 + 交换区克隆 + 维护机的旁路发布克隆。

    模板名带 `receive-` 前缀是本文件的标识别，与 test_exchange 的 `exchange-*`、
    test_judgment_set 的 `judgment-*`、test_judged_sources 的 `judged-sources-*` 不撞。
    """
    names = _PLAN_PATHS
    return WorldTemplate.obtain(
        "receive-plan",
        lambda site: site.world(names["remote"], names["exchange"], names["work"]))


class SourceWorldMixin:
    """在用例的样例台里搭来源侧小世界：plan 克隆（旁路克隆当维护机）+ judged 裸远端。

    两种用例共用：`PrepareCase`（直接考 prepare_sources）与 `RunOnceWiringTests`（整趟收取，
    样例台来自 `ConsoleWorld` 的 `box`）。
    """

    box: GitSandbox
    exchange: pathlib.Path

    def remote(self, machine: str) -> pathlib.Path:
        """该来源的裸远端（没有就现建一个空库）；快照里的批准地址就指着它。"""
        path = self.box.tmp / f"judged-{machine}.git"
        if not path.exists():
            self.box.new_remote(f"judged-{machine}.git")
        return path

    def source(self, machine: str, *, remote: str | None = None) -> judged_sources.SourceEntry:
        """快照里的一条来源（默认批准地址指向本用例的裸远端）。"""
        return judged_sources.SourceEntry(
            machine_id=machine, repo=f"judged-{machine}",
            remote=remote if remote is not None else str(self.remote(machine)))

    def publish_snapshot(self, *entries) -> None:
        """维护机 m4 从旁路克隆发布一版来源清单（内容走 01 的真实序列化）。"""
        text = judged_sources.snapshot_text(
            owner=OWNER, generator="m4", generated_at=STAMP, sources=tuple(entries))
        self.box.commit_push(self.box.tmp / _PLAN_PATHS["work"], {SNAPSHOT_REL: text})

    def judged(self, machine: str) -> pathlib.Path:
        """交换区里那本 `judged-<机器>`（prepare 建的或用例事先克隆的）。"""
        return self.exchange / f"judged-{machine}"


class PrepareCase(unittest.TestCase, SourceWorldMixin):
    """共用夹具：交换区 + plan 克隆（旁路克隆当维护机 m4）+ 各用例自己搭的 judged 远端。"""

    def setUp(self):
        self.box = GitSandbox(self, prefix="bestseller-receive-")
        self.exchange = self.box.tmp / "exchange"
        self.exchange.mkdir()
        _plan_template().take(self.box)

    def prepare(self, *, machine_id="m1") -> judged_sources.PreparedSources:
        return judged_sources.prepare_sources(self.exchange, machine_id=machine_id)


class CloneMissingTests(PrepareCase):
    """缺本地目录：按交换区根与来源身份建 clone；空远端库是「已接入」不是失败。"""

    def test_a_declared_source_without_a_local_clone_is_cloned_and_connected(self):
        self.publish_snapshot(self.source("m4"))

        result = self.prepare()

        self.assertIsNone(result.not_ready, result.not_ready)
        self.assertEqual([entry.machine_id for entry in result.entries], ["m4"])
        entry = result.entries[0]
        self.assertFalse(entry.failure, entry.failure)
        self.assertEqual(entry.action, "cloned")
        self.assertEqual(entry.state, judged_sources.SOURCE_CONNECTED_NO_PACKAGE,
                         "空远端库：已接入、当前还没有判断集包")
        self.assertTrue((self.judged("m4") / ".git").exists())
        self.assertEqual(result.snapshot_state, "ok")
        self.assertFalse(result.stale)


class ExistingCloneTests(PrepareCase):
    """已有仓库：核对现场、只许快进；空远端的老克隆照旧「已接入」。"""

    def test_an_existing_up_to_date_clone_is_kept_and_reported_unchanged(self):
        self.box.clone(self.remote("m4"), "exchange/judged-m4")
        self.publish_snapshot(self.source("m4"))

        result = self.prepare()

        entry = result.entries[0]
        self.assertFalse(entry.failure, entry.failure)
        self.assertEqual(entry.action, "unchanged", "远端还空着：没有可拉的")
        self.assertEqual(entry.state, judged_sources.SOURCE_CONNECTED_NO_PACKAGE)

    def test_a_new_package_on_the_remote_is_fast_forwarded(self):
        remote = self.remote("m4")
        self.box.clone(remote, "exchange/judged-m4")
        self.box.seed(remote, {PACKAGE_REL: b"judgment-package-bytes"})
        self.publish_snapshot(self.source("m4"))

        result = self.prepare()

        entry = result.entries[0]
        self.assertFalse(entry.failure, entry.failure)
        self.assertEqual(entry.action, "updated")
        self.assertEqual(entry.state, judged_sources.SOURCE_CONNECTED, "有判断集包了")
        self.assertTrue((self.judged("m4") / PACKAGE_REL).exists(),
                        "快进把新内容落进了工作区")


class BlockedSourceTests(PrepareCase):
    """坏现场按来源阻断：不删、不重建、不改 origin、不清理、不 merge——点名原因。"""

    def blocked_entry(self, result) -> judged_sources.PreparedSource:
        entry = result.entries[0]
        self.assertEqual(entry.state, judged_sources.SOURCE_BLOCKED, entry.failure)
        self.assertTrue(entry.failure)
        self.assertNotIn(entry.machine_id, [m for m, _ in result.collect_sources()],
                         "被阻断的来源不进收取名单：连它的包都不读")
        return entry

    def test_an_existing_clone_with_another_origin_is_blocked_without_rewriting_it(self):
        self.box.clone(self.remote("m4"), "exchange/judged-m4")
        approved = "git@gitee.com:elsewhere/judged-m4.git"
        self.publish_snapshot(self.source("m4", remote=approved))

        entry = self.blocked_entry(self.prepare())

        self.assertIn("origin", entry.failure)
        origin = self.box.must("remote", "get-url", "origin", cwd=self.judged("m4")).strip()
        self.assertNotEqual(origin, approved, "不改 origin")

    def test_a_clone_with_local_commits_is_blocked_and_not_merged(self):
        self.box.clone(self.remote("m4"), "exchange/judged-m4")
        clone = self.judged("m4")
        self.box.write_files(clone, {"local.txt": "manual"})
        self.box.must("add", "local.txt", cwd=clone)
        self.box.must("commit", "-m", "local", cwd=clone)
        self.box.seed(self.remote("m4"), {PACKAGE_REL: b"pkg"})   # 远端也前进了一格
        self.publish_snapshot(self.source("m4"))

        entry = self.blocked_entry(self.prepare())

        self.assertIn("快进", entry.failure)
        self.assertTrue((clone / "local.txt").exists(), "本地那笔提交与内容原样留着")

    def test_a_clone_ahead_of_its_upstream_is_blocked_not_collected(self):
        """本地领先（远端没动，pull 说「已是最新」）：照样是有人动过它——读它的包
        等于读一份未经来源发布的判断集，按来源阻断。"""
        self.box.clone(self.remote("m4"), "exchange/judged-m4")
        clone = self.judged("m4")
        self.box.write_files(clone, {PACKAGE_REL: b"hand-made-package"})
        self.box.must("add", PACKAGE_REL, cwd=clone)
        self.box.must("commit", "-m", "hand made", cwd=clone)
        self.publish_snapshot(self.source("m4"))

        entry = self.blocked_entry(self.prepare())

        self.assertIn("上游", entry.failure)
        self.assertTrue((clone / PACKAGE_REL).exists(), "不重置、不重建")

    def test_a_dirty_worktree_is_blocked_and_left_alone(self):
        self.box.clone(self.remote("m4"), "exchange/judged-m4")
        (self.judged("m4") / "stray.txt").write_text("残迹", encoding="utf-8")
        self.publish_snapshot(self.source("m4"))

        entry = self.blocked_entry(self.prepare())

        self.assertIn("未提交", entry.failure)
        self.assertTrue((self.judged("m4") / "stray.txt").exists(), "不清理、不重置")

    def test_a_directory_that_is_not_a_clone_is_blocked_and_not_rebuilt(self):
        stray = self.exchange / "judged-m4"
        stray.mkdir()
        (stray / "notes.txt").write_text("人工放的", encoding="utf-8")
        self.publish_snapshot(self.source("m4"))

        entry = self.blocked_entry(self.prepare())

        self.assertIn("git 克隆", entry.failure)
        self.assertTrue((stray / "notes.txt").exists(), "目录原样留着，不删不重建")

    def test_a_damaged_repository_is_blocked_with_the_git_cause(self):
        self.box.clone(self.remote("m4"), "exchange/judged-m4")
        for child in (self.judged("m4") / ".git").iterdir():
            if child.name != "config":
                child.rename(child.with_name(child.name + "-gone"))   # 把 .git 弄残
        self.publish_snapshot(self.source("m4"))

        entry = self.blocked_entry(self.prepare())

        self.assertIn("读不动", entry.failure)


class ManifestSafetyTests(PrepareCase):
    """清单不能注入任意本地路径：非法条目按条目阻断，不碰文件系统，别条照常。"""

    def evil(self, machine_id: str, *, repo: str | None = None,
             remote: str | None = None) -> judged_sources.SourceEntry:
        return judged_sources.SourceEntry(
            machine_id=machine_id, repo=repo if repo is not None else f"judged-{machine_id}",
            remote=remote if remote is not None else str(self.remote("m4")))

    def assert_blocked(self, entry: judged_sources.PreparedSource) -> None:
        self.assertEqual(entry.state, judged_sources.SOURCE_BLOCKED, entry.failure)
        self.assertIn("清单条目非法", entry.failure)

    def test_traversal_absolute_and_separator_names_are_all_rejected(self):
        names = ["../../evil", "..", "m1/../m2", "m1\\..\\m2", "m1\\m2",
                 "C:evil", "C:\\Windows", "/etc/passwd", "m0", "m01", "m-x", "m"]
        self.publish_snapshot(*[self.evil(name) for name in names])

        result = self.prepare()

        self.assertEqual(len(result.entries), len(names))
        for entry in result.entries:
            self.assert_blocked(entry)
        self.assertEqual(sorted(p.name for p in self.exchange.iterdir()), ["plan"],
                         "一个目录都没被建出来（连本机那台 m1 也不碰）")
        self.assertTrue(result.not_ready, "全被阻断：接收通道未就绪")

    def test_a_repo_or_remote_that_does_not_match_the_machine_is_rejected(self):
        self.publish_snapshot(
            self.evil("m4", repo="judged-m5"),                    # 库名与机器编号不一致
            self.evil("m5", repo="judged-m5", remote=""),         # 没有批准远端地址
        )

        result = self.prepare()

        for entry in result.entries:
            self.assert_blocked(entry)

    def test_one_evil_entry_does_not_stop_the_good_ones(self):
        self.publish_snapshot(self.evil("../../evil"), self.source("m4"))

        result = self.prepare()

        by_machine = {entry.machine_id: entry for entry in result.entries}
        self.assert_blocked(by_machine["../../evil"])
        good = by_machine["m4"]
        self.assertFalse(good.failure, good.failure)
        self.assertEqual(good.state, judged_sources.SOURCE_CONNECTED_NO_PACKAGE)
        self.assertIsNone(result.not_ready, result.not_ready)
        self.assertEqual([m for m, _ in result.collect_sources()], ["m4"])


class StalePlanTests(PrepareCase):
    """plan 拉不动：用本机上一版快照处理已有且匹配的来源；不新建克隆，给显式告警。"""

    def plan_offline(self) -> None:
        """先让 plan 克隆拉上快照，再把 plan 远端挪走——pull 从此失败，本地留着上一版。"""
        self.box.must("pull", cwd=self.box.tmp / _PLAN_PATHS["exchange"])
        (self.box.tmp / _PLAN_PATHS["remote"]).rename(self.box.tmp / "plan-gone")

    def test_a_stale_snapshot_updates_existing_clones_but_never_creates_new_ones(self):
        self.box.clone(self.remote("m4"), "exchange/judged-m4")
        self.publish_snapshot(self.source("m4"), self.source("m2"))
        self.plan_offline()

        result = self.prepare()

        self.assertTrue(result.stale)
        self.assertIn("上一版", "；".join(result.warnings))
        by_machine = {entry.machine_id: entry for entry in result.entries}
        m4 = by_machine["m4"]
        self.assertFalse(m4.failure, m4.failure)
        self.assertEqual(m4.state, judged_sources.SOURCE_CONNECTED_NO_PACKAGE,
                         "已有且匹配：照常处理")
        m2 = by_machine["m2"]
        self.assertEqual(m2.state, judged_sources.SOURCE_SKIPPED)
        self.assertFalse(self.judged("m2").exists(), "过期快照不新建克隆")
        self.assertEqual([m for m, _ in result.collect_sources()], ["m4"])
        self.assertIsNone(result.not_ready, "还有能处理的来源：不是未就绪")


class NotReadyTests(PrepareCase):
    """没有任何可用来源 → 接收通道未就绪（不能表现成「没有新判断集」）。"""

    def test_no_plan_clone_and_no_local_sources_is_not_ready(self):
        (self.box.tmp / _PLAN_PATHS["exchange"]).rename(self.box.tmp / "plan-away")

        result = self.prepare()

        self.assertEqual(result.snapshot_state, "no_plan_clone")
        self.assertIn("计划库还没", result.not_ready)
        self.assertIn("judged", result.not_ready, "点名本机也没有别的 judged 库")
        self.assertEqual(result.collect_sources(), ())

    def test_a_snapshot_that_was_never_published_is_not_ready(self):
        result = self.prepare()

        self.assertEqual(result.snapshot_state, "absent")
        self.assertIn("还没发布过", result.not_ready)

    def test_a_broken_snapshot_is_refused_not_guessed(self):
        self.box.commit_push(self.box.tmp / _PLAN_PATHS["work"], {SNAPSHOT_REL: b"not a manifest at all"})

        result = self.prepare()

        self.assertEqual(result.snapshot_state, "broken")
        self.assertIn("读不出来", result.not_ready)

    def test_an_unknown_snapshot_format_is_refused(self):
        text = judged_sources.snapshot_text(owner=OWNER, generator="m4", generated_at=STAMP,
                                            sources=(self.source("m4"),))
        self.box.commit_push(self.box.tmp / _PLAN_PATHS["work"], {
            SNAPSHOT_REL: text.replace('"format_version": "v1"', '"format_version": "v9"')})

        result = self.prepare()

        self.assertEqual(result.snapshot_state, "unknown_format")
        self.assertIn("格式版本", result.not_ready)
        self.assertFalse(self.judged("m4").exists(), "不按不认识的格式动任何目录")

    def test_when_every_declared_source_is_blocked_it_is_not_ready_with_names(self):
        self.publish_snapshot(self.source("m4", remote=str(self.box.tmp / "nowhere.git")))

        result = self.prepare()

        self.assertIn("都没接上", result.not_ready)
        self.assertIn("judged-m4", result.not_ready)

    def test_a_local_clone_outside_the_manifest_still_counts_as_a_source(self):
        self.box.clone(self.remote("m2"), "exchange/judged-m2")

        result = self.prepare()

        self.assertIsNone(result.not_ready, "本地已有 judged 库：不是未就绪")
        self.assertEqual([(m, path.name) for m, path in result.collect_sources()],
                         [("m2", "judged-m2")])
        self.assertEqual([m for m, _ in result.local_only], ["m2"])

    def test_the_machines_own_repo_in_the_manifest_is_skipped_silently(self):
        self.publish_snapshot(self.source("m1"), self.source("m4"))

        result = self.prepare()

        self.assertEqual([entry.machine_id for entry in result.entries], ["m4"])
        self.assertFalse(self.judged("m1").exists(), "本机那本不发自己收，也不建克隆")

    def test_a_snapshot_with_only_the_machines_own_repo_is_not_ready(self):
        self.publish_snapshot(self.source("m1"))

        result = self.prepare()

        self.assertEqual(result.entries, ())
        self.assertIn("只有本机", result.not_ready)


class CloneFailureIsolationTests(PrepareCase):
    """单来源失败按来源阻断，其他来源继续（够不着的远端、克隆没成）。"""

    def test_a_failing_clone_blocks_only_that_source(self):
        self.publish_snapshot(
            self.source("m4", remote=str(self.box.tmp / "nowhere.git")),
            self.source("m2"))

        result = self.prepare()

        by_machine = {entry.machine_id: entry for entry in result.entries}
        self.assertEqual(by_machine["m4"].state, judged_sources.SOURCE_BLOCKED)
        self.assertIn("克隆没成功", by_machine["m4"].failure)
        m2 = by_machine["m2"]
        self.assertFalse(m2.failure, m2.failure)
        self.assertEqual(m2.state, judged_sources.SOURCE_CONNECTED_NO_PACKAGE)
        self.assertIsNone(result.not_ready)
        self.assertEqual([m for m, _ in result.collect_sources()], ["m2"])


class RunOnceWiringTests(unittest.TestCase, SourceWorldMixin):
    """整趟接线：收取任务先按来源清单接入，再走既有收取；报告与退出码说清状态。

    只在实际收取任务里接入——开窗口（`Api.state`）与只发布/只汇总都不接入。
    """

    def setUp(self):
        self.world = ConsoleWorld(self)              # 机器 m1；没配 gitee_owner = 接收机
        self.box = self.world.box
        self.exchange = self.world.root
        self.world.analysis_config()
        _plan_template().take(self.box)

    def away(self, name: str) -> None:
        """把交换区里的一件挪走（改名不删：绕开 git 只读对象）。"""
        (self.exchange / name).rename(self.box.tmp / f"{name}-away")

    def test_a_collect_task_clones_the_declared_source_and_reports_it_connected(self):
        self.world.judged("m4")
        self.away("judged-m4")                       # 本机交换区里还没有这本克隆
        self.publish_snapshot(self.source("m4"))

        outcome = self.world.run(only="collect")

        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        self.assertTrue((self.judged("m4") / ".git").exists(), "收取任务补上了克隆")
        report = self.world.report()
        self.assertIn("来源接入：judged-m4 已接入", report)
        self.assertIn("还没发布过判断集", report, "空远端库：已接入、没有包——不是失败")

    def test_a_blocked_source_asks_for_a_look_while_the_others_still_collect(self):
        self.world.judged("m4", "m5")
        self.world.judged_publish("m4", rows=(("pair-1", "sig-1"),))
        (self.exchange / "judged-m5" / "stray.txt").write_text("残迹", encoding="utf-8")
        self.publish_snapshot(self.source("m4"), self.source("m5"))

        outcome = self.world.run(only="collect")

        self.assertEqual(outcome.exit_code, 1, "一条被阻断：有需要人看一眼的")
        report = self.world.report()
        self.assertIn("judged-m5 被阻断", report)
        self.assertIn("judged-m5 没接入", todo_of(report))
        results = {result.machine: result for result in outcome.judgment_collect.results}
        self.assertEqual(sorted(results), ["m4"], "被阻断的来源不进收取名单：不读它的包")
        self.assertFalse(results["m4"].failed, results["m4"].failure)
        self.assertEqual(results["m4"].added.get("judgments"), 1, "健康来源照收")

    def test_without_any_usable_source_the_run_says_receive_channel_not_ready(self):
        self.away("plan")                            # 计划库还没 clone

        outcome = self.world.run(only="collect")

        self.assertEqual(outcome.exit_code, 2, "没有任何可用来源 = 本机没做成事")
        self.assertIn("接收通道未就绪", "\n".join(outcome.failures))
        report = self.world.report()
        self.assertIn("接收通道未就绪", report)
        self.assertIn("接收通道未就绪", todo_of(report))

    def test_a_stale_plan_still_collects_known_sources_with_a_warning(self):
        self.world.judged("m4")
        self.world.judged_publish("m4", rows=(("pair-1", "sig-1"),))
        self.publish_snapshot(self.source("m4"))
        self.box.must("pull", cwd=self.exchange / "plan")     # plan 克隆先拿到快照
        (self.box.tmp / _PLAN_PATHS["remote"]).rename(self.box.tmp / "plan-gone")

        outcome = self.world.run(only="collect")

        self.assertEqual(outcome.exit_code, 1, "用了上一版：有需要人看一眼的")
        result = outcome.judgment_collect.results[0]
        self.assertFalse(result.failed, result.failure)
        self.assertEqual(result.added.get("judgments"), 1, "已有且匹配的来源照常收")
        self.assertIn("上一版", self.world.report())

    def test_opening_the_exchange_window_does_not_touch_sources(self):
        self.world.judged("m4")
        self.away("judged-m4")
        self.publish_snapshot(self.source("m4"))

        exchange.Api(self.world.cfg()).state()

        self.assertFalse(self.judged("m4").exists(), "开窗口不接入来源（只有收取任务才接）")

    def test_a_publish_only_task_does_not_prepare_sources(self):
        self.world.judged("m4")
        self.away("judged-m4")
        self.publish_snapshot(self.source("m4"))

        self.world.run(only="publish")

        self.assertFalse(self.judged("m4").exists(), "只发不收的任务不接入来源")
        self.assertNotIn("来源接入", self.world.report())


if __name__ == "__main__":
    unittest.main()
