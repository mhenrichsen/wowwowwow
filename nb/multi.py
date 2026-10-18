"""Four WoW clients side by side: instance N <-> config BetaLocalN.wtf <-> one quadrant of the main screen.

  python -m nb.multi configs      write BetaLocal2..4.wtf (low graphics, windowed, quadrant size)
  python -m nb.multi launch N     start client N through D:\\tcforever\\play.py (the key patch)
  python -m nb.multi place        move every client window into its quadrant
  python -m nb.multi list         instance -> pid, hwnd, rect
Instance 1 is the client started with the stock BetaLocal.wtf.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys

GAME_DIR = r"C:\Program Files (x86)\World of Warcraft\_classic_beta_"
PLAY = r"D:\tcforever\play.py"
PY = os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Python\Python313\python.exe")
N = 4

u32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def config_name(inst: int) -> str:
    return "BetaLocal.wtf" if inst == 1 else f"BetaLocal{inst}.wtf"


def work_area():
    r = wt.RECT()
    u32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)       # SPI_GETWORKAREA, primary monitor
    return r.left, r.top, r.right, r.bottom


def quadrant(inst: int):
    """(x, y, w, h) of the outer window: 1 top left, 2 top right, 3 bottom left, 4 bottom right."""
    l, t, r, b = work_area()
    w, h = (r - l) // 2, (b - t) // 2
    i = inst - 1
    return l + (i % 2) * w, t + (i // 2) * h, w, h


LOW = {   # cheap settings so four clients fit on one GPU; the bot only needs the UI + nameplates
    "graphicsQuality": "1", "RAIDgraphicsQuality": "1", "graphicsViewDistance": "1", "graphicsShadowQuality": "0",
    "graphicsLiquidDetail": "0", "graphicsParticleDensity": "1", "graphicsSSAO": "0", "graphicsDepthEffects": "0",
    "graphicsComputeEffects": "0", "graphicsOutlineMode": "0", "graphicsTextureResolution": "0",
    "graphicsSpellDensity": "0", "graphicsProjectedTextures": "0", "graphicsEnvironmentDetail": "1",
    "graphicsGroundClutter": "0", "graphicsPBRLiquidDetail": "0", "farclip": "1500", "horizonClip": "1500",
    "sunShafts": "0", "SSAO": "0", "shadowMode": "0", "reflectionMode": "0", "refraction": "0",
    "maxFPS": "30", "maxFPSBk": "30", "vsync": "0", "gxMaximize": "0", "gxWindow": "1",
    "playIntroMovie": "0", "Sound_EnableAllSound": "0",
}


# A client reads EVERY pad, even in the background, and its gamepad cursor then drags the one system mouse
# (2026-10-14: a client at its login screen warped the cursor to its corner whenever any bot steered).
# So each client starts with the pad off and pointed at no device; Bot.claim_pad switches it on, in world,
# for its own device only. Rewritten before every launch: the client saves the in-game values on exit.
PAD_OFF = {
    "GamePadEnable": "0", "GamePadSingleActiveID": "999", "GamePadCursorOnLogin": "0",
    "GamePadCursorAutoEnable": "0", "GamePadCursorCentering": "0", "GamePadCursorForTargeting": "0",
    "GamePadTouchCursorEnable": "0",
}


def _set(lines: list[str], want: dict) -> list[str]:
    out, seen, keys = [], set(), {k.lower(): k for k in want}
    for line in lines:
        m = re.match(r'SET (\S+) "(.*)"', line)
        if m and m.group(1).lower() in keys:
            k = keys[m.group(1).lower()]
            if k not in seen:
                out.append(f'SET {m.group(1)} "{want[k]}"')
            seen.add(k)
        else:
            out.append(line)
    return out + [f'SET {k} "{v}"' for k, v in want.items() if k not in seen]


def prep_config(inst: int):
    """Before a launch: pad off (PAD_OFF) in this instance's config file."""
    path = os.path.join(GAME_DIR, "WTF", config_name(inst))
    if not os.path.exists(path):
        write_configs()
    lines = open(path, encoding="utf-8").read().splitlines()
    open(path, "w", encoding="utf-8").write("\n".join(_set(lines, PAD_OFF)) + "\n")


def write_configs():
    src = open(os.path.join(GAME_DIR, "WTF", "BetaLocal.wtf"), encoding="utf-8").read().splitlines()
    for inst in range(2, N + 1):
        _, _, w, h = quadrant(inst)
        accounts = "".join(("!" if i == inst else "") + f"WoW{i}|" for i in range(1, N + 1))
        want = dict(LOW, GxWindowedResolution=f"{w}x{h}", accountList=accounts, **PAD_OFF)
        path = os.path.join(GAME_DIR, "WTF", config_name(inst))
        open(path, "w", encoding="utf-8").write("\n".join(_set(src, want)) + "\n")
        print("wrote", path)


def clients() -> dict[int, int]:
    """instance -> pid of the running WowB.exe processes (by their -config argument)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"name='WowB.exe'\" | "
                          "% { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
                         capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        pid, cmd = line.split("\t", 1)
        m = re.search(r"-config\s+\"?BetaLocal(\d*)\.wtf", cmd, re.I)
        inst = int(m.group(1) or 1) if m else 1
        found[inst] = int(pid)
    return found


def window_of(pid: int) -> int:
    hits = []

    def cb(hwnd, _):
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(64)
            u32.GetClassNameW(hwnd, buf, 64)
            if buf.value == "waApplication Window":
                hits.append(hwnd)
        return True
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return hits[0] if hits else 0


def find_instance_window(inst: int) -> int:
    pid = clients().get(inst)
    hwnd = window_of(pid) if pid else 0
    if not hwnd:
        raise RuntimeError(f"no WoW window for instance {inst}")
    return hwnd


def place(inst: int | None = None):
    for i, pid in sorted(clients().items()):
        if inst and i != inst:
            continue
        hwnd = window_of(pid)
        if not hwnd:
            print(f"{i}: pid {pid} has no window yet"); continue
        x, y, w, h = quadrant(i)
        u32.ShowWindow(hwnd, 9)                         # SW_RESTORE (un-maximize)
        u32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0004 | 0x0010)    # NOZORDER | NOACTIVATE
        print(f"{i}: pid {pid} hwnd {hwnd:#x} -> {x},{y} {w}x{h}")


def launch(inst: int):
    prep_config(inst)
    subprocess.Popen([PY, "-u", PLAY, config_name(inst)], cwd=os.path.dirname(PLAY),
                     stdout=open(os.path.join(os.path.dirname(PLAY), "dl", f"play{inst}.out"), "w"),
                     stderr=subprocess.STDOUT, creationflags=0x08000000)
    print(f"launched client {inst} ({config_name(inst)})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "configs":
        write_configs()
    elif cmd == "launch":
        launch(int(sys.argv[2]))
    elif cmd == "place":
        place(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    else:
        for i, pid in sorted(clients().items()):
            hwnd = window_of(pid)
            r = wt.RECT(); u32.GetWindowRect(hwnd, ctypes.byref(r))
            print(f"{i}: pid {pid} hwnd {hwnd:#x} rect {r.left},{r.top},{r.right},{r.bottom}")
