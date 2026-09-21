"""独立分析入口：只读库存，一次一致读取产生与源库解耦的快照。"""
from __future__ import annotations

import copy
import base64
import sqlite3
import threading
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from . import crawler_identity
from .analysis_store import DraftStore
from .db import utcnow
from .matching import MatchingConfig, MatchingService, ModelConfig, identity, summarize_group, version

DEFAULT_DRAFT_FILE = "analysis-drafts.sqlite"


@dataclass(frozen=True)
class AnalysisConfig:
    database: Path
    full_capture_weekday: int = 1  # ISO: Monday=1
    matching: MatchingConfig | None = None
    # 分析草稿库。直接构造时缺省与库存库同目录；配置文件里缺省在配置目录（见 from_file）。
    store: Path | None = None

    def __post_init__(self):
        if self.store is None:
            object.__setattr__(self, 'store', self.database.with_name(DEFAULT_DRAFT_FILE))
        if self.matching and self.matching.cache.resolve() == self.database.resolve():
            raise ValueError('同款缓存不能使用库存数据库')
        reserved = {self.database.resolve()}
        if self.matching:
            reserved.add(self.matching.cache.resolve())
        if self.store.resolve() in reserved:
            raise ValueError('分析草稿库不能与库存数据库或同款缓存共用文件')

    @classmethod
    def from_file(cls, path: Path) -> AnalysisConfig:
        try:
            with path.open("rb") as stream:
                document = tomllib.load(stream)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"找不到分析配置：{path}\n"
                f"请复制 {path.with_name('analysis.example.toml')} 为 {path}，"
                "再改成本机的值（database、matching.cache 等）。"
            ) from None
        cfg = document['analysis']
        weekday = cfg.get("full_capture_weekday", 1)
        if type(weekday) is not int or not 1 <= weekday <= 7:
            raise ValueError("全量抓取提醒星期必须为 1 至 7")
        database = Path(cfg["database"])
        if not database.is_absolute():
            database = path.resolve().parent / database
        store = Path(cfg.get("store", DEFAULT_DRAFT_FILE))
        if not store.is_absolute():
            store = path.resolve().parent / store
        matching = None
        if 'matching' in document:
            options = document['matching']
            cache = Path(options.get('cache', 'matching.sqlite'))
            if not cache.is_absolute():
                cache = path.resolve().parent / cache
            if cache.resolve() == database.resolve():
                raise ValueError('同款缓存不能使用库存数据库')
            matching = MatchingConfig(cache.resolve(), ModelConfig(**options.get('model', {})),
                ModelConfig(**options['vision']) if 'vision' in options else None,
                options.get('mode', 'disabled'), options.get('concurrency', 2), options.get('candidates', 6))
        return cls(database.resolve(), weekday, matching, store.resolve())


class AnalysisService:
    def __init__(self, config: AnalysisConfig, *, running=None):
        self.config = config
        self.running = running or crawler_identity.is_running
        self._snapshots = {}
        # 本次分析开始时账本记录过的商品版本，供信息变更标注在模型重试后仍然成立。
        self._recorded_versions = {}
        self._lock = threading.Lock()
        self.matcher = MatchingService(config.matching) if config.matching and config.matching.mode != 'disabled' else None

    @property
    def store(self):
        """草稿库跟着当前配置走：配置换了库或换了存储位置立即生效。"""
        return DraftStore(self.config.store)

    @contextmanager
    def _read(self):
        # 不调用采集 connect：分析读取不得建表、迁移或清理采集身份。
        conn = sqlite3.connect(self.config.database.resolve().as_uri()+"?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def settings(self):
        return {"full_capture_weekday": self.config.full_capture_weekday}

    def coverage(self, day):
        date.fromisoformat(day)
        with self._read() as conn:
            return [dict(row) for row in conn.execute("""
                WITH scope AS (
                    SELECT shop_key, shop_name FROM shops
                    UNION ALL
                    SELECT shop_key, MAX(shop_name) FROM inventory
                    WHERE shop_key NOT IN (SELECT shop_key FROM shops) GROUP BY shop_key
                ), counts AS (
                    SELECT shop_key, COUNT(DISTINCT offer_id) AS products, COUNT(*) AS skus
                    FROM inventory WHERE date=? AND stock IS NOT NULL GROUP BY shop_key
                ) SELECT scope.shop_key, scope.shop_name,
                    COALESCE(counts.products,0) AS products, COALESCE(counts.skus,0) AS skus
                  FROM scope LEFT JOIN counts USING(shop_key) ORDER BY scope.shop_key
            """, (day,))]

    def start(self, start, end, acknowledged=False):
        if date.fromisoformat(start) >= date.fromisoformat(end):
            raise ValueError("结束日期必须晚于开始日期")
        if self.running() and not acknowledged:
            return {"needs_confirmation": True}
        with self._read() as conn:
            # 入选商品只由区间内真实库存决定。其所有历史行同时冻结，供
            # 区间外基准、SKU 身份和后续规格有效段计算使用。
            rows = [dict(row) for row in conn.execute("""
                WITH selected AS (
                    SELECT DISTINCT shop_key, offer_id FROM inventory
                    WHERE date BETWEEN ? AND ? AND stock IS NOT NULL
                ) SELECT i.* FROM inventory i JOIN selected s
                  ON i.shop_key=s.shop_key AND i.offer_id=s.offer_id
                  WHERE i.stock IS NOT NULL
                  ORDER BY i.shop_key,i.offer_id,i.sku_id,i.date
            """, (start, end))]
            if not rows:
                raise ValueError("该日期区间没有可分析的库存，请重新选择日期")
            products = [dict(row) for row in conn.execute("""
                SELECT DISTINCT i.shop_key,i.offer_id,p.product_url,p.product_name,
                    p.main_image_url,p.last_seen_at
                FROM inventory i LEFT JOIN products p ON p.offer_id=i.offer_id
                WHERE i.date BETWEEN ? AND ? AND i.stock IS NOT NULL
                ORDER BY i.shop_key,i.offer_id
            """, (start, end))]
            shops = [dict(row) for row in conn.execute("SELECT * FROM shops ORDER BY shop_key")]
            has_history = conn.execute("SELECT 1 FROM sqlite_master WHERE name='product_information_versions'").fetchone()
            for product in products:
                version = conn.execute('''SELECT v.*, a.mime, a.content
                    FROM product_information_versions v LEFT JOIN product_image_assets a
                    ON a.content_hash=v.content_hash
                    WHERE v.shop_key=? AND v.offer_id=? AND v.observed_date<=?
                    ORDER BY v.observed_date DESC,v.observed_at DESC,v.id DESC LIMIT 1''',
                    (product['shop_key'], product['offer_id'], end)).fetchone() if has_history else None
                product['image_data'] = None
                product['image_error'] = '该日期没有历史图片'
                product['information_version'] = version['id'] if version else None
                product['information_complete'] = bool(version and version['product_name'] and version['content_hash'])
                product['information_note'] = ''
                if version:
                    product['product_name'] = version['product_name']
                    product['image_hash'] = version['content_hash']
                    product['image_error'] = version['image_error']
                    if not version['product_name']:
                        product['information_note'] = '仅图片证据：名称待重新观测，尚未形成完整商品信息版本'
                    if version['content']:
                        product['image_data'] = _data_url(version['mime'], version['content'])
                else:
                    historical = [r for r in rows if r['shop_key']==product['shop_key'] and r['offer_id']==product['offer_id'] and r['date']<=end]
                    product['product_name'] = max(historical, key=lambda r:r['date'])['product_name'] if historical else None
                product['main_image_url'] = None
        calculations = calculate_inventory(rows, start, end)
        for product in products:
            key = (product["shop_key"], product["offer_id"])
            result = calculations.get(key, {"sales": 0, "points": [], "skus": []})
            product.update(result)
        snapshot = {"id": uuid4().hex, "start": start, "end": end,
                    "frozen_at": utcnow(), "inventory": rows, "products": products,
                    "shops": shops, "dirty": False, "saved_at": None, "groups": []}
        # 「继续上次分析」读原快照；新日期区间走这里：重算销量后，把已保存的
        # 人工确认按商品版本套回来（票 09），不适用的留在待确认由人工处理。
        recorded = reuse_decisions(snapshot, self.store.ledger())
        with self._lock:
            self._match(snapshot)
            mark_information_changes(snapshot, recorded)
            rank_groups(snapshot)
            self._snapshots[snapshot["id"]] = snapshot
            self._recorded_versions[snapshot["id"]] = recorded
        return copy.deepcopy(snapshot)

    def _match(self, snapshot):
        if self.matcher:
            snapshot['groups'] = self.matcher.suggest(snapshot['products'], snapshot['groups'], snapshot.get('excluded', ()))
        else:
            for p in snapshot['products']:
                p.update(origin='新商品', match_label='暂无匹配同款', candidate_groups=[], matching_status='模型匹配未启用')

    def retry_matching(self, analysis_id):
        with self._lock:
            if analysis_id not in self._snapshots:
                raise ValueError('分析已不存在')
            snapshot = copy.deepcopy(self._snapshots[analysis_id])
            self._match(snapshot)
            mark_information_changes(snapshot, self._recorded_versions.get(analysis_id, {}))
            rank_groups(snapshot)
            self._snapshots[analysis_id] = snapshot
            return copy.deepcopy(snapshot)

    def get(self, analysis_id):
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                # 内存里没有就回落到已保存版本：重启后仍能打开同一次分析。
                stored = self.store.read(analysis_id)
                if stored is None:
                    raise ValueError("分析已不存在，请重新选择日期")
                snapshot = self._restore(stored)
                self._snapshots[analysis_id] = snapshot
            return copy.deepcopy(snapshot)

    def _restore(self, stored):
        """把磁盘上的草稿还原成内存工作草稿：已保存版本没有未保存修改。"""
        snapshot = self._rehydrate(stored.payload)
        snapshot['dirty'] = False
        snapshot['saved_at'] = stored.saved_at
        return snapshot

    def draft(self):
        """最近一次成功保存的草稿摘要，供「继续上次分析」入口显示。"""
        latest = self.store.latest()
        if latest is None:
            return {"available": False}
        return {"available": True, "id": latest['id'], "start": latest['start'],
                "end": latest['end'], "saved_at": latest['saved_at']}

    def save_draft(self, analysis_id):
        return self._save(analysis_id, require_confirmed=False)

    def save_and_view(self, analysis_id):
        return self._save(analysis_id, require_confirmed=True)

    def _save(self, analysis_id, require_confirmed):
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                raise ValueError('分析已不存在，请重新选择日期')
            if require_confirmed and any(not group['confirmed'] for group in snapshot['groups']):
                raise ValueError('还有待确认的同款组，请先完成确认')
            saved_at = utcnow()
            # 先落盘再改内存：写失败时本次修改仍留在页面，并保持未保存标记。
            # 草稿版本与决策账本同一次事务提交，页面此刻的确认/排除即全局最新。
            self.store.write(analysis_id, snapshot['start'], snapshot['end'], saved_at,
                             self._draft_payload(snapshot, saved_at),
                             merge_decisions(snapshot, self.store.ledger()))
            snapshot['dirty'] = False
            snapshot['saved_at'] = saved_at
            return copy.deepcopy(snapshot)

    def discard(self, analysis_id):
        """放弃未保存的修改：回到最近成功保存的版本；从未保存过则整体丢弃。"""
        with self._lock:
            stored = self.store.read(analysis_id)
            if stored is None:
                self._snapshots.pop(analysis_id, None)
                return {"reverted": False}
            self._snapshots[analysis_id] = self._restore(stored)
            return {"reverted": True}

    def _draft_payload(self, snapshot, saved_at):
        """落盘的是这次保存后的完整版本：不带未保存标记，时间是本次保存时间。

        图片不进草稿正文：只存内容哈希，读回时从持久图片资产取内容。
        """
        payload = copy.deepcopy(snapshot)
        payload['dirty'] = False
        payload['saved_at'] = saved_at
        for product in payload['products']:
            product['image_data'] = None
        return payload

    def _rehydrate(self, payload):
        hashes = sorted({p['image_hash'] for p in payload['products'] if p.get('image_hash')})
        assets = {}
        if hashes:
            with self._read() as conn:
                for offset in range(0, len(hashes), 400):
                    chunk = hashes[offset:offset+400]
                    placeholders = ','.join('?' * len(chunk))
                    for row in conn.execute(f"SELECT content_hash,mime,content FROM product_image_assets"
                                            f" WHERE content_hash IN ({placeholders})", chunk):
                        assets[row['content_hash']] = row
        for product in payload['products']:
            product['image_data'] = None
            asset = assets.get(product.get('image_hash'))
            if asset and asset['content']:
                product['image_data'] = _data_url(asset['mime'], asset['content'])
            elif product.get('image_hash') and not product.get('image_error'):
                product['image_error'] = '历史图片资产不可用'
        return payload

    def confirm(self, analysis_id, group_id):
        return self.confirm_groups(analysis_id, [group_id])

    def confirm_groups(self, analysis_id, group_ids):
        return self._set_confirmation(analysis_id, group_ids, True)

    def withdraw(self, analysis_id, group_id):
        return self._set_confirmation(analysis_id, [group_id], False)

    def _set_confirmation(self, analysis_id, group_ids, confirmed):
        if not isinstance(group_ids, list) or not all(isinstance(g, str) for g in group_ids):
            raise ValueError('请选择有效同款组')
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                raise ValueError("分析已不存在，请重新选择日期")
            targets = set(group_ids)
            groups = [g for g in snapshot['groups'] if g['id'] in targets]
            if len(groups) != len(targets):
                raise ValueError("同款组不存在")
            for group in groups:
                if group['confirmed'] != confirmed:
                    group['confirmed'] = confirmed
                    # A withdrawn human decision must survive model retries.
                    group['adjusted'] = True
                    snapshot['dirty'] = True
            return copy.deepcopy(snapshot)

    def source(self, offer_id):
        with self._read() as conn:
            row = conn.execute('SELECT product_url FROM products WHERE offer_id=?', (offer_id,)).fetchone()
            return {'url': row['product_url'] if row else None}

    def edit_group(self, analysis_id, action, group_id, member, target_id=None):
        """Commit an atomic edit only to the in-memory analysis draft."""
        from .grouping import edit_group
        with self._lock:
            if analysis_id not in self._snapshots:
                raise ValueError('分析已不存在')
            snapshot = copy.deepcopy(self._snapshots[analysis_id])
            edit_group(snapshot, action, group_id, member, target_id, self.matcher)
            rank_groups(snapshot)
            self._snapshots[analysis_id] = snapshot
            return copy.deepcopy(snapshot)


def _dates(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last-first).days + 1)]


def _data_url(mime: str, content: bytes) -> str:
    return 'data:' + mime + ';base64,' + base64.b64encode(content).decode('ascii')


def calculate_inventory(rows: list[dict], start: str, end: str) -> dict:
    """Infer representation segments from frozen observations, never SKU names."""
    products = {}
    for row in rows:
        products.setdefault((row['shop_key'], row['offer_id']), []).append(row)
    result = {}
    for key, observations in products.items():
        daily = {}
        for row in observations:
            daily.setdefault(row['date'], []).append(row)
        segments = []
        for day, observed in sorted(daily.items()):
            shape = {r['sku_id'] == 'default' for r in observed}
            if len(shape) != 1:
                raise ValueError('同日存在相反规格形态，无法分析')
            shape = shape.pop()
            if not segments or segments[-1]['shape'] != shape:
                segments.append({'start': day, 'shape': shape, 'rows': []})
            segments[-1]['rows'].extend(observed)
        skus = []
        dates = _dates(start, end)
        for index, segment in enumerate(segments):
            lower = max(start, segment['start']) if index else start
            upper = min(end, (date.fromisoformat(segments[index+1]['start']) - timedelta(days=1)).isoformat()) if index+1 < len(segments) else end
            if lower > upper:
                continue
            calculated = _calculate_segment(segment['rows'], lower, upper)[key]
            first_day = min(r['date'] for r in segment['rows'])
            for sku in calculated['skus']:
                first = min(r['date'] for r in segment['rows'] if r['sku_id'] == sku['sku_id'])
                # A newly appearing SKU cannot be backfilled into an established shape.
                active_start = max(lower, first) if first > first_day else lower
                points = {p['date']: p for p in sku['points'] if p['date'] >= active_start}
                sku['points'] = [points.get(day, {'date': day, 'stock': None, 'sales': 0, 'color': 'inactive', 'actual': False}) for day in dates]
                sku['segment'] = index + 1
                sku['sales'] = sum(p['sales'] for p in sku['points'])
                skus.append(sku)
        bucket = {'skus': skus, 'sales': sum(s['sales'] for s in skus),
                  'points': _aggregate_points([s['points'] for s in skus])}
        switches = {segment['start'] for segment in segments[1:]}
        for point in bucket['points']:
            point['segment_start'] = point['date'] in switches
        result[key] = bucket
    return result


def _calculate_segment(rows: list[dict], start: str, end: str) -> dict:
    """Build SKU-first inventory points: the per-SKU daily rows every layer aggregates from."""
    dates = _dates(start, end)
    by_sku = {}
    for row in rows:
        by_sku.setdefault((row["shop_key"], row["offer_id"], row["sku_id"]), []).append(row)
    products = {}
    for key, observations in by_sku.items():
        observations.sort(key=lambda row: row["date"])
        values = {row["date"]: row["stock"] for row in observations}
        before = [d for d in values if d < start]
        # When the start day has no observation, the nearest later observation
        # may be inside the selected interval (for example on the end day).
        after = [d for d in values if d > start]
        prior = values[max(before)] if before else None
        fallback = values[min(after)] if after else None
        previous = None
        points = []
        for day in dates:
            actual = day in values
            stock = values[day] if actual else (previous if previous is not None else (prior if prior is not None else fallback))
            if not actual and previous is None and prior is None and fallback is None:
                stock = None
            sales = 0 if previous is None or stock is None else max(previous-stock, 0)
            if day == start:
                sales = 0
            color = "yellow" if not actual and stock is not None else (
                "red" if previous is not None and stock is not None and stock > previous else "green"
            )
            points.append({"date": day, "stock": stock, "sales": sales, "color": color, "actual": actual})
            previous = stock
        product_key = key[:2]
        products.setdefault(product_key, {"skus": []})["skus"].append(
            {"sku_id": key[2], "name": observations[-1].get("sku_name") or key[2],
             "sales": sum(p["sales"] for p in points), "points": points})
    return products


def rank_groups(snapshot: dict) -> None:
    """组级排名视图：组序、成员次序、组总销量与组图逐日点只从这里产出（规格 §4、§6、§14）。

    先按同款分组把成员收成稳定次序（销量降序，并列按身份），再按组总销量降序给出组序
    （并列保持原顺序，稳定），最后把成员的逐日点聚到组层。冻结后快照入库前调用一次，
    之后的读取与导出都消费同一份结果，页面不再自己重算销量或排序。只依赖快照数据，
    可以安全地对同一份快照反复调用。
    """
    products = {identity(p): p for p in snapshot['products']}
    for group in snapshot['groups']:
        summarize_group(group, products)
        group['points'] = _aggregate_points([products[identity(m)]['points'] for m in group['members']])
    snapshot['ranking'] = [group['id'] for group in sorted(snapshot['groups'], key=lambda group: -group['sales'])]


def _aggregate_points(series: list[list[dict]]) -> list[dict]:
    """把多个成员的逐日点聚合成上一层：库存只累加当日有效观测（全缺即未知），销量求和，补货向上汇总。"""
    if not series:
        return []
    aggregated = []
    for index, lead in enumerate(series[0]):
        active = [member[index] for member in series if member[index]['stock'] is not None]
        aggregated.append({
            'date': lead['date'],
            'stock': sum(point['stock'] for point in active) if active else None,
            'sales': sum(member[index]['sales'] for member in series),
            'color': 'red' if any(member[index]['color'] == 'red' for member in series) else 'green',
            'segment_start': any(member[index].get('segment_start') for member in series),
        })
    return aggregated


def reuse_decisions(snapshot: dict, decisions: dict) -> dict:
    """把账本里已保存的人工确认套到新分析的成员上，返回账本记录过的商品版本。

    关系按成员逐一核对证据版本：版本未变的成员沿用确认，版本变了的不套旧确认；
    版本未变的其余成员照旧成组（票 09）。不适用与未参与的商品留在待确认。
    排除关系按商品身份生效，不受版本影响；只有两端都在本次分析里才加载。
    """
    products = snapshot['products']
    current = {identity(p): version(p) for p in products}
    recorded = {}
    parent = {}

    def find(member):
        while parent.setdefault(member, member) != member:
            member = parent[member]
        return member

    confirmed = set()
    for relation in decisions['relations']:
        for member, member_version in relation:
            recorded.setdefault(member, set()).add(member_version)
        intact = [member for member, member_version in relation if current.get(member) == member_version]
        for member in intact[1:]:
            parent[find(intact[0])] = find(member)
        confirmed.update(intact)
    for member, member_version in decisions['standalone']:
        recorded.setdefault(member, set()).add(member_version)
        if current.get(member) == member_version:
            confirmed.add(member)
    groups = []
    clusters = {}
    for p in products:
        member = identity(p)
        if member in confirmed:
            root = find(member)
            if root not in clusters:
                clusters[root] = {'id': f'G{len(groups)+1}', 'confirmed': True, 'members': []}
                groups.append(clusters[root])
            clusters[root]['members'].append({'shop_key': p['shop_key'], 'offer_id': p['offer_id']})
        else:
            groups.append({'id': f'G{len(groups)+1}', 'confirmed': False,
                           'members': [{'shop_key': p['shop_key'], 'offer_id': p['offer_id']}]})
    snapshot['groups'] = groups
    snapshot['excluded'] = [list(pair) for pair in decisions['excluded']
                            if pair[0] in current and pair[1] in current]
    return recorded


def mark_information_changes(snapshot: dict, recorded: dict) -> None:
    """账本里记录过的版本与当前版本不一致的商品标为信息变更：旧确认不适用于新证据。"""
    for product in snapshot['products']:
        versions = recorded.get(identity(product))
        if versions and version(product) not in versions:
            product['origin'] = '信息变更'


def merge_decisions(snapshot: dict, decisions: dict) -> dict:
    """保存这次人工整理后的完整账本。

    确认的多商品组按在场成员写回关系；成员这次没参与（新区间不显示）时关系
    保留旧版本，只把在场成员更新为当前版本——保存局部区间不能抹掉全局历史
    关系与排除（票 09）。在场却已被人工挪出该组的成员按最新决定移出关系。
    """
    present = {identity(p): version(p) for p in snapshot['products']}
    group_of = {identity(m): g for g in snapshot['groups'] for m in g['members']}
    clusters = {}
    for group in snapshot['groups']:
        if group['confirmed'] and len(group['members']) > 1:
            clusters[group['id']] = {identity(m): present[identity(m)] for m in group['members']}
    relations = []
    for relation in decisions['relations']:
        in_view = [member for member, _ in relation if member in present]
        if not in_view:
            relations.append([list(member) for member in relation])
            continue
        targets = {group_of[member]['id'] for member in in_view}
        if len(targets) == 1:
            (gid,) = targets
            if gid in clusters:
                for member, member_version in relation:
                    if member not in present:
                        clusters[gid].setdefault(member, member_version)
                continue
        remaining = [(member, member_version) for member, member_version in relation if member not in present]
        if len(remaining) > 1:
            relations.append([list(member) for member in remaining])
    for members in clusters.values():
        relations.append([[member, member_version] for member, member_version in sorted(members.items())])
    standalone = []
    for p in snapshot['products']:
        group = group_of[identity(p)]
        if group['confirmed'] and len(group['members']) == 1:
            standalone.append([identity(p), present[identity(p)]])
    excluded = [list(pair) for pair in decisions['excluded']
                if not (pair[0] in present and pair[1] in present)]
    excluded += [list(pair) for pair in snapshot.get('excluded', [])]
    return {'relations': sorted(relations), 'standalone': sorted(standalone),
            'excluded': sorted({tuple(pair) for pair in excluded})}
