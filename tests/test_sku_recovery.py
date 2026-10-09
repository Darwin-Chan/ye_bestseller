"""Ticket 07 lifecycle acceptance at the public service seam."""
import json
import unittest
from bestseller_monitor.matching import ModelFailure
from bestseller_monitor.analysis import AnalysisService
import test_sku_recommendations as fixtures
from helpers import product_picture


class SkuRecoveryTests(unittest.TestCase):
    setUp = fixtures.SkuRecommendationTests.setUp
    observe = fixtures.SkuRecommendationTests.observe
    member = staticmethod(fixtures.SkuRecommendationTests.member)
    group_for = classmethod(fixtures.SkuRecommendationTests.group_for.__func__)
    enable_model = fixtures.SkuRecommendationTests.enable_model
    seed = fixtures.SkuRecommendationTests.seed
    analyze = fixtures.SkuRecommendationTests.analyze
    anchors = fixtures.SkuRecommendationTests.anchors
    evidence = fixtures.SkuRecommendationTests.evidence

    def test_partial_pairs_restore_and_retry_only_unfinished_call(self):
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无编码", shop="B01", image=product_picture("green"))
        self.seed("#C#", shop="C01", image=product_picture("blue"))
        def partial(content):
            if len(self.calls) == 2:
                raise ModelFailure("模型请求失败")
            return dict(same=True, confidence=.95, reason="主体一致")
        self.result = partial
        first = self.analyze()
        self.assertEqual(2, len(self.calls))
        self.service.save_draft(first["id"])
        restored = AnalysisService(self.service.config, running=lambda: False)
        saved = restored.get(first["id"])
        self.assertEqual(1, saved["matching"]["completed"])
        self.assertEqual(1, saved["matching"]["retryable"])
        self.assertEqual(2, saved["matching"]["unfinished_members"])
        self.assertEqual(1, saved["matching"]["completed_members"])
        self.result = dict(same=False, confidence=.95, reason="其他候选非同款")
        final = restored.retry_matching(first["id"])
        self.assertEqual(3, len(self.calls))
        self.assertEqual(self.group_for(final, a)["id"], self.group_for(final, b)["id"])
        self.assertEqual(0, final["matching"]["retryable"])
        restored.retry_matching(first["id"])
        self.assertEqual(3, len(self.calls))

    def test_saved_valid_result_survives_missing_model_configuration(self):
        from dataclasses import replace
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        first = self.analyze()
        self.service.save_draft(first["id"])
        self.service = AnalysisService(replace(self.service.config, matching=None), running=lambda: False)
        result = self.analyze()
        self.assertEqual(self.group_for(result, a)["id"], self.group_for(result, b)["id"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, result["matching"]["completed"])

    def split_code_anchors(self):
        a = self.seed("#A#", image=product_picture("red"))
        c = self.seed("#A#", shop="C01", image=product_picture("blue"))
        first = self.analyze()
        split = self.service.edit_group(first["id"], "remove", self.group_for(first, c)["id"], c)
        self.service.save_draft(first["id"])
        b = self.seed("#A#", shop="B01", image=product_picture("green"))
        d = self.seed("#A#", shop="D01", image=product_picture("purple"))
        return a, b, c, d, split

    def test_partial_recommendations_only_retry_failed_not_successful_uncertainty(self):
        a, b, c, d, _ = self.split_code_anchors()
        def partial(context):
            if len(self.recommend_calls) == 2:
                raise ModelFailure("候选请求失败")
            return dict(group_id=None, reason="已比较，不能唯一推荐")
        self.recommend_result = partial
        first = self.analyze()
        self.assertEqual(2, len(self.recommend_calls))
        self.assertEqual(1, first["matching"]["completed"])
        self.assertEqual(1, first["matching"]["retryable"])
        self.service.save_draft(first["id"])
        restored = AnalysisService(self.service.config, running=lambda: False)
        restored.get(first["id"])
        self.recommend_result = lambda context: dict(group_id=context["candidates"][0]["id"], reason="补完推荐")
        final = restored.retry_matching(first["id"])
        self.assertEqual(3, len(self.recommend_calls))
        self.assertEqual(0, len(self.calls))
        self.assertIsNone(self.evidence(final, b)["recommendation"]["group_id"])
        self.assertEqual("completed_uncertain", self.evidence(final, b)["model_work"][0]["state"])
        self.assertEqual(0, final["matching"]["retryable"])
        restored.retry_matching(first["id"])
        self.assertEqual(3, len(self.recommend_calls))

    def test_stopped_recommendation_drops_late_result_and_resumes_only_remaining(self):
        import threading
        a, b, c, d, _ = self.split_code_anchors()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def delayed(context):
            if len(self.recommend_calls) == 2:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("external substitute timed out")
            return dict(group_id=context["candidates"][0]["id"], reason="有效推荐")
        self.recommend_result = delayed
        accepted = self.service.start_job("2026-09-07", "2026-09-14")
        self.assertTrue(entered.wait(5))
        self.service.request_stop(accepted["id"])
        release.set()
        terminal = self.service.wait_terminal(accepted["id"], 5)
        first = self.service.get(accepted["id"])
        self.assertEqual("ready", terminal["state"])
        self.assertTrue(terminal["stop_requested"])
        self.assertEqual(1, first["matching"]["completed"])
        self.assertEqual(1, first["matching"]["reasons"]["stopped"])
        self.assertNotIn("recommendation", self.evidence(first, d))
        self.service.save_draft(first["id"])
        final = self.service.retry_matching(first["id"])
        self.assertEqual(3, len(self.recommend_calls))
        self.assertEqual("recommended", self.evidence(final, d)["matching_state"])
        self.assertEqual(0, final["matching"]["unfinished"])



    def test_unconfigured_model_preserves_codes_and_manual_decisions_and_can_resume(self):
        from dataclasses import replace
        a = self.seed("#A#", image=product_picture("red"))
        c = self.seed("#A#", shop="C01", image=product_picture("blue"))
        b = self.seed("无码", shop="B01", image=product_picture("green"))
        self.result = dict(same=False, confidence=.95, reason="不同主体")
        first = self.analyze()
        self.service.edit_group(first["id"], "exclude", self.group_for(first, b)["id"], b)
        self.service.save_draft(first["id"])
        config = self.service.config
        self.service = AnalysisService(replace(config, matching=None), running=lambda: False)
        d = self.seed("无编码新成员", shop="D01", image=product_picture("purple"))
        result = self.analyze()
        self.assertEqual(self.group_for(result, a)["id"], self.group_for(result, c)["id"])
        self.assertTrue(self.group_for(result, b)["excluded_group"])
        self.assertIn("needs_config", {t["state"] for t in result["model_tasks"].values()})
        before = len(self.calls)
        self.service.save_draft(result["id"])
        self.service = AnalysisService(config, running=lambda: False)
        self.service.get(result["id"])
        final = self.service.retry_matching(result["id"])
        self.assertGreater(len(self.calls), before)
        self.assertTrue(self.group_for(final, b)["excluded_group"])
        self.assertEqual(0, final["matching"]["retryable"])


    def test_unconfigured_recommendations_are_distinct_from_successful_uncertainty(self):
        from dataclasses import replace
        a, b, c, d, _ = self.split_code_anchors()
        config = self.service.config
        self.service = AnalysisService(replace(config, matching=None), running=lambda: False)
        first = self.analyze()
        self.assertEqual(2, first["matching"]["reasons"]["needs_config"])
        self.assertEqual(0, len(self.recommend_calls))
        self.service.save_draft(first["id"])
        self.service = AnalysisService(config, running=lambda: False)
        restored = self.service.get(first["id"])
        self.assertEqual(2, restored["matching"]["retryable"])
        self.recommend_result = dict(group_id=None, reason="成功但无法唯一推荐")
        final = self.service.retry_matching(first["id"])
        self.assertEqual(2, len(self.recommend_calls))
        self.assertEqual(2, final["matching"]["reasons"]["completed_uncertain"])
        self.service.retry_matching(first["id"])
        self.assertEqual(2, len(self.recommend_calls))


    def test_missing_evidence_and_negative_judgments_survive_restoration(self):
        self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        d = self.seed("缺图", shop="D01")
        self.result = dict(same=False, confidence=.95, reason="不同主体")
        first = self.analyze()
        self.service.save_draft(first["id"])
        saved = AnalysisService(self.service.config, running=lambda: False).get(first["id"])
        self.assertEqual("completed_negative", self.evidence(saved, b)["model_work"][0]["state"])
        self.assertEqual("missing_evidence", self.evidence(saved, d)["model_work"][0]["state"])
        self.assertFalse(self.evidence(saved, d)["model_work"][0]["retryable"])
        self.assertEqual(1, saved["matching"]["completed"])


    def test_unavailable_pairs_and_recommendations_keep_distinct_branch_states(self):
        self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        def unavailable(content):
            raise ModelFailure("模型不可用")
        self.result = unavailable
        first = self.analyze()
        work = self.evidence(first, b)["model_work"]
        self.assertEqual(["judgment"], [t["branch"] for t in work])
        self.assertEqual(["model_unavailable"], [t["state"] for t in work])
        self.service.save_draft(first["id"])
        self.assertEqual(first["matching"], AnalysisService(self.service.config, running=lambda: False).get(first["id"])["matching"])
        self.result = dict(same=True, confidence=.95, reason="恢复后有效")
        final = self.service.retry_matching(first["id"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual(1, final["matching"]["completed"])


    def test_stopped_pair_late_result_never_applies_or_becomes_a_paid_cache_hit(self):
        import threading
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def delayed(content):
            entered.set()
            if not release.wait(5):
                raise AssertionError("external substitute timed out")
            return dict(same=True, confidence=.95, reason="迟到结果")
        self.result = delayed
        accepted = self.service.start_job("2026-09-07", "2026-09-14")
        self.assertTrue(entered.wait(5))
        self.service.request_stop(accepted["id"])
        release.set()
        self.service.wait_terminal(accepted["id"], 5)
        first = self.service.get(accepted["id"])
        self.assertNotEqual(self.group_for(first, a)["id"], self.group_for(first, b)["id"])
        self.assertEqual(1, first["matching"]["reasons"]["stopped"])
        final = self.service.retry_matching(first["id"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual(self.group_for(final, a)["id"], self.group_for(final, b)["id"])


    def test_failed_pair_retry_honors_exclusion_binding_and_remove_boundaries(self):
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("无码", shop="B01", image=product_picture("blue"))
        c = self.seed("#C#", shop="C01", image=product_picture("green"))
        def fail(content):
            raise ModelFailure("模型请求失败")
        self.result = fail
        first = self.analyze()
        self.assertEqual(2, len(self.calls))
        excluded = self.service.edit_group(first["id"], "exclude", self.group_for(first, b)["id"], b)
        final = self.service.retry_matching(first["id"])
        self.assertTrue(self.group_for(final, b)["excluded_group"])
        self.assertEqual(2, len(self.calls))
        final = self.service.edit_group(first["id"], "move", self.group_for(final, b)["id"], b, self.group_for(final, a)["id"])
        self.result = dict(same=True, confidence=.95, reason="恢复")
        final = self.service.retry_matching(first["id"])
        self.assertEqual(self.group_for(final, a)["id"], self.group_for(final, b)["id"])
        final = self.service.edit_group(first["id"], "remove", self.group_for(final, b)["id"], b)
        before = len(self.calls)
        final = self.service.retry_matching(first["id"])
        self.assertNotEqual(self.group_for(final, a)["id"], self.group_for(final, b)["id"])
        self.assertEqual(before, len(self.calls))

    def test_retry_revalidates_raw_sku_name_picture_source_and_rule(self):
        from unittest.mock import patch
        from bestseller_monitor.db import SKU_IMAGE_OWN
        a = self.seed("#A#", image=product_picture("red"))
        b = self.seed("原始无编码名", shop="B01", image=product_picture("blue"))
        first = self.analyze()
        self.assertEqual(1, len(self.calls))
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="新原始无编码名", sku_stock=90)], shop="B01", image=product_picture("blue"))
        changed = self.service.retry_matching(first["id"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual("新原始无编码名", self.evidence(changed, b)["raw_sku_name"])
        self.observe("2026-09-14", [dict(sku_id="s", sku_name="新原始无编码名", sku_stock=90,
            sku_image_evidence=dict(product_picture("blue"), source=SKU_IMAGE_OWN))], shop="B01", image=product_picture("blue"))
        self.service.retry_matching(first["id"])
        self.assertEqual(3, len(self.calls))
        with patch("bestseller_monitor.sku_matching.RULE_VERSION", "sku07-test-rule-v2"):
            self.service.retry_matching(first["id"])
        self.assertEqual(4, len(self.calls))

    def test_recommendation_retry_revalidates_candidate_evidence_members_and_rule(self):
        from unittest.mock import patch
        a, b, c, d, _ = self.split_code_anchors()
        first = self.analyze()
        self.assertEqual(2, len(self.recommend_calls))
        self.service.retry_matching(first["id"])
        self.assertEqual(2, len(self.recommend_calls))
        self.seed("#A#新版", image=product_picture("orange"))
        changed = self.service.retry_matching(first["id"])
        self.assertEqual(4, len(self.recommend_calls))
        e = self.seed("#A#", shop="E01", image=product_picture("pink"))
        final = self.service.retry_matching(first["id"])
        self.assertIn(e, [self.member(m["shop_key"], m["offer_id"], m["sku_id"]) for m in final["members"]])
        self.assertEqual(5, len(self.recommend_calls))
        with patch("bestseller_monitor.sku_recommendations.RULE_VERSION", "sku07-recommend-rule-v2"):
            self.service.retry_matching(first["id"])
        self.assertEqual(8, len(self.recommend_calls))

    def test_failed_recommendation_retry_honors_exclude_bind_and_remove(self):
        a, b, c, d, _ = self.split_code_anchors()
        def fail(context):
            raise ModelFailure("模型不可用")
        self.recommend_result = fail
        first = self.analyze()
        self.assertEqual(2, first["matching"]["reasons"]["model_unavailable"])
        excluded = self.service.edit_group(first["id"], "exclude", self.group_for(first, b)["id"], b)
        self.recommend_result = lambda context: dict(group_id=None, reason="不能唯一推荐")
        final = self.service.retry_matching(first["id"])
        self.assertEqual(3, len(self.recommend_calls))
        self.assertTrue(self.group_for(final, b)["excluded_group"])
        bound = self.service.edit_group(first["id"], "move", self.group_for(final, b)["id"], b, self.group_for(final, a)["id"])
        final = self.service.retry_matching(first["id"])
        self.assertEqual(self.group_for(final, a)["id"], self.group_for(final, b)["id"])
        removed = self.service.edit_group(first["id"], "remove", self.group_for(final, b)["id"], b)
        final = self.service.retry_matching(first["id"])
        self.assertNotEqual(self.group_for(final, a)["id"], self.group_for(final, b)["id"])
        self.assertNotIn(self.group_for(final, a)["id"], self.evidence(final, b).get("candidate_groups", []))

    def test_analysis_range_change_revalidates_judgment_context(self):
        self.seed("#A#", image=product_picture("red"))
        self.seed("无码", shop="B01", image=product_picture("blue"))
        self.analyze()
        other = self.service.start("2026-09-08", "2026-09-14")
        self.assertEqual(2, len(self.calls))
        self.service.retry_matching(other["id"])
        self.assertEqual(2, len(self.calls))

    def test_analysis_range_change_revalidates_recommendation_context(self):
        self.split_code_anchors()
        self.analyze()
        other = self.service.start("2026-09-08", "2026-09-14")
        self.assertEqual(4, len(self.recommend_calls))
        self.service.retry_matching(other["id"])
        self.assertEqual(4, len(self.recommend_calls))

    def test_partial_positive_recommendation_restores_without_duplicate_call(self):
        a, b, c, d, _ = self.split_code_anchors()
        def partial(context):
            if len(self.recommend_calls) == 2:
                raise ModelFailure("second recommendation failed")
            return dict(group_id=context["candidates"][0]["id"], reason="有效归属")
        self.recommend_result = partial
        first = self.analyze()
        self.assertGreater(self.group_for(first, b)["sku_count"], 1)
        self.service.save_draft(first["id"])
        restored = AnalysisService(self.service.config, running=lambda: False)
        restored.get(first["id"])
        self.recommend_result = lambda context: dict(group_id=context["candidates"][0]["id"], reason="补做有效推荐")
        final = restored.retry_matching(first["id"])
        self.assertEqual(3, len(self.recommend_calls))
        self.assertEqual(self.group_for(first, b)["id"], self.group_for(final, b)["id"])
        self.assertEqual("有效归属", self.evidence(final, b)["recommendation"]["reason"])
        restored.retry_matching(first["id"])
        self.assertEqual(3, len(self.recommend_calls))

    def test_retry_recommendation_rechecks_manual_save_during_external_call(self):
        a, b, c, d, _ = self.split_code_anchors()
        def fail(context):
            raise ModelFailure("调用失败")
        self.recommend_result = fail
        first = self.analyze()
        self.service.save_draft(first["id"])
        actor = AnalysisService(self.service.config, running=lambda: False)
        actor.get(first["id"])
        def manual(context):
            saved = actor.get(first["id"])
            if not self.group_for(saved, b).get("excluded_group"):
                actor.edit_group(first["id"], "exclude", self.group_for(saved, b)["id"], b)
                actor.save_draft(first["id"])
            return dict(group_id=context["candidates"][0]["id"], reason="不能覆盖人工排除")
        self.recommend_result = manual
        final = self.service.retry_matching(first["id"])
        self.assertTrue(self.group_for(final, b)["excluded_group"])
        self.assertEqual("人工排除", self.evidence(final, b)["grouping_basis"])

class SkuRecoveryBrowserTests(unittest.TestCase):
    setUp = fixtures.SkuRecommendationBrowserTests.setUp
    observe = SkuRecoveryTests.observe
    member = staticmethod(SkuRecoveryTests.member)
    group_for = classmethod(SkuRecoveryTests.group_for.__func__)
    enable_model = SkuRecoveryTests.enable_model
    seed = SkuRecoveryTests.seed
    analyze = SkuRecoveryTests.analyze
    anchors = SkuRecoveryTests.anchors
    evidence = SkuRecoveryTests.evidence
    split_code_anchors = SkuRecoveryTests.split_code_anchors
    begin = fixtures.SkuRecommendationBrowserTests.begin

    def retain(self, name):
        import os
        from pathlib import Path
        target = os.environ.get("SKU07_ARTIFACTS")
        if target:
            path = Path(target); path.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path / (name + ".png")), full_page=True)

    def test_page_partial_failure_save_restore_selective_retry_matches_public_service(self):
        from playwright.sync_api import expect
        self.seed("#A#", image=product_picture("red"))
        self.seed("无码", shop="B01", image=product_picture("green"))
        self.seed("#C#", shop="C01", image=product_picture("blue"))
        def partial(content):
            if len(self.calls) == 2:
                raise ModelFailure("模型请求失败")
            return dict(same=True, confidence=.95, reason="已完成有效主体判断")
        self.result = partial
        self.begin()
        expect(self.page.locator("#matchingStatus")).to_contain_text("调用失败")
        expect(self.page.locator("#runMatching")).to_be_enabled()
        self.page.locator("#skuQuery").fill("B01")
        expect(self.page.locator(".sku-evidence-card")).to_contain_text("普通图文判断")
        expect(self.page.locator(".sku-evidence-card")).to_contain_text("调用失败")
        self.retain("pair-partial-failure")
        self.page.locator("#saveDraft").click()
        expect(self.page.locator("#saveStatus")).to_contain_text("已保存")
        self.page.reload()
        expect(self.page.locator("#matchingStatus")).to_contain_text("已完成 1")
        self.result = dict(same=False, confidence=.95, reason="剩余候选非同款")
        self.page.locator("#runMatching").click()
        expect(self.page.locator("#matchingStatus")).to_contain_text("未完成 0")
        expect(self.page.locator("#runMatching")).to_be_disabled()
        self.assertEqual(3, len(self.calls))
        self.page.locator("#skuQuery").fill("B01")
        expect(self.page.locator(".group-status")).to_have_text("待确认")
        expect(self.page.locator(".sku-evidence-card").filter(has_text="原始 SKU 名称：无码")).to_contain_text("复用有效判断")
        self.retain("pair-retry-legal-pending")



    def test_page_missing_configuration_evidence_and_resume(self):
        from dataclasses import replace
        from playwright.sync_api import expect
        config = self.service.config
        self.seed("#A#", image=product_picture("red"))
        self.seed("无码", shop="B01", image=product_picture("blue"))
        self.seed("无图片", shop="C01")
        self.service.config = replace(config, matching=None)
        self.begin()
        expect(self.page.locator("#matchingStatus")).to_contain_text("需要模型配置")
        expect(self.page.locator("#matchingStatus")).to_contain_text("缺少图片证据")
        self.retain("needs-config-and-evidence")
        self.assertEqual(0, len(self.calls))
        self.service.config = config
        self.page.locator("#runMatching").click()
        expect(self.page.locator("#matchingStatus")).to_contain_text("已完成 1")
        expect(self.page.locator("#matchingStatus")).to_contain_text("缺少图片证据")
        expect(self.page.locator("#runMatching")).to_be_disabled()
        self.assertEqual(1, len(self.calls))
        self.retain("config-restored-legal-pending")

    def test_page_recommendation_stop_and_selective_resume(self):
        import threading
        from playwright.sync_api import expect
        self.split_code_anchors()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def delayed(context):
            if len(self.recommend_calls) == 2:
                entered.set()
                if not release.wait(10):
                    raise ModelFailure("test release timed out")
            return dict(group_id=None, reason="成功但不能唯一推荐")
        self.recommend_result = delayed
        self.begin()
        self.assertTrue(entered.wait(5))
        self.page.locator("#maskStop").click()
        release.set()
        expect(self.page.locator("#matchingStatus")).to_contain_text("用户/运行已停止")
        expect(self.page.locator("#matchingStatus")).to_contain_text("已完成但无法推荐唯一组")
        self.assertEqual(2, len(self.recommend_calls))
        self.retain("recommendation-stopped-partial")
        self.page.locator("#saveDraft").click()
        expect(self.page.locator("#saveStatus")).to_contain_text("已保存")
        self.page.reload()
        expect(self.page.locator("#matchingStatus")).to_contain_text("用户/运行已停止")
        self.page.locator("#runMatching").click()
        expect(self.page.locator("#matchingStatus")).to_contain_text("未完成 0")
        self.assertEqual(3, len(self.recommend_calls))
        expect(self.page.locator("#runMatching")).to_be_disabled()
        self.retain("recommendation-resumed-uncertain")
