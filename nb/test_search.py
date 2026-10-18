"""Offline tests for the search mode (Bot.search_here / sweep / name_target) with a fake world.

Run: python -m nb.test_search   (from the wowbot folder). Touches no game window.
"""

from __future__ import annotations

import math
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import bot as botmod            # noqa: E402
from nb.bot import Bot, FOCAL           # noqa: E402
from nb.eye import Plate                # noqa: E402

W = 1920
botmod.log = lambda m: LOGS.append(m)
LOGS: list[str] = []
# the live nb/STOP file (a stopped instance 1) ends sweep() at once: the tests must not depend on it
botmod.STOP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "STOP.test-none")


def plate(npc, sx, ghash, **kw):
    d = dict(sx=sx, sy=500, npc=npc, level=3, hp=100, hostile=True, dead=False, on_me=False, is_target=False,
             tapped=False, combat=False, player=False, elite=False, friendly=False, token=1, ghash=ghash)
    d.update(kw)
    return Plate(**d)


class FakeWorld:
    """Camera yaw (+ = left) turned by the pad; units sit at world bearings (+ = left of yaw 0)."""

    def __init__(self, units, k=0.8, offscreen=False):
        self.units, self.k, self.offscreen = units, k, offscreen   # offscreen: units behind us still have plates          # {ghash: (npc, bearing)}; k = real camera rate / commanded
        self.yaw, self.rate, self.t = 0.0, 0.0, time.time()
        self.pans = []

    def advance(self):
        now = time.time()
        self.yaw += self.rate * self.k * (now - self.t)
        self.t = now

    def state(self):
        self.advance()
        plates = []
        for gh, (npc, b) in self.units.items():
            rel = (b - self.yaw + math.pi) % (2 * math.pi) - math.pi      # + = left of the camera
            if abs(rel) < math.atan(W / 2 / FOCAL):
                sx = W / 2 - math.tan(rel) * FOCAL
                plates.append(plate(npc, int(sx), gh))
            elif self.offscreen:
                plates.append(plate(npc, -1, gh))
        return SimpleNamespace(plates=plates, combat=False, dead=False, level=3, hp_pct=100.0, mana_pct=100.0,
                               x=0.0, y=0.0, has_target=False, t_dead=False, t_npc=0, t_attackable=False,
                               t_tapped=False, t_player=False, t_level=0, t_hash=0)


class FakeMover:
    def __init__(self, world, pad=True):
        self.w, self.pad, self.stops, self.nudges, self.done = world, pad, 0, 0, 0

    def padding(self):
        return self.pad

    def pan(self, rate):
        if not self.pad:
            return False
        self.w.advance()
        self.w.rate = rate
        self.w.pans.append(rate)
        return True

    def aim_done(self):
        self.w.advance()
        self.w.rate = 0.0
        self.done += 1

    def stop(self):
        self.stops += 1

    def nudge(self, t=0.15):
        self.nudges += 1

    def state(self):
        return self.w.state()


def make_bot(world, pad=True, wanted=(946,)):
    b = Bot.__new__(Bot)
    b.mv = FakeMover(world, pad)
    b.W = W
    b.wanted = set(wanted)
    b.grind_ok = False
    b.blacklist = {}
    b.avoid = []
    b.searched, b.find_names, b.cam_off = {}, None, False
    b.pan_gain, b.sweep_sign, b.m_t = 1.0, -1, 0.0
    b.s = world.state
    b.qdb = SimpleNamespace(names={946: "Frostmane Seer", 947: "Other"})
    b.keys = SimpleNamespace(chat=lambda l: CHAT.append(l), press=lambda k, hold=0.04: PRESSED.append(k))
    return b


CHAT: list[str] = []
PRESSED: list[str] = []


def test_sweep_finds_plate_behind():
    LOGS.clear()
    w = FakeWorld({1: (299, 1.0), 2: (946, math.pi)})          # wanted mob straight behind us
    b = make_bot(w)
    t0 = time.time()
    how = b.search_here(10, 10)
    assert how == "plate", how
    assert b.cam_off
    assert time.time() - t0 < 4.5, time.time() - t0
    assert any("sweep: measured" in m for m in LOGS), LOGS
    assert 0.4 <= b.pan_gain <= 2.5
    # camera rate was 0.8x the command: the gain must have moved towards 0.8
    assert b.pan_gain < 1.0, b.pan_gain
    assert w.rate == 0.0                                         # stick released
    assert any(m.startswith("arrive") for m in LOGS)
    # the same cell is skipped for 120 s
    assert b.search_here(5, 5) == "skip"


def test_offscreen_plate_is_enough():
    LOGS.clear()
    w = FakeWorld({2: (946, math.pi)}, offscreen=True)
    b = make_bot(w)
    assert b.search_here(10, 10) == "plate"
    assert w.pans == []                                          # no sweep needed


def test_sweep_nothing_then_name(with_plate=True):
    LOGS.clear(); CHAT.clear(); PRESSED.clear()
    w = FakeWorld({1: (299, 1.0), 3: (299, -2.0)})
    b = make_bot(w)
    state = w.state

    def keyed(k, hold=0.04):
        PRESSED.append(k)
        if k == "num*":
            def st():
                s = state()
                s.has_target, s.t_npc, s.t_attackable, s.t_level, s.t_hash = True, 946, True, 3, 77
                if with_plate:        # name_target rejects a hit without a nameplate (beyond plate range)
                    s.plates.append(plate(946, 1342, 77, is_target=True))
                return s
            b.s = st
    b.keys.press = keyed
    how = b.search_here(50, 50)
    if not with_plate:
        assert how == "far", (how, LOGS)                  # roam() moves on to another point (REPORT.md change 2)
        assert any("out of plate range" in m for m in LOGS), LOGS
        assert PRESSED[-1] == "f11", PRESSED               # the rejected target is dropped again
        return
    assert how == "name", (how, LOGS)
    assert CHAT == ["/be find Frostmane Seer"], CHAT
    assert PRESSED[:2] == ["f11", "num*"], PRESSED
    assert b.mv.nudges == 1                                     # sweep ended empty: character realigned
    assert any("HIT npc 946" in m and "plate=on" in m for m in LOGS), LOGS
    # names unchanged: no second chat line
    b.searched.clear()
    b.search_here(50, 50)
    assert len(CHAT) == 1


def test_name_target_without_plate_is_rejected():
    test_sweep_nothing_then_name(with_plate=False)


def test_no_pad_skips_sweep():
    LOGS.clear(); CHAT.clear()
    w = FakeWorld({1: (299, 1.0)})
    b = make_bot(w, pad=False)
    assert b.sweep() is None
    assert w.pans == []


def test_toggles_off():
    LOGS.clear(); CHAT.clear()
    botmod.SEARCH_SWEEP = botmod.SEARCH_NAME = False
    try:
        w = FakeWorld({1: (299, 1.0)})
        b = make_bot(w)
        assert b.search_here(1, 1) is None
        assert w.pans == [] and CHAT == []
    finally:
        botmod.SEARCH_SWEEP = botmod.SEARCH_NAME = True


def test_seen_helpers():
    w = FakeWorld({2: (946, math.pi)}, offscreen=True)
    b = make_bot(w)
    s = w.state()
    assert [p.npc for p in b.wanted_plates(s)] == [946]          # off-screen (sx -1) counts
    assert s.plates[0].sx == -1
    assert not b.far_stronger(s)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("\n".join(LOGS[-6:]))
