"""Gear upgrades: score items from the world DB for the character's class and
equip anything in the backpack that beats what is worn.

Perception is memory (equipped and bag item GUIDs from the player's
descriptors, item entries from the object manager). Item stats come from
item_template. Proficiency comes from the skill lines (Cloth, Staves, ...), so
the same code works for every class; only the stat weights differ.

Scores are in stat points. Raw numbers in item_template grow at very different
rates over a 1-60 run - weapon dps goes ~0.75 -> 40, armour on a body slot
~3 -> 139, a primary stat ~0.3 -> 26 - so any fixed exchange rate between them
(the old "dps is worth 3.0 str, armour 0.05 str") is only true at the one level
it was written for. Every non-stat term is therefore divided by what
item_template pays for that quantity at the item level THIS CHARACTER is
entitled to wear (ILVL_UNITS, fitted offline below) and multiplied back by the
stat unit there, which leaves the weights dimensionless - "one typical item's
worth of armour is worth 0.69 of one typical item's worth of stats, to a
warrior" - and true at level 5 and at level 60. The exchange rate belongs to
the character, not to the item, so every item in one decision is measured with
the same ruler and only the character's level moves it.

    python gear.py        # show what is worn, what is in the bag, and upgrades
"""

from __future__ import annotations

import struct

import offsets as O
import worlddb

CLIENT_BUILD = 5875          # 1.12.1, the only build this bot talks to

# UpdateFields_1_12_1.h: PLAYER_FIELD_INV_SLOT_HEAD = UNIT_END + 0x12A, 23 guids
# (19 equipment slots + 4 bag slots).
INV_SLOT_HEAD = (0x6 + 0xB6 + 0x12A) * 4
EQUIP_SLOTS = 19

# InventoryType -> equipment slot(s). 13 (one-hand) is resolved by slots_for(),
# which is the only place allowed to decide whether the off-hand is available.
SLOTS = {1: [0], 2: [1], 3: [2], 4: [3], 5: [4], 20: [4], 6: [5], 7: [6], 8: [7],
         9: [8], 10: [9], 11: [10, 11], 12: [12, 13], 16: [14], 13: [15], 17: [15],
         21: [15], 22: [16], 14: [16], 23: [16], 15: [17], 26: [17], 28: [17],
         24: [19]}
TWO_HAND = 17
MAIN_HAND, OFF_HAND, RANGED = 15, 16, 17
# 1.12 has 19 equipment slots (0..18); ammo is not one of them - the client
# keeps it in a bag and remembers the entry in PLAYER_AMMO_ID. Ammo still needs
# a slot key so slots_for() can answer for inventory_type 24 without a special
# case at every call site; 19 is past the end of the worn array, which every
# caller reads through _worn_score(), so it scores as permanently empty.
AMMO_SLOT = 19

# item class 2 (weapon) subclass -> skill line (SkillLine.dbc); class 4 (armor) likewise
WEAPON_SKILL = {0: 44, 1: 172, 2: 45, 3: 46, 4: 54, 5: 160, 6: 229, 7: 43, 8: 55,
                10: 136, 13: 473, 15: 173, 16: 176, 18: 226, 19: 228}
ARMOR_SKILL = {1: 415, 2: 414, 3: 413, 4: 293, 6: 433}      # 0 misc: anyone
RANGED_SUBCLASS = (2, 3, 16, 18, 19)        # bow, gun, thrown, crossbow, wand
DUAL_WIELD = 118            # SkillLine.dbc: the skill warriors/rogues/hunters
                            # train at 20 and nobody else ever has
SHIELD_SKILL = 433

# UpdateFields_1_12_1.h: PLAYER_SKILL_INFO_1_1 = UNIT_END + 0x212, 128 x (id, value, bonus).
# The spellbook in memory only lists visible spells - passive proficiencies
# (Cloth, Staves, ...) are missing there, but always have a skill line.
SKILL_INFO = (0x6 + 0xB6 + 0x212) * 4


def known_skills(g) -> set[int]:
    d = g.p.u32(g.player().base + O.OBJ_DESCRIPTORS)
    raw = g.p.read(d + SKILL_INFO, 128 * 12)
    return {struct.unpack_from("<I", raw, i * 12)[0] & 0xFFFF for i in range(128)} - {0}

# stat type -> name (item_template stat_typeN)
STAT = {3: "agi", 4: "str", 5: "int", 6: "spi", 7: "sta"}
RES = ("holy_res", "fire_res", "nature_res", "frost_res", "shadow_res", "arcane_res")

# --- what an item level is worth -------------------------------------------
# Fitted once, offline, from item_template: the median stat total, weapon dps
# and body-armour value of every de-duplicated class 2/4 row in each item level
# bucket, made monotone (see _backups and the fit script in the scratchpad).
# Shields and off-hand holdables are left out of the armour median because a
# shield carries ~11x a body slot's armour and would drag the curve; they are
# scored against this same curve, which is what makes a shield's armour show up
# as the large number it really is.
ILVL_UNITS = {
    1:  (0.26,  0.75,   3.0),
    5:  (1.07,  2.00,  19.0),
    10: (1.88,  4.50,  26.0),
    15: (3.00,  7.65,  39.0),
    20: (5.00, 11.16,  45.0),
    25: (8.00, 15.27,  52.5),
    30: (11.0, 18.93,  54.0),
    35: (13.0, 22.21,  62.5),
    40: (15.0, 28.70,  77.0),
    45: (18.0, 30.00, 103.0),
    50: (20.0, 34.50, 111.0),
    55: (23.0, 36.52, 129.5),
    60: (26.0, 40.31, 139.0),
    65: (31.0, 47.97, 154.0),
    70: (33.0, 51.44, 193.0),
    75: (48.0, 58.46, 203.0),
    80: (48.96, 59.83, 207.1),
}
_ILVL_KEYS = sorted(ILVL_UNITS)
# Each row is the median over the five item levels centred on it, not the five
# above it, or the curve would lag the real jumps (armour steps up hard at 40)
# and item level 39 would out-score item level 41. Below item level 15 barely
# any item carries a stat at all - the median there is 1-3 points over a
# handful of rows - so those buckets are scaled down from the dense dps and
# armour curves instead of measured directly. Where two neighbouring medians
# tied (armour 30 vs 35, the top two buckets) the upper one is nudged 2% up, so
# units() is strictly increasing and two different item levels never tie.


def units(item_level: int) -> tuple[float, float, float]:
    """(stat unit, weapon dps unit, armour unit) at this item level.

    Linear between the fitted buckets, flat outside them. All three are > 0 and
    strictly increasing in item level, which is what makes a heavier item at a
    fixed character level score higher rather than tie.
    """
    il = max(_ILVL_KEYS[0], min(int(item_level or 1), _ILVL_KEYS[-1]))
    lo = _ILVL_KEYS[0]
    for k in _ILVL_KEYS:
        if k <= il:
            lo = k
    if lo == _ILVL_KEYS[-1]:
        return ILVL_UNITS[lo]
    hi = _ILVL_KEYS[_ILVL_KEYS.index(lo) + 1]
    f = (il - lo) / (hi - lo)
    a, b = ILVL_UNITS[lo], ILVL_UNITS[hi]
    return tuple(x + (y - x) * f for x, y in zip(a, b))       # type: ignore[return-value]


def expected_item_level(level: int) -> int:
    """The item level a character of this level is entitled to wear.

    item_template is built on one rule and keeps it for the whole run: an
    item's item_level is its required_level + 5. Measured over every white/
    green/blue class 2/4 row, the median item_level of required_level r is
    exactly r + 5 for every r from 2 to 59. Level 1 is the exception - every
    item from item level 1 to 6 asks for required_level 1 - so the bottom of
    the curve is flat.
    """
    lv = max(1, min(int(level), 60))
    return 3 if lv == 1 else lv + 5


# --- equip effects ----------------------------------------------------------
# item_template names up to five spells per item; spelltrigger 1 means "while
# equipped". The spell's aura tells us what the tooltip line is. A +12 spell
# damage staff and the same staff without it used to score identically, which
# stops mattering at level 1 and starts mattering around 20, when quest rewards
# begin carrying these.
#   13 mod damage done (miscValue = school mask)   135 mod healing done
#   99 melee attack power   124 ranged attack power (the same tooltip line)
#   52 melee crit %   71 spell crit %   54 melee hit %   55 spell hit %
#   85 power regen (mana per 5s)   49 dodge %   47 parry %   30 mod skill
# Auras outside this table are on-use procs, utility (fishing, stealth
# detection) or flat resistances: deliberately unscored, because nothing in
# the scorer can put a number on "Argent Dawn Commission".
AURA_KEY = {135: "heal", 99: "attack_power", 124: "attack_power", 52: "crit",
            71: "spell_crit", 54: "hit", 55: "spell_hit", 85: "mp5",
            49: "dodge", 47: "dodge"}
MAGIC_SCHOOLS = 126         # holy|fire|nature|frost|shadow|arcane mask
DEFENSE_SKILL = 95

# Point-valued effects, in units of "one item's stat budget": fitted the same
# way as ILVL_UNITS, as median effect value / median stat total in the same
# bucket, and flat across item levels (spell damage 0.59-0.88, AP 0.65-1.33),
# which is why one number per effect is enough.
EFFECT_UNIT = {"spell_dmg": 0.67, "heal": 0.73, "attack_power": 0.88,
               "mp5": 0.17, "defense": 0.16, "weapon_skill": 0.16}
# Percentage effects are already dimensionless; their unit is one percentage
# point at any item level. They are multiplied by the stat unit instead, so
# 1% crit is worth more stat points at 60 than at 20 - which is the point of
# a percentage.
PERCENT_EFFECT = ("crit", "spell_crit", "hit", "spell_hit", "dodge")

EFFECT_KEYS = tuple(EFFECT_UNIT) + PERCENT_EFFECT

# --- weights ----------------------------------------------------------------
# Every key, for every class id that exists in 1.12. Stat weights are per point
# (1.0 = the class's best stat). Every other weight is dimensionless: "one
# typical item's worth of this is worth N typical items' worth of stats".
#
# The dps and armour weights are calibrated so that at item level 10 - the band
# this bot has actually played - they reproduce the old per-point constants
# (warrior dps 3.0, armour 0.05, caster dps 0.3, armour 0.02). Above that the
# curve, not the constant, decides: at item level 60 the same warrior weight
# pays 4.63 stat points per dps and 0.13 per armour, because that is what dps
# and armour have grown to by then - a number typed in at level 7 cannot know.
WEIGHT_KEYS = ("str", "agi", "int", "spi", "sta", "armor", "dps") + EFFECT_KEYS + RES

WEIGHTS = {
    # warrior: two-hander until it learns to dual wield, so the weapon is the
    # character. 1 AP = 0.5 str in 1.12, which is where attack_power comes from.
    1: {"str": 1.0, "agi": 0.7, "int": 0.0, "spi": 0.1, "sta": 0.9,
        "armor": 0.69, "dps": 7.18, "spell_dmg": 0.0, "heal": 0.0,
        "attack_power": 0.44, "mp5": 0.0, "defense": 0.10, "weapon_skill": 0.30,
        "crit": 0.50, "spell_crit": 0.0, "hit": 0.60, "spell_hit": 0.0, "dodge": 0.20},
    # paladin: levels as retribution, so a strength/weapon profile with enough
    # int and spirit to keep healing itself between pulls.
    2: {"str": 1.0, "agi": 0.45, "int": 0.35, "spi": 0.25, "sta": 0.85,
        "armor": 0.62, "dps": 7.18, "spell_dmg": 0.15, "heal": 0.15,
        "attack_power": 0.44, "mp5": 0.15, "defense": 0.10, "weapon_skill": 0.30,
        "crit": 0.50, "spell_crit": 0.05, "hit": 0.60, "spell_hit": 0.05, "dodge": 0.20},
    # hunter: agility leads, and the dps term covers the ranged weapon it
    # actually kills with (inventory_type 15/26 scores through the same term).
    3: {"str": 0.3, "agi": 1.0, "int": 0.35, "spi": 0.1, "sta": 0.7,
        "armor": 0.49, "dps": 7.18, "spell_dmg": 0.0, "heal": 0.0,
        "attack_power": 0.44, "mp5": 0.15, "defense": 0.05, "weapon_skill": 0.25,
        "crit": 0.50, "spell_crit": 0.0, "hit": 0.60, "spell_hit": 0.0, "dodge": 0.15},
    4: {"str": 0.5, "agi": 1.0, "int": 0.0, "spi": 0.1, "sta": 0.8,
        "armor": 0.55, "dps": 7.18, "spell_dmg": 0.0, "heal": 0.0,
        "attack_power": 0.44, "mp5": 0.0, "defense": 0.05, "weapon_skill": 0.35,
        "crit": 0.55, "spell_crit": 0.0, "hit": 0.70, "spell_hit": 0.0, "dodge": 0.20},
    5: {"str": 0.0, "agi": 0.1, "int": 1.0, "spi": 0.8, "sta": 0.8,
        "armor": 0.27, "dps": 0.71, "spell_dmg": 1.00, "heal": 0.70,
        "attack_power": 0.0, "mp5": 0.60, "defense": 0.0, "weapon_skill": 0.0,
        "crit": 0.0, "spell_crit": 0.45, "hit": 0.0, "spell_hit": 0.50, "dodge": 0.05},
    # shaman: levels with a weapon in hand and heals between pulls, so it is the
    # one class where the melee and the caster half both carry weight.
    7: {"str": 0.8, "agi": 0.7, "int": 0.8, "spi": 0.5, "sta": 0.9,
        "armor": 0.55, "dps": 4.84, "spell_dmg": 0.50, "heal": 0.30,
        "attack_power": 0.35, "mp5": 0.30, "defense": 0.05, "weapon_skill": 0.25,
        "crit": 0.40, "spell_crit": 0.20, "hit": 0.45, "spell_hit": 0.20, "dodge": 0.15},
    8: {"str": 0.0, "agi": 0.1, "int": 1.0, "spi": 0.6, "sta": 0.8,
        "armor": 0.27, "dps": 0.71, "spell_dmg": 1.10, "heal": 0.05,
        "attack_power": 0.0, "mp5": 0.50, "defense": 0.0, "weapon_skill": 0.0,
        "crit": 0.0, "spell_crit": 0.50, "hit": 0.0, "spell_hit": 0.55, "dodge": 0.05},
    9: {"str": 0.0, "agi": 0.1, "int": 0.8, "spi": 0.5, "sta": 1.0,
        "armor": 0.27, "dps": 0.71, "spell_dmg": 1.10, "heal": 0.05,
        "attack_power": 0.0, "mp5": 0.40, "defense": 0.0, "weapon_skill": 0.0,
        "crit": 0.0, "spell_crit": 0.40, "hit": 0.0, "spell_hit": 0.50, "dodge": 0.05},
    # druid: levels in bear/cat, where the weapon's dps is not used at all -
    # form damage comes from attack power - so the dps weight is a fraction of
    # the melee classes' and the stats on the weapon are what matters.
    11: {"str": 0.8, "agi": 0.7, "int": 0.6, "spi": 0.4, "sta": 0.9,
         "armor": 0.62, "dps": 1.45, "spell_dmg": 0.40, "heal": 0.30,
         "attack_power": 0.50, "mp5": 0.30, "defense": 0.05, "weapon_skill": 0.10,
         "crit": 0.45, "spell_crit": 0.20, "hit": 0.50, "spell_hit": 0.20, "dodge": 0.20},
}
# Resistance gear is raid gear: it is never a levelling upgrade, and an item
# picked for 8 fire resist over 5 stamina is a wasted quest reward. Zero, said
# out loud for every class, rather than left out of the table and defaulted.
for _w in WEIGHTS.values():
    for _k in RES:
        _w[_k] = 0.0

# Not a fallback any more: every class id that can exist on a 1.12 server is in
# WEIGHTS above. This is what an unknown class id scores with while it shouts.
DEFAULT_WEIGHTS = {k: 0.0 for k in WEIGHT_KEYS}
DEFAULT_WEIGHTS.update({"str": 0.6, "agi": 0.6, "int": 0.6, "sta": 0.8, "spi": 0.4,
                        "armor": 0.41, "dps": 2.39})
_warned_classes: set[int] = set()

for _c, _w in WEIGHTS.items():                  # a missing key is a silent zero
    _missing = set(WEIGHT_KEYS) - set(_w)
    _extra = set(_w) - set(WEIGHT_KEYS)
    if _missing or _extra:
        raise AssertionError(f"gear.WEIGHTS[{_c}]: missing {_missing}, unexpected {_extra}")


def weights(class_id: int) -> dict:
    w = WEIGHTS.get(int(class_id))
    if w is None:
        if class_id not in _warned_classes:
            _warned_classes.add(class_id)
            print(f"gear: no weights for class {class_id} - scoring it as a generic "
                  f"melee character. Add it to gear.WEIGHTS.")
        return DEFAULT_WEIGHTS
    return w


_items: dict[int, dict] = {}


def _equip_effects(rows) -> dict[int, dict[str, float]]:
    """{item entry: {effect key: value}} for the equip-triggered spells.

    Same shape of query consumables.table() uses: spell_template carries one
    row per client build, so take the newest build this client can see.
    "rows" is [(item entry, spell id)] with spelltrigger already filtered.
    """
    want = sorted({s for _, s in rows if s})
    if not want:
        return {}
    cols = ",".join(f"sp.effectApplyAuraName{i},sp.effectBasePoints{i},sp.effectMiscValue{i}"
                    for i in (1, 2, 3))
    sql = (f"SELECT sp.entry,{cols} FROM spell_template sp "
           "JOIN (SELECT entry, MAX(build) b FROM spell_template "
           f"WHERE build <= {CLIENT_BUILD} AND entry IN ({','.join(map(str, want))}) "
           "GROUP BY entry) ms ON ms.entry = sp.entry AND ms.b = sp.build")
    spell: dict[int, dict[str, float]] = {}
    for r in worlddb._query(sql):
        v = [int(x) if str(x).lstrip("-").isdigit() else 0 for x in r]
        eff: dict[str, float] = {}
        ap: list[float] = []
        for i in range(3):
            aura, val, misc = v[1 + i * 3], v[2 + i * 3] + 1, v[3 + i * 3]
            if not aura or val <= 0:
                continue
            if aura == 13:                      # mod damage done
                if misc & MAGIC_SCHOOLS:
                    # a single-school bonus only helps the spells of that
                    # school, so it counts half until we know the spec
                    whole = (misc & MAGIC_SCHOOLS) == MAGIC_SCHOOLS
                    eff["spell_dmg"] = eff.get("spell_dmg", 0) + val * (1.0 if whole else 0.5)
                elif misc & 1:
                    # flat physical damage per swing; 14 attack power = 1 dps,
                    # and a levelling weapon swings about every 2 s
                    ap.append(val * 7.0)
                continue
            if aura == 30:                      # mod skill, miscValue = skill line
                if misc == DEFENSE_SKILL:
                    eff["defense"] = eff.get("defense", 0) + val
                elif misc in WEAPON_SKILL.values():
                    eff["weapon_skill"] = eff.get("weapon_skill", 0) + val
                continue
            if aura == 85 and misc:             # only mana regen, not energy/rage
                continue
            key = AURA_KEY.get(aura)
            if key == "attack_power":
                ap.append(float(val))           # see below: 99 and 124 are one line
            elif key:
                eff[key] = eff.get(key, 0) + val
        if ap:
            # "Attack Power 20" is written as melee (99) and ranged (124) auras
            # with the same value; the tooltip says 20 once, so take it once.
            eff["attack_power"] = eff.get("attack_power", 0) + max(ap)
        spell[v[0]] = eff
    out: dict[int, dict[str, float]] = {}
    for entry, sid in rows:
        for k, val in spell.get(sid, {}).items():
            d = out.setdefault(entry, {})
            d[k] = d.get(k, 0) + val
    return out


def items(entries) -> dict[int, dict]:
    missing = sorted({int(e) for e in entries} - _items.keys())
    if missing:
        stats = ",".join(f"stat_type{i},stat_value{i}" for i in range(1, 11))
        res = ",".join(f"t.{c}" for c in ("holy_res", "fire_res", "nature_res",
                                          "frost_res", "shadow_res", "arcane_res"))
        spells = ",".join(f"t.spellid_{i},t.spelltrigger_{i}" for i in range(1, 6))
        rows = worlddb._query(
            "SELECT t.entry, t.name, t.class, t.subclass, t.inventory_type, t.armor, t.required_level, "
            "t.allowable_class, t.quality, t.dmg_min1, t.dmg_max1, t.delay, "
            f"t.item_level, t.buy_price, t.sell_price, {stats}, {res}, {spells} "
            "FROM item_template t JOIN (SELECT entry, MAX(patch) p FROM item_template "
            f"WHERE entry IN ({','.join(map(str, missing))}) GROUP BY entry) m "
            "ON t.entry = m.entry AND t.patch = m.p")
        equip_spells = []
        fresh = []
        for r in rows:
            v = [int(x) if str(x).lstrip("-").isdigit() else x for x in r]
            it = {"entry": v[0], "name": r[1], "class": v[2], "subclass": v[3], "inv": v[4],
                  "armor": v[5], "req_level": v[6], "classes": v[7], "quality": v[8],
                  "dps": (float(r[9]) + float(r[10])) / 2 / (v[11] / 1000.0) if v[11] else 0.0,
                  "item_level": v[12] or 1, "buy_price": v[13], "sell_price": v[14],
                  "stats": {}, "res": {}, "effects": {}}
            for i in range(10):
                t, val = v[15 + 2 * i], v[16 + 2 * i]
                if t in STAT and val:
                    it["stats"][STAT[t]] = it["stats"].get(STAT[t], 0) + val
            for i, name in enumerate(RES):
                if v[35 + i]:
                    it["res"][name] = v[35 + i]
            for i in range(5):
                sid, trig = v[41 + 2 * i], v[42 + 2 * i]
                if sid and trig == 1:           # 1 = while equipped
                    equip_spells.append((it["entry"], sid))
            fresh.append(it)
            _items[it["entry"]] = it
        for entry, eff in _equip_effects(equip_spells).items():
            _items[entry]["effects"] = eff
        for e in missing:
            _items.setdefault(e, None)
    return {e: _items[e] for e in entries if _items.get(e)}


def score(it: dict | None, class_id: int, level: int | None = None) -> float:
    """What this item is worth to this character, in stat points.

    Every non-stat term is divided by what item_template pays for that quantity
    at the item level this character is entitled to wear, and multiplied back
    by the stat unit there. So the exchange rate - how much strength a point of
    weapon dps is worth - is a property of the character, not of the item, and
    every item in one decision is measured with the same ruler. It moves as the
    character levels, which is the whole point: at level 5 a point of dps is
    worth 3.0 strength to a warrior, at level 60 it is worth 4.6.

    level=None means "no character to measure against" (the module's own
    dump, an item looked at on its own): fall back to the item's own level.
    """
    if not it:
        return 0.0
    w = weights(class_id)
    u_stat, u_dps, u_armor = units(expected_item_level(level) if level
                                   else it.get("item_level", 1))
    s = sum(w[k] * v for k, v in it["stats"].items() if k in w)
    s += sum(w[k] * v for k, v in it.get("res", {}).items() if k in w)
    if it["armor"]:
        s += w["armor"] * (it["armor"] / u_armor) * u_stat
    if it["class"] == 2 and it["dps"]:
        s += w["dps"] * (it["dps"] / u_dps) * u_stat
    for k, v in it.get("effects", {}).items():
        if k in PERCENT_EFFECT:
            s += w[k] * v * u_stat
        else:
            s += w[k] * v / EFFECT_UNIT[k]
    # tie-breaker so an empty slot is always filled by any wearable item
    return s + 0.01 + 0.001 * it["quality"]


def can_use(it: dict, class_id: int, level: int, known: set[int]) -> bool:
    if it["inv"] not in SLOTS or it["req_level"] > level:
        return False
    if it["classes"] not in (-1, 0) and not it["classes"] & (1 << (class_id - 1)):
        return False
    if it["class"] == 2:
        return WEAPON_SKILL.get(it["subclass"]) in known
    if it["class"] == 4:
        need = ARMOR_SKILL.get(it["subclass"])
        return need is None or need in known
    return False


def _worn_score(worn_scores, slot: int) -> float:
    """Score of what sits in a slot; slots past the end of the array are empty."""
    return worn_scores[slot] if 0 <= slot < len(worn_scores) else 0.0


def slots_for(it: dict, worn: list[int], worn_scores: list[float],
              known: set[int]) -> tuple[list[int] | None, float]:
    """(equipment slots this item may take, score it would displace).

    slots is None when equipping it would be a downgrade by construction - an
    off-hand while a two-hander is worn. This used to live inside upgrades()
    only, so best_choice() resolved a two-hander to SLOTS[17] = [15] and
    compared it against the main hand alone, valuing it as if the off-hand were
    free; that is the same bug in two places, so there is now one place.

    Dual wield is a trained skill (118, at level 20 for warriors, rogues and
    hunters), not a class fact: before that the off-hand does not exist, and a
    character that has it and leaves the slot empty is throwing away a whole
    weapon's worth of damage for the rest of the run.
    """
    inv = it["inv"]
    if inv == TWO_HAND:
        return [MAIN_HAND], _worn_score(worn_scores, MAIN_HAND) + _worn_score(worn_scores, OFF_HAND)
    if inv == 13:                               # one-hand
        slots = [MAIN_HAND, OFF_HAND] if DUAL_WIELD in known else [MAIN_HAND]
    elif inv in (22, 14, 23):                   # off-hand, shield, holdable
        slots = [OFF_HAND]
    else:
        slots = SLOTS.get(inv)
        if slots is None:
            return None, 0.0
    if OFF_HAND in slots:
        main = worn[MAIN_HAND] if MAIN_HAND < len(worn) else 0
        two_handed = main and (items([main]).get(main) or {}).get("inv") == TWO_HAND
        if two_handed:
            if MAIN_HAND not in slots:
                return None, 0.0                # would unequip a two-hander
            slots = [MAIN_HAND]                 # the off-hand is not free yet
    return slots, min(_worn_score(worn_scores, s) for s in slots)


def durability(g) -> tuple[int, int, float]:
    """(current, maximum, lowest percent) over the worn items that wear."""
    me = g.player()
    d = g.p.u32(me.base + O.OBJ_DESCRIPTORS)
    worn_g = set(struct.unpack("<19Q", g.p.read(d + INV_SLOT_HEAD, EQUIP_SLOTS * 8))) - {0}
    cur = mx = 0
    low = 100.0
    for base, guid, typ in g.iter_objects():
        if typ == O.TYPE_ITEM and guid in worn_g:
            idesc = g.p.u32(base + O.OBJ_DESCRIPTORS)
            c, m = g.p.u32(idesc + O.ITEM_FIELD_DURABILITY), g.p.u32(idesc + O.ITEM_FIELD_MAX_DURABILITY)
            if m:
                cur += c
                mx += m
                low = min(low, 100.0 * c / m)
    return cur, mx, low


def worn_and_bag(g) -> tuple[list[int], dict[int, int]]:
    """([entry per equipment slot, 0 = empty], {bag guid: entry})."""
    me = g.player()
    d = g.p.u32(me.base + O.OBJ_DESCRIPTORS)
    worn_g = struct.unpack("<19Q", g.p.read(d + INV_SLOT_HEAD, EQUIP_SLOTS * 8))
    bag_g = g._all_item_guids()             # backpack and every equipped bag
    entry_of = {}
    for base, guid, typ in g.iter_objects():
        if typ in (O.TYPE_ITEM, O.TYPE_CONTAINER):
            entry_of[guid] = g.p.u32(g.p.u32(base + O.OBJ_DESCRIPTORS) + O.OBJECT_FIELD_ENTRY)
    worn = [entry_of.get(x, 0) if x else 0 for x in worn_g]
    bag = {x: entry_of[x] for x in bag_g if x and x in entry_of}
    return worn, bag


def worn_scores(worn: list[int], class_id: int, level: int,
                info: dict | None = None) -> list[float]:
    info = info if info is not None else items([e for e in worn if e])
    return [score(info.get(e), class_id, level) for e in worn]


def upgrades(g, p) -> list[tuple[int, float, str]]:
    """Bag items worth equipping, best first: [(entry, gain, description)]."""
    me = g.player()
    known = known_skills(g)
    worn, bag = worn_and_bag(g)
    info = items(set(worn) | set(bag.values()) - {0})
    cls = me.class_id
    ws = worn_scores(worn, cls, me.level, info)
    out = []
    for entry in set(bag.values()):
        it = info.get(entry)
        if not it or not can_use(it, cls, me.level, known):
            continue
        slots, old = slots_for(it, worn, ws, known)
        if slots is None:
            continue
        new = score(it, cls, me.level)
        if new > old + 0.05:
            out.append((entry, new - old, f"{it['name']} ({new:.1f} vs {old:.1f})"))
    return sorted(out, key=lambda t: -t[1])


def best_choice(choices: list[int], class_id: int, level: int, known: set[int],
                worn: list[int] | None = None, bag: list[int] | None = None) -> int:
    """1-based index of the quest reward to take.

    The biggest upgrade over what the character would otherwise have in that
    slot - which is the better of what is worn and what is already in the bags,
    since gear_check equips the bag item within twenty seconds anyway. When
    nothing on offer is usable (the cloth/leather/mail/plate reward sets that
    no single character can wear through), take the one that sells for most:
    that money is what buys the vendor weapon in vendor_purchases(), so an
    unusable reward is still worth choosing carefully.
    """
    info = items([c for c in choices if c])
    worn = list(worn or [])
    all_worn = items([w for w in worn if w])
    ws = worn_scores(worn, class_id, level, all_worn)
    if bag:
        bag_info = items([b for b in bag if b])
        for entry, it in bag_info.items():
            if not can_use(it, class_id, level, known):
                continue
            slots, _ = slots_for(it, worn, ws, known)
            for s in slots or []:
                if 0 <= s < len(ws):
                    ws[s] = max(ws[s], score(it, class_id, level))
    best, best_gain = 0, float("-inf")
    for i, c in enumerate(choices, 1):
        it = info.get(c)
        if not it or not can_use(it, class_id, level, known):
            continue
        slots, old = slots_for(it, worn, ws, known)
        if slots is None:
            continue
        gain = score(it, class_id, level) - old
        if gain > best_gain:
            best, best_gain = i, gain
    if best and best_gain > 0.05:
        return best
    # nothing usable, or nothing worth wearing: take the money instead
    sellable = sorted(((info.get(c, {}).get("sell_price", 0), -i, i)
                       for i, c in enumerate(choices, 1)), reverse=True)
    if sellable and sellable[0][0] > 0:
        return sellable[0][2]
    return best or 1


# --- turning money into power ----------------------------------------------

def vendor_items(vendor_entries) -> dict[int, tuple[dict, int]]:
    """{item entry: (item, buy_price)} for the gear these vendors sell.

    One query, de-duplicated per patch exactly like items(), and restricted to
    weapons and armour - the vendor's food, reagents and bags are somebody
    else's problem. Vendors carry a weapon at nearly every required_level from
    1 to 60, which is why this is a rule for the whole run and not a patch for
    one starting zone.
    """
    ids = sorted({int(e) for e in vendor_entries})
    if not ids:
        return {}
    rows = worlddb._query(
        "SELECT DISTINCT v.item FROM npc_vendor v "
        f"JOIN item_template t ON t.entry = v.item AND t.class IN (2, 4) "
        f"WHERE v.entry IN ({','.join(map(str, ids))})")
    entries = [int(r[0]) for r in rows]
    info = items(entries)
    return {e: (it, it["buy_price"]) for e, it in info.items() if it["buy_price"] > 0}


def vendor_purchases(offers, class_id: int, level: int, known: set[int],
                     worn: list[int], budget: int) -> list[tuple[int, int, float, str]]:
    """What to buy with this budget, best value first: [(entry, price, gain, why)].

    Greedy on gain per copper, one item per slot, never over budget. No money
    policy lives here: the caller decides what the budget is (repairs and
    training come first), this only says what the money is worth in power.

    The rule that makes it safe on a levelling character: nothing is bought
    whose gain per copper is under a quarter of the best main-hand candidate's.
    A level-7 character holding 400 copper and a 2.14 dps starter weapon while
    a 5.00 dps vendor weapon sits on the shelf for 414 was losing 42% of its
    health per kill; a pair of 1-armour bracers for the same money changes
    nothing.
    """
    worn = list(worn or [])
    ws = worn_scores(worn, class_id, level)
    cand = []
    for entry, (it, price) in dict(offers).items():
        if price <= 0 or price > budget:
            continue
        if not can_use(it, class_id, level, known):
            continue
        slots, old = slots_for(it, worn, ws, known)
        if slots is None:
            continue
        new = score(it, class_id, level)
        gain = new - old
        if gain <= 0.05:
            continue
        slot = min(slots, key=lambda s: _worn_score(ws, s))
        empty = not (0 <= slot < len(worn) and worn[slot])
        cand.append((gain / price, slot == MAIN_HAND, empty, gain, entry, price, it, new))
    if not cand:
        return []
    # Best gain per copper first - that is what maximises the power bought with
    # this budget - and ties broken toward the main hand, then an empty slot,
    # then the biggest gain: a character with nothing in a slot gains the whole
    # item, but a weapon it swings every 2 s is worth more than the bracer it
    # has never had.
    cand.sort(key=lambda c: (-c[0], not c[1], not c[2], -c[3]))
    best_mh = max((c[0] for c in cand if c[1]), default=0.0)
    have = list(ws)             # what each slot holds as the basket grows
    out, spent, taken = [], 0, set()
    for rate, is_mh, empty, _gain, entry, price, it, new in cand:
        if best_mh and rate < best_mh / 4 and not is_mh:
            continue
        if spent + price > budget:
            continue
        slots, old = slots_for(it, worn, have, known)
        if slots is None:
            continue
        slot = min(slots, key=lambda s: _worn_score(have, s))
        gain = new - old
        if slot in taken or gain <= 0.05:       # already bought for this slot
            continue
        # A two-hander and anything in the off hand cannot both be worn, but
        # they land in different slot keys (15 and 16), so `taken` alone does
        # not catch the pair - and slots_for() only knows what is WORN, not
        # what this basket has already put in the main hand. Without this the
        # basket bought a 2H axe and a shield in one trip and the shield was
        # unequippable money gone.
        if it["inv"] == TWO_HAND and OFF_HAND in taken:
            continue                            # already bought an off hand
        taken.add(slot)
        if it["inv"] == TWO_HAND:
            taken.add(OFF_HAND)                 # the two-hander occupies it
            if 0 <= OFF_HAND < len(have):
                have[OFF_HAND] = 0.0            # and unequips what was there
        if 0 <= slot < len(have):
            have[slot] = new
        spent += price
        out.append((entry, price, gain, f"{it['name']} for slot {slot}: {old:.1f} -> {new:.1f}"))
    return out


# A representative inventory_type per equipment slot, and how much armour that
# slot carries against the all-slots median of its item level bucket (fitted
# offline from item_template: a chest is 1.79 of it, a cloak 0.32, hands 0.99;
# those hold within a few percent from item level 1 to 80). This is only used
# to build the median item a slot ought to hold.
SLOT_INV = {0: 1, 1: 2, 2: 3, 4: 5, 5: 6, 6: 7, 7: 8, 8: 9, 9: 10, 10: 11,
            11: 11, 12: 12, 13: 12, 14: 16, 15: 13, 16: 13, 17: 15}
ARMOR_SHARE = {1: 1.08, 3: 1.11, 5: 1.79, 6: 0.96, 7: 1.39, 8: 1.12, 9: 0.73,
               10: 0.99, 16: 0.32, 20: 0.69}
# The shield is the one slot whose share of the curve is not a constant: it
# carries 1.7x a body slot's armour at item level 1 and 14x at 60. One number
# fitted anywhere on that line is wrong everywhere else, so it gets three.
SHIELD_SHARE = {1: 1.7, 20: 9.1, 60: 14.5}
STALE_SLOTS = tuple(SLOT_INV)           # shirt (3) and tabard (18) carry nothing


def _armor_share(inv: int, ilvl: int) -> float:
    if inv != 14:
        return ARMOR_SHARE.get(inv, 0.0)
    keys = sorted(SHIELD_SHARE)
    il = max(keys[0], min(int(ilvl), keys[-1]))
    lo = max(k for k in keys if k <= il)
    if lo == keys[-1]:
        return SHIELD_SHARE[lo]
    hi = min(k for k in keys if k > il)
    f = (il - lo) / (hi - lo)
    return SHIELD_SHARE[lo] + (SHIELD_SHARE[hi] - SHIELD_SHARE[lo]) * f


def _median_item(inv: int, ilvl: int, class_id: int) -> dict:
    """The item item_template says a slot of this kind holds at this level."""
    u_stat, u_dps, u_armor = units(ilvl)
    w = weights(class_id)
    top = max(("str", "agi", "int"), key=lambda k: w[k])
    weapon = inv in (13, 17, 15, 21, 22, 26)
    return {"entry": 0, "name": f"median inv {inv}", "class": 2 if weapon else 4,
            "subclass": 0, "inv": inv, "armor": u_armor * _armor_share(inv, ilvl),
            "req_level": 0, "classes": -1, "quality": 2, "item_level": ilvl,
            "dps": u_dps if weapon else 0.0, "buy_price": 0, "sell_price": 0,
            "stats": {top: u_stat}, "res": {}, "effects": {}}


def staleness_of(worn: list[int], class_id: int, level: int,
                 known: set[int]) -> tuple[dict[int, tuple[int, int, float]], float]:
    """({slot: (worn item level, expected item level, share of score)}, gap 0..1).

    upgrades() only ever compared the bags against the body, so a slot that was
    empty, or held the item level 2 thing the character started the run in,
    produced no signal at all - at level 7 or at level 47. This is the absolute
    measure: how far behind the item level this character level is entitled to
    (expected_item_level) the worn gear is, weighted by that slot's share of
    what the character's gear is worth to this class - for a warrior at level 7
    the main hand is 0.24 of it, the shield 0.15, a wrist 0.03 - so the gap
    means the same thing at every level and for every class.
    """
    exp = expected_item_level(level)
    info = items([e for e in worn if e])
    per: dict[int, tuple[int, int, float]] = {}
    total_w = short = 0.0
    for slot in STALE_SLOTS:
        inv = SLOT_INV[slot]
        if slot == OFF_HAND:
            # the off-hand only exists once the character can use one
            if DUAL_WIELD in known:
                inv = 13
            elif SHIELD_SKILL in known:
                inv = 14
            else:
                inv = None
        elif slot == RANGED and not ({WEAPON_SKILL[s] for s in RANGED_SUBCLASS} & known):
            inv = None                  # no bow, gun, crossbow, thrown or wand skill
        w = score(_median_item(inv, exp, class_id), class_id, level) if inv else 0.0
        it = info.get(worn[slot]) if slot < len(worn) else None
        ilvl = int(it["item_level"]) if it else 0
        per[slot] = (ilvl, exp, w)
        total_w += w
        short += w * min(1.0, max(0.0, (exp - ilvl) / float(exp)))
    if total_w:                         # weights out as shares, so they read
        for slot, (ilvl, e, w) in per.items():      # the same at level 7 and 60
            per[slot] = (ilvl, e, w / total_w)
    return per, (short / total_w if total_w else 0.0)


def staleness(g) -> tuple[dict[int, tuple[int, int, float]], float]:
    me = g.player()
    worn, _ = worn_and_bag(g)
    return staleness_of(worn, me.class_id, me.level, known_skills(g))


if __name__ == "__main__":
    from game import Game
    from wowmem import Process
    p = Process()
    g = Game(p)
    worn, bag = worn_and_bag(g)
    info = items(set(worn) | set(bag.values()) - {0})
    cls = g.player().class_id
    for s, e in enumerate(worn):
        if e:
            print(f"slot {s:2d}: {info.get(e, {}).get('name', e)}  "
                  f"score {score(info.get(e), cls, g.player().level):.1f}")
    print("bag:", [info.get(e, {}).get("name", e) for e in bag.values()])
    print("upgrades:", upgrades(g, p))
    per, gap = staleness(g)
    print(f"staleness: gap {gap:.2f} at level {g.player().level} "
          f"(expected item level {expected_item_level(g.player().level)})")
    for s in sorted(per, key=lambda s: -per[s][2]):
        ilvl, exp, w = per[s]
        print(f"  slot {s:2d}: item level {ilvl:3d} vs {exp:3d}  weight {w:6.1f}")
