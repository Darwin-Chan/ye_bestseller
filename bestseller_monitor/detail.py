"""商品详情页抓取。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import dedupe, rounds
from .config import Config
from .db import (SKU_IMAGE_FILLED, SKU_IMAGE_NONE, SKU_IMAGE_OWN, Database, cst_date,
                 utcnow)
from .parse import (extract_main_image, extract_skus_from_html, extract_title,
                    is_single_spec_offer)

log = logging.getLogger(__name__)


class DetailParseFailed(Exception):
    """详情页 SKU 结构无法解析，需要人工校准。"""

    def __init__(self, message: str, html: str = ""):
        super().__init__(message)
        self.html = html


def parse_detail_html(html: str, product_url: str) -> dict:
    """解析并校验详情页，只有所有 SKU 都有库存时才可作为成功快照写入。"""
    try:
        rows = extract_skus_from_html(html)
        product_name = extract_title(html) or ""
    except Exception as exc:
        raise DetailParseFailed(f"详情页 SKU 解析异常：{exc}", html=html) from exc
    if not rows:
        if is_single_spec_offer(html):
            raise DetailParseFailed(
                f"单规格商品缺少商品级可售量：{product_url}", html=html,
            )
        raise DetailParseFailed(f"详情页未解析到 SKU：{product_url}", html=html)
    if any(row.get("sku_stock") is None for row in rows):
        raise DetailParseFailed(f"详情页存在缺失库存的 SKU：{product_url}", html=html)
    return {"product_name": product_name, "rows": rows}


def save_raw_page(cfg: Config, round_id: int, offer_id: str, html: str) -> Path:
    d = cfg.raw_page_dir / f"round_{round_id}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{offer_id}.html"
    path.write_text(html, encoding="utf-8")
    return path


def readable(html: str) -> bool:
    """这份 html 已经能读出 SKU 行了吗——详情页「可读」的判据。

    等页面可读（`detail_visit.ReadyDetailVisit`）与判解析失败用的是同一个判据：
    有 SKU 行才叫读到了，页面还在渲染、或者压根是拦截页时都不是。
    """
    try:
        return bool(extract_skus_from_html(html))
    except Exception:  # noqa: BLE001 —— 读不出来就是「还不可读」，由解析那一侧报失败
        return False


# ---------- 一次详情观测的规则（点击式列表与逐店补采共用） ----------

class Outcome(str, Enum):
    """一次详情观测的结果。module 只返回结果，事件由调用方按它记。"""

    SUBMITTED = "submitted"          # 观测成功，已提交库存快照
    SKIPPED_TODAY = "skipped_today"  # 今天已经采过，跳过（不消耗详情机会）
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"   # 本轮尝试额度已用尽，没有读页面
    FAILED = "failed"                # 观测失败，已记失败行
    DUPLICATE = "duplicate"          # 同一次遍历里已经观测过这个商品


class FailureKind(str, Enum):
    """这次观测是怎么没成的：调用方据此决定记哪几条事件。"""

    READ = "read"        # 读不到页面内容（导航、连接、取内容都算）
    PARSE = "parse"      # 读到了页面，但解析不了（或取主图时崩了）


@dataclass(frozen=True)
class DetailTarget:
    """要取观测的那个商品：哪个店铺、哪条榜单行、机会挂在哪个标识上。

    `offer_id` 是已知的商品编号：点击式列表从弹窗地址读出来（卡片已经点开，但还没读内容），
    逐店补采本来就知道。两条路因此都能「先算账、再读页面」。
    """

    shop_key: str
    shop_url: str
    shop_name: str
    product_url: str
    slot_key: str
    offer_id: str
    list_title: str | None = None
    duplicate: bool = False   # 同一次遍历里已经见过这个商品（不再补写跳过行）


@dataclass(frozen=True)
class Observation:
    """adapter 交回的一次观测：成功 payload，或失败原因。

    失败文案的用词收在这里（读不到页 / 解析失败 / 解析崩了），由 `observe_html` 挑。
    """

    payload: dict | None = None
    failure: str | None = None
    raw_html: str = ""        # 失败时的原始页内容，有则存档供校准
    kind: FailureKind | None = None

    @property
    def ok(self) -> bool:
        """这次读到了页面吗（读到了才有 payload）。调用方按它决定记哪条事件。"""
        return self.payload is not None

    @property
    def sku_count(self) -> int:
        """读到的 SKU 行数；没读到就是 0（事件备注的 `sku_count=` 一律从这里取值）。"""
        return 0 if self.payload is None else len(self.payload["rows"])

    @classmethod
    def read_failed(cls, exc: Exception) -> "Observation":
        return cls(failure=f"详情页读取失败：{exc}", kind=FailureKind.READ)

    @classmethod
    def denied(cls, html: str = "") -> "Observation":
        """落到反爬拦截页（deny）：不是这个商品自己的问题，但得说清是它没读到。

        账目与阈值在 `guard.ready_detail_page()`；这一条只是把理由写进失败记录、留下原始页
        供校准（候选 04，补采路径与点击路径一样「deny 不算这个商品的失败原因」）。"""
        return cls(failure="详情页被反爬拦截（deny）", raw_html=html, kind=FailureKind.READ)

    @classmethod
    def parse_failed(cls, exc: Exception, html: str = "") -> "Observation":
        return cls(failure=f"解析失败：{exc}", raw_html=html, kind=FailureKind.PARSE)

    @classmethod
    def parse_crashed(cls, exc: Exception, html: str = "") -> "Observation":
        return cls(failure=f"详情页解析异常：{exc}", raw_html=html, kind=FailureKind.PARSE)


def observe_html(html: str, product_url: str) -> Observation:
    """把已经取得的详情 HTML 翻译成一次观测：解析 → 取主图。

    页面读取、等待和异常重试由 `detail_visit` 负责；这里的 interface 只接收当前 HTML，
    因此不会再让调用方缓存 guard 之前的旧页面。解析失败与解析崩了都算解析失败，并保留
    原始页供校准。
    """
    try:
        payload = parse_detail_html(html, product_url)
        payload["main_image_url"] = extract_main_image(html)
    except DetailParseFailed as exc:
        return Observation.parse_failed(exc, exc.html)
    except Exception as exc:  # noqa: BLE001 —— 解析崩了也留原始页（解析失败的一种）
        return Observation.parse_crashed(exc, html)
    return Observation(payload=payload)


@dataclass(frozen=True)
class CaptureResult:
    outcome: Outcome
    offer_id: str | None = None
    sku_count: int | None = None   # 提交成功时的 SKU 行数（调用方记事件用）


def capture_observation(db: Database, cfg: Config, human, round_id: int,
                        target: DetailTarget, read, *, attempts: int | None = None,
                        on_attempt_failed=None) -> CaptureResult:
    """一次详情观测的完整规则。

    - 进详情之前先问轮次：跨天、已过截止线、已终态就不再开始新的详情采集。
    - 同日去重按商品编号判断：跳过时不读页面、不消耗详情机会（ADR-0001）。调用方若已经在
      打开页面前占过一次机会（点击式列表必须先占——预算就是用来限制详情访问的），
      这次跳过会把它退还。
    - 初次访问与补采共享同一份尝试额度。`attempts=None`（逐店补采）时额度用尽就不读了；
      `attempts=1`（点击式列表）这一次既然已经点开，读一次就够，失败交给随后的补采接手。
    - 提交之后再看一次停止判定：已提交的数据保留，判定不回滚它。

    `read` 是调用方提供的当前详情观测回调；页面取得、验证、等待与关闭都由详情访问 seam
    及其所属调用方负责，回调只返回一次 `Observation`。
    失败记录与文案、成功提交都在这里；事件由调用方按 `CaptureResult` 记。
    """
    shop_key = target.shop_key
    offer_id = target.offer_id

    rounds.ensure_workable(db, round_id, utcnow())

    if _collected_today(db, shop_key, offer_id):
        dedupe.release_slot(db, round_id, shop_key, target.slot_key)
        return _skip_today(db, round_id, target, offer_id)
    first_attempt = dedupe.next_attempt(db, round_id, shop_key, offer_id)
    if attempts is None and first_attempt > cfg.max_attempts_per_page:
        log.info("店铺 %s 商品 %s 本轮尝试已用尽（%s/%s），不再补采",
                 shop_key, offer_id, first_attempt - 1, cfg.max_attempts_per_page)
        return CaptureResult(Outcome.ATTEMPTS_EXHAUSTED, offer_id)
    if target.duplicate:
        # 同一次遍历里已经观测过这个商品：不再提交，也不动账本（改前点击路径如此）。
        return CaptureResult(Outcome.DUPLICATE, offer_id)

    dedupe.claim_slot(db, round_id, shop_key, target.slot_key,
                      cfg.max_detail_opportunities_per_round)
    dedupe.bind_card_to_offer(db, round_id, shop_key, target.slot_key, offer_id)

    last_attempt = (cfg.max_attempts_per_page if attempts is None
                    else first_attempt + attempts - 1)
    return _capture_attempts(db, cfg, human, round_id, target, read, offer_id,
                             first_attempt, last_attempt, on_attempt_failed)


def _collected_today(db: Database, shop_key: str, offer_id: str) -> bool:
    """今天已经有这个商品的成功库存观测吗（同日去重的唯一判据）。"""
    return db.inventory_exists(shop_key, offer_id, cst_date())


def _skip_today(db: Database, round_id: int, target: DetailTarget,
                offer_id: str) -> CaptureResult:
    """记一条跳过：不访问详情、不消耗机会、不构成库存证据（ADR-0001/ADR-0002）。"""
    if not target.duplicate:
        db.mark_skipped(round_id, target.shop_key, target.shop_url, target.shop_name,
                        offer_id, target.product_url, target.list_title)
    log.info("店铺 %s 商品 %s 今日已有库存，跳过详情抓取", target.shop_key, offer_id)
    return CaptureResult(Outcome.SKIPPED_TODAY, offer_id)


def _capture_attempts(db: Database, cfg: Config, human, round_id: int, target: DetailTarget,
                      read, offer_id: str, first_attempt: int, last_attempt: int,
                      on_attempt_failed) -> CaptureResult:
    """按额度试到成功为止：失败记一行，最后一次失败就以 FAILED 收场。"""
    note = ""
    for attempt in range(first_attempt, last_attempt + 1):
        observation = read()
        payload = observation.payload if observation.failure is None else None
        if payload is None:
            note = _failure_note(cfg, round_id, offer_id, observation)
            db.mark_failure(round_id, target.shop_key, offer_id, attempt, note,
                            shop_url=target.shop_url, shop_name=target.shop_name,
                            product_url=target.product_url, product_name=target.list_title)
            log.warning("店铺 %s 商品 %s 第 %s 次失败：%s",
                        target.shop_key, offer_id, attempt, note)
            if on_attempt_failed is not None:
                on_attempt_failed(note)
            if attempt < last_attempt:
                human.sleep(human.retry_delay(attempt))
            continue

        from .product_images import acquire
        image_evidence = acquire(payload.get('main_image_url'))
        _attach_sku_image_evidence(payload["rows"], image_evidence, acquire)
        db.submit_inventory_snapshot(
            round_id=round_id,
            shop_key=target.shop_key,
            shop_url=target.shop_url,
            shop_name=target.shop_name,
            offer_id=offer_id,
            product_url=target.product_url,
            list_title=target.list_title,
            detail_title=payload["product_name"],
            main_image_url=payload.get("main_image_url"),
            sku_rows=payload["rows"],
            collected_at=utcnow(),
            attempt=attempt,
            image_evidence=image_evidence,
        )
        # 提交之后再看一次：已提交的数据保留，停止判定不回滚它。
        rounds.ensure_workable(db, round_id, utcnow())
        log.info("店铺 %s 商品 %s 抓取成功：%s 个 SKU（第 %s 次尝试）",
                 target.shop_key, offer_id, len(payload["rows"]), attempt)
        return CaptureResult(Outcome.SUBMITTED, offer_id, sku_count=len(payload["rows"]))
    return CaptureResult(Outcome.FAILED, offer_id)


def _attach_sku_image_evidence(rows: list[dict], main_image: dict, acquire) -> None:
    """给每个 SKU 行挂上这次观测的图证据（来源三态见 `db.SKU_IMAGE_*`）。

    图地址来自票 01 的载荷（`row["sku_image_url"]`，None = 页面没配图）。空图不是失败：
    用当次观测的商品主图代填；当次主图也缺（没地址或下载败）就记无图行。
    有地址但下载/校验败是失败行：如实记因、不代填、不借相邻观测的图——图失败不进失败
    快照、不占详情重试账，只随流水行落库，不挡这次库存提交。
    """
    for row in rows:
        url = str(row.get("sku_image_url") or "").strip()
        if url:
            image = acquire(url)
            if "content" in image:
                row["sku_image_evidence"] = {"url": url, "source": SKU_IMAGE_OWN,
                                             "hash": image["hash"], "mime": image["mime"],
                                             "content": image["content"]}
            else:
                row["sku_image_evidence"] = {"url": url, "source": SKU_IMAGE_OWN,
                                             "error": image.get("error")}
        elif "content" in main_image:
            row["sku_image_evidence"] = {"url": None, "source": SKU_IMAGE_FILLED}
        else:
            row["sku_image_evidence"] = {"url": None, "source": SKU_IMAGE_NONE}


def _failure_note(cfg: Config, round_id: int, offer_id: str,
                  observation: Observation) -> str:
    """失败文案：原因 + 有原始页时存档并附上路径。"""
    note = observation.failure or "详情页读取失败"
    if observation.raw_html:
        note = f"{note}；原始页面：{save_raw_page(cfg, round_id, offer_id, observation.raw_html)}"
    return note
