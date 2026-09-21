"""票 10：跨店同款排名、构成树与完整双轴图。

服务层的组级聚合是数值边界，期望值按规格手算；浏览器层走真实页面加临时库存库，
核对 A40—A43 的用户可见结果。"""
import unittest

import test_analysis as fixture
from helpers import new_round
from playwright.sync_api import expect
from test_matching import picture
from bestseller_monitor.analysis import rank_groups


def day(date, stock, sales=0, color='green', segment_start=False):
    return {'date': date, 'stock': stock, 'sales': sales,
            'color': color, 'segment_start': segment_start}


def member(shop, offer, *, sales, points):
    return {'shop_key': shop, 'offer_id': offer, 'product_name': f'{shop}-{offer}',
            'sales': sales, 'points': points, 'skus': []}


def group(*members):
    return {'id': 'G1', 'confirmed': False,
            'members': [{'shop_key': m['shop_key'], 'offer_id': m['offer_id']} for m in members]}


class GroupRankingTests(unittest.TestCase):
    """组图与排名只经 rank_groups 一处产出：SKU → 商品 → 同款组的聚合顺序（规格 §4、§6、§14）。"""

    def test_group_points_follow_sku_product_group_order(self):
        # 商品一：SKU a 100→80、SKU b 50→90，商品日点 150→170、20 销量、补货红点（A03）。
        first = member('A01', '11', sales=20,
                       points=[day('2026-09-07', 150), day('2026-09-08', 170, 20, 'red')])
        # 商品二：跨店正常下降，60→40。
        second = member('A02', '22', sales=40,
                        points=[day('2026-09-07', 100), day('2026-09-08', 60, 40)])
        snapshot = {'products': [first, second], 'groups': [group(first, second)]}
        rank_groups(snapshot)
        ranked = snapshot['groups'][0]
        self.assertEqual(ranked['sales'], 60)
        # 并列稳定选择与代表商品都看成员次序：高销量在前。
        self.assertEqual([m['offer_id'] for m in ranked['members']], ['22', '11'])
        self.assertEqual(ranked['points'], [day('2026-09-07', 250), day('2026-09-08', 230, 60, 'red')])

    def test_ties_break_by_identity_not_display_order(self):
        later = member('A02', '22', sales=20, points=[day('2026-09-07', 50), day('2026-09-08', 30, 20)])
        earlier = member('A01', '11', sales=20, points=[day('2026-09-07', 100), day('2026-09-08', 80, 20)])
        snapshot = {'products': [later, earlier], 'groups': [group(later, earlier)]}
        rank_groups(snapshot)
        ranked = snapshot['groups'][0]
        self.assertEqual([m['offer_id'] for m in ranked['members']], ['11', '22'])
        self.assertEqual(ranked['sales'], 40)

    def test_unknown_member_stock_stays_unknown_and_switches_mark_the_group(self):
        declining = member('A01', '11', sales=10,
                           points=[day('2026-09-07', 100), day('2026-09-08', 90, 10), day('2026-09-09', None)])
        # 商品二在 8 日切换规格：9 日一段库存未知，不能按 0 计入组库存。
        switched = member('A02', '22', sales=0,
                          points=[day('2026-09-07', 50), day('2026-09-08', None, segment_start=True),
                                  day('2026-09-09', None)])
        snapshot = {'products': [declining, switched], 'groups': [group(declining, switched)]}
        rank_groups(snapshot)
        self.assertEqual(snapshot['groups'][0]['points'], [
            day('2026-09-07', 150),
            day('2026-09-08', 90, 10, 'green', segment_start=True),
            day('2026-09-09', None),
        ])


class RankingBrowserTests(unittest.TestCase):
    """真实页面加临时库存库：跨店组排名、构成树与双轴图的可见行为（A40—A43）。"""

    setUp = fixture.AnalysisBrowserTests.setUp
    stop_server = fixture.AnalysisBrowserTests.stop_server
    submit = fixture.AnalysisBrowserTests.submit
    dates = fixture.AnalysisBrowserTests.dates

    def seed_product(self, shop, offer, name, observations, color='red'):
        """一个商品的多日观测：observations 为 [(日期, [(sku_id, sku_name, 库存), ...])]。"""
        for day, skus in observations:
            rid = new_round(self.db, shop, run_date=day)
            self.db.submit_inventory_snapshot(
                round_id=rid, shop_key=shop, shop_url=f'https://{shop.lower()}.example',
                shop_name=f'店铺{int(shop[1:])}', offer_id=offer,
                product_url=f'https://detail.1688.com/offer/{offer}.html',
                list_title=name, detail_title=name, main_image_url=f'https://img.example/{offer}.png',
                image_evidence=picture(color),
                sku_rows=[dict(sku_id=sku_id, sku_name=sku_name, sku_stock=stock)
                          for sku_id, sku_name, stock in skus],
                collected_at=day + 'T04:00:00+00:00', attempt=1)

    @staticmethod
    def group_for(snapshot, offer):
        return next(g for g in snapshot['groups'] if any(m['offer_id'] == offer for m in g['members']))

    def join_group(self, sid, offer, target_offer):
        """把商品并进目标商品所在的组：走真实的人工移动入口。"""
        snapshot = self.service.get(sid)
        source = self.group_for(snapshot, offer)
        target = self.group_for(snapshot, target_offer)
        member = next(m for m in source['members'] if m['offer_id'] == offer)
        self.service.edit_group(sid, 'move', source['id'], member, target['id'])

    def start_review(self, end='2026-09-14'):
        self.page.get_by_label('开始日期', exact=True).fill('2026-09-07')
        self.page.get_by_label('结束日期', exact=True).fill(end)
        if end != '2026-09-14':
            self.page.get_by_role('button', name='继续', exact=True).click()  # 非全量抓取日提醒
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.get_by_role('heading', name='确认同款')).to_be_visible()
        return self.page.url.split('analysis=')[1]

    def save_and_show_results(self):
        """确认全部组后进结果页：保存门禁落地后由「保存分组并查看畅销品」把守（票 08），按钮在就先保存。"""
        sid = self.page.url.split('analysis=')[1]
        snapshot = self.service.get(sid)
        self.service.confirm_groups(sid, [g['id'] for g in snapshot['groups']])
        self.page.reload()
        expect(self.page.get_by_role('heading', name='确认同款')).to_be_visible()
        if self.page.get_by_role('button', name='保存分组并查看畅销品').count():
            self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        expect(self.page.get_by_role('heading', name='初步畅销品')).to_be_visible()

    def test_ranking_orders_group_totals_with_representative_and_zero_sales_toggle(self):
        # A40：排名行给整组总销量、代表取本区间最高销量成员；零销量组默认隐藏可勾选。
        self.submit('2026-09-08', 60)  # 数据库商品 杯子：7 日 100、8 日 60、14 日 80
        self.seed_product('A02', '222', '杯子2', [('2026-09-07', [('red', '红色', 100)]),
                                                  ('2026-09-08', [('red', '红色', 90)]),
                                                  ('2026-09-14', [('red', '红色', 30)])])
        self.seed_product('A03', '333', '杯子3', [('2026-09-07', [('red', '红色', 100)]),
                                                  ('2026-09-08', [('red', '红色', 90)]),
                                                  ('2026-09-14', [('red', '红色', 60)])])
        self.seed_product('A04', '444', '杯子4', [('2026-09-07', [('red', '红色', 100)]),
                                                  ('2026-09-14', [('red', '红色', 100)])])
        sid = self.start_review()
        self.join_group(sid, '222', '11')
        self.join_group(sid, '333', '11')
        self.save_and_show_results()
        rows = self.page.locator('#ranking > details')
        expect(rows).to_have_count(1)
        row = rows.first
        expect(row.locator('summary')).to_contain_text('杯子2')  # 70 > 40，代表是本区间最高成员
        expect(row.locator('summary .number')).to_have_text('150 销量')  # 整组 70+40+40，不是代表销量
        expect(row.locator('summary')).to_contain_text('3 家店铺 · 3 个商品 · 3 个 SKU')
        self.page.get_by_label('显示零销量组').check()
        expect(rows).to_have_count(2)
        expect(rows.nth(1).locator('summary')).to_contain_text('杯子4')
        expect(rows.nth(1).locator('summary .number')).to_have_text('0 销量')
        # A40：换区间后最高贡献者变化，代表名称跟着换。
        self.page.get_by_role('button', name='重新选择日期').click()
        self.page.get_by_label('结束日期', exact=True).fill('2026-09-08')
        self.page.get_by_role('button', name='继续', exact=True).click()  # 周二不是全量抓取日
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.get_by_role('heading', name='确认同款')).to_be_visible()
        sid = self.page.url.split('analysis=')[1]
        self.join_group(sid, '222', '11')
        self.join_group(sid, '333', '11')
        self.save_and_show_results()
        row = self.page.locator('#ranking > details').first
        expect(row.locator('summary .product-title b')).to_have_text('杯子')
        expect(row.locator('summary .number')).to_have_text('60 销量')

    def test_group_tree_expands_skus_with_hovers_and_per_chart_legends(self):
        # A07/A08/A41/A43：组内树、展开全部与单 SKU 展开、三种图例、hover 原文。
        self.seed_product('A02', '222', '杯子甲', [('2026-09-07', [('red', '红色', 100)]),
                                                  ('2026-09-08', [('red', '红色', 120)]),
                                                  ('2026-09-10', [('red', '红色', 90)])])
        self.seed_product('A03', '333', '杯子乙', [('2026-09-07', [('blue', '蓝色', 50)]),
                                                  ('2026-09-08', [('blue', '蓝色', 40)]),
                                                  ('2026-09-10', [('blue', '蓝色', 30)])])
        sid = self.start_review('2026-09-10')
        self.join_group(sid, '333', '222')
        self.save_and_show_results()
        row = self.page.locator('#ranking > details').first
        expect(row.locator(':scope > summary')).to_contain_text('杯子甲')
        expect(row.locator(':scope > summary .number')).to_have_text('50 销量')
        expect(row.locator(':scope > summary')).to_contain_text('2 家店铺 · 2 个商品 · 2 个 SKU')
        expect(self.page.get_by_text('点击展开')).to_have_count(0)
        # 代表商品的来源图标打开新标签页，不触发行展开。
        self.page.context.route('https://detail.1688.com/**', lambda route: route.fulfill(body='source'))
        with self.page.expect_popup() as opened:
            row.locator(':scope > summary').get_by_role('link', name='商品源地址').click()
        opened.value.wait_for_url('**/222.html')
        opened.value.close()
        expect(row).not_to_have_attribute('open', '')
        row.locator(':scope > summary').click()
        expect(row).to_have_attribute('open', '')
        # 组展开后：总图加店铺 → 商品 → SKU 构成树；商品图与销量默认可见，没有商品重复图。
        group_chart = row.locator('.chart-block[data-sku="false"] .inventory-chart')
        expect(group_chart).to_be_visible()
        expect(row.locator('.chart-block')).to_have_count(3)
        expect(row.locator('.sku-row .chart-block')).to_have_count(2)
        expect(row.get_by_role('heading', name='同款构成')).to_be_visible()
        expect(row).to_contain_text('店铺2')
        expect(row).to_contain_text('店铺3')
        expect(row.locator('.tree .product img')).to_have_count(2)
        expect(row).to_contain_text('商品销量 30')
        expect(row).to_contain_text('商品销量 20')
        expect(row.locator('.sku-row[open]')).to_have_count(0)
        expect(row.locator('.sku-row .sku-total')).to_have_text(['销量 30', '销量 20'])
        expect(row.locator('.sku-row img')).to_have_count(0)
        expand = row.locator('.tree-heading button.expand-all')
        self.assertLess(expand.evaluate('node=>parseFloat(getComputedStyle(node).fontSize)'), 14)
        # 组图：右上两种状态的图例、左上曲线标识；补货红点与全缺失日上层仍绿。
        legend = row.locator('.chart-block[data-sku="false"] .stock-legend')
        expect(legend).to_contain_text('● 正常观测')
        expect(legend).to_contain_text('● 疑似补货')
        expect(legend).not_to_contain_text('当日未抓取库存')
        expect(row.locator('.chart-block[data-sku="false"] .chart-key')).to_contain_text('库存（左轴）')
        expect(row.locator('.chart-block[data-sku="false"] .chart-key')).to_contain_text('累计销量（右轴）')
        group_chart.locator('circle.stock-point[data-date="2026-09-08"]').hover()
        expect(self.page.locator('#tip')).to_have_text('存在疑似补货SKU')
        group_chart.locator('circle.stock-point[data-date="2026-09-09"]').hover()
        expect(self.page.locator('#tip')).to_be_hidden()
        # 单 SKU 也能在原树内展开：三种状态图例与 hover 原文。
        sku = row.locator('.sku-row').filter(has_text='红色').first
        sku.locator('summary').click()
        expect(row.locator('.sku-row[open]')).to_have_count(1)
        sku_legend = sku.locator('.stock-legend')
        expect(sku_legend).to_contain_text('● 正常观测')
        expect(sku_legend).to_contain_text('● 当日未抓取库存')
        expect(sku_legend).to_contain_text('● 疑似补货')
        sku_chart = sku.locator('.inventory-chart')
        sku_chart.locator('circle.stock-point[data-date="2026-09-08"]').hover()
        expect(self.page.locator('#tip')).to_have_text('库存增加，疑似补货，该时段销量设定为0')
        sku_chart.locator('circle.stock-point[data-date="2026-09-09"]').hover()
        expect(self.page.locator('#tip')).to_have_text('当日未抓取库存')
        sku_chart.locator('circle.stock-point[data-date="2026-09-07"]').hover()
        expect(self.page.locator('#tip')).to_be_hidden()
        # 展开全部／收起全部控制原树内的 SKU 图。
        expand.click()
        expect(row.locator('.sku-row[open]')).to_have_count(2)
        expect(expand).to_have_text('收起全部')
        expand.click()
        expect(row.locator('.sku-row[open]')).to_have_count(0)
        expect(expand).to_have_text('展开全部')
        row.locator(':scope > summary').click()
        expect(row).not_to_have_attribute('open', '')
        # 删除项：无全局图例、覆盖串、范围开关与旧明细入口。
        review = self.page.locator('#reviewPage')
        for text in ['点击展开', '完整范围', '实际抓取覆盖', '查看每天库存与销量计算', '进一步展开明细']:
            expect(review).not_to_contain_text(text)

    def test_dual_axis_ranges_keep_every_point_in_axis_with_one_shared_x(self):
        # A42：高库存小波动、恒定库存、约三倍波动宽度与真实刻度，同日两序列同 X。
        self.seed_product('A02', '222', '稳定杯', [('2026-09-07', [('flat', '常驻', 500)]),
                                                  ('2026-09-08', [('flat', '常驻', 500)]),
                                                  ('2026-09-14', [('flat', '常驻', 500)])])
        self.seed_product('A03', '333', '波动杯', [('2026-09-07', [('mild', '小波动', 10000)]),
                                                  ('2026-09-08', [('mild', '小波动', 9950)]),
                                                  ('2026-09-14', [('mild', '小波动', 9900)])])
        sid = self.start_review()
        self.join_group(sid, '222', '333')
        self.save_and_show_results()
        row = self.page.locator('#ranking > details').first
        row.locator('summary').click()
        group_chart = row.locator('.chart-block[data-sku="false"] .inventory-chart')
        # 组库存 10,500 → 10,400：范围约三倍波动宽度，刻度是真实值。
        expect(group_chart.locator('.axis-label.stock-axis')).to_have_text(['10,300', '10,450', '10,600'])
        expect(group_chart).to_contain_text('累计销量')
        # 恒定库存给上下留白，不出现零宽坐标轴。
        flat = row.locator('.sku-row').filter(has_text='常驻').first
        flat.locator('summary').click()
        expect(flat.locator('.axis-label.stock-axis')).to_have_text(['495', '500', '505'])
        # 所有点都在轴内，同日两序列共用一个 X，数值不被改写。
        circles = group_chart.locator('circle.stock-point')
        sales = group_chart.locator('path.sales-point')
        expect(circles).to_have_count(8)
        expect(sales).to_have_count(8)
        xs = {point.get_attribute('data-date'): point.get_attribute('data-x') for point in sales.all()}
        for circle in circles.all():
            self.assertEqual(xs[circle.get_attribute('data-date')], circle.get_attribute('cx'))
            self.assertLessEqual(42, float(circle.get_attribute('cy')))
            self.assertLessEqual(float(circle.get_attribute('cy')), 210)
        for point in sales.all():
            self.assertLessEqual(42, float(point.get_attribute('data-y')))
            self.assertLessEqual(float(point.get_attribute('data-y')), 210)
        # 未抓取日保留填充库存，不按未知 0 补线；终点都带数值标签。
        mild = row.locator('.sku-row').filter(has_text='小波动').first
        mild.locator('summary').click()
        mild_chart = mild.locator('.inventory-chart')
        expect(mild_chart.locator('circle.stock-point[data-date="2026-09-09"]')).to_have_attribute('data-stock', '9950')
        expect(mild_chart.locator('circle.stock-point[data-date="2026-09-14"]')).to_have_attribute('data-stock', '9900')
        expect(mild_chart.locator('.point-label').last).to_have_text('9,900')
        expect(mild_chart.locator('.point-label').nth(14)).to_have_text('100')

