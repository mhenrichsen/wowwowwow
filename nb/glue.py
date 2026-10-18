"""Read the WoW glue screens (login, game-account pick, character select, disconnect dialogs) with Windows OCR
of the client window alone (PrintWindow, never the desktop)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

from PIL import Image

from .win import Capture

HERE = os.path.dirname(os.path.abspath(__file__))
OCR = os.path.join(HERE, "ocr.ps1")


def read_text(hwnd: int, scale: float = 1.0) -> list[dict]:
    """OCR lines [{t, x, y, w, h}] in client-pixel coordinates."""
    a = Capture(hwnd).grab()
    img = Image.fromarray(a)
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    fd, path = tempfile.mkstemp(suffix=".png", prefix=f"glue{hwnd:x}_")
    os.close(fd)
    try:
        img.save(path)
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", OCR, path],
                             capture_output=True, encoding="utf-8", errors="replace", timeout=60).stdout.strip()
    finally:
        os.remove(path)
    try:
        lines = json.loads(out, strict=False) if out.startswith("[") else []
    except ValueError:
        lines = []
    for l in lines:
        for k in ("x", "y", "w", "h"):
            l[k] = int(l[k] / scale)
    return lines


def screen(lines: list[dict]) -> str:
    """Name the glue screen from its text."""
    txt = " | ".join(l["t"].lower() for l in lines)
    if "gameplay ruleset" in txt:
        return "ruleset"                        # first login on an account: Normal / PvP / Roleplay / Hardcore
    if "full name" in txt or "type main name" in txt:
        return "name"                           # character creation: customize + name page
    if "skin color" in txt and ("hair style" in txt or "face" in txt) and "horde" not in txt:
        return "name"                           # ... its heading OCR'd badly ("Fun Na | Back | otne", inst2 12:04-12:07
        #                                         2026-10-16: read as unknown, Esc back to the race page, 6 loops)
    if "alliance" in txt and "horde" in txt and "customize" in txt:
        return "race"                           # character creation: race / class page
    if any(k in txt for k in ("disconnected", "lost connection", "unable to connect", "connection failed",
                              "you have been", "server is down", "timed out")):
        return "dialog"
    if "enter world" in txt:
        return "charselect"
    if "create new character" in txt or "create character" in txt:
        return "charselect"
    if "wow1" in txt or "wow2" in txt or "select a game account" in txt or "game account" in txt:
        return "accounts"
    if "connecting" in txt or "authenticating" in txt or "logging in" in txt or "retrieving" in txt \
            or "loading" in txt:
        return "busy"
    # the login screen needs its own controls: a lone "remember" matched the in-world RestedXP welcome ("Remember to
    # act responsibly ...", client 3 20:25:14) and the password got typed into the world as keybinds
    if "act responsibly" not in txt and ("assword" in txt or "remember account" in txt or "remembljer" in txt) \
            and ("log in" in txt or "login" in txt or "@" in txt):
        return "login"
    if "okay" in txt or "cancel" in txt:
        return "dialog"
    return "unknown"


if __name__ == "__main__":
    import sys
    from .multi import find_instance_window
    h = find_instance_window(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
    ls = read_text(h)
    for l in ls:
        print(l)
    print("screen:", screen(ls))
