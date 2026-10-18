"""Offline checks of nb/engage.py (the fight's progress tracker) and nb/util.py, driven by synthetic state
sequences on a fake clock (python -m nb.test_engage)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import engage                    # noqa: E402
from nb.util import Deadline, Cooldown   # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def st(t_hp=100.0, on_me=False, w10=False, w28=False, combat=False, err=0, errn=0, x=0.0, y=0.0, t_x=None, t_y=None):
    return SimpleNamespace(t_hp_pct=t_hp, t_on_me=on_me, t_within10=w10, t_within28=w28, t_in_ranged=w28,
                           combat=combat, err=err, errn=errn, x=x, y=y, t_x=t_x, t_y=t_y)


def pl(sy=300, sx=900):
    return SimpleNamespace(sx=sx, sy=sy)


def run(eg, clk, seq, pulled=False):
    """seq: [(t, state, plate)] -> [(t, Action)] of every check() that fired."""
    out = []
    for t, s, p in seq:
        clk.t = t
        eg.update(s, p)
        a = eg.check(s, pulled)
        if a:
            out.append((t, a))
    return out


def test_never_reached_gives_up_after_12s_without_closing():
    clk = Clock()
    clk.t = 1000.0
    eg = engage.Engage(clock=clk)
    seq = [(1000 + k, st(), pl(300)) for k in range(14)]
    acts = run(eg, clk, seq)
    assert acts and acts[0][0] == 1013 and acts[0][1].kind == "giveup", acts
    assert "never reached it in 12 s" in acts[0][1].why


def test_closing_in_keeps_the_approach_alive():
    """A long walk to the mob: the plate keeps dropping on screen (closer) - no give-up at 12 s any more."""
    clk = Clock()
    clk.t = 1000.0
    eg = engage.Engage(clock=clk, focal=900.0)
    seq = [(1000 + k, st(), pl(300 + 20 * k)) for k in range(20)]           # +20 px/s >= 18 px (0.02 x 900)
    assert run(eg, clk, seq) == []
    # ... a better range band counts too
    clk.t = 2000.0
    eg2 = engage.Engage(clock=clk)
    seq = [(2000 + k, st(w28=k >= 10), None) for k in range(25)]
    acts = run(eg2, clk, seq)
    assert acts and acts[0][0] == 2023, acts                            # 12 s after the band change at 2010
    # ... and a known position closing
    clk.t = 3000.0
    eg3 = engage.Engage(clock=clk)
    seq = [(3000 + k, st(t_x=60.0 - 3 * k, t_y=0.0), None) for k in range(18)]
    assert run(eg3, clk, seq) == []


def test_no_damage_after_pull_and_far_target():
    clk = Clock()
    clk.t = 1000.0
    eg = engage.Engage(clock=clk)
    seq = [(1000 + k, st(t_hp=84.0), pl(300)) for k in range(17)]           # engaged (under 99%) but nothing lands
    acts = run(eg, clk, seq, pulled=True)
    assert acts[0][0] == 1016 and "no damage on it and not on us in 15 s" in acts[0][1].why, acts
    clk.t = 2000.0
    eg2 = engage.Engage(far=True, clock=clk)
    acts = run(eg2, clk, [(2000 + k, st(), None) for k in range(27)])
    assert acts[0][0] == 2026 and "25 s" in acts[0][1].why, acts


def test_line_of_sight_count():
    clk = Clock()
    clk.t = 1000.0
    eg = engage.Engage(clock=clk)
    seq = [(1000.0, st(errn=1), None), (1000.5, st(err=3, errn=2), None), (1001.0, st(err=3, errn=2), None),
           (1001.5, st(err=3, errn=3), None), (1002.0, st(err=3, errn=4), None)]
    acts = run(eg, clk, seq)
    assert len(acts) == 1 and acts[0][0] == 1002.0 and "line of sight" in acts[0][1].why, acts
    # engaged first: errors no longer count
    clk.t = 2000.0
    eg2 = engage.Engage(clock=clk)
    seq = [(2000.0, st(t_hp=90.0, on_me=True, w10=True, errn=1), None)] + \
          [(2000.5 + k, st(t_hp=90.0 - 2 * k, on_me=True, w10=True, err=3, errn=2 + k), None) for k in range(4)]
    assert run(eg2, clk, seq) == []


def test_stall_ladder_in_melee():
    """On us in melee, hp not dropping: face at 5 s, interact-walk at 8, reposition at 11, then the ladder starts
    over; a mob on us is never given up here."""
    clk = Clock()
    clk.t = 1000.0
    eg = engage.Engage(clock=clk)
    seq = [(1000.0, st(t_hp=80, on_me=True, w10=True), pl())]
    seq += [(1000.0 + 0.5 * k, st(t_hp=80, on_me=True, w10=True), pl()) for k in range(1, 60)]
    acts = run(eg, clk, seq)
    kinds = [(t, a.kind) for t, a in acts]
    assert kinds[:4] == [(1005.0, "face"), (1008.0, "interact"), (1011.0, "reposition"), (1016.0, "face")], kinds
    assert all(a.kind != "giveup" for _t, a in acts)


def test_stall_ladder_resets_on_damage_and_gives_up_off_us():
    clk = Clock()
    clk.t = 1000.0
    eg = engage.Engage(clock=clk)
    seq = [(1000.0 + k, st(t_hp=80 - (3 if k >= 4 else 0), on_me=True, w10=True), pl()) for k in range(9)]
    acts = run(eg, clk, seq)
    assert acts == [], acts                                  # the hp drop at +4 s restarted the clock (face due at +9)
    # hit, not on us, combat flag up (another mob holds us): face 12, interact 15, reposition 18, give up 21
    clk.t = 2000.0
    eg2 = engage.Engage(clock=clk)
    seq = [(2000.0, st(t_hp=60, on_me=True), pl())] + [(2000.0 + k, st(t_hp=60, combat=True), pl()) for k in range(1, 23)]
    kinds = [(t, a.kind) for t, a in run(eg2, clk, seq)]
    assert kinds == [(2012.0, "face"), (2015.0, "interact"), (2018.0, "reposition"), (2021.0, "giveup")], kinds


def test_come_wait_and_finish_clock():
    clk = Clock()
    clk.t = 1000.0
    eg = engage.Engage(clock=clk)
    run(eg, clk, [(1000.0, st(on_me=True), pl(300)), (1003.0, st(on_me=True, w28=True), pl(300))])
    clk.t = 1008.0
    assert not eg.come_wait() and not eg.come_wait(caster=True)
    clk.t = 1009.5
    assert eg.come_wait(caster=True) and not eg.come_wait()          # a caster stands off: walk to it after 6 s
    clk.t = 1015.5
    assert eg.come_wait()
    # the melee finish's clock: since its hp last dropped
    clk.t = 2000.0
    eg2 = engage.Engage(clock=clk)
    run(eg2, clk, [(2000.0, st(t_hp=20, on_me=True, w10=True), None), (2001.0, st(t_hp=18, on_me=True, w10=True), None)])
    clk.t = 2004.5
    assert abs(eg2.hp_still() - 3.5) < 1e-9
    sw = {"n": 0, "next": 0.0, "hold": 0.0, "said": False}
    assert engage.finish_still_step(True, 3.5, False, 2004.5, sw)
    assert not engage.finish_still_step(True, 2.0, False, 2004.5, sw)
    assert not engage.finish_still_step(True, 3.5, True, 2004.5, sw)


def test_swing_step_timing():
    sw = {"n": 0, "next": 0.0, "hold": 0.0, "said": False}
    assert engage.swing_step(sw, False, False, True, 10.0) == "attack" and sw["next"] == 10.0 + engage.SWING_RETRY
    assert engage.swing_step(sw, False, False, True, 10.5) == "wait"
    assert engage.swing_step(sw, False, False, True, 11.3) == "interact"
    assert engage.swing_step(sw, False, True, True, 20.0) == "wait"          # never into a running cast
    t = 20.0
    while sw["n"] < engage.SWING_TRIES:
        assert engage.swing_step(sw, False, False, True, t) in ("interact", "wait")
        t += engage.SWING_RETRY
    assert engage.swing_step(sw, False, False, True, t) == "attack_retry"
    assert engage.swing_step(sw, False, False, True, t + 1.0) == "dead"
    assert engage.swing_step(sw, True, False, True, t + 2.0) == "on" and sw["n"] == 0


def test_chase_progress():
    clk = Clock()
    clk.t = 1000.0
    ch = engage.Chase(clock=clk)
    for k in range(7):                                       # runs away: the plate rises on screen, hp unchanged
        clk.t = 1000.0 + k
        ch.update(st(t_hp=15), pl(400 - 10 * k))
    assert not ch.stalled()
    clk.t = 1008.5
    ch.update(st(t_hp=15), pl(330))
    assert ch.stalled()
    clk.t = 2000.0
    ch2 = engage.Chase(clock=clk)
    for k in range(20):                                      # we gain on it: closer every 2 s
        clk.t = 2000.0 + k
        ch2.update(st(t_hp=15), pl(300 + 10 * k))
        assert not ch2.stalled()
    assert ch2.press_due() and not ch2.press_due()
    clk.t += engage.CHASE_PRESS_S
    assert ch2.press_due()


def test_util_deadline_cooldown():
    clk = Clock(5.0)
    clk.time = clk                   # nb.util reads clock.time()
    d = Deadline(10, clock=clk)
    assert not d.passed() and d.left() == 10
    clk.t = 12.0
    assert d.running() and abs(d.left() - 3.0) < 1e-9 and d.elapsed() == 7.0
    clk.t = 15.1
    assert d.passed()
    d.restart()
    assert not d.passed() and d.left() == 10
    c = Cooldown(2.5, clock=clk)
    assert c.ready()                 # first call fires and arms it
    assert not c.ready()
    clk.t = 17.7
    assert c.ready()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
