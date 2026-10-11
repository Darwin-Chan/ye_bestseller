"""判断来源清单（票 01、ADR-0052）：维护机经 Gitee API 生成、发布到 `plan` 库的来源快照。

**这份清单解决什么**：收谁的判断集，一直是**本地口径**——`judgment_set.collect()` 只枚举交换区
里实际存在的 `judged-*` 目录。远端多了一本来源、本地没建克隆时，接收机就无从知道「该有谁」。
来源清单把「预期来源与已批准远端地址」变成一份共享事实，接收机据此补齐本地克隆（`prepare_sources`，
接收侧在票 02：缺的建克隆、已有的只快进更新并核对远端地址；坏现场按来源阻断；plan 拉不动时只用
上一版快照处理已有且匹配的来源；没有任何可用来源报「接收通道未就绪」）。

**谁维护**：m4（汇总机）是唯一的生成者，也是唯一持有该用途 Gitee API token 的机器。token 只从
环境变量 `GITEE_TOKEN` 读，**不进仓库、不进快照、不进日志**；没有 token 的机器（m1/m2/m3）读到
空就是「本机不是来源维护机」，只读取清单、不扫 Gitee。

**快照**：落在 `plan` 库根下的 `judged-sources.json`（与 `shops.csv`、`machines.json` 并列）。
含格式版本、可信 owner、生成机器、生成时间与逐条来源（机器编号、库名、远端地址）；**不含**任何
凭据、密钥、Cookie 或包内容，也不自引用发布提交的 SHA（生成提交没法写进它自己）——发布信息由
消费方读 plan 那一笔提交得到（ADR-0052 决策 2）。

**包含范围**：可信 owner 下、**私有**且名称严格匹配 `judged-<machine_id>`、机器编号符合正式规则
（`m<正整数>`，见 `MACHINE_ID_RE`）的库。`judged-test`、`judged-old`、`judged-demo` 这类不合规名
不自动纳入。**库存在即入列**——远端还是空库也代表该来源应接入（空库不是失败）。

**保护**：相对上一版有效快照，来源**减少**或某来源**远端地址变化**都先报告、保留上一版、等人确认，
不自动覆盖（交换台以 `--confirm-source-shrink` 显式确认后才按新的发）。**无变化不产生提交**（同内容
空操作，与周包/判断包同规）。查询失败、翻页翻不到底、上一版快照读不动都**不覆盖**上一版——「查不全」
绝不能表现成「来源变少了」。

**快照语义**：这是快照不是强约束——某来源从清单消失**不**触发接收机删除已有本地克隆（删克隆另属人工）。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import urllib.error
import urllib.request
from typing import Callable, Protocol

from bestseller_monitor.db import CST
from bestseller_monitor.export import pull_fresh, restore_remote
from bestseller_monitor.git_channel import ChannelError, GitChannel
# 来源库的目录名前缀与判断集同源一处（`judged-<机器>`）：不再各写一份「judged-」。
from bestseller_monitor.judgment_set import PACKAGE_REL, REPO_PREFIX, judged_repos
from bestseller_monitor.plan_step import PLAN_REPO_DIR

# 快照在 plan 库里的相对路径（库根下，与 shops.csv / machines.json 并列）。
SNAPSHOT_REL = "judged-sources.json"
# 快照格式的口径版本：字段、含范围或筛选口径变化时进一位（与周包/判断包同规）。
FORMAT_VERSION = "v1"
# Gitee API token 的环境变量名（唯一读口；不进仓库、不进快照、不进日志）。
TOKEN_ENV = "GITEE_TOKEN"
GITEE_API_BASE = "https://gitee.com/api/v5"
# 一页多少条（Gitee v5 上限 100）与最多翻几页（翻这么多还满页 = 查不到底，算不完整）。
PER_PAGE = 100
MAX_PAGES = 50
# 远端地址的推导形态：与上机清单里的 clone 地址同规（SSH、owner/库名）。
REMOTE_TEMPLATE = "git@gitee.com:{owner}/{repo}.git"
# 正式机器编号规则：m 后接正整数（三台采集机 m1/m2/m3、汇总机自取，如 m4）。
# 前导零（m01）、m0、非数字（judged-test / judged-old / judged-demo）都不合规则。
MACHINE_ID_RE = re.compile(r"m[1-9][0-9]*\Z")


class SourceQueryError(RuntimeError):
    """来源查询失败（API 不通、token 账号核对不过、某一页没成、翻页翻不到底、返回不是仓库列表）。

    调用方据此「不覆盖上一版快照」。消息里只带页号、账号名与错误种类，**绝不含 token**。
    """


class RepoProvider(Protocol):
    """来源查询的接缝：一次拿到该 owner 下的**全部**仓库。

    真实实现是 `GiteeApi`（翻页）；测试用替身给固定清单或抛 `SourceQueryError`——
    过滤、缩减保护、无变化这些判定因此都能在无网络、无凭据下验。
    """

    def list_repos(self) -> tuple["GiteeRepo", ...]: ...


@dataclasses.dataclass(frozen=True)
class GiteeRepo:
    """Gitee 上一只仓库里我们关心的字段。

    `remote` 是该库的已批准远端地址（Gitee API 的 `ssh_url`）；空则按 owner + 库名推导。
    """

    name: str
    owner: str
    private: bool
    remote: str = ""


@dataclasses.dataclass(frozen=True)
class SourceEntry:
    """快照里的一条来源：机器编号、库名、已批准远端地址。"""

    machine_id: str
    repo: str
    remote: str


@dataclasses.dataclass(frozen=True)
class RefreshResult:
    """跑一次刷新做了什么（交换台报告与进度行从这里读）。

    `published` = 清单在远端上（含「确认了一遍、同内容」那一趟，与判断集 `PublishResult` 同规）。
    `unchanged` = 与已发布那份同来源同地址：没写文件、没提交、没推送。
    `held` = 来源减少或远端地址变化：保留上一版、等人确认（`note`/`warnings` 说清）。
    `note` = 「本机不是来源维护机」这类可读说明——不是失败，别家照跑。
    """

    owner: str
    generator: str
    sources: tuple[SourceEntry, ...] = ()
    published: bool = False
    unchanged: bool = False
    held: bool = False
    commit: str | None = None
    previous: tuple[SourceEntry, ...] = ()
    warnings: tuple[str, ...] = ()
    note: str | None = None
    failure: str | None = None

    @property
    def failed(self) -> bool:
        return self.failure is not None


@dataclasses.dataclass(frozen=True)
class _PreviousSnapshot:
    """上一版快照的读回：`state` 取 `absent`（还没发布过）/ `ok`（读到了）/ `broken`（在但读不动）。

    `format_version` 照抄文件里的原值——生成侧只看来源，消费侧（票 02）拿它挡不认识的版本。
    """

    state: str
    owner: str = ""
    format_version: str = ""
    sources: tuple[SourceEntry, ...] = ()


def _remote(owner: str, repo: str) -> str:
    return REMOTE_TEMPLATE.format(owner=owner, repo=repo)


def provider_from_config(owner: str) -> "GiteeApi | None":
    """按环境变量 `GITEE_TOKEN` 现搭一个真实 Gitee 客户端；没配 token 回 None。

    owner 由配置（`machine.gitee_owner`）给；token 只从环境读（不进仓库，见模块 docstring）。
    """
    token = os.environ.get(TOKEN_ENV, "").strip()
    return GiteeApi(owner, token) if token else None


def refresh(exchange_root: str | pathlib.Path, *, owner: str, generator: str,
            provider: "RepoProvider | None", now: dt.datetime | None = None,
            confirm_shrink: bool = False) -> RefreshResult:
    """刷新并发布来源清单（ADR-0052）：查询 → 过滤 → 保护判据 → 无变化/发布。

    `provider` 为空表示「配了 owner 但没有 token」——如实报失败，不静默装成没来源。
    `confirm_shrink=True` 才把减少/改址后的新清单发出去（默认保留上一版、等人确认；交换台从
    `--confirm-source-shrink` 传进来）。`now` 是快照上记的生成时刻（北京时间），缺省取现在。
    """
    owner = str(owner or "").strip()
    if not owner:
        return RefreshResult(owner="", generator=generator, note=(
            "本机没配 Gitee owner（machine.gitee_owner）：来源清单由维护机（m4）生成并发布，"
            "接收机只读取——刷新来源清单这趟没做。"))
    repo = pathlib.Path(exchange_root) / PLAN_REPO_DIR
    if not (repo / ".git").exists():
        return RefreshResult(owner=owner, generator=generator, failure=(
            f"计划库还没有 clone 到 {repo}：来源清单发在 plan 库的 {SNAPSHOT_REL}，"
            "先按上机清单把 plan 库 clone 下来（第 9 / 11 步），再跑交换台。"))
    if provider is None:
        return RefreshResult(owner=owner, generator=generator, failure=(
            f"本机配了 gitee_owner = {owner}，但读不到 Gitee API token（环境变量 {TOKEN_ENV}）："
            "维护机刷新来源清单要配好这个 token（只读、不进仓库、不进日志）。"))

    channel = GitChannel(repo)
    try:
        pull_fresh(channel)
    except ChannelError as exc:
        return RefreshResult(owner=owner, generator=generator, failure=(
            f"拉不到 plan 库：{exc}\n来源清单这次没刷新，上一版有效快照照旧有效。"))

    previous = _read_snapshot(channel)
    if previous.state == "broken":
        return RefreshResult(owner=owner, generator=generator, failure=(
            f"plan 库里的上一版来源清单读不出来（{SNAPSHOT_REL}）：不覆盖、不猜——先看一眼，"
            "确认无碍后把它从 plan 库删掉（一次提交）再重跑。"))
    try:
        discovered = provider.list_repos()
    except SourceQueryError as exc:
        return RefreshResult(owner=owner, generator=generator, previous=previous.sources, failure=(
            f"来源查询没成（{exc}）：查不全绝不覆盖上一版有效快照，改好之后重跑。"))

    sources = _select(discovered, owner=owner)
    by_machine = {entry.machine_id: entry for entry in previous.sources}
    removed = [machine for machine in by_machine
               if machine not in {source.machine_id for source in sources}]
    changed = sorted(source.machine_id for source in sources
                     if by_machine.get(source.machine_id)
                     and by_machine[source.machine_id].remote != source.remote)
    if (removed or changed) and not confirm_shrink:
        return RefreshResult(owner=owner, generator=generator, sources=sources, held=True,
                             previous=previous.sources,
                             warnings=(_hold_warning(removed, changed),))

    if previous.state == "ok" and previous.owner == owner and previous.sources == sources:
        return RefreshResult(owner=owner, generator=generator, sources=sources, published=True,
                             unchanged=True, previous=previous.sources, commit=channel.head())
    return _publish(channel, owner=owner, generator=generator, sources=sources,
                    previous=previous.sources, now=now)


def _publish(channel: GitChannel, *, owner: str, generator: str,
             sources: tuple[SourceEntry, ...], previous: tuple[SourceEntry, ...],
             now: dt.datetime | None) -> RefreshResult:
    """把新快照写进 plan 克隆、一次提交推上去；推送失败把克隆退回远端状态。"""
    generated_at = now or dt.datetime.now(CST)
    text = snapshot_text(owner=owner, generator=generator, generated_at=generated_at,
                         sources=sources)
    target = channel.path / SNAPSHOT_REL
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        channel.commit(f"publish judged sources ({len(sources)})", [target])
        channel.push()
    except (ChannelError, OSError) as exc:
        note = restore_remote(channel)
        tail = f"\n（克隆没能退回远端状态：{note}）" if note else ""
        return RefreshResult(owner=owner, generator=generator, sources=sources, previous=previous,
                             failure=(f"来源清单没发出去：{exc}\n上一版有效快照照旧有效，"
                                      f"通道修好后重跑即可。{tail}"))
    return RefreshResult(owner=owner, generator=generator, sources=sources, published=True,
                         previous=previous, commit=channel.head())


def snapshot_text(*, owner: str, generator: str, generated_at: dt.datetime,
                  sources: tuple[SourceEntry, ...]) -> str:
    """快照的正文：键排序、来源按机器编号排序——同内容序列化出来逐字节一致。"""
    document = {
        "format_version": FORMAT_VERSION,
        "owner": owner,
        "generated_by": generator,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "sources": [{"machine_id": source.machine_id, "repo": source.repo,
                     "remote": source.remote} for source in sources],
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _select(repos, *, owner: str) -> tuple[SourceEntry, ...]:
    """过滤 + 规范化：只留可信 owner 下、私有、名称合规的 `judged-<机器>` 库。

    同一机器编号出现两次（异常，取先到的那只）。远端地址取 provider 给的 `remote`，
    没有就按 owner + 库名推导——快照里因此永远是一条可 clone 的批准地址。
    """
    found: list[SourceEntry] = []
    seen: set[str] = set()
    for repo in repos:
        if not repo.private or repo.owner != owner:
            continue
        if not repo.name.startswith(REPO_PREFIX):
            continue
        machine_id = repo.name[len(REPO_PREFIX):]
        if machine_id in seen or not MACHINE_ID_RE.match(machine_id):
            continue
        seen.add(machine_id)
        found.append(SourceEntry(machine_id=machine_id, repo=repo.name,
                                 remote=repo.remote or _remote(owner, repo.name)))
    return tuple(sorted(found, key=lambda entry: entry.machine_id))


def _hold_warning(removed: list[str], changed: list[str]) -> str:
    parts = []
    if removed:
        parts.append("来源减少：" + "、".join(f"{REPO_PREFIX}{m}" for m in sorted(removed)))
    if changed:
        parts.append("远端地址变化：" + "、".join(f"{REPO_PREFIX}{m}" for m in sorted(changed)))
    return ("；".join(parts) + "。已保留上一版有效快照、没有覆盖——先核对 token 权限与库是否真被删/"
            "改名，确认无误后重跑交换台并加 `--confirm-source-shrink` 按新的发布。")


def _read_snapshot(channel: GitChannel) -> _PreviousSnapshot:
    """读回上一版有效快照（HEAD 里那份）。

    **缺席**（还没发布过）与**读不动**（文件坏了、不是我们写的）分开：坏了绝不拿候选覆盖——
    把坏文件当「没有上一版」，就会静默关掉「来源减少」这条保护（减一个来源也照发）。
    """
    raw = channel.read_path(SNAPSHOT_REL)
    if raw is None:
        return _PreviousSnapshot(state="absent")
    try:
        document = json.loads(raw.decode("utf-8"))
        owner = str(document.get("owner") or "")
        format_version = str(document.get("format_version") or "")
        sources = tuple(SourceEntry(machine_id=str(entry["machine_id"]),
                                    repo=str(entry["repo"]), remote=str(entry["remote"]))
                        for entry in document.get("sources") or ())
    except (ValueError, UnicodeDecodeError, KeyError, TypeError, AttributeError):
        return _PreviousSnapshot(state="broken")
    return _PreviousSnapshot(state="ok", owner=owner, format_version=format_version,
                             sources=sources)


# ---------- Gitee API 客户端（唯一的网络口） ----------

# 一次 GET 的形态：URL 与请求头进、解析后的 JSON 出；出错就抛（由客户端折成 SourceQueryError）。
Fetch = Callable[[str, dict], object]


def _http_json(url: str, headers: dict) -> object:
    """默认传输：urllib 发一次 GET 读回 JSON。token 放请求头，不进 URL（不会漏进日志）。"""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SourceQueryError(f"{type(exc).__name__}: {exc}") from None


class GiteeApi:
    """Gitee API v5 的仓库列表口：授权用户（token 本人）的可见仓库逐页翻到底。

    接口形状按 Gitee v5 文档与 m4 真机探针（2026-10-11）：`GET /user/repos?visibility=all&per_page=&page=`
    ——公开库列表接口 `GET /users/{owner}/repos` 对本人的有效 token 也看不到私有库（票 05 现场
    教训：空列表必须能区分「真没有」与「看错了地方」）。认证用 `Authorization: token <token>` 头；
    列仓库前先经 `GET /user` 核对 token 账号与可信 owner 一致，不一致按查询失败处理。

    现场证据（主仓）：`.scratch/logs/tickets/issue-06-m1-judgment-receive/05/2026-10-10/m4-证据/`
    （`诊断-来源清单发布空-20261011.md`、`探针输出-20261011-221632.txt`）；修好后重跑 m4 向导、
    清单落 4 个来源是票 05 后继的人工项。
    """

    def __init__(self, owner: str, token: str, *, base: str = GITEE_API_BASE,
                 per_page: int = PER_PAGE, fetch: Fetch | None = None):
        self.owner = owner.strip()
        self._token = token
        self._base = base.rstrip("/")
        self._per_page = per_page
        self._fetch = fetch or _http_json

    def list_repos(self) -> tuple[GiteeRepo, ...]:
        """先核对 token 账号，再翻页取全部仓库；短页或空页即到底，翻到 `MAX_PAGES` 还满页算查不全。"""
        self._verify_account()
        out: list[GiteeRepo] = []
        page = 1
        while True:
            batch = self._page(page)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < self._per_page:
                break
            page += 1
            if page > MAX_PAGES:
                raise SourceQueryError(
                    f"分页不完整：翻过 {MAX_PAGES} 页还是满页，翻不到底——不覆盖上一版快照")
        return tuple(out)

    def _verify_account(self) -> None:
        """`GET /user`：token 是谁的账号。与可信 owner 不一致 = 看错了地方，按查询失败处理。

        token 换过账号而 owner 配置没跟上时，`/user/repos` 照样 200——只会安静地列出**别人**的
        仓库或空表；这里先拦住，让「空列表」永远代表「真没有」。比对按逐字（含大小写）：下游
        `_select` 的 owner 过滤与上一版 owner 比对都是逐字口径，这里放宽会在两层之间留下
        「核对过、却全被过滤」的缝——那正是静默发空清单。
        """
        payload = self._get_json(f"{self._base}/user", operation="token 账号核对")
        login = str(payload.get("login") or "") if isinstance(payload, dict) else ""
        if not login:
            raise SourceQueryError(
                f"token 账号核对没成：/user 返回的不是带 login 的对象（{type(payload).__name__}）")
        if login != self.owner:
            raise SourceQueryError(
                f"token 账号是 {login}，与配置的可信 owner {self.owner} 不一致——查错了账号；"
                "核对 machine.gitee_owner 与 GITEE_TOKEN（token 是不是换过账号？注意大小写）")

    def _get_json(self, url: str, *, operation: str) -> object:
        try:
            return self._fetch(url, {"Authorization": f"token {self._token}",
                                     "Accept": "application/json"})
        except SourceQueryError:
            raise
        except Exception as exc:                       # 网络/解析都折成同一档
            raise SourceQueryError(f"{operation}没成（{type(exc).__name__}）：{exc}") from None

    def _page(self, number: int) -> list[GiteeRepo]:
        url = (f"{self._base}/user/repos"
               f"?visibility=all&per_page={self._per_page}&page={number}")
        payload = self._get_json(url, operation=f"第 {number} 页查询")
        if not isinstance(payload, list):
            raise SourceQueryError(f"第 {number} 页返回的不是仓库列表（{type(payload).__name__}）")
        return [_repo_of(item) for item in payload]


def _repo_of(item: object) -> GiteeRepo:
    """一条 API 记录折成 `GiteeRepo`：名字、owner（namespace.path）、私有标志、ssh_url。"""
    if not isinstance(item, dict):
        raise SourceQueryError(f"仓库条目不是对象（{type(item).__name__}）")
    name = str(item.get("name") or "")
    namespace = item.get("namespace")
    owner = str(namespace.get("path") or "") if isinstance(namespace, dict) else ""
    if not owner:
        full_name = str(item.get("full_name") or "")
        owner = full_name.split("/", 1)[0] if "/" in full_name else ""
    return GiteeRepo(name=name, owner=owner, private=bool(item.get("private")),
                     remote=str(item.get("ssh_url") or ""))


# ---------- 接收侧（票 02）：按来源清单自动接入本地克隆 ----------

# 每条来源的接入终态（票 02；spec §3 的收款状态词表在报告那一票里扩全，这里先落三档）：
# 已接入（有判断集包）/ 已接入但还没有包 / 被阻断（不动现场，点名原因）/ 这次没接入（跳过，见 reason）。
SOURCE_CONNECTED = "connected"
SOURCE_CONNECTED_NO_PACKAGE = "connected_no_package"
SOURCE_BLOCKED = "blocked"
SOURCE_SKIPPED = "skipped"
# 这次对本地克隆实际做的动作（`PreparedSource.action`；阻断/跳过时为空）。
ACTION_CLONED = "cloned"        # 新建
ACTION_UPDATED = "updated"      # 快进到新内容
ACTION_UNCHANGED = "unchanged"  # 已是最新


@dataclasses.dataclass(frozen=True)
class PreparedSource:
    """一条来源的接入结果：本地目录在哪、结局如何、为什么。

    `action` 只说这次对本地克隆做了什么（`ACTION_CLONED` / `ACTION_UPDATED` /
    `ACTION_UNCHANGED`）；被阻断或跳过时为空。`failure` 是阻断的具体原因（原样引用
    git 的输出）；`reason` 是「这次没接入」的说明——两者都不是整趟的失败（整趟的结论
    在 `PreparedSources` 上）。
    """

    machine_id: str
    repo: str
    remote: str
    path: pathlib.Path
    state: str
    action: str = ""
    failure: str | None = None
    reason: str | None = None

    @property
    def usable(self) -> bool:
        """这条来源这次能不能交给收取：已接入的才算（被阻断/跳过的不读它的包）。"""
        return self.state in (SOURCE_CONNECTED, SOURCE_CONNECTED_NO_PACKAGE)

    @property
    def blocked(self) -> bool:
        return self.state == SOURCE_BLOCKED

    @property
    def skipped(self) -> bool:
        return self.state == SOURCE_SKIPPED


@dataclasses.dataclass(frozen=True)
class PreparedSources:
    """跑一次来源接入做了什么（收取任务与周报从这里读）。

    `snapshot_state`：`ok`（读到了清单）/ `absent`（plan 里还没有这份文件）/ `broken`
    （在但读不动）/ `no_plan_clone`（计划库还没 clone）。`stale` = 计划库这次拉不动、
    用的本机上一版快照（只处理已有且匹配的来源，不新建克隆）。`not_ready` 非空 =
    没有任何可用来源（「接收通道未就绪」的原因）——不能表现成「没有新判断集」。
    `local_only` 是交换区里清单外已有的 judged 库（快照语义：不删克隆，照收）。
    `snapshot_version` 是读清单时计划库 HEAD 的短哈希（票 03：落进导入账的「清单版本」；
    计划库没有克隆或还没有提交时为空）。
    """

    machine_id: str
    root: pathlib.Path
    entries: tuple[PreparedSource, ...] = ()
    snapshot_state: str = "no_plan_clone"
    snapshot_version: str = ""
    stale: bool = False
    warnings: tuple[str, ...] = ()
    not_ready: str | None = None
    local_only: tuple[tuple[str, pathlib.Path], ...] = ()

    @property
    def ready(self) -> bool:
        return self.not_ready is None

    def collect_sources(self) -> tuple[tuple[str, pathlib.Path], ...]:
        """这次该交给自己收取的来源名单：接入成功的 + 清单外本地已有的（排序）。

        被阻断的来源不在名单里——阻断就是连读它的包都不读；清单外已有的照旧收
        （快照语义：来源从清单消失不触发删克隆，本地克隆按老口径继续收）。
        """
        connected = [(entry.machine_id, entry.path) for entry in self.entries if entry.usable]
        return tuple(sorted(connected + list(self.local_only)))


def prepare_sources(exchange_root: str | pathlib.Path, *, machine_id: str) -> PreparedSources:
    """收取任务启动时的来源接入：读最近一次有效来源清单，把本地克隆补齐。

    只由收取那半调（`exchange.run_once` 走 `judgment_set.collect` 之前）——打开交换窗口
    与启动分析都不触发。逐条来源独立处理，一条坏不拦别条；目标目录只由**经校验的来源
    身份**推出（`<exchange_root>/judged-<机器>`），清单不能注入本地路径。
    """
    root = pathlib.Path(exchange_root)
    plan = root / PLAN_REPO_DIR
    warnings: list[str] = []
    stale = False
    entries: list[PreparedSource] = []
    snapshot_state = "no_plan_clone"
    snapshot_version = ""
    trouble: str | None = None      # 清单不可用时的那句「为什么」（告警与未就绪共用一处）
    if not (plan / ".git").exists():
        trouble = f"计划库还没 clone 到 {plan}（上机清单第 9 / 11 步），读不到来源清单"
        warnings.append(f"{trouble}——来源没法自动接入。")
    else:
        channel = GitChannel(plan)
        try:
            channel.pull()
        except ChannelError as exc:
            stale = True
            warnings.append(f"计划库这次拉不动（{exc}）：\n"
                            "改用本机上一版来源清单（可能不是最新）——只处理已有且匹配的来源，"
                            "不新建克隆；通道恢复后重跑一次。")
        # 票 03：读清单这一刻的计划库提交 = 这次收取看到的「清单版本」（stale 时就是本机那版）。
        snapshot_version = channel.head_or_none() or ""
        previous = _read_snapshot(channel)
        if previous.state == "absent":
            snapshot_state = "absent"
            trouble = f"来源清单还没发布过（plan 里没有 {SNAPSHOT_REL}）"
            warnings.append(f"{trouble}——先让维护机（m4）跑一趟交换台刷新并发布。")
        elif previous.state == "broken":
            snapshot_state = "broken"
            trouble = f"来源清单读不出来（plan 里的 {SNAPSHOT_REL} 坏了）"
            warnings.append(f"{trouble}——不覆盖、不猜，先看一眼、修好再跑。")
        elif previous.format_version != FORMAT_VERSION:
            snapshot_state = "unknown_format"
            trouble = (f"来源清单的格式版本本机不认识（读到 {previous.format_version!r}，"
                       f"本机只懂 {FORMAT_VERSION}）")
            warnings.append(f"{trouble}——不按它动本地目录，升级本机程序再跑。")
        else:
            snapshot_state = "ok"
            entries = _connect_sources(root, machine_id=machine_id,
                                       sources=previous.sources, stale=stale)
    declared = {entry.machine_id for entry in entries}
    local_only = tuple((machine, path) for machine, path in
                       judged_repos(root, exclude=machine_id) if machine not in declared)
    not_ready = None
    if not any(entry.usable for entry in entries) and not local_only:
        not_ready = _not_ready_text(trouble, entries=entries)
    return PreparedSources(machine_id=machine_id, root=root, entries=tuple(entries),
                           snapshot_state=snapshot_state, snapshot_version=snapshot_version,
                           stale=stale, warnings=tuple(warnings), not_ready=not_ready,
                           local_only=local_only)


def _connect_sources(root: pathlib.Path, *, machine_id: str,
                     sources: tuple[SourceEntry, ...], stale: bool) -> list[PreparedSource]:
    """逐条接入：本机那本跳过；缺目录就建克隆；已有目录核对现场、只快进更新。

    每条来源独立收尾（结果无论如何都有一条）；被阻断的现场一律不动它——不删、不重建、
    不改 origin、不清理、不 merge、不重置，只点名具体原因。
    """
    out: list[PreparedSource] = []
    for entry in sources:
        if entry.machine_id == machine_id:
            continue                            # 本机那本自己发布、自己不发自己收
        repo = f"{REPO_PREFIX}{entry.machine_id}"
        dest = root / repo

        def resolved(state: str, **kwargs) -> None:
            out.append(PreparedSource(entry.machine_id, repo, entry.remote, dest, state, **kwargs))

        if not MACHINE_ID_RE.match(entry.machine_id) or entry.repo != repo or not entry.remote:
            resolved(SOURCE_BLOCKED, failure=(
                f"清单条目非法（机器编号/库名/远端地址不合规）："
                f"machine_id={entry.machine_id!r}、repo={entry.repo!r}——不按它动本地目录，"
                "先核对维护机发布的清单。"))
            continue
        cloned = False
        if not dest.exists():
            if stale:
                resolved(SOURCE_SKIPPED, reason=(
                    "本机还没有这本的克隆，而计划库这次拉不动（用的是上一版快照）："
                    "不从过期快照新建克隆——等计划库恢复后重跑。"))
                continue
            try:
                GitChannel.clone(entry.remote, dest)
            except ChannelError as exc:
                resolved(SOURCE_BLOCKED, failure=(
                    f"克隆没成功：{exc}\n核对批准地址与凭据/网络后重跑（这本这次没接上）。"))
                continue
            cloned = True
        channel = GitChannel(dest)
        if not (dest / ".git").exists():
            resolved(SOURCE_BLOCKED, failure=(
                f"{dest} 已存在但不是 git 克隆：不删、不重建——先人工看一眼它是什么，"
                "收拾好之后重跑。"))
            continue
        try:
            origin = channel.remote_url()
        except ChannelError as exc:
            resolved(SOURCE_BLOCKED, failure=(
                f"仓库读不动（{exc}）：不自动修——人工看一眼这本（坏了的仓库不重建）。"))
            continue
        if origin is None:
            resolved(SOURCE_BLOCKED, failure=(
                f"本地这本 {repo} 没有 origin 这个远端：不替它补（补一个就等于替它选来源）"
                "——人工核对这本是哪来的，收拾好之后重跑。"))
            continue
        if origin != entry.remote:
            resolved(SOURCE_BLOCKED, failure=(
                f"远端不匹配：本地 origin 是 {origin}，清单批准的是 {entry.remote}——"
                "不改 origin、不动它；核对这本是哪来的，收拾好之后重跑。"))
            continue
        if cloned:
            action = ACTION_CLONED
        else:
            try:
                changes = channel.local_changes()
            except ChannelError as exc:
                resolved(SOURCE_BLOCKED, failure=(
                    f"仓库读不动（{exc}）：不自动修——人工看一眼这本。"))
                continue
            if changes:
                shown = "、".join(changes[:5]) + ("…" if len(changes) > 5 else "")
                resolved(SOURCE_BLOCKED, failure=(
                    f"工作区有未提交的改动（{shown}）：不清理、不重置——这本是只读来源，"
                    "本机不该在它里面改文件；人工看一眼再重跑。"))
                continue
            before = channel.head_or_none()
            try:
                channel.pull_ff_only()
            except ChannelError as exc:
                resolved(SOURCE_BLOCKED, failure=(
                    f"更新没成功（只许快进，不 merge、不重置）：{exc}\n"
                    "本地有分叉的提交就说明有人动过这本，人工核对后重跑。"))
                continue
            if not channel.head_matches_upstream():
                resolved(SOURCE_BLOCKED, failure=(
                    "本地这本有上游没有的提交（只许快进：pull 说「已是最新」也可能藏着"
                    "这种提交）——读它的包等于读一份未经来源发布的判断集；不 merge、不重置，"
                    "人工核对这本是哪来的、收拾好之后重跑。"))
                continue
            action = ACTION_UPDATED if channel.head_or_none() != before else ACTION_UNCHANGED
        if channel.read_path(PACKAGE_REL) is None:
            resolved(SOURCE_CONNECTED_NO_PACKAGE, action=action)
        else:
            resolved(SOURCE_CONNECTED, action=action)
    return out


def _not_ready_text(trouble: str | None, *, entries: list[PreparedSource]) -> str:
    """「接收通道未就绪」的原因与下一步：为什么一条可用的来源都没有。"""
    parts: list[str] = []
    if trouble is not None:
        parts.append(trouble)
    else:
        blocked = [entry.repo for entry in entries if entry.blocked]
        skipped = [entry.repo for entry in entries if entry.skipped]
        if blocked:
            parts.append("清单里的来源都没接上（" + "、".join(blocked) + "）")
        if skipped:
            parts.append("计划库这次拉不动、用的是上一版快照，本机又还没有这些来源的克隆"
                         "（" + "、".join(skipped) + "）：不新建克隆")
        if not blocked and not skipped:
            parts.append("来源清单里没有别的来源可接（清单空着，或只有本机自己那本）")
    parts.append("本机也没有别的 judged-* 库可收")
    return "；".join(parts)
