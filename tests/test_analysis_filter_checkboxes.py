"""票 03：确认屏「商品来源」「匹配状态」两个下拉改界面内勾选。

语义＝组内多选并集、两组之间且、全不勾不筛、与搜索词之间且；
联动＝改任一勾选或搜索词清空勾选、回第 1 页；命中成员浅绿、命中组整组展示。

浏览器用例走真实页面与本地接口，只替换模型与图片网络（沿用分组编辑夹具）。
"""
import unittest
from pathlib import Path

from playwright.sync_api import expect

import test_group_editing as fixture
from helpers import new_round
from test_matching import picture


class InlineFilterCheckboxTests(unittest.TestCase):
    setUp = fixture.GroupEditingTests.setUp
    stop_server = fixture.GroupEditingTests.stop_server
    submit = fixture.GroupEditingTests.submit
    dates = fixture.GroupEditingTests.dates
    seed = fixture.GroupEditingTests.seed
    switch_tab = fixture.GroupEditingTests.switch_tab
    model = fixture.GroupEditingTests.model

    def review(self):
        """选好日期、进入确认同款页；等快照信息落定，也就是这一趟运行已经结束。"""
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('日期区间')

    def box(self, name):
        return self.page.get_by_role('checkbox', name=name)

    def shown_groups(self):
        return self.page.locator('.group-choice').evaluate_all('nodes=>nodes.map(n=>n.dataset.group)')

    def snapshot_and_labels(self):
        """服务侧的事实：整份快照，与逐商品的匹配标签（供页面比对）。"""
        sid = self.page.url.split('analysis=')[1]
        snapshot = self.service.get(sid)
        label = {(p['shop_key'], p['offer_id']): p['match_label'] for p in snapshot['products']}
        return snapshot, label

    def groups_with_labels(self, *labels):
        snapshot, label = self.snapshot_and_labels()
        return sorted(g['id'] for g in snapshot['groups']
                      if any(label[(m['shop_key'], m['offer_id'])] in labels for m in g['members']))

    def group_id_for(self, offer):
        snapshot, _ = self.snapshot_and_labels()
        return next(g['id'] for g in snapshot['groups']
                    if any(m['offer_id'] == offer for m in g['members']))

    def changed_scene(self):
        """6 件商品：1-2-3 同款（M0）、4 / 5 / 6 各成一组；1 的图片换成蓝色 → 信息变更。

        事实（探针 03-probe-labels.py）：11=信息变更+唯一、22=新商品+唯一、
        33=新商品+多组、44/55=新商品+唯一、66=新商品+暂无。
        """
        self.model()
        self.service.start('2026-09-07', '2026-09-14')   # 先把红图写进证据账本
        new_round(self.db, 'A01', run_date='2026-09-07')
        self.submit('2026-09-14', 99, name='杯子1', image_evidence=picture('blue'))

    def seed_singletons(self, count):
        """再加 count 件互不相似的商品：每件一组，凑出跨页的分组列表。"""
        # 先落回 09-07：open 会给 09-14 那轮收尾，才换得动店铺范围（同 3012 用例的手法）。
        new_round(self.db, 'A01', run_date='2026-09-07')
        rid = new_round(self.db, *(f'A{i:02}' for i in range(1, 13)), run_date='2026-09-14')
        for i in range(count):
            offer = str(10000 + i)
            self.db.submit_inventory_snapshot(round_id=rid, shop_key=f'A{i % 12 + 1:02}',
                shop_url='https://shop.example', shop_name=f'店铺{i % 12 + 1}', offer_id=offer,
                product_url=f'https://detail.1688.com/offer/{offer}.html',
                list_title=f'规模商品{i:04}', detail_title=f'规模商品{i:04}', main_image_url='',
                sku_rows=[dict(sku_id='one', sku_name='标准', sku_stock=100)],
                collected_at='2026-09-14T04:00:00+00:00', attempt=1)

    def test_selects_give_way_to_checkbox_rows_with_the_note(self):
        self.seed()
        self.review()
        expect(self.page.locator('.review-filters select')).to_have_count(0)
        for name in ('新商品', '信息变更', '暂无匹配同款', '匹配唯一同款', '匹配多组同款'):
            expect(self.box(name)).to_be_visible()
            expect(self.box(name)).not_to_be_checked()
        expect(self.page.get_by_text('标签属于商品；同一商品同时符合两类条件才命中。命中后展示完整组，突出匹配成员。',
                                     exact=True)).to_be_visible()
        expect(self.page.locator('.group-choice')).to_have_count(4)

    def test_origin_and_label_rows_intersect_and_keep_whole_groups(self):
        self.changed_scene()
        self.review()
        expect(self.page.locator('.group-choice')).to_have_count(4)
        # 两组之间是且：信息变更 ∩ 匹配多组同款 没有同时成立的商品 → 一组不显示。
        self.box('信息变更').check()
        self.box('匹配多组同款').check()
        expect(self.page.locator('.group-choice')).to_have_count(0)
        # 换成新商品 ∩ 匹配多组同款 → 1-2-3 那组：整组 3 件都在，命中成员只有 33。
        self.box('信息变更').uncheck()
        self.box('新商品').check()
        group = self.group_id_for('33')
        self.assertEqual(self.shown_groups(), [group])
        expect(self.page.locator('#groupDetail .matching-member')).to_have_count(3)
        expect(self.page.locator('#groupDetail .filter-hit')).to_have_count(1)
        expect(self.page.locator('#groupDetail .filter-hit')).to_have_attribute('data-product', '33')
        Path('.scratch/work').mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path='.scratch/work/ticket03-filter-checkboxes.png')
        # 来源两项全勾与全不勾等价：都勾、都不勾，都还是同一组。
        self.box('信息变更').check()
        self.assertEqual(self.shown_groups(), [group])
        self.box('新商品').uncheck()
        expect(self.page.locator('.group-choice')).to_have_count(0)
        self.box('信息变更').uncheck()
        self.assertEqual(self.shown_groups(), [group])
        # 全不勾＝不筛：取消最后一道条件就回到全部 4 组。
        self.box('匹配多组同款').uncheck()
        expect(self.page.locator('.group-choice')).to_have_count(4)
        expect(self.page.locator('#groupDetail .filter-hit')).to_have_count(0)

    def test_labels_union_inside_the_row_and_intersect_with_search(self):
        self.changed_scene()
        self.review()
        # 组内并集：只勾「暂无匹配同款」的命中集合；再加上「匹配唯一同款」＝两集合的并。
        only_none = self.groups_with_labels('暂无匹配同款')
        self.box('暂无匹配同款').check()
        self.assertEqual(sorted(self.shown_groups()), only_none)
        expect(self.page.locator('.group-choice')).to_have_count(1)
        union = self.groups_with_labels('暂无匹配同款', '匹配唯一同款')
        self.box('匹配唯一同款').check()
        self.assertEqual(sorted(self.shown_groups()), union)
        expect(self.page.locator('.group-choice')).to_have_count(4)
        self.box('匹配唯一同款').uncheck()
        # 与搜索词之间是且：杯子4 命中 4 那组，但它没有「暂无匹配同款」的商品 → 空。
        self.page.locator('#reviewQuery').fill('杯子4')
        expect(self.box('暂无匹配同款')).to_be_checked()
        expect(self.page.locator('.group-choice')).to_have_count(0)
        self.page.locator('#reviewQuery').fill('')
        self.assertEqual(sorted(self.shown_groups()), only_none)

    def test_filter_change_clears_checked_groups_and_returns_to_first_page(self):
        self.seed_singletons(25)   # 连 setUp 里那件，共 26 件商品、26 组独立组
        self.review()
        expect(self.page.locator('#pageInfo')).to_have_text('1 / 2')
        self.page.get_by_role('button', name='下一页', exact=True).click()
        expect(self.page.locator('#pageInfo')).to_have_text('2 / 2')
        self.page.locator('#products input[type=checkbox]').first.check()
        expect(self.page.locator('#selectionCount')).to_have_text('已勾选 1 组')
        # 改勾选：清空勾选、回第 1 页；列表还有 26 组，不是被筛空的那种「第 1 页」。
        self.box('新商品').check()
        expect(self.page.locator('#pageInfo')).to_have_text('1 / 2')
        expect(self.page.locator('#selectionCount')).to_have_text('已勾选 0 组')
        expect(self.page.locator('.group-choice')).to_have_count(20)
        # 页签与筛选的组合关系不变：切页签不清勾选，筛选照旧生效。
        self.switch_tab('已确认')
        expect(self.box('新商品')).to_be_checked()
        expect(self.page.locator('.group-choice')).to_have_count(0)
        self.switch_tab('待确认')
        expect(self.page.locator('.group-choice')).to_have_count(20)
        # 改搜索词同样清空勾选；搜索与勾选并存时各管各的。
        self.page.locator('#products input[type=checkbox]').first.check()
        expect(self.page.locator('#selectionCount')).to_have_text('已勾选 1 组')
        self.page.locator('#reviewQuery').fill('规模商品')
        expect(self.page.locator('#selectionCount')).to_have_text('已勾选 0 组')
        expect(self.box('新商品')).to_be_checked()
        expect(self.page.locator('.group-choice')).to_have_count(20)
