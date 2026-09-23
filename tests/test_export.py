"""票据 08：导出——周包、raw 库与图片通道的验收测试。

三个接缝（spec「测试边界」）：

- **包**（`build_package`）：交出来的就是那个 SQLite 文件本身，用例开它读表与元数据；
- **一次导出**（`export`）：产物 = raw 库里的文件（真跑 git，远端是本地裸库）、
  outbox 里的包、返回的结果对象；图片库是系统边界（COS），用替身；
- **图片通道**（`image_store`）：coscli 是外部二进制（子进程打桩），清单解析对着真输出样例。

夹具只搭一个够用的小世界：两家店、两个商品、两周各一条观测，跨周边界都在 W37/W38 上
（与 spec §12 的补历史场景同一套日历）。
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import pathlib
import sqlite3
import unittest

from bestseller_monitor import db as dbmod
from bestseller_monitor import export
from bestseller_monitor.db import CST, cst_date
from bestseller_monitor.image_store import ImageStoreError
from bestseller_monitor.weekly_plan import PlanError, iso_week_label
from tests.git_repos import GitSandbox
from helpers import crawler_cfg, isolated_locks

# 字面量哈希（不是从字节算出来的）：包/清单里的 key 由哈希与 mime 推出来，
# 期望值因此也是字面量——不跟实现共算式。
H1 = "ab" + "1" * 62          # W37 那张图（jpeg）
H2 = "cd" + "2" * 62          # W38 那张图（png）
H3 = "ef" + "3" * 62          # 只在 SKU 图流水里出现的图（png）
H4 = "12" + "4" * 62          # 谁也没引用的那张（jpeg）


class FakeImageStore:
    """图片库替身：只记 key 与字节，模拟「桶里已有什么」。"""

    def __init__(self, existing=()):
        self.existing = set(existing)
        self.uploaded: dict[str, bytes] = {}

    def existing_keys(self) -> set[str]:
        return set(self.existing)

    def upload(self, key: str, data: bytes) -> None:
        self.uploaded[key] = data
        self.existing.add(key)


def build_source_db(path: pathlib.Path) -> sqlite3.Connection:
    """夹具：两周、两店、两商品的源库（直插，不走采集路径）。"""
    conn = dbmod.open(path)
    conn.executemany(
        "INSERT INTO shops(shop_key, shop_name, shop_url, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?)",
        [("A01", "店铺甲", "https://a01.example/", "2026-09-01T00:00:00+08:00",
          "2026-09-09T10:00:00+08:00"),
         ("A02", "店铺乙", "https://a02.example/", "2026-09-14T00:00:00+08:00",
          "2026-09-16T10:00:00+08:00")],
    )
    conn.executemany(
        "INSERT INTO products(offer_id, product_url, product_name, main_image_url, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
        [("11", "https://detail.1688.com/offer/11.html", "商品一", None,
          "2026-09-09T10:00:00+08:00", "2026-09-09T10:00:00+08:00"),
         ("22", "https://detail.1688.com/offer/22.html", "商品二", None,
          "2026-09-16T10:00:00+08:00", "2026-09-16T10:00:00+08:00")],
    )
    conn.executemany(
        "INSERT INTO skus(offer_id, sku_name, sku_id, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?)",
        [("11", "规格一", "s1", "2026-09-09T10:00:00+08:00", "2026-09-09T10:00:00+08:00"),
         ("22", "规格二", "s2", "2026-09-16T10:00:00+08:00", "2026-09-16T10:00:00+08:00")],
    )
    conn.executemany(
        "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, price, "
        "shop_name, product_name, sku_name) VALUES (?,?,?,?,?,?,?,?,?)",
        [("A01", "11", "s1", "2026-09-09", 5, 1.5, "店铺甲", "商品一", "规格一"),
         ("A02", "22", "s2", "2026-09-16", 8, 2.0, "店铺乙", "商品二", "规格二")],
    )
    conn.executemany(
        "INSERT INTO product_image_assets(content_hash, mime, content) VALUES (?,?,?)",
        [(H1, "image/jpeg", b"jpeg-bytes-1"), (H2, "image/png", b"png-bytes-2")],
    )
    conn.executemany(
        "INSERT INTO product_information_versions(shop_key, offer_id, observed_at, "
        "observed_date, product_name, image_url, content_hash, image_error) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [("A01", "11", "2026-09-09T10:00:00+08:00", "2026-09-09", "商品一",
          "https://img.example/1.jpg", H1, None),
         ("A02", "22", "2026-09-16T10:00:00+08:00", "2026-09-16", "商品二",
          "https://img.example/2.png", H2, None)],
    )
    conn.commit()
    return conn


def add_sku_image(conn: sqlite3.Connection, day: str, *, shop_key="A01", offer_id="11",
                  sku_id="s1", content_hash=None, image_error=None, source="专属图",
                  url=None) -> None:
    """直插一条 SKU 图流水行（不走采集路径）；观测时刻取当天 10:00。"""
    conn.execute(
        "INSERT INTO sku_image_versions(shop_key, offer_id, sku_id, observed_at, "
        "observed_date, image_url, content_hash, image_error, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (shop_key, offer_id, sku_id, f"{day}T10:00:00+08:00", day, url, content_hash,
         image_error, source))
    conn.commit()


class PackageBuildTests(unittest.TestCase):
    """接缝 1：交出来的包文件本身。"""

    def setUp(self):
        self.box = GitSandbox(self, prefix="bestseller-export-")
        self.source = self.box.tmp / "bestseller.db"
        self.conn = build_source_db(self.source)
        self.addCleanup(self.conn.close)
        self.package = self.box.tmp / "outbox" / "W37-m1.db"

    def build(self, week="2026-W37"):
        export.build_package(
            self.source, self.package, week=week, machine_id="m1",
            crawl_in_progress=False,
            generated_at=dt.datetime(2026, 9, 21, 10, 30, tzinfo=CST),
        )
        conn = sqlite3.connect(self.package)
        self.addCleanup(conn.close)
        return conn

    def test_package_carries_the_week_slice_only(self):
        """包里只有这一周的观测：别的周的库存与版本行不进来，图片字节不进来。"""
        conn = self.build("2026-W37")

        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(
            tables,
            {"shops", "products", "skus", "inventory", "product_information_versions",
             "sku_image_versions", "product_image_assets", "exchange_meta"})
        self.assertEqual(
            [tuple(r) for r in conn.execute(
                "SELECT shop_key, offer_id, sku_id, date FROM inventory")],
            [("A01", "11", "s1", "2026-09-09")])
        self.assertEqual(
            [tuple(r) for r in conn.execute(
                "SELECT shop_key, offer_id, observed_date FROM product_information_versions")],
            [("A01", "11", "2026-09-09")])
        self.assertEqual(
            [tuple(r) for r in conn.execute(
                "SELECT shop_key FROM shops ORDER BY shop_key")], [("A01",)])
        self.assertEqual(
            [tuple(r) for r in conn.execute(
                "SELECT offer_id FROM products ORDER BY offer_id")], [("11",)])
        self.assertEqual(
            [tuple(r) for r in conn.execute(
                "SELECT offer_id, sku_id FROM skus ORDER BY offer_id")], [("11", "s1")])
        self.assertEqual(
            [tuple(r) for r in conn.execute(
                "SELECT content_hash, mime FROM product_image_assets")], [(H1, "image/jpeg")])
        # 图片字节不进包（spec §3）：资产表只有元数据两列。
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(product_image_assets)")}
        self.assertEqual(columns, {"content_hash", "mime"})

    def test_the_sku_image_ledger_carries_the_week_slice_only(self):
        """SKU 图流水与版本行同规：本周窗口的行进包，别的周不进；`id` 不进包。

        失败行与无图行照带（失败原因是观测事实），来源三态原样过去。
        """
        add_sku_image(self.conn, "2026-09-09", content_hash=H1,
                      url="https://img.example/sku-1.jpg")
        add_sku_image(self.conn, "2026-09-16", shop_key="A02", offer_id="22", sku_id="s2",
                      content_hash=H2, url="https://img.example/sku-2.png")
        add_sku_image(self.conn, "2026-09-16", shop_key="A02", offer_id="22", sku_id="s3",
                      image_error="超时", url="https://img.example/sku-3.png")
        add_sku_image(self.conn, "2026-09-16", shop_key="A02", offer_id="22", sku_id="s4",
                      source="无图")
        conn = self.build("2026-W37")

        self.assertEqual(
            [row[1] for row in conn.execute("PRAGMA table_info(sku_image_versions)")],
            ["shop_key", "offer_id", "sku_id", "observed_at", "observed_date", "image_url",
             "content_hash", "image_error", "source"],
            "包的列集照本机表的名（id 是本机 rowid，不进包）")
        self.assertEqual(
            [tuple(row) for row in conn.execute("SELECT * FROM sku_image_versions")],
            [("A01", "11", "s1", "2026-09-09T10:00:00+08:00", "2026-09-09",
              "https://img.example/sku-1.jpg", H1, None, "专属图")])
        self.assertEqual(
            export.read_package_meta(conn)["rows"]["sku_image_versions"], 1)

    def test_package_metadata_names_the_machine_the_week_and_the_counts(self):
        """元数据表：机器、周、生成时刻、各表行数、口径版本、导出时是否在采。"""
        conn = self.build("2026-W37")

        meta = export.read_package_meta(conn)
        self.assertEqual(meta["machine_id"], "m1")
        self.assertEqual(meta["week"], "2026-W37")
        self.assertEqual(meta["generated_at"], "2026-09-21T10:30:00+08:00")
        self.assertEqual(meta["format_version"], export.EXPORT_FORMAT_VERSION)
        self.assertIs(meta["crawl_in_progress"], False)
        self.assertEqual(meta["rows"]["inventory"], 1)
        self.assertEqual(meta["rows"]["product_information_versions"], 1)
        self.assertEqual(meta["rows"]["sku_image_versions"], 0)
        self.assertEqual(meta["rows"]["product_image_assets"], 1)
        self.assertEqual(meta["rows"]["shops"], 1)
        self.assertEqual(meta["rows"]["products"], 1)
        self.assertEqual(meta["rows"]["skus"], 1)

    def test_package_assets_are_the_union_of_versions_and_the_ledger(self):
        """资产行 = 本周版本行引用的 ∪ 本周 SKU 图流水引用的；没被引用的不进包。"""
        self.conn.executemany(
            "INSERT INTO product_image_assets(content_hash, mime, content) VALUES (?,?,?)",
            [(H3, "image/png", b"png-bytes-3"), (H4, "image/jpeg", b"jpeg-bytes-4")])
        add_sku_image(self.conn, "2026-09-16", shop_key="A02", offer_id="22", sku_id="s2",
                      content_hash=H3, url="https://img.example/sku-3.png")
        add_sku_image(self.conn, "2026-09-16", shop_key="A02", offer_id="22", sku_id="s3",
                      content_hash=H2, source="主图代填")          # 与版本行同一张：并集去重
        add_sku_image(self.conn, "2026-09-16", shop_key="A02", offer_id="22", sku_id="s4",
                      source="无图")
        add_sku_image(self.conn, "2026-09-16", shop_key="A02", offer_id="22", sku_id="s5",
                      image_error="超时", url="https://img.example/sku-5.png")
        add_sku_image(self.conn, "2026-09-09", content_hash=H1)    # 上一周：不进这个包

        conn = self.build("2026-W38")

        self.assertEqual(
            [tuple(row) for row in conn.execute(
                "SELECT content_hash, mime FROM product_image_assets ORDER BY content_hash")],
            [(H2, "image/png"), (H3, "image/png")],
            "版本行那张（H2）与只在流水里的那张（H3）；无图/失败行没有哈希，上一周的不进")

    def test_a_silent_week_exports_empty_tables_not_other_weeks(self):
        """没有任何一周数据的周窗口：七张表都空，但结构齐全。"""
        conn = self.build("2026-W36")

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM product_image_assets").fetchone()[0], 0)
        self.assertEqual(export.read_package_meta(conn)["rows"]["inventory"], 0)

    def test_the_package_says_v2_in_its_metadata(self):
        """口径版本进了一位（v2）：SKU 图流水与资产并集是这个格式的一部分。"""
        conn = self.build("2026-W37")

        self.assertEqual(export.read_package_meta(conn)["format_version"], "v2")

    def test_crawl_in_progress_is_recorded_as_given(self):
        export.build_package(
            self.source, self.package, week="2026-W37", machine_id="m1",
            crawl_in_progress=True,
            generated_at=dt.datetime(2026, 9, 21, 10, 30, tzinfo=CST))

        conn = sqlite3.connect(self.package)
        self.addCleanup(conn.close)
        self.assertIs(export.read_package_meta(conn)["crawl_in_progress"], True)


class ExportRunTests(unittest.TestCase):
    """接缝 2：跑一次导出——raw 库里的文件（真跑 git）、outbox 里的包、结果对象。"""

    def setUp(self):
        self.box = GitSandbox(self, prefix="bestseller-export-")
        self.source = self.box.tmp / "data" / "bestseller.db"
        self.conn = build_source_db(self.source)
        self.addCleanup(self.conn.close)
        self.remote = self.box.new_remote("raw-m1.git")
        self.exchange = self.box.tmp / "exchange"
        self.box.must("clone", str(self.remote), str(self.exchange / "raw-m1"))
        self.store = FakeImageStore()
        self.cfg = crawler_cfg(db_file=self.source, machine_id="m1",
                               exchange_root=self.exchange)

    def export(self, week="2026-W38", **kwargs):
        kwargs.setdefault("store", self.store)
        return export.export(self.cfg, week=week, **kwargs)

    def read_published(self, rel="data/2026/W38-m1.db.gz") -> sqlite3.Connection:
        """从远端（经一个干净克隆）取回包并打开它。"""
        check = self.box.clone(self.remote, f"check-{len(list(self.box.tmp.glob('check-*')))}")
        raw = gzip.decompress((check / rel).read_bytes())
        path = self.box.tmp / f"published-{len(raw)}.db"
        path.write_bytes(raw)
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        return conn

    def commits(self) -> int:
        check = self.box.clone(self.remote, "counter")
        out = self.box.must("rev-list", "--count", "HEAD", cwd=check)
        return int(out.strip())

    def test_export_publishes_the_package_and_manifest_in_one_commit(self):
        result = self.export("2026-W38")

        self.assertTrue(result.published)
        self.assertFalse(result.unchanged)
        self.assertFalse(result.failed)
        self.assertEqual(result.package_rel, "data/2026/W38-m1.db.gz")
        self.assertEqual(result.manifest_rel, "data/2026/W38-m1.manifest.json.gz")
        self.assertEqual(result.rows["inventory"], 1)
        self.assertEqual(result.rows["product_information_versions"], 1)
        package = self.read_published()
        meta = export.read_package_meta(package)
        self.assertEqual(meta["week"], "2026-W38")
        self.assertEqual(meta["machine_id"], "m1")
        self.assertEqual(
            [tuple(r) for r in package.execute(
                "SELECT shop_key, offer_id, date FROM inventory")],
            [("A02", "22", "2026-09-16")],
            "包里是这一周的切片")
        check = self.box.clone(self.remote, "one-commit")
        self.assertEqual(result.commit,
                         self.box.must("rev-parse", "--short", "HEAD", cwd=check).strip(),
                         "结果里记的提交就是远端收到的那笔")
        files = [line for line in self.box.must(
            "show", "--pretty=format:", "--name-only", "HEAD", cwd=check).splitlines() if line]
        self.assertEqual(sorted(files),
                         ["data/2026/W38-m1.db.gz", "data/2026/W38-m1.manifest.json.gz"],
                         "包与清单是同一次提交")
        self.assertEqual(self.commits(), 1)

    def test_reexport_without_new_data_makes_no_extra_commit(self):
        first = self.export("2026-W38")
        second = self.export("2026-W38")

        self.assertTrue(first.published)
        self.assertFalse(first.unchanged)
        self.assertTrue(second.published, "包仍在远端（这次只是确认了一遍）")
        self.assertTrue(second.unchanged, "同内容重打包：没有新提交")
        self.assertEqual(second.commit, first.commit, "还是那一笔提交")
        self.assertFalse(second.failed)
        self.assertEqual(self.commits(), 1, "同周重跑不产生多余提交")

    def test_new_data_in_the_same_week_is_published_as_a_new_commit(self):
        self.export("2026-W38")
        self.conn.execute(
            "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, price, "
            "shop_name, product_name, sku_name) VALUES (?,?,?,?,?,?,?,?,?)",
            ("A02", "22", "s2", "2026-09-17", 9, 2.0, "店铺乙", "商品二", "规格二"))
        self.conn.commit()

        result = self.export("2026-W38")

        self.assertTrue(result.published)
        self.assertFalse(result.unchanged)
        self.assertEqual(self.commits(), 2)
        package = self.read_published()
        self.assertEqual(
            [row[0] for row in package.execute(
                "SELECT date FROM inventory ORDER BY date")],
            ["2026-09-16", "2026-09-17"])

    def test_a_package_from_the_previous_format_version_is_republished(self):
        """远端那份是上一版程序发的（六表、没有 SKU 图流水）：新程序读不成它的摘要，同数据也重发。"""
        self.export("2026-W38")
        clone = self.exchange / "raw-m1"
        rel = "data/2026/W38-m1.db.gz"
        path = self.box.tmp / "as-v1.db"
        path.write_bytes(gzip.decompress((clone / rel).read_bytes()))
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE sku_image_versions")
        conn.execute("DELETE FROM exchange_meta WHERE key='rows_sku_image_versions'")
        conn.execute("UPDATE exchange_meta SET value='v1' WHERE key='format_version'")
        conn.commit()
        conn.close()
        self.box.commit_push(clone, {rel: gzip.compress(path.read_bytes(), mtime=0)},
                             message="上一版程序发的包")
        before = int(self.box.must("rev-list", "--count", "main", cwd=self.remote).strip())

        result = self.export("2026-W38")

        self.assertTrue(result.published, result.failure)
        self.assertFalse(result.unchanged, "上一版格式的包认不出：同数据也要重发")
        self.assertEqual(self.commits(), before + 1, "重发正是多的那一笔")
        self.assertEqual(export.read_package_meta(self.read_published())["format_version"],
                         "v2")

    def test_a_backfilled_week_lands_in_its_own_directory_file(self):
        result = self.export("2026-W37")

        self.assertTrue(result.published)
        self.assertEqual(result.package_rel, "data/2026/W37-m1.db.gz")
        package = self.read_published("data/2026/W37-m1.db.gz")
        self.assertEqual(
            [tuple(r) for r in package.execute(
                "SELECT shop_key, offer_id, date FROM inventory")],
            [("A01", "11", "2026-09-09")])

    def test_unreachable_raw_repo_reports_failure_and_keeps_the_package(self):
        self.box.must("remote", "set-url", "origin",
                      str(self.box.tmp / "gone.git"), cwd=self.exchange / "raw-m1")

        result = self.export("2026-W38")

        self.assertTrue(result.failed)
        self.assertFalse(result.published)
        self.assertIn("拉取", result.failure)
        self.assertIsNone(result.images, "包都没发出去，图片这趟不做")
        self.assertTrue((self.exchange / "outbox" / "W38-m1.db.gz").exists(),
                        "包留在本机 outbox，修好后重跑即可")

    def test_a_rejected_push_cleans_the_clone_and_the_next_run_heals(self):
        counter = self.box.tmp / "hook-runs"
        self.box.install_declining_hook(self.exchange / "raw-m1", counter)

        first = self.export("2026-W38")

        self.assertTrue(first.failed)
        self.assertFalse(first.published)
        self.assertIsNone(first.images, "包都没发出去，图片这趟不做")
        status = self.box.must("status", "--porcelain", cwd=self.exchange / "raw-m1")
        self.assertEqual(status.strip(), "", "推送失败后克隆要退回远端状态（没有半截改动）")
        self.assertTrue((self.exchange / "outbox" / "W38-m1.db.gz").exists(),
                        "包留在本机 outbox，修好后重跑即可发布")

        self.box.remove_pre_push(self.exchange / "raw-m1")
        second = self.export("2026-W38")

        self.assertTrue(second.published, second.failure)
        check = self.box.clone(self.remote, "healed")
        self.assertTrue((check / "data" / "2026" / "W38-m1.db.gz").exists(),
                        "通道恢复后重跑，包要真的到远端")

    def test_a_dirty_leftover_clone_is_repaired_before_publishing(self):
        """上次导出中断留下的脏工作区：拉取先退回远端状态，再照常发布。"""
        self.export("2026-W38")
        tracked = self.exchange / "raw-m1" / "data" / "2026" / "W38-m1.db.gz"
        tracked.write_bytes(b"half-written junk")          # 跟踪文件被改脏：pull --rebase 会拒绝
        self.conn.execute(
            "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, price, "
            "shop_name, product_name, sku_name) VALUES (?,?,?,?,?,?,?,?,?)",
            ("A02", "22", "s2", "2026-09-18", 7, 2.0, "店铺乙", "商品二", "规格二"))
        self.conn.commit()

        result = self.export("2026-W38")

        self.assertTrue(result.published, result.failure)
        self.assertEqual(self.commits(), 2)
        status = self.box.must("status", "--porcelain", cwd=self.exchange / "raw-m1")
        self.assertEqual(status.strip(), "")

    def test_crawl_in_progress_follows_the_crawler_lock(self):
        from bestseller_monitor import single_instance
        with isolated_locks():
            lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
            self.assertIsNotNone(lock)
            try:
                running = self.export("2026-W38")
            finally:
                lock.release()
        idle = self.export("2026-W38")

        self.assertTrue(running.crawl_in_progress)
        self.assertIs(export.read_package_meta(self.read_published())["crawl_in_progress"],
                      True, "发布出去的那份包如实记着导出时采集正在跑")
        self.assertFalse(idle.crawl_in_progress)

    def test_manifest_lists_exactly_the_keys_the_package_references(self):
        """清单恰等于包引用的 key 集：版本行引用的 ∪ SKU 图流水引用的，多一个少一个都不行。

        W38 多一张只在流水里的图（H3），流水里代填主图那张（H2）与版本行同一张、只算一个 key。
        """
        self.conn.execute(
            "INSERT INTO product_image_assets(content_hash, mime, content) VALUES (?,?,?)",
            (H3, "image/png", b"png-bytes-3"))
        add_sku_image(self.conn, "2026-09-16", shop_key="A02", offer_id="22", sku_id="s2",
                      content_hash=H3, url="https://img.example/sku-3.png")
        add_sku_image(self.conn, "2026-09-16", shop_key="A02", offer_id="22", sku_id="s3",
                      content_hash=H2, source="主图代填")
        add_sku_image(self.conn, "2026-09-09", content_hash=H1)
        self.export("2026-W38")
        key2 = f"img/cd/{H2}.png"
        key3 = f"img/ef/{H3}.png"

        check = self.box.clone(self.remote, "manifest-w38")
        raw = gzip.decompress(
            (check / "data" / "2026" / "W38-m1.manifest.json.gz").read_bytes())
        manifest = json.loads(raw.decode("utf-8"))

        self.assertEqual(manifest["package"], "W38-m1.db.gz")
        self.assertEqual(manifest["keys"], [key2, key3])

        self.export("2026-W37")
        check37 = self.box.clone(self.remote, "manifest-w37")
        raw = gzip.decompress(
            (check37 / "data" / "2026" / "W37-m1.manifest.json.gz").read_bytes())
        self.assertEqual(json.loads(raw.decode("utf-8"))["keys"],
                         [f"img/ab/{H1}.jpg"])

    def test_images_upload_only_new_keys(self):
        first = self.export("2026-W38")
        key2 = f"img/cd/{H2}.png"

        self.assertEqual(first.images.uploaded, 1)
        self.assertEqual(first.images.skipped, 0)
        self.assertEqual(first.images.uploaded_bytes, len(b"png-bytes-2"))
        self.assertEqual(first.images.missing, ())
        self.assertIsNone(first.images.failure)
        self.assertEqual(self.store.uploaded, {key2: b"png-bytes-2"})

        second = self.export("2026-W38")

        self.assertEqual((second.images.uploaded, second.images.skipped), (0, 1),
                         "已经传过的 key 不再传")
        self.assertEqual(second.images.uploaded_bytes, 0)

    def test_a_ledger_only_image_uploads_once_and_backfills_after_a_failed_run(self):
        """只在 SKU 图流水里出现的图：只传新增；同内容重跑补上上次没传成的那张。"""
        class Flaky(FakeImageStore):
            def __init__(self):
                super().__init__()
                self.refuse = True

            def upload(self, key: str, data: bytes) -> None:
                if self.refuse:
                    raise ImageStoreError("COS 503：稍后再试")
                super().upload(key, data)

        self.conn.execute(
            "INSERT INTO product_image_assets(content_hash, mime, content) VALUES (?,?,?)",
            (H3, "image/png", b"png-bytes-3"))
        add_sku_image(self.conn, "2026-09-16", shop_key="A02", offer_id="22", sku_id="s2",
                      content_hash=H3, url="https://img.example/sku-3.png")
        store = Flaky()

        first = self.export("2026-W38", store=store)

        self.assertTrue(first.published, "包照发；图片这趟没传成")
        self.assertIn("503", first.images.failure)
        self.assertEqual(store.uploaded, {})

        store.refuse = False
        second = self.export("2026-W38", store=store)

        self.assertTrue(second.unchanged, "同内容重跑：没有新提交")
        self.assertEqual(store.uploaded,
                         {f"img/cd/{H2}.png": b"png-bytes-2",
                          f"img/ef/{H3}.png": b"png-bytes-3"},
                         "上次没传上去的这次补上（版本行那张与流水那张都补）")
        self.assertEqual(second.images.skipped, 0)

    def test_keys_already_in_the_bucket_do_not_disturb_the_judgement(self):
        self.store = FakeImageStore(existing=["img/zz/other-machine.jpg"])

        result = self.export("2026-W38")

        self.assertEqual(result.images.uploaded, 1, "别人的 key 不顶替本包要传的")
        self.assertEqual(result.images.skipped, 0)

    def test_a_leftover_package_from_a_crash_does_not_masquerade_as_published(self):
        """崩溃在「写完包、还没提交」留下的未跟踪残迹，不能骗过发布判定——
        判定比的是 HEAD 里那份（远端的事实），不是工作区里躺着什么。"""
        self.box.commit_push(self.exchange / "raw-m1", {"README.md": "seed\n"},
                             message="seed")
        first = self.export("2026-W38")
        clone = self.exchange / "raw-m1"
        package = clone / "data" / "2026" / "W38-m1.db.gz"
        built_bytes = package.read_bytes()
        self.box.must("reset", "--hard", "HEAD~1", cwd=clone)
        self.box.must("push", "--force", "origin", "main", cwd=clone)  # 远端退回种子那笔
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_bytes(built_bytes)          # 未跟踪的「残迹」，内容恰与新包一致

        result = self.export("2026-W38")

        self.assertTrue(result.published, result.failure)
        self.assertFalse(result.unchanged, "工作区里的残迹不当作「已发布」")
        self.assertNotEqual(result.commit, first.commit)
        check = self.box.clone(self.remote, "after-crash")
        self.assertTrue((check / "data" / "2026" / "W38-m1.db.gz").exists(),
                        "包要真的进远端，而不是被当成已发布跳过")

    def test_a_failing_image_upload_is_reported_but_does_not_unpublish(self):
        class Exploding(FakeImageStore):
            def upload(self, key: str, data: bytes) -> None:
                raise ImageStoreError("COS 403：凭据没有写权限")

        result = self.export("2026-W38", store=Exploding())

        self.assertTrue(result.published, "包已经发出去了")
        self.assertIn("403", result.images.failure)
        self.assertEqual(self.commits(), 1)

    def test_images_whose_bytes_are_missing_locally_are_recorded_not_hidden(self):
        """库里的字节不见了（不该发生，但发生了要如实记，不挡别的上传）。"""
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("DELETE FROM product_image_assets WHERE content_hash=?", (H2,))
        self.conn.commit()
        key2 = f"img/cd/{H2}.png"

        result = export.upload_new_images(self.source, (export.ImageRef(key2, H2),),
                                          self.store)

        self.assertEqual(result.missing, (key2,))
        self.assertEqual(result.uploaded, 0)
        self.assertIsNone(result.failure)

    def test_missing_cos_bucket_is_reported_as_an_image_failure(self):
        cfg = crawler_cfg(db_file=self.source, machine_id="m1",
                          exchange_root=self.exchange, cos_bucket="")

        result = export.export(cfg, week="2026-W38")

        self.assertTrue(result.published, "包照发；图片这一半缺配置要说得出来")
        self.assertIn("cos_bucket", result.images.failure)

    def test_missing_raw_clone_points_at_the_onboarding_step(self):
        cfg = crawler_cfg(db_file=self.source, machine_id="m9",
                          exchange_root=self.exchange)

        result = export.export(cfg, week="2026-W38", store=self.store)

        self.assertTrue(result.failed)
        self.assertIn("raw-m9", result.failure)
        self.assertIn("clone", result.failure)

    def test_week_window_export_publishes_each_week_on_its_own(self):
        """补历史：一次按周窗口打包（W36/W37 这类），一周一个包各落各的路径。"""
        results = export.export_weeks(self.cfg, ["2026-W36", "2026-W37"],
                                      store=self.store)

        self.assertEqual([r.package_rel for r in results],
                         ["data/2026/W36-m1.db.gz", "data/2026/W37-m1.db.gz"])
        self.assertTrue(all(r.published for r in results))
        self.assertEqual([r.rows["inventory"] for r in results], [0, 1])
        check = self.box.clone(self.remote, "backfill")
        self.assertTrue((check / "data" / "2026" / "W36-m1.db.gz").exists())
        self.assertTrue((check / "data" / "2026" / "W37-m1.db.gz").exists())

    def test_export_alongside_a_running_writer_is_consistent_and_unblocking(self):
        """与采集并行：快照不含未提交的半截事务，也不把正在写的那一头卡住。"""
        writer = sqlite3.connect(self.source)          # 另一个连接 = 正在跑的采集
        self.addCleanup(writer.close)
        writer.execute("BEGIN")
        writer.execute(
            "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, price, "
            "shop_name, product_name, sku_name) VALUES (?,?,?,?,?,?,?,?,?)",
            ("A02", "22", "s2", "2026-09-19", 3, 2.0, "店铺乙", "商品二", "规格二"))

        result = self.export("2026-W38")

        self.assertTrue(result.published, result.failure)
        package = self.read_published()
        self.assertEqual(
            [row[0] for row in package.execute("SELECT date FROM inventory ORDER BY date")],
            ["2026-09-16"], "未提交的那半截不进包")
        writer.commit()                                # 写者没被挡：提交照常成功
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 3)

    def test_current_week_defaults_to_this_weeks_label(self):
        """不给周窗口时用现在的北京日期所在周（界面上「导出本周」的那条路）。"""
        today = dt.date.fromisoformat(cst_date())

        result = export.export(self.cfg, store=self.store)

        self.assertEqual(result.week, iso_week_label(today))


class WeekLabelTests(unittest.TestCase):
    """周编号 → 窗口/包名：周界的算法在 weekly_plan 一处，布局在 spec §2 一处。"""

    def test_package_path_carries_the_year_of_the_week_label(self):
        # 2026-W01 的周一落在 2025 年，仍归 2026（与计划文件命名同一口径）
        self.assertEqual(export.package_rel_path("2026-W01", "m1"),
                         "data/2026/W01-m1.db.gz")
        self.assertEqual(export.week_window("2026-W37"), ("2026-09-07", "2026-09-13"))

    def test_an_illegal_week_is_rejected_loudly(self):
        with self.assertRaises(PlanError) as ctx:
            export.package_rel_path("2026-W99", "m1")

        self.assertIn("2026-W99", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
