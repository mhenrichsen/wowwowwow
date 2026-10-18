"""Zone map percentages (as used by guides, ".goto Elwynn Forest,56.7,44") to
world coordinates, using the client's own WorldMapArea.dbc."""

from __future__ import annotations

import struct

DBC = r"C:\Users\MadsG\Desktop\wow-forever-server\server\data\5875\dbc\WorldMapArea.dbc"

# RestedXP names a zone by its Classic uiMapID as often as by name
# (".goto 1415,45.2,71.3"). From the addon's own table, DB/classic/db.lua.
UI_MAP = {
    1411: "Durotar", 1412: "Mulgore", 1413: "The Barrens", 1414: "Kalimdor",
    1415: "Eastern Kingdoms", 1416: "Alterac Mountains", 1417: "Arathi Highlands",
    1418: "Badlands", 1419: "Blasted Lands", 1420: "Tirisfal Glades",
    1421: "Silverpine Forest", 1422: "Western Plaguelands", 1423: "Eastern Plaguelands",
    1424: "Hillsbrad Foothills", 1425: "The Hinterlands", 1426: "Dun Morogh",
    1427: "Searing Gorge", 1428: "Burning Steppes", 1429: "Elwynn Forest",
    1430: "Deadwind Pass", 1431: "Duskwood", 1432: "Loch Modan",
    1433: "Redridge Mountains", 1434: "Stranglethorn Vale", 1435: "Swamp of Sorrows",
    1436: "Westfall", 1437: "Wetlands", 1438: "Teldrassil", 1439: "Darkshore",
    1440: "Ashenvale", 1441: "Thousand Needles", 1442: "Stonetalon Mountains",
    1443: "Desolace", 1444: "Feralas", 1445: "Dustwallow Marsh", 1446: "Tanaris",
    1447: "Azshara", 1448: "Felwood", 1449: "Un'Goro Crater", 1450: "Moonglade",
    1451: "Silithus", 1452: "Winterspring", 1453: "Stormwind City", 1454: "Orgrimmar",
    1455: "Ironforge", 1456: "Thunder Bluff", 1457: "Darnassus", 1458: "Undercity",
    1459: "Alterac Valley",
}

# The DBC stores Blizzard's internal names, some of them misspelled, and
# prefix matching cannot bridge those ("Hillsbrad" is not a prefix of
# "Hilsbrad"). Keys and values are in _key() form.
ALIAS = {
    "hillsbradfoothills": "hilsbrad",
    "hillsbrad": "hilsbrad",
    "thehinterlands": "hinterlands",
    "thebarrens": "barrens",
    "azshara": "aszhara",
    "darnassus": "darnassis",
    "orgrimmar": "ogrimmar",
    "easternkingdoms": "azeroth",
    # RXP's own names for Stormwind and the Plaguelands as the Classic
    # client draws them. The "...New" forms are Wrath-era maps that need a
    # coordinate transform; no Survival guide uses them, so they stay
    # unresolved and get reported instead of quietly landing wrong.
    "stormwindclassic": "stormwind",
    "eplclassic": "easternplaguelands",
}

_zones: dict[str, tuple[int, float, float, float, float]] | None = None


def _key(name: str) -> str:
    return name.lower().replace(" ", "").replace("'", "")


def _load():
    global _zones
    if _zones is None:
        d = open(DBC, "rb").read()
        _, n, f, size, _ = struct.unpack_from("<4s4I", d)
        strings = d[20 + n * size:]
        _zones = {}
        for i in range(n):
            r = d[20 + i * size:20 + (i + 1) * size]
            _id, map_id, _area, name_off = struct.unpack_from("<4I", r)
            left, right, top, bottom = struct.unpack_from("<4f", r, 16)
            name = strings[name_off:strings.index(b"\0", name_off)].decode()
            if name:
                _zones[name.lower()] = (map_id, left, right, top, bottom)
    return _zones


def zone(name: str):
    """Match a guide zone ("Elwynn Forest", "1429") to the DBC's ("Elwynn")."""
    name = str(name).strip()
    if name.isdigit():
        name = UI_MAP.get(int(name), name)
    key = _key(name)
    key = ALIAS.get(key, key)
    zs = _load()
    if key in zs:
        return zs[key]
    if key.endswith("new"):
        return None                     # see ALIAS: needs a transform we do not have
    for k in sorted(zs, key=len, reverse=True):
        if key.startswith(k) or k.startswith(key):
            return zs[k]
    return None


def to_world(px: float, py: float, zone_name: str):
    """(map id, world x, world y) for a point given in map percent."""
    z = zone(zone_name)
    if z is None:
        return None
    map_id, left, right, top, bottom = z
    return map_id, top - (top - bottom) * py / 100.0, left - (left - right) * px / 100.0
