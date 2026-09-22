# -*- coding: utf-8 -*-
"""票 18：同款判断的预算下限（分数低于 matching.min_score 的召回对不交模型判断）。

预算规则不是判定：被挡下的对保持「没判过」＝未知、不产生任何边，被挡下的商品状态为
「候选低于判断下限，未判断」——与低把握／未召回同档，计入控件的「已判断」（重试改变
不了它，放开来要调低下限重启分析程序）。

样本用 8×8 黑白位图控制分数：位图分四段 16 位就是四个视觉 token 的取值，左右两半图
互相一段不共，于是分数＝名称双字重合数＋视觉段重合数，可逐对精算。判断路径只替换
外部传输（test_matching 的 ModelTransport）。
"""
import base64
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import test_matching
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect
from bestseller_monitor.matching import (MATCH_JUDGED, STATUS_BELOW_FLOOR, STATUS_CACHE, STATUS_MODEL,
                                         MatchingConfig, MatchingService, candidate_pairs, identity,
                                         scored_pairs)
from bestseller_monitor.product_images import evidence
from helpers import submit_offer


def bitmap(rows):
    """8×8 黑白位图（'1'＝白、'0'＝黑）：白像素恰 32 个，均值落在黑白之间、位图原样还原。"""
    image = Image.new('L', (8, 8))
    image.putdata([255 if bit == '1' else 0 for row in rows for bit in row])
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


LEFT = ['00001111'] * 8      # 左半白：四段 token 全同
RIGHT = ['11110000'] * 8     # 右半白：与 LEFT 一段不共
TOP = ['11111111'] * 4 + ['00000000'] * 4      # 上半白：与 LEFT／RIGHT 一段不共
BOTTOM = ['00000000'] * 4 + ['11111111'] * 4   # 下半白：与 TOP 一段不共


def product(number, name, rows=None, shop=None):
    """一件测试商品；给了 rows 就带一张 8×8 位图作图片证据。"""
    return dict(shop_key=shop or 'S'+str(number), offer_id=str(number), product_name=name,
                image_hash=None if rows is None else 'h'+str(number),
                image_data=None if rows is None else 'data:image/png;base64,'+base64.b64encode(bitmap(rows)).decode(),
                information_complete=True, sales=number)


class FloorConfigTests(unittest.TestCase):
    """配置键：字段、缺省、校验与配置文件读取。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_default_is_four_and_zero_means_no_limit(self):
        self.assertEqual(MatchingConfig(Path(self.tmp.name)/'c.sqlite').min_score, 4)
        self.assertEqual(MatchingConfig(Path(self.tmp.name)/'c.sqlite', min_score=0).min_score, 0)

    def test_invalid_floor_is_rejected(self):
        for bad in (-1, 101, 4.0, True, '4'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                MatchingConfig(Path(self.tmp.name)/'c.sqlite', min_score=bad)

    def test_config_file_reads_the_key_and_defaults_when_absent(self):
        path = Path(self.tmp.name)/'analysis.toml'
        path.write_text('[analysis]\ndatabase="inventory.sqlite"\n[matching]\nmode="direct"\nmin_score=6\n',
                        encoding='utf-8')
        self.assertEqual(AnalysisConfig.from_file(path).matching.min_score, 6)
        path.write_text('[analysis]\ndatabase="inventory.sqlite"\n[matching]\nmode="direct"\n', encoding='utf-8')
        self.assertEqual(AnalysisConfig.from_file(path).matching.min_score, 4)


class FloorScoreTests(unittest.TestCase):
    """召回入口：分数＝共享 token 数，candidate_pairs 返回同一批对、只丢分数。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_scored_pairs_counts_shared_tokens(self):
        products = [product(1, '月牙杯', LEFT), product(2, '月牙杯', RIGHT),
                    product(3, '月牙碗', LEFT)]
        scores = scored_pairs(products, 6)
        self.assertEqual(scores[(0, 1)], 2)   # 两个名称双字，视觉四段全不共
        self.assertEqual(scores[(0, 2)], 5)   # 一个名称双字，视觉四段全共
        self.assertEqual(scores[(1, 2)], 1)   # 一个名称双字，视觉一段不共
        self.assertEqual(sorted(scores), candidate_pairs(products, 6))
        self.assertEqual(candidate_pairs(products, 6), [(0, 1), (0, 2), (1, 2)])

    def test_generic_buckets_do_not_count_as_evidence(self):
        """挤进通用大桶的共享 token 不记分：倒排表对超过 128 件的桶只收前 128 件。

        130 件同图、名称各不相干（单字名出不了双字片段）：共享的只有那张图的四个视觉
        token，它们的桶满在第 128 件，129 号哪个桶都没进——它与 0 号的对照样召回得到，
        分数却是 0。分数是预算刻度，不是「有共享就一定算证据」。
        """
        products = [product(i, chr(0x4e00+i), LEFT) for i in range(130)]
        scores = scored_pairs(products, 6)
        self.assertEqual(scores[(0, 1)], 4)     # 早入表的两件：四个视觉 token 全算
        self.assertEqual(scores[(0, 129)], 0)   # 晚入表的 129 号：一个都不算


class FloorSuggestTests(unittest.TestCase):
    """判断路径：低于下限的对不进 todo／errors、不写判断行、不产生任何边。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = test_matching.ModelTransport()
        # 被挡下的对若真判了都是同款——挡住它们才会让 2 落到 {1,3} 的组外。
        self.transport.decisions[('月牙杯', '月牙杯')] = True
        self.transport.decisions[('月牙杯', '月牙碗')] = True
        self.transport.decisions[('月牙杯', '陶瓷饮具')] = True
        self.transport.decisions[('玻璃花瓶', '玻璃花瓶')] = True
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=self.transport).start()

    def config(self, min_score=4):
        return MatchingConfig(Path(self.tmp.name)/'cache.sqlite', mode='direct', min_score=min_score)

    def scenario(self):
        """五对召回、分数 1–5：够得着缺省下限的 (1,3)=5、(2,4)=4，其余 2、1、3 全被挡。

        1 月牙杯 左半白｜2 月牙杯 右半白｜3 月牙碗 左半白｜4 陶瓷饮具 右半白｜
        5 玻璃花瓶 上半白｜6 玻璃花瓶 下半白
        """
        return [product(1, '月牙杯', LEFT, 'A01'), product(2, '月牙杯', RIGHT, 'A02'),
                product(3, '月牙碗', LEFT, 'A03'), product(4, '陶瓷饮具', RIGHT, 'A04'),
                product(5, '玻璃花瓶', TOP, 'B01'), product(6, '玻璃花瓶', BOTTOM, 'B02')]

    def suggest(self, products, min_score=4, service=None):
        service = service or MatchingService(self.config(min_score))
        with self.assertLogs('bestseller_monitor.matching', level='INFO') as captured:
            groups = service.suggest(products, test_matching.singles(products))
        return service, groups, [record.getMessage() for record in captured.records]

    @staticmethod
    def sizes(groups):
        return sorted(len(group['members']) for group in groups)

    def compares(self):
        return sum('Compare the same' in call['messages'][0]['content'] for call in self.transport.calls)

    def test_floor_blocks_calls_keeps_recall_count_and_leaves_no_edges(self):
        products = self.scenario()
        _, groups, lines = self.suggest(products)
        # 只判够得着下限的两对（1-3、2-4 都判成同款）：其余三对被挡在门外。
        self.assertEqual(self.compares(), 2)
        # 被挡下的对不产生任何边：2 只与 4 同组，不与 1、3 同组（它们在没下限时是三件一组）。
        self.assertEqual(self.sizes(groups), [1, 1, 2, 2])
        grouped = {frozenset(member['offer_id'] for member in group['members'])
                   for group in groups if len(group['members']) == 2}
        self.assertEqual(grouped, {frozenset(('1', '3')), frozenset(('2', '4'))})
        # 收尾行的「召回 N 对」仍按全部召回对计（5 对），挡下另起一行（3 对）。
        self.assertIn('召回 5 对', lines[-1])
        self.assertIn('新判 2', lines[-1])
        self.assertIn('失败 0', lines[-1])
        self.assertIn('低于判断下限挡下 3 对', '\n'.join(lines))
        # 被挡下的对不写判断行。
        with closing(sqlite3.connect(self.config().cache)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM judgments').fetchone()[0], 2)

    def test_blocked_products_get_their_own_status_not_judged_negative(self):
        products = self.scenario()
        self.suggest(products)
        statuses = {p['offer_id']: p['matching_status'] for p in products}
        # 召回全被挡下、又没进任何已判对的两件：新状态，不是「未召回候选」，更不是判否。
        self.assertEqual(statuses['5'], STATUS_BELOW_FLOOR)
        self.assertEqual(statuses['6'], STATUS_BELOW_FLOOR)
        # 进了已判对的（含只判否的 2、4）照旧记判断来源，不是被挡下。
        self.assertEqual([statuses[offer] for offer in ('1', '2', '3', '4')], [STATUS_MODEL]*4)
        self.assertEqual([p['matching_state'] for p in products[4:]], [MATCH_JUDGED, MATCH_JUDGED])

    def test_blocked_products_get_their_evidence_index_rows(self):
        """回放器要能复算这批商品的召回：证据索引行照写。"""
        products = self.scenario()
        self.suggest(products)
        with closing(sqlite3.connect(self.config().cache)) as conn:
            indexed = {row[0] for row in conn.execute('SELECT identity FROM evidence')}
        self.assertIn(identity(products[4]), indexed)
        self.assertIn(identity(products[5]), indexed)

    def test_second_run_keeps_the_blocked_status_and_calls_nothing(self):
        products = self.scenario()
        service, _, _ = self.suggest(products)
        self.transport.calls.clear()
        _, _, lines = self.suggest(products, service=service)
        self.assertEqual(self.compares(), 0)
        self.assertEqual([p['matching_status'] for p in products[4:]], [STATUS_BELOW_FLOOR]*2)
        self.assertEqual([p['matching_status'] for p in products[:4]], [STATUS_CACHE]*4)
        # 被挡下的对每趟都照旧挡下（召回是每趟重算的），只是不花调用。
        self.assertIn('本机命中 2', lines[-1])
        self.assertIn('新判 0', lines[-1])
        self.assertIn('召回 5 对', lines[-1])

    def test_a_judged_pair_below_the_floor_is_still_a_cache_hit(self):
        """下限只管花不花钱：已判过的对照旧命中缓存，不算被挡下、也不摘掉它的边（ADR-0040 决策 3）。

        演示路径的往返（零下限判一遍 → 调回缺省下限再跑）就走这条：被挡下的定义是「没判过
        又够不着下限」，不是「分数低」——否则已判的对会被说成「未判断」，它的边却还在装配里。
        """
        products = self.scenario()
        service, _, _ = self.suggest(products, min_score=0)           # 零下限先判一遍：五对全判
        self.transport.calls.clear()
        _, groups, lines = self.suggest(products, service=service)    # 再按缺省下限 4 跑
        self.assertEqual(self.compares(), 0)
        self.assertEqual(self.sizes(groups), [1, 2, 3])               # 与零下限那趟逐组一致
        self.assertIn('本机命中 5', lines[-1])
        self.assertIn('新判 0', lines[-1])
        self.assertNotIn('低于判断下限挡下', '\n'.join(lines))
        self.assertEqual([p['matching_status'] for p in products[4:]], [STATUS_CACHE]*2)

    def test_blocked_pairs_are_not_counted_as_failures(self):
        """模型整趟失败时，被挡下的对既不算失败也不写判断行。"""
        self.transport.fail_comparisons = True
        products = self.scenario()
        self.suggest(products)
        _, _, lines = self.suggest(products)
        self.assertIn('失败 2 对', lines[-1])
        self.assertIn('低于判断下限挡下 3 对', '\n'.join(lines))
        self.assertEqual([p['matching_status'] for p in products[4:]], [STATUS_BELOW_FLOOR]*2)
        with closing(sqlite3.connect(self.config().cache)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM judgments').fetchone()[0], 0)

    def test_zero_floor_reproduces_the_previous_behaviour(self):
        products = self.scenario()
        _, groups, lines = self.suggest(products, min_score=0)
        self.assertEqual(self.compares(), 5)
        self.assertEqual(self.sizes(groups), [1, 2, 3])   # 1-2-3 成团、5-6 成对
        self.assertNotIn('低于判断下限挡下', '\n'.join(lines))
        self.assertEqual([p['matching_status'] for p in products], [STATUS_MODEL]*6)

    def test_floor_is_inclusive(self):
        """分数恰好等于下限的对照判（≥ 下限，不是 ＞）。"""
        products = [product(1, '月牙杯', RIGHT, 'A01'), product(2, '陶瓷饮具', RIGHT, 'A02')]
        self.suggest(products)      # 名称双字不共、视觉四段全共：分数恰 4
        self.assertEqual(self.compares(), 1)
        self.assertEqual([p['matching_status'] for p in products], [STATUS_MODEL, STATUS_MODEL])


class FloorServiceTests(unittest.TestCase):
    """快照层：被挡下的商品进控件的「已判断」，按钮没有可重试的失败。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'inventory.db'
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        for key in ('A01', 'A02'):
            self.conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES (?,?)", (key, '店铺'+key))
        self.conn.commit()
        self.transport = test_matching.ModelTransport()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=self.transport).start()

    def service(self, min_score):
        matching = MatchingConfig(Path(self.tmp.name)/'matching.sqlite', mode='direct', min_score=min_score)
        return AnalysisService(AnalysisConfig(self.path, matching=matching), running=lambda: False)

    def submit_pair(self):
        """跨店同名两件、图片各占一半（左右半白）：召回分只有名称双字＝2，够不着缺省下限 4。"""
        for offer, shop, rows in [('11', 'A01', LEFT), ('22', 'A02', RIGHT)]:
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 80)]:
                submit_offer(self.db, offer, day, stock, name='月牙杯', shop_key=shop,
                             image_url='https://img.example/'+offer, image_evidence=evidence(bitmap(rows)))

    def test_floored_product_counts_as_judged_and_needs_no_retry(self):
        self.submit_pair()
        snapshot = self.service(4).start('2026-09-07', '2026-09-14')
        self.assertEqual([p['matching_status'] for p in snapshot['products']], [STATUS_BELOW_FLOOR]*2)
        self.assertEqual([p['matching_state'] for p in snapshot['products']], [MATCH_JUDGED]*2)
        self.assertEqual(snapshot['matching']['judged'], 2)
        self.assertEqual(snapshot['matching']['retryable'], 0)
        self.assertEqual(self.transport.calls, [])   # 一对都没交出去

    def test_zero_floor_judges_the_same_pair(self):
        self.submit_pair()
        snapshot = self.service(0).start('2026-09-07', '2026-09-14')
        self.assertEqual([p['matching_status'] for p in snapshot['products']], [STATUS_MODEL]*2)
        self.assertEqual(snapshot['matching']['judged'], 2)
        self.assertEqual(len(self.transport.calls), 2)   # 一次核验＋一次判断


if __name__ == '__main__':
    unittest.main()
