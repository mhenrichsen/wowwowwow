"""Small shared helpers: exceptions swallowed with a log line once per call site, and give-up timers."""

from __future__ import annotations

import sys
import time

_SEEN: set = set()          # tags already logged (swallow / swallowed)


def _stderr(msg: str):
    print(msg, file=sys.stderr, flush=True)


def swallowed(tag: str, e: BaseException, log=None) -> bool:
    """Note an exception that the caller handles by carrying on: one log line (class + message) the first time per
    tag, nothing after that. True when this call logged."""
    if tag in _SEEN:
        return False
    _SEEN.add(tag)
    try:
        (log or _stderr)(f"{tag}: {type(e).__name__}: {e} (ignored; logged once)")
    except Exception:
        pass                                     # a broken log function must not change the caller's flow
    return True


class swallow:
    """`with swallow("tag", log): ...` is `try: ... except Exception: pass` with swallowed()'s log-once line.
    The exception is kept in .error (None when the block ran through)."""

    def __init__(self, tag: str, log=None, exc=Exception):
        self.tag, self.log, self.exc = tag, log, exc
        self.error = None

    def __enter__(self):
        return self

    def __exit__(self, et, e, tb):
        if et is None or not issubclass(et, self.exc):
            return False
        self.error = e
        swallowed(self.tag, e, self.log)
        return True


class Deadline:
    """A give-up time `secs` from now (or from `start`): `while not d.passed(): ...`. Reads the clock through
    module `time` at each call, so tests that swap a module's clock see it via clock=."""

    def __init__(self, secs: float, start: float | None = None, clock=None):
        self.clock = clock or time
        self.secs = secs
        self.t0 = self.clock.time() if start is None else start

    def restart(self):
        """Start the window again from now (a stuck clock after progress)."""
        self.t0 = self.clock.time()

    def elapsed(self) -> float:
        return self.clock.time() - self.t0

    def left(self) -> float:
        return self.secs - self.elapsed()

    def passed(self) -> bool:
        """More than `secs` since the start (the old `time.time() - t0 > secs`)."""
        return self.elapsed() > self.secs

    def running(self) -> bool:
        """Still inside the window (the old `time.time() - t0 < secs`)."""
        return self.elapsed() < self.secs


class Cooldown:
    """At most once per `secs`: `if cd.ready(): ...` is True the first time and then again once `secs` have passed
    since the last True (the old `if now - last > secs: last = now; ...`)."""

    def __init__(self, secs: float, clock=None):
        self.clock = clock or time
        self.secs = secs
        self.last = None

    def ready(self, now: float | None = None) -> bool:
        now = self.clock.time() if now is None else now
        if self.last is not None and now - self.last <= self.secs:
            return False
        self.last = now
        return True

    def reset(self):
        self.last = None
