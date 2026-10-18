"""Mage: ranged caster. Tested live from level 1 to 5."""

from . import Ability, Buff, ClassProfile

FROST_NOVA = Ability("Frost Nova", 7, cooldown=25, then="step_back",
                     when={"enemy_in_melee": True, "power_at_least": 55,    # mana points
                           "no_sheep_near": True})

PROFILE = ClassProfile(
    name="mage", class_id=8, power="mana", ranged=True, engage_range=29,
    # 29, not 26: Frostbolt reaches 29 (the rotation already says
    # target_within 29) and every yard of opening distance is time the
    # mob spends running instead of hitting us. The user watched it
    # walk well inside its own range before opening.
    trainer_class="mage",
    # cast times are the real ones (Fireball rank 3 is 2.5 s): checked at 1.6 s,
    # a late mana tick passed as "cast done" and the next key pressed mid-cast failed
    rotation=[
        # a mob in melee range: root it and walk away
        FROST_NOVA,
        # (a Frostbolt-and-walk kite when the root is down was tried and
        # dropped: the user judged it too inefficient - nova + move only)
        # first cast on a rooted mob: the root breaks on the first Fireball
        # hit ("control ended after 4s" every fight) - a Frostbolt's slow
        # outlives the root, so the mob crawls back instead of sprinting
        Ability("Frostbolt", 4, cast_time=2.2,
                when={"target_within": 29, "target_has_aura": "Frost Nova",
                      "target_missing_aura": "Frostbolt"}),
        # opener from range: the 40% slow buys two more casts before melee
        Ability("Frostbolt", 4, cast_time=2.2,
                when={"target_within": 29, "target_beyond": 14, "target_hp_above": 95}),
        # Fireball rank 3 (level 12): 53-73 +8 burn for 65 mana against
        # Frostbolt rank 2's 31 for 35 - twice the damage per cast and more
        # per mana, so a goretusk dies in 4 casts instead of 7 (10-19 s of
        # melee per fight came from those extra casts). Frostbolt rank 3
        # (level 14) matches it and slows, so it takes over again.
        Ability("Fireball", 2, cast_time=2.5, when={"target_within": 34, "level_below": 14}),
        Ability("Frostbolt", 4, cast_time=2.2, when={"target_within": 29}),
        Ability("Fireball", 2, cast_time=2.5, when={"target_within": 34}),
    ],
    crowd_control=Ability("Polymorph", 1, cast_time=1.6, target="extra",
                          when={"power_at_least": 60}),
    emergency=[FROST_NOVA],
    can_escape=True,
    buffs=[Buff("Frost Armor", 3), Buff("Arcane Intellect", 8)],
    drinks=True,
    conjure_water=Ability("Conjure Water", 9, cast_time=3.6),
    conjure_food=Ability("Conjure Food", 10, cast_time=3.6),
)
