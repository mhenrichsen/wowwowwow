"""Offline checks of the spawn-aware flee route and rest spot (python -m nb.test_spawn_safety).

ROWS: the real tcf_world rows (dangers._load(), 2026-10-15) hostile to dwarves within 190 yd of bot 2's 11:37:42 flee
start (-5416,-2733) and 110 yd of the rest spots of bot 3 11:38:20 (~(-5283,-2725)) and bot 4 11:10:10 (~(-5040,-2904),
it died at (-5037,-2902)): map, x, y, z, wander, entry, level lo, hi, rank, faction, detection.
"""

from __future__ import annotations

import math
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import threat                    # noqa: E402
from nb import hazards                   # noqa: E402
from nb import bot as botmod             # noqa: E402
from nb.bot import Bot                   # noqa: E402
from nb.eye import Plate                 # noqa: E402

LOGS: list[str] = []
botmod.log = lambda m: LOGS.append(m)

ROWS = [
    (0, -5579.17, -2784.51, 368.72, 3.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5579.17, -2784.51, 368.72, 3.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5552.6, -2750.48, 364.84, 3.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5552.6, -2750.48, 364.84, 3.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5544.19, -2730.56, 366.68, 0.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5544.19, -2730.56, 366.68, 0.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5542.12, -2721.15, 366.9, 0.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5542.12, -2721.15, 366.9, 0.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5537.03, -2742.93, 367.41, 3.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5537.03, -2742.93, 367.41, 3.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5536.03, -2781.71, 364.15, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5532.29, -2731.88, 367.35, 3.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5532.29, -2731.88, 367.35, 3.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5531.37, -2720.57, 367.3, 0.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5531.37, -2720.57, 367.3, 0.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5516.51, -2749.29, 363.09, 3.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5516.51, -2749.29, 363.09, 3.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5483.01, -2749.8, 364.29, 3.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5483.01, -2749.8, 364.29, 3.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5478.31, -2837.02, 354.13, 5.0, 1186, 11, 12, 0, 44, 20.0),
    (0, -5449.84, -2849.87, 346.1, 5.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5448.36, -2715.99, 366.0, 10.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5448.36, -2715.99, 366.0, 10.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5442.9, -2788.44, 363.53, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5417.78, -2816.71, 357.8, 5.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5416.86, -2750.04, 368.37, 3.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5416.86, -2750.04, 368.37, 3.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5409.46, -2725.87, 376.37, 5.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5409.46, -2725.87, 376.37, 5.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5395.74, -2713.9, 365.86, 0.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5395.74, -2713.9, 365.86, 0.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5389.64, -2708.84, 365.96, 0.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5389.64, -2708.84, 365.96, 0.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5389.54, -2715.18, 364.6, 0.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5389.54, -2715.18, 364.6, 0.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5388.97, -2726.34, 363.24, 3.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5388.97, -2726.34, 363.24, 3.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5385.77, -2782.78, 364.75, 10.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5385.77, -2782.78, 364.75, 10.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5385.39, -2784.51, 364.06, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5384.28, -2750.99, 364.88, 10.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5384.28, -2750.99, 364.88, 10.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5382.47, -2706.48, 366.02, 3.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5382.47, -2706.48, 366.02, 3.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5378.18, -2717.18, 362.74, 3.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5378.18, -2717.18, 362.74, 3.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5351.88, -2815.63, 347.67, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5348.99, -2733.52, 360.76, 5.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5348.99, -2733.52, 360.76, 5.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5316.57, -2751.5, 354.8, 5.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5316.57, -2751.5, 354.8, 5.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5316.55, -2834.93, 336.86, 2.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5312.15, -2774.34, 350.11, 5.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5312.15, -2774.34, 350.11, 5.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5308.79, -2821.55, 343.28, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5299.25, -2716.79, 343.5, 0.0, 1176, 11, 12, 0, 26, 20.0),
    (0, -5299.25, -2716.79, 343.5, 0.0, 1202, 11, 12, 0, 26, 20.0),
    (0, -5297.74, -2723.39, 342.74, 0.0, 1176, 11, 12, 0, 26, 20.0),
    (0, -5297.74, -2723.39, 342.74, 0.0, 1202, 11, 12, 0, 26, 20.0),
    (0, -5296.09, -2716.71, 342.86, 0.0, 1176, 11, 12, 0, 26, 20.0),
    (0, -5296.09, -2716.71, 342.86, 0.0, 1202, 11, 12, 0, 26, 20.0),
    (0, -5294.14, -2720.25, 342.61, 0.0, 1176, 11, 12, 0, 26, 20.0),
    (0, -5294.14, -2720.25, 342.61, 0.0, 1202, 11, 12, 0, 26, 20.0),
    (0, -5293.88, -2703.4, 344.14, 3.0, 1176, 11, 12, 0, 26, 20.0),
    (0, -5293.88, -2703.4, 344.14, 3.0, 1202, 11, 12, 0, 26, 20.0),
    (0, -5291.55, -2696.65, 344.72, 3.0, 1176, 11, 12, 0, 26, 20.0),
    (0, -5291.55, -2696.65, 344.72, 3.0, 1202, 11, 12, 0, 26, 20.0),
    (0, -5289.7, -2757.66, 352.13, 5.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5289.7, -2757.66, 352.13, 5.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5286.94, -2702.36, 343.69, 0.0, 1176, 11, 12, 0, 26, 20.0),
    (0, -5286.94, -2702.36, 343.69, 0.0, 1202, 11, 12, 0, 26, 20.0),
    (0, -5282.26, -2718.91, 342.5, 5.0, 1176, 11, 12, 0, 26, 20.0),
    (0, -5282.26, -2718.91, 342.5, 5.0, 1202, 11, 12, 0, 26, 20.0),
    (0, -5274.9, -2703.02, 343.61, 0.0, 1176, 11, 12, 0, 26, 20.0),
    (0, -5274.9, -2703.02, 343.61, 0.0, 1202, 11, 12, 0, 26, 20.0),
    (0, -5258.08, -2759.08, 350.75, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5250.52, -2731.25, 348.86, 0.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5250.52, -2731.25, 348.86, 0.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5250.22, -2748.44, 350.9, 5.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5250.22, -2748.44, 350.9, 5.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5228.57, -2804.31, 341.8, 10.0, 1186, 11, 12, 0, 44, 20.0),
    (0, -5228.34, -2719.61, 347.1, 10.0, 1186, 11, 12, 0, 44, 20.0),
    (0, -5182.02, -2692.73, 349.64, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5085.03, -2883.63, 325.57, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5078.11, -2986.15, 332.2, 10.0, 1186, 11, 12, 0, 44, 20.0),
    (0, -5060.23, -3002.1, 325.87, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5054.17, -3004.17, 325.96, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5051.03, -2851.03, 328.65, 5.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5050.67, -2898.73, 335.77, 10.0, 1195, 10, 11, 0, 22, 20.0),
    (0, -5040.57, -2916.17, 333.81, 10.0, 1186, 11, 12, 0, 44, 20.0),
    (0, -5035.18, -2935.55, 336.17, 10.0, 1186, 11, 12, 0, 44, 20.0),
    (0, -5034.82, -2889.04, 337.44, 10.0, 1186, 11, 12, 0, 44, 20.0),
    (0, -5000.78, -2974.06, 314.48, 1.0, 1174, 12, 13, 0, 26, 20.0),
    (0, -5000.78, -2974.06, 314.48, 1.0, 1175, 12, 13, 0, 26, 20.0),
    (0, -4988.9, -2958.24, 315.71, 2.0, 1177, 14, 14, 0, 26, 20.0),
    (0, -4985.51, -2948.12, 315.52, 5.0, 1425, 15, 15, 4, 26, 20.0),
    (0, -4985.32, -2986.91, 315.07, 1.0, 1174, 12, 13, 0, 26, 20.0),
    (0, -4985.32, -2986.91, 315.07, 1.0, 1175, 12, 13, 0, 26, 20.0),
    (0, -4984.43, -2941.66, 316.49, 0.0, 1174, 12, 13, 0, 26, 20.0),
    (0, -4984.43, -2941.66, 316.49, 0.0, 1175, 12, 13, 0, 26, 20.0),
    (0, -4979.96, -2943.12, 316.35, 2.0, 1174, 12, 13, 0, 26, 20.0),
    (0, -4979.96, -2943.12, 316.35, 2.0, 1175, 12, 13, 0, 26, 20.0),
    (0, -4979.77, -2982.26, 347.96, 3.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -4979.77, -2982.26, 347.96, 3.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -4971.39, -2956.08, 315.86, 2.0, 1177, 14, 14, 0, 26, 20.0),
]

B2_FLEE = (-5416.0, -2733.0)
B2_1186 = (-5478.31, -2837.02)          # the level 12 1186 that picked bot 2 up at the end of its 118 yd run
B3_REST = (-5283.0, -2725.0)
B4_REST = (-5040.0, -2904.0)


def plate(**kw):
    d = dict(sx=900, sy=500, npc=1176, level=12, hp=100, hostile=True, dead=False, on_me=False, is_target=False,
             tapped=False, combat=False, player=False, elite=False, friendly=False, token=1, ghash=7)
    d.update(kw)
    return Plate(**d)


def st(x, y, **kw):
    d = dict(level=12, hp_pct=100.0, mana_pct=100.0, mana=500, manamax=500, combat=False, plates=[], x=x, y=y,
             z=0.0, facing=0.0, dead=False, has_target=False, t_dead=False, t_on_me=False, actions=[], flags=0)
    d.update(kw)
    return SimpleNamespace(**d)


def make_bot(states, crumbs=()):
    b = Bot.__new__(Bot)
    seq = list(states)
    b.s = lambda: seq.pop(0) if len(seq) > 1 else seq[0]
    b.tm = threat.Model(threat.Spawns(rows=ROWS, hostile=lambda f: True))
    b.hazards = hazards.Hazards(persist=False)
    b.crumbs = list(crumbs)
    b.wanted, b.visited = set(), {}
    b.walk_rest, b.pull_anyway_t, b.last_multi, b.fight_maxatt = 0.0, 0.0, False, 0
    b.fight_costs = botmod.deque(maxlen=botmod.FIGHT_COSTS_N)
    b.W, b.cam_off = 1920, False
    b.keys = SimpleNamespace(press=lambda k, hold=0.04: None)
    b.cast = lambda name, wait_done=True: False
    b.has_item = lambda e, s: True
    b.slot_of = lambda name, s=None: None
    b.rot = SimpleNamespace(heal_next=0.0, bar=lambda s: {})
    went = []
    b.mv = SimpleNamespace(goto=lambda x, y, z=None, **kw: went.append((round(x), round(y))) or "ok",
                           stop=lambda: None)
    return b, went


def line(a, b, n):
    return [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]


def test_flee_route_avoids_the_spawn_that_killed_bot2():
    """bot 2 11:37:42: outmatched by a 1162 (a correct flee), ran 118 yd back the way it came and a level 12 1186 at
    the end of it took it. The trail here comes from the SW past that 1186; another heading enters fewer spawns."""
    x0, y0 = B2_FLEE
    crumbs = line((x0 - 62, y0 - 104), (x0, y0), 24)[:-1]          # walked in from the SW, oldest first
    b, went = make_bot([st(x0, y0, level=12)], crumbs)
    s = st(x0, y0)
    trail = b._trail_legs(x0, y0)
    t_risk, t_sp = b.route_risk(12, x0, y0, trail)
    assert any(1186 in p.entries for p in t_sp), t_sp           # the old choice runs into it
    legs, kind, why = b.flee_route(s, x0, y0)
    assert kind != "trail", (kind, why)
    risk, sp = b.route_risk(12, x0, y0, legs)
    assert risk < t_risk and not any(1186 in p.entries for p in sp), (kind, risk, t_risk, sp)
    # flee() runs that route
    LOGS.clear()
    b2, went2 = make_bot([st(x0, y0, combat=True, plates=[plate(on_me=True)])] * 3 + [st(x0, y0)], crumbs)
    assert b2.flee("outmatched: test") == "fled"
    assert any(m.startswith(f"flee: route {kind}") for m in LOGS), LOGS
    assert went2 and math.hypot(went2[0][0] - legs[0][0], went2[0][1] - legs[0][1]) < 1, (went2, legs)


def test_flee_route_prefers_the_trail_when_it_is_clear():
    """Out in the open with no spawn on the way back: the trail (bonus) wins over a straight line."""
    x0, y0 = -5600.0, -3300.0                                   # no rows within ~150 yd
    b, _ = make_bot([st(x0, y0)], line((x0 + 100, y0), (x0, y0), 20)[:-1])
    legs, kind, why = b.flee_route(st(x0, y0), x0, y0)
    assert kind == "trail", (kind, why)
    # a deadly area on the trail: not the trail any more
    b.hazards.add("death", x0 + 110, y0)
    b.hazards.add("death", x0 + 130, y0 + 10)
    legs, kind, why = b.flee_route(st(x0, y0), x0, y0)
    assert kind != "trail", (kind, why)


def rest_case(spot, crumbs, **kw):
    """rest() at `spot`: -> (went, LOGS). The state after the walk has an attacker, which ends rest() at once."""
    LOGS.clear()
    s0 = st(*spot, **kw)
    after = st(*spot, combat=True, plates=[plate(on_me=True)])
    b, went = make_bot([s0, s0, s0, after], crumbs)
    b.rest()
    return b, went


def test_rest_moves_off_bot3_kobold_camp():
    """bot 3 11:38:20: 'rest: hp 71% mana 15% hostile_plates=5 (standing ...)', a kobold spawn 6 yd away, aggroed
    3 s later and died on the chain. Rest walks to a spot no spawn's aggro covers first."""
    crumbs = line((-5292.0, -2785.0), (-5285.0, -2731.0), 11)    # it walked in from the south (route at 11:37:46)
    b, went = rest_case(B3_REST, crumbs, hp_pct=71.0, mana_pct=15.0, mana=75)
    assert len(b.tm.spot_risk(12, *B3_REST)) >= 5
    assert went, LOGS
    assert b.tm.spot_risk(12, *went[0]) == [], went
    assert 15 <= math.hypot(went[0][0] - B3_REST[0], went[0][1] - B3_REST[1]) <= 61
    assert any("spawn(s)' aggro reach this spot" in m and "moving" in m for m in LOGS), LOGS


def test_rest_moves_off_bot4_wanderers():
    """bot 4 11:10:10: 'rest: hp 92% mana 29% hostile_plates=4 (standing ...)' 12 yd from a 1195 and a level 12 1186
    (10 yd wanders); a fight 1 s later, dead at (-5037,-2902). It came from the east: back along the trail."""
    crumbs = line((-4927.0, -2880.0), (-5034.0, -2902.0), 22)
    plates = [plate(sx=500 + 100 * k, ghash=20 + k) for k in range(4)]        # 4 awake in view: a standing rest
    b, went = rest_case(B4_REST, crumbs, hp_pct=92.0, mana_pct=29.0, mana=145, plates=plates)
    assert went, LOGS
    assert b.tm.spot_risk(12, *went[0]) == [], went
    assert any("(trail" in m for m in LOGS), LOGS


def test_rest_stays_on_a_clear_spot():
    b, went = rest_case((-5600.0, -3300.0), (), hp_pct=60.0, mana_pct=15.0, mana=75)
    assert went == [], went
    assert not any("moving" in m for m in LOGS), LOGS


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
