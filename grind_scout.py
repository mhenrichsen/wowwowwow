"""Grind spots for a melee class: many easy targets, one pull at a time.

find_stage.py picks spots for the mage. A warrior has no root or sheep, so
what matters is different:

  targets    beasts (and yetis/wendigos): they never flee at low health,
             never cast, never call friends. Kill history (events.jsonl):
             Frostmane trolls cost 44-64% hp and 35-45 s each, beasts of the
             same level about half that.
  pairs      the share of targets with ANY other hostile spawn within 14 yd -
             every one of those is an add. All hostiles count, not just the
             targets: the Wendigo deaths were a second Wendigo, the leopard
             deaths a leopard walking by a bear.
  neighbours nothing elite and nothing 3+ levels above the band within 100 yd
  terrain    no water on the spot (the bot swims badly)

    python grind_scout.py [lo hi]        all bands, or one band
"""
from __future__ import annotations

import math
import sys

import dangers
import navmesh
import worlddb
from find_stage import dryness

ZONES = {
    "Dun Morogh": (-6700.0, -2200.0, -4900.0, 1100.0),
    "Loch Modan": (-6000.0, -4400.0, -4600.0, -2300.0),
    "Wetlands": (-4300.0, -3700.0, -2400.0, -600.0),
}
BANDS = [(1, 3), (3, 5), (5, 7), (7, 9), (9, 11), (11, 13), (13, 15), (15, 17), (17, 19), (19, 21)]
RADIUS = 80.0       # the area a stage grinds in
AGGRO = 14.0        # a same-level mob this close to the target joins


def all_spawns(box):
    rows = worlddb._query(f"""
        SELECT c.id, c.position_x, c.position_y, c.position_z, ct.name, ct.level_min, ct.level_max,
               ct.rank, ct.type, ct.faction
        FROM creature c JOIN creature_template ct ON ct.entry = c.id
        WHERE c.map = 0 AND c.position_x BETWEEN {box[0]} AND {box[2]}
          AND c.position_y BETWEEN {box[1]} AND {box[3]}
    """)
    out = []
    for r in rows:
        out.append(dict(entry=int(r[0]), x=float(r[1]), y=float(r[2]), z=float(r[3]), name=r[4],
                        lo=int(r[5]), hi=int(r[6]), rank=int(r[7]), type=int(r[8]), faction=int(r[9])))
    return out


def hostile(s):
    # "not friendly to a dwarf": factions.is_hostile() says the wolves and
    # boars of faction 32/189 are not hostile, yet they attack on sight
    import factions
    return not factions.is_friendly(3, s["faction"])


def is_target(s):
    # beasts; yetis and wendigos (humanoid in the db) fight like beasts.
    # Not crocolisks: they live in and walk into the loch
    if "Crocolisk" in s["name"]:
        return False
    return s["type"] == 1 or "Wendigo" in s["name"] or "Yeti" in s["name"]


def scout(lo, hi, want=3, nav=None):
    res = []
    bad = set(dangers.NEMESIS)
    for zone, box in ZONES.items():
        sp, seen_pt = [], set()
        for s in all_spawns(box):
            # pooled spawns list the same point several times (114 rows of
            # Small Crag Boar in Coldridge): a point with itself is no pair
            k = (round(s["x"] / 3), round(s["y"] / 3))
            if hostile(s) and s["type"] != 8 and k not in seen_pt:     # 8 = critter
                seen_pt.add(k)
                sp.append(s)
        tg = [s for s in sp if is_target(s) and s["rank"] == 0 and lo <= s["hi"] and s["lo"] <= hi]
        for c in tg:
            near_t = [s for s in tg if math.dist((c["x"], c["y"]), (s["x"], s["y"])) <= RADIUS]
            if len(near_t) < 6:
                continue
            near_all = [s for s in sp if math.dist((c["x"], c["y"]), (s["x"], s["y"])) <= RADIUS + 20]
            if any(s["rank"] > 0 or s["entry"] in bad or s["lo"] >= hi + 3 for s in near_all):
                continue
            # pairs: targets with another hostile (any kind, not far below) within AGGRO
            pairs = 0
            for s in near_t:
                if any(o is not s and o["hi"] >= lo - 1
                       and math.dist((o["x"], o["y"]), (s["x"], s["y"])) < (AGGRO if lo >= 5 else 8.0)
                       for o in near_all):
                    pairs += 1
            others = [s for s in near_all if not is_target(s) and s["hi"] >= lo - 1]
            pair_frac = pairs / len(near_t)
            singles = len(near_t) - pairs
            score = singles - 0.5 * len(others)
            res.append(dict(zone=zone, x=c["x"], y=c["y"], z=c["z"], n=len(near_t), singles=singles,
                            pair_frac=pair_frac, others=len(others), score=score,
                            mobs=sorted({f"{s['name']} {s['lo']}-{s['hi']}" for s in near_t}),
                            other_names=sorted({s["name"] for s in others})[:4],
                            entries=sorted({s["entry"] for s in near_t})))
    res.sort(key=lambda r: -r["score"])
    out, seen = [], []
    nav = nav or navmesh.NavMesh()
    for r in res:
        if any(math.dist((r["x"], r["y"]), p) < 200 for p in seen):
            continue
        dry, wet = dryness(nav, r["x"], r["y"], r["z"])
        if wet:
            continue
        seen.append((r["x"], r["y"]))
        out.append(r)
        if len(out) >= want:
            break
    return out


def show(lo, hi, nav):
    print(f"\nlevel {lo}-{hi}:")
    rs = scout(lo, hi, nav=nav)
    if not rs:
        print("  nothing clean")
    for r in rs:
        print(f"  {r['zone']:11} ({r['x']:.0f},{r['y']:.0f},{r['z']:.0f})  {r['n']} targets, "
              f"{r['singles']} pullable alone ({100 * (1 - r['pair_frac']):.0f}%), {r['others']} non-beasts near")
        print(f"      {', '.join(r['mobs'][:4])}" + (f"   | others: {', '.join(r['other_names'])}" if r['other_names'] else ""))


if __name__ == "__main__":
    nav = navmesh.NavMesh()
    if len(sys.argv) == 3:
        show(int(sys.argv[1]), int(sys.argv[2]), nav)
    else:
        for lo, hi in BANDS:
            show(lo, hi, nav)
