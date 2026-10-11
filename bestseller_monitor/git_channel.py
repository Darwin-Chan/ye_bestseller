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
- 每次 git 子进程调用都带同一处上限（`_TIMEOUT_SEC`）：等满上限就放弃这次调用（杀进程
  树、不等它收尾，见 `_abandon`），归一成通道错误原样报出（issue 05 后续：挂住比失败
  更糟——没有结论、没有退出码、没有日志）。
- 失败消息一律带上 git 退出码；两路皆空时按「进程被外部结束」那一档说人话（issue 05：
  丢掉返回码的那版只剩一句「（git 没有输出）」，分不清 git 自己失败与进程被外部杀掉）
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import time

DEFAULT_PUSH_ATTEMPTS = 5

# 通道里每次 git 子进程调用的共享上限，300 秒一档（与 cos_store 的 coscli 子进程同档）：
# 交换包体量在百 KB~MB 量级，正常一次 clone/pull/push 是秒级到十几秒；等满 300 秒只可能
# 来自通道连上了却不回应（网络黑洞、对端假死）或 git 进程卡死——而不是「包大」。没有它，
# 交换台会无限挂住：没有结论、没有退出码、没有日志（issue 05）。
_TIMEOUT_SEC = 300

# 放弃一次超时调用时留给「杀进程＋收尸」的宽限：taskkill 与 wait 都是本机小动作，
# 正常一瞬间；超过就不再等——调用方要立刻拿到结论，这才是超时这一档的意义。
_ABANDON_GRACE_SEC = 10

# 远端还没有这个分支时的两种 git 说法（LC_ALL=C 下稳定）
_UNBORN_REMOTE = re.compile(r"no such ref was fetched|couldn't find remote ref", re.IGNORECASE)
# 仓库里没有这条 remote 时 git 的稳定说法（`remote get-url` 拿不到地址的那一档）
_NO_SUCH_REMOTE = re.compile(r"no such remote", re.IGNORECASE)
# 「远端在本机拉取之后又动了」两种拒绝措辞：普通非快进，与抢跑窗口里的陈旧信息保护。
# 别的推送失败（认证、网络、钩子拒绝）不含这些字样，不重试。
_RETRY_HINT = re.compile(
    r"non-fast-forward|fetch first|Updates were rejected"
    r"|cannot lock ref .*but expected",
    re.IGNORECASE,
)

# 两路皆空时用来分档的下界：Windows 上进程被外部结束（TerminateProcess／安全软件拦）
# 留下的是 NTSTATUS 那一档的码（如 0xC000013A 被结束），POSIX 下被信号杀死则是负数。
_ABNORMAL_EXIT_FLOOR = 0xC0000000


class ChannelError(RuntimeError):
    """通道操作失败；消息带命令与 git 的输出摘要，供上游告警原样引用。"""


def _git_env() -> dict[str, str]:
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")


def _run_git(what: str, args: tuple[str, ...], **kwargs) -> subprocess.CompletedProcess:
    """起一个 git 子进程；每次调用共用同一处上限（`_TIMEOUT_SEC`），超时归一成通道错误。

    不用 `subprocess.run(timeout=…)`：超时后它还会阻塞地把被杀死进程的两个流收干，
    而 git 的网络助手（git-remote-http）攥着那份管道句柄不放时，这一步本身就无限等
    ——实测「对端收下连接却不回应」那一档，run 的 timeout 兜不住（票 01 证据
    `real-timeout-stall-smoke-run-based.txt`）。这里自己拿 Popen：超时即杀进程树、
    不再等它交管子，「放弃这次调用」就真的放弃。

    耗时现测（起表放在这一层，文本／字节两条都覆盖）：`TimeoutExpired` 只带上限、
    不带实际耗时，而这一档的现场事实正是「等满多久才放弃」。
    """
    started = time.monotonic()
    proc = subprocess.Popen(["git", *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **kwargs)
    try:
        out, err = proc.communicate(timeout=_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        _abandon(proc)
        raise _timeout_failure(what, args, exc, time.monotonic() - started) from exc
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _run(what: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _run_git(what, args, text=True, encoding="utf-8", errors="replace",
                    env=_git_env())


def _run_bytes(what: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    """同一个 git 通道，但拿字节（`git show HEAD:<路径>` 读包这类二进制内容）。"""
    return _run_git(what, args, env=_git_env())


def _failure(what: str, args: tuple[str, ...], done: subprocess.CompletedProcess[str]) -> ChannelError:
    """把 git 两个流里有的话都带上：`push --porcelain` 的拒绝理由在 **stdout**（stderr
    只有一句「failed to push some refs」）——只挑一个流会把最要紧的那句丢掉。

    **退出码一律带上**（issue 05）：两路皆空时它是唯一的线索，原先被丢掉，现场只留下
    一句「（git 没有输出）」，等于什么都没说——返回码 1（git 自己失败）与
    0xC000013A（进程被外部结束）本该是两种结论。
    """
    detail = "\n".join(part.strip() for part in (done.stderr, done.stdout) if part.strip())
    head = f"{what}失败（git {' '.join(args)}）："
    if detail:
        return ChannelError(f"{head}\n{detail}（git 退出码 {done.returncode}）")
    return ChannelError(f"{head}\n{_silent_failure_note(done.returncode)}")


def _silent_failure_note(returncode: int) -> str:
    """两个流都空时那句人话：git 自己失败几乎不会什么都不说。

    认证被拒、网络不可达、non-fast-forward、rebase 冲突、index.lock 占用都会留下字；
    能同时满足「返回码非零 + 两路皆空」的，更像进程被外部结束或异常终止。
    """
    if returncode < 0 or returncode >= _ABNORMAL_EXIT_FLOOR:
        return (f"git 一个字节都没输出，退出码 {returncode} 是「进程被外部结束」那一档："
                f"多半被安全软件或系统拦掉了。重跑一次通常就好；"
                f"再现就把这一行和当时的时刻留着。")
    return (f"git 一个字节都没输出（退出码 {returncode}）：这不像 git 自己报的错"
            f"（那类失败都会留话），更像进程被外部结束或通道瞬断。重跑一次通常就好。")


def _timeout_failure(what: str, args: tuple[str, ...], exc: subprocess.TimeoutExpired,
                     elapsed: float) -> ChannelError:
    """超时那一档（issue 05 后续票 01）：等满上限就放弃这次调用，不再无限等。

    形态与 `_failure` 同款：第一行是动作＋完整命令，下面给结论——那里在末尾放 git
    退出码，这里放「超过上限、已放弃」与实测耗时（诊断要的现场事实），上游原样引用。
    """
    return ChannelError(
        f"{what}超时（git {' '.join(args)}）：\n"
        f"超过 {exc.timeout} 秒上限，已放弃这次调用（实际耗时 {elapsed:.1f} 秒）："
        f"通道多半连上了却不回应（网络黑洞、对端假死）——不是 git 自己报的错；"
        f"重跑一次通常就好，再现就把这一行和当时的时刻留着。"
    )


def _abandon(proc: subprocess.Popen) -> None:
    """超时放弃这次调用：杀掉 git 连同它的子进程，收尸，但**不**等它把管道交干。

    Windows 上 git 把 stdout／stderr 的管道句柄传给了网络助手（git-remote-http）：
    只杀 git.exe，助手还攥着管子，谁再去读干那两个流就会一直等下去。taskkill /T
    整棵树一起杀，不成再退回只杀直接子进程——反正都不再读了。
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=_ABANDON_GRACE_SEC)
        except (OSError, subprocess.TimeoutExpired):
            pass
    proc.kill()
    try:
        proc.wait(timeout=_ABANDON_GRACE_SEC)   # 收尸；免僵尸与「仍在运行」告警
    except subprocess.TimeoutExpired:
        pass


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
        done = _run("克隆", *args)
        if done.returncode != 0:
            raise _failure("克隆", args, done)
        return cls(target)

    def pull(self) -> None:
        """拉取远端；远端还没有分支（新空库）时视为没有可拉的。"""
        self._pull("拉取", "--rebase")

    def pull_ff_only(self) -> None:
        """接收侧专用：拉取远端且**只允许快进**——本地有分叉的提交就失败，绝不重写历史。

        与发布侧的 `pull`（`--rebase`）分开：收来的别人那本 judged 库是只读来源，
        本机在它里面不该有本地提交，有就说明现场不对（人工动过），如实报错、由调用方
        按来源阻断——不自动 merge、不重置。`--no-rebase` 写死是不吃本机 git 配置的
        影响（pull.rebase 之类），空库（远端还没有分支）与 `pull` 同规视为没有可拉的。
        """
        self._pull("快进拉取", "--ff-only", "--no-rebase")

    def _pull(self, what: str, *flags: str) -> None:
        args = ("-C", str(self.path), "pull", *flags)
        done = _run(what, *args)
        if done.returncode != 0 and not _UNBORN_REMOTE.search(done.stderr or ""):
            raise _failure(what, args, done)

    def remote_url(self, name: str = "origin") -> str | None:
        """远端 `name` 的地址；没有这条 remote 时回 None（别的失败原样抛）。"""
        args = ("-C", str(self.path), "remote", "get-url", name)
        done = _run("读取远端地址", *args)
        if done.returncode == 0:
            return done.stdout.strip()
        if _NO_SUCH_REMOTE.search(done.stderr or ""):
            return None
        raise _failure("读取远端地址", args, done)

    def head_matches_upstream(self) -> bool:
        """HEAD 是否与上游一致；本地领先或落后都不一致（两端都还没提交的空库算一致）。

        接收侧判据用：收来的库是来源的只读镜像，本地有上游没有的提交就说明有人动过它
        ——读它的包等于读一份未经来源发布的判断集。
        """
        args = ("-C", str(self.path), "rev-list", "--left-right", "--count", "@{u}...HEAD")
        done = _run("对比上游", *args)
        if done.returncode != 0:
            return self.head_or_none() is None      # 还没有上游：空库的克隆，两端一致
        behind, ahead = (int(count) for count in done.stdout.split())
        return behind == 0 and ahead == 0

    def head_or_none(self) -> str | None:
        """当前提交；还没有任何提交（空库的克隆）回 None。"""
        try:
            return self.head()
        except ChannelError:
            return None

    def local_changes(self) -> list[str]:
        """工作区里未提交的改动与未跟踪文件的 porcelain 清单；干净时为空。

        准备串里的 pull 在有未提交改动时会直接失败，所以调用方要先问这一句：
        上次写到一半崩掉留下的残迹得先收拾掉，否则这台机器每次开程序都卡在
        「拉不动」（见 `plan_step._heal_dirty_clone`）。
        """
        args = ("-C", str(self.path), "status", "--porcelain")
        done = _run("查看状态", *args)
        if done.returncode != 0:
            raise _failure("查看状态", args, done)
        return [line for line in done.stdout.splitlines() if line.strip()]

    def commit(self, message: str, paths: list[str | pathlib.Path]) -> bool:
        """提交这些路径的当前内容；没有可提交的变化时返回 False（不是错误）。"""
        names = [str(p) for p in paths]
        add_args = ("-C", str(self.path), "add", "--", *names)
        done = _run("暂存", *add_args)
        if done.returncode != 0:
            raise _failure("暂存", add_args, done)
        commit_args = ("-C", str(self.path), "commit", "-m", message, "--", *names)
        done = _run("提交", *commit_args)
        if done.returncode == 0:
            return True
        if "nothing to commit" in (done.stdout + done.stderr):
            return False
        raise _failure("提交", commit_args, done)

    def push(self, *, attempts: int = DEFAULT_PUSH_ATTEMPTS) -> None:
        """先 pull --rebase 再 push；被 non-fast-forward 拒绝就重来，至多 attempts 次（含首次）。"""
        for _ in range(max(attempts, 1)):
            self.pull()
            args = ("-C", str(self.path), "push", "--porcelain")
            done = _run("推送", *args)
            if done.returncode == 0:
                return
            if not _rejected(done):
                raise _failure("推送", args, done)
            # 被拒：远端在本机 pull 之后又动了；下一轮 pull --rebase 会把本地提交接上去
        raise ChannelError(
            f"推送被 non-fast-forward 拒绝，连续尝试 {max(attempts, 1)} 次都没成功：\n"
            f"远端一直有更快的提交。稍后再试；反复失败说明有另一台机器在同一条线上高频发布。"
        )

    def head(self) -> str:
        """当前提交的短哈希（导出报告里引用「发到了哪个提交」）。"""
        args = ("-C", str(self.path), "rev-parse", "--short", "HEAD")
        done = _run("读取提交", *args)
        if done.returncode != 0:
            raise _failure("读取提交", args, done)
        return done.stdout.strip()

    def read_path(self, path: str) -> bytes | None:
        """HEAD 里这个仓库相对路径的字节；HEAD 里没有它（新库/新文件）时回 None。

        问「远端那份是什么」用这一条（比如导出判「已发布的那份同不同内容」）——
        工作区可能留着崩溃残迹，不能当事实。
        """
        args = ("-C", str(self.path), "show", f"HEAD:{pathlib.PurePosixPath(path)}")
        done = _run_bytes("读取 HEAD 内容", *args)
        return done.stdout if done.returncode == 0 else None

    def reset_to_upstream(self, *, clean: bool = False) -> None:
        """把分支与工作区退回远端状态：未推送出去的提交与改动都会被撤掉。

        失败降级路径专用（推送没成功时把克隆还原干净，让下次开程序从头来）——
        正常流程里撤掉的只有本次刚写进去的那笔（同步开始时 pull 会先要求工作区干净）。
        中途撞上 rebase 冲突（`pull --rebase` 留下的现场）也一并清掉：不清的话克隆会
        卡在「变基进行中」，下次拉取直接失败。调用方手里的本机副本不受影响。

        `clean=True` 时连未跟踪文件一起清掉（写给机器管理的克隆用：写进去的都立刻
        提交、失败就退回，未跟踪的只可能是崩在半途的残迹）。
        """
        if any((self.path / ".git" / d).exists() for d in ("rebase-merge", "rebase-apply")):
            abort_args = ("-C", str(self.path), "rebase", "--abort")
            done = _run("终止变基", *abort_args)
            if done.returncode != 0:
                raise _failure("终止变基", abort_args, done)
        args = ("-C", str(self.path), "reset", "--hard", "@{u}")
        done = _run("回退", *args)
        if done.returncode != 0:
            raise _failure("回退", args, done)
        if clean:
            clean_args = ("-C", str(self.path), "clean", "-fd")
            done = _run("清未跟踪文件", *clean_args)
            if done.returncode != 0:
                raise _failure("清未跟踪文件", clean_args, done)


def _rejected(done: subprocess.CompletedProcess[str]) -> bool:
    """这次 push 是不是「远端动过、得先整合」式的拒绝——只有这种才值得重试。

    优先认 stderr 的措辞（fetch first / cannot lock ref … but expected）；porcelain 的
    普通 `[rejected]` 作兜底。`[remote rejected]`（钩子拒绝等）不在此列：重试解决不了它。
    """
    if _RETRY_HINT.search(done.stderr or ""):
        return True
    flags = [line for line in (done.stdout or "").splitlines() if line.startswith("!")]
    return any("[rejected]" in line and "[remote rejected]" not in line for line in flags)
