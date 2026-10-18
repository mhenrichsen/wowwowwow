"""Posted W / arrow keys to a background client, with and without fake activation; watch the system cursor."""
import collections, ctypes, ctypes.wintypes as wt, sys, time
from nb.eye import Eye
from nb.win import Keys
from nb.multi import find_instance_window
from nb.move import fake_activate

inst = int(sys.argv[1]) if len(sys.argv) > 1 else 2
hwnd = find_instance_window(inst)
e = Eye(hwnd); k = Keys(hwnd)
u32 = ctypes.windll.user32; p = wt.POINT()
print("WoW in front:", u32.GetForegroundWindow() == hwnd)
TRIALS = [("W", False, "w"), ("left arrow", False, "left"), ("W again", False, "w"), ("right arrow", False, "right")]
if "--deact" in sys.argv:          # a real background state first: the client was told it lost activation
    for m, w in ((0x1C, 0), (0x86, 0), (0x06, 0), (0x08, 0)):
        u32.SendMessageW(hwnd, m, w, 0)
    time.sleep(0.5)
for label, act, key in TRIALS:
    s0 = e.read(); pts = []
    if act: fake_activate(hwnd)
    k.down(key)
    for i in range(30):
        if act: fake_activate(hwnd)
        time.sleep(0.05); u32.GetCursorPos(ctypes.byref(p)); pts.append((p.x, p.y))
    k.up(key); time.sleep(0.4); s1 = e.read()
    tr = sum(abs(a[0]-b[0]) + abs(a[1]-b[1]) for a, b in zip(pts, pts[1:]))
    turned = abs((s1.facing - s0.facing + 3.1416) % 6.2832 - 3.1416)
    print(f"{label:28} moved {((s1.x-s0.x)**2+(s1.y-s0.y)**2)**0.5:5.1f} yd turned {turned:.2f} rad  cursor travel {tr}")
