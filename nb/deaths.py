"""Shared death spots (nb/death_spots.json) on nb.store: another bot's death spot becomes a route cost for every bot
for a while (inst4 08:26 and inst2 09:05 both died to the same mob pair at (-5041,-2900) walking to talk targets on
different guide steps).

Drop-in for the helpers in nb/bot.py (same names, same file layout: [[x, y, time, inst], ...]). Each death is merged
into the file under its lock, so two bots dying at once both land; entries older than SHARED_DEATH_S are dropped on
write and ignored on read, as before.
"""

from __future__ import annotations

import os

from nb import inst
from nb.store import Store
from nb.util import swallowed

SHARED_DEATHS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "death_spots.json")
SHARED_DEATH_S = 1800.0

_STORE: dict = {}


def store(path: str = None) -> Store:
    path = path or SHARED_DEATHS
    st = _STORE.get(path)
    if st is None:
        st = _STORE[path] = Store(path, default=list, ttl=SHARED_DEATH_S, stamp=lambda e: e[2])
    return st


def note_shared_death(x, y, path: str = None, now: float = None, who=None):
    """Add our death at (x, y) to the shared file (expired entries dropped). Never raises."""
    import time
    now = time.time() if now is None else now
    who = inst.INST if who is None else who
    try:
        store(path).update(lambda d: d.append([round(x, 1), round(y, 1), now, who]))
    except Exception as e:
        swallowed("deaths.note_shared_death", e)


def shared_deaths(path: str = None, who=None) -> list:
    """(x, y) of the other bots' deaths in the last SHARED_DEATH_S."""
    who = inst.INST if who is None else who
    try:
        return [(e[0], e[1]) for e in store(path).read() if e[3] != who]
    except Exception as e:
        swallowed("deaths.shared_deaths", e)
        return []
