"""Shaman (Dwarf Shaman on the local server): melee with mana, levels 1-10 - NOT yet tested live.

Spells and levels are this server's own, from trainer template 10 (Grundar
Stormcaller, 18200, the trainer tools/dwarf_shaman.py added in Coldridge):

    lvl  1  Rockbiter Weapon                lvl  8  Lightning Shield, Stoneclaw Totem,
    lvl  4  Earth Shock                             Earth Shock r2, Lightning Bolt r2,
    lvl  6  Healing Wave r2, Earthbind Totem        Rockbiter Weapon r2
                                            lvl 10  Flame Shock, Flametongue Weapon,
                                                    Strength of Earth Totem

A new character's bar (playercreateinfo_action for race 3 / class 7):
1=Attack, 2=Lightning Bolt, 3=Healing Wave, 4=Stoneform. Those keep the slots
the client gives them (a profile slot that disagrees is re-placed every 60 s,
see classes/paladin.py); everything the bot places itself goes at 5 and up.

Shape: open with a Lightning Bolt at range, then melee with Rockbiter on the
weapon and Earth Shock on cooldown. Heal with Healing Wave, in the fight when
it goes badly and after it instead of eating (user: self-healing classes heal,
drink only under 20% mana).
"""

from . import Ability, Buff, ClassProfile

# Same reach as the other melee profiles: big mobs are swung at from 5-5.5 yd.
MELEE = 6.0

PROFILE = ClassProfile(
    name="shaman", class_id=7, power="mana", ranged=False, engage_range=3.5,
    trainer_class="shaman", melee_auto_attack=True, drinks=True,
    # One Lightning Bolt before closing: free damage while the mob walks in.
    opener=Ability("Lightning Bolt", 2, cast_time=1.5,
                   when={"target_beyond": 10, "power_pct_at_least": 40}),
    rotation=[
        # 6 s cooldown instant; the shaman's main attack below level 10.
        Ability("Earth Shock", 5, cooldown=6,
                when={"target_within": 20, "power_pct_at_least": 30}),
        # Flame Shock at 10: a DoT, only on a mob that will live through it.
        Ability("Flame Shock", 6, cooldown=6,
                when={"target_within": 20, "target_hp_above": 50,
                      "target_missing_aura": "Flame Shock",
                      "power_pct_at_least": 45}),
        # Heal in the fight before it is too late: earlier with adds on us
        # (same two-threshold reasoning as the paladin's Holy Light).
        Ability("Healing Wave", 3, target="self", cast_time=2.5,
                when={"my_hp_below": 60, "enemies_at_least": 2,
                      "power_pct_at_least": 20}),
        Ability("Healing Wave", 3, target="self", cast_time=2.5,
                when={"my_hp_below": 40, "power_pct_at_least": 20}),
    ],
    emergency=[
        # Stoneform (dwarf racial, instant): armor + removes poison/bleed.
        Ability("Stoneform", 4, target="self", cooldown=180, gcd=False,
                when={"my_hp_below": 45}),
        Ability("Healing Wave", 3, target="self", cast_time=2.5,
                when={"my_hp_below": 45, "power_pct_at_least": 10}),
    ],
    buffs=[
        # Weapon enchant: 5 min, no aura to check.
        Buff("Rockbiter Weapon", 7, duration=300, weapon=True),
        Buff("Lightning Shield", 8, duration=600),
    ],
    rest_heal=Ability("Healing Wave", 3, target="self", cast_time=2.5),
    pull_mana=20.0,
)
