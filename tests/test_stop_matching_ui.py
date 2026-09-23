"""票 02（停得下）：停止匹配、重试入口与停止后的落态——浏览器缝。

真页面 + 真本地服务 + 假模型网络（沿用票 01 的遮罩夹具与假传输：
`pass_first` 决定前几对判断放行，其余卡在闸门上，遮罩因此停得下来可供断言）。
服务缝（终态契约、缓存里的落态）在 test_stop_matching.py。
"""
import unittest
from pathlib import Path

from playwright.sync_api import expect

import test_analysis as fixture
import test_analysis_progress_ui as mask_fixture
import test_group_editing as editing
from bestseller_monitor.matching import MatchingConfig, MatchingService


class MatchingStopTests(unittest.TestCase):
    setUp = fixture.AnalysisBrowserTests.setUp
    stop_server = fixture.AnalysisBrowserTests.stop_server
    submit = fixture.AnalysisBrowserTests.submit
    submit_product = fixture.AnalysisBrowserTests.submit_product
    dates = fixture.AnalysisBrowserTests.dates
    seed = editing.GroupEditingTests.seed
    model = mask_fixture.MatchingMaskTests.model
    enter = mask_fixture.MatchingMaskTests.enter

    def stopped_run(self):
        """跑起来、判完两对就点停止，返回（遮罩, 假传输）——各用例的公共前置。"""
        transport = self.model(pass_first=2)
        self.seed(4)
        mask = self.enter()
        expect(mask.locator('#maskMain')).to_have_text('已完成 2 / 6 对')
        expect(mask.locator('#maskStopNote')).to_be_hidden()   # 还没停：这两句先不说
        mask.get_by_role('button', name='停止匹配').click()
        return mask, transport

    def test_stopping_state_waits_for_the_call_in_flight_then_lands_on_the_page(self):
        mask, transport = self.stopped_run()

        expect(mask.locator('#maskTitle')).to_have_text('正在停止匹配…')
        expect(mask.locator('#maskPhase')).to_contain_text('最多 30 秒')
        expect(mask.locator('#maskPhase')).to_contain_text('已判的会留下')
        expect(mask.locator('#maskStop')).to_be_disabled()
        expect(mask.locator('#maskMain')).to_have_text('已完成 2 / 6 对')     # 数字停在被点那一刻
        # 停止态的两句实话：停下后怎么落，以及两个入口同一态、窗口留到收尾完（票 03 那半）。
        expect(mask.locator('#maskStopNote')).to_be_visible()
        expect(mask.locator('#maskStopNote')).to_contain_text('没判断的对记为「失败」')
        expect(mask.locator('#maskStopNote')).to_contain_text('进的都是这一态')
        expect(mask.locator('#maskStopNote')).to_contain_text('收尾完成前窗口不消失')
        expect(mask.locator('.mask-steps li.cur')).to_have_text('逐对判断同款')

        transport.release.set()                    # 在途的两对收尾：这遮罩才关
        expect(mask).to_be_hidden()
        expect(self.page.locator('#matchingStatus')).to_have_text(
            '本次匹配已停止：已完成 4 / 6 对，未判完的对记为「失败」，建议重试')
        expect(self.page.get_by_role('button', name='模型匹配同款')).to_be_enabled()
        expect(self.page.locator('#groupDetail')).to_contain_text('已停止匹配，未判断')
        expect(self.page.locator('.group-choice', has_text='4 个商品')).to_be_visible()

    def test_retry_runs_through_the_same_mask_with_only_the_pairs_left(self):
        mask, transport = self.stopped_run()
        transport.release.set()
        expect(mask).to_be_hidden()

        retry = self.model(pass_first=0)           # 重试的第一对卡住：遮罩才留得住
        self.page.get_by_role('button', name='模型匹配同款').click()
        expect(mask.locator('#maskTitle')).to_have_text('正在匹配同款（重试）')
        expect(mask.locator('#maskMain')).to_have_text('已完成 0 / 2 对')
        expect(mask.locator('#maskSub')).to_have_text('本次要判 2 对，其余 4 对命中缓存（不花钱）')
        expect(mask.get_by_role('button', name='停止匹配')).to_be_visible()
        # 遮罩下面还是本页已有分组，不是骨架（票 01 的规矩里重试那趟的例外）。
        expect(self.page.locator('.group-choice', has_text='4 个商品')).to_be_visible()
        expect(self.page.locator('.skeleton')).to_have_count(0)
        expect(self.page.locator('.step.active')).to_have_attribute('data-tab', 'review')

        retry.release.set()
        expect(mask).to_be_hidden()
        expect(self.page.locator('#matchingStatus')).to_have_text('模型匹配完成：4 个商品已判断')
        expect(self.page.get_by_role('button', name='模型匹配同款')).to_be_disabled()

    def test_instant_retry_never_leaves_the_mask_standing(self):
        """没有要判的对（全命中缓存）时重试秒完：遮罩不挂住，页面直接是跑完的样子。"""
        mask, transport = self.stopped_run()
        transport.release.set()                    # 闸门一直开着：重试那两对秒完
        expect(mask).to_be_hidden()

        self.page.get_by_role('button', name='模型匹配同款').click()
        expect(self.page.locator('#matchingStatus')).to_have_text('模型匹配完成：4 个商品已判断')
        expect(mask).to_be_hidden()

    def test_reload_during_a_retry_lands_back_on_the_review_page(self):
        mask, transport = self.stopped_run()
        transport.release.set()
        expect(mask).to_be_hidden()

        retry = self.model(pass_first=0)
        self.page.get_by_role('button', name='模型匹配同款').click()
        expect(mask.locator('#maskMain')).to_have_text('已完成 0 / 2 对')

        # 刷新后按分析号接回：快照还在判断锁后面取不到，遮罩下面先架骨架（跑完照常填页）。
        self.page.reload()
        mask = self.page.get_by_role('dialog', name='匹配进度')
        expect(mask.locator('#maskTitle')).to_have_text('正在匹配同款（重试）')
        expect(mask.locator('#maskMain')).to_have_text('已完成 0 / 2 对')
        expect(self.page.locator('#reviewPage')).to_be_visible()

        retry.release.set()
        expect(mask).to_be_hidden()
        expect(self.page.locator('#matchingStatus')).to_have_text('模型匹配完成：4 个商品已判断')
        expect(self.page.locator('.group-choice', has_text='4 个商品')).to_be_visible()

    def test_missing_cache_of_a_retry_fails_in_place_and_retry_re_runs_matching(self):
        mask, transport = self.stopped_run()
        transport.release.set()
        expect(mask).to_be_hidden()

        # 判断缓存换成打不开的目录：重试那趟就地报「判断缓存或分析草稿读写失败」。
        blocked = Path(self.tmp.name) / 'cache-as-directory'
        blocked.mkdir()
        good = self.service.matcher
        self.service.matcher = MatchingService(MatchingConfig(blocked, mode='direct'))
        self.page.get_by_role('button', name='模型匹配同款').click()
        expect(mask.locator('#maskTitle')).to_have_text('匹配没能完成')
        expect(mask.locator('#maskError')).to_contain_text('判断缓存或分析草稿读写失败')
        expect(mask.get_by_role('button', name='返回重选日期')).to_be_visible()

        # 修好配置再点「重试」：重跑的是匹配那一段（快照还在），跑完照常落地。
        self.service.matcher = good
        mask.get_by_role('button', name='重试').click()
        expect(mask).to_be_hidden()
        expect(self.page.locator('#matchingStatus')).to_have_text('模型匹配完成：4 个商品已判断')

    def test_stop_entry_is_gone_in_the_hard_failure_state(self):
        mask, transport = self.stopped_run()
        transport.release.set()
        expect(mask).to_be_hidden()

        # 空库存区间的硬失败：只有「重试」与「返回重选日期」，没有停止入口可点。
        self.page.get_by_role('button', name='重新选择日期').click()
        failed = self.enter('2026-10-05', '2026-10-12')
        expect(failed.locator('#maskTitle')).to_have_text('匹配没能完成')
        expect(failed.locator('#maskLive')).to_be_hidden()
        expect(failed.locator('#maskStopNote')).to_be_hidden()


if __name__ == '__main__':
    unittest.main()
