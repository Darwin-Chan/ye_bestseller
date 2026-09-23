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

    def test_empty_sku_map_without_offer_signal_is_not_single_spec(self):
        # 空 skuInfoMap 但缺 isSkuOffer 标记：不按单规格处理，也不得用 price/amount 兜底。
        html = '<script>var x={"skuModel":{"skuInfoMap":[],"skuPriceScale":"0.02"},"price":"0.02","amount":1};</script>'
        rows = extract_skus_from_html(html)
        self.assertEqual(rows, [])

    def test_single_spec_offer_yields_one_default_sku_row(self):
        # 平台不使用 SKU 交易：整件商品按一条默认 SKU 行记录，库存取商品级可售量。
        html = (
            '<script>var x={"global":{"model":{'
            '"offerSign":{"isSkuOffer":false,"isPreSell":false},'
            '"skuModel":{"skuInfoMap":[],"skuPriceScale":"0.05"},'
            '"tradeModel":{"canBookedAmount":2119641,"canBookedAmountOriginal":999999,'
            '"priceDisplay":"0.05","originalPriceDisplay":"0.48"}}}};</script>'
        )
        rows = extract_skus_from_html(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku_id"], "default")
        self.assertEqual(rows[0]["sku_name"], "默认(单规格)")
        self.assertEqual(rows[0]["sku_stock"], 2119641)
        self.assertEqual(rows[0]["sku_price"], 0.05)

    def test_single_spec_requires_sku_trade_unsupported(self):
        # skuTradeSupported 作佐证：平台自述支持 SKU 交易却给不出明细，按页面结构变化处理。
        html = (
            '<script>var x={"offerSign":{"isSkuOffer":false},'
            '"skuModel":{"skuInfoMap":[]},'
            '"tradeModel":{"canBookedAmount":100,"skuTradeSupported":true,'
            '"priceDisplay":"0.02"}};</script>'
        )
        self.assertEqual(extract_skus_from_html(html), [])

    def test_single_spec_requires_explicit_empty_sku_map(self):
        # isSkuOffer=false 但 skuInfoMap 键完全缺失：属于页面结构变化，不能按单规格兜底。
        html = (
            '<script>var x={"offerSign":{"isSkuOffer":false},'
            '"tradeModel":{"canBookedAmount":100,"priceDisplay":"0.02"}};</script>'
        )
        self.assertEqual(extract_skus_from_html(html), [])

    def test_single_spec_price_falls_back_to_current_price_range(self):
        # 没有 priceDisplay 时退到当前区间价首档，口径与多规格取 discountPrice 一致。
        html = (
            '<script>var x={"offerSign":{"isSkuOffer":false},'
            '"skuModel":{"skuInfoMap":[]},'
            '"tradeModel":{"canBookedAmount":100,"offerPriceModel":'
            '{"currentPrices":[{"beginAmount":2,"price":"0.07"}]}}};</script>'
        )
        rows = extract_skus_from_html(html)
        self.assertEqual(rows[0]["sku_price"], 0.07)

    def test_page_without_single_spec_signal_is_not_single_spec(self):
        # 没有 isSkuOffer / skuInfoMap 标记的页面属于结构变化，不能被商品级字段兜底成成功。
        html = '<script>var x={"price":"0.02","quantity":5,"amount":1};</script>'
        self.assertEqual(extract_skus_from_html(html), [])

    def test_single_spec_without_bookable_amount_is_not_success(self):
        # 单规格但缺商品级可售量：按不完整库存观测处理，不写空库存的成功行。
        html = (
            '<script>var x={"offerSign":{"isSkuOffer":false},'
            '"skuModel":{"skuInfoMap":[]},'
            '"tradeModel":{"priceDisplay":"0.02"}};</script>'
        )
        self.assertEqual(extract_skus_from_html(html), [])

    def test_multi_sku_offer_is_unaffected(self):
        html = (
            '<script>var x={"offerSign":{"isSkuOffer":true},"skuModel":{"skuInfoMap":{'
            '"小号":{"skuId":111,"discountPrice":"0.18","canBookCount":648651,"specAttrs":"小号"},'
            '"大号":{"skuId":222,"discountPrice":"0.21","canBookCount":10,"specAttrs":"大号"}}}};</script>'
        )
        rows = extract_skus_from_html(html)
        self.assertEqual([r["sku_id"] for r in rows], ["111", "222"])
        self.assertEqual([r["sku_stock"] for r in rows], [648651, 10])


class SkuImageTests(unittest.TestCase):
    """SKU 图地址：从内嵌 JSON skuModel.skuProps 的 imageUrl 映射到每个 SKU（票 01）。

    片段取自实测页（offer 588733952962，`.scratch/probe-page.html` / `probe-result.json`）：
    名字里的 #C0WEK# 是平台内部码，两个「白色（袋装）」靠它区分；specAttrs 在 JSON 里
    是 `&gt;`，按 `>` 分段后逐段与规格值 name 精确匹配。
    """

    REAL_HTML = (
        '<script>var x={"skuModel":{"skuProps":['
        '{"fid":3216,"prop":"颜色","value":['
        '{"imageUrl":"https://cbu01.alicdn.com/img/ibank/O1CN0196OWJd1XFVLIsYcYj_!!948142894-0-cib.jpg","name":"粉色（袋装）#C1H5S#"},'
        '{"imageUrl":"https://cbu01.alicdn.com/img/ibank/O1CN013aupPr1XFVLJ0Fji7_!!948142894-0-cib.jpg","name":"白色（袋装）#C1JGM#"},'
        '{"imageUrl":"https://cbu01.alicdn.com/img/ibank/O1CN014oWNCW1XFVLItUChn_!!948142894-0-cib.jpg","name":"白色（袋装）#C0WEK#"},'
        '{"imageUrl":"https://cbu01.alicdn.com/img/ibank/O1CN01D3ZIeq1XFVLJIgee8_!!948142894-0-cib.jpg","name":"梅花形颜色随机#C15QP#"}]},'
        '{"fid":1234,"prop":"规格","value":[{"name":"/"}]}],'
        '"skuInfoMap":{'
        '"粉色（袋装）#C1H5S#&gt;/":{"skuId":1,"discountPrice":"0.13","canBookCount":100,'
        '"specAttrs":"粉色（袋装）#C1H5S#&gt;/"},'
        '"白色（袋装）#C0WEK#&gt;/":{"skuId":2,"discountPrice":"0.13","canBookCount":100,'
        '"specAttrs":"白色（袋装）#C0WEK#&gt;/"},'
        '"白色（袋装）#C1JGM#&gt;/":{"skuId":3,"discountPrice":"0.13","canBookCount":100,'
        '"specAttrs":"白色（袋装）#C1JGM#&gt;/"},'
        '"梅花形颜色随机#C15QP#&gt;/":{"skuId":4,"discountPrice":"0.13","canBookCount":100,'
        '"specAttrs":"梅花形颜色随机#C15QP#&gt;/"}}}};</script>'
    )

    def test_each_sku_gets_its_colour_image(self):
        rows = extract_skus_from_html(self.REAL_HTML)
        by_name = {r["sku_name"]: r["sku_image_url"] for r in rows}
        self.assertEqual(
            by_name["粉色（袋装）#C1H5S#&gt;/"],
            "https://cbu01.alicdn.com/img/ibank/O1CN0196OWJd1XFVLIsYcYj_!!948142894-0-cib.jpg",
        )
        # 同名色按 #code# 内部码各取各的图，不串。
        self.assertEqual(
            by_name["白色（袋装）#C0WEK#&gt;/"],
            "https://cbu01.alicdn.com/img/ibank/O1CN014oWNCW1XFVLItUChn_!!948142894-0-cib.jpg",
        )
        self.assertEqual(
            by_name["白色（袋装）#C1JGM#&gt;/"],
            "https://cbu01.alicdn.com/img/ibank/O1CN013aupPr1XFVLJ0Fji7_!!948142894-0-cib.jpg",
        )
        # 实测页 14/14 全带图（含「颜色随机」型）；`&gt;` 后的 "/" 段（规格值，无图）不干扰命中。
        self.assertEqual(
            by_name["梅花形颜色随机#C15QP#&gt;/"],
            "https://cbu01.alicdn.com/img/ibank/O1CN01D3ZIeq1XFVLJIgee8_!!948142894-0-cib.jpg",
        )

    MULTI_SPEC_HTML = (
        '<script>var x={"skuProps":['
        '{"prop":"颜色","value":[{"imageUrl":"https://img/red.png","name":"红色#A1#"},'
        '{"name":"无色#A2#"}]},'
        '{"prop":"规格","value":[{"imageUrl":"https://img/big.png","name":"大号#B1#"}]}],'
        '"skuInfoMap":{'
        '"红色#A1#&gt;大号#B1#":{"skuId":1,"discountPrice":"1","canBookCount":5,'
        '"specAttrs":"红色#A1#&gt;大号#B1#"},'
        '"无色#A2#&gt;大号#B1#":{"skuId":2,"discountPrice":"1","canBookCount":5,'
        '"specAttrs":"无色#A2#&gt;大号#B1#"}}};</script>'
    )

    def test_multi_segment_key_takes_the_first_hit_segment(self):
        """specAttrs 按 `>` 分段逐段匹配：命中段不一定在第一段。"""
        rows = extract_skus_from_html(self.MULTI_SPEC_HTML)
        by_name = {r["sku_name"]: r["sku_image_url"] for r in rows}
        self.assertEqual(by_name["红色#A1#&gt;大号#B1#"], "https://img/red.png")
        self.assertEqual(by_name["无色#A2#&gt;大号#B1#"], "https://img/big.png")

    def test_a_value_without_image_url_is_empty(self):
        """规格值没有 imageUrl（平台没配图）：空值，不算异常、也不许抛。"""
        html = (
            '<script>{"skuProps":[{"prop":"颜色","value":[{"name":"无色#A2#"}]}],'
            '"skuInfoMap":{"无色#A2#":{"skuId":1,"discountPrice":"1","canBookCount":5,'
            '"specAttrs":"无色#A2#"}}}</script>'
        )
        rows = extract_skus_from_html(html)
        self.assertEqual(rows[0]["sku_stock"], 5)
        self.assertIsNone(rows[0]["sku_image_url"])

    def test_page_without_sku_props_leaves_the_image_empty(self):
        html = ('<script>{"skuInfoMap":{"小号#C0615#":{"skuId":1,"discountPrice":"0.18",'
                '"canBookCount":648651,"specAttrs":"小号#C0615#"}}}</script>')
        rows = extract_skus_from_html(html)
        self.assertIsNone(rows[0]["sku_image_url"])

    def test_single_spec_offer_row_has_no_image(self):
        html = (
            '<script>var x={"global":{"model":{'
            '"offerSign":{"isSkuOffer":false,"isPreSell":false},'
            '"skuModel":{"skuInfoMap":[],"skuPriceScale":"0.05"},'
            '"tradeModel":{"canBookedAmount":2119641,"priceDisplay":"0.05"}}}};</script>'
        )
        rows = extract_skus_from_html(html)
        self.assertEqual(rows[0]["sku_id"], "default")
        self.assertIsNone(rows[0]["sku_image_url"])

    def test_text_fallback_rows_have_no_image(self):
        rows = extract_skus_from_html('<div>小号#C0615# ¥0.18 库存6486515个</div>')
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["sku_image_url"])

    def test_malformed_sku_props_do_not_break_inventory(self):
        """图是附加字段：畸形规格结构只跳过它，库存照常解析、其余映射照常生效。"""
        html = (
            '<script>{"skuProps":[{"name":"坏结构","value":123},"不是字典",'
            '{"prop":"颜色","value":[{"name":"红色#A1#","imageUrl":{"url":"x"}},'
            '{"name":"白色#A2#","imageUrl":"https://img/white.png"}]}],'
            '"skuInfoMap":{"白色#A2#":{"skuId":1,"discountPrice":"0.18",'
            '"canBookCount":648651,"specAttrs":"白色#A2#"}}}</script>'
        )
        rows = extract_skus_from_html(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku_stock"], 648651)
        self.assertEqual(rows[0]["sku_image_url"], "https://img/white.png")

    def test_broken_sku_props_json_does_not_break_inventory(self):
        html = (
            '<script>{"skuProps":[{"name":,'
            '"skuInfoMap":{"小号#C0615#":{"skuId":1,"discountPrice":"0.18",'
            '"canBookCount":648651,"specAttrs":"小号#C0615#"}}}</script>'
        )
        rows = extract_skus_from_html(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku_stock"], 648651)
        self.assertIsNone(rows[0]["sku_image_url"])


if __name__ == "__main__":
    unittest.main()
