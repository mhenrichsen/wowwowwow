"""Audit the RestedXP Survival guide chain for one race and class.

    python guide_audit.py [--race Dwarf] [--class Warrior] [--paths]

For every guide of the chain (rxp.chain, following #next) it simulates the
quests the character will have handed in by then and reports each .accept
that will not work: race or class locked, above the guide's level, or a
prerequisite the chain never completes. It also notes quests the world DB
types as dungeon / elite / escort that the guide did not tag - those are
attempted - and checks every .fly against the taxi nodes the bot knows.

--paths also checks the navmesh: every NPC the guide talks to and every
quest giver / ender gets "polygon under it?" and "path from the previous
one?". Kalimdor NPCs are only checked when map 1 tiles exist.
"""
from __future__ import annotations
import math, os, re, sys
import rxp
import worlddb

HERE = os.path.dirname(os.path.abspath(__file__))
# quest_template.RequiredRaces / RequiredClasses bits
RACE_BIT = {"Human": 1, "Orc": 2, "Dwarf": 4, "NightElf": 8, "Scourge": 16,
            "Tauren": 32, "Gnome": 64, "Troll": 128}
CLASS_BIT = {"Warrior": 1, "Paladin": 2, "Hunter": 4, "Rogue": 8, "Priest": 16,
             "Shaman": 64, "Mage": 128, "Warlock": 256, "Druid": 1024}
_qcache: dict[int, dict] = {}


def qinfo(qid: int) -> dict | None:
    if qid in _qcache:
        return _qcache[qid]
    rows = worlddb._query(
        "SELECT entry, Title, MinLevel, QuestLevel, RequiredRaces, RequiredClasses, PrevQuestId, NextQuestId, "
        "ExclusiveGroup, Type, SpecialFlags, QuestFlags, "
        "ReqCreatureOrGOId1, ReqCreatureOrGOCount1, ReqCreatureOrGOId2, ReqCreatureOrGOCount2, "
        "ReqItemId1, ReqItemCount1, ReqItemId2, ReqItemCount2, SrcItemId, "
        "(SELECT GROUP_CONCAT(id) FROM creature_questrelation r WHERE r.quest=q.entry), "
        "(SELECT GROUP_CONCAT(id) FROM creature_involvedrelation r WHERE r.quest=q.entry), "
        "(SELECT GROUP_CONCAT(id) FROM gameobject_questrelation r WHERE r.quest=q.entry), "
        "(SELECT GROUP_CONCAT(id) FROM gameobject_involvedrelation r WHERE r.quest=q.entry) "
        f"FROM quest_template q WHERE entry={qid} ORDER BY patch DESC LIMIT 1")
    if not rows:
        _qcache[qid] = None
        return None
    r = rows[0]
    ids = lambda v: [int(x) for x in v.split(",")] if v not in ("NULL", "", None) else []
    q = {"id": int(r[0]), "title": r[1], "min_level": int(r[2]), "level": int(r[3]), "races": int(r[4]),
         "classes": int(r[5]), "prev": int(r[6]), "next": int(r[7]), "excl": int(r[8]), "type": int(r[9]),
         "special": int(r[10]), "flags": int(r[11]),
         "kill": [(int(r[12]), int(r[13])), (int(r[14]), int(r[15]))],
         "items": [(int(r[16]), int(r[17])), (int(r[18]), int(r[19]))], "src_item": int(r[20]),
         "givers": ids(r[21]), "enders": ids(r[22]), "giver_objs": ids(r[23]), "ender_objs": ids(r[24])}
    q["kill"] = [k for k in q["kill"] if k[0]]
    q["items"] = [k for k in q["items"] if k[0]]
    _qcache[qid] = q
    return q


def quest_ok(q, who) -> str | None:
    if q["races"] and not q["races"] & RACE_BIT.get(who.race, 0):
        return "race-locked"
    if q["classes"] and not q["classes"] & CLASS_BIT.get(who.cls, 0):
        return "class-locked"
    if q["type"] == 81:
        return "dungeon"
    if q["type"] == 1:
        return "elite/group"
    if q["type"] == 41:
        return "pvp"
    if q["type"] == 84:
        return "escort"
    if q["type"] == 62:
        return "raid"
    if q["special"] & 2 and any(c > 0 for c, _ in q["kill"]) and not q["items"]:
        return "escort/event"
    return None


def describe(q) -> str:
    parts = []
    for c, n in q["kill"]:
        parts.append(f"{'Kill' if c > 0 else 'Use'} {n} {worlddb.name(c) if c > 0 else 'object %d' % -c}")
    for i, n in q["items"]:
        parts.append(f"Collect {n} item {i}")
    return "; ".join(parts) or "deliver"


def steps_for(name, who):
    g = rxp.get(name, who)
    rxp.resolve_npcs(g.steps)
    return g, g.steps


def guide_levels(name) -> tuple[int, int]:
    m = re.match(r"\s*(\d+)-(\d+)", name)
    return (int(m.group(1)), int(m.group(2))) if m else (1, 60)


def audit(who, paths=False):
    done: set[int] = set()
    active: set[int] = set()
    report = []
    nav = None
    if paths:
        import navmesh
        navmesh.TIME_BUDGET = None
        nav = {0: navmesh.NavMesh()}
        try:
            nav[369] = navmesh.NavMesh(map_id=369)
        except Exception:
            pass
        try:
            nav[1] = navmesh.NavMesh(map_id=1)
        except Exception as e:
            print("no Kalimdor mesh:", e)
    for name in rxp.chain(who):
        g, steps = steps_for(name, who)
        lvl, lvl_end = guide_levels(name)
        issues = []
        flights = set()
        for i, st in enumerate(steps):
            for tag, args in st.actions:
                if tag == "F":
                    flights.add(args.strip())
                if tag not in ("QA", "QT", "QC"):
                    continue
                qid = int(re.match(r"\s*(\d+)", args).group(1))
                q = qinfo(qid)
                if q is None:
                    issues.append(f"step {i+1}: quest {qid} not in the DB"); continue
                if tag == "QT":
                    done.add(qid); active.discard(qid); continue
                if tag == "QC":
                    continue
                why = quest_ok(q, who)
                if why in ("race-locked", "class-locked"):
                    issues.append(f"step {i+1}: [QA{qid}] {q['title']}: {why} - the NPC will not offer it")
                    continue
                if why:
                    # The DB type is a poor guide: "Return to Ironforge" is a
                    # delivery typed Group, "Tinkmaster Overspark" a talk quest
                    # typed Dungeon. The guide's own .dungeon / .group tags are
                    # the curated signal and already drop the real ones. What
                    # is left is attempted - and an elite kill target is never
                    # hunted (questing.nearest_spawn), so it just fails.
                    issues.append(f"step {i+1}: [QA{qid}] {q['title']}: DB type {why}, "
                                  f"not tagged in the guide - attempted")
                if q["min_level"] > lvl_end + 1:
                    issues.append(f"step {i+1}: [QA{qid}] {q['title']}: needs level {q['min_level']} (guide starts at {lvl})")
                # prerequisite chain
                chain, p, seen = [], q["prev"], set()
                while p and p not in seen:
                    seen.add(p)
                    if p < 0:
                        if -p not in active and -p not in done:
                            chain.append(("active", -p))
                        break
                    if p in done:
                        break
                    pq = qinfo(p)
                    if pq is None:
                        break
                    chain.append(("done", p))
                    p = pq["prev"]
                if chain:
                    # either way the prerequisite is never met, so the bot's
                    # accept() finds the quest unavailable and skips it
                    chain.reverse()
                    for kind, pid in chain:
                        pq = qinfo(pid)
                        bad = quest_ok(pq, who) if pq else "unknown"
                        if bad not in ("race-locked", "class-locked", "unknown"):
                            bad = None           # a type flag: attempted like any other
                        if bad or not (pq["givers"] or pq["giver_objs"]):
                            issues.append(f"step {i+1}: [QA{qid}] {q['title']}: needs {pid} '{pq['title'] if pq else '?'}' first - {bad or 'no NPC giver'}")
                            break
                    else:
                        issues.append(f"step {i+1}: [QA{qid}] {q['title']}: needs "
                                      f"{[p for _, p in chain]} first, which the chain never does "
                                      f"- the bot skips it")
                    continue
                active.add(qid)
        # flights
        import bridge
        for f in sorted(flights):
            if not any(k.lower() in f.lower() or f.lower() in k.lower() for k in bridge.TAXI_NODES):
                issues.append(f"flight to '{f}': no such taxi node in bridge.TAXI_NODES")
        # pathing
        if nav is not None:
            issues += path_check(steps, nav)
        report.append(f"## {name} ({len(steps)} steps for a {who.race} {who.cls})")
        report += ["- " + x for x in issues] or ["- ok"]
        print("\n".join(report[-len(issues) - 2:]), flush=True)
    open(os.path.join(HERE, "audit_report.md"), "w", encoding="utf-8").write("\n".join(report) + "\n")
    return report


_spawn_cache: dict[int, list] = {}


def spawns(entry):
    if entry not in _spawn_cache:
        rows = worlddb._query(f"SELECT map, position_x, position_y, position_z FROM creature WHERE id={entry}")
        _spawn_cache[entry] = [(int(m), float(x), float(y), float(z)) for m, x, y, z in rows]
    return _spawn_cache[entry]


def taxi_landings():
    """(map, x, y, z) of every Alliance taxi node the bot knows."""
    import struct, bridge
    dbc = os.path.join(os.path.dirname(HERE), "wow-forever-server", "server", "data", "5875", "dbc", "TaxiNodes.dbc")
    d = open(dbc, "rb").read()
    _, n, _, rs, _ = struct.unpack_from("<4sIIII", d, 0)
    by_id = {}
    for i in range(n):
        nid, m, x, y, z = struct.unpack_from("<IIfff", d, 20 + i * rs)
        by_id[nid] = (m, x, y, z)
    return [by_id[i] for i, _ in bridge.TAXI_NODES.values() if i in by_id]


LEG_MAX = 2500.0          # longer legs are flown / trammed: check from the nearest flight point instead
TRAM_SW = (-8354.23, 524.07, 91.8)
TRAM_IF = (-4839.51, -1317.74, 501.87)


def path_check(steps, nav) -> list[str]:
    """Walk the guide's NPCs in order, leg by leg, the way the bot does.

    Each leg starts where the previous one ended (same map). A leg longer
    than LEG_MAX yd is what the bot flies or rides the tram for, so it is
    checked from the nearest flight point (or the tram landing) on the
    target's side instead. Cross-map legs are boats/portals - not checked.
    """
    out = []
    npcs = []
    for st in steps:
        for n in st.npcs:
            npcs.append(n)
        for tag, args in st.actions:
            if tag in ("QA", "QT"):
                q = qinfo(int(re.match(r"\s*(\d+)", args).group(1)))
                if q:
                    npcs += q["givers"] if tag == "QA" else q["enders"]
    landings = taxi_landings()
    seen, cur = set(), None            # cur = (map, x, y, z) where the bot stands
    for n in npcs:
        if n in seen:
            continue
        seen.add(n)
        sp = spawns(n)
        if not sp:
            out.append(f"path: NPC {n} {worlddb.name(n)} has no spawn"); continue
        if cur is not None and any(s[0] == cur[0] for s in sp):
            best = min((s for s in sp if s[0] == cur[0]), key=lambda s: math.dist(s[1:3], cur[1:3]))
        else:
            best = sp[0]
        m = best[0]
        if m not in nav:
            out.append(f"path: {worlddb.name(n)} ({n}) is on map {m} - no mesh yet"); cur = best; continue
        start = None
        if cur is not None and cur[0] == m:
            d = math.dist(cur[1:3], best[1:3])
            if m == 0 and (cur[1] < -7300) != (best[1] < -7300):
                start = TRAM_IF if best[1] > -7300 else TRAM_SW
                if math.dist(start[:2], best[1:3]) > LEG_MAX:
                    start = None                 # tram, then a flight
            elif d <= LEG_MAX:
                start = cur[1:]
        if start is None:
            near = [l for l in landings if l[0] == m]
            if near:
                l = min(near, key=lambda l: math.dist(l[1:3], best[1:3]))
                if math.dist(l[1:3], best[1:3]) <= LEG_MAX * 2:
                    start = l[1:]
        poly, _ = nav[m].nearest_poly(best[1], best[2], best[3], max_dist=3.0)
        where = f"{worlddb.name(n)} ({n}) at ({best[1]:.0f},{best[2]:.0f},{best[3]:.0f})"
        if poly is None:
            alt = nav[m].reachable_near(start, best[1:], radius=12.0) if start else None
            out.append(f"path: {where} has no polygon under it"
                       + (f" - reachable spot {math.dist(alt[:2], best[1:3]):.1f} yd away" if alt else " - NOT REACHABLE"))
            if alt:
                cur = (m,) + tuple(alt)      # an off-mesh NPC must not become the next leg's start
            continue
        if start is not None and math.dist(start[:2], best[1:3]) > 3.0:
            if not nav[m].find_path(start, best[1:], max_nodes=1500000):
                out.append(f"path: {where}: no path from ({start[0]:.0f},{start[1]:.0f}), "
                           f"{math.dist(start[:2], best[1:3]):.0f} yd away")
                continue                 # the next leg starts from the last spot we did reach
        cur = best
    return out


if __name__ == "__main__":
    def opt(flag, default):
        return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default
    audit(rxp.Who(opt("--class", "Warrior"), opt("--race", "Dwarf"),
                  "Horde" if opt("--race", "Dwarf") in ("Orc", "Scourge", "Tauren", "Troll")
                  else "Alliance"),
          paths="--paths" in sys.argv)
