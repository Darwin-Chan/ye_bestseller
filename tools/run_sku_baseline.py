"""Independent, fixed-configuration SKU baseline runner.

Run `python tools/run_sku_baseline.py --help`. Only --mode live can use a network.
Use this in a dedicated process: temporary instrumentation is process-global.
"""
from __future__ import annotations
import argparse
import csv
import json
import hashlib
import inspect
import os
import sys
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bestseller_monitor import sku_matching, sku_recommendations
from bestseller_monitor.matching import ImageJudge, digest, scored_pairs
from bestseller_monitor.sku_grouping import identity
from tools.sku_baseline_io import (load_input, load_config, make_config, now, read_json,
                                   safe_id, save_json, snapshot_for, source_version)
from tools.sku_baseline_transport import RequestLedger, run_lock, validate_pricing


def coverage(snapshot, config):
    """Audit recall without changing its candidates or production ordering."""
    eligible = sorted((m for m in snapshot['members'] if m.get('image_data')), key=identity)
    pairs = scored_pairs(eligible, config.candidates)
    return dict(image_members=len(eligible), recalled_pairs=len(pairs),
                score_qualified_pairs=sum(score >= config.min_score for score in pairs.values()),
                total_image_pairs=len(eligible) * (len(eligible) - 1) // 2)


def candidate_result(candidate, snapshot, ledger, elapsed, mode, pair_coverage):
    reasons = []
    for member in snapshot['members']:
        if not member.get('image_data'):
            reasons.append(dict(member=[member[k] for k in ('shop_key', 'offer_id', 'sku_id')],
                                reason=member['image_error'], kind='missing_evidence'))
    tasks = list(snapshot.get('model_tasks', {}).values())
    for task in tasks:
        if not task['completed']:
            reasons.append(dict(members=task['members'], reason=task['reason'], kind=task['state']))
    if mode == 'precheck':
        reasons.append(dict(kind='precheck_only', reason='model stages not executed'))
    if ledger.pause_reason:
        reasons.append(dict(kind=ledger.pause_reason, reason='run paused at request boundary'))
    members = snapshot['members']
    for member in members:
        # Uncalled pairs/members never acquire a negative result.
        if (member.get('matching_state') == 'unjudged' and not member.get('merchant_code')
                and not any(identity(member) in t['members'] for t in tasks) and len(members) > 1):
            reasons.append(dict(member=identity(member), kind='unjudged', reason=member.get('matching_note')))
        member.pop('image_data', None)
    pair_coverage.update(judgment_tasks=sum(t['branch'] == 'judgment' and len(t['members']) == 2 for t in tasks),
                         recommendation_tasks=sum(t['branch'] == 'recommendation' for t in tasks))
    return dict(set_id=candidate['id'], stratum=candidate['stratum'], candidate_source=candidate['candidate_source'],
                cross_offer_constructed=candidate['candidate_source'] != 'same_offer',
                denominator=len(members), all_pair_count=len(members) * (len(members) - 1) // 2,
                coverage=pair_coverage, manual_source=candidate.get('manual', {}).get('provenance', '未采用人工约束'),
                production_matching=snapshot.get('matching'), snapshot=snapshot, complete=not reasons,
                reasons=reasons, elapsed_seconds=elapsed,
                costs=ledger.summary([r for r in ledger.rows if r['set_id'] == candidate['id']]))


def write_reports(output, data, results, ledger, elapsed, mode):
    strata = defaultdict(lambda: dict(sets=0, members=0, complete_sets=0, all_pair_count=0))
    rows = []
    for result in results:
        summary = strata[result['stratum']]
        for key, amount in (('sets', 1), ('members', result['denominator']), ('complete_sets', int(result['complete'])),
                            ('all_pair_count', result['all_pair_count'])):
            summary[key] += amount
        snap = result['snapshot']
        groups = {identity(ref): group for group in snap['groups'] for ref in group['members']}
        for m in snap['members']:
            group = groups[identity(m)]
            rows.append(dict(set_id=result['set_id'], member_key=identity(m), group_id=group['id'],
                             grouping_basis=group['grouping_basis'], group_status=group['status'],
                             matching_state=m.get('matching_state', ''), merchant_code=m.get('merchant_code'),
                             recommendation=json.dumps(m.get('recommendation'), ensure_ascii=False),
                             image_judgments=json.dumps(m.get('image_judgments', []), ensure_ascii=False),
                             model_work=json.dumps(m.get('model_work', []), ensure_ascii=False),
                             note=m.get('matching_note', ''), evidence_version=m['evidence_version']))
    for stratum, summary in strata.items():
        ids = {r['set_id'] for r in results if r['stratum'] == stratum}
        summary['costs'] = ledger.summary([r for r in ledger.rows if r['set_id'] in ids])
        summary['elapsed_seconds'] = sum(r['elapsed_seconds'] for r in results if r['stratum'] == stratum)
    with (output / 'members.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ['set_id', 'member_key'])
        writer.writeheader()
        writer.writerows(rows)
    report = dict(schema=1, generated_at=now(), mode=mode, synthetic=data['synthetic'],
                  members=sum(len(s['members']) for s in data['sets']), sets=len(data['sets']),
                  reported_members=len(rows), reported_sets=len(results),
                  complete_sets=sum(r['complete'] for r in results), strata=dict(strata),
                  elapsed_seconds=elapsed, **ledger.summary())
    save_json(output / 'report.json', report)
    return report


def limits(mode, data, frozen, options):
    pricing = validate_pricing(read_json(options['pricing'])) if options.get('pricing') else None
    max_calls, budget = options.get('max_calls'), options.get('budget')
    if max_calls is not None and (type(max_calls) is not int or max_calls <= 0):
        raise ValueError('max_calls must be a positive integer')
    if budget is not None:
        try:
            value = Decimal(str(budget))
            if not value.is_finite() or value <= 0:
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise ValueError('budget must be finite and positive') from None
    if mode == 'live':
        if options.get('allow_live') is not True or data['synthetic']:
            raise ValueError('live requires explicit --allow-live and real inputs')
        if pricing is None or max_calls is None or budget is None:
            raise ValueError('live requires confirmed pricing, call limit and budget')
        if frozen['mode'] == 'disabled':
            raise ValueError('live matching is disabled')
        for kind in ('model', 'vision'):
            model = frozen[kind]
            if model and not any(p['endpoint'] == model['endpoint'] and p['model'] == model['model'] for p in pricing['models']):
                raise ValueError('pricing model does not match frozen configuration')
    return pricing, max_calls, budget


def run(manifest, config, output, *, mode='precheck', fixtures=None, resume=False, **options):
    if mode not in ('precheck', 'offline', 'live'):
        raise ValueError('unknown mode')
    data, inputs = load_input(manifest, pack=options.get('pack'))
    frozen = load_config(config)
    if mode == 'offline' and (not data['synthetic'] or fixtures is None):
        raise ValueError('offline requires synthetic inputs and explicit HTTP fixtures')
    if mode != 'offline' and fixtures is not None:
        raise ValueError('fixtures are only valid in offline mode')
    fixture_data = read_json(fixtures) if fixtures else None
    pricing, max_calls, budget = limits(mode, data, frozen, options)
    code = source_version()
    fingerprint = digest(dict(inputs=inputs, config=frozen, source=code['source_digest'], mode=mode,
                              fixtures=fixture_data, pricing=pricing, max_calls=max_calls, budget=str(budget)))
    output = Path(output).resolve()
    if resume:
        if not output.is_dir() or read_json(output / 'frozen.json')['fingerprint'] != fingerprint:
            raise ValueError('resume fingerprint mismatch: input/config/source/limits/fixtures changed')
    else:
        output.mkdir(parents=True, exist_ok=False)
    with run_lock(output):
        if not resume:
            save_json(output / 'frozen.json', dict(input=data, inputs=inputs, config=frozen, code=code, mode=mode,
                      pricing=pricing, max_calls=max_calls, budget=budget, fingerprint=fingerprint,
                      production_parameters=dict(temperature=0, response_format=dict(type='json_object')),
                      prompt_source='bestseller_monitor/matching.py',
                      prompt_method_sha256={name: hashlib.sha256(inspect.getsource(getattr(ImageJudge, name)).encode()).hexdigest()
                                            for name in ('verify', 'compare', 'recommend', '_visual_evidence')},
                      rule_versions=dict(judgment=sku_matching.RULE_VERSION,
                                         recommendation=sku_recommendations.RULE_VERSION),
                      frozen_at=now()))
        return execute(output, data, frozen, fixture_data, pricing, max_calls, budget, mode)


def execute(output, data, frozen, fixture_data, pricing, max_calls, budget, mode):
    started_run = time.perf_counter()
    ledger = RequestLedger(output, fixture=fixture_data, pricing=pricing, max_calls=max_calls, budget=budget)
    previous_report = read_json(output / 'report.json') if (output / 'report.json').exists() else {}
    results = []
    old_key = os.environ.get('SKU_BASELINE_OFFLINE_KEY')
    try:
        if mode == 'offline':
            os.environ['SKU_BASELINE_OFFLINE_KEY'] = 'synthetic-not-a-credential'
        with ledger.observe():
            for candidate in data['sets']:
                started = time.perf_counter()
                directory = output / 'sets' / candidate['id']
                checkpoint = directory / 'checkpoint.json'
                saved = read_json(checkpoint) if checkpoint.exists() else dict(model_tasks={})
                # An issued response without a durable production task result cannot be resent.
                for row in ledger.rows:
                    if row['set_id'] != candidate['id'] or row['state'] != 'response':
                        continue
                    task = saved['model_tasks'].get((row.get('task') or {}).get('key'))
                    if task is None:
                        ledger.pause_reason = 'response_requires_reconciliation'
                snap = snapshot_for(data, candidate)
                snap['model_tasks'] = saved['model_tasks']
                ledger.set_id = candidate['id']
                ledger.on_task = lambda snapshot: save_json(checkpoint, dict(model_tasks=snapshot['model_tasks']))
                cfg = make_config(frozen, directory / 'cache.sqlite', offline=mode == 'offline')
                pair_coverage = coverage(snap, cfg)
                try:
                    if mode != 'precheck':
                        sku_matching.apply(snap, cfg, stop=lambda: bool(ledger.pause_reason), machine='research-' + mode)
                except KeyboardInterrupt:
                    ledger.pause_reason = ledger.pause_reason or 'interrupted'
                except Exception:
                    # Do not expose provider bodies/URLs/credentials in CLI errors or reports.
                    ledger.pause_reason = 'unexpected_execution_error'
                ledger.audit_accounting()
                save_json(checkpoint, dict(model_tasks=snap.get('model_tasks', {})))
                elapsed = time.perf_counter() - started
                if (directory / 'result.json').exists():
                    elapsed += read_json(directory / 'result.json').get('elapsed_seconds', 0)
                result = candidate_result(candidate, snap, ledger, elapsed, mode, pair_coverage)
                save_json(directory / 'result.json', result)
                results.append(result)
                # Continue assembling every candidate denominator with the stop gate held.
    finally:
        ledger.on_task = None
        if old_key is None:
            os.environ.pop('SKU_BASELINE_OFFLINE_KEY', None)
        else:
            os.environ['SKU_BASELINE_OFFLINE_KEY'] = old_key
    elapsed = previous_report.get('elapsed_seconds', 0) + time.perf_counter() - started_run
    return write_reports(output, data, results, ledger, elapsed, mode)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--mode', choices=('precheck', 'offline', 'live'), default='precheck')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output-root', type=Path, default=REPO / '.scratch/tool-output/sku-fixed-baseline-run')
    parser.add_argument('--pack', type=Path, help='original read-only sample pack; required for real input')
    parser.add_argument('--fixtures', type=Path)
    parser.add_argument('--pricing', type=Path)
    parser.add_argument('--max-calls', type=int)
    parser.add_argument('--budget', help='amount in pricing-table currency')
    parser.add_argument('--allow-live', action='store_true', help='explicit authorization for paid HTTP requests')
    parser.add_argument('--resume', action='store_true')
    args = vars(parser.parse_args(argv))
    output = args.pop('output_root') / safe_id(args.pop('run_id'))
    try:
        report = run(output=output, **args)
    except (ValueError, OSError, KeyError, TypeError):
        print('Input/run validation failed; check input contract, output existence and resume fingerprint.', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['complete_sets'] == report['sets'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
