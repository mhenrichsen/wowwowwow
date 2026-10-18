"""Background keyboard input: post key messages straight to the WoW window.

The 1.12.1 client reads keyboard input from its window message queue, so
PostMessage works while the window is unfocused - you can keep using the PC.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.PostMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
u32.MapVirtualKeyW.argtypes = (wt.UINT, wt.UINT)

WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102

VK = {
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "-": 0xBD, "=": 0xBB,
    **{str(i): 0x30 + i for i in range(10)},
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{f"num{i}": 0x60 + i for i in range(10)},          # VK_NUMPAD0..9
}


def _lparam(vk: int, up: bool) -> int:
    scan = u32.MapVirtualKeyW(vk, 0)
    lp = 1 | (scan << 16)
    if up:
        lp |= (1 << 30) | (1 << 31)
    return lp


class Keyboard:
    def __init__(self, hwnd: int):
        self.hwnd = hwnd

    def down(self, key: str):
        vk = VK[key]
        u32.PostMessageW(self.hwnd, WM_KEYDOWN, vk, _lparam(vk, False))

    def up(self, key: str):
        vk = VK[key]
        u32.PostMessageW(self.hwnd, WM_KEYUP, vk, _lparam(vk, True))

    def press(self, key: str, hold: float = 0.03):
        # the client acts on WM_KEYDOWN as it dispatches it, so the hold only
        # has to outlast one frame; at 0.05 + 0.03 a five-key bridge command
        # (F11, two digits, an argument, F12) took 0.4 s before its own wait
        self.down(key)
        time.sleep(hold)
        self.up(key)
        time.sleep(0.02)

    def type_text(self, text: str):
        for ch in text:
            u32.PostMessageW(self.hwnd, WM_CHAR, ord(ch), 1)
            time.sleep(0.015)

    def chat(self, line: str):
        """Open the chat box, type a line (e.g. a slash command) and send it."""
        self.press("enter")
        time.sleep(0.35)          # the edit box needs a frame or two to open
        self.type_text(line)
        time.sleep(0.2)
        self.press("enter")
        time.sleep(0.3)


class NotInWorld(Exception):
    """Raised instead of sending input while the client is not in the world.

    Keys sent at the login / character screen would type into the account
    and password boxes and submit them, so nothing may be sent there.
    """


class GuardedKeyboard(Keyboard):
    def __init__(self, hwnd: int, in_world):
        super().__init__(hwnd)
        self._in_world = in_world

    def _check(self):
        if not self._in_world():
            raise NotInWorld()

    def press(self, key: str, hold: float = 0.03):
        self._check()
        super().press(key, hold)

    def down(self, key: str):
        self._check()
        super().down(key)

    def up(self, key: str):
        super().up(key)                       # always allowed: never leave a key held

    def chat(self, line: str):
        self._check()
        super().chat(line)
