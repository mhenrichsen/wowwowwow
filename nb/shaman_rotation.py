"""Shaman 1-10 combat rotation for the nb bot: State in, spell name (or None) out.

Nothing here touches the game. nb.bot.Bot asks `fight_spell()` once per fight-loop pass and
`buff_spell()` between fights, casts what comes back with Bot.cast() (keys only) and reports
the outcome through `note_cast()`. None means "no opinion": the old Lightning Bolt / melee code
keeps running, so a spell that is not on the bar (not learned yet, or no free slot) just drops out.

What the shaman has, from the world DB (spell_template + this server's trainer log); Teo trains
at levels 2/4/6/8/10:

    level 1    Lightning Bolt 1, Healing Wave 1
    level 2    Rockbiter Weapon 1 (Teo; weapon imbue, kept up by Bot.imbue)
    level 4    Earth Shock 1 (instant, 6 s shared shock cooldown, interrupts), Stoneskin Totem
    level 6    Healing Wave 2, Earthbind Totem
    level 8    Lightning Bolt 2, Earth Shock 2, Rockbiter Weapon 2, Lightning Shield 1, Stoneclaw Totem 1
    level 10   Flame Shock 1 (+ Flametongue, Searing, Strength of Earth, none of them useful solo here)

Per-level use (a spell that is missing from the bar is skipped):

    1-3   Lightning Bolt from range (filler, best damage per mana: 0.9-1.1), Healing Wave at < 55% hp.
    4-7   + Earth Shock: interrupts a casting mob, finishes a mob at <= 30%, goes off as the mob reaches
            melee at <= 65% hp, and on cooldown in melee while mana is comfortable (>= SHOCK_MANA), always
            leaving the mana for a heal: it is instant, and a bolt in melee is slowed by pushback.
            In melee the bolts stop at <= 25% mob hp: the weapon (auto-attack) finishes it.
    8-9   + Lightning Shield out of combat when mana can spare it (the strip reports the aura), or
            as the first spell of a pull; Stoneclaw Totem as the panic button (a mob on us at < 40% hp:
            it pulls the mob off us so the 3 s Healing Wave is not interrupted).
    10    + Flame Shock as an opener on a fresh, healthy mob (the DoT is weak: only with mana to burn).

Damage per mana is why the filler stays Lightning Bolt: Earth Shock 1 is 0.6 dmg/mana against 0.9 for a
bolt, so shocks are for time and for safety, not for sustained damage. Totems other than Stoneclaw
(Stoneskin 30 armour, Earthbind slow, Searing) do not pay off in a 10 s solo fight.
"""

from __future__ import annotations

import time
from collections import deque

from nb.rotation_base import BaseRotation, MELEE_FINISH_HP, in_melee_now, melee_finish   # noqa: F401
from nb import threat
from nb import resources   # heal / cast affordability (one copy with the bot)

# spell id -> (name, mana cost). Every rank a level 1-10 shaman can put on a bar.
SPELL_IDS = {
    403: ("Lightning Bolt", 15), 529: ("Lightning Bolt", 30), 548: ("Lightning Bolt", 45), 915: ("Lightning Bolt", 75),
    331: ("Healing Wave", 25), 332: ("Healing Wave", 45), 547: ("Healing Wave", 80), 913: ("Healing Wave", 155),
    8042: ("Earth Shock", 30), 8044: ("Earth Shock", 50), 8045: ("Earth Shock", 85),
    8017: ("Rockbiter Weapon", 15), 8018: ("Rockbiter Weapon", 25), 8019: ("Rockbiter Weapon", 45),
    324: ("Lightning Shield", 45), 325: ("Lightning Shield", 80),
    8050: ("Flame Shock", 55), 8052: ("Flame Shock", 95),
    5730: ("Stoneclaw Totem", 15), 6390: ("Stoneclaw Totem", 30),
    2484: ("Earthbind Totem", 30),          # flee() drops it at the start of a run (it slows the chasers 50%)
}
NAMES = {i: n for i, (n, _c) in SPELL_IDS.items()}

# trainer level -> spells it adds (documentation + the offline test)
LEARNED_AT = {
    1: ["Lightning Bolt", "Healing Wave"],
    2: ["Rockbiter Weapon"],
    4: ["Earth Shock"],
    6: [],                       # Healing Wave 2 (a rank, not a new spell)
    8: ["Lightning Shield", "Stoneclaw Totem"],
    10: ["Flame Shock"],
}

HEAL_AT = 55.0               # % hp: heal below this
HEAL_AT_HEAVY = 65.0         # ... when the mob takes more than HEAVY %/s off us
HEAVY = 6.0
PANIC_HP = 40.0              # % hp: Stoneclaw (mob on us) before the heal
SHOCK_CD = 6.0               # Earth / Flame Shock share a 6 s cooldown
SHOCK_MANA = 40.0            # % mana needed for a shock that is only there for speed; only with the mob in melee
                             # (run 4 at any range only burned mana; the human run 20261014_1959 shocked 87x in
                             # 70 of 86 level 4+ fights, 49 of them as the mob arrived)
SHOCK_ARRIVE_HP = 65.0       # % mob hp: Earth Shock it as it reaches melee at or below this (mana for shock + heal)
FINISH_HP = 30.0             # % mob hp: the finishing shock
SHIELD_CD = 25.0             # s between Lightning Shield attempts
SHIELD_SPARE_OOC = 0.50      # share of the pool that must remain after an out-of-combat shield
SHIELD_SPARE_FIGHT = 0.40
TOTEM_CD = 30.0
HEAL_CAST = 2.0              # s a Healing Wave takes here (rank 2 is 2.0 s in the DB)
LOOKAHEAD = 3.0              # s to the next heal decision: one Lightning Bolt (+ GCD, + pushback) blocks the fight loop
PROJ_FLOOR = 35.0            # heal now when the hp projected LOOKAHEAD s ahead falls below this
GAP_RESET = 8.0              # s without a sample = a new fight (3.0 fired between two bolts: 'losing 0.0%/s' at 13:28:43)
HEAL_GAIN = 25.0             # % hp one Healing Wave nets under fire (inst3 15:32: 33->53, 24->41, 24->49)
FLEE_MARGIN = 1.4            # doomed(): damage still to come > this x (hp + the heals our mana buys) ...
# ... and dead within this many seconds without a heal. Was 10 with the run check at the FULL fight rate: hp/inc < 10
# and hp - 20*inc > 15 cannot both hold, so the cost check never fired (bot 1 23:16:22-44: fresh bear at 81% hp and
# 22% mana, no heal left, fought on to the death)
FLEE_TTD = 20.0
FLEE_MARGIN_NOHEAL = 0.85    # doomed() margin with no mana for a Healing Wave: flee a fight that is merely even
FLEE_CHASE = 1.0             # share of the fight's damage rate taken while running from melee chasers (dazes): ~all
#                              of it (2026-10-15: inst3 00:53 53%->3% and 01:11 55%->0 in 19 s, inst4 00:46 64%->20%
#                              chased by one Wendigo; 0.5 let those flees through)
# the static break-off rules (a pack on us, a mob far over our level) live in nb/threat.py: travel_abort uses the same
# ones (review 2026-10-15 phase 2). Aliases for the old names:
FLEE_PACK, FLEE_PACK_HP = threat.FLEE_PACK, threat.FLEE_PACK_HP
ELITE_LEVELS, FLEE_LEVEL_GAP = threat.ELITE_LEVELS, threat.FLEE_LEVEL_GAP
FLEE_CASTER_HP = 50.0        # % hp: below this a caster is fought out, not run from: it bolts the runner all the way
FLEE_FINISH_HP = 20.0       # % mob hp: at or below, finish it before any flee check
FLEE_MULTI_HP = 70.0         # % hp: 2+ on us below this: fight it out (melee chasers keep pace; 1 of 7 flees survived)
FLEE_RUN_S = 20.0            # s of running at the fight's damage rate a flee must survive (with 15% to spare)
                             # (inst3 17:46:27 fled a Seer at 14% hp: dead after 5 s / 19 yd; 17:36:56 at 75%: got away)


class Rotation(BaseRotation):
    """The shaman's rotation (nb/classes.py: the shaman profile)."""

    def __init__(self, log=None):
        self.log = log or (lambda m: None)
        self.shock_next = 0.0          # the strip's ready flag lies in combat: pace shocks ourselves
        self.heal_next = 0.0
        self.totem_next = 0.0
        self.shield_next = 0.0
        self.shield_blind = 0          # shield cast fine but the aura bit never showed
        self.shield_off_until = 0.0
        self.fs_target = None
        self.seen: set[str] = set()
        self.hist: deque = deque(maxlen=80)    # (t, hp%, target hp%, target)
        self.inc = self.out = 0.0              # latest rates from _sample (% of our / its hp per s)
        self.fight_t0 = 0.0                    # first sample of this fight
        self.out_avg = 0.0                     # % of the target's hp per s since we first saw it this fight
        self.casters: set = set()              # targets seen casting this fight
        self.reason = ""

    # ------------------------------------------------------------ bar
    @staticmethod
    def bar(s) -> dict:
        """name -> (slot, spell id, mana cost) of the best rank on the bar, from the strip's action ids."""
        out: dict = {}
        for slot, sid in enumerate(s.actions, 1):
            hit = SPELL_IDS.get(sid)
            if hit and (hit[0] not in out or hit[1] > out[hit[0]][2]):
                out[hit[0]] = (slot, sid, hit[1])
        return out

    @staticmethod
    def _can(s, bar, name, rng: bool = False):
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
    def _sample(self, s, target, now):
        if self.hist and now - self.hist[-1][0] > GAP_RESET:
            self.hist.clear()                         # a new fight
        if not self.hist:
            self.fight_t0 = now
            self.casters = set()
        if s.t_casting and target:
            self.casters.add(target)
        if not self.hist or now - self.hist[-1][0] >= 0.2:
            self.hist.append((now, s.hp_pct, s.t_hp_pct, target))
        # fight-long rate on this target: the 5 s window reads 0.0 while nothing lands (inst3 17:46:23-27: Seer at
        # 18% 'at 0.0%/s' -> ttk 60 s -> fled; it had gone 100 -> 18% in 22 s)
        same = [x for x in self.hist if x[3] == target]
        self.out_avg = (max(0.0, same[0][2] - same[-1][2]) / (same[-1][0] - same[0][0])
                        if len(same) >= 2 and same[-1][0] - same[0][0] >= 3.0 else 0.0)
        h = [x for x in self.hist if now - x[0] <= 5.0]
        if len(h) < 2 or h[-1][0] - h[0][0] < 1.0:
            self.inc = self.out = 0.0
            return 0.0, 0.0
        dt = h[-1][0] - h[0][0]
        inc = sum(max(0.0, a[1] - b[1]) for a, b in zip(h, h[1:])) / dt          # % of our hp lost per s
        out = sum(max(0.0, a[2] - b[2]) for a, b in zip(h, h[1:]) if a[3] == b[3]) / dt   # % of its hp per s
        self.inc, self.out = inc, out
        return inc, out

    def doomed(self, s, attackers, now=None, pack=None, elite=False) -> str:
        """Why this fight is lost at the current pace, or "". Lost: what the attackers still take off us before the
        last one dies (each further one from full, at the damage rate we deal now) is over FLEE_MARGIN x (hp + the
        heals our mana buys), and we are under FLEE_TTD s from death. Replayed over the 2026-10-14 logs it fires in
        the inst2 wolf fight at 16:04:37-39 (55-66% hp, 12-14 s before the death) and for Vejrek at 13:29:57; of
        the fights won only at inst3 15:32:29-32 (two level+1 mobs, 32-41% hp, 5% mana; it ended at 24% hp)."""
        now = time.time() if now is None else now
        inc, out = self.inc, self.out          # from the previous pass (fight_spell samples)
        if attackers < 1 or s.dead or s.t_dead:
            return ""
        pack = attackers if pack is None else pack
        # a pack only makes us break off before anything is invested (hp and the mob still high): of the 2026-10-14
        # flees with 2+ on us, all 3 from 88%+ hp got away, 1 of 7 from under 60% did (bot 1 21:14: fled 3 trolls at
        # 59% with its target at 12%, chased down to 1% in 25 s). inst3 17:42:51: 3 Seers at 100% -> stay away.
        if threat.pack_flee(pack, s.hp_pct, s.t_hp_pct):
            return f"doomed: {pack} on us at {s.hp_pct:.0f}% hp"
        # a mob 2+ levels over us on us with most of its hp: run before anything is invested. Old Icebeard (11) at
        # its cave mouth killed level 9 bots at full hp three times (inst2 03:18, inst3 04:06, inst4 04:15)
        # an elite counts ELITE_LEVELS over its level: Old Icebeard (11 elite) took a level 10 bot from 89% to 0 in 30 s
        # with two heals (inst3 04:31), mob still at 66%
        tl = getattr(s, "t_level", 0) or 0
        if threat.level_flee(s.level, tl, s.t_hp_pct, elite, getattr(s, "t_trivial", False)):
            return f"doomed: level {tl}{' elite' if elite else ''} vs our {s.level} at {s.t_hp_pct:.0f}% mob hp"
        if s.t_hp_pct <= FLEE_FINISH_HP:
            return ""                          # finish it first: running from a mob at 12% never helped (1%, 12%)
        heal_mana = resources.heal_ok(s, self.bar(s))          # a Healing Wave on the bar and affordable now
        if attackers >= 2 and s.hp_pct < FLEE_MULTI_HP and heal_mana:
            # melee chasers keep pace and daze: fight it out and heal (1 of 7 escaped). Only with a heal left: bot 1
            # 22:09:55-22:10:06 fought two fresh Wendigos at 3% mana and died; then the cost check below decides
            return ""
        if inc < 2.0 or now - self.fight_t0 < 5.0:
            return ""
        out = out if out > 0.5 else self.out_avg
        if out <= 0.5:
            # no measured damage rate on the mob (a fresh add, a stalled pass): a guessed 60 s kill made fights doomed
            # that were not (inst3 00:53:46 "mob 81% at 0.0%/s" -> fled at 53% with 28% mana, chased 25 s to 3%, died;
            # inst2 23:44:22, inst4 00:22:26 the same 0.0%/s)
            return ""
        ttk1 = s.t_hp_pct / out
        if ttk1 <= 3.0:
            return ""                          # it dies in the next few seconds: kill it, then judge the rest
        if s.hp_pct - inc * FLEE_CHASE * FLEE_RUN_S <= 15:
            return ""                          # the run itself loses hp (a chaser hits less often): a run we cannot survive
        per = 100.0 / out if out > 0.5 else 60.0
        inc1 = inc / attackers
        need = sum(inc1 * (attackers - k) * (ttk1 if k == 0 else per) for k in range(attackers))
        hw = self.bar(s).get("Healing Wave")
        pool = s.hp_pct + (int(s.mana // hw[2]) * HEAL_GAIN if hw and hw[2] else 0.0)
        # no heal left: the estimate has no mana to absorb a miss, and a death (ghost run, minutes) costs far more than a
        # flee (~20 s). inst2 00:08:53: a fresh mob at 56% hp / 3% mana, need 60 vs pool 56 (< 1.4x): fought on, died
        # with it at 27%
        margin = FLEE_MARGIN if heal_mana else FLEE_MARGIN_NOHEAL
        if need > margin * pool and s.hp_pct / inc < FLEE_TTD:
            if self._cur_target() in self.casters and s.hp_pct < FLEE_CASTER_HP:
                return ""                      # running only adds the run to the fight: fight it out
            return (f"doomed: {need:.0f}% still to take vs {pool:.0f}% hp+heals, losing {inc:.1f}%/s, "
                    f"mob {s.t_hp_pct:.0f}% at {out:.1f}%/s, {attackers} on us")
        return ""

    # ------------------------------------------------------------ in a fight
    def fight_spell(self, s, pulled=False, in_melee=False, in_bolt=False, attackers=0, target=0, now=None):
        """The instant / support spell to cast now, or None (then: Lightning Bolt, or melee at low mana)."""
        now = time.time() if now is None else now
        self.reason = ""
        inc, out = self._sample(s, target, now)
        if s.dead or s.casting_now or s.no_control or not s.has_target or s.t_dead:
            return None                                # never start a cast on top of a cast
        bar = self.bar(s)
        if not bar:
            return None
        on_me = bool(s.t_on_me or attackers > 0)
        out = out if out > 0.5 else self.out_avg
        ttk = s.t_hp_pct / out if out > 0.5 else 99.0   # seconds until the mob dies at the current pace
        lb = self._can(s, bar, "Lightning Bolt")
        es = self._can(s, bar, "Earth Shock", rng=True)
        if es and now < self.shock_next:
            es = None
        hw = self._can(s, bar, "Healing Wave")
        hw_b = bar.get("Healing Wave")
        caster = target in self.casters
        sc = self._can(s, bar, "Stoneclaw Totem")
        if sc and (now < self.totem_next or not s.t_within10 or not on_me or s.t_hp_pct <= 25
                   or (hw and s.mana < sc[2] + hw[2])):
            sc = None                                  # keep the mana for the heal it protects

        # 1. heal (Healing Wave takes ~3 s: a mob that is about to die is cheaper than the heal)
        if hw and now >= self.heal_next:
            # two on us or a heavy hitter: heal earlier; and judge the hp we will have at the NEXT decision, one
            # bolt from now (inst2 16:04:39 hp 55 'fine' -> 16:04:42 heal at 30%; two wolves took 7-11 %/s)
            # a caster hits in ~18% lumps (Seer bolt ~28 of ~160 hp): the 5 s rate lags, heal from the heavy line
            limit = HEAL_AT_HEAVY if (inc >= HEAVY or attackers >= 2 or caster) else HEAL_AT
            # 'finish instead of heal' only when the mob dies before we drop under 20% at this pace (inst3 17:46:21:
            # skipped the heal at 38% hp for a Seer at 35%, ~10 s from dead at 3.5%/s)
            finish = (s.t_hp_pct <= 35 and s.hp_pct >= 30 and lb and s.mana_pct >= 20 and attackers < 2
                      and s.hp_pct - inc * ttk > 20) or \
                     (ttk < 2.5 and s.hp_pct - inc * ttk > 25)
            if (s.hp_pct < limit or s.hp_pct - inc * LOOKAHEAD < PROJ_FLOOR) and not finish:
                lethal = on_me and s.hp_pct - inc * (HEAL_CAST + 0.5) <= 5
                if es and ((lethal and ttk < HEAL_CAST and s.t_hp_pct <= 30)
                           or (on_me and s.hp_pct < 30 and 0 < s.t_hp_pct <= 25)):
                    self.reason = f"finisher before the heal ({s.hp_pct:.0f}% hp, mob {s.t_hp_pct:.0f}%)"
                    return "Earth Shock"               # instant: the cast would not land before we do
                if sc and (s.hp_pct < PANIC_HP or attackers >= 2):
                    self.reason = f"panic ({s.hp_pct:.0f}% hp, {attackers} on us)"
                    return "Stoneclaw Totem"
                self.reason = f"heal at {s.hp_pct:.0f}% (mob {s.t_hp_pct:.0f}%, losing {inc:.1f}%/s)"
                return "Healing Wave"

        # 2. interrupt a casting mob
        # ... but not with the heal's mana while we are the one who needs it (17:46:22: third interrupt, 24 -> 10% mana)
        if es and s.t_casting and s.t_hp_pct > 0 and not (hw_b and s.hp_pct < HEAL_AT_HEAVY
                                                          and s.mana - es[2] < hw_b[2] and s.t_hp_pct > 15):
            self.reason = "interrupt"
            return "Earth Shock"

        # 3. Lightning Shield: one GCD now pays back over the rest of the fight (3 charges)
        ls = self._can(s, bar, "Lightning Shield")
        if ls and not (s.auras & 1) and now >= self.shield_next and now >= self.shield_off_until \
                and s.hp_pct >= 50 and s.mana - ls[2] >= SHIELD_SPARE_FIGHT * s.manamax \
                and (not pulled or s.t_hp_pct >= 60) and not (in_melee and attackers >= 2):
            self.reason = "shield up before the mob lands"
            return "Lightning Shield"

        # 4. Flame Shock opener on a fresh mob (level 10; the DoT is small, so only with mana to burn)
        fs = self._can(s, bar, "Flame Shock", rng=True)
        if fs and now >= self.shock_next and target != self.fs_target and s.t_hp_pct >= 70 \
                and s.mana_pct >= 70 and s.mana - fs[2] >= 0.5 * s.manamax:
            self.reason = "opener"
            return "Flame Shock"

        # 5. Earth Shock: finisher, as the mob arrives, else on cooldown in melee while mana is comfortable.
        # A heal must stay affordable after the shock (except the old hurting finisher: it ends the fight now)
        if es:
            heal_left = not hw_b or s.mana - es[2] >= hw_b[2]
            if on_me and 0 < s.t_hp_pct <= FINISH_HP and (s.hp_pct < 45 or heal_left):
                self.reason = f"finisher ({s.hp_pct:.0f}% hp, mob {s.t_hp_pct:.0f}%)"
                return "Earth Shock"
            if in_melee_now(s, in_melee) and 0 < s.t_hp_pct <= SHOCK_ARRIVE_HP and heal_left:
                self.reason = f"mob in melee at {s.t_hp_pct:.0f}% (mana {s.mana_pct:.0f}%)"
                return "Earth Shock"
            if in_melee_now(s, in_melee) and s.mana_pct >= SHOCK_MANA and s.mana - es[2] >= 0.35 * s.manamax \
                    and s.t_hp_pct >= 40 and heal_left:
                self.reason = f"on cooldown in melee (mana {s.mana_pct:.0f}%)"
                return "Earth Shock"
        return None

    # ------------------------------------------------------------ between fights
    def buff_spell(self, s, now=None):
        """Out-of-combat buff to cast now (Rockbiter Weapon stays with Bot.imbue), or None."""
        now = time.time() if now is None else now
        self.reason = ""
        if s.dead or s.ghost or s.combat or s.moving or s.casting_now or s.no_control:
            return None
        bar = self.bar(s)
        ls = self._can(s, bar, "Lightning Shield")
        if ls and not (s.auras & 1) and now >= self.shield_next and now >= self.shield_off_until \
                and s.hp_pct >= 60 and s.mana - ls[2] >= SHIELD_SPARE_OOC * s.manamax:
            self.reason = "no shield up"
            return "Lightning Shield"
        return None

    # ------------------------------------------------------------ bookkeeping
    def note_cast(self, name, ok, s, now=None):
        """Called by the bot after it tried `name`: timers, the first-use log line, shield sanity."""
        now = time.time() if now is None else now
        if name == "Healing Wave":
            # cast() returns once the wave has landed: 3.5 s more skipped a whole bolt cycle (inst2 16:04:44 42% ->
            # no heal -> 16:04:47 24% -> no heal (47.5) -> 16:04:50 4%)
            self.heal_next = now + 1.0
        elif name in ("Earth Shock", "Flame Shock"):
            self.shock_next = now + (SHOCK_CD if ok else 1.0)
            if ok and name == "Flame Shock":
                self.fs_target = self._cur_target()
        elif name == "Stoneclaw Totem":
            self.totem_next = now + (TOTEM_CD if ok else 5.0)
        elif name == "Lightning Shield":
            self.shield_next = now + (SHIELD_CD if ok else 3 * SHIELD_CD)
            if ok:
                if s.auras & 1:
                    self.shield_blind = 0
                else:
                    self.shield_blind += 1              # cast, but the strip does not show the aura
                    if self.shield_blind >= 2:
                        self.shield_off_until = now + 600
                        self.shield_blind = 0
                        self.log("rot: Lightning Shield aura never shows in the strip; off for 10 min")
        if not ok:
            self.log(f"rot: {name} did not go off ({self.reason})")
            return
        if name not in self.seen:
            self.seen.add(name)
            self.log(f"rot: first use of {name} at level {s.level} ({self.reason})")
        elif name != "Healing Wave":                    # the bot already logs its heals
            self.log(f"rot: {name} ({self.reason}) hp {s.hp_pct:.0f} mana {s.mana_pct:.0f} mob {s.t_hp_pct:.0f}")

    def _cur_target(self):
        return self.hist[-1][3] if self.hist else None
