"""Class profiles: everything class-specific the bot needs, as data.

The combat engine (combat.py) and the rest of the bot are class-agnostic.
A profile tells them:

* which spell sits on which action-bar slot (the bot places them itself),
* the combat rotation as an ordered list of Abilities with conditions -
  the engine casts the first one whose conditions all hold,
* crowd control for extra attackers, emergency abilities, whether running
  away can work,
* resource (mana / rage / energy), melee or ranged, buffs, eating/drinking.

Condition vocabulary for Ability.when (all must hold):

    target_hp_below / target_hp_above      target health %
    my_hp_below / my_hp_above              own health %
    power_at_least                         absolute mana / rage / energy
    power_pct_at_least                     % of max
    enemies_at_least                       attackers not crowd-controlled
    enemy_in_melee: True/False             any such attacker within 6 yd
    target_within / target_beyond          yards
    target_missing_aura / self_missing_aura  spell name (any rank)
    target_has_aura / self_has_aura          spell name (any rank)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Ability:
    name: str                       # spell name exactly as in the spellbook
    slot: int                       # action bar slot 1-12 (keys 1..9, 0, -, =)
    when: dict = field(default_factory=dict)
    cast_time: float = 0.0          # seconds; 0 = instant
    cooldown: float = 0.0           # seconds, tracked by the bot
    target: str = "enemy"           # enemy | self | extra (a second attacker)
    then: str | None = None         # follow-up movement, e.g. "step_back"
    gcd: bool = True                # triggers the 1.5 s global cooldown
    queued: bool = False            # "on next swing": pays and lands at the
                                    # next white hit, nothing to see at press time


@dataclass
class Buff:
    name: str
    slot: int
    min_power_pct: float = 30       # don't buff below this
    duration: float = 0             # seconds; >0 = refresh before it runs out
    weapon: bool = False            # weapon enchant (Rockbiter): no aura to see,
                                    # so it is kept up by `duration` alone


@dataclass
class ClassProfile:
    name: str
    class_id: int
    power: str                      # mana | rage | energy
    ranged: bool
    engage_range: float             # stop this far from the target
    rotation: list[Ability]
    crowd_control: Ability | None = None
    opener: Ability | None = None   # cast once, at range, before closing
    emergency: list[Ability] = field(default_factory=list)
    can_escape: bool = False        # try to run when losing (needs roots / CC)
    buffs: list[Buff] = field(default_factory=list)
    drinks: bool = False            # rest with water (mana users)
    conjure_water: Ability | None = None
    conjure_food: Ability | None = None
    trainer_class: str = ""
    melee_auto_attack: bool = False  # start auto-attack when engaging
    # heal ourselves after a fight instead of eating (mana is regained
    # between fights for free; food is bought), and the mana below which we
    # drink before the next pull (None: --pull-mana)
    rest_heal: Ability | None = None
    pull_mana: float | None = None

    def bar(self) -> dict[str, int]:
        """Spell name -> slot for everything the bot should place."""
        out = {}
        for a in [*self.rotation, *self.emergency,
                  *([self.opener] if self.opener else []),
                  *([self.crowd_control] if self.crowd_control else []),
                  *([self.conjure_water] if self.conjure_water else []),
                  *([self.conjure_food] if self.conjure_food else [])]:
            out.setdefault(a.name, a.slot)
        for b in self.buffs:
            out.setdefault(b.name, b.slot)
        return out


def all_profiles() -> list[ClassProfile]:
    """Every profile in this package, found by looking.

    A hardcoded tuple here meant a new class had to be registered in two
    places, and the one that was missed was silent: bridge.install() built
    the add-on's spell-name table from ("mage", "priest", "warrior") only, so
    a paladin's spells were absent from WowBot_S and every placement answered
    "unknown spell <id>" in chat and left the bar empty. Discovery cannot go
    stale that way.
    """
    import importlib
    import pkgutil
    out = []
    for m in pkgutil.iter_modules(__path__):
        prof = getattr(importlib.import_module(f"{__name__}.{m.name}"), "PROFILE", None)
        if isinstance(prof, ClassProfile):
            out.append(prof)
    return sorted(out, key=lambda p: p.class_id)


def load(class_id: int) -> ClassProfile:
    for prof in all_profiles():
        if prof.class_id == class_id:
            return prof
    raise KeyError(f"no profile for class {class_id} yet - add one in classes/")
