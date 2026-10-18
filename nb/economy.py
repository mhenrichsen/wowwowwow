"""Combat economy for the nb bot: xp per kill, the mana a pull needs, which spawns are alive.

Pure helpers (xp_for, spawn_xp, mana_line, point_score: nb/test_economy.py checks them offline) plus the server's
own spawn data: tcf_world.creature (+ creature_classic_level, the levels the server really spawns) and
tcf_characters.respawn (a row per dead creature spawn until it respawns; spawns not listed are alive).
"""

from __future__ import annotations

import math
import threading
import time
from collections import namedtuple

import worlddb

RUN_SPEED = 7.0          # yd/s on foot: path time to a hunt point
POINT_OVERHEAD = 25.0    # s per kill besides the walk (pull, fight, loot): xp / (path time + this)
RESPAWN_TTL = 20.0       # s: the respawn table is re-read at most this often (a ~0.3 s query, run in a thread)

Spawn = namedtuple("Spawn", "guid entry x y z lo hi rank faction name respawn")


# ------------------------------------------------------------ pure
def xp_for(pl: int, ml: int, elite: bool = False) -> int:
    """Classic (1.12) xp for killing a level `ml` mob at level `pl` (Formulas::BaseGain): (pl*5 + 45) scaled
    +5% per level above us (at most 4), down to 0 at the gray level below us. Checked against the log: level 7
    killing level 3 / 4 mobs got 16 / 32 xp against 80 for a level 7 one (inst1, 2026-10-14)."""
    if pl <= 0 or ml <= 0:
        return 0
    base = pl * 5 + 45
    if ml >= pl:
        xp = base * (1 + 0.05 * min(ml - pl, 4))
    else:
        from nb.threat import gray_level         # the one gray formula (Trinity::XP::GetGrayLevel)
        if ml <= gray_level(pl):
            return 0
        zd = (5 if pl < 8 else 6 if pl < 10 else 7 if pl < 12 else 8 if pl < 16 else 9 if pl < 20 else
              11 if pl < 30 else 12 if pl < 40 else 13 if pl < 45 else 14 if pl < 50 else 15 if pl < 55 else
              16 if pl < 60 else 17)
        xp = base * (1 - (pl - ml) / zd)
    return int(round(xp * (2 if elite else 1)))      # round: 80 * (1 - 4/5) is 15.999.. in floats, the server gave 16


def spawn_xp(pl: int, lo: int, hi: int, top: int | None = None) -> float:
    """Expected xp of a spawn that comes up at a random level lo..hi; levels above `top` (default: our level,
    what valid_plate pulls) count 0, we walk past them."""
    top = pl if top is None else top
    lv = range(max(1, lo), max(lo, hi) + 1)
    return sum(xp_for(pl, l) if l <= top else 0 for l in lv) / max(1, len(lv))


def mana_line(costs, default: float, margin: float = 15.0, lo: float = 40.0, hi: float = 90.0,
              min_n: int = 5) -> tuple[float, int]:
    """The mana % a pull should start with: p90 of the measured per-fight cost + margin, clamped to lo..hi.
    Fewer than min_n samples: the default. -> (line, samples used)."""
    c = sorted(float(x) for x in costs)
    if len(c) < min_n:
        return float(default), len(c)
    p90 = c[min(len(c) - 1, max(0, math.ceil(0.9 * len(c)) - 1))]
    return float(min(hi, max(lo, p90 + margin))), len(c)


def point_score(xp: float, dist: float, respawn_left: float = 0.0) -> float:
    """xp per second of a trip to a spawn: we get there after the walk or when it respawns, whichever is later."""
    return xp / (max(dist / RUN_SPEED, max(0.0, respawn_left)) + POINT_OVERHEAD)


# ------------------------------------------------------------ server data
_spawns: list | None = None
_by_pos: dict = {}
_spawns_fail_t = 0.0     # last failed spawns() read (at() does not retry for a minute)


def spawns() -> list:
    """Every non-critter, non-vendor creature spawn on map 0 with the server's spawn levels (cached; ~1.3 s)."""
    global _spawns, _by_pos, _spawns_fail_t
    if _spawns is None:
        if time.time() - _spawns_fail_t < 60:
            raise RuntimeError("server spawn table read failed <60 s ago")
        try:
            rows = _read_spawns()
        except Exception:
            _spawns_fail_t = time.time()
            raise
        out = []
        for r in rows:
            if len(r) < 11:
                continue
            g, e, x, y, z, lo, hi, rk, fac, name, rs = r[:11]
            out.append(Spawn(int(g), int(e), float(x), float(y), float(z), int(lo), int(hi), int(rk), int(fac),
                             name, int(rs)))
        _by_pos = {(round(s.x), round(s.y)): s for s in out}
        _spawns = out
    return _spawns


def _read_spawns():
    return worlddb._query_db(
        "tcf_world",
        "SELECT c.guid, c.id, c.position_x, c.position_y, c.position_z, l.level_min, l.level_max, "
        "t.Classification, t.faction, t.name, c.spawntimesecs "
        "FROM creature c JOIN creature_template t ON t.entry = c.id JOIN creature_classic_level l ON l.entry = c.id "
        "WHERE c.map = 0 AND t.npcflag = 0 AND t.type <> 8 "
        "AND c.guid NOT IN (SELECT guid FROM game_event_creature WHERE eventEntry > 0)")


def at(x: float, y: float):
    """The server spawn at this spawn position (the 1.12 tables the quest planner reads share the positions:
    168 of 168 Troll Whelp / Crag Boar / Young Black Bear spawns), or None."""
    if _spawns is None:
        try:
            spawns()                     # after a failure it raises at once for a minute: no query per point
        except Exception:
            return None
    return _by_pos.get((round(x), round(y)))


_dead: dict = {}         # spawn guid -> unix time it respawns
_dead_t = 0.0            # when _dead was last read successfully
_dead_try = 0.0          # when a refresh was last started
_lock = threading.Lock()


def _refresh():
    global _dead, _dead_t
    try:
        rows = worlddb._query_db(
            "tcf_characters",
            "SELECT r.spawnId, r.respawnTime FROM respawn r JOIN tcf_world.creature c ON c.guid = r.spawnId "
            "WHERE r.type = 0 AND r.mapId = 0")
        d = {int(g): float(t) for g, t in rows}
        with _lock:
            _dead, _dead_t = d, time.time()
    except Exception:
        pass


def dead(block: bool = False) -> dict:
    """{spawn guid: respawn unix time} of dead creature spawns (cached RESPAWN_TTL s, refreshed in a thread)."""
    global _dead_try
    now = time.time()
    if now - _dead_try > RESPAWN_TTL:
        _dead_try = now
        if block:
            _refresh()
        else:
            threading.Thread(target=_refresh, daemon=True).start()
    with _lock:
        return _dead


def dead_known() -> bool:
    """The respawn table was read in the last two minutes (else every spawn counts as unknown, not alive)."""
    return time.time() - _dead_t < 120


def respawn_left(guid, now: float | None = None) -> float:
    """Seconds until this spawn is up again (0: alive). A guid we cannot place counts as alive."""
    if not guid:
        return 0.0
    t = dead().get(int(guid))
    return max(0.0, t - (now or time.time())) if t else 0.0
