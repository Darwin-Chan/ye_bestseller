"""Frozen research inputs; no runtime database/config loader is used."""
from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from bestseller_monitor.db import cst_date

from PIL import Image
from bestseller_monitor.matching import MatchingConfig, ModelConfig, digest
from bestseller_monitor.sku_codes import code_evidence
from bestseller_monitor.sku_grouping import _new_group, apply_codes, apply_saved, identity, refresh
from bestseller_monitor.sku_matching import evidence_version

REPO = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save_json(path, value):
    """Replace one checkpoint atomically after flushing its bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def source_version():
    files = sorted((REPO / 'bestseller_monitor').glob('*.py')) + [
        REPO / 'tools' / name for name in ('run_sku_baseline.py', 'sku_baseline_io.py', 'sku_baseline_transport.py')]
    hashes = {p.relative_to(REPO).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}
    try:
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True, stderr=subprocess.DEVNULL).strip()
        status = subprocess.check_output(['git', 'status', '--porcelain=v1', '--untracked-files=all'],
                                         cwd=REPO, text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        head, status = 'unknown', 'unknown'
    return dict(head=head, worktree_status=status, files=hashes, source_digest=digest(hashes))


def load_config(path):
    # Only the matching section is read. Runtime database/cache paths are never opened.
    raw = tomllib.loads(Path(path).read_text(encoding='utf-8-sig'))['matching']
    fields = ('mode', 'concurrency', 'candidates', 'min_score')
    defaults = MatchingConfig(cache=Path('unused'))
    frozen = {k: raw.get(k, getattr(defaults, k)) for k in fields}
    for kind in ('model', 'vision'):
        model = raw.get(kind)
        frozen[kind] = ({k: model.get(k, getattr(ModelConfig(), k)) for k in ('endpoint', 'model', 'key_env', 'timeout')}
                        if model is not None else None)
    for model in (frozen['model'], frozen['vision']):
        if model is not None:
            url = urlsplit(model['endpoint'])
            if url.username or url.password or url.query or url.fragment:
                raise ValueError('endpoint must not embed credentials, query or fragment')
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', model['key_env']):
                raise ValueError('invalid key environment variable name')
    if frozen['model'] is None:
        raise ValueError('matching.model must be explicit')
    make_config(frozen, Path('unused'))
    return frozen


def make_config(frozen, cache, *, offline=False):
    values = copy.deepcopy(frozen)
    for kind in ('model', 'vision'):
        if values[kind] is not None:
            if offline:
                values[kind]['key_env'] = 'SKU_BASELINE_OFFLINE_KEY'
            values[kind] = ModelConfig(**values[kind])
    return MatchingConfig(cache=Path(cache), **values)


def safe_id(value):
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM' + str(i) for i in range(1, 10)} | {'LPT' + str(i) for i in range(1, 10)}
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value)
            or value.upper() in reserved):
        raise ValueError('set id must contain only letters, digits, dash or underscore')
    return value


def check_pack(pack, sets):
    if pack is None:
        raise ValueError('real input requires --pack for candidate boundary verification')
    with (Path(pack) / 'members.csv').open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    with (Path(pack) / 'sets.csv').open(encoding='utf-8-sig', newline='') as stream:
        original_sets = list(csv.DictReader(stream))
    expected = [(r['set_id'], identity(r)) for r in rows]
    actual = [(s['id'], identity(m)) for s in sets for m in s['members']]
    if len(original_sets) != 34 or len(rows) != 536 or len(set(expected)) != 536:
        raise ValueError('original sample boundary must be 34 sets / 536 unique memberships')
    if len(sets) != 34 or sorted(expected) != sorted(actual):
        raise ValueError('candidate memberships differ from original sample pack')
    originals = {r['set_id']: r for r in original_sets}
    if len(originals) != 34 or set(originals) != {s['id'] for s in sets}:
        raise ValueError('original candidate set identities are invalid')
    for s in sets:
        original = originals.get(s['id'], {})
        if int(original['candidate_size']) != len(s['members']):
            raise ValueError('candidate size differs from original sample pack')
        for key in ('stratum', 'candidate_source'):
            if s.get(key) != original.get(key):
                raise ValueError('candidate metadata differs from original sample pack')
    return {name: hashlib.sha256((Path(pack) / name).read_bytes()).hexdigest() for name in ('sets.csv', 'members.csv')}


def load_input(path, *, pack=None):
    path = Path(path).resolve()
    data = read_json(path)
    if data.get('schema') != 1 or type(data.get('synthetic')) is not bool or not data.get('provenance'):
        raise ValueError('schema=1, explicit synthetic flag and provenance are required')
    analysis = data['analysis']
    start, end = date.fromisoformat(analysis['start']), date.fromisoformat(analysis['end'])
    if start > end:
        raise ValueError('analysis start is after end')
    sets = data['sets']
    if not sets:
        raise ValueError('empty candidate boundary')
    seen_sets = set()
    assets = {}
    for s in sets:
        sid = safe_id(s['id'])
        if sid.casefold() in seen_sets:
            raise ValueError('duplicate set id')
        seen_sets.add(sid.casefold())
        if s.get('stratum') not in ('1-10', '11-25', '26-50') or not s.get('candidate_source') or not s['members']:
            raise ValueError('candidate metadata or members missing')
        seen = set()
        for m in s['members']:
            required = ('shop_key', 'offer_id', 'sku_id', 'product_name', 'raw_sku_name', 'single_sku',
                        'name_source', 'matching_input', 'observed_at', 'observed_date', 'image_hash',
                        'image_source', 'image_error', 'evidence_version', 'provenance')
            if any(k not in m for k in required) or any(not isinstance(m[k], str) or not m[k] for k in ('shop_key', 'offer_id', 'sku_id')):
                raise ValueError('member evidence fields missing')
            key = identity(m)
            if key in seen:
                raise ValueError('duplicate SKU membership')
            seen.add(key)
            if (m['single_sku'] is not None and type(m['single_sku']) is not bool) or not m['provenance']:
                raise ValueError('explicit single_sku (true/false/null) and member provenance required')
            if m['single_sku'] is None and not m.get('evidence_note'):
                raise ValueError('unknown single_sku requires evidence_note')
            if m['matching_input'] != dict(product_name=m['product_name'], sku_name='' if m['single_sku'] else m['raw_sku_name']):
                raise ValueError('matching_input does not match production SKU semantics')
            m.update(code_evidence(m))
            if evidence_version(m) != m['evidence_version']:
                raise ValueError('production evidence_version mismatch')
            if m['observed_date']:
                if date.fromisoformat(m['observed_date']) > end:
                    raise ValueError('evidence is later than analysis end')
            if m['observed_at']:
                if not m['observed_date'] or cst_date(m['observed_at']) != m['observed_date']:
                    raise ValueError('observation timestamp/date mismatch')
            # Unavailable historical assets stay missing; invalid supplied assets are errors.
            for field in ('image_data', 'image_judgments', 'recommendation', 'candidate_groups',
                          'multiple_candidates', 'model_work', 'matching_state', 'added_to_group'):
                m.pop(field, None)
            asset = m.get('asset_path')
            if asset:
                if not m['observed_at'] or m['image_source'] not in ('sku', 'product_main_same_observation'):
                    raise ValueError('image requires same-observation evidence/source')
                image_path = (path.parent / asset).resolve()
                raw = image_path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != m['image_hash']:
                    raise ValueError('image content hash mismatch')
                with Image.open(io.BytesIO(raw)) as image:
                    image.verify()
                    mime = Image.MIME.get(image.format)
                if mime not in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'):
                    raise ValueError('unsupported image format')
                m['image_data'] = 'data:' + mime + ';base64,' + base64.b64encode(raw).decode('ascii')
                assets[str(image_path)] = m['image_hash']
            else:
                m['image_data'] = None
                if not m['image_error']:
                    raise ValueError('missing image requires an explicit reason')
            # Research has no inventory/stock claim.
            m.update(sales=0, points=[])
        manual = s.get('manual')
        if manual:
            if manual.get('confirmed_applicable') is not True or not manual.get('provenance'):
                raise ValueError('manual constraints require explicit applicability confirmation/source')
            saved = manual['snapshot']
            saved_members = {identity(m): m for m in saved['members']}
            current = {identity(m): m for m in s['members']}
            if len(saved_members) != len(saved['members']) or not set(saved_members) <= set(current):
                raise ValueError('manual scope outside candidate boundary')
            for key, member in saved_members.items():
                if evidence_version(member) != evidence_version(current[key]):
                    raise ValueError('manual evidence version mismatch')
            refs = {identity(r) for g in saved['groups'] for r in g['members']}
            exclusions = set(saved.get('excluded_skus', [])) | {k for pair in saved.get('excluded', []) for k in pair}
            if not refs <= set(saved_members) or not exclusions <= set(current):
                raise ValueError('manual references outside frozen scope')
    pack_hashes = check_pack(pack, sets) if not data['synthetic'] else None
    return data, dict(manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), assets=assets, pack=pack_hashes)


def snapshot_for(data, candidate):
    members = copy.deepcopy(candidate['members'])
    snapshot = dict(id=candidate['id'], start=data['analysis']['start'], end=data['analysis']['end'],
                    frozen_at=now(), member_kind='sku', draft_version=3, inventory=[], members=members,
                    products=[], groups=[_new_group(m, group_id='sku-' + str(i + 1)) for i, m in enumerate(members)],
                    shops=[], dirty=True, saved_at=None, conflicts=[], excluded=[], limitations=[])
    refresh(snapshot)
    if candidate.get('manual'):
        saved = copy.deepcopy(candidate['manual']['snapshot'])
        # Only explicitly adopted human constraints may enter a new research run.
        for member in saved['members']:
            for field in ('image_judgments', 'recommendation', 'candidate_groups', 'multiple_candidates'):
                member.pop(field, None)
        apply_saved(snapshot, saved)
    apply_codes(snapshot)
    return snapshot
