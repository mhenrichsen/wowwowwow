"""Deeprun Tram (map 369) on foot: the tunnel between the two stations is walkable (the bot's map-369 mesh,
data_tcf369, connects the Stormwind arrival to the Ironforge exit, ~2750 yd), so the bot walks it instead of waiting
for a car (a clock-timed ride missed the cars 2026-10-18).

  portals()                area-trigger teleports: (id, map, x, y, z, radius, dest_map, dx, dy, dz)
  portal_into(map, xy)     the portal into a map nearest xy
  walk_tram(bot, xy)       portal in, walk the tunnel to the far end's exit portal, back out on map 0
"""

from __future__ import annotations

import math
import time

DB2_AREATRIGGER = "D:/tcforever/server/data/dbc/enUS/AreaTrigger.db2"
MAP = 369
SW_END_Y = 1000.0          # arrival y above this = the Stormwind end
WALK_TIMEOUT_S = 900.0
_portals = None


def portals() -> list:
    """[(id, map, x, y, z, radius, dest_map, dx, dy, dz)] for the areatrigger_teleport rows (box: its longer half-side)."""
    global _portals
    if _portals is None:
        import db2
        import worlddb
        shapes = {}
        for rid, v in db2.read(DB2_AREATRIGGER).items():
            f = lambda i: db2.as_float(v[i][0])           # noqa: E731
            pos = [db2.as_float(u) for u in v[0]]
            rad = f(6) if f(6) > 0 else max(f(7), f(8)) / 2
            shapes[rid] = (v[2][0], pos[0], pos[1], pos[2], rad)
        out = []
        for tid, mp, lx, ly, lz in worlddb._query_db(
                "tcf_world", "SELECT a.ID, w.MapID, w.LocX, w.LocY, w.LocZ FROM areatrigger_teleport a "
                "JOIN world_safe_locs w ON w.ID = a.PortLocID"):
            sh = shapes.get(int(tid))
            if sh:
                out.append((int(tid), sh[0], sh[1], sh[2], sh[3], max(sh[4], 3.0), int(mp), float(lx), float(ly),
                            float(lz)))
        _portals = out
    return _portals


def portal_into(map_id: int, near_xy, within=60.0):
    """The portal on another map that leads into `map_id`, nearest to near_xy within `within` yd (or None)."""
    best = None
    for p in portals():
        if p[6] == map_id and p[1] != map_id:
            d = math.hypot(p[2] - near_xy[0], p[3] - near_xy[1])
            if d <= within and (best is None or d < best[0]):
                best = (d, p)
    return best[1] if best else None


def _s(bot, wait=25.0):
    """bot.s() that rides out the strip being unreadable while a map loads."""
    end = time.time() + wait
    while True:
        try:
            return bot.s()
        except RuntimeError:
            if time.time() > end:
                raise
            time.sleep(0.5)


def _log(bot, msg):
    try:
        bot.rlog(msg)
    except Exception:
        print(msg)


def walk_tram(bot, entrance_xy=None, back=False) -> bool:
    """Portal into the Deeprun (when not in it yet), walk the tunnel to the exit portal at the other end, step out.
    True once back on map 0 at the far end."""
    mv = bot.mv
    s = _s(bot)
    if s.inst == 0:
        pin = portal_into(MAP, entrance_xy or (s.x, s.y), within=400.0)
        if pin is None:
            _log(bot, "tram: no portal into the tram near here")
            return False
        _log(bot, f"tram: walking into trigger {pin[0]} at ({pin[2]:.0f},{pin[3]:.0f})")
        bot.travel_to(pin[2], pin[3], pin[4], tol=max(1.0, pin[5] * 0.4), timeout=240)
        t = time.time()
        while _s(bot).inst != MAP and time.time() - t < 12:
            time.sleep(0.3)
        if _s(bot).inst != MAP:
            _log(bot, "tram: the trigger did not take us in")
            return False
        time.sleep(1.5)
    s = _s(bot)
    if s.inst != MAP:
        return False
    mv.set_map(MAP)
    time.sleep(0.5)
    s = _s(bot)
    # the exit at the far end: a portal out of the tram whose y is on the other side
    far_sw = s.y < SW_END_Y
    if back:                                  # the exit at the end we are at
        far_sw = not far_sw
    outs = [p for p in portals() if p[1] == MAP and p[6] != MAP and (p[3] > SW_END_Y) == far_sw]
    if not outs:
        return False
    ex = outs[0]
    _log(bot, f"tram: walking the tunnel to exit trigger {ex[0]} ({ex[2]:.0f},{ex[3]:.0f})")
    t = time.time()
    while time.time() - t < WALK_TIMEOUT_S:
        s = _s(bot)
        if s.inst != MAP:
            break
        if s.dead:
            return False
        try:
            r = mv.goto(ex[2], ex[3], ex[4], tol=max(1.0, ex[5] * 0.4), timeout=240)
        except RuntimeError:                  # the strip is unreadable while the exit loads the next map
            time.sleep(2.0)
            continue
        if r == "abort":
            return False
    s = _s(bot, 60.0)
    if s.inst == ex[6]:
        mv.set_map(ex[6])
        mv.z = ex[9]                          # the portal's own landing height: the floor pick would take the mountain
        mv._last_xy = (s.x, s.y, time.time())  # over the Ironforge lobby (z 750 over 502) and plan 1600 yd up it
        _log(bot, f"tram: out on map {ex[6]} at ({s.x:.0f},{s.y:.0f})")
        return True
    return False
