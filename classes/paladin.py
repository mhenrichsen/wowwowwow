"""Paladin: melee, mana. Starter profile for levels 1-20 - NOT yet tested live.

Everything below is taken from trainer template 29 on this server (the full
paladin trainer; template 28 is the 6-spell starter stub), so the levels are
the server's own, not remembered ones:

    lvl  1  Devotion Aura          lvl  8  Hammer of Justice, Purify, Parry
    lvl  4  Judgement, Bless. Might    lvl 10  Lay on Hands, Seal of Righteousness r2
    lvl  6  Holy Light, Divine Protection, Seal of the Crusader

The trainer's entries are the SECOND rank of each spell; rank 1 of Seal of
Righteousness and of Holy Light is granted at character creation. Verified
live: a fresh level-1 dwarf paladin's bar reads
1=Attack, 2=Seal of Righteousness, 3=Holy Light, 4=Stoneform, 5=Find Treasure.
So a new paladin has exactly one damage tool - a seal that procs off white
hits - and no attack ability at all until Judgement at level 4.

SLOTS ARE NOT FREE CHOICES
--------------------------
MageBot.has() tests "is a rank of this spell in the slot THIS PROFILE names",
so a slot here that disagrees with where the client already put the spell is
never satisfied: fix_action_bar logs "action bar missing Holy Light" and
re-places it every 60 s for the whole run. Observed live before this was
aligned. Seal of Righteousness and Holy Light therefore keep the slots the
client gives them (2 and 3); everything the bot places itself goes at 5 and
above. Slot 4 is left to the Stoneform racial, which the bot does not use but
which costs nothing to leave alone.

WHY THE ROTATION IS SHAPED THIS WAY
-----------------------------------
A levelling paladin's damage is its white swings plus the Seal of
Righteousness proc on each of them. The seal is therefore the rotation's
first duty and everything else is opportunistic: a seal that has fallen off
is a flat ~30% damage loss for as long as it is down.

Judgement consumes the seal. That is the one piece of paladin logic the
engine cannot infer, and getting it wrong is worse than not judging at all:
judging and then standing there unseals the character for the rest of the
fight. So Judgement is gated on having enough mana left to put the seal
straight back on (SEAL_MANA), and the seal line sits ABOVE it in the
rotation so the very next tick re-seals.

Mana is the real constraint at these levels, not rage or cooldowns. A
paladin that judges every cooldown drinks after every second fight, which
costs more wall-clock than the judgement damage saves - hence the
power_pct_at_least gate rather than a bare power_at_least.
"""

from . import Ability, Buff, ClassProfile

# Same number and the same reason as classes/warrior.py: big mobs are swung
# at from 5.0-5.5 yd centre-to-centre, and a 5.0 range check failed every
# ability against them while the white hits landed.
MELEE = 6.0

# Rank 1 Seal of Righteousness costs 20 mana; leave at least that much behind
# when judging so the seal can go straight back up. Judgement itself is a
# percentage of base mana, which is why this is a floor and not a subtraction.
SEAL_MANA = 20

PROFILE = ClassProfile(
    name="paladin", class_id=2, power="mana", ranged=False, engage_range=3.5,
    trainer_class="paladin", melee_auto_attack=True, drinks=True,
    # Run from a fight we are losing. Left at the default False, the engine
    # never considers it, and the third death on this character was exactly
    # that: a 45 s 3v1 fought down to 6% hp - the heals worked, 17% -> 53% -
    # then the engine chained straight into the next wolf and died three
    # seconds later with no disengage available. combat.escape() only needs
    # to break 50 yd of threat radius for the mobs to evade, and a paladin
    # can buy that with Hammer of Justice or Divine Protection, which is what
    # can_escape assumes is available.
    can_escape=True,
    # A paladin's crowd control is Hammer of Justice. Leaving this unset told
    # the engine the class had none, so every multi-mob break-off logged
    # "no root for another cooldown - running without one" and ran with three
    # troggs hitting us in the back: 87% hp to dead in 13 s at 16:15, and the
    # same shape at 15:58. A 6 s stun is shorter than a Polymorph but it is
    # exactly what escape() needs - the gap that turns a 60 yd run into a
    # leash instead of a corpse. It costs nothing to name it: the engine only
    # reaches for cc when it is off cooldown and something is actually on us,
    # and the rotation entry below still spends it on fights going badly.
    crowd_control=Ability("Hammer of Justice", 6, cooldown=60, target="extra",
                          when={"target_within": 10}),
    rotation=[
        # The seal first: a dropped seal is the single biggest dps loss
        # available to a levelling paladin, and it lasts only 30 s.
        Ability("Seal of Righteousness", 2, target="self",
                when={"self_missing_aura": "Seal of Righteousness",
                      "power_at_least": SEAL_MANA}),
        # Judgement consumes the seal, so only when we can afford to re-seal
        # immediately afterwards. 10 s cooldown, 10 yd range.
        Ability("Judgement", 5, cooldown=10,
                when={"self_has_aura": "Seal of Righteousness",
                      "target_within": 10, "power_at_least": SEAL_MANA * 2,
                      "power_pct_at_least": 35}),
        # Stun a mob that is actually hurting us. 60 s cooldown, so it is
        # spent on the fights that are going badly rather than on cooldown.
        Ability("Hammer of Justice", 6, cooldown=60,
                when={"my_hp_below": 55, "target_within": MELEE,
                      "power_pct_at_least": 20}),
        # Heal mid-fight rather than dying and walking back. 2.5 s cast and
        # it will take pushback; still cheaper than a corpse run.
        #
        # TWO thresholds, because the right one depends on how many things are
        # hitting us. Solo, 40% is plenty of runway for a 2.5 s cast. Against a
        # pack it is not: measured in both deaths on this character, hp fell
        # 37% -> 16% in nine seconds at 3v1, so a heal started at 40% is still
        # casting when we die and a heal started at 16% never lands at all.
        # Starting at 65% with two or more attackers is the same heal, begun
        # early enough to finish. It costs mana only in the fights that are
        # already going wrong, because enemies_at_least counts attackers that
        # are not crowd-controlled.
        Ability("Holy Light", 3, target="self", cast_time=2.5,
                when={"my_hp_below": 65, "enemies_at_least": 2,
                      "power_pct_at_least": 25}),
        Ability("Holy Light", 3, target="self", cast_time=2.5,
                when={"my_hp_below": 40, "power_pct_at_least": 30}),
    ],
    emergency=[
        # ORDER MATTERS: pick() takes the first entry whose conditions hold,
        # and the ability it returns is cast a beat later, by which time the
        # health that chose it has moved. With the bubble at 45 and the heal
        # at 50 there was a five-point window where the engine chose a 2.5 s
        # Holy Light while two Harvest Golems were in melee - it took pushback
        # on every hit, logged "emergency: Holy Light (did not go off)" at
        # 29% hp, and that was the death at 17:56. The bubble is instant, so
        # raising it above the heal means it fires FIRST in any fight where
        # both apply, and the heal then lands behind it. Divine Protection
        # was off cooldown the whole time; it was simply never chosen.
        #
        # ...and then the SECOND instant, before the cast. Same ordering trap
        # as the bubble, one rung lower: with Lay on Hands last, Holy Light's
        # own condition (50% hp, 15% mana) still held at 14% hp, so pick()
        # never reached the instant full heal and chose a 2.5 s cast that
        # could not land in time - death at 18:25, "emergency: Holy Light" at
        # 14% and a corpse three seconds later. Instants before casts,
        # ordered by how desperate they are.
        #
        # Bubble first - it is instant and buys the time the heal needs.
        # Raised from 25% for the same reason as the rotation heal above: at
        # 3v1 the character crossed 37% -> dead in fourteen seconds, so a
        # bubble armed at 25% is spent with nothing left to heal behind it.
        Ability("Divine Protection", 7, target="self", cooldown=300,
                when={"my_hp_below": 55}),
        # The last resort, and a long enough cooldown that it is worth
        # spending only when the alternative is dying - which is why it moved
        # from 12% to 22%. At 12% a 2.5 s Holy Light that keeps getting pushed
        # back never gets us there alive, and an hour-long cooldown is cheaper
        # than a corpse run.
        Ability("Lay on Hands", 8, target="self", cooldown=3600,
                when={"my_hp_below": 22}),
        Ability("Holy Light", 3, target="self", cast_time=2.5,
                when={"my_hp_below": 50, "power_pct_at_least": 15}),
    ],
    # Devotion Aura is a permanent toggle (level 1, one cast, never expires
    # until another aura replaces it), and Blessing of Might lasts 5 min.
    # Both belong out of combat, which is exactly what buffs are for.
    # Blessing of Might is 5 min. Recast only once it had fallen off, it was
    # up about three quarters of the time: casts ran 5-8 min apart, and the
    # gap was spent fighting. duration lets the bot refresh it early.
    buffs=[Buff("Devotion Aura", 9), Buff("Blessing of Might", 10, duration=300)],
    # Heal after every fight rather than eat, and pull again with 20% mana:
    # the paladin bought 5 bread every ~5 minutes at 125c a stack while its
    # mana refilled for nothing between fights (user, 2026-09-24).
    rest_heal=Ability("Holy Light", 3, target="self", cast_time=2.5),
    pull_mana=20.0,
)
