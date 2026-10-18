"""Window capture + posted keyboard input for the modern (WowB.exe) client."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

import numpy as np

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32")
u32.PostMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
u32.MapVirtualKeyW.argtypes = (wt.UINT, wt.UINT)
u32.PrintWindow.argtypes = (wt.HWND, wt.HDC, wt.UINT)
g32.GetDIBits.argtypes = (wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT)


def find_window() -> int:
    hwnd = u32.FindWindowW("waApplication Window", None) or u32.FindWindowW(None, "World of Warcraft")
    if not hwnd:
        raise RuntimeError("WowB window not found")
    return hwnd


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class Capture:
    """PrintWindow into a reusable bitmap; read the top `rows` scanlines or all."""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self._alloc()

    def _alloc(self):
        r = wt.RECT()
        u32.GetClientRect(self.hwnd, ctypes.byref(r))
        self.w, self.h = r.right, r.bottom
        hdc = u32.GetDC(self.hwnd)
        self.mdc = g32.CreateCompatibleDC(hdc)
        self.bmp = g32.CreateCompatibleBitmap(hdc, self.w, self.h)
        g32.SelectObject(self.mdc, self.bmp)
        u32.ReleaseDC(self.hwnd, hdc)

    def grab(self, rows: int | None = None) -> np.ndarray:
        r = wt.RECT()
        u32.GetClientRect(self.hwnd, ctypes.byref(r))
        if (r.right, r.bottom) != (self.w, self.h):
            g32.DeleteObject(self.bmp); g32.DeleteDC(self.mdc)
            self._alloc()
        u32.PrintWindow(self.hwnd, self.mdc, 1 | 2)
        n = min(rows or self.h, self.h)
        # bottom-up DIB: scan lines h-n..h-1 are the top n rows of the window
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), self.w, self.h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = np.empty((n, self.w, 4), np.uint8)
        g32.GetDIBits(self.mdc, self.bmp, self.h - n, n, buf.ctypes.data, ctypes.byref(bi), 0)
        return buf[::-1, :, [2, 1, 0]]                                 # flip, BGRX -> RGB


WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
VK = {
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    **{str(i): 0x30 + i for i in range(10)},
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{f"num{i}": 0x60 + i for i in range(10)},
    "num*": 0x6A, "num/": 0x6F, "num-": 0x6D, "num+": 0x6B, "num.": 0x6E,
    "home": 0x24, "end": 0x23, "insert": 0x2D,
    # BotEye BotEyeRXPNpc (RXP step NPC, RXP_PLAN.md section 4): PAGEUP first, DELETE / PAGEDOWN as fallbacks. Posted
    # without the extended bit like INSERT (HOME / END carry it and their overrides never fired on this account).
    "pageup": 0x21, "pagedown": 0x22, "delete": 0x2E,
}
EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x6F, 0x24, 0x23}


def _lparam(vk: int, up: bool) -> int:
    scan = u32.MapVirtualKeyW(vk, 0)
    lp = 1 | (scan << 16)
    if vk in EXTENDED:
        lp |= 1 << 24
    if up:
        lp |= (1 << 30) | (1 << 31)
    return lp


class NotInWorld(RuntimeError):
    """The BotEye strip is not on screen (login/loading screen): send nothing."""


class Keys:
    def __init__(self, hwnd: int, in_world=None):
        self.hwnd = hwnd
        self.held: set[str] = set()
        self.in_world = in_world        # callable -> bool; None = unguarded
        self.before: dict = {}          # key -> callable run just before that key goes down
        self.after: dict = {}           # key -> callable run 0.25 s after that key comes up

    def _check(self):
        if self.in_world is not None and not self.in_world():
            self.release_all()
            raise NotInWorld("strip not visible - not sending keys")

    def down(self, k: str):
        self._check()
        if k in self.before:
            self.before[k]()
        vk = VK[k]
        u32.PostMessageW(self.hwnd, WM_KEYDOWN, vk, _lparam(vk, False))
        self.held.add(k)

    def up(self, k: str):
        vk = VK[k]
        u32.PostMessageW(self.hwnd, WM_KEYUP, vk, _lparam(vk, True))
        self.held.discard(k)
        if k in self.after:
            time.sleep(0.25)            # sent messages jump the posted queue: let the key be handled first
            self.after[k]()

    def release_all(self):
        for k in list(self.held):
            vk = VK[k]
            u32.PostMessageW(self.hwnd, WM_KEYUP, vk, _lparam(vk, True))
        self.held.clear()

    def press(self, k: str, hold: float = 0.04):
        self.down(k)
        time.sleep(hold)
        self.up(k)
        time.sleep(0.02)

    def type_text(self, text: str):
        self._check()
        for ch in text:
            u32.PostMessageW(self.hwnd, WM_CHAR, ord(ch), 1)
            time.sleep(0.012)

    def chat(self, line: str):
        self._check()
        self.press("enter")
        time.sleep(0.3)
        self.type_text(line)
        time.sleep(0.15)
        self.press("enter")
        time.sleep(0.25)
