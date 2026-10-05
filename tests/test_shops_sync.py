"""票据 04：共享店铺清单同步的验收测试。

接缝 = `shops_sync.sync_shared_shops`：给（本机清单路径、计划库克隆路径），
按内容哈希判方向，并在真 git 仓库上真的推/拉；结果对象讲清做了什么、要不要人看一眼。

期望值都是手写的清单内容，不重算实现；「远端那份长什么样」经全新克隆看，
与另一台机器会看到的完全一样。

世界（长尾地基税票 04）：`ShopsSyncTestCase` 一族的世界每测试进程只搭一次模板、用例
`seed_shared` 复制取独立副本——形状与建序见 `build_world`（计划库克隆总是**未出生**
的：克隆先出生、内容后到，正是本文件在测的「pull 把远端内容带下来」那一序）。真 git
照旧真跑：推送、rebase 撞车、BOM/CRLF 判读、全新克隆读远端。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import unittest
from pathlib import Path

from bestseller_monitor import shops_sync
from bestseller_monitor.config import load_shops
from tests.git_repos import GitSandbox, WorldTemplate

BOM = b"\xef\xbb\xbf"
V1 = ("shop_key,shop_name,shop_url,pages,active\n"
      "A01,店一,https://a01.example/,3,1\n")
LOCAL_EDIT = V1 + "A02,店二,https://a02.example/,5,1\n"
REPO_EDIT = V1 + "A03,店三,https://a03.example/,7,1\n"
# 两边都在同一行上各改各的：rebase 时必撞车
CLASH_LOCAL = V1.replace("3,1", "5,1")
CLASH_REPO = V1.replace("3,1", "9,1")

# 计划库世界里的相对路径（plan.git 与 exchange/plan 沿用现搭时的名字）
PLAN_REMOTE = "plan.git"
PLAN_CLONE = "exchange/plan"
OTHER_CLONE = "other-machine"      # 另一台机器的克隆（只在有共享清单的世界里）


def build_world(site: GitSandbox, *, shared: str | None) -> list[str]:
    """搭一件计划库世界：空远端 ＋ 未出生克隆（先建克隆）＋ 视内容 seed ＋ 另一台机器的克隆。

    这一件世界的序是「克隆先出生、内容后到」（`GitSandbox.world` 文档里的两步写法）——
    克隆出来是未出生的 main，共享清单等着 sync 里的 pull 落地；`shared=None` 就是全新
    计划库（第一台机器都还没推过，也就没有别的机器）。有共享清单时「另一台机器」的克隆
    在 seed 之后才建——它要带着那份历史，改一笔就能直接推。
    """
    paths = site.world(PLAN_REMOTE, PLAN_CLONE)
    # 「克隆先出生」是这件世界的签名，建成的样子在这里查一眼：出生过的克隆会带 `.git/logs`
    # （`clone: from …` 那条 reflog），未出生的不会。谁把两步序改成一步到位（比如
    # `world(..., files=…)`），这里当场红——那会把「pull 把远端内容带下来」的覆盖悄悄弄没。
    if (site.tmp / PLAN_CLONE / ".git" / "logs").exists():
        raise AssertionError(
            f"计划库克隆该是未出生的（克隆要建在 seed 之前），{PLAN_CLONE} 已经出生了")
    if shared is not None:
        site.seed(site.tmp / PLAN_REMOTE, {"shops.csv": shared.encode("utf-8")})
        site.clone(site.tmp / PLAN_REMOTE, OTHER_CLONE)
        paths.append(OTHER_CLONE)
    return paths


def world_template(shared: str | None) -> WorldTemplate:
    """取（必要时真搭一次）一件计划库世界的模板；远端内容不同的世界各自成件。

    模板名按远端那份内容的摘要走（内容一样共用一件；`None`＝全新计划库，自成一档）；
    前缀 `shops-sync-` 是本文件的识别标，与别的测试文件的模板不撞（缓存按名字走、
    整个测试进程共用）。
    """
    tag = ("empty" if shared is None
           else hashlib.sha256(shared.encode("utf-8")).hexdigest()[:10])
    return WorldTemplate.obtain(
        f"shops-sync-{tag}", lambda site: build_world(site, shared=shared))


class ShopsSyncTestCase(unittest.TestCase):
    """样例台：<tmp>/exchange/plan 是计划库克隆；<tmp>/config/shops.csv 是本机副本。

    世界由 `seed_shared` 取（每测试进程只真搭一次模板、用例复制取独立副本）；
    形状与建序见 `build_world`。
    """

    def setUp(self):
        self.box = GitSandbox(self)
        self.remote = self.box.tmp / PLAN_REMOTE
        self.plan = self.box.tmp / PLAN_CLONE
        self.local = self.box.tmp / "config" / "shops.csv"
        self._copies = 0
        self._world_taken = False

    # ---- 手 ----

    def write_local_bytes(self, data: bytes) -> None:
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.local.write_bytes(data)

    def write_local(self, text: str, *, encoding: str = "utf-8") -> None:
        self.write_local_bytes(text.encode(encoding))

    def seed_shared(self, text: str | None) -> None:
        """取一件计划库世界（复制模板里的副本）。

        `text` 是远端里等着的那份首版共享清单（第一台机器建库时推上去的）；`None`
        表示全新的计划库——远端还是空的、还没有共享清单。内容不同的世界各自成件，
        本进程第一次取某件时才真搭模板（真跑 git），之后每次取只是复制——这笔一次性
        成本算在第一个取到它的用例头上；一件世界在一个用例里只取一次（`take` 的目标
        已存在就报错）。
        """
        world_template(text).take(self.box)
        self._world_taken = True

    def other_pushes(self, text: str) -> None:
        """另一台机器改了共享清单并推上去（它的克隆取世界时就带着首版历史）。

        它像一台常驻的机器：起点停在 seed 那一笔，不会回头拉新的远端状态——调用点须是
        远端仍停在那儿的时候（本文件的几处都是：前面那趟 sync 判成 IN_SYNC、没有推过）。
        """
        other = self.box.tmp / OTHER_CLONE
        if not other.is_dir():
            raise AssertionError(
                f"这件世界没有「另一台机器」（{OTHER_CLONE}）：它只在有共享清单的"
                "世界里——先 seed_shared(V1)")
        self.box.commit_push(other, {"shops.csv": text.encode("utf-8")},
                             message="另一台机器改的共享清单")

    def fresh_clone(self) -> Path:
        """一份全新克隆——另一台机器现在会看到的远端内容。"""
        self._copies += 1
        return self.box.clone(self.remote, f"check-{self._copies}")

    def shared_bytes(self) -> bytes:
        return (self.fresh_clone() / "shops.csv").read_bytes()

    def sync(self) -> shops_sync.SyncResult:
        if not self._world_taken:
            raise AssertionError(
                "先 seed_shared 取一件世界（setUp 只摆路径、不再现搭计划库）；"
                "漏了它，sync 会把「世界没取」误报成「计划库还没 clone」")
        return shops_sync.sync_shared_shops(self.local, self.plan)


class DirectionTests(ShopsSyncTestCase):
    """哈希判方向的三个分支各自可验；内容噪声不算变化。"""

    def test_same_content_is_in_sync(self):
        self.seed_shared(V1)
        self.write_local(V1)

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.IN_SYNC)
        self.assertFalse(outcome.warning)

    def test_encoding_and_line_ending_noise_is_not_a_change(self):
        """Excel 双击存一次（BOM、CRLF）不该被当成一次真实修改。"""
        self.seed_shared(V1)
        self.write_local_bytes(BOM + V1.replace("\n", "\r\n").encode("utf-8"))

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.IN_SYNC)

    def test_only_local_changed_pushes(self):
        self.seed_shared(V1)
        self.write_local(V1)
        self.sync()                                  # 建立基线
        self.write_local(LOCAL_EDIT)

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.PUSHED)
        self.assertFalse(outcome.warning)
        self.assertEqual(self.shared_bytes().decode("utf-8-sig"), LOCAL_EDIT)

    def test_only_repo_changed_pulls_and_overwrites_the_local_copy(self):
        self.seed_shared(V1)
        self.write_local(V1)
        self.sync()
        self.other_pushes(REPO_EDIT)

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.PULLED)
        self.assertFalse(outcome.warning)
        self.assertEqual(self.local.read_text(encoding="utf-8-sig"), REPO_EDIT)
        self.assertTrue(self.local.read_bytes().startswith(BOM),
                        "拉下来的副本也写成共享清单的规范形态")
        self.assertEqual(self.sync().action, shops_sync.SyncAction.IN_SYNC,
                         "拉完立刻再同步：已经一致，不重复拉")

    def test_both_changed_stops_and_shows_both_versions(self):
        self.seed_shared(V1)
        self.write_local(V1)
        self.sync()
        self.write_local(LOCAL_EDIT)
        self.other_pushes(REPO_EDIT)
        before_local = self.local.read_bytes()

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.CONFLICT)
        self.assertFalse(outcome.warning)
        self.assertIsNotNone(outcome.diff)
        self.assertIn("-A02", outcome.diff, "本机那版多出的行在差异里")
        self.assertIn("+A03", outcome.diff, "库那版多出的行在差异里")
        self.assertIn("A03", outcome.message, "差异要随消息给人看")
        self.assertEqual(self.local.read_bytes(), before_local, "停下就不能动本机那份")
        self.assertEqual(self.shared_bytes().decode("utf-8-sig"), REPO_EDIT,
                         "停下就不能动库那份")

    def test_without_a_baseline_two_differing_versions_stop(self):
        """新机器上两边都在、又不一致：没有基线判不出方向，按「两边都变」停下。"""
        self.seed_shared(V1)
        self.write_local(LOCAL_EDIT)

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.CONFLICT)


class FirstContactTests(ShopsSyncTestCase):
    """清单还只有一边时的两个回合。"""

    def test_local_missing_is_pulled_from_the_plan_repo(self):
        self.seed_shared(V1)

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.PULLED)
        self.assertEqual(self.local.read_text(encoding="utf-8-sig"), V1)
        self.assertTrue(self.local.read_bytes().startswith(BOM))
        self.assertEqual([s.key for s in load_shops(self.local)], ["A01"])

    def test_first_push_writes_a_bom_copy_that_load_shops_reads(self):
        """共享清单的磁盘形态：UTF-8 带 BOM（load_shops 第一分支即命中）。"""
        self.seed_shared(None)      # 全新计划库：远端还没有共享清单
        self.write_local(LOCAL_EDIT)

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.PUSHED)
        copy = self.fresh_clone() / "shops.csv"
        self.assertTrue(copy.read_bytes().startswith(BOM), "推上去的清单必须带 BOM")
        self.assertEqual([s.name for s in load_shops(copy)], ["店一", "店二"])

    def test_legacy_gbk_local_becomes_the_shared_list(self):
        """m1 现机首次：本机清单还是 GBK，推上去那份转成 UTF-8 BOM，本机副本跟上。"""
        self.seed_shared(None)      # 全新计划库：远端还没有共享清单
        self.write_local(LOCAL_EDIT, encoding="gbk")

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.PUSHED)
        copy = self.fresh_clone() / "shops.csv"
        self.assertTrue(copy.read_bytes().startswith(BOM))
        self.assertEqual([s.name for s in load_shops(copy)], ["店一", "店二"])
        self.assertTrue(self.local.read_bytes().startswith(BOM), "本机那份也统一成同一编码")

    def test_both_sides_missing_is_an_error(self):
        self.seed_shared(None)      # 全新计划库 ＋ 没有本机清单：两边都没有
        with self.assertRaises(shops_sync.ShopsSyncError) as ctx:
            self.sync()

        self.assertIn("shops.csv", str(ctx.exception))


class DegradationTests(ShopsSyncTestCase):
    """通道到不了或推不动：不拦开轮——用本机现值继续，把原因作为告警交出去。"""

    def test_unreachable_repo_warns_and_keeps_local(self):
        self.seed_shared(V1)
        self.write_local(V1)
        self.sync()
        self.write_local(LOCAL_EDIT)
        gone = self.box.tmp / "gone.git"
        self.box.must("remote", "set-url", "origin", self.box.sh_path(gone), cwd=self.plan)
        before = self.local.read_bytes()

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.UNREACHABLE)
        self.assertTrue(outcome.warning)
        self.assertEqual(self.local.read_bytes(), before)
        self.assertIn("does not appear to be a git repository", outcome.message)

    def test_missing_plan_clone_warns_and_keeps_local(self):
        """还没走上机清单第 9 步：计划库没 clone，本机照样开轮。"""
        self.seed_shared(None)
        shutil.rmtree(self.plan)    # 「计划库没 clone」是显式摆出来的空位（sync 靠它判 UNREACHABLE）
        self.write_local(V1)

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.UNREACHABLE)
        self.assertTrue(outcome.warning)
        self.assertTrue(self.local.exists())

    def test_missing_clone_and_missing_local_is_an_error(self):
        self.seed_shared(None)      # 全新计划库 ＋ 没有本机清单：两边都没有
        with self.assertRaises(shops_sync.ShopsSyncError):
            self.sync()

    def test_push_failure_warns_and_leaves_no_half_state(self):
        self.seed_shared(V1)
        self.write_local(V1)
        self.sync()
        self.write_local(LOCAL_EDIT)
        before = self.local.read_bytes()
        runs = self.box.tmp / "hook-runs"
        self.box.install_declining_hook(self.plan, runs)

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.PUSH_FAILED)
        self.assertTrue(outcome.warning)
        self.assertEqual(self.local.read_bytes(), before, "本机现值一分不动")
        self.assertEqual(self.shared_bytes().decode("utf-8-sig"), V1, "远端没被动过")
        self.assertEqual(self.box.must("log", "origin/main..HEAD", "--oneline", cwd=self.plan),
                         "", "克隆回到远端状态：不留下未推送的提交")
        self.assertEqual(self.box.must("status", "--porcelain", cwd=self.plan), "",
                         "工作区干净")

        # 通道修好后（钩子撤掉）下次同步直接成：本地改动还在，方向仍是「只本机变」
        self.box.remove_pre_push(self.plan)
        self.assertEqual(self.sync().action, shops_sync.SyncAction.PUSHED)
        self.assertEqual(self.shared_bytes().decode("utf-8-sig"), LOCAL_EDIT)

    def make_local_readonly(self) -> None:
        """把本机副本设成只读：模拟它被别的程序占着、写不进去。"""
        os.chmod(self.local, stat.S_IREAD)
        self.addCleanup(os.chmod, self.local, stat.S_IREAD | stat.S_IWRITE)

    def test_push_that_clashes_with_the_other_machine_warns_and_clears_the_clone(self):
        """两台机器在同一次推送窗口里改了同一行：rebase 撞车，告警收场。

        克隆不能卡在 rebase 现场——清干净，让下一轮以「两边都变」的形式把差异摆出来。
        """
        self.seed_shared(V1)
        self.write_local(V1)
        self.sync()
        self.write_local(CLASH_LOCAL)
        other = self.box.tmp / OTHER_CLONE
        self.box.write_files(other, {"shops.csv": CLASH_REPO})
        self.box.must("add", "--", "shops.csv", cwd=other)
        self.box.must("commit", "-m", "同一行的另一种改法", "--", "shops.csv", cwd=other)
        self.box.install_racing_hook(self.plan, other)

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.PUSH_FAILED)
        self.assertTrue(outcome.warning)
        self.assertEqual(self.box.must("status", "--porcelain", cwd=self.plan), "",
                         "克隆不能卡在 rebase 现场")
        self.assertEqual(self.box.must("log", "origin/main..HEAD", "--oneline", cwd=self.plan),
                         "", "不留下未推送的提交")
        self.assertEqual((self.plan / "shops.csv").read_text(encoding="utf-8"), CLASH_REPO,
                         "克隆回到远端状态")
        self.assertEqual(self.local.read_text(encoding="utf-8"), CLASH_LOCAL,
                         "本机现值一分不动")

        # 下一轮：方向判成「两边都变」，把两版差异摆到人面前
        self.box.remove_pre_push(self.plan)
        follow_up = self.sync()
        self.assertEqual(follow_up.action, shops_sync.SyncAction.CONFLICT)
        self.assertIn("-" + CLASH_LOCAL.splitlines()[-1], follow_up.diff)

    def test_push_lands_even_if_the_local_copy_cannot_be_rewritten(self):
        """副本写不动不该拦推送：库那边成了；本机副本统一编码这事下次再说。"""
        self.seed_shared(None)      # 全新计划库：远端还没有共享清单
        self.write_local(LOCAL_EDIT)
        self.make_local_readonly()

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.PUSHED)
        self.assertFalse(outcome.warning)
        self.assertIn("写不动", outcome.message)
        self.assertEqual(self.shared_bytes().decode("utf-8-sig"), LOCAL_EDIT)

    def test_pull_that_cannot_overwrite_the_local_copy_warns(self):
        self.seed_shared(V1)
        self.write_local(V1)
        self.sync()
        self.other_pushes(REPO_EDIT)
        before = self.local.read_bytes()
        self.make_local_readonly()

        outcome = self.sync()

        self.assertEqual(outcome.action, shops_sync.SyncAction.PULL_FAILED)
        self.assertTrue(outcome.warning)
        self.assertIn("写不动", outcome.message)
        self.assertEqual(self.local.read_bytes(), before, "本机现值一分不动")

        os.chmod(self.local, stat.S_IREAD | stat.S_IWRITE)   # 处理好之后：下次同步拉成
        self.assertEqual(self.sync().action, shops_sync.SyncAction.PULLED)
        self.assertEqual(self.local.read_text(encoding="utf-8-sig"), REPO_EDIT)


if __name__ == "__main__":
    unittest.main()
