"""Class profiles: everything class-specific the nb bot needs, in one place. The bot is general; which class it plays
comes from configuration (login.RACECLASS for the instance -> route.CLASS), never from guessing.

A profile names spells by role, so the fight / rest / flee code asks "the class's heal", "its ranged attack", "its
snare" instead of naming a class's spells:

    spells       spell id -> (name, mana cost): every rank the bot recognises on the action bar (BotEye's
                 BAR_FAMILIES keeps the best rank on the bar)
    rotation     the class's rotation (State in, spell out; nb/rotation_base.py is the interface). Classes without one
                 get the base rotation: no instants, buffs from `buffs`, hp-only doomed()
    ranged       the ranged attack approach() stops in range of and the fight opens with (None: walk into melee)
    auto_ranged  the ranged attack repeats by itself once started (Auto Shot, wand Shoot): pressed only while off
    nuke         a cast spell fired again and again at range (Lightning Bolt, Fireball, Smite); None: the ranged auto
                 attack / the weapon carry the fight
    nuke_in_melee  keep casting the nuke point-blank (a caster's weapon is weak)
    heal         the self-heal used after fights / when hurt (None: food)
    cast_time    s per cast-time spell (the strip's casting flag is unreliable in combat)
    finishers    spells for a fleeing runner, the cheapest castable one is used
    snare        dropped / cast as we flee, to slow the chasers
    imbue        weapon buff kept up out of combat
    buffs        out-of-combat self-buffs, best first (BotEye aura bit 0 reads "one of the class buffs is up")
    self_spells  rotation spells that do not touch the mob (a cast of anything else counts as the pull)
    probe        spells that need us to face the mob, pressed by face_probe to test facing (none: the swing's
                 "facing the wrong way" errors are watched instead)
    pull_stand   yd a ranged opener stands off the mob (the ranged attack's reach minus a margin)
    mana         the power bar (the strip's "mana") is mana; False for rage / energy classes
    pet          the class keeps a pet: Bot.pet_step calls / revives / mends / feeds it, the fight sends it in
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nb import route


@dataclass(frozen=True)
class Profile:
    name: str
    spells: dict
    rotation: str = "nb.rotation_base:BaseRotation"
    ranged: str | None = None
    auto_ranged: bool = False
    nuke: str | None = None
    nuke_in_melee: bool = False
    heal: str | None = None
    cast_time: dict = field(default_factory=dict)
    finishers: tuple = ()
    snare: str | None = None
    imbue: str | None = None
    buffs: tuple = ()
    self_spells: frozenset = frozenset()
    probe: tuple = ()
    pull_stand: float = 28.0
    mana: bool = True              # the power bar is mana (rage / energy: no mana lines, no drinking)
    pet: bool = False              # the class keeps a pet (BotEye CLASS_CFG.pet: its call / revive / mend / feed keys)
    tame: str = ""                 # the spell that gets a pet from the world (bot.py tame_step; BotEye key N)

    def make_rotation(self, log=None):
        mod, cls = self.rotation.split(":")
        import importlib
        r = getattr(importlib.import_module(mod), cls)(log)
        if getattr(r, "profile", None) is None:
            r.profile = self
        return r

    @property
    def names(self) -> dict:
        """spell id -> name (Bot.SPELLS)."""
        return {i: n for i, (n, _c) in self.spells.items()}


def _shaman() -> Profile:
    from nb.shaman_rotation import SPELL_IDS
    return Profile(
        name="shaman", spells=dict(SPELL_IDS), rotation="nb.shaman_rotation:Rotation",
        ranged="Lightning Bolt", nuke="Lightning Bolt", nuke_in_melee=True, heal="Healing Wave",
        cast_time={"Lightning Bolt": 1.5, "Healing Wave": 2.0},
        finishers=("Earth Shock", "Lightning Bolt"), snare="Earthbind Totem", imbue="Rockbiter Weapon",
        buffs=("Lightning Shield",),
        self_spells=frozenset({"Healing Wave", "Lightning Shield", "Stoneclaw Totem", "Stoneskin Totem",
                               "Earthbind Totem", "Rockbiter Weapon"}),
        probe=("Lightning Bolt",), pull_stand=28.0)


def _hunter() -> Profile:
    from nb.hunter_rotation import SPELL_IDS
    return Profile(
        name="hunter", spells=dict(SPELL_IDS), rotation="nb.hunter_rotation:HunterRotation",
        ranged="Auto Shot", auto_ranged=True,
        finishers=("Arcane Shot",), snare="Concussive Shot",
        buffs=("Aspect of the Hawk", "Aspect of the Monkey"),
        self_spells=frozenset({"Aspect of the Hawk", "Aspect of the Monkey"}),
        probe=("Arcane Shot", "Serpent Sting"), pull_stand=33.0, pet=True, tame="Tame Beast")


# Classes without a rotation of their own yet: the spells the generic fight / rest code needs by role (ids from
# spell_template, ranks to ~14; costs 0 = unknown, so affordability is the client's call)
def _warrior() -> Profile:
    return Profile(
        name="warrior", spells={78: ("Heroic Strike", 0), 284: ("Heroic Strike", 0), 285: ("Heroic Strike", 0),
                                1608: ("Heroic Strike", 0), 6673: ("Battle Shout", 0), 5242: ("Battle Shout", 0),
                                100: ("Charge", 0), 6178: ("Charge", 0), 772: ("Rend", 0), 6546: ("Rend", 0)},
        buffs=("Battle Shout",), self_spells=frozenset({"Battle Shout"}), mana=False)


def _paladin() -> Profile:
    return Profile(
        name="paladin", spells={635: ("Holy Light", 0), 639: ("Holy Light", 0), 647: ("Holy Light", 0),
                                21084: ("Seal of Righteousness", 0), 20287: ("Seal of Righteousness", 0),
                                20288: ("Seal of Righteousness", 0), 20271: ("Judgement", 0),
                                465: ("Devotion Aura", 0), 10290: ("Devotion Aura", 0)},
        heal="Holy Light", cast_time={"Holy Light": 2.5},
        buffs=("Devotion Aura", "Seal of Righteousness"),
        self_spells=frozenset({"Holy Light", "Devotion Aura", "Seal of Righteousness"}))


def _rogue() -> Profile:
    return Profile(
        name="rogue", spells={1752: ("Sinister Strike", 0), 1757: ("Sinister Strike", 0),
                              1758: ("Sinister Strike", 0), 2098: ("Eviscerate", 0), 6760: ("Eviscerate", 0)},
        mana=False)


def _druid() -> Profile:
    return Profile(
        name="druid", spells={5176: ("Wrath", 0), 5177: ("Wrath", 0), 5178: ("Wrath", 0),
                              5185: ("Healing Touch", 0), 5186: ("Healing Touch", 0), 5187: ("Healing Touch", 0),
                              1126: ("Mark of the Wild", 0), 5232: ("Mark of the Wild", 0),
                              8921: ("Moonfire", 0), 8924: ("Moonfire", 0)},
        ranged="Wrath", nuke="Wrath", nuke_in_melee=True, heal="Healing Touch",
        cast_time={"Wrath": 1.5, "Healing Touch": 1.5}, finishers=("Moonfire", "Wrath"),
        buffs=("Mark of the Wild",), self_spells=frozenset({"Healing Touch", "Mark of the Wild"}),
        probe=("Wrath",), pull_stand=28.0)


def _mage() -> Profile:
    return Profile(
        name="mage", spells={133: ("Fireball", 0), 143: ("Fireball", 0), 145: ("Fireball", 0),
                             168: ("Frost Armor", 0), 7300: ("Frost Armor", 0)},
        ranged="Fireball", nuke="Fireball", nuke_in_melee=True, cast_time={"Fireball": 2.5},
        finishers=("Fireball",), buffs=("Frost Armor",), self_spells=frozenset({"Frost Armor"}),
        probe=("Fireball",), pull_stand=33.0)


def _priest() -> Profile:
    return Profile(
        name="priest", spells={585: ("Smite", 0), 591: ("Smite", 0), 598: ("Smite", 0),
                               2050: ("Lesser Heal", 0), 2052: ("Lesser Heal", 0), 2053: ("Lesser Heal", 0),
                               1243: ("Power Word: Fortitude", 0), 1244: ("Power Word: Fortitude", 0),
                               589: ("Shadow Word: Pain", 0), 594: ("Shadow Word: Pain", 0)},
        ranged="Smite", nuke="Smite", nuke_in_melee=True, heal="Lesser Heal",
        cast_time={"Smite": 2.0, "Lesser Heal": 2.0}, finishers=("Shadow Word: Pain", "Smite"),
        buffs=("Power Word: Fortitude",), self_spells=frozenset({"Lesser Heal", "Power Word: Fortitude"}),
        probe=("Smite",), pull_stand=28.0)


def _warlock() -> Profile:
    return Profile(
        name="warlock", spells={686: ("Shadow Bolt", 0), 695: ("Shadow Bolt", 0), 705: ("Shadow Bolt", 0),
                                687: ("Demon Skin", 0), 696: ("Demon Skin", 0),
                                348: ("Immolate", 0), 707: ("Immolate", 0)},
        ranged="Shadow Bolt", nuke="Shadow Bolt", nuke_in_melee=True, cast_time={"Shadow Bolt": 2.5},
        finishers=("Shadow Bolt",), buffs=("Demon Skin",), self_spells=frozenset({"Demon Skin"}),
        probe=("Shadow Bolt",), pull_stand=28.0, pet=True)


_PROFILES = {"shaman": _shaman, "hunter": _hunter, "warrior": _warrior, "paladin": _paladin, "rogue": _rogue,
             "druid": _druid, "mage": _mage, "priest": _priest, "warlock": _warlock}


def get(cls: str) -> Profile:
    try:
        return _PROFILES[cls]()
    except KeyError:
        raise ValueError(f"no class profile for {cls!r} (nb/classes.py has {sorted(_PROFILES)})")


def all_spell_names() -> dict:
    """spell id -> name over every profile (offline tools that read any class's bar)."""
    out: dict = {}
    for f in _PROFILES.values():
        out.update(f().names)
    return out


PROFILE = get(route.CLASS)       # the class this instance plays
