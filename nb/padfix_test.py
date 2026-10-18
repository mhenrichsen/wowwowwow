"""Pad steering of a BACKGROUND client without touching the system cursor: one experiment per trial.

  python -m nb.padfix_test <inst> <trial> [<trial> ...] [--pad2]

Plugs one virtual pad, claims it on the client by device-list difference (while fake-activating it), then per trial:
runs the trial's Lua setup, drives the pad for <= 1.8 s while re-activating the window with the trial's message set,
samples GetCursorPos every 50 ms, releases the sticks and reports yards moved, radians turned, cursor travel, samples
at the window centre and the frame-state counts of the in-game logger (F/f freelook, C/c cursor control, M/m mouselook).
A trial stops early once the cursor sits on the window centre for 4 samples (the capture, spares the user's mouse).
--pad2 then plugs a second pad that is NOT claimed and drives it alone: window 4 must not move.
Every CVar a trial touches is put back at the end; GamePadSingleActiveID 999 + GamePadEnable 0.
"""
import collections, ctypes, ctypes.wintypes as wt, math, sys, time
from nb.eye import Eye
from nb.win import Keys
from nb.multi import find_instance_window
from nb.vpad import Pad
from nb.ev import ev as _ev

u32 = ctypes.windll.user32
u32.SendMessageTimeoutW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                                    ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)

ACT = {     # activation message sets (msg, wParam)
    "full": ((0x1C, 1), (0x86, 1), (0x06, 1), (0x07, 0)),
    "app": ((0x1C, 1),),
    "nc": ((0x86, 1),),
    "act": ((0x06, 1),),
    "focus": ((0x07, 0),),
    "app_nc": ((0x1C, 1), (0x86, 1)),
    "app_act": ((0x1C, 1), (0x06, 1)),
    "app_focus": ((0x1C, 1), (0x07, 0)),
    "none": (),
}
DEACT = ((0x07 + 1, 0), (0x86, 0), (0x06, 0), (0x1C, 0))       # WM_KILLFOCUS, NCACTIVATE 0, ACTIVATE 0, ACTIVATEAPP 0

EXP1A = ("(function() SetCVar('GamePadCursorAutoDisableSticks',0) SetCVar('GamePadCursorAutoDisableJump',0) "
         "return GetCVar('GamePadCursorAutoDisableSticks') end)()")
EXP1B = ("(function() SetGamePadCursorControl(false) SetGamePadFreeLook(false) "
         "return tostring(IsGamePadFreelookEnabled())..tostring(IsGamePadCursorControlEnabled()) end)()")
EXP1 = EXP1A
EXP2 = ("(function() BFL=0 BFF=BFF or CreateFrame('Frame') BFF.SetScript(BFF,'OnUpdate',function() "
        "if IsGamePadFreelookEnabled() then SetGamePadFreeLook(false) BFL=BFL+1 end end) return 1 end)()")
EXP2_OFF = "(function() if BFF then BFF.SetScript(BFF,'OnUpdate',nil) end return BFL end)()"
TANK = ("(function() SetCVar('GamePadTankTurnSpeed',{v}) SetCVar('GamePadFaceMovementMaxAngle',180) "
        "SetCVar('GamePadFaceMovementMaxAngleCombat',180) SetCVar('GamePadTurnWithCamera',0) "
        "return GetCVar('GamePadTankTurnSpeed') end)()")

# name: (activation set, (lx, ly, rx, ry), [lua before], [lua read-backs after])
TRIALS = {
    "idle":        ("full", (0, 0, 0, 0), [], []),
    "base_full":   ("full", (0, 1, 0.6, 0), [], []),
    "base_left":   ("full", (0, 1, 0, 0), [], []),
    "base_right":  ("full", (0, 0, 0.6, 0), [], []),
    "base_app":    ("app", (0, 1, 0.6, 0), [], []),
    "base_nc":     ("nc", (0, 1, 0.6, 0), [], []),
    "base_act":    ("act", (0, 1, 0.6, 0), [], []),
    "base_focus":  ("focus", (0, 1, 0.6, 0), [], []),
    "base_appnc":  ("app_nc", (0, 1, 0.6, 0), [], []),
    "exp1_full":   ("full", (0, 1, 0.6, 0), [EXP1A, EXP1B], []),
    "exp1_left":   ("full", (0, 1, 0, 0), [EXP1A, EXP1B], []),
    "exp1_right":  ("full", (0, 0, 0.6, 0), [EXP1A, EXP1B], []),
    "exp2_full":   ("full", (0, 1, 0.6, 0), [EXP1A, EXP1B, EXP2], [EXP2_OFF]),
    "tank1":       ("full", (0.6, 1, 0, 0), [EXP1A, EXP1B, TANK.format(v=1)], []),
    "tank3":       ("full", (0.6, 1, 0, 0), [EXP1A, EXP1B, TANK.format(v=3)], []),
}

# CVars any trial may change -> put back at the end (read at start)
TOUCHED = ["GamePadCursorAutoDisableSticks", "GamePadCursorAutoDisableJump", "GamePadTankTurnSpeed",
           "GamePadFaceMovementMaxAngle", "GamePadFaceMovementMaxAngleCombat", "GamePadTurnWithCamera",
           "GamePadAnalogMovement", "GamePadOverlapMouseMs", "GamePadStickAxisButtons"]

LOGGER = ("(function() BST={} BSU=function() local s=(IsGamePadFreelookEnabled() and 'F' or 'f')..(IsGamePadCursorControlEnabled()"
          " and 'C' or 'c')..(IsMouselooking() and 'M' or 'm') BST[s]=(BST[s] or 0)+1 end return 1 end)()")
LOGGER_ON = "(function() BSF=BSF or CreateFrame('Frame') BSF.SetScript(BSF,'OnUpdate',BSU) return 1 end)()"
LOGGER_OFF = "(function() if BSF then BSF.SetScript(BSF,'OnUpdate',nil) end return 1 end)()"
LOG_RESET = "(function() BST={} return 1 end)()"
LOG_READ = "(function() local t={} for k,v in pairs(BST) do t[#t+1]=k..'='..v end return table.concat(t,' ') end)()"
STATE = ("GetCVar('GamePadEnable')..' '..GetCVar('GamePadSingleActiveID')..' '..GetCVar('GamePadCursorAutoDisableSticks')"
         "..' '..tostring(IsGamePadFreelookEnabled())..' '..tostring(IsGamePadCursorControlEnabled())")
MAPPED = ("(function() local s=C_GamePad.GetDeviceMappedState({id}) if not s then return 'nil' end local t={{}} "
          "for _,v in ipairs(s.sticks) do t[#t+1]=format('%.2f,%.2f',v.x,v.y) end return table.concat(t,' ') end)()")
IDS = "(function() local o='' for _,id in ipairs(C_GamePad.GetAllDeviceIDs()) do o=o..id..' ' end return o end)()"


CAD = next((float(a[6:]) for a in sys.argv if a.startswith("--cad=")), 0.4)   # s between activations
NODEACT = "--nodeact" in sys.argv
JIGGLE = "--jiggle" in sys.argv
PAD2_ACT = next((a[10:] for a in sys.argv if a.startswith("--pad2act=")), "full")


def deact(hwnd):
    if not NODEACT:
        send(hwnd, DEACT)


def send(hwnd, msgs):
    for m, w in msgs:
        u32.SendMessageTimeoutW(hwnd, m, w, 0, 0x0002, 200, None)


def fg_class():
    buf = ctypes.create_unicode_buffer(64); u32.GetClassNameW(u32.GetForegroundWindow(), buf, 64); return buf.value


def cursor():
    p = wt.POINT(); u32.GetCursorPos(ctypes.byref(p)); return p.x, p.y


def centre(hwnd):
    """Where the client parks the cursor while it mouselooks: x centre, CursorCenteredYPos (0.4) of the height
    (measured: window 4 parks it at (2580,989), window 2 at (2580,293))."""
    r = wt.RECT(); u32.GetClientRect(hwnd, ctypes.byref(r))
    p = wt.POINT(r.right // 2, int(r.bottom * 0.4)); u32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def main():
    inst = int(sys.argv[1])
    names = [a for a in sys.argv[2:] if not a.startswith("--")]
    hwnd = find_instance_window(inst)
    eye, keys = Eye(hwnd), Keys(hwnd)
    ev = lambda x: _ev(x, eye, keys, wait=3)
    if u32.GetForegroundWindow() == hwnd:
        print("window is in front: fake activation would be a no-op; aborting"); return
    cx, cy = centre(hwnd)
    home = cursor()
    neutral = (cx + 250, cy + 250)        # the cursor starts every trial here (inside window 4, off the park point)
    print(f"hwnd {hwnd:#x}  cursor park point ({cx},{cy})  cursor at start {home}  neutral {neutral}")
    orig = {}
    for c in TOUCHED:
        orig[c] = ev(f"GetCVar('{c}')")[2:]
    print("original cvars", orig)
    en = 0 if "--late" in sys.argv else 1
    print("pad pre:", ev(f"(function() SetCVar('GamePadSingleActiveID',999) SetCVar('GamePadEnable',{en}) return GetCVar('GamePadEnable')..' '..GetCVar('GamePadSingleActiveID') end)()"))
    before = set(ev(IDS)[2:].split())
    print("devices before", sorted(before))
    A = Pad()
    for _ in range(30):
        send(hwnd, ACT["full"]); time.sleep(0.1)
    deact(hwnd)
    new = sorted(set(ev(IDS)[2:].split()) - before, key=int)
    print("new device", new)
    stop_streak = 0
    try:
        if not new:
            print("no new device: cannot claim"); return
        # one pad shows up as TWO ids: the device itself and the client's "combined" device (all pads merged,
        # = C_GamePad.GetCombinedDeviceID()). Claiming the combined id reads every pad: claim the device id.
        comb = ev("tostring(C_GamePad.GetCombinedDeviceID())")[2:]
        own = [i for i in new if i != comb]
        print(f"  combined id {comb}; device ids {own}")
        pid = own[0] if own else new[0]
        forced = next((a[8:] for a in sys.argv if a.startswith("--claim=")), None)
        if forced is not None:
            pid = forced
        print("claim:", ev(f"(function() SetCVar('GamePadEnable',1) SetCVar('GamePadSingleActiveID',{pid}) return GetCVar('GamePadSingleActiveID') end)()"))
        print("logger:", ev(LOGGER), ev(LOGGER_ON))
        for name in names:
            act, (lx, ly, rx, ry), pre, post = TRIALS[name]
            for x in pre:
                print(f"  [{name}] {x[:70]}... => {ev(x)}")
            if ev(f"GetCVar('GamePadSingleActiveID')")[2:] != pid:
                print("  claim lost, re-claiming:", ev(f"SetCVar('GamePadSingleActiveID',{pid}) or GetCVar('GamePadSingleActiveID')"))
            ev(LOG_RESET)
            fgc = fg_class()
            for _ in range(25):           # another WoW in front takes the pad input exclusively: wait for a non-WoW one
                if fgc != "waApplication Window":
                    break
                time.sleep(0.2); fgc = fg_class()
            u32.SetCursorPos(*neutral); time.sleep(0.05)
            s0 = eye.read(); pts = [cursor()]; fg = 0; aborted = False
            A.pad.left_joystick_float(lx, ly); A.pad.right_joystick_float(rx, ry); A.pad.update()
            t0 = time.time(); ta = 0.0; at_c = 0; jig = 3
            try:
                while time.time() - t0 < 1.8:
                    if time.time() - ta > CAD:
                        ta = time.time(); send(hwnd, ACT[act])
                    time.sleep(0.05)
                    if JIGGLE:            # a user's hand on the mouse: +-3 px relative moves (raw input), net zero
                        jig = -jig; u32.mouse_event(0x0001, jig, 0, 0, 0)
                    c = cursor(); pts.append(c)
                    fg += u32.GetForegroundWindow() == hwnd
                    at_c = at_c + 1 if abs(c[0] - cx) <= 3 and abs(c[1] - cy) <= 3 else 0
                    if at_c >= 4:
                        aborted = True; break
            finally:
                A.pad.left_joystick_float(0, 0); A.pad.right_joystick_float(0, 0); A.pad.update()
            dt = time.time() - t0
            time.sleep(0.4); deact(hwnd); s1 = eye.read()
            tr = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(pts, pts[1:]))
            nc = sum(abs(p[0] - cx) <= 3 and abs(p[1] - cy) <= 3 for p in pts)
            dev = max(abs(p[0] - neutral[0]) + abs(p[1] - neutral[1]) for p in pts)   # > ~10 px = the client moved it
            d = math.hypot(s1.x - s0.x, s1.y - s0.y); turned = wrap(s1.facing - s0.facing)
            print(f"TRIAL {name:12} act={act:8} sticks=({lx},{ly},{rx},{ry}) {dt:.1f}s  moved {d:5.1f} yd  turned {turned:+.2f} rad  "
                  f"cursor travel {tr:6} px  max-dev {dev:4} px  at-park {nc}/{len(pts)}  fg {fg}  {'ABORTED(capture) ' if aborted else ''}"
                  f"top {collections.Counter(pts).most_common(2)}  front={fgc!r}")
            print(f"  frames {ev(LOG_READ)}  state {ev(STATE)}")
            for x in post:
                print(f"  post {x[:60]} => {ev(x)}")
            stop_streak = stop_streak + 1 if tr > 20000 else 0
            if stop_streak >= 3:
                print("three trials > 20000 px in a row: stopping"); break
        if "--pad2" in sys.argv:
            B = Pad()
            for _ in range(5):
                send(hwnd, ACT["full"]); time.sleep(0.1)
            u32.SetCursorPos(*neutral); time.sleep(0.05)
            s0 = eye.read(); pts = [cursor()]
            B.pad.left_joystick_float(0, 1); B.pad.right_joystick_float(0.6, 0); B.pad.update()
            t0 = time.time(); ta = 0.0
            try:
                while time.time() - t0 < 1.8:
                    if time.time() - ta > CAD:
                        ta = time.time(); send(hwnd, ACT[PAD2_ACT])
                    time.sleep(0.05); pts.append(cursor())
            finally:
                B.pad.left_joystick_float(0, 0); B.pad.right_joystick_float(0, 0); B.pad.update()
            time.sleep(0.4); deact(hwnd); s1 = eye.read()
            tr = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(pts, pts[1:]))
            print(f"TRIAL pad2(unclaimed) moved {math.hypot(s1.x - s0.x, s1.y - s0.y):5.1f} yd  turned {wrap(s1.facing - s0.facing):+.2f} rad  "
                  f"cursor travel {tr} px  top {collections.Counter(pts).most_common(2)}")
            print("  ids now", ev(IDS), " active", ev("GetCVar('GamePadSingleActiveID')"))
    finally:
        A.pad.reset(); A.pad.update()
        u32.SetCursorPos(*home)
        print("logger off:", ev(LOGGER_OFF), ev(EXP2_OFF))
        for c, v in orig.items():
            if v and v != ev(f"GetCVar('{c}')")[2:]:
                print("restore", c, v, ev(f"SetCVar('{c}','{v}') or GetCVar('{c}')"))
        print("pad off:", ev("(function() SetCVar('GamePadSingleActiveID',999) SetCVar('GamePadEnable',0) return GetCVar('GamePadEnable')..' '..GetCVar('GamePadSingleActiveID') end)()"))


if __name__ == "__main__":
    main()
