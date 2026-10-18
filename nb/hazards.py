"""Hazards: one store for the bot's "dangerous spots" (review 2026-10-15, phase 2 step 1).

bot.py kept four lists for the same idea - self.avoid (death / flee / danger circles, + AVOID_*_R appends in on_death,
flee and run()), nb/death_spots.json (note_shared_death / shared_deaths: the other bots' deaths), self.hunt_skip (hunt
points roam() leaves alone) and elite_fight.json (a chest guard that fought us) - each with its own expiry filter and
its own copy of "is (x, y) inside one". A Hazard is (kind, x, y, r, until, inst, info); the kinds:

  death       60 yd / 30 min, persisted + SHARED: the other bots route round it too, at 40 yd (inst4 08:26 and inst2
              09:05 both died to the same mob pair at (-5041,-2900) walking to talk targets on different guide steps)
  flee        45 yd / 30 min: where a lost fight was broken off
  danger      50 yd / 20 min: a mob 2+ levels up (or elite) in view (run())
  hunt_skip   40 yd / 60 s: hunt points round a spot that just gave nothing (REPORT.md change 2)
  roam_fail   40 yd / 10 min: hunt points round a walk that ended stuck / timed out / without a path
  guard_fight 30 yd / 30 min, persisted (not shared): an elite chest guard fought us (info npc=...)

ROUTE_KINDS are route costs (Mover.zones, travel_to) and the roam / hunt filter; HUNT_KINDS only filter hunt points.
Persisted kinds live in ONE file, nb/hazards.json, for all instances: written atomically (tmp + os.replace, retried:
on Windows the replace fails while another bot has the file open), read through an mtime cache. A bot reads its own
persisted entries back after a restart (the old self.avoid was lost at every restart) and the others' SHARED ones.
nb/death_spots.json (the old code's file) is still read, so a bot not yet restarted onto this code is seen too.

PERSIST is off by default: tests (and any Bot built with __new__) never touch the live file; nb/bot.py's __main__
turns it on, as rxp_follow.PERSIST.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "hazards.json")
LEGACY_DEATHS = os.path.join(HERE, "death_spots.json")
PERSIST = False

Hazard = namedtuple("Hazard", "kind x y r until inst info")

# kind -> (radius yd, ttl s, persisted, radius the OTHER bots use or None = not shared)
KINDS = {
    "death": (60.0, 1800.0, True, 40.0),
    "flee": (45.0, 1800.0, False, None),
    "danger": (50.0, 1200.0, False, None),
    "hunt_skip": (40.0, 60.0, False, None),
    "roam_fail": (40.0, 600.0, False, None),
    "guard_fight": (30.0, 1800.0, True, None),
    # the long memory of a death (written with every "death"): only for death_area clustering, no cost of its own
    "death_mark": (0.0, 4 * 3600.0, True, 0.0),
}
ROUTE_KINDS = ("death", "flee", "danger")
HUNT_KINDS = ("hunt_skip", "roam_fail")
# Deadly areas (derived, never stored): deaths - ours and the other bots' - within AREA_GROUP_R yd of a group's densest
# one merge into one "death_area" round their centroid, AREA_R0 + AREA_STEP per death beyond the first (AREA_MAX at
# most, and always AREA_PAD past the farthest member), live until the last member runs out. A 40-60 yd death circle is
# far smaller than a camp: inst3 died 8 times round (-5386,-2778)..(-5535,-2729) 10:05-10:55, the last ones on an RXP
# "grind to 13" step whose hunt points kept leading back in. roam() keeps its hunt points out of these (ROAM_KINDS);
# they are no route cost (a 150+ yd cost bends every errand: inst4 00:25's 621 yd detour round a 150 yd circle).
# Deaths far apart in time count too: every death leaves a 4 h death_mark, and a cluster lives AREA_TTL_N per member
# from its newest one (bot 4 08:26 / 11:10 / 12:22, bot 2 09:05 round (-5040,-2900): 30-75 min apart, so the 30 min
# deaths never overlapped and no area ever formed). A cluster of AREA_ROUTE_N+ is also a route cost ("death_route"),
# capped at AREA_ROUTE_MAX yd so errands bend round the spot, not round a 220 yd circle.
AREA_TTL_N = 1800.0
# 2 deaths are enough for the route cost (capped): bot 4 walked a vendor errand at 18:42 through (-5799,-2869), where
# bot 3 (16:59) and bot 4 (18:19) had died - a 2-death area, roam-only then - and died there a third time
AREA_ROUTE_N, AREA_ROUTE_MAX = 2, 100.0
AREA_GROUP_R = 75.0     # yd round a group's densest death
AREA_RUNNER_PAD = 30.0  # yd more round a deadly area for hunt points (outside): runner range
AREA_MIN_N = 2
AREA_R0, AREA_STEP, AREA_MAX, AREA_PAD = 60.0, 30.0, 150.0, 40.0
ROUTE_KINDS = ROUTE_KINDS + ("death_route",)
ROAM_KINDS = ROUTE_KINDS + ("death_area",)
STAT_EVERY = 5.0        # s: the shared file's mtime is checked at most this often


class Hazards:
    def __init__(self, inst: int = 1, path: str | None = None, persist: bool | None = None, clock=time.time,
                 legacy: str | None = LEGACY_DEATHS):
        self.inst = inst
        self.path = path or PATH
        self.persist = PERSIST if persist is None else persist
        self.clock = clock
        self.legacy = legacy
        self._mine: list = []            # this bot's hazards (every kind; persisted ones are also in the file)
        self._file: list = []            # the file's entries (all instances), as Hazards
        self._mt = None                  # (mtime of hazards.json, mtime of death_spots.json) last read
        self._stat_t = -1e18
        self._loaded_own = False

    # ---------------------------------------------------------------- file
    def _read(self, force=False):
        if not self.persist:
            return
        now = self.clock()
        if not force and now - self._stat_t < STAT_EVERY:
            return
        self._stat_t = now
        mt = tuple(os.path.getmtime(p) if os.path.exists(p) else None for p in (self.path, self.legacy or ""))
        if mt == self._mt and not force:
            return
        out = []
        try:
            for e in json.load(open(self.path)) if mt[0] else ():
                out.append(Hazard(e["k"], float(e["x"]), float(e["y"]), float(e["r"]), float(e["u"]), int(e["i"]),
                                  e.get("info") or {}))
        except Exception:
            out = [h for h in self._file if h.info.get("_src") != "legacy"]      # keep the last good read
        try:                              # old-code bots: [[x, y, t, inst]], a death lasting SHARED 30 min
            r, ttl = KINDS["death"][0], KINDS["death"][1]
            for e in json.load(open(self.legacy)) if mt[1] else ():
                if int(e[3]) != self.inst:
                    out.append(Hazard("death", float(e[0]), float(e[1]), r, float(e[2]) + ttl, int(e[3]),
                                      {"_src": "legacy"}))
        except Exception:
            pass
        self._mt, self._file = mt, out
        if not self._loaded_own:          # after a restart: our own persisted hazards are ours again
            self._loaded_own = True
            have = {(h.kind, h.x, h.y, h.until) for h in self._mine}
            self._mine += [h for h in out if h.inst == self.inst and (h.kind, h.x, h.y, h.until) not in have
                           and h.info.get("_src") != "legacy"]

    def _write(self):
        if not self.persist:
            return
        now = self.clock()
        self._read(force=True)
        keep = [h for h in self._file if h.inst != self.inst and h.until > now and h.info.get("_src") != "legacy"]
        keep += [h for h in self._mine if KINDS.get(h.kind, (0, 0, False))[2] and h.until > now]
        rows = [{"k": h.kind, "x": round(h.x, 1), "y": round(h.y, 1), "r": h.r, "u": round(h.until, 1),
                 "i": h.inst, **({"info": h.info} if h.info else {})} for h in keep]
        tmp = f"{self.path}.{os.getpid()}.tmp"
        for k in range(4):
            try:
                with open(tmp, "w") as f:
                    json.dump(rows, f)
                os.replace(tmp, self.path)
                break
            except OSError:
                time.sleep(0.05 * (k + 1))
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        self._mt = None                   # re-read next time (our own write changed the mtime)
        self._stat_t = -1e18

    # ---------------------------------------------------------------- write side
    def add(self, kind, x, y, r=None, ttl=None, shared=None, now=None, **info) -> Hazard:
        """A new hazard of `kind` at (x, y); r / ttl default to KINDS. shared=None: the kind's own rule (True forces
        a persisted, shared entry with the kind's radius for the others; False keeps it in memory)."""
        r0, ttl0, persist, _ = KINDS.get(kind, (40.0, 600.0, False, None))
        now = self.clock() if now is None else now
        h = Hazard(kind, float(x), float(y), float(r0 if r is None else r), now + (ttl0 if ttl is None else ttl),
                   self.inst, dict(info, shared=True) if shared else dict(info))
        self._read()                      # (loads our own persisted entries first after a restart)
        self.prune(now)
        # the same hazard again (a guard_fight note every 5 s of the fight, a second death record at one spot): the
        # new one replaces it instead of piling up rows in the shared file
        self._mine = [o for o in self._mine if not (o.kind == kind and (
            (info and {k: v for k, v in o.info.items() if k != "shared"} == info)
            or (not info and math.hypot(o.x - h.x, o.y - h.y) < 1.0)))]
        self._mine.append(h)
        if kind == "death":
            m = Hazard("death_mark", h.x, h.y, 0.0, now + KINDS["death_mark"][1], self.inst, {})
            self._mine = [o for o in self._mine if not (o.kind == "death_mark" and math.hypot(o.x - m.x, o.y - m.y) < 1.0)]
            self._mine.append(m)
        if (persist if shared is None else shared):
            self._write()
        return h

    def prune(self, now=None):
        now = self.clock() if now is None else now
        self._mine = [h for h in self._mine if h.until > now]

    def clear(self, kinds=None):
        """Forget this bot's hazards of these kinds (memory only; tests and a fresh hunt)."""
        self._mine = [h for h in self._mine if kinds is not None and h.kind not in kinds]

    # ---------------------------------------------------------------- read side
    def active(self, kinds=None, bbox=None, now=None, mine_only=False) -> list:
        """Live hazards (ours, and the other bots' shared ones at their shared radius) of these kinds, inside
        bbox (xmin, ymin, xmax, ymax) if given."""
        now = self.clock() if now is None else now
        self._read()
        out = [h for h in self._mine if h.until > now]
        if not mine_only:
            for h in self._file:
                if h.inst == self.inst or h.until <= now:
                    continue
                sr = KINDS.get(h.kind, (0, 0, False, None))[3]
                if sr is None and not h.info.get("shared"):
                    continue
                out.append(h._replace(r=sr if sr is not None else h.r))
        if kinds is None or "death_area" in kinds or "death_route" in kinds:
            marks = [h for h in out if h.kind == "death_mark"]
            if not marks:                  # legacy deaths without marks still cluster
                marks = [h for h in out if h.kind == "death"]
            out = [h for h in out if h.kind != "death_mark"]
            out += [a for a in self.areas(marks, now=now) if a.until > now]
        if kinds is not None:
            out = [h for h in out if h.kind in kinds]
        if bbox is not None:
            out = [h for h in out if bbox[0] - h.r <= h.x <= bbox[2] + h.r and bbox[1] - h.r <= h.y <= bbox[3] + h.r]
        return out

    _areas_cache: dict = {}

    @classmethod
    def areas(cls, deaths, now=None) -> list:
        ds = list(deaths)
        key = tuple(sorted((h.kind, h.x, h.y, h.until) for h in ds))
        got = cls._areas_cache.get(key)
        if got is None:
            if len(cls._areas_cache) > 64:
                cls._areas_cache.clear()
            got = cls._areas_cache[key] = cls._areas(ds)
        return got

    @staticmethod
    def _areas(deaths) -> list:
        """death_area hazards (and death_route for AREA_ROUTE_N+) from death marks: groups (AREA_GROUP_R yd round the densest death)
        of AREA_MIN_N+ deaths. A cluster lives AREA_TTL_N s per member from its newest death (a mark's own ttl is the
        long memory; a plain "death" member counts its 30 min)."""
        ds = list(deaths)
        left, out = list(range(len(ds))), []
        while left:
            # greedy fixed-radius groups round the densest death, not single-link chains: 34 deaths over 4 h round
            # Loch Modan chain into one blob at 150 yd links, which would cover Thelsamar itself
            def nb(i):
                return [j for j in left if math.hypot(ds[i].x - ds[j].x, ds[i].y - ds[j].y) <= AREA_GROUP_R]
            seed = max(left, key=lambda i: len(nb(i)))
            grp = set(nb(seed))
            left = [j for j in left if j not in grp]
            if len(grp) < AREA_MIN_N:
                continue
            m = [ds[i] for i in grp]
            cx, cy = sum(h.x for h in m) / len(m), sum(h.y for h in m) / len(m)
            far = max(math.hypot(h.x - cx, h.y - cy) for h in m)
            r = max(min(AREA_MAX, AREA_R0 + AREA_STEP * (len(m) - 1)), min(AREA_MAX, far + AREA_PAD))
            born = max(h.until - KINDS[h.kind][1] for h in m)          # time of the newest death
            until = born + AREA_TTL_N * len(m)
            out.append(Hazard("death_area", cx, cy, r, until, 0, {"n": len(m)}))
            if len(m) >= AREA_ROUTE_N:
                out.append(Hazard("death_route", cx, cy, min(AREA_ROUTE_MAX, far + KINDS["death"][0]), until, 0,
                                  {"n": len(m)}))
        return out

    def covering(self, x, y, kinds=None, now=None, mine_only=False) -> list:
        return [h for h in self.active(kinds, now=now, mine_only=mine_only) if math.hypot(x - h.x, y - h.y) < h.r]

    def blocked(self, x, y, kinds=None, now=None, mine_only=False) -> bool:
        """(x, y) lies inside a live hazard of these kinds (strictly: a point on the rim is free, as unskipped())."""
        return any(math.hypot(x - h.x, y - h.y) <= h.r for h in self.active(kinds, now=now, mine_only=mine_only))

    def outside(self, pts, kinds, now=None, mine_only=False) -> list:
        """The points (x, y, ...) outside every live hazard of these kinds - a deadly area (death_area) by
        AREA_RUNNER_PAD more: a mob pulled at its edge runs into it at 15% and comes back with friends (bot 4 L527,
        deaths 17:22 / 17:34 beside (-5430,-2726), three "leaving the runner: deadly area" lines)."""
        hs = self.active(kinds, now=now, mine_only=mine_only)
        return [p for p in pts if all(math.hypot(p[0] - h.x, p[1] - h.y) > h.r + (AREA_RUNNER_PAD if h.kind == "death_area"
                                                                                else 0.0) for h in hs)]

    def first_end(self, kinds, now=None):
        """When the earliest live hazard of these kinds runs out (None: none live)."""
        hs = self.active(kinds, now=now)
        return min(h.until for h in hs) if hs else None

    def cost_zones(self, bbox=None, z=0.0, kinds=ROUTE_KINDS, extra=(), now=None) -> list:
        """[(x, y, z, r)] for Mover.zones: the live route hazards plus `extra` static zones (x, y, z, r) - the
        spawn-table aggro zones of Bot.spawn_zones() - filtered to bbox too."""
        out = [(h.x, h.y, z, h.r) for h in self.active(kinds, bbox=bbox, now=now)]
        for e in extra:
            if bbox is None or (bbox[0] - e[3] <= e[0] <= bbox[2] + e[3] and bbox[1] - e[3] <= e[1] <= bbox[3] + e[3]):
                out.append(tuple(e))
        return out


class HazardsAttr:
    """Bot attribute holding the Hazards store, made on first use (tests build Bot with __new__)."""

    def __set_name__(self, owner, name):
        self.slot = "_hz_" + name

    def __get__(self, obj, cls=None):
        if obj is None:
            return self
        h = obj.__dict__.get(self.slot)
        if h is None:
            try:
                from nb import inst
                i = inst.INST
            except Exception:
                i = 1
            h = obj.__dict__[self.slot] = Hazards(i)
        return h

    def __set__(self, obj, value):
        obj.__dict__[self.slot] = value
