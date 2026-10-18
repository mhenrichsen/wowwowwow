"""Find a grind spot worth standing in, and prove it before we go there.

Every bad spot tonight failed one of four checks, so this runs all four:

  composition  what actually spawns within the leash, not what the zone is
               "known for" (a bear pair killed the mage at a spot picked for
               its wood lurkers)
  terrain      dry ground, no water polys - the bot wedges while swimming and
               spent 30 minutes in Lake Everstill earning nothing
  spacing      mobs far enough apart to pull one at a time; the Redridge
               gnoll camp had all 24 within 20 yd of each other
  neighbours   nothing elite, nothing far above our level, and nothing on the
               never-fight list nearby (Lord Malathrom, 31 elite, lives next
               to an otherwise perfect wolf spot)

Usage:  python find_stage.py <min_level> <max_level> <xmin> <ymin> <xmax> <ymax>
"""

from __future__ import annotations

import math
import statistics
import sys
from collections import Counter

import dangers
import navmesh
import worlddb

LEASH = 90.0            # the radius a stage actually grinds in
MIN_MOBS = 8            # fewer than this and we spend the time walking


def min_spacing(level: int) -> float:
    """How far apart mobs must be before a spot is worth grinding.

    A camp is only a problem when a second mob can kill us. The level-18
    mage died to pairs, so 8 yd was right there - but in Coldridge Valley
    every spot in the zone fails that rule, and a level-3 warrior pulling
    two level-3 boars is fine. Scale it with how much the mobs can hurt.
    """
    return 8.0 if level >= 12 else 4.0


def spawns(lo: int, hi: int, box) -> list[tuple]:
    rows = worlddb._query(f"""
        SELECT c.id, c.position_x, c.position_y, c.position_z, ct.name, ct.level_max, ct.rank
        FROM creature c JOIN creature_template ct ON ct.entry = c.id
        WHERE c.map = 0 AND ct.level_max BETWEEN {lo} AND {hi}
          AND c.position_x BETWEEN {box[0]} AND {box[2]}
          AND c.position_y BETWEEN {box[1]} AND {box[3]}
    """)
    return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), r[4],
             int(r[5]), int(r[6])) for r in rows]


def spacing(near) -> float:
    """Median distance to the nearest OTHER spawn point.

    Distinct spawn points, not rows: half the creature rows in a starter
    zone share coordinates with another, because a spawn pool lists several
    alternatives for one point and only one of them is up. Counting rows put
    a 0 yd neighbour next to almost every mob and scored every spot in
    Coldridge Valley as a camp.
    """
    pts = sorted({(round(p[1], 1), round(p[2], 1)) for p in near})
    nn = []
    for i, p in enumerate(pts):
        d = sorted(math.dist(p, q) for j, q in enumerate(pts) if i != j)
        if d:
            nn.append(d[0])
    return statistics.median(nn) if nn else 0.0


def dryness(nav, cx, cy, cz) -> tuple[int, int]:
    wet = dry = 0
    for k in range(24):
        a = k * math.pi / 12
        for r in (0, 30, 60, 90):
            key, _ = nav.nearest_poly(cx + r * math.cos(a), cy + r * math.sin(a), cz)
            if key is None:
                continue
            t = nav.tiles[key[0]]
            f = t.pflags[key[1]]
            if f & navmesh.NAV_WATER and not f & navmesh.NAV_GROUND:
                wet += 1
            else:
                dry += 1
    return dry, wet


def neighbours(cx, cy, level: int) -> list[str]:
    rows = worlddb._query(f"""
        SELECT ct.name, ct.level_max, ct.rank FROM creature c
        JOIN creature_template ct ON ct.entry = c.id
        WHERE c.map = 0 AND c.position_x BETWEEN {cx - 120} AND {cx + 120}
          AND c.position_y BETWEEN {cy - 120} AND {cy + 120}
          AND (ct.rank > 0 OR ct.level_max >= {level + 4})
    """)
    return sorted({f"{r[0]} ({r[1]}{'+elite' if r[2] != '0' else ''})" for r in rows})


def find(lo: int, hi: int, box, want=5):
    pts = spawns(lo, hi, box)
    print(f"{len(pts)} spawns of level {lo}-{hi} in the box")
    if not pts:
        return []
    bad = set(dangers.NEMESIS)
    nav = navmesh.NavMesh()
    cand = []
    for e, x, y, z, name, lvl, rank in pts:
        if e in bad or rank > 0:
            continue
        near = [q for q in pts if math.dist((x, y), (q[1], q[2])) <= LEASH]
        if len(near) < MIN_MOBS or any(q[0] in bad or q[6] > 0 for q in near):
            continue
        sp = spacing(near)
        avg_lvl = sum(q[5] for q in near) / len(near)
        if sp < min_spacing(avg_lvl):
            continue
        cand.append((len(near), sum(q[5] for q in near) / len(near), sp, x, y, z,
                     Counter(q[4] for q in near)))
    cand.sort(key=lambda r: (-r[0], -r[1]))
    out, seen = [], []
    for n, avg, sp, x, y, z, c in cand:
        if any(math.dist((x, y), (a, b)) < 150 for a, b in seen):
            continue
        dry, wet = dryness(nav, x, y, z)
        if wet:
            continue                        # any water at all disqualifies it
        nb = neighbours(x, y, int(avg))
        seen.append((x, y))
        out.append((x, y, z, n, avg, sp, c, dry, nb))
        print(f"  ({x:.0f},{y:.0f},{z:.0f}) {n} mobs avg lvl {avg:.1f} "
              f"spacing {sp:.0f} yd dry {dry}/{dry}")
        print(f"      {', '.join(f'{k}x{v}' for k, v in c.most_common(4))}")
        print(f"      neighbours: {nb[:3] if nb else 'clear'}")
        if len(out) >= want:
            break
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) != 6:
        print(__doc__)
        sys.exit(1)
    find(int(a[0]), int(a[1]), tuple(float(v) for v in a[2:6]))
