"""Class trainers the nb bot walks to. `npc` is the id the client puts in the
creature GUID (what the nameplate strip reports); `db_entry` is the id in the
tc_world creature_template, which differs on this server."""

from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
from nb import inst
STATE = inst.path("trained.json")

TRAINERS = {             # (the first trainer of a class: the old guide driver's 'trainer' step)
    "shaman": {
        "name": "Teo Hammerstorm", "title": "Shaman Trainer",
        "npc": 257446, "db_entry": 37115, "map": 0, "max_level": 6,
        "pos": (-6050.29, 383.95, 398.96),     # Anvilmar, top level (tcf_world spawn)
    },
}
# Every shaman trainer on this server (tcf_world creature_trainer): Teo's list (trainer 11) stops at level 6,
# Ingrid Dunwald in Kharanos (trainer 10, upstairs in Thunderbrew Distillery) teaches everything up to 60.
# The guide's Ironforge trainer (Eldrun Stormbreaker 258098) has no spawn here.
SHAMAN_TRAINERS = [
    TRAINERS["shaman"],
    {"name": "Ingrid Dunwald", "title": "Shaman Trainer", "npc": 258113, "map": 0, "max_level": 60,
     "pos": (-5582.9, -541.5, 403.6)},
    # Ironforge's (polestar ClassTrainers.lua lists him; position from tcf_world creature 9909451)
    {"name": "Eldrun Stormbreaker", "title": "Shaman Trainer", "npc": 258098, "map": 0, "max_level": 60,
     "pos": (-4642.4, -1086.5, 500.8)},
]


# Hunter trainers (tcf_world creature_trainer -> trainer_spell 1005515 etc., levels 1-60 each). The human guide's
# Tordrin Sternblade (248415, Northshire) and Josephine Carson (Goldshire) have no spawn on this server: Stormwind's
# Dwarven District trio, Kharanos (Grif Wildheart) and Ironforge (Hall of Arms) are the real ones.
HUNTER_TRAINERS = [
    # the guide's Northshire trainer: added to tcf_world 2026-10-17 (nb/sql_tordrin.sql), trainer 1005515's list
    {"name": "Tordrin Sternblade", "title": "Hunter Trainer", "npc": 248415, "map": 0, "max_level": 60,
     "pos": (-8884.2, -242.1, 80.9)},
    {"name": "Einris Brightspear", "title": "Hunter Trainer", "npc": 5515, "map": 0, "max_level": 60,
     "pos": (-8415.8, 552.7, 95.5)},
    {"name": "Ulfir Ironbeard", "title": "Hunter Trainer", "npc": 5516, "map": 0, "max_level": 60,
     "pos": (-8410.3, 548.6, 95.5)},
    {"name": "Grif Wildheart", "title": "Hunter Trainer", "npc": 1231, "map": 0, "max_level": 60,
     "pos": (-5618.5, -454.1, 407.8)},
    {"name": "Daera Brightspear", "title": "Hunter Trainer", "npc": 5115, "map": 0, "max_level": 60,
     "pos": (-5043.0, -1274.7, 510.4)},
]
from nb import route   # noqa: E402
CLASS = route.CLASS      # the configured class (login.RACECLASS for this instance)
CLASS_TRAINERS = {"shaman": SHAMAN_TRAINERS, "hunter": HUNTER_TRAINERS}
# levels a class's trainer adds spells at (tcf_world trainer_spell); a class not listed: every even level
EVEN_LEVELS = tuple(range(2, 61, 2))
CLASS_LEVELS = {"shaman": (2, 6, 8, 10, 12, 14, 16, 18, 20),
                # hunter: trainer_spell 1005515 adds spells at 4, 6, 8, ... (level 1 Track Beasts is known at start)
                "hunter": (4, 6, 8, 10, 12, 14, 16, 18, 20)}
# (db, sql) -> rows of (level, spell, copper): the class trainer's price list (min_cost); none: 0 (unknown)
COST_SQL = {"shaman": ("mangos", "select reqlevel, spell, min(spellcost) from npc_trainer_template where entry=10 "
                                   "group by reqlevel, spell"),
            "hunter": ("tcf_world", "select ReqLevel, SpellId, MoneyCost from trainer_spell where TrainerId=1005515")}


def set_class(cls: str) -> None:
    """Which class's trainers, training levels and cost table (the default: the configured class)."""
    global CLASS, TRAIN_LEVELS, _costs
    if cls != CLASS:
        CLASS, TRAIN_LEVELS, _costs = cls, CLASS_LEVELS.get(cls, EVEN_LEVELS), None


def class_trainers() -> list:
    """The class's trainers (empty: none known - the RXP .trainer steps name them)."""
    return CLASS_TRAINERS.get(CLASS, [])


def trainer_for(level: int, pos) -> dict:
    """The nearest class trainer that teaches spells of this level."""
    import math
    lst = class_trainers()
    if not lst:
        return None
    ok = [t for t in lst if t["max_level"] >= due_level(level)] or lst[-1:]
    return min(ok, key=lambda t: math.hypot(t["pos"][0] - pos[0], t["pos"][1] - pos[1]))

TRAIN_EVERY = 2          # levels


# levels at which the class's trainers add spells (CLASS_LEVELS). Level 1-2 spells (ReqLevel 0) are bought on the
# first visit
TRAIN_LEVELS = CLASS_LEVELS.get(CLASS, EVEN_LEVELS)


def due_level(level: int) -> int:
    """The level a training trip is due for, 0 when none yet. No trip at levels where nothing new is taught
    (2026-10-14: the bot walked to Teo at level 4 for a list that only has level 0 and 6 spells)."""
    return max((lv for lv in TRAIN_LEVELS if lv <= level), default=0)


def trained_level() -> int:
    try:
        return int(json.load(open(STATE))["level"])
    except Exception:
        return 0


def mark_trained(level: int) -> None:
    json.dump({"level": due_level(level)}, open(STATE, "w"))


def is_due(level: int) -> bool:
    return due_level(level) > trained_level()


# Gray-item vendor next to the trainer (Durnan Furcutter, tc_world spawn 305729); the addon sells
# every gray item on MERCHANT_SHOW.
VENDOR = {"name": "Durnan Furcutter", "npc": 836, "map": 0, "pos": (-6056.38, 385.21, 392.85)}

# Repair + general vendors, nearest one is used. Coldridge has none; Kharanos and Anvilmar do.
VENDORS = [
    VENDOR,
    {"name": "Thrawn Boltar", "npc": 1690, "map": 0, "pos": (-5568.99, -429.11, 397.41)},     # Kharanos
    {"name": "Morhan Coppertongue", "npc": 167, "map": 0, "pos": (-5343.7, -2932.1, 324.4)},  # Thelsamar, repairs
]


_costs: list | None = None


def min_cost(have: int, due: int) -> int:
    """Cheapest class spell (copper) that becomes learnable between trained level `have` and `due`, from the class's
    trainer price list (COST_SQL). 0 when the table has nothing for those levels or the class has no list."""
    global _costs
    try:
        if _costs is None:
            import worlddb
            if CLASS not in COST_SQL:
                _costs = []
                return 0
            db, sql = COST_SQL[CLASS]
            rows = worlddb._query(sql) if db == "mangos" else worlddb._query_db(db, sql)
            _costs = [(int(r[0]), int(r[1]), int(r[2])) for r in rows]
        c = [cost for lv, _sp, cost in _costs if have < lv <= due and cost > 0]
        return min(c) if c else 0
    except Exception:
        _costs = []                                 # DB down: do not re-spawn mariadb.exe on every loop pass
        return 0
