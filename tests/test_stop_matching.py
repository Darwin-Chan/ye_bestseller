"""票 02（停得下）：协作停止、停止后的落态与终态契约——服务缝。

只用分析服务本身，不开浏览器：停止请求把运行推到终态、未判的对记成可重试的失败、
已判的留在判断缓存里、重试只补没判过的；`wait_terminal` 返回后不再有任何写入。
浏览器缝（遮罩上的停止按钮、重试入口、停止后的状态行）在 test_stop_matching_ui.py。
"""
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_analysis_progress as seam_fixture
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect
from bestseller_monitor.matching import (STATUS_STOPPED, MatchingConfig, assemble_groups)
from helpers import submit_offer
from test_analysis_progress import StagedTransport


def cache_digest(path):
    """判断缓存里看得见的一切：表清单、行数、内容——只比字节更稳（WAL 不影响它）。"""
    with closing(sqlite3.connect(path)) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return '\n'.join(f'{table}:{conn.execute(f"""SELECT * FROM "{table}" ORDER BY 1""").fetchall()}'
                         for table in tables)


class StopSeamTests(unittest.TestCase):
    """服务缝：一次真实运行（假模型、真服务、真临时库）。"""

    # 受理与等待的读数辅助与票 01 那套同形：按名别名复用，不复制实现。
    start = seam_fixture.ProgressSeamTests.start
    wait_for = seam_fixture.ProgressSeamTests.wait_for

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'inventory.db'
        self.cache = Path(self.tmp.name) / 'match.sqlite'
        conn = connect(self.path)
        self.addCleanup(conn.close)
        self.db = Database(conn)
        # 四件同图同款的商品：召回 6 对，全部高于判断下限，冷却缓存下 todo=6。
        for index in range(1, 5):
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 100 - index)]:
                submit_offer(self.db, str(index * 11), day, stock,
                             shop_key=f'A{index:02}', shop_name=f'店铺{index}',
                             name=f'杯子{index}', color='red')
        self.transport = self.use_transport()
        self.service = AnalysisService(
            AnalysisConfig(self.path, matching=MatchingConfig(self.cache, mode='direct')),
            running=lambda: False)
        self.jobs = []
        # 最后登记、最先跑：用例失败在半路时也要把闸门放行并等运行收尾，
        # 否则后台线程攥着临时库不放，临时目录删不掉。
        self.addCleanup(self.drain_jobs)

    def use_transport(self, pass_first=2):
        """换一副假传输：前 `pass_first` 次对级判断放行，其余卡在闸门上等用例放行。"""
        gate = StagedTransport(pass_first)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=gate).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(gate.release.set)
        return gate

    def drain_jobs(self):
        self.transport.release.set()
        for analysis_id in self.jobs:
            self.wait_for(analysis_id, lambda reading: reading['state'] in ('ready', 'failed'),
                          timeout=10)

    def wait_state(self, analysis_id, states=('ready',), timeout=15):
        """等终态。缺省只认「跑完」——票 02 的用例要能一眼看出被停止的运行有没有落成失败。"""
        return self.wait_for(analysis_id, lambda reading: reading['state'] in states, timeout)

    def count_rows(self, table):
        with closing(sqlite3.connect(self.cache)) as conn:
            return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def judging_stopped(self):
        """跑到「已判两对、其余卡在闸门上」，然后点停止——票 02 的公共前置。"""
        accepted = self.start()
        reading = self.wait_for(accepted['id'], lambda r: r['judged'] >= 2)
        self.assertEqual(reading['state'], 'matching')
        return accepted['id']

    def test_stop_records_unjudged_pairs_as_retryable_failures(self):
        analysis_id = self.judging_stopped()

        stopping = self.service.request_stop(analysis_id)
        self.assertEqual(stopping['state'], 'stopping', '停止请求立刻转「正在停止」态')
        self.assertTrue(stopping['stop_requested'])
        self.assertEqual(stopping['judged'], 2, '在途的两对还在等它自己的超时')
        self.assertEqual(stopping['stop_timeout_sec'], 30, '页面上的「最多 N 秒」照模型超时说')

        self.transport.release.set()
        final = self.wait_state(analysis_id)
        # 在途的两对跑完了（已判的会留下），没轮到的两对记成可重试的失败。
        self.assertEqual(final['state'], 'ready')
        self.assertEqual((final['judged'], final['todo'], final['failed']), (4, 6, 2))
        self.assertTrue(final['stop_requested'])
        self.assertEqual(self.count_rows('judgments'), 4, '已判的对一对不多、一对不少落在缓存里')

        snapshot = self.service.get(analysis_id)
        summary = snapshot['matching']
        self.assertEqual(summary['retryable'], summary['failed'])
        self.assertIn(STATUS_STOPPED, summary['reasons'], '未判的对说的是「已停止匹配，未判断」')
        self.assertEqual(summary['judged'] + summary['failed'], 4, '四个商品的结论都定稿了')
        self.assertEqual(len(snapshot['groups']), 1, '装配照常跑完：四件同图商品仍是一组')
        self.assertEqual(snapshot['matching']['failed'], summary['failed'])

    def test_retry_job_judges_only_the_pairs_that_were_left(self):
        analysis_id = self.judging_stopped()
        self.service.request_stop(analysis_id)
        self.transport.release.set()
        self.wait_state(analysis_id)

        gate = self.use_transport(pass_first=0)      # 重试的第一对卡住：读数才停得下来
        accepted = self.service.retry_matching_job(analysis_id)
        self.assertEqual(accepted, {'id': analysis_id, 'state': 'matching'})
        self.assertGreaterEqual(self.service.job_state(analysis_id)['phase_index'], 1,
                                '重试不冻结库存：阶段从「召回候选同款」起')
        reading = self.wait_for(analysis_id, lambda r: r['todo'] == 2)
        self.assertTrue(reading['retry'], '读数认得出这是重试那趟（页面据此说「（重试）」）')
        self.assertEqual(reading['judged'], 0)
        self.assertEqual(reading['cached_hits'], 4, '已判的命中缓存，不重复花钱')

        gate.release.set()
        final = self.wait_state(analysis_id)
        self.assertEqual((final['judged'], final['failed']), (2, 0))
        self.assertEqual(self.count_rows('judgments'), 6, '重试把没判的两对补上：缓存里六对齐全')
        summary = self.service.get(analysis_id)['matching']
        self.assertEqual((summary['failed'], summary['reasons']), (0, []))
        self.assertEqual(summary['judged'], 4)

    def test_wait_terminal_returns_with_nothing_left_to_write(self):
        analysis_id = self.judging_stopped()
        self.service.request_stop(analysis_id)
        self.transport.release.set()

        final = self.service.wait_terminal(analysis_id, timeout=15)
        self.assertEqual(final['state'], 'ready')
        settled = cache_digest(self.cache)
        time.sleep(1.5)                 # 静置：还有写的话，这里就该变（票 03 只依赖这条契约）
        self.assertEqual(cache_digest(self.cache), settled, 'wait_terminal 返回后判断缓存还在被写')
        self.assertEqual(self.count_rows('recommendations'), 1,
                         '推荐快照（装配的产物）也在 wait_terminal 之前落库')

    def test_stop_during_assembly_leaves_this_runs_result_intact(self):
        self.transport.release.set()    # 判断全放行：这一次只卡装配
        gate = threading.Event()

        def gated(*args, **kwargs):
            gate.wait(10)
            return assemble_groups(*args, **kwargs)

        with patch('bestseller_monitor.matching.assemble_groups', side_effect=gated):
            analysis_id = self.start()['id']
            self.wait_for(analysis_id, lambda r: r['phase_index'] == 4)
            self.assertTrue(self.service.request_stop(analysis_id)['stop_requested'])
            gate.set()
            final = self.wait_state(analysis_id)

        self.assertEqual((final['judged'], final['failed']), (6, 0), '装配那一段不被打断')
        snapshot = self.service.get(analysis_id)
        self.assertEqual(len(snapshot['groups']), 1)
        self.assertEqual(snapshot['matching']['reasons'], [])

    def test_stop_request_after_the_run_finished_is_ignored(self):
        self.transport.release.set()
        analysis_id = self.start()['id']
        self.wait_state(analysis_id)

        self.assertFalse(self.service.request_stop(analysis_id)['stop_requested'],
                         '已经收尾的运行不再受理停止：它没有还剩的写入了')
        self.assertEqual(self.service.job_state(analysis_id)['state'], 'ready')

    def test_second_retry_is_refused_while_one_is_still_running(self):
        analysis_id = self.judging_stopped()
        self.service.request_stop(analysis_id)
        self.transport.release.set()
        self.wait_state(analysis_id)

        gate = self.use_transport(pass_first=0)         # 重试卡在闸门上，一直在跑
        self.service.retry_matching_job(analysis_id)
        with self.assertRaises(ValueError):
            self.service.retry_matching_job(analysis_id)
        gate.release.set()
        self.wait_state(analysis_id)


if __name__ == '__main__':
    unittest.main()
