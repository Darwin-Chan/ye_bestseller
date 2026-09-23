"""票据 13：三机验收口径的机械化核对（tools/acceptance_check.py）的验收测试。

接缝：`acceptance_check.run_checks` 一组验收现场的输入 → 一张判定表（每条口径
PASS / FAIL / 跳过），与 `main` 的退出码。输入都是真实产物——计划库克隆里已发布的
计划文件、各机的数据库文件、交换区里的周包与周报；期望值来自口径本身（0 处违例、
缺口逐条有说明、同批包收敛一致……），不从实现里回算。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor import db as dbmod
from bestseller_monitor import export, merge, weekly_plan
from bestseller_monitor.config import Shop
from bestseller_monitor.image_store import ImageStoreError, image_key
from helpers import insert_sku_image
from tools import acceptance_check

WEEK = "2026-W40"
MONDAY, SUNDAY = dt.date(2026, 9, 28), dt.date(2026, 10, 4)
AS_OF = "2026-10-04"                      # 「该采到」的截止日：整周
DAYS = tuple((MONDAY + dt.timedelta(days=i)).isoformat() for i in range(7))
SHOPS = [Shop("A01", "店铺A01", "https://a01.example/", pages=3),
         Shop("A02", "店铺A02", "https://a02.example/", pages=2),
         Shop("A03", "店铺A03", "https://a03.example/", pages=1)]
MACHINES = ["m1", "m2", "m3"]


def seed_round(conn: sqlite3.Connection, rid: int, day: str, shops, *,
               reason: str | None = "COMPLETED", status: str = "待处理") -> None:
    """一天的轮次事实：轮次终态（None = 进行中）+ 范围里的店铺（含榜单状态）。"""
    conn.execute(
        "INSERT INTO rounds(id, started_at, run_date, terminal_reason) VALUES (?,?,?,?)",
        (rid, f"{day}T10:00:00+08:00", day, reason))
    conn.executemany(
        "INSERT INTO shop_rounds(round_id, shop_key, shop_url, shop_name, list_status) "
        "VALUES (?,?,?,?,?)",
        [(rid, key, f"https://{key.lower()}.example/", f"店铺{key}", status) for key in shops])
    conn.commit()


def seed_inventory(conn: sqlite3.Connection, pairs) -> None:
    """(店铺, 日期) 的库存行：每对一个商品一个规格。"""
    conn.executemany(
        "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock) VALUES (?,?,?,?,?)",
        [(key, f"{key}-o1", "s1", day, 5) for key, day in pairs])
    conn.commit()


def plan_document() -> dict:
    """一份真实形态的计划文件：三台机器各分到一家店。"""
    return {
        "week": WEEK,
        "generated_at": "2026-09-28T08:00:00+08:00",
        "generated_by": "m1",
        "algo_version": "v1",
        "params": {"constraint_weeks": 1, "balance_weeks": 4},
        "shops": [{"key": s.key, "name": s.name, "pages": s.pages} for s in SHOPS],
        "machines": list(MACHINES),
        "assignments": {"A01": "m1", "A02": "m2", "A03": "m3"},
        "checks": {"relaxed": {}, "eligible_counts": {"A01": 3, "A02": 3, "A03": 3},
                   "idle": []},
    }


class World:
    """验收现场的临时世界：计划库克隆 + 各机库 + 交换区。"""

    def __init__(self, test: unittest.TestCase):
        self.tmp = Path(tempfile.mkdtemp(prefix="bestseller-acceptance-"))
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plan = self.tmp / "exchange" / "plan"
        (self.plan / "plan").mkdir(parents=True)
        self.exchange = self.tmp / "exchange"

    def publish_plan(self, document: dict | None = None, *, md: str | None = None) -> None:
        document = document or plan_document()
        (self.plan / "plan" / f"{WEEK}.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        text = md if md is not None else weekly_plan.render_plan_md(document, [])
        (self.plan / "plan" / f"{WEEK}.md").write_text(text, encoding="utf-8")

    def seed_db(self, name: str, coverage, *, observed_at: str = "10:00:00",
                image_hash: str | None = None, rounds: bool = False) -> Path:
        """一份机器库：覆盖面的库存行 + 身份表行（rounds=True 时补「每天一轮完成」）。"""
        path = self.tmp / f"{name}.db"
        conn = dbmod.open(path)
        try:
            stamp = "2026-10-01T00:00:00+08:00"
            if rounds:
                for rid, day in enumerate(sorted({day for _, day in coverage}), start=1):
                    shops = sorted({shop_key for shop_key, d in coverage if d == day})
                    seed_round(conn, rid, day, shops, status="完成")
            seed_inventory(conn, list(coverage))
            conn.executemany(
                "INSERT INTO shops(shop_key, shop_name, shop_url, first_seen_at, last_seen_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(shop_key) DO NOTHING",
                [(shop_key, f"店铺{shop_key}", f"https://{shop_key.lower()}.example/",
                  stamp, stamp) for shop_key, _ in coverage])
            conn.executemany(
                "INSERT INTO products(offer_id, product_url, product_name, first_seen_at, "
                "last_seen_at) VALUES (?,?,?,?,?) ON CONFLICT(offer_id) DO NOTHING",
                [(f"{shop_key}-o1", f"https://detail.example/{shop_key}-o1.html",
                  f"商品{shop_key}-o1", stamp, stamp) for shop_key, _ in coverage])
            conn.executemany(
                "INSERT INTO skus(offer_id, sku_name, sku_id, first_seen_at, last_seen_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(offer_id, sku_id) DO NOTHING",
                [(f"{shop_key}-o1", "规格一", "s1", stamp, stamp)
                 for shop_key, _ in coverage])
            if image_hash is not None:
                conn.execute("INSERT OR IGNORE INTO product_image_assets(content_hash, mime, "
                             "content) VALUES (?,?,?)", (image_hash, "image/jpeg", b"x"))
            conn.executemany(
                "INSERT INTO product_information_versions(shop_key, offer_id, observed_at, "
                "observed_date, content_hash) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                [(shop_key, f"{shop_key}-o1", f"{day}T{observed_at}+08:00", day, image_hash)
                 for shop_key, day in coverage])
            conn.commit()
        finally:
            conn.close()
        return path

    def package_from(self, machine: str, source: Path, *,
                     meta_machine: str | None = None) -> Path:
        """把一份库的周包放进交换区（真打包）。"""
        package = self.tmp / f"{WEEK}-{machine}.db"
        export.build_package(source, package, week=WEEK, machine_id=meta_machine or machine,
                             crawl_in_progress=False,
                             generated_at=dt.datetime(2026, 10, 4, 20, 0, tzinfo=dbmod.CST))
        rel = export.package_rel_path(WEEK, machine)
        target = self.exchange / f"raw-{machine}" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(gzip.compress(package.read_bytes(), mtime=0))
        return target

    def publish_package(self, machine: str, coverage=(("A01", DAYS[0]),), *,
                        meta_machine: str | None = None,
                        observed_at: str = "10:00:00",
                        image_hash: str | None = None) -> tuple[Path, Path]:
        """把一台机器的周包放进交换区（真打包，包名与包内容都由参数定）。

        返回 (交换区里的包, 本机源库)——源库就是这台机器自己那份库，收敛用例拿它当
        「本机采集的行」。
        """
        source = self.seed_db(f"{machine}-source", list(coverage), observed_at=observed_at,
                              image_hash=image_hash)
        return self.package_from(machine, source, meta_machine=meta_machine), source

    def write_report(self, text: str) -> None:
        path = self.exchange / "报告" / f"{WEEK}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class PlanLineTests(unittest.TestCase):
    """口径一：计划核对行「相邻周同机器 0 处」。"""

    def setUp(self):
        self.world = World(self)
        self.world.publish_plan()

    def test_zero_violations_pass_and_the_line_is_quoted(self):
        result = acceptance_check.check_plan_line(self.world.plan, WEEK)

        self.assertEqual(result.status, acceptance_check.PASS)
        self.assertIn("相邻周同机器 0 处", "\n".join(result.lines))

    def test_nonzero_violations_fail(self):
        # 手写一份核对行非零的 .md：口径要求这个数是 0
        md = (self.world.plan / "plan" / f"{WEEK}.md").read_text(encoding="utf-8")
        md = md.replace("相邻周同机器 0 处", "相邻周同机器 2 处")
        (self.world.plan / "plan" / f"{WEEK}.md").write_text(md, encoding="utf-8")

        result = acceptance_check.check_plan_line(self.world.plan, WEEK)

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("相邻周同机器 2 处", "\n".join(result.lines))

    def test_a_missing_published_plan_fails_with_a_pointer(self):
        (self.world.plan / "plan" / f"{WEEK}.md").unlink()

        result = acceptance_check.check_plan_line(self.world.plan, WEEK)

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("计划", "\n".join(result.lines))


class ShareTests(unittest.TestCase):
    """口径二：本机份额 × 轮次对照（缺口逐条有说明）。"""

    def setUp(self):
        self.world = World(self)
        self.world.publish_plan()
        self.db_path = self.world.tmp / "m1.db"
        self.conn = dbmod.open(self.db_path)
        self.addCleanup(self.conn.close)

    def check(self):
        return acceptance_check.check_share(
            self.world.plan, WEEK, "m1", self.db_path, as_of=AS_OF)

    def test_a_fully_covered_share_passes(self):
        for rid, day in enumerate(DAYS, start=1):
            seed_round(self.conn, rid, day, ["A01"])
            seed_inventory(self.conn, [("A01", day)])

        result = self.check()

        self.assertEqual(result.status, acceptance_check.PASS)
        text = "\n".join(result.lines)
        self.assertIn("7/7", text)
        self.assertIn("缺口 0", text)

    def test_a_gap_names_the_day_and_the_round_outcome(self):
        for rid, day in enumerate(d for d in DAYS if d != "2026-10-02"):
            seed_round(self.conn, rid, day, ["A01"])
        # 10-02 那天有轮次（别的店），终态 COMPLETED——这就是这一天缺口的说明
        seed_round(self.conn, 90, "2026-10-02", ["A02"], reason="COMPLETED")

        result = self.check()

        self.assertEqual(result.status, acceptance_check.PASS)
        text = "\n".join(result.lines)
        self.assertIn("A01 10-02", text)
        self.assertIn("当天采集完成了，但这家店没有数据", text)

    def test_a_day_with_no_round_says_that_much(self):
        for rid, day in enumerate(d for d in DAYS if d != "2026-10-03"):
            seed_round(self.conn, rid, day, ["A01"])

        result = self.check()

        self.assertEqual(result.status, acceptance_check.PASS)
        text = "\n".join(result.lines)
        self.assertIn("A01 10-03", text)
        self.assertIn("当天没有采集记录", text)

    def test_an_open_round_is_a_half_day_and_fails(self):
        for rid, day in enumerate(d for d in DAYS if d != "2026-10-04"):
            seed_round(self.conn, rid, day, ["A01"])
        seed_round(self.conn, 99, "2026-10-04", ["A01"], reason=None)

        result = self.check()

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("半截", "\n".join(result.lines))

    def test_a_non_completed_listing_is_noted_with_its_status(self):
        for rid, day in enumerate(DAYS, start=1):
            seed_round(self.conn, rid, day, ["A01"],
                       status="失败" if day == "2026-10-01" else "完成")
            seed_inventory(self.conn, [("A01", day)])

        result = self.check()

        self.assertEqual(result.status, acceptance_check.PASS)
        text = "\n".join(result.lines)
        self.assertIn("A01 10-01", text)
        self.assertIn("失败", text)

    def test_an_empty_share_is_a_legal_empty_week(self):
        document = plan_document()
        document["assignments"] = {"A01": "m2", "A02": "m2", "A03": "m3"}
        self.world.publish_plan(document)

        result = self.check()

        self.assertEqual(result.status, acceptance_check.PASS)
        self.assertIn("空手", "\n".join(result.lines))

    def test_a_missing_published_plan_fails(self):
        (self.world.plan / "plan" / f"{WEEK}.json").unlink()

        result = self.check()

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("计划", "\n".join(result.lines))


class PackageTests(unittest.TestCase):
    """口径三：周包发布与「还没来的包」。"""

    def setUp(self):
        self.world = World(self)
        self.world.publish_plan()

    def publish_package(self, machine: str, *, meta_machine: str | None = None) -> None:
        self.world.publish_package(machine, meta_machine=meta_machine)

    def write_report(self, text: str) -> None:
        self.world.write_report(text)

    def check(self):
        return acceptance_check.check_packages(self.world.exchange, WEEK, self.world.plan)

    def test_all_packages_present_pass(self):
        for machine in MACHINES:
            self.publish_package(machine)
        self.write_report("**结果**：干净（退出码 0）· 缺口 0 · 冲突 0 · 还没来的包 0\n")

        result = self.check()

        self.assertEqual(result.status, acceptance_check.PASS)
        self.assertIn("3/3", "\n".join(result.lines))

    def test_a_missing_package_that_the_report_names_passes(self):
        for machine in ("m1", "m2"):
            self.publish_package(machine)
        self.write_report("**结果**：有需要人看一眼的地方（退出码 1）· 缺口 0 · 冲突 0 · "
                          "还没来的包 1\n- 还没收到的包：raw-m3 的 W40 还没发布或没拉到\n")

        result = self.check()

        self.assertEqual(result.status, acceptance_check.PASS)
        self.assertIn("raw-m3", "\n".join(result.lines))

    def test_a_missing_package_the_report_does_not_name_fails(self):
        for machine in ("m1", "m2"):
            self.publish_package(machine)
        self.write_report("**结果**：干净（退出码 0）· 缺口 0 · 冲突 0 · 还没来的包 0\n")

        result = self.check()

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("raw-m3", "\n".join(result.lines))

    def test_a_machine_merely_appearing_in_the_received_table_is_not_an_explanation(self):
        for machine in ("m1", "m2"):
            self.publish_package(machine)
        # 「收进来的包」表的来源列里也有 raw-m3 这几个字：那不是「还没收到的包」的说明
        self.write_report("**结果**：有需要人看一眼的地方（退出码 1）· 缺口 0 · 冲突 0 · "
                          "还没来的包 1\n\n"
                          "## 二、收进来的包\n"
                          "| 来源 | 周 | 行数 | 新增 | 覆盖 | 冲突 | 图片 | 结果 |\n"
                          "|---|---|---|---|---|---|---|---|\n"
                          "| raw-m3 | W40 | 17 | 17 | 0 | 0 | 0（0.0 MB） | 已导入 |\n")

        result = self.check()

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("raw-m3", "\n".join(result.lines))

    def test_a_missing_package_without_any_report_fails(self):
        for machine in ("m1", "m2"):
            self.publish_package(machine)

        result = self.check()

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("周报", "\n".join(result.lines))

    def test_a_package_whose_meta_contradicts_its_name_fails(self):
        for machine in MACHINES:
            self.publish_package(machine, meta_machine="m2" if machine == "m3" else machine)
        self.write_report("**结果**：干净（退出码 0）· 缺口 0 · 冲突 0 · 还没来的包 0\n")

        result = self.check()

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("m3", "\n".join(result.lines))


class ConvergenceTests(unittest.TestCase):
    """口径四：任取两台，同一批包合并后的取胜结果一致。"""

    def setUp(self):
        self.world = World(self)
        self.world.publish_plan()

    def merged_db(self, machine_id: str, packages, *, own: Path | None = None,
                  name: str | None = None) -> Path:
        """一份机器库：本机采集的行（own，给了就照它起库）+ 按给定顺序真导入的包。"""
        path = self.world.tmp / f"{name or machine_id}.db"
        if own is not None:
            source, target = sqlite3.connect(own), sqlite3.connect(path)
            source.backup(target)
            target.close()
            source.close()
        conn = dbmod.open(path)
        self.addCleanup(conn.close)
        for machine in packages:
            package = self.world.exchange / f"raw-{machine}" / \
                export.package_rel_path(WEEK, machine)
            merge.import_package(conn, package, machine_id=machine_id)
        return path

    def test_two_machines_that_collected_different_shares_converge(self):
        _, m2_own = self.world.publish_package("m2", (("A01", DAYS[0]),))
        _, m3_own = self.world.publish_package("m3", (("A02", DAYS[1]),),
                                               observed_at="11:00:00")
        m2 = self.merged_db("m2", ["m3"], own=m2_own)
        m3 = self.merged_db("m3", ["m2"], own=m3_own)

        result = acceptance_check.check_convergence({"m2": m2, "m3": m3})

        self.assertEqual(result.status, acceptance_check.PASS)
        text = "\n".join(result.lines)
        self.assertIn("m2 × m3", text)
        self.assertIn("inventory", text)

    def test_import_order_does_not_change_the_result(self):
        self.world.publish_package("m2", (("A01", DAYS[0]),))
        self.world.publish_package("m3", (("A02", DAYS[1]),), observed_at="11:00:00")
        first = self.merged_db("m1", ["m2", "m3"], name="m1-first")
        second = self.merged_db("m1", ["m3", "m2"], name="m1-second")

        result = acceptance_check.check_convergence({"m1 先收 m2": first, "m1 先收 m3": second})

        self.assertEqual(result.status, acceptance_check.PASS)

    def test_a_diverged_group_fails_and_is_named(self):
        _, own = self.world.publish_package("m2", (("A01", DAYS[0]),))
        m2 = self.merged_db("m2", [], own=own)
        m3 = self.merged_db("m3", [], own=own)          # 同一份数据、另一个标签
        conn = dbmod.open(m3)
        conn.execute("UPDATE inventory SET stock=99 WHERE shop_key='A01'")
        conn.commit()
        conn.close()

        result = acceptance_check.check_convergence({"m2": m2, "m3": m3})

        self.assertEqual(result.status, acceptance_check.FAIL)
        text = "\n".join(result.lines)
        self.assertIn("inventory", text)
        self.assertIn("A01", text)

    def test_column_order_difference_is_not_a_mismatch(self):
        # 真库的产品表与新库同列不同序（历史迁移的既成事实）：语义相同就该算一致
        _, own = self.world.publish_package("m2", (("A01", DAYS[0]),))
        m2 = self.merged_db("m2", [], own=own)
        m3 = self.merged_db("m3", [], own=own)
        conn = dbmod.open(m3)
        try:
            rows = conn.execute(
                "SELECT offer_id, product_url, product_name, first_seen_at, last_seen_at, "
                "main_image_url FROM products").fetchall()
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DROP TABLE products")
            conn.execute("CREATE TABLE products (offer_id TEXT PRIMARY KEY, product_url TEXT, "
                         "product_name TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT, "
                         "main_image_url TEXT)")
            conn.executemany(
                "INSERT INTO products (offer_id, product_url, product_name, first_seen_at, "
                "last_seen_at, main_image_url) VALUES (?,?,?,?,?,?)",
                [tuple(row) for row in rows])
            conn.commit()
        finally:
            conn.close()

        result = acceptance_check.check_convergence({"m2": m2, "m3": m3})

        self.assertEqual(result.status, acceptance_check.PASS)

    def test_a_real_schema_difference_fails(self):
        _, own = self.world.publish_package("m2", (("A01", DAYS[0]),))
        m2 = self.merged_db("m2", [], own=own)
        m3 = self.merged_db("m3", [], own=own)
        conn = dbmod.open(m3)
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN extra_note TEXT")
            conn.commit()
        finally:
            conn.close()

        result = acceptance_check.check_convergence({"m2": m2, "m3": m3})

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("列集不同", "\n".join(result.lines))

    def test_a_diverged_sku_image_ledger_fails_and_is_named(self):
        """SKU 图流水进对照：两台库里同一组行不一致就得点名。"""
        _, own = self.world.publish_package("m2", (("A01", DAYS[0]),))
        m2 = self.merged_db("m2", [], own=own)
        m3 = self.merged_db("m3", [], own=own)
        conn = dbmod.open(m3)
        insert_sku_image(conn, day=DAYS[0], shop_key="A01", offer_id="A01-o1", sku_id="s1")
        conn.close()

        result = acceptance_check.check_convergence({"m2": m2, "m3": m3})

        self.assertEqual(result.status, acceptance_check.FAIL)
        text = "\n".join(result.lines)
        self.assertIn("sku_image_versions", text)
        self.assertIn("A01", text)

    def test_the_same_ledger_rows_under_different_rowids_are_not_a_mismatch(self):
        """流水行的 `id` 是本机 rowid（导入会重排，spec §9 的已知约束）：不参与比对。"""
        _, own = self.world.publish_package("m2", (("A01", DAYS[0]),))
        m2 = self.merged_db("m2", [], own=own)
        m3 = self.merged_db("m3", [], own=own)
        for name, rowid in ((m2, 1), (m3, 77)):
            conn = dbmod.open(name)
            insert_sku_image(conn, day=DAYS[0], shop_key="A01", offer_id="A01-o1", sku_id="s1")
            conn.execute("UPDATE sku_image_versions SET id=?", (rowid,))
            conn.commit()
            conn.close()

        result = acceptance_check.check_convergence({"m2": m2, "m3": m3})

        self.assertEqual(result.status, acceptance_check.PASS, "\n".join(result.lines))
        self.assertIn("sku_image_versions", "\n".join(result.lines), "小计行里报的是这张表")

    def test_fewer_than_two_dbs_skips(self):
        _, own = self.world.publish_package("m2", (("A01", DAYS[0]),))
        m2 = self.merged_db("m2", [], own=own)

        result = acceptance_check.check_convergence({"m2": m2})

        self.assertEqual(result.status, acceptance_check.SKIP)
        self.assertIn("两台", "\n".join(result.lines))


class FakeImageStore:
    """图片库替身：key → 字节；没给字节的 key 取不到（模拟桶里没有）。"""

    def __init__(self, blobs: dict[str, bytes] | None = None):
        self.blobs = dict(blobs or {})

    def existing_keys(self) -> set[str]:
        return set(self.blobs)

    def upload(self, key: str, data: bytes) -> None:
        self.blobs[key] = data

    def fetch(self, key: str) -> bytes:
        if key not in self.blobs:
            raise ImageStoreError(f"桶里没有 {key}（替身）")
        return self.blobs[key]


class ColdStartTests(unittest.TestCase):
    """口径五：纯汇总机冷启动重放。"""

    def setUp(self):
        self.world = World(self)
        self.world.publish_plan()
        self.image_bytes = b"jpeg-bytes-demo"
        self.image_hash = hashlib.sha256(self.image_bytes).hexdigest()

    def packages(self) -> list[Path]:
        return sorted(self.world.exchange.glob(
            f"raw-*/data/2026/W40-*{export.PACKAGE_SUFFIX}"))

    def replayed_db(self, *, store=None, skip=(), name="m4") -> Path:
        """一份从交换区重放出来的新库；`skip` 里的包名不导入（模拟漏收）。"""
        path = self.world.tmp / f"{name}.db"
        conn = dbmod.open(path)
        for package in self.packages():
            if package.name in skip:
                continue
            merge.import_package(conn, package, machine_id="m4", store=store)
        conn.close()
        return path

    def test_a_replayed_cold_start_passes(self):
        self.world.publish_package("m2", (("A01", DAYS[0]),),
                                   image_hash=self.image_hash)
        self.world.publish_package("m3", (("A02", DAYS[1]),))
        store = FakeImageStore({image_key(self.image_hash, "image/jpeg"): self.image_bytes})

        result = acceptance_check.check_cold_start(self.replayed_db(store=store),
                                                   self.world.exchange)

        self.assertEqual(result.status, acceptance_check.PASS)
        text = "\n".join(result.lines)
        self.assertIn("2/2", text)
        self.assertIn("inventory", text)
        self.assertIn(f"（{DAYS[0]}..{DAYS[1]}）", text, "冷库的 inventory 覆盖起止要报给人看")

    def test_an_unimported_package_fails_and_is_named(self):
        self.world.publish_package("m2", (("A01", DAYS[0]),))
        self.world.publish_package("m3", (("A02", DAYS[1]),))

        result = acceptance_check.check_cold_start(
            self.replayed_db(skip=("W40-m3.db.gz",)), self.world.exchange)

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("W40-m3.db.gz", "\n".join(result.lines))

    def test_a_missing_image_fails_with_a_note(self):
        self.world.publish_package("m2", (("A01", DAYS[0]),),
                                   image_hash=self.image_hash)

        result = acceptance_check.check_cold_start(self.replayed_db(store=FakeImageStore()),
                                                   self.world.exchange)

        self.assertEqual(result.status, acceptance_check.FAIL)
        text = "\n".join(result.lines)
        self.assertIn("图", text)
        self.assertIn("缺图", text)

    def test_an_image_that_only_the_ledger_references_counts_as_referenced(self):
        """「图片按清单补齐」的引用集 = 版本行 ∪ SKU 图流水行：只在流水里引用的那张也算。"""
        _, source = self.world.publish_package("m2", (("A01", DAYS[0]),))   # 版本行不带图
        conn = dbmod.open(source)
        conn.execute("INSERT OR IGNORE INTO product_image_assets(content_hash, mime, content) "
                     "VALUES (?,?,?)", (self.image_hash, "image/jpeg", self.image_bytes))
        insert_sku_image(conn, day=DAYS[0], shop_key="A01", offer_id="A01-o1", sku_id="s1",
                         content_hash=self.image_hash)
        conn.close()
        self.world.package_from("m2", source)      # 重新打包：清单里带上这张

        without_bytes = acceptance_check.check_cold_start(
            self.replayed_db(store=FakeImageStore()), self.world.exchange)

        self.assertEqual(without_bytes.status, acceptance_check.FAIL)
        text = "\n".join(without_bytes.lines)
        self.assertIn("引用 1 个内容哈希", text, "引用数如实：这张只被流水行引用")
        self.assertIn("缺图", text)

        with_bytes = acceptance_check.check_cold_start(
            self.replayed_db(name="m4-with-bytes", store=FakeImageStore(
                {image_key(self.image_hash, "image/jpeg"): self.image_bytes})),
            self.world.exchange)

        self.assertEqual(with_bytes.status, acceptance_check.PASS,
                         "\n".join(with_bytes.lines))

    def test_an_empty_db_fails_the_table_counts(self):
        self.world.publish_package("m2", (("A01", DAYS[0]),))

        result = acceptance_check.check_cold_start(self.replayed_db(skip=("W40-m2.db.gz",)),
                                                   self.world.exchange)

        self.assertEqual(result.status, acceptance_check.FAIL)
        self.assertIn("shops 0", "\n".join(result.lines))


class CliTests(unittest.TestCase):
    """整链：一个小而全的验收现场，五条口径全过、退出码 0；缺输入跳过不算失败。"""

    def setUp(self):
        self.world = World(self)
        self.world.publish_plan()
        self.image_bytes = b"jpeg-bytes-demo"
        self.image_hash = hashlib.sha256(self.image_bytes).hexdigest()
        self.store = FakeImageStore(
            {image_key(self.image_hash, "image/jpeg"): self.image_bytes})
        share = {"m1": "A01", "m2": "A02", "m3": "A03"}
        self.dbs: dict[str, Path] = {}
        packages: dict[str, Path] = {}
        for machine, shop in share.items():
            own = self.world.seed_db(machine, [(shop, day) for day in DAYS],
                                     image_hash=self.image_hash, rounds=True)
            self.dbs[machine] = own
            packages[machine] = self.world.package_from(machine, own)
        for machine in share:                       # 每台收另外两台的包
            conn = dbmod.open(self.dbs[machine])
            for other, package in packages.items():
                if other != machine:
                    merge.import_package(conn, package, machine_id=machine, store=self.store)
            conn.close()
        self.m4 = self.world.tmp / "m4.db"          # 纯汇总机：冷启动重放全部包
        conn = dbmod.open(self.m4)
        for package in packages.values():
            merge.import_package(conn, package, machine_id="m4", store=self.store)
        conn.close()
        self.world.write_report("**结果**：干净（退出码 0）· 缺口 0 · 冲突 0 · 还没来的包 0\n")

    def argv(self, **overrides) -> list[str]:
        values = dict(
            plan=str(self.world.plan), exchange_root=str(self.world.exchange),
            machine=[f"{machine}={path}" for machine, path in self.dbs.items()],
            merge_only=f"m4={self.m4}")
        values.update(overrides)
        argv = ["--week", WEEK, "--as-of", AS_OF]
        if values["plan"]:
            argv += ["--plan", values["plan"]]
        if values["exchange_root"]:
            argv += ["--exchange-root", values["exchange_root"]]
        for spec in values["machine"]:
            argv += ["--machine", spec]
        if values["merge_only"]:
            argv += ["--merge-only", values["merge_only"]]
        return argv

    def run_main(self, argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = acceptance_check.main(argv)
        return code, out.getvalue()

    def test_the_full_world_passes_with_exit_code_zero(self):
        code, text = self.run_main(self.argv())

        self.assertEqual(code, 0)
        self.assertIn("**结果**：通过 · 跑 7 项：过 7 / 没过 0 / 跳过 0", text)
        self.assertIn("## 1. 计划核对行 —— 通过", text)
        self.assertIn("## 7. 纯汇总机冷启动重放 —— 通过", text)

    def test_a_failed_criterion_means_exit_code_one(self):
        for package in self.world.exchange.glob("raw-m3/**/*.db.gz"):
            package.unlink()

        code, text = self.run_main(self.argv())

        self.assertEqual(code, 1)
        self.assertIn("不通过", text)

    def test_missing_inputs_are_skipped_not_failed(self):
        code, text = self.run_main(["--week", WEEK, "--plan", str(self.world.plan)])

        self.assertEqual(code, 0)
        self.assertIn("跑 4 项：过 1 / 没过 0 / 跳过 3", text)

    def test_usage_errors_exit_two(self):
        cases = (
            ["--plan", str(self.world.plan)],                                  # 缺 --week
            ["--week", WEEK, "--plan", str(self.world.plan), "--machine", "m1=nope.db"],
            ["--week", WEEK, "--plan", str(self.world.plan), "--machine", "m1"],
            ["--week", "2026-W99", "--plan", str(self.world.plan)],
            ["--week", WEEK, "--machine", f"m1={self.dbs['m1']}",
             "--merge-only", f"m1={self.m4}"],                                 # 编号重复
        )
        for argv in cases:
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as ctx:
                        acceptance_check.main(argv)
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
