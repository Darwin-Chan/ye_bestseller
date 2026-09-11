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
from bestseller_monitor.pipeline import requested_round_shops, run_round  # noqa: E402
from bestseller_monitor.rounds import ScopeMismatch  # noqa: E402
from bestseller_monitor import sound  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=ROOT / "config" / "config.toml")
    p.add_argument("--shops", type=Path, default=None, help="覆盖 config 中的店铺 CSV")
    p.add_argument("--pages-per-shop", type=int, default=None, help="覆盖每家店翻页上限")
    p.add_argument("--max-detail", type=int, default=None, help="覆盖单轮详情预算（商品机会数）")
    p.add_argument("--limit-shops", type=str, default=None, help="逗号分隔的 shop_key 白名单")
    p.add_argument("--no-shuffle", action="store_true", help="不随机打乱店内商品顺序")
    p.add_argument("--mode", choices=["shops"], default="shops",
                   help="仅支持 shops（商品URL模式已移除）")
    return p.parse_args()


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    """把命令行参数应用到配置。

    `--pages-per-shop` 是显式覆盖，要同时写进两个字段：`max_pages_per_shop`
    是运行时快照与 GUI 展示用的有效值，`pages_per_shop_override` 是优先级判定
    用的「命令行说过话」标记——少了后者，店铺自己配了 pages 的店会按店铺值翻页
    （IS-35）。
    """
    if args.shops:
        cfg = cfg.replace(shop_csv=args.shops)
    if args.pages_per_shop is not None:
        cfg = cfg.replace(max_pages_per_shop=args.pages_per_shop,
                          pages_per_shop_override=args.pages_per_shop)
    if args.max_detail is not None:
        cfg = cfg.replace(max_detail_opportunities_per_round=args.max_detail)
    if args.no_shuffle:
        cfg = cfg.replace(shuffle_within_shop=False)
    return cfg


def main() -> int:
    args = parse_args()
    cfg = Config.from_file(args.config, root=ROOT)
    cfg = apply_overrides(cfg, args)
    limit_keys = None
    if args.limit_shops:
        limit_keys = {s.strip() for s in args.limit_shops.split(",") if s.strip()}

    all_shops = load_shops(cfg.shop_csv)
    _db = Database(connect(cfg.db_file))
    _db.upsert_shops(all_shops)
    _db.conn.close()
    # 不带 --limit-shops 的裸运行是「开始或续跑」：今天已有进行中的轮次就按轮次
    # 自身的范围续跑（续跑不得增删店铺），否则用配置里的有效店铺新建一轮。
    shops = requested_round_shops(cfg, limit_keys)
    if not shops:
        if limit_keys is not None:
            print("limit-shops 与 shops.csv 没有任何匹配。")
        else:
            print("shops.csv 中没有有效店铺（active=1）。")
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

    try:
        run_round(cfg, shops)
    except ScopeMismatch as exc:
        print(f"\n>>> {exc}\n")
        return 3
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已收到中断信号，当前轮次保留，可再次运行续跑。")
        raise SystemExit(130)
