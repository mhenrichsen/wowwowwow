"""World -> screen projection from the client's own camera, so the bot can
click things the way a player does (game objects: click-to-move's "use
object" order is not reliable when written from outside - see questing.py).

Camera (build 5875): [[0xB4B2BC] + 0x65B8] ->
    +0x08  position x, y, z
    +0x14  3x3 rotation: row 0 = forward, row 1 = left, row 2 = up
    +0x40  field of view (radians), +0x44 aspect ratio
Verified by projecting a known object and reading the client's mouse-over
GUID back.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import struct

WORLD_FRAME = 0xB4B2BC
ACTIVE_CAMERA = 0x65B8
MOUSEOVER_GUID = 0xB4E2C8

u32 = ctypes.WinDLL("user32", use_last_error=True)


def client_size(hwnd) -> tuple[int, int]:
    r = wt.RECT()
    u32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def camera(p):
    cam = p.u32(p.u32(WORLD_FRAME) + ACTIVE_CAMERA)
    pos = struct.unpack("<3f", p.read(cam + 0x08, 12))
    m = struct.unpack("<9f", p.read(cam + 0x14, 36))
    fov, aspect = struct.unpack("<2f", p.read(cam + 0x40, 8))
    return pos, (m[0:3], m[3:6], m[6:9]), fov, aspect


def world_to_screen(p, x, y, z):
    """Client-area pixel (sx, sy) of a world point, or None if off screen.

    Calibrated by scanning the screen for known objects via the mouse-over
    GUID: the horizontal half-angle is fov * 22.5 degrees (fov 1.5708 ->
    70.7 deg wide) and the vertical follows the window's aspect ratio.
    """
    pos, (fwd, left, up), fov, _aspect = camera(p)
    d = (x - pos[0], y - pos[1], z - pos[2])
    cx = sum(a * b for a, b in zip(d, fwd))
    cy = sum(a * b for a, b in zip(d, left))
    cz = sum(a * b for a, b in zip(d, up))
    if cx <= 0.1:
        return None
    w, h = client_size(p.hwnd)
    tx = math.tan(math.radians(fov * 45.0) / 2)
    ty = tx / (w / h)
    sx = w / 2 - (cy / cx) / tx * (w / 2)
    sy = h / 2 - (cz / cx) / ty * (h / 2)
    if not (5 <= sx < w - 5 and 5 <= sy < h - 5):
        return None
    return int(sx), int(sy)


def find_on_screen(p, guid, x, y, z, heights=(0.5, 0.2, 0.9), radii=None):
    """Pixel where the mouse-over GUID is `guid`, searching around the
    projected point (objects have size; the projection is approximate).
    Moves the real cursor; the caller restores it."""
    import time
    import mouse
    # a full miss is 147 probes at 0.04 s = 6 s of hovering mid-fight; give
    # up sooner and let the caller fall back to the bridge
    deadline = time.time() + 2.5
    for dz in heights:
        s = world_to_screen(p, x, y, z + dz)
        if s is None:
            continue
        # close camera (a wall behind us) makes the projection error large
        # on screen: search wider then
        for r in ((0, 12, 24, 40, 60, 90, 130) if radii is None else radii):
            for k in range(1 if r == 0 else 8):
                if time.time() > deadline:
                    return None
                a = k * math.pi / 4
                px, py = int(s[0] + r * math.cos(a)), int(s[1] + r * math.sin(a))
                mouse.hover(p.hwnd, px, py)
                time.sleep(0.04)
                if p.u64(MOUSEOVER_GUID) == guid:
                    return px, py
    return None
