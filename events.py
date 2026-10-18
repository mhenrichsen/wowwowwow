"""Structured event log: one JSON line per thing that happened.

bot.log is written for a person and answering "where did the hour go?" from
it means regex archaeology. This is written for dbg.py: every record has a
time, a kind and the run it belongs to, so time per activity, per guide step
and per quest can be added up exactly.

    {"t": 1790019569.58, "run": 1790019512, "ev": "action", "tag": "QC", ...}

Kinds: start, state, step, action, kill, level, death, stuck. Writing never
raises - a full disk must not stop the bot.
"""

from __future__ import annotations

import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "data", "events.jsonl")
RUN = int(time.time())              # one id per bot process
_lock = threading.Lock()


def emit(ev: str, **fields) -> None:
    rec = {"t": round(time.time(), 2), "run": RUN, "ev": ev, **fields}
    try:
        line = json.dumps(rec, separators=(",", ":"), default=str)
        with _lock, open(PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read(path: str = PATH):
    """Every record in the file, oldest first (skipping any torn line)."""
    try:
        f = open(path, encoding="utf-8")
    except OSError:
        return
    with f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue
