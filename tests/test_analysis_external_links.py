"""票 01：商品外链点击后交系统默认浏览器打开（不再预开 about:blank）。

真页面加真本地服务；把「新窗口打开」换成可断言的替身——记下每次请求的地址。
四处入口（确认屏成员卡、组内新增商品搜索结果、结果屏排名行、同款构成树）共用
同一实现，这里逐个走一遍点击路径：先取地址、以真实地址发起打开请求、没有空窗。
"""
import re
import unittest

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect

import test_analysis as fixture
from helpers import submit_offer


class ExternalSourceLinkTests(unittest.TestCase):
    setUp = fixture.AnalysisBrowserTests.setUp
    stop_server = fixture.AnalysisBrowserTests.stop_server
    submit = fixture.AnalysisBrowserTests.submit   # setUp 靠它铺底（商品 11 的两天观测）
    dates = fixture.AnalysisBrowserTests.dates

    def seed_product(self, shop, offer, name, days):
        """一个商品的多日观测：days 为 [(日期, 库存), ...]，源地址指向自己的详情页。"""
        for day, stock in days:
            submit_offer(self.db, offer, day, stock, name=name, shop_key=shop,
                         shop_name=f'店铺{int(shop[1:])}', color='red')

    def set_url(self, offer, url):
        """库里这条商品的当前有效地址：页面只经 /api/source 读它。"""
        self.conn.execute("UPDATE products SET product_url=? WHERE offer_id=?", (url, offer))
        self.conn.commit()

    def review(self):
        self.dates()
        self.page.get_by_role('button', name='下一步、进入同款确认').click()
        expect(self.page.locator('#snapshotInfo')).to_contain_text('日期区间')
        return self.page.url.split('analysis=')[1]

    def join_group(self, sid, offer, target_offer):
        """把商品并进目标商品所在的组：走服务层的人工移动入口（浏览器不参与）。"""
        snapshot = self.service.get(sid)
        source = next(g for g in snapshot['groups'] if any(m['offer_id'] == offer for m in g['members']))
        target = next(g for g in snapshot['groups'] if any(m['offer_id'] == target_offer for m in g['members']))
        member = next(m for m in source['members'] if m['offer_id'] == offer)
        self.service.edit_group(sid, 'move', source['id'], member, target['id'])

    def spy_new_windows(self):
        """把「新窗口打开」换成替身：记下每次请求的地址与目标，不开真窗口。"""
        self.page.evaluate("()=>{window.__opened=[];"
                           " window.open=(url,target)=>{"
                           "window.__opened.push({url:String(url),target:String(target)});return null;}}")

    def opened(self):
        calls = self.page.evaluate("window.__opened||[]")
        self.assertFalse([call for call in calls if call['url'].startswith('about:')],
                         f'不得预开空窗，实际请求过：{calls}')
        return calls

    def click_and_check_request(self, link, offer):
        """点图标：先看到对 /api/source 的取地址请求，再断言这一次的打开请求。"""
        with self.page.expect_request(lambda request: f'/api/source?offer={offer}' in request.url):
            link.click()

    def wait_for_open(self, url):
        """window.open 在取数之后，比请求发出晚一拍：等它落下再对账。"""
        try:
            self.page.wait_for_function('url=>window.__opened.some(call=>call.url===url)',
                                        arg=url, timeout=5000)
        except PlaywrightTimeoutError:
            pass  # 没记到：交给下面的断言把「实际记录到什么」报出来

    def assert_opened_once(self, url):
        self.wait_for_open(url)
        self.assertEqual(self.opened(), [{'url': url, 'target': '_blank'}])

    def assert_opened_last(self, url):
        self.wait_for_open(url)
        self.assertEqual(self.opened()[-1], {'url': url, 'target': '_blank'})

    def test_member_card_opens_the_address_fetched_now_not_the_one_in_the_page(self):
        self.seed_product('A02', '222', '杯子乙', [('2026-09-07', 100), ('2026-09-14', 60)])
        self.review()
        self.spy_new_windows()
        link = self.page.locator('#groupDetail .matching-member').first.get_by_role('link', name='商品源地址')
        offer = link.get_attribute('data-source-offer')
        # 先让页面手上的地址跟住库里的一条，等刷新落地；再把库里改成另一条。
        # 点击时页面里留下的仍是上一条，只有真去取数才拿得到下一条。
        self.set_url(offer, f'https://detail.1688.com/offer/{offer}.html?seen=1')
        self.page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(link).to_have_attribute('href', f'https://detail.1688.com/offer/{offer}.html?seen=1')
        fresh = f'https://detail.1688.com/offer/{offer}.html?fresh=2'
        self.set_url(offer, fresh)
        # 点击前页面里留下的仍是上一条：这时点下去，只有真去取数才拿得到下面这条。
        expect(link).to_have_attribute('href', f'https://detail.1688.com/offer/{offer}.html?seen=1')

        self.click_and_check_request(link, offer)
        self.assert_opened_once(fresh)

    def test_search_result_icon_opens_its_own_products_address(self):
        self.seed_product('A02', '222', '杯子乙', [('2026-09-07', 100), ('2026-09-14', 60)])
        self.set_url('222', 'https://detail.1688.com/offer/222.html?latest=1')
        self.review()
        self.spy_new_windows()

        # 先选中不含「杯子乙」的那一组：组内新增商品的搜索结果只给不在当前组里的商品，
        # 而默认选中的是左列第一组（票 05 起按组内商品数/销量排，这里轮到「杯子乙」）。
        self.page.locator('.group-choice').filter(has_text='杯子 ·').click()
        self.page.get_by_role('button', name='组内新增商品').click()
        self.page.get_by_role('textbox', name='搜索商品').fill('杯子乙')
        item = self.page.locator('#addResults .search-item').filter(has_text='杯子乙')
        link = item.get_by_role('link', name='商品源地址')
        self.assertEqual(link.get_attribute('data-source-offer'), '222')

        self.click_and_check_request(link, '222')
        self.assert_opened_once('https://detail.1688.com/offer/222.html?latest=1')

    def test_ranking_row_and_tree_icons_open_each_products_own_address(self):
        self.seed_product('A02', '222', '杯子乙', [('2026-09-07', 100), ('2026-09-14', 60)])
        sid = self.review()
        self.join_group(sid, '222', '11')
        snapshot = self.service.get(sid)
        self.service.confirm_groups(sid, [g['id'] for g in snapshot['groups']])
        self.page.reload()
        self.page.get_by_role('button', name='保存分组并查看畅销品').click()
        expect(self.page.get_by_role('heading', name=re.compile('畅销品'))).to_be_visible()
        self.spy_new_windows()

        row = self.page.locator('#ranking > details').first
        summary_link = row.locator(':scope > summary').get_by_role('link', name='商品源地址')
        represent = summary_link.get_attribute('data-source-offer')
        self.click_and_check_request(summary_link, represent)
        self.assert_opened_once(f'https://detail.1688.com/offer/{represent}.html')
        expect(row).not_to_have_attribute('open', '')  # 点图标不连带展开这一行

        row.locator(':scope > summary').click()
        expect(row).to_have_attribute('open', '')
        expect(row.locator('.tree').get_by_role('link', name='商品源地址')).to_have_count(2)
        # 树里换一件商品点（代表之外的那件）：打开的地址要跟着这一件走。
        other = '11' if represent == '222' else '222'
        self.assertNotEqual(other, represent)
        tree_link = row.locator(f'.tree [data-source-offer="{other}"]')
        expect(tree_link).to_have_attribute('href', f'https://detail.1688.com/offer/{other}.html')
        self.click_and_check_request(tree_link, other)
        self.assert_opened_last(f'https://detail.1688.com/offer/{other}.html')

    def test_address_that_is_no_longer_valid_shows_the_reason_and_opens_nothing(self):
        self.review()
        self.spy_new_windows()
        link = self.page.locator('#groupDetail .matching-member').first.get_by_role('link', name='商品源地址')
        offer = link.get_attribute('data-source-offer')
        self.set_url(offer, '')  # 取数这一刻已经没有有效地址

        self.click_and_check_request(link, offer)
        expect(self.page.locator('#error')).to_have_text('暂无商品源地址')
        self.assertEqual(self.opened(), [])

    def test_icon_without_an_address_at_freeze_time_stays_disabled(self):
        self.set_url('11', '')
        self.review()
        detail = self.page.locator('#groupDetail')
        expect(detail.get_by_role('link', name='商品源地址')).to_have_count(0)
        expect(detail.locator('[data-source-offer]')).to_have_attribute('aria-disabled', 'true')
