"""票 01（看得见）：异步受理、判断进度、硬失败如实报因——服务缝。

只用分析服务本身，不开浏览器：受理立刻返回、读数能走到终态、读数不被服务锁堵住、
硬失败如实报因、预计时长按本次速率给（样本不足不给）。
浏览器缝（遮罩、轮询、刷新接回）在 test_analysis_progress_ui.py。
"""
import json
import os
import re
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import test_matching
from bestseller_monitor.analysis import PHASES, AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect
from bestseller_monitor.matching import MatchingConfig
from helpers import submit_offer
from test_matching import ModelTransport


class StagedTransport(ModelTransport):
    """前两次对级判断放行、之后卡住：用例能在"判断进行中"这一稳定态读进度。

    `concurrency=2`：前两次跑完（已判断 2 对），第三次起卡在闸门上不再前进——
    读数因此是可断言的，不用和线程赛跑。
    """

    def __init__(self, pass_first=2):
        super().__init__()
        self._pass_first = pass_first
        self._seen = 0
        self._lock = threading.Lock()
        self.release = threading.Event()
        self.blocked = threading.Event()

    def __call__(self, request, timeout):
        payload = json.loads(request.data)
        if 'Compare the same physical product style' in payload['messages'][0]['content']:
            with self._lock:
                mine = self._seen
                self._seen += 1
            if mine >= self._pass_first:
                self.blocked.set()
                if not self.release.wait(10):
                    raise AssertionError('判断一直卡在闸门上（用例没放行）')
        return super().__call__(request, timeout)


class FakeClock:
    """可控时钟：预计时长按「判断阶段跑了多久」算，用真钟就测不了。"""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ProgressSeamTests(unittest.TestCase):
    """服务缝：一次真实运行（假模型、真服务、真临时库）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'inventory.db'
        conn = connect(self.path)
        self.addCleanup(conn.close)
        self.db = Database(conn)
        # 四件同图同款的商品：召回 6 对，全部高于判断下限，冷却缓存下 todo=6。
        for index in range(1, 5):
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 100 - index)]:
                submit_offer(self.db, str(index * 11), day, stock,
                             shop_key=f'A{index:02}', shop_name=f'店铺{index}',
                             name=f'杯子{index}', color='red')
        self.transport = StagedTransport()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=self.transport).start()
        self.service = AnalysisService(
            AnalysisConfig(self.path, matching=MatchingConfig(Path(self.tmp.name) / 'match.sqlite',
                                                              mode='direct')),
            running=lambda: False)
        self.jobs = []
        # 最后登记、最先跑：用例失败在半路时也要把闸门放行并等运行收尾，
        # 否则后台线程攥着临时库不放，临时目录删不掉。
        self.addCleanup(self.drain_jobs)

    def drain_jobs(self):
        self.transport.release.set()
        for analysis_id in self.jobs:
            self.wait_state(analysis_id, timeout=10)

    def start(self, start='2026-09-07', end='2026-09-14', acknowledged=False):
        accepted = self.service.start_job(start, end, acknowledged)
        if 'id' in accepted:
            self.jobs.append(accepted['id'])
        return accepted

    def wait_state(self, analysis_id, states=('ready', 'failed'), timeout=15):
        return self.wait_for(analysis_id, lambda reading: reading['state'] in states, timeout)

    def wait_for(self, analysis_id, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reading = self.service.job_state(analysis_id)
            if predicate(reading):
                return reading
            time.sleep(0.01)
        self.fail(f'等不到想要的读数：{self.service.job_state(analysis_id)}')

    def test_start_job_accepts_immediately_and_progress_walks_to_ready(self):
        accepted = self.start()
        self.assertEqual(accepted['state'], 'matching')
        self.assertTrue(accepted['id'])

        # 前两对放行、其余卡在闸门上：这一刻的读数是稳定的（已判 2 对、共 6 对）。
        reading = self.wait_for(accepted['id'], lambda r: r['judged'] >= 2)
        self.assertEqual(reading['state'], 'matching')
        self.assertEqual(reading['phases'], list(PHASES))
        self.assertEqual(reading['phase_index'], 3, '卡在判断中时阶段应停在「逐对判断同款」')
        self.assertEqual(reading['products'], 4)
        self.assertEqual(reading['eligible'], 4)
        self.assertEqual(reading['todo'], 6)
        self.assertEqual(reading['judged'], 2)
        self.assertGreaterEqual(reading['elapsed_sec'], 0)
        self.assertEqual(reading['error'], '')

        self.transport.release.set()
        final = self.wait_state(accepted['id'])
        self.assertEqual(final['state'], 'ready')
        self.assertEqual(final['phase_index'], 4)
        self.assertEqual(final['judged'], final['todo'])
        snapshot = self.service.get(accepted['id'])
        self.assertEqual(snapshot['id'], accepted['id'], '快照编号就是受理时给分析号')
        self.assertEqual(len(snapshot['groups']), 1, '四件同图商品装配成一组')

    def test_progress_reads_do_not_wait_for_the_service_lock(self):
        """匹配期间服务锁被判断占着；进度读数必须绕开它，否则页面轮询会被堵死。"""
        accepted = self.start()
        self.wait_for(accepted['id'], lambda r: r['judged'] >= 2)
        with ThreadPoolExecutor(1) as pool:
            reading = pool.submit(self.service.job_state, accepted['id']).result(timeout=5)
        self.assertEqual(reading['state'], 'matching')
        self.transport.release.set()
        self.wait_state(accepted['id'])

    def test_failed_job_reports_the_reason_and_date_order_stays_synchronous(self):
        with self.assertRaises(ValueError):
            self.service.start_job('2026-09-14', '2026-09-07')
        accepted = self.start('2026-10-01', '2026-10-02')
        final = self.wait_state(accepted['id'], states=('failed',))
        self.assertEqual(final['error'], '该日期区间没有可分析的库存，请重新选择日期')

    def test_cache_failure_reports_the_store_not_the_inventory(self):
        """判断缓存打不开：说缓存那句，不把锅甩给库存库（规格点名的场景）。"""
        blocked = Path(self.tmp.name) / 'cache-as-directory'
        blocked.mkdir()
        self.service = AnalysisService(
            AnalysisConfig(self.path, matching=MatchingConfig(blocked, mode='direct')),
            running=lambda: False)
        accepted = self.start()
        final = self.wait_state(accepted['id'], states=('failed',))
        self.assertEqual(final['error'], '判断缓存或分析草稿读写失败，请检查分析配置与磁盘后重试')

    def test_eta_waits_for_enough_samples_then_follows_the_measured_rate(self):
        """预计时长：头一分钟（样本不足）不给；给出来时按本次实测速率算。"""
        clock = FakeClock()
        self.service = AnalysisService(self.service.config, running=lambda: False, clock=clock)
        accepted = self.start()
        self.wait_for(accepted['id'], lambda r: r['judged'] >= 2)
        self.assertEqual(self.service.job_state(accepted['id'])['eta_text'], '',
                         '刚判完两对就不该给预计')
        clock.advance(30)
        self.assertEqual(self.service.job_state(accepted['id'])['eta_text'], '',
                         '跑满半分钟仍算样本不足')
        # 判断跑了 60 秒、判完 2 对、还剩 4 对：4 × 60/2 = 120 秒 → 2 分钟。
        clock.advance(30)
        self.assertEqual(self.service.job_state(accepted['id'])['eta_text'], '2 分钟')
        self.transport.release.set()
        self.wait_state(accepted['id'])

    def test_final_counts_match_the_usage_line(self):
        with self.assertLogs('bestseller_monitor.matching', level='INFO') as logs:
            accepted = self.start()
            self.transport.release.set()
            final = self.wait_state(accepted['id'])
        line = test_matching.usage_line([record.getMessage() for record in logs.records])
        self.assertEqual(final['cached_hits'], int(re.search(r'本机命中 (\d+)', line).group(1)))
        self.assertEqual(final['judged'], int(re.search(r'新判 (\d+)', line).group(1)))
        self.assertEqual(final['failed'], int(re.search(r'失败 (\d+) 对', line).group(1)))
        self.assertEqual(final['todo'], final['judged'], '冷缓存：本次要判的正是新判的那些')
        self.assertEqual(final['blocked'], 0, '这四件商品的对都高于判断下限')


if __name__ == '__main__':
    unittest.main()
