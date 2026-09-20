"""票据 04：交换区 git 通道的验收测试——真跑 git，远端是本地裸库。

三条验收路径就是三个类：pull / push / push 被 non-fast-forward 拒绝后重试。
其余失败（认证、网络、钩子拒绝）不重试的边界单独钉住。
"""
from __future__ import annotations

import unittest

from bestseller_monitor.git_channel import ChannelError, GitChannel
from tests.git_repos import GitSandbox


class CloneTests(unittest.TestCase):
    def test_clone_brings_remote_content_to_the_target_path(self):
        box = GitSandbox(self)
        remote = box.new_remote()
        box.seed(remote, {"shops.csv": "shop_key\nA01\n"})
        target = box.tmp / "exchange" / "plan"

        channel = GitChannel.clone(str(remote), target)

        self.assertEqual(channel.path, target)
        self.assertEqual((target / "shops.csv").read_text(encoding="utf-8"),
                         "shop_key\nA01\n")


class PullTests(unittest.TestCase):
    def setUp(self):
        self.box = GitSandbox(self)
        self.remote = self.box.new_remote()

    def test_pull_brings_new_commits_from_the_remote(self):
        self.box.seed(self.remote, {"shops.csv": "v1\n"})
        mine = self.box.clone(self.remote, "plan")
        other = self.box.clone(self.remote, "other")
        self.box.commit_push(other, {"shops.csv": "v2\n"})

        GitChannel(mine).pull()

        self.assertEqual((mine / "shops.csv").read_text(encoding="utf-8"), "v2\n")

    def test_pull_tolerates_a_remote_that_has_no_branch_yet(self):
        """Gitee 上刚建的空库：远端还没有这个分支，没有可拉的，不算失败。"""
        mine = self.box.clone(self.remote, "plan")

        GitChannel(mine).pull()                     # 不抛

    def test_pull_surfaces_a_broken_remote(self):
        self.box.seed(self.remote, {"x.txt": "x\n"})
        mine = self.box.clone(self.remote, "plan")
        self.box.must("remote", "set-url", "origin", str(self.box.tmp / "gone.git"), cwd=mine)

        with self.assertRaises(ChannelError) as ctx:
            GitChannel(mine).pull()

        self.assertIn("does not appear to be a git repository", str(ctx.exception))


class PushTests(unittest.TestCase):
    def setUp(self):
        self.box = GitSandbox(self)
        self.remote = self.box.new_remote()
        self.box.seed(self.remote, {"base.txt": "base\n"})
        self.mine = self.box.clone(self.remote, "plan")

    def test_commit_then_push_lands_on_the_remote(self):
        self.box.write_files(self.mine, {"new.txt": "new\n"})
        channel = GitChannel(self.mine)

        committed = channel.commit("add new.txt", [self.mine / "new.txt"])
        channel.push()

        self.assertTrue(committed)
        landed = self.box.clone(self.remote, "check")
        self.assertEqual((landed / "new.txt").read_text(encoding="utf-8"), "new\n")

    def test_commit_reports_false_when_there_is_nothing_to_commit(self):
        committed = GitChannel(self.mine).commit("no-op", [self.mine / "base.txt"])

        self.assertFalse(committed)

    def test_push_retries_when_rejected_non_fast_forward(self):
        """远端在本机 pull 之后、push 落地之前又动了——唯一的被拒窗口。

        用 pre-push 钩子在推送途中让另一台机器抢先把提交推上去，制造真拒绝；
        通道应当自己 `pull --rebase` 重来，最终两条提交都在远端上。
        """
        other = self.box.clone(self.remote, "other")
        self.box.write_files(other, {"other.txt": "other\n"})
        self.box.must("add", "--", "other.txt", cwd=other)
        self.box.must("commit", "-m", "other", "--", "other.txt", cwd=other)
        self.box.install_racing_hook(self.mine, other)
        self.box.write_files(self.mine, {"mine.txt": "mine\n"})
        channel = GitChannel(self.mine)
        self.assertTrue(channel.commit("add mine.txt", [self.mine / "mine.txt"]))

        channel.push()

        landed = self.box.clone(self.remote, "check")
        self.assertEqual((landed / "mine.txt").read_text(encoding="utf-8"), "mine\n")
        self.assertEqual((landed / "other.txt").read_text(encoding="utf-8"), "other\n")

    def test_push_gives_up_after_the_attempt_budget(self):
        """远端每次都抢先动一格：重试用尽就报错，不无限打转。"""
        other = self.box.clone(self.remote, "other")
        self.box.install_advancing_hook(self.mine, other)
        self.box.write_files(self.mine, {"mine.txt": "mine\n"})
        channel = GitChannel(self.mine)
        self.assertTrue(channel.commit("add mine.txt", [self.mine / "mine.txt"]))

        with self.assertRaises(ChannelError) as ctx:
            channel.push(attempts=3)

        self.assertIn("3 次", str(ctx.exception))

    def test_push_does_not_retry_failures_other_than_rejection(self):
        """钩子拒绝（凭据被拒、网络断也同此路）：只试一次，原样报错。"""
        runs = self.box.tmp / "hook-runs"
        self.box.install_declining_hook(self.mine, runs)
        self.box.write_files(self.mine, {"mine.txt": "mine\n"})
        channel = GitChannel(self.mine)
        self.assertTrue(channel.commit("add mine.txt", [self.mine / "mine.txt"]))

        with self.assertRaises(ChannelError):
            channel.push()

        self.assertEqual(runs.read_text(encoding="utf-8").split(), ["ran"],
                         "非 non-fast-forward 的失败不许重试")


class LocalChangesTests(unittest.TestCase):
    """准备串开跑前的残迹自检：未提交改动与未跟踪文件都要看得见、清得掉。"""

    def setUp(self):
        self.box = GitSandbox(self)
        self.remote = self.box.new_remote()
        self.box.seed(self.remote, {"plan/2026-W38.json": "{}\n"})
        self.mine = self.box.clone(self.remote, "plan")

    def test_a_clean_clone_reports_nothing(self):
        self.assertEqual(GitChannel(self.mine).local_changes(), [])

    def test_uncommitted_and_untracked_debris_are_reported_and_discarded(self):
        self.box.write_files(self.mine, {"plan/2026-W39.json": "{ 半截\n"})   # 未跟踪残迹
        self.box.must("rm", "--", "plan/2026-W38.json", cwd=self.mine)       # 已删未提交

        channel = GitChannel(self.mine)

        self.assertEqual(len(channel.local_changes()), 2)

        channel.reset_to_upstream(clean=True)

        self.assertEqual(channel.local_changes(), [])
        self.assertTrue((self.mine / "plan" / "2026-W38.json").exists(), "已跟踪文件退回远端状态")
        self.assertFalse((self.mine / "plan" / "2026-W39.json").exists(), "未跟踪残迹被清掉")


class HeadAndPathTests(unittest.TestCase):
    """发布侧要的两件事：拿到发到哪笔提交；按 HEAD 读一个路径的字节（二进制不被改写）。"""

    def setUp(self):
        self.box = GitSandbox(self)
        self.remote = self.box.new_remote()
        self.box.seed(self.remote, {"note.txt": "seed\n"})
        self.mine = self.box.clone(self.remote, "raw-m1")

    def test_head_is_the_short_hash_of_the_current_commit(self):
        channel = GitChannel(self.mine)

        self.assertEqual(channel.head(),
                         self.box.must("rev-parse", "--short", "HEAD",
                                       cwd=self.mine).strip())

    def test_read_path_returns_the_bytes_committed_at_head(self):
        blob = bytes(range(256))                       # 全是二进制的字节：不许被解码改写
        self.box.commit_push(self.mine, {"data/2026/W38-m1.db.gz": blob},
                             message="export W38 m1")

        self.assertEqual(GitChannel(self.mine).read_path("data/2026/W38-m1.db.gz"), blob)

    def test_read_path_is_none_when_head_does_not_have_it(self):
        """工作区里有（未跟踪的残迹）也不算——问的是 HEAD 那份。"""
        self.box.write_files(self.mine, {"data/2026/W38-m1.db.gz": b"leftover"})

        self.assertIsNone(GitChannel(self.mine).read_path("data/2026/W38-m1.db.gz"))


if __name__ == "__main__":
    unittest.main()
