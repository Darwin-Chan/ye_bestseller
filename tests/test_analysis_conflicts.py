"""判断集跨机共享线票 07：分析页面的冲突裁决与来源展示。

服务层边界：`AnalysisService` 的 start／confirm／withdraw／edit_group／save／get／export
与草稿库账本、冲突账（`DraftStore`）。冲突经 `DraftStore.merge_incoming` 预置——那就是
收取判断集时走的同一口（收取本身的端到端用例归票 03／04）。浏览器用例在文件后半，
真页面 + 本地服务 + 临时库。

手工数字按规格手算，不照抄实现；模型场景只替换外部传输（test_matching 的 ModelTransport）。
"""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_analysis as fixture
from playwright.sync_api import expect
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect
from bestseller_monitor.matching import MatchingConfig, ModelConfig, identity, version
from helpers import submit_offer
from test_judgment_set import group, ledger_of, member
from test_matching import ModelTransport


def write_ledger(store, ledger):
    """往草稿库写一版本机账本（与保存走的同一条公开口）。"""
    store.write("draft", "2026-09-01", "2026-09-02", "2026-09-23T01:00:00+08:00",
                {"products": []}, ledger)


class ConflictViewCase(unittest.TestCase):
    """共用夹具：临时库存库（四件商品）+ 临时草稿库 + 真账本与真冲突账。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inventory.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES ('A01','店铺1')")
        self.conn.commit()
        for offer, name, color in [('11', '月牙杯', 'red'), ('22', '云朵杯', 'blue'),
                                   ('33', '树叶杯', 'green'), ('44', '贝壳杯', 'yellow')]:
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 80)]:
                submit_offer(self.db, offer, day, stock, name=name, color=color)
        self.store_path = Path(self.tmp.name) / "drafts.sqlite"
        self.service = self.open_service()

    def service_config(self):
        """这份服务的分析配置：本机编号与草稿库每个用例都要，匹配按用例另加。"""
        return {'store': self.store_path, 'machine': 'm4'}

    def open_service(self):
        """一次「分析程序启动」：同一批磁盘文件、新的内存。"""
        return AnalysisService(AnalysisConfig(self.path, **self.service_config()),
                               running=lambda: False)

    def versions(self, snapshot):
        return {identity(p): version(p) for p in snapshot['products']}

    def relation(self, snapshot, *offers, machine='m4', confirmed=True):
        versions = self.versions(snapshot)
        return {'members': [[member(offer), versions[member(offer)]] for offer in offers],
                'confirmed': confirmed, 'machine_id': machine}

    def write_ledger(self, ledger):
        write_ledger(self.service.store, ledger)

    def carry(self, *, relations=(), excluded=(), source="m1"):
        """收进来一版外来决定：不冲突即并、相对的进冲突账。"""
        return self.service.store.merge_incoming(
            ledger_of(relations=relations, excluded=excluded), source=source,
            seen_at="2026-09-23T02:00:00+08:00")

    def group_for(self, snapshot, offer):
        return next(g for g in snapshot['groups'] if any(m['offer_id'] == offer for m in g['members']))

    def product_for(self, snapshot, offer):
        return next(p for p in snapshot['products'] if p['offer_id'] == offer)


class ConflictViewTests(ConflictViewCase):
    """冲突落到页面上：落回待确认、说明与两侧来源、裁决消解、没露面的不动。"""

    def test_a_conflict_against_a_confirmed_group_falls_back_to_pending_with_both_sources(self):
        # 本机确认了树叶杯与贝壳杯是一组；m1 排除了同一对。
        base = self.service.start("2026-09-07", "2026-09-14")
        self.write_ledger(ledger_of(relations=[self.relation(base, "33", "44")]))
        carried = self.carry(excluded=[[member("33"), member("44")]])
        self.assertEqual([c.kind for c in carried.conflicts], ["group_vs_exclusion"])

        snapshot = self.service.start("2026-09-07", "2026-09-14")

        (entry,) = snapshot["conflicts"]
        self.assertEqual(entry["kind"], "group_vs_exclusion")
        self.assertEqual(set(entry["members"]), {member("33"), member("44")})
        self.assertFalse(entry["resolved"])
        self.assertEqual(
            entry["note"],
            "冲突：m1 把 树叶杯、贝壳杯 排除在外（不成组）；本机 认为 树叶杯、贝壳杯 是一组。请人工裁决。")
        leaf = self.group_for(snapshot, "33")
        self.assertTrue(leaf["confirmed"], "本机决定不被静默改动")
        self.assertEqual(leaf["conflicts"], [entry["note"]], "组上落回「待确认（有冲突）」")
        self.assertEqual(self.group_for(snapshot, "44")["conflicts"], [entry["note"]])
        # 没裁决之前保存与导出照旧拦着（落回待确认＝要人再看一遍）。
        with self.assertRaisesRegex(ValueError, "还有待确认的同款组"):
            self.service.save_and_view(snapshot["id"])
        with self.assertRaisesRegex(ValueError, "还有待确认的同款组"):
            self.service.export(snapshot["id"], "<!doctype html><html></html>")

    def test_confirming_the_group_adjudicates_and_saves(self):
        base = self.service.start("2026-09-07", "2026-09-14")
        self.write_ledger(ledger_of(relations=[self.relation(base, "33", "44")]))
        self.carry(excluded=[[member("33"), member("44")]])
        snapshot = self.service.start("2026-09-07", "2026-09-14")
        sid = snapshot["id"]

        result = self.service.confirm(sid, self.group_for(snapshot, "33")["id"])

        self.assertEqual(self.group_for(result, "33")["conflicts"], [])
        self.assertTrue(result["conflicts"][0]["resolved"])
        self.assertTrue(result["dirty"], "裁决要能随保存存下来")
        for offer in ("11", "22"):
            result = self.service.confirm(sid, self.group_for(result, offer)["id"])
        self.service.save_and_view(sid)
        (row,) = self.service.store.conflicts()
        self.assertNotEqual(row.resolved_at, "")
        # 重开：已裁决的不再落回；新分析同样不再落回，本机决定照常已确认。
        reopened = self.open_service()
        self.assertEqual(reopened.get(sid)["conflicts"], [])
        self.assertEqual(self.group_for(reopened.get(sid), "33")["conflicts"], [])
        fresh = reopened.start("2026-09-07", "2026-09-14")
        self.assertEqual(fresh["conflicts"], [])
        self.assertTrue(self.group_for(fresh, "33")["confirmed"])

    def test_an_adjudication_is_remembered_only_after_a_save(self):
        base = self.service.start("2026-09-07", "2026-09-14")
        self.write_ledger(ledger_of(relations=[self.relation(base, "33", "44")]))
        self.carry(excluded=[[member("33"), member("44")]])
        sid = self.service.start("2026-09-07", "2026-09-14")["id"]
        self.service.save_draft(sid)                     # 基准版本：裁决之前

        self.service.confirm(sid, self.group_for(self.service.get(sid), "33")["id"])
        self.assertEqual(self.service.store.conflicts()[0].resolved_at, "", "没保存就还没落账")

        self.service.discard(sid)

        back = self.service.get(sid)
        self.assertEqual(len(back["conflicts"]), 1)
        self.assertFalse(back["conflicts"][0]["resolved"])
        self.assertNotEqual(self.group_for(back, "33")["conflicts"], [])

    def test_only_the_adjudicated_conflict_dissolves(self):
        base = self.service.start("2026-09-07", "2026-09-14")
        self.write_ledger(ledger_of(excluded=[[member("11"), member("22")],
                                              [member("33"), member("44")]]))
        carried = self.carry(relations=[self.relation(base, "11", "22", machine="m1"),
                                        self.relation(base, "33", "44", machine="m1")])
        self.assertEqual(len(carried.conflicts), 2)
        snapshot = self.service.start("2026-09-07", "2026-09-14")
        self.assertEqual(len(snapshot["conflicts"]), 2)

        result = self.service.confirm(snapshot["id"], self.group_for(snapshot, "11")["id"])

        self.assertEqual(self.group_for(result, "11")["conflicts"], [])
        self.assertEqual(self.group_for(result, "22")["conflicts"], [])
        self.assertEqual(len(self.group_for(result, "33")["conflicts"]), 1, "另一条照旧")
        self.service.save_draft(snapshot["id"])
        stamped = sorted(bool(row.resolved_at) for row in self.service.store.conflicts())
        self.assertEqual(stamped, [False, True])

    def test_removing_a_member_adjudicates_and_accepts_the_other_side(self):
        # 本机确认一组；m1 排除同一对。人接受 m1 的排除：把贝壳杯移出组。
        base = self.service.start("2026-09-07", "2026-09-14")
        self.write_ledger(ledger_of(relations=[self.relation(base, "33", "44")]))
        self.carry(excluded=[[member("33"), member("44")]])
        snapshot = self.service.start("2026-09-07", "2026-09-14")

        result = self.service.edit_group(snapshot["id"], "remove",
                                         self.group_for(snapshot, "33")["id"],
                                         {'shop_key': 'A01', 'offer_id': '44'})

        self.assertTrue(result["conflicts"][0]["resolved"])
        self.assertEqual(self.group_for(result, "33")["conflicts"], [])
        self.assertEqual(self.group_for(result, "44")["conflicts"], [])
        self.assertIn([member("33"), member("44")], result["excluded"], "接受排除＝账本记下排除对")

    def test_a_withdrawn_relation_conflict_names_both_sides(self):
        # 本机撤回过月牙杯与云朵杯的关系；m1 确认了同一对。
        base = self.service.start("2026-09-07", "2026-09-14")
        self.write_ledger(ledger_of(relations=[self.relation(base, "11", "22", confirmed=False)]))
        carried = self.carry(relations=[self.relation(base, "11", "22", machine="m1")])
        self.assertEqual([c.kind for c in carried.conflicts], ["confirm_vs_withdraw"])

        snapshot = self.service.start("2026-09-07", "2026-09-14")

        (entry,) = snapshot["conflicts"]
        self.assertEqual(
            entry["note"],
            "冲突：m1 认为 月牙杯、云朵杯 是一组；本机 撤回过「月牙杯、云朵杯 是一组」。请人工裁决。")
        group_ = self.group_for(snapshot, "11")
        self.assertFalse(group_["confirmed"], "撤回过的关系照旧是待确认")
        self.assertEqual(group_["conflicts"], [entry["note"]])

    def test_conflict_notes_are_capped_for_long_groups(self):
        base = self.service.start("2026-09-07", "2026-09-14")
        self.write_ledger(ledger_of(excluded=[[member("11"), member("22")]]))
        # 四名成员的外来组：说明里只列前三名，其余按数目交代。
        self.carry(relations=[{'members': [[member(o), self.versions(base)[member(o)]]
                                           for o in ('11', '22', '33', '44')],
                               'confirmed': True, 'machine_id': 'm1'}])

        snapshot = self.service.start("2026-09-07", "2026-09-14")

        note = snapshot["conflicts"][0]["note"]
        self.assertIn("m1 认为 月牙杯、云朵杯、树叶杯 等 4 个商品 是一组", note)

    def test_conflicts_off_screen_wait_for_a_range_that_covers_them(self):
        # 本机排除了 88 与 99；m1 认为它们一组。这次区间里一个都不在场。
        self.write_ledger(ledger_of(excluded=[[member("88"), member("99")]]))
        self.carry(relations=[group("88", "99", machine="m1")])

        snapshot = self.service.start("2026-09-07", "2026-09-14")

        self.assertEqual(snapshot["conflicts"], [])
        self.service.save_draft(snapshot["id"])
        self.assertEqual([row.resolved_at for row in self.service.store.conflicts()], [""],
                         "没露过面就不算裁决过")

    def test_a_conflict_with_one_member_present_still_shows(self):
        # 本机排除了月牙杯与 88；m1 认为它们一组。88 不在本区间，月牙杯在。
        self.write_ledger(ledger_of(excluded=[[member("11"), member("88")]]))
        self.carry(relations=[group("11", "88", machine="m1")])

        snapshot = self.service.start("2026-09-07", "2026-09-14")

        (entry,) = snapshot["conflicts"]
        self.assertIn("月牙杯", entry["note"])
        self.assertIn("88", entry["note"], "缺席成员按商品号交代")
        self.assertEqual(self.group_for(snapshot, "11")["conflicts"], [entry["note"]])

    def test_a_plain_analysis_carries_no_conflict_or_source_noise(self):
        snapshot = self.service.start("2026-09-07", "2026-09-14")

        self.assertEqual(snapshot["conflicts"], [])
        self.assertTrue(all(g["conflicts"] == [] for g in snapshot["groups"]))
        self.assertTrue(all(p["matching_source"] == "" for p in snapshot["products"]))

    def test_a_carried_decision_names_the_machine_that_confirmed_it(self):
        base = self.service.start("2026-09-07", "2026-09-14")
        self.write_ledger(ledger_of(relations=[self.relation(base, "33", "44", machine="m1")],
                                    standalone=[[member('11'), self.versions(base)[member('11')],
                                                 'm2']]))
        snapshot = self.service.start("2026-09-07", "2026-09-14")

        self.assertEqual(self.group_for(snapshot, "33")["machine"], "m1")
        self.assertEqual(self.group_for(snapshot, "11")["machine"], "m2", "独立成组也带来源")

        # 本机自己确认的组：来源就是本机；页面按 settings 里的本机编号压掉它。
        self.service.confirm(snapshot["id"], self.group_for(snapshot, "22")["id"])
        self.service.save_draft(snapshot["id"])
        fresh = self.open_service().start("2026-09-07", "2026-09-14")
        self.assertEqual(self.group_for(fresh, "22")["machine"], "m4")
        self.assertEqual(self.service.settings()["machine"], "m4")


class ProvenanceTests(ConflictViewCase):
    """模型判断的来源：这条缓存来自哪台机器（本机产生的照旧记本机编号）。"""

    @property
    def cache(self):
        return Path(self.tmp.name) / 'matching.sqlite'

    def service_config(self):
        return {'matching': MatchingConfig(self.cache, ModelConfig(model='deepseek-chat'),
                                           mode='direct'),
                'store': self.store_path, 'machine': 'm4'}

    def setUp(self):
        super().setUp()
        # 召回靠共享 token：判得出的对要同名同图（同名同色＝同一份图片证据）。
        for day, stock in [('2026-09-07', 100), ('2026-09-14', 80)]:
            submit_offer(self.db, '22', day, stock, name='月牙杯', color='red')
        self.transport = ModelTransport()
        self.transport.decisions[('月牙杯', '月牙杯')] = True
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=self.transport).start()

    def test_a_cache_hit_names_the_machine_that_judged_it(self):
        # 本机判出的：来源记本机（页面上不另外标注，它是默认的那个来源）。
        first = self.service.start('2026-09-07', '2026-09-14')
        moon = self.product_for(first, '11')
        self.assertEqual(moon['matching_status'], '模型')
        self.assertEqual(moon['matching_source'], 'm4')
        calls = len(self.transport.calls)

        # 判断行改称 m1（缓存行随身带来源）：重开后再跑，命中是 m1 的。
        with closing(sqlite3.connect(self.cache)) as conn, conn:
            conn.execute("UPDATE judgments SET machine_id='m1'")
        second = self.open_service().start('2026-09-07', '2026-09-14')

        moon = self.product_for(second, '11')
        self.assertEqual(moon['matching_status'], '缓存')
        self.assertEqual(moon['matching_source'], 'm1')
        self.assertEqual(len(self.transport.calls), calls, '缓存命中不再调用模型')


class ConflictBrowserTests(unittest.TestCase):
    """票 07 浏览器核对：真实页面 + 本地服务 + 临时库。"""

    setUp = fixture.AnalysisBrowserTests.setUp
    stop_server = fixture.AnalysisBrowserTests.stop_server
    submit = fixture.AnalysisBrowserTests.submit
    submit_product = fixture.AnalysisBrowserTests.submit_product
    add_product = fixture.AnalysisBrowserTests.add_product
    dates = fixture.AnalysisBrowserTests.dates
    switch_tab = fixture.AnalysisBrowserTests.switch_tab
    back_to_review = fixture.AnalysisBrowserTests.back_to_review

    def seed_group(self, *, machine, offers=('11', '22')):
        """本机账本里确认一组（来源机器由用例给）。"""
        base = self.service.start('2026-09-07', '2026-09-14')
        versions = {identity(p): version(p) for p in base['products']}
        write_ledger(self.service.store, ledger_of(
            relations=[{'members': [[member(o), versions[member(o)]] for o in offers],
                        'confirmed': True, 'machine_id': machine}]))

    def test_conflicted_group_falls_back_to_pending_and_adjudication_clears_it(self):
        self.add_product('22', '云朵杯', 90, 60, color='blue')
        self.seed_group(machine='')
        self.service.store.merge_incoming(ledger_of(excluded=[[member('11'), member('22')]]),
                                          source='m1', seen_at='2026-09-23T02:00:00+08:00')
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()

        # 落回待确认：页签筛选与计数都算它。
        conflicted = self.page.locator('.group-choice', has_text='状态：待确认（有冲突）')
        expect(conflicted).to_have_count(1)
        expect(self.page.locator('#snapshotInfo .snapshot-tally')).to_contain_text('其中 1 组待确认')
        # 搜索照常：命中的还在，没命中的筛掉。
        self.page.locator('#reviewQuery').fill('云朵杯')
        expect(self.page.locator('.group-choice')).to_have_count(1)
        self.page.locator('#reviewQuery').fill('不存在的东西')
        expect(self.page.locator('.group-choice')).to_have_count(0)
        self.page.locator('#reviewQuery').fill('')

        # 组上有说明与两侧来源；落回的组两个动词都在手边（确认＝按本机决定办、撤回＝不算一组）。
        conflicted.click()
        conflict = self.page.locator('#groupDetail .conflict')
        expect(conflict).to_contain_text('m1 把 杯子、云朵杯 排除在外（不成组）')
        expect(conflict).to_contain_text('本机 认为 杯子、云朵杯 是一组')
        expect(self.page.get_by_role('button', name='确认当前分组', exact=True)).to_be_visible()
        expect(self.page.get_by_role('button', name='撤回当前分组', exact=True)).to_be_visible()
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        expect(self.page.locator('.group-choice', has_text='有冲突')).to_have_count(0)
        self.switch_tab('已确认')
        self.page.locator('.group-choice').first.click()
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认')
        expect(self.page.locator('#groupDetail .conflict')).to_have_count(0)

        # 裁决后照常可保存进结果页。
        gate = self.page.get_by_role('button', name='保存分组并查看畅销品')
        expect(gate).to_be_enabled()
        gate.click()
        expect(self.page.locator('#results')).to_be_visible()
        sid = self.page.url.split('analysis=')[1]
        self.assertNotEqual(self.service.store.conflicts()[0].resolved_at, '')
        self.assertEqual(self.service.start('2026-09-07', '2026-09-14')['conflicts'], [],
                         '裁决后的视图不再落回：冲突行留着作历史')

    def test_withdrawing_a_fallen_back_group_adjudicates_in_one_step(self):
        # 想「这不算一组」的人：撤回一步到位，不必先确认再撤回（票 07 裁决入口）。
        self.add_product('22', '云朵杯', 90, 60, color='blue')
        self.seed_group(machine='')
        self.service.store.merge_incoming(ledger_of(excluded=[[member('11'), member('22')]]),
                                          source='m1', seen_at='2026-09-23T02:00:00+08:00')
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()

        self.page.locator('.group-choice', has_text='待确认（有冲突）').click()
        self.page.get_by_role('button', name='撤回当前分组', exact=True).click()

        expect(self.page.locator('#groupDetail .conflict')).to_have_count(0)
        expect(self.page.locator('.group-choice', has_text='有冲突')).to_have_count(0)
        sid = self.page.url.split('analysis=')[1]
        snapshot = self.service.get(sid)
        self.assertTrue(snapshot['conflicts'][0]['resolved'])
        group = next(g for g in snapshot['groups'] if any(m['offer_id'] == '11' for m in g['members']))
        self.assertFalse(group['confirmed'], '撤回就是把确认拿掉')
        self.service.save_draft(sid)
        self.assertNotEqual(self.service.store.conflicts()[0].resolved_at, '')

    def test_a_carried_group_names_the_machine_that_confirmed_it(self):
        self.add_product('22', '云朵杯', 90, 60, color='blue')
        self.add_product('33', '树叶杯', 70, 40, color='green')
        self.add_product('44', '贝壳杯', 60, 30, color='yellow')
        base = self.service.start('2026-09-07', '2026-09-14')
        versions = {identity(p): version(p) for p in base['products']}
        write_ledger(self.service.store, ledger_of(
            relations=[{'members': [[member(o), versions[member(o)]] for o in ('11', '22')],
                        'confirmed': True, 'machine_id': 'm1'},
                       {'members': [[member(o), versions[member(o)]] for o in ('33', '44')],
                        'confirmed': True, 'machine_id': ''}]))
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()

        # 账本里两组都带确认、快照不脏：落屏门禁直接给结果屏（票 06）；要看的标注在确认屏。
        expect(self.page.get_by_role('heading', name='畅销品', exact=True)).to_be_visible()
        self.back_to_review()
        self.switch_tab('已确认')
        self.page.locator('.group-choice').filter(has_text='云朵杯 ·').first.click()
        expect(self.page.locator('#groupDetail .group-status')).to_contain_text('由 m1 确认')
        self.page.locator('.group-choice').filter(has_text='树叶杯 ·').first.click()
        expect(self.page.locator('#groupDetail .group-status')).to_have_text('已确认')

    def test_a_cache_hit_from_another_machine_is_tagged(self):
        # 同名同图的一对才会进判断；两段：本机判的没标注，改称 m1 后标注出来。
        for offer in ('11', '22'):
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 80 if offer == '22' else 90)]:
                self.submit_product(offer, day, stock, name='月牙杯', color='red')
        cache = Path(self.tmp.name) / 'matching.sqlite'
        self.stop_server()
        self.service = AnalysisService(AnalysisConfig(
            self.path, matching=MatchingConfig(cache, ModelConfig(model='deepseek-chat'),
                                              mode='direct'), machine='m4'),
            running=lambda: self.running)
        self.server, self.thread = fixture.serve(self.service)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=ModelTransport()).start()

        first = self.service.start('2026-09-07', '2026-09-14')
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#analysis={first['id']}")
        card = self.page.locator('#groupDetail [data-product="11"]')
        expect(card).to_contain_text('模型')
        self.assertNotIn('来自', card.inner_text(), '本机判的不另标注')

        with closing(sqlite3.connect(cache)) as conn, conn:
            conn.execute("UPDATE judgments SET machine_id='m1'")
        second = self.service.start('2026-09-07', '2026-09-14')
        # 同一地址只换 hash 不触发整页导航：显式重载，让页面按新分析重取。
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#analysis={second['id']}")
        self.page.reload()

        expect(self.page.locator('#groupDetail [data-product="11"]')).to_contain_text('缓存 · 来自 m1')

    def test_a_plain_page_shows_nothing_new(self):
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()

        detail = self.page.locator('#groupDetail')
        expect(detail.locator('.group-status')).to_have_text('待确认')
        self.assertEqual(detail.locator('.conflict').count(), 0)
        text = detail.inner_text()
        self.assertNotIn('来自', text)
        self.assertNotIn('由', text, '本机（或没有来源）的组不带来源标注')
        self.assertNotIn('有冲突', self.page.locator('#products').inner_text())
        expect(self.page.get_by_role('button', name='保存分组并查看畅销品')).to_be_disabled()


if __name__ == "__main__":
    unittest.main()
