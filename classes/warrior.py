"""Warrior: melee, rage. Tested live from level 1 in Coldridge Valley."""

from . import Ability, ClassProfile

# Centre-to-centre yards at which a melee ability still lands. It was 5, but
# big mobs (Wendigos, bears, boars) are swung at from 5.0-5.5 yd - white hits
# land, and every ability failed the range check: 2-3 casts in a 30 s fight,
# none at all with two Wendigos on us at 54% rage (death at 11:01)
MELEE = 6.0

PROFILE = ClassProfile(
    name="warrior", class_id=1, power="rage", ranged=False, engage_range=3.5,
    trainer_class="warrior", melee_auto_attack=True,
    # Charge cannot sit in the rotation: it only works out of combat and
    # only from 8-25 yd, and by the time the rotation runs the engine has
    # already walked us into melee. It closes the gap itself and hands us
    # the rage to open with, which a warrior otherwise starts a fight
    # without.
    # Charge and Heroic Strike are off the global cooldown in 1.12: the
    # engine slept a full GCD after each (Charge: 1.2 s standing in melee
    # before the first swing; Heroic Strike: one ability every ~6 s over a
    # 1500 s sample). Heroic Strike also takes its rage at the next white
    # hit, so "did the power drop" is not how to tell that it went off.
    opener=Ability("Charge", 7, when={"target_beyond": 8, "target_within": 25}, gcd=False),
    rotation=[
        Ability("Execute", 5, when={"target_hp_below": 20, "power_at_least": 15}),
        Ability("Battle Shout", 4, target="self",
                when={"self_missing_aura": "Battle Shout", "power_at_least": 10}),
        Ability("Thunder Clap", 6, cooldown=4,
                when={"enemies_at_least": 2, "power_at_least": 20, "target_within": MELEE}),
        Ability("Rend", 3, when={"target_missing_aura": "Rend", "target_hp_above": 40,
                                 "power_at_least": 10, "target_within": MELEE}),
        Ability("Heroic Strike", 2, when={"power_at_least": 15, "target_within": MELEE},
                gcd=False, queued=True),
    ],
)
