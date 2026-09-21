"""票据 04 的 git 样例台：本地裸库当远端、克隆当机器，真跑 git 子进程。

通道与清单同步本身也真跑 git（「对本地裸库可测」就是这张票的验收口径），
所以这里不替身任何东西：夹具只负责搭出干净、可复现的小世界。

每个用例的 git 子进程都带一套隔离环境（`GitSandbox` 启动时 patch 进 os.environ，
产物代码里的 git 子进程同样继承）：

- 固定作者身份，不依赖本机 git 配置；
- `GIT_CONFIG_NOSYSTEM=1` ＋ 空的 `GIT_CONFIG_GLOBAL`：本机的 core.autocrlf、
  commit.gpgsign 之类不该影响用例结果（系统配置在 Windows 上通常被安装器设过）。

钩子脚本一律写成 LF 行尾——Windows 上带 CRLF 的 sh 脚本会以 `\r` 报错。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "样例台",
    "GIT_AUTHOR_EMAIL": "sandbox@example.invalid",
    "GIT_COMMITTER_NAME": "样例台",
    "GIT_COMMITTER_EMAIL": "sandbox@example.invalid",
}


def git(*args: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    """真跑一次 git，原样返回结果（成功失败都交给调用方判）。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C"),
    )


class GitSandbox:
    """一个用例的临时世界：裸库当远端、克隆当机器，外加隔离的 git 环境。"""

    def __init__(self, test: unittest.TestCase, prefix: str = "bestseller-git-"):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
        self._hooks = 0
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        empty_global = self.tmp / "empty-gitconfig"
        empty_global.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            **GIT_IDENTITY,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(empty_global),
        })
        patcher.start()
        test.addCleanup(patcher.stop)

    # ---- git 原语 ----

    def must(self, *args: str, cwd: pathlib.Path | None = None) -> str:
        done = git(*args, cwd=cwd)
        if done.returncode != 0:
            raise AssertionError(
                f"样例台 git 失败：git {' '.join(args)}\n{done.stdout}\n{done.stderr}"
            )
        return done.stdout

    # ---- 世界搭建 ----

    def new_remote(self, name: str = "remote.git") -> pathlib.Path:
        """建一个空裸库当远端（与 Gitee 上新建的空库同形：还没有任何提交）。"""
        path = self.tmp / name
        self.must("init", "--bare", "--initial-branch=main", str(path))
        return path

    def clone(self, remote: pathlib.Path, name: str) -> pathlib.Path:
        """clone 一份当某台机器的库；空库 clone 出来是 unborn 分支，同样保留。"""
        path = self.tmp / name
        self.must("clone", str(remote), str(path))
        return path

    def write_files(self, clone: pathlib.Path,
                    files: dict[str, str | bytes]) -> None:
        for name, content in files.items():
            path = clone / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8", newline="")

    def commit_push(self, clone: pathlib.Path, files: dict[str, str | bytes],
                    *, message: str = "change") -> None:
        """在克隆里写文件、提交并推送（模拟另一台机器发布一个提交）。"""
        self.write_files(clone, files)
        names = list(files)
        self.must("add", "--", *names, cwd=clone)
        self.must("commit", "-m", message, "--", *names, cwd=clone)
        self.must("push", cwd=clone)

    def seed(self, remote: pathlib.Path, files: dict[str, str | bytes],
             *, message: str = "seed") -> None:
        """给空裸库放第一个提交（经一个临时克隆走正常发布路径）。"""
        work = pathlib.Path(tempfile.mkdtemp(prefix="seed-", dir=self.tmp))
        self.must("clone", str(remote), str(work))
        self.commit_push(work, files, message=message)
        shutil.rmtree(work, ignore_errors=True)

    def install_pre_push(self, clone: pathlib.Path, script: str) -> None:
        """装一个 pre-push 钩子（LF 行尾）：用它做「推送途中的远端变化」这类交错。"""
        hooks = clone / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        script = "#!/bin/sh\n" + script.replace("\r\n", "\n").lstrip("\n")
        (hooks / "pre-push").write_bytes(script.encode("utf-8"))

    def remove_pre_push(self, clone: pathlib.Path) -> None:
        (clone / ".git" / "hooks" / "pre-push").unlink()

    def install_racing_hook(self, clone: pathlib.Path, other: pathlib.Path) -> None:
        """pre-push 钩子：只生效一次，把 other 里备好的提交抢先推上远端。

        用来制造「远端在本机 pull 之后、push 落地之前又动了」——non-fast-forward
        拒绝（含抢跑窗口的陈旧信息保护）发生的唯一窗口。
        """
        self._hooks += 1
        marker = self.tmp / f"raced-{self._hooks}"
        self.install_pre_push(clone, f'''
if [ ! -f "{self.sh_path(marker)}" ]; then
  touch "{self.sh_path(marker)}"
  git -C "{self.sh_path(other)}" push -q
fi
exit 0
''')

    def install_declining_hook(self, clone: pathlib.Path, counter: pathlib.Path) -> None:
        """pre-push 钩子：每次推送记一笔并拒绝——凭据被拒、网络断那类不可重试的失败。"""
        self.install_pre_push(clone, self._declining_body(counter))

    def _declining_body(self, counter: pathlib.Path) -> str:
        """「记一笔再拒绝」的钩子正文（不含 shebang）：三个拒绝类钩子共用这一份。"""
        return f'echo ran >> "{self.sh_path(counter)}"\nexit 1\n'

    def install_read_only_remote(self, remote: pathlib.Path, counter: pathlib.Path) -> None:
        """给裸库装 pre-receive 钩子：每次推送记一笔并拒绝——只读部署公钥那一侧的形态。

        与 pre-push 钩子不同，拒绝发生在**服务端**：客户端拿到的是 `[remote rejected]`
        （git 自己的说法是 hook declined），正是「这台机器的 key 没有写权限」时客户端
        看到的东西。规格的判据：push 被拒 = 只读档位配置正确（spec §11）。
        """
        hooks = remote / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-receive").write_bytes(
            ("#!/bin/sh\n" + self._declining_body(counter)).encode("utf-8"))

    def publish_into_bare(self, remote: pathlib.Path, clone: pathlib.Path) -> None:
        """让另一个克隆的提交落到裸库上，不经过 push（也就不经过 pre-receive 钩子）。

        给「远端只收有写权限的机器」那类用例用：只读那台照常 pull 得到它，自己推不上去。
        """
        self.must("--git-dir", str(remote), "fetch", str(clone), "main:refs/heads/main")

    def install_declining_commit_hook(self, clone: pathlib.Path, counter: pathlib.Path) -> None:
        """pre-commit 钩子：每次提交记一笔并拒绝——模拟「提交这一步本身失败」。

        这时刚写下的文件还没进任何提交（未跟踪）：只 reset 清不掉，是检验
        「发布失败后不留假的重读对象」的那条路（见 `plan_step._reread_after_failed_publish`）。
        """
        hooks = clone / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        script = f'#!/bin/sh\necho ran >> "{self.sh_path(counter)}"\nexit 1\n'
        (hooks / "pre-commit").write_bytes(script.encode("utf-8"))

    def install_advancing_hook(self, clone: pathlib.Path, other: pathlib.Path) -> None:
        """pre-push 钩子：每次推送都让远端再前进一格——把推送重试次数用尽。"""
        self.install_pre_push(clone, f'''
git -C "{self.sh_path(other)}" commit --allow-empty -qm tick
git -C "{self.sh_path(other)}" push -q
exit 0
''')

    def sh_path(self, path: pathlib.Path) -> str:
        """给钩子脚本里用的路径写法：正斜杠，sh 里不担心反斜杠转义。"""
        return str(path).replace("\\", "/")
