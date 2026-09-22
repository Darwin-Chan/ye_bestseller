"""票据 08、18：图片通道的接缝——key 的推法、清单解析、coscli 适配器。

coscli 是外部二进制（系统边界），用例给它打桩；解析逻辑对着真输出样例单独钉——
夹具于票据 18 按 2026-09-23 m4 上 coscli v1.0.9 的实测输出重录（表格格式，见 ADR-0041）。
"""
from __future__ import annotations

import pathlib
import subprocess
import unittest
from unittest import mock

from bestseller_monitor.cos_store import CosCliImageStore, parse_listing
from bestseller_monitor.image_store import ImageStoreError, image_key

H1 = "ab" + "1" * 62
H2 = "cd" + "2" * 62

# coscli v1.0.9 `ls -r` 的真实输出形态（2026-09-23 在 m4 上实测）：表格排版，数据行首列是
# 裸 key，末尾一条 TOTAL OBJECTS 汇总行。夹具按真机一屏删节重录——真哈希换成 H1/H2、行裁
# 到这三条、行尾空白割掉，列位照原样（汇总行的 `|` 与数据行同列）；第一行数据是 key 恰为
# `img/` 的零字节探察物，照常按「存在」算。原始一屏见 ticket18 的 real-listing.txt。
LISTING = f"""\
                                      KEY                                     |     TYPE     |       LAST MODIFIED       |                ETAG                |      SIZE       | RESTORESTATUS
------------------------------------------------------------------------------+--------------+---------------------------+------------------------------------+-----------------+----------------
  img/                                                                        | MAZ_STANDARD | 2026-09-23T01:31:32+08:00 | "d41d8cd98f00b204e9800998ecf8427e" | 0.00 B          |
  img/ab/{H1}.jpg | MAZ_STANDARD | 2026-09-23T00:51:12+08:00 | "0f3ed164ec045887f69ba2fc9015ae44" | 160.88 KB       |
  img/cd/{H2}.png | MAZ_STANDARD | 2026-09-23T00:51:12+08:00 | "7bb9b61533039d1f6595d3c4f69c11d8" | 75.30 KB        |
------------------------------------------------------------------------------+--------------+---------------------------+------------------------------------+-----------------+----------------
                                                                                                                                                                TOTAL OBJECTS:  |      3
                                                                                                                                                              ------------------+----------------
"""

# 空前缀/空桶的真实形态（2026-09-23 实测，样本 ticket18 的 real-empty-listing.txt）：紧凑
# 表头 + 「TOTAL OBJECTS: 0」，没有数据行。
EMPTY_LISTING = """  KEY | TYPE | LAST MODIFIED | ETAG |      SIZE       | RESTORESTATUS
------+------+---------------+------+-----------------+----------------
------+------+---------------+------+-----------------+----------------
                                      TOTAL OBJECTS:  |       0
                                    ------------------+----------------
"""

# 票 08 起写下的 URL 行形态——零实测样本（票 08 自己注明真机契约留待实测），保留是防某台
# 机器上的别的 coscli 版本再走「认不出 → 全量重传」的老路；两种形态之外的行不猜。
LEGACY_LISTING = f"""cos://bucket/img/ab/{H1}.jpg   12345   2026-09-20 19:41:00 +0800 CST
cos://bucket/img/cd/{H2}.png   6789   2026-09-20 19:42:00 +0800 CST

Total 2 objects
"""


class ImageKeyTests(unittest.TestCase):
    def test_key_is_content_addressed_with_the_extension_from_mime(self):
        self.assertEqual(image_key(H1, "image/jpeg"), f"img/ab/{H1}.jpg")
        self.assertEqual(image_key(H2, "image/png"), f"img/cd/{H2}.png")
        self.assertEqual(image_key(H2, "image/webp"), f"img/cd/{H2}.webp")
        self.assertEqual(image_key(H1, "image/gif"), f"img/ab/{H1}.gif")

    def test_an_unknown_mime_is_rejected_loudly(self):
        with self.assertRaisesRegex(ImageStoreError, "image/avif"):
            image_key(H1, "image/avif")


class ParseListingTests(unittest.TestCase):
    def test_a_real_table_listing_yields_the_keys(self):
        keys = parse_listing(LISTING)

        self.assertEqual(keys, {"img/", f"img/ab/{H1}.jpg", f"img/cd/{H2}.png"})

    def test_legacy_url_rows_still_parse(self):
        keys = parse_listing(LEGACY_LISTING)

        self.assertEqual(keys, {f"img/ab/{H1}.jpg", f"img/cd/{H2}.png"})

    def test_an_empty_listing_yields_nothing(self):
        self.assertEqual(parse_listing(EMPTY_LISTING), set())

    def test_keys_outside_the_image_prefix_are_not_ours(self):
        url_row = f"cos://bucket/other/{H1}.jpg   1   2026-09-20 19:41:00 +0800 CST\n"
        table_row = f'  other/{H1}.jpg | MAZ_STANDARD | 2026-09-23T00:51:12+08:00 | "e" | 1 KB |\n'

        self.assertEqual(parse_listing(url_row), set())
        self.assertEqual(parse_listing(table_row), set())

    def test_a_summary_that_contradicts_the_rows_is_loud(self):
        """汇总行说有对象、却一个 key 都没认出 = 输出形态变了：报错拒走，不再悄悄全量重传。"""
        drifted = ("  one | unfamiliar | shape\n"
                   "                                      TOTAL OBJECTS:  |       855\n")

        with self.assertRaisesRegex(ImageStoreError, "形态"):
            parse_listing(drifted)


class CosCliTests(unittest.TestCase):
    def store(self) -> CosCliImageStore:
        return CosCliImageStore("bucket")

    def test_existing_keys_asks_coscli_for_the_img_prefix(self):
        done = subprocess.CompletedProcess([], 0, stdout=LISTING, stderr="")
        with mock.patch("bestseller_monitor.cos_store.subprocess.run",
                        return_value=done) as run:
            keys = self.store().existing_keys()

        self.assertEqual(run.call_args.args[0],
                         ["coscli", "ls", "-r", "cos://bucket/img/"])
        self.assertEqual(keys, {"img/", f"img/ab/{H1}.jpg", f"img/cd/{H2}.png"})

    def test_a_bucket_with_objects_yields_a_nonempty_key_set(self):
        """2026-09-23 的缺陷场景：真机一屏（表格输出）曾解析出 0 个 key → 每趟全量重传。"""
        done = subprocess.CompletedProcess([], 0, stdout=LISTING, stderr="")
        with mock.patch("bestseller_monitor.cos_store.subprocess.run",
                        return_value=done):
            keys = self.store().existing_keys()

        self.assertTrue(keys)
        self.assertIn(f"img/ab/{H1}.jpg", keys)
        self.assertIn(f"img/cd/{H2}.png", keys)

    def test_a_nonzero_exit_is_a_store_error_with_the_output(self):
        done = subprocess.CompletedProcess([], 1, stdout="",
                                           stderr="AccessDenied: no such bucket")
        with mock.patch("bestseller_monitor.cos_store.subprocess.run",
                        return_value=done):
            with self.assertRaisesRegex(ImageStoreError, "AccessDenied"):
                self.store().existing_keys()

    def test_a_missing_binary_points_at_the_onboarding_step(self):
        with mock.patch("bestseller_monitor.cos_store.subprocess.run",
                        side_effect=FileNotFoundError("coscli")):
            with self.assertRaisesRegex(ImageStoreError, "上机清单"):
                self.store().existing_keys()

    def test_upload_sends_the_bytes_through_a_temp_file(self):
        seen: dict[str, bytes] = {}

        def fake_run(argv, **kwargs):
            local = pathlib.Path(argv[2])
            seen["argv"] = list(argv)
            seen["data"] = local.read_bytes()
            seen["existed_at_call"] = local.exists()
            seen["path"] = local
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch("bestseller_monitor.cos_store.subprocess.run",
                        side_effect=fake_run):
            self.store().upload(f"img/ab/{H1}.jpg", b"jpeg-bytes")

        self.assertEqual(seen["argv"][:2], ["coscli", "cp"])
        self.assertEqual(seen["argv"][3], f"cos://bucket/img/ab/{H1}.jpg")
        self.assertEqual(seen["data"], b"jpeg-bytes")
        self.assertTrue(seen["existed_at_call"])
        self.assertFalse(seen["path"].exists(), "临时文件用完就清")

    def test_upload_failure_is_a_store_error(self):
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="403 Forbidden")
        with mock.patch("bestseller_monitor.cos_store.subprocess.run",
                        return_value=done):
            with self.assertRaisesRegex(ImageStoreError, "403"):
                self.store().upload(f"img/ab/{H1}.jpg", b"jpeg-bytes")

    def test_fetch_downloads_through_a_temp_file_and_returns_the_bytes(self):
        """汇总导入拉图用的取口：coscli 只吃文件路径，先 cp 到临时文件再读。"""
        seen: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            path = pathlib.Path(argv[3])
            seen["argv"] = list(argv)
            seen["path"] = path
            path.write_bytes(b"jpeg-bytes")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch("bestseller_monitor.cos_store.subprocess.run",
                        side_effect=fake_run):
            data = self.store().fetch(f"img/ab/{H1}.jpg")

        self.assertEqual(data, b"jpeg-bytes")
        self.assertEqual(seen["argv"][:2], ["coscli", "cp"])
        self.assertEqual(seen["argv"][2], f"cos://bucket/img/ab/{H1}.jpg")
        self.assertFalse(seen["path"].exists(), "临时文件用完就清")

    def test_fetch_failure_is_a_store_error(self):
        done = subprocess.CompletedProcess([], 1, stdout="",
                                           stderr="NoSuchKey: The specified key does not exist")
        with mock.patch("bestseller_monitor.cos_store.subprocess.run",
                        return_value=done):
            with self.assertRaisesRegex(ImageStoreError, "NoSuchKey"):
                self.store().fetch(f"img/ab/{H1}.jpg")


if __name__ == "__main__":
    unittest.main()
