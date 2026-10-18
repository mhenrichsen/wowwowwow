"""Offline checks of nb/hazards.py (python -m nb.test_hazards): kinds, TTLs, the shared file between bots, deadly areas.

The shared file lives in a temp dir: nothing here touches the live nb/hazards.json.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import hazards                   # noqa: E402
from nb.hazards import Hazards, ROUTE_KINDS, HUNT_KINDS   # noqa: E402

TMP = tempfile.mkdtemp(prefix="nb_hz_")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def store(inst, clock, name="hz.json", legacy=None):
    return Hazards(inst, path=os.path.join(TMP, name), persist=True, clock=clock,
                   legacy=legacy or os.path.join(TMP, "none.json"))


def test_kinds_radii_and_ttls():
    """The old bot.py values: death 60 / 30 min, flee 45 / 30 min, danger 50 / 20 min, skip 40 yd / 60 s, roam fail
    40 yd / 10 min (same behaviour after the move)."""
    c = Clock()
    h = Hazards(1, persist=False, clock=c)
    for k, xy in (("death", (0, 0)), ("flee", (500, 0)), ("danger", (1000, 0)), ("hunt_skip", (2000, 0)),
                  ("roam_fail", (3000, 0))):
        h.add(k, *xy)
    got = {x.kind: (x.r, x.until - c.t) for x in h.active()}
    assert got == {"death": (60, 1800), "flee": (45, 1800), "danger": (50, 1200), "hunt_skip": (40, 60),
                   "roam_fail": (40, 600)}, got
    assert h.blocked(10, 0, ROUTE_KINDS) and not h.blocked(10, 0, HUNT_KINDS)
    assert h.outside([(2030, 0), (2041, 0)], HUNT_KINDS) == [(2041, 0)]
    c.t += 61
    assert h.first_end(HUNT_KINDS) == c.t - 61 + 600              # the 60 s skip ran out, the roam fail did not
    c.t += 600
    assert h.first_end(HUNT_KINDS) is None
    assert [x.kind for x in h.active(ROUTE_KINDS)] == ["death", "flee", "danger"]


def test_cost_zones_and_bbox():
    c = Clock()
    h = Hazards(1, persist=False, clock=c)
    h.add("flee", 100, 100)
    h.add("hunt_skip", 100, 100)                                     # not a route cost
    z = h.cost_zones(z=5.0, extra=[(900, 900, 1.0, 30.0)])
    assert z == [(100.0, 100.0, 5.0, 45.0), (900, 900, 1.0, 30.0)], z
    assert h.cost_zones(bbox=(0, 0, 300, 300), extra=[(900, 900, 1.0, 30.0)]) == [(100.0, 100.0, 0.0, 45.0)]


def test_deaths_are_shared_and_survive_a_restart():
    c = Clock()
    a, b = store(1, c), store(2, c)
    a.add("death", 10, 20)
    a.add("flee", 500, 500)                                          # not persisted, not shared
    a.add("guard_fight", 700, 700, npc=1271)                         # persisted, not shared
    seen = b.active()
    assert [(x.kind, x.r) for x in seen] == [("death", 40.0)], seen  # the other bots: 40 yd
    rows = json.load(open(os.path.join(TMP, "hz.json")))
    assert sorted(r["k"] for r in rows) == ["death", "death_mark", "guard_fight"], rows
    b.add("death", 3000, 3000)                                       # b's write keeps a's rows
    assert sorted(r["i"] for r in json.load(open(os.path.join(TMP, "hz.json")))) == [1, 1, 1, 2, 2]
    a2 = store(1, c)                                                 # a restarts: its own persisted hazards are back
    assert sorted((x.kind, x.r) for x in a2.active(mine_only=True)) == [("death", 60.0), ("guard_fight", 30.0)]
    assert [x.r for x in a2.active(("death",)) if x.inst == 2] == [40.0]
    c.t += 1801
    assert store(3, c).active() == []                                 # (the 4 h marks alone show nothing)


def test_legacy_death_spots_are_read():
    """Bots not yet restarted onto this code write nb/death_spots.json ([x, y, t, inst])."""
    c = Clock()
    leg = os.path.join(TMP, "death_spots.json")
    json.dump([[1.0, 2.0, c.t - 100, 4], [5.0, 6.0, c.t - 100, 1], [9.0, 9.0, c.t - 4000, 3]], open(leg, "w"))
    h = store(1, c, name="hz2.json", legacy=leg)
    got = [(x.x, x.y, x.r, x.inst) for x in h.active(("death",))]
    assert got == [(1.0, 2.0, 40.0, 4)], got                          # not our own, not the expired one


def test_same_hazard_replaces():
    c = Clock()
    h = Hazards(1, persist=False, clock=c)
    for _ in range(5):
        h.add("guard_fight", 700, 700, npc=1271)                     # a note every 5 s of one elite fight
        c.t += 5
    h.add("guard_fight", 700, 700, npc=999)
    assert len(h.active(("guard_fight",))) == 2


def test_death_areas_grow_with_deaths():
    """inst3 died 5 times round (-5394,-2710)..(-5373,-2793) 10:05-11:30, the 40-60 yd circles far smaller than the camp.
    Deaths within AREA_GROUP_R of a group's densest one merge into one area that grows with the count; roam's
    ROAM_KINDS keep hunt points out of it, and at 3+ its centre is a (capped) route cost."""
    c = Clock()
    a, b = store(1, c, name="hz3.json"), store(2, c, name="hz3.json")
    a.add("death", -5394, -2710)
    assert a.active(("death_area",)) == []                           # one death: just its circle
    c.t += 300
    b.add("death", -5386, -2778)                                     # another bot, 68 yd off
    ar = a.active(("death_area",))
    assert len(ar) == 1 and ar[0].info["n"] == 2 and ar[0].r >= 90, ar
    r2 = ar[0].r
    for k, (x, y) in enumerate(((-5390, -2718), (-5359, -2764), (-5373, -2793))):
        c.t += 300
        (a if k % 2 else b).add("death", x, y)
    ar = a.active(("death_area",))
    assert len(ar) == 1 and ar[0].info["n"] == 5 and ar[0].r > r2 and ar[0].r <= hazards.AREA_MAX, ar
    pts = [(-5440, -2700), (-5600, -2550), (-5200, -2950)]
    assert a.outside(pts, hazards.ROAM_KINDS) == [(-5600, -2550), (-5200, -2950)], a.active(("death_area",))
    rt = a.active(("death_route",))
    assert len(rt) == 1 and rt[0].r <= hazards.AREA_ROUTE_MAX, rt
    assert not [z for z in a.cost_zones() if z[3] == ar[0].r]       # the whole area is never a route cost
    far = store(3, c, name="hz3.json")
    far.add("death", -4000, -1000)                                   # far away: its own circle, no area
    assert len(far.active(("death_area",))) == 1
    c.t += 1801                                                      # the 30 min death circles are gone ...
    ar = far.active(("death_area",))
    assert len(ar) == 1 and ar[0].info["n"] == 5, ar                  # ... the 5-death area lives on (5 x 30 min)
    c.t += 5 * 1800
    assert far.active(("death_area",)) == []


def test_a_chain_of_deaths_does_not_become_one_blob():
    """Deaths every ~70 yd along a 400 yd line: single-link would make one area over all of it; groups stay local."""
    c = Clock()
    h = store(1, c, name="hz5.json")
    for k in range(7):
        h.add("death", -5000 - 70 * k, -2800)
        c.t += 60
    ar = h.active(("death_area",))
    assert len(ar) >= 2 and all(a.info["n"] <= 3 for a in ar), ar


def test_deaths_far_apart_in_time_still_cluster():
    """bot 4 08:26 / 11:10 / 12:22 and bot 2 09:05 round (-5040,-2900): 30-75 min apart, the 30 min deaths never
    overlapped. The 4 h marks cluster them: an area, and at 3+ a route cost of at most AREA_ROUTE_MAX yd."""
    c = Clock()
    a, b = store(4, c, name="hz4.json"), store(2, c, name="hz4.json")
    a.add("death", -5039, -2905)
    c.t += 39 * 60
    b.add("death", -5045, -2898)
    c.t += 45 * 60
    assert a.active(("death",)) == []                                 # both 30 min deaths gone
    assert len(a.active(("death_area",))) == 1                        # the pair lives 2 x 30 min from the newest
    c.t += 30 * 60
    assert a.active(("death_area",)) == []
    assert a.active(("death_route",)) == []                           # 2 deaths: no route cost yet
    a.add("death", -5037, -2902)
    rt = a.active(("death_route",))
    assert len(rt) == 1 and rt[0].r <= hazards.AREA_ROUTE_MAX and rt[0].info["n"] == 3, rt
    assert any(abs(z[0] + 5040) < 5 for z in a.cost_zones())         # errands now pay to pass it


def test_bot_attr_is_memory_only():
    from nb.hazards import HazardsAttr

    class B:
        hz = HazardsAttr()
    b = B()
    assert b.hz.persist is False and b.hz.active() == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
