"""Global database of NPC services: vendors, repairers, trainers, innkeepers...

Built once from the world DB into data/npcs.json (every spawn on every map),
then queried at runtime: "nearest repairer friendly to me on this map",
"nearest mage trainer that still has something to teach me", and so on.

    python services.py build           # (re)generate data/npcs.json
    python services.py near vendor     # list services near the character
"""

from __future__ import annotations

import json
import math
import time
import os
import sys

import factions
import worlddb

HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(HERE, "data", "npcs.json")

# UNIT_NPC_FLAG_* (core/src/game/Objects/UnitDefines.h)
FLAGS = {
    "gossip": 0x1, "questgiver": 0x2, "vendor": 0x4, "flightmaster": 0x8,
    "trainer": 0x10, "spirithealer": 0x20, "innkeeper": 0x80, "banker": 0x100,
    "auctioneer": 0x1000, "stablemaster": 0x2000, "repair": 0x4000,
}
SERVICE_MASK = (FLAGS["vendor"] | FLAGS["flightmaster"] | FLAGS["trainer"] | FLAGS["innkeeper"]
                | FLAGS["banker"] | FLAGS["auctioneer"] | FLAGS["repair"] | FLAGS["spirithealer"])
TRAINER_TYPES = {0: "class", 1: "mount", 2: "tradeskill", 3: "pet"}
CLASSES = {1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest",
           7: "shaman", 8: "mage", 9: "warlock", 11: "druid"}


# the Forever server (TrinityCore, modern flag values): tcf_world.creature_template.npcflag
TCF_FLAGS = {
    "gossip": 0x1, "questgiver": 0x2, "trainer": 0x10, "vendor": 0x80, "repair": 0x1000,
    "flightmaster": 0x2000, "spirithealer": 0x4000, "innkeeper": 0x10000, "banker": 0x20000,
    "auctioneer": 0x200000, "stablemaster": 0x400000,
}
TCF_SERVICES = ("vendor", "flightmaster", "trainer", "innkeeper", "banker", "auctioneer", "repair", "spirithealer")


def build_tcf() -> str:
    """data/npcs.json from the Forever server's own world DB (tcf_world): the spawns this server really has.
    Until 2026-10-15 the file came from the 1.12 VMaNGOS DB (build()), whose positions this server moved in places."""
    import worlddb as w
    mask = 0
    for k in TCF_SERVICES:
        mask |= TCF_FLAGS[k]
    rows = w._query_db(
        "tcf_world",
        "SELECT c.guid, c.map, c.position_x, c.position_y, c.position_z, t.entry, t.name, t.subname, "
        "IF(c.npcflag IS NULL OR c.npcflag = 0, t.npcflag, c.npcflag), t.faction, t.trainer_class, "
        "IFNULL(tr.Type, -1), IFNULL(ct.TrainerID, 0) "
        "FROM creature c JOIN creature_template t ON t.entry = c.id "
        "LEFT JOIN creature_trainer ct ON ct.CreatureID = t.entry LEFT JOIN trainer tr ON tr.Id = ct.TrainerID "
        f"WHERE (IF(c.npcflag IS NULL OR c.npcflag = 0, t.npcflag, c.npcflag) & {mask}) <> 0 "
        "AND t.name NOT LIKE '[%'")
    ev = {int(g): int(e) for e, g in w._query_db("tcf_world", "SELECT eventEntry, guid FROM game_event_creature")}
    npcs, seen = [], set()
    for guid, m, x, y, z, entry, name, sub, flags, fac, tclass, ttype, tid in rows:
        if int(guid) in seen:
            continue
        seen.add(int(guid))
        flags = int(flags)
        n = {"guid": int(guid), "entry": int(entry), "name": name,
             "title": "" if sub in ("NULL", "") else sub,
             "map": int(m), "pos": [round(float(x), 2), round(float(y), 2), round(float(z), 2)],
             "faction": int(fac),
             "services": [k for k in TCF_SERVICES if flags & TCF_FLAGS[k]]}
        if int(guid) in ev:
            n["event"] = ev[int(guid)]
        if flags & TCF_FLAGS["trainer"]:
            tt = int(ttype)
            n["trainer"] = {"type": TRAINER_TYPES.get(tt, "class" if int(tclass) else str(tt)),
                            "class": CLASSES.get(int(tclass)), "template": int(tid)}
        npcs.append(n)
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    tmp = DB_FILE + ".tmp"
    json.dump({"source": "tcf_world (Forever server, TrinityCore)", "npcs": npcs}, open(tmp, "w", encoding="utf-8"),
              indent=1)
    os.replace(tmp, DB_FILE)
    return DB_FILE


def build() -> str:
    rows = worlddb._query(
        "SELECT c.guid, c.map, c.position_x, c.position_y, c.position_z, t.entry, t.name, "
        "t.subname, t.npc_flags, t.faction, t.trainer_type, t.trainer_class, t.trainer_id "
        "FROM creature c JOIN creature_template t ON t.entry = c.id AND t.patch = "
        "(SELECT MAX(patch) FROM creature_template WHERE entry = c.id) "
        f"WHERE (t.npc_flags & {SERVICE_MASK}) <> 0 AND c.patch_min <= 10 AND c.patch_max >= 10")
    # spawns that belong to a world event (gec.event > 0: only while it runs;
    # < 0: gone while it runs). Wagner Hammerstrike, a Harvest Festival
    # vendor, was picked as the nearest vendor in September and the bot
    # walked 473 yd up a hill to an empty spot (13:10).
    ev = {int(g): int(e) for g, e in worlddb._query("SELECT guid, event FROM game_event_creature")}
    npcs = []
    for guid, m, x, y, z, entry, name, sub, flags, fac, ttype, tclass, tid in rows:
        flags = int(flags)
        n = {"guid": int(guid), "entry": int(entry), "name": name,
             "title": "" if sub in ("NULL", "") else sub,
             "map": int(m), "pos": [round(float(x), 2), round(float(y), 2), round(float(z), 2)],
             "faction": int(fac),
             "services": [k for k, f in FLAGS.items() if flags & f and f & SERVICE_MASK]}
        if int(guid) in ev:
            n["event"] = ev[int(guid)]
        if flags & FLAGS["trainer"]:
            n["trainer"] = {"type": TRAINER_TYPES.get(int(ttype), str(ttype)),
                            "class": CLASSES.get(int(tclass)), "template": int(tid)}
        npcs.append(n)
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    json.dump({"source": "VMaNGOS world DB, build 5875", "npcs": npcs},
              open(DB_FILE, "w", encoding="utf-8"), indent=1)
    return DB_FILE


_db: list[dict] | None = None


def all_npcs() -> list[dict]:
    global _db
    if _db is None:
        if not os.path.exists(DB_FILE):
            build()
        _db = json.load(open(DB_FILE, encoding="utf-8"))["npcs"]
    return _db


_events: dict | None = None


def event_active(event: int, now: float | None = None) -> bool:
    """Is world event `event` running now? From game_event's own schedule:
    it starts at start_time and repeats every `occurence` minutes for
    `length` minutes."""
    global _events
    if _events is None:
        _events = {}
        try:
            import calendar
            for e, st, occ, ln in worlddb._query(
                    "SELECT entry, start_time, occurence, length FROM game_event"):
                t0 = calendar.timegm(time.strptime(st, "%Y-%m-%d %H:%M:%S"))
                _events[int(e)] = (t0, int(occ) * 60, int(ln) * 60)
        except Exception:
            pass
    ev = _events.get(abs(event))
    if not ev:
        return False
    now = time.time() if now is None else now
    t0, occ, ln = ev
    if now < t0:
        return False
    return occ <= 0 or (now - t0) % occ < ln


def present(n: dict) -> bool:
    """Is this spawn in the world right now (event spawns only while their
    event runs; negative events remove a spawn while they run)."""
    e = n.get("event")
    if not e:
        return True
    on = event_active(e)
    return on if e > 0 else not on


def find(service: str, map_id: int, pos, player_faction: int, *,
         trainer_class: str | None = None, trainer_type: str | None = None,
         where=None, limit: int = 5) -> list[dict]:
    """Nearest NPCs offering `service`, friendly to the player, on this map."""
    out = []
    for n in all_npcs():
        if n["map"] != map_id or service not in n["services"]:
            continue
        if not factions.is_friendly(player_faction, n["faction"]):
            continue
        t = n.get("trainer")
        if trainer_class and (not t or t["class"] != trainer_class):
            continue
        if trainer_type and (not t or t["type"] != trainer_type):
            continue
        if where and not where(n):
            continue
        if not present(n):
            continue
        out.append(n)
    out.sort(key=lambda n: math.dist(n["pos"][:2], pos[:2]))
    return out[:limit]


if __name__ == "__main__":
    if sys.argv[1:2] in (["build"], ["build_tcf"], ["build_mangos"]):
        path = build() if sys.argv[1:2] == ["build_mangos"] else build_tcf()
        db = all_npcs()
        from collections import Counter
        c = Counter(s for n in db for s in n["services"])
        print(f"wrote {path}: {len(db)} NPC spawns - " +
              ", ".join(f"{k} {v}" for k, v in c.most_common()))
    elif sys.argv[1:2] == ["near"]:
        from game import Game
        from wowmem import Process
        with Process() as p:
            me = Game(p).player()
        kind = sys.argv[2] if len(sys.argv) > 2 else "vendor"
        for n in find(kind, 0, (me.x, me.y), me.faction,
                      trainer_class=sys.argv[3] if len(sys.argv) > 3 else None, limit=8):
            print(f"  {math.dist(n['pos'][:2], (me.x, me.y)):6.0f} yd  {n['name']:<24} "
                  f"<{n['title']}>  {','.join(n['services'])}")
    else:
        print(__doc__)
