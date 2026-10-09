"""Shared roll-up of already calculated inventory series; no sales inference."""
def aggregate_points(series: list[list[dict]]) -> list[dict]:
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

