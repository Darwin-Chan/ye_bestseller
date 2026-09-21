"""1688 店铺畅销榜 × SKU 库存快照 MVP 入口。

用法：
    python run.py                          # 开始或续跑一轮（范围与页数从本周计划取）
    python run.py --pages-per-shop 2       # 冒烟：每家店只翻 2 页（压过计划快照）
    python run.py --max-detail 60          # 冒烟：全轮最多 60 个详情页
    python run.py --limit-shops A01,A02    # 只处理指定店铺
    python run.py --ignore-plan            # 逃生口：拉不到计划库且本地没有本周计划时照开
                                           # （自由采集 + 记账为计划外；计划可用时不生效）

开跑前先走一次「开轮前的一次准备」（spec §6，与界面打开时同一步，见
`plan_step.prepare_week`）：拉计划库 → 同步店铺清单 → 确认/生成/发布本周计划 →
把整周计划落进本机计划表 → 缺口检查告警（只告警不拦）。拉不到计划库、本地也没有
本周计划时**默认拒绝开轮**（退出码 `plan_step.PLAN_REFUSED_EXIT_CODE`）——今天已有
进行中的轮次时除外，那一轮按轮次自身续跑；逃生口 `--ignore-plan` 按「自由采集 +
记账为计划外」运行。显式点名了归别台机器的店（`--limit-shops`）= 越权补采：**放行
并上报**，记进轮次备注与本机计划外账（`plan_step.record_deviations`）。

纯汇总机（`machine.role = merge_only`）不做采集：在碰采集清单与库之前就明确拒绝
（退出码 2，文案 `plan_step.MERGE_ONLY_REFUSAL`）——这台机器上跑数据交换台。

`--shops` 是人工冒烟的清单覆盖：开轮前的准备把它当作「本机清单副本」与计划库比对
（两边都变会停下不猜，单边变化可能被推送）——拿临时清单冒烟时留意这一点。
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT))

from bestseller_monitor.config import ROLE_MERGE_ONLY, Config, load_shops  # noqa: E402
from bestseller_monitor.db import Database, connect, cst_date  # noqa: E402
from bestseller_monitor.pipeline import (  # noqa: E402
    CrawlerAlreadyRunning,
    requested_round_shops,
    run_round,
)
from bestseller_monitor.rounds import ScopeMismatch  # noqa: E402
from bestseller_monitor import plan_step, rounds, single_instance, sound  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=ROOT / "config" / "config.toml")
    p.add_argument("--shops", type=Path, default=None, help="覆盖 config 中的店铺 CSV")
    p.add_argument("--pages-per-shop", type=int, default=None, help="覆盖每家店翻页上限")
    p.add_argument("--max-detail", type=int, default=None, help="覆盖单轮详情预算（商品机会数）")
    p.add_argument("--limit-shops", type=str, default=None, help="逗号分隔的 shop_key 白名单")
    p.add_argument("--ignore-plan", action="store_true",
                   help="拉不到计划库且本地没有本周计划时仍开轮（自由采集 + 记账为计划外）")
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
    # 纯汇总机不做采集（spec §6 降级表末行、§11）：在碰采集清单与库之前就拒绝——这台
    # 机器上没有采集清单是常态（不采 1688），库该由数据交换台开；这里一样都不动。
    if cfg.role == ROLE_MERGE_ONLY:
        print(f"\n>>> {plan_step.MERGE_ONLY_REFUSAL}\n")
        return 2
    limit_keys = None
    if args.limit_shops:
        limit_keys = {s.strip() for s in args.limit_shops.split(",") if s.strip()}

    all_shops = load_shops(cfg.shop_csv)
    conn = connect(cfg.db_file)
    db = Database(conn)
    try:
        db.upsert_shops(all_shops)
        # 开轮前的一次准备（spec §6）：拉计划库 → 同步清单 → 确认/生成/发布本周计划 →
        # 落库 → 缺口告警。界面打开时走的是同一步。
        prep = plan_step.prepare_week(cfg, db)
        for warning in prep.warnings:
            print(f"\n>>> {warning}\n")
        free = False
        # 正向判据（can_start）：以后 PrepStatus 多了新结局也不会在这里被静默放行
        if not prep.can_start:
            # 「默认拒绝开轮」拦的是新开轮：今天已有进行中的轮次就按轮次自身续跑
            # （逃生口开出来的那一轮也才续得下去）；真没路可走时给显式逃生口。
            if rounds.active_round(db, cst_date()) is not None:
                free = True
                print("\n>>> 拉不到计划库、本地也没有本周计划：继续跑今天已开的那一轮"
                      "（范围以轮次自身为准）。\n")
            elif args.ignore_plan:
                free = True
                print("\n>>> 按 --ignore-plan 运行（逃生口）：自由采集 + 记账为计划外——"
                      "本轮的店都会记进计划外账，汇总侧会按冲突处理。\n")
            else:
                print(f"\n>>> 开轮前准备没通过，默认拒绝开轮：\n{prep.reason}\n")
                return plan_step.PLAN_REFUSED_EXIT_CODE
        elif args.ignore_plan:
            print("\n>>> 本周计划可用，--ignore-plan 不生效：范围仍按计划。\n")
        # 页数四层的计划层与轮次范围都从这份落库计划读（与界面同一份，不解析计划文件）
        plan = plan_step.stored_plan(db, prep.week)
        cfg = cfg.replace(plan_pages=plan.pages() if plan is not None else None)
        # 不带 --limit-shops 的裸运行是「开始或续跑」：今天已有进行中的轮次就按轮次
        # 自身的范围续跑（续跑不得增删店铺），否则取本周计划里归本机的店；逃生口放行
        # （free）时取本机清单里启用的店。
        shops = requested_round_shops(cfg, limit_keys, week=prep.week, free=free)
        # 越权/计划外的店：放行并上报——记账落在轮次备注与本机计划外账（run_round 里写）。
        deviations = plan_step.plan_deviations(plan, cfg.machine_id,
                                               [shop.key for shop in shops])
        if deviations:
            print(f"\n>>> {plan_step.deviation_note(deviations)}\n")
    finally:
        conn.close()
    if not shops:
        if limit_keys is not None:
            print("limit-shops 与清单没有任何匹配。")
            return 2
        if prep.idle:
            print(f"本周计划（{prep.week}）里没有归本机的店（空手）：没有要采的店。")
            return 0
        if prep.my_shops:
            print("本周计划里归本机的店在本机清单里找不到："
                  f"{'、'.join(prep.my_shops)}——先把它们补回 {cfg.shop_csv}（照上机清单"
                  "第 11 步的清单同步），或等下一份计划；这一轮没有可采的店。")
            return 2
        print("本机清单里没有有效店铺（active=1）。")
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
        run_round(cfg, shops, deviations=deviations)
    except CrawlerAlreadyRunning as exc:
        # 同一时刻至多一个采集进程：抢不到锁就说清楚，并用一个只表示这件事的退出码，
        # 界面据此提示原因（launcher 与调用方不会把它当成一轮正常结束）。
        print(f"\n>>> {exc}\n")
        return single_instance.CRAWLER_BUSY_EXIT_CODE
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
