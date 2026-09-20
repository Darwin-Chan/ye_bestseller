"""Manual membership and exclusions belong to the current analysis draft."""
from .matching import identity
from uuid import uuid4


def edit_group(snapshot, action, group_id, member, target_id, matcher):
    source = next((g for g in snapshot['groups'] if g['id'] == group_id), None)
    if source is None or member not in source['members']:
        raise ValueError('商品已不在当前组，请刷新后重试')
    exclusions = {frozenset(pair) for pair in snapshot.get('excluded', [])}
    key = identity(member)
    if action == 'move':
        target = next((g for g in snapshot['groups'] if g['id'] == target_id), None)
        if target is None or target is source:
            raise ValueError('请选择其他有效目标组')
        # An explicit human join supersedes only conflicting target relationships.
        exclusions -= {frozenset((key, identity(m))) for m in target['members']}
    elif action == 'remove':
        if len(source['members']) == 1:
            raise ValueError('单商品组不能移除唯一商品')
        exclusions.update(frozenset((key, identity(m))) for m in source['members'] if m != member)
        target = {'id': 'G'+uuid4().hex[:12], 'confirmed': False, 'members': [], 'sales': 0}
        snapshot['groups'].append(target)
    else:
        raise ValueError('不支持的分组操作')
    source['members'].remove(member)
    source['adjusted'] = True
    target['members'].append(member)
    target['adjusted'] = True
    if not source['members']:
        snapshot['groups'].remove(source)
    snapshot['excluded'] = sorted(sorted(pair) for pair in exclusions)
    refresh(snapshot, matcher)
    if action == 'remove':
        product = next(p for p in snapshot['products'] if identity(p) == key)
        choices = [g for g in snapshot['groups'] if g['id'] in product['candidate_groups']
                   and g is not target and not g['confirmed']]
        if len(choices) == 1:
            choices[0]['members'].append(member)
            choices[0]['adjusted'] = True
            snapshot['groups'].remove(target)
            refresh(snapshot, matcher)
    snapshot['dirty'] = True


def refresh(snapshot, matcher):
    products = {identity(p): p for p in snapshot['products']}
    for group in snapshot['groups']:
        group['members'].sort(key=lambda m: (-products[identity(m)]['sales'], identity(m)))
        group['sales'] = sum(products[identity(m)]['sales'] for m in group['members'])
    if matcher:
        matcher.refresh_candidates(snapshot['products'], snapshot['groups'], snapshot.get('excluded', ()))
    else:
        for p in snapshot['products']:
            group = next(g for g in snapshot['groups'] if any(identity(m) == identity(p) for m in g['members']))
            p['candidate_groups'] = [group['id']] if len(group['members']) > 1 else []
            p['match_label'] = '匹配唯一同款' if p['candidate_groups'] else '暂无匹配同款'
