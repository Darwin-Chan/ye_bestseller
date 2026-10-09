"""Ticket 03 SKU ranking integration coverage at the public AnalysisService seam."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect
from helpers import new_round, product_picture
from bestseller_monitor.db import SKU_IMAGE_OWN


class SkuRankingServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inventory.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.service = AnalysisService(
            AnalysisConfig(self.path, member_kind="sku", output=Path(self.tmp.name)/"reports"), running=lambda: False
        )

    def observe(self, day, rows, *, offer="11", shop="A01", title="杯子", image=None):
        self.db.submit_inventory_snapshot(
            round_id=new_round(self.db, shop, run_date=day),
            shop_key=shop,
            shop_url="https://shop.test/" + shop,
            shop_name="店铺" + shop,
            offer_id=offer,
            product_url="https://offer.test/" + offer,
            list_title=title,
            detail_title=title,
            main_image_url="",
            image_evidence=image,
            sku_rows=rows,
            collected_at=day + "T01:00:00+00:00",
            attempt=1,
        )

    @staticmethod
    def member(shop, offer, sku):
        return {"shop_key": shop, "offer_id": offer, "sku_id": sku}

    @classmethod
    def group_for(cls, snapshot, member):
        key = (member["shop_key"], member["offer_id"], member["sku_id"])
        return next(
            group
            for group in snapshot["groups"]
            if any(
                (item["shop_key"], item["offer_id"], item["sku_id"]) == key
                for item in group["members"]
            )
        )

    def contamination(self):
        for day, a, outside in [("2026-09-07",20,200),("2026-09-14",15,100)]:
            self.observe(day, [dict(sku_id="a", sku_name="单个装", sku_stock=a, observation_complete=True),
                               dict(sku_id="outside", sku_name="组外", sku_stock=outside, observation_complete=True)], title="甲")
        for day, b in [("2026-09-07",30),("2026-09-14",21)]:
            self.observe(day, [dict(sku_id="b", sku_name="双件套", sku_stock=b, observation_complete=True)], offer="22", shop="B01", title="乙")
        s=self.service.start("2026-09-07", "2026-09-14")
        a=self.member("A01", "11", "a")
        b=self.member("B01", "22", "b")
        return self.service.edit_group(s["id"], "move", self.group_for(s,b)["id"], b, self.group_for(s,a)["id"]), a, b

    def test_public_group_result_excludes_other_skus_from_every_level(self):
        s,a,b=self.contamination()
        g=self.group_for(s,a)
        self.assertEqual(b, g["representative"])
        self.assertEqual(14, g["sales"])
        self.assertEqual((2,2,2), (g["shop_count"],g["product_count"],g["sku_count"]))
        self.assertEqual([5,9], [p["sales"] for p in g["products"]])
        self.assertEqual([["a"],["b"]], [[m["sku_id"] for m in p["members"]] for p in g["products"]])
        self.assertEqual(14, sum(p["sales"] for p in g["points"]))
        self.assertEqual(36, g["points"][-1]["stock"])
        self.assertEqual(5, sum(p["sales"] for p in g["products"][0]["points"]))
        self.assertEqual(15, g["products"][0]["points"][-1]["stock"])
        self.assertEqual("乙", next(m for m in s["members"] if m["sku_id"]=="b")["display_name"])
        self.assertEqual(100, next(g for g in s["groups"] if g["id"]==s["ranking"][0])["sales"])
        self.assertEqual(g["id"],s["review_order"][0])

    def test_save_view_export_and_restart_use_the_same_sku_result(self):
        s,a,b=self.contamination()
        with self.assertRaisesRegex(ValueError, "待确认"):
            self.service.save_and_view(s["id"])
        s=self.service.confirm_groups(s["id"],[g["id"] for g in s["groups"]])
        s=self.service.save_and_view(s["id"])
        self.assertFalse(s["dirty"])
        restored=AnalysisService(self.service.config, running=lambda: False).get(s["id"])
        self.assertEqual(s["groups"],restored["groups"])
        self.assertEqual(s["members"],restored["members"])
        self.assertEqual({"11":"https://offer.test/11","22":"https://offer.test/22"},self.service.export_sources(s["id"])["urls"])
        exported=self.service.export(s["id"], "<!doctype html><p>offline</p>")
        self.assertEqual("<!doctype html><p>offline</p>",Path(exported["path"]).read_text(encoding="utf-8"))
        self.service.withdraw_groups(s["id"],[self.group_for(s,a)["id"]])
        with self.assertRaisesRegex(ValueError,"未保存"):
            self.service.export(s["id"],"<!doctype html><p>offline</p>")
        self.service.save_draft(s["id"])
        with self.assertRaisesRegex(ValueError,"待确认"):
            self.service.export(s["id"],"<!doctype html><p>offline</p>")

    def merge_all(self,s):
        target=s["groups"][0]["id"]
        for group in s["groups"][1:]:
            for ref in group["members"]:
                s=self.service.edit_group(s["id"],"move",group["id"],ref,target)
        return s

    def test_equal_sales_choose_stable_identity_not_image_availability(self):
        own=product_picture("red")
        for day,stock in [("2026-09-07",100),("2026-09-14",90)]:
            self.observe(day,[dict(sku_id="z",sku_name="双件套",sku_stock=stock,observation_complete=True,
                sku_image_evidence=dict(own,source=SKU_IMAGE_OWN)),
                dict(sku_id="a",sku_name="单个装",sku_stock=stock,observation_complete=True)],title="套装")
        s=self.merge_all(self.service.start("2026-09-07","2026-09-14"))
        g=s["groups"][0]
        self.assertEqual(self.member("A01","11","a"),g["representative"])
        self.assertEqual(20,g["sales"])  # ten singles plus ten double-packs, NOT thirty
        self.assertEqual((1,1,2),(g["shop_count"],g["product_count"],g["sku_count"]))
        a=next(m for m in s["members"] if m["sku_id"]=="a")
        z=next(m for m in s["members"] if m["sku_id"]=="z")
        self.assertEqual(("missing",None),(a["image_source"],a["image_data"]))
        self.assertEqual("套装 · 单个装",a["display_name"])
        self.assertEqual("sku",z["image_source"])
        self.assertTrue(z["image_data"].startswith("data:image/png;base64,"))
        self.service.confirm_groups(s["id"],[g["id"]]);self.service.save_and_view(s["id"])
        for _ in range(2):
            restored=AnalysisService(self.service.config,running=lambda:False).get(s["id"])
            self.assertEqual(g["representative"],restored["groups"][0]["representative"])

    def test_single_sku_title_and_same_observation_main_fallback(self):
        main=product_picture("blue")
        for day,stock in [("2026-09-07",100),("2026-09-14",80)]:
            self.observe(day,[dict(sku_id="s",sku_name="原名",sku_stock=stock,observation_complete=True)],title="单品",image=main)
        s=self.service.start("2026-09-07","2026-09-14")
        m=s["members"][0]
        self.assertEqual("单品",m["display_name"])
        self.assertEqual("原名",m["raw_sku_name"])
        self.assertEqual(("product_main_same_observation",main["hash"]),(m["image_source"],m["image_hash"]))
        self.assertTrue(m["image_data"].startswith("data:image/png;base64,"))

    def test_group_keeps_inventory_shape_switch_reset_and_inactive_boundaries(self):
        days=[(8,"default",90),(9,"s",50),(10,"s",45),(11,"default",80),(12,"default",70),(13,"s",60),(14,"s",50)]
        for day,sku,stock in days:
            self.observe(f"2026-09-{day:02}",[dict(sku_id=sku,sku_name=sku,sku_stock=stock,observation_complete=True)])
        s=self.merge_all(self.service.start("2026-09-07","2026-09-14"))
        self.assertEqual(25,s["groups"][0]["sales"])
        self.assertEqual([0,0,0,5,0,10,0,10],[p["sales"] for p in s["groups"][0]["points"]])
        self.assertEqual([90,90,50,45,80,70,60,50],[p["stock"] for p in s["groups"][0]["points"]])
        self.assertEqual(["2026-09-09","2026-09-11","2026-09-13"],[p["date"] for p in s["groups"][0]["points"] if p.get("segment_start")])

    def test_old_saved_draft_restores_boundaries_without_live_inventory(self):
        for day, sku, stock in [(8, "default", 90), (9, "s", 50), (10, "s", 45)]:
            self.observe(f"2026-09-{day:02}", [dict(sku_id=sku, sku_name=sku,
                sku_stock=stock, observation_complete=True)])
        snapshot = self.merge_all(self.service.start("2026-09-07", "2026-09-14"))
        self.service.confirm_groups(snapshot["id"], [snapshot["groups"][0]["id"]])
        snapshot = self.service.save_and_view(snapshot["id"])
        # Public store fixture emulates the earlier locally saved SKU format.
        for member in snapshot["members"]:
            member.pop("shape_switches", None)
            for point in member["points"]:
                point.pop("segment_start", None)
        self.service.store.write_sku(snapshot)
        self.conn.close()
        self.path.unlink()
        restored = AnalysisService(self.service.config, running=lambda: False).get(snapshot["id"])
        self.assertEqual(5, restored["groups"][0]["sales"])
        self.assertEqual(["2026-09-09"], [point["date"] for point in
            restored["groups"][0]["points"] if point.get("segment_start")])
        self.assertEqual(snapshot["groups"][0]["representative"], restored["groups"][0]["representative"])

class SkuRankingBrowserTests(unittest.TestCase):
    observe=SkuRankingServiceTests.observe
    member=staticmethod(SkuRankingServiceTests.member)
    group_for=classmethod(SkuRankingServiceTests.group_for.__func__)
    contamination=SkuRankingServiceTests.contamination

    def setUp(self):
        from playwright.sync_api import sync_playwright
        from test_analysis import serve
        SkuRankingServiceTests.setUp(self)
        self.server,self.thread=serve(self.service)
        def stop():
            self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.addCleanup(stop)
        pw=sync_playwright().start(); self.addCleanup(pw.stop)
        self.browser=pw.chromium.launch(channel="msedge",headless=True); self.addCleanup(self.browser.close)
        self.page=self.browser.new_page()
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}")

    def open_snapshot(self,s):
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#analysis="+s["id"])
        self.page.reload()

    def retain(self,label,report=None,offline=None):
        target=os.environ.get("SKU03_ARTIFACTS")
        if target:
            directory=Path(target);directory.mkdir(parents=True,exist_ok=True)
            self.page.screenshot(path=str(directory/(label+"-online.png")),full_page=True)
            if report:
                shutil.copyfile(report,directory/(label+"-report.html"))
            if offline:
                offline.screenshot(path=str(directory/(label+"-offline.png")),full_page=True)

    def test_multi_page_zero_filter_and_offline_full_export(self):
        from playwright.sync_api import expect
        for offer in range(23):
            for day,stock in [("2026-09-07",100),("2026-09-14",100-offer)]:
                self.observe(day,[dict(sku_id="s",sku_name="原始",sku_stock=stock,observation_complete=True)],offer=str(offer),title=f"商品{offer}")
        s=self.service.start("2026-09-07","2026-09-14")
        self.service.confirm_groups(s["id"],[g["id"] for g in s["groups"]]);s=self.service.save_and_view(s["id"])
        self.open_snapshot(s)
        expect(self.page.locator("#results")).to_be_visible()
        expect(self.page.locator("#ranking > details")).to_have_count(20)
        self.page.locator("#rankNext").click()
        expect(self.page.locator("#ranking > details")).to_have_count(2)
        expect(self.page.locator("#ranking > details").first.locator("summary")).to_contain_text("21")
        self.page.locator("#showZeroGroups").check()
        expect(self.page.locator("#rankPageInfo")).to_contain_text("第 1 / 2 屏 · 共 23 组")
        self.page.locator("#rankNext").click()
        expect(self.page.locator("#ranking > details")).to_have_count(3)
        self.page.locator("#showZeroGroups").uncheck()
        self.page.locator("#rankNext").click()  # export from page TWO with zeros hidden
        self.page.locator("#exportReport").click()
        expect(self.page.locator("#exportStatus")).to_contain_text("已导出")
        report=next(self.service.config.output.glob("*.html"))
        context=self.browser.new_context(offline=True)
        self.addCleanup(context.close)
        offline=context.new_page(); requests=[]
        offline.on("request",lambda request:requests.append(request.url))
        offline.goto(report.as_uri())
        expect(offline.locator(".rank")).to_have_count(23)
        expect(offline.locator(".sku-row")).to_have_count(23)
        expect(offline.locator(".rank").last.locator("summary").first).to_contain_text("0 组销量")
        offline.locator(".rank").last.locator("summary").first.click()
        offline.get_by_role("button",name="展开全部").last.click()
        expect(offline.locator(".rank").last.locator(".sku-row")).to_have_attribute("open","")
        self.assertFalse([url for url in requests if url.startswith("http")])
        self.retain("pagination",report,offline)
        self.page.locator("#backToReview").click()
        expect(self.page.locator("#reviewPage")).to_be_visible()
        self.page.locator("#saveAndView").click()
        expect(self.page.locator("#rankPageInfo")).to_contain_text("第 1 / 2 屏")

    def test_images_titles_sources_and_missing_representative_survive_offline(self):
        from playwright.sync_api import expect
        own=product_picture("red");main=product_picture("blue")
        for day,stock in [("2026-09-07",100),("2026-09-14",90)]:
            self.observe(day,[dict(sku_id="a",sku_name="单个装",sku_stock=stock,observation_complete=True),
                dict(sku_id="z",sku_name="双件套",sku_stock=stock,observation_complete=True,
                    sku_image_evidence=dict(own,source=SKU_IMAGE_OWN))],title="套装")
        for day,stock in [("2026-09-07",100),("2026-09-14",85)]:
            self.observe(day,[dict(sku_id="s",sku_name="原始名称",sku_stock=stock,observation_complete=True)],offer="33",title="单品",image=main)
        s=self.service.start("2026-09-07","2026-09-14")
        a=self.member("A01","11","a");z=self.member("A01","11","z")
        s=self.service.edit_group(s["id"],"move",self.group_for(s,z)["id"],z,self.group_for(s,a)["id"])
        group=self.group_for(s,a)
        self.open_snapshot(s)
        expect(self.page.locator("#groupDetail .sku-group-summary")).to_contain_text("套装 · 单个装")
        expect(self.page.locator("#groupDetail .sku-group-summary")).to_contain_text("当次缺图")
        expect(self.page.locator("#groupDetail .sku-group-summary img")).to_have_count(0)
        self.service.confirm_groups(s["id"],[g["id"] for g in s["groups"]])
        self.page.reload()
        self.page.locator("#saveAndView").click()
        row=self.page.locator(f'#ranking > details[data-group="{group["id"]}"]')
        expect(row.locator("summary").first).to_contain_text("套装 · 单个装")
        expect(row.locator("summary").first.locator("img")).to_have_count(0)
        row.locator("summary").first.click()
        zrow=row.locator('[data-sku-id="z"]')
        expect(zrow.locator("img")).to_have_count(1)
        expect(zrow).to_contain_text("SKU 专图")
        second=self.page.locator("#ranking > details").nth(1)
        expect(second.locator("summary").first).to_contain_text("同次商品主图代填")
        expect(second.locator("summary").first).not_to_contain_text("原始名称")
        self.page.context.route("https://offer.test/**",lambda route:route.fulfill(status=200,body="source"))
        with self.page.expect_popup() as popup:
            row.locator("summary").first.get_by_role("link",name="商品源地址").click()
        popup.value.wait_for_load_state()
        self.assertEqual("https://offer.test/11",popup.value.url)
        popup.value.close()
        self.page.locator("#exportReport").click()
        expect(self.page.locator("#exportStatus")).to_contain_text("已导出")
        report=next(self.service.config.output.glob("*.html"))
        context=self.browser.new_context(offline=True);self.addCleanup(context.close)
        offline=context.new_page();offline.goto(report.as_uri())
        offline_row=offline.locator(f'details[data-group="{group["id"]}"]')
        expect(offline_row.locator("summary").first.locator("img")).to_have_count(0)
        expect(offline_row.locator("summary").first).to_contain_text("当次缺图")
        offline_row.locator("summary").first.click()
        expect(offline_row.locator('[data-sku-id="z"] img')).to_have_count(1)
        self.assertEqual(zrow.locator("img").get_attribute("src"),offline_row.locator('[data-sku-id="z"] img').get_attribute("src"))
        expect(offline.locator(".rank").nth(1).locator("summary").first).to_contain_text("同次商品主图代填")
        self.assertEqual("https://offer.test/11",offline_row.locator("summary").first.locator("a").get_attribute("href"))
        self.retain("images",report,offline)

    def test_real_confirmation_results_and_export_share_sku_representative_and_tree(self):
        from playwright.sync_api import expect
        s,a,b=self.contamination()
        g=self.group_for(s,a)
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#analysis="+s["id"])
        self.page.reload()
        expect(self.page.locator("#products [data-sku-group]").first).to_contain_text("乙")
        expect(self.page.locator("#groupDetail .sku-group-summary")).to_contain_text("组销量 14")
        self.assertEqual("https://offer.test/22",self.page.locator("#groupDetail .sku-group-summary a").get_attribute("href"))
        self.page.locator("#groupDetail").get_by_role("button",name="确认当前分组",exact=True).click()
        self.page.locator("#products [data-sku-group]").nth(1).click()
        self.page.locator("#groupDetail").get_by_role("button",name="确认当前分组",exact=True).click()
        self.page.locator("#saveAndView").click()
        expect(self.page.locator("#results")).to_be_visible()
        row=self.page.locator(f'#ranking > details[data-group="{g["id"]}"]')
        expect(row.locator("summary").first).to_contain_text("组销量")
        expect(row.locator("summary").first).to_contain_text("14")
        row.locator("summary").first.click()
        expect(row.locator(".sku-row")).to_have_count(2)
        expect(row).to_contain_text("本组内销量小计 5")
        expect(row).not_to_contain_text("组外")
        self.assertEqual("36",row.locator('.chart-block').first.locator('.stock-point').last.get_attribute('data-stock'))
        self.page.locator("#exportReport").click()
        expect(self.page.locator("#exportStatus")).to_contain_text("已导出")
        report=next(self.service.config.output.glob("*.html"))
        offline=self.browser.new_page()
        offline.context.set_offline(True)
        offline.goto(report.as_uri())
        expect(offline.locator(".rank")).to_have_count(2)
        offline_row=offline.locator(f'details[data-group="{g["id"]}"]')
        expect(offline_row.locator("summary").first).to_contain_text("乙")
        offline_row.locator("summary").first.click()
        expect(offline_row.locator(".sku-row")).to_have_count(2)
        expect(offline_row).to_contain_text("本组内销量小计 5")
        expect(offline_row).not_to_contain_text("组外")
        expect(offline.locator("body")).to_contain_text("并非统一实物数量")
        self.retain("contamination",report,offline)
        offline.context.set_offline(False)
        self.page.locator("#backToReview").click()
        expect(self.page.locator("#reviewPage")).to_be_visible()
        self.page.reload()
        expect(self.page.locator("#results")).to_be_visible()

if __name__ == "__main__":
    unittest.main()
