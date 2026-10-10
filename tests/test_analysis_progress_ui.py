"""票 01（看得见）：遮罩、轮询、刷新接回、硬失败与「需配置」——浏览器缝。

真页面 + 真本地服务 + 假模型网络（沿用分组编辑夹具与 test_matching 的假传输：
只有模型与图片网络被替换，页面、HTTP、服务、临时库都是真的）。
"""
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect

import test_analysis as fixture
import test_group_editing as editing
from bestseller_monitor.matching import MatchingConfig, MatchingService
from test_analysis_progress import StagedTransport


def _drain_run(service, transport):
    """放行闸门，并等这次运行走到终态（终态以后不再有任何写入）。

    正常路径用例自己会等到遮罩关闭；失败在半路时也得给后台判断线程松手的机会——
    否则它还攥着 match.sqlite，临时目录清理会撞 WinError 32（重负载下真撞过）。
    """
    transport.release.set()
    reading = service.in_flight_job()
    if reading:
        service.wait_terminal(reading['id'], timeout=30)


class MatchingMaskTests(unittest.TestCase):
    setUp = fixture.AnalysisBrowserTests.setUp
    stop_server = fixture.AnalysisBrowserTests.stop_server
    submit = fixture.AnalysisBrowserTests.submit
    submit_product = fixture.AnalysisBrowserTests.submit_product
    dates = fixture.AnalysisBrowserTests.dates
    seed = editing.GroupEditingTests.seed

    def model(self, **options):
        """四件同图商品 + 假模型；`pass_first` 决定前几对判断放行（其余卡在闸门上等用例放行）。"""
        transport = StagedTransport(**options)
        self.service.matcher = MatchingService(
            MatchingConfig(Path(self.tmp.name) / 'match.sqlite', mode='direct'))
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test'}).start()
        patch('bestseller_monitor.matching.urlopen', side_effect=transport).start()
        # 最后登记、最先跑：失败在半路也要放行闸门并等运行到终态（见 _drain_run）。
        self.addCleanup(_drain_run, self.service, transport)
        return transport

    def wait_judged(self, count, timeout=15):
        """等这次运行的服务读数真的判完 count 对（假传输的闸门保证读数停得住）。

        遮罩文本最多慢一个轮询周期；先等读数、再断言文本，5 秒的 DOM 窗口就不和
        后台判断在重负载下赛跑。读数等不到时照常红，失败信息带着当时的读数。
        """
        deadline = time.monotonic() + timeout
        reading = None
        while time.monotonic() < deadline:
            reading = self.service.in_flight_job()
            if reading and reading['judged'] >= count:
                return reading
            time.sleep(0.01)
        self.fail(f'等不到判断数走到 {count}：{reading}')

    def enter(self, start='2026-09-07', end='2026-09-14'):
        """在日期页点「下一步」，返回遮罩。"""
        self.dates()
        if (start, end) != ('2026-09-07', '2026-09-14'):
            self.page.get_by_label('开始日期', exact=True).fill(start)
            self.page.get_by_label('结束日期', exact=True).fill(end)
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        return self.page.get_by_role('dialog', name='匹配进度')

    def test_mask_shows_phase_and_counts_then_closes_into_the_review_page(self):
        transport = self.model(pass_first=2)
        self.seed(4)
        self.submit_product('99', '2026-09-07', 50, name='无图商品')   # 缺证据、不参与判断
        mask = self.enter()
        expect(mask).to_be_visible()
        expect(self.page.locator('#datePage')).to_be_hidden()
        expect(self.page.locator('.step.active')).to_have_attribute('data-tab', 'review')
        expect(mask.locator('#maskTitle')).to_have_text('正在匹配同款')
        expect(mask.locator('#maskPhase')).to_contain_text('第 4 步 / 共 5 步')
        expect(mask.locator('#maskPhase')).to_contain_text('逐对判断同款')
        expect(mask.locator('.mask-steps li.cur')).to_have_text('逐对判断同款')
        self.wait_judged(2)                    # 先等服务读数判完两对，再看遮罩文本（慢一个轮询周期）
        expect(mask.locator('#maskMain')).to_have_text('已完成 2 / 6 对')
        expect(mask.locator('#maskSub')).to_contain_text('可判 4 个商品')
        expect(mask.locator('#maskSub')).to_contain_text('命中缓存不花钱')
        expect(mask.locator('#maskSub')).to_contain_text('另有 1 个商品缺证据不参与')
        expect(mask.locator('#maskTime')).to_contain_text('已用')
        expect(mask.locator('#maskTime')).to_contain_text('正在估算')   # 假模型是秒回的，样本不足

        transport.release.set()
        expect(mask).to_be_hidden()
        expect(self.page.locator('.group-choice', has_text='4 个商品')).to_be_visible()
        self.assertIn('analysis=', self.page.url)

    def test_mask_time_says_when_judging_is_done(self):
        """判完全部对数、还在装配时，时间行不该继续说「正在估算」（那是判断阶段的话）。"""
        self.seed(4)
        self.enter()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('日期区间')
        self.assertEqual(self.page.evaluate("timeSuffix({todo: 6, judged: 6, eta_text: ''})"),
                         '判断已完成，正在装配')
        self.assertEqual(self.page.evaluate("timeSuffix({todo: 6, judged: 2, eta_text: ''})"), '正在估算…')
        self.assertEqual(self.page.evaluate("timeSuffix({todo: 6, judged: 2, eta_text: '8 分钟'})"),
                         '预计还需约 8 分钟')

    def test_mask_survives_escape_and_backdrop_until_the_run_ends(self):
        transport = self.model(pass_first=2)
        self.seed(4)
        mask = self.enter()
        expect(mask).to_be_visible()

        self.page.keyboard.press('Escape')
        expect(mask).to_be_visible()
        self.page.mouse.click(5, 5)
        expect(mask).to_be_visible()

        transport.release.set()
        expect(mask).to_be_hidden()

    def test_hard_failure_stays_in_the_mask_with_both_ways_out(self):
        # 区间选两个「全量抓取日」（周一）之后的空区间：既没有库存，也不触发全量提醒。
        mask = self.enter('2026-10-05', '2026-10-12')
        expect(mask.locator('#maskTitle')).to_have_text('匹配没能完成')
        expect(mask.locator('#maskPhase')).to_contain_text('2026-10-05 — 2026-10-12')
        expect(mask.locator('#maskError')).to_contain_text('该日期区间没有可分析的库存，请重新选择日期')
        expect(self.page.locator('#error')).to_be_hidden()
        expect(mask.get_by_role('button', name='重试')).to_be_visible()

        mask.get_by_role('button', name='返回重选日期').click()
        expect(mask).to_be_hidden()
        expect(self.page.locator('#datePage')).to_be_visible()
        expect(self.page.get_by_label('开始日期', exact=True)).to_have_value('2026-10-05')
        self.assertNotIn('analysis=', self.page.url)

    def test_reload_re_attaches_to_the_running_analysis(self):
        transport = self.model(pass_first=2)
        self.seed(4)
        mask = self.enter()
        self.wait_judged(2)                    # 先等服务读数判完两对，再看遮罩文本（慢一个轮询周期）
        expect(mask.locator('#maskMain')).to_have_text('已完成 2 / 6 对')

        self.page.reload()
        mask = self.page.get_by_role('dialog', name='匹配进度')
        expect(mask).to_be_visible()
        expect(mask.locator('#maskMain')).to_have_text('已完成 2 / 6 对')

        transport.release.set()
        expect(mask).to_be_hidden()
        expect(self.page.locator('.group-choice', has_text='4 个商品')).to_be_visible()

    def test_confirmation_comes_before_the_mask_when_a_crawl_is_running(self):
        transport = self.model(pass_first=2)
        self.seed(4)
        self.running = True
        mask = self.enter()
        expect(self.page.locator('#notice')).to_be_visible()
        expect(self.page.locator('#noticeText')).to_contain_text('锁定当前数据')
        expect(mask).to_be_hidden()

        self.page.get_by_role('button', name='继续').click()
        expect(mask).to_be_visible()
        self.wait_judged(2)                    # 先等服务读数判完两对，再看遮罩文本（慢一个轮询周期）
        expect(mask.locator('#maskMain')).to_have_text('已完成 2 / 6 对')
        transport.release.set()
        expect(mask).to_be_hidden()
        self.running = False

    def test_instant_run_goes_straight_to_the_loaded_page_without_flashing_the_mask(self):
        """匹配没启用（或全命中缓存）时秒完：确认同款页直接填好，遮罩不闪一下。"""
        self.seed(4)
        self.page.evaluate("()=>{window.maskFlashed=false;"
                           "document.getElementById('matchMask').addEventListener('close',"
                           "()=>{window.maskFlashed=true});}")
        mask = self.enter()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('日期区间')
        expect(self.page.locator('.group-choice')).to_have_count(4)
        self.assertFalse(self.page.evaluate("()=>window.maskFlashed"))
        expect(mask).to_be_hidden()

    def test_missing_key_is_not_a_hard_failure(self):
        self.model(pass_first=2)
        self.seed(4)
        patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}).start()
        mask = self.enter()
        expect(mask).to_be_hidden()
        expect(self.page.locator('#matchMask')).to_be_hidden()
        expect(self.page.locator('#reviewPage')).to_be_visible()
        expect(self.page.locator('#matchControl button')).to_be_disabled()
        expect(self.page.locator('#matchControl')).to_have_attribute(
            'title', '重试无效：请检查分析配置后重启分析程序')


if __name__ == '__main__':
    unittest.main()
