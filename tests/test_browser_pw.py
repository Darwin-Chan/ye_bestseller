"""详情页 parser 判据与导航 adapter。"""
import unittest
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_pw, detail, detail_visit

READY = '<script>{"skuInfoMap":{"A":{"skuId":1,"canBookCount":2}}}</script>'
LOADING = "<html><body>加载中</body></html>"


class ReadableTests(unittest.TestCase):
    """「可读」的判据只有这一处：有 SKU 行才算读到了。"""

    def test_readable_means_the_page_has_sku_rows(self):
        self.assertTrue(detail.readable(READY))
        self.assertFalse(detail.readable(LOADING))
        self.assertFalse(detail.readable(""), "空页不算读到")


class NavigateDetailTests(unittest.TestCase):
    def test_navigation_returns_the_opened_detail_without_reading_html(self):
        page = MagicMock()
        events = []

        opened = browser_pw.navigate_detail(
            page, "https://detail.1688.com/offer/11.html", MagicMock(),
            emit=lambda event, **kw: events.append((event, kw)),
        )

        self.assertIsInstance(opened, detail_visit.OpenedDetail)
        self.assertIs(opened.page, page)
        page.content.assert_not_called()
        self.assertEqual([event for event, _ in events], ["detail_nav"])


if __name__ == "__main__":
    unittest.main()
