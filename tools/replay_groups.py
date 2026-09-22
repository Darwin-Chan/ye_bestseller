"""同款分组回放器（票 20）：判断缓存上的离线复算工具——一次运行、零模型调用。

用途：① 12 店全量数据到位后定权重与复算标定（ADR-0040 挂账③）；② 给票 18／19 的验收判据出数。

用法：

    python tools/replay_groups.py --cache F:/AI/bestseller_runtime/data/matching.sqlite \\
        --config config/analysis.toml --weights 1,1.5,2,3,4 --json .scratch/tool-output/replay.json

商品清单＝缓存里最后一次 `recommendations` 的那批商品与顺序（程序当时的清单），证据按
（身份, 版本）从 `evidence` 取。**召回、读边、装配都 import 生产实现**（`matching.scored_pairs`
／`_judged_edges`／`assemble_groups`），同源、不复刻——回放出来的数就是重跑一遍分析会得的数。

只读承诺：**判断缓存只读打开**（`?mode=ro`；打不开时拷一份临时副本、在副本上恢复后读，原库
一个字节不动），草稿库走生产的读取路径（`DraftStore.ledger()`——老形状的库会在它上面按打开
路径补列，那是生产自己的迁移）；**不发任何模型请求、不回写判断缓存**。

退役的团装配（完全图）只是本工具内的**对照实现**：2026-09-23 退役（票 19 `ffc6e47` 起同款装配
换成带约束的相关聚类），逐行抄自当年的一次性标定脚本 `.scratch/tool-output/e2_measure.py`
（该脚本已被本工具替代、不再是证据入口）。留它是为了让报告出「现状对照」那一行——E2 标定
实测里的 558 组／最大组 5 就是它的数。

退出码：0 = 出了报告；1 = 输入不可用（缓存/草稿库不存在、读不了、没有可回放的商品清单）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from bestseller_monitor.analysis import AnalysisConfig, reuse_decisions                     # noqa: E402
from bestseller_monitor.analysis_store import DraftStore                                     # noqa: E402
from bestseller_monitor.matching import (NEGATIVE_WEIGHT, MatchingConfig, _judged_edges,      # noqa: E402
                                         assemble_groups, digest, identity, scored_pairs,
                                         signature_of, version)

# `_judged_edges` 名带下划线，但它是生产里唯一「本机署名下正负边一次读全」的那口读
# （票 19 为装配而设）——回放要的正是它，照 import 不重写。

# 缺省配置值从生产配置类取，不另抄一份（`MatchingConfig` 的缓存路径只作占位，本工具不用它）。
_DEFAULTS = MatchingConfig(Path('.'))
# 扫描档位：1 是 E2 的「收益端」（最大组 7／覆盖 94.1%／纯度 98.1%），发行值取自
# `NEGATIVE_WEIGHT`，4 及以上是「退化成团装配」那一端。
DEFAULT_WEIGHTS = (1.0, 1.5, NEGATIVE_WEIGHT, 3.0, 4.0)
DEFAULT_MIN_SCORE = _DEFAULTS.min_score
DEFAULT_CANDIDATES = _DEFAULTS.candidates
CLIQUE_KEY = 'clique'                                        # 报告里的键（与权重档位并列）
CLIQUE_CAPTION = '团装配（2026-09-23 退役，只作对照）'        # 控制台上那一行的抬头
CLIQUE_RETIRED = '2026-09-23（票 19：同款装配换成带约束的相关聚类）'
# 高把握线：低把握＝把握不到它（含判非同款的那些，E2 标定实测的 23 对就是这么数的）。
# 生产里同一条线写死在两处（`matching._judged_edges` 分正负边、`_suggest` 定 `STATUS_LOW`），
# 没有具名常量可用；本工具只在这里数一个数，不参与判定。
LOW_CONFIDENCE = .8


def weights_of(text):
    """`--weights 1,1.5,2` 的解析：逗号分隔的非负数＝装配的负边分歧权重。"""
    values = []
    for part in str(text).replace('，', ',').split(','):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            raise argparse.ArgumentTypeError('权重得是数字：%s' % part) from None
        if value < 0:
            raise argparse.ArgumentTypeError('权重不能为负：%s' % part)
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError('至少要有一个权重')
    return tuple(values)


def _open_direct(path):
    """只读 URI 打开原库：第一条查询就会撞上「读不了」的库（热日志要回滚、别的进程占着写锁）。"""
    conn = sqlite3.connect('file:' + Path(path).as_posix() + '?mode=ro', uri=True, timeout=30)
    conn.execute('SELECT count(*) FROM sqlite_master')
    return conn


def open_readonly(path):
    """只读打开一份 sqlite，返回 (连接, 说明)。

    只读 URI 优先——**原库一个字节都不动**，这是回放的硬承诺。打开不了（库上有热日志要
    回滚、别的进程正占着写锁——2026-09-22 夜那次标定就撞上过）时拷一份临时副本，在**副本**
    上打开让 sqlite 自己恢复；副本是临时文件、留着给人看，说明里写出它落在哪。
    """
    try:
        return _open_direct(path), ''
    except sqlite3.OperationalError as exc:
        directory = Path(tempfile.mkdtemp(prefix='replay-cache-'))
        for suffix in ('', '-journal', '-wal', '-shm'):
            source = Path(str(path) + suffix)
            if source.exists():
                shutil.copy2(source, directory / (Path(path).name + suffix))
        conn = sqlite3.connect(directory / Path(path).name, timeout=30)
        conn.execute('SELECT count(*) FROM sqlite_master')
        return conn, '只读打开失败（%s），已拷临时副本到 %s' % (exc, directory)


def load_universe(conn):
    """商品清单＝最后一次 `recommendations` 的那批商品（顺序即程序当时的顺序）。

    证据按 (身份, 版本) 取——payload 记了每件商品的版本，`evidence` 表按同一个键存名称与
    图片。**缺证据行的商品如实计数、不当失败**：特征复算不出来，本次按「没有召回 token 的
    独立商品」参与（进装配、不进任何召回对）。版本不符另行计数：证据行与 payload 对不上时
    边就接不到这件商品上，值得报出来。
    """
    row = conn.execute('SELECT payload FROM recommendations ORDER BY rowid DESC LIMIT 1').fetchone()
    if row is None:
        raise ValueError('缓存里没有 recommendations：这份缓存没跑过分析，没有可回放的商品清单')
    payload = json.loads(row[0])
    evidence = {}
    for ident, ver, name, image_hash, image_data in conn.execute(
            'SELECT identity,version,name,image_hash,image_data FROM evidence'):
        evidence[(ident, ver)] = (name, image_hash, image_data)
    products, missing, mismatched = [], [], 0
    for entry in payload['products']:
        name, image_hash, image_data = evidence.get((entry['identity'], entry['version']),
                                                    (None, None, None))
        if name is None and image_data is None:
            missing.append(entry['identity'])
        shop_key, offer_id = json.loads(entry['identity'])
        product = {'shop_key': shop_key, 'offer_id': offer_id, 'sales': 0,
                   'product_name': name or '', 'image_hash': image_hash, 'image_data': image_data}
        if image_hash is not None and version(product) != entry['version']:
            mismatched += 1
        products.append(product)
    return products, payload['groups'], missing, mismatched


def input_groups(products, payload_groups, store, machine):
    """装配的硬约束侧：输入分组与排除对，返回 (groups, excluded)。

    给了 `--store` 就读真账本：走生产的 `DraftStore.ledger()` 与 `analysis.reuse_decisions`
    ——「账本怎么变成输入分组」只由生产实现说，这里不复刻（含版本不适用、撤回保留成员等规则）。
    没给就按 payload 的已确认／已整理组推导：它们在当时那次运行的输出里就是冻结组，其余商品
    各自成单商品组（排除对只有账本里有，这条路上为空）。
    """
    if store is not None:
        decisions = DraftStore(Path(store), machine).ledger()
        snapshot = {'products': products}
        reuse_decisions(snapshot, decisions)
        return snapshot['groups'], snapshot['excluded']
    frozen = [group for group in payload_groups if group['confirmed'] or group.get('adjusted')]
    frozen_ids = {group['id'] for group in frozen}
    frozen_members = {identity(member) for group in frozen for member in group['members']}
    groups, serial = [], 0
    for group in frozen:
        copy = {'id': group['id'], 'confirmed': group['confirmed'],
                'members': [{'shop_key': member['shop_key'], 'offer_id': member['offer_id']}
                            for member in group['members']]}
        if group.get('adjusted'):
            copy['adjusted'] = True
        groups.append(copy)
    for p in products:
        if identity(p) in frozen_members:
            continue
        while 'S%d' % serial in frozen_ids:
            serial += 1
        groups.append({'id': 'S%d' % serial, 'confirmed': False,
                       'members': [{'shop_key': p['shop_key'], 'offer_id': p['offer_id']}]})
        serial += 1
    return groups, []


def clique_assembly(products, positive):
    """退役的团装配（完全图）——**只作对照实现、不参与生产**，返回按商品下标的分组。

    组＝成员两两有正判断边的完全图：没判过的对与判否的对一样挡人，团上限把「有正边但没
    判全」的商品挡在组外。2026-09-23 退役（票 19 `ffc6e47`：换成 `matching.assemble_groups`
    的带约束相关聚类，缺边当未知）。逐行抄自当年的一次性标定脚本 `.scratch/tool-output/
    e2_measure.py`（历史证据，已不维护）——照抄是为了让报告里的「现状对照」那一行与 E2
    标定实测（558 组／最大组 5／覆盖 89.8%）逐项同源。
    """
    fingerprints = {i: version(p) for i, p in enumerate(products)}
    known = set(positive)
    neighbors = defaultdict(set)
    for relation in known:
        for fingerprint in relation:
            neighbors[fingerprint].update(relation)

    def matches(i, j):
        return i != j and frozenset((fingerprints[i], fingerprints[j])) in known

    groups_by_version = defaultdict(dict)
    output = []
    order = {}
    for i in range(len(products)):
        options = {id(g): g for f in neighbors[fingerprints[i]]
                   for g in groups_by_version[f].values()}
        target = None
        for g in sorted(options.values(), key=lambda g: order[id(g)]):
            if all(matches(i, member) for member in g):
                target = g
                break
        if target is None:
            target = []
            output.append(target)
            order[id(target)] = len(order)
        target.append(i)
        groups_by_version[fingerprints[i]][id(target)] = target
    return output


def group_metrics(groups, positive_pairs, negative_pairs):
    """一份分组的指标，与 E2 标定实测同口径：**按命中判断（版本对）展开到本次召回的商品对**算，
    每个判断行对应它召回命中上的那一对商品——覆盖＝组内正边／全部正边，纯度＝组内正边／组内
    正负边。ρ、分数曲线、下限与这里的覆盖／纯度用的是同一批对，报告里的数彼此对得上。

    同名同图的两件商品（同一个版本）之间，一个判断行覆盖多对商品；E2 标定实测按命中上的那
    一对算（不做版本展开），本工具照它的口径，好让「与 E2 逐项对上」这条验收成立。
    """
    where = {identity(member): index for index, group in enumerate(groups)
             for member in group['members']}
    inside_positive = sum(1 for a, b in positive_pairs if a in where and where[a] == where[b])
    inside_negative = sum(1 for a, b in negative_pairs if a in where and where[a] == where[b])
    sizes = Counter(len(group['members']) for group in groups if len(group['members']) >= 2)
    return {'groups': len(groups),
            'groups_ge2': sum(sizes.values()),
            'largest': max((len(group['members']) for group in groups), default=0),
            'size_hist': {str(size): count for size, count in sorted(sizes.items())},
            'members_in_ge2': sum(size * count for size, count in sizes.items()),
            'positive_inside': inside_positive,
            'negative_inside': inside_negative,
            'coverage': round(inside_positive / len(positive_pairs), 4) if positive_pairs else None,
            'purity': round(inside_positive / (inside_positive + inside_negative), 4)
                      if (inside_positive + inside_negative) else None}


def compare_with_payload(groups, payload_groups):
    """与 payload 实际分组的逐组对照：按成员集合数相同组数，两侧各有几组不在对方里。"""
    replay = Counter(frozenset(identity(member) for member in group['members']) for group in groups)
    payload = Counter(frozenset(identity(member) for member in group['members'])
                      for group in payload_groups)
    return {'identical': sum((replay & payload).values()),
            'replay_groups': sum(replay.values()), 'payload_groups': sum(payload.values()),
            'replay_only': sum((replay - payload).values()),
            'payload_only': sum((payload - replay).values())}


def metric_row(groups, positive_pairs, negative_pairs, payload_groups, seconds=None):
    """一行指标：分组本体（partition）＋指标＋与 payload 的对照＋耗时。"""
    row = dict(group_metrics(groups, positive_pairs, negative_pairs))
    row['partition'] = groups
    row['payload_compare'] = compare_with_payload(groups, payload_groups)
    if seconds is not None:
        row['seconds'] = round(seconds, 2)
    return row


def replay(cache, *, store=None, config=None, weights=DEFAULT_WEIGHTS, min_score=None):
    """在判断缓存上复算一遍，返回报告（打印与落盘是 `main` 的事）。

    署名与配置值：给了 `--config` 就用本机的（署名＝当前配置的署名四件，`candidates`／
    `min_score` 读配置）；没给就取缓存里行数最多的署名并在报告里注明用的哪个，配置值走缺省。
    """
    started = time.monotonic()
    cache = Path(cache)
    analysis = AnalysisConfig.from_file(Path(config)) if config is not None else None
    settings = analysis.matching if analysis is not None else None
    if store is not None and not Path(store).exists():
        raise ValueError('草稿库不存在：%s' % store)
    conn, note = open_readonly(cache)
    try:
        products, payload_groups, missing, mismatched = load_universe(conn)
        counts = Counter(row[0] for row in conn.execute('SELECT signature FROM judgments'))
        if settings is not None:
            signature, source = signature_of(settings), 'config'
        else:
            if not counts:
                raise ValueError('缓存里一条判断都没有：没有可复算的判定边')
            signature, source = counts.most_common(1)[0][0], 'cache'
        candidates = settings.candidates if settings is not None else DEFAULT_CANDIDATES
        floor = min_score if min_score is not None else (
            settings.min_score if settings is not None else DEFAULT_MIN_SCORE)

        scores = scored_pairs(products, candidates)
        positive, negative = _judged_edges(conn, signature)
        # 判断表的主键是（版本对, 署名）——认一条判断就用同一个摘要，式样与 `_suggest` 里
        # 那一处内联相同（那是生产唯一铸法，没有可 import 的函数）。
        recalled = {digest(sorted([version(products[i]), version(products[j])])): (i, j)
                    for (i, j) in scores}
        matched = {}
        curve = defaultdict(lambda: [0, 0])
        low, outside = 0, 0
        for pair, first, second, raw in conn.execute(
                'SELECT pair,evidence_a,evidence_b,result FROM judgments WHERE signature=?',
                (signature,)):
            row = recalled.get(pair)
            if row is None:
                outside += 1                 # 署名对、但不是本次召回的对（别的区间/别的清单）
                continue
            result = json.loads(raw)
            inside = frozenset((first, second)) in positive
            matched[pair] = (row[0], row[1], scores[row], inside)
            curve[scores[row]][0] += 1
            curve[scores[row]][1] += inside
            if float(result['confidence']) < LOW_CONFIDENCE:
                low += 1
        judged = len(matched)
        positives = sum(1 for _, _, _, inside in matched.values() if inside)
        # 命中判断展开到商品对（每个判断行对应它召回命中上的那一对）：指标、分数曲线与
        # 下限分析都用这批对，与 E2 标定实测同口径。
        positive_pairs = {(identity(products[i]), identity(products[j]))
                          for i, j, _, inside in matched.values() if inside}
        negative_pairs = {(identity(products[i]), identity(products[j]))
                          for i, j, _, inside in matched.values() if not inside}
        blocked = [row for row in matched.values() if row[2] < floor]
        blocked_positive = sum(1 for row in blocked if row[3])

        groups, excluded = input_groups(products, payload_groups, store,
                                        analysis.machine if analysis is not None else '')
        assemblies = {}
        started_at = time.monotonic()
        clique_groups = [{'id': 'C%d' % index, 'confirmed': False,
                          'members': [{'shop_key': products[i]['shop_key'],
                                       'offer_id': products[i]['offer_id']} for i in row]}
                         for index, row in enumerate(clique_assembly(products, positive))]
        row = metric_row(clique_groups, positive_pairs, negative_pairs, payload_groups,
                         time.monotonic() - started_at)
        row['retired'] = CLIQUE_RETIRED
        assemblies[CLIQUE_KEY] = row
        for weight in weights:
            started_at = time.monotonic()
            groups_of_row = assemble_groups(products, groups, positive, negative, excluded,
                                            weight=weight)
            row = metric_row(groups_of_row, positive_pairs, negative_pairs, payload_groups,
                             time.monotonic() - started_at)
            row['weight'] = weight
            assemblies['w%s' % weight] = row
        report = {'cache': str(cache), 'store': str(store) if store else None,
                  'config': str(config) if config else None, 'note': note,
                  'signature': signature, 'signature_source': source,
                  'signature_rows': counts.get(signature, 0), 'signatures': len(counts),
                  'candidates': candidates, 'min_score': floor, 'weights': list(weights),
                  'products': len(products), 'evidence_missing': missing,
                  'version_mismatch': mismatched, 'payload_groups': len(payload_groups),
                  'payload_frozen_groups': sum(1 for group in payload_groups
                                               if group['confirmed'] or group.get('adjusted')),
                  'input_groups': len(groups), 'frozen_groups': sum(
                      1 for group in groups if group['confirmed'] or group.get('adjusted')),
                  'excluded_pairs': len(excluded),
                  # 召回按商品对数（与 E2 标定实测的 3803 同口径：同名同图的商品对各自算一对）；
                  # 判定与 ρ 按判断行（版本对）——两者在重图商品上会差几对，生产自己的收尾行
                  # 「召回 N 对」数的是去重后的版本对。验收对的是 E2，故照 E2 的口径。
                  'recall': len(scores), 'judged': judged, 'unjudged': len(scores) - judged,
                  'outside_recall': outside, 'positive': positives,
                  'negative': judged - positives, 'low_confidence': low,
                  'rho': positives / judged if judged else None,
                  'score_curve': {str(score): {'pairs': counts_, 'positive': same,
                                                'rate': round(same / counts_, 3)}
                                  for score, (counts_, same) in sorted(curve.items())},
                  'floor': {'min_score': floor, 'blocked': len(blocked),
                            'blocked_ratio': len(blocked) / judged if judged else None,
                            'blocked_positive': blocked_positive,
                            'blocked_positive_ratio': blocked_positive / positives if positives else None},
                  'assemblies': assemblies,
                  'seconds': round(time.monotonic() - started, 2)}
        return report
    finally:
        conn.close()


def _percent(value):
    return '—' if value is None else '%.1f%%' % (100 * value)


def print_report(report):
    """控制台一行式汇总（对齐采集／交换台的一行汇总风格）；细节都在 `--json` 里。"""
    note = '；%s' % report['note'] if report['note'] else ''
    print('回放读取：缓存 %s（只读%s） · 商品 %d · 证据行缺失 %d · 版本不符 %d · payload 分组 %d 个（冻结 %d）'
          % (report['cache'], note, report['products'], len(report['evidence_missing']),
             report['version_mismatch'], report['payload_groups'], report['payload_frozen_groups']))
    if report['evidence_missing']:
        print('证据行缺失的商品（特征复算不出，按独立商品计入）：%s'
              % '、'.join(report['evidence_missing'][:5]))
    source = ('--config 的本机署名' if report['signature_source'] == 'config'
              else '缓存里行数最多者')
    print('署名：%s…（%s，%d 行 / 缓存共 %d 个署名） · 召回 k=%d · 下限 %d · 输入分组 %d 个（冻结 %d） · 排除对 %d'
          % (report['signature'][:12], source, report['signature_rows'], report['signatures'],
             report['candidates'], report['min_score'], report['input_groups'],
             report['frozen_groups'], report['excluded_pairs']))
    print('复算召回：%d 对 · 命中判断 %d 对（ρ=%s，正边 %d · 低把握 %d） · 判不动 %d 对 · 不属本次召回的判断行 %d'
          % (report['recall'], report['judged'], _percent(report['rho']), report['positive'],
             report['low_confidence'], report['unjudged'], report['outside_recall']))
    print('分数→同款率：' + ' · '.join(
        '%s 分 %d/%d（%s）' % (score, row['positive'], row['pairs'], _percent(row['rate']))
        for score, row in report['score_curve'].items()))
    floor = report['floor']
    print('下限 %d：挡下 %d 对（判定预算 %s） · 挡下正边 %d 条（%s）'
          % (floor['min_score'], floor['blocked'], _percent(floor['blocked_ratio']),
             floor['blocked_positive'], _percent(floor['blocked_positive_ratio'])))
    for label, row in report['assemblies'].items():
        compare = row['payload_compare']
        caption = CLIQUE_CAPTION if label == CLIQUE_KEY else label
        sizes = ' '.join('%s人×%s' % (size, count) for size, count in row['size_hist'].items())
        print('装配 %s：%d 组（≥2 件 %d · 最大 %d） · 规模 %s · 覆盖 %s · 纯度 %s · 负边入组 %d · 与 payload 相同 %d/%d · %.2f 秒'
              % (caption, row['groups'], row['groups_ge2'], row['largest'], sizes or '—',
                 _percent(row['coverage']), _percent(row['purity']), row['negative_inside'],
                 compare['identical'], compare['replay_groups'], row.get('seconds', 0.0)))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='同款分组回放器：在判断缓存上复算召回与装配（只读、零模型调用）')
    parser.add_argument('--cache', required=True, help='判断缓存库（matching.cache）')
    parser.add_argument('--store', help='分析草稿库；给了就用真账本当硬约束（缺省按 payload 的已确认组推导）')
    parser.add_argument('--config', help='分析配置；用它认本机署名与 min_score（缺省取缓存里行数最多的署名）')
    parser.add_argument('--weights', type=weights_of, default=DEFAULT_WEIGHTS,
                        help='逗号分隔的负边分歧权重，缺省 %s' % ','.join('%g' % w for w in DEFAULT_WEIGHTS))
    parser.add_argument('--min-score', type=int, help='判断预算下限；缺省读配置，再缺省 %d' % DEFAULT_MIN_SCORE)
    parser.add_argument('--json', help='把机器可读的报告写到这个路径')
    args = parser.parse_args(argv)
    if not Path(args.cache).exists():
        print('缓存不存在：%s' % args.cache)
        print('请用 --cache 指向分析程序写的判断缓存（config/analysis.toml 的 matching.cache）。')
        return 1
    try:
        report = replay(args.cache, store=args.store, config=args.config,
                        weights=args.weights, min_score=args.min_score)
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        print('回放不了：%s' % exc)
        return 1
    print_report(report)
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8',
                       newline='\n')
        print('已写出：%s' % out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
