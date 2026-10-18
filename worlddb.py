"""Static creature metadata (names, types) from the local server's world DB.

Memory tells us WHAT is where; the world DB tells us what an entry IS (name,
critter or not, elite or not). Queried once and cached.
"""

from __future__ import annotations


import subprocess

MARIADB = r"C:\Users\MadsG\Desktop\wow-forever-server\mariadb\bin\mariadb.exe"
CREATURE_TYPE_CRITTER = 8


class CreatureInfo:
    __slots__ = ("entry", "name", "type", "rank", "min_level", "max_level")

    def __init__(self, entry, name, type_, rank, min_level, max_level):
        self.entry, self.name, self.type, self.rank = entry, name, type_, rank
        self.min_level, self.max_level = min_level, max_level

    @property
    def critter(self) -> bool:
        return self.type == CREATURE_TYPE_CRITTER

    @property
    def elite(self) -> bool:
        return self.rank > 0


_cache: dict[int, CreatureInfo] = {}


def _query(sql: str) -> list[list[str]]:
    return _query_db("mangos", sql)


def _query_db(db: str, sql: str) -> list[list[str]]:
    out = subprocess.run(
        [MARIADB, "-u", "root", "-pvmangos", "-h", "127.0.0.1", "-P", "3310",
         "-B", "-N", db, "-e", sql],
        capture_output=True, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode:
        raise RuntimeError(out.stderr.strip())
    return [l.split("\t") for l in out.stdout.splitlines() if l]


def creatures(entries) -> dict[int, CreatureInfo]:
    missing = sorted({int(e) for e in entries} - _cache.keys())
    if missing:
        # creature_template has one row per client build; take the newest <= 5875.
        rows = _query(
            "SELECT t.entry, t.name, t.type, t.rank, t.level_min, t.level_max "
            "FROM creature_template t JOIN (SELECT entry, MAX(patch) p FROM creature_template "
            f"WHERE entry IN ({','.join(map(str, missing))}) GROUP BY entry) m "
            "ON t.entry = m.entry AND t.patch = m.p")
        for e, n, ty, rk, lo, hi in rows:
            _cache[int(e)] = CreatureInfo(int(e), n, int(ty), int(rk), int(lo), int(hi))
    return {e: _cache[e] for e in entries if e in _cache}


def name(entry: int) -> str:
    info = creatures([entry]).get(entry)
    return info.name if info else f"#{entry}"


def creature_flags(entry: int) -> int:
    """npc_flags of a creature template (0x80 = innkeeper, 0x10 = trainer...)."""
    rows = _query("SELECT npc_flags FROM creature_template WHERE entry=%d" % entry)
    return int(rows[0][0]) if rows else 0
