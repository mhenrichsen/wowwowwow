"""Clicks on the WoW window. The client reads the REAL cursor position when a
button message arrives (a posted click alone lands wherever the cursor is),
so the cursor is moved to the spot first. Used only for the login/character
screens, where no Lua chat command is available."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

u32 = ctypes.WinDLL("user32")
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)     # real pixels, no scaling
except Exception:
    pass
u32.PostMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


def click(hwnd: int, x: int, y: int, double: bool = False):
    """Left-click at client pixel (x, y) of the WoW window."""
    pt = wt.POINT(x, y)
    u32.ClientToScreen(hwnd, ctypes.byref(pt))
    old = wt.POINT()
    u32.GetCursorPos(ctypes.byref(old))
    u32.SetCursorPos(pt.x, pt.y)
    lp = (y << 16) | x
    time.sleep(0.15)
    u32.PostMessageW(hwnd, 0x0200, 0, lp)                  # WM_MOUSEMOVE
    time.sleep(0.15)
    for _ in range(2 if double else 1):
        u32.PostMessageW(hwnd, 0x0201, 1, lp)              # WM_LBUTTONDOWN
        time.sleep(0.08)
        u32.PostMessageW(hwnd, 0x0202, 0, lp)              # WM_LBUTTONUP
        time.sleep(0.1)
    time.sleep(0.2)
    u32.SetCursorPos(old.x, old.y)                         # give the cursor back


def hover(hwnd: int, x: int, y: int):
    """Put the cursor over client pixel (x, y) (leaves it there)."""
    pt = wt.POINT(x, y)
    u32.ClientToScreen(hwnd, ctypes.byref(pt))
    u32.SetCursorPos(pt.x, pt.y)
    u32.PostMessageW(hwnd, 0x0200, 0, (y << 16) | x)       # WM_MOUSEMOVE


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("mi", _MOUSEINPUT)]


def _send_mouse(flags: int):
    inp = _INPUT(0, _MOUSEINPUT(0, 0, 0, flags, 0, None))
    u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _focus(hwnd: int, old_fg: int) -> bool:
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.12)
    if u32.GetForegroundWindow() == hwnd:
        return True
    # Windows refuses focus changes from a background process unless the
    # caller's thread shares input state with the foreground thread
    fg_thread = u32.GetWindowThreadProcessId(old_fg, None)
    me = ctypes.WinDLL("kernel32").GetCurrentThreadId()
    u32.AttachThreadInput(me, fg_thread, True)
    u32.SetForegroundWindow(hwnd)
    u32.AttachThreadInput(me, fg_thread, False)
    time.sleep(0.12)
    if u32.GetForegroundWindow() == hwnd:
        return True
    # last resort: a tap on ALT makes us the "last input" process, which
    # lifts Windows' foreground lock for the next SetForegroundWindow
    u32.keybd_event(0x12, 0, 0, 0)
    u32.keybd_event(0x12, 0, 2, 0)
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    return u32.GetForegroundWindow() == hwnd


def real_left_click(hwnd: int, x: int, y: int, hold: float = 0.06) -> bool:
    """A genuine left-click at client pixel (x, y) (UI buttons such as loot
    slots, which ignore posted messages too)."""
    old_fg = u32.GetForegroundWindow()
    old = wt.POINT()
    u32.GetCursorPos(ctypes.byref(old))
    if not _focus(hwnd, old_fg):
        return False
    hover(hwnd, x, y)
    time.sleep(0.08)
    _send_mouse(0x0002)                                    # MOUSEEVENTF_LEFTDOWN
    time.sleep(hold)
    _send_mouse(0x0004)                                    # MOUSEEVENTF_LEFTUP
    time.sleep(0.1)
    if old_fg and old_fg != hwnd:
        u32.SetForegroundWindow(old_fg)
    u32.SetCursorPos(old.x, old.y)
    return True


def real_right_click(hwnd: int, x: int, y: int, hold: float = 0.06) -> bool:
    """A genuine right-click at client pixel (x, y), as the user would do it.

    Posted button messages never reach the client's world interaction, so the
    window is brought to the front for a moment and a real button press is
    injected; the previous foreground window and the cursor are then given
    back. Used for quest objects (crates, plants), which the client only opens
    via its own click handling (the hidden "Opening" cast). Returns False if
    the window could not be focused."""
    old_fg = u32.GetForegroundWindow()
    old = wt.POINT()
    u32.GetCursorPos(ctypes.byref(old))
    if not _focus(hwnd, old_fg):
        return False
    hover(hwnd, x, y)
    time.sleep(0.08)
    _send_mouse(0x0008)                                    # MOUSEEVENTF_RIGHTDOWN
    time.sleep(hold)
    _send_mouse(0x0010)                                    # MOUSEEVENTF_RIGHTUP
    time.sleep(0.1)
    if old_fg and old_fg != hwnd:
        u32.SetForegroundWindow(old_fg)
    u32.SetCursorPos(old.x, old.y)
    return True


def right_click(hwnd: int, x: int, y: int):
    """Right-click at client pixel (x, y): interact, like a player does."""
    old = wt.POINT()
    u32.GetCursorPos(ctypes.byref(old))
    hover(hwnd, x, y)
    time.sleep(0.15)
    lp = (y << 16) | x
    u32.PostMessageW(hwnd, 0x0204, 2, lp)                  # WM_RBUTTONDOWN
    time.sleep(0.08)
    u32.PostMessageW(hwnd, 0x0205, 0, lp)                  # WM_RBUTTONUP
    time.sleep(0.2)
    u32.SetCursorPos(old.x, old.y)                         # give the cursor back
