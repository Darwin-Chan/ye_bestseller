"""票 02：一套外壳——白面板、页签计数与结果条目铺满。

浏览器用例走真实页面与本地接口，只替换模型与图片网络（沿用分组编辑夹具）。
面板、铺满与页签样式是视觉契约，这里钉住可断言的部分：面板归属（谁在里面、谁在外）、
「一套」的实测对齐（确认屏与日期屏同款）、徽标文本与选中态、条目白底的来源。
"""
import re
import unittest

from playwright.sync_api import expect

import test_analysis_conflicts as conflicts
import test_group_editing as fixture


def panel_style(page, selector):
    """一个节点作为面板的可见长相：底色、边框样式与宽度、圆角、内边距。"""
    return page.eval_on_selector(selector, """el=>{const s=getComputedStyle(el);
        return [s.backgroundColor,s.borderTopStyle,s.borderTopWidth,s.borderRadius,s.padding]}""")


def tab_texts(page):
    """三个页签的可见文本（含计数徽标），顺序即页签顺序。"""
    return [page.get_by_role('tab').nth(index).inner_text() for index in range(3)]


class ShellPanelTests(unittest.TestCase):
    setUp = fixture.GroupEditingTests.setUp
    stop_server = fixture.GroupEditingTests.stop_server
    submit = fixture.GroupEditingTests.submit
    dates = fixture.GroupEditingTests.dates
    seed = fixture.GroupEditingTests.seed
    switch_tab = fixture.GroupEditingTests.switch_tab

    def review(self):
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('日期区间')

    def tab_style(self, label):
        """页签的可计算样式。票 02 起页签名带计数徽标，按标签前缀认。"""
        return self.page.get_by_role('tab', name=re.compile(f'^{label}')).evaluate("""el=>{const s=getComputedStyle(el);
            return {background:s.backgroundColor,bottom:parseFloat(s.borderBottomWidth)||0,color:s.color,
                    weight:parseFloat(s.fontWeight)||0}}""")

    def test_review_screen_wears_the_same_panel_as_the_date_screen(self):
        self.seed()
        date_panel = panel_style(self.page, '#datePage')
        self.review()
        self.assertEqual(panel_style(self.page, '#reviewPage'), date_panel)
        # 面板包住屏内全部内容（统计、页签、筛选、批量行、两栏、分页、保存行）。
        self.assertEqual(self.page.evaluate("""()=>{const panel=document.querySelector('#reviewPage');
            return ['#snapshotInfo','.review-tabs','.review-filters','#batchRow','.review-layout','#prev','#saveAndView']
                .map(selector=>panel.contains(document.querySelector(selector)))}"""), [True] * 7)
        # 阶段图例留在面板外。
        self.assertTrue(self.page.evaluate(
            "()=>!document.querySelector('#reviewPage').contains(document.querySelector('.tabs'))"))

    def test_right_column_drops_its_second_white_card(self):
        self.seed()
        self.review()
        # 右列栏容器不再自刷白、不再自带边框与内衬：白与内边距都由面板出。
        detail = self.page.eval_on_selector('#groupDetail', """el=>{const s=getComputedStyle(el);
            return {background:s.backgroundColor,border:s.borderTopStyle,padding:s.paddingTop}}""")
        self.assertNotEqual(detail['background'], panel_style(self.page, '#reviewPage')[0])
        self.assertEqual([detail['border'], detail['padding']], ['none', '0px'])

    def test_tab_badges_count_groups_and_follow_confirmations(self):
        self.seed()
        self.review()
        self.assertEqual(tab_texts(self.page), ['待确认 4', '已确认 0', '全部 4'])
        # 确认一组：它离开待确认、进已确认，全部不变。
        self.page.locator('.group-choice').first.click()
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        expect(self.page.locator('.group-choice')).to_have_count(3)
        self.assertEqual(tab_texts(self.page), ['待确认 3', '已确认 1', '全部 4'])

    def test_selected_tab_is_an_underline_not_a_solid_block(self):
        self.seed()
        self.review()
        selected, idle = self.tab_style('待确认'), self.tab_style('已确认')
        # 选中态只看相对关系：底色不等于字色（不再是实心主色块）、下划线比未选中宽、字比未选中重。
        self.assertNotEqual(selected['background'], selected['color'])
        self.assertGreater(selected['bottom'], idle['bottom'])
        self.assertGreater(selected['weight'], idle['weight'])
        self.assertEqual(selected['color'], 'rgb(37, 99, 235)')   # 主色（与页面调色板同源）
        self.switch_tab('已确认')
        moved = self.tab_style('已确认'), self.tab_style('待确认')
        self.assertGreater(moved[0]['bottom'], moved[1]['bottom'])
        self.assertGreater(moved[0]['weight'], moved[1]['weight'])

    def test_result_entries_borrow_the_screen_panel_white(self):
        self.seed()
        self.review()
        self.page.get_by_role('button', name='确认当前筛选全部组').click()
        self.page.get_by_role('dialog').get_by_role('button', name='确认', exact=True).click()
        self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        expect(self.page.get_by_role('heading', name='初步畅销品')).to_be_visible()
        # 结果屏自己是一层白面板（与日期屏同款）；条目与它的展开行不再单独刷白，白底由面板承担。
        white = panel_style(self.page, '#datePage')[0]
        self.assertEqual(self.page.eval_on_selector(
            '#results', "el=>getComputedStyle(el).backgroundColor"), white)
        for selector in ('#ranking > details', '#ranking > details > summary'):
            self.assertNotEqual(self.page.eval_on_selector(
                selector, "el=>getComputedStyle(el).backgroundColor"), white)


class ShellPanelConflictCountTests(unittest.TestCase):
    """冲突落回的组算「待确认」：票 07 的口径在票 02 的徽标上同样成立。"""

    setUp = conflicts.ConflictBrowserTests.setUp
    stop_server = conflicts.ConflictBrowserTests.stop_server
    submit = conflicts.ConflictBrowserTests.submit
    submit_product = conflicts.ConflictBrowserTests.submit_product
    add_product = conflicts.ConflictBrowserTests.add_product
    dates = conflicts.ConflictBrowserTests.dates
    seed_group = conflicts.ConflictBrowserTests.seed_group

    def test_conflicted_group_counts_as_pending_in_the_tab_badges(self):
        self.add_product('22', '云朵杯', 90, 60, color='blue')
        self.seed_group(machine='')
        self.service.store.merge_incoming(conflicts.ledger_of(excluded=[[conflicts.member('11'),
                                                                       conflicts.member('22')]]),
                                          source='m1', seen_at='2026-09-23T02:00:00+08:00')
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('其中 1 组待确认')
        self.assertEqual(tab_texts(self.page), ['待确认 1', '已确认 0', '全部 1'])
        # 一个动作裁决掉冲突：它才真正进已确认。
        self.page.get_by_role('button', name='确认当前分组', exact=True).click()
        expect(self.page.locator('#groupDetail .conflict')).to_have_count(0)
        self.assertEqual(tab_texts(self.page), ['待确认 0', '已确认 1', '全部 1'])


if __name__ == '__main__':
    unittest.main()
