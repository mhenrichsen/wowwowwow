"""Capture the WoW window to a PNG (works while the window is in the background)."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys

from PIL import Image

from wowmem import find_wow_window

u32 = ctypes.WinDLL("user32")
g32 = ctypes.WinDLL("gdi32")
PW_RENDERFULLCONTENT = 0x2


def capture(path: str, hwnd: int | None = None, scale: float = 1.0) -> str:
    hwnd = hwnd or find_wow_window()
    r = wt.RECT()
    u32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    hdc = u32.GetDC(hwnd)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mdc, bmp)
    # PW_CLIENTONLY (1) | PW_RENDERFULLCONTENT (2): captures D3D content too.
    u32.PrintWindow(hwnd, mdc, 1 | PW_RENDERFULLCONTENT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                    ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    g32.DeleteObject(bmp)
    g32.DeleteDC(mdc)
    u32.ReleaseDC(hwnd, hdc)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    if scale != 1.0:
        img = img.resize((int(w * scale), int(h * scale)))
    img.save(path)
    return path


if __name__ == "__main__":
    print(capture(sys.argv[1] if len(sys.argv) > 1 else "wow.png",
                  scale=float(sys.argv[2]) if len(sys.argv) > 2 else 0.6))
