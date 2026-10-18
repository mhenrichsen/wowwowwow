"""Levelling routes: where to grind at which level, and where to sell/train.

A route is a JSON file in routes/ with a list of stages:

    {"name": "...", "min_level": 5, "max_level": 7,        # used while 5 <= level < 7
     "center": [x, y, z], "radius": 70,                  # leash area
     "mobs": [525, 113, ...]}                            # entries to hunt (empty = any)

Vendors, repairers and trainers are NOT part of a route: the bot looks up the
nearest suitable one at runtime in the global NPC database (services.py).

`python routes.py build` regenerates routes/human_mage_elwynn.json from the
world DB: for each stage it picks the densest cluster of that stage's mobs.
Edit the JSON freely afterwards; the bot only reads it.
"""

from __future__ import annotations

import json
import math
import os
import sys

import worlddb

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTES = os.path.join(HERE, "routes")

# Stage specs for a human mage in Elwynn Forest. Mob entries were chosen from
# the level/spawn survey of zone 12 (see README): non-elite, not critters,
# not the seasonal Scourge Invasion spawns.
HUMAN_MAGE_ELWYNN = [
    ("Northshire - wolves and kobolds", 1, 3, [299, 69, 6]),
    # A mage should out-level a zone's mobs by about one level: mobs a level
    # above take 20 s+ each and chain-aggro on the way in (two deaths at
    # level 5 among the level 6 wolves west of Goldshire).
    ("Northshire - vineyards", 3, 6, [38, 257, 80]),
    ("Elwynn - west of Goldshire", 6, 8, [525, 113, 94, 30], 110),
    ("Elwynn - south", 8, 10, [524, 1922, 476], 110),
    ("Elwynn - east", 10, 13, [118, 46, 732, 478], 110),
]


def spawns(entries) -> list[tuple[int, float, float, float]]:
    rows = worlddb._query(
        "SELECT id, position_x, position_y, position_z FROM creature WHERE map=0 AND id IN "
        f"({','.join(map(str, entries))})")
    return [(int(e), float(x), float(y), float(z)) for e, x, y, z in rows]


def densest(points, radius):
    """Centre of the spot with the most spawns within `radius`."""
    best, best_n = None, -1
    for _, x, y, _ in points:
        near = [(px, py, pz) for _, px, py, pz in points if math.hypot(px - x, py - y) <= radius]
        if len(near) > best_n:
            best_n = len(near)
            cx = sum(p[0] for p in near) / len(near)
            cy = sum(p[1] for p in near) / len(near)
            # z of the spawn closest to the centre (ground height there)
            cz = min(near, key=lambda p: math.hypot(p[0] - cx, p[1] - cy))[2]
            best = (round(cx, 1), round(cy, 1), round(cz, 1))
    return best, best_n


def build(stages=HUMAN_MAGE_ELWYNN, name="human_mage_elwynn", radius=70.0):
    out = {"name": "Human mage - Elwynn Forest 1-13", "stages": []}
    for title, lo, hi, mobs, *r in stages:
        stage_radius = float(r[0]) if r else radius
        pts = spawns(mobs)
        center, n = densest(pts, stage_radius * 0.8)
        out["stages"].append({
            "name": title, "min_level": lo, "max_level": hi,
            "center": list(center), "radius": stage_radius, "mobs": mobs,
            "spawns_in_area": n,
        })
        print(f"{lo:>2}-{hi:<2} {title:<34} centre {center}  {n:>3} spawns")
    os.makedirs(ROUTES, exist_ok=True)
    path = os.path.join(ROUTES, name + ".json")
    json.dump(out, open(path, "w", encoding="utf-8"), indent=2)
    print("wrote", path)
    return path


def load(name_or_path: str) -> dict:
    path = name_or_path if os.path.exists(name_or_path) else os.path.join(ROUTES, name_or_path + ".json")
    return json.load(open(path, encoding="utf-8"))


def stage_for(route: dict, level: int, pos=None) -> dict:
    """The stage for this level; with several (a main spot and a safe
    fallback), the one nearest to pos."""
    import math
    stages = route["stages"]
    fit = [s for s in stages if s["min_level"] <= level < s["max_level"]]
    if not fit:
        # No stage covers this level (nothing spans 19-23). The old fallback
        # was stages[0] - Northshire, levels 1-3 - so a level-19 character
        # would have crossed the map to grind starter wolves for no xp at
        # all. Fall back to the highest stage we have already outgrown
        # instead: too low still beats level 1-3, and it keeps us near where
        # we are.
        below = [s for s in stages if s["max_level"] <= level]
        if below:
            top = max(s["max_level"] for s in below)
            fit = [s for s in below if s["max_level"] == top]
    if fit:
        if pos is not None and len(fit) > 1:
            return min(fit, key=lambda s: math.dist(s["center"][:2], pos[:2]))
        return fit[0]
    return stages[-1] if level >= stages[-1]["max_level"] else stages[0]


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        build()
    else:
        print(__doc__)
