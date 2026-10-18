"""The world DB's side of a quest guide: who gives and takes each quest,
what to kill or collect, which mobs drop an item and which objects hold it.

The guide itself (rxp.py) gives the ORDER and the ROUTE; this supplies HOW
to do each step, from the server's own tables.
"""

from __future__ import annotations

import worlddb


# ---------------------------------------------------------------- DB checks
_quests: dict[int, dict] = {}


def quests(ids) -> dict[int, dict]:
    missing = sorted({int(i) for i in ids} - _quests.keys())
    if missing:
        rows = worlddb._query(
            "SELECT q.entry, q.Title, q.MinLevel, q.QuestLevel, q.RequiredRaces, q.RequiredClasses, "
            "q.ReqCreatureOrGOId1, q.ReqCreatureOrGOId2, q.ReqCreatureOrGOId3, q.ReqCreatureOrGOId4, "
            "q.ReqCreatureOrGOCount1, q.ReqCreatureOrGOCount2, q.ReqCreatureOrGOCount3, q.ReqCreatureOrGOCount4, "
            "q.ReqItemId1, q.ReqItemId2, q.ReqItemId3, q.ReqItemId4, "
            "q.ReqItemCount1, q.ReqItemCount2, q.ReqItemCount3, q.ReqItemCount4, "
            "q.ReqSpellCast1, q.RewXP, q.SrcItemId, "
            "(SELECT GROUP_CONCAT(id) FROM creature_questrelation r WHERE r.quest=q.entry), "
            "(SELECT GROUP_CONCAT(id) FROM gameobject_questrelation r WHERE r.quest=q.entry), "
            "(SELECT GROUP_CONCAT(id) FROM creature_involvedrelation r WHERE r.quest=q.entry), "
            "(SELECT GROUP_CONCAT(id) FROM gameobject_involvedrelation r WHERE r.quest=q.entry), "
            "(SELECT COUNT(*) FROM areatrigger_involvedrelation r WHERE r.quest=q.entry) "
            "FROM quest_template q JOIN (SELECT entry, MAX(patch) p FROM quest_template "
            f"WHERE entry IN ({','.join(map(str, missing))}) GROUP BY entry) m "
            "ON q.entry = m.entry AND q.patch = m.p")

        def ids_(v):
            return [int(x) for x in v.split(",")] if v not in ("NULL", "") else []

        for r in rows:
            e = int(r[0])
            # keep the slot index k: the quest log counts objectives by their
            # ReqCreatureOrGOId column, and dropping the empty ones here
            # would renumber them
            kills = [(int(r[6 + k]), int(r[10 + k]), k) for k in range(4) if int(r[6 + k])]
            items = [(int(r[14 + k]), int(r[18 + k])) for k in range(4) if int(r[14 + k])]
            _quests[e] = {
                "id": e, "title": r[1], "min_level": int(r[2]), "level": int(r[3]),
                "races": int(r[4]), "classes": int(r[5]),
                # ReqCreatureOrGOId > 0 = kill creature, < 0 = use game object
                "kill": [(c, n) for c, n, _ in kills if c > 0],
                "use_object": [(-c, n) for c, n, _ in kills if c < 0],
                "slot": {abs(c): k for c, _, k in kills},
                "collect": items, "cast": int(r[22]), "xp": int(r[23]),
                "starter_npcs": ids_(r[25]), "starter_objects": ids_(r[26]),
                "ender_npcs": ids_(r[27]), "ender_objects": ids_(r[28]),
                "explore": int(r[29]) > 0,
            }
    return {i: _quests[i] for i in ids if i in _quests}


def item_sources(item_ids) -> dict[int, dict]:
    """Where a quest item comes from: creature drops and/or game objects."""
    if not item_ids:
        return {}
    ids = ",".join(map(str, set(item_ids)))
    out = {i: {"creatures": set(), "objects": set()} for i in item_ids}
    # a 0.03% chance (Elwynn murlocs and the Murloc Eye) is not a source:
    # 40 minutes of foragers for nothing. Negative = quest drop (100%).
    for item, cr in worlddb._query(
            f"SELECT item, entry FROM creature_loot_template WHERE item IN ({ids}) "
            "AND (ChanceOrQuestChance < 0 OR ChanceOrQuestChance >= 5)"):
        out[int(item)]["creatures"].add(int(cr))
    for item, go in worlddb._query(
            "SELECT l.item, g.entry FROM gameobject_loot_template l JOIN gameobject_template g "
            f"ON g.data1 = l.entry AND g.type IN (3, 25) WHERE l.item IN ({ids})"):
        out[int(item)]["objects"].add(int(go))
    return out


def item_vendors(item: int) -> set[int]:
    """Creature entries whose vendor list carries the item."""
    return {int(r[0]) for r in worlddb._query(f"SELECT entry FROM npc_vendor WHERE item={item}")}
