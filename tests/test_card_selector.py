"""商品卡片图片选择器的回归测试。

列表页店铺头部有一个 `<img class="hover-trigger">` 图标（48×48 的 tps-48-48.png，
渲染成 12×12，不在商品网格内），它排在所有商品图之前。选择器一旦把它算进来，
下标 0 就不是商品卡，点击它不会弹出详情页，每一页都会记一次点击失败。

这里用真实 CSS 引擎（lxml + cssselect，随 DrissionPage 一起安装）对 DOM 片段求值，
断言选择器只命中商品图——不需要浏览器。
"""
import unittest

from lxml import html as lxml_html

from bestseller_monitor.browser_pw import _PRODUCT_IMG_SEL

_PAGE = """
<html><body>
  <div class="shop-header">
    <div class="hover-trigger" id="pcMainCompanyNameV2"><span>义乌市中茂箱包有限公司</span></div>
    <img class="hover-trigger"
         src="https://img.alicdn.com/imgextra/i3/O1CN01JIBUrF22o5JIWtkAI_!!6000000007166-2-tps-48-48.png">
  </div>
  <div id="offerList">
    <a><img class="main-picture" src="https://cbu01.alicdn.com/img/ibank/a.310x310.jpg"></a>
    <a><img class="main-picture" src="https://cbu01.alicdn.com/img/ibank/b.310x310.jpg"></a>
  </div>
</body></html>
"""


class CardSelectorTests(unittest.TestCase):
    def test_selector_skips_shop_header_icon(self):
        picked = lxml_html.fromstring(_PAGE).cssselect(_PRODUCT_IMG_SEL)
        self.assertEqual(
            [el.get("src") for el in picked],
            ["https://cbu01.alicdn.com/img/ibank/a.310x310.jpg",
             "https://cbu01.alicdn.com/img/ibank/b.310x310.jpg"],
        )

    def test_index_zero_is_a_product_image(self):
        picked = lxml_html.fromstring(_PAGE).cssselect(_PRODUCT_IMG_SEL)
        self.assertEqual(picked[0].get("class"), "main-picture")


if __name__ == "__main__":
    unittest.main()
