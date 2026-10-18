"""Which RestedXP route this instance follows: the race / class it plays (login.RACECLASS, by inst) -> the
rxp.Who the guide parser filters steps with, and the walkable guide chain (rxp_policy reads it at import).

Dwarf: 01-05 Coldridge -> 05-11 Dun Morogh -> 12-14 Loch Modan -> 13-15 Westfall (the 11-12 Elwynn detour needs the
flight / tram). Human: 01-06 Northshire -> 06-11 Elwynn Forest -> 13-15 Westfall: the guide's 11-13 Loch Modan is
reached by the Deeprun Tram (map 369 has no mesh), Westfall is a walk from Elwynn."""
from __future__ import annotations

from nb import inst

from nb.login import RACECLASS
if inst.INST not in RACECLASS:
    raise SystemExit(f"nb: instance {inst.INST} has no race / class in login.RACECLASS: configure it there")
RACE, CLASS = RACECLASS[inst.INST][:2]

WHO = (CLASS.capitalize(), RACE.capitalize(), "Alliance")
RACE_MASK = {"human": 1, "orc": 2, "dwarf": 4, "nightelf": 8, "undead": 16, "tauren": 32, "gnome": 64,
             "troll": 128}[RACE]                              # quest_template AllowableRaces bits
CLASS_MASK = {"warrior": 1, "paladin": 2, "hunter": 4, "rogue": 8, "priest": 16, "shaman": 64, "mage": 128,
              "warlock": 256, "druid": 1024}[CLASS]           # ... AllowableClasses bits

if RACE == "human":
    GUIDE_1, GUIDE_2, GUIDE_3 = "01-06 Northshire", "06-11 Elwynn Forest", "13-15 Westfall"
    GUIDE_JUMP = {"11-13 Loch Modan": GUIDE_3}            # needs the tram
    JUMP_FLY = {}
    # the file names (rxp.get / guide_plan.GUIDES: "#name" as written)
    FILE_GUIDES = ("1-6 Northshire", "6-11 Elwynn Forest", "13-15 Westfall")
    LEVELS = (6, 11)                                         # pick_guide: under 6 -> 1, under 11 -> 2, else 3
    FACTION = 1                                              # the player's faction template (human)
    HOME, HOME_R = (-8913.0, -137.0), 380.0                  # Northshire Valley (the low-level grind area)
    # quest areas (quests.AREAS: spawns outside these are ignored): Northshire + Elwynn Forest, Westfall
    AREAS = [(-9400.0, -300.0, 1300.0), (-10600.0, 1200.0, 1100.0)]
else:
    GUIDE_1, GUIDE_2, GUIDE_3 = "01-05 Coldridge Valley", "05-11 Dun Morogh", "12-14 Loch Modan (Dwarf/Gnome)"
    GUIDE_JUMP = {"11-12 Elwynn (Dwarf/Gnome)": GUIDE_3}
    JUMP_FLY = {GUIDE_2: GUIDE_3}
    FILE_GUIDES = ("1-5 Coldridge Valley", "5-11 Dun Morogh", "12-14 Loch Modan (Dwarf/Gnome)")
    LEVELS = (5, 11)
    FACTION = 3                                              # dwarf
    HOME, HOME_R = (-6130.0, 450.0), 430.0                   # Coldridge Valley
    # Coldridge + Kharanos + east Dun Morogh, the North Gate pass, Loch Modan
    AREAS = [(-6000.0, 0.0, 1400.0), (-5650.0, -1800.0, 500.0), (-5400.0, -3150.0, 1300.0)]


def who():
    import rxp
    return rxp.Who(*WHO)
