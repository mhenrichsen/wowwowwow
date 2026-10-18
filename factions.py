"""Faction reactions from FactionTemplate.dbc - the same rules the client uses
to colour a nameplate red / yellow / green."""

from __future__ import annotations

import os
import struct

DBC = r"C:\Users\MadsG\Desktop\wow-forever-server\server\data\5875\dbc\FactionTemplate.dbc"


class FactionTemplate:
    __slots__ = ("id", "faction", "flags", "our_mask", "friend_mask", "enemy_mask",
                 "enemies", "friends")


def _load() -> dict[int, FactionTemplate]:
    data = open(DBC, "rb").read()
    magic, n, fields, size, _ = struct.unpack_from("<4s4I", data)
    assert magic == b"WDBC" and fields == 14, (magic, fields)
    out = {}
    for i in range(n):
        r = struct.unpack_from("<14I", data, 20 + i * size)
        t = FactionTemplate()
        t.id, t.faction, t.flags, t.our_mask, t.friend_mask, t.enemy_mask = r[:6]
        t.enemies = {e for e in r[6:10] if e}
        t.friends = {f for f in r[10:14] if f}
        out[t.id] = t
    return out


_T = _load()


def is_friendly(me: int, other: int) -> bool:
    a, b = _T.get(me), _T.get(other)
    if not a or not b:
        return False
    if b.faction in a.enemies:
        return False
    if b.faction in a.friends or a.faction == b.faction:
        return True
    return bool(a.friend_mask & b.our_mask) or bool(b.friend_mask & a.our_mask)


def is_hostile(me: int, other: int) -> bool:
    a, b = _T.get(me), _T.get(other)
    if not a or not b:
        return False
    if b.faction in a.enemies or a.faction in b.enemies:
        return True
    if b.faction in a.friends:
        return False
    return bool(a.enemy_mask & b.our_mask) or bool(b.enemy_mask & a.our_mask)
