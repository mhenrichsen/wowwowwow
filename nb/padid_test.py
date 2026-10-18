"""Hot-plugged pad claimed by list difference; measure movement and cursor grabs per input (one client reads it)."""
import collections, ctypes, ctypes.wintypes as wt, sys, time
from nb.eye import Eye
from nb.win import Keys
from nb.multi import find_instance_window
from nb.move import fake_activate
from nb.vpad import Pad
from nb.ev import ev

inst = int(sys.argv[1]) if len(sys.argv) > 1 else 2
extra = sys.argv[2:]                      # lua statements to run before the trials
hwnd = find_instance_window(inst)
e = Eye(hwnd); k = Keys(hwnd)
u32 = ctypes.windll.user32
IDS = "(function() local o='' for _,id in ipairs(C_GamePad.GetAllDeviceIDs()) do o=o..id..' ' end return o end)()"
before = set(ev(IDS, e, k)[2:].split())
A = Pad()
for i in range(30):
    fake_activate(hwnd); time.sleep(0.1)
new = sorted(set(ev(IDS, e, k)[2:].split()) - before, key=int)
print("new device", new)
print(ev(f"SetCVar('GamePadEnable', 1) or SetCVar('GamePadSingleActiveID', {new[-1]}) or GetCVar('GamePadSingleActiveID')", e, k))
for x in extra:
    print(x, "=>", ev(x, e, k))
p = wt.POINT()
for label, fwd, tx in (("forward", 1.0, 0.0), ("turn", 0.0, 0.6), ("idle", 0.0, 0.0)):
    s0 = e.read(); pts = []
    A.fwd, A.turn_x = fwd, tx; A._push()
    for i in range(40):
        fake_activate(hwnd); time.sleep(0.05); u32.GetCursorPos(ctypes.byref(p)); pts.append((p.x, p.y))
    A.fwd, A.turn_x = 0.0, 0.0; A._push(); time.sleep(0.4); s1 = e.read()
    tr = sum(abs(a[0]-b[0]) + abs(a[1]-b[1]) for a, b in zip(pts, pts[1:]))
    print(f"{label:8} moved {((s1.x-s0.x)**2+(s1.y-s0.y)**2)**0.5:5.1f} yd  cursor travel {tr:6}  {collections.Counter(pts).most_common(2)}")
