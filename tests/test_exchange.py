"""票据 10：交换台一次运行与周报的验收测试。

接缝（spec「测试边界」）：

- **一次运行**（`exchange.run_once`）：产物 = 报告文件、本机库（导进来的行）、raw 库
  （发布的包）、退出码。世界照票据 08/09 的用例搭：真 git（本地裸库当远端）、真打包、
  真导入；图片库是系统边界（COS），用替身。
- **交换区的读口**（`export.week_packages` / `merge.package_shop_days`）：找包与读包的
  公开读口，用例直接对它们。
- **入口**（`exchange.main` 与窗口的 `Api`）：退出码与窗口接线，配替身。

报告形态的期望值对照 `.scratch/multi-machine-collection/prototype/07-exchange-report-sample*.md`
三份样例（干净 / 同周第二次 / 缺口与冲突）——只对结构与关键行，不逐字复制样例文案。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import io
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import exchange
from bestseller_monitor import db as dbmod
from bestseller_monitor import exchange as exchange_mod
from bestseller_monitor import export, judgment_set, merge, single_instance
from bestseller_monitor.analysis_store import DraftStore
from bestseller_monitor.config import ROLE_COLLECTOR, ROLE_MERGE_ONLY
from bestseller_monitor.db import CST
from bestseller_monitor.image_store import ImageStoreError
from bestseller_monitor.matching import prepare_cache
from helpers import crawler_cfg, group, insert_sku_image, ledger_of, member, store_weekly_plan
from tests import git_repos
from tests.git_repos import GitSandbox, WorldTemplate, git

WEEK = "2026-W38"
DAYS = ("2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
        "2026-09-19", "2026-09-20")
# 样例那次运行的同一天（W38 的周日晚上）：七天都算「该采到」。
RUN_AT = dt.datetime(2026, 9, 20, 19, 41, tzinfo=CST)
# 字面量哈希（不是从字节算出来的）：包里的 key 由哈希与 mime 推出来，期望值因此也是
# 字面量——不跟实现共算式（与 test_export 同一口径）。
IMG_HASH = "ab" + "1" * 62


def seed_judgments(cache, *, machine_id, rows=(("pair-1", "sig-1"),)) -> None:
    """往判断缓存里预置判断行（spec「预置缓存行」的口径，不调模型）。

    交换台这半不消费判断（消费在分析那半），所以结果正文只要是段 JSON、行能进包就行。
    """
    conn = sqlite3.connect(cache)
    try:
        prepare_cache(conn, machine_id)
        conn.executemany(
            "INSERT OR REPLACE INTO judgments(pair,signature,machine_id,evidence_a,evidence_b,"
            "result) VALUES (?,?,?,?,?,?)",
            [(pair, sig, machine_id, f"ev-{pair}-a", f"ev-{pair}-b", '{"same": true}')
             for pair, sig in rows])
        conn.commit()
    finally:
        conn.close()


def todo_of(report: str) -> str:
    """报告「下次该做什么」那一节的正文：节号随冲突节在不在（五 / 六）变。"""
    marker = "## 六、下次该做什么" if "## 六、下次该做什么" in report else "## 五、下次该做什么"
    return report.split(marker)[1]


class FakeImageStore:
    """图片库替身：只记 key 与字节，模拟「桶里已有什么」。取不到就报 ImageStoreError。"""

    def __init__(self, existing=()):
        self.existing = set(existing)
        self.uploaded: dict[str, bytes] = {}
        self.fetched: dict[str, bytes] = {}

    def existing_keys(self) -> set[str]:
        return set(self.existing)

    def upload(self, key: str, data: bytes) -> None:
        self.uploaded[key] = data
        self.existing.add(key)

    def fetch(self, key: str) -> bytes:
        if key in self.uploaded:
            return self.uploaded[key]
        if key in self.fetched:
            return self.fetched[key]
        raise ImageStoreError(f"桶里没有 {key}（替身）")


_SHOP_SEEDS = {
    "shops": (
        "INSERT INTO shops(shop_key, shop_name, shop_url, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(shop_key) DO NOTHING",
        lambda shop_key, day, at: (shop_key, f"店铺{shop_key}",
                                   f"https://{shop_key.lower()}.example/",
                                   f"{day}T{at}+08:00", f"{day}T{at}+08:00")),
    "products": (
        "INSERT INTO products(offer_id, product_url, product_name, main_image_url, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(offer_id) DO NOTHING",
        lambda shop_key, day, at: (f"{shop_key}-o1",
                                   f"https://detail.1688.com/offer/{shop_key}-o1.html",
                                   f"商品{shop_key}-o1", None,
                                   "2026-09-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00")),
    "skus": (
        "INSERT INTO skus(offer_id, sku_name, sku_id, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(offer_id, sku_id) DO NOTHING",
        lambda shop_key, day, at: (f"{shop_key}-o1", "规格一", "s1",
                                   "2026-09-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00")),
}


def seed_coverage(conn: sqlite3.Connection, coverage, *, observed_at="10:00:00",
                  image_hash: str | None = None) -> None:
    """按 (店铺, 日期) 覆盖表直插行：每店一个商品、一个规格、一条库存与版本行。

    `observed_at` 是这些行的当天采集时刻（拼到日期后面）；给了 `image_hash` 就带上
    图片（版本行引用它、资产表存字节）。重复的 (店铺, 日期) 由各表的冲突子句吞掉，
    可以增量地补。
    """
    for sql, row_of in _SHOP_SEEDS.values():
        conn.executemany(sql, [row_of(shop_key, day, observed_at)
                               for shop_key, day in coverage])
    conn.executemany(
        "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, price, "
        "shop_name, product_name, sku_name) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(shop_key, offer_id, sku_id, date) DO UPDATE SET "
        "stock=excluded.stock, price=excluded.price",
        [(shop_key, f"{shop_key}-o1", "s1", day, 5, 1.5, f"店铺{shop_key}",
          f"商品{shop_key}-o1", "规格一") for shop_key, day in coverage])
    if image_hash is not None:
        # 资产行先落：版本行的 content_hash 有外键指向它（导入侧要关外键走的正是这条）
        conn.execute(
            "INSERT OR IGNORE INTO product_image_assets(content_hash, mime, content) "
            "VALUES (?,?,?)", (image_hash, "image/jpeg", b"jpeg-bytes"))
    conn.executemany(
        "INSERT INTO product_information_versions(shop_key, offer_id, observed_at, "
        "observed_date, product_name, image_url, content_hash, image_error) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(shop_key, f"{shop_key}-o1", f"{day}T{observed_at}+08:00", day,
          f"商品{shop_key}-o1", None, image_hash, None) for shop_key, day in coverage])
    conn.commit()


def _machine_world_paths(kind: str, machine: str) -> dict[str, str]:
    """一件「某台机器的世界」里三样东西的相对路径：裸远端、交换区里的克隆、旁路工作克隆。

    名字与现搭时一模一样，`_world_template` 与 `ConsoleWorld` 都从这里取，免得两处各写一份。
    """
    if kind not in ("raw", "judged"):
        raise ValueError(f"不认识的世界种类：{kind}")
    suffix = "" if kind == "raw" else "-judged"
    return {"remote": f"{kind}-{machine}.git",
            "exchange": f"exchange/{kind}-{machine}",
            "work": f"{machine}{suffix}-work"}


_PLAN_PATHS = {"remote": "plan.git", "exchange": "exchange/plan", "work": "plan-work"}


def _build_machine_world(site: GitSandbox, *, remote: str, exchange: str,
                         work: str) -> list[str]:
    """搭一台机器的一套 git 小世界：空裸远端 + 交换区里的克隆 + 旁路工作克隆。

    三样东西的相对路径由调用方给（`_machine_world_paths`），远端真 init、克隆真 clone，
    与用例里现搭时逐字节同形，只是搬进了模板根。远端是空的（未出生 main），克隆出来既
    没有对象也没有 reflog，所以两个克隆逐字节相同——第二份直接用第一份的副本（纯文件
    复制），省一次 clone 子进程，结果与再 clone 一次一样。
    """
    (site.tmp / exchange).parent.mkdir(parents=True, exist_ok=True)
    bare = site.new_remote(remote)
    site.clone(bare, exchange)
    shutil.copytree(site.tmp / exchange, site.tmp / work)
    return [remote, exchange, work]


def _world_template(kind: str, machine: str) -> WorldTemplate:
    """取（必要时搭一次）一件世界模板：`raw` / `judged` 各半，每件 = 一台机器的那一套。"""
    names = _machine_world_paths(kind, machine)
    return WorldTemplate.obtain(f"exchange-{kind}-{machine}",
                                lambda site: _build_machine_world(site, **names))


def _plan_template() -> WorldTemplate:
    """取（必要时搭一次）计划库那一件模板：与一台机器的那套同形，只是不带机器名。

    模板里是空裸库 + 未出生的克隆（与上机清单里的形状一致）；种子提交由用的用例自己走
    正常发布路径（`seed`），所以「克隆出来还是未出生、pull 才把它落地」这一路照旧有覆盖。
    """
    return WorldTemplate.obtain("exchange-plan",
                                lambda site: _build_machine_world(site, **_PLAN_PATHS))


def _selftest_template() -> WorldTemplate:
    """取（必要时搭一次）原语用例用的那件小模板（四处共用，只搭一次）。"""
    return WorldTemplate.obtain(
        "exchange-selftest", lambda site: _build_machine_world(
            site, remote="raw-mt.git", exchange="exchange/raw-mt", work="mt-work"))


class WorldTemplateTests(unittest.TestCase):
    """「取世界」原语（`git_repos.WorldTemplate`）自己的小用例：票 01 的地基税改造架在它上面。

    钉四件事：同名模板在一个进程里只搭一次；取世界只复制（一个 git 子进程都不起）、同一件
    世界在一处只取一次；复制出来的副本互相独立、也碰不到模板；副本里的 git 是真的（远端还是
    未出生分支的空裸库、发布走正常克隆推送、钩子照旧可装可触发）。
    """

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


class ConsoleWorld:
    """一台机器跑交换台的小世界：三个 raw 库的裸远端 + 本机克隆 + 本机库 + 计划表。

    `publish()` 模拟别的机器发周包：经**另一个**克隆推送（交换区里那份要等交换台
    自己 pull 才到——与真实三机一致）。

    三台机器那套世界每测试进程只搭一次（`_world_template`），这里复制取独立副本：
    复制是纯文件操作，副本之间的推送互不可见（见 `git_repos.WorldTemplate`）。
    """

    def __init__(self, test: unittest.TestCase, *, machine_id="m1",
                 role=ROLE_COLLECTOR):
        self.test = test
        self.box = GitSandbox(test, prefix="bestseller-exchange-")
        self.root = self.box.tmp / "exchange"
        self.root.mkdir()                        # 交换区根：取模板之前就得在
        self.machine_id = machine_id
        self.role = role
        self.work: dict[str, Path] = {}          # 别的机器发布用的旁路克隆
        for machine in ("m1", "m2", "m3"):
            _world_template("raw", machine).take(self.box)     # 复制取副本（模板每进程搭一次）
            self.work[machine] = self.box.tmp / _machine_world_paths("raw", machine)["work"]
        self.db_path = self.box.tmp / f"{machine_id}.db"
        self.conn = dbmod.open(self.db_path)
        test.addCleanup(self.conn.close)
        self.store = FakeImageStore()
        # 判断集这半（票 05）：默认没配分析配置——要考它的用例调 analysis_config()／judged()
        self.analysis_path: Path | None = None
        self.cache: Path | None = None
        self.judgment_store: Path | None = None
        self.judged_remotes: dict = {}
        self.judged_work: dict = {}

    # ---- 世界搭建 ----

    def plan(self, *assignments) -> None:
        """把本周计划落进本机计划表：(shop_key, machine_id, pages)。"""
        store_weekly_plan(dbmod.Database(self.conn), WEEK, *assignments)

    def crawls(self, coverage, *, observed_at="10:00:00", round_reason=None) -> None:
        """本机在这些 (店铺, 日期) 上采到过（写本机库；给了 `round_reason` 就补当天轮次）。"""
        seed_coverage(self.conn, coverage, observed_at=observed_at)
        if round_reason is not None:
            days = sorted({day for _, day in coverage})
            self.conn.executemany(
                "INSERT OR IGNORE INTO rounds(id, run_date, terminal_reason, started_at, "
                "finished_at) VALUES (?,?,?,?,?)",
                [(int(day.replace("-", "")), day, round_reason, f"{day}T10:00:00+08:00",
                  f"{day}T10:05:00+08:00") for day in days])
            self.conn.commit()

    def round_on(self, day: str, reason: str | None) -> None:
        """补一条当天轮次事实（reason=None = 进行中，没有终态）。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO rounds(id, run_date, terminal_reason, started_at, "
            "finished_at) VALUES (?,?,?,?,?)",
            (int(day.replace("-", "")), day, reason, f"{day}T19:00:00+08:00",
             None if reason is None else f"{day}T19:30:00+08:00"))
        self.conn.commit()

    def publish(self, machine: str, coverage, *, week=WEEK, observed_at="10:00:00",
                image_hash: str | None = None):
        """别的机器发布一个周包（经它的旁路克隆推到裸库）。"""
        source = self.box.tmp / f"{machine}-source.db"
        conn = dbmod.open(source) if not source.exists() else sqlite3.connect(source)
        try:
            seed_coverage(conn, coverage, observed_at=observed_at, image_hash=image_hash)
        finally:
            conn.close()
        package = self.box.tmp / f"{week}-{machine}.db"
        export.build_package(source, package, week=week, machine_id=machine,
                             crawl_in_progress=False,
                             generated_at=dt.datetime(2026, 9, 20, 19, 0, tzinfo=CST))
        rel = export.package_rel_path(week, machine)
        self.box.commit_push(
            self.work[machine],
            {rel: gzip.compress(package.read_bytes(), mtime=0)},
            message=f"export {week}-{machine}")
        return package

    # ---- 判断集这半（票 05）：judged-* 库 + 分析配置 + 本机与别机的判断集 ----

    def judged(self, *machines) -> None:
        """给这些机器建 judged-<机器>：裸库当远端 + 交换区里的克隆 + 一个旁路克隆。

        旁路克隆是「那台机器自己发布」用的（与 raw 那半的 `self.work` 同一个意思）：
        交换区里那份要等交换台自己 pull 才到——与真实两机一致。名单口径是「交换区里
        实际存在的库」（`judgment_set.judged_repos`），所以按点名的机器一件一件取模板，
        不多带一件。
        """
        for machine in machines or ("m1", "m2", "m3"):
            _world_template("judged", machine).take(self.box)
            names = _machine_world_paths("judged", machine)
            self.judged_remotes[machine] = self.box.tmp / names["remote"]
            self.judged_work[machine] = self.box.tmp / names["work"]

    def analysis_config(self, *, cache=None, store=None) -> Path:
        """写一份分析配置：判断缓存与人工决定账本指向本机的两个库（交换台从它读）。"""
        self.cache = cache or self.box.tmp / "matching.sqlite"
        self.judgment_store = store or self.box.tmp / "analysis-drafts.sqlite"
        path = self.box.tmp / "config" / "analysis.toml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            "[analysis]\n"
            f'database = "{self.db_path.as_posix()}"\n'
            f'store = "{self.judgment_store.as_posix()}"\n'
            "\n[matching]\n"
            f'cache = "{self.cache.as_posix()}"\n',
            encoding="utf-8")
        self.analysis_path = path
        return path

    def judgments_local(self, rows=(("pair-1", "sig-1"),)) -> None:
        """往本机判断缓存里预置判断行（spec「预置缓存行」的口径，不调模型）。"""
        seed_judgments(self.cache, machine_id=self.machine_id, rows=rows)

    def decisions_local(self, ledger, *, machine=None, store=None) -> None:
        """往本机（或指定机器）的账本里写一版人工决定（与 `DraftStore.write` 同一条公开口）。"""
        DraftStore(store or self.judgment_store, machine or self.machine_id).write(
            "draft", "2026-09-01", "2026-09-02", "2026-09-22T20:00:00+08:00",
            {"products": []}, ledger)

    def judged_publish(self, machine, *, rows=(("pair-1", "sig-1"),), ledger=None) -> None:
        """别的机器发布判断集：经它的旁路克隆推到裸库（真打包，与票 03 同一条路）。

        包在 judged-<机器> 库里的相对路径就是 `PACKAGE_REL`（一个库一本、覆盖同名路径）。
        """
        cache = self.box.tmp / f"{machine}-cache.sqlite"
        store = self.box.tmp / f"{machine}-drafts.sqlite"
        seed_judgments(cache, machine_id=machine, rows=rows)
        if ledger is not None:
            DraftStore(store, machine).write("draft", "2026-09-01", "2026-09-02",
                                             "2026-09-22T20:00:00+08:00",
                                             {"products": []}, ledger)
        package = self.box.tmp / f"judgments-{machine}.db"
        judgment_set.build_package(cache, package, machine_id=machine,
                                   generated_at=dt.datetime(2026, 9, 20, 19, 0, tzinfo=CST),
                                   store=store)
        self.box.commit_push(self.judged_work[machine],
                             {judgment_set.PACKAGE_REL:
                              gzip.compress(package.read_bytes(), mtime=0)},
                             message=f"judged publish {machine}")

    def publish_broken_judged(self, machine, payload=b"not a judgment set at all") -> None:
        """别的机器那本 judged 库里放一个读不成判断集的包（坏包那一路）。"""
        self.box.commit_push(self.judged_work[machine],
                             {judgment_set.PACKAGE_REL: payload}, message=f"broken {machine}")

    def judged_local_bytes(self, machine: str, rel: str | None = None) -> bytes | None:
        """交换区里 judged-<机器> 那份克隆的 HEAD 里有没有这个文件（真 pull 之后可读）。"""
        from bestseller_monitor.git_channel import GitChannel
        return GitChannel(self.root / f"judged-{machine}").read_path(
            rel or judgment_set.PACKAGE_REL)

    # ---- 跑一次 ----

    def cfg(self, **overrides):
        values = dict(machine_id=self.machine_id, role=self.role,
                      exchange_root=self.root, db_file=self.db_path,
                      cos_bucket="", logs_dir=self.box.tmp / "logs")
        if self.analysis_path is not None:
            values["analysis_config"] = self.analysis_path
        values.update(overrides)
        return crawler_cfg(**values)

    def sync(self, *machines) -> None:
        """把交换区里的克隆拉到远端最新（模拟「上次运行拉过」——交换台自己只在汇总那半拉）。"""
        from bestseller_monitor.git_channel import GitChannel
        for machine in machines or ("m2", "m3"):
            GitChannel(self.root / f"raw-{machine}").pull()

    def make_read_only(self, *remotes: str) -> dict[str, Path]:
        """把这些裸库换成只读（服务端拒绝一切推送），返回各库的推送计数文件。

        只读部署公钥那一侧的形态（spec §11）：clone / pull 照常，写入被服务端拒。
        `remotes` 用裸库名（`raw-m1.git` / `plan.git`，与 `new_remote` 同一个叫法）。
        """
        counters: dict[str, Path] = {}
        for name in remotes:
            counter = self.box.tmp / f"{name}-push-attempts"
            self.box.install_read_only_remote(self.box.tmp / name, counter)
            counters[name] = counter
        return counters

    def run(self, **kwargs):
        kwargs.setdefault("week", WEEK)
        kwargs.setdefault("now", RUN_AT)
        kwargs.setdefault("store", self.store)
        return exchange_mod.run_once(self.cfg(), **kwargs)

    def report(self, week=WEEK) -> str:
        return (self.root / "报告" / f"{week}.md").read_text(encoding="utf-8")

    def published_bytes(self, repo: str, rel: str) -> bytes | None:
        from bestseller_monitor.git_channel import GitChannel
        return GitChannel(self.root / repo).read_path(rel)


class CleanRunTests(unittest.TestCase):
    """样例一（干净）：全链跑通、退出码 0、报告与原型同形。"""

    def setUp(self):
        self.world = ConsoleWorld(self)
        self.world.plan(("A01", "m1", 3), ("A02", "m2", 3), ("A03", "m3", 3))
        self.world.crawls([("A01", day) for day in DAYS])
        self.world.publish("m2", [("A02", day) for day in DAYS])
        self.world.publish("m3", [("A03", day) for day in DAYS])

    def test_a_clean_run_publishes_imports_and_writes_the_weekly_report(self):
        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 0)
        report = self.world.report()
        self.assertTrue(report.startswith("# 库存数据交换周报 · 2026-W38\n"))
        self.assertIn("本机 **m1**（采集机）· 2026-09-20 19:41 运行 · 覆盖 9月14日 – 9月20日",
                      report)
        self.assertIn("**结果**：干净（退出码 0）· 缺口 0 · 冲突 0 · 还没来的包 0 · "
                      "判断集冲突 0", report)
        self.assertIn("**本次运行**：导出已发布 · 新收 2 个包（34 行）", report)
        for title in ("## 一、本机发布", "## 二、收进来的包", "## 三、判断集",
                      "## 四、缺口与还没来的", "## 五、下次该做什么"):
            self.assertIn(title, report)
        self.assertNotIn("## 五、冲突", report)     # 干净场景不出现冲突节（样例的同形）
        self.assertIn("W38-m1.db.gz", report)
        self.assertIn("- 本机负责：A01", report)
        self.assertIn("- 行数：inventory 7 · products 1 · skus 1 · 版本 7 · SKU 图 0", report)
        self.assertIn("| raw-m2 | W38 | 17 | 17 | 0 | 0 | 0（0.0 MB） | 已导入 |", report)
        self.assertIn("| raw-m3 | W38 | 17 | 17 | 0 | 0 | 0（0.0 MB） | 已导入 |", report)
        self.assertIn("1. 没有待办：本周干净", report)

    def test_the_run_publishes_the_package_and_merges_the_received_rows(self):
        outcome = self.world.run()

        # raw-m1 的远端拿到本机周包（本地裸库当远端，真跑 git）
        published = self.world.published_bytes("raw-m1", export.package_rel_path(WEEK, "m1"))
        self.assertIsNotNone(published)
        # 本机库吃到两个包的行：A02、A03 的库存行都在
        rows = self.world.conn.execute(
            "SELECT DISTINCT shop_key FROM inventory WHERE date BETWEEN '2026-09-14' AND "
            "'2026-09-20' ORDER BY shop_key").fetchall()
        self.assertEqual([r[0] for r in rows], ["A01", "A02", "A03"])
        # 幂等账记着两个包（本机的包不入账——只收别人的）
        packages = self.world.conn.execute(
            "SELECT machine_id, week FROM import_packages ORDER BY machine_id").fetchall()
        self.assertEqual([tuple(r) for r in packages], [("m2", WEEK), ("m3", WEEK)])
        self.assertEqual(len(outcome.imports), 2)


class SkuImageLedgerCase(unittest.TestCase):
    """SKU 图这半的共用夹具：一趟干净世界 + 往本机库里直插 SKU 图流水行。"""

    def setUp(self):
        self.world = ConsoleWorld(self)
        self.world.plan(("A01", "m1", 3), ("A02", "m2", 3), ("A03", "m3", 3))
        self.world.crawls([("A01", day) for day in DAYS])
        self.world.publish("m2", [("A02", day) for day in DAYS])
        self.world.publish("m3", [("A03", day) for day in DAYS])

    def add_sku_image(self, day, *, content_hash=None, image_error=None,
                      source="专属图") -> None:
        """直插一条 A01 那件商品的 SKU 图流水行；给了哈希就连字节一起进资产池。"""
        if content_hash is not None:
            self.world.conn.execute(
                "INSERT OR IGNORE INTO product_image_assets(content_hash, mime, content) "
                "VALUES (?,?,?)", (content_hash, "image/jpeg", b"sku-jpeg-bytes"))
        insert_sku_image(self.world.conn, day=day, shop_key="A01", offer_id="A01-o1",
                         sku_id="s1", content_hash=content_hash, image_error=image_error,
                         source=source, url="https://img.example/sku.jpg")


class SkuImageReportTests(SkuImageLedgerCase):
    """周报带 SKU 图：行数一行纳入流水计数，失败的进待办——空图与代填不是失败。"""

    def test_the_report_counts_the_ledger_rows(self):
        self.add_sku_image(DAYS[0], content_hash=IMG_HASH)
        self.add_sku_image(DAYS[1], source="无图")

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.export.rows["sku_image_versions"], 2)
        self.assertIn("- 行数：inventory 7 · products 1 · skus 1 · 版本 7 · SKU 图 2",
                      self.world.report())

    def test_failed_sku_images_go_to_the_todo_with_the_retry_entry(self):
        self.add_sku_image(DAYS[0], content_hash=IMG_HASH, source="主图代填")
        self.add_sku_image(DAYS[1], source="无图")
        self.add_sku_image(DAYS[2], image_error="超时")

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 0)
        todo = todo_of(self.world.report())
        self.assertIn("1 条 SKU 图下载失败", todo)
        self.assertIn("python -m bestseller_monitor.product_images", todo)
        self.assertIn("--database", todo, "照抄能跑：命令要带必给的 --database")
        self.assertIn("--retry-sku-image", todo, "待办点到重试命令的 SKU 图入口（票 03 的形状）")

    def test_empty_and_filled_sku_images_are_not_failures(self):
        self.add_sku_image(DAYS[0], content_hash=IMG_HASH, source="主图代填")
        self.add_sku_image(DAYS[1], source="无图")

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 0)
        report = self.world.report()
        self.assertIn("1. 没有待办：本周干净", report)
        self.assertNotIn("SKU 图下载失败", report)


class SecondRunTests(unittest.TestCase):
    """样例二（同周第二次）：导出无新提交、重复包标「已导入过 → 跳过」、报告重写同一份。"""

    def setUp(self):
        self.world = ConsoleWorld(self)
        self.world.plan(("A01", "m1", 3), ("A02", "m2", 3), ("A03", "m3", 3))
        self.world.crawls([("A01", day) for day in DAYS])
        self.world.publish("m2", [("A02", day) for day in DAYS])
        self.world.publish("m3", [("A03", day) for day in DAYS])
        self.first = self.world.run()
        self.first_report = self.world.report()

    def test_second_run_rewrites_one_report_and_marks_skipped_packages(self):
        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 0)
        report = self.world.report()
        self.assertIn("**本次运行**：导出：本周包上次已发布（无新提交） · 跳过 2 个已导入",
                      report)
        self.assertIn("| raw-m2 | W38 | 17 | — | — | — | — | 已导入过 → 跳过 |", report)
        self.assertIn("| raw-m3 | W38 | 17 | — | — | — | — | 已导入过 → 跳过 |", report)
        self.assertNotEqual(report, self.first_report, "同周重跑要重写同一份（不是两份相加）")
        # 一周一份：报告目录里只有这一个文件
        reports = sorted(path.name for path in (self.world.root / "报告").iterdir())
        self.assertEqual(reports, ["2026-W38.md"])
        # 「第一次发的包」还在（状态累积）：本机发布一节仍是这次运行的事实
        self.assertIn("- 包：W38-m1.db.gz", report)


class WeeklyReportTests(unittest.TestCase):
    """样例三（缺口与冲突）：五节齐全、退出码 1、缺口/冲突/还没来的包三条都在。"""

    def setUp(self):
        self.world = ConsoleWorld(self)
        # A02 归 m3（本机多采它 → 冲突）、A04 归 m2（它的包一直没来）
        self.world.plan(("A01", "m1", 3), ("A02", "m3", 3), ("A03", "m1", 3), ("A04", "m2", 3))
        # 本机：A01 七天、A03 缺 09-17（详情预算耗尽中止）、09-15 越权多采了 A02
        self.world.crawls([("A01", day) for day in DAYS], round_reason="COMPLETED")
        self.world.crawls([("A03", day) for day in DAYS if day != "2026-09-17"],
                          round_reason="COMPLETED")
        self.world.round_on("2026-09-17", "DETAIL_BUDGET_EXHAUSTED")
        self.world.crawls([("A02", "2026-09-15")], observed_at="15:02:00")
        self.world.publish("m3", [("A02", day) for day in DAYS], observed_at="16:40:00")

    def test_a_week_with_gaps_and_conflicts_renders_the_full_sample_shape(self):
        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 1)
        report = self.world.report()
        self.assertIn("**结果**：有需要人看一眼的地方（退出码 1）· 缺口 1 · 冲突 1 · "
                      "还没来的包 1 · 判断集冲突 0", report)
        self.assertIn("**本次运行**：导出已发布 · 新收 1 个包", report)
        # 四、缺口与还没来的：本机缺一天（带轮次终态的人话）+ 还没收到的包 + 口径
        self.assertIn("- 本机缺一天：A03 09-17 —— 当天采集在详情预算耗尽后中止"
                      "（历史日期补不了，如实记一笔）", report)
        self.assertIn("- 还没收到的包：raw-m2 的 W38 还没发布或没拉到", report)
        self.assertIn("- 口径：缺口 = 计划里该采到的（店铺 × 日期），在收到的包里找不到",
                      report)
        # A04 归 m2（包没来）：它的七天不记缺口，只记「还没来的包」——A04 整个报告不出现
        self.assertNotIn("A04", report)
        # 五、冲突：本机（计划外多采）与计划机，取后到者、覆盖与保留的行数按账里的事实
        # （本机那组两行：库存行同键被替换＝覆盖 1 行；版本行的 observed_at 不同键、留着＝保留 1 行）
        self.assertIn("- 重复采集：A02 09-15：本机 15:02（计划外多采）与计划机 16:40 都采到；"
                      "取后到者（m3），覆盖 1 行、保留 1 行", report)
        self.assertIn("- 明细记在本机导入账（本机视角：导入 raw-m3 时发现）；冲突不入交换区",
                      report)
        # 六、下次该做什么：三件（还没来的包 / 别再勾选 A02 / 本机缺的一天）
        todo = todo_of(report)
        self.assertIn("raw-m2 的 W38 还没发布或没拉到", todo)
        self.assertIn("提醒本机操作者：不要手工勾选本期不归本机的店（A02）", todo)
        self.assertIn("本机缺一天（A03 09-17 —— 当天采集在详情预算耗尽后中止）无法回填",
                      todo)

    def test_other_machines_gaps_come_from_their_packages_coverage(self):
        # m3 的包缺 A02 09-16 那天（当天采集中止）：计划该采到、包里没有 → 缺口
        world = ConsoleWorld(self)
        world.plan(("A01", "m1", 3), ("A02", "m3", 3))
        world.crawls([("A01", day) for day in DAYS], round_reason="COMPLETED")
        world.publish("m3", [("A02", day) for day in DAYS if day != "2026-09-16"])

        outcome = world.run()

        self.assertEqual(outcome.exit_code, 1)
        report = world.report()
        self.assertIn("- raw-m3 缺一天：A02 09-16 —— 计划里该采到，收到的包里没有这一天",
                      report)
        todo = todo_of(report)
        self.assertIn("raw-m3 的包缺一天（A02 09-16）", todo)


class OnlyHalfTests(unittest.TestCase):
    """`--only export|merge` 两个方向各跑一半：另一半真的没动。"""

    def setUp(self):
        self.world = ConsoleWorld(self)
        self.world.plan(("A01", "m1", 3), ("A03", "m3", 3))
        self.world.crawls([("A01", day) for day in DAYS])
        self.world.publish("m3", [("A03", day) for day in DAYS])

    def test_only_export_publishes_and_leaves_the_merge_untouched(self):
        self.world.sync()                    # 上次拉过、这次只发不汇总：包在本地但没有账
        outcome = self.world.run(only="export")

        self.assertEqual(outcome.exit_code, 0)
        self.assertIsNotNone(self.world.published_bytes(
            "raw-m1", export.package_rel_path(WEEK, "m1")))
        self.assertEqual(self.world.conn.execute(
            "SELECT COUNT(*) FROM import_packages").fetchone()[0], 0)
        self.assertIsNone(self.world.conn.execute(
            "SELECT 1 FROM inventory WHERE shop_key='A03'").fetchone())
        report = self.world.report()
        self.assertIn("**本次运行**：只跑了导出 · 导出已发布", report)
        self.assertIn("| raw-m3 | W38 | — | — | — | — | — | 还没汇总 |", report)

    def test_only_merge_collects_without_exporting(self):
        outcome = self.world.run(only="merge")

        self.assertEqual(outcome.exit_code, 0)
        self.assertIsNone(self.world.published_bytes(
            "raw-m1", export.package_rel_path(WEEK, "m1")))
        self.assertEqual(len(outcome.imports), 1)
        report = self.world.report()
        self.assertIn("**本次运行**：只跑了汇总 · 新收 1 个包（17 行）", report)
        self.assertIn("- 导出：本次只跑了汇总（--only merge），没做导出", report)


class AnalysisPathDefaultsTests(unittest.TestCase):
    """交换台只读分析配置的两个键（不整份加载），缺省名必须与分析那半一致。

    两处各写一遍是故意的（见 `exchange.judgment_paths` 的 docstring：交换台不该被分析配置
    里别的段落挡住）；这条护栏让「写岔了」变成一次红灯，而不是悄悄指向另一个库。
    """

    def test_the_default_names_match_what_the_analysis_half_would_resolve(self):
        # 延迟 import：只这条护栏依赖分析那半，别的用例不因它受牵连
        from bestseller_monitor.analysis import AnalysisConfig

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.toml"
            path.write_text('[analysis]\ndatabase = "stock.db"\n\n[matching]\nmode = "direct"\n',
                            encoding="utf-8")
            config = AnalysisConfig.from_file(path)

        self.assertEqual(config.matching.cache,
                         (Path(tmp) / exchange_mod.ANALYSIS_CACHE_DEFAULT).resolve())
        self.assertEqual(config.store,
                         (Path(tmp) / exchange_mod.ANALYSIS_STORE_DEFAULT).resolve())


class JudgmentSetCase(unittest.TestCase):
    """票 05 的共用夹具：一趟干净的数据交换（无缺口无冲突）＋ 判断集这半就位。

    本机是 m1：判断缓存里预置两行判断、账本按用例给；judged-m1/m2/m3 三本库都建好。
    """

    def setUp(self):
        self.world = ConsoleWorld(self)
        self.world.plan(("A01", "m1", 3), ("A02", "m2", 3), ("A03", "m3", 3))
        self.world.crawls([("A01", day) for day in DAYS])
        self.world.publish("m2", [("A02", day) for day in DAYS])
        self.world.publish("m3", [("A03", day) for day in DAYS])
        self.world.judged()                  # judged-m1（本机那本）+ judged-m2/m3
        self.world.analysis_config()         # 判断缓存与账本的位置（analysis.toml）
        self.world.judgments_local(rows=(("pair-1", "sig-1"), ("pair-2", "sig-2")))

    def cache_rows(self):
        """本机缓存里的判断行 (pair, 来源机器) 与导入账的来源；导入账还没建就是空的。"""
        with contextlib.closing(sqlite3.connect(self.world.cache)) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            rows = conn.execute(
                "SELECT pair, machine_id FROM judgments ORDER BY pair").fetchall()
            sources = ([row[0] for row in conn.execute(
                "SELECT source_machine FROM judgment_imports ORDER BY rowid")]
                if "judgment_imports" in tables else [])
        return [tuple(row) for row in rows], sources

    def draft_store(self):
        return DraftStore(self.world.judgment_store, "m1")


class JudgmentSetTests(JudgmentSetCase):
    """整趟里判断集的两条动作（ADR-0039 决策 7）：发布与收取都在之内；周报第三节记账。"""

    def test_a_full_run_publishes_our_set_and_collects_the_others(self):
        self.world.judged_publish("m2", rows=(("m2-pair", "m2-sig"),))

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        self.assertIsNotNone(self.world.judged_local_bytes("m1"), "本机那本真收到了包")
        self.assertIsNotNone(self.world.judged_local_bytes("m2"), "收取那半拉过了别家那本")
        rows, sources = self.cache_rows()
        self.assertEqual(rows, [("m2-pair", "m2"), ("pair-1", "m1"), ("pair-2", "m1")])
        self.assertEqual(sources, ["m2"])
        report = self.world.report()
        self.assertIn("## 三、判断集", report)
        self.assertIn("- 发布：判断 2 条 · 视觉描述 0 条 · 证据 0 条 · 人工决定 0 条 → judged-m1 @ ",
                      report)
        self.assertIn("- 收取：judged-m2 —— 判断 1 条 · 视觉描述 0 条 · 证据 0 条 · 人工决定 0 条"
                      "（新增 1 行）", report)
        self.assertIn("- 收取：judged-m3 没有判断集可收", report)
        self.assertIn("- 采纳：没有（收到的判断集里没有新的人工决定）", report)
        self.assertIn("- 冲突：没有", report)
        self.assertIn("**本次运行**：导出已发布 · 发布判断 2 条 · 新收 2 个包（34 行）"
                      " · 收下 1 份判断集", report)

    def test_a_confirmed_group_from_another_machine_is_adopted_and_named(self):
        """判据 A04：m2 确认一组 → 本机收取后该组已是本机账本里的已确认组，周报点名来源。"""
        self.world.judged_publish("m2", rows=(),
                                  ledger=ledger_of(relations=[group("o1", "o2", machine="m2")]))

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        report = self.world.report()
        self.assertIn("- 收取：judged-m2 —— 判断 0 条 · 视觉描述 0 条 · 证据 0 条 · 人工决定 1 条"
                      "（新增 1 行）", report)
        self.assertIn("- 采纳：1 条人工决定（来自 m2）—— 已进本机账本", report)
        relations = self.draft_store().ledger()["relations"]
        self.assertEqual([row["members"] for row in relations],
                         [[[member("o1"), "vo1"], [member("o2"), "vo2"]]])
        self.assertEqual(relations[0]["machine_id"], "m2", "来源随行带走")

    def test_a_new_conflict_asks_for_a_look_and_is_named_on_both_sides(self):
        # 本机排除过 (o1, o2)，m2 把它们并进同一组：一边并组、一边排除（spec §6 第一类）
        self.world.decisions_local(ledger_of(excluded=[[member("o1"), member("o2")]]))
        self.world.judged_publish("m2", rows=(),
                                  ledger=ledger_of(relations=[group("o1", "o2", machine="m2")]))

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 1, "这趟新记下的冲突：有需要人看一眼的")
        report = self.world.report()
        self.assertIn("· 判断集冲突 1", report)
        self.assertIn("- 冲突：一边并组、一边排除 —— 外来的 m2 的 组 vs 本机的 m1 的 排除对"
                      f"（商品 {member('o1')}、{member('o2')}；在分析页面上裁决）", report)
        self.assertEqual([c.kind for c in self.draft_store().conflicts()], ["group_vs_exclusion"])

    def test_a_second_run_skips_the_collected_set_and_forwards_it_on(self):
        """收进来的判断进了本机判断集：下一次发布把它带上（内容身份不变，票 03 的转发口径）；
        那之后本机判断集不再变，发布才是空操作。"""
        self.world.judged_publish("m2", rows=(("m2-pair", "m2-sig"),))
        self.world.run()

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        report = self.world.report()
        self.assertIn("- 发布：判断 3 条 · 视觉描述 0 条 · 证据 0 条 · 人工决定 0 条 → judged-m1 @ ",
                      report)
        self.assertIn("- 收取：judged-m2 —— 已收过", report)
        self.assertIn("跳过 1 份已收过的判断集", report)

        third = self.world.run()

        self.assertEqual(third.exit_code, 0, third.failures)
        self.assertIn("与已发布那份同内容（无新提交）", self.world.report())


class JudgmentActionAloneTests(JudgmentSetCase):
    """两个动作可单独运行且互不依赖（票 05 的验收线）。"""

    def setUp(self):
        super().setUp()
        self.world.judged_publish("m2", rows=(("m2-pair", "m2-sig"),))
        # 上次运行拉过（与 OnlyHalfTests 同一条约定）：只跑一个动作时不带拉取，
        # 别家的周包要在本地克隆里看得到，退出码才只反映这个动作本身。
        self.world.sync()

    def test_publish_alone_leaves_both_other_halves_untouched(self):
        outcome = self.world.run(only="publish")

        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        self.assertIsNotNone(self.world.judged_local_bytes("m1"), "发布照常")
        # 数据那半没动：没导出、没汇总；判断那半的收取也没动
        self.assertIsNone(self.world.published_bytes("raw-m1", export.package_rel_path(WEEK, "m1")))
        self.assertEqual(self.world.conn.execute(
            "SELECT COUNT(*) FROM import_packages").fetchone()[0], 0)
        self.assertEqual(self.cache_rows()[1], [])
        report = self.world.report()
        self.assertIn("- 导出：本次只跑了发布判断集（--only publish），没做导出", report)
        self.assertIn("- 收取：本次只跑了发布判断集（--only publish），没做收取判断集", report)

    def test_collect_alone_brings_the_set_without_publishing_ours(self):
        outcome = self.world.run(only="collect")

        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        self.assertEqual(self.cache_rows()[1], ["m2"])
        self.assertIsNone(self.world.judged_local_bytes("m1"), "本机那本这次没发")
        report = self.world.report()
        self.assertIn("- 发布：本次只跑了收取判断集（--only collect），没做发布判断集", report)

    def test_a_merge_only_machine_still_publishes_its_judgments(self):
        """纯汇总机跳过的是采集包的导出，判断集照发（spec §9、ADR-0039 决策 3）。"""
        world = ConsoleWorld(self, machine_id="m4", role=ROLE_MERGE_ONLY)
        world.publish("m2", [("A02", day) for day in DAYS])
        world.publish("m3", [("A03", day) for day in DAYS])
        world.judged("m4", "m2", "m3")
        world.analysis_config()
        world.judgments_local(rows=(("pair-1", "sig-1"),))

        outcome = world.run()

        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        self.assertIsNotNone(world.judged_local_bytes("m4"), "转角色不影响发布")
        report = world.report()
        self.assertIn("- 导出：本机是纯汇总机，跳过", report)
        self.assertIn("- 发布：判断 1 条 · 视觉描述 0 条 · 证据 0 条 · 人工决定 0 条 → judged-m4 @ ",
                      report)


class JudgmentTroubleTests(JudgmentSetCase):
    """判断集这半的失败档位（与采集侧同规）与「不中止其余步骤」。"""

    def test_a_missing_judged_repo_is_a_note_a_readable_failure_and_the_rest_runs(self):
        self.world.judged_publish("m2", rows=(("m2-pair", "m2-sig"),))
        shutil.rmtree(self.world.root / "judged-m1")      # 本机那本没 clone 上（第 14 步）

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 2, "发布没成 = 本机没做成事（与「导出没成功」同规）")
        notes = "\n".join(outcome.check.notes)
        self.assertIn("judged-m1", notes)
        self.assertIn("上机清单第 14 步", notes)
        report = self.world.report()
        self.assertIn("- 发布：判断 2 条 · 视觉描述 0 条 · 证据 0 条 · 人工决定 0 条 —— 没发出去（",
                      report)
        self.assertEqual(self.cache_rows()[1], ["m2"], "发布的失败不挡收取")
        self.assertIn("本机判断集没发出去（见第三节）：通道或克隆修好后重跑一次",
                      todo_of(report))

    def test_a_broken_source_set_is_a_hard_failure_and_the_others_still_come_in(self):
        self.world.publish_broken_judged("m2")
        self.world.judged_publish("m3", rows=(("m3-pair", "m3-sig"),))

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 2, "包读不出来 = 没收成（与「有包没导成」同规）")
        report = self.world.report()
        self.assertIn("- 收取：judged-m2 这本没收成（", report)
        self.assertIn("- 收取：judged-m3 —— 判断 1 条", report)
        self.assertEqual(self.cache_rows()[1], ["m3"], "坏的那家不挡别家")
        self.assertIn("judged-m2 的判断集没导成（见第三节）", todo_of(report))

    def test_an_unreachable_source_asks_for_a_look_without_stopping_the_others(self):
        self.world.judged_publish("m2", rows=(("m2-pair", "m2-sig"),))
        self.world.box.must("remote", "set-url", "origin",
                            str(self.world.box.tmp / "gone.git"),
                            cwd=self.world.root / "judged-m3")

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 1, "拉不动 = 通道没拉成（软，与采集侧同规）")
        report = self.world.report()
        self.assertIn("- 收取：judged-m3 这本拉不动（", report)
        self.assertEqual(self.cache_rows()[1], ["m2"], "坏的那家不挡别家")
        self.assertIn("judged-m3 拉不动（见第三节）：通道恢复后重跑一次", todo_of(report))

    def test_without_an_analysis_config_it_says_so_and_changes_nothing_else(self):
        world = ConsoleWorld(self)          # 不写 analysis.toml：判断集这半整趟没做
        world.plan(("A01", "m1", 3), ("A03", "m3", 3))
        world.crawls([("A01", day) for day in DAYS])
        world.publish("m3", [("A03", day) for day in DAYS])

        outcome = world.run()

        self.assertEqual(outcome.exit_code, 0, "配置的事不折成交换台的失败（同 cos_bucket 的处置）")
        self.assertIn("判断集这半没做", "\n".join(outcome.check.notes))
        report = world.report()
        self.assertIn("- 这趟没做：本机还没有分析配置（", report)
        self.assertIn("analysis.toml", report)
        self.assertIn("**本次运行**：导出已发布 · 新收 1 个包（17 行）", report)


class ExitCodeTwoTests(unittest.TestCase):
    """退出码 2：本机没做成事——导出没发成、或纯汇总机被要求只导出。"""

    def test_a_failed_export_reports_two_but_still_collects(self):
        world = ConsoleWorld(self)
        world.plan(("A01", "m1", 3), ("A03", "m3", 3))
        world.crawls([("A01", day) for day in DAYS])
        world.publish("m3", [("A03", day) for day in DAYS])
        shutil.rmtree(world.root / "raw-m1")      # 本机的 raw 库没 clone 上（上机清单第 9 步）

        outcome = world.run()

        self.assertEqual(outcome.exit_code, 2)
        report = world.report()
        self.assertIn("**结果**：本机没做成事（退出码 2）", report)
        self.assertIn("- 包：W38-m1.db.gz 这次没发出去（", report)
        # 硬失败不拦汇总这半：m3 的包照样收进来了
        self.assertEqual(len(outcome.imports), 1)
        self.assertIn("1. 导出没成功（见第一节）：修好通道后重跑一次",
                      todo_of(report))

    def test_merge_only_machine_asked_to_only_export_did_nothing(self):
        world = ConsoleWorld(self, machine_id="m4", role=ROLE_MERGE_ONLY)
        world.plan(("A01", "m1", 3))
        world.publish("m2", [("A02", day) for day in DAYS])

        outcome = world.run(only="export")

        self.assertEqual(outcome.exit_code, 2)
        self.assertIn("- 导出：本机是纯汇总机，跳过", world.report())


class MergeOnlyMachineTests(unittest.TestCase):
    """纯汇总机：导出跳过并明示，汇总与报告照常（spec §7；票 11 的档位在本票先跑通）。"""

    def setUp(self):
        self.world = ConsoleWorld(self, machine_id="m4", role=ROLE_MERGE_ONLY)
        self.world.publish("m2", [("A02", day) for day in DAYS])
        self.world.publish("m3", [("A03", day) for day in DAYS])

    def test_export_entry_is_kept_with_the_skip_note_and_merge_still_runs(self):
        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 0)
        report = self.world.report()
        self.assertIn("本机 **m4**（纯汇总机）", report)
        self.assertIn("- 导出：本机是纯汇总机，跳过", report)
        self.assertIn("- 本机负责：无（纯汇总机，不参与计划）", report)
        self.assertIn("**本次运行**：导出：本机是纯汇总机，跳过 · 新收 2 个包（34 行）", report)
        self.assertEqual(len(outcome.imports), 2)
        # 没有落库计划（纯汇总机不跑准备串）：缺口与「还没来的包」如实说没法对照
        self.assertIn("- 本机没有本周（2026-W38）的落库计划", report)


class ReadOnlyCredentialRunTests(unittest.TestCase):
    """只读凭据下的一轮完整运行（票据 11，spec §11）：clone / pull / 汇总照常，一个 push 都不试。

    「push 被拒 = 只读档位配置正确」：把几个裸库都换成只读（服务端 pre-receive 拒绝
    一切写入，push 通道在 test_git_channel 里单独有实证），这一轮跑完它们一次都没被
    碰过——纯汇总机的正常一轮里根本没有写交换区的动作。
    """

    def setUp(self):
        self.world = ConsoleWorld(self, machine_id="m4", role=ROLE_MERGE_ONLY)
        self.world.publish("m2", [("A02", day) for day in DAYS])
        self.world.publish("m3", [("A03", day) for day in DAYS])
        _plan_template().take(self.world.box)   # 纯汇总机不跑准备串：plan 靠拉取保鲜
        # 种子提交走正常发布路径；本机那份 plan 克隆取来时还没出生，等这一趟 pull 落地
        self.world.box.seed(self.world.box.tmp / "plan.git",
                            {"machines.json": '["m1", "m2", "m3"]\n'})

    def test_the_whole_run_never_writes_to_the_exchange(self):
        counters = self.world.make_read_only("raw-m1.git", "raw-m2.git", "raw-m3.git", "plan.git")

        outcome = self.world.run()

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(len(outcome.imports), 2)
        self.assertEqual(self.world.conn.execute(
            "SELECT COUNT(DISTINCT shop_key || date) FROM inventory").fetchone()[0], 14,
            "两个包的店 × 天（2 × 7）都汇总进来了")
        report = self.world.report()
        self.assertIn("- 导出：本机是纯汇总机，跳过", report)
        self.assertIn("| raw-m2 | W38 | 17 | 17 | 0 | 0 | 0（0.0 MB） | 已导入 |", report)
        # plan 那半也拉到了：本机那份克隆取来还是未出生的，这一趟 pull 把种子落进工作区
        self.assertEqual((self.world.root / "plan" / "machines.json").read_text(
            encoding="utf-8"), '["m1", "m2", "m3"]\n')
        for name, counter in counters.items():
            self.assertFalse(counter.exists(), f"{name} 被推过：只读档位下不该有写交换区的动作")
        self.assertFalse((self.world.root / "outbox").exists(), "导出跳过：连包都不该打")


class WeekWindowTests(unittest.TestCase):
    """指定周窗口（补历史）：导出与报告都按那一周走。"""

    def test_backfilling_a_past_week_imports_its_packages_too(self):
        world = ConsoleWorld(self)
        world.publish("m2", [("A02", "2026-09-09")], week="2026-W37")

        outcome = world.run(week="2026-W37")

        self.assertEqual(outcome.exit_code, 0)        # 收齐了、没别的毛病（只是没落库计划可对照）
        self.assertTrue((world.root / "报告" / "2026-W37.md").exists())
        self.assertEqual(world.conn.execute(
            "SELECT week FROM import_packages").fetchone()[0], "2026-W37")


class ColdStartReplayTests(unittest.TestCase):
    """冷启动重放（spec §11；纯汇总机清单第 6 步）：一轮把账里没见过的包全收进来。

    「新机器从交换区重放全部包」是一次冷启动的事：只收本周的话，全新纯汇总机得人按周
    逐周跑一遍才收敛，报告里的「收进来的包」也永远只有本周那一行。历史里已经收过的
    包不重进报告表（状态累积以本周为焦点），别的机器迟到补发的历史包下次运行自动补上。
    """

    def test_one_cold_start_run_replays_every_package_in_the_area(self):
        world = ConsoleWorld(self, machine_id="m4", role=ROLE_MERGE_ONLY)
        world.publish("m1", [("A01", "2026-09-05")], week="2026-W36")
        world.publish("m1", [("A01", "2026-09-09")], week="2026-W37")
        world.publish("m2", [("A02", day) for day in DAYS])
        world.sync("m1", "m2", "m3")

        outcome = world.run()

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(len(outcome.imports), 3)
        report = world.report()
        # 清单第 6 步的判据形态：收进来的包 = 交换区全部（含历史周）
        self.assertIn("| raw-m1 | W36 |", report)
        self.assertIn("| raw-m1 | W37 |", report)
        self.assertIn("| raw-m2 | W38 |", report)
        self.assertIn("**本次运行**：导出：本机是纯汇总机，跳过 · 新收 3 个包", report)
        ledger = world.conn.execute(
            "SELECT week FROM import_packages ORDER BY week").fetchall()
        self.assertEqual([row[0] for row in ledger], ["2026-W36", "2026-W37", "2026-W38"])

    def test_a_second_run_does_not_relist_history_already_collected(self):
        world = ConsoleWorld(self, machine_id="m4", role=ROLE_MERGE_ONLY)
        world.publish("m1", [("A01", "2026-09-05")], week="2026-W36")
        world.publish("m2", [("A02", day) for day in DAYS])
        world.sync("m1", "m2", "m3")
        world.run()
        self.assertIn("| raw-m1 | W36 |", world.report())

        second = world.run()

        report = world.report()
        self.assertEqual(len(second.imports), 1, "历史那份安静跳过，不占这次运行的账")
        self.assertNotIn("W36", report)
        self.assertIn("| raw-m2 | W38 | 17 | — | — | — | — | 已导入过 → 跳过 |", report)

    def test_a_late_backfilled_package_is_caught_up_by_the_next_run(self):
        world = ConsoleWorld(self)                    # 采集机 m1
        world.plan(("A01", "m1", 3), ("A03", "m3", 3))
        world.crawls([("A01", day) for day in DAYS])
        world.publish("m2", [("A02", "2026-09-09")], week="2026-W37")   # 迟到的历史包
        world.publish("m3", [("A03", day) for day in DAYS])
        world.sync("m2", "m3")

        outcome = world.run()

        self.assertEqual(outcome.exit_code, 0)
        ledger = world.conn.execute(
            "SELECT week FROM import_packages ORDER BY week").fetchall()
        self.assertEqual([row[0] for row in ledger], ["2026-W37", "2026-W38"])
        self.assertIn("| raw-m2 | W37 |", world.report())


class PullAndImageTroubleTests(unittest.TestCase):
    """拉不动与缺图：都如实记、都要人看一眼（退出码 1），不拦汇总。"""

    def test_a_repo_that_cannot_pull_is_reported_not_fatal(self):
        world = ConsoleWorld(self)
        world.plan(("A01", "m1", 3))
        world.crawls([("A01", day) for day in DAYS])
        shutil.rmtree(world.root / "raw-m2")
        (world.root / "raw-m2").mkdir()               # 目录在，但不是个 git 库：pull 必失败

        outcome = world.run()

        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual([note.ok for note in outcome.pulls if note.repo == "raw-m2"], [False])
        report = world.report()
        self.assertIn("raw-m2 拉不动（", todo_of(report))

    def test_images_that_cannot_be_fetched_are_counted_in_the_report(self):
        world = ConsoleWorld(self)
        world.plan(("A01", "m1", 3), ("A03", "m3", 3))
        world.crawls([("A01", day) for day in DAYS])
        world.publish("m3", [("A03", day) for day in DAYS], image_hash=IMG_HASH)

        outcome = world.run()

        self.assertEqual(outcome.exit_code, 1)
        report = world.report()
        self.assertIn("- 图片：先拉图再插行，仍缺的如实记（本次 1 张）", report)
        self.assertIn("这次有 1 张图没拉到（见第二节），如实记一笔",
                      todo_of(report))
        # 缺图不挡导入：行照插
        self.assertEqual(world.conn.execute(
            "SELECT COUNT(*) FROM inventory WHERE shop_key='A03'").fetchone()[0], 7)


class ExportOwnershipTests(unittest.TestCase):
    """导出只发「自己那份」（票据 10 的报告口径逼出来的）：导入回来的行不以本机名义再发。"""

    def setUp(self):
        self.world = ConsoleWorld(self)
        self.world.plan(("A01", "m1", 3), ("A03", "m3", 3))
        self.world.crawls([("A01", day) for day in DAYS])
        self.world.publish("m3", [("A03", day) for day in DAYS])
        self.world.run()                 # 全链：m1 发了自己的包、收进 m3 的

    def _rebuild(self) -> Path:
        package = self.world.box.tmp / "again.db"
        export.build_package(self.world.db_path, package, week=WEEK, machine_id="m1",
                             crawl_in_progress=False,
                             generated_at=dt.datetime(2026, 9, 21, 9, 0, tzinfo=CST))
        return package

    def test_imported_rows_are_not_re_exported_under_this_machines_name(self):
        # 收完别人的行，本机库里 A03 的行都在；再打一份包，里面只能有本机自己的 A01
        package = self._rebuild()
        shops = {row[0] for row in _package_query(
            package, "SELECT DISTINCT shop_key FROM inventory WHERE date BETWEEN "
                     "'2026-09-14' AND '2026-09-20'")}
        self.assertEqual(shops, {"A01"})
        self.assertEqual(_package_query(
            package, "SELECT COUNT(*) FROM product_information_versions"), [(7,)])
        # 身份表同理：A03 的商品行不进（描述列的最近写者是 m3）
        offers = {row[0] for row in _package_query(package, "SELECT offer_id FROM products")}
        self.assertEqual(offers, {"A01-o1"})

    def test_a_group_this_machine_recrawled_later_is_exported_again(self):
        # 导入之后本机又采过这一组（越权补采/交接）：账里的时刻对不上了，按本机算——
        # 这一组必须照发，否则本机与全世界分叉、真冲突也不再浮出来
        self.world.crawls([("A03", "2026-09-16")], observed_at="18:00:00")

        package = self._rebuild()

        self.assertEqual(_package_query(
            package, "SELECT shop_key, date FROM inventory WHERE shop_key='A03'"),
            [("A03", "2026-09-16")])


def _package_query(package: Path, sql: str):
    conn = sqlite3.connect(package)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


class PlanRepoPullTests(unittest.TestCase):
    """拉取也管 plan 库（spec §7「对另外三个库 pull」）：纯汇总机不跑准备串，靠这一趟保鲜。"""

    def test_the_plan_clone_is_pulled_along_with_the_raw_repos(self):
        world = ConsoleWorld(self)
        world.plan(("A01", "m1", 3))
        world.crawls([("A01", day) for day in DAYS])
        _plan_template().take(world.box)
        # 裸库与克隆在模板里都是空的（未出生）；名册这一推是它的第一个提交，
        # 交换区那份克隆靠这一趟 pull 才落地——与上机时的形状一致。
        world.box.commit_push(world.box.tmp / "plan-work",
                              {"machines.json": '["m1", "m2", "m3"]\n'}, message="roster")

        outcome = world.run()

        self.assertIn("plan", [note.repo for note in outcome.pulls])
        self.assertEqual((world.root / "plan" / "machines.json").read_text(encoding="utf-8"),
                         '["m1", "m2", "m3"]\n')


class ReasonGlossCoverageTests(unittest.TestCase):
    """报告的缺口理由要盖住轮次的全部终态：新增一个终态时在这里点名，别静默降级。"""

    def test_every_terminal_reason_has_a_report_gloss(self):
        from bestseller_monitor.rounds import TerminalReason

        missing = [reason.value for reason in TerminalReason
                   if reason.value not in exchange_mod._REASON_GLOSS]

        self.assertEqual(missing, [])


class ExchangeReadPortTests(unittest.TestCase):
    """给调用方的读口：按周找包、读包覆盖、包名里的周号。"""

    def test_package_week_reads_both_naming_spellings(self):
        self.assertEqual(export.package_week("W38-m2.db.gz"), 38)
        self.assertEqual(export.package_week("38-m2.db.gz"), 38)
        self.assertIsNone(export.package_week("W38-m2.db"))
        self.assertIsNone(export.package_week("2026-W38-m2.db.gz"))
        self.assertIsNone(export.package_week("notes.txt"))

    def test_week_packages_finds_only_that_weeks_packages(self):
        world = ConsoleWorld(self)
        world.publish("m2", [("A02", day) for day in DAYS])
        world.publish("m2", [("A02", "2026-09-09")], week="2026-W37")
        # 一个名字不合布局的杂项包：不认
        junk = world.root / "raw-m3" / "data" / "2026" / "2026-W38-m3.db.gz"
        junk.parent.mkdir(parents=True)
        junk.write_bytes(b"not a package")
        world.sync()

        found = export.week_packages(world.root, WEEK)

        self.assertEqual([ref.machine for ref in found], ["m2"])
        self.assertEqual(found[0].rel, "raw-m2/data/2026/W38-m2.db.gz")
        self.assertEqual(found[0].path.name, "W38-m2.db.gz")

    def test_package_shop_days_reads_the_coverage_in_the_window(self):
        world = ConsoleWorld(self)
        package = world.publish("m2", [("A02", day) for day in DAYS])

        full = merge.package_shop_days(package, dt.date(2026, 9, 14), dt.date(2026, 9, 20))
        part = merge.package_shop_days(package, dt.date(2026, 9, 15), dt.date(2026, 9, 16))

        self.assertEqual(len(full), 7)
        self.assertEqual(part, frozenset({("A02", "2026-09-15"), ("A02", "2026-09-16")}))


# 入口测试的真配置文件（与 test_run_cli 的 MINIMAL_CONFIG 同形；db 指向小世界的库，
# 交换区根按约定从 data_dir 的上一级取，正落在 ConsoleWorld 的 exchange/ 上）。
CONFIG_TOML = """
[run]
human_pause_minutes = 1
max_pages_per_shop = 30
max_detail_opportunities_per_round = 1000
max_attempts_per_page = 2
fail_rate_limit = 0.1
shuffle_within_shop = true

[human]
detail_delay_sec = [0.0, 0.0]
long_pause_interval = [1, 1]
long_pause_sec = [0.0, 0.0]
batch_size = 1
batch_rest_sec = [0.0, 0.0]
list_delay_sec = [0.0, 0.0]
action_delay_sec = [0.0, 0.0]
read_delay_sec = [0.0, 0.0]
retry_base_sec = 0.0
retry_jitter_sec = 0.0

[browser]
user_data_path = "profile"
timeout_ms = 1000

[paths]
shop_csv = "shops.csv"
db_file = "m1.db"
data_dir = "data"
logs_dir = "logs"
screenshot_dir = "screenshots"
raw_page_dir = "raw_pages"

[machine]
machine_id = "m1"
"""


def write_config(world: ConsoleWorld) -> Path:
    config = world.box.tmp / "config" / "config.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(CONFIG_TOML, encoding="utf-8")
    return config


class EntryPointTests(unittest.TestCase):
    """命令行入口：退出码、控制台摘要与 exchange.log（计划任务与小窗口同一入口）。"""

    def test_main_runs_once_and_returns_the_exit_code(self):
        world = ConsoleWorld(self)
        world.plan(("A01", "m1", 3), ("A03", "m3", 3))
        world.crawls([("A01", day) for day in DAYS])
        world.publish("m3", [("A03", day) for day in DAYS])
        config = write_config(world)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = exchange.main(["--config", str(config), "--week", WEEK])

        # 配置里没配图片桶：包发得出去，图片这半如实报缺 → 有需要人看一眼（退出码 1）。
        # 退出码 0 的干净路径在 CleanRunTests（那里给了图片库替身）。
        self.assertEqual(code, 1)
        self.assertIn("报告：", out.getvalue())
        self.assertIn("退出码 1：有需要人看一眼的地方", out.getvalue())
        self.assertIn("图片：没传完（配置缺 machine.cos_bucket", world.report())
        # 完整逐次流水在 logs/exchange.log 里事后可查
        log_text = (world.box.tmp / "logs" / "exchange.log").read_text(encoding="utf-8")
        self.assertIn("导入 W38-m3.db.gz", log_text)
        self.assertIn("报告已写到", log_text)

    def test_only_takes_the_two_judgment_actions_too(self):
        """`--only publish|collect` 与 export/merge 并列（票 05、ADR-0039 决策 7）。"""
        for action in ("publish", "collect"):
            self.assertEqual(exchange.parse_args(["--only", action]).only, action)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            exchange.parse_args(["--only", "publish-judgments"])

    def test_main_without_a_config_file_says_what_to_do_and_returns_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "config.toml"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = exchange.main(["--config", str(missing)])

        self.assertEqual(code, 2)
        self.assertIn("找不到配置", out.getvalue())

    def test_main_rejects_a_bad_week_loudly(self):
        world = ConsoleWorld(self)
        config = write_config(world)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = exchange.main(["--config", str(config), "--week", "38"])

        self.assertEqual(code, 2)
        self.assertIn("周编号格式非法", out.getvalue())

    def test_main_on_the_fatal_path_does_not_claim_a_report_file(self):
        """交换区根不在（上机清单第 4/9 步还差着）：点名说清楚、退出码 2、不写报告——
        收尾也不能说「报告：None」。"""
        world = ConsoleWorld(self)
        config = write_config(world)
        shutil.rmtree(world.root)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = exchange.main(["--config", str(config), "--week", WEEK])

        self.assertEqual(code, 2)
        self.assertIn("交换区根目录不存在", out.getvalue())
        self.assertIn("退出码 2：本机没做成事", out.getvalue())
        self.assertNotIn("报告：", out.getvalue())


class _ClosingEvent:
    """pywebview 的 `window.events.closing`：支持 `+=` 挂处理器。"""

    def __init__(self):
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class WindowStub:
    """窗口替身：只带 open_window 用到的 events.closing。"""

    def __init__(self, title: str, kwargs: dict):
        self.title = title
        self.kwargs = kwargs
        self.events = SimpleNamespace(closing=_ClosingEvent())


def unique_lock_name() -> str:
    """每个用例一把自己的锁名：不跟本机真实运行的交换台抢同一把（与 single_instance 用例同规）。"""
    return rf"Local\bestseller_test_exchange_{uuid.uuid4().hex}"


class WindowTests(unittest.TestCase):
    """小窗口（独立小工具）：标题、状态与两个次要按钮走同一个 Api。"""

    def test_window_opens_with_the_chinese_title_and_the_api(self):
        created = {}

        def fake_create(title, **kwargs):
            window = WindowStub(title, kwargs)
            created.update(kwargs, title=title, window=window)
            return window

        with patch.object(exchange.webview, "create_window", side_effect=fake_create), \
                patch.object(exchange.webview, "start"):
            code = exchange.open_window(crawler_cfg(machine_id="m1"))

        self.assertEqual(code, 0)
        self.assertEqual(created["title"], "1688 畅销品监控 · 库存数据交换")
        self.assertIsInstance(created["js_api"], exchange.Api)
        self.assertIn("1688 畅销品监控 · 库存数据交换", created["html"])
        # 关窗事件有接线（运行中关窗只记录，票 14 Q10）
        self.assertEqual(len(created["window"].events.closing.handlers), 1)

    def test_state_names_the_role_and_keeps_the_export_entry_with_a_note(self):
        merge_only = exchange.Api(crawler_cfg(machine_id="m4", role=ROLE_MERGE_ONLY)).state()
        self.assertEqual(merge_only["machine_id"], "m4")
        self.assertEqual(merge_only["role_gloss"], "纯汇总机")
        self.assertTrue(merge_only["merge_only"])
        self.assertIn("纯汇总机", merge_only["export_note"])

        collector = exchange.Api(crawler_cfg(machine_id="m1")).state()
        self.assertEqual(collector["role_gloss"], "采集机")
        self.assertFalse(collector["merge_only"])
        self.assertEqual(collector["export_note"], "")

    def test_start_runs_in_the_background_and_poll_streams_the_lines(self):
        seen = {}

        def fake_run(cfg, *, only=None, emit=None, **kwargs):
            seen["only"] = only
            emit("检查：本机 m1（采集机）")
            emit("导出已发布")
            return SimpleNamespace(exit_code=1, report_path=None)

        api = exchange.Api(crawler_cfg(machine_id="m1"), run=fake_run)
        self.assertEqual(api.start("merge"), {"ok": True})
        polled = api.poll()
        for _ in range(500):
            if not polled["running"]:
                break
            time.sleep(0.01)
            polled = api.poll()

        self.assertFalse(polled["running"])
        self.assertEqual(polled["exit_code"], 1)
        self.assertEqual(seen["only"], "merge")
        self.assertEqual(polled["lines"], ["检查：本机 m1（采集机）", "导出已发布",
                                           "退出码 1：有需要人看一眼的地方"])
        self.assertEqual(api.state()["exit_code"], 1)

    def test_a_finished_run_ends_with_the_cli_closing_lines(self):
        """结局行（票 14）：CLI 的收尾句（报告路径 + 退出码口径）窗口里也要看得到。"""
        report = Path("F:/AI/bestseller_runtime/exchange/报告") / f"{WEEK}.md"

        def fake_run(cfg, *, only=None, emit=None, **kwargs):
            emit("报告已写到 " + str(report))
            return SimpleNamespace(exit_code=2, report_path=report)

        api = exchange.Api(crawler_cfg(machine_id="m1"), run=fake_run)
        api.start(None)
        polled = api.poll()
        for _ in range(500):
            if not polled["running"]:
                break
            time.sleep(0.01)
            polled = api.poll()

        self.assertEqual(polled["lines"][-2:], [f"报告：{report}",
                                                "退出码 2：本机没做成事"])

    def test_a_run_that_died_before_the_report_still_ends_with_the_verdict(self):
        def fake_run(cfg, *, only=None, emit=None, **kwargs):
            raise RuntimeError("替身炸了")

        api = exchange.Api(crawler_cfg(machine_id="m1"), run=fake_run)
        api.start(None)
        polled = api.poll()
        for _ in range(500):
            if not polled["running"]:
                break
            time.sleep(0.01)
            polled = api.poll()

        self.assertEqual(polled["lines"][-1], "退出码 2：本机没做成事")
        self.assertNotIn("报告：", polled["lines"][-2])

    def test_close_note_is_quiet_when_nothing_is_running(self):
        api = exchange.Api(crawler_cfg(machine_id="m1"),
                           run=lambda *a, **k: SimpleNamespace(exit_code=0, report_path=None))

        self.assertIsNone(api.close_note())

    def test_close_note_records_a_window_closed_mid_run(self):
        """运行中关窗只记录、不拦（票 14 Q10）：重跑能修（导出幂等、导入有幂等账）。"""
        gate = threading.Event()

        def slow_run(cfg, *, only=None, emit=None, **kwargs):
            gate.wait(5)
            return SimpleNamespace(exit_code=0, report_path=None)

        api = exchange.Api(crawler_cfg(machine_id="m1"), run=slow_run)
        api.start(None)
        try:
            for _ in range(500):
                if api.poll()["running"]:
                    break
                time.sleep(0.01)
            note = api.close_note()
        finally:
            gate.set()

        self.assertIn("重跑能修", note)


class WindowPageTests(unittest.TestCase):
    """窗口页文案与纯汇总机的置灰（票 14：按钮照用户用词、导出入口保留但不给点）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parent.parent
                    / "bestseller_monitor" / "pages" / "ui_exchange.html").read_text(encoding="utf-8")

    def test_the_first_button_is_named_like_the_user_says_it(self):
        self.assertIn("导出&amp;汇总", self.html)
        self.assertNotIn("完整运行", self.html)

    def test_the_export_button_greys_out_on_a_merge_only_machine(self):
        # 入口保留着、不藏（spec §7），但纯汇总机上没有可做的事：「仅导出」置灰。
        self.assertIn("merge_only", self.html)
        self.assertIn("exportBtn.disabled", self.html)

    def test_the_two_judgment_actions_are_buttons_of_their_own(self):
        """发布／收取判断集与整趟、两个半趟并列（ADR-0039 决策 7）：窗口里也点得到。"""
        self.assertIn("发布判断集", self.html)
        self.assertIn("收取判断集", self.html)
        self.assertIn("start('publish')", self.html)
        self.assertIn("start('collect')", self.html)


class WeekWithWindowTests(unittest.TestCase):
    """`--window` 与 `--week` 同传明确拒绝（票 14 Q10；现状是静默忽略）。"""

    def test_window_with_a_week_is_refused_with_a_pointer_to_the_script(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = exchange.main(["--window", "--week", WEEK])

        self.assertEqual(code, 2)
        self.assertIn("--week", out.getvalue())
        self.assertIn("python exchange.py --week", out.getvalue())

    def test_the_refusal_comes_before_the_config_is_read(self):
        """参数冲突是用错入口，不是配置问题：没配置也要先报这一条。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = exchange.main(["--window", "--week", WEEK,
                                  "--config", "Z:/nope/config.toml"])

        self.assertEqual(code, 2)
        self.assertNotIn("找不到配置", out.getvalue())


class MutexTests(unittest.TestCase):
    """运行互斥（票 14 Q7）：同一台机器同一时刻至多一次交换台运行，窗口与命令行同规。"""

    def test_a_run_takes_the_exchange_lock_under_its_module_name(self):
        world = ConsoleWorld(self)
        config = write_config(world)
        asked: list[str] = []
        name = unique_lock_name()
        real_acquire = single_instance.acquire

        def recording_acquire(lock_name):
            asked.append(lock_name)
            return real_acquire(name)

        with patch.object(single_instance, "acquire", side_effect=recording_acquire), \
                patch.object(exchange_mod, "run_once",
                             return_value=SimpleNamespace(exit_code=0, report_path=None)):
            code = exchange.main(["--config", str(config)])

        self.assertEqual(code, 0)
        self.assertEqual(asked, [single_instance.EXCHANGE_LOCK])

    def test_a_second_console_run_gets_one_line_and_exit_two(self):
        world = ConsoleWorld(self)
        config = write_config(world)
        name = unique_lock_name()
        with patch.object(single_instance, "EXCHANGE_LOCK", name):
            holder = single_instance.acquire(name)
            self.assertIsNotNone(holder)
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = exchange.main(["--config", str(config)])
            finally:
                holder.release()

        self.assertEqual(code, 2)
        self.assertIn("已经在运行", out.getvalue())
        # 被拒也要落账：exchange.log 里查得到原因（抢锁发生在日志配置之后，票 14 审查收口）
        log_text = (world.box.tmp / "logs" / "exchange.log").read_text(encoding="utf-8")
        self.assertIn("已经在运行", log_text)

    def test_a_second_window_pops_and_exits_zero(self):
        """第二个实例沿用界面那条约定（ADR-0008）：自己弹窗说明、退出 0 让启动壳保持安静。"""
        world = ConsoleWorld(self)
        config = write_config(world)
        name = unique_lock_name()
        told: list[str] = []
        with patch.object(single_instance, "EXCHANGE_LOCK", name):
            holder = single_instance.acquire(name)
            self.assertIsNotNone(holder)
            try:
                with patch.object(exchange, "_message_box",
                                  side_effect=lambda text, title=None: told.append(text)):
                    code = exchange.main(["--window", "--config", str(config)])
            finally:
                holder.release()

        self.assertEqual(code, 0)
        self.assertEqual(len(told), 1)
        self.assertIn("已经在运行", told[0])

    def test_the_second_instance_popup_honours_no_dialog(self):
        world = ConsoleWorld(self)
        config = write_config(world)
        name = unique_lock_name()
        with patch.object(single_instance, "EXCHANGE_LOCK", name), \
                patch.dict("os.environ", {"BESTSELLER_NO_DIALOG": "1"}):
            holder = single_instance.acquire(name)
            self.assertIsNotNone(holder)
            try:
                with patch.object(exchange, "_message_box") as box:
                    code = exchange.main(["--window", "--config", str(config)])
            finally:
                holder.release()

        self.assertEqual(code, 0)
        box.assert_not_called()

    def test_consecutive_runs_in_one_process_are_not_concurrency(self):
        """锁在每次运行结束时释放：同进程里连续调用 main() 不算并发（票 14 Q7）。"""
        world = ConsoleWorld(self)
        config = write_config(world)
        name = unique_lock_name()
        with patch.object(single_instance, "EXCHANGE_LOCK", name), \
                patch.object(exchange_mod, "run_once",
                             return_value=SimpleNamespace(exit_code=0, report_path=None)):
            first = exchange.main(["--config", str(config)])
            second = exchange.main(["--config", str(config)])

        self.assertEqual((first, second), (0, 0))
        self.assertFalse(single_instance.is_held(name), "main() 返回后锁该已释放")

    def test_the_window_holds_the_lock_for_as_long_as_it_is_open(self):
        world = ConsoleWorld(self)
        config = write_config(world)
        name = unique_lock_name()
        held_during_window: list[bool] = []

        def fake_create(title, **kwargs):
            return WindowStub(title, kwargs)

        with patch.object(single_instance, "EXCHANGE_LOCK", name), \
                patch.object(exchange.webview, "create_window", side_effect=fake_create), \
                patch.object(exchange.webview, "start",
                             side_effect=lambda **kw: held_during_window.append(
                                 single_instance.is_held(name))):
            code = exchange.main(["--window", "--config", str(config)])

        self.assertEqual(code, 0)
        self.assertEqual(held_during_window, [True], "窗口开着时锁要在手里")
        self.assertFalse(single_instance.is_held(name), "关窗后锁该释放")


class WindowConfigFailureTests(unittest.TestCase):
    def test_window_mode_reports_a_broken_config_on_stderr(self):
        """窗口模式没人看控制台：写 stderr（启动壳收着这条管道，弹窗里会带出来）。"""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = exchange.main(["--window", "--config", "Z:/nope/config.toml"])

        self.assertEqual(code, 2)
        self.assertIn("找不到配置", err.getvalue())

    def test_window_startup_failure_is_exit_two_so_the_shell_can_pop(self):
        """窗口起不来折成退出码 2：启动壳的弹窗政策（2 与启动失败才弹）才够得着
        子进程侧的启动失败——不然双击壳只会什么都不说（票 14 审查收口）。"""
        world = ConsoleWorld(self)
        config = write_config(world)
        err = io.StringIO()
        with patch.object(exchange, "open_window",
                          side_effect=RuntimeError("WebView2 没装")), \
                contextlib.redirect_stderr(err):
            code = exchange.main(["--window", "--config", str(config)])

        self.assertEqual(code, 2)
        self.assertIn("WebView2 没装", err.getvalue())

    def test_missing_pywebview_names_the_fix(self):
        world = ConsoleWorld(self)
        config = write_config(world)
        err = io.StringIO()
        with patch.object(exchange, "webview", None), contextlib.redirect_stderr(err):
            code = exchange.main(["--window", "--config", str(config)])

        self.assertEqual(code, 2)
        self.assertIn("pywebview", err.getvalue())
        self.assertIn("pip install -r requirements.txt", err.getvalue())


if __name__ == "__main__":
    unittest.main()
