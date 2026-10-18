"""Flight masters: fly a long errand leg instead of walking it (2026-10-15: the 12-14 Loch Modan guide ends in
Ironforge and goes on in Stormwind by the Deeprun Tram; the walk is 3000+ yd through level 30 zones, and the bots
stood an hour at the Ironforge vendor).

  taxi.py (the old bot's)  node positions and flight times from the client's TaxiNodes / TaxiPath(Node).dbc
  known nodes              tcf_characters.characters.taximask of OUR character: the online row nearest our position
                           (bit n-1 of the byte string = node n; TrinityCore's TaxiMask)
  flight masters           tcf_world creatures with the flight master npcflag (8192) within FM_NODE_R yd of a node
  plan(...)                fly when walk(us -> A) + air(A -> B) + walk(B -> goal) + overhead beats the walk by
                           GAIN; straight-line yards x WALK_FACTOR stand for both walks (no path plan to decide)

Bot.fly_leg does it: walk to A's flight master, interact (BotEye picks the gossip's flight option: /be want taxi),
/be fly <B> (BotEye takes the node on TAXIMAP_OPENED), wait out the flight (the strip's taxi bit), then walk on.
"""

from __future__ import annotations

import math
import time

FLY_MIN = 700.0          # yd: a goal nearer than this is walked
WALK_SPEED = 5.0         # yd/s over ground, fights and detours included (taxi.py: 4.8-5.3 measured, old bot)
WALK_FACTOR = 1.35       # path length / straight line
OVERHEAD_S = 45.0        # s: talk, take-off, landing
GAIN = 0.75              # fly only when it takes under this share of the walk
FM_NODE_R = 60.0         # yd: a flight master stands on his node
MASK_TTL = 300.0         # s the known-node mask is kept
MATCH_R = 1500.0         # yd: the saved position of our row lags ours by minutes of walking
MAP = 0

_fms = None              # node id -> (entry, name, x, y, z)
_mask = (0.0, None)      # (read at, set of node ids)


def flight_masters() -> dict:
    """{node id: (npc entry, name, x, y, z)}: the flight master spawn nearest each map-0 node, within FM_NODE_R."""
    global _fms
    if _fms is None:
        import taxi
        import worlddb
        rows = worlddb._query_db(
            "tcf_world", "SELECT t.entry, t.name, c.position_x, c.position_y, c.position_z FROM creature c "
            "JOIN creature_template t ON t.entry = c.id WHERE c.map = %d AND (t.npcflag & 8192)" % MAP)
        out = {}
        for nid, n in taxi._data().nodes.items():
            if n["map"] != MAP:
                continue
            nx, ny = n["pos"][:2]
            best = None
            for e, name, x, y, z in rows:
                d = math.hypot(float(x) - nx, float(y) - ny)
                if d <= FM_NODE_R and (best is None or d < best[0]):
                    best = (d, (int(e), name, float(x), float(y), float(z)))
            if best:
                out[nid] = best[1]
        _fms = out
    return _fms


def mask_nodes(mask: str) -> set:
    """taximask "250 168 4 ..." -> {node ids}: byte i bit b is node i*8 + b + 1."""
    out = set()
    for i, v in enumerate(mask.split()):
        v = int(v)
        for b in range(8):
            if v >> b & 1:
                out.add(i * 8 + b + 1)
    return out


def known_nodes(x, y, now=None, name=None) -> set:
    """Our character's known nodes: its row by name when given, else the online character row nearest (x, y)
    (positions are saved every few minutes; the bots stand hundreds of yards apart or more). Empty when the DB cannot
    tell. (By position alone a hearth left the saved row 3 km behind: bot 3 2026-10-16 06:20 in Coldridge matched no
    row, knew no node, and planned 3.6 km on foot to Westfall, 'no path', every 40 s.)"""
    global _mask
    now = now or time.time()
    if _mask[1] is not None and now - _mask[0] < MASK_TTL:
        return _mask[1]
    import worlddb
    if name and str(name).isalpha():
        rows = worlddb._query_db("tcf_characters", "SELECT taximask FROM characters WHERE name = '%s'" % name)
        if rows:
            _mask = (now, mask_nodes(rows[0][0]))
            return _mask[1]
    rows = worlddb._query_db("tcf_characters",
                             "SELECT position_x, position_y, taximask FROM characters WHERE online = 1 AND map = %d" % MAP)
    best = min(rows, key=lambda r: math.hypot(float(r[0]) - x, float(r[1]) - y), default=None)
    nodes = mask_nodes(best[2]) if best and math.hypot(float(best[0]) - x, float(best[1]) - y) < MATCH_R else set()
    _mask = (now, nodes)
    return nodes


def walk_s(yd: float) -> float:
    return yd * WALK_FACTOR / WALK_SPEED


def plan(me, goal, known, fms, air=None, force=False, node_ok=None):
    """-> (A, B, saving s, why) to fly from node A to node B, or None. me / goal: (x, y). air(a, b) -> s or None
    (taxi.route_seconds over the known nodes). force: there is no walk (the planner found no path): the fastest
    flight, whatever the straight-line walk would take. node_ok(n) -> False leaves node n out (a flight master in a zone
    far above us: the forced plan from Coldridge took Thorium Point in Searing Gorge, level 45)."""
    d = math.hypot(goal[0] - me[0], goal[1] - me[1])
    if d < FLY_MIN:
        return None
    if air is None:
        import taxi
        air = lambda a, b: taxi.route_seconds(a, b, known)       # noqa: E731
    usable = [n for n in known if n in fms and (node_ok is None or node_ok(n))]
    if len(usable) < 2:
        return None
    walk = float("inf") if force else walk_s(d)
    best = None
    for a in usable:
        da = math.hypot(fms[a][2] - me[0], fms[a][3] - me[1])
        if walk_s(da) >= walk * GAIN:
            continue
        for b in usable:
            if b == a:
                continue
            t = air(a, b)
            if t is None:
                continue
            db = math.hypot(fms[b][2] - goal[0], fms[b][3] - goal[1])
            total = walk_s(da) + t + OVERHEAD_S + walk_s(db)
            if total < walk * GAIN and (best is None or total < best[2]):
                best = (a, b, total, da, db)
    if best is None:
        return None
    a, b, total, da, db = best
    if force:
        return a, b, 0.0, (f"no walk to the goal ({d:.0f} yd); {da:.0f} yd to {fms[a][1]}, fly, {db:.0f} yd from "
                           f"{fms[b][1]}'s landing: {total:.0f}s")
    return a, b, walk - total, (f"walk {walk:.0f}s ({d:.0f} yd) vs {da:.0f} yd to {fms[a][1]}, fly, {db:.0f} yd "
                                f"from {fms[b][1]}'s landing: {total:.0f}s")
