"""Zone of a world point: the server .map files' area grid (16 x 16 per tile, AreaTable ids) and the classic
AreaTable.dbc's parent links, so a subzone (Goldshire) reads as its zone (Elwynn Forest).
Zone mob levels (polestar_data ZoneLevels, P10..P90 of hostile normal mobs) give polestar's zone danger."""
from __future__ import annotations

import os
import re
import struct

import numpy as np

import terrain

AREA_DBC = r"C:\Users\MadsG\Desktop\wow-forever-server\server\data\5875\dbc\AreaTable.dbc"
LEVELS_LUA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "polestar_data", "addon_Polestar_Maps_ZoneLevels.lua")
_areas: dict | None = None
_grid: dict = {}
_levels: dict | None = None


def _load_areas():
    global _areas
    if _areas is None:
        d = open(AREA_DBC, "rb").read()
        _, n, f, size, _ = struct.unpack_from("<4s4I", d)
        strings = d[20 + n * size:]
        _areas = {}
        for i in range(n):
            r = struct.unpack_from(f"<{f}I", d, 20 + i * size)
            name = strings[r[11]:strings.index(b"\0", r[11])].decode("utf-8", "replace")
            _areas[r[0]] = (r[2], name)
    return _areas


def _area_grid(map_id, tx, ty):
    key = (map_id, tx, ty)
    if key in _grid:
        return _grid[key]
    g = None
    p = os.path.join(terrain.MAP_DIR, f"{map_id:04d}_{tx:02d}_{ty:02d}.map")
    if os.path.exists(p):
        with open(p, "rb") as fh:
            data = fh.read()
        h = struct.unpack_from("<4s10I", data, 0)
        ao = h[3] if h[0] == b"MAPS" else 0
        if ao:
            _, fl, ga = struct.unpack_from("<4sHH", data, ao)
            if fl & 1:                                       # MAP_AREA_NO_AREA: one area for the tile
                g = np.full((16, 16), ga, np.int32)
            else:
                g = np.frombuffer(data, "<u2", 256, ao + 8).reshape(16, 16).astype(np.int32)
    _grid[key] = g
    return g


def area_at(x, y, map_id=None) -> int:
    """AreaTable id (zone or subzone) at a world point; 0 when unknown."""
    if map_id is None:
        import curmap
        map_id = curmap.MAP
    gx, gy = 128 * (32 - x / terrain.TILE), 128 * (32 - y / terrain.TILE)
    tx, ty = int(gx // 128), int(gy // 128)
    g = _area_grid(map_id, tx, ty)
    if g is None:
        return 0
    i, j = min(15, max(0, int((gx - tx * 128) // 8))), min(15, max(0, int((gy - ty * 128) // 8)))
    return int(g[i, j])


def zone_name(x, y, map_id=None) -> str | None:
    """The zone's name (the top of the AreaTable parent chain) at a world point."""
    a = area_at(x, y, map_id)
    areas = _load_areas()
    seen = 0
    while a in areas and areas[a][0] and seen < 5:
        a, seen = areas[a][0], seen + 1
    return areas[a][1] if a in areas else None


def zone_levels() -> dict:
    """{zone name: (min, max)} from polestar's ZoneLevels table."""
    global _levels
    if _levels is None:
        _levels = {}
        try:
            for m in re.finditer(r"\[\d+\]\s*=\s*\{\s*(\d+),\s*(\d+)[^}]*\},\s*--\s*(.+)", open(LEVELS_LUA, encoding="utf-8").read()):
                _levels[m.group(3).strip()] = (int(m.group(1)), int(m.group(2)))
        except OSError:
            pass
    return _levels


def danger_factor(x, y, level, map_id=None) -> float:
    """polestar Danger.lua: x3 in a zone whose mobs start ZONE_HARD levels over ours, x20 past ZONE_DEADLY."""
    zn = zone_name(x, y, map_id)
    lv = zone_levels().get(zn) if zn else None
    if not lv or level is None:
        return 1.0
    return danger_factor_of(lv, level)


def danger_factor_of(lv, level) -> float:
    over = lv[0] - level
    if over > ZONE_DEADLY:
        return 20.0
    if over >= ZONE_HARD:
        return 3.0
    return 1.0


ZONE_HARD = 4       # levels: a zone's lowest mobs this far over us cost x3
ZONE_DEADLY = 10    # ... past this x20
