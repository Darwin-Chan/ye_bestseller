"""Manual dedicated Edge: facts only, never account inference or browser teardown."""
from __future__ import annotations

import ctypes
import dataclasses
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time

from .browser_proc import decode_console_output
from .config import Config

log = logging.getLogger(__name__)

HOME = 'https://www.1688.com/'
RETRY = '请自行关闭专用 Edge（包括后台窗口），确认退出后即可继续（界面会自动核对）；不要关闭日常 Edge。'
# 读事实那趟 PowerShell 的缰绳。热态实测 0.85–1.01 秒，本机冷态首遇 6.28 秒（2026-10-12 的
# m2 记录包 2026-10-12-1713-m2）。它只是「别挂太久」的上限：**超时不等于读到占用**——超时
# 折进「读不成」档，不拦开轮（见 ADR-0056）。
_PROBE_TIMEOUT_SEC = 30

# 独占打开探到的 WinError：别人持着 vs 我们没权限，是两回事。
_SHARING_VIOLATION = 32
_ACCESS_DENIED = 5

# 「读到的东西」与「没读到」的分界，就是这份放行表：只有读到占用（occupied / port_busy）
# 与配置错误（failed）落在表外——读不成（unreadable）不许冒充读到占用。checking 是
# 「还没读到第一份事实」：同样不拦，界面显示「正在核对」。
_STARTABLE_STATES = ('free', 'closed', 'unknown', 'unreadable', 'checking')

# 读路径最多等这么久让一次刷新落地——够快就当场拿到新事实，不够就先用上一份快照。
_INLINE_WAIT_SEC = 0.25


@dataclasses.dataclass(frozen=True)
class Settings:
    root: str
    source: str
    executable: str
    profile: str
    port: int
    start_browser: bool

    @classmethod
    def of(cls, cfg, source):
        root = Path(getattr(cfg, 'root', Path(source).parent.parent)).resolve()
        return cls(str(root), str(Path(source).resolve()),
                   str((root / cfg.chrome_path).resolve()) if cfg.chrome_path else '',
                   str((root / cfg.user_data_path).resolve()), int(cfg.attach_port),
                   bool(cfg.start_browser))

    def to_json(self):
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, sort_keys=True)


@dataclasses.dataclass(frozen=True)
class Facts:
    state: str
    message: str
    detail: str = ''


class _ProbeFact(Exception):
    """一条**确定读到**的事实：读到的东西本身构成结论（占用、目录形状不对）。"""

    def __init__(self, state, message):
        super().__init__(message)
        self.state = state
        self.message = message


class BrowserRuntime:
    """OS-only adapter. No cookies, navigation, connection or termination."""
    def config_error(self, settings):
        if not settings.start_browser:
            return 'GUI 手动打开和开始采集要求 browser.start_browser=true；CLI 外部连接模式不受影响。'
        local = os.environ.get('LOCALAPPDATA')
        if local:
            profile = Path(settings.profile).resolve()
            for product in ('Microsoft/Edge/User Data', 'Google/Chrome/User Data'):
                daily = (Path(local)/product).resolve()
                if profile == daily or daily in profile.parents:
                    return '配置错误：专用目录不能使用日常浏览器 User Data 或其子目录；请配置独立目录。'
        if not settings.executable or not Path(settings.executable).is_file():
            return f'未找到 Edge：{settings.executable or "browser.chrome_path 为空"}'
        if not 1 <= settings.port <= 65535:
            return '配置错误：browser.attach_port 必须在 1–65535 之间。'
        return None

    def observe(self, settings):
        """读一次事实。输出分档按「这是不是一个读到的事实」：读不成不许冒充读到占用。"""
        started = time.monotonic()
        if os.name != 'nt':
            return Facts('unreadable', '此浏览器事实适配器仅支持 Windows，无法安全核验；'
                                       '本次不拦开轮，界面会继续自动核对。',
                         detail='非 Windows 主机')
        result = None
        try:
            _check_directory(Path(settings.profile))
            # Enumerate all processes, not a guessed port owner. Null command lines
            # on browser processes are uncertainty, not absence.
            script = """$ErrorActionPreference='Stop';
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);
$p=@(Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object {
[pscustomobject]@{name=$_.Name;pid=$_.ProcessId;parent=$_.ParentProcessId;cmd=$_.CommandLine}});
$t=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | ForEach-Object {
[pscustomobject]@{port=$_.LocalPort;pid=$_.OwningProcess}});
@{processes=$p;ports=$t}|ConvertTo-Json -Depth 4 -Compress"""
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                '-WindowStyle', 'Hidden', '-Command', script], capture_output=True,
                timeout=_PROBE_TIMEOUT_SEC, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode != 0:
                # 查询自己失败：这是「没问成」，不是「没有」。
                raise OSError(f'进程/端口查询失败或权限不足（返回码 {result.returncode}）')
            data = json.loads(result.stdout.decode('utf-8-sig'))
            processes = data['processes']
            by_pid = {p['pid']: p for p in processes}
            names = {'msedge.exe', 'chrome.exe', Path(settings.executable).name.lower()}
            unknown = False
            for proc in processes:
                cmd = proc['cmd']
                known_browser = proc['name'].lower() in names
                if not cmd:
                    if known_browser:
                        unknown = True
                    continue
                args = _windows_args(cmd)
                profile = _profile_arg(args)
                if profile is not None:
                    if not Path(profile).is_absolute():
                        unknown = True
                    elif os.path.normcase(str(Path(profile).resolve())) == os.path.normcase(settings.profile):
                        return Facts('occupied', '实际进程仍占用专用数据目录。',
                                     detail=self._detail(started, result))
                elif known_browser and any(a.startswith('--type=') for a in args):
                    # A surviving child without a readable browser ancestor may
                    # still hold profile files after the last window closed.
                    parent = by_pid.get(proc['parent'])
                    seen = {proc['pid']}
                    while parent and parent['pid'] not in seen:
                        seen.add(parent['pid'])
                        if parent['name'].lower() in names and parent['cmd']:
                            parent_args = _windows_args(parent['cmd'])
                            if not any(a.startswith('--type=') for a in parent_args):
                                break
                        parent = by_pid.get(parent['parent'])
                    else:
                        unknown = True
            if unknown:
                # 看到了浏览器但核不实归属：这是读到的事实，但归属判定靠的是命令行文本——
                # 软判据不该有否决权，最终归属由开抓时的身份门（CDP PID ＋ OS 创建时刻）判。
                return Facts('unknown', '存在无法读取或确认目录归属的浏览器进程；'
                                        '本次不拦开轮，真正归属由开抓时判定。',
                             detail=self._detail(started, result))
            if any(int(p['port']) == settings.port for p in data['ports']):
                return Facts('port_busy', '调试端口被既有实例占用，拒绝接管或另开标签。',
                             detail=self._detail(started, result))
            return Facts('free', '实际进程与目录检查未发现占用。',
                         detail=self._detail(started, result))
        except _ProbeFact as fact:
            return Facts(fact.state, fact.message, detail=self._detail(started, result))
        except subprocess.TimeoutExpired:
            return Facts('unreadable',
                         f'读浏览器事实超时（上限 {_PROBE_TIMEOUT_SEC} 秒）；'
                         '本次不拦开轮，界面会继续自动核对。',
                         detail=self._detail(started, result))
        except Exception as exc:
            return Facts('unreadable', f'无法读取专用目录或进程事实：{exc}；'
                                       '本次不拦开轮，界面会继续自动核对。',
                         detail=self._detail(started, result))

    @staticmethod
    def _detail(started, result, note=''):
        parts = [f'耗时={time.monotonic() - started:.2f}s']
        if result is not None:
            parts.append(f'返回码={result.returncode}')
            # PowerShell 的 stderr 跟随控制台码页（中文 Windows 是 GBK），与 netstat 同一档。
            stderr = decode_console_output(getattr(result, 'stderr', b'')).strip()
            if stderr:
                parts.append('stderr=' + stderr[:200].replace('\n', ' '))
        if note:
            parts.append(note)
        return ' '.join(parts)


    def launch(self, settings):
        Path(settings.profile).mkdir(parents=True, exist_ok=True)
        return subprocess.Popen([settings.executable,
            f'--user-data-dir={settings.profile}',
            f'--remote-debugging-port={settings.port}',
            '--no-first-run', '--no-default-browser-check', HOME],
            cwd=settings.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


class DedicatedBrowser:
    def __init__(self, cfg, source, *, runtime=None, role='界面'):
        self.settings = Settings.of(cfg, source)
        self.runtime = runtime or BrowserRuntime()
        self.role = role
        self._read_config = isinstance(cfg, Config)
        self._lock = threading.RLock()
        self._opened = False
        self._opening = False
        self._failure = None
        self._unconfirmed = False
        # 事实快照：读路径只读它，从不在请求线程里跑那条 PowerShell。快照本身由一把
        # 只护它的锁守着——探测与等待都在锁外，免得刷新线程与读路径互相堵死。
        self._facts_lock = threading.Lock()
        self._facts = None
        self._facts_at = 0.0
        self._refreshing = False
        self._refresh_done = threading.Event()
        self._refresh_done.set()

    # ---------- 事实快照：探测在后台，读路径不等它 ----------
    def _start_refresh(self):
        """安排一次后台刷新（单飞）；返回是否有刷新在飞。

        每次读都安排一次，与今天「每次取数都跑一次探测」同节奏——变的是探测不再占请求
        线程。要降频率属于枚举降本，另议（见 spec 的 Out of Scope）。
        """
        with self._facts_lock:
            if self._refreshing:
                return True
            self._refreshing = True
            self._refresh_done.clear()
        threading.Thread(target=self._refresh, daemon=True,
                         name=f'浏览器事实核对-{self.role}').start()
        return True

    def _refresh(self):
        try:
            fact = self.runtime.observe(self.settings)
        except Exception as exc:
            fact = Facts('unreadable', f'无法读取浏览器事实：{exc}；'
                                       '本次不拦开轮，界面会继续自动核对。')
        with self._facts_lock:
            self._facts = fact
            self._facts_at = time.monotonic()
            self._refreshing = False
            self._refresh_done.set()
        # 每次探测留一行：命中哪一档、花了多久、查询自己的返回码与 stderr 摘要。
        log.info('浏览器事实（%s）：档位=%s %s', self.role, fact.state,
                 getattr(fact, 'detail', '') or fact.message[:120])

    def _read_facts(self):
        """读路径：必要时安排一次刷新，最多等一小会儿；等不到就用上一份（可能还没有）。"""
        if self._start_refresh():
            self._refresh_done.wait(_INLINE_WAIT_SEC)
        with self._facts_lock:
            return self._facts, self._refreshing

    def refresh_now(self, timeout=None):
        """强制取一次新事实并等它落地；`timeout=None` 等满一次探测的缰绳。

        给**主动动作**用：采集子进程启动时的契约复核要一份当场的事实（等满），界面上的
        主动动作（打开专用 Edge、开始/继续）只等 `_INLINE_WAIT_SEC`——它们要的是一个
        眼下够用的结论，等不到就先用上一份，真问题由采集子进程复核那道门兜底。
        """
        self._start_refresh()
        self._refresh_done.wait(_PROBE_TIMEOUT_SEC + 5 if timeout is None else timeout)
        with self._facts_lock:
            return self._facts

    def configuration_error(self):
        try:
            if self._read_config:
                current = Config.from_file(Path(self.settings.source), root=Path(self.settings.root))
                if Settings.of(current, self.settings.source) != self.settings:
                    return '浏览器配置已变化；请重新打开界面，按新配置准备后重试。'
            return self.runtime.config_error(self.settings)
        except Exception as exc:
            return f'配置错误，无法重新核验：{exc}；请修正配置并重新打开界面。'

    def start_error(self):
        error = self.configuration_error()
        if error:
            return error
        # 主动动作：先催一次当下的事实（最多等一小会儿），再照分档给结论。
        self.refresh_now(_INLINE_WAIT_SEC)
        view = self.status()
        if not view['can_start']:
            return view['message'] + RETRY
        return None

    def status(self):
        """读一份事实快照（必要时安排后台刷新）。**不在请求线程里跑探测**。"""
        with self._lock:
            if self._opening:
                return self._view('opening', '正在打开专用 Edge，请使用即将出现的窗口。')
            error = self.configuration_error()
            if error:
                return self._view('failed', error)
        fact, pending = self._read_facts()
        with self._lock:
            state, message = ((fact.state, fact.message) if fact is not None
                              else ('checking', '正在核对浏览器事实…'))
            if state == 'occupied':
                self._unconfirmed = False
            elif self._unconfirmed and state in ('free', 'unreadable', 'unknown', 'checking'):
                state, message = 'unconfirmed', '启动已返回但未观察到专用目录占用，创建归属无法确认；请检查窗口并重新打开 GUI。'
            if state == 'free':
                state, message = ('closed', '已关闭，专用目录未占用。') if self._opened else ('free', '专用目录未占用。')
            elif state == 'occupied':
                message = '专用目录仍被浏览器占用，请使用已有窗口，不会另开标签。'
            error = self.configuration_error()
            if error:
                state, message = 'failed', error
            elif self._failure and state in _STARTABLE_STATES:
                state, message = 'failed', self._failure
            # 还没有可用事实、而且有刷新在飞：让页面接着问，读到就停（别一直轮询）。
            hold = bool(pending) and state in ('checking', 'unreadable')
            return self._view(state, message, pending=hold)

    def _view(self, state, message, *, pending=False):
        return {**dataclasses.asdict(self.settings), 'state': state,
                'message': message, 'retry': RETRY,
                'can_start': state in _STARTABLE_STATES, 'pending': pending}


    def reserve_open(self):
        # 主动动作：先催一次当下的事实（锁外等），别拿一份旧快照决定「复用还是新开」。
        # 配置确定不对（如用了日常 profile）时不发探测——与目录判据同一纪律。
        if self.configuration_error() is None:
            self.refresh_now(_INLINE_WAIT_SEC)
        with self._lock:
            self._failure = None
            view = self.status()
            if view['state'] in ('opening', 'occupied'):
                return {'ok': True, 'browser': view, 'reused': True}
            if not view['can_start']:
                return {'ok': False, 'error': view['message'] + RETRY, 'browser': view}
            self._opening = True
            self._unconfirmed = True
        return None

    def finish_open(self):
        try:
            self.runtime.launch(self.settings)
            with self._lock:
                self._opened = True
        except Exception as exc:
            with self._lock:
                self._failure = f'打开专用 Edge 失败：{exc}'
            return {'ok': False, 'error': self._failure}
        finally:
            with self._lock:
                self._opening = False
        return {'ok': True, 'browser': self.status()}

    def open(self):
        reserved = self.reserve_open()
        return reserved if reserved is not None else self.finish_open()


def _windows_args(command):
    count = ctypes.c_int()
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    ptr = parse(command, ctypes.byref(count))
    if not ptr:
        raise OSError('无法解析进程命令行')
    try:
        return [ptr[i] for i in range(count.value)]
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(ptr, ctypes.c_void_p))


def _profile_arg(args):
    for i, arg in enumerate(args):
        if arg.startswith('--user-data-dir='):
            return arg.split('=', 1)[1]
        if arg == '--user-data-dir':
            return args[i+1] if i+1 < len(args) else ''
    return None


def _exists_readable(path):
    # stat failures other than absence must not become "directory free".
    try:
        path.stat()
        return True
    except FileNotFoundError:
        return False


def _check_directory(profile):
    """目录级的判定：读到的东西（锁文件、文件被持着）拦人，读不成的只记不拦。"""
    current = profile
    while not _exists_readable(current):
        if current.parent == current:
            raise _ProbeFact('unreadable', '专用目录没有可读取的父目录')
        current = current.parent
    if not current.is_dir():
        raise _ProbeFact('failed', '专用数据目录或父目录不是目录')
    # Explicit enumeration surfaces PermissionError; exists() alone can hide it.
    list(current.iterdir())
    if _exists_readable(profile):
        for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
            if _exists_readable(profile/name):
                raise _ProbeFact('occupied', '专用目录仍有浏览器锁，不能证明已释放')
        # Windows Chromium uses file-sharing locks, not a Unix SingletonLock.
        # Probe existing metadata handles exclusively; never read credential data.
        for file in (profile/'Local State', profile/'Default'/'Preferences'):
            if not _exists_readable(file):
                continue
            held = _sharing_probe(file)
            if held == _SHARING_VIOLATION:
                raise _ProbeFact('occupied', '专用目录文件被其它实例占用')
            if held == _ACCESS_DENIED:
                raise _ProbeFact('unreadable', '专用目录文件无读取权限')
            if held:
                raise _ProbeFact('unreadable', f'专用目录文件无法核验（WinError {held}）')


def _sharing_probe(path):
    """独占打开探一次这个文件：0 = 没人持着，否则是 WinError 码。

    共享冲突（32）本身就是「有实例在跑」的证据；权限不足（5）只是我们没读成——这两件事
    在旧代码里共用一个 OSError 文案，靠的是把最后错误码取回来才分得开。
    """
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    handle = create(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error() or -1
    close = kernel32.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_void_p
    close(handle)
    return 0



def verify_gui_settings(cfg, *, runtime=None):
    """Recheck the frozen contract at child load and while holding crawler lock.

    Only a GUI explicit-new invocation supplies this contract. Ordinary CLI CDP
    borrowing and ticket 03 resume retain their existing policies.
    """
    frozen = getattr(cfg, 'gui_browser_settings', None)
    if frozen is None:
        return
    try:
        expected = Settings(**json.loads(frozen))
        if Settings.of(cfg, expected.source) != expected:
            raise ValueError('浏览器配置与界面快照不一致，请重新打开界面。')
        service = DedicatedBrowser(cfg, expected.source, runtime=runtime, role='采集子进程复核')
        # 子进程要的是**当场**的事实：界面那份快照可能已经旧了，而且这里没有界面可等。
        service.refresh_now()
        reason = service.start_error()
        if reason:
            raise ValueError(reason)
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f'GUI 浏览器配置快照无效：{exc}') from exc
