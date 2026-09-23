"""票 15：销量分析界面七处调整（配色、编号、顶部两行、无 SKU 明细、页签与批量撤回）。

浏览器用例走真实页面与本地接口，只替换模型与图片网络（沿用分组编辑夹具）。
"""
import re
import unittest

from playwright.sync_api import expect

import test_group_editing as fixture


class AnalysisUiAdjustmentTests(unittest.TestCase):
    setUp = fixture.GroupEditingTests.setUp
    stop_server = fixture.GroupEditingTests.stop_server
    submit = fixture.GroupEditingTests.submit
    dates = fixture.GroupEditingTests.dates
    seed = fixture.GroupEditingTests.seed
    switch_tab = fixture.GroupEditingTests.switch_tab

    def review(self):
        """选好日期、进入确认同款页，返回分析编号。

        票 01 起确认同款页先出骨架、长判断时遮罩盖在上面；这里等快照信息落定，
        也就是这一趟运行已经结束、页面已经填好。
        """
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('日期区间')
        return self.page.url.split('analysis=')[1]

    def confirm_first(self, times=1):
        """在待确认列表里逐个确认最前面的组（确认后它离开列表，所以每次都点第一个）。

        每点一次要等列表真的少一组：确认请求在飞的这段里 updateDraft 忽略新点击，
        不等就把下一次点击丢掉。
        """
        for _ in range(times):
            before = self.page.locator('.group-choice').count()
            self.page.locator('.group-choice').first.click()
            self.page.get_by_role('button', name='确认当前分组', exact=True).click()
            expect(self.page.locator('.group-choice')).to_have_count(before - 1)

    def test_palette_and_header_match_the_inventory_program(self):
        self.seed()
        self.assertEqual(self.page.evaluate("getComputedStyle(document.body).backgroundColor"), 'rgb(245, 247, 250)')
        self.assertEqual(self.page.evaluate("getComputedStyle(document.body).color"), 'rgb(31, 41, 55)')
        header = self.page.evaluate("()=>{const h=getComputedStyle(document.querySelector('header'));"
                                    " return [h.backgroundColor, getComputedStyle(document.querySelector('header h1')).color]}")
        self.assertEqual(header, ['rgba(0, 0, 0, 0)', 'rgb(31, 41, 55)'])
        self.assertEqual(
            self.page.get_by_role('button', name='下一步、进入同款确认').evaluate("node=>getComputedStyle(node).backgroundColor"),
            'rgb(37, 99, 235)')

    def test_coverage_shop_codes_and_two_line_snapshot_info(self):
        self.seed()
        self.dates()
        for which in ('开始', '结束'):
            table = self.page.get_by_role('table', name=f'{which}日期真实抓取')
            expect(table.locator('tbody tr').first).to_contain_text('A01 店铺1')

        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        meta = self.page.locator('#snapshotInfo .snapshot-meta')
        expect(meta).to_contain_text('日期区间：2026-09-07 — 2026-09-14')
        expect(meta).to_contain_text(re.compile(r'数据固定于：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'))
        # 两组“标题：内容”排在同一行。
        self.assertTrue(self.page.evaluate(
            "()=>{const spans=document.querySelectorAll('#snapshotInfo .snapshot-meta span');"
            " return spans.length===2&&spans[0].offsetTop===spans[1].offsetTop}"))
        tally = self.page.locator('#snapshotInfo .snapshot-tally')
        expect(tally).to_have_text('4 个商品 · 4 组同款（其中 4 组待确认）')
        self.assertEqual(tally.evaluate("node=>getComputedStyle(node).fontSize"), '22px')
        self.assertEqual(tally.evaluate("node=>getComputedStyle(node).color"), 'rgb(37, 99, 235)')

    def test_member_cards_drop_the_daily_sku_table(self):
        self.seed()
        self.review()
        self.page.locator('.group-choice').first.click()
        detail = self.page.locator('#groupDetail')
        expect(detail.locator('.matching-member').first).to_be_visible()
        expect(detail.locator('.matching-member table')).to_have_count(0)
        expect(detail.locator('.matching-member details')).to_have_count(0)
        expect(detail.locator('.matching-member aside b')).to_have_text('其他疑似归组')

    def test_tab_order_default_and_all_tab_has_no_selection_controls(self):
        self.seed()
        self.review()
        tabs = self.page.get_by_role('tab')
        expect(tabs).to_have_count(3)
        # 票 02 起页签带计数徽标：这里只认标签本身（顺序）；徽标文本由票 02 的用例钉。
        self.assertEqual([tabs.nth(index).inner_text().split()[0] for index in range(3)],
                         ['待确认', '已确认', '全部'])
        expect(tabs.nth(0)).to_have_attribute('aria-selected', 'true')
        expect(self.page.get_by_role('checkbox')).to_have_count(4)
        expect(self.page.locator('#batchRow')).to_be_visible()
        expect(self.page.get_by_role('button', name='确认勾选组')).to_be_visible()

        self.switch_tab('全部')
        expect(self.page.locator('#batchRow')).to_be_hidden()
        expect(self.page.locator('#selectionCount')).to_be_hidden()
        expect(self.page.get_by_role('checkbox')).to_have_count(0)
        expect(self.page.get_by_role('button', name='确认勾选组')).to_have_count(0)
        expect(self.page.get_by_role('button', name='确认当前筛选全部组')).to_have_count(0)
        expect(self.page.locator('.group-choice')).to_have_count(4)

    def test_confirmed_tab_bulk_withdraw_returns_groups_to_pending(self):
        self.seed()
        sid = self.review()
        self.confirm_first(2)
        expect(self.page.locator('#snapshotInfo')).to_contain_text('其中 2 组待确认')

        self.switch_tab('已确认')
        expect(self.page.locator('.group-choice')).to_have_count(2)
        expect(self.page.get_by_role('button', name='撤回勾选组')).to_be_disabled()
        expect(self.page.get_by_role('button', name='撤回当前筛选全部组')).to_be_enabled()
        self.page.get_by_role('checkbox').first.check()
        expect(self.page.locator('#selectionCount')).to_have_text('已勾选 1 组')

        self.page.get_by_role('button', name='撤回勾选组').click()
        dialog = self.page.get_by_role('dialog', name='撤回同款商品分组', exact=True)
        expect(dialog.locator('p').nth(0)).to_have_text('将撤回1个同款分组，包含1个商品。')
        expect(dialog.locator('p').nth(1)).to_be_hidden()
        dialog.get_by_role('button', name='撤回', exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(self.page.locator('#dirtyStatus')).to_have_text('未保存')
        expect(self.page.locator('#snapshotInfo')).to_contain_text('其中 3 组待确认')
        expect(self.page.locator('.group-choice')).to_have_count(1)
        self.assertEqual(sum(g['confirmed'] for g in self.service.get(sid)['groups']), 1)

        self.page.get_by_role('button', name='撤回当前筛选全部组').click()
        dialog = self.page.get_by_role('dialog', name='撤回同款商品分组', exact=True)
        expect(dialog.locator('p').first).to_have_text('将撤回1个同款分组，包含1个商品。')
        dialog.get_by_role('button', name='撤回', exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('其中 4 组待确认')
        expect(self.page.locator('.group-choice')).to_have_count(0)
        self.assertEqual(sum(g['confirmed'] for g in self.service.get(sid)['groups']), 0)

    def test_dialogs_keep_the_right_copy(self):
        self.seed()
        self.review()
        self.confirm_first(2)
        # 确认侧：两行文案都在。
        self.page.get_by_role('button', name='确认当前筛选全部组').click()
        dialog = self.page.get_by_role('dialog', name='确认同款商品分组', exact=True)
        expect(dialog.locator('p').nth(0)).to_have_text('将确认2个同款分组，包含2个商品。')
        expect(dialog.locator('p').nth(1)).to_have_text('其中不含匹配到多组的商品')
        expect(dialog.get_by_role('button', name='确认', exact=True)).to_be_visible()
        dialog.get_by_role('button', name='取消', exact=True).click()

        # 撤回侧：只有一行，且上一轮的第二行文案不留残文。
        self.switch_tab('已确认')
        self.page.get_by_role('checkbox').first.check()
        self.page.get_by_role('button', name='撤回勾选组').click()
        dialog = self.page.get_by_role('dialog', name='撤回同款商品分组', exact=True)
        expect(dialog.locator('p').nth(0)).to_have_text('将撤回1个同款分组，包含1个商品。')
        expect(dialog.locator('p').nth(1)).to_have_text('')
        expect(dialog.get_by_role('button', name='撤回', exact=True)).to_be_visible()

    def test_single_group_withdraw_works_from_the_all_tab(self):
        self.seed()
        sid = self.review()
        self.confirm_first()
        self.switch_tab('全部')
        self.page.locator('.group-choice').first.click()
        expect(self.page.get_by_role('button', name='撤回当前分组', exact=True)).to_be_visible()
        expect(self.page.get_by_role('button', name='确认当前分组', exact=True)).to_have_count(0)
        self.page.get_by_role('button', name='撤回当前分组').click()
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('待确认')
        self.assertEqual(sum(g['confirmed'] for g in self.service.get(sid)['groups']), 0)

    def test_chart_colors_come_from_the_shared_palette(self):
        """图表取色也走同一套变量：色值写死在 JS 里，改主色时图就悄悄掉队。"""
        self.seed()
        self.review()
        self.page.get_by_role('button', name='确认当前筛选全部组').click()
        self.page.get_by_role('dialog').get_by_role('button', name='确认', exact=True).click()
        self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        expect(self.page.get_by_role('heading', name='初步畅销品')).to_be_visible()
        self.page.locator('#ranking > details > summary').first.click()
        chart = self.page.locator('#ranking .inventory-chart').first
        expect(chart).to_be_visible()
        colors = chart.evaluate("""node=>{const pick=(sel,prop)=>getComputedStyle(node.querySelector(sel))[prop];
            return {stock:pick('.stock-line','stroke'), sales:pick('.sales-line','stroke'),
                    point:pick('.sales-point','stroke'), grid:pick('.grid-line','stroke'),
                    date:pick('.date-label','fill')}}""")
        self.assertEqual(colors, {'stock': 'rgb(22, 163, 74)', 'sales': 'rgb(37, 99, 235)',
                                  'point': 'rgb(37, 99, 235)', 'grid': 'rgb(229, 233, 240)',
                                  'date': 'rgb(107, 114, 128)'})

    def test_service_bulk_withdraw_matches_single_withdraw_semantics(self):
        self.seed()
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        self.service.confirm_groups(sid, ['G1', 'G2', 'G3'])
        withdrawn = self.service.withdraw_groups(sid, ['G1', 'G3'])
        states = {g['id']: g['confirmed'] for g in withdrawn['groups']}
        self.assertEqual([states['G1'], states['G2'], states['G3']], [False, True, False])
        self.assertTrue(withdrawn['dirty'])
        self.assertEqual(withdrawn['products'], snapshot['products'])
        self.assertEqual(withdrawn['inventory'], snapshot['inventory'])
        with self.assertRaises(ValueError):
            self.service.withdraw_groups(sid, ['G1', 'missing'])
        with self.assertRaises(ValueError):   # 空目标不算一次成功的撤回
            self.service.withdraw_groups(sid, [])
        self.assertEqual(self.service.get(sid), withdrawn)
