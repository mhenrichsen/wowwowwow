"""Where the roads are, as a hashed 10 yd grid -> O(1) "is this on a road?".

The navmesh prices water, steep ground and mob camps per step; roads are the
mirror image of that - a *discount*, so the search prefers them where they
exist and ignores them where they do not.

Source: the terrain texture layers in the client's terrain.MPQ. Every ADT
chunk that paints a road texture (ElwynnCobbleStoneBase, LochModanBrickRoadBase,
WetlandsRoad01, ...) contributes a sample at ~4 yd resolution. The points were
extracted offline into data/road_pts_<map>.json (61k on Eastern Kingdoms,
48k on Kalimdor).

It must be a discount on road steps, never a penalty on off-road ones. The two
rank routes identically, but a penalty inflates every step cost and so deflates
the weighted heuristic (1.25 -> 0.75), and the search explodes: a measured
15.8 s Menethil -> Ironforge became 25.0 s.

Known blind spots: Dun Morogh has no road texture at all (its snow road is
unpainted), and neither do Badlands and Searing Gorge. Those zones simply get
no road preference, which costs nothing - the search behaves as it always did.
"""

from __future__ import annotations

import json
import os
import pickle

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

CELL = 10.0          # yards per grid cell
DILATE = 1           # cells of dilation -> a road corridor ~10-20 yd wide
_OFF = 4096          # keeps packed keys positive
_STRIDE = 16384

_cache: dict[int, frozenset] = {}


def key(x: float, y: float) -> int:
    """Packed grid key for a WoW (x, y). Must match build()."""
    return (int(x // CELL) + _OFF) * _STRIDE + (int(y // CELL) + _OFF)


def build(points, dilate: int = DILATE) -> set:
    out = set()
    for p in points:
        gx = int(p[0] // CELL) + _OFF
        gy = int(p[1] // CELL) + _OFF
        for dx in range(-dilate, dilate + 1):
            for dy in range(-dilate, dilate + 1):
                out.add((gx + dx) * _STRIDE + (gy + dy))
    return out


def load(map_id: int) -> frozenset:
    """Road cells for a map, or an empty set when we have no data for it."""
    if map_id in _cache:
        return _cache[map_id]
    pkl = os.path.join(DATA, f"road_grid_{map_id:03d}.pkl")
    try:
        with open(pkl, "rb") as f:
            grid = pickle.load(f)
        _cache[map_id] = grid
        return grid
    except Exception:
        pass
    src = os.path.join(DATA, f"road_pts_{map_id}.json")
    if not os.path.exists(src):
        _cache[map_id] = frozenset()
        return _cache[map_id]
    with open(src, encoding="utf-8") as f:
        pts = json.load(f)
    grid = frozenset(build(pts))
    _cache[map_id] = grid
    try:
        with open(pkl, "wb") as f:
            pickle.dump(grid, f, protocol=4)
    except Exception:
        pass
    return grid


if __name__ == "__main__":
    import random
    import time
    for m in (0, 1):
        t = time.time()
        g = load(m)
        print(f"map {m}: {len(g)} road cells, loaded in {time.time() - t:.1f}s")
    g = load(0)
    R = random.Random(1)
    probe = [(R.uniform(-12000, 1000), R.uniform(-5000, 3000)) for _ in range(200000)]
    t = time.time()
    hits = sum(1 for x, y in probe if key(x, y) in g)
    dt = time.time() - t
    print(f"200k lookups in {dt:.2f}s ({dt / 200000 * 1e9:.0f} ns each), {hits} on road")
