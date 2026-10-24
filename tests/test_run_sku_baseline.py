"""Offline integration tests for the independent SKU research entry point."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import run_sku_baseline as runner
from bestseller_monitor.sku_codes import code_evidence
from bestseller_monitor.sku_matching import evidence_version


class BaselineFixtures:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'analysis.toml'
        self.config.write_text('[matching]\nmode="direct"\ncandidates=6\nmin_score=4\n'
                               '[matching.model]\nendpoint="https://offline.invalid/v1/chat/completions"\n'
                               'model="frozen-test-model"\nkey_env="SKU_TEST_KEY"\ntimeout=30\n', encoding='utf-8')

    def member(self, sku, name='#A#'):
        m = dict(shop_key='shop', offer_id='offer', sku_id=sku, product_name='physical shared product words alpha beta',
                 raw_sku_name=name, single_sku=False, name_source='sku',
                 matching_input=dict(product_name='physical shared product words alpha beta', sku_name=name),
                 observed_at='2026-10-01T10:00:00+08:00', observed_date='2026-10-01',
                 image_hash=None, image_source='missing', image_error='synthetic missing image',
                 provenance='synthetic test fixture')
        m.update(code_evidence(m))
        m['evidence_version'] = evidence_version(m)
        return m

    def manifest(self, members):
        path = self.root / 'input.json'
        data = dict(schema=1, synthetic=True, analysis=dict(start='2026-10-01', end='2026-10-02'),
                    provenance='synthetic fixture', sets=[dict(id='set-1', stratum='1-10',
                    candidate_source='same_offer', members=members)])
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_precheck_preserves_missing_denominator_and_code_groups_without_network(self):
        source = self.manifest([self.member('1'), self.member('2'), self.member('3', '#B#')])
        before = source.read_bytes()
        with patch('bestseller_monitor.matching.urlopen', side_effect=AssertionError('network forbidden')):
            report = runner.run(source, self.config, self.root / 'run', mode='precheck')
        self.assertEqual(report['members'], 3)
        self.assertEqual(report['http_calls'], 0)
        result = json.loads((self.root / 'run/sets/set-1/result.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(len(g['members']) for g in result['snapshot']['groups']), [1, 2])
        self.assertFalse(result['complete'])
        self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

class ModelFlowTests(BaselineFixtures, unittest.TestCase):
    def image_member(self, sku, name='plain sku'):
        import hashlib
        from PIL import Image
        m = self.member(sku, name)
        asset = self.root / (sku + '.png')
        Image.new('RGB', (12, 12), 'red').save(asset)
        m.update(asset_path=asset.name, image_hash=hashlib.sha256(asset.read_bytes()).hexdigest(),
                 image_source='sku', image_error='')
        m['evidence_version'] = evidence_version(m)
        return m

    def fixtures(self, judgments, recommendations=None, usage=None):
        path = self.root / 'responses.json'
        data = dict(usage=usage if usage is not None else dict(prompt_tokens=100, completion_tokens=20,
                    prompt_cache_hit_tokens=10, prompt_cache_miss_tokens=90),
                    responses=dict(judgment=judgments, recommendation=recommendations or []))
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_real_judge_parser_and_assembly_use_only_simulated_http(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        responses = self.fixtures([dict(value=dict(same=True, confidence=.95, reason='same object'))])
        with patch('bestseller_monitor.matching.urlopen', side_effect=AssertionError('network forbidden')):
            report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses)
        self.assertEqual(report['http_calls'], 2)  # visual challenge + judgment
        result = json.loads((self.root / 'run/sets/set-1/result.json').read_text(encoding='utf-8'))
        self.assertEqual(len(result['snapshot']['groups']), 1)
        self.assertTrue(result['complete'])
        ledger = json.loads((self.root / 'run/requests.json').read_text(encoding='utf-8'))
        self.assertEqual([r['kind'] for r in ledger], ['verify', 'judgment'])
        self.assertEqual(ledger[1]['tokens']['prompt_tokens'], 100)
        self.assertEqual(ledger[1]['task']['branch'], 'judgment')

    def test_ambiguous_uncoded_member_uses_production_recommendation(self):
        source = self.manifest([self.image_member('a', '#A#'), self.image_member('b', '#B#'), self.image_member('n')])
        positive = dict(value=dict(same=True, confidence=.95, reason='candidate same shape'))
        responses = self.fixtures([positive, positive], [dict(value=dict(group_id=None, reason='two legal candidates'))])
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses)
        ledger = json.loads((self.root / 'run/requests.json').read_text(encoding='utf-8'))
        self.assertEqual([r['kind'] for r in ledger], ['verify', 'judgment', 'judgment', 'verify', 'recommendation'])
        result = json.loads((self.root / 'run/sets/set-1/result.json').read_text(encoding='utf-8'))
        self.assertEqual(len(result['snapshot']['groups']), 3)
        tasks = list(result['snapshot']['model_tasks'].values())
        self.assertIn('completed_uncertain', [t['state'] for t in tasks])
        self.assertTrue(report['complete_sets'])

    def prices(self):
        path = self.root / 'prices.json'
        path.write_text(json.dumps(dict(confirmed=True, source='synthetic rates only', effective_at='2026-10-01',
                        currency='TEST', models=[dict(endpoint='https://offline.invalid/v1/chat/completions',
                        model='frozen-test-model', input_per_million='2', cached_input_per_million='1',
                        output_per_million='3', request_reserve='0.001')])), encoding='utf-8')
        return path

    def test_cost_and_call_limit_stop_before_next_http(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        responses = self.fixtures([dict(value=dict(same=True, confidence=.95, reason='same'))])
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses,
                            pricing=self.prices(), max_calls=1, budget='1')
        self.assertEqual(report['http_calls'], 1)
        self.assertEqual(report['estimated_cost'], '0.00025')
        self.assertEqual(report['pause_reason'], 'call_limit')
        self.assertEqual(report['complete_sets'], 0)


    def test_resume_reuses_completed_null_recommendation_without_paid_repeats(self):
        source = self.manifest([self.image_member('a', '#A#'), self.image_member('b', '#B#'), self.image_member('n')])
        positive = dict(value=dict(same=True, confidence=.95, reason='candidate'))
        responses = self.fixtures([positive, positive], [dict(value=dict(group_id=None, reason='ambiguous'))])
        first = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses)
        second = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses, resume=True)
        self.assertEqual(second['http_calls'], first['http_calls'])
        self.assertEqual(second['complete_sets'], 1)
        changed = json.loads(source.read_text())
        changed['provenance'] = 'changed input'
        source.write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses, resume=True)

    def test_interrupted_request_is_durable_and_resume_never_resends(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        responses = self.fixtures([dict(interrupt=True)])
        first = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses)
        self.assertEqual(first['http_calls'], 2)
        self.assertEqual(first['pause_reason'], 'request_requires_reconciliation')
        second = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses, resume=True)
        self.assertEqual(second['http_calls'], 2)
        self.assertEqual(second['complete_sets'], 0)

    def test_missing_usage_stays_unknown_and_blocks_next_request_when_priced(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        responses = self.fixtures([], usage={})
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses,
                            pricing=self.prices(), max_calls=10, budget='1')
        self.assertEqual(report['http_calls'], 1)
        self.assertIsNone(report['estimated_cost'])
        self.assertEqual(report['tokens']['prompt_tokens'], dict(known=0, unknown_requests=1))
        self.assertEqual(report['pause_reason'], 'usage_or_cost_unknown')


    def test_budget_reserve_pauses_before_any_http(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=self.fixtures([]),
                            pricing=self.prices(), max_calls=10, budget='0.0009')
        self.assertEqual(report['http_calls'], 0)
        self.assertEqual(report['estimated_cost'], '0')
        self.assertEqual(report['pause_reason'], 'budget_reserve_limit')

    def test_cache_is_not_shared_between_identical_candidate_sets(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        data = json.loads(source.read_text())
        data['sets'].append(dict(data['sets'][0], id='set-2'))
        source.write_text(json.dumps(data), encoding='utf-8')
        responses = self.fixtures([dict(value=dict(same=False, confidence=.99, reason='different'))])
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses)
        self.assertEqual(report['http_calls'], 4)
        self.assertEqual(report['members'], 4)
        self.assertTrue((self.root / 'run/sets/set-1/cache.sqlite').exists())
        self.assertTrue((self.root / 'run/sets/set-2/cache.sqlite').exists())

    def test_manual_boundary_is_explicit_and_blocks_automatic_merge(self):
        from bestseller_monitor.sku_grouping import bind_manual_members
        from tools.sku_baseline_io import snapshot_for, load_input
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        data, _ = load_input(source)
        saved = snapshot_for(data, data['sets'][0])
        bind_manual_members(saved, saved['groups'][0], confirmed=True)
        bind_manual_members(saved, saved['groups'][1], confirmed=True)
        data = json.loads(source.read_text())
        data['sets'][0]['manual'] = dict(confirmed_applicable=True, provenance='user-confirmed synthetic constraint', snapshot=saved)
        source.write_text(json.dumps(data), encoding='utf-8')
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=self.fixtures([]))
        self.assertEqual(report['http_calls'], 0)
        result = json.loads((self.root / 'run/sets/set-1/result.json').read_text(encoding='utf-8'))
        self.assertEqual(len(result['snapshot']['groups']), 2)
        data['sets'][0]['manual']['confirmed_applicable'] = False
        source.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'manual constraints'):
            runner.run(source, self.config, self.root / 'rejected', mode='precheck')

    def test_invalid_json_is_not_a_negative_judgment_or_auto_retried(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        report = runner.run(source, self.config, self.root / 'run', mode='offline',
                            fixtures=self.fixtures([dict(raw='not JSON')]))
        self.assertEqual(report['http_calls'], 2)
        self.assertEqual(report['complete_sets'], 0)
        result = json.loads((self.root / 'run/sets/set-1/result.json').read_text(encoding='utf-8'))
        self.assertEqual(len(result['snapshot']['groups']), 2)
        self.assertEqual([t['state'] for t in result['snapshot']['model_tasks'].values()], ['failed'])

    def test_low_confidence_remains_incomplete(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        report = runner.run(source, self.config, self.root / 'run', mode='offline',
                            fixtures=self.fixtures([dict(value=dict(same=True, confidence=.7, reason='uncertain'))]))
        self.assertEqual(report['complete_sets'], 0)
        result = json.loads((self.root / 'run/sets/set-1/result.json').read_text(encoding='utf-8'))
        self.assertEqual([t['state'] for t in result['snapshot']['model_tasks'].values()], ['inconclusive'])

    def test_production_visual_challenge_keeps_exactly_two_attempts(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        fixture = self.fixtures([])
        data = json.loads(fixture.read_text())
        data['responses']['verify'] = [dict(value=dict(colors=[])), dict(value=dict(colors=[]))]
        fixture.write_text(json.dumps(data), encoding='utf-8')
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=fixture)
        self.assertEqual(report['http_calls'], 2)
        self.assertEqual(report['calls_by_kind'], {'verify': 2})
        self.assertEqual(report['complete_sets'], 0)

    def test_evidence_and_asset_tampering_are_rejected_without_calls(self):
        members = [self.image_member('1'), self.image_member('2')]
        source = self.manifest(members)
        (self.root / '1.png').write_bytes(b'bad asset')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            runner.run(source, self.config, self.root / 'run')
        self.assertFalse((self.root / 'run').exists())
        members[0]['asset_path'] = None
        members[0]['image_error'] = 'unavailable'
        members[0]['evidence_version'] = 'wrong'
        source = self.manifest(members)
        with self.assertRaisesRegex(ValueError, 'evidence_version mismatch'):
            runner.run(source, self.config, self.root / 'run')

    def test_resume_rejects_configuration_and_source_change(self):
        source = self.manifest([self.member('1')])
        responses = self.fixtures([])
        runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses)
        with patch('tools.run_sku_baseline.source_version', return_value=dict(source_digest='different-code')):
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses, resume=True)
        self.config.write_text(self.config.read_text().replace('candidates=6', 'candidates=5'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses, resume=True)

    def test_real_mode_is_closed_for_synthetic_inputs_and_missing_pack(self):
        source = self.manifest([self.member('1')])
        with patch('bestseller_monitor.matching.urlopen', side_effect=AssertionError('network forbidden')):
            with self.assertRaisesRegex(ValueError, 'explicit.*real'):
                runner.run(source, self.config, self.root / 'run', mode='live')
            data = json.loads(source.read_text())
            data['synthetic'] = False
            source.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '--pack'):
                runner.run(source, self.config, self.root / 'run', mode='live', allow_live=True)
        self.assertFalse((self.root / 'run').exists())


    def test_caption_mode_executes_real_description_pipeline(self):
        self.config.write_text(self.config.read_text().replace('mode="direct"', 'mode="caption"') +
            '\n[matching.vision]\nendpoint="https://offline.invalid/v1/chat/completions"\n'
            'model="frozen-test-vision"\nkey_env="SKU_TEST_VISION_KEY"\ntimeout=30\n', encoding='utf-8')
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        fixture = self.fixtures([dict(value=dict(same=False, confidence=.98, reason='different shape'))])
        data = json.loads(fixture.read_text())
        data['responses']['caption'] = [dict(value=dict(description='red physical object'))]
        fixture.write_text(json.dumps(data), encoding='utf-8')
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=fixture)
        self.assertEqual(report['calls_by_kind'], dict(verify=1, caption=1, judgment=1))
        self.assertEqual(report['complete_sets'], 1)

    def test_unknown_single_sku_is_preserved_without_product_code_fallback(self):
        m = self.member('1', '')
        m.update(single_sku=None, product_name='product #A#', evidence_note='historical completeness unknown',
                 matching_input=dict(product_name='product #A#', sku_name=''))
        m.update(code_evidence(m))
        m['evidence_version'] = evidence_version(m)
        source = self.manifest([m])
        runner.run(source, self.config, self.root / 'run')
        result = json.loads((self.root / 'run/sets/set-1/result.json').read_text(encoding='utf-8'))
        self.assertIsNone(result['snapshot']['members'][0]['merchant_code'])

    def test_overwrite_is_rejected_and_missing_db_paths_are_not_created(self):
        source = self.manifest([self.member('1')])
        self.config.write_text(self.config.read_text() + '\n[analysis]\ndatabase="NEVER.sqlite"\nstore="NEVER-drafts.sqlite"\n', encoding='utf-8')
        runner.run(source, self.config, self.root / 'run')
        with self.assertRaises(FileExistsError):
            runner.run(source, self.config, self.root / 'run')
        self.assertFalse((self.root / 'NEVER.sqlite').exists())
        self.assertFalse((self.root / 'NEVER-drafts.sqlite').exists())

    def test_cost_unknown_on_last_response_is_reported_as_pause(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        responses = self.fixtures([dict(value=dict(same=False, confidence=.95, reason='different'), usage={})])
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses,
                            pricing=self.prices(), max_calls=10, budget='1')
        self.assertEqual(report['pause_reason'], 'usage_or_cost_unknown')
        self.assertIsNone(report['estimated_cost'])
        result = json.loads((self.root / 'run/sets/set-1/result.json').read_text(encoding='utf-8'))
        self.assertTrue(all(t['completed'] for t in result['snapshot']['model_tasks'].values()))

    def test_response_without_task_checkpoint_is_never_resent(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        responses = self.fixtures([dict(value=dict(same=False, confidence=.95, reason='different'))])
        runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses)
        checkpoint = self.root / 'run/sets/set-1/checkpoint.json'
        checkpoint.write_text(json.dumps(dict(model_tasks={})), encoding='utf-8')
        # The cache also survives; the runner still requires reconciliation of the orphan response.
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses, resume=True)
        self.assertEqual(report['http_calls'], 2)
        self.assertEqual(report['pause_reason'], 'response_requires_reconciliation')

    def test_request_reserve_overrun_pauses_and_keeps_known_actual_estimate(self):
        source = self.manifest([self.image_member('1'), self.image_member('2')])
        responses = self.fixtures([], usage=dict(prompt_tokens=100000, completion_tokens=20000,
                    prompt_cache_hit_tokens=10000, prompt_cache_miss_tokens=90000))
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=responses,
                            pricing=self.prices(), max_calls=10, budget='1')
        self.assertEqual(report['http_calls'], 1)
        self.assertEqual(report['estimated_cost'], '0.25')
        self.assertEqual(report['pause_reason'], 'request_reserve_exceeded')

    def real_pack(self):
        import csv
        members = []
        candidates = []
        for i in range(34):
            size = 8 if i == 33 else 16
            sid = 'set-' + str(i)
            current = [self.member(str(n), '#A#') for n in range(size)]
            candidates.append(dict(id=sid, stratum='1-10' if size == 8 else '11-25',
                                   candidate_source='same_offer', members=current))
            members.extend(dict(set_id=sid, **{k: m[k] for k in ('shop_key', 'offer_id', 'sku_id')}) for m in current)
        path = self.manifest([self.member('unused')])
        data = json.loads(path.read_text())
        data.update(synthetic=False, sets=candidates)
        path.write_text(json.dumps(data), encoding='utf-8')
        pack = self.root / 'pack'
        pack.mkdir()
        for name, rows in (('members.csv', members), ('sets.csv', [dict(set_id=s['id'], stratum=s['stratum'],
                           candidate_source=s['candidate_source'], candidate_size=len(s['members'])) for s in candidates])):
            with (pack / name).open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        return path, pack

    def test_real_candidate_boundary_and_live_authorization_requirements(self):
        source, pack = self.real_pack()
        with patch('bestseller_monitor.matching.urlopen', side_effect=AssertionError('network forbidden')):
            with self.assertRaisesRegex(ValueError, 'explicit.*real'):
                runner.run(source, self.config, self.root / 'run', mode='live', pack=pack)
            with self.assertRaisesRegex(ValueError, 'confirmed pricing'):
                runner.run(source, self.config, self.root / 'run', mode='live', pack=pack, allow_live=True)
            report = runner.run(source, self.config, self.root / 'precheck', pack=pack)
            self.assertEqual(report['members'], 536)
            self.assertEqual(report['sets'], 34)
            self.assertEqual(report['http_calls'], 0)
            data = json.loads(source.read_text())
            data['sets'][0]['members'].pop()
            source.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'memberships differ'):
                runner.run(source, self.config, self.root / 'rejected', pack=pack)
        self.assertFalse((self.root / 'run').exists())

    def test_two_uncoded_positives_cannot_bridge_different_codes(self):
        source = self.manifest([self.image_member('a', '#A#'), self.image_member('b', '#B#'), self.image_member('n')])
        positive = dict(value=dict(same=True, confidence=.95, reason='same shape candidate'))
        runner.run(source, self.config, self.root / 'precheck')
        checked = json.loads((self.root / 'precheck/sets/set-1/result.json').read_text(encoding='utf-8'))
        target = next(g['id'] for g in checked['snapshot']['groups'] if g['merchant_codes'] == ['A'])
        response = self.fixtures([positive, positive], [dict(value=dict(group_id=target, reason='selected visual candidate'))])
        report = runner.run(source, self.config, self.root / 'run', mode='offline', fixtures=response)
        result = json.loads((self.root / 'run/sets/set-1/result.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(len(g['members']) for g in result['snapshot']['groups']), [1, 2])
        self.assertEqual(report['calls_by_kind']['recommendation'], 1)
        self.assertTrue(all(len(g['merchant_codes']) <= 1 for g in result['snapshot']['groups']))

    def test_concurrent_resume_is_refused_without_requests(self):
        from tools.sku_baseline_transport import run_lock
        source = self.manifest([self.member('1')])
        runner.run(source, self.config, self.root / 'run')
        with run_lock(self.root / 'run'):
            with self.assertRaisesRegex(ValueError, 'locked'):
                runner.run(source, self.config, self.root / 'run', resume=True)


if __name__ == '__main__':
    unittest.main()
