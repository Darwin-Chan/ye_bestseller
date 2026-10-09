"""SKU 草稿：真实临时库存 → 分析服务公开发起/读取/恢复边界。"""
import tempfile
import unittest
from pathlib import Path
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect, SKU_IMAGE_OWN
from helpers import new_round, product_picture

class SkuDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'inventory.db'
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.service = AnalysisService(AnalysisConfig(self.path), running=lambda: False)

    def observe(self, day, rows, *, title='杯子', offer='11', shop='A01', image=None):
        self.db.submit_inventory_snapshot(
            round_id=new_round(self.db, shop, run_date=day), shop_key=shop, shop_url='https://shop.test',
            shop_name='店铺', offer_id=offer, product_url='https://offer.test/'+offer,
            list_title=title, detail_title=title, main_image_url='', sku_rows=[dict(observation_complete=True, **row) if 'observation_complete' not in row else row for row in rows],
            collected_at=day+'T01:00:00+00:00', attempt=1, image_evidence=image)

    def test_partial_visible_fallback_is_unknown_and_keeps_raw_matching_name_after_restore(self):
        from bestseller_monitor.parse import extract_skus_from_html
        rows=extract_skus_from_html('<div>Red ￥5 库存80</div><div>Blue ￥6 库存待查询</div>')
        self.assertEqual(['Red'],[row['sku_name'] for row in rows])
        self.observe('2026-09-14',rows)
        draft=self.service.start('2026-09-07','2026-09-14')
        member=draft['members'][0]
        self.assertFalse(member['observation_complete'])
        self.assertIsNone(member['observation_sku_count'])
        self.assertIsNone(member['single_sku'])
        self.assertEqual('Red',member['matching_input']['sku_name'])
        self.assertIn('完整性无法证明',member['evidence_note'])
        self.service.save_draft(draft['id'])
        restarted=AnalysisService(self.service.config,running=lambda:False)
        self.assertEqual(member,restarted.get(draft['id'])['members'][0])

    def test_skipped_json_entry_does_not_prove_complete_sku_collection(self):
        from bestseller_monitor.parse import extract_skus_from_html
        rows=extract_skus_from_html('<script>var x={"skuInfoMap":{"Red":{"skuId":"r","canBookCount":80,"specAttrs":"Red"},"Blue":null}};</script>')
        self.observe('2026-09-14',rows)
        member=self.service.start('2026-09-07','2026-09-14')['members'][0]
        self.assertFalse(member['observation_complete'])
        self.assertIsNone(member['single_sku'])
        self.assertEqual('Red',member['matching_input']['sku_name'])

    def test_success_without_collection_provenance_does_not_prove_single_sku(self):
        self.db.submit_inventory_snapshot(round_id=new_round(self.db,'A01',run_date='2026-09-14'),
            shop_key='A01',shop_url='https://shop.test',shop_name='店铺',offer_id='11',
            product_url='https://offer.test/11',list_title='杯子',detail_title='杯子',
            main_image_url='',sku_rows=[dict(sku_id='r',sku_name='Red',sku_stock=80)],
            collected_at='2026-09-14T01:00:00+00:00',attempt=1)
        member=self.service.start('2026-09-07','2026-09-14')['members'][0]
        self.assertFalse(member['observation_complete'])
        self.assertIsNone(member['single_sku'])
        self.assertEqual('Red',member['matching_input']['sku_name'])

    def test_members_are_skus_and_draft_restores_pending_identity_and_sales(self):
        self.observe('2026-09-07', [dict(sku_id='red', sku_name='红色 #A#', sku_stock=100, sku_price=5),
                                    dict(sku_id='blue', sku_name='蓝色 #A#', sku_stock=50, sku_price=8)])
        self.observe('2026-09-14', [dict(sku_id='red', sku_name='红色 #A#', sku_stock=80, sku_price=5),
                                    dict(sku_id='blue', sku_name='蓝色 #A#', sku_stock=45, sku_price=8)])
        draft = self.service.start('2026-09-07', '2026-09-14')
        self.assertEqual('sku', draft['member_kind'])
        self.assertEqual([('A01','11','blue'), ('A01','11','red')],
                         [(m['shop_key'],m['offer_id'],m['sku_id']) for m in draft['members']])
        self.assertEqual([5,20], [m['sales'] for m in draft['members']])
        self.assertEqual(1, len(draft['groups']))  # Ticket 04: same-code default, not human confirmation.
        self.assertEqual(25, draft['groups'][0]['sales'])
        self.assertTrue(all(not g['confirmed'] for g in draft['groups']))
        self.service.save_draft(draft['id'])
        restarted = AnalysisService(self.service.config, running=lambda: False)
        restored = restarted.get(draft['id'])
        self.assertEqual(draft['members'], restored['members'])
        self.assertEqual(draft['groups'], restored['groups'])

    def test_evidence_uses_complete_observation_and_original_names_not_filtered_members(self):
        self.observe('2026-09-07', [dict(sku_id='red', sku_name='旧名字', sku_stock=100, sku_price=5),
                                    dict(sku_id='blue', sku_name='蓝色', sku_stock=50, sku_price=8)])
        self.observe('2026-09-14', [dict(sku_id='red', sku_name='结束日 #A#', sku_stock=80, sku_price=6),
                                    dict(sku_id='blue', sku_name='蓝色', sku_stock=50, sku_price=8)], title='历史商品')
        self.observe('2026-09-21', [dict(sku_id='red', sku_name='未来 #B#', sku_stock=10, sku_price=99)], title='未来商品')
        draft=self.service.start('2026-09-07','2026-09-14')
        red=next(m for m in draft['members'] if m['sku_id']=='red')
        self.assertEqual('历史商品', red['product_name'])
        self.assertEqual('结束日 #A#', red['raw_sku_name'])
        self.assertEqual('结束日 #A#', red['matching_input']['sku_name'])
        self.assertFalse(red['single_sku'])
        self.assertTrue(red['observation_complete'])
        self.assertEqual(2, red['observation_sku_count'])
        self.assertEqual('2026-09-14T01:00:00+00:00',red['observed_at'])
        self.assertEqual((80,6,20), (red['stock'],red['price'],red['sales']))
        self.assertEqual(2,len(draft['members']))  # 零销量 SKU 不从完整集合中抹掉

    def test_default_and_single_real_sku_keep_raw_names_and_empty_match_sku_name(self):
        for offer,sku_id,name in [('11','default','默认(单规格)'), ('12','real','默认(单规格)')]:
            self.observe('2026-09-07',[dict(sku_id=sku_id,sku_name=name,sku_stock=100,sku_price=3)],offer=offer)
            self.observe('2026-09-14',[dict(sku_id=sku_id,sku_name=name,sku_stock=80,sku_price=4)],offer=offer)
        members=self.service.start('2026-09-07','2026-09-14')['members']
        self.assertEqual(['system_default','observed'],[m['name_source'] for m in members])
        self.assertTrue(all(m['single_sku'] for m in members))
        self.assertEqual(['',''],[m['matching_input']['sku_name'] for m in members])
        self.assertEqual(['杯子','杯子'],[m['matching_input']['product_name'] for m in members])
        self.assertEqual(['默认(单规格)','默认(单规格)'],[m['raw_sku_name'] for m in members])

    def test_images_are_paired_to_latest_sku_observation_and_frozen_in_saved_draft(self):
        own=product_picture('red'); main=product_picture('blue'); future=product_picture('green')
        self.observe('2026-09-07',[dict(sku_id='s',sku_name='旧名',sku_stock=100)],image=main)
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='结束日名',sku_stock=80,
            sku_image_evidence=dict(own,source=SKU_IMAGE_OWN,url='https://image.test/sku'))],image=main)
        self.observe('2026-09-21',[dict(sku_id='s',sku_name='未来名',sku_stock=5,
            sku_image_evidence=dict(future,source=SKU_IMAGE_OWN))],image=future)
        draft=self.service.start('2026-09-07','2026-09-14'); member=draft['members'][0]
        self.assertEqual(('sku',own['hash'],'https://image.test/sku'),
                         (member['image_source'],member['image_hash'],member['image_url']))
        self.assertTrue(member['image_data'].startswith('data:image/png;base64,'))
        self.service.save_draft(draft['id'])
        self.conn.close(); self.path.unlink()  # 真实源库不可用仍能恢复被冻结的证据
        restored=AnalysisService(self.service.config,running=lambda:False).get(draft['id'])
        self.assertEqual(member,restored['members'][0])

    def test_same_observation_main_image_fallback_and_missing_images_do_not_borrow_old_day(self):
        image=product_picture('blue')
        self.observe('2026-09-07',[dict(sku_id='s',sku_name='s',sku_stock=100)],image=image)
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='s',sku_stock=80)],image=image)
        member=self.service.start('2026-09-07','2026-09-14')['members'][0]
        self.assertEqual(('product_main_same_observation',image['hash']),
                         (member['image_source'],member['image_hash']))
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='无图当次',sku_stock=75)])
        member=self.service.start('2026-09-07','2026-09-14')['members'][0]
        self.assertEqual(('missing',None,None),
                         (member['image_source'],member['image_hash'],member['image_data']))
        self.assertEqual('无图当次',member['raw_sku_name'])

    def test_legacy_observation_cannot_claim_single_sku_from_accumulated_rows(self):
        self.observe('2026-09-07',[dict(sku_id='s',sku_name='真实名称',sku_stock=100)])
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='真实名称',sku_stock=80)])
        self.conn.execute('DROP TABLE sku_observation_batches'); self.conn.commit()
        member=self.service.start('2026-09-07','2026-09-14')['members'][0]
        self.assertIsNone(member['single_sku'])
        self.assertFalse(member['observation_complete'])
        self.assertIn('完整性无法证明',member['evidence_note'])
        self.assertEqual('真实名称',member['matching_input']['sku_name'])

    def test_newer_legacy_inventory_without_snapshot_exposes_time_and_completeness_gap(self):
        self.observe('2026-09-07',[dict(sku_id='s',sku_name='旧名',sku_stock=100)],image=product_picture('red'))
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='最新库存名',sku_stock=80,sku_price=9)])
        self.conn.execute('DROP TABLE sku_observation_batches')
        self.conn.execute("DELETE FROM snapshots WHERE collected_at LIKE '2026-09-14%'")
        self.conn.commit()
        member=self.service.start('2026-09-07','2026-09-14')['members'][0]
        self.assertEqual(('最新库存名',80,9),(member['raw_sku_name'],member['stock'],member['price']))
        self.assertEqual('2026-09-14',member['observed_date'])
        self.assertIsNone(member['observed_at'])
        self.assertFalse(member['observation_complete'])
        self.assertIsNone(member['single_sku'])
        self.assertEqual(('missing',None),(member['image_source'],member['image_data']))
        self.assertIn('精确时间',member['evidence_note'])

    def test_legacy_title_and_image_come_from_same_detail_observation(self):
        self.db.submit_inventory_snapshot(round_id=new_round(self.db,'A01',run_date='2026-09-14'),
            shop_key='A01',shop_url='https://shop.test',shop_name='店铺',offer_id='11',
            product_url='https://offer.test/11',list_title='列表旧标题',detail_title='详情真实标题',
            main_image_url='https://image.test/main',sku_rows=[dict(sku_id='s',sku_name='规格',sku_stock=80)],
            collected_at='2026-09-14T01:00:00+00:00',attempt=1,image_evidence=product_picture('blue'))
        self.conn.execute('DROP TABLE sku_observation_batches');self.conn.commit()
        member=self.service.start('2026-09-07','2026-09-14')['members'][0]
        self.assertEqual('详情真实标题',member['product_name'])
        self.assertEqual('详情真实标题',member['matching_input']['product_name'])
        self.assertEqual('product_main_same_observation',member['image_source'])

    def test_missing_platform_identity_is_exposed_instead_of_claimed_as_platform_id(self):
        from bestseller_monitor.parse import extract_skus_from_html
        rows=extract_skus_from_html('<script>var x={"skuInfoMap":{"红色":{"canBookCount":20,"specAttrs":"红色"}}};</script>')
        self.observe('2026-09-14',rows)
        member=self.service.start('2026-09-07','2026-09-14')['members'][0]
        self.assertEqual('derived',member['sku_id_source'])
        self.assertIn('平台 SKU 编号缺失',member['evidence_note'])
        self.assertEqual('红色',member['raw_sku_name'])

    def test_same_platform_id_in_different_shops_remains_distinct_and_latest_set_is_not_accumulated(self):
        self.observe('2026-09-07',[dict(sku_id='s',sku_name='s',sku_stock=100),dict(sku_id='removed',sku_name='已下架',sku_stock=0)])
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='s',sku_stock=80)])
        self.observe('2026-09-13',[dict(sku_id='s',sku_name='同编号其他店',sku_stock=30)],shop='B02')
        draft=self.service.start('2026-09-07','2026-09-14')
        members=[m for m in draft['members'] if m['sku_id']=='s']
        self.assertEqual(['A01','B02'],[m['shop_key'] for m in members])
        self.assertEqual([1,1],[m['observation_sku_count'] for m in members])
        self.assertEqual(['',''],[m['matching_input']['sku_name'] for m in members])
        self.assertEqual(3,len(draft['groups']))

    def test_failed_save_keeps_unsaved_draft_and_discard_preserves_last_frozen_version(self):
        import sqlite3
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='s',sku_stock=80)])
        draft=self.service.start('2026-09-07','2026-09-14')
        target=self.service.store.path
        target.mkdir()  # 真实 I/O 失败，不 mock 分析服务或保存方法。
        with self.assertRaises(sqlite3.OperationalError):
            self.service.save_draft(draft['id'])
        self.assertTrue(self.service.get(draft['id'])['dirty'])
        target.rmdir()
        saved=self.service.save_draft(draft['id'])
        self.assertFalse(saved['dirty'])
        self.assertEqual({'reverted':True},self.service.discard(draft['id']))
        self.assertEqual(saved,self.service.get(draft['id']))
        fresh=self.service.start('2026-09-07','2026-09-14')
        self.assertEqual({'reverted':False},self.service.discard(fresh['id']))
        with self.assertRaises(ValueError):
            self.service.get(fresh['id'])

    def test_future_new_sku_does_not_break_historical_analysis_or_enter_public_inventory(self):
        self.observe('2026-09-07',[dict(sku_id='old',sku_name='旧规格',sku_stock=100)])
        self.observe('2026-09-14',[dict(sku_id='old',sku_name='旧规格',sku_stock=80)])
        self.observe('2026-09-21',[dict(sku_id='old',sku_name='未来改名',sku_stock=60),dict(sku_id='new',sku_name='未来新增',sku_stock=30)])
        draft=self.service.start('2026-09-07','2026-09-14')
        self.assertEqual(['old'],[m['sku_id'] for m in draft['members']])
        self.assertEqual(20,draft['members'][0]['sales'])
        self.assertTrue(all(r['date']<='2026-09-14' for r in draft['inventory']))

    def test_same_second_continue_opens_last_successful_save_not_uuid_order(self):
        from unittest.mock import patch
        from uuid import UUID
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='s',sku_stock=80)])
        with patch('bestseller_monitor.analysis.utcnow',return_value='2026-10-08T01:00:00+00:00'), \
             patch('bestseller_monitor.analysis.uuid4',side_effect=[UUID('f'*32),UUID('0'*32)]):
            first=self.service.start('2026-09-07','2026-09-14')
            self.service.save_draft(first['id'])
            second=self.service.start('2026-09-07','2026-09-14')
            self.service.save_draft(second['id'])
            restarted=AnalysisService(self.service.config,running=lambda:False)
            self.assertEqual(second['id'],restarted.draft()['id'])
            self.assertEqual(second['members'],restarted.get(restarted.draft()['id'])['members'])
            self.service.save_draft(first['id'])
            self.assertEqual(first['id'],restarted.draft()['id'])

    def test_sku_mutations_and_retry_are_open_but_unconfirmed_results_remain_gated(self):
        self.observe('2026-09-07',[dict(sku_id='s',sku_name='真实名称',sku_stock=100)])
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='真实名称',sku_stock=80)])
        ledger=dict(relations=[],standalone=[['A01|11','old-version','machine']],excluded=[['A01|11','A01|12','machine']])
        self.service.store.write('old','2026-09-07','2026-09-14','2026-09-14T02:00:00+00:00',{},ledger)
        draft=self.service.start('2026-09-07','2026-09-14'); group=draft['groups'][0]['id']
        self.assertEqual('missing_image',draft['members'][0]['matching_state'])
        self.assertIn('缺少图片证据',draft['members'][0]['matching_note'])
        confirmed=self.service.confirm(draft['id'],group)
        self.assertTrue(confirmed['groups'][0]['confirmed'])
        withdrawn=self.service.withdraw(draft['id'],group)
        self.assertFalse(withdrawn['groups'][0]['confirmed'])
        removed=self.service.edit_group(draft['id'],'remove',group,draft['members'][0])
        self.assertEqual(1,len(removed['groups']))
        retried = self.service.retry_matching(draft['id'])
        self.assertEqual(0, retried["matching"]["retryable"])
        self.assertEqual(1, len(retried["groups"]))
        accepted = self.service.retry_matching_job(draft['id'])
        terminal = self.service.wait_terminal(accepted["id"], 5)
        self.assertEqual("ready", terminal["state"])
        operations=[lambda:self.service.save_and_view(draft['id']),
            lambda:self.service.export(draft['id'],'<!doctype html>unsafe')]
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    operation()
        self.service.save_draft(draft['id'])
        self.assertEqual(ledger,self.service.store.ledger())
    def test_stock_shape_segments_and_name_changes_do_not_duplicate_sku_sales(self):
        self.observe('2026-09-08',[dict(sku_id='default',sku_name='默认(单规格)',sku_stock=90)])
        self.observe('2026-09-09',[dict(sku_id='s',sku_name='旧名 #A#',sku_stock=50)])
        self.observe('2026-09-10',[dict(sku_id='s',sku_name='改名 #B#',sku_stock=45)])
        self.observe('2026-09-11',[dict(sku_id='default',sku_name='默认(单规格)',sku_stock=80)])
        self.observe('2026-09-12',[dict(sku_id='default',sku_name='默认(单规格)',sku_stock=70)])
        self.observe('2026-09-13',[dict(sku_id='s',sku_name='又改名 #C#',sku_stock=60)])
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='最终名 #D#',sku_stock=50)])
        draft=self.service.start('2026-09-07','2026-09-14')
        self.assertEqual(['default','s'],[m['sku_id'] for m in draft['members']])
        # Each representation segment is calculated independently.  The default
        # segment resumes at 80 -> 70 after the temporary multi-SKU segment, so
        # this fixture has one 10-unit sale rather than bridging or duplicating
        # the intervening representation change.
        self.assertEqual([10,15],[m['sales'] for m in draft['members']])
        self.assertEqual([1,2],[sum(p['sales']>0 for p in m['points']) for m in draft['members']])
        self.assertEqual('最终名 #D#',draft['members'][1]['raw_sku_name'])


class SkuDraftBrowserTests(unittest.TestCase):
    observe=SkuDraftTests.observe

    def setUp(self):
        from playwright.sync_api import sync_playwright
        from test_analysis import serve
        SkuDraftTests.setUp(self)
        self.observe('2026-09-07',[dict(sku_id='s',sku_name='原始 SKU #A#',sku_stock=100)])
        self.observe('2026-09-14',[dict(sku_id='s',sku_name='原始 SKU #A#',sku_stock=80)])
        self.server,self.thread=serve(self.service)
        def stop():
            self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.addCleanup(stop)
        pw=sync_playwright().start(); self.addCleanup(pw.stop)
        self.browser=pw.chromium.launch(channel='msedge',headless=True); self.addCleanup(self.browser.close)
        self.page=self.browser.new_page()
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}')

    def begin(self):
        self.page.locator('#start').fill('2026-09-07'); self.page.locator('#end').fill('2026-09-14')
        self.page.locator('#begin').click()

    def test_confirmation_search_matches_all_five_sku_fields(self):
        from playwright.sync_api import expect

        for day, stock in [('2026-09-07', 100), ('2026-09-14', 80)]:
            self.observe(
                day,
                [dict(sku_id='blue', sku_name='蓝色规格', sku_stock=stock)],
                title='蓝色商品',
                offer='22',
            )

        self.begin()
        expect(self.page.locator('#products [data-sku-group]')).to_have_count(2)
        target_group = self.page.locator('#products [data-sku-group]').nth(1).get_attribute('data-sku-group')
        for query in ['蓝色商品', '蓝色规格', '22', 'blue', target_group]:
            self.page.locator('#skuQuery').fill(query)
            expect(self.page.locator('#products [data-sku-group]')).to_have_count(1)
            expect(self.page.locator('#groupDetail')).to_contain_text('蓝色商品')
            expect(self.page.locator('#groupDetail')).to_contain_text('蓝色规格')
    def test_real_entry_displays_sku_evidence_and_save_reload_keeps_review(self):
        from playwright.sync_api import expect
        self.begin()
        expect(self.page.locator('#reviewPage')).to_be_visible()
        expect(self.page.locator('.sku-evidence-card')).to_have_count(1)
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('平台 SKU：s')
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('原始 SKU #A#')
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('区间销量：20')
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('匹配 SKU 名称：空')
        expect(self.page.locator('#runMatching')).to_be_disabled()
        expect(self.page.locator('#saveAndView')).to_be_disabled()
        self.page.locator('#saveDraft').click()
        expect(self.page.locator('#saveStatus')).to_contain_text('已保存')
        self.page.reload()
        expect(self.page.locator('.sku-evidence-card')).to_have_count(1)
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('原始 SKU #A#')
        expect(self.page.locator('#results')).to_be_hidden()

    def test_sku_paging_selection_and_http_restart_restore_frozen_evidence(self):
        from playwright.sync_api import expect
        from test_analysis import serve
        self.observe('2026-09-14',[dict(sku_id=f'p{i:02}',sku_name=f'规格{i:02}',sku_stock=100-i) for i in range(22)],offer='22',image=product_picture('blue'))
        self.begin()
        expect(self.page.locator('#products [data-sku-group]')).to_have_count(20)
        self.page.locator('#next').click()
        expect(self.page.locator('#products [data-sku-group]')).to_have_count(3)
        self.page.locator('#products [data-sku-group]').last.click()
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('规格21')
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('同次商品主图代填')
        self.page.locator('#saveDraft').click()
        expect(self.page.locator('#saveStatus')).to_contain_text('已保存')
        saved_id=self.page.url.split('analysis=')[1]
        restarted=AnalysisService(self.service.config,running=lambda:False)
        server,thread=serve(restarted)
        def stop():
            server.shutdown();server.server_close();thread.join()
        self.addCleanup(stop)
        self.page.goto(f'http://127.0.0.1:{server.server_port}/#analysis='+saved_id)
        expect(self.page.locator('#reviewPage')).to_be_visible()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('23 个 SKU')
        self.page.locator('#next').click()
        self.page.locator('#products [data-sku-group]').last.click()
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('规格21')
        expect(self.page.locator('.sku-evidence-card img')).to_be_visible()

    def test_saved_legacy_confirmed_groups_are_readable_but_mutations_are_disabled(self):
        from playwright.sync_api import expect
        legacy=AnalysisService(AnalysisConfig(self.path,member_kind='product'),running=lambda:False)
        old=legacy.start('2026-09-07','2026-09-14')
        legacy.confirm(old['id'],old['groups'][0]['id']);legacy.save_draft(old['id'])
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/#analysis='+old['id'])
        self.page.reload()
        expect(self.page.locator('#groupDetail')).to_contain_text('旧商品级草稿仅供查阅')
        expect(self.page.locator('#groupDetail')).to_contain_text('杯子')
        expect(self.page.locator('#saveDraft')).to_be_disabled()
        expect(self.page.locator('#saveAndView')).to_be_disabled()
        expect(self.page.locator('#runMatching')).to_be_disabled()
        expect(self.page.get_by_role('button',name='组内新增商品',exact=True)).to_have_count(0)

    def test_legacy_data_gap_and_current_missing_image_are_visible(self):
        from playwright.sync_api import expect
        self.conn.execute('DROP TABLE sku_observation_batches'); self.conn.commit()
        self.begin()
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('历史观测完整性无法证明')
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('当次缺图')
        expect(self.page.locator('.sku-evidence-card')).to_contain_text('单 SKU：无法证明')
