"""独立分析入口：只读库存，一次一致读取产生与源库解耦的快照。"""
from __future__ import annotations

import copy
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


@dataclass(frozen=True)
class AnalysisConfig:
    database: Path
    full_capture_weekday: int = 1  # ISO: Monday=1

    @classmethod
    def from_file(cls, path: Path) -> AnalysisConfig:
        with path.open("rb") as stream:
            cfg = tomllib.load(stream)["analysis"]
        weekday = cfg.get("full_capture_weekday", 1)
        if type(weekday) is not int or not 1 <= weekday <= 7:
            raise ValueError("全量抓取提醒星期必须为 1 至 7")
        database = Path(cfg["database"])
        if not database.is_absolute():
            database = path.resolve().parent / database
        return cls(database.resolve(), weekday)


class AnalysisService:
    def __init__(self, config: AnalysisConfig, *, running=None):
        self.config = config
        self.running = running or crawler_identity.is_running
        self._snapshots = {}
        self._lock = threading.Lock()

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
            self._snapshots[snapshot["id"]] = snapshot
        return copy.deepcopy(snapshot)

    def get(self, analysis_id):
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                raise ValueError("分析已不存在，请重新选择日期；本阶段尚不支持关闭程序后恢复")
            return copy.deepcopy(snapshot)

    def confirm(self, analysis_id, group_id):
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                raise ValueError("分析已不存在，请重新选择日期")
            group = next((item for item in snapshot["groups"] if item["id"] == group_id), None)
            if group is None:
                raise ValueError("同款组不存在")
            group["confirmed"] = True
            return copy.deepcopy(snapshot)


def _dates(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last-first).days + 1)]


def calculate_inventory(rows: list[dict], start: str, end: str) -> dict:
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
