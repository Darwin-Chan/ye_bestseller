"""Ticket 05: real history and decisions at the analysis service seam."""
import unittest
from unittest.mock import patch
import test_sku_codes as code_fixtures
from helpers import product_picture


class SkuMatchingTests(unittest.TestCase):
    observe = code_fixtures.MerchantCodeTests.observe
    member = staticmethod(code_fixtures.MerchantCodeTests.member)
    group_for = classmethod(code_fixtures.MerchantCodeTests.group_for.__func__)
    enable_model = code_fixtures.MerchantCodeTests.enable_model
    seed = code_fixtures.MerchantCodeTests.seed

    def setUp(self):
        code_fixtures.MerchantCodeTests.setUp(self)
        self.enable_model()
        self.calls = []
        self.result = {"same": True, "confidence": .95, "reason": "主体形状与构造一致，仅颜色和包装不同"}
        def respond(config, content, instruction, **kwargs):
            if kwargs.get("kind") == "verify":
                import base64, io
                from PIL import Image
                image = Image.open(io.BytesIO(base64.b64decode(content[0]["image_url"]["url"].split(",", 1)[1])))
                colors = {(255, 0, 0): "red", (0, 255, 0): "green", (0, 0, 255): "blue"}
                return {"colors": [colors[image.getpixel((i * 50 + 25, 25))] for i in range(5)]}
            self.calls.append((content, instruction))
            return self.result(content) if callable(self.result) else self.result
        boundary = patch("bestseller_monitor.matching.request_json", side_effect=respond)
        boundary.start(); self.addCleanup(boundary.stop)

    def test_unique_image_target_adds_to_confirmed_group_and_reuses_judgment(self):
        a = self.seed("红 #A#", image=product_picture("red"))
        first = self.service.start("2026-09-07", "2026-09-14")
        gid = self.group_for(first, a)["id"]
        self.service.confirm_groups(first["id"], [gid])
        self.service.save_draft(first["id"])
        b = self.seed("蓝双件包装", shop="B01", image=product_picture("blue"))
        result = self.service.start("2026-09-07", "2026-09-14")
        group = self.group_for(result, b)
        self.assertEqual(gid, group["id"])
        self.assertFalse(group["confirmed"])
        self.assertEqual([b], group["added_members"])
        self.assertEqual(20, group["sales"])
        self.assertEqual(1, len(self.calls))
        self.assertIn("图文", group["grouping_basis"])
        self.assertIn("主体", result["members"][1]["matching_note"])
        again = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(1, len(self.calls))
        self.assertEqual(gid, self.group_for(again, b)["id"])
        self.service.confirm_groups(again["id"], [gid])
        self.service.save_draft(again["id"])
        from bestseller_monitor.analysis import AnalysisService
        restored = AnalysisService(self.service.config, running=lambda: False).get(again["id"])
        self.assertTrue(self.group_for(restored, b)["confirmed"])

    def test_separate_skus_match_distinct_external_members_using_selected_history(self):
        from bestseller_monitor.db import SKU_IMAGE_OWN
        import json
        main = product_picture("green")
        for day, stock in [("2026-09-07", 100), ("2026-09-14", 90)]:
            self.observe(day, [dict(sku_id="cup", sku_name="杯子红", sku_stock=stock,
                                   sku_image_evidence=dict(product_picture("red"), source=SKU_IMAGE_OWN)),
                               dict(sku_id="plate", sku_name="盘子蓝", sku_stock=stock,
                                   sku_image_evidence=dict(product_picture("blue"), source=SKU_IMAGE_OWN))], image=main, title="杯子盘子历史")
        self.seed("#CUP#", shop="B01", title="杯子历史", image=product_picture("red"))
        self.seed("#PLATE#", shop="C01", title="盘子历史", image=product_picture("blue"))
        self.observe("2026-09-21", [dict(sku_id="cup", sku_name="未来错误证据", sku_stock=1)], title="未来标题", image=main)
        def verdict(content):
            texts = [json.loads(c["text"]) for c in content if c["type"] == "text"]
            same = ("杯子" in texts[0]["product_name"] and "杯子" in texts[1]["product_name"] and
                    all("盘子蓝" != t["sku_name"] for t in texts)) or ("盘子" in texts[0]["product_name"] and "盘子" in texts[1]["product_name"] and
                    all("杯子红" != t["sku_name"] for t in texts))
            return dict(same=same, confidence=.95, reason="图文主体核对")
        self.result = verdict
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(2, len(result["groups"]))
        self.assertEqual(self.group_for(result, self.member("A01", "11", "cup"))["id"], self.group_for(result, self.member("B01", "11", "s"))["id"])
        self.assertEqual(self.group_for(result, self.member("A01", "11", "plate"))["id"], self.group_for(result, self.member("C01", "11", "s"))["id"])
        for content, instruction in self.calls:
            self.assertIn("sales piece count", instruction)
            self.assertNotIn("未来", str(content))
            texts = [json.loads(c["text"]) for c in content if c["type"] == "text"]
            self.assertTrue(all(t["product_name"] for t in texts))
            for text, image in zip(texts, [c["image_url"]["url"] for c in content if c["type"] == "image_url"]):
                if text["sku_name"] in ("杯子红", "盘子蓝"):
                    import base64
                    expected = product_picture("red" if text["sku_name"] == "杯子红" else "blue")
                    self.assertEqual(expected["content"], base64.b64decode(image.split(",", 1)[1]))
                else:
                    self.assertEqual("", text["sku_name"])

    def test_manual_exclusion_saved_while_model_is_in_flight_wins_at_application(self):
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        self.result = dict(same=False, confidence=.95, reason="不同主体")
        first = self.service.start("2026-09-07", "2026-09-14")
        self.service.save_draft(first["id"])
        # Changed evidence forces a real new external judgment; human saves an exclusion during it.
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="无码", sku_stock=90)], shop="B01", image=product_picture("green"))
        def response(content):
            excluded = self.service.edit_group(first["id"], "exclude", self.group_for(first, b)["id"], b)
            self.service.save_draft(excluded["id"])
            return dict(same=True, confidence=.95, reason="主体一致")
        self.result = response
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertNotEqual(self.group_for(result, a)["id"], self.group_for(result, b)["id"])
        self.assertTrue(self.group_for(result, b)["excluded_group"])

    def test_two_unencoded_skus_have_one_legal_membership_not_reciprocal_moves(self):
        a = self.seed("红", image=product_picture("red"))
        b = self.seed("蓝双件", shop="B01", image=product_picture("blue"))
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(1, len(result["groups"]))
        self.assertEqual(2, result["groups"][0]["sku_count"])
        self.assertEqual(20, result["groups"][0]["sales"])
        self.assertEqual({tuple(a.values()), tuple(b.values())}, {tuple(ref.values()) for ref in result["groups"][0]["members"]})

    def test_ambiguous_codes_use_images_but_different_valid_codes_never_call(self):
        self.seed("#A#", image=product_picture("red"))
        self.seed("#B#", shop="B01", image=product_picture("blue"))
        separated = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(0, len(self.calls))
        self.assertEqual(2, len(separated["groups"]))
        self.seed("#A# #B#", shop="C01", image=product_picture("green"))
        result = self.service.start("2026-09-07", "2026-09-14")
        ambiguous = next(m for m in result["members"] if m["shop_key"] == "C01")
        self.assertIsNone(ambiguous["merchant_code"])
        self.assertTrue(ambiguous["code_ambiguous"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual("pending_candidates", ambiguous["matching_state"])
        self.assertEqual(2, len(ambiguous["candidate_groups"]))
        self.assertEqual(3, len(result["groups"]))
        self.assertEqual(30, sum(g["sales"] for g in result["groups"]))
        self.service.save_draft(result["id"])
        from bestseller_monitor.analysis import AnalysisService
        restored = AnalysisService(self.service.config, running=lambda: False).get(result["id"])
        self.assertEqual(ambiguous["candidate_groups"], restored["members"][2]["candidate_groups"])

    def test_missing_same_observation_image_never_borrows_or_clears_human_choice(self):
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="无码", sku_stock=90)], shop="B01")
        self.observe("2026-09-21", [dict(sku_id="s", sku_name="未来", sku_stock=80)], shop="B01", image=product_picture("green"))
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(0, len(self.calls))
        self.assertEqual(2, len(result["groups"]))
        missing = result["members"][1]
        self.assertIsNone(missing["image_data"])
        self.assertIn("缺少图片证据", missing["matching_note"])
        result = self.service.edit_group(result["id"], "move", self.group_for(result, b)["id"], b, self.group_for(result, a)["id"])
        self.service.confirm_groups(result["id"], [self.group_for(result, b)["id"]])
        self.service.save_draft(result["id"])
        again = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(1, len(again["groups"]))
        self.assertTrue(again["groups"][0]["confirmed"])
        self.assertEqual(0, len(self.calls))

    def test_identity_exclusion_blocks_images_even_after_new_evidence(self):
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        result = self.service.start("2026-09-07", "2026-09-14")
        result = self.service.edit_group(result["id"], "exclude", self.group_for(result, b)["id"], b)
        self.service.save_draft(result["id"])
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="改名", sku_stock=90)], shop="B01", image=product_picture("green"))
        self.calls.clear()
        again = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(0, len(self.calls))
        self.assertTrue(self.group_for(again, b)["excluded_group"])
        self.assertNotEqual(self.group_for(again, a)["id"], self.group_for(again, b)["id"])

    def test_definite_negative_reuses_but_changed_sku_evidence_does_not(self):
        self.seed("#A#", image=product_picture("red"))
        self.seed("无码", shop="B01", image=product_picture("blue"))
        self.result = dict(same=False, confidence=.95, reason="主体构造不同")
        first = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(2, len(first["groups"]))
        again = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(1, len(self.calls))
        self.assertEqual(2, len(again["groups"]))
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="新规格", sku_stock=90)], shop="B01", image=product_picture("green"))
        self.result = dict(same=True, confidence=.95, reason="主体一致")
        changed = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(2, len(self.calls))
        self.assertEqual(1, len(changed["groups"]))

    def test_failure_uncertainty_and_invalid_results_are_not_cached_as_negative(self):
        from bestseller_monitor.matching import ModelFailure
        self.seed("#A#", image=product_picture("red"))
        self.seed("无码", shop="B01", image=product_picture("blue"))
        for response in [dict(same=False, confidence=.2), dict(same="no", confidence=.95), ModelFailure("受控请求失败")]:
            def outcome(content):
                if isinstance(response, Exception):
                    raise response
                return response
            self.result = outcome
            incomplete = self.service.start("2026-09-07", "2026-09-14")
            self.assertEqual(2, len(incomplete["groups"]))
            self.assertIn("pending", incomplete["members"][1]["matching_state"])
        self.result = dict(same=True, confidence=.95, reason="重分析有有效证据")
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(4, len(self.calls))
        self.assertEqual(1, len(result["groups"]))

    def test_saved_image_basis_survives_confirmed_normal_reanalysis(self):
        self.seed("#A#", image=product_picture("red"))
        self.seed("无码", shop="B01", image=product_picture("blue"))
        result = self.service.start("2026-09-07", "2026-09-14")
        self.service.confirm_groups(result["id"], [result["groups"][0]["id"]])
        self.service.save_draft(result["id"])
        again = self.service.start("2026-09-07", "2026-09-14")
        self.assertTrue(again["groups"][0]["confirmed"])
        self.assertEqual(1, len(self.calls))
        self.assertTrue(again["members"][1].get("image_judgments"))

    def test_multiple_distinct_new_members_join_unique_coded_target(self):
        self.seed("#A#", image=product_picture("red"))
        self.seed("蓝", shop="B01", image=product_picture("blue"))
        self.seed("绿", shop="C01", image=product_picture("green"))
        import json
        def verdict(content):
            images = [c["image_url"]["url"] for c in content if c["type"] == "image_url"]
            import base64
            same = any(base64.b64decode(image.split(",", 1)[1]) == product_picture("red")["content"] for image in images)
            return dict(same=same, confidence=.95 if same else .2, reason="与编码锚点主体相同")
        self.result = verdict
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(1, len(result["groups"]))
        self.assertEqual(30, result["groups"][0]["sales"])

    def test_model_signature_rule_and_legacy_product_cache_are_isolated_from_sku(self):
        from dataclasses import replace
        from bestseller_monitor.analysis import AnalysisService
        from bestseller_monitor.matching import MatchingService
        self.seed("#A#", image=product_picture("red"))
        self.seed("无编码", shop="B01", image=product_picture("blue"))
        first = self.service.start("2026-09-07", "2026-09-14")
        products = [dict(m, image_hash=m["image_hash"], origin="history", information_complete=True) for m in first["members"]]
        groups = [dict(id="p"+str(i), members=[dict(shop_key=m["shop_key"], offer_id=m["offer_id"])], confirmed=False) for i, m in enumerate(products)]
        # Populate the old product ledger through its public matching boundary.
        MatchingService(self.service.config.matching).suggest(products, groups)
        before = len(self.calls)
        changed = replace(self.service.config.matching, model=replace(self.service.config.matching.model, model="sku-another-model"))
        self.service = AnalysisService(replace(self.service.config, matching=changed), running=lambda: False)
        self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(before + 1, len(self.calls))
        with patch("bestseller_monitor.sku_matching.RULE_VERSION", "sku-next-rule"):
            self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(before + 2, len(self.calls))
        import datetime
        import sqlite3
        from contextlib import closing
        from bestseller_monitor.judgment_set import build_package
        target = self.path.with_name("old-package.db")
        build_package(changed.cache, target, machine_id="test", generated_at=datetime.datetime.now(datetime.timezone.utc), store=self.service.store.path)
        with closing(sqlite3.connect(target)) as conn:
            self.assertNotIn("sku_judgments", {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
            for result, in conn.execute("SELECT result FROM judgments"):
                self.assertNotIn("sku-physical", result)


class SkuMatchingBrowserTests(unittest.TestCase):
    observe = SkuMatchingTests.observe
    member = staticmethod(SkuMatchingTests.member)
    group_for = classmethod(SkuMatchingTests.group_for.__func__)
    enable_model = SkuMatchingTests.enable_model
    seed = SkuMatchingTests.seed

    def setUp(self):
        from playwright.sync_api import sync_playwright
        from test_analysis import serve
        SkuMatchingTests.setUp(self)
        self.server, self.thread = serve(self.service)
        def close():
            self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.addCleanup(close)
        pw = sync_playwright().start(); self.addCleanup(pw.stop)
        self.browser = pw.chromium.launch(channel="msedge", headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}")

    def begin(self):
        self.page.locator("#start").fill("2026-09-07")
        self.page.locator("#end").fill("2026-09-14")
        self.page.locator("#begin").click()

    def retain(self, name):
        import os
        from pathlib import Path
        target = os.environ.get("SKU05_ARTIFACTS")
        if target:
            path = Path(target); path.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path / (name + ".png")), full_page=True)
            self.page.locator(".sku-evidence-card").last.scroll_into_view_if_needed()
            self.page.screenshot(path=str(path / (name + "-evidence.png")), full_page=True)

    def test_real_analysis_addition_reconfirmation_save_and_reload(self):
        from playwright.sync_api import expect
        from bestseller_monitor.db import SKU_IMAGE_OWN
        for day, stock in [("2026-09-07", 100), ("2026-09-14", 90)]:
            self.observe(day, [dict(sku_id="coded", sku_name="红 #A#", sku_stock=stock,
                                   sku_image_evidence=dict(product_picture("red"), source=SKU_IMAGE_OWN))], image=product_picture("green"))
        self.begin()
        expect(self.page.locator(".group-status")).to_have_text("待确认")
        self.page.get_by_role("button", name="确认当前分组", exact=True).click()
        self.page.locator("#saveDraft").click()
        expect(self.page.locator("#saveStatus")).to_contain_text("已保存")
        self.seed("蓝双件包装", shop="B01", image=product_picture("blue"))
        self.page.locator("#back").click()
        self.begin()
        expect(self.page.locator("#products [data-sku-group]")).to_have_count(1)
        expect(self.page.locator(".group-status")).to_have_text("待确认")
        card = self.page.locator('[data-sku="s"]')
        expect(card).to_contain_text("图文依据")
        expect(card).to_contain_text("主体形状与构造一致")
        expect(card).to_contain_text("同次商品主图代填")
        expect(card).to_contain_text("新增 SKU")
        expect(self.page.locator('[data-sku="coded"]')).to_contain_text("SKU 专图")
        expect(self.page.locator("#groupDetail")).to_contain_text("整个组需要重新确认")
        expect(card).not_to_contain_text("95%")
        self.retain("image-addition-pending")
        self.page.get_by_role("button", name="确认当前分组", exact=True).click()
        self.page.locator("#saveDraft").click()
        expect(self.page.locator("#saveStatus")).to_contain_text("已保存")
        self.page.reload()
        expect(self.page.locator("#results")).to_be_visible()
        self.page.locator("#backToReview").click()
        expect(self.page.locator(".group-status")).to_have_text("已确认")
        expect(self.page.locator('[data-sku="s"]')).to_contain_text("图文依据")
        self.retain("image-addition-confirmed-restored")
        self.assertEqual(1, len(self.calls))

    def test_real_missing_image_and_multiple_candidates_are_visible(self):
        from playwright.sync_api import expect
        self.seed("#A#", image=product_picture("red"))
        self.seed("#B#", shop="B01", image=product_picture("blue"))
        self.seed("无编码候选", shop="C01", image=product_picture("green"))
        self.seed("缺图成员", shop="D01")
        self.begin()
        expect(self.page.locator("#products [data-sku-group]")).to_have_count(4)
        self.page.locator("#skuQuery").fill("C01")
        expect(self.page.locator("#groupDetail")).to_contain_text("多个图文候选")
        expect(self.page.locator("#groupDetail")).to_contain_text("未选择归属")
        self.retain("multiple-image-candidates")
        self.page.locator("#skuQuery").fill("D01")
        expect(self.page.locator("#groupDetail")).to_contain_text("缺少图片证据")
        expect(self.page.locator("#groupDetail")).to_contain_text("当次缺图")
        expect(self.page.locator("#groupDetail")).to_contain_text("暂时独立")
        self.retain("missing-image-pending")
