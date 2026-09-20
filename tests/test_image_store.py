"""票据 08：图片通道的接缝——key 的推法、清单解析、coscli 适配器。

coscli 是外部二进制（系统边界），用例给它打桩；解析逻辑对着真输出样例单独钉。
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

# coscli ls -r 的真实输出形态：一条 key 一行（URL、大小、时间），末尾可能有汇总行。
LISTING = f"""cos://bucket/img/ab/{H1}.jpg   12345   2026-09-20 19:41:00 +0800 CST
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
    def test_keys_come_out_of_coscli_output(self):
        keys = parse_listing(LISTING, prefix="img/")

        self.assertEqual(keys, {f"img/ab/{H1}.jpg", f"img/cd/{H2}.png"})

    def test_lines_without_a_cos_url_are_ignored(self):
        self.assertEqual(parse_listing("Total 0 objects\n\n", prefix="img/"), set())

    def test_keys_outside_the_prefix_are_not_ours(self):
        text = f"cos://bucket/other/{H1}.jpg   1   2026-09-20 19:41:00 +0800 CST\n"

        self.assertEqual(parse_listing(text, prefix="img/"), set())


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
        self.assertEqual(keys, {f"img/ab/{H1}.jpg", f"img/cd/{H2}.png"})

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


if __name__ == "__main__":
    unittest.main()
