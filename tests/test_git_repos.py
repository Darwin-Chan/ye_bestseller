"""「取世界」原语（`git_repos.WorldTemplate`）自己的小用例：各文件的地基税改造架在它上面。

票 01 先把它们暂留在 test_exchange 里（那一票的改动范围只许动 `tests/git_repos.py` 与
`tests/test_exchange.py`）；长尾地基税线的 02 票起把它们归位到这里——后续票（03–06）新增的
原语用例也落这个文件。钉五件事：同名模板在一个进程里只搭一次；取世界只复制（一个 git 子
进程都不起）、同一件世界在一处只取一次；复制出来的副本互相独立、也碰不到模板；副本里的 git
是真的（远端还是未出生分支的空裸库、发布走正常克隆推送、钩子照旧可装可触发）；**带历史
的件**（票 03 起）同样能取——副本干净、地址改写连 reflog 一起、真能推（见
`_build_seeded_world`）。
"""
from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests import git_repos
from tests.git_repos import GitSandbox, WorldTemplate, git


def _build_world(site: GitSandbox) -> list[str]:
    """原语用例用的一件小世界：空裸库 + 交换区里的克隆 + 旁路工作克隆（与各票的世界同形）。

    建法见 `GitSandbox.world`：远端真 init、克隆真 clone，第二份是第一份的整份复制。
    """
    return site.world("raw-mt.git", "exchange/raw-mt", "mt-work")


def _selftest_template() -> WorldTemplate:
    """取（必要时搭一次）原语用例用的那件小模板（三处共用，只搭一次）。"""
    return WorldTemplate.obtain("git-repos-selftest", _build_world)


def _build_seeded_world(site: GitSandbox) -> list[str]:
    """带历史的一件小世界（票 03 起的能力）：已 seed 的裸库 ＋ 它的两份克隆。

    与上面那件空件不同，这里的远端有提交、克隆因此带着检出与 reflog——模板件的「带历史」
    契约（见 `git_repos.WorldTemplate` 的文档）靠它钉住；两份克隆又是各票里 m2/m3 那种
    「第一份真 clone、其余复制」的形状。
    """
    return site.world("seeded-mt.git", "seeded-mt", "seeded-mt-work",
                      files={"note.txt": "第一版\n"})


def _seeded_template() -> WorldTemplate:
    """取（必要时搭一次）带历史那件小模板（两处共用，只搭一次）。"""
    return WorldTemplate.obtain("git-repos-seeded-selftest", _build_seeded_world)


def _files_mentioning(root: Path, needle: str) -> list[str]:
    """这棵树里哪些文件（相对路径）的字节里出现 needle 串——副本里不该再有模板地址。

    按字节比、不按文本读：件里什么文件都有（二进制对象、git 按各种编码写的文本），
    解码读一遍只会为跟本用例无关的字节翻车。needle 取「bestseller-template-」这个前缀
    （模板根都建在它底下），三种拼法一网打尽——git 写进配置的是转义写法，拿完整路径的
    原样拼法去比是比不出来的。
    """
    blob = needle.encode("utf-8")
    return [str(path.relative_to(root)) for path in sorted(root.rglob("*"))
            if path.is_file() and blob in path.read_bytes()]


class WorldTemplateTests(unittest.TestCase):
    """「取世界」原语（`git_repos.WorldTemplate`）自己的小用例：每个用例钉住模块文档里的一条契约。"""

    def test_a_template_is_built_once_per_process(self):
        name = f"selftest-{uuid.uuid4().hex}"
        built: list[Path] = []

        def build(site):
            built.append(site.tmp)
            return []          # 这件只用来看「搭了几次」，不必有内容

        first = WorldTemplate.obtain(name, build)
        second = WorldTemplate.obtain(name, build)

        self.assertIs(first, second)
        self.assertEqual(built, [first.root], "同名模板在一个进程里只搭一次")

    def test_taking_a_world_copies_and_never_runs_git(self):
        box = GitSandbox(self, prefix="bestseller-take-quiet-")
        template = _selftest_template()     # 先在补丁外取到：第一趟取要真搭模板（真跑 git）

        # 取世界里混进一次 git 子进程就是地基税那半边复发：把样例台的 git 换成炸雷
        with patch.object(git_repos, "git",
                          side_effect=AssertionError("取世界不该起 git 子进程")):
            template.take(box)                    # 复制 + 改写地址，都不许碰 git
            with self.assertRaises(FileExistsError):
                template.take(box)                # 同一处再取一次：报错，不覆盖

        self.assertTrue((box.tmp / "exchange" / "raw-mt").is_dir(),
                        "重复取报错之后，先前那份副本该还在")

    def test_every_take_is_a_world_of_its_own(self):
        template = _selftest_template()
        box_a = GitSandbox(self, prefix="bestseller-take-a-")
        box_b = GitSandbox(self, prefix="bestseller-take-b-")
        template.take(box_a)
        template.take(box_b)

        # 副本里的克隆指着副本自己的裸库（配置文本里是转义写法，所以问 git 要地址）
        origin = Path(box_a.must("remote", "get-url", "origin",
                                 cwd=box_a.tmp / "exchange" / "raw-mt").strip())
        self.assertEqual(origin, box_a.tmp / "raw-mt.git",
                         "副本里的克隆该指着副本自己的裸库，不是模板的")
        # 配置文本里也不该再有模板路径的残迹（模板根都建在 bestseller-template- 前缀下）
        raw_config = (box_a.tmp / "exchange" / "raw-mt" / ".git"
                      / "config").read_text(encoding="utf-8")
        self.assertNotIn("bestseller-template-", raw_config)

        # A 里推一个提交：A 自己拉得到；B 与模板都看不见
        work = box_a.tmp / "mt-work"
        box_a.write_files(work, {"f.txt": "a\n"})
        box_a.must("add", "--", "f.txt", cwd=work)
        box_a.must("commit", "-m", "a", cwd=work)
        box_a.must("push", cwd=work)
        box_a.must("pull", "--rebase", cwd=box_a.tmp / "exchange" / "raw-mt")
        self.assertTrue((box_a.tmp / "exchange" / "raw-mt" / "f.txt").exists())
        for name, bare in (("B", box_b.tmp / "raw-mt.git"),
                           ("模板", template.root / "raw-mt.git")):
            self.assertNotEqual(
                git("--git-dir", str(bare), "rev-parse", "--verify", "main").returncode, 0,
                f"{name} 的裸库看不见 A 的推送")

    def test_a_copied_world_speaks_real_git(self):
        box = GitSandbox(self, prefix="bestseller-take-git-")
        _selftest_template().take(box)
        bare = box.tmp / "raw-mt.git"
        work = box.tmp / "mt-work"

        # 远端还是未出生分支的空裸库：clone 出来 HEAD 指着 main、还没有提交
        clone = box.clone(bare, "unborn-check")
        self.assertEqual(git("symbolic-ref", "--short", "HEAD", cwd=clone).stdout.strip(),
                         "main")
        self.assertNotEqual(git("rev-parse", "--verify", "HEAD", cwd=clone).returncode, 0,
                            "空裸库 clone 出来该是未出生的 main")

        # 发布走正常路径：克隆里写、提交、推送，裸库里就有提交了
        box.commit_push(work, {"f.txt": "x\n"}, message="publish")
        self.assertEqual(git("--git-dir", str(bare), "rev-parse", "main").returncode, 0)

        # 钩子照旧能装、能触发：pre-receive（服务端拒绝 = 只读档位的形态）
        counter = box.tmp / "declined"
        box.install_read_only_remote(bare, counter)
        box.write_files(work, {"g.txt": "y\n"})
        box.must("add", "--", "g.txt", cwd=work)
        box.must("commit", "-m", "second", cwd=work)
        self.assertNotEqual(git("push", cwd=work).returncode, 0)
        self.assertTrue(counter.exists(), "pre-receive 钩子真的跑了")

        # 客户端侧：pre-push 记一笔再拒绝
        counter2 = box.tmp / "client-declined"
        box.install_declining_hook(work, counter2)
        self.assertNotEqual(git("push", cwd=work).returncode, 0)
        self.assertTrue(counter2.exists(), "pre-push 钩子真的跑了")

    def test_a_seeded_world_is_copied_clean_and_keeps_no_template_address(self):
        template = _seeded_template()
        box = GitSandbox(self, prefix="bestseller-take-seeded-")
        template.take(box)
        clone, work, bare = (box.tmp / "seeded-mt", box.tmp / "seeded-mt-work",
                             box.tmp / "seeded-mt.git")

        # 副本带着模板里那条历史，但要和刚 clone 出来一样干净（索引的 stat 也对得上）
        self.assertEqual(box.must("status", "--porcelain", cwd=clone), "")
        commits = box.must("log", "--oneline", cwd=clone).splitlines()
        self.assertEqual(len(commits), 1, "模板里那一笔 seed 跟着来了")
        self.assertTrue(commits[0].endswith("seed"))
        self.assertEqual(box.must("rev-parse", "HEAD", cwd=clone).strip(),
                         git("--git-dir", str(bare), "rev-parse", "main").stdout.strip(),
                         "HEAD 就是副本自己裸库的 main")
        # 第二份克隆是第一份的整份复制：同一条历史、同一个远端
        self.assertEqual(box.must("status", "--porcelain", cwd=work), "")
        self.assertEqual(box.must("rev-parse", "HEAD", cwd=work).strip(),
                         box.must("rev-parse", "HEAD", cwd=clone).strip())
        for name, tree in (("第一份克隆", clone), ("复制出来的第二份", work)):
            self.assertEqual(Path(box.must("remote", "get-url", "origin", cwd=tree).strip()),
                             bare, f"{name}的 origin 该指着副本自己的裸库")
            self.assertEqual(box.must("rev-parse", "--abbrev-ref", "@{u}", cwd=tree).strip(),
                             "origin/main", f"{name}的上游也还认得出来")

        # 副本整棵树里不该再有模板地址：配置与 .git/logs 的 reflog 都改写了
        self.assertEqual(_files_mentioning(box.tmp, "bestseller-template-"), [],
                         "副本里还留着模板地址（模板根都建在这个前缀底下）")

    def test_a_copied_seeded_world_speaks_real_git(self):
        template = _seeded_template()
        box = GitSandbox(self, prefix="bestseller-take-seeded-git-")
        template.take(box)
        clone, work, bare = (box.tmp / "seeded-mt", box.tmp / "seeded-mt-work",
                             box.tmp / "seeded-mt.git")

        # 上游是副本自己的裸库：pull 空转，不是回模板那儿拉
        self.assertEqual(git("pull", "--rebase", cwd=clone).returncode, 0)

        # 复制出来的第二份真推一笔：落进副本自己的裸库；模板只被复制、从不被写，看不见它
        box.commit_push(work, {"second.txt": "第二版\n"}, message="second")
        self.assertEqual(git("--git-dir", str(bare), "show", "main:second.txt").returncode, 0)
        self.assertNotEqual(
            git("--git-dir", str(template.root / "seeded-mt.git"),
                "show", "main:second.txt").returncode, 0,
            "副本的推送到不了模板")


if __name__ == "__main__":
    unittest.main()
