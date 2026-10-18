"""Offline checks of nb/resources.py: the pull / rest / drink lines as multiples of measured costs
(python -m nb.test_resources)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import resources as rs           # noqa: E402
from nb import classes                   # noqa: E402
classes.PROFILE = classes.get("shaman")  # a healer's bar (Healing Wave); test_no_heal_class_eats: the hunter
from nb import threat                    # noqa: E402

LOGS: list[str] = []
HW2, LB2 = (2, 332, 45), (1, 529, 30)       # Healing Wave 2 / Lightning Bolt 2 (rotation.SPELL_IDS costs)


def st(level=7, hp=100.0, mana=100.0, manamax=300, t_hp=100.0):
    return SimpleNamespace(level=level, hp_pct=hp, mana_pct=mana, mana=mana * manamax / 100.0, manamax=manamax,
                           t_hp_pct=t_hp, plates=[])


def model(costs=(), bar=None, multi=False, boss=False, melee=()):
    return rs.Resources(costs=lambda: costs, bar=lambda s: bar or {}, multi=lambda: multi, boss=lambda s: boss,
                        melee=lambda: melee, log=LOGS.append)


def test_defaults_without_measurements():
    """No fights measured, no bar: the old bot.py numbers."""
    r = model()
    ln = r.lines(st())
    assert (ln.pull_hp, ln.pull_mana, ln.exit_hp, ln.exit_mana, ln.boss) == (85, 80, 90, 85, False), ln
    assert r.lines(st(level=4))[1:4:2] == (45, 65)               # levels 1-5: 45 / 65
    assert r.ready(st(hp=85, mana=80)) and not r.ready(st(hp=84, mana=99)) and not r.ready(st(hp=99, mana=79))
    assert r.rest_target(st()) == (90, 85)
    assert r.drink_line(st()) == 20.0
    assert not r.rest_heal_ok(st(mana=100))                        # no heal on the bar: no rest heal (eat)
    assert r.melee_kill_hp(12) == rs.MELEE_KILL_HP
    assert r.affordable(st(mana=1), "Lightning Bolt")             # cost unknown: no gate


def test_pull_line_is_kill_cost_plus_a_heal():
    costs = [(7, c) for c in (55, 60, 62, 65, 70, 71)] + [(5, 99)]
    # without a bar: median 65 + the old 15 -> 80 / 85
    r = model(costs)
    ln = r.lines(st())
    assert (ln.pull_mana, ln.exit_mana) == (80.0, 85.0), ln
    assert any("rest line: level 7 mana 80%" in m for m in LOGS), LOGS
    # with the bar: + one Healing Wave (45 of 300 = 15%) -> the same here; a bigger pool -> a lower line
    assert model(costs, {"Healing Wave": HW2}).lines(st()).pull_mana == 80.0
    assert model(costs, {"Healing Wave": HW2}).lines(st(manamax=450)).pull_mana == 75.0
    # cheap fights: clamped to PULL_LO; dear ones to PULL_HI (exit at most EXIT_HI)
    assert model([(7, 10)] * 6, {"Healing Wave": HW2}).lines(st()).pull_mana == rs.PULL_LO
    hi = model([(7, 95)] * 6).lines(st())
    assert (hi.pull_mana, hi.exit_mana) == (rs.PULL_HI, rs.EXIT_HI)
    # 4 fights: still the default
    assert model([(7, 30)] * 4).lines(st()).pull_mana == 80.0


def test_multi_and_boss_lines():
    r = model([(7, 30)] * 6, multi=True)
    ln = r.lines(st())
    assert (ln.pull_mana, ln.exit_mana) == (80.0, 85.0), ln           # after a 2+ mob fight: the flat line at least
    b = model(boss=True).lines(st())
    assert (b.pull_hp, b.pull_mana, b.exit_hp, b.exit_mana, b.boss) == (95, 90, 95, 95, True)


def test_drink_and_heal_lines_from_spell_costs():
    bar = {"Healing Wave": HW2, "Lightning Bolt": LB2}
    r = model(bar=bar)
    assert r.drink_line(st(manamax=300)) == 15.0                       # one heal's worth: 45 / 300
    assert r.drink_line(st(manamax=150)) == 20.0                       # never above 20 (heal, don't eat)
    assert r.drink_line(st(manamax=900)) == rs.DRINK_MIN
    assert r.rest_heal_ok(st(mana=23)) and not r.rest_heal_ok(st(mana=22))   # 1.5 heals = 22.5%
    assert r.heal_ok(st(mana=15)) and not r.heal_ok(st(mana=14))
    assert not model().heal_ok(st())                                   # no heal on the bar
    assert r.affordable(st(mana=10), "Lightning Bolt") and not r.affordable(st(mana=9), "Lightning Bolt")
    assert rs.spell_cost(bar, "Earth Shock") == 0.0 and rs.can_afford(st(mana=0), bar, "Earth Shock")


def test_bolt_floor():
    bar = {"Healing Wave": HW2}
    r = model(bar=bar)
    assert r.bolt_floor(st(hp=90)) == 15.0
    assert r.bolt_floor(st(hp=50)) == 20.0                             # hurt: a heal (15%) + 5
    assert r.bolt_floor(st(hp=90), attackers=2) == 20.0
    assert model().bolt_floor(st(hp=50)) == 26.0                       # no bar: the old 26
    assert r.bolt_floor(st(hp=50, mana=10)) == 0.0                     # no heal affordable: the floor only idles us
    assert r.bolt_floor(st(hp=50, t_hp=20)) == 0.0
    assert r.bolt_floor(st(hp=50, mana=10), zero=False) == 20.0        # (the ranged branch keeps it)


def test_m1_and_melee_kill():
    costs = [(12, c) for c in (30, 32, 35, 36, 40)]
    r = model(melee=[(12, 30.0), (12, 35.0), (11, 40.0), (9, 1.0)])
    assert abs(r.melee_kill_hp(12) - 42.0) < 1e-6                     # 1.2 x the median of the 3 within a level
    assert model(melee=[(12, 30.0), (12, 35.0)]).melee_kill_hp(12) == rs.MELEE_KILL_HP   # too few


def test_bot_attr_reads_the_bot():
    from nb import bot as botmod
    from nb.bot import Bot
    botmod.log = LOGS.append
    b = Bot.__new__(Bot)
    b.fight_costs = [(8, 40.0)] * 6
    b.last_multi = False
    b.wanted = set()
    b.rot = SimpleNamespace(bar=lambda s: {"Healing Wave": HW2})
    s = st(level=8)
    assert b.res.lines(s).pull_mana == 55.0                            # 40 + 15
    assert b.rest_line(s)[:4] == (85.0, 55.0, 90.0, 60.0)
    b.last_multi = True
    assert b.rest_line(s)[1] == 80.0


def test_no_heal_class_eats():
    """A class without a heal (the hunter): no rest heal on any mana, the drink line stays 20%; a rage class drinks
    never and has no mana lines."""
    old = classes.PROFILE
    try:
        classes.PROFILE = classes.get("hunter")
        r = model(bar={"Auto Shot": (1, 75, 0), "Raptor Strike": (2, 2973, 15)})
        assert rs.heal_name() is None and not r.rest_heal_ok(st(mana=100)) and r.heal_pct(st()) is None
        assert r.drink_line(st()) == 20.0
        classes.PROFILE = classes.get("warrior")
        r = model()
        assert r.drink_line(st()) == 0.0 and r.lines(st(level=9)).pull_mana == 0.0
    finally:
        classes.PROFILE = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
