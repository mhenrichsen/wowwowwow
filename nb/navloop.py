"""Navigation benchmark: walk a fixed loop of legs in Coldridge Valley with the bot's own Mover and record, per leg,
the result, the time, the stucks and the planned length.

    python -u -m nb.navloop --inst N [--loops K] [--offset M] [--tag NAME]

Results: nb/navloop.jsonl (one line per leg, all instances). The legs run between quest NPCs of the 1-5 guide -
Anvilmar's hall (Teo up the stairs, Grund, Grundel), the camps outside, the Spirit Stone hill - indoors and out, both
ways. A leg ends on arrival, on the walk's own timeout / no path, or when a fight starts (the leg is then left out:
it says nothing about navigation); the bot's own fight / loot / rest / death code then handles it (nb.bot.Bot) and
the next leg starts from where it ended. STOP file (inst.path("STOP")):
the benchmark ends after the current leg.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

from nb import bot as B
from nb import inst

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "navloop.jsonl")
STOP = inst.path("STOP")

# (name, x, y, z): DB spawn positions of guide NPCs (tcf_world), the points the bot really walks to
POINTS = [
    ("Teo", -6050.3, 383.9, 399.0),          # Anvilmar, up the stairs
    ("Grund", -6093.8, 390.0, 395.6),        # Anvilmar hall
    ("Sten", -6214.9, 328.2, 383.7),         # outside, first camp
    ("Grundel", -6104.5, 384.0, 395.6),      # Anvilmar, vendor
    ("Nori", -6371.1, 571.8, 385.8),         # west camp
    ("Teo", -6050.3, 383.9, 399.0),
    ("Thalos", -6235.8, 152.9, None),        # south road
    ("Grund", -6093.8, 390.0, 395.6),
    ("Manifestation", -5908.1, 576.5, 500.6),  # Spirit Stone hill top
    ("Teo", -6050.3, 383.9, 399.0),
    ("Hands", -6248.8, 135.0, 431.7),        # up the tunnel path
    ("Grundel", -6104.5, 384.0, 395.6),
]


CONSOLE = "D:/tcforever/console_cmd.py"
MARIADB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "wow-forever-server", "mariadb", "bin", "mariadb.exe")
TELE_UP = 1.0      # yd above the spawn height the start point was taken from (a DB spawn stands on its floor)


LAND_R = 2.9          # yd: a teleport to an NPC lands within this of its spawn (inside the NPC leg's arrival radius)


def open_landing(x, y, z):
    """Where a teleport to the NPC at (x, y, z) puts us: the grid node within LAND_R with the most clearance, not
    the spawn itself - a vendor stands behind its counter (#752, Thurman Mullby's spawn has 0.4 yd of room; the
    walk out of it wedged, rxp28 08:37)."""
    import gridpath as gp
    try:
        G = gp.Grid([(x, y, z), (x + 0.1, y, z)], band=LAND_R + 2)
    except Exception:                                 # noqa: BLE001 - no grid here: the spawn as before
        return x, y, z
    best, bk = (x, y, z), None
    for v in range(len(G.nz)):
        px, py, pz = G.xyz(v)
        d = math.hypot(px - x, py - y)
        if d <= LAND_R and abs(pz - z) <= 0.6:          # the spawn's own floor, not the counter top
            k = (round(float(G.clr[v]), 1), -d)
            if bk is None or k > bk:
                best, bk = (float(px), float(py), float(pz)), k
    return best


LIFT_NEAR = 120.0     # --rxp: yd from a lift shaft that start and goal must both lie within (one at each level)


def lift_between(map_id, a, b):
    """The name of a lift (polestar_data) whose shaft both points lie near, one by its top and one by its bottom
    (a z within a third of the lift's rise of each end); None otherwise or when either height is unknown."""
    if a[2] is None or b[2] is None:
        return None
    try:
        import polestar_data
        for name, c, wx, wy, top, bot, _ in polestar_data.lift_stops():
            if c != map_id:
                continue
            if max(math.hypot(a[0] - wx, a[1] - wy), math.hypot(b[0] - wx, b[1] - wy)) > LIFT_NEAR:
                continue
            third = (top - bot) / 3
            ends = sorted((a[2], b[2]))
            if ends[0] < bot + third and ends[1] > top - third:
                return name
    except Exception:                                 # noqa: BLE001
        return None
    return None


def teleport(bot, log, i, name, x, y, z, mp=0):
    """Server-side teleport of this client's character to (x, y, z + TELE_UP) through a game_tele row and the
    worldserver console; returns once the strip shows us there and standing."""
    import subprocess
    tn = f"nbt{i:02d}"
    sql = (f"delete from game_tele where name='{tn}'; insert into game_tele "
           f"(position_x,position_y,position_z,orientation,map,name) values ({x},{y},{z + TELE_UP},0,{mp},'{tn}');")
    subprocess.run([MARIADB, "-u", "root", "-pvmangos", "-h", "127.0.0.1", "-P", "3310", "tcf_world", "-e", sql],
                   check=True, capture_output=True)
    who = bot_name()
    subprocess.run([sys.executable, CONSOLE, "reload game_tele"], check=True, capture_output=True)
    subprocess.run([sys.executable, CONSOLE, f"tele name {who} {tn}"], check=True, capture_output=True)
    t0 = time.time()
    while time.time() - t0 < 40:
        time.sleep(1.0)
        try:
            s = bot.mv.state()
        except Exception:                             # noqa: BLE001 - loading screen
            continue
        if math.hypot(s.x - x, s.y - y) < 6:
            time.sleep(2.0)                           # the client settles (falls the TELE_UP, loads models)
            # the strip has no height: the floor we were put on is known here (stacked floors: a canal under a street)
            bot.mv.z = z
            bot.mv.state()
            log(f"teleported to {name} ({x:.1f},{y:.1f},{z:.1f})")
            return
    raise RuntimeError(f"teleport to {name} not seen")


RXP_TALK = {"QA", "QT", "V", "CI", "S", "P", "F", "LE", "H"}   # step actions done at the step's .target NPC
PEACE_HP = 35.0       # --rxp: hp % below which the walk stops to fight what is on it (self-defence only)
PEACE_COMBAT_WAIT = 12.0   # --rxp: s settle waits for combat to drop (after an escape) when low, to rest
PEACE_REST = 60.0     # --rxp: hp % below which it heals / rests between legs, out of combat
PEACE_ZONE_R = 6.0    # --rxp: yd kept from each hostile spawn (contact range + wander)
CIRCLE_IN = 2.0       # --rxp: yd inside a guide waypoint's radius the walk aims for
CIRCLE_GRADE = 0.9    # --rxp: ... on ground no steeper than this within 1.5 yd (no sliding off)
TELE_SNAP = 40.0      # --rxp: a teleport lands on the nearest DB spawn within this many yd of its point
PEACE_ESCAPES = 2      # --rxp: teleport-escapes on one leg before it is given up (skipped)
BOAT_D = 500.0        # --rxp: yd: no walking path to a goal this far is a crossing (a boat on one map): teleport
TELE_FAR = 3000.0     # --rxp: yd: a longer leg is a teleport (the trams: ~4 km; the longest walks 2.7 km)
LEVEL_JUMP = 20.0     # --rxp: a walk goal this many yd above / below us ...
LEVEL_NEAR = 10.0     # ... takes a model floor within this many yd of our height instead
LEVEL_NEAR_D = 150.0  # yd: the level rule applies this near the goal only (our height says nothing from afar)
RXP_JUMP = {"F", "H"}                                           # flight / hearth: the next point is a teleport


def rxp_route(first="1-5 Coldridge Valley", max_level=30):
    """The RestedXP route from guide `first` through the guides that start below max_level, as
    [(label, map, x, y, npc name or "", teleport first)]: every waypoint of every step in order, a step with a .target
    and a talk action as its NPC (at the step's first waypoint). Flights and hearths, and a map change (boat, tram),
    become teleports to the next point."""
    import re
    import rxp
    import worldmap
    rxp.GUIDE_DIR = rxp.FOREVER_DIR
    from nb import route
    cat = rxp.catalog(route.who())
    out, name, jump, last_map = [], first, False, None
    while name in cat:
        m = re.match(r"(\d+)-", name)
        if m and int(m.group(1)) >= max_level:
            break
        for st in cat[name].steps:
            tags = {t for t, _ in st.actions}
            pts = []
            for zn, ga, gb, rad in st.gotos:
                zn = str(zn)
                if "/" in zn:                       # "1426/0": world coordinates (y, x) on map 0 / 1
                    pts.append((int(zn.split("/")[1]), gb, ga, rad))
                else:
                    w = worldmap.to_world(ga, gb, zn)
                    if w is not None:
                        pts.append(w + (rad,))
            if not pts:
                if tags & RXP_JUMP:
                    jump = True
                continue
            npc = st.targets[0].split("::")[0] if st.targets and tags & RXP_TALK else ""
            for k, (mp, x, y, rad) in enumerate(pts[:1] if npc else pts):
                lbl = f"{name[:12]} L{st.line_no}" + (f" {npc}" if npc and k == 0 else "")
                tele = jump or (last_map is not None and mp != last_map)
                if out and not tele and math.hypot(out[-1][2] - x, out[-1][3] - y) < 3 and not npc:
                    continue
                out.append((lbl, mp, float(x), float(y), npc if k == 0 else "", tele, rad))
                jump, last_map = False, mp
            if tags & RXP_JUMP:
                jump = True
        name = cat[name].next_guide
    return out


def _db_rows(sql):
    import subprocess
    out = subprocess.run([MARIADB, "-u", "root", "-pvmangos", "-h", "127.0.0.1", "-P", "3310", "-N", "tcf_world",
                          "-e", sql], capture_output=True, text=True).stdout
    return [tuple(float(v) for v in l.split("	")) for l in out.strip().splitlines() if l]


def rxp_goal(bot, x, y, npc, mp=0, tele=False):
    """_rxp_goal's point, lifted to the water's surface when it lies under water (a sunken chest, the sea bed): we
    swim there, the arrival test is flat, and the mesh does not join the surface to the bed (#991, Darkshore's
    wreck at z -21.3: the planner's pick was the wreck's deck at -3.1, by a 600 yd land detour that wedged 5 times,
    rxp32 10:11; a teleport onto the bed left us walking it under water, 9 stucks, rxp33 10:14)."""
    gx, gy, gz = _rxp_goal(bot, x, y, npc, mp, tele)
    try:
        wet = sorted(set(bot.mv.floors_at(gx, gy)) - set(bot.mv.floors_at(gx, gy, dry=True)))
    except Exception:                                 # noqa: BLE001
        wet = []
    if wet and wet[-1] > gz + 1.0:
        return gx, gy, wet[-1]
    return gx, gy, gz


def _rxp_goal(bot, x, y, npc, mp=0, tele=False):
    """(x, y, z) to walk to for a route point: an NPC's DB spawn nearest RXP's waypoint (within NPC_SNAP yd), else the
    bot's own RXP point height (rxp_follow._rxp_point: a DB spawn's beside it, else the lowest floor on our island).
    Off map 0 (the bot's DB index and quest DB are map 0 only): tcf_world spawns of that map, else the lowest mesh
    floor at the point (navmesh.ground_z). tele (a hearth / flight / map change lands here): the same spawn / lowest
    floor rule on map 0 too - our island's floor is wrong across a teleport (#426, the Ironforge hearth: the
    mountain top at z 792 over the city; the character fell to its death, rxp19 04:41)."""
    from nb import rxp_follow as RF
    s = bot.mv.state()
    if npc and mp == 0:
        cands = [p for src in bot._rxp_npc_cands(npc) for p in src]
        near = [p for p in cands if math.hypot(p[0] - x, p[1] - y) <= RF.NPC_SNAP]
        if near:
            p = min(near, key=lambda p: math.hypot(p[0] - x, p[1] - y))
            return float(p[0]), float(p[1]), float(p[2])
    if tele and not npc:
        # land on the nearest spawn: a creature or object stands on a real floor (the Ironforge hearth's point lies
        # over the flight master, whose floor at z 502 the mesh does not hold there - only the mountain at 792)
        rows = _db_rows(f"SELECT position_x, position_y, position_z FROM creature WHERE map = {mp} AND position_x "
                        f"BETWEEN {x - TELE_SNAP} AND {x + TELE_SNAP} AND position_y BETWEEN {y - TELE_SNAP} AND "
                        f"{y + TELE_SNAP} UNION SELECT position_x, position_y, position_z FROM gameobject WHERE map = "
                        f"{mp} AND position_x BETWEEN {x - TELE_SNAP} AND {x + TELE_SNAP} AND position_y BETWEEN "
                        f"{y - TELE_SNAP} AND {y + TELE_SNAP}")
        if rows:
            return tuple(float(v) for v in min(rows, key=lambda p: math.hypot(p[0] - x, p[1] - y)))
    if mp != 0 or tele:
        if npc:
            q = npc.replace("\\", "\\\\").replace("'", "\\'")
            rows = _db_rows(f"SELECT c.position_x, c.position_y, c.position_z FROM creature c JOIN creature_template t "
                            f"ON t.entry = c.id WHERE c.map = {mp} AND t.name = '{q}'")
            near = [p for p in rows if math.hypot(p[0] - x, p[1] - y) <= RF.NPC_SNAP]
            if near:
                return min(near, key=lambda p: math.hypot(p[0] - x, p[1] - y))
        rows = _db_rows(f"SELECT position_x, position_y, position_z FROM creature WHERE map = {mp} AND position_x "
                        f"BETWEEN {x - 3} AND {x + 3} AND position_y BETWEEN {y - 3} AND {y + 3} UNION "
                        f"SELECT position_x, position_y, position_z FROM gameobject WHERE map = {mp} AND position_x "
                        f"BETWEEN {x - 3} AND {x + 3} AND position_y BETWEEN {y - 3} AND {y + 3}")
        if rows:
            return x, y, min(rows, key=lambda p: math.hypot(p[0] - x, p[1] - y))[2]
        floors = list(bot.mv.floors_at(x, y))
        gz = bot.mv.nm.ground_z(x, y)
        cands = sorted(set(floors + ([gz] if gz is not None else [])))
        dry = list(bot.mv.floors_at(x, y, dry=True))      # a dock over water: land on the dock (#755)
        if dry:
            cands = sorted(dry)
        return x, y, (cands[0] if cands else 0.0)
    gx, gy, gz = (float(v) for v in bot._rxp_point(x, y, s))
    # the mesh can lack a city's inside floors where the mountain lies over them (Ironforge: the point by the tram
    # read z 750, the mountain; the walk planned 1610 yd up it and wedged, rxp20 #439): a model floor at the point
    # near our own height wins over a far floor
    # ... only near the goal, and never over a DB spawn's height: from Redridge (z 78) the rule swapped the Trade
    # District street (a vendor at 109.7, 1.5 yd off the point) for the canal under it at 77.7, and the walk wedged
    # under the city 9 times (#1298, rxp39 13:36)
    if (s.z is not None and abs(gz - s.z) > LEVEL_JUMP and math.hypot(gx - s.x, gy - s.y) < LEVEL_NEAR_D
            and bot._db_z(x, y) is None):
        # a mesh floor nearer our height first (walkable for sure: a model surface can be a roof - #553's 339.7 over
        # the 335.1 floor had no polygon, "no path"); a model floor only when the mesh has none near us
        mesh = [f for f in bot.mv.floors_at(x, y, dry=True) if abs(f - s.z) < abs(gz - s.z)]
        if mesh:
            return x, y, min(mesh, key=lambda f: abs(f - s.z))
        near = [f for f in model_floors(x, y) if abs(f - s.z) <= LEVEL_NEAR]
        if near:
            return x, y, min(near, key=lambda f: abs(f - s.z))
    return gx, gy, gz


def circle_aim(mv, s, cx, cy, z, tol):
    """A spot inside a guide waypoint's radius to walk to instead of its centre: on the near side, on a mesh floor
    near the goal's height or ours (#526: the point lies at a cliff's foot 65 yd under the ridge we walk), on gentle
    ground (#648: the near-side spot was on the cliff-top slope; standing there the character slid over the edge and
    lost 90% hp in the fall, rxp25 07:36). Tries depths and angles off the line to us; None: walk to the centre."""
    import terrain
    d = math.hypot(s.x - cx, s.y - cy)
    base = math.atan2(s.y - cy, s.x - cx)
    for depth in (tol - CIRCLE_IN, tol * 0.6, tol * 0.3):
        for off in (0.0, 0.35, -0.35, 0.7, -0.7):
            if depth <= 0 or depth >= d:
                continue
            px, py = cx + depth * math.cos(base + off), cy + depth * math.sin(base + off)
            ring = [(px + 1.5 * math.cos(a * math.pi / 4), py + 1.5 * math.sin(a * math.pi / 4)) for a in range(8)]
            g = max(float(terrain.grade(qx, qy)) for qx, qy in [(px, py)] + ring)
            if g != g or g > CIRCLE_GRADE:                   # nan: a terrain hole (a building's floor: fine) ...
                if g == g:
                    continue
            for zh in ([z] if z is not None else []) + ([s.z] if s.z else []):
                hit = mv.nm.nearest_poly(px, py, zh, 8.0)
                if hit[0] is not None:
                    return px, py, float(hit[1][1])
    return None


def model_floors(x, y, top=2000.0, bottom=-1000.0):
    """Heights of every model (WMO / M2) surface at (x, y), highest first."""
    import vgeom
    P = vgeom.Patch(x - 1, y - 1, x + 1, y + 1, bottom, top)
    out, zt = [], top
    while True:
        f = P.floor(x, y, zt, bottom)
        if f is None:
            return out
        out.append(f)
        zt = f - 0.5


def bot_name():
    """The online character's name (this harness drives one client)."""
    import subprocess
    out = subprocess.run([MARIADB, "-u", "root", "-pvmangos", "-h", "127.0.0.1", "-P", "3310", "-N", "tcf_characters",
                          "-e", "select name from characters where online=1"], capture_output=True, text=True).stdout
    names = out.split()
    if len(names) != 1:
        raise RuntimeError(f"want one online character, got {names}")
    return names[0]


def main():
    args = sys.argv[1:]
    loops = int(args[args.index("--loops") + 1]) if "--loops" in args else 1
    off = int(args[args.index("--offset") + 1]) if "--offset" in args else 0
    tag = args[args.index("--tag") + 1] if "--tag" in args else "run"
    # --legs Grund,Manifestation: walk only these points (in this order, repeated --loops times)
    only = args[args.index("--legs") + 1].split(",") if "--legs" in args else None
    lines: list[str] = []

    def log(msg):
        lines.append(msg)
        print(time.strftime("%H:%M:%S"), f"[{inst.INST}]", msg, flush=True)

    bot = B.Bot()
    mv = bot.mv
    mv.log = lambda m: (lines.append(m), B.log(m))
    mv.stop_file = STOP
    # steer with the virtual pad like the real bot (Bot.run claims it in its loop; this harness never runs that loop)
    try:
        bot.claim_pad()
    except Exception as e:                            # noqa: BLE001 - keys still work
        log(f"pad claim failed: {e!r}; keyboard steering")
    log(f"steering: {'pad' if mv.pad and mv.pad_ok else 'keys'}")

    def settle():
        """Fights, loot, rest and death through the bot's own code, until the character stands idle and alive."""
        t = time.time()
        while time.time() - t < 600 and not os.path.exists(STOP):
            s = bot.s()
            if s.dead or s.ghost:
                bot.on_death(); continue
            if peace:
                # no fights on the route, but stay alive: a level 60 in starter gear loses ~5 hp/s to each trogg that
                # tags it, and the Gol'Bolar Quarry walk drained it to death (rxp9 02:50:43). Heal / rest when low;
                # defend only below PEACE_HP with something on us
                hit = s.combat or bot.attackers(s)
                if hit:
                    # never fight: the walk's escape teleport handles it. Just after that teleport combat still reads
                    # on for a few seconds: low, wait it out and rest - else the walk went on at 27% and the next
                    # tag killed us (Duskwood #1634/#1635, rxp46 18:00, rxp47 18:06)
                    if s.hp_pct < PEACE_REST and time.time() - t < PEACE_COMBAT_WAIT:
                        time.sleep(0.5); continue
                    return
                elif s.hp_pct < PEACE_REST:
                    bot.rest(); continue
                else:
                    return
            if s.has_target and s.t_dead and not bot.attackers(s):
                bot.loot(); continue
            if s.combat or bot.attackers(s):
                if bot.lingering_combat(s):
                    bot.rest(True); continue
                if bot.pick_target() or bot.find_attacker(time.time() - 3):
                    if bot.fight() == "killed":
                        bot.loot()
                else:
                    time.sleep(0.3)
                continue
            if s.hp_pct < 60 or s.mana_pct < 30:
                bot.rest(); continue
            return
    pts = POINTS[off % len(POINTS):] + POINTS[:off % len(POINTS)]
    if only:
        byname = {p[0]: p for p in POINTS}
        pts = [byname[n] for n in only]
    seq = [p for _ in range(loops) for p in pts]
    # --places FILE (nb/tight_places.json): per place a teleport to its start, a leg to the goal and one back
    tele = {}
    if "--places" in args:
        places = json.load(open(args[args.index("--places") + 1], encoding="utf-8"))
        if "--only" in args:
            want = args[args.index("--only") + 1].split(",")
            places = [pl for pl in places if any(w.lower() in pl["name"].lower() for w in want)]
        first = int(args[args.index("--from") + 1]) if "--from" in args else 0
        seq = []
        for _ in range(loops):
            for i, pl in enumerate(places[first:], first):
                sx, sy, sz = pl["start"]
                gx, gy, gz = pl["goal"]
                tele[len(seq)] = (i, pl["name"], sx, sy, sz)
                seq.append((f"{pl['name']}>in", gx, gy, gz))
                seq.append((f"{pl['name']}>out", sx, sy, sz))
    # --rxp: the RestedXP route 1 -> --to-level (default 30), every waypoint and NPC in order (rxp_route); no fights
    # (a level 60 walks grey zones); flights, hearths and map changes are teleports. --from N: start at point N
    peace = "--rxp" in args
    lazy = {}
    if peace:
        top = int(args[args.index("--to-level") + 1]) if "--to-level" in args else 30
        first = int(args[args.index("--from") + 1]) if "--from" in args else 0
        route = rxp_route(max_level=top)
        log(f"rxp route: {len(route)} points to level {top}, from point {first}")
        seq = []
        for i, (lbl, mp, x, y, npc, tp, rad) in enumerate(route[first:], first):
            lazy[len(seq)] = (i, mp, npc, tp, rad)
            seq.append((f"#{i} {lbl}", x, y, None))
    # per leg into nb/navtrack.jsonl: every state the mover reads (time, x, y, z, facing) and each route it follows
    track, routes, hp_seen = [], [], []
    _state, _route = mv.state, mv.route

    def state_rec(*a, **k):
        t_ = time.time()
        s_ = _state(*a, **k)
        dt_ = time.time() - t_
        gap_ = t_ - track[-1][0] if track else 0.0
        if dt_ > 0.3 or gap_ > 0.4:
            # a blind stretch: at 7 yd/s a 0.7 s pause walks 5 yd unsteered
            log(f"slow tick: state read {dt_:.2f} s, {gap_:.2f} s since the last read")
        hp_ = getattr(s_, "hp_pct", None)
        if hp_ is not None and hp_seen and hp_ < hp_seen[-1] - 10:
            log(f"hp drop {hp_seen[-1]:.0f}% -> {hp_:.0f}% at ({s_.x:.1f},{s_.y:.1f}) combat={bool(getattr(s_, 'combat', 0))} "
                f"plates={[(getattr(p_, 'npc', 0), getattr(p_, 'lvl', 0)) for p_ in (getattr(s_, 'plates', None) or [])][:6]}")
        if hp_ is not None:
            hp_seen[:] = [hp_]
        track.append((round(time.time(), 2), round(s_.x, 2), round(s_.y, 2), round(s_.z or 0.0, 2),
                      round(getattr(s_, "facing", 0.0) or 0.0, 3)))
        return s_

    def route_rec(*a, **k):
        r_ = _route(*a, **k)
        routes.append((round(time.time(), 2), [[round(float(v), 2) for v in q] for q in r_[0]]))
        return r_
    mv.state, mv.route = state_rec, route_rec
    # a blind stretch's cause: when no state was read for STALL_S, the main thread's stack (once per stall)
    import threading
    import traceback
    main_id = threading.get_ident()

    def stall_watch():
        seen = 0.0
        while True:
            time.sleep(0.1)
            if not track or track[-1][0] == seen or time.time() - track[-1][0] < 0.45:
                continue
            seen = track[-1][0]
            fr = sys._current_frames().get(main_id)
            if fr is not None:
                st = traceback.extract_stack(fr)[-6:]
                log("stall stack: " + " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
                                                  for f in reversed(st)))
    threading.Thread(target=stall_watch, daemon=True).start()
    def peace_zones(mp, ax, ay, bx, by):
        """Peace mode: every hostile spawn near the leg as a PEACE_ZONE_R yd zone. Grey mobs still hit within
        contact range at Rate.Creature.Aggro 0 (rxp11: Rockjaw troggs killed the starter-geared 60 in Gol'Bolar
        Quarry), so the route keeps out of their reach."""
        import dangers, factions
        lo_x, hi_x = min(ax, bx) - 60, max(ax, bx) + 60
        lo_y, hi_y = min(ay, by) - 60, max(ay, by) + 60
        return [(r[1], r[2], r[3], PEACE_ZONE_R) for r in dangers._load()
                if r[0] == mp and lo_x < r[1] < hi_x and lo_y < r[2] < hi_y
                and factions.is_hostile(B.DWARF_FACTION, r[9])]
    def steady_state():
        """mv.state() through a moment without the strip (a loading hitch, a tunnel's dark frame: rxp26 07:48 crashed
        the whole walk on one failed read after a leg)."""
        for _ in range(20):
            try:
                return mv.state()
            except RuntimeError:
                time.sleep(0.5)
        return mv.state()

    def leg_tol(li):
        """Arrival radius: 3 yd (an NPC: talk range), a guide waypoint its own radius (RXP's goto completes inside it:
        the Frostmane Hold point L1284 sits up the rocks of the cave mouth, r 20). No cap: the Gol'Bolar point #350
        (r 30) lies a ramp turn down among the troggs, 23 yd from where #349 ended; RXP counts it done there."""
        e = lazy.get(li)
        if not e or e[2] or not e[4]:
            return 3.0
        return max(3.0, float(e[4]))
    for li, (name, x, y, z) in enumerate(seq):
        if os.path.exists(STOP):
            break
        if li in lazy:
            i_, mp_, npc_, tp_, rad_ = lazy[li]
            if mp_ != mv.nm.map_id:
                mv.set_map(mp_)                       # the other continent: its mesh before planning anything there
            s0 = steady_state()
            # a leg past TELE_FAR is travel the guide does by tram / boat / flight (Ironforge -> Stormwind, #440:
            # the Deeprun Tram stays on map 0, so nothing marked it, and the walk set off on 4 km of road)
            d0 = math.hypot(x - s0.x, y - s0.y)
            jump = tp_ or li == 0 and d0 > 200 or d0 > TELE_FAR
            x, y, z = rxp_goal(bot, x, y, npc_, mp_, tele=jump)
            seq[li] = (name, x, y, z)
            if jump:
                lx, ly, lz = open_landing(x, y, z) if npc_ and mp_ == 0 else (x, y, z)
                tele[li] = (i_ % 100, name, lx, ly, lz, mp_)
        if li in tele:
            mv.stop()
            try:
                teleport(bot, log, *tele[li])
            except Exception as e:                    # noqa: BLE001
                log(f"teleport failed: {e!r}")
                continue
        try:
            settle()
        except Exception as e:                        # noqa: BLE001 - a strip glitch must not end the benchmark
            log(f"settle failed: {e!r}")
            time.sleep(2); continue
        s = steady_state()
        frm = (round(s.x, 1), round(s.y, 1), round(s.z or 0, 1))
        n0 = len(lines)
        del track[:], routes[:]
        t0 = time.time()
        fought = []
        fights, fight_s = 0, 0.0
        if peace:
            mv.zones = peace_zones(mv.nm.map_id, s.x, s.y, x, y)
        tol = leg_tol(li)
        # peace mode: a guide waypoint is done the moment we are inside its radius (RXP's own test), wherever the
        # route is: the mover only tests the arrival radius on the route's last stretch
        arrived = []
        cx, cy = x, y

        def inside(st):
            return math.hypot(cx - st.x, cy - st.y) < tol and not arrived.append(1)
        # ... and the walk aims at the circle's near side, not its centre: the centre can sit in a spot the client
        # will not let us reach (#415's lies in the Algaz Station tower base, r 12; the walk to it wedged 9 times in
        # four runs while the circle's north side was 11 yd of open road away)
        d_ = math.hypot(s.x - cx, s.y - cy)
        if peace and tol > 3.0 and d_ > tol:
            aim = circle_aim(mv, s, cx, cy, z, tol)
            if aim:
                x, y, z = aim
        swim_try = False                              # (goto's timeout: 240 s, or ~2.2 yd/s of the straight line for
        #                                               a long leg - 2262 yd of road timed out at 514 s, #1465 rxp43)
        z0 = z                                        # the goal's own height, before a no-path retry drops it
        while True:
            if peace and inside(s):
                res = "ok"
                break
            try:
                res = mv.goto(x, y, z, tol=tol, timeout=max(240.0, 0.45 * math.hypot(x - s.x, y - s.y) + 60.0),
                              abort=(lambda st: inside(st) or st.hp_pct < PEACE_HP and not fought.append(1)) if peace
                              else (lambda st: bool(st.combat) and not fought.append(1)))
                if arrived:
                    res = "ok"
                    break
                lift = lift_between(mv.nm.map_id, frm, (x, y, z)) if res == "nopath" and peace else None
                if lift and not os.path.exists(STOP):
                    # a lift ride (polestar TravelData timetables): the mesh has no floor through the shaft, and the
                    # bot cannot see the car - the ride is a jump like a boat's
                    log(f"no walk to {name}: the {lift} lift between us, teleporting")
                    try:
                        teleport(bot, log, 97, name, x, y, z if z is not None else
                                 (list(mv.floors_at(x, y)) or [mv.nm.ground_z(x, y) or 0.0])[0], mv.nm.map_id)
                        res = "ok"
                    except Exception as e:            # noqa: BLE001
                        log(f"teleport failed: {e!r}")
                    break
                if res == "nopath" and peace and math.hypot(x - frm[0], y - frm[1]) > BOAT_D and not os.path.exists(STOP):
                    # no walk over this far: a boat on the same map (Auberdine -> Rut'theran, 2 km of sea: two 40 s
                    # plans and a skipped leg, #1460 rxp42 15:45) - the crossing is a jump like a map change
                    log(f"no walk to {name} ({math.hypot(x - frm[0], y - frm[1]):.0f} yd): a crossing, teleporting")
                    try:
                        teleport(bot, log, 97, name, x, y, z if z is not None else
                                 (list(mv.floors_at(x, y)) or [mv.nm.ground_z(x, y) or 0.0])[0], mv.nm.map_id)
                        res = "ok"
                    except Exception as e:            # noqa: BLE001
                        log(f"teleport failed: {e!r}")
                    break
                if res == "nopath" and peace and z is not None and not os.path.exists(STOP):
                    # our goal height has no floor the planner can reach: let it pick the floor at the point
                    log(f"no path to {name} at z {z:.1f}: trying the floor the planner picks")
                    z, swim_try = None, True
                    continue
                if res == "nopath" and peace and swim_try and not os.path.exists(STOP):
                    # a goal on the sea bed (#826, Darkshore: water 0.2 over the bed at -23.3) - the mesh does not
                    # join the surface to the bed: aim at the water's surface over it, we swim there anyway and the
                    # arrival test is flat
                    swim_try = False
                    wet = sorted(set(mv.floors_at(x, y)) - set(mv.floors_at(x, y, dry=True)))
                    if wet:
                        z = wet[-1]
                        log(f"no path to {name}: aiming at the water's surface over it (z {z:.1f})")
                        continue
            except RuntimeError as e:                 # strip not visible: leave the leg out (a fight's result)
                log(f"goto failed: {e!r}")
                res, fought = "error", [1]
                break
            if peace and res == "abort" and not fought and not os.path.exists(STOP):
                # the planner gives up when combat starts (a mob pulled us while it planned, #1276 rxp38 13:11):
                # escape like a hit, not a skipped leg
                try:
                    if mv.state().combat:
                        fought.append(1)
                except Exception:                     # noqa: BLE001
                    pass
            if not fought or res == "ok" or fights >= 3 or os.path.exists(STOP):
                break
            # a mob pulled us on the way: fight it out, then walk on to the same point (the leg's walking time
            # leaves the fight out)
            fights += 1
            mv.stop()
            tf = time.time()
            if peace:
                # no fights in peace mode: the starter-geared 60 lost 3 -> 5 Loch Modan troggs from 34% (rxp24
                # 06:26, #555). A teleport back to the leg's start drops combat; rest there, then walk it again
                if fights > PEACE_ESCAPES:
                    log(f"hit again on the leg to {name}: giving it up")
                    res = "hit"
                    break
                log(f"hit on the leg to {name} (hp {mv.state().hp_pct:.0f}%): teleporting back to its start")
                try:
                    teleport(bot, log, 98, "escape", frm[0], frm[1], frm[2], mv.nm.map_id)
                except Exception as e:                # noqa: BLE001
                    log(f"teleport failed: {e!r}")
            try:
                settle()
            except Exception as e:                    # noqa: BLE001
                log(f"settle failed: {e!r}")
            fight_s += time.time() - tf
            del fought[:]
            log(f"fight {fights} on the leg to {name} over ({fight_s:.0f} s): walking on")
        dt = time.time() - t0 - fight_s
        mv.stop()
        s = steady_state()
        leg = lines[n0:]
        plan = None
        for m in leg:
            if m.startswith("route to") and " yd," in m:
                try:
                    plan = float(m.split(" pts, ")[1].split(" yd")[0])
                except (IndexError, ValueError):
                    pass
                break
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tag": tag, "inst": inst.INST, "to": name,
               "from": frm, "goal": (x, y, z), "res": res, "fight": bool(fought), "s": round(dt, 1),
               "straight": round(math.hypot(x - frm[0], y - frm[1]), 1), "plan": plan,
               "end_d": round(math.hypot(x - s.x, y - s.y), 1),
               "stucks": sum(1 for m in leg if m.startswith("stuck at")),
               "stuck_at": [m.split("stuck at ")[1].split(" ")[0] for m in leg if m.startswith("stuck at")],
               "replans": sum(1 for m in leg if "replanning" in m), "fights": fights}
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        with open(os.path.join(os.path.dirname(OUT), "navtrack.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": rec["t"], "tag": tag, "inst": inst.INST, "to": name, "res": res,
                                "routes": routes, "track": track}) + "\n")
        log(f"LEG ->{name}: {res} in {dt:.0f}s, plan {plan} yd, straight {rec['straight']} yd, "
            f"{rec['stucks']} stuck(s){' (fight)' if fought else ''}")
        if peace and res != "ok" and li in lazy and not os.path.exists(STOP):
            # a leg the walk could not finish: logged for the fix list, then teleport onto its goal so the rest of
            # the route still gets walked (the Algaz Station fence, #416, beat five general fixes in a row)
            log(f"SKIPPED-STUCK {name}: teleporting to its goal ({x:.1f},{y:.1f},{z if z is None else round(z, 1)}) "
                f"from ({frm[0]},{frm[1]},{frm[2]}) map {mv.nm.map_id}")
            if z is None and z0 is not None:
                # the goal's own height (an NPC's spawn), not the highest floor there: Rut'theran's 54 yd ledge over
                # Vesprystus at 23 - the walk off it was a fall to death (#1460/#1461 rxp42 15:46)
                z = z0
            if z is None:                             # the planner's pick failed too: the highest floor there
                fz = list(mv.floors_at(x, y))
                if not fz:
                    # no mesh floor: the terrain, never our own height (from 165 over Stonetalon's 125 ground the
                    # fall took half our hp, #1418 rxp41 15:08)
                    try:
                        import numpy as np
                        import terrain
                        th = float(terrain.heights(np.array([x]), np.array([y]))[0])
                    except Exception:                 # noqa: BLE001
                        th = float("nan")
                    gz_ = mv.nm.ground_z(x, y)
                    fz = [th if th == th else (gz_ if gz_ is not None else (s.z or 0.0))]
                z = float(fz[0])
            try:
                teleport(bot, log, lazy[li][0] % 100, name, x, y, z, mv.nm.map_id)
            except Exception as e:                    # noqa: BLE001
                log(f"teleport failed: {e!r}")
    mv.stop()


if __name__ == "__main__":
    main()
