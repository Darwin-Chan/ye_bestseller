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
from .db import utcnow
from .matching import MatchingConfig, MatchingService, ModelConfig


@dataclass(frozen=True)
class AnalysisConfig:
    database: Path
    full_capture_weekday: int = 1  # ISO: Monday=1
    matching: MatchingConfig | None = None

    def __post_init__(self):
        if self.matching and self.matching.cache.resolve() == self.database.resolve():
            raise ValueError('同款缓存不能使用库存数据库')

    @classmethod
    def from_file(cls, path: Path) -> AnalysisConfig:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
            cfg = document['analysis']
        weekday = cfg.get("full_capture_weekday", 1)
        if type(weekday) is not int or not 1 <= weekday <= 7:
            raise ValueError("全量抓取提醒星期必须为 1 至 7")
        database = Path(cfg["database"])
        if not database.is_absolute():
            database = path.resolve().parent / database
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
        return cls(database.resolve(), weekday, matching)


class AnalysisService:
    def __init__(self, config: AnalysisConfig, *, running=None):
        self.config = config
        self.running = running or crawler_identity.is_running
        self._snapshots = {}
        self._lock = threading.Lock()
        self.matcher = MatchingService(config.matching) if config.matching and config.matching.mode != 'disabled' else None

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
                        product['image_data'] = 'data:'+version['mime']+';base64,'+base64.b64encode(version['content']).decode('ascii')
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
                    "shops": shops, "groups": [
                        {"id": f"G{i+1}", "confirmed": False,
                         "members": [{"shop_key": p["shop_key"], "offer_id": p["offer_id"]}],
                         "sales": p["sales"]}
                        for i, p in enumerate(products)]}
        with self._lock:
            self._match(snapshot)
            self._snapshots[snapshot["id"]] = snapshot
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
            self._snapshots[analysis_id] = snapshot
            return copy.deepcopy(snapshot)

    def get(self, analysis_id):
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                raise ValueError("分析已不存在，请重新选择日期；本阶段尚不支持关闭程序后恢复")
            return copy.deepcopy(snapshot)

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
            self._snapshots[analysis_id] = snapshot
            return copy.deepcopy(snapshot)


def _dates(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last-first).days + 1)]


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
        bucket = {'skus': skus, 'sales': sum(s['sales'] for s in skus), 'points': []}
        switches = {segment['start'] for segment in segments[1:]}
        for i, day in enumerate(dates):
            active = [s['points'][i] for s in skus if s['points'][i]['stock'] is not None]
            bucket['points'].append({'date': day, 'stock': sum(p['stock'] for p in active) if active else None,
                                    'segment_start': day in switches,
                                    'sales': sum(p['sales'] for p in active),
                                    'color': 'red' if any(p['color'] == 'red' for p in active) else 'green'})
        result[key] = bucket
    return result


def _calculate_segment(rows: list[dict], start: str, end: str) -> dict:
    """Build SKU-first inventory points; this is the single calculation seam for the UI."""
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
        bucket = products.setdefault(product_key, {"sales": 0, "points": [], "skus": []})
        bucket["skus"].append({"sku_id": key[2], "name": observations[-1].get("sku_name") or key[2], "sales": sum(p["sales"] for p in points), "points": points})
    for bucket in products.values():
        bucket["sales"] = sum(sku["sales"] for sku in bucket["skus"])
        bucket["points"] = [{
            "date": day,
            "stock": sum(sku["points"][i]["stock"] for sku in bucket["skus"] if sku["points"][i]["stock"] is not None) or None,
            "sales": sum(sku["points"][i]["sales"] for sku in bucket["skus"]),
            "color": "red" if any(sku["points"][i]["color"] == "red" for sku in bucket["skus"]) else "green",
        } for i, day in enumerate(dates)]
    return products
