"""独立分析入口：只读库存，一次一致读取产生与源库解耦的快照。"""
from __future__ import annotations

import copy
import base64
import logging
import sqlite3
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from . import crawler_identity
from .analysis_store import DraftStore, SIDE_EXCLUSION, SIDE_STANDALONE, source_of
from .config import machine_id_of
from .db import utcnow
from .matching import (MATCH_DISABLED, PHASE_ASSEMBLE, PHASE_JUDGE, PHASE_RECALL, PHASE_VERIFY,
                       MatchingConfig, MatchingService, ModelConfig, ORIGIN_CHANGED,
                       STATUS_DISABLED, identity, matching_summary, offer_of, state_of_status,
                       summarize_group, version)
from .report import report_name, write_report

log = logging.getLogger(__name__)

DEFAULT_DRAFT_FILE = "analysis-drafts.sqlite"
DEFAULT_OUTPUT_DIR = "output"
# 机器编号的权威文件（与 analysis.toml 同在 config/ 下，ADR-0034）：分析工具没有身份，
# 判断与决定的来源列要写本机编号，只从它借读 [machine] machine_id 这一个键。
MACHINE_CONFIG_NAME = "config.toml"

# 判断运行的五个阶段：次序就是程序真跑的次序（ADR-0044 的「阶段行」按它显示）。
# 阶段名只在 _PHASE_OF 里写一遍（matching 报它自己的四个标识），次序由它推出来。
_PHASE_OF = {PHASE_RECALL: '召回候选同款', PHASE_VERIFY: '检查模型可用性',
             PHASE_JUDGE: '逐对判断同款', PHASE_ASSEMBLE: '装配同款组'}
PHASES = ('冻结库存数据', *_PHASE_OF.values())
_PHASE_INDEX = {phase_id: PHASES.index(label) for phase_id, label in _PHASE_OF.items()}
JUDGING_PHASE = _PHASE_INDEX[PHASE_JUDGE]   # 「逐对判断同款」在第几步：预计时长从这一步起算
# 判断跑够这么久才给预计：一两条的样本次算出的是噪声（规格「头一分钟显示正在估算」）。
ETA_MIN_SAMPLE_SEC = 60.0
# 库存库与判断缓存读写失败（sqlite3/OSError）对页面说的话：HTTP 层与 job 失败态同源。
# 分两句是因为动的是两个库：冻结阶段读库存库，之后的判断缓存与草稿库另有说法。
READ_FAILURE_TEXT = '读取库存数据失败，请检查分析配置和数据库后重试'
STORE_FAILURE_TEXT = '判断缓存或分析草稿读写失败，请检查分析配置与磁盘后重试'


# 一次运行的终态：到这里就没有任何写入了（wait_terminal 的契约，ADR-0044）。
TERMINAL_STATES = ('ready', 'failed')


def _fmt_eta(seconds: float) -> str:
    """预计时长的说法：进整到分钟，最少说 1 分钟。"""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f'{minutes} 分钟'
    hours, rest = divmod(minutes, 60)
    return f'{hours} 小时 {rest} 分钟' if rest else f'{hours} 小时'


def _failure_text(exc: BaseException, storage_text: str) -> str:
    """硬失败对页面说的话：域错误说原文，库与文件读写用调用方给的那句（在动哪个库谁知道）。"""
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return str(exc)
    if isinstance(exc, (sqlite3.Error, OSError)):
        return storage_text
    return '分析没能完成，请重试'


class _Progress:
    """一次分析运行的进度读数：自己的锁，不占服务锁（页面轮询要读它，见 ADR-0044）。

    只有运行观测——不落库、不跨机（与「判断用量」同规）。`failure` 留着原始异常，
    同步的 `start()` 要原样抛回给既有调用方。停止标志也住在这里：请求停下只是置它，
    判断循环在每对之间认领（票 02）。
    """

    def __init__(self, analysis_id: str, start: str, end: str, clock=time.monotonic, *,
                 phase_index: int = 0, retry: bool = False, stop_timeout: float = 30.0):
        self.id = analysis_id
        self.start, self.end = start, end
        # 重试那次运行（票 02）：页面据此说「（重试）」，刷新接回也认得出它。
        self.retry = retry
        # 在途判断最多等多久：模型调用自己的超时，页面上的「最多 N 秒」照它说。
        self.stop_timeout = stop_timeout
        self.failure = None
        self._clock = clock
        self._started = clock()
        self._judging_started = None
        self._state = 'matching'
        self._error = ''
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._fields = {'phase_index': phase_index, 'products': 0, 'eligible': 0,
                        'missing_evidence': 0, 'todo': 0, 'cached_hits': 0, 'blocked': 0,
                        'judged': 0, 'failed': 0}

    def update(self, **fields) -> None:
        with self._lock:
            index = fields.pop('phase_index', None)
            if index is not None:
                self._fields['phase_index'] = index
                if index == JUDGING_PHASE and self._judging_started is None:
                    self._judging_started = self._clock()
            self._fields.update(fields)

    def reporter(self):
        """给匹配层用的回调：认它报的阶段标识，其余字段原样收下。"""
        def report(fields):
            index = _PHASE_INDEX.get(fields.pop('phase', None))
            if index is not None:
                fields['phase_index'] = index
            self.update(**fields)
        return report

    def finish(self) -> None:
        with self._lock:
            self._state = 'ready'
        self._finished.set()

    def fail(self, message: str, exception: BaseException) -> None:
        with self._lock:
            self._state = 'failed'
            self._error = message
            self.failure = exception
        self._finished.set()

    def request_stop(self) -> bool:
        """请求停下这次运行：置标志、状态转「正在停止」；收尾由运行自己走完（票 02）。

        已经到终态的运行不再受理——它没有还剩的写入了，停止请求落空是实话。
        """
        with self._lock:
            if self._state in TERMINAL_STATES:
                return False
            self._stop.set()
            self._state = 'stopping'
            return True

    def is_terminal(self) -> bool:
        """这次运行是否已经收尾（终态＝不再有任何写入，见 wait_terminal 的契约）。"""
        with self._lock:
            return self._state in TERMINAL_STATES

    def stop_requested(self) -> bool:
        """给匹配层用的回调：该不该停（判断循环在每对之间问一次）。"""
        return self._stop.is_set()

    def wait(self, timeout) -> bool:
        return self._finished.wait(timeout)

    def snapshot(self) -> dict:
        with self._lock:
            payload = dict(self._fields)
            payload.update(id=self.id, state=self._state, phases=list(PHASES),
                           start=self.start, end=self.end, error=self._error,
                           elapsed_sec=round(self._clock() - self._started, 1),
                           eta_text=self._eta_text(), retry=self.retry,
                           stop_requested=self._stop.is_set(),
                           stop_timeout_sec=int(round(self.stop_timeout)))
        return payload

    def _eta_text(self) -> str:
        """按本次实测速率估剩余时长。

        样本够才给（判断阶段跑满 ETA_MIN_SAMPLE_SEC 且至少判完一对）——一两条的样本
        算出的是噪声；不足时给空串，页面显示「正在估算…」（规格「头一分钟显示正在估算」）。
        """
        judged = self._fields['judged']
        remaining = self._fields['todo'] - judged
        if self._judging_started is None or judged <= 0 or remaining <= 0:
            return ''
        span = self._clock() - self._judging_started
        if span < ETA_MIN_SAMPLE_SEC:
            return ''
        return _fmt_eta(remaining * span / judged)


@dataclass(frozen=True)
class AnalysisConfig:
    database: Path
    full_capture_weekday: int = 1  # ISO: Monday=1
    matching: MatchingConfig | None = None
    # 分析草稿库。直接构造时缺省与库存库同目录；配置文件里缺省在配置目录（见 from_file）。
    store: Path | None = None
    # 离线报告的导出目录：缺省项目的 output 文件夹；配置文件里同样有缺省（见 from_file）。
    output: Path | None = None
    # 本机编号（来源列的写入用，票 02）：配置里读不出就是空串，来源列如实留空。
    machine: str = ''

    def __post_init__(self):
        if self.store is None:
            object.__setattr__(self, 'store', self.database.with_name(DEFAULT_DRAFT_FILE))
        if self.output is None:
            object.__setattr__(self, 'output', Path(__file__).resolve().parent.parent / DEFAULT_OUTPUT_DIR)
        if self.matching and self.matching.cache.resolve() == self.database.resolve():
            raise ValueError('同款缓存不能使用库存数据库')
        reserved = {self.database.resolve()}
        if self.matching:
            reserved.add(self.matching.cache.resolve())
        if self.store.resolve() in reserved:
            raise ValueError('分析草稿库不能与库存数据库或同款缓存共用文件')

    @classmethod
    def from_file(cls, path: Path) -> AnalysisConfig:
        try:
            with path.open("rb") as stream:
                document = tomllib.load(stream)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"找不到分析配置：{path}\n"
                f"请复制 {path.with_name('analysis.example.toml')} 为 {path}，"
                "再改成本机的值（database、matching.cache 等）。"
            ) from None
        cfg = document['analysis']
        weekday = cfg.get("full_capture_weekday", 1)
        if type(weekday) is not int or not 1 <= weekday <= 7:
            raise ValueError("全量抓取提醒星期必须为 1 至 7")
        database = Path(cfg["database"])
        if not database.is_absolute():
            database = path.resolve().parent / database
        store = Path(cfg.get("store", DEFAULT_DRAFT_FILE))
        if not store.is_absolute():
            store = path.resolve().parent / store
        # 缺省相对配置目录回到项目根：仓库里真配置放 config/，导出的报告进项目的 output。
        output = Path(cfg.get('output', Path('..') / DEFAULT_OUTPUT_DIR))
        if not output.is_absolute():
            output = path.resolve().parent / output
        matching = None
        if 'matching' in document:
            options = document['matching']
            cache = Path(options.get('cache', 'matching.sqlite'))
            if not cache.is_absolute():
                cache = path.resolve().parent / cache
            if cache.resolve() == database.resolve():
                raise ValueError('同款缓存不能使用库存数据库')
            matching = MatchingConfig(cache.resolve(), ModelConfig(**options.get('model', {})),
                ModelConfig(**options['vision']) if 'vision' in options else None,
                options.get('mode', 'disabled'), options.get('concurrency', 2), options.get('candidates', 6),
                options.get('min_score', 4))
        return cls(database.resolve(), weekday, matching, store.resolve(), output.resolve(),
                   machine_id_of(path.with_name(MACHINE_CONFIG_NAME)))


class AnalysisService:
    def __init__(self, config: AnalysisConfig, *, running=None, clock=time.monotonic):
        self.config = config
        self.running = running or crawler_identity.is_running
        self._clock = clock
        self._snapshots = {}
        # 运行中的分析（进度读数）按分析号记着：页面靠它轮询，也靠它刷新接回（票 01）。
        # 与快照同一寿命，服务重启后自然清空（页面按号取不到就回日期页）。
        self._jobs = {}
        self._jobs_lock = threading.Lock()
        self._lock = threading.Lock()
        self.matcher = MatchingService(config.matching, config.machine) if config.matching and config.matching.mode != 'disabled' else None

    @property
    def store(self):
        """草稿库跟着当前配置走：配置换了库或换了存储位置立即生效。"""
        return DraftStore(self.config.store, self.config.machine)

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
        return {"full_capture_weekday": self.config.full_capture_weekday,
                "machine": self.config.machine}

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

    def start_job(self, start, end, acknowledged=False):
        """受理一次分析：立刻给分析号，冻结与判断在后台跑（进度用 job_state 读）。

        同步只做两件便宜事：日期顺序、以及「有采集在跑要先确认」；其余全进后台，
        失败如实落在 job_state 里、由页面就地报错（ADR-0044，票 01）。
        """
        if date.fromisoformat(start) >= date.fromisoformat(end):
            raise ValueError("结束日期必须晚于开始日期")
        if self.running() and not acknowledged:
            return {"needs_confirmation": True}
        progress = _Progress(uuid4().hex, start, end, self._clock,
                             stop_timeout=self._stop_timeout())
        return self._claim(progress, self._run_job, 'analysis')

    def job_state(self, analysis_id):
        """这次运行到哪了：阶段、对数、时长、预计（样本不足不给）、失败原因。"""
        return self._job(analysis_id).snapshot()

    def request_stop(self, analysis_id):
        """请求停下这次运行（协作停止）：立刻返回读数，收尾由运行自己走完（票 02）。

        停止请求不打断装配与排名（那一段改了会毁掉本次结果）；判断循环在每对之间认领它，
        在途的那几个模型调用等它们自己回来（最多一个模型超时）。
        """
        progress = self._job(analysis_id)
        progress.request_stop()
        return progress.snapshot()

    def wait_terminal(self, analysis_id, timeout=None):
        """等这次运行到终态；返回即不再有任何写入——放锁要排在它之后（ADR-0044）。

        被停止的运行也算走到终态：停止只是让判断提前收摊，余数批提交、装配、快照落内存
        一件都不少（都排在 `_finished` 之前）。
        """
        progress = self._job(analysis_id)
        if not progress.wait(timeout):
            raise TimeoutError('分析还在运行')
        return progress.snapshot()

    def start(self, start, end, acknowledged=False):
        """同步跑完一次分析（既有调用方与测试的入口）：失败原样抛回给调用方。"""
        accepted = self.start_job(start, end, acknowledged)
        if accepted.get('needs_confirmation'):
            return accepted
        progress = self._job(accepted['id'])
        progress.wait(None)
        if progress.failure is not None:
            raise progress.failure
        return self.get(accepted['id'])

    def _job(self, analysis_id):
        with self._jobs_lock:
            progress = self._jobs.get(analysis_id)
        if progress is None:
            raise ValueError('分析已不存在，请重新选择日期')
        return progress

    def _stop_timeout(self):
        """在途判断最多等多久：模型调用自己的超时（配置里可改，缺省 30 秒）。

        caption 模式下逐对判断走视觉服务，两个超时里取大的那个才是实话。
        """
        matching = self.config.matching
        if matching is None:
            return 30.0
        return max([matching.model.timeout] + ([matching.vision.timeout] if matching.vision else []))

    def _run_job(self, progress):
        """后台跑完一次分析：冻结（读库存库）→ 套回人工决定 → 匹配排序（动判断缓存与草稿库）。

        两段各自收失败：动的是两个不同的库，对页面说的话也不一样（规格要求
        「就地显示真实原因」，缓存写不进去时说「读取库存数据失败」就是甩锅）。
        """
        try:
            snapshot = self._freeze(progress.id, progress.start, progress.end)
        except Exception as exc:  # noqa: BLE001 —— 失败原因要原样交给页面
            return self._fail(progress, '冻结库存', exc, READ_FAILURE_TEXT)
        try:
            # 「继续上次分析」读原快照；新日期区间走这里：重算销量后，把已保存的
            # 人工分组按商品版本套回来（票 09），不适用的留在待确认由人工处理。
            decisions = self.store.ledger()
            reuse_decisions(snapshot, decisions)
            with self._lock:
                self._match(snapshot, progress=progress.reporter(), stop=progress.stop_requested)
                mark_information_changes(snapshot, recorded_versions(decisions))
                rank_groups(snapshot)
                self._apply_conflicts(snapshot)
                self._snapshots[snapshot["id"]] = snapshot
            progress.finish()
        except Exception as exc:  # noqa: BLE001 —— 失败原因要原样交给页面
            self._fail(progress, '判断与装配', exc, STORE_FAILURE_TEXT)

    def _freeze(self, analysis_id, start, end):
        """冻结一次分析的输入：区间库存、商品证据版本与区间销量。"""
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
            has_history = conn.execute("SELECT 1 FROM sqlite_master WHERE name='product_information_versions'").fetchone()
            for product in products:
                version = conn.execute('''SELECT v.*, a.mime, a.content
                    FROM product_information_versions v LEFT JOIN product_image_assets a
                    ON a.content_hash=v.content_hash
                    WHERE v.shop_key=? AND v.offer_id=? AND v.observed_date<=?
                    ORDER BY v.observed_date DESC,v.observed_at DESC,v.id DESC LIMIT 1''',
                    (product['shop_key'], product['offer_id'], end)).fetchone() if has_history else None
                product['image_data'] = None
                product['image_error'] = '该日期没有历史图片'
                product['information_version'] = version['id'] if version else None
                product['information_complete'] = bool(version and version['product_name'] and version['content_hash'])
                product['information_note'] = ''
                if version:
                    product['product_name'] = version['product_name']
                    product['image_hash'] = version['content_hash']
                    product['image_error'] = version['image_error']
                    if not version['product_name']:
                        product['information_note'] = '仅图片证据：名称待重新观测，尚未形成完整商品信息版本'
                    if version['content']:
                        product['image_data'] = _data_url(version['mime'], version['content'])
                else:
                    historical = [r for r in rows if r['shop_key']==product['shop_key'] and r['offer_id']==product['offer_id'] and r['date']<=end]
                    product['product_name'] = max(historical, key=lambda r:r['date'])['product_name'] if historical else None
                product['main_image_url'] = None
        calculations = calculate_inventory(rows, start, end)
        for product in products:
            key = (product["shop_key"], product["offer_id"])
            result = calculations.get(key, {"sales": 0, "points": [], "skus": []})
            product.update(result)
        return {"id": analysis_id, "start": start, "end": end,
                "frozen_at": utcnow(), "inventory": rows, "products": products,
                "shops": shops, "dirty": False, "saved_at": None, "groups": []}

    def _match(self, snapshot, reason='', progress=None, stop=None):
        if self.matcher:
            snapshot['groups'] = self.matcher.suggest(snapshot['products'], snapshot['groups'],
                                                      snapshot.get('excluded', ()), reason=reason,
                                                      progress=progress, stop=stop)
        else:
            for p in snapshot['products']:
                p.update(origin='新商品', match_label='暂无匹配同款', candidate_groups=[],
                         matching_status=STATUS_DISABLED, matching_state=MATCH_DISABLED,
                         matching_source='')
        snapshot['matching'] = matching_summary(snapshot['products'])

    def retry_matching(self, analysis_id, progress=None, stop=None):
        with self._lock:
            if analysis_id not in self._snapshots:
                raise ValueError('分析已不存在')
            snapshot = copy.deepcopy(self._snapshots[analysis_id])
            # 信息变更是本次分析展示的事实（重开的草稿也带着它）：重试只重跑匹配，
            # 不把已经标出的变更刷回未标注。
            marked = {identity(p) for p in snapshot['products'] if p.get('origin') == ORIGIN_CHANGED}
            self._match(snapshot, reason='重试', progress=progress, stop=stop)
            for p in snapshot['products']:
                if identity(p) in marked:
                    p['origin'] = ORIGIN_CHANGED
            rank_groups(snapshot)
            attach_conflicts(snapshot)
            self._snapshots[analysis_id] = snapshot
            return copy.deepcopy(snapshot)

    def retry_matching_job(self, analysis_id):
        """受理一次「模型匹配同款」重试：立刻给读数，判断在后台跑（票 02，ADR-0044）。

        与首次分析共用同一份读数、同一块遮罩：读数就按分析号记，页面照原样轮询、
        刷新也照原样接回。重试不冻结库存（快照已冻结），阶段从「召回候选同款」起。
        """
        # 先挡一道：正在跑的那趟全程提着服务锁，第二道判定在 _claim 里（那次才是判定）。
        if self._job_in_flight(analysis_id) is not None:
            raise ValueError('这次分析还在运行中')
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                raise ValueError('分析已不存在')
            start, end = snapshot['start'], snapshot['end']
        progress = _Progress(analysis_id, start, end, self._clock, phase_index=1, retry=True,
                             stop_timeout=self._stop_timeout())
        return self._claim(progress, self._run_retry, 'analysis-retry')

    def _claim(self, progress, target, thread_prefix):
        """受理念头：登记并起后台线程，同一份快照同时只允许一趟在跑。

        判定与登记在同一把锁里：同一份快照跑两趟会互相盖写判断缓存与内存快照
        （另一个标签页、另一次点击），而「先看有没有在跑、再去登记」中间有缝。
        """
        with self._jobs_lock:
            running = self._jobs.get(progress.id)
            if running is not None and not running.is_terminal():
                raise ValueError('这次分析还在运行中')
            self._jobs[progress.id] = progress
        threading.Thread(target=target, args=(progress,), daemon=True,
                         name=f'{thread_prefix}-{progress.id[:8]}').start()
        return {"id": progress.id, "state": "matching"}

    def _job_in_flight(self, analysis_id):
        """这个分析号上还在跑（没到终态）的那一次运行；没有就回 None。"""
        with self._jobs_lock:
            progress = self._jobs.get(analysis_id)
        return progress if progress is not None and not progress.is_terminal() else None

    def _fail(self, progress, stage, exc, storage_text):
        """失败收尾：日志留全栈，页面上说人话（_failure_text 按异常种类挑文案）。"""
        log.exception('分析运行失败（%s）', stage)
        progress.fail(_failure_text(exc, storage_text), exc)

    def _run_retry(self, progress):
        """后台跑完一次重试：动的是判断缓存与草稿库（重试不读库存库）。"""
        try:
            self.retry_matching(progress.id, progress=progress.reporter(),
                                stop=progress.stop_requested)
            progress.finish()
        except Exception as exc:  # noqa: BLE001 —— 失败原因要原样交给页面
            self._fail(progress, '重试判断', exc, STORE_FAILURE_TEXT)

    def get(self, analysis_id):
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                # 内存里没有就回落到已保存版本：重启后仍能打开同一次分析。
                stored = self.store.read(analysis_id)
                if stored is None:
                    raise ValueError("分析已不存在，请重新选择日期")
                snapshot = self._restore(stored)
                self._snapshots[analysis_id] = snapshot
            return copy.deepcopy(snapshot)

    def _restore(self, stored):
        """把磁盘上的草稿还原成内存工作草稿：已保存版本没有未保存修改。"""
        snapshot = self._rehydrate(stored.payload)
        snapshot['dirty'] = False
        snapshot['saved_at'] = stored.saved_at
        # 冲突不在草稿正文里（那是每次现读账的最新事实）：重开这一刻重挂一次，
        # 保存之后又收进来的冲突因此照样看得到（票 07）。
        self._apply_conflicts(snapshot)
        return snapshot

    def _apply_conflicts(self, snapshot):
        """把账上还没裁决的冲突折进这份快照（票 07）。

        只带成员在本次分析里露过面的那些：一个都不在场说明这次区间管不着它，
        留着等覆盖到它们的那次分析。裁决过（`resolved_at` 非空）的不再出现——
        行还在账上，页面不再拦人。
        """
        present = {identity(p) for p in snapshot['products']}
        names = {identity(p): (p.get('product_name') or p['offer_id']) for p in snapshot['products']}
        entries = []
        for conflict in self.store.conflicts():
            if conflict.resolved_at:
                continue
            if not (set(conflict.members) & present):
                continue
            entries.append({'digest': conflict.digest, 'kind': conflict.kind,
                            'members': list(conflict.members),
                            'note': conflict_note(conflict, names, self.config.machine),
                            'resolved': False})
        snapshot['conflicts'] = entries
        attach_conflicts(snapshot)

    def _resolve_conflicts(self, snapshot, members):
        """人在页面上动了这些商品：牵涉其中、还没裁决的冲突就此消解（票 07）。

        消解是本次分析内与保存时的事：账本身不动（冲突行只增），保存时把裁决时刻
        写进冲突行。裁决是人的动作，这里不判对错——重复点「确认」也算数。
        """
        resolved = False
        for entry in snapshot.get('conflicts', []):
            if not entry['resolved'] and set(entry['members']) & members:
                entry['resolved'] = True
                resolved = True
        if resolved:
            snapshot['dirty'] = True
        attach_conflicts(snapshot)

    def draft(self):
        """最近一次成功保存的草稿摘要，供「继续上次分析」入口显示。"""
        latest = self.store.latest()
        if latest is None:
            return {"available": False}
        return {"available": True, "id": latest['id'], "start": latest['start'],
                "end": latest['end'], "saved_at": latest['saved_at']}

    def save_draft(self, analysis_id):
        return self._save(analysis_id, require_confirmed=False)

    def save_and_view(self, analysis_id):
        return self._save(analysis_id, require_confirmed=True)

    def _save(self, analysis_id, require_confirmed):
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                raise ValueError('分析已不存在，请重新选择日期')
            if require_confirmed and pending_groups(snapshot):
                raise ValueError('还有待确认的同款组，请先完成确认')
            saved_at = utcnow()
            # 先落盘再改内存：写失败时本次修改仍留在页面，并保持未保存标记。
            # 草稿版本与决策账本同一次事务提交，页面此刻的确认/排除即全局最新；
            # 冲突的裁决（resolved_at）也跟这一次保存一起生效（票 07）。
            resolved = [entry['digest'] for entry in snapshot['conflicts'] if entry['resolved']]
            self.store.write(analysis_id, snapshot['start'], snapshot['end'], saved_at,
                             self._draft_payload(snapshot, saved_at),
                             merge_decisions(snapshot, self.store.ledger(), self.config.machine),
                             resolved=resolved)
            snapshot['dirty'] = False
            snapshot['saved_at'] = saved_at
            return copy.deepcopy(snapshot)

    def discard(self, analysis_id):
        """放弃未保存的修改：回到最近成功保存的版本；从未保存过则整体丢弃。"""
        with self._lock:
            stored = self.store.read(analysis_id)
            if stored is None:
                self._snapshots.pop(analysis_id, None)
                return {"reverted": False}
            self._snapshots[analysis_id] = self._restore(stored)
            return {"reverted": True}

    def _draft_payload(self, snapshot, saved_at):
        """落盘的是这次保存后的完整版本：不带未保存标记，时间是本次保存时间。

        图片不进草稿正文：只存内容哈希，读回时从持久图片资产取内容。冲突与组上的
        冲突说明也不进：那是账上现读的事实，读回时按那一刻重新挂（票 07）。
        """
        payload = copy.deepcopy(snapshot)
        payload['dirty'] = False
        payload['saved_at'] = saved_at
        payload['conflicts'] = []
        for group in payload['groups']:
            group['conflicts'] = []
        for product in payload['products']:
            product['image_data'] = None
        return payload

    def _rehydrate(self, payload):
        hashes = sorted({p['image_hash'] for p in payload['products'] if p.get('image_hash')})
        assets = {}
        if hashes:
            with self._read() as conn:
                for offset in range(0, len(hashes), 400):
                    chunk = hashes[offset:offset+400]
                    placeholders = ','.join('?' * len(chunk))
                    for row in conn.execute(f"SELECT content_hash,mime,content FROM product_image_assets"
                                            f" WHERE content_hash IN ({placeholders})", chunk):
                        assets[row['content_hash']] = row
        for product in payload['products']:
            product['image_data'] = None
            asset = assets.get(product.get('image_hash'))
            if asset and asset['content']:
                product['image_data'] = _data_url(asset['mime'], asset['content'])
            elif product.get('image_hash') and not product.get('image_error'):
                product['image_error'] = '历史图片资产不可用'
        # 票 17 之前保存的老草稿没有状态码：按文案回填。结论一律重算，与商品状态同源。
        for product in payload['products']:
            if not product.get('matching_state'):
                product['matching_state'] = state_of_status(product.get('matching_status'))
            product.setdefault('matching_source', '')   # 票 07 之前的草稿没有来源列
        payload['matching'] = matching_summary(payload['products'])
        return payload

    def confirm(self, analysis_id, group_id):
        return self.confirm_groups(analysis_id, [group_id])

    def confirm_groups(self, analysis_id, group_ids):
        return self._set_confirmation(analysis_id, group_ids, True)

    def withdraw(self, analysis_id, group_id):
        return self.withdraw_groups(analysis_id, [group_id])

    def withdraw_groups(self, analysis_id, group_ids):
        return self._set_confirmation(analysis_id, group_ids, False)

    def _set_confirmation(self, analysis_id, group_ids, confirmed):
        if not isinstance(group_ids, list) or not group_ids or not all(isinstance(g, str) for g in group_ids):
            raise ValueError('请选择有效同款组')
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                raise ValueError("分析已不存在，请重新选择日期")
            targets = set(group_ids)
            groups = [g for g in snapshot['groups'] if g['id'] in targets]
            if len(groups) != len(targets):
                raise ValueError("同款组不存在")
            for group in groups:
                if group['confirmed'] != confirmed:
                    group['confirmed'] = confirmed
                    # A withdrawn human decision must survive model retries.
                    group['adjusted'] = True
                    snapshot['dirty'] = True
            # 人动了这些组：牵涉其中的冲突就此算裁决过（票 07）。重复点「确认」这类
            # 状态不动的动作也算——它表达的正是「就按本机的决定办」。
            self._resolve_conflicts(
                snapshot, {identity(m) for group in groups for m in group['members']})
            return copy.deepcopy(snapshot)

    def source(self, offer_id):
        with self._read() as conn:
            row = conn.execute('SELECT product_url FROM products WHERE offer_id=?', (offer_id,)).fetchone()
            return {'url': row['product_url'] if row else None}

    def export_sources(self, analysis_id):
        """报告要用的来源地址：只取本次分析的商品在导出时仍有效的详情地址（规格 §8）。"""
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                stored = self.store.read(analysis_id)
                if stored is None:
                    raise ValueError("分析已不存在，请重新选择日期")
                snapshot = stored.payload
            offers = sorted({product['offer_id'] for product in snapshot['products']})
        urls = {}
        with self._read() as conn:
            for offset in range(0, len(offers), 400):
                chunk = offers[offset:offset+400]
                placeholders = ','.join('?' * len(chunk))
                for row in conn.execute("SELECT offer_id, product_url FROM products"
                                        f" WHERE offer_id IN ({placeholders}) AND product_url != ''", chunk):
                    urls[row['offer_id']] = row['product_url']
        return {'urls': urls}

    def export(self, analysis_id, html):
        """把页面生成的完整报告写进 output：用已保存快照，不重查库存、不重跑模型。"""
        if not isinstance(html, str) or not html.startswith('<!doctype html'):
            raise ValueError('导出内容无效，未写入任何文件')
        with self._lock:
            snapshot = self._snapshots.get(analysis_id)
            if snapshot is None:
                raise ValueError("分析已不存在，请重新选择日期")
            if snapshot.get('dirty'):
                raise ValueError('当前分析有未保存的修改，请先保存再导出')
            if pending_groups(snapshot):
                raise ValueError('还有待确认的同款组，请先完成确认')
            start, end = snapshot['start'], snapshot['end']
        # 写盘放在锁外：导出只读快照，重名冲突由写入端的序号兜底。
        path = write_report(self.config.output, report_name(start, end, utcnow()), html)
        return {'path': str(path)}

    def edit_group(self, analysis_id, action, group_id, member, target_id=None):
        """Commit an atomic edit only to the in-memory analysis draft."""
        from .grouping import edit_group
        with self._lock:
            if analysis_id not in self._snapshots:
                raise ValueError('分析已不存在')
            snapshot = copy.deepcopy(self._snapshots[analysis_id])
            moved = identity(member)
            edit_group(snapshot, action, group_id, member, target_id, self.matcher)
            # 人挪了这个商品：它落进/离开的组上的冲突也算裁决过（票 07）；
            # 说明跟着成员重挂，没被动的冲突照旧。
            touched = {moved} | {identity(m) for group in snapshot['groups']
                                 if group['id'] in (group_id, target_id)
                                 or any(identity(m2) == moved for m2 in group['members'])
                                 for m in group['members']}
            self._resolve_conflicts(snapshot, touched)
            rank_groups(snapshot)
            self._snapshots[analysis_id] = snapshot
            return copy.deepcopy(snapshot)


def _dates(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last-first).days + 1)]


def _data_url(mime: str, content: bytes) -> str:
    return 'data:' + mime + ';base64,' + base64.b64encode(content).decode('ascii')


def calculate_inventory(rows: list[dict], start: str, end: str) -> dict:
    """Infer representation segments from frozen observations, never SKU names."""
    products = {}
    for row in rows:
        products.setdefault((row['shop_key'], row['offer_id']), []).append(row)
    result = {}
    for key, observations in products.items():
        daily = {}
        for row in observations:
            daily.setdefault(row['date'], []).append(row)
        segments = []
        for day, observed in sorted(daily.items()):
            shape = {r['sku_id'] == 'default' for r in observed}
            if len(shape) != 1:
                raise ValueError('同日存在相反规格形态，无法分析')
            shape = shape.pop()
            if not segments or segments[-1]['shape'] != shape:
                segments.append({'start': day, 'shape': shape, 'rows': []})
            segments[-1]['rows'].extend(observed)
        skus = []
        dates = _dates(start, end)
        for index, segment in enumerate(segments):
            lower = max(start, segment['start']) if index else start
            upper = min(end, (date.fromisoformat(segments[index+1]['start']) - timedelta(days=1)).isoformat()) if index+1 < len(segments) else end
            if lower > upper:
                continue
            calculated = _calculate_segment(segment['rows'], lower, upper)[key]
            first_day = min(r['date'] for r in segment['rows'])
            for sku in calculated['skus']:
                first = min(r['date'] for r in segment['rows'] if r['sku_id'] == sku['sku_id'])
                # A newly appearing SKU cannot be backfilled into an established shape.
                active_start = max(lower, first) if first > first_day else lower
                points = {p['date']: p for p in sku['points'] if p['date'] >= active_start}
                sku['points'] = [points.get(day, {'date': day, 'stock': None, 'sales': 0, 'color': 'inactive', 'actual': False}) for day in dates]
                sku['segment'] = index + 1
                sku['sales'] = sum(p['sales'] for p in sku['points'])
                skus.append(sku)
        bucket = {'skus': skus, 'sales': sum(s['sales'] for s in skus),
                  'points': _aggregate_points([s['points'] for s in skus])}
        switches = {segment['start'] for segment in segments[1:]}
        for point in bucket['points']:
            point['segment_start'] = point['date'] in switches
        result[key] = bucket
    return result


def _calculate_segment(rows: list[dict], start: str, end: str) -> dict:
    """Build SKU-first inventory points: the per-SKU daily rows every layer aggregates from."""
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
        products.setdefault(product_key, {"skus": []})["skus"].append(
            {"sku_id": key[2], "name": observations[-1].get("sku_name") or key[2],
             "sales": sum(p["sales"] for p in points), "points": points})
    return products


def rank_groups(snapshot: dict) -> None:
    """组级排名视图：组序、成员次序、组总销量与组图逐日点只从这里产出（规格 §4、§6、§14）。

    先按同款分组把成员收成稳定次序（销量降序，并列按身份），再按组总销量降序给出组序
    （并列保持原顺序，稳定），最后把成员的逐日点聚到组层。冻结后快照入库前调用一次，
    之后的读取与导出都消费同一份结果，页面不再自己重算销量或排序。只依赖快照数据，
    可以安全地对同一份快照反复调用。
    """
    products = {identity(p): p for p in snapshot['products']}
    for group in snapshot['groups']:
        summarize_group(group, products)
        group['points'] = _aggregate_points([products[identity(m)]['points'] for m in group['members']])
    snapshot['ranking'] = [group['id'] for group in sorted(snapshot['groups'], key=lambda group: -group['sales'])]


def _aggregate_points(series: list[list[dict]]) -> list[dict]:
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


def product_versions(products: list[dict]) -> dict:
    """商品身份 → 当前证据版本：复用与信息变更标注都以此为准。"""
    return {identity(p): version(p) for p in products}


def recorded_versions(decisions: dict) -> dict:
    """账本记录过的商品版本：身份 → 版本集合，供信息变更标注使用。"""
    recorded = {}
    for relation in decisions['relations']:
        for member, member_version in relation['members']:
            recorded.setdefault(member, set()).add(member_version)
    for entry in decisions['standalone']:
        recorded.setdefault(entry[0], set()).add(entry[1])
    return recorded


# ---- 冲突落到分析页面上（票 07）：落回待确认、说明与两侧来源、裁决消解 ----


def pending_groups(snapshot: dict) -> list[dict]:
    """还没定下来的组：没确认的，或身上挂着未裁决冲突的（票 07）。

    冲突组按待确认算（页面上的页签、计数与保存门都用这一条）——「回到人面前」。
    """
    return [group for group in snapshot['groups']
            if not group['confirmed'] or group['conflicts']]


def attach_conflicts(snapshot: dict) -> None:
    """把没裁决的冲突挂到相关的组上：组上的 `conflicts` 就是页面要显示的那些说明。

    冲突牵涉的商品在哪几个组里，说明就挂在哪几个组上（缺席的成员没有组，说明里
    照旧点名）。组或冲突状态一变就重挂一次，说明因此跟着成员走。
    """
    by_member = {}
    for group in snapshot['groups']:
        for member in group['members']:
            by_member.setdefault(identity(member), []).append(group)
    attached = {}
    for entry in snapshot.get('conflicts', []):
        if entry['resolved']:
            continue
        for member in entry['members']:
            for group in by_member.get(member, ()):
                notes = attached.setdefault(group['id'], [])
                if entry['note'] not in notes:
                    notes.append(entry['note'])
    for group in snapshot['groups']:
        group['conflicts'] = attached.get(group['id'], [])


def _display_name(member: str, names: dict) -> str:
    """说明里对商品的称呼：在场的有名字用名字，缺席的退回商品号。"""
    return names.get(member) or offer_of(member)


def _side_phrase(content: dict, names: dict, machine: str) -> str:
    """冲突一方的说法：谁（来源机器）说了什么（内容），人话一句。"""
    who = '本机' if content.get('machine') == machine else (content.get('machine') or '未知机器')
    listed = [_display_name(member, names) for member, _ in content['members']]
    shown = '、'.join(listed[:3]) + (f' 等 {len(listed)} 个商品' if len(listed) > 3 else '')
    if content['kind'] == SIDE_EXCLUSION:
        return f'{who} 把 {shown} 排除在外（不成组）'
    if content['kind'] == SIDE_STANDALONE:
        return f'{who} 认为 {shown} 单独成组'
    if not content.get('confirmed'):
        return f'{who} 撤回过「{shown} 是一组」'
    return f'{who} 认为 {shown} 是一组'


def conflict_note(conflict, names: dict, machine: str) -> str:
    """一条冲突的人话（票 07）：外来一侧先说，本机一侧跟上，两侧来源都在句子里。"""
    sides = [_side_phrase(conflict.incoming, names, machine)]
    sides += [_side_phrase(content, names, machine) for content in conflict.local]
    return '冲突：' + '；'.join(sides) + '。请人工裁决。'


def reuse_decisions(snapshot: dict, decisions: dict) -> None:
    """把账本里已保存的人工分组按证据版本套到新分析的成员上（票 09）。

    确认过的关系套回已确认组；撤回过的关系保留成员与成组次序、但不自动确认，
    也不让模型重排；版本变化与未参与的商品留在待确认由人工处理。排除关系按
    商品身份生效，只有两端都在本次分析里才加载。

    组上带来源机器（票 07）：折进这个组的关系（或独立确认）里先遇到的那条说了算
    ——页面要说「这组决定由谁确认」。写回（`merge_decisions`）对关系沿用同一条
    「先折进来的那条留来源」的口径；这里只是把同一份来源提前读给页面看。
    """
    products = snapshot['products']
    current = product_versions(products)
    confirmed = set()
    pinned = set()
    roots = {'confirmed': {}, 'pending': {}}

    def find(kind, member):
        while roots[kind].setdefault(member, member) != member:
            member = roots[kind][member]
        return member

    anchors = []                     # (kind, 首名成员, 来源机器)：等根定下来再归组
    for relation in decisions['relations']:
        intact = [member for member, member_version in relation['members']
                  if current.get(member) == member_version]
        kind = 'confirmed' if relation['confirmed'] else 'pending'
        for member in intact[1:]:
            roots[kind][find(kind, intact[0])] = find(kind, member)
        (confirmed if relation['confirmed'] else pinned).update(intact)
        if intact:
            anchors.append((kind, intact[0], source_of(relation, '')))
    for entry in decisions['standalone']:
        if current.get(entry[0]) == entry[1]:
            confirmed.add(entry[0])
            anchors.append(('confirmed', entry[0], source_of(entry, '')))
    machines = {}
    for kind, anchor, machine_id in anchors:
        machines.setdefault((kind, find(kind, anchor)), machine_id)
    groups = []
    clusters = {}

    def open_group(is_confirmed, adjusted=False, machine=''):
        group = {'id': f'G{len(groups) + 1}', 'confirmed': is_confirmed, 'members': [],
                 'machine': machine}
        if adjusted:
            group['adjusted'] = True
        groups.append(group)
        return group

    def group_for(kind, member):
        key = (kind, find(kind, member))
        if key not in clusters:
            clusters[key] = open_group(kind == 'confirmed', adjusted=kind == 'pending',
                                       machine=machines.get(key, ''))
        return clusters[key]

    for p in products:
        member = identity(p)
        if member in confirmed:
            group = group_for('confirmed', member)
        elif member in pinned:
            group = group_for('pending', member)
        else:
            group = open_group(False)
        group['members'].append({'shop_key': p['shop_key'], 'offer_id': p['offer_id']})
    snapshot['groups'] = groups
    # 排除对只按身份生效（快照只带两端的身份，来源机器留在账本里）。
    snapshot['excluded'] = [list(pair[:2]) for pair in decisions['excluded']
                            if pair[0] in current and pair[1] in current]


def mark_information_changes(snapshot: dict, recorded: dict) -> None:
    """账本里记录过的版本与当前版本不一致的商品标为信息变更：旧确认不适用于新证据。"""
    for product in snapshot['products']:
        versions = recorded.get(identity(product))
        if versions and version(product) not in versions:
            product['origin'] = ORIGIN_CHANGED


def merge_decisions(snapshot: dict, decisions: dict, machine: str) -> dict:
    """保存这次人工整理后的完整账本。

    确认的分组按在场成员写回关系，并认领这次不显示的缺席成员——保存局部区间
    不能抹掉全局历史关系；撤回过的显示状态写入账本（成员保留、记为未确认）；
    证据版本已变或已被人工挪走的在场成员按最新决定移出关系。排除关系同理：
    只有两端都在本次分析里，这次保存才可能改写它们。独立确认同规（票 04）：
    成员不在本次分析里的行原样保留（版本与来源随行），在场而没被确认的照旧不写——
    撤回独立确认即删除它。

    来源机器（票 02）：行上原有的来源随行保留（收进来的决定往返一次不被改写）——关系折进
    本机的确认组时，来源随那条关系带走；独立确认与排除对按「身份（＋版本）」从原账本找回。
    只有本机这次新产生的行才记 `machine`；只影响来源列，不改变任何一条合并规则。
    """
    present = product_versions(snapshot['products'])
    group_of = {identity(m): g for g in snapshot['groups'] for m in g['members']}
    clusters = {}
    # 组的来源写回取自「折进这个组的关系」；多条关系折进同一个组时保留先遇到的那条
    # ——与 `reuse_decisions` 给页面读的来源是同一份口径（票 07：组上的「由谁确认」）。
    cluster_sources = {}
    for group in snapshot['groups']:
        if group['confirmed']:
            clusters[group['id']] = {identity(m): present[identity(m)] for m in group['members']}
    relations = []
    for relation in decisions['relations']:
        source = source_of(relation, machine)
        # 证据版本变了的在场成员不再适用于旧关系；缺席成员带旧版本原样保留。
        kept = [(member, present[member] if member in present else member_version)
                for member, member_version in relation['members']
                if member not in present or present[member] == member_version]
        confirmed = relation['confirmed']
        in_view = [member for member, _ in kept if member in present]
        if in_view:
            current_groups = {group_of[member]['id'] for member in in_view}
            if len(current_groups) == 1:
                (gid,) = current_groups
                if gid in clusters:
                    # 关系延续在这个确认组上：认领缺席成员后整条写回，来源留给组。
                    cluster_sources.setdefault(gid, source)
                    for member, member_version in kept:
                        if member not in present:
                            clusters[gid].setdefault(member, member_version)
                    continue
                if group_of[in_view[0]].get('adjusted'):
                    # 撤回（或人工整理过）的分组：成员保留，确认状态记为未确认。
                    confirmed = False
            else:
                # 成员散在多个组里：人为挪走或已落在确认组里的不回写，
                # 其余（模型暂时放置、没人动过）保持归属。
                kept = [(member, member_version) for member, member_version in kept
                        if member not in present
                        or (not group_of[member].get('adjusted')
                            and group_of[member]['id'] not in clusters)]
        if len(kept) > 1:
            relations.append({'members': [list(member) for member in kept], 'confirmed': confirmed,
                              'machine_id': source})
    for gid, members in clusters.items():
        if len(members) > 1:
            relations.append({'members': [[member, member_version] for member, member_version in sorted(members.items())],
                              'confirmed': True, 'machine_id': cluster_sources.get(gid, machine)})
    # 独立确认与排除对按「身份（＋版本）」从原账本找回来源：键没变的行不是本机新产生的。
    known_standalone = {tuple(entry[:2]): entry[2] for entry in decisions['standalone'] if len(entry) > 2}
    standalone = []
    for members in clusters.values():
        if len(members) == 1:
            (member, member_version), = members.items()
            standalone.append([member, member_version,
                               known_standalone.get((member, member_version), machine)])
    # 认领缺席的独立确认（票 04）：成员不在本次分析里的行原样保留——与关系同一口径，
    # 保存局部区间不能抹掉全局历史；在场而没被确认的照旧不写（撤回即删除）。
    standalone += [list(entry) for entry in decisions['standalone'] if entry[0] not in present]
    known_excluded = {tuple(pair[:2]): pair[2] for pair in decisions['excluded'] if len(pair) > 2}

    def with_source(pair):
        return [pair[0], pair[1], known_excluded.get(tuple(pair[:2]), machine)]

    excluded = [with_source(pair) for pair in decisions['excluded']
                if not (pair[0] in present and pair[1] in present)]
    excluded += [with_source(pair) for pair in snapshot.get('excluded', [])]
    relations.sort(key=lambda relation: relation['members'])
    return {'relations': relations, 'standalone': sorted(standalone),
            'excluded': sorted({tuple(pair) for pair in excluded})}
