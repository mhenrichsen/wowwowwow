"""Summary of nb/navloop.jsonl per tag: legs, failures, stucks, seconds per 100 yd of plan (fights / far legs left out).

    python -m nb.navloop_report [tag ...]
"""
import json
import os
import sys
from collections import Counter

R = [json.loads(l) for l in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "navloop.jsonl"))]
tags = sys.argv[1:] or sorted({r["tag"] for r in R})
for tag in tags:
    rs = [r for r in R if r["tag"] == tag and not r["fight"] and r["res"] not in ("abort", "dead")
          and (r["plan"] or r["straight"]) < 800]
    ok = [r for r in rs if r["res"] == "ok"]
    yd = sum((r["plan"] or r["straight"]) for r in ok) or 1
    spots = Counter(s for r in rs for s in r["stuck_at"])
    hall = sum(1 for r in rs for s in r["stuck_at"] if -6110 < float(s[1:-1].split(",")[0]) < -6040
               and 360 < float(s[1:-1].split(",")[1]) < 400)
    print(f"{tag}: {len(rs)} legs, {len(rs) - len(ok)} failed, {sum(r['stucks'] for r in rs)} stucks "
          f"({hall} in Anvilmar), {100 * sum(r['s'] for r in ok) / yd:.1f} s per 100 yd, "
          f"legs with a stuck {sum(1 for r in rs if r['stucks'])}")
