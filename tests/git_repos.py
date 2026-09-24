"""票据 04 的 git 样例台：本地裸库当远端、克隆当机器，真跑 git 子进程。

通道与清单同步本身也真跑 git（「对本地裸库可测」就是这张票的验收口径），
所以这里不替身任何东西：夹具只负责搭出干净、可复现的小世界。

每个用例的 git 子进程都带一套隔离环境（`GitSandbox` 启动时 patch 进 os.environ，
产物代码里的 git 子进程同样继承）：

- 固定作者身份，不依赖本机 git 配置；
- `GIT_CONFIG_NOSYSTEM=1` ＋ 空的 `GIT_CONFIG_GLOBAL`：本机的 core.autocrlf、
  commit.gpgsign 之类不该影响用例结果（系统配置在 Windows 上通常被安装器设过）。

钩子脚本一律写成 LF 行尾——Windows 上带 CRLF 的 sh 脚本会以 `\r` 报错。

世界模板（2026-09-24，长尾地基税那张票的试点）：`WorldTemplate` 把「每个用例现搭世界」变成
「每测试进程只搭一次、用例复制取独立副本」——语义与代价见该类的文档；`GitSandbox.world`
是各文件搭模板件都走的那个形状（一个裸远端 ＋ 若干份克隆，可带历史）。
"""
from __future__ import annotations

import atexit
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "样例台",
    "GIT_AUTHOR_EMAIL": "sandbox@example.invalid",
    "GIT_COMMITTER_NAME": "样例台",
    "GIT_COMMITTER_EMAIL": "sandbox@example.invalid",
}


def _force_rmtree(path: pathlib.Path | str) -> None:
    """删一棵树，尽力而为；git 摆成只读的文件（松散对象）先去掉只读位再删。

    Windows 上 `shutil.rmtree(ignore_errors=True)` 删不掉只读文件，于是每个跑过推送的
    临时目录都会留在 %TEMP% 里；这里多一步 `chmod +w` 再删，剩下的失败仍然吞掉
    （清理失败不该让用例红）。
    """

    def clear_readonly(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        try:
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onexc=clear_readonly)


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
        self._bind(tempfile.mkdtemp(prefix=prefix))
        test.addCleanup(_force_rmtree, self.tmp)

        patcher = self._git_env()
        patcher.start()
        test.addCleanup(patcher.stop)

    @classmethod
    def in_root(cls, root: pathlib.Path) -> "GitSandbox":
        """一个占着现成目录的样例台：搭世界模板用（不属于任何用例、不挂 cleanup）。

        模板根由调用方给，所以这个样例台没有自己的临时目录也不用清；环境的活儿在
        `_git_env()` 里，搭模板时由调用方自己带上（`WorldTemplate._build` 就是这么做的）。
        """
        site = cls.__new__(cls)
        site._bind(root)
        return site

    def _bind(self, root: pathlib.Path | str) -> None:
        """把样例台落在某个目录上：字段只在这里初始化，用例的与搭模板的两条路径共用。"""
        self.tmp = pathlib.Path(root)
        self._hooks = 0

    def _git_env(self):
        """隔离的 git 环境（固定身份、不读本机与系统的 git 配置），patch 进 os.environ。"""
        empty_global = self.tmp / "empty-gitconfig"
        empty_global.write_text("", encoding="utf-8")
        return mock.patch.dict(os.environ, {
            **GIT_IDENTITY,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(empty_global),
        })

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
        _force_rmtree(work)      # 推过的库有只读对象，得先 chmod 再删（同模板清理）

    def world(self, remote: str, *clones: str,
              files: dict[str, str | bytes] | None = None) -> list[str]:
        """在这座样例台里搭一件小世界：一个裸远端 ＋ 若干份克隆；返回相对路径清单。

        `files` 给了就先走正常发布路径给远端放上第一个提交（远端与克隆因此**带历史**）、
        没给就是空库（未出生 main）——两种形状生产里都真存在（已发布的计划库、刚建出来
        还没人推的库）。顺序是「内容先到、机器克隆后建」（克隆出来就带着它）；要反过来的
        形状（克隆先建出来、内容后到：空库的克隆是未出生的，靠 pull 落地——清单同步那半
        的世界），分两步写：先 `world(remote, *clones)`，之后 `seed(self.tmp / remote, files)`。

        克隆总有一份是真的 `clone`，其余几份是第一份的整份复制：同一个远端、同一条历史，
        复制与再 clone 一次结果相同，省下重复的子进程。返回的清单按序、远端在头一个，
        直接当 `WorldTemplate.obtain` 的 builder 结果用——各文件搭模板件都从这里起手。
        """
        if (self.tmp / remote).exists():
            raise AssertionError(f"搭世界：{remote} 已经在了（一件世界在一座样例台里只搭一次）")
        bare = self.new_remote(remote)
        if files:
            self.seed(bare, files)
        for index, rel in enumerate(clones):
            (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            if index == 0:                          # 第一份真 clone
                self.clone(bare, rel)
            else:                                   # 其余是第一份的整份复制
                shutil.copytree(self.tmp / clones[0], self.tmp / rel)
        return [remote, *clones]

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


class WorldTemplate:
    """进程内只搭一次的「世界」模板：搭好之后只读，用例复制取独立副本。

    `build(site)` 在模板根里搭出一件世界（远端裸库、克隆、……）并返回它占的相对路径清单
    （复制按这份清单走）；同名模板在一个测试进程里只会真搭一次，之后每次 `take()` 都是
    纯文件复制、不再起 git 子进程。副本与「现场搭一份」在 git 语义上等价——远端还是那些
    裸库、克隆还是那些克隆，只有克隆配置里指着模板根的地址（origin）被改写成指着副本自己；
    用例之间因此看不见彼此的改动。

    件里的库**可以带历史**（长尾地基税票 03 起）：已 seed 的裸库、带着检出的克隆都行。
    `take()` 改的是文件里指着模板根的地址——克隆配置里的 origin、`.git/logs/**` 的 reflog
    （克隆的 `clone: from <模板路径>` 就写在里头）、裸库的 config——件里其余文件也逐份
    查一遍，还留着模板地址就当场报错（git 还会往别处写地址，比如取过的克隆有
    `.git/FETCH_HEAD`：那种形状要先把文件加进改写清单，别让它悄悄带进副本）；全程不起
    git 子进程。带历史的克隆副本与「现场 clone 一份」的差别只剩 reflog 里那条来源消息写的
    是副本自己的地址（远端、历史、检出、上游都一样；索引沿用模板那份的 stat 记录，头回
    碰它时 git 会重描一遍，结果不变）。
    「取世界不新增 git 子进程」这条靠的是「模板的 git 调用只发生在这一进程第一次取该件
    的时候」——之后每次 `take()` 都只是复制。

    模板只被复制、从不被写：某个用例在副本里推送、装钩子、删库，都污染不到模板本身，也
    污染不到别的用例。缓存按名字走、不认 build 函数，名字在整个测试进程里共用——别的
    测试文件也用这套原语时，前缀写自己的线名，别撞。

    搭模板的 git 调用走 `GitSandbox.in_root()` ＋ `_git_env()` 那套隔离环境（再加一个空的
    `GIT_TEMPLATE_DIR`：见 `_build`），与用例里的 patch 同形（身份、配置都固定），所以
    模板搭出来的库与本机 git 配置无关。
    """

    _lock = threading.Lock()
    _built: dict[str, "WorldTemplate"] = {}

    def __init__(self, root: pathlib.Path, paths: tuple[str, ...]):
        self.root = root
        self.paths = paths

    @classmethod
    def obtain(cls, name: str, build) -> "WorldTemplate":
        """取同名模板：这一进程里第一次取的时候真搭一次（真跑 git），之后只复制。

        件的内容不一样就换一个名字——按内容算个短摘要拼进名字里（见
        `test_plan_step.world_template`），别让两件不同的世界共用同一个名字。
        """
        with cls._lock:
            template = cls._built.get(name)
            if template is None:
                template = cls._built[name] = cls._build(name, build)
            return template

    @classmethod
    def _build(cls, name: str, build) -> "WorldTemplate":
        root = pathlib.Path(tempfile.mkdtemp(prefix=f"bestseller-template-{name}-"))
        atexit.register(_force_rmtree, root)
        site = GitSandbox.in_root(root)
        # 模板根里的空模板目录：git 不再往每个库里摆样例钩子、description、info/exclude
        # 这些死文件（git 从不执行、用例从不读），一件世界少上百个文件，复制（每用例一次）
        # 也因此便宜；钩子目录要装钩子时由样例台自己 mkdir。副本与现搭的差别只有这些。
        empty_template = root / "empty-git-template"
        empty_template.mkdir()
        with site._git_env(), mock.patch.dict(
                os.environ, {"GIT_TEMPLATE_DIR": str(empty_template)}):
            return cls(root, tuple(build(site)))

    def take(self, sandbox: GitSandbox) -> None:
        """把这份模板复制进某个沙盒（纯文件操作），并校验副本不再指着模板。

        目的地已存在就报错（一件世界在一个沙盒里只取一次）——克隆地址这时已改写成
        副本自己的，重复取会把地址改花。复制中出错时把**这次落地的**几件清掉再抛，
        免得留下半个世界把真正的错因盖住（先前就在那儿的路径不碰）；正在复制的这一件
        也算「这次落地的」——`copytree` 先把目标建出来、批到最后才抛，漏掉它就会让
        下一次取世界撞上一句「文件已存在」，把真正的原因盖住。
        """
        created: list[pathlib.Path] = []
        try:
            for rel in self.paths:
                target = sandbox.tmp / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise FileExistsError(
                        f"取世界：{target} 已经在了（一件世界在一个沙盒里只取一次）")
                created.append(target)      # 记在复制之前：半份的也要清掉
                shutil.copytree(self.root / rel, target)
            self._repoint(sandbox.tmp)
        except Exception:
            for target in created:
                _force_rmtree(target)
            raise

    def _repoint(self, dest_root: pathlib.Path) -> None:
        """把副本里指着模板根的地址改写成副本自己的（克隆的 origin、裸库的 url、reflog 的来源）。

        不改 `git remote set-url` 而改文本：取世界不许起 git 子进程（票 01 的验收线），
        而配置里要换的就是这一个路径。一个路径准备三种拼法：原样、配置里的转义写法
        （Windows 路径的反斜杠会写成 `\\`）、正斜杠写法；先换转义写法——它最具体，
        换完就不会再被别的拼法误伤。

        要改的是「git 记来源地址的地方」：克隆的 `.git/config` 与 `.git/logs/**`、裸库的
        `config`。件里**其余**文件也逐份查一遍残留（见 `_reject_leftover`）：git 还会往
        别处写地址（取过的克隆有 `.git/FETCH_HEAD`、有人往模板里装过钩子……），那种形状
        会在这里当场报错，逼着把文件加进改写清单，而不是悄悄带进每一份副本。
        """

        def spellings(path: pathlib.Path) -> tuple[bytes, ...]:
            native = str(path)
            return tuple(spell.encode("utf-8") for spell in
                         (native.replace("\\", "\\\\"), path.as_posix(), native))

        pairs = tuple(zip(spellings(self.root), spellings(dest_root)))
        for rel in self.paths:
            tree = dest_root / rel
            if (tree / ".git").is_dir():                # 克隆：配置在 .git 里，reflog 也在
                written = [tree / ".git" / "config"]
                logs = tree / ".git" / "logs"
                if logs.is_dir():
                    written += sorted(p for p in logs.rglob("*") if p.is_file())
            elif (tree / "HEAD").is_file():             # 裸库：配置在根上
                written = [tree / "config"]
            else:
                raise AssertionError(f"取世界：{rel} 既不像克隆也不像裸库（{tree}）")
            for path in written:
                _rewrite_address(path, pairs)
            for path in sorted(tree.rglob("*")):
                if path.is_file():
                    _reject_leftover(path, path.read_bytes(), pairs)


def _rewrite_address(path: pathlib.Path, pairs) -> None:
    """把一份 git 文件里的模板地址换成副本地址；换完还留着模板地址就报错。

    按字节换、按字节写回：这些文件是 git 写的（内容可能不是本机编码），按文本解码读
    一遍只会为跟本票无关的字节翻车；路径自己的编码与 git 写下的那份一致（UTF-8）。
    """
    data = path.read_bytes()
    fixed = data
    for old, new in pairs:
        fixed = fixed.replace(old, new)
    _reject_leftover(path, fixed, pairs)
    if fixed != data:
        path.write_bytes(fixed)


def _reject_leftover(path: pathlib.Path, data: bytes, pairs) -> None:
    """这份文件的字节里还留着模板地址就报错（副本与模板必须互不相干）。"""
    for old, _ in pairs:
        if old in data:
            raise AssertionError(
                f"取世界：{path} 里还留着模板地址（{old.decode('utf-8')}）")
