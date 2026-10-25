"""交换台的一次运行与周报（spec §7/§8 + 判断集的两条动作，ADR-0039 决策 7）。

**一次运行**（`run_once`，手工触发）：刷新来源清单 → 检查 → 导出 → 发布判断集 → 拉取 →
汇总 → 接入来源 → 收取判断集 → 报告；`--only export|merge|publish|collect` 只跑其中一个动作（刷新来源
清单、检查与写报告照常，其余各个环节的「没做」如实写）。退出码：`0` 干净 / `1` 有需要人看一眼的
（已发布包的覆盖缺口、冲突、通道没拉成、图片缺、判断集本周记下的决定冲突、来源清单有来源减少/
改址、来源接入被阻断或用了上一版快照）/ `2` 本机没做成事（导出没成功、有包没导成、纯汇总机被要求只导出、
判断集没发出去或没收进来、来源清单没刷新、接收通道未就绪）。

- **刷新来源清单**（票 01、ADR-0052）：维护机（配了 `machine.gitee_owner` 的 m4）经 Gitee API
  生成 `judged-*` 来源快照、发布到 plan 库；只在实际交换任务启动时刷新——打开窗口、启动分析都不
  触发。没配 owner 的机器是「不是维护机」的可读说明，不是失败。来源减少或远端地址变化先保留上一版、
  等人确认（退出码 1）；查询/发布失败是硬失败（退出码 2）。
- **判断集这半**（票 05、spec §3）：发布与收取都挂在整趟里，两个动作互不依赖、也能各自
  单独跑（`--only publish` / `--only collect`）；**纯汇总机照常发布判断**——判断库与角色
  档位无关（spec §9），跳过的只是采集包的导出。两个库（判断缓存与人工决定账本）的路径从
  分析配置（`config/analysis.toml`，与 config.toml 同目录）读；读不出只在检查段与报告里
  如实写「这半没做」，不折成失败。判断集失败的档位与采集侧同规：**发布没成**同「导出没
  成功」、**judged 库拉不动**同「通道没拉成」（软）、**包读不出来/没导进去**同「有包没导成」。

- **检查**：配置与角色、交换区与图片仓库可达性、本机本周采到了哪些天、判断库与判断集的
  两个库路径。交换区根不在是致命的（本机没做成事），其余检查结果只记注记。
- **导出**：`export.export`（票据 08 的一致性快照 + 周包 + 图片只传新增）。纯汇总机跳过并
  明示（保留入口不藏）；导出失败不拦汇总这半，如实记进报告与退出码。
- **发布判断集**：`judgment_set.publish` 把本机判断集推到 `judged-<机器>`（同内容空操作）。
- **拉取**：对另外的每个交换库 pull（别的 `raw-*` 库 + `plan` 库——纯汇总机不跑采集
  准备串，plan 克隆靠这一趟保鲜）。拉不动记一笔（需要人看一眼），不拦汇总。
- **汇总**：交换区里的包逐个 `merge.import_package`（票据 09 的确定性合并、同哈希幂等跳过）。
  采集机继续跳过本机自己的包；纯汇总机把交换区里可见的全部 raw 包都纳入导入账，
  包括本机历史包。**不只本周**：账里还没有的历史包也一起收——冷启动一次重放全部
  （spec §11），别的机器迟到补发的历史包下次运行自动补上（各台最终收敛到同一份全量库）；
  账里已有的历史包安静跳过，不重进报告表。
- **接入来源**（票 02、ADR-0052）：收取那半先读最近一次有效来源清单（plan 库里的
  `judged-sources.json`），按它补齐缺的 `judged-<机器>` 本地克隆、只快进更新已有的、
  核对远端地址；只在实际收取任务里做——开窗口、启动分析都不触发。plan 拉不动时用本机
  上一版快照，只处理已有且匹配的来源（不新建克隆）。单来源失败按来源阻断、别家继续；
  被阻断的来源不进收取名单（连它的包都不读）。没有任何可用来源 = 「接收通道未就绪」
  （本机没做成事），不能表现成「没有新判断集」。
- **收取判断集**：`judgment_set.collect` 按接入好的名单（来源清单接入的 + 清单外本地已有的
  `judged-*` 库，spec §9）上各家的判断集、按内容摘要幂等导入；缺库没 clone、别人还没发布
  都只给可读提示，不中止其余步骤。票 03 起每份包在导入账里记全审计身份（来源提交、清单
  版本、格式与署名/证据摘要、行数），逐次流水与周报报的是同一份（任务 ID、来源名、远端、
  提交、包相对路径、摘要、计数）；整趟汇总状态 ok / partial_failure /
  receive_channel_not_ready。票 04 起读包先过**格式与包内自洽门禁**（不过的整包拒绝、
  一条不导，报告点名原因），过了的按**本机当前契约**（分析配置的模型署名，
  `judgment_paths` 从 [matching] 推）逐行记**门禁回执**：署名不符或证据版本没有着落的
  判断行照收留档、记 gated_out、不驱动当前分析（契约读不出记「未评估」）；冲突行的
  双方来源、**来源提交**与时刻进本机冲突账。周报里导入数、gated_out 数与当前采纳
  分列：采纳从本机库现读（判断按本机当前分析署名下、证据版本有着落的缓存行，本机还没
  在当前署名下跑过分析记「未评估」，不拿导入数冒充）。
- **报告**：`<交换区根>/报告/<年>-W<周>.md`——**一周一份、同周重跑重写同一份**，
  内容是**状态累积**（从计划表、本机库、交换区、幂等账与冲突账现读）而不是两次运行
  日志相加；开头「本次运行」行说明这一次具体做了什么。逐次流水在 `logs/exchange.log`。

报告固定六节（§8 + 票 05 的判断集一节）：本机发布 / 收进来的包 / 判断集 / 缺口与还没来的 /
冲突 / 下次该做什么；干净场景不出现「冲突」节（三份样例的形态）。两处口径写死在这里：

- **缺口** = 计划里该采到的（店铺 × 日期）在收到的包里找不到。本机份额对照本机库
  （「本机本周采到了哪些天」）；别机份额对照它的包（包还没来就记「还没来的包」，
  不把它的店全记成缺口）。只把已经过去（含今天）的天算作「该采到」；
  历史缺口补不了，如实记、不催办。
- **冲突** 从本机导入账（`import_conflicts`）读，**不入交换区**——「我见过这个冲突吗」
  取决于本机导入过哪些包，所以冲突一节是本机视角。说明里「计划外多采」由计划表
  推断（claim 的机器不是那家店本周的计划归属）。判断集的**决定冲突**另一节记账
  （从本机冲突账现读，只增不自动消解），与这里的采集冲突不是一回事。

报告是本机视角的；报告里的机器中文名（采集机 / 纯汇总机）与配置共用 `config.ROLE_GLOSS`。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import pathlib
import sqlite3
import tomllib
from collections.abc import Callable

from bestseller_monitor import (export, judgment_set, judged_sources, merge, plan_step,
                                rounds, sku_sharing, weekly_plan)
from bestseller_monitor.analysis_store import (CONFLICT_CONFIRM_VS_WITHDRAW,
                                               CONFLICT_DIFFERENT_GROUPS,
                                               CONFLICT_GROUP_VS_EXCLUSION, DraftStore)
from bestseller_monitor.config import ROLE_GLOSS, ROLE_MERGE_ONLY, is_merge_only
from bestseller_monitor.db import CST, Database, connect
from bestseller_monitor.git_channel import ChannelError, GitChannel
from bestseller_monitor.image_store import ImageStoreError
from bestseller_monitor.matching import ModelConfig, signature_of_model

log = logging.getLogger(__name__)

REPORT_DIR = "报告"                 # 周报落在交换区根下的这个目录（本机所有，不进交换集）
ONLY_EXPORT = "export"
ONLY_MERGE = "merge"
ONLY_PUBLISH = "publish"            # 只发布判断集（检查 + 发布 + 报告）
ONLY_COLLECT = "collect"            # 只收取判断集（检查 + 收取 + 报告）
# 各动作的短名（`--only` 的取值与报告里「只跑了…」那句共用一处）。
ONLY_GLOSS = {ONLY_EXPORT: "导出", ONLY_MERGE: "汇总",
              ONLY_PUBLISH: "发布判断集", ONLY_COLLECT: "收取判断集"}

EXIT_CLEAN = 0                      # 干净
EXIT_NEEDS_LOOK = 1                 # 有需要人看一眼的
EXIT_NOTHING_DONE = 2               # 本机没做成事（给以后挂计划任务留的判据）

# 退出码的人话（报告「结果」行与命令行收尾共用一处）：spec §7 的三个数各对应一句。
VERDICT = {EXIT_CLEAN: "干净", EXIT_NEEDS_LOOK: "有需要人看一眼的地方",
           EXIT_NOTHING_DONE: "本机没做成事"}

# 轮次终态 → 报告里那句人话（spec §8 的样例：「当天采集在详情预算耗尽后中止」）。
_REASON_GLOSS = {
    "IDENTITY_VERIFICATION_TIMEOUT": "当天采集因身份验证等待超时自动中止，已保存数据保留",
    "COMPLETED": "当天采集完成了，但这家店没有数据",
    "DETAIL_BUDGET_EXHAUSTED": "当天采集在详情预算耗尽后中止",
    "FAIL_RATE_EXCEEDED": "当天采集因失败率超限中止",
    "DAY_BOUNDARY": "当天采集撞上当日截止线中止",
    "DENY_EXCEEDED": "当天采集因被拒次数超限中止",
    "ABANDONED": "当天采集被中止（放弃）",
    "STARTUP_FAILED": "当天采集启动或初始化时失败，未进入采集",
    "LEGACY_UNKNOWN": "当天采集的终态不详（旧数据）；结束原因：未记录",
}


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """检查步骤的结局：`fatal` 非空 = 连起点都没有（本机没做成事，不再往下走）。"""

    fatal: str | None
    notes: tuple[str, ...]
    crawled: tuple[tuple[str, tuple[str, ...]], ...]   # 本机负责的店 → 覆盖到的天


@dataclasses.dataclass(frozen=True)
class PullNote:
    """一个 raw 库的拉取结果。"""

    repo: str                       # raw-m2
    failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclasses.dataclass(frozen=True)
class PackageRow:
    """报告「收进来的包」表的一行；`None` 的单元格渲染成「—」。"""

    repo: str                       # raw-m2
    week_short: str                 # W38
    rows: int | None
    inserted: int | None
    replaced: int | None
    conflicts: int | None
    images: tuple[int, int] | None  # (张数, 字节)
    result: str


@dataclasses.dataclass(frozen=True)
class GapLine:
    """一个缺口：计划里该采到的（店铺 × 日期）在收到的包里找不到。"""

    machine: str                    # 这份份额归哪台机器（本机 = cfg.machine_id）
    shop_key: str
    date: str
    reason: str


@dataclasses.dataclass(frozen=True)
class ConflictEntry:
    """冲突账里的一条（同一 (日期, 店铺) 上两个不同机器标识的 claim）。"""

    shop_key: str
    date: str
    winner_machine: str
    winner_at: str                  # 取胜方那次采集的时刻（ISO）
    loser_machine: str
    loser_at: str
    replaced: int                   # 败方被覆盖的行数
    kept: int                       # 败方保留的行数
    packages: tuple[str, ...]       # 在哪几个包的导入里发现的（raw-<机器>）


@dataclasses.dataclass(frozen=True)
class JudgmentPaths:
    """判断集这半要的两个库（都在本机，路径从分析配置读）+ 本机当前分析契约的署名。

    `signature` 是门禁与采纳的当前口径（票 04）：从 [matching] 的模型三件推
    （`matching.signature_of_model`，与分析消费认的署名同一口径）；读不出、怪值时是
    空串——门禁与采纳如实记「未评估」，不把读不出的配置当成某个署名。
    """

    cache: pathlib.Path
    store: pathlib.Path
    signature: str = ""


# 分析配置里两个键的默认值（与 `AnalysisConfig.from_file` 同口径）：相对路径以配置文件
# 所在目录为基准；键没写就用这两个默认文件名（那两个值必须与分析那半一致——不然交换台
# 与分析的缓存会各指一处）。
ANALYSIS_CACHE_DEFAULT = "matching.sqlite"
ANALYSIS_STORE_DEFAULT = "analysis-drafts.sqlite"


def judgment_paths(cfg) -> tuple[JudgmentPaths | None, str | None]:
    """从分析配置（`config/analysis.toml`）读判断集的两个库路径与本机当前契约署名；读不出给一句可读说明。

    只读 `[matching]` 与 `[analysis]` 两节里的 cache／store、不整份加载分析配置——模型段、
    视觉段或库里别的东西写错都不该拦住交换台（与 `config.machine_id_of` 那条「只读一个键」
    的口径同规）。契约署名（票 04）从 [matching] 的模型三件推：模型段写坏了只让署名留空
    （门禁与采纳记「未评估」），同样不拦住交换台——那是分析那半启动时才报的错。
    路径规则跟 `AnalysisConfig.from_file` 一致：相对路径以配置文件所在目录为基准、缺省名
    同上（那两个默认值写在这里，是因为交换台不整份加载分析配置）。

    读不出（文件不在、读不动、没有 `[matching]` 一节）回 `(None, 说明)`：判断集这半整趟
    没做，检查段的注记与报告第三节都用这一句说明——不折成失败（那是上机阶段的事）。
    """
    path = pathlib.Path(cfg.analysis_config)
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError:
        return None, (f"本机还没有分析配置（{path}）：判断缓存与账本的位置从它读，"
                      "判断集这半没做——配好之后重跑即可。")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"分析配置读不出来（{path}）：{exc}——判断集这半没做。"
    matching = raw.get("matching")
    if not isinstance(matching, dict):
        return None, (f"分析配置里没有 [matching] 一节（{path}）：判断缓存在哪读不出来，"
                      "判断集这半没做。")
    analysis = raw.get("analysis")
    store = analysis.get("store") if isinstance(analysis, dict) else None
    base = path.resolve().parent

    def resolve(value: str) -> pathlib.Path:
        resolved = pathlib.Path(str(value))
        return resolved if resolved.is_absolute() else (base / resolved)

    return JudgmentPaths(cache=resolve(matching.get("cache") or ANALYSIS_CACHE_DEFAULT),
                         store=resolve(store or ANALYSIS_STORE_DEFAULT),
                         signature=_contract_signature(matching)), None


def _contract_signature(matching: dict) -> str:
    """本机当前分析契约的署名（票 04）：从 [matching] 的模型三件推，与分析的消费同源。

    缺省值与 `AnalysisConfig.from_file` 同规（`ModelConfig(**matching.get('model', {}))`，
    没有 [matching.vision] 就没有视觉模型）——分析程序会用的那份配置，这里推出来的就是
    同一个署名。读不出、怪值时给空串：门禁与采纳如实记「未评估」，不去猜一个署名。
    """
    try:
        model = ModelConfig(**dict(matching.get("model") or {}))
        vision = ModelConfig(**dict(matching["vision"])) if "vision" in matching else None
        return signature_of_model(model.endpoint, model.model,
                                  vision.model if vision else None)
    except (AttributeError, TypeError, ValueError):
        return ""


@dataclasses.dataclass(frozen=True)
class RunOutcome:
    """一次运行的完整账：报告从它渲染，退出码由它算，命令行与窗口读它。"""

    week: str
    machine_id: str
    role: str
    ran_at: dt.datetime
    check: CheckResult
    only: str | None = None                 # 只跑哪个动作（None = 整趟）
    export: export.ExportResult | None = None
    export_note: str | None = None          # 没跑导出时的说明（纯汇总机跳过 / --only 别的动作）
    pulls: tuple[PullNote, ...] = ()
    imports: tuple[merge.ImportResult, ...] = ()
    merge_ran: bool = False
    packages: tuple[PackageRow, ...] = ()
    gaps: tuple[GapLine, ...] = ()
    partial_gaps: tuple[GapLine, ...] = ()
    conflicts: tuple[ConflictEntry, ...] = ()
    missing_machines: tuple[str, ...] = ()
    todo: tuple[str, ...] = ()
    summary: str = ""
    failures: tuple[str, ...] = ()          # 硬失败（退出码 2 的判据）
    exit_code: int = EXIT_CLEAN
    report_path: pathlib.Path | None = None
    self_shops: tuple[str, ...] = ()
    plan_known: bool = False
    plan_of_shop: dict[str, str] = dataclasses.field(default_factory=dict)
    # 判断集这半（票 05）：两个库的路径（None = 本机没有分析配置，这半没做）+ 这一趟的
    # 发布与收取结果 + 本机冲突账（决定冲突；与上面的采集冲突不是一回事）。
    judgments: JudgmentPaths | None = None
    judgment_note: str | None = None
    judgment_publish: judgment_set.PublishResult | None = None
    judgment_collect: judgment_set.CollectOutcome | None = None
    judgment_conflicts: tuple = ()
    # 票 03：这次运行的 ID（收取日志与报告共用）、收取那半的汇总状态与没成名单
    # （ok / partial_failure / receive_channel_not_ready；state 为 None = 这趟没收取）、
    # 以及各来源的当前采纳读数（从本机库现读：导入数不是采用数）。
    task_id: str = ""
    judgment_collect_state: str | None = None
    judgment_collect_failed: tuple = ()
    judgment_adoption: tuple = ()
    # 判断来源清单（票 01、ADR-0052）：维护机（m4）在任务启动时刷新并发布的结果。None =
    # 这一趟没走刷新（例如 fatal 提前返回）。非维护机是「没配 owner」的可读说明，不是失败。
    source_refresh: judged_sources.RefreshResult | None = None
    # 来源接入（票 02）：收取那半按最近一次有效快照补齐/核对本地克隆的结果。None = 这一趟
    # 没有收取（没配分析配置、或 --only 别的动作）。被阻断的来源不进收取名单。
    source_prep: judged_sources.PreparedSources | None = None
    # SKU 判断集这半（票 08）：同一两个源库、同一 judged-<机器> 库，包走新路径
    # （data/sku-judgments.db.gz）；None = 这趟没跑（与判断集同一条件）。
    sku_publish: sku_sharing.SkuPublishResult | None = None
    sku_collect: sku_sharing.SkuCollectOutcome | None = None


def _emitter(emit: Callable[[str], None] | None) -> Callable[[str], None]:
    """进度行同时进日志与调用方的回调（小窗口用回调；命令行直接 print）。"""

    def say(message: str) -> None:
        log.info("%s", message)
        if emit is not None:
            emit(message)

    return say


def _task_id(machine_id: str, now: dt.datetime) -> str:
    """这次运行的 ID（票 03）：机器编号 + 运行时刻。收取的逐次流水与周报共用一处生成。"""
    return f"{machine_id}-{now:%Y%m%d-%H%M%S}"


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else "（没有说明）"


def _mb(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB"


def week_days(week: str, as_of: dt.date) -> tuple[dt.date, ...]:
    """这一周「该采到」的天：整周里已经过去（含 as_of 当天）的那些；未来周一个都没有。

    周报的缺口口径与三机验收核对（`tools/acceptance_check.py`）共用这一处推导。
    """
    monday, sunday = weekly_plan.week_window(week)
    last = min(sunday, as_of)
    return tuple(day for day in (monday + dt.timedelta(days=i) for i in range(7))
                 if day <= last)


def check_environment(cfg, *, db: Database, week: str, now: dt.datetime,
                      store=None) -> CheckResult:
    """检查（spec §7）：配置与角色、交换区与图片仓库可达性、本机本周采到了哪些天、
    判断集这半的两个库与判断库。

    只读，不改任何东西；`fatal` 非空 = 交换区根不在，连起点都没有（不再往下走）。
    判断库缺失、还没 clone 都只记注记——发布那一步会自己点名，其余步骤照常。
    """
    machine_id = str(cfg.machine_id)
    root = pathlib.Path(cfg.exchange_root)
    if not root.is_dir():
        return CheckResult(fatal=(
            f"交换区根目录不存在：{root}\n"
            "照上机清单第 4/9 步建好运行根与交换库的克隆（本机那条 raw 库 + plan；"
            "名册里别家的等它们入册再 clone），再跑交换台。"), notes=(), crawled=())

    notes: list[str] = []
    repos = {path.name for path in root.glob("raw-*") if path.is_dir()}
    if not is_merge_only(cfg) and f"raw-{machine_id}" not in repos:
        notes.append(f"本机的 raw 库还没 clone 到 {root / f'raw-{machine_id}'}："
                     "导出这半会按「没做成事」报（上机清单第 9 步）。")
    if not [repo for repo in repos if repo != f"raw-{machine_id}"]:
        notes.append("交换区里没有别的 raw 库克隆：拉取与汇总这半没有可收的包。")

    _judgment_check_notes(cfg, notes, root=root, machine_id=machine_id)

    store = store if store is not None else export.default_store(cfg)
    if store is None:
        notes.append("配置缺 machine.cos_bucket：图片通道没有桶可用（导出与汇总都会"
                     "按「图片这半没做」如实记）。")
    else:
        try:
            store.existing_keys()
        except ImageStoreError as exc:
            notes.append(f"图片库连不上：{exc}")

    return CheckResult(fatal=None, notes=tuple(notes),
                       crawled=_self_crawled_days(db, week, machine_id, now))


def _judgment_check_notes(cfg, notes: list[str], *, root: pathlib.Path,
                          machine_id: str) -> None:
    """判断集这半的就位情况（票 05）：分析配置在不在、判断库建了没有。

    三条各自独立：没配分析配置就整趟没做；本机那本 missing 或交换区里一本别家的都没有，
    各记一条（名单口径与收取那半共用 `judgment_set.judged_repos`）。都只记注记，
    不中止其余步骤。
    """
    paths, note = judgment_paths(cfg)
    if paths is None:
        notes.append(note)
    own = f"{judgment_set.REPO_PREFIX}{machine_id}"
    if not (root / own).is_dir():
        notes.append(f"本机的判断库 {own} 还没 clone 到 {root / own}：发布判断集那一步会点名它"
                     "（上机清单第 14 步）。")
    if not judgment_set.judged_repos(root, exclude=machine_id):
        notes.append("交换区里还没有别的 judged 库：来源清单里有来源的话，收取时会自动接入"
                     "补齐克隆（没有清单就只收本地已有的）。")
    if _source_owner(cfg) and not (root / plan_step.PLAN_REPO_DIR).is_dir():
        notes.append(f"本机是来源维护机（配了 machine.gitee_owner），但 plan 库还没 clone 到 "
                     f"{root / plan_step.PLAN_REPO_DIR}：刷新判断来源清单那一步会点名它"
                     "（上机清单第 9 / 11 步）。")


def _self_crawled_days(db: Database, week: str, machine_id: str,
                       now: dt.datetime) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """本机负责的店里，本周各覆盖到了哪些天（从本机库读）。没有落库计划就是空。"""
    plan = plan_step.stored_plan(db, week)
    if plan is None:
        return ()
    days = week_days(week, now.date())
    if not days:
        return ()
    span = (days[0].isoformat(), days[-1].isoformat())
    out = []
    for shop_key in plan.machine_keys(machine_id):
        covered = {str(row[0]) for row in db.conn.execute(
            "SELECT DISTINCT date FROM inventory WHERE shop_key=? AND date BETWEEN ? AND ?",
            (shop_key, *span))}
        out.append((shop_key, tuple(day.isoformat() for day in days
                                    if day.isoformat() in covered)))
    return tuple(out)


def _source_owner(cfg) -> str:
    """本机配置的可信 owner（空 = 本机不是来源维护机）：只读一个键，两处消费共用一处口径。"""
    return str(cfg.gitee_owner or "").strip()


def _refresh_sources(cfg, *, machine_id: str, now: dt.datetime, provider, confirm_shrink: bool,
                     say) -> judged_sources.RefreshResult:
    """维护机在任务启动时经 Gitee API 刷新并发布判断来源清单（票 01、ADR-0052）。

    只由 `run_once` 调——打开交换窗口（`Api.state`）与启动分析都不走它，因此天然不触发。
    没配 `machine.gitee_owner` 的机器是「不是维护机」的可读说明（不是失败）；配了 owner 但
    `provider` 为空时现搭一个真实 Gitee 客户端（token 只从环境变量读）。`confirm_shrink` 是
    维护人对「来源减少 / 远端地址变化」的显式确认（`--confirm-source-shrink`）。
    """
    owner = _source_owner(cfg)
    if not owner:
        say("刷新判断来源清单：跳过（本机不是来源维护机）")
        return judged_sources.refresh(cfg.exchange_root, owner="", generator=machine_id,
                                      provider=provider, now=now)
    say("刷新判断来源清单…")
    if provider is None:
        provider = judged_sources.provider_from_config(owner)
    result = judged_sources.refresh(cfg.exchange_root, owner=owner, generator=machine_id,
                                    provider=provider, now=now, confirm_shrink=confirm_shrink)
    say("来源清单：" + _source_text(result))
    return result


def _pull_others(cfg, machine_id: str, say) -> tuple[PullNote, ...]:
    """对另外的每个交换库 pull：别的 raw 库 + `plan` 库（spec §7 原文「对另外三个库 pull」——
    成稿于四库时代；五库时代即名册里别家的每条 raw 库 + `plan`）。

    plan 库跟着拉是纯汇总机那条线的要紧事：它不跑采集准备串，不拉就永远停在 clone
    时那份；采集机上重复拉一次无害（同内容是个空动作）。没 clone 的目录跳过——那是
    上机清单第 9 步的事，检查步骤会点名。
    """
    root = pathlib.Path(cfg.exchange_root)
    repos = {path for path in root.glob("raw-*") if path.is_dir()}
    repos.discard(root / f"raw-{machine_id}")
    plan_repo = root / plan_step.PLAN_REPO_DIR
    if plan_repo.is_dir():
        repos.add(plan_repo)
    notes: list[PullNote] = []
    for path in sorted(repos, key=lambda p: p.name):
        try:
            GitChannel(path).pull()
        except ChannelError as exc:
            notes.append(PullNote(repo=path.name, failure=str(exc)))
            say(f"拉取 {path.name}：没成（{_first_line(str(exc))}）")
        else:
            notes.append(PullNote(repo=path.name))
            say(f"拉取 {path.name}：完成")
    return tuple(notes)


def _merge_packages(cfg, db: Database, *, week: str, machine_id: str, store,
                    say) -> dict[pathlib.Path, merge.ImportResult]:
    """把交换区里的 raw 包逐个收进本机库。

    采集机跳过本机自己的包；纯汇总机重放交换区里可见的全部包，包括本机历史包。
    不只收本周：账里还没有的历史包也一起收——冷启动一次重放全部（spec §11），
    别的机器迟到补发的历史包下次运行自动补上（各台最终收敛到同一份全量库）。
    账里已有且没有缺图的历史包安静跳过：不占这次运行的账、不重进报告表（状态累积以本周为焦点）。
    同哈希的重复导入本来就安全（幂等账）；仍有缺图的历史包也照旧走一次来补图。这里只是让「本周的」照旧走一次好让报告
    把它标成「已导入过 → 跳过」。
    """
    root = pathlib.Path(cfg.exchange_root)
    known = {str(row["package_sha256"]): int(row["images_missing"])
             for row in db.conn.execute(
                 "SELECT package_sha256, images_missing FROM import_packages")}
    imported: dict[pathlib.Path, merge.ImportResult] = {}
    for ref in export.all_packages(root):
        if ref.machine == machine_id and cfg.role != ROLE_MERGE_ONLY:
            continue
        if ref.week != week:
            try:
                sha = merge.package_sha256(ref.path)
            except OSError:
                sha = None                       # 读不动：交给导入那一步如实报
            if sha is not None and sha in known and known[sha] == 0:
                say(f"跳过 {ref.path.name}：{ref.week or '历史'} 的包账里已有")
                continue
        result = merge.import_package(db.conn, ref.path, machine_id=machine_id, store=store)
        imported[ref.path] = result
        if result.failed:
            say(f"导入 {ref.path.name}：没导成（{_first_line(result.failure)}）")
        elif result.skipped:
            if result.images_retried:
                say(f"重跑 {ref.path.name}：补拉图片 {result.images_pulled:,} 张（仍缺 "
                    f"{result.images_missing:,} 张）")
            else:
                say(f"跳过 {ref.path.name}：已导入过（同哈希）")
        else:
            say(f"导入 {ref.path.name}：新增 {result.rows_inserted:,} 行、"
                f"覆盖 {result.rows_replaced:,} 行、冲突 {result.conflicts} 处、"
                f"拉图 {result.images_pulled} 张")
    if not imported:
        say("交换区里没有别的机器发的包可收。")
    return imported


def _ledger_row(conn: sqlite3.Connection, path: pathlib.Path):
    """幂等账里这个包的那一行（按包内容哈希找）；哈希都算不出来时 None。"""
    try:
        sha = merge.package_sha256(path)
    except OSError:
        return None
    return conn.execute("SELECT * FROM import_packages WHERE package_sha256=?",
                        (sha,)).fetchone()


def run_once(cfg, *, only: str | None = None, week: str | None = None,
             now: dt.datetime | None = None, store=None,
             emit: Callable[[str], None] | None = None,
             source_provider=None, confirm_shrink: bool = False) -> RunOutcome:
    """一次运行：刷新来源清单 → 检查 → 导出 → 发布判断集 → 拉取 → 汇总 → 接入来源 → 收取判断集 → 报告。

    `only` 取 `ONLY_EXPORT` / `ONLY_MERGE` / `ONLY_PUBLISH` / `ONLY_COLLECT` 之一，
    只跑那个动作（None = 整趟）；`week` 默认本周（补历史指定周窗口，导出与报告都按它走）；
    `store` 是图片库（None = 按配置现搭）；`emit` 收进度行（小窗口用）。
    `source_provider` 是来源查询的接缝（票 01）：None = 现搭真实 Gitee 客户端；测试注入替身。
    `confirm_shrink` 是维护人对来源减少 / 远端地址变化的显式确认（`--confirm-source-shrink`）。
    退出码见模块 docstring。
    """
    if getattr(cfg, "local_trial", False):
        raise ValueError("本机试采上下文不得导出或交换；正式判断集请使用正式配置。")
    now = now or dt.datetime.now(CST)
    week = week or export.current_week()
    machine_id = str(cfg.machine_id)
    role = str(cfg.role)
    # 环境检查会验证默认图片库；后续汇总必须复用同一个实例，才能实际拉图.
    store = store if store is not None else export.default_store(cfg)
    say = _emitter(emit)
    task_id = _task_id(machine_id, now)      # 票 03：这次运行的 ID（收取日志与报告共用）

    conn = connect(cfg.db_file)
    try:
        db = Database(conn)
        check = check_environment(cfg, db=db, week=week, now=now, store=store)
        if check.fatal is not None:
            say(f"检查没通过：{check.fatal}")
            return RunOutcome(week=week, machine_id=machine_id, role=role, ran_at=now,
                              check=check, failures=(check.fatal,),
                              exit_code=EXIT_NOTHING_DONE)
        say(f"检查：本机 {machine_id}（{ROLE_GLOSS[role]}）")
        for note in check.notes:
            say(f"检查注记：{note}")
        if check.crawled:
            say("本机本周采到：" + "；".join(
                f"{shop_key} {len(days)} 天" for shop_key, days in check.crawled))

        # ---- 判断来源清单（票 01、ADR-0052）：维护机在任务启动时刷新并发布 ----
        # 只在这里做：打开交换窗口（`Api.state`）与启动分析都不走 run_once，因此天然不触发。
        source_refresh = _refresh_sources(cfg, machine_id=machine_id, now=now,
                                          provider=source_provider,
                                          confirm_shrink=confirm_shrink, say=say)

        # ---- 导出这半：导出（纯汇总机跳过）· 发布判断集（与角色档位无关，spec §9）----
        export_result: export.ExportResult | None = None
        export_note: str | None = None
        if only is not None and only != ONLY_EXPORT:
            export_note = f"本次只跑了{ONLY_GLOSS[only]}（--only {only}），没做导出"
        elif is_merge_only(cfg):
            export_note = "本机是纯汇总机，跳过"
            say(f"导出：{export_note}（入口保留着，不藏）。")
        else:
            say(f"导出 {week} 的包…")
            export_result = export.export(cfg, week=week, store=store)
            say(_export_line(export_result))

        judgments, judgment_note = judgment_paths(cfg)   # 读不出时检查段已给了同一句说明
        judgment_publish: judgment_set.PublishResult | None = None
        sku_publish: sku_sharing.SkuPublishResult | None = None
        if judgments is not None and only in (None, ONLY_PUBLISH):
            say("发布本机的判断集…")
            judgment_publish = judgment_set.publish(cfg.exchange_root, machine_id,
                                                    cache=judgments.cache,
                                                    store=judgments.store, now=now)
            say(_judgment_publish_line(judgment_publish))
            say("发布本机的 SKU 判断集…")
            sku_publish = sku_sharing.publish(cfg.exchange_root, machine_id,
                                              cache=judgments.cache,
                                              store=judgments.store, now=now)
            say(_sku_publish_line(sku_publish))

        # ---- 拉取 + 汇总这半 ----
        pulls: tuple[PullNote, ...] = ()
        imported: dict[pathlib.Path, merge.ImportResult] = {}
        merge_ran = only in (None, ONLY_MERGE)
        if merge_ran:
            pulls = _pull_others(cfg, machine_id, say)
            imported = _merge_packages(cfg, db, week=week, machine_id=machine_id,
                                       store=store, say=say)

        # ---- 收取判断集（票 02：先按来源清单接入本地克隆，再按接入名单收）----
        judgment_collect: judgment_set.CollectOutcome | None = None
        sku_collect: sku_sharing.SkuCollectOutcome | None = None
        source_prep: judged_sources.PreparedSources | None = None
        if judgments is not None and only in (None, ONLY_COLLECT):
            say("接入判断来源（读来源清单、补齐本地克隆）…")
            source_prep = judged_sources.prepare_sources(cfg.exchange_root, machine_id=machine_id)
            for line in _source_prep_lines(source_prep):
                say(line)
            # 票 03：这趟收取的任务 ID 与读到的清单版本——逐次流水与报告说的是同一份。
            say(f"收取别的机器的判断集（任务 "
                f"{_collect_task_text(task_id, source_prep.snapshot_version)}）…")
            judgment_collect = judgment_set.collect(
                cfg.exchange_root, machine_id, cache=judgments.cache,
                store=judgments.store,
                now=now.astimezone(dt.timezone.utc),   # 账上的收到时刻按 UTC 记（票 03 的口径）
                sources=source_prep.collect_sources(),
                snapshot_version=source_prep.snapshot_version,
                contract_signature=judgments.signature)   # 门禁的当前契约口径（票 04）
            for text in _judgment_collect_texts(judgment_collect):
                say(f"收取：{text}")
            state, failed_sources = _receive_state(source_prep, judgment_collect)
            say("收取结果：" + _receive_state_text(state, failed_sources))
            say("收取别的机器的 SKU 判断集…")
            sku_collect = sku_sharing.collect(
                cfg.exchange_root, machine_id, cache=judgments.cache,
                store=judgments.store,
                now=now.astimezone(dt.timezone.utc))
            say(_sku_collect_line(sku_collect))

        outcome = _assemble(cfg, db, week=week, now=now, machine_id=machine_id, role=role,
                            check=check, export_result=export_result, export_note=export_note,
                            pulls=pulls, imported=imported, merge_ran=merge_ran, only=only,
                            judgments=judgments, judgment_note=judgment_note,
                            judgment_publish=judgment_publish,
                            judgment_collect=judgment_collect,
                            source_refresh=source_refresh, source_prep=source_prep,
                            task_id=task_id,
                            sku_publish=sku_publish, sku_collect=sku_collect)
        report_path = pathlib.Path(cfg.exchange_root) / REPORT_DIR / f"{week}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render(outcome), encoding="utf-8")
        say(f"报告已写到 {report_path}")
        say(f"本次运行：{outcome.summary}（退出码 {outcome.exit_code}）")
        for failure in outcome.failures:
            say(f"没做成事：{failure}")
        return dataclasses.replace(outcome, report_path=report_path)
    finally:
        conn.close()


def _export_line(result: export.ExportResult) -> str:
    name = pathlib.Path(result.package_rel).name
    if result.failed:
        return f"导出没成功：{_first_line(result.failure)}"
    if result.unchanged:
        return f"导出：{name} 与已发布那份同内容（无新提交）"
    return f"导出已发布：{name} → raw-{result.machine_id} @ {result.commit}"


def _judgment_publish_line(result: judgment_set.PublishResult) -> str:
    """发布这一趟的进度行（正文与报告第三节共用一处措辞，这里只换前缀）。"""
    return "发布判断集：" + _judgment_publish_text(result)


def _receive_state(source_prep, collect) -> tuple[str, tuple[str, ...]]:
    """收取那半的汇总状态与没成的名单（票 03、spec §2）。

    receive_channel_not_ready（没有任何可用来源，票 02 沿用）> partial_failure（有东西
    没成：整趟没收成、接入被阻断/跳过，或收取拉不动/没导成）> ok。整趟绝不能显示全部
    成功——这正是 2026-09-24 那类误导的落点。
    """
    if source_prep is not None and not source_prep.ready:
        return judgment_set.COLLECT_STATE_NOT_READY, ()
    failed: list[str] = []
    if collect is not None and collect.failure is not None:
        failed.append(f"整趟没做成（{_first_line(collect.failure)}）")
    if source_prep is not None:
        failed += [entry.repo for entry in source_prep.entries if entry.blocked or entry.skipped]
    if collect is not None:
        failed += [f"{judgment_set.REPO_PREFIX}{result.machine}"
                   for result in collect.results if result.failed]
    if failed:
        return judgment_set.COLLECT_STATE_PARTIAL_FAILURE, tuple(failed)
    return judgment_set.COLLECT_STATE_OK, ()


def _receive_state_text(state: str, failed: tuple[str, ...]) -> str:
    """汇总状态的人话（报告「- 收取结果：」与进度行共用）：partial_failure 点名没成的。"""
    if state == judgment_set.COLLECT_STATE_PARTIAL_FAILURE:
        return "partial_failure —— 没成：" + "、".join(failed)
    return state


def _collect_task_text(task_id: str, snapshot_version: str) -> str:
    """收取任务的正文（报告「- 收取任务：」与逐次流水共用一处）：任务 ID + 清单版本。

    清单版本读不到（计划库没有克隆、或还没有任何提交）时如实写「无」，不装成读到过。
    """
    return (f"{task_id}；来源清单 "
            + (snapshot_version or "无（计划库没有克隆或还没有提交）"))


def _sku_publish_line(result: sku_sharing.SkuPublishResult) -> str:
    """SKU 发布这一趟的进度行（正文与报告第三节共用一处措辞）。"""
    return "发布 SKU 判断集：" + _sku_publish_text(result)


def _sku_collect_line(outcome: sku_sharing.SkuCollectOutcome) -> str:
    """SKU 收取这一趟的进度行：一家一句，没得收也说明一句。"""
    return "收取 SKU 判断集：" + "；".join(_sku_collect_texts(outcome))


def _sku_publish_text(result: sku_sharing.SkuPublishResult) -> str:
    """SKU 发布结果的正文（报告「- 发布 SKU：」与命令行共用这一处措辞）。"""
    decided = sum(result.rows.get(name, 0) for name in sku_sharing.LEDGER_TABLES)
    counts = (f"判断 {result.rows.get('sku_judgments', 0)} 条 · "
              f"推荐 {result.rows.get('sku_recommendations', 0)} 条 · "
              f"人工决定 {decided} 条")
    if result.failed:
        return f"{counts} —— 没发出去（{_first_line(result.failure)}）"
    if result.note is not None:
        return result.note
    if result.unchanged:
        return f"{counts} —— 与已发布那份同内容（无新提交）"
    return f"{counts} → {sku_sharing.REPO_PREFIX}{result.machine_id} @ {result.commit}"


def _sku_collect_texts(collect: sku_sharing.SkuCollectOutcome) -> list[str]:
    """SKU 收取结果每个来源一句（没有可收的、整趟没做成也是一句）：报告与命令行共用。"""
    if collect.failure is not None:
        return [f"没做成（{_first_line(collect.failure)}）"]
    if not collect.results:
        note = f"（{collect.notes[0]}）" if collect.notes else ""
        return [f"没有 SKU 判断集可收{note}"]
    texts = []
    for result in collect.results:
        name = f"{sku_sharing.REPO_PREFIX}{result.machine}"
        if result.failed:
            what = "拉不动" if result.unreachable else "没收成"
            texts.append(f"{name} 这本{what}（{_first_line(result.failure)}）")
        elif result.note is not None:
            texts.append(f"{name} 没有 SKU 判断集可收 —— {result.note}")
        elif result.skipped:
            texts.append(f"{name} —— 已收过（无变化）")
        else:
            conflict_note = f"，冲突 {len(result.conflicts)} 条" if result.conflicts else ""
            texts.append(f"{name} —— 新增 {sum(result.added.values()):,} 行"
                         f"（并进决定 {result.adopted_total} 条）{conflict_note}")
    return texts


def _assemble(cfg, db: Database, *, week, now, machine_id, role, check, export_result,
              export_note, pulls, imported, merge_ran, only, judgments=None,
              judgment_note=None, judgment_publish=None, judgment_collect=None,
              source_refresh=None, source_prep=None, task_id="",
              sku_publish=None, sku_collect=None) -> RunOutcome:
    """把这次运行与库里的现成事实折成报告模型（状态累积，不是运行日志相加）。"""
    root = pathlib.Path(cfg.exchange_root)
    plan = plan_step.stored_plan(db, week)
    days = week_days(week, now.date())
    week_refs = export.week_packages(root, week)
    plan_of_shop = ({row.shop_key: row.machine_id for row in plan.rows}
                    if plan is not None else {})

    # 「收进来的包」= 这次真收的（含冷启动的历史包）+ 本周的全部可见包（含还没碰过的）。
    # 采集机仍只展示别机包；纯汇总机的本机历史包也属于冷启动收敛范围，应能在报告中对账。
    include_own = role == ROLE_MERGE_ONLY
    others = {ref.path: ref for ref in export.all_packages(root)
              if include_own or ref.machine != machine_id}
    weekly_paths = {ref.path for ref in week_refs}
    shown = sorted((ref for path, ref in others.items()
                    if path in imported or path in weekly_paths),
                   key=lambda ref: (ref.week or "", ref.machine, ref.path.name))
    packages_rows = tuple(_package_row(db, ref, imported.get(ref.path)) for ref in shown)

    planned = set(plan_of_shop.values())
    arrived = {ref.machine for ref in week_refs}
    missing = tuple(sorted(m for m in planned if m != machine_id and m not in arrived))

    gaps, partial_gaps = _gaps(db, plan, week=week, days=days, machine_id=machine_id,
                                packages=week_refs, missing=missing)
    conflicts = _conflicts(db, week=week, machine_id=machine_id, plan=plan)
    image_missing_total = sum(result.images_missing for result in imported.values())
    judgment_conflicts = _judgment_conflicts(judgments, machine_id, week=week)
    # 票 03：收取那半的汇总状态与各来源的当前采纳读数（采纳从本机库现读；读不出来只落
    # 日志、报告如实记「未评估」——交换台不能因为一个附带的读而停摆）。
    collect_state: str | None = None
    collect_failed: tuple = ()
    adoption: tuple = ()
    if judgment_collect is not None and judgments is not None:
        collect_state, collect_failed = _receive_state(source_prep, judgment_collect)
        if judgment_collect.results:
            try:
                adoption = judgment_set.current_adoption(
                    judgments.cache, judgments.store, machine_id,
                    [result.machine for result in judgment_collect.results],
                    contract_signature=judgments.signature)   # 当前契约口径（票 04）
            except sqlite3.Error as exc:
                log.warning("采用读数读不出来（%s）：%s", judgments.cache, exc)
    todo = _todo(machine_id=machine_id, week=week, plan_of_shop=plan_of_shop, gaps=gaps,
                 conflicts=conflicts, missing=missing, pulls=pulls,
                 export_result=export_result, imported=imported,
                 image_missing_total=image_missing_total,
                 judgment_publish=judgment_publish, judgment_collect=judgment_collect,
                 source_refresh=source_refresh, source_prep=source_prep)

    failures: list[str] = []
    if export_result is not None and export_result.failed:
        failures.append(f"导出没成功：{export_result.failure}")
    failures += [f"{result.package} 没导成：{result.failure}"
                 for result in imported.values() if result.failed]
    if only == ONLY_EXPORT and role == ROLE_MERGE_ONLY:
        failures.append("本机是纯汇总机：导出这半没有可做的事")
    failures += _judgment_failures(judgment_publish, judgment_collect)
    failures += _source_failures(source_refresh)
    failures += _source_prep_failures(source_prep)
    failures += _sku_failures(sku_publish, sku_collect)
    # 整份远端周包未到是正常的空结果；报告仍保留 missing，只有已发布包的覆盖缺口才升级为软告警。
    soft = bool(gaps) or bool(conflicts) or any(not p.ok for p in pulls) \
        or (export_result is not None and export_result.images is not None
            and export_result.images.failure is not None) or image_missing_total > 0 \
        or bool(judgment_conflicts) or _judgment_soft(judgment_collect) \
        or (source_refresh is not None and source_refresh.held) \
        or _source_prep_soft(source_prep) or _sku_soft(sku_collect)

    return RunOutcome(
        week=week, machine_id=machine_id, role=role, ran_at=now, check=check, only=only,
        export=export_result, export_note=export_note, pulls=pulls,
        imports=tuple(imported.values()), merge_ran=merge_ran, packages=packages_rows,
        gaps=gaps, partial_gaps=partial_gaps, conflicts=conflicts,
        missing_machines=missing, todo=todo,
        summary=_summary(only=only, export_result=export_result, export_note=export_note,
                         imported=imported, merge_ran=merge_ran,
                         judgment_publish=judgment_publish, judgment_collect=judgment_collect,
                         source_prep=source_prep),
        failures=tuple(failures),
        exit_code=(EXIT_NOTHING_DONE if failures
                   else EXIT_NEEDS_LOOK if soft else EXIT_CLEAN),
        self_shops=plan.machine_keys(machine_id) if plan is not None else (),
        plan_known=plan is not None, plan_of_shop=plan_of_shop,
        judgments=judgments, judgment_note=judgment_note,
        judgment_publish=judgment_publish, judgment_collect=judgment_collect,
        judgment_conflicts=judgment_conflicts, source_refresh=source_refresh,
        source_prep=source_prep, task_id=task_id, judgment_collect_state=collect_state,
        judgment_collect_failed=collect_failed, judgment_adoption=adoption,
        sku_publish=sku_publish, sku_collect=sku_collect)


def _judgment_conflicts(judgments, machine_id: str, *, week: str) -> tuple:
    """本机冲突账里**本周记下的**决定冲突（票 04）：报告与退出码都只看这一周。

    读冲突账就好——重复收取时结果里不复现、账是累计的（票 04 的口径）；但账只增不自动
    消解，往次的冲突会让以后每一趟都红着，所以这里按记下的时刻切出本周边界，与采集侧
    「冲突」按周（`_conflicts`）同规。账本库读不出来只落日志、不当失败：交换台不能因为
    一个附带的读而停摆。
    """
    if judgments is None:
        return ()
    try:
        stored = DraftStore(judgments.store, machine_id).conflicts()
    except sqlite3.Error as exc:
        log.warning("冲突账读不出来（%s）：%s", judgments.store, exc)
        return ()
    monday, sunday = weekly_plan.week_window(week)
    return tuple(conflict for conflict in stored
                 if _in_week(conflict.seen_at, monday=monday, sunday=sunday))


def _in_week(moment: str, *, monday: dt.date, sunday: dt.date) -> bool:
    """冲突记下的时刻（收取时刻，UTC ISO）落在这周（北京时间）里吗。

    读不出的时刻留着——宁可多列一条，也别把一条冲突悄悄吞掉。
    """
    try:
        when = dt.datetime.fromisoformat(str(moment))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return monday <= when.astimezone(CST).date() <= sunday


def _judgment_failures(publish, collect) -> list[str]:
    """判断集这半的硬失败（退出码 2）：与采集侧「导出没成功」「有包没导成」同规。"""
    failures: list[str] = []
    if publish is not None and publish.failed:
        failures.append(f"判断集没发出去：{publish.failure}")
    if collect is not None:
        if collect.failure is not None:
            failures.append(f"判断集没收进来：{collect.failure}")
        failures += [f"judged-{result.machine} 的判断集没导成：{result.failure}"
                     for result in collect.results if result.failed and not result.unreachable]
    return failures


def _judgment_soft(collect) -> bool:
    """需要人看一眼的判断集状况（退出码 1）：某家 judged 库拉不动（与「通道没拉成」同规）。

    本周记下的决定冲突另算（`_assemble` 里与采集侧的冲突并列）。
    """
    return bool(collect) and any(result.failed and result.unreachable
                                 for result in collect.results)


def _sku_failures(publish, collect) -> list[str]:
    """SKU 判断集这半的硬失败（退出码 2）：与判断集同规——版本不认识的明确拒绝也算。"""
    failures: list[str] = []
    if publish is not None and publish.failed:
        failures.append(f"SKU 判断集没发出去：{publish.failure}")
    if collect is not None:
        if collect.failure is not None:
            failures.append(f"SKU 判断集没收进来：{collect.failure}")
        failures += [f"judged-{result.machine} 的 SKU 判断集没导成：{result.failure}"
                     for result in collect.results if result.failed and not result.unreachable]
    return failures


def _sku_soft(collect) -> bool:
    """需要人看一眼的 SKU 状况（退出码 1）：某家 judged 库拉不动（同判断集口径）。"""
    return bool(collect) and any(result.failed and result.unreachable
                                 for result in collect.results)


def _source_failures(source_refresh) -> list[str]:
    """来源清单这半的硬失败（退出码 2）：维护机没能刷新/发布清单（与「导出没成功」同规）。"""
    if source_refresh is not None and source_refresh.failed:
        return [f"判断来源清单没刷新：{source_refresh.failure}"]
    return []


def _source_text(result: judged_sources.RefreshResult) -> str:
    """来源清单这一趟的正文（报告「来源清单：」与命令行共用一处措辞）。"""
    if result.failed:
        return f"没刷新（{_first_line(result.failure)}）"
    if result.note is not None:
        return result.note
    count = len(result.sources)
    if result.held:
        why = result.warnings[0] if result.warnings else "来源有变化"
        return f"{count} 个来源 —— 保留了上一版有效快照、等人确认（{why}）"
    if result.unchanged:
        return f"{count} 个来源，与上一版相同（无新提交）"
    return f"{count} 个来源 → plan @ {result.commit}"


def _source_row(outcome: RunOutcome) -> str:
    """报告第三节的「来源清单」一行：只在实际交换任务里刷过、且本机是维护机时才有。"""
    return f"- 来源清单：{_source_text(outcome.source_refresh)}"


# 来源接入（票 02）的进度行与报告行共用这一处措辞：动作短名 + 「当前没有判断集包」。
_PREP_ACTION_GLOSS = {judged_sources.ACTION_CLONED: "新克隆",
                      judged_sources.ACTION_UPDATED: "已快进",
                      judged_sources.ACTION_UNCHANGED: "已是最新"}


def _source_prep_text(entry: judged_sources.PreparedSource) -> str:
    """一条来源接入结果的人话（进度行与报告第三节共用）。"""
    if entry.blocked:
        return f"{entry.repo} 被阻断（{_first_line(entry.failure)}）"
    if entry.skipped:
        return f"{entry.repo} 这次没接入（{_first_line(entry.reason)}）"
    gloss = _PREP_ACTION_GLOSS.get(entry.action, entry.action or "已接入")
    if entry.state == judged_sources.SOURCE_CONNECTED_NO_PACKAGE:
        return f"{entry.repo} 已接入（{gloss}；当前没有判断集包）"
    return f"{entry.repo} 已接入（{gloss}）"


def _source_prep_lines(prep: judged_sources.PreparedSources) -> list[str]:
    """来源接入这一趟的进度行：告警在前、逐条来源在中、未就绪收尾。"""
    lines = [f"来源接入：{warning}" for warning in prep.warnings]
    lines += [f"来源接入：{_source_prep_text(entry)}" for entry in prep.entries]
    if not prep.ready:
        lines.append(f"接收通道未就绪：{prep.not_ready}")
    return lines


def _source_prep_row(outcome: RunOutcome) -> str:
    """报告第三节的「来源接入」一行（票 02 + 票 03 的「已发现」）：逐条来源的接入结局、
    清单外本地已有的来源，或整趟的未就绪。

    清单外的本地 judged 库点名列出（票 03 的口径：清单里「已声明」，清单外「已发现」）；
    照旧收——快照语义：来源从清单消失不触发删克隆。
    """
    prep = outcome.source_prep
    local = ("；清单外本地已有：" + "、".join(f"{judgment_set.REPO_PREFIX}{machine}"
                                          for machine, _ in prep.local_only) + "（照旧收）"
             if prep.local_only else "")
    if not prep.ready:
        return f"- 来源接入：接收通道未就绪 —— {prep.not_ready}"
    if not prep.entries:
        why = _first_line(prep.warnings[0]) if prep.warnings else "来源清单里没有别的来源"
        return f"- 来源接入：没有可接入的来源清单（{why}）{local}"
    head = "（用了上一版来源清单）" if prep.stale else ""
    return (f"- 来源接入{head}："
            + " · ".join(_source_prep_text(entry) for entry in prep.entries) + local)


def _source_prep_failures(source_prep) -> list[str]:
    """来源接入这半的硬失败（退出码 2）：没有任何可用来源 = 接收通道未就绪。

    不能表现成「没有新判断集」——这正是 2026-09-24 现场那类误导的落点。
    """
    if source_prep is not None and not source_prep.ready:
        return [f"接收通道未就绪：{source_prep.not_ready}"]
    return []


def _source_prep_soft(source_prep) -> bool:
    """需要人看一眼的接入状况（退出码 1）：某来源被阻断/跳过，或计划库拉不动用了上一版。

    未就绪另有硬失败那一档，不在这里重复记。
    """
    if source_prep is None or not source_prep.ready:
        return False
    return source_prep.stale or any(entry.blocked or entry.skipped
                                    for entry in source_prep.entries)


def _package_row(db: Database, ref: export.PackageRef,
                 result: merge.ImportResult | None) -> PackageRow:
    """一行：这次的导入结果优先；汇总没跑（或没碰它）时回落到幂等账里的状态。"""
    row = PackageRow(repo=f"raw-{ref.machine}",
                     week_short=f"W{export.package_week(ref.path.name)}",
                     rows=None, inserted=None, replaced=None, conflicts=None, images=None,
                     result="")
    if result is not None and result.failed:
        return dataclasses.replace(row, result=f"导入没成功：{_first_line(result.failure)}")
    if result is not None and result.images_retried:
        status = (f"补图后仍缺 {result.images_missing:,} 张"
                  if result.images_missing else "图片已补齐")
        return dataclasses.replace(row, rows=result.rows_total, result=f"已导入过 → {status}")
    if result is not None and result.skipped:
        return dataclasses.replace(row, rows=result.rows_total, result="已导入过 → 跳过")
    if result is not None:
        return dataclasses.replace(
            row, rows=result.rows_total, inserted=result.rows_inserted,
            replaced=result.rows_replaced, conflicts=result.conflicts,
            images=(result.images_pulled, result.images_bytes), result="已导入")
    known = _ledger_row(db.conn, ref.path)
    if known is not None:
        return dataclasses.replace(row, rows=known["rows_total"], result="已导入过")
    return dataclasses.replace(row, result="还没汇总")


def _gaps(db: Database, plan, *, week: str, days: tuple[dt.date, ...], machine_id: str,
          packages: tuple[export.PackageRef, ...], missing: tuple[str, ...]
          ) -> tuple[tuple[GapLine, ...], tuple[GapLine, ...]]:
    """按计划店铺评估整段覆盖，并分出整段异常与部分日期提示。

    覆盖的判据是「本机库 ∪ 交换区里全部已到包的覆盖」；一份远端周包还没到时，
    由 ``missing`` 的“还没收到的包”语义负责，不把它提前算成缺口。只有一个计划店铺
    在全部已评估日期都没有库存观测，才进入 ``gaps``；只缺部分日期的事实进入
    ``partial_gaps``，供报告追查但不参与退出码和待办。
    """
    if plan is None or not days:
        return (), ()
    span = (days[0].isoformat(), days[-1].isoformat())
    local = {(str(row[0]), str(row[1])) for row in db.conn.execute(
        "SELECT DISTINCT shop_key, date FROM inventory WHERE date BETWEEN ? AND ?", span)}
    coverage: set[tuple[str, str]] = set()
    for ref in packages:
        try:
            coverage |= merge.package_shop_days(ref.path, days[0], days[-1])
        except (OSError, EOFError, sqlite3.Error) as exc:
            log.warning("缺口检查读不动包 %s：%s（该包的导入失败会另行报出）", ref.rel, exc)

    gaps: list[GapLine] = []
    partial_gaps: list[GapLine] = []
    for row in plan.rows:
        if row.machine_id in missing:
            continue                        # “还没来的包”那一行负责这台机器
        row_gaps: list[GapLine] = []
        for day in days:
            pair = (row.shop_key, day.isoformat())
            if pair in local or pair in coverage:
                continue
            if row.machine_id == machine_id:
                reason = gap_reason(db, pair[1])
            else:
                reason = "计划里该采到，收到的包里没有这一天"
            row_gaps.append(GapLine(machine=row.machine_id, shop_key=row.shop_key,
                                     date=pair[1], reason=reason))
        if not row_gaps:
            continue
        (gaps if len(row_gaps) == len(days) else partial_gaps).extend(row_gaps)
    return tuple(gaps), tuple(partial_gaps)

def gap_reason(db: Database, day: str) -> str:
    """本机缺这一天，为什么：当天的轮次终态（进行中 = 半截的一天）。

    周报的缺口理由与三机验收核对（`tools/acceptance_check.py`）共用这一处推导——
    「缺口逐条有说明」两边说的必须是同一句人话。
    """
    if rounds.active_round(db, day) is not None:
        return "当天采集还在跑（半截的一天）"
    on_day = rounds.on_date(db, day)
    if not on_day:
        return "当天没有采集记录"
    reason = on_day[-1].reason
    if reason is None:
        return "当天采集的终态不详；结束原因：未记录"
    if reason == rounds.TerminalReason.STARTUP_FAILED:
        detail = rounds.ending_detail(db, on_day[-1].id) or "未记录"
        return f"{_REASON_GLOSS[reason.value]}；失败原因：{detail}"
    return _REASON_GLOSS.get(reason.value, f"当天采集的终态是 {reason.value}")


def _conflicts(db: Database, *, week: str, machine_id: str,
               plan) -> tuple[ConflictEntry, ...]:
    """本机导入账里的本周冲突，按日期、店铺排序。"""
    monday, sunday = weekly_plan.week_window(week)
    rows = db.conn.execute(
        "SELECT c.observed_date, c.shop_key, c.winner_machine, c.winner_at, c.loser_machine, "
        "c.loser_at, c.loser_rows_replaced, c.loser_rows_kept, "
        "p.machine_id AS package_machine "
        "FROM import_conflicts c LEFT JOIN import_packages p "
        "ON p.package_sha256 = c.package_sha256 "
        "WHERE c.observed_date BETWEEN ? AND ? ORDER BY c.observed_date, c.shop_key",
        (monday.isoformat(), sunday.isoformat())).fetchall()
    return tuple(ConflictEntry(
        shop_key=str(row["shop_key"]), date=str(row["observed_date"]),
        winner_machine=str(row["winner_machine"]), winner_at=str(row["winner_at"]),
        loser_machine=str(row["loser_machine"]), loser_at=str(row["loser_at"]),
        replaced=int(row["loser_rows_replaced"]), kept=int(row["loser_rows_kept"]),
        packages=((f"raw-{row['package_machine']}",) if row["package_machine"] else ()))
        for row in rows)


def _summary(*, only, export_result, export_note, imported, merge_ran,
             judgment_publish=None, judgment_collect=None, source_prep=None) -> str:
    """报告开头的「本次运行」行：这一次具体做了什么。判断集两段只在真跑了时出现。"""
    parts: list[str] = []
    if only is not None:
        parts.append(f"只跑了{ONLY_GLOSS[only]}")
    if export_result is not None:
        if export_result.failed:
            parts.append("导出没成功")
        elif export_result.unchanged:
            parts.append("导出：本周包上次已发布（无新提交）")
        else:
            parts.append("导出已发布")
    elif export_note is not None and only in (None, ONLY_EXPORT):
        parts.append(f"导出：{export_note}")
    if judgment_publish is not None:
        if judgment_publish.failed:
            parts.append("判断集没发出去")
        elif judgment_publish.note is not None:
            parts.append("本机没有判断集可发布")
        elif judgment_publish.unchanged:
            parts.append("发布判断集：同内容（无新提交）")
        else:
            parts.append(f"发布判断 {judgment_publish.rows.get('judgments', 0)} 条")
    if merge_ran:
        results = list(imported.values())
        fresh = [r for r in results if not r.skipped and not r.failed]
        retried = [r for r in results if r.images_retried and not r.failed]
        skipped = [r for r in results if r.skipped and not r.images_retried]
        failed = [r for r in results if r.failed]
        if fresh:
            parts.append(f"新收 {len(fresh)} 个包（{sum(r.rows_total for r in fresh):,} 行）")
        if retried:
            missing = sum(r.images_missing for r in retried)
            parts.append(f"为 {len(retried)} 个已导入包补图（仍缺 {missing:,} 张）")
        if skipped:
            parts.append(f"跳过 {len(skipped)} 个已导入")
        if failed:
            parts.append(f"{len(failed)} 个包没导成")
        if not (fresh or retried or skipped or failed):
            parts.append("没有新包")
    if source_prep is not None:
        if not source_prep.ready:
            parts.append("接收通道未就绪")
        else:
            cloned = [entry for entry in source_prep.entries
                      if entry.action == judged_sources.ACTION_CLONED]
            off = [entry for entry in source_prep.entries if entry.blocked or entry.skipped]
            if cloned:
                parts.append(f"新接入来源 {len(cloned)} 个")
            if off:
                parts.append(f"{len(off)} 个来源没接入")
    if judgment_collect is not None:
        results = list(judgment_collect.results)
        fresh = [r for r in results if not r.skipped and not r.failed and r.note is None]
        skipped = [r for r in results if r.skipped]
        failed = [r for r in results if r.failed]
        if fresh:
            parts.append(f"收下 {len(fresh)} 份判断集")
        if skipped:
            parts.append(f"跳过 {len(skipped)} 份已收过的判断集")
        if failed:
            parts.append(f"{len(failed)} 份判断集没收到")
        if judgment_collect.failure is not None:
            parts.append("判断集没收成")
        if not (fresh or skipped or failed) and judgment_collect.failure is None:
            parts.append("没有判断集可收")
    return " · ".join(parts)


def _day_count(count: int) -> str:
    return "一天" if count == 1 else f"{count} 天"


def _by_machine(gaps: tuple[GapLine, ...]) -> list[tuple[str, list[GapLine]]]:
    """按份额归属的机器分组（机器标识排序）——缺口节与待办节共用。"""
    return [(machine, [gap for gap in gaps if gap.machine == machine])
            for machine in sorted({gap.machine for gap in gaps})]


def _gap_detail(gaps: list[GapLine], *, reasons: bool) -> str:
    """一组缺口的行内明细；`reasons` 带上理由（本机份额才有人话理由可带）。"""
    return "；".join(f"{gap.shop_key} {gap.date[5:]}" + (f" —— {gap.reason}" if reasons else "")
                     for gap in gaps)


def _todo(*, machine_id, week, plan_of_shop, gaps, conflicts, missing, pulls, export_result,
          imported, image_missing_total, judgment_publish=None,
          judgment_collect=None, source_refresh=None, source_prep=None) -> tuple[str, ...]:
    """「下次该做什么」：从这次运行的事实与账里长出来。"""
    week_short = week.split("-")[1]
    items: list[str] = []
    for machine in missing:
        items.append(f"raw-{machine} 的 {week_short} 还没发布或没拉到。它发布后重跑一次"
                     "汇总就能收进来（重复导入是安全的）")

    self_over: list[str] = []
    for entry in conflicts:
        planned = plan_of_shop.get(entry.shop_key)
        if planned is not None and planned != machine_id and entry.shop_key not in self_over \
                and machine_id in (entry.winner_machine, entry.loser_machine):
            self_over.append(entry.shop_key)
    if self_over:
        items.append(f"提醒本机操作者：不要手工勾选本期不归本机的店（{'、'.join(sorted(self_over))}），"
                     "多采只会变成汇总侧的一条冲突")
    unexplained = [c for c in conflicts if c.shop_key not in self_over]
    if unexplained:
        items.append(f"还有 {len(unexplained)} 处冲突不是本机多采造成的（见第{CONFLICTS_SECTION}节）："
                     "确认是越权、降级逃生口还是换周交接不清")

    if mine := [gap for gap in gaps if gap.machine == machine_id]:
        items.append(f"本机缺{_day_count(len(mine))}（{_gap_detail(mine, reasons=True)}）"
                     "无法回填，不影响下周")
    others = [gap for gap in gaps if gap.machine != machine_id]
    for machine, group in _by_machine(tuple(others)):
        items.append(f"raw-{machine} 的包缺{_day_count(len(group))}"
                     f"（{_gap_detail(group, reasons=False)}）：历史日期补不了，如实记一笔")

    for pull in pulls:
        if not pull.ok:
            items.append(f"{pull.repo} 拉不动（{_first_line(pull.failure)}）：通道恢复后重跑一次")
    if export_result is not None:
        if export_result.failed:
            items.append("导出没成功（见第一节）：修好通道后重跑一次")
        elif export_result.images is not None and export_result.images.failure is not None:
            items.append("图片通道这趟没传成（见第一节）：通道恢复后重跑一次导出就能补上")
        if export_result.sku_image_failures:
            # 空图与代填不是失败（`export.failed_sku_images` 的口径），这里只点失败的行。
            items.append(f"本周有 {export_result.sku_image_failures} 条 SKU 图下载失败"
                         "（见第一节）：用 SKU 图重试命令按最新失败行补回来"
                         "（`python -m bestseller_monitor.product_images "
                         "--database <库存库路径> --retry-sku-image <流水行编号>`），"
                         "库存数据不动")
    for result in imported.values():
        if result.failed:
            items.append(f"{result.package} 没导成（见第二节）：修好后重跑一次"
                         "（重跑等价于首次导入，安全）")
    if image_missing_total:
        items.append(f"这次有 {image_missing_total} 张图没拉到（见第二节），如实记一笔")
    if judgment_publish is not None and judgment_publish.failed:
        items.append("本机判断集没发出去（见第三节）：通道或克隆修好后重跑一次")
    if judgment_collect is not None:
        if judgment_collect.failure is not None:
            items.append("判断集没收进来（见第三节）：多半是分析程序正占着缓存，"
                         "等它跑完再重跑一次交换台")
        for result in judgment_collect.results:
            if not result.failed:
                continue
            if result.unreachable:
                items.append(f"judged-{result.machine} 拉不动（见第三节）：通道恢复后重跑一次")
            else:
                items.append(f"judged-{result.machine} 的判断集没导成（见第三节）："
                             "让那台机器重发一版、或查一下这个包（重跑等价于首次导入）")
    if source_refresh is not None and source_refresh.failed:
        items.append("判断来源清单没刷新（见第三节）：token / 网络 / plan 克隆修好后重跑一次")
    elif source_refresh is not None and source_refresh.held:
        items.append("判断来源清单有来源减少或远端地址变化（见第三节）：核对 token 权限与库是否"
                     "真被删/改名，确认无误后跑 `python exchange.py --confirm-source-shrink` "
                     "按新的发布")
    if source_prep is not None and not source_prep.ready:
        items.append(f"接收通道未就绪（见第三节）：{source_prep.not_ready}")
    elif source_prep is not None and source_prep.stale:
        items.append("计划库这次拉不动、来源接入用了上一版快照（见第三节）：通道恢复后重跑一次；"
                     "本机还没有克隆的来源这次没有新建")
    if source_prep is not None:
        for entry in source_prep.entries:
            if entry.blocked:
                items.append(f"{entry.repo} 没接入（见第三节）：{_first_line(entry.failure)}")

    return tuple(items) if items else ("没有待办：本周干净",)


# ---------- 报告渲染（三份原型样例的形态 + 票 05 的判断集一节） ----------

# 「冲突」节的编号（汉数字）：标题与待办里「见第X节」这类指路共用这一处（其余各节的编号
# 就写在自己的标题里——它们不随内容出现或消失）。判断集那节（三）在它前面，所以它从样例
# 里的「四」挪到了「五」；待办那节自己跟着它走（见 `_todo_section`）。
CONFLICTS_SECTION = "五"


def _num(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _images_cell(images: tuple[int, int] | None) -> str:
    return "—" if images is None else f"{images[0]}（{_mb(images[1])}）"


def _hhmm(moment: str) -> str:
    try:
        return dt.datetime.fromisoformat(str(moment)).strftime("%H:%M")
    except ValueError:
        return str(moment)


def _side_text(outcome: RunOutcome, entry: ConflictEntry, machine: str, at: str) -> str:
    """冲突里一方的人话：本机 / 计划机（那家店本周的计划归属）/ 机器编号，计划外多采注明。"""
    planned = outcome.plan_of_shop.get(entry.shop_key)
    who = "本机" if machine == outcome.machine_id else (
        "计划机" if planned == machine else machine)
    note = "（计划外多采）" if planned is not None and planned != machine else ""
    return f"{who} {_hhmm(at)}{note}"


def _render(outcome: RunOutcome) -> str:
    monday, sunday = weekly_plan.week_window(outcome.week)
    span = f"{monday.month}月{monday.day}日 – {sunday.month}月{sunday.day}日"
    lines = [
        f"# 库存数据交换周报 · {outcome.week}",
        "",
        f"本机 **{outcome.machine_id}**（{ROLE_GLOSS[outcome.role]}）· "
        f"{outcome.ran_at:%Y-%m-%d %H:%M} 运行 · 覆盖 {span}",
        "",
        f"**结果**：{VERDICT[outcome.exit_code]}（退出码 {outcome.exit_code}）· "
        f"缺口 {len(outcome.gaps)} · "
        f"冲突 {len(outcome.conflicts)} · 还没来的包 {len(outcome.missing_machines)} · "
        f"判断集冲突 {len(outcome.judgment_conflicts)} · "
        f"部分日期提示 {len(outcome.partial_gaps)}",
        f"**本次运行**：{outcome.summary}",
        "",
    ]
    lines += _publish_section(outcome)
    lines += _received_section(outcome)
    lines += _judgment_section(outcome)
    lines += _gaps_section(outcome)
    lines += _conflicts_section(outcome)
    lines += _todo_section(outcome)
    return "\n".join(lines) + "\n"


def _judgment_section(outcome: RunOutcome) -> list[str]:
    """第三节：判断集这半的记账（票 05、spec §8）——发布了几条判断、收进了谁的、
    采纳了几条决定（点名来源）、冲突几条（点名双方）。

    发布与收取那几行从**这一趟**的结果读（没跑或没有可发的如实写）；冲突那行从**本机
    冲突账**现读、切在本周（账是累计的，重复收取时结果里不复现，票 04 的口径）。
    来源清单那一行（票 01）也记在这里：它是判断集的输入（收谁），与本节的发布收取是同一件事。
    """
    out = ["## 三、判断集"]
    # 来源清单这一行只在它是本机做的事时才出现：维护机（配了 owner）刷了/没刷成都记；
    # 「本机不是来源维护机」是恒定事实、不是状态，收进报告只会每周重复一句噪声。
    if outcome.source_refresh is not None and outcome.source_refresh.note is None:
        out.append(_source_row(outcome))
    # 来源接入一行（票 02）：只在收取真跑过时出现（没配分析配置 / --only 别的动作都不出现）。
    if outcome.source_prep is not None:
        out.append(_source_prep_row(outcome))
    if outcome.judgments is None:
        # 没配分析配置：判断集这半整趟没做——如实写一句，不折成失败（那是上机阶段的事）
        out.append(f"- 这趟没做：{outcome.judgment_note}")
        return out + [""]
    out.append(_judgment_publish_row(outcome))
    if outcome.judgment_collect is not None:
        out.append(_judgment_collect_task_row(outcome))
    out += _judgment_collect_rows(outcome)
    if outcome.judgment_collect_state is not None:
        out.append(_judgment_collect_state_row(outcome))
    out.append(_judgment_adopt_row(outcome))
    out += _judgment_conflict_rows(outcome)
    if outcome.sku_publish is not None:
        out.append("- 发布 SKU：" + _sku_publish_text(outcome.sku_publish))
    if outcome.sku_collect is not None:
        out += ["- 收取 SKU：" + text for text in _sku_collect_texts(outcome.sku_collect)]
    out.append("- 口径：逐级分开记账——来源已声明/已发现 → 已接入（含「已接入但当前没有"
               "判断集包」）→ 包已发现/已接收 → 已导入 → 门禁（格式/自洽不过 = 整包拒"
               "绝；署名不符或证据版本没有着落的判断行照收留档、记 gated_out、不驱动"
               "当前分析）→ 冲突 → 采纳；导入数不是采用数（采纳从本机库现读）。判断集"
               "走 judged-<机器> 这条独立通道（spec §3）；同内容重复发布/重复收取都是"
               "空操作；人工决定不冲突即生效、冲突只记不动两边的决定（双方来源、来源"
               "提交与时刻都留档），在分析页面上裁决（冲突账只增不自动消解，这里只列"
               "本周记下的）；SKU 判断集走同库新文件（data/sku-judgments.db.gz），"
               "与旧商品级包互不相扰")
    return out + [""]


def _judgment_only_note(outcome: RunOutcome, what: str) -> str:
    """判断集这半没跑时的那句说明（与「一、本机发布」的导出说明同一个句式）。"""
    return (f"本次只跑了{ONLY_GLOSS[outcome.only]}（--only {outcome.only}），没做{what}")


def _judgment_counts(rows: dict[str, int]) -> str:
    """判断集各表多少行的一行话（发布与收取共用；键就是包内六类表的名字）。"""
    decided = sum(rows.get(name, 0) for name in judgment_set.LEDGER_TABLES)
    return (f"判断 {rows.get('judgments', 0)} 条 · 视觉描述 {rows.get('visual_evidence', 0)} 条 · "
            f"证据 {rows.get('evidence', 0)} 条 · 人工决定 {decided} 条")


def _judgment_publish_text(result: judgment_set.PublishResult) -> str:
    """发布结果的正文（报告「- 发布：」与命令行「发布判断集：」共用这一处措辞）。"""
    counts = _judgment_counts(result.rows)
    if result.failed:
        return f"{counts} —— 没发出去（{_first_line(result.failure)}）"
    if result.note is not None:
        return result.note
    if result.unchanged:
        return f"{counts} —— 与已发布那份同内容（无新提交）"
    return f"{counts} → {judgment_set.REPO_PREFIX}{result.machine_id} @ {result.commit}"


def _collect_identity(result: judgment_set.CollectResult) -> str:
    """这份包的审计身份（票 03）：包相对路径、来源提交、内容摘要、远端地址。

    报告行与逐次流水共用这一处措辞——日志里报的数都能对回导入账上的同一行。
    """
    parts = [f"包 {result.package_rel}"]
    if result.source_commit:
        parts.append(f"提交 {result.source_commit}")
    if result.digest:
        parts.append(f"摘要 {result.digest}")
    if result.remote:
        parts.append(f"远端 {result.remote}")
    return "；".join(parts)


def _gate_text(result: judgment_set.CollectResult) -> str:
    """这份包的门禁回执（票 04）：gated_out 数量与原因、未评估——接在计数行后面（带前导「；」）。

    没记过门禁的老账（票 04 之前收的）如实说「未记录」，不冒充 0。gated_out 数每份都印
    （验收要求四个数同时可见）；契约读不出时补一句「未评估 N 条」说清哪部分判不了。
    """
    summary = result.gate_summary
    if not summary:
        return "；门禁未记录（这份包在票 04 之前收过）"
    details = []
    if summary.get("signature"):
        details.append(f"署名不符 {summary['signature']}")
    if summary.get("evidence_version"):
        details.append(f"证据版本没有着落 {summary['evidence_version']}")
    parts = [f"门禁 gated_out {result.gated_out} 条"
             + (f"（{'、'.join(details)}）" if details else "")]
    if summary.get("unevaluated"):
        parts.append(f"未评估 {summary['unevaluated']} 条（本机没有可用的分析署名）")
    return "；" + "；".join(parts)


def _judgment_collect_texts(collect: judgment_set.CollectOutcome) -> list[str]:
    """收取结果每个来源一句（没有可收的、整趟没做成也是一句）：报告与逐次流水共用。

    收下与已收过的两类带上审计身份（票 03：包路径、来源提交、内容摘要、远端地址）与
    门禁回执（票 04：gated_out 数量与原因、未评估）——每个数都能对回导入账上的同一行。
    """
    if collect.failure is not None:
        return [f"没做成（{_first_line(collect.failure)}）"]
    if not collect.results:
        note = f"（{collect.notes[0]}）" if collect.notes else ""
        return [f"没有判断集可收{note}"]
    texts = []
    for result in collect.results:
        name = f"{judgment_set.REPO_PREFIX}{result.machine}"
        if result.failed:
            what = "拉不动" if result.unreachable else "没收成"
            texts.append(f"{name} 这本{what}（{_first_line(result.failure)}）")
        elif result.note is not None:
            texts.append(f"{name} 没有判断集可收 —— {result.note}")
        elif result.skipped:
            texts.append(f"{name} —— 已收过（{_judgment_counts(result.rows)}；无变化）"
                         f"{_gate_text(result)}；{_collect_identity(result)}")
        else:
            texts.append(f"{name} —— {_judgment_counts(result.rows)}"
                         f"（新增 {sum(result.added.values()):,} 行）{_gate_text(result)}"
                         f"；{_collect_identity(result)}")
    return texts


def _judgment_collect_task_row(outcome: RunOutcome) -> str:
    """「收取任务」一行（票 03）：任务 ID 与这趟读到的来源清单版本（plan 提交的短哈希）。"""
    version = outcome.source_prep.snapshot_version if outcome.source_prep is not None else ""
    return "- 收取任务：" + _collect_task_text(outcome.task_id, version)


def _judgment_collect_state_row(outcome: RunOutcome) -> str:
    """「收取结果」一行（票 03、spec §2）：ok / partial_failure / receive_channel_not_ready。"""
    return ("- 收取结果："
            + _receive_state_text(outcome.judgment_collect_state, outcome.judgment_collect_failed))


def _judgment_publish_row(outcome: RunOutcome) -> str:
    """「发布」一行：本机发了几条判断、去了哪（或为什么没发）。"""
    result = outcome.judgment_publish
    if result is None:
        return f"- 发布：{_judgment_only_note(outcome, '发布判断集')}"
    return f"- 发布：{_judgment_publish_text(result)}"


def _judgment_collect_rows(outcome: RunOutcome) -> list[str]:
    """「收取」每来源一行：收进了谁的判断集、收进来多少；没有可收的如实写「没有」。"""
    collect = outcome.judgment_collect
    if collect is None:
        return [f"- 收取：{_judgment_only_note(outcome, '收取判断集')}"]
    return [f"- 收取：{text}" for text in _judgment_collect_texts(collect)]


def _judgment_adopt_row(outcome: RunOutcome) -> str:
    """「采纳」一行（票 03 的口径、票 04 收紧）：导入数不是采用数——采用从本机库现读。

    判断侧算**本机当前分析署名**下、且证据版本在本机有着落的缓存行（分析消费的正是这些
    行；换过模型配置后旧署名下的行不算了）；本机还没有当前署名下的判断（本机自己写的
    行里没有契约那个署名）记「未评估」，不拿导入数冒充、不伪造零；决定侧算本机账本里
    在效的行（已确认的关系、单独成组、排除对，ADR-0039 判据 2 的「采纳」）。这行是
    **当前状态**：同周重跑读到的是同一份，不是「这趟新增了几条」。
    """
    collect = outcome.judgment_collect
    if collect is None or not collect.results:
        return "- 采纳：没有（这趟没有收到判断集）"
    if not outcome.judgment_adoption:
        return "- 采纳：未评估（采用读数这趟没读出来，见日志）"
    adopted = [info for info in outcome.judgment_adoption
               if info.decisions or info.judgments]
    if adopted:
        def counts(info):
            judged = "未评估" if info.judgments is None else f"{info.judgments} 条"
            return f"{info.machine} 判断 {judged}、人工决定 {info.decisions} 条"
        return ("- 采纳：" + "；".join(counts(info) for info in adopted)
                + "（判断 = 本机当前分析署名下、证据版本有着落的缓存行；"
                "人工决定 = 本机账本在效的行）")
    if not outcome.judgment_adoption[0].evaluated:
        return ("- 采纳：判断 未评估（本机还没有当前分析署名下的判断——先在缓存里闲置待用；"
                "跑过一次当前配置的分析后这里才有采用口径）· 人工决定 0 条在效")
    return ("- 采纳：没有（判断都不在本机当前分析署名下、或证据版本对不上；"
            "账本里没有这些来源的在效决定）")


# 冲突三类的人话（spec §6）：键就是 `analysis_store` 那三个类别名，不在这里重拼字符串。
_CONFLICT_GLOSS = {
    CONFLICT_GROUP_VS_EXCLUSION: "一边并组、一边排除",
    CONFLICT_DIFFERENT_GROUPS: "同一成员分进不同的组",
    CONFLICT_CONFIRM_VS_WITHDRAW: "确认与撤回相对",
}
# 冲突一侧「决定内容」的种类：`analysis_store` 写进冲突账的内容字典里的 kind 值
# （relation／standalone／exclusion，那边没有公开常量），键按那个形状写。
_DECISION_GLOSS = {"relation": "组", "standalone": "单独成组", "exclusion": "排除对"}


def _judgment_conflict_rows(outcome: RunOutcome) -> list[str]:
    """「冲突」每笔一行：点名双方（谁的什么 vs 谁的什么）与商品；没有就如实写「没有」。

    列的是**本周记下的**那些（与采集侧「冲突」按周同规）；往次的冲突留着账里、在分析
    页面上看——口径行说明这一点。外来那侧随行带上包的来源提交（票 04，记过才显示）。
    """
    conflicts = outcome.judgment_conflicts
    if not conflicts:
        return ["- 冲突：没有"]
    rows = []
    for conflict in conflicts:
        kind = _CONFLICT_GLOSS.get(conflict.kind, conflict.kind)
        incoming = _decision_side(conflict.incoming)
        local = "、".join(_decision_side(content) for content in conflict.local)
        members = "、".join(conflict.members)
        packed = f"；外来包提交 {conflict.source_commit}" if conflict.source_commit else ""
        rows.append(f"- 冲突：{kind} —— 外来的 {incoming} vs 本机的 {local}"
                    f"（商品 {members}{packed}；在分析页面上裁决）")
    return rows


def _decision_side(content: dict) -> str:
    """冲突一方的说法：谁的 + 什么决定（组 / 单独成组 / 排除对）。"""
    kind = _DECISION_GLOSS.get(content.get("kind", ""), content.get("kind", "决定"))
    return f"{content.get('machine', '?')} 的 {kind}"


def _publish_section(outcome: RunOutcome) -> list[str]:
    out = ["## 一、本机发布"]
    result = outcome.export
    if result is None:
        # 没跑导出（纯汇总机跳过 / --only merge）：入口保留着的那句话 +
        # 本机负责的店照旧对照计划说清楚（没有导出结果，行数/图片这两行无从谈起）
        out.append(f"- 导出：{outcome.export_note}")
        out.append("- 本机负责：" + _self_shops_text(outcome))
        return out + [""]
    name = pathlib.Path(result.package_rel).name
    if result.failed:
        out.append(f"- 包：{name} 这次没发出去（{_first_line(result.failure)}）")
    else:
        out.append(f"- 包：{name}（{_mb(result.package_size)}）→ "
                   f"raw-{result.machine_id} @ {result.commit}")
    out.append("- 本机负责：" + _self_shops_text(outcome))
    rows = result.rows
    out.append("- 行数：inventory {:,} · products {:,} · skus {:,} · 版本 {:,} · SKU 图 {:,}"
               .format(rows.get("inventory", 0), rows.get("products", 0),
                       rows.get("skus", 0), rows.get("product_information_versions", 0),
                       rows.get("sku_image_versions", 0)))
    out.append("- 图片：" + _images_text(result.images))
    return out + [""]


def _self_shops_text(outcome: RunOutcome) -> str:
    if outcome.role == ROLE_MERGE_ONLY:
        return "无（纯汇总机，不参与计划）"
    if not outcome.plan_known:
        return f"本机没有本周（{outcome.week}）的落库计划，无从对照"
    if not outcome.self_shops:
        return "本周空手（计划里没有归本机的店）"
    return "、".join(outcome.self_shops)


def _images_text(images) -> str:
    if images is None:
        return "这趟没做（导出没成功）"
    if images.failure is not None:
        text = (f"没传完（{_first_line(images.failure)}）；已传 {images.uploaded} 张"
                f"（{_mb(images.uploaded_bytes)}）")
    else:
        text = f"新增 {images.uploaded} 张（{_mb(images.uploaded_bytes)}）"
    text += f"；已有 {images.skipped} 张跳过"
    if images.missing:
        text += f"；本机没有字节的 {len(images.missing)} 张（对方从图片库取）"
    return text


def _received_section(outcome: RunOutcome) -> list[str]:
    out = ["## 二、收进来的包"]
    if not outcome.packages:
        return out + ["- 没有收到别的机器的包", ""]
    out += ["| 来源 | 周 | 行数 | 新增 | 覆盖 | 冲突 | 图片 | 结果 |",
            "|---|---|---|---|---|---|---|---|"]
    for row in outcome.packages:
        out.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            row.repo, row.week_short, _num(row.rows), _num(row.inserted),
            _num(row.replaced), _num(row.conflicts), _images_cell(row.images), row.result))
    if outcome.merge_ran:
        missing_total = sum(result.images_missing for result in outcome.imports)
        out.append(f"- 图片：先拉图再插行，仍缺的如实记（本次 {missing_total} 张）")
    return out + [""]


def _gaps_section(outcome: RunOutcome) -> list[str]:
    out = ["## 四、缺口与还没来的"]
    wrote = False
    if not outcome.plan_known:
        out.append(f"- 本机没有本周（{outcome.week}）的落库计划：缺口与「还没来的包」"
                   "这次没法对照（先让采集这边的准备串落一次库，再来汇总）")
        wrote = True
    else:
        mine = [gap for gap in outcome.gaps if gap.machine == outcome.machine_id]
        if mine:
            first, *rest = mine
            out.append(f"- 本机缺{_day_count(len(mine))}：{first.shop_key} {first.date[5:]}"
                       f" —— {first.reason}（历史日期补不了，如实记一笔）")
            for gap in rest:
                out.append(f"- 本机还缺：{gap.shop_key} {gap.date[5:]} —— {gap.reason}"
                           "（历史日期补不了，如实记一笔）")
            wrote = True
        others = [gap for gap in outcome.gaps if gap.machine != outcome.machine_id]
        for machine, group in _by_machine(tuple(others)):
            out.append(f"- raw-{machine} 缺{_day_count(len(group))}："
                       f"{_gap_detail(group, reasons=False)} —— 计划里该采到，"
                       "收到的包里没有这一天（历史日期补不了，如实记一笔）")
            wrote = True
        if outcome.missing_machines:
            who = "、".join(f"raw-{machine}" for machine in outcome.missing_machines)
            out.append(f"- 还没收到的包：{who} 的 {outcome.week.split('-')[1]} "
                       "还没发布或没拉到")
            wrote = True

        partial_mine = [gap for gap in outcome.partial_gaps
                        if gap.machine == outcome.machine_id]
        if partial_mine:
            out.append(f"- 部分日期缺口（提示，不计异常）：本机 "
                       f"{_gap_detail(partial_mine, reasons=True)}")
            wrote = True
        partial_others = [gap for gap in outcome.partial_gaps
                          if gap.machine != outcome.machine_id]
        for machine, group in _by_machine(tuple(partial_others)):
            out.append(f"- 部分日期缺口（提示，不计异常）：raw-{machine} "
                       f"{_gap_detail(group, reasons=True)}")
            wrote = True
        if not wrote:
            out.append("- 没有缺口")
    if outcome.gaps:
        out.append("- 口径：缺口 = 计划里该采到的店铺，在已评估日期范围内每天都找不到库存观测")
    if outcome.partial_gaps:
        out.append("- 口径：部分日期缺口 = 同一店铺至少有一天有库存观测，只缺其余日期；保留提示，不计异常")
    return out + [""]

def _conflicts_section(outcome: RunOutcome) -> list[str]:
    if not outcome.conflicts:
        return []                           # 干净场景不出现这一节（样例的形态）
    out = [f"## {CONFLICTS_SECTION}、冲突"]
    for entry in outcome.conflicts:
        loser = _side_text(outcome, entry, entry.loser_machine, entry.loser_at)
        winner = _side_text(outcome, entry, entry.winner_machine, entry.winner_at)
        pick = (f"取后到者（{entry.winner_machine}）" if entry.winner_at > entry.loser_at
                else f"同时刻，取机器标识较大的（{entry.winner_machine}）")
        out.append(f"- 重复采集：{entry.shop_key} {entry.date[5:]}：{loser}与{winner} "
                   f"都采到；{pick}，覆盖 {entry.replaced} 行、保留 {entry.kept} 行")
    packages = sorted({name for entry in outcome.conflicts for name in entry.packages})
    out.append("- 口径：按（日期, 店铺）整组取胜，胜负由包内采集时刻定（同时刻比机器标识）"
               "——所以哪台机器先导入，算出来的库都一样")
    out.append(f"- 明细记在本机导入账（本机视角：导入 {'、'.join(packages) or '包'} 时发现）；"
               "冲突不入交换区")
    return out + [""]


def _todo_section(outcome: RunOutcome) -> list[str]:
    # 节号跟着「冲突」节的在不在走：干净场景没有第五节，待办就是「五」（样例一/二的样子）
    number = "六" if outcome.conflicts else "五"
    out = [f"## {number}、下次该做什么"]
    out += [f"{index}. {item}" for index, item in enumerate(outcome.todo, start=1)]
    return out
