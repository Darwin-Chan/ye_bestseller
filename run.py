"""1688 店铺畅销榜 × SKU 库存快照 MVP 入口。

用法：
    python run.py                          # 开始或续跑一轮
    python run.py --pages-per-shop 2       # 冒烟：每家店只翻 2 页
    python run.py --max-detail 60          # 冒烟：全轮最多 60 个详情页
    python run.py --limit-shops A01,A02    # 只处理指定店铺
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT))

from bestseller_monitor.config import Config, load_shops  # noqa: E402
from bestseller_monitor.db import Database, connect  # noqa: E402
from bestseller_monitor.pipeline import run_round  # noqa: E402
from bestseller_monitor import sound  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=ROOT / "config" / "config.toml")
    p.add_argument("--shops", type=Path, default=None, help="覆盖 config 中的店铺 CSV")
    p.add_argument("--pages-per-shop", type=int, default=None, help="覆盖每家店翻页上限")
    p.add_argument("--max-detail", type=int, default=None, help="覆盖单轮详情页上限")
    p.add_argument("--limit-shops", type=str, default=None, help="逗号分隔的 shop_key 白名单")
    p.add_argument("--no-shuffle", action="store_true", help="不随机打乱店内商品顺序")
    p.add_argument("--mode", choices=["shops"], default="shops",
                   help="仅支持 shops（商品URL模式已移除）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = Config.from_file(args.config, root=ROOT)
    if args.shops:
        cfg = cfg.replace(shop_csv=args.shops)
    if args.pages_per_shop is not None:
        cfg = cfg.replace(max_pages_per_shop=args.pages_per_shop)
    if args.max_detail is not None:
        cfg = cfg.replace(max_detail_pages_per_round=args.max_detail)
    if args.no_shuffle:
        cfg = cfg.replace(shuffle_within_shop=False)
    limit_keys = None
    if args.limit_shops:
        limit_keys = {s.strip() for s in args.limit_shops.split(",") if s.strip()}

    all_shops = load_shops(cfg.shop_csv)
    _db = Database(connect(cfg.db_file))
    _db.upsert_shops(all_shops)
    _db.conn.close()
    shops = [s for s in all_shops if s.active]
    if not shops:
        print("shops.csv 中没有有效店铺（active=1）。")
        return 2
    if limit_keys:
        shops = [s for s in shops if s.key in limit_keys]
        if not shops:
            print("limit-shops 与 shops.csv 没有任何匹配。")
            return 2

    cfg.ensure_dirs()
    sound.configure(cfg.alarm_on_intervention)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(cfg.logs_dir / "run.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"),
        ],
    )

    run_round(cfg, shops)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已收到中断信号，当前轮次保留，可再次运行续跑。")
        raise SystemExit(130)
