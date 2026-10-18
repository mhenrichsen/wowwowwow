"""Tactical look-ahead, step 1: forecast and log, decide nothing.

Two questions, answered from the bot's own measured fights:
  fight(...)  - a fight with n mobs from (hp, mana): how long, and the hp / mana left at the end. Verdict "ok" (the
                p90 fight still ends above HP_FLOOR with mana >= 0), "tight" (the median one does, the p90 one not),
                "lose" (the median one does not).
  walk(...)   - the spawns whose aggro a planned walk crosses (threat.Model.path_risk), grouped into encounters
                (spawns within threat.ASSIST_R of each other come together), each with the hp / mana we reach it with
                (regen while walking) and its fight() forecast.

Book keeps one record per finished fight: (npc, level gap, attackers, seconds, hp spent, mana spent, outcome). A
mob's cost is the median / p90 of its own lone kills (MIN_NPC+), else of lone kills at the same level gap (MIN_GAP+),
else the defaults. n mobs: they die one after another, so the k-th hits for k fights' time: hp x n(n+1)/2 (the same
as threat.pack_need's 1 + PACK_W (n - 1) per mob at PACK_W 0.5), mana and time x n. A heal turns HEAL_GAIN hp out of
one heal's mana when the hp would end under HEAL_FLOOR.

2026-10-15 19:50-22:40, 4 bots: 17 deaths cost ~80 min, standing rests ~80 min; 13 of the deaths began with the bot
attacked (pulled=False), mostly walking or resting in a camp after a kill at 17-30% mana. The walk forecast is the
look-ahead for those; the fight forecast checks itself against the outcome (Bot.fight logs both).
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import namedtuple

from nb import threat

# defaults until the bot's own fights are measured (a level 15 caster, 2026-10-15 19:50-22:40: lone kills median 48%
# mana, ~20 s, ~30% hp)
DEF_S, DEF_HP, DEF_MANA = 20.0, 30.0, 48.0
# the fight needs mana (a class whose damage is a cast filler: nb/classes.py Profile.nuke). False: the weapon / ranged
# auto attack carry it - running dry neither ends it nor slows it. Set by the bot from its class profile
MANA_FIGHTS = True
DEF_P90 = 1.4                 # p90 / median when nothing is measured
MIN_NPC, MIN_GAP = 3, 5       # lone kills of this npc / at this level gap before they set its cost
GAP_W = 0.12                  # cost change per level of gap (threat.LEVEL_W)
BOOK_N = 400                  # records kept
HP_FLOOR = 20.0               # % hp a fight must end above to count as "ok"
HEAL_FLOOR = 35.0             # % hp: under this a heal is assumed (rotation.PROJ_FLOOR)
HEAL_GAIN = 25.0              # % hp one heal nets under fire (shaman_rotation.HEAL_GAIN)
HEAL_S = 2.0
NO_HEAL = float("inf")        # heal_pct None: the class has no heal on the bar - no heals are forecast
REGEN_HP_S = 0.5              # % hp / s regained walking out of combat
REGEN_MANA_S = 0.6            # % mana / s regained walking (the walked rests: ~20 points in 25-40 s)
RUN_SPEED = 7.0
WALK_HORIZON = 150.0          # yd of the walk looked at

Cost = namedtuple("Cost", "s hp mana s90 hp90 mana90 n src")
Forecast = namedtuple("Forecast", "n s end_hp end_mana end_hp90 end_mana90 heals verdict src")
Encounter = namedtuple("Encounter", "d n npc level arrive_hp arrive_mana fc")


def _med(v):
    v = sorted(v)
    return v[len(v) // 2] if v else 0.0


def _p90(v):
    v = sorted(v)
    return v[min(len(v) - 1, (9 * len(v)) // 10)] if v else 0.0


class Book:
    """Finished fights: dicts {t, npc, gap, n, s, hp, mana, out}. Persisted to path (json) when given."""

    def __init__(self, path=None, rows=None):
        self.path = path
        self._memo = {}
        self.rows = list(rows or ())
        if rows is None and path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.rows = json.load(f)[-BOOK_N:]
            except Exception:
                self.rows = []

    def add(self, npc, gap, n, s, hp, mana, out, now=None):
        self.rows.append({"t": round(now or time.time()), "npc": int(npc or 0), "gap": int(gap), "n": int(n),
                          "s": round(s, 1), "hp": round(hp, 1), "mana": round(mana, 1), "out": out})
        del self.rows[:-BOOK_N]
        self._memo = {}
        if self.path:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.rows, f)
                os.replace(tmp, self.path)
            except Exception:
                pass

    def cost(self, npc, gap) -> Cost:
        """One mob's cost: its own lone kills, else lone kills at this level gap, else the defaults (memoized until
        the next add)."""
        k = (npc, gap, len(self.rows))
        c = self._memo.get(k)
        if c is None:
            c = self._memo[k] = self._cost(npc, gap)
        return c

    def _cost(self, npc, gap) -> Cost:
        lone = [r for r in self.rows if r["n"] <= 1 and r["out"] == "killed"]
        for src, rs, need in (("npc", [r for r in lone if npc and r["npc"] == npc], MIN_NPC),
                              ("gap", [r for r in lone if r["gap"] == gap], MIN_GAP)):
            if len(rs) >= need:
                s, hp, mn = ([max(0.0, r[k]) for r in rs] for k in ("s", "hp", "mana"))
                return Cost(_med(s), _med(hp), _med(mn), _p90(s), _p90(hp), _p90(mn), len(rs), src)
        # a level gap moves a cost ~GAP_W a level (threat.LEVEL_W): all lone kills, scaled from their own gaps
        if len(lone) >= MIN_GAP:
            g0 = _med([r["gap"] for r in lone])
            f = max(0.4, 1.0 + GAP_W * (gap - g0))
            s, hp, mn = ([max(0.0, r[k]) for r in lone] for k in ("s", "hp", "mana"))
            return Cost(_med(s) * f, _med(hp) * f, _med(mn) * f, _p90(s) * f, _p90(hp) * f, _p90(mn) * f,
                        len(lone), "all")
        f = max(0.4, 1.0 + GAP_W * gap)
        return Cost(DEF_S * f, DEF_HP * f, DEF_MANA * f, DEF_S * f * DEF_P90, DEF_HP * f * DEF_P90,
                    DEF_MANA * f * DEF_P90, 0, "default")


def _end(hp, mana, n, s1, hp1, mana1, heal_pct):
    """(seconds, end hp, end mana, heals) of n mobs at one mob's (s1, hp1, mana1)."""
    n = max(1, n)
    s, hp_cost, mana_cost = s1 * n, hp1 * n * (n + 1) / 2.0, mana1 * n
    e_hp, e_mana, heals = hp - hp_cost, mana - mana_cost, 0
    while e_hp < HEAL_FLOOR and e_mana >= heal_pct and heals < 6:
        heals += 1
        e_hp += HEAL_GAIN
        e_mana -= heal_pct
        s += HEAL_S
    return s, min(100.0, e_hp), e_mana, heals


def fight(book: Book, pl, t_level, npc, hp, mana, n=1, heal_pct=None) -> Forecast:
    c = book.cost(npc, (t_level or pl) - pl)
    hpct = heal_pct if heal_pct else NO_HEAL
    s, e_hp, e_mana, heals = _end(hp, mana, n, c.s, c.hp, c.mana, hpct)
    _s9, e_hp9, e_mana9, _h9 = _end(hp, mana, n, c.s90, c.hp90, c.mana90, hpct)
    ok = lambda h, m: h >= HP_FLOOR and (m >= 0.0 or not MANA_FIGHTS)
    verdict = "ok" if ok(e_hp9, e_mana9) else "tight" if ok(e_hp, e_mana) else "lose"
    return Forecast(max(1, n), round(s), round(e_hp), round(e_mana), round(e_hp9), round(e_mana9), heals, verdict,
                    f"{c.src}{c.n}" if c.n else c.src)


def fmt(fc: Forecast) -> str:
    return (f"n={fc.n} ~{fc.s}s -> hp {fc.end_hp}% mana {fc.end_mana}% (p90 hp {fc.end_hp90}% mana {fc.end_mana90}%"
            f"{f', {fc.heals} heal(s)' if fc.heals else ''}) [{fc.verdict}, {fc.src}]")


def _along(pts):
    """Cumulative distance at each point."""
    acc, out = 0.0, [0.0]
    for a, b in zip(pts, pts[1:]):
        acc += math.hypot(b[0] - a[0], b[1] - a[1])
        out.append(acc)
    return out


def _cut(pts, horizon):
    """pts up to `horizon` yd along."""
    out, acc = [pts[0]], 0.0
    for a, b in zip(pts, pts[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        if acc + d >= horizon:
            f = (horizon - acc) / d if d > 0 else 0.0
            out.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
            return out
        acc += d
        out.append(b)
    return out


def _first_reach(p, pts, reach):
    """Distance along pts where the walk first comes within reach of spawn p (None: never)."""
    acc = 0.0
    for a, b in zip(pts, pts[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        if threat._seg_d(p.x, p.y, a[0], a[1], b[0], b[1]) <= reach:
            # the closest point on this segment, back off to where the reach starts (good enough: segment start)
            dx, dy = b[0] - a[0], b[1] - a[1]
            n2 = dx * dx + dy * dy
            t = 0.0 if n2 <= 0 else max(0.0, min(1.0, ((p.x - a[0]) * dx + (p.y - a[1]) * dy) / n2))
            cx, cy = a[0] + t * dx, a[1] + t * dy
            perp = math.hypot(p.x - cx, p.y - cy)
            back = math.sqrt(max(0.0, reach * reach - perp * perp))
            return max(0.0, acc + t * d - back)
        acc += d
    return None


def walk(model, book: Book, pl, pts, hp, mana, heal_pct=None, horizon=WALK_HORIZON, pad=threat.ROUTE_PAD) -> list:
    """Encounters on the first `horizon` yd of the walk pts [(x, y, ...)], nearest first."""
    if not pts or len(pts) < 2:
        return []
    cut = _cut([(p[0], p[1]) for p in pts], horizon)
    on = model.path_risk(pl, cut, pad=pad)
    if not on:
        return []
    hits = []
    for p in on:
        reach = threat.aggro_radius(pl, p.hi) + p.wander + (threat.ELITE_PAD if p.rank > 0 else 0.0) + pad
        d = _first_reach(p, cut, reach)
        if d is not None:
            hits.append((d, p))
    hits.sort(key=lambda t: t[0])
    out, used = [], set()
    for d, p in hits:
        if id(p) in used:
            continue
        used.add(id(p))
        # what comes with it: on-path spawns of its faction in assist range, and the live ones beside it off the path
        grp = [p]
        for _d2, q in hits:
            if id(q) not in used and q.fac == p.fac and math.hypot(q.x - p.x, q.y - p.y) <= threat.ASSIST_R:
                used.add(id(q))
                grp.append(q)
        try:
            for _d3, q in model.sp.near(p.x, p.y, threat.ASSIST_R):
                if q is not p and q not in grp and q.fac == p.fac and not threat.is_gray(pl, q.hi):
                    grp.append(q)
        except Exception:
            pass
        n = sum(1 for q in grp if not threat.is_gray(pl, q.hi)) or 1
        t = d / RUN_SPEED
        a_hp, a_mana = min(100.0, hp + REGEN_HP_S * t), min(100.0, mana + REGEN_MANA_S * t)
        npc = min(p.entries) if p.entries else 0
        fc = fight(book, pl, p.hi, npc, a_hp, a_mana, n, heal_pct)
        out.append(Encounter(round(d), n, npc, p.hi, round(a_hp), round(a_mana), fc))
        if len(out) >= 3:
            break
    return out


def fmt_walk(enc) -> str:
    return "; ".join(f"at {e.d} yd npc {e.npc} lvl {e.level}" + (f" (+{e.n - 1})" if e.n > 1 else "")
                     + f" arriving hp {e.arrive_hp}% mana {e.arrive_mana}%: {fmt(e.fc)}" for e in enc)


# ------------------------------------------------------------ mid-fight: stay or flee (step 1: logged, decides nothing)
# 2026-10-15 23:00 - 10-16 06:25, 135 flees: 66 died (doomed 11/47, outmatched 20/48, hopeless 23/26, losing the race
# 9/11); the ones that got away began at a median 77% hp, the dead ones at 62%. The rules flee on hp already lost.
# mid() forecasts the rest of THIS fight from the live rates and every attacker's plate hp; FleeBook gives the measured
# chance a flee from this hp with this many on us dies; decide() compares the two in seconds.
P90_INC, P90_OUT = 1.3, 0.77      # the bad case: damage taken x this, damage dealt x this
MELEE_OUT_F = 0.5                 # out of mana: the weapon alone kills at this share of the rate
MID_STEP = 0.5                    # s per simulation step
MID_CAP_S = 240.0
P_DIE = {"ok": 0.03, "tight": 0.15, "lose": 0.6}    # stay: death chance by verdict (priors; the checks calibrate them)
DEATH_S = 120.0                   # s a death costs (release, run back, rest; sickness not counted)
FLEE_S = 25.0                     # s a flee costs that gets away (the run + the walk back)
REPULL_S = 20.0                   # s: the fight given up is fought again later
FLEE_PRIOR = (0.5, 4.0)           # a flee bucket starts from this death chance with this weight

Mid = namedtuple("Mid", "n s end_hp end_hp90 verdict p_die")


def _sim(hp, mana, mobs, inc_each, heal_pct, mana_s):
    """mobs: [(hp %, seconds a full one takes)] in kill order. -> (seconds, end hp). Heals under HEAL_FLOOR."""
    t, alive = 0.0, list(mobs)
    while alive and hp > 0 and t < MID_CAP_S:
        if hp < HEAL_FLOOR and mana >= heal_pct:
            hp += HEAL_GAIN - len(alive) * inc_each * HEAL_S
            mana -= heal_pct
            t += HEAL_S
            continue
        mhp, full = alive[0]
        rate = 100.0 / max(1.0, full) * (1.0 if (mana > 0 or not MANA_FIGHTS) else MELEE_OUT_F)
        dt = min(MID_STEP, mhp / rate)
        hp -= len(alive) * inc_each * dt
        mana -= mana_s * dt
        alive[0] = (mhp - rate * dt, full)
        if alive[0][0] <= 0.01:
            alive.pop(0)
        t += dt
    return t, min(100.0, hp)


def mid(book: Book, pl, hp, mana, att, target_hp=None, inc=0.0, out=0.0, heal_pct=None) -> Mid:
    """The rest of this fight. att: [(level, plate hp %, npc, is_target)] of what is on us. inc / out: the live rates
    (% of our hp / of the target's hp per s; 0 = not measured yet: the book's)."""
    hpct = heal_pct if heal_pct else NO_HEAL
    if not att:
        return Mid(0, 0, round(hp), round(hp), "ok", P_DIE["ok"])
    mobs, inc_book, mana_s = [], 0.0, 0.0
    for lv, php, npc, tgt in att:
        c = book.cost(npc, (lv or pl) - pl)
        full = (100.0 / out) if (tgt and out > 0.5) else c.s
        h = target_hp if (tgt and target_hp is not None) else php
        mobs.append((tgt, max(1.0, float(h)), max(3.0, full), c))
    mobs.sort(key=lambda m: (not m[0], m[1]))            # the target first, then the weakest
    n = len(mobs)
    inc_book = sum(m[3].hp / max(1.0, m[3].s) for m in mobs) / n
    mana_s = sum(m[3].mana / max(1.0, m[3].s) for m in mobs) / n
    inc_each = (inc / n) if inc > 0.5 else inc_book
    order = [(m[1], m[2]) for m in mobs]
    s, e = _sim(hp, mana, order, inc_each, hpct, mana_s)
    order90 = [(h, f / P90_OUT) for h, f in order]
    _s9, e9 = _sim(hp, mana, order90, inc_each * P90_INC, hpct, mana_s)
    v = "ok" if e9 >= HP_FLOOR else "tight" if e >= HP_FLOOR else "lose"
    return Mid(n, round(s), round(e), round(e9), v, P_DIE[v])


class FleeBook:
    """Finished flees: {t, hp, n, out} (out: clear / chased / dead). p_dead(hp, n): the measured share that died
    from this hp band (20 wide) with this many on us (1, 2, 3+), pulled to FLEE_PRIOR when there are few."""

    def __init__(self, path=None, rows=None):
        self.path = path
        self.rows = list(rows or ())
        if rows is None and path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.rows = json.load(f)[-BOOK_N:]
            except Exception:
                self.rows = []

    def add(self, hp, n, out, now=None):
        if self.path and os.path.exists(self.path):          # shared by the bots: their flees since our last read
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.rows = json.load(f)[-BOOK_N:]
            except Exception:
                pass
        self.rows.append({"t": round(now or time.time()), "hp": round(hp), "n": int(n), "out": out})
        del self.rows[:-BOOK_N]
        if self.path:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.rows, f)
                os.replace(tmp, self.path)
            except Exception:
                pass

    @staticmethod
    def _band(hp, n):
        return min(4, int(hp) // 20), min(3, max(1, int(n)))

    def p_dead(self, hp, n):
        """-> (chance, dead, of)."""
        b = self._band(hp, n)
        rs = [r for r in self.rows if self._band(r["hp"], r["n"]) == b]
        allr = self.rows
        prior = (sum(1 for r in allr if r["out"] == "dead") + FLEE_PRIOR[0] * FLEE_PRIOR[1]) / (len(allr) + FLEE_PRIOR[1])
        d = sum(1 for r in rs if r["out"] == "dead")
        return (d + prior * FLEE_PRIOR[1]) / (len(rs) + FLEE_PRIOR[1]), d, len(rs)


def decide(m: Mid, p_flee: float):
    """-> ("stay" | "flee", stay cost s, flee cost s)."""
    stay = m.p_die * DEATH_S + m.s
    flee = FLEE_S + REPULL_S + p_flee * DEATH_S
    return ("flee" if flee < stay else "stay"), round(stay), round(flee)
