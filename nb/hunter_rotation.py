"""Hunter 1-14 combat rotation for the nb bot: State in, spell name (or None) out (nb/rotation_base.py's interface:
bar / fight_spell / buff_spell / note_cast / doomed). Profile: nb/classes.py.

Nothing here touches the game. The fight loop keeps Auto Shot running (BotEye's "/cast !Auto Shot" key, which never
toggles it off) and asks fight_spell() for the instant to add; None means "no opinion" (Auto Shot / melee carry on).

What a hunter has to level 14 (spell_template ids; the trainer teaches every even level):

    level 1    Auto Shot (75), Raptor Strike 1 (2973), Track Beasts later
    level 4    Serpent Sting 1 (1978), Aspect of the Monkey (13163)
    level 6    Hunter's Mark 1 (1130), Arcane Shot 1 (3044)
    level 8    Raptor Strike 2 (14260), Concussive Shot (5116), Serpent Sting 2 (13549 - level 10 in some data)
    level 10   Aspect of the Hawk 1 (13165), (Tame Beast quest chain - no pet before 14 in this run)
    level 12   Arcane Shot 2 (14281), Wing Clip 1 (2974)

Use (a spell missing from the bar is skipped):
    ranged (8-35 yd)   Hunter's Mark on a fresh, healthy mob when mana allows; Serpent Sting once per target while
                       the mob has 50%+ hp left (the DoT needs time); Arcane Shot on its 6 s cooldown while mana is
                       over ARCANE_MANA, and as the finisher on any mana; Concussive Shot once as the mob closes
                       (more Auto Shots before it lands in melee). No kiting (the user's rule for casters: nova-and-
                       move only; a hunter stands and shoots, then melees).
    melee (< 5 yd)     Raptor Strike on its 6 s cooldown (next-melee, the auto-attack does the rest); Wing Clip at
                       level 12 only to stop a fleeing mob (humanoids run at 15%).
    out of combat      Aspect of the Hawk (level 10+) else Aspect of the Monkey, when the strip shows no aspect.
No heal: rest is food (and drink below DRINK_MANA), and doomed() weighs hp alone.
"""

from __future__ import annotations

import time

from nb.rotation_base import BaseRotation

# spell id -> (name, mana cost). Every rank a level 1-14 hunter can put on a bar.
SPELL_IDS = {
    75: ("Auto Shot", 0),
    2973: ("Raptor Strike", 15), 14260: ("Raptor Strike", 25), 14261: ("Raptor Strike", 35),
    1978: ("Serpent Sting", 15), 13549: ("Serpent Sting", 30), 13550: ("Serpent Sting", 50),
    1130: ("Hunter's Mark", 15),
    3044: ("Arcane Shot", 25), 14281: ("Arcane Shot", 35),
    5116: ("Concussive Shot", 35),
    13163: ("Aspect of the Monkey", 20),
    13165: ("Aspect of the Hawk", 20),
    2974: ("Wing Clip", 40),
}
NAMES = {i: n for i, (n, _c) in SPELL_IDS.items()}

LEARNED_AT = {
    1: ["Auto Shot", "Raptor Strike"],
    4: ["Serpent Sting", "Aspect of the Monkey"],
    6: ["Hunter's Mark", "Arcane Shot"],
    8: ["Concussive Shot"],
    10: ["Aspect of the Hawk"],
    12: ["Wing Clip"],
}

ASPECTS = ("Aspect of the Hawk", "Aspect of the Monkey")     # best first
ASPECT_CD = 30.0              # s between aspect attempts (a missing aura bit must not spam it)
RAPTOR_CD = 6.0
ARCANE_CD = 6.0
STING_HP = 50.0               # % mob hp: a Serpent Sting only on a mob with this much left (the DoT ticks 15 s)
MARK_HP = 80.0                # % mob hp: Hunter's Mark only on a fresh mob
MARK_MANA = 50.0              # % mana for the Mark (it is 15 mana for +ranged AP: worth it on long fights only)
ARCANE_MANA = 40.0            # % mana kept back by the Arcane Shot filler (the sting and Raptor Strike come first)
FINISH_HP = 20.0              # % mob hp: Arcane Shot finishes on any mana
CONC_HP = 35.0                # % mob hp: Concussive Shot as it closes only while it has more than this
CLOSE_YD = 10.0               # "closing": the strip's within-10 yd flag while not yet in melee
DEAD_ZONE = 8.0               # yd: no ranged shot inside this (Auto Shot's minimum range)


class HunterRotation(BaseRotation):
    def __init__(self, log=None):
        super().__init__(log)
        self.raptor_next = 0.0
        self.arcane_next = 0.0
        self.stung: set = set()        # targets with our Serpent Sting this fight
        self.marked: set = set()
        self.conc: set = set()

    def spell_ids(self) -> dict:
        return SPELL_IDS

    def new_fight(self):
        self.stung, self.marked, self.conc = set(), set(), set()

    # ------------------------------------------------------------ in a fight
    def fight_spell(self, s, pulled=False, in_melee=False, in_bolt=False, attackers=0, target=0, now=None):
        """The instant to fire now, or None (Auto Shot / the auto-attack carry the fight)."""
        now = time.time() if now is None else now
        self.reason = ""
        self._sample(s, target, now)
        if s.dead or s.casting_now or s.no_control or not s.has_target or s.t_dead:
            return None
        bar = self.bar(s)
        if not bar:
            return None
        melee = bool(in_melee) and not in_bolt         # inside Auto Shot's minimum range (in_bolt: Auto Shot reads in range)
        close = bool(s.t_within28) and not in_melee and bool(s.t_on_me)
        hp_t = s.t_hp_pct

        if melee and not pulled and not s.t_on_me:
            return None                                # Bot.hunter_step steps back to open with the bow first
        if melee:
            rs = self._can(s, bar, "Raptor Strike", rng=True)
            if rs and now >= self.raptor_next:
                self.reason = f"melee, mob {hp_t:.0f}%"
                return "Raptor Strike"
            wc = self._can(s, bar, "Wing Clip")
            if wc and getattr(s, "t_fleeing", False) and target not in self.conc:
                self.reason = "the mob runs"
                return "Wing Clip"
            return None

        if close:                                      # it runs at us: slow it for more shots before melee
            cs = self._can(s, bar, "Concussive Shot", rng=True)
            if cs and hp_t > CONC_HP and target not in self.conc:
                self.reason = f"slow it as it closes ({hp_t:.0f}%)"
                return "Concussive Shot"

        hm = self._can(s, bar, "Hunter's Mark", rng=True)
        if hm and target not in self.marked and hp_t >= MARK_HP and s.mana_pct >= MARK_MANA and not pulled:
            self.reason = "mark the fresh mob"
            return "Hunter's Mark"
        ss = self._can(s, bar, "Serpent Sting", rng=True)
        if ss and target not in self.stung and hp_t >= STING_HP:
            self.reason = f"sting ({hp_t:.0f}%)"
            return "Serpent Sting"
        ar = self._can(s, bar, "Arcane Shot", rng=True)
        if ar and now >= self.arcane_next:
            if hp_t <= FINISH_HP:
                self.reason = f"finisher ({hp_t:.0f}%)"
                return "Arcane Shot"
            if s.mana_pct >= ARCANE_MANA:
                self.reason = f"filler (mana {s.mana_pct:.0f}%)"
                return "Arcane Shot"
        return None

    # ------------------------------------------------------------ bookkeeping
    def note_cast(self, name, ok, s, now=None):
        now = time.time() if now is None else now
        tgt = self.hist[-1][3] if self.hist else None
        if name == "Raptor Strike":
            self.raptor_next = now + (RAPTOR_CD if ok else 1.0)
        elif name == "Arcane Shot":
            self.arcane_next = now + (ARCANE_CD if ok else 1.0)
        elif name == "Serpent Sting" and ok:
            self.stung.add(tgt)
        elif name == "Hunter's Mark" and ok:
            self.marked.add(tgt)
        elif name == "Concussive Shot" and ok:
            self.conc.add(tgt)
        elif name in ASPECTS:
            self.buff_next = now + ASPECT_CD
        self.log_cast(name, ok, s)
