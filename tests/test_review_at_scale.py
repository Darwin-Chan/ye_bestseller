"""Review state through the analysis service and its real local browser API."""
import unittest
from pathlib import Path

from playwright.sync_api import expect
from helpers import new_round
from test_matching import picture

import test_group_editing as fixture


class ReviewAtScaleTests(unittest.TestCase):
    setUp = fixture.GroupEditingTests.setUp
    stop_server = fixture.GroupEditingTests.stop_server
    submit = fixture.GroupEditingTests.submit
    dates = fixture.GroupEditingTests.dates
    seed = fixture.GroupEditingTests.seed
    switch_tab = fixture.GroupEditingTests.switch_tab
    model = fixture.GroupEditingTests.model
    group_for = staticmethod(fixture.GroupEditingTests.group_for)

    def test_bulk_confirmation_and_withdrawal_preserve_current_membership(self):
        self.seed()
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        member = {'shop_key': 'A02', 'offer_id': '22'}
        self.service.edit_group(sid, 'move', 'G2', member, 'G1')
        before = self.service.edit_group(sid, 'remove', 'G1', member)
        self.service.confirm(sid, 'G3')
        confirmed = self.service.confirm_groups(sid, ['G1', 'G3', 'G1'])
        self.assertTrue(all(g['confirmed'] for g in confirmed['groups'] if g['id'] in ['G1', 'G3']))
        withdrawn = self.service.withdraw(sid, 'G1')
        self.assertFalse(next(g for g in withdrawn['groups'] if g['id'] == 'G1')['confirmed'])
        self.assertTrue(withdrawn['dirty'])
        for field in ('excluded', 'products', 'inventory'):
            self.assertEqual(withdrawn[field], before[field])
        self.assertEqual([g['members'] for g in withdrawn['groups']], [g['members'] for g in before['groups']])
        retried = self.service.retry_matching(sid)
        self.assertEqual(retried['groups'], withdrawn['groups'])
        with self.assertRaises(ValueError):
            self.service.confirm_groups(sid, ['G1', 'missing'])
        self.assertEqual(self.service.get(sid), retried)

    def test_browser_tabs_bulk_dialog_and_withdrawal_empty_state(self):
        self.seed()
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.get_by_role('tab')).to_have_count(3)
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        self.page.get_by_role('button', name='确认当前筛选全部组').click()
        dialog = self.page.get_by_role('dialog', name='确认同款商品分组', exact=True)
        expect(dialog.locator('p').nth(0)).to_have_text('将确认3个同款分组，包含3个商品。')
        expect(dialog.locator('p').nth(1)).to_have_text('其中不含匹配到多组的商品')
        dialog.get_by_role('button', name='确认', exact=True).click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('4 个商品 · 4 组同款（其中 0 组待确认）')
        expect(self.page.locator('#dirtyStatus')).to_have_text('未保存')
        expect(self.page.get_by_role('button', name='确认当前筛选全部组')).to_be_disabled()
        self.switch_tab('已确认')
        self.page.get_by_role('button', name='撤回当前分组').click()
        expect(self.page.locator('.group-choice')).to_have_count(3)
        self.switch_tab('待确认')
        expect(self.page.locator('.group-choice')).to_have_count(1)
        expect(self.page.locator('#groupDetail h3')).to_have_text('G1 · 1 个商品')
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        expect(self.page.locator('#groupDetail')).to_contain_text('暂无符合条件的分组')
        expect(self.page.locator('.group-choice')).to_have_count(0)

    def test_browser_filters_require_one_member_and_confirm_complete_adjusted_group(self):
        transport = self.model()
        # Establish historical model evidence, then change only product A's image.
        self.service.start('2026-09-07', '2026-09-14')
        new_round(self.db, 'A01', run_date='2026-09-07')
        self.submit('2026-09-14', 99, name='杯子1', image_evidence=picture('blue'))
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('.group-choice')).to_have_count(4)
        self.page.get_by_label('商品来源', exact=True).select_option('信息变更')
        self.page.get_by_label('匹配状态', exact=True).select_option('匹配多组同款')
        # A changed but has only one match; C has multiple matches but is new.
        expect(self.page.locator('.group-choice')).to_have_count(0)
        self.page.get_by_label('商品来源', exact=True).select_option('新商品')
        expect(self.page.locator('.group-choice')).to_have_count(1)
        expect(self.page.locator('#groupDetail .matching-member')).to_have_count(3)
        expect(self.page.locator('#groupDetail .filter-hit')).to_have_count(1)
        expect(self.page.locator('#groupDetail .filter-hit')).to_have_attribute('data-product', '33')
        expect(self.page.locator('.matching-tags a, .matching-tags button')).to_have_count(0)
        self.assertTrue(self.page.locator('.matching-tags span').evaluate_all("nodes=>nodes.every(n=>getComputedStyle(n).cursor==='default')"))
        sid = self.page.url.split('analysis=')[1]
        # Human joins an unmatched product into the filtered group before confirmation.
        self.page.get_by_role('button', name='组内新增商品').click()
        self.page.get_by_role('textbox', name='搜索商品', exact=True).fill('杯子6')
        self.page.locator('#addResults').get_by_role('button', name='添加到当前组').click()
        expect(self.page.locator('#groupDetail .matching-member')).to_have_count(4)
        before = self.service.get(sid)
        self.page.get_by_role('button', name='确认当前筛选全部组').click()
        dialog = self.page.get_by_role('dialog', name='确认同款商品分组', exact=True)
        expect(dialog.locator('p').nth(0)).to_have_text('将确认1个同款分组，包含4个商品。')
        expect(dialog.locator('p').nth(1)).to_have_text('其中包含1个匹配到多组的商品，这些商品按照系统推荐分组推进。')
        Path('.scratch/work').mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path='.scratch/work/ticket07-bulk-multiple.png')
        calls = len(transport.calls)
        dialog.get_by_role('button', name='确认', exact=True).click()
        expect(dialog).not_to_be_visible()
        after = self.service.get(sid)
        self.assertEqual([g['members'] for g in after['groups']], [g['members'] for g in before['groups']])
        self.switch_tab('已确认')
        self.page.get_by_role('button', name='撤回当前分组').click()
        expect(self.page.locator('.group-choice')).to_have_count(0)
        withdrawn = self.service.get(sid)
        self.assertEqual(withdrawn['products'], after['products'])
        self.assertEqual(withdrawn.get('excluded'), after.get('excluded'))
        self.assertEqual(len(transport.calls), calls)
        retried = self.service.retry_matching(sid)
        self.assertEqual(self.group_for(retried, '11'), self.group_for(withdrawn, '11'))

    def test_browser_3012_products_cross_page_selection_search_and_full_confirmation(self):
        new_round(self.db, 'A01', run_date='2026-09-07')
        rid = new_round(self.db, *(f'A{i:02}' for i in range(1, 13)), run_date='2026-09-14')
        for i in range(3011):
            shop = f'A{i % 12 + 1:02}'
            offer = str(10000+i)
            self.db.submit_inventory_snapshot(round_id=rid, shop_key=shop,
                shop_url='https://shop.example', shop_name=f'店铺{i % 12 + 1}', offer_id=offer,
                product_url=f'https://detail.1688.com/offer/{offer}.html',
                list_title=f'规模商品{i:04}', detail_title=f'规模商品{i:04}', main_image_url='',
                sku_rows=[dict(sku_id='one', sku_name='标准', sku_stock=100)],
                collected_at='2026-09-14T04:00:00+00:00', attempt=1)
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('3012 个商品 · 3012 组同款（其中 3012 组待确认）', timeout=20000)
        expect(self.page.locator('.group-choice')).to_have_count(20)
        for index in (1, 2):
            choice = self.page.locator('.group-choice').nth(index)
            group = choice.get_attribute('data-group')
            choice.click()
            expect(self.page.locator('#groupDetail h3')).to_have_text(f'{group} · 1 个商品')
        self.page.get_by_role('checkbox').first.check()
        self.page.get_by_role('button', name='下一页', exact=True).click()
        expect(self.page.locator('#groupDetail h3')).to_have_text('G21 · 1 个商品')
        self.page.get_by_role('checkbox').first.check()
        self.page.get_by_role('button', name='上一页', exact=True).click()
        expect(self.page.get_by_role('checkbox').first).to_be_checked()
        self.page.get_by_role('button', name='确认勾选组').click()
        dialog = self.page.get_by_role('dialog', name='确认同款商品分组', exact=True)
        expect(dialog.locator('p').first).to_have_text('将确认2个同款分组，包含2个商品。')
        dialog.get_by_role('button', name='确认', exact=True).click()
        expect(dialog).not_to_be_visible()
        self.page.get_by_role('checkbox').nth(1).check()
        self.page.get_by_label('搜索分组商品', exact=True).fill('规模商品3010')
        expect(self.page.locator('.group-choice')).to_have_count(1)
        expect(self.page.locator('#groupDetail')).to_contain_text('规模商品3010')
        expect(self.page.locator('#selectionCount')).to_have_text('已勾选 0 组')
        self.page.get_by_role('checkbox').first.check()
        self.page.get_by_label('商品来源', exact=True).select_option('新商品')
        expect(self.page.locator('#selectionCount')).to_have_text('已勾选 0 组')
        self.page.get_by_role('checkbox').first.check()
        self.switch_tab('待确认')
        expect(self.page.locator('#selectionCount')).to_have_text('已勾选 0 组')
        self.page.get_by_label('搜索分组商品', exact=True).fill('')
        self.page.get_by_role('button', name='确认当前筛选全部组').click()
        expect(dialog.locator('p').first).to_have_text('将确认3010个同款分组，包含3010个商品。')
        expect(dialog.locator('p').nth(1)).to_have_text('其中不含匹配到多组的商品')
        self.page.screenshot(path='.scratch/work/ticket07-scale.png')
        dialog.get_by_role('button', name='确认', exact=True).click()
        expect(dialog).not_to_be_visible(timeout=30000)
        expect(self.page.locator('#snapshotInfo')).to_contain_text('其中 0 组待确认')
        expect(self.page.locator('#groupDetail')).to_contain_text('暂无符合条件的分组')
        sid = self.page.url.split('analysis=')[1]
        result = self.service.get(sid)
        self.assertEqual(sum(g['confirmed'] for g in result['groups']), 3012)
        self.switch_tab('已确认')
        expect(self.page.locator('.group-choice')).to_have_count(20)
        self.page.get_by_role('button', name='撤回当前分组').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('其中 1 组待确认')
