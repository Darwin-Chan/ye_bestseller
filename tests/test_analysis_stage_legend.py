"""票 16：分析页顶部的分阶段图例（选择区间 → 确认同款 → 分析结果）。

浏览器用例走真实页面与本地接口，只替换模型与图片网络（沿用分组编辑夹具）。
"""
import re
import unittest

from playwright.sync_api import expect

import test_group_editing as fixture


class StageLegendTests(unittest.TestCase):
    setUp = fixture.GroupEditingTests.setUp
    stop_server = fixture.GroupEditingTests.stop_server
    submit = fixture.GroupEditingTests.submit
    dates = fixture.GroupEditingTests.dates
    switch_tab = fixture.GroupEditingTests.switch_tab

    def stages(self):
        """图例三节点的 [标签, 当前, 已完成]，顺序即节点顺序。"""
        return self.page.eval_on_selector_all(
            '.tabs .step',
            'els=>els.map(el=>[el.querySelector(".lbl").textContent,'
            ' el.classList.contains("active"), el.classList.contains("done")])')

    def assert_stages(self, current, done):
        labels = ['选择区间', '确认同款', '分析结果']
        self.assertEqual(self.stages(),
                         [[label, label == current, label in done] for label in labels])

    def dot_color(self, label):
        """节点圆点的背景色：灯色由 CSS 承担，这里把它钉到组件契约上。"""
        dot = self.page.locator('.tabs .step').filter(has_text=label).locator('.dot')
        return dot.evaluate('el => getComputedStyle(el).backgroundColor')

    def test_the_legend_tracks_the_three_screens_and_resets_on_the_way_back(self):
        expect(self.page.get_by_role('heading', name='选择销量计算区间')).to_be_visible()
        self.assert_stages('选择区间', set())
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.get_by_role('heading', name='确认同款')).to_be_visible()
        self.assert_stages('确认同款', {'选择区间'})
        self.assertEqual(self.dot_color('选择区间'), 'rgb(22, 163, 74)')
        self.assertEqual(self.dot_color('确认同款'), 'rgb(37, 99, 235)')
        self.assertEqual(self.dot_color('分析结果'), 'rgb(255, 255, 255)')
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        expect(self.page.get_by_role('heading', name='初步畅销品')).to_be_visible()
        self.assert_stages('分析结果', {'选择区间', '确认同款'})
        # 重开页面沿 #analysis= 自动恢复到结果屏，灯序不变。
        self.page.reload()
        expect(self.page.get_by_role('heading', name='初步畅销品')).to_be_visible()
        self.assert_stages('分析结果', {'选择区间', '确认同款'})
        self.page.get_by_role('button', name='重新选择日期').click()
        expect(self.page.get_by_role('heading', name='选择销量计算区间')).to_be_visible()
        self.assert_stages('选择区间', set())

    def test_the_legend_is_inert_and_keeps_the_review_tabs_untouched(self):
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.get_by_role('heading', name='确认同款')).to_be_visible()
        # 与库存抓取页一致：纯指示，不挂点击行为。属性与 property 分开查：
        # 未设置时 el.onclick 是 null（IDL 属性），函数值又会被 Playwright 序列化成
        # None——只在页面里比较出布尔值才判得动。addEventListener 查不到，由上面
        # 「点完不动」与页签断言兜底。
        probes = self.page.eval_on_selector_all(
            '.tabs .step',
            'els=>els.map(el=>el.onclick === null && el.getAttribute("onclick") === null)')
        self.assertEqual(probes, [True, True, True])
        for tab in ('dates', 'review', 'result'):
            self.page.locator(f'.tabs .step[data-tab="{tab}"]').click()
        # 本页的切屏动作多在 await 之后落地：等一拍再断言，异步副作用也逃不掉。
        self.page.wait_for_timeout(200)
        self.assert_stages('确认同款', {'选择区间'})
        expect(self.page.get_by_role('heading', name='确认同款')).to_be_visible()
        # 图例没有借 data-tab 混进页签：页签仍是票 15 的三个，选中仍停在「待确认」。
        self.assertEqual(self.page.get_by_role('tab').count(), 3)
        expect(self.page.get_by_role('tab', name=re.compile('^待确认'))).to_have_attribute('aria-selected', 'true')
        self.switch_tab('全部')
        self.assert_stages('确认同款', {'选择区间'})


if __name__ == '__main__':
    unittest.main()
