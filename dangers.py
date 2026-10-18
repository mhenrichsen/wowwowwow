"""Where NOT to walk: aggro zones of mobs that could kill us.

Built from the world DB spawn table. A spawn is dangerous when its mob is
hostile to us (neutral mobs never start a fight) and either several levels
above us or elite. Its zone radius follows the server's own formula
(Creature::GetAttackDistance): detection_range + (mob level - our level),
at least 5 yd, plus how far the mob wanders from its spawn point and a safety
margin.
"""

from __future__ import annotations

import factions
import worlddb

# Mobs this character has never beaten. Measured over 178 logs at level
# 15-17: Black Dragon Whelp 6 fights / 6 escapes / 2 deaths, Dire Condor
# 2/2/2, Redridge Mongrel 1/1/1 - nine fights and not one win - while Great
# Goretusk went 24 fights with no escape and no death. The answer to a mob
# we cannot beat is distance, so give these a wide berth when routing.
NEMESIS = {
    441: "Black Dragon Whelp",
    428: "Dire Condor",
    423: "Redridge Mongrel",
    521: "Lupos",              # level 23 RARE ELITE that roams the
                               # Duskwood wolf spot - a level-19 mage
                               # has no business fighting it
}
NEMESIS_BERTH = 12.0    # extra yards on top of the usual aggro radius

LEVEL_MARGIN = 1        # when travelling, steer around mobs this many levels above us
                        # (3 was too lax: level 6 wolves chain-aggro'd a level 5 mage)
SAFETY_MARGIN = 8.0     # extra yards on top of the aggro radius
CAMP_RADIUS = 15.0      # hostile spawns this close together form a camp ...
CAMP_SIZE = 3           # ... of at least this many: dangerous even at our level
                        # (a level 5 mage beats one Defias thug, not three)

_spawns = None


TCF_DETECT = 20.0     # yd: TrinityCore's creature_template has no detection_range; the classic aggro base


def _load_tcf():
    """The Forever server's own spawn table (tcf_world): positions, the levels it really spawns
    (creature_classic_level) and patrol routes (waypoint_path_node via creature_addon.PathId). The 1.12 table
    (_load_mangos) was the source until 2026-10-15; this server moved and added spawns."""
    rows = worlddb._query_db(
        "tcf_world",
        "SELECT c.map, c.position_x, c.position_y, c.position_z, c.wander_distance, "
        "t.entry, l.level_min, l.level_max, t.Classification, t.faction, t.type "
        "FROM creature c JOIN creature_template t ON t.entry = c.id JOIN creature_classic_level l ON l.entry = c.id "
        "WHERE t.npcflag = 0 AND t.type <> 8 "
        "AND c.guid NOT IN (SELECT guid FROM game_event_creature WHERE eventEntry > 0)")
    out = [(int(m), float(x), float(y), float(z), float(w), int(e), int(lo), int(hi), int(rk), int(fac), TCF_DETECT)
           for m, x, y, z, w, e, lo, hi, rk, fac, _t in rows]
    _types.update({int(r[5]): int(r[10]) for r in rows})
    # patrolling elites / rares are dangerous along their route (see _load_mangos): every ~15 yd counts
    try:
        prow = worlddb._query_db(
            "tcf_world",
            "SELECT c.guid, c.map, n.PositionX, n.PositionY, n.PositionZ, t.entry, l.level_min, l.level_max, "
            "t.Classification, t.faction "
            "FROM creature c JOIN creature_addon a ON a.guid = c.guid JOIN waypoint_path_node n ON n.PathId = a.PathId "
            "JOIN creature_template t ON t.entry = c.id JOIN creature_classic_level l ON l.entry = c.id "
            "WHERE a.PathId > 0 AND t.Classification > 0 AND t.npcflag = 0 ORDER BY c.guid, n.NodeId")
        last = {}
        for g, m, x, y, z, e, lo, hi, rk, fac in prow:
            x, y = float(x), float(y)
            q = last.get(g)
            if q and (x - q[0]) ** 2 + (y - q[1]) ** 2 < 15.0 ** 2:
                continue
            last[g] = (x, y)
            out.append((int(m), x, y, float(z), 0.0, int(e), int(lo), int(hi), int(rk), int(fac), TCF_DETECT))
    except Exception:
        pass
    return out


def _load():
    global _spawns
    if _spawns is None:
        try:
            _spawns = _load_tcf()
            if _spawns:
                return _spawns
        except Exception:
            pass
        _spawns = _load_mangos()
    return _spawns


def _load_mangos():
    global _spawns
    if True:
        rows = worlddb._query(
            "SELECT c.map, c.position_x, c.position_y, c.position_z, c.wander_distance, "
            "t.entry, t.level_min, t.level_max, t.rank, t.faction, t.detection_range, t.type "
            "FROM creature c JOIN creature_template t ON t.entry = c.id AND t.patch = "
            "(SELECT MAX(patch) FROM creature_template WHERE entry = c.id) "
            "WHERE t.npc_flags = 0 AND t.type <> 8 AND c.patch_min <= 10 AND c.patch_max >= 10 "
            # spawns that exist only during a world event (Scourge Invasion's
            # Skeletal Troopers, event 17): the grinder walked 266 yd to a
            # "hunting ground" of 29 of them and found nothing there
            "AND c.guid NOT IN (SELECT guid FROM game_event_creature WHERE event > 0)")
        _spawns = [(int(m), float(x), float(y), float(z), float(w), int(e), int(lo), int(hi),
                    int(rk), int(fac), float(det)) for m, x, y, z, w, e, lo, hi, rk, fac, det, _t in rows]
        _types.update({int(r[5]): int(r[11]) for r in rows})
        # Elites and rares that PATROL (movement_type 2) are dangerous along
        # their whole route, not just at the spawn: Old Icebeard (elite 11)
        # walks 150 yd of road and caught the level-7 warrior 55 yd from his
        # spawn point. Every ~15 yd of the route counts as a spawn of its own.
        try:
            prow = worlddb._query(
                "SELECT c.guid, c.map, m.position_x, m.position_y, m.position_z, "
                "t.entry, t.level_min, t.level_max, t.rank, t.faction, t.detection_range "
                "FROM creature c JOIN creature_movement m ON m.id = c.guid "
                "JOIN creature_template t ON t.entry = c.id AND t.patch = "
                "(SELECT MAX(patch) FROM creature_template WHERE entry = c.id) "
                "WHERE c.movement_type = 2 AND t.rank > 0 AND t.npc_flags = 0 "
                "AND c.patch_min <= 10 AND c.patch_max >= 10 ORDER BY c.guid, m.point")
            last = {}
            for g, m, x, y, z, e, lo, hi, rk, fac, det in prow:
                x, y = float(x), float(y)
                p = last.get(g)
                if p and (x - p[0]) ** 2 + (y - p[1]) ** 2 < 15.0 ** 2:
                    continue
                last[g] = (x, y)
                _spawns.append((int(m), x, y, float(z), 0.0, int(e), int(lo), int(hi),
                                int(rk), int(fac), float(det)))
        except Exception:
            pass
    return _spawns


_types: dict[int, int] = {}          # creature entry -> creature_template.type
SHEEPABLE_TYPES = (1, 7)             # beast, humanoid: Polymorph works, a pair is survivable


_camp: dict[int, int] | None = None


def _camp_sizes():
    """Spawn index -> number of spawns (itself included) within CAMP_RADIUS."""
    global _camp
    if _camp is None:
        sp = _load()
        grid: dict[tuple[int, int, int], list[int]] = {}
        for i, s in enumerate(sp):
            grid.setdefault((s[0], int(s[1] // CAMP_RADIUS), int(s[2] // CAMP_RADIUS)), []).append(i)
        _camp = {}
        for i, s in enumerate(sp):
            gx, gy = int(s[1] // CAMP_RADIUS), int(s[2] // CAMP_RADIUS)
            n = 0
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in grid.get((s[0], gx + dx, gy + dy), ()):
                        o = sp[j]
                        if (o[1] - s[1]) ** 2 + (o[2] - s[2]) ** 2 <= CAMP_RADIUS ** 2:
                            n += 1
            _camp[i] = n
    return _camp


HUB_R = 25.0           # yd round a hostile faction's service NPC (polestar hubs.js: vendor, trainer, flight master, inn)
HUB_LINK = 60.0        # yd: service NPCs this close form one town; a town needs HUB_MIN of them
HUB_MIN = 3
_hubs = None


def _load_hubs():
    """[(map, x, y, z, faction)] service NPCs (npcflag set) of towns: polestar's hostile-hub disks, from tcf_world."""
    global _hubs
    if _hubs is None:
        try:
            rows = worlddb._query_db(
                "tcf_world",
                "SELECT c.map, c.position_x, c.position_y, c.position_z, t.faction FROM creature c "
                "JOIN creature_template t ON t.entry = c.id WHERE t.npcflag <> 0 AND c.map IN (0, 1)")
            pts = [(int(m), float(x), float(y), float(z), int(f)) for m, x, y, z, f in rows]
        except Exception:                                 # noqa: BLE001
            pts = []
        # keep the ones in a town: HUB_MIN+ service NPCs linked within HUB_LINK (a lone quest giver in the wild
        # is no hub)
        grid = {}
        for i, p in enumerate(pts):
            grid.setdefault((p[0], int(p[1] // HUB_LINK), int(p[2] // HUB_LINK)), []).append(i)
        keep = []
        for i, p in enumerate(pts):
            n = 0
            for a in (-1, 0, 1):
                for b in (-1, 0, 1):
                    for j in grid.get((p[0], int(p[1] // HUB_LINK) + a, int(p[2] // HUB_LINK) + b), ()):
                        if j != i and abs(pts[j][1] - p[1]) <= HUB_LINK and abs(pts[j][2] - p[2]) <= HUB_LINK:
                            n += 1
            if n + 1 >= HUB_MIN:
                keep.append(p)
        _hubs = keep
    return _hubs


def zones(map_id: int, level: int, faction: int, bbox=None):
    """[(x, y, z, radius)] aggro zones to avoid at this level.

    bbox: optional (xmin, ymin, xmax, ymax) to limit the search.
    """
    out = []
    camp = _camp_sizes()
    for i, (m, x, y, z, wander, _entry, lo, hi, rank, fac, det) in enumerate(_load()):
        if m != map_id:
            continue
        if bbox and not (bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]):
            continue
        # a camp of three level-10/11 footpads plus a golem killed a level-14
        # mage on a repair run: camps count four levels down, big camps six
        n_camp = camp.get(i, 1)
        in_camp = (n_camp >= CAMP_SIZE and hi >= level - 4) or (n_camp >= 5 and hi >= level - 6)
        # a pair at our level (Defias trapper + smuggler north of Sentinel
        # Hill) is a lost fight for a mage too: a vendor run through them died
        # ... and a pair of anything Polymorph cannot take (harvest golems,
        # 11-12, killed a level-14 mage twice today) counts three levels down
        pair = camp.get(i, 1) >= 2 and (hi >= level - 1 or
                                        (hi >= level - 3 and _types.get(_entry) not in SHEEPABLE_TYPES))
        nemesis = _entry in NEMESIS
        if not (nemesis or hi >= level + LEVEL_MARGIN or (rank > 0 and hi >= level - 2)
                or in_camp or pair):
            continue
        if not factions.is_hostile(faction, fac):
            continue
        aggro = max(det + (hi - level), 5.0)
        berth = NEMESIS_BERTH if nemesis else 0.0
        out.append((x, y, z, aggro + wander + SAFETY_MARGIN + berth))
    # hostile towns at any level (their guards call each other; polestar prices them x20)
    for m, x, y, z, fac in _load_hubs():
        if m != map_id or (bbox and not (bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3])):
            continue
        if factions.is_hostile(faction, fac):
            out.append((x, y, z, HUB_R))
    return out


if __name__ == "__main__":
    import sys
    lvl = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    zs = zones(0, lvl, 1, bbox=(-10200, -1600, -8300, 900))
    print(f"level {lvl}: {len(zs)} dangerous spawns in Elwynn; "
          f"radius {min(z[3] for z in zs):.0f}-{max(z[3] for z in zs):.0f} yd")
