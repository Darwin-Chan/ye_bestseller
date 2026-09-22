"""票 11：导出可独立打开的 HTML 报告。

服务层覆盖命名、落盘不覆盖与失败无残留；浏览器层走真实页面加临时库存库，
导出后停服务、断网打开生成的文件，核对 A44—A45。
"""
import dataclasses
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bestseller_monitor
import test_analysis as fixture
import test_bestseller_ranking as ranking
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect
from bestseller_monitor.report import report_name, write_report
from helpers import submit_offer
from playwright.sync_api import expect


class ReportFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / 'output'

    def test_report_name_carries_the_date_range_and_export_stamp(self):
        name = report_name('2026-09-07', '2026-09-14', '2026-09-21T09:30:12+00:00')
        self.assertEqual(name, 'bestseller-2026-09-07_2026-09-14-20260921093012.html')

    def test_write_creates_the_directory_and_never_overwrites_an_existing_name(self):
        name = report_name('2026-09-07', '2026-09-14', '2026-09-21T09:30:12+00:00')
        first = write_report(self.out, name, '<!doctype html><p>第一份</p>')
        second = write_report(self.out, name, '<!doctype html><p>第二份</p>')
        self.assertEqual(first, self.out / name)
        self.assertEqual(second.name, name.replace('.html', '-2.html'))
        self.assertIn('第一份', first.read_text(encoding='utf-8'))
        self.assertIn('第二份', second.read_text(encoding='utf-8'))

    def test_failed_write_leaves_no_half_report_behind(self):
        name = report_name('2026-09-07', '2026-09-14', '2026-09-21T09:30:12+00:00')
        with patch('bestseller_monitor.report.os.fdopen', side_effect=OSError('磁盘已满')):
            with self.assertRaisesRegex(OSError, '磁盘已满'):
                write_report(self.out, name, '<!doctype html><p>写不完</p>')
        self.assertEqual(list(self.out.iterdir()), [])

    def test_output_path_that_is_a_file_fails_without_writing_anything(self):
        occupied = Path(self.tmp.name) / 'output-is-a-file'
        occupied.write_text('占位', encoding='utf-8')
        with self.assertRaises(OSError):
            write_report(occupied, 'report.html', '<!doctype html><p>x</p>')
        self.assertEqual(occupied.read_text(encoding='utf-8'), '占位')


class ExportConfigurationTests(unittest.TestCase):
    def test_output_directory_defaults_to_project_output_and_reads_from_config(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        config = root / 'analysis.toml'
        config.write_text('[analysis]\ndatabase = "inventory.db"\noutput = "reports"\n', encoding='utf-8')
        self.assertEqual(AnalysisConfig.from_file(config).output, root / 'reports')
        config.write_text('[analysis]\ndatabase = "inventory.db"\n', encoding='utf-8')
        self.assertEqual(AnalysisConfig.from_file(config).output, (root / '../output').resolve())
        default = AnalysisConfig(root / 'inventory.db')
        self.assertEqual(default.output, Path(bestseller_monitor.__file__).resolve().parent.parent / 'output')


class ExportServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'inventory.db'
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.submit('11', '2026-09-07', 100, name='月牙杯')
        self.submit('11', '2026-09-14', 80, name='月牙杯')
        self.submit('22', '2026-09-07', 50, name='云朵杯')
        self.submit('22', '2026-09-14', 40, name='云朵杯')
        self.out = Path(self.tmp.name) / 'output'
        self.service = AnalysisService(AnalysisConfig(self.path, output=self.out), running=lambda: False)

    def submit(self, offer, day, stock, *, name):
        submit_offer(self.db, offer, day, stock, name=name, color='red')

    def saved_snapshot(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        for group in ('G1', 'G2'):
            self.service.confirm(snapshot['id'], group)
        self.service.save_and_view(snapshot['id'])
        return snapshot['id']

    def test_export_writes_the_saved_analysis_to_output_and_leaves_it_unchanged(self):
        sid = self.saved_snapshot()
        before = self.service.get(sid)
        stored = self.service.store.read(sid)
        result = self.service.export(sid, '<!doctype html><p>报告正文</p>')
        self.assertEqual(set(result), {'path'})
        path = Path(result['path'])
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, self.out)
        self.assertTrue(path.name.startswith('bestseller-2026-09-07_2026-09-14-'))
        self.assertEqual(path.read_text(encoding='utf-8'), '<!doctype html><p>报告正文</p>')
        # 导出不改动已保存的分析，也不改草稿版本。
        self.assertEqual(self.service.get(sid), before)
        self.assertEqual(self.service.store.read(sid).payload, stored.payload)

    def test_export_refuses_pending_groups_dirty_edits_unknown_ids_and_invalid_html(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        with self.assertRaisesRegex(ValueError, '待确认'):
            self.service.export(sid, '<!doctype html><p>报告</p>')
        self.service.confirm(sid, 'G1')
        self.service.save_draft(sid)
        with self.assertRaisesRegex(ValueError, '待确认'):
            self.service.export(sid, '<!doctype html><p>报告</p>')
        self.service.confirm(sid, 'G2')  # 全部确认但未保存
        with self.assertRaisesRegex(ValueError, '未保存'):
            self.service.export(sid, '<!doctype html><p>报告</p>')
        self.service.save_and_view(sid)
        with self.assertRaisesRegex(ValueError, '分析已不存在'):
            self.service.export('missing', '<!doctype html><p>报告</p>')
        for invalid in ('<p>不是报告</p>', None, ['x']):
            with self.assertRaisesRegex(ValueError, '导出内容无效'):
                self.service.export(sid, invalid)
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))

    def test_export_sources_lists_current_addresses_of_this_analysis_only(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        self.assertEqual(self.service.export_sources(snapshot['id']), {'urls': {
            '11': 'https://detail.1688.com/offer/11.html',
            '22': 'https://detail.1688.com/offer/22.html'}})
        self.conn.execute("UPDATE products SET product_url='' WHERE offer_id='22'")
        self.conn.commit()
        self.assertEqual(list(self.service.export_sources(snapshot['id'])['urls']), ['11'])
        # 库里再有别的商品也不返回：只取本次分析选中的商品（规格 §8 的导出时地址）。
        submit_offer(self.db, '33', '2026-09-14', 5, name='新增杯')
        self.assertEqual(list(self.service.export_sources(snapshot['id'])['urls']), ['11'])
        with self.assertRaisesRegex(ValueError, '分析已不存在'):
            self.service.export_sources('missing')


class OfflineReportBrowserTests(unittest.TestCase):
    """真实页面 → 本地服务 → output；停服务后用 file:// 打开生成的文件（A44—A45）。"""

    stop_server = fixture.AnalysisBrowserTests.stop_server
    submit = fixture.AnalysisBrowserTests.submit  # 夹具 setUp 用它铺底数
    seed_product = ranking.RankingBrowserTests.seed_product
    start_review = ranking.RankingBrowserTests.start_review
    save_and_show_results = ranking.RankingBrowserTests.save_and_show_results
    switch_tab = ranking.RankingBrowserTests.switch_tab

    def setUp(self):
        fixture.AnalysisBrowserTests.setUp(self)
        self.out = Path(self.tmp.name) / 'output'
        self.service.config = dataclasses.replace(self.service.config, output=self.out)

    def export(self):
        self.page.get_by_role('button', name='导出 HTML 报告').click()
        expect(self.page.locator('#exportStatus')).to_contain_text('已导出 · ')
        return Path(self.page.locator('#exportStatus').inner_text().split(' · ', 1)[1])

    def test_export_writes_the_full_analysis_to_output_and_it_opens_offline(self):
        # 组数超过一页（默认每页 20）：一个补货加缺失日的组、一个正常组、22 个零销量组。
        self.seed_product('A02', '222', '杯子甲', [('2026-09-07', [('red', '红色', 100)]),
                                                  ('2026-09-08', [('red', '红色', 120)]),
                                                  ('2026-09-10', [('red', '红色', 90)])])
        for offer in range(21, 43):
            self.seed_product('A02', str(offer), f'零销量杯{offer}',
                              [('2026-09-07', [('red', '红色', 100)]),
                               ('2026-09-14', [('red', '红色', 100)])])
        sid = self.start_review()
        self.save_and_show_results()
        # 冻结之后源库又变了：零销量杯21 在 14 日重报为 7。报告必须用已保存快照。
        self.seed_product('A02', '21', '零销量杯21', [('2026-09-14', [('red', '红色', 7)])])
        self.conn.execute("UPDATE products SET product_url='https://detail.1688.com/offer/222.html?export=1'"
                          " WHERE offer_id='222'")
        self.conn.commit()
        # 导出时浏览器在分组列表第 2 页，所有行保持折叠。
        self.page.get_by_role('button', name='下一页').click()
        expect(self.page.locator('#pageInfo')).to_contain_text('2 / 2')
        path = self.export()
        self.assertEqual(path.parent, self.out)
        self.assertIn('2026-09-07_2026-09-14', path.name)
        # 再次导出不覆盖上一份：文件名带各自的导出标识。
        again = self.export()
        self.assertNotEqual(again, path)
        self.assertTrue(path.exists() and again.exists())
        Path('.scratch/work').mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path='.scratch/work/ticket11-export-status.png')

        snapshot = self.service.get(sid)
        self.stop_server()
        offline_requests = []
        self.page.on('request', lambda request: offline_requests.append(request.url))
        self.page.goto(path.as_uri())
        expect(self.page.get_by_role('heading', name='销量分析报告')).to_be_visible()
        main = self.page.locator('main')
        expect(main).to_contain_text('2026-09-07 — 2026-09-14')
        # 报告里的时刻按本机时间读到秒，不再印原始 UTC 串。
        expect(main).to_contain_text(re.compile(r'数据固定于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'))
        expect(main).to_contain_text(re.compile(r'保存于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'))
        # 完整已确认分析：全部 24 组（不止当前第 2 页），零销量组也在。
        rows = self.page.locator('#ranking > details')
        expect(rows).to_have_count(24)
        expect(rows.first.locator(':scope > summary')).to_contain_text('杯子甲')
        expect(rows.first.locator(':scope > summary .number')).to_have_text('30 销量')
        expect(self.page.locator('#ranking')).to_contain_text('零销量杯42')
        expect(rows.last.locator(':scope > summary .number')).to_have_text('0 销量')
        # 所有图和 SKU 明细随文件提供，不依赖本地服务（每组 1 商品 1 SKU）。
        expect(self.page.locator('#ranking .inventory-chart')).to_have_count(48)
        # 冻结的库存：杯子 14 日就是冻结时的 80，双轴图数据来自快照本身。
        cup = rows.nth(1)
        expect(cup.locator(':scope > summary')).to_contain_text('杯子')
        cup.locator(':scope > summary').click()
        expect(cup.locator('.chart-block[data-sku="false"] .inventory-chart')
               .locator('circle.stock-point[data-date="2026-09-14"]')).to_have_attribute('data-stock', '80')
        # 零销量杯21 在冻结后被重报为 7，报告仍按冻结时的 100 呈现。
        frozen = self.page.locator('#ranking > details').filter(has_text='零销量杯21').first
        frozen.locator(':scope > summary').click()
        expect(frozen.locator('.chart-block[data-sku="false"] .inventory-chart')
               .locator('circle.stock-point[data-date="2026-09-14"]')).to_have_attribute('data-stock', '100')
        # 展开与 hover 与在线一致：组图红点提示补货、正常点无提示。
        top = rows.first
        top.locator(':scope > summary').click()
        chart = top.locator('.chart-block[data-sku="false"] .inventory-chart')
        expect(chart).to_be_visible()
        chart.locator('circle.stock-point[data-date="2026-09-08"]').hover()
        expect(self.page.locator('#tip')).to_have_text('存在疑似补货SKU')
        chart.locator('circle.stock-point[data-date="2026-09-07"]').hover()
        expect(self.page.locator('#tip')).to_be_hidden()
        # 构成树与 SKU 图：展开全部、三种状态 hover、图片资产内嵌。
        expect(top.get_by_role('heading', name='同款构成')).to_be_visible()
        top.locator('.tree-heading button.expand-all').click()
        sku_chart = top.locator('.sku-row[open]').first.locator('.inventory-chart')
        expect(sku_chart).to_be_visible()
        sku_chart.locator('circle.stock-point[data-date="2026-09-08"]').hover()
        expect(self.page.locator('#tip')).to_have_text('库存增加，疑似补货，该时段销量设定为0')
        sku_chart.locator('circle.stock-point[data-date="2026-09-09"]').hover()
        expect(self.page.locator('#tip')).to_have_text('当日未抓取库存')
        Path('.scratch/work').mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path='.scratch/work/ticket11-offline-report.png')
        photo = top.locator('.tree .product img')
        expect(photo).to_have_count(1)
        self.assertTrue(photo.evaluate('img=>img.complete && img.naturalWidth>0'))
        # 打开报告到点开来源链接之前，没有任何 http(s) 请求：断网、无服务也能读。
        self.assertTrue(offline_requests)
        self.assertFalse([url for url in offline_requests if url.startswith(('http://', 'https://'))])
        # 来源图标用导出时仍有效的详情地址，新标签页打开；跳转需要网络，不影响离线阅读。
        link = top.locator('.tree').get_by_role('link', name='商品源地址').first
        expect(link).to_have_attribute('href', 'https://detail.1688.com/offer/222.html?export=1')
        expect(link).to_have_attribute('target', '_blank')
        self.page.context.route('https://detail.1688.com/**', lambda route: route.fulfill(body='source'))
        with self.page.expect_popup() as opened:
            link.click()
        opened.value.wait_for_url('**/222.html?export=1')
        opened.value.close()
        # 无分组修改、保存草稿或采集控制动作；无本地服务的隐藏请求。
        for name in ['确认当前分组', '撤回当前分组', '组内新增商品', '暂时保存', '保存分组并查看畅销品',
                     '重试模型匹配', '导出 HTML 报告', '上一页', '下一页']:
            expect(self.page.get_by_role('button', name=name)).to_have_count(0)
        for text in ['完整范围', '实际抓取覆盖', '查看每天库存与销量计算', '进一步展开明细', '点击展开']:
            expect(self.page.locator('body')).not_to_contain_text(text)
        content = path.read_text(encoding='utf-8')
        for forbidden in ['/api/', 'fetch(', 'DEEPSEEK_API_KEY', '127.0.0.1', 'localhost']:
            self.assertNotIn(forbidden, content)
        # 必要样式内嵌：断网（服务已停）仍按原样式呈现（与库存抓取同一套底色）。
        self.assertEqual(self.page.evaluate('getComputedStyle(document.body).backgroundColor'),
                         'rgb(245, 247, 250)')

    def test_failed_export_shows_the_real_error_and_retry_succeeds(self):
        sid = self.start_review()
        self.save_and_show_results()
        before = self.service.get(sid)
        occupied = Path(self.tmp.name) / 'output-is-a-file'
        occupied.write_text('占位', encoding='utf-8')
        self.service.config = dataclasses.replace(self.service.config, output=occupied)
        self.page.get_by_role('button', name='导出 HTML 报告').click()
        expect(self.page.locator('#error')).to_contain_text('导出报告失败')
        expect(self.page.locator('#exportStatus')).to_have_text('')
        # 失败不冒充成功、不留半份报告，也不改变已保存的分析。
        self.assertEqual(occupied.read_text(encoding='utf-8'), '占位')
        self.assertEqual(self.service.get(sid), before)
        # 修好输出目录后重试成功。
        self.service.config = dataclasses.replace(self.service.config, output=self.out)
        self.page.get_by_role('button', name='导出 HTML 报告').click()
        expect(self.page.locator('#exportStatus')).to_contain_text('已导出 · ')
        expect(self.page.locator('#error')).to_be_hidden()
        self.assertEqual(len(list(self.out.glob('*.html'))), 1)
