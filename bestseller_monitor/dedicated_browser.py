"""Manual dedicated Edge: facts only, never account inference or browser teardown."""
from __future__ import annotations

import ctypes
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import threading

from .config import Config

HOME = 'https://www.1688.com/'
RETRY = '请自行关闭专用 Edge（包括后台窗口），确认退出后点刷新重试；不要关闭日常 Edge。'


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
        if os.name != 'nt':
            return Facts('unknown', '此浏览器事实适配器仅支持 Windows，无法安全核验。')
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
                timeout=12, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode != 0:
                raise OSError('进程/端口查询失败或权限不足')
            data = json.loads(result.stdout.decode('utf-8-sig'))
            processes = data['processes']
            by_pid = {p['pid']: p for p in processes}
            names = {'msedge.exe', 'chrome.exe', Path(settings.executable).name.lower()}
            unknown = False
            for proc in processes:
                if proc['name'].lower() not in names:
                    continue
                cmd = proc['cmd']
                if not cmd:
                    unknown = True
                    continue
                args = _windows_args(cmd)
                profile = _profile_arg(args)
                if profile is not None:
                    if not Path(profile).is_absolute():
                        unknown = True
                    elif os.path.normcase(str(Path(profile).resolve())) == os.path.normcase(settings.profile):
                        return Facts('occupied', '实际进程仍占用专用数据目录。')
                elif any(a.startswith('--type=') for a in args):
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
                return Facts('unknown', '存在无法读取或确认目录归属的浏览器进程。')
            if any(int(p['port']) == settings.port for p in data['ports']):
                return Facts('port_busy', '调试端口被既有实例占用，拒绝接管或另开标签。')
            return Facts('free', '实际进程与目录检查未发现占用。')
        except Exception as exc:
            return Facts('unknown', f'无法读取专用目录或进程事实：{exc}')

    def launch(self, settings):
        Path(settings.profile).mkdir(parents=True, exist_ok=True)
        return subprocess.Popen([settings.executable,
            f'--user-data-dir={settings.profile}',
            f'--remote-debugging-port={settings.port}',
            '--no-first-run', '--no-default-browser-check', HOME],
            cwd=settings.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


class DedicatedBrowser:
    def __init__(self, cfg, source, *, runtime=None):
        self.settings = Settings.of(cfg, source)
        self.runtime = runtime or BrowserRuntime()
        self._read_config = isinstance(cfg, Config)
        self._lock = threading.RLock()
        self._opened = False
        self._opening = False
        self._failure = None
        self._unconfirmed = False

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
        view = self.status()
        if not view['can_start']:
            return view['message'] + RETRY
        return None

    def status(self):
        with self._lock:
            try:
                fact = self.runtime.observe(self.settings)
                state, message = fact.state, fact.message
                if state == 'occupied':
                    self._unconfirmed = False
                elif self._unconfirmed and state == 'free':
                    state, message = 'unknown', '启动已返回但未观察到专用目录占用，创建归属无法确认；请检查窗口并重新打开 GUI。'
            except Exception as exc:
                state, message = 'unknown', f'无法读取浏览器事实：{exc}'
            if self._opening:
                state, message = 'opening', '正在打开专用 Edge，请使用即将出现的窗口。'
            elif state == 'free':
                state, message = ('closed', '已关闭，专用目录未占用。') if self._opened else ('free', '专用目录未占用。')
            elif state == 'occupied':
                message = '专用目录仍被浏览器占用，请使用已有窗口，不会另开标签。'
            error = self.configuration_error()
            if error:
                state, message = 'failed', error
            elif self._failure and state in ('free', 'closed'):
                state, message = 'failed', self._failure
            return {**dataclasses.asdict(self.settings), 'state': state,
                    'message': message, 'retry': RETRY,
                    'can_start': state in ('free', 'closed')}

    def reserve_open(self):
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
    current = profile
    while not _exists_readable(current):
        if current.parent == current:
            raise OSError('专用目录没有可读取的父目录')
        current = current.parent
    # Explicit enumeration surfaces PermissionError; exists() alone can hide it.
    list(current.iterdir())
    if not current.is_dir():
        raise OSError('专用数据目录或父目录不是目录')
    if _exists_readable(profile):
        for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
            if _exists_readable(profile/name):
                raise OSError('专用目录仍有浏览器锁，不能证明已释放')
        # Windows Chromium uses file-sharing locks, not a Unix SingletonLock.
        # Probe existing metadata handles exclusively; never read credential data.
        for file in (profile/'Local State', profile/'Default'/'Preferences'):
            if not _exists_readable(file):
                continue
            create = ctypes.windll.kernel32.CreateFileW
            create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            create.restype = ctypes.c_void_p
            handle = create(str(file), 0x80000000, 0, None, 3, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise OSError('专用目录文件被占用或无读取权限')
            close = ctypes.windll.kernel32.CloseHandle
            close.argtypes = [ctypes.c_void_p]
            close(handle)


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
        service = DedicatedBrowser(cfg, expected.source, runtime=runtime)
        reason = service.start_error()
        if reason:
            raise ValueError(reason)
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f'GUI 浏览器配置快照无效：{exc}') from exc
