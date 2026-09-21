"""分析草稿的保存与恢复：真实服务边界 + 临时库存库，重启不依赖旧进程内存。

浏览器流程验收在 test_analysis.py；这里覆盖保存的往返、原子性与草稿版本兼容。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect
from helpers import submit_offer


class DraftPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inventory.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.conn.execute("INSERT INTO shops(shop_key,shop_name) VALUES ('A01','店铺1')")
        self.conn.commit()
        self.submit('11', '2026-09-07', 100, name='月牙杯')
        self.submit('11', '2026-09-14', 80, name='月牙杯')
        self.submit('22', '2026-09-07', 50, name='云朵杯')
        self.submit('22', '2026-09-14', 40, name='云朵杯')
        self.service = self.open_service()

    def open_service(self):
        """每次调用都代表一次重新启动：新进程、新内存、同一份磁盘文件。"""
        return AnalysisService(AnalysisConfig(self.path), running=lambda: False)

    def submit(self, offer, day, stock, *, name, color='red'):
        submit_offer(self.db, offer, day, stock, name=name, color=color)

    def test_saved_draft_restores_frozen_snapshot_and_progress_after_restart(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        self.service.confirm(sid, 'G1')
        saved = self.service.save_draft(sid)
        self.assertFalse(saved['dirty'])
        self.assertTrue(saved['saved_at'])

        restarted = self.open_service()
        restored = restarted.get(sid)
        self.assertEqual(restored['start'], '2026-09-07')
        self.assertEqual(restored['end'], '2026-09-14')
        self.assertEqual(restored['frozen_at'], snapshot['frozen_at'])
        self.assertEqual(restored['inventory'], snapshot['inventory'])
        self.assertEqual(restored['groups'], saved['groups'])
        self.assertEqual(restored['products'][0]['image_data'], snapshot['products'][0]['image_data'])
        self.assertFalse(restored['dirty'])
        self.assertEqual(restored['saved_at'], saved['saved_at'])
        self.assertEqual(restarted.draft(), {'available': True, 'id': sid,
            'start': '2026-09-07', 'end': '2026-09-14', 'saved_at': saved['saved_at']})
        # 落盘的完整版本本身不带未保存标记，保存时间与版本行一致。
        conn = sqlite3.connect(self.service.config.store)
        payload, stored_at = conn.execute(
            'SELECT payload, saved_at FROM analysis_drafts WHERE id=?', (sid,)).fetchone()
        conn.close()
        self.assertEqual(json.loads(payload)['saved_at'], stored_at)
        self.assertFalse(json.loads(payload)['dirty'])

    def test_file_configuration_saves_the_draft_next_to_it(self):
        config = Path(self.tmp.name) / "analysis.toml"
        config.write_text('[analysis]\ndatabase = "inventory.db"\n', encoding="utf-8")
        service = AnalysisService(AnalysisConfig.from_file(config), running=lambda: False)
        snapshot = service.start('2026-09-07', '2026-09-14')
        service.save_draft(snapshot['id'])
        self.assertTrue((Path(self.tmp.name) / 'analysis-drafts.sqlite').exists())
        # 生产入口（从配置文件构造）重启后也能打开同一次分析。
        restarted = AnalysisService(AnalysisConfig.from_file(config), running=lambda: False)
        self.assertEqual(restarted.get(snapshot['id'])['groups'], service.get(snapshot['id'])['groups'])

    def test_only_the_two_save_actions_persist_the_draft(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        self.service.confirm(sid, 'G1')
        self.service.retry_matching(sid)  # 模型缓存的自动落盘不算保存草稿
        restarted = self.open_service()
        self.assertEqual(restarted.draft(), {'available': False})
        with self.assertRaisesRegex(ValueError, '分析已不存在'):
            restarted.get(sid)

    def test_failed_save_keeps_previous_version_and_unsaved_edits(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        self.service.confirm(sid, 'G1')
        first = self.service.save_draft(sid)
        self.service.confirm(sid, 'G2')

        # 注入中途失败：另一个连接占着写锁，这次保存无法提交。
        locker = sqlite3.connect(self.service.config.store)
        self.addCleanup(locker.close)
        locker.execute('BEGIN IMMEDIATE')
        locker.execute("UPDATE analysis_drafts SET saved_at=saved_at WHERE id=?", (sid,))
        real_connect = sqlite3.connect
        with patch('bestseller_monitor.analysis_store.sqlite3.connect',
                   side_effect=lambda path: real_connect(path, timeout=0)):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.save_draft(sid)

        # 上一个成功版本仍可恢复，失败的那次没有写进任何一半。
        restored = self.open_service().get(sid)
        self.assertEqual(restored['groups'], first['groups'])
        self.assertEqual(restored['saved_at'], first['saved_at'])
        # 本次修改仍留在页面草稿里，并保持未保存标记。
        draft = self.service.get(sid)
        self.assertTrue(draft['dirty'])
        self.assertTrue(next(g for g in draft['groups'] if g['id'] == 'G2')['confirmed'])
        self.assertEqual(draft['saved_at'], first['saved_at'])

        # 解除占用后可以重试，重试成功后新版本才生效。
        locker.rollback()
        retried = self.service.save_draft(sid)
        self.assertFalse(retried['dirty'])
        self.assertEqual(self.open_service().get(sid)['groups'], retried['groups'])

    def test_save_and_view_requires_every_group_confirmed(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        with self.assertRaisesRegex(ValueError, '待确认'):
            self.service.save_and_view(sid)
        for group in ('G1', 'G2'):
            self.service.confirm(sid, group)
        viewed = self.service.save_and_view(sid)
        self.assertFalse(viewed['dirty'])
        self.assertEqual(self.open_service().get(sid), viewed)

    def test_discard_returns_to_the_last_saved_version(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        self.service.confirm(sid, 'G1')
        saved = self.service.save_draft(sid)
        self.service.confirm(sid, 'G2')

        self.assertEqual(self.service.discard(sid), {'reverted': True})
        self.assertEqual(self.service.get(sid), saved)

    def test_resume_keeps_the_saved_snapshot_after_source_changes(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        self.service.save_draft(sid)
        # 保存之后源库变了：补了一笔库存，名称与图片在结束日重报了一版。
        self.submit('11', '2026-09-12', 5, name='改名后的杯子', color='blue')
        self.submit('11', '2026-09-14', 70, name='改名后的杯子', color='blue')

        restarted = self.open_service()
        restored = restarted.get(sid)
        self.assertEqual(restored['start'], '2026-09-07')
        self.assertEqual(restored['end'], '2026-09-14')
        self.assertEqual(restored['inventory'], snapshot['inventory'])
        old = next(p for p in restored['products'] if p['offer_id'] == '11')
        self.assertEqual(old['product_name'], '月牙杯')
        self.assertEqual(old['sales'], snapshot['products'][0]['sales'])
        self.assertEqual(old['image_data'], next(p for p in snapshot['products'] if p['offer_id'] == '11')['image_data'])
        # 明确重新开始分析时才读到新源数据。
        fresh = restarted.start('2026-09-07', '2026-09-14')
        new = next(p for p in fresh['products'] if p['offer_id'] == '11')
        self.assertEqual(new['product_name'], '改名后的杯子')
        self.assertNotEqual(new['image_data'], old['image_data'])
        self.assertNotEqual(new['sales'], old['sales'])

    def test_unknown_draft_version_stays_visible_and_is_not_cleared(self):
        snapshot = self.service.start('2026-09-07', '2026-09-14')
        sid = snapshot['id']
        self.service.save_draft(sid)
        conn = sqlite3.connect(self.service.config.store)
        conn.execute('UPDATE analysis_drafts SET schema_version=99')
        conn.commit()
        conn.close()

        restarted = self.open_service()
        self.assertTrue(restarted.draft()['available'])
        with self.assertRaisesRegex(ValueError, '不兼容'):
            restarted.get(sid)
        conn = sqlite3.connect(self.service.config.store)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM analysis_drafts').fetchone()[0], 1)
        conn.close()
