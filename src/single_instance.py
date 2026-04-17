"""Windows single-instance guard via named mutex."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Global\\DicTide_7c2a9e1b_SingleInstance"

_mutex_handle: wintypes.HANDLE | None = None


def acquire() -> bool:
    """
    Create the app mutex. Returns True if this process owns the new mutex.
    Returns False if another instance already holds it.
    """
    global _mutex_handle
    if _mutex_handle is not None:
        return True
    if sys.platform != "win32":
        return True

    kernel32.CreateMutexW.argtypes = (
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    h = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not h:
        return True
    err = kernel32.GetLastError()
    if err == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(h)
        return False
    _mutex_handle = h
    return True


def release() -> None:
    global _mutex_handle
    if _mutex_handle is not None and sys.platform == "win32":
        kernel32.CloseHandle(_mutex_handle)
    _mutex_handle = None


def notify_already_running() -> None:
    """Tell the user a copy is already running (no Tk required)."""
    if sys.platform != "win32":
        return
    MB_OK = 0x00000000
    user32.MessageBoxW.argtypes = (
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    )
    user32.MessageBoxW.restype = int
    user32.MessageBoxW(
        None,
        "DicTide is already running.\n"
        "Check the system tray or taskbar.",
        "DicTide",
        MB_OK,
    )
