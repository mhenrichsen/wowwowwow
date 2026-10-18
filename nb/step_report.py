"""Per-step report of RXP walk logs (polestar Recorder's step-time report, for nb's navloop output).
python -m nb.step_report nb/rxp45.out nb/rxp46.out ...  ->  time, planned / straight yd, stucks per step, and the
steps worth a look: skipped, slow (DETOUR_RATIO x the straight line or SLOW_YPS), stuck-heavy, fights."""
from __future__ import annotations

import re
import sys
from collections import Counter

LEG = re.compile(r"^(\d\d:\d\d:\d\d) \[\d\] LEG ->#(\d+) (.+?): (\w+) in (\d+)s, plan (None|[\d.]+) yd, "
                 r"straight ([\d.]+) yd, (\d+) stuck\(s\)(.*)$")
SKIP = re.compile(r"SKIPPED-STUCK #(\d+)")
DETOUR_RATIO = 1.8     # planned / straight yd above this (over DETOUR_MIN yd) is a detour
DETOUR_MIN = 150.0
SLOW_YPS = 3.0         # yd/s below this over a planned leg of DETOUR_MIN+ yd is slow (run speed ~7)
STUCK_MANY = 3


def main(paths):
    legs, skips = [], set()
    for p in paths:
        for line in open(p, encoding="utf-8", errors="replace"):
            m = LEG.match(line)
            if m:
                t, n, name, res, sec, plan, straight, st, rest = m.groups()
                legs.append(dict(n=int(n), name=name, res=res, sec=int(sec), plan=None if plan == "None" else float(plan),
                                 straight=float(straight), stuck=int(st), fight="(fight)" in rest, t=t, log=p))
            m = SKIP.search(line)
            if m:
                skips.add(int(m.group(1)))
    if not legs:
        print("no LEG lines")
        return
    res = Counter(l["res"] for l in legs)
    tot = sum(l["sec"] for l in legs)
    walked = sum(l["plan"] or l["straight"] for l in legs)
    print(f"{len(legs)} legs #{legs[0]['n']}..#{legs[-1]['n']}: {tot / 3600:.1f} h walking, {walked / 1000:.1f} km, "
          f"{sum(l['stuck'] for l in legs)} stucks, {len(skips)} skipped; results {dict(res)}")
    look = []
    for l in legs:
        why = []
        if l["n"] in skips:
            why.append("SKIPPED")
        if l["stuck"] >= STUCK_MANY:
            why.append(f"{l['stuck']} stucks")
        if l["fight"]:
            why.append("fight")
        if l["plan"] and l["straight"] > DETOUR_MIN / 2 and l["plan"] > DETOUR_MIN and l["plan"] / max(l["straight"], 1) > DETOUR_RATIO:
            why.append(f"detour x{l['plan'] / l['straight']:.1f}")
        if l["res"] == "ok" and l["plan"] and l["plan"] > DETOUR_MIN and l["plan"] / max(l["sec"], 1) < SLOW_YPS:
            why.append(f"slow {l['plan'] / max(l['sec'], 1):.1f} yd/s")
        if why:
            look.append((l, why))
    print(f"\n{len(look)} steps to look at:")
    for l, why in look:
        print(f"  #{l['n']:<5} {l['t']} {l['name'][:40]:40s} {l['res']:6s} {l['sec']:4d}s "
              f"plan {l['plan'] or 0:6.0f} straight {l['straight']:6.0f}  {', '.join(why)}")
    by_zone = Counter()
    for l in legs:
        by_zone[l["name"].split(" L")[0]] += l["sec"]
    print("\ntime by guide section:")
    for z, sec in by_zone.most_common():
        print(f"  {z:22s} {sec / 60:6.1f} min")


if __name__ == "__main__":
    main(sys.argv[1:])
