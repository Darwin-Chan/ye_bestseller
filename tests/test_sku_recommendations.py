"""Ticket 06: real history and decisions; control only external model requests."""
import json
import unittest
from unittest.mock import patch
import test_sku_matching as fixtures
from helpers import product_picture
from bestseller_monitor.matching import request_json as real_request_json


class SkuRecommendationTests(unittest.TestCase):
    observe = fixtures.SkuMatchingTests.observe
    member = staticmethod(fixtures.SkuMatchingTests.member)
    group_for = classmethod(fixtures.SkuMatchingTests.group_for.__func__)
    enable_model = fixtures.SkuMatchingTests.enable_model
    seed = fixtures.SkuMatchingTests.seed

    def setUp(self):
        fixtures.SkuMatchingTests.setUp(self)
        self.recommend_calls = []
        self.recommend_result = lambda context: dict(group_id=context["candidates"][0]["id"], reason="杯口与底座证据更完整")
        from bestseller_monitor.matching import request_json
        pair_boundary = request_json
        def respond(config, content, instruction, **kwargs):
            if kwargs.get("kind") != "recommend":
                return pair_boundary(config, content, instruction, **kwargs)
            context = json.loads(content[0]["text"])
            self.recommend_calls.append((context, content, instruction))
            return self.recommend_result(context) if callable(self.recommend_result) else self.recommend_result
        boundary = patch("bestseller_monitor.matching.request_json", side_effect=respond)
        boundary.start(); self.addCleanup(boundary.stop)

    def anchors(self):
        a = self.seed("#A#", image=product_picture("red"))
        c = self.seed("#C#", shop="C01", image=product_picture("blue"))
        first = self.analyze()
        self.service.confirm_groups(first["id"], [g["id"] for g in first["groups"]])
        self.service.save_draft(first["id"])
        b = self.seed("蓝双件包装", shop="B01", image=product_picture("green"))
        return a, b, c, first

    def analyze(self):
        return self.service.start("2026-09-07", "2026-09-14")

    def evidence(self, snapshot, ref):
        return next(m for m in snapshot["members"] if all(m[k] == v for k, v in ref.items()))

    def test_unique_recommendation_keeps_alternatives_and_unconfirms_only_target(self):
        a, b, c, first = self.anchors()
        target = self.group_for(first, a)["id"]
        self.recommend_result = dict(group_id=target, reason="杯口与底座证据更完整")
        result = self.analyze()
        self.assertEqual(target, self.group_for(result, b)["id"])
        self.assertNotEqual(target, self.group_for(result, c)["id"])
        self.assertFalse(self.group_for(result, b)["confirmed"])
        self.assertTrue(self.group_for(result, c)["confirmed"])
        member = self.evidence(result, b)
        self.assertEqual("recommended", member["matching_state"])
        self.assertEqual(2, len(member["candidate_groups"]))
        self.assertEqual("杯口与底座证据更完整", member["recommendation"]["reason"])
        self.assertEqual([b], self.group_for(result, b)["added_members"])
        self.assertEqual(30, sum(g["sales"] for g in result["groups"]))
        context, content, instruction = self.recommend_calls[0]
        self.assertEqual("B01", context["member"]["shop_key"])
        self.assertEqual("", context["member"]["matching_input"]["sku_name"])
        self.assertEqual({"A01", "C01"}, {g["members"][0]["shop_key"] for g in context["candidates"]})
        self.assertEqual(3, len([p for p in content if p["type"] == "image_url"]))
        self.assertTrue(all(g["members"][0]["image_source"] for g in context["candidates"]))
        self.assertIn("sales piece count", instruction)

    def test_uncertain_is_successful_reusable_and_survives_saved_draft(self):
        a, b, c, _ = self.anchors()
        self.recommend_result = dict(group_id=None, reason="两组图文证据均不足以唯一归属")
        first = self.analyze()
        self.assertEqual(1, self.group_for(first, b)["sku_count"])
        m = self.evidence(first, b)
        self.assertEqual("pending_candidates", m["matching_state"])
        self.assertIn("多个候选，无法确定归属", m["matching_note"])
        self.assertIsNone(m["recommendation"]["group_id"])
        self.service.save_draft(first["id"])
        from bestseller_monitor.analysis import AnalysisService
        restored = AnalysisService(self.service.config, running=lambda: False).get(first["id"])
        self.assertEqual(m["recommendation"], self.evidence(restored, b)["recommendation"])
        again = self.analyze()
        self.assertEqual(1, len(self.recommend_calls))
        self.assertEqual(2, len(self.calls))
        self.assertTrue(self.evidence(again, b)["recommendation"]["cached"])

    def test_unsaved_automatic_candidates_have_stable_local_cache_context(self):
        self.seed("#A#", image=product_picture("red"))
        self.seed("#C#", shop="C01", image=product_picture("blue"))
        b = self.seed("无编码", shop="B01", image=product_picture("green"))
        first = self.analyze()
        again = self.analyze()
        self.assertEqual(1, len(self.recommend_calls))
        self.assertTrue(self.evidence(again, b)["recommendation"]["cached"])
        self.assertEqual(self.group_for(first, b)["id"], self.group_for(again, b)["id"])

    def test_confirm_save_restore_and_reanalysis_preserve_recommendation_audit(self):
        a, b, c, _ = self.anchors()
        result = self.analyze()
        before = self.evidence(result, b)
        candidates = before["candidate_groups"]
        self.service.confirm_groups(result["id"], [self.group_for(result, b)["id"]])
        self.service.save_draft(result["id"])
        from bestseller_monitor.analysis import AnalysisService
        restored = AnalysisService(self.service.config, running=lambda: False).get(result["id"])
        self.assertEqual(candidates, self.evidence(restored, b)["candidate_groups"])
        again = self.analyze()
        m = self.evidence(again, b)
        self.assertEqual(before["recommendation"]["reason"], m["recommendation"]["reason"])
        self.assertEqual(candidates, m["candidate_groups"])
        self.assertEqual(self.group_for(result, b)["id"], self.group_for(again, b)["id"])
        self.assertEqual(1, len(self.recommend_calls))

    def split_anchors(self, *, mixed=False, missing=False):
        a = self.seed("#A#", image=product_picture("red"))
        c = self.seed("#A#", shop="C01", image=None if missing else product_picture("blue"))
        result = self.analyze()
        result = self.service.edit_group(result["id"], "remove", self.group_for(result, c)["id"], c)
        if mixed:
            d = self.seed("#D#", shop="D01", image=product_picture("purple"))
            self.service.save_draft(result["id"])
            result = self.analyze()
            result = self.service.edit_group(result["id"], "move", self.group_for(result, d)["id"], d, self.group_for(result, a)["id"])
        self.service.confirm_groups(result["id"], [g["id"] for g in result["groups"]])
        self.service.save_draft(result["id"])
        b = self.seed("#A#", shop="B01", image=product_picture("green"))
        return a, b, c, result

    def test_same_code_human_split_uses_comparison_without_merging_anchors(self):
        a, b, c, first = self.split_anchors()
        target = self.group_for(first, c)["id"]
        self.recommend_result = dict(group_id=target, reason="该人工组的杯底证据更接近")
        result = self.analyze()
        self.assertEqual(target, self.group_for(result, b)["id"])
        self.assertNotEqual(self.group_for(result, a)["id"], target)
        self.assertEqual(0, len(self.calls))
        self.assertEqual(1, len(self.recommend_calls))
        self.assertFalse(self.group_for(result, b)["confirmed"])
        self.assertEqual(2, len(self.evidence(result, b)["candidate_groups"]))

    def test_mixed_code_exception_is_local_to_candidate_group(self):
        a, b, c, first = self.split_anchors(mixed=True)
        target = self.group_for(first, a)["id"]
        self.recommend_result = dict(group_id=target, reason="局部混合组的图文证据支持")
        result = self.analyze()
        self.assertEqual(target, self.group_for(result, b)["id"])
        self.assertNotEqual(target, self.group_for(result, c)["id"])
        x = self.seed("#D#", shop="E01", image=product_picture("yellow"))
        y = self.seed("#A#", shop="F01", image=product_picture("orange"))
        # No saved human mixed group in this independent service: codes remain unrelated.
        from dataclasses import replace
        from bestseller_monitor.analysis import AnalysisService
        other = AnalysisService(replace(self.service.config, store=self.path.with_name("separate.sqlite")), running=lambda: False)
        independent = other.start("2026-09-07", "2026-09-14")
        self.assertNotEqual(self.group_for(independent, x)["id"], self.group_for(independent, y)["id"])

    def test_missing_candidate_picture_does_not_make_name_only_recommendation(self):
        a, b, c, first = self.split_anchors(missing=True)
        result = self.analyze()
        self.assertEqual(0, len(self.recommend_calls))
        self.assertEqual(1, self.group_for(result, b)["sku_count"])
        self.assertIn("缺少图片证据", self.evidence(result, b)["matching_note"])
        self.assertEqual(self.group_for(first, a)["id"], self.group_for(result, a)["id"])

    def test_fresh_recommendation_cannot_overwrite_inflight_human_membership(self):
        a, b, c, first = self.anchors()
        self.recommend_result = dict(group_id=None, reason="无法确定")
        saved = self.analyze()
        self.service.save_draft(saved["id"])
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="新证据", sku_stock=90)], shop="B01", image=product_picture("purple"))
        target = self.group_for(saved, a)["id"]
        def choose(context):
            edited = self.service.edit_group(saved["id"], "move", self.group_for(saved, b)["id"], b, self.group_for(saved, c)["id"])
            self.service.save_draft(edited["id"])
            return dict(group_id=target, reason="不应覆盖人工")
        self.recommend_result = choose
        result = self.analyze()
        self.assertEqual(self.group_for(saved, c)["id"], self.group_for(result, b)["id"])
        self.assertEqual("manual", self.evidence(result, b)["matching_state"])

    def test_cache_invalidates_when_candidate_member_set_changes(self):
        a, b, c, _ = self.anchors()
        first = self.analyze()
        self.assertEqual(1, len(self.recommend_calls))
        new = self.seed("#A#", shop="D01", image=product_picture("purple"))
        again = self.analyze()
        self.assertEqual(2, len(self.recommend_calls))
        self.assertEqual(self.group_for(again, a)["id"], self.group_for(again, new)["id"])
        self.assertTrue(any(len(g["members"]) == 2 for g in self.recommend_calls[-1][0]["candidates"]))

    def test_cache_invalidates_candidate_evidence_and_image_source(self):
        from bestseller_monitor.db import SKU_IMAGE_OWN
        a, b, c, _ = self.anchors()
        self.analyze()
        self.observe("2026-09-07", [dict(sku_id="s", sku_name="#A#", sku_stock=100)], image=product_picture("red"))
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="#A#", sku_stock=90,
            sku_image_evidence=dict(product_picture("red"), source=SKU_IMAGE_OWN))], image=product_picture("red"))
        result = self.analyze()
        self.assertEqual(2, len(self.recommend_calls))
        self.assertEqual("sku", next(g["members"][0]["image_source"] for g in self.recommend_calls[-1][0]["candidates"] if g["members"][0]["shop_key"] == "A01"))
        self.assertFalse(self.evidence(result, b)["recommendation"]["cached"])

    def test_cache_invalidates_candidate_title_and_sku_name(self):
        a, b, c, _ = self.anchors()
        self.analyze()
        self.seed("杯子包装 #C#", shop="C01", title="新标题杯子", image=product_picture("blue"))
        self.analyze()
        self.assertEqual(2, len(self.recommend_calls))
        m = next(g["members"][0] for g in self.recommend_calls[-1][0]["candidates"] if g["members"][0]["shop_key"] == "C01")
        self.assertEqual("新标题杯子", m["product_name"])
        self.assertEqual("杯子包装 #C#", m["raw_sku_name"])

    def test_cache_invalidates_member_evidence(self):
        a, b, c, _ = self.anchors()
        self.analyze()
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="新包装", sku_stock=90)], shop="B01", title="新杯子", image=product_picture("purple"))
        self.analyze()
        self.assertEqual(2, len(self.recommend_calls))
        self.assertEqual("新包装", self.recommend_calls[-1][0]["member"]["raw_sku_name"])

    def test_cache_invalidates_rule_version(self):
        a, b, c, _ = self.anchors()
        self.analyze()
        # Simulate a deployed rule revision, not a mocked analysis collaborator.
        from bestseller_monitor import sku_recommendations
        old = sku_recommendations.RULE_VERSION
        self.addCleanup(setattr, sku_recommendations, "RULE_VERSION", old)
        sku_recommendations.RULE_VERSION = "sku-candidate-recommendation-test-v2"
        self.analyze()
        self.assertEqual(2, len(self.recommend_calls))
        self.assertEqual("sku-candidate-recommendation-test-v2", self.recommend_calls[-1][0]["rule"])

    def test_new_human_exclusion_prevents_cached_recommendation_application(self):
        a, b, c, _ = self.anchors()
        result = self.analyze()
        edited = self.service.edit_group(result["id"], "exclude", self.group_for(result, b)["id"], b)
        self.service.save_draft(edited["id"])
        again = self.analyze()
        self.assertTrue(self.group_for(again, b)["excluded_group"])
        self.assertEqual(1, self.group_for(again, b)["sku_count"])
        self.assertEqual("manual", self.evidence(again, b)["matching_state"])
        self.assertEqual(1, len(self.recommend_calls))

    def test_new_human_split_preserves_membership_instead_of_using_cache(self):
        a, b, c, _ = self.anchors()
        result = self.analyze()
        edited = self.service.edit_group(result["id"], "remove", self.group_for(result, b)["id"], b)
        self.service.save_draft(edited["id"])
        again = self.analyze()
        self.assertEqual(1, self.group_for(again, b)["sku_count"])
        self.assertNotEqual(self.group_for(again, a)["id"], self.group_for(again, b)["id"])
        self.assertEqual(1, len(self.recommend_calls))

    def test_human_excluded_candidate_is_not_sent_to_model(self):
        a, b, c, first = self.anchors()
        d = self.seed("#D#", shop="D01", image=product_picture("purple"))
        # Save an excluded candidate before the next public analysis.
        self.recommend_result = dict(group_id=None, reason="无法确定")
        current = self.analyze()
        current = self.service.edit_group(current["id"], "exclude", self.group_for(current, c)["id"], c)
        self.service.save_draft(current["id"])
        self.recommend_calls.clear()
        result = self.analyze()
        self.assertEqual(1, len(self.recommend_calls))
        shops = {m["shop_key"] for g in self.recommend_calls[0][0]["candidates"] for m in g["members"]}
        self.assertEqual({"A01", "D01"}, shops)
        self.assertNotEqual(self.group_for(result, b)["id"], self.group_for(result, c)["id"])

    def test_manual_exclusion_during_recommendation_rejects_fresh_result(self):
        a, b, c, _ = self.anchors()
        self.recommend_result = dict(group_id=None, reason="无法确定")
        saved = self.analyze()
        self.service.save_draft(saved["id"])
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="新图证据", sku_stock=90)], shop="B01", image=product_picture("purple"))
        def choose(context):
            edited = self.service.edit_group(saved["id"], "exclude", self.group_for(saved, b)["id"], b)
            self.service.save_draft(edited["id"])
            return dict(group_id=context["candidates"][0]["id"], reason="过时推荐不能生效")
        self.recommend_result = choose
        result = self.analyze()
        self.assertTrue(self.group_for(result, b)["excluded_group"])
        self.assertEqual(1, self.group_for(result, b)["sku_count"])
        self.assertEqual("manual", self.evidence(result, b)["matching_state"])

    def test_nonexistent_target_is_not_uncertainty_or_valid_cache_entry(self):
        a, b, c, _ = self.anchors()
        self.recommend_result = dict(group_id="no-such-group", reason="无效目标")
        result = self.analyze()
        self.assertEqual(1, self.group_for(result, b)["sku_count"])
        self.assertEqual("pending", self.evidence(result, b)["matching_state"])
        self.assertNotIn("recommendation", self.evidence(result, b))
        self.recommend_result = dict(group_id=None, reason="正常无法确定")
        again = self.analyze()
        self.assertEqual(2, len(self.recommend_calls))
        self.assertEqual("pending_candidates", self.evidence(again, b)["matching_state"])

    def test_caption_mode_supplies_visual_evidence_for_all_candidates(self):
        from dataclasses import replace
        from bestseller_monitor.analysis import AnalysisService
        a, b, c, _ = self.anchors()
        cfg = self.service.config.matching
        self.service = AnalysisService(replace(self.service.config, matching=replace(cfg, mode="caption", vision=cfg.model)), running=lambda: False)
        from bestseller_monitor.matching import request_json
        previous = request_json
        def describe(config, content, instruction, **kwargs):
            if kwargs.get("kind") == "caption":
                return dict(description="杯口杯底的真实视觉证据")
            return previous(config, content, instruction, **kwargs)
        with patch("bestseller_monitor.matching.request_json", side_effect=describe):
            result = self.analyze()
        content = self.recommend_calls[0][1]
        self.assertEqual(3, len([p for p in content if p.get("text", "").startswith("Image evidence:")]))
        self.assertEqual("recommended", self.evidence(result, b)["matching_state"])

    def test_real_request_transport_and_usage_accept_recommendation_calls(self):
        import base64
        import io
        from PIL import Image
        from contextlib import contextmanager
        a, b, c, _ = self.anchors()
        requests = []
        fail_recommendation = False
        @contextmanager
        def external(request, **kwargs):
            payload = json.loads(request.data)
            prompt = payload["messages"][0]["content"]
            content = payload["messages"][1]["content"]
            if "colors" in prompt:
                image = Image.open(io.BytesIO(base64.b64decode(content[0]["image_url"]["url"].split(",", 1)[1])))
                colors = {(255, 0, 0): "red", (0, 255, 0): "green", (0, 0, 255): "blue"}
                result = dict(colors=[colors[image.getpixel((i*50+25, 25))] for i in range(5)])
            elif "Choose at most ONE" in prompt:
                if fail_recommendation:
                    raise OSError("provider unavailable")
                context = json.loads(content[0]["text"])
                result = dict(group_id=context["candidates"][0]["id"], reason="完整请求路径的推荐")
                requests.append(payload)
            else:
                result = dict(same=True, confidence=.95, reason="主体一致")
            body = dict(choices=[dict(message=dict(content=json.dumps(result)))], usage=dict(prompt_tokens=10, completion_tokens=5))
            yield io.BytesIO(json.dumps(body).encode())
        with patch("bestseller_monitor.matching.request_json", real_request_json), patch("bestseller_monitor.matching.urlopen", side_effect=external):
            result = self.analyze()
        self.assertEqual(1, len(requests))
        self.assertEqual("recommended", self.evidence(result, b)["matching_state"])
        self.assertEqual("完整请求路径的推荐", self.evidence(result, b)["recommendation"]["reason"])
        # An external failure must remain a pending model call, not crash usage accounting.
        fail_recommendation = True
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="新图证据", sku_stock=90)], shop="B01", image=product_picture("purple"))
        with patch("bestseller_monitor.matching.request_json", real_request_json), patch("bestseller_monitor.matching.urlopen", side_effect=external):
            failed = self.analyze()
        self.assertEqual("pending", self.evidence(failed, b)["matching_state"])
        self.assertNotIn("recommendation", self.evidence(failed, b))



class SkuRecommendationBrowserTests(unittest.TestCase):
    observe = SkuRecommendationTests.observe
    member = staticmethod(SkuRecommendationTests.member)
    group_for = classmethod(SkuRecommendationTests.group_for.__func__)
    enable_model = SkuRecommendationTests.enable_model
    seed = SkuRecommendationTests.seed
    analyze = SkuRecommendationTests.analyze
    anchors = SkuRecommendationTests.anchors
    evidence = SkuRecommendationTests.evidence
    begin = fixtures.SkuMatchingBrowserTests.begin

    def setUp(self):
        from playwright.sync_api import sync_playwright
        from test_analysis import serve
        SkuRecommendationTests.setUp(self)
        self.server, self.thread = serve(self.service)
        def close():
            self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.addCleanup(close)
        pw = sync_playwright().start(); self.addCleanup(pw.stop)
        self.browser = pw.chromium.launch(channel="msedge", headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}")

    def retain(self, name):
        import os
        from pathlib import Path
        target = os.environ.get("SKU06_ARTIFACTS")
        if target:
            path = Path(target); path.mkdir(parents=True, exist_ok=True)
            self.page.locator(".sku-recommendation").last.scroll_into_view_if_needed()
            self.page.screenshot(path=str(path / (name + ".png")), full_page=True)

    def test_recommendation_reason_alternatives_save_restore_and_human_move(self):
        from playwright.sync_api import expect
        a, b, c, first = self.anchors()
        target = self.group_for(first, a)["id"]
        alternative = self.group_for(first, c)["id"]
        self.recommend_result = dict(group_id=target, reason="杯口与底座证据更完整")
        self.begin()
        self.page.locator("#skuQuery").fill("B01")
        card = self.page.locator(".sku-evidence-card").filter(has_text="蓝双件包装")
        expect(card).to_contain_text("多候选推荐")
        expect(card).to_contain_text("杯口与底座证据更完整")
        expect(card).to_contain_text("其他候选")
        expect(card).to_contain_text(alternative)
        expect(card).to_contain_text("并非人工确认")
        expect(card).to_contain_text("新增 SKU")
        expect(card).not_to_contain_text("95%")
        expect(self.page.locator(".group-status")).to_have_text("待确认")
        self.retain("recommended-pending-alternatives")
        self.page.locator("#saveDraft").click()
        expect(self.page.locator("#saveStatus")).to_contain_text("已保存")
        self.page.reload()
        # A recommended group is pending, so restoration stays on review.
        self.page.locator("#skuQuery").fill("B01")
        expect(card).to_contain_text("其他候选")
        self.retain("recommended-restored")
        self.page.once("dialog", lambda dialog: dialog.accept(alternative))
        card.get_by_role("button", name="移到其他组", exact=True).click()
        expect(self.page.locator("#groupDetail")).to_contain_text(alternative)
        expect(card).to_contain_text("人工归组")
        self.page.locator("#saveDraft").click()
        expect(self.page.locator("#saveStatus")).to_contain_text("已保存")
        saved = self.service.get(self.service.store.latest()["id"])
        self.assertEqual(alternative, self.group_for(saved, b)["id"])
        self.retain("human-moved-to-alternative")

    def test_successful_uncertainty_is_visible_and_restored_without_fake_scores(self):
        from playwright.sync_api import expect
        self.seed("#A#", image=product_picture("red"))
        self.seed("#C#", shop="C01", image=product_picture("blue"))
        b = self.seed("无编码候选", shop="B01", image=product_picture("green"))
        self.recommend_result = dict(group_id=None, reason="候选证据无法支持唯一归属")
        self.begin()
        self.page.locator("#skuQuery").fill("B01")
        card = self.page.locator(".sku-evidence-card")
        expect(card).to_contain_text("多个候选，无法确定归属")
        expect(card).to_contain_text("候选证据无法支持唯一归属")
        expect(card).to_contain_text("其他候选")
        expect(card).not_to_contain_text("模型失败")
        expect(card).not_to_contain_text("95%")
        self.retain("uncertain-independent")
        self.page.locator("#saveDraft").click()
        expect(self.page.locator("#saveStatus")).to_contain_text("已保存")
        self.page.reload()
        self.page.locator("#skuQuery").fill("B01")
        expect(card).to_contain_text("多个候选，无法确定归属")
        self.retain("uncertain-restored")
