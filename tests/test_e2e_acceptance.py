"""票 13：完整浏览器流程与真实样本验收（A01—A46）。

本票只补「真实组合及规模」验收：用实际 HTML、本地分析接口和临时数据库把前序切片
串起来跑一遍，并核对 12 家店铺 / 3,012 商品的规模行为。A01—A46 里各票主责的数值与
操作边界已由主责票自己的测试覆盖（test_analysis / test_matching / test_group_editing /
test_review_at_scale / test_bestseller_ranking / test_offline_report / test_analysis_drafts /
test_analysis_reuse），这里不重写；逐条对照见 docs/ops/畅销品分析验收记录.md。

只有外部模型与图像网络用可控替身（test_matching 的 ModelTransport），页面、分析服务、
草稿库和临时库存库都是真的。
"""
import dataclasses
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# test_analysis / helpers 等测试模块以裸名互相 import，测试目录需在 sys.path 上。
sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import expect

from helpers import new_round
from test_matching import ModelTransport, picture
import test_analysis as fixture
from bestseller_monitor.analysis import AnalysisService
from bestseller_monitor.matching import MatchingConfig, MatchingService


class EndToEndAcceptanceTests(unittest.TestCase):
    """端到端验收：真实页面 + 真实服务 + 临时库，模型与图像网络可控替换。"""

    setUp = fixture.AnalysisBrowserTests.setUp
    stop_server = fixture.AnalysisBrowserTests.stop_server
    restart_service = fixture.AnalysisBrowserTests.restart_service
    submit = fixture.AnalysisBrowserTests.submit  # 夹具 setUp 用它铺底数

    # ---------------------------------------------------------------- 夹具

    def seed(self, shops=('A01', 'A02'), products=4):
        """铺两个店铺的跨店同款候选：同款靠名称与图片证据交给模型判断。

        店铺 A01 / A02 各放同样的商品名，图片颜色相同；这不构成「已经同款」的结论，
        模型建议与人工确认才是。
        """
        for index in range(1, products + 1):
            for shop in shops:
                offer = f'{shop}{index}'
                for day, stock in [('2026-09-07', 100), ('2026-09-14', 100 - index * 5)]:
                    rid = new_round(self.db, shop, run_date=day)
                    self.db.submit_inventory_snapshot(
                        round_id=rid, shop_key=shop, shop_url='https://shop.example',
                        shop_name=f'店铺{shop}', offer_id=offer,
                        product_url=f'https://detail.1688.com/offer/{offer}.html',
                        list_title=f'同款杯{index}', detail_title=f'同款杯{index}',
                        main_image_url='', image_evidence=picture('red'),
                        sku_rows=[{'sku_id': 'std', 'sku_name': '标准', 'sku_stock': stock}],
                        collected_at=f'{day}T04:00:00+00:00', attempt=1)

    def use_model(self, products=None, same=(), names=None):
        """把模型换成固定响应：`same` 里的名字对判为同款，其余跨名对判为不同。

        给了 `names` 就直接用它；否则按 `products` 生成「同款杯N」。

        同名对（跨店同名）不在枚举里，走 ModelTransport 的图片比较：本夹具给所有商品
        同一张图，因此跨店同名商品会被判为同款——这正是「同款靠名称与图片证据合并」。
        需要「模型没说同款、留给人工判断」时，把其中一个改成别的名字并显式判 False。
        """
        if names is None:
            names = [f'同款杯{i}' for i in range(1, (products or 0) + 1)]
        transport = ModelTransport()
        wanted = {tuple(sorted(pair)) for pair in same}
        transport.decisions = {
            tuple(sorted((names[i], names[j]))): tuple(sorted((names[i], names[j]))) in wanted
            for i in range(len(names)) for j in range(i + 1, len(names))}
        self.service.matcher = MatchingService(
            MatchingConfig(Path(self.tmp.name) / 'match.sqlite', mode='direct'))
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=transport).start()
        return transport

    def dates(self, start='2026-09-07', end='2026-09-14'):
        """选日期并进入同款确认，等页面真正落到某次分析后返回它的分析标识。"""
        self.page.get_by_label('开始日期', exact=True).fill(start)
        self.page.get_by_label('结束日期', exact=True).fill(end)
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        self.page.wait_for_function("() => location.hash.startsWith('#analysis=')")
        return self.page.url.split('analysis=')[1]

    def drop_fixture_product(self):
        """夹具 setUp 铺的底数（offer_id='11'）不属于验收样本，去掉免得混进计数。"""
        self.conn.execute("DELETE FROM inventory WHERE offer_id='11'")
        self.conn.commit()

    def group_with(self, snapshot, offer):
        return next(g for g in snapshot['groups'] if any(m['offer_id'] == offer for m in g['members']))

    def export_to(self, directory):
        """把导出目录指到用例自己的临时目录，别污染仓库 output/。"""
        self.service.config = dataclasses.replace(self.service.config, output=directory)

    # ------------------------------------------------------- 组合：演示路径

    def test_demo_path_select_dates_to_offline_report(self):
        """演示路径一次走完：选日期 → 固定数据 → 模型建议 → 对比纠错 → 批量确认与撤回
        → 暂存 → 关闭重开 → 保存排名 → 离线报告。

        组合证据：每一步的真实页面状态都由上一步在同一个分析快照上留下，最终报告里
        的代表商品、组总销量与构成树都来自这一份快照。
        """
        self.seed()
        # 同款杯1／2／3 互相判为同款（跨店同名也并入），同款杯4 只与同款杯3 同款：
        # 于是同款杯4 自成一组、候选指向那一组，正好用来走「对比纠错」。
        transport = self.use_model(4, same={('同款杯1', '同款杯2'), ('同款杯1', '同款杯3'),
                                            ('同款杯2', '同款杯3'), ('同款杯3', '同款杯4')})
        out = Path(self.tmp.name) / 'output'
        self.export_to(out)
        self.drop_fixture_product()

        # 选日期：两日真实抓取数量先在页面上核对。
        self.page.get_by_label('开始日期', exact=True).fill('2026-09-07')
        self.page.get_by_label('结束日期', exact=True).fill('2026-09-14')
        expect(self.page.get_by_role('table', name='开始日期真实抓取').locator('tbody tr')).to_have_count(12)
        self.page.get_by_role('button', name='下一步、进入同款确认').click()

        # 固定数据：进入待确认列表，模型给出的建议仍是待确认。
        expect(self.page.locator('#snapshotInfo')).to_contain_text('数据固定于')
        expect(self.page.locator('#snapshotInfo')).to_contain_text('待确认')
        sid = self.page.url.split('analysis=')[1]
        snapshot = self.service.get(sid)
        self.assertEqual(len(snapshot['products']), 8)
        self.assertTrue(all(not g['confirmed'] for g in snapshot['groups']))

        # 对比纠错：挑一件模型给了候选组的商品，从它的候选里并入已有组。
        owner = {m['offer_id']: g['id'] for g in snapshot['groups'] for m in g['members']}
        with_candidates = [p for p in snapshot['products']
                           if any(c != owner[p['offer_id']] for c in (p.get('candidate_groups') or []))]
        self.assertTrue(with_candidates, '模型没有给出任何候选组，对比纠错这一步无从进行')
        target = with_candidates[0]
        self.page.locator(f'.group-choice[data-group="{owner[target["offer_id"]]}"]').click()
        self.page.locator(f'#groupDetail [data-product="{target["offer_id"]}"] aside button').first.click()
        dialog = self.page.get_by_role('dialog', name='其他疑似归组', exact=True)
        expect(dialog).to_be_visible()
        dialog.locator('input[type=radio]').first.check()
        expect(dialog.locator('#candidateStatus')).to_contain_text('已选')
        dialog.get_by_role('button', name='加入选中组').click()
        expect(dialog).not_to_be_visible()

        # 批量确认：一次确认当前筛选下的全部待确认组，文案给出实际组数与商品数。
        after_join = self.service.get(sid)
        pending = [g for g in after_join['groups'] if not g['confirmed']]
        members = sum(len(g['members']) for g in pending)
        self.page.get_by_role('button', name='确认当前筛选全部组').click()
        bulk = self.page.get_by_role('dialog', name='确认同款商品分组', exact=True)
        expect(bulk.locator('p').first).to_have_text(
            f'将确认{len(pending)}个同款分组，包含{members}个商品。')
        bulk.get_by_role('button', name='确认', exact=True).click()
        self.assertTrue(all(g['confirmed'] for g in self.service.get(sid)['groups']))

        # 撤回：到「已确认」页签撤回一组，它回到待确认并且草稿标记未保存。
        self.page.get_by_role('tab', name='已确认', exact=True).click()
        self.page.get_by_role('button', name='撤回当前分组').click()
        expect(self.page.locator('#dirtyStatus')).to_have_text('未保存')

        # 暂存：有待确认组也能保存，保存后给出成功时间，草稿不再标记未保存。
        self.page.get_by_role('button', name='暂时保存').click()
        expect(self.page.locator('#saveStatus')).to_contain_text('已保存', timeout=10000)
        expect(self.page.locator('#dirtyStatus')).to_have_text('')

        # 关闭重开：销毁旧服务与旧进程内存，再从同一份磁盘文件恢复。
        self.restart_service()
        expect(self.page.get_by_role('button', name='继续上次分析')).to_be_visible()
        self.page.get_by_role('button', name='继续上次分析').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('8商品')
        # 撤回过的组恢复后仍是待确认，保存没有被当成审核完成。
        self.page.get_by_role('tab', name='待确认', exact=True).click()
        expect(self.page.locator('.group-choice')).to_have_count(1)

        # 保存排名：仍有待确认时不能直接进结果页。
        expect(self.page.get_by_role('button', name='保存分组并查看畅销品')).to_be_disabled()
        expect(self.page.locator('#ranking')).to_be_hidden()
        self.page.get_by_role('tab', name='全部', exact=True).click()
        self.page.get_by_role('button', name='确认当前筛选全部组').click()
        self.page.get_by_role('dialog', name='确认同款商品分组', exact=True).get_by_role(
            'button', name='确认', exact=True).click()
        self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        final = self.service.get(sid)
        expect(self.page.locator('#ranking .rank')).to_have_count(len(final['groups']))

        # 离线报告：导出后停掉本地服务，报告仍可读。
        self.page.get_by_role('button', name='导出 HTML 报告').click()
        expect(self.page.locator('#exportStatus')).to_contain_text('已导出', timeout=30000)
        report = Path(self.page.locator('#exportStatus').inner_text().split('·', 1)[1].strip())
        self.assertEqual(report.parent, out)
        self.assertIn('2026-09-07_2026-09-14', report.name)

        self.stop_server()
        self.page.goto(report.as_uri())
        expect(self.page.get_by_role('heading', name='畅销品分析报告')).to_be_visible()
        expect(self.page.locator('#ranking .rank')).to_have_count(len(final['groups']))
        # 折叠的排名行在离线报告里仍能展开，图与图例随报告内嵌。
        first = self.page.locator('#ranking .rank').first
        first.locator('summary').first.click()
        expect(first.locator('svg').first).to_be_visible()
        expect(first.locator('.chart-key').first).to_be_visible()
        content = report.read_text(encoding='utf-8')
        for forbidden in ['DEEPSEEK_API_KEY', '/api/', '127.0.0.1']:
            self.assertNotIn(forbidden, content)
        # 模型只被真实调用过：报告不再需要它，人工确认保持了模型建议的分组。
        self.assertTrue(transport.calls)

    # ------------------------------------------------- 组合：同一份快照口径

    def test_one_snapshot_feeds_sales_representative_tree_and_charts(self):
        """结果同时含规格切换、缺失、补货与跨店合并，且代表商品、组总销量、
        构成树和双轴图都来自同一份固定快照（规格 §14「本分析内一致」）。
        """
        # 模型没说这两件是同款（名字不同），跨店合并留给人工判断。
        self.use_model(names=['同款杯1', '异名杯'])
        self.drop_fixture_product()
        # A02 那件：正常下降，用于跨店合并。
        for day, stock in [('2026-09-07', 60), ('2026-09-14', 45)]:
            rid = new_round(self.db, 'A02', run_date=day)
            self.db.submit_inventory_snapshot(
                round_id=rid, shop_key='A02', shop_url='https://shop.example',
                shop_name='店铺A02', offer_id='A021',
                product_url='https://detail.1688.com/offer/A021.html',
                list_title='异名杯', detail_title='异名杯', main_image_url='',
                image_evidence=picture('red'),
                sku_rows=[{'sku_id': 'std', 'sku_name': '标准', 'sku_stock': stock}],
                collected_at=f'{day}T04:00:00+00:00', attempt=1)
        # A01 这件：单规格 → 多规格（形态切换），中间缺 9 日，10 日补货。
        for day, rows in [
            ('2026-09-07', [('默认', 100)]),
            ('2026-09-08', [('默认', 90)]),
            ('2026-09-10', [('白色', 40), ('米色', 60)]),
            ('2026-09-14', [('白色', 30), ('米色', 50)]),
        ]:
            rid = new_round(self.db, 'A01', run_date=day)
            self.db.submit_inventory_snapshot(
                round_id=rid, shop_key='A01', shop_url='https://shop.example',
                shop_name='店铺A01', offer_id='A011',
                product_url='https://detail.1688.com/offer/A011.html',
                list_title='同款杯1', detail_title='同款杯1', main_image_url='',
                image_evidence=picture('red'),
                sku_rows=[{'sku_id': key, 'sku_name': key, 'sku_stock': stock}
                          for key, stock in rows],
                collected_at=f'{day}T04:00:00+00:00', attempt=1)

        sid = self.dates()
        snapshot = self.service.get(sid)
        # 模型给的是两个单商品组，人工把它们并为同一个跨店同款组。
        pair = self.group_with(snapshot, 'A011')
        other = self.group_with(snapshot, 'A021')
        self.assertNotEqual(pair['id'], other['id'])
        self.service.edit_group(sid, 'move', other['id'], other['members'][0], pair['id'])
        self.service.confirm(sid, pair['id'])
        # 暂存后重开程序，从「继续上次分析」拿回这份已确认的分析再进结果页。
        self.service.save_and_view(sid)
        self.restart_service()
        self.page.get_by_role('button', name='继续上次分析').click()
        self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        expect(self.page.locator('#ranking')).to_be_visible()

        # 组总销量是两名成员销量的和，不是代表商品自己的销量。
        frozen = self.service.get(sid)
        group = next(g for g in frozen['groups'] if g['id'] == pair['id'])
        sales = {(p['shop_key'], p['offer_id']): p['sales'] for p in frozen['products']}
        self.assertEqual(len(group['members']), 2)
        self.assertEqual(group['sales'],
                         sum(sales[(m['shop_key'], m['offer_id'])] for m in group['members']))
        self.assertEqual(group['sales'], 45)  # A011 形态切换后 30 + A021 正常下降 15
        ranked = self.page.locator(f'#ranking details[data-group="{pair["id"]}"]')
        expect(ranked).to_be_visible()
        expect(ranked.locator('summary').first).to_contain_text('45')

        # 展开：同一份快照落成的构成树与图表，商品图片与商品/SKU 销量都在。
        ranked.locator('summary').first.click()
        expect(ranked.locator('svg').first).to_be_visible()
        expect(ranked).to_contain_text('同款杯1')
        expect(ranked).to_contain_text('异名杯')

    # ------------------------------------------------------------- 规模验收

    def test_scale_review_stays_usable_at_3012_products_across_pages(self):
        """12 家店铺 × 251 商品 = 3,012：覆盖表、分页、跨页勾选与批量确认都可用。

        规模是这个用例唯一的目的；数值边界由主责票覆盖。
        """
        self.drop_fixture_product()
        for shop_idx in range(1, 13):
            shop = f'A{shop_idx:02}'
            # 轮次按 (店铺, 日期) 建一次就够；按商品逐条建会白白多出六千个轮次。
            rounds = {day: new_round(self.db, shop, run_date=day)
                      for day in ('2026-09-07', '2026-09-14')}
            for i in range(251):
                offer = f'{shop_idx * 10000 + i}'
                name = f'规模商品{int(offer):06d}'
                for day, stock in [('2026-09-07', 100), ('2026-09-14', 100 - i % 50)]:
                    self.db.submit_inventory_snapshot(
                        round_id=rounds[day], shop_key=shop, shop_url='https://shop.example',
                        shop_name=f'店铺{shop_idx}', offer_id=offer,
                        product_url=f'https://detail.1688.com/offer/{offer}.html',
                        list_title=name, detail_title=name,
                        main_image_url='', image_evidence=picture('red'),
                        sku_rows=[{'sku_id': 'std', 'sku_name': '标准', 'sku_stock': stock}],
                        collected_at=f'{day}T04:00:00+00:00', attempt=1)

        self.page.get_by_label('开始日期', exact=True).fill('2026-09-07')
        self.page.get_by_label('结束日期', exact=True).fill('2026-09-14')
        expect(self.page.get_by_role('table', name='开始日期真实抓取').locator('tbody tr')).to_have_count(12)
        self.page.get_by_role('button', name='下一步、进入同款确认').click()

        expect(self.page.locator('#snapshotInfo')).to_contain_text('3012商品', timeout=30000)
        expect(self.page.locator('.group-choice')).to_have_count(20)

        # 搜索覆盖全量而不是当前页：最后一页的商品也能直接命中它的整组。
        self.page.get_by_label('搜索分组商品', exact=True).fill('规模商品030250')
        expect(self.page.locator('.group-choice')).to_have_count(1)
        expect(self.page.locator('#groupDetail')).to_contain_text('规模商品030250')
        self.page.get_by_label('搜索分组商品', exact=True).fill('')
        expect(self.page.locator('.group-choice')).to_have_count(20)

        # 跨页勾选：翻页后旧的勾选保留，批量确认覆盖两页选中的完整组。
        self.page.get_by_role('checkbox').first.check()
        self.page.get_by_role('button', name='下一页', exact=True).click()
        expect(self.page.locator('#pageInfo')).to_contain_text('2')
        self.page.get_by_role('checkbox').first.check()
        self.page.get_by_role('button', name='上一页', exact=True).click()
        expect(self.page.get_by_role('checkbox').first).to_be_checked()
        self.page.get_by_role('button', name='确认勾选组').click()
        bulk = self.page.get_by_role('dialog', name='确认同款商品分组', exact=True)
        expect(bulk.locator('p').first).to_have_text('将确认2个同款分组，包含2个商品。')
        bulk.get_by_role('button', name='确认', exact=True).click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('2待确认', timeout=10000)

    # --------------------------------------------- 组合：草稿四态与新区间复用

    def test_new_range_reuses_confirmation_in_the_browser_and_recomputes_sales(self):
        """换到更长的日期区间：页面按新数据重算销量，适用的已保存人工确认自动复用，
        不必重新确认（A37 的组合证据；服务层边界由 test_analysis_reuse 覆盖）。"""
        # 名字不同，模型不会自动并组；同款关系留给人工确认。
        self.use_model(names=['同款杯1', '异名杯'])
        self.drop_fixture_product()
        # 两个店铺各一件；09-21 的库存不同，用来区分两件商品的销量。
        for shop, name, closing in [('A01', '同款杯1', 75), ('A02', '异名杯', 65)]:
            offer = f'{shop}1'
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 95), ('2026-09-21', closing)]:
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
        snapshot = self.service.get(sid)
        pair = self.group_with(snapshot, 'A011')
        other = self.group_with(snapshot, 'A021')
        self.assertNotEqual(pair['id'], other['id'])
        self.service.edit_group(sid, 'move', other['id'], other['members'][0], pair['id'])
        self.service.confirm(sid, pair['id'])
        self.service.save_draft(sid)
        first_sales = next(g for g in self.service.get(sid)['groups'] if g['id'] == pair['id'])['sales']

        # 页面上换一个更长的区间：销量按新数据重算。
        self.page.get_by_role('button', name='重新选择日期').click()
        second_sid = self.dates(end='2026-09-21')
        self.assertNotEqual(second_sid, sid)
        second = self.service.get(second_sid)
        reused = self.group_with(second, 'A011')
        self.assertEqual({m['offer_id'] for m in reused['members']}, {'A011', 'A021'})
        self.assertTrue(reused['confirmed'])
        # 新期间销量 = 100→95 的 5，加上 95→75 的 20、95→65 的 30。
        sales = {(p['shop_key'], p['offer_id']): p['sales'] for p in second['products']}
        self.assertEqual(reused['sales'],
                         sum(sales[(m['shop_key'], m['offer_id'])] for m in reused['members']))
        self.assertNotEqual(reused['sales'], first_sales)
        # 旧分析仍保留自己的区间与销量，没被新分析改写。
        original = self.service.get(sid)
        self.assertEqual(original['end'], '2026-09-14')
        self.assertEqual(next(g for g in original['groups'] if g['id'] == pair['id'])['sales'],
                         first_sales)
        # 页面上看到的是复用后的已确认组。
        expect(self.page.locator('#snapshotInfo')).to_contain_text('2商品')
        self.page.get_by_role('tab', name='已确认', exact=True).click()
        expect(self.page.locator('.group-choice')).to_have_count(1)

    def test_draft_holding_all_four_states_survives_restart_and_source_changes(self):
        """一份草稿同时含已确认、待确认、排除与撤回，暂存后终止应用、再改源库存；
        重开恢复的仍是旧快照与完整人工进度（A35—A36 的组合证据）。"""
        self.seed(products=4)
        sid = self.dates()
        snapshot = self.service.get(sid)

        # 已确认：把 A02 的同款杯1 并入 A01 的同款杯1 后确认。
        first = self.group_with(snapshot, 'A011')
        moved = self.group_with(snapshot, 'A021')
        self.service.edit_group(sid, 'move', moved['id'], moved['members'][0], first['id'])
        self.service.confirm(sid, first['id'])

        # 排除：把同款杯2 从多成员组里移出，记下与原组其他成员的排除关系。
        source = self.group_with(self.service.get(sid), 'A022')
        second = self.group_with(self.service.get(sid), 'A012')
        self.service.edit_group(sid, 'move', second['id'], second['members'][0], source['id'])
        removed = self.service.edit_group(sid, 'remove', source['id'], second['members'][0])
        self.assertTrue(removed['excluded'])

        # 撤回：确认同款杯3 再撤回，草稿里保留一个待确认组。
        third = self.group_with(removed, 'A013')
        self.service.confirm(sid, third['id'])
        self.service.withdraw(sid, third['id'])

        # 暂存，然后终止整个应用并改动源库存与商品图。
        self.service.save_draft(sid)
        baseline = self.service.get(sid)
        self.stop_server()
        rid = new_round(self.db, 'A01', run_date='2026-09-20')
        self.db.submit_inventory_snapshot(
            round_id=rid, shop_key='A01', shop_url='https://shop.example',
            shop_name='店铺A01', offer_id='A011',
            product_url='https://detail.1688.com/offer/A011.html',
            list_title='改名后的杯', detail_title='改名后的杯', main_image_url='',
            image_evidence=picture('blue'),
            sku_rows=[{'sku_id': 'std', 'sku_name': '标准', 'sku_stock': 1}],
            collected_at='2026-09-20T04:00:00+00:00', attempt=1)

        # 重开：换一个进程与内存，仍读同一份磁盘文件。
        self.service = AnalysisService(self.service.config, running=lambda: False)
        restored = self.service.get(sid)
        self.assertEqual(restored['start'], baseline['start'])
        self.assertEqual(restored['end'], baseline['end'])
        self.assertEqual(restored['products'], baseline['products'])
        self.assertEqual(restored['inventory'], baseline['inventory'])
        self.assertEqual(restored['excluded'], baseline['excluded'])
        # 四态都在：确认状态、待确认、排除关系、撤回后的待确认。
        by_id = {g['id']: g for g in restored['groups']}
        self.assertTrue(by_id[first['id']]['confirmed'])
        self.assertFalse(by_id[third['id']]['confirmed'])
        self.assertTrue(restored['excluded'])
        self.assertNotEqual(restored.get('dirty'), True)


if __name__ == '__main__':
    unittest.main()
