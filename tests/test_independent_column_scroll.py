"""票 04：确认屏左右两栏各自定高、内部滚动（R03）。

真页面加真本地服务（沿用分组编辑夹具）。用例只钉看得见的行为：两栏能不能各滚各的、
点靠下的组要不要连整页一起滚、分页行在不在左列底部、换组后右列回不回顶、
窄屏还有没有内部定高（矮窗口下两栏仍要可用）。两栏高度按视口算，所以这些用例在宽视口下跑。
"""
import re
import unittest

from playwright.sync_api import expect

import test_group_editing as fixture
from helpers import new_round


class IndependentColumnScrollTests(unittest.TestCase):
    setUp = fixture.GroupEditingTests.setUp
    stop_server = fixture.GroupEditingTests.stop_server
    submit = fixture.GroupEditingTests.submit   # 继承来的 setUp 自己要用它铺底数据
    dates = fixture.GroupEditingTests.dates

    WIDE = {'width': 1440, 'height': 1200}   # 桌面窗口：两栏装得下，也拿得到足够的高度
    NARROW = {'width': 640, 'height': 900}   # 原型断点（800px）之下

    GROUPS = 34   # 每件商品先自成一組：拼完还剩 22 组，左列长过一屏、也够翻两页
    BIG = 8       # 一个大组拼到 8 件商品：右列长过一屏
    SECOND = 6    # 再拼一个大组：换组回顶要「高组换高组」才判别得出（短组会被浏览器钳回 0）
    OFFERS = [str(1000 + index) for index in range(GROUPS)]

    def seed_scroll_data(self):
        """GROUPS 件商品铺在 12 家店铺上（店铺表只预置了 A01–A12）。"""
        for index in range(self.GROUPS):
            shop = f'A{index % 12 + 1:02}'
            for day, stock in [('2026-09-07', 100), ('2026-09-14', 100 - index % 10)]:
                rid = new_round(self.db, shop, run_date=day)
                self.db.submit_inventory_snapshot(
                    round_id=rid, shop_key=shop, shop_url='https://shop.example',
                    shop_name=f'店铺{index % 12 + 1}', offer_id=self.OFFERS[index],
                    product_url=f'https://detail.1688.com/offer/{self.OFFERS[index]}.html',
                    list_title=f'滚动商品{index:02}', detail_title=f'滚动商品{index:02}',
                    main_image_url='', sku_rows=[dict(sku_id='one', sku_name='标准', sku_stock=stock)],
                    collected_at=day + 'T04:00:00+00:00', attempt=1)

    @staticmethod
    def group_for(snapshot, offer):
        return next(group for group in snapshot['groups']
                    if any(member['offer_id'] == offer for member in group['members']))

    def merge(self, sid, offers):
        """服务侧把这几个商品并进同一组（走人工移动入口）。"""
        for offer in offers[1:]:
            snapshot = self.service.get(sid)
            source = self.group_for(snapshot, offer)
            member = next(m for m in source['members'] if m['offer_id'] == offer)
            self.service.edit_group(sid, 'move', source['id'], member,
                                    self.group_for(snapshot, offers[0])['id'])

    def review_with_groups(self, viewport=None):
        """进确认屏，再把前 BIG＋SECOND 件拼成两个大组，返回分析编号。"""
        self.page.set_viewport_size(viewport or self.WIDE)
        self.seed_scroll_data()
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('日期区间')
        sid = self.page.url.split('analysis=')[1]
        self.merge(sid, self.OFFERS[:self.BIG])
        self.merge(sid, self.OFFERS[self.BIG:self.BIG + self.SECOND])
        self.page.reload()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('日期区间')
        return sid

    def scroll(self, selector, top):
        self.page.locator(selector).evaluate("(node, top) => { node.scrollTop = top; }", top)

    def scroll_top(self, selector):
        return self.page.locator(selector).evaluate("node => node.scrollTop")

    def overflow(self, selector):
        """这个元素自己还要滚多少：0 表示它不是自己的滚动容器。"""
        return self.page.locator(selector).evaluate("node => node.scrollHeight - node.clientHeight")

    def open_big_group(self):
        """点开拼出来的大组：右列换上 8 张成员卡。"""
        self.page.locator('.group-choice', has_text=f'{self.BIG} 个商品').first.click()
        expect(self.page.locator('#groupDetail .matching-member')).to_have_count(self.BIG)

    def open_second_group(self):
        """点开另一个大组：右列换上 6 张成员卡。"""
        self.page.locator('.group-choice', has_text=f'{self.SECOND} 个商品').first.click()
        expect(self.page.locator('#groupDetail .matching-member')).to_have_count(self.SECOND)

    def test_each_column_scrolls_on_its_own_and_leaves_the_rest_of_the_page_alone(self):
        self.review_with_groups()
        self.open_big_group()
        self.assertGreater(self.overflow('#products'), 0, '左列该是自己的滚动容器')
        self.assertGreater(self.overflow('#groupDetail'), 0, '右列该是自己的滚动容器')
        stats = self.page.locator('#snapshotInfo').bounding_box()
        save = self.page.get_by_role('button', name='暂时保存').bounding_box()

        self.scroll('#products', 200)
        self.assertEqual(self.scroll_top('#products'), 200)
        self.assertEqual(self.scroll_top('#groupDetail'), 0, '滚左列不该动右列')
        self.assertEqual(self.page.evaluate('window.scrollY'), 0, '滚左列不该动整页')

        self.scroll('#groupDetail', 200)
        self.assertEqual(self.scroll_top('#groupDetail'), 200)
        self.assertEqual(self.scroll_top('#products'), 200, '滚右列不该动左列')
        self.assertEqual(self.page.evaluate('window.scrollY'), 0, '滚右列不该动整页')

        self.assertAlmostEqual(self.page.locator('#snapshotInfo').bounding_box()['y'],
                               stats['y'], delta=0.5, msg='统计行留在文档流里')
        self.assertAlmostEqual(self.page.get_by_role('button', name='暂时保存').bounding_box()['y'],
                               save['y'], delta=0.5, msg='保存行留在文档流里')

    def test_a_group_near_the_bottom_opens_without_scrolling_the_page(self):
        self.review_with_groups()
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollHeight'),
                             self.page.evaluate('window.innerHeight') + 2,
                             '整页该装得下：两栏各自滚，页面本身不用滚')
        self.scroll('#products', 100000)   # 列表内部滚到底
        choice = self.page.locator('.group-choice').last
        chosen = choice.get_attribute('data-group')
        choice.click()
        expect(self.page.locator('#groupDetail h3')).to_have_text(
            re.compile(rf'^{re.escape(chosen)} · \d+ 个商品$'))
        self.assertEqual(self.page.evaluate('window.scrollY'), 0, '点靠下的组不该带动整页')
        top = self.page.locator('#groupDetail .matching-member').first.bounding_box()['y']
        self.assertGreaterEqual(top, 0, '该组商品该在视口里')
        self.assertLess(top, self.page.evaluate('window.innerHeight'))

    def test_the_pager_stays_at_the_bottom_of_the_left_column(self):
        self.review_with_groups()
        column = self.page.locator('#products').bounding_box()
        pager = self.page.locator('#pageInfo').bounding_box()
        self.assertLessEqual(pager['x'] + pager['width'], column['x'] + column['width'] + 1,
                             '分页行该在左列里，不该横跨整页')
        self.assertGreaterEqual(pager['y'], column['y'] + column['height'] - 1,
                                '分页行该在列表之外、左列底部')

        self.scroll('#products', 200)
        expect(self.page.get_by_role('button', name='下一页')).to_be_visible()
        self.assertAlmostEqual(self.page.locator('#pageInfo').bounding_box()['y'], pager['y'],
                               delta=0.5, msg='滚列表不该带走分页行')

        self.page.get_by_role('button', name='下一页').click()   # 翻到第 2 页也钉在原处
        expect(self.page.locator('#pageInfo')).to_have_text('2 / 2')
        self.assertAlmostEqual(self.page.locator('#pageInfo').bounding_box()['y'], pager['y'],
                               delta=0.5, msg='翻页后分页行还在左列底部')

    def test_a_short_window_keeps_both_columns_usable(self):
        # 窗口矮到整页装不下时（两栏有下限），两栏仍要各滚各的、分页行仍在左列底部。
        self.review_with_groups({'width': 1440, 'height': 820})
        self.open_big_group()
        self.assertGreater(self.overflow('#products'), 0, '矮窗口里左列还是自己的滚动容器')
        self.assertGreater(self.overflow('#groupDetail'), 0, '矮窗口里右列还是自己的滚动容器')
        self.scroll('#products', 120)
        self.assertEqual(self.scroll_top('#groupDetail'), 0, '矮窗口里滚左列也不该动右列')

        column = self.page.locator('#products').bounding_box()
        pager = self.page.locator('#pageInfo').bounding_box()
        self.assertLessEqual(pager['x'] + pager['width'], column['x'] + column['width'] + 1,
                             '矮窗口里分页行仍在左列里')
        self.assertGreaterEqual(pager['y'], column['y'] + column['height'] - 1,
                                '矮窗口里分页行仍在列表之外、左列底部')

    def test_switching_group_sends_the_detail_back_to_the_top(self):
        # 高组换高组：新组内容一样撑得满，浏览器不会替我们把滚动位置钳回顶部，
        # 回不回顶就只由页面自己的行为决定。
        self.review_with_groups()
        self.open_big_group()
        self.scroll('#groupDetail', 300)
        self.assertGreater(self.scroll_top('#groupDetail'), 0)

        second = self.page.locator('.group-choice', has_text=f'{self.SECOND} 个商品').first
        chosen = second.get_attribute('data-group')
        second.click()
        expect(self.page.locator('#groupDetail h3')).to_have_text(
            re.compile(rf'^{re.escape(chosen)} · {self.SECOND} 个商品$'))
        self.assertEqual(self.scroll_top('#groupDetail'), 0, '换组后右列该回顶')

    def test_narrow_screen_stacks_the_columns_and_drops_the_fixed_height(self):
        self.review_with_groups()
        self.assertGreater(self.overflow('#products'), 0, '宽视口下左列自己滚')

        self.page.set_viewport_size(self.NARROW)
        self.assertEqual(self.overflow('#products'), 0, '窄屏回到整页一条滚动：左列不再自己滚')
        self.assertEqual(self.overflow('#groupDetail'), 0, '窄屏回到整页一条滚动：右列不再自己滚')
        upper = self.page.locator('#products').bounding_box()
        lower = self.page.locator('#groupDetail').bounding_box()
        self.assertAlmostEqual(upper['x'], lower['x'], delta=1, msg='窄屏单栏堆叠')
        self.assertGreater(lower['y'], upper['y'], msg='右栏该堆在左栏下面')
        self.assertGreater(self.page.evaluate('document.documentElement.scrollHeight'),
                           self.page.evaluate('window.innerHeight'), '窄屏整页该能滚')


if __name__ == '__main__':
    unittest.main()
