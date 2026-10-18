"""Issue triage: mine every bot log for deaths, navigation trouble, failed
guide steps and errors, group them into issues, rank them and track them
across runs in data/issues.json.

    python triage.py scan [--hours H]        rank open issues (default: last 24 h)
    python triage.py show ID [n]             examples with log context
    python triage.py fixed ID "what changed" mark fixed; it shows as REGRESSED if it comes back
    python triage.py wontfix ID "why"        stop ranking it
    python triage.py list [--all]            the tracker, open first
    python triage.py json [--hours H]        machine-readable ranking (for the workflow)

An issue id is a short signature: death:<cause>:<mob kind>, stuck:<x>,<y>
(a 30 yd cell), navfail:<x>,<y>, step:<guide>#<n>:<tag>, error:<text>,
warn:<text>. Text is normalised (numbers -> #) so repeats group.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
TRACK = os.path.join(HERE, "data", "issues.json")
TS = re.compile(r"^(\d\d):(\d\d):(\d\d) (.*)$")

# lines that are noise, not problems
IGNORE = re.compile(r"steering round|fight status|back on the remembered route|re-joining the remembered")
# recurring warnings worth counting (normalised text becomes the id)
WARN = re.compile(
    r"not found on screen for a click|interaction did not trigger|did NOT go off|no damage landing"
    r"|no progress for|cannot afford|not sitting after|revive spot unreachable|no navmesh path"
    r"|is out of reach|leaving it for later|come in packs|giving up on this destination"
    r"|swimming|death trap|could not|timed out|unreachable")
ERROR = re.compile(r"Traceback|Error\b|Exception|failed: ", re.I)


def norm(s: str) -> str:
    s = re.sub(r"\(-?\d+(\.\d+)?, -?\d+(\.\d+)?(, -?\d+(\.\d+)?)?\)", "(X,Y)", s)
    s = re.sub(r"-?\d+(\.\d+)?", "#", s)
    return re.sub(r"\s+", " ", s).strip()[:90]


def cell(x, y, size=30):
    return f"{int(x // size * size)},{int(y // size * size)}"


# ------------------------------------------------------------------ reading

def log_files():
    fs = glob.glob(os.path.join(HERE, "logs", "bot_*.log")) + [os.path.join(HERE, "bot.log")]
    return sorted((f for f in fs if os.path.exists(f)), key=os.path.getmtime)


def read_log(path):
    """[(epoch, text)] - log lines carry only HH:MM:SS; date them backwards
    from the file's mtime, stepping a day back when time runs backwards."""
    raw = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = TS.match(line.rstrip("\n"))
            if m:
                raw.append((int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]), m[4]))
            elif raw and line.strip():
                raw.append((raw[-1][0], line.rstrip("\n")))     # traceback lines
    if not raw:
        return []
    end = dt.datetime.fromtimestamp(os.path.getmtime(path))
    day = dt.datetime(end.year, end.month, end.day).timestamp()
    if raw[-1][0] > end.hour * 3600 + end.minute * 60 + end.second + 60:
        day -= 86400
    out, prev = [], None
    for secs, text in reversed(raw):
        if prev is not None and secs > prev + 60:
            day -= 86400
        prev = secs
        out.append((day + secs, text))
    out.reverse()
    return out


def all_lines(since):
    seen = set()
    for f in log_files():
        if os.path.getmtime(f) < since:
            continue
        lines = read_log(f)
        # rewind_step/restarts copy bot.log into logs/: skip exact duplicates
        key = (lines[0] if lines else None, len(lines))
        if key in seen:
            continue
        seen.add(key)
        yield f, lines


# ------------------------------------------------------------------ classify

MOB_LVL = re.compile(r"attacking (.+?) \(lvl (\d+)\)")
JOIN = re.compile(r"\+ (.+?) \(lvl (\d+)\) joined .* hp (\d+)%")
HPSTAT = re.compile(r"us (\d+)% hp")
LEVEL = re.compile(r"LEVEL (\d+) reached|level \d+->(\d+)")


def death_cause(ctx, my_level):
    """Why we died, from the ~60 lines before the death."""
    fights = [(m[1], int(m[2])) for m in (MOB_LVL.search(t) for _, t in ctx) if m]
    joins = [(m[1], int(m[2]), int(m[3])) for m in (JOIN.search(t) for _, t in ctx) if m]
    last_start_hp = None
    for i, (_, t) in enumerate(ctx):
        if MOB_LVL.search(t):
            nxt = [HPSTAT.search(u) for _, u in ctx[i:i + 4]]
            nxt = [int(m[1]) for m in nxt if m]
            if nxt:
                last_start_hp = nxt[0]
    text = "\n".join(t for _, t in ctx)
    mob = (joins[-1][0] if joins else fights[-1][0] if fights else "?")
    kind = re.sub(r"^(Young|Elder|Small|Large|Ragged|Burly|Rockjaw|Frostmane) ", "", mob)
    if "durability" in text and "0%" in text:
        cause = "broken-gear"
    elif joins:
        cause = "add-joined"
    elif last_start_hp is not None and last_start_hp < 60:
        cause = "pulled-low-hp"
    elif fights and my_level and fights[-1][1] > my_level:
        cause = "higher-level-mob"
    elif "drown" in text.lower() or "swimming" in text:
        cause = "drowning"
    elif "fall" in text.lower():
        cause = "fall"
    else:
        cause = "lost-1v1"
    return cause, kind


def scan(hours=24.0):
    since = time.time() - hours * 3600
    issues = defaultdict(lambda: {"count": 0, "examples": [], "first": None, "last": None})

    def add(iid, ts, fname, idx, summary, kind):
        it = issues[iid]
        # a loop prints the same line every second: count episodes, not lines
        if kind in ("warn", "step", "error") and it["last"] and 0 <= ts - it["last"] < 60:
            it["last"] = ts
            it["repeats"] = it.get("repeats", 0) + 1
            return
        it["count"] += 1
        it["kind"] = kind
        it["first"] = min(it["first"] or ts, ts)
        it["last"] = max(it["last"] or ts, ts)
        if len(it["examples"]) < 6:
            it["examples"].append({"t": ts, "file": os.path.basename(fname), "line": idx, "text": summary})

    for fname, lines in all_lines(since):
        level = None
        stuck_run = None
        for i, (ts, t) in enumerate(lines):
            if ts < since or IGNORE.search(t):
                continue
            m = LEVEL.search(t)
            if m:
                level = int(m[1] or m[2])
            m = re.search(r"started at .*|guide: .* for a (\w+) (\w+)", t)
            if "died (death #" in t:
                ctx = lines[max(0, i - 60):i + 1]
                lv = level
                for _, u in reversed(ctx):
                    mm = re.search(r"kills=\d+ xp", u)
                    if mm:
                        break
                cause, kind = death_cause(ctx, lv)
                add(f"death:{cause}:{kind}", ts, fname, i, t, "death")
            elif re.search(r"stuck at \(", t):
                mm = re.search(r"stuck at \((-?[\d.]+), (-?[\d.]+)\)", t)
                c = cell(float(mm[1]), float(mm[2]))
                # count a stuck episode once, not every unstuck attempt
                if stuck_run != c or "attempt 1" in t:
                    add(f"stuck:{c}", ts, fname, i, t, "nav")
                stuck_run = c
            elif "giving up on this destination" in t or "walk FAILED" in t:
                add("nav:gave-up-destination", ts, fname, i, t, "nav")
            elif re.search(r"step \d+: \[.*\] failed \d+ times|step \d+: skipping \[", t):
                mm = re.search(r"step (\d+): (?:skipping )?\[([A-Z]+)([^\]]*)\]", t)
                g = ""
                for _, u in reversed(lines[:i]):
                    gm = re.search(r"guide: (.+?), \d+ steps", u)
                    if gm:
                        g = gm[1]
                        break
                if mm and mm[2] not in ("DESTROY",):
                    add(f"step:{g}#{mm[1]}:{mm[2]}{mm[3]}", ts, fname, i, t, "step")
            elif ERROR.search(t) and not WARN.search(t):
                add(f"error:{norm(t)}", ts, fname, i, t, "error")
            elif WARN.search(t):
                add(f"warn:{norm(t)}", ts, fname, i, t, "warn")

    # navigation failures from the event log: long walks that ended short
    try:
        evs = [json.loads(l) for l in open(os.path.join(HERE, "data", "events.jsonl"))]
        for i, e in enumerate(evs):
            if e["t"] < since or e["ev"] != "nav_end" or e.get("ok") or e.get("left", 0) < 15:
                continue
            if e.get("secs", 0) < 30:
                continue                 # quick aborts are fights on the way
            # nav_end carries no reason yet, and _follow returns "interrupted"
            # on aggro: the Young Black Bear by the Kharanos road cut corpse
            # runs 60-100 s in, logging 387 yd "navfails" at (-5506,-428)
            # every time. Look at what came next, by time (pos events are
            # dense): fighting/dead at once, or a kill/death/restart within
            # 30 s ('state' is only emitted on change, so the kill can be the
            # first event, 13-25 s after the cut) means it was no dead end.
            nxt = None
            for f in evs[i + 1:]:
                if f["t"] - e["t"] > 30:
                    break
                if f["ev"] != "pos":
                    nxt = f
                    break
            if nxt and ((nxt["ev"] == "state" and nxt.get("s") in ("fighting", "dead")
                         and nxt["t"] - e["t"] < 5)
                        or nxt["ev"] in ("kill", "death", "start")):
                continue
            x, y = e["at"][:2]
            add(f"navfail:{cell(x, y)}", e["t"], "events.jsonl", 0,
                f"walk ended {e['left']} yd short after {e['secs']:.0f}s at ({x:.0f}, {y:.0f})", "nav")
    except FileNotFoundError:
        pass
    return issues


# ------------------------------------------------------------------ ranking/tracking

WEIGHT = {"death": 10.0, "error": 6.0, "nav": 3.0, "step": 2.0, "warn": 0.3}


def score(it):
    return WEIGHT.get(it["kind"], 1.0) * it["count"]


def load_track():
    try:
        return json.load(open(TRACK))
    except Exception:
        return {}


def save_track(tr):
    json.dump(tr, open(TRACK, "w"), indent=1)


def merged(hours):
    issues = scan(hours)
    tr = load_track()
    for iid, it in issues.items():
        rec = tr.setdefault(iid, {"status": "open", "first_seen": it["first"]})
        rec["last_seen"] = max(rec.get("last_seen", 0), it["last"])
        rec["kind"] = it["kind"]
        rec["count_window"] = it["count"]
        if rec["status"] == "fixed" and it["last"] > rec.get("fixed_at", 0) + 60:
            rec["status"] = "regressed"
            rec["regressed_at"] = it["last"]
        it["status"] = rec["status"]
        it["note"] = rec.get("note", "")
        # after a fix only occurrences since then count
        if rec.get("fixed_at"):
            it["count_since_fix"] = sum(1 for _ in it["examples"] if _["t"] > rec["fixed_at"])
    save_track(tr)
    return issues, tr


def fmt_t(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def cmd_scan(args, as_json=False):
    hours = float(args[args.index("--hours") + 1]) if "--hours" in args else 24.0
    issues, _ = merged(hours)
    ranked = sorted(((iid, it) for iid, it in issues.items() if it["status"] != "wontfix"),
                    key=lambda p: -score(p[1]))
    if as_json:
        out = []
        for iid, it in ranked[:40]:
            out.append({"id": iid, "kind": it["kind"], "count": it["count"], "score": round(score(it), 1),
                        "status": it["status"], "note": it["note"], "repeats": it.get("repeats", 0), "first": fmt_t(it["first"]),
                        "last": fmt_t(it["last"]), "examples": [dict(e, t=fmt_t(e["t"])) for e in it["examples"]]})
        print(json.dumps(out, indent=1))
        return
    print(f"issues in the last {hours:g} h, ranked (score = weight x count)")
    for kind in ("death", "error", "nav", "step", "warn"):
        rows = [(iid, it) for iid, it in ranked if it["kind"] == kind]
        if not rows:
            continue
        print(f"\n{kind.upper()} ({sum(it['count'] for _, it in rows)})")
        for iid, it in rows[:12 if kind != "warn" else 8]:
            flag = {"open": "", "fixed": " [fixed]", "regressed": " [REGRESSED]"}.get(it["status"], "")
            rep = f" (+{it['repeats']} repeats)" if it.get("repeats") else ""
            print(f"  {score(it):6.1f}  x{it['count']:<3} {iid[:70]}{flag}{rep}   last {fmt_t(it['last'])}")


def cmd_show(args):
    iid = args[0]
    n = int(args[1]) if len(args) > 1 else 3
    hours = 24 * 30
    issues = scan(hours)
    hits = [k for k in issues if k == iid] or [k for k in issues if iid in k]
    if not hits:
        print("no such issue in the last 30 days")
        return
    for k in hits[:3]:
        it = issues[k]
        rec = load_track().get(k, {})
        print(f"== {k}  x{it['count']}  {rec.get('status', 'open')}  {rec.get('note', '')}")
        files = {os.path.basename(f): f for f in log_files()}
        for ex in it["examples"][-n:]:
            print(f"-- {fmt_t(ex['t'])} {ex['file']}:{ex['line']}  {ex['text']}")
            f = files.get(ex["file"])
            if f:
                lines = read_log(f)
                for ts, t in lines[max(0, ex["line"] - 25):ex["line"] + 3]:
                    if not IGNORE.search(t):
                        print(f"   {dt.datetime.fromtimestamp(ts).strftime('%H:%M:%S')} {t}")


def cmd_mark(args, status):
    tr = load_track()
    hits = [k for k in tr if k == args[0]] or [k for k in tr if args[0] in k]
    if not hits:
        print("no tracked issue matches - run scan first")
        return
    for k in hits:
        tr[k].update(status=status, note=" ".join(args[1:]), fixed_at=time.time())
        print(f"{k}: {status}")
    save_track(tr)


def cmd_list(args):
    tr = load_track()
    order = {"regressed": 0, "open": 1, "fixed": 2, "wontfix": 3}
    for k, r in sorted(tr.items(), key=lambda p: (order.get(p[1]["status"], 9), -p[1].get("last_seen", 0))):
        if "--all" not in args and r["status"] in ("fixed", "wontfix") and \
                time.time() - r.get("fixed_at", 0) > 7 * 86400:
            continue
        print(f"  {r['status']:9} {k[:70]:70} last {fmt_t(r.get('last_seen', 0))}  {r.get('note', '')}")


COMMANDS = {
    "scan": cmd_scan,
    "json": lambda a: cmd_scan(a, as_json=True),
    "show": cmd_show,
    "fixed": lambda a: cmd_mark(a, "fixed"),
    "wontfix": lambda a: cmd_mark(a, "wontfix"),
    "list": cmd_list,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
