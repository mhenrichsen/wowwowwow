"""Quest runner: executes a RestedXP guide (rxp.py) step by step.

Mixed into MageBot next to Combat. The guide decides WHAT to do and in which
order; the world DB (guide.quests / item_sources) says HOW; the live quest
log in memory says whether it worked. Everything else - fighting back,
looting, resting, dying, training - stays in the main loop, which calls
quest_tick() whenever there is nothing more urgent to do.
"""

from __future__ import annotations

import json
import math
import os
import re
import time

import bridge as br
import events
import factions
import gear
import spells
import guide as guides
import rxp
import worlddb
import worldmap
from combat import add_radius

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRESS_DIR = os.path.join(HERE, "data")
QUEST_COMPLETE = 0x01000000        # quest log state word: objectives done
CTM_INTERACT_OBJECT = 7
SEARCH_RADIUS = 60.0
# The add radius lives in combat.py and is scaled by level
# (combat.add_radius). Quest-mob selection used a flat 24 yd of its own, so
# the scaling written to stop a level-1 warrior rejecting every trogg in
# Coldridge never reached the code that actually picks quest targets.

def _log(msg):
    from combat import _bot          # the __main__ bot module, not a second copy
    _bot().log(msg)


def _state(state, **kw):
    from combat import _bot
    _bot().set_state(state, **kw)


def _qid(args: str) -> int:
    """Leading number of a guide action: "92,3" (quest 92, objective 3) -> 92."""
    import re
    m = re.match(r"\s*(\d+)", args)
    if not m:
        raise ValueError(f"no quest id in {args!r}")
    return int(m.group(1))


# Quests the bot cannot physically do. Skipped on sight rather than after
# three failures, because each attempt is minutes of walking.
#   3741 Hilary's Necklace - the necklace lies on the bed of Lake Everstill.
#        The bot swam out for it, wedged, and spent 30 minutes there earning
#        0 xp; there is no dry land within 200 yd of the objective.
UNDOABLE_QUESTS = {
    3741: "the objective is underwater",
}
# Quests that cost this character more than they pay. These five ids used to
# be a hand-written skip list; each was added after a named incident, and
# between them they cover levels 6-13 of one route and none of the ~2000
# quests from 13 to 60. The evidence behind every one of them - attempts,
# wall-clock, deaths, xp - is now recorded for EVERY quest in
# data/quest_cost_<char>.json, and ledger_skip() applies the same judgement
# at every level. These stay only as the seed of a fresh ledger, so day one
# behaves exactly as today did.
QUEST_SKIP_SEED = {
    # every source is a Defias camp of trapper (12-13, hits for half our hp)
    # + smuggler (throws through every cast): four deaths on 2026-09-20 in
    # 25 minutes
    12: "The People's Militia: Defias trapper/smuggler camps",
    102: "Patrolling Westfall: gold dust from the same camps",
    153: "Red Leather Bandanas: the same camps",
    # (315 The Perfect Stout was here: 26 minutes stuck on Shimmer Ridge on
    # 2026-09-22, on the old mesh. The bot mesh of 2026-09-23 routes to all
    # 23 Frostmane Seer spawns; the character's own ledger decides now.)
    # headhunters throw from range inside a camp of 20 trolls: a level-9
    # warrior died three times in 15 min pulling them
    287: "Frostmane Hold: headhunters inside the troll camp",
}

# Budgeting. A guide step is worth the xp it pays, measured against how fast
# this character is actually earning - so the same rule reads "ten minutes"
# for a 113 xp step at level 7 and "half an hour" for a 2850 xp quest at 52.
XP_FLOOR = 400.0             # xp/h floor, so a stalled character still has a budget
BUDGET_TOLERANCE = 1.5       # a step may cost 1.5x what its xp is worth
BUDGET_MIN = 240.0           # ... but never cut anything shorter than 4 minutes
BUDGET_MAX = 1800.0          # ... and never let one action burn half an hour
# What share of a quest's reward each of its three actions is allowed to
# spend. The quest pays once, at the turn-in; accepting it and handing it in
# are travel and two keypresses, the objectives are the work.
BUDGET_SHARE = {"QA": 0.35, "QC": 1.0, "QT": 0.5}
LEDGER_BAD = 4.0             # seconds per xp this far above baseline = not worth doing
LEDGER_DEATHS = 2            # ... or this many deaths while trying it
LEDGER_LEVELS = 3            # a ledger entry expires after this many levels
LEDGER_MAX_SKIP = 0.2        # at most this share of a guide's steps may be ledger-skipped

HEARTH_COOLDOWN = 3600.0     # 1.12 hearthstone
# SkillLine.dbc ids for the lines the guides gate steps on. The character's
# skill VALUES are not in the spellbook, only which lines it has, so these
# answer "do we have it at all" - which is what every `.skill <trade>,<1,1`
# line in the corpus asks, and (for riding, which cannot exist below 75) the
# 45 `.skill riding,75,1` ones as well. See step_gate.
SKILL_LINES = {"firstaid": 129, "cooking": 185, "fishing": 356, "herbalism": 182,
               "alchemy": 171, "blacksmithing": 164, "leatherworking": 165,
               "tailoring": 197, "engineering": 202, "enchanting": 333,
               "mining": 186, "skinning": 393, "lockpicking": 633, "riding": 762}


class QuestRunner:
    # ------------------------------------------------------------ setup
    def setup_guide(self, name: str | None = None, force: bool = False):
        """Load a RestedXP guide by its #name ("6-10 Dun Morogh"; "_" for spaces).

        "auto" (or no name) picks where this character's level sits on its
        race's chain. A restart passes the same name every time, so unless
        forced, saved progress that is further along the chain wins.
        """
        me = self.g.player()
        self.who = rxp.Who.of(self.profile.name, me.race)
        self.char_name = self.g.player_name()
        self.prog_file = os.path.join(PROGRESS_DIR, f"quest_progress_{self.char_name}.json")
        saved = json.load(open(self.prog_file)) if os.path.exists(self.prog_file) else {}
        chain = self.chain = rxp.chain(self.who)
        name = (name or "").replace("_", " ").strip()
        if name.lower() in ("", "auto", "rxp"):
            name = rxp.guide_for_level(self.who, me.level)
        if (not force and saved.get("guide") in chain and name in chain
                and chain.index(saved["guide"]) > chain.index(name)):
            name = saved["guide"]
        self.guide = rxp.get(name, self.who)
        rxp.resolve_npcs(self.guide.steps)
        self.gsteps = self.guide.steps
        self.prog = {"guide": self.guide.name, "step": 0, "done": []}
        stash = lambda g: os.path.join(
            PROGRESS_DIR, f"quest_progress_{self.char_name}_{re.sub(r'[^\w.-]+', '_', g)}.json")
        if saved.get("guide") == self.prog["guide"]:
            self.prog = saved
        else:
            # switching guides: keep the other guide's place, and pick up
            # this one's if we had one
            if saved.get("guide"):
                json.dump(saved, open(stash(saved["guide"]), "w"))
            if os.path.exists(stash(self.prog["guide"])):
                self.prog = json.load(open(stash(self.prog["guide"])))
        qids = {_qid(a) for s in self.gsteps for t, a in s.actions
                if t in ("QA", "QT", "QC")}
        self.qids = qids
        # Both memos below are answers ABOUT the guide we just replaced:
        # _qt_step maps qid -> index into self.gsteps, and _later_qids is
        # "what the guides AFTER this one hand in". bot.py calls setup_guide
        # again mid-run when the route advances, so keeping them would make
        # qt_step_of() and later_qids() answer for the previous guide for the
        # rest of the process.
        self.__dict__.pop("_qt_step", None)
        self.__dict__.pop("_later_qids", None)
        self.__dict__.pop("_later_known", None)
        self.qdb = guides.quests(qids)
        items = [i for q in self.qdb.values() for i, _ in q["collect"]]
        self.item_src = guides.item_sources(items)
        self.step_fail: dict[str, int] = {}
        self.step_started: dict[str, float] = {}
        if self.prog["step"] == 0 and not self.prog["done"]:
            self.prog["step"] = self.resume_point()
        _log(f"guide: {self.guide.name}, {len(self.gsteps)} steps for a "
             f"{self.who.race} {self.who.cls}; starting at step {self.prog['step'] + 1}")

    def guide_min_level(self) -> int:
        """Lowest level at which this guide is worth starting.

        The name ("19-20 Redridge") says where the guide sits in the route,
        NOT what its quests need. 12 of the 13 early Redridge quests are
        available at level 15 or below, yet the name gate kept a level-17
        mage grinding at ~3000 xp/h instead of questing. Take the name as an
        upper bound and the quests' own MinLevel as the truth: the gate is
        the level at which most of the opening quests can be picked up.
        """
        import re
        m = re.match(r"\s*(\d+)\s*-\s*\d+", getattr(self.guide, "name", "") or "")
        named = int(m.group(1)) if m else 0
        if not named:
            return 0
        need = self._guide_quest_level()
        return min(named, need) if need else named

    def _guide_quest_level(self) -> int:
        """Level at which 80% of the guide's first quests can be accepted."""
        cache = self.__dict__.setdefault("_gql_cache", {})
        name = getattr(self.guide, "name", "") or ""
        if name in cache:
            return cache[name]
        out = 0
        try:
            ids = []
            for st in (getattr(self.guide, "steps", None) or [])[:40]:
                for tag, arg in (getattr(st, "actions", None) or []):
                    if tag == "QA":
                        ids.append(_qid(arg))
            ids = list(dict.fromkeys(ids))[:15]
            if ids:
                rows = worlddb._query(
                    "SELECT MinLevel FROM quest_template WHERE entry IN (%s)"
                    % ",".join(map(str, ids)))
                lv = sorted(int(r[0]) for r in rows)
                if lv:
                    # the 80th percentile: one gated quest should not hold
                    # the whole guide back, it just fails and is skipped
                    out = lv[min(len(lv) - 1, int(len(lv) * 0.8))]
        except Exception as e:
            _log(f"  could not read quest levels for '{name}': {e}")
            out = 0
        cache[name] = out
        return out

    def save_progress(self):
        os.makedirs(PROGRESS_DIR, exist_ok=True)
        json.dump(self.prog, open(self.prog_file, "w"))

    # ------------------------------------------------- what a step is worth
    def xp_rate(self) -> float:
        """This character's trailing xp per hour over the last 15 minutes.

        The unit every budget below is denominated in. It tightens by itself
        as the character gets faster, which a constant like STEP_TIME_LIMIT
        can never do: 600 s was ten minutes for a 113 xp pickup at level 7
        (the single biggest measured loss) and gives up far too early on a
        2850 xp quest at 52.
        """
        hist = self.__dict__.setdefault("_xph_hist", [])
        # sampled at most every few seconds: reading the player costs a
        # memory walk and this is asked several times per tick
        if time.time() - self.__dict__.get("_xph_t", 0) > 5.0:
            self._xph_t = time.time()
            try:
                hist.append((time.time(), self.xp_total(self.g.player())))
            except Exception:
                pass
            cut = time.time() - 900
            while len(hist) > 2 and hist[0][0] < cut:
                hist.pop(0)
        if len(hist) < 2:
            return 0.0
        span = hist[-1][0] - hist[0][0]
        return max(0.0, (hist[-1][1] - hist[0][1]) / span * 3600) if span > 120 else 0.0

    def tag_seconds(self, tag: str) -> float | None:
        """Median wall-clock this character has spent on a completed `tag`."""
        got = sorted((self.cost_ledger()["tags"].get(tag) or [])[-20:])
        return got[len(got) // 2] if len(got) >= 3 else None

    def step_timer(self) -> float:
        """Extra seconds the guide's own .timer allows the current step."""
        try:
            return float(self.gsteps[self.prog["step"]].timer or 0.0)
        except Exception:
            return 0.0

    def action_budget(self, tag: str, args: str) -> float:
        """Seconds this action may burn before it is not worth finishing.

        A quest action is worth its share of the quest's own RewXP (which
        guide.quests has always loaded and nothing ever read) at the rate we
        are actually earning. Everything else is worth what it has cost this
        character before. Both are relative: they mean the same thing at
        level 6 and at level 56, in Coldridge and in Un'Goro.
        """
        if tag == "XP":
            return float("inf")       # a grind step ends when the level does
        rate = max(self.xp_rate(), XP_FLOOR)
        # a step the guide marked with .timer is waiting out an RP sequence
        # it knows the length of: that time is not ours to reclaim
        extra = self.step_timer()
        if tag in ("QA", "QC", "QT"):
            try:
                xp = (self.qdb.get(_qid(args)) or {}).get("xp") or 0
            except Exception:
                xp = 0
            if xp > 0:
                share = BUDGET_SHARE[tag]
                if tag == "QT":
                    # a finished quest: the whole reward is still on the
                    # table and the work is sunk - handing it in is worth it
                    try:
                        st = self.g.quest_log().get(_qid(args))
                        if st and st[0] & QUEST_COMPLETE:
                            share = 1.0
                    except Exception:
                        pass
                secs = xp * share / rate * 3600 * BUDGET_TOLERANCE
                return max(BUDGET_MIN, min(BUDGET_MAX, secs)) + extra
        med = self.tag_seconds(tag)
        secs = med * 3 * BUDGET_TOLERANCE if med else self.STEP_TIME_LIMIT
        return max(BUDGET_MIN, min(BUDGET_MAX, secs)) + extra

    # ------------------------------------------------- the cost ledger
    def cost_ledger(self) -> dict:
        """Per-quest cost record for this character, loaded once.

        {"quests": {id: {attempts, secs, deaths, xp, lvl}}, "tags": {tag: [secs]}}
        """
        led = self.__dict__.get("_ledger")
        if led is not None:
            return led
        path = os.path.join(PROGRESS_DIR, f"quest_cost_{self.char_name}.json")
        self._ledger_file = path
        try:
            led = json.load(open(path, encoding="utf-8"))
            led.setdefault("quests", {})
            led.setdefault("tags", {})
        except Exception:
            # A fresh ledger starts where the hand-written skip list stood,
            # so a new character behaves exactly as today on its first run.
            # "lvl" is what the entry expires against, and for a seed row
            # that is the level the quest is MET at - not the level at which
            # this file happened to be created. Stamping the latter expired
            # all five at level 4, before any of the quests is reachable.
            # 0 means "not known yet": ledger_skip fills it in from the
            # quest's own level the first time it asks, and never expires a
            # seed row while it is still 0.
            led = {"quests": {str(q): {"attempts": 2, "secs": 0.0, "deaths": 2,
                                       "xp": 0, "lvl": 0, "seed": 1, "why": why}
                              for q, why in QUEST_SKIP_SEED.items()},
                   "tags": {}}
        self._ledger = led
        return led

    def ledger_save(self):
        try:
            os.makedirs(PROGRESS_DIR, exist_ok=True)
            json.dump(self.cost_ledger(), open(self._ledger_file, "w", encoding="utf-8"))
        except Exception:
            pass

    def ledger_note(self, tag: str, args: str, secs: float, deaths: int, done: bool,
                    first: bool = True):
        """Book what an action just cost. Every action, not just the bad ones.

        `first` is False for a repeat booking of the SAME action: an action
        that fails is booked again on every failing tick, and each of those
        is more seconds of the one attempt, not another attempt. Counting
        the ticks made ledger_skip's "two attempts" bar mean "two failing
        ticks of one action", which two ticks a minute apart clear.
        """
        led = self.cost_ledger()
        if tag in ("QA", "QC", "QT"):
            try:
                qid = _qid(args)
            except ValueError:
                return
            q = led["quests"].setdefault(str(qid), {"attempts": 0, "secs": 0.0,
                                                    "deaths": 0, "xp": 0, "lvl": 0})
            q["attempts"] = int(q.get("attempts", 0)) + (0 if done or not first else 1)
            q["secs"] = round(float(q.get("secs", 0)) + secs, 1)
            q["deaths"] = int(q.get("deaths", 0)) + max(0, deaths)
            try:
                q["xp"] = (self.qdb.get(qid) or {}).get("xp") or q.get("xp", 0)
                q["lvl"] = self.g.player().level
            except Exception:
                pass
        elif done:
            # the median completed time for a tag IS the budget for that tag
            got = led["tags"].setdefault(tag, [])
            got.append(round(secs, 1))
            del got[:-20]
        self.ledger_save()

    def book_cost(self, tag: str, args: str, act_key, done: bool, elapsed=None):
        """Book an action's cost so far, counting each second only once."""
        acts_t = self.__dict__.setdefault("_act_t", {})
        acts_d = self.__dict__.setdefault("_act_deaths", {})
        acts_b = self.__dict__.setdefault("_act_booked", {})
        spent = elapsed if elapsed is not None else (
            time.time() - acts_t.get(act_key, time.time()))
        deaths = getattr(self, "deaths", 0)
        self.ledger_note(tag, args, max(0.0, spent - acts_b.get(act_key, 0.0)),
                         deaths - acts_d.get(act_key, deaths), done=done,
                         first=act_key not in acts_b)
        acts_b[act_key] = spent
        acts_d[act_key] = deaths

    def ledger_skip(self, qid: int | None, count: bool = True) -> str | None:
        """Why the ledger says not to try this quest again (None = try it).

        Two attempts or two deaths of evidence before anything is skipped,
        and the entry expires when the character is three levels stronger -
        the guide's "while you're here" pickup that was a trap at level 9 is
        often free at 12, and a route that never retries anything is a route
        that loses quest xp it could have had.
        """
        if not qid:
            return None
        led = self.cost_ledger()["quests"].get(str(qid))
        if not led:
            return None
        try:
            level = self.g.player().level
        except Exception:
            level = 0
        base = int(led.get("lvl", 0))
        if led.get("seed") and not base:
            # a seed row is an incident about a quest, not about a run: it
            # expires three levels after the quest is reachable, which is
            # the quest's own level. Unknown (the quest is not in this
            # guide's qdb yet) means "do not expire it yet".
            base = int(((getattr(self, "qdb", None) or {}).get(qid) or {}).get("level") or 0)
            if base:
                led["lvl"] = base
                self.ledger_save()
        if (base or not led.get("seed")) and level >= base + LEDGER_LEVELS:
            self.cost_ledger()["quests"].pop(str(qid), None)
            self.ledger_save()
            _log(f"  quest {qid}: {LEDGER_LEVELS} levels stronger than when it was "
                 "dropped - trying it again")
            return None
        # never skip so much of a guide that we stop questing and just walk
        cap = max(1, int(LEDGER_MAX_SKIP * len(getattr(self, "gsteps", []) or [1])))
        if count and len(self.__dict__.setdefault("_ledger_skipped", set())) >= cap:
            return None
        why = led.get("why")
        if int(led.get("deaths", 0)) >= LEDGER_DEATHS:
            why = why or f"{led['deaths']} deaths while trying it"
        else:
            xp = max(1, int(led.get("xp", 0)))
            secs = float(led.get("secs", 0))
            baseline = 3600.0 / max(self.xp_rate(), XP_FLOOR)      # seconds per xp
            if int(led.get("attempts", 0)) < 2 or secs / xp <= LEDGER_BAD * baseline:
                return None
            why = why or (f"{secs / 60:.0f} min for {xp} xp over "
                          f"{led['attempts']} attempts")
        if count:
            self.__dict__.setdefault("_ledger_skipped", set()).add(qid)
        return why

    def pointless_waypoint(self) -> str | None:
        """A travel waypoint whose every following quest action will be
        skipped anyway: say why, so the walk is skipped too.

        The block is the steps after this one up to the next travel step
        (GO, hearth, flight). It only counts as pointless when it holds quest
        actions and nothing else that needs the place (a vendor, a trainer, a
        grind), and each quest action is vetoed by the ledger or is for a
        quest we do not hold. Ugibd walked 2.5 minutes toward Frostmane Hold
        and then skipped all three of its "Kill Headhunters" steps on cost."""
        here = self.prog["step"]
        log = None
        quests = 0
        for s in self.gsteps[here + 1:here + 8]:
            for tag, args in s.actions:
                if tag in ("GO", "H", "F", "P"):
                    return (f"it only leads to {quests} quest step(s) that will be skipped"
                            if quests else None)
                if tag not in ("QA", "QC", "QT"):
                    return None
                try:
                    qid = _qid(args)
                except Exception:
                    return None
                if self.ledger_veto(tag, qid):
                    quests += 1
                    continue
                if tag in ("QC", "QT"):
                    if log is None:
                        log = self.g.quest_log()
                    if qid not in log:
                        quests += 1
                        continue
                return None
        return None

    def ledger_veto(self, tag: str, qid: int | None) -> str | None:
        """The ledger's answer for ONE guide action (None = run it).

        The ledger decides whether to START a quest, never whether to FINISH
        one. A quest already in the log has had its cost paid; skipping its
        turn-in throws the xp away as well, which is strictly worse than
        either doing the quest or never taking it. So: QA always, QC only
        while there is still work left to pay for, QT never.
        """
        if not qid or tag == "QT":
            return None
        if tag == "QC":
            try:
                state = self.g.quest_log().get(qid)
            except Exception:
                state = None
            if state and state[0] & QUEST_COMPLETE:
                return None            # one conversation from being xp
        return self.ledger_skip(qid)

    # ------------------------------------------------------------ state
    def guide_items(self) -> set[int]:
        """Items the rest of the guide still needs: every "collect N" (CI)
        step from here on, and the item objectives of every guide quest not
        yet handed in. The vendor must not sell these. Keeping only the items
        of quests already in the log sold the collection for quests the guide
        picks up LATER - four Chunks of Boar Meat and six Crag Boar Ribs were
        farmed, sold at the next bags-full vendor trip, and both steps gave
        up after ten minutes each."""
        out: set[int] = set()
        steps = getattr(self, "gsteps", None)
        if not steps:
            return out
        start = self.prog.get("step", 0) if getattr(self, "prog", None) else 0
        for s in steps[start:]:
            for t, a in s.actions:
                if t == "CI":
                    try:
                        out.add(int(a.partition(",")[0]))
                    except ValueError:
                        pass
        try:
            done = self.quests_done()
        except Exception:
            done = set()
        for qid, q in (getattr(self, "qdb", None) or {}).items():
            if qid not in done:
                out |= {int(i) for i, _ in q.get("collect", ())}
        return out

    def quests_done(self) -> set[int]:
        """Quests this character has handed in (the server's own record)."""
        low = self.g.local_guid() & 0xFFFFFFFF
        rows = worlddb._query_db(
            "characters", f"SELECT quest FROM character_queststatus WHERE guid={low} AND rewarded=1")
        # the DB only sees hand-ins at the next character save (minutes later)
        return {int(r[0]) for r in rows} | set(self.prog.get("rewarded", []))

    def guide_shopping_reserve(self, ahead: int = 20) -> int:
        """Copper the next `ahead` guide steps will spend buying items from a
        vendor (CI steps for vendor-sold items we do not hold yet)."""
        if not getattr(self, "guide_active", False) or not getattr(self, "gsteps", None):
            return 0
        i0 = self.prog.get("step", 0)
        key = (self.prog.get("guide"), i0)
        cached = self.__dict__.get("_shop_reserve")
        if cached and cached[0] == key and time.time() - cached[2] < 60:
            return cached[1]
        bag = self.g.bag_items()
        total = 0
        try:
            for st in self.gsteps[i0:i0 + ahead]:
                for tag, args in st.actions:
                    if tag != "CI":
                        continue
                    parts = str(args).replace(" ", "").split(",")
                    item, need = int(parts[0]), int(parts[1]) if len(parts) > 1 and parts[1] else 1
                    missing = need - bag.get(item, 0)
                    if missing <= 0 or not guides.item_vendors(item):
                        continue
                    # only what a quest needs handed in (Thunder Ale for Bitter
                    # Rivals); the guide's optional gear buys (a 4s84c Large
                    # Axe) would reserve the whole purse and starve the food
                    need_q = worlddb._query(
                        "SELECT 1 FROM quest_template WHERE %d IN (ReqItemId1, ReqItemId2, "
                        "ReqItemId3, ReqItemId4) LIMIT 1" % item)
                    if not need_q:
                        continue
                    rows = worlddb._query(f"SELECT buy_price FROM item_template WHERE entry={item} LIMIT 1")
                    if rows:
                        total += int(rows[0][0]) * missing
        except Exception:
            pass
        self._shop_reserve = (key, total, time.time())
        return total

    def nearest_spawn_of(self, entries, kind="creature"):
        """spawn_of for many entries in ONE query. A Mining Pick has 113
        sellers; one spawn_of each stood the paladin still for 30 s next to
        the vendor (08:36:47-08:37:17)."""
        entries = [int(e) for e in entries]
        if not entries:
            return None
        me = self.g.player()
        table = "creature" if kind == "creature" else "gameobject"
        ids = ",".join(map(str, entries))
        m = self.continent()
        for mm in (m, 1 - m):
            rows = worlddb._query(f"SELECT id, position_x, position_y, position_z FROM {table} "
                                  f"WHERE id IN ({ids}) AND map={mm}")
            if rows:
                best = min(rows, key=lambda r: math.dist((float(r[1]), float(r[2])), (me.x, me.y)))
                entry = int(best[0])
                name = worlddb.name(entry) if kind == "creature" else f"object {entry}"
                return {"entry": entry, "name": name, "title": "",
                        "pos": [float(v) for v in best[1:4]], "kind": kind, "map": mm}
        return None

    def spawn_of(self, entry: int, kind="creature"):
        """Nearest spawn of an NPC / object as a visit_npc() dict."""
        me = self.g.player()
        table = "creature" if kind == "creature" else "gameobject"
        m = self.continent()
        rows = worlddb._query(
            f"SELECT position_x, position_y, position_z FROM {table} WHERE id={entry} AND map={m}")
        if not rows:
            # on the other continent: visit_npc takes the boat (bot.cross_sea)
            m = 1 - m
            rows = worlddb._query(
                f"SELECT position_x, position_y, position_z FROM {table} WHERE id={entry} AND map={m}")
        if not rows:
            return None
        pos = min(([float(v) for v in r] for r in rows),
                  key=lambda p: math.dist(p[:2], (me.x, me.y)))
        name = worlddb.name(entry) if kind == "creature" else f"object {entry}"
        return {"entry": entry, "name": name, "title": "", "pos": pos, "kind": kind, "map": m}

    # ------------------------------------------------------------ main entry
    def quest_tick(self, me) -> bool:
        """Advance the guide. False when the guide is finished."""
        while self.prog["step"] < len(self.gsteps):
            step = self.gsteps[self.prog["step"]]
            # A step's conditions are checked once, on arrival - as the addon
            # does: "Step skipped: Missing pre-requisites".
            if getattr(self, "_gated", None) != (self.prog["guide"], self.prog["step"]):
                self._gated = (self.prog["guide"], self.prog["step"])
                why = None if self.prog["done"] else self.step_gate(step)
                me_now = self.g.player()
                events.emit("step", guide=self.prog["guide"], step=self.prog["step"] + 1,
                            text=step.text[:120], lvl=me_now.level, xp=me_now.xp, skipped=why)
                if why:
                    _log(f"  step {self.prog['step'] + 1}: skipped - {why}")
                    self.prog["step"] += 1
                    self.prog["done"] = []
                    self.prog["skipped"] = []
                    self.save_progress()
                    continue
            pending = [i for i in range(len(step.actions)) if i not in self.prog["done"]]
            if not pending:
                self.prog["step"] += 1
                self.prog["done"] = []
                self.prog["skipped"] = []
                self.save_progress()
                continue
            i = pending[0]
            tag, args = step.actions[i]
            act_key = (self.prog["guide"], self.prog["step"], i)
            acts_t = self.__dict__.setdefault("_act_t", {})
            acts_d = self.__dict__.setdefault("_act_deaths", {})
            acts_b = self.__dict__.setdefault("_act_booked", {})
            acts_t.setdefault(act_key, time.time())
            acts_d.setdefault(act_key, getattr(self, "deaths", 0))
            if act_key not in self.__dict__.setdefault("_act_xp", {}):
                try:
                    self._act_xp[act_key] = (time.time(), self.xp_total(self.g.player()))
                except Exception:
                    pass
            # Only time spent inside the action counts against its budget.
            # Fights, rests, loot and vendor trips between two calls are the
            # bot's own business (and the fights pay xp): a hand-in of The
            # Stolen Journal was skipped at 4 of 4 minutes, three of them
            # spent killing whelps that jumped us on the way to Grelin.
            acts_l = self.__dict__.setdefault("_act_last", {})
            gap = time.time() - acts_l.get(act_key, time.time())
            if gap > 3.0:
                acts_t[act_key] += gap
            _state("questing", detail=f"step {self.prog['step'] + 1}: {tag} {args}")
            try:
                qid = _qid(args) if tag in ("QA", "QC", "QT") and args.split() else None
                ledger_why = self.ledger_veto(tag, qid)
                if not ledger_why and tag == "GO":
                    ledger_why = self.pointless_waypoint()
                if ledger_why and tag == "GO":
                    _log(f"  step {self.prog['step'] + 1}: not walking to this waypoint - "
                         f"{ledger_why}")
                    result = "skip"
                elif ledger_why:
                    _log(f"  step {self.prog['step'] + 1}: skipping quest {qid} - this "
                         f"character's ledger: {ledger_why}")
                    result = "skip"
                else:
                    result = self.do_action(step, tag, args)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:                  # one odd guide line must not kill the run
                import traceback
                _log(f"  step {self.prog['step'] + 1}: [{tag}{args}] raised {type(e).__name__}: {e}")
                _log("  " + traceback.format_exc().strip().splitlines()[-3].strip()[:160])
                result = "failed"
            acts_l[act_key] = time.time()
            if result in ("done", "skip", "failed"):
                me_now = self.g.player()
                events.emit("action", guide=self.prog["guide"], step=self.prog["step"] + 1, i=i,
                            tag=tag, args=args, result=result,
                            secs=round(time.time() - acts_t[act_key], 1),
                            lvl=me_now.level, xp=me_now.xp)
                # "skip" is not a failed attempt (a prerequisite we do not
                # have yet), but it did cost the walk that found that out.
                # Book only what has not been booked yet: an action that
                # fails once a minute is booked once a minute, and charging
                # it its whole elapsed time each go would treble the bill.
                self.book_cost(tag, args, act_key, done=result != "failed")
                if result != "failed":
                    acts_t.pop(act_key, None)
                    acts_d.pop(act_key, None)
                    acts_b.pop(act_key, None)
            if result in ("done", "skip"):
                if result == "skip":
                    _log(f"  step {self.prog['step'] + 1}: skipping [{tag}{args}]")
                    self.prog.setdefault("skipped", []).append(i)
                self.prog["done"].append(i)
                self.save_progress()
            else:
                key = f"{self.prog['step']}:{i}"
                if time.time() - getattr(self, "_wedged_at", 0) < 180:
                    result = "working"       # stuck in geometry: not the step's fault
                self.step_fail[key] = self.step_fail.get(key, 0) + (result == "failed")
                # A step can fail slowly: three 5-minute walks into a cliff at
                # Gol'Bolar spent 20 minutes on one turn-in at zero xp. Cap
                # the wall-clock a single action may burn, not just its tries.
                started = self.step_started.setdefault(key, time.time())
                spent = time.time() - started
                # ... but only for a step that is actually failing. A grind
                # step ("XP17 Grind nearby until Level 17") reports "working"
                # for as long as it takes and must never be timed out: the
                # cap skipped one after 11 minutes of healthy grinding.
                stalled = (spent > self.STEP_TIME_LIMIT and tag != "XP"
                           and self.step_fail[key] >= 1)
                # The wall-clock cap above only fires for an action that has
                # FAILED at least once, so an action that keeps answering
                # "working" - which hunt() does for as long as there is a mob
                # in sight - has no cap at all. The budget does not care what
                # the action says about itself, only what it has cost against
                # what it pays (action_budget), so it closes that hole and is
                # the same rule for a 113 xp pickup at level 7 and a 2850 xp
                # quest at 52. Not while the geometry has us wedged: that is
                # the navmesh's failure, not the step's.
                elapsed = time.time() - acts_t.get(act_key, time.time())
                budget = self.action_budget(tag, args)
                over = (elapsed > budget and tag != "XP"
                        and time.time() - getattr(self, "_wedged_at", 0) > 180)
                if over and tag == "QC":
                    # A kill objective pays as it goes: The Troll Cave was
                    # skipped 10 whelps in at 8,194 xp/h (07:59) - grinding
                    # with a reward at the end. Keep it while the kills alone
                    # earn close to our usual rate.
                    try:
                        t_x, x0 = self._act_xp[act_key]
                        paid = (self.xp_total(self.g.player()) - x0) / max(60.0, time.time() - t_x) * 3600
                        if paid >= 0.7 * self.xp_rate() and elapsed < 3 * budget:
                            over = False
                    except Exception:
                        pass
                if self.step_fail[key] >= 3 or stalled or over:
                    why = (f"failed {self.step_fail[key]} times" if self.step_fail[key] >= 3
                           else f"{spent / 60:.0f} min of failing - giving up" if stalled
                           else f"{elapsed / 60:.0f} min against a {budget / 60:.0f} min "
                                f"budget at {self.xp_rate():.0f} xp/h")
                    _log(f"  step {self.prog['step'] + 1}: [{tag}{args}] {why} - skipping")
                    events.emit("action", guide=self.prog["guide"], step=self.prog["step"] + 1, i=i,
                                tag=tag, args=args, result="gave up", why=why,
                                over=round(elapsed - budget, 1) if over else 0,
                                budget=round(budget, 1), xph=round(self.xp_rate()),
                                secs=round(time.time() - acts_t.pop(act_key, time.time()), 1))
                    # the evidence that this quest is not worth doing, in the
                    # one place that will still know it three levels later
                    self.book_cost(tag, args, act_key, done=False, elapsed=elapsed)
                    acts_d.pop(act_key, None)
                    acts_b.pop(act_key, None)
                    self.prog.setdefault("skipped", []).append(i)
                    self.prog["done"].append(i)
                    self.save_progress()
            return True
        return False

    # ------------------------------------------------------------ actions
    def do_action(self, step, tag, args) -> str:
        """Returns done | skip | working | failed."""
        if tag in ("QA", "QC", "QT"):
            qid = _qid(args)
            why = UNDOABLE_QUESTS.get(qid)
            if why:
                _log(f"  quest {qid}: {why} - skipping without trying")
                return "skip"
        if tag == "AB":
            return self.abandon(_qid(args), step)
        if tag == "QA":
            return self.accept(_qid(args), step)
        if tag == "QT":
            return self.turn_in(_qid(args), step)
        if tag == "QC":
            one = self.objective_of(args)
            return self.complete(_qid(args), step, obj=one[0] if one else None)
        if tag == "XP":
            return self.grind_to(args)
        if tag in ("V", "R"):
            return self.vendor_visit(repair=tag == "R")
        if tag in ("LE", "T"):
            return self.train_now()
        if tag == "UI":
            return self.equip(_qid(args))
        if tag == "CI":
            item, _, n = args.partition(",")
            return self.collect(int(item), int(n or 1), step)
        if tag == "S":
            return self.set_hearth(step)
        if tag == "H":
            return self.use_hearth(args.strip())
        if tag == "P":
            return self.learn_flight_path()
        if tag == "F":
            return self.fly_to(self.taxi_node_for(args.strip(), step))
        if tag == "GO":
            return self.go_to(args)
        if tag == "USE":
            return self.use_item(_qid(args), step)
        if tag == "DESTROY":
            # Never. The guide's one use on our chain is the Hearthstone at
            # level 1 (a speedrun trick to free a bag slot), and deleting an
            # item off the character cannot be undone.
            _log(f"  guide says to destroy item {args} - the bot never deletes items")
            return "skip"
        return "skip"          # CAST (tracking spells), ...: not supported yet

    # -- the status page's view of the guide
    def guide_status(self) -> dict | None:
        """The step in hand and how far along it is, for web.py.

        Called from the status thread every refresh, so everything here is
        a memory read or a cached lookup - never a query per call.
        """
        steps = getattr(self, "gsteps", None)
        if not steps or not getattr(self, "guide_active", False):
            return None
        i, chain = self.prog["step"], getattr(self, "chain", [])
        out = {"guide": self.guide.name, "chain_len": len(chain),
               "chain_pos": chain.index(self.guide.name) + 1 if self.guide.name in chain else None,
               "step": min(i + 1, len(steps)), "steps": len(steps),
               "text": "guide finished", "actions": [], "next": []}
        if i >= len(steps):
            return out
        step = steps[i]
        me, log, bag = self.g.player(), self.g.quest_log(), self.g.bag_items()
        done, skipped = set(self.prog["done"]), set(self.prog.get("skipped", []))
        cur = next((j for j in range(len(step.actions)) if j not in done), None)
        out["text"] = step.text
        out["actions"] = [
            {"label": self.action_label(t, a),
             "state": ("skipped" if j in skipped else "done" if j in done
                       else "current" if j == cur else "todo"),
             "progress": self.action_progress(t, a, me, log, bag)
             if j == cur or (t in ("QC", "QT") and j not in done) else []}
            for j, (t, a) in enumerate(step.actions)]
        for s in steps[i + 1:i + 5]:
            if s.actions and all(t == "GO" for t, _ in s.actions):
                n = len(s.actions)
                out["next"].append(f"Travel: {n} waypoint{'s' if n > 1 else ''} "
                                   f"in {s.actions[-1][1].split(',')[0]}")
            else:
                out["next"].append(" \u00b7 ".join(self.action_label(t, a) for t, a in s.actions))
        return out

    def action_label(self, tag: str, args: str) -> str:
        def title():
            q = self.qdb.get(_qid(args)) or {}
            return q.get("title") or f"quest {_qid(args)}"
        if tag == "QA":
            return f"Accept {title()}"
        if tag == "QT":
            return f"Hand in {title()}"
        if tag == "QC":
            obj = self.objective_of(args)
            return f"Complete {title()}" + (f": {obj[1]}" if obj else "")
        if tag == "GO":
            z, x, y, *_ = args.split(",")
            return f"Travel to {z} ({float(x):.0f}, {float(y):.0f})"
        if tag == "XP":
            return f"Grind to level {args}"
        if tag == "CI":
            item, _, n = args.partition(",")
            return f"Collect {n or 1} {self.item_name(int(item))}"
        if tag in ("UI", "USE", "DESTROY"):
            verb = {"UI": "Equip", "USE": "Use", "DESTROY": "Destroy"}[tag]
            return (f"{verb} {self.item_name(_qid(args))}"
                    + (" (never done)" if tag == "DESTROY" else ""))
        return {"V": "Sell and restock at a vendor", "LE": "Train at the class trainer",
                "P": "Learn the flight path", "S": "Set the hearthstone here",
                "F": f"Fly to {args}", "H": args or "Hearth"}.get(tag, f"{tag} {args}")

    def action_progress(self, tag, args, me, log, bag) -> list[dict]:
        """How far an action is: objective counters, items in the bags,
        yards to a waypoint, xp toward a grind target."""
        if tag in ("QC", "QT"):
            qid = _qid(args)
            q = self.qdb.get(qid)
            if not q or qid not in log:
                return []
            word = log[qid][0]
            if word & QUEST_COMPLETE:
                return [{"label": "objectives complete - hand it in", "have": 1, "need": 1}]

            def have(kind, e, n):
                if kind == "item":
                    return min(bag.get(e, 0), n)
                slot = q["slot"].get(e)
                return (word >> (6 * slot)) & 0x3F if slot is not None else 0
            objs = [{"label": name, "have": have(kind, e, n), "need": n}
                    for kind, e, name, n in self.objectives(q)]
            one = self.objective_of(args) if tag == "QC" else None
            return [objs[one[0]]] if one else objs
        if tag == "CI":
            item, _, n = args.partition(",")
            n = int(n or 1)
            return [{"label": self.item_name(int(item)),
                     "have": min(bag.get(int(item), 0), n), "need": n}]
        if tag == "XP":
            target = re.match(r"\d+", args)
            return [{"label": f"level {me.level} of {target.group(0) if target else args}",
                     "have": me.xp, "need": me.next_xp}]
        if tag == "GO":
            z, x, y, *_ = args.split(",")
            w = worldmap.to_world(float(x), float(y), z)
            if w and w[0] == self.continent():
                return [{"label": f"{math.dist((me.x, me.y), w[1:]):.0f} yd to go",
                         "have": None, "need": None}]
        return []

    def objectives(self, q) -> list[tuple]:
        """A quest's objectives in quest-log order: (kind, entry, name, count).

        Kills and objects by their objective slot, then the items - the
        order RestedXP numbers them in (".complete 170,2" is the second).
        """
        by_slot = sorted([("kill", c, n) for c, n in q["kill"]]
                         + [("object", o, n) for o, n in q["use_object"]],
                         key=lambda o: q["slot"].get(o[1], 9))
        return ([(k, e, worlddb.name(e) if k == "kill" else self.object_name(e), n)
                 for k, e, n in by_slot]
                + [("item", it, self.item_name(it), n) for it, n in q["collect"]])

    def objective_of(self, args: str):
        """(index, name) of the objective a ".complete <quest>,<n>" names."""
        parts = args.split(",")
        q = self.qdb.get(_qid(args))
        if len(parts) < 2 or not parts[1].strip().isdigit() or not q:
            return None
        k = int(parts[1]) - 1
        objs = self.objectives(q)
        return (k, objs[k][2]) if 0 <= k < len(objs) else None

    def item_name(self, item: int) -> str:
        names = self.__dict__.setdefault("_item_names", {})
        if item not in names:
            rows = worlddb._query(f"SELECT name FROM item_template WHERE entry={int(item)} "
                                  "ORDER BY patch DESC LIMIT 1")
            names[item] = rows[0][0] if rows else f"item {item}"
        return names[item]

    def object_name(self, entry: int) -> str:
        names = self.__dict__.setdefault("_object_names", {})
        if entry not in names:
            rows = worlddb._query(f"SELECT name FROM gameobject_template WHERE entry={int(entry)} "
                                  "ORDER BY patch DESC LIMIT 1")
            names[entry] = rows[0][0] if rows else f"object {entry}"
        return names[entry]

    # -- step conditions and resuming
    def step_gate(self, step) -> str | None:
        """Why a step's conditions say not to run it (None = run it).

        Each quest check passes when ANY of its ids qualifies, as in the
        addon (functions.lua, addon.functions.isOnQuest and friends).

        The character-state conditions (rxp.STEP_CONDITION: itemcount, money,
        skill, aura, cooldown, maxlevel) FAIL OPEN: anything this bot cannot
        read - a reputation, a profession level above 1, the cooldown of an
        item that is not the hearthstone - runs the step, which is what
        happened to all of them before they were parsed at all. A gate that
        wrongly skips a step loses content silently; a gate that wrongly runs
        one costs a walk, and the budget in quest_tick puts a cap on that.
        """
        me = self.g.player()
        if step.min_level and me.level < step.min_level:
            return f"needs level {step.min_level}"
        log = self.g.quest_log()
        done = bag = skills = None
        for kind, ids in step.requires:
            if isinstance(ids, dict):
                why, bag, skills = self.state_gate(kind, ids, me, bag, skills)
                if why:
                    return why
                continue
            if kind in ("isQuestTurnedIn", "isQuestAvailable") and done is None:
                done = self.quests_done()
            complete = [i for i in ids if i in log and log[i][0] & QUEST_COMPLETE]
            if kind == "minLevel" and me.level < ids[0]:
                return f"needs level {ids[0]}"
            if kind == "maxLevel" and me.level > ids[0]:
                return f"only below level {ids[0] + 1}"
            if kind == "isOnQuest" and not any(i in log for i in ids):
                return f"not on quest {ids}"
            if kind == "isNotOnQuest" and any(i in log for i in ids):
                return f"already on quest {ids}"
            if kind == "isQuestTurnedIn" and not any(i in done for i in ids):
                return f"quest {ids} not handed in yet"
            if kind == "isQuestAvailable" and all(i in done for i in ids):
                return f"quest {ids} already handed in"
            if kind == "isQuestComplete" and not complete:
                return f"quest {ids} not complete"
            if kind == "isQuestNotComplete" and complete:
                return f"quest {ids} already complete"
        return None

    def state_gate(self, kind: str, req: dict, me, bag, skills):
        """One character-state condition: (why to skip or None, bag, skills).

        bag and skills are threaded through so a step with four .itemcount
        lines reads the bags once.
        """
        if kind == "itemcount":
            bag = self.g.bag_items() if bag is None else bag
            have = sum(bag.get(i, 0) for i in req["ids"])
            op, n, eq = req["op"], req["n"], req["eq"]
            ok = ((eq and have == n) or (have * op > n * op)
                  or (not eq and op == 0 and have >= n))
            if not ok:
                return (f"we hold {have} of item {req['ids'][0]}, the step wants "
                        f"{'<' if op < 0 else '>' if op > 0 else ''}{n}"), bag, skills
        elif kind == "money":
            # ".money <90" marks a step that is pointless BELOW 90 gold
            ok = me.money >= req["copper"] if req["op"] == "<" else me.money < req["copper"]
            if not ok:
                return (f"needs {'at least' if req['op'] == '<' else 'less than'} "
                        f"{req['copper'] / 10000:.2f}g, we have "
                        f"{me.money / 10000:.2f}g"), bag, skills
        elif kind == "skill":
            line = SKILL_LINES.get(req["skill"])
            if line:
                skills = gear.known_skills(self.g) if skills is None else skills
                # The spellbook gives us the skill LINE, not its value, so
                # bound it: a line we do not have is 0, and one we do have is
                # at least 1 - at least 75 for riding, which cannot be
                # trained below that. Only decide when both bounds agree.
                known = line in skills
                lo = 0 if not known else (75 if req["skill"] == "riding" else 1)
                hi = lo if not known else 10 ** 6
                if (lo >= req["level"]) == (hi >= req["level"]) \
                        and (lo >= req["level"]) == (not req["under"]):
                    return (f"{req['skill']} is "
                            f"{'below' if req['under'] else 'at least'} "
                            f"{req['level']}"), bag, skills
        elif kind == "aura":
            # ".aura 15366" = the step is for getting that buff; ".aura -9991"
            # = the step is for curing that debuff. Either way it only runs
            # when the aura's presence does not already answer it.
            has = req["id"] in set(getattr(me, "auras", ()) or ())
            if has != req["absent"]:
                return (f"aura {req['id']} is "
                        f"{'not on us' if req['absent'] else 'already on us'}"), bag, skills
        elif kind == "cooldown":
            left = self.item_cooldown(req["what"], req["id"])
            if left is not None:
                if (req["op"] == ">" and left > req["secs"]) or \
                        (req["op"] == "<" and left < req["secs"]):
                    return (f"item {req['id']} has {left / 60:.0f} min of "
                            "cooldown left"), bag, skills
        elif kind == "reputation":
            # nothing in memory reads standings yet: run the step
            said = self.__dict__.setdefault("_rep_said", set())
            if req["faction"] not in said:
                said.add(req["faction"])
                _log(f"  step gate: reputation with {req['faction']} is not readable "
                     "- running the step anyway")
        return None, bag, skills

    def item_cooldown(self, what: str, item: int) -> float | None:
        """Seconds of cooldown left, or None when we cannot know.

        Only the hearthstone, whose use this module records itself - and it
        is also the only id the whole guide chain gates on (64 of 64
        .cooldown lines are item 6948).
        """
        if what != "item" or item != 6948:
            return None
        try:
            used = float(json.load(open(os.path.join(PROGRESS_DIR, "hearth.json")))["used"])
        except Exception:
            return None
        return max(0.0, used + HEARTH_COOLDOWN - time.time())

    def resume_point(self) -> int:
        """Where to start a guide we have no saved place in.

        The guide is in order, so the last step that touches a quest this
        character has already handed in is as far as we have been. Starting
        from the top instead would re-walk every travel leg of the zone to
        accept quests that are long done.

        But never past work we still hold: a quest in the log whose
        objective or hand-in comes earlier than that. The switch from the
        Sage guide would otherwise have stranded "The Stolen Journal" - its
        accept, kill and hand-in all sit before the last done quest.
        """
        done = self.quests_done()
        held = set(self.g.quest_log())
        last, first_held = 0, None
        for i, s in enumerate(self.gsteps):
            ids = {_qid(a) for t, a in s.actions if t in ("QA", "QT", "QC")}
            if ids & done:
                last = i
            work = {_qid(a) for t, a in s.actions if t in ("QT", "QC")}
            if first_held is None and work & held:
                first_held = i
        return last if first_held is None else min(last, first_held)

    # -- travel
    GO_RADIUS = 5.0            # yd, for a waypoint that gives none of its own

    def go_to(self, args: str) -> str:
        """.goto: walk to one waypoint of the guide's route (map percent).

        These are the author's own path between quest hubs - the reason for
        following a survival guide at all - so walk them carefully, around
        the aggro circles, rather than straight.
        """
        zone, x, y, *rest = args.split(",")
        radius = float(rest[0]) if rest and rest[0] else self.GO_RADIUS
        w = worldmap.to_world(float(x), float(y), zone)
        if w is None:
            _log(f"  waypoint in unknown zone {zone!r} - skipping it")
            return "skip"
        map_id, wx, wy = w
        me = self.g.player()
        here = map_id == self.continent()
        if here and math.dist((me.x, me.y), (wx, wy)) <= radius:
            return "done"
        nav = self.nav_for(map_id)
        wz = None
        if nav and map_id in (0, 1):
            # the map point is a spot on the ground: the terrain height, when
            # the mesh has a floor there. ground_z with our own height as the
            # hint picked Anvilmar's terrace 35 yd up (z 435 over ground at
            # 400) and the bot climbed a mountain for 4 minutes to reach it
            import terrain
            tz = terrain.height(map_id, wx, wy)
            if tz is not None and nav.nearest_poly(wx, wy, tz, max_dist=3.0)[0] is not None:
                wz = tz
        if wz is None:
            wz = nav.ground_z(wx, wy, hint=me.z if here else None) if nav else None
        if wz is None:
            if not here:
                _log(f"  waypoint {zone} {x},{y} is off the navmesh - skipping it")
                return "skip"
            wz = me.z
        far = math.dist((me.x, me.y), (wx, wy)) if here else 3000.0
        if here and nav:
            # The waypoint is only a routing hint - the author's line between
            # hubs. A walk far longer than the line is not that line: "Travel
            # to Anvilmar" from 65 yd became a 780 yd route over the ridge
            # (z 544) once stuck spots had walled off the ramp. Any distance,
            # not only under 100 yd.
            path = nav.find_path((me.x, me.y, me.z), (wx, wy, wz), budget=4.0,
                                 holes=getattr(self, "obstacles", ()))
            if not path and far > radius + 10:
                # no route on the mesh: walking at it only finds that out the
                # slow way, and the next step has its own destination
                _log(f"  waypoint {far:.0f} yd off has no route on the mesh - counting it as passed")
                return "done"
            if path:
                pts = [(me.x, me.y)] + [p[:2] for p in path]
                walk = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
                if walk > max(2.5 * far, far + 80.0):
                    _log(f"  waypoint is a {walk:.0f} yd walk for {far:.0f} yd "
                         f"({wz - me.z:+.0f} yd up) - counting it as passed")
                    return "done"
        ok = self.navigate(wx, wy, wz, stop_short=max(0.0, radius - 2.0),
                           timeout=max(120.0, far / 4.0), careful=True, dest_map=map_id)
        me = self.g.player()
        if map_id == self.continent() and math.dist((me.x, me.y), (wx, wy)) <= radius + 2.0:
            return "done"
        if me.in_combat:
            return "working"
        if not ok:
            # retrying a waypoint we could not reach is what walked us round
            # the mountain; the next step has its own destination
            _log(f"  could not reach the waypoint ({math.dist((me.x, me.y), (wx, wy)):.0f} yd off) "
                 "- moving on to the next step")
            return "done"
        # The walk says it arrived but we stand outside the radius: the mesh
        # stops short of the spot (Anvilmar's waypoint sits on a terrace the
        # route ends below). Retrying it is the same walk - it failed ten
        # times across the Coldridge runs, 2-6 min each. A waypoint is a hint
        # for the route, not an objective: count it as passed.
        _log(f"  stopped {math.dist((me.x, me.y), (wx, wy)):.0f} yd from the waypoint "
             "- as close as the mesh goes, moving on")
        return "done"

    def use_item(self, item: int, step) -> str:
        """.use: a quest item, used at the step's first waypoint.

        Best effort: a single use, untargeted. Items meant for a mob (the
        Rat Catcher's Flute) do nothing this way; the step's own .complete
        then fails and is skipped, as any undoable objective is.
        """
        if self.g.bag_items().get(item, 0) == 0:
            return "skip"
        if step.gotos:
            z, x, y, r = step.gotos[0]
            got = self.go_to(f"{z},{x},{y},{r or ''}")
            if got != "done":
                return got
        # command 20 is UseContainerItem: it equips gear and uses anything else
        self.bridge.send(br.EQUIP_ITEM, item, wait=1.0)
        return "done"

    # -- flight paths
    FLIGHTS_FILE = os.path.join(PROGRESS_DIR, "flights.json")

    STEP_TIME_LIMIT = 600.0          # seconds one guide action may burn before we move on

    def _flights_file(self) -> str:
        """Per character: one flights.json for everybody gave a new dwarf
        the flight points Ugibd had found - Dsa walked 996 yd to Gryth
        Thurden for a Thelsamar flight it did not know (11:27)."""
        try:
            return os.path.join(PROGRESS_DIR, f"flights_{self.g.player_name()}.json")
        except Exception:
            return self.FLIGHTS_FILE

    def flights_known(self) -> set:
        """Flight points this character knows: the server's known_taxi_mask,
        plus what we learned since its last character save."""
        cached = self.__dict__.get("_flights_db")
        if not cached or time.time() - cached[0] > 60:
            names = None
            try:
                import taxi
                low = self.g.local_guid() & 0xFFFFFFFF
                rows = worlddb._query_db(
                    "characters", f"SELECT known_taxi_mask FROM characters WHERE guid={low}")
                mask = [int(x) for x in rows[0][0].split()]
                ids = {i * 32 + b + 1 for i, m in enumerate(mask) for b in range(32) if m >> b & 1}
                names = {n for n in br.TAXI_NODES if taxi.node_by_name(n) in ids}
            except Exception:
                pass
            cached = self._flights_db = (time.time(), names)
        try:
            mine = set(json.load(open(self._flights_file())))
        except Exception:
            mine = set()
        if cached[1] is None:
            return mine
        return cached[1] | mine

    def note_flight(self, fm) -> str | None:
        """Record the taxi node a flight master stands at (by distance)."""
        node = min(br.TAXI_NODES.items(), key=lambda kv: math.dist(kv[1][1][:2], fm["pos"][:2]))
        if math.dist(node[1][1][:2], fm["pos"][:2]) > 150:
            return None
        try:
            mine = set(json.load(open(self._flights_file())))
        except Exception:
            mine = set()
        json.dump(sorted(mine | {node[0]}), open(self._flights_file(), "w"))
        _log(f"flight: {node[0]} is now a known flight point")
        return node[0]

    def learn_nearby_flight(self, max_walk=300.0) -> bool:
        """Passing through a town: pick up its flight point if we lack it."""
        me = self.g.player()
        fm = self.nearest_service(me, "flightmaster")
        if not fm or math.dist(fm["pos"][:2], (me.x, me.y)) > max_walk:
            return False
        node = min(br.TAXI_NODES.items(), key=lambda kv: math.dist(kv[1][1][:2], fm["pos"][:2]))
        if math.dist(node[1][1][:2], fm["pos"][:2]) > 150 or node[0] in self.flights_known():
            return False
        _log(f"flight: learning the {node[0]} flight point at {fm['name']}")
        if not self.visit_npc(fm):
            return False
        self.bridge.send(br.CLOSE_ALL)
        self.note_flight(fm)
        return True

    def learn_flight_path(self) -> str:
        """Talking to a flight master discovers the node."""
        me = self.g.player()
        fm = self.nearest_service(me, "flightmaster")
        if not fm or math.dist(fm["pos"][:2], (me.x, me.y)) > 400:
            return "skip"
        _log(f"flight: learning the path at {fm['name']}")
        if not self.visit_npc(fm):
            return "working" if self.g.player().in_combat else "failed"
        self.bridge.send(br.CLOSE_ALL)
        self.note_flight(fm)
        return "done"

    def taxi_node_for(self, dest: str, step=None) -> str:
        """A .fly destination as one of bridge.TAXI_NODES' names.

        Guidelime named the flight point ("Sentinel Hill"); RestedXP names
        the ZONE ("Westfall"), which matched no node, so every flight became
        a walk. A zone becomes a flight point in it (bridge.TAXI_ZONES) that
        a flight master actually stands at - four Plaguelands towers and
        Teldrassil's "Fishing Village" are taxi nodes nobody can fly to -
        nearest the step's last waypoint there, where the guide has us land.
        """
        for k in br.TAXI_NODES:          # a node, or its start: "Nethergarde"
            if k.lower() in dest.lower() or dest.lower() in k.lower():
                return k
        z = worldmap.zone(dest)
        if z is None:
            return dest
        live = [n for n, zn in br.TAXI_ZONES.items()
                if worldmap.zone(zn) == z and n in self.live_taxi_nodes()]
        if len(live) < 2:
            return live[0] if live else dest
        target = None
        for zn, x, y, _ in reversed(getattr(step, "gotos", None) or []):
            if worldmap.zone(zn) == z:
                target = worldmap.to_world(x, y, zn)[1:]
                break
        if target is None:
            target = worldmap.to_world(50.0, 50.0, dest)[1:]
        return min(live, key=lambda n: math.dist(br.TAXI_NODES[n][1][:2], target))

    def live_taxi_nodes(self) -> set[str]:
        """TAXI_NODES with a flight master standing at them (world DB)."""
        cache = self.__dict__.get("_live_taxi")
        if cache is None:
            import taxi
            rows = worlddb._query(
                "SELECT c.map, c.position_x, c.position_y FROM creature c "
                "JOIN creature_template ct ON ct.entry = c.id "
                "WHERE ct.npc_flags & 8 AND c.map IN (0, 1) GROUP BY c.guid")
            fms = [(int(m), float(x), float(y)) for m, x, y in rows]
            nodes = taxi._data().nodes
            cache = {n for n, (nid, land) in br.TAXI_NODES.items()
                     if any(m == nodes[nid]["map"] and math.dist((x, y), land[:2]) < 40
                            for m, x, y in fms)}
            self._live_taxi = cache
        return cache

    def fly_to(self, dest: str, max_walk: float = 500.0) -> str:
        node = next((v for k, v in br.TAXI_NODES.items() if k.lower() in dest.lower()), None)
        if node is None:
            _log(f"flight: unknown destination '{dest}' - walking")
            return "skip"
        code, land = node
        me = self.g.player()
        if math.dist(land[:2], (me.x, me.y)) < 150:
            return "done"                            # already there
        known = self.flights_known()
        dest_name = next(k for k, v in br.TAXI_NODES.items() if v is node)
        if known and dest_name not in known:
            _log(f"flight: {dest_name} is not a known flight point yet - walking")
            return "skip"
        fm = self.nearest_service(me, "flightmaster")
        if not fm or math.dist(fm["pos"][:2], (me.x, me.y)) > max_walk:
            return "skip"                            # no flight master near: walk
        if known and self.note_flight(fm) not in known:
            pass                                     # our own node is learned by talking
        _log(f"flight: {fm['name']} -> {dest}")
        if not self.visit_npc(fm):
            return "working" if self.g.player().in_combat else "failed"
        self.bridge.send(br.TAXI, code, wait=1.5)   # gossip -> taxi map
        self.bridge.send(br.TAXI, code, wait=2.0)   # take the node
        t0 = time.time()
        moved = False
        while time.time() - t0 < 240:
            me = self.g.player()
            if math.dist(land[:2], (me.x, me.y)) < 80:
                _log(f"flight: landed at {dest}")
                time.sleep(2.0)
                return "done"
            if time.time() - t0 > 8 and not me.moving and not moved:
                break                                # never took off
            moved = moved or me.moving
            time.sleep(1.0)
        self.bridge.send(br.CLOSE_ALL)
        _log("flight: did not take off (path unknown or no money) - walking")
        return "skip"

    # -- hearthstone
    def set_hearth(self, step) -> str:
        """Bind at the innkeeper the guide has us at: an S step follows the inn
        hand-in, so an innkeeper named by the neighbouring steps wins over the
        nearest one (which was Stormwind's while we strayed there)."""
        me = self.g.player()
        idx = self.gsteps.index(step)
        near_npcs = {e for s in self.gsteps[max(0, idx - 3):idx + 3] for e in s.npcs}
        inn = None
        for entry in near_npcs:
            cand = self.spawn_of(entry)
            if cand and worlddb.creature_flags(entry) & 0x80:
                inn = dict(cand, title="Innkeeper")
                break
        inn = inn or self.nearest_service(me, "innkeeper")
        if not inn:
            return "skip"
        _log(f"hearth: binding at {inn['name']}")
        if not self.visit_npc(inn):
            return "working" if self.g.player().in_combat else "failed"
        # gossip -> binder option -> confirm popup: one press each
        self.bridge.send(br.BIND_HEARTH, wait=1.2)
        self.bridge.send(br.BIND_HEARTH, wait=1.0)
        self.bridge.send(br.CLOSE_ALL)
        self.hearth_note(bound=inn.get("entry"))
        return "done"

    def hearth_note(self, **fields):
        """Merge fields into data/hearth.json (used, bound)."""
        hs_file = os.path.join(PROGRESS_DIR, "hearth.json")
        try:
            d = json.load(open(hs_file))
        except Exception:
            d = {}
        d.update(fields)
        json.dump(d, open(hs_file, "w"))
        return d

    def use_hearth(self, dest: str = "") -> str:
        me = self.g.player()
        if me.in_combat:
            return "working"
        if self.g.bag_items().get(6948, 0) == 0:
            return "skip"
        hs_file = os.path.join(PROGRESS_DIR, "hearth.json")
        if dest:
            # "Hearth to Auberdine" with the stone bound in Westfall would
            # have hearthed the wrong way: only when bound near that place
            try:
                bound = json.load(open(hs_file)).get("bound")
            except Exception:
                bound = None
            node = next((v for k, v in br.TAXI_NODES.items() if k.lower() in dest.lower()), None)
            inn = self.spawn_of(bound) if bound else None
            if node is None or inn is None or math.dist(node[1][:2], inn["pos"][:2]) > 1500:
                _log(f"hearth: the stone is not bound near {dest} - walking instead")
                return "skip"
        try:
            last = json.load(open(hs_file))["used"]
        except Exception:
            last = 0
        if time.time() - last < 3600:
            _log(f"hearth: on cooldown for {(3600 - time.time() + last) / 60:.0f} more min")
            return "skip"
        _log("hearth: using the hearthstone")
        self.stop()
        self.bridge.send(br.USE_HEARTH, wait=0.5)
        start = (me.x, me.y)
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                me = self.g.player()
            except Exception:
                # the loading screen: the player object is gone for a few
                # seconds. That is the hearth firing, not a failure (an
                # unhandled error here retried the step and lost the cooldown)
                time.sleep(0.5)
                continue
            if math.dist((me.x, me.y), start) > 100:
                self.hearth_note(used=time.time())
                time.sleep(3.0)                      # loading screen settles
                self.home = (me.x, me.y, me.z)
                return "done"
            if me.in_combat:
                return "working"
            time.sleep(0.5)
        _log("  hearthstone did not fire (cooldown?) - walking instead")
        return "skip"

    # -- accept
    QUEST_LOG_CAP = 20            # 1.12 hard limit

    PRUNE_AT = 17                 # start making room here, not at the wall

    def abandon(self, qid: int, step=None) -> str:
        """The guide's own .abandon: drop a quest it says we are done with.

        129 of these in the chain, all of them the author saying "this one
        is not going to be finished" - a Stranglethorn Fever, a Path of
        Defense the route never returns to. Ignoring them is half of why the
        log fills up. Never drop work that is finished or in hand though:
        complete means it is one conversation from being xp.
        """
        log = self.g.quest_log()
        if qid not in log:
            return "done"
        if log[qid][0] & QUEST_COMPLETE:
            _log(f"  not abandoning quest {qid}: it is complete and worth handing in")
            return "skip"
        if step is not None and any(t in ("QC", "QT") and _qid(a) == qid
                                    for t, a in step.actions):
            return "skip"             # the step we are on still wants it
        title = (self.qdb.get(qid) or {}).get("title") or f"quest {qid}"
        _log(f"quest: abandoning '{title}' ({qid}) - the guide gives up on it here")
        self.bridge.send(br.ABANDON_QUEST, qid, wait=1.2)
        return "done" if qid not in self.g.quest_log() else "failed"

    def qt_step_of(self, qid: int) -> int | None:
        """Index of the last guide step that hands `qid` in."""
        idx = self.__dict__.get("_qt_step")
        if idx is None:
            idx = self._qt_step = {}
            for n, s in enumerate(self.gsteps):
                for t, a in s.actions:
                    if t == "QT":
                        try:
                            idx[_qid(a)] = n
                        except ValueError:
                            pass
        return idx.get(qid)

    def later_qids(self) -> set[int]:
        """Quests a LATER guide in this character's chain hands in.

        170 accepts in the chain are turned in by a different guide than the
        one that took them, so "not in the guide we are on" is no evidence
        that a quest is dead weight - and pruning on that alone threw away
        work that was still coming. Parsing the rest of the chain costs half
        a second, once.
        """
        got = self.__dict__.get("_later_qids")
        if got is not None:
            return got
        got = set()
        try:
            cat = rxp.catalog(self.who)
            chain = list(getattr(self, "chain", []) or [])
            here = chain.index(self.guide.name) if self.guide.name in chain else -1
            for name in (chain[here + 1:] if here >= 0 else []):
                for s in (cat[name].steps if name in cat else []):
                    for t, a in s.actions:
                        if t == "QT":
                            try:
                                got.add(_qid(a))
                            except ValueError:
                                pass
        except Exception as e:
            _log(f"  could not read the rest of the chain ({e}) - "
                 "treating cross-guide quests as still wanted")
            got = None                    # unknown: prune_quest_log keeps them
        self._later_qids = got if got is not None else set()
        self._later_known = got is not None
        return self._later_qids

    def spare_quests(self, log: dict, dead_only: bool = False) -> list[int]:
        """Quests in the log this run is not going to finish.

        "Appears anywhere in the loaded guide" used to mean "will be
        finished", so a quest whose turn-in we had already given up on could
        never be dropped while it still held one of the 20 slots. Being in
        the guide text is not evidence about THIS run; where the step
        pointer is, what was skipped and what the cost ledger says are.

        What IS evidence that a quest is still wanted is that something
        ahead of us hands it in - this guide from `here` on, OR any later
        guide in the chain. 170 of the chain's accepts are turned in by a
        different guide than the one that took them, so "the guide we are on
        has no QT for it" says nothing at all; abandoning on that alone
        throws away the accept travel and guarantees the later guide's QT
        step fails. An unreadable chain (later_known False) counts as
        "wanted", same as later_qids' docstring promises.

        dead_only drops the judgement calls (we gave up on the turn-in, the
        ledger says it costs more than it pays) and keeps only quests this
        route will never hand in. Both judgements are temporary - a ledger
        row expires three levels later - so they are worth a quest log slot
        right up until the log is actually at the wall.
        """
        out = []
        here = self.prog["step"]
        skipped = set(self.prog.get("skipped") or [])
        step = self.gsteps[here] if here < len(self.gsteps) else None
        later = self.later_qids()
        later_known = self.__dict__.get("_later_known", True)
        for q in log:
            if log[q][0] & QUEST_COMPLETE:
                continue                       # one conversation from being xp
            qt = self.qt_step_of(q) if q in self.qids else None
            ahead = qt is not None and qt >= here       # this guide still hands it in
            if not ahead and (q in later or not later_known):
                continue                       # ... or a later guide does
            if not ahead:
                out.append(q)                  # nothing on the route hands it in
                continue
            if dead_only:
                continue
            gave_up = step is not None and any(
                t == "QT" and i in skipped and _qid(a) == q
                for i, (t, a) in enumerate(step.actions))
            if gave_up or self.ledger_skip(q, count=False):
                out.append(q)
        return out

    def prune_quest_log(self, want: int = 0) -> int:
        """Abandon quests this run will never finish, to make room.

        The log caps at 20 and a full log makes every accept fail with no
        error the bot can see: it had 20 quests, 13 of them left behind in
        Elwynn, Westfall and Loch Modan, and had silently stopped picking
        anything up. 170 accepts in the chain are handed in by a LATER
        guide, so a dozen cross-guide quests in the bag is normal - which is
        why the room is made at PRUNE_AT, before the next accept is the one
        that silently fails.
        """
        log = self.g.quest_log()
        need = len(log) + want - self.QUEST_LOG_CAP          # must go
        room = len(log) + want - self.PRUNE_AT               # should go
        if room <= 0:
            return 0
        # Making room early (PRUNE_AT, before the accept that silently fails)
        # is worth doing for dead weight only. A quest we gave up on or that
        # the ledger dislikes is still ours to finish later, so it keeps its
        # slot until the log is genuinely at the wall.
        spare = self.spare_quests(log, dead_only=need <= 0)
        if not spare:
            if need > 0:
                _log(f"  quest log full ({len(log)}/{self.QUEST_LOG_CAP}) and every "
                     "quest belongs to this run or is ready to hand in")
            return 0
        # Cheapest first, where cheap means "least xp for the time it would
        # still take" - the quest level says what it was worth to whoever
        # wrote the guide, not what it is worth to us from here.
        led = self.cost_ledger()["quests"]

        def value(q):
            xp = (self.qdb.get(q) or {}).get("xp") or 0
            if q not in self.qids and q not in self.later_qids():
                return -1.0                    # not on this route at all
            spent = float((led.get(str(q)) or {}).get("secs", 0.0))
            return xp / max(60.0, spent + BUDGET_MIN)

        spare.sort(key=value)
        dropped = 0
        for q in spare[:max(need, min(room, len(spare)))]:
            title = (self.qdb.get(q) or {}).get("title") or f"quest {q}"
            self.bridge.send(br.ABANDON_QUEST, q, wait=1.2)
            if q not in self.g.quest_log():
                _log(f"  quest log {len(log)}/{self.QUEST_LOG_CAP} - abandoned '{title}' "
                     f"({q}): {'not in this guide' if q not in self.qids else 'this run is past it'}")
                dropped += 1
            else:
                _log(f"  could not abandon '{title}' ({q})")
        return dropped

    PICKUP_YD = 200.0

    def pick_up_missing(self, qid: int, step, depth: int = 0, anchor=None) -> str | None:
        """The guide hands in a quest it never had us take: take it now from
        its giver, if that is close. "working" while at it, None if not.

        RestedXP's survival guide turns in Distracting Jarven (308) with no
        step that accepts it, or its prerequisite - the Guarded Thunderbrew
        Barrel (403), taken and handed in at the barrel itself. Every dwarf
        and gnome lost that pair and the Bitter Rivals / Return to Marleth
        chain behind it, ~765 xp (Dsa, Dsaa, Ddqr). A prerequisite is followed
        one level deep, and only a talk-only one (no objectives)."""
        if depth > 1:
            return None
        for x in (qid,):
            if x not in self.qdb:
                try:
                    self.qdb.update(guides.quests([x]))
                except Exception:
                    return None
        q = self.qdb.get(qid)
        if not q or qid in self.quests_done():
            return None
        if anchor is None:
            # where the guide is sending us anyway: the hand-in
            end = self.pick_npc(step, q["ender_npcs"]) or                 (q["ender_objects"] and self.spawn_of(q["ender_objects"][0], "gameobject"))
            anchor = end["pos"][:2] if end else None
        why = self.prerequisites_met(qid)
        if why and why.startswith("needs quest "):
            prev = int(why.split()[2])
            if prev not in self.qdb:
                try:
                    self.qdb.update(guides.quests([prev]))
                except Exception:
                    return None
            pq = self.qdb.get(prev)
            if not pq or pq["kill"] or pq["collect"] or pq["use_object"] or pq["explore"] or pq["cast"]:
                return None
            if prev not in self.g.quest_log():
                return self.pick_up_missing(prev, step, depth + 1, anchor)
            r = self.turn_in(prev, step)
            return "working" if r in ("done", "working") else None
        if why:
            return None
        giver = self.pick_npc(step, q["starter_npcs"]) or             (q["starter_objects"] and self.spawn_of(q["starter_objects"][0], "gameobject"))
        if not giver:
            return None
        me = self.g.player()
        if (math.dist(giver["pos"][:2], (me.x, me.y)) > self.PICKUP_YD
                and not (anchor and math.dist(giver["pos"][:2], anchor) <= self.PICKUP_YD)):
            return None
        if self.autocomplete(qid):
            _log(f"quest: the guide never did '{q['title']}' ({qid}) - completing it at {giver['name']}")
            return "working" if self.complete_auto(qid, giver) else None
        _log(f"quest: the guide never took '{q['title']}' ({qid}) - taking it from {giver['name']}")
        r = self.accept(qid, step)
        return "working" if r in ("done", "working") else None

    def autocomplete(self, qid: int) -> bool:
        """Method 0: the giver completes it on the spot and it never enters
        the quest log (840 of the DB's quests - the Guarded Thunderbrew
        Barrel and Distracting Jarven among them). Accept presses do nothing
        for these and a turn-in that waits for it in the log never starts."""
        cache = self.__dict__.setdefault("_autoq", {})
        if qid not in cache:
            try:
                rows = worlddb._query(
                    "SELECT Method FROM quest_template WHERE entry=%d ORDER BY patch DESC LIMIT 1" % qid)
                cache[qid] = bool(rows) and int(rows[0][0]) == 0
            except Exception:
                cache[qid] = False
        return cache[qid]

    def complete_auto(self, qid: int, giver) -> bool:
        """Talk to the giver, pick the quest from its list, then press through
        complete -> reward. True if it went through (the reward panel closed
        with the frame gone, or a required item left the bags)."""
        q = self.qdb.get(qid) or {}
        if not self.talk_to(giver):
            return False
        bag0 = self.g.bag_items()
        self.bridge.send(br.ACCEPT_QUEST, qid, wait=0.5)     # selects it from the list
        for _ in range(6):
            self.bridge.send(br.TURN_IN_QUEST, qid, wait=0.4)
        time.sleep(0.3)
        self.bridge.send(br.CLOSE_ALL)
        items = [i for i, _ in q.get("collect", [])]
        ok = (not items) or any(self.g.bag_items().get(i, 0) < bag0.get(i, 0) for i in items)
        if ok:
            self.prog["rewarded"] = sorted(set(self.prog.get("rewarded", [])) | {qid})
            self.save_progress()
            _log(f"  done - xp {self.g.player().xp}")
        else:
            _log(f"  '{q.get('title', qid)}' did not complete at {giver['name']}")
        return ok

    def accept(self, qid: int, step) -> str:
        q = self.qdb.get(qid)
        if q is None:
            return "skip"
        if qid in self.g.quest_log() or qid in self.quests_done():
            return "done"
        why = self.prerequisites_met(qid)
        if why and why.startswith("needs level "):
            # A level short by a few kills: earn it here rather than lose the
            # quest. The guide's own "grind to 4" before Scalding Mornbrew
            # Delivery was met by banked turn-in xp (grind_to), and the
            # level-4 quest was then skipped 84 xp short (07:48).
            me = self.g.player()
            need = int(why.split()[-1])
            if need == me.level + 1 and me.next_xp - me.xp <= 0.3 * me.next_xp:
                short = me.next_xp - me.xp
                if self.banked_xp(me) >= short:
                    # finished quests pay the level: hand one in rather than
                    # walk 380 yd to grind 44 xp (07:50)
                    log = self.g.quest_log()
                    for cq in [c for c, st in log.items() if st and st[0] & QUEST_COMPLETE]:
                        if cq in self.qdb:
                            _log(f"quest: '{q['title']}' needs level {need}, {short} xp away - "
                                 f"handing in '{self.qdb[cq]['title']}' first")
                            r = self.turn_in(cq, step)
                            if r in ("done", "working"):
                                return "working"
                _log(f"quest: '{q['title']}' needs level {need}, "
                     f"{short} xp away - earning it first")
                r = self.grind_to(str(need), use_banked=False)
                return "working" if r == "done" else r      # then accept it
        if why:
            _log(f"quest: '{q['title']}' ({qid}) not available - {why} - skipping")
            return "skip"
        giver = self.pick_npc(step, q["starter_npcs"]) or \
            (q["starter_objects"] and self.spawn_of(q["starter_objects"][0], "gameobject"))
        if not giver:
            return "skip"
        self.prune_quest_log(want=1)
        if self.npc_banned(giver):
            return "skip"
        _log(f"quest: accepting '{q['title']}' ({qid}) from {giver['name']}")
        if not self.talk_to(giver):
            return "working" if self.g.player().in_combat else "failed"
        if qid in self.g.quest_log():
            return "done"
        # 1st press picks the quest from the NPC's list, the 2nd accepts the
        # detail page. Two fixed presses lost the race whenever the detail
        # panel was slow: the 2nd press re-selected instead of accepting and
        # the quest was never taken. 'Encroaching Gnolls' failed this way
        # three times at Guard Parker, which then skipped 'Assessing the
        # Threat' for needing it. Press until the quest log shows it.
        for attempt in range(6):
            # poll the log instead of a flat 1.2-1.6 s per press: the quest
            # usually lands within 0.5 s, and three accepts at one NPC were
            # ~12 s of standing (Sten Stoutarm, 16:11). The first press only
            # opens the quest from the gossip list, so do not sit 1 s on it.
            self.bridge.send(br.ACCEPT_QUEST, qid, wait=0.1)
            t_acc = time.time()
            while time.time() - t_acc < (0.4 if attempt < 3 else 1.2):
                if qid in self.g.quest_log():
                    break
                time.sleep(0.1)
            if qid in self.g.quest_log():
                break
        self.bridge.send(br.CLOSE_ALL)
        if qid in self.g.quest_log():
            return "done"
        _log(f"  '{q['title']}' did not go into the quest log after 6 presses")
        return "failed"

    # -- hand in
    def turn_in(self, qid: int, step) -> str:
        q = self.qdb.get(qid)
        if q is None:
            return "skip"
        if qid in self.quests_done():
            return "done"
        log = self.g.quest_log()
        if qid not in log:
            # never taken (or failed earlier) - or the guide forgot to take
            # it: pick it up if the giver is close
            return self.pick_up_missing(qid, step) or "skip"
        if not (log[qid][0] & QUEST_COMPLETE) and not self.objectives_met(q):
            # an objective we put aside earlier (death trap, too-strong mobs):
            # finish it now that the guide wants the quest handed in
            if getattr(self, "_finish_note", None) != qid:
                _log(f"quest: '{q['title']}' ({qid}) is not complete - finishing it first")
                self._finish_note = qid
            r = self.complete(qid, step)
            return "working" if r == "done" else r
        ender = self.pick_npc(step, q["ender_npcs"]) or \
            (q["ender_objects"] and self.spawn_of(q["ender_objects"][0], "gameobject"))
        if not ender:
            return "skip"
        if self.npc_banned(ender):
            return "skip"
        _log(f"quest: handing in '{q['title']}' ({qid}) at {ender['name']}")
        if not self.talk_to(ender):
            return "working" if self.g.player().in_combat else "failed"
        self.choose_reward(qid)
        # select -> complete -> take reward, one press each
        # The add-on acts on whichever quest frame is open, so a press too
        # early is harmless and simply repeated. Waiting 0.9 s after each
        # meant 1.8 s of standing at every hand-in, since only the third
        # press takes the quest out of the log; frames open in ~0.2 s here.
        for _ in range(8):
            self.bridge.send(br.TURN_IN_QUEST, qid, wait=0.1)
            t_ti = time.time()
            while time.time() - t_ti < 0.35 and qid in self.g.quest_log():
                time.sleep(0.05)
            if qid not in self.g.quest_log():
                break
        time.sleep(0.2)
        self.bridge.send(br.CLOSE_ALL)
        if qid not in self.g.quest_log():
            self.quests_rewarded = getattr(self, "quests_rewarded", 0) + 1
            self.prog["rewarded"] = sorted(set(self.prog.get("rewarded", [])) | {qid})
            self.save_progress()
            _log(f"  done - xp {self.g.player().xp}")
            return "done"
        return "failed"

    def objectives_met(self, q) -> bool:
        """Collect-only (or objective-less) quests: complete when the items are
        in the bags, even if the log's complete bit was not seen."""
        if q["kill"] or q["use_object"]:
            return False
        bag = self.g.bag_items()
        return all(bag.get(item, 0) >= need for item, need in q["collect"])

    def prerequisites_met(self, qid: int) -> str | None:
        """None when the quest can be taken, else why not."""
        rows = worlddb._query(
            "SELECT PrevQuestId, MinLevel FROM quest_template WHERE entry=%d "
            "ORDER BY patch DESC LIMIT 1" % qid)
        if not rows:
            return None
        prev, min_level = int(rows[0][0]), int(rows[0][1])
        if prev > 0 and prev not in self.quests_done():
            return f"needs quest {prev} first"
        if min_level > self.g.player().level:
            return f"needs level {min_level}"
        return None

    def choose_reward(self, qid: int):
        """Tell the add-on which reward to take (1-based), scored by gear.py."""
        rows = worlddb._query(
            "SELECT RewChoiceItemId1, RewChoiceItemId2, RewChoiceItemId3, RewChoiceItemId4, "
            "RewChoiceItemId5, RewChoiceItemId6 FROM quest_template WHERE entry=%d "
            "ORDER BY patch DESC LIMIT 1" % qid)
        choices = [int(c) for c in rows[0] if int(c)] if rows else []
        if len(choices) < 2:
            self.bridge.send(br.REWARD_CHOICE, 1)
            return
        me = self.g.player()
        worn, _ = gear.worn_and_bag(self.g)
        pick = gear.best_choice(choices, me.class_id, me.level, gear.known_skills(self.g), worn)
        info = gear.items(choices)
        _log(f"  reward: {info.get(choices[pick - 1], {}).get('name', choices[pick - 1])}")
        self.bridge.send(br.REWARD_CHOICE, pick)

    # -- objectives
    @staticmethod
    def objective_done(q, entry, target: int, need: int) -> bool:
        """Has this one kill/use objective already been filled?

        The whole quest used to be hunted as a single set, so a quest that
        wanted six Rockjaw Troggs and six Burly ones kept attacking Burlies
        after they were 6/6 - the Burly is simply the one that walks past
        first.

        The 1.12 quest log packs the four counters into one word at **six
        bits each**, not one byte each: the word 0x181 for that quest reads
        [1, 6] as 6-bit fields (correct) and [129, 1] as bytes (nonsense).
        The complete flag sits above them at 0x01000000.
        """
        slot = q.get("slot", {}).get(target)
        if slot is None:
            return False                    # unknown slot: hunt it as before
        return ((entry[0] >> (6 * slot)) & 0x3F) >= need

    def complete(self, qid: int, step, obj: int | None = None) -> str:
        q = self.qdb.get(qid)
        log = self.g.quest_log()
        if q is None or qid not in log:
            return "skip"
        if log[qid][0] & QUEST_COMPLETE:
            _log(f"quest: '{q['title']}' objectives complete")
            return "done"
        if obj is not None:
            # ".complete 317,2" is objective 2 only (Thick Bear Fur): with the
            # fur in the bag the step is done - the guide gets the boar ribs
            # later. Treating it as the whole quest farmed ribs for 9 minutes.
            objs = self.objectives(q)
            if 0 <= obj < len(objs):
                kind, e, name, need = objs[obj]
                if kind == "item":
                    if self.g.bag_items().get(e, 0) >= need:
                        _log(f"quest: '{q['title']}': {name} done")
                        return "done"
                    src = self.item_src.get(e, {})
                    if not (src.get("creatures") or src.get("objects")):
                        return self.explore(qid, q["title"])
                    return self.hunt(set(src.get("creatures", set())), set(src.get("objects", set())),
                                     step, q["title"])
                if self.objective_done(q, log[qid], e, need):
                    _log(f"quest: '{q['title']}': {name} done")
                    return "done"
                return self.hunt({e} if kind == "kill" else set(), {e} if kind == "object" else set(),
                                 step, q["title"])
        kill = {c for c, need in q["kill"] if not self.objective_done(q, log[qid], c, need)}
        drop_mobs, objects = set(), set()
        bag = self.g.bag_items()
        for item, need in q["collect"]:
            if bag.get(item, 0) >= need:
                continue
            src = self.item_src.get(item, {})
            drop_mobs |= src.get("creatures", set())
            objects |= src.get("objects", set())
        objects |= {o for o, need in q["use_object"]
                    if not self.objective_done(q, log[qid], o, need)}
        if not (kill or drop_mobs or objects):
            return self.explore(qid, q["title"])
        return self.hunt(kill | drop_mobs, objects, step, q["title"])

    def explore(self, qid: int, title: str) -> str:
        """Exploration quests ("Scout through the Fargodeep Mine"): walk into
        the quest's area trigger. Positions come from the world DB."""
        rows = worlddb._query(
            "SELECT t.x, t.y, t.z, t.radius FROM areatrigger_involvedrelation r "
            "JOIN areatrigger_template t ON t.id = r.id WHERE r.quest=%d AND t.map_id=%d" % (qid, self.continent()))
        if not rows:
            return "skip"                       # item-use / event quest: not supported
        me = self.g.player()
        pts = [tuple(float(v) for v in r) for r in rows]
        tries = getattr(self, "_explore_tries", {})
        pts = [p for p in pts if tries.get((qid, p[:2]), 0) < 2]
        if not pts:
            return "failed"
        x, y, z, r = min(pts, key=lambda p: math.dist(p[:2], (me.x, me.y)))
        trap = self.death_trap()
        if trap and math.dist(trap, (x, y)) < 120:
            _log(f"quest: '{title}' trigger is where we keep dying - leaving it for later")
            return "failed"
        _log(f"quest: exploring for '{title}', trigger {math.dist((x, y), (me.x, me.y)):.0f} yd away")
        self.navigate(x, y, z, stop_short=max(1.0, r * 0.4), timeout=300)
        time.sleep(1.0)
        me = self.g.player()
        if math.dist((x, y), (me.x, me.y)) < r + 5:
            # we stood in it: only that counts as a try (fights on the way
            # used to burn all the tries before we ever got there)
            tries[(qid, (x, y))] = tries.get((qid, (x, y)), 0) + 1
            self._explore_tries = tries
        return "working"                        # next round sees the complete bit

    DRINK_ITEMS = {1179, 159, 1205, 1708, 1645, 8766}     # milk, water: not needed with Conjure Water

    def collect(self, item: int, need: int, step) -> str:
        if self.g.bag_items().get(item, 0) >= need:
            return "done"
        sellers = guides.item_vendors(item)
        if sellers:
            # "Buy up to N ..." guide lines: a shopping step, not a drop
            known = spells.known_by_name(self.p)
            rows = worlddb._query(f"SELECT class, subclass FROM item_template WHERE entry={item}")
            consumable = bool(rows) and int(rows[0][0]) == 0
            if (item in self.DRINK_ITEMS and "Conjure Water" in known) or                     (consumable and "Conjure Food" in known and "Conjure Water" in known):
                _log(f"  item {item} is food or drink we can conjure - skipping the purchase")
                return "skip"
            return self.buy(item, need, sellers)
        src = guides.item_sources([item]).get(item, {})
        if not (src.get("creatures") or src.get("objects")):
            return "skip"
        return self.hunt(src.get("creatures", set()), src.get("objects", set()), step,
                         f"item {item}")

    def buy(self, item: int, need: int, sellers: set[int]) -> str:
        me = self.g.player()
        vendor = self.nearest_spawn_of(sellers)
        if vendor is None:
            return "skip"
        have = self.g.bag_items().get(item, 0)
        rows = worlddb._query(f"SELECT buy_price, name FROM item_template WHERE entry={int(item)} LIMIT 1")
        if rows and int(rows[0][0]) * (need - have) > me.money:
            # the guide's Large Axe is 4s84c at level 6 with 1s in the bag:
            # three trips to the smith and 30 s for a purchase that cannot happen
            _log(f"shopping: {rows[0][1]} costs {int(rows[0][0]) * (need - have)}c, "
                 f"we have {me.money}c - skipping")
            return "skip"
        _log(f"shopping: {need - have} x item {item} from {vendor['name']}")
        if not self.visit_npc(dict(vendor, title="Vendor")):
            return "working" if self.g.player().in_combat else "failed"
        self.bridge.send(br.OPEN_MERCHANT, wait=1.0)
        self.bridge.send(br.BUY_ITEM, item * 100 + (need - have), wait=1.5)
        self.bridge.send(br.CLOSE_ALL)
        return "done" if self.g.bag_items().get(item, 0) >= need else "failed"

    def hunt(self, mobs: set[int], objects: set[int], step, what: str) -> str:
        """One unit of work toward kill/collect objectives."""
        me = self.g.player()
        # what we are here to kill: travel must walk up to these, not round them
        self._hunt_mobs = (set(mobs), time.time())
        # 1. a matching mob in view
        info = worlddb.creatures(mobs) if mobs else {}
        strong = me.level + self.STRONG_LEVELS
        if info and all(ci.min_level >= strong for ci in info.values()):
            need = min(ci.min_level for ci in info.values()) - self.STRONG_LEVELS + 1
            _log(f"quest: {what} needs level {need} (mobs of level "
                 f"{min(ci.min_level for ci in info.values())}) - leaving it for later")
            return "skip"          # the hand-in step comes back to it (turn_in -> complete)
        cands = [u for u in self.g.units() if u.entry in mobs and not u.dead
                 and not u.tapped_by_other and me.dist(u) < SEARCH_RADIUS
                 and u.level < strong
                 and self.blacklist.get(u.guid, 0) < time.time()
                 and self.pack_mobs.get(u.entry, 0) < time.time()]
        # mine levels: close in the air, far on foot - those are not in view.
        # Rank by the walk, not the straight line (a mob over a ridge is the
        # "nearest" and a long way round)
        walk = {}
        for u in sorted(cands, key=me.dist)[:6]:
            w = self.walk_dist(me, u)
            if w is not None and w < 1.6 * me.dist(u) + 8:
                walk[u.guid] = w
        cands = [u for u in cands if u.guid in walk]
        if cands:
            # Camps (Defias, kobolds) stand in groups: prefer a target with no
            # other hostile near it, like the grinder does. A target with adds
            # costs +40 yd per add; two or more adds only as a last resort.
            units = self.g.units()
            uinfo = worlddb.creatures({u.entry for u in units})
            hostile = [u for u in units if not u.dead and not u.npc_flags
                       and factions.is_hostile(me.faction, u.faction)
                       and not (uinfo.get(u.entry) and uinfo[u.entry].critter)]

            def seg_dist(h, c):
                # distance from h to our walk in to c (straight line)
                ax, ay, bx, by = me.x, me.y, c.x, c.y
                L = (bx - ax) ** 2 + (by - ay) ** 2 or 1.0
                f = max(0.0, min(1.0, ((h.x - ax) * (bx - ax) + (h.y - ay) * (by - ay)) / L))
                return math.hypot(h.x - (ax + f * (bx - ax)), h.y - (ay + f * (by - ay)))

            def adds(c):
                # from level 5 a mob our level or above joins from 11-14 yd,
                # and one we walk past on the way in comes too: a level-7
                # warrior crossed 56 yd of leopards to a bear and died to the pair
                return sum(1 for h in hostile if h.guid != c.guid and (
                    h.dist(c) < add_radius(me.level, c, me, self.profile.ranged)
                    or (me.level >= 5 and h.level >= me.level
                        and (h.dist(c) < 14.0 or seg_dist(h, c) < 12.0))))
            # never walk to a mob fight() will refuse on arrival (see
            # pair_mates): take the nearest one it will actually fight
            thr = self.threats()
            pullable = [c for c in cands if not self.pair_mates(me, c, thr)]
            cands = pullable or cands
            # a much stronger mob next to the target would join the fight
            cands = [c for c in cands if not any(
                h.guid != c.guid and h.level >= strong
                and h.dist(c) < add_radius(me.level, c, me, self.profile.ranged) + 6
                for h in hostile)] or cands
            # a mob above our level costs a melee class ~60% hp alone (a level-8
            # Elder Crag Boar took a level-7 warrior 72 -> 11%): take the
            # ones our level first, even if they are further off
            t = min(cands, key=lambda c: walk.get(c.guid, me.dist(c)) + 40 * adds(c)
                    + (25 * max(0, c.level - me.level) if not self.profile.ranged else 0))
            if adds(t) >= 2 and me.dist(t) > 25 and getattr(self, "_lone_waits", 0) < 8:
                # wait a little for the group to split up (patrols move)
                # instead of pulling all of it; static camps never do, so
                # after ~16 s take the best target anyway
                self._lone_waits = getattr(self, "_lone_waits", 0) + 1
                _state("questing", detail=f"{what}: waiting for a lone target")
                time.sleep(2.0)
                return "working"
            self._lone_waits = 0
            # not a fresh pull on an empty tank: at 12:47 a rest at 54% hp /
            # 24% mana stepped away from one wendigo and this pulled the next
            # (18% hp, an escape). Rest first unless it is already on us.
            pm = self.profile.pull_mana if self.profile.pull_mana is not None else self.args.pull_mana
            if t.target != me.guid and (me.hp_pct < self.args.pull_hp
                                        or (self.profile.drinks and me.mana_pct < pm)):
                _log(f"quest: {what}: hp {me.hp_pct:.0f}% mana {me.mana_pct:.0f}% - "
                     "resting before the next pull")
                return "working"
            _state("questing", detail=f"{what}: {info.get(t.entry).name if info.get(t.entry) else t.entry}")
            self.fight(t, what)
            return "working"
        # 2. a quest object in view
        gos = [o for o in self.g.gameobjects() if o["entry"] in objects
               and math.dist((o["x"], o["y"]), (me.x, me.y)) < SEARCH_RADIUS
               and self.blacklist.get(o["guid"], 0) < time.time()]
        if gos:
            # a thug next to the crate interrupts the opening cast: clear it first
            o = min(gos, key=lambda o: math.dist((o["x"], o["y"]), (me.x, me.y)))
            near = [h for h in self.aggressors() if not h.dead
                    and math.dist((h.x, h.y), (o["x"], o["y"])) < 14 and me.dist(h) < 30
                    and self.blacklist.get(h.guid, 0) < time.time()]
            if near:
                t = min(near, key=me.dist)
                friends = [h for h in self.threats() if h.guid != t.guid and not h.dead
                           and h.dist(t) < add_radius(me.level, t, me, self.profile.ranged)]
                if friends:
                    _log(f"  {worlddb.name(t.entry)} and {len(friends)} friend(s) guard the object"
                         " - trying another one")
                    self.blacklist[o["guid"]] = time.time() + 120
                    return "working"
                _state("questing", detail=f"{what}: clearing {worlddb.name(t.entry)} near the object")
                self.fight(t, f"{what} (clearing the object)")
                return "working"
            if getattr(self, "_obj_fails", 0) >= 4:
                # objects can't be used from the background yet (see
                # use_object): give up on this objective rather than loop
                return "failed"
            o = min(gos, key=lambda o: math.dist((o["x"], o["y"]), (me.x, me.y)))
            self._obj_fails = 0 if self.use_object(o) else getattr(self, "_obj_fails", 0) + 1
            return "working"
        # 3. go where they spawn - not into a camp of a kind that swarms us
        packs = {m for m in mobs if self.pack_mobs.get(m, 0) > time.time()}
        if packs and not (mobs - packs) and not objects:
            _log(f"quest: {what}: {', '.join(worlddb.name(m) for m in packs)} come in packs - leaving it for later")
            return "skip"
        target = self.nearest_spawn(mobs - packs, objects, step)
        if target is None:
            named = self.named_spawn(mobs - packs)
            if named is None:
                return "failed"
            # ... but not when the spawns were ruled out as guarded by
            # stronger mobs: "leaving it for later", then "going back to the
            # named mob's spawn" walked into Fargodeep Mine for Goldtooth,
            # three kobolds joined and the escape died (05:19)
            if time.time() - getattr(self, "_guarded_veto", 0) < 5:
                return "failed"
            trap = self.death_trap()
            if trap and math.dist(trap, named[:2]) < 120:
                _log(f"quest: {what}: the named mob's spawn is where we keep dying - leaving it for later")
                return "failed"
            # a named quest mob (Grik'nir, 1 spawn) is not "nowhere" because
            # we have been to its spot: it is dead and respawns in a minute
            # or two. Go there and wait instead of skipping the quest.
            if math.dist(named[:2], (me.x, me.y)) > 25:
                _log(f"quest: {what}: going back to the named mob's spawn, "
                     f"{math.dist(named[:2], (me.x, me.y)):.0f} yd")
                if (not self.navigate(*named, stop_short=15.0, timeout=300, careful=True)
                        and not self.g.player().in_combat):
                    return "failed"          # no way there: not a loop of 1 s retries
                return "working"
            w0 = self.__dict__.setdefault("_respawn_wait", {}).setdefault(named[:2], time.time())
            if time.time() - w0 > 360:
                _log(f"quest: {what}: waited 6 min at the spawn - giving up")
                return "failed"
            _state("questing", detail=f"{what}: waiting for the respawn "
                                      f"({time.time() - w0:.0f}s)")
            time.sleep(2.0)
            return "working"
        trap = self.death_trap()
        if trap and math.dist(trap, target[:2]) < 120:
            _log(f"quest: {what} area is where we keep dying - leaving it for later")
            return "failed"
        # a camp around the spot (Bingles' supplies lie among 5 troggs):
        # do not walk in - stand off 35 yd and pull them one at a time
        camp = [u for u in self.aggressors() if not u.dead and u.level >= me.level - 4
                and math.dist((u.x, u.y), target[:2]) < 30
                and self.blacklist.get(u.guid, 0) < time.time()]
        if len(camp) >= 3:
            dx, dy = me.x - target[0], me.y - target[1]
            d = math.hypot(dx, dy) or 1.0
            edge = (target[0] + dx / d * 35.0, target[1] + dy / d * 35.0, me.z)
            # 35 yd back along the walk in, not along a straight line: in the
            # Frostmane cave the straight-line spot was in the rock and snapped
            # to the mountain 100 yd above ("climb +100 yd", a minute stuck)
            try:
                path = self.nav.find_path((me.x, me.y, me.z), tuple(target[:3]), budget=2.0) if self.nav else None
            except Exception:
                path = None
            if path and len(path) >= 2:
                edge, left = tuple(path[-1]), 35.0
                for a, b in zip(reversed(path[:-1]), reversed(path[1:])):
                    seg = math.dist(a, b)
                    if seg >= left:
                        f = left / seg
                        edge = tuple(b[i] + (a[i] - b[i]) * f for i in range(3))
                        break
                    left -= seg
                    edge = tuple(a)
            if math.dist((me.x, me.y), edge[:2]) > 6:
                _log(f"quest: {what} area is a camp of {len(camp)} - pulling from the edge")
                if (not self.navigate(*edge, stop_short=3.0, timeout=120, careful=True)
                        and not self.g.player().in_combat and not self.fought_recently()):
                    # the edge spot is off the mesh: try another spawn point.
                    # Not after a fight on the way: a whelp pulled en route
                    # marked Grik'nir's only spawn unreachable for 15 min and
                    # The Stolen Journal was skipped standing next to him.
                    self._unreachable = getattr(self, "_unreachable", []) + [(target[0], target[1], time.time() + 900)]
                return "working"
            t = min(camp, key=me.dist)
            friends = [u for u in camp if u.guid != t.guid and u.dist(t) < 12]
            if len(friends) >= 2:
                _log(f"quest: {what}: {worlddb.name(t.entry)} stands with {len(friends)} others - leaving the camp for later")
                # the whole camp, not just this spot: the next spawn point
                # 40 yd over was the same camp (dead in 8 s)
                self._unreachable = getattr(self, "_unreachable", []) + [(target[0], target[1], time.time() + 900, 70.0)]
                return "working"
            _state("questing", detail=f"{what}: pulling {worlddb.name(t.entry)} from the camp edge")
            self.fight(t, f"{what} (camp edge)")
            return "working"
        # Ask the mesh before walking. A spawn point inside a cave the mesh
        # does not enter, or on a ledge above us, used to be found out by
        # walking at it: the Grizzled Den's point sent the paladin up the
        # hillside above the cave twice, stuck five times each, 3 min a go.
        # A point with no route and no reachable ground within 15 yd at its
        # own height is out of reach now, without the walk.
        if self.nav and math.dist(target[:2], (me.x, me.y)) > 20:
            try:
                start, goal = (me.x, me.y, me.z), tuple(target[:3])
                ok_here = (self.nav.find_path(start, goal, budget=2.0)
                           or self.nav.reachable_near(start, goal, radius=15.0, dz=6.0))
            except Exception:
                ok_here = True               # the check itself failed: walk as before
            if not ok_here:
                self._unreachable = getattr(self, "_unreachable", []) + [(target[0], target[1], time.time() + 1800)]
                _log(f"  spawn point ({target[0]:.0f}, {target[1]:.0f}) has no route on the mesh - trying another")
                return "working"
        _log(f"quest: heading to {what} area, {math.dist(target[:2], (me.x, me.y)):.0f} yd")
        if self.navigate(*target, stop_short=15.0, timeout=300, careful=True):
            # only a spot we actually reached counts as searched: a fight on
            # the way used to burn the single Goldtooth spawn in three tries
            self._searched = getattr(self, "_searched", []) + [target[:2]]
            self.__dict__.setdefault("_unreach_run", {}).pop((what, round(time.time() / 1800)), None)
        elif not self.g.player().in_combat:
            # no mesh route (a trogg spawn inside a cave the mesh does not
            # enter): three 3-minute searches for the same point in Thelsamar
            self._unreachable = getattr(self, "_unreachable", []) + [(target[0], target[1], time.time() + 1800)]
            _log(f"  spawn point ({target[0]:.0f}, {target[1]:.0f}) is out of reach - trying another")
            # three out of reach in a row: the camp is up a ridge the mesh
            # cannot get us onto (Shimmer Ridge's seers: 3 x 3 minutes at
            # 1 yd/s). Give the step up; the hand-in step comes back to it.
            key = (what, round(time.time() / 1800))
            n = self.__dict__.setdefault("_unreach_run", {}).get(key, 0) + 1
            self._unreach_run[key] = n
            if n >= 3:
                _log(f"quest: {what}: 3 spawn points out of reach - leaving it")
                self._unreach_run[key] = 0
                return "failed"
        return "working"

    STRONG_LEVELS = 2            # a mob this many levels above us is not worth the fight (a lvl-14 watcher at 12: 79->19% hp)

    def named_spawn(self, mobs):
        """The spawn point of a named quest mob (at most 2 spawns in the
        world, not elite), or None."""
        if not mobs:
            return None
        # only when EVERY source is named: Bear Meat also drops from a rare
        # bear with one spawn, and "wait for him" looped once a second
        # instead of hunting the ordinary bears
        info = worlddb.creatures(set(mobs))
        best = None
        me = self.g.player()
        for m in mobs:
            if info.get(m) and info[m].rank > 0:
                return None
            rows = worlddb._query(f"SELECT position_x, position_y, position_z FROM creature "
                                  f"WHERE map={self.continent()} AND id={int(m)}")
            if not 1 <= len(rows) <= 2:
                return None
            p = min((tuple(float(v) for v in r) for r in rows),
                    key=lambda p: math.dist(p[:2], (me.x, me.y)))
            if best is None or math.dist(p[:2], (me.x, me.y)) < math.dist(best[:2], (me.x, me.y)):
                best = p
        return best

    def fought_recently(self, secs=20.0) -> bool:
        return time.time() - getattr(self, "last_fight_end", 0) < secs

    def mesh_reachable(self, me, pt) -> bool:
        """Is pt on the same connected patch of navmesh as we are?"""
        nav = getattr(self, "nav", None)
        if nav is None:
            return True
        try:
            comp = nav.components()
            a, _ = nav.nearest_poly(me.x, me.y, me.z, max_dist=12.0)
            b, _ = nav.nearest_poly(pt[0], pt[1], pt[2], max_dist=12.0)
            if a is None or b is None:
                return True                  # let the walk decide
            return comp.get(a) == comp.get(b)
        except Exception:
            return True

    def strong_spawn_near(self, me, pt, radius=45.0) -> bool:
        """A mob at least STRONG_LEVELS above us spawns within radius of pt
        (Harvest Watchers in Saldean's fields killed a level-11 mage)."""
        import dangers
        for m, x, y, z, wander, entry, lo, hi, rank, fac, det in dangers._load():
            if m != self.continent() or hi < me.level + self.STRONG_LEVELS:
                continue
            if math.dist((x, y), pt[:2]) < radius + wander and factions.is_hostile(me.faction, fac):
                return True
        return False

    def nearest_spawn(self, mobs, objects, step):
        me = self.g.player()
        pts = []
        if step.goto:
            w = worldmap.to_world(*step.goto)
            if w:
                pts.append((w[1], w[2], me.z))
        def spawns(table, ids):
            rows = worlddb._query(
                f"SELECT position_x, position_y, position_z FROM {table} WHERE map={self.continent()} AND id IN "
                f"({','.join(map(str, ids))})") if ids else []
            return [tuple(float(v) for v in r) for r in rows]
        # rares (Slark, 2-3x the health) are not a farm; and a murloc oracle
        # standing among coastrunners is a coastrunner pull
        info = worlddb.creatures(set(mobs))
        mobs = {m for m in mobs if not (info.get(m) and info[m].rank > 0)}
        # nor a kind that outlevels us (Longshore's murloc warriors are 15-16)
        mobs = {m for m in mobs if not (info.get(m) and info[m].min_level >= me.level + self.STRONG_LEVELS)}
        mob_pts = spawns("creature", mobs)
        packs = {int(k) for k, v in self.pack_mobs.items() if v > time.time()}
        if packs and mob_pts:
            ppts = spawns("creature", packs)
            mob_pts = [p for p in mob_pts if all(math.dist(p[:2], q[:2]) > 30 for q in ppts)]
        # a camp of the kind we hunt (five murloc oracles within 25 yd) is a
        # multi-pull whatever its entry: leave those spawn points out
        # ... counting every hostile spawn around it, not only the hunted
        # kind: Longshore interleaves coastrunners, warriors and oracles
        import dangers
        hostile = [(x, y, hi) for m, x, y, z, w, entry, lo, hi, rank, fac, det in dangers._load()
                   if m == self.continent() and math.dist((x, y), (me.x, me.y)) < 1600
                   and factions.is_hostile(me.faction, fac)]
        # ... and a pair at our level (Defias trapper 13 + smuggler 12 by
        # the Sentinel Hill road) killed us on arrival: no camps, no pairs
        open_pts = [p for p in mob_pts
                    if sum(1 for q in hostile if math.dist(p[:2], q[:2]) < 35) < 3
                    and sum(1 for q in hostile if math.dist(p[:2], q[:2]) < 30
                            and q[2] >= me.level - 1) < 2]
        # ... unless that rules out a named quest mob altogether: Grik'nir's
        # one spawn sits among 8 cave trolls, and "no spawn points" failed
        # The Stolen Journal and its follow-up chain. The camp-edge pulling
        # in the caller deals with whichever trolls are actually alive.
        if mob_pts and not open_pts and len(mob_pts) <= 2:
            _log("quest: the only spawn is inside a camp - going anyway, pulling from the edge")
            open_pts = mob_pts
        mob_pts = open_pts
        obj_pts = spawns("gameobject", objects)
        if mob_pts and obj_pts:
            # Generic containers (Food Crate 3662...) stand in every town too:
            # only those near the mobs that drop the item are quest sources.
            # Stormwind's canal crates once drew the bot 800 yd away.
            obj_pts = [o for o in obj_pts if any(math.dist(o[:2], m[:2]) < 150 for m in mob_pts)]
        pts += mob_pts + obj_pts
        # the item's other sources live in other zones: with the Westfall
        # coastrunners ruled out (packs), the "nearest" murloc for the stew
        # was 10,694 yd away and the bot walked straight toward Wetlands.
        # Measured from the step's own coordinates, not from us: after a
        # training trip to Ironforge every Loch Modan step was "not in this
        # zone" and the guide skipped its way through the whole zone.
        # 1500 yd was tight even measured from the step: Gathering Idols'
        # trogg caves are 1598 yd from its coordinates and got skipped. A
        # zone is 2000-4000 yd across; the case this guards against (a
        # Wetlands murloc for a Westfall stew) was 10,694 yd away.
        ZONE_REACH = 2500
        anchor = pts[0][:2] if (step.goto and pts) else (me.x, me.y)
        far = [p for p in pts if math.dist(p[:2], anchor) > ZONE_REACH]
        if far and len(far) == len(pts):
            _log(f"quest: nearest spawn is {min(math.dist(p[:2], anchor) for p in pts):.0f} yd "
                 "from the step - not in this zone")
            return None
        pts = [p for p in pts if math.dist(p[:2], anchor) <= ZONE_REACH]
        # skip spawns we just searched, and spawns guarded by much stronger mobs
        seen = getattr(self, "_searched", [])
        pts = [p for p in pts if all(math.dist(p[:2], s) > 25 for s in seen[-6:])]
        gone = [u for u in getattr(self, "_unreachable", []) if u[2] > time.time()]
        pts = [p for p in pts if all(math.dist(p[:2], u[:2]) > (u[3] if len(u) > 3 else 25) for u in gone)]
        # drop spawns on a disconnected patch of mesh in one go: the Bingles
        # camp sits on an isolated 1022-poly island, and trying its spawn
        # points one at a time was five walks to nowhere per quest step
        walkable = [p for p in pts if self.mesh_reachable(me, p)]
        if pts and not walkable:
            _log(f"quest: all {len(pts)} spawn points are on unreachable ground - skipping")
            return None
        pts = walkable
        safe = [p for p in pts if not self.strong_spawn_near(me, p)]
        if pts and not safe:
            _log("quest: every spawn point is guarded by stronger mobs - leaving it for later")
            self._guarded_veto = time.time()
        pts = safe
        if not pts:
            self._searched = []
            return None
        # nearest, but a spot ringed by mobs of our level costs 40 yd per mob:
        # the sack of oats between two harvest golems was a death
        import dangers
        crowd = [(x, y, w) for m, x, y, z, w, e, lo, hi, rank, fac, det in dangers._load()
                 if m == self.continent() and hi >= me.level and factions.is_hostile(me.faction, fac)
                 and math.dist((x, y), (me.x, me.y)) < 600]

        zones = dangers.zones(0, me.level, me.faction,
                              bbox=(me.x - 700, me.y - 700, me.x + 700, me.y + 700))

        def cost(p):
            n = sum(1 for x, y, w in crowd if math.dist((x, y), p[:2]) < 30 + w)
            inside = any(math.dist(z[:2], p[:2]) < z[3] for z in zones)
            return math.dist(p[:2], (me.x, me.y)) + 40 * n + (250 if inside else 0)
        return min(pts, key=cost)

    LOOT_SLOT_PX = (50, 255)          # first loot button, 1920x1080 client, default UI scale

    def use_object(self, o, quest_giver=False) -> bool:
        """Open / use a game object (quest crates, plants...) like a player.
        quest_giver: the object opens a quest dialog (a half-eaten body, a
        wanted poster): stop right after the click and leave the frame open
        for the accept / hand-in commands.

        The click-to-move "use object" order never opens chests: the client
        only opens them from its own right-click handling (a hidden "Opening"
        cast), and the server ignores a plain use packet on a chest. So this
        walks to ~3 yd, finds the object on screen with the mouse-over GUID,
        sends a genuine right-click (the window is focused for a moment), and
        takes the loot with a genuine click on the loot slot - the client's
        auto-loot flag only covers corpses.
        """
        import offsets as O
        import mouse
        import screenpos
        me = self.g.player()
        d = math.dist((o["x"], o["y"]), (me.x, me.y))
        if d > 4.5 or d < 1.5:
            ang = math.atan2(me.y - o["y"], me.x - o["x"])
            for turn in (0.0, 1.5, -1.5, 3.1):          # a fence or row may block one side
                a = ang + turn
                self.navigate(o["x"] + 3 * math.cos(a), o["y"] + 3 * math.sin(a), o["z"],
                              stop_short=0.3, timeout=30)
                me = self.g.player()
                if math.dist((o["x"], o["y"]), (me.x, me.y)) <= 4.5:
                    break
            self.stop()
            time.sleep(0.3)
        me = self.g.player()
        self.ctm.face(me, o["x"], o["y"])                # camera follows the facing
        want = math.atan2(o["y"] - me.y, o["x"] - me.x)
        t0 = time.time()
        while time.time() - t0 < 2.5:                    # turning is rate-limited now
            f = self.g.player().facing
            if abs((want - f + math.pi) % (2 * math.pi) - math.pi) < 0.15:
                break
            time.sleep(0.05)
        time.sleep(0.5)                                  # let the camera catch up
        self.aim_camera(o["x"], o["y"])                  # it does not, standing still
        before = self.quest_counts()
        got = False
        pos = None
        for _ in range(2):
            pos = screenpos.find_on_screen(self.p, o["guid"], o["x"], o["y"], o["z"],
                                           heights=(0.5, 0.2, 0.9, 1.3, 0.0))
            if pos:
                break
            time.sleep(0.8)
        if pos is None and quest_giver:
            # a quest object the camera cannot see (the Guarded Thunderbrew
            # Barrel in the Kharanos cellar, 19:05): use it by click-to-move,
            # the order a right-click writes (action 7 = use object)
            _log(f"  object {o['entry']} not found on screen - using it by click-to-move")
            me = self.g.player()
            self.ctm.interact(7, o["guid"], me, o["x"], o["y"], o["z"])
            time.sleep(1.5)
            self.ctm.reset_fields()
            return True
        if pos is None:
            _log(f"  object {o['entry']} not found on screen")
        elif not mouse.real_right_click(self.p.hwnd, *pos):
            _log("  could not focus the game window for the click")
        elif quest_giver:
            time.sleep(1.5)                              # the quest frame opens
            self.ctm.reset_fields()
            return True
        else:
            loot_base = self.g.player().base + O.PLAYER_LOOT_GUID
            t0 = time.time()
            # the "Opening" cast takes a moment; the loot window then opens
            while time.time() - t0 < 4 and not self.p.u64(loot_base) and self.quest_counts() == before:
                if self.g.player().in_combat:
                    break
                time.sleep(0.2)
            seen_loot = bool(self.p.u64(loot_base))
            clicks = 0
            for _ in range(4):                          # take every slot (usually one)
                if self.quest_counts() != before or self.g.player().in_combat:
                    break
                mouse.real_left_click(self.p.hwnd, *self.LOOT_SLOT_PX)
                clicks += 1
                time.sleep(0.7)
            time.sleep(0.3)
            got = self.quest_counts() != before
            self.bridge.send(br.CLOSE_ALL)
            if not got:
                _log(f"  object click: loot window {'seen' if seen_loot else 'not seen'}, "
                     f"{clicks} slot clicks, combat={self.g.player().in_combat}, "
                     f"{time.time() - t0:.1f}s")
        self.ctm.reset_fields()
        _log(f"  used object {o['entry']}: {'progress' if got else 'nothing'}")
        self.blacklist[o["guid"]] = time.time() + (300 if got else 60)
        return got

    def quest_counts(self):
        """Quest log counters + bag contents: kill / use objectives count in
        the log, collected items only in the bags."""
        return (tuple(sorted(self.g.quest_log().items())),
                tuple(sorted(self.g.bag_items().items())))

    # -- misc
    def grind_to(self, args: str, use_banked: bool = True) -> str:
        # .xp targets as rxp.py passes them: "9" (reach 9), "9.5" (halfway through 9),
        # "9-1000" (1000 xp short of 9), "9+1000" (1000 xp into 9)
        spec = args.split()[0]
        me = self.g.player()
        # Count the xp already banked in finished quests we have not handed in
        # yet: at 05:24 "grinding to 7+1600" at 1012 xp with Kobold Candles,
        # Gold Dust Exchange and The Fargodeep Mine complete in the log (1580
        # xp) - half an hour of mage grinding for xp a few turn-ins pay.
        banked = self.banked_xp(me) if use_banked else 0
        if banked:
            import types
            lvl_e, xp_e = me.level, me.xp + banked
            if xp_e >= me.next_xp:
                lvl_e, xp_e = lvl_e + 1, xp_e - me.next_xp
            me = types.SimpleNamespace(level=lvl_e, xp=xp_e, next_xp=me.next_xp,
                                       x=me.x, y=me.y, z=me.z)
        if "+" in spec:
            lvl, _, extra = spec.partition("+")
            target_lvl, extra = int(lvl), int(extra or 0)
            ok = me.level > target_lvl or (me.level == target_lvl and me.xp >= extra)
        else:
            lvl, _, rest = spec.partition("-")
            if "." in lvl:
                target_lvl, frac = int(lvl.split(".")[0]), float("0." + lvl.split(".")[1])
                ok = me.level > target_lvl or (me.level == target_lvl and me.xp >= frac * me.next_xp)
            else:
                target_lvl = int(lvl)
                left = int(rest) if rest else 0
                ok = me.level >= target_lvl or (me.level == target_lvl - 1 and
                                                me.next_xp - me.xp <= left)
        if ok:
            if banked:
                _log(f"quest: grind to {spec} covered by {banked} xp of quests to hand in")
            return "done"
        me = self.g.player()
        _state("questing", detail=f"grinding to {spec}")
        spot = getattr(self, "_grind_spot", None)
        if spot is None or spot[2] != spec:
            spot = ((me.x, me.y, me.z), time.time(), spec)
            # The step starts wherever the last one ended - often a camp the
            # quest before gave up on. "Grinding to 10" began in the trogg
            # quarry right after both of its quests were skipped as packed
            # and guarded, pulled level-10 troggs in pairs and died twice
            # (10:16, 10:37). Grind here only if the hunting-ground rules
            # (no packs, no pair camps, no stronger mobs, no recent death)
            # would pick this place too; otherwise go and find one.
            try:
                here = self.hunting_spot(me, max_dist=120.0)
            except Exception:
                here = True
            if here is None:
                _log(f"  grind to {spec}: no safe prey right here - looking for a hunting ground")
                spot = ((me.x, me.y, me.z), 0.0, spec)
            self._grind_spot = spot
        self.home, self.radius, self.stage_mobs = spot[0], 80.0, set()
        # A grind step is paid in xp per minute, so only mobs within
        # GRIND_LEVEL_GAP of us count as prey - for the kills here and for the
        # pulls on the walk to a hunting ground. At level 9 Ugibd ground
        # level-5 boars (~38 xp, some 0) for half an hour at 4,500 xp/h, and
        # every walk to the level-9 hunting ground "turned into a fight" with
        # one of them. Mobs that attack us are fought regardless.
        self.prey_min_level = me.level - self.GRIND_LEVEL_GAP
        try:
            return self._grind_step(me, spec, spot)
        finally:
            self.prey_min_level = 0

    GRIND_LEVEL_GAP = 2

    def banked_xp(self, me) -> int:
        """XP the complete-but-not-handed-in quests in our log will pay."""
        try:
            ids = [q for q, st in self.g.quest_log().items() if st and st[0] & QUEST_COMPLETE]
            if not ids:
                return 0
            rows = worlddb._query("SELECT QuestLevel, RewXP FROM quest_template WHERE entry IN (%s)"
                                  % ",".join(str(int(q)) for q in ids))
            # grey quests pay a fraction: count only those within 5 levels
            return sum(int(x) for lvl, x in rows if int(lvl) >= me.level - 5)
        except Exception:
            return 0

    def _grind_step(self, me, spec, spot) -> str:
        prey = None if spot[1] == 0.0 else self.find_prey(me)
        if prey:
            self._grind_spot = (spot[0], time.time(), spec)
            self.fight(prey, f"grinding to {spec}")
            return "working"
        if spot[1] != 0.0 and self.walk_to_prey(me):
            # a mob in sight beyond pull range: walk to it rather than wait
            self._grind_spot = (spot[0], time.time(), spec)
            return "working"
        # 4 s, not 20: standing 20 s in an empty spot before looking for a
        # hunting ground was the longest freeze of a 17:21 grind step
        if time.time() - spot[1] > 4:
            # "Grind nearby until level 16" at a spot with nothing to fight
            # (only a Loch Frenzy in the water) idled for ten minutes: go to
            # the nearest cluster of mobs our level instead
            nxt = self.hunting_spot(me, exclude=spot[0])
            if nxt is None:
                _log("  no hunting spot found around here - waiting for respawns")
                self._grind_spot = (spot[0], time.time(), spec)
                time.sleep(5.0)
                return "working"
            _log(f"  nothing to fight here - hunting ground {nxt[3]} ({nxt[4]} spawns), "
                 f"{math.dist(nxt[:2], (me.x, me.y)):.0f} yd away")
            self.buff_if_needed(me)
            # topping up is for a long walk into the unknown, not for the
            # next field over: Ddqr sat from 68% mana for a ground 57 yd away
            if math.dist(nxt[:2], (me.x, me.y)) > 150:
                self.need_full_rest = True
            self.rest_if_needed(self.g.player())
            if not self.navigate(*nxt[:3], stop_short=8.0, timeout=600, careful=True):
                me = self.g.player()
                if me.in_combat or any(u.target == me.guid and me.dist(u) < 40 for u in self.threats()):
                    _log("  the walk to the hunting ground turned into a fight - going on afterwards")
                elif self.nav and self.nav.nearest_poly(me.x, me.y, me.z)[0] is None:
                    _log("  could not get to the hunting ground - we are off the mesh, not blaming the spot")
                else:
                    self._unreachable = getattr(self, "_unreachable", []) + [(nxt[0], nxt[1], time.time() + 1800, 70)]
                    _log("  could not get to the hunting ground - trying another")
            me = self.g.player()
            self._grind_spot = ((me.x, me.y, me.z), time.time(), spec)
            return "working"
        time.sleep(1.0)
        return "working"

    def hunting_spot(self, me, exclude=None, max_dist=1500.0):
        """Densest cluster of hostile, non-elite spawns of our level within
        max_dist: (x, y, z, name, count) or None. Clusters with a stronger
        spawn, a recent death or an unreachable mark near them are skipped."""
        no_cc = None
        import dangers
        cell = 40.0
        # our own level at most: a +1 mob pays ~10% more xp for ~30% more
        # fight, and three of Ddqr's four deaths (18:03, 18:38, 18:50) were
        # level-10 camps (Dark Iron spies, Rockjaw troggs) pulled at level 9
        # that brought adds. Quest targets are chosen elsewhere.
        lo_ok, hi_ok = me.level - 4, me.level
        cont = self.continent()
        grid: dict[tuple[int, int], list] = {}
        strong: list = []
        for m, x, y, z, wander, entry, lo, hi, rank, fac, det in dangers._load():
            if m != cont or not factions.is_hostile(me.faction, fac):
                continue
            if math.dist((x, y), (me.x, me.y)) > max_dist:
                continue
            if hi >= me.level + self.STRONG_LEVELS or rank > 0:
                strong.append((x, y))
                continue
            if (lo < lo_ok or hi > hi_ok or hi < getattr(self, "prey_min_level", 0)
                    or dangers._types.get(entry) == 8):
                continue
            grid.setdefault((int(x // cell), int(y // cell)), []).append((x, y, z, entry, hi))
        gone = [(u[0], u[1], u[3] if len(u) > 3 else 25) for u in getattr(self, "_unreachable", [])
                if u[2] > time.time()]
        # four hours, not one: the Grizzled Den's wendigos killed a warrior at
        # 08:51, 08:55, 11:01 and 11:14 - each time the hour had passed and
        # "Grind to 9" picked the den again as the densest cluster around
        deaths = [sp[:2] for sp in self.death_spots(4 * 3600)]
        best = None
        for (gx, gy), pts in grid.items():
            # the cell and its neighbours: a 120 yd square, about one leash
            if no_cc is None:
                try:
                    no_cc = not any(self.has(sp) for sp in ("Frost Nova", "Polymorph"))
                except Exception:
                    no_cc = False
            allp = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    allp += grid.get((gx + dx, gy + dy), [])
            if len(allp) < 4:
                continue
            cx = sum(p[0] for p in allp) / len(allp)
            cy = sum(p[1] for p in allp) / len(allp)
            cz = min(allp, key=lambda p: math.dist(p[:2], (cx, cy)))[2]
            if exclude and math.dist((cx, cy), exclude[:2]) < 60:
                continue
            if any(math.dist((cx, cy), s) < 60 for s in strong):
                continue
            if any(math.dist((cx, cy), g[:2]) < g[2] + 40 for g in gone):
                continue
            if any(math.dist((cx, cy), d) < 120 for d in deaths):
                continue
            if getattr(self, "nav", None) and self.nav.nearest_poly(cx, cy, cz, max_dist=10.0)[0] is None:
                continue                      # Loch Frenzies and crocolisks in the water
            d = math.dist((cx, cy), (me.x, me.y))
            # weigh each spawn by the xp it gives: a level-8 warrior ground
            # through young wendigos (5-6) at 3,700 xp/h because a camp of
            # them was the densest thing around
            worth = sum(max(0.2, 1.0 - 0.2 * max(0, me.level - p[4])) for p in allp)
            # a dense cluster is dense because they stand in pairs: count
            # the ones that can be pulled alone (grind_scout.py's rule - a
            # mob our level joins from 14 yd)
            # ... for a caster too until it has a way out of a pair: a
            # level-7 mage without Frost Nova or Polymorph walked to the
            # Murloc hunting ground, two joined and it died (05:36)
            if not self.profile.ranged or no_cc:
                paired = sum(1 for p in allp if any(q is not p and q[4] >= me.level - 1
                                                    and 2.0 < math.dist(p[:2], q[:2]) < 14.0 for q in allp))
                # ... and a cluster that is MOSTLY pairs is a camp, not a
                # hunting ground: the penalty alone still let the Frostmane
                # troll camp win once the low-level fields were filtered out,
                # and walking in pulled three Seers and then a fourth (02:44)
                if paired > len(allp) * 0.5:
                    continue
                worth *= max(0.1, 1.0 - paired / len(allp))
            # mobs we have already learned come in packs
            packs = getattr(self, "pack_mobs", {})
            if sum(1 for p in allp if packs.get(p[3], 0) > time.time()) > len(allp) * 0.3:
                continue
            score = worth / (1.0 + d / 250.0)
            if best is None or score > best[0]:
                names = {}
                for p in allp:
                    names[p[3]] = names.get(p[3], 0) + 1
                top = max(names, key=names.get)
                best = (score, (cx, cy, cz, worlddb.name(top), len(allp)))
        return best[1] if best else None

    def vendor_visit(self, repair=False) -> str:
        if self.g.bag_item_count() < 4 and not repair:
            return "done"
        if time.time() - getattr(self, "last_trip_done", 0) < 240:
            return "done"                     # just came from a vendor
        self.town_trip("guide: vendor")
        return "done"

    def train_now(self) -> str:
        if self.useful_trainer(self.g.player()):
            self.next_trip = 0
            self.town_trip("guide: train")
        return "done"

    def equip(self, item: int) -> str:
        if self.g.bag_items().get(item, 0) == 0:
            return "skip"
        self.bridge.send(br.EQUIP_ITEM, item, wait=1.0)
        return "done"

    # -- NPC helpers
    def pick_npc(self, step, candidates):
        """Prefer the NPC the guide line names ([TAR]) if it is a valid one."""
        pref = [n for n in step.npcs if n in candidates] or candidates
        if not pref:
            return None
        return self.nearest_spawn_of(pref)

    def talk_to(self, npc) -> bool:
        if npc.get("kind") == "gameobject":
            me = self.g.player()
            if math.dist(npc["pos"][:2], (me.x, me.y)) > 8:
                self.navigate(*npc["pos"], stop_short=5.0, timeout=300)
            gos = [o for o in self.g.gameobjects() if o["entry"] == npc["entry"]]
            if not gos:
                return False
            o = min(gos, key=lambda o: math.dist((o["x"], o["y"]), npc["pos"][:2]))
            return self.use_object(o, quest_giver=True)
        # An NPC the last visit could not reach stays unreachable for the ban:
        # walking there again is the same failure. The accept step for Tundra
        # MacGrann re-walked his cliff two seconds after "could not reach
        # Tundra MacGrann - avoiding them for 30 min", three times over.
        if self.npc_banned(npc):
            return False
        return self.visit_npc(npc) is not None

    def npc_banned(self, npc) -> bool:
        """True (and says so) while the last failed visit's ban on this NPC
        runs. Accept and turn-in steps SKIP on it rather than fail: a failure
        right after a wedge counts as "working", and the accept step for
        Tundra MacGrann spun on the ban twice a second."""
        if not npc or npc.get("kind") == "gameobject":
            return False
        key = (npc.get("entry"), round(npc["pos"][0]), round(npc["pos"][1]))
        left = getattr(self, "npc_bad", {}).get(key, 0) - time.time()
        if left > 0:
            _log(f"  {npc.get('name', 'the NPC')} could not be reached - "
                 f"not walking there again for {left / 60:.0f} min")
            return True
        return False

