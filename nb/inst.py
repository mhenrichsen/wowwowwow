"""Which of the side-by-side clients this bot drives (`--inst N` or env NB_INST, default 1).

Instance 1 keeps the original files in nb/ (Shamaa); instance N>1 keeps its log, lock, STOP and progress
files in nb/instN/. Import this before anything that builds a file path.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _parse() -> int:
    for i, a in enumerate(sys.argv):
        if a == "--inst" and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
        if a.startswith("--inst="):
            return int(a.split("=", 1)[1])
    return int(os.environ.get("NB_INST", "1"))


INST = _parse()
os.environ["NB_INST"] = str(INST)
DIR = HERE if INST == 1 else os.path.join(HERE, f"inst{INST}")
os.makedirs(DIR, exist_ok=True)

# |right stick Y| this bot's pad holds while BotEye claims it (one value per instance, under the stick dead zone)
PAD_SIG = round(0.05 + 0.03 * INST, 3)


def path(name: str) -> str:
    return os.path.join(DIR, name)


def window() -> int:
    """hwnd of this instance's client (by its -config BetaLocalN.wtf)."""
    from nb.multi import find_instance_window
    return find_instance_window(INST)


def args() -> list[str]:
    """sys.argv[1:] without the --inst option."""
    out, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a == "--inst":
            skip = True
            continue
        if a.startswith("--inst="):
            continue
        out.append(a)
    return out
