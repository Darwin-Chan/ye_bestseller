import unittest

from bestseller_monitor.parse import (
    extract_offer_links,
    extract_sku_rows,
    extract_skus_from_html,
    extract_title,
    parse_price,
    parse_stock,
)


class ParseTests(unittest.TestCase):
    def test_offer_links_dedupe_and_order(self):
        html = """
        <a href="https://detail.1688.com/offer/111.html">A</a>
        <a href="https://detail.1688.com/offer/222.html">B</a>
        <a href="https://detail.1688.com/offer/111.html">A-repeat</a>
        """
        self.assertEqual(
            extract_offer_links(html),
            [("111", "https://detail.1688.com/offer/111.html"),
             ("222", "https://detail.1688.com/offer/222.html")],
        )

    def test_stock_and_price(self):
        self.assertEqual(parse_stock("6486515个"), 6486515)
        self.assertEqual(parse_stock("64.8万"), 648000)
        self.assertEqual(parse_stock("1,234"), 1234)
        self.assertEqual(parse_stock(0), 0)
        self.assertEqual(parse_stock(0.0), 0)
        self.assertEqual(parse_stock("0"), 0)
        self.assertIsNone(parse_stock("暂无"))
        self.assertIsNone(parse_stock(-1))
        self.assertIsNone(parse_stock(False))
        self.assertEqual(parse_price("¥0.18"), 0.18)
        self.assertEqual(parse_price(1.25), 1.25)
        self.assertEqual(parse_price(0), 0.0)
        self.assertIsNone(parse_price(-1))
        self.assertIsNone(parse_price(False))

    def test_title_and_sku_rows(self):
        html = """
        <html><head>
        <meta property="og:title" content="批发针线盒套装10件套" />
        <title>批发针线盒套装 - 1688</title>
        </head><body>
          <div>老太婆针线盒#C19J5# ¥0.08 库存278976个</div>
          <div>小号#C0615# ¥0.18 库存6486515个</div>
          <div>中号#C0/DM# ¥0.21 库存6470915个</div>
        </body></html>
        """
        self.assertEqual(extract_title(html), "批发针线盒套装10件套")
        rows = extract_sku_rows(html)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["sku_name"], "老太婆针线盒#C19J5#")
        self.assertEqual(rows[0]["sku_price"], 0.08)
        self.assertEqual(rows[0]["sku_stock"], 278976)

    def test_sku_map_json(self):
        html = (
            '<script>var x={"skuInfoMap":{"小号#C0615#":{"skuId":111,"discountPrice":"0.18",'
            '"canBookCount":648651,"specAttrs":"小号#C0615#"}}};</script>'
        )
        rows = extract_skus_from_html(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku_name"], "小号#C0615#")
        self.assertEqual(rows[0]["sku_price"], 0.18)
        self.assertEqual(rows[0]["sku_stock"], 648651)
        self.assertEqual(rows[0]["sku_id"], "111")

    def test_sku_map_preserves_numeric_zero_stock_and_price(self):
        html = (
            '<script>{"skuInfoMap":{"小号":{"skuId":111,"discountPrice":0,'
            '"canBookCount":0,"specAttrs":"小号"}}}</script>'
        )
        rows = extract_skus_from_html(html)
        self.assertEqual(rows[0]["sku_price"], 0.0)
        self.assertEqual(rows[0]["sku_stock"], 0)

    def test_single_spec_default_sku(self):
        # skuInfoMap 为空数组且仅有价格：没有明确库存时不得作为成功 SKU 返回。
        html = '<script>var x={"skuModel":{"skuInfoMap":[],"skuPriceScale":"0.02"},"price":"0.02","amount":1};</script>'
        rows = extract_skus_from_html(html)
        self.assertEqual(rows, [])  # amount 不做库存，避免误判


if __name__ == "__main__":
    unittest.main()
