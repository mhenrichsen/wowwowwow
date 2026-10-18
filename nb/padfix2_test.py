"""Cursor-park experiments on ONE background client steered by its own virtual pad (successor of padfix_test).

  python -m nb.padfix2_test <inst> "<trial>" ["<trial>" ...] [--noclaim-reset]
  python -m nb.padfix2_test guard <seconds>          only the cursor guard (all 4 park points), e.g. around a bot stop

A trial is a space-separated step list, first token = label:
  cur:in | cur:out | cur:home | cur:X,Y        put the system cursor somewhere (in = inside the window off the park point,
                                               out = the user's start position if outside every WoW window, else taskbar)
  drive:lx,ly,rx,ry,secs                       sticks on, fake-activate every 0.4 s, sticks off
  idle:secs                                    sticks 0, keep fake-activating every 0.4 s
  wait:secs                                    sticks 0, no messages
  lua:NAME                                     run LUA[NAME] via /be eval (prints the answer)
  key:K,hold                                   posted key press
  mm:X,Y                                       posted WM_MOUSEMOVE at client coords (cursor does not move)
  act                                          one fake activation
  claim                                        unplug + plug a new pad and claim it again (re-claim)
  keep:X,Y | keep:off                          background thread: posted WM_MOUSEMOVE at X,X+1 / Y every 0.5 s (THE FIX)
  mmd:X,Y[,X2,Y2] | mmd:off                    posted WM_MOUSEMOVE during drive steps only
  acts:full|app|nc|act|focus|app_nc|...        activation message set used by drive steps
  tgt:NPCID / interact:secs / ui               target a plate by key, fake-activate + press m, print ui bits
  nudge:dx,dy                                  move the real cursor a few px (does a client snap it back?)
Result (2026-10-14): the client warps the cursor to its centred point whenever a stick moves while
IsUsingMouse() is false (no mouse message in the last GamePadOverlapMouseMs = 2000 ms), cursor in or out of the window.
A guard thread polls GetCursorPos every ~1 ms; a jump onto any client's park point is logged (ms) and undone at once
(SetCursorPos back to the last good position). A trial stops after its first park. An OnUpdate logger in the client
records state edges with GetTime(): F/f freelook, C/c cursor control, M/m mouselook, S/s stick live, G/g using
gamepad, U/u using mouse. Every CVar in TOUCHED is put back at the end; the pad ends GamePadEnable 0 / ID 999.
"""
import ctypes, ctypes.wintypes as wt, math, sys, threading, time
from nb.eye import Eye
from nb.win import Keys
from nb import multi
from nb.vpad import Pad
from nb.ev import ev as _ev

u32 = ctypes.windll.user32
winmm = ctypes.windll.winmm
u32.SendMessageTimeoutW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                                    ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
u32.PostMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
ACT = ((0x1C, 1), (0x86, 1), (0x06, 1), (0x07, 0))
ACTS = {"full": ACT, "app": ((0x1C, 1),), "nc": ((0x86, 1),), "act": ((0x06, 1),), "focus": ((0x07, 0),),
        "app_nc": ((0x1C, 1), (0x86, 1)), "app_act": ((0x1C, 1), (0x06, 1)), "app_focus": ((0x1C, 1), (0x07, 0)),
        "act_focus": ((0x06, 1), (0x07, 0)), "none": ()}

TOUCHED = ["GamePadCursorAutoDisableSticks", "GamePadCursorAutoDisableJump", "GamePadTankTurnSpeed",
           "GamePadFaceMovementMaxAngle", "GamePadFaceMovementMaxAngleCombat", "GamePadTurnWithCamera",
           "GamePadAnalogMovement", "GamePadOverlapMouseMs", "GamePadCameraYawSpeed", "GamePadCameraPitchSpeed",
           "CursorFreelookCentering", "CursorStickyCentering", "CursorCenteredYPos", "GamePadCursorCenteredEmulation",
           "GamePadFaceMovementMount", "GamePadCursorAutoEnable", "GamePadCursorCentering", "GamePadEmulateShift",
           "GamePadCursorForTargeting", "GamePadCursorOnLogin", "GamePadTouchCursorEnable"]

LUA = {
    # state probes
    "st": ("(function() return (IsGamePadFreelookEnabled() and 'F' or 'f')..(IsGamePadCursorControlEnabled() and 'C' or 'c')"
           "..(IsMouselooking() and 'M' or 'm')..' '..GetCVar('GamePadSingleActiveID')..' '..GetCVar('GamePadEnable') end)()"),
    "cur": "(function() local x,y=GetCursorPosition() return format('%.0f,%.0f scale %.2f',x,y,UIParent:GetEffectiveScale()) end)()",
    # freelook / cursor toggles
    "fl0": "(function() SetGamePadFreeLook(false) return tostring(IsGamePadFreelookEnabled()) end)()",
    "fl1": "(function() SetGamePadFreeLook(true) return tostring(IsGamePadFreelookEnabled()) end)()",
    "cc1": "(function() SetGamePadCursorControl(true) return tostring(IsGamePadCursorControlEnabled()) end)()",
    "cc0": "(function() SetGamePadCursorControl(false) return tostring(IsGamePadCursorControlEnabled()) end)()",
    "mlstop": "(function() MouselookStop() return tostring(IsMouselooking()) end)()",
    "mlstart": "(function() MouselookStart() return tostring(IsMouselooking()) end)()",
    "close": "(function() CloseLoot() CloseGossip() return 1 end)()",
    # keep-freelook-on hook (d) / keep-freelook-off hook
    "hook_on": ("(function() BFK=BFK or CreateFrame('Frame') BFK.SetScript(BFK,'OnUpdate',function() "
                "if not IsGamePadFreelookEnabled() then SetGamePadFreeLook(true) end end) return 1 end)()"),
    "hook_off": ("(function() BFK=BFK or CreateFrame('Frame') BFK.SetScript(BFK,'OnUpdate',function() "
                 "if IsGamePadFreelookEnabled() then SetGamePadFreeLook(false) end end) return 1 end)()"),
    "hook_ml": ("(function() BFK=BFK or CreateFrame('Frame') BFK.SetScript(BFK,'OnUpdate',function() if IsGamePadFreelookEnabled() "
                "then SetGamePadFreeLook(false) end if IsMouselooking() then MouselookStop() end end) return 1 end)()"),
    "hook_none": "(function() if BFK then BFK.SetScript(BFK,'OnUpdate',nil) end return 1 end)()",
    # CVar sets
    "sticky1": "(function() SetCVar('CursorStickyCentering',1) return GetCVar('CursorStickyCentering') end)()",
    "fc0": "(function() SetCVar('CursorFreelookCentering',0) return GetCVar('CursorFreelookCentering') end)()",
    "emu0": "(function() SetCVar('GamePadCursorCenteredEmulation',0) return GetCVar('GamePadCursorCenteredEmulation') end)()",
    "ovl": "(function() SetCVar('GamePadOverlapMouseMs',3600000) return GetCVar('GamePadOverlapMouseMs') end)()",
    "yaw0": ("(function() SetCVar('GamePadCameraYawSpeed',0) SetCVar('GamePadCameraPitchSpeed',0) "
             "return GetCVar('GamePadCameraYawSpeed')..GetCVar('GamePadCameraPitchSpeed') end)()"),
    "tank": ("(function() SetCVar('GamePadTankTurnSpeed',1) SetCVar('GamePadFaceMovementMaxAngle',180) "
             "SetCVar('GamePadFaceMovementMaxAngleCombat',180) SetCVar('GamePadTurnWithCamera',0) "
             "return GetCVar('GamePadTankTurnSpeed') end)()"),
    "ads0": "(function() SetCVar('GamePadCursorAutoDisableSticks',0) return GetCVar('GamePadCursorAutoDisableSticks') end)()",
    "ads2": "(function() SetCVar('GamePadCursorAutoDisableSticks',2) return GetCVar('GamePadCursorAutoDisableSticks') end)()",
    "ypos0": "(function() SetCVar('CursorCenteredYPos',0) return GetCVar('CursorCenteredYPos') end)()",
    "pad0": "(function() SetCVar('GamePadEnable',0) return GetCVar('GamePadEnable') end)()",
    "ovl0": "(function() SetCVar('GamePadOverlapMouseMs',2000) return GetCVar('GamePadOverlapMouseMs') end)()",
    "pad1": "(function() SetCVar('GamePadEnable',1) return GetCVar('GamePadEnable') end)()",
}

LOG = [
    "(function() BTL={} BTLs='' BTL0=GetTime() BTLid={pid} return 1 end)()",
    ("(function() BTLf=function() return (IsGamePadFreelookEnabled() and 'F' or 'f')..(IsGamePadCursorControlEnabled()"
     " and 'C' or 'c')..(IsMouselooking() and 'M' or 'm') end return 1 end)()"),
    ("(function() BTLg=function() local m=C_GamePad.GetDeviceMappedState(BTLid) local s=m and m.sticks local a=s and s[1] "
     "and (math.abs(s[1].x)+math.abs(s[1].y)) or 0 return a>0.2 and 'S' or 's' end return 1 end)()"),
    ("(function() BTLu=function() return (IsUsingGamepad and IsUsingGamepad() and 'G' or 'g')..(IsUsingMouse and "
     "IsUsingMouse() and 'U' or 'u') end return 1 end)()"),
    ("(function() BTLF=BTLF or CreateFrame('Frame') BTLF.SetScript(BTLF,'OnUpdate',function() local s=BTLf()..BTLg()..BTLu()"
     " if s~=BTLs then BTLs=s BTL[#BTL+1]=format('%.2f%s',GetTime()-BTL0,s) end end) return 1 end)()"),
]
LOG_RESET = "(function() BTL={} BTLs='' BTL0=GetTime() return 1 end)()"
LOG_N = "(function() return #BTL end)()"
LOG_CHUNK = "(function() return table.concat(BTL,' ',{a},math.min(#BTL,{b})) end)()"
LOG_OFF = "(function() if BTLF then BTLF.SetScript(BTLF,'OnUpdate',nil) end return 1 end)()"
IDS = "(function() local o='' for _,id in ipairs(C_GamePad.GetAllDeviceIDs()) do o=o..id..' ' end return o end)()"


def cursor():
    p = wt.POINT(); u32.GetCursorPos(ctypes.byref(p)); return p.x, p.y


def client_rect(hwnd):
    r = wt.RECT(); u32.GetClientRect(hwnd, ctypes.byref(r))
    p = wt.POINT(0, 0); u32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y, r.right, r.bottom


def park_points():
    out = {}
    for i, pid in multi.clients().items():
        h = multi.window_of(pid)
        if h:
            x, y, w, hh = client_rect(h)
            out[i] = (x + w // 2, y + int(hh * 0.4))
    return out


def inside_any_wow(c):
    for i, pid in multi.clients().items():
        h = multi.window_of(pid)
        if h:
            x, y, w, hh = client_rect(h)
            if x <= c[0] < x + w and y <= c[1] < y + hh:
                return i
    return 0


class Guard(threading.Thread):
    """Polls the cursor ~1 ms; a jump (> 20 px) onto a park point is logged and undone."""

    def __init__(self, parks):
        super().__init__(daemon=True)
        self.parks, self.events, self.on, self.good = parks, [], True, cursor()
        self.lock = threading.Lock()
        self.restore = True         # undo parks; switched off when a client re-parks within 300 ms (pinned)
        self.pinned = 0             # client that pins the cursor (re-parks every restore), 0 = none
        self.expect = None          # where the harness put the cursor; any other non-park change is logged as MOVE
        self.moves = []

    def run(self):
        winmm.timeBeginPeriod(1)
        prev = cursor()
        try:
            while self.on:
                c = cursor()
                if c != prev:
                    hit = [i for i, q in self.parks.items() if abs(q[0] - c[0]) <= 2 and abs(q[1] - c[1]) <= 2]
                    jump = abs(c[0] - prev[0]) + abs(c[1] - prev[1])
                    if hit and jump > 20:
                        t = time.perf_counter(); g = self.good
                        again = bool(self.events) and self.events[-1]["park"] == hit[0] and t - self.events[-1]["t"] < 0.3
                        if again:
                            self.restore = False; self.pinned = hit[0]
                        dt = -1.0
                        if self.restore:
                            u32.SetCursorPos(*g)
                            n = 0
                            while cursor() != g and n < 50:
                                n += 1; time.sleep(0.0005)
                            dt = (time.perf_counter() - t) * 1000
                        with self.lock:
                            self.events.append(dict(t=t, wall=time.time(), park=hit[0], frm=prev, to=c, restore_ms=dt))
                        print(f"    GUARD {time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} park{hit[0]} "
                              f"{prev}->{c} " + (f"restored to {g} in {dt:.1f} ms" if dt >= 0 else "PINNED: left there"),
                              flush=True)
                        c = cursor()
                    elif not hit:
                        if self.expect is not None and c != self.expect:
                            self.moves.append((time.time(), prev, c))
                            print(f"    MOVE {time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} {prev}->{c} "
                                  f"(expected {self.expect})", flush=True)
                            self.expect = c
                        self.good = c
                prev = c
                time.sleep(0.001)
        finally:
            winmm.timeEndPeriod(1)

    def count(self):
        with self.lock:
            return len(self.events)


def guard_only(secs):
    g = Guard(park_points()); print("guard parks", g.parks, flush=True); g.start()
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(0.2)
    g.on = False; g.join(1)
    print(f"guard done: {len(g.events)} parks undone", flush=True)


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


class Rig:
    def __init__(self, inst):
        self.inst = inst
        self.hwnd = multi.find_instance_window(inst)
        self.eye, self.keys = Eye(self.hwnd), Keys(self.hwnd)
        self.parks = park_points()
        self.cx, self.cy = self.parks[inst]
        self.home = cursor()
        self.neutral = (self.cx + 250, self.cy + 200)
        self.out = self.home if not inside_any_wow(self.home) else (2200, 1420)
        self.pad = None
        self.pid = None
        self.last_set = None
        self.aborted = False
        self.actset = ACT
        self.mm_during = None
        self.keep = False
        self.keep_pt = (1000, 450)
        self.last_act = 0.0
        self.guard = Guard(self.parks)

    def ev(self, x, wait=3):
        return _ev(x, self.eye, self.keys, wait=wait)

    def keeper(self):
        """While self.keep: post WM_MOUSEMOVE (two in-window points 1 px apart, alternating) every 0.5 s, never within
        0.15 s after an activation (a move right after activation is lost): keeps IsUsingMouse() true."""
        k = 0
        while self.keep:
            if time.time() - self.last_act > 0.15:
                k += 1
                x, y = self.keep_pt[0] + (k & 1), self.keep_pt[1]
                u32.PostMessageW(self.hwnd, 0x0200, 0, (y << 16) | x)
                time.sleep(0.5)
            else:
                time.sleep(0.05)

    def act(self, msgs=None):
        self.last_act = time.time()
        if u32.GetForegroundWindow() != self.hwnd:
            for m, w in (ACT if msgs is None else msgs):
                u32.SendMessageTimeoutW(self.hwnd, m, w, 0, 0x0002, 200, None)

    def sticks(self, lx, ly, rx, ry):
        self.pad.pad.left_joystick_float(lx, ly); self.pad.pad.right_joystick_float(rx, ry); self.pad.pad.update()

    def claim(self):
        if self.pad:
            self.sticks(0, 0, 0, 0)
            del self.pad.pad
            self.pad = None
            time.sleep(1.0)
        print("  pad pre:", self.ev("(function() SetCVar('GamePadSingleActiveID',999) SetCVar('GamePadEnable',1) "
                                    "return GetCVar('GamePadEnable')..' '..GetCVar('GamePadSingleActiveID') end)()"))
        for _ in range(15):
            self.act(); time.sleep(0.1)
        before = set(self.ev(IDS)[2:].split())
        self.pad = Pad()
        for _ in range(30):
            self.act(); time.sleep(0.1)
        new = sorted(set(self.ev(IDS)[2:].split()) - before, key=lambda s: int(s) if s.isdigit() else 0)
        comb = self.ev("tostring(C_GamePad.GetCombinedDeviceID())")[2:]
        own = [i for i in new if i != comb]
        print(f"  devices before {sorted(before)} new {new} combined {comb} own {own}")
        if not own:
            raise RuntimeError("no own device")
        self.pid = own[0]
        print("  claim:", self.ev(f"(function() SetCVar('GamePadSingleActiveID',{self.pid}) return GetCVar('GamePadSingleActiveID') end)()"))
        print("  logger id:", self.ev(f"(function() BTLid={self.pid} return BTLid end)()"))

    def unpin(self):
        """A client re-parks the cursor at every restore: end its mouselook, then pad off, then give the cursor back."""
        print(f"  PINNED by client {self.guard.pinned}: MouselookStop + freelook off ...", flush=True)
        if self.pad:
            self.sticks(0, 0, 0, 0)
        self.actset = ACT; self.mm_during = None
        for step in ("mlstop", "fl0", "pad0"):
            print(f"    {step} => {self.ev(LUA[step])}")
            n0 = self.guard.count()
            self.guard.restore = True; self.guard.pinned = 0
            u32.SetCursorPos(*self.guard.good); time.sleep(0.4)
            if self.guard.count() == n0:
                print(f"    released after {step}")
                if step == "pad0":
                    self.aborted = True
                    print("    (pad off: no further trials this run)")
                return step
        self.aborted = True
        return None

    def read_log(self):
        n = self.ev(LOG_N)
        try:
            n = int(n[2:])
        except ValueError:
            return f"(log n={n})"
        parts, a = [], 1
        while a <= n and a < 120:
            r = self.ev(LOG_CHUNK.format(a=a, b=a + 7))
            parts.append(r[2:] if r.startswith("R:") else r)
            a += 8
        return " ".join(parts)

    def post_mm(self):
        self.mm_i = getattr(self, "mm_i", 0) + 1
        x, y = self.mm_during[self.mm_i % len(self.mm_during)]
        u32.PostMessageW(self.hwnd, 0x0200, 0, (y << 16) | x)

    def drive(self, lx, ly, rx, ry, secs, start_parks):
        tm = 0.0
        if self.mm_during:          # one mouse move BEFORE the activation + stick (one right after activation is lost)
            self.post_mm(); tm = time.time(); time.sleep(0.06)
        self.sticks(lx, ly, rx, ry)
        t0 = time.time(); ta = 0.0; parked = False
        last = self.eye.read(); tr = 0.0; path = 0.0; ts = time.time()
        try:
            while time.time() - t0 < secs:
                if time.time() - ta > 0.4:
                    ta = time.time(); self.act(self.actset)
                if self.mm_during and time.time() - tm > self.mm_period and time.time() - ta > 0.15:
                    tm = time.time(); self.post_mm()
                    continue
                if time.time() - ts > 0.25:
                    ts = time.time(); cur_ = self.eye.read()
                    if cur_ and last:
                        path += math.hypot(cur_.x - last.x, cur_.y - last.y); tr += wrap(cur_.facing - last.facing)
                        last = cur_
                if False:
                    tm = time.time(); self.mm_i = getattr(self, "mm_i", 0) + 1
                    x, y = self.mm_during[self.mm_i % len(self.mm_during)]
                    u32.PostMessageW(self.hwnd, 0x0200, 0, (y << 16) | x)
                time.sleep(0.01)
                if self.guard.count() > start_parks:
                    parked = True
                    time.sleep(0.15)       # let the stick stay a moment so a storm would show, then stop
                    break
        finally:
            self.sticks(0, 0, 0, 0)
        dt = time.time() - t0
        print(f"    drive {dt:.1f}s: path {path:.1f} yd ({path/max(dt,0.1):.1f} yd/s)  turned {tr:+.2f} rad ({tr/max(dt,0.1):+.2f} rad/s)")
        return parked

    def trial(self, spec):
        toks = spec.split()
        label, steps = toks[0], toks[1:]
        print(f"=== TRIAL {label}: {' '.join(steps)}", flush=True)
        self.ev(LOG_RESET)
        tr0 = time.time()
        p0 = self.guard.count()
        s0 = self.eye.read()
        marks = []
        parked = False
        for st in steps:
            k, _, a = st.partition(":")
            marks.append(f"{time.time()-tr0:.2f}:{k}")
            if k == "cur":
                c = {"in": self.neutral, "out": self.out, "home": self.home}.get(a) or tuple(int(v) for v in a.split(","))
                self.guard.expect = c
                u32.SetCursorPos(*c); self.last_set = c; time.sleep(0.05)
            elif k == "nudge":           # move the cursor a few px: does a mouselooking client snap it back?
                dx, dy = (int(v) for v in a.split(","))
                c0 = cursor(); c = (c0[0] + dx, c0[1] + dy)
                self.guard.expect = c
                u32.SetCursorPos(*c); self.last_set = c; time.sleep(0.3)
            elif k == "drive":
                lx, ly, rx, ry, secs = (float(v) for v in a.split(","))
                parked = self.drive(lx, ly, rx, ry, secs, p0)
            elif k == "idle":
                t0 = time.time()
                while time.time() - t0 < float(a):
                    self.act(); time.sleep(0.4)
            elif k == "wait":
                time.sleep(float(a))
            elif k == "lua":
                print(f"    lua {a} => {self.ev(LUA[a])}")
            elif k == "key":
                kk, _, h = a.partition(",")
                self.keys.press(kk, float(h or 0.05))
            elif k == "mm":
                x, y = (int(v) for v in a.split(","))
                u32.PostMessageW(self.hwnd, 0x0200, 0, (y << 16) | x)
            elif k == "act":
                self.act()
            elif k == "keep":
                if a == "off":
                    self.keep = False
                else:
                    x, y = (int(v) for v in a.split(","))
                    self.keep_pt = (x, y)
                    u32.PostMessageW(self.hwnd, 0x0200, 0, (y << 16) | (x + 3)); time.sleep(0.06)
                    if not self.keep:
                        self.keep = True
                        threading.Thread(target=self.keeper, daemon=True).start()
            elif k == "tgt":
                s_ = self.eye.read()
                p_ = next((p for p in s_.plates if p.npc == int(a) and not p.dead), None) if s_ else None
                if p_ and p_.token:
                    self.keys.press(f"f{p_.token}" if p_.token <= 10 else f"num{p_.token - 11}"); time.sleep(0.4)
                print(f"    tgt {a}: token {p_ and p_.token} -> target npc {self.eye.read().t_npc}")
            elif k == "interact":
                self.act(); self.keys.press("m")
                t_ = time.time(); op = False
                while time.time() - t_ < float(a or 6):
                    time.sleep(0.2); s_ = self.eye.read()
                    if s_ and any(s_.ui_open(i) for i in range(4)):
                        op = True; break
                print(f"    interact: window open={op} ui={s_ and s_.ui}")
            elif k == "ui":
                s_ = self.eye.read(); print(f"    ui bits {s_ and s_.ui}")
            elif k == "acts":
                self.actset = ACTS[a]
            elif k == "mmd":
                v = [int(x) for x in a.split(",")] if a != "off" else []
                self.mm_during = [(v[i], v[i + 1]) for i in range(0, len(v) - 1, 2)] or None
                self.mm_period = 0.5
            elif k == "claim":
                self.claim()
            if self.guard.count() > p0:
                parked = True
                break
        time.sleep(0.3)
        if self.guard.pinned:
            self.unpin()
        s1 = self.eye.read()
        ev_ = self.guard.events[p0:]
        mv_ = [m for m in self.guard.moves if m[0] >= tr0]
        if mv_:
            print(f"  client-made non-park cursor moves: {[(round(m[0]-tr0, 3), m[1], m[2]) for m in mv_]}")
        d = math.hypot(s1.x - s0.x, s1.y - s0.y) if s0 and s1 else -1
        turned = wrap(s1.facing - s0.facing) if s0 and s1 else 0
        parks = [f"park{e['park']}@{e['wall']-tr0:.3f}s({e['restore_ms']:.1f}ms)" for e in ev_]
        print(f"RESULT {label}: moved {d:.1f} yd turned {turned:+.2f} rad parks {len(ev_)} {parks} "
              f"cursor-was-in-window={inside_any_wow(self.guard.good)}", flush=True)
        print(f"  steps {' '.join(marks)}")
        print(f"  ingame {self.read_log()}", flush=True)
        return parked


def main():
    if sys.argv[1] == "guard":
        guard_only(float(sys.argv[2])); return
    rig = Rig(int(sys.argv[1]))
    if u32.GetForegroundWindow() == rig.hwnd:
        print("window is in front; aborting"); return
    print(f"hwnd {rig.hwnd:#x} park ({rig.cx},{rig.cy}) parks {rig.parks} home {rig.home} neutral {rig.neutral} out {rig.out}")
    rig.guard.start()
    orig = {c: rig.ev(f"GetCVar('{c}')")[2:] for c in TOUCHED}
    print("original cvars", orig, flush=True)
    try:
        rig.claim()
        for x in LOG:
            r = rig.ev(x.replace("{pid}", str(rig.pid)))
            if r != "R:1":
                print("logger chunk failed", r, x[:60])
        print("  state", rig.ev(LUA["st"]))
        for spec in [a for a in sys.argv[2:] if not a.startswith("--")]:
            rig.trial(spec)
            if len(rig.guard.events) >= 12 or rig.aborted:
                print("stopping this run (parks so far: %d, aborted=%s)" % (len(rig.guard.events), rig.aborted)); break
    finally:
        try:
            if rig.pad:
                rig.sticks(0, 0, 0, 0)
            print("logger off", rig.ev(LOG_OFF), rig.ev(LUA["hook_none"]))
            for c, v in orig.items():
                if v and v != "(no answer)" and v != rig.ev(f"GetCVar('{c}')")[2:]:
                    print("restore", c, v, rig.ev(f"SetCVar('{c}','{v}') or GetCVar('{c}')"))
            print("pad off:", rig.ev("(function() SetCVar('GamePadSingleActiveID',999) SetCVar('GamePadEnable',0) "
                                     "return GetCVar('GamePadEnable')..' '..GetCVar('GamePadSingleActiveID') end)()"))
            rig.keep = False
            if rig.pad:
                t = time.time(); del rig.pad.pad; rig.pad = None
                time.sleep(2.0)
                print(f"  pad unplugged (GamePadEnable 0 first); parks during unplug: "
                      f"{[e for e in rig.guard.events if e['wall'] > t]}")
        finally:
            if rig.last_set is not None and (cursor() == rig.last_set or inside_any_wow(cursor())):
                u32.SetCursorPos(*rig.home)       # the user did not take the mouse meanwhile: put it back
            rig.guard.on = False; rig.guard.join(1)
            print(f"total parks this run: {len(rig.guard.events)}", flush=True)


if __name__ == "__main__":
    main()
