"""time_budget.py LOG [since HH:MM:SS]: where a bot's time went, by the log line that starts each stretch.
Categories: fight, rest, walk, talk, vendor, train, dead, plan, stuck, other. A stretch runs until the next
categorized line; gaps over GAP s count as 'silent' (nothing logged: a hang or a long wait)."""
import re, sys
GAP = 30.0
CATS = [
    ("dead", r"died at|ghost|resurrection sickness|revive"),
    ("fight", r"fight npc|  fight:|KILL|rot: |flee:|chase:|finish:|forecast mid"),
    ("loot", r"loot "),
    ("rest", r"rest: |drinking|eating"),
    ("vendor", r"vendor:|stock:|slow vendor"),
    ("train", r"train"),
    ("talk", r"rxp talk|quest: |gossip|turn in|accept"),
    ("plan", r"planning |slow path"),
    ("stuck", r"stuck at|wedge|escaped"),
    ("walk", r"route to|roam|rxp goal|rxp hunt|rxp kill|rxp step|fly:"),
]
def t(s):
    h, m, x = map(int, s.split(":")); return h * 3600 + m * 60 + x
path = sys.argv[1]; since = t(sys.argv[2]) if len(sys.argv) > 2 else None
rows = []
for ln in open(path, encoding="utf-8", errors="replace"):
    m = re.match(r"(\d\d:\d\d:\d\d) (?:\[\d\] )?(.*)", ln)
    if not m or "steer tele" in ln or "pad claim" in ln:
        continue
    tt = t(m.group(1))
    if since is not None and tt < since:
        continue
    cat = next((c for c, rx in CATS if re.search(rx, m.group(2))), None)
    rows.append((tt, cat))
tot = {}
cur, t0 = "other", rows[0][0] if rows else 0
last = t0
for tt, cat in rows:
    gap = tt - last
    if gap > GAP:
        tot["silent"] = tot.get("silent", 0) + gap
        tot[cur] = tot.get(cur, 0) + (last - t0)
        t0, cur = tt, cat or cur
    elif cat and cat != cur:
        tot[cur] = tot.get(cur, 0) + (tt - t0)
        cur, t0 = cat, tt
    last = tt
tot[cur] = tot.get(cur, 0) + (last - t0)
span = sum(tot.values()) or 1
print(f"{path}: {span/60:.0f} min: " + ", ".join(f"{k} {v/60:.1f}m ({100*v/span:.0f}%)" for k, v in sorted(tot.items(), key=lambda x: -x[1])))
