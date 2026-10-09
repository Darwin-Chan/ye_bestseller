"""Issue 07 票 01：旧快照同步回归。

固定「页面先读到初始快照、后台直接改状态并保存」的顺序，用 `snapshot_sync` 的
一次刷新 + S/R/U 核对把页面同步到保存后状态，再走原有业务断言。修复的是测试时序
（页面不会自动感知后台改写，spec 已确认是测试竞态），产品代码不动。

红证据（同一顺序不补刷新时原「畅销品」断言必红）在票 01 证据目录的受控重放里；
本用例只保留同步后的正向路径，不再依赖「后台保存恰好赶在页面读取之前」。
"""
import sys
import unittest
from pathlib import Path

# test_analysis / test_e2e_acceptance 等测试模块以裸名互相 import，测试目录需在 sys.path 上。
sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import expect

from helpers import new_round
from test_matching import picture
import test_e2e_acceptance as e2e
from snapshot_sync import sync_after_save, wait_initial_presentation


class OldSnapshotSyncTests(unittest.TestCase):
    """两店各一商品、两个日期：页面先呈现初始未确认分组，后台并组确认保存，再同步复核。"""

    setUp = e2e.EndToEndAcceptanceTests.setUp
    stop_server = e2e.EndToEndAcceptanceTests.stop_server
    restart_service = e2e.EndToEndAcceptanceTests.restart_service
    submit = e2e.EndToEndAcceptanceTests.submit  # 夹具 setUp 用它铺底数
    switch_tab = e2e.EndToEndAcceptanceTests.switch_tab
    back_to_review = e2e.EndToEndAcceptanceTests.back_to_review
    use_model = e2e.EndToEndAcceptanceTests.use_model
    drop_fixture_product = e2e.EndToEndAcceptanceTests.drop_fixture_product
    group_with = e2e.EndToEndAcceptanceTests.group_with
    dates = e2e.EndToEndAcceptanceTests.dates

    def test_backend_save_then_one_reload_reaches_results_page(self):
        """页面持旧快照时后台并组、确认并保存；一次刷新 + S/R/U 核对后进入「畅销品」，
        呈现的组、成员、确认状态与销量都对应这次保存。"""
        # 名字不同，模型不会自动并组；同款关系留给人工确认（本用例按既定决定仍走后台代办）。
        self.use_model(names=['同款杯1', '异名杯'])
        self.drop_fixture_product()
        for shop, name in [('A01', '同款杯1'), ('A02', '异名杯')]:
            offer = f'{shop}1'
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 95)]:
                rid = new_round(self.db, shop, run_date=day)
                self.db.submit_inventory_snapshot(
                    round_id=rid, shop_key=shop, shop_url='https://shop.example',
                    shop_name=f'店铺{shop}', offer_id=offer,
                    product_url=f'https://detail.1688.com/offer/{offer}.html',
                    list_title=name, detail_title=name,
                    main_image_url='', image_evidence=picture('red'),
                    sku_rows=[{'sku_id': 'std', 'sku_name': '标准', 'sku_stock': stock}],
                    collected_at=f'{day}T04:00:00+00:00', attempt=1)

        sid = self.dates()
        # 稳定时序：等页面真正呈现初始未确认分组；此刻页面持有的就是后台修改前的快照。
        wait_initial_presentation(self.page, products=2, groups=2, pending=2)

        snapshot = self.service.get(sid)
        pair = self.group_with(snapshot, 'A011')
        other = self.group_with(snapshot, 'A021')
        self.assertNotEqual(pair['id'], other['id'])
        self.service.edit_group(sid, 'move', other['id'], other['members'][0], pair['id'])
        self.service.confirm(sid, pair['id'])
        # 保存返回就是本次同步的独立预期；成员与确认状态按它先核对一遍。
        saved = self.service.save_draft(sid)
        saved_pair = next(g for g in saved['groups'] if g['id'] == pair['id'])
        self.assertEqual({m['offer_id'] for m in saved_pair['members']}, {'A011', 'A021'})
        self.assertTrue(saved_pair['confirmed'])

        fetched = sync_after_save(self.page, sid, saved)

        # 原有业务断言不下移、不弱化：结果屏真的在，短区间数值随呈现仍成立。
        expect(self.page.get_by_role('heading', name='畅销品', exact=True)).to_be_visible()
        reused = self.group_with(fetched, 'A011')
        self.assertEqual({m['offer_id'] for m in reused['members']}, {'A011', 'A021'})
        self.assertTrue(reused['confirmed'])
        # 短区间销量 = 各 (100-95) 的 5，两件合组 10。
        self.assertEqual(reused['sales'], 10)
        sales = {(p['shop_key'], p['offer_id']): p['sales'] for p in fetched['products']}
        self.assertEqual(sales[('A01', 'A011')], 5)
        self.assertEqual(sales[('A02', 'A021')], 5)

        # U 的「返回确认页核对」：正常返回操作后，已确认页签下这组与两名成员都在页面上。
        self.back_to_review()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('2 个商品')
        self.switch_tab('已确认')
        expect(self.page.locator('.group-choice')).to_have_count(1)
        self.page.locator('.group-choice').first.click()
        detail = self.page.locator('#groupDetail')
        expect(detail).to_contain_text('2 个商品')
        expect(detail).to_contain_text('同款杯1')
        expect(detail).to_contain_text('异名杯')
        expect(detail).to_contain_text('同款组销量 10')
        expect(detail).to_contain_text('已确认')


if __name__ == '__main__':
    unittest.main()
