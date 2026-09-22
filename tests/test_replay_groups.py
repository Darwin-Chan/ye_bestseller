# -*- coding: utf-8 -*-
"""票 20：同款分组回放器（`tools/replay_groups.py`）的用例。

接缝一：`replay_groups.replay(cache, ...)`——一份判断缓存（＋可选草稿库、配置）→ 一份报告；
接缝二：`replay_groups.main(argv)`——命令行、控制台一行式汇总与 `--json` 落盘。

夹具造一份与生产同形的判断缓存：建表走生产的 `prepare_cache`，三类行按生产写缓存的形状塞
（`evidence` 的名称／版本、`judgments` 的（版本对, 署名）与结果、`recommendations` 的 payload）。
期望值全部手算、不从被测函数回算：召回分＝共享 token 数，名称只由两个字母块拼出来
（`aabbcc` 的 token 是 aa/ab/bb/bc/cc，共用几个一眼数得出）；ρ、分数曲线、下限省量与
纯度／覆盖都是小整数比。
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor.analysis import AnalysisConfig
from bestseller_monitor.analysis_store import DraftStore
from bestseller_monitor.matching import (MatchingConfig, digest, identity, prepare_cache,
                                         signature_of, version)
from tools import replay_groups

# 主夹具的五件商品与它们的共享 token 数（手算，别名只用一次所以数得准）：
#   A=`aabbcc`（aa,ab,bb,bc,cc） B=`aabbcc`（同 A） C=`aabb`（aa,ab,bb）
#   D=`xxyy`（xx,xy,yy） E=`aabbc`（aa,ab,bb,bc）
#   A-B 5 · A-E 4 · B-E 4 · A-C 3 · B-C 3 · C-E 3 · 带 D 的对 0（D 谁也不共享 ⇒ 不召回）
NAMES = {'A': 'aabbcc', 'B': 'aabbcc', 'C': 'aabb', 'D': 'xxyy', 'E': 'aabbc'}
# 敏感度夹具：四件商品同一个名称（两两共享 3 个 token ⇒ 六个对全召回），E 仍孤立。
TWIN_NAMES = {'A': 'aabb', 'B': 'aabb', 'C': 'aabb', 'D': 'aabb', 'E': 'xxyy'}
K = 6
FLOOR = 4
# 主夹具的判定：A-B／A-C 判同款，A-E 判同款但低把握（按负边算），C-E 判非同款且低把握；
# B-C 与 B-E 没判过。低把握那条按「把握不到高把握线」数（含判非同款的），与 E2 实测口径一致。
JUDGED = (('A', 'B', True, .9), ('A', 'C', True, .9), ('A', 'E', True, .5), ('C', 'E', False, .5))


def product(letter, names=NAMES, twins=()):
    """一件商品：版本＝名称＋图片哈希，编号不同则版本不同（A 与 B 同名、不同图）。

    `twins` 里的字母与 A 同图：A 与它因此**同名同图＝同一个版本**（一个判断行覆盖两对商品）。
    """
    image_hash = 'hA' if letter in twins else 'h' + letter
    return {'shop_key': 'S', 'offer_id': letter, 'product_name': names[letter],
            'image_hash': image_hash, 'image_data': None, 'sales': 0}


def group_of(*letters, gid=None, confirmed=False):
    """payload 里的一个分组（成员只带 shop_key／offer_id，与程序落库同形）。"""
    return {'id': gid or 'G' + letters[0], 'confirmed': confirmed,
            'members': [{'shop_key': 'S', 'offer_id': letter} for letter in letters], 'sales': 0}


def local_signature(cache):
    """本机署名：与配置无关的那几个缺省值（示例配置里就是这些），供缓存行使用。"""
    return signature_of(MatchingConfig(Path(cache)))


class Workspace:
    """一份临时工作区：判断缓存（可选另写草稿库）＋一份分析配置。"""

    def __init__(self, root: Path, twins=()):
        self.root = root
        self.twins = twins          # 与 A 同名同图的字母（同一个版本）
        self.cache = root / 'matching.sqlite'
        self.store = root / 'analysis-drafts.sqlite'
        self.config = root / 'analysis.toml'

    def build_cache(self, *, judged=JUDGED, groups=None, missing=(), names=NAMES):
        """按生产形状铺一份缓存：`judged` 是 (对, same, confidence)，`missing` 是不写证据行的商品。"""
        self.products = [product(letter, names, self.twins) for letter in 'ABCDE']
        by_letter = {p['offer_id']: p for p in self.products}
        conn = sqlite3.connect(self.cache)
        try:
            prepare_cache(conn, 'm1')
            for p in self.products:
                if p['offer_id'] in missing:
                    continue
                conn.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?)',
                             (identity(p), version(p), p['product_name'], p['image_hash'],
                              p['image_data'], '新商品'))
            for first, second, same, confidence in judged:
                left, right = by_letter[first], by_letter[second]
                conn.execute('INSERT INTO judgments(pair,signature,machine_id,evidence_a,evidence_b,'
                             'result) VALUES (?,?,?,?,?,?)',
                             (digest(sorted([version(left), version(right)])), local_signature(self.cache),
                              'm1', version(left), version(right),
                              json.dumps({'same': same, 'confidence': confidence})))
            groups = groups if groups is not None else [group_of('A', 'B'), group_of('C'),
                                                        group_of('D'), group_of('E')]
            conn.execute('INSERT INTO recommendations(payload) VALUES (?)', (json.dumps({
                'groups': groups,
                'products': [{'identity': identity(p), 'version': version(p),
                              'candidates': [], 'status': '缓存'} for p in self.products]},
                ensure_ascii=False),))
            conn.commit()
        finally:
            conn.close()
        return self

    def write_config(self, *, min_score=FLOOR, candidates=K):
        """一份分析配置：`[matching.model]` 取示例配置的缺省值，好与缓存的署名对上。"""
        self.config.write_text(f'''[analysis]
database = "{(self.root / 'bestseller.db').as_posix()}"

[matching]
mode = "disabled"
cache = "{self.cache.as_posix()}"
candidates = {candidates}
min_score = {min_score}

[matching.model]
endpoint = "https://api.deepseek.com/v1/chat/completions"
model = "deepseek-chat"
key_env = "DEEPSEEK_API_KEY"
timeout = 30
''', encoding='utf-8', newline='\n')
        return self.config

    def write_ledger(self, *, relation=None, excluded=()):
        """草稿库的账本：`relation` 是 (成员, 已确认) 的一行，`excluded` 是排除对。"""
        relations = []
        if relation:
            letters, confirmed = relation
            relations.append({'members': [[identity(product(letter, NAMES, self.twins)),
                                           version(product(letter, NAMES, self.twins))]
                                          for letter in letters],
                              'confirmed': confirmed, 'machine_id': 'm1'})
        DraftStore(self.store, 'm1').write('an-1', '2026-09-21', '2026-09-22',
                                           '2026-09-23T01:00:00+08:00', {},
                                           {'relations': relations, 'standalone': [],
                                            'excluded': [[identity(product(a, NAMES, self.twins)),
                                                          identity(product(b, NAMES, self.twins))]
                                                         for a, b in excluded]})
        return self.store


@contextlib.contextmanager
def captured():
    """把控制台收进 buffer：一行式汇总的断言看它。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


class FixtureTests(unittest.TestCase):
    """夹具自检：手算的召回分与「配置认出的署名＝缓存里写的署名」两条前提。"""

    def test_shared_token_counts_match_the_hand_counting(self):
        products = [product(letter) for letter in 'ABCDE']
        scores = replay_groups.scored_pairs(products, K)
        expected = {frozenset(('A', 'B')): 5, frozenset(('A', 'E')): 4, frozenset(('B', 'E')): 4,
                    frozenset(('A', 'C')): 3, frozenset(('B', 'C')): 3, frozenset(('C', 'E')): 3}
        by_pair = {frozenset((products[i]['offer_id'], products[j]['offer_id'])): score
                   for (i, j), score in scores.items()}
        self.assertEqual({key: by_pair.get(key) for key in expected}, expected)
        self.assertEqual(len(scores), len(expected))          # 带 D 的对一分不共享 ⇒ 不召回

    def test_config_signature_equals_the_caches_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp)).build_cache()
            config = workspace.write_config()
            self.assertEqual(signature_of(AnalysisConfig.from_file(config).matching),
                             local_signature(workspace.cache))


class ReplayNumbersTests(unittest.TestCase):
    """复算的数字：ρ、分数曲线、下限省量、判不动与缺证据的如实计数。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name)).build_cache()
        self.report = replay_groups.replay(self.workspace.cache,
                                           config=self.workspace.write_config())

    def test_recall_and_judged_counts(self):
        report = self.report
        self.assertEqual(report['candidates'], K)
        self.assertEqual(report['recall'], 6)                # 手算的六对有共享 token
        self.assertEqual(report['judged'], 4)                # B-C 与 B-E 没判过（判不动）
        self.assertEqual(report['unjudged'], 2)
        self.assertEqual(report['outside_recall'], 0)
        self.assertEqual(report['positive'], 2)              # A-B、A-C
        self.assertEqual(report['negative'], 2)
        # 低把握＝把握不到高把握线：A-E（判同款）与 C-E（判非同款）两条都算（E2 实测口径）。
        self.assertEqual(report['low_confidence'], 2)
        self.assertAlmostEqual(report['rho'], 2 / 4)

    def test_recall_counts_product_pairs_while_judgments_count_version_pairs(self):
        # A 与 B 同名同图＝同一个版本：判断行按版本对存（一条覆盖两个商品对），
        # 召回按商品对算——与 E2 标定实测的 3803／3786 同口径。
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), twins=('B',)).build_cache(
                judged=(('A', 'C', True, .9),))
            report = replay_groups.replay(workspace.cache, weights=(2.0,))
            self.assertEqual(report['recall'], 6)            # 五件商品两两共享 token 的六对
            self.assertEqual(report['judged'], 1)            # 但判断只有一条（版本对 A/B-C）
            self.assertEqual(report['unjudged'], 5)
            self.assertEqual(report['positive'], 1)

    def test_defaults_come_from_the_production_config(self):
        # 缺省 k 与下限不另抄一份：跟着 `matching.MatchingConfig` 的缺省走。
        from bestseller_monitor.matching import MatchingConfig
        self.assertEqual(replay_groups.DEFAULT_CANDIDATES, MatchingConfig(Path('.')).candidates)
        self.assertEqual(replay_groups.DEFAULT_MIN_SCORE, MatchingConfig(Path('.')).min_score)

    def test_score_curve_buckets_by_recall_score(self):
        curve = self.report['score_curve']
        self.assertEqual(curve['5'], {'pairs': 1, 'positive': 1, 'rate': 1.0})
        self.assertEqual(curve['4'], {'pairs': 1, 'positive': 0, 'rate': 0.0})
        self.assertEqual(curve['3'], {'pairs': 2, 'positive': 1, 'rate': 0.5})

    def test_floor_analysis_counts_blocked_pairs_and_their_positive_edges(self):
        floor = self.report['floor']
        self.assertEqual(floor['min_score'], FLOOR)
        self.assertEqual(floor['blocked'], 2)                # 3 分的 A-C 与 C-E（B-C 没判过、不在预算里）
        self.assertAlmostEqual(floor['blocked_ratio'], 2 / 4)
        self.assertEqual(floor['blocked_positive'], 1)       # 被挡下的 A-C 是正边
        self.assertAlmostEqual(floor['blocked_positive_ratio'], 1 / 2)

    def test_production_weight_is_among_the_default_scan_targets(self):
        # 发行值必须默认出现在扫描里：报告里那一行要能与 payload（生产装配的产物）对着看。
        from bestseller_monitor.matching import NEGATIVE_WEIGHT
        self.assertIn(NEGATIVE_WEIGHT, replay_groups.DEFAULT_WEIGHTS)
        report = replay_groups.replay(self.workspace.cache)
        self.assertEqual(report['weights'], list(replay_groups.DEFAULT_WEIGHTS))
        self.assertEqual(sorted(report['assemblies']),
                         ['clique', 'w1.0', 'w1.5', 'w2.0', 'w3.0', 'w4.0'])
        self.assertEqual(report['assemblies']['w2.0']['weight'], NEGATIVE_WEIGHT)

    def test_missing_evidence_products_are_counted_and_stay_out_of_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp)).build_cache(missing=('D',))
            report = replay_groups.replay(workspace.cache, config=workspace.write_config())
            self.assertEqual([json.loads(entry)[1] for entry in report['evidence_missing']], ['D'])
            self.assertEqual(report['version_mismatch'], 0)   # 缺证据的不算版本不符
            self.assertEqual(report['products'], 5)
            self.assertEqual(report['recall'], 6)             # D 不进任何召回对
            placed = [member['offer_id'] for group in report['assemblies']['w2.0']['partition']
                      for member in group['members']]
            self.assertIn('D', placed)                        # 仍进装配（独立商品）


class AssemblyTests(unittest.TestCase):
    """装配扫描与对照：每权重一行指标、团装配对照实现、与 payload 的逐组对照。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name)).build_cache()
        self.report = replay_groups.replay(self.workspace.cache,
                                           config=self.workspace.write_config(),
                                           weights=(1.0, 2.0))

    def test_weights_scan_reports_one_row_per_weight(self):
        # 缺边不阻塞：B-C 没判过 ⇒ 不挡「{A,B} 与 C」合并（正边跨越 1、负边 0），三件一组；
        # 这一批里没有跨越组界的负边，所以两个档位同划（权重敏感度见下一条用例）。
        row = self.report['assemblies']['w1.0']
        self.assertEqual((row['groups'], row['groups_ge2'], row['largest']), (3, 1, 3))
        self.assertEqual(row['size_hist'], {'3': 1})
        self.assertEqual((row['positive_inside'], row['negative_inside']), (2, 0))
        self.assertEqual((row['coverage'], row['purity']), (1.0, 1.0))
        heavy = self.report['assemblies']['w2.0']
        self.assertEqual((heavy['groups'], heavy['largest']), (3, 3))

    def test_weight_changes_the_partition_on_a_negative_crossing(self):
        # 四件商品两两共享 3 个 token（六个对全召回）：A-B／A-C／A-D／B-C／B-D 判同款、
        # C-D 判非同款。最后一轮合并的跨越是正边 2、负边 1 ⇒ w=1 合并（增益 1）、w=2 停（0）。
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp)).build_cache(
                judged=(('A', 'B', True, .9), ('A', 'C', True, .9), ('A', 'D', True, .9),
                        ('B', 'C', True, .9), ('B', 'D', True, .9), ('C', 'D', False, .9)),
                groups=[group_of('A', 'B', 'C'), group_of('D'), group_of('E')], names=TWIN_NAMES)
            report = replay_groups.replay(workspace.cache, weights=(1.0, 2.0))
            self.assertEqual((report['judged'], report['positive']), (6, 5))
            light = report['assemblies']['w1.0']
            self.assertEqual((light['groups'], light['largest'], light['negative_inside']),
                             (2, 4, 1))
            self.assertEqual((light['coverage'], light['purity']), (1.0, round(5 / 6, 4)))
            heavy = report['assemblies']['w2.0']
            self.assertEqual((heavy['groups'], heavy['largest'], heavy['size_hist']),
                             (3, 3, {'3': 1}))
            # {A,B,C} 组里留下 A-B／A-C／B-C 三条正边，A-D 与 B-D 跨到 D 上（3/5）。
            self.assertEqual((heavy['coverage'], heavy['purity']), (0.6, 1.0))
            # payload 就是团装配那版（[A,B,C],[D],[E]）：w=2 与它逐组一致，w=1 少两组。
            self.assertEqual(report['assemblies']['w2.0']['payload_compare']['identical'], 3)
            self.assertEqual(report['assemblies']['w1.0']['payload_compare'],
                             {'identical': 1, 'replay_groups': 2, 'payload_groups': 3,
                              'replay_only': 1, 'payload_only': 2})

    def test_retired_clique_assembly_is_the_frozen_reference(self):
        row = self.report['assemblies']['clique']
        # 团：A-B 一对一簇；C 与 [A,B] 里的 B 没有正边（B-C 没判过＝挡人）⇒ 自成一组。
        self.assertEqual((row['groups'], row['groups_ge2'], row['largest']), (4, 1, 2))
        self.assertEqual((row['coverage'], row['purity']), (0.5, 1.0))
        self.assertIn('retired', row)                        # 退役日期与原因随报告走

    def test_payload_compare_counts_identical_groups(self):
        compare = self.report['assemblies']['clique']['payload_compare']
        self.assertEqual(compare,
                         {'identical': 4, 'replay_groups': 4, 'payload_groups': 4,
                          'replay_only': 0, 'payload_only': 0})       # payload 就是团装配那版
        self.assertEqual(self.report['assemblies']['w1.0']['payload_compare'],
                         {'identical': 2, 'replay_groups': 3, 'payload_groups': 4,
                          'replay_only': 1, 'payload_only': 2})

    def test_recall_edges_and_assembly_are_the_production_implementations(self):
        from bestseller_monitor import matching
        self.assertIs(replay_groups.scored_pairs, matching.scored_pairs)
        self.assertIs(replay_groups.assemble_groups, matching.assemble_groups)
        self.assertIs(replay_groups._judged_edges, matching._judged_edges)


class ConstraintInputTests(unittest.TestCase):
    """输入分组：`--store` 用真账本；没给时按 payload 的已确认组推导。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name)).build_cache()

    def test_confirmed_ledger_group_is_frozen_and_beats_the_edge(self):
        store = self.workspace.write_ledger(relation=(('A', 'E'), True), excluded=(('B', 'C'),))
        report = replay_groups.replay(self.workspace.cache, store=store, weights=(2.0,))
        row = report['assemblies']['w2.0']
        self.assertEqual(report['frozen_groups'], 1)
        self.assertEqual(report['excluded_pairs'], 1)
        frozen = [entry for entry in row['partition'] if entry['confirmed']]
        self.assertEqual(len(frozen), 1)
        self.assertEqual(sorted(member['offer_id'] for member in frozen[0]['members']), ['A', 'E'])
        # A 被冻结 ⇒ 与 C 的正边不再成组（B,C 只剩单身）；A-E 那条低把握负边留在组内（约束赢）。
        self.assertEqual(row['largest'], 2)
        self.assertEqual(row['negative_inside'], 1)
        self.assertEqual(row['coverage'], 0.0)

    def test_payload_confirmed_group_is_frozen_without_a_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp)).build_cache(
                groups=[group_of('A', 'E', confirmed=True), group_of('B'), group_of('C'),
                        group_of('D')])
            report = replay_groups.replay(workspace.cache, config=workspace.write_config(),
                                          weights=(2.0,))
            self.assertEqual(report['frozen_groups'], 1)
            frozen = [entry for entry in report['assemblies']['w2.0']['partition']
                      if entry['confirmed']]
            self.assertEqual(len(frozen), 1)
            self.assertEqual(sorted(member['offer_id'] for member in frozen[0]['members']), ['A', 'E'])


class CliTests(unittest.TestCase):
    """命令行：一行式汇总、--json 落盘、只读承诺与退出码。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = Workspace(self.root).build_cache()
        self.json_path = self.root / 'replay.json'

    def test_console_summary_has_one_line_per_weight(self):
        with captured() as buffer:
            code = replay_groups.main(['--cache', str(self.workspace.cache), '--weights', '1,2'])
        self.assertEqual(code, 0)
        lines = buffer.getvalue().splitlines()
        self.assertTrue(any(line.startswith('回放读取：') for line in lines))
        self.assertTrue(any('命中判断 4 对（ρ=50.0%，正边 2 · 低把握 2）' in line for line in lines))
        self.assertTrue(any('下限 4：挡下 2 对（判定预算 50.0%） · 挡下正边 1 条（50.0%）' in line
                            for line in lines))
        self.assertTrue(any(line.startswith('装配 w1.0：3 组（≥2 件 1 · 最大 3） · 规模 3人×1')
                            for line in lines))
        self.assertTrue(any(line.startswith('装配 团装配（2026-09-23 退役') for line in lines))

    def test_json_output_is_written_with_lf_and_round_trips(self):
        with captured():
            code = replay_groups.main(['--cache', str(self.workspace.cache),
                                       '--weights', '1,2', '--json', str(self.json_path)])
        self.assertEqual(code, 0)
        raw = self.json_path.read_bytes()
        self.assertNotIn(b'\r\n', raw)
        document = json.loads(raw.decode('utf-8'))
        self.assertEqual(document['recall'], 6)
        self.assertEqual(sorted(document['assemblies']), ['clique', 'w1.0', 'w2.0'])
        self.assertEqual(document['signature_source'], 'cache')

    def test_signature_falls_back_to_the_most_rows_and_says_which(self):
        with captured() as buffer:
            code = replay_groups.main(['--cache', str(self.workspace.cache), '--weights', '2'])
        self.assertEqual(code, 0)
        line = next(line for line in buffer.getvalue().splitlines() if line.startswith('署名：'))
        self.assertIn(local_signature(self.workspace.cache)[:12], line)
        self.assertIn('缓存里行数最多者', line)

    def test_config_supplies_the_signature_and_the_floor(self):
        config = self.workspace.write_config(min_score=5)
        with captured() as buffer:
            code = replay_groups.main(['--cache', str(self.workspace.cache), '--weights', '2',
                                       '--config', str(config)])
        self.assertEqual(code, 0)
        lines = buffer.getvalue().splitlines()
        line = next(line for line in lines if line.startswith('署名：'))
        self.assertIn('--config', line)
        # 下限 5 挡下 4 分的 A-E 与 3 分的 A-C／C-E（共 3 对／4 对）；被挡下的 A-C 正边 1 条。
        self.assertTrue(any('下限 5：挡下 3 对（判定预算 75.0%） · 挡下正边 1 条' in line
                            for line in lines))

    def test_read_only_promise_leaves_the_cache_untouched(self):
        before = self.workspace.cache.read_bytes()
        with captured():
            replay_groups.main(['--cache', str(self.workspace.cache), '--weights', '2'])
        self.assertEqual(self.workspace.cache.read_bytes(), before)
        leftovers = [p.name for p in self.root.glob('matching.sqlite-*')]
        self.assertEqual(leftovers, [])

    def test_a_cache_that_refuses_read_only_access_is_read_through_a_copy(self):
        # 热日志／被写锁占着时 sqlite 不认只读打开：拷一份临时副本、在副本上读（原库不动）。
        from unittest import mock
        before = self.workspace.cache.read_bytes()
        failure = sqlite3.OperationalError('unable to open database file')
        with mock.patch.object(replay_groups, '_open_direct', side_effect=failure):
            conn, note = replay_groups.open_readonly(self.workspace.cache)
            try:
                self.assertEqual(conn.execute('SELECT count(*) FROM judgments').fetchone()[0], 4)
            finally:
                conn.close()
        self.assertIn('已拷临时副本到', note)
        self.assertEqual(self.workspace.cache.read_bytes(), before)
        # 读的还是这份缓存：回放整条路在副本上照样出数。
        with mock.patch.object(replay_groups, '_open_direct', side_effect=failure):
            report = replay_groups.replay(self.workspace.cache, weights=(2.0,))
        self.assertEqual((report['recall'], report['judged']), (6, 4))
        self.assertIn('已拷临时副本到', report['note'])

    def test_bad_inputs_exit_with_a_usable_message(self):
        with captured() as buffer:
            code = replay_groups.main(['--cache', str(self.root / '没有这个库.sqlite')])
        self.assertEqual(code, 1)
        self.assertIn('缓存不存在', buffer.getvalue())
        with self.assertRaises(SystemExit) as caught, captured():
            replay_groups.main(['--cache', str(self.workspace.cache), '--weights', '1,走'])
        self.assertEqual(caught.exception.code, 2)

    def test_no_recommendations_payload_exits_1(self):
        empty = self.root / 'empty.sqlite'
        conn = sqlite3.connect(empty)
        prepare_cache(conn, 'm1')
        conn.commit()
        conn.close()
        with captured() as buffer:
            code = replay_groups.main(['--cache', str(empty)])
        self.assertEqual(code, 1)
        self.assertIn('recommendations', buffer.getvalue())


class ModelCallTests(unittest.TestCase):
    """零模型调用：跑一遍不触碰发模型请求的那一口。"""

    def test_replay_never_reaches_the_model_request(self):
        from unittest import mock
        from bestseller_monitor import matching
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp)).build_cache()
            with mock.patch.object(matching, 'request_json',
                                   side_effect=AssertionError('不许发模型请求')):
                with captured():
                    code = replay_groups.main(['--cache', str(workspace.cache), '--weights', '2'])
        self.assertEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
