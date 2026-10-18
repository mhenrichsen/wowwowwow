"""Read-only dead-time detector: finds time the bot wastes without knowing it.

Samples the game every 0.25 s and prints each stretch of
  STILL      2+ s standing still outside combat, not sitting/dead/casting
  NO-DAMAGE  in combat with a mob in melee range on us while no hostile
             near us loses health - 4+ s before the first hit, 8+ s after - and we are not casting - auto-attack off,
             a target the server never heard of, swinging at air (the slow
             "attacked back" starts of 2026-09-24: 15 of 30 fights a mob
             started stood 6-11 s like this and no bot log line said so)
with the bot state and the log lines written during it. Every report adds to
running totals, so a bug shows up as a growing number, not an anecdote.

    py -3 idle_probe.py >> idle.log     (runs next to the bot, never writes memory)
"""
import json, math, time
import offsets as O
from wowmem import Process
from game import Game

p = Process(writable=False)
g = Game(p)
NO_DAMAGE_FIRST = 4.0      # before the first hit on the mobs on us: one swing
NO_DAMAGE_GAP = 8.0        # after it: a 3.6 s 2-hander plus a miss or two
STILL_S = 2.0
totals = {"STILL": [0, 0.0], "NO-DAMAGE": [0, 0.0]}


def status():
    try:
        return json.load(open("status.json", encoding="utf-8"))
    except Exception:
        return {}


def casting(me) -> bool:
    try:
        return bool(p.u32(me.base + O.UNIT_CASTING_SPELL))
    except Exception:
        return False


def report(kind, secs, st0, seen, extra=""):
    st = status()
    new = [l for l in st.get("log", []) if l not in seen]
    totals[kind][0] += 1
    totals[kind][1] += secs
    tot = ", ".join(f"{k} {n}x {s / 60:.1f} min" for k, (n, s) in totals.items())
    print(f"{time.strftime('%H:%M:%S')} {kind} {secs:4.1f}s state={st0.get('state')}"
          f"/{str(st0.get('detail', ''))[:40]} -> {st.get('state')}{extra} [{tot}]"
          f" | before: {seen[-1][9:110] if seen else ''}"
          + "".join(f"\n      during: {l[:110]}" for l in new[-4:]), flush=True)


still_since = nd_since = None
nd_hit = False
pos = None
st_still = st_nd = {}
seen_still = seen_nd = []
hp_seen = {}
hit_once = set()           # mobs we have damaged at least once
while True:
    try:
        me = g.player()
        units = g.units()
    except Exception:
        time.sleep(1)
        continue
    now = time.time()

    # --- standing still outside combat
    busy = me.in_combat or me.sitting or me.dead or me.ghost or casting(me)
    moved = pos is None or math.dist(pos, (me.x, me.y)) > 0.3
    if moved or busy:
        if still_since and now - still_since >= STILL_S:
            report("STILL", now - still_since, st_still, seen_still)
        still_since = None
        pos = (me.x, me.y)
    if not busy and still_since is None:
        still_since, st_still = now, status()
        seen_still = list(st_still.get("log", []))

    # --- in combat, in melee, and nothing is getting hurt
    near = [u for u in units if not u.dead and me.dist(u) < 8.0 and u.target == me.guid]
    dropped = False
    for u in near:
        prev = hp_seen.get(u.guid)
        if prev is not None and u.health < prev:
            dropped = True
            hit_once.add(u.guid)
        hp_seen[u.guid] = u.health
    idle_fight = (me.in_combat and not me.dead and not me.sitting and not casting(me)
                  and any(me.dist(u) <= 5.0 for u in near) and not dropped)
    if idle_fight:
        if nd_since is None:
            nd_hit = any(u.guid in hit_once for u in near)
            nd_since, st_nd = now, status()
            seen_nd = list(st_nd.get("log", []))
    else:
        limit = NO_DAMAGE_GAP if nd_hit else NO_DAMAGE_FIRST
        if nd_since and now - nd_since >= limit:
            report("NO-DAMAGE", now - nd_since, st_nd, seen_nd,
                   f" ({len(near)} on us, server target "
                   f"{'a mob on us' if me.target in {u.guid for u in near} else hex(me.target)})")
        nd_since = None
    time.sleep(0.25)
