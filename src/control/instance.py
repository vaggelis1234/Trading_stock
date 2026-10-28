"""One Trading Bot process at a time, on Windows."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000
MUTEX_NAME = "Local\\TradeStocksTradingBot"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.SetLastError.argtypes = [wintypes.DWORD]
kernel32.SetLastError.restype = None
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class AlreadyRunning(RuntimeError):
    """A Trading Bot process already holds the instance lock."""


class InstanceLock:
    """Named mutex. A second acquire fails until the holder releases it."""

    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self.handle = None

    def acquire(self) -> None:
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        last_error = ctypes.get_last_error()
        if not handle:
            raise OSError(last_error, "CreateMutexW failed")
        if last_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise AlreadyRunning("Trading Bot is already running")
        self.handle = handle

    def release(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None


def bot_process_running(name: str = MUTEX_NAME) -> bool:
    """True when some process holds the bot mutex. Does not take ownership."""
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, name)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True
