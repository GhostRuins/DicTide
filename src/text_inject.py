"""
Windows text injection for dictation: SendInput (Unicode) and clipboard helpers.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

VK_CONTROL = 0x11
VK_V = 0x56


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class INPUT_UNION(ctypes.Union):
    _fields_ = (
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    )


class INPUT(ctypes.Structure):
    _fields_ = (
        ("type", wintypes.DWORD),
        ("union", INPUT_UNION),
    )


def _keyboard_input(scan: int, keyup: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE
    if keyup:
        flags |= KEYEVENTF_KEYUP
    return INPUT(
        type=INPUT_KEYBOARD,
        union=INPUT_UNION(
            ki=KEYBDINPUT(
                wVk=0,
                wScan=scan & 0xFFFF,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )


def _keyboard_input_vk(wVk: int, keyup: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if keyup else 0
    return INPUT(
        type=INPUT_KEYBOARD,
        union=INPUT_UNION(
            ki=KEYBDINPUT(
                wVk=wVk & 0xFFFF,
                wScan=0,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )


def _send_input_keyboard(inputs: list[INPUT]) -> None:
    user32.SendInput.argtypes = (
        wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
    )
    user32.SendInput.restype = wintypes.UINT
    input_size = ctypes.sizeof(INPUT)
    n = user32.SendInput(len(inputs), (INPUT * len(inputs))(*inputs), input_size)
    if n != len(inputs):
        raise OSError(ctypes.get_last_error(), "SendInput did not inject all events")


def inject_text(text: str) -> None:
    """
    Type ``text`` into the foreground window using ``SendInput`` with
    ``KEYEVENTF_UNICODE`` (UTF-16 code units). Does not use the clipboard.

    Empty ``text`` is skipped (no input). Spacing and punctuation are exactly
    as given; no characters are inserted automatically.
    """
    if not text:
        return

    utf16_le = text.encode("utf-16-le")
    if len(utf16_le) % 2:
        raise ValueError("internal: utf-16-le must be even length")

    user32.SendInput.argtypes = (
        wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
    )
    user32.SendInput.restype = wintypes.UINT

    input_size = ctypes.sizeof(INPUT)
    # Batch to avoid huge single allocations; each UTF-16 unit = 2 INPUT events.
    max_events = 512
    batch: list[INPUT] = []

    for code_unit, in struct.iter_unpack("<H", utf16_le):
        batch.append(_keyboard_input(code_unit, keyup=False))
        batch.append(_keyboard_input(code_unit, keyup=True))
        if len(batch) >= max_events:
            n = user32.SendInput(len(batch), (INPUT * len(batch))(*batch), input_size)
            if n != len(batch):
                raise OSError(ctypes.get_last_error(), "SendInput did not inject all events")
            batch.clear()

    if batch:
        n = user32.SendInput(len(batch), (INPUT * len(batch))(*batch), input_size)
        if n != len(batch):
            raise OSError(ctypes.get_last_error(), "SendInput did not inject all events")


def _copy_transcript_clipboard_ctypes(text: str) -> None:
    """Set clipboard to ``text`` as CF_UNICODETEXT (UTF-16 with NUL)."""
    payload = (text + "\0").encode("utf-16-le")
    size = len(payload)

    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = ()
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = ()
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE

    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    if not user32.OpenClipboard(None):
        raise OSError(ctypes.get_last_error(), "OpenClipboard failed")
    try:
        if not user32.EmptyClipboard():
            raise OSError(ctypes.get_last_error(), "EmptyClipboard failed")
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h_mem:
            raise OSError(ctypes.get_last_error(), "GlobalAlloc failed")
        try:
            ptr = kernel32.GlobalLock(h_mem)
            if not ptr:
                raise OSError(ctypes.get_last_error(), "GlobalLock failed")
            try:
                ctypes.memmove(ptr, payload, size)
            finally:
                kernel32.GlobalUnlock(h_mem)
        except Exception:
            kernel32.GlobalFree(h_mem)
            raise
        if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            kernel32.GlobalFree(h_mem)
            raise OSError(ctypes.get_last_error(), "SetClipboardData failed")
    finally:
        user32.CloseClipboard()


def _get_clipboard_text_ctypes() -> str | None:
    """Read ``CF_UNICODETEXT`` as a Python string, or ``None`` on failure."""
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = ()
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = (wintypes.UINT,)
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    if not user32.OpenClipboard(None):
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            return None
        try:
            s = ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()
    if not s:
        return None
    return s


def copy_transcript_to_clipboard(text: str) -> None:
    """
    Copy the full transcript to the Windows clipboard as Unicode text.

    **Empty string:** the clipboard is set to an empty string (valid
    ``CF_UNICODETEXT`` with only a terminating null). Callers that prefer a no-op
    can check ``if text:`` before calling.

    Uses ``pyperclip`` when installed; otherwise ``OpenClipboard`` /
    ``SetClipboardData`` via ``ctypes``.
    """
    try:
        import pyperclip
    except ImportError:
        _copy_transcript_clipboard_ctypes(text)
    else:
        pyperclip.copy(text)


def get_clipboard_text() -> str | None:
    """
    Return the clipboard as Unicode text, or ``None`` if reading fails or the
    content is empty.

    Uses ``pyperclip.paste()`` when ``pyperclip`` is installed; otherwise
    ``OpenClipboard`` / ``GetClipboardData`` (``CF_UNICODETEXT``, UTF-16) via
    ``ctypes``.
    """
    try:
        import pyperclip
    except ImportError:
        return _get_clipboard_text_ctypes()
    try:
        s = pyperclip.paste()
    except Exception:
        return None
    if not s:
        return None
    return str(s)


def inject_via_clipboard_paste(text: str, *, restore_clipboard: bool = True) -> None:
    """
    Put ``text`` on the clipboard, simulate **Ctrl+V** with ``SendInput`` using
    virtual-key codes, then optionally restore the previous clipboard string.

    Empty ``text`` is a no-op. The prior clipboard value is read with
    `get_clipboard_text`. If that returns ``None`` (read failure or empty
    clipboard), restoration is skipped so the clipboard is left holding the
    injected ``text`` after the paste; it is not cleared to an empty string.

    Restoration uses `copy_transcript_to_clipboard` so behavior matches normal
    copy (``pyperclip`` or ctypes).
    """
    if not text:
        return
    prior = get_clipboard_text()
    copy_transcript_to_clipboard(text)
    paste_keys = [
        _keyboard_input_vk(VK_CONTROL, keyup=False),
        _keyboard_input_vk(VK_V, keyup=False),
        _keyboard_input_vk(VK_V, keyup=True),
        _keyboard_input_vk(VK_CONTROL, keyup=True),
    ]
    _send_input_keyboard(paste_keys)
    if restore_clipboard and prior is not None:
        copy_transcript_to_clipboard(prior)
