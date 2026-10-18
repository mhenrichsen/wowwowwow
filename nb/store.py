"""One small JSON state file shared by the bot processes (4 run at once): death spots, wedge spots, climb votes and
blocks, dead talks, step deaths.

    st = Store(path, default=list)
    st.read()              # cheap: re-parsed only when the file's mtime / size changed; the last good copy on a torn
                           # or unreadable file; default() when there is none
    st.update(fn)          # under an OS lock on <path>.lock: fn(the newest data on disk) changes it in place or returns
                           # the new data, which is written atomically. So a save merges with what the other bots
                           # wrote since our read instead of clobbering it. False when the lock stayed busy / the
                           # write failed (callers carry on either way).

On disk the JSON is whatever `encode` makes of the data (`decode` reads it back), so existing files keep their layout.
Optional per-entry TTL (ttl + stamp(entry) -> time): expired entries are left out of read() and dropped on update().
Atomic write: a temp file beside the target, then os.replace (retried while a reader holds the file on Windows).
"""

from __future__ import annotations

import json
import os
import time

try:
    import msvcrt
except ImportError:                      # not Windows: no OS lock (the bots only run on Windows)
    msvcrt = None

LOCK_WAIT = 2.0                          # s a writer waits for the lock before giving up
REPLACE_TRIES, REPLACE_GAP = 20, 0.05    # os.replace retries while another process holds the target open


def atomic_write(path: str, data, indent=None) -> bool:
    """json.dump to <path>.<pid>.tmp, then os.replace onto path (retried on PermissionError). False if it failed."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
    except (OSError, TypeError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    for _ in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(REPLACE_GAP)
        except OSError:
            break
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


class FileLock:
    """An OS lock on one byte of a lock file (msvcrt.locking), as nb/bad_spots.lock always was. acquire() polls up
    to `wait` s; no msvcrt: always acquired."""

    def __init__(self, path: str, wait: float = LOCK_WAIT):
        self.path, self.wait = path, wait
        self._f = None

    def acquire(self) -> bool:
        if msvcrt is None:
            return True
        try:
            f = open(self.path, "a+")
        except OSError:
            return False
        t_end = time.time() + self.wait
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                self._f = f
                return True
            except OSError:
                if time.time() > t_end:
                    f.close()
                    return False
                time.sleep(0.05)

    def release(self):
        f, self._f = self._f, None
        if f is None:
            return
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        f.close()


class Store:
    def __init__(self, path: str, default=dict, decode=None, encode=None, lock: str | None = None,
                 ttl: float | None = None, stamp=None, indent=None):
        """default: factory of the empty data. decode(raw JSON) -> data / encode(data) -> JSON (identity when None).
        lock: the lock file (default <path>.lock). ttl/stamp: entries (list items, or dict values) whose stamp(entry)
        is ttl+ s old count as gone; a stamp of None never expires."""
        self.path = path
        self.default = default
        self.decode = decode or (lambda d: d)
        self.encode = encode or (lambda d: d)
        self.lock = lock or path + ".lock"
        self.ttl, self.stamp = ttl, stamp
        self.indent = indent
        self.data = default()
        self._sig = None

    # ---------------------------------------------------------------- reading
    def _sig_now(self):
        st = os.stat(self.path)
        return st.st_mtime_ns, st.st_size

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return self.decode(json.load(f))

    def fresh(self, data, now: float | None = None):
        """data without its expired entries (no ttl: data itself)."""
        if self.ttl is None or self.stamp is None:
            return data
        now = time.time() if now is None else now

        def alive(e):
            t = self.stamp(e)
            return t is None or now - float(t) < self.ttl
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if alive(v)}
        return [e for e in data if alive(e)]

    def read(self, now: float | None = None):
        """The data, re-read only when the file changed since the last read / write (mtime + size). Missing file: the
        last copy (default() at first); torn / unreadable: the last good copy."""
        try:
            sig = self._sig_now()
        except OSError:
            return self.fresh(self.data, now)
        if sig != self._sig:
            try:
                self.data, self._sig = self._load(), sig
            except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
                pass                     # mid-replace / broken: keep the last good copy
        return self.fresh(self.data, now)

    def changed(self) -> bool:
        """Has the file changed since our last read / write?"""
        try:
            return self._sig_now() != self._sig
        except OSError:
            return False

    # ---------------------------------------------------------------- writing
    def update(self, fn) -> bool:
        """fn(data) on the newest file under the lock: it changes data in place (returns None) or returns the new
        data. Written atomically; expired entries (ttl) dropped first. False if the lock stayed busy for LOCK_WAIT s
        or the write failed."""
        lk = FileLock(self.lock)
        if not lk.acquire():
            return False
        try:
            try:
                data = self._load()
            except FileNotFoundError:
                data = self.default()
            except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
                data = self._copy(self.data)         # a broken file: rebuild from our copy
            data = self.fresh(data)
            out = fn(data)
            if out is not None:
                data = out
            if not atomic_write(self.path, self.encode(data), self.indent):
                return False
            self.data = data
            try:
                self._sig = self._sig_now()
            except OSError:
                self._sig = None
            return True
        finally:
            lk.release()

    @staticmethod
    def _copy(d):
        if isinstance(d, dict):
            return dict(d)
        if isinstance(d, set):
            return set(d)
        return list(d)
