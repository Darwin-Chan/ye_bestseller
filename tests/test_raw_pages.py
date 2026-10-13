"""原始页面存档的落点约定（ADR-0057）：命名、落点，与既有两个写点的一致性。

这些只测外部可见的东西——文件名与目录、写出的内容、以及 `detail.save_raw_page` /
`listing.save_raw_listing_page` 改走一处之后磁盘表现不变。不测内部实现。
"""
import unittest

from helpers import crawler_cfg

from bestseller_monitor import detail, listing, raw_pages


class RawPagesTests(unittest.TestCase):
    def setUp(self):
        self.cfg = crawler_cfg()

    def test_detail_page_keeps_the_offer_id_name(self):
        path = raw_pages.save_detail_page(self.cfg, 7, "11", "<html>x</html>")
        self.assertEqual(path, self.cfg.raw_page_dir / "round_7" / "11.html")
        self.assertEqual(path.read_text(encoding="utf-8"), "<html>x</html>")

    def test_listing_page_keeps_the_listing_prefix_and_sanitizes_the_key(self):
        path = raw_pages.save_listing_page(self.cfg, 7, "A:01/店", "<html>x</html>")
        self.assertEqual(path.parent, self.cfg.raw_page_dir / "round_7")
        self.assertEqual(path.name, "listing_A_01_.html")

    def test_intervention_page_carries_the_offer_and_the_type(self):
        path = raw_pages.save_intervention_page(self.cfg, 7, "11", "slider", "<html>x</html>")
        self.assertEqual(path, self.cfg.raw_page_dir / "round_7" / "11.slider.html")

    def test_a_slider_and_a_deny_for_one_offer_do_not_collide(self):
        """同一个商品先撞滑块、后吃 deny 是两份证据，不该互相覆盖（ADR-0057）。"""
        slider = raw_pages.save_intervention_page(self.cfg, 7, "11", "slider", "<html>s</html>")
        deny = raw_pages.save_detail_page(self.cfg, 7, "11", "<html>d</html>")
        self.assertNotEqual(slider, deny)
        self.assertTrue(slider.exists() and deny.exists())

    def test_a_listing_intervention_has_no_offer_identity(self):
        path = raw_pages.save_listing_intervention_page(
            self.cfg, 7, "A01", "login", "<html>x</html>")
        self.assertEqual(path.parent, self.cfg.raw_page_dir / "round_7")
        self.assertEqual(path.name, "intervention_A01.login.html")

    def test_relpath_is_relative_to_the_raw_dir(self):
        path = raw_pages.save_detail_page(self.cfg, 7, "11", "<html>x</html>")
        self.assertEqual(raw_pages.relpath(self.cfg, path), "round_7/11.html")

    def test_the_two_legacy_writers_keep_their_disk_shape(self):
        """`detail.save_raw_page` / `listing.save_raw_listing_page` 改走一处后表现不变。"""
        d = detail.save_raw_page(self.cfg, 7, "11", "<html>x</html>")
        self.assertEqual(d, self.cfg.raw_page_dir / "round_7" / "11.html")
        lst = listing.save_raw_listing_page(self.cfg, 7, "A01", "<html>y</html>")
        self.assertEqual(lst, self.cfg.raw_page_dir / "round_7" / "listing_A01.html")
