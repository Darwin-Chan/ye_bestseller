"""会话级单实例锁：同名的第二个持有者直接拿不到锁。

界面进程与采集进程各持一把，用来回答「是不是已经有一个在跑」；交换台拿第三把
`EXCHANGE_LOCK`（窗口开着时锁在窗口进程手里，命令行与窗口同规——采集线票 14 的运行
互斥）；销量分析拿第四把 `ANALYSIS_LOCK`（窗口与 `--serve` 同规、共用一把——分析线
票 14 的运行互斥）。
锁是内核对象，持有它的进程被强杀时由系统释放，所以不需要清理残留文件；用 `Local\\`
前缀，同一个 Windows 会话内互斥、跨会话互不干扰（本项目是单机单账号）。
"""
from __future__ import annotations

import ctypes
import logging
import os

log = logging.getLogger(__name__)

GUI_LOCK = r"Local\bestseller_gui"
CRAWLER_LOCK = r"Local\bestseller_crawler"
# 交换台：同一台机器同一时刻至多一次交换台运行（窗口与命令行同规）。
EXCHANGE_LOCK = r"Local\bestseller_exchange"
# 销量分析：同一台机器同一时刻至多一次分析运行（窗口与 --serve 同规）。
ANALYSIS_LOCK = r"Local\bestseller_analysis"

# 采集进程抢不到锁时的退出码：界面据此提示「已有采集在跑」，而不是把它当成一轮正常结束。
# （现有约定里 2 = 没有有效店铺、3 = 店铺范围不符，这里避开。）
CRAWLER_BUSY_EXIT_CODE = 4

_ERROR_ALREADY_EXISTS = 183
_ERROR_FILE_NOT_FOUND = 2
_SYNCHRONIZE = 0x00100000


class InstanceLock:
    """已经抢到手的锁。release() 之后不要再用它。"""

    def __init__(self, handle, name: str):
        self.name = name
        self._handle = handle
        self._released = False

    def release(self) -> None:
        if self._released or self._handle is None:
            return
        self._released = True
        _kernel32().CloseHandle(self._handle)


def acquire(name: str) -> InstanceLock | None:
    """抢一把锁；已经有持有者时返回 None（不等待）。"""
    if os.name != "nt":
        return InstanceLock(None, name)
    kernel32 = _kernel32()
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, True, name)
    last_error = ctypes.get_last_error()
    if not handle:
        raise OSError(f"创建命名互斥体失败：{name}（GetLastError={last_error}）")
    if last_error == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return InstanceLock(handle, name)


def is_held(name: str) -> bool:
    """只问不抢：这个名字现在有没有持有者。"""
    if os.name != "nt":
        return False
    kernel32 = _kernel32()
    ctypes.set_last_error(0)
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() != _ERROR_FILE_NOT_FOUND


_KERNEL32 = None


def _kernel32():
    """kernel32（带 last-error）。第一次调用时加载并声明类型。

    use_last_error=True 才有 ctypes.get_last_error() 可读（ctypes.windll 那份不带）；
    句柄是 64 位指针，不声明 restype 会被默认的 c_int 截断。
    """
    global _KERNEL32
    if _KERNEL32 is None:
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        lib.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        lib.CreateMutexW.restype = ctypes.c_void_p
        lib.OpenMutexW.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_wchar_p]
        lib.OpenMutexW.restype = ctypes.c_void_p
        lib.CloseHandle.argtypes = [ctypes.c_void_p]
        lib.CloseHandle.restype = ctypes.c_int
        _KERNEL32 = lib
    return _KERNEL32
