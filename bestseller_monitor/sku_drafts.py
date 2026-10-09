"""本地 SKU 草稿：沿用库存有效段；不消费商品级判断或人工账本。"""
from __future__ import annotations

import base64
import json
from datetime import datetime

from .db import cst_date, SKU_IMAGE_OWN
from .parse import DEFAULT_SKU_ID, sku_name_source
from .sku_grouping import _new_group, refresh
from .sku_codes import code_evidence


def _time(value):
    return datetime.fromisoformat(value).timestamp()


def _evidence(conn, shop, offer, sku_id, latest, end):
    """最近 SKU 观测，不以最近商品观测/其他日期图片替代该 SKU 的证据。"""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    candidates=[]
    if 'sku_observation_batches' in tables:
        for batch in conn.execute('SELECT * FROM sku_observation_batches WHERE shop_key=? AND offer_id=? AND observed_date<=?', (shop,offer,end)):
            skus=json.loads(batch['skus_json'])
            count = len(skus) if all(s.get('observation_complete') is True for s in skus) else None
            for sku in skus:
                if sku['sku_id']==sku_id:
                    candidates.append((_time(batch['observed_at']), 1, batch['id'], dict(batch), sku, count))
    if 'snapshots' in tables:
        for row in conn.execute("SELECT * FROM snapshots WHERE shop_key=? AND offer_id=? AND sku_id=? AND page_status='成功'", (shop,offer,sku_id)):
            if cst_date(row['collected_at'])<=end:
                batch=dict(observed_at=row['collected_at'],product_name=row['product_name'],product_url=row['product_url'])
                candidates.append((_time(row['collected_at']), 0, row['id'], batch, dict(row), None))
    if candidates and latest['date'] > cst_date(max(candidates, key=lambda c:c[:3])[3]['observed_at']):
        candidates = []  # 库存是更晚观测，不能用早日完整快照冒充其证据。
    if candidates:
        _, _, _, batch, sku, count=max(candidates, key=lambda c:c[:3])
    else:
        batch=dict(observed_at=None,observed_date=latest['date'],product_name=latest['product_name'],product_url=None)
        sku=dict(sku_id=sku_id,sku_name=latest['sku_name'],sku_stock=latest['stock'],sku_price=latest['price'])
        count=None
    source=sku.get('name_source') or sku_name_source(sku_id, sku['sku_name'])
    id_source = sku.get('sku_id_source') or ('system_default' if sku_id == DEFAULT_SKU_ID else 'unknown')
    single = count==1 if count is not None else (True if source=='system_default' else None)
    note='' if count is not None else '历史观测完整性无法证明；不能推定单 SKU'
    if id_source == 'derived':
        note += '；平台 SKU 编号缺失，保留既有衍生编号，不能保证改名后身份连续'
    elif id_source == 'unknown':
        note += '；历史 SKU 编号来源无法恢复'
    if not batch['observed_at']:
        note += '；仅保留库存日期，精确时间与同次图片无法恢复'
    result = dict(product_name=batch['product_name'],raw_sku_name=sku['sku_name'],
        stock=sku['sku_stock'],price=sku['sku_price'],product_url=batch.get('product_url'),
        observed_at=batch['observed_at'],observed_date=cst_date(batch['observed_at']) if batch['observed_at'] else batch['observed_date'],observation_complete=count is not None,
        observation_sku_count=count,single_sku=single,name_source=source,sku_id_source=id_source,evidence_note=note,
        matching_input=dict(product_name=batch['product_name'],sku_name='' if single else sku['sku_name']))
    title, image = _image(conn,tables,batch,sku,shop,offer,sku_id)
    result.update(image)
    result['product_name'] = title
    result['matching_input']['product_name'] = title
    result.update(code_evidence(result))
    return result


def freeze(conn, analysis_id, start, end, calculate, frozen_at):
    rows = [dict(r) for r in conn.execute("""
        WITH selected AS (SELECT DISTINCT shop_key,offer_id FROM inventory
            WHERE date BETWEEN ? AND ? AND stock IS NOT NULL)
        SELECT i.* FROM inventory i JOIN selected s USING(shop_key,offer_id)
        WHERE i.stock IS NOT NULL AND i.date<=? ORDER BY shop_key,offer_id,sku_id,date
    """, (start,end,end))]
    if not rows:
        raise ValueError('该日期区间没有可分析的库存，请重新选择日期')
    members=[]
    for (shop,offer), bucket in sorted(calculate(rows,start,end).items()):
        for sku_id in sorted({s['sku_id'] for s in bucket['skus']}):
            parts=[s for s in bucket['skus'] if s['sku_id']==sku_id]
            observed=[r for r in rows if (r['shop_key'],r['offer_id'],r['sku_id'])==(shop,offer,sku_id) and r['date']<=end]
            latest=max(observed,key=lambda r:r['date'])
            points=[]
            for i in range(len(parts[0]['points'])):
                active=[s['points'][i] for s in parts if s['points'][i]['stock'] is not None]
                points.append(dict(active[0]) if active else dict(parts[0]['points'][i]))
                points[-1]['segment_start'] = bool(active) and bucket['points'][i].get('segment_start', False)
            members.append(dict(shop_key=shop,offer_id=offer,sku_id=sku_id,
                product_name=latest['product_name'],raw_sku_name=latest['sku_name'],
                stock=latest['stock'],price=latest['price'],sales=sum(s['sales'] for s in parts),
                points=points,shape_switches=bucket['shape_switches'],matching_state='unjudged',confirmed=False))
            members[-1].update(_evidence(conn,shop,offer,sku_id,latest,end))
    groups=[]
    for member in members:
        group=_new_group(member, group_id='sku-'+str(len(groups)+1))
        group['status']='待确认'
        groups.append(group)
    snapshot=dict(id=analysis_id,start=start,end=end,frozen_at=frozen_at,member_kind='sku',
                draft_version=3,inventory=rows,members=members,products=[],groups=groups,
                shops=[dict(s) for s in conn.execute('SELECT * FROM shops ORDER BY shop_key')],
                dirty=True,saved_at=None,conflicts=[],excluded=[],
                review_order=[g['id'] for g in groups],
                limitations=['无有效编码／编码歧义的图文判断尚未开放；多候选比较与跨机共享尚未开放'])
    refresh(snapshot)
    return snapshot

def _image(conn, tables, batch, sku, shop, offer, sku_id):
    observed_at=batch['observed_at']
    if 'skus_json' not in batch and observed_at:
        # 旧流水只能精确连到同次观测。重试图片的不同时间版本不是原观测证据。
        if 'sku_image_versions' in tables:
            row=conn.execute('SELECT * FROM sku_image_versions WHERE shop_key=? AND offer_id=? AND sku_id=? AND observed_at=? ORDER BY id DESC LIMIT 1',
                             (shop,offer,sku_id,observed_at)).fetchone()
            if row:
                sku=dict(sku,image_hash=row['content_hash'],image_url=row['image_url'],image_error=row['image_error'],image_source=row['source'])
        if 'product_information_versions' in tables:
            row=conn.execute('SELECT * FROM product_information_versions WHERE shop_key=? AND offer_id=? AND observed_at=? ORDER BY id DESC LIMIT 1',
                             (shop,offer,observed_at)).fetchone()
            if row:
                batch=dict(batch,product_name=row['product_name'],main_image_hash=row['content_hash'],main_image_url=row['image_url'],main_image_error=row['image_error'])
    own=sku.get('image_source')==SKU_IMAGE_OWN and sku.get('image_hash')
    image_hash=sku.get('image_hash') if own else batch.get('main_image_hash')
    source='sku' if own else ('product_main_same_observation' if image_hash else 'missing')
    url=sku.get('image_url') if own else batch.get('main_image_url')
    error=sku.get('image_error') if own else batch.get('main_image_error')
    asset=conn.execute('SELECT mime,content FROM product_image_assets WHERE content_hash=?',(image_hash,)).fetchone() if image_hash and 'product_image_assets' in tables else None
    data=None
    if asset and asset['content']:
        data='data:'+asset['mime']+';base64,'+base64.b64encode(asset['content']).decode('ascii')
    elif image_hash:
        error='历史图片资产不可用'
    else:
        error=error or '当次 SKU 图与商品主图均缺失'
    return batch['product_name'], dict(image_source=source,image_hash=image_hash,image_data=data,image_url=url,
                image_error=error,image_observed_at=observed_at)


def restore_curve_boundaries(snapshot, calculate):
    """Backfill display boundaries in ticket 01/02 drafts from frozen rows only."""
    if all('shape_switches' in member for member in snapshot['members']):
        return
    buckets = calculate(snapshot['inventory'], snapshot['start'], snapshot['end'])
    for member in snapshot['members']:
        bucket = buckets.get((member['shop_key'], member['offer_id']))
        if not bucket:
            continue
        member.setdefault('shape_switches', bucket['shape_switches'])
        starts = {point['date'] for point in bucket['points'] if point.get('segment_start')}
        for point in member['points']:
            point['segment_start'] = point['stock'] is not None and point['date'] in starts
