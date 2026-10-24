"""Request-boundary audit for a dedicated, single-process research runner.

Instrumentation observes production calls and always delegates to their original
implementations. Offline mode replaces only HTTP transport, never ModelWork or
ImageJudge behavior. No authorization headers or raw provider error bodies persist.
"""
from __future__ import annotations
import base64
import copy
import io
import json
import os
import time
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

from PIL import Image
from bestseller_monitor import matching, sku_lifecycle
from tools.sku_baseline_io import now, read_json, save_json

TOKEN_FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens',
                'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')


class Paused(Exception):
    pass


class BytesResponse:
    def __init__(self, raw):
        self.raw = raw
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.raw[:size]


class OfflineHTTP:
    """Explicit fixture response streams, independently indexed per candidate set."""
    def __init__(self, fixture, rows):
        self.fixture = fixture
        self.indices = Counter((r['set_id'], r['kind']) for r in rows if r['state'] != 'intent')

    def send(self, request, timeout, *, set_id, kind):
        payload = json.loads(request.data)
        index = self.indices[(set_id, kind)]
        self.indices[(set_id, kind)] += 1
        scoped = self.fixture.get('sets', {}).get(set_id, self.fixture)
        sequence = scoped.get('responses', {}).get(kind, [])
        item = sequence[index] if index < len(sequence) else None
        if item is None and kind == 'verify':
            data = payload['messages'][1]['content'][0]['image_url']['url']
            with Image.open(io.BytesIO(base64.b64decode(data.split(',', 1)[1]))) as image:
                colors = {(255, 0, 0): 'red', (0, 255, 0): 'green', (0, 0, 255): 'blue'}
                value = dict(colors=[colors[image.convert('RGB').getpixel((i * 50 + 25, 25))] for i in range(5)])
            item = dict(value=value)
        if item is None:
            raise ValueError('offline response stream exhausted')
        if item.get('interrupt'):
            raise KeyboardInterrupt()
        if item.get('transport_error'):
            raise OSError('synthetic transport failure')
        if 'raw' in item:
            return BytesResponse(item['raw'].encode('utf-8'))
        usage = item.get('usage', scoped.get('usage', self.fixture.get('usage')))
        body = dict(choices=[dict(message=dict(content=json.dumps(item['value'])))])
        if usage is not None:
            body['usage'] = usage
        return BytesResponse(json.dumps(body).encode('utf-8'))


def token_values(usage):
    usage = usage if isinstance(usage, dict) else {}
    return {field: value if type(value := usage.get(field)) is int and value >= 0 else None for field in TOKEN_FIELDS}


def validate_pricing(pricing):
    if not isinstance(pricing, dict) or pricing.get('confirmed') is not True or not all(
            pricing.get(k) for k in ('source', 'effective_at', 'currency', 'models')):
        raise ValueError('confirmed pricing source, effective_at, currency and models required')
    for model in pricing['models']:
        if not model.get('endpoint') or not model.get('model'):
            raise ValueError('pricing requires exact endpoint/model')
        for field in ('input_per_million', 'cached_input_per_million', 'output_per_million', 'request_reserve'):
            try:
                value = Decimal(str(model[field]))
                if not value.is_finite() or value < 0 or (field == 'request_reserve' and value == 0):
                    raise ValueError('invalid price')
            except (KeyError, ValueError, InvalidOperation):
                raise ValueError('invalid or incomplete pricing rates/reserve') from None
    pairs = [(m['endpoint'], m['model']) for m in pricing['models']]
    if len(pairs) != len(set(pairs)):
        raise ValueError('duplicate pricing model')
    return pricing


class RequestLedger:
    def __init__(self, output, *, fixture=None, pricing=None, max_calls=None, budget=None):
        self.path = output / 'requests.json'
        self.rows = read_json(self.path) if self.path.exists() else []
        self.pricing = pricing
        self.max_calls = max_calls
        self.budget = Decimal(str(budget)) if budget is not None else None
        self.set_id = None
        self.kind = None
        self.task = None
        self.on_task = None
        self.pause_reason = None
        self.offline = OfflineHTTP(fixture, self.rows) if fixture is not None else None
        if any(r['state'] in ('intent', 'uncertain') for r in self.rows):
            self.pause_reason = 'request_requires_reconciliation'
        self.flush()

    def flush(self):
        save_json(self.path, self.rows)

    def model_price(self, endpoint, model):
        if self.pricing:
            return next((m for m in self.pricing['models'] if m['endpoint'] == endpoint and m['model'] == model), None)
        return None

    def spent(self):
        return sum((Decimal(r['estimated_cost']) for r in self.rows if r.get('estimated_cost') is not None), Decimal(0))

    def audit_accounting(self):
        if self.pause_reason or not self.pricing:
            return
        if any(r.get('estimated_cost') is None for r in self.rows):
            self.pause_reason = 'usage_or_cost_unknown'
        elif any(Decimal(r['estimated_cost']) > Decimal(str(self.model_price(r['endpoint'], r['model'])['request_reserve']))
                 for r in self.rows):
            self.pause_reason = 'request_reserve_exceeded'
        elif self.budget is not None and self.spent() >= self.budget:
            self.pause_reason = 'budget_limit'

    def gate(self, endpoint, model):
        self.audit_accounting()
        if self.pause_reason:
            raise Paused(self.pause_reason)
        if self.max_calls is not None and len(self.rows) >= self.max_calls:
            self.pause_reason = 'call_limit'
        if self.pricing:
            price = self.model_price(endpoint, model)
            if price is None:
                self.pause_reason = 'pricing_model_missing'
            elif any(r.get('estimated_cost') is None for r in self.rows):
                self.pause_reason = 'usage_or_cost_unknown'
            elif self.budget is not None and self.spent() + Decimal(str(price['request_reserve'])) > self.budget:
                self.pause_reason = 'budget_reserve_limit'
        if self.pause_reason:
            raise Paused(self.pause_reason)

    def estimate(self, row):
        price = self.model_price(row['endpoint'], row['model'])
        tokens = row['tokens']
        if price is None:
            return None
        prompt, completion = tokens['prompt_tokens'], tokens['completion_tokens']
        hit, miss = tokens['prompt_cache_hit_tokens'], tokens['prompt_cache_miss_tokens']
        if prompt is None or completion is None:
            return None
        if Decimal(str(price['input_per_million'])) == Decimal(str(price['cached_input_per_million'])):
            hit, miss = 0, prompt
        elif hit is None or miss is None or hit + miss != prompt:
            return None
        value = (Decimal(miss) * Decimal(str(price['input_per_million'])) +
                 Decimal(hit) * Decimal(str(price['cached_input_per_million'])) +
                 Decimal(completion) * Decimal(str(price['output_per_million']))) / Decimal(1000000)
        return str(value)

    def send(self, original, request, timeout):
        payload = json.loads(request.data)
        self.gate(request.full_url, payload['model'])
        row = dict(id=len(self.rows) + 1, set_id=self.set_id, kind=self.kind, task=copy.deepcopy(self.task),
                   endpoint=request.full_url, model=payload['model'], request_digest=matching.digest(payload),
                   state='intent', issued_at=now(), response_at=None, elapsed_seconds=None, http_status=None,
                   usage=None, tokens=token_values(None), estimated_cost=None, supplier_bill=None,
                   cost_status='unknown', simulated=self.offline is not None)
        self.rows.append(row)
        self.flush()  # Intent is durable BEFORE entering transport; a crash stays uncertain.
        started = time.perf_counter()
        try:
            response = (self.offline.send(request, timeout, set_id=self.set_id, kind=self.kind)
                        if self.offline else original(request, timeout=timeout))
            row['http_status'] = getattr(response, 'status', None)
            with response:
                raw = response.read(1024 * 1024 + 1)
        except BaseException:
            row.update(state='uncertain', response_at=now(), elapsed_seconds=time.perf_counter() - started,
                       cost_status='unknown_transport_outcome')
            self.pause_reason = 'request_requires_reconciliation'
            self.flush()
            raise
        row.update(state='response', response_at=now(), elapsed_seconds=time.perf_counter() - started)
        try:
            body = json.loads(raw) if len(raw) <= 1024 * 1024 else None
            usage = body.get('usage') if isinstance(body, dict) else None
            row.update(usage=usage, tokens=token_values(usage))
            row['estimated_cost'] = self.estimate(row)
            row['cost_status'] = 'estimated' if row['estimated_cost'] is not None else 'usage_or_pricing_unknown'
        except (ValueError, TypeError, KeyError):
            row['cost_status'] = 'invalid_response_usage_unknown'
        self.flush()
        return BytesResponse(raw)

    @contextmanager
    def observe(self):
        original_http = matching.urlopen
        original_json = matching.request_json
        original_execute = sku_lifecycle.ModelWork.execute
        ledger = self

        def request_json(config, content, instruction, usage=None, kind=''):
            old = ledger.kind
            ledger.kind = {'compare': 'judgment', 'recommend': 'recommendation'}.get(kind, kind)
            try:
                return original_json(config, content, instruction, usage=usage, kind=kind)
            finally:
                ledger.kind = old

        def execute(work, branch, context, members, rule, cache_key, call):
            old = ledger.task
            ledger.task = dict(branch=branch, key=matching.digest(dict(branch=branch, context=context)),
                               members=members, rule=rule)
            try:
                result = original_execute(work, branch, context, members, rule, cache_key, call)
                if ledger.on_task:
                    ledger.on_task(work.snapshot)
                # Record that the production parser/task has handled these responses.
                for row in ledger.rows:
                    if row['set_id'] == ledger.set_id and row.get('task', {}).get('key') == ledger.task['key']:
                        row['task_checkpointed'] = True
                        row['production_task_state'] = work.tasks[ledger.task['key']]['state']
                ledger.flush()
                return result
            finally:
                ledger.task = old

        matching.urlopen = lambda request, timeout: ledger.send(original_http, request, timeout)
        matching.request_json = request_json
        sku_lifecycle.ModelWork.execute = execute
        try:
            yield self
        finally:
            matching.urlopen = original_http
            matching.request_json = original_json
            sku_lifecycle.ModelWork.execute = original_execute

    def summary(self, rows=None):
        rows = self.rows if rows is None else rows
        totals = {field: dict(known=sum(r['tokens'][field] or 0 for r in rows),
                              unknown_requests=sum(r['tokens'][field] is None for r in rows)) for field in TOKEN_FIELDS}
        return dict(http_calls=len(rows), http_elapsed_seconds=sum(r.get('elapsed_seconds') or 0 for r in rows), calls_by_kind=dict(Counter(r['kind'] for r in rows)), tokens=totals,
                    known_estimated_cost=str(sum((Decimal(r['estimated_cost']) for r in rows if r['estimated_cost'] is not None), Decimal(0))),
                    estimated_cost=(None if any(r['estimated_cost'] is None for r in rows) else
                                    str(sum((Decimal(r['estimated_cost']) for r in rows), Decimal(0)))),
                    supplier_bill=None if rows else '0', currency=self.pricing.get('currency') if self.pricing else None,
                    pricing_status='confirmed' if self.pricing else 'not_provided', pause_reason=self.pause_reason)


@contextmanager
def run_lock(output):
    """OS lock releases on process death; an old lock file is safe to retain."""
    path = output / 'run.lock'
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('run is locked by another process') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
