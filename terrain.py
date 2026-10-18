"""Terrain heights from the server's .map files (TrinityCore src/server/game/Maps/GridMap.cpp, MapDefines.h).

<data>/maps/<map:04>_<tx:02>_<ty:02>.map, tx = floor(32 - wx / 533.333), ty = floor(32 - wy / 533.333).
Header (11 u32): "MAPS", version 10, build, area off/size, height off/size, liquid off/size, holes off/size.
Height chunk: "MHGT", u32 flags (1 no height, 2 uint16, 4 uint8, 8 flight bounds), f gridHeight, f gridMaxHeight,
then V9 129x129 and V8 128x128 (float, or ints scaled (max - min) / 65535 or / 255 above gridHeight).
V9[i][j] lies at wx = (32 - tx - i / 128) * 533.333, wy = (32 - ty - j / 128) * 533.333; each square splits into four
triangles round its centre V8. Holes: 16 x 16 x 8 bytes, square (i, j) is a hole when
holes[(i // 8) * 128 + (j // 8) * 8 + i % 8] & (1 << (j % 8)).
"""
from __future__ import annotations

import math
import os
import struct

import numpy as np

MAP_DIR = r"D:\tcforever\server\data\maps"
TILE = 533.33333
_tiles: dict = {}


def _load(map_id, tx, ty):
    key = (map_id, tx, ty)
    if key in _tiles:
        return _tiles[key]
    r = None
    p = os.path.join(MAP_DIR, f"{map_id:04d}_{tx:02d}_{ty:02d}.map")
    if os.path.exists(p):
        with open(p, "rb") as f:
            data = f.read()
        h = struct.unpack_from("<4s10I", data, 0)
        if h[0] == b"MAPS":
            hoff, hsize, holes_off, holes_size = h[5], h[6], h[9], h[10]
            if hoff:
                magic, flags, gmin, gmax = struct.unpack_from("<4sIff", data, hoff)
                pos = hoff + 16
                if flags & 1:
                    v9 = np.full((129, 129), gmin, np.float32)
                    v8 = np.full((128, 128), gmin, np.float32)
                elif flags & 2:
                    m = (gmax - gmin) / 65535
                    v9 = np.frombuffer(data, "<u2", 129 * 129, pos).reshape(129, 129) * m + gmin
                    v8 = np.frombuffer(data, "<u2", 128 * 128, pos + 2 * 129 * 129).reshape(128, 128) * m + gmin
                elif flags & 4:
                    m = (gmax - gmin) / 255
                    v9 = np.frombuffer(data, "<u1", 129 * 129, pos).reshape(129, 129) * m + gmin
                    v8 = np.frombuffer(data, "<u1", 128 * 128, pos + 129 * 129).reshape(128, 128) * m + gmin
                else:
                    v9 = np.frombuffer(data, "<f4", 129 * 129, pos).reshape(129, 129)
                    v8 = np.frombuffer(data, "<f4", 128 * 128, pos + 4 * 129 * 129).reshape(128, 128)
                holes = None
                if holes_off and holes_size:
                    hb = np.frombuffer(data, "u1", 16 * 16 * 8, holes_off).reshape(16, 16, 8)
                    ii, jj = np.meshgrid(np.arange(128), np.arange(128), indexing="ij")
                    holes = (hb[ii // 8, jj // 8, ii % 8] >> (jj % 8)) & 1 > 0
                r = (np.asarray(v9, np.float64), np.asarray(v8, np.float64), holes)
    _tiles[key] = r
    return r


def heights(wx, wy, map_id=None):
    """Terrain height at arrays of WoW points (nan off the map or in a terrain hole)."""
    if map_id is None:
        import curmap
        map_id = curmap.MAP
    wx, wy = np.asarray(wx, float), np.asarray(wy, float)
    gx = 128 * (32 - wx / TILE)
    gy = 128 * (32 - wy / TILE)
    out = np.full(wx.shape, np.nan)
    tx, ty = np.floor(gx / 128).astype(int), np.floor(gy / 128).astype(int)
    for a, b in set(zip(tx.ravel().tolist(), ty.ravel().tolist())):
        t = _load(map_id, a, b)
        if t is None:
            continue
        v9, v8, holes = t
        m = (tx == a) & (ty == b)
        x, y = gx[m] - a * 128, gy[m] - b * 128
        xi, yi = np.clip(np.floor(x).astype(int), 0, 127), np.clip(np.floor(y).astype(int), 0, 127)
        fx, fy = x - xi, y - yi
        h1, h2 = v9[xi, yi], v9[xi + 1, yi]
        h3, h4 = v9[xi, yi + 1], v9[xi + 1, yi + 1]
        h5 = v8[xi, yi]
        # GridMap::getHeightFromFloat's four triangles round the centre (corners h1 (0,0) h2 (1,0) h3 (0,1) h4 (1,1))
        t1 = _plane(0, 0, h1, 1, 0, h2, 0.5, 0.5, h5, fx, fy)
        t2 = _plane(0, 0, h1, 0, 1, h3, 0.5, 0.5, h5, fx, fy)
        t3 = _plane(1, 0, h2, 1, 1, h4, 0.5, 0.5, h5, fx, fy)
        t4 = _plane(0, 1, h3, 1, 1, h4, 0.5, 0.5, h5, fx, fy)
        z = np.where(fx + fy < 1, np.where(fx > fy, t1, t2), np.where(fx > fy, t3, t4))
        if holes is not None:
            z = np.where(holes[xi, yi], np.nan, z)
        out[m] = z
    return out


def grade(wx, wy, map_id=None):
    """Rise per yd of the terrain triangle under arrays of WoW points (its gradient's length; nan off the map / in a
    hole). The triangle the client stands on, not a smoothed slope: a 21 yd mesh polygon at 0.67 can hold a 1.24
    face (the south road, -6285,179)."""
    if map_id is None:
        import curmap
        map_id = curmap.MAP
    wx, wy = np.asarray(wx, float), np.asarray(wy, float)
    gx = 128 * (32 - wx / TILE)
    gy = 128 * (32 - wy / TILE)
    out = np.full(wx.shape, np.nan)
    tx, ty = np.floor(gx / 128).astype(int), np.floor(gy / 128).astype(int)
    unit = TILE / 128                                     # yd per grid square
    for a, b in set(zip(tx.ravel().tolist(), ty.ravel().tolist())):
        tl = _load(map_id, a, b)
        if tl is None:
            continue
        v9, v8, holes = tl
        m = (tx == a) & (ty == b)
        x, y = gx[m] - a * 128, gy[m] - b * 128
        xi, yi = np.clip(np.floor(x).astype(int), 0, 127), np.clip(np.floor(y).astype(int), 0, 127)
        fx, fy = x - xi, y - yi
        h1, h2 = v9[xi, yi], v9[xi + 1, yi]
        h3, h4 = v9[xi, yi + 1], v9[xi + 1, yi + 1]
        h5 = v8[xi, yi]
        g1 = _slope(0, 0, h1, 1, 0, h2, 0.5, 0.5, h5)
        g2 = _slope(0, 0, h1, 0, 1, h3, 0.5, 0.5, h5)
        g3 = _slope(1, 0, h2, 1, 1, h4, 0.5, 0.5, h5)
        g4 = _slope(0, 1, h3, 1, 1, h4, 0.5, 0.5, h5)
        g = np.where(fx + fy < 1, np.where(fx > fy, g1, g2), np.where(fx > fy, g3, g4)) / unit
        if holes is not None:
            g = np.where(holes[xi, yi], np.nan, g)
        out[m] = g
    return out


def _slope(x1, y1, z1, x2, y2, z2, x3, y3, z3):
    """Gradient length (rise per grid unit) of the plane through three points (vectorised over z)."""
    ux, uy, uz = x2 - x1, y2 - y1, z2 - z1
    vx, vy, vz = x3 - x1, y3 - y1, z3 - z1
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    return np.hypot(nx, ny) / np.abs(nz)


def _plane(x1, y1, z1, x2, y2, z2, x3, y3, z3, x, y):
    """z on the plane through three points (vectorised over z and x, y)."""
    ux, uy, uz = x2 - x1, y2 - y1, z2 - z1
    vx, vy, vz = x3 - x1, y3 - y1, z3 - z1
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    return z1 - (nx * (x - x1) + ny * (y - y1)) / nz


_liq: dict = {}


def _load_liquid(map_id, tx, ty):
    """(entry 16x16, flags 16x16, level 129x129 or None, base level) of a tile's MLIQ chunk, or None
    (GridMap::loadLiquidData: the heights cover width x height vertices from (offsetX, offsetY))."""
    key = (map_id, tx, ty)
    if key in _liq:
        return _liq[key]
    r = None
    p = os.path.join(MAP_DIR, f"{map_id:04d}_{tx:02d}_{ty:02d}.map")
    if os.path.exists(p):
        with open(p, "rb") as f:
            data = f.read()
        h = struct.unpack_from("<4s10I", data, 0)
        lo = h[7] if h[0] == b"MAPS" else 0
        if lo:
            _, fl, lflags, ltype, ox, oy, w, hh, lvl = struct.unpack_from("<4sBBHBBBBf", data, lo)
            pos = lo + 16
            if fl & 1:                                          # MAP_LIQUID_NO_TYPE: one type for the tile
                ent = np.full((16, 16), ltype, np.int32)
                flg = np.full((16, 16), lflags, np.int32)
            else:
                ent = np.frombuffer(data, "<u2", 256, pos).reshape(16, 16).astype(np.int32)
                flg = np.frombuffer(data, "u1", 256, pos + 512).reshape(16, 16).astype(np.int32)
                pos += 768
            lev = None
            if not fl & 2:                                      # heights present
                lev = np.full((129, 129), np.nan)
                lev[ox:ox + w, oy:oy + hh] = np.frombuffer(data, "<f4", w * hh, pos).reshape(w, hh)
            r = (ent, flg, lev, float(lvl))
    _liq[key] = r
    return r


def liquid(wx, wy, map_id=None):
    """(entry, level) arrays at WoW points: the liquid type id (0 = none) and its surface height (nan where none)."""
    if map_id is None:
        import curmap
        map_id = curmap.MAP
    wx, wy = np.asarray(wx, float), np.asarray(wy, float)
    gx = 128 * (32 - wx / TILE)
    gy = 128 * (32 - wy / TILE)
    ent = np.zeros(wx.shape, np.int32)
    lev = np.full(wx.shape, np.nan)
    tx, ty = np.floor(gx / 128).astype(int), np.floor(gy / 128).astype(int)
    for a, b in set(zip(tx.ravel().tolist(), ty.ravel().tolist())):
        t = _load_liquid(map_id, a, b)
        if t is None:
            continue
        e, f, lv, base = t
        m = (tx == a) & (ty == b)
        x, y = gx[m] - a * 128, gy[m] - b * 128
        xi, yi = np.clip(np.floor(x).astype(int), 0, 127), np.clip(np.floor(y).astype(int), 0, 127)
        has = f[xi // 8, yi // 8] != 0
        ent[m] = np.where(has, e[xi // 8, yi // 8], 0)
        z = lv[xi, yi] if lv is not None else np.full(xi.shape, base)
        lev[m] = np.where(has, z, np.nan)
    return ent, lev
