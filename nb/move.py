"""Keyboard steering along navmesh paths for the modern client."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import math
import os
import threading
import time
from types import SimpleNamespace

import curmap
import navmesh
import numpy as np

from nb.store import Store
from nb.util import Deadline, swallow, swallowed

# the Forever server's mesh (this client's terrain, navmesh.TCF_MMAP_DIR) when it is there; the 1.12-era bot
# mesh otherwise. NB_MESH env overrides (a path).
NB_MESH = os.environ.get("NB_MESH") or (navmesh.TCF_MMAP_DIR if navmesh.is_tcf(navmesh.TCF_MMAP_DIR, 0)
                                        else navmesh.BOT_MMAP_DIR)

try:
    from nb.vpad import Pad
except ImportError:                  # (vpad itself survives a missing vgamepad: Pad() raises then)
    Pad = None

TURN_RATE = math.pi          # rad/s with the arrow keys
ISLAND_EXIT_TRIES = 6     # island exits tried on the collision grid, nearest first
CORNER_POP = 3.0             # yd: switch to the next waypoint
CORNER_REJOIN = 6.0           # yd: a corner passed this far off, the next leg nearer: popped (advance)
CORNER_BLEND = 7.0           # yd: start swinging towards the next waypoint
STEER_GAIN = 3.0             # 1/s: wanted turn rate = gain * heading error
STEER_DT = 0.05              # s per steering slice (pulse-width modulation)
STEER_DEAD = 0.03            # rad: ignore smaller errors
def pad_rate(err: float, derr: float = 0.0) -> float:
    """Right-stick turn rate (rad/s) while running: PD on the heading error. The stick turns the CAMERA and the
    character's facing (what the strip shows, ~0.1 s late) trails it, so a plain P loop overshoots and weaves:
    simulated 1-3 yd rms side to side at any gain for a 0.15-0.4 s trail, and the error flipped sign 0.5-0.9 times
    a second in the steer tele of bots 1-4 ("running drunk", pad only; keys turn the character itself). The damping
    term on d(err)/dt brings the simulated weave under 0.1 yd across that range (PD 2 / 0.6)."""
    u = PAD_KP * err + PAD_KD * derr
    if abs(err) < PAD_DEAD and abs(u) < 0.3:
        return 0.0
    return max(-3.5, min(3.5, u))


PAD_KP = 2.0                 # 1/s
PAD_KD = 0.6                 # s
PAD_DEAD = 0.07              # rad: inside this (and a small PD output) the stick stays centred (0.04: the camera
                             # twitched ~3 deg rms on straight runs, the user: 'jittering')
PAD_D_ALPHA = 0.4            # low-pass share of each new d(err)/dt sample (the facing read is quantised and late)
STEER_TELE_S = 60.0         # s of pad running per "steer tele" log line (_steer_tele)
PAD_GAIN = 4.0              # 1/s: pad turn rate = gain * error (max ~3.5 rad/s)
SLOW_ERR = 1.0               # rad: above this heading error the stick eases off
SLOW_FWD = 0.45              # forward stick while turning sharply
BLEND_T = 0.5                # share of the next leg blended in at the waypoint
# Look-ahead steering (pure pursuit): aim at the path point this far ahead of the character instead of at the next
# waypoint. 50 of the 81 Anvilmar stucks on 2026-10-14 were >= 1.5 yd off the planned line (median 2.3 yd): the
# 3 yd pop + 7 yd blend swung wide of the corners, and at full speed the turn radius is ~2 yd.
# Outdoors in the open 4 yd: pure pursuit's heading error is about off / look-ahead, so at 2.5 yd every 0.3 yd zigzag
# of the path swung the heading (turn rate flipping sign ~2/s on every full6 leg, the user: 'jittering a lot') and a
# 90 deg corner asked 2 v / L = 5.6 rad/s at run speed, past the pad's 3.5 - hence the corner slow-downs. At 4 yd the
# same corner takes 3.5 rad/s at full speed. Near walls and objects (tight legs) the short one stays.
LOOK_AHEAD = 4.0             # yd, outdoors in the open
LOOK_AHEAD_IN = 1.5          # yd, inside a building (door frames, stair turns)
# yd, on a route only the server mesh has (narrow hill trails the bot mesh's clearance rejects): that mesh keeps no
# margin from the edge, and a 2.5 yd look-ahead cut the spur's bends and slid the character into the pockets beside
# it (inst2 21:53-21:54 on the Spirit Stone climb: 9 stucks at (-6100,480) / (-6120,471))
LOOK_AHEAD_TIGHT = 1.0
CORNER_SLOW_ANG = math.radians(75)   # a corner turning more than this ... (45 slowed to a walk at every grid bend)
CORNER_SLOW_D = 3.0          # yd: ... this close ahead along the path: slow down for it
OFF_LINE_SLOW = 1.2          # yd off the planned line: slow down until back on it
RESYNC_SEGS = 12             # polestar Walk.lua: legs ahead searched for the nearest one
RESYNC_D = 3.0               # yd: ... it must be this near us
RESYNC_GAIN = 2.0            # yd: ... and this much nearer than the current leg
STRAY_D = 25.0               # yd off every leg near us: the route is lost, replan (polestar's STRAY)
STRAY_MAX = 3                # such replans per walk
STRAY_GAP = 10.0             # s between them
OFF_ROUTE_LOG = 12.0         # yd off the planned line: log it (every 5 s) - a run gone astray
CORNER_FWD = 0.45            # forward share in those spots (pad stick; W duty cycle in keys mode)
W_PULSE_T = 0.3              # s: keys mode cannot half-press W, so it is held for CORNER_FWD of each such period
MERGE_IN = 2.0               # yd: indoor path points closer than this are merged into one corner
MERGE_TOL = 0.5              # yd: ... when dropping a point moves the line less than this
CLAMP_ANG = 0.35             # rad: indoor corners turning more than this are walked to before turning
# No slowing on a steep outdoor leg (rise / run above this, ~31 deg): the W pulse lets go of W, and on the Spirit Stone
# trail ((-6118,498,405) -> (-6091,496,425) -> (-6052,488,467), 0.74-1.05) the character then slides back; inst4
# 17:33-17:36 was stuck 14 times at (-6101,496) / (-6077,494) on it (old code: 7 at 13:32)
STEEP_NO_SLOW = 0.6

# No hand-placed routes (user rule 2026-10-14: every path is computed). Climbs the bot mesh (50 deg cap) cuts off
# come from the server-mesh fallback in _path, steered round this walk's wedges and the shared bad_spots.json.


_u32 = ctypes.windll.user32
_u32.SendMessageTimeoutW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                                     ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)


_LAST_ACT: dict = {}            # hwnd -> time of the last fake_activate (the mouse keeper stays clear of it)


def fake_activate(hwnd: int):
    """Tell a background WoW window it is the active one (WM_ACTIVATEAPP, WM_NCACTIVATE, WM_ACTIVATE,
    WM_SETFOCUS) so it reads its gamepad; no-op for the real foreground window."""
    if not hwnd or _u32.GetForegroundWindow() == hwnd:
        return
    _LAST_ACT[hwnd] = time.time()
    for msg, wp in ((0x1C, 1), (0x86, 1), (0x06, 1), (0x07, 0)):
        _u32.SendMessageTimeoutW(hwnd, msg, wp, 0, 0x0002, 200, None)     # SMTO_ABORTIFHUNG


# maps the server's own mmaps lack (map 369, the Deeprun Tram: its file is an empty header): the bot's generated mesh
MAP_MESH = {369: os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_tcf369", "mmaps")}

class MouseKeeper(threading.Thread):
    """Keeps a pad-steered background client believing the mouse is in use, so it never parks the system cursor.
    The client warps the cursor to its centre point on every frame a stick moves while IsUsingMouse() is false, i.e.
    2 s (GamePadOverlapMouseMs) after its last mouse message (nb/padfix2_test.py, 2026-10-14: park 49 ms after the
    stick moved without this; 0 parks in all trials and a 10-min soak with it). A posted WM_MOUSEMOVE at an in-window
    point, alternating 1 px, every KEEP_PERIOD s; never within KEEP_AFTER_ACT s after a fake activation (a move right
    after one is lost). Posted messages reach only this window: the user's real cursor never moves."""
    KEEP_PERIOD, KEEP_AFTER_ACT = 0.5, 0.15
    FX, FY = 1000 / 1704, 450 / 657           # the tested point (1000,450) in a 1704x657 client, as a fraction

    def __init__(self, hwnd: int):
        super().__init__(daemon=True)
        self.hwnd, self.on = hwnd, True

    def _pt(self):
        r = ctypes.wintypes.RECT()
        _u32.GetClientRect(self.hwnd, ctypes.byref(r))
        return int(r.right * self.FX), int(r.bottom * self.FY)

    def _post(self, x, y):
        _u32.PostMessageW(self.hwnd, 0x0200, 0, (int(y) << 16) | (int(x) & 0xFFFF))      # WM_MOUSEMOVE

    def run(self):
        x, y = self._pt()
        self._post(x + 3, y)                   # prime: the first move is a real change
        time.sleep(0.06)
        k = 0
        while self.on:
            if time.time() - _LAST_ACT.get(self.hwnd, 0.0) > self.KEEP_AFTER_ACT:
                k += 1
                if k % 20 == 0:
                    x, y = self._pt()          # window resized / moved to another quadrant
                self._post(x + (k & 1), y)
                time.sleep(self.KEEP_PERIOD)
            else:
                time.sleep(0.05)

    def stop(self):
        self.on = False


CLAIM_BUSY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "padclaim.busy")
PAD_TOKEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "padtoken.lock")
# kill switch: while nb/PAD_OFF exists every bot steers with keys (a pad-driving client parks the system cursor once
# when it enters gamepad freelook/mouselook: 6 cursor yanks in 2 min with the one-at-a-time token, 15:42-15:44)
PAD_OFF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PAD_OFF")
PAD_ONE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PAD_ONE")    # one pad-steering bot at a time

# Shared wedge map: 319 of the 518 stuck events of 2026-10-14 were within 3 yd of a spot a bot had been stuck at in
# an earlier walk, and the replan after every third stuck searched the same mesh again (same path). Every instance
# appends its stuck points to nb/bad_spots.json; a spot counts once hits from BAD_MIN_EPS different goto walks agree.
BAD_SPOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bad_spots.json")
BAD_LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bad_spots.lock")
BAD_MERGE = 2.0              # yd: a stuck point this close to a known spot is another hit on that spot
BAD_MIN_EPS = 2              # different goto walks that must have wedged there
BAD_EPS_KEEP = 12            # walk ids kept per spot
BAD_HOLE_R = 1.0             # indoors: a navmesh hole (cuts small polygons, plus a detour box round it)
BAD_AVOID_R = 2.5            # outdoors: a navmesh avoid circle (a cost: big polygons must stay usable)
INDOOR_FLAG = 2048           # BotEye player flag bit 11 = IsIndoors()
# A second stuck this close to an earlier stuck of the same walk: we are back in the same trap (nb.log 19:46-19:49:
# 18 stucks at (-6055.4,383.2), each escape worked and the unchanged route led back in). Cut it out and replan at
# once instead of on every third stuck.
WEDGE_NEAR = 2.0             # yd
TRAP_R = 8.0                 # yd: a 2nd walk ending stuck this close to the last one (5 min) -> break_out()
TRAP_OUT = 10.0              # yd from the trap that count as out
BIG_DROP_PROBE = ((0, 0), (3, 0), (-3, 0), (0, 3), (0, -3))   # xy offsets searched for the old floor
BIG_DROP = 20.0             # yd: a floor pick this far below the last one without hp lost is not a fall
TRAP_HOLE_R, TRAP_HOLE_S = 4.0, 1800.0  # a broken-out trap: a hole of this radius in every route for this long
# Wedge spots as straightened-path penalties (navmesh find_path spot_pen), weighted by evidence. The old avoid cost was
# 500 per step whose portal MIDPOINT lay in the circle: a 3-walk snag in the Algaz bunker's south door (-4797.9,-2684.0)
# paid it on every small door polygon, the 122-hit / 12-walk rise wedge (-4793.8,-2631.2) on 16-21 yd hillside polygons
# paid nothing, and every plan from the bunker took the rise it never got up (inst4 2026-10-15 06:59-07:58). Now each
# spot costs BAD_PEN_PER per distinct walk past the first (cap BAD_PEN_MAX), both as the avoid cost and as a penalty
# on the corridor step the straightened path passes it on, so a 2-walk snag never outweighs a ~100 yd detour while a
# spot a dozen walks wedged on is charged on any polygon size.
BAD_PEN_PER, BAD_PEN_MAX = 100.0, 500.0
BAD_PEN_R_IN = 1.2           # yd: path-penalty radius of an indoor spot
BAD_PEN_R_OUT = 3.0          # yd: ... of an outdoor one (the bot cuts corners: the rise wedge sat 2.8 yd off the line)
# Where a walk wedged, the way it was heading: a penalty circle WEDGE_AHEAD yd along the route from the stuck point.
# A hole at the stuck point itself sits under our feet at the replan and is dropped (inst4 07:43-07:47: 9 stucks at
# (-4814.8,-2668.5), each replan east into the same wall). Kept WEDGE_PEN_S s for every later walk (shelved steps
# and restarted walks started down the same corridor: inst4 07:45:35 -> 07:45:54).
WEDGE_AHEAD, WEDGE_PEN_R, WEDGE_PEN_S = 3.5, 1.2, 600.0
# give-up timers (nb.util.Deadline)
ORBIT_S = 2.0                # s far off the heading while running before a turn on the spot (an orbit)
NEAR_DONE_ISLAND = 15.0      # yd: ... when the goal lies on a mesh island apart from ours (path strategy goal_island)
NEAR_DONE = 2.0              # yd past goto's tol: blocked on the last leg this close to the goal counts as arrived
STUCK_S = 1.5                # s without 1.5 yd of progress in goto: one stuck
TIP_GAP = 1.0                # s after an in-place turn before another (the step that takes its heading)
IN_PLACE_CMD = 1.5           # pad right-stick rate for a standing camera turn (Mover.turn_in_place)
IN_PLACE_RATE = 1.7          # rad/s that command turns the camera (2.5 / -1.6 / 3.0 wanted -> 2.24 / -1.43 / 2.69 at 1.9)
TURN_IN_PLACE = 1.0          # rad of steering error: the walker is turning, not wedged
SLOW_PROGRESS = 0.6          # yd in STUCK_S: slowed (snared), not wedged
FAR_WALK_YD = 800.0      # yd: a goto farther than this asks the bot's far_walk hook (a flight) first
ESCAPE_BACK_S = 3.0          # s escape() drives back towards the mesh when we stand off it
TIGHT_AFTER_BACK_S = 45.0    # s of LOOK_AHEAD_TIGHT after stepping back onto the mesh
ESCAPE_TRY_S = 1.4           # s per heading offset of escape()'s side-steps
BREAK_OUT_S = 3.0            # s per direction of break_out()
SYNC_MIN_S = 0.25            # s sync_heading() runs at least before a settled facing ends it


def spot_weight(sp: dict) -> float:
    """Evidence cost of a wedge spot: BAD_PEN_PER per distinct walk beyond the first, capped at BAD_PEN_MAX."""
    n = len(set(sp.get("eps", ())))
    return max(BAD_PEN_PER, min(BAD_PEN_MAX, BAD_PEN_PER * (n - 1)))

# Floor tracking (Mover.state: the strip has no height, so z comes from the mesh, with our last height as the hint).
# ground_z(hint) ranks the polygons BESIDE us by their centre and height only: after a drop off Teo's stairs
# (inst3 20:30) the character stood in the eroded mesh gap 0.3-0.6 yd beside the floor polygon at 392.8 and the
# stair / platform polygons (397.2 / 399.1, 1.2-1.6 yd away) won on height, so z stayed on the platform and every
# replan started from there - straight east into the platform's side. floor_z ranks them by edge distance; a drop
# costs little (one can fall any height), a rise beyond a stair step is next to impossible.
FLOOR_REACH = 3.0            # yd: polygons this far beside us are candidates
FLOOR_CLIMB = 3.0            # yd: up to this above the last height (ground_z's own stair rule)
FLOOR_FALL_W = 0.3           # score per yd a candidate lies below the last height (0.05: Booty Bay, a deck 5.5 yd under the walkway beat the walkway 0.5 yd aside - replanned from the wrong deck, 8 stucks)
FLOOR_UP_PEN = 20.0          # score of a candidate higher than FLOOR_CLIMB above it
# Fell off the path's floor (indoors: the polygons we are on lie this far below the path at our spot): replan from
# the floor we are on now instead of driving at a waypoint overhead until the stuck check fires.
JUMP_RESET = 60.0            # yd between two state reads: a jump no run makes (state() drops the floor hint)
FALL_DZ = 3.5                # yd
FALL_GAP = 2.0               # s between two such replans
FALL_MAX = 3                 # per walk
FALL_FLOOR_TOL = 3.0         # yd: a floor this close to the path height must exist where we left it (_floor_at_path)


def _poly_dist(px, pz, vs) -> float:
    """2D distance (navmesh rc frame) from a point to a polygon's edges."""
    n = len(vs)
    return min(seg_proj(px, pz, float(vs[i, 0]), float(vs[i, 2]), float(vs[(i + 1) % n, 0]),
                        float(vs[(i + 1) % n, 2]))[1] for i in range(n))


def _floors(nm, x, y):
    """Heights of the walkable polygons containing (x, y) (Mover.floors_at without the mover)."""
    rx, _, rz = navmesh.wow_to_rc(x, y, 0.0)
    tl = nm.tile_at(rx, rz)
    if tl is None:
        return []
    zs = set()
    m = (tl.pmin[:, 0] <= rx) & (tl.pmax[:, 0] >= rx) & (tl.pmin[:, 2] <= rz) & (tl.pmax[:, 2] >= rz)
    for i in np.nonzero(m)[0].tolist():
        if not tl.usable(i):
            continue
        vs = tl.verts[tl.pverts[i, :tl.pcount[i]]]
        if navmesh._point_in_poly_2d(rx, rz, vs):
            zs.add(round(navmesh.poly_height(rx, rz, vs), 1))
    return sorted(zs)


def seed_floor(nm, x, y, indoor=None):
    """Our floor at (x, y) with no height to go on (polestar's snap order): floors in a small mesh pocket only when
    nothing else is there (the terrain under Stormwind's city model, #1600); indoors (IsIndoors) the lowest floor in a
    building; outdoors the floor nearest the terrain's height (the ground, not a roof or a ledge above it);
    nm.ground_z when the mesh has no floor here."""
    try:
        floors = sorted(set(_floors(nm, x, y)))
    except Exception as e:
        swallowed("move.seed_floor", e)
        floors = []
    if not floors:
        return nm.ground_z(x, y)
    if len(floors) == 1:
        return floors[0]
    try:
        keep = [f for f in floors if not nm.in_pocket(x, y, f)]
        floors = keep or floors
    except Exception as e:
        swallowed("move.seed_floor_pocket", e)
    if len(floors) == 1:
        return floors[0]
    if indoor:
        try:
            inb = [f for f in floors if (nd := nm.nearest_poly(x, y, f)[0]) is not None and nm.in_building(nd)]
            if inb:
                return inb[0]
        except Exception as e:
            swallowed("move.seed_floor_indoor", e)
    try:
        import terrain
        th = float(terrain.heights(np.array([x]), np.array([y]), nm.map_id)[0])
    except Exception:                                     # noqa: BLE001
        th = float("nan")
    if th == th:
        return min(floors, key=lambda f: abs(f - th))
    return floors[-1]


def floor_z(nm, x, y, hint=None):
    """Height of the floor we stand on at (x, y), given our last height `hint` (see FLOOR_*). A polygon we stand in
    within FLOOR_CLIMB yd of the hint wins outright (stairs, as ground_z). Otherwise every polygon within FLOOR_REACH
    yd competes on its edge distance plus FLOOR_FALL_W per yd below the hint (FLOOR_UP_PEN above the climb): the floor
    right beside us beats one higher up a yard further off. No hint, or no polygon near: nm.ground_z as before."""
    if hint is None or not hasattr(nm, "tile_at"):
        return nm.ground_z(x, y, hint=hint)
    try:
        rx, _, rz = navmesh.wow_to_rc(x, y, 0.0)
        t = nm.tile_at(rx, rz)
        if t is None:
            return nm.ground_z(x, y, hint=hint)
        r = FLOOR_REACH
        m = ((t.pmin[:, 0] - r <= rx) & (t.pmax[:, 0] + r >= rx) &
             (t.pmin[:, 2] - r <= rz) & (t.pmax[:, 2] + r >= rz))
        cands = []
        for i in np.nonzero(m)[0]:
            i = int(i)
            if not t.usable(i) or nm._island((t.key, i)):
                continue
            vs = t.verts[t.pverts[i, :t.pcount[i]]]
            h = navmesh.poly_height(rx, rz, vs)          # the height at our point, not the polygon's middle
            ins = navmesh._point_in_poly_2d(rx, rz, vs)
            cands.append((ins, h, vs))
        close = [(abs(h - hint), h) for ins, h, _ in cands if ins and abs(h - hint) <= FLOOR_CLIMB]
        if close:
            return min(close)[1]
        best = None
        for ins, h, vs in cands:
            d = 0.0 if ins else _poly_dist(rx, rz, vs)
            if d > r:
                continue
            if h <= hint:
                sc = d + FLOOR_FALL_W * (hint - h)
            elif h - hint <= FLOOR_CLIMB:
                sc = d
            else:
                sc = d + FLOOR_UP_PEN + (h - hint)
            if best is None or sc < best[0]:
                best = (sc, h)
        if best is not None:
            return best[1]
    except Exception as e:
        swallowed("move.floor_z", e)
    return nm.ground_z(x, y, hint=hint)


def leg_z(route, k, px, py):
    """Height of the path at our projection onto the leg route[k-1] -> route[k]; None when a point has no height."""
    a, b = route[k - 1], route[k]
    if not a[2] or not b[2]:
        return None
    t, _ = seg_proj(px, py, a[0], a[1], b[0], b[1])
    return a[2] + t * (b[2] - a[2])


# 'No path' memory: inst1 planned the same four unreachable Kharanos NPCs (Maxan Anvol, Tharek Blackstone, Tognus
# Flintfire, Pilot Bellowfiz) from the Spirit Stone hill at 13:37, 13:40 and 14:46, 1.6-18.5 s per plan with the
# character standing still, and every one ended "no path". A failed plan is remembered NOPATH_TTL s per start island
# + start cell + level + goal; Mover.path() then answers None at once (a found path clears the entry).
NOPATH_TTL = 600.0           # s
NOPATH_CELL = 100.0          # yd: start cell (a plan from far off can fail on the search budget, not on the mesh)


def nopath_key(island, sx: float, sy: float, level, x: float, y: float, z=None) -> tuple:
    return (island, round(sx / NOPATH_CELL), round(sy / NOPATH_CELL), int(level or 0),
            round(x / 3.0), round(y / 3.0), None if z is None else round(z / 4.0))


def merge_hit(spots: list, x: float, y: float, z, inside, ep: str, t: float | None = None) -> dict:
    """Add one stuck point to the spot list (in place): onto the nearest spot within BAD_MERGE yd (same floor),
    else as a new spot. Returns that spot."""
    best = None
    for sp in spots:
        if z is not None and sp.get("z") is not None and abs(sp["z"] - z) > 4.0:
            continue                                   # another storey
        d = math.hypot(sp["x"] - x, sp["y"] - y)
        if d <= BAD_MERGE and (best is None or d < best[0]):
            best = (d, sp)
    if best is None:
        sp = {"x": round(x, 2), "y": round(y, 2), "z": None if z is None else round(z, 2),
              "in": bool(inside), "n": 0, "eps": []}
        spots.append(sp)
    else:
        sp = best[1]
        n = sp["n"]
        sp["x"] = round((sp["x"] * n + x) / (n + 1), 2)
        sp["y"] = round((sp["y"] * n + y) / (n + 1), 2)
        if inside is not None:
            sp["in"] = bool(inside)
        if sp.get("z") is None and z is not None:
            sp["z"] = round(z, 2)
    sp["n"] += 1
    if ep not in sp["eps"]:
        sp["eps"] = (sp["eps"] + [ep])[-BAD_EPS_KEEP:]
    if t is not None:
        sp["t"] = round(t, 1)
    return sp


BAD_REPEAT_S = 600.0         # s: two walks to the same goal closer together than this are one repeated mistake


def ep_parts(ep: str):
    """A walk id 'inst-t0@goal' (goto) -> (t0 or None, goal or None). Log-seeded ids ('log1-14:11:57-x,y') and ids
    from before the goal was recorded give (None, None): unknown, counted as different walks (the old rule)."""
    head, _, goal = str(ep).partition("@")
    try:
        t = float(head.rpartition("-")[2])
    except ValueError:
        t = None
    return t, (goal or None)


def spot_active(sp: dict) -> bool:
    """A spot is a known wedge once two different walks agree on it: different goals, or the same goal at least
    BAD_REPEAT_S apart. The same wrong goal walked twice in a row is one mistake, not two witnesses (2026-10-14 RXP
    mode: two walks to Teo's xy on the floor under his platform made (-6055.3,382.9) a hole, eps 1-1791999992.1 /
    1-1792000075.5, 83 s apart)."""
    eps = list(dict.fromkeys(sp.get("eps", ())))
    if len(eps) < BAD_MIN_EPS:
        return False
    parts = [ep_parts(e) for e in eps]
    for i in range(1, len(parts)):
        for j in range(i):
            (ta, ga), (tb, gb) = parts[i], parts[j]
            if ga is None or gb is None or ga != gb:
                return True
            if ta is None or tb is None or abs(ta - tb) >= BAD_REPEAT_S:
                return True
    return False


PATH_DEADLINE = 40.0        # s for one whole plan (all fallbacks)
PLAN_CHECK_S = 0.5          # s between state reads while a plan runs (path(): given up when hit / in combat)
PLAN_HIT_HP = 3.0           # hp % lost during a plan that gives it up
ALT_MESH_ON = False          # indoor wedges re-plan on the 1.12-era server mesh: off (its buildings are not this
                             # client's - Booty Bay 2026-10-16: two more wedges on its legs)
ALT_MESH_R = 60.0           # yd round an indoor wedge where plans start on the server mesh
ALT_MESH_S = 120.0          # s that lasts
ALT_MESH_LEG = 35.0         # yd of the route taken from the server mesh before the Forever mesh takes over
ALT_OFF_S = 900.0           # s no server-mesh leg is tried near an indoor wedge after one wedged too / was rejected
ALT_CLEAR = 1.5             # yd: a server-mesh leg passing this close to a known wedge spot or trap is rejected
# learned wedge spots / trap holes / wedge-heading penalties as path holes and costs: off. They patched bad planning
# instead of fixing it and piled up (22 in Anvilmar's hall, several on the human's own line, run 8 2026-10-16); the
# user: "holes should not exist". The spots are still recorded (bad_spots.json) for diagnosis
LEARNED_SPOTS = False
# indoors the old bot mesh (data_r4, generated with 1.2 yd clearance) plans the line a player walks; the Forever mesh
# threads stair rails and furniture (Anvilmar: r4 = the human's recorded line out past Teo's stairs, the Forever mesh
# 1.5 yd east along the rail and through the tables; 4-20 wedges per character per run since the switch, 1-3 before)
INDOOR_MESH = os.environ.get("NB_INDOOR_MESH") or None   # off: the Forever mesh everywhere (user 2026-10-16)
INDOOR_BUDGET = 3.0          # s for one indoor leg search
CLIMB_VOTES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "climb_votes.json")
CLIMB_PROOF_EPS = 2          # different goto walks wedged on one polygon step: a proven (persisted) block
CLIMB_RELOAD_S = 60.0        # how often a bot merges the proven blocks the others wrote
CLIMB_SPREAD_COST = 80.0     # yd (session-only) added to each uphill step into a polygon a proven wedge climbs into
CLIMB_AGAIN_COST = 200.0     # yd (session-only) added to a proven step each time a walk wedges on it again
CROSS_CHECK_S = 1.0          # s between polygon reads while walking (a crossed learned step is lifted)
CLIMB_INTO_DZ = 2.0          # yd: a proven wedge step into a polygon centred this far above our floor is a climb


def climb_vote(frm, to, ep: str) -> int:
    """Record that walk `ep` was wedged crossing polygon step frm -> to; return how many different walks were."""
    key = f"{tuple(frm[0])}:{int(frm[1])}>{tuple(to[0])}:{int(to[1])}"
    n = [1]

    def add(spots):
        for v in spots:
            if v.get("k") == key:
                if ep not in v["eps"]:
                    v["eps"] = (v["eps"] + [ep])[-8:]
                n[0] = len(v["eps"])
                return
        spots.append({"k": key, "eps": [ep], "t": time.time()})

    BadSpots(CLIMB_VOTES, CLIMB_VOTES + ".lock").update(add)
    return n[0]


def _spots_decode(d) -> list:
    return list(d.get("spots", [])) if isinstance(d, dict) else []


class BadSpots:
    """nb/bad_spots.json ({"spots": [...]}), shared by all instances: an nb.store.Store - read on change (mtime),
    written under an OS lock on nb/bad_spots.lock by a temp file + os.replace, so a reader never sees half a file.
    Also nb/climb_votes.json (same layout, climb_vote)."""

    def __init__(self, path: str = BAD_SPOTS, lock: str = BAD_LOCK):
        self.path, self.lock = path, lock
        self.store = Store(path, default=list, decode=_spots_decode, encode=lambda sp: {"spots": sp}, lock=lock,
                           indent=0)

    @property
    def spots(self) -> list:
        return self.store.data

    def load(self) -> list:
        return self.store.read()

    def active(self, bbox=None) -> list:
        out = []
        for sp in self.load():
            if not spot_active(sp):
                continue
            if bbox and not (bbox[0] <= sp["x"] <= bbox[2] and bbox[1] <= sp["y"] <= bbox[3]):
                continue
            out.append(sp)
        return out

    def update(self, fn) -> bool:
        """fn(spots) changes the list in place; done under the lock on the newest file. False if the lock stayed
        busy for 2 s or the write failed (the walk goes on either way)."""
        def run(spots):
            fn(spots)                    # in place: a returned value is not the new list (Store.update would take it)
        return self.store.update(run)

    def hit(self, x, y, z, inside, ep: str):
        """Record one stuck point: (spot, True if this hit made it active), or (None, False) if not written."""
        out = []

        def fn(spots):
            was = {id(sp) for sp in spots if spot_active(sp)}
            sp = merge_hit(spots, x, y, z, inside, ep, time.time())
            out.append((sp, spot_active(sp) and id(sp) not in was))
        ok = self.update(fn)
        return out[0] if ok and out else (None, False)


def stuck_hits_from_logs(paths) -> list:
    """(x, y, episode id, n) for every 'stuck at (x,y) #n' line of nb logs. A walk's counter restarts at #1, so a #1
    opens a new episode (per instance); the same line copied into several logs (old_run1/old_run2 overlap, idle.log
    quotes nb.log) counts once."""
    import re
    rx = re.compile(r"(\d\d:\d\d:\d\d)\s+(?:\[(\d+)\]\s+)?stuck at \((-?\d+\.\d+),\s*(-?\d+\.\d+)\) #(\d+)")
    seen, out = set(), []
    for p in paths:
        m_inst = re.search(r"inst(\d+)", p)
        f_inst = m_inst.group(1) if m_inst else "1"
        cur = {}
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            for m in rx.finditer(line):
                hhmmss, tag, xs, ys, ns = m.groups()
                inst = tag or f_inst
                key = (inst, hhmmss, xs, ys, ns)
                n = int(ns)
                if n == 1 or inst not in cur:
                    cur[inst] = f"log{inst}-{hhmmss}-{xs},{ys}"
                if key in seen:
                    continue
                seen.add(key)
                out.append((float(xs), float(ys), cur[inst], n))
    return out


def seg_proj(px, py, ax, ay, bx, by):
    """(t, distance): p projected onto the segment a-b, t clamped to 0..1."""
    dx, dy = bx - ax, by - ay
    n2 = dx * dx + dy * dy
    t = 0.0 if n2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / n2))
    return t, math.hypot(ax + t * dx - px, ay + t * dy - py)


def turn_at(route, k) -> float:
    """Heading change (rad, unsigned) of the route at point k; 0 at the ends."""
    if k <= 0 or k >= len(route) - 1:
        return 0.0
    a = math.atan2(route[k][1] - route[k - 1][1], route[k][0] - route[k - 1][0])
    b = math.atan2(route[k + 1][1] - route[k][1], route[k + 1][0] - route[k][0])
    return abs(wrap(b - a))


def steep_leg(a, b) -> bool:
    """Does the leg a -> b climb or drop faster than STEEP_NO_SLOW? Unknown heights (0 / None) count as not steep."""
    if not a[2] or not b[2]:
        return False
    run = math.dist(a[:2], b[:2])
    return run > 0.5 and abs(b[2] - a[2]) / run > STEEP_NO_SLOW


SPLIT_LEG = 4.0              # yd: route() splits longer legs before flagging the tight ones
SMOOTH_TOL = 0.35            # yd: an open-ground path point this close to the line past it is dropped (smooth_open)


def smooth_open(route, inside):
    """Drop the open-ground points (neither leg tight) that lie within SMOOTH_TOL of the line from the last kept point
    to the next one: the clearance push leaves 0.75 yd-spaced points wobbling 0.1-0.3 yd, and each wobble was a heading
    swing. Bends, heights (a point whose height is off the line by 0.5+ yd) and tight legs are kept."""
    if len(route) < 3:
        return list(route), list(inside)
    out, flg = [route[0]], [inside[0]]
    dropped = []                                         # points dropped since the last kept one: all must stay close

    def near_line(q, a, b):
        u, d = seg_proj(q[0], q[1], a[0], a[1], b[0], b[1])
        return d < SMOOTH_TOL and (not q[2] or not a[2] or not b[2] or abs(q[2] - (a[2] + u * (b[2] - a[2]))) < 0.5)
    for k in range(1, len(route) - 1):
        q, nxt = route[k], route[k + 1]
        if not inside[k] and not inside[k + 1] and all(near_line(p, out[-1], nxt) for p in dropped + [q]):
            dropped.append(q)
            continue
        out.append(q)
        flg.append(inside[k])
        dropped = []
    out.append(route[-1])
    flg.append(inside[-1])
    return out, flg


def near_collision(route) -> list:
    """Indices k of the route's legs route[k-1] -> route[k] that run near collision geometry at body height or on
    model floors (gridpath.collision_runs, unpadded)."""
    try:
        import gridpath
        pts = np.asarray([(q[0], q[1], q[2] or 0.0) for q in route], float)
        if len(pts) < 2:
            return []
        idx, dense = [], [pts[0]]
        for k in range(1, len(pts)):
            a, b = pts[k - 1], pts[k]
            n = max(1, int(math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]))))
            for i in range(1, n + 1):
                dense.append(a + (b - a) * (i / n))
                idx.append(k)
        idx = [1] + idx
        out = set()
        for i, j in gridpath.collision_runs(np.asarray(dense), pad=0):
            out.update(idx[i:j + 1])
        return sorted(out)
    except Exception as e:
        swallowed("move.near_collision", e)
        return []


def merge_indoor(route, inside):
    """Indoors, drop path points that sit < MERGE_IN yd from the previous kept point when the line without them stays
    within MERGE_TOL yd of them (the funnel and the doodad fixer leave clusters of 0.5-1.5 yd legs at door frames
    and stair turns: a stop-and-turn at each). Points that bend round something are kept. Returns (route, inside)."""
    if len(route) < 3:
        return list(route), list(inside)
    out, flg = [route[0]], [inside[0]]
    for k in range(1, len(route) - 1):
        q, nxt = route[k], route[k + 1]
        if inside[k] and math.dist(q[:2], out[-1][:2]) < MERGE_IN \
                and seg_proj(q[0], q[1], out[-1][0], out[-1][1], nxt[0], nxt[1])[1] < MERGE_TOL:
            continue
        out.append(q)
        flg.append(inside[k])
    out.append(route[-1])
    flg.append(inside[-1])
    return out, flg


def corner_clamps(route, inside) -> list:
    """Which points must be walked to before turning (indoor corners: the line from 1.5 yd short of the west-door
    corner (-6132.7,382.6) to the next point grazes the frame (-6132.5,381.5), 15:10:57 / 15:16:10). Of several such
    corners < MERGE_IN yd apart only the last stops the look-ahead: the polyline still runs through the others."""
    n = len(route)
    cl = [bool(inside[k]) and 0 < k < n - 1 and turn_at(route, k) > CLAMP_ANG for k in range(n)]
    for k in range(1, n - 1):
        if cl[k] and cl[k + 1] and math.dist(route[k][:2], route[k + 1][:2]) < MERGE_IN:
            cl[k] = False
    return cl


def advance(route, k, clamp, px, py) -> int:
    """Move the target index k (we walk the leg route[k-1] -> route[k]) past the points we have reached.
    Clamped (indoor) corners: within 0.75 yd, or past the corner and within 1.5 yd (the old indoor rule). Others:
    past the perpendicular through the point, or nearer the next leg than this one - within CORNER_POP yd, so a
    sideways escape cannot skip a corner the path needs."""
    n = len(route)
    while k < n - 1:
        wx, wy = route[k][0], route[k][1]
        ax, ay = route[k - 1][0], route[k - 1][1]
        d = math.hypot(wx - px, wy - py)
        passed = (px - wx) * (wx - ax) + (py - wy) * (wy - ay) > 0
        if clamp[k]:
            if d < 0.75 or (passed and d < 1.5):
                k += 1
                continue
            break
        nearer_next = (seg_proj(px, py, wx, wy, route[k + 1][0], route[k + 1][1])[1]
                       <= seg_proj(px, py, ax, ay, wx, wy)[1])
        if d < 1.0 or (d < CORNER_POP and (passed or nearer_next)):
            k += 1
            continue
        # overshot a corner wide (off the line by more than CORNER_POP): past it and nearer the next leg, the corner
        # is behind us - else we turn back to it (hill3 19:46:26: 4.7 yd past (-6105.9,503.2), ran back uphill)
        if d < CORNER_REJOIN and passed and nearer_next:
            k += 1
            continue
        break
    return k


def resync(route, k, clamp, px, py) -> int:
    """k moved on to the nearest of the next RESYNC_SEGS legs when that one is clearly nearer (RESYNC_GAIN yd) and close
    (RESYNC_D yd); never back, and never past a clamped (indoor) corner, which must be walked to."""
    n = len(route)
    if k >= n - 1:
        return k
    cur = seg_proj(px, py, route[k - 1][0], route[k - 1][1], route[k][0], route[k][1])[1]
    best, bd = k, cur
    for j in range(k + 1, min(n, k + RESYNC_SEGS + 1)):
        if clamp[j - 1]:
            break
        d = seg_proj(px, py, route[j - 1][0], route[j - 1][1], route[j][0], route[j][1])[1]
        if d < bd:
            best, bd = j, d
    if best != k and bd < RESYNC_D and cur - bd > RESYNC_GAIN:
        return best
    return k


def look_ahead(route, k, clamp, px, py, dist):
    """Pure pursuit on the leg route[k-1] -> route[k]: (carrot x, carrot y, distance off the line, along-path
    distance to the next corner turning more than CORNER_SLOW_ANG, or None). The carrot is `dist` yd along the path
    from our projection onto the leg, but never past a clamped corner or the last point."""
    n = len(route)
    ax, ay = route[k - 1][0], route[k - 1][1]
    bx, by = route[k][0], route[k][1]
    t, off = seg_proj(px, py, ax, ay, bx, by)
    foot = (ax + t * (bx - ax), ay + t * (by - ay))
    carrot = (route[-1][0], route[-1][1])
    rem, pos, j = dist, foot, k
    while j < n:
        leg = math.dist(pos, route[j][:2])
        if leg >= rem:
            f = rem / leg if leg else 0.0
            carrot = (pos[0] + (route[j][0] - pos[0]) * f, pos[1] + (route[j][1] - pos[1]) * f)
            break
        if j == n - 1 or clamp[j]:
            carrot = (route[j][0], route[j][1])
            break
        rem -= leg
        pos, j = (route[j][0], route[j][1]), j + 1
    corner, along, pos, j = None, 0.0, foot, k
    while j < n - 1:
        along += math.dist(pos, route[j][:2])
        if along > CORNER_SLOW_D:
            break
        if turn_at(route, j) > CORNER_SLOW_ANG:
            corner = along
            break
        pos, j = (route[j][0], route[j][1]), j + 1
    return carrot[0], carrot[1], off, corner


def _is_wow(hwnd: int) -> bool:
    buf = ctypes.create_unicode_buffer(64)
    _u32.GetClassNameW(hwnd, buf, 64)
    return buf.value == "waApplication Window"


def fake_deactivate(hwnd: int):
    """Undo fake_activate: a client that believes it is active ignores posted W/arrow keys (14:3x: 0.1 yd instead
    of 10 yd while activated), so give it the background state back after an interact press."""
    if not hwnd or _u32.GetForegroundWindow() == hwnd:
        return
    for msg, wp in ((0x06, 0), (0x86, 0), (0x1C, 0), (0x08, 0)):          # ACTIVATE, NCACTIVATE, ACTIVATEAPP, KILLFOCUS
        _u32.SendMessageTimeoutW(hwnd, msg, wp, 0, 0x0002, 200, None)


def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def bearing(x0, y0, x1, y1) -> float:
    return math.atan2(y1 - y0, x1 - x0) % (2 * math.pi)


def walks_through(p, z) -> bool:
    """Does the waypoint list p pass inside the zone z = (x, y, z, r) (any leg, not just the waypoints)?"""
    pts = [(float(q[0]), float(q[1])) for q in p]
    for (ax, ay), (bx, by) in zip(pts, pts[1:] or pts):
        dx, dy = bx - ax, by - ay
        n2 = dx * dx + dy * dy
        t = 0.0 if n2 == 0 else max(0.0, min(1.0, ((z[0] - ax) * dx + (z[1] - ay) * dy) / n2))
        if math.hypot(ax + t * dx - z[0], ay + t * dy - z[1]) < z[3]:
            return True
    return False


def indoor(x, y, z) -> bool:
    """Waypoint inside a building (WMO box): paths there hug door frames, pillars and stair turns."""
    try:
        import vmap
        return bool(vmap.inside_building(x, y, z))
    except Exception as e:
        swallowed("move.indoor", e)
        return False


def client_indoor(s) -> bool:
    """The client's IsIndoors() (BotEye flag bit) where the strip has it; else the WMO box test. The box of the
    Algaz bunker also covers the hillside beside it (inst4 2026-10-15 07:33: outdoor rules switched off there)."""
    fl = getattr(s, "flags", None)
    return bool(fl & INDOOR_FLAG) if isinstance(fl, int) else indoor(s.x, s.y, s.z)


class _Aborted(Exception):
    """goto()'s abort() fired inside a wedge escape / break-out (they run several seconds without the main loop)."""


class Mover:
    def __init__(self, eye, keys, log=print):
        self.eye, self.keys, self.log = eye, keys, log
        self.nm = navmesh.NavMesh(NB_MESH, curmap.MAP)
        # (gate on the real mesh, goal xy, indoors only) of NPC spots on sealed mesh patches, learned by path() from
        # island_exit (no hand-placed gates: floor_z keeps a hut's floor, not its roof, as our height)
        self.gates = []
        self.bad = BadSpots()           # nb/bad_spots.json: wedge spots shared by all instances
        self._nopath: dict = {}         # nopath_key -> until: plans that found nothing (NOPATH_TTL)
        self._wedges = []               # this walk's own stuck points: holes for its replans
        self._ep = None                 # id of the current goto walk (one "episode" for bad_spots)
        self.inst = os.environ.get("NB_INST", "1")
        self.z = None
        self.running = False
        self.pad = None
        self.pad_ok = False             # our pad is plugged in AND this client reads only it (Bot.claim_pad)
        self._fg_t, self._mode, self._tok = 0.0, None, None
        self.zones = []                 # (x, y, z, r) areas the path should detour around (strong mobs)
        # The virtual pad is plugged in by Bot.claim_pad (it has to appear as a NEW device in this client), not here.
        # NB_PAD=0: keys only.
        self.want_pad = Pad is not None and os.environ.get("NB_PAD", "1") != "0"

    def make_pad(self) -> bool:
        """Plug in our virtual pad (a quick restart can find ViGEmBus still holding the old one: retry ~20 s)."""
        err = None
        for attempt in range(10):
            try:
                self.pad = Pad()
                self.start_keeper()            # normally already running: Bot.claim_pad starts it first
                return True
            except Exception as e:
                err = e
                time.sleep(2.0)
        self.log(f"no virtual gamepad ({err}): keyboard steering")
        return False

    def start_keeper(self):
        """Start the MouseKeeper for this window (idempotent). Before GamePadEnable 1 and before the pad is plugged
        in: started at make_pad, client 1 still parked the cursor once during its claim (22:07:02, 13 s before the
        claim ended) - the enable / device arrival came while it thought the mouse idle."""
        if getattr(self, "keeper", None) is None and self.eye.cap.hwnd:
            self.keeper = MouseKeeper(self.eye.cap.hwnd)
            self.keeper.start()
            time.sleep(0.6)                    # one keep-alive move in before anything gamepad happens

    def drop_pad(self):
        if self.pad:
            with swallow("move.drop_pad", self.log):
                self.pad.stop()
                del self.pad.pad              # vgamepad unplugs the device when the object goes away
        if getattr(self, "keeper", None) is not None:
            self.keeper.stop()                 # after the sticks are gone (pad.stop above)
            self.keeper = None
        self.pad, self.pad_ok = None, False

    def padding(self) -> bool:
        """Steer with the virtual pad, also when the game window is not in front: an unfocused client
        ignores the pad until it gets an activation message, so a background window is told it is
        active again every 0.4 s (measured 2026-10-14: 0.0 yd without, 13.9 yd with). Each client only
        reads its own pad (GamePadSingleActiveID, claimed by Bot.claim_pad)."""
        if not (self.pad and self.pad_ok):
            return False
        now = time.time()
        if now - self._fg_t > 0.4:
            self._fg_t = now
            hwnd = self.eye.cap.hwnd
            fg = _u32.GetForegroundWindow()
            if fg == hwnd:
                mode = "pad"                       # really in front: the pad just works
            elif _is_wow(fg) or os.path.exists(CLAIM_BUSY) or os.path.exists(PAD_OFF):
                # another WoW window in front gets the pad input instead of us (measured: 0.0 yd), and while
                # some bot is claiming its pad every stick must be still: posted keys meanwhile
                mode = "keys"
            elif not os.path.exists(PAD_ONE) or self._token(True):
                # 15:38:49: two or more fake-activated clients steering by pad at once each parked the system cursor
                # and fought; the MouseKeeper (make_pad) removes the park itself, so every bot may pad at once. The
                # old one-at-a-time token comes back with the file nb/PAD_ONE.
                mode = "pad"
                fake_activate(hwnd)                # never the deactivate set: that kills pad input for good
            else:
                mode = "keys"                     # another bot holds the pad token
            if mode == "keys":
                self._token(False)
            if mode != self._mode:
                if self._mode is not None and (mode == "pad" or _is_wow(fg)):
                    pass                           # token hand-overs are routine: no log line
                self.stop(keep_token=True)
                self._mode = mode
        return self._mode == "pad"

    # ------------------------------------------------------------- basics
    def state(self):
        s = self.eye.read()
        if s is None:
            raise RuntimeError("strip not visible")
        # a torn strip frame (inst1 01:08:43: x -19490, 13649 yd "off the route"): a jump no run can make in the
        # time since the last read is re-read (up to 3 times) instead of steered on
        last = getattr(self, "_last_xy", None)
        for _ in range(3):
            if last is None or math.hypot(s.x - last[0], s.y - last[1]) <= 60.0 or time.time() - last[2] > 1.0:
                break
            time.sleep(0.05)
            s2 = self.eye.read()
            if s2 is None:
                break
            s = s2
        if last is not None and math.hypot(s.x - last[0], s.y - last[1]) > JUMP_RESET and self.z is not None:
            # a real jump (teleport, hearth, flight's end, login): the last floor height belongs to another place.
            # As a hint it picked a wrong floor (Slaughtered Lamb start: main search failed, 42-61 s planning)
            self.log(f"position jumped {math.hypot(s.x - last[0], s.y - last[1]):.0f} yd: floor height re-picked")
            self.z = None
        self._last_xy = (s.x, s.y, time.time())
        # an edit box left holding the keyboard (a chat line whose Enter got lost) eats every key: the moves type
        # into it (inst3 23:30-23:37: a ghost typed "w w aw dsw" into a Say line, turned on the pad, never moved).
        # Esc clears it without sending. Only BotEye build 5+ reports focus (older builds: the always-shown box).
        if getattr(getattr(s, "rxp", None), "build", 0) >= 5 and getattr(s, "chat_open", False) and time.time() - getattr(self, "_chat_esc_t", 0.0) > 1.5:
            self._chat_esc_t = time.time()
            self.log("an edit box holds the keyboard: Esc")
            self.keys.press("esc")
        if self.z is None:
            # no floor hint (start, a jump): polestar's snap - never a pocket floor, then the lowest building floor
            # when the client says indoors, else the floor on the terrain (the street, not a roof)
            fl = getattr(s, "flags", None)
            z = seed_floor(self.nm, s.x, s.y, bool(fl & INDOOR_FLAG) if isinstance(fl, int) else None)
        else:
            z = floor_z(self.nm, s.x, s.y, self.z)      # edge-distance floor pick: a drop is seen (FLOOR_*)
        hp = getattr(s, "hp_pct", None)
        if (z is not None and self.z is not None and z < self.z - BIG_DROP and isinstance(hp, (int, float))
                and hp >= getattr(self, "_hp_last", hp) - 5 and not getattr(s, "swimming", False)
                and any(abs(f - self.z) <= 6.0 for dx, dy in BIG_DROP_PROBE
                        for f in self._floors_safe(s.x + dx, s.y + dy))):
            # a 20+ yd drop costs hp; none lost: the floor pick jumped to a layer under us (inst3 04:04:46 at the
            # frozen lake: ice 404 -> its bed 350.7, every route replanned along the bed into the bank). The old
            # floor is looked for around us too: at the ice's edge the xy read can sit just past the ice polygon
            # (inst3 04:33:48, -5445,-141: bed 351.8 taken, the ice 408.7 a yard off)
            if time.time() - getattr(self, "_drop_log_t", 0.0) > 10:
                self._drop_log_t = time.time()
                self.log(f"floor pick {z:.1f} is {self.z - z:.0f} yd under {self.z:.1f} with no hp lost: kept {self.z:.1f}")
            z = None
        elif z is not None and self.z is not None and z < self.z - BIG_DROP:
            self.log(f"floor dropped {self.z:.1f} -> {z:.1f} at ({s.x:.1f},{s.y:.1f}) hp {hp} "
                     f"(last {getattr(self, '_hp_last', None)}) swim={int(bool(getattr(s, 'swimming', False)))}")
        if isinstance(hp, (int, float)):
            self._hp_last = hp
        if z is not None:
            self.z = z
        s.z = self.z if self.z is not None else 0.0
        return s

    def run(self, on: bool):
        """Start/stop running forward (left stick when a pad is up, else W)."""
        if self.padding():
            self.pad.forward(1.0 if on else 0.0)
            if not on:
                self.pad.turn(0.0)
        elif on:
            self.keys.down("w")
        else:
            self.keys.up("w")
        self.running = on

    def _token(self, want: bool) -> bool:
        """Hold / release the one-pad-at-a-time token (an OS lock on nb/padtoken.lock)."""
        import msvcrt
        if want and self._tok is None:
            f = open(PAD_TOKEN, "a+")
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                self._tok = f
            except OSError:
                f.close()
        elif not want and self._tok is not None:
            try:
                self._tok.seek(0)
                msvcrt.locking(self._tok.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._tok.close()
            self._tok = None
        return self._tok is not None

    def stop(self, keep_token: bool = False):
        if self.pad:
            self.pad.stop()
        if not keep_token and self._tok is not None:
            self._token(False)                    # standing still: let another bot have the pad
            self._fg_t = 0.0                      # and look again on the next move
        self.keys.up("w")
        self.keys.up("left"); self.keys.up("right")
        self.running = False

    def turn(self, err: float):
        """Turn by err radians (positive = left) with a timed arrow press."""
        if abs(err) < 0.02:
            return
        key = "left" if err > 0 else "right"
        self.keys.down(key)
        time.sleep(abs(err) / TURN_RATE)
        self.keys.up(key)

    def aim(self, err: float):
        """Swing the CAMERA by err radians (+ = left) with the pad's right stick, one slice."""
        self.pad.turn(PAD_GAIN * err if abs(err) >= STEER_DEAD else 0.0)
        time.sleep(0.03)

    def aim_done(self):
        if self.pad:
            self.pad.turn(0.0)

    def pan(self, rate: float) -> bool:
        """Swing the camera at a constant rate (rad/s, + = left) with the right stick. Pad only:
        returns False (and does nothing) when the pad is not usable (game window not in front)."""
        if not self.padding():
            return False
        self.pad.turn(rate)
        return True

    def nudge(self, t: float = 0.15):
        """A short step forward: the character swings to the camera heading, so
        what is centred on screen is also what the character faces."""
        if self.padding():
            self.pad.forward(0.5)
            time.sleep(t)
            self.pad.stop()
        else:
            self.keys.down("w")
            time.sleep(t)
            self.keys.up("w")

    def turn_run(self, ang: float, maxt: float = 1.2, tol: float = 0.2) -> float:
        """Turn the character by ang rad (+ = left); returns the turn achieved. A standing character does not turn
        with the pad, so run slowly and steer with the right stick: character and camera swing round together on a
        ~2 yd arc (the motion drive() makes). Closed loop on s.facing. No pad: timed arrow-key turn."""
        f_prev = self.state().facing
        done = 0.0
        if not self.padding():
            self.turn(ang)
            time.sleep(0.1)
            return wrap(self.state().facing - f_prev)
        turning = Deadline(maxt, clock=time)
        try:
            self.pad.forward(SLOW_FWD)
            while turning.running():
                f = self.state().facing
                done += wrap(f - f_prev)
                f_prev = f
                rem = ang - done
                if abs(rem) < tol:
                    break
                self.pad.turn(max(-3.5, min(3.5, PAD_GAIN * rem)))
                time.sleep(0.03)
        finally:
            self.stop()
        time.sleep(0.05)
        return done + wrap(self.state().facing - f_prev)

    def turn_in_place(self, err: float):
        """Face err radians further (+ = left) without walking. Keys: the arrows turn the character on the spot. Pad:
        a standing right-stick turn swings only the camera, and the character snaps to the camera's heading on its
        next step - so swing the camera by err at the measured standing rate (cmd 1.5 -> ~1.9 rad/s, padturn test
        2026-10-17) and let the next drive() step take the heading."""
        if not self.padding():
            self.run(False)
            self.turn(err)
            return
        self.pad.forward(0.0)
        self.pad.turn(IN_PLACE_CMD if err > 0 else -IN_PLACE_CMD)
        time.sleep(min(2.0, abs(err) / IN_PLACE_RATE))
        self.pad.turn(0.0)
        self._pd_prev = None
        self.running = False
        self._tip_t = time.time()        # the next drive() step takes the heading: no second turn before it

    def sync_heading(self, maxt: float = 0.6):
        """Replacement for nudge() after a camera swing: step forward until the character has caught up with
        the camera heading (facing stops changing). A fixed 0.12 s only turns ~0.4 rad."""
        if not self.padding():
            return self.nudge(0.12)
        syncing = Deadline(maxt, clock=time)
        f = self.state().facing
        still = 0
        self.pad.forward(0.5)
        try:
            time.sleep(0.08)
            while syncing.running():
                f2 = self.state().facing
                still = still + 1 if abs(wrap(f2 - f)) < 0.01 else 0
                f = f2
                if still >= 3 and syncing.elapsed() > SYNC_MIN_S:
                    break
                time.sleep(0.03)
        finally:
            self.pad.stop()

    def _steer_tele(self, cmd: float, err: float):
        """Pad steering telemetry: commanded turn rate vs the facing rate the strip shows, binned, plus how often the
        heading error flips sign (the weave). One log line per STEER_TELE_S of running."""
        s = self.eye.last
        if s is None:
            return
        tl = self.__dict__.setdefault("_tele", {"t0": time.time(), "prev": None, "bins": {}, "flips": 0, "n": 0,
                                               "aerr": 0.0, "last_sign": 0})
        now = time.time()
        pv = tl["prev"]
        if pv is not None and s.frame != pv[3] and now - pv[0] < 0.3:
            rate = wrap(s.facing - pv[1]) / max(now - pv[0], 1e-3)
            b = min(int(abs(pv[2]) / 0.25), 8)
            sm = tl["bins"].setdefault(b, [0, 0.0])
            sm[0] += 1; sm[1] += abs(rate) * (1 if rate * pv[2] >= 0 else -1)
        if pv is None or s.frame != pv[3]:
            tl["prev"] = (now, s.facing, cmd, s.frame)
        sg = 1 if err > 0.03 else -1 if err < -0.03 else 0
        if sg and tl["last_sign"] and sg != tl["last_sign"]:
            tl["flips"] += 1
        if sg:
            tl["last_sign"] = sg
        tl["n"] += 1; tl["aerr"] += abs(err)
        if now - tl["t0"] >= STEER_TELE_S:
            span = now - tl["t0"]
            curve = " ".join(f"{b * 0.25:.2f}+:{v[1] / v[0]:+.2f}({v[0]})" for b, v in sorted(tl["bins"].items()) if v[0])
            self.log(f"steer tele: {span:.0f}s err flips {tl['flips'] / span:.2f}/s mean |err| "
                     f"{tl['aerr'] / max(tl['n'], 1):.3f} rad; cmd rad/s -> measured: {curve}")
            self._tele = {"t0": now, "prev": None, "bins": {}, "flips": 0, "n": 0, "aerr": 0.0, "last_sign": 0}

    def drive(self, err: float, fwd: float = 1.0):
        """Move forward while steering towards a bearing error (radians, + = left).
        With the pad, forward follows the CAMERA heading, so arrow-key turns get
        undone the moment the stick goes forward; turn with the right stick only
        and slow down in sharp turns so the character takes an arc.
        fwd < 1: slow down (a sharp corner close ahead, off the planned line) - the stick eases off; with posted keys
        (PAD_OFF, the normal mode) W is held for fwd of every W_PULSE_T s while the arrows keep turning, so the
        character turns on a tighter arc than the ~2 yd of a full-speed arrow turn."""
        if self.padding():
            self.pad.forward(min(fwd, SLOW_FWD if abs(err) > SLOW_ERR else 1.0))
            now = time.time()
            pe = getattr(self, "_pd_prev", None)
            d = 0.0
            if pe is not None and 0.01 < now - pe[1] < 0.3 and abs(wrap(err - pe[0])) < 0.5:
                d = max(-3.0, min(3.0, wrap(err - pe[0]) / (now - pe[1])))
                d = PAD_D_ALPHA * d + (1 - PAD_D_ALPHA) * getattr(self, "_pd_d", 0.0)
            self._pd_d = d
            self._pd_prev = (err, now)
            cmd = pad_rate(err, d)
            self.pad.turn(cmd)
            self._steer_tele(cmd, err)
            self.running = True
            time.sleep(0.03)
            return
        if abs(err) > 1.2:
            self.run(False)
            self.turn(err)
            time.sleep(0.25)
            return
        # duty cycle by accumulator, not the clock: at a 0.15/0.3 s loop period the clock phase repeats and W stayed
        # off for whole stuck windows (trail diagnosis 17:5x)
        self._wacc = 0.0 if fwd >= 0.99 else getattr(self, "_wacc", 0.0) + fwd
        on = fwd >= 0.99 or self._wacc >= 1.0
        if on:
            self._wacc = max(0.0, self._wacc - 1.0)
        if on and not self.running:
            self.run(True)
        elif not on and self.running:
            self.run(False)
        self.steer(err)

    def steer(self, err: float):
        """One slice of proportional steering while running: hold the turn key
        for a fraction of the slice, so the turn rate follows the error like a
        mouse would, instead of whole-key bursts."""
        if self.padding():
            # analog: the right stick sets the turn rate continuously
            self.pad.turn(STEER_GAIN * err if abs(err) >= STEER_DEAD else 0.0)
            time.sleep(0.03)
            return
        if abs(err) < STEER_DEAD:
            time.sleep(STEER_DT)
            return
        rate = max(-TURN_RATE, min(TURN_RATE, STEER_GAIN * err))
        pulse = abs(rate) / TURN_RATE * STEER_DT
        key = "left" if rate > 0 else "right"
        if pulse >= 0.006:
            self.keys.down(key)
            time.sleep(pulse)
            self.keys.up(key)
        time.sleep(max(0.0, STEER_DT - pulse))

    def face(self, x, y, tol=0.1):
        for _ in range(3):
            s = self.state()
            err = wrap(bearing(s.x, s.y, x, y) - s.facing)
            if abs(err) < tol:
                return True
            self.turn(err)
            time.sleep(0.05)
        return False

    def face_angle(self, ang, tol=0.1):
        for _ in range(3):
            s = self.state()
            err = wrap(ang - s.facing)
            if abs(err) < tol:
                return True
            self.turn(err)
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------- paths
    def _nopath_key(self, s, x, y, z):
        """Cache key of a 'no path' answer: start island, a NOPATH_CELL yd start cell, our level (the server-mesh
        fallback's danger zones are level-based) and the goal to 3 yd. None when the start is off the mesh."""
        try:
            pz = getattr(s, "z", None) or self.nm.ground_z(s.x, s.y)       # state() fills z from the mesh (0: unknown)
            node = self.nm.nearest_poly(s.x, s.y, pz)[0] if pz is not None else None
            isl = self.nm.components().get(node) if node is not None else None
        except Exception as e:
            swallowed("move.nopath_key", e, self.log)
            isl = None
        if isl is None:
            return None
        return nopath_key(isl, s.x, s.y, getattr(s, "level", 0), x, y, z)

    def path(self, x, y, z=None):
        t0 = time.time()
        s0 = self.state()
        lv = getattr(s0, "level", None)
        for m in (self.nm, getattr(self, "nm_server", None)):
            if m is not None:
                m.level = lv if isinstance(lv, int) and lv > 0 else None     # zone danger (navmesh / zones.py)
        key = self._nopath_key(s0, x, y, z)
        until = self._nopath.get(key, 0.0) if key is not None else 0.0
        if until > t0:
            self.log(f"no path to ({x:.0f},{y:.0f}) from island {key[0]} (cached, {until - t0:.0f}s left)")
            return None
        # one plan stacks many searches (wedge spots on/off, free-start retry, penalty rounds, floor and island
        # fallbacks), each with its own budget: inst4 2026-10-15 09:29-09:31 stood 133.6 s for "none". Cap the whole
        meshes = [m for m in (self.nm, getattr(self, "nm_server", None)) if m is not None]
        # the character stands still while we plan: a long plan is given up as soon as we are hit or in combat
        # (inst2 15:47-15:48: a 40 s plan, then another, died with no fight line; peer review 2026-10-15)
        self.plan_hit = None
        hp0, ab = getattr(s0, "hp_pct", 100.0), getattr(self, "_plan_abort", None) or getattr(self, "_abort", None)
        c0 = bool(getattr(s0, "combat", False))
        if math.hypot(x - s0.x, y - s0.y) > 250:
            self.log(f"planning {math.hypot(x - s0.x, y - s0.y):.0f} yd to ({x:.0f},{y:.0f}) ({len(self.zones)} danger zones)")
        chk = {"t": t0}

        def interrupt():
            now = time.time()
            if self.plan_hit is not None:
                return True
            if now - chk["t"] < PLAN_CHECK_S:
                return False
            chk["t"] = now
            try:
                st = self.state()
            except Exception as e:
                swallowed("move.plan_interrupt", e, self.log)
                return False
            # only a change counts: a flee / chase route is planned in combat and losing hp
            fresh = not c0 and st.combat
            hit = not c0 and st.hp_pct < hp0 - PLAN_HIT_HP
            if fresh or hit or st.dead or (ab is not None and ab(st)):
                self.plan_hit = ("combat" if fresh else f"hp {hp0:.0f} -> {st.hp_pct:.0f}" if hit
                                 else "dead" if st.dead else "abort")
                return True
            return False
        for m in meshes:
            m.deadline = t0 + PATH_DEADLINE
            m.interrupt = interrupt
        try:
            r = self._path(x, y, z)
            if r:
                r = self._indoor_legs(s0, r, x, y, z)
        finally:
            for m in (self.nm, getattr(self, "nm_server", None)):
                if m is not None:
                    m.deadline = None
                    m.interrupt = None
        if self.plan_hit is not None:
            self.log(f"plan to ({x:.0f},{y:.0f}) given up after {time.time() - t0:.1f}s: {self.plan_hit}")
            return None                    # not cached as "no path": the mesh did not say so
        dt = time.time() - t0
        if dt > 1.5:                       # the character stands still while we plan
            s = self.state()
            self.log(f"slow path: {dt:.1f}s to plan {math.hypot(x - s.x, y - s.y):.0f} yd "
                     f"({'found by ' + str(getattr(self, 'path_strategy', None)) if r else 'none'}, "
                     f"{len(self.zones)} danger zones)")
        if key is not None:
            if r:
                self._nopath.pop(key, None)
            else:
                now = time.time()
                self._nopath = {k: u for k, u in self._nopath.items() if u > now}
                self._nopath[key] = now + NOPATH_TTL
        return r

    def _inside(self, x, y, z) -> bool:
        try:
            import vmap
            return bool(z) and vmap.inside_building(x, y, z)
        except Exception as e:
            swallowed("move.inside", e, self.log)
            return False

    def _indoor_legs(self, s0, r, x, y, z):
        """Re-plan the route's indoor ends on INDOOR_MESH: from the start to where the route leaves the building, and
        from where it enters the goal's building to the goal (the whole route when it never leaves). The Forever
        mesh keeps the outdoor part (its terrain is this client's; r4's hills differ)."""
        if not INDOOR_MESH or not os.path.isdir(INDOOR_MESH):
            return r
        sz = getattr(self, "z", None) if getattr(self, "z", None) is not None else s0.z
        gz = r[-1][2] if r[-1][2] else z
        a_in, b_in = self._inside(s0.x, s0.y, sz), self._inside(x, y, gz)
        if not (a_in or b_in):
            return r
        try:
            if getattr(self, "nm_in", None) is None:
                self.nm_in = navmesh.NavMesh(INDOOR_MESH, curmap.MAP)
            out = [tuple(p) for p in r]
            outdoor = [i for i, p in enumerate(out) if not self._inside(p[0], p[1], p[2] or sz)]
            if a_in and b_in and not outdoor:
                p = self.nm_in.find_path((s0.x, s0.y, sz), (x, y, gz), budget=INDOOR_BUDGET)
                if p:
                    self.log(f"indoor route on the indoor mesh: {len(p)} pts")
                    return [tuple(q) for q in p]
                return r
            if b_in and outdoor:
                j = outdoor[-1]
                p = self.nm_in.find_path(out[j], (x, y, gz), budget=INDOOR_BUDGET)
                if p:
                    out = out[:j + 1] + [tuple(q) for q in p]
                    self.log(f"goal indoors: last {len(p)} pts on the indoor mesh")
            if a_in and outdoor:
                i = outdoor[0]
                p = self.nm_in.find_path((s0.x, s0.y, sz), out[i], budget=INDOOR_BUDGET)
                if p:
                    out = [tuple(q) for q in p] + out[i + 1:]
                    self.log(f"start indoors: first {len(p)} pts on the indoor mesh")
            return out
        except Exception as e:
            swallowed("move.indoor_legs", e, self.log)
            return r

    # The planner's fallbacks, tried in this order until one gives a route; all of them share the whole-plan
    # PATH_DEADLINE (path() sets nm.deadline, which every find_path below honours). Each strategy is
    # _ps_<name>(plan) -> route or None; plan holds the start state, the goal and what earlier strategies worked
    # out (the danger zones and wedge spots are prepared by "main" and reused by the later ones).
    #   alt_mesh     wedged again and again in a building: the first ALT_MESH_LEG yd on the server's data_r3 mesh
    #                (only while client_indoor; _alt_mesh_path, _alt_leg_bad)
    #   gate         leaving a sealed NPC spot through the gate learned for it (goal_island)
    #   island_start standing on a small sealed patch: walk to the nearest real mesh first
    #   main         the bot's mesh round danger zones and wedge spots (_find: the holes-attempt budget, then
    #                without the spots; penalties always), plus the zones a far route bends into
    #   no_zones     the same without danger zones (a route through one beats no route)
    #   start_floor  every other floor at our xy as the start (the strip has no height)
    #   goal_island  the goal on a sealed patch: to the nearest real mesh within 40 yd (learns a gate)
    #   server_mesh  the server's own mesh (only on the 1.12-era meshes, not the Forever one)
    # Stuck recovery in goto (flip_floor, block_crossing / _climb_into / block_uphill_into, the _floor_at_path
    # fall check) changes what these plan on; it is not a strategy of its own.
    PATH_STRATEGIES = ("alt_mesh", "gate", "island_start", "main", "no_zones", "start_floor", "goal_island",
                       "server_mesh")

    def _path(self, x, y, z=None):
        self.path_src = "bot"            # "server" when only the server mesh had a route (goto steers it tighter)
        self.path_strategy = None
        s = self.state()
        gz = z if z is not None else self.nm.ground_z(x, y)
        if gz is None:
            return [(x, y, s.z)]
        plan = SimpleNamespace(s=s, x=x, y=y, z=z, gz=gz,
                               far=math.hypot(x - s.x, y - s.y) > 250,      # danger-weighted A* over 1000+ yd never finishes
                               zones=(), all_zones=(), spots={})
        for name in self.PATH_STRATEGIES:
            if getattr(self, "plan_hit", None) is not None:
                return None                  # interrupted (path()): no more fallbacks
            p = getattr(self, "_ps_" + name)(plan)
            if p:
                self.path_strategy = name
                if name != "main":
                    self.log(f"path to ({x:.0f},{y:.0f}): {len(p)} points by strategy {name}")
                return p
        return None

    @staticmethod
    def _floats(p) -> list:
        return [(float(a), float(b), float(c)) for a, b, c in p]

    def _ps_alt_mesh(self, plan):
        s = plan.s
        alt = getattr(self, "_alt_mesh", None)
        # only while really indoors: on the hillside outside the Algaz bunker data_r3 also climbs the 332 -> 338 rise,
        # and it knows none of the Forever mesh's blocked steps (inst4 07:40-07:41)
        if alt and time.time() < alt[2] and math.hypot(s.x - alt[0], s.y - alt[1]) < ALT_MESH_R and client_indoor(s):
            return self._alt_mesh_path(s, plan.x, plan.y, plan.gz, alt)
        return None

    def _ps_gate(self, plan):
        s, x, y, gz = plan.s, plan.x, plan.y, plan.gz
        for ex, goal, need_in in self.gates:
            dg, de = math.hypot(s.x - goal[0], s.y - goal[1]), math.hypot(s.x - ex[0], s.y - ex[1])
            fl = getattr(s, "flags", None)
            if need_in and isinstance(fl, int):
                # IsIndoors(): really in the hut; right at the NPC (20 yd inside from the door) counts as well, in
                # case the client calls a small hut outdoors
                use = (bool(fl & INDOOR_FLAG) and dg < 25 and de < 40) or dg < 6
            else:
                use = dg < 12 or de < 9            # no indoor flag (or a learned gate): only right beside it
            if use:
                # the learned gate is the real polygon nearest the goal, which can lie through a wall (Neal Allen's
                # Menethil shop: 3 stucks on its back wall, #1536 rxp45): the collision grid walks to it, or the gate
                # is skipped and island_start finds a door
                hop = self._gate_hop(s, ex)
                if hop is None:
                    continue
                rest = self.nm.find_path(ex, (x, y, gz), dangers=(), budget=30)
                if rest:
                    self.log(f"leaving the sealed spot via its gate ({ex[0]:.0f},{ex[1]:.0f})")
                    return [(s.x, s.y, s.z)] + hop + [ex] + self._floats(rest)
        return None

    def _gate_hop(self, s, ex):
        """The collision grid's way from s to a gate (inner points), or None when it cannot walk there."""
        try:
            import gridpath
            gridpath.MESH = self.nm
            fails = []
            r = gridpath.refine([(s.x, s.y, s.z), tuple(ex)], log=lambda m: None, fails=fails)
        except Exception as e:
            swallowed("move.gate_hop", e, self.log)
            return []
        if fails or len(r) < 2 or math.dist(r[-1][:2], ex[:2]) > 2.0:
            self.log(f"the gate ({ex[0]:.0f},{ex[1]:.0f}) is not walkable from here: skipped")
            return None
        return [tuple(float(c) for c in p) for p in r[1:-1]]

    def _ps_island_start(self, plan):
        # standing on a small sealed patch (a hut floor): walk to the nearest real mesh first
        s = plan.s
        try:
            lead = self.nm.island_exit((s.x, s.y, s.z), 40)
        except Exception as e:
            swallowed("move.island_exit_start", e, self.log)
            lead = None
        if lead:
            # the nearest exit can lie through a wall (Neal Allen's Menethil shop: the mesh lacks its floor, the
            # nearest real polygon was past the back wall; 9 stucks, #1215 rxp37 12:28): take the nearest exit the
            # collision grid walks to from here, by the grid's way
            hop = None
            try:
                import gridpath
                gridpath.MESH = self.nm
                for cand in self.nm.island_exits((s.x, s.y, s.z), 40, n=ISLAND_EXIT_TRIES):
                    fails = []
                    r = gridpath.refine([(s.x, s.y, s.z), cand], log=lambda m: None, fails=fails)
                    if not fails and len(r) > 2 and math.dist(r[-1][:2], cand[:2]) < 2.0:
                        lead, hop = cand, [tuple(float(c) for c in p) for p in r[1:-1]]
                        # out of the door, the real mesh is under us: leave the grid there (its way on to this
                        # exit polygon can double back round the building)
                        for k, q in enumerate(hop):
                            if self.nm.on_big_patch(q):
                                lead, hop = q, hop[:k]
                                break
                        break
            except Exception as e:
                swallowed("move.island_exit_grid", e, self.log)
            self.log(f"on a mesh island at ({s.x:.0f},{s.y:.0f}); leaving via ({lead[0]:.0f},{lead[1]:.0f})"
                     + (f" by the grid ({len(hop)} pts)" if hop else ""))
            rest = self.nm.find_path(lead, (plan.x, plan.y, plan.gz), dangers=(), budget=30)
            if rest:
                return [(s.x, s.y, s.z)] + (hop or []) + [lead] + self._floats(rest)
        return None

    def _ps_main(self, plan):
        s, x, y, gz, far = plan.s, plan.x, plan.y, plan.gz, plan.far
        zones = tuple(self.zones)
        # a zone around the start or the goal cannot be avoided (14:12: a 150 yd death zone covered the
        # Anvilmar door; every plan from inside failed, 13.5 s each, and the bot wedged upstairs)
        zones = tuple(z for z in zones if math.hypot(z[0] - s.x, z[1] - s.y) > z[3]
                      and math.hypot(z[0] - x, z[1] - y) > z[3])
        all_zones = zones
        if far:          # only the zones near the straight line, else the search floods the map
            dx, dy = x - s.x, y - s.y
            n2 = dx * dx + dy * dy or 1.0
            def near_line(z):
                t = max(0.0, min(1.0, ((z[0] - s.x) * dx + (z[1] - s.y) * dy) / n2))
                return math.hypot(z[0] - (s.x + t * dx), z[1] - (s.y + t * dy)) < z[3] + 60
            zones = tuple(z for z in zones if near_line(z))
        spots = self._spot_kw(s, x, y)
        plan.zones, plan.all_zones, plan.spots = zones, all_zones, spots
        p = self._find(spots, (s.x, s.y, s.z), (x, y, gz), dangers=zones, budget=(12 if zones else 30) if far else 10)
        # the real route can bend far off the straight line into zones that filter dropped (inst2 16:04: the road to
        # Kharanos ran 29 yd from four Snow Tracker Wolves 214 yd off the line): add the ones it walks through, re-plan
        for _ in range(2 if p and far else 0):
            add = tuple(z for z in all_zones if z not in zones and walks_through(p, z))
            if not add:
                break
            p2 = self._find(spots, (s.x, s.y, s.z), (x, y, gz), dangers=zones + add, budget=12)
            if not p2:
                break
            zones, p = zones + add, p2
            plan.zones = zones
        return self._floats(p) if p else None

    def _ps_no_zones(self, plan):
        if not plan.zones:                     # zones are a preference: a route through one beats no route
            return None
        s = plan.s
        p = self._find(plan.spots, (s.x, s.y, s.z), (plan.x, plan.y, plan.gz), dangers=(), budget=30)
        return self._floats(p) if p else None

    def _ps_start_floor(self, plan):
        # the strip has no height (z reads 0): in a building the start polygon can be the wrong
        # floor (hut: the roof level above the floor we stand on). Try every floor at our xy.
        s = plan.s
        sg = self.nm.ground_z(s.x, s.y)
        cand = self.floors_at(s.x, s.y)
        if sg is not None:                     # lower floors / islands the polygon test misses
            cand += [sg - 4.0 * k for k in range(1, 6)]
        # a floor in a small mesh pocket last (polestar's snap: pockets only when nothing else plans; the terrain under
        # Stormwind's city model won from the street at 105 and the walk wedged 9 times on the city above, #1600)
        try:
            cand.sort(key=lambda fz: bool(self.nm.in_pocket(s.x, s.y, fz)))
        except Exception as e:
            swallowed("move.start_floor_pocket", e, self.log)
        for fz in cand:
            p = self.nm.find_path((s.x, s.y, fz), (plan.x, plan.y, plan.gz), dangers=plan.zones, budget=4)
            if p:
                self.log(f"start floor z={fz:.0f} at ({s.x:.0f},{s.y:.0f}) has a path")
                return self._floats(p)
        return None

    def _ps_goal_island(self, plan):
        # the goal can sit on a small sealed patch of mesh (a hut floor): path to the nearest
        # real mesh within 40 yd and let goto make the last leg on foot
        s, x, y = plan.s, plan.x, plan.y
        try:
            ex = self.nm.island_exit((x, y, plan.gz), 40)
        except Exception as e:
            swallowed("move.island_exit_goal", e, self.log)
            ex = None
        if ex:
            p = self.nm.find_path((s.x, s.y, s.z), ex, dangers=(), budget=30)
            if p:
                self.gates.append((ex, (x, y), False))     # the door of that sealed spot: leave the same way
                return self._floats(p)
        return None

    def _ps_server_mesh(self, plan):
        if NB_MESH == navmesh.TCF_MMAP_DIR:
            return None
        # (only on the 1.12-era meshes: the Forever mesh is this client's own terrain, and data_r3 is not)
        # the bot mesh (50 deg slope cap) has no route up some steep trails the game allows (the hill path to
        # the Spirit Stone in northern Coldridge, 2026-10-14): fall back to the server's own mesh
        s, x, y, z, zones, spots = plan.s, plan.x, plan.y, plan.z, plan.zones, plan.spots
        p = None
        try:
            if getattr(self, "nm_server", None) is None:
                self.nm_server = navmesh.NavMesh(navmesh.MMAP_DIR, curmap.MAP)
            gz2 = z if z is not None else self.nm_server.ground_z(x, y)
            if gz2 is not None:
                # this mesh goes where the bot mesh does not (hill trails): keep clear of strong spawns there
                # (13:30 the fallback route led a level 5 shaman past Vejrek, a level 11 named troll)
                sz = tuple(zones)
                try:
                    import dangers
                    pad = 150.0
                    bbox = (min(s.x, x) - pad, min(s.y, y) - pad, max(s.x, x) + pad, max(s.y, y) + pad)
                    sz += tuple(dangers.zones(0, getattr(s, "level", 1) or 1, 3, bbox=bbox))   # 3 = dwarf faction
                except Exception as e:
                    self.log(f"server mesh: no danger zones ({e!r})")
                # round this walk's wedges and the shared wedge spots too, else a replan after a stuck on a
                # hill trail finds the same route again
                p = self.nm_server.find_path((s.x, s.y, s.z), (x, y, gz2), dangers=sz, budget=30,
                                             **spots) if spots else None
                if not p:
                    p = self.nm_server.find_path((s.x, s.y, s.z), (x, y, gz2), dangers=sz, budget=30)
                if p and sz:
                    hits = [d for d in sz for q in p if math.hypot(float(q[0]) - d[0], float(q[1]) - d[1]) < d[3]]
                    if hits:
                        self.log(f"server mesh route to ({x:.0f},{y:.0f}) crosses {len(hits)} danger points; not taking it")
                        p = None
                if p:
                    self.log(f"path to ({x:.0f},{y:.0f}) only on the server mesh ({len(p)} points)")
                    self.path_src = "server"
        except Exception as e:
            self.log(f"server mesh fallback failed: {e!r}")
        return self._floats(p) if p else None

    def _alt_mesh_path(self, s, x, y, gz, alt):
        """Wedged again and again in a building on the Forever mesh: walk out on the server's data_r3 mesh, whose
        buildings match this client's walls (Algaz bunker, inst4 2026-10-15 07:14-07:24: the Forever mesh ran one
        polygon from the ground floor through a wall to (-4800,-2683); data_r3 and the bot mesh both leave by the
        north exit (-4816,-2642)). Only the leg within ALT_MESH_LEG yd of the wedge; the Forever mesh from there."""
        # The leg knew nothing learned: from the ground floor wedge (-4815,-2668) data_r3 went out the north passage
        # and up the rise the Forever mesh has proven-blocked ((9,21) 10 -> 13), and its heights (3-3.5 yd above the
        # Forever floor there) fired a false 'off the path's floor' replan at (-4800.8,-2633.4) every time, back into
        # the wall: a ping-pong for 45+ min (inst4 07:44-07:58). So: plan it round the same spots / wedges / traps,
        # reject it when it crosses a blocked Forever step or passes a known wedge, and give its points Forever heights.
        try:
            if getattr(self, "nm_server", None) is None:
                self.nm_server = navmesh.NavMesh(navmesh.MMAP_DIR, curmap.MAP)
            gz2 = self.nm_server.ground_z(x, y)
            a = (s.x, s.y, self.z if self.z is not None else s.z)
            kw = dict(self._spot_kw(s, x, y))
            now = time.time()
            traps = tuple((hx, hy, hz, hr) for hx, hy, hz, hr, t in getattr(self, "_trap_holes", [])
                          if LEARNED_SPOTS and t > now and math.hypot(hx - s.x, hy - s.y) > hr + 1.0)
            if traps:
                kw["holes"] = tuple(kw.get("holes", ())) + traps
            own = self._own_pen(a)
            if own:
                kw["spot_pen"] = tuple(kw.get("spot_pen", ())) + own
            ps = self.nm_server.find_path(a, (x, y, gz2 if gz2 is not None else gz), budget=10, **kw)
            if not ps:
                return None
            ps = [(float(q[0]), float(q[1]), float(q[2])) for q in ps]
            cut = next((i for i, q in enumerate(ps) if math.hypot(q[0] - alt[0], q[1] - alt[1]) > ALT_MESH_LEG), None)
            why = self._alt_leg_bad(a, ps[:len(ps) if cut is None else cut + 1], kw)
            if why:
                self._alt_mesh = None
                self._alt_off = (alt[0], alt[1], now + ALT_OFF_S)
                self.log(f"server mesh leg {why}: not taking it (Forever mesh only here for {ALT_OFF_S / 60:.0f} min)")
                return None
            # Forever heights for the leg (the fall check and block_crossing compare them with our Forever floor)
            for i, q in enumerate(ps):
                fz = floor_z(self.nm, q[0], q[1], hint=q[2])
                if fz is not None and abs(fz - q[2]) < 6.0:
                    ps[i] = (q[0], q[1], fz)
            if cut is None:
                out = ps
            else:
                # the Forever rest round the same spots (it went straight back to the rise without them)
                rest = self._find(self._spot_kw(SimpleNamespace(x=ps[cut][0], y=ps[cut][1], z=ps[cut][2]), x, y),
                                  ps[cut], (x, y, gz), budget=30)
                if not rest:
                    return None
                out = ps[:cut + 1] + [(float(a), float(b), float(c)) for a, b, c in rest]
            self.log(f"wedged in a building: the first {ALT_MESH_LEG:.0f} yd on the server mesh "
                     f"(first {[(round(q[0]), round(q[1])) for q in out[:3]]})")
            self.path_src = "server"
            return out
        except Exception as e:
            self.log(f"server mesh leg failed: {e!r}")
            return None

    def _alt_leg_bad(self, a, leg, kw):
        """Why a server-mesh leg must not be walked, or None: it crosses a blocked Forever-mesh step (sampled every
        yard, each point mapped onto the Forever polygon at its own height), or passes within ALT_CLEAR yd of a
        known wedge spot / trap / recent wedge heading (kw's spot_pen and holes; none round the start a)."""
        pts = [a] + list(leg)
        prev = None
        for p, q in zip(pts, pts[1:]):
            n = max(1, int(math.dist(p[:2], q[:2])))
            for i in range(n + 1):
                f = i / n
                px, py, pz = p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f, p[2] + (q[2] - p[2]) * f
                node, _ = self.nm.nearest_poly(px, py, pz)
                if node is None:
                    continue
                if prev is not None and node != prev and (prev, node) in self.nm.blocked_climbs:
                    return f"crosses a blocked Forever step at ({px:.0f},{py:.0f})"
                prev = node
        circles = [c[:4] for c in kw.get("spot_pen", ())] + [c[:4] for c in kw.get("holes", ())]
        for cx, cy, cz, _r in circles:
            if math.hypot(cx - a[0], cy - a[1]) < 2.0:
                continue
            for p, q in zip(pts, pts[1:]):
                t, d = seg_proj(cx, cy, p[0], p[1], q[0], q[1])
                if d < ALT_CLEAR and (cz is None or abs(p[2] + t * (q[2] - p[2]) - cz) < 4.0):
                    return f"passes the wedge spot ({cx:.0f},{cy:.0f})"
        return None

    def _spot_kw(self, s, x, y) -> dict:
        """find_path keywords for the known wedge spots in the route's box: indoors holes (r BAD_HOLE_R: the small
        room polygons go, plus a detour box), outdoors avoid circles (r BAD_AVOID_R, a cost), and this walk's own
        stuck points as holes. Outdoor spots are also detour boxes (navmesh detours=, r BAD_HOLE_R): the avoid cost
        only hits a step whose portal midpoint lies in the circle, and outdoor portals are long (offline replay: the
        Felix -> Talin Keeneye path kept passing 0.6 yd from the 3-walk spot (-6170.8,397.0) with avoid alone).
        Spots under our feet or at the goal cannot be avoided and are left out."""
        if not LEARNED_SPOTS:
            return {}
        holes, avoid, detours, pen = list(self._wedges), [], [], []
        pad = min(80.0, 20.0 + 0.25 * math.hypot(x - s.x, y - s.y))
        bbox = (min(s.x, x) - pad, min(s.y, y) - pad, max(s.x, x) + pad, max(s.y, y) + pad)
        try:
            act = self.bad.active(bbox)
        except Exception as e:
            swallowed("move.spot_kw", e, self.log)
            act = []
        for sp in act:
            if math.hypot(sp["x"] - s.x, sp["y"] - s.y) < 2.0 or math.hypot(sp["x"] - x, sp["y"] - y) < 2.5:
                continue
            sz = sp.get("z")
            sz = s.z if sz is None else sz
            w = spot_weight(sp)
            if sp.get("in"):
                holes.append((sp["x"], sp["y"], sz, BAD_HOLE_R))
                pen.append((sp["x"], sp["y"], sz, BAD_PEN_R_IN, w))
            else:
                avoid.append((sp["x"], sp["y"], sz, BAD_AVOID_R, w))
                detours.append((sp["x"], sp["y"], sz, BAD_HOLE_R))
                pen.append((sp["x"], sp["y"], sz, BAD_PEN_R_OUT, w))
        kw = {}
        if holes:
            kw["holes"] = tuple(holes)
        if avoid:
            kw["avoid"] = tuple(avoid)
            kw["detours"] = tuple(detours)
        if pen:
            kw["spot_pen"] = tuple(pen)
        return kw

    def _own_pen(self, a) -> tuple:
        """Penalty circles that hold whatever else is dropped: the ways recent walks wedged heading (WEDGE_AHEAD) and
        the broken-out traps (TRAP_HOLE_R), BAD_PEN_MAX each; none round the start a."""
        if not LEARNED_SPOTS:
            return ()
        now = time.time()
        self._wedge_pen = [w for w in getattr(self, "_wedge_pen", []) if w[5] > now]
        out = [w[:5] for w in self._wedge_pen if math.hypot(w[0] - a[0], w[1] - a[1]) > w[3] + 1.5]
        out += [(hx, hy, hz, hr, BAD_PEN_MAX) for hx, hy, hz, hr, t in getattr(self, "_trap_holes", [])
                if t > now and math.hypot(hx - a[0], hy - a[1]) > hr + 1.0]
        return tuple(out)

    def _note_wedge_ahead(self, s, route, k, clamp):
        """Remember the way this wedge was heading (WEDGE_AHEAD yd along the route) as a penalty for later plans."""
        try:
            cx, cy, _, _ = look_ahead(route, k, clamp, s.x, s.y, WEDGE_AHEAD)
        except Exception as e:
            swallowed("move.note_wedge_ahead", e, self.log)
            return
        if math.hypot(cx - s.x, cy - s.y) < 1.0:
            return
        now = time.time()
        self._wedge_pen = [w for w in getattr(self, "_wedge_pen", []) if w[5] > now
                           and math.hypot(w[0] - cx, w[1] - cy) > 0.7]
        self._wedge_pen.append((cx, cy, s.z, WEDGE_PEN_R, BAD_PEN_MAX, now + WEDGE_PEN_S))

    def _find(self, spots: dict, a, b, **kw):
        """nm.find_path with the wedge spots; without them when they close every route (a hole can fill a door)."""
        # traps that needed a break_out stay holes for TRAP_HOLE_S, even when they close every route: walking back
        # into one cost inst1 15 min at (-5446,-268) (02:45-03:00, 3 walks, 2 break-outs, the same west exit)
        now = time.time()
        if now - getattr(self, "_climbs_read", 0.0) > CLIMB_RELOAD_S:
            self._climbs_read = now
            with swallow("move.reload_climbs", self.log):
                if self.nm.reload_climbs():
                    self.log("climb blocks: merged the other bots' proven steps")
        self._trap_holes = [h for h in getattr(self, "_trap_holes", []) if h[4] > now] if LEARNED_SPOTS else []
        sx, sy = a[0], a[1]
        hard = tuple((hx, hy, hz, hr) for hx, hy, hz, hr, _t in self._trap_holes
                     if math.hypot(hx - sx, hy - sy) > hr + 1.0)    # never a hole round the start
        if hard:
            kw = dict(kw)
            kw["holes"] = tuple(kw.get("holes", ())) + hard
        # penalties cannot close a route, so they stay in the plan without spots too (inst2 07:42-07:44: 'planned
        # without them' 4 times, each replan the path that had just wedged)
        own = self._own_pen(a)
        if own:
            kw = dict(kw)
            kw["spot_pen"] = tuple(kw.get("spot_pen", ())) + own
        if spots:
            merged = {**kw, **spots}
            if hard:
                merged["holes"] = tuple(spots.get("holes", ())) + kw["holes"]
            if own:
                merged["spot_pen"] = tuple(spots.get("spot_pen", ())) + own
            # holes that close every route make the search run to its budget (twice: find_path retries
            # with a free start). inst1 2026-10-15 06:38-06:43 Thelsamar inn: 11 plans of 3-19 yd at 21 s
            # each. A short walk round a hole needs little search; the fallback below has the full budget
            d = math.dist(a[:2], b[:2])
            merged["budget"] = min(kw.get("budget") or 30.0, max(1.5, d / 25.0))
            p = self.nm.find_path(a, b, **merged)
            if p:
                return p
        p = self.nm.find_path(a, b, **kw)
        if p and spots:
            self.log(f"no path round {len(spots.get('holes', ()))} wedge holes / {len(spots.get('avoid', ()))} "
                     f"wedge spots to ({b[0]:.0f},{b[1]:.0f}): planned without them")
        return p

    def set_map(self, m: int):
        """Switch to map m (a boat / portal / teleport to the other continent): its navmesh, and the collision, terrain
        and object layers through curmap. The height is re-picked on the next state read."""
        if m == curmap.MAP and self.nm.map_id == m:
            return
        curmap.MAP = m
        mesh = MAP_MESH.get(m, NB_MESH)
        self.nm = navmesh.NavMesh(mesh, m)
        self.nm_in = self.nm_server = None
        self.gates = []
        self.z = None
        self.log(f"map {m}: navmesh {mesh}")

    def floors_at(self, x, y, dry=False):
        """Heights of the walkable polygons that contain (x, y), highest first. dry: leave out swim-only polygons
        (a water surface beside a dock)."""
        try:
            rx, _, rz = navmesh.wow_to_rc(x, y, 0.0)
            tl = self.nm.tile_at(rx, rz)
            if tl is None:
                return []                            # no tile here (off the loaded map): no floors
            zs = set()
            for i in range(len(tl.pcount)):
                if not tl.usable(i):
                    continue
                f = int(tl.pflags[i])
                if dry and f & navmesh.NAV_WATER and not f & navmesh.NAV_GROUND:
                    continue
                vs = tl.verts[tl.pverts[i, :tl.pcount[i]]]
                if navmesh._point_in_poly_2d(rx, rz, vs):
                    zs.add(round(navmesh.poly_height(rx, rz, vs), 1))
            return sorted(zs, reverse=True)[:6]
        except Exception as e:
            swallowed("move.floors_at", e, getattr(self, "log", None))
            return []

    def goto(self, x, y, z=None, tol=2.0, timeout=120.0, abort=None, careful=False, direct=False, stop=True):
        """Walk to (x, y). abort(state) -> True stops early (returns 'abort').
        direct=True walks the straight line (a RestedXP route waypoint: the guide's trail is meant to be walked
        straight; the server-mesh path to the Spirit Stone wedged the character off the trail, 13:32-13:34).
        stop=False keeps running on an 'ok' arrival (the next leg of a waypoint chain takes over the stick; the caller
        stops if none follows). Any other end stops as before."""
        arrived = False
        self.path_src = "bot"
        # a walk zones long: the bot's far-walk hook first (Bot.far_walk: fly when it saves time), whatever started
        # the walk - the gather walk (a quest chest) took 8 km on foot from Loch Modan through the level 35 Badlands
        # (bots 3 / 4 2026-10-16 03:25, ~150 deaths each)
        far = getattr(self, "far_walk", None)
        if far is not None and not direct:
            s0 = self.state()
            if math.hypot(x - s0.x, y - s0.y) > FAR_WALK_YD:
                try:
                    if far(x, y, z, abort) == "abort":
                        return "abort"
                except Exception as e:
                    self.log(f"far walk hook: {e!r}")
        if direct:
            pts = [(x, y, z or 0.0)]
        else:
            self._plan_abort = abort
            try:
                pts = self.path(x, y, z)
            finally:
                self._plan_abort = None
            if pts is None and getattr(self, "plan_hit", None) is not None:
                return "abort"               # the main loop fights / flees; the walk is planned again after
        if pts is None and far is not None and not direct:
            # no walk at all to a far goal (another landmass, a mountain range): any flight that gets near it
            s0 = self.state()
            if math.hypot(x - s0.x, y - s0.y) > FAR_WALK_YD:
                try:
                    fr = far(x, y, z, abort, True)
                except Exception as e:
                    fr = None
                    self.log(f"far walk hook: {e!r}")
                if fr == "abort":
                    return "abort"
                if fr == "ok":
                    pts = self.path(x, y, z)
        if pts is None:
            self.log(f"no path to ({x:.0f},{y:.0f})")
            s0 = self.state()
            if math.hypot(x - s0.x, y - s0.y) > 30:
                return "nopath"          # a straight line over cliffs only wedges the character
            pts = [(x, y, z or 0.0)]
        if not pts or math.dist(pts[-1][:2], (x, y)) > 3:
            pts = (pts or []) + [(x, y, z or 0.0)]
        plen = sum(math.dist(pts[k][:2], pts[k + 1][:2]) for k in range(len(pts) - 1))
        timeout = max(timeout, plen / 5.0 + 30.0)       # a 1400 yd quest trip needs more than 180 s
        if plen > 60:
            self.log(f"route to ({x:.0f},{y:.0f}): {len(pts)} pts, {plen:.0f} yd, first "
                     + " ".join(f"({p[0]:.0f},{p[1]:.0f})" for p in pts[:3]))
        walk = Deadline(timeout, clock=time)
        # walk id + its goal (spot_active: the same goal walked again within BAD_REPEAT_S is not a second witness)
        self._ep = f"{self.inst}-{walk.t0:.1f}@{x:.0f},{y:.0f}" + ("" if z is None else f",{z:.0f}")
        self._wedges = []
        s = self.state()
        hook = getattr(self, "on_route", None)        # the bot's look-ahead (forecast_walk): log only
        if hook is not None and plen > 20:
            try:
                hook(pts, s)
            except Exception:
                pass
        route, inside, clamp = self.route(s, pts)
        k = 1
        prog, prog_pos = Deadline(STUCK_S, clock=time), None     # no 1.5 yd of progress for STUCK_S: a stuck
        stucks = 0
        flipped = False
        self._big_err_t = None           # (ORBIT_S)
        stuck_pts = []                   # this walk's stuck points (WEDGE_NEAR)
        falls, fall_t = 0, 0.0           # replans after dropping off the path's floor (FALL_*)
        strays, stray_t = 0, 0.0         # replans after straying STRAY_D yd off the route (polestar)
        self._abort = abort              # (escape / break_out check it too: bot 2 12:41:38-12:42:07 sat 30 s in
        try:                             # wedge escapes while a level 20 elite took it from 100% to 38% hp)
            while True:
                if walk.passed():
                    return "timeout"
                s = self.state()
                if abort and abort(s):
                    return "abort"
                sf = getattr(self, "stop_file", None)
                if sf and os.path.exists(sf):
                    return "abort"       # a restart's STOP: end the walk so run() can centre the pad and exit
                if s.dead or s.ghost and False:
                    # one frame is not enough: a read as an edit box opened said dead mid-walk, alive the next
                    # second (#798, rxp30 09:05:24 - the leg was given up and teleported)
                    time.sleep(0.3)
                    s = self.state()
                    if s.dead:
                        return "dead"
                    self.log("a dead read the next frame did not confirm: walking on")
                # swimming, a clamped corner's 0.75 yd is not hit: the bot orbited the shipwreck's refined corners
                # at 5 yd/s for 50 s (Darkshore #830, rxp31 09:24:40-09:25:15) - the open rule takes them in water
                k = advance(route, k, [False] * len(clamp) if getattr(s, "swimming", False) else clamp, s.x, s.y)
                # polestar's follower: the nearest of the next RESYNC_SEGS legs is where we are - an escape or a
                # slide that put us beside a later leg walks on from there, never back to the corner behind
                k = resync(route, k, clamp, s.x, s.y)
                wx, wy = route[k][0], route[k][1]
                # ... and STRAY_D yd off every leg near us, the route is lost: plan again from here
                if (len(route) > 2 and strays < STRAY_MAX and time.time() - stray_t > STRAY_GAP
                        and min(seg_proj(s.x, s.y, route[j - 1][0], route[j - 1][1], route[j][0], route[j][1])[1]
                                for j in range(max(1, k - 1), min(len(route), k + RESYNC_SEGS + 1))) > STRAY_D):
                    strays, stray_t = strays + 1, time.time()
                    self.log(f"strayed {STRAY_D:.0f}+ yd off the route at ({s.x:.0f},{s.y:.0f}): replanning from here")
                    np_ = self.path(x, y, z)
                    if np_:
                        s = self.state()
                        route, inside, clamp = self.route(s, np_ + [(x, y, z or 0.0)])
                        k = 1
                        prog_pos = None
                        prog.restart()
                        continue
                if k == len(route) - 1 and math.hypot(wx - s.x, wy - s.y) < tol:
                    arrived = True
                    return "ok"
                # dropped off the path's floor indoors (off a stair side): the rest of the route is overhead
                if (falls < FALL_MAX and (inside[k] or inside[k - 1])
                        and time.time() - fall_t > FALL_GAP):
                    ez = leg_z(route, k, s.x, s.y)
                    if ez is not None and s.z and s.z < ez - FALL_DZ and self._floor_at_path(route, k, s, ez):
                        falls, fall_t = falls + 1, time.time()
                        self.log(f"off the path's floor at ({s.x:.1f},{s.y:.1f}): z {s.z:.1f}, path {ez:.1f}; "
                                 f"replanning from here")
                        np_ = self.path(x, y, z)
                        if np_:
                            s = self.state()
                            route, inside, clamp = self.route(s, np_ + [(x, y, z or 0.0)])
                            k = 1
                            prog_pos = None
                            prog.restart()
                            continue
                # steer at a point LOOK_AHEAD yd further along the path (pure pursuit), not at the next waypoint:
                # the heading swings into each corner and pulls back onto the line after a drift. Indoors a shorter
                # look-ahead and no corner cutting (14:48/14:51 a blend aimed 6-9 yd past the Anvilmar neck
                # (-6085,373) and wedged the bot on the wall at (-6083.7,377.9)): corner_clamps stops it at corners.
                tl = getattr(self, "_tight", None)
                if tl is None or len(tl) != len(route):
                    tl = inside
                la = LOOK_AHEAD_IN if tl[k] or tl[k - 1] else LOOK_AHEAD
                if getattr(self, "path_src", "bot") == "server" and not direct:
                    la = min(la, LOOK_AHEAD_TIGHT)        # an edge-hugging server-mesh trail: follow it closely
                if time.time() < getattr(self, "_tight_until", 0.0):
                    la = min(la, LOOK_AHEAD_TIGHT)        # just stepped back onto the mesh: a narrow ledge, no cutting
                cx, cy, off, corner = look_ahead(route, k, clamp, s.x, s.y, la)
                if off > OFF_ROUTE_LOG and time.time() - getattr(self, "_off_log_t", 0.0) > 5.0:
                    # a run far off its line (inst2 23:34:39-23:35:02: 70 yd east of the Beldin -> ammo route, wedged)
                    self._off_log_t = time.time()
                    self.log(f"off the route by {off:.0f} yd at ({s.x:.0f},{s.y:.0f}): leg {k}/{len(route) - 1} -> "
                             f"({wx:.0f},{wy:.0f}), aim ({cx:.0f},{cy:.0f}), facing {s.facing:.2f} "
                             f"err {wrap(bearing(s.x, s.y, cx, cy) - s.facing):+.2f} moving={getattr(s, 'moving', '?')}")
                # slow for a sharp corner close ahead, or when off the line (turn radius at full speed ~2 yd)
                # slow for a corner or a drift only where cutting it can hit something (tight legs); in the open the
                # 4 yd look-ahead turns any corner up to 90 deg at run speed, and sharper ones raise the heading error
                # past SLOW_ERR, where drive eases the stick by itself
                # the slow stick is a WALK (0.45 deflection): only for a sharp corner close ahead on a tight leg, or a
                # drift off the line indoors. A drift outdoors near rocks is steered back at run speed.
                tight = tl[k] or tl[k - 1] or la < LOOK_AHEAD
                slow = ((tight and corner is not None and k < len(route) - 1)
                        or ((inside[k] or inside[k - 1]) and off > OFF_LINE_SLOW))
                if (slow and not inside[k] and steep_leg(route[k - 1], route[k])
                        and (route[k][2] or 0.0) > (route[k - 1][2] or 0.0)):
                    slow = False                 # a steep climb is walked at full speed: with W let go the
                    #                              character slides back (Spirit Stone trail, inst4 17:33-17:36).
                    #                              Downhill it slows: at 7 yd/s it overshot the trail's 60 deg
                    #                              zigzags by 5 yd (hill3 19:46:26, -6106,508)
                err = wrap(bearing(s.x, s.y, cx, cy) - s.facing)
                fwd = CORNER_FWD if slow else 1.0
                # far off the heading for ORBIT_S while running: circling a point inside our turning circle (2.5 min
                # at full turn, |err| 1.5 rad, no stuck counted - Stonetalon #1481, rxp44 16:13-16:16): turn on the spot
                if abs(err) > TURN_IN_PLACE:
                    if getattr(self, "_big_err_t", None) is None:
                        self._big_err_t = time.time()
                else:
                    self._big_err_t = None
                orbit = self._big_err_t is not None and time.time() - self._big_err_t > ORBIT_S
                if ((tight or orbit or not getattr(s, "moving", True)) and abs(err) > TURN_IN_PLACE
                        and time.time() - getattr(self, "_tip_t", 0.0) > TIP_GAP):
                    self._big_err_t = None
                    # facing far off the line on a tight leg (a hairpin) or from a standstill (just talked to an NPC):
                    # turn on the spot first. Running while turning swung the bot ~1 yd off the grid's line into the
                    # post the route went round (Adlin's stall, rxp1 00:15:18), and from a standing start 2.6 rad off,
                    # a 2 yd arc into a post beside an open leg (Senir Whitebeard, Kharanos, rxp2 01:06:39)
                    self.turn_in_place(err)
                    prog.restart()               # the turn stood still on purpose (up to 2 s): not a stuck
                    continue
                self.drive(err, fwd)
                # swimming: hold ascend so we stay at the surface. The mesh has the lake bed as ground (no water flag
                # on this client's tiles) and the bot walked the bottom of the lake at (-5446,-268), 50 yd under the
                # surface, wedged 15 min against the far bank (inst1 02:45-03:00, window capture: underwater)
                swim = bool(getattr(s, "swimming", False))
                if swim != getattr(self, "_swim_up", False):
                    self._swim_up = swim
                    (self.keys.down if swim else self.keys.up)("space")
                    if swim:
                        self.log(f"swimming at ({s.x:.0f},{s.y:.0f}): holding ascend to keep to the surface")
                # stuck check
                if prog_pos is None or math.hypot(s.x - prog_pos[0], s.y - prog_pos[1]) > 1.5:
                    prog_pos = (s.x, s.y)
                    prog.restart()
                    self._cross_check(s)
                elif prog.passed() and (abs(err) > TURN_IN_PLACE
                                        or math.hypot(s.x - prog_pos[0], s.y - prog_pos[1]) >= SLOW_PROGRESS):
                    # not a wedge: turning on the spot at a sharp corner (the stair foot below Teo, 1.7 rad, stood
                    # still > STUCK_S), or moving slowly (a snare / daze after a fight: 10 yd in 8 s at full
                    # forward in Anvilmar's west hall, logged as two stucks 10 yd apart)
                    prog_pos = (s.x, s.y)
                    prog.restart()
                elif prog.passed() and math.hypot(route[-1][0] - s.x, route[-1][1] - s.y) < tol + (
                        NEAR_DONE_ISLAND if getattr(self, "path_strategy", None) == "goal_island" else NEAR_DONE):
                    # (a goal on a patch the mesh does not join to ours, the client walls it off too: Stonetalon's
                    # giant roots, 9 stucks 12 yd from it, #1413 rxp40 15:00 - blocked this near is there)
                    # blocked just short of the goal (a barrel the collision data lacks beside Booty Bay's Baron,
                    # 3.8 yd off): close enough - talking reaches 5 yd
                    self.log(f"blocked {math.hypot(route[-1][0] - s.x, route[-1][1] - s.y):.1f} yd short of the goal: "
                             f"arrived")
                    arrived = True
                    return "ok"
                elif prog.passed():
                    stucks += 1
                    self.log(f"stuck at ({s.x:.1f},{s.y:.1f}) #{stucks} (steer err {err:+.2f}, fwd {CORNER_FWD if slow else 1.0:.2f}, "
                             f"off {off:.1f} yd, corner {corner if corner is None else round(corner, 1)}, "
                             f"moving={getattr(s, 'moving', '?')})")
                    again = any(math.hypot(s.x - a, s.y - b) <= WEDGE_NEAR for a, b in stuck_pts)
                    stuck_pts.append((s.x, s.y))
                    self._note_stuck(s)
                    self.unstick(stucks)
                    if stucks >= 2:
                        self.escape(wx, wy)
                    prog.restart()
                    # back in a trap of this walk (WEDGE_NEAR): replan round it now; scattered stucks every third
                    if again or stucks % 3 == 0:
                        if again:
                            self.log(f"wedged again within {WEDGE_NEAR:.0f} yd of an earlier stuck: "
                                     f"replanning round ({s.x:.1f},{s.y:.1f})")
                        # a replan on the unchanged mesh finds the same path: cut the spot we keep wedging on out first
                        if not any(math.hypot(s.x - w[0], s.y - w[1]) < 0.5 for w in self._wedges):
                            self._wedges.append((s.x, s.y, s.z, BAD_HOLE_R))
                        # ... and the way we were heading, which the replan from here can still be charged for
                        self._note_wedge_ahead(s, route, k, clamp)
                        # 3+ stucks of this walk within TRAP_R: the 1 yd wedge holes leave the same corridor open
                        # (3+ stuck episodes were 48 of 61 stuck minutes over 4 bots, 03:20-06:20; inst3 31 stucks
                        # in 4 min at (-5535,-97)): cut a TRAP_HOLE_R hole round their centre for a while, now
                        near = [p for p in stuck_pts if math.hypot(p[0] - s.x, p[1] - s.y) <= TRAP_R]
                        # (outdoors only: indoors a 4 yd hole can close the door the route needs)
                        if len(near) >= 3 and not client_indoor(s):
                            cx = sum(p[0] for p in near) / len(near)
                            cy = sum(p[1] for p in near) / len(near)
                            self._trap_holes = [h for h in getattr(self, "_trap_holes", []) if h[4] > time.time()]
                            if not any(math.hypot(h[0] - cx, h[1] - cy) < 2.0 for h in self._trap_holes):
                                self._trap_holes.append((cx, cy, s.z, TRAP_HOLE_R, time.time() + TRAP_HOLE_S))
                                self.log(f"{len(near)} stucks within {TRAP_R:.0f} yd at ({cx:.0f},{cy:.0f}): routes keep "
                                         f"{TRAP_HOLE_R:.0f} yd clear of it for {TRAP_HOLE_S / 60:.0f} min")
                        a0 = getattr(self, "_alt_mesh", None)
                        if again and a0 and getattr(self, "path_src", "bot") == "server":
                            # the server-mesh leg wedged too: one failed leg must not be replayed for ALT_MESH_S s
                            # (inst4 07:39:42-07:40:28: the same data_r3 leg up the Algaz rise 7 times in a row)
                            self._alt_mesh = None
                            self._alt_off = (a0[0], a0[1], time.time() + ALT_OFF_S)
                            self.log(f"the server-mesh leg wedged too at ({s.x:.0f},{s.y:.0f}): Forever mesh only "
                                     f"within {ALT_MESH_R:.0f} yd for {ALT_OFF_S / 60:.0f} min")
                        elif ALT_MESH_ON and again and client_indoor(s) and NB_MESH == navmesh.TCF_MMAP_DIR:
                            off = getattr(self, "_alt_off", None)
                            if not (a0 and time.time() < a0[2] and math.hypot(s.x - a0[0], s.y - a0[1]) < ALT_MESH_R) \
                                    and not (off and time.time() < off[2]
                                             and math.hypot(s.x - off[0], s.y - off[1]) < ALT_MESH_R):
                                self._alt_mesh = (s.x, s.y, time.time() + ALT_MESH_S)
                        if again:
                            self.block_crossing(s, wx, wy, route, k)
                            if not flipped:
                                # the first repeat at one spot: plan from the other floor here (a frozen lake: ice
                                # 403-409 over its bed 357; inst1 03:11-03:15 believed the bed after a restart and only
                                # switched after 3 walks / 27 stucks, when the step was already skipped)
                                flipped = True
                                self.flip_floor(s)
                        np_ = self.path(x, y, z)
                        if np_:
                            s2 = self.state()
                            route, inside, clamp = self.route(s2, np_ + [(x, y, z or 0.0)])
                            k = 1
                    if stucks > 8:
                        # walks that keep ending stuck in the same few yards: a pit the mesh calls open (inst2
                        # 02:33-02:36, 4 walks / 36 stucks inside 10 x 7 yd at (-4998,-330)): break out for real
                        now = time.time()
                        self._traps = [t for t in getattr(self, "_traps", []) if now - t[2] < 300]
                        if sum(1 for t in self._traps if math.hypot(t[0] - s.x, t[1] - s.y) < TRAP_R) >= 1:
                            # every stuck spot of this walk becomes a hole: inst1's block was a line (x -5446,
                            # y -264..-279), one hole at the centre left the route 7 yd off it, along the same wall
                            exp = time.time() + TRAP_HOLE_S
                            self._trap_holes = [h for h in getattr(self, "_trap_holes", []) if h[4] > time.time()]
                            self._trap_holes += [(w[0], w[1], w[2], TRAP_HOLE_R, exp) for w in self._wedges]
                            self.break_out(s)
                        self._traps.append((s.x, s.y, now))
                        return "stuck"
        except _Aborted:
            self.log("walk aborted inside a wedge escape")
            return "abort"
        finally:
            self._abort = None
            self._wedges = []
            if getattr(self, "_swim_up", False):
                self._swim_up = False
                self.keys.up("space")
            if stop or not arrived:
                self.stop()

    def route(self, s, pts):
        """The polyline goto follows: our position, then pts (points < 0.3 yd apart dropped, indoor clusters merged).
        Returns (route, inside flags, clamped corners)."""
        route = [(s.x, s.y, s.z)]
        for q in pts:
            q = (float(q[0]), float(q[1]), float(q[2]))
            if math.dist(q[:2], route[-1][:2]) >= 0.3:
                route.append(q)
            elif len(route) > 1:
                route[-1] = q                        # keep the later point (the goal stays the goal)
        if len(route) == 1:
            route.append(route[0])
        # long legs split into SPLIT_LEG pieces first: the tight flag is per leg, and a 213 yd open leg that only ends
        # near a rock was followed tightly all the way (the pieces of open ground are merged again by smooth_open)
        fine = [route[0]]
        for q in route[1:]:
            a = fine[-1]
            n = int(math.dist(a[:2], q[:2]) // SPLIT_LEG)
            for i in range(1, n + 1):
                f = i / (n + 1)
                fine.append((a[0] + (q[0] - a[0]) * f, a[1] + (q[1] - a[1]) * f,
                             (a[2] + (q[2] - a[2]) * f) if a[2] and q[2] else q[2]))
            fine.append(q)
        route = fine
        # flags per leg: 1 indoors (a building: corner clamps, merging, the fall check), 2 near collision geometry
        # outdoors (the Spirit Stone hilltop's rocks, fences, tents: the short look-ahead only - the grid gave them
        # ~1.5 yd of room and the open look-ahead's corner swing drifted 1.3 yd into a rock, hill3 19:42:24)
        flags = [0] + [1 if indoor(*q) else 0 for q in route[1:]]
        for k in near_collision(route):
            flags[k] |= 2
        route, flags = smooth_open(route, flags)
        route, flags = merge_indoor(route, flags)
        inside = [bool(f & 1) for f in flags]
        self._tight = [bool(f) for f in flags]           # goto: the short look-ahead on these legs
        return route, inside, corner_clamps(route, inside)

    def _note_stuck(self, s):
        """Append this stuck point to nb/bad_spots.json (shared by all instances; never stops the walk)."""
        fl = getattr(s, "flags", None)
        inside = bool(fl & INDOOR_FLAG) if isinstance(fl, int) else indoor(s.x, s.y, s.z)
        try:
            sp, new = self.bad.hit(s.x, s.y, s.z, inside, self._ep or f"{self.inst}-{time.time():.1f}")
            if new:
                self.log(f"wedge spot ({sp['x']:.1f},{sp['y']:.1f}) now known from {BAD_MIN_EPS} walks: "
                         + (f"routes keep clear of it ({'hole' if sp['in'] else 'avoid + detour'})" if LEARNED_SPOTS
                            else "recorded only (learned spots are off)"))
        except Exception as e:
            self.log(f"bad_spots: {e!r}")

    def _abort_check(self, s):
        ab = getattr(self, "_abort", None)
        if ab is not None and ab(s):
            self.stop()
            raise _Aborted()

    def escape(self, tx, ty):
        """Wedged against something the mesh does not know: head off at an angle to the way we
        wanted to go (jumping), until we have moved a few yards, then let the path resume."""
        s0 = self.state()
        # off the mesh (a rock pocket beside a narrow pass): the side-steps below led deeper in (inst3 04:43-04:45,
        # 12 stucks in an unwalkable patch at (-5515,-110) beside a 4 yd ridge at y -98). Back onto the mesh first
        back = self.mesh_back(s0)
        if back is not None:
            run = Deadline(ESCAPE_BACK_S, clock=time)
            while run.running():
                s = self.state()
                self._abort_check(s)
                if math.hypot(back[0] - s.x, back[1] - s.y) < 1.0:
                    break
                self.drive(wrap(bearing(s.x, s.y, back[0], back[1]) - s.facing))
                if int(run.left() * 4) % 3 == 0:
                    self.keys.press("space", 0.05)
            s = self.state()
            if self._floors_safe(s.x, s.y):
                # the walk had cut off a narrow strip (inst3 04:48-04:51: off at x -5535..-5550 every 10-15 s on a
                # 4-6 yd ledge with LOOK_AHEAD 2.5): follow the line closely for a while
                self._tight_until = time.time() + TIGHT_AFTER_BACK_S
                self.log(f"escaped the wedge at ({s0.x:.0f},{s0.y:.0f}): off the mesh, back on at ({s.x:.0f},{s.y:.0f})")
                self.stop()
                return
        for off in (math.pi / 2, -math.pi / 2, 2.4, -2.4, math.pi):
            run = Deadline(ESCAPE_TRY_S, clock=time)
            while run.running():
                s = self.state()
                self._abort_check(s)
                self.drive(wrap(bearing(s.x, s.y, tx, ty) + off - s.facing))
                if int(run.left() * 4) % 3 == 0:
                    self.keys.press("space", 0.05)
            s = self.state()
            if math.hypot(s.x - s0.x, s.y - s0.y) > 3.0:
                self.log(f"escaped the wedge at ({s0.x:.0f},{s0.y:.0f}) (heading offset {off:.1f})")
                break
        self.stop()

    def mesh_back(self, s):
        """The nearest walkable point (a floor within 8 yd of our height) when (s.x, s.y) has no mesh floor, else
        None. Rings of 1.5-9 yd, 16 directions."""
        if self._floors_safe(s.x, s.y):
            return None
        for r in (1.5, 3.0, 4.5, 6.0, 9.0):
            best = None
            for i in range(16):
                a = i * math.pi / 8
                px, py = s.x + r * math.cos(a), s.y + r * math.sin(a)
                fl = self._floors_safe(px, py)
                if fl and (self.z is None or any(abs(f - self.z) <= 8.0 for f in fl)):
                    # prefer the ring point a yard further in too: the mesh's edge itself is the wall
                    px2, py2 = s.x + (r + 1.0) * math.cos(a), s.y + (r + 1.0) * math.sin(a)
                    score = 0 if self._floors_safe(px2, py2) else 1
                    if best is None or score < best[0]:
                        best = (score, (px2, py2) if score == 0 else (px, py))
            if best:
                return best[1]
        return None

    def block_crossing(self, s, wx, wy, route=None, k=1):
        """Wedged twice at one spot heading for (wx, wy): block the polygon step from here into the polygon 3 yd
        that way (session-only, NavMesh.block_climb proven=False). Holes skip polygons over HOLE_POLY_MAX, and inst1's
        wall at x -5446 (02:45-03:00) was the edge of a big valley polygon: routes kept crossing it."""
        try:
            cur, _ = self.nm.nearest_poly(s.x, s.y, s.z)
            d = math.hypot(wx - s.x, wy - s.y) or 1.0
            probes = [(s.x + (wx - s.x) / d * st, s.y + (wy - s.y) / d * st, s.z) for st in (3.0, 5.0)]
            # then along the route ahead (1 yd steps, 10 yd): the straight 3/5 yd probes stayed inside one big
            # polygon below the Algaz bunker rise (332 -> 338 at (-4793,-2631), inst4 07:09-07:11: 9 wedges, no block)
            if route:
                # (route[k] is (wx, wy): these start the same way, and the walk below must not jump back to us)
                probes = []
                prev, walked = (s.x, s.y, s.z), 0.0
                for q in route[max(k, 1):]:
                    seg = math.dist(prev[:2], q[:2])
                    for i in range(1, int(seg) + 1):
                        f = i / seg
                        probes.append((prev[0] + (q[0] - prev[0]) * f, prev[1] + (q[1] - prev[1]) * f,
                                       prev[2] + ((q[2] or prev[2]) - prev[2]) * f))
                    walked += seg
                    prev = (q[0], q[1], q[2] or prev[2])
                    if walked > 10.0:
                        break
            # walk the probes polygon by polygon: a step that is already blocked is passed and the next one ahead is
            # taken. Returning at the first polygon that differed blocked nothing new once (9,21) 10 -> 13 was proven:
            # A* entered the same big rise polygon from (9,21) 8 instead and crossed the same ledge (inst4 07:37-07:58)
            prev = cur
            nodes = []                       # (polygon, x, y) along the probes, consecutive duplicates dropped
            for px, py, pz in probes:
                nb, _ = self.nm.nearest_poly(px, py, pz)
                if nb is not None and (not nodes or nodes[-1][0] != nb):
                    nodes.append((nb, px, py))
            outdoors = not client_indoor(s)
            for i, (nb, px, py) in enumerate(nodes):
                if prev is None or nb == prev:
                    continue
                key = ((tuple(prev[0]), int(prev[1])), (tuple(nb[0]), int(nb[1])))
                was_proven = key in self.nm.blocked_climbs and key not in self.nm.soft_climbs
                if not was_proven:
                    # a step two walks (any bot) were wedged on is proven: the shared file keeps it for every bot and
                    # restart. inst1-4 2026-10-15: 63 stucks at the Algaz bunker hillside (-4795,-2630..-2684), each
                    # bot and each restart relearning the same session-only blocks
                    # outdoors only: indoors a wedge is often clutter by a door or stair the route needs (a proven
                    # block inside the Algaz bunker, (8,20) 4053 -> 4055, could seal the way to Stormpike)
                    if (outdoors and
                            climb_vote(prev, nb, self._ep or f"{self.inst}-{time.time():.1f}") >= CLIMB_PROOF_EPS):
                        if self.nm.block_climb(prev, nb, proven=True):
                            self.log(f"blocked the step at ({s.x:.0f},{s.y:.0f}) towards ({px:.0f},{py:.0f}) "
                                     f"for good ({CLIMB_PROOF_EPS}+ walks wedged on it)")
                        self._climb_into(s, nodes[i:])
                        return
                    elif self.nm.block_climb(prev, nb, proven=False):
                        self.log(f"blocked the step at ({s.x:.0f},{s.y:.0f}) towards ({px:.0f},{py:.0f}) for this session")
                        return
                if was_proven and outdoors:
                    # wedged again on a step we already charge for: it costs more each time (session-only)
                    self.nm.climb_pen[key] = self.nm.climb_pen.get(key, 0.0) + CLIMB_AGAIN_COST
                    self._climb_into(s, nodes[i:])
                    self.log(f"wedged again on a proven step at ({s.x:.0f},{s.y:.0f}): it now costs "
                             f"{self.nm.climb_pen[key]:.0f} yd more")
                    return
                prev = nb                    # this step was blocked already: the next one along the route
        except Exception as e:
            self.log(f"block_crossing failed: {e!r}")

    def _cross_check(self, s) -> None:
        """Read our polygon every CROSS_CHECK_S while making progress; a step we just walked that a block or a cost
        was learned against is lifted (NavMesh.crossed): the character proved it walkable."""
        now = time.time()
        nm = getattr(self, "nm", None)
        if (nm is None or now - getattr(self, "_cross_t", 0.0) < CROSS_CHECK_S
                or not (getattr(nm, "blocked_climbs", None) or getattr(nm, "climb_pen", None))):
            return
        self._cross_t = now
        try:
            cur, _ = self.nm.nearest_poly(s.x, s.y, s.z)
        except Exception as e:
            swallowed("move.cross_check", e, self.log)
            return
        prev, self._cross_poly = getattr(self, "_cross_poly", None), cur
        if cur is None or prev is None or cur == prev:
            return
        if self.nm.crossed(prev, cur):
            self.log(f"walked a learned climb block at ({s.x:.0f},{s.y:.0f}): lifted")

    def _climb_into(self, s, nodes) -> bool:
        """A proven wedge on a CLIMB: every polygon along the probes ahead (`nodes`, from the wedged step on) whose
        centre lies CLIMB_INTO_DZ+ yd above our floor gets every uphill step into it blocked, for good. The ledge lies
        INSIDE such big hillside polygons, and blocking the one step let A* enter the same polygon from another lower
        neighbour and cross the same ledge ((9,21) 10 -> 13 proven, then 8 -> 13, and via 176 when that went: Algaz
        rise, inst4 07:37-07:58). Downhill out of them stays open (bot 4 came DOWN that rise at 07:03:55, never back
        up). True when something new was blocked."""
        if not s.z:
            return False
        n, at = 0, None
        for nb, px, py in nodes:
            try:
                cz = float(self.nm.tiles[nb[0]].center[nb[1]][1])
            except Exception as e:
                swallowed("move.climb_into", e, self.log)
                continue
            if cz < s.z + CLIMB_INTO_DZ:
                continue
            k = self.nm.penalize_uphill_into(nb, CLIMB_SPREAD_COST)
            if k and at is None:
                at = (px, py, cz)
            n += k
        if n:
            self.log(f"charged {n} more step(s) up into the climb ahead (+{CLIMB_SPREAD_COST:.0f} yd) from ({at[0]:.0f},{at[1]:.0f}) (polygon "
                     f"{at[2] - s.z:.1f} yd above us): a wedged climb is entered from every side")
        return n > 0

    def break_out(self, s0):
        """Trapped (a 2nd walk stuck within TRAP_R yd): run 3 s in each of 8 directions, jumping, from the start
        spot, and keep the first that gets TRAP_OUT yd away."""
        self.log(f"trapped at ({s0.x:.0f},{s0.y:.0f}): 2 walks ended stuck here; breaking out in 8 directions; "
                 f"routes keep {TRAP_HOLE_R:.0f} yd clear of it for {TRAP_HOLE_S / 60:.0f} min")
        self._trap_holes = [h for h in getattr(self, "_trap_holes", []) if h[4] > time.time()]
        self._trap_holes.append((s0.x, s0.y, s0.z, TRAP_HOLE_R, time.time() + TRAP_HOLE_S))
        for k in range(8):
            ang = s0.facing + math.pi + k * math.pi / 4 * (1 if k % 2 else -1)
            run = Deadline(BREAK_OUT_S, clock=time)
            while run.running():
                s = self.state()
                self._abort_check(s)
                self.drive(wrap(ang - s.facing))
                if int(run.left() * 3) % 2 == 0:
                    self.keys.press("space", 0.05)
                time.sleep(0.05)
            s = self.state()
            if math.hypot(s.x - s0.x, s.y - s0.y) > TRAP_OUT:
                self.log(f"broke out to ({s.x:.0f},{s.y:.0f}) heading {ang:.1f}")
                break
        self.stop()
        self.flip_floor(self.state())

    def _floor_at_path(self, route, k, s, ez) -> bool:
        """Is there a floor to have fallen FROM: a polygon at the leg's foot point (our projection onto it) within
        FALL_FLOOR_TOL yd of the path height ez and 2+ yd above us? A path height interpolated up a slope, or taken
        from another mesh, has none: 401 of 490 'off the path's floor' replans of 2026-10-14/15 fired where the mesh
        has one polygon only (inst4 07:44-07:58 at (-4800.8,-2633.4): z 329.2 vs path 332.8 on the data_r3 leg, each
        replan back into the Algaz bunker wall). Unknown floors (no tile): treated as a fall, as before."""
        a, b = route[k - 1], route[k]
        t, _ = seg_proj(s.x, s.y, a[0], a[1], b[0], b[1])
        fx, fy = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
        fl = self._floors_safe(fx, fy) + self._floors_safe(s.x, s.y)
        if not fl:
            return True
        return any(abs(f - ez) <= FALL_FLOOR_TOL and f > s.z + 2.0 for f in fl)

    def _floors_safe(self, x, y):
        try:
            return self.floors_at(x, y)
        except Exception as e:
            swallowed("move.floors_safe", e, getattr(self, "log", None))
            return []

    def flip_floor(self, s) -> bool:
        """The strip has no height: floor_z keeps the floor we were on, and at a two-floor spot (a frozen lake: ice
        405-409, its bed 357; inst1 02:45-03:00) a wrong pick plans every route along the other layer into the same
        wall. Switch to the nearest other floor (8+ yd away) at this xy; False when there is none.
        Never inside a building: its floors are storeys, and the floor above is not where we stand (inst4 2026-10-15
        07:15:58 Algaz bunker: 328 -> 337.7, then steered at the upper storey's points into the ground floor's wall)."""
        if client_indoor(s):
            return False
        try:
            alt = [f for f in self.floors_at(s.x, s.y) if self.z is None or abs(f - self.z) > 8.0]
        except Exception as e:
            swallowed("move.flip_floor", e, self.log)
            alt = []
        if not alt:
            return False
        nz = min(alt, key=lambda f: abs(f - (self.z or 0.0)))
        self.log(f"planning from the other floor here: z {self.z if self.z is None else round(self.z, 1)} -> {nz:.1f}")
        self.z = nz
        return True

    def unstick(self, n):
        self.keys.press("space", 0.05)
        side = "a" if n % 2 else "d"
        self.keys.down(side); time.sleep(0.5); self.keys.up(side)
        if n % 3 == 2:
            self.run(False)
            self.keys.down("s"); time.sleep(0.8); self.keys.up("s")
            self.running = False


SEED_LOGS = ["nb.log", "inst2/nb.log", "inst3/nb.log", "inst4/nb.log", "old_run1/nb.log", "old_run2/nb.log",
             "old_run3/nb.log", "old_run4/nb.log", "idle.log", "old_run4/idle.log", "bench.log"]


def seed_bad_spots(paths=None, out: str = BAD_SPOTS, nm=None) -> list:
    """Fill nb/bad_spots.json from the 'stuck at' lines of old logs (merged into what is there). Heights come from
    the bot mesh (lowest floor at the xy), indoor from the vmap building boxes. Returns the spot list."""
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [p if os.path.isabs(p) else os.path.join(here, p) for p in (paths or SEED_LOGS)]
    hits = stuck_hits_from_logs([p for p in paths if os.path.exists(p)])
    if nm is None:
        nm = navmesh.NavMesh(NB_MESH, curmap.MAP)
    zc = {}

    def gz(x, y, hint=None):
        try:
            return nm.ground_z(x, y, hint=hint)
        except Exception as e:
            swallowed("move.seed_ground_z", e)
            return None
    for x, y, _ep, _n in hits:
        zc.setdefault((round(x), round(y)), gz(x, y))
    # the lowest polygon CONTAINING a point can be a roof: (-6083.7,377.9) read 442.4 and (-6064,374) 434.8 over the
    # Anvilmar floor at 392.8-394.1 that their neighbours read. Re-read such points with the lowest neighbour as hint.
    fixed = {}
    for (kx, ky), z in zc.items():
        lows = [z2 for (x2, y2), z2 in zc.items() if z2 is not None and abs(x2 - kx) <= 10 and abs(y2 - ky) <= 10]
        if z is not None and lows and min(lows) < z - 15:    # not 6: Teo's platform is 6.3 yd over Durnan's floor
            z2 = gz(kx, ky, hint=min(lows))
            if z2 is not None:
                z = z2
        fixed[(kx, ky)] = z

    def fn(spots):
        for x, y, ep, _n in hits:
            z = fixed[(round(x), round(y))]
            merge_hit(spots, x, y, z, indoor(x, y, z) if z is not None else False, ep)
    bs = BadSpots(out, out + ".lock" if out != BAD_SPOTS else BAD_LOCK)
    if not bs.update(fn):
        raise RuntimeError("bad_spots.json: could not write")
    return bs.spots


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["seed-bad-spots"]:
        sp = seed_bad_spots(sys.argv[2:] or None)
        act = [q for q in sp if spot_active(q)]
        print(f"{len(sp)} spots, {len(act)} active ({sum(1 for q in act if q['in'])} indoor) -> {BAD_SPOTS}")
    else:
        print("usage: python -m nb.move seed-bad-spots [log ...]")
