"""票据 09：汇总导入（确定性合并语义）的验收测试。

接缝（spec「测试边界」）：`merge.import_package`——「给定一批包 + 一个库，得到一份库」。
夹具：每台包侧机器一个源库，用 `export.build_package` 从它真打出包（与票据 08 的产物
同形、走同一段打包代码）；本机侧的采集行直插（不走采集路径，够判据用）。

图片哈希从夹具字节算出来（内容寻址的定义就是"哈希即身份"）：导入侧会核对
sha256(取回的字节) == content_hash，字面量哈希过不了这条核对。
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import pathlib
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from bestseller_monitor import db as dbmod
from bestseller_monitor import export, merge, plan_step, rounds
from bestseller_monitor.db import CST
from bestseller_monitor.image_store import ImageStoreError, image_key
from helpers import insert_sku_image, new_round, product_picture

JPEG = b"jpeg-bytes-1"
PNG = b"png-bytes-2"
H1 = hashlib.sha256(JPEG).hexdigest()
H2 = hashlib.sha256(PNG).hexdigest()
KEY1 = image_key(H1, "image/jpeg")
KEY2 = image_key(H2, "image/png")

DAY = "2026-09-16"
D16_0900 = "2026-09-16T09:00:00+00:00"
D16_1000 = "2026-09-16T10:00:00+00:00"
SEEN_FIRST = "2026-09-01T00:00:00+00:00"

_UNSET = object()


def sku_image(sku_id="s1", *, observed_at=D16_0900, day=DAY, content_hash=H1, url=None,
              image_error=None, source="专属图", shop_key="A01", offer_id="11") -> dict:
    """一条 SKU 图流水行的夹具参数——交给 `helpers.insert_sku_image` 写（列集只在那一处）。

    观测时刻显式给（缺省这台机器的 claim 时刻）：汇总用例靠它定 claim 与去重键。
    """
    return {"day": day, "shop_key": shop_key, "offer_id": offer_id, "sku_id": sku_id,
            "observed_at": observed_at, "url": url, "content_hash": content_hash,
            "image_error": image_error, "source": source}


def machine_world(claim_at, *, stock, product_name="商品一", inventory_name=_UNSET,
                  shop_name="店铺甲", sku_name="规格一", sku_id="s1", first_seen=SEEN_FIRST,
                  extra=(), image=H1, sku_images=()):
    """一台机器对 (店铺 A01, 2026-09-16, 商品 11) 的一次采集，按各表行给出。

    `extra`：只有这台机器采到的商品，每项 (offer_id, stock)。
    `inventory_name`：库存行的 product_name 单独给（None 合法）——验"整行替换"用。
    `sku_id`：单规格商品用 `parse.DEFAULT_SKU_ID`（"default"）。
    `sku_images`：这台机器的 SKU 图流水行，用 `sku_image(...)` 组（引用到的哈希自己要
    进 `assets`，不然字节拉不回来——那正是「缺图」的情形）。
    """
    if inventory_name is _UNSET:
        inventory_name = product_name
    rows = {
        "shops": [("A01", shop_name, "https://a01.example/", first_seen, claim_at)],
        "products": [("11", "https://detail.1688.com/offer/11.html", product_name, None,
                      first_seen, claim_at)],
        "skus": [("11", sku_name, sku_id, first_seen, claim_at)],
        "inventory": [("A01", "11", sku_id, DAY, stock, 1.5, shop_name, inventory_name,
                       sku_name)],
        "versions": [("A01", "11", claim_at, DAY, product_name, "https://img.example/1.jpg",
                      image, None)],
        "assets": [(image, "image/jpeg" if image == H1 else "image/png",
                    JPEG if image == H1 else PNG)],
    }
    for offer_id, extra_stock in extra:
        rows["products"].append((offer_id, f"https://detail.1688.com/offer/{offer_id}.html",
                                 f"商品{offer_id}", None, claim_at, claim_at))
        rows["skus"].append((offer_id, f"规格{offer_id}", f"s{offer_id}", claim_at, claim_at))
        rows["inventory"].append(("A01", offer_id, f"s{offer_id}", DAY, extra_stock, 2.0,
                                  shop_name, f"商品{offer_id}", f"规格{offer_id}"))
        rows["versions"].append(("A01", offer_id, claim_at, DAY, f"商品{offer_id}", None,
                                 None, None))
    rows["sku_images"] = list(sku_images)
    return rows


def machine_source(path: pathlib.Path, *, shops=(), products=(), skus=(), inventory=(),
                   versions=(), assets=(), sku_images=()) -> None:
    """一台机器的源库：直插交换集各表（不走采集路径）。"""
    conn = dbmod.open(path)
    conn.executemany("INSERT INTO shops(shop_key, shop_name, shop_url, first_seen_at, "
                     "last_seen_at) VALUES (?,?,?,?,?)", shops)
    conn.executemany("INSERT INTO products(offer_id, product_url, product_name, main_image_url, "
                     "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)", products)
    conn.executemany("INSERT INTO skus(offer_id, sku_name, sku_id, first_seen_at, last_seen_at) "
                     "VALUES (?,?,?,?,?)", skus)
    conn.executemany("INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, price, "
                     "shop_name, product_name, sku_name) VALUES (?,?,?,?,?,?,?,?,?)", inventory)
    conn.executemany("INSERT INTO product_image_assets(content_hash, mime, content) "
                     "VALUES (?,?,?)", assets)
    conn.executemany("INSERT INTO product_information_versions(shop_key, offer_id, observed_at, "
                     "observed_date, product_name, image_url, content_hash, image_error) "
                     "VALUES (?,?,?,?,?,?,?,?)", versions)
    for row in sku_images:              # SKU 图流水的列集在 helpers 一处（04 审查的门规）
        insert_sku_image(conn, **row)
    conn.commit()
    conn.close()


def crawl_locally(conn: sqlite3.Connection, *, observed_at=D16_1000, stock=7,
                  sku_id="s1", content_hash=None) -> None:
    """本机采集写入的两行（版本 + 库存）：合并判据与行并集都够用。"""
    conn.execute(
        "INSERT INTO product_information_versions(shop_key, offer_id, observed_at, "
        "observed_date, product_name, image_url, content_hash, image_error) "
        "VALUES ('A01','11',?,?,?,?,?,?)",
        (observed_at, DAY, "商品一", None, content_hash, None))
    conn.execute(
        "INSERT INTO inventory(shop_key, offer_id, sku_id, date, stock, price, shop_name, "
        "product_name, sku_name) VALUES ('A01','11',?,?,?,?,?,?,?)",
        (sku_id, DAY, stock, 1.0, "店铺甲", "商品一", "规格一"))
    conn.commit()


def dump(conn: sqlite3.Connection) -> dict:
    """交换集逐表逐行快照（排序后）："任意顺序导入结果一致"就比这一份。"""
    snapshot = {}
    for table in export.EXCHANGE_TABLES:
        cols = ", ".join(table.names)
        snapshot[table.name] = [
            tuple(row) for row in
            conn.execute(f"SELECT {cols} FROM {table.name} ORDER BY {cols}")]
    return snapshot


class FakeImageStore:
    """图片库替身：桶里有什么、取过什么。"""

    def __init__(self, blobs=None):
        self.blobs = dict(blobs or {})
        self.fetched: list[str] = []

    def existing_keys(self) -> set[str]:
        return set(self.blobs)

    def upload(self, key: str, data: bytes) -> None:
        self.blobs[key] = data

    def fetch(self, key: str) -> bytes:
        self.fetched.append(key)
        if key not in self.blobs:
            raise ImageStoreError(f"对象不存在：{key}")
        return self.blobs[key]


class MergeCase(unittest.TestCase):
    """共用夹具：临时世界、包与库的搭法。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bestseller-merge-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def package(self, machine_id: str, rows: dict, *, week="2026-W38",
                generated_at=None) -> pathlib.Path:
        """源库 → 包（走票据 08 的打包代码，产物与真发出去的包同形）。"""
        source = self.tmp / f"source-{machine_id}.db"
        machine_source(source, **rows)
        package = self.tmp / "packages" / f"{week}-{machine_id}.db"
        export.build_package(source, package, week=week, machine_id=machine_id,
                             crawl_in_progress=False,
                             generated_at=generated_at or
                             dt.datetime(2026, 9, 21, 10, 30, tzinfo=CST))
        return package

    def local(self, machine_id="m1") -> sqlite3.Connection:
        conn = dbmod.connect(self.tmp / f"local-{machine_id}.db")
        self.addCleanup(conn.close)
        return conn

    def import_(self, conn, package, *, machine_id="m1", store=None) -> merge.ImportResult:
        return merge.import_package(conn, package, machine_id=machine_id, store=store)

    def rows(self, conn, sql, params=()):
        return [tuple(row) for row in conn.execute(sql, params)]


class MergeOrderTests(MergeCase):
    """验收 1：任意顺序导入同一批包，得到同一份库。"""

    def test_the_later_claim_replaces_same_key_rows_and_keeps_the_others(self):
        conn = self.local()
        self.import_(conn, self.package("m2", machine_world(
            D16_0900, stock=5, product_name="商品一(旧)", extra=[("33", 2)])))
        result = self.import_(conn, self.package("m3", machine_world(
            D16_1000, stock=8, product_name="商品一", sku_name="规格一甲",
            shop_name="店铺甲", first_seen="2026-09-05T00:00:00+00:00", extra=[("22", 4)])))

        self.assertFalse(result.failed)
        self.assertEqual(result.machine_id, "m3")
        self.assertEqual(
            self.rows(conn, "SELECT winner_machine, loser_machine FROM import_conflicts"),
            [("m3", "m2")], "同一天两机都采了：后到的包与前一个包的 claim 构成冲突")
        # 同键：m3 更晚，整行取 m3 的
        self.assertEqual(
            self.rows(conn, "SELECT stock, price FROM inventory "
                            "WHERE shop_key='A01' AND offer_id='11'"), [(8, 1.5)])
        # 键不同：两台机器独有的商品都留着（行级并集）
        self.assertEqual(
            self.rows(conn, "SELECT offer_id, stock FROM inventory ORDER BY offer_id"),
            [("11", 8), ("22", 4), ("33", 2)])
        # 身份表：MIN/MAX + 描述列按最近一次观测取胜
        self.assertEqual(
            self.rows(conn, "SELECT first_seen_at, last_seen_at, product_name FROM products "
                            "WHERE offer_id='11'")[0],
            ("2026-09-01T00:00:00+00:00", D16_1000, "商品一"))
        self.assertEqual(
            self.rows(conn, "SELECT sku_name FROM skus WHERE offer_id='11' AND sku_id='s1'"),
            [("规格一甲",)])
        self.assertEqual(
            self.rows(conn, "SELECT first_seen_at, last_seen_at, shop_name FROM shops "
                            "WHERE shop_key='A01'")[0],
            ("2026-09-01T00:00:00+00:00", D16_1000, "店铺甲"))
        # 版本表：不同 observed_at 是不同键，历史观测都留着
        self.assertEqual(
            self.rows(conn, "SELECT observed_at FROM product_information_versions "
                            "WHERE offer_id='11' ORDER BY observed_at"),
            [(D16_0900,), (D16_1000,)])

    def test_same_key_rows_are_replaced_wholesale_not_column_merged(self):
        """取胜方整行替换：它的 NULL 也要盖掉旧值（不逐列拼、不非空覆盖）。"""
        conn = self.local()
        self.import_(conn, self.package("m2", machine_world(
            D16_0900, stock=5, product_name="商品一(旧)")))
        self.import_(conn, self.package("m3", machine_world(
            D16_1000, stock=8, product_name="商品一", inventory_name=None)))

        self.assertEqual(
            self.rows(conn, "SELECT product_name FROM inventory WHERE offer_id='11'"),
            [(None,)], "m3 的 NULL 盖掉 m2 的'商品一(旧)'——整行替换不是非空覆盖")
        self.assertEqual(
            self.rows(conn, "SELECT product_name FROM products WHERE offer_id='11'"),
            [("商品一",)])

    def test_import_order_does_not_change_the_resulting_library(self):
        m2 = self.package("m2", machine_world(D16_0900, stock=5, product_name="商品一(旧)",
                                              extra=[("33", 2)]))
        m3 = self.package("m3", machine_world(D16_1000, stock=8, product_name="商品一",
                                              extra=[("22", 4)]))

        first = self.local("m1")
        for package in (m2, m3):
            self.import_(first, package, machine_id="m1")
        second = self.local("m4")          # 另一台（纯汇总机编号）反过来导
        for package in (m3, m2):
            self.import_(second, package, machine_id="m4")

        self.assertEqual(dump(first), dump(second), "同一批包、任意顺序、任意本机编号，逐表逐行一致")

    def test_an_exact_time_tie_goes_to_the_larger_machine_id_either_order(self):
        """平局（两台机器的最大 observed_at 一模一样）按 machine_id 字典序定胜，与顺序无关。"""
        m2 = self.package("m2", machine_world(D16_1000, stock=5, product_name="商品二甲",
                                              sku_name="规格二", shop_name="店铺二"))
        m3 = self.package("m3", machine_world(D16_1000, stock=8, product_name="商品三",
                                              sku_name="规格三", shop_name="店铺三"))

        first = self.local("m1")
        self.import_(first, m2)
        self.import_(first, m3)
        second = self.local("m4")
        self.import_(second, m3, machine_id="m4")
        self.import_(second, m2, machine_id="m4")

        self.assertEqual(dump(first), dump(second), "平局也要顺序无关")
        self.assertEqual(
            self.rows(first, "SELECT stock FROM inventory WHERE offer_id='11'"), [(8,)],
            "m3 > m2，平局归 m3")
        self.assertEqual(
            self.rows(first, "SELECT product_name FROM products WHERE offer_id='11'"),
            [("商品三",)], "身份表平局同样按 machine_id")

    def test_a_losing_packages_unique_rows_survive(self):
        """败方"不重复的部分都保留"：半截清单的另一半不会被抹掉。"""
        conn = self.local()
        self.import_(conn, self.package("m3", machine_world(D16_1000, stock=8,
                                                            extra=[("22", 4)])))
        self.import_(conn, self.package("m2", machine_world(D16_0900, stock=5,
                                                            extra=[("33", 2)])))

        self.assertEqual(
            self.rows(conn, "SELECT offer_id, stock FROM inventory ORDER BY offer_id"),
            [("11", 8), ("22", 4), ("33", 2)], "更早的包只是赢不了同键，它独有的行照进")


class GranularityTests(MergeCase):
    """验收 1（续）：相反粒度整组取胜；多规格 SKU 集合取并集。"""

    def multi_package(self, machine_id, claim_at, *, stock_by_sku):
        rows = machine_world(claim_at, stock=1, product_name="商品一")
        rows["inventory"] = [("A01", "11", sku_id, DAY, stock, 1.5, "店铺甲", "商品一",
                              f"规格{sku_id}") for sku_id, stock in stock_by_sku.items()]
        rows["skus"] = [("11", f"规格{sku_id}", sku_id, claim_at, claim_at)
                        for sku_id in stock_by_sku]
        rows["assets"] = []
        rows["versions"] = [("A01", "11", claim_at, DAY, "商品一", None, None, None)]
        return self.package(machine_id, rows)

    def test_a_winning_multi_spec_package_clears_the_local_single_form(self):
        conn = self.local()
        crawl_locally(conn, observed_at=D16_1000, stock=7, sku_id="default")

        self.import_(conn, self.multi_package("m2", "2026-09-16T11:00:00+00:00",
                                              stock_by_sku={"s1": 3, "s2": 4}))

        self.assertEqual(
            self.rows(conn, "SELECT sku_id, stock FROM inventory ORDER BY sku_id"),
            [("s1", 3), ("s2", 4)], "后来居上的多规格把本机同日单规格整组清掉")

    def test_a_losing_multi_spec_package_does_not_mix_forms(self):
        conn = self.local()
        crawl_locally(conn, observed_at=D16_1000, stock=7, sku_id="default")

        self.import_(conn, self.multi_package("m2", D16_0900, stock_by_sku={"s1": 3, "s2": 4}))

        self.assertEqual(
            self.rows(conn, "SELECT sku_id, stock FROM inventory"),
            [("default", 7)], "更早的多规格整组丢掉，不与本机单规格并集")

    def test_multi_sku_sets_union_keeps_the_losers_extra_skus(self):
        conn = self.local()
        self.import_(conn, self.multi_package("m3", D16_0900, stock_by_sku={"s1": 3, "s2": 4}))
        self.import_(conn, self.multi_package("m2", D16_1000, stock_by_sku={"s1": 9}))

        self.assertEqual(
            self.rows(conn, "SELECT sku_id, stock FROM inventory ORDER BY sku_id"),
            [("s1", 9), ("s2", 4)], "同 SKU 取赢家、独有的 SKU 保留（库存快照提交语义）")

    def test_opposite_forms_across_two_packages_resolve_the_same_either_order(self):
        """两个包粒度相反：整组取胜的结果与导入顺序无关（胜方是 m3 的多规格）。"""
        single = self.package("m2", machine_world(D16_0900, stock=5, sku_id="default"))
        multi = self.multi_package("m3", D16_1000, stock_by_sku={"s1": 3, "s2": 4})

        first = self.local("m1")
        self.import_(first, single)
        self.import_(first, multi)
        second = self.local("m4")
        self.import_(second, multi, machine_id="m4")
        self.import_(second, single, machine_id="m4")

        self.assertEqual(dump(first), dump(second))
        self.assertEqual(
            self.rows(first, "SELECT sku_id FROM inventory ORDER BY sku_id"),
            [("s1",), ("s2",)], "单规格整组丢掉，不留同日混合形态")


class LocalCollectionTests(MergeCase):
    """验收 5：本机自身采集的 claim 不被更早的包覆盖；冲突账字段齐全。"""

    def test_an_earlier_package_does_not_overwrite_a_local_collection(self):
        conn = self.local("m1")
        crawl_locally(conn, observed_at=D16_1000, stock=7)
        package = self.package("m2", machine_world(D16_0900, stock=5, product_name="商品一(旧)",
                                                   extra=[("33", 2)]))

        result = self.import_(conn, package, machine_id="m1")

        self.assertEqual(result.conflicts, 1)
        self.assertEqual(
            self.rows(conn, "SELECT stock FROM inventory WHERE offer_id='11'"), [(7,)],
            "本机 10:00 采的没有被别人 09:00 的包覆盖")
        self.assertEqual(
            self.rows(conn, "SELECT offer_id, stock FROM inventory ORDER BY offer_id"),
            [("11", 7), ("33", 2)], "包侧独有的行照进")

    def test_the_conflict_entry_carries_both_sides_and_the_loser_counts(self):
        conn = self.local("m1")
        crawl_locally(conn, observed_at=D16_1000, stock=7)
        package = self.package("m2", machine_world(D16_0900, stock=5, product_name="商品一(旧)",
                                                   extra=[("33", 2)]))

        self.import_(conn, package, machine_id="m1")

        conflict = self.rows(
            conn, "SELECT observed_date, shop_key, winner_side, winner_machine, winner_at, "
                  "loser_machine, loser_at, loser_rows_replaced, loser_rows_kept "
                  "FROM import_conflicts")
        self.assertEqual(conflict, [(
            DAY, "A01", "local", "m1", D16_1000, "m2", D16_0900,
            1,   # 败方同键被覆盖：库存行 11/s1
            3,   # 败方保留：版本行 11@09:00、商品 33 的版本行与库存行
        )])

    def test_the_winning_package_records_the_conflict_the_other_way_round(self):
        conn = self.local("m1")
        crawl_locally(conn, observed_at=D16_0900, stock=7)
        package = self.package("m2", machine_world(D16_1000, stock=5, product_name="商品一(旧)"))

        self.import_(conn, package, machine_id="m1")

        self.assertEqual(
            self.rows(conn, "SELECT winner_side, winner_machine, loser_machine "
                            "FROM import_conflicts"),
            [("package", "m2", "m1")])
        self.assertEqual(
            self.rows(conn, "SELECT stock FROM inventory WHERE offer_id='11'"), [(5,)])

    def test_the_same_machine_is_not_a_conflict(self):
        conn = self.local("m2")
        crawl_locally(conn, observed_at=D16_1000, stock=7)   # 本机就是 m2：自己的旧包

        result = self.import_(conn, self.package(
            "m2", machine_world(D16_0900, stock=5, product_name="商品一(旧)")), machine_id="m2")

        self.assertEqual(result.conflicts, 0)
        self.assertEqual(self.rows(conn, "SELECT 1 FROM import_conflicts"), [])
        self.assertEqual(
            self.rows(conn, "SELECT stock FROM inventory WHERE offer_id='11'"), [(7,)])

    def test_the_generated_at_of_a_package_is_not_a_judgement(self):
        """判据是采集时刻，不是导出时刻：导出得再晚也赢不了本机更晚的采集。"""
        conn = self.local("m1")
        crawl_locally(conn, observed_at=D16_1000, stock=7)
        package = self.package("m2", machine_world(D16_0900, stock=5),
                               generated_at=dt.datetime(2026, 9, 30, 12, 0, tzinfo=CST))

        self.import_(conn, package, machine_id="m1")

        self.assertEqual(
            self.rows(conn, "SELECT stock FROM inventory WHERE offer_id='11'"), [(7,)])

    def test_the_account_records_the_winning_claim_and_description_sources(self):
        """取胜方账：观测表按 (店铺, 日期)、身份表按 (表, 主键) 记住谁是这个值/行的写者。"""
        conn = self.local("m1")
        for machine_id, claim_at, stock in (("m2", D16_0900, 5), ("m3", D16_1000, 8),
                                            ("m4", "2026-09-16T08:00:00+00:00", 1)):
            self.import_(conn, self.package(machine_id, machine_world(claim_at, stock=stock)),
                         machine_id="m1")

        self.assertEqual(
            self.rows(conn, "SELECT shop_key, observed_date, claim_at, machine_id "
                            "FROM merge_claims"),
            [("A01", DAY, D16_1000, "m3")],
            "更晚的 m4 包（08:00）不改变账里的取胜方")
        self.assertEqual(
            self.rows(conn, "SELECT seen_at, machine_id FROM merge_seen "
                            "WHERE table_name='products' AND row_key='11'"),
            [(D16_1000, "m3")])
        self.assertEqual(
            self.rows(conn, "SELECT seen_at, machine_id FROM merge_seen "
                            "WHERE table_name='shops' AND row_key='A01'"),
            [(D16_1000, "m3")])


class OverreachConflictTests(MergeCase):
    """票据 07 的联验点：越权补采的重复在汇总侧长成冲突，两边的账对得上号。

    本机 m1 越权采了 A01（本周计划归 m2）——采集侧的账（`plan_deviations`，轮次备注同源）
    记着「计划外多采」；计划机 m2 的包进来后，同一 (日期, 店铺) 上两个 claim 合成一条
    冲突。报告据此写「本机 …（计划外多采）与计划机 … 都采到」是票据 10 的事；这里先钉
    两边账的字段齐全、标识对得上（本机库的采集行直插，与上面同款夹具）。
    """

    def test_the_overreach_duplicate_becomes_a_conflict_both_ledgers_can_explain(self):
        conn = self.local("m1")
        db = dbmod.Database(conn)
        crawl_locally(conn, observed_at=D16_1000, stock=7)      # 本机越权采的那一遍
        opened = rounds.open(db, rounds.RoundRequest(
            DAY, (rounds.ShopScope("A01", "https://a01.example/", "店铺甲"),)))
        plan_step.record_deviations(
            db, opened.round, "m1",
            (plan_step.PlanDeviation(
                "A01", plan_step.DeviationKind.OVERREACH, "m2", "本周计划归 m2"),),
            now=dt.datetime(2026, 9, 16, 18, 0, tzinfo=CST))
        package = self.package("m2", machine_world(D16_0900, stock=5))  # 计划机的包

        self.import_(conn, package, machine_id="m1")

        conflict = self.rows(
            conn, "SELECT observed_date, shop_key, winner_machine, loser_machine "
                  "FROM import_conflicts")
        self.assertEqual(conflict, [(DAY, "A01", "m1", "m2")],
                         "本机 10:00 采的未被 m2 09:00 的包覆盖；重复长成一条冲突")
        deviation = self.rows(
            conn, "SELECT run_date, shop_key, machine_id, kind, planned_machine "
                  "FROM plan_deviations")
        self.assertEqual(deviation, [(DAY, "A01", "m1", "overreach", "m2")],
                         "采集侧账字段齐全，且 (日期, 店铺) 与冲突行对得上号")


class IdempotencyTests(MergeCase):
    """验收 2：同包重导跳过；整包事务失败全回滚、重跑等价首次。"""

    def test_the_same_package_is_imported_once(self):
        conn = self.local()
        package = self.package("m2", machine_world(D16_0900, stock=5, extra=[("33", 2)]))
        first = self.import_(conn, package)
        before = dump(conn)
        ledger_before = self.rows(conn, "SELECT * FROM import_packages")

        second = self.import_(conn, package)

        self.assertTrue(first.rows_inserted > 0)
        self.assertTrue(second.skipped, "同哈希直接跳过")
        self.assertFalse(second.failed)
        self.assertEqual(second.rows_inserted, first.rows_inserted, "回执回放首次那份计数")
        self.assertEqual(second.sha256, first.sha256)
        self.assertEqual(dump(conn), before, "跳过的导入不动任何表")
        self.assertEqual(self.rows(conn, "SELECT * FROM import_packages"), ledger_before)

    def test_a_failed_import_rolls_back_and_the_rerun_equals_a_first_import(self):
        conn = self.local()
        package = self.package("m2", machine_world(D16_0900, stock=5, extra=[("33", 2)]))

        with mock.patch.object(merge, "_merge_identity",
                               side_effect=sqlite3.OperationalError("disk I/O error")):
            failed = self.import_(conn, package)

        self.assertTrue(failed.failed)
        self.assertIn("已全部回滚", failed.failure)
        self.assertEqual(dump(conn), dump(dbmod.connect(self.tmp / "empty.db")),
                         "失败后库和全新的一样")
        self.assertEqual(self.rows(conn, "SELECT * FROM import_packages"), [])
        self.assertEqual(self.rows(conn, "SELECT * FROM import_conflicts"), [])
        self.assertEqual(self.rows(conn, "SELECT * FROM merge_claims"), [])
        self.assertEqual(self.rows(conn, "SELECT * FROM merge_seen"), [])

        rerun = self.import_(conn, package)
        fresh = self.local("m5")
        self.import_(fresh, package, machine_id="m5")

        self.assertFalse(rerun.failed, rerun.failure)
        self.assertEqual(dump(conn), dump(fresh), "重跑等价于首次导入")

    def test_a_file_that_is_not_a_package_fails_cleanly(self):
        conn = self.local()
        junk = self.tmp / "junk.db"
        junk.write_bytes(b"definitely not a database")

        result = self.import_(conn, junk)

        self.assertTrue(result.failed)
        self.assertIn("包读不出来", result.failure)

    def test_a_package_without_the_exchange_tables_fails_cleanly(self):
        conn = self.local()
        empty = self.tmp / "empty-package.db"
        sqlite_conn = sqlite3.connect(empty)
        sqlite_conn.execute("CREATE TABLE something_else (x TEXT)")
        sqlite_conn.commit()
        sqlite_conn.close()

        result = self.import_(conn, empty)

        self.assertTrue(result.failed)
        self.assertIn("包读不出来", result.failure)


class ProjectionTests(MergeCase):
    """验收 3：包的列集与本地不必相同（旧包多一列 / 少一列都能导）。"""

    def test_a_package_with_an_extra_column_still_imports(self):
        """`inventory.diff` 退役后旧包多一列：显式投影列，不 SELECT *。"""
        package = self.package("m2", machine_world(D16_0900, stock=5))
        sqlite_conn = sqlite3.connect(package)
        sqlite_conn.execute("ALTER TABLE inventory ADD COLUMN diff INTEGER")
        sqlite_conn.execute("UPDATE inventory SET diff=3")
        sqlite_conn.commit()
        sqlite_conn.close()

        conn = self.local()
        result = self.import_(conn, package)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(
            self.rows(conn, "SELECT stock, product_name FROM inventory WHERE offer_id='11'"),
            [(5, "商品一")])
        self.assertEqual(
            [row[1] for row in conn.execute("PRAGMA table_info(inventory)")].count("diff"), 0,
            "本机表没有 diff 列，也不会被包多出来的列带着改")

    def test_a_package_missing_a_column_imports_with_null(self):
        package = self.package("m2", machine_world(D16_0900, stock=5))
        sqlite_conn = sqlite3.connect(package)
        sqlite_conn.execute("ALTER TABLE inventory DROP COLUMN sku_name")
        sqlite_conn.execute("ALTER TABLE product_information_versions DROP COLUMN image_url")
        sqlite_conn.commit()
        sqlite_conn.close()

        conn = self.local()
        result = self.import_(conn, package)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(
            self.rows(conn, "SELECT stock, sku_name FROM inventory WHERE offer_id='11'"),
            [(5, None)], "包缺的列按 NULL 落库")
        self.assertEqual(
            self.rows(conn, "SELECT product_name, image_url FROM "
                            "product_information_versions WHERE offer_id='11'"),
            [("商品一", None)])

    def test_a_package_missing_an_identity_column_merges_over_an_existing_row(self):
        """身份表缺列（旧包少一列）最易露馅的一路：本机已有同主键行、包侧取胜。"""
        conn = self.local()
        conn.execute(
            "INSERT INTO products(offer_id, product_url, product_name, main_image_url, "
            "first_seen_at, last_seen_at) VALUES ('11', 'https://old.example/', '旧名', "
            "'https://img.example/old.jpg', ?, ?)", (SEEN_FIRST, D16_0900))
        conn.commit()
        package = self.package("m3", machine_world(D16_1000, stock=8, product_name="商品一"))
        sqlite_conn = sqlite3.connect(package)
        sqlite_conn.execute("ALTER TABLE products DROP COLUMN main_image_url")
        sqlite_conn.commit()
        sqlite_conn.close()

        result = self.import_(conn, package)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(
            self.rows(conn, "SELECT product_name, main_image_url FROM products "
                            "WHERE offer_id='11'"),
            [("商品一", None)],
            "描述列整份取取胜方；包缺的列按 None，不拿本机旧值兜")

    def test_a_package_missing_a_required_column_fails_cleanly(self):
        """包缺 NOT NULL 的主键/时间列 = 不成一个完整的包：整包失败回滚，不是崩溃。"""
        package = self.package("m2", machine_world(D16_0900, stock=5))
        sqlite_conn = sqlite3.connect(package)
        sqlite_conn.execute("ALTER TABLE products DROP COLUMN first_seen_at")
        sqlite_conn.commit()
        sqlite_conn.close()

        conn = self.local()
        result = self.import_(conn, package)

        self.assertTrue(result.failed)
        self.assertIn("已全部回滚", result.failure)
        self.assertEqual(dump(conn), dump(dbmod.connect(self.tmp / "empty2.db")),
                         "失败发生在中途（库存行已插过），整包仍然全回滚")
        self.assertEqual(self.rows(conn, "SELECT * FROM import_packages"), [])


def degrade_to_v1(package: pathlib.Path) -> pathlib.Path:
    """把一个真包演成 v1 旧包：新表 DROP、元数据里的 `rows_` 键删掉、口径版本改回 v1。

    照 ProjectionTests 那组兼容用例的改包手法（在包文件上动手、不改打包代码）与
    test_export 那条「上一版格式的包」的造法——上一版程序发的包就长这样：没有 SKU 图
    流水，元数据里也没有它的行数键。
    """
    path = package.with_name(package.stem + "-v1.db")
    shutil.copyfile(package, path)
    sqlite_conn = sqlite3.connect(path)
    sqlite_conn.execute("DROP TABLE sku_image_versions")
    sqlite_conn.execute("DELETE FROM exchange_meta WHERE key='rows_sku_image_versions'")
    sqlite_conn.execute("UPDATE exchange_meta SET value='v1' WHERE key='format_version'")
    sqlite_conn.commit()
    sqlite_conn.close()
    return path


class LegacyPackageTests(MergeCase):
    """验收 6：旧包（v1 格式）照收——SKU 图这半为无；老六表缺席仍当场报错。"""

    def test_a_v1_package_without_the_new_table_or_its_meta_key_imports(self):
        """旧包照收：其余各表逐行落库，导入计数如实（少的就是新表那半）。"""
        package = self.package("m2", machine_world(D16_0900, stock=5, extra=[("33", 2)]))

        conn = self.local()
        result = self.import_(conn, degrade_to_v1(package))

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(result.rows_total, 10, "六表九行 + 资产表一行（新表这半为无）")
        self.assertEqual(result.rows_inserted, 9, "资产行走图片通道，不算「插入」")
        self.assertEqual(
            self.rows(conn, "SELECT offer_id, stock FROM inventory ORDER BY offer_id"),
            [("11", 5), ("33", 2)], "旧包照收，各行齐全")
        self.assertEqual(self.rows(conn, "SELECT 1 FROM sku_image_versions"), [])

    def test_a_package_missing_one_of_the_old_six_tables_still_fails_loudly(self):
        """老六表缺席仍是「那不是交换集里的包」：一表演一遍，整包当场报错、库不动。"""
        empty = dbmod.connect(self.tmp / "empty-legacy.db")
        self.addCleanup(empty.close)
        for table in ("shops", "products", "skus", "inventory",
                      "product_information_versions", "product_image_assets"):
            package = self.package(f"m2-{table}", machine_world(D16_0900, stock=5))
            sqlite_conn = sqlite3.connect(package)
            sqlite_conn.execute(f"DROP TABLE {table}")
            sqlite_conn.commit()
            sqlite_conn.close()

            conn = self.local(table)
            result = self.import_(conn, package)

            self.assertTrue(result.failed, table)
            self.assertIn(f"包里没有 {table} 表", result.failure, table)
            self.assertEqual(dump(conn), dump(empty), table)


class ImageTests(MergeCase):
    """验收 4：先拉图再插行；缺图照插、缺口如实。

    版本行与 SKU 图流水行的引用都走这一套：包里的资产表是两者的并集（导出侧），
    所以这里不为新表加分支，只用用例把这条钉住。
    """

    def test_images_are_pulled_before_any_row_lands(self):
        conn = self.local()
        store = FakeImageStore({KEY1: JPEG})
        seen = {}

        original = store.fetch

        def spy(key):
            seen["versions_seen_at_fetch"] = conn.execute(
                "SELECT COUNT(*) FROM product_information_versions").fetchone()[0]
            return original(key)

        store.fetch = spy
        result = self.import_(conn, self.package(
            "m2", machine_world(D16_0900, stock=5)), store=store)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(seen["versions_seen_at_fetch"], 0, "先拉图、再插行")
        self.assertEqual(result.images_pulled, 1)
        self.assertEqual(store.fetched, [KEY1])
        self.assertEqual(
            self.rows(conn, "SELECT content_hash, mime, content FROM product_image_assets"),
            [(H1, "image/jpeg", JPEG)], "图片按内容寻址落本机资产表（含字节）")

    def test_images_already_local_are_not_fetched_again(self):
        conn = self.local()
        conn.execute("INSERT INTO product_image_assets VALUES (?, ?, ?)",
                     (H1, "image/jpeg", JPEG))
        conn.commit()
        store = FakeImageStore({KEY1: JPEG})

        result = self.import_(conn, self.package(
            "m2", machine_world(D16_0900, stock=5)), store=store)

        self.assertEqual((result.images_pulled, result.images_skipped), (0, 1))
        self.assertEqual(store.fetched, [])

    def test_missing_images_do_not_block_the_rows_and_are_counted(self):
        conn = self.local()
        store = FakeImageStore()          # 桶里什么都没有

        result = self.import_(conn, self.package(
            "m2", machine_world(D16_0900, stock=5)), store=store)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(result.images_missing, 1)
        self.assertIn("对象不存在", result.images_note)
        self.assertEqual(
            self.rows(conn, "SELECT stock FROM inventory WHERE offer_id='11'"), [(5,)],
            "缺图照插：观测是既成事实")
        self.assertEqual(
            self.rows(conn, "SELECT content_hash FROM product_information_versions "
                            "WHERE offer_id='11'"), [(H1,)],
            "版本行照插，content_hash 指向本机还没有字节的资产")
        self.assertEqual(
            self.rows(conn, "SELECT 1 FROM product_image_assets"), [])

    def test_bytes_that_do_not_match_the_hash_are_not_stored(self):
        conn = self.local()
        store = FakeImageStore({KEY1: b"corrupted-bytes"})

        result = self.import_(conn, self.package(
            "m2", machine_world(D16_0900, stock=5)), store=store)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(result.images_missing, 1)
        self.assertIn("对不上哈希", result.images_note)
        self.assertEqual(self.rows(conn, "SELECT 1 FROM product_image_assets"), [])

    def test_no_bucket_configured_reports_the_gap(self):
        conn = self.local()

        result = self.import_(conn, self.package(
            "m2", machine_world(D16_0900, stock=5)), store=None)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(result.images_missing, 1)
        self.assertIn("cos_bucket", result.images_note)

    def test_a_ledger_only_image_is_pulled_before_any_ledger_row_lands(self):
        """只在 SKU 图流水里出现的图（版本行不引用）也走「先拉图、再插行」。"""
        conn = self.local()
        store = FakeImageStore({KEY1: JPEG, KEY2: PNG})
        seen = {}
        original = store.fetch

        def spy(key):
            seen["ledger_rows_at_fetch"] = conn.execute(
                "SELECT COUNT(*) FROM sku_image_versions").fetchone()[0]
            return original(key)

        store.fetch = spy
        rows = machine_world(D16_0900, stock=5, sku_images=[
            sku_image("s1", content_hash=H2, url="https://img.example/sku-2.png")])
        rows["assets"].append((H2, "image/png", PNG))

        result = self.import_(conn, self.package("m2", rows), store=store)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(seen["ledger_rows_at_fetch"], 0, "先拉图、再插行")
        self.assertEqual(result.images_pulled, 2, "版本行那张与只在流水里的那张都拉了")
        self.assertIn(KEY2, store.fetched)
        self.assertEqual(
            self.rows(conn, "SELECT mime, content FROM product_image_assets "
                            "WHERE content_hash=?", (H2,)),
            [("image/png", PNG)], "流水引用的字节落进本机资产池")
        self.assertEqual(
            self.rows(conn, "SELECT content_hash FROM sku_image_versions"), [(H2,)])

    def test_a_ledger_image_already_local_is_not_fetched_again(self):
        conn = self.local()
        conn.executemany("INSERT INTO product_image_assets VALUES (?,?,?)",
                         [(H1, "image/jpeg", JPEG), (H2, "image/png", PNG)])
        conn.commit()
        store = FakeImageStore({KEY1: JPEG, KEY2: PNG})
        rows = machine_world(D16_0900, stock=5, sku_images=[
            sku_image("s1", content_hash=H2, url="https://img.example/sku-2.png")])
        rows["assets"].append((H2, "image/png", PNG))

        result = self.import_(conn, self.package("m2", rows), store=store)

        self.assertEqual((result.images_pulled, result.images_skipped), (0, 2))
        self.assertEqual(store.fetched, [])
        self.assertEqual(
            self.rows(conn, "SELECT content_hash FROM sku_image_versions"), [(H2,)])

    def test_ledger_bytes_that_do_not_match_the_hash_are_not_stored(self):
        conn = self.local()
        conn.execute("INSERT INTO product_image_assets VALUES (?,?,?)", (H1, "image/jpeg", JPEG))
        conn.commit()
        store = FakeImageStore({KEY2: b"corrupted-bytes"})
        rows = machine_world(D16_0900, stock=5, sku_images=[
            sku_image("s1", content_hash=H2, url="https://img.example/sku-2.png")])
        rows["assets"].append((H2, "image/png", PNG))

        result = self.import_(conn, self.package("m2", rows), store=store)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(result.images_missing, 1)
        self.assertIn("对不上哈希", result.images_note)
        self.assertEqual(
            self.rows(conn, "SELECT 1 FROM product_image_assets WHERE content_hash=?", (H2,)),
            [], "对不上内容寻址哈希的字节不落库")
        self.assertEqual(
            self.rows(conn, "SELECT content_hash FROM sku_image_versions"), [(H2,)],
            "流水行照插：本机缺字节的行如实保留")

    def test_missing_ledger_images_do_not_block_the_rows_and_are_counted(self):
        conn = self.local()
        conn.execute("INSERT INTO product_image_assets VALUES (?,?,?)", (H1, "image/jpeg", JPEG))
        conn.commit()
        rows = machine_world(D16_0900, stock=5, sku_images=[
            sku_image("s1", content_hash=H2, url="https://img.example/sku-2.png")])
        rows["assets"].append((H2, "image/png", PNG))

        result = self.import_(conn, self.package("m2", rows), store=FakeImageStore())

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(result.images_missing, 1)
        self.assertIn("对象不存在", result.images_note)
        self.assertEqual(
            self.rows(conn, "SELECT offer_id, stock FROM inventory WHERE offer_id='11'"),
            [("11", 5)], "缺图不挡导入：观测是既成事实")
        self.assertEqual(
            self.rows(conn, "SELECT content_hash FROM sku_image_versions"), [(H2,)])

    def test_bytes_arriving_with_a_later_package_fill_the_earlier_rows(self):
        """本机缺字节的行如实留着；对方补传的字节随下次收包到齐（行不用重写）。"""
        conn = self.local()
        first = self.import_(conn, self.package("m2", machine_world(
            D16_0900, stock=5, sku_images=[sku_image()])), store=None)

        self.assertFalse(first.failed, first.failure)
        self.assertEqual(self.rows(conn, "SELECT 1 FROM product_image_assets"), [],
                         "没配桶：这次没拉到字节（这半如实记缺）")

        later = self.import_(conn, self.package("m3", machine_world(
            D16_1000, stock=8, sku_images=[sku_image(observed_at=D16_1000)])),
            store=FakeImageStore({KEY1: JPEG}))

        self.assertEqual(later.images_pulled, 1, "补传的那张随这个包拉回来")
        self.assertEqual(
            self.rows(conn, "SELECT content FROM product_image_assets WHERE content_hash=?",
                      (H1,)), [(JPEG,)])
        self.assertEqual(
            self.rows(conn, "SELECT content_hash FROM sku_image_versions "
                            "ORDER BY observed_at"), [(H1,), (H1,)],
            "先收的那行照旧，现在读得到字节了")


class SkuImageMergeTests(MergeCase):
    """验收 5：SKU 图流水按观测表一侧合并——逐行先查后写，无胜负裁决。"""

    def test_ledger_rows_land_with_their_source_and_count_as_inserted(self):
        """新表照收：来源三态原样落库，每条流水行各算一条插入。"""
        conn = self.local()
        rows = machine_world(D16_0900, stock=5, sku_images=[
            sku_image("s1", content_hash=H1, url="https://img.example/sku-1.jpg"),
            sku_image("s2", content_hash=H1, source="主图代填"),
            sku_image("s3", content_hash=None, source="无图"),
            sku_image("s4", content_hash=None, image_error="超时",
                      url="https://img.example/sku-4.png"),
        ])

        result = self.import_(conn, self.package("m2", rows))

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(result.rows_inserted, 9, "交换集五行 + 四条流水行")
        self.assertEqual(
            self.rows(conn, "SELECT sku_id, observed_at, content_hash, image_error, source "
                            "FROM sku_image_versions ORDER BY sku_id"),
            [("s1", D16_0900, H1, None, "专属图"),
             ("s2", D16_0900, H1, None, "主图代填"),
             ("s3", D16_0900, None, None, "无图"),
             ("s4", D16_0900, None, "超时", "专属图")],
            "行照包侧原样落：观测时刻、哈希、失败原因与来源三态都在")

    def test_ledger_rows_of_two_machines_are_kept_side_by_side(self):
        """各机观测天然互异：两台机器同一个商品的流水行都留着，没有胜负裁决。"""
        conn = self.local()
        self.import_(conn, self.package("m2", machine_world(
            D16_0900, stock=5, sku_images=[sku_image()])))
        self.import_(conn, self.package("m3", machine_world(
            D16_1000, stock=8, sku_images=[
                sku_image(observed_at=D16_1000, url="https://img.example/sku-2.png")])))

        self.assertEqual(
            self.rows(conn, "SELECT observed_at, image_url FROM sku_image_versions "
                            "ORDER BY observed_at"),
            [(D16_0900, None), (D16_1000, "https://img.example/sku-2.png")])

    def test_a_ledger_row_that_already_landed_is_skipped_not_duplicated(self):
        """同一个观测随另一个包又来一趟（换周交接那类）：只留一行。

        本机去重索引没建起来（老库：UNIQUE 建不上被迁移跳过）也照样挡得住——
        先查后写，不依赖索引在不在（版本表先例的同一课）。
        """
        conn = self.local()
        conn.execute("DROP INDEX idx_sku_image_dedupe")
        conn.commit()
        rows = machine_world(D16_0900, stock=5, sku_images=[sku_image()])
        self.import_(conn, self.package("m2", rows))

        again = self.import_(conn, self.package(
            "m3", rows, generated_at=dt.datetime(2026, 9, 22, 9, 0, tzinfo=CST)))

        self.assertFalse(again.failed, again.failure)
        self.assertEqual(
            self.rows(conn, "SELECT COUNT(*) FROM sku_image_versions"), [(1,)],
            "同键的流水行没有被重复插入")
        self.assertEqual(again.rows_inserted, 0, "这一趟没有任何新行")

    def test_ledger_rows_do_not_enter_the_claim_decision(self):
        """claim 裁决仍只管库存与版本表：本机采得更晚时，包里的流水行照收，
        冲突的败方计数与没带流水行时一模一样（流水行不算「保留的行」）。"""
        conn = self.local("m1")
        crawl_locally(conn, observed_at=D16_1000, stock=7)
        rows = machine_world(D16_0900, stock=5, extra=[("33", 2)], sku_images=[
            sku_image("s1"), sku_image("s2", url="https://img.example/sku-2.png")])

        result = self.import_(conn, self.package("m2", rows), machine_id="m1")

        self.assertEqual(
            self.rows(conn, "SELECT winner_side, winner_machine, loser_machine, "
                            "loser_rows_replaced, loser_rows_kept FROM import_conflicts"),
            [("local", "m1", "m2", 1, 3)])
        self.assertEqual(
            self.rows(conn, "SELECT sku_id FROM sku_image_versions ORDER BY sku_id"),
            [("s1",), ("s2",)], "流水行照收（不参与谁赢谁输）")
        self.assertEqual(result.rows_inserted, 10,
                         "本机库里只有库存与版本各一行（身份表还空着）：身份五行 + 包侧独有的"
                         "库存一行 + 版本两行 + 流水两行，流水行照票面进这一份计数")


class EndToEndTests(MergeCase):
    """验收 6（端到端一条）：采集落库 → 导出 → 另一库汇总 → 两端一致；再演一遍旧包照收。"""

    def collected_source(self) -> tuple[pathlib.Path, dict]:
        """走采集写入路径造一份源库：三个 SKU，分别是专属图 / 主图代填 / 无图。"""
        path = self.tmp / "collected.db"
        conn = dbmod.open(path)
        self.addCleanup(conn.close)
        blue, main = product_picture("blue"), product_picture("red")
        database = dbmod.Database(conn)
        database.submit_inventory_snapshot(
            round_id=new_round(database, "A01", run_date=DAY),
            shop_key="A01", shop_url="https://a01.example/", shop_name="店铺甲",
            offer_id="11", product_url="https://detail.1688.com/offer/11.html",
            list_title="商品一", detail_title="商品一",
            main_image_url="https://img.example/main.png", image_evidence=main,
            collected_at="2026-09-16T10:00:00+08:00", attempt=1,
            sku_rows=[
                {"sku_id": "own", "sku_name": "蓝", "sku_stock": 3,
                 "sku_image_evidence": {"url": "https://img.example/blue.png",
                                        "source": dbmod.SKU_IMAGE_OWN, **blue}},
                {"sku_id": "filled", "sku_name": "素色", "sku_stock": 2,
                 "sku_image_evidence": {"url": None, "source": dbmod.SKU_IMAGE_FILLED}},
                {"sku_id": "blank", "sku_name": "随机", "sku_stock": 1,
                 "sku_image_evidence": {"url": None, "source": dbmod.SKU_IMAGE_NONE}},
            ])
        return path, {"blue": blue, "main": main}

    def test_a_collected_week_round_trips_with_the_same_ledger_and_bytes(self):
        source, pictures = self.collected_source()
        package = self.tmp / "packages" / "2026-W38-m2.db"
        export.build_package(source, package, week="2026-W38", machine_id="m2",
                             crawl_in_progress=False,
                             generated_at=dt.datetime(2026, 9, 21, 10, 30, tzinfo=CST))
        store = FakeImageStore({
            image_key(picture["hash"], picture["mime"]): picture["content"]
            for picture in pictures.values()})
        conn = self.local("m4")
        source_conn = dbmod.open(source)
        self.addCleanup(source_conn.close)

        result = self.import_(conn, package, machine_id="m4", store=store)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(
            dump(conn)["sku_image_versions"], dump(source_conn)["sku_image_versions"],
            "两端 SKU 图流水逐行一致（列集与行序取自包格式定义那一处）")
        self.assertEqual(
            self.rows(conn, "SELECT sku_id, source FROM sku_image_versions ORDER BY sku_id"),
            [("blank", "无图"), ("filled", "主图代填"), ("own", "专属图")],
            "三种来源都到了：代填行与无图行是这条要钉住的形态")
        referenced = [hash_ for hash_, in self.rows(
            conn, "SELECT DISTINCT content_hash FROM sku_image_versions "
                  "WHERE content_hash IS NOT NULL")]
        self.assertEqual(len(referenced), 2, "专属图那张与代填的主图那张")
        for content_hash in referenced:
            self.assertEqual(
                self.rows(conn, "SELECT mime, content FROM product_image_assets "
                                "WHERE content_hash=?", (content_hash,)),
                self.rows(source_conn, "SELECT mime, content FROM product_image_assets "
                                       "WHERE content_hash=?", (content_hash,)),
                "引用的字节在本机资产表里对得上")

        as_v1 = degrade_to_v1(package)
        other = self.local("m5")
        legacy = self.import_(other, as_v1, machine_id="m5", store=store)

        self.assertFalse(legacy.failed, legacy.failure)
        self.assertEqual(self.rows(other, "SELECT 1 FROM sku_image_versions"), [],
                         "同一个包演成旧包：照收，只是 SKU 图这半为无")
        self.assertEqual(
            self.rows(other, "SELECT offer_id, sku_id, stock FROM inventory ORDER BY sku_id"),
            self.rows(conn, "SELECT offer_id, sku_id, stock FROM inventory ORDER BY sku_id"),
            "其余各表与收新包那份逐行一致")


class PackageFormTests(MergeCase):
    """发布形态（.db.gz）与包文件身份。"""

    def test_a_gzipped_published_package_imports_and_is_identified_by_its_bytes(self):
        package = self.package("m2", machine_world(D16_0900, stock=5))
        published = package.with_suffix(".db.gz")
        published.write_bytes(gzip.compress(package.read_bytes(), mtime=0))

        conn = self.local()
        first = self.import_(conn, published)
        second = self.import_(conn, published)

        expected_sha = hashlib.sha256(published.read_bytes()).hexdigest()
        self.assertFalse(first.failed, first.failure)
        self.assertEqual(first.sha256, expected_sha, "包身份 = 包文件内容的 SHA-256")
        self.assertEqual(
            self.rows(conn, "SELECT stock FROM inventory WHERE offer_id='11'"), [(5,)])
        self.assertTrue(second.skipped)


if __name__ == "__main__":
    unittest.main()
