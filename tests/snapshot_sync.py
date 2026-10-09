"""Issue 07 测试侧同步接缝：等页面应用初始快照；保存后一次刷新并做 S/R/U 三层核对。

用例按 issue 07 规格 D1 直接调后台代办并组、确认、保存；保存成功后用
`sync_after_save` 把页面同步到保存后状态。这里只有测试侧的两件事：

- `wait_initial_presentation`：等到页面真正呈现本次快照（不是只等 hash 或标题）。
  地址 hash 在遮罩入口就出现、进度 ready 也只是运行终态，都不能当快照已应用的
  依据；完成条件是页面上已经展示对应的分组内容（spec §2）。
- `sync_after_save`：核对保存返回（S），主动刷新同一分析恰一次并捕获该次导航
  产生的同分析 GET（R），再核对页面呈现（U）。它不代办保存、不循环刷新、不盲目
  重试、不固定 sleep；失败按阶段抛 `SyncFailure`（S/R/U 呈现/U 落屏），消息带
  分析号、区间、保存标记、成员/确认摘要、当前可见屏与响应状态（AC16）。
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect

__all__ = ['SyncFailure', 'wait_initial_presentation', 'sync_after_save']

DEFAULT_TIMEOUT_MS = 10000


class SyncFailure(AssertionError):
    """保存后同步没成立：`phase` 指出卡在哪一层（S 保存 / R 读取 / U 呈现 / U 落屏）。"""

    def __init__(self, phase: str, message: str):
        super().__init__(f'{phase}：{message}')
        self.phase = phase


def _screen_state(page):
    """页面的可见屏、遮罩与所持快照摘要（等待条件与失败诊断共用，只读页面自身状态）。"""
    return page.evaluate(
        "() => ({"
        " hash: location.hash,"
        " screen: document.getElementById('datePage').hidden"
        "   ? (document.getElementById('reviewPage').hidden ? '结果' : '确认')"
        "   : '日期',"
        " mask: document.getElementById('matchMask').open,"
        " tally: (document.querySelector('#snapshotInfo .snapshot-tally') || {textContent: ''}).textContent,"
        " held: currentSnapshot ? {"
        "   id: currentSnapshot.id,"
        "   dirty: currentSnapshot.dirty,"
        "   saved_at: currentSnapshot.saved_at || null,"
        "   confirmed: currentSnapshot.groups.map(g => groupConfirmed(g)),"
        "   result_gate: resultsReady(currentSnapshot),"
        " } : null,"
        "})")


def _state_brief(state):
    held = state.get('held') or {}
    return (f"当前可见屏={state.get('screen')}（遮罩{'未关' if state.get('mask') else '已关'}）；"
            f"列表计数={state.get('tally') or '未呈现'}；"
            f"页面持有的快照 id={held.get('id')} dirty={held.get('dirty')} "
            f"confirmed={held.get('confirmed')} 结果页门禁={held.get('result_gate')}")


def wait_initial_presentation(page, *, products, groups, pending, timeout_ms=15000):
    """等页面真正应用本次初始快照并呈现初始分组，返回地址里的分析号。

    完成条件＝等待遮罩已关 + 快照信息条给出「products 个商品 · groups 组同款
    （其中 pending 组待确认）」——即页面已呈现对应的分组内容。hash 出现、进度
    ready、任意标题可见都不算（spec §2）。
    """
    tally = page.locator('#snapshotInfo .snapshot-tally')
    try:
        expect(page.locator('#matchMask')).to_be_hidden(timeout=timeout_ms)
        expect(tally).to_contain_text(f'{products} 个商品')
        expect(tally).to_contain_text(f'{groups} 组同款（其中 {pending} 组待确认）')
    except AssertionError as exc:
        raise SyncFailure('初始呈现', f'页面未呈现初始快照（期望 {products} 个商品、{groups} 组同款、'
                                      f'{pending} 组待确认）：{_state_brief(_screen_state(page))}') from exc
    return page.url.split('analysis=')[1]


def _member_key(member):
    """成员的稳定身份：商品级取店铺+商品号，SKU 级再带平台 SKU 号。"""
    return (member.get('shop_key'), member.get('offer_id'), member.get('sku_id'))


def _groups_brief(snapshot):
    parts = []
    for group in snapshot.get('groups') or []:
        keys = sorted(map(_member_key, group.get('members') or []), key=str)
        members = '、'.join(f'{key[0]}/{key[1]}' for key in keys)
        parts.append(f"{group.get('id')}[{members}]（confirmed={bool(group.get('confirmed'))}，"
                     f"销量={group.get('sales')}）")
    return '；'.join(parts) or '无分组'


def _snapshot_brief(snapshot):
    return (f"分析 {snapshot.get('id')}（{snapshot.get('start')} — {snapshot.get('end')}）"
            f"dirty={snapshot.get('dirty')} saved_at={snapshot.get('saved_at')}；"
            f'分组：{_groups_brief(snapshot)}')


def _js_number(value):
    """页面把销量直接插进文本（`${group.sales}`）：整数别带出浮点的 `.0`。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _saved_expectation(saved, analysis_id):
    """S 层：保存返回本身就是这次同步的独立预期（后面的读取只来对，不反过来覆盖它）。"""
    problems = []
    if saved.get('id') != analysis_id:
        problems.append(f"分析身份不符（返回 {saved.get('id')}，期望 {analysis_id}）")
    if not (saved.get('start') and saved.get('end')):
        problems.append('缺少日期区间')
    if saved.get('dirty') is not False:
        problems.append(f"仍是未保存状态（dirty={saved.get('dirty')}）")
    if not saved.get('saved_at'):
        problems.append('缺少保存时间 saved_at（没有保存完成信号）')
    if not saved.get('groups'):
        problems.append('没有分组')
    if problems:
        raise SyncFailure('S(保存)', f"保存返回不是一次完成的保存：{'；'.join(problems)}；"
                                     f'{_snapshot_brief(saved)}')
    return saved


def _read_mismatches(fetched, expected):
    problems = []
    if fetched.get('id') != expected['id']:
        problems.append(f"分析身份不符（读到 {fetched.get('id')}，期望 {expected['id']}）")
    if (fetched.get('start'), fetched.get('end')) != (expected['start'], expected['end']):
        problems.append(f"区间不符（读到 {fetched.get('start')} — {fetched.get('end')}，"
                        f"期望 {expected['start']} — {expected['end']}）")
    if fetched.get('dirty') is not False:
        problems.append(f"读到的是未保存状态（dirty={fetched.get('dirty')}）")
    if not fetched.get('saved_at') or fetched.get('saved_at') != expected['saved_at']:
        problems.append(f"保存标记不符（读到 saved_at={fetched.get('saved_at')}，"
                        f"期望 {expected['saved_at']}）")
    expected_groups = {g['id']: g for g in expected.get('groups') or []}
    fetched_groups = {g['id']: g for g in fetched.get('groups') or []}
    if set(fetched_groups) != set(expected_groups):
        problems.append(f"分组集合不符（读到 {sorted(fetched_groups)}，期望 {sorted(expected_groups)}）")
    else:
        for gid, want in expected_groups.items():
            got = fetched_groups[gid]
            got_members = sorted(map(_member_key, got.get('members') or []), key=str)
            want_members = sorted(map(_member_key, want.get('members') or []), key=str)
            if got_members != want_members:
                problems.append(f'{gid} 成员不符（读到 {got_members}，期望 {want_members}）')
            if bool(got.get('confirmed')) != bool(want.get('confirmed')):
                problems.append(f"{gid} 确认状态不符（读到 {bool(got.get('confirmed'))}，"
                                f"期望 {bool(want.get('confirmed'))}）")
            if got.get('sales') != want.get('sales'):
                problems.append(f"{gid} 销量不符（读到 {got.get('sales')}，期望 {want.get('sales')}）")
    return problems


def sync_after_save(page, analysis_id, saved, *, timeout_ms=DEFAULT_TIMEOUT_MS):
    """保存（S）→ 刷新同一分析恰一次并捕获该次导航的同分析 GET（R）→ 页面呈现（U）。

    `saved` 是 save_draft／save_and_view 的成功返回，作为独立预期；本函数只刷新一次、
    不做别的页面操作，失败按阶段抛 `SyncFailure`。返回 R 读到的快照正文，供用例继续核对。

    页面文案按商品级快照核对（快照信息条「N 个商品 · G 组同款」）；本 issue 的两个
    场景都是商品级分析，SKU 级快照的文案不同，需要时另补。
    """
    expected = _saved_expectation(saved, analysis_id)
    origin = '{0.scheme}://{0.netloc}'.format(urlsplit(page.url))
    captured = []

    def remember(response):
        if response.request.method != 'GET':
            return
        url = urlsplit(response.url)
        if url.path != '/api/analysis' or parse_qs(url.query).get('id') != [analysis_id]:
            return
        if f'{url.scheme}://{url.netloc}' != origin:
            return
        captured.append(response)

    # 监听先于刷新建立：只认这一次导航产生的同源同分析 GET（spec §3）。
    page.on('response', remember)
    try:
        page.reload()
        deadline = time.monotonic() + timeout_ms / 1000
        while not captured:
            if time.monotonic() >= deadline:
                raise SyncFailure('R(读取)', f'刷新后 {timeout_ms}ms 内没有捕获到同分析快照 GET；'
                                             f'{_snapshot_brief(expected)}；'
                                             f'{_state_brief(_screen_state(page))}')
            page.wait_for_timeout(25)
        response = captured[0]
        if response.status != 200:
            raise SyncFailure('R(读取)', f'刷新读取的状态码不是 200（实际 {response.status}）；'
                                         f'{_snapshot_brief(expected)}')
        try:
            fetched = response.json()
        except Exception as exc:
            raise SyncFailure('R(读取)', f'刷新读取的正文不是可解析的 JSON'
                                         f'（状态码 {response.status}）；{_snapshot_brief(expected)}') from exc
        problems = _read_mismatches(fetched, expected)
        if problems:
            raise SyncFailure('R(读取)', f"刷新读到的内容与保存返回不一致：{'；'.join(problems)}；"
                                         f'读到：{_snapshot_brief(fetched)}')

        # U：新文档呈现。先等这份快照在页面上出现，再等结果屏；两段分开报阶段（AC16）。
        tally = page.locator('#snapshotInfo .snapshot-tally')
        try:
            expect(tally).to_contain_text(f"{len(fetched.get('products') or [])} 个商品", timeout=timeout_ms)
            expect(tally).to_contain_text(f"{len(fetched['groups'])} 组同款", timeout=timeout_ms)
        except AssertionError as exc:
            raise SyncFailure('U(呈现)', f'新文档没有呈现刷新读到的快照；{_snapshot_brief(fetched)}；'
                                         f'{_state_brief(_screen_state(page))}') from exc
        try:
            expect(page.get_by_role('heading', name='畅销品', exact=True)).to_be_visible(timeout=timeout_ms)
        except AssertionError as exc:
            raise SyncFailure('U(落屏)', '快照已呈现但「畅销品」结果页没有打开'
                                         f'（门禁未通过或落屏未发生）；{_snapshot_brief(fetched)}；'
                                         f'{_state_brief(_screen_state(page))}') from exc
        # 结果屏内容与 R 对应：默认只排有销量的组、每屏 20 组（GROUPS_PER_PAGE），核第一屏。
        groups_by_id = {g['id']: g for g in fetched['groups']}
        ranking = fetched.get('ranking') or [g['id'] for g in fetched['groups']]
        for gid in [gid for gid in ranking
                    if (groups_by_id.get(gid) or {}).get('sales', 0) > 0][:20]:
            row = page.locator(f'.rank[data-group="{gid}"]')
            try:
                expect(row).to_have_count(1)
                expect(row.locator('.number')).to_contain_text(_js_number(groups_by_id[gid]['sales']))
            except AssertionError as exc:
                raise SyncFailure('U(呈现)', f'结果屏没有按刷新读到的快照列出 {gid}'
                                             f"（期望销量 {groups_by_id[gid]['sales']}）；"
                                             f'{_snapshot_brief(fetched)}') from exc
        if len(captured) != 1:
            raise SyncFailure('R(读取)', f'本次刷新出现了 {len(captured)} 个同分析快照 GET'
                                         f'（期望恰 1 个）；{_snapshot_brief(expected)}')
        return fetched
    finally:
        page.remove_listener('response', remember)
