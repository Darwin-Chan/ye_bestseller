"""详情页 parser 判据与导航 adapter。"""
import inspect
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
    def test_navigation_takes_no_config(self):
        """导航只做「到那一页」：等待、判据与读取都不吃配置（候选 02 同款死形参）。

        这条非用签名不可——少掉 `cfg` 之后，多传一个位置实参会被**静默**绑到 `emit` 上，
        不报错、事件却发不出去。
        """
        self.assertEqual(list(inspect.signature(browser_pw.navigate_detail).parameters),
                         ["page", "product_url", "emit"])

    def test_navigation_returns_the_opened_detail_without_reading_html(self):
        page = MagicMock()
        events = []

        opened = browser_pw.navigate_detail(
            page, "https://detail.1688.com/offer/11.html",
            emit=lambda event, **kw: events.append((event, kw)),
        )

        self.assertIsInstance(opened, detail_visit.OpenedDetail)
        self.assertIs(opened.page, page)
        page.content.assert_not_called()
        self.assertEqual([event for event, _ in events], ["detail_nav"])


class CompatSurfaceTests(unittest.TestCase):
    """`browser_pw` 对外转出的旧名：只留诊断工具真在用的那几个。

    这条边界两侧一起钉——留哪些、删哪些都是决定，光钉「少了几个名字」说不清。
    依据是 [ADR-0012](../docs/adr/0012-listing-module-and-single-page-walk.md) 那条模式
    （搬到别处的私有名在这里「只留工具需要的兼容别名」）与 `browser_pw.py` 里那句「别删」。
    """

    # 诊断工具从 browser_pw 取的：四个旧私有名 + 一个 guard 的公开名
    TOOL_NAMES = ("_body_text", "_captcha_visible", "_is_punish_url", "_resolved",
                  "intervention_kind")
    # 没有消费者的那些：留着只会让人以为还有人在用
    DEAD_NAMES = ("DenyTracker", "RoundDenyExceeded", "ShopDenyExceeded",
                  "_is_deny_url", "_vtype")

    def test_only_the_names_tools_use_survive(self):
        for name in self.TOOL_NAMES:
            self.assertTrue(hasattr(browser_pw, name), f"{name} 还有诊断工具在用")
        for name in self.DEAD_NAMES:
            self.assertFalse(hasattr(browser_pw, name), f"{name} 已经没有消费者")


if __name__ == "__main__":
    unittest.main()
