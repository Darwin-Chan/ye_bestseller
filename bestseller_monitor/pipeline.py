"""轮次调度：榜单 → 详情 → 差分 → 导出。"""
from __future__ import annotations

import logging
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import browser_dp, browser_pw, dedupe, rounds, single_instance, stop_request
from .browser_pw import DenyTracker, ShopDenyExceeded, RoundDenyExceeded
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
from .detail import DetailParseFailed, capture_detail_payload, save_raw_page
from .guard import RoundPauseRequired
from .parse import extract_main_image
from .rounds import Round, RoundRequest, ShopScope, TerminalReason
from .stop_request import StopRequested
from .listing import ListingLoadFailed, crawl_shop_listing, save_raw_listing_page

log = logging.getLogger(__name__)

# 用户按暂停打断某家店时，这家店的失败原因。人工介入超时另有文案，不许混用（ADR-0009）。
PAUSE_BY_USER_NOTE = "用户在界面暂停，本店未完成"

# 「该不该继续」这一族异常：不是采集失败，处理方式一律是原样上抛（ADR-0009）。
STOP_EXCEPTIONS = (StopRequested, RoundPauseRequired, DayBoundaryReached,
                   DetailBudgetExhausted)


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

        if cfg.driver == "pw_cdp":
            _run_pwcdp_round(db, cfg, round_id, work_shops)
        elif cfg.driver == "drission":
            _run_dp_round(db, cfg, round_id, work_shops)
        else:
            _run_pw_round(db, cfg, round_id, work_shops)
        _finalize_round(db, cfg, opened.round)
    except StopRequested:
        # 界面按了暂停：轮次保持进行中，已抓数据保留，可再次运行续跑。
        log.info("收到界面暂停请求：本轮停在检查点，已抓数据保留、可续跑。")
        print("\n>>> 本轮已暂停（可再次运行续跑）。\n")
    except RoundDenyExceeded as exc:
        # 整轮 deny 超限是终态：数据保留，但本轮不可续跑，只能新开一轮。
        note = f"本轮因整轮 deny 超过阈值而意外中止：{exc}；已抓取数据已保留，不可续跑"
        settled = rounds.finish_if_open(db, opened.round, TerminalReason.DENY_EXCEEDED, note=note)
        if settled.reason is TerminalReason.DENY_EXCEEDED:
            log.error("本轮意外中止：%s", note)
            print(f"\n>>> {note}，请启动新的抓取轮次。\n")
        else:
            _report_late_stop(settled, TerminalReason.DENY_EXCEEDED)
    except DayBoundaryReached:
        settled = rounds.finish_if_open(db, opened.round, TerminalReason.DAY_BOUNDARY,
                                        note=DAY_BOUNDARY_NOTE)
        if settled.reason is TerminalReason.DAY_BOUNDARY:
            log.warning("轮次 #%s：%s", round_id, DAY_BOUNDARY_NOTE)
            print(f"\n>>> {DAY_BOUNDARY_NOTE}。\n")
        else:
            _report_late_stop(settled, TerminalReason.DAY_BOUNDARY)
    except DetailBudgetExhausted:
        settled = rounds.finish_if_open(db, opened.round, TerminalReason.DETAIL_BUDGET_EXHAUSTED,
                                        note=DETAIL_BUDGET_NOTE)
        if settled.reason is TerminalReason.DETAIL_BUDGET_EXHAUSTED:
            log.warning("轮次 #%s：%s", round_id, DETAIL_BUDGET_NOTE)
            print(f"\n>>> {DETAIL_BUDGET_NOTE}。\n")
        else:
            _report_late_stop(settled, TerminalReason.DETAIL_BUDGET_EXHAUSTED)
    except RoundPauseRequired as exc:
        # 人工处理超时等情况：保留轮次状态，提示稍后续跑
        log.error("本轮暂停：%s", exc)
        print(f"\n>>> 本轮已暂停（可再次运行续跑）：{exc}\n")
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


def _run_pw_round(db: Database, cfg: Config, round_id: int, shops: list[Shop]) -> None:
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(cfg.profile_dir),
            channel=cfg.browser_channel,
            headless=cfg.headless,
            slow_mo=cfg.slow_mo_ms,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=AutomationControlled,IsolateOrigins,site-per-process",
            ],
            ignore_default_args=["--enable-automation"],
        )
        page = context.new_page()
        page.set_default_timeout(cfg.timeout_ms)
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
            window.chrome = window.chrome || { runtime: {} };
            """
        )
        try:
            _run_listing_phase(db, cfg, round_id, shops, page)
            _run_detail_phase(db, cfg, round_id, page)
        finally:
            context.close()


def _run_dp_round(db: Database, cfg: Config, round_id: int, shops: list[Shop]) -> None:
    page = browser_dp.create_page(cfg)
    try:
        _run_listing_dp(db, cfg, round_id, shops, page)
        _run_detail_dp(db, cfg, round_id, page)
    finally:
        try:
            page.quit()
        except Exception:
            pass
        browser_dp.stop_browser()


def _run_listing_dp(db: Database, cfg: Config, round_id: int, shops: list[Shop], page) -> None:
    db.set_phase(round_id, "listing")
    done = db.completed_listing_keys(round_id)
    human = Humanizer(cfg)
    for shop in shops:
        # 每家店开始之前先问轮次：跨天收尾发生在还没动这家店的干净点上。
        rounds.ensure_workable(db, round_id, utcnow())
        if shop.key in done:
            log.info("店铺 %s 本轮已完成榜单，跳过", shop.key)
            continue
        try:
            offers, pages_read = browser_dp.crawl_shop_listing(page, shop, cfg, human)
            db.save_shop_offers(round_id, shop.key, shop.url, shop.name, offers, pages_read)
        except ListingLoadFailed as exc:
            _record_listing_failure(db, cfg, round_id, shop, exc)
        except StopRequested:
            _record_incomplete_listing(db, round_id, shop, PAUSE_BY_USER_NOTE)
            raise
    log.info("店铺阶段完成（含逐店即时补抓）")


def _run_detail_dp(db: Database, cfg: Config, round_id: int, page) -> None:
    _run_pending_detail_phase(
        db, cfg, round_id,
        lambda d, c, h, r, o: _capture_one_dp(d, c, h, r, o, page),
    )


def _run_pending_detail_phase(db: Database, cfg: Config, round_id: int, capture) -> None:
    """逐个处理待补采商品；取得一次详情的能力由各驱动的 capture 提供。"""
    db.set_phase(round_id, "detail")
    human = Humanizer(cfg)
    offers = _pending_detail_offers(db, round_id, cfg)
    log.info("详情阶段：待处理商品 %s 个", len(offers))
    processed = _capture_pending_offers(
        db, cfg, human, round_id, offers,
        lambda offer: capture(db, cfg, human, round_id, offer),
    )
    log.info("详情阶段结束：本轮处理 %s 个商品", processed)


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


def _capture_offer_detail(db: Database, cfg: Config, human: Humanizer, round_id: int, offer,
                          fetch, *, on_attempt_failed=None) -> None:
    """同日去重与补采 module：一个商品的详情采集规则集中在这里。

    - 同日去重按商品编号判断，跳过不消耗预算
    - 初次访问与补采共享同一份尝试额度
    - 进入详情前申请轮次详情预算，用尽则结束本轮
    - 重试、失败记录与成功提交

    fetch 是 adapter 边界，只负责取一次详情并返回 payload。
    """
    shop_key = offer["shop_key"]
    offer_id = offer["offer_id"]
    product_url = offer["product_url"]
    shop_url = offer["shop_url"]
    shop_name = offer["shop_name"]
    max_attempts = cfg.max_attempts_per_page

    # 进详情之前先问轮次：跨到次日或已过截止线就不再开始新的详情采集。
    rounds.ensure_workable(db, round_id, utcnow())

    if db.inventory_exists(shop_key, offer_id, cst_date()):
        db.mark_skipped(round_id, shop_key, shop_url, shop_name, offer_id, product_url,
                        offer["list_title"])
        log.info("店铺 %s 商品 %s 今日已有库存，跳过详情抓取", shop_key, offer_id)
        return

    # 初次访问和补采共享同一份尝试额度；已经用尽的商品不再进入详情。
    next_attempt_no = dedupe.next_attempt(db, round_id, shop_key, offer_id)
    if next_attempt_no > max_attempts:
        log.info("店铺 %s 商品 %s 本轮尝试已用尽（%s/%s），不再补采",
                 shop_key, offer_id, next_attempt_no - 1, max_attempts)
        return

    # 同日跳过不消耗预算；只有真正要进入详情的商品才申请机会。
    dedupe.claim_offer_slot(db, round_id, shop_key, offer_id,
                            cfg.max_detail_opportunities_per_round)

    for attempt in range(next_attempt_no, max_attempts + 1):
        try:
            payload = fetch()
        except DetailParseFailed as exc:
            raw_path = save_raw_page(cfg, round_id, offer_id, exc.html) if exc.html else ""
            note = f"解析失败：{exc}；原始页面：{raw_path}"
            db.mark_failure(round_id, shop_key, offer_id, attempt, note)
            log.warning("第 %s 次解析失败：%s", attempt, note)
            if on_attempt_failed is not None:
                on_attempt_failed(note)
            if attempt < max_attempts:
                human.sleep(human.retry_delay(attempt))
            continue
        except STOP_EXCEPTIONS:
            # 停止判定（暂停／跨天／预算）不是「访问异常」：原样上抛，别记成一次失败尝试。
            # 长睡眠的切片会在 fetch 途中抛出来，这一层是它唯一的兜底。
            raise
        except Exception as exc:
            note = f"访问异常：{exc}"
            db.mark_failure(round_id, shop_key, offer_id, attempt, note)
            log.warning("第 %s 次访问失败：%s", attempt, note)
            if on_attempt_failed is not None:
                on_attempt_failed(note)
            if attempt < max_attempts:
                human.sleep(human.retry_delay(attempt))
            continue

        img = extract_main_image(payload["html"])
        db.submit_inventory_snapshot(
            round_id=round_id,
            shop_key=shop_key,
            shop_url=shop_url,
            shop_name=shop_name,
            offer_id=offer_id,
            product_url=product_url,
            list_title=offer["list_title"],
            detail_title=payload["product_name"],
            main_image_url=img,
            sku_rows=payload["rows"],
            collected_at=utcnow(),
            attempt=attempt,
        )
        # 提交之后再看一次：已提交的数据保留，停止判定不回滚它。
        rounds.ensure_workable(db, round_id, utcnow())
        log.info("店铺 %s 商品 %s 抓取成功：%s 个 SKU（第 %s 次尝试）",
                 shop_key, offer_id, len(payload["rows"]), attempt)
        return


def _capture_one_dp(db: Database, cfg: Config, human: Humanizer, round_id: int, offer, page) -> None:
    """DrissionPage 版单个详情抓取（带重试与数据库写入）。"""
    def fetch():
        return browser_dp.capture_detail_payload(page, offer["product_url"], cfg, human)

    _capture_offer_detail(db, cfg, human, round_id, offer, fetch)


def _run_listing_phase(db: Database, cfg: Config, round_id: int, shops: list[Shop], page) -> None:
    db.set_phase(round_id, "listing")
    done = db.completed_listing_keys(round_id)
    human = Humanizer(cfg)
    for shop in shops:
        # 每家店开始之前先问轮次：跨天收尾发生在还没动这家店的干净点上。
        rounds.ensure_workable(db, round_id, utcnow())
        if shop.key in done:
            log.info("店铺 %s 本轮已完成榜单，跳过", shop.key)
            continue
        try:
            offers, pages_read = crawl_shop_listing(page, shop, cfg, human)
            db.save_shop_offers(
                round_id, shop.key, shop.url, shop.name, offers, pages_read,
            )
        except ListingLoadFailed as exc:
            _record_listing_failure(db, cfg, round_id, shop, exc)
        except StopRequested:
            _record_incomplete_listing(db, round_id, shop, PAUSE_BY_USER_NOTE)
            raise
    log.info("榜单阶段完成")


def _run_detail_phase(db: Database, cfg: Config, round_id: int, page) -> None:
    _run_pending_detail_phase(
        db, cfg, round_id,
        lambda d, c, h, r, o: _capture_one(d, c, h, r, o, page),
    )


def _capture_one(
    db: Database, cfg: Config, human: Humanizer, round_id: int, offer, page,
) -> None:
    """带重试的单个详情抓取。"""
    def fetch():
        return capture_detail_payload(page, offer["product_url"], cfg, human)

    _capture_offer_detail(db, cfg, human, round_id, offer, fetch)


def _finalize_round(db: Database, cfg: Config, run: Round) -> None:
    round_id = run.id
    incomplete = db.incomplete_listings(round_id)
    if incomplete:
        keys = ", ".join(row["shop_key"] for row in incomplete)
        raise RoundPauseRequired(f"榜单阶段未完成：{keys}；请检查存档页面后续跑")
    total, succeeded = db.offer_counts(round_id)
    click_fail = db.click_card_failures(round_id)   # 点击后未得到商品编号的卡片
    attempted = total + click_fail
    failed = (total - succeeded) + click_fail
    fail_rate = failed / attempted if attempted else 0.0
    if attempted and fail_rate > cfg.fail_rate_limit:
        note = (f"失败率 {fail_rate:.1%} 超过阈值 {cfg.fail_rate_limit:.0%}"
                f"（快照失败 {total - succeeded}，点击未得商品 {click_fail}），需人工决策")
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
                     round_id, attempted, succeeded, click_fail)
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
            offers, pages_read = browser_pw.crawl_store_by_click(
                page, shop, cfg, human, db=db, round_id=round_id, emit=emit,
                deny_tracker=deny_tracker,
            )
            log.info("店铺 %s 榜单：%s 个商品（%s 页）", shop.key, len(offers), pages_read)
            db.save_shop_offers(round_id, shop.key, shop.url, shop.name, offers, pages_read)
        except ShopDenyExceeded as exc:
            _record_incomplete_listing(db, round_id, shop, f"榜单 deny 超过阈值：{exc}")
            continue
        except RoundDenyExceeded as exc:
            _record_incomplete_listing(db, round_id, shop, f"整轮 deny 超过阈值：{exc}")
            log.error("整轮 deny 超过阈值，中止本轮：%s", exc)
            raise
        except DetailBudgetExhausted:
            _record_incomplete_listing(db, round_id, shop, DETAIL_BUDGET_NOTE)
            raise
        except DayBoundaryReached:
            _record_incomplete_listing(db, round_id, shop, DAY_BOUNDARY_NOTE)
            raise
        except RoundPauseRequired as exc:
            # 人工验证等不到结果：轮次保持可续跑，这家店要如实记为未完成。
            _record_incomplete_listing(db, round_id, shop, f"人工介入未完成：{exc}")
            raise
        except StopRequested:
            # 用户在界面按了暂停：轮次同样保持可续跑，但原因不许写成人工介入。
            _record_incomplete_listing(db, round_id, shop, PAUSE_BY_USER_NOTE)
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
    def emit_detail(event: str, **kw: object) -> None:
        if emit is not None:
            emit(event, shop_key=offer["shop_key"], **kw)

    def fetch():
        return browser_pw.capture_detail(page, offer["product_url"], cfg, human,
                                         emit=emit_detail)

    def on_attempt_failed(note: str) -> None:
        emit_detail("detail_fail", offer_id=offer["offer_id"], phase="detail", note=note)

    _capture_offer_detail(db, cfg, human, round_id, offer, fetch,
                          on_attempt_failed=on_attempt_failed)
