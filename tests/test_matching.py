"""Real cache / analysis / browser; only the external model transport is controlled."""
import base64
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import expect

import test_analysis as browser_fixture
from bestseller_monitor.matching import (MatchingConfig, MatchingService, ModelConfig,
                                         candidate_pairs, identity)
from bestseller_monitor.product_images import evidence
from helpers import new_round


def picture(color):
    stream = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(stream, format='PNG')
    return evidence(stream.getvalue())


def product(number, color='red', name='杯子'):
    image = picture(color)
    return dict(shop_key='shop'+str(number), offer_id=str(number), product_name=name,
                image_hash=image['hash'], image_data='data:image/png;base64,'+base64.b64encode(image['content']).decode(),
                information_complete=True, sales=number)


def singles(products):
    return [dict(id='G'+str(i), members=[{'shop_key': p['shop_key'], 'offer_id': p['offer_id']}],
                 confirmed=False, sales=p['sales']) for i, p in enumerate(products)]


class ModelTransport:
    def __init__(self):
        self.calls = []
        self.fail_comparisons = False
        self.text_only = False
        self.decisions = {}
        self.fail_names = set()

    def __call__(self, request, timeout):
        payload = json.loads(request.data)
        self.calls.append(payload)
        instruction = payload['messages'][0]['content']
        content = payload['messages'][1]['content']
        images = [Image.open(io.BytesIO(base64.b64decode(item['image_url']['url'].split(',')[1])))
                  for item in content if item['type'] == 'image_url']
        if 'five image blocks' in instruction:
            colors = {(255, 0, 0): 'red', (0, 255, 0): 'green', (0, 0, 255): 'blue'}
            result = {'colors': [] if self.text_only else [colors[images[0].getpixel((i*50+25, 25))] for i in range(5)]}
        elif 'Describe physical' in instruction:
            result = {'description': str(images[0].getpixel((0, 0)))}
        else:
            names = tuple(sorted(json.loads(item['text'])['name'] for item in content if item.get('text', '').startswith('{')))
            if self.fail_comparisons or names in self.fail_names:
                raise OSError('provider error with secret-value')
            visual = [str(im.getpixel((0, 0))) for im in images] or [item['text'] for item in content if item.get('text', '').startswith('Image evidence:')]
            result = {'same': self.decisions.get(names, visual[0] == visual[1]), 'confidence': .99}
        for im in images:
            im.close()
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(result)}}]}).encode())


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = MatchingConfig(Path(self.tmp.name)/'cache.sqlite', mode='direct')
        self.transport = ModelTransport()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value', 'VISION_API_KEY': 'vision-secret'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=self.transport).start()

    def test_cross_shop_visual_matching_persistent_cache_and_changed_evidence(self):
        products = [product(1, name='月牙杯'), product(2, name='陶瓷饮具'), product(3, 'blue', '月牙杯')]
        groups = MatchingService(self.config).suggest(products, singles(products))
        self.assertEqual(sorted(len(g['members']) for g in groups), [1, 2])
        self.assertTrue(all(not g['confirmed'] for g in groups))
        count = len(self.transport.calls)
        products[0]['sales'] = 0
        repeated = MatchingService(self.config).suggest(products, singles(products))
        self.assertEqual(len(self.transport.calls), count)
        self.assertEqual(sum(g['sales'] for g in repeated), 5)
        products[0]['product_name'] = '新版月牙杯'
        MatchingService(self.config).suggest(products, singles(products))
        self.assertEqual(products[0]['origin'], '信息变更')
        self.assertEqual(products[1]['origin'], '新商品')
        after = len(self.transport.calls)
        MatchingService(self.config).suggest(products, singles(products))
        self.assertEqual(len(self.transport.calls), after)
        self.assertEqual(products[0]['origin'], '信息变更')

    def test_text_only_endpoint_and_failures_never_fake_visual_match(self):
        products = [product(1), product(2)]
        self.transport.text_only = True
        matcher = MatchingService(self.config)
        groups = matcher.suggest(products, singles(products))
        self.assertEqual(len(groups), 2)
        self.assertIn('图像能力核验未通过', products[0]['matching_status'])
        self.transport.text_only = False
        self.transport.fail_comparisons = True
        groups = matcher.suggest(products, groups)
        self.assertEqual(len(groups), 2)
        self.assertNotIn('secret-value', json.dumps(products, ensure_ascii=False))
        self.transport.fail_comparisons = False
        self.assertEqual(len(matcher.suggest(products, groups)), 1)

    def test_caption_pipeline_and_manual_facts_take_precedence(self):
        config = MatchingConfig(self.config.cache, vision=ModelConfig(model='vision', key_env='VISION_API_KEY'), mode='caption')
        products = [product(1), product(2), product(3)]
        groups = singles(products)
        groups[0]['confirmed'] = True
        output = MatchingService(config).suggest(products, groups, [(identity(products[1]), identity(products[2]))])
        self.assertEqual(len(output), 3)
        self.assertEqual(output[0]['members'], groups[0]['members'])
        self.assertTrue(output[0]['confirmed'])
        main = [call for call in self.transport.calls if 'Compare the same' in call['messages'][0]['content']]
        self.assertTrue(main)
        self.assertTrue(any('Image evidence:' in item.get('text', '') for item in main[0]['messages'][1]['content']))
        self.assertEqual(sum('Describe physical' in c['messages'][0]['content'] for c in self.transport.calls), 1)

    def test_recall_is_bounded_for_3012_products(self):
        template = product(1)
        products = [{**template, 'offer_id': str(i), 'product_name': '商品 '+str(i)} for i in range(3012)]
        pairs = candidate_pairs(products, 6)
        self.assertGreater(len(pairs), 3000)
        self.assertLessEqual(len(pairs), 3012*6)

    def test_large_incremental_run_deduplicates_identical_model_inputs(self):
        template = product(1)
        products = [{**template, 'offer_id': str(i)} for i in range(3012)]
        self.transport.decisions[('杯子', '杯子')] = False
        groups = MatchingService(self.config).suggest(products, singles(products))
        self.assertEqual(len(groups), 3012)
        self.assertEqual(len(self.transport.calls), 2)  # one capability probe and one evidence pair
        MatchingService(self.config).suggest(products, groups)
        self.assertEqual(len(self.transport.calls), 2)

    def test_partial_retry_and_multiple_candidates_do_not_duplicate_members(self):
        products = [product(1, name='A杯'), product(2, name='B杯'), product(3, name='C杯')]
        self.transport.decisions[('A杯', 'B杯')] = False
        self.transport.fail_names.add(('B杯', 'C杯'))
        matcher = MatchingService(self.config)
        groups = matcher.suggest(products, singles(products))
        initial = len(self.transport.calls)
        self.transport.fail_names.clear()
        groups = matcher.suggest(products, groups)
        self.assertEqual(len(self.transport.calls), initial+1)
        self.assertEqual(products[2]['match_label'], '匹配多组同款')
        self.assertEqual(sum(len(g['members']) for g in groups), 3)
        self.assertEqual(sum(g['sales'] for g in groups), 6)
        groups[0]['adjusted'] = True
        prior_members = copy.deepcopy(groups[0]['members'])
        self.assertEqual(matcher.suggest(products, groups)[0]['members'], prior_members)


class MatchingBrowserTests(unittest.TestCase):
    setUp = browser_fixture.AnalysisBrowserTests.setUp
    stop_server = browser_fixture.AnalysisBrowserTests.stop_server
    submit = browser_fixture.AnalysisBrowserTests.submit
    dates = browser_fixture.AnalysisBrowserTests.dates

    def test_browser_grouping_retry_cache_and_version_change(self):
        self.service.matcher = MatchingService(MatchingConfig(Path(self.tmp.name)/'matching.sqlite', mode='direct'))
        self.submit('2026-09-07', 100, name='月牙杯', image_url='https://img.example/a', image_evidence=picture('red'))
        self.submit('2026-09-14', 80, name='月牙杯', image_url='https://img.example/a', image_evidence=picture('red'))
        for shop, offer, color, name in [('A02', '22', 'red', '陶瓷饮具'), ('A03', '33', 'blue', '月牙杯')]:
            for day in ('2026-09-07', '2026-09-14'):
                rid = new_round(self.db, shop, run_date=day)
                self.db.submit_inventory_snapshot(round_id=rid, shop_key=shop, shop_url='https://shop.example', shop_name=shop,
                    offer_id=offer, product_url='https://detail.1688.com/offer/'+offer+'.html', list_title=name, detail_title=name,
                    main_image_url='https://image.example/a', sku_rows=[dict(sku_id='default', sku_name='标准', sku_stock=100)],
                    collected_at=day+'T04:00:00+00:00', attempt=1, image_evidence=picture(color))
        transport = ModelTransport()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value'}), patch('bestseller_monitor.matching.urlopen', side_effect=transport):
            transport.fail_comparisons = True
            self.dates()
            self.page.get_by_role('button', name='下一步、进入同款确认').click()
            expect(self.page.get_by_role('article')).to_have_count(3)
            expect(self.page.get_by_text('模型请求失败或响应格式无效，请检查后台配置后重试').first).to_be_visible()
            transport.fail_comparisons = False
            self.page.get_by_role('button', name='重试模型匹配').click()
            expect(self.page.get_by_role('article')).to_have_count(2)
            expect(self.page.get_by_role('article').first).to_contain_text('陶瓷饮具')
            expect(self.page.get_by_role('article').first).to_contain_text('月牙杯')
            expect(self.page.get_by_role('button', name='匹配唯一同款')).to_have_count(0)
            calls = len(transport.calls)
            self.page.get_by_role('button', name='重试模型匹配').click()
            expect(self.page.get_by_text('缓存', exact=True).first).to_be_visible()
            self.assertEqual(len(transport.calls), calls)
            self.page.get_by_role('button', name='确认当前分组').first.click()
            expect(self.page.get_by_role('button', name='已确认')).to_have_count(1)
            self.page.get_by_role('button', name='重试模型匹配').click()
            expect(self.page.get_by_role('button', name='已确认')).to_have_count(1)
            rid = self.conn.execute("SELECT MAX(round_id) FROM snapshots WHERE shop_key='A01'").fetchone()[0]
            self.db.submit_inventory_snapshot(round_id=rid, shop_key='A01', shop_url='https://shop.example', shop_name='店铺1',
                offer_id='11', product_url='https://detail.1688.com/offer/11.html', list_title='名称变更杯', detail_title='名称变更杯',
                main_image_url='', sku_rows=[dict(sku_id='red', sku_name='红色', sku_stock=80)],
                collected_at='2026-09-14T04:00:00+00:00', attempt=1, image_evidence=picture('red'))
            self.page.get_by_role('button', name='重新选择日期').click()
            self.dates()
            self.page.get_by_role('button', name='下一步、进入同款确认').click()
            expect(self.page.get_by_text('信息变更', exact=True)).to_be_visible()
            self.assertNotIn('secret-value', self.page.content())
