"""Offline checks of nb/economy.py and the bot's walked rest / roam choice (fake states, no game, no DB).

Run: python -m nb.test_economy   (from the wowbot folder).
"""

from __future__ import annotations

import dataclasses
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import economy                  # noqa: E402
from nb import bot as botmod            # noqa: E402
from nb.bot import Bot                  # noqa: E402
from nb import hazards, threat          # noqa: E402

LOGS: list[str] = []
botmod.log = lambda m: LOGS.append(m)


def test_xp_for():
    # the log: level 7 got 16 / 32 xp for level 3 / 4 mobs and 80 for level 7 (inst1, 2026-10-14)
    assert economy.xp_for(7, 7) == 80
    assert economy.xp_for(7, 4) == 32
    assert economy.xp_for(7, 3) == 16
    assert economy.xp_for(7, 2) == 0                 # gray
    assert economy.xp_for(7, 8) == 84
    assert economy.xp_for(7, 13) == economy.xp_for(7, 11) == 96     # +5% a level, at most 4
    assert economy.xp_for(6, 6) == 75 and economy.xp_for(6, 5) == 60     # 15:18:00 / 15:18:32 kills
    assert economy.xp_for(5, 2) == 28                # no gray level up to 5
    assert economy.xp_for(1, 1) == 50
    assert economy.xp_for(7, 7, elite=True) == 160
    assert economy.xp_for(0, 5) == 0 and economy.xp_for(5, 0) == 0


def test_spawn_xp():
    assert economy.spawn_xp(7, 6, 7) == (64 + 80) / 2
    assert economy.spawn_xp(6, 6, 7) == 75 / 2       # a level 7 roll is above us: walked past, 0
    assert economy.spawn_xp(7, 3, 4) == (16 + 32) / 2


def test_mana_line():
    assert economy.mana_line([], 80) == (80.0, 0)
    assert economy.mana_line([50, 60, 70, 55], 80) == (80.0, 4)          # under 5 fights: the default
    line, n = economy.mana_line([55, 60, 62, 65, 70, 71], 80)
    assert n == 6 and line == 86.0, line                                  # p90 71 + 15
    assert economy.mana_line([20] * 10, 80)[0] == 40.0                    # clamped low
    assert economy.mana_line([85] * 10, 80)[0] == 90.0                    # clamped high
    assert economy.mana_line([30] * 9 + [60], 80)[0] == 45.0              # one bad fight in 10 is the 10%


def test_point_score():
    near_live = economy.point_score(80, 70)                 # 10 s walk
    near_dead = economy.point_score(80, 70, respawn_left=120)
    far_live = economy.point_score(80, 350)                 # 50 s walk
    assert near_live > far_live > near_dead
    assert economy.point_score(80, 70, respawn_left=5) == near_live    # respawns before we get there
    assert economy.point_score(80, 70) > economy.point_score(32, 0)    # a level-7 mob 70 yd off beats a gray-ish one here


# ------------------------------------------------------------ bot side
def st(level=7, hp=100.0, mana=100.0, combat=False, x=0.0, y=0.0, plates=()):
    return SimpleNamespace(level=level, hp_pct=hp, mana_pct=mana, combat=combat, plates=list(plates), x=x, y=y, z=0.0,
                           dead=False, has_target=False, t_dead=False)


def make_bot(states):
    b = Bot.__new__(Bot)
    seq = list(states)

    def s():
        return seq.pop(0) if len(seq) > 1 else seq[0]
    b.s = s
    b.wanted, b.grind_ok, b.blacklist, b.avoid, b.visited = set(), True, {}, [], {}
    b.walk_rest, b.pull_anyway_t, b.last_multi, b.fight_maxatt = 0.0, 0.0, False, 0
    b.fight_costs, b._line_log = botmod.deque(maxlen=botmod.FIGHT_COSTS_N), None
    b.rot = SimpleNamespace(heal_next=0.0)
    b.keys = SimpleNamespace(press=lambda k, hold=0.04: None)
    b.cast = lambda name, wait_done=True: False
    b.has_item = lambda e, s: True
    b.W = 1920
    b.tm = threat.Model(threat.Spawns(rows=[], hostile=lambda f: True))   # no world DB: an empty spawn table
    b.hazards = hazards.Hazards(persist=False)
    return b


def hs(b):
    """The bot's live hunt-point skip zones as the old (x, y, r, until) tuples."""
    return [(h.x, h.y, h.r, h.until) for h in b.hazards.active(hazards.HUNT_KINDS)]


def set_hs(b, zones):
    b.hazards.clear()
    for x, y, r, until in zones:
        b.hazards.add("hunt_skip", x, y, r, until - botmod.time.time())


def test_rest_big_gap_is_not_walked():
    # 40% vs the 80 line: a walk meets mobs half empty (bot 2 18:57:05, 38 vs 90, dead) - rest standing
    LOGS.clear()
    b = make_bot([st(mana=40, hp=90)])
    b.rest()
    assert not any("walking it off" in m for m in LOGS), LOGS


def test_rest_walks_between_drink_and_line():
    LOGS.clear()
    b = make_bot([st(mana=55, hp=90)])
    b.rest()
    assert b.walk_rest > 0, LOGS
    assert any("walking it off" in m for m in LOGS), LOGS
    assert not b.ready_to_pull(st(mana=60, hp=90))
    assert b.ready_to_pull(st(mana=81, hp=90))             # default line 80 until 5 fights are measured
    b.s = lambda: st(mana=82, hp=90)
    b.rest()
    assert b.walk_rest == 0.0 and any("walked it off" in m for m in LOGS), LOGS


def test_rest_line_from_fights():
    b = make_bot([st()])
    for c in (55, 60, 62, 65, 70, 71):
        b.fight_costs.append((7, c))
    b.fight_costs.append((5, 99))                           # another level: not used once 5 at this level exist
    hp, mana, ehp, emana, boss = b.rest_line(st())
    assert (mana, emana, boss) == (80.0, 85.0, False), (mana, emana)   # median 65 + 15
    assert b.rest_line(st(level=4))[1] == botmod.LOW_REST_MANA


def test_rest_stands_below_drink_line_and_after_multi():
    for kw, multi in ((dict(mana=15, hp=90), False), (dict(mana=50, hp=90), True), (dict(mana=50, hp=60), False)):
        LOGS.clear()
        b = make_bot([st(**kw), st(combat=True)])           # the standing loop's first read ends it
        b.last_multi = multi
        b.rest()
        assert b.walk_rest == 0.0, (kw, LOGS)
        assert any(m.startswith("rest: hp") for m in LOGS), (kw, LOGS)
        assert not b.last_multi


def test_grind_rejects_low_mobs():
    from nb.eye import Plate
    b = make_bot([st()])
    b.boss_ready = lambda st_, p: True

    def pl(level, on_me=False, npc=1125):
        return Plate(sx=900, sy=500, npc=npc, level=level, hp=100, hostile=True, dead=False, on_me=on_me,
                     is_target=False, tapped=False, combat=False, player=False, elite=False, friendly=False,
                     token=1, ghash=level * 10 + int(on_me))
    me = st(level=7)
    assert b.valid_plate(pl(7), me) and b.valid_plate(pl(5), me)
    assert not b.valid_plate(pl(4), me)                     # 3 below: 32 xp, walked past
    assert b.valid_plate(pl(4, on_me=True), me)             # unless it attacks us
    b.wanted = {706}
    assert b.valid_plate(pl(4, npc=706), me)                # ... or the quest wants it
    assert not b.valid_plate(pl(4), me)


def roam_bot(pts, left, me=None, visited=()):
    b = make_bot([me or st()])
    b.indoors = lambda p: False
    b.far_stronger = lambda s: False
    went = []
    arrived = [False]

    def goto(x, y, z=None, **kw):
        went.append((round(x), round(y)))
        arrived[0] = True
        return "ok"
    b.mv = SimpleNamespace(goto=goto, stop=lambda: None)
    for p in visited:
        b.visited[(round(p[0]), round(p[1]))] = botmod.time.time()
    economy.dead_known = lambda: True
    economy.respawn_left = lambda g, now=None: 0.0 if arrived[0] else left.get(g, 0.0)
    economy.at = lambda x, y: None
    b.roam(pts)
    return went


def test_roam_prefers_live_and_xp():
    LOGS.clear()
    pts = [(70, 0, 0, 1, 80.0),        # 70 yd, dead for 200 s
           (140, 0, 0, 2, 80.0),       # 140 yd, alive
           (30, 0, 0, 3, 16.0)]        # 30 yd, alive, gray-ish
    assert roam_bot(pts, {1: 200.0}) == [(140, 0)], LOGS


def test_roam_all_visited_goes_live_or_soonest():
    LOGS.clear()
    pts = [(70, 0, 0, 1, 80.0), (140, 0, 0, 2, 80.0), (300, 0, 0, 3, 80.0)]
    assert roam_bot(pts, {1: 200.0, 2: 90.0}, visited=pts) == [(300, 0)], LOGS        # the one that is up
    assert any("1 up again" in m for m in LOGS), LOGS
    LOGS.clear()
    assert roam_bot(pts, {1: 200.0, 2: 90.0, 3: 150.0}, visited=pts) == [(140, 0)], LOGS   # first to respawn
    assert any("first respawns in 90s" in m for m in LOGS), LOGS
    assert not any("waiting for respawns" in m for m in LOGS), LOGS


def test_roam_stops_short_while_mana_is_low():
    LOGS.clear()
    b = make_bot([st(mana=40, hp=90, x=0, y=0)])
    b.indoors = lambda p: False
    b.far_stronger = lambda s: False
    b.walk_rest = 1.0
    calls = []

    def goto(x, y, z=None, abort=None, **kw):
        calls.append(abort(st(mana=40 if not calls else 85, hp=90, x=x - 30, y=y)))   # 30 yd short of the spawn
        return "abort" if calls[-1] else "ok"
    b.mv = SimpleNamespace(goto=goto, stop=lambda: None)
    waits = []
    b.wait_ready = lambda why: waits.append(why) or True
    b.pull_in_view = lambda s: False
    economy.dead_known = lambda: True
    economy.respawn_left = lambda g, now=None: 0.0
    b.roam([(100, 0, 0, 1, 80.0)])
    assert calls == [True, False], calls                   # stopped short, waited, then walked on
    assert waits and "short of the spawn" in waits[0], waits


def hunt_bot(pts_me=None, how="skip", plates=()):
    """A roam() bot on a quest hunt whose search_here answers `how`; returns (bot, went, searches)."""
    me = pts_me or st(plates=plates)
    b = make_bot([me])
    b.indoors = lambda p: False
    b.far_stronger = lambda s: False
    b.packed = lambda p, lvl, r=12.0: 0
    b.attackers = lambda s: []
    b.wanted = {706}
    went, searches = [], []

    def goto(x, y, z=None, **kw):
        went.append((round(x), round(y)))
        return "ok"
    b.mv = SimpleNamespace(goto=goto, stop=lambda: None)
    b.search_here = lambda x, y: searches.append((round(x), round(y))) or how
    economy.dead_known = lambda: True
    economy.respawn_left = lambda g, now=None: 0.0
    economy.at = lambda x, y: None
    return b, went, searches


def test_roam_skip_zone_after_near_skip():
    """inst2 20:53-20:55: the spawn underfoot answered 'skip' and roam() picked it again every 0.5 s (REPORT.md
    change 2). A near point that gives nothing blocks every point within 40 yd of it for 60 s."""
    LOGS.clear()
    pts = [(5, 0, 0, 1, 80.0), (30, 0, 0, 2, 80.0), (100, 0, 0, 3, 80.0)]
    b, went, _ = hunt_bot()
    b.roam(pts)
    assert went == [(5, 0)], went
    assert len(hs(b)) == 1 and hs(b)[0][:3] == (5, 0, botmod.HUNT_SKIP_R), hs(b)
    assert 59 < hs(b)[0][3] - botmod.time.time() <= botmod.HUNT_SKIP_S
    assert any("searched <120 s ago" in m for m in LOGS), LOGS
    b.visited.clear()                                       # as after "all hunt points visited <240s ago; N up"
    b.roam(pts)
    assert went == [(5, 0), (100, 0)], went                 # the 5 and 30 yd points are inside the zone
    # the zone runs out after HUNT_SKIP_S: the point is a candidate again
    set_hs(b, [(5, 0, 40.0, botmod.time.time() - 1)])
    b.visited.clear()
    b.roam(pts[:2])
    assert went[-1] == (5, 0) and len(went) == 3, went


def test_roam_far_mob_moves_on():
    """The named mob is beyond plate range: the point (even a far one) is left for 60 s, roam goes elsewhere."""
    LOGS.clear()
    pts = [(100, 0, 0, 1, 80.0), (400, 0, 0, 2, 80.0)]
    b, went, _ = hunt_bot(how="far")
    b.roam(pts)
    assert went == [(100, 0)] and len(hs(b)) == 1, (went, hs(b))
    assert any("beyond plate range" in m for m in LOGS), LOGS
    b.visited.clear()
    b.roam(pts)
    assert went == [(100, 0), (400, 0)], went


def test_roam_no_zone_when_far_or_found():
    for how, pts in (("skip", [(100, 0, 0, 1, 80.0)]),       # 100 yd walk: not the point underfoot
                     ("plate", [(5, 0, 0, 1, 80.0)]),        # a wanted plate: pick_target takes it
                     ("combat", [(5, 0, 0, 1, 80.0)])):
        b, went, _ = hunt_bot(how=how)
        b.roam(pts)
        assert went and hs(b) == [], (how, hs(b))
    # a mob pick_target would take is in view: no zone either
    from nb.eye import Plate
    pl = Plate(sx=900, sy=500, npc=1125, level=7, hp=100, hostile=True, dead=False, on_me=False, is_target=False,
               tapped=False, combat=False, player=False, elite=False, friendly=False, token=1, ghash=9)
    b, went, _ = hunt_bot(how=None, plates=[pl])
    b.boss_ready = lambda s, p: True
    b.roam([(5, 0, 0, 1, 80.0)])
    assert went and hs(b) == [], hs(b)


def test_roam_near_nothing_found_and_grind():
    b, went, _ = hunt_bot(how=None)                         # searched, nothing found, 5 yd away
    b.roam([(5, 0, 0, 1, 80.0)])
    assert len(hs(b)) == 1
    b, went, searches = hunt_bot(how=None)
    b.wanted = set()                                        # grind: no search, the spawn underfoot shows nothing
    b.roam([(8, 0, 0, 1, 80.0)])
    assert searches == [] and len(hs(b)) == 1, (searches, hs(b))


def test_roam_all_points_zoned_waits():
    LOGS.clear()
    b, went, _ = hunt_bot()
    set_hs(b, [(0, 0, 40.0, botmod.time.time() + 0.6)])
    t0 = botmod.time.time()
    b.roam([(5, 0, 0, 1, 80.0), (20, 0, 0, 2, 80.0)])
    assert went == [], went                                 # no walk to the point underfoot
    assert 0.4 < botmod.time.time() - t0 < 2.0
    assert any("inside a skip zone" in m for m in LOGS), LOGS


def test_chain_ready():
    """REPORT.md change 6: right after a loot, a valid mob in view is pulled before rest / buffs / vendor, when hp
    and mana are at rest_line()'s pull line."""
    from nb.eye import Plate
    pl = Plate(sx=900, sy=500, npc=1125, level=7, hp=100, hostile=True, dead=False, on_me=False, is_target=False,
               tapped=False, combat=False, player=False, elite=False, friendly=False, token=1, ghash=9)
    b = make_bot([st()])
    b.boss_ready = lambda s, p: True
    b.attackers = lambda s: [p for p in s.plates if p.on_me and not p.dead]
    now = botmod.time.time()
    b.loot_done_t = now - 1
    assert b.chain_ready(st(plates=[pl], mana=85, hp=90))                  # default line 80 / 85
    assert not b.chain_ready(st(plates=[pl], mana=70, hp=90))              # below the pull line: rest first
    assert not b.chain_ready(st(plates=[pl], mana=95, hp=80))
    assert not b.chain_ready(st(plates=[], mana=95, hp=95))                # nothing in view
    assert not b.chain_ready(st(plates=[dataclasses.replace(pl, sx=-1)], mana=95, hp=95))   # behind the camera
    pl4 = dataclasses.replace(pl, level=4)
    b.last_multi = True
    assert not b.chain_ready(st(plates=[pl4], mana=79, hp=95, level=4))     # after a 2+ mob fight: the full line
    b.last_multi = False
    assert b.chain_ready(st(plates=[pl4], mana=50, hp=90, level=4))         # levels 1-5: LOW_REST_MANA 45
    b.loot_done_t = now - botmod.CHAIN_PULL_S - 1
    assert not b.chain_ready(st(plates=[pl], mana=95, hp=95))              # only straight after a loot
    b.loot_done_t = 0.0
    assert not b.chain_ready(st(plates=[pl], mana=95, hp=95))


def loot_state(**kw):
    d = dict(has_target=True, t_dead=True, t_hash=55, t_tapped=False, t_on_me=False, t_loot_known=False,
             t_within10=True, moving=False, errn=0, err=0, money=10, bags_free=5, quests=[], plates=[],
             x=0.0, y=0.0, combat=False, ui_open=lambda i: False)
    d.update(kw)
    return SimpleNamespace(**d)


def test_loot_one_moves_on_without_window():
    """REPORT.md change 6: one interact; no loot window within LOOT_OPEN_WAIT s standing = move on (was 1.6 s),
    unless CanLootUnit said the corpse holds loot for us."""
    from nb.kills import Kill
    for seen, waited in (({}, (botmod.LOOT_OPEN_WAIT, 1.4)), ({55: {"v": True, "t": 5.0, "dead": 0.0}}, (1.6, 2.3))):
        b = make_bot([loot_state()])
        pressed = []
        b.keys = SimpleNamespace(press=lambda k, hold=0.04: pressed.append(k))
        b.target_corpse = lambda c: True
        b.target_plate = lambda s: None
        b.loot_seen = seen
        c = Kill(botmod.time.time(), 1125, 55, 7, "xp", 0, 0.0, 0.0, 0.0, 0.0, 1)
        t0 = botmod.time.time()
        res = b.loot_one(c)
        el = botmod.time.time() - t0
        assert res == "noopen" and pressed.count("m") == 1, (res, pressed)
        assert waited[0] <= el < waited[1], (seen, el)
        assert c.has_loot is (True if seen else None)


def test_retry_loot_one_interact():
    """REPORT.md change 6: a corpse that opened no window is tried once more only when CanLootUnit said it holds
    loot for us (58 of 190 kills paid a retry on 2026-10-14)."""
    from nb.kills import retry_loot
    assert not retry_loot("noopen", 1, 7, set())                    # CanLootUnit unknown: move on
    assert not retry_loot("noopen", 1, 7, set(), False)
    assert retry_loot("noopen", 1, 7, set(), True)
    assert not retry_loot("noopen", 2, 7, set(), True)              # once only
    assert not retry_loot("noopen", 1, 7, {7}, True)                # a neighbour of the fight opened
    assert retry_loot("toofar", 2, 7, set()) and not retry_loot("toofar", 3, 7, set())
    assert not retry_loot("looted", 1, 7, set(), True)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
