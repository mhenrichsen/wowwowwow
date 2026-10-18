"""Quest planning for the nb bot from the (1.12) world DB + the live quest log.

The client addon accepts/turns in whatever an NPC offers, so the bot only has
to decide WHERE to go: a quest giver with something new, the ender of a
finished quest, or the mobs a quest still needs.
"""

from __future__ import annotations

import json
import math
import os
import time

import worlddb

HERE = os.path.dirname(os.path.abspath(__file__))
from nb import inst
DONE_FILE = inst.path("quests_done.json")

from nb import route as _route   # noqa: E402
RACE_MASK, CLASS_MASK = _route.RACE_MASK, _route.CLASS_MASK   # the configured race / class (login.RACECLASS)
AREA = (-6000.0, 0.0, 1400.0)       # Coldridge + Kharanos + east Dun Morogh
# where the bot quests: Dun Morogh, the North Gate pass and Loch Modan (all walkable on the bot mesh).
# Ironforge, the tram and the flight/boat legs of the RestedXP route are outside on purpose.
AREAS = list(_route.AREAS)     # (nb/route.py: dwarf = AREA, the North Gate pass, Loch Modan)
MAX_QUEST_MIN = 20                  # quests up to this required level are loaded
SKIP = {287, 312, 291,                    # packs/elites/far errands (2160 Supplies to Tannok is back in: a pure delivery)
        310, 311, 308, 403}               # the ale-barrel chain: barrel 270 only exists ~40 s after quest 308's script runs
# (5541 Ammo for Rumbleshot is back in: its data path is complete. 419 The Lost Pilot is back in: it ends at object
# 2059 through Bot.use_object, which now waits for the auto-offered 417 A Pilot's Revenge before closing windows)
POI_MARGIN = 40.0                   # yd around a quest's objective area (tcf_world quest_poi) where its mobs still count
# explore quests: only these of a quest's areatriggers are worth walking to. 287 Frostmane Hold also lists 169
# ("Unused, Tunnel to Arctikus", mangos areatrigger_template), which sits on the elite Great Father Arctikus 1260
EXPLORE_ONLY = {287: {97}}
# special objectives (quest_objectives Type 14 criteria tree / 19 areatrigger enter) with no data the bot can read:
# quest -> ('sit', spot, seconds) | ('visit', spot, radius).
#   96608 The Great Outdoors: "Sit near the campfire" + "Gain the Boosted Rest buff" at Basic Campfire 529161
#         (tcf gameobject, map 0, the Kharanos one)
#   98319 Secure the Mountain: areatrigger 44117 has no spawn yet (QUEST_PLAN.md S6 adds one at (-5506.3,-317.1),
#         r 8, which then wins); the RestedXP guide's own spot until then
SPECIAL_SPOTS = {96608: ("sit", (-5644.99, -496.71, 396.65), 75),
                 98319: ("visit", (-5506.3, -312.5, 352.5), 8.0)}
RARE_ELITE = (1, 2, 4)              # Classification elite, rare elite, rare: only hunted at or above their level


def _q(sql):
    return worlddb._query(sql)


def _tcf(sql):
    return worlddb._query_db("tcf_world", sql)


# quests the server added (no row in quest_template): quest id -> (ender npc as the client sees it, position, title).
# 98581 "Earth Sapta" is picked up from Sten Stoutarm after Dwarven Outfitters and turned in to the shaman trainer.
CUSTOM_ENDERS = {98581: (257446, (-6051.3, 389.26, 398.96), "Earth Sapta")}


class QuestDB:
    def __init__(self):
        self.q = {}
        rows = _q("SELECT entry, Title, MinLevel, QuestLevel, PrevQuestId, RequiredRaces, RequiredClasses, "
                  "ReqCreatureOrGOId1, ReqCreatureOrGOId2, ReqCreatureOrGOId3, ReqCreatureOrGOId4, "
                  "ReqItemId1, ReqItemId2, ReqItemId3, ReqItemId4, ExclusiveGroup, Method, RequiredSkill, "
                  "RequiredSkillValue, SrcItemId "
                  "FROM quest_template q WHERE patch = (SELECT MAX(patch) FROM quest_template WHERE entry=q.entry) "
                  "AND MinLevel <= %d" % MAX_QUEST_MIN)
        short = 0
        for r in rows:
            if len(r) < 20 or not str(r[0]).isdigit():
                # a row the mysql client split (a title with a line break, or output cut while another query ran):
                # bot 1 13:0x and bot 2 17:28:31 died on start with IndexError here
                short += 1
                continue
            e = int(r[0])
            self.q[e] = dict(id=e, title=r[1], min=int(r[2]), level=int(r[3]), prev=int(r[4]),
                             races=int(r[5]), classes=int(r[6]),
                             kill=[int(x) for x in r[7:11] if int(x) > 0],
                             items=[int(x) for x in r[11:15] if int(x) > 0],
                             excl=int(r[15]), method=int(r[16]), skill=int(r[17]),
                             # skill_min: the skill VALUE asked for (384 Beer Basted Boar Ribs names Cooking 185 at
                             # value 0: anyone may take it); src: the item the quest hands out on accept (Method 0 =
                             # autocomplete in 1.12). The server's own values overwrite these in _load_objective_model
                             skill_min=int(r[18]), src=int(r[19]), auto=int(r[16]) == 0)
        if short:
            print(f"quests: {short} malformed quest_template row(s) skipped", flush=True)
        self.starters = self._rel("creature_questrelation")
        self.enders = self._rel("creature_involvedrelation")
        self.spawns = {}      # npc -> [(x,y,z)]
        ids = {n for v in list(self.starters.values()) + list(self.enders.values()) for n in v}
        for q in self.q.values():
            ids |= set(q["kill"])
        self.item_src = {}
        self.item_chance = {}     # (item, creature) -> drop chance in percent
        self._counts = {}         # quest -> objective counters from the live log (255 = filled)
        self._level = None        # character level of the current plan() call (objective mobs above level+1 are left out)
        items = {i for q in self.q.values() for i in q["items"]}
        if items:
            # drop chances from the server's own loot (tcf_world); the 1.12 table only for items it does not list
            got = self._tcf_loot(items)
            ids |= {m for it in got for m in self.item_src.get(it, ())}
            rest = items - got
            rows = _q("SELECT t.entry, l.item, l.ChanceOrQuestChance FROM creature_loot_template l JOIN creature_template t "
                      "ON t.loot_id = l.entry WHERE l.item IN (%s) AND l.ChanceOrQuestChance <> 0"
                      % ",".join(map(str, rest))) if rest else []
            for e, it, ch in rows:
                self.item_src.setdefault(int(it), set()).add(int(e))
                self.item_chance[(int(it), int(e))] = abs(float(ch))
                ids.add(int(e))
        # quest items that come from chests (gameobject loot), not from mobs: item -> [(go entry, (x, y, z))]
        self.go_src = {}
        self.skip_gather = {}     # (quest, go, spot) -> time until which a failed object is left alone
        self.gathered = {}        # (quest, go, spot) -> time it was last looted (respawn wait)
        if items:
            try:
                for it, ge, gx, gy, gz in _q(
                        "SELECT l.item, t.entry, s.position_x, s.position_y, s.position_z "
                        "FROM gameobject_loot_template l JOIN gameobject_template t "
                        "ON t.type = 3 AND t.data1 = l.entry AND t.patch = "
                        "(SELECT MAX(patch) FROM gameobject_template WHERE entry = t.entry) "
                        "JOIN gameobject s ON s.id = t.entry AND s.map = 0 "
                        "WHERE l.item IN (%s) AND l.ChanceOrQuestChance <> 0" % ",".join(map(str, items))):
                    self.go_src.setdefault(int(it), []).append((int(ge), (float(gx), float(gy), float(gz))))
            except Exception:
                pass
        rows = _q("SELECT id, position_x, position_y, position_z FROM creature WHERE map = 0 AND id IN (%s)"
                  % ",".join(map(str, ids)))
        for e, x, y, z in rows:
            self.spawns.setdefault(int(e), []).append((float(x), float(y), float(z)))
        # a dropper the server's loot names but the 1.12 spawn table lacks: its spawns come from tcf_world
        miss = ids - set(self.spawns)
        self._tcf_mobs = set()    # creatures whose spawns / names / levels come from tcf_world
        if miss:
            try:
                for e, x, y, z in _tcf("SELECT id, position_x, position_y, position_z FROM creature WHERE map = 0 AND id IN (%s)"
                                       % ",".join(map(str, miss))):
                    self.spawns.setdefault(int(e), []).append((float(x), float(y), float(z)))
                self._tcf_mobs = miss & set(self.spawns)
            except Exception as e:
                print(f"quests: tcf_world spawns failed: {e!r}")
        # quests that end at a game object (a barrel, a chest ...) instead of an NPC
        self.go_enders = {}       # quest -> [(go id, (x, y, z))]
        # objects the bot cannot use yet: the Thunderbrew ale barrel (quest 310) has no softinteract target at its spot
        self.skip_go = {270}
        # hunt quests whose targets are all indoors (caves): skipped by the bot. (218 The Stolen Journal was here while
        # Grik'nir's Frost Shock hit ~190: a server bug, SpellInfo.cpp UsesClassicFlatBasePoints, fixed 2026-10-14 16:30)
        self.skip_q = set()
        self.skip_lvl = {}        # quest -> character level when it went into skip_q (a level-up lets it back in)
        self.shelved = {}         # quest -> (until, level): left alone for a while (no target seen at its spawn ...)
        self.poi = {}             # quest -> {objective creature/item id: [polygon [(x, y)], ...]} (tcf_world quest_poi, map 0)
        self.focus = None
        for qid_, go_, x_, y_, z_ in _q("SELECT r.quest, g.id, g.position_x, g.position_y, g.position_z "
                                        "FROM gameobject_involvedrelation r JOIN gameobject g ON g.id = r.id "
                                        "WHERE g.map = 0"):
            if int(qid_) in self.q:
                self.go_enders.setdefault(int(qid_), []).append((int(go_), (float(x_), float(y_), float(z_))))
        self.names = {int(e): n for e, n in _q("SELECT entry, name FROM creature_template WHERE entry IN (%s)"
                                                 % ",".join(map(str, ids)))}
        # highest level each creature spawns at (for choosing hunts we can actually win)
        self.mob_hi = {}
        try:
            import dangers
            for row in dangers._load():
                self.mob_hi[int(row[5])] = max(self.mob_hi.get(int(row[5]), 0), int(row[7]))
        except Exception:
            pass
        if self._tcf_mobs:                        # their names and levels from the server too (Stonesplinter Scout 1162 ...)
            try:
                nl = ",".join(map(str, self._tcf_mobs))
                for e, n in _tcf(f"SELECT entry, name FROM creature_template WHERE entry IN ({nl})"):
                    self.names.setdefault(int(e), n)
                for e, hi in _tcf(f"SELECT entry, level_max FROM creature_classic_level WHERE entry IN ({nl})"):
                    self.mob_hi[int(e)] = max(self.mob_hi.get(int(e), 0), int(hi))
            except Exception as e:
                print(f"quests: tcf_world creature info failed: {e!r}")
        try:
            self._add_server_quests()
        except Exception as e:                    # the 1.12 data alone still drives the bot
            print(f"quests: tcf_world supplement failed: {e!r}")
        try:
            self._add_questitem_objects()
        except Exception as e:
            print(f"quests: gameobject_questitem failed: {e!r}")
        # the objective model (QUEST_PLAN.md WP2). Defaults first: any part that fails leaves the old behaviour
        self.explore = {}         # quest -> [(areatrigger id, (x, y, z), radius)]: "explore" quests (287 Frostmane Hold)
        self.go_starters = {}     # quest -> [(go id, (x, y, z))]: quests an object gives (2059 wreck -> 417 ...)
        self.vendor_src = {}      # item -> [(npc, (x, y, z))]: objective items only a vendor sells (384's malt)
        self.cls = {}             # creature -> creature_template.Classification (1 elite, 2 rare elite, 3 boss, 4 rare)
        # plan kinds beyond talk/hunt/use/gather that the bot can carry out: 'explore', 'visit', 'sit'. Empty until
        # Bot implements them (QUEST_PLAN.md WP3a), so plan() never hands the bot a tuple it cannot run
        self.can_do = set()
        self.log = print          # Bot puts its own log here (nb.log)
        self._lvl_note = set()    # (quest, level) whose 'all objective mobs too high' line was logged
        for q in self.q.values():
            q.setdefault("idx", {})
            q.setdefault("special", [])
        try:
            self._load_objective_model()
        except Exception as e:
            print(f"quests: objective model failed: {e!r}")
        try:
            self._load_poi()
        except Exception as e:                    # no areas: every spawn of an objective mob counts, as before
            print(f"quests: quest_poi failed: {e!r}")
        self.done = set()
        if os.path.exists(DONE_FILE):
            self.done = set(json.load(open(DONE_FILE)))
        self._apply_corrections()

    def _apply_corrections(self):
        """polestar's Forever fixes to the 1.12 quest data (polestar_data/tools_lib_data_corrections.json, each with
        its source): a changed prerequisite (465 Nek'rosh's Gambit now follows 98221) and race masks (Skyborne)."""
        fn = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "polestar_data",
                          "tools_lib_data_corrections.json")
        try:
            corr = json.load(open(fn, encoding="utf-8")).get("quests", {})
        except (OSError, ValueError):
            return
        n = 0
        for k, v in corr.items():
            q = self.q.get(int(k)) if str(k).isdigit() else None
            st = (v or {}).get("set") or {}
            if q is None or q.get("server"):
                continue                      # the server's own rows are Forever's already
            pre = st.get("pre") or st.get("preAny")
            if pre:
                q["prev"] = int(pre[0])
                n += 1
            if "races" in st and isinstance(st["races"], int) and st["races"] < (1 << 32):
                q["races"] = st["races"]
                n += 1
        self.corrections = n

    def _add_server_quests(self):
        """Quests this server (TrinityCore forever, tcf_world) has but the 1.12 tables do not: the Forever
        additions such as Archaic Rune / Call of Earth for dwarf shamans. Same dict layout as the 1.12 rows;
        NPC ids are the server's own (what the client puts in the GUID), spawns/loot come from tcf_world too."""
        rows = _tcf("SELECT q.ID, q.LogTitle, COALESCE(c.MinLevel, 1), COALESCE(c.QuestLevel, l.QuestLevel, 1), "
                    "COALESCE(a.PrevQuestID, 0), q.AllowableRaces, COALESCE(a.AllowableClasses, 0), "
                    "COALESCE(a.ExclusiveGroup, 0), COALESCE(a.RequiredSkillID, 0) FROM quest_template q "
                    "LEFT JOIN quest_template_addon a ON a.ID = q.ID LEFT JOIN quest_classic_level c ON c.ID = q.ID "
                    "LEFT JOIN quest_template_classic_level l ON l.ID = q.ID "
                    "WHERE COALESCE(c.MinLevel, 1) <= %d AND (q.ID IN (SELECT quest FROM creature_queststarter) "
                    "OR q.ID IN (SELECT quest FROM creature_questender) "
                    # quests given / taken by an object only (a barrel, a wreck ...) count too
                    "OR q.ID IN (SELECT quest FROM gameobject_queststarter) "
                    "OR q.ID IN (SELECT quest FROM gameobject_questender))" % MAX_QUEST_MIN)
        new = {}
        for r in rows:
            e = int(r[0])
            if e in self.q:
                continue
            races = int(r[5])
            new[e] = dict(id=e, title=r[1], min=int(r[2]), level=int(r[3]), prev=int(r[4]),
                          races=0 if races >= (1 << 63) - 1 or races < 0 else races, classes=int(r[6]),
                          kill=[], items=[], excl=int(r[7]), method=2, skill=int(r[8]), server=True,
                          skill_min=0, src=0, auto=False)          # (set from tcf in _load_objective_model)
        if not new:
            return
        ql = ",".join(map(str, new))
        # the server's objective order (ObjectMgr.cpp L4897 loads quest_objectives ORDER BY QuestID, Order,
        # StorageIndex): 97277 lists its item (Order 0) before its kill (Order 1). _load_objective_model keeps the
        # counter index of each objective in that same order
        for qid, typ, obj in _tcf(f"SELECT QuestID, Type, ObjectID FROM quest_objectives WHERE QuestID IN ({ql}) "
                                  "ORDER BY QuestID, `Order`, StorageIndex"):
            key = {"0": "kill", "1": "items"}.get(typ)
            if key and int(obj) > 0:
                new[int(qid)][key].append(int(obj))
        self.q.update(new)
        for table, rel in (("creature_queststarter", self.starters), ("creature_questender", self.enders)):
            for n, q in _tcf(f"SELECT id, quest FROM {table} WHERE quest IN ({ql})"):
                rel.setdefault(int(q), set()).add(int(n))
        ids = {n for q in new for rel in (self.starters, self.enders) for n in rel.get(q, ())}
        ids |= {m for q in new.values() for m in q["kill"]}
        items = {i for q in new.values() for i in q["items"]}
        if items:
            il = ",".join(map(str, items))
            self._tcf_loot(items)
            ids |= {m for it in items for m in self.item_src.get(it, ())}
            try:
                for it, ge, gx, gy, gz in _tcf(
                        "SELECT l.Item, t.entry, s.position_x, s.position_y, s.position_z FROM gameobject_loot_template l "
                        "JOIN gameobject_template t ON t.type = 3 AND t.Data1 = l.Entry "
                        f"JOIN gameobject s ON s.id = t.entry AND s.map = 0 WHERE l.Item IN ({il})"):
                    self.go_src.setdefault(int(it), []).append((int(ge), (float(gx), float(gy), float(gz))))
            except Exception:
                pass
        ids -= set(self.spawns)
        if ids:
            nl = ",".join(map(str, ids))
            for e, x, y, z in _tcf(f"SELECT id, position_x, position_y, position_z FROM creature WHERE map = 0 AND id IN ({nl})"):
                self.spawns.setdefault(int(e), []).append((float(x), float(y), float(z)))
            for e, n in _tcf(f"SELECT entry, name FROM creature_template WHERE entry IN ({nl})"):
                self.names.setdefault(int(e), n)
            for e, hi in _tcf(f"SELECT entry, level_max FROM creature_classic_level WHERE entry IN ({nl})"):
                self.mob_hi[int(e)] = max(self.mob_hi.get(int(e), 0), int(hi))
        for qid_, go_, x_, y_, z_ in _tcf("SELECT r.quest, g.id, g.position_x, g.position_y, g.position_z "
                                          f"FROM gameobject_questender r JOIN gameobject g ON g.id = r.id "
                                          f"WHERE g.map = 0 AND r.quest IN ({ql})"):
            self.go_enders.setdefault(int(qid_), []).append((int(go_), (float(x_), float(y_), float(z_))))

    def _tcf_loot(self, items):
        """Creature drop chances for these items from the server's loot (tcf_world). -> the items it lists."""
        got = set()
        try:
            rows = _tcf("SELECT d.Entry, l.Item, l.Chance FROM creature_loot_template l "
                        "JOIN creature_template_difficulty d ON d.LootID = l.Entry AND d.DifficultyID = 0 "
                        "WHERE l.Item IN (%s) AND l.Chance > 0" % ",".join(map(str, items)))
        except Exception as e:
            print(f"quests: tcf_world loot failed: {e!r}")
            return got
        for e, it, ch in rows:
            self.item_src.setdefault(int(it), set()).add(int(e))
            self.item_chance[(int(it), int(e))] = float(ch)
            got.add(int(it))
        return got

    def _add_questitem_objects(self):
        """Quest items an object hands out with no loot table of its own (gameobject_questitem): Flintfire's Shipment
        (object 672508, type 3 with Data1 0) gives item 280413 for quest 98321, and the Data1 join above never saw it."""
        items = {i for q in self.q.values() for i in q["items"]}
        if not items:
            return
        for it, ge, gx, gy, gz in _tcf("SELECT q.ItemId, s.id, s.position_x, s.position_y, s.position_z "
                                       "FROM gameobject_questitem q JOIN gameobject s ON s.id = q.GameObjectEntry AND s.map = 0 "
                                       "WHERE q.ItemId IN (%s)" % ",".join(map(str, items))):
            pos = (float(gx), float(gy), float(gz))
            have = self.go_src.setdefault(int(it), [])
            if not any(round(p[0]) == round(pos[0]) and round(p[1]) == round(pos[1]) for _, p in have):
                have.append((int(ge), pos))

    def _load_objective_model(self):
        """Server-side facts about each quest's objectives (QUEST_PLAN.md WP2 #1-#9). Every part is on its own: a
        missing table / column costs only that part."""
        if not self.q:
            return
        ql = ",".join(map(str, self.q))
        # --- server fields (tcf_world runs the game, so they win over the 1.12 row): QuestType 0 = autocomplete
        # (308, 403: never enter the log), StartItem = handed out on accept, RequiredSkillPoints = skill value asked
        provided_n = {}
        try:
            for e, qt, si, sp, pc in _tcf("SELECT q.ID, q.QuestType, q.StartItem, COALESCE(a.RequiredSkillPoints, 0), "
                                          "COALESCE(a.ProvidedItemCount, 0) FROM quest_template q "
                                          f"LEFT JOIN quest_template_addon a ON a.ID = q.ID WHERE q.ID IN ({ql})"):
                q = self.q[int(e)]
                q["auto"], q["src"], q["skill_min"] = int(qt) == 0, int(si), int(sp)
                provided_n[int(e)] = max(1, int(pc))
        except Exception as e:
            print(f"quests: tcf quest fields failed: {e!r}")
        # --- #1/#2 counter index per objective, in the server's order (the client's GetQuestObjectives order, which
        # BotEye packs into the 3 counters). 1.12-only quests keep kill-first (the 1.12 client's own order)
        rows = {}
        try:
            for qid, typ, obj, amt in _tcf("SELECT QuestID, Type, ObjectID, Amount FROM quest_objectives "
                                           f"WHERE QuestID IN ({ql}) ORDER BY QuestID, `Order`, StorageIndex"):
                rows.setdefault(int(qid), []).append((int(typ), int(obj), int(amt)))
        except Exception as e:
            print(f"quests: tcf objective order failed: {e!r}")
        kinds = {0: "kill", 1: "item", 14: "special", 19: "special"}
        for qid, q in self.q.items():
            default = {("kill", m): i for i, m in enumerate(q["kill"])}
            default.update({("item", it): len(q["kill"]) + k for k, it in enumerate(q["items"])})
            srv = rows.get(qid)
            if not srv:
                q["idx"] = default
                continue
            idx = {}
            for i, (typ, obj, _) in enumerate(srv):
                idx.setdefault((kinds.get(typ, f"t{typ}"), obj), i)
                if typ in (14, 19):                  # 14 criteria tree (96608 sit at a fire), 19 areatrigger enter (98319)
                    q["special"].append((typ, obj, i))
            used = set(idx.values())
            for key, i in default.items():           # a 1.12 objective the server rows lack: its old slot, if free
                if key not in idx and i not in used:
                    idx[key] = i
            q["idx"] = idx
        # --- #6 provided items: an item objective the quest itself hands out (417's Pilot's Log, 2160's supplies,
        # 6661's flute, 94465/94467's torch) needs no drop source. Only when the quest gives as many as it asks
        for qid, q in self.q.items():
            src = q.get("src") or 0
            if not src or src not in q["items"]:
                continue
            need = next((a for t, o, a in rows.get(qid, ()) if t == 1 and o == src), 1)
            if need <= provided_n.get(qid, 1):
                q["items"] = [i for i in q["items"] if i != src]
                q["provided"] = [src]
        # --- #3 explore triggers (QUEST_FLAGS_COMPLETION_AREA_TRIGGER 0x4: MiscHandler.cpp L571 calls
        # AreaExploredOrEventHappens when the player enters one of these)
        try:
            rel = set()
            for db in (_tcf, _q):
                for t, qq in db(f"SELECT id, quest FROM areatrigger_involvedrelation WHERE quest IN ({ql})"):
                    rel.add((int(t), int(qq)))
            if rel:
                tl = ",".join(str(t) for t, _ in rel)
                pos = {}
                for t, x, y, z, r in _q("SELECT id, x, y, z, radius FROM areatrigger_template a WHERE map_id = 0 AND "
                                        f"id IN ({tl}) AND build = (SELECT MAX(build) FROM areatrigger_template "
                                        "WHERE id = a.id)"):
                    pos[int(t)] = ((float(x), float(y), float(z)), float(r))
                for t, qq in sorted(rel):
                    if t in pos and t in EXPLORE_ONLY.get(qq, {t}):
                        self.explore.setdefault(qq, []).append((t, pos[t][0], pos[t][1]))
        except Exception as e:
            print(f"quests: explore triggers failed: {e!r}")
        # --- #2 areatrigger-enter objectives (type 19): where the server spawns that trigger (98319's 44117 is added
        # by track S: QUEST_PLAN.md S6). Without a spawn the guide spot in SPECIAL_SPOTS is used
        self.trigger_pos = {}     # areatrigger template id -> ((x, y, z), radius)
        at = {o for q in self.q.values() for t, o, _ in q["special"] if t == 19}
        if at:
            try:
                for t, x, y, z, shape, r in _tcf(
                        "SELECT p.AreaTriggerId, a.PosX, a.PosY, a.PosZ, p.Shape, p.ShapeData0 FROM areatrigger a "
                        "JOIN areatrigger_create_properties p ON p.Id = a.AreaTriggerCreatePropertiesId "
                        "AND p.IsCustom = a.IsCustom WHERE a.MapId = 0 AND p.AreaTriggerId IN (%s)"
                        % ",".join(map(str, at))):
                    rad = float(r) if int(shape) == 0 and float(r) > 0 else 5.0      # 0 = sphere; others: a small ring
                    self.trigger_pos[int(t)] = ((float(x), float(y), float(z)), rad)
            except Exception as e:
                print(f"quests: areatrigger spawns failed: {e!r}")
        # --- #4 object starters (tcf gameobject_queststarter, the 1.12 gameobject_questrelation): 269 -> 403,
        # 270 -> 311, 2059 -> 417
        try:
            seen = set()
            for db, table in ((_tcf, "gameobject_queststarter"), (_q, "gameobject_questrelation")):
                for qq, go, x, y, z in db(f"SELECT r.quest, g.id, g.position_x, g.position_y, g.position_z FROM {table} r "
                                          f"JOIN gameobject g ON g.id = r.id WHERE g.map = 0 AND r.quest IN ({ql})"):
                    k = (int(qq), int(go), round(float(x)), round(float(y)))
                    if k not in seen:
                        seen.add(k)
                        self.go_starters.setdefault(int(qq), []).append((int(go), (float(x), float(y), float(z))))
        except Exception as e:
            print(f"quests: object starters failed: {e!r}")
        # --- #7 vendor sources: objective items with no drop and no object source (384's Rhapsody Malt 2894 and
        # 308's Thunder Ale 2686: Innkeeper Belm 1247 sells both)
        try:
            need = {i for q in self.q.values() for i in q["items"]} - set(self.item_src) - set(self.go_src)
            if need:
                vend = {}
                for n, it in _tcf("SELECT entry, item FROM npc_vendor WHERE item IN (%s)" % ",".join(map(str, need))):
                    vend.setdefault(int(n), set()).add(int(it))
                miss = set(vend) - set(self.spawns)
                if miss:
                    ml = ",".join(map(str, miss))
                    for db in (_q, _tcf):
                        for e, x, y, z in db(f"SELECT id, position_x, position_y, position_z FROM creature "
                                             f"WHERE map = 0 AND id IN ({ml})"):
                            if int(e) in miss:
                                self.spawns.setdefault(int(e), []).append((float(x), float(y), float(z)))
                        miss -= set(self.spawns)
                        if not miss:
                            break
                if vend:
                    for e, nm in _tcf("SELECT entry, name FROM creature_template WHERE entry IN (%s)"
                                      % ",".join(map(str, vend))):
                        self.names.setdefault(int(e), nm)
                for n, its in vend.items():
                    for p in self.spawns.get(n, ()):
                        for it in its:
                            self.vendor_src.setdefault(it, []).append((n, p))
        except Exception as e:
            print(f"quests: vendor sources failed: {e!r}")
        # --- #9 rare / elite marks (creature_template.Classification): Edan the Howler 1137 is a level 9 rare (4)
        try:
            ids = set(self.spawns) | {m for q in self.q.values() for m in q["kill"]}
            ids |= {m for it in self.item_src.values() for m in it}
            if ids:
                for e, c in _tcf("SELECT entry, Classification FROM creature_template WHERE Classification <> 0 "
                                 "AND entry IN (%s)" % ",".join(map(str, ids))):
                    self.cls[int(e)] = int(c)
        except Exception as e:
            print(f"quests: creature classification failed: {e!r}")

    def obj_idx(self, qid, kind, obj):
        """The live-log counter index of one objective ('kill' | 'item' | 'special', id), or None."""
        q = self.q.get(qid)
        if not q:
            return None
        idx = q.get("idx")
        if idx:
            return idx.get((kind, obj))
        # no model (it failed to load): the old kill-first order
        if kind == "kill" and obj in q["kill"]:
            return q["kill"].index(obj)
        if kind == "item" and obj in q["items"]:
            return len(q["kill"]) + q["items"].index(obj)
        return None

    @staticmethod
    def filled(counts, i) -> bool:
        """Is counter i of a quest's live counters full (255)? Unknown indexes (None, past the 3 BotEye sends) are not."""
        return i is not None and i < len(counts or ()) and counts[i] == 255

    def special_errands(self, qid, counts):
        """Plan tuples for the unfilled special objectives of a quest in the log (WP2 #2):
        ('sit', qid, pos, why, secs, idxs) | ('visit', qid, pos, why, radius, idxs)."""
        q = self.q.get(qid)
        if not q or not q.get("special"):
            return []
        todo = [(t, o, i) for t, o, i in q["special"] if not self.filled(counts, i)]
        if not todo:
            return []
        idxs = [i for _, _, i in todo]
        out = []
        spot = SPECIAL_SPOTS.get(qid)
        if spot and spot[0] == "sit":
            out.append(("sit", qid, spot[1], f"sit for {q['title']}", spot[2], idxs))
        else:
            for t, o, i in todo:
                if t != 19:
                    continue                         # a criteria tree we have no spot for: nothing we can do
                pr = self.trigger_pos.get(o) if hasattr(self, "trigger_pos") else None
                if not pr and spot and spot[0] == "visit":
                    pr = (spot[1], spot[2])
                if pr:
                    out.append(("visit", qid, pr[0], f"visit the spot for {q['title']}", pr[1], [i]))
        return out

    def _load_poi(self):
        """Objective areas from the server's quest_poi (map 0): per objective target (creature or item id) its
        polygons. ObjectiveIndex -1 / 32 are the giver / ender marks, not objectives."""
        ql = ",".join(map(str, self.q))
        polys = {}
        for qid, idx, obj, x, y in _tcf("SELECT p.QuestID, p.Idx1, p.QuestObjectID, pp.X, pp.Y FROM quest_poi p "
                                        "JOIN quest_poi_points pp ON pp.QuestID = p.QuestID AND pp.Idx1 = p.Idx1 "
                                        "WHERE p.MapID = 0 AND p.ObjectiveIndex >= 0 AND p.ObjectiveIndex < 32 "
                                        f"AND p.QuestObjectID > 0 AND p.QuestID IN ({ql}) "
                                        "ORDER BY p.QuestID, p.Idx1, pp.Idx2"):
            polys.setdefault((int(qid), int(obj), int(idx)), []).append((float(x), float(y)))
        for (qid, obj, _), pts in polys.items():
            self.poi.setdefault(qid, {}).setdefault(obj, []).append(pts)

    def _rel(self, table):
        out = {}
        for row in _q(f"SELECT id, quest FROM {table}"):
            # a short / garbled row (inst3 04:07: one row came back with one column during a 4-bot restart and the
            # bot died in __init__): skip it, not the whole bot
            if len(row) < 2 or not row[0].strip().isdigit() or not row[1].strip().isdigit():
                continue
            out.setdefault(int(row[1]), set()).add(int(row[0]))
        return out

    def save(self):
        json.dump(sorted(self.done), open(DONE_FILE, "w"))

    # ---------------------------------------------------------------
    def in_area(self, pos):
        return any(math.hypot(pos[0] - a[0], pos[1] - a[1]) < a[2] for a in AREAS)

    def npc_pos(self, npc, near):
        pts = [p for p in self.spawns.get(npc, []) if self.in_area(p)]
        if not pts:
            return None
        return min(pts, key=lambda p: math.hypot(p[0] - near[0], p[1] - near[1]))

    def eligible(self, qid, level, log_ids):
        q = self.q.get(qid)
        if not q or qid in self.done or qid in log_ids or qid in SKIP:
            return False
        # a skill VALUE asked for (we cannot read our skills): 384 names Cooking 185 at value 0, so it passes
        if q["min"] > level or q["level"] > level + 1 or q.get("skill_min", q["skill"]) > 0:
            return False
        if q["races"] and not q["races"] & RACE_MASK:
            return False
        if q["classes"] and not q["classes"] & CLASS_MASK:
            return False
        p = q["prev"]
        if p > 0 and p not in self.done:
            return False
        if p < 0 and -p not in log_ids:
            return False
        # a quest that is some other quest's required predecessor chain: fine.
        # exclusive group: skip if a sibling is done
        if q["excl"] > 0 and any(o["excl"] == q["excl"] and o["id"] in self.done for o in self.q.values()):
            return False
        return True

    # --------------------------------------------------------------- shelving
    def shelve(self, qid, secs, level):
        """Leave a quest's hunt alone for `secs` (or until the next level-up)."""
        self.shelved[qid] = (time.time() + secs, level)

    def skip(self, qid, level):
        """Skip a quest's hunt until the next level-up (all targets indoors ...)."""
        self.skip_q.add(qid)
        self.skip_lvl[qid] = level

    def is_shelved(self, qid) -> bool:
        return qid in self.skip_q or self.shelved.get(qid, (0, 0))[0] > time.time()

    def expire(self, level):
        """Timed-out shelves go; so does every shelve / skip made at a lower level (a level-up can make it doable)."""
        now = time.time()
        for qid, (until, lvl) in list(self.shelved.items()):
            if until <= now or lvl < level:
                del self.shelved[qid]
        for qid, lvl in list(self.skip_lvl.items()):
            if lvl < level:
                self.skip_q.discard(qid)
                del self.skip_lvl[qid]

    # --------------------------------------------------------------- objectives
    def _targets(self, qid):
        """Unfinished objectives -> {mob: {objective creature/item id}}: kill mobs themselves, item droppers."""
        q = self.q.get(qid)
        if not q:
            return {}
        counts = self._counts.get(qid) or ()
        out = {}
        # counter index per objective in the server's order (97277: item first, kill second), _load_objective_model
        for m in q["kill"]:
            if not self.filled(counts, self.obj_idx(qid, "kill", m)):   # a filled kill objective is done
                out.setdefault(m, set()).add(m)
        for it in q["items"]:
            if self.filled(counts, self.obj_idx(qid, "item", it)):
                continue                      # this item is already collected: its droppers are not worth a kill
            src = self.item_src.get(it, set())
            if not src:
                continue
            best = max(self.item_chance.get((it, m), 0.0) for m in src)
            # farm the mobs that actually drop it: a source at 0.01 % next to one at 45 % is not a source
            keep = {m for m in src if self.item_chance.get((it, m), 0.0) >= min(best, max(3.0, best * 0.2))}
            for m in keep or src:
                out.setdefault(m, set()).add(it)
        return out

    def _spawns_for(self, qid, mob, objs):
        """A mob's walkable-area spawns inside its objective's quest_poi area + POI_MARGIN (all of them when the
        server gives no area for that objective)."""
        pts = [p for p in self.spawns.get(mob, []) if self.in_area(p)]
        poi = self.poi.get(qid, {})
        if not objs or any(o not in poi for o in objs):
            return pts                        # some objective of this mob has no area: do not narrow it
        areas = [poly for o in objs for poly in poi[o]]
        return [p for p in pts if any(near_poly(p, poly, POI_MARGIN) for poly in areas)]

    def _usable(self, qid):
        """{mob: spawn points} of the unfinished objectives we would hunt now: mobs above our level+1 and mobs
        with no spawn in the objective area are left out."""
        out = {}
        tg = self._targets(qid)
        too_high = 0
        for m, objs in tg.items():
            if self._level is not None:
                hi = self.mob_hi.get(m, 0)
                # rare / elite (Classification 1, 2, 4) only at or above their level: Edan the Howler 1137 (L9 rare)
                # was a 313 target at level 8
                if hi > self._level + 1 or (hi > self._level and getattr(self, "cls", {}).get(m, 0) in RARE_ELITE):
                    too_high += 1
                    continue
            pts = self._spawns_for(qid, m, objs)
            if pts:
                out[m] = pts
        if tg and too_high == len(tg):
            # diagnostic only (the quest is deferred either way): once per quest and level
            key = (qid, self._level)
            note = getattr(self, "_lvl_note", None)
            if note is not None and key not in note:
                note.add(key)
                try:
                    self.log(f"plan: {qid} {self.q[qid]['title']} all objective mobs above level+1 "
                             f"(or rare/elite above level {self._level})")
                except Exception:
                    pass
        return out

    def objective_mobs(self, qid):
        return set(self._usable(qid))

    def plan(self, me, log_quests, only=None, pickups=True, area=None):
        """Next errand: ('talk', npc, pos, why) | ('hunt', mobs, points, why) | None."""
        log_ids = {q.qid for q in log_quests}
        here = (me.x, me.y)
        errands = []
        self._counts = {q.qid: q.counts for q in log_quests}
        self._level = me.level
        self.expire(me.level)
        if only is not None:                  # the guide asks about particular quests: plan for those only
            log_quests = [q for q in log_quests if q.qid in only]
        # 1. finished quests -> their ender; talk-only quests too
        for lq in log_quests:
            q = self.q.get(lq.qid)
            if not q:
                cu = CUSTOM_ENDERS.get(lq.qid)         # server-made quest: not in the DB, ender found by hand
                if cu and lq.complete:
                    errands.append((math.dist(here, cu[1][:2]), ("talk", cu[0], cu[1], f"hand in {cu[2]}")))
                continue
            # talk-only = no kill / item / special objective and no explore trigger (96608 sits at a fire, 287 also
            # explores Frostmane Hold: neither is a plain hand-in)
            talk_only = not q["kill"] and not q["items"] and not q.get("special") and not self.explore.get(lq.qid)
            if not lq.complete:
                for e in self.special_errands(lq.qid, lq.counts):
                    if e[0] in self.can_do and self.in_area(e[2]):
                        errands.append((math.dist(here, e[2][:2]), e))
                trig = [t_ for t_ in self.explore.get(lq.qid, ()) if self.in_area(t_[1])]
                if trig and "explore" in self.can_do and all(
                        self.filled(lq.counts, self.obj_idx(lq.qid, k, o))
                        for k, objs in (("kill", q["kill"]), ("item", q["items"])) for o in objs):
                    # every counter is full but the quest is not complete: the area is what is left
                    t, pos, r = min(trig, key=lambda t_: math.dist(here, t_[1][:2]))
                    errands.append((math.dist(here, pos[:2]),
                                    ("explore", lq.qid, pos, f"explore the area for {q['title']}", r, t)))
            if lq.complete or talk_only:
                for npc in self.enders.get(lq.qid, ()):
                    p = self.npc_pos(npc, here)
                    if p:
                        errands.append((math.dist(here, p[:2]), ("talk", npc, p, f"hand in {q['title']}")))
                if lq.complete:
                    for go, pos in self.go_enders.get(lq.qid, ()):
                        if go not in self.skip_go and self.in_area(pos):
                            errands.append((math.dist(here, pos[:2]), ("use", go, pos, f"hand in {q['title']} at an object")))
        # 2. new quests - but finish what we hold first: with 5 hunt quests open, no more pick-ups
        open_hunts = [lq for lq in log_quests if lq.qid in self.q and not lq.complete and
                      (self.q[lq.qid]["kill"] or self.q[lq.qid]["items"]) and self._hunt_pts(lq.qid)]
        for qid, npcs in (self.starters.items() if pickups and len(open_hunts) < 5 else ()):
            if not self.eligible(qid, me.level, log_ids):
                continue
            for npc in npcs:
                p = self.npc_pos(npc, here)
                if p and math.dist(here, p[:2]) < 900:
                    errands.append((math.dist(here, p[:2]) + 30, ("talk", npc, p, f"pick up {self.q[qid]['title']}")))
        # quests only an object gives (wreck 2059 -> 417). Not autocomplete ones (QuestType 0: 403 at barrel 269):
        # they never enter the log, so use_object could not tell it worked and would use the object again and again
        for qid, gos in (self.go_starters.items() if pickups and len(open_hunts) < 5 else ()):
            if self.starters.get(qid) or self.q.get(qid, {}).get("auto") or not self.eligible(qid, me.level, log_ids):
                continue
            for go, pos in gos:
                if go not in self.skip_go and self.in_area(pos) and math.dist(here, pos[:2]) < 900:
                    errands.append((math.dist(here, pos[:2]) + 30,
                                    ("use", go, pos, f"pick up {self.q[qid]['title']} at an object")))
        if errands:
            return min(errands, key=lambda e: e[0])[1]
        # 3. hunt the objectives of ONE quest until it is done (sticky focus), nearest first
        # 2b. quest items that only chests give: walk to the nearest chest we have not looted lately
        now = time.time()
        chests = []
        for lq in log_quests:
            q = self.q.get(lq.qid)
            if not q or lq.complete or self.is_shelved(lq.qid):
                continue
            for it in q["items"]:
                if self.filled(lq.counts, self.obj_idx(lq.qid, "item", it)):
                    continue                  # this objective is already done (the strip reads 255 for a filled one)
                for go, pos in self.go_src.get(it, ()):
                    key = (lq.qid, go, round(pos[0]), round(pos[1]))
                    if self.skip_gather.get(key, 0) > now or self.gathered.get(key, 0) > now - 600:
                        continue
                    if self.in_area(pos):
                        chests.append((math.dist(here, pos[:2]),
                                       ("gather", go, pos, f"loot a chest for {q['title']}", lq.qid, key)))
        if chests:
            return min(chests, key=lambda c: c[0])[1]
        focus = self.focus
        cands = []
        for lq in log_quests:
            if lq.complete or lq.qid not in self.q or self.is_shelved(lq.qid):
                continue
            pts = self._hunt_pts(lq.qid)
            if not pts:
                continue
            d = min(math.dist(here, p[:2]) for p in pts)
            cands.append((d, lq.qid, pts))
        if not cands:
            return None
        def too_hard(qid):
            mobs = self.objective_mobs(qid)
            return bool(mobs) and min(self.mob_hi.get(m, 0) for m in mobs) > me.level + 1
        easy = [c for c in cands if not too_hard(c[1])]
        pool = easy or cands                 # only when nothing easy is left do we try a hard one
        pick = next((c for c in pool if c[1] == focus), None) or min(pool, key=lambda c: c[0])
        self.focus = pick[1]
        mobs = self.objective_mobs(pick[1])
        pts = pick[2]
        if area:
            pts = [p for p in pts if area(p)] or pts
        return ("hunt", mobs, pts, f"objectives of {self.q[pick[1]]['title']}")

    def _hunt_pts(self, qid):
        # one spot per position: a server spawn with several entries (1161/1162 troggs) lists the same point twice
        return list({(round(p[0]), round(p[1])): p for pts in self._usable(qid).values() for p in pts}.values())


def near_poly(p, poly, margin):
    """Is point p inside the polygon, or within `margin` yd of its outline (1- and 2-point areas too)?"""
    x, y = p[0], p[1]
    n = len(poly)
    if n >= 3:
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        if inside:
            return True
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L))
        if math.hypot(x - (ax + t * dx), y - (ay + t * dy)) <= margin:
            return True
    return False
