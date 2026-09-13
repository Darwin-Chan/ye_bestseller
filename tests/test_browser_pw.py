"""补采那条详情访问的页面节拍：等页面可读，而不是固定睡 2 秒（候选 04）。"""
import unittest
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw, detail

READY = '<script>{"skuInfoMap":{"A":{"skuId":1,"canBookCount":2}}}</script>'
LOADING = "<html><body>加载中</body></html>"


class ReadableTests(unittest.TestCase):
    """「可读」的判据只有这一处：有 SKU 行才算读到了。"""

    def test_readable_means_the_page_has_sku_rows(self):
        self.assertTrue(detail.readable(READY))
        self.assertFalse(detail.readable(LOADING))
        self.assertFalse(detail.readable(""), "空页不算读到")


class WaitUntilDetailReadableTests(unittest.TestCase):
    def page(self, url: str = "https://detail.1688.com/offer/11.html"):
        page = MagicMock()
        page.url = url
        return page

    def test_it_returns_as_soon_as_the_page_is_readable(self):
        page = self.page()
        page.content.side_effect = [LOADING, READY]

        with patch.object(browser_pw.time, "sleep"):
            html = browser_pw.wait_until_detail_readable(page)

        self.assertEqual(html, READY)
        self.assertEqual(page.content.call_count, 2, "第一遍没读到就再问一次")

    def test_it_gives_up_after_the_timeout_and_hands_back_the_html(self):
        """等不到就把当前 html 交出去：判失败是解析那一侧的事，这里只负责别干等。"""
        page = self.page()
        page.content.return_value = LOADING

        with patch.object(browser_pw.time, "sleep") as sleep:
            html = browser_pw.wait_until_detail_readable(page, timeout=0)

        self.assertEqual(html, LOADING)
        sleep.assert_not_called()

    def test_a_deny_page_returns_at_once(self):
        """deny 页不该在这儿干等：它是限流，要尽快交给 guard 记账（候选 04）。"""
        page = self.page("https://s.1688.com/bsop-punish?x=1")
        page.content.return_value = LOADING

        with patch.object(browser_pw.time, "sleep") as sleep:
            html = browser_pw.wait_until_detail_readable(page, timeout=0)

        self.assertEqual(html, LOADING)
        sleep.assert_not_called()
        self.assertEqual(page.content.call_count, 1)

    def test_a_punish_page_returns_at_once(self):
        page = self.page("https://x/punish?x5secdata=1")
        page.content.return_value = LOADING

        with patch.object(browser_pw.time, "sleep"):
            browser_pw.wait_until_detail_readable(page, timeout=0)

        self.assertEqual(page.content.call_count, 1)


if __name__ == "__main__":
    unittest.main()
