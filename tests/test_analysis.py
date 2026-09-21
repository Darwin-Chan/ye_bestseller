"""真实浏览器 → 本地 HTTP → 临时库存库，不替换分析接口。"""
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from bestseller_monitor.db import connect, Database
from helpers import new_round, submit_offer
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService, calculate_inventory
from bestseller_monitor.analysis_http import create_server


def serve(service):
    """起一个本地分析服务的 HTTP 线程，返回 (server, thread)。

    夹具以按名别名 setUp 的方式被别的用例文件复用，所以这里用模块级函数，
    不占夹具方法名。
    """
    server = create_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


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
        self.server, self.thread = serve(self.service)
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

    def restart_service(self):
        """销毁旧服务与它的 HTTP 进程，再用同一份磁盘文件重新启动。"""
        self.stop_server()
        self.service = AnalysisService(self.service.config, running=lambda: self.running)
        self.server, self.thread = serve(self.service)
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}")

    def choose_group(self, text):
        self.page.locator(".group-choice", has_text=text).first.click()

    def submit_product(self, offer, day, stock, *, name, color=None):
        submit_offer(self.db, offer, day, stock, name=name, color=color)

    def add_product(self, offer, name, first, second, *, color=None):
        self.submit_product(offer, "2026-09-07", first, name=name, color=color)
        self.submit_product(offer, "2026-09-14", second, name=name, color=color)

    def submit(self, day, stock, *, name='杯子', image_url='', image_evidence=None):
        submit_offer(self.db, "11", day, stock, name=name, image_url=image_url, image_evidence=image_evidence)

    def test_historical_images_survive_source_failure_and_keep_names_paired(self):
        import io
        import struct
        import zlib
        from unittest.mock import patch
        from bestseller_monitor.product_images import acquire

        def png(rgb):
            def chunk(kind, data):
                return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data))
            return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
                    +chunk(b'IDAT', zlib.compress(b'\0'+rgb))+chunk(b'IEND', b''))

        url = 'https://images.example/current.png'
        for day, name, color in [('2026-09-07', '旧款杯子', b'\xff\0\0'),
                                 ('2026-09-14', '新款杯子', b'\0\xff\0')]:
            with patch('bestseller_monitor.product_images.urlopen', return_value=io.BytesIO(png(color))):
                self.submit(day, 100, name=name, image_url=url, image_evidence=acquire(url))
        old = self.service.start('2026-09-06', '2026-09-07')['products'][0]
        new = self.service.start('2026-09-07', '2026-09-14')['products'][0]
        self.assertNotEqual(old['image_hash'], new['image_hash'])
        self.assertEqual(old['product_name'], '旧款杯子')
        self.assertEqual(new['product_name'], '新款杯子')
        with patch('bestseller_monitor.product_images.urlopen', side_effect=OSError('offline')):
            self.page.get_by_label('开始日期', exact=True).fill('2026-08-31')
            self.page.get_by_label('结束日期', exact=True).fill('2026-09-07')
            self.page.get_by_role('button', name='下一步、进入同款确认').click()
            expect(self.page.get_by_role('heading', name='旧款杯子')).to_be_visible()
            self.page.get_by_role('button', name='放大商品图片').click()
            expect(self.page.get_by_role('dialog', name='商品图片')).to_be_visible()
            self.assertTrue(self.page.locator('#largeImage').evaluate('(img)=>img.complete && img.naturalWidth===1'))
            self.assertEqual(self.page.locator('#largeImage').get_attribute('src'), old['image_data'])
            self.page.get_by_role('button', name='关闭大图').click()
            source = self.page.get_by_role('link', name='商品源地址')
            expect(source).to_have_attribute('target', '_blank')
            expect(source).to_have_attribute('href', 'https://detail.1688.com/offer/11.html')
            self.conn.execute("UPDATE products SET product_url='https://detail.1688.com/offer/11.html?current=1' WHERE offer_id='11'")
            self.conn.commit()
            self.page.context.route('https://detail.1688.com/**', lambda route: route.fulfill(body='current source'))
            with self.page.expect_popup() as opened:
                source.click()
            opened.value.wait_for_url('**/11.html?current=1')
            opened.value.close()
            self.page.get_by_role('button', name='重新选择日期').click()
            self.dates()
            self.page.get_by_role('button', name='下一步、进入同款确认').click()
            expect(self.page.get_by_role('heading', name='新款杯子')).to_be_visible()
            self.assertEqual(self.page.get_by_role('img', name='新款杯子').get_attribute('src'), new['image_data'])
            failure = acquire(url)
        self.submit('2026-09-15', 80, name='失败版本', image_url=url, image_evidence=failure)
        failed = self.service.start('2026-09-07', '2026-09-15')['products'][0]
        self.assertIsNone(failed['image_data'])
        self.assertIn('offline', failed['image_error'])
        with patch('bestseller_monitor.product_images.urlopen', return_value=io.BytesIO(png(b'\0\xff\0'))):
            self.submit('2026-09-16', 70, name='重试版本', image_url=url+'?new', image_evidence=acquire(url+'?new'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM product_image_assets').fetchone()[0], 2)
        self.submit('2026-09-17', 60, image_url=url, image_evidence={'content': b'\x89PNG\r\n\x1a\ninvalid'})
        self.assertIsNone(self.service.start('2026-09-07', '2026-09-17')['products'][0]['image_data'])
        failed_id = self.conn.execute('SELECT MAX(id) FROM product_information_versions').fetchone()[0]
        inventories = [tuple(r) for r in self.conn.execute('SELECT * FROM inventory')]
        with patch('bestseller_monitor.product_images.urlopen', return_value=io.BytesIO(png(b'\0\xff\0'))), patch('bestseller_monitor.db.utcnow', return_value='2026-09-18T04:00:00+00:00'):
            retried = self.db.retry_product_image(failed_id)
        self.assertIsNone(retried['image_error'])
        self.assertEqual(inventories, [tuple(r) for r in self.conn.execute('SELECT * FROM inventory')])
        self.assertIsNone(self.service.start('2026-09-07', '2026-09-17')['products'][0]['image_data'])
        self.assertEqual(self.service.start('2026-09-07', '2026-09-18')['products'][0]['image_hash'], new['image_hash'])
        incomplete = self.service.start('2026-09-07', '2026-09-18')['products'][0]
        self.assertIsNone(incomplete['product_name'])
        self.assertFalse(incomplete['information_complete'])
        self.assertIn('名称待重新观测', incomplete['information_note'])
        with patch('bestseller_monitor.product_images.urlopen', side_effect=AssertionError('opening must not fetch')):
            for _ in range(2):
                reopened = connect(self.path)
                self.assertEqual(reopened.execute('SELECT COUNT(*) FROM product_image_assets').fetchone()[0], 2)
                reopened.close()

    def test_legacy_inventory_never_borrows_current_image(self):
        self.conn.execute('DELETE FROM product_information_versions')
        self.conn.execute("UPDATE products SET main_image_url='https://images.example/current.png'")
        self.conn.execute("UPDATE products SET product_url=''")
        self.conn.commit()
        product = self.service.start('2026-09-07', '2026-09-14')['products'][0]
        self.assertIsNone(product['image_data'])
        self.assertEqual(product['image_error'], '该日期没有历史图片')
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.get_by_text('历史图片缺失', exact=True)).to_be_visible()
        expect(self.page.get_by_role('button', name='放大商品图片')).to_have_count(0)
        expect(self.page.get_by_role('link', name='商品源地址')).to_have_count(0)
        self.conn.execute("UPDATE products SET product_url='https://detail.1688.com/offer/11.html'")
        self.conn.commit()
        self.page.reload()
        expect(self.page.get_by_role('link', name='商品源地址')).to_have_attribute('href', 'https://detail.1688.com/offer/11.html')

    def test_truncated_jpeg_is_missing_evidence_not_failed_inventory(self):
        import io
        from PIL import Image
        content = io.BytesIO()
        Image.new('RGB', (12, 12), 'red').save(content, format='JPEG')
        self.submit('2026-09-14', 42, image_evidence={'content': content.getvalue()[:-2]})
        product = self.service.start('2026-09-07', '2026-09-14')['products'][0]
        self.assertEqual(product['points'][-1]['stock'], 42)
        self.assertIsNone(product['image_data'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM product_image_assets').fetchone()[0], 0)

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
        expect(self.page.locator("#groupDetail .group-status")).to_have_text("待确认")
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
        expect(self.page.locator(".group-choice")).to_have_count(20)
        self.page.get_by_role("button", name="下一页").click()
        expect(self.page.locator(".group-choice")).to_have_count(3)

    def test_confirmation_unlocks_initial_ranking_and_sku_details(self):
        self.dates()
        self.page.get_by_role("button", name="下一步、进入同款确认").click()
        expect(self.page.get_by_role("heading", name="初步畅销品")).not_to_be_visible()
        self.page.get_by_role("button", name="确认当前分组").click()
        expect(self.page.get_by_role("heading", name="初步畅销品")).not_to_be_visible()
        self.page.get_by_role("button", name="保存分组并查看畅销品").click()
        expect(self.page.get_by_role("heading", name="初步畅销品")).to_be_visible()
        rank = self.page.locator('#ranking > details').first
        expect(rank.locator(':scope > summary')).to_contain_text('01')
        expect(rank.locator(':scope > summary')).to_contain_text('杯子')
        expect(rank.locator(':scope > summary .number')).to_have_text('20 销量')
        expect(self.page.get_by_text('点击展开')).to_have_count(0)
        rank.locator(':scope > summary').click()
        expect(rank.locator('.chart-block[data-sku="false"] .inventory-chart')).to_be_visible()
        sku = rank.locator('.sku-row').filter(has_text='红色')
        expect(sku.locator('summary')).to_contain_text('销量 20')
        sku.locator('summary').click()
        expect(sku.locator('.inventory-chart')).to_be_visible()

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
        expect(self.page.locator('.group-choice')).to_have_count(2)
        for index in range(2):
            self.page.locator('.group-choice').nth(index).click()
            self.page.get_by_role('button', name='确认当前分组', exact=True).click()
            expect(self.page.get_by_role('button', name='已确认', exact=True)).to_be_disabled()
        self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        self.page.locator('#ranking > details > summary').first.click()
        expect(self.page.locator('#ranking > details').first).to_contain_text('切换商品')
        expect(self.page.locator('#ranking > details > summary').first).to_contain_text('30')
        expect(self.page.locator('#ranking .inventory-chart').first).to_be_visible()
        rank = self.page.locator('#ranking > details').first
        chart = rank.locator('.inventory-chart').first
        expect(chart.locator('.stock-line')).to_have_count(6)  # 切换日不连线
        expect(chart.locator('.sales-line')).to_have_count(7)
        default_chart = rank.locator('details .inventory-chart').first
        expect(default_chart.locator('circle[data-stock]')).to_have_count(3)
        expect(default_chart.locator('circle[data-date="2026-09-10"]')).to_have_count(0)
        for day, stock in [(15, 20), (16, 0)]:
            stamp = f'2026-09-{day}'
            rid = new_round(self.db, 'A01', run_date=stamp)
            self.db.submit_inventory_snapshot(
                round_id=rid, shop_key='A01', shop_url='https://shop.example',
                shop_name='店铺1', offer_id='22', product_url='https://detail.1688.com/offer/22.html',
                list_title='切换商品', detail_title='切换商品', main_image_url='',
                sku_rows=[dict(sku_id='default', sku_name='default', sku_stock=stock)],
                collected_at=stamp+'T04:00:00+00:00', attempt=1)
        # Existing analysis remains frozen after source changes.
        self.page.reload()
        expect(self.page.locator('#ranking > details > summary').first).to_contain_text('30')
        self.page.get_by_role('button', name='重新选择日期').click()
        self.dates()
        self.page.get_by_label('结束日期', exact=True).fill('2026-09-16')
        self.page.get_by_role('button', name='继续', exact=True).click()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('.group-choice')).to_have_count(2)
        for index in range(2):
            self.page.locator('.group-choice').nth(index).click()
            self.page.get_by_role('button', name='确认当前分组', exact=True).click()
            expect(self.page.get_by_role('button', name='已确认', exact=True)).to_be_disabled()
        self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        self.page.locator('#ranking > details > summary').first.click()
        expect(self.page.locator('#ranking > details > summary').first).to_contain_text('50')
        final_chart = self.page.locator('#ranking > details').first.locator('details .inventory-chart').last
        expect(final_chart.locator('circle[data-stock]')).to_have_count(2)
        expect(final_chart.locator('circle[data-stock="0"]')).to_have_count(1)


    def test_save_and_restart_resumes_the_same_frozen_analysis(self):
        self.add_product('22', '云朵杯', 60, 50, color='red')
        self.add_product('33', '树叶杯', 40, 30)
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.get_by_role('heading', name='确认同款')).to_be_visible()
        sid = self.page.url.split('analysis=')[1]

        # 部分确认：只确认杯子所在组。
        self.choose_group('G1')
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        expect(self.page.get_by_role('button', name='已确认', exact=True)).to_be_disabled()
        # 人工增：把树叶杯加入云朵杯所在组；人工减：再移出并留下排除关系。
        self.choose_group('云朵杯')
        self.page.get_by_role('button', name='组内新增商品').click()
        self.page.get_by_role('textbox', name='搜索商品', exact=True).fill('树叶杯')
        self.page.locator('#addResults').get_by_role('button', name='添加到当前组').click()
        expect(self.page.locator('#groupDetail .matching-member')).to_have_count(2)
        self.page.locator('#groupDetail .matching-member[data-product="33"] .remove-member').click()
        self.page.get_by_role('button', name='确认移除').click()
        expect(self.page.locator('#groupDetail .matching-member')).to_have_count(1)
        # 撤回：树叶杯的新组先确认再撤回。
        self.choose_group('树叶杯')
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        self.page.get_by_role('tab', name='已确认', exact=True).click()
        self.choose_group('树叶杯')
        self.page.get_by_role('button', name='撤回当前分组').click()
        self.page.get_by_role('tab', name='全部', exact=True).click()

        # 暂存：留在当前页，显示成功时间。
        self.page.get_by_role('button', name='暂时保存').click()
        expect(self.page.locator('#saveStatus')).to_have_text(re.compile(r'^已保存 · \d{2}-\d{2} \d{2}:\d{2}$'))
        expect(self.page.locator('#dirtyStatus')).to_have_text('')
        before = self.service.get(sid)
        self.assertEqual(len(before['excluded']), 1)
        frozen_image = next(p for p in before['products'] if p['offer_id'] == '22')['image_data']

        # 保存之后源库变了：结束日重报云朵杯的新名称、新图片与新库存。
        self.submit_product('22', '2026-09-14', 20, name='改名后的云朵杯', color='blue')

        # 关闭页面和本地服务进程后重新启动，继续上次分析。
        self.restart_service()
        expect(self.page.get_by_role('button', name='继续上次分析')).to_be_visible()
        self.page.get_by_role('button', name='继续上次分析').click()
        expect(self.page.get_by_role('heading', name='确认同款')).to_be_visible()
        expect(self.page.locator('#snapshotInfo')).to_contain_text(before['frozen_at'])
        expect(self.page.locator('#snapshotInfo')).to_contain_text('其中2待确认')

        # 已确认仍是已确认，撤回仍是待确认，成员与排除关系不变。
        restored = self.service.get(sid)
        self.assertEqual(restored['excluded'], before['excluded'])
        self.assertEqual([g['id'] for g in restored['groups'] if g['confirmed']], ['G1'])
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认')
        Path('work').mkdir(exist_ok=True)
        self.page.screenshot(path='work/ticket08-resume.png')
        # 恢复的是原快照：旧名称、旧图片、旧库存都在，不受源库更新影响。
        self.choose_group('云朵杯')
        expect(self.page.locator('#groupDetail')).to_contain_text('云朵杯')
        expect(self.page.locator('#groupDetail')).not_to_contain_text('改名后的云朵杯')
        expect(self.page.locator('#groupDetail img')).to_have_attribute('src', frozen_image)
        expect(self.page.locator('#groupDetail tr', has_text='2026-09-14')).to_contain_text('50')
        self.choose_group('树叶杯')
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('待确认')

        # 收尾：全部确认后保存并查看，进入结果。
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        self.choose_group('云朵杯')
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        expect(self.page.get_by_role('heading', name='初步畅销品')).to_be_visible()

    def test_save_and_view_needs_no_pending_and_failed_saves_can_retry(self):
        self.add_product('22', '云朵杯', 60, 50)
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        save_and_view = self.page.get_by_role('button', name='保存分组并查看畅销品')
        self.choose_group('G1')
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        # 仍有待确认组：不能跳过确认进入结果，但仍可暂存。
        expect(save_and_view).to_be_disabled()
        expect(self.page.get_by_role('heading', name='初步畅销品')).not_to_be_visible()
        self.page.get_by_role('button', name='暂时保存').click()
        expect(self.page.locator('#saveStatus')).to_have_text(re.compile(r'^已保存 · \d{2}-\d{2} \d{2}:\d{2}$'))
        expect(self.page.get_by_role('heading', name='初步畅销品')).not_to_be_visible()

        # 保存失败：留在原处、保持未保存标记，可重试。
        self.choose_group('云朵杯')
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        expect(self.page.locator('#dirtyStatus')).to_have_text('未保存')
        broken = Path(self.tmp.name) / 'drafts-are-a-directory'
        broken.mkdir()
        self.service.config = AnalysisConfig(self.path, store=broken)
        self.page.get_by_role('button', name='暂时保存').click()
        expect(self.page.locator('#error')).to_contain_text('保存分析草稿失败')
        expect(self.page.locator('#dirtyStatus')).to_have_text('未保存')
        expect(self.page.locator('#saveStatus')).to_have_text('')
        Path('work').mkdir(exist_ok=True)
        self.page.screenshot(path='work/ticket08-save-failure.png')
        # 保存并查看失败同样不呈现结果。
        expect(save_and_view).to_be_enabled()
        save_and_view.click()
        expect(self.page.locator('#error')).to_contain_text('保存分析草稿失败')
        expect(self.page.get_by_role('heading', name='初步畅销品')).not_to_be_visible()
        # 修好存储后重试成功，才进入结果。
        self.service.config = AnalysisConfig(self.path)
        save_and_view.click()
        expect(self.page.get_by_role('heading', name='初步畅销品')).to_be_visible()
        expect(self.page.locator('#error')).not_to_be_visible()

    def test_unsaved_leave_prompt_and_old_draft_errors_stay_visible(self):
        self.add_product('22', '云朵杯', 60, 50)
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        self.choose_group('G1')
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        expect(self.page.locator('#dirtyStatus')).to_have_text('未保存')

        # 普通筛选与组切换不触发离开提示。
        self.page.get_by_role('tab', name='待确认', exact=True).click()
        self.page.get_by_role('tab', name='全部', exact=True).click()
        self.choose_group('云朵杯')
        expect(self.page.locator('#notice')).not_to_be_visible()

        # 未保存时离开：取消保留编辑。
        self.page.get_by_role('button', name='重新选择日期').click()
        expect(self.page.get_by_role('dialog')).to_contain_text('未保存')
        self.page.get_by_role('button', name='取消').click()
        expect(self.page.get_by_role('heading', name='确认同款')).to_be_visible()
        expect(self.page.locator('#dirtyStatus')).to_have_text('未保存')
        # 未保存时正常关闭会被拦截；保存后不再拦截。
        probe = "() => { const event = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented; }"
        self.assertTrue(self.page.evaluate(probe))
        self.page.get_by_role('button', name='暂时保存').click()
        expect(self.page.locator('#dirtyStatus')).to_have_text('')
        self.assertFalse(self.page.evaluate(probe))

        # 再改一笔后明确放弃：回到最近保存的版本。
        self.choose_group('云朵杯')
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        self.page.get_by_role('button', name='重新选择日期').click()
        self.page.get_by_role('button', name='放弃修改').click()
        expect(self.page.get_by_role('heading', name='选择销量计算区间')).to_be_visible()
        self.page.get_by_role('button', name='继续上次分析').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('其中1待确认')
        self.choose_group('云朵杯')
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('待确认')

        # 旧草稿版本错误可见，且不被静默清空。
        conn = sqlite3.connect(self.service.config.store)
        conn.execute('UPDATE analysis_drafts SET schema_version=99')
        conn.commit()
        conn.close()
        self.restart_service()
        expect(self.page.get_by_role('button', name='继续上次分析')).to_be_visible()
        self.page.get_by_role('button', name='继续上次分析').click()
        expect(self.page.locator('#error')).to_contain_text('不兼容')
        conn = sqlite3.connect(self.service.config.store)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM analysis_drafts').fetchone()[0], 1)
        conn.close()


class InventoryCalculationTests(unittest.TestCase):
    def test_new_sku_and_missing_day_do_not_end_existing_sku(self):
        rows = [dict(shop_key='A', offer_id='P', sku_id=sku,
                     date=f'2026-09-{day:02}', stock=stock)
                for day, sku, stock in [(7, 'a', 100), (9, 'a', 90),
                                        (10, 'b', 50), (14, 'b', 40)]]
        result = calculate_inventory(rows, '2026-09-07', '2026-09-14')[('A', 'P')]
        self.assertEqual(result['sales'], 20)
        self.assertEqual(result['points'][-1]['stock'], 130)
        added = next(s for s in result['skus'] if s['sku_id'] == 'b')
        self.assertIsNone(added['points'][0]['stock'])
        self.assertEqual(added['points'][3]['sales'], 0)
        self.assertEqual(result['skus'][0]['points'][1]['color'], 'yellow')

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
