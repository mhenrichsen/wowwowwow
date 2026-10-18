"""Watch the system cursor for WoW warps: prints a line whenever it lands on a client's park point
(client x centre, 0.4 of the client height) or jumps > 300 px within 50 ms.  python -m nb.cursorwatch [seconds]"""
import ctypes, ctypes.wintypes as wt, sys, time
from nb import multi

u32 = ctypes.windll.user32
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 120
parks = {}
for i, pid in multi.clients().items():
    h = multi.window_of(pid)
    r = wt.RECT(); u32.GetClientRect(h, ctypes.byref(r))
    p = wt.POINT(r.right // 2, int(r.bottom * 0.4)); u32.ClientToScreen(h, ctypes.byref(p))
    parks[i] = (p.x, p.y)
print("park points", parks, flush=True)
p = wt.POINT(); last = None; t0 = time.time(); events = 0
while time.time() - t0 < secs:
    u32.GetCursorPos(ctypes.byref(p)); c = (p.x, p.y)
    if last and c != last:
        hit = [i for i, q in parks.items() if abs(q[0] - c[0]) <= 2 and abs(q[1] - c[1]) <= 2]
        jump = abs(c[0] - last[0]) + abs(c[1] - last[1])
        if hit or jump > 300:
            events += 1
            print(f"{time.strftime('%H:%M:%S')} cursor {last} -> {c} jump {jump}px park-of={hit}", flush=True)
    last = c
    time.sleep(0.05)
print(f"done: {events} warp events in {secs:.0f} s", flush=True)
