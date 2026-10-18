"""Offline tests for the facing recovery (Bot.face_probe / face_recover / handle_err, Mover.turn_run) with fakes.

Run: python -m nb.test_face   (from the wowbot folder). Touches no game window.
"""

from __future__ import annotations

import math
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import bot as botmod            # noqa: E402
from nb.bot import Bot                  # noqa: E402
from nb.move import Mover, wrap         # noqa: E402

LOGS: list[str] = []
botmod.log = lambda m: LOGS.append(m)
from nb import classes                  # noqa: E402
SHAMAN = classes.get("shaman")          # the probe / opener tests use a caster's profile (a facing probe spell)
botmod.CLS = SHAMAN
W = 1920


class FakeMv:
    def __init__(self, turn_gain=1.0, pad=True):
        self.turns, self.pad, self.gain, self.facing, self.stops = [], pad, turn_gain, 0.0, 0

    def padding(self):
        return self.pad

    def stop(self):
        self.stops += 1

    def aim_done(self):
        pass

    def turn(self, a):
        self.turns.append(("arrow", a))
        self.facing += a

    def turn_run(self, a, **kw):
        self.turns.append(("run", a))
        got = a * self.gain
        self.facing += got
        return got

    def state(self):
        return SimpleNamespace(facing=self.facing)


def make_bot(works_after, mv=None, near=True, hp=100.0):
    """works_after: what turns the probe bolt succeed: the number of turn_run/interact steps needed (None: never)."""
    b = Bot.__new__(Bot)
    b.mv = mv or FakeMv()
    b.W = W
    b.errn, b.steps, b.cam_off, b.face_fail, b.last_err_n, b.m_t = 0, 0, False, 0, None, 0.0
    b.keys = SimpleNamespace(press=lambda k, hold=0.04: (PRESSED.append(k), setattr(b, "steps", b.steps + (1 if k == "m" and M_WORKS else 0))))
    st = SimpleNamespace(t_within10=near, hp_pct=hp, moving=False, t_on_me=near, plates=[])

    def s():
        st.errn, st.err, st.facing = b.errn, 4, b.mv.facing
        return st
    b.s = s
    b.cast_calls = 0

    def cast(name, wait_done=True):
        b.cast_calls += 1
        if works_after is not None and b.steps + len(b.mv.turns) >= works_after:
            return True
        b.errn += 1                                   # facing error
        return b.handle_err(4)
    b.cast = cast
    b.cancel_walk = lambda: None
    b.rot = SimpleNamespace(bar=lambda s: {"Lightning Bolt": (1, 403, 0)}, _can=lambda s, bar, n, rng=False: bar.get(n),
                            note_cast=lambda *a: None)
    b.res = SimpleNamespace(affordable=lambda s, n: True)
    return b


def test_probe_without_a_probe_spell_watches_the_swing():
    """A class with no facing spell to press (a melee class, a hunter in melee): the swing's facing errors are the
    probe - a fresh one is -1, none while swinging is 1, out of melee 0."""
    botmod.CLS = classes.get("warrior")
    try:
        b = Bot.__new__(Bot)
        b.FACE_WATCH_S = 0.3
        st = SimpleNamespace(errn=5, err=0, t_within10=True, has_target=True, t_dead=False, autoattack=True,
                             actions=[6603] + [0] * 9)
        b.s = lambda: st
        b.rot = SimpleNamespace(bar=lambda s: {}, _can=lambda *a, **k: None)
        b.keys = SimpleNamespace(press=lambda k, hold=0.04: PRESSED.append(k))
        assert b.face_probe() == 1                       # swinging, no facing error: we face it
        n = [5]

        def erring():
            n[0] += 1
            st.errn, st.err = n[0], 4
            return st
        b.s = erring
        assert b.face_probe() == -1                      # "facing the wrong way" while swinging
        st2 = SimpleNamespace(errn=0, err=0, t_within10=False, has_target=True, t_dead=False, autoattack=False)
        b.s = lambda: st2
        assert b.face_probe() == 0                       # out of melee: cannot tell
    finally:
        botmod.CLS = SHAMAN


PRESSED: list[str] = []
M_WORKS = False


def test_defaults_and_plate_side():
    b = Bot.__new__(Bot)
    assert b.last_side == 1 and b.recovering is False and b.no_face_until == 0.0
    b.W = W
    p = SimpleNamespace(is_target=True, sx=1500)
    b.s = lambda: SimpleNamespace(plates=[p])
    b.target_plate()
    assert b.last_side == -1                          # right of centre
    p.sx = 300
    b.target_plate()
    assert b.last_side == 1
    p.sx = -1
    b.target_plate()
    assert b.last_side == 1                           # off screen: keep the last side


def test_probe_fine_does_nothing():
    LOGS.clear(); PRESSED.clear()
    b = make_bot(works_after=0)
    assert b.face_recover("t") is True
    assert PRESSED == [] and b.mv.turns == []
    assert b.cast_calls == 1 and not b.recovering and b.no_face_until > time.time()


def test_interact_fixes_it():
    global M_WORKS
    LOGS.clear(); PRESSED.clear(); M_WORKS = True
    try:
        b = make_bot(works_after=1)
        assert b.face_recover("t") is True
        assert PRESSED == ["m"] and b.mv.turns == []
        assert time.time() < b.no_face_until          # nothing may touch the character for ~1 s
    finally:
        M_WORKS = False


def test_scan_turns_same_direction_towards_last_side():
    LOGS.clear(); PRESSED.clear()
    b = make_bot(works_after=2)
    b.last_side = -1
    assert b.face_recover("t") is True
    assert b.mv.turns == [("run", -2.0), ("run", -2.0)], b.mv.turns
    assert any("face_recover(t)" in m for m in LOGS)
    assert b.face_fail == 0 and not b.recovering


def test_never_works_gives_up_after_two_steps():
    LOGS.clear(); PRESSED.clear()
    b = make_bot(works_after=None)
    assert b.face_recover("t") is False
    assert len(b.mv.turns) == 2 and not b.recovering


def test_dead_pad_falls_back_to_arrows():
    LOGS.clear(); PRESSED.clear()
    b = make_bot(works_after=None, mv=FakeMv(turn_gain=0.0))
    b.face_recover("t")
    assert ("arrow", 2.0) in b.mv.turns
    assert any("did not turn" in m for m in LOGS), LOGS


def test_hurt_far_mob_skips_interact():
    LOGS.clear(); PRESSED.clear()
    b = make_bot(works_after=None, near=False, hp=40.0)
    b.face_recover("t")
    assert "m" not in PRESSED


def test_handle_err_no_recursion_and_melee_path():
    LOGS.clear(); PRESSED.clear()
    b = make_bot(works_after=0)
    b.face_fail = 0
    b.recovering = True
    assert b.handle_err(4) is False and b.face_fail == 0          # the probe's own error is ignored
    b.recovering = False
    called = []
    b.face_recover = lambda why="": called.append(why) or True
    b.target_plate = lambda s=None: None
    assert b.handle_err(4) is False and called == ["err"]         # adjacent attacker: straight to the recovery


def test_face_target_gate_after_interact():
    b = make_bot(works_after=0)
    b.no_face_until = time.time() + 1.0
    assert b.face_target(sync=True) is False                      # leaves the character alone
    b.no_face_until = 0.0
    called = []
    b.face_recover = lambda why="": called.append(why) or True
    b.target_plate = lambda s=None: None
    assert b.face_target(sync=True) is True and called == ["melee"]
    called.clear()
    assert b.face_target(sync=False) is True and called == []     # adjacent, no plate: no pan


class FakePad:
    def __init__(self, world):
        self.w, self.fwd, self.rate = world, 0.0, 0.0

    def forward(self, v):
        self.w.advance(); self.fwd = v

    def turn(self, r):
        self.w.advance(); self.rate = r

    def stop(self):
        self.w.advance(); self.fwd = self.rate = 0.0


class World:
    def __init__(self, k=0.8):
        self.facing, self.k, self.t = 0.0, k, time.time()
        self.pad = FakePad(self)

    def advance(self):
        now = time.time()
        if self.pad.fwd > 0:                         # a standing character does not turn with the pad
            self.facing = wrap(self.facing + self.pad.rate * self.k * (now - self.t))
        self.t = now


def make_mover(world):
    m = Mover.__new__(Mover)
    m.pad, m.running = world.pad, False
    m._tok = None                    # Mover.stop() releases the pad token (move.py _token); none held here
    m.padding = lambda: True
    m.state = lambda: (world.advance(), SimpleNamespace(facing=world.facing))[1]
    m.keys = SimpleNamespace(up=lambda k: None, down=lambda k: None)
    return m


def test_turn_run_closed_loop():
    w = World()
    m = make_mover(w)
    got = m.turn_run(2.0)
    assert abs(got - 2.0) < 0.45, got
    assert abs(wrap(w.facing) - wrap(got)) < 0.1
    assert w.pad.fwd == 0.0 and w.pad.rate == 0.0                  # stick released
    w2 = World(k=0.0)                                             # dead pad: reports it did not turn
    assert abs(make_mover(w2).turn_run(2.0, maxt=0.3)) < 0.05


def test_sync_heading_stops_when_facing_settles():
    w = World()
    m = make_mover(w)
    t0 = time.time()
    m.sync_heading()
    assert time.time() - t0 < 0.65 and w.pad.fwd == 0.0


# ---- REPORT change 3: the swing in melee must really run (ensure_swing)
def swing_bot():
    PRESSED.clear(); LOGS.clear()
    b = Bot.__new__(Bot)
    b.m_t = 0.0
    b.keys = SimpleNamespace(press=lambda k, hold=0.04: PRESSED.append(k))
    return b


def test_ensure_swing_attack_then_interact():
    b = swing_bot()
    s = SimpleNamespace(autoattack=False, casting_now=False, t_within10=True)
    sw = Bot.swing_state()
    assert b.ensure_swing(s, 1, sw) is True and PRESSED == ["1"]       # the Attack slot first
    assert b.ensure_swing(s, 1, sw) is True and PRESSED == ["1"]       # not again before SWING_RETRY s
    sw["next"] = 0.0
    assert b.ensure_swing(s, 1, sw) is True and PRESSED == ["1", "m"]  # still off: interact (never toggles it off)
    assert sw["hold"] > time.time() and b.no_face_until > time.time() and b.m_t > 0
    s.autoattack = True
    assert b.ensure_swing(s, 1, sw) is True and sw["n"] == 0 and PRESSED == ["1", "m"]
    assert any("swing on after 2" in m for m in LOGS), LOGS


def test_ensure_swing_gives_up_and_waits_for_casts():
    b = swing_bot()
    s = SimpleNamespace(autoattack=False, casting_now=True, t_within10=True)
    sw = Bot.swing_state()
    assert b.ensure_swing(s, 1, sw) is True and PRESSED == []          # never press into a running cast
    s.casting_now = False
    for _ in range(botmod.SWING_TRIES):
        sw["next"] = 0.0
        assert b.ensure_swing(s, 1, sw) is True
    assert PRESSED == ["1"] + ["m"] * (botmod.SWING_TRIES - 1)
    sw["next"] = 0.0
    assert b.ensure_swing(s, 1, sw) is False and PRESSED[-1] == "1"   # not startable: bolts carry it, Attack every 3 s
    assert sw["next"] > time.time() + 2.5
    s.t_within10 = False
    sw2 = Bot.swing_state()
    assert b.ensure_swing(s, 1, sw2) is True and len(PRESSED) == botmod.SWING_TRIES + 1   # not in melee: no press


# ---- REPORT change 7: open from bolt range, wait there for the bolt (opener_hold / approach(opener=True))
def opener_bot(ready=True, rng=True, mana=100, on_me=False, t_hp=100.0):
    from nb.shaman_rotation import Rotation
    b = Bot.__new__(Bot)
    b.rot = Rotation()
    b.ranged = "Lightning Bolt"
    b.W = W
    b.m_t = 0.0
    st = SimpleNamespace(has_target=True, t_dead=False, t_on_me=on_me, t_hp_pct=t_hp, mana=mana, mana_pct=mana,
                         t_within10=False, actions=[403, 331] + [0] * 10, plates=[])
    st.ready = lambda slot: ready
    st.in_range = lambda slot: rng
    b.s = lambda: st
    b.slot_of = lambda name, s=None: 1 if name == "Lightning Bolt" else None
    drives = []
    b.mv = SimpleNamespace(stop=lambda: None, drive=lambda e: drives.append(e), padding=lambda: True)
    b.target_plate = lambda s=None: SimpleNamespace(sx=W / 2)
    walks = []
    b.cancel_walk = lambda: walks.append(1)
    return b, st, drives, walks


def test_opener_hold():
    b, st, _, _ = opener_bot()
    assert b.opener_hold(st, 1) is True and b.hold_t0 == 0.0           # in range + ready: stop, _fight casts
    st.ready = lambda slot: False
    assert b.opener_hold(st, 1) is True and b.hold_t0 > 0               # not ready: wait in place ...
    b.hold_t0 = time.time() - botmod.OPENER_WAIT - 0.5
    assert b.opener_hold(st, 1) is False                                # ... but not on a flag that never comes
    b.hold_t0 = 0.0
    st.mana = 10                                                        # no bolt's worth (15): walk in, melee
    assert b.opener_hold(st, 1) is False
    st.mana = 40
    assert b.opener_hold(st, 1) is True                                 # low mana is fine for the opener
    st.t_on_me = True
    assert b.opener_hold(st, 1) is False                                # it is coming: not an opener any more
    st.t_on_me = False
    st.in_range = lambda slot: False
    assert b.opener_hold(st, 1) is False                                # out of bolt range: keep walking
    assert b.opener_hold(st, None) is False


def test_approach_opener_waits_instead_of_walking_in():
    b, st, drives, walks = opener_bot(ready=False, mana=30)
    t0 = time.time()
    b.approach(1.0, opener=True)
    assert drives == [] and walks == [1] and time.time() - t0 < 0.5    # stopped at bolt range, did not walk
    b2, st2, drives2, _ = opener_bot(ready=False, mana=30)
    b2.approach(0.2)                                                    # old rule (mana > 45 and ready): walks in
    assert drives2, "approach without opener should still walk"


# ---- REPORT change 5: the mob nearest the character first; a failed key blocks only its own mob
def plate(sx, sy, ghash, token, npc=100, on_me=False):
    return SimpleNamespace(sx=sx, sy=sy, ghash=ghash, token=token, npc=npc, on_me=on_me, dead=False, hp=100,
                           is_target=False)


def pick_bot(plates, takes=True):
    """takes: a plate key targets its own plate (else nothing gets targeted)."""
    PRESSED.clear(); LOGS.clear()
    b = Bot.__new__(Bot)
    b.W, b.wanted, b.blacklist, b.want_seen_t = W, set(), {}, 0.0
    st = SimpleNamespace(plates=plates, has_target=False, t_dead=False, t_npc=100)
    b.s = lambda: st
    b.valid_plate = lambda p, s: not b.blacklist.blocked(p.ghash)      # (nb/backoff.py)

    def press(k, hold=0.04):
        PRESSED.append(k)
        if takes and k.startswith("f") and k[1:].isdigit() and int(k[1:]) <= 10:
            for p in plates:
                p.is_target = p.token == int(k[1:])
            st.has_target = any(p.is_target for p in plates)
    b.keys = SimpleNamespace(press=press)
    return b


def test_near_rank_lower_plate_first_then_centre():
    b = pick_bot([])
    band = botmod.PICK_SY_BAND * botmod.FOCAL
    far_centre, near_side = plate(W / 2, 200, 1, 1), plate(W / 2 + 600, 200 + 3 * band, 2, 2)
    assert b.near_rank(near_side) < b.near_rank(far_centre)            # lower on screen = closer: first
    level_centre, level_side = plate(W / 2 + 50, 400, 3, 3), plate(W / 2 + 500, 400 + 0.1, 4, 4)
    if int(400 // band) == int(400.1 // band):
        assert b.near_rank(level_centre) < b.near_rank(level_side)     # same band: nearer screen centre
    assert b.near_rank(plate(-1, -1, 5, 5)) > b.near_rank(far_centre)  # off screen: last


def test_pick_target_takes_the_nearest_mob():
    band = botmod.PICK_SY_BAND * botmod.FOCAL
    ps = [plate(W / 2, 150, 11, 1), plate(W / 2 + 700, 150 + 4 * band, 12, 2), plate(W / 2 - 300, 150 + band, 13, 3)]
    b = pick_bot(ps)
    assert b.pick_target() is True
    assert PRESSED == ["f2"], PRESSED                                   # the lowest plate, not the centred one


def test_pick_target_failed_key_blocks_only_that_mob():
    band = botmod.PICK_SY_BAND * botmod.FOCAL
    ps = [plate(W / 2, 150 + 4 * band, 21, 1), plate(W / 2, 150, 22, 0), plate(W / 2 + 200, 150, 23, 0)]
    b = pick_bot(ps, takes=False)
    assert b.pick_target() is False
    assert PRESSED[0] == "f1" and "tab" in PRESSED and PRESSED[-1] == "f11", PRESSED
    assert set(b.blacklist) == {21}, b.blacklist                        # only the mob whose key failed
    assert any("skipping only those" in m for m in LOGS), LOGS
    # nothing has a key: only the best candidate (the one tab was after) is skipped, the next pick takes the next
    ps2 = [plate(W / 2, 150 + 4 * band, 31, 0), plate(W / 2, 150, 32, 0)]
    b2 = pick_bot(ps2, takes=False)
    assert b2.pick_target() is False and set(b2.blacklist) == {31}, b2.blacklist
    # an attacker on us is never blacklisted (old rule kept)
    ps3 = [plate(W / 2, 300, 41, 1, on_me=True)]
    b3 = pick_bot(ps3, takes=False)
    b3.pick_target()
    assert not b3.blacklist


# ---- REPORT change 4: floor tracking + wedge replans (move.py floor_z / goto)
def fake_floor_mesh(polys):
    """polys: [(xy vertex list in WoW coords, height)] in one tile -> an nm floor_z can read (rc = (y, z, x))."""
    import numpy as np
    verts, pverts, pcount = [], [], []
    for pts, h in polys:
        idx = []
        for x, y in pts:
            idx.append(len(verts))
            verts.append((y, h, x))
        pverts.append(idx + [0] * (6 - len(idx)))
        pcount.append(len(idx))
    verts = np.array(verts, dtype=float)
    pverts = np.array(pverts)
    pmin = np.array([verts[pverts[i, :pcount[i]]].min(axis=0) for i in range(len(polys))])
    pmax = np.array([verts[pverts[i, :pcount[i]]].max(axis=0) for i in range(len(polys))])
    t = SimpleNamespace(key=(0, 0), verts=verts, pverts=pverts, pcount=np.array(pcount), pmin=pmin, pmax=pmax,
                        usable=lambda i: True)
    return SimpleNamespace(tile_at=lambda rx, rz: t, _island=lambda node: False,
                           ground_z=lambda x, y, hint=None: -1.0)


def test_floor_z_sees_a_drop_beside_the_stairs():
    from nb.move import floor_z
    sq = lambda x0, y0, x1, y1: [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    nm = fake_floor_mesh([(sq(0, 0, 10, 10), 392.8),           # the floor: its edge 0.4 yd west of the point
                          (sq(11.5, 0, 14, 10), 399.1),        # the platform 1.5 yd east
                          (sq(5, 0, 15, 10), 423.3)])          # the storey above, over the point
    assert floor_z(nm, 10.4, 5, 399.1) == 392.8                # fell off: the floor beside us, not the platform
    assert floor_z(nm, 11.0, 5, 399.1) == 399.1                # 0.5 yd off the platform edge: still on it
    assert floor_z(nm, 5, 5, 392.8) == 392.8                   # inside, within a stair step: as ground_z
    assert floor_z(nm, 12, 5, 397.0) == 399.1                  # a stair step up (<= FLOOR_CLIMB) is fine
    assert floor_z(nm, 10.4, 5, None) == -1.0                  # no hint: nm.ground_z unchanged
    far = fake_floor_mesh([(sq(0, 0, 10, 10), 392.8)])
    assert floor_z(far, 30, 30, 392.8) == -1.0                 # nothing within FLOOR_REACH: ground_z


def test_floor_z_on_the_real_mesh_at_teo():
    """Bot mesh (skipped without it): inst3 20:30 stood beside Teo's stairs at 392.8 while ground_z(hint) kept the
    platform height, and the plan from there ran straight into the platform's side; floor_z gives the floor and the
    plan goes round the south stairs."""
    try:
        import navmesh
        from nb.move import floor_z
        nm = navmesh.NavMesh(navmesh.BOT_MMAP_DIR, 0)
    except Exception as e:
        print("  (skipped: no bot mesh:", repr(e), ")")
        return
    for (x, y), h in (((-6055.4, 383.2), 399.06), ((-6055.1, 378.4), 397.19), ((-6056.0, 380.0), 397.19)):
        assert nm.ground_z(x, y, hint=h) > 396                   # the old answer: still on the stairs / platform
        z = floor_z(nm, x, y, h)
        assert abs(z - 392.8) < 0.5, (x, y, z)
        p = nm.find_path((x, y, z), (-6050.3, 383.9, 398.96), budget=10)
        assert p and any(float(q[1]) < 373.0 for q in p), p      # round the south side, up the stairs
    for (x, y), h in (((-6052.1, 378.9), 397.19), ((-6051.5, 381.0), 399.1), ((-6104.4, 384.1), 395.7)):
        assert abs(floor_z(nm, x, y, h) - nm.ground_z(x, y, hint=h)) < 0.5   # on the stairs / platform: unchanged


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, d):
        self.t += max(0.0, d)


def walk_mover(positions, z=None, route_z=None, inside=False):
    """A Mover whose character stands at positions[i] (i bumped by each stuck's unstick); goto's clock is virtual."""
    from nb import move
    clock = FakeClock()
    m = Mover.__new__(Mover)
    m.inst, m._wedges, m.logs, m.plans = "t", [], [], []
    m.log = m.logs.append
    pos = {"i": 0}

    def state():
        x, y = positions[min(pos["i"], len(positions) - 1)]
        return SimpleNamespace(x=x, y=y, z=z or 0.0, facing=0.0, dead=False, ghost=False, flags=0)
    m.state = state
    m.drive = lambda err, fwd=1.0: clock.sleep(0.1)
    def unstick(n):
        pos["i"] += 1
        clock.sleep(0.5)
    m.unstick = unstick
    m.escape = lambda tx, ty: clock.sleep(1.4)
    m._note_stuck = lambda s: None
    m.stop = lambda *a, **k: None

    def path(x, y, zz=None):
        m.plans.append((pos["i"], list(m._wedges)))
        return [(x, y, route_z or 0.0)]
    m.path = path
    m.route = lambda s, pts: ([(s.x, s.y, route_z if route_z is not None else s.z)] + [tuple(q) for q in pts],
                              [inside] * (len(pts) + 1), [False] * (len(pts) + 1))
    return m, clock, move


def run_goto(m, clock, move, **kw):
    old = move.time
    move.time = clock
    try:
        return m.goto(-5900.0, 400.0, **kw)
    finally:
        move.time = old


def test_goto_second_stuck_at_the_same_spot_replans_at_once():
    m, clock, move = walk_mover([(-6055.4, 383.2)] * 20)
    assert run_goto(m, clock, move) == "stuck"
    stucks = [l for l in m.logs if l.startswith("stuck at")]
    assert len(stucks) == 9, m.logs
    # plans: the walk's own, then one per stuck from #2 on (each a repeat within WEDGE_NEAR; old code: #3, #6, #9)
    assert len(m.plans) == 1 + 8, m.plans
    assert m.plans[1][1] and len(m.plans[-1][1]) == 1, m.plans           # one wedge hole, not one per stuck
    assert any("wedged again" in l for l in m.logs)


def test_goto_scattered_stucks_keep_every_third_replan():
    pts = [(-6055.0 + 3.0 * k, 383.0) for k in range(20)]           # each stuck 3 yd from the last
    m, clock, move = walk_mover(pts)
    assert run_goto(m, clock, move) == "stuck"
    assert len(m.plans) == 1 + 3, m.plans                              # stucks #3, #6, #9 only (old rule)
    assert not any("wedged again" in l for l in m.logs)


def test_goto_replans_after_dropping_off_the_path_floor():
    m, clock, move = walk_mover([(-6055.1, 378.4)] * 20, z=392.8, route_z=399.1, inside=True)
    run_goto(m, clock, move)
    falls = [l for l in m.logs if "off the path's floor" in l]
    assert len(falls) == move.FALL_MAX, m.logs                         # capped per walk
    assert m.plans[1][0] == 0 and not any(l.startswith("stuck at") for l in m.logs[:1]), m.logs
    m2, clock2, move2 = walk_mover([(-6055.1, 378.4)] * 20, z=398.0, route_z=399.1, inside=True)
    run_goto(m2, clock2, move2)
    assert not any("off the path's floor" in l for l in m2.logs)       # on the path's floor: no replan
    m3, clock3, move3 = walk_mover([(-6055.1, 378.4)] * 20, z=392.8, route_z=399.1, inside=False)
    run_goto(m3, clock3, move3)
    assert not any("off the path's floor" in l for l in m3.logs)       # outdoors: off (hill polygons)


def test_finish_step_interacts_then_bolts_when_hp_stands():
    s = SimpleNamespace(t_within10=True, t_hp_pct=20.0, casting_now=False)
    sw = Bot.swing_state()
    assert Bot.finish_step(s, False, True, 1.0, sw, 100.0) == (True, False)      # weapon on it: hold the bolts
    assert Bot.finish_step(s, False, True, botmod.FINISH_STALL + 0.5, sw, 100.0) == (True, True)   # not landing
    sw["next"] = 200.0
    assert Bot.finish_step(s, False, True, botmod.FINISH_STALL + 0.5, sw, 100.0) == (True, False)  # SWING_RETRY
    assert Bot.finish_step(s, False, True, botmod.FINISH_BOLT + 0.1, sw, 100.0)[0] is False        # bolt it
    s.casting_now = True
    sw["next"] = 0.0
    assert Bot.finish_step(s, False, True, botmod.FINISH_STALL + 0.5, sw, 100.0) == (True, False)  # not mid-cast
    assert Bot.finish_step(s, True, True, 1.0, sw, 100.0)[0] is False                              # caster
    s.t_hp_pct = 60.0
    assert Bot.finish_step(s, False, True, 1.0, sw, 100.0)[0] is False                             # not nearly dead


def test_no_hand_placed_routes_in_move():
    """User rule: paths are computed. No hand-placed trails or gates; the learned-gate list starts empty."""
    import inspect
    from nb import move
    assert not hasattr(move, "TRAILS") and not hasattr(move, "trail_legs") and not hasattr(Mover, "_trail")
    src = inspect.getsource(Mover.__init__)
    assert "self.gates = []" in src, src


def test_server_mesh_fallback_avoids_this_walks_wedges():
    """No bot-mesh route (a hill climb): the server-mesh plan gets the wedge spots, so a replan after a stuck there
    does not find the same route again; without a route round them it plans without them."""
    calls = []
    import nb.move as _mv
    _mv.LEARNED_SPOTS = True      # the learned-hole mechanism (off in the bot) still works when switched on
    _mv.NB_MESH = _mv.navmesh.BOT_MMAP_DIR     # the fallback exists only on the 1.12-era meshes (not the Forever one)
    sys.modules["dangers"] = SimpleNamespace(zones=lambda *a, **k: [])    # no world DB in the test
    m = Mover.__new__(Mover)
    m.logs = []
    m.log = m.logs.append
    m.gates, m.zones, m._wedges = [], [], [(-6100.0, 457.0, 430.0, 1.0)]
    m.state = lambda: SimpleNamespace(x=-6122.0, y=491.0, z=402.0, flags=0, level=5)
    m.nm = SimpleNamespace(ground_z=lambda x, y, hint=None: 400.0, island_exit=lambda p, r: None,
                           find_path=lambda a, b, **kw: None, tile_at=lambda rx, rz: None)
    m.floors_at = lambda x, y: []
    m.bad = SimpleNamespace(active=lambda bbox: [])

    def srv(a, b, **kw):
        calls.append(kw)
        return [a, b] if (kw.get("holes") or ok_without[0]) else None
    m.nm_server = SimpleNamespace(ground_z=lambda x, y: 509.0, find_path=srv)
    ok_without = [False]
    p = m._path(-6013.0, 468.0, 509.0)
    assert p and calls[0].get("holes") == ((-6100.0, 457.0, 430.0, 1.0),), calls
    calls.clear()
    m.nm_server = SimpleNamespace(ground_z=lambda x, y: 509.0,
                                  find_path=lambda a, b, **kw: (calls.append(kw), None if kw.get("holes") else [a, b])[1])
    p = m._path(-6013.0, 468.0, 509.0)
    assert p and len(calls) == 2 and "holes" not in calls[1], calls
    _mv.LEARNED_SPOTS = False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
