"""票 09：新日期区间复用已保存的人工确认与排除。

服务层边界：AnalysisService 的 start／save_draft／save_and_view 与临时库存库、
临时草稿库。手工数字按规格手算，不照抄实现。模型场景只替换外部传输（test_matching
的 ModelTransport），分析服务与缓存真实贯通。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect
from bestseller_monitor.matching import MatchingConfig, identity
from helpers import submit_offer
from test_matching import ModelTransport


class DecisionReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inventory.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES ('A01','店铺1')")
        self.conn.commit()
        # 月牙杯与云朵杯跨越两个区间；树叶杯只在第一个区间有观测。都带图片证据，
        # 版本随名称与图片内容变化（票 04 的信息版本）。
        for offer, name, points in [
            ('11', '月牙杯', [('2026-09-07', 100), ('2026-09-14', 80), ('2026-09-16', 70), ('2026-09-21', 50)]),
            ('22', '云朵杯', [('2026-09-07', 60), ('2026-09-14', 50), ('2026-09-16', 40), ('2026-09-21', 30)]),
            ('33', '树叶杯', [('2026-09-07', 30), ('2026-09-14', 20)]),
        ]:
            for day, stock in points:
                submit_offer(self.db, offer, day, stock, name=name, color='red')
        self.service = self.open_service()

    def open_service(self):
        """每次调用都代表一次重新启动：新进程、新内存、同一份磁盘文件。"""
        return AnalysisService(AnalysisConfig(self.path), running=lambda: False)

    def group_for(self, snapshot, offer):
        return next(g for g in snapshot['groups'] if any(m['offer_id'] == offer for m in g['members']))

    def join(self, sid, offer, target_offer):
        snapshot = self.service.get(sid)
        source = self.group_for(snapshot, offer)
        target = self.group_for(snapshot, target_offer)
        member = next(m for m in source['members'] if m['offer_id'] == offer)
        return self.service.edit_group(sid, 'move', source['id'], member, target['id'])

    def test_new_range_reuses_saved_confirmation_and_recomputes_sales(self):
        # 第一段 7—14：人工把三个杯子并成一组并确认，保存。
        first = self.service.start('2026-09-07', '2026-09-14')
        sid1 = first['id']
        self.join(sid1, '22', '11')
        self.join(sid1, '33', '11')
        self.service.confirm(sid1, self.group_for(self.service.get(sid1), '11')['id'])
        group = self.group_for(self.service.get(sid1), '11')
        self.assertEqual(len(group['members']), 3)
        self.assertEqual(group['sales'], 40)  # 手算：11 日降 20、22 日降 10、33 日降 10
        self.service.save_draft(sid1)

        # 第二段 15—21：只有月牙杯、云朵杯有观测。保存过的确认自动生效，不要求重新确认。
        second = self.service.start('2026-09-15', '2026-09-21')
        self.assertEqual([p['offer_id'] for p in second['products']], ['11', '22'])
        group2 = self.group_for(second, '11')
        self.assertEqual({m['offer_id'] for m in group2['members']}, {'11', '22'})
        self.assertTrue(group2['confirmed'])
        # 新区间按新数据重算：11 日降 30（80 补 15 日、16 日 70、21 日 50），22 日降 20。
        self.assertEqual(group2['sales'], 50)
        # 同一商品只归属一个组，库存不跨分析相加。
        memberships = [m['offer_id'] for g in second['groups'] for m in g['members']]
        self.assertEqual(sorted(memberships), ['11', '22'])
        self.assertEqual({row['offer_id'] for row in second['inventory']}, {'11', '22'})

        self.service.save_draft(second['id'])
        # 重启后新区间的确认仍在，旧分析的结果保留自己的销量。
        restarted = self.open_service()
        self.assertTrue(self.group_for(restarted.get(second['id']), '11')['confirmed'])
        self.assertEqual(self.group_for(restarted.get(sid1), '11')['sales'], 40)

    def test_version_change_keeps_other_members_confirmed_and_flags_change(self):
        first = self.service.start('2026-09-07', '2026-09-14')
        sid1 = first['id']
        self.join(sid1, '22', '11')
        self.join(sid1, '33', '11')
        self.service.confirm(sid1, self.group_for(self.service.get(sid1), '11')['id'])
        self.service.save_draft(sid1)

        # 树叶杯换了图片、名称不变：旧确认不套给新版本，其余成员保持已确认。
        submit_offer(self.db, '33', '2026-09-14', 20, name='树叶杯', color='green')
        second = self.service.start('2026-09-07', '2026-09-14')
        group = self.group_for(second, '11')
        self.assertEqual({m['offer_id'] for m in group['members']}, {'11', '22'})
        self.assertTrue(group['confirmed'])
        pending = self.group_for(second, '33')
        self.assertFalse(pending['confirmed'])
        self.assertEqual({m['offer_id'] for m in pending['members']}, {'33'})
        leaf = next(p for p in second['products'] if p['offer_id'] == '33')
        self.assertEqual(leaf['origin'], '信息变更')
        others = [p['origin'] for p in second['products'] if p['offer_id'] != '33']
        self.assertNotIn('信息变更', others)

        # 人工把树叶杯重新并回并确认、保存：下个区间三个成员一起复用。
        self.join(second['id'], '33', '11')
        self.service.confirm(second['id'], self.group_for(self.service.get(second['id']), '11')['id'])
        self.service.save_draft(second['id'])
        third = self.service.start('2026-09-07', '2026-09-14')
        self.assertEqual({m['offer_id'] for m in self.group_for(third, '11')['members']}, {'11', '22', '33'})
        self.assertTrue(self.group_for(third, '11')['confirmed'])

    def test_partial_save_keeps_the_global_relation_for_absent_members(self):
        # 7—14 确认三个杯子同款并保存；15—21 只见得到前两个。
        first = self.service.start('2026-09-07', '2026-09-14')
        sid1 = first['id']
        self.join(sid1, '22', '11')
        self.join(sid1, '33', '11')
        self.service.confirm(sid1, self.group_for(self.service.get(sid1), '11')['id'])
        self.service.save_draft(sid1)

        second = self.service.start('2026-09-15', '2026-09-21')
        self.assertEqual({m['offer_id'] for m in self.group_for(second, '11')['members']}, {'11', '22'})
        self.service.save_draft(second['id'])

        # 扩大区间找回缺席的树叶杯：全局关系仍在，销量按新区间重算。
        third = self.service.start('2026-09-07', '2026-09-21')
        group = self.group_for(third, '11')
        self.assertEqual({m['offer_id'] for m in group['members']}, {'11', '22', '33'})
        self.assertTrue(group['confirmed'])
        self.assertEqual(group['sales'], 90)  # 手算：50 + 30 + 10
        memberships = [m['offer_id'] for g in third['groups'] for m in g['members']]
        self.assertEqual(len(memberships), len(set(memberships)))
        # 旧草稿保留自己的结果，不被后来的分析改写。
        self.assertEqual(self.group_for(self.open_service().get(sid1), '11')['sales'], 40)

    def test_exclusions_survive_partial_saves_and_reappearing_members(self):
        first = self.service.start('2026-09-07', '2026-09-14')
        sid1 = first['id']
        self.join(sid1, '22', '11')
        self.join(sid1, '33', '11')
        group = self.group_for(self.service.get(sid1), '11')
        self.service.confirm(sid1, group['id'])
        # 人工把树叶杯移出已确认组：记录排除关系，树叶杯独立待确认。
        removed = self.service.edit_group(sid1, 'remove', group['id'],
                                          {'shop_key': 'A01', 'offer_id': '33'})
        self.assertEqual({tuple(pair) for pair in removed['excluded']},
                         {(identity({'shop_key': 'A01', 'offer_id': '11'}),
                           identity({'shop_key': 'A01', 'offer_id': '33'})),
                          (identity({'shop_key': 'A01', 'offer_id': '22'}),
                           identity({'shop_key': 'A01', 'offer_id': '33'}))})
        self.assertTrue(self.group_for(removed, '11')['confirmed'])
        self.assertFalse(self.group_for(removed, '33')['confirmed'])
        self.service.save_draft(sid1)

        # 33 缺席的区间：不加载只有一端在场的排除，但保存不能删掉它们。
        second = self.service.start('2026-09-15', '2026-09-21')
        self.assertEqual(second['excluded'], [])
        self.service.save_draft(second['id'])

        # 33 回来：排除关系仍生效，且不会自动并回已确认组。
        third = self.service.start('2026-09-07', '2026-09-21')
        self.assertEqual({tuple(pair) for pair in third['excluded']},
                         {(identity({'shop_key': 'A01', 'offer_id': '11'}),
                           identity({'shop_key': 'A01', 'offer_id': '33'})),
                          (identity({'shop_key': 'A01', 'offer_id': '22'}),
                           identity({'shop_key': 'A01', 'offer_id': '33'}))})
        self.assertEqual({m['offer_id'] for m in self.group_for(third, '11')['members']}, {'11', '22'})
        self.assertFalse(self.group_for(third, '33')['confirmed'])

    def test_saving_a_single_member_range_keeps_the_whole_relation(self):
        # 已保存三人关系后，新区间只观察到一名成员：保存不能把关系拆散。
        first = self.service.start('2026-09-07', '2026-09-14')
        sid1 = first['id']
        self.join(sid1, '22', '11')
        self.join(sid1, '33', '11')
        self.service.confirm(sid1, self.group_for(self.service.get(sid1), '11')['id'])
        self.service.save_draft(sid1)

        submit_offer(self.db, '11', '2026-09-24', 45, name='月牙杯', color='red')
        second = self.service.start('2026-09-23', '2026-09-24')
        self.assertEqual([p['offer_id'] for p in second['products']], ['11'])
        self.assertTrue(self.group_for(second, '11')['confirmed'])
        self.service.save_draft(second['id'])

        third = self.service.start('2026-09-07', '2026-09-21')
        group = self.group_for(third, '11')
        self.assertEqual({m['offer_id'] for m in group['members']}, {'11', '22', '33'})
        self.assertTrue(group['confirmed'])

    def test_withdrawal_across_ranges_keeps_members_without_confirmation(self):
        first = self.service.start('2026-09-07', '2026-09-14')
        sid1 = first['id']
        self.join(sid1, '22', '11')
        self.join(sid1, '33', '11')
        self.service.confirm(sid1, self.group_for(self.service.get(sid1), '11')['id'])
        self.service.save_draft(sid1)

        # 新区间撤回并保存：成员保持一组待确认，下个区间不因账本复活成已确认。
        second = self.service.start('2026-09-07', '2026-09-14')
        self.service.withdraw(second['id'], self.group_for(second, '11')['id'])
        self.service.save_draft(second['id'])
        third = self.service.start('2026-09-07', '2026-09-14')
        group = self.group_for(third, '11')
        self.assertEqual({m['offer_id'] for m in group['members']}, {'11', '22', '33'})
        self.assertFalse(group['confirmed'])

    def test_information_change_mark_survives_restart_and_model_retry(self):
        first = self.service.start('2026-09-07', '2026-09-14')
        self.service.confirm(first['id'], self.group_for(first, '33')['id'])
        self.service.save_draft(first['id'])
        submit_offer(self.db, '33', '2026-09-14', 20, name='树叶杯', color='green')
        second = self.service.start('2026-09-07', '2026-09-14')
        self.service.save_draft(second['id'])

        restarted = self.open_service()
        restored = restarted.get(second['id'])
        leaf = next(p for p in restored['products'] if p['offer_id'] == '33')
        self.assertEqual(leaf['origin'], '信息变更')
        retried = restarted.retry_matching(second['id'])
        leaf = next(p for p in retried['products'] if p['offer_id'] == '33')
        self.assertEqual(leaf['origin'], '信息变更')

    def test_standalone_confirmation_reuses_and_withdrawal_does_not_resurrect(self):
        # 只确认树叶杯的独立分组；另两个保持待确认也不进账本。
        first = self.service.start('2026-09-07', '2026-09-14')
        self.service.confirm(first['id'], self.group_for(first, '33')['id'])
        self.service.save_draft(first['id'])

        # 新区间：独立确认自动生效；从未确认过的组仍是待确认。
        second = self.service.start('2026-09-07', '2026-09-14')
        self.assertTrue(self.group_for(second, '33')['confirmed'])
        self.assertFalse(self.group_for(second, '11')['confirmed'])
        self.assertFalse(self.group_for(second, '22')['confirmed'])

        # 撤回并保存后，再开新区间不会被账本复活成已确认。
        self.service.withdraw(second['id'], self.group_for(second, '33')['id'])
        self.service.save_draft(second['id'])
        third = self.service.start('2026-09-07', '2026-09-14')
        self.assertFalse(self.group_for(third, '33')['confirmed'])


class ModelReuseTests(unittest.TestCase):
    """模型启用时的复用：信息未变不重判，证据变了只判受影响的配对（规格 §7）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'inventory.db'
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES ('A01','店铺1')")
        self.conn.commit()
        for offer, name in [('11', '月牙杯'), ('22', '陶瓷饮具'), ('33', '月牙杯')]:
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 80)]:
                submit_offer(self.db, offer, day, stock, name=name, color='red')
        self.transport = ModelTransport()
        self.transport.decisions = {('月牙杯', '月牙杯'): True, ('月牙杯', '陶瓷饮具'): False}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=self.transport).start()
        self.service = AnalysisService(AnalysisConfig(
            self.path, matching=MatchingConfig(Path(self.tmp.name) / 'matching.sqlite', mode='direct')),
            running=lambda: False)

    def group_for(self, snapshot, offer):
        return next(g for g in snapshot['groups'] if any(m['offer_id'] == offer for m in g['members']))

    def product_for(self, snapshot, offer):
        return next(p for p in snapshot['products'] if p['offer_id'] == offer)

    def test_changed_image_rejudges_only_affected_pairs_and_keeps_others_confirmed(self):
        # 第一段：模型把两个月牙杯判成同款、陶瓷饮具独立；人工确认月牙杯组并保存。
        first = self.service.start('2026-09-07', '2026-09-14')
        group = self.group_for(first, '11')
        self.assertEqual({m['offer_id'] for m in group['members']}, {'11', '33'})
        self.service.confirm(first['id'], group['id'])
        self.service.save_draft(first['id'])
        calls = len(self.transport.calls)

        # 第二个 33 换图：只重判与它有关的配对；另一名月牙杯保持已确认。
        submit_offer(self.db, '33', '2026-09-14', 80, name='月牙杯', color='blue')
        second = self.service.start('2026-09-07', '2026-09-14')
        self.assertTrue(self.group_for(second, '11')['confirmed'])
        leaf = self.product_for(second, '33')
        self.assertFalse(self.group_for(second, '33')['confirmed'])
        self.assertEqual(leaf['origin'], '信息变更')
        # 展示待处理建议：新证据仍建议并进已确认的月牙杯组，但不自动并。
        self.assertEqual(leaf['match_label'], '匹配唯一同款')
        self.assertEqual(leaf['candidate_groups'], [self.group_for(second, '11')['id']])
        self.assertGreater(len(self.transport.calls), calls)
        # 同样的输入再开一段：判断全部命中缓存，不再调用模型。
        cached = len(self.transport.calls)
        third = self.service.start('2026-09-07', '2026-09-14')
        self.assertEqual(len(self.transport.calls), cached)
        self.assertTrue(self.group_for(third, '11')['confirmed'])
        self.assertFalse(self.group_for(third, '33')['confirmed'])
        # 人工把 33 并回并保存：下个区间两个成员一起复用确认。
        member = next(m for m in self.group_for(third, '33')['members'] if m['offer_id'] == '33')
        self.service.edit_group(third['id'], 'move', self.group_for(third, '33')['id'],
                                member, self.group_for(third, '11')['id'])
        self.service.save_draft(third['id'])
        fourth = self.service.start('2026-09-07', '2026-09-14')
        final = self.group_for(fourth, '11')
        self.assertEqual({m['offer_id'] for m in final['members']}, {'11', '33'})
        self.assertTrue(final['confirmed'])
