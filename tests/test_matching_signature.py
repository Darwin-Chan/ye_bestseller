"""票 02：判断按署名并存、只消费同署名。

服务层边界：`MatchingService.suggest` 在真实缓存上的行为——命中、模型调用数、分组
依据与来源列；外部只替换模型传输（test_matching 的 ModelTransport）。老缓存的升级
拿一张旧形状的库文件当夹具。人工决定账本的来源列走 `AnalysisService` 的保存路径与
`DraftStore` 的写入接口。
"""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.analysis_store import DraftStore
from bestseller_monitor.db import Database, connect
from bestseller_monitor.matching import (STATUS_CACHE, STATUS_MODEL, MatchingConfig, MatchingService,
                                         ModelConfig, identity, judgment_sources, version)
from helpers import submit_offer
from test_matching import ModelTransport, product, singles


class SignatureCoexistenceTests(unittest.TestCase):
    """同一对证据版本、不同署名各留各的；本机只把同署名当命中。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / 'cache.sqlite'
        self.transport = ModelTransport()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=self.transport).start()

    def matcher(self, model='deepseek-chat', machine_id='', cache=None):
        config = MatchingConfig(cache or self.cache, ModelConfig(model=model), mode='direct')
        return MatchingService(config, machine_id=machine_id)

    def test_foreign_signature_rows_are_kept_but_never_consumed(self):
        products = [product(1, name='月牙杯'), product(2, 'blue', '月牙杯')]
        self.transport.decisions[('月牙杯', '月牙杯')] = True
        self.matcher('model-a').suggest(products, singles(products))
        # 本机换成模型 B，结论相反：A 的判断是异署名——不占「已判断」，也不驱动本机组。
        self.transport.decisions[('月牙杯', '月牙杯')] = False
        calls = len(self.transport.calls)
        groups = self.matcher('model-b').suggest(products, singles(products))
        self.assertGreater(len(self.transport.calls), calls)
        self.assertEqual([p['matching_status'] for p in products], [STATUS_MODEL, STATUS_MODEL])
        self.assertEqual(len(groups), 2)
        # 两条并存、互不覆盖：换回 A，零调用命中它自己那条。
        calls = len(self.transport.calls)
        groups = self.matcher('model-a').suggest(products, singles(products))
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual([p['matching_status'] for p in products], [STATUS_CACHE, STATUS_CACHE])
        self.assertEqual(len(groups), 1)

    def test_sources_record_the_first_machine_and_keep_first_come(self):
        products = [product(1), product(2)]
        self.transport.decisions[('杯子', '杯子')] = True
        self.matcher(machine_id='m1').suggest(products, singles(products))
        self.assertEqual(set(judgment_sources(self.cache).values()), {'m1'})
        # 本机（m4）跑同一份配置：同署名直接命中，先到的那条来源不被改写。
        calls = len(self.transport.calls)
        self.matcher(machine_id='m4').suggest(products, singles(products))
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(set(judgment_sources(self.cache).values()), {'m1'})
        # 另一张全新的缓存：本机产生的判断记本机编号。
        own = Path(self.tmp.name) / 'own.sqlite'
        self.matcher(machine_id='m4', cache=own).suggest(products, singles(products))
        self.assertEqual(set(judgment_sources(own).values()), {'m4'})


class LegacyCacheUpgradeTests(unittest.TestCase):
    """老缓存（主键只有版本对）升级到按署名并存：升一次、命中照旧、来源记本机。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / 'cache.sqlite'
        self.transport = ModelTransport()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=self.transport).start()

    def matcher(self, machine_id=''):
        return MatchingService(MatchingConfig(self.cache, mode='direct'), machine_id=machine_id)

    def downgrade_to_legacy(self):
        """把缓存改回旧形状：只有版本对主键，没有署名列与来源列。"""
        with closing(sqlite3.connect(self.cache)) as conn:
            rows = conn.execute('SELECT pair,evidence_a,evidence_b,result FROM judgments').fetchall()
        self.cache.unlink()
        with closing(sqlite3.connect(self.cache)) as legacy:
            legacy.execute('CREATE TABLE judgments (pair TEXT PRIMARY KEY, evidence_a TEXT,'
                           ' evidence_b TEXT, result TEXT)')
            legacy.executemany('INSERT INTO judgments VALUES (?,?,?,?)', rows)
            legacy.commit()

    def test_legacy_rows_are_upgraded_and_keep_hitting(self):
        products = [product(1), product(2)]
        self.transport.decisions[('杯子', '杯子')] = True
        self.matcher(machine_id='m1').suggest(products, singles(products))
        self.downgrade_to_legacy()
        # 升级后照常命中：重开不重付模型钱；来源记本机（升级前只有本机能写这张表）。
        calls = len(self.transport.calls)
        groups = self.matcher(machine_id='m4').suggest(products, singles(products))
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual([p['matching_status'] for p in products], [STATUS_CACHE, STATUS_CACHE])
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(judgment_sources(self.cache).values()), {'m4'})
        with closing(sqlite3.connect(self.cache)) as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(judgments)')}
        self.assertLessEqual({'signature', 'machine_id'}, columns)
        # 再开一次：已经升过，不再重建（行为与数据都不变）。
        calls = len(self.transport.calls)
        self.matcher(machine_id='m4').suggest(products, singles(products))
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(set(judgment_sources(self.cache).values()), {'m4'})

    def test_a_broken_row_stops_the_upgrade_without_losing_anything(self):
        """升级要么整表升完、要么原样不动：坏行在动表之前就炸，老判断一条不丢。"""
        products = [product(1), product(2)]
        self.transport.decisions[('杯子', '杯子')] = True
        self.matcher(machine_id='m1').suggest(products, singles(products))
        self.downgrade_to_legacy()
        with closing(sqlite3.connect(self.cache)) as legacy:
            legacy.execute("INSERT INTO judgments VALUES ('p9','v9','v8','{broken json')")
            legacy.commit()
            before = legacy.execute('SELECT COUNT(*) FROM judgments').fetchone()[0]
        with self.assertRaises(ValueError):
            self.matcher(machine_id='m4').suggest(products, singles(products))
        with closing(sqlite3.connect(self.cache)) as conn:
            self.assertNotIn('signature', {row[1] for row in conn.execute('PRAGMA table_info(judgments)')})
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM judgments').fetchone()[0], before)
        # 坏行修掉后照常升级、照常命中。
        with closing(sqlite3.connect(self.cache)) as conn:
            conn.execute("DELETE FROM judgments WHERE pair='p9'")
            conn.commit()
        calls = len(self.transport.calls)
        groups = self.matcher(machine_id='m4').suggest(products, singles(products))
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(len(groups), 1)

    def test_a_failed_rebuild_rolls_back_and_leaves_the_legacy_table_intact(self):
        """升级中途失败（回填撞主键）也不留半截：事务回滚，老表原样、一行不丢。"""
        products = [product(1), product(2)]
        self.transport.decisions[('杯子', '杯子')] = True
        self.matcher(machine_id='m1').suggest(products, singles(products))
        with closing(sqlite3.connect(self.cache)) as conn:
            rows = conn.execute('SELECT pair,evidence_a,evidence_b,result FROM judgments').fetchall()
        self.cache.unlink()
        with closing(sqlite3.connect(self.cache)) as legacy:
            # 没有主键的旧表 + 一条重复版本对：升级回填时才会撞上新主键。
            legacy.execute('CREATE TABLE judgments (pair TEXT, evidence_a TEXT, evidence_b TEXT, result TEXT)')
            legacy.executemany('INSERT INTO judgments VALUES (?,?,?,?)', rows + [rows[0]])
            legacy.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.matcher(machine_id='m4').suggest(products, singles(products))
        with closing(sqlite3.connect(self.cache)) as conn:
            self.assertNotIn('signature', {row[1] for row in conn.execute('PRAGMA table_info(judgments)')})
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM judgments').fetchone()[0], len(rows) + 1)

    def test_refresh_reads_a_legacy_cache_and_upgrades_it_in_place(self):
        """确认页的编辑走 refresh（照旧只读优先）：老形状的库也读得出正例，顺带升一次级。"""
        products = [product(1, name='月牙杯'), product(2, 'blue', '月牙杯')]
        self.transport.decisions[('月牙杯', '月牙杯')] = True
        self.matcher(machine_id='m1').suggest(products, singles(products))
        self.downgrade_to_legacy()
        self.matcher(machine_id='m4').refresh_candidates(products, singles(products))
        self.assertEqual([p['match_label'] for p in products], ['匹配唯一同款', '匹配唯一同款'])
        self.assertEqual(set(judgment_sources(self.cache).values()), {'m4'})


class DecisionSourceTests(unittest.TestCase):
    """三张人工决定表加来源机器列：本机写入记本机编号，异来源行读得出、不被改写。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inventory.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES ('A01','店铺1')")
        self.conn.commit()
        for offer, name in [('11', '月牙杯'), ('22', '云朵杯'), ('33', '树叶杯')]:
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 80)]:
                submit_offer(self.db, offer, day, stock, name=name, color='red')
        self.service = self.open_service()

    def open_service(self, machine='m4'):
        return AnalysisService(AnalysisConfig(self.path, machine=machine), running=lambda: False)

    def group_for(self, snapshot, offer):
        return next(g for g in snapshot['groups'] if any(m['offer_id'] == offer for m in g['members']))

    def join(self, sid, offer, target_offer):
        snapshot = self.service.get(sid)
        source, target = self.group_for(snapshot, offer), self.group_for(snapshot, target_offer)
        member = next(m for m in source['members'] if m['offer_id'] == offer)
        return self.service.edit_group(sid, 'move', source['id'], member, target['id'])

    def test_local_decisions_record_the_local_machine(self):
        first = self.service.start('2026-09-07', '2026-09-14')
        sid = first['id']
        self.join(sid, '22', '11')
        self.join(sid, '33', '11')
        self.service.confirm(sid, self.group_for(self.service.get(sid), '11')['id'])
        self.service.save_draft(sid)
        # 再把树叶杯移出已确认组（排除对进账本），并把它单独确认成独立组。
        snapshot = self.service.get(sid)
        group = self.group_for(snapshot, '11')
        self.service.edit_group(sid, 'remove', group['id'], {'shop_key': 'A01', 'offer_id': '33'})
        self.service.confirm(sid, self.group_for(self.service.get(sid), '33')['id'])
        self.service.save_draft(sid)

        ledger = self.service.store.ledger()
        self.assertEqual([relation['machine_id'] for relation in ledger['relations']], ['m4'])
        self.assertEqual([entry[2] for entry in ledger['standalone']], ['m4'])
        self.assertEqual({entry[2] for entry in ledger['excluded']}, {'m4'})

    def test_legacy_draft_ledger_gains_the_source_column_on_open(self):
        """老草稿库（账本还没有来源列）打开时补列：补列前的行只有本机能写，记本机编号。"""
        first = self.service.start('2026-09-07', '2026-09-14')
        self.join(first['id'], '22', '11')
        self.service.confirm(first['id'], self.group_for(self.service.get(first['id']), '11')['id'])
        self.service.save_draft(first['id'])
        with closing(sqlite3.connect(self.service.config.store)) as conn:
            conn.execute('ALTER TABLE manual_relations RENAME TO legacy_relations')
            conn.execute('CREATE TABLE manual_relations (members TEXT PRIMARY KEY,'
                         ' confirmed INTEGER NOT NULL, saved_at TEXT NOT NULL)')
            conn.execute('INSERT INTO manual_relations(members,confirmed,saved_at)'
                         ' SELECT members,confirmed,saved_at FROM legacy_relations')
            conn.execute('DROP TABLE legacy_relations')
            conn.commit()
        ledger = self.service.store.ledger()
        self.assertEqual([relation['machine_id'] for relation in ledger['relations']], ['m4'])

    def test_a_foreign_relation_keeps_its_source_when_members_are_all_in_view(self):
        """成员全在场的关系折进本机确认组再写回：来源随关系带走，不被改成本机编号。"""
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        members = [[identity(p), version(p)] for p in snapshot['products'] if p['offer_id'] in ('11', '22')]
        DraftStore(self.service.config.store, machine='m1').write(
            'foreign', '2026-09-01', '2026-09-02', '2026-09-20T00:00:00+00:00', {'products': []},
            {'relations': [{'members': members, 'confirmed': True, 'machine_id': 'm1'}],
             'standalone': [], 'excluded': []})
        again = self.service.start('2026-09-07', '2026-09-14')      # m1 的确认组直接生效
        self.assertTrue(self.group_for(again, '11')['confirmed'])
        self.service.save_draft(again['id'])                        # 本机再保存一次
        relations = [relation for relation in self.service.store.ledger()['relations']
                     if [member[0] for member in relation['members']] == sorted(m[0] for m in members)]
        self.assertEqual([relation['machine_id'] for relation in relations], ['m1'])

    def test_foreign_decision_keeps_its_source_across_a_save(self):
        absent_a, absent_b = identity({'shop_key': 'A01', 'offer_id': '55'}), identity({'shop_key': 'A01', 'offer_id': '66'})
        leaf = next(p for p in self.service.start('2026-09-07', '2026-09-14')['products'] if p['offer_id'] == '33')
        # 预置「来自 m1」的三类决定（导入行在票 03/04 才进来，这里按账本形状直接写）。
        DraftStore(self.service.config.store, machine='m1').write(
            'foreign', '2026-09-01', '2026-09-02', '2026-09-20T00:00:00+00:00', {'products': []},
            {'relations': [{'members': [[absent_a, 'v55'], [absent_b, 'v66']], 'confirmed': True, 'machine_id': 'm1'}],
             'standalone': [[identity(leaf), version(leaf), 'm1']],
             'excluded': [[absent_a, absent_b, 'm1']]})
        self.assertEqual([relation['machine_id'] for relation in self.service.store.ledger()['relations']], ['m1'])

        # 本机重起一次分析并保存：m1 的行不只是读得出，往返一次也不被改写成 m4。
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        self.service.save_draft(snapshot['id'])
        ledger = self.service.store.ledger()
        foreign = next(relation for relation in ledger['relations']
                       if [member[0] for member in relation['members']] == [absent_a, absent_b])
        self.assertEqual(foreign['machine_id'], 'm1')
        self.assertEqual([entry[2] for entry in ledger['standalone']], ['m1'])
        self.assertEqual([entry[2] for entry in ledger['excluded']], ['m1'])


if __name__ == '__main__':
    unittest.main()
