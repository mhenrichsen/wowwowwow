"""Which activation messages let a background WoW read its pad, and do any of them grab the system cursor?
Each variant: pad forward + right-stick turn for 2 s with WoW in the background; reports yards moved and how far
the system cursor travelled (the user keeps the mouse still)."""
import ctypes
import ctypes.wintypes as wt
import sys
import time

from nb.eye import Eye
from nb.multi import find_instance_window
from nb.vpad import Pad
from nb.win import Keys
from nb.ev import ev

u32 = ctypes.windll.user32
inst = int(sys.argv[1]) if len(sys.argv) > 1 else 1
hwnd = find_instance_window(inst)
eye = Eye(hwnd); keys = Keys(hwnd)

if "--reload" in sys.argv:
    print("reload:", ev("ReloadUI()", eye, keys, wait=0.5)); time.sleep(10)
print("cvars:", ev("GetCVar('GamePadCursorAutoEnable')..GetCVar('GamePadCursorCentering')..GetCVar('GamePadCursorForTargeting')", eye, keys))
pad = Pad()
print("claim:", ev("tostring(BotEyePadClaim(0.08))", eye, keys) if not pad.pad.right_joystick_float(0.0, 0.08) and not pad.pad.update() else "")
pad._push()

import tkinter as tk
root = tk.Tk(); root.title("bgpad focus taker"); root.geometry("220x60+1500+760")
root.update(); root.attributes("-topmost", True); root.focus_force(); root.update()
if u32.GetForegroundWindow() == hwnd:
    u32.keybd_event(0x12, 0, 0, 0); u32.keybd_event(0x12, 0, 2, 0)
    u32.SetForegroundWindow(u32.GetParent(root.winfo_id())); root.update()
time.sleep(0.5)
print("WoW in front:", u32.GetForegroundWindow() == hwnd)

VARIANTS = {
    "none": (),
    "ACTIVATEAPP": ((0x1C, 1),),
    "ACTIVATEAPP+NCACTIVATE": ((0x1C, 1), (0x86, 1)),
    "ACTIVATE": ((0x06, 1),),
    "full (old)": ((0x1C, 1), (0x86, 1), (0x06, 1), (0x07, 0)),
}


def cursor():
    p = wt.POINT(); u32.GetCursorPos(ctypes.byref(p)); return p.x, p.y


for name, msgs in VARIANTS.items():
    s0 = eye.read(); c0 = cursor(); travel = 0.0; last = c0
    pad.fwd, pad.turn_x = 1.0, 0.6; pad._push()
    t = time.time()
    while time.time() - t < 2.0:
        for m, w in msgs:
            u32.SendMessageTimeoutW(hwnd, m, w, 0, 2, 200, None)
        root.update()
        for _ in range(4):
            time.sleep(0.025)
            c = cursor(); travel += abs(c[0] - last[0]) + abs(c[1] - last[1]); last = c
    pad.fwd, pad.turn_x = 0.0, 0.0; pad._push()
    time.sleep(0.5); root.update()
    s1 = eye.read()
    d = ((s1.x - s0.x) ** 2 + (s1.y - s0.y) ** 2) ** 0.5
    print(f"{name:24} moved {d:5.1f} yd  turned {abs((s1.facing - s0.facing + 3.1416) % 6.2832 - 3.1416):.2f} rad  "
          f"cursor travel {travel:6.0f} px")
    # a real deactivation between variants, so each starts from 'background'
    u32.SendMessageTimeoutW(hwnd, 0x1C, 0, 0, 2, 200, None)
    u32.SendMessageTimeoutW(hwnd, 0x06, 0, 0, 2, 200, None)
    time.sleep(0.5)
root.destroy()
