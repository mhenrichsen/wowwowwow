"""Offline nav eval over polestar's route corpus (polestar_data/tools_lib_nav_eval_routes.json, its world-point
routes) plus RXP guide legs: plans each with navmesh.find_path and reports no-paths, length vs straight line, the
steepest climb per yd, swim share and time. python -m nb.nav_eval [--rxp N] [--level L] [--out FILE]
Run it before and after a navigation change: a new NOPATH or a jump in length is the regression to read."""
from __future__ import annotations

import math
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import curmap      # noqa: E402
import navmesh     # noqa: E402


def routes(n_rxp=0):
    import polestar_data
    out = []
    for r in polestar_data.eval_routes()["routes"]:
        a, b = r.get("from"), r.get("to")
        if isinstance(a, list) and isinstance(b, list) and r.get("map") in (0, 1):
            out.append((f"polestar:{r['id']}", r["map"], tuple(a), tuple(b), r.get("level")))
    if n_rxp:
        from nb import navloop
        rr = navloop.rxp_route()
        legs = [(i, rr[i - 1], rr[i]) for i in range(1, len(rr))
                if rr[i - 1][1] == rr[i][1] and 80 < math.hypot(rr[i][2] - rr[i - 1][2], rr[i][3] - rr[i - 1][3]) < 900]
        for i, a, b in legs[::max(1, len(legs) // n_rxp)]:
            out.append((f"rxp:#{i}", a[1], (a[2], a[3], None), (b[2], b[3], None), None))
    return out


def main(argv):
    n_rxp = int(argv[argv.index("--rxp") + 1]) if "--rxp" in argv else 0
    level = int(argv[argv.index("--level") + 1]) if "--level" in argv else None
    outp = argv[argv.index("--out") + 1] if "--out" in argv else None
    f = open(outp, "w") if outp else sys.stdout
    nms, tot = {}, dict(n=0, nopath=0, sec=0.0)
    for name, mp, a, b, lv in routes(n_rxp):
        curmap.MAP = mp
        nm = nms.get(mp) or navmesh.NavMesh(navmesh.TCF_MMAP_DIR, mp)
        nms[mp] = nm
        nm.level = lv or level
        A = (a[0], a[1], a[2] if a[2] is not None else nm.ground_z(a[0], a[1]))
        B = (b[0], b[1], b[2] if b[2] is not None else nm.ground_z(b[0], b[1]))
        if A[2] is None or B[2] is None:
            print(f"{name:45s} no floor at an end", file=f, flush=True)
            continue
        t = time.time()
        try:
            p = nm.find_path(A, B, budget=40)
        except Exception as e:                      # noqa: BLE001
            p = None
            print(f"{name:45s} ERROR {e!r}", file=f, flush=True)
        dt = time.time() - t
        tot["n"] += 1
        tot["sec"] += dt
        straight = math.dist(A[:2], B[:2])
        if not p:
            tot["nopath"] += 1
            print(f"{name:45s} NOPATH straight {straight:6.0f} {dt:5.1f}s", file=f, flush=True)
            continue
        L = sum(math.dist(p[k][:2], p[k + 1][:2]) for k in range(len(p) - 1))
        # steepest climb over a 4+ yd stretch (point heights jitter on short segments)
        climb, j = 0.0, 0
        for k in range(1, len(p)):
            while j < k and math.dist(p[j][:2], p[k][:2]) > 8.0:
                j += 1
            run = math.dist(p[j][:2], p[k][:2])
            if run >= 4.0:
                climb = max(climb, (p[k][2] - p[j][2]) / run)
        print(f"{name:45s} {L:7.0f} yd  x{L / max(straight, 1):4.2f}  climb {math.degrees(math.atan(max(climb, 0))):4.0f} deg"
              f"  {dt:5.1f}s", file=f, flush=True)
    print(f"TOTAL {tot['n']} routes, {tot['nopath']} no path, {tot['sec']:.0f}s planning", file=f, flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
