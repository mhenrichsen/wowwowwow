"""Priest: ranged caster that heals itself. Starter profile for levels 1-12 -
NOT yet tested live."""

from . import Ability, Buff, ClassProfile

PROFILE = ClassProfile(
    name="priest", class_id=5, power="mana", ranged=True, engage_range=26,
    trainer_class="priest",
    rotation=[
        Ability("Power Word: Shield", 5, target="self", cooldown=15,
                when={"my_hp_below": 70, "self_missing_aura": "Weakened Soul"}),
        Ability("Lesser Heal", 6, target="self", cast_time=2.6,
                when={"my_hp_below": 45}),
        Ability("Shadow Word: Pain", 4,
                when={"target_missing_aura": "Shadow Word: Pain", "target_hp_above": 30,
                      "target_within": 29}),
        Ability("Smite", 2, cast_time=2.6, when={"target_within": 29}),
    ],
    emergency=[Ability("Power Word: Shield", 5, target="self", cooldown=15,
                       when={"self_missing_aura": "Weakened Soul"})],
    buffs=[Buff("Power Word: Fortitude", 3), Buff("Inner Fire", 7)],
    drinks=True,
)
