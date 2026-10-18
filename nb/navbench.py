"""Navigation benchmark + tuner for the nb pixel bot.

Runs a fixed loop of legs inside Anvilmar (buildings, stairs) and across the
open ground around the quest NPCs, times every leg, and (with --tune) searches
the steering constants in nb/move.py for the fastest lap.

    python -m nb.navbench              # one lap with the current constants
    python -m nb.navbench --tune       # coordinate search, results in nb/bench_results.json
Stop: create nb/STOP. Aborts a run (never fights) on combat or death.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import move as M                      # noqa: E402
from nb.eye import Eye                        # noqa: E402
from nb.move import Mover                     # noqa: E402
from nb.win import Keys                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STOP = os.path.join(HERE, "STOP")
RESULTS = os.path.join(HERE, "bench_results.json")
LOG = os.path.join(HERE, "bench.log")

# (name, x, y, z, indoor) - NPC spots in Coldridge Valley; no caves
ROUTE = [
    ("Sten Stoutarm (outdoor)",      -6215.0, 328.0, 383.7, False),
    ("Spirit healer (hill)",         -6170.0, 347.0, 400.2, False),
    ("Thorgas Grimson (Anvilmar)",   -6092.0, 365.0, 395.6, True),
    ("Solm Hargrin (Anvilmar)",      -6094.0, 405.0, 395.6, True),
    ("Durnan Furcutter (east bldg)", -6056.0, 385.0, 392.8, True),
    ("Alamar Grimm (upper floor)",   -6049.0, 391.0, 399.0, True),
    ("Bromos Grummner (Anvilmar)",   -6121.0, 382.0, 395.8, True),
]
TOL = 3.0
LEG_TIMEOUT = 100.0
FAIL_PENALTY = 60.0

PARAMS = {                                    # name -> candidate values (current first)
    "CORNER_POP":   [3.0, 2.0, 4.0, 5.0],
    "CORNER_BLEND": [7.0, 4.0, 10.0, 14.0],
    "BLEND_T":      [0.5, 0.3, 0.7, 0.9],
    "PAD_GAIN":     [4.0, 3.0, 6.0, 8.0],
    "SLOW_ERR":     [1.0, 0.7, 1.4],
    "SLOW_FWD":     [0.45, 0.7, 0.3],
}


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class Bench:
    def __init__(self):
        self.eye = Eye()
        self.keys = Keys(self.eye.cap.hwnd, self.eye.in_world)
        self.stucks = 0

        def mlog(m):
            if "stuck at" in m:
                self.stucks += 1
            log("    " + m)
        self.mv = Mover(self.eye, self.keys, mlog)

    def abort_reason(self, s):
        if os.path.exists(STOP):
            return "stop"
        if s.combat or s.dead or s.ghost:
            return "combat/dead"
        return None

    def leg(self, name, x, y, z):
        s = self.mv.state()
        path = self.mv.path(x, y, z)
        plen = 0.0
        if path:
            pts = [(s.x, s.y)] + [(p[0], p[1]) for p in path]
            plen = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        self.stucks = 0
        t0 = time.time()
        r = self.mv.goto(x, y, z, tol=TOL, timeout=LEG_TIMEOUT, abort=lambda st: self.abort_reason(st) is not None)
        dt = time.time() - t0
        return {"leg": name, "result": r, "time": round(dt, 1), "path": round(plen),
                "speed": round(plen / dt, 2) if dt > 0 and r == "ok" else 0, "stucks": self.stucks}

    def lap(self):
        rows = []
        for name, x, y, z, indoor in ROUTE:
            row = self.leg(name, x, y, z)
            row["indoor"] = indoor
            rows.append(row)
            log(f"  {name:32s} {row['result']:7s} {row['time']:6.1f}s path {row['path']:4d} yd "
                f"{row['speed']:4.1f} yd/s stucks {row['stucks']}")
            if row["result"] in ("abort", "dead"):
                break
        ok = all(r["result"] == "ok" for r in rows) and len(rows) == len(ROUTE)
        total = sum(r["time"] for r in rows) + FAIL_PENALTY * sum(r["result"] != "ok" for r in rows)
        aborted = any(r["result"] in ("abort", "dead") for r in rows)
        return {"ok": ok, "aborted": aborted, "score": round(total, 1), "legs": rows}

    def go_to_start(self):
        n, x, y, z, _ = ROUTE[-1]
        log(f"positioning at {n}")
        return self.mv.goto(x, y, z, tol=TOL, timeout=150.0, abort=lambda st: self.abort_reason(st) is not None)

    def evaluate(self, params):
        for k, v in params.items():
            setattr(M, k, v)
        log("run " + json.dumps(params))
        res = self.lap()
        res["params"] = dict(params)
        with open(RESULTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(res) + "\n")
        log(f"  => score {res['score']} s ({'ok' if res['ok'] else 'FAILED/aborted'})")
        return res


def current():
    return {k: getattr(M, k) for k in PARAMS}


def main():
    if os.path.exists(STOP):
        os.remove(STOP)
    b = Bench()
    try:
        r = b.go_to_start()
        if r not in ("ok",):
            log(f"could not reach the start ({r}); aborting")
            return
        best = b.evaluate(current())
        if "--tune" not in sys.argv:
            return
        base = dict(best["params"])
        for name, cands in PARAMS.items():
            for v in cands:
                if v == base[name] or os.path.exists(STOP) or best["aborted"]:
                    continue
                trial = dict(base); trial[name] = v
                res = b.evaluate(trial)
                if res["aborted"]:
                    best = res
                    break
                if res["ok"] and res["score"] < best["score"] - 0.5:
                    best, base = res, dict(trial)
                    log(f"  new best {best['score']} s with {name}={v}")
        for k, v in base.items():
            setattr(M, k, v)
        log("BEST " + json.dumps(base) + f" score {best['score']}")
        json.dump({"best": base, "score": best["score"]}, open(os.path.join(HERE, "bench_best.json"), "w"))
    finally:
        b.mv.stop()


if __name__ == "__main__":
    main()
