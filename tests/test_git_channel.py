"""票据 04：交换区 git 通道的验收测试——真跑 git，远端是本地裸库。

三条验收路径就是三个类：pull / push / push 被 non-fast-forward 拒绝后重试。
其余失败（认证、网络、钩子拒绝）不重试的边界单独钉住；两路皆空的静默失败（issue 05）
另有一类，钉住失败消息里的退出码与分档；每次 git 调用的共享上限与超时归一同另有一类
（issue 05 后续票 01，`CallTimeoutTests`）。

世界（长尾地基税票 05）：各用例的铺场（裸库远端 ＋ seed 链 ＋ 夹具克隆）每测试进程只真搭
一次模板、用例复制取独立副本；`GitChannel.clone` / `pull` / `push` 本体与被测真操作照旧
真跑（含读回验证的全新克隆）。多个用例共用的内容各自成件（按内容与克隆名单的摘要取名，
见 `world_template`）；单用例内容取共享空件、在副本上自己走两步（见 `blank_world_template`）。
"""
from __future__ import annotations

import hashlib
import subprocess
import unittest
from unittest import mock

from bestseller_monitor import git_channel
from bestseller_monitor.git_channel import ChannelError, GitChannel
from tests.git_repos import GitSandbox, WorldTemplate

# 各件世界在副本里的相对路径（沿用现搭时的名字）
REMOTE = "remote.git"
MINE = "plan"
OTHER = "other"
RAW_M1 = "raw-m1"      # 发布侧那件（HeadAndPathTests）的克隆名

# 各件世界（模板件）远端第一笔提交的内容——内容一样的世界共用一件模板（按摘要取名）；
# 单用例内容不设常量、就地写在用例里（见 `blank_world_template`）
V1 = "v1\n"
BASE = "base\n"
NOTE = "seed\n"
PLAN_JSON = "{}\n"


def blank_world_template() -> WorldTemplate:
    """共享的空件：一个空裸库当远端（「先建远端」那一步）。

    单用例内容的世界不单独成件（成件的一次性搭建与用例自铺场的调用数持平：2–6 次，
    各只服务一条用例；取舍见本票 Comments 的等价性清单）——用例取这份空件后自己走
    两步：`seed` 给副本里的远端放上首笔提交、再 `clone` 出夹具克隆，与逐用例现搭的
    调用序逐拍一致；只有 init 这一次调用被三个用例共用省掉。
    """
    def build(site: GitSandbox) -> list[str]:
        paths = site.world(REMOTE)
        # 这件空件的签名是「远端还没有任何分支」（未出生的裸库）：谁把它改成带 seed 的件，
        # 这里当场红——tolerates 的「空库 pull 不抛」、broken 的起点、CloneTests 的自 seed
        # 都会以别的理由照常绿（实测：播上种后 17 项全绿），覆盖被悄悄换掉。裸库默认不写
        # reflog（票 03 定论），所以查引用：刚 init 的空裸库 refs/ 下没有文件。
        if any(p.is_file() for p in (site.tmp / REMOTE / "refs").rglob("*")):
            raise AssertionError("空件的远端该还没有任何引用（它该是刚 init 的空裸库）")
        return paths

    return WorldTemplate.obtain("git-channel-blank", build)


def world_template(files: dict[str, str | bytes], *clones: str) -> WorldTemplate:
    """取（必要时真搭一次）一件「已 seed 的裸远端 ＋ 若干夹具克隆」的世界（一步写法）。

    建序就是本文件世界的签名：先 seed（正常发布路径）再建克隆，克隆带着那条历史出生
    ——「带历史的模板件」是长尾地基税票 03 起原语的能力（见 `git_repos.WorldTemplate`）。
    模板名按 seed 内容与克隆名单的摘要走（两样都一样才共用一件）；前缀 `git-channel-`
    是本文件的识别标，与别的测试文件的模板不撞（缓存按名字走、整个测试进程共用）。
    """
    tag = hashlib.sha256(
        repr((sorted(files.items()), clones)).encode("utf-8")).hexdigest()[:10]

    def build(site: GitSandbox) -> list[str]:
        paths = site.world(REMOTE, *clones, files=files)
        # 件里的克隆该是「出生过的」：建在 seed 之后，带着检出与 `.git/logs` 的 reflog。
        # 谁把建序改成两步写法（先建克隆、后 seed 的空件那种形状），这里当场红——那份
        # 世界里克隆是未出生的，「pull 拉的是接在 v1 上的增量提交」这类覆盖会被悄悄弄没
        # （先例：`test_shops_sync.build_world` 的反向建成即查）。
        if clones and not (site.tmp / clones[0] / ".git" / "logs").is_dir():
            raise AssertionError(
                f"世界里的 {clones[0]} 该是带着历史出生的（克隆要建在 seed 之后）")
        return paths

    return WorldTemplate.obtain(f"git-channel-{tag}", build)


class CloneTests(unittest.TestCase):
    def test_clone_brings_remote_content_to_the_target_path(self):
        box = GitSandbox(self)
        blank_world_template().take(box)        # 空件：空裸库当远端；单用例内容，用例自己 seed
        remote = box.tmp / REMOTE
        box.seed(remote, {"shops.csv": "shop_key\nA01\n"})
        target = box.tmp / "exchange" / "plan"

        channel = GitChannel.clone(str(remote), target)

        self.assertEqual(channel.path, target)
        self.assertEqual((target / "shops.csv").read_text(encoding="utf-8"),
                         "shop_key\nA01\n")


class PullTests(unittest.TestCase):
    def setUp(self):
        self.box = GitSandbox(self)
        self.remote = self.box.tmp / REMOTE

    def test_pull_brings_new_commits_from_the_remote(self):
        world_template({"shops.csv": V1}, MINE, OTHER).take(self.box)   # 远端已 seed v1
        mine = self.box.tmp / MINE
        other = self.box.tmp / OTHER
        self.box.commit_push(other, {"shops.csv": "v2\n"})

        GitChannel(mine).pull()

        self.assertEqual((mine / "shops.csv").read_text(encoding="utf-8"), "v2\n")

    def test_pull_tolerates_a_remote_that_has_no_branch_yet(self):
        """Gitee 上刚建的空库：远端还没有这个分支，没有可拉的，不算失败。"""
        blank_world_template().take(self.box)   # 空件：远端还没人推过
        mine = self.box.clone(self.remote, MINE)

        GitChannel(mine).pull()                     # 不抛

    def test_pull_surfaces_a_broken_remote(self):
        blank_world_template().take(self.box)
        self.box.seed(self.remote, {"x.txt": "x\n"})    # 单用例内容：用例自己 seed
        mine = self.box.clone(self.remote, MINE)
        self.box.must("remote", "set-url", "origin", str(self.box.tmp / "gone.git"), cwd=mine)

        with self.assertRaises(ChannelError) as ctx:
            GitChannel(mine).pull()

        self.assertIn("does not appear to be a git repository", str(ctx.exception))


class PushTests(unittest.TestCase):
    def setUp(self):
        self.box = GitSandbox(self)
        world_template({"base.txt": BASE}, MINE, OTHER).take(self.box)
        self.remote = self.box.tmp / REMOTE
        self.mine = self.box.tmp / MINE

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
        other = self.box.tmp / OTHER
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
        other = self.box.tmp / OTHER
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

    def test_push_onto_an_unreachable_channel_fails_without_retrying(self):
        """通道不可达（远端没了）：push 的第一步 pull 就失败——不重试、不无限打转。"""
        self.box.must("remote", "set-url", "origin", str(self.box.tmp / "gone.git"),
                      cwd=self.mine)
        self.box.write_files(self.mine, {"mine.txt": "mine\n"})
        channel = GitChannel(self.mine)
        self.assertTrue(channel.commit("add mine.txt", [self.mine / "mine.txt"]))

        with self.assertRaises(ChannelError) as ctx:
            channel.push()

        self.assertIn("拉取失败", str(ctx.exception),
                      "报的是第一步的拉取：没有落到重试循环里")
        self.assertIn("does not appear to be a git repository", str(ctx.exception))


class ReadOnlyCredentialTests(unittest.TestCase):
    """只读凭据（spec §11）：clone / pull 照常，push 被远端拒绝、不重试、原样报出。

    远端装 pre-receive 钩子拒绝一切写入——只读部署公钥那一侧的形态：写权限在服务端
    被拿掉，客户端拿到的是 `[remote rejected]`。「push 被拒 = 只读档位配置正确」。
    """

    def setUp(self):
        self.box = GitSandbox(self)
        world_template({"shops.csv": V1}, MINE, OTHER).take(self.box)   # 与 PullTests 共用一件
        self.remote = self.box.tmp / REMOTE
        self.attempts = self.box.tmp / "push-attempts"
        self.box.install_read_only_remote(self.remote, self.attempts)
        self.mine = self.box.tmp / MINE    # 只读凭据下 clone 照常（钩子只拦 push；clone 在模板里）

    def test_pull_still_brings_what_another_machine_published(self):
        """别的机器（有写权限）发布的提交，只读这台 pull 得到。"""
        other = self.box.tmp / OTHER
        self.box.write_files(other, {"shops.csv": "v2\n"})
        self.box.must("add", "--", "shops.csv", cwd=other)
        self.box.must("commit", "-m", "v2", "--", "shops.csv", cwd=other)
        self.box.publish_into_bare(self.remote, other)

        GitChannel(self.mine).pull()

        self.assertEqual((self.mine / "shops.csv").read_text(encoding="utf-8"), "v2\n")

    def test_push_is_remote_rejected_and_never_retried(self):
        self.box.write_files(self.mine, {"mine.txt": "mine\n"})
        channel = GitChannel(self.mine)
        self.assertTrue(channel.commit("add mine.txt", [self.mine / "mine.txt"]))

        with self.assertRaises(ChannelError) as ctx:
            channel.push()

        self.assertEqual(self.attempts.read_text(encoding="utf-8").split(), ["ran"],
                         "远端拒绝不是 non-fast-forward：只试一次，不无限重试")
        self.assertIn("remote rejected", str(ctx.exception), "把远端的拒绝原样带回来")
        # 远端一个字节都没变；本机那笔提交还留着（谁也没撤它）
        landed = self.box.clone(self.remote, "check")
        self.assertEqual((landed / "shops.csv").read_text(encoding="utf-8"), V1)
        self.assertFalse((landed / "mine.txt").exists())


class LocalChangesTests(unittest.TestCase):
    """准备串开跑前的残迹自检：未提交改动与未跟踪文件都要看得见、清得掉。"""

    def setUp(self):
        self.box = GitSandbox(self)
        world_template({"plan/2026-W38.json": PLAN_JSON}, MINE).take(self.box)
        self.mine = self.box.tmp / MINE

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
        world_template({"note.txt": NOTE}, RAW_M1).take(self.box)
        self.mine = self.box.tmp / RAW_M1

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


class SilentFailureTests(unittest.TestCase):
    """两路皆空、返回码非零的静默失败（issue 05）：消息要带退出码，并说清是哪一档。

    真 git 自己失败几乎总会留话（认证、网络、冲突、锁），「两路皆空」造不出来——这一件
    在进程边界（`_run`）给一个空返回，其余照旧真跑；最后一条用真 git 的失败（远端地址
    失效）钉住「有输出时照样带退出码」。
    """

    def setUp(self):
        self.box = GitSandbox(self)
        self.repo = self.box.tmp / "raw-m1"

    def _silent(self, returncode: int):
        done = subprocess.CompletedProcess(["git"], returncode, "", "")
        return mock.patch.object(git_channel, "_run", return_value=done)

    def test_a_silent_pull_failure_names_the_exit_code(self):
        with self._silent(128):
            with self.assertRaises(ChannelError) as ctx:
                GitChannel(self.repo).pull()

        message = str(ctx.exception)
        self.assertIn("拉取失败", message)
        self.assertIn("退出码 128", message)
        self.assertNotIn("（git 没有输出）", message, "空括号那版等于什么都没说")

    def test_a_silently_killed_git_is_named_as_killed(self):
        """被外部结束的码（Windows 的 NTSTATUS 那一档、POSIX 的负数）要说成那一档。"""
        for killed in (-1073741510, 0xC000013A, 0xC0000005):
            with self.subTest(returncode=killed), self._silent(killed):
                with self.assertRaises(ChannelError) as ctx:
                    GitChannel(self.repo).pull()

            message = str(ctx.exception)
            self.assertIn("外部结束", message)
            self.assertIn(str(killed), message)

    def test_the_message_joins_both_streams_and_still_names_the_exit_code(self):
        """push --porcelain 的理由在 stdout、stderr 只有一句：两个流都得带上。"""
        done = subprocess.CompletedProcess(
            ["git"], 1, "! [remote rejected] master\n", "failed to push some refs\n")
        with mock.patch.object(git_channel, "_run", return_value=done):
            with self.assertRaises(ChannelError) as ctx:
                GitChannel(self.repo).pull()

        message = str(ctx.exception)
        self.assertIn("failed to push some refs", message)
        self.assertIn("! [remote rejected] master", message)
        self.assertIn("退出码 1", message)

    def test_a_failure_with_output_keeps_gits_words_and_adds_the_code(self):
        blank_world_template().take(self.box)
        self.box.seed(self.box.tmp / REMOTE, {"x.txt": "x\n"})
        mine = self.box.clone(self.box.tmp / REMOTE, MINE)
        self.box.must("remote", "set-url", "origin", str(self.box.tmp / "gone.git"), cwd=mine)

        with self.assertRaises(ChannelError) as ctx:
            GitChannel(mine).pull()

        message = str(ctx.exception)
        self.assertIn("does not appear to be a git repository", message)
        self.assertRegex(message, r"退出码 \d+")


class CallTimeoutTests(unittest.TestCase):
    """票 01（issue 05 后续）：通道里每次 git 调用共用一处上限，超时归一成通道错误。

    真等满上限的用例不写（也不许写）：这一件在进程边界（`subprocess.run`）用假进程
    捕获参数、注入 `TimeoutExpired`，不真跑 git——与 `SilentFailureTests` 同款路子。
    """

    def setUp(self):
        self.box = GitSandbox(self)
        self.repo = self.box.tmp / "raw-m1"

    def _fake_git(self, calls):
        """假 git 进程：记下每次调用的参数，按子命令回一个「像成了」的返回。"""
        outputs = {"rev-list": "0\t0\n", "rev-parse": "abc1234\n",
                   "remote": "https://example.invalid/x.git\n"}

        def fake(cmd, **kwargs):
            calls.append((cmd, kwargs))
            sub = cmd[1] if cmd[1] != "-C" else cmd[3]
            out = outputs.get(sub, "")
            if kwargs.get("text"):
                return subprocess.CompletedProcess(cmd, 0, out, "")
            return subprocess.CompletedProcess(cmd, 0, out.encode("utf-8"), b"")

        return fake

    def _hang(self):
        """假 git 进程：真给 `subprocess.run` 那一档超时——立刻抛 `TimeoutExpired`。"""
        def fake(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return fake

    def test_every_git_call_takes_the_limit_from_the_one_constant(self):
        """逐条钉住：所有路（含读字节的 `show`）的上限都取自同一处常量。

        把常量临时换成一个哨兵值再跑：哪条路要是另抄了一个数（比如硬编码 300），
        捕获到的就不是哨兵——「集中在一处」当场破。
        """
        calls = []
        channel = GitChannel(self.repo)
        with mock.patch.object(git_channel.subprocess, "run",
                               side_effect=self._fake_git(calls)), \
                mock.patch.object(git_channel, "_TIMEOUT_SEC", 4242):
            GitChannel.clone("https://example.invalid/plan.git", self.box.tmp / "clone")
            channel.pull()
            channel.pull_ff_only()
            channel.remote_url()
            channel.head_matches_upstream()
            channel.local_changes()
            channel.commit("m", [self.box.tmp / "f.txt"])
            channel.push()
            channel.head()
            channel.read_path("data/x.db.gz")
            channel.reset_to_upstream(clean=True)

        self.assertTrue(any(kw.get("text") for _, kw in calls), "文本这几路得真跑过")
        self.assertTrue(any(not kw.get("text") for _, kw in calls),
                        "读字节那条路（`_run_bytes`）也得跑过")
        for cmd, kwargs in calls:
            self.assertEqual(kwargs.get("timeout"), 4242,
                             f"{' '.join(cmd)} 没带（或没带对）上限")

    def test_the_limit_is_one_shared_300_second_constant(self):
        """300 秒一档、与 coscli 子进程同档：取值依据写在常量旁，值本身钉在这里。"""
        self.assertEqual(git_channel._TIMEOUT_SEC, 300)

    def test_a_timed_out_call_becomes_a_channel_error_naming_it_all(self):
        """超时归一：消息里有动作、完整命令、上限与实际耗时，说清「超过上限、已放弃」。"""
        clock = mock.Mock()
        clock.monotonic.side_effect = [100.0, 431.5]    # 起表 100.0，放弃那一刻 431.5
        with mock.patch.object(git_channel.subprocess, "run", side_effect=self._hang()), \
                mock.patch.object(git_channel, "time", clock):
            with self.assertRaises(ChannelError) as ctx:
                GitChannel(self.repo).pull()

        message = str(ctx.exception)
        self.assertIn("拉取超时（git -C", message)        # 动作＋完整命令开头
        self.assertIn(str(self.repo), message)           # 命令带路径
        self.assertIn("300 秒上限", message)              # 上限秒数
        self.assertIn("实际耗时 331.5 秒", message)        # 实测耗时（431.5 − 100.0）
        self.assertIn("已放弃", message)                  # 「超过上限、已放弃」那一档

    def test_a_push_timeout_is_not_a_rejection_and_gets_no_retry(self):
        """超时不是 non-fast-forward 拒绝：push 那一步超时只试一次，原样报出。"""
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            if "push" in cmd:
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(git_channel.subprocess, "run", side_effect=fake):
            with self.assertRaises(ChannelError) as ctx:
                GitChannel(self.repo).push()

        self.assertEqual(sum("push" in cmd for cmd in calls), 1,
                         "超时不许触发重试（重试只认 non-fast-forward 拒绝）")
        self.assertIn("推送超时", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
