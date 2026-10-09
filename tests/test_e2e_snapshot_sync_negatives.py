"""Issue 07 票 03：保存后同步契约的可诊断反例（AC03–AC08；AC16 负例侧）。

正向两场景证明同步成立（票 01 旧快照回归、票 02 完整换区间）；本文件只做反例：每个
用例在测试边界构造一种「保存后同步不该成立」的状态，断言它恰好在该层级被拒绝（精确
匹配 `SyncFailure.phase` 与消息关键字段），不让任何一层吞并其他层级，也不把不同层级
混成一次标题超时。故障控制只存在于测试边界（`page.route` 拦截、`add_init_script`
注入与直接传入快照），不新增产品开关；产品代码与 `snapshot_sync` 辅助均为零改动。

各用例的预期失败层级：

- `test_held_snapshot_get...`       → R(读取)：快照 GET 被扣住未放行，不宣告同步成功
- `test_replaying_old...`           → R(读取)：200＋同分析号也不够，内容不符
- `test_other_analysis_response...` → R(读取)：别的分析/区间的响应不能当本次证据
- `test_never_saved...`             → S(保存)：初始 dirty=false 顶不了保存事实
- `test_saved_with_pending...`      → U(落屏)：保存/读取真实成立，结果页门禁不成立
- `test_network_ok_but_page...`     → U(呈现)：网络正确但页面未应用
"""
import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# test_analysis / helpers 等测试模块以裸名互相 import，测试目录需在 sys.path 上。
sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import expect

from helpers import new_round
from test_matching import picture
import test_e2e_acceptance as e2e
from snapshot_sync import SyncFailure, sync_after_save, wait_initial_presentation


class SyncContractNegativeTests(unittest.TestCase):
    """反例共用 e2e 夹具（真实页面＋真实服务＋临时库存/草稿库），模型仍用固定替身。"""

    setUp = e2e.EndToEndAcceptanceTests.setUp
    stop_server = e2e.EndToEndAcceptanceTests.stop_server
    restart_service = e2e.EndToEndAcceptanceTests.restart_service
    submit = e2e.EndToEndAcceptanceTests.submit
    switch_tab = e2e.EndToEndAcceptanceTests.switch_tab
    back_to_review = e2e.EndToEndAcceptanceTests.back_to_review
    use_model = e2e.EndToEndAcceptanceTests.use_model
    drop_fixture_product = e2e.EndToEndAcceptanceTests.drop_fixture_product
    group_with = e2e.EndToEndAcceptanceTests.group_with
    dates = e2e.EndToEndAcceptanceTests.dates

    # ---------------------------------------------------------------- 夹具

    def seed_products(self, rows):
        """按 (店铺, 商品号, 名称, 起始库存, 结束库存) 铺 09-07/09-14 两天的观测。"""
        for shop, offer, name, first, second in rows:
            for day, stock in [('2026-09-07', first), ('2026-09-14', second)]:
                rid = new_round(self.db, shop, run_date=day)
                self.db.submit_inventory_snapshot(
                    round_id=rid, shop_key=shop, shop_url='https://shop.example',
                    shop_name=f'店铺{shop}', offer_id=offer,
                    product_url=f'https://detail.1688.com/offer/{offer}.html',
                    list_title=name, detail_title=name,
                    main_image_url='', image_evidence=picture('red'),
                    sku_rows=[{'sku_id': 'std', 'sku_name': '标准', 'sku_stock': stock}],
                    collected_at=f'{day}T04:00:00+00:00', attempt=1)

    def seed_pair(self):
        """与票 01 同一夹具：两店各一商品，名字不同（跨店同款关系留给人工确认）。"""
        self.use_model(names=['同款杯1', '异名杯'])
        self.drop_fixture_product()
        self.seed_products([('A01', 'A011', '同款杯1', 100, 95),
                            ('A02', 'A021', '异名杯', 100, 95)])

    def backend_merge_and_confirm(self, sid, member_offer, target_offer):
        """后台把 member_offer 并入 target_offer 所在组并确认（不保存）；返回目标组号。"""
        snapshot = self.service.get(sid)
        pair = self.group_with(snapshot, target_offer)
        other = self.group_with(snapshot, member_offer)
        self.assertNotEqual(pair['id'], other['id'])
        self.service.edit_group(sid, 'move', other['id'], other['members'][0], pair['id'])
        self.service.confirm(sid, pair['id'])
        return pair['id']

    # ------------------------------------------------- 测试边界的监听与拦截

    def capture_target_gets(self):
        """登记同源快照 GET 的响应记录（AC04/AC08 回放用的「先前真实捕获」素材）。"""
        seen = []

        def remember(response):
            url = urlsplit(response.url)
            if response.request.method == 'GET' and url.path == '/api/analysis':
                seen.append(response)

        self.page.on('response', remember)
        self.addCleanup(self.page.remove_listener, 'response', remember)
        return seen

    def capture_sid_body(self, seen, sid):
        """取本分析初始加载那次真实 GET 的正文（此刻页面与它已按之呈现）。"""
        response = next(r for r in seen if parse_qs(urlsplit(r.url).query).get('id') == [sid])
        return response.json()

    def route_analysis(self, handler):
        """在 page 上拦截 /api/analysis*；测试结束显式撤掉，不让作用域漏到别的用例。"""
        self.page.route('**/api/analysis*', handler)
        self.addCleanup(self.page.unroute, '**/api/analysis*')

    def hold_target_get(self, sid):
        """扣住下一次同分析快照 GET：拦到只存不放行，等测试按事件显式放行。

        返回 (held, held_event)：held 拿到挂起的 route，held_event 在拦截发生时置位。
        """
        held, held_event = [], threading.Event()

        def hold(route):
            request = route.request
            url = urlsplit(request.url)
            if (request.method == 'GET' and url.path == '/api/analysis'
                    and parse_qs(url.query).get('id') == [sid] and not held):
                held.append(route)      # 只记不放行：这一次读取挂起，直到测试 continue_
                held_event.set()
                return
            route.continue_()

        self.route_analysis(hold)
        return held, held_event

    # ------------------------------------------------------------ AC03 读取未完成

    def test_held_snapshot_get_is_not_a_success_until_released(self):
        """刻意扣住刷新读取的同分析 GET：未放行前不得宣告同步成功、也没有下一步业务界面；
        按事件放行（continue_ 交回真实服务器）后响应内容与页面呈现正确。不用固定延时。"""
        self.seed_pair()
        sid = self.dates()
        self.assertEqual(wait_initial_presentation(self.page, products=2, groups=2, pending=2), sid)
        pair_id = self.backend_merge_and_confirm(sid, 'A021', 'A011')
        saved = self.service.save_draft(sid)

        held, held_event = self.hold_target_get(sid)
        with self.assertRaises(SyncFailure) as caught:
            sync_after_save(self.page, sid, saved, timeout_ms=2000)
        exc = caught.exception
        self.assertEqual(exc.phase, 'R(读取)')
        message = str(exc)
        self.assertIn('没有捕获到同分析快照 GET', message)
        self.assertIn(sid, message)
        self.assertIn('当前可见屏=日期', message)   # 页面临时停在刷新中途，未应用也未落屏
        # GET 未完成：没有宣告同步成功，也没有可继续的下一步业务界面
        self.assertTrue(held_event.wait(5), '刷新读取的同分析 GET 从未被拦截到')
        self.assertEqual(len(held), 1)
        expect(self.page.get_by_role('heading', name='畅销品', exact=True)).not_to_be_visible()
        expect(self.page.locator('#datePage')).to_be_visible()
        expect(self.page.locator('#snapshotInfo')).not_to_contain_text('个商品')

        # 按事件放行：真实服务器应答这次挂起的 GET，页面把保存后的快照落屏（内容与呈现正确）
        with self.page.expect_response(
                lambda response: response.request.method == 'GET'
                and f'id={sid}' in response.url) as released:
            held[0].continue_()
        self.assertEqual(released.value.status, 200)
        body = released.value.json()
        self.assertEqual(body['id'], sid)
        self.assertFalse(body['dirty'])
        self.assertTrue(body['saved_at'])
        expect(self.page.get_by_role('heading', name='畅销品', exact=True)).to_be_visible()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('2 个商品 · 1 组同款（其中 0 组待确认）')
        expect(self.page.locator(f'.rank[data-group="{pair_id}"] .number')).to_contain_text('10')

    # ------------------------------------------------------------ AC04 旧快照回放

    def test_replaying_the_earlier_same_id_snapshot_fails_at_read_stage(self):
        """刷新读回同一分析号的旧快照：即便 200、分析号正确，成员/确认/保存内容不符也必须
        归类「刷新读取不成立」。仅本反例回放先前从真实响应捕获的旧正文。"""
        self.seed_pair()
        seen = self.capture_target_gets()
        sid = self.dates()
        self.assertEqual(wait_initial_presentation(self.page, products=2, groups=2, pending=2), sid)
        old_body = self.capture_sid_body(seen, sid)     # 初始加载时真实捕获的旧快照
        self.backend_merge_and_confirm(sid, 'A021', 'A011')
        saved = self.service.save_draft(sid)
        self.assertFalse(saved['dirty'])
        self.assertTrue(saved['saved_at'])

        stale = json.dumps(old_body, ensure_ascii=False)
        self.route_analysis(lambda route: route.fulfill(
            status=200, body=stale, content_type='application/json; charset=utf-8'))

        with self.assertRaises(SyncFailure) as caught:
            sync_after_save(self.page, sid, saved)
        exc = caught.exception
        self.assertEqual(exc.phase, 'R(读取)')
        message = str(exc)
        self.assertIn('刷新读到的内容与保存返回不一致', message)
        self.assertNotIn('分析身份不符', message)        # 200 且分析号正确：不是读错分析
        self.assertNotIn('刷新读取的状态码不是 200', message)
        self.assertIn('保存标记不符', message)           # saved_at 仍是旧快照的空值
        self.assertIn('分组集合不符', message)           # 2 个旧分组对 1 个已保存分组
        self.assertIn(f'读到：分析 {sid}', message)      # 同分析号、旧内容，诊断可定位
        # 旧内容呈现出来了，但没有结果屏：同步没有被放行
        expect(self.page.locator('#snapshotInfo')).to_contain_text('2 组同款（其中 2 组待确认）')
        expect(self.page.get_by_role('heading', name='畅销品', exact=True)).not_to_be_visible()

    # ------------------------------------------------------------ AC05 别的分析

    def test_response_for_another_analysis_is_not_readiness_evidence(self):
        """刷新核对时回放另一个真实分析（不同分析号、不同区间）的响应：不得被当成本次响应。
        「不能仅凭页面标题放行」：这份响应自己的页面呈现恰会进结果屏，同步仍必须拒绝。"""
        self.seed_pair()
        sid = self.dates()
        self.assertEqual(wait_initial_presentation(self.page, products=2, groups=2, pending=2), sid)
        self.backend_merge_and_confirm(sid, 'A021', 'A011')
        saved = self.service.save_draft(sid)

        # 另一个真实分析：区间到 09-21；保存且全部确认（它自己呈现会直接进结果屏）。
        other = self.service.start('2026-09-07', '2026-09-21')
        other_id = other['id']
        for group in other['groups']:
            self.service.confirm(other_id, group['id'])
        self.service.save_draft(other_id)
        # 从真实本地 API 抓它的正文（不是手编 JSON，也不改动本页状态）。
        with urllib.request.urlopen(
                f'http://127.0.0.1:{self.server.server_port}/api/analysis?id={other_id}') as reply:
            other_body = reply.read().decode('utf-8')

        self.route_analysis(lambda route: route.fulfill(
            status=200, body=other_body, content_type='application/json; charset=utf-8'))

        with self.assertRaises(SyncFailure) as caught:
            sync_after_save(self.page, sid, saved)
        exc = caught.exception
        self.assertEqual(exc.phase, 'R(读取)')
        message = str(exc)
        self.assertIn(f'分析身份不符（读到 {other_id}，期望 {sid}）', message)
        self.assertIn('区间不符', message)
        self.assertIn('2026-09-21', message)
        # 不能仅凭页面标题放行：被回放的分析自己呈现成了结果屏，同步仍拒绝
        expect(self.page.get_by_role('heading', name='畅销品', exact=True)).to_be_visible()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('2026-09-21')

    # ------------------------------------------- AC06 未保存 / 无保存完成事实

    def test_never_saved_state_cannot_enter_post_save_sync(self):
        """两种「没有成功保存事实」的状态都必须在 S 层被拒、一步都不往下走：
        初始快照 dirty=false 但从未保存（saved_at 空）不被顶替成保存事实；
        后台有未保存编辑（dirty=true）也不得进入「保存后同步成功」。"""
        self.seed_pair()
        sid = self.dates()
        self.assertEqual(wait_initial_presentation(self.page, products=2, groups=2, pending=2), sid)
        seen = self.capture_target_gets()

        # 子例 1：刚冻结、从未保存——初始 dirty=false 不能代替保存完成信号
        fresh = self.service.get(sid)
        self.assertFalse(fresh['dirty'])
        self.assertIsNone(fresh['saved_at'])
        with self.assertRaises(SyncFailure) as caught:
            sync_after_save(self.page, sid, fresh)
        first = caught.exception
        self.assertEqual(first.phase, 'S(保存)')
        self.assertIn('缺少保存时间', str(first))
        self.assertNotIn('未保存状态', str(first))      # 拒绝理由是「缺保存事实」，不是 dirty
        self.assertIn('dirty=False', str(first))        # 诊断里看得到它确实不脏

        # 子例 2：后台并组、确认但未保存——脏状态不得进入「保存后同步成功」
        self.backend_merge_and_confirm(sid, 'A021', 'A011')
        dirty = self.service.get(sid)
        self.assertTrue(dirty['dirty'])
        self.assertIsNone(dirty['saved_at'])
        with self.assertRaises(SyncFailure) as caught:
            sync_after_save(self.page, sid, dirty)
        second = caught.exception
        self.assertEqual(second.phase, 'S(保存)')
        self.assertIn('仍是未保存状态（dirty=True）', str(second))
        self.assertIn('缺少保存时间', str(second))

        # 两次都短路在 S：没有发生任何刷新读取，页面也停在初始未确认呈现
        self.assertEqual(seen, [])
        expect(self.page.get_by_role('heading', name='畅销品', exact=True)).not_to_be_visible()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('2 个商品 · 2 组同款（其中 2 组待确认）')

    # ------------------------------------------- AC07 保存成功但仍有未确认组

    def test_saved_with_pending_groups_fails_result_screen_gate(self):
        """保存真实成功、刷新读取也真实成立（S/R 都过），但还有未确认组：不得按其通过
        「全部确认且进结果页」的验收；失败落在 U(落屏)，页面保留确认屏与已确认组。"""
        self.use_model(names=['同款杯1', '异名杯', '同款杯3', '异名壶'])
        self.drop_fixture_product()
        self.seed_products([('A01', 'A011', '同款杯1', 100, 95),
                            ('A01', 'A012', '同款杯3', 100, 90),
                            ('A02', 'A021', '异名杯', 100, 95),
                            ('A02', 'A022', '异名壶', 100, 80)])
        sid = self.dates()
        self.assertEqual(wait_initial_presentation(self.page, products=4, groups=4, pending=4), sid)
        # 只把一对并组确认，另一对留待确认；有待确认组也允许保存（与「暂时保存」同一契约）。
        pair_id = self.backend_merge_and_confirm(sid, 'A021', 'A011')
        saved = self.service.save_draft(sid)
        self.assertFalse(saved['dirty'])
        self.assertTrue(saved['saved_at'])
        self.assertEqual(len(saved['groups']), 3)
        self.assertEqual(len([g for g in saved['groups'] if not g['confirmed']]), 2)

        with self.assertRaises(SyncFailure) as caught:
            sync_after_save(self.page, sid, saved)
        exc = caught.exception
        self.assertEqual(exc.phase, 'U(落屏)')      # 不是 S/R，也不是 U(呈现)：卡在结果页门禁
        message = str(exc)
        self.assertIn('门禁未通过或落屏未发生', message)
        self.assertIn('当前可见屏=确认', message)
        self.assertIn('结果页门禁=False', message)
        self.assertIn('confirmed=True', message)    # 已确认与待确认在诊断里可分辨
        self.assertIn('confirmed=False', message)

        # 保留确认页：已确认页签下这一组（两个成员、销量 10）仍在
        expect(self.page.get_by_role('heading', name='畅销品', exact=True)).not_to_be_visible()
        self.switch_tab('已确认')
        expect(self.page.locator('.group-choice')).to_have_count(1)
        self.page.locator('.group-choice').first.click()
        detail = self.page.locator('#groupDetail')
        expect(detail).to_contain_text('2 个商品')
        expect(detail).to_contain_text('同款杯1')
        expect(detail).to_contain_text('异名杯')
        expect(detail).to_contain_text('同款组销量 10')
        expect(detail).to_contain_text('已确认')

    # ------------------------------------------- AC08 网络正确但页面未应用

    def test_network_ok_but_page_not_applying_fails_at_presentation_stage(self):
        """保存与目标 GET 内容全部正确（R 成立），但页面没有应用新快照：不能以网络成功
        放行；失败须落在 U(呈现) 而不是 U(落屏)——故障只在测试边界注入，不预设产品缺陷。"""
        self.seed_pair()
        seen = self.capture_target_gets()
        sid = self.dates()
        self.assertEqual(wait_initial_presentation(self.page, products=2, groups=2, pending=2), sid)
        old_body = self.capture_sid_body(seen, sid)
        self.backend_merge_and_confirm(sid, 'A021', 'A011')
        saved = self.service.save_draft(sid)

        # 测试边界故障注入：网络层照常拿到正确的新快照（同步辅助的 R 监听看到真响应），
        # 但页面代码这一次从 fetch 拿到旧正文——模拟「页面未应用新快照」。
        stale = json.dumps(old_body, ensure_ascii=False)
        self.page.add_init_script(
            "const realFetch = window.fetch.bind(window);\n"
            f"const staleBody = {json.dumps(stale)};\n"
            "window.fetch = async (input, init) => {\n"
            "  const response = await realFetch(input, init);\n"
            "  const url = typeof input === 'string' ? input : (input && input.url) || '';\n"
            f"  if (url.includes({json.dumps('/api/analysis?id=' + sid)})) {{\n"
            "    response.text().catch(() => {});   // 把真实正文读掉：网络层完整送达，同步辅助的 R 监听才取得到\n"
            "    return new Response(staleBody, {status: 200, headers: {'Content-Type': 'application/json; charset=utf-8'}});\n"
            "  }\n"
            "  return response;\n"
            "};\n")

        with self.assertRaises(SyncFailure) as caught:
            sync_after_save(self.page, sid, saved)
        exc = caught.exception
        self.assertEqual(exc.phase, 'U(呈现)')
        message = str(exc)
        self.assertIn('新文档没有呈现刷新读到的快照', message)
        self.assertNotIn('门禁未通过或落屏未发生', message)   # 是呈现不成立，不是落屏不成立
        self.assertIn('列表计数=2 个商品 · 2 组同款（其中 2 组待确认）', message)  # 页面对照
        self.assertIn('confirmed=[False, False]', message)    # 页面所持仍是旧快照
        # 页面停在旧内容，没有结果屏
        expect(self.page.locator('#snapshotInfo')).to_contain_text('2 组同款（其中 2 组待确认）')
        expect(self.page.get_by_role('heading', name='畅销品', exact=True)).not_to_be_visible()


if __name__ == '__main__':
    unittest.main()
