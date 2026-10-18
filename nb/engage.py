"""Engage watchdog: one progress tracker for a fight's target (review 2026-10-15, phase 3 step 2).

bot._fight carried separate clocks for the same question, "is this fight getting anywhere": the stall check (mob hp
unmoved 5 / 12 s -> "fight stalled ... checking facing"), the line-of-sight count (3x err 3 -> give up), "never
reached it in 12 s", "no damage on it ... in 15 s", the melee finish's FINISH_STALL / FINISH_BOLT, the swing retries
and "it is on us but does not come: walk to it after 6 / 12 s of fight". Here they are one tracker:

  progress   - the target's hp dropping (HP_STEP below the lowest seen), or the distance closing: a better range band
               (within 10 < bolt / 28 yd < beyond), the plate lower on screen by SY_STEP x FOCAL (the camera looks down
               on the ground: lower = closer), or the target's position (t_x / t_y, when the strip has it) CLOSE_YD
               nearer.
  give up    - not hit yet (never on us, hp not 2 under its start) and no progress for GIVEUP_NEAR s (12; 15 once it is
               engaged / pulled or the combat flag is up; 25 for a mob targeted by name beyond plate range); or
               LOS_GIVEUP_N "not in line of sight" errors before it engaged. The caller blacklists through the Backoff.
  recoveries - on us (or the combat flag up) with its hp not dropping: face at STALL_MELEE s (on us in melee) /
               STALL_RANGE s, interact-walk TIER_STEP s later, reposition TIER_STEP s after that; with the target NOT on
               us a last tier gives it up. Then the ladder starts over (a mob on us is never given up here: doomed() /
               threat decide a flee).
  come wait  - on us, out of melee, not closing for COME_WAIT s (COME_WAIT_CASTER for a caster): walk to it.
  finish     - hp_still() + finish_still_step(): the melee finish's "hp not moving" clock (FINISH_STALL interact, FINISH_BOLT bolt).
  chase      - Chase: a fleeing target is chased while the distance closes or its hp drops; CHASE_GIVEUP_S without
               either and the chase is over.

swing_step() holds ensure_swing()'s retry timing (SWING_RETRY / SWING_TRIES); the key presses stay in bot.py.
"""

from __future__ import annotations

import math
import time
from collections import namedtuple

GIVEUP_NEAR = 12.0       # s without progress: never reached it (not engaged, not pulled, no combat flag)
GIVEUP_HIT = 15.0        # ... engaged / pulled / combat flag up, but no damage on it and it is not on us
GIVEUP_FAR = 25.0        # ... targeted by name beyond plate range: the walk takes longer
LOS_GIVEUP_N = 3         # "not in line of sight" errors before it engaged (nameplates show through walls)
HIT_DROP = 2.0           # % under its starting hp: our damage lands
HP_STEP = 1.0            # % hp drop that counts as progress
SY_STEP = 0.02           # x FOCAL px lower on screen: closer
CLOSE_YD = 2.0           # yd nearer (t_x / t_y)
STALL_MELEE = 5.0        # s: on us in melee, hp not dropping: face
STALL_RANGE = 12.0       # s: otherwise
TIER_STEP = 3.0          # s between the recovery tiers
COME_WAIT, COME_WAIT_CASTER = 12.0, 6.0
FINISH_STALL = 3.0       # s: melee finish with its hp not moving: interact into reach
FINISH_BOLT = 6.0        # s: ... still not moving: the bolts finish it
SWING_RETRY = 1.2        # s
SWING_TRIES = 4
SWING_HOLD = 0.8         # s without a bolt after an interact press
SWING_ATTACK_EVERY = 3.0  # s: Attack retried this often once the swing counts as not startable
CHASE_GIVEUP_S = 8.0     # s of a chase without progress
CHASE_PRESS_S = 0.7      # s between interact-walk presses on a runner

Action = namedtuple("Action", "kind why")     # kind: face | interact | reposition | giveup
TIERS = ("face", "interact", "reposition")


def band(s) -> int:
    """0 within 10 yd, 1 within bolt / 28 yd, 2 beyond."""
    if getattr(s, "t_within10", False):
        return 0
    if getattr(s, "t_within28", False) or getattr(s, "t_in_ranged", False):
        return 1
    return 2


def target_dist(s):
    tx, ty = getattr(s, "t_x", None), getattr(s, "t_y", None)
    if tx is None or ty is None:
        return None
    return math.hypot(tx - s.x, ty - s.y)


class Engage:
    def __init__(self, far=False, focal=900.0, clock=time.time):
        self.clock = clock
        self.far = far
        self.focal = focal
        self.rearm()

    def rearm(self, attacker=False, s=None):
        """A new target: forget the old one's progress. attacker: a mid-fight pick (it is on us: never 'no damage')."""
        now = self.clock()
        self.t_prog = self.t_close = self.t_hit = now
        self.hp0 = self.best_hp = None
        self.best_band, self.best_sy, self.best_d = 9, None, None
        self.hit = bool(attacker)
        self.engaged = bool(s is not None and getattr(s, "t_on_me", False))
        self.los_n = 0
        self.los_seen = getattr(s, "errn", None) if s is not None else None
        self.tier = 0

    # ---------------------------------------------------------------- input
    def update(self, s, plate=None) -> bool:
        """Feed one state (and the target plate). -> whether it showed progress."""
        now = self.clock()
        if self.los_seen is None:
            self.los_seen = getattr(s, "errn", None)
        if s.t_on_me or s.t_hp_pct < 99:
            self.engaged = True
        prog = False
        hp = s.t_hp_pct
        if self.hp0 is None:
            self.hp0 = self.best_hp = hp
        elif hp <= self.best_hp - HP_STEP:
            self.best_hp = hp
            self.t_hit = now
            self.tier = 0
            prog = True
        if s.t_on_me or (self.hp0 is not None and hp < self.hp0 - HIT_DROP):
            self.hit = True
        closer = False
        b = band(s)
        if b < self.best_band:
            closer = self.best_band != 9
            self.best_band = b
        sy = getattr(plate, "sy", -1) if plate is not None and getattr(plate, "sx", -1) >= 0 else -1
        if sy is not None and sy >= 0:
            if self.best_sy is None:
                self.best_sy = sy
            elif sy >= self.best_sy + SY_STEP * self.focal:
                self.best_sy = sy
                closer = True
        d = target_dist(s)
        if d is not None:
            if self.best_d is None:
                self.best_d = d
            elif d <= self.best_d - CLOSE_YD:
                self.best_d = d
                closer = True
        if closer:
            self.t_close = now
            prog = True
        if prog:
            self.t_prog = now
        if getattr(s, "err", 0) == 3 and getattr(s, "errn", None) != self.los_seen:
            self.los_seen = s.errn
            if not self.engaged and not s.t_on_me:
                self.los_n += 1
        return prog

    # ---------------------------------------------------------------- clocks
    def idle(self) -> float:
        """s since any progress."""
        return self.clock() - self.t_prog

    def hp_still(self) -> float:
        """s since the target's hp last dropped (the melee finish's clock)."""
        return self.clock() - self.t_hit

    def come_wait(self, caster=False) -> bool:
        """On us but not in melee and not closing for COME_WAIT(_CASTER) s: walk to it."""
        return self.clock() - self.t_close > (COME_WAIT_CASTER if caster else COME_WAIT)

    # ---------------------------------------------------------------- verdicts
    def check(self, s, pulled=False) -> Action | None:
        """One recovery / give-up due now, else None. Call after update()."""
        now = self.clock()
        if self.los_n >= LOS_GIVEUP_N and not self.engaged and not s.t_on_me:
            return Action("giveup", f"no line of sight to it ({self.los_n}x)")
        if not self.hit and not s.t_on_me:
            near = not (self.engaged or pulled or s.combat)
            lim = GIVEUP_FAR if self.far else (GIVEUP_NEAR if near else GIVEUP_HIT)
            if now - self.t_prog > lim:
                if near:
                    return Action("giveup", f"never reached it in {lim:.0f} s")
                return Action("giveup", f"no damage on it and not on us in {lim:.0f} s (err {getattr(s, 'err', 0)})")
        if not (s.t_on_me or s.combat):
            return None
        melee = s.t_on_me and s.t_within10
        tiers = TIERS + (() if s.t_on_me else ("giveup",))
        due = (STALL_MELEE if melee else STALL_RANGE) + TIER_STEP * self.tier
        still = now - self.t_hit
        if still < due:
            return None
        kind = tiers[min(self.tier, len(tiers) - 1)]
        self.tier += 1
        if self.tier >= len(tiers):
            self.tier = 0
            self.t_hit = now                     # the ladder starts over
        why = f"no progress for {still:.1f}s at {s.t_hp_pct:.0f}% hp"
        if kind == "giveup":
            why = f"not on us and no damage on it for {still:.0f} s"
        return Action(kind, why)


def finish_still_step(fin, still, casting, now, sw) -> bool:
    """The melee finish's interact press: past FINISH_STALL s without the mob's hp moving, once per SWING_RETRY."""
    return bool(fin and still > FINISH_STALL and not casting and now >= sw["next"])


def swing_step(sw, autoattack, casting, within10, now) -> str:
    """ensure_swing()'s timing. -> "on" (swing runs: counters reset), "wait" (nothing now, still startable), "dead"
    (nothing now, not startable), "attack" / "interact" (press now, startable), "attack_retry" (not startable: press
    Attack, SWING_ATTACK_EVERY s). The caller presses; sw is updated here."""
    if autoattack:
        sw["n"], sw["next"] = 0, 0.0
        return "on"
    if casting or not within10 or now < sw["next"]:
        return "wait" if sw["n"] < SWING_TRIES else "dead"
    if sw["n"] >= SWING_TRIES:
        sw["next"] = now + SWING_ATTACK_EVERY
        return "attack_retry"
    kind = "attack" if sw["n"] == 0 else "interact"
    sw["n"] += 1
    sw["next"] = now + SWING_RETRY
    return kind


class Chase:
    """A fleeing target chased with interact-walk: progress = the distance closing or its hp dropping (an Engage
    tracker); over after CHASE_GIVEUP_S without either."""

    def __init__(self, focal=900.0, clock=time.time):
        self.clock = clock
        self.eg = Engage(focal=focal, clock=clock)
        self.t0 = clock()
        self.press_next = 0.0
        self.said = False

    def update(self, s, plate=None) -> bool:
        return self.eg.update(s, plate)

    def stalled(self) -> bool:
        return self.eg.idle() > CHASE_GIVEUP_S

    def press_due(self) -> bool:
        now = self.clock()
        if now >= self.press_next:
            self.press_next = now + CHASE_PRESS_S
            return True
        return False
