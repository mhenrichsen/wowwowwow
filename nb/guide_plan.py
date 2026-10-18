"""Drive the nb quest planner from the RestedXP Dwarf/Gnome route.

The route (rxp.py parses the RXPGuides Lua) gives the ORDER: which quest to pick up, hunt, hand in and
when to grind a level. The real work is still done by QuestDB.plan(), asked about one quest at a time, so
the driver returns the same tuples ('talk' | 'hunt' | 'use' | 'gather' | None).

State is read from the live quest log every call (no stored position): a step is satisfied by what the
log, the hand-in set and the level say. Anything the driver cannot decide falls back to the normal planner,
and so does any error. Modes (file nb/GUIDE_MODE, default 'shadow'):

    on      the guide decides
    shadow  the guide's decision is only logged ("guide: ... | planner: ..."), the planner still drives
    off     the planner alone (also: create the file nb/GUIDE_OFF to switch it off without a restart)
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import rxp                 # noqa: E402

OFF_FILE = os.path.join(HERE, "GUIDE_OFF")
MODE_FILE = os.path.join(HERE, "GUIDE_MODE")
# RestedXP v4.11.23 Forever route, the walkable part: the Elwynn/Westfall/Darkshore legs need a flight, the tram or a
# boat, so after Dun Morogh the route continues in Loch Modan and the planner fills the rest of Loch Modan to 20
from nb import route   # noqa: E402
GUIDES = route.FILE_GUIDES     # (dwarf: "1-5 Coldridge Valley", "5-11 Dun Morogh", "12-14 Loch Modan (Dwarf/Gnome)")
rxp.GUIDE_DIR = rxp.FOREVER_DIR
# quests the route names but this server / our bot cannot do (the planner's own SKIP list is added)
BLOCKED = {384, 308, 314,
           97277,           # Grund and Gozwin: its giver (2756) has npcflag 0 on this server (no quest offer)
           86614}           # not on server: no quest_template row in tcf_world or mangos (QUEST_PLAN.md track S #9)
# (413 Shimmer Stout is back in: no reason was ever given for it; MinLevel 8, so the route's own "QA 413 needs level 8:
# grind" applies, and 414 follows it. 432/433 are back in behind LEVEL_GATE below: quest_poi now keeps their hunts to
# the objective area, quests._spawns_for)
# (218 The Stolen Journal was blocked while Grik'nir's Frost Shock hit ~174-210: the server's SpellInfo.cpp
# UsesClassicFlatBasePoints bug made creature spells ~5.8x too strong; fixed 2026-10-14 16:30, now ~19 dmg.
# Grik'nir is pulled from Bot.BOSS_MIN_LEVEL on, through the can_hunt gate.)
# step actions that need nothing from us (bags, spells, travel: nb handles them elsewhere)
IGNORED = {"DESTROY", "CI", "USE", "CAST", "V", "LE", "S", "H", "F", "P", "UI", "AB", "GO"}
# quests whose mobs out-fight the character below this level (died twice to the Grizzled Den wendigos at level 7)
# 432 Those Blasted Troggs! (QuestLevel 9) and 433 The Public Servant (QuestLevel 11) at 8 / 9. 319 A Favor for
# Evershine is out of the gate: no deaths near its mobs, and it now runs at its route step 45 instead of a second trip
LEVEL_GATE = {313: 8, 432: 8, 433: 9}
GUIDE_END = 99           # cut the route at its "grind to N" step (99 = run the whole route)
# quest items a ".use" step may ask for -> the BotEye key whose macro uses it (BotEye.lua keymacros)
ITEM_KEYS = {6635: "insert"}      # Earth Sapta (Call of Earth 94374)
USE_COOLDOWN = 120                # s after a use before the same step may ask again
STUCK_MIN = {"talk": 8, "hunt": 25, "gather": 25, "use": 10, "grind": 20}
try:
    from nb import inst as _inst
    STATE_FILE = _inst.path("guide_state.json")
except Exception:
    STATE_FILE = os.path.join(HERE, "guide_state.json")


def load_state() -> dict:
    """The guide / hunt state the last run left (nb.bot writes it): {} when there is none or it is unreadable."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(d: dict):
    """Write atomically (a crash mid-write must not leave half a file for the next start)."""
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def fp_key(fp) -> str:
    """A progress fingerprint as a string that survives a JSON round trip."""
    return json.dumps(fp, separators=(",", ":"))


def read_mode() -> str:
    try:
        m = open(MODE_FILE).read().strip().lower()
        return m if m in ("on", "shadow", "off") else "shadow"
    except OSError:
        return "shadow"


class GuideDriver:
    def __init__(self, qdb, log, blocked=None, mode="shadow", who=None, can_hunt=None, state=None):
        self.qdb, self.log, self.blocked, self.mode = qdb, log, blocked or (lambda npc, pos, lvl: False), mode
        # can_hunt(quest, mobs, points, level) -> False: the hunt is not for now (a boss we may not pull yet, every
        # target indoors, shelved). The step is then left like a satisfied one and the route goes on
        self.can_hunt = can_hunt or (lambda q, mobs, pts, lvl: not qdb.is_shelved(q))
        self.steps: list = []
        self.passed: dict[int, int] = {}                     # step -> character level when the stuck budget passed it
        self.opt: set[int] = set()                           # mobs of optional (completewith) objectives
        self.since: dict[int, tuple] = {}                    # step -> (progress fingerprint key, wall-clock start)
        self.used: dict[int, float] = {}                     # step -> time its item was last used
        self._last = None
        self.skip = set(BLOCKED)
        try:
            from quests import SKIP as _skip
        except Exception:
            try:
                from nb.quests import SKIP as _skip
            except Exception:
                _skip = set()
        self.skip |= set(_skip)
        if mode == "off":
            return
        try:
            who = who or route.who()
            steps = []
            for name in GUIDES:
                steps += rxp.get(name, who).steps
            rxp.resolve_npcs(steps)
            cut = len(steps)
            for i, s in enumerate(steps):                    # the route's own end: "grind to 10"
                if any(t == "XP" and a.split("+")[0].split(".")[0] == str(GUIDE_END) for t, a in s.actions):
                    cut = i + 1
                    break
            self.steps = steps[:cut]
            self.log(f"guide: {len(self.steps)} route steps loaded ({mode})")
        except Exception as e:                               # a guide problem must never cost the bot
            self.log(f"guide: could not load the route ({e!r}); using the planner")
            self.steps, self.mode = [], "off"
        self.load(load_state() if state is None else state)

    # ---------------------------------------------------------------- state across restarts
    def export(self) -> dict:
        """Passed steps (with the level and route line they were passed at) and the stuck clocks, as ages: the
        time the bot was down does not count as time stuck."""
        now = time.time()
        ln = lambda i: self.steps[i].line_no if 0 <= i < len(self.steps) else -1
        return {"passed": {str(i): [lvl, ln(i)] for i, lvl in self.passed.items()},
                "since": {str(i): [k, now - t, ln(i)] for i, (k, t) in self.since.items()},
                "used": {str(i): t for i, t in self.used.items()}}

    def load(self, d: dict):
        """Back from export(); entries for another route (line number moved) are dropped."""
        g = (d or {}).get("guide") or {}
        now = time.time()
        ok = lambda i, ln: 0 <= i < len(self.steps) and self.steps[i].line_no == ln
        try:
            for k, (lvl, ln) in (g.get("passed") or {}).items():
                if ok(int(k), ln):
                    self.passed[int(k)] = int(lvl)
            for k, (key, age, ln) in (g.get("since") or {}).items():
                if ok(int(k), ln):
                    self.since[int(k)] = (key, now - float(age))
            for k, t in (g.get("used") or {}).items():
                self.used[int(k)] = float(t)
        except Exception as e:
            self.log(f"guide: could not read the saved guide state ({e!r}); starting clean")
            self.passed, self.since, self.used = {}, {}, {}
        if self.passed or self.since:
            self.log(f"guide: restored {len(self.passed)} passed step(s) {sorted(self.passed)} and {len(self.since)} stuck clock(s)")

    # ---------------------------------------------------------------- public
    def plan(self, me, lqs):
        real = self.qdb.plan  # the planner mutates focus/_counts: shadow mode restores them
        self.opt = set()
        if self.mode == "off" or not self.steps or os.path.exists(OFF_FILE):
            return real(me, lqs)
        if getattr(me, "nquests", 0) > len(lqs):             # quest log not fully read yet
            return real(me, lqs)
        focus, counts = self.qdb.focus, dict(getattr(self.qdb, "_counts", {}))
        try:
            kind, res, why = self._decide(me, lqs)
        except Exception as e:
            self.log(f"guide: error {e!r}; using the planner this tick")
            self.qdb.focus, self.qdb._counts = focus, counts
            return real(me, lqs)
        if self.mode == "shadow":
            plain = None
            self.qdb.focus, self.qdb._counts = focus, counts
            plain = real(me, lqs)
            self._note(why, res, plain)
            self.qdb.focus, self.qdb._counts = focus, counts
            return plain
        if kind == "defer":
            self.qdb.focus, self.qdb._counts = focus, counts
            return real(me, lqs)
        self._note(why, res, None)
        return res                                           # a tuple, or None = grind

    # --------------------------------------------------------------- internals
    def _note(self, why, res, plain):
        sig = (why, res[0] if res else None, res[1] if res and len(res) > 1 and not isinstance(res[1], set) else None)
        if sig == self._last and plain is None:
            return
        self._last = sig
        self.log(f"guide: {why} -> {self._short(res)}" + (f" | planner: {self._short(plain)}" if self.mode == "shadow" else ""))

    @staticmethod
    def _short(p):
        if not p:
            return "grind/none"
        if p[0] == "hunt":
            return f"hunt {p[3]}"
        return f"{p[0]} {p[3] if len(p) > 3 else ''}"

    def _decide(self, me, lqs):
        """-> (kind, tuple|None, why); kind 'act' (use the tuple, None = grind) or 'defer' (use the planner)."""
        qdb = self.qdb
        log = {q.qid: q for q in lqs}
        done = qdb.done
        for d in list(done):                                 # a finished follow-up means its predecessor is done too
            p = qdb.q.get(d, {}).get("prev", 0)
            if p and p not in done:
                done.add(p)
        ids = set(log)
        now = time.time()
        for i, lvl in list(self.passed.items()):
            if lvl < me.level:                               # passed at a lower level: a level-up may make it doable
                del self.passed[i]
                self.log(f"guide: level {me.level}: step {i} (passed at level {lvl}) is back on the route")
        for i, st in enumerate(self.steps):
            if i in self.passed:
                continue
            if st.min_level and me.level < st.min_level:
                return "act", None, f"step {i} needs level {st.min_level}: grind"
            for tag, args in st.actions:
                r = self._action(i, st, tag, args, me, lqs, log, done, ids)
                if r is None:
                    continue                                # satisfied / ignorable: next action
                kind, res, why = r
                # stuck budget: the same step with nothing changing for too long is passed
                # a grind step (res None) progresses with every kill's xp: without it in the fingerprint inst3
                # 16:46:14 passed "XP 7: grind to 7" as stuck while it was grinding towards 7
                fp = fp_key((sorted(ids), [list(q.counts) for q in lqs], len(done), me.level,
                             getattr(me, "xp", 0) if res is None else 0))
                prev = self.since.get(i)
                if not prev or prev[0] != fp:
                    self.since[i] = (fp, now)
                elif now - prev[1] > 60 * STUCK_MIN.get(res[0] if res else "grind", 15):
                    self.log(f"guide: step {i} ({why}) made no progress for {int((now - prev[1]) / 60)} min; skipping it")
                    self.passed[i] = me.level
                    self.since.pop(i, None)
                    continue
                return kind, res, f"step {i}/{len(self.steps)} L{st.line_no} {why}"
        return "defer", None, "route finished"

    def _action(self, i, st, tag, args, me, lqs, log, done, ids):
        qdb = self.qdb
        if tag == "USE":
            r = self._use_item(i, st, args, log)
            if r:
                return r
        if tag in IGNORED:
            return None
        if tag == "XP":
            try:
                target = int(float(args.split("+")[0]))
                extra = int(args.split("+")[1].split()[0]) if "+" in args else 0    # ".xp 4+640": level 4 and 640 xp into it
            except (ValueError, IndexError):
                return None
            if me.level < target:
                return "act", None, f"{tag} {args}: grind to {target}"
            xp = getattr(me, "xp", None)
            if extra and me.level == target and isinstance(xp, int) and getattr(me, "xpmax", 0) and xp < extra:
                return "act", None, f"{tag} {args}: grind to {extra} xp (at {xp})"
            return None
        if tag not in ("QA", "QT", "QC"):
            return None
        q = int(args.split(",")[0])
        if q in self.skip or q not in qdb.q:
            return None
        if tag in ("QC", "QA") and me.level < LEVEL_GATE.get(q, 0):
            return None                                      # too strong for now: the route moves on, the gate lifts at the level
        info = qdb.q[q]
        if tag == "QA":
            if q in ids or q in done:
                return None
            # not one level early: the giver offers nothing yet and the visit is wasted (26% of NPC talks changed
            # nothing, 2026-10-14 runs)
            if not qdb.eligible(q, 99, ids) or info["min"] > me.level:
                return None if info["min"] <= me.level else ("act", None, f"QA {q} needs level {info['min']}: grind")
            starters = qdb.starters.get(q, set())
            npc = next((n for n in st.npcs if n in starters), None) or next(iter(starters), None)
            near = (me.x, me.y)
            if npc is None:
                # no creature gives it: an object may (2059 Dwarven wreck -> 417 A Pilot's Revenge, QUEST_PLAN.md WP2
                # #4). Not autocomplete quests (QuestType 0, 403): they never show in the log, so the step could not end
                if info.get("auto"):
                    return None
                gos = [(go, p) for go, p in getattr(qdb, "go_starters", {}).get(q, ())
                       if go not in qdb.skip_go and qdb.in_area(p)]
                if not gos:
                    return None
                go, p = min(gos, key=lambda g: math.dist(near, g[1][:2]))
                return "act", ("use", go, p, f"pick up {info['title']} at an object"), f"QA {q}"
            pos = qdb.npc_pos(npc, near)
            if not pos:
                return None
            if self.blocked(npc, pos, me.level):
                return None                                  # giver shelved for now: the route goes on, the step comes back (no planner: user wants the guide only)
            return "act", ("talk", npc, pos, f"pick up {info['title']}"), f"QA {q}"
        if tag == "QT":
            if q in done or q not in ids:
                return None
            lq = log[q]
            if not lq.complete and me.level < LEVEL_GATE.get(q, 0):
                return None                                  # gated quest: not before its level
            if not lq.complete and (info["kill"] or info["items"] or info.get("special")
                                    or getattr(qdb, "explore", {}).get(q)):
                return self._complete(q, me, lqs, st, f"QT {q} (not finished yet)")
            res = qdb.plan(me, lqs, only={q}, pickups=False)
            if not res:
                return None
            if res[0] == "talk" and self.blocked(res[1], res[2], me.level):
                return None                                  # ender shelved for now: next route step, back here when it expires
            return "act", res, f"QT {q}"
        # QC
        if q not in ids or q in done:
            return None
        return self._complete(q, me, lqs, st, f"QC {args}")

    def _use_item(self, i, st, args, log):
        """A '.use <item>' step at a spot while on a quest (Earth Sapta at the Spirit Stone summons the Minor
        Manifestation of Earth for Call of Earth). Only items with a BotEye key (ITEM_KEYS); once used, the step
        rests USE_COOLDOWN s so the next step (talk to what it summoned) can run."""
        try:
            item = int(args.split(",")[0])
        except ValueError:
            return None
        key = ITEM_KEYS.get(item)
        quests = [q for tag, v in st.requires if tag == "isOnQuest" for q in (v if isinstance(v, list) else [])]
        if not key or not st.goto or not quests or any(q in self.skip for q in quests):
            return None
        if not any(q in log for q in quests):        # (a talk-only quest reads complete at once: do not test that)
            return None
        if time.time() - self.used.get(i, 0) < USE_COOLDOWN:
            return None
        a, b, zone = st.goto
        if not str(zone).endswith("/0"):
            return None                                  # only world coordinates ("1426/0": y, x) are understood
        # the route's own way there: waypoints of the steps since the last hand-in/pick-up (the Spirit Stone
        # trail; a single fallback path looped at the foot of the hill, 13:32)
        via = []
        for j in range(i - 1, -1, -1):
            pst = self.steps[j]
            if any(t in ("QA", "QT") for t, _ in pst.actions):
                break
            for zn, ga, gb, _r in reversed(pst.gotos):
                if str(zn).endswith("/0"):
                    via.append((gb, ga, None))
        via.reverse()
        return "act", ("useitem", key, (b, a, None), f"use item {item} for quest {quests[0]}", i, via), f"USE {item}"

    def note_used(self, step_i):
        self.used[step_i] = time.time()

    def _complete(self, q, me, lqs, st, why):
        lq = next((x for x in lqs if x.qid == q), None)
        if lq is None or lq.complete:
            return None
        if getattr(st, "completewith", None):               # optional en-route work: kill its mobs when they are met
            focus, counts = self.qdb.focus, dict(getattr(self.qdb, "_counts", {}))
            try:
                res = self.qdb.plan(me, lqs, only={q}, pickups=False)
                if res and res[0] == "hunt" and self.can_hunt(q, res[1], res[2], me.level):
                    self.opt |= set(res[1])
            except Exception:
                pass
            self.qdb.focus, self.qdb._counts = focus, counts
            return None
        try:
            n = int(why.split(",")[1].split()[0]) if why.startswith("QC") and "," in why else 0
        except ValueError:
            n = 0
        if n and n <= 3 and n <= len(lq.counts) and lq.counts[n - 1] == 255:
            return None                                     # that objective is already filled
        res = self.qdb.plan(me, lqs, only={q}, pickups=False)
        if not res:
            return None
        if res[0] == "hunt" and not self.can_hunt(q, res[1], res[2], me.level):
            return None                                     # not pullable / reachable now: the route goes on, back here later
        return "act", res, why
