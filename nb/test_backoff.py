"""Offline checks of nb/backoff.py and the bot tables ported onto it (no game, no DB, no file writes).

Run: python -m nb.test_backoff   (from the wowbot folder).
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import bot as botmod            # noqa: E402
from nb.backoff import Backoff, BackoffAttr   # noqa: E402
from nb.bot import Bot                  # noqa: E402

LOGS: list[str] = []
botmod.log = lambda m: LOGS.append(m)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_set_blocked_until():
    c = Clock()
    b = Backoff(c)
    assert not b.blocked(1) and b.until(1) == 0.0
    b.set(1, 300)
    assert b.blocked(1) and b.until(1) == 1300.0
    c.t = 1299.9
    assert b.blocked(1)
    c.t = 1300.0
    assert not b.blocked(1)                                  # until is exclusive, as the old `until > now`
    b.set(1, 15)                                             # a fixed block replaces a longer one (old dict assignment)
    assert b.until(1) == 1315.0
    b.unblock(1)
    assert not b.blocked(1)


def test_fail_escalates_and_tally_outlives_the_block():
    c = Clock()
    b = Backoff(c)
    assert b.fail("k", (120, 600, 3600)) == 120 and b.until("k") == 1120
    c.t = 5000.0                                             # the block ran out long ago
    b.prune()
    assert "k" not in b and b.count("k") == 1
    assert b.fail("k", (120, 600, 3600)) == 600
    assert b.fail("k", (120, 600, 3600)) == 3600
    assert b.fail("k", (120, 600, 3600)) == 3600              # past the schedule: its last entry
    b.ok("k")
    assert not b.blocked("k") and b.count("k") == 0
    assert b.fail("k", (120, 600, 3600)) == 120


def test_tally_under_another_key():
    """talk(): three tallies shelve one NPC key."""
    c = Clock()
    b = Backoff(c)
    assert b.fail(7, (600, 3600), tally=("target_miss", 7)) == 600
    assert b.count(("target_miss", 7)) == 1 and b.count(7) == 0
    b.set(7, 60)                                             # another cause (miss_talks) shortens the block
    assert b.fail(7, (600, 3600), tally=("target_miss", 7)) == 3600
    b.reset(("target_miss", 7))
    assert b.count(("target_miss", 7)) == 0 and b.blocked(7)   # reset keeps the running block
    assert b.bump(("owed", 7)) == 1 and b.bump(("owed", 7)) == 2 and not b.blocked(("owed", 7))


def test_json_round_trip():
    c = Clock()
    b = Backoff(c)
    b.set(5, 100)
    b.fail((218, 3080, (1.0, 2.0)), (120, 600))                # a gather key: nested tuple
    b.set(6, 10)
    c.t = 1050.0                                             # 6 ran out: not saved
    rows = json.loads(json.dumps(b.to_json()))
    assert sorted(map(str, rows)) == sorted(map(str, [[5, 1100.0], [[218, 3080, [1.0, 2.0]], 1120.0, 1]]))
    b2 = Backoff.from_json(rows, clock=c)
    assert b2.blocked(5) and b2.blocked((218, 3080, (1.0, 2.0))) and not b2.blocked(6)
    assert b2.count((218, 3080, (1.0, 2.0))) == 1 and b2.count(5) == 0
    c.t = 1200.0
    assert len(Backoff.from_json(rows, clock=c)) == 0         # everything over by now


def test_old_blacklist_format_both_ways():
    """guide_state.json "blacklist" stays [[ghash, until]]: an old save loads, and the new save reads in the old
    code's loop (`for g, u in ...`), so a rollback keeps the rest of the restore."""
    old = [[123, 2000.0], ["456", 2100.0], [789, 10.0]]
    b = Backoff.from_json(old, now=1000.0, key=int)
    assert set(b) == {123, 456} and b.until(456) == 2100.0
    for g, u in b.to_json(now=1000.0):                       # the old restore_state loop
        assert int(g) in (123, 456) and float(u) > 1000.0


def test_backoff_attr_lazy_and_dict_coercion():
    class Owner:
        tab = BackoffAttr()
    o = Owner.__new__(Owner)
    assert isinstance(o.tab, Backoff) and o.tab is o.tab       # made on first use, then kept
    o.tab = {3: 1e12}                                         # the old dict form (tests: b.blacklist = {})
    assert isinstance(o.tab, Backoff) and o.tab.blocked(3)
    b = Backoff()
    o.tab = b
    assert o.tab is b


def test_bot_tables_on_a_bare_bot():
    b = Bot.__new__(Bot)
    for name in ("blacklist", "pick_npc", "no_offer", "gfail", "guard_skip", "noloot", "retry"):
        assert isinstance(getattr(b, name), Backoff), name
    b.blacklist = {}
    assert not b.blacklist
    assert Bot.GATHER_BACKOFF == (120, 600, 3600)
    assert botmod.EVENT_NPCS == (15732, 16484) and botmod.EVENT_NPC_S == 6 * 3600


def test_bot_imbue_retry():
    b = Bot.__new__(Bot)
    st = SimpleNamespace(weapon_buff=False, weapon_secs=0)
    assert b.imbue_due(st)
    b.retry.set("imbue", 20)
    assert not b.imbue_due(st)
    b.retry.unblock("imbue")
    assert b.imbue_due(st)


def test_bot_save_restore_blacklist():
    saved = {}
    old_save = botmod.save_state
    botmod.save_state = lambda d: saved.update(d)          # no write into a live bot's guide_state.json
    try:
        b = Bot.__new__(Bot)
        b.state_t, b.guide, b._saved_guide = 0.0, None, {}
        b.qdb = SimpleNamespace(shelved={}, skip_lvl={}, skip_q=set(), skip=lambda q, lvl: None)
        b.hunt_watch, b.want_seen_t, b.visited, b.fight_costs = {}, 0.0, {}, []
        b.blacklist.set(4242, 300)
        b.blacklist.set(99, -1)                              # over: pruned, not saved
        b.save_state(force=True)
        assert saved["blacklist"] == [[4242, b.blacklist.until(4242)]] and 99 not in b.blacklist
        b2 = Bot.__new__(Bot)
        b2.qdb, b2.hunt_watch, b2.visited, b2.fight_costs = b.qdb, {}, {}, []
        b2.restore_state(json.loads(json.dumps(saved)))
        assert b2.blacklist.blocked(4242) and set(b2.blacklist) == {4242}
        assert any("blacklist 1" in m for m in LOGS), LOGS[-3:]
    finally:
        botmod.save_state = old_save


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
