"""Navigation debugging from the command line, in text.

navview.py is the interactive viewer for a person; this is for quick
questions while the bot runs ("why can't it reach Grik'nir?"), answered in
text with the same navmesh.py the bot uses, and the track it actually walked
(the pos / nav / stuck events in data/events.jsonl).

    python navdbg.py here [P]              the ground under us (or P): floors,
                                           polygon flags, mesh island and its size
    python navdbg.py path TO [--from P] [--dangers]
                                           would a walk from us (or P) to TO work?
                                           length, detour ratio, time, and why not
    python navdbg.py scene [--min 5]       in words: the ground we are on, the walk
                                           to the current goal (detour ratio), hostile
                                           spawns by direction, how we moved lately
                                           (circling, standing still, stuck, failed walks)
    python navdbg.py navs [n]              the last n walks: from, to, result, time

Points (P / TO):  x,y[,z]  |  mob:<name|id> (nearest spawn)  |  npc:<name|id>
                  |  obj:<name|id>  |  goal (the bot's current destination)  |  me
Without a z, the floor height nearest our own is used.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import Counter

import events
import navmesh
import worlddb

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "status.json")

_nav = None


def nav() -> navmesh.NavMesh:
    global _nav
    if _nav is None:
        _nav = navmesh.NavMesh(map_id=0)
    return _nav


def status() -> dict:
    try:
        return json.load(open(STATUS, encoding="utf-8"))
    except Exception:
        return {}


def me_pos():
    p = status().get("pos")
    return tuple(p[:3]) if p else None


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def spawn_near(table, tmpl, arg, ref):
    """Nearest map-0 spawn of a creature/gameobject given by id or name."""
    key = "entry" if table == "creature" else "entry"
    if arg.isdigit():
        ids = [int(arg)]
    else:
        ids = [int(r[0]) for r in worlddb._query(
            f"SELECT DISTINCT {key} FROM {tmpl} WHERE name LIKE '%{_esc(arg)}%' LIMIT 20")]
    if not ids:
        return None
    rows = worlddb._query(f"SELECT position_x, position_y, position_z FROM {table} "
                          f"WHERE map=0 AND id IN ({','.join(map(str, ids))})")
    pts = [tuple(float(v) for v in r) for r in rows]
    if not pts:
        return None
    return min(pts, key=lambda p: math.dist(p[:2], ref[:2])) if ref else pts[0]


def point(arg: str, ref=None):
    """Parse a point argument into WoW (x, y, z)."""
    arg = arg.strip()
    if arg == "me":
        return me_pos()
    if arg == "goal":
        s = status()
        g, p = s.get("goal"), s.get("pos")
        if not (g and p):
            return None
        x, y = p[0] + g[0], p[1] + g[1]
        return (x, y, nav().ground_z(x, y, hint=p[2]) or p[2])
    for pre, table, tmpl in (("mob:", "creature", "creature_template"),
                             ("npc:", "creature", "creature_template"),
                             ("obj:", "gameobject", "gameobject_template")):
        if arg.startswith(pre):
            return spawn_near(table, tmpl, arg[len(pre):], ref or me_pos())
    parts = [float(v) for v in arg.replace(" ", "").split(",")]
    if len(parts) == 2:
        hint = (ref or me_pos() or (0, 0, None))[2]
        z = nav().ground_z(parts[0], parts[1], hint=hint)
        return (parts[0], parts[1], z if z is not None else (hint or 0.0))
    return tuple(parts[:3])


def fmt(p) -> str:
    return f"({p[0]:.1f}, {p[1]:.1f}, {p[2]:.1f})" if p else "?"


def comp_sizes() -> Counter:
    return Counter(nav().components().values())


# ------------------------------------------------------------------ here
def cmd_here(args):
    p = point(args[0]) if args else me_pos()
    if not p:
        print("no position")
        return
    n = nav()
    print(f"at {fmt(p)}")
    floors = []
    rx, rz = p[1], p[0]
    t = n.tile_at(rx, rz)
    if t is not None:
        m = ((t.pmin[:, 0] - 0.5 <= rx) & (t.pmax[:, 0] + 0.5 >= rx) &
             (t.pmin[:, 2] - 0.5 <= rz) & (t.pmax[:, 2] + 0.5 >= rz))
        for i in m.nonzero()[0]:
            vs = t.verts[t.pverts[i, :t.pcount[i]]]
            if navmesh._point_in_poly_2d(rx, rz, vs):
                floors.append((float(t.center[i, 1]), int(i), int(t.pflags[i]), t.usable(i)))
    print("floors here: " + (", ".join(f"z {z:.1f} flags {flags:#x}{'' if ok else ' UNUSABLE'}"
                                        for z, i, flags, ok in sorted(floors, reverse=True))
                             or "none (off the mesh)"))
    poly, _ = n.nearest_poly(*p)
    if poly is None:
        back = n.reachable_near(p, p, radius=60.0, tries=0)
        print("no usable polygon within reach" + (f"; nearest mesh {fmt(back)}, "
              f"{math.dist(back[:2], p[:2]):.1f} yd" if back else ""))
        return
    tt = n.tiles[poly[0]]
    c = n.components().get(poly)
    size = comp_sizes()[c]
    biggest = comp_sizes().most_common(1)[0]
    print(f"polygon {poly} flags {int(tt.pflags[poly[1]]):#x} area {int(tt.parea[poly[1]])} "
          f"centre z {tt.center[poly[1], 1]:.1f}  building {n.in_building(poly)}  "
          f"steepest edge {n.max_edge_slope(poly):.2f}")
    print(f"mesh island {c}: {size} polygons"
          + ("  (the main landmass)" if c == biggest[0] else
             f"  - NOT the main landmass ({biggest[1]} polys): nothing outside it is reachable"))
    try:
        holes = json.load(open(os.path.join(HERE, "data", "obstacles.json")))
    except Exception:
        holes = []
    near = [h for h in holes if math.dist(h[:2], p[:2]) < 25]
    if near:
        print(f"learned holes within 25 yd: {len(near)}: "
              + ", ".join(f"({h[0]:.0f}, {h[1]:.0f}) {math.dist(h[:2], p[:2]):.0f} yd" for h in near[:6]))


# ------------------------------------------------------------------ path
def plan(a, b, use_dangers=False):
    n = nav()
    try:
        holes = [tuple(h) for h in json.load(open(os.path.join(HERE, "data", "obstacles.json")))]
    except Exception:
        holes = []
    dz = ()
    if use_dangers:
        import dangers
        lvl = status().get("level") or 1
        pad = 300
        dz = dangers.zones(0, lvl, 3, bbox=(min(a[0], b[0]) - pad, min(a[1], b[1]) - pad,
                                            max(a[0], b[0]) + pad, max(a[1], b[1]) + pad))
    far = math.dist(a[:2], b[:2])
    kw = {"max_nodes": 1500000, "budget": 150.0} if far > 700 else {}
    t0 = time.time()
    path = n.find_path(a, b, dangers=dz, holes=holes, **kw)
    return path, time.time() - t0, len(dz)


def cmd_path(args):
    frm = None
    if "--from" in args:
        i = args.index("--from")
        frm = point(args[i + 1])
        args = args[:i] + args[i + 2:]
    use_d = "--dangers" in args
    args = [a for a in args if a != "--dangers"]
    a = frm or me_pos()
    b = point(" ".join(args), ref=a)
    if not (a and b):
        print("cannot resolve the points")
        return
    n = nav()
    straight = math.dist(a[:2], b[:2])
    print(f"from {fmt(a)} to {fmt(b)}: {straight:.0f} yd straight, dz {b[2] - a[2]:+.1f}")
    pa, pb = n.nearest_poly(*a)[0], n.nearest_poly(*b)[0]
    comp = n.components()
    for label, p, poly in (("start", a, pa), ("end", b, pb)):
        if poly is None:
            near = n.reachable_near(p, p, radius=40.0, tries=0)
            print(f"  {label}: NO polygon near {fmt(p)} (height off, or off the mesh)"
                  + (f"; nearest mesh {fmt(near)}, {math.dist(near[:2], p[:2]):.1f} yd" if near else ""))
    if pa is not None and pb is not None and comp.get(pa) != comp.get(pb):
        sz = comp_sizes()
        print(f"  DIFFERENT mesh islands: start on {comp.get(pa)} ({sz[comp.get(pa)]} polys), "
              f"end on {comp.get(pb)} ({sz[comp.get(pb)]} polys) - no walk exists")
        alt = n.reachable_near(a, b, radius=40.0, tries=8)
        if alt:
            print(f"  closest reachable point: {fmt(alt)}, {math.dist(alt[:2], b[:2]):.1f} yd from the target")
    path, secs, nd = plan(a, b, use_d)
    if not path:
        print(f"  NO PATH ({secs:.1f}s search{f', {nd} danger zones' if nd else ''})")
        return
    pts = [a] + list(path)
    length = sum(math.dist(pts[i][:2], pts[i + 1][:2]) for i in range(len(pts) - 1))
    climb = sum(max(0.0, pts[i + 1][2] - pts[i][2]) for i in range(len(pts) - 1))
    print(f"  path: {length:.0f} yd ({length / max(straight, 1):.2f}x straight), {len(path)} waypoints, "
          f"climb {climb:.0f} yd, planned in {secs:.2f}s{f', {nd} danger zones' if nd else ''}; "
          f"~{length / 7:.0f}s at run speed")
    step = max(1, len(pts) // 12)
    print("  via " + " -> ".join(f"({p[0]:.0f},{p[1]:.0f},{p[2]:.0f})" for p in pts[::step]))


# ------------------------------------------------------------------ navs
def cmd_navs(args):
    k = int(args[0]) if args else 15
    evs = [e for e in events.read() if e["ev"] in ("nav", "nav_end", "stuck")]
    rows, cur = [], None
    for e in evs:
        if e["ev"] == "nav":
            cur = {"s": e, "stuck": 0}
            rows.append(cur)
        elif cur is not None and e["ev"] == "stuck":
            cur["stuck"] += 1
        elif cur is not None and e["ev"] == "nav_end":
            cur["e"] = e
            cur = None
    for r in rows[-k:]:
        s, e = r["s"], r.get("e")
        t = time.strftime("%H:%M:%S", time.localtime(s["t"]))
        res = ("ARRIVED" if e["ok"] else f"FAILED {e['left']} yd short") if e else "still going"
        print(f"{t} {s['dist']:>5} yd  ({s['at'][0]:.0f},{s['at'][1]:.0f}) -> ({s['to'][0]:.0f},{s['to'][1]:.0f})  "
              f"{res}" + (f" after {e['secs']:.0f}s ({s['dist'] / max(e['secs'], 1):.1f} yd/s)" if e else "")
              + (f"  stuck x{r['stuck']}" if r["stuck"] else ""))


# ------------------------------------------------------------------ scene
COMPASS = ["N", "NW", "W", "SW", "S", "SE", "E", "NE"]


def bearing(frm, to) -> str:
    """Compass direction: WoW +x is north, +y is west."""
    a = math.degrees(math.atan2(to[1] - frm[1], to[0] - frm[0])) % 360
    return COMPASS[int((a + 22.5) // 45) % 8]


def cmd_scene(args):
    """What is round us, in words: ground, the plan, mobs, how we moved."""
    mins = float(args[args.index("--min") + 1]) if "--min" in args else 5.0
    s = status()
    me = me_pos()
    if not me:
        print("no position")
        return
    n = nav()
    poly = n.nearest_poly(*me)[0]
    lvl = s.get("level") or 1
    print(f"{s.get('name')} lvl {lvl} at {fmt(me)}, {s.get('state')}"
          f" ({(s.get('detail') or '')[:60]})")
    if poly is None:
        print("  OFF the navmesh")
    else:
        big = comp_sizes().most_common(1)[0][0]
        print(f"  ground: {'inside a building/cave' if n.in_building(poly) else 'open ground'}"
              + ("" if n.components().get(poly) == big else ", ON A MESH ISLAND (cut off from the world)"))
    goal = point("goal")
    if goal:
        path, secs, _ = plan(me, goal)
        straight = math.dist(me[:2], goal[:2])
        if path:
            pts = [me] + list(path)
            walk = sum(math.dist(pts[i][:2], pts[i + 1][:2]) for i in range(len(pts) - 1))
            print(f"  heading {bearing(me, goal)} to {fmt(goal)}: {straight:.0f} yd straight, "
                  f"{walk:.0f} yd by path ({walk / max(straight, 1):.1f}x), dz {goal[2] - me[2]:+.0f}")
            if walk > 2.5 * straight and straight > 20:
                print("    NOTE: long detour - the goal may be on another level or behind a wall")
        else:
            print(f"  heading {bearing(me, goal)} to {fmt(goal)}, {straight:.0f} yd: NO PATH from here")
    # live units from the status file (relative positions)
    units = s.get("units") or []
    if units:
        kinds = Counter(u[2] for u in units if len(u) > 2)
        mobs = sorted((math.hypot(u[0], u[1]), bearing((0, 0), (u[0], u[1]))) for u in units
                      if len(u) > 2 and u[2] == "mob")
        print("  in view: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common())
              + ("; nearest live mobs " + ", ".join(f"{d:.0f} yd {b}" for d, b in mobs[:4]) if mobs else ""))
    # spawns round us from the DB, grouped by direction
    try:
        import dangers
        import factions
        groups = {}
        for mp, x, y, z, w, e, lo, hi, rank, fac, det in dangers._load():
            if mp == 0 and math.dist((x, y), me[:2]) < 60 and factions.is_hostile(3, fac):
                k = (bearing(me, (x, y)), worlddb.name(e), lo, hi)
                groups.setdefault(k, []).append(math.dist((x, y), me[:2]))
        if groups:
            print("  hostile spawns within 60 yd:")
            for (b, name, lo, hi), ds in sorted(groups.items(), key=lambda kv: min(kv[1]))[:8]:
                tag = " (ABOVE US)" if hi >= lvl + 2 else ""
                print(f"    {len(ds)} x {name} {lo}-{hi}{tag}, {b}, nearest {min(ds):.0f} yd")
    except Exception as ex:
        print("  spawns:", ex)
    # how we moved over the last few minutes
    since = time.time() - mins * 60
    evs = [e for e in events.read() if e["t"] >= since]
    pos = [e for e in evs if e["ev"] == "pos"]
    if len(pos) > 1:
        walked = sum(math.dist((a["x"], a["y"]), (b["x"], b["y"])) for a, b in zip(pos, pos[1:]))
        net = math.dist((pos[0]["x"], pos[0]["y"]), (pos[-1]["x"], pos[-1]["y"]))
        print(f"  last {mins:.0f} min: walked {walked:.0f} yd, net {net:.0f} yd {bearing((pos[0]['x'], pos[0]['y']), (pos[-1]['x'], pos[-1]['y']))}"
              + ("  - CIRCLING (walked 3x+ the net distance)" if walked > 3 * max(net, 10) and walked > 60 else ""))
        # standing still while "travelling" is the stuck signature
        still, run0 = [], None
        for a, b in zip(pos, pos[1:]):
            moving = math.dist((a["x"], a["y"]), (b["x"], b["y"])) > 1.0
            if not moving and a.get("s") == "travelling":
                run0 = run0 or a
            elif run0:
                if b["t"] - run0["t"] >= 6:
                    still.append((run0, b["t"] - run0["t"]))
                run0 = None
        for r, secs in still[-4:]:
            print(f"    stood still {secs:.0f}s while travelling at ({r['x']:.0f}, {r['y']:.0f}) "
                  f"{time.strftime('%H:%M:%S', time.localtime(r['t']))}")
    stuck = [e for e in evs if e["ev"] == "stuck"]
    if stuck:
        spots = Counter((round(e["x"] / 10) * 10, round(e["y"] / 10) * 10) for e in stuck)
        print("  stuck reports: " + ", ".join(f"({x},{y}) x{k}" for (x, y), k in spots.most_common(4)))
    navs = [e for e in evs if e["ev"] == "nav_end" and not e["ok"]]
    for e in navs[-3:]:
        print(f"  walk FAILED {e['left']} yd short after {e['secs']:.0f}s at ({e['at'][0]:.0f}, {e['at'][1]:.0f})")


COMMANDS = {"here": cmd_here, "path": cmd_path, "scene": cmd_scene, "navs": cmd_navs}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
