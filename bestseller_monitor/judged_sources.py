"""判断来源清单（票 01、ADR-0052）：维护机经 Gitee API 生成、发布到 `plan` 库的来源快照。

**这份清单解决什么**：收谁的判断集，一直是**本地口径**——`judgment_set.collect()` 只枚举交换区
里实际存在的 `judged-*` 目录。远端多了一本来源、本地没建克隆时，接收机就无从知道「该有谁」。
来源清单把「预期来源与已批准远端地址」变成一份共享事实，接收机据此补齐本地克隆（接收侧在票 02）。

**谁维护**：m4（汇总机）是唯一的生成者，也是唯一持有该用途 Gitee API token 的机器。token 只从
环境变量 `GITEE_TOKEN` 读，**不进仓库、不进快照、不进日志**；没有 token 的机器（m1/m2/m3）读到
空就是「本机不是来源维护机」，只读取清单、不扫 Gitee。

**快照**：落在 `plan` 库根下的 `judged-sources.json`（与 `shops.csv`、`machines.json` 并列）。
含格式版本、可信 owner、生成机器、生成时间与逐条来源（机器编号、库名、远端地址）；**不含**任何
凭据、密钥、Cookie 或包内容，也不自引用发布提交的 SHA（生成提交没法写进它自己）。

**包含范围**：可信 owner 下、**私有**且名称严格匹配 `judged-<machine_id>`、机器编号符合正式规则
（`m<正整数>`，见 `MACHINE_ID_RE`）的库。`judged-test`、`judged-old`、`judged-demo` 这类不合规名
不自动纳入。**库存在即入列**——远端还是空库也代表该来源应接入（空库不是失败）。

**保护**：相对上一版有效快照，来源**减少**或某来源**远端地址变化**都先报告、保留上一版、等人确认，
不自动覆盖（`confirm_shrink=True` 才按新的发）。**无变化不产生提交**（同内容空操作，与周包/判断包
同规）。查询失败、翻页翻不到底也**不覆盖**上一版——「查不全」绝不能表现成「来源变少了」。

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
import urllib.parse
import urllib.request
from typing import Callable, Protocol

from bestseller_monitor.db import CST
from bestseller_monitor.export import pull_fresh, restore_remote
from bestseller_monitor.git_channel import ChannelError, GitChannel
from bestseller_monitor.plan_step import PLAN_REPO_DIR

# 快照在 plan 库里的相对路径（库根下，与 shops.csv / machines.json 并列）。
SNAPSHOT_REL = "judged-sources.json"
# 快照格式的口径版本：字段、含范围或筛选口径变化时进一位（与周包/判断包同规）。
FORMAT_VERSION = "v1"
# 来源库的目录名前缀：judged-<机器>（与 raw-<机器>、plan 并列）。
REPO_PREFIX = "judged-"
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
    """来源查询失败（API 不通、某一页没成、翻页翻不到底、返回不是仓库列表）。

    调用方据此「不覆盖上一版快照」。消息里只带页号与错误种类，**绝不含 token**。
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
    `confirm_shrink=True` 才把减少/改址后的新清单发出去（默认保留上一版、等人确认）。
    `now` 是快照上记的生成时刻（北京时间），缺省取现在。
    """
    owner = str(owner or "").strip()
    if not owner:
        return RefreshResult(owner="", generator=generator, note=(
            "本机没配 Gitee owner（machine.gitee_owner）：来源清单由维护机（m4）生成并发布，"
            "接收机只读取——刷新来源清单这趟没做。"))
    repo = pathlib.Path(exchange_root) / PLAN_REPO_DIR
    if not (repo / ".git").exists():
        return RefreshResult(owner, generator, failure=(
            f"计划库还没有 clone 到 {repo}：来源清单发在 plan 库的 {SNAPSHOT_REL}，"
            "先按上机清单把 plan 库 clone 下来（第 9 / 11 步），再跑交换台。"))
    if provider is None:
        return RefreshResult(owner, generator, failure=(
            f"本机配了 gitee_owner = {owner}，但读不到 Gitee API token（环境变量 {TOKEN_ENV}）："
            "维护机刷新来源清单要配好这个 token（只读、不进仓库、不进日志）。"))

    channel = GitChannel(repo)
    try:
        pull_fresh(channel)
    except ChannelError as exc:
        return RefreshResult(owner, generator, failure=(
            f"拉不到 plan 库：{exc}\n来源清单这次没刷新，上一版有效快照照旧有效。"))

    previous_owner, previous = _read_snapshot(channel)
    try:
        discovered = provider.list_repos()
    except SourceQueryError as exc:
        return RefreshResult(owner, generator, previous=previous, failure=(
            f"来源查询没成（{exc}）：查不全绝不覆盖上一版有效快照，改好之后重跑。"))

    sources = _select(discovered, owner=owner)
    removed = [entry.machine_id for entry in previous
               if entry.machine_id not in {source.machine_id for source in sources}]
    changed = sorted(source.machine_id for source in sources
                     for old in previous
                     if old.machine_id == source.machine_id and old.remote != source.remote)
    if (removed or changed) and not confirm_shrink:
        return RefreshResult(owner, generator, sources, held=True, previous=previous,
                             warnings=(_hold_warning(removed, changed),))

    if previous_owner == owner and previous == sources:
        return RefreshResult(owner, generator, sources, published=True, unchanged=True,
                             previous=previous, commit=channel.head())
    return _publish(channel, owner=owner, generator=generator, sources=sources,
                    previous=previous, now=now)


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
        return RefreshResult(owner, generator, sources, previous=previous, failure=(
            f"来源清单没发出去：{exc}\n上一版有效快照照旧有效，通道修好后重跑即可。{tail}"))
    return RefreshResult(owner, generator, sources, published=True, previous=previous,
                         commit=channel.head())


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
            "改名，确认无误后再发布（重跑并显式确认这次变化）。")


def _read_snapshot(channel: GitChannel) -> tuple[str, tuple[SourceEntry, ...]]:
    """读回上一版有效快照（HEAD 里那份）的 owner 与来源；没有或读不动算空。

    读不动（文件坏了/不是我们写的）当「没有上一版」——与判断包「远端那份读不成判断集就照常
    覆盖」同一条自愈口径：拿本机的真内容盖掉它正是修复，而不是停下报错。
    """
    raw = channel.read_path(SNAPSHOT_REL)
    if raw is None:
        return "", ()
    try:
        document = json.loads(raw.decode("utf-8"))
        owner = str(document.get("owner") or "")
        sources = tuple(SourceEntry(machine_id=str(entry["machine_id"]),
                                    repo=str(entry["repo"]), remote=str(entry["remote"]))
                        for entry in document.get("sources") or ())
    except (ValueError, UnicodeDecodeError, KeyError, TypeError, AttributeError):
        return "", ()
    return owner, sources


# ---------- Gitee API 客户端（唯一的网络口） ----------

# 一页 GET 的形态：URL 与请求头进、解析后的 JSON 出；出错就抛（由 `_page` 折成 SourceQueryError）。
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
    """Gitee API v5 的仓库列表口：可信 owner 下 `type=all`（含私有）逐页翻到底。

    接口形状按 Gitee v5 文档（`GET /users/{owner}/repos?type=all&per_page=&page=`，
    认证用 `Authorization: token <token>` 头）。**真机上能否按这套跑通，是票 05 的人工验收项**——
    这里只把实现与失败语义写清，不宣称已在真实 Gitee 上验证过。
    """

    def __init__(self, owner: str, token: str, *, base: str = GITEE_API_BASE,
                 per_page: int = PER_PAGE, fetch: Fetch | None = None):
        self.owner = owner
        self._token = token
        self._base = base.rstrip("/")
        self._per_page = per_page
        self._fetch = fetch or _http_json

    def list_repos(self) -> tuple[GiteeRepo, ...]:
        """翻页取全部仓库；短页或空页即到底，翻到 `MAX_PAGES` 还满页算查不全。"""
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

    def _page(self, number: int) -> list[GiteeRepo]:
        url = (f"{self._base}/users/{urllib.parse.quote(self.owner)}/repos"
               f"?type=all&per_page={self._per_page}&page={number}")
        try:
            payload = self._fetch(url, {"Authorization": f"token {self._token}",
                                        "Accept": "application/json"})
        except SourceQueryError:
            raise
        except Exception as exc:                       # 网络/解析都折成同一档
            raise SourceQueryError(f"第 {number} 页查询没成（{type(exc).__name__}）：{exc}") from None
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
