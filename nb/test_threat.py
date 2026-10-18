"""Offline checks of nb/threat.py: pull / fight estimates against the deaths of 2026-10-15 (python -m nb.test_threat).

The spawn rows are the real tcf_world rows (dangers._load(), 2026-10-15) within 90 yd of each death site, so the test
needs no DB: map, x, y, z, wander, entry, level lo, hi, rank, faction, detection.
"""

from __future__ import annotations

import math
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import threat                    # noqa: E402
from nb import economy                   # noqa: E402
from nb import hazards                   # noqa: E402
from nb import bot as botmod             # noqa: E402
from nb.bot import Bot                   # noqa: E402
from nb.eye import Plate                 # noqa: E402

LOGS: list[str] = []
botmod.log = lambda m: LOGS.append(m)

HOSTILE_FACTIONS = {26, 59, 44, 22}
ROWS = [
    (0, -5900.21, -1679.66, 406.61, 0.0, 1127, 7, 8, 0, 189, 20.0),
    (0, -5891.4, -1688.04, 403.99, 0.0, 1689, 9, 10, 0, 189, 20.0),
    (0, -5840.64, -1590.02, 362.18, 0.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5837.93, -1580.96, 362.36, 0.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5825.71, -1593.61, 361.92, 0.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5823.92, -1662.05, 359.27, 5.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5810.3, -1670.7, 359.04, 5.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5808.03, -1623.19, 359.49, 5.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5799.46, -1666.03, 359.0, 5.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5793.42, -1670.55, 359.05, 5.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5791.92, -1615.96, 359.76, 5.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5790.45, -1643.81, 359.06, 5.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5772.32, -1634.92, 359.06, 5.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5753.28, -1657.42, 359.0, 5.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5752.23, -1669.72, 358.91, 5.0, 1115, 8, 9, 0, 59, 20.0),
    (0, -5752.23, -1669.72, 358.91, 5.0, 1117, 9, 10, 0, 59, 20.0),
    (0, -5751.1, -1628.34, 359.03, 0.0, 1115, 8, 9, 0, 59, 20.0),
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
    (0, -5448.36, -2715.99, 366.0, 10.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5448.36, -2715.99, 366.0, 10.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5442.9, -2788.44, 363.53, 10.0, 1195, 10, 11, 0, 22, 20.0),
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
    (0, -5348.99, -2733.52, 360.76, 5.0, 1161, 11, 12, 0, 59, 20.0),
    (0, -5348.99, -2733.52, 360.76, 5.0, 1162, 11, 12, 0, 59, 20.0),
    (0, -5316.57, -2751.5, 354.8, 5.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5316.57, -2751.5, 354.8, 5.0, 1173, 10, 11, 0, 26, 20.0),
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
    (0, -5228.34, -2719.61, 347.1, 10.0, 1186, 11, 12, 0, 44, 20.0),
    (0, -5039.53, -2674.9, 319.4, 0.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5039.53, -2674.9, 319.4, 0.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5034.79, -2685.5, 319.39, 3.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5034.79, -2685.5, 319.39, 3.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5033.7, -2674.35, 319.35, 0.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5033.7, -2674.35, 319.35, 0.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5032.69, -2680.43, 319.47, 0.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5032.69, -2680.43, 319.47, 0.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5016.86, -2616.37, 331.45, 5.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5016.86, -2616.37, 331.45, 5.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5016.45, -2683.57, 323.99, 5.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5016.45, -2683.57, 323.99, 5.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -5012.29, -2637.54, 326.4, 5.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -5012.29, -2637.54, 326.4, 5.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -4984.76, -2714.89, 326.7, 5.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -4984.76, -2714.89, 326.7, 5.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -4967.21, -2646.91, 331.66, 2.0, 1172, 10, 11, 0, 26, 20.0),
    (0, -4967.21, -2646.91, 331.66, 2.0, 1173, 10, 11, 0, 26, 20.0),
    (0, -4954.16, -2649.74, 330.38, 5.0, 1186, 11, 12, 0, 44, 20.0),
    (0, -4881.68, -2663.33, 334.61, 5.0, 1190, 10, 11, 0, 189, 20.0),
]


def model():
    return threat.Model(threat.Spawns(rows=ROWS, hostile=lambda f: f in HOSTILE_FACTIONS))


def pull(m, pl, me, tgt, npc, tl, hp=100.0, mana=100.0, plates=()):
    ang = math.atan2(tgt[1] - me[1], tgt[0] - me[0])
    return m.pull(pl, me[0], me[1], ang, npc, tl, hp, mana, 0.0, plates=plates)


# ------------------------------------------------------------ levels
def test_gray_formula():
    assert [threat.gray_level(v) for v in (1, 5, 6, 9, 10, 12, 20, 39, 40, 59, 60)] == \
        [0, 0, 1, 4, 4, 6, 13, 31, 31, 47, 51]
    assert threat.is_gray(12, 6) and not threat.is_gray(12, 7) and not threat.is_gray(5, 1)
    for pl in range(6, 60):                      # one formula: economy.xp_for gives 0 exactly on the gray levels
        g = threat.gray_level(pl)
        assert economy.xp_for(pl, g) == 0 and economy.xp_for(pl, g + 1) > 0, pl
    assert threat.low(12, 9) and not threat.low(12, 10)       # the bot's 'not worth a stop' band is not gray
    assert threat.aggro_radius(12, 12) == 20 and threat.aggro_radius(12, 1) == 9 and threat.aggro_radius(1, 60) == 45


def test_pool_alternatives_merge():
    """dangers._load() lists both spawns of a pool (1161 main + 1162 alternative at one spot): one point here."""
    sp = model().sp
    pts = [p for d, p in sp.near(-5395.74, -2713.9, 0.5, live=False)]
    assert len(pts) == 1 and pts[0].entries == {1161, 1162}, pts
    assert sum(1 for r in ROWS if abs(r[1] + 5395.74) < 0.01 and abs(r[2] + 2713.9) < 0.01) == 2


# ------------------------------------------------------------ pulls
def test_kobold_camp_1026():
    """inst3 10:26: Tunnel Rat Kobold 1202 lvl 11 (pooled with 1176) pulled with crowd=1 on screen; 4 on us in 8 s."""
    e = pull(model(), 12, (-5318, -2752), (-5296, -2717), 1202, 11)
    assert e.verdict == "skip" and e.est >= 4 and e.src == "spawn", e
    assert 1202 in e.target.entries


def test_bear_beside_the_target_1006():
    """inst4 10:06: pulled at 100/100 with crowd=1; the Elder Black Bear (another faction, 13 yd from the target)
    had our spot in its aggro - a pair at our level is never pulled at level 10+."""
    e = pull(model(), 11, (-4945, -2648), (-4967, -2647), 1173, 10)
    assert e.verdict == "skip" and e.est == 2, e
    assert [w for _p, _f, w in e.joiners] == ["aggro"], e.joiners


def test_tunnel_rat_camp_1104():
    """inst2 11:04:46: pulled npc 1173 (Tunnel Rat, lvl 10) at level 12 with crowd=0 and nothing else on the plates;
    bolt=0 at the pick, bolted from (-5002,-2684) - 31 yd from the 4-spawn rat camp at (-5033,-2680); 4 on us by
    11:04:55, the flee covered 35 yd, dead at 11:05:05. The spawn table rates it a pack; a lone rat is a pull."""
    m = model()
    e = pull(m, 12, (-4990, -2686), (-5033.7, -2674.35), 1173, 10)
    assert e.verdict == "skip" and e.est >= 3 and e.src == "spawn", e
    assert sum(1 for _p, f, w in e.joiners if w == "assist") >= 2, e.joiners
    lone = pull(m, 12, (-5016, -2590), (-5016.86, -2616.37), 1173, 10)
    assert lone.verdict == "pull" and lone.est == 1, lone


def test_stonesplinter_camp_vs_lone():
    m = model()
    camp = pull(m, 12, (-5420, -2700), (-5396, -2714), 1161, 11)
    assert camp.verdict == "skip" and camp.est >= 3, camp
    lone = pull(m, 12, (-5448, -2690), (-5448, -2716), 1161, 12)
    assert lone.verdict == "pull" and lone.est == 1, lone
    low = pull(m, 12, (-5448, -2690), (-5448, -2716), 1161, 12, mana=40)   # under the pull line: not even a lone one
    assert low.verdict == "skip", low


def test_dead_spawns_do_not_count():
    """economy's respawn table: a camp whose members are dead is a lone pull (alive() filters joiners)."""
    sp = threat.Spawns(rows=ROWS, hostile=lambda f: f in HOSTILE_FACTIONS)
    tgt = (-5395.74, -2713.9)
    sp.alive = lambda p: (round(p.x, 2), round(p.y, 2)) == tgt or p.y < -2740    # only the target (+ far ones) up
    e = pull(threat.Model(sp), 12, (-5420, -2700), tgt, 1161, 11)
    assert e.verdict == "pull" and e.est == 1, e


def test_plates_only_and_low_levels():
    """No bearing (camera swung off the heading): the plates decide. One awake level-12 beside it at level 12 is a
    pair (inst4 10:06 had crowd=1). At level 4 (a fight ~35% mana) the even pair is just over the line with the pack
    factor (3.00 vs 2.52), and a level 4 with one level 2 beside it is a pull."""
    m = threat.Model(threat.Spawns(rows=[], hostile=lambda f: True))
    e = m.pull(12, 0, 0, None, 1, 12, 100, 100, 0.0, plates=[(12, False, False)])
    assert e.verdict == "skip" and e.est == 2 and e.src == "plates", e
    e = m.pull(4, 0, 0, None, 1, 4, 100, 100, 0.0, plates=[(4, False, False)])
    assert e.verdict == "skip" and abs(e.weight - 3.0) < 1e-6, e
    e = m.pull(4, 0, 0, None, 1, 2, 100, 100, 0.0, plates=[(2, False, False)])
    assert e.verdict == "pull", e
    e = m.pull(12, 0, 0, None, 1, 12, 100, 100, 0.0, plates=[(5, False, True)])   # a gray one
    assert e.verdict == "pull", e


def test_plate_geometry_scales_with_focal():
    """beside(): bearing + depth in FOCAL units, not raw px (450 px was 0.5 rad at 1080 px, 0.82 rad at 657 px)."""
    W = 1168
    focal = 900.0 * 657 / 1080
    p = SimpleNamespace(sx=600, sy=300, ghash=1)
    near = SimpleNamespace(sx=600 + int(0.25 * focal), sy=320, ghash=2)
    far_side = SimpleNamespace(sx=600 + 400, sy=300, ghash=3)          # < 450 px, but 0.6 rad off
    deeper = SimpleNamespace(sx=610, sy=300 - int(0.3 * focal), ghash=4)
    assert threat.beside(p, near, W, focal)
    assert not threat.beside(p, far_side, W, focal) and not threat.beside(p, deeper, W, focal)


def test_loot_walk_into_the_camp():
    """inst3 10:12 / 10:20: 19-22 yd walks to a Stonesplinter corpse came back with the next camp member on us."""
    m = model()
    for me, corpse in (((-5545, -2745), (-5532, -2732)), ((-5570, -2770), (-5553, -2750))):
        assert m.loot_risk(12, me[0], me[1], corpse[0], corpse[1], 1162), (me, corpse)
    assert not m.loot_risk(12, -5470, -2650, -5470, -2670, 1161)     # out in the open: nothing to walk into


def test_flee_help():
    """inst3 10:05: the target fled at 14% into its friends (5 on us). A camp target has friends to run to."""
    m = model()
    camp = pull(m, 12, (-5420, -2700), (-5396, -2714), 1161, 11)
    assert m.flee_help(camp.target) >= 1
    lone = pull(m, 12, (-5448, -2690), (-5448, -2716), 1161, 12)
    assert m.flee_help(lone.target) == 0


# ------------------------------------------------------------ in the fight
def test_hopeless_pack_is_fled_whatever_the_run_costs():
    m1 = 0.0
    # bot 4 21:30:52: 44% hp at 6%/s, a 37% and a full mob at 2.5%/s: ~7 s to live, ~55 s to kill - run at 44%
    race = threat.fresh_add(15, 44, 30, [(14, 37, False, False), (14, 100, False, False)], 0.0,
                            heal_ok=True, inc=6.0, out=2.5)
    assert race.startswith("losing the race"), race
    # winning it: the same pair at 1%/s in, 5%/s out - fight
    assert not threat.fresh_add(15, 80, 60, [(14, 37, False, False), (14, 100, False, False)],
                                0.0, heal_ok=True, inc=1.0, out=5.0).startswith("losing")
    # bot 3 21:03:31: 3 on us, 40% hp, 84% mana, heal affordable, losing 12%/s: ratio 2.5 - run (3 s left)
    fast = threat.fresh_add(14, 40, 84, [(13, 60, False, False), (13, 100, False, False), (12, 100, False, False)],
                            m1, heal_ok=True, inc=12.0)
    assert fast.startswith(("dying fast", "hopeless")), fast
    # bot 2 19:39:50: 2 on us need 1.46 vs capacity 0.50 at 13% mana, no heal affordable: 2.9x, under 3 - run now
    dry = threat.fresh_add(14, 71, 13, [(13, 100, False, False), (12, 60, False, False)], m1, heal_ok=False)
    assert dry.startswith("hopeless"), dry
    # bot 2 19:08:25: "3 on us need 3.82 vs capacity 0.18 (66% hp 1% mana)" stayed in (the run cost veto), dead
    why = threat.fresh_add(14, 66, 1, [(13, 70, False, False), (13, 100, False, False), (12, 100, False, False)], m1)
    assert why.startswith("hopeless"), why
    # a near-even pair still fights
    assert threat.fresh_add(14, 90, 90, [(14, 100, False, False), (12, 60, False, False)], m1, heal_ok=True) == "" or         not threat.fresh_add(14, 90, 90, [(14, 100, False, False), (12, 60, False, False)], m1).startswith("hopeless")


def test_fresh_add_at_low_resources():
    m1 = 0.0
    # inst3 10:40: kill at 44% hp / 20% mana, a fresh level 11 already on us -> flee (it fought: dead in 12 s)
    # (FLEE_DPS_W 3.5: at 44% hp the run itself costs ~39% - fight; bot 4 13:29:41 fled at 56% and lost 56% in 16 s)
    assert threat.fresh_add(12, 44, 20, [(11, 100, False, False)], m1) == ""
    assert threat.fresh_add(12, 70, 20, [(11, 100, False, False)], m1).startswith("outmatched")
    assert threat.fresh_add(12, 56, 16, [(12, 100, False, False)], m1, inc=1.0) == ""     # bot 4 13:29:41
    # inst4 10:05:53: 47% hp / 27% mana, a fresh level 10 at level 11
    assert threat.fresh_add(11, 47, 27, [(10, 100, False, False)], 0.0) == ""   # the run: ~39%
    assert threat.fresh_add(11, 70, 27, [(10, 100, False, False)], 0.0)
    assert threat.fresh_add(12, 100, 90, [(12, 100, False, False)], m1) == ""     # a normal pull: fight
    assert threat.fresh_add(12, 44, 20, [(12, 40, False, False)], m1) == ""       # invested: doomed() judges
    assert threat.fresh_add(12, 44, 20, [(5, 100, False, True)], m1) == ""        # a gray one nibbling
    # doomed()'s 'fight it out' zone: 2+ on us under 70% hp with a heal affordable
    # ... when near even (need 1.5 x 1.5 pack = 2.25 vs capacity 0.60 is not: hopeless, run - staying at 3.75x died
    # every time, bot 2 19:08-19:10; 1 of 7 such runs got away on 2026-10-14)
    assert threat.fresh_add(12, 60, 30, [(12, 100, False, False), (12, 50, False, False)], m1,
                            heal_ok=True).startswith("hopeless")
    assert threat.fresh_add(12, 65, 60, [(12, 100, False, False), (11, 50, False, False)], m1, heal_ok=True) == ""


def test_pack_need_scales_with_attackers():
    one = threat.pack_need(14, [(14, 100, False, False)])
    two = threat.pack_need(14, [(14, 100, False, False)] * 2)
    assert abs(two - 2 * one * 1.5) < 1e-9, (one, two)        # 2 at once cost more than 2 apart
    gray = threat.pack_need(14, [(14, 100, False, False), (5, 100, False, True)])
    assert gray < two                                          # a gray one adds no pack factor


def test_travel_and_doomed_share_rules():
    def att(*lv):
        return [(v, threat.is_gray(12, v)) for v in lv]
    assert not threat.travel_fight(12, 80, att(8))            # low: outrun
    assert not threat.travel_fight(12, 80, att(5, 5, 6))      # 3 gray: outrun too (the old code stopped at any 3)
    assert threat.travel_fight(12, 80, att(12))               # a real one: stop (fight, or fight() flees)
    assert threat.travel_fight(14, 100, att(10, 10, 10))       # 3 low, not gray: fought at full hp (bot 2 18:38)
    assert threat.travel_fight(14, 100, att(10, 10))
    assert threat.travel_fight(12, 50, att(5))                # hurt: whatever it is
    assert not threat.travel_fight(12, 50, [])
    assert threat.pack_flee(3, 90, 80) and not threat.pack_flee(3, 80, 80) and not threat.pack_flee(2, 100, 100)
    assert threat.level_flee(9, 12, 60) and threat.level_flee(9, 9, 60, elite=True) and not threat.level_flee(9, 11, 60)


# ------------------------------------------------------------ bot side
def plate(**kw):
    d = dict(sx=900, sy=500, npc=1190, level=12, hp=100, hostile=True, dead=False, on_me=False, is_target=False,
             tapped=False, combat=False, player=False, elite=False, friendly=False, token=1, ghash=7)
    d.update(kw)
    return Plate(**d)


def st(**kw):
    d = dict(level=12, hp_pct=100.0, mana_pct=100.0, mana=500, manamax=500, combat=False, plates=[], x=0.0, y=0.0,
             z=0.0, facing=0.0, dead=False, has_target=False, t_dead=False, t_on_me=False, t_hash=0, t_level=0,
             t_hp_pct=0.0, t_trivial=False, t_within10=False, actions=[], flags=0)
    d.update(kw)
    return SimpleNamespace(**d)


def make_bot(states):
    b = Bot.__new__(Bot)
    seq = list(states)
    b.s = lambda: seq.pop(0) if len(seq) > 1 else seq[0]
    b.wanted, b.grind_ok, b.blacklist, b.visited = set(), True, {}, {}
    b.walk_rest, b.pull_anyway_t, b.last_multi, b.fight_maxatt = 0.0, 0.0, False, 0
    b.fight_costs, b._line_log = botmod.deque(maxlen=botmod.FIGHT_COSTS_N), None
    b.W, b.cam_off = 1920, False
    b.tm = threat.Model(threat.Spawns(rows=[], hostile=lambda f: True))
    b.hazards = hazards.Hazards(persist=False)
    b.keys = SimpleNamespace(press=lambda k, hold=0.04: None)
    b.cast = lambda name, wait_done=True: False
    b.has_item = lambda e, s: False
    b.rot = SimpleNamespace(heal_next=0.0, bar=lambda s: {})
    return b


def test_lingering_combat_no_fresh_pull():
    """inst4 10:49:52-10:50:28: a KILL at 62% hp / 15% mana (a 2nd boar held by our Stoneclaw Totem), the combat flag
    lingered, the combat branch picked the totem's boar and pulled it at 17% mana; dead 22 s later. With nothing on us
    and resources under the pull line the main loop rests instead - and the pull estimate says skip anyway."""
    LOGS.clear()
    boar = plate(threat=True)                         # it has us on its list, but hits the totem
    s = st(combat=True, hp_pct=62, mana_pct=17, plates=[boar])
    b = make_bot([s])
    assert b.lingering_combat(s)
    assert any("no fresh pull" in m for m in LOGS), LOGS
    keep, _ = b.pull_filter(s, [boar])
    assert keep == [], keep                           # weight 1 vs 0.75 x capacity 0.40
    assert not b.lingering_combat(st(combat=True, hp_pct=62, mana_pct=17, plates=[plate(on_me=True)]))   # on us
    assert not b.lingering_combat(st(combat=True, hp_pct=95, mana_pct=95))                  # at the pull line
    b2 = make_bot([s])
    assert b2.lingering_combat(s)
    assert not b2.lingering_combat(st(combat=True, hp_pct=55, mana_pct=17))   # hp falling: an unseen attacker
    # rest(linger=True) rests under the lingering flag (rest() alone returns at once in combat)
    LOGS.clear()
    b3 = make_bot([st(combat=True, hp_pct=62, mana_pct=17)])
    b3.rest()
    assert not any(m.startswith("rest: hp") for m in LOGS), LOGS
    b3.s = iter([st(combat=True, hp_pct=62, mana_pct=17)] * 2 + [st(hp_pct=95, mana_pct=96)] * 50).__next__
    b3.rest(linger=True)
    assert any(m.startswith("rest: hp") for m in LOGS), LOGS


def test_outmatched_flees_a_fresh_add():
    """The bot's fight-time check: a fresh level 12 on us at 70% hp / 5% mana -> a flee reason (enough hp to run on;
    at 44% the run, ~3.5% hp/s for 15 s, costs as much as the fight - FLEE_DPS_W)."""
    add = plate(level=12, on_me=True, is_target=True, hp=100)
    s = st(hp_pct=70, mana_pct=5, mana=100, plates=[add], has_target=True, t_on_me=True, t_level=12, t_hp_pct=100)
    b = make_bot([s])
    why = b.outmatched(s, 0.0)
    assert why.startswith("outmatched"), why
    s2 = st(hp_pct=100, mana_pct=95, plates=[add], has_target=True, t_on_me=True, t_level=12, t_hp_pct=100)
    assert b.outmatched(s2, 0.0) == ""


def chase_bot(s, clock, bar=None, can=None):
    b = make_bot([s])
    b.mv = SimpleNamespace(stop=lambda: None)
    b.clock = lambda: clock[0]
    b._chase = None
    pressed, cast = [], []
    b.keys = SimpleNamespace(press=lambda k, hold=0.04: pressed.append(k))
    b.rot = SimpleNamespace(bar=lambda s_: bar or {}, _can=can or (lambda s_, bar_, n, rng=False: None), shock_next=0.0)
    b.rot_cast = lambda n: cast.append(n) or True
    b.cast = lambda n, wait_done=True: cast.append(n) or True
    b.face_target = lambda sync=False: True
    return b, pressed, cast


def runner(hp=14, sy=300, plates=(), **kw):
    tgt = plate(sx=1000, sy=sy, is_target=True, hp=hp, ghash=1)
    return st(has_target=True, t_hp_pct=hp, plates=[tgt, *plates], **kw)


def test_fleeing_target_is_chased_not_let_go():
    """bot 2 11:15:43: a 15% runner let go came back with friends (next pull maxatt=2). A runner is chased with
    interact-walk (re-pressed every CHASE_PRESS_S) until the chase stops making
    progress (the distance closing / its hp dropping) for CHASE_GIVEUP_S."""
    LOGS.clear()
    clock = [1000.0]
    friend = plate(sx=1040, sy=310, ghash=2)
    b, pressed, cast = chase_bot(runner(), clock)
    assert b.fleeing_target(runner(), True, True, None, None) is True
    assert pressed == ["m"] and any(m.startswith("chase: target at 14% runs") for m in LOGS), LOGS
    clock[0] += 0.3
    assert b.fleeing_target(runner(), True, True, None, None) is True and pressed == ["m"]  # paced
    clock[0] += 0.5
    assert b.fleeing_target(runner(), True, True, None, None) is True and pressed == ["m", "m"]
    # closing in (the plate lower on screen) at +6 s: still chasing at +12 s
    clock[0] = 1006.0
    assert b.fleeing_target(runner(sy=360), True, True, None, None) is True
    clock[0] = 1012.0
    assert b.fleeing_target(runner(sy=360), True, True, None, None) is True
    # ... its hp dropping is progress too
    clock[0] = 1013.0
    assert b.fleeing_target(runner(hp=10, sy=360), True, True, None, None) is True
    clock[0] = 1020.5
    assert b.fleeing_target(runner(hp=10, sy=360), True, True, None, None) is True
    # no progress for CHASE_GIVEUP_S: over
    clock[0] = 1021.5
    assert b.fleeing_target(runner(hp=10, sy=360), True, True, None, None) == "gone"
    assert any(m.startswith("chase: no progress") for m in LOGS), LOGS
    assert not any("letting it go" in m for m in LOGS), LOGS
    # caught up (in melee) / on us again / not low: no chase, the fight goes on as usual
    assert b.fleeing_target(runner(t_within10=True), True, True, None, None) is False
    assert b.fleeing_target(runner(hp=60), True, True, None, None) is False and b._chase is None


def test_no_walking_chase_to_friends_without_a_finisher():
    """bot 2 14:10:23: chased a 15% runner to 3 friends at 9% mana, nothing castable, 4 on us, dead. With friends to run
    to and no affordable finisher the runner is left, not walked after."""
    LOGS.clear()
    friend = plate(sx=1040, sy=310, ghash=2)
    b, pressed, cast = chase_bot(runner(plates=[friend]), [1000.0])
    assert b.fleeing_target(runner(plates=[friend]), True, True, None, None) == "gone"
    assert pressed == [] and any("no finisher is affordable" in m for m in LOGS), LOGS


def test_lone_mob_flee_must_be_clear_and_survivable():
    """bot 3 11:30:24: 'need 0.75 vs capacity 0.73 (58% hp 38% mana)' - fled one level 12 (at 75%), 33 yd in 19 s,
    dead. A near-miss is a fight (heal first); a flee must be clearly better and survivable. bot 2 11:16 (88% hp, 7%
    mana, a level 10, need 0.50): either way, but it must not die - the fight from 88% (melee line) or a run it
    survives."""
    m1 = 0.0
    one = [(12, 75, False, False)]
    assert threat.fresh_add(12, 58, 38, one, m1, heal_ok=True) == ""                 # near-miss on the old capacity
    assert threat.fresh_add(12, 58, 38, one, m1, heal_ok=True, hp_kill=60.0) == ""   # and with the melee line
    # the bot side: same numbers through Bot.outmatched (a heal on the bar, 38% of 500 mana)
    add = plate(level=12, on_me=True, is_target=True, hp=75)
    s = st(hp_pct=58, mana_pct=38, mana=190, plates=[add], has_target=True, t_on_me=True, t_level=12, t_hp_pct=75)
    b = make_bot([s])
    b.rot = SimpleNamespace(heal_next=0.0, bar=lambda s_: {"Healing Wave": (2, 332, 45)}, inc=4.0)
    assert b.outmatched(s, m1) == ""
    # bot 2 11:16: fought (the weapon kills a level 10 from 88%), and a flee would have been survivable too
    lone = [(10, 100, False, False)]
    assert threat.fresh_add(12, 88, 7, lone, m1, hp_kill=60.0) == ""
    assert 88 - threat.flee_loss(0.50) >= threat.FLEE_SURVIVE_HP
    # clearly lost and the run survivable: flee (a level 14 fresh at full hp, no mana: need 1.5 vs 1.17, run ~79%)
    why = threat.fresh_add(12, 100, 0, [(14, 100, False, False)], m1, hp_kill=60.0)
    assert why.startswith("outmatched") and "the run costs" in why, why
    # clearly lost but the run kills us (a fast measured incoming rate): fight it out
    assert threat.fresh_add(12, 90, 0, [(14, 100, False, False)], m1, hp_kill=60.0, inc=6.0) == ""
    # 2+ on us: the run must be survivable too (bot 4 14:14:13: two level 14s at 93% hp, the run ~131%, dead) - a fast
    # pair is fought; a weak pair at full hp still runs
    # (at 7% mana that pair is hopeless, need 3.0 vs 0.5: no heal, a sure loss in place - the run is the only chance)
    assert threat.fresh_add(12, 88, 7, [(12, 100, False, False)] * 2, m1, hp_kill=60.0, inc=9.0).startswith("hopeless")
    assert threat.fresh_add(12, 88, 60, [(12, 100, False, False)] * 2, m1, hp_kill=60.0, inc=9.0) == ""
    assert threat.fresh_add(12, 100, 0, [(8, 100, False, False)] * 2, m1).startswith(("outm", "hopeless"))


def test_fresh_pull_gate_in_fight():
    """bot 2 11:32:05: pick_target skipped a camp, 13 s later roam -> search_here -> fight() (the quest mob by name)
    pulled it anyway (est=3, weight 3.00 vs 0.75 x capacity 1.96), 2 joined, fled at 48% hp. fight() itself refuses
    a fresh pull the estimate skips, whatever path took the target."""
    LOGS.clear()
    tgt = plate(sx=960, sy=400, is_target=True, ghash=0x1234, npc=1161)
    s = st(has_target=True, t_hash=0xAB1234, t_npc=1161, t_level=12, t_hp_pct=100, plates=[tgt])
    b = make_bot([s])
    pressed, fought = [], []
    b.keys = SimpleNamespace(press=lambda k, hold=0.04: pressed.append(k))
    b._fight = lambda: fought.append(1) or "killed"
    est = threat.Estimate(3, 3.0, 1.96, "skip", "2 assist; weight 3.00 vs 0.75 x capacity 1.96", None, [], 0, "spawn")
    b.pull_est = lambda s_, p_: est
    assert b.fight() == "refused" and fought == [] and pressed == ["f11"], (fought, pressed)
    assert any(m.startswith("pull refused: npc 1161 lvl 12 est=3") for m in LOGS), LOGS
    assert b.blacklist.blocked(0x1234) and b.blacklist.blocked(0xAB1234)     # pick_target / search_here skip it
    # the estimate says pull: fought
    b.pull_est = lambda s_, p_: est._replace(verdict="pull")
    assert b.pull_refused(s) == ""
    # already on us / attackers / no plate (beyond plate range) / THREAT_LIVE off: never refused
    b.pull_est = lambda s_, p_: est
    assert b.pull_refused(st(has_target=True, t_on_me=True, t_hash=5, plates=[tgt])) == ""
    att = plate(on_me=True, ghash=9)
    assert b.pull_refused(st(has_target=True, t_hash=5, plates=[tgt, att])) == ""
    assert b.pull_refused(st(has_target=True, t_hash=5, plates=[])) == ""
    live, threat.THREAT_LIVE = threat.THREAT_LIVE, False
    try:
        assert b.pull_refused(st(has_target=True, t_hash=5, plates=[tgt])) == ""
    finally:
        threat.THREAT_LIVE = live


def test_chase_fires_the_cheapest_finisher_in_range():
    """The class's finishers (nb/classes.py Profile.finishers): the cheapest one castable now (rot._can: on the bar,
    ready, in range, affordable); none castable: interact-walk after it."""
    from nb import classes
    old = botmod.CLS
    botmod.CLS = classes.get("shaman")              # finishers Earth Shock, Lightning Bolt
    try:
        clock = [50.0]
        es, lb = (2, 8042, 30), (1, 403, 15)
        b, pressed, cast = chase_bot(runner(), clock, can=lambda s_, bar_, n, rng=False: es if n == "Earth Shock" else None)
        assert b.fleeing_target(runner(), True, True, None, None) is True and cast == ["Earth Shock"] and pressed == []
        # a rank-1 bolt (15) in range is cheaper than the shock (30)
        b, pressed, cast = chase_bot(runner(), clock, can=lambda s_, bar_, n, rng=False: {"Earth Shock": es,
                                                                                         "Lightning Bolt": lb}.get(n))
        assert b.fleeing_target(runner(), True, True, None, 1) is True and cast == ["Lightning Bolt"], cast
        # nothing castable now (cooldown / out of range): interact-walk
        b, pressed, cast = chase_bot(runner(), clock, can=lambda s_, bar_, n, rng=False: None)
        assert b.fleeing_target(runner(), True, True, None, 1) is True and cast == [] and pressed == ["m"]
    finally:
        botmod.CLS = old


def test_chase_stops_at_deadly_area_elite_and_joiners():
    LOGS.clear()
    clock = [10.0]
    b, pressed, _ = chase_bot(runner(), clock)
    # two deaths 60 yd ahead make a death_area round them (hazards.AREA_*): the runner heads into it
    b.hazards.add("death", 60.0, 5.0)
    b.hazards.add("death", 70.0, -5.0)
    assert b.fleeing_target(runner(), True, True, None, None) == "gone"
    assert any("chase: leaving the runner" in m and "deadly area" in m for m in LOGS), LOGS
    # an elite spawn whose aggro covers where it runs (threat spawn table)
    LOGS.clear()
    b, pressed, _ = chase_bot(runner(), clock)
    b.tm = threat.Model(threat.Spawns(rows=[(0, 35.0, -3.0, 0.0, 0.0, 99, 14, 14, 1, 44)], hostile=lambda f: True))
    assert b.fleeing_target(runner(), True, True, None, None) == "gone"
    assert any("elite's aggro" in m for m in LOGS), LOGS
    # an awake elite plate beside it on screen
    LOGS.clear()
    b, pressed, _ = chase_bot(runner(), clock)
    el = plate(sx=1030, sy=305, elite=True, ghash=3)
    assert b.fleeing_target(runner(plates=[el]), True, True, None, None) == "gone"
    assert any("elite beside it" in m for m in LOGS), LOGS
    # friends join: the fight / flee rules take over (no chase pass)
    LOGS.clear()
    b, pressed, _ = chase_bot(runner(), clock)
    assert b.fleeing_target(runner(), True, True, None, None) is True
    joined = plate(sx=700, sy=600, on_me=True, ghash=4)
    # ... and with no finisher castable now, the ones hitting us come first (bot 2 16:50:08-16: 8 s on a 5% runner)
    assert b.fleeing_target(runner(plates=[joined]), True, True, None, None) == "switch"
    assert any("joined" in m for m in LOGS) and any("turning on them" in m for m in LOGS) and b._chase is None, LOGS


def test_outmatched_lone_mob_fought_on_hp():
    """bot 2 11:16:07: 'outmatched: 1 on us need 0.50 vs capacity 0.48 (88% hp 7% mana)' - a lone level 10 at level
    12 fled at 88% hp. The weapon kills it: melee capacity (resources.melee_kill_hp) keeps the fight."""
    m1 = 0.0
    lone = [(10, 100, False, False)]
    assert threat.fresh_add(12, 88, 7, lone, m1) == ""     # 0.50 vs 0.48 on the old capacity: a near-miss now
    assert threat.fresh_add(12, 88, 7, lone, m1, hp_kill=60.0) == ""                # melee counted
    assert threat.fresh_add(12, 88, 7, [(12, 100, False, False)], m1, hp_kill=60.0) == ""   # an even one too
    # still fled: low hp (inst3 10:40, inst4 10:05:53) and 2+ on us
    assert threat.fresh_add(12, 44, 20, [(11, 100, False, False)], m1, hp_kill=60.0) == ""   # the run costs more
    assert threat.fresh_add(12, 70, 5, [(12, 100, False, False)], m1, hp_kill=60.0).startswith("outmatched")
    # inst4 10:05:53 (47% hp / 27% mana, a fresh level 10 at level 11): with the melee line a near-miss (need 0.75 vs
    # 0.73): fight and heal (the old rule, no melee line, still flees it)
    assert threat.fresh_add(11, 47, 27, [(10, 100, False, False)], 0.0, hp_kill=60.0) == ""
    two = [(12, 100, False, False), (12, 100, False, False)]
    assert threat.fresh_add(12, 88, 7, two, m1, hp_kill=60.0).startswith("hopeless")   # no heal left: run anyway
    # the bot side: Bot.outmatched passes the melee line
    add = plate(level=10, on_me=True, is_target=True, hp=100)
    s = st(hp_pct=88, mana_pct=7, mana=35, plates=[add], has_target=True, t_on_me=True, t_level=10, t_hp_pct=100)
    b = make_bot([s])
    assert b.outmatched(s, m1) == ""
    s2 = st(hp_pct=45, mana_pct=7, mana=35, plates=[add], has_target=True, t_on_me=True, t_level=10, t_hp_pct=100)
    assert b.outmatched(s2, m1).startswith("outmatched")
    # measured melee kills make the line: 1.2 x the median (35 -> 42), at least resources.MELEE_LO (30)
    s3 = st(hp_pct=45, mana_pct=7, mana=35, plates=[add], has_target=True, t_on_me=True, t_level=10, t_hp_pct=100)
    assert b.outmatched(s3, m1).startswith("outmatched")                           # default 60% hp a kill
    b.melee_costs = [(12, 30.0), (12, 35.0), (11, 40.0), (5, 1.0)]
    assert abs(b.res.melee_kill_hp(12) - 42.0) < 0.01
    b.melee_costs = [(12, 15.0), (12, 18.0), (12, 20.0)]
    assert b.res.melee_kill_hp(12) == 30.0
    assert b.outmatched(s3, m1) == ""


def test_roam_grind_prefers_lone_and_discounts_packs():
    """Grinding too (inst3's 8 Stonesplinter deaths, the last ones on an RXP xp step): a lone spawn first."""
    b = make_bot([st(level=12)])
    b.indoors = lambda p: False
    b.far_stronger = lambda s: False
    b.packed = lambda p, lvl, r=12.0: 0 if p[0] == 200 else 3
    went = []
    b.mv = SimpleNamespace(goto=lambda x, y, z=None, **kw: went.append((round(x), round(y))) or "ok",
                           stop=lambda: None)
    economy.dead_known = lambda: True
    economy.respawn_left = lambda g, now=None: 0.0
    economy.at = lambda x, y: None
    b.roam([(50, 0, 0, 1, 100.0), (200, 0, 0, 2, 100.0)])      # the near one sits in a camp
    assert went == [(200, 0)], went


def test_lower_pair_is_not_half_a_mob_1158():
    """bot 2 11:58:16, level 13: a level 11 with one level 11 beside it weighed 1.00 (0.5 each) vs 0.75 x 2.08 and was
    pulled; the pair cost 95% mana + 32% hp, flee, dead. Below-level slope + pack factor: (0.76 + 0.76) x 1.5 = 2.28."""
    m = threat.Model(threat.Spawns(rows=[], hostile=lambda f: True))
    e = m.pull(13, 0, 0, None, 1, 11, 100, 100, 48.0, plates=[(11, False, False)])
    assert e.verdict == "skip" and abs(e.weight - 2.28) < 0.01, e
    e = m.pull(13, 0, 0, None, 1, 11, 100, 100, 48.0)
    assert e.verdict == "pull" and abs(e.weight - 0.76) < 0.01, e


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
