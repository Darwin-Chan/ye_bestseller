# -*- coding: utf-8 -*-
"""判断用量（一次运行的记账日志）：模型替身带 usage，缓存与分析服务是真的。

覆盖：收尾行把商品/对数/调用/token/缓存命中记全；每批一行进度；响应没带 usage 时
记「未提供」而不是 0；重试那趟标「重试」且命中缓存时 0 次调用。

样本注意：商品对按「版本对」记账（version = 名称 + 图片哈希），同名同图的商品版本相同、
多个对会折叠成一条判断——所以这里的商品都做成版本互不相同。
"""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_matching
from bestseller_monitor.matching import MatchingConfig, MatchingService, candidate_pairs

USAGE = {'prompt_tokens': 111, 'completion_tokens': 7,
         'prompt_cache_hit_tokens': 33, 'prompt_cache_miss_tokens': 78}


class UsageTransport(test_matching.ModelTransport):
    """在父进程替身基础上给每次响应补一段 usage（DeepSeek 的形状）。"""

    def __call__(self, request, timeout):
        response = super().__call__(request, timeout)
        body = json.loads(response.read())
        body['usage'] = dict(USAGE)
        return io.BytesIO(json.dumps(body).encode())


class BrokenBodyTransport(UsageTransport):
    """前 N 次比较的响应体坏掉：请求确实发出去了，但内容读不出来。"""

    def __init__(self, broken):
        super().__init__()
        self.broken = broken

    def __call__(self, request, timeout):
        payload = json.loads(request.data)
        if self.broken > 0 and 'five image blocks' not in payload['messages'][0]['content']:
            self.broken -= 1
            return io.BytesIO(b'not json')
        return super().__call__(request, timeout)


def distinct(number):
    """编号不同的商品：名称不同 → 版本不同 → 每个对都是独立的一条判断。"""
    return test_matching.product(number, name='月牙杯'+str(number))


class MatchingUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = MatchingConfig(Path(self.tmp.name)/'cache.sqlite', mode='direct')
        self.transport = UsageTransport()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value', 'VISION_API_KEY': 'vision-secret'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=self.transport).start()

    def run_suggest(self, products, service=None, reason=''):
        service = service or MatchingService(self.config)
        with self.assertLogs('bestseller_monitor.matching', level='INFO') as captured:
            service.suggest(products, test_matching.singles(products), reason=reason)
        return service, [record.getMessage() for record in captured.records]

    def test_summary_counts_products_pairs_calls_tokens_and_cache_hits(self):
        products = [distinct(i) for i in (1, 2, 3)]
        _, lines = self.run_suggest(products)
        summary = test_matching.usage_line(lines)
        self.assertTrue(summary.startswith('本次判断用量：'), summary)
        # 3 个商品两两都被召回（top-6 覆盖全），3 次判断 + 1 次视觉核验 = 4 次调用。
        self.assertIn('商品 3（可判 3）', summary)
        self.assertIn('召回 3 对', summary)
        self.assertIn('本机命中 0', summary)
        self.assertIn('新判 3', summary)
        self.assertIn('失败 0', summary)
        self.assertIn('调用 4 次（判断 3/描述 0/核验 1）', summary)
        self.assertIn('输入 444 输出 28 tok', summary)
        self.assertIn('供应商缓存命中 132 tok', summary)

    def test_progress_line_after_each_batch(self):
        products = [distinct(i) for i in range(40)]
        expected = len(candidate_pairs(products, 6))
        self.assertGreaterEqual(expected, 20)   # 至少凑满一批（批大小按常量 patch 成 20）
        with patch('bestseller_monitor.matching.JUDGMENT_COMMIT_BATCH', 20):
            _, lines = self.run_suggest(products)
        progress = [line for line in lines if line.startswith('判断进度：')]
        self.assertTrue(progress, '应每批落一次盘、每批打一行进度')
        self.assertTrue(progress[0].startswith('判断进度：新判 20/%d 对' % expected), progress[0])
        summary = test_matching.usage_line(lines)
        self.assertIn('新判 %d · 失败 0 对' % expected, summary)
        self.assertIn('调用 %d 次（判断 %d/描述 0/核验 1）' % (expected+1, expected), summary)

    def test_missing_usage_is_marked_not_counted_as_zero(self):
        plain = test_matching.ModelTransport()
        patch('bestseller_monitor.matching.urlopen', side_effect=plain).start()
        products = [distinct(i) for i in (1, 2, 3)]
        _, lines = self.run_suggest(products)
        summary = test_matching.usage_line(lines)
        self.assertIn('输入 - 输出 - tok', summary)
        self.assertIn('供应商缓存命中 -', summary)
        self.assertIn('未提供用量 4 次', summary)

    def test_retry_run_marks_itself_and_reuses_cache(self):
        products = [distinct(i) for i in (1, 2, 3)]
        service = MatchingService(self.config)
        service.suggest(products, test_matching.singles(products))
        _, lines = self.run_suggest(products, service=service, reason='重试')
        summary = test_matching.usage_line(lines)
        self.assertTrue(summary.startswith('本次判断用量（重试）：'), summary)
        self.assertIn('本机命中 3', summary)
        self.assertIn('新判 0', summary)
        self.assertIn('调用 0 次（判断 0/描述 0/核验 0）', summary)

    def test_unusable_response_still_counts_the_call(self):
        """响应体坏掉的那次：请求发出去了就算一次调用，用量记「未提供」，判断算失败。"""
        transport = BrokenBodyTransport(broken=1)
        patch('bestseller_monitor.matching.urlopen', side_effect=transport).start()
        products = [distinct(i) for i in (1, 2, 3)]
        _, lines = self.run_suggest(products)
        summary = test_matching.usage_line(lines)
        self.assertIn('失败 1 对', summary)
        # 3 次比较（1 次响应坏）+ 1 次核验 = 4 次调用；token 只来自 3 次可用响应。
        self.assertIn('调用 4 次（判断 3/描述 0/核验 1）', summary)
        self.assertIn('输入 333 输出 21 tok', summary)
        self.assertIn('未提供用量 1 次', summary)


if __name__ == '__main__':
    unittest.main()
