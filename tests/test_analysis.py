"""真实浏览器 → 本地 HTTP → 临时库存库，不替换分析接口。"""
import tempfile
import threading
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from bestseller_monitor.db import connect, Database
from helpers import new_round
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService, calculate_inventory
from bestseller_monitor.analysis_http import create_server

class AnalysisBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inventory.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        for i in range(1, 13):
            self.conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES (?,?)", (f"A{i:02}", f"店铺{i}"))
        self.conn.commit()
        self.submit("2026-09-07", 100)
        self.submit("2026-09-14", 80)
        self.running = False
        self.service = AnalysisService(AnalysisConfig(self.path), running=lambda: self.running)
        self.server = create_server(self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.browser = self.pw.chromium.launch(channel="msedge", headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}")

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def submit(self, day, stock):
        rid = new_round(self.db, "A01", run_date=day)
        self.db.submit_inventory_snapshot(round_id=rid, shop_key="A01", shop_url="https://shop.example",
            shop_name="店铺1", offer_id="11", product_url="https://detail.1688.com/offer/11.html",
            list_title="杯子", detail_title="杯子", main_image_url="", sku_rows=[
                {"sku_id":"red", "sku_name":"红色", "sku_stock":stock}],
            collected_at=day+"T04:00:00+00:00", attempt=1)

    def dates(self):
        self.page.get_by_label("开始日期", exact=True).fill("2026-09-07")
        self.page.get_by_label("结束日期", exact=True).fill("2026-09-14")

    def test_select_dates_freeze_and_keep_data_while_source_changes(self):
        self.dates()
        expect(self.page.get_by_role("table").first.get_by_role("row")).to_have_count(13)
        expect(self.page.get_by_role("table").first).to_contain_text("店铺12")
        self.running = True
        self.page.get_by_role("button", name="下一步、进入同款确认").click()
        expect(self.page.get_by_role("dialog")).to_contain_text("当前存在运行中的库存抓取程序")
        self.page.get_by_role("button", name="继续").click()
        expect(self.page.get_by_role("heading", name="确认同款")).to_be_visible()
        expect(self.page.get_by_text("待确认", exact=True)).to_be_visible()
        expect(self.page.get_by_role("table")).to_contain_text("80")
        self.submit("2026-09-14", 55)
        self.page.reload()
        expect(self.page.get_by_role("table")).to_contain_text("80")
        self.page.get_by_role("button", name="重新选择日期").click()
        self.dates()
        self.running = False
        self.page.get_by_role("button", name="下一步、进入同款确认").click()
        expect(self.page.get_by_role("heading", name="确认同款")).to_be_visible()
        expect(self.page.get_by_role("table")).to_contain_text("55")

    def test_invalid_empty_and_failed_reads_can_retry_without_partial_analysis(self):
        self.page.get_by_role("button", name="下一步、进入同款确认").click()
        expect(self.page.get_by_role("alert")).to_contain_text("结束日期必须晚于开始日期")
        self.page.get_by_label("开始日期", exact=True).fill("2025-09-01")
        self.page.get_by_label("结束日期", exact=True).fill("2025-09-08")
        self.page.get_by_role("button", name="下一步、进入同款确认").click()
        expect(self.page.get_by_role("alert")).to_contain_text("没有可分析的库存")
        expect(self.page.get_by_role("heading", name="选择销量计算区间")).to_be_visible()
        self.service.config = AnalysisConfig(Path(self.tmp.name)/"missing.db")
        self.dates()
        self.page.get_by_role("button", name="下一步、进入同款确认").click()
        expect(self.page.get_by_role("alert")).to_contain_text("读取库存数据失败")
        self.service.config = AnalysisConfig(self.path)
        self.page.get_by_role("button", name="下一步、进入同款确认").click()
        expect(self.page.get_by_role("heading", name="确认同款")).to_be_visible()

    def test_weekday_configuration_and_paused_round(self):
        config = Path(self.tmp.name)/"analysis.toml"
        config.write_text('[analysis]\ndatabase = "inventory.db"\nfull_capture_weekday = 2\n', encoding="utf-8")
        self.service.config = AnalysisConfig.from_file(config)
        self.page.reload()
        self.page.get_by_label("开始日期", exact=True).fill("2026-09-07")
        expect(self.page.get_by_role("dialog")).to_contain_text("只有每周二会全量抓取库存数据")
        self.page.get_by_role("button", name="继续").click()
        self.page.get_by_label("结束日期", exact=True).fill("2026-09-15")
        expect(self.page.get_by_role("table").nth(1).get_by_role("row")).to_have_count(13)
        self.page.get_by_role("button", name="下一步、进入同款确认").click()
        # Fixture rounds remain ongoing, but no collector process exists.
        expect(self.page.get_by_role("heading", name="确认同款")).to_be_visible()
        expect(self.page.get_by_role("dialog")).not_to_be_visible()

    def test_snapshot_includes_prior_baselines_and_all_sku_identities(self):
        self.submit("2026-09-06", 120)
        result = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual([r["stock"] for r in result["inventory"]], [120, 100, 80])
        result["inventory"][0]["stock"] = -1
        self.assertEqual(self.service.get(result["id"])["inventory"][0]["stock"], 120)
        import json
        json.dumps(self.service.get(result["id"]))

    def test_large_product_list_is_paged_without_rendering_all_inventory(self):
        for offer in range(20, 42):
            rid = new_round(self.db, "A01", run_date="2026-09-14")
            self.db.submit_inventory_snapshot(round_id=rid, shop_key="A01", shop_url="https://shop.example",
                shop_name="店铺1", offer_id=str(offer), product_url=f"https://detail.1688.com/offer/{offer}.html",
                list_title=f"商品{offer}", detail_title=f"商品{offer}", main_image_url="",
                sku_rows=[{"sku_id":"one", "sku_name":"标准", "sku_stock":offer}],
                collected_at="2026-09-14T04:00:00+00:00", attempt=1)
        self.dates()
        self.page.get_by_role("button", name="下一步、进入同款确认").click()
        expect(self.page.get_by_role("article")).to_have_count(20)
        self.page.get_by_role("button", name="下一页").click()
        expect(self.page.get_by_role("article")).to_have_count(3)

    def test_confirmation_unlocks_initial_ranking_and_sku_details(self):
        self.dates()
        self.page.get_by_role("button", name="下一步、进入同款确认").click()
        expect(self.page.get_by_role("heading", name="初步畅销品")).not_to_be_visible()
        self.page.get_by_role("button", name="确认当前分组").click()
        expect(self.page.get_by_role("heading", name="初步畅销品")).to_be_visible()
        self.page.get_by_text("01 杯子 · 20 销量", exact=True).click()
        expect(self.page.get_by_text("红色 · 销量 20", exact=True)).to_be_visible()

    def test_switching_inventory_through_database_and_browser(self):
        for day, values in [(7, [('default', 100)]), (8, [('default', 90)]),
                            (10, [('white', 40), ('cream', 60)]),
                            (14, [('white', 30), ('cream', 50)])]:
            stamp = f'2026-09-{day:02}'
            rid = new_round(self.db, 'A01', run_date=stamp)
            self.db.submit_inventory_snapshot(
                round_id=rid, shop_key='A01', shop_url='https://shop.example',
                shop_name='店铺1', offer_id='22', product_url='https://detail.1688.com/offer/22.html',
                list_title='切换商品', detail_title='切换商品', main_image_url='',
                sku_rows=[dict(sku_id=sku, sku_name=sku, sku_stock=stock) for sku, stock in values],
                collected_at=stamp+'T04:00:00+00:00', attempt=1)
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.get_by_role('button', name='确认当前分组', exact=True)).to_have_count(2)
        for remaining in (1, 0):
            self.page.get_by_role('button', name='确认当前分组', exact=True).first.click()
            expect(self.page.get_by_role('button', name='确认当前分组', exact=True)).to_have_count(remaining)
        self.page.locator('#ranking > details > summary').first.click()
        expect(self.page.locator('#ranking > details').first).to_contain_text('切换商品')
        expect(self.page.locator('#ranking > details > summary').first).to_contain_text('30')
        expect(self.page.locator('#ranking .inventory-chart').first).to_be_visible()


class InventoryCalculationTests(unittest.TestCase):
    def test_representation_segments_do_not_bridge_switches(self):
        observations = [(7, 'default', 100), (8, 'default', 90),
                        (10, 'white', 40), (10, 'cream', 60),
                        (14, 'white', 30), (14, 'cream', 50)]
        rows = [dict(shop_key='A', offer_id='P', sku_id=sku,
                     date=f'2026-09-{day:02}', stock=stock)
                for day, sku, stock in observations]
        result = calculate_inventory(rows, '2026-09-07', '2026-09-14')[('A', 'P')]
        self.assertEqual(result['sales'], 30)
        self.assertEqual(result['points'][3]['stock'], 100)
        self.assertIsNone(result['skus'][0]['points'][3]['stock'])
        rows.extend([dict(shop_key='A', offer_id='P', sku_id='default',
                          date='2026-09-15', stock=20),
                     dict(shop_key='A', offer_id='P', sku_id='default',
                          date='2026-09-16', stock=0)])
        result = calculate_inventory(rows, '2026-09-07', '2026-09-16')[('A', 'P')]
        self.assertEqual(result['sales'], 50)
        self.assertEqual(result['points'][-1]['stock'], 0)
        self.assertEqual(result['points'][-2]['sales'], 0)

    def test_sku_first_calculation_handles_baselines_missing_days_restock_and_multi_sku(self):
        rows = [
            {"shop_key": "A", "offer_id": "P", "sku_id": "down", "sku_name": "下降", "date": "2026-09-06", "stock": 120},
            {"shop_key": "A", "offer_id": "P", "sku_id": "down", "sku_name": "下降", "date": "2026-09-07", "stock": 100},
            {"shop_key": "A", "offer_id": "P", "sku_id": "down", "sku_name": "下降", "date": "2026-09-14", "stock": 80},
            {"shop_key": "A", "offer_id": "P", "sku_id": "restock", "sku_name": "补货", "date": "2026-09-07", "stock": 50},
            {"shop_key": "A", "offer_id": "P", "sku_id": "restock", "sku_name": "补货", "date": "2026-09-08", "stock": 90},
            {"shop_key": "A", "offer_id": "Q", "sku_id": "future", "sku_name": "未来基准", "date": "2026-09-14", "stock": 30},
        ]
        result = calculate_inventory(rows, "2026-09-07", "2026-09-14")
        product = result[("A", "P")]
        self.assertEqual(product["sales"], 20)
        down = next(s for s in product["skus"] if s["sku_id"] == "down")
        self.assertEqual(down["points"][0]["sales"], 0)
        self.assertEqual(down["points"][1]["stock"], 100)
        self.assertEqual(down["points"][1]["color"], "yellow")
        self.assertEqual(down["points"][-1]["sales"], 20)
        restock = next(s for s in product["skus"] if s["sku_id"] == "restock")
        self.assertEqual(restock["points"][1]["color"], "red")
        self.assertEqual(restock["points"][1]["sales"], 0)
        self.assertEqual(result[("A", "Q")]["skus"][0]["points"][0]["stock"], 30)

    def test_product_sales_sum_skus_instead_of_diffing_total_stock(self):
        rows = [
            {"shop_key": "A", "offer_id": "P", "sku_id": "a", "date": "2026-09-07", "stock": 100},
            {"shop_key": "A", "offer_id": "P", "sku_id": "a", "date": "2026-09-08", "stock": 80},
            {"shop_key": "A", "offer_id": "P", "sku_id": "b", "date": "2026-09-07", "stock": 50},
            {"shop_key": "A", "offer_id": "P", "sku_id": "b", "date": "2026-09-08", "stock": 90},
        ]
        result = calculate_inventory(rows, "2026-09-07", "2026-09-08")
        self.assertEqual(result[("A", "P")]["sales"], 20)
