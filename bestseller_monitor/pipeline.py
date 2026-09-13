"""轮次调度：榜单 → 详情 → 差分 → 导出。"""
from __future__ import annotations

import logging
import os
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import browser_pw, click_listing, dedupe, detail, rounds, single_instance, stop_request
from .click_listing import DenyTracker, ShopDenyExceeded, RoundDenyExceeded
from .config import Config, Shop, load_shops
from .db import (
    Database,
    connect,
    utcnow,
    cst_date,
    DayBoundaryReached,
    DetailBudgetExhausted,
    DAY_BOUNDARY_NOTE,
    DETAIL_BUDGET_NOTE,
)
from .delay import Humanizer
from .detail import DetailParseFailed
from .parse import extract_main_image
from .guard import RoundPauseRequired
from .rounds import Round, RoundRequest, ShopScope, TerminalReason
from .stop_request import StopRequested
from .listing import ListingLoadFailed, save_raw_listing_page

log = logging.getLogger(__name__)

# 用户按暂停打断某家店时，这家店的失败原因。人工介入超时另有文案，不许混用（ADR-0009）。
PAUSE_BY_USER_NOTE = "用户在界面暂停，本店未完成"

class StopScope(str, Enum):
    """这个停止影响谁：整轮，还是只跳过这家店。"""

    ROUND = "round"   # 整轮的停止：该店记为未完成并上抛
    SHOP = "shop"     # 只跳过这家店：该店记为未完成，接着跑下一家


@dataclass(frozen=True)
class StopOutcome:
    """一个停止异常该怎么收尾（ADR-0009）。

    文案与分类放在一起：谁抛出这个异常，谁就该知道「本店怎么记、轮次写不写终态、
    给操作者看哪一句」。模板里的 `{exc}` / `{note}` / `{round_id}` 由收尾处替换。
    """

    round_end: TerminalReason | None = None   # 轮次终态；None = 保持进行中（可续跑）
    round_note: str = ""      # 写终态时的说明
    shop_note: str = ""       # 该店本轮未完成的备注
    notice: str = ""          # 给操作者看的那一句
    log_line: str = ""        # 日志那一行
    ladder_log: str = ""      # 单店阶梯额外要记的那一行（多数异常不需要）
    log_level: int = logging.INFO
    scope: StopScope = StopScope.ROUND


# 「这个异常代表哪种停止」只有这一处定义；两套阶梯都查它。
# 查法是「自己 → 父类」（见 stop_outcome）：登记了父类就等于登记了它的子类，
# 而子类自己登记的条目优先——RoundDenyExceeded 继承 RoundPauseRequired，
# 两者各占一行，谁也不遮谁，也就不再需要靠 except 的先后顺序来保证。
_STOP_OUTCOMES: dict[type, StopOutcome] = {
    StopRequested: StopOutcome(
        shop_note=PAUSE_BY_USER_NOTE,
        log_line="收到界面暂停请求：本轮停在检查点，已抓数据保留、可续跑。",
        notice="本轮已暂停（可再次运行续跑）。",
    ),
    RoundDenyExceeded: StopOutcome(
        round_end=TerminalReason.DENY_EXCEEDED,
        round_note="本轮因整轮 deny 超过阈值而意外中止：{exc}；已抓取数据已保留，不可续跑",
        shop_note="整轮 deny 超过阈值：{exc}",
        ladder_log="整轮 deny 超过阈值，中止本轮：{exc}",
        log_line="本轮意外中止：{note}",
        notice="{note}，请启动新的抓取轮次。",
        log_level=logging.ERROR,
    ),
    DayBoundaryReached: StopOutcome(
        round_end=TerminalReason.DAY_BOUNDARY,
        round_note=DAY_BOUNDARY_NOTE,
        shop_note=DAY_BOUNDARY_NOTE,
        log_line="轮次 #{round_id}：{note}",
        notice="{note}。",
        log_level=logging.WARNING,
    ),
    DetailBudgetExhausted: StopOutcome(
        round_end=TerminalReason.DETAIL_BUDGET_EXHAUSTED,
        round_note=DETAIL_BUDGET_NOTE,
        shop_note=DETAIL_BUDGET_NOTE,
        log_line="轮次 #{round_id}：{note}",
        notice="{note}。",
        log_level=logging.WARNING,
    ),
    RoundPauseRequired: StopOutcome(
        shop_note="人工介入未完成：{exc}",
        log_line="本轮暂停：{exc}",
        notice="本轮已暂停（可再次运行续跑）：{exc}",
        log_level=logging.ERROR,
    ),
    ShopDenyExceeded: StopOutcome(
        shop_note="榜单 deny 超过阈值：{exc}",
        scope=StopScope.SHOP,
    ),
}

# 「该不该继续」这一族异常：整轮级的停止，处理方式一律是原样上抛（ADR-0009）。
STOP_EXCEPTIONS = tuple(exc for exc, outcome in _STOP_OUTCOMES.items()
                        if outcome.scope is StopScope.ROUND)

# 单店那条阶梯要认得的全部：跳过单店那种也在内。
STOP_WITH_OUTCOME = tuple(_STOP_OUTCOMES)


def stop_outcome(exc: BaseException) -> StopOutcome:
    """查这个异常该怎么收尾：先看它自己，再沿继承链找最近的登记项。

    `InterventionTimeout` 就是靠这条落进「人工介入未完成」——它继承
    `RoundPauseRequired`，不必单独登记。
    """
    for cls in type(exc).__mro__:
        outcome = _STOP_OUTCOMES.get(cls)
        if outcome is not None:
            return outcome
    raise KeyError(f"没有登记这个停止异常：{type(exc).__name__}")


def round_shops(db: Database, round_id: int, cfg: Config) -> list[Shop]:
    """轮次自身的店铺范围，作为本次处理的店铺列表。

    续跑不得增删店铺，所以范围永远取自轮次：配置里已经没有的店铺照样按轮次
    记录的名称与地址跑完，配置只用来补翻页上限这类运行时参数。
    """
    configured: dict[str, Shop] = {}
    shop_csv = getattr(cfg, "shop_csv", None)
    if shop_csv:
        configured = {shop.key: shop for shop in load_shops(shop_csv)}
    shops: list[Shop] = []
    for scope in rounds.scope_shops(db, round_id):
        known = configured.get(scope.key)
        shops.append(Shop(
            key=scope.key,
            name=scope.name,
            url=scope.url,
            home_url=known.home_url if known else None,
            offer_list_url=known.offer_list_url if known else None,
            pages=known.pages if known else None,
        ))
    return shops


def requested_round_shops(cfg: Config, limit_keys: set[str] | None = None) -> list[Shop]:
    """本次调用要用的店铺范围。

    limit_keys 给出时是明确的范围请求（命令行 `--limit-shops` 或界面勾选）：
    与今天进行中的轮次不一致就由 open() 拒绝。
    为 None 表示「开始或续跑」：今天已有进行中的轮次就按轮次自身的范围续跑，
    否则用配置里的有效店铺。
    """
    all_shops = [shop for shop in load_shops(cfg.shop_csv) if shop.active]
    if limit_keys is not None:
        return [shop for shop in all_shops if shop.key in limit_keys]
    conn = connect(cfg.db_file)
    try:
        db = Database(conn)
        active = rounds.active_round(db, cst_date())
        if active is not None:
            return round_shops(db, active.id, cfg)
    finally:
        conn.close()
    return all_shops


def _pending_detail_offers(db: Database, round_id: int, cfg: Config,
                           shop_key: str | None = None):
    rows = list(db.pending_offers(round_id, cfg.max_attempts_per_page, shop_key=shop_key))
    if not cfg.shuffle_within_shop:
        return rows
    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[row["shop_key"]].append(row)
    out = []
    for shop_rows in grouped.values():
        random.shuffle(shop_rows)
        out.extend(shop_rows)
    return out


def _record_listing_failure(
    db: Database, cfg: Config, round_id: int, shop: Shop, exc: ListingLoadFailed,
) -> None:
    raw_path = save_raw_listing_page(cfg, round_id, shop.key, exc.html) if exc.html else ""
    note = f"榜单失败：{exc}；原始页面：{raw_path}"
    db.mark_listing_failure(round_id, shop.key, note)
    log.warning("店铺 %s 榜单失败，保留待续跑：%s", shop.key, note)


def _record_incomplete_listing(db: Database, round_id: int, shop: Shop, note: str) -> None:
    """店铺尝试过但没拿到完整榜单：记失败态与原因，已发现的商品原样保留。"""
    db.mark_listing_failure(round_id, shop.key, note)
    log.warning("店铺 %s 榜单未完成（已发现商品保留）：%s", shop.key, note)


class CrawlerAlreadyRunning(RuntimeError):
    """已有采集进程在跑：同一时刻至多一个，由会话锁保证（见 single_instance）。"""


def run_round(cfg: Config, shops: list[Shop]) -> None:
    """开始或续跑一轮。

    shops 是本次调用请求的店铺范围：今天已有进行中的轮次时范围必须一致，否则
    open() 拒绝；跨日则由 open() 先给旧轮按跨天中止收尾再新建。真正交给驱动的
    处理列表永远取自轮次自身，续跑不会因为配置变化而增删店铺。

    「同一时刻至多一个采集进程」在这里守着：界面与命令行共用同一把会话锁，
    抢不到的那一方抛 CrawlerAlreadyRunning，不建轮次、不写快照（命令行入口在读配置
    时会顺手同步一次 shops 表，那不是采集数据）。
    """
    lock = single_instance.acquire(single_instance.CRAWLER_LOCK)
    if lock is None:
        raise CrawlerAlreadyRunning(
            "已有采集进程在运行：同一时刻只能跑一轮，等它结束或在界面里中止它。"
        )
    try:
        _run_round_locked(cfg, shops)
    finally:
        lock.release()


def _run_round_locked(cfg: Config, shops: list[Shop]) -> None:
    """拿到采集锁之后真正干活的部分。"""
    cfg.ensure_dirs()
    conn = connect(cfg.db_file)
    db = Database(conn)
    started_at: str | None = None
    try:
        request = RoundRequest(
            run_date=cst_date(),
            shops=tuple(ShopScope(shop.key, shop.url, shop.name) for shop in shops),
        )
        opened = rounds.open(db, request)
        round_id = opened.round.id
        # 身份行只给界面看「谁在跑」；是不是真的还有进程在跑以会话锁为准。
        started_at = db.record_crawler_process(pid=os.getpid(), round_id=round_id,
                                               note=_command_note())
        # 长睡眠的切片问的就是这一句：轮次还允许干活吗（ADR-0009）。
        stop_request.install(lambda: rounds.ensure_workable(db, round_id, utcnow()))
        log.info(
            "%s轮次 #%s（%s，%s 家店）",
            "新建" if opened.created else "续跑", round_id, request.run_date,
            len(opened.round.shop_keys),
        )
        if opened.superseded:
            stale = "、".join(f"#{row.id}" for row in opened.superseded)
            note = f"上一轮（{stale}）已跨天，按跨天中止收尾；本轮新建 #{round_id}"
            log.warning("%s", note)
            print(f"\n>>> {note}。\n")
        work_shops = round_shops(db, round_id, cfg)

        # 采集驱动只有一条；别的值在配置加载处就被拦住（ADR-0010），这里不再有分支。
        _run_pwcdp_round(db, cfg, round_id, work_shops)
        _finalize_round(db, cfg, opened.round)
    except STOP_WITH_OUTCOME as exc:
        # 停止这一族在这里统一收尾：分类与文案都来自 _STOP_OUTCOMES。
        outcome = stop_outcome(exc)
        note = outcome.round_note.format(exc=exc)
        fields = {"exc": exc, "note": note, "round_id": round_id}
        if outcome.round_end is None:
            # 保持可续跑：数据保留，轮次不写终态。
            log.log(outcome.log_level, outcome.log_line.format(**fields))
            print(f"\n>>> {outcome.notice.format(**fields)}\n")
        else:
            settled = rounds.finish_if_open(db, opened.round, outcome.round_end, note=note)
            if settled.reason is outcome.round_end:
                log.log(outcome.log_level, outcome.log_line.format(**fields))
                print(f"\n>>> {outcome.notice.format(**fields)}\n")
            else:
                _report_late_stop(settled, outcome.round_end)
    finally:
        stop_request.uninstall()
        db.clear_crawler_process()
        # 消费过的停止请求只对这一个进程有效，走到这里就把它删掉（ADR-0009）。
        db.clear_stop_request(target_pid=os.getpid(), target_started_at=started_at)
        conn.close()


def _command_note() -> str:
    """身份行里的命令行摘要：跨会话看见它时，能认出这是谁起的进程。"""
    return " ".join([Path(sys.argv[0]).name, *sys.argv[1:]])[:200]


def _report_late_stop(settled: Round, reason: TerminalReason) -> None:
    """这一轮早就被别处收掉了：如实说，别把本次的停止原因当成它的原因。

    典型情形：命令行起的采集还在跑，用户在界面里把那一轮人工中止了；采集进程要到
    下一个检查点才停下，那时它以为自己是「跨天中止」，其实这一轮早有终态了。
    """
    log.info("轮次 #%s 已经是 %s，本次的停止原因（%s）不改写它的终态。",
             settled.id, settled.reason.value, reason.value)
    print(f"\n>>> 轮次 #{settled.id} 已经是 {settled.reason.value}，"
          "本次停止不改写它的终态。\n")


def _capture_pending_offers(db: Database, cfg: Config, human: Humanizer, round_id: int,
                            offers, capture) -> int:
    """逐个补采；需要中止本轮的中断原样抛出，其余异常记为失败后继续。

    兜底失败记录接着本轮的尝试额度编号，初次访问和补采共用同一份额度。
    """
    processed = 0
    for offer in offers:
        processed += 1
        try:
            capture(offer)
        except STOP_EXCEPTIONS:
            raise
        except Exception as exc:  # 兜底：异常也记录失败，不中断整轮
            log.exception("详情抓取意外失败：%s", offer["product_url"])
            db.mark_failure(
                round_id, offer["shop_key"], offer["offer_id"],
                dedupe.next_attempt(db, round_id, offer["shop_key"], offer["offer_id"]),
                str(exc),
            )
        if processed % cfg.batch_size == 0:
            human.before_batch_rest()
    return processed


def _finalize_round(db: Database, cfg: Config, run: Round) -> None:
    round_id = run.id
    incomplete = db.incomplete_listings(round_id)
    if incomplete:
        keys = ", ".join(row["shop_key"] for row in incomplete)
        raise RoundPauseRequired(f"榜单阶段未完成：{keys}；请检查存档页面后续跑")
    tally = db.round_tally(round_id)
    click_fail = tally.click_card_failures        # 点击后未得到商品编号的卡片
    attempted = tally.discovered + click_fail
    failed = tally.failed_offers + click_fail
    fail_rate = failed / attempted if attempted else 0.0
    if attempted and fail_rate > cfg.fail_rate_limit:
        note = (f"失败率 {fail_rate:.1%} 超过阈值 {cfg.fail_rate_limit:.0%}"
                f"（快照失败 {tally.failed_offers}，点击未得商品 {click_fail}），需人工决策")
        settled = rounds.finish_if_open(db, run, TerminalReason.FAIL_RATE_EXCEEDED, note=note)
        if settled.reason is TerminalReason.FAIL_RATE_EXCEEDED:
            log.warning("轮次 #%s：%s", round_id, note)
            print(f"\n>>> {note}。请检查数据库 data/bestseller.db 中的结果后再决定。\n")
        else:
            _report_late_stop(settled, TerminalReason.FAIL_RATE_EXCEEDED)
    else:
        settled = rounds.finish_if_open(db, run, TerminalReason.COMPLETED)
        if settled.reason is TerminalReason.COMPLETED:
            log.info("轮次 #%s 完成（尝试 %s，成功 %s，点击未得商品 %s）",
                     round_id, attempted, tally.handled, click_fail)
        else:
            _report_late_stop(settled, TerminalReason.COMPLETED)
    db.commit()


# ---------- Playwright 连接接管（pw_cdp）路径 ----------

def _run_pwcdp_round(db: Database, cfg: Config, round_id: int, shops: list[Shop]) -> None:
    config_hash = db.record_params(cfg)
    emit = db.event_logger(round_id, config_hash)
    pw, br, page, ctx = browser_pw.open_session(cfg)
    deny_tracker = DenyTracker(cfg.deny_window_minutes * 60)
    try:
        _run_listing_pw(db, cfg, round_id, shops, page, emit=emit, deny_tracker=deny_tracker)
    finally:
        browser_pw.close_session(pw, br)


def _run_listing_pw(db: Database, cfg: Config, round_id: int, shops: list[Shop], page,
                    emit=None, deny_tracker=None) -> None:
    db.set_phase(round_id, "listing")
    done = db.completed_listing_keys(round_id)
    human = Humanizer(cfg)
    for shop in shops:
        # 每家店开始之前先问轮次：跨天收尾发生在还没动这家店的干净点上。
        rounds.ensure_workable(db, round_id, utcnow())
        if shop.key in done:
            log.info("店铺 %s 本轮已完成榜单，跳过列表", shop.key)
            _retry_shop_pending_pw(db, cfg, round_id, shop, page, human, emit=emit)
            continue
        try:
            listing_page = click_listing.PlaywrightListing(page, shop, cfg, human)
            offers, pages_read = click_listing.crawl_store_by_click(
                listing_page, shop, cfg, human, db=db, round_id=round_id, emit=emit,
                deny_tracker=deny_tracker,
            )
            log.info("店铺 %s 榜单：%s 个商品（%s 页）", shop.key, len(offers), pages_read)
            db.save_shop_offers(round_id, shop.key, shop.url, shop.name, offers, pages_read)
        except STOP_WITH_OUTCOME as exc:
            # 该店如实记为未完成（备注文案来自 _STOP_OUTCOMES），
            # 整轮级的停止原样上抛、跳过单店的那种接着跑下一家。
            outcome = stop_outcome(exc)
            _record_incomplete_listing(db, round_id, shop,
                                       outcome.shop_note.format(exc=exc))
            if outcome.ladder_log:
                log.log(outcome.log_level, outcome.ladder_log.format(exc=exc))
            if outcome.scope is StopScope.SHOP:
                continue
            raise
        except ListingLoadFailed as exc:
            _record_listing_failure(db, cfg, round_id, shop, exc)
            continue
        _retry_shop_pending_pw(db, cfg, round_id, shop, page, human, emit=emit)
    log.info("榜单阶段完成")


def _retry_shop_pending_pw(db: Database, cfg: Config, round_id: int, shop: Shop, page,
                           human: Humanizer, emit=None) -> None:
    """某店榜单保存完成后，立即补抓该店本轮尚未成功的商品，再进入下一家店。"""
    offers = _pending_detail_offers(db, round_id, cfg, shop_key=shop.key)
    if not offers:
        return
    log.info("店铺 %s 榜单完成后立即补抓 %s 个失败商品", shop.key, len(offers))
    _capture_pending_offers(
        db, cfg, human, round_id, offers,
        lambda offer: _capture_one_pw(db, cfg, human, round_id, offer, page, emit=emit),
    )


def _capture_one_pw(db: Database, cfg: Config, human: Humanizer, round_id: int, offer, page,
                    emit=None) -> None:
    """逐店补采一个商品：adapter 负责取一次详情，规则在 detail.capture_observation。

    编号在取观测前就已知，所以「今天采过就不打开页面、额度用尽就不进详情」这条省事的路
    在这里成立（候选 02 / ADR-0013）。
    """
    def emit_detail(event: str, **kw: object) -> None:
        if emit is not None:
            emit(event, shop_key=offer["shop_key"], **kw)

    def observe() -> detail.Observation:
        try:
            payload = browser_pw.capture_detail(page, offer["product_url"], cfg, human,
                                                emit=emit_detail)
            payload["main_image_url"] = extract_main_image(payload["html"])
        except STOP_EXCEPTIONS:
            # 停止判定（暂停／跨天／预算）不是「访问异常」：原样上抛。
            raise
        except DetailParseFailed as exc:
            return detail.Observation.parse_failed(exc, exc.html)
        except Exception as exc:
            return detail.Observation.access_failed(exc)
        return detail.Observation(payload=payload)

    def on_attempt_failed(note: str) -> None:
        emit_detail("detail_fail", offer_id=offer["offer_id"], phase="detail", note=note)

    detail.capture_observation(
        db, cfg, human, round_id,
        detail.DetailTarget(
            shop_key=offer["shop_key"], shop_url=offer["shop_url"],
            shop_name=offer["shop_name"], product_url=offer["product_url"],
            slot_key=offer["offer_id"], list_title=offer["list_title"],
            offer_id=offer["offer_id"],
        ),
        observe, on_attempt_failed=on_attempt_failed)
