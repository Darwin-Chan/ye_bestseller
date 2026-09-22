"""票 01：判断缓存分批提交——分析跑到一半被结束，已提交的判断留在缓存里。

判断阶段按小批落盘（判据 A10）：被结束（关窗、任务管理器结束进程）时**已提交的那些批**
留在库里，重开只补未判的——已提交的商品对零模型调用。判断行与它的证据索引行同批写入，
不留「有判断、证据索引缺行」的半截状态。

两个接缝：
- 服务层：`MatchingService.suggest`，真库、外部模型传输替换（test_matching 的 ModelTransport）；
  传输在第 N 次同款比较时抛出逃逸异常——与硬结束进程的事务结局等价：未提交的那批回滚。
- 进程边界：子进程跑真的 `suggest`，`os._exit` 硬结束（不跑 finally、不回滚，未提交事务靠
  SQLite 的 journal 恢复丢掉），与 2026-09-22 实测的「整批跑完、未提交、整体回滚」同形。
"""
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bestseller_monitor.matching import JUDGMENT_COMMIT_BATCH, MatchingConfig, MatchingService, version

from helpers import product_picture
from test_matching import ModelTransport, singles

ROOT = Path(__file__).resolve().parents[1]

COMPARE = 'Compare the same physical product style'

# 子进程：吃父进程写好的商品清单，跑真的 suggest；第 kill_after 次同款比较之后再来的请求
# 直接 os._exit——等价于关窗／任务管理器结束进程。
PROBE = r'''
import base64, io, json, os, sys, time
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from bestseller_monitor.matching import MatchingConfig, MatchingService

products = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
groups = [{"id": "G%d" % i, "confirmed": False, "sales": p["sales"],
           "members": [{"shop_key": p["shop_key"], "offer_id": p["offer_id"]}]} for i, p in enumerate(products)]
kill_after, served = int(sys.argv[3]), [0]


def transport(request, timeout):
    # 每次比较都睡一下：替身比真模型快三个数量级，不压一压，主线程的批提交会被甩在后面，
    # 硬结束就可能正好落在一次提交中间（那一批按提交点语义整批丢，测不到「已提交的批在」）。
    time.sleep(0.01)
    payload = json.loads(request.data)
    instruction = payload["messages"][0]["content"]
    content = payload["messages"][1]["content"]
    images = [Image.open(io.BytesIO(base64.b64decode(part["image_url"]["url"].split(",", 1)[1])))
              for part in content if part["type"] == "image_url"]
    if "five image blocks" in instruction:
        colors = {(255, 0, 0): "red", (0, 255, 0): "green", (0, 0, 255): "blue"}
        result = {"colors": [colors[images[0].getpixel((i * 50 + 25, 25))] for i in range(5)]}
    else:
        served[0] += 1
        if served[0] > kill_after:
            os._exit(1)     # 硬结束：不跑 finally、不回滚，与关窗同形
        visual = [str(image.getpixel((0, 0))) for image in images]
        result = {"same": visual[0] == visual[1], "confidence": 0.99}
    for image in images:
        image.close()
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode())


with patch("bestseller_monitor.matching.urlopen", side_effect=transport):
    MatchingService(MatchingConfig(Path(sys.argv[1]), mode="direct")).suggest(products, groups)
'''


class InterruptedTransport(ModelTransport):
    """第 limit 次同款比较之后再来的调用抛逃逸异常：判断阶段被打断，未提交的批随连接回滚。

    每次比较睡一下跟真模型的量级看齐（理由同 PROBE）：替身太快时主线程还在提交上一批，
    打断点就不是「批与批之间」了。
    """

    def __init__(self, limit):
        super().__init__()
        self.limit = limit
        self.served = 0

    def __call__(self, request, timeout):
        time.sleep(0.01)
        payload = json.loads(request.data)
        if payload['messages'][0]['content'].startswith(COMPARE):
            self.served += 1
            if self.served > self.limit:
                raise KeyboardInterrupt
        return super().__call__(request, timeout)


def write_products(path, count=12):
    """商品清单只造一次、三处（冷跑、子进程、重开）都从这里读：版本与缓存键逐字一致。

    同色（视觉同款）、异名（版本互不相同，证据索引能按版本对号）。
    """
    image = product_picture('red')
    data = 'data:image/png;base64,' + base64.b64encode(image['content']).decode()
    items = [dict(shop_key='shop' + str(n), offer_id=str(n), product_name='月牙杯%02d' % n,
                  image_hash=image['hash'], image_data=data, information_complete=True, sales=n)
             for n in range(count)]
    path.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
    return json.loads(path.read_text(encoding='utf-8'))


def asked_pairs(transport):
    """这次运行问过模型的商品对（按名字；名字＋图哈希就是判断的键）。"""
    names = []
    for payload in transport.calls:
        if payload['messages'][0]['content'].startswith(COMPARE):
            content = payload['messages'][1]['content']
            names.append(tuple(sorted(json.loads(part['text'])['name'] for part in content
                                      if part.get('text', '').startswith('{'))))
    return names


def judged_rows(cache):
    """盘上已提交的判断与证据索引（打开即触发 journal 恢复，如进程被硬结束过所留）。"""
    conn = sqlite3.connect(cache)
    try:
        judgments = conn.execute('SELECT evidence_a,evidence_b FROM judgments').fetchall()
        evidence = {row[0] for row in conn.execute('SELECT version FROM evidence')}
    finally:
        conn.close()
    return judgments, evidence


def member_names(groups):
    return sorted(sorted(m['shop_key'] + '/' + m['offer_id'] for m in g['members']) for g in groups)


class MatchingCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-value'}).start()

    def cache(self, name):
        return Path(self.tmp.name)/(name+'.sqlite')

    def suggest(self, cache, items, transport):
        with patch('bestseller_monitor.matching.urlopen', side_effect=transport):
            return MatchingService(MatchingConfig(cache, mode='direct')).suggest(items, singles(items))

    def test_interrupted_run_keeps_whole_batches_only(self):
        """批与批之间退出：库里的判断是整数批——当前批整批回滚，不半批落盘。

        每批 3 条、传输在第 8 次同款比较时打断：已完成的判断以 3 的整数倍留在库里，
        证据索引与判断的版本集合一致（同批写入）。
        """
        items = write_products(Path(self.tmp.name)/'products.json')
        batch = 3
        with patch('bestseller_monitor.matching.JUDGMENT_COMMIT_BATCH', batch):
            with self.assertRaises(KeyboardInterrupt):
                self.suggest(self.cache('interrupted'), items, InterruptedTransport(7))

        judgments, evidence = judged_rows(self.cache('interrupted'))
        self.assertEqual(len(judgments) % batch, 0, f'次数不是整数批：{len(judgments)}')
        self.assertGreater(len(judgments), 0, '中途退出前跑完的批应当留在库里')
        self.assertEqual(evidence, {v for row in judgments for v in row},
                         '判断与证据索引同批落盘：两边的版本集合必须一致')

    def test_judgment_phase_flushes_before_the_grouping_tail(self):
        """判断阶段一结束就落盘：后面的分组计算出意外，判断也不悬在事务里。

        批 3、共 50 对判断：整批提交到 48，余下 2 条靠判断阶段的收尾提交落盘。分组计算
        被打断（逃逸异常）后，库里应当是全部 50 条——没有收尾提交就只剩 48。
        """
        items = write_products(Path(self.tmp.name)/'products.json')
        transport = InterruptedTransport(10 ** 9)     # 不打断：让判断阶段整个跑完
        with patch('bestseller_monitor.matching.JUDGMENT_COMMIT_BATCH', 3), \
                patch('bestseller_monitor.matching.update_candidates', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.suggest(self.cache('tail'), items, transport)

        asked = asked_pairs(transport)
        judgments, evidence = judged_rows(self.cache('tail'))
        self.assertNotEqual(len(asked) % 3, 0, '这条用例测的就是余数批；换夹具时别让它整除')
        self.assertEqual(len(judgments), len(asked), '判断阶段一结束就该全部落盘')
        self.assertEqual(evidence, {v for row in judgments for v in row},
                         '判断与证据索引同批落盘：两边的版本集合必须一致')

    def test_killed_process_rerun_fills_only_the_unjudged(self):
        """真关窗：冷跑量出全量调用数 → 子进程被硬结束 → 重开只补未判的（A10 演示路径）。"""
        items = write_products(Path(self.tmp.name)/'products.json')
        cold = ModelTransport()
        reference = self.suggest(self.cache('cold'), items, cold)
        asked = asked_pairs(cold)
        names_by_version = {version(p): p['product_name'] for p in items}

        child = subprocess.run(
            [sys.executable, '-c', PROBE, str(self.cache('killed')),
             str(Path(self.tmp.name)/'products.json'), str(len(asked) - 5)],
            cwd=ROOT, capture_output=True, env={**os.environ, 'DEEPSEEK_API_KEY': 'secret-value'})

        self.assertNotEqual(child.returncode, 0, child.stderr.decode('utf-8', 'replace')[-400:])
        judgments, evidence = judged_rows(self.cache('killed'))
        self.assertTrue(0 < len(judgments) < len(asked),
                        f'中途结束应当留下部分判断：{len(judgments)}/{len(asked)}')
        self.assertEqual(len(judgments) % JUDGMENT_COMMIT_BATCH, 0,
                         f'留在库里的判断是整数批：{len(judgments)} 不是 {JUDGMENT_COMMIT_BATCH} 的倍数')
        self.assertEqual(evidence, {v for row in judgments for v in row},
                         '判断与证据索引同批落盘：两边的版本集合必须一致')

        committed = {tuple(sorted(names_by_version[v] for v in row)) for row in judgments}
        reopened = ModelTransport()
        resumed = self.suggest(self.cache('killed'), items, reopened)
        self.assertEqual(set(asked_pairs(reopened)), set(asked) - committed,
                         '重开只补未判的：已提交的商品对零调用')
        self.assertEqual(member_names(resumed), member_names(reference), '重开补齐后的分组与冷跑一致')


if __name__ == '__main__':
    unittest.main()
