"""What the character knows, what is on its bars, and what it could learn.

Known spells and action bar come from memory (offsets verified live); spell
names, ranks and trainer prices come from the world DB.
"""

from __future__ import annotations

import struct

import offsets as O
import worlddb

MAGE_TRAINER_TEMPLATE = 5
BUILD = 5875

KEY_FOR_SLOT = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8",
                9: "9", 10: "0", 11: "-", 12: "="}


def known_spells(p) -> set[int]:
    raw = p.read(O.SPELLBOOK, 4 * 1024)
    out = set()
    for (v,) in struct.iter_unpack("<I", raw):
        if not v:
            break
        out.add(v)
    return out


def action_bar(p) -> list[int]:
    return list(struct.unpack("<120I", p.read(O.ACTION_BAR, 480)))


_names: dict[int, str] = {}


def spell_names(ids) -> dict[int, str]:
    missing = [i for i in ids if i not in _names]
    if missing:
        rows = worlddb._query(
            "SELECT s.entry, s.name FROM spell_template s JOIN (SELECT entry, MAX(build) b "
            f"FROM spell_template WHERE build<={BUILD} AND entry IN ({','.join(map(str, missing))}) "
            "GROUP BY entry) m ON s.entry=m.entry AND s.build=m.b")
        for e, n in rows:
            _names[int(e)] = n
    return {i: _names.get(i, f"#{i}") for i in ids}


def known_by_name(p) -> dict[str, set[int]]:
    """Spell name -> ids of every rank we know."""
    out: dict[str, set[int]] = {}
    for sid, name in spell_names(known_spells(p)).items():
        out.setdefault(name, set()).add(sid)
    return out


def rank_ids(name: str) -> set[int]:
    """All spell ids that share this name (every rank; used for aura checks)."""
    rows = worlddb._query(
        f"SELECT DISTINCT entry FROM spell_template WHERE name='{name}' AND build<={BUILD}")
    return {int(r[0]) for r in rows}


_plans: dict[int, list[dict]] = {}


def trainer_plan(template: int = MAGE_TRAINER_TEMPLATE) -> list[dict]:
    """Everything a trainer template teaches for this build (cached)."""
    if template in _plans:
        return _plans[template]
    rows = worlddb._query(
        "SELECT t.reqlevel, t.spellcost, t.spell, "
        "(SELECT effectTriggerSpell1 FROM spell_template x WHERE x.entry=t.spell "
        f"AND x.build<={BUILD} ORDER BY build DESC LIMIT 1) "
        f"FROM npc_trainer_template t WHERE t.entry={template} "
        f"AND {BUILD} BETWEEN t.build_min AND t.build_max ORDER BY t.reqlevel")
    plan = [{"level": int(lv), "cost": int(c), "teach": int(t), "learns": int(l)}
            for lv, c, t, l in rows if l and l != "NULL"]
    names = spell_names([x["learns"] for x in plan])
    for x in plan:
        x["name"] = names[x["learns"]]
    _plans[template] = plan
    return plan


def trainable_now(p, level: int, plan=None) -> list[dict]:
    plan = plan or trainer_plan()
    known = known_spells(p)
    return [x for x in plan if x["level"] <= level and x["learns"] not in known]
