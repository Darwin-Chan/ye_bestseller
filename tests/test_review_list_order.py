"""票 05：确认屏左列按组内商品数排序。

规则：组内商品数降序 → 并列按组总销量降序 → 再并列保持稳定。次序由服务端产出
（`review_order`，与结果屏名次同源、同更新时机），页面照单渲染、不自行排序；
结果屏名次与导出报告的组序不动（区间总销量降序、并列稳定）。

服务缝直接断言次序数据与「名次不受影响」；页面缝用真页面 + 真服务断言左列照单，
页签、搜索、筛选与 20 组/页分页都作用在这份次序上。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import test_analysis as fixture
import test_group_editing as editing
from playwright.sync_api import expect
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService, rank_groups
from bestseller_monitor.db import Database, connect
from helpers import submit_offer


# 商品在哪一组：组编辑用例里已有一份查找，别再造一份。
# 用例用它按规则自算期望次序，不抄服务端给的那份。
group_of = editing.GroupEditingTests.group_for


def product(shop, offer, sales):
    return {'shop_key': shop, 'offer_id': offer, 'sales': sales, 'points': [], 'skus': []}


def group(id, *members):
    return {'id': id, 'confirmed': False,
            'members': [{'shop_key': m['shop_key'], 'offer_id': m['offer_id']} for m in members]}


def member_of(snapshot, offer):
    return next(m for g in snapshot['groups'] for m in g['members'] if m['offer_id'] == offer)


class ReviewOrderDataTests(unittest.TestCase):
    """次序数据：成员数降序 → 并列销量降序 → 再并列稳定；名次与组集合次序都不动。"""

    def test_bigger_groups_first_then_higher_sales_within_the_same_size(self):
        small = [product('A01', '11', 10), product('A02', '22', 20)]   # G1：两件，30
        single = product('A03', '33', 500)                             # G2：一件，500
        big = [product('A04', '44', 30), product('A05', '55', 40)]     # G3：两件，70
        snapshot = {'products': small + [single] + big,
                    'groups': [group('G1', *small), group('G2', single), group('G3', *big)]}
        rank_groups(snapshot)
        self.assertEqual(snapshot['review_order'], ['G3', 'G1', 'G2'])
        # 结果屏名次仍是组总销量降序：左列次序不参与，也改不动它。
        self.assertEqual(snapshot['ranking'], ['G2', 'G3', 'G1'])

    def test_ties_keep_the_group_collection_order_and_repeat_identically(self):
        first = [product('A01', '11', 25), product('A02', '22', 25)]
        second = [product('A03', '33', 25), product('A04', '44', 25)]
        solo = product('A05', '55', 900)
        snapshot = {'products': first + second + [solo],
                    'groups': [group('G1', *first), group('G2', *second), group('G3', solo)]}
        rank_groups(snapshot)
        self.assertEqual(snapshot['review_order'], ['G1', 'G2', 'G3'])   # 并列：保持组集合次序
        rank_groups(snapshot)
        self.assertEqual(snapshot['review_order'], ['G1', 'G2', 'G3'])   # 重算两次一致
        # 两份次序各排各的：组集合自身与名次的并列稳定序都没被动过。
        self.assertEqual([g['id'] for g in snapshot['groups']], ['G1', 'G2', 'G3'])
        self.assertEqual(snapshot['ranking'], ['G3', 'G1', 'G2'])


class ReviewOrderServiceTests(unittest.TestCase):
    """人工动作后的重算与往返：编辑改次序，确认/撤回照算；保存读回还是这一份。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'inventory.db'
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES ('A01','店铺1')")
        self.conn.commit()
        # 四件商品各自成组（没有匹配服务时一件一组）：区间销量 10 / 20 / 70 / 60。
        for offer, name, closing in [('11', '月牙杯', 90), ('22', '云朵杯', 80),
                                     ('33', '树叶杯', 30), ('44', '贝壳杯', 40)]:
            submit_offer(self.db, offer, '2026-09-07', 100, name=name)
            submit_offer(self.db, offer, '2026-09-14', closing, name=name)
        self.service = self.open_service()

    def open_service(self):
        """一次「重开程序」：同一批磁盘文件、新的内存。"""
        return AnalysisService(AnalysisConfig(self.path), running=lambda: False)

    def test_edit_confirm_and_withdraw_recompute_the_order(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        # 起手都是单商品组：左列按销量降序（70 / 60 / 20 / 10）。
        self.assertEqual(snapshot['review_order'],
                         [group_of(snapshot, o)['id'] for o in ('33', '44', '22', '11')])
        # 人把销量最低的两件并成一组：成员数排在销量前面，它立刻升到最前。
        merged = self.service.edit_group(sid, 'move', group_of(snapshot, '11')['id'],
                                         member_of(snapshot, '11'), group_of(snapshot, '22')['id'])
        self.assertEqual(merged['review_order'],
                         [group_of(merged, '22')['id'], group_of(merged, '33')['id'],
                          group_of(merged, '44')['id']])
        # 确认与撤回不改成员数、不改销量：次序照算一遍，还是这一份。
        confirmed = self.service.confirm(sid, group_of(merged, '22')['id'])
        self.assertEqual(confirmed['review_order'], merged['review_order'])
        withdrawn = self.service.withdraw(sid, group_of(merged, '22')['id'])
        self.assertEqual(withdrawn['review_order'], merged['review_order'])

    def test_saved_draft_carries_the_order_and_earlier_drafts_get_it_on_restore(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        self.service.save_draft(sid)
        self.assertEqual(self.open_service().get(sid)['review_order'], snapshot['review_order'])
        # 本票之前存的草稿里没有这一份（或留着旧规则算的）：读回时按现行规则补算，存的次序不作数。
        for stale in (None, list(reversed(snapshot['review_order']))):
            conn = sqlite3.connect(self.service.config.store)
            try:
                payload = json.loads(conn.execute('SELECT payload FROM analysis_drafts').fetchone()[0])
                if stale is None:
                    payload.pop('review_order')
                else:
                    payload['review_order'] = stale
                conn.execute('UPDATE analysis_drafts SET payload=?',
                             (json.dumps(payload, ensure_ascii=False),))
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(self.open_service().get(sid)['review_order'], snapshot['review_order'])


class ReviewOrderBrowserTests(unittest.TestCase):
    """真页面 + 真服务：左列照服务端给的次序渲染（页面不排序），筛选与分页都在它上面。"""

    setUp = fixture.AnalysisBrowserTests.setUp
    stop_server = fixture.AnalysisBrowserTests.stop_server
    submit = fixture.AnalysisBrowserTests.submit
    dates = fixture.AnalysisBrowserTests.dates
    switch_tab = fixture.AnalysisBrowserTests.switch_tab

    def seed(self, count):
        """count 件商品各自成组（offer 从 '001' 起，躲开夹具自带那件 '11'）：降幅＝i，销量＝i。"""
        for i in range(1, count + 1):
            shop = f'A{i % 12 + 1:02}'
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 100 - i)]:
                submit_offer(self.db, f'{i:03}', day, stock, name=f'杯子{i:03}',
                             shop_key=shop, shop_name=f'店铺{int(shop[1:])}')

    def start_review(self):
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('日期区间')
        return self.page.url.split('analysis=')[1]

    def listed(self):
        return self.page.locator('.group-choice').evaluate_all('nodes=>nodes.map(n=>n.dataset.group)')

    def merge(self, snapshot, offer, target_offer):
        """把 offer 并进 target 所在的组（走服务的编辑口，页面刷新后照它渲染）。"""
        return self.service.edit_group(snapshot['id'], 'move',
                                       group_of(snapshot, offer)['id'], member_of(snapshot, offer),
                                       group_of(snapshot, target_offer)['id'])

    def test_browser_left_column_follows_the_server_order_through_search_and_tabs(self):
        self.seed(3)
        sid = self.start_review()
        # 销量最低的两件并成一组（2 件、销量 3）：成员数压过销量，它排到销量最高的商品前面；
        # 装配残留的次序也不是它（那件销量最高的商品本来就是第一组）。
        self.merge(self.service.get(sid), '001', '002')
        self.page.reload()
        snapshot = self.service.get(sid)
        merged, best, rest = (group_of(snapshot, o)['id'] for o in ('002', '11', '003'))
        expect(self.page.locator('#groupDetail h3')).to_have_text(f'{merged} · 2 个商品')
        self.assertEqual(self.listed(), [merged, best, rest])
        # 搜索命中组内一件商品：整组照旧展示，左列次序不变（票 03 的联动不受影响）。
        self.page.get_by_label('搜索分组商品', exact=True).fill('杯子002')
        self.assertEqual(self.listed(), [merged])
        expect(self.page.locator('#groupDetail .matching-member')).to_have_count(2)
        self.page.get_by_label('搜索分组商品', exact=True).fill('')
        self.assertEqual(self.listed(), [merged, best, rest])
        # 确认与撤回后次序即时正确：并组升到最前，撤回后仍是最前。
        # 确认／撤回是异步请求：等列表真的少了／多了那一组再看次序，别抢在重画前面读。
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        expect(self.page.locator('.group-choice')).to_have_count(2)
        self.assertEqual(self.listed(), [best, rest])
        self.switch_tab('已确认')
        self.page.get_by_role('button', name='撤回当前分组').click()
        self.switch_tab('待确认')
        expect(self.page.locator('.group-choice')).to_have_count(3)
        self.assertEqual(self.listed(), [merged, best, rest])

    def test_browser_paging_slices_the_new_order(self):
        self.seed(22)
        sid = self.start_review()
        # 销量最低的一件并进夹具自带那件（2 件、销量 21）：新次序里它是第一组，按销量排它排不上。
        self.merge(self.service.get(sid), '001', '11')
        self.page.reload()
        snapshot = self.service.get(sid)
        merged = group_of(snapshot, '11')['id']
        # 其余是单商品组：销量 22 … 2 降序（夹具自带那件已并进第一组，不在其中）。
        descending = [group_of(snapshot, f'{i:03}')['id'] for i in range(22, 1, -1)]
        expect(self.page.locator('#pageInfo')).to_have_text('1 / 2')
        self.assertEqual(self.listed(), [merged] + descending[:19])
        self.page.get_by_role('button', name='下一页', exact=True).click()
        expect(self.page.locator('#pageInfo')).to_have_text('2 / 2')
        self.assertEqual(self.listed(), descending[19:])
