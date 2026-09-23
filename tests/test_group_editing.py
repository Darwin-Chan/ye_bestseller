"""Grouping edits through the real analysis service and browser, with temporary inventory."""
import unittest
import os
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import expect

import test_analysis as fixture
from helpers import new_round
from test_matching import picture, ModelTransport
from bestseller_monitor.matching import MatchingConfig, MatchingService, identity


class GroupEditingTests(unittest.TestCase):
    setUp = fixture.AnalysisBrowserTests.setUp
    stop_server = fixture.AnalysisBrowserTests.stop_server
    submit = fixture.AnalysisBrowserTests.submit
    dates = fixture.AnalysisBrowserTests.dates
    switch_tab = fixture.AnalysisBrowserTests.switch_tab

    def seed(self, count=4):
        for i in range(1, count + 1):
            shop, offer = f'A{i:02}', str(i * 11)
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 100-i)]:
                rid = new_round(self.db, shop, run_date=day)
                self.db.submit_inventory_snapshot(round_id=rid, shop_key=shop,
                    shop_url='https://shop.example', shop_name=f'店铺{i}', offer_id=offer,
                    product_url=f'https://detail.1688.com/offer/{offer}.html',
                    list_title=f'杯子{i}', detail_title=f'杯子{i}', main_image_url='',
                    image_evidence=picture('red'),
                    sku_rows=[dict(sku_id='red', sku_name='红色', sku_stock=stock)],
                    collected_at=day+'T04:00:00+00:00', attempt=1)

    def test_manual_move_preserves_all_four_state_combinations_and_unique_membership(self):
        self.seed()
        for source_confirmed in (False, True):
            for target_confirmed in (False, True):
                snapshot = self.service.start('2026-09-07', '2026-09-14')
                sid = snapshot['id']
                if source_confirmed:
                    self.service.confirm(sid, 'G1')
                if target_confirmed:
                    self.service.confirm(sid, 'G3')
                # Build AB in source, then explicitly move B to C's group.
                b = {'shop_key': 'A02', 'offer_id': '22'}
                self.service.edit_group(sid, 'move', 'G2', b, 'G1')
                result = self.service.edit_group(sid, 'move', 'G1', b, 'G3')
                groups = {g['id']: g for g in result['groups']}
                self.assertEqual(groups['G1']['confirmed'], source_confirmed)
                self.assertEqual(groups['G3']['confirmed'], target_confirmed)
                self.assertNotIn('G2', groups)
                self.assertEqual(groups['G3']['sales'], 5)
                self.assertEqual(sum(b in g['members'] for g in groups.values()), 1)
                self.assertEqual(len(groups['G1']['members']), 1)
                with self.assertRaises(ValueError):
                    self.service.edit_group(sid, 'move', 'G1', b, 'G3')
                self.assertEqual(self.service.get(sid), result)

    def test_single_member_removal_is_rejected_without_changing_draft(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        with self.assertRaisesRegex(ValueError, '单商品组'):
            self.service.edit_group(snapshot['id'], 'remove', snapshot['groups'][0]['id'],
                                    {'shop_key': 'A01', 'offer_id': '11'})
        self.assertEqual(self.service.get(snapshot['id']), snapshot)

    def test_browser_search_move_remove_and_single_member_controls(self):
        self.seed()
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        # 这个用例确认后还要接着操作同一组，「全部」页签下组不因确认离开列表。
        # 点哪一组与左列次序无关（票 05 起大组在前）：按组号选，别拿序号当组号。
        self.switch_tab('全部')
        expect(self.page.locator('.group-choice')).to_have_count(4)
        self.page.locator('.group-choice').filter(has_text='G2 ·').click()
        expect(self.page.locator('#groupDetail h3')).to_have_text('G2 · 1 个商品')
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        self.page.get_by_role('button', name='组内新增商品').click()
        self.page.get_by_role('textbox', name='搜索商品').fill('杯子1')
        self.page.locator('#addResults').get_by_role('button', name='添加到当前组').click()
        expect(self.page.locator('#groupDetail h3')).to_have_text('G2 · 2 个商品')
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认')
        expect(self.page.locator('.group-choice')).to_have_count(3)
        expect(self.page.locator('#groupDetail .matching-member').first).to_contain_text('杯子2')
        self.page.get_by_role('button', name='从当前分组移除商品').first.click()
        expect(self.page.get_by_role('dialog', name='移除商品')).to_contain_text('1. 移除商品后，会记住它与当前组其他商品的排除关系。')
        self.page.get_by_role('button', name='确认移除', exact=True).click()
        expect(self.page.locator('#groupDetail h3')).to_have_text('G2 · 1 个商品')
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认')
        expect(self.page.get_by_role('button', name='从当前分组移除商品')).to_have_count(0)
        sid = self.page.url.split('analysis=')[1]
        draft = self.service.get(sid)
        self.assertTrue(draft['dirty'])
        self.assertEqual(len(draft['excluded']), 1)
        self.assertEqual(sum(len(g['members']) for g in draft['groups']), 4)

    def model(self):
        self.seed(6)
        transport = ModelTransport()
        transport.decisions = {(f'杯子{i}', f'杯子{j}'): (i, j) in [(1,2),(1,3),(2,3),(3,4),(3,5)]
                               for i in range(1,7) for j in range(i+1,7)}
        self.service.matcher = MatchingService(MatchingConfig(Path(self.tmp.name)/'match.sqlite', mode='direct'))
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=transport).start()
        return transport

    @staticmethod
    def group_for(snapshot, offer):
        return next(g for g in snapshot['groups'] if any(m['offer_id'] == offer for m in g['members']))

    def test_removal_exclusions_unique_multiple_none_and_retry_priority(self):
        transport = self.model()
        for confirmed_offers, expected_size in [((),1), (('55',),2), (('44','55'),1)]:
            snapshot = self.service.start('2026-09-07','2026-09-14')
            sid = snapshot['id']
            source = self.group_for(snapshot, '33')['id']
            self.service.confirm(sid, source)
            for offer in confirmed_offers:
                self.service.confirm(sid, self.group_for(snapshot, offer)['id'])
            result = self.service.edit_group(sid, 'remove', source, {'shop_key':'A03','offer_id':'33'})
            self.assertEqual(len(self.group_for(result,'33')['members']), expected_size)
            self.assertFalse(self.group_for(result,'33')['confirmed'])
            self.assertTrue(self.group_for(result,'11')['confirmed'])
            self.assertEqual(len(self.group_for(result,'11')['members']),2)
            self.assertEqual(len(result['excluded']),2)
            calls = len(transport.calls)
            retried = self.service.retry_matching(sid)
            self.assertEqual(retried['groups'], result['groups'])
            self.assertEqual(retried['excluded'],result['excluded'])
            self.assertEqual(len(transport.calls),calls)
        # A removal with no evidence stays independent and reports no match.
        result = self.service.edit_group(sid, 'remove', source, {'shop_key':'A01','offer_id':'11'})
        self.assertEqual(next(p for p in result['products'] if p['offer_id']=='11')['match_label'], '暂无匹配同款')
        self.assertEqual(len(self.group_for(result,'22')['members']),1)
        # Joining A back with B clears only AB, leaving AC and BC exclusions.
        result = self.service.edit_group(sid,'move',self.group_for(result,'11')['id'],
                                        {'shop_key':'A01','offer_id':'11'},source)
        self.assertEqual(len(result['excluded']),2)
        self.assertTrue(all(identity({'shop_key':'A03','offer_id':'33'}) in pair for pair in result['excluded']))

    def test_browser_candidate_comparison_preserves_selection_and_moves_atomically(self):
        self.model()
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        # 确认一组之后还要从它的成员卡进对比弹窗，「全部」页签下这一组不会离开列表。
        self.switch_tab('全部')
        expect(self.page.locator('.group-choice')).to_have_count(4)
        self.page.get_by_role('button', name='确认当前分组',exact=True).click()
        card = self.page.locator('#groupDetail [data-product="33"]')
        card.locator('aside button').first.click()
        dialog = self.page.get_by_role('dialog',name='其他疑似归组',exact=True)
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role('button',name='加入选中组')).to_be_disabled()
        dialog.locator('input[type=radio]').first.check()
        expect(dialog.locator('#candidateStatus')).to_contain_text('已选')
        selected = dialog.locator('input:checked').input_value()
        dialog.locator('.candidate').last.locator('summary').click()
        dialog.locator('#compareBody').evaluate('(node)=>node.scrollTop=node.scrollHeight')
        scroll = dialog.locator('#compareBody').evaluate('(node)=>node.scrollTop')
        dialog.locator('.candidate').last.get_by_role('button',name='放大商品图片').click()
        expect(self.page.get_by_role('dialog',name='商品图片',exact=True)).to_be_visible()
        self.page.get_by_role('button',name='关闭大图').click()
        self.assertEqual(dialog.locator('input:checked').input_value(), selected)
        self.assertEqual(dialog.locator('#compareBody').evaluate('(node)=>node.scrollTop'),scroll)
        # Every source icon identifies its own product, including current and candidate cards.
        for link in dialog.get_by_role('link',name='商品源地址').all():
            offer = link.get_attribute('data-source-offer')
            expect(link).to_have_attribute('href',f'https://detail.1688.com/offer/{offer}.html')
        dialog.get_by_role('button',name='移出当前组').click()
        self.page.get_by_role('dialog',name='移除商品',exact=True).get_by_role('button',name='取消',exact=True).click()
        self.assertEqual(dialog.locator('input:checked').input_value(),selected)
        self.assertEqual(dialog.locator('#compareBody').evaluate('(node)=>node.scrollTop'),scroll)
        expect(dialog.locator('.candidate[open]')).to_have_count(2)
        sid = self.page.url.split('analysis=')[1]
        before = self.service.get(sid)
        dialog.get_by_role('button',name='取消',exact=True).click()
        self.assertEqual(self.service.get(sid),before)
        card.locator('aside button').first.click()
        dialog.locator('input[type=radio]').first.check()
        target = dialog.locator('input:checked').input_value()
        dialog.get_by_role('button',name='加入选中组').click()
        expect(dialog).not_to_be_visible()
        after = self.service.get(sid)
        self.assertEqual(self.group_for(after,'33')['id'],target)
        self.assertTrue(self.group_for(after,'11')['confirmed'])
        self.assertFalse(self.group_for(after,'33')['confirmed'])
        self.assertEqual(sum(len(g['members']) for g in after['groups']),6)

    def test_browser_long_comparison_single_member_and_current_source_links(self):
        transport = self.model()
        self.seed(24)
        transport.decisions = {(f'杯子{i}', f'杯子{j}') if f'杯子{i}' < f'杯子{j}' else (f'杯子{j}', f'杯子{i}'):
                              (i,j) in [(1,2),(1,3),(2,3)] or i==3
                              for i in range(1,25) for j in range(i+1,25)}
        self.service.matcher = MatchingService(MatchingConfig(Path(self.tmp.name)/'match.sqlite',mode='direct', candidates=20))
        self.dates()
        self.page.get_by_role('button',name='下一步、进入同款确认').click()
        card = self.page.locator('#groupDetail [data-product="33"]')
        card.locator('aside button').first.click()
        dialog=self.page.get_by_role('dialog',name='其他疑似归组',exact=True)
        self.assertGreater(dialog.locator('.candidate').count(),10)
        dialog.locator('.candidate').last.locator('summary').click()
        dialog.locator('input[type=radio]').last.check()
        body=dialog.locator('#compareBody')
        body.evaluate('(node)=>node.scrollTop=node.scrollHeight')
        self.assertGreater(body.evaluate('(node)=>node.scrollTop'),0)
        bottom=dialog.get_by_role('button',name='加入选中组').bounding_box()
        self.assertLess(bottom['y']+bottom['height'],self.page.viewport_size['height'])
        self.page.context.route('https://detail.1688.com/**',lambda route:route.fulfill(body='product source'))
        self.conn.execute("UPDATE products SET product_url='https://detail.1688.com/offer/33.html?latest=1' WHERE offer_id='33'")
        self.conn.commit()
        with self.page.expect_popup() as opened:
            dialog.locator('#compareCurrent').get_by_role('link',name='商品源地址').click()
        opened.value.wait_for_url('**/33.html?latest=1')
        opened.value.close()
        Path('.scratch/work').mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path='.scratch/work/ticket06-comparison.png')
        dialog.get_by_role('button',name='移出当前组').click()
        self.page.get_by_role('button',name='确认移除',exact=True).click()
        expect(dialog).not_to_be_visible()
        # Find the new singleton across pages; its candidate comparison must hide removal.
        self.page.get_by_role('button',name='下一页',exact=True).click()
        self.page.locator('.group-choice').filter(has_text='杯子3 ·').click()
        expect(self.page.get_by_role('button',name='从当前分组移除商品')).to_have_count(0)
        self.page.locator('#groupDetail aside button').first.click()
        expect(dialog.get_by_role('button',name='移出当前组')).to_be_hidden()

    def test_browser_moves_preserve_four_source_and_target_state_combinations(self):
        self.seed()
        for source_confirmed in (False,True):
            for target_confirmed in (False,True):
                self.dates()
                self.page.get_by_role('button',name='下一步、进入同款确认').click()
                # 四种状态组合要在同一份列表里来回切换，「全部」页签下组不因状态离开列表。
                # 起点那一组显式点：默认选中的是左列第一组，而左列次序由服务端给（票 05）。
                self.switch_tab('全部')
                expect(self.page.locator('.group-choice')).to_have_count(4)
                self.page.locator('.group-choice').filter(has_text='G1 ·').click()
                if source_confirmed:
                    self.page.get_by_role('button',name='确认当前分组',exact=True).click()
                    expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认')
                # Add B to A so the source survives the subsequent move.
                self.page.get_by_role('button',name='组内新增商品').click()
                self.page.get_by_role('textbox',name='搜索商品').fill('杯子2')
                self.page.locator('#addResults').get_by_role('button',name='添加到当前组').click()
                expect(self.page.locator('#groupDetail h3')).to_have_text('G1 · 2 个商品')
                self.page.locator('.group-choice').filter(has_text='G3 ·').click()
                if target_confirmed:
                    self.page.get_by_role('button',name='确认当前分组',exact=True).click()
                    expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认')
                self.page.get_by_role('button',name='组内新增商品').click()
                self.page.get_by_role('textbox',name='搜索商品').fill('杯子2')
                self.page.locator('#addResults').get_by_role('button',name='添加到当前组').click()
                expect(self.page.locator('#groupDetail h3')).to_have_text('G3 · 2 个商品')
                expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认' if target_confirmed else '待确认')
                self.page.locator('.group-choice').filter(has_text='G1 ·').click()
                expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认' if source_confirmed else '待确认')
                expect(self.page.locator('#groupDetail h3')).to_have_text('G1 · 1 个商品')
                self.page.get_by_role('button',name='重新选择日期').click()
                self.page.get_by_role('button',name='放弃修改').click()

    def test_browser_removal_auto_joins_only_unique_pending_group(self):
        self.model()
        self.dates()
        self.page.get_by_role('button',name='下一步、进入同款确认').click()
        # 确认两组后要接着对它们做移除／查看状态，「全部」页签下组不会离开列表。
        self.switch_tab('全部')
        expect(self.page.locator('.group-choice')).to_have_count(4)
        self.page.locator('.group-choice').filter(has_text='杯子5 ·').click()
        self.page.get_by_role('button',name='确认当前分组',exact=True).click()
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认')
        self.page.locator('.group-choice').first.click()
        self.page.get_by_role('button',name='确认当前分组',exact=True).click()
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认')
        self.page.locator('#groupDetail [data-product="33"]').get_by_role('button',name='从当前分组移除商品').click()
        self.page.get_by_role('button',name='确认移除',exact=True).click()
        expect(self.page.locator('#groupDetail h3')).to_contain_text('2 个商品')
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认')
        self.page.locator('.group-choice').filter(has_text='杯子4 ·').click()
        expect(self.page.locator('#groupDetail h3')).to_contain_text('2 个商品')
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('待确认')
        expect(self.page.locator('#groupDetail [data-product="33"]')).to_be_visible()

    def test_failed_edit_does_not_partially_commit_draft(self):
        import sqlite3
        self.model()
        snapshot=self.service.start('2026-09-07','2026-09-14')
        path=self.service.matcher.config.cache
        renamed=path.with_suffix('.temporarily-unavailable')
        path.rename(renamed)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.service.edit_group(snapshot['id'],'remove',self.group_for(snapshot,'33')['id'],
                                        {'shop_key':'A03','offer_id':'33'})
            self.assertEqual(self.service.get(snapshot['id']),snapshot)
            self.assertFalse(path.exists())
        finally:
            renamed.rename(path)
