"""Record a human play session from the BotEye strip (read-only: no keys, no clicks, no screenshots).

  python -m nb.record 4            record instance 4 to nb/human/<date>_inst4.jsonl until Ctrl+C or nb/human/STOP
One JSON line per strip read (~5 Hz): position, facing, hp/mana, xp, flags, casting, target, nearby plates, quest log,
money, open windows and the RestedXP step. Keystrokes are not logged; casts and targets show in the strip.
Used to build trail routes / NPC approach spots and a per-RXP-step timing baseline against the bot.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time

from nb.eye import Eye
from nb.multi import find_instance_window

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "human")
STOP = os.path.join(OUT, "STOP")
HZ = 5.0


def plain(o):
    if dataclasses.is_dataclass(o):
        return {k: plain(v) for k, v in dataclasses.asdict(o).items()}
    if isinstance(o, (list, tuple)):
        return [plain(v) for v in o]
    if isinstance(o, dict):
        return {k: plain(v) for k, v in o.items()}
    if isinstance(o, float):
        return round(o, 2)
    return o


def row(s) -> dict:
    d = {k: getattr(s, k) for k in ("t", "x", "y", "z", "facing", "hp", "hpmax", "mana", "manamax", "level", "xp",
                                    "xpmax", "flags", "casting", "money", "bags_free", "ui", "nquests", "err", "errn")}
    if s.has_target:
        d["tgt"] = {k: getattr(s, k) for k in ("t_npc", "t_hp", "t_hpmax", "t_level", "t_reaction", "t_flags",
                                               "t_x", "t_y")}
    d["plates"] = [(p.npc, p.level, p.hp, p.hostile, p.dead, p.combat, p.on_me, p.is_target) for p in s.plates]
    d["quests"] = plain(s.quests)
    d["msg"] = s.msg if s.msgn else ""
    if s.rxp is not None and s.rxp.loaded:
        r = s.rxp
        d["rxp"] = {"guide": r.guide, "index": r.index, "line": r.line, "stepctr": r.stepctr, "kinds": r.kinds,
                    "wp": r.wp, "quests": r.quests, "text": r.text}
    return plain(d)


def main(inst: int):
    os.makedirs(OUT, exist_ok=True)
    if os.path.exists(STOP):
        os.remove(STOP)
    path = os.path.join(OUT, time.strftime("%Y%m%d_%H%M") + f"_inst{inst}.jsonl")
    print(f"recording instance {inst} -> {path}  (Ctrl+C or create {STOP} to stop)", flush=True)
    eye, h, n, last_print = None, 0, 0, 0.0
    with open(path, "a", encoding="utf-8") as f:
        while not os.path.exists(STOP):
            t0 = time.time()
            try:
                hw = find_instance_window(inst)
                if hw != h or eye is None:                   # client restarted / first run
                    h, eye = hw, Eye(hw)
                s = eye.read()
            except Exception as e:                           # window resized / gone: rebuild the capture next tick
                s, eye = None, None
                if t0 - last_print > 30:
                    print(time.strftime("%H:%M:%S"), f"no strip ({e!r})", flush=True)
                    last_print = t0
            if s is not None:
                f.write(json.dumps(row(s)) + "\n")
                n += 1
                if n % 300 == 0:
                    f.flush()
                    g = s.rxp.guide if s.rxp and s.rxp.loaded else "-"
                    print(time.strftime("%H:%M:%S"), f"{n} rows  L{s.level}  ({s.x:.0f},{s.y:.0f})  {g}", flush=True)
            time.sleep(max(0.0, 1.0 / HZ - (time.time() - t0)))
    print(f"stopped: {n} rows in {path}", flush=True)


if __name__ == "__main__":
    try:
        main(int(sys.argv[1]) if sys.argv[1:] else 4)
    except KeyboardInterrupt:
        print("stopped (Ctrl+C)")
