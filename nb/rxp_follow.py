"""Follow RestedXP (RXP) instead of the bot's own guide driver (nb/RXP_PLAN.md section 3). Mixed into nb.bot.Bot.

RXP decides WHAT is next (BotEye exports its current step in strip row 4, eye.Rxp); the bot only does the walking,
the targeting, the fighting and the key presses, and RXP's own automation (with BotEye's handlers stepping aside, plan
section 4) takes the quests, rewards and gossip. One decision per main-loop tick (Bot.rxp_step):

    no RXP state / RXP not loaded            grind (return False)
    flag off or no guide                     /be rxp on (BotEye loads the route by level)
    frame hidden                             /be rxp on (rxp_config re-shows it; isHidden stops all of RXP)
    on a taxi                                wait
    guide policy (rxp_policy.guide_action)   /be rxp load <guide> or grind
    step policy (rxp_policy.step_policy)     /be rxp skip / load, or grind under a LEVEL_GATE
    stall budget (rxp_policy.STALL_MIN)      /be rxp skip, then /be rxp next on the same step
    talk  -> walk to the step's waypoint, target the step NPC (PAGEUP = BotEyeRXPNpc), interact, wait for RXP
    use   -> walk there, press INSERT (BotEyeQuestItem = /use item:<block 205>)
    kill  -> roam the step's points with RXP's mob list as the wanted set (the existing roam / fight / loot)
    xp    -> grind (return False)
    goto  -> walk into the radius; RXP completes it (map.lua:1322-1331)
    deathskip / manual / unknown -> skip (manual after MANUAL_WAIT s)

WHERE to walk is the old driver's (2026-10-14 "navigation way worse than before"): RXP's waypoints are map % points
without a height. An NPC goal is the NPC's server spawn (trainers / CUSTOM_ENDERS / QuestDB / tcf_world, by name, the
one nearest RXP's waypoint; RXP's xy instead when that spawn is > NPC_AGREE yd off and no other DB position sits on the
waypoint: an NPC moved in this game version), walked like talk() / train_step / vendor_step; any other RXP point takes the height of a
DB spawn beside it, else the lowest floor we can path to, never the floor we stand on (Teo: wp height from the player
392.8 under his 399.4 platform, 18 stucks, run.out 19:46-19:49). Kill steps hunt the DB spawns of their mobs round
RXP's points; use steps walk the guide's own earlier waypoints (use_item_at); goto chains run on without stopping.

Input rules: posted keys only; the only chat lines are local /be commands, and never while an NPC window is open
(a held Shift / Ctrl changes RXP's automation, plan section 4 "Modifiers").
Switch: env NB_RXP=1 (0 = off) or the file nb/instN/RXP_MODE / nb/RXP_MODE containing "on". The old driver
(guide_plan.py / QuestDB.plan) stays the default and the rollback.
"""

from __future__ import annotations

import dataclasses
import json
import math

PERSIST = False   # bot.py __main__ turns it on: step death counts survive a restart (rx_deaths.json)
DEAD_TALK_N = 2           # bots whose talks at an NPC opened no window beside its spawn before the rest skip it
DEAD_TALK_KEEP_S = 6 * 3600.0


RX_DEATH_AREA = 250.0          # yd from the step's hunt points: a death farther off is a travel death
DEATH_SKIPS = "death_skips.json"   # shared: steps bots skipped after DEATH_SKIP deaths (same layout as dead_talk.json)


def _step_tag(key) -> str:
    return "|".join(map(str, key))


def _home_failed(value=None):
    """Per instance: was the last .home step skipped (home_failed flag file)? value=True/False sets it."""
    if not PERSIST:
        return False
    with swallow("rxp_follow.home_failed", print):
        from nb import inst as _inst
        p = _inst.path("home_failed")
        if value is None:
            return os.path.exists(p)
        if value:
            open(p, "w").close()
        elif os.path.exists(p):
            os.remove(p)
    return bool(value)


def _dead_talk_path(fname="dead_talk.json"):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)


_STORES: dict = {}       # path -> nb.store.Store of the shared / per-instance state files below


def _store(path: str, default=dict) -> Store:
    st = _STORES.get(path)
    if st is None:
        st = _STORES[path] = Store(path, default=default)
    return st


def _dead_talk_bots(name: str, fname="dead_talk.json") -> set:
    """Instances that recorded this name (an NPC with a no-window talk, or a step tag) within DEAD_TALK_KEEP_S in the
    shared nb/<fname> ({name: {inst: time}}; re-read only when the file changed)."""
    if not PERSIST:
        return set()
    try:
        import time as _t
        d = _store(_dead_talk_path(fname)).read().get(name, {})
        return {k for k, t in d.items() if _t.time() - float(t) < DEAD_TALK_KEEP_S}
    except Exception as e:
        swallowed("rxp_follow.dead_talk_bots", e, print)
        return set()


def _dead_talk_note(name: str, fname="dead_talk.json"):
    """Record this instance under `name` in the shared nb/<fname>, merged under the file's lock with what the other
    bots wrote."""
    if not PERSIST or not name:
        return
    with swallow("rxp_follow.dead_talk_note", print):
        import time as _t
        from nb import inst as _inst

        def add(d):
            d.setdefault(name, {})[str(_inst.INST)] = _t.time()
        _store(_dead_talk_path(fname)).update(add)
import os
import re
import time

from nb import inst
from nb.store import Store
from nb.util import Deadline, swallow, swallowed
from nb import trainers
from nb import economy
from nb import rxp_policy as pol
from nb.quests import CUSTOM_ENDERS
from nb.eye import (K_ACCEPT, K_GOTO, K_WANDER, K_TARGET, K_MOB, K_UNITSCAN, K_TRAIN, K_TRAINER, K_TURNIN, K_VENDOR,
                    Q_ACCEPT, Q_TURNIN, Q_COMPLETE, Q_COLLECT)

HERE = os.path.dirname(os.path.abspath(__file__))
MODE_FILE = os.path.join(HERE, "RXP_MODE")
# BotEye binds BotEyeRXPNpc to PAGEUP, DELETE and PAGEDOWN (HOME / END overrides never fired on this account,
# BotEye.lua:934-936). NB_RXP_KEYS overrides the list after a `/be rxp keys NUMPAD9` (e.g. NB_RXP_KEYS=num9).
NPC_KEYS = tuple(os.environ.get("NB_RXP_KEYS", "pageup delete pagedown").split())
# s an NPC window may stay open before the bot closes it: RXP picks quests on GOSSIP_SHOW / QUEST_DETAIL at once
# (RXPGuides.lua:1100-1219), BotEye's option pick waits 0.3 s, its reward pick 1 s, its junk sale 1.5 s and its trainer
# buyer retries for 4 s. A window still open after that has nothing left for the step (.vendor / .trainer complete on
# MERCHANT_CLOSED / TRAINER_CLOSED). ui bits: 0 gossip, 1 quest, 2 merchant, 3 trainer (BotEye fill_misc)
HOLD = {0: 3.0, 1: 4.0, 2: 4.0, 3: 6.0}   # merchant 4 s: BotEye's 2nd buy pass runs 2 s after MERCHANT_SHOW
DEATH_AREA_SHELVE_S = 600.0   # s a kill step whose every hunt point lies in a deadly area is set aside
DEATH_AREA_SHELVES = 1        # times it is set aside; the next time the step is skipped
WALK_FAILS = 3           # failed walks to one step's goal (another floor tried, shelved in between) -> skip it
# 108 of RXP's 111 talk waypoints sit on the NPC's spawn xy; a DB spawn of the step NPC within this many yd of the
# waypoint is where it stands (Mountaineer Kadrell patrols, 181 yd off: then the waypoint, and targeting by name)
NPC_SNAP = 40.0
# yd: a DB spawn this close to RXP's waypoint agrees with it (Teo: 0 yd). Farther and no other DB position on the
# waypoint: the NPC moved in this game version (RXP's guide is written for it) and RXP's xy is where it stands
NPC_AGREE = 4.0
UNAVAIL_S = 1800.0     # rxp: s a quest its giver did not offer stays unavailable (or until a level up)
POCKET_POLYS = 5000    # rxp: a mesh component this small (under-city terrain, a cellar) is no goal floor beside another
NEAR_TRY = 12.0          # yd: a walk that stuck / timed out this close to the NPC still targets + interacts from there
SHELVE = {"stuck": 300.0, "nopath": 300.0, "timeout": 120.0}   # s a step is left (grind) after a failed walk (talk():
#                          no_offer stuck 300 / timeout 120; RXP mode used to re-walk the same goal at once, 3x 160 s)
SERVICE = K_TRAIN | K_TRAINER | K_VENDOR   # walked like train_step / vendor_step: tol 3, 480 s, travel_abort
DB_Z_R = 3.0             # yd: a DB creature / object spawn this close to an RXP point gives the point its height
HUNT_AREA = 150.0        # yd round RXP's kill points in which DB spawns of the step's mobs are hunted (quests.plan area)
# The server DB is not used to find mobs (the user, 2026-10-17: "we cannot rely on DB at all"): a kill step hunts at
# RestedXP's own points, and its mobs are the ones RXP marks on the plates (rxp_ids). The quest DB / spawn table
# lookups below stay for DB_MOBS = True only.
DB_MOBS = False
TRAIN_NEAR = 700.0       # yd: a class trainer this near is worth the catch-up walk (rxp_train_due, hunter)
HUNT_MIN_PTS = 3         # fewer spawns than this round RXP's points: the ring is widened to 2 x HUNT_AREA
HUNT_NEAR = 700.0       # yd: no spawn in that area -> the spawns this close to us (quest_step's roam filter)
GOTO_WAIT = 3.0          # s at a goto waypoint without RXP completing it -> skip it (the next step's goal)
GOTO_CHAIN_S = 5.0       # s: a goto leg started this soon after the last one's arrival is walked straight (direct)
# give-up timers (nb.util.Deadline)
WINDOW_WAIT_S = 15.0     # s _rxp_wait_windows waits at most after an interact
WINDOW_FIRST_S = 1.6     # s: a window may take this long to open after the press
WINDOW_SHUT_S = 0.6      # s every NPC window must stay closed to count as done
APPROACH_WAIT_S = 15.0   # s _rxp_interact closes in on its target before the presses
ITEM_READY_WAIT_S = 10.0  # s a use step waits for the quest item to come off cooldown
USE_CAST_S, USE_CAST_HS_S = 6.0, 12.0   # s a use waits for its cast (Hearthstone casts 10 s, Earth Sapta ~1.5 s)
USE_CAST_MIN_S = 1.5     # s after the press before "not casting" ends that wait
GOTO_RUNON_S = 0.6       # s after a goto arrival for RXP's radius check to move on (the run-on)
EMOTE_AGAIN_S = 4.0      # s before a sit step's emote is sent once more
NOQ_WAIT_S = 10.0        # s of quests read as missing from the log before that counts


def norm_guide(name) -> str:
    """RXP's guide name in the strip ('01-05 Coldridge Valley') -> the parsed guide's ('1-5 Coldridge Valley')."""
    return re.sub(r"(?<!\d)0(?=\d)", "", str(name or ""))


_GUIDE_STEPS: dict | None = None


def guide_steps() -> dict:
    """{normalized guide name: [rxp.Step]} of the walkable route (guide_plan.GUIDES), parsed once. {} on any error."""
    global _GUIDE_STEPS
    if _GUIDE_STEPS is None:
        _GUIDE_STEPS = {}
        with swallow("rxp_follow.guide_steps", print):
            from nb import guide_plan          # sets rxp.GUIDE_DIR to the Forever guides
            import rxp
            from nb import route
            who = route.who()
            for g in guide_plan.GUIDES:
                with swallow(f"rxp_follow.guide_steps {g}", print):
                    _GUIDE_STEPS[norm_guide(g)] = rxp.get(g, who).steps
    return _GUIDE_STEPS


def use_route(steps, item, x, y) -> list:
    """The guide's own way to a '.use <item>' step at (x, y): the world waypoints of the steps since the last pick-up /
    hand-in, in order (guide_plan._use_item, guide_plan.py:349-358: a single pathfinding route to the Spirit Stone
    looped at the foot of the hill, 13:32). [] when no parsed step matches the item and the spot."""
    for i, st in enumerate(steps):
        if not any(t == "USE" and str(a).split(",")[0] == str(item) for t, a in st.actions):
            continue
        w = [(gb, ga) for zn, ga, gb, _r in st.gotos if str(zn).endswith("/0")]
        if not w or math.hypot(w[0][0] - x, w[0][1] - y) > 3.0:
            continue
        via = []
        for j in range(i - 1, -1, -1):
            pst = steps[j]
            if any(t in ("QA", "QT") for t, _ in pst.actions):
                break
            for zn, ga, gb, _r in reversed(pst.gotos):
                if str(zn).endswith("/0"):
                    via.append((gb, ga, None))
        via.reverse()
        return via
    return []


_NPC_BY_NAME: dict = {}
_npc_fail_t = 0.0


def db_npc_spawns(name: str) -> list:
    """tcf_world creature spawns (x, y, z) on map 0 of every entry with this name (cached per name; [] for 60 s after
    a failed read). The last source of _rxp_npc_cands: NPCs the quest DB and the trainer table do not list."""
    global _npc_fail_t
    if name in _NPC_BY_NAME:
        return _NPC_BY_NAME[name]
    if time.time() - _npc_fail_t < 60:
        return []
    try:
        import worlddb
        q = name.replace("\\", "\\\\").replace("'", "\\'")
        rows = worlddb._query_db(
            "tcf_world", "SELECT c.position_x, c.position_y, c.position_z FROM creature c JOIN creature_template t "
                         "ON t.entry = c.id WHERE c.map = 0 AND t.name = '%s'" % q)
        out = [(float(a), float(b), float(c)) for a, b, c in rows]
    except Exception as e:
        swallowed("rxp_follow.db_npc_spawns", e, print)
        _npc_fail_t = time.time()
        return []
    _NPC_BY_NAME[name] = out
    return out


_QOBJ: dict = {}
_qobj_fail_t = 0.0


_FOCUS: dict = {}


def spell_focus_near(x: float, y: float, r: float = 12.0):
    """The nearest tcf_world gameobject of type 8 (spell focus: campfires, braziers) within r yd of (x, y) on map 0, as
    (x, y, z), or None. Cached per 5 yd cell."""
    key = (round(x / 5), round(y / 5), r)
    if key in _FOCUS:
        return _FOCUS[key]
    best = None
    try:
        import worlddb
        rows = worlddb._query_db(
            "tcf_world", "SELECT g.position_x, g.position_y, g.position_z FROM gameobject g JOIN gameobject_template t "
                         "ON t.entry = g.id WHERE g.map = 0 AND t.type = 8 AND g.position_x BETWEEN %f AND %f AND "
                         "g.position_y BETWEEN %f AND %f" % (x - r, x + r, y - r, y + r))
        for gx, gy, gz in rows:
            d = math.hypot(float(gx) - x, float(gy) - y)
            if d <= r and (best is None or d < best[0]):
                best = (d, (float(gx), float(gy), float(gz)))
    except Exception as e:
        swallowed("rxp_follow.spell_focus_near", e, print)
        return None
    _FOCUS[key] = best[1] if best else None
    return _FOCUS[key]


def quest_objectives(qid: int) -> list:
    """tcf_world quest_objectives of a quest as [(Type, ObjectID, Description)] in the server's order (ORDER BY Order,
    StorageIndex = the client's GetQuestObjectives order = BotEye's counters, quests.py _load_objective_model). Cached;
    [] for 60 s after a failed read."""
    global _qobj_fail_t
    if qid in _QOBJ:
        return _QOBJ[qid]
    if time.time() - _qobj_fail_t < 60:
        return []
    try:
        import worlddb
        rows = worlddb._query_db("tcf_world", "SELECT Type, ObjectID, Description FROM quest_objectives WHERE QuestID = "
                                              "%d ORDER BY `Order`, StorageIndex" % int(qid))
        out = [(int(t), int(o), str(d or "")) for t, o, d in rows]
    except Exception as e:
        swallowed("rxp_follow.quest_objectives", e, print)
        _qobj_fail_t = time.time()
        return []
    _QOBJ[qid] = out
    return out


NOWIN_NEAR = 2.5           # yd from an NPC's spawn: no window after 3 talks from here = give up (no "closer next")
SIT_FOCUS_TOL = 1.5       # yd from a campfire (spell focus) a sit step stops at (_rxp_special_step)
SIT_KEY = os.environ.get("NB_SIT_KEY", "x")   # SITORSTAND, the client's default X binding (bindings-cache.wtf only
#                                               rebinds M); toggles, so pressed only when the character stands


def rxp_mode() -> bool:
    """RXP mode on? env NB_RXP decides when set (1/on/true or 0/off/false), else the first RXP_MODE file found
    (per instance, then nb/) containing 'on'. Default off: the old guide driver."""
    env = os.environ.get("NB_RXP", "").strip().lower()
    if env in ("1", "on", "true", "yes"):
        return True
    if env in ("0", "off", "false", "no"):
        return False
    for p in (inst.path("RXP_MODE"), MODE_FILE):
        try:
            with open(p) as f:
                return f.read().strip().lower() == "on"
        except OSError:
            continue
    return False


class RxpFollow:
    rxp_mode = False          # class defaults: Bot.__new__ test bots (test_search.py) never run rxp_init
    rxp_hunting = False       # a kill step is being hunted: roam() searches at its points even before a mob is known

    def rlog(self, msg: str):            # Bot overrides it with nb.bot.log
        print(msg, flush=True)

    # ------------------------------------------------------------ state
    def rxp_init(self):
        self.rxp_ids: set[int] = set()   # npc ids seen on plates with the rxp_mob bit (reset on a name-list change)
        self._rx: dict = {}              # per-step working state (reset when (guide, line) changes)
        self._rx_esc: dict = {}          # (guide, line) -> escapes sent (1st skip, then next)
        self._rx_deaths: dict = self._rx_deaths_load()   # (guide, line) -> deaths on it (survives a restart)
        self._rx_on = {"t": 0.0, "n": 0} # /be rxp on attempts
        self._rx_load_t: dict = {}       # guide -> last /be rxp load
        self._rx_said: dict = {}         # tag -> last one-off message (logged when it changes)
        self._rx_tctr, self._rx_tctr_t = None, 0.0
        self._rx_dead = False
        self._rx_hide_t = 0.0
        self._rx_dbidx = None            # 4 yd grid of DB spawn / object positions (x, y, z) for point heights
        self._rx_dbidx_t, self._rx_dbidx_at = 0, 0.0    # server spawns in it, build time
        self._rx_names = None            # qdb name -> {entry}
        self._rx_goto_prev = None        # (x, y, t) of the last goto leg's arrival (the next leg runs on from there)
        # qid -> {giver, where, tries}: accepts that were escaped (or quests missing at a later step of theirs); the
        # later step goes back to the giver first (_rxp_lost_accept). Saved in guide_state.json (bot.save_state)
        self.rx_lost: dict = {}
        self._rx_presses = 0             # interact calls made (_rxp_lost_accept: did a visit happen)
        eye = getattr(self, "eye", None)
        if eye is not None:
            eye.hooks.append(self._rxp_hook)

    def _rxp_hook(self, st):
        """Eye hook: learn the npc ids of plates in RXP's mob list (names map to ids only through the plates) and
        keep them wanted, so pick_target / roam's abort see them at once. Plate bits lag a list change by a frame
        (BotEye-1+2 report), so nothing is learned for 0.6 s after one."""
        r = getattr(st, "rxp", None)
        if not self.rxp_mode or r is None:
            return
        now = getattr(st, "t", None) or time.time()
        if r.tgtctr != self._rx_tctr:
            first = self._rx_tctr is None
            self._rx_tctr, self._rx_tctr_t = r.tgtctr, now
            if not first:
                self.wanted = set(self.wanted) - self.rxp_ids
                self.rxp_ids = set()
            return
        if now - self._rx_tctr_t < 0.6:
            return
        for p in st.plates:
            if p.rxp_mob and p.npc and not p.friendly and not p.player:
                self.rxp_ids.add(p.npc)
                self.wanted.add(p.npc)

    def _rx_once(self, tag, msg):
        if self._rx_said.get(tag) != msg:
            self._rx_said[tag] = msg
            self.rlog(msg)

    def _rx_where(self, r) -> str:
        return f"{r.guide} L{r.line} #{r.index}"

    # ------------------------------------------------------------ commands (local /be lines)
    def _rxp_cmd(self, sub: str):
        s = self.s()
        if getattr(s, "moving", False):
            self.mv.stop()                # never type while running (a goto run-on): the stick would steer nothing
        if any(s.ui_open(i) for i in range(4)):
            self.close_windows()          # never type while an NPC window is open (plan section 4 "Modifiers")
        self._chat_quiet("/be rxp " + sub)
        time.sleep(0.5)

    def _rxp_stale(self, r) -> str | None:
        """pol.stale_boteye against the BotEye.lua on disk (read at most once a minute)."""
        now = time.time()
        if now - getattr(self, "_rx_disk_t", -1e9) > 60:
            self._rx_disk_t = now
            try:
                from nb.eye import disk_build
                self._rx_disk = disk_build()
            except Exception as e:
                swallowed("rxp_follow.disk_build", e, self.rlog)
                self._rx_disk = None
        return pol.stale_boteye(r, getattr(self, "_rx_disk", None))

    def _rxp_escape(self, r, why: str) -> bool:
        """Stall escape: /be rxp skip first (marks the step's open elements skipped, GuideWindow.lua:239-249), then
        /be rxp next for the same step (SetStep(index + 1), GuideWindow.lua:755). One per 5 s: the strip has to show
        the last one first."""
        rx = self._rx
        now = time.time()
        if now - rx.get("esc_t", 0.0) < 5:
            time.sleep(0.5)
            return True
        key = (r.guide, r.line, r.index)     # line: for the logs (stable); index: two steps that read line 0
        nxt = pol.NEXT_GUIDE.get(r.guide)
        if nxt and not r.kinds and not r.quests and self._rx_esc.get(key, 0) >= 2:
            # the guide's last step (an empty #label, 12-14 Loch Modan L977 "Fly2WF") is done and RXP waits for the
            # player to pick a #next guide: bots 1-3 escaped it every 2 min for an hour in Ironforge
            return self._rxp_load(nxt, f"{r.guide}'s last step is done ({why})")
        if self._rx_esc.get(key, 0) >= pol.ESC_MAX and now - rx.get("esc_t", 0.0) < pol.ESC_PAUSE:
            # RXP does not move off this step (e.g. the last step with a sticky one open): no /be rxp line every
            # 5 s forever; grind and try once more every ESC_PAUSE s (review 2026-10-14)
            self._rx_once("escmax", f"rxp: {self._rx_esc[key]} escapes left {self._rx_where(r)} where it was "
                                    f"({why}); grinding, next try in {pol.ESC_PAUSE:.0f} s")
            self.wanted, self.grind_ok = set(self.rxp_ids), True
            return False
        n = self._rx_esc[key] = self._rx_esc.get(key, 0) + 1
        rx["esc_t"], rx["acc"] = now, 0.0
        sub = "skip" if n == 1 else "next"
        self.rlog(f"rxp {sub}: {self._rx_where(r)} kinds={pol.kind_names(r.kinds)} quests={r.quests} "
                  f"{r.text!r}: {why}")
        if r.kinds & pol.K_HOME:
            _home_failed(True)               # the hearthstone stays bound elsewhere: no .hs steps until a bind works
        self._rxp_cmd(sub)
        self._rxp_note_lost(r, why)
        self._rxp_note_gaveup(r)
        return True

    # -------- given-up quests (run 6, 2026-10-16: Grik'nir (218) skipped for deaths, then 40 min shelving its
    # turn-in at Grelin with 282's accept behind it; 98319 skipped, then 3 visits to a giver that cannot offer it and
    # 4 talks at Gretchen for 98319's turn-in + 98323, whose prerequisite it is): a quest whose step was skipped
    # unfinished, and every quest whose prerequisite chain runs through one, cannot be done - its steps are left at
    # once. Kept across restarts (rx_gaveup.json).
    def _rx_gaveup_set(self) -> set:
        g = getattr(self, "_rx_gaveup", None)
        if g is None:
            try:
                with open(inst.path("rx_gaveup.json"), encoding="utf-8") as f:
                    g = {int(q) for q in json.load(f)}
            except Exception:
                g = set()
            self._rx_gaveup = g
        return g

    def _rxp_note_gaveup(self, r):
        g = self._rx_gaveup_set()
        s = self.s()
        new = []
        for qid, tag, done in r.quests:
            if done or qid in g:
                continue
            h = s.quest(qid)
            if h is not None and h.complete:
                continue                     # done anyway: its hand-in still works
            # (an escaped ACCEPT is not given up: its later steps go back to the giver first, _rxp_lost_accept;
            # when those visits fail, that later step's skip gives it up)
            if tag in (Q_COMPLETE, Q_COLLECT) or (h is None and tag == Q_TURNIN):
                g.add(qid)
                new.append(qid)
        fn = getattr(self, "_rx_gaveup_file", None) or inst.path("rx_gaveup.json")
        if new:
            self.rlog(f"rxp: gave up quest(s) {new} at {self._rx_where(r)}; steps that need them are skipped")
            with swallow("rxp_follow.gaveup save", self.rlog):
                with open(fn, "w", encoding="utf-8") as f:
                    json.dump(sorted(g), f)

    def _rxp_note_unoffered(self, r, giver, since):
        """polestar's unavailable-quest rule: the giver's window opened (BotEye 'I:offers', after `since`) without this
        step's accept -> that quest is not offered now (level, prerequisite, breadcrumb); its accept and later steps
        are left (_rx_quest_dead) until our level changes or UNAVAIL_S pass."""
        off = getattr(self, "offers", None)
        if not off or off[0] < since:
            return
        s = self.s()
        un = self.__dict__.setdefault("_rx_unavail", {})
        for qid, tag, done in r.quests:
            if tag != Q_ACCEPT or done or s.quest(qid) is not None or qid in off[1] or qid in un:
                continue
            un[qid] = (getattr(s, "level", None), time.time(), giver)
            self.rlog(f"rxp: {giver!r}'s window offers {sorted(off[1]) or 'nothing'}, not {qid}: not available now "
                      f"(level {getattr(s, 'level', '?')}); its steps are left until a level up")

    def _rx_ancestor_gaveup(self, qid) -> int:
        """The given-up quest in qid's prerequisite chain (quest DB 'prev'), 0 if none."""
        g = self._rx_gaveup_set()
        qd = getattr(getattr(self, "qdb", None), "q", None) or {}
        seen = set()
        p = (qd.get(qid) or {}).get("prev", 0)
        while p and p > 0 and p not in seen:
            if p in g:
                return p
            seen.add(p)
            p = (qd.get(p) or {}).get("prev", 0)
        return 0

    def _rx_quest_dead(self, qid, tag, s) -> str | None:
        """Why (qid, tag) on this step cannot be done, or None."""
        h = s.quest(qid)
        if h is not None and h.complete:
            return None
        if h is not None:
            return f"{qid} was given up unfinished" if tag == Q_TURNIN and qid in self._rx_gaveup_set() else None
        a = self._rx_ancestor_gaveup(qid)
        if a:
            return f"{qid} needs {a}, which was given up"
        un = (getattr(self, "_rx_unavail", None) or {}).get(qid)
        if un is not None:
            lv, t0, giver = un
            if lv == getattr(s, "level", None) and time.time() - t0 < UNAVAIL_S:
                return f"{qid} is not offered by {giver} at level {lv}"
            self._rx_unavail.pop(qid, None)       # a level up (or time) may have opened it: ask again
        if tag != Q_ACCEPT and qid in self._rx_gaveup_set():
            return f"{qid} was given up"
        return None

    def _rxp_dead_chain(self, r, s) -> str | None:
        """Every open quest of a quest-only step is dead (_rx_quest_dead): the reason, else None."""
        if not r.quests or r.kinds & ~(pol.KILL | K_ACCEPT | K_TURNIN | K_TARGET | K_GOTO | K_WANDER):
            return None
        if getattr(s, "nquests", 0) > len(s.quests):
            return None                      # the strip does not list the whole log: "not in the log" is unknown
        why = []
        for qid, tag, done in r.quests:
            if done:
                continue
            w = self._rx_quest_dead(qid, tag, s)
            if w is None:
                return None
            why.append(w)
        return "; ".join(dict.fromkeys(why)) or None

    # -------- skipped accepts (inst3 20:29:21: Sten's 179 escaped after 16 no-window presses, then its complete step
    # "not in the log" and the L132 turn-in lost the whole chain): remember them, go back to the giver later
    def _rxp_note_lost(self, r, why: str):
        lost = getattr(self, "rx_lost", None)
        if lost is None:
            return
        s = self.s()
        giver = pol.target_name(r) or "?"
        for qid, tag, done in r.quests:
            if (tag != Q_ACCEPT or done or qid in lost or qid in pol.SKIP_QUESTS or giver in pol.SKIP_NPC_NAMES
                    or s.quest(qid) is not None):
                continue
            lost[qid] = {"giver": giver, "where": self._rx_where(r), "tries": 0}
            self.rlog(f"rxp: accept of {qid} at {giver!r} escaped ({why}); its next step goes back for it")
        # saved with the rest by the main loop's save_state (every STATE_SAVE_S s, bot.py)

    def _rxp_lost_entry(self, qid, s, r):
        """The lost-accept record of a quest that is not in the log: the one an escape left, or (implied) a new one
        when the quest DB knows its giver - a later step of a quest that was never taken (bot 3 has no record of
        179: that skip ran before this code). None: in the log, out of tries, or nobody to ask."""
        lost = getattr(self, "rx_lost", None)
        if lost is None or s.quest(qid) is not None:
            return None
        if self._rx_ancestor_gaveup(qid) or qid in self._rx_gaveup_set():
            return None                      # its giver cannot offer it: the chain before it was given up
        e = lost.get(qid)
        if e is None:
            q = getattr(self, "qdb", None)
            if qid in pol.SKIP_QUESTS or not (getattr(q, "starters", None) or {}).get(qid):
                return None
            e = lost[qid] = {"giver": "quest DB giver", "where": f"missing at {self._rx_where(r)}", "tries": 0}
        if e["tries"] >= pol.LOST_TRIES:
            self._rx_once(f"lostmax{qid}", f"rxp: {e['tries']} visits to the giver of {qid} did not get it; "
                                           f"leaving it to the step's own skip")
            return None
        return e

    def _rxp_lost_arm(self, qid, e):
        """/be rxp accept <qid>: BotEye takes that offer (RXP accepts only an active step's quests)."""
        now = time.time()
        if now - e.get("cmd_t", 0.0) > pol.LOST_ARM:
            e["cmd_t"] = now
            self._rxp_cmd(f"accept {qid}")

    def _rxp_lost_check(self, qid, e, why: str) -> bool:
        """After a visit: count it; the quest in the log -> the record is done."""
        e["tries"] += 1
        if self.s().quest(qid) is not None:
            self.rx_lost.pop(qid, None)
            self.rlog(f"rxp: quest {qid} accepted again ({why}, {e['where']}); doing it now")
            return True
        self.rlog(f"rxp: visit {e['tries']}/{pol.LOST_TRIES} for the skipped accept of {qid} ({why}): not in the log")
        return False

    def _rxp_lost_accept(self, r, s, qids) -> bool:
        """A step needs quests that are not in the log: walk to the quest DB giver of a lost accept and take it there
        (_rxp_talk_qdb's Q_ACCEPT path, BotEye armed with /be rxp accept). True = the tick was used."""
        for qid in qids:
            e = self._rxp_lost_entry(qid, s, r)
            if e is None:
                continue
            self._rx_once(f"lostgo{qid}", f"rxp: quest {qid} is not in the log; its accept was skipped ({e['where']}, "
                                          f"{e['giver']}): to its giver first")
            self._rxp_lost_arm(qid, e)
            n0 = self._rx_presses
            rr = dataclasses.replace(r, quests=[(qid, Q_ACCEPT, False)])
            did = self._rxp_talk_qdb(rr, self.s())
            if self._rx_presses != n0 or not did:
                self._rxp_lost_check(qid, e, "quest DB giver" if did else "giver not targeted")
            return True
        return False

    def _rxp_load(self, name: str, why: str) -> bool:
        now = time.time()
        if now - self._rx_load_t.get(name, 0.0) < 60:
            self._rx_once("load", f"rxp: load {name} sent <60 s ago and not shown yet; grinding meanwhile")
            self.wanted, self.grind_ok = set(self.rxp_ids), True
            return False
        self._rx_load_t[name] = now
        self.rlog(f"rxp load {name}: {why}")
        self._rxp_cmd("load " + name)
        time.sleep(2.0)
        return True

    def _rxp_switch_on(self, r) -> bool:
        """BotEyeDB.rxp off or no guide loaded: /be rxp on (BotEye's rxp_autoload loads the route by level and retries
        every 2 s while RXP registers its guides). Three tries, then grind and try again every 2 min."""
        t, now = self._rx_on, time.time()
        if now - t["t"] < 12:
            time.sleep(1.0)
            return True
        if t["n"] >= 3 and now - t["t"] < 120:
            self._rx_once("on", f"rxp: no guide after {t['n']} /be rxp on (flag={r.flag} loaded={r.loaded}); "
                                f"grinding, retrying in 2 min")
            self.wanted, self.grind_ok = set(), True
            return False
        t["n"] += 1
        t["t"] = now
        self.rlog(f"rxp on (try {t['n']}): flag={r.flag} loaded={r.loaded} guide={r.guide}")
        self._rxp_cmd("on")
        time.sleep(2.5)
        return True

    # ------------------------------------------------------------ the decision
    def rxp_step(self) -> bool:
        """One RXP decision (see the module docstring). True = the tick was used; False = grind (main loop roams)."""
        s = self.s()
        r = getattr(s, "rxp", None)
        self._rx_dead = False
        self.rxp_hunting = False
        if getattr(self, "_rx_running", False):
            # the last goto leg left the stick held for the next one: anything else this tick starts from a stop
            self._rx_running = False
            if r is None or not r.present or not r.wp or pol.classify(r) != "goto":
                self.mv.stop()
        if r is None or not r.present:
            self._rx_once("absent", "rxp: no RestedXP state in the strip ("
                          + ("BotEye without row 4" if r is None else "RXPGuides not loaded") + "); grinding")
            self.wanted, self.grind_ok = set(), True
            return False
        if not r.flag or not r.loaded:
            return self._rxp_switch_on(r)
        self._rx_on["n"] = 0
        if r.hidden:
            now = time.time()
            since = getattr(self, "_rx_hide_since", 0.0) or now
            self._rx_hide_since = since
            if now - self._rx_hide_t > 30:
                self._rx_hide_t = now
                self.rlog("rxp: the RXP frame is hidden (all its automation is off): /be rxp on")
                self._rxp_cmd("on")
            if now - since > pol.HIDDEN_GRIND:  # still hidden: no stand-still, grind while /be rxp on retries
                self._rx_once("hidden", f"rxp: RXP hidden for over {pol.HIDDEN_GRIND:.0f} s; grinding meanwhile")
                self.wanted, self.grind_ok = set(self.rxp_ids), True
                return False
            time.sleep(1.0)
            return True
        self._rx_hide_since = 0.0
        self._rx_said.pop("hidden", None)
        if r.taxi:
            time.sleep(1.0)                     # a .fly in progress
            return True
        ga = pol.guide_action(r, s.level)
        if ga is not None:
            if ga[0] == "load":
                return self._rxp_load(ga[1], ga[2])
            self._rx_once("grind", f"rxp: {ga[2]}; grinding")
            self.wanted, self.grind_ok = set(self.rxp_ids), True
            return False

        now = time.time()
        key = (r.guide, r.line, r.index)
        rx = self._rx
        if rx.get("key") != key:
            rx.clear()
            rx.update(key=key, t0=now, last=0.0, acc=0.0, sig=None, talks=0, talked=False, uses=0, use_t=0.0,
                      walk_fail=0, arrive=0, act=None)
            sk = f" sticky={pol.kind_names(r.skinds)}" if r.skinds else ""
            self.rlog(f"rxp step {self._rx_where(r)}: kinds={pol.kind_names(r.kinds)}{sk} quests={r.quests} "
                      f"wp={tuple(round(v, 1) if isinstance(v, float) else v for v in r.wp) if r.wp else None} "
                      f"pts={len(r.pts)} mobs={r.n_mobs} targets={r.n_targets} item={r.item or '-'} {r.text!r}")
            stale = self._rxp_stale(r)
            if stale:
                self._rx_once("stale", f"rxp: {stale}")
        # stall clock: time on this step without any progress (gaps capped: a corpse run does not count)
        gap = now - rx["last"] if rx["last"] else 0.0
        rx["last"] = now
        sig = pol.progress_sig(r, s)
        if sig != rx["sig"]:
            rx["sig"], rx["acc"] = sig, 0.0
        else:
            rx["acc"] += min(gap, pol.STALL_GAP)
        self.wanted = set(self.rxp_ids) | {p.npc for p in s.plates if p.rxp_mob and p.npc and not p.friendly}
        self.grind_ok = False

        act = pol.classify(r, rx["uses"])
        if act == "use" and (r.kinds & K_MOB or getattr(r, "n_mobs", 0)):
            self.wanted = set()                   # a taming step: its beasts are the rod's target, never killed
        if act != rx["act"]:
            rx["act"] = act
            self.rlog(f"rxp {act}: {self._rx_where(r)} {r.text!r}")
        pp = pol.step_policy(r, s.level)
        if pp is not None and pp[0] == "gate":
            # the grind is the wait for the level: it must not run the stall clock (kill budget 25 min < a level at
            # 7-8) or the grind's deaths into a skip of the very quest the gate keeps for later (review 2026-10-14)
            rx["acc"], rx["gated"] = 0.0, True
            self._rx_once("gate", f"rxp: quest {pp[1]} waits for level {pp[2]} (LEVEL_GATE); grinding")
            self.wanted, self.grind_ok = set(), True
            return False
        rx["gated"] = False
        if rx.get("shelf", 0.0) > now:
            # a failed walk shelved the step (talk()'s no_offer): grind meanwhile; that is not stall time
            rx["acc"] = rx.get("shelf_acc", 0.0)
            self._rx_once("shelf", f"rxp: {self._rx_where(r)} shelved ({rx.get('shelf_why') or 'after walk failure %d' % rx.get('walk_fail', 0)}); grinding")
            self.wanted, self.grind_ok = set(self.rxp_ids), True
            return False
        if r.done and now - rx["t0"] > 10:
            return self._rxp_escape(r, "step complete but RXP has not moved on for 10 s")
        budget = pol.STALL_MIN.get(act)
        if budget and rx["acc"] > budget * 60:
            return self._rxp_escape(r, f"rxp stall: no progress for {budget} min ({act})")
        if act == "kill" and self._rx_deaths.get(key, 0) >= pol.DEATH_SKIP:
            _dead_talk_note(_step_tag(key), DEATH_SKIPS)
            return self._rxp_escape(r, f"died {self._rx_deaths[key]} times on this step")
        if (act == "kill" and self._rx_deaths.get(key, 0) >= pol.DEATH_SKIP_SHARED
                and len(_dead_talk_bots(_step_tag(key), DEATH_SKIPS)) >= 1):
            # another bot died out of this step already (433's Bonesnapper quarry: inst3/inst4 3+ deaths each,
            # 04:51-05:44): fewer deaths of our own are enough to know. Was 2 other bots: inst2 died at the Filthy
            # Paws camp (L272, 08:04) after inst3's 3 deaths there and walked straight back to the chest. Was 1 own
            # death: one bad pull skipped the quest for every later bot (307 / 267 / 433 left open, review 2026-10-15)
            return self._rxp_escape(r, f"died here, and bots {sorted(_dead_talk_bots(_step_tag(key), DEATH_SKIPS))} "
                                       f"skipped this step for deaths")
        dead = self._rxp_dead_chain(r, s)
        if dead:
            return self._rxp_escape(r, f"quest chain given up: {dead}")
        if pp is not None:
            if pp[0] == "load":
                return self._rxp_load(pp[1], pp[2])
            if pp[0] == "skip" and not (pp[2] and act == "talk" and not rx["talked"]):
                return self._rxp_escape(r, pp[1])
        if act == "talk":
            return self._rxp_talk(r, s)
        if act in ("use", "hs"):
            return self._rxp_use(r, s, hs=act == "hs")
        if act == "kill":
            sp = None if rx.get("special_off") else self._rxp_special(r, s)
            if sp is not None:
                return self._rxp_special_step(r, s, sp)
            return self._rxp_kill(r, s)
        if act == "xp":
            self._rx_once("xp", f"rxp: grind to level {r.xp_level} ({self._rx_where(r)})")
            self.grind_ok = True
            return False
        if act == "goto":
            return self._rxp_goto(r, s)
        if act == "deathskip":
            # #completewith next softcore steps (file L692, L1322): the next step finishes them; current = skip
            return self._rxp_escape(r, "deathskip step")
        return self._rxp_manual(r, s)

    # ------------------------------------------------------------ helpers
    # -------- where: DB positions and heights for RXP's height-less points
    def _rxp_spawns(self) -> list:
        """economy.spawns() (tcf_world mobs with guid / level / name), [] when the DB cannot be read."""
        try:
            return economy.spawns()
        except Exception as e:
            swallowed("rxp_follow.spawns", e, self.rlog)
            return []

    def _rxp_db_npcs(self, name: str) -> list:
        return db_npc_spawns(name)

    def _db_index(self) -> dict:
        """4 yd grid of every DB position we know (creature spawns, quest objects, trainers / vendors), built once
        (again after 120 s while the server spawn table was unreadable)."""
        now = time.time()
        if self._rx_dbidx is not None and (self._rx_dbidx_t or now - self._rx_dbidx_at < 120):
            return self._rx_dbidx
        idx: dict = {}

        def add(p):
            try:
                x, y, z = float(p[0]), float(p[1]), float(p[2])
            except (TypeError, ValueError, IndexError):
                return
            idx.setdefault((math.floor(x / 4), math.floor(y / 4)), []).append((x, y, z))
        sp = self._rxp_spawns()
        for p in sp:
            add((p.x, p.y, p.z))
        q = getattr(self, "qdb", None)
        for pts in (getattr(q, "spawns", None) or {}).values():
            for p in pts:
                add(p)
        for attr in ("go_enders", "go_starters", "go_src"):
            for lst in (getattr(q, attr, None) or {}).values():
                for _go, p in lst:
                    add(p)
        for t in list(trainers.class_trainers()) + list(trainers.VENDORS):
            add(t["pos"])
        self._rx_dbidx, self._rx_dbidx_t, self._rx_dbidx_at = idx, len(sp), now
        return idx

    def _db_z(self, x, y):
        """Height of the nearest DB spawn / object within DB_Z_R yd of (x, y), or None."""
        idx = self._db_index()
        cx, cy = math.floor(x / 4), math.floor(y / 4)
        best = None
        for i in (cx - 1, cx, cx + 1):
            for j in (cy - 1, cy, cy + 1):
                for p in idx.get((i, j), ()):
                    d = math.hypot(p[0] - x, p[1] - y)
                    if d <= DB_Z_R and (best is None or d < best[0]):
                        best = (d, p[2])
        return best[1] if best else None

    def _comp(self, x, y, z):
        """Mesh island (navmesh.components) of the polygon at (x, y, z), or None."""
        nm = getattr(self.mv, "nm", None)
        try:
            node = nm.nearest_poly(x, y, z)[0]
            return nm.components().get(node) if node is not None else None
        except Exception as e:
            swallowed("rxp_follow.comp", e, self.rlog)
            return None

    def _rxp_point(self, x, y, s):
        """(x, y, z) to walk to for an RXP point (cached per step). Never the player's height as a hint: with it
        navmesh.ground_z prefers a floor at our own height next to the point (navmesh.py:495-506), and Teo's waypoint
        read 392.8 under his 399.4 platform (run.out 19:46-19:49, 18 stucks at (-6055.4,383.2)).
        1. the height of a DB spawn / object within DB_Z_R yd; 2. the lowest floor at the xy on our own mesh island
        (floors_at + ground_z without hint); 3. a point inside a wall / rock (no floor contains it): the nearest
        polygon centre within 8 yd (snapped); else ground_z without hint."""
        cache = self._rx.setdefault("pt", {})
        k = (round(x, 1), round(y, 1))
        if k not in cache:
            cache[k] = self._rxp_point_calc(x, y, s)
        return cache[k]

    def _rxp_point_calc(self, x, y, s):
        z = self._db_z(x, y)
        if z is not None:
            return (x, y, z)
        mv = self.mv
        nm = getattr(mv, "nm", None)
        try:
            gz = nm.ground_z(x, y) if nm is not None else None
        except Exception as e:
            swallowed("rxp_follow.point_ground_z", e, self.rlog)
            gz = None
        fl = getattr(mv, "floors_at", None)
        try:
            floors = list(fl(x, y)) if fl else []
        except Exception as e:
            swallowed("rxp_follow.point_floors", e, self.rlog)
            floors = []
        sz = getattr(s, "z", None) or gz or 0.0
        hint = None
        if not floors and fl:
            rn = getattr(nm, "reachable_near", None)
            snap = None
            # no floor and no ground_z: the point lies on ground the mesh left out (a cliff face) - the terrain's
            # own height is the hint, not 0 (Stonetalon #1418: hint 0, nothing found within 30 yd of it, the walk
            # went straight at the mountain and wedged 9 times; the mesh lay 14 yd off, 17 yd below, rxp41 15:07)
            hint = gz
            if hint is None:
                try:
                    import numpy as np
                    import terrain
                    th = float(terrain.heights(np.array([x]), np.array([y]))[0])
                    hint = th if th == th else None
                except Exception as e:
                    swallowed("rxp_follow.point_terrain", e, self.rlog)
            if hint is None:
                hint = sz
            if rn is not None:
                for rad in (8.0, 25.0):
                    try:
                        snap = rn((s.x, s.y, sz), (x, y, hint), radius=rad, tries=0, dz=30.0)
                    except Exception as e:
                        swallowed("rxp_follow.point_snap", e, self.rlog)
                        snap = None
                    if snap:
                        break
            if snap:
                self._rx_once("snap", f"rxp: point ({x:.1f},{y:.1f}) is off the mesh; walking to the nearest floor "
                                      f"({snap[0]:.1f},{snap[1]:.1f},{snap[2]:.1f})")
                return (float(snap[0]), float(snap[1]), float(snap[2]))
        if not floors and gz is None and hint is not None:
            return (x, y, hint)
        cands = sorted(set(floors + ([gz] if gz is not None else [])))
        # a dry floor wins over a swim-only one: the lowest floor beside a dock is the harbour's water (#755, Gilbert
        # Gray on the Stormwind docks: dock 3.6, water 0.2 - the walk swam to him and wedged climbing out, rxp29 08:45)
        try:
            dry = list(fl(x, y, dry=True)) if fl and floors else []
        except Exception as e:
            swallowed("rxp_follow.point_dry", e, self.rlog)
            dry = []
        if dry:
            cands = sorted(set(dry))
        if not cands:
            return (x, y, sz)
        if len(cands) > 1:
            mine = self._comp(s.x, s.y, sz)
            if mine is not None:
                for fz in cands:
                    if self._comp(x, y, fz) == mine:
                        return (x, y, fz)
            # none on our island: a floor in a small separate pocket of mesh is no goal (the terrain under the
            # Stormwind city model, 2849 polygons, beneath a shop floor the mesh seals off: the lowest floor won, the
            # walk wedged 9 times and the skip teleport put us under the city, #1600 rxp46 17:32). A sealed patch
            # (no component: goal_island walks to it) or a big island stays.
            keep = [fz for fz in cands if not self._pocket(x, y, fz)]
            if keep and len(keep) < len(cands):
                self._rx_once("pocket", f"rxp: point ({x:.1f},{y:.1f}): floors {cands} lie in a small mesh pocket "
                                        f"but {keep}")
                cands = keep
        return (x, y, cands[0])

    def _pocket(self, x, y, z) -> bool:
        """The polygon at (x, y, z) lies in a mesh component of fewer than POCKET_POLYS polygons."""
        nm = getattr(self.mv, "nm", None)
        try:
            return bool(nm.in_pocket(x, y, z, POCKET_POLYS))
        except Exception as e:
            swallowed("rxp_follow.pocket", e, self.rlog)
            return False

    def _rxp_npc_cands(self, name: str) -> list:
        """Spawn lists (x, y, z) of an NPC by name, one per source in the old driver's order: the trainer / vendor
        table (+ CUSTOM_ENDERS of those ids), the quest DB's spawns (via its names), tcf_world creature by name."""
        q = getattr(self, "qdb", None)
        if self._rx_names is None:
            rev: dict = {}
            for e, n in (getattr(q, "names", None) or {}).items():
                rev.setdefault(n, set()).add(e)
            self._rx_names = rev
        ids = set(self._rx_names.get(name, ()))
        tr = [t for t in list(trainers.class_trainers()) + list(trainers.VENDORS) if t.get("name") == name]
        tids = {t.get("npc") for t in tr} | ids
        first = [tuple(t["pos"]) for t in tr] + [tuple(p) for npc, p, _ in CUSTOM_ENDERS.values() if npc in tids]
        sp = getattr(q, "spawns", None) or {}
        return [first, [tuple(p) for e in ids for p in sp.get(e, ())], self._rxp_db_npcs(name)]

    def _db_other_near(self, x, y, own, rad=None) -> bool:
        """A DB position (another NPC / mob / object) within rad yd of (x, y) that is none of `own` (this NPC's spawns)."""
        rad = NPC_AGREE if rad is None else rad
        idx = self._db_index()
        cx, cy = math.floor(x / 4), math.floor(y / 4)
        for i in (cx - 1, cx, cx + 1):
            for j in (cy - 1, cy, cy + 1):
                for p in idx.get((i, j), ()):
                    if (math.hypot(p[0] - x, p[1] - y) <= rad
                            and not any(math.hypot(p[0] - o[0], p[1] - o[1]) < 0.5 for o in own)):
                        return True
        return False

    def _rxp_moved_z(self, x, y, dbz, s):
        """Height at RXP's xy of a moved NPC: a DB spawn / object within DB_Z_R yd; else the floor at the xy (on our own
        mesh island when known) nearest the old DB spawn's height (a moved NPC stays on its storey); else _rxp_point.
        -> (z, why)."""
        z = self._db_z(x, y)
        if z is not None:
            return z, "DB height beside it"
        mv = self.mv
        nm = getattr(mv, "nm", None)
        fl = getattr(mv, "floors_at", None)
        try:
            floors = list(fl(x, y)) if fl else []
        except Exception as e:
            swallowed("rxp_follow.moved_floors", e, self.rlog)
            floors = []
        try:
            gz = nm.ground_z(x, y) if nm is not None else None
        except Exception as e:
            swallowed("rxp_follow.moved_ground_z", e, self.rlog)
            gz = None
        cands = sorted(set(floors + ([gz] if gz is not None else [])))
        if len(cands) > 1:
            mine = self._comp(s.x, s.y, getattr(s, "z", None) or dbz)
            if mine is not None:
                ours = [f for f in cands if self._comp(x, y, f) == mine]
                cands = ours or cands
        if cands:
            return min(cands, key=lambda f: abs(f - dbz)), "floor nearest the old spawn's height"
        return self._rxp_point(x, y, s)[2], "floor guess"

    def _rxp_npc_goal(self, r, s):
        """Where the step NPC stands: (x, y, z, how). The nearest spawn of the first source with one within NPC_SNAP
        yd of RXP's waypoint: within NPC_AGREE yd it agrees with RXP -> the DB spawn ('db', Teo: same xy, the DB's
        height). Farther: RXP's waypoint belongs to another NPC of a multi-target step when a DB position of someone
        else sits on it (L286: Bellowfiz's spot for Stonegear, L295: Beldin's for Loslor; their own spawns are where
        the human talked to them, human/20261014_1959_inst4) -> still the DB spawn; else the NPC moved in this game
        version (RXP's guide is written for it) -> RXP's xy with _rxp_moved_z ('moved'). No spawn near: the waypoint
        (_rxp_point, 'wp'). Cached per (waypoint, name): a multi-target step keeps its wp when the .target changes."""
        if not r.wp:
            return None
        rx = self._rx
        wx, wy = r.wp[0], r.wp[1]
        name = pol.target_name(r)
        key = (wx, wy, name)
        if rx.get("goal_key") == key:
            return rx["goal"]
        if rx.get("goal_key") is not None:
            rx.pop("npc_tol", None)           # the last NPC's no-window tolerance is not this one's
        goal, why = None, ""
        if name:
            cands = self._rxp_npc_cands(name)
            own = [p for src in cands for p in src]
            for src in cands:
                near = [p for p in src if math.hypot(p[0] - wx, p[1] - wy) <= NPC_SNAP]
                if near:
                    p = min(near, key=lambda p: math.hypot(p[0] - wx, p[1] - wy))
                    d = math.hypot(p[0] - wx, p[1] - wy)
                    if d <= NPC_AGREE:
                        goal, why = (p[0], p[1], p[2], "db"), "DB spawn"
                    elif self._db_other_near(wx, wy, own):
                        goal, why = (p[0], p[1], p[2], "db"), "DB spawn; RXP's waypoint is another NPC's spot"
                    else:
                        z, zw = self._rxp_moved_z(wx, wy, p[2], s)
                        goal = (wx, wy, z, "moved")
                        why = (f"RXP waypoint: moved NPC, DB spawn ({p[0]:.1f},{p[1]:.1f},{p[2]:.1f}) {d:.0f} yd "
                               f"off; height: {zw}")
                    break
        if goal is None:
            goal, why = self._rxp_point(wx, wy, s) + ("wp",), "RXP waypoint"
        rx["goal_key"], rx["goal"] = key, goal
        self.rlog(f"rxp goal: {name or '?'} at ({goal[0]:.1f},{goal[1]:.1f},{goal[2]:.1f}) "
                  f"({why}, {math.hypot(goal[0] - wx, goal[1] - wy):.0f} yd from RXP's)")
        return goal

    # -------- how: walking, as the old driver does
    def _rxp_walk(self, r, x, y, tol, s, timeout=180, z=None, abort=None, near=0.0, via=(), direct=False,
                  stop=True, shelve=True, alt=None) -> str | None:
        """Walk to a step's goal. None = there, or a stuck / timed-out walk ended within `near` yd (try the NPC
        from here, as talk() does); 'abort' = combat / dead; 'fail' = counted in _rxp_walk_failed (another floor,
        shelved, or skipped). z None: _rxp_point (may snap x, y). via: the guide's own waypoints (walk_via).
        shelve=False (goto / manual: travel, nothing to come back for): no shelving, the WALK_FAILS-th skips."""
        if math.hypot(x - s.x, y - s.y) <= tol + 0.5:
            return None
        if alt is None:
            alt = z is None               # a DB height is the NPC's own floor: no retry on another one
        if z is None:
            x, y, z = self._rxp_point(x, y, s)
        z = self._rx.get("zalt", {}).get((round(x), round(y)), z)
        # walked rest through mob country: under the pull line with a pullable mob in view, stand and rest first
        # (inst1 02:18:23-59 walked off 42% mana towards the Frostmane trigger, pulled a level 9 troll at 56% and
        # died; the 3rd death on that road)
        if getattr(self, "walk_rest", False) and not s.combat and not self.ready_to_pull(s) and self.pull_in_view(s):
            self.wait_ready("a mob in view on the walk")
            return "abort"
        abort = abort or (lambda st: st.combat)
        if getattr(self, "walk_rest", False):
            abort0 = abort

            def abort(st):
                return abort0(st) or (not self.ready_to_pull(st) and self.pull_in_view(st))
        if via:
            res = self.walk_via((x, y, z), via, tol=tol, timeout=timeout, abort=abort)
        else:
            kw = {}
            if direct:
                kw["direct"] = True
            if not stop:
                kw["stop"] = False
            res = self.travel_to(x, y, z, tol=tol, timeout=timeout, abort=abort, **kw)
        if res in ("abort", "dead"):
            return "abort"
        if res in ("nopath", "stuck", "timeout"):
            st = self.s()
            d = math.hypot(x - st.x, y - st.y)
            if near and d <= near and res != "nopath":
                self._rx["near_fail"] = (res, x, y, z, shelve, alt)
                self.rlog(f"rxp: walk={res} {d:.0f} yd from ({x:.0f},{y:.0f}); trying the NPC from here")
                return None
            self._rxp_walk_failed(r, res, x, y, z, shelve, alt)
            return "fail"
        return None

    def _rxp_walk_failed(self, r, res, x, y, z, shelve=True, alt=True):
        """A failed walk: the 1st tries the goal's other floor (nearest in height) next tick, only for a guessed height
        (alt: a DB spawn's height is its floor; the floor next to it is the 392.8 under Teo's platform); then the step is shelved
        SHELVE[res] s (rxp_step grinds), and the WALK_FAILS-th skips it (talk(): no_offer, never an instant re-walk)."""
        rx = self._rx
        # counted across restarts too: bot 4's L563 giver stands on a peak with no mesh path, and the restarts every
        # 10-15 min reset the count at 2 of 3 - it shelved and re-walked the step for an hour (18:24-19:00), grinding
        prev = self._rx_walkfail_get(rx.get("key"))
        rx["walk_fail"] = max(rx.get("walk_fail", 0), prev) + 1
        n = rx["walk_fail"]
        self._rx_walkfail_put(rx.get("key"), n)
        self.rlog(f"rxp: walk={res} to ({x:.0f},{y:.0f},{z if z is None else round(z, 1)}) for "
                  f"{self._rx_where(r)} ({n}/{WALK_FAILS})")
        if n >= WALK_FAILS:
            self._rxp_escape(r, f"goal unreachable ({res})")
            return
        if alt and not rx.get("alt_done"):
            rx["alt_done"] = True
            fl = getattr(self.mv, "floors_at", None)
            try:
                floors = list(fl(x, y)) if fl else []
            except Exception as e:
                swallowed("rxp_follow.walk_failed_floors", e, self.rlog)
                floors = []
            alts =[f for f in floors if z is None or abs(f - z) > 2.0]
            if alts:
                alt = min(alts, key=lambda f: abs(f - (z or 0.0)))
                rx.setdefault("zalt", {})[(round(x), round(y))] = alt
                self.rlog(f"rxp: next walk to ({x:.0f},{y:.0f}) on the floor at z={alt:.1f}")
                return
        if not shelve:
            return
        rx["shelf"], rx["shelf_acc"] = time.time() + SHELVE.get(res, 120.0), rx.get("acc", 0.0)
        self.rlog(f"rxp: shelving {self._rx_where(r)} for {SHELVE.get(res, 120.0):.0f} s after walk={res}")

    def _rxp_near_failed(self, r) -> bool:
        """The walk ended short (NEAR_TRY) and the NPC could not be targeted from there: count that walk now."""
        nf = self._rx.pop("near_fail", None)
        if nf is None:
            return False
        self._rxp_walk_failed(r, *nf)
        return True

    def _rxp_npc_walk(self, r, s) -> str | None:
        """Walk to the step NPC's DB spawn: talk() (tol 5, 180 s, combat aborts), or for trainer / vendor steps
        train_step / vendor_step (tol 3, 480 s, travel_abort: gray mobs at our heels are outrun)."""
        goal = self._rxp_npc_goal(r, s)
        if goal is None:
            return None
        svc = bool(r.kinds & SERVICE)
        tol = self._rx.get("npc_tol") or (3.0 if svc else 5.0)     # NOWIN_TOL after a talk that opened no window
        return self._rxp_walk(r, goal[0], goal[1], tol, s, timeout=480 if svc else 180, z=goal[2],
                              abort=self.travel_abort if svc else None, near=NEAR_TRY, alt=goal[3] != "db")

    def _rxp_progress(self, r0, r1) -> bool:
        return r1 is not None and (r1.stepctr != r0.stepctr or r1.line != r0.line or r1.kinds != r0.kinds
                                   or [q[2] for q in r1.quests] != [q[2] for q in r0.quests])

    def _rxp_target_npc(self, r) -> bool:
        """Target the step's .target NPC: BotEyeRXPNpc (/targetexact <first .target name>) on PAGEUP, the DELETE /
        PAGEDOWN fallbacks, then the F-key of a plate with the rxp_npc bit. The key that worked is tried first next."""
        s = self.s()
        if s.has_target and s.t_rxp_npc and not s.t_dead:
            return True
        if not r.n_targets:
            return False
        good = getattr(self, "_rx_npc_key", None)
        for k in ((good,) if good else ()) + tuple(k for k in NPC_KEYS if k != good):
            self.keys.press(k)
            time.sleep(0.35)
            s = self.s()
            if not s.t_rxp_npc and s.has_target:
                time.sleep(0.3)               # target bits follow the 0.25 s name lists
                s = self.s()
            if s.has_target and s.t_rxp_npc:
                if k != good:
                    self.rlog(f"rxp: step NPC key {k} works (npc {s.t_npc})")
                self._rx_npc_key = k
                return True
        p = next((p for p in s.plates if p.rxp_npc and not p.dead and p.token), None)
        if p is not None:
            self.keys.press(f"f{p.token}" if p.token <= 10 else f"num{p.token - 11}")
            time.sleep(0.35)
            if self.s().t_rxp_npc:
                self._rx_once("npckey", f"rxp: step NPC keys {NPC_KEYS} did not target it; its plate key did "
                                        f"(try /be rxp keys NUMPAD9 with NB_RXP_KEYS=num9)")
                return True
        if s.has_target and not s.t_rxp_npc:
            self.keys.press("f11")
        return False

    def _rxp_wait_windows(self):
        """After an interact: wait until every NPC window is closed for 0.6 s (talk(), bot.py), closing any that
        outstays HOLD. -> (a window opened, the trainer window opened)."""
        wait = Deadline(WINDOW_WAIT_S, clock=time)
        shut, opened, trainer, since = None, False, False, {}
        while wait.running():
            time.sleep(0.2)
            s = self.s()
            now = time.time()
            if s.combat:
                break
            bits = [i for i in range(4) if s.ui_open(i)]
            opened = opened or bool(bits)
            trainer = trainer or 3 in bits
            for i in list(since):
                if i not in bits:
                    del since[i]
            for i in bits:
                since.setdefault(i, now)
            if any(now - since[i] > HOLD[i] for i in bits):
                self.rlog(f"rxp: window(s) {bits} left open (ui={s.ui}); closing")
                self.close_windows()
                since.clear()
                continue
            if bits or s.moving or now - wait.t0 < WINDOW_FIRST_S:
                shut = None
            elif shut is None:
                shut = now
            elif now - shut >= WINDOW_SHUT_S:
                break
        return opened, trainer

    def _rxp_step_closer(self, n, pos, far=False):
        """A press opened no window: interact range is shorter than t_within10 (loot_one, bot.py:1169-1177 does the
        same for corpses). Drive at the target's plate 0.4 s x the try, unless already under 2.5 yd of its spawn;
        from the 3rd press also re-target through its plate key (a secure click, not the name macro).
        far (the last press said err 12, too far): the targeted unit is not where pos says (a multi-target step's
        next NPC, a moved one: run.out 21:42-21:43 Loslor 20 yd off, 1.3 yd from the goal): re-target by its plate key
        at once and drive at the live unit's plate whatever pos says (approach: no plate -> interact-walk)."""
        s = self.s()
        if far:
            self._rx_once("far", f"rxp: err 12 (too far) at "
                                 f"{'?' if pos is None else round(math.hypot(pos[0] - s.x, pos[1] - s.y), 1)} yd "
                                 f"from the goal: approaching the targeted unit (npc {s.t_npc}) by its plate")
            self.approach(min(3.0, 0.8 * n + 0.7), force=True)
        elif pos is None or math.hypot(pos[0] - s.x, pos[1] - s.y) > 2.5:
            self.approach(0.4 * n, force=True)
        if far or n >= 3:
            s = self.s()
            p = next((p for p in s.plates if p.token and not p.dead and
                      ((s.t_npc and p.npc == s.t_npc) or getattr(p, "rxp_npc", False))), None)
            if p is not None:
                self.keys.press(f"f{p.token}" if p.token <= 10 else f"num{p.token - 11}")
                time.sleep(0.35)

    def _rxp_interact(self, r, retarget, is_target, who: str, pos=None) -> str:
        """Close in on the target, press interact (M), let RXP / BotEye work the windows. pos: the NPC's spawn (x, y)
        for the log's per-press distance. A press that opens nothing steps closer first (_rxp_step_closer).
        -> 'advanced' (the step moved on) | 'progress' (an element finished) | 'nothing' | 'nowindow' | 'combat'."""
        r0 = r
        self._rx_presses = getattr(self, "_rx_presses", 0) + 1
        closing = Deadline(APPROACH_WAIT_S, clock=time)
        while closing.running():
            s = self.s()
            if not s.has_target or s.combat or s.t_within10:
                break
            self.approach(1.5, force=True)
        result, presses, train, far = "nowindow", 0, False, False
        trail = []                            # per press: yd to the spawn / err code (inst3: no record of either)
        while presses < 4:
            presses += 1
            s = self.s()
            if s.combat:
                result = "combat"
                break
            if presses > 1 and (result == "nowindow" or far):
                self._rxp_step_closer(presses, pos, far=far)
                s = self.s()
            if not is_target(s) and not retarget():
                break
            e0 = s.errn
            self.keys.press("m")              # keys.before['m'] runs fake_activate first
            opened, tr = self._rxp_wait_windows()
            st = self.s()
            d = f"{math.hypot(pos[0] - st.x, pos[1] - st.y):.1f}" if pos else "?"
            trail.append(f"{d}yd/err{st.err if st.errn != e0 else 0}")
            # too far: the live unit is elsewhere (_rxp_step_closer). "Target is too far away." has no BotEye code
            # (err 99): inst2 03:45-03:46 at Earthseer Farsen and Father Gavin, 1 yd from the DB spawn, skipped both
            far = st.errn != e0 and (st.err == 12 or (st.err == 99 and "too far" in (getattr(st, "msg", "") or "").lower()))
            train = train or tr
            r1 = self.s().rxp
            if self._rxp_progress(r0, r1):
                result = "advanced" if (r1.stepctr != r0.stepctr or r1.line != r0.line) else "progress"
                if r0.kinds & pol.K_HOME and not r1.kinds & pol.K_HOME:
                    _home_failed(False)       # the .home element finished: the stone is bound here now
                if result == "advanced" or not r1.kinds & pol.TALK:
                    break
                r0 = r1                       # more for the same NPC (a 2nd quest): press again
                continue
            if opened:
                result = "nothing"
                break
            # no window at all: the press never reached the client (talk(): 26% of talks), not a refusal
        self.close_windows()
        self.keys.press("f11")
        self._rx["far"] = far and result == "nowindow"
        s = self.s()
        self.rlog(f"rxp talk {who}: {result} after {presses} press(es) ({self._rx_where(r)}, quests {r.quests} -> "
                  f"{s.rxp.quests if s.rxp else None}) presses [{' '.join(trail)}] note {getattr(s, 'msg', '')!r}")
        if train and r.kinds & (K_TRAIN | K_TRAINER):
            self._rxp_train_record()
        return result

    # ------------------------------------------------------------ talk
    def _rxp_talk(self, r, s) -> bool:
        rx = self._rx
        # a .fly step is travel: far from its flight master (bot 3 17:25 on the Westfall border, its .fly Westfall
        # master inside Stormwind, whose mesh does not join Elwynn's: 40 s "none" plans, then a death on the way)
        # the next steps' own goals travel (Bot.fly_leg) - skip it
        if r.kinds & pol.K_FLY and not r.kinds & (K_ACCEPT | K_TURNIN) and r.wp:
            d = math.hypot(r.wp[0] - s.x, r.wp[1] - s.y)
            if d > pol.FLY_STEP_FAR:
                return self._rxp_escape(r, f"fly step {d:.0f} yd from its flight master; the next steps travel "
                                           f"on their own")
        # an NPC that opened no window beside its spawn for DEAD_TALK_N bots (server data: Father Gavin 1253 has
        # npcflag 0; Earthseer Farsen is a custom entry): the next bot skips it without the 3-4 min of talks
        # Not when a hand-in of this step is in our log: we have something for the NPC, or an unfinished objective
        # the incomplete-turn-in hunt below does first (307 Filthy Paws: Mountaineer Stormpike marked by bots 1 and 3,
        # inst2 / inst4 skipped the hand-in and the quest never finished; quest review 2026-10-15)
        tn = pol.target_name(r)
        ours = any(tag == Q_TURNIN and not done and s.quest(qid) is not None for qid, tag, done in r.quests)
        if tn and not ours and len(_dead_talk_bots(tn)) >= DEAD_TALK_N:
            return self._rxp_escape(r, f"{tn!r} opened no window for bots {sorted(_dead_talk_bots(tn))} beside its "
                                       f"spawn; not trying again")
        # an accept-only step whose quests' prerequisites are still in our log (not turned in) cannot give them: 291
        # The Reports needs 287 Frostmane Hold handed in; every bot walked to Senir and talked 4 times (4 min each)
        if r.quests and all(tag == Q_ACCEPT for _, tag, _d in r.quests):
            qd = getattr(getattr(self, "qdb", None), "q", None) or {}
            blocked = [(qid, (qd.get(qid) or {}).get("prev", 0)) for qid, _t, done in r.quests if not done]
            # (the log entry may read complete: the client marks 287 done at 5/5 whelps, the server wants the trigger)
            if blocked and all(p > 0 and s.quest(p) is not None for _, p in blocked):
                return self._rxp_escape(r, "accept of " + ", ".join(f"{q} (needs {p} turned in; it is still in "
                                                                    f"our log)" for q, p in blocked))
        # a turn-in of a quest that is not in the log can never complete; one that is in the log but not complete
        # needs its objectives first (a missed hunt): hunt them with the quest DB's data (coverage, no gate)
        turn = [q[0] for q in r.quests if q[1] == Q_TURNIN and not q[2]]
        other = r.kinds & pol.TALK & ~K_TURNIN or (r.kinds & K_TRAIN and not r.train_known)
        if turn and other and not rx.get("special_off") and getattr(s, "nquests", 0) <= len(s.quests):
            # an accept|turnin step whose hand-in is unfinished by a sit / visit (inst3 23:43 Eric #6: 96608's campfire
            # sit undone, 4 talks "changed nothing", step skipped): the special objective first, then the talk
            have = [s.quest(q) for q in turn]
            if all(h is not None and not h.complete for h in have):
                sp = self._rxp_special_for(r, s, turn)
                if sp is not None:
                    self._rx_once("turnin_sp", f"rxp: turn-in of {turn} but the quest is not complete: its "
                                               f"objective is a {sp[0]} ({sp[1] or ''}) - doing that first")
                    return self._rxp_special_step(r, s, sp)
                # a kill objective short (inst3 01:52 Rejold #52: 319 at 2/6 Ice Claw Bears, 4 talks, skipped with
                # 320's accept behind it): hunt it as the pure turn-in branch does
                self._rx_once("turnin", f"rxp: turn-in of {turn} but the quest is not complete: hunting its "
                                        f"objectives first")
                return self._rxp_kill(r, s, extra=turn)
        # the full-log check only guards "not in the log" (the strip lists up to 20 quests); quests it does show as
        # incomplete are incomplete either way (inst1 01:18-01:19: 287 / 98323 shown incomplete, 18+ in the log, so
        # 4 talks at Senir opened the progress window and the hand-in of an hour's Frostmane hunt was skipped)
        full_log = getattr(s, "nquests", 0) <= len(s.quests)
        if self._rxp_explore_walk(r, s):
            return True
        if rx.get("explore_dead") and turn and not other and not any(
                (q := s.quest(qid)) is not None and q.complete for qid in turn):
            # no walk back to the ender for 4 talks that cannot work (the server still wants the trigger). With
            # accepts or a finished hand-in at the same NPC the talk still goes (Hunt 10:53: 62's Fargodeep trigger
            # death skipped Dughan's whole step - 40 complete, 76 / 35 never accepted)
            return self._rxp_escape(r, f"turn-in of {turn}: died on the way to its explore trigger")
        # a step with accepts / other hand-ins at the same NPC talks first: those are safe now, the trigger walk may
        # not be (Hunt 10:52: died in Fargodeep on the way to 62's trigger with 40 complete, 76 / 35 unaccepted)
        ex = self._rxp_explore_due(turn, s) if turn and (not other or rx.get("talks", 0) >= 1) else None
        if ex is not None:
            # kills done, the explore trigger not entered: there first, not via 4 talks at the ender (inst1 walked
            # Kharanos -> Senir -> the Hold, 1250 yd, after each death on that road, 01:40-02:30)
            t, (x, y, z), rad = ex
            rx["explore_spot"] = (x, y, z, rad)
            self.rlog(f"rxp: turn-in of {turn}: explore trigger {t} at ({x:.0f},{y:.0f}) not entered yet: there first")
            return True
        if turn and not other:
            have = [s.quest(q) for q in turn]
            if full_log and all(h is None for h in have):
                rx.setdefault("noq_t", time.time())
                if time.time() - rx["noq_t"] > NOQ_WAIT_S:
                    if self._rxp_lost_accept(r, s, turn):
                        return True               # its accept was skipped: to the giver first, then hunt, then here
                    return self._rxp_escape(r, f"turn-in of {turn}: not in the quest log")
                time.sleep(1.0)
                return True
            if all(h is not None and not h.complete for h in have):
                # an objective done by being somewhere (sit at the campfire, 96608 after its own step was skipped:
                # inst3 23:25-23:31 hunted it at Eric's instead) runs the special step, not a hunt
                sp = self._rxp_special_for(r, s, turn)
                if sp is not None and not self._rx.get("special_off"):
                    self._rx_once("turnin_sp", f"rxp: turn-in of {turn} but the quest is not complete: its "
                                               f"objective is a {sp[0]} ({sp[1] or ''}) - doing that first")
                    return self._rxp_special_step(r, s, sp)
                self._rx_once("turnin", f"rxp: turn-in of {turn} but the quest is not complete: hunting its "
                                        f"objectives first")
                return self._rxp_kill(r, s, extra=turn)
        name = pol.target_name(r) or "?"
        if self._rxp_npc_walk(r, s) is not None:
            return True
        if not self._rxp_target_npc(r):
            if self._rxp_talk_qdb(r, self.s()):
                rx["talked"] = True
                rx.pop("near_fail", None)
                return True
            if self._rxp_near_failed(r):
                return True                   # stopped short and nothing to target there: a failed walk after all
            rx["talks"] += 1
            st = self.s()
            self.rlog(f"rxp: could not target {name!r} at ({st.x:.0f},{st.y:.0f}) "
                      f"({rx['talks']}/{pol.TALK_TRIES}, targets={r.n_targets})")
            if rx["talks"] >= pol.TALK_TRIES:
                return self._rxp_escape(r, f"step NPC {name!r} not found")
            time.sleep(1.0)
            return True
        rx.pop("near_fail", None)
        # a turn-in here of a quest that is not in the log while the step also talks for other things (L132 at Sten:
        # accepts + turn-in 179): BotEye takes its offer on this visit too
        log_read = getattr(s, "nquests", 0) <= len(s.quests)
        lost = ([(q, e) for q in turn for e in [self._rxp_lost_entry(q, s, r)] if e is not None]
                if other and log_read else [])
        for q, e in lost:
            self._rxp_lost_arm(q, e)
        goal = rx.get("goal")
        t_talk = time.time()
        res = self._rxp_interact(r, lambda: self._rxp_target_npc(r), lambda st: st.t_rxp_npc, name,
                                 pos=goal[:2] if goal else None)
        if res == "nowindow" and rx.pop("far", False) and not rx.get("far_try") and self._rxp_target_npc(r):
            # too far (err 12) from the live unit: it is not at the goal. Close in on the targeted unit itself (its
            # plate, else interact-walk) and talk once more before the no-window counts
            rx["far_try"] = True
            self.rlog(f"rxp: {name!r} answered err 12 (too far) at the goal: walking to the targeted unit, once more")
            self.approach(3.0, force=True)
            res = self._rxp_interact(r, lambda: self._rxp_target_npc(r), lambda st: st.t_rxp_npc, name,
                                     pos=goal[:2] if goal else None)
        rx["talked"] = True
        self._rxp_note_unoffered(r, name, t_talk)
        for q, e in lost:
            self._rxp_lost_check(q, e, f"at {name!r}")
        if res in ("advanced", "progress", "combat"):
            rx["talks"] = rx["nowin"] = 0
            rx.pop("far_try", None)
            return True
        st0 = self.s()
        if res == "nowindow" and ("inventory is full" in (st0.msg or "").lower() or st0.bags_free == 0)                 and rx.get("bag_trips", 0) < 2:
            # the hand-in / accept needs a free slot: "Inventory is full." (Northshire 2026-10-17: Eagan Peltskinner's
            # turn-in of 33 was skipped after 3 such talks). Sell first, then talk again - not a no-window try
            rx["bag_trips"] = rx.get("bag_trips", 0) + 1
            self.rlog(f"rxp: talk at {name!r} failed on full bags ({st0.bags_free} free): selling first, then again")
            self.vendor_nogray_free = 99                 # a full-bag trip even if the last sale found little to sell
            self.retry.set("vendor", 0)
            self.vendor_step()
            rx["nowin"] = 0
            return True
        if res == "nowindow":
            # no window at all is not a refusal (inst3 16 presses at Sten 20:28-20:29 -> 179 skipped): its own count,
            # re-walked to NOWIN_TOL yd of the spawn, more tries on a quest step before the skip
            rx["nowin"] = rx.get("nowin", 0) + 1
            rx["npc_tol"] = pol.NOWIN_TOL
            lim = pol.NOWIN_TRIES_QUEST if r.kinds & (K_ACCEPT | K_TURNIN) else pol.NOWIN_TRIES
            st = self.s()
            d = f"{math.hypot(goal[0] - st.x, goal[1] - st.y):.1f} yd" if goal else "? yd"
            if rx["nowin"] >= 2:
                self._rx_once("nowin", f"rxp: interact dead on this client? {rx['nowin']} talks at {name!r} opened "
                                       f"no window ({d} from its spawn, err={st.err})")
            if rx["nowin"] >= lim:
                _dead_talk_note(name)
                return self._rxp_escape(r, f"{rx['nowin']} talks to {name!r} opened no window, {d} from its spawn "
                                           f"(interact dead on this client)")
            # an NPC with nothing for us opens no window at all: the step's hand-ins are not in the log (their accept
            # was lost) -> its offers behind them are not there either (inst2 23:27-23:28: 8 talks, 98 s at Senir
            # Whitebeard with 420 never accepted; Tannok and Maxan opened at once right after)
            missing = turn and all(st.quest(q) is None for q in turn) and getattr(st, "nquests", 0) <= len(st.quests)
            if missing and rx["nowin"] >= 2:
                return self._rxp_escape(r, f"{name!r} opened no window twice and the step's hand-ins {turn} are not "
                                           f"in the log: nothing for us there")
            # already beside the spawn: "closer next" cannot help (inst4 00:28:40-00:31:27: 8 x 4 presses at 1.4 yd from
            # Mountaineer Gretchen, 171 s, while every other bot's interact worked)
            if goal and rx["nowin"] >= 3 and math.hypot(goal[0] - st.x, goal[1] - st.y) <= NOWIN_NEAR:
                _dead_talk_note(name)
                return self._rxp_escape(r, f"{rx['nowin']} talks to {name!r} opened no window from {d} of its spawn")
            self.rlog(f"rxp: talk at {name!r} opened no window ({rx['nowin']}/{lim}, {d}); closer next")
            return True
        rx["talks"] += 1
        # a vendor .collect (Adlin's water, file L119-129): BotEye buys it in the merchant window (buy_collect).
        # Skip only when it cannot be bought; while it is affordable, visit again (up to BUY_TALKS)
        verdict = pol.buy_verdict(self.s().rxp or r) if res == "nothing" else None
        if verdict == "retry":
            b = self.s().rxp or r
            if rx["talks"] < pol.BUY_TALKS:
                self.rlog(f"rxp buy: {name!r} sells item {b.buy_item} ({b.buy_need} more at {b.buy_price}c, money "
                          f"{getattr(self.s(), 'money', '?')}c) and it is affordable: again ({rx['talks']}/{pol.BUY_TALKS})")
                return True
            return self._rxp_escape(r, f"{rx['talks']} visits to {name!r}: item {b.buy_item} affordable but not bought")
        if verdict:
            return self._rxp_escape(r, f"buy at {name!r}: {verdict}")
        if rx["talks"] >= pol.TALK_TRIES and turn and not other and not rx.get("explored"):
            # an explore objective (areatrigger_involvedrelation) is no count on the strip: 287 Frostmane Hold showed
            # 5/5 whelps, the server kept it incomplete (trigger 97 never entered) and inst1 skipped the hand-in 3 x
            # (01:18-01:35). Walk into its trigger, then talk again
            q = getattr(self, "qdb", None)
            trig = [t for qid in turn for t in (getattr(q, "explore", {}) or {}).get(qid, ())]
            if trig:
                rx["explored"], rx["talks"] = True, 0
                t, (x, y, z), rad = min(trig, key=lambda t_: math.hypot(t_[1][0] - s.x, t_[1][1] - s.y))
                rx["explore_spot"] = (x, y, z, rad)
                self.rlog(f"rxp: turn-in of {turn} at {name!r} changed nothing: entering explore trigger {t} at "
                          f"({x:.0f},{y:.0f}) first")
                return True
        if (rx["talks"] >= pol.TALK_TRIES and turn and not other and not full_log and not rx.get("turn_hunted")
                and all(s.quest(q) is None for q in turn)):
            # a turn-in the strip cannot show (20+ quests in the log) that 4 talks did not take is most likely
            # incomplete (inst1 01:18 / 01:22 at Senir: the progress window opened for 287 / 98323): hunt its
            # objectives once (the quest DB's data), then talk again before any skip
            rx["turn_hunted"], rx["talks"] = True, 0
            self.rlog(f"rxp: turn-in of {turn} at {name!r} changed nothing and the strip cannot show those quests "
                      f"(log of {getattr(s, 'nquests', '?')}): hunting their objectives first")
            return self._rxp_kill(r, self.s(), extra=turn)
        if rx["talks"] >= pol.TALK_TRIES:
            # a vendor .collect with no buy state: say why (a BotEye without buy_collect never buys it)
            stale = self._rxp_stale(self.s().rxp or r) if pol.vendor_collect(r) else None
            return self._rxp_escape(r, f"{rx['talks']} talks to {name!r} changed nothing"
                                       + (f" ({stale})" if stale else ""))
        return True

    def _rxp_talk_qdb(self, r, s) -> bool:
        """No .target in the step (or it cannot be targeted): the quest DB's giver / ender for its accept / turn-in
        elements, an NPC by its plate or a game object (use_object)."""
        q = getattr(self, "qdb", None)
        if q is None:
            return False
        name = pol.target_name(r)
        for qid, tag, done in r.quests:
            if done or tag not in (Q_ACCEPT, Q_TURNIN):
                continue
            npcs = (q.starters if tag == Q_ACCEPT else q.enders).get(qid, ())
            for npc in npcs:
                pos = None
                with swallow("rxp_follow.npc_pos", self.rlog):
                    pos = q.npc_pos(npc, (s.x, s.y))
                guess = False
                if r.wp and name and (getattr(q, "names", None) or {}).get(npc) == name:
                    # RXP's waypoint is this NPC's: the step goal's rule (its DB spawn when it agrees, RXP's xy
                    # when it moved), not the quest DB spawn nearest us
                    g = self._rxp_npc_goal(r, s)
                    if g and g[3] in ("db", "moved"):
                        pos, guess = g[:3], g[3] == "moved"
                if not any(p.npc == npc for p in s.plates) and pos and math.hypot(pos[0] - s.x, pos[1] - s.y) > 8:
                    # the quest DB spawn with its own height, walked as talk() walks it
                    if self._rxp_walk(r, pos[0], pos[1], 5.0, s, timeout=180, z=pos[2], near=NEAR_TRY,
                                      alt=guess) is not None:
                        return True
                if self.target_npc(npc, tries=3):
                    self._rxp_interact(r, lambda: self.target_npc(npc, tries=3), lambda st: st.t_npc == npc,
                                       f"npc {npc} (quest DB)", pos=pos[:2] if pos else None)
                    return True
            gos = (q.go_starters if tag == Q_ACCEPT else q.go_enders).get(qid, ())
            for go, pos in gos:
                if go not in q.skip_go:
                    self.use_object(go, pos, f"{'accept' if tag == Q_ACCEPT else 'turn in'} {qid} at an object (RXP)")
                    return True
        return False

    # ------------------------------------------------------------ use
    def _rxp_use(self, r, s, hs=False) -> bool:
        """Press INSERT (BotEyeQuestItem = /use item:<block 205 item>, fed out of combat). Also the .hs step: 6948 is
        in the step's activeItems (functions.lua:2577-2586)."""
        rx = self._rx
        if hs and not r.item_ready:
            return self._rxp_escape(r, "hearthstone on cooldown")
        if hs and _home_failed():
            # the guide's last .home step was skipped: the stone still points at an older inn (all 4 bots bound in
            # Coldridge on 2026-10-15 06:25, every .home skipped), a hearth would cost the whole walk back
            return self._rxp_escape(r, "the hearthstone is bound elsewhere (a .home step was skipped): walking instead")
        if not r.item:
            return self._rxp_manual(r, s)
        if time.time() - rx["use_t"] < 8:
            time.sleep(0.5)                   # the last use is still being judged by RXP
            return True
        if r.wp and not hs:
            rad = r.wp[2]
            # the guide's own earlier waypoints as straight trail legs (use_item_at / walk_via), not one pathfinding
            # route: that one looped at the foot of the Spirit Stone hill (13:32) and slid into its pocket (14:14)
            w = self._rxp_walk(r, r.wp[0], r.wp[1], max(1.0, 0.6 * rad) if rad else 4.0, s, timeout=240,
                               via=self._rxp_use_route(r))
            if w is not None:
                return True
        if (r.kinds & K_MOB or getattr(r, "n_mobs", 0)) and not hs:
            return self._rxp_use_on_mob(r, s)  # an item used on one of the step's mobs (Taming the Beast's rods)
        if r.kinds & K_TARGET and not hs:
            self._rxp_target_npc(r)           # an item used on a unit (taming rods, file L1655)
        self.mv.stop()
        time.sleep(0.4)
        ready = Deadline(ITEM_READY_WAIT_S, clock=time)
        while ready.running():
            st = self.s()
            if st.combat or (st.rxp and st.rxp.item_ready):
                break
            time.sleep(0.5)
        s0 = self.s()
        self.keys.press("insert")
        cast = Deadline(USE_CAST_HS_S if hs else USE_CAST_S, clock=time)
        while cast.running():
            time.sleep(0.3)
            st = self.s()
            if st.combat:
                break
            if cast.elapsed() > USE_CAST_MIN_S and not st.casting_now:
                break
        st = self.s()
        rx["uses"] += 1
        rx["use_t"] = time.time()
        self.rlog(f"rxp use item {r.item} ({rx['uses']}/{pol.USE_MAX}): msg {st.msg!r} "
                  f"err={st.err if st.errn != s0.errn else 0} step {'moved' if self._rxp_progress(r, st.rxp) else 'same'}")
        return True

    USE_MOB_RANGE_WAIT = 12.0   # s walking at a step mob before the use is tried from where we are
    USE_MOB_CAST_S = 25.0       # s a use on a mob may channel (a taming rod)
    USE_MOB_TRIES = 10          # targeting / walking rounds on one step before the press counts as a use anyway

    def _rxp_use_on_mob(self, r, s) -> bool:
        """'.use <item> >> Use the Taming Rod on a Rockhide Boar' + '.mob Rockhide Boar' (06-11, Taming the Beast):
        the step's mob is the item's target, not a kill. Find one (BotEyeFind: RXP's targeting macro for the step's
        mobs; else walk the step's points), walk into range, stand still and use the item on it, then wait out the
        channel without fighting back. A pet already out is dismissed first (one beast at a time). The step's mobs are
        not hunted meanwhile (rxp_step clears wanted for this step)."""
        rx = self._rx
        self._no_pet_until = time.time() + 60.0
        if getattr(s, "pet_exists", False):
            self.rlog("rxp use: a pet is out; dismissing it before the taming")
            self.keys.press("g")
            time.sleep(1.0)
        tries = rx["use_tries"] = rx.get("use_tries", 0) + 1
        st = self.s()
        if not (st.has_target and st.t_rxp_mob and not st.t_dead and st.t_attackable):
            self.keys.press("num*")               # BotEyeFind: /target the step's mob by name (RXP's macro)
            time.sleep(0.4)
            st = self.s()
        if not (st.has_target and st.t_rxp_mob and not st.t_dead and st.t_attackable):
            if st.has_target and not st.t_rxp_mob:
                self.keys.press("f11")
            pts = self._rxp_points(r, s) or self._rxp_ring(r, s)
            if pts:
                k = rx.get("use_pt", 0) % len(pts)
                x, y, z = pts[k][:3]
                if math.hypot(x - st.x, y - st.y) < 12:
                    rx["use_pt"] = k + 1              # this point reached and nothing here: the next one
                else:
                    self._rx_once(f"usewalk{k}", f"rxp use: no {r.text[:40]!r} mob in reach; walking to point {k + 1}"
                                                 f"/{len(pts)} ({x:.0f},{y:.0f})")
                    self.mv.goto(x, y, z, tol=6, timeout=25, abort=lambda q: q.combat or (q.has_target and q.t_rxp_mob))
            return True
        # the step's mob is targeted: into range, then the item on it. 10 yd, not the 28 yd flag: the Taming Rod
        # said "Out of range" from inside 28 yd (Hunt 12:36-12:37, twice, and the targeted boar was then fought)
        if not st.t_within10:
            t = time.time()
            while time.time() - t < self.USE_MOB_RANGE_WAIT:
                st = self.s()
                if st.t_within10 or st.t_dead or not st.has_target or st.combat:
                    break
                self.approach(1.0, force=True, close=True)
            self.mv.stop(); self.cancel_walk()
        st = self.s()
        if st.t_dead or not st.has_target:
            return True
        if self.cam_off or not st.t_within10:
            self.face_target()
        self.mv.stop()
        time.sleep(0.3)
        s0 = self.s()
        self.keys.press("insert")                 # BotEyeQuestItem: /use item:<the step's item> on the target
        time.sleep(0.6)
        started = self.s().casting_now
        t = time.time()
        while time.time() - t < self.USE_MOB_CAST_S:
            st = self.s()
            if not st.casting_now and time.time() - t > USE_CAST_MIN_S:
                break
            time.sleep(0.2)
        st = self.s()
        err = st.err if st.errn != s0.errn else 0
        if started or tries >= self.USE_MOB_TRIES:
            rx["uses"] += 1
        rx["use_t"] = time.time()
        self.rlog(f"rxp use item {r.item} on npc {s0.t_npc} ({'channelled' if started else 'no cast'}, use "
                  f"{rx['uses']}/{pol.USE_MAX}): msg {st.msg!r} err={err} pet={int(getattr(st, 'pet_exists', 0))} "
                  f"step {'moved' if self._rxp_progress(r, st.rxp) else 'same'}")
        if not started and not st.combat:
            # no channel: drop the target, or the main loop takes the selected beast for a pull and kills it
            self.keys.press("f11")                # (out of range: the next round walks into 10 yd first)
        return True

    def _rxp_guide_steps(self) -> dict:
        return guide_steps()

    def _rxp_use_route(self, r) -> list:
        """use_route for this step from the parsed guide (matched by item and spot, not by r.line: that is RXP's own
        line count, rxp_policy docstring). Cached per step; [] when nothing matches."""
        rx = self._rx
        if "via" not in rx:
            via = []
            try:
                via = use_route(self._rxp_guide_steps().get(norm_guide(r.guide), []), r.item, r.wp[0], r.wp[1])
            except Exception as e:
                self._rx_once("via", f"rxp: guide route lookup failed ({e!r})")
            rx["via"] = via
            if via:
                self.rlog(f"rxp use: walking the guide's {len(via)} route waypoint(s) to ({r.wp[0]:.0f},{r.wp[1]:.0f})")
        return rx["via"]

    # ------------------------------------------------------------ kill / collect
    def _rxp_anchors(self, r):
        pts = [(x, y) for x, y, rad, fl in r.pts if not fl & 2]
        if not pts and r.wp:
            pts = [(r.wp[0], r.wp[1])]
        return pts

    def _rxp_points(self, r, s):
        return [self._rxp_point(x, y, s) for x, y in self._rxp_anchors(r)]

    RING_R = (35.0, 70.0)     # yd: a step with too few points to roam is roamed on rings this far round its points
    RING_N = 8
    RING_MIN = 3

    PREV_AREA_STEPS = 2       # a step without points of its own grinds where the guide's last kill steps were

    def _rxp_prev_area(self, r) -> list:
        """(x, y) of the guide's previous kill steps (a .mob / .complete step with world .gotos), the nearest
        PREV_AREA_STEPS of them before this one: the area a pointless step ('.xp 10 >> Grind to 10', 06-11 L1780,
        Goldshire 2026-10-18: an hour of 'no hunt points; waiting') is ground in. [] when the guide does not parse."""
        steps = guide_steps().get(norm_guide(getattr(r, "guide", "")), [])
        line = getattr(r, "line", 0) or 0
        before = [st for st in steps if (st.line_no or 0) < line]
        out, n = [], 0
        for st in reversed(before):
            w = [(gb, ga) for zn, ga, gb, _r in st.gotos if str(zn).endswith("/0")]
            if w and (st.mobs or any(t == "QC" for t, _a in st.actions)):
                out += [p for p in w if p not in out]
                n += 1
                if n >= self.PREV_AREA_STEPS:
                    break
        return out

    def _rxp_ring(self, r, s):
        """Points round the step's own points (its area: no DB): a grind / kill step with 1-2 points - or one inside
        a building, which roam() drops (Goldshire 2026-10-18: the 'grind to 10' step's only point read indoors, the
        bot stood 'no hunt points; waiting' for an hour) - is roamed round them; a step with none round the guide's
        last kill steps (_rxp_prev_area)."""
        out = []
        anchors = self._rxp_anchors(r) or self._rxp_prev_area(r)
        if anchors and not self._rxp_anchors(r) and self._rx.get("prev_area_said") != getattr(r, "index", None):
            self._rx["prev_area_said"] = getattr(r, "index", None)
            self.rlog(f"rxp: {self._rx_where(r)} has no area of its own: grinding round the last kill steps' "
                      f"{len(anchors)} point(s)")
        if len(anchors) >= self.RING_MIN and not self._rxp_anchors(r):
            return [self._rxp_point(x, y, s) for x, y in anchors]      # enough points of their own to roam
        for ax, ay in anchors:
            for rad in self.RING_R:
                for k in range(self.RING_N):
                    a = 2 * math.pi * k / self.RING_N
                    out.append(self._rxp_point(ax + rad * math.cos(a), ay + rad * math.sin(a), s))
        return out

    def _rxp_hunt_pts(self, r, s, qpts, extra):
        """Hunt points of a kill step, the old driver's: DB spawns (with height, and guid + xp for roam's respawn
        waits) of RXP's mob (strip text), of the mob ids learned from its plates and of the quest DB's objective
        mobs, kept to HUNT_AREA yd round RXP's points (quests.plan area); none there: all of them within HUNT_NEAR.
        RXP's own points only when no spawn is known (one 'Crag Boar' point, 05-11 L686, is 115 yd from any boar).
        -> (points, source)."""
        if not DB_MOBS:
            # RXP's points (the guide's area); a turn-in's missed objectives: polestar's camps (qpts), not the DB
            # every point of the step: RXP marks a #loop's points completed (b1) as we pass them, so after one lap
            # only 1 of the 8 Kobold Worker points was left (2026-10-17) - for a kill step they all are its area
            if extra:
                return list(qpts), "RXP's points"
            pts = [self._rxp_point(x, y, s) for x, y, _rad, _fl in r.pts] or self._rxp_points(r, s)
            return pts, f"RXP's points ({len(pts)})"
        text = r.text or ""
        mob = text.split("|M:", 1)[1].split("::", 1)[0].strip() if "|M:" in text else ""
        cut = len(text) >= 44                 # BotEye cuts the text at 45 chars: the mob name may be a prefix
        ids = set(self.rxp_ids)
        pts, seen = [], set()
        if mob or ids:
            for p in self._rxp_spawns():
                nm = p.name or ""
                if (mob and (nm == mob or (cut and len(mob) >= 4 and nm.startswith(mob)))) or p.entry in ids:
                    k = (round(p.x), round(p.y))
                    if k not in seen:
                        seen.add(k)
                        pts.append((p.x, p.y, p.z, p.guid, 1.0))     # quest points all worth 1 (roam)
        for p in qpts:
            k = (round(p[0]), round(p[1]))
            if k not in seen:
                seen.add(k)
                pts.append(tuple(p[:3]))
        if pts:
            anchors = [] if extra else self._rxp_anchors(r)
            area = [p for p in pts if any(math.hypot(p[0] - a[0], p[1] - a[1]) <= HUNT_AREA for a in anchors)]
            if 0 < len(area) < HUNT_MIN_PTS:
                # 1-2 spawns round RXP's points are camped on respawn timers: 317's bears had 1 (-6070,-261) and
                # inst3/inst4 waited 8-14 min there (01:16-01:33); the ring twice as wide holds 10 more
                wide = [p for p in pts if any(math.hypot(p[0] - a[0], p[1] - a[1]) <= 2 * HUNT_AREA for a in anchors)]
                if len(wide) > len(area):
                    return wide, f"DB spawns round RXP's points (widened to {2 * HUNT_AREA:.0f} yd)"
            if area and self._all_dead(area):
                # every spawn here dead for a while (2 bots on 315's 9 Seers: inst3 waited 3-4 min per round,
                # 03:37-03:58): the ring twice as wide, if something is up there
                wide = [p for p in pts if any(math.hypot(p[0] - a[0], p[1] - a[1]) <= 2 * HUNT_AREA for a in anchors)]
                live = [p for p in wide if p not in area and not self._all_dead([p])]
                if live:
                    return area + live, f"DB spawns round RXP's points (+{len(live)} up within {2 * HUNT_AREA:.0f} yd)"
            if area:
                return area, "DB spawns round RXP's points"
            close = [p for p in pts if math.hypot(p[0] - s.x, p[1] - s.y) <= HUNT_NEAR]
            if close:
                return close, "DB spawns"
            # none near: a same-named mob elsewhere on the continent is not this step's (review 2026-10-14)
            if not extra and anchors:
                return self._rxp_points(r, s), "RXP's points (no DB spawn near)"
            return pts, "DB spawns"
        return (self._rxp_points(r, s) if not extra else []), "RXP's points"

    def _polestar_pts(self, qids, s) -> list:
        """(x, y, z) camps of these quests' unfinished objectives from polestar_data (each spot once per spawn it
        holds, so roam weighs a 9-spawn camp over a single one), on our map only."""
        pts = []
        try:
            import polestar_data
            mp = getattr(getattr(self.mv, "nm", None), "map_id", None)
            for qid in qids:
                lq = s.quest(qid)
                for oi in range(1, 7):
                    e = polestar_data.spots_for(qid, oi)
                    if not e:
                        continue
                    cnt = getattr(lq, "counts", None) if lq is not None else None
                    if cnt and oi - 1 < len(cnt) and cnt[oi - 1] == 255:
                        continue                           # this objective is filled (BotEye: 255)
                    if mp is not None and e.get("map") is not None and e["map"] != mp:
                        continue
                    for x, y, z, r_, n, alt in e["spots"]:
                        if alt and len(e["spots"]) > 1:
                            continue                       # fallback camps only when nothing else is listed
                        zz = z if z is not None else self._rxp_point_calc(x, y, s)[2]
                        pts += [(x, y, zz)] * max(1, min(int(n), 5))
            if pts:
                self._rx_once(f"polestar{qids}", f"rxp: {qids}: no quest DB spawns, {len(pts)} polestar camp points")
        except Exception as e:
            swallowed("rxp_follow.polestar_pts", e, self.rlog)
        return pts

    @staticmethod
    def _all_dead(pts, min_left=60.0) -> bool:
        """Every point a known spawn (guid) that tcf_characters.respawn has dead for min_left+ s more."""
        try:
            from nb import economy
            if not economy.dead_known():
                return False
            now = time.time()
            return all(len(p) >= 5 and p[3] and economy.respawn_left(p[3], now) > min_left for p in pts)
        except Exception as e:
            swallowed("rxp_follow.all_dead", e, print)
            return False

    def _qdb_hunt(self, qids, s):
        """Objective mobs and spawn points of these quests from the quest DB (its object / spawn knowledge, not its
        planner): for steps without a .mob list and for a turn-in whose objectives were missed."""
        q = getattr(self, "qdb", None)
        mobs, pts = set(), []
        if q is None or not qids or not DB_MOBS:
            return mobs, pts
        try:
            q._counts = {lq.qid: lq.counts for lq in s.quests}
            q._level = s.level
            for qid in qids:
                mobs |= set(q.objective_mobs(qid))
                pts += q._hunt_pts(qid)
        except Exception as e:
            self._rx_once("qdb", f"rxp: quest DB lookup for {sorted(qids)} failed ({e!r})")
        return mobs, pts

    def _rxp_object_errand(self, qids, s, objects_only=False):
        """A chest / object that gives a quest item of these quests (quests.py go_src), nearest, not looted lately.
        objects_only: only items no mob drops (quests.py item_src) - for steps that also name mobs."""
        q = getattr(self, "qdb", None)
        if q is None:
            return None
        now, best = time.time(), None
        for qid in qids:
            qd, lq = q.q.get(qid), s.quest(qid)
            if not qd or lq is None or lq.complete:
                continue
            for it in qd["items"]:
                if q.filled(lq.counts, q.obj_idx(qid, "item", it)):
                    continue
                if objects_only and q.item_src.get(it):
                    continue
                for go, pos in q.go_src.get(it, ()):
                    key = (qid, go, round(pos[0]), round(pos[1]))
                    if q.skip_gather.get(key, 0) > now or q.gathered.get(key, 0) > now - 600:
                        continue
                    d = math.hypot(pos[0] - s.x, pos[1] - s.y)
                    if best is None or d < best[0]:
                        best = (d, (go, pos, f"loot a chest for {qd['title']} (RXP)", qid, key))
        return best[1] if best else None

    def _rxp_explore_walk(self, r, s) -> bool:
        """Walking into an explore trigger (rx['explore_spot']): True while on it, False when there is none."""
        rx = self._rx
        if not rx.get("explore_spot"):
            return False
        x, y, z, rad = rx["explore_spot"]
        if self._rxp_walk(r, x, y, max(1.5, min(4.0, rad - 2.0)), s, z=z, shelve=False) is not None:
            return True
        self.mv.stop()
        time.sleep(2.0)                       # inside the trigger: the server counts it on entry
        rx.pop("explore_spot")
        rx["explored"] = True
        self.rlog(f"rxp: inside the explore trigger at ({x:.0f},{y:.0f}); back to the step")
        return True

    def _rxp_explore_due(self, qids, s):
        """An explore trigger of these quests to walk into now: the quest is in the log, not complete, and every
        kill / item objective the strip shows is filled (287 Frostmane Hold: 5/5 whelps, the Hold never entered;
        inst1 hunted RXP's #55 for an hour, 00:15-01:13). -> (trigger, (x, y, z), radius) or None."""
        q = getattr(self, "qdb", None)
        ex = getattr(q, "explore", None) or {}
        if self._rx.get("explored"):
            return None
        for qid in qids:
            lq = s.quest(qid)
            # not lq.complete: the client marks 287 complete at 5/5 whelps (the strip, 02:44: inst1 / inst4 287
            # complete), the server wants the trigger too; rx['explored'] stops a second walk-in
            if not ex.get(qid) or lq is None:
                continue
            qd = getattr(q, "q", {}).get(qid) or {}
            try:
                done = all(q.filled(lq.counts, q.obj_idx(qid, k, o))
                           for k, objs in (("kill", qd.get("kill", ())), ("item", qd.get("items", ()))) for o in objs)
            except Exception as e:
                swallowed("rxp_follow.explore_due", e, self.rlog)
                done = False
            if done:
                return min(ex[qid], key=lambda t_: math.hypot(t_[1][0] - s.x, t_[1][1] - s.y))
        return None

    def _rxp_kill(self, r, s, extra=()) -> bool:
        """Roam the step's points; RXP's mob list (plates with rxp_mob, learned ids) is the wanted set, so the existing
        pick_target / fight / loot do the work and roam's search (sweep, Num* = BotEyeFind from the mob list) runs at
        each point. Objects (chests) through gather_object; HUNT_WAIT at the points with nothing seen -> skip."""
        rx = self._rx
        self.rxp_hunting = True
        qids = [qid for qid, tag, done in r.quests if not done and tag in (Q_COMPLETE, Q_COLLECT)] + list(extra)
        log_read = getattr(s, "nquests", 0) <= len(s.quests)
        if qids and log_read and all(s.quest(q) is None for q in qids):
            rx.setdefault("noq_t", time.time())
            if time.time() - rx["noq_t"] > NOQ_WAIT_S:
                # 10 s of the same read (a log still loading is not a missing quest), then a skipped accept (inst3
                # 179 at Sten) goes back to its giver; only without one is the step skipped
                if self._rxp_lost_accept(r, s, qids):
                    return True
                return self._rxp_escape(r, f"quests {qids} are not in the log")
        else:
            rx.pop("noq_t", None)
        if self._rxp_explore_walk(r, s):
            return True
        if rx.get("explore_dead"):
            return self._rxp_escape(r, f"{qids}: died on the way to the explore trigger")
        ex = self._rxp_explore_due(qids, s)
        if ex is not None:
            t, (x, y, z), rad = ex
            rx["explore_spot"] = (x, y, z, rad)
            self.rlog(f"rxp: {qids}: kills done, explore trigger {t} at ({x:.0f},{y:.0f}) not entered: walking in")
            return True
        # the quest DB's objective mobs and their spawns for every kill step (the old driver's hunt), not only for
        # steps without a .mob list
        mobs, qpts = self._qdb_hunt(qids, s)
        if not qpts and qids and (extra or not self._rxp_anchors(r)):
            # the quest DB knows no spawn of these objectives and RXP gives no point either (a turn-in's missed
            # objectives, a step without coordinates): polestar's objective camps (Guides.lua spots by quest and
            # objective index). With RXP points those stay the hunt (test_talk_sten: the guide's two wolf points)
            qpts = self._polestar_pts(qids, s)
        self.wanted |= mobs
        named_mobs = bool(r.n_mobs or r.kinds & (K_MOB | K_UNITSCAN))
        if not extra:
            # an item only an object gives is gathered even when the step also names mobs: 5541 Ammo for Rumbleshot
            # (Ammo Crate 176785, gameobject_questitem) lists Young Black Bears as a hazard, and the bot hunted bears
            # beside the crate for 6 min (bot 1, 21:43-21:50)
            e = self._rxp_object_errand(qids, s, objects_only=named_mobs)
            if e is not None:
                self.gather_object(*e)
                return True
            gs = getattr(self, "guard_skip", None)            # bot.py's Backoff (gather_object sets it)
            if not named_mobs and gs is not None and any(gs.blocked(q) for q in qids):
                # its chest is guarded by an elite that beat us: roaming its point only meets the elite again
                return self._rxp_escape(r, f"{qids}: an elite guards the chest and fought us")
        if not named_mobs and not extra:
            if not mobs and r.kinds & K_TARGET and r.n_targets:
                return self._rxp_interact_step(r, s)      # an objective done by talking (gossip)
        pts, src = self._rxp_hunt_pts(r, s, qpts, extra)
        if not pts:
            return self._rxp_manual(r, s)
        self._rx["area"] = [(float(q[0]), float(q[1])) for q in pts]   # where this step's deaths count
        # every point inside a deadly area (deaths of any bot clustered there): roam keeps them all then - its filter
        # only drops some - and the bots fed the camp (L218 Riverpaws: bot 3 19:33, bot 2 19:40, 15 yd apart). Shelve
        # the step a while and grind elsewhere; it comes back when the area has cooled
        hz_ = getattr(self, "hazards", None)
        if hz_ is not None:
            with swallow("rxp_follow.hunt_death_area", self.rlog):
                if not hz_.outside(pts, ("death_area",)):
                    # ... at most DEATH_AREA_SHELVES times: then the step is skipped and the guide goes on (bots 2-4
                    # 2026-10-16 09:22-10:00 shelved Grik'nir (218) every 10 min and ground Coldridge at level 6 for
                    # 40 min while the guide waited)
                    key = self._rx_where(r)
                    das = getattr(self, "_da_shelves", None)
                    if das is None:                       # (kept across restarts, as the walk-failure counts)
                        try:
                            with open(inst.path("rx_dashelf.json"), encoding="utf-8") as f:
                                das = self._da_shelves = json.load(f)
                        except Exception:
                            das = self._da_shelves = {}
                    das[key] = das.get(key, 0) + 1
                    with swallow("rxp_follow.dashelf save", self.rlog):
                        with open(inst.path("rx_dashelf.json"), "w", encoding="utf-8") as f:
                            json.dump(das, f)
                    if das[key] > DEATH_AREA_SHELVES:
                        return self._rxp_escape(r, f"every hunt point in a deadly area, shelved {DEATH_AREA_SHELVES} "
                                                   f"times already")
                    rx["shelf"], rx["shelf_acc"] = time.time() + DEATH_AREA_SHELVE_S, rx.get("acc", 0.0)
                    rx["shelf_why"] = "every hunt point in a deadly area"
                    self.rlog(f"rxp: shelving {self._rx_where(r)} for {DEATH_AREA_SHELVE_S:.0f} s: all {len(pts)} hunt "
                              f"point(s) lie in a deadly area")
                    self.wanted, self.grind_ok = set(self.rxp_ids), True
                    return False
        # every point indoors: the step's mobs live in a cave (98319 Wendigos in the Grizzled Den, bot 1 21:59:37 was
        # skipped for it): go in, as for Grik'nir (218). Mixed steps keep the outdoor points only (indoor_ok False).
        # a quest that has us explore a place (areatrigger_involvedrelation) sends us inside it anyway: its mobs there
        # count (287 Frostmane Hold: 14 of 15 outdoor whelp spawns dead, 3 bots waiting 3-4 min on respawns at 02:35)
        ex = getattr(getattr(self, "qdb", None), "explore", None) or {}
        # ... and when the outdoor ones are too few to hunt: 4 bots took turns on the ONE outdoor whelp spawn of 182
        # (the rest in the Frostmane cave), 30-60 s respawn waits each round (2026-10-16 08:50-09:09)
        n_out = sum(1 for p in pts if not self.indoors(p))
        indoor_ok = (any(q in pol.INDOOR_QUESTS or ex.get(q) for q in qids)
                     or n_out < HUNT_MIN_PTS)
        now = time.time()
        rx.setdefault("hunt_t", now)
        if any(p.npc in self.wanted and not p.dead for p in s.plates):
            self.want_seen_t = now
        if not any(math.hypot(p[0] - s.x, p[1] - s.y) < 35 for p in pts) or s.combat:
            rx["hunt_t"] = now                # travelling or fighting: the clock restarts
        elif now - max(rx["hunt_t"], getattr(self, "want_seen_t", 0.0)) > pol.HUNT_WAIT:
            return self._rxp_escape(r, f"no target seen for {pol.HUNT_WAIT // 60} min at its points")
        if not rx.get("hunt_log"):
            rx["hunt_log"] = True
            self.rlog(f"rxp hunt: {r.text!r} quests {qids} at {len(pts)} point(s) ({src}), wanted {sorted(self.wanted)}"
                      + (f" (+quest DB mobs {sorted(mobs)})" if mobs else ""))
        self.roam(pts, indoor_ok=indoor_ok)
        return True

    def _rxp_interact_step(self, r, s) -> bool:
        """Talk to the step NPC without the quest-tag bookkeeping (a kill/collect step whose only actor is a .target)."""
        if self._rxp_npc_walk(r, s) is not None:
            return True
        if not self._rxp_target_npc(r):
            if self._rxp_near_failed(r):
                return True
            self._rx["talks"] += 1
            if self._rx["talks"] >= pol.TALK_TRIES:
                return self._rxp_escape(r, f"step NPC {pol.target_name(r)!r} not found")
            time.sleep(1.0)
            return True
        self._rx.pop("near_fail", None)
        self._rxp_interact(r, lambda: self._rxp_target_npc(r), lambda st: st.t_rxp_npc, pol.target_name(r))
        return True

    # ------------------------------------------------------------ emote / wait / visit (.complete without a kill)
    def _rxp_qobjs(self, qid) -> list:
        return quest_objectives(qid)

    def _rxp_step_text(self, r) -> str:
        """The parsed guide step's text for this strip step (BotEye sends only 'T:|M:'): the step with the same open
        .complete quests whose first world waypoint is RXP's (within 3 yd). Cached per step; '' when none matches."""
        rx = self._rx
        if "gtext" in rx:
            return rx["gtext"]
        want = {q[0] for q in r.quests if q[1] == Q_COMPLETE}
        text = ""
        try:
            for st in self._rxp_guide_steps().get(norm_guide(r.guide), []):
                qc = {int(str(a).split(",")[0]) for t, a in st.actions if t == "QC" and str(a).split(",")[0].isdigit()}
                if not want or qc != want:
                    continue
                w = [(gb, ga) for zn, ga, gb, _r in st.gotos if str(zn).endswith("/0")]
                if r.wp and w and not any(math.hypot(p[0] - r.wp[0], p[1] - r.wp[1]) <= 3.0 for p in w):
                    continue
                text = st.text or ""
                break
        except Exception as e:
            self._rx_once("gtext", f"rxp: guide text lookup failed ({e!r})")
        rx["gtext"] = text
        return text

    def _rxp_special(self, r, s):
        """pol.special_step for this step (server objective types + the guide text), or None."""
        return self._rxp_special_for(r, s, [q[0] for q in r.quests if not q[2] and q[1] == Q_COMPLETE])

    def _rxp_special_for(self, r, s, qids):
        """pol.special_step for these quests' objectives (also an unfinished hand-in's: its sit / emote / visit)."""
        if not qids or r.n_mobs:
            return None
        objs = {qid: self._rxp_qobjs(qid) for qid in qids}
        logq = {}
        for qid in qids:
            lq = s.quest(qid)
            if lq is not None:
                logq[qid] = (bool(lq.complete), tuple(lq.counts))
        return pol.special_step(r, objs, logq, self._rxp_step_text(r), qids=list(qids))

    def _rxp_be(self, line: str):
        """A local /be command (not /be rxp): stopped, no NPC window open (as _rxp_cmd)."""
        s = self.s()
        if getattr(s, "moving", False):
            self.mv.stop()
        if any(s.ui_open(i) for i in range(4)):
            self.close_windows()
        self._chat_quiet("/be " + line)

    def _rxp_visit_spot(self, r, s, trig):
        """Where an area-trigger objective is: the server's areatrigger spawn (QuestDB.trigger_pos), the guide spot
        (quests.SPECIAL_SPOTS 'visit'), the step's last wander point, RXP's waypoint. -> (x, y, z | None, radius)."""
        q = getattr(self, "qdb", None)
        tp = getattr(q, "trigger_pos", None) or {}
        for t in trig:
            if t in tp:
                (x, y, z), rad = tp[t]
                return x, y, z, rad
        with swallow("rxp_follow.visit_spot", self.rlog):
            from nb.quests import SPECIAL_SPOTS
            for qid, tag, done in r.quests:
                sp = SPECIAL_SPOTS.get(qid)
                if not done and sp and sp[0] == "visit":
                    return sp[1][0], sp[1][1], sp[1][2], float(sp[2])
        wander = [(x, y) for x, y, rad, fl in r.pts if fl & 1 and not fl & 2]
        if wander:
            return wander[-1][0], wander[-1][1], None, 5.0
        if r.wp:
            return r.wp[0], r.wp[1], None, float(r.wp[2] or 5.0)
        return None

    def _rxp_special_step(self, r, s, sp) -> bool:
        """A .complete step done by being somewhere: walk there, then
        visit       -> stand inside the trigger VISIT_WAIT s (no completion: back to the old hunt of its points);
        emote/wait  -> stop, sit (the sit key, once, when standing: the server's campfire rest starts on the stand
                       state, MiscHandler.cpp HandleStandStateChangeOpcode) and the emote (/be emote <token>: the text
                       emote the criteria count), then stand still until RXP moves on, SPECIAL_WAIT s per try.
        A fight ends a try (the main loop fights; moving or being hit stands the character up)."""
        kind, tok, trig = sp
        rx = self._rx
        if rx.get("sp_kind") != sp:
            rx["sp_kind"] = sp
            self.rlog(f"rxp {kind}: {self._rx_where(r)} quests={r.quests} "
                      f"{('emote ' + tok + ' ') if tok else ''}{self._rxp_step_text(r)[:80]!r}")
        if kind == "visit":
            spot = self._rxp_visit_spot(r, s, trig)
            if spot is None:
                rx["special_off"] = True
                return True
            x, y, z, rad = spot
            tol = max(1.5, min(4.0, rad - 2.0))
            res = self._rxp_walk(r, x, y, tol, s, z=z, shelve=False)
            if res is not None:
                return True
            rx.setdefault("visit_t", time.time())
            if time.time() - rx["visit_t"] > pol.VISIT_WAIT:
                rx["special_off"] = True
                self.rlog(f"rxp visit: inside ({x:.0f},{y:.0f}) for {pol.VISIT_WAIT:.0f} s, the objective did not "
                          f"count; hunting the step's points instead")
                return True
            self.mv.stop()
            time.sleep(1.0)
            return True
        if not r.wp:
            return self._rxp_manual(r, s)
        gx, gy, tol = r.wp[0], r.wp[1], 3.0
        if tok == "SIT":
            # "Sit near Eric's Basic Campfire" (96608): RXP's point is 3.8 yd off the fire, the bot sat 4.5 yd from it
            # and the campfire rest never came on (inst3 23:16-23:19, 2 x 90 s). Sit next to the spell focus itself
            f = spell_focus_near(gx, gy)
            if f is not None:
                gx, gy, tol = f[0], f[1], SIT_FOCUS_TOL
        res = self._rxp_walk(r, gx, gy, tol, s, shelve=False)
        if res is not None:
            rx["sp_stand"] = True
            return True
        return self._rxp_sit_wait(r, tok)

    def _rxp_sit_wait(self, r, tok) -> bool:
        rx = self._rx
        n = rx["sp_tries"] = rx.get("sp_tries", 0) + 1
        if n > pol.SPECIAL_TRIES:
            return self._rxp_escape(r, f"{pol.SPECIAL_TRIES} tries of {(tok or 'waiting').lower()} at the spot "
                                       f"did not complete it")
        self.mv.stop()
        time.sleep(0.5)                       # stopped before the sit (a sit while running is lost)
        key = (r.guide, r.line, r.index)
        st = self.s()
        sig0 = pol.progress_sig(r, st)
        if tok == "SIT" and rx.get("sp_stand", True):
            self.keys.press(SIT_KEY)          # SITORSTAND toggles: only from standing (after a walk / a fight)
            rx["sp_stand"] = False
            time.sleep(1.0)
        if tok == "SIT" and spell_focus_near(st.x, st.y, 15) is not None:
            # the server starts the campfire rest (1289723) on the sit itself (HandleStandStateChangeOpcode, fire within
            # 15 yd): no rest = the sit did not happen (inst3 23:50-23:54: 3 x 90 s waited, no rest ever seen)
            rest = getattr(self.s(), "campfire_rest", None)
            if rest is False:
                time.sleep(1.5)
                rest = getattr(self.s(), "campfire_rest", None)
            if rest is False:
                self.keys.press(SIT_KEY)      # we were sitting already and the toggle stood us up, or the press was lost
                time.sleep(1.5)
                rest = getattr(self.s(), "campfire_rest", None)
            self.rlog(f"rxp emote SIT: campfire rest after the sit key: {rest}")
            if rest is False:
                rx["sp_stand"] = True
                if n >= 2:
                    return self._rxp_escape(r, "sitting by the campfire never started the campfire rest")
                return True
        emotes, emote_t = 0, 0.0
        if tok:
            self._rxp_be(f"emote {tok.lower()}")
            emotes, emote_t = 1, time.time()
        wait = Deadline(pol.SPECIAL_WAIT, clock=time)
        self.rlog(f"rxp {('emote ' + tok) if tok else 'wait'}: try {n}/{pol.SPECIAL_TRIES} at "
                  f"({st.x:.1f},{st.y:.1f}), standing still up to {pol.SPECIAL_WAIT:.0f} s"
                  f"{' (campfire rest on)' if getattr(st, 'campfire_rest', False) else ''}")
        progressed = False
        while wait.running():
            time.sleep(1.0)
            st = self.s()
            if getattr(st, "dead", False) or getattr(st, "ghost", False):
                return True
            if st.combat or self.attackers(st):
                rx["sp_stand"] = True         # hit / fighting: standing again
                self.rlog(f"rxp {('emote ' + tok) if tok else 'wait'}: attacked after {wait.elapsed():.0f} s; "
                          f"fighting, then sitting again")
                return True
            r1 = st.rxp
            if r1 is None or not r1.present or (r1.guide, r1.line, r1.index) != key or r1.done:
                self.rlog(f"rxp {('emote ' + tok) if tok else 'wait'}: step done after {wait.elapsed():.0f} s")
                return True
            sig = pol.progress_sig(r1, st)
            if sig != sig0:
                sig0 = sig
                progressed = True
                self.rlog(f"rxp {('emote ' + tok) if tok else 'wait'}: progress after {wait.elapsed():.0f} s "
                          f"{r1.quests} {[q.counts for q in st.quests if q.qid in {x[0] for x in r1.quests}]}")
                if self._rxp_progress(r, r1) and pol.classify(r1) != "kill":
                    return True
            elif tok and emotes < 2 and not progressed and time.time() - emote_t > EMOTE_AGAIN_S:
                # the emote counts only with the campfire rest on (criteria modifier: has aura 1289723); the rest
                # starts on the stand state, which may reach the server after the first emote: once more
                self._rxp_be(f"emote {tok.lower()}")
                emotes, emote_t = emotes + 1, time.time()
        self.rlog(f"rxp {('emote ' + tok) if tok else 'wait'}: no completion after {pol.SPECIAL_WAIT:.0f} s "
                  f"(try {n}/{pol.SPECIAL_TRIES})")
        return True

    # ------------------------------------------------------------ goto / manual
    def _rxp_goto(self, r, s) -> bool:
        """A chain of goto steps is one walk: the old driver never stopped at them (guide_plan IGNORED 'GO'), and a
        stop + 1 s sleep per waypoint is the stop-and-go walking the user judges the bot by. tol 0.9 x radius: RXP
        completes the goto on its own radius check (map.lua:1322-1331) while we still run; the next leg is pathed
        from where we are and the character does not stop in between. No completion GOTO_WAIT s after arrival -> skip (RXP then shows the next step's goal)."""
        if not r.wp:
            return self._rxp_manual(r, s)
        rx = self._rx
        x, y, rad = r.wp[0], r.wp[1], r.wp[2]
        tol = max(1.0, 0.9 * rad) if rad else 3.0
        now = time.time()
        if math.hypot(x - s.x, y - s.y) <= tol + 0.5:
            rx.setdefault("arrive_t", now)
            if now - rx["arrive_t"] >= GOTO_WAIT:
                self.mv.stop()
                return self._rxp_escape(r, f"at the waypoint for {GOTO_WAIT:.0f} s, RXP did not complete it")
            time.sleep(0.5)
            return True
        # every leg is pathed on the bot mesh (review 2026-10-14: a straight 'direct' leg between two RXP waypoints
        # is not a hand-checked trail; it runs into walls / off ledges and its stucks land in bad_spots.json as
        # wedges on walkable mesh). The run-on (stop=False) alone removes the stop-and-go.
        w = self._rxp_walk(r, x, y, tol, s, stop=False, shelve=False)
        st = self.s()
        if w is None and math.hypot(x - st.x, y - st.y) <= tol + 0.5:
            self._rx_goto_prev = (x, y, time.time())
            check = Deadline(GOTO_RUNON_S, clock=time)
            while check.running():            # RXP's radius check: the next goto leg takes the stick over at once
                r1 = st.rxp
                if self._rxp_progress(r, r1):
                    if r1 is not None and r1.wp and pol.classify(r1) == "goto":
                        self._rx_running = True   # rxp_step stops us if the next tick is not that goto leg
                        return True           # still running: no stop between the legs
                    break
                time.sleep(0.1)
                st = self.s()
        self.mv.stop()
        return True

    # (a tram portal within 80 yd of the step's waypoint and within 600 yd of us)

    def _rxp_tram(self, r, s) -> bool:
        """A manual step whose waypoints lead to the Deeprun Tram portal (RXP '.subzone 2257 >> Enter the Deeprun
        Tram'): walk the tunnel to the other end (nb/transit.py walk_tram). True when it handled the step this tick."""
        rx = self._rx
        if not r.wp or s.inst not in (0, 369) or rx.get("tram_done") or rx.get("tram_tries", 0) >= 2:
            return False
        try:
            from nb import transit
            pin = transit.portal_into(transit.MAP, (r.wp[0], r.wp[1]), within=80.0)
            if pin is None:
                if s.inst == transit.MAP:        # in the tunnel by accident (a path through a portal): back out
                    transit.walk_tram(self, None, back=True)
                    self.mv.set_map(self.s().inst)
                    return True
                return False
            if s.inst == 0 and math.hypot(pin[2] - s.x, pin[3] - s.y) > 600.0:
                return False
            rx["tram_tries"] = rx.get("tram_tries", 0) + 1
            self.rlog(f"rxp tram {self._rx_where(r)}: walking the Deeprun tunnel")
            ok = transit.walk_tram(self, (pin[2], pin[3]))
        except Exception as e:
            swallowed("rxp_follow._rxp_tram", e, self.rlog)
            return False
        finally:
            with swallow("rxp_follow._rxp_tram map", self.rlog):
                self.mv.set_map(self.s().inst)       # the mover follows the map we ended on
        rx["tram_done"] = bool(ok)
        self.rlog(f"rxp tram: {'arrived' if ok else 'gave up'}")
        return True

    def _rxp_manual(self, r, s) -> bool:
        """'+text' / unknown tag / nothing actionable: walk to its waypoint if one is far (the text is usually 'go
        there'), wait MANUAL_WAIT s for the sticky companions to clear it, then skip (and next MANUAL_WAIT s later)."""
        rx = self._rx
        if self._rxp_tram(r, s):
            return True
        far = r.wp and math.hypot(r.wp[0] - s.x, r.wp[1] - s.y) > pol.MANUAL_TRAVEL_YD
        if far and not rx.get("manual_far_said"):
            # nothing the bot can do there (no kind, no quest): not worth a trip across zones (bot 1 22:53 flew 6 min
            # from Westfall to Ironforge for an empty Loch Modan step, skipped it on arrival, and had no way back)
            rx["manual_far_said"] = True
            self.rlog(f"rxp manual step {self._rx_where(r)}: its waypoint is "
                      f"{math.hypot(r.wp[0] - s.x, r.wp[1] - s.y):.0f} yd off and it asks nothing of us; not travelling")
        if r.wp and not rx.get("manual_walked") and not far:
            if math.hypot(r.wp[0] - s.x, r.wp[1] - s.y) > 25:
                if self._rxp_walk(r, r.wp[0], r.wp[1], 5.0, s, shelve=False) == "abort":
                    return True
            rx["manual_walked"] = True
        now = time.time()
        if rx.get("manual_t") is None:
            rx["manual_t"] = now
            self.rlog(f"rxp manual step {self._rx_where(r)} kinds={pol.kind_names(r.kinds)} {r.text!r}: "
                      f"waiting {pol.MANUAL_WAIT:.0f} s")
        if now - rx["manual_t"] >= pol.MANUAL_WAIT:
            rx["manual_t"] = now
            return self._rxp_escape(r, f"manual step, nothing for the bot to do ({pol.kind_names(r.kinds)})")
        time.sleep(1.0)
        return True

    # ------------------------------------------------------------ death / training
    # the step death count lived in memory only: every bot restart reset it, and inst4 died 6 times on 433's
    # Bonesnapper step (04:51-05:33, each "death 1" or "2") without DEATH_SKIP ever firing
    RX_DEATHS_KEEP_S = 7200.0

    def _rx_deaths_file(self):
        if not PERSIST:
            return None
        try:
            from nb import inst
            return inst.path("rx_deaths.json")
        except Exception as e:
            swallowed("rxp_follow.rx_deaths_file", e, self.rlog)
            return None

    def _rx_walkfail_file(self):
        f = self._rx_deaths_file()
        return f and f.replace("rx_deaths.json", "rx_walkfail.json")

    def _rx_walkfail_get(self, key) -> int:
        """Failed walks to this step's goal recorded in rx_walkfail.json within RX_DEATHS_KEEP_S (0: none)."""
        f = self._rx_walkfail_file()
        if not key or not f or not os.path.exists(f):
            return 0
        try:
            now = __import__("time").time()
            for e in _store(f, list).read():
                if tuple(e["key"]) == tuple(key) and now - float(e.get("t", 0)) < self.RX_DEATHS_KEEP_S:
                    return int(e["n"])
        except Exception as ex:
            swallowed("rxp_follow.rx_walkfail_get", ex, self.rlog)
        return 0

    def _rx_walkfail_put(self, key, n):
        f = self._rx_walkfail_file()
        if not key or not f:
            return
        with swallow("rxp_follow.rx_walkfail_put", self.rlog):
            now = __import__("time").time()

            def merge(disk):
                keep = [e for e in disk if tuple(e["key"]) != tuple(key) and now - float(e.get("t", 0)) < self.RX_DEATHS_KEEP_S]
                return keep + [{"key": list(key), "n": int(n), "t": now}]
            _store(f, list).update(merge)

    def _rx_deaths_load(self) -> dict:
        """{step key: deaths} of rx_deaths.json ([{"key", "n", "t"}], per instance) newer than RX_DEATHS_KEEP_S."""
        f = self._rx_deaths_file()
        if not f or not os.path.exists(f):
            return {}
        try:
            now = __import__("time").time()
            return {tuple(e["key"]): int(e["n"]) for e in _store(f, list).read()
                    if now - float(e.get("t", 0)) < self.RX_DEATHS_KEEP_S}
        except Exception as ex:
            swallowed("rxp_follow.rx_deaths_load", ex, self.rlog)
            return {}

    def _rx_deaths_save(self):
        """Every step's count; the current step's time is now, the others keep the time on disk (atomic write)."""
        f = self._rx_deaths_file()
        if not f:
            return
        with swallow("rxp_follow.rx_deaths_save", self.rlog):
            now = __import__("time").time()

            def merge(disk):
                old = {tuple(e["key"]): float(e.get("t", now)) for e in disk}
                return [{"key": list(k), "n": n, "t": now if k == self._rx.get("key") else old.get(k, now)}
                        for k, n in self._rx_deaths.items()]
            _store(f, list).update(merge)

    def rxp_note_death(self, s):
        """Main loop, dead or ghost: count one death on the current step (DEATH_SKIP deaths on a kill step skip it).
        The corpse run itself is on_death's, unchanged; the stall clock skips the time (STALL_GAP)."""
        if not self.rxp_mode or self._rx_dead:
            return
        self._rx_dead = True
        key = self._rx.get("key")
        if key is None or self._rx.get("gated"):
            # a death in the LEVEL_GATE grind, or before the first step after a restart (a vendor walk: bot 2 18:39),
            # is not the step's - but said, a silent one read like a bug
            self.rlog(f"rxp: death at ({s.x:.0f},{s.y:.0f}) {'in the gate grind' if key else 'before any step'}: "
                      f"not counted")
            return
        area = self._rx.get("area")
        # where we died, not where the ghost stands: a strip read after the release is at the spirit healer (bot 2 15:01
        # died at (-5318,-2755) in its grind area, the count read (-5351,-2882), 321 yd off: 'not counted')
        dx, dy = s.x, s.y
        lp = getattr(self, "last_pos", None)
        if getattr(s, "ghost", False) and lp and time.time() - lp[2] < 120:
            dx, dy = lp[0], lp[1]
        if area and min(math.hypot(dx - a[0], dy - a[1]) for a in area) > RX_DEATH_AREA:
            # died on the way, not at the step: no reason to skip it. inst4 died twice at the Rockjaw quarry on a Loch
            # Modan boar step (09:25, 09:35) and death-skipped it at 09:49; inst2 lost L498 the same way
            self.rlog(f"rxp: death on the way to {key[0]} L{key[1]} at ({dx:.0f},{dy:.0f}), "
                      f"{min(math.hypot(dx - a[0], dy - a[1]) for a in area):.0f} yd from its area: not counted")
            return
        n = self._rx_deaths[key] = self._rx_deaths.get(key, 0) + 1
        self._rx_deaths_save()
        self.rlog(f"rxp: death {n} on {key[0]} L{key[1]} ({self._rx.get('act')})")
        if self._rx.pop("explore_spot", None) is not None:
            # dead on the way into an explore trigger: no second try this step. The Frostmane Hold mouth killed
            # level 9 bots 10+ times in an hour (02:18-03:20), each a spirit-healer walk of 600+ yd back
            self._rx["explored"] = self._rx["explore_dead"] = True
            self.rlog("rxp: died on the way to the explore trigger: giving it up for this step")

    def rxp_flag_guard(self, s):
        """Old guide driver, but BotEyeDB.rxp is still on (left by a live test; BotEyeDB is account-wide): BotEye's
        quest / gossip / merchant handlers then step aside for RXP (rxp_owns) and the old driver's talks open windows
        nobody takes. One local /be rxp off per run, never with an NPC window open (review 2026-10-14)."""
        r = getattr(s, "rxp", None)
        if self.rxp_mode or r is None or not r.flag or getattr(self, "_rx_off_sent", False):
            return
        if any(s.ui_open(i) for i in range(4)):
            return
        self._rx_off_sent = True
        self.rlog("rxp: old guide driver but BotEye's RXP flag is on: /be rxp off")
        self._chat_quiet("/be rxp off")

    def rxp_train_due(self, s) -> bool:
        """RXP mode: training happens at the guide's own .trainer steps (TALK), never as a catch-up trip (the user,
        2026-10-17: "go to the trainer when RestedXP says so" - the level 4 catch-up walked a hunter 2 km from
        Northshire to Stormwind through level 6 mobs and died twice)."""
        return False

    def _rxp_train_record(self):
        """After a trainer window on an RXP step: BotEye's purchase record -> trained.json, as train_step does, so the
        catch-up does not walk to the trainer again."""
        try:
            from nb.ev import ev
            txt = ev("tostring(BotEyeDB.lastTrain)", self.eye, self.keys, wait=2.0)
        except Exception as e:
            self.rlog(f"rxp train: could not read the addon's record ({e!r})")
            return
        s = self.s()
        m = re.search(r"bought (\d+) of (\d+) poor (\d+) need (\d+)", txt or "")
        if (m and int(m.group(3)) == 0) or "empty" in (txt or ""):
            trainers.mark_trained(s.level)
            self.train_need = 0
            self.rlog(f"rxp train: trained at level {s.level} ({txt})")
        elif m:
            self.train_need = int(m.group(4))
            self.rlog(f"rxp train: {m.group(3)} spell(s) unaffordable, need {self.train_need} copper ({txt})")
        else:
            self.rlog(f"rxp train: no purchase record ({txt!r})")
