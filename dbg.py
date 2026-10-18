"""Debugging and tuning the bot: one place to ask it questions.

    python dbg.py status              live snapshot: level, xp/h, guide step, objectives
    python dbg.py watch [secs] [--no-stuck]
                                      wait (default 540 s) for a death, level-up, stop,
                                      exception or a burst of stuck reports; then status
    python dbg.py report [--all]      this run (or every run): xp/h, time per activity,
                                      xp from kills vs quests, slowest steps, skips, stuck
                                      spots, deaths
    python dbg.py steps [n]           the last n guide steps: time and xp each
    python dbg.py tail [n]            the last n events
    python dbg.py quest <id|name>     quest from the world DB + where the guide uses it
    python dbg.py mob <id|name>       creature: level, rank, spawns nearest to us
    python dbg.py guide [name] [a-b]  guide steps as the bot reads them

The numbers come from data/events.jsonl (events.py), not from bot.log.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import Counter

import events
import worlddb

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "status.json")
BOT_LOG = os.path.join(HERE, "bot.log")

_xp_table: dict[int, int] | None = None


def xp_table() -> dict[int, int]:
    global _xp_table
    if _xp_table is None:
        _xp_table = {int(l): int(x) for l, x in worlddb._query(
            "SELECT lvl, xp_for_next_level FROM player_xp_for_level WHERE lvl < 60")}
    return _xp_table


def total_xp(lvl: int, xp: int) -> int:
    """xp earned since level 1, so gains across a level-up add up."""
    t = xp_table()
    return sum(t.get(l, 0) for l in range(1, lvl)) + xp


def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def status_file() -> dict:
    try:
        return json.load(open(STATUS, encoding="utf-8"))
    except Exception:
        return {}


def run_events(all_runs=False) -> list[dict]:
    evs = list(events.read())
    if not evs or all_runs:
        return evs
    last = evs[-1]["run"]
    return [e for e in evs if e["run"] == last]


# ------------------------------------------------------------------ status
def cmd_status(_args=None):
    s = status_file()
    if not s:
        print("no status.json")
        return
    age = time.time() - s.get("ts", 0)
    live = s.get("running") and age < 10
    print(f"{s.get('name')}  level {s.get('level')}  xp {s.get('xp')}/{s.get('next_xp')}  "
          f"{s.get('xp_per_hour') or '?'} xp/h  deaths {s.get('deaths')}  kills {s.get('kills')}  "
          f"{s.get('money', 0)}c  up {hms(s.get('uptime', 0))}  "
          f"{s.get('state') if live else 'NOT RUNNING'} ({age:.0f}s old)")
    g = s.get("guide")
    if not g:
        print("no guide step published")
        return
    print(f"guide {g.get('chain_pos')}/{g.get('chain_len')} '{g['guide']}'  "
          f"step {g['step']}/{g['steps']}: {g.get('text', '')[:90]}")
    marks = {"done": "x", "current": ">", "todo": " ", "skipped": "-"}
    for a in g.get("actions", []):
        prog = ", ".join(f"{p['label']} {p['have']}/{p['need']}" if p.get("need") is not None
                         else p["label"] for p in a.get("progress", []))
        print(f"  [{marks.get(a['state'], '?')}] {a['label']}" + (f"  ({prog})" if prog else ""))
    for n in g.get("next", [])[:3]:
        print(f"   next: {n[:110]}")


# ------------------------------------------------------------------ watch
def cmd_watch(args):
    quiet_stuck = "--no-stuck" in args
    args = [a for a in args if a != "--no-stuck"]
    limit = int(args[0]) if args else 540
    n0 = sum(1 for _ in events.read())
    try:
        log0 = os.path.getsize(BOT_LOG)
    except OSError:
        log0 = 0
    t0, why = time.time(), "timeout"
    while time.time() - t0 < limit:
        new = list(events.read())[n0:]
        kinds = Counter(e["ev"] for e in new)
        if kinds["death"]:
            why = "DEATH"
        elif kinds["level"]:
            why = "LEVEL UP"
        elif kinds["stuck"] >= 6 and not quiet_stuck:
            why = f"STUCK x{kinds['stuck']}"
        else:
            try:
                with open(BOT_LOG, "rb") as f:
                    f.seek(log0)
                    tail = f.read().decode("utf-8", "replace")
            except OSError:
                tail = ""
            if "stopped." in tail:
                why = "BOT STOPPED"
            elif "Traceback" in tail or " raised " in tail:
                why = "EXCEPTION"
        if why != "timeout":
            break
        time.sleep(5)
    new = list(events.read())[n0:]
    kinds = Counter(e["ev"] for e in new)
    print(f"== {why} after {time.time() - t0:.0f}s ({time.strftime('%H:%M')})  "
          f"kills {kinds['kill']}  steps {kinds['step']}  stuck {kinds['stuck']}")
    for e in new:
        if e["ev"] in ("death", "level") or (e["ev"] == "action" and e["result"] != "done"):
            print("  " + fmt_event(e))
    cmd_status()


# ------------------------------------------------------------------ report
def cmd_report(args):
    evs = run_events(all_runs="--all" in args)
    if not evs:
        print("no events yet")
        return
    s = status_file()
    alive = s.get("running") and time.time() - s.get("ts", 0) < 10
    t_end = time.time() if alive else evs[-1]["t"]
    t_beg = evs[0]["t"]
    start = next((e for e in evs if e["ev"] == "start"), {})
    xp_pts = [(e["t"], total_xp(e["lvl"], e["xp"]), e) for e in evs if "lvl" in e and "xp" in e]
    if alive and s.get("level"):
        xp_pts.append((t_end, total_xp(s["level"], s["xp"]), {"ev": "now"}))
    gained = xp_pts[-1][1] - xp_pts[0][1] if len(xp_pts) > 1 else 0
    hours = max(1e-6, (t_end - t_beg) / 3600)
    lv0 = xp_pts[0][2].get("lvl") if xp_pts else "?"
    lv1 = s.get("level") if alive else (xp_pts[-1][2].get("lvl") if xp_pts else "?")
    print(f"{start.get('char', '?')}: {hms(t_end - t_beg)}, level {lv0} -> {lv1}, "
          f"{gained} xp = {gained / hours:,.0f} xp/h"
          + ("" if alive else "  (not running)"))

    # where the xp came from: each gain goes to the event that reported it
    src = Counter()
    for (_, a, _), (_, b, e) in zip(xp_pts, xp_pts[1:]):
        if b > a:
            src["kills" if e["ev"] == "kill" else
                "quests" if e["ev"] == "action" and e.get("tag") == "QT" else "other"] += b - a
    if gained:
        print("xp from: " + ", ".join(f"{k} {v} ({100 * v / gained:.0f}%)" for k, v in src.most_common()))

    # time per activity, from state changes
    st = [e for e in evs if e["ev"] == "state"]
    per = Counter()
    for a, b in zip(st, st[1:] + [{"t": t_end}]):
        per[a["s"]] += b["t"] - a["t"]
    tot = sum(per.values()) or 1
    print("time:    " + ", ".join(f"{k} {hms(v)} ({100 * v / tot:.0f}%)" for k, v in per.most_common(8)))

    kills = [e for e in evs if e["ev"] == "kill"]
    if kills:
        avg = sum(e["secs"] for e in kills) / len(kills)
        loss = sum(e.get("hp0", 0) - e.get("hp1", 0) for e in kills) / len(kills)
        print(f"kills:   {len(kills)}, {avg:.0f}s each, -{loss:.0f}% hp each; "
              + ", ".join(f"{m} x{n}" for m, n in Counter(e["mob"] for e in kills).most_common(5)))

    steps = step_rows(evs, t_end)
    if steps:
        print(f"steps:   {len(steps)} ({sum(1 for r in steps if r['skipped'])} skipped on arrival); slowest:")
        for r in sorted(steps, key=lambda r: -r["secs"])[:6]:
            print(f"   {hms(r['secs']):>7} {r['xp']:>5}xp  {r['guide']} #{r['step']}: {r['text'][:60]}")
    bad = [e for e in evs if e["ev"] == "action" and e["result"] != "done"]
    if bad:
        print(f"not done: {len(bad)}")
        for e in bad[-8:]:
            print("   " + fmt_event(e))
    stuck = [e for e in evs if e["ev"] == "stuck"]
    if stuck:
        spots = Counter((round(e["x"] / 25) * 25, round(e["y"] / 25) * 25) for e in stuck)
        print(f"stuck:   {len(stuck)} reports; worst spots: "
              + ", ".join(f"({x},{y}) x{n}" for (x, y), n in spots.most_common(4)))
    for e in (e for e in evs if e["ev"] == "death"):
        print("death:   " + fmt_event(e))


def step_rows(evs, t_end) -> list[dict]:
    """One row per guide step reached: how long we were on it, xp gained there."""
    st = [e for e in evs if e["ev"] == "step"]
    pts = [(e["t"], total_xp(e["lvl"], e["xp"])) for e in evs if "lvl" in e and "xp" in e]
    rows = []
    for a, b in zip(st, st[1:] + [{"t": t_end}]):
        inside = [x for t, x in pts if a["t"] <= t <= b["t"]]
        rows.append({"guide": a["guide"], "step": a["step"], "text": a.get("text", ""),
                     "skipped": a.get("skipped"), "secs": b["t"] - a["t"],
                     "xp": (max(inside) - min(inside)) if len(inside) > 1 else 0})
    return rows


def cmd_steps(args):
    n = int(args[0]) if args else 15
    evs = run_events()
    s = status_file()
    t_end = time.time() if s.get("running") else (evs[-1]["t"] if evs else time.time())
    for r in step_rows(evs, t_end)[-n:]:
        print(f"{hms(r['secs']):>7} {r['xp']:>5}xp  #{r['step']:<3} "
              + (f"(skipped: {r['skipped']}) " if r["skipped"] else "") + r["text"][:80])


def fmt_event(e) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(e["t"]))
    k = e["ev"]
    if k == "action":
        return (f"{t} step {e['step']} [{e['tag']} {e['args']}] {e['result']} after {e.get('secs', 0):.0f}s"
                + (f" ({e['why']})" if e.get("why") else ""))
    if k == "kill":
        return f"{t} killed {e['mob']} ({e.get('mlvl')}) in {e['secs']:.0f}s, hp {e.get('hp0')}->{e.get('hp1')}%"
    if k == "step":
        return f"{t} step {e['step']}: {e.get('text', '')[:70]}" + (f"  SKIPPED ({e['skipped']})" if e.get("skipped") else "")
    if k == "death":
        return f"{t} DIED (#{e['n']}) at level {e.get('lvl')} at ({e['x']}, {e['y']}), step {e.get('step')}"
    rest = {a: b for a, b in e.items() if a not in ("t", "run", "ev")}
    return f"{t} {k} {rest}"


def cmd_tail(args):
    n = int(args[0]) if args else 25
    for e in list(events.read())[-n:]:
        print(fmt_event(e))


# ------------------------------------------------------------------ world DB lookups
def _player():
    s = status_file()
    p = s.get("pos")
    return (p[0], p[1]) if isinstance(p, (list, tuple)) and len(p) >= 2 else None


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def cmd_quest(args):
    import guide
    arg = " ".join(args)
    if arg.isdigit():
        ids = [int(arg)]
    else:
        ids = [int(r[0]) for r in worlddb._query(
            f"SELECT DISTINCT entry FROM quest_template WHERE Title LIKE '%{_esc(arg)}%' LIMIT 8")]
    if not ids:
        print("no such quest")
        return
    me = _player()
    for qid, q in guide.quests(ids).items():
        print(f"[{qid}] {q['title']}  level {q['level']} (min {q['min_level']})  xp {q.get('xp')}")
        rows = worlddb._query(f"SELECT PrevQuestId, NextQuestId, Type FROM quest_template "
                              f"WHERE entry={qid} ORDER BY patch DESC LIMIT 1")
        if rows:
            print(f"   prev {rows[0][0]}  next {rows[0][1]}  type {rows[0][2]}")
        for c, n in q["kill"]:
            print(f"   kill {n} x {worlddb.name(c)} ({c})")
        for o, n in q["use_object"]:
            print(f"   use {n} x object {o}")
        for it, n in q["collect"]:
            src = guide.item_sources([it]).get(it, {})
            print(f"   collect {n} x item {it}  (dropped by {len(src.get('creatures', ()))} kinds, "
                  f"{len(src.get('objects', ()))} objects)")
        for role, ids_ in (("from", q["starter_npcs"]), ("to", q["ender_npcs"])):
            for e in ids_[:3]:
                sp = worlddb._query(f"SELECT map, position_x, position_y FROM creature WHERE id={e} LIMIT 1")
                where = ""
                if sp:
                    m, x, y = int(sp[0][0]), float(sp[0][1]), float(sp[0][2])
                    where = f" map {m} ({x:.0f}, {y:.0f})" + (f", {math.dist(me, (x, y)):.0f} yd away" if me else "")
                print(f"   {role} {worlddb.name(e)} ({e}){where}")
        uses = guide_uses(qid)
        print("   guide: " + (", ".join(uses[:8]) if uses else "not in the Dwarf Warrior chain"))


def guide_uses(qid: int) -> list[str]:
    import rxp
    who = rxp.Who("Warrior", "Dwarf", "Alliance")
    cat = rxp.catalog(who)
    out = []
    for name in rxp.chain(who):
        for i, s in enumerate(cat[name].steps, 1):
            for t, a in s.actions:
                if t in ("QA", "QT", "QC") and a.split(",")[0].strip() == str(qid):
                    out.append(f"{name} #{i} {t}")
    return list(dict.fromkeys(out))


def cmd_mob(args):
    arg = " ".join(args)
    where = f"entry={int(arg)}" if arg.isdigit() else f"name LIKE '%{_esc(arg)}%'"
    rows = worlddb._query(f"SELECT entry, name, level_min, level_max, rank, faction FROM creature_template "
                          f"WHERE {where} GROUP BY entry LIMIT 8")
    me = _player()
    for e, n, lo, hi, rank, fac in rows:
        sp = [(int(m), float(x), float(y)) for m, x, y in worlddb._query(
            f"SELECT map, position_x, position_y FROM creature WHERE id={e}")]
        print(f"[{e}] {n}  level {lo}-{hi}{'  ELITE' if int(rank) else ''}  faction {fac}  {len(sp)} spawns")
        if me and sp:
            near = sorted(sp, key=lambda p: math.dist(me, p[1:]))[:3]
            print("   nearest: " + ", ".join(f"({x:.0f}, {y:.0f}) {math.dist(me, (x, y)):.0f} yd"
                                            for _, x, y in near))


def cmd_guide(args):
    import rxp
    who = rxp.Who("Warrior", "Dwarf", "Alliance")
    rng = next((a for a in args if "-" in a and a.replace("-", "").isdigit()), None)
    name = " ".join(a for a in args if a != rng).replace("_", " ") or None
    if not name:
        name = (status_file().get("guide") or {}).get("guide") or rxp.entry_guide(who)
    g = rxp.get(name, who)
    a, b = (int(x) for x in rng.split("-")) if rng else (1, len(g.steps))
    print(f"{g.name}: {len(g.steps)} steps, next '{g.next_guide}', dropped {g.dropped}")
    for i, s in enumerate(g.steps[a - 1:b], a):
        acts = " ".join(f"{t}:{x}" for t, x in s.actions)
        gate = f" if {s.requires}" if s.requires else ""
        print(f"{i:3} {acts[:70]:70}{gate}  | {s.text[:60]}")


COMMANDS = {"status": cmd_status, "watch": cmd_watch, "report": cmd_report, "steps": cmd_steps,
            "tail": cmd_tail, "quest": cmd_quest, "mob": cmd_mob, "guide": cmd_guide}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
