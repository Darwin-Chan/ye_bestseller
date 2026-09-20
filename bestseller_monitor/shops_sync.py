"""共享店铺清单的同步（spec §4/§6）：本机 config/shops.csv ⇄ 计划库根目录 shops.csv。

共享清单只有一份、住在计划库里；本机那份是副本（编辑面仍在本机）。开轮前按内容
哈希判方向——基线 = 上次同步时两边一致的那份内容的哈希，记在交换区根目录的
`shops-sync.json`：

- 只本机变 → 写回计划库并推送；共享清单一律 **UTF-8 带 BOM** 写出
- 只库里变 → 拉取覆盖本机副本
- 两边都变（或本机没有基线，判不出方向）→ 停下把两版差异给人看，不动任何一边
- 通道不可达 / 推不动 → **不拦开轮**：用本机现值继续，把原因作为告警交出去

「内容」按文本口径算：解码（UTF-8 BOM 优先、退 GBK，与 `load_shops` 同一读法）、
行尾归一、去尾部空行——Excel 双击存一次带来的 BOM/CRLF 差异不算真实修改。

动计划库工作区的只有「推送」这一条路；推送失败就把克隆撤回远端状态（只可能撤掉
本次刚写入的内容，本机副本始终不动）。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import difflib
import hashlib
import json
import pathlib

from bestseller_monitor.git_channel import ChannelError, GitChannel

SHARED_FILE_NAME = "shops.csv"
STATE_FILE_NAME = "shops-sync.json"
PUSH_COMMIT_MESSAGE = "sync shared shops.csv"

ACTION_IN_SYNC = "in_sync"
ACTION_PUSHED = "pushed"
ACTION_PULLED = "pulled"
ACTION_CONFLICT = "conflict"
ACTION_UNREACHABLE = "unreachable"
ACTION_PUSH_FAILED = "push_failed"
ACTION_PULL_FAILED = "pull_failed"

_BOM = b"\xef\xbb\xbf"
_BEIJING = dt.timezone(dt.timedelta(hours=8))


class ShopsSyncError(RuntimeError):
    """同步进行不下去，也不是「用本机现值降级」能了事的（两边都没有清单等）。"""


@dataclasses.dataclass(frozen=True)
class SyncOutcome:
    """一次同步的结果。

    `warning=True` 表示不拦开轮：调用方告警之后照常继续，用的还是本机现值。
    `diff` 只在「两边都变」时有值（同一份差异也随消息给人看）。
    """

    action: str
    message: str
    warning: bool = False
    diff: str | None = None


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _digest(text: str) -> str:
    return hashlib.sha256(_normalized(text).encode("utf-8")).hexdigest()


def _canonical_bytes(text: str) -> bytes:
    """共享清单的磁盘形态：UTF-8 带 BOM、LF 行尾、末尾一个换行。"""
    return _BOM + (_normalized(text) + "\n").encode("utf-8")


def _read_text(path: pathlib.Path) -> str | None:
    return _decode(path.read_bytes()) if path.exists() else None


def _write_local_copy(local_csv: pathlib.Path, data: bytes) -> str | None:
    """写本机副本；写不动（只读、被别的程序占着）时返回原因，由调用方降级为告警。"""
    try:
        local_csv.parent.mkdir(parents=True, exist_ok=True)
        local_csv.write_bytes(data)
    except OSError as exc:
        return f"{local_csv} 写不动（{exc}）"
    return None


def _diff(local_csv: pathlib.Path, local_text: str,
          shared_csv: pathlib.Path, repo_text: str) -> str:
    lines = difflib.unified_diff(
        _normalized(local_text).splitlines(),
        _normalized(repo_text).splitlines(),
        fromfile=f"本机 {local_csv}", tofile=f"计划库 {shared_csv}", lineterm="")
    return "\n".join(lines)


def _read_baseline(state_path: pathlib.Path) -> str | None:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                      # 没有/读不动 = 没有基线，按「两边都变」停下
    digest = data.get("shops_sha256") if isinstance(data, dict) else None
    return digest if isinstance(digest, str) and digest else None


def _write_baseline(state_path: pathlib.Path, digest: str, action: str) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "shops_sha256": digest,
        "synced_at": dt.datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "action": action,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _restore_remote(channel: GitChannel) -> str | None:
    """推送失败后把克隆撤回远端状态；回退不了就把原因带出去（本机副本不受影响）。"""
    try:
        channel.reset_to_upstream()
    except ChannelError as exc:
        return str(exc)
    return None


def _push(channel: GitChannel, shared_csv: pathlib.Path, local_csv: pathlib.Path,
          local_text: str, state_path: pathlib.Path) -> SyncOutcome:
    """把本机现值写进计划库并推送。本机副本一笔不动——失败时它就是「本机现值」。"""
    try:
        shared_csv.write_bytes(_canonical_bytes(local_text))
    except OSError as exc:
        return SyncOutcome(ACTION_PUSH_FAILED,
                           f"计划库工作区写不进去：{shared_csv}（{exc}）。\n"
                           f"本机 {local_csv} 的改动留着，这次用本机现值开轮。", warning=True)
    try:
        channel.commit(PUSH_COMMIT_MESSAGE, [shared_csv])
        channel.push()
    except ChannelError as exc:
        note = _restore_remote(channel)
        tail = f"\n（克隆没能回退到远端状态：{note}）" if note else ""
        return SyncOutcome(ACTION_PUSH_FAILED,
                           f"共享店铺清单推送没成功：{exc}\n"
                           f"本机 {local_csv} 的改动留着（计划库那份没变），"
                           f"这次用本机现值开轮；通道修好后下次开程序会再试。{tail}",
                           warning=True)
    note = _write_local_copy(local_csv, _canonical_bytes(local_text))
    _write_baseline(state_path, _digest(local_text), ACTION_PUSHED)
    tail = f"（本机副本没能统一成同一编码：{note}）" if note else ""
    return SyncOutcome(ACTION_PUSHED,
                       f"共享店铺清单已推送：本机改动写进计划库 {shared_csv}"
                       f"（UTF-8 带 BOM）；本机副本已统一成同一编码。{tail}")


def sync_shared_shops(local_csv: str | pathlib.Path, plan_clone: str | pathlib.Path, *,
                      state_path: str | pathlib.Path | None = None) -> SyncOutcome:
    """同步本机清单与计划库共享清单；返回做了什么。

    每次都先 pull 计划库（判方向要看库里的现值）。能不能开轮由调用方定：
    `warning=True` 的那些结果都表示「用本机现值继续」。
    """
    local_csv = pathlib.Path(local_csv)
    plan_clone = pathlib.Path(plan_clone)
    state_path = (pathlib.Path(state_path) if state_path is not None
                  else plan_clone.parent / STATE_FILE_NAME)
    shared_csv = plan_clone / SHARED_FILE_NAME

    if not (plan_clone / ".git").exists():
        if not local_csv.exists():
            raise ShopsSyncError(
                f"计划库还没 clone 到 {plan_clone}、本机也没有 {local_csv}："
                "没有可用的店铺清单。按上机清单第 9 步 clone 计划库，"
                "或先在本机建好 config/shops.csv 再开程序。")
        return SyncOutcome(ACTION_UNREACHABLE,
                           f"计划库还没 clone 到 {plan_clone}（上机清单第 9 步）："
                           f"这次跳过清单同步，用本机 {local_csv} 继续。", warning=True)

    channel = GitChannel(plan_clone)
    try:
        channel.pull()
    except ChannelError as exc:
        if not local_csv.exists():
            raise ShopsSyncError(
                f"够不到计划库、本机也没有 {local_csv}：没有可用的店铺清单。\n{exc}") from exc
        return SyncOutcome(ACTION_UNREACHABLE,
                           f"拉不到计划库：{exc}\n"
                           f"这次用本机 {local_csv} 继续开轮；通道修好后下次开程序会再同步。",
                           warning=True)

    local_text = _read_text(local_csv)
    repo_text = _read_text(shared_csv)
    if local_text is None and repo_text is None:
        raise ShopsSyncError(
            f"两边都没有店铺清单：{local_csv} 与 {shared_csv} 都缺。先在本机建好 "
            "config/shops.csv（可抄 config/shops.example.csv），再同步一次。")

    if local_text is None:                       # 新机器：库里已有，拉下来当本机副本
        note = _write_local_copy(local_csv, shared_csv.read_bytes())
        if note:
            return SyncOutcome(ACTION_PULL_FAILED,
                               f"计划库那份没能落到本机：{note}。\n"
                               "处理好（比如关掉正在占着它的程序）再开一次程序。",
                               warning=True)
        _write_baseline(state_path, _digest(repo_text), ACTION_PULLED)
        return SyncOutcome(ACTION_PULLED, f"本机还没有店铺清单：已从计划库拉到 {local_csv}。")

    if repo_text is None:                        # 第一台机器：本机清单成为共享清单
        return _push(channel, shared_csv, local_csv, local_text, state_path)

    local_digest, repo_digest = _digest(local_text), _digest(repo_text)
    if local_digest == repo_digest:
        _write_baseline(state_path, local_digest, ACTION_IN_SYNC)
        return SyncOutcome(ACTION_IN_SYNC,
                           f"共享店铺清单一致：本机 {local_csv} 与计划库那份同内容。")

    baseline = _read_baseline(state_path)
    if repo_digest == baseline:                  # 只本机变
        return _push(channel, shared_csv, local_csv, local_text, state_path)
    if local_digest == baseline:                 # 只库里变
        note = _write_local_copy(local_csv, shared_csv.read_bytes())
        if note:
            return SyncOutcome(ACTION_PULL_FAILED,
                               f"计划库那份没能覆盖本机：{note}。\n"
                               "它可能被别的程序占着（比如正开在 Excel 里）——"
                               "处理好再开一次程序；这次仍用本机现值开轮。", warning=True)
        _write_baseline(state_path, repo_digest, ACTION_PULLED)
        return SyncOutcome(ACTION_PULLED, f"共享店铺清单已拉取：计划库那份覆盖了本机 {local_csv}。")

    diff = _diff(local_csv, local_text, shared_csv, repo_text)   # 两边都变：停下，不猜
    why = ("两边都变了" if baseline is not None
           else "本机还没有同步基线（第一次同步就撞上两边不一致）")
    return SyncOutcome(ACTION_CONFLICT,
                       f"共享店铺清单{why}，停下不猜：本机 {local_csv} 与计划库 {shared_csv} "
                       "都要人定夺——照下面的差异改成你想要的样子（或删掉本机那份、"
                       f"接受库里的），再开一次程序。\n\n两版差异（本机 → 计划库）：\n{diff}",
                       diff=diff)
