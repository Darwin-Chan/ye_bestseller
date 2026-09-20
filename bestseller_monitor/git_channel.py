"""交换区的 git 通道（spec §2）：对某个交换库做 clone / pull / push。

全仓第一处 git 调用（spec §6：采集启动路径此前零网络能力）。口径：

- **推送**：push 前先 `pull --rebase`；被 non-fast-forward 拒绝就重试，至多 5 次
  （一机一目录一库天然免文本冲突，被拒只可能是远端在我们拉取之后又动了）。
- **不重试其余失败**：认证被拒、网络不可达、钩子拒绝都原样报错——重试 5 次只会被
  仓库记成失败连接；由调用方按「推不动不拦开轮」降级。
- 子进程一律 `GIT_TERMINAL_PROMPT=0`：凭据不齐时立刻失败，不挂在等人输密码的提示上。
- 诊断输出固定英文（`LC_ALL=C`）：判定「被拒」用 `push --porcelain` 的稳定标记，
  不依赖人话文案。
- 空库（远端还没有分支）上的 `pull` 不算失败：没有可拉的而已。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

DEFAULT_PUSH_ATTEMPTS = 5

# 远端还没有这个分支时的两种 git 说法（LC_ALL=C 下稳定）
_UNBORN_REMOTE = re.compile(r"no such ref was fetched|couldn't find remote ref", re.IGNORECASE)
# 「远端在本机拉取之后又动了」两种拒绝措辞：普通非快进，与抢跑窗口里的陈旧信息保护。
# 别的推送失败（认证、网络、钩子拒绝）不含这些字样，不重试。
_RETRY_HINT = re.compile(
    r"non-fast-forward|fetch first|Updates were rejected"
    r"|cannot lock ref .*but expected",
    re.IGNORECASE,
)


class ChannelError(RuntimeError):
    """通道操作失败；消息带命令与 git 的输出摘要，供上游告警原样引用。"""


def _git_env() -> dict[str, str]:
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")


def _run(*args: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_git_env(),
    )


def _failure(what: str, args: tuple[str, ...], done: subprocess.CompletedProcess[str]) -> ChannelError:
    detail = done.stderr.strip() or done.stdout.strip() or "（git 没有输出）"
    return ChannelError(f"{what}失败（git {' '.join(args)}）：\n{detail}")


class GitChannel:
    """一个交换库的本地克隆；所有操作都在它里面发生。"""

    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)

    @classmethod
    def clone(cls, url: str, path: str | pathlib.Path) -> "GitChannel":
        """把 url 克隆到 path，返回指向它的通道。"""
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        args = ("clone", url, str(target))
        done = _run(*args)
        if done.returncode != 0:
            raise _failure("克隆", args, done)
        return cls(target)

    def pull(self) -> None:
        """拉取远端；远端还没有分支（新空库）时视为没有可拉的。"""
        args = ("-C", str(self.path), "pull", "--rebase")
        done = _run(*args)
        if done.returncode != 0 and not _UNBORN_REMOTE.search(done.stderr or ""):
            raise _failure("拉取", args, done)

    def commit(self, message: str, paths: list[str | pathlib.Path]) -> bool:
        """提交这些路径的当前内容；没有可提交的变化时返回 False（不是错误）。"""
        names = [str(p) for p in paths]
        add_args = ("-C", str(self.path), "add", "--", *names)
        done = _run(*add_args)
        if done.returncode != 0:
            raise _failure("暂存", add_args, done)
        commit_args = ("-C", str(self.path), "commit", "-m", message, "--", *names)
        done = _run(*commit_args)
        if done.returncode == 0:
            return True
        if "nothing to commit" in (done.stdout + done.stderr):
            return False
        raise _failure("提交", commit_args, done)

    def push(self, *, attempts: int = DEFAULT_PUSH_ATTEMPTS) -> None:
        """先 pull --rebase 再 push；被 non-fast-forward 拒绝就重来，至多 attempts 次。"""
        for _ in range(max(attempts, 1)):
            self.pull()
            args = ("-C", str(self.path), "push", "--porcelain")
            done = _run(*args)
            if done.returncode == 0:
                return
            if not _rejected(done):
                raise _failure("推送", args, done)
            # 被拒：远端在本机 pull 之后又动了；下一轮 pull --rebase 会把本地提交接上去
        raise ChannelError(
            f"推送被 non-fast-forward 拒绝，连续重试 {max(attempts, 1)} 次都没成功：\n"
            f"远端一直有更快的提交。稍后再试；反复失败说明有另一台机器在同一条线上高频发布。"
        )

    def reset_to_upstream(self) -> None:
        """把分支与工作区退回上游状态：撤掉还没推送出去的本地提交与改动。

        失败降级路径专用（推送没成功时把克隆还原干净，让下次开程序从头来）；
        只可能撤掉本次刚写进去的内容——调用方手里的本机副本不受影响。
        中途撞上 rebase 冲突（`pull --rebase` 留下的现场）也一并清掉：不清的话克隆会
        卡在「变基进行中」，下次拉取直接失败。
        """
        if any((self.path / ".git" / d).exists() for d in ("rebase-merge", "rebase-apply")):
            abort_args = ("-C", str(self.path), "rebase", "--abort")
            done = _run(*abort_args)
            if done.returncode != 0:
                raise _failure("终止变基", abort_args, done)
        args = ("-C", str(self.path), "reset", "--hard", "@{u}")
        done = _run(*args)
        if done.returncode != 0:
            raise _failure("回退", args, done)


def _rejected(done: subprocess.CompletedProcess[str]) -> bool:
    """这次 push 是不是「远端动过、得先整合」式的拒绝——只有这种才值得重试。

    优先认 stderr 的措辞（fetch first / cannot lock ref … but expected）；porcelain 的
    普通 `[rejected]` 作兜底。`[remote rejected]`（钩子拒绝等）不在此列：重试解决不了它。
    """
    if _RETRY_HINT.search(done.stderr or ""):
        return True
    flags = [line for line in (done.stdout or "").splitlines() if line.startswith("!")]
    return any("[rejected]" in line and "[remote rejected]" not in line for line in flags)
