"""票 06：分析结果独立成屏——保存后进入、可返回，排名 20 组/屏分页。

浏览器用例走真实页面与本地接口（沿用分组编辑夹具）：落屏与返回、刷新落屏规则、
分页边界与全局名次、再进屏与零销量开关都回第 1 屏、导出 HTML 报告仍是全量。
"""
import dataclasses
import re
import unittest
from pathlib import Path

from playwright.sync_api import expect

import test_analysis as analysis_fixture
import test_group_editing as fixture


class ResultsScreenTests(unittest.TestCase):
    setUp = fixture.GroupEditingTests.setUp
    stop_server = fixture.GroupEditingTests.stop_server
    submit = fixture.GroupEditingTests.submit
    dates = fixture.GroupEditingTests.dates
    switch_tab = fixture.GroupEditingTests.switch_tab
    seed = fixture.GroupEditingTests.seed
    add_product = analysis_fixture.AnalysisBrowserTests.add_product
    submit_product = analysis_fixture.AnalysisBrowserTests.submit_product
    back_to_review = analysis_fixture.AnalysisBrowserTests.back_to_review

    def review(self):
        """选好区间、跑完分析，停在确认屏（快照真的落定）。"""
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('日期区间')

    def confirm_all(self):
        self.page.get_by_role('button', name='确认当前筛选全部组').click()
        self.page.get_by_role('dialog').get_by_role('button', name='确认', exact=True).click()
        expect(self.page.locator('#saveAndView')).to_be_enabled()

    def save_and_view(self):
        self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        expect(self.page.locator('#results')).to_be_visible()

    def results_heading(self):
        return self.page.get_by_role('heading', name='畅销品', exact=True)

    def rank_rows(self):
        return self.page.locator('#ranking > details')

    def rank_page(self):
        return self.page.locator('#rankPageInfo')

    def rank_pager(self, label):
        """结果屏自己的分页按钮：与确认屏左列的同名按钮按可访问名区分。"""
        return self.page.get_by_role('button', name=f'畅销品{label}')

    def test_save_lands_on_the_results_screen_and_back_returns_to_the_last_saved_review(self):
        self.seed()
        self.review()
        expect(self.page.locator('#reviewPage')).to_be_visible()
        expect(self.results_heading()).to_be_hidden()      # 结果不再挂在确认屏下方
        self.confirm_all()
        self.save_and_view()
        # 结果屏独屏：屏内标题「畅销品」，日期屏与确认屏都让位。
        expect(self.results_heading()).to_be_visible()
        expect(self.page.locator('#reviewPage')).to_be_hidden()
        expect(self.page.locator('#datePage')).to_be_hidden()
        # 阶段图例由当前屏派生点亮：结果节点当前，前两节点已完成。
        expect(self.page.locator('.tabs .step[data-tab="result"]')
               ).to_have_class(re.compile(r'\bactive\b'))
        # 返回：回确认屏，呈现最近一次保存的版本——四组都在「已确认」页签里，待确认页是空的。
        self.back_to_review()
        expect(self.results_heading()).to_be_hidden()
        expect(self.page.locator('#dirtyStatus')).to_have_text('')
        expect(self.page.locator('#saveStatus')).to_contain_text('已保存')
        self.switch_tab('待确认')
        expect(self.page.locator('.group-choice')).to_have_count(0)
        self.switch_tab('已确认')
        expect(self.page.locator('.group-choice')).to_have_count(4)

    def test_reload_lands_on_results_only_when_saved_and_without_pending_groups(self):
        self.seed()
        self.review()
        # 还有待确认组：刷新回确认屏。
        self.page.reload()
        expect(self.page.locator('#reviewPage')).to_be_visible()
        expect(self.results_heading()).to_be_hidden()
        # 全部确认但还没保存：仍是确认屏（门禁要求「已保存」）。
        self.confirm_all()
        self.page.reload()
        expect(self.page.locator('#reviewPage')).to_be_visible()
        expect(self.results_heading()).to_be_hidden()
        # 已保存且没有待确认组：刷新落在结果屏。
        self.save_and_view()
        self.page.reload()
        expect(self.results_heading()).to_be_visible()
        expect(self.page.locator('#reviewPage')).to_be_hidden()

    def test_ranking_pages_twenty_groups_with_boundaries_and_global_numbers(self):
        self.seed(21)
        self.review()
        self.confirm_all()
        self.save_and_view()
        previous, following = self.rank_pager('上一页'), self.rank_pager('下一页')
        expect(self.rank_rows()).to_have_count(20)          # 21 组：第 1 屏满 20 组
        expect(self.rank_page()).to_have_text('第 1 / 2 屏 · 共 21 组')
        expect(previous).to_be_disabled()
        expect(following).to_be_enabled()
        following.click()
        expect(self.rank_rows()).to_have_count(1)
        expect(self.rank_page()).to_have_text('第 2 / 2 屏 · 共 21 组')
        expect(following).to_be_disabled()
        expect(previous).to_be_enabled()
        # 名次是全局序号：第 2 屏第一行接着 21。
        expect(self.rank_rows().first.locator(':scope > summary .product > b')).to_have_text('21')
        previous.click()
        expect(self.rank_page()).to_have_text('第 1 / 2 屏 · 共 21 组')

    def test_zero_sales_toggle_and_reentering_the_screen_both_return_to_the_first_page(self):
        self.seed(21)
        self.add_product('99', '零销量杯', 100, 100)         # 两天同量：零销量组，默认不显示
        self.review()
        self.confirm_all()
        self.save_and_view()
        self.rank_pager('下一页').click()
        expect(self.rank_page()).to_have_text('第 2 / 2 屏 · 共 21 组')
        # 切「显示零销量组」：回第 1 屏，零销量组进列表（共 22 组）。
        self.page.get_by_label('显示零销量组').check()
        expect(self.rank_page()).to_have_text('第 1 / 2 屏 · 共 22 组')
        self.rank_pager('下一页').click()
        expect(self.rank_page()).to_have_text('第 2 / 2 屏 · 共 22 组')
        # 返回确认屏再进结果屏：分屏回第 1 屏。
        self.back_to_review()
        self.save_and_view()
        expect(self.rank_page()).to_have_text('第 1 / 2 屏 · 共 22 组')

    def test_export_from_a_later_page_still_writes_every_group(self):
        self.seed(21)
        self.review()
        self.confirm_all()
        self.save_and_view()
        self.rank_pager('下一页').click()
        expect(self.rank_rows()).to_have_count(1)           # 屏上只剩第 2 屏这一页
        out = Path(self.tmp.name) / 'output'
        self.service.config = dataclasses.replace(self.service.config, output=out)
        self.page.get_by_role('button', name='导出 HTML 报告').click()
        expect(self.page.locator('#exportStatus')).to_contain_text('已导出 · ')
        report = Path(self.page.locator('#exportStatus').inner_text().split(' · ', 1)[1])
        self.assertEqual(report.parent, out)
        # 打开报告数一遍：21 组都在，与屏上这一屏多少行无关。
        self.page.goto(report.as_uri())
        expect(self.page.get_by_role('heading', name='销量分析报告')).to_be_visible()
        expect(self.page.locator('#ranking > details')).to_have_count(21)


if __name__ == '__main__':
    unittest.main()
