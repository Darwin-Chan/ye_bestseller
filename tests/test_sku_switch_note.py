"""票 07：说人话——SKU 行去掉「第 N 个规格有效段」，改由商品级切换说明交代形态切换。

服务缝：切换表只收区间里看得见的翻转（与图上断线同一口径：切换前后两段都得落在区间内）。
页面缝：真页面＋真本地服务，核对 SKU 行文案、说明行的三要素与多行次序；导出报告同一渲染。
"""
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import expect

import test_analysis as analysis_fixture
import test_analysis_results_screen as results_fixture
import test_group_editing as fixture
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService, calculate_inventory
from bestseller_monitor.db import Database, connect
from helpers import new_round
from test_matching import picture

# 09-09 与 09-14 换成多规格、09-11 换回单规格：三行说明的两组断言（页面与导出报告）共用。
MULTI_SWITCH_NOTES = [
    '09-09 起切换为多规格（此前单规格），切换日重新建立基准',
    '09-11 起切换为单规格（此前多规格），切换日重新建立基准',
    '09-14 起切换为多规格（此前单规格），切换日重新建立基准']


def seed_shape_days(db, shop, offer, title, days):
    """按天提交一个商品的观测：days 是 (日, [(规格号, 名称, 库存), ...])，粒度随用例给。"""
    for day, skus in days:
        stamp = f'2026-09-{day:02}'
        rid = new_round(db, shop, run_date=stamp)
        db.submit_inventory_snapshot(
            round_id=rid, shop_key=shop, shop_url='https://shop.example',
            shop_name=f'店铺{shop[-2:]}', offer_id=offer,
            product_url=f'https://detail.1688.com/offer/{offer}.html',
            list_title=title, detail_title=title, main_image_url='',
            image_evidence=picture('red'),
            sku_rows=[dict(sku_id=sku, sku_name=name, sku_stock=stock)
                      for sku, name, stock in skus],
            collected_at=stamp + 'T04:00:00+00:00', attempt=1)


class ShapeSwitchTableTests(unittest.TestCase):
    """calculate_inventory 的切换表：日期＋切换前后形态（True＝单规格），按时间顺序。"""

    def bucket(self, days, start='2026-09-07', end='2026-09-14'):
        rows = [dict(shop_key='A', offer_id='P', sku_id=sku, sku_name=sku,
                     date=f'2026-09-{day:02}', stock=stock)
                for day, skus in days for sku, stock in skus]
        return calculate_inventory(rows, start, end)[('A', 'P')]

    def test_a_shape_that_never_changes_carries_no_switches(self):
        bucket = self.bucket([(7, [('red', 100)]), (14, [('red', 40)])])
        self.assertEqual(bucket['shape_switches'], [])

    def test_a_switch_inside_the_interval_reports_the_day_and_both_shapes(self):
        bucket = self.bucket([(7, [('default', 100)]), (8, [('default', 90)]),
                              (10, [('white', 40), ('cream', 60)]),
                              (14, [('white', 30), ('cream', 50)])])
        self.assertEqual(bucket['shape_switches'],
                         [{'date': '2026-09-10', 'shape_before': True, 'shape_after': False}])

    def test_repeated_switches_list_in_time_order(self):
        bucket = self.bucket([(7, [('default', 100)]), (9, [('white', 40), ('cream', 60)]),
                              (11, [('default', 80)]), (14, [('white', 30), ('cream', 50)])])
        self.assertEqual([(sw['date'], sw['shape_before'], sw['shape_after'])
                          for sw in bucket['shape_switches']],
                         [('2026-09-09', True, False), ('2026-09-11', False, True),
                          ('2026-09-14', True, False)])

    def test_a_switch_with_one_side_outside_the_interval_stays_out(self):
        # 切换在区间之前：区间里只看得见切换后的形态，没有可讲的翻转。
        bucket = self.bucket([(1, [('default', 100)]), (3, [('white', 40)]),
                              (10, [('white', 30)])])
        self.assertEqual(bucket['shape_switches'], [])
        # 切换日恰在区间第一天：区间里同样只看得见一种形态。
        bucket = self.bucket([(5, [('default', 100)]), (7, [('white', 40)]),
                              (10, [('white', 30)])])
        self.assertEqual(bucket['shape_switches'], [])
        # 切换在区间结束之后：区间里只看得见切换前的形态。
        bucket = self.bucket([(7, [('default', 100)]), (14, [('default', 90)]),
                              (20, [('white', 40)])])
        self.assertEqual(bucket['shape_switches'], [])


class DraftCompatibilityTests(unittest.TestCase):
    """票 07 之前保存的草稿没有切换表：读回时照冻结库存补算（与既有老草稿补键同规）。"""

    def test_a_draft_without_the_switch_table_gets_it_back_on_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'inventory.db'
        conn = connect(path)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES ('A01','店铺1')")
        conn.commit()
        seed_shape_days(Database(conn), 'A01', '11', '换过规格的杯子',
                        [(7, [('default', 'default', 100)]),
                         (10, [('red', '红色', 40)]),
                         (14, [('red', '红色', 30)])])
        service = AnalysisService(AnalysisConfig(path), running=lambda: False)
        snapshot = service.start('2026-09-07', '2026-09-14')
        service.save_draft(snapshot['id'])
        # 把存好的正文退回「没有这个键」的老样子：模拟票 07 之前保存的草稿。
        drafts = connect(service.config.store)
        self.addCleanup(drafts.close)
        payload = json.loads(drafts.execute('SELECT payload FROM analysis_drafts WHERE id=?',
                                            (snapshot['id'],)).fetchone()['payload'])
        for product in payload['products']:
            product.pop('shape_switches', None)
        drafts.execute('UPDATE analysis_drafts SET payload=? WHERE id=?',
                       (json.dumps(payload), snapshot['id']))
        drafts.commit()
        # 重启读回：说明行的据照冻结库存补上，与全新跑一趟的结论一致。
        restored = AnalysisService(AnalysisConfig(path), running=lambda: False).get(snapshot['id'])
        self.assertEqual(restored['products'][0]['shape_switches'],
                         [{'date': '2026-09-10', 'shape_before': True, 'shape_after': False}])


class SwitchNoteBrowserTests(unittest.TestCase):
    setUp = fixture.GroupEditingTests.setUp
    stop_server = fixture.GroupEditingTests.stop_server
    submit = analysis_fixture.AnalysisBrowserTests.submit
    dates = analysis_fixture.AnalysisBrowserTests.dates
    start_review = fixture.GroupEditingTests.start_review
    confirm_all = results_fixture.ResultsScreenTests.confirm_all
    save_and_view = results_fixture.ResultsScreenTests.save_and_view

    def seed_shapes(self, shop, offer, title, days):
        seed_shape_days(self.db, shop, offer, title, days)

    def open_group(self, title):
        group = self.page.locator('#ranking > details').filter(has_text=title).first
        group.locator(':scope > summary').click()
        return group

    def test_sku_rows_drop_the_segment_label_and_switchless_products_add_no_note(self):
        self.seed_shapes('A05', '55', '纯多规格杯',
                         [(7, [('red', '红色', 100)]), (14, [('red', '红色', 40)])])
        self.dates()
        self.start_review()
        self.confirm_all()
        self.save_and_view()
        group = self.open_group('纯多规格杯')
        expect(group.locator('.sku-row summary > span').first).to_have_text('▸ 红色')
        expect(group.locator('.switch-note')).to_have_count(0)
        # 段号标注在结果屏上任何地方都不再出现。
        expect(self.page.locator('#results')).not_to_contain_text('规格有效段')

    def test_a_switched_product_lists_one_note_per_switch_in_time_order(self):
        self.seed_shapes('A05', '55', '换过规格的杯子', [
            (7, [('default', 'default', 100)]), (8, [('default', 'default', 90)]),
            (9, [('red', '红色', 40), ('blue', '蓝色', 60)]),
            (10, [('red', '红色', 30), ('blue', '蓝色', 50)]),
            (11, [('default', 'default', 70)]), (12, [('default', 'default', 60)]),
            (14, [('red', '红色', 30), ('blue', '蓝色', 50)])])
        self.dates()
        self.start_review()
        self.confirm_all()
        self.save_and_view()
        group = self.open_group('换过规格的杯子')
        expect(group.locator('.switch-note')).to_have_text(MULTI_SWITCH_NOTES)
        Path('.scratch/work').mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path='.scratch/work/ticket07-switch-note.png', full_page=True)
        # 导出报告与页面共用同一份渲染：打开报告文件，说明行仍是这三行，段号字样也不在。
        out = Path(self.tmp.name) / 'output'
        self.service.config = dataclasses.replace(self.service.config, output=out)
        self.page.get_by_role('button', name='导出 HTML 报告').click()
        expect(self.page.locator('#exportStatus')).to_contain_text('已导出 · ')
        report = Path(self.page.locator('#exportStatus').inner_text().split(' · ', 1)[1])
        self.page.goto(report.as_uri())
        expect(self.page.get_by_role('heading', name='销量分析报告')).to_be_visible()
        row = self.page.locator('#ranking > details').filter(has_text='换过规格的杯子').first
        row.locator(':scope > summary').click()
        expect(row.locator('.switch-note')).to_have_text(MULTI_SWITCH_NOTES)
        expect(self.page.locator('#ranking')).not_to_contain_text('规格有效段')


if __name__ == '__main__':
    unittest.main()
