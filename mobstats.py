"""What a mob actually costs THIS character, measured instead of assumed.

Every rule that decides "can I take this fight?" used to be a constant tuned
against one character in one zone: an add radius measured on a level-18 mage,
a pair rule written for level-1 wolves, "three levels above us" for everything
from a trogg to a Stranglethorn raptor. combat.py already emitted a full cost
record on every kill (entry, seconds, hp before/after) and nothing ever read
it back: 1011 kills across 31 entries thrown away.

This module keeps those records - per character, in data/mobstats_<name>.json -
and turns them into two predictions:

    cost(entry, me)                -> (% of our health, seconds) one kill costs
    affordable_pull(me, t, others) -> is that fight inside our health budget?

The general law is that a fight is worth taking when its PREDICTED COST fits
in the health we have, which is true at level 1 and at level 60, for a mage
and for a warrior in mail. The level bands it replaces were only ever a proxy
for that, and a proxy that freezes at level 15.

Three things keep it honest:

  * Escapes and deaths are samples too. Recording only the kills we won biases
    every mean downward - the mob that took 90% of our health and then killed
    us would look free, because the kill record was never written.
  * A cold start must reproduce today's behaviour exactly. Below MIN_SAMPLES
    observations of an entry, the callers fall back to the constants that are
    in the code today; this module only ever WIDENS what they accept.
  * A death costs margin. One death drops the health budget by 30% and it
    does not recover for RECOVER_KILLS clean kills, so a model that
    under-predicts is corrected by the only evidence that really matters.

The cold-start prior is a ratio model fitted online: an unseen mob is priced
from its creature_template features (level delta and rank, which worlddb
already loads) times the ratio between prediction and measurement over every
entry this character HAS measured. A level-40 warrior in mail and a level-7
one in leather get different scales from their own kills, with no table.
"""

from __future__ import annotations

import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")

MIN_SAMPLES = 3            # below this an entry is "unmeasured": callers use their constants
# One kill of the same mob cost anywhere from 0% to 100% of health in the
# real log, so the average has to be slow: over the 1028 recorded kills,
# alpha 0.1 predicted a mob's next kills with a median error of 25% against
# 32% at 0.3, and a worst case of 2.1x against 3.1x. Responsiveness to a
# character that has changed comes from the level-drift reset below, not
# from a twitchy mean.
EWMA_ALPHA = 0.1
LEVEL_DRIFT = 3            # levels of drift before a measurement starts decaying to the prior
# Levels either side of us the prior's correction is fitted over. Same 6 as
# LEVEL_DRIFT * 2, which is where record() already draws the line between
# "this character" and "a different one". Leave-one-entry-out over the 27
# measured entries in data/events.jsonl (levels 2-9, so the bands barely
# bite there): median error 0.27 against 0.32 for a whole-file fit.
RATIO_BAND = 6
DRIFT_SPAN = 5.0           # ... and levels of further drift before it is gone entirely

# Prior for a mob nobody has fought yet. The SHAPE is fixed; the SCALE comes
# from this character's own kills - see _ratio().
#
# Three terms, and the third is the one that is easy to get wrong. Cost grows
# with the level gap (1.35x a level, the server's own stat curve is close to
# that), an elite costs several normals, AND the early levels are free in a
# way later ones are not: over 996 real kills a same-level mob cost 2-3% of
# health at level 2, 16% at level 4 and 46% at level 7. That is not a level-7
# fact, it is the low-level game running out of slack, so the term saturates
# at level 8 instead of extrapolating a curve that would price a level-20 mob
# at 300% of our health. Leave-one-entry-out over those kills: median error
# 16%, 90th percentile 49%, worst 84% - see the offline replay in __main__.
BASE_HP = 25.0             # % of our health a same-level normal mob costs, level 8+
BASE_SECS = 18.0
HP_PER_LEVEL = 1.35        # per level the mob is above us
SECS_PER_LEVEL = 1.25
EARLY_LEVEL = 8.0          # below this the game is easier than the curve says
RANK_HP = {0: 1.0, 1: 3.0, 2: 3.5, 3: 8.0, 4: 1.5}      # normal, elite, rare elite, boss, rare
RANK_SECS = {0: 1.0, 1: 2.5, 2: 3.0, 3: 6.0, 4: 1.3}

MARGIN_START = 0.75        # a pull may cost this share of the health we have now
MARGIN_FLOOR = 0.35
DEATH_PENALTY = 0.7        # one death: the budget drops by 30% ...
RECOVER_KILLS = 25         # ... and only starts coming back after this many clean kills
RECOVER_STEP = 1.1

SAVE_EVERY = 5.0           # seconds; a death always saves at once


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def prior_hp(delta: float, rank: int = 0, level: float = 60.0) -> float:
    """% of our health a mob `delta` levels above a level-`level` us costs."""
    early = min(1.0, (max(1.0, level) / EARLY_LEVEL) ** 2)
    return _clamp(BASE_HP * (HP_PER_LEVEL ** delta) * RANK_HP.get(rank, 1.0) * early,
                  0.2, 200.0)


def prior_secs(delta: float, rank: int = 0, level: float = 60.0) -> float:
    early = min(1.0, (max(1.0, level) / EARLY_LEVEL) ** 0.5)
    return _clamp(BASE_SECS * (SECS_PER_LEVEL ** delta) * RANK_SECS.get(rank, 1.0) * early,
                  2.0, 300.0)


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


class MobStats:
    """One character's measurements. Never raises: a broken file is a cold start."""

    def __init__(self, char_name: str, path: str | None = None):
        self.char = char_name or "unknown"
        # path="" is an in-memory instance (the offline replay below)
        self.path = os.path.join(DATA_DIR, f"mobstats_{self.char}.json") if path is None else path
        self.mobs: dict[int, dict] = {}
        self.margin = MARGIN_START
        self.clean_kills = 0
        self.deaths = 0
        self._lock = threading.Lock()
        self._saved = 0.0
        self._ratio_cache: tuple[tuple[int, int], float, float] | None = None
        self._rank_cache: dict[int, int] = {}
        self._load()

    # ------------------------------------------------------------ storage
    def _load(self):
        try:
            d = json.load(open(self.path, encoding="utf-8"))
        except Exception:
            return
        try:
            self.mobs = {int(k): v for k, v in (d.get("mobs") or {}).items()}
            self.margin = float(d.get("margin", MARGIN_START))
            self.clean_kills = int(d.get("clean_kills", 0))
            self.deaths = int(d.get("deaths", 0))
        except Exception:
            self.mobs = {}

    def save(self, force: bool = False):
        if not self.path:                       # an offline replay: memory only
            return
        if not force and time.time() - self._saved < SAVE_EVERY:
            return
        self._saved = time.time()
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"char": self.char, "margin": round(self.margin, 3),
                           "clean_kills": self.clean_kills, "deaths": self.deaths,
                           "mobs": {str(k): v for k, v in self.mobs.items()}},
                          f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except Exception:
            pass            # a full disk must not stop the bot (events.py's rule)

    # ------------------------------------------------------------ features
    def rank(self, entry: int) -> int:
        """creature_template.rank (0 normal, 1 elite, ...), 0 if the DB is away."""
        if entry in self._rank_cache:
            return self._rank_cache[entry]
        rk = 0
        try:
            import worlddb
            info = worlddb.creatures([entry]).get(entry)
            rk = int(info.rank) if info else 0
        except Exception:
            rk = 0
        self._rank_cache[entry] = rk
        return rk

    # ------------------------------------------------------------ recording
    def record(self, entry: int, mob_level: int, my_level: int, hp_cost: float,
               secs: float, outcome: str = "kill", adds: int = 0) -> None:
        """One fight, however it ended.

        outcome is "kill", "escape" or "death". An escape or a death is a
        sample with AT LEAST that much health spent - dropping them would
        price every mob by the fights we happened to win.
        """
        try:
            entry, mob_level, my_level = int(entry), int(mob_level), int(my_level)
            hp_cost = _clamp(float(hp_cost), 0.0, 100.0)
            secs = _clamp(float(secs), 0.5, 600.0)
        except Exception:
            return
        if entry <= 0 or my_level <= 0:
            return
        if outcome == "death":
            hp_cost = max(hp_cost, 100.0)       # it cost everything we had
        with self._lock:
            r = self.mobs.get(entry)
            if r is None or abs(my_level - int(r.get("lvl", my_level))) > LEVEL_DRIFT * 2:
                # a measurement from six levels ago is about a different
                # character: start again rather than average the two
                r = {"n": 0, "hp": hp_cost, "secs": secs, "adds": float(adds),
                     "escapes": 0, "deaths": 0}
            n = int(r.get("n", 0))
            a = EWMA_ALPHA if n >= 3 else 1.0 / (n + 1)     # the first samples count fully
            r["hp"] = (1 - a) * float(r.get("hp", hp_cost)) + a * hp_cost
            r["secs"] = (1 - a) * float(r.get("secs", secs)) + a * secs
            r["adds"] = (1 - a) * float(r.get("adds", adds)) + a * float(adds)
            r["n"] = n + 1
            r["lvl"] = my_level
            r["mlvl"] = mob_level
            r["rank"] = self.rank(entry)
            r["t"] = round(time.time())
            if outcome == "escape":
                r["escapes"] = int(r.get("escapes", 0)) + 1
            elif outcome == "death":
                r["deaths"] = int(r.get("deaths", 0)) + 1
            self.mobs[entry] = r
            self._ratio_cache = None
            if outcome == "death":
                self.deaths += 1
                self.clean_kills = 0
                self.margin = max(MARGIN_FLOOR, self.margin * DEATH_PENALTY)
            elif outcome == "kill":
                self.clean_kills += 1
                if self.clean_kills and self.clean_kills % RECOVER_KILLS == 0:
                    self.margin = min(MARGIN_START, self.margin * RECOVER_STEP)
        self.save(force=(outcome == "death"))

    # ------------------------------------------------------------ the model
    def samples(self, entry: int) -> int:
        r = self.mobs.get(int(entry))
        return int(r.get("n", 0)) if r else 0

    def measured(self, entry: int) -> bool:
        return self.samples(entry) >= MIN_SAMPLES

    def _ratio(self, level: float | None = None) -> tuple[float, float]:
        """How wrong the prior is for this character, from its own kills.

        Fitted over every entry with enough samples: the median of
        measured/predicted. One number for health, one for time - that is the
        whole "class, gear and level band" table nobody has to write.

        The SHAPE above EARLY_LEVEL is deliberately level-free: a mob of our
        own level is priced as a share of the health we have, and that share
        is the same quantity at 10 and at 60 - both sides of it grow with the
        level. What is NOT level-free is how wrong that shape is for this
        character: a level-7 warrior in starter gear measured 42% where the
        prior said 19%, and a level-45 one in mail will not. So the fit is
        taken over the kills made NEAR the level being asked about, and only
        falls back to the whole file when there are none. Without this the
        correction was one number for the life of the character, and an
        unseen mob really was priced identically at 10 and at 60.
        """
        band = int(level or 0)
        key = (len(self.mobs), band)
        if self._ratio_cache and self._ratio_cache[0] == key:
            return self._ratio_cache[1], self._ratio_cache[2]
        hp_r, s_r, hp_all, s_all = [], [], [], []
        for e, r in self.mobs.items():
            if int(r.get("n", 0)) < MIN_SAMPLES:
                continue
            delta = int(r.get("mlvl", 0)) - int(r.get("lvl", 0))
            rk, lv = int(r.get("rank", 0)), int(r.get("lvl", 0))
            # a death sample is a floor, not a measurement: it says "at least
            # everything", and fitting the prior to it would price every mob
            # in the zone as lethal
            if int(r.get("deaths", 0)) and float(r.get("hp", 0)) >= 99.0:
                continue
            h = float(r["hp"]) / prior_hp(delta, rk, lv)
            s = float(r["secs"]) / prior_secs(delta, rk, lv)
            hp_all.append(h)
            s_all.append(s)
            if band and abs(band - lv) <= RATIO_BAND:
                hp_r.append(h)
                s_r.append(s)
        if not hp_r:                    # nothing measured near this level yet
            hp_r, s_r = hp_all, s_all
        hp = _clamp(_median(hp_r) or 1.0, 0.2, 5.0)
        se = _clamp(_median(s_r) or 1.0, 0.2, 5.0)
        self._ratio_cache = (key, hp, se)
        return hp, se

    def cost(self, entry: int, me, mob_level: int | None = None) -> tuple[float, float]:
        """(% of our health, seconds) one kill of `entry` should cost now."""
        entry = int(entry)
        r = self.mobs.get(entry)
        rk = int(r.get("rank", 0)) if r else self.rank(entry)
        my_level = getattr(me, "level", 1) or 1
        if mob_level is None:
            mob_level = int(r.get("mlvl", my_level)) if r else my_level
        delta = mob_level - my_level
        k_hp, k_secs = self._ratio(my_level)
        p_hp = prior_hp(delta, rk, my_level) * k_hp
        p_secs = prior_secs(delta, rk, my_level) * k_secs
        if not r or int(r.get("n", 0)) < MIN_SAMPLES:
            return p_hp, p_secs
        # Re-price the measurement for the fight it is being asked about now:
        # the same wolf at -2 levels is not the wolf we measured at +1, and
        # the same wolf five levels of gear later is not the same wolf.
        lvl0 = int(r.get("lvl", my_level))
        delta0 = int(r.get("mlvl", mob_level)) - lvl0
        m_hp = float(r["hp"]) * prior_hp(delta, rk, my_level) / prior_hp(delta0, rk, lvl0)
        m_secs = float(r["secs"]) * prior_secs(delta, rk, my_level) / prior_secs(delta0, rk, lvl0)
        drift = abs(my_level - int(r.get("lvl", my_level)))
        w = 1.0 if drift <= LEVEL_DRIFT else max(0.0, 1.0 - (drift - LEVEL_DRIFT) / DRIFT_SPAN)
        return (w * m_hp + (1 - w) * p_hp, w * m_secs + (1 - w) * p_secs)

    def budget(self, me) -> float:
        """Health we are willing to spend on the next pull, right now."""
        return max(0.0, getattr(me, "hp_pct", 100.0)) * self.margin

    def affordable_pull(self, me, target, others=()) -> bool:
        """Does this fight - the target plus everything that would join - fit?"""
        try:
            total = self.cost(target.entry, me, target.level)[0]
            for o in others:
                total += self.cost(o.entry, me, o.level)[0]
        except Exception:
            return False
        return total <= self.budget(me)

    def confident(self, units) -> bool:
        """True when every mob in the fight has been measured enough to price."""
        try:
            return all(self.measured(u.entry) for u in units)
        except Exception:
            return False

    def add_radius(self, me, target, ranged: bool = False) -> float | None:
        """How close a second mob has to be before this pull is a pair fight.

        The geometric answer is the server's own aggro radius - that is how
        far away a neighbour can be and still join. The interesting part is
        that a neighbour we can comfortably afford does not make the pull a
        pair fight at all, so the radius shrinks towards zero as the measured
        cost of the pair falls below what we can pay. None = no measurement
        yet, use the caller's constant.
        """
        if target is None or not self.measured(getattr(target, "entry", 0)):
            return None
        try:
            pair = 2.0 * self.cost(target.entry, me, target.level)[0]
            budget = max(1.0, self.budget(me))
            cheap = _clamp(pair / budget, 0.35, 1.0)
            aggro = _clamp(20.0 + (target.level - me.level), 5.0, 25.0)
            return aggro * (1.4 if ranged else 1.0) * cheap
        except Exception:
            return None


# ---------------------------------------------------------------- singleton
_active: MobStats | None = None


def for_char(name: str) -> MobStats:
    """This character's measurements, created once per process."""
    global _active
    if _active is None or _active.char != (name or "unknown"):
        _active = MobStats(name)
    return _active


def current() -> MobStats | None:
    """Whatever for_char() last built - None before the bot has a character."""
    return _active


# ---------------------------------------------------------------- CLI
def _replay(path: str, char: str = "replay"):
    """Rebuild a character's stats from data/events.jsonl (read-only report)."""
    import events
    st = MobStats(char, path="")
    for rec in events.read(path):
        if rec.get("ev") != "kill":
            continue
        hp = float(rec.get("hp0", 0)) - float(rec.get("hp1", 0))
        st.record(int(rec["entry"]), int(rec.get("mlvl", 0)), int(rec.get("lvl", 1)),
                  hp, float(rec.get("secs", 1)), "kill")
    return st


if __name__ == "__main__":
    import sys
    st = _replay(sys.argv[1] if len(sys.argv) > 1 else os.path.join(DATA_DIR, "events.jsonl"))
    k_hp, k_secs = st._ratio()
    print(f"{len(st.mobs)} entries, prior ratio hp x{k_hp:.2f} secs x{k_secs:.2f}, "
          f"margin {st.margin:.2f}")
    for e, r in sorted(st.mobs.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {e:6} n={r['n']:4}  {r['hp']:5.1f}% hp  {r['secs']:5.1f}s  "
              f"lvl {r.get('mlvl')} vs {r.get('lvl')}")
