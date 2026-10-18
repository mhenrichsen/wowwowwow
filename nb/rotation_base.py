"""The rotation interface every class's rotation implements, and the base rotation classes without one of their own
use (nb/classes.py: Profile.rotation). State in, spell name (or None) out; nothing here touches the game.

    bar(s)            name -> (slot, spell id, mana cost) of the best rank on the bar (the profile's spell table)
    fight_spell(...)  the spell to fire now in a fight, or None (the ranged auto attack / nuke / weapon carry on)
    buff_spell(s)     an out-of-combat self-buff to cast now, or None
    note_cast(...)    the outcome of a cast the bot made for us
    doomed(...)       why the fight is lost (flee), or ""
    inc / out         % of our / the target's hp per s, from the samples fight_spell takes
    casters           targets that stand off and cast (they are walked to, not waited for)

The base rotation casts no instants, keeps the profile's buffs up (BotEye aura bit 0: one of them is on us) and
judges a fight on hp alone (no heals to count)."""
from __future__ import annotations

import time
from collections import deque

from nb import threat

GAP_RESET = 8.0               # s without a sample = a new fight
FLEE_MARGIN = 1.0             # doomed(): damage still to take > this x hp (no heals counted)
FLEE_TTD = 15.0               # ... and under this many s from death at the current rate
BUFF_BIT = 1                  # BotEye aura bit 0: a class self-buff is up
BUFF_CD = 30.0                # s between buff attempts (a missing aura bit must not spam it)
MELEE_FINISH_HP = 25.0        # % mob hp: in melee, no more nukes at or below this; the weapon finishes it


def in_melee_now(s, in_melee=False) -> bool:
    """The target is within 10 yd (the strip's distance flag) or the caller says it is in melee."""
    return bool(in_melee or s.t_within10)


def melee_finish(s, caster=False, swing_ok=True) -> bool:
    """In melee with the mob at or below MELEE_FINISH_HP: leave it to the weapon instead of another cast. Not for a
    caster mob (it stands 5-10 yd off and casts back: no swing lands) and not while the swing could not be started
    (swing_ok=False), or the fight only idles (inst3 17:46:23-26: Seer at 18%, 4 s of no cast and no swing)."""
    return bool(s.t_within10 and 0 < s.t_hp_pct <= MELEE_FINISH_HP and not caster and swing_ok)


class BaseRotation:
    profile = None                # nb.classes.Profile (set by Profile.make_rotation)
    heal_next = 0.0               # rest() waits for this before a heal (rotations with a heal pace it)

    def __init__(self, log=None):
        self.log = log or (lambda m: None)
        self.buff_next = 0.0
        self.seen: set[str] = set()
        self.hist: deque = deque(maxlen=80)    # (t, hp%, target hp%, target)
        self.inc = self.out = 0.0
        self.fight_t0 = 0.0
        self.reason = ""
        self.casters: set = set()

    # ------------------------------------------------------------ bar
    def spell_ids(self) -> dict:
        return self.profile.spells if self.profile is not None else {}

    def bar(self, s) -> dict:
        out: dict = {}
        ids = self.spell_ids()
        for slot, sid in enumerate(s.actions, 1):
            hit = ids.get(sid)
            if hit and (hit[0] not in out or hit[1] > out[hit[0]][2]):
                out[hit[0]] = (slot, sid, hit[1])
        return out

    @staticmethod
    def _can(s, bar, name, rng=False):
        """(slot, id, cost) when the spell is on the bar, off cooldown and affordable (and in range if asked)."""
        b = bar.get(name)
        if not b:
            return None
        slot, _sid, cost = b
        if not s.ready(slot) or s.mana < cost:
            return None
        if rng and not s.in_range(slot):
            return None
        return b

    # ------------------------------------------------------------ rates
    def new_fight(self):
        """A sample gap: per-fight state starts over (subclasses clear their own sets)."""

    def reset_fight(self):
        """Bot._fight starts a new fight: the rates and per-fight state start over."""
        self.hist.clear()
        self.inc = self.out = 0.0
        self.new_fight()

    def _sample(self, s, target, now):
        if self.hist and now - self.hist[-1][0] > GAP_RESET:
            self.hist.clear()
            self.new_fight()
        if not self.hist:
            self.fight_t0 = now
        if not self.hist or now - self.hist[-1][0] >= 0.2:
            self.hist.append((now, s.hp_pct, s.t_hp_pct, target))
        h = [x for x in self.hist if now - x[0] <= 5.0]
        if len(h) < 2 or h[-1][0] - h[0][0] < 1.0:
            self.inc = self.out = 0.0
            return 0.0, 0.0
        dt = h[-1][0] - h[0][0]
        self.inc = sum(max(0.0, a[1] - b[1]) for a, b in zip(h, h[1:])) / dt
        self.out = sum(max(0.0, a[2] - b[2]) for a, b in zip(h, h[1:]) if a[3] == b[3]) / dt
        return self.inc, self.out

    def doomed(self, s, attackers, now=None, pack=None, elite=False) -> str:
        """Why this fight is lost, or "": the pack / level rules (threat.py), then hp alone - what the attackers
        still take off us before they die is over FLEE_MARGIN x our hp and we are under FLEE_TTD s from death."""
        now = time.time() if now is None else now
        if attackers < 1 or s.dead or s.t_dead:
            return ""
        pack = attackers if pack is None else pack
        if threat.pack_flee(pack, s.hp_pct, s.t_hp_pct):
            return f"doomed: {pack} on us at {s.hp_pct:.0f}% hp"
        tl = getattr(s, "t_level", 0) or 0
        if threat.level_flee(s.level, tl, s.t_hp_pct, elite, getattr(s, "t_trivial", False)):
            return f"doomed: level {tl}{' elite' if elite else ''} vs our {s.level} at {s.t_hp_pct:.0f}% mob hp"
        inc, out = self.inc, self.out
        if s.t_hp_pct <= 20 or inc < 2.0 or out <= 0.5 or now - self.fight_t0 < 5.0:
            return ""
        ttk = s.t_hp_pct / out
        need = inc * ttk + (attackers - 1) * inc / max(attackers, 1) * (100.0 / out)
        if need > FLEE_MARGIN * s.hp_pct and s.hp_pct / inc < FLEE_TTD:
            if attackers == 1 and s.hp_pct - threat.flee_loss(1.0, inc) < threat.FLEE_SURVIVE_HP:
                return ""          # a lone chaser hits us all the way: the run kills us surer than the fight
            return f"doomed: {need:.0f}% still to take vs {s.hp_pct:.0f}% hp, losing {inc:.1f}%/s"
        return ""

    # ------------------------------------------------------------ in a fight
    def fight_spell(self, s, pulled=False, in_melee=False, in_bolt=False, attackers=0, target=0, now=None):
        """No instants of its own: the ranged attack / nuke / weapon carry the fight (Bot._fight)."""
        self._sample(s, target, time.time() if now is None else now)
        return None

    # ------------------------------------------------------------ between fights
    def buff_spell(self, s, now=None):
        """The first castable profile buff while none is up (aura bit 0)."""
        now = time.time() if now is None else now
        self.reason = ""
        if s.dead or s.ghost or s.combat or s.casting_now or s.no_control:
            return None
        if s.auras & BUFF_BIT or now < self.buff_next or self.profile is None:
            return None
        bar = self.bar(s)
        for b in self.profile.buffs:
            if self._can(s, bar, b):
                self.reason = "no class buff up"
                return b
        return None

    # ------------------------------------------------------------ bookkeeping
    def note_cast(self, name, ok, s, now=None):
        now = time.time() if now is None else now
        if self.profile is not None and name in self.profile.buffs:
            self.buff_next = now + BUFF_CD
        self.log_cast(name, ok, s)

    def log_cast(self, name, ok, s):
        if not ok:
            self.log(f"rot: {name} did not go off ({self.reason})")
            return
        if name not in self.seen:
            self.seen.add(name)
            self.log(f"rot: first use of {name} at level {s.level} ({self.reason})")
        else:
            self.log(f"rot: {name} ({self.reason}) hp {s.hp_pct:.0f} mana {s.mana_pct:.0f} mob {s.t_hp_pct:.0f}")
