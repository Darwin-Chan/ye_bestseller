"""票 17：确认同款页的「模型匹配同款」控件。

按钮常驻，只有存在可重试的模型判断失败时可用，其余置灰并在悬停给出原因；点击后
无论成败都展示本次结果（已判断商品数、仍未成功的原因与下一步建议）。服务层边界：
AnalysisService 返回的快照（商品的 matching_state 与快照 matching 结论）；浏览器
边界：确认同款页上的控件与状态行。模型场景只替换外部传输（test_matching 的
ModelTransport），分析服务与缓存真实贯通。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect

import test_analysis as browser_fixture
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect
from bestseller_monitor.matching import (MATCH_DISABLED, MATCH_FAILED, MATCH_JUDGED, MATCH_MISSING,
                                         MATCH_NEEDS_CONFIG, MatchingConfig, MatchingService, ModelConfig,
                                         matching_summary)
from helpers import new_round, submit_offer
from test_matching import ModelTransport, picture


class MatchingControlTests(unittest.TestCase):
    """服务层：状态码、快照结论与失败分档。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inventory.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        for key in ('A01', 'A02', 'A03'):
            self.conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES (?,?)", (key, '店铺'+key))
        self.conn.commit()
        self.transport = ModelTransport()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=self.transport).start()

    def service(self, matching=None):
        """每次调用都代表一次重新启动：新进程、新内存、同一份磁盘文件。"""
        return AnalysisService(AnalysisConfig(self.path, matching=matching), running=lambda: False)

    def matching(self, key_env='DEEPSEEK_API_KEY'):
        return MatchingConfig(Path(self.tmp.name)/'matching.sqlite', ModelConfig(key_env=key_env), mode='direct')

    def three_products(self):
        """两件同款月牙杯与一件红色陶瓷饮具：召回出的商品对（11-33、11-22）都可被传输控制。"""
        for offer, shop, name, color in [('11', 'A01', '月牙杯', 'red'), ('33', 'A02', '月牙杯', 'blue'),
                                         ('22', 'A03', '陶瓷饮具', 'red')]:
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 80)]:
                submit_offer(self.db, offer, day, stock, name=name, shop_key=shop, color=color)

    def test_failed_judgements_are_retryable_and_retry_reports_judged_products(self):
        service = self.service(self.matching())
        self.three_products()
        self.transport.fail_comparisons = True
        snapshot = service.start('2026-09-07', '2026-09-14')
        self.assertEqual([p['matching_state'] for p in snapshot['products']], [MATCH_FAILED]*3)
        self.assertEqual(snapshot['matching'], {
            'judged': 0, 'failed': 3, 'retryable': 3, 'config': 0, 'missing_evidence': 0, 'disabled': 0,
            'reasons': ['模型请求失败或响应格式无效，请检查后台配置后重试']})
        self.transport.fail_comparisons = False
        retried = service.retry_matching(snapshot['id'])
        self.assertEqual([p['matching_state'] for p in retried['products']], [MATCH_JUDGED]*3)
        self.assertEqual(retried['matching']['judged'], 3)
        self.assertEqual(retried['matching']['failed'], 0)
        self.assertEqual(retried['matching']['reasons'], [])
        # 判断完成后重试不再请求模型：结论与调用数都不变。
        calls = len(self.transport.calls)
        again = service.retry_matching(retried['id'])
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(again['matching']['judged'], 3)

    def test_config_failure_is_not_retryable(self):
        service = self.service(self.matching(key_env='MATCHING_KEY_NOT_SET'))
        self.three_products()
        with patch.dict(os.environ, {'MATCHING_KEY_NOT_SET': ''}):
            snapshot = service.start('2026-09-07', '2026-09-14')
        self.assertEqual([p['matching_state'] for p in snapshot['products']], [MATCH_NEEDS_CONFIG]*3)
        self.assertEqual(snapshot['matching']['config'], 3)
        self.assertEqual(snapshot['matching']['retryable'], 0)
        self.assertEqual(snapshot['matching']['reasons'], ['模型密钥未配置'])

    def test_low_confidence_and_missing_evidence_are_not_failures(self):
        service = self.service(self.matching())
        self.three_products()
        for day, stock in [('2026-09-07', 20), ('2026-09-14', 10)]:
            submit_offer(self.db, '44', day, stock, name='缺图杯', shop_key='A03')   # 没有图片证据
        self.transport.confidence = .4
        snapshot = service.start('2026-09-07', '2026-09-14')
        states = {p['offer_id']: p['matching_state'] for p in snapshot['products']}
        self.assertEqual(states['44'], MATCH_MISSING)
        self.assertEqual(states['11'], MATCH_JUDGED)
        self.assertEqual(snapshot['matching']['judged'], 3)
        self.assertEqual(snapshot['matching']['missing_evidence'], 1)
        self.assertEqual(snapshot['matching']['failed'], 0)
        # 低把握是完成的判断：文案照旧可见，但它不让按钮可用（重试也不会改判）。
        self.assertIn('低把握，待人工核对', [p['matching_status'] for p in snapshot['products']])

    def test_disabled_matching_is_not_a_failure(self):
        service = self.service(None)     # 没配 [matching]：分析照跑，模型不参与
        self.three_products()
        snapshot = service.start('2026-09-07', '2026-09-14')
        self.assertEqual([p['matching_state'] for p in snapshot['products']], [MATCH_DISABLED]*3)
        self.assertEqual(snapshot['matching']['disabled'], 3)
        self.assertEqual(snapshot['matching']['failed'], 0)
        self.assertEqual(snapshot['matching']['reasons'], [])

    def test_summary_dedupes_reasons_and_counts_every_state(self):
        products = [{'matching_state': MATCH_FAILED, 'matching_status': '模型请求失败或响应格式无效，请检查后台配置后重试'},
                    {'matching_state': MATCH_FAILED, 'matching_status': '模型请求失败或响应格式无效，请检查后台配置后重试'},
                    {'matching_state': MATCH_FAILED, 'matching_status': '视觉描述缺失'},
                    {'matching_state': MATCH_NEEDS_CONFIG, 'matching_status': '模型密钥未配置'},
                    {'matching_state': MATCH_JUDGED, 'matching_status': '低把握，待人工核对'},
                    {'matching_state': MATCH_MISSING, 'matching_status': '缺少完整名称与图片证据'},
                    {'matching_state': MATCH_DISABLED, 'matching_status': '模型匹配未启用'}]
        self.assertEqual(matching_summary(products), {
            'judged': 1, 'failed': 4, 'retryable': 3, 'config': 1, 'missing_evidence': 1, 'disabled': 1,
            'reasons': ['模型请求失败或响应格式无效，请检查后台配置后重试', '视觉描述缺失', '模型密钥未配置']})

    def test_legacy_draft_without_state_codes_is_classified_on_restore(self):
        """票 17 之前保存的草稿只有文案：读回时按文案回填，控件照样判得出亮灰。"""
        service = self.service(self.matching())
        self.three_products()
        self.transport.fail_comparisons = True
        snapshot = service.start('2026-09-07', '2026-09-14')
        service.save_draft(snapshot['id'])
        stored = service.store.read(snapshot['id'])
        legacy = stored.payload
        for product in legacy['products']:
            product.pop('matching_state')
        legacy.pop('matching')
        service.store.write(snapshot['id'], legacy['start'], legacy['end'], stored.saved_at, legacy, service.store.ledger())

        restored = self.service(self.matching()).get(snapshot['id'])
        self.assertEqual([p['matching_state'] for p in restored['products']], [MATCH_FAILED]*3)
        self.assertEqual(restored['matching']['retryable'], 3)
        self.assertEqual(restored['matching']['reasons'], ['模型请求失败或响应格式无效，请检查后台配置后重试'])

    def test_legacy_draft_with_config_failures_still_says_needs_config(self):
        """老草稿的三条配置类文案也要认出来：它们重试无效，不能回填成可重试。"""
        service = self.service(self.matching(key_env='MATCHING_KEY_NOT_SET'))
        self.three_products()
        with patch.dict(os.environ, {'MATCHING_KEY_NOT_SET': ''}):
            snapshot = service.start('2026-09-07', '2026-09-14')
        service.save_draft(snapshot['id'])
        stored = service.store.read(snapshot['id'])
        legacy = stored.payload
        for product in legacy['products']:
            product.pop('matching_state')
        legacy.pop('matching')
        service.store.write(snapshot['id'], legacy['start'], legacy['end'], stored.saved_at, legacy, service.store.ledger())

        restored = self.service(self.matching(key_env='MATCHING_KEY_NOT_SET')).get(snapshot['id'])
        self.assertEqual([p['matching_state'] for p in restored['products']], [MATCH_NEEDS_CONFIG]*3)
        self.assertEqual(restored['matching']['retryable'], 0)
        self.assertEqual(restored['matching']['config'], 3)


class MatchingControlBrowserTests(unittest.TestCase):
    setUp = browser_fixture.AnalysisBrowserTests.setUp
    stop_server = browser_fixture.AnalysisBrowserTests.stop_server
    submit = browser_fixture.AnalysisBrowserTests.submit
    dates = browser_fixture.AnalysisBrowserTests.dates
    switch_tab = browser_fixture.AnalysisBrowserTests.switch_tab

    def use_matcher(self, key_env='DEEPSEEK_API_KEY'):
        self.service.matcher = MatchingService(MatchingConfig(Path(self.tmp.name)/'matching.sqlite',
                                                              ModelConfig(key_env=key_env), mode='direct'))

    def three_products(self):
        """与浏览器夹具同一套商品：11(月牙杯红)、33(月牙杯蓝)、22(陶瓷饮具红)。"""
        self.submit('2026-09-07', 100, name='月牙杯', image_url='https://img.example/a', image_evidence=picture('red'))
        self.submit('2026-09-14', 80, name='月牙杯', image_url='https://img.example/a', image_evidence=picture('red'))
        for shop, offer, color, name in [('A02', '33', 'blue', '月牙杯'), ('A03', '22', 'red', '陶瓷饮具')]:
            for day in ('2026-09-07', '2026-09-14'):
                rid = new_round(self.db, shop, run_date=day)
                self.db.submit_inventory_snapshot(round_id=rid, shop_key=shop, shop_url='https://shop.example', shop_name=shop,
                    offer_id=offer, product_url='https://detail.1688.com/offer/'+offer+'.html', list_title=name, detail_title=name,
                    main_image_url='https://image.example/a', sku_rows=[dict(sku_id='default', sku_name='标准', sku_stock=100)],
                    collected_at=day+'T04:00:00+00:00', attempt=1, image_evidence=picture(color))

    def open_review(self):
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()

    def test_button_reports_the_result_and_grays_out_once_judgements_complete(self):
        self.use_matcher()
        self.three_products()
        transport = ModelTransport()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value'}), patch('bestseller_monitor.matching.urlopen', side_effect=transport):
            transport.fail_comparisons = True
            self.open_review()
            button = self.page.get_by_role('button', name='模型匹配同款')
            expect(button).to_be_enabled()
            expect(self.page.locator('#matchingStatus')).to_have_text('3 个商品模型判断失败，可重试')
            transport.fail_comparisons = False
            button.click()
            expect(self.page.locator('#matchingStatus')).to_have_text('模型匹配完成：3 个商品已判断')
            expect(button).to_be_disabled()
            expect(self.page.locator('#matchControl')).to_have_attribute('title', '本次分析的模型匹配已完成')
            # 结果只属于这一次点击：换组查看后回到常显状态（已无可重试失败，故为空）。
            self.page.locator('.group-choice').nth(1).click()
            expect(self.page.locator('#matchingStatus')).to_be_empty()

    def test_partial_failure_keeps_the_button_usable_and_names_each_reason(self):
        self.use_matcher()
        self.three_products()
        transport = ModelTransport()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value'}), patch('bestseller_monitor.matching.urlopen', side_effect=transport):
            transport.fail_names = {('月牙杯', '月牙杯')}    # 只让 11-33 那一对失败，其余商品对照常判断
            self.open_review()
            button = self.page.get_by_role('button', name='模型匹配同款')
            expect(self.page.locator('#matchingStatus')).to_have_text('2 个商品模型判断失败，可重试')
            button.click()
            expect(self.page.locator('#matchingStatus')).to_have_text(
                '模型匹配完成：1 个商品已判断，2 个仍未成功——模型请求失败或响应格式无效，请检查后台配置后重试，建议重试')
            expect(button).to_be_enabled()
            expect(self.page.locator('#matchControl')).to_have_attribute('title', '')
            # 结果只属于这一次点击：切页签清掉它，常显状态行接手（失败还在，按钮仍可用）。
            self.switch_tab('已确认')
            expect(self.page.locator('#matchingStatus')).to_have_text('2 个商品模型判断失败，可重试')
            self.switch_tab('待确认')
            transport.fail_names = set()
            button.click()
            expect(self.page.locator('#matchingStatus')).to_have_text('模型匹配完成：3 个商品已判断')
            expect(button).to_be_disabled()

    def test_result_names_products_that_never_entered_judgement(self):
        self.use_matcher()
        self.submit('2026-09-07', 100, name='月牙杯', image_url='https://img.example/a', image_evidence=picture('red'))
        self.submit('2026-09-14', 80, name='月牙杯', image_url='https://img.example/a', image_evidence=picture('red'))
        for day, stock in [('2026-09-07', 60), ('2026-09-14', 50)]:
            submit_offer(self.db, '33', day, stock, name='月牙杯', shop_key='A02', color='blue')
        for day, stock in [('2026-09-07', 20), ('2026-09-14', 10)]:
            submit_offer(self.db, '22', day, stock, name='缺图杯', shop_key='A03')   # 只有名称，没有图片证据
        transport = ModelTransport()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value'}), patch('bestseller_monitor.matching.urlopen', side_effect=transport):
            transport.fail_comparisons = True
            self.open_review()
            button = self.page.get_by_role('button', name='模型匹配同款')
            expect(self.page.locator('#matchingStatus')).to_have_text('2 个商品模型判断失败，可重试')
            transport.fail_comparisons = False
            button.click()
            expect(self.page.locator('#matchingStatus')).to_have_text(
                '模型匹配完成：2 个商品已判断；另有 1 个商品缺少完整名称与图片证据，未进入判断')

    def test_button_is_disabled_and_explains_when_matching_is_not_configured(self):
        self.open_review()      # 夹具默认没有 [matching]：没有可判断的模型
        button = self.page.get_by_role('button', name='模型匹配同款')
        expect(button).to_be_disabled()
        expect(self.page.locator('#matchControl')).to_have_attribute(
            'title', '未启用模型匹配（配置 analysis.toml 的 [matching] 后重启分析）')
        expect(self.page.locator('#matchingStatus')).to_be_empty()

    def test_config_failure_leaves_the_button_disabled_and_points_at_the_config(self):
        self.use_matcher(key_env='MATCHING_KEY_NOT_SET')
        self.three_products()
        with patch.dict(os.environ, {'MATCHING_KEY_NOT_SET': ''}):
            self.open_review()
            button = self.page.get_by_role('button', name='模型匹配同款')
            expect(button).to_be_disabled()
            expect(self.page.locator('#matchingStatus')).to_have_text('3 个商品模型判断失败，请检查分析配置后重启分析程序')
            expect(self.page.locator('#matchControl')).to_have_attribute('title', '重试无效：请检查分析配置后重启分析程序')


if __name__ == '__main__':
    unittest.main()
