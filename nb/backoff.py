"""Backoff: one registry for the bot's TTL'd "leave it alone" tables (review 2026-10-15, phase 1 step B).

bot.py had a dozen ad-hoc dicts for the same idea - blacklist (plate -> until), pick_npc_fail / pick_npc_skip,
no_offer + target_miss / miss_talks / owed_tries, qdb.gfail, guard_skip, noloot, vendor_retry / train_retry /
imbue_retry - each with its own get(k, 0) > time.time() test and its own escalation code. A Backoff keeps, per key:

  until  - the time before which the key is blocked (set() overwrites it, as the old dict assignments did)
  count  - a failure tally (bump() / fail()), kept when the block runs out: an escalating schedule still escalates
           on the next failure (talk(): a 2nd target miss shelves an NPC 1 h, however long after the 1st)

A tally may live under another key than the block it drives (fail(key, ..., tally=(...))): talk()'s three counters
all shelve the same NPC key. Keys are ints, strings or tuples; to_json() / from_json() keep them round-trippable.
"""

from __future__ import annotations

import time


def _key(k):
    """JSON turns tuples into lists: back to (hashable) tuples."""
    return tuple(_key(x) for x in k) if isinstance(k, list) else k


class Backoff:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._until: dict = {}     # key -> until (wall clock)
        self._n: dict = {}         # key -> failures so far (bump / fail; ok / reset clear it)

    # ---------------------------------------------------------------- blocks
    def set(self, key, seconds: float, now: float | None = None) -> float:
        """Block key for `seconds` from now (a fixed block; replaces any block it had). -> the until time."""
        u = self._until[key] = (self.clock() if now is None else now) + seconds
        return u

    def blocked(self, key, now: float | None = None) -> bool:
        return self._until.get(key, 0.0) > (self.clock() if now is None else now)

    def until(self, key) -> float:
        """When key's block ends (0.0: never blocked; a past time once it ran out and before prune())."""
        return self._until.get(key, 0.0)

    def unblock(self, key):
        """End key's block now; its tally stays."""
        self._until.pop(key, None)

    # ---------------------------------------------------------------- tallies
    def count(self, key) -> int:
        return self._n.get(key, 0)

    def bump(self, key) -> int:
        """One more failure on key (no block). -> the new count."""
        n = self._n[key] = self._n.get(key, 0) + 1
        return n

    def reset(self, key):
        """Forget key's tally; a running block stays."""
        self._n.pop(key, None)

    def fail(self, key, schedule=(600, 3600), now: float | None = None, tally=None) -> float:
        """One more failure: bump the tally (key's own, or `tally`) and block key for schedule[n - 1] s, the last entry
        for every failure past the schedule's length. -> the block's length in s."""
        n = self.bump(key if tally is None else tally)
        ttl = schedule[min(n, len(schedule)) - 1]
        self.set(key, ttl, now)
        return ttl

    def ok(self, key, tally=None):
        """Success: key's block and its tally (or `tally`) are gone."""
        self._until.pop(key, None)
        self._n.pop(key if tally is None else tally, None)

    # ---------------------------------------------------------------- housekeeping
    def prune(self, now: float | None = None) -> int:
        """Drop blocks that ran out (tallies stay: see the module doc). -> how many were dropped."""
        now = self.clock() if now is None else now
        dead = [k for k, u in self._until.items() if u <= now]
        for k in dead:
            del self._until[k]
        return len(dead)

    def __iter__(self):
        """The keys with a block entry (live or not yet pruned), like iterating the old {key: until} dict."""
        return iter(list(self._until))

    def __len__(self):
        return len(self._until)

    def __contains__(self, key):
        return key in self._until

    def __repr__(self):
        return f"Backoff(until={self._until!r}, n={self._n!r})"

    # ---------------------------------------------------------------- persistence
    def to_json(self, now: float | None = None) -> list:
        """[[key, until], ...] for the live blocks, [key, until, n] where the key also has a tally (absolute
        wall-clock untils, as guide_state.json's "blacklist" always was; a 2-column table reads in the old code too)."""
        now = self.clock() if now is None else now
        out = []
        for k, u in self._until.items():
            if u > now:
                n = self._n.get(k)
                out.append([k, u] if not n else [k, u, n])
        return out

    @classmethod
    def from_json(cls, rows, now: float | None = None, clock=time.time, key=_key) -> "Backoff":
        """The inverse of to_json (rows of 2 or 3); blocks already over are dropped. `key` maps a saved key back
        (default: lists -> tuples; bot.py passes int for the plate hashes)."""
        b = cls(clock)
        b.load(rows, now, key)
        return b

    def load(self, rows, now: float | None = None, key=_key) -> int:
        """Add to_json rows to this registry (later rows win). -> how many blocks were taken."""
        now = self.clock() if now is None else now
        n = 0
        for r in rows or ():
            k, u = key(r[0]), float(r[1])
            if u > now:
                self._until[k] = u
                n += 1
                if len(r) > 2 and r[2]:
                    self._n[k] = int(r[2])
        return n

    @classmethod
    def from_dict(cls, d, clock=time.time) -> "Backoff":
        """{key: until} (the old dict form: tests assign b.blacklist = {} on a Bot built with __new__)."""
        b = cls(clock)
        b._until.update(d or {})
        return b


class BackoffAttr:
    """Bot attribute that holds a Backoff, made on first use: tests build Bot with __new__ (no __init__), which the old
    code met with self.__dict__.setdefault("pick_npc_skip", {}). Assigning a dict ({key: until}) converts it."""

    def __set_name__(self, owner, name):
        self.slot = "_bo_" + name

    def __get__(self, obj, cls=None):
        if obj is None:
            return self
        b = obj.__dict__.get(self.slot)
        if b is None:
            b = obj.__dict__[self.slot] = Backoff()
        return b

    def __set__(self, obj, value):
        obj.__dict__[self.slot] = value if isinstance(value, Backoff) else Backoff.from_dict(value)
