"""nb/claims.py - hunt-area claims shared by the bot instances (nb/claims.json).

Every roam() pick writes the hunt point this bot walks to and farms (one entry per instance, refreshed by each
pick); roam() of the other bots then prefers points outside those areas. 2026-10-14 15:53-16:01: level 7 inst1
grinded the whelps level 3 inst4 needed for its quest, and inst4 skipped the quest. Only the point a bot is really
hunting is claimed, not its whole hunt list: bots of very different levels rarely want the same spawns.

A quest hunt beats a grind: a grinding bot keeps out of every other bot's claim, a quest hunt only out of other quest
hunts (it needs those mobs; a grinder can go anywhere). Entries expire after CLAIM_TTL s, so a bot that walked to
town, died or stopped frees its area by itself. The file is written under an OS lock on nb/claims.lock by a temp
file + os.replace (as nb/bad_spots.json), so a reader never sees half a file.
"""

from __future__ import annotations

import json
import math
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLAIMS = os.path.join(HERE, "claims.json")
CLAIM_LOCK = os.path.join(HERE, "claims.lock")
CLAIM_TTL = 300.0        # s: a claim not refreshed for this long is dropped
CLAIM_R = 40.0           # yd round a claimed point (roam()'s cluster radius)
WRITE_MOVE = 15.0        # yd: the claim is rewritten when the hunted point moves this far ...
WRITE_EVERY = 60.0       # s: ... or the kind changes, or this long after the last write (keeps it alive)


def live(entries: dict, me: str, now: float, ttl: float = CLAIM_TTL) -> list:
    """The other instances' claims that have not expired."""
    out = []
    for k, c in (entries or {}).items():
        if str(k) == str(me) or not isinstance(c, dict):
            continue
        try:
            if now - float(c.get("t", 0)) <= ttl:
                out.append(dict(c, inst=str(k)))
        except (TypeError, ValueError):
            continue
    return out


def blocks(c: dict, quest: bool) -> bool:
    """Does another bot's claim c keep us out? A grind keeps out of every claim, a quest hunt only of quest claims."""
    return (not quest) or bool(c.get("quest"))


def split(pts, others, quest: bool, r: float = CLAIM_R):
    """(free, taken): hunt points outside / inside the claims that block us. taken holds (point, claim) pairs."""
    cl = [c for c in others if blocks(c, quest)]
    free, taken = [], []
    for p in pts:
        hit = next((c for c in cl if math.hypot(p[0] - c["x"], p[1] - c["y"]) < r), None)
        if hit is None:
            free.append(p)
        else:
            taken.append((p, hit))
    return free, taken


def need_write(last, x: float, y: float, quest: bool, now: float) -> bool:
    """Rewrite our claim? last = (x, y, quest, t) of the last write, or None."""
    if last is None:
        return True
    lx, ly, lq, lt = last
    return (math.hypot(x - lx, y - ly) >= WRITE_MOVE or bool(lq) != bool(quest) or now - lt >= WRITE_EVERY)


class Claims:
    def __init__(self, inst, path: str = CLAIMS, lock: str = CLAIM_LOCK):
        self.inst = str(inst)
        self.path, self.lock = path, lock
        self.entries: dict = {}
        self._mt = None
        self._last = None             # (x, y, quest, t) of our last write

    def _read(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            d = json.load(f)
        c = d.get("claims", {}) if isinstance(d, dict) else {}
        return c if isinstance(c, dict) else {}

    def load(self) -> dict:
        try:
            mt = os.stat(self.path).st_mtime_ns
        except OSError:
            return self.entries
        if mt != self._mt:
            try:
                self.entries, self._mt = self._read(), mt
            except (OSError, ValueError):
                pass                                     # mid-replace / unreadable: keep the last good copy
        return self.entries

    def others(self, now: float | None = None) -> list:
        return live(self.load(), self.inst, time.time() if now is None else now)

    def claim(self, x: float, y: float, level: int, quest: bool, npcs=()) -> bool:
        """Claim the hunt point (x, y) for this instance. Skipped when the same claim was written lately."""
        now = time.time()
        if not need_write(self._last, x, y, quest, now):
            return True
        entry = {"x": round(float(x), 1), "y": round(float(y), 1), "t": round(now, 1), "level": int(level),
                 "quest": bool(quest), "npcs": sorted(int(n) for n in npcs)[:8]}

        def fn(entries):
            for k in [k for k, c in entries.items() if not isinstance(c, dict) or now - float(c.get("t", 0)) > CLAIM_TTL]:
                entries.pop(k, None)                     # expired: drop them while we are writing anyway
            entries[self.inst] = entry
        ok = self._update(fn)
        if ok:
            self._last = (x, y, quest, now)
        return ok

    def release(self) -> bool:
        """Drop our claim (the bot stops hunting here)."""
        self._last = None
        return self._update(lambda entries: entries.pop(self.inst, None))

    def _update(self, fn) -> bool:
        """fn(entries) changes the dict in place; done under the lock on the newest file. False if the lock stayed
        busy for 1 s or the write failed (the hunt goes on either way)."""
        import msvcrt
        try:
            lf = open(self.lock, "a+")
        except OSError:
            return False
        try:
            t_end = time.time() + 1.0
            while True:
                try:
                    lf.seek(0)
                    msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > t_end:
                        return False
                    time.sleep(0.05)
            try:
                try:
                    entries = self._read()
                except FileNotFoundError:
                    entries = {}
                except (OSError, ValueError):
                    entries = dict(self.entries)         # a broken file: rebuild from our copy
                fn(entries)
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"claims": entries}, f, indent=0)
                for _ in range(20):                       # a reader holding the file open blocks the replace briefly
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:
                        time.sleep(0.05)
                else:
                    os.remove(tmp)
                    return False
                self.entries = entries
                self._mt = os.stat(self.path).st_mtime_ns
                return True
            finally:
                lf.seek(0)
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            return False
        finally:
            lf.close()
