"""Ticket 04 acceptance through real observations and public analysis operations."""
import unittest
from unittest.mock import patch
import test_sku_grouping as fixtures
import test_sku_drafts as evidence_fixtures


class MerchantCodeTests(unittest.TestCase):
    setUp = fixtures.SkuGroupingServiceTests.setUp
    observe = evidence_fixtures.SkuDraftTests.observe
    member = staticmethod(fixtures.SkuGroupingServiceTests.member)
    group_for = classmethod(fixtures.SkuGroupingServiceTests.group_for.__func__)

    def enable_model(self):
        from dataclasses import replace
        from bestseller_monitor.analysis import AnalysisService
        from bestseller_monitor.matching import MatchingConfig, ModelConfig
        import os
        key = patch.dict(os.environ, {"SKU04_TEST_KEY": "isolated-dummy-key"})
        key.start(); self.addCleanup(key.stop)
        matching = MatchingConfig(self.path.with_name("matching.sqlite"),
            ModelConfig(endpoint="http://127.0.0.1:9/v1/chat/completions", key_env="SKU04_TEST_KEY"), mode="direct", min_score=0)
        self.service = AnalysisService(replace(self.service.config, matching=matching), running=lambda: False)

    def seed(self, name, *, shop="A01", offer="11", sku="s", title="杯子", image=None):
        for day, stock in [("2026-09-07", 100), ("2026-09-14", 90)]:
            self.observe(day, [dict(sku_id=sku, sku_name=name, sku_stock=stock)], shop=shop, offer=offer, title=title, image=image)
        return self.member(shop, offer, sku)

    def test_cross_shop_same_code_without_images_or_model(self):
        self.enable_model()
        self.seed("红色单件 # ab-1 #")
        self.seed("蓝色双件包装 ＃AB-1＃", shop="B01")
        with patch("bestseller_monitor.matching.urlopen", side_effect=AssertionError("模型不得调用")) as calls:
            snapshot = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(0, calls.call_count)
        self.assertEqual(1, len(snapshot["groups"]))
        self.assertFalse(snapshot["groups"][0]["confirmed"])
        self.assertEqual(20, snapshot["groups"][0]["sales"])
        self.assertEqual(["AB-1", "AB-1"], [m["merchant_code"] for m in snapshot["members"]])
        self.assertEqual("同码归组", snapshot["groups"][0]["grouping_basis"])

    def test_same_code_with_real_pictures_and_configured_model_never_calls_transport(self):
        from helpers import product_picture
        self.enable_model()
        self.seed("红 #A#", image=product_picture("red"))
        self.seed("蓝双件 #A#", shop="B01", image=product_picture("blue"))
        with patch("bestseller_monitor.matching.urlopen", side_effect=AssertionError("模型不得调用")) as calls:
            snapshot = self.service.start("2026-09-07", "2026-09-14")
        self.assertTrue(all(m["image_data"] for m in snapshot["members"]))
        self.assertEqual(0, calls.call_count)
        self.assertEqual(1, len(snapshot["groups"]))

    def test_human_split_survives_reanalysis_and_new_member_keeps_candidates(self):
        left = self.seed("红 #A#")
        right = self.seed("蓝 #A#", shop="B01")
        snapshot = self.service.start("2026-09-07", "2026-09-14")
        snapshot = self.service.edit_group(snapshot["id"], "remove", self.group_for(snapshot, right)["id"], right)
        ids = {self.group_for(snapshot, left)["id"], self.group_for(snapshot, right)["id"]}
        self.service.save_draft(snapshot["id"])
        new = self.seed("新 #A#", shop="C01")
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(3, len(result["groups"]))
        self.assertEqual(ids, {self.group_for(result, left)["id"], self.group_for(result, right)["id"]})
        evidence = next(m for m in result["members"] if m["shop_key"] == "C01")
        self.assertEqual(ids, set(evidence["candidate_groups"]))
        self.assertIn("多个候选", evidence["matching_note"])
        self.assertEqual(1, self.group_for(result, new)["sku_count"])
        self.service.save_draft(result["id"])
        from bestseller_monitor.analysis import AnalysisService
        restart = AnalysisService(self.service.config, running=lambda: False)
        restored = restart.get(result["id"])
        self.assertEqual(ids, set(next(m for m in restored["members"] if m["shop_key"] == "C01")["candidate_groups"]))

    def test_confirmed_code_change_keeps_membership_and_requires_reconfirmation(self):
        left = self.seed("红 #A#")
        right = self.seed("蓝 #A#", shop="B01")
        snapshot = self.service.start("2026-09-07", "2026-09-14")
        group_id = self.group_for(snapshot, left)["id"]
        self.service.confirm_groups(snapshot["id"], [group_id])
        self.service.save_draft(snapshot["id"])
        self.observe("2026-09-21", [dict(sku_id="s", sku_name="红 #B#", sku_stock=80)])
        result = self.service.start("2026-09-07", "2026-09-21")
        group = self.group_for(result, left)
        self.assertEqual(group_id, group["id"])
        self.assertEqual(group_id, self.group_for(result, right)["id"])
        self.assertFalse(group["confirmed"])
        self.assertIn("商家编码", next(m for m in result["members"] if m["shop_key"] == "A01")["evidence_changes"])
        self.assertEqual(["A", "B"], group["merchant_codes"])
        self.assertTrue(group["code_conflict"])
        self.service.save_draft(result["id"])
        again = self.service.start("2026-09-07", "2026-09-21")
        self.assertFalse(self.group_for(again, left)["confirmed"])
        self.assertTrue(self.group_for(again, left)["code_conflict"])

    def test_new_same_code_member_invalidates_whole_confirmed_group_and_is_marked(self):
        left = self.seed("红 #A#")
        snapshot = self.service.start("2026-09-07", "2026-09-14")
        group_id = self.group_for(snapshot, left)["id"]
        self.service.confirm_groups(snapshot["id"], [group_id])
        self.service.save_draft(snapshot["id"])
        newcomer = self.seed("新 #A#", shop="B01")
        result = self.service.start("2026-09-07", "2026-09-14")
        group = self.group_for(result, newcomer)
        self.assertEqual(group_id, group["id"])
        self.assertFalse(group["confirmed"])
        self.assertEqual([newcomer], group["added_members"])
        self.assertEqual("同码归组", next(m for m in result["members"] if m["shop_key"] == "B01")["grouping_basis"])
        self.service.save_draft(result["id"])
        again = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual([newcomer], self.group_for(again, newcomer)["added_members"])
        accepted = self.service.confirm_groups(again["id"], [group_id])
        self.assertFalse(self.group_for(accepted, newcomer)["added_members"])

    def test_code_normalization_ambiguity_and_internal_differences(self):
        cases = [("# ab-1 #", "AB-1", False), ("＃AB-1＃", "AB-1", False),
                 ("#ab-1# # AB-1 #", "AB-1", False), ("#ab 1#", "AB 1", False),
                 ("#ab1#", "AB1", False), ("##", None, False), ("#   ＃", None, False),
                 ("#open", None, False), ("#A# #B#", None, True)]
        for index, (name, _, _) in enumerate(cases):
            self.seed(name, offer=str(100 + index))
        result = self.service.start("2026-09-07", "2026-09-14")
        for member, (raw, expected, ambiguous) in zip(result["members"], cases):
            with self.subTest(raw=raw):
                self.assertEqual(raw, member["raw_sku_name"])
                self.assertEqual(expected, member["merchant_code"])
                self.assertEqual(ambiguous, member["code_ambiguous"])
                if ambiguous:
                    self.assertEqual(["A", "B"], member["merchant_codes"])
                    self.assertIn("编码有歧义", member["matching_note"])
        self.assertEqual(7, len(result["groups"]))

    def test_single_sku_code_source_is_not_empty_model_input(self):
        cases = [("s", "真实 #RAW#", "商品 #TITLE#", "RAW", "raw_sku_name"),
                 ("s", "真实无码", "商品 #TITLE#", None, "raw_sku_name"),
                 ("default", "默认(单规格)", "商品 #TITLE#", "TITLE", "product_name"),
                 ("s", "默认(单规格)", "商品 #TITLE#", None, "raw_sku_name")]
        for index, (sku, name, title, _, _) in enumerate(cases):
            self.seed(name, sku=sku, title=title, offer=str(100 + index))
        result = self.service.start("2026-09-07", "2026-09-14")
        for member, (_, raw, title, code, source) in zip(result["members"], cases):
            with self.subTest(raw=raw, sku=member["sku_id"]):
                self.assertTrue(member["single_sku"])
                self.assertEqual(raw, member["raw_sku_name"])
                self.assertEqual(dict(product_name=title, sku_name=""), member["matching_input"])
                self.assertEqual(code, member["merchant_code"])
                self.assertEqual(source, member["code_source"])

    def test_different_codes_separate_without_model_but_manual_override_is_local(self):
        self.enable_model()
        from helpers import product_picture
        image = product_picture("red")
        a = self.seed("红 #A#", image=image)
        b = self.seed("蓝 #B#", shop="B01", image=image)
        with patch("bestseller_monitor.matching.urlopen", side_effect=AssertionError("不调用模型")) as calls:
            first = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(0, calls.call_count)
        self.assertEqual(2, len(first["groups"]))
        first = self.service.edit_group(first["id"], "move", self.group_for(first, b)["id"], b, self.group_for(first, a)["id"])
        gid = self.group_for(first, a)["id"]
        self.service.confirm_groups(first["id"], [gid])
        self.service.save_draft(first["id"])
        a2 = self.seed("另A #A#", shop="C01")
        b2 = self.seed("另B #B#", shop="D01")
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(1, len(result["groups"]))
        self.assertEqual(gid, self.group_for(result, a2)["id"])
        self.assertEqual(gid, self.group_for(result, b2)["id"])
        self.assertFalse(self.group_for(result, a)["confirmed"])
        # Split off another A anchor; the mixed group must not merge it through B.
        result = self.service.edit_group(result["id"], "remove", gid, a2)
        separate = self.group_for(result, a2)["id"]
        self.service.save_draft(result["id"])
        a3 = self.seed("第三A #A#", shop="E01")
        b3 = self.seed("第三B #B#", shop="F01")
        next_result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(gid, self.group_for(next_result, b3)["id"])
        self.assertEqual(separate, self.group_for(next_result, a2)["id"])
        self.assertEqual(1, self.group_for(next_result, a3)["sku_count"])
        self.assertEqual(3, len(next_result["groups"]))

    def test_normalization_equivalence_keeps_confirmation_but_name_change_does_not(self):
        member = self.seed("红 # ab-1 #")
        first = self.service.start("2026-09-07", "2026-09-14")
        gid = self.group_for(first, member)["id"]
        self.service.confirm_groups(first["id"], [gid]); self.service.save_draft(first["id"])
        self.observe("2026-09-21", [dict(sku_id="s", sku_name="红 ＃AB-1＃", sku_stock=80)])
        equivalent = self.service.start("2026-09-07", "2026-09-21")
        self.assertTrue(self.group_for(equivalent, member)["confirmed"])
        self.assertFalse(equivalent["members"][0]["evidence_changes"])
        self.service.save_draft(equivalent["id"])
        self.observe("2026-09-28", [dict(sku_id="s", sku_name="蓝 ＃AB-1＃", sku_stock=70)])
        changed = self.service.start("2026-09-07", "2026-09-28")
        self.assertFalse(self.group_for(changed, member)["confirmed"])
        self.assertIn("SKU 名称", changed["members"][0]["evidence_changes"])

    def test_unconfirmed_automatic_group_recomputes_changed_code(self):
        a = self.seed("红 #A#")
        b = self.seed("蓝 #A#", shop="B01")
        first = self.service.start("2026-09-07", "2026-09-14")
        self.service.save_draft(first["id"])
        self.observe("2026-09-21", [dict(sku_id="s", sku_name="红 #B#", sku_stock=80)])
        result = self.service.start("2026-09-07", "2026-09-21")
        self.assertNotEqual(self.group_for(result, a)["id"], self.group_for(result, b)["id"])
        self.assertEqual(2, len(result["groups"]))

    def test_exclusion_wins_over_same_code_after_evidence_change_and_restart(self):
        a = self.seed("红 #A#")
        b = self.seed("蓝 #A#", shop="B01")
        first = self.service.start("2026-09-07", "2026-09-14")
        first = self.service.edit_group(first["id"], "exclude", self.group_for(first, b)["id"], b)
        self.service.save_draft(first["id"])
        self.observe("2026-09-21", [dict(sku_id="s", sku_name="新蓝 #A#", sku_stock=80)], shop="B01")
        result = self.service.start("2026-09-07", "2026-09-21")
        self.assertTrue(self.group_for(result, b)["excluded_group"])
        self.assertNotEqual(self.group_for(result, a)["id"], self.group_for(result, b)["id"])
        self.service.save_draft(result["id"])
        from bestseller_monitor.analysis import AnalysisService
        restart = AnalysisService(self.service.config, running=lambda: False)
        replay = restart.start("2026-09-07", "2026-09-21")
        self.assertTrue(self.group_for(replay, b)["excluded_group"])

    def test_picture_and_product_name_changes_require_confirmation(self):
        from helpers import product_picture
        for day in ("2026-09-07", "2026-09-14"):
            self.observe(day, [dict(sku_id="s", sku_name="红 #A#", sku_stock=100)], image=product_picture("red"))
        first = self.service.start("2026-09-07", "2026-09-14")
        gid = first["groups"][0]["id"]
        self.service.confirm_groups(first["id"], [gid]); self.service.save_draft(first["id"])
        self.observe("2026-09-21", [dict(sku_id="s", sku_name="红 #A#", sku_stock=90)], title="新杯子", image=product_picture("blue"))
        result = self.service.start("2026-09-07", "2026-09-21")
        self.assertFalse(result["groups"][0]["confirmed"])
        self.assertIn("SKU 图片", result["members"][0]["evidence_changes"])
        self.assertIn("商品名称", result["members"][0]["evidence_changes"])

    def test_future_code_does_not_enter_historical_analysis(self):
        self.seed("红 #A#")
        self.observe("2026-09-21", [dict(sku_id="s", sku_name="未来 #B#", sku_stock=80)])
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual("A", result["members"][0]["merchant_code"])
        self.assertEqual("红 #A#", result["members"][0]["raw_sku_name"])

    def test_automatic_newcomer_not_yet_confirmed_can_recompute_on_code_change(self):
        original = self.seed("红 #A#")
        first = self.service.start("2026-09-07", "2026-09-14")
        gid = self.group_for(first, original)["id"]
        self.service.confirm_groups(first["id"], [gid]); self.service.save_draft(first["id"])
        new = self.seed("新 #A#", shop="B01")
        added = self.service.start("2026-09-07", "2026-09-14")
        self.service.save_draft(added["id"])
        self.observe("2026-09-21", [dict(sku_id="s", sku_name="新 #B#", sku_stock=80)], shop="B01")
        changed = self.service.start("2026-09-07", "2026-09-21")
        self.assertEqual(gid, self.group_for(changed, original)["id"])
        self.assertNotEqual(gid, self.group_for(changed, new)["id"])

    def test_unencoded_human_membership_is_not_discarded_by_unimplemented_model(self):
        a = self.seed("真实无码")
        b = self.seed("另一真实名", shop="B01")
        first = self.service.start("2026-09-07", "2026-09-14")
        first = self.service.edit_group(first["id"], "move", self.group_for(first, b)["id"], b, self.group_for(first, a)["id"])
        self.service.save_draft(first["id"])
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(1, len(result["groups"]))
        self.assertEqual(2, result["groups"][0]["sku_count"])

    def test_exclusion_survives_analysis_interval_where_sku_is_absent(self):
        a = self.seed("红 #A#")
        b = self.seed("蓝 #A#", shop="B01", offer="22")
        first = self.service.start("2026-09-07", "2026-09-14")
        first = self.service.edit_group(first["id"], "exclude", self.group_for(first, b)["id"], b)
        self.service.save_draft(first["id"])
        self.observe("2026-09-21", [dict(sku_id="s", sku_name="红 #A#", sku_stock=80)])
        absent = self.service.start("2026-09-15", "2026-09-21")
        self.assertEqual(1, len(absent["members"]))
        self.service.save_draft(absent["id"])
        self.observe("2026-09-28", [dict(sku_id="s", sku_name="蓝改名 #A#", sku_stock=80)], shop="B01", offer="22")
        returned = self.service.start("2026-09-07", "2026-09-28")
        self.assertTrue(self.group_for(returned, b)["excluded_group"])
        self.assertNotEqual(self.group_for(returned, a)["id"], self.group_for(returned, b)["id"])

    def test_pre_ticket04_saved_sku_result_restores_codes_without_live_inventory(self):
        from bestseller_monitor.analysis import AnalysisService
        self.seed("红 # a #")
        result = self.service.start("2026-09-07", "2026-09-14")
        self.service.confirm_groups(result["id"], [result["groups"][0]["id"]])
        result = self.service.save_and_view(result["id"])
        # Public storage fixture: prior ticket's frozen SKU result, not product cache.
        for member in result["members"]:
            for field in ("merchant_code", "merchant_codes", "code_ambiguous", "code_source", "evidence_rule", "grouping_basis"):
                member.pop(field, None)
        for group in result["groups"]:
            for field in ("manual_membership", "manual_members", "merchant_codes", "grouping_basis"):
                group.pop(field, None)
        self.service.store.write_sku(result)
        self.conn.close(); self.path.unlink()
        restored = AnalysisService(self.service.config, running=lambda: False).get(result["id"])
        self.assertEqual("A", restored["members"][0]["merchant_code"])
        self.assertEqual(["A"], restored["groups"][0]["merchant_codes"])
        self.assertEqual("人工归组", restored["groups"][0]["grouping_basis"])
        self.assertTrue(restored["groups"][0]["confirmed"])
        self.assertEqual(10, restored["groups"][0]["sales"])

    def test_manual_override_clears_unfinished_matching_notices(self):
        a = self.seed("无码")
        b = self.seed("蓝 #A#", shop="B01")
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertIn("尚未开放", result["members"][0]["matching_note"])
        result = self.service.edit_group(result["id"], "move", self.group_for(result, a)["id"], a, self.group_for(result, b)["id"])
        member = next(m for m in result["members"] if m["shop_key"] == "A01")
        self.assertEqual("人工归组", member["grouping_basis"])
        self.assertEqual("", member["matching_note"])
        self.assertEqual([], member["candidate_groups"])
        self.service.confirm_groups(result["id"], [self.group_for(result, a)["id"]])
        self.service.save_draft(result["id"])
        again = self.service.start("2026-09-07", "2026-09-14")
        self.assertTrue(self.group_for(again, a)["confirmed"])


class MerchantCodeBrowserTests(unittest.TestCase):
    def setUp(self):
        from dataclasses import replace
        evidence_fixtures.SkuDraftBrowserTests.setUp(self)
        self.service.config = replace(self.service.config, output=self.path.parent / "reports")
    observe = evidence_fixtures.SkuDraftTests.observe
    begin = evidence_fixtures.SkuDraftBrowserTests.begin

    def open_snapshot(self, snapshot):
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#analysis=" + snapshot["id"])
        self.page.reload()

    def retain(self, label):
        import os
        from pathlib import Path
        target = os.environ.get("SKU04_ARTIFACTS")
        if target:
            directory = Path(target); directory.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(directory / (label + ".png")), full_page=True)
            if label in ("changed-code-conflict", "automatic-addition", "ambiguous-code"):
                self.page.locator("#groupDetail .change").last.scroll_into_view_if_needed()
                self.page.screenshot(path=str(directory / (label + "-notice.png")), full_page=True)

    def test_real_confirmation_six_search_fields_and_ambiguity(self):
        from playwright.sync_api import expect
        for day, stock in [("2026-09-07", 100), ("2026-09-14", 80)]:
            self.observe(day, [dict(sku_id="blue", sku_name="蓝规格 # ab-1 #", sku_stock=stock)], title="蓝色商品", offer="22")
            self.observe(day, [dict(sku_id="amb", sku_name="#Q# #Z#", sku_stock=stock)], offer="33")
        self.begin()
        expect(self.page.locator("#skuQuery")).to_have_attribute("placeholder", "商品名称、SKU 名称、商家编码、商品编号、平台 SKU 编号或同款分组编号")
        self.page.locator("#skuQuery").fill("AB-1")
        expect(self.page.locator("#products [data-sku-group]")).to_have_count(1)
        target = self.page.locator("#products [data-sku-group]").get_attribute("data-sku-group")
        for query in ["蓝色商品", "蓝规格", "AB-1", "22", "blue", target]:
            self.page.locator("#skuQuery").fill(query)
            expect(self.page.locator("#products [data-sku-group]")).to_have_count(1)
            expect(self.page.locator(".sku-evidence-card")).to_contain_text("商家编码：AB-1")
            expect(self.page.locator(".sku-evidence-card")).to_contain_text("同码归组")
        self.retain("six-field-search")
        self.page.locator("#skuQuery").fill("amb")
        expect(self.page.locator(".sku-evidence-card")).to_contain_text("编码有歧义")
        expect(self.page.locator(".sku-evidence-card")).to_contain_text("Q、Z")
        expect(self.page.locator(".sku-evidence-card")).to_contain_text("图文判断尚未开放")
        self.retain("ambiguous-code")
        self.page.get_by_role("button", name="添加 SKU", exact=True).click()
        for query in ["ab-1", target]:
            self.page.locator("#addQuery").fill(query)
            expect(self.page.locator("#addResults .search-item")).to_have_count(1)
            expect(self.page.locator("#addResults")).to_contain_text("商家编码：AB-1")

    def test_browser_automatic_addition_then_confirmed_code_conflict(self):
        from playwright.sync_api import expect
        first = self.service.start("2026-09-07", "2026-09-14")
        gid = first["groups"][0]["id"]
        self.service.confirm_groups(first["id"], [gid]); self.service.save_draft(first["id"])
        self.observe("2026-09-07", [dict(sku_id="new", sku_name="新 #A#", sku_stock=100)], shop="B01")
        self.observe("2026-09-14", [dict(sku_id="new", sku_name="新 #A#", sku_stock=80)], shop="B01")
        added = self.service.start("2026-09-07", "2026-09-14")
        self.open_snapshot(added)
        expect(self.page.locator(".group-status")).to_have_text("待确认")
        expect(self.page.locator('[data-sku="new"]')).to_contain_text("新增 SKU")
        expect(self.page.locator('[data-sku="new"]')).to_contain_text("同码依据：A")
        self.retain("automatic-addition")
        self.page.get_by_role("button", name="确认当前分组", exact=True).click()
        self.page.locator("#saveDraft").click()
        expect(self.page.locator("#saveStatus")).to_contain_text("已保存")
        self.observe("2026-09-21", [dict(sku_id="s", sku_name="原始 SKU #B#", sku_stock=70)])
        changed = self.service.start("2026-09-07", "2026-09-21")
        self.open_snapshot(changed)
        expect(self.page.locator(".group-status")).to_have_text("待确认")
        expect(self.page.locator('[data-sku="s"]')).to_contain_text("证据变化")
        expect(self.page.locator('[data-sku="s"]')).to_contain_text("商家编码：B")
        expect(self.page.locator("#groupDetail")).to_contain_text("编码冲突")
        expect(self.page.locator("#groupDetail")).to_contain_text("保留原人工归属")
        self.page.locator("#saveDraft").click()
        expect(self.page.locator("#saveStatus")).to_contain_text("已保存")
        self.page.reload()
        expect(self.page.locator("#groupDetail")).to_contain_text("编码冲突")
        self.retain("changed-code-conflict")

    def test_online_and_offline_results_share_codes_and_group_only_sales(self):
        from playwright.sync_api import expect
        for day, stock in [("2026-09-07", 100), ("2026-09-14", 80)]:
            self.observe(day, [dict(sku_id="same", sku_name="蓝 #a#", sku_stock=stock),
                               dict(sku_id="outside", sku_name="组外 #B#", sku_stock=stock)], shop="B01", offer="22")
        result = self.service.start("2026-09-07", "2026-09-14")
        group = next(g for g in result["groups"] if g["merchant_codes"] == ["A"])
        self.open_snapshot(result)
        # Explicit confirmation makes result/report access legal, but never adds
        # the other SKU from the same product to this group's sales or curves.
        for g in result["groups"]:
            self.service.confirm_groups(result["id"], [g["id"]])
        self.page.reload()
        self.page.locator("#saveAndView").click()
        expect(self.page.locator("#results")).to_be_visible()
        row = self.page.locator(f'#ranking > details[data-group="{group["id"]}"]')
        expect(row.locator("summary").first).to_contain_text("商家编码：A")
        row.locator("summary").first.click()
        expect(row).to_contain_text("人工归组")
        expect(row.locator(".sku-row")).to_have_count(2)
        expect(row).to_contain_text("本组内销量小计 20")
        expect(row).not_to_contain_text("组外")
        self.page.locator("#exportReport").click()
        expect(self.page.locator("#exportStatus")).to_contain_text("已导出")
        report = next(self.service.config.output.glob("*.html"))
        context = self.browser.new_context(offline=True); self.addCleanup(context.close)
        offline = context.new_page(); offline.goto(report.as_uri())
        exported = offline.locator(f'details[data-group="{group["id"]}"]')
        expect(exported.locator("summary").first).to_contain_text("商家编码：A")
        exported.locator("summary").first.click()
        expect(exported).to_contain_text("人工归组")
        expect(exported.locator(".sku-row")).to_have_count(2)
        expect(exported).to_contain_text("本组内销量小计 20")
        expect(exported).not_to_contain_text("组外")
        self.retain("online-code-report")
        import os
        if os.environ.get("SKU04_ARTIFACTS"):
            offline.screenshot(path=str(__import__("pathlib").Path(os.environ["SKU04_ARTIFACTS"]) / "offline-code-report.png"), full_page=True)
