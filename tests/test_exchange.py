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
from bestseller_monitor import export, merge, single_instance
from bestseller_monitor.config import ROLE_COLLECTOR, ROLE_MERGE_ONLY
from bestseller_monitor.db import CST
from bestseller_monitor.image_store import ImageStoreError
from helpers import crawler_cfg, store_weekly_plan
from tests.git_repos import GitSandbox

WEEK = "2026-W38"
DAYS = ("2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
        "2026-09-19", "2026-09-20")
# 样例那次运行的同一天（W38 的周日晚上）：七天都算「该采到」。
RUN_AT = dt.datetime(2026, 9, 20, 19, 41, tzinfo=CST)
# 字面量哈希（不是从字节算出来的）：包里的 key 由哈希与 mime 推出来，期望值因此也是
# 字面量——不跟实现共算式（与 test_export 同一口径）。
IMG_HASH = "ab" + "1" * 62


def todo_of(report: str) -> str:
    """报告「下次该做什么」那一节的正文：节号随冲突节在不在（四 / 五）变。"""
    marker = "## 五、下次该做什么" if "## 五、下次该做什么" in report else "## 四、下次该做什么"
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


class ConsoleWorld:
    """一台机器跑交换台的小世界：三个 raw 库的裸远端 + 本机克隆 + 本机库 + 计划表。

    `publish()` 模拟别的机器发周包：经**另一个**克隆推送（交换区里那份要等交换台
    自己 pull 才到——与真实三机一致）。
    """

    def __init__(self, test: unittest.TestCase, *, machine_id="m1",
                 role=ROLE_COLLECTOR):
        self.test = test
        self.box = GitSandbox(test, prefix="bestseller-exchange-")
        self.root = self.box.tmp / "exchange"
        self.root.mkdir()
        self.machine_id = machine_id
        self.role = role
        self.work: dict[str, Path] = {}          # 别的机器发布用的旁路克隆
        for machine in ("m1", "m2", "m3"):
            remote = self.box.new_remote(f"raw-{machine}.git")
            self.box.clone(remote, f"exchange/raw-{machine}")       # 交换区里的克隆
            self.work[machine] = self.box.clone(remote, f"{machine}-work")
        self.db_path = self.box.tmp / f"{machine_id}.db"
        self.conn = dbmod.open(self.db_path)
        test.addCleanup(self.conn.close)
        self.store = FakeImageStore()

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

    # ---- 跑一次 ----

    def cfg(self, **overrides):
        values = dict(machine_id=self.machine_id, role=self.role,
                      exchange_root=self.root, db_file=self.db_path,
                      cos_bucket="", logs_dir=self.box.tmp / "logs")
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
        self.assertIn("**结果**：干净（退出码 0）· 缺口 0 · 冲突 0 · 还没来的包 0", report)
        self.assertIn("**本次运行**：导出已发布 · 新收 2 个包（34 行）", report)
        for title in ("## 一、本机发布", "## 二、收进来的包", "## 三、缺口与还没来的",
                      "## 四、下次该做什么"):
            self.assertIn(title, report)
        self.assertNotIn("## 四、冲突", report)     # 干净场景不出现冲突节（样例的同形）
        self.assertIn("W38-m1.db.gz", report)
        self.assertIn("- 本机负责：A01", report)
        self.assertIn("- 行数：inventory 7 · products 1 · skus 1 · 版本 7", report)
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
                      "还没来的包 1", report)
        self.assertIn("**本次运行**：导出已发布 · 新收 1 个包", report)
        # 三、缺口与还没来的：本机缺一天（带轮次终态的人话）+ 还没收到的包 + 口径
        self.assertIn("- 本机缺一天：A03 09-17 —— 当天采集在详情预算耗尽后中止"
                      "（历史日期补不了，如实记一笔）", report)
        self.assertIn("- 还没收到的包：raw-m2 的 W38 还没发布或没拉到", report)
        self.assertIn("- 口径：缺口 = 计划里该采到的（店铺 × 日期），在收到的包里找不到",
                      report)
        # A04 归 m2（包没来）：它的七天不记缺口，只记「还没来的包」——A04 整个报告不出现
        self.assertNotIn("A04", report)
        # 四、冲突：本机（计划外多采）与计划机，取后到者、覆盖与保留的行数按账里的事实
        # （本机那组两行：库存行同键被替换＝覆盖 1 行；版本行的 observed_at 不同键、留着＝保留 1 行）
        self.assertIn("- 重复采集：A02 09-15：本机 15:02（计划外多采）与计划机 16:40 都采到；"
                      "取后到者（m3），覆盖 1 行、保留 1 行", report)
        self.assertIn("- 明细记在本机导入账（本机视角：导入 raw-m3 时发现）；冲突不入交换区",
                      report)
        # 五、下次该做什么：三件（还没来的包 / 别再勾选 A02 / 本机缺的一天）
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
        plan_remote = self.world.box.new_remote("plan.git")
        self.world.box.seed(plan_remote, {"machines.json": '["m1", "m2", "m3"]\n'})
        self.world.box.clone(plan_remote, "exchange/plan")   # 纯汇总机不跑准备串：plan 靠拉取保鲜

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
        remote = world.box.new_remote("plan.git")
        world.box.clone(remote, "exchange/plan")
        work = world.box.clone(remote, "plan-work")
        world.box.commit_push(work, {"machines.json": '["m1", "m2", "m3"]\n'},
                              message="roster")

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
