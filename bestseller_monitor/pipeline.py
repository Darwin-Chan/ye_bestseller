"""轮次调度：榜单 → 详情 → 差分 → 导出。"""
from __future__ import annotations

import logging
import hashlib
import random
from collections import defaultdict

from playwright.sync_api import sync_playwright

from . import browser_dp, browser_pw
from .browser_pw import DenyTracker, ShopDenyExceeded, RoundDenyExceeded
from .config import Config, Shop
from .db import Database, connect, utcnow, cst_date
from .delay import Humanizer
from .detail import DetailParseFailed, capture_detail_payload, save_raw_page
from .parse import extract_main_image
from .listing import crawl_shop_listing
from .report import export_round_csv, export_round_excel, update_stock_deltas

log = logging.getLogger(__name__)


def _ensure_db_shops(db: Database, round_id: int, shops: list[Shop]) -> None:
    for shop in shops:
        db.add_shop(round_id, shop.key, shop.url, shop.name)


def _pending_detail_offers(db: Database, round_id: int, cfg: Config):
    rows = list(db.pending_offers(round_id, cfg.max_attempts_per_page))
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


def run_round(cfg: Config, shops: list[Shop]) -> None:
    cfg.ensure_dirs()
    conn = connect(cfg.db_file)
    db = Database(conn)
    round_id = db.start_or_resume()
    _ensure_db_shops(db, round_id, shops)

    try:
        if cfg.driver == "pw_cdp":
            _run_pwcdp_round(db, cfg, round_id, shops)
        elif cfg.driver == "drission":
            _run_dp_round(db, cfg, round_id, shops)
        else:
            _run_pw_round(db, cfg, round_id, shops)
        _finalize_round(db, cfg, round_id)
    except RuntimeError as exc:
        # 人工处理超时等情况：保留轮次状态，提示稍后续跑
        log.error("本轮暂停：%s", exc)
        print(f"\n>>> 本轮已暂停（可再次运行续跑）：{exc}\n")
    finally:
        conn.close()


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
        _run_listing_phase(db, cfg, round_id, shops, page)
        _run_detail_phase(db, cfg, round_id, page)
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
    done = {row["shop_key"] for row in db.shops_to_list(round_id) if row["list_status"] == "完成"}
    human = Humanizer(cfg)
    for shop in shops:
        if shop.key in done:
            log.info("店铺 %s 本轮已完成榜单，跳过", shop.key)
            continue
        offers, pages_read = browser_dp.crawl_shop_listing(page, shop, cfg, human)
        db.save_shop_offers(round_id, shop.key, shop.url, shop.name, offers, pages_read)
    log.info("榜单阶段完成")


def _run_detail_dp(db: Database, cfg: Config, round_id: int, page) -> None:
    db.set_phase(round_id, "detail")
    human = Humanizer(cfg)
    offers = _pending_detail_offers(db, round_id, cfg)
    log.info("详情阶段：待处理商品 %s 个", len(offers))
    processed = 0
    cap = cfg.max_detail_pages_per_round
    for offer in offers:
        if processed >= cap:
            log.warning("已达单轮详情上限 %s，剩余留待下一轮。", cap)
            break
        processed += 1
        try:
            _capture_one_dp(db, cfg, human, round_id, offer, page)
        except RuntimeError:
            raise
        except Exception as exc:
            log.exception("详情抓取意外失败：%s", offer["product_url"])
            db.mark_failure(round_id, offer["shop_key"], offer["offer_id"], 1, str(exc))
        if processed % cfg.batch_size == 0:
            human.before_batch_rest()
    log.info("详情阶段结束：本轮处理 %s 个商品", processed)


def _capture_one_dp(db: Database, cfg: Config, human: Humanizer, round_id: int, offer, page) -> None:
    """DrissionPage 版单个详情抓取（带重试与数据库写入）。"""
    shop_key = offer["shop_key"]
    offer_id = offer["offer_id"]
    product_url = offer["product_url"]
    shop_url = offer["shop_url"]
    shop_name = offer["shop_name"]
    max_attempts = cfg.max_attempts_per_page
    collected_at = utcnow()

    day = cst_date()
    if offer["list_title"] and db.inventory_exists_by_name(shop_key, offer["list_title"], day):
        db.mark_skipped(round_id, shop_key, shop_url, shop_name, offer_id, product_url,
                        offer["list_title"], note="今日已有同名库存，跳过")
        log.info("店铺 %s 商品「%s」今日已有库存，跳过详情抓取", shop_key, offer["list_title"][:40])
        return
    if db.inventory_exists(shop_key, offer_id, day):
        db.mark_skipped(round_id, shop_key, shop_url, shop_name, offer_id, product_url,
                        offer["list_title"])
        log.info("店铺 %s 商品 %s 今日已有库存，跳过详情抓取", shop_key, offer_id)
        return

    for attempt in range(1, max_attempts + 1):
        try:
            payload = browser_dp.capture_detail_payload(page, product_url, cfg, human)
        except DetailParseFailed as exc:
            raw_path = save_raw_page(cfg, round_id, offer_id, exc.html) if exc.html else ""
            note = f"解析失败：{exc}；原始页面：{raw_path}"
            db.mark_failure(round_id, shop_key, offer_id, attempt, note)
            log.warning("第 %s 次解析失败：%s", attempt, note)
            if attempt < max_attempts:
                human.sleep(human.retry_delay(attempt))
            continue
        except RuntimeError:
            raise
        except Exception as exc:
            note = f"访问异常：{exc}"
            db.mark_failure(round_id, shop_key, offer_id, attempt, note)
            log.warning("第 %s 次访问失败：%s", attempt, note)
            if attempt < max_attempts:
                human.sleep(human.retry_delay(attempt))
            continue

        img = extract_main_image(payload["html"])
        db.upsert_product(offer_id, product_url, payload["product_name"], img)
        rows = []
        for sku in payload["rows"]:
            sku_id = sku.get("sku_id") or hashlib.sha1(
                f"{offer_id}|{sku['sku_name']}".encode("utf-8")
            ).hexdigest()[:16]
            rows.append(
                {
                    "round_id": round_id,
                    "shop_key": shop_key,
                    "shop_url": shop_url,
                    "shop_name": shop_name,
                    "offer_id": offer_id,
                    "product_url": product_url,
                    "product_name": payload["product_name"] or offer["list_title"],
                    "sku_id": sku_id,
                    "sku_name": sku["sku_name"],
                    "sku_price": sku["sku_price"],
                    "sku_stock": sku["sku_stock"],
                    "collected_at": collected_at,
                    "page_status": "成功",
                    "attempt": attempt,
                }
            )
        db.clear_failures(round_id, shop_key, offer_id)
        for r in rows:
            r["main_image_url"] = img
        for r in rows:
            r["main_image_url"] = img
        db.save_snapshot_rows(round_id, shop_key, rows)
        log.info("店铺 %s 商品 %s 抓取成功：%s 个 SKU（第 %s 次）", shop_key, offer_id, len(rows), attempt)
        return


def _run_listing_phase(db: Database, cfg: Config, round_id: int, shops: list[Shop], page) -> None:
    db.set_phase(round_id, "listing")
    done = {row["shop_key"] for row in db.shops_to_list(round_id) if row["list_status"] == "完成"}
    human = Humanizer(cfg)
    for shop in shops:
        if shop.key in done:
            log.info("店铺 %s 本轮已完成榜单，跳过", shop.key)
            continue
        offers, pages_read = crawl_shop_listing(page, shop, cfg, human)
        db.save_shop_offers(
            round_id, shop.key, shop.url, shop.name, offers, pages_read,
        )
    log.info("榜单阶段完成")


def _run_detail_phase(db: Database, cfg: Config, round_id: int, page) -> None:
    db.set_phase(round_id, "detail")
    human = Humanizer(cfg)
    offers = _pending_detail_offers(db, round_id, cfg)
    log.info("详情阶段：待处理商品 %s 个", len(offers))

    processed = 0
    cap = cfg.max_detail_pages_per_round
    for offer in offers:
        if processed >= cap:
            log.warning("已达单轮详情上限 %s，剩余商品留待下一轮。", cap)
            break
        processed += 1
        try:
            _capture_one(db, cfg, human, round_id, offer, page)
        except RuntimeError:
            raise
        except Exception as exc:  # 兜底：异常也记录失败，不中断整轮
            log.exception("详情抓取意外失败：%s", offer["product_url"])
            db.mark_failure(round_id, offer["shop_key"], offer["offer_id"], 1, str(exc))
        if processed % cfg.batch_size == 0:
            human.before_batch_rest()
    log.info("详情阶段结束：本轮处理 %s 个商品", processed)


def _capture_one(
    db: Database, cfg: Config, human: Humanizer, round_id: int, offer, page,
) -> None:
    """带重试的单个详情抓取。"""
    shop_key = offer["shop_key"]
    offer_id = offer["offer_id"]
    product_url = offer["product_url"]
    shop_url = offer["shop_url"]
    shop_name = offer["shop_name"]
    max_attempts = cfg.max_attempts_per_page
    collected_at = utcnow()

    day = cst_date()
    if offer["list_title"] and db.inventory_exists_by_name(shop_key, offer["list_title"], day):
        db.mark_skipped(round_id, shop_key, shop_url, shop_name, offer_id, product_url,
                        offer["list_title"], note="今日已有同名库存，跳过")
        log.info("店铺 %s 商品「%s」今日已有库存，跳过详情抓取", shop_key, offer["list_title"][:40])
        return
    if db.inventory_exists(shop_key, offer_id, day):
        db.mark_skipped(round_id, shop_key, shop_url, shop_name, offer_id, product_url,
                        offer["list_title"])
        log.info("店铺 %s 商品 %s 今日已有库存，跳过详情抓取", shop_key, offer_id)
        return

    for attempt in range(1, max_attempts + 1):
        try:
            payload = capture_detail_payload(page, product_url, cfg, human)
        except DetailParseFailed as exc:
            raw_path = save_raw_page(cfg, round_id, offer_id, exc.html) if exc.html else ""
            note = f"解析失败：{exc}；原始页面：{raw_path}"
            db.mark_failure(round_id, shop_key, offer_id, attempt, note)
            log.warning("第 %s 次解析失败：%s", attempt, note)
            if attempt < max_attempts:
                human.sleep(human.retry_delay(attempt))
            continue
        except RuntimeError:
            raise
        except Exception as exc:
            note = f"访问异常：{exc}"
            db.mark_failure(round_id, shop_key, offer_id, attempt, note)
            log.warning("第 %s 次访问失败：%s", attempt, note)
            if attempt < max_attempts:
                human.sleep(human.retry_delay(attempt))
            continue

        img = extract_main_image(payload["html"])
        db.upsert_product(offer_id, product_url, payload["product_name"], img)
        rows = []
        for sku in payload["rows"]:
            sku_id = sku.get("sku_id") or hashlib.sha1(
                f"{offer_id}|{sku['sku_name']}".encode("utf-8")
            ).hexdigest()[:16]
            rows.append(
                {
                    "round_id": round_id,
                    "shop_key": shop_key,
                    "shop_url": shop_url,
                    "shop_name": shop_name,
                    "offer_id": offer_id,
                    "product_url": product_url,
                    "product_name": payload["product_name"] or offer["list_title"],
                    "sku_id": sku_id,
                    "sku_name": sku["sku_name"],
                    "sku_price": sku["sku_price"],
                    "sku_stock": sku["sku_stock"],
                    "collected_at": collected_at,
                    "page_status": "成功",
                    "attempt": attempt,
                }
            )
        db.clear_failures(round_id, shop_key, offer_id)
        for r in rows:
            r["main_image_url"] = img
        db.save_snapshot_rows(round_id, shop_key, rows)
        log.info(
            "店铺 %s 商品 %s 抓取成功：%s 个 SKU（第 %s 次尝试）",
            shop_key, offer_id, len(rows), attempt,
        )
        return


def _finalize_round(db: Database, cfg: Config, round_id: int) -> None:
    update_stock_deltas(db, round_id)
    total, succeeded = db.offer_counts(round_id)
    click_fail = db.click_card_failures(round_id)   # 点击后未得到商品编号的卡片
    attempted = total + click_fail
    failed = (total - succeeded) + click_fail
    fail_rate = failed / attempted if attempted else 0.0
    if attempted and fail_rate > cfg.fail_rate_limit:
        note = (f"失败率 {fail_rate:.1%} 超过阈值 {cfg.fail_rate_limit:.0%}"
                f"（快照失败 {total - succeeded}，点击未得商品 {click_fail}），需人工决策")
        db.finish_round(round_id, status="需人工-失败率超限", note=note)
        log.warning("轮次 #%s：%s", round_id, note)
        print(f"\n>>> {note}。请检查 data/ 与 output/ 中的结果后再决定。\n")
    else:
        db.finish_round(round_id, status="完成")
        log.info("轮次 #%s 完成（尝试 %s，成功 %s，点击未得商品 %s）",
                 round_id, attempted, succeeded, click_fail)

    try:
        export_round_csv(db, cfg, round_id)
    except Exception:
        log.exception("CSV 导出失败")
    try:
        export_round_excel(db, cfg, round_id)
    except Exception:
        log.exception("Excel 导出失败（请确认已安装 openpyxl：pip install -r requirements.txt）")
    db.commit()


# ---------- Playwright 连接接管（pw_cdp）路径 ----------

def _run_pwcdp_round(db: Database, cfg: Config, round_id: int, shops: list[Shop]) -> None:
    config_hash = db.record_params(cfg)
    emit = db.event_logger(round_id, config_hash)
    pw, br, page, ctx = browser_pw.open_session(cfg)
    deny_tracker = DenyTracker(cfg.deny_window_minutes * 60)
    try:
        _run_listing_pw(db, cfg, round_id, shops, page, emit=emit, deny_tracker=deny_tracker)
        _run_detail_pw(db, cfg, round_id, page, emit=emit)
    finally:
        browser_pw.close_session(pw, br)


def _run_listing_pw(db: Database, cfg: Config, round_id: int, shops: list[Shop], page,
                    emit=None, deny_tracker=None) -> None:
    db.set_phase(round_id, "listing")
    done = {row["shop_key"] for row in db.shops_to_list(round_id) if row["list_status"] == "完成"}
    human = Humanizer(cfg)
    for shop in shops:
        if shop.key in done:
            log.info("店铺 %s 本轮已完成榜单，跳过", shop.key)
            continue
        try:
            offers, pages_read = browser_pw.crawl_store_by_click(
                page, shop, cfg, human, db=db, round_id=round_id, emit=emit,
                deny_tracker=deny_tracker,
            )
            log.info("店铺 %s 榜单：%s 个商品（%s 页）", shop.key, len(offers), pages_read)
            db.save_shop_offers(round_id, shop.key, shop.url, shop.name, offers, pages_read)
        except ShopDenyExceeded as exc:
            log.warning("店铺 %s 因 deny 超过阈值，跳过（本轮不保存残缺榜单）：%s", shop.key, exc)
            continue
        except RoundDenyExceeded as exc:
            log.error("整轮 deny 超过阈值，中止本轮：%s", exc)
            raise RuntimeError(str(exc)) from exc
    log.info("榜单阶段完成")


def _run_detail_pw(db: Database, cfg: Config, round_id: int, page, emit=None) -> None:
    db.set_phase(round_id, "detail")
    human = Humanizer(cfg)
    offers = _pending_detail_offers(db, round_id, cfg)
    log.info("详情阶段：待处理商品 %s 个", len(offers))
    processed = 0
    cap = cfg.max_detail_pages_per_round
    for offer in offers:
        if processed >= cap:
            log.warning("已达单轮详情上限 %s，剩余留待下一轮。", cap)
            break
        processed += 1
        try:
            _capture_one_pw(db, cfg, human, round_id, offer, page, emit=emit)
        except RuntimeError:
            raise
        except Exception as exc:
            log.exception("详情抓取意外失败：%s", offer["product_url"])
            db.mark_failure(round_id, offer["shop_key"], offer["offer_id"], 1, str(exc))
        if processed % cfg.batch_size == 0:
            human.before_batch_rest()
    log.info("详情阶段结束：本轮处理 %s 个商品", processed)


def _capture_one_pw(db: Database, cfg: Config, human: Humanizer, round_id: int, offer, page,
                    emit=None) -> None:
    shop_key = offer["shop_key"]
    offer_id = offer["offer_id"]
    product_url = offer["product_url"]
    shop_url = offer["shop_url"]
    shop_name = offer["shop_name"]
    max_attempts = cfg.max_attempts_per_page
    collected_at = utcnow()

    day = cst_date()
    if offer["list_title"] and db.inventory_exists_by_name(shop_key, offer["list_title"], day):
        db.mark_skipped(round_id, shop_key, shop_url, shop_name, offer_id, product_url,
                        offer["list_title"], note="今日已有同名库存，跳过")
        log.info("店铺 %s 商品「%s」今日已有库存，跳过详情抓取", shop_key, offer["list_title"][:40])
        return
    if db.inventory_exists(shop_key, offer_id, day):
        db.mark_skipped(round_id, shop_key, shop_url, shop_name, offer_id, product_url,
                        offer["list_title"])
        log.info("店铺 %s 商品 %s 今日已有库存，跳过详情抓取", shop_key, offer_id)
        return

    def emit_detail(event: str, **kw: object) -> None:
        if emit is not None:
            emit(event, shop_key=shop_key, **kw)

    for attempt in range(1, max_attempts + 1):
        try:
            payload = browser_pw.capture_detail(page, product_url, cfg, human, emit=emit_detail)
        except DetailParseFailed as exc:
            raw_path = save_raw_page(cfg, round_id, offer_id, exc.html) if exc.html else ""
            note = f"解析失败：{exc}；原始页面：{raw_path}"
            db.mark_failure(round_id, shop_key, offer_id, attempt, note)
            emit_detail("detail_fail", offer_id=offer_id, phase="detail", note=note)
            log.warning("第 %s 次解析失败：%s", attempt, note)
            if attempt < max_attempts:
                human.sleep(human.retry_delay(attempt))
            continue
        except RuntimeError:
            raise
        except Exception as exc:
            note = f"访问异常：{exc}"
            db.mark_failure(round_id, shop_key, offer_id, attempt, note)
            emit_detail("detail_fail", offer_id=offer_id, phase="detail", note=note)
            log.warning("第 %s 次访问失败：%s", attempt, note)
            if attempt < max_attempts:
                human.sleep(human.retry_delay(attempt))
            continue

        img = extract_main_image(payload["html"])
        db.upsert_product(offer_id, product_url, payload["product_name"], img)
        rows = []
        for sku in payload["rows"]:
            sku_id = sku.get("sku_id") or hashlib.sha1(
                f"{offer_id}|{sku['sku_name']}".encode("utf-8")
            ).hexdigest()[:16]
            rows.append({
                "round_id": round_id, "shop_key": shop_key, "shop_url": shop_url,
                "shop_name": shop_name, "offer_id": offer_id, "product_url": product_url,
                "product_name": payload["product_name"] or offer["list_title"],
                "sku_id": sku_id, "sku_name": sku["sku_name"], "sku_price": sku["sku_price"],
                "sku_stock": sku["sku_stock"], "collected_at": collected_at,
                "page_status": "成功", "attempt": attempt,
            })
        db.clear_failures(round_id, shop_key, offer_id)
        db.save_snapshot_rows(round_id, shop_key, rows)
        log.info("店铺 %s 商品 %s 抓取成功：%s 个 SKU（第 %s 次）", shop_key, offer_id, len(rows), attempt)
        return
