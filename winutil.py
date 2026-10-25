# -*- coding: utf-8 -*-
"""Windows 系统能力封装：DPI、剪贴板、全局鼠标/键盘钩子、热键、窗口与开机自启。

这个模块不依赖任何第三方库，全部使用 ctypes 调用 Win32 API。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
winmm = ctypes.WinDLL("winmm", use_last_error=True)

# ---------------------------------------------------------------------------
# 基础类型 / 常量
# ---------------------------------------------------------------------------
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP = 0x8000
VK_ESCAPE = 0x1B
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_Q = 0x51
VK_W = 0x57
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
CF_UNICODETEXT = 13
CF_TEXT = 1
CF_OEMTEXT = 7
CF_HDROP = 15
CF_DIB = 8
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
DWM_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 这些格式不是普通 HGLOBAL，无法安全地原样备份/还原，直接跳过。
_SKIP_CLIPBOARD_FORMATS = {
    2,    # CF_BITMAP (HBITMAP)
    3,    # CF_METAFILEPICT
    9,    # CF_PALETTE
    14,   # CF_ENHMETAFILE
    16,   # CF_LOCALE（部分应用还原时会出错）
    0x80, # CF_OWNERDISPLAY
    0x81, # CF_DSPTEXT
    0x82, # CF_DSPBITMAP
    0x83, # CF_DSPMETAFILEPICT
    0x8E, # CF_DSPENHMETAFILE
}
# 0x0200 - 0x03FF 是私有格式，也跳过。
for _i in range(0x200, 0x400):
    _SKIP_CLIPBOARD_FORMATS.add(_i)


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wt.DWORD)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD),
                ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

# ---------------------------------------------------------------------------
# 函数原型
# ---------------------------------------------------------------------------
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wt.DWORD
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetDoubleClickTime.argtypes = []
user32.GetDoubleClickTime.restype = wt.UINT
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.SetWindowRgn.argtypes = [wt.HWND, wt.HANDLE, wt.BOOL]
user32.SetWindowRgn.restype = ctypes.c_int
user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wt.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wt.BOOL
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
user32.EnumClipboardFormats.argtypes = [wt.UINT]
user32.EnumClipboardFormats.restype = wt.UINT
user32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wt.UINT
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.RegisterHotKey.restype = wt.BOOL
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wt.BOOL
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, WPARAM, LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.TranslateMessage.restype = wt.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, WPARAM, LPARAM]
user32.PostThreadMessageW.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(POINT)]
user32.ScreenToClient.restype = wt.BOOL
user32.MonitorFromPoint.argtypes = [POINT, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wt.BOOL
user32.SetProcessDPIAware.argtypes = []
user32.SetProcessDPIAware.restype = wt.BOOL
user32.GetDpiForSystem.argtypes = []
user32.GetDpiForSystem.restype = wt.UINT
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wt.BOOL
user32.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
user32.MessageBoxW.restype = ctypes.c_int
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalSize.argtypes = [wt.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                                wt.LPWSTR,
                                                ctypes.POINTER(wt.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL

gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.CreateRoundRectRgn.restype = wt.HANDLE
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.DeleteObject.restype = wt.BOOL

winmm.mciSendStringW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.UINT, wt.HWND]
winmm.mciSendStringW.restype = wt.UINT
winmm.mciGetErrorStringW.argtypes = [wt.UINT, wt.LPWSTR, wt.UINT]
winmm.mciGetErrorStringW.restype = wt.BOOL


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def set_dpi_awareness() -> None:
    """在创建 Tk 窗口前调用，避免高分屏发虚。"""
    try:
        # PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def get_system_dpi() -> int:
    try:
        return int(user32.GetDpiForSystem()) or 96
    except Exception:
        return 96


def set_app_user_model_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "HuaCiFanYi.Translator.1"
        )
    except Exception:
        pass


def get_cursor_pos() -> tuple[int, int]:
    pt = POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return int(pt.x), int(pt.y)
    return 0, 0


def get_foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def get_root_window(hwnd: int) -> int:
    if not hwnd:
        return 0
    return int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)


def _window_class(hwnd: int) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    n = user32.GetClassNameW(hwnd, buf, 256)
    return buf.value if n else ""


def get_process_name(hwnd: int) -> str:
    if not hwnd:
        return ""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size)
        ):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def get_work_area(x: int | None = None, y: int | None = None) -> tuple[int, int, int, int]:
    if x is None or y is None:
        x, y = get_cursor_pos()
    pt = POINT(int(x), int(y))
    mon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        r = mi.rcWork
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = RECT()
    if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    return 0, 0, 0, 0


def point_in_client(hwnd: int, x: int, y: int) -> bool:
    """判断屏幕坐标是否落在窗口客户区（用于排除标题栏/边框拖动）。"""
    if not hwnd:
        return False
    pt = POINT(int(x), int(y))
    if not user32.ScreenToClient(wt.HWND(hwnd), ctypes.byref(pt)):
        return True
    r = RECT()
    if not user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(r)):
        return True
    return r.left <= pt.x < r.right and r.top <= pt.y < r.bottom


def set_round_region(hwnd: int, radius: int) -> None:
    """用窗口区域裁出真正的圆角（比 DWM 圆角更可靠，适用于无边框窗口）。"""
    if not hwnd:
        return
    left, top, right, bottom = get_window_rect(hwnd)
    w = max(1, right - left)
    h = max(1, bottom - top)
    r = max(2, int(radius))
    region = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r * 2, r * 2)
    if region:
        # 成功后 region 归系统所有，不要 DeleteObject。
        user32.SetWindowRgn(wt.HWND(hwnd), region, True)


def apply_rounded_corners(hwnd: int) -> None:
    """Windows 11 圆角；其他系统静默失败。"""
    if sys.getwindowsversion().build < 22000:
        return
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        pref = ctypes.c_int(DWMWCP_ROUND)
        dwmapi.DwmSetWindowAttribute(
            wt.HWND(hwnd), DWM_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref),
        )
    except Exception:
        pass


def keep_topmost_noactivate(hwnd: int) -> None:
    try:
        user32.SetWindowPos(
            wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    except Exception:
        pass


def resize_window(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """绕过 Tk 几何管理器直接改窗口矩形，用于流畅的窗口动画。"""
    if not hwnd:
        return
    try:
        user32.SetWindowPos(
            wt.HWND(hwnd), None, int(x), int(y), int(w), int(h),
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
    except Exception:
        pass


def message_box(text: str, title: str = "划词翻译", flags: int = 0x40) -> None:
    try:
        user32.MessageBoxW(None, str(text), str(title), flags)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 剪贴板
# ---------------------------------------------------------------------------
def _open_clipboard(retries: int = 12, delay: float = 0.012) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def read_clipboard_text() -> str:
    if not _open_clipboard():
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr) or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_clipboard_text(text: str) -> bool:
    data = (text + "\0").encode("utf-16-le")
    if not _open_clipboard():
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        return True
    finally:
        user32.CloseClipboard()


class ClipboardBackup:
    """把剪贴板里可安全复制的 HGLOBAL 格式全部快照，稍后原样还原。"""

    def __init__(self, items: list[tuple[int, bytes]]):
        self.items = items
        self.captured_seq = int(user32.GetClipboardSequenceNumber())

    @classmethod
    def capture(cls, max_bytes: int = 64 * 1024 * 1024) -> Optional["ClipboardBackup"]:
        if not _open_clipboard():
            log.warning("备份剪贴板失败：无法打开剪贴板")
            return None
        items: list[tuple[int, bytes]] = []
        total = 0
        fmt = 0
        try:
            while True:
                fmt = int(user32.EnumClipboardFormats(fmt))
                if not fmt:
                    break
                if fmt in _SKIP_CLIPBOARD_FORMATS:
                    continue
                handle = user32.GetClipboardData(fmt)
                if not handle:
                    continue
                size = int(kernel32.GlobalSize(handle))
                if size <= 0:
                    continue
                total += size
                if total > max_bytes:
                    log.info("剪贴板内容超过 %d MB，跳过自动取词", max_bytes // 1024 // 1024)
                    return None
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    continue
                try:
                    data = ctypes.string_at(ptr, size)
                finally:
                    kernel32.GlobalUnlock(handle)
                items.append((fmt, data))
        finally:
            user32.CloseClipboard()
        return cls(items)

    def restore(self) -> bool:
        if not _open_clipboard():
            log.warning("还原剪贴板失败：无法打开剪贴板")
            return False
        try:
            if not user32.EmptyClipboard():
                return False
            # Unicode 文本最后写，确保它是最完整的。
            ordered = sorted(self.items, key=lambda x: 1 if x[0] == CF_UNICODETEXT else 0)
            for fmt, data in ordered:
                handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not handle:
                    continue
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    continue
                try:
                    ctypes.memmove(ptr, data, len(data))
                finally:
                    kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(fmt, handle):
                    log.debug("还原剪贴板格式 %s 失败", fmt)
            return True
        finally:
            user32.CloseClipboard()


def _send_key(vk: int, down: bool) -> None:
    inp = INPUT()
    inp.type = 1  # INPUT_KEYBOARD
    flags = 0 if down else 0x0002  # KEYEVENTF_KEYUP
    inp.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_ctrl_c() -> None:
    _send_key(VK_CONTROL, True)
    time.sleep(0.018)
    _send_key(ord("C"), True)
    time.sleep(0.018)
    _send_key(ord("C"), False)
    time.sleep(0.018)
    _send_key(VK_CONTROL, False)


def is_modifier_down() -> bool:
    for vk in (VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            return True
    return False


def wait_modifiers_released(timeout: float = 0.55) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_modifier_down():
            return True
        time.sleep(0.012)
    return not is_modifier_down()


def capture_selection(settle: float = 0.045,
                      wait_mods: float = 0.55,
                      copy_timeout: float = 0.9) -> tuple[str, str]:
    """向当前焦点窗口发送 Ctrl+C，取出选中文字，并尽力还原原剪贴板。

    返回 (文字, 原因)。文字为空时，原因说明失败类型。
    """
    if not wait_modifiers_released(wait_mods):
        return "", "modifiers"
    backup = ClipboardBackup.capture()
    if backup is None:
        return "", "clipboard_busy"
    if settle > 0:
        time.sleep(settle)
    before_seq = int(user32.GetClipboardSequenceNumber())
    send_ctrl_c()
    deadline = time.time() + copy_timeout
    text = ""
    while time.time() < deadline:
        time.sleep(0.014)
        if int(user32.GetClipboardSequenceNumber()) == before_seq:
            continue
        current = read_clipboard_text()
        if current and current.strip():
            text = current
            break
    # 立刻还原：剪贴板序列号已经变化且 Unicode 文本已经就绪，
    # 此时不会影响我们读取；还原能让用户原来的剪贴板内容不丢失。
    if not text or read_clipboard_text() == text:
        backup.restore()
    # 清除可能夹带的 NUL 和首尾空白
    text = (text or "").replace("\x00", "").strip("\r\n\t ")
    return text, ("ok" if text else "empty")


def _run_powershell_get_clipboard() -> None:  # 仅调试用，不参与程序逻辑
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            creationflags=0x08000000, check=False,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 音频发音（有道音频，MCI 直接播放 URL）
# ---------------------------------------------------------------------------
_audio_lock = threading.Lock()
_audio_alias = "hcfy_audio"


def _mci(cmd: str, buf: ctypes.Array, size: int) -> int:
    return int(winmm.mciSendStringW(cmd, buf, size, None))


def play_audio_url(url: str) -> bool:
    if not url:
        return False

    def worker() -> None:
        with _audio_lock:
            buf = ctypes.create_unicode_buffer(512)
            try:
                _mci(f"close {_audio_alias}", buf, 512)
                err = _mci(
                    f'open "{url}" type mpegvideo alias {_audio_alias}',
                    buf, 512,
                )
                if err:
                    ebuf = ctypes.create_unicode_buffer(512)
                    winmm.mciGetErrorStringW(err, ebuf, 512)
                    log.info("发音打开失败：%s", ebuf.value)
                    return
                err = _mci(f"play {_audio_alias}", buf, 512)
                if err:
                    _mci(f"close {_audio_alias}", buf, 512)
            except Exception:
                log.exception("发音播放异常")

    threading.Thread(target=worker, name="audio", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# 全局输入钩子
# ---------------------------------------------------------------------------
@dataclass
class HookEvent:
    kind: str                    # select / double / outside / escape / hotkey
    x: int = 0
    y: int = 0
    manual: bool = False
    hotkey_id: int = 0


class GlobalInput:
    """全局鼠标 + 键盘钩子，以及 Alt+Q / Alt+W 热键。

    所有回调都在钩子线程里执行，回调本身必须非常快（通常只是放入队列）。
    """

    def __init__(
        self,
        on_event: Callable[[HookEvent], None],
        ui_windows: Callable[[], tuple[int, ...]],
        popup_visible: Callable[[], bool],
        auto_enabled: Callable[[], bool],
    ):
        self.on_event = on_event
        self.ui_windows = ui_windows
        self.popup_visible = popup_visible
        self.auto_enabled = auto_enabled
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._mouse_hook = None
        self._keyboard_hook = None
        self._running = False
        self._mouse_proc = HOOKPROC(self._mouse_callback)
        self._key_proc = HOOKPROC(self._key_callback)
        self._down_time = 0.0
        self._down_pos = (0, 0)
        self._down_root = 0
        self._down_foreground = 0
        self._down_is_ui = False
        self._down_client = True
        self._moved = 0
        self._last_down_time = 0.0
        self._last_down_pos = (0, 0)
        self._double_pending = False
        self._last_fire = 0.0
        self.hotkeys: dict[int, str] = {}

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._thread_main, name="global-input", daemon=True
        )
        self._thread.start()
        self._ready.wait(2.0)

    def stop(self) -> None:
        self._running = False
        if self._thread_id:
            try:
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.2)

    # ------------------------------------------------------------------
    def _thread_main(self) -> None:
        self._thread_id = int(kernel32.GetCurrentThreadId())
        hinst = kernel32.GetModuleHandleW(None)
        try:
            self._mouse_hook = user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._mouse_proc, hinst, 0
            )
            if not self._mouse_hook:
                log.error("安装鼠标钩子失败：%s", ctypes.get_last_error())
        except Exception:
            log.exception("安装鼠标钩子异常")
        try:
            self._keyboard_hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._key_proc, hinst, 0
            )
            if not self._keyboard_hook:
                log.error("安装键盘钩子失败：%s", ctypes.get_last_error())
        except Exception:
            log.exception("安装键盘钩子异常")

        self._register_hotkeys()
        self._ready.set()

        msg = wt.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            if msg.message == WM_HOTKEY:
                self.on_event(HookEvent("hotkey", hotkey_id=int(msg.wParam)))
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # 从消息循环退出后清理
        try:
            if self._mouse_hook:
                user32.UnhookWindowsHookEx(self._mouse_hook)
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
        except Exception:
            pass
        for hid in list(self.hotkeys):
            try:
                user32.UnregisterHotKey(None, hid)
            except Exception:
                pass
        self._mouse_hook = None
        self._keyboard_hook = None

    def _register_hotkeys(self) -> None:
        specs = [
            (101, "Alt+Q", MOD_ALT | MOD_NOREPEAT, VK_Q, "Ctrl+Alt+Q",
             MOD_CONTROL | MOD_ALT | MOD_NOREPEAT),
            (102, "Alt+W", MOD_ALT | MOD_NOREPEAT, VK_W, "Ctrl+Alt+W",
             MOD_CONTROL | MOD_ALT | MOD_NOREPEAT),
        ]
        for hid, label, mods, vk, alt_label, alt_mods in specs:
            if user32.RegisterHotKey(None, hid, mods, vk):
                self.hotkeys[hid] = label
            elif user32.RegisterHotKey(None, hid, alt_mods, vk):
                self.hotkeys[hid] = alt_label
            else:
                log.warning("注册热键失败：%s", label)
                self.hotkeys[hid] = ""

    # ------------------------------------------------------------------
    def _ui_roots(self) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in self.ui_windows() if x)
        except Exception:
            return ()

    def _is_ui(self, hwnd: int) -> bool:
        root = get_root_window(hwnd)
        return bool(root) and root in self._ui_roots()

    def _should_ignore_process(self, hwnd: int) -> bool:
        name = get_process_name(hwnd)
        if name in {
            "explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe",
            "shellexperiencehost.exe", "lockapp.exe", "textinputhost.exe",
            "pythonw.exe", "python.exe",
        }:
            return True
        cls = _window_class(hwnd)
        return cls in {
            "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
            "NotifyIconOverflowWindow", "TaskListThumbnailWnd",
        }

    # ------------------------------------------------------------------
    def _mouse_callback(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code < 0:
            return int(user32.CallNextHookEx(None, n_code, w_param, l_param))
        try:
            if w_param == WM_LBUTTONDOWN:
                self._on_down()
            elif w_param == WM_MOUSEMOVE:
                if self._down_time:
                    data = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    dx = data.pt.x - self._down_pos[0]
                    dy = data.pt.y - self._down_pos[1]
                    dist = dx * dx + dy * dy
                    if dist > self._moved:
                        self._moved = dist
            elif w_param == WM_LBUTTONUP:
                self._on_up()
        except Exception:
            log.exception("鼠标钩子异常")
        return int(user32.CallNextHookEx(None, n_code, w_param, l_param))

    def _on_down(self) -> None:
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        now = time.time()
        hwnd = int(user32.WindowFromPoint(pt) or 0)
        fg = int(user32.GetForegroundWindow() or 0)
        self._down_time = now
        self._down_pos = (pt.x, pt.y)
        self._down_root = get_root_window(hwnd)
        self._down_foreground = fg
        self._down_is_ui = self._is_ui(hwnd)
        self._down_client = point_in_client(self._down_root, pt.x, pt.y)
        self._moved = 0
        log.debug("鼠标按下 pt=%s,%s hwnd=%s fg=%s ui=%s client=%s",
                  pt.x, pt.y, hwnd, fg, self._down_is_ui, self._down_client)
        if self._down_is_ui:
            self._double_pending = False
            return
        if self.popup_visible():
            self.on_event(HookEvent("outside", pt.x, pt.y))
        dbl_ms = int(user32.GetDoubleClickTime())
        same_spot = (
            abs(pt.x - self._last_down_pos[0]) <= 4
            and abs(pt.y - self._last_down_pos[1]) <= 4
        )
        within = (now - self._last_down_time) * 1000 <= dbl_ms
        self._double_pending = bool(within and same_spot)
        self._last_down_time = now
        self._last_down_pos = (pt.x, pt.y)

    def _on_up(self) -> None:
        if not self._down_time:
            return
        down_time = self._down_time
        down_pos = self._down_pos
        foreground = self._down_foreground
        is_ui = self._down_is_ui
        client = self._down_client
        moved2 = self._moved
        self._down_time = 0.0
        if is_ui or not client:
            return
        if not self.auto_enabled():
            return
        if is_modifier_down():
            return
        now = time.time()
        log.debug("鼠标抬起 moved2=%s ui=%s auto=%s mods=%s elapsed=%.3f",
                  moved2, is_ui, self.auto_enabled(), is_modifier_down(),
                  now - down_time)
        if now - self._last_fire < 0.18:
            return
        # 拖拽划词
        if moved2 >= 36 and (now - down_time) >= 0.045:
            if self._should_ignore_process(foreground):
                return
            self._last_fire = now
            self.on_event(HookEvent("select", int(down_pos[0]), int(down_pos[1])))
            return
        # 双击取词（第二次按下时 _double_pending 已标记）
        if self._double_pending and moved2 < 36:
            if self._should_ignore_process(foreground):
                return
            self._double_pending = False
            self._last_fire = now
            self.on_event(HookEvent("double", int(down_pos[0]), int(down_pos[1])))

    def _key_callback(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code < 0:
            return int(user32.CallNextHookEx(None, n_code, w_param, l_param))
        try:
            if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                data = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if int(data.vkCode) == VK_ESCAPE and self.popup_visible():
                    self.on_event(HookEvent("escape"))
                    return 1
        except Exception:
            log.exception("键盘钩子异常")
        return int(user32.CallNextHookEx(None, n_code, w_param, l_param))


# ---------------------------------------------------------------------------
# 单实例 / 开机自启 / 文件
# ---------------------------------------------------------------------------
_mutex_handle = None


def acquire_single_instance(name: str = "Local\\HuaCiFanYi.SingleInstance") -> bool:
    global _mutex_handle
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def _pythonw_path() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    if exe.name.lower() == "pythonw.exe":
        return str(exe)
    # pyw / 其他启动器：尝试同目录 pythonw
    cand = exe.with_name("pythonw.exe")
    if cand.exists():
        return str(cand)
    return str(exe)


def set_autostart(enabled: bool, script_path: str, value_name: str = "划词翻译") -> bool:
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                if getattr(sys, "frozen", False):
                    # 打包后的 exe 直接启动自己，不依赖 Python。
                    command = f'"{Path(sys.executable).resolve()}"'
                else:
                    command = f'"{_pythonw_path()}" "{script_path}"'
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, value_name)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        log.exception("设置开机自启失败")
        return False


def get_autostart(value_name: str = "划词翻译") -> bool:
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ
        ) as key:
            try:
                winreg.QueryValueEx(key, value_name)
                return True
            except FileNotFoundError:
                return False
    except Exception:
        return False


def open_folder(path: str | Path) -> None:
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        log.exception("打开文件夹失败")


def copy_text(text: str) -> bool:
    """写入剪贴板（会覆盖当前剪贴板，用于用户主动点“复制”）。"""
    return _set_clipboard_text(text)


def set_clipboard_text(text: str) -> bool:
    return _set_clipboard_text(text)


