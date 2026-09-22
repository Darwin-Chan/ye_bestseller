# -*- coding: utf-8 -*-
"""票 19：同款装配改为带约束的相关聚类（缺边当未知）。

装配抽成了纯函数：只吃「商品清单＋输入分组＋已判的正负边＋排除对」，不碰判断缓存、
模型与账本——本文件据此直接构造输入，钉 ADR-0040 的判据 1／2／3 与权重边界。
边按证据版本记（`version()` 一处铸）：样本里每件商品各自一个版本，一条边就是一对商品。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor.matching import (NEGATIVE_WEIGHT, MatchingConfig, _judged_edges, assemble_groups,
                                         identity, prepare_cache, signature_of, version)


def product(number, shop='S'):
    """一件测试商品：版本＝名称＋图片哈希，编号不同则版本不同。"""
    return {'shop_key': shop + str(number), 'offer_id': str(number), 'product_name': '商品' + str(number),
            'image_hash': 'h' + str(number), 'sales': number}


def singles(products):
    """输入分组：每件商品一个单商品组（与分析层传进来的形状同形）。"""
    return [{'id': 'G' + str(i), 'confirmed': False, 'machine': '',
             'members': [{'shop_key': p['shop_key'], 'offer_id': p['offer_id']}]}
            for i, p in enumerate(products)]


def edge(products, first, second):
    """一对商品的已判边：按证据版本，与判断表的键同口径。"""
    by_offer = {p['offer_id']: p for p in products}
    return frozenset((version(by_offer[str(first)]), version(by_offer[str(second)])))


def partition(groups):
    """分组折成「成员编号的集合」列表（断言用），组内组间次序无关。"""
    return sorted(sorted(member['offer_id'] for member in group['members']) for group in groups)


class MissingEdgeTests(unittest.TestCase):
    """判据 1：缺边不阻塞——没判过的对是未知，不再把有正边连着的人拆开。"""

    def setUp(self):
        self.products = [product(number) for number in range(1, 7)]

    def test_unjudged_pairs_do_not_hold_an_otherwise_connected_group_apart(self):
        # 1 与 2／3 未被召回（没判过），与 4／5／6 判过同款：六件按正边连成一簇。
        positive = {edge(self.products, 1, 4), edge(self.products, 1, 5), edge(self.products, 1, 6)}
        groups = assemble_groups(self.products, singles(self.products), positive, set())
        self.assertEqual(partition(groups), [['1', '4', '5', '6'], ['2'], ['3']])

    def test_every_product_lands_in_exactly_one_group(self):
        positive = {edge(self.products, 1, 2)}
        groups = assemble_groups(self.products, singles(self.products), positive, set())
        placed = [identity(member) for group in groups for member in group['members']]
        self.assertEqual(sorted(placed), sorted(identity(p) for p in self.products))
        self.assertEqual(len(placed), len(set(placed)))


class ConstraintTests(unittest.TestCase):
    """判据 2：人工决定是硬约束——冻结组不拆不增员、排除对不并，冲突时约束赢。"""

    def test_a_confirmed_group_stays_whole_despite_conflicting_edges(self):
        products = [product(number) for number in range(1, 4)]
        groups = singles(products)
        groups[0] = {'id': 'G0', 'confirmed': True, 'machine': 'm1',
                     'members': [dict(groups[0]['members'][0]), dict(groups[1]['members'][0])]}
        # 组里有一条判非同款的边、组外有正边拉人：约束赢——不拆、不增员。
        negative = {edge(products, 1, 2)}
        positive = {edge(products, 1, 3), edge(products, 2, 3)}
        output = assemble_groups(products, groups, positive, negative)
        self.assertEqual(partition(output), [['1', '2'], ['3']])
        kept = next(group for group in output if group['id'] == 'G0')
        self.assertTrue(kept['confirmed'])
        self.assertEqual(kept['machine'], 'm1')

    def test_an_adjusted_group_keeps_its_members_in_order(self):
        products = [product(number) for number in range(1, 4)]
        groups = [{'id': 'G7', 'confirmed': False, 'adjusted': True, 'machine': '',
                   'members': [{'shop_key': products[1]['shop_key'], 'offer_id': '2', 'extra': 'x'},
                               {'shop_key': products[0]['shop_key'], 'offer_id': '1'}]},
                  {'id': 'G2', 'confirmed': False, 'machine': '',
                   'members': [{'shop_key': products[2]['shop_key'], 'offer_id': '3'}]}]
        positive = {edge(products, 1, 3), edge(products, 2, 3)}
        output = assemble_groups(products, groups, positive, set())
        kept = next(group for group in output if group['id'] == 'G7')
        self.assertEqual([member['offer_id'] for member in kept['members']], ['2', '1'])   # 次序照旧
        self.assertEqual([set(member) for member in kept['members']], [{'shop_key', 'offer_id'}]*2)
        self.assertEqual(partition(output), [['1', '2'], ['3']])

    def test_exclusions_forbid_merging_even_when_a_positive_edge_crosses(self):
        products = [product(number) for number in range(1, 3)]
        positive = {edge(products, 1, 2)}
        excluded = [(identity(products[0]), identity(products[1]))]
        groups = assemble_groups(products, singles(products), positive, set(), excluded)
        self.assertEqual(partition(groups), [['1'], ['2']])

    def test_an_independently_confirmed_single_blocks_all_merges(self):
        products = [product(number) for number in range(1, 4)]
        groups = singles(products)
        groups[0]['confirmed'] = True          # 独立确认：不与任何其他商品并组
        positive = {edge(products, 1, 2), edge(products, 1, 3), edge(products, 2, 3)}
        output = assemble_groups(products, groups, positive, set())
        self.assertEqual(partition(output), [['1'], ['2', '3']])
        self.assertTrue(next(group for group in output if group['members'][0]['offer_id'] == '1')['confirmed'])

    def test_exclusions_naming_an_absent_product_are_ignored_not_fatal(self):
        """排除对的另一端不在本次分析里（账本按身份生效、跨区间复用）：照常装配，不炸。"""
        products = [product(number) for number in range(1, 3)]
        positive = {edge(products, 1, 2)}
        absent = identity({'shop_key': 'S9', 'offer_id': '99'})
        present = identity(products[0])
        groups = assemble_groups(products, singles(products), positive, set(),
                                 [(absent, present), (present, absent)])
        self.assertEqual(partition(groups), [['1', '2']])


class DeterminismTests(unittest.TestCase):
    """判据 3：同一输入两次装配逐组一致；并列 gain 的取舍由簇 id 对定死。"""

    def fixture(self):
        # 正边 1-2、1-3，负边 2-3：并 (1,2) 与并 (1,3) 的 gain 并列（都是 1），
        # 先并哪对决定了谁留在组外。
        products = [product(number) for number in range(1, 4)]
        positive = {edge(products, 1, 2), edge(products, 1, 3)}
        negative = {edge(products, 2, 3)}
        return products, positive, negative

    def test_the_same_input_assembles_identically(self):
        products, positive, negative = self.fixture()
        first = assemble_groups(products, singles(products), positive, negative)
        second = assemble_groups(products, singles(products), positive, negative)
        self.assertEqual(json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))

    def test_a_gain_tie_goes_to_the_smaller_cluster_id_pair(self):
        products, positive, negative = self.fixture()
        groups = assemble_groups(products, singles(products), positive, negative)
        self.assertEqual(partition(groups), [['1', '2'], ['3']])   # 并列先取 (0,1)
        # 商品顺序一换，并列对换了赢家——先并的那对仍是 id 对更小的那对。
        swapped = [products[0], products[2], products[1]]
        positive = {edge(swapped, 1, 2), edge(swapped, 1, 3)}
        negative = {edge(swapped, 2, 3)}
        output = assemble_groups(swapped, singles(swapped), positive, negative)
        self.assertEqual(partition(output), [['1', '3'], ['2']])


class WeightTests(unittest.TestCase):
    """权重边界（ADR-0040 决策 1）：w=0 只看正边跨越，w 极大时负边一条都不许跨。"""

    def fixture(self):
        # 1-2、2-3 判同款，1-3 判非同款：候选合并的 gain 并列，取 id 对小的那对。
        products = [product(number) for number in range(1, 4)]
        positive = {edge(products, 1, 2), edge(products, 2, 3)}
        negative = {edge(products, 1, 3)}
        return products, positive, negative

    def test_zero_weight_only_counts_positive_crossings(self):
        products, positive, negative = self.fixture()
        output = assemble_groups(products, singles(products), positive, negative, weight=0)
        self.assertEqual(partition(output), [['1', '2', '3']])

    def test_the_shipped_weight_keeps_a_negative_edge_out_of_the_group(self):
        products, positive, negative = self.fixture()
        self.assertEqual(NEGATIVE_WEIGHT, 2.0)
        output = assemble_groups(products, singles(products), positive, negative)
        self.assertEqual(partition(output), [['1', '2'], ['3']])

    def test_a_huge_weight_forbids_any_merge_across_a_negative_edge(self):
        """w 极大＝跨越负边的合并一律不干（E2 实测 w≥4 退化成团装配的极限口径）。

        正边成团的形状：1-2-3 两两判过同款，3 与 4 判过非同款——4 不许把 3 拉走，
        结果就是团装配的划分。
        """
        products = [product(number) for number in range(1, 5)]
        positive = {edge(products, 1, 2), edge(products, 1, 3), edge(products, 2, 3), edge(products, 3, 4)}
        negative = {edge(products, 1, 4), edge(products, 2, 4)}
        output = assemble_groups(products, singles(products), positive, negative, weight=10**6)
        self.assertEqual(partition(output), [['1', '2', '3'], ['4']])


class ShapeTests(unittest.TestCase):
    """形状照旧：新组走 M 序列（跳过已用 id）、按成员集合复用原组 id，组序随前序。"""

    def test_new_groups_skip_used_ids_and_reuse_original_ids_by_member_set(self):
        products = [product(number) for number in range(1, 4)]
        groups = singles(products)             # G0、G1、G2
        groups[1]['id'] = 'M0'                 # 输入里已经有一个 M 编号：序列要跳过它
        positive = {edge(products, 1, 2)}
        output = assemble_groups(products, groups, positive, set())
        self.assertEqual(partition(output), [['1', '2'], ['3']])
        self.assertEqual([group['id'] for group in output], ['M1', 'G2'])
        # 新组的键与旧装配逐字同形（不含 machine 之类的额外键）：重跑对拍按整份 dict 比。
        self.assertEqual([set(group) for group in output], [{'id', 'confirmed', 'members'}]*2)
        self.assertEqual([group['confirmed'] for group in output], [False, False])

    def test_frozen_groups_keep_their_own_keys(self):
        """冻结组按输入原样保留（旧装配的 dict(g) 口径）：组上带的 machine／adjusted 照带。"""
        products = [product(number) for number in range(1, 4)]
        groups = [{'id': 'G5', 'confirmed': True, 'machine': 'm1', 'sales': 7,
                   'members': [{'shop_key': products[0]['shop_key'], 'offer_id': '1'}]},
                  {'id': 'G6', 'confirmed': False, 'members': [{'shop_key': products[1]['shop_key'], 'offer_id': '2'}]},
                  {'id': 'G7', 'confirmed': False, 'members': [{'shop_key': products[2]['shop_key'], 'offer_id': '3'}]}]
        output = assemble_groups(products, groups, {edge(products, 1, 3)}, set())
        kept = next(group for group in output if group['id'] == 'G5')
        self.assertEqual(kept, {'id': 'G5', 'confirmed': True, 'machine': 'm1', 'sales': 7,
                                'members': [{'shop_key': products[0]['shop_key'], 'offer_id': '1'}]})
        self.assertEqual(partition(output), [['1'], ['2'], ['3']])   # 确认组不增员


class JudgedEdgeTests(unittest.TestCase):
    """边读缓存（票 19）：一趟读出正边与负边——低把握算负边（与 E2 实测口径一致）。"""

    def test_split_follows_confidence_and_keeps_other_signatures_out(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        evidence_rows = [('p1', 'vA', 'vB', True, 1.0), ('p2', 'vC', 'vD', True, .5),
                         ('p3', 'vE', 'vF', False, 1.0), ('p4', 'vG', 'vH', True, .8)]
        config = MatchingConfig(Path(tmp.name) / 'cache.sqlite', mode='direct')
        signature = signature_of(config)
        conn = sqlite3.connect(config.cache)
        self.addCleanup(conn.close)
        prepare_cache(conn, 'm1')
        for pair, first, second, same, confidence in evidence_rows:
            conn.execute('INSERT INTO judgments(pair,signature,machine_id,evidence_a,evidence_b,result)'
                         ' VALUES (?,?,?,?,?,?)',
                         (pair, signature, 'm1', first, second, json.dumps({'same': same, 'confidence': confidence})))
        conn.execute('INSERT INTO judgments(pair,signature,machine_id,evidence_a,evidence_b,result)'
                     ' VALUES (?,?,?,?,?,?)',
                     ('foreign', 'another-signature', 'm2', 'vI', 'vJ',
                      json.dumps({'same': True, 'confidence': 1.0})))
        conn.commit()
        positive, negative = _judged_edges(conn, signature)
        self.assertEqual(positive, {frozenset(('vA', 'vB')), frozenset(('vG', 'vH'))})
        self.assertEqual(negative, {frozenset(('vC', 'vD')), frozenset(('vE', 'vF'))})


if __name__ == '__main__':
    unittest.main()
