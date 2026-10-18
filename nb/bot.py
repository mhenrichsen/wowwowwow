"""Grinding/questing bot for the modern (WowB.exe) client on the user's server.

Reads state from the BotEye addon's pixel strip (nb.eye), acts with posted
keys (nb.win), walks with the old navmesh (nb.move). Stop: create nb/STOP.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dangers            # noqa: E402  (old bot: spawn table)
import factions           # noqa: E402

from nb import inst      # noqa: E402  (first: per-instance file paths)
from nb.eye import Eye    # noqa: E402
from nb import eye as eye_mod   # noqa: E402
from nb.win import Capture   # noqa: E402
from nb.ev import ev      # noqa: E402
import ctypes             # noqa: E402
from nb.move import Mover, bearing, wrap, fake_activate   # noqa: E402
from nb.win import Keys   # noqa: E402
from nb.quests import QuestDB   # noqa: E402
from nb.guide_plan import GuideDriver, read_mode, load_state, save_state   # noqa: E402
from nb.kills import (KillTracker, Kill, note_loot, loot_verdict, fresh_corpses,   # noqa: E402
                      soft_allowed, retry_loot)
from nb import claims as hunt_claims   # noqa: E402  (nb/claims.json: which bot hunts where)
from nb import trainers   # noqa: E402
from nb import classes    # noqa: E402  (the class profile: spells by role, rotation - login.RACECLASS picks it)
from nb.rotation_base import melee_finish   # noqa: E402
from nb import economy    # noqa: E402  (xp per kill, the rest line, live spawns from tcf_world / tcf_characters)
from nb.rxp_follow import RxpFollow, rxp_mode   # noqa: E402  (RXP mode: follow RestedXP, nb/RXP_PLAN.md section 3)
from nb.rxp_policy import HUNT_WAIT, INDOOR_QUESTS   # noqa: E402  (shared with RXP mode: one copy)
from nb.backoff import BackoffAttr   # noqa: E402  (the TTL'd 'leave it alone' tables)
from nb import hazards as hz   # noqa: E402  (one store for the dangerous spots: deaths, flees, skip zones)
from nb.hazards import HazardsAttr, ROUTE_KINDS, HUNT_KINDS   # noqa: E402
from nb import threat     # noqa: E402  (how many mobs a pull / fight brings, and whether hp + mana beat them)
from nb import resources as rs   # noqa: E402  (the pull / rest / drink lines as multiples of measured costs)
from nb.resources import ResourcesAttr   # noqa: E402
from nb import engage     # noqa: E402  (one progress tracker for a fight: stall / give-up / chase)
from nb import stock      # noqa: E402  (water / food in the bags, who sells the next stock: BotEye build 13)
from nb import flight     # noqa: E402  (flight masters: a long errand leg by air)
from nb import forecast   # noqa: E402  (tactical look-ahead: fight / walk forecasts, logged against outcomes)
from nb.util import Deadline, Cooldown, swallow, swallowed   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = inst.path("nb.log")
STOP = inst.path("STOP")
from nb import route   # noqa: E402  (race: faction template, home valley, guide chain)
DWARF_FACTION = route.FACTION     # (the name is the dwarf run's: the player's faction template)

CLS = classes.PROFILE         # the class this instance plays (nb/classes.py)
SPELLS = {6603: "Attack"}     # spell id -> name: every rank the class profile knows, and Attack
SPELLS.update(CLS.names)
threat.PULL_STAND = CLS.pull_stand        # a ranged opener stands this far off (its ranged attack's reach)
eye_mod.State.POWER_IS_MANA = CLS.mana    # rage / energy: the strip's power bar reads as full to the mana lines
forecast.MANA_FIGHTS = bool(CLS.nuke)     # only a caster's fight stops when the mana runs out
threat.CASTER = bool(CLS.nuke)
SLOT_KEY ={i: str(i % 10) for i in range(1, 11)}
# Below level 8 stay in Coldridge Valley: the mobs there match our level. Out in Dun Morogh level 7-8
# monsters killed the character ~7 times in 30 minutes and xp fell from ~130/min to ~27/min.
HOME, HOME_R = route.HOME, route.HOME_R     # (dwarf: Coldridge (-6130, 450), 430 yd)
LOCAL_MAX_LEVEL = 5
ELITE_ZONE_LEVELS = 3     # spawn_zones of mobs this many levels above us (and every elite) cost on every walk
# HUNT_WAIT (s at a spawn with no quest mob before the quest is shelved) and INDOOR_QUESTS (The Stolen Journal: Grik'nir
# the Cold, level 5, 40% drop, sits in the Frostmane cave) are imported from rxp_policy: one copy for both drivers
HUNT_SHELVE = 600                # ... and how long it is then left alone (was: skipped for the whole run)
# Quest bosses that are pulled only from full, adds cleared: Grik'nir (808) casts an instant Frost Shock (spell 21030)
# 1-3 s after aggro, then every 9-12 s. It hit 96-210 and killed us from 88-100% hp (13:11, 13:18, 15:58) while the
# server's SpellInfo.cpp UsesClassicFlatBasePoints bug made creature spells ~5.8x too strong; fixed 2026-10-14 16:30,
# his Frost Shock is now ~19 dmg.
BOSS_NPCS = {808}
BOSS_HP, BOSS_MANA, BOSS_MAX_CROWD = rs.BOSS_HP, rs.BOSS_MANA, 1
BOSS_MIN_LEVEL = 1       # no level floor: with the server's spell damage fixed (16:30) Grik'nir is a normal level 5
#                          (killed in 13-15 s at level 7, -12% hp, 18:01/18:03); boss_ready still wants full hp / mana
STATE_SAVE_S = 15        # guide_state.json is rewritten at most this often (and at once after a skip / shelve)
CAST_TIME = dict(CLS.cast_time)   # s per cast-time spell; the strip's casting flag is unreliable in combat
# the pull / rest / drink lines: nb/resources.py (Bot.res). The defaults, kept here under their old names:
REST_HP, REST_MANA = rs.REST_HP, rs.REST_MANA             # pull line without measurements (85 / 80)
REST_EXIT_HP, REST_EXIT_MANA = rs.REST_HP + rs.EXIT_MARGIN_HP, rs.REST_MANA + rs.EXIT_MARGIN_MANA   # rest target 90 / 85
LOW_LEVEL, LOW_REST_MANA, LOW_EXIT_MANA = rs.LOW_LEVEL, rs.LOW_PULL_MANA, rs.LOW_EXIT_MANA   # levels 1-5: 45 / 65
DRINK_BELOW = rs.DRINK_BELOW                               # never drink above 20% mana (Bot.res.drink_line)
# Rest while walking: 251 standing rests (~35 min) on 2026-10-14; at level 6+ the flat 80% line meant a rest after
# almost every kill (inst1 15:17-15:33: 9.6-14 s standing per 32 s kill cycle, fights 100% -> 29-45% mana). Between
# the drink line and the rest line, hp fine and nothing on us, the bot walks on (mana regenerates while moving) and
# only stops WALK_REST_STOP yd short of the next spawn / when a pull is in view to wait out the rest.
WALK_REST_HP = 75        # hp at or above which the rest is walked (below it rest() heals first, or stands)
WALK_REST_STOP = 35.0    # yd: stop this far from the next hunt point while the mana is still short
WALK_REST_WAIT = 40.0    # s: longest wait there before pulling anyway (the standing rest's 45 s cap, minus the walk)
# Hunt-point loop (nb/human/REPORT.md change 2): roam() kept picking the spawn underfoot, search_here answered "skip"
# (cell searched <120 s ago) and the loop started over every 0.5 s - 8.6 min of 131 ("all hunt points visited <240s
# ago; 1 up again", inst2 20:53:18-20:55:24). A point < HUNT_SKIP_NEAR yd away that gives nothing, or a search whose
# mob is beyond plate range, now blocks every hunt point within HUNT_SKIP_R yd of it for HUNT_SKIP_S s.
HUNT_SKIP_NEAR = 15.0    # yd
HUNT_SKIP_R = hz.KINDS["hunt_skip"][0]        # 40 yd   (the zones: nb/hazards.py kinds hunt_skip / roam_fail)
HUNT_SKIP_S = hz.KINDS["hunt_skip"][1]        # 60 s
ROAM_FAIL_SKIP_S = hz.KINDS["roam_fail"][1]   # 600 s: a hunt point whose walk ended stuck / timed out is left this long
# avoid circles (nb/hazards.py ROUTE_KINDS death 60 / flee 45 / danger 50 yd -> Mover zones, hunt-point filter): round
# the camp that killed / chased us, a few aggro radii, not a district. 150 / 90 / 110 yd covered Kharanos' roads (inst4
# 00:25: a 621 yd route east round a flee spot where the straight road north was 458 yd; inst2 00:34 wedged 70 yd off)
# talk givers shelved for EVENT_NPC_S at every start (no_offer): event-only NPCs that are not there - Wonderform
# Operator 15732 (Winter Veil, z 495 up a slope the mesh cannot reach), Lieutenant Nevell 16484 (Scourge invasion)
EVENT_NPCS = (15732, 16484)
EVENT_NPC_S = 6 * 3600
# deaths are shared (nb/hazards.py kind "death", nb/hazards.json): another bot's death spot is a route cost for every
# bot for 30 min (inst4 08:26 and inst2 09:05 both died to the same mob pair at (-5041,-2900))
# walked-rest density, spawn_zones camps and the pack pull rule: nb/threat.py (REST_DENSE_*, PACK_R / PACK_N, est)
LOOT_RISK_D = 20.0     # yd: a corpse this far is not walked to under the pull line with a pullable mob in view
PACK_XP_DISCOUNT = 0.5   # roam(): a hunt point's xp score / (1 + this x its packed neighbours, at most 4)
# Loot (REPORT.md change 6): one interact per corpse; no window within LOOT_OPEN_WAIT s (standing) = move on, unless
# CanLootUnit said the corpse holds loot for us. After a loot, a valid mob in view is pulled before buffs / training /
# vendor when hp / mana are at the pull line (the human targets the next mob 2.3 s after a kill, the bot took 8 s).
LOOT_OPEN_WAIT = 0.8     # s
CHAIN_PULL_S = 5.0       # s after a loot in which the chain pull applies
# Fight (REPORT.md changes 3 + 7). In melee the swing must really run (the human had auto-attack on 1.2 s after the mob
# arrived, in 100 of 122 fights; the bot 6-27% of combat time): press Attack, and when the strip's auto-attack bit is
# still off SWING_RETRY s later, interact ('m': always starts the swing, never toggles it off); after SWING_TRIES
# presses the swing counts as not startable and the bolts carry the fight. Not pulled yet: stop at bolt range and wait
# there for the opener, at most OPENER_WAIT s of a 'not ready' flag.
# (the give-up / retry timing lives in nb/engage.py with the fight's other clocks)
SWING_RETRY, SWING_TRIES, SWING_HOLD = engage.SWING_RETRY, engage.SWING_TRIES, engage.SWING_HOLD   # 1.2 s, 4, 0.8 s
OPENER_WAIT = 3.0        # s
FINISH_STALL, FINISH_BOLT = engage.FINISH_STALL, engage.FINISH_BOLT   # 3 / 6 s: melee finish, hp not moving: interact / bolt
# Target choice (REPORT.md change 5): the mob nearest the CHARACTER first, not the one nearest screen centre (47% of
# the bot's fights started out of bolt range; 63% of the human's picks were already in range). The camera looks down
# on the ground ahead, so a plate lower on the screen stands closer; plates in the same PICK_SY_BAND * FOCAL px band
# count as equally far and the one nearer screen centre wins. A key press that does not take its mob blocks only
# that mob for PICK_FAIL_S s (it blocked every candidate: ~40 s of lost picks per run).
PICK_SY_BAND = 0.08      # x FOCAL px (44 px in the 657 px windows)
PICK_NPC_SKIP_S = 60.0   # s: a mob kind that failed 2 picks in a row is left alone this long (pick_target)
PICK_FAIL_S = 15.0      # s
PACK_PULL_N = 3         # old pick rule (shadow / THREAT_LIVE off): never pull a mob (not below us) with this many awake
#                         hostiles beside it on screen. Live: threat.Model.pull's estimate
FIGHT_COSTS_N = 20       # one-mob fights whose mana cost sets the level-6+ rest line (economy.mana_line: p90 + 15)
LOCKOUT_S, LOCKOUT_FLEE_HP = 8.0, 60.0   # cast() fails of 2+ spells within this many s: locked out (Bot.locked_out)
LOOK_FLEE_MANA, LOOK_FLEE_HP = 15.0, 45.0   # find_attacker: attackers in view we cannot target, under these: flee
HUNT_FAR_N = 40           # hunt points taken when none is within 600 yd (hunt_points)
GRIND_BAND = 2           # grind hunt points: spawns that can be level-2 .. level (inst1 at level 7 killed 46 of 51
                         # mobs 3-4 levels below: 16-32 xp each instead of 80)
QUEST_SIDE_KILLS = True  # on quest steps also pull non-quest mobs in view (valid_plate; quest mobs still go first)
FOCAL = 900.0            # px: screen x offset -> angle (calibrated below)
GCD_S = 1.5                                               # s: global cooldown after a spell press (cast())
# (face by interact before the camera pan was tried and removed: 21 ok / 45 miss over the 4 bots 23:10-23:29 -
# Click-to-Move often did not turn at all, or walked 1-4 yd in first - each miss ~0.5 s on top of the pan)
SEARCH_SWEEP = os.environ.get("NB_SWEEP", "1") != "0"   # camera sweep at a hunt point when no quest mob is in view
SEARCH_NAME = os.environ.get("NB_NAME", "1") != "0"      # /target by name (reaches mobs beyond nameplate range)
SWEEP_RATE = 1.6         # rad/s: commanded camera sweep rate (right stick ~0.73), self-calibrated by Bot.pan_gain
# Ironforge / Coldridge Mountaineer spawns (world DB): where a lost fight runs to when they are close
GUARDS = [(-5592, -490), (-5683, -483), (-5551, -462), (-5491, -667), (-5766, -539), (-5909, -68), (-5908, 28),
          (-6016, -333), (-6115, 372), (-6183, 376), (-6241, 320), (-6230, 120)]


def log(msg: str):
    line = time.strftime("%H:%M:%S ") + (f"[{inst.INST}] " if inst.INST != 1 else "") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class Bot(RxpFollow):
    last_side = 1         # side the target plate was last seen on: +1 left of centre, -1 right
    recovering = False     # inside face_recover (stops handle_err recursion from the probe cast)
    no_face_until = 0.0    # nothing may touch the character before this time (after an interact press / recovery)
    FACE_STEP = 2.0        # rad per scan step: a cast needs the target within +-pi/2, so two same-direction steps cover the circle
    hold_t0 = 0.0          # opener_hold(): when the bolt-not-ready wait at bolt range began (0 = not waiting)
    # 'leave it alone' tables (nb/backoff.py; made on first use, so a Bot built with __new__ in the tests has them)
    blacklist = BackoffAttr()   # plate guid-hash: a mob that failed a pick (PICK_FAIL_S), was tapped (120 s) or
    #                             timed out / out of sight in _fight (300 s). Saved in guide_state.json
    pick_npc = BackoffAttr()    # npc id: tally = picks in a row that took nothing; the 2nd blocks the kind
    #                             PICK_NPC_SKIP_S (pick_target)
    no_offer = BackoffAttr()    # talk-giver npc id: blocked = the guide / planner leaves the giver alone. talk()'s
    #                             tallies live in it under ("target_miss" | "miss_talks" | "owed", npc)
    gfail = BackoffAttr()       # gather key (quest, object, spot): failed chest attempts, GATHER_BACKOFF s each;
    #                             mirrored into qdb.skip_gather, which the planners read (quests.py, rxp_follow.py)
    guard_skip = BackoffAttr()  # quest id: its chest's elite guard fought us - RXP mode leaves the step (1 h)
    noloot = BackoffAttr()      # npc id: tally of its corpses that opened no loot window (3 + never looted: skipped)
    retry = BackoffAttr()       # "vendor" / "train" / "imbue": no trip / attempt while blocked
    hazards = HazardsAttr()     # nb/hazards.py: death / flee / danger circles (route costs + roam filter), hunt_skip /
    #                             roam_fail (hunt-point filter), guard_fight; deaths shared through nb/hazards.json
    tm = threat.Model()         # nb/threat.py: pull / fight estimates from the server spawn table + the plates
    res = ResourcesAttr()       # nb/resources.py: pull line, rest target, drink line, heal / cast affordability

    def __init__(self):
        global FOCAL
        self.eye = Eye(inst.window())
        self.keys = Keys(self.eye.cap.hwnd, self.eye.in_world)
        self.mv = Mover(self.eye, self.keys, log)
        self.mv.stop_file = STOP          # a walk ends on a restart's STOP file (halt_inputs then centres the pad)
        self.mv.on_route = self.forecast_walk   # each planned walk: the encounters on its first 150 yd (logged)
        self.mv.far_walk = lambda x, y, z, abort, force=False: self.fly_leg(x, y, z, abort=abort or self.travel_abort,
                                                                            force=force)
        self.book = forecast.Book(inst.path("forecast_book.json"))   # finished fights: the forecasts' costs
        self._fc = None                   # the running fight's forecast (Bot._fight), checked in Bot.fight
        self.rest_need = None             # (hp, mana) a look-ahead rest stands up at (forecast_rest)
        self.fbook = forecast.FleeBook(os.path.join(inst.HERE, "forecast_flee.json"))   # all bots' flees
        self._mid = None                  # the running fight's mid-fight forecast state (mid_forecast)
        # a background client ignores NPC interact (gossip / trainer / vendor) until told it is active
        # (inst2 14:44-14:58: every NPC talk failed while corpses still looted); harmless to the mouse with no pad
        self.keys.before["m"] = lambda: fake_activate(self.eye.cap.hwnd)
        self.W = self.eye.cap.w
        FOCAL = 900.0 * self.eye.cap.h / 1080        # focal length in px grows with the window height
        log(f"instance {inst.INST}: window {self.W}x{self.eye.cap.h}, FOCAL {FOCAL:.0f}")
        self.visited: dict[tuple, float] = {}
        self.kills = 0
        self.death_pos = None
        self.last_pos = None              # (x, y, t) of the latest fight() iteration: where a death most likely happened
        self.dj_t = 0.0                   # last time fight() wrote death.json while hurt
        self.last_err_n = None
        self.err5_seen = None
        self.kt = KillTracker()
        self.pad_claimed, self.pad_claim_t = False, 0.0
        self.eye.hooks.append(self.kt.update)
        self.crumbs: deque = deque(maxlen=60)     # (x, y) every 5 yd walked out of combat: the way back for flee()
        self.eye.hooks.append(self._crumb)
        self.flee_t = 0.0                         # last flee(): at most one a minute
        self._dz = (None, [])                     # (level, spawn-table zones) cache for spawn_zones()
        self.corpses: list = []           # Kill records waiting to be looted
        self.fight_no = 0                 # count of _fight() calls: Kill.fight groups the corpses of one fight
        self.loot_seen: dict = {}         # guid hash -> CanLootUnit of that dead target (kills.note_loot)
        self.eye.hooks.append(lambda st: note_loot(self.loot_seen, st))
        self.fight_opened: set = set()    # fights with a corpse whose loot window opened (looted / empty)
        self.fight_soft_fail: dict = {}   # fight -> soft-interact loot tries that opened nothing
        self.claims = hunt_claims.Claims(inst.INST)   # nb/claims.json: the hunt point each bot is farming
        self._claim_log = None            # last 'claimed by another bot' line, logged when it changes
        self.qdb = QuestDB()
        self.qdb.log = log                # its diagnostics ('plan: ... all objective mobs above level+1') go to nb.log
        self.want_seen_t = 0.0            # last sign of the hunted quest mobs: a kill, a wanted plate, an objective count moving
        self._obj_sig = None              # (quest, counts) of the live log, for that last one
        self.hunt_watch: dict = {}        # {q, mobs, t}: the HUNT_WAIT clock of the current hunt
        self.state_t = 0.0                # last guide_state.json write
        saved = load_state()              # guide / hunt state of the last run (nb/guide_state.json)
        self.rxp_mode = rxp_mode()        # env NB_RXP / nb/RXP_MODE: RestedXP drives (rxp_step), no GuideDriver
        if self.rxp_mode:
            log("rxp: RXP mode on: RestedXP decides the route (rxp_step); guide_plan / QuestDB planning off")
            self.guide = None
            # the old driver's saved route progress is written back unchanged by save_state, so a rollback to the
            # old driver does not start from an empty guide_state.json "guide" section (review 2026-10-14)
            self._saved_guide = (saved or {}).get("guide") or {}
        else:
            try:                               # RestedXP route on top of the planner (falls back to it on any problem)
                self.guide = GuideDriver(self.qdb, log, blocked=lambda npc, pos, lvl: self.no_offer.blocked(npc)
                                         or (lvl < 4 and not self.in_home(pos)), mode=read_mode(),
                                         can_hunt=self.can_hunt, state=saved)
            except Exception as e:
                log(f"guide: disabled ({e!r})")
                self.guide = None
        for npc in EVENT_NPCS:                      # not there outside their event (EVENT_NPCS)
            self.no_offer.set(npc, EVENT_NPC_S)
        self.wanted: set[int] = set()
        self.searched: dict[tuple, float] = {}      # (x/20, y/20) cell -> when search_here last looked around there
        self.loot_done_t = 0.0                      # end of the last loot(): chain_ready() pulls the next mob first
        self.find_names = None                      # names currently loaded into the BotEyeFind macro
        self.cam_off = False                        # the camera was swung away from the character heading
        self.pan_gain = 1.0                         # measured/commanded camera rate while standing (self-calibrating)
        self.sweep_sign = -1
        self.m_t = 0.0                              # last interact-walk ('m') started for a target without a plate
        self.grind_ok = False            # True only when the route says grind: else only quest mobs (or attackers) are fought
        self._indoor_cache: dict = {}
        self.face_fail = 0
        self.nopick_t = 0.0               # when the main loop first found itself in combat with nothing pick_target() takes
        self.last_side, self.recovering, self.no_face_until = 1, False, 0.0
        self.loot_ok: set[int] = set()       # npcs we have looted at least once
        try:                                # copper the trainer still wants (survives restarts: 15:09 each restart
            self.train_need = json.load(open(inst.path("train_need.json")))   # cost a 52 s walk to Teo for nothing)
        except Exception:
            self.train_need = 0
        self.rot = CLS.make_rotation(log)   # the class's rotation (nb/classes.py): which spell next
        self.ranged = CLS.ranged            # the ranged attack approach() / _fight() stop for (None: melee)
        log(f"class: {CLS.name} (ranged {CLS.ranged}, nuke {CLS.nuke}, heal {CLS.heal}): {type(self.rot).__name__}")
        trainers.min_cost(0, 0)             # load the trainer cost table now, not after a level-up kill (8.8 s stall)
        self.walk_rest = 0.0                # when the current walked rest began (0: none; rest() / ready_to_pull)
        self.pull_anyway_t = 0.0            # a wait_ready() ran out: the next pull goes ahead without another wait
        self.last_multi = False             # the last fight had 2+ attackers: the next rest is a full standing one
        self.fight_maxatt = 0               # most attackers at once in the running / last fight()
        self.fight_costs: deque = deque(maxlen=FIGHT_COSTS_N)   # (level, mana % a one-mob fight cost)
        self.melee_costs: deque = deque(maxlen=FIGHT_COSTS_N)   # (level, hp % a melee-only one-mob kill cost)
        self._line_log = None               # last rest line logged (it is logged when it moves)
        try:                                # server spawn table + who is dead, now (a 1.3 s query) and not mid-roam
            log(f"economy: {len(economy.spawns())} server spawns on map 0, {len(economy.dead(block=True))} dead")
        except Exception as e:
            log(f"economy: server spawn table unavailable ({e!r}); hunt points from the 1.12 table")
        try:                                # the threat model's spawn index now, not at the first pick (a ~2 s build)
            log(f"threat: {len(self.tm.sp.points)} hostile spawn points (pools merged); "
                f"THREAT_LIVE={int(threat.THREAT_LIVE)}")
        except Exception as e:
            log(f"threat: spawn table unavailable ({e!r}); the old plate rules decide")
        self.rxp_init()                     # RXP-mode state + the eye hook that learns RXP's mob ids (no-op when off)
        self.restore_state(saved)

    def rlog(self, msg: str):
        log(msg)                            # rxp_follow logs through the module log (tests patch nb.bot.log)

    # ------------------------------------------------------------ helpers
    def target_npc(self, npc: int, tries: int = 6) -> bool:
        """Target a friendly NPC like a player: its nameplate has a key."""
        for _ in range(tries):
            s = self.s()
            if s.t_npc == npc:
                return True
            p = next((p for p in s.plates if p.npc == npc and not p.dead), None)
            if p is not None and p.token:
                self.keys.press(f"f{p.token}" if p.token <= 10 else f"num{p.token - 11}")
                time.sleep(0.35)
            else:
                time.sleep(0.4)
        return self.s().t_npc == npc

    def close_windows(self):
        """Escape closes gossip/quest/merchant/loot windows, one per press."""
        for _ in range(4):
            s = self.s()
            if not any(s.ui_open(i) for i in range(4)):
                return
            self.keys.press("esc")
            time.sleep(0.3)

    def s(self):
        st = self.mv.state()
        m = getattr(st, "msg", None) or ""
        if m.startswith("I:offers ") and getattr(st, "msgn", None) != getattr(self, "_offers_n", None):
            # BotEye note_offers: the quests the window just opened offers (rxp_follow's unavailable-quest rule)
            self._offers_n = st.msgn
            ids = m[len("I:offers "):].strip()
            if ids != "?":
                self.offers = (time.time(), {int(q) for q in ids.split(",") if q.isdigit()})
        # the latest live position, whatever the bot was doing: a death's corpse guess (a flee or a rest moves us
        # far from the fight: bot 3 22:51 fled 200 yd, died there, and looked for the corpse where the fight began)
        if not getattr(st, "dead", False) and not getattr(st, "ghost", False) and (getattr(st, "hp_pct", 0) or 0) > 0:
            self.last_pos = (st.x, st.y, time.time())
        return st

    def ensure_window(self) -> bool:
        """After a client restart (login watchdog) the window is new: re-attach capture, keys and the pad."""
        if ctypes.windll.user32.IsWindow(self.eye.cap.hwnd):
            return True
        try:
            h = inst.window()
        except RuntimeError:
            return False
        self.eye.cap = Capture(h)
        self.keys.hwnd = h
        self.pad_claimed = False
        log(f"client window changed: now {h:#x}")
        return True

    PAD_IDS = ("(function() SetCVar('GamePadEnable',1) local c=C_GamePad.GetCombinedDeviceID() local o='' "
               "for _,id in ipairs(C_GamePad.GetAllDeviceIDs()) do if id~=c then o=o..id..' ' end end return o end)()")

    def claim_pad(self):
        """Plug in our virtual pad and make this client read only it (2026-10-14 pad workflow): the client lists a new
        device only while it believes it is active, and one pad adds TWO ids - its own and the combined device
        (C_GamePad.GetCombinedDeviceID, = every pad; claiming that made all clients read all pads and fight over the
        mouse). Own id = ids after - ids before - combined. One bot at a time (padclaim.lock); while a claim runs
        every bot steers with keys (padclaim.busy), so no stick moves while this client reads all pads."""
        import msvcrt
        from nb.move import CLAIM_BUSY
        self.pad_claim_t = time.time()
        mv = self.mv
        from nb.move import PAD_OFF
        if not mv.want_pad or os.path.exists(PAD_OFF):
            # keys only: no claim, which costs ~35 s of standing still (Shamab died idle during one, 16:01:53-16:02:28).
            # A client without its own pad must read none (device ids get reused: it would read another bot's pad)
            ev("SetCVar('GamePadEnable',0) or 'off'", self.eye, self.keys)
            self.pad_claimed = True
            return
        lockf = open(os.path.join(inst.HERE, "padclaim.lock"), "a+")
        t0 = time.time()
        while True:
            try:
                msvcrt.locking(lockf.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() - t0 > 90:
                    log("pad claim: another bot holds the claim lock; keys for now")
                    lockf.close()
                    return
                time.sleep(0.5)
        try:
            open(CLAIM_BUSY, "w").close()
            time.sleep(0.6)                                  # the other bots see it and let go of their sticks
            mv.drop_pad()
            mv.start_keeper()                                # mouse "in use" before GamePadEnable 1 (move.MouseKeeper)
            # enable first and let the client (while 'active') list the pads already plugged in: read in the same
            # call that enables it, the list is empty and every other bot's pad then looks new (15:34: own={1,2})
            ev("SetCVar('GamePadEnable',1) or 'on'", self.eye, self.keys)
            for _ in range(15):
                fake_activate(self.eye.cap.hwnd)
                time.sleep(0.1)
            r0 = ev(self.PAD_IDS, self.eye, self.keys, wait=3.0)
            before = set(r0[2:].split()) if r0.startswith("R:") else None
            ok = before is not None and mv.make_pad()
            own = set()
            if ok:
                for _ in range(30):                          # the client notices the new device only while 'active'
                    fake_activate(self.eye.cap.hwnd)
                    time.sleep(0.1)
                r1 = ev(self.PAD_IDS, self.eye, self.keys, wait=3.0)
                own = (set(r1[2:].split()) if r1.startswith("R:") else set()) - before
            if len(own) == 1:
                pid = own.pop()
                r2 = ev(f"SetCVar('GamePadSingleActiveID',{pid}) and GetCVar('GamePadSingleActiveID')", self.eye, self.keys)
                mv.pad_ok = r2.strip() == f"R:{pid}"
            if mv.pad_ok:
                self.pad_claimed = True
                log(f"pad claim: device {pid} is ours ({r0} -> {r1}); steering with the virtual gamepad")
            else:
                log(f"pad claim FAILED (before={r0!r} own={own}): keys, retry in 2 min")
                mv.drop_pad()
                ev("SetCVar('GamePadEnable',0) or 'off'", self.eye, self.keys)
                self.pad_claim_t = time.time() + 100
        finally:
            try:
                os.remove(CLAIM_BUSY)
            except OSError:
                pass
            msvcrt.locking(lockf.fileno(), msvcrt.LK_UNLCK, 1)
            lockf.close()

    def slot_of(self, name: str, s=None) -> int | None:
        s = s or self.s()
        for i, sid in enumerate(s.actions, 1):
            if SPELLS.get(sid) == name:
                return i
        return None

    def err_since(self, n0):
        s = self.eye.last
        return s.err if s and s.errn != n0 else 0

    def cast(self, name: str, wait_done=True) -> bool:
        s = self.s()
        slot = self.slot_of(name, s)
        if not slot or not s.ready(slot):
            return False
        # below the spell's cost the press only wastes the GCD while the mob hits us (inst4 2026-10-15 08:26:32:
        # a cast at 5 mana, cost 30, mob at 2%; died). The caller falls back to melee / its next option
        if not self.res.affordable(s, name):
            return False
        if s.moving:
            self.mv.stop(); self.cancel_walk(); time.sleep(0.15)
        t_wait = time.time()
        while s.casting_now and time.time() - t_wait < 3.5:       # a cast is running (maybe pushed back): wait
            time.sleep(0.05)
            s = self.s()
        t_gcd = time.time()
        while s.on_gcd and not s.casting_now and time.time() - t_gcd < 1.6:   # global cooldown: a press now only errors
            time.sleep(0.05)
            s = self.s()
        # ... and by the clock: the strip's on_gcd read clear right after an instant Earth Shock, the Lightning Bolt
        # pressed ~1 s later was swallowed, and the bot stood out its whole cast time ("never started": 185 of 216
        # bot 1 kills, 2026-10-14)
        while time.time() < getattr(self, "gcd_until", 0.0) and not s.casting_now:
            time.sleep(0.05)
            s = self.s()
        if s.no_control:                                          # stunned / feared: nothing we press will work
            time.sleep(0.3)
            return False
        n0 = s.errn
        mana0 = s.mana
        self.keys.press(SLOT_KEY[slot])
        self.gcd_until = time.time() + GCD_S
        t = time.time()
        started = False
        while time.time() - t < 0.6:
            s = self.s()
            if s.casting_now:
                started = True
                break
            # chat lines ("S:", "I:") bump errn with err == 0: not a cast failure. err keeps the last real code, so a
            # BotEye note also is not one (inst3 20:33:00 "Lightning Bolt failed: I:rxp skip 1", BotEye.lua rxp_cmd)
            if s.errn != n0 and s.err and not (s.msg or "").startswith(("I:", "S:", "E:")):
                log(f"{name} failed: {s.msg}")
                self.gcd_until = 0.0                 # a failed cast triggers no global cooldown
                return self.handle_err(s.err)
            time.sleep(0.05)
        if not started and CAST_TIME.get(name, 0.0) and not s.on_gcd:
            # no cast and no global cooldown 0.6 s after the press: most likely swallowed (147 "never started" over
            # 4 bots in run 6, each standing out a whole cast time). Press once more: if the first one did start (the
            # flag reads 0 in combat) the second only errors or queues the same spell, it does not clip the cast
            n0 = s.errn
            self.keys.press(SLOT_KEY[slot])
            self.gcd_until = time.time() + GCD_S
            t = time.time()
            while time.time() - t < 0.5:
                s = self.s()
                if s.casting_now:
                    started = True
                    log(f"{name}: the first press was swallowed; the second started it")
                    break
                if s.errn != n0 and s.err in (1, 3, 4):
                    log(f"{name} failed: {s.msg}")
                    self.gcd_until = 0.0
                    return self.handle_err(s.err)
                time.sleep(0.05)
            n0 = s.errn                              # a second-press error ("in progress") is not a failed cast
        if started:
            self.face_fail = 0
        if not started:
            # the casting flag can read 0 in combat: wait out the known cast time, else the
            # next key press clips the cast ("Interrupted" / "not ready yet")
            ct = CAST_TIME.get(name, 0.0)
            if ct:
                end = time.time() + max(0.0, ct - 0.6) + 0.15
                while time.time() < end:
                    s = self.s()
                    if s.errn != n0 and s.err:
                        break
                    time.sleep(0.05)
                # honest report: a timed cast that spent no mana never started (phantom heal: the key
                # press was swallowed). Regen over the cast is far below the spell's cost.
                cost = self.res.spell_cost(s, name)
                if cost:
                    end2 = time.time() + 0.4          # mana is spent when the cast lands: allow for strip lag
                    while mana0 - s.mana < 0.4 * cost and time.time() < end2:
                        time.sleep(0.05)
                        s = self.s()
                    if mana0 - s.mana < 0.4 * cost:
                        log(f"{name} never started (mana {mana0:.0f} -> {s.mana:.0f}, cost {cost:.0f})")
                        fails = [f for f in getattr(self, "_cast_fails", []) if time.time() - f[0] < LOCKOUT_S]
                        self._cast_fails = fails + [(time.time(), name)]
                        return False
                self._cast_fails = []
            return True          # instant spell
        if wait_done:
            # the strip's casting flag now follows the cast EVENTS (start .. stop), so it covers pushback;
            # the cap only guards against a missed event
            cap = CAST_TIME.get(name, 3.5) + 2.5
            while time.time() - t < cap:
                s = self.s()
                if not s.casting_now:
                    break
                time.sleep(0.05)
        return True

    FACE_WATCH_S = 2.6       # s of swinging watched for a facing error when no probe spell can be pressed (> a swing)

    def probe_castable(self, s) -> bool:
        """One of the class's facing probe spells (CLS.probe) can be pressed at the target now."""
        bar = self.rot.bar(s)
        return any(self.rot._can(s, bar, n, rng=True) and self.res.affordable(s, n) for n in CLS.probe)

    def face_probe(self) -> int:
        """Is the target in front of us? 1 yes, -1 a facing error, 0 cannot tell. Presses the first castable spell of
        the class's probe list (CLS.probe: spells that need facing; a facing error costs no mana and no GCD) and judges
        by the outcome. With none castable (a melee class, a hunter in melee) the auto-attack is the probe: within
        10 yd and swinging, a fresh "facing the wrong way" error in FACE_WATCH_S s is -1, none is 1."""
        s = self.s()
        n0 = s.errn
        bar = self.rot.bar(s)
        for name in CLS.probe:
            if self.rot._can(s, bar, name, rng=True) and self.res.affordable(s, name):
                if self.cast(name, wait_done=False):
                    self.rot.note_cast(name, True, self.s())
                    return 1
                s = self.s()
                if s.errn != n0 and s.err == 13:
                    break                           # too close for a ranged probe (hunter in melee): watch the swing
                return -1 if (s.errn != n0 and s.err in (1, 4)) else 0
        if not (s.t_within10 and s.has_target and not s.t_dead):
            return 0
        if not s.autoattack:
            a = self.slot_of("Attack", s)
            if a:
                self.keys.press(SLOT_KEY[a])        # start the swing: its errors are the probe
            else:
                self.interact_walk()                # (interact also turns us onto the target)
        t = time.time()
        while time.time() - t < self.FACE_WATCH_S:
            s = self.s()
            if s.errn != n0 and s.err in (1, 4):
                return -1
            time.sleep(0.05)
        return 1 if s.autoattack else 0

    def face_recover(self, why="") -> bool:
        """Turn to a target that is beside/behind us (the plate is no bearing in melee) without touching
        anything else. Returns True only if the probe (face_probe) says we face it."""
        if self.recovering:
            return False
        self.recovering = True
        t0 = time.time()
        s = self.s(); f0 = s.facing; fg = int(bool(self.mv.padding())); side = self.last_side
        try:
            self.mv.stop(); self.mv.aim_done()
            # called on a fresh facing error (handle_err / maybe_reface: why "err" / "melee") with no probe spell to
            # press: that error is the evidence - turn now (a swing-watch probe can miss the next one and do nothing)
            # (Hunt 10:45:53: a fresh err 1 in melee, a probe pressed instead, "facing fine", 10 s at a mob that
            # stayed at 100% while two others hit us, died)
            r = -1 if why in ("err", "melee") else self.face_probe()
            # (s.err is the LAST error and stays: inst2 06:00:27-47 read err=1 the whole fight with the mob centred on
            # screen, sx 858-865. A stale code is no facing fault: only face_probe's fresh error counts)
            if r != -1:
                return r == 1                                   # facing is fine, or unknowable: nothing to repair
            # rung 1: the game turns us (interact). No other input until it ends.
            if s.t_within10 or s.hp_pct >= 60:
                self.interact_walk()                            # (the finally below sets no_face_until)
                t = time.time()
                while time.time() - t < 1.0:
                    time.sleep(0.05)
                    s = self.s()
                    if time.time() - t > 0.35 and not s.moving:
                        break
                if s.moving:
                    self.cancel_walk()
                r = self.face_probe()
                if r == 1:
                    return True
                if r == 0:
                    return False
            # rung 2-3: closed-loop run-turn, same direction twice, a probe after each
            for k in range(2):
                got = self.mv.turn_run(side * self.FACE_STEP)
                log(f"  face scan {k + 1}: asked {side * self.FACE_STEP:+.1f} got {got:+.2f} fg={fg}")
                if abs(got) < 0.5:
                    log("  face: the character did not turn (pad/keys not reaching the game?)")
                    if fg:
                        self.mv.turn(side * self.FACE_STEP)     # last resort: arrow keys
                    else:
                        break
                self.cam_off = False
                r = self.face_probe()
                if r == 1:
                    return True
                if r == 0:
                    break
            return False
        finally:
            self.recovering = False
            self.face_fail = 0
            try:
                self.last_err_n = self.s().errn                 # the error we just handled must not trigger face_target again
                f1 = self.s().facing
            except Exception:
                f1 = f0
            self.no_face_until = time.time() + 1.0
            log(f"  face_recover({why}) {time.time() - t0:.1f}s facing {f0:.2f}->{f1:.2f} fg={fg}")

    def handle_err(self, code) -> bool:
        if code in (1, 4):              # facing
            if self.recovering:
                return False                  # the probe's own error
            s = self.s()
            p = self.target_plate(s)
            self.face_fail += 1
            log(f"  facing error {code} #{self.face_fail}: facing {s.facing:.2f} plate sx {p.sx if p else None} "
                f"(centre {self.W // 2}) fg={int(bool(self.mv.padding()))} cam_off={int(self.cam_off)}")
            if self.face_fail >= 2 or (s.t_within10 and s.t_on_me):
                self.face_recover("err")               # adjacent attacker: the plate pan cannot fix it
                return False                           # the caller's own spell did not go off
            self.face_target(sync=True)
        elif code == 3:                  # no line of sight (mob behind a rock / slope): walk towards it
            t = time.time()
            try:
                while time.time() - t < 2.0:
                    s = self.s()
                    if not s.has_target or s.t_dead or s.t_on_me:
                        break
                    p = self.target_plate(s)
                    if p is None or p.sx < 0:
                        if not self.pan_to_plate(mark=False):    # swing the camera round to find it
                            self.mv.run(False)
                            self.mv.turn(0.7)
                        continue
                    self.mv.drive(-math.atan2(p.sx - self.W / 2, FOCAL))
            finally:
                self.mv.stop()
        return False

    def target_plate(self, s=None):
        s = s or self.s()
        p = next((p for p in s.plates if p.is_target), None)
        if p is not None and p.sx >= 0:
            self.last_side = -1 if p.sx > self.W / 2 else 1
        return p

    def interact_walk(self, hold: float = 0.0) -> float:
        """Interact ('m') on the target: with autoInteract the game turns us onto it, walks into reach and starts the
        swing. m_t notes the walk (cancel_walk ends it, approach spaces the presses); hold > 0: nothing else touches
        the character for that long (face_target's no_face_until) so the walk can turn us. -> the press time."""
        self.keys.press("m")
        now = self.m_t = time.time()
        if hold:
            self.no_face_until = now + hold
        return now

    def pan_to_plate(self, mark: bool = True) -> bool:
        """The target plate is off screen: swing the CAMERA round to the side it was last seen on (plate sx is
        camera-relative). Pad + SEARCH_SWEEP only; False (nothing done) otherwise, and the caller turns the character
        its own way. mark: note that the camera is now off the character heading (cam_off; handle_err's
        line-of-sight walk never set it)."""
        if not (self.mv.padding() and SEARCH_SWEEP):
            return False
        self.mv.pan(self.last_side * SWEEP_RATE / self.pan_gain)
        if mark:
            self.cam_off = True
        time.sleep(0.05)
        return True

    def maybe_reface(self, s, last_face: float) -> float:
        """_fight()'s melee branch: face the target again every 1.5 s, or at once on a new error 1 / 2 / 4
        (the facing codes; sync then makes face_target turn the character too). -> the new last_face time."""
        fresh = s.errn != self.last_err_n and s.err in (1, 2, 4)
        if time.time() - last_face > 1.5 or fresh:
            self.last_err_n = s.errn
            self.face_target(sync=fresh)       # (s.err keeps the LAST code: a stale facing error is no fault)
            return time.time()
        return last_face

    def face_target(self, sync: bool = False) -> bool:
        """Face the target. Nameplate positions are camera-relative and the character
        only follows the camera while it moves, so (with the pad) steer the camera
        with the right stick until the plate is centred, then take one short step so
        the character turns to the camera heading. Arrow-key turning rotates the
        character but not the camera, and never converges on the plate."""
        t0 = time.time()
        aimed = self.cam_off                    # a search sweep left the camera off the character heading
        if time.time() < self.no_face_until and not self.recovering:
            return False                        # an interact press / recovery is turning the character: leave it alone
        s = self.s(); p = self.target_plate(s)
        if s.t_within10 and s.t_on_me and not self.recovering:
            if sync:
                return self.face_recover("melee")
            if p is None or p.sx < 0:
                return True                    # adjacent attacker, plate not visible: a pan only decouples the camera
        while time.time() - t0 < 1.5:
            s = self.s()
            p = self.target_plate(s)
            if p is None or p.sx < 0:
                if not self.pan_to_plate():         # not on screen: swing the CAMERA round (plate sx is camera-relative)
                    self.mv.aim_done()
                    self.mv.turn(math.pi / 3)       # not on screen: look around
                    time.sleep(0.1)
                aimed = True
                continue
            ang = -math.atan2(p.sx - self.W / 2, FOCAL)
            if abs(ang) < 0.15:
                self.mv.aim_done()
                if self.mv.padding() and (aimed or sync):
                    self.mv.sync_heading()      # only after the camera was swung (or a facing error): no step on every cast
                self.cam_off = False
                return True
            if self.mv.padding():
                aimed = True
                self.mv.aim(ang)
            else:
                self.mv.turn(ang)
                time.sleep(0.12)
        self.mv.aim_done()
        return False

    # ------------------------------------------------------------ targets
    SIDE_AREA = 60.0     # yd round the RXP step's hunt points where non-step mobs in view are taken on the way

    def side_kill_ok(self, p, me) -> bool:
        """A mob the step does not want, on screen: kill it on the way only inside the RestedXP step's area and while
        no step mob is in view (Northshire 2026-10-17: the bot killed Young Wolves on the kobold step - the kobolds
        were refused, so the wolves were the only candidates)."""
        if p.sx < 0 or not QUEST_SIDE_KILLS or threat.neutral_npc(p.npc):
            return False                         # (a neutral one never comes at us: not worth a detour)
        if any(q.npc in self.wanted and not q.dead and not q.tapped and not q.friendly for q in me.plates):
            return False
        area = (getattr(self, "_rx", None) or {}).get("area") if self.rxp_mode else None
        if area and min(math.hypot(me.x - a[0], me.y - a[1]) for a in area) > self.SIDE_AREA:
            return False
        return True

    def valid_plate(self, p, me) -> bool:
        if not p.hostile or p.dead or p.player or p.elite or p.friendly:
            return False
        if p.tapped and not p.on_me:
            return False
        if not self.grind_ok and not p.on_me and p.npc not in self.wanted and not self.side_kill_ok(p, me):
            # not a quest mob: only one on screen (a plate shows within ~41 yd) is taken on the way - the human
            # kills what is near while questing (126 kills/h vs the bot's 48-69, run 8 2026-10-16: on quest steps the
            # bot walked past every other mob to hunt points and stood waiting for respawns). Off screen: leave it
            return False
        if p.npc not in self.wanted and not p.on_me and p.level <= me.level - 3:
            # on a quest errand: gray mobs on the way are not worth the stop. Grinding too: inst1 at level 7
            # killed 46 of 51 mobs 3-4 levels below for 16-32 xp each (a level 7 one gives 80; economy.xp_for)
            return False
        # never pull above our level, except a quest mob one level up at full resources (run 4: level 8 mobs
        # for a level 7 shaman ended fights at 39-51% hp / 2-9% mana and fed the death chains)
        top = me.level + (1 if p.npc in self.wanted and me.hp_pct >= 95 and me.mana_pct >= 95 else 0)
        if p.level > top or p.level < me.level - 6:
            return False
        if self.blacklist.blocked(p.ghash):
            return False
        if not p.on_me and self.pick_npc.blocked(p.npc):
            return False
        if p.combat and not p.on_me:
            return False
        if p.npc in BOSS_NPCS and not p.on_me and not self.boss_ready(me, p):
            return False                 # rest / clear the adds first (rest() tops up while the boss is on the plates)
        return True

    def boss_ready(self, st, p) -> bool:
        """A burst quest boss is pulled only from full, with at most BOSS_MAX_CROWD clearable adds near it."""
        if st.level < BOSS_MIN_LEVEL or st.hp_pct < BOSS_HP or st.mana_pct < BOSS_MANA:
            return False
        # adds: valid mobs beside it on screen (threat.beside: bearing + depth band; the boss off screen: all of them)
        adds = sum(1 for q in st.plates if q.ghash != p.ghash and q.npc not in BOSS_NPCS and not q.trivial
                   and self.valid_plate(q, st) and (p.sx < 0 or threat.beside(p, q, self.W, FOCAL)))
        return adds <= BOSS_MAX_CROWD

    def travel_abort(self, st):
        """Abort a service walk (trainer / vendor) only for a real threat (threat.travel_fight): low / gray mobs at our
        heels are outrun instead of fought, else every one restarts the whole trip. A real one is fought - or fled by
        fight()'s rules (doomed / threat.fresh_add), the same ones: no separate '3 attackers' line here any more."""
        return threat.travel_fight(st.level, st.hp_pct, [(p.level, p.trivial) for p in self.attackers(st)])

    def attackers(self, s):
        """Plates fighting us, or our pet (a pet's fight is ours: the bot helps it)."""
        return [p for p in s.plates if (p.on_me or p.on_pet) and not p.dead]

    def wanted_plates(self, st):
        """Quest-mob plates we could fight, whether or not their marker is on screen."""
        return [p for p in st.plates if p.npc in self.wanted and self.valid_plate(p, st)]

    def yaw_of(self, p):
        """Bearing of a plate relative to the camera (+ = left of screen centre)."""
        return -math.atan2(p.sx - self.W / 2, FOCAL)

    def far_stronger(self, st):
        """Something far stronger ahead (on screen, threat.stronger): stop, mark the area, re-plan. Not while we are
        inside one of our own avoid circles already (walking out of it)."""
        if not threat.stronger(st.plates, st.level, on_screen=True):
            return False
        return not self.hazards.covering(st.x, st.y, ROUTE_KINDS, mine_only=True)

    def pick_target(self) -> bool:
        """Target a valid mob. While anything is attacking us only an attacker (plate on_me) is
        accepted: the plate keys are name-based /target macros that are frozen in combat, so a key can
        land on a same-name or fresh mob that is not hitting us."""
        s = self.s()
        cands = [p for p in s.plates if self.valid_plate(p, s) or ((p.on_me or p.on_pet) and not p.dead)]
        if not cands:
            return False
        att = {p.ghash for p in cands if p.on_me}
        crowd = {}                       # ghash -> how many it brings (out of combat; pull_filter)
        if not att:
            cands, crowd = self.pull_filter(s, cands)
            if not cands:
                return False
        if any(p.npc in self.wanted for p in cands):
            # the hunt's HUNT_WAIT clock (quest_step): its mob is right here - and pullable. A quest mob that only
            # ever stands in a camp we will not pull lets the step shelve instead of standing at it for ever
            self.want_seen_t = time.time()
        want = att or {p.ghash for p in cands if p.npc in self.wanted}

        def ok(tp, st):
            if st.t_dead:
                return False
            if tp is None:
                # the name key took another unit of that name that has no painted plate (BotEye paints 12): the
                # target block says enough - a live, attackable, untapped step mob is the one we want (Northshire
                # 2026-10-17: "no target from 1 of 1 candidate" after every other press, each a 15-60 s skip)
                return (not att and st.has_target and st.t_attackable and not st.t_tapped
                        and st.t_npc in self.wanted and (st.t_level or 0) <= (st.level or 1) + 2)
            return tp.on_me if att else (self.valid_plate(tp, st) or tp.on_me)

        multi = len(att) >= 2            # several on us: the weakest first, then the nearest (near_rank)
        # out of combat a lone one goes first (the Seer camp at (-5340,-250): crowd 2-3 pulls, maxatt 3-4, 12+ deaths
        # over 4 bots 01:51-04:14)
        order = sorted(cands, key=lambda p: (bool(want) and p.ghash not in want,
                                             (p.hp if p.hp > 0 else 100) if multi else 0,
                                             crowd.get(p.ghash, 0) >= 2,
                                             self.near_rank(p)))
        if att:
            order = [p for p in order if p.ghash in att]
        dead_npc = None
        tried = []                       # mobs whose own key did not take them (PICK_FAIL_S)
        for p in order[:3]:
            if not p.token or p.token > 20 or p.npc == dead_npc:
                continue
            self.keys.press(f"f{p.token}" if p.token <= 10 else f"num{p.token - 11}")
            time.sleep(0.3)
            s = self.s()
            tp = self.target_plate(s)
            if ok(tp, s):
                self.pick_npc.reset(s.t_npc)                  # the kind's failures in a row start over
                return True
            tried.append(p)
            if s.has_target and s.t_dead:          # the name macro took a same-name corpse: that name's keys will too
                log(f"  pick: key f{p.token} hit a corpse of npc {s.t_npc}; skipping that name")
                dead_npc = s.t_npc
                self.keys.press("f11"); time.sleep(0.1)
            if att and tp is not None:
                log(f"  pick: key landed on non-attacker npc {tp.npc} (att={len(att)}); retry")
        if not att and self.rxp_mode:
            # the plate keys are /target <name> macros: with several of the same name round us they land on another
            # one (no plate, or not a candidate) and the candidate got skipped. RXP's targeting macro (BotEyeFind,
            # BotEye rxp_find_text) picks the step's mob its own way: take it when it is one we would pull
            ghs = {c.ghash for c in cands}
            self.keys.press("num*"); time.sleep(0.35)
            s = self.s()
            p = self.target_plate(s)
            if ok(p, s) and (p is None or p.ghash in ghs or p.npc in self.wanted):
                self.pick_npc.reset(s.t_npc)
                log(f"  pick: RXP target key took npc {s.t_npc} after the plate key missed")
                return True
        for i in range(6 if att else 4):
            s = self.s()
            p = self.target_plate(s)
            if ok(p, s) and (p is None or not want or p.ghash in want):
                return True
            self.keys.press("tab")
            time.sleep(0.25)
        s = self.s()
        p = self.target_plate(s)
        if ok(p, s) and (p is None or not want or p.ghash in want):
            return True
        if not att:      # nothing is hitting us: skip the mob(s) that failed, the next pick takes the next one
            bad = [p for p in (tried or order[:1]) if not p.on_me]
            for p in bad:
                self.blacklist.set(p.ghash, PICK_FAIL_S)
                # the same KIND failing pick after pick (inst4 23:06-23:11: npc 708 x 14, 2 s each): its name key does
                # not take it from here; leave every one of that kind alone for a while
                nf = self.pick_npc.bump(p.npc)
                if nf >= 2:
                    self.pick_npc.set(p.npc, PICK_NPC_SKIP_S)
                    self.pick_npc.reset(p.npc)
                    log(f"  pick: npc {p.npc} failed {nf} picks in a row; all of them skipped {PICK_NPC_SKIP_S:.0f}s")
            if bad:
                log(f"  pick: no target from {len(bad)} of {len(cands)} candidate(s) "
                    f"(npc {', '.join(str(p.npc) for p in bad)}); skipping only those {PICK_FAIL_S:.0f}s")
        self.keys.press("f11")                     # do not leave a rejected unit targeted (tgt=1 in idle.log)
        return False

    def near_rank(self, p) -> tuple:
        """Sort key for pick_target: estimated distance to the character from the plate's screen position (see
        PICK_SY_BAND): lower on screen first, then nearer screen centre; plates off screen last."""
        if p.sx < 0 or p.sy < 0:
            return (1, 0, 1e9)
        band = max(1.0, PICK_SY_BAND * FOCAL)
        return (0, -int(p.sy // band), abs(p.sx - self.W / 2))

    def _old_crowd(self, s, p) -> int:
        """The old plates-only crowd: awake hostiles within 450 px of it on screen (shadow comparison and the _fight
        log only; 450 px was 0.5 rad at 1080 px and 0.82 rad in the 657 px windows)."""
        if p is None or getattr(p, "sx", -1) < 0:
            return 0
        return sum(1 for q in s.plates if q is not p and getattr(q, "ghash", 0) != getattr(p, "ghash", 1)
                   and threat.awake_hostile(q) and getattr(q, "sx", -1) >= 0 and abs(q.sx - p.sx) < 450)

    def locked_out(self) -> int:
        """Timed casts that never started in the last LOCKOUT_S s, when 2+ different spells did: a school lockout
        (a Defias Kick / Pummel) or a silence - nothing we cast lands (bot 2 18:19:43-20:01: 12 bolts / heals never
        started with mana rising, 2 on it, 47% -> dead). 0: not locked out."""
        f = [x for x in getattr(self, "_cast_fails", []) if time.time() - x[0] < LOCKOUT_S]
        return len(f) if len({n for _, n in f}) >= 2 else 0

    def pull_est(self, s, p):
        """threat.Model.pull for plate p: its bearing (character facing + plate yaw; with the camera swung off the
        heading, or the plate off screen, only the plates count), the plates beside it, our hp / mana against the
        measured cost of a fight. None when the estimate failed (no spawn table): the old rule decides."""
        try:
            lv = s.level or 1
            beside = [(q.level, q.elite, q.trivial)        # neutral ones beside it stay out of a pull
                      for q in threat.plates_beside(s.plates, p, self.W, FOCAL, pred=threat.awake_hostile)]
            ang = None if (getattr(p, "sx", -1) < 0 or self.cam_off) else s.facing + self.yaw_of(p)
            e = self.tm.pull(lv, s.x, s.y, ang, p.npc, p.level, s.hp_pct, s.mana_pct,
                             0.0, elite=p.elite,
                             trivial=p.trivial, plates=beside)
            if e.verdict != "pull":
                return e
            # one place for the extra skip rules, so pick, pull_refused and _fight agree (bot 4 13:29:14 pulled an
            # est=2 "weight 2.64 vs 0.75 x 2.00" at the Thelsamar-road spot, its 5th death there)
            # under the pull line no fresh pull starts, whatever path found the mob (bot 2 14:09:59 pulled at 35% mana
            # 9 s into a walked rest, line 90; inst4 the same at 46 / 52 under 83)
            try:
                if not self.res.ready(s):
                    ln = self.res.lines(s)
                    return e._replace(verdict="skip", why=e.why + f"; under the pull line (hp {s.hp_pct:.0f}/"
                                                                f"{ln.pull_hp:.0f} mana {s.mana_pct:.0f}/{ln.pull_mana:.0f})")
            except Exception as ex:
                swallowed("bot.pull_est line", ex, log)
            cautious = self.cautious()
            if cautious and e.est > 1:
                return e._replace(verdict="skip", why=e.why + f"; cautious ({cautious} deaths in the last hour): "
                                                            f"lone pulls only")
            if p.npc not in self.wanted:
                tx, ty = (e.target.x, e.target.y) if e.target is not None else (s.x, s.y)
                dz = self.hazards.covering(tx, ty, ("death_route",))
                if dz:
                    return e._replace(verdict="skip", why=e.why + f"; in a deadly spot ({dz[0].info.get('n')} deaths "
                                                                f"round ({dz[0].x:.0f},{dz[0].y:.0f}))")
            return e
        except Exception as e:
            if time.time() - getattr(self, "_est_err_t", 0.0) > 300:
                self._est_err_t = time.time()
                log(f"threat: estimate unavailable ({e!r}); the old pull rule decides")
            return None

    @staticmethod
    def line_skip(e) -> bool:
        """A skip only for our own hp / mana (pull_est's pull line): the mob is fine to pull after the rest - no
        blacklist."""
        return "under the pull line" in (getattr(e, "why", "") or "")

    def pull_filter(self, s, cands):
        """Out of combat: the candidates worth pulling, and {ghash: mobs it brings beside itself} for the sort.
        Old rule: a pack is PACK_PULL_N awake plates beside it within 450 px, it not below us (the Gol'Bolar quarry:
        crowd=3 pulls at level 10 died to the adds 6 times in 30 min, inst2/inst4 04:34-05:04; two level 10
        Bonesnappers killed a level 10 from full mana, inst4 05:09:36; a level 9 with crowd 2 too, 05:17:34).
        New rule (threat.THREAT_LIVE): threat.Model.pull - the spawn table's assist / aggro neighbours and the plates,
        weighed against hp + mana (inst3 10:26: crowd=1 on screen, 4 on us). Disagreements are logged either way."""
        lv = getattr(s, "level", 0) or 0
        old_keep, new_keep, crowd_old, crowd_new = [], [], {}, {}
        now = time.time()
        seen = getattr(self, "_thr_log", None)
        if seen is None:
            seen = self._thr_log = {}
        for p in cands:
            c = self._old_crowd(s, p)
            crowd_old[p.ghash] = min(2, c)
            old_skip = getattr(p, "sx", -1) >= 0 and getattr(p, "level", 0) >= lv - 1 and c >= PACK_PULL_N - 1
            if not old_skip:
                old_keep.append(p)
            e = self.pull_est(s, p)          # always: the shadow log compares even when the old rule acts
            if e is None:
                if not old_skip:
                    new_keep.append(p)
                crowd_new[p.ghash] = crowd_old[p.ghash]
                continue
            crowd_new[p.ghash] = e.est - 1
            if e.verdict == "pull":
                new_keep.append(p)
            elif threat.THREAT_LIVE and p.ghash and not self.line_skip(e):
                # left alone a while, as pull_refused does: else valid_plate() still counts it and roam's walk stops
                # for it every second (bot 3 12:54:30-12:55: 25 routes planned in 25 s, never walked, a skipped 1202
                # in view)
                self.blacklist.set(p.ghash, self.PULL_REFUSE_S)
                # and a route cost round the pack: else the next route walks straight past it (bot 3 19:19:44 skipped
                # a gnoll pack, 19:19:49 walked into it at 94% hp, the flee crossed 3 more spawns, dead 19:20:07)
                t = getattr(e, "target", None)
                if t is not None and getattr(t, "x", None) is not None and (e.est or 0) >= 2:
                    with swallow("bot.skip_danger", log):
                        self.hazards.add("danger", t.x, t.y, r=self.SKIP_DANGER_R, ttl=self.SKIP_DANGER_S,
                                         shared=False, skip=f"{round(t.x)},{round(t.y)}")
            if (e.verdict == "skip") != old_skip and now - seen.get(p.ghash, 0.0) > 30:
                seen[p.ghash] = now
                log(f"threat: old={'skip' if old_skip else 'pull'} new={e.verdict} est={e.est} npc {p.npc} lvl {p.level} "
                    f"({getattr(s, 'hp_pct', 0):.0f}% hp {getattr(s, 'mana_pct', 0):.0f}% mana, plates crowd {c}, "
                    f"{e.src}): {e.why}"
                    f"{'' if threat.THREAT_LIVE else ' [shadow]'}")
        if len(seen) > 200:
            self._thr_log = {k: t for k, t in seen.items() if now - t < 60}
        keep, crowd = (new_keep, crowd_new) if threat.THREAT_LIVE else (old_keep, crowd_old)
        if len(keep) < len(cands) and now - getattr(self, "_pack_log_t", 0.0) > 30:
            self._pack_log_t = now
            log(f"  pick: {len(cands) - len(keep)} of {len(cands)} candidate(s) would bring too many; not pulling those"
                f" ({'threat estimate' if threat.THREAT_LIVE else f'{PACK_PULL_N - 1}+ others beside on screen'})")
        return keep, crowd

    SKIP_DANGER_R, SKIP_DANGER_S = 30.0, 600.0   # a pack judged too big: route cost round its spawn for 10 min
    CAUTIOUS_N, CAUTIOUS_S = 2, 3600.0   # own deaths within this many s: pull only what comes alone

    def cautious(self) -> int:
        """Own deaths in the last CAUTIOUS_S s when there are CAUTIOUS_N+ (hazards' 4 h death marks), else 0. bot 2
        died 5 times and bot 3 9+ on one RXP xp step (L541, the Stonesplinter / south Loch Modan packs) - each death a
        pull the estimate allowed at est 2, or an add; after two, a pair is no longer worth it."""
        try:
            now = time.time()
            ttl = hz.KINDS["death_mark"][1]
            n = sum(1 for h in self.hazards.active(("death_mark",), mine_only=True, now=now)
                    if h.until - ttl > now - self.CAUTIOUS_S)
        except Exception as e:
            swallowed("bot.cautious", e, log)
            return 0
        return n if n >= self.CAUTIOUS_N else 0

    def find_attacker(self, since: float) -> bool:
        """In combat for a while but pick_target() takes nothing: the attacker is behind the camera (no
        nameplate) or its plate does not say on_me. Do what a player does: TAB, then turn ~120 degrees and
        look again, the whole circle. Any living attackable target that targets us or has us on its threat
        list is accepted, plate or not (fight() copes with a plateless adjacent target: interact turns us).
        Logs on every call, so a stand-still in combat is never silent (nb.log 15:49:17-15:51:09: stood
        112 s in the pick_target()/sleep(0.3) branch while an unseen whelp killed us)."""
        s = self.s()
        thr = [p for p in s.plates if p.hostile and not p.dead and (p.on_me or p.threat)]
        log(f"combat {time.time() - since:.0f}s, nothing pickable: hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}% "
            f"combat={int(s.combat)} threatened={int(s.threatened)} plates={len(s.plates)} "
            f"att={len(self.attackers(s))} thr={len(thr)} fg={int(bool(self.mv.padding()))}; looking round")

        def hit(st):
            return (st.has_target and not st.t_dead and st.t_attackable and not st.t_player
                    and (st.t_on_me or st.t_threat))

        if CLS.pet and getattr(s, "pet_combat", False):
            # the pet fights something we cannot pick (no plate on us): /assist pet takes its target
            self.keys.press(self.PET_KEYS["assist"])
            time.sleep(0.3)
            st = self.s()
            if st.has_target and not st.t_dead and st.t_attackable and not st.t_player:
                log(f"  assist pet: took its target (npc {st.t_npc})")
                return True
        if CLS.heal and s.hp_pct < 50 and self.res.heal_ok(s):   # something unseen hits us: stay alive while we look
            self.keys.press("f11"); time.sleep(0.1)
            self.cast(CLS.heal)
        for k in range(4):                         # look, then 3 turns of ~2.1 rad: the whole circle
            if k:
                st = self.s()
                if st.dead or not (st.combat or self.attackers(st)):
                    break
                # an attacker plate in view whose name key did not take it: turn to it, TAB takes what is in front
                # (inst1 04:53:08-24: att=1 from the first turn, 3 full turns, 100% -> 8% hp, died)
                seen = [p for p in self.attackers(st) if p.sx >= 0]
                if seen:
                    p = min(seen, key=lambda q: abs(q.sx - self.W / 2))
                    got = self.mv.turn_run(self.yaw_of(p), maxt=1.0)
                else:
                    got = self.mv.turn_run(self.last_side * 2.1, maxt=1.5)
                self.cam_off = False
                st = self.s()
                log(f"  look round {k}: turned {got:+.2f} rad, plates={len(st.plates)} att={len(self.attackers(st))}")
                if self.pick_target():             # its on_me plate is in view now
                    return True
                att = self.attackers(st)
                if att and (st.mana_pct < LOOK_FLEE_MANA or st.hp_pct < LOOK_FLEE_HP):
                    # attackers in view the keys do not take, hp / mana too low to stand and turn (bot 4 17:34:48-58:
                    # look rounds 1-2 saw att=2 among 8 plates at 7% mana, died; 17:22:04 the same after "key landed
                    # on non-attacker" twice): run
                    r = self.flee(f"{len(att)} attacker(s) we cannot target at {st.hp_pct:.0f}% hp "
                                  f"{st.mana_pct:.0f}% mana")
                    log(f"  look round: flee -> {r}")
                    return False
            for _ in range(3):
                self.keys.press("tab"); time.sleep(0.25)
                st = self.s()
                if hit(st):
                    tp = self.target_plate(st)
                    log(f"  found attacker npc {st.t_npc} lvl {st.t_level} after {k} turn(s) "
                        f"(plate={'none' if tp is None else ('off' if tp.sx < 0 else 'on')} on_me={int(st.t_on_me)})")
                    return True
        self.keys.press("f11")                     # do not leave a non-attacker targeted
        return False

    # ------------------------------------------------------------ fight
    def fight(self) -> str:
        """_fight() plus what it cost: the mana of a one-mob kill feeds the level-6+ rest line (Bot.res), the hp of a
        melee-only one (under resources.MELEE_ONLY_MANA mana, a mob within a level of ours) the melee capacity, and a
        fight with 2+ attackers makes the next rest a full standing one. A fresh pull the threat estimate skips is not
        fought at all: "refused" (pull_refused; callers treat it as not fought)."""
        s0 = self.s()
        why = self.pull_refused(s0)
        if why:
            return "refused"
        m0, hp0, lvl, tl = s0.mana_pct, s0.hp_pct, s0.level, getattr(s0, "t_level", 0) or 0
        self.fight_maxatt = len(self.attackers(s0))
        self.walk_rest = self.pull_anyway_t = 0.0      # a walked rest ends with the pull; rest() starts the next
        self._fc, t0 = None, time.time()
        res = self._fight()
        swallow_call(lambda: self.forecast_check(s0, res, time.time() - t0))
        self.last_multi = self.last_multi or self.fight_maxatt >= 2
        if res == "killed" and self.fight_maxatt <= 1:
            s1 = self.s()
            cost = m0 - s1.mana_pct
            if m0 >= 50 and cost >= 0:                  # a normal pull from a normal start
                self.fight_costs.append((lvl, round(cost, 1)))
                npc = getattr(s0, "t_npc", 0) or 0
                if npc:
                    nc = getattr(self, "npc_costs", None)
                    if nc is None:
                        nc = self.npc_costs = {}
                    nc.setdefault(npc, deque(maxlen=8)).append(round(cost, 1))
            if 0 <= cost < rs.MELEE_ONLY_MANA and tl and abs(tl - lvl) <= 1 and hp0 - s1.hp_pct > 0:
                mc = getattr(self, "melee_costs", None)
                if mc is None:
                    mc = self.melee_costs = deque(maxlen=FIGHT_COSTS_N)
                mc.append((lvl, round(hp0 - s1.hp_pct, 1)))
                log(f"melee kill: level {tl} cost {hp0 - s1.hp_pct:.0f}% hp ({cost:.0f}% mana); melee line now "
                    f"{self.res.melee_kill_hp(lvl):.0f}% hp per kill ({len(mc)} measured)")
        return res

    def forecast_fight(self, s, n):
        """Log the forecast of the fight starting now (n mobs) -> the Forecast, None on failure."""
        try:
            fc = forecast.fight(self.book, s.level or 1, getattr(s, "t_level", 0) or s.level or 1,
                                getattr(s, "t_npc", 0) or 0, s.hp_pct, s.mana_pct, n, self.res.heal_pct(s))
            log(f"forecast: fight npc {getattr(s, 't_npc', 0)} lvl {getattr(s, 't_level', 0)} from hp {s.hp_pct:.0f}% "
                f"mana {s.mana_pct:.0f}%: {forecast.fmt(fc)}")
            return fc
        except Exception as ex:
            swallowed("bot.forecast_fight", ex, log)
            return None

    def forecast_check(self, s0, res, dur):
        """A finished fight: into the book (its costs feed the next forecasts), and the forecast against it."""
        if res not in ("killed", "dead", "fled", "lost", "timeout"):
            return
        s1 = self.s()
        npc, tl, lv = getattr(s0, "t_npc", 0) or 0, getattr(s0, "t_level", 0) or 0, s0.level or 1
        if not npc and self._fc is None:
            return
        n = max(1, self.fight_maxatt)
        dhp, dmana = s0.hp_pct - s1.hp_pct, s0.mana_pct - s1.mana_pct
        self.book.add(npc, (tl or lv) - lv, n, dur, dhp, dmana, res)
        fc = self._fc
        if fc is not None:
            hit = (res == "killed" and fc.verdict != "lose") or (res in ("dead", "fled") and fc.verdict != "ok")
            log(f"forecast check: {res} n={n} {dur:.0f}s hp {s1.hp_pct:.0f}% mana {s1.mana_pct:.0f}% vs forecast "
                f"n={fc.n} ~{fc.s}s hp {fc.end_hp}% mana {fc.end_mana}% [{fc.verdict}] -> "
                f"{'right' if hit else 'WRONG'}{'' if n == fc.n else f' (attackers {fc.n} -> {n})'}")
        self._fc = None
        md, self._mid = getattr(self, "_mid", None), None
        if md is not None and (md.get("flee_at") or md.get("fled_at")):
            fa, fd = md.get("flee_at"), md.get("fled_at")
            log(f"forecast mid check: {res}; the forecast said flee "
                + (f"at {fa[0]:.0f}s (hp {fa[1]:.0f}%, n={fa[2]})" if fa else "never")
                + "; the bot fled " + (f"at {fd[0]:.0f}s (hp {fd[1]:.0f}%)" if fd else "never"))

    MID_EVERY_S = 2.0          # s between mid-fight forecasts (and at once when the attacker count changes)

    def mid_forecast(self, s):
        """In the fight loop: the rest of this fight from the live rates and the plates, against a flee now (the
        measured flee deaths from this hp with this many on us). Logged when the attacker count, the verdict or the
        advice changes; decides nothing yet."""
        md = getattr(self, "_mid", None)
        if md is None or s.dead:
            return
        att = self.attackers(s)
        now = time.time()
        if len(att) == md["n"] and now - md["t"] < self.MID_EVERY_S:
            return
        md["t"] = now
        lv = s.level or 1
        rows = [(p.level, p.hp, p.npc, bool(p.is_target)) for p in att]
        if not rows and s.has_target and not s.t_dead and getattr(s, "t_on_me", False):
            rows = [(s.t_level, s.t_hp_pct, s.t_npc, True)]
        if not rows:
            return
        out = max(getattr(self.rot, "out", 0.0) or 0.0, getattr(self.rot, "out_avg", 0.0) or 0.0)
        m = forecast.mid(self.book, lv, s.hp_pct, s.mana_pct, rows, target_hp=s.t_hp_pct if s.has_target else None,
                         inc=getattr(self.rot, "inc", 0.0) or 0.0, out=out, heal_pct=self.res.heal_pct(s))
        pf, d, of = self.fbook.p_dead(s.hp_pct, m.n)
        dec, cs, cf = forecast.decide(m, pf)
        if dec == "flee" and md["flee_at"] is None:
            md["flee_at"] = (now - md["t0"], s.hp_pct, m.n)
        if m.n != md["n"] or dec != md["dec"] or m.verdict != md["v"]:
            log(f"forecast mid: {now - md['t0']:.0f}s n={m.n} hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}%: stay ~{m.s}s -> "
                f"hp {m.end_hp}% (p90 {m.end_hp90}%) [{m.verdict}] P(die) {m.p_die:.2f} ~{cs}s; flee P(die) {pf:.2f} "
                f"({d}/{of} here) ~{cf}s -> {dec}")
        md["n"], md["dec"], md["v"] = m.n, dec, m.verdict

    def forecast_walk(self, pts, s=None):
        """Mover.goto's hook for each planned walk: the encounters on its first forecast.WALK_HORIZON yd, logged
        when one is not "ok" (the same set once per 20 s)."""
        try:
            s = s or self.s()
            if s.combat or getattr(s, "dead", False) or getattr(s, "ghost", False):
                return
            enc = forecast.walk(self.tm, self.book, s.level or 1, [(s.x, s.y)] + [tuple(p[:2]) for p in pts],
                                s.hp_pct, s.mana_pct, self.res.heal_pct(s))
            bad = [e for e in enc if e.fc.verdict != "ok"]
            if not bad:
                return
            key = tuple((e.npc, e.d // 20) for e in bad)
            now = time.time()
            last = getattr(self, "_fcw_last", (None, 0.0))
            if key == last[0] and now - last[1] < 20:
                return
            self._fcw_last = (key, now)
            log(f"forecast: walk from hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}%: {forecast.fmt_walk(enc)}")
            self.forecast_rest(s, bad[0])
        except Exception as ex:
            swallowed("bot.forecast_walk", ex, log)

    FC_REST_YD = 100.0         # yd: a "lose" encounter this near on the walk ahead is rested for first
    FC_REST_GAP_S = 45.0       # s between two look-ahead rests

    def forecast_need(self, s, e):
        """(hp, mana) to start the walk with so encounter e forecasts "ok" ("tight" when "ok" is out of reach), or
        None when even full hp / mana does not beat it (too strong: a rest does not help)."""
        lv, heal = s.level or 1, self.res.heal_pct(s)
        t = e.d / forecast.RUN_SPEED
        reg_h, reg_m = forecast.REGEN_HP_S * t, forecast.REGEN_MANA_S * t

        def v(hp, mana):
            return forecast.fight(self.book, lv, e.level, e.npc, min(100.0, hp + reg_h), min(100.0, mana + reg_m),
                                  e.n, heal).verdict
        for want in ("ok", "tight"):
            best = None
            for hp in range(int(s.hp_pct) // 5 * 5, 101, 5):
                for mana in range(int(s.mana_pct) // 5 * 5, 101, 5):
                    ok = v(hp, mana) == want or (want == "tight" and v(hp, mana) == "ok")
                    if ok and (best is None or (hp - s.hp_pct) + (mana - s.mana_pct) < best[2]):
                        best = (max(hp, s.hp_pct), max(mana, s.mana_pct), (hp - s.hp_pct) + (mana - s.mana_pct))
                        break
            if best is not None:
                return best[0], best[1], want
        return None

    def forecast_rest(self, s, e) -> bool:
        """The walk ahead meets encounter e at "lose" within FC_REST_YD yd: rest here first (a standing rest up to
        forecast_need) when that turns it "ok" / "tight" and this spot is out of every spawn's reach. 39 of 39
        'lose' fights 2026-10-15 23:00 - 10-16 06:25 began with the bot attacked, 27 of them at low hp / mana."""
        if (e.d > self.FC_REST_YD or getattr(self, "_fc_resting", False) or s.combat or self.attackers(s)
                or time.time() - getattr(self, "_fc_rest_t", 0.0) < self.FC_REST_GAP_S):
            return False
        need = self.forecast_need(s, e)
        if need is None:
            log(f"forecast: npc {e.npc} lvl {e.level} at {e.d} yd loses even from full hp / mana; not a rest's job")
            return False
        hp, mana, want = need
        if hp <= s.hp_pct + 2 and mana <= s.mana_pct + 2:
            return False
        try:
            if self.tm.spot_risk(s.level or 1, s.x, s.y):
                log("forecast: would rest first, but this spot is in a spawn's reach; walking on")
                return False
        except Exception:
            pass
        self._fc_rest_t = time.time()
        log(f"forecast: resting before the walk: hp {s.hp_pct:.0f}->{hp:.0f}% mana {s.mana_pct:.0f}->{mana:.0f}% "
            f"makes npc {e.npc} lvl {e.level} (+{e.n - 1}) at {e.d} yd '{want}'")
        self._fc_resting, self.rest_need = True, (hp, mana)
        try:
            self.mv.stop()
            self.rest()
        finally:
            self._fc_resting, self.rest_need = False, None
            self._fc_rest_t = time.time()
        return True

    PULL_REFUSE_S = 60.0       # s a refused pull's mob is left alone (blacklist: pick_target and search_here skip it)

    def pull_refused(self, s) -> str:
        """The pull gate for EVERY fresh pull, whatever path took the target (pick_target, the chain pull, the quest
        mob by name in roam -> search_here, RXP): not on us, no attackers, and threat.Model.pull says skip -> clear
        the target, blacklist it PULL_REFUSE_S s, log, -> why. Else "". bot 2 11:32:05: pick_target skipped a camp at
        11:31:52, 13 s later the by-name path pulled it anyway ("est=3 weight 3.00 vs 0.75 x capacity 1.96"), 2
        joined, fled at 48% hp."""
        if not threat.THREAT_LIVE or not s.has_target or s.t_dead or s.t_on_me or self.attackers(s):
            return ""
        p = self.target_plate(s)
        if p is None:
            return ""                                  # beyond plate range: no estimate (the old way)
        e = self.pull_est(s, p)
        if e is None or e.verdict != "skip":
            return ""
        gh = p.ghash if p.ghash else s.t_hash
        for k in ({gh, s.t_hash, s.t_hash & 0xFFFF} - {0, None}) if not self.line_skip(e) else ():
            self.blacklist.set(k, self.PULL_REFUSE_S)
        self.keys.press("f11")
        why = f"npc {s.t_npc} lvl {s.t_level} est={e.est} ({e.src}: {e.why})"
        log(f"pull refused: {why}; left alone {self.PULL_REFUSE_S:.0f}s")
        return why

    @staticmethod
    def swing_state() -> dict:
        """Per-target state of ensure_swing(): presses so far, next press time, no-bolt hold, logged."""
        return {"n": 0, "next": 0.0, "hold": 0.0, "said": False}

    def ensure_swing(self, s, melee, sw) -> bool:
        """The mob is in melee: make sure auto-attack runs. Attack first (it toggles: pressed only while the strip
        reads the swing off); still off SWING_RETRY s later: interact ('m'), which starts the swing and walks the last
        yards, again every SWING_RETRY s. Returns False once SWING_TRIES presses did not start it (the caller then
        bolts as before; Attack is still retried every 3 s), else True."""
        n0 = sw["n"]
        step = engage.swing_step(sw, s.autoattack, s.casting_now, s.t_within10, time.time())
        if step == "on":
            if n0 > 1 and not sw["said"]:
                sw["said"] = True
                log(f"  swing on after {n0} press(es)")
            return True
        if step in ("wait", "dead"):
            return step == "wait"
        if step == "attack_retry":
            if not sw["said"]:
                sw["said"] = True
                log(f"  swing never showed after {sw['n']} presses: bolting on")
            self.keys.press(SLOT_KEY[melee])
            return False
        if step == "attack":
            self.keys.press(SLOT_KEY[melee])
        else:                                       # interact never toggles a swing off that the strip missed
            now = self.interact_walk(hold=1.0)      # let the interact-walk turn us
            sw["hold"] = now + SWING_HOLD
        return True

    @staticmethod
    def finish_step(s, caster, swing_ok, still, sw, now) -> tuple:
        """Melee finish (rotation.melee_finish) given `still` s without the mob's hp moving: (hold the bolts,
        press interact now). Past FINISH_STALL s the weapon is evidently not reaching it: interact (walks into
        reach, starts the swing), once per SWING_RETRY s; past FINISH_BOLT s the bolts finish it after all."""
        fin = melee_finish(s, caster=caster, swing_ok=swing_ok) and still < FINISH_BOLT
        return fin, engage.finish_still_step(fin, still, s.casting_now, now, sw)

    def _fight(self) -> str:
        s = self.s()
        p = self.target_plate(s)
        self.hold_t0 = 0.0
        if not s.has_target or s.t_dead:
            return "none"
        gh = p.ghash if p else s.t_hash
        far = p is None                  # targeted by name beyond plate range: the walk to it takes longer
        lb = self.slot_of(self.ranged, s)
        melee = self.slot_of("Attack", s) or 1
        t0 = time.time()
        # crowd: the old plates-only count (450 px) and the threat estimate, logged side by side: which one predicts
        # maxatt (it goes into the "fight: died" / KILL lines too)
        crowd = self._old_crowd(s, p) if p is not None else -1
        e = self.pull_est(s, p) if (p is not None and not s.t_on_me and not self.attackers(s)) else None
        if threat.THREAT_LIVE and e is not None and e.verdict == "skip":
            # pull_refused() read an earlier frame (no plate, or other plates beside it): the gate holds here too
            for k in ({p.ghash, s.t_hash, s.t_hash & 0xFFFF} - {0, None}) if not self.line_skip(e) else ():
                self.blacklist.set(k, self.PULL_REFUSE_S)
            self.keys.press("f11")
            log(f"pull refused: npc {s.t_npc} lvl {s.t_level} est={e.est} ({e.src}: {e.why}); left alone "
                f"{self.PULL_REFUSE_S:.0f}s")
            return "refused"
        est = e.est if e else -1
        log(f"fight npc {s.t_npc} lvl {s.t_level} ({s.hp_pct:.0f}% hp {s.mana_pct:.0f}% mana) att={len(self.attackers(s))} "
            f"crowd={crowd}" + (f" est={e.est} ({e.src}: {e.why})" if e else ""))
        self._fc = self.forecast_fight(s, max(1, len(self.attackers(s)), e.est if e else 1))
        self._mid = {"t0": time.time(), "t": 0.0, "n": -1, "dec": None, "v": None, "flee_at": None, "fled_at": None}
        tpt = e.target if e else None    # the target's spawn point (threat.Model.locate): where a fleeing target runs
        m1 = 0.0                          # (threat.capacity is hp-only: no mana-per-mob)
        maxatt = len(self.attackers(s))
        pulled = False
        self.kt.take()
        if not self.attackers(s) or len(self.attackers(s)) <= 1:
            swallow_call(self.rot.reset_fight)    # a new fight's rates are its own (not the last fight's 2-mob ones)
        self.fight_no = getattr(self, "fight_no", 0) + 1
        dbg = Cooldown(2.5)
        last_face = 0
        sw = self.swing_state()          # ensure_swing(): Attack presses / interacts while auto-attack reads off
        # nb/engage.py: progress (its hp dropping, the distance closing) -> the stall recoveries and the give-ups
        eg = engage.Engage(far=far, focal=FOCAL)
        eg.rearm(s=s)
        self._chase = None
        zero_hp = 0
        cap = Deadline(90)

        def rearm():
            """A new target was taken mid-fight: forget the old target's state."""
            nonlocal gh, pulled, zero_hp, tpt
            tpt = None                           # its spawn is not known: fleeing_target goes by the plates
            sw.update(self.swing_state())
            self.hold_t0 = 0.0
            self._chase = None
            s2 = self.s()
            tp = self.target_plate(s2)
            gh = tp.ghash if tp else s2.t_hash
            pulled = bool(s2.t_on_me)
            eg.rearm(attacker=True, s=s2)        # a mid-fight pick is an attacker: never "no damage" timed out
            zero_hp = 0

        def give_up(why):
            log(f"fight: {why}; giving up on this mob")
            self.blacklist.set(gh, 300)
            self.keys.press("f11")
            return "timeout"

        while not cap.passed():
            if os.path.exists(STOP):
                return "stop"
            s = self.s()
            if s.dead:
                log(f"fight: died (maxatt={maxatt}, crowd={crowd}, est={est})")
                return "dead"
            self.last_pos = (s.x, s.y, time.time())
            maxatt = max(maxatt, len(self.attackers(s)))
            self.fight_maxatt = max(self.fight_maxatt, maxatt)
            swallow_call(lambda: self.mid_forecast(s))
            if s.hp_pct < 30 and time.time() - self.dj_t > 5:       # about to die: keep the corpse position fresh
                self.dj_t = time.time()
                try:
                    json.dump([s.x, s.y], open(inst.path("death.json"), "w"))
                except Exception:
                    pass
            ks = self.kt.take()
            for k in ks:
                self.kills += 1
                k.fight = self.fight_no
                self.corpses.append(k)
                if k.npc in self.wanted:
                    self.want_seen_t = time.time()
                log(f"KILL npc {k.npc} lvl {k.level} by={k.by} xp+{k.xp} last_hp={k.hp:.0f}% in {time.time() - t0:.0f}s "
                    f"maxatt={maxatt} est={est}")
            if ks and not self.attackers(s) and (not s.has_target or s.t_dead):
                return "killed"
            if ks and s.has_target and not s.t_dead:
                tp0 = self.target_plate(s)
                if (tp0.ghash if tp0 else s.t_hash) != gh:
                    # the kill moved the client's target on to a fresh attacker (Earth Shock finished a runner, bot 4
                    # 16:32:19): its state is new - the old one's "no progress" clock read "stalled at 100%" at once
                    rearm()
            if not s.has_target:
                # target lost (it died and got cleared, or it ran): pick an attacker
                if self.attackers(s) and self.pick_target():
                    rearm(); continue
                if self.kt.waiting():
                    time.sleep(0.1)
                    continue
                log(f"target lost without kill evidence (lost={self.kt.lost})")
                return "done"
            if s.t_dead:
                if not ks and s.hp_pct >= 5:    # dead flag the tracker has not credited yet (t_dead can misread while we die)
                    self.corpses.append(Kill(s.t, s.t_npc, s.t_hash, s.t_level, "dead", 0, 0.0, s.x, s.y, s.facing,
                                             self.fight_no))
                    self.kills += 1
                    if s.t_npc in self.wanted:
                        self.want_seen_t = time.time()
                    log(f"KILL npc {s.t_npc} lvl {s.t_level} by=dead")
                if self.attackers(s):           # more of them on us: the next one, not the loot
                    self.keys.press("f11")
                    if self.pick_target():
                        rearm(); continue
                return "killed"
            if s.t_hp_pct <= 0 and not s.t_dead:
                zero_hp += 1
                if zero_hp >= 2:               # a corpse (or a lost plate) with a stale target: never bolt it
                    self.keys.press("f11")
                    if self.attackers(s) and self.pick_target():
                        rearm(); continue
                    return "killed"
            else:
                zero_hp = 0
            if s.t_tapped and not s.t_on_me and not pulled:
                self.blacklist.set(gh, 120)
                self.keys.press("f11")
                return "tapped"
            if dbg.ready():
                pl = self.target_plate(s)
                log(f"  fight: hp {s.hp_pct:.0f} t_hp {s.t_hp_pct:.0f} w10={int(s.t_within10)} bolt={int(bool(lb and s.in_range(lb)))} "
                    f"rdy={int(bool(lb and s.ready(lb)))} mana {s.mana_pct:.0f} on_me={int(s.t_on_me)} pulled={pulled} "
                    f"err={s.err} sx={pl.sx if pl else None} cast={int(s.casting_now)} auto={int(s.autoattack)} "
                    f"att={len(self.attackers(s))} thr={sum(1 for q in s.plates if q.threat and not q.dead)}")
            # a fight we cannot outlast: break off while there is hp left to run on (inst2 16:04:39, 55% hp,
            # two Snow Tracker Wolves on us; it bolted on to 4% instead). Before the engage watchdog: its recoveries
            # 'continue', and bot 4 16:32:19-25 faced / interact-walked with 4 on it from 61 to 13% hp, no flee check
            tp = self.target_plate(s)
            if tp and tp.elite and time.time() - getattr(self, "_guard_note_t", 0.0) > 5:
                self._guard_note_t = time.time()                 # gather_object: a guard that fought us (hazards)
                self.hazards.add("guard_fight", s.x, s.y, npc=s.t_npc)
            why = self.rot.doomed(s, len(self.attackers(s)), pack=sum(1 for q in self.attackers(s) if not q.trivial),
                                  elite=bool(tp and tp.elite))
            soft = False
            lk = self.locked_out()
            if not why and lk and s.hp_pct < LOCKOUT_FLEE_HP and self.attackers(s):
                why = f"locked out: {lk} casts never started in {LOCKOUT_S:.0f}s, {s.hp_pct:.0f}% hp"
            if not why:
                why = self.outmatched(s, m1)
                soft = bool(why)                 # a capacity call, not one of doomed()'s break-off rules
            # (never for a hopeless / dying-fast call: bot 2 21:05:02 finished a 12% target first with 4 on it, spent its
            # last mana, fled at 3% mana with 3 still on it, dead)
            if why and not why.startswith(("hopeless", "dying fast", "losing the race"))                     and s.t_hp_pct <= self.FLEE_FINISH_FIRST[0] and s.hp_pct >= self.FLEE_FINISH_FIRST[1]                     and s.has_target and not s.t_dead:
                # nearly dead target, our hp still good: finish it first (bot 3 18:04:11 fled 2 at 84% hp with its
                # target at 24% and 5% mana, wedged, dead) - the next pass judges the rest
                if getattr(self, "_finish_first_t", None) != gh:
                    self._finish_first_t = gh
                    log(f"  flee wanted ({why}) but the target is at {s.t_hp_pct:.0f}% and we at {s.hp_pct:.0f}%: "
                        f"finishing it first")
                why = ""
            # (a hopeless call waits only 5 s after the last flee: bot 3 19:40:49 "new=flee hopeless" 14 s after a
            # flee ran no run at all - the 60 s spacing ate it - and died in place 5 s later)
            if why and time.time() - self.flee_t > (5 if why.startswith("hopeless") else 60):
                self.flee_t = time.time()
                r = self.flee(why, soft=soft)
                if r != "stay":
                    return r
            # the engage watchdog (nb/engage.py): no progress - its hp not dropping, the distance not closing - gives
            # tiered recoveries (face, interact-walk, reposition) and, for a mob not on us that we never hit, the
            # give-up: never reached it 12 s / no damage 15 s (25 far: targeted by name), 3x no line of sight (a mob in
            # a cave: nameplates show through walls; inst4 04:57:58-04:58:26: 28 s of bolts that never landed at a
            # quarry mouth until 3 others came, died). A chase (fleeing_target) runs its own tracker
            eg.update(s, self.target_plate(s))
            act = None if self._chase is not None else eg.check(s, pulled)
            if act is not None:
                if act.kind == "giveup":
                    return give_up(act.why)
                if act.kind == "face":
                    log(f"  fight stalled at {s.t_hp_pct:.0f}% hp (err {s.err}): checking facing")
                    self.face_recover("stall")
                    if s.t_within10 and not self._chase:
                        # the probe can call a wrong facing fine (no fresh error to see): an interact turns us
                        # onto it and starts the swing either way (Hunt 10:46:02: facing "fine", 14 s more at 100%)
                        self.interact_walk(hold=1.0)
                        sw["hold"] = time.time() + SWING_HOLD
                elif act.kind == "interact":
                    log(f"  engage: {act.why}: interact-walk")
                    self.interact_walk(hold=1.0)
                    sw["hold"] = time.time() + SWING_HOLD
                elif act.kind == "reposition":
                    log(f"  engage: {act.why}: reposition ({'step back' if s.t_within10 else 'walk to it'})")
                    if s.t_within10:
                        self.keys.press("s", hold=0.5)      # back off a step: it follows and paths round what blocked
                    else:
                        self.approach(1.2, force=True)
                continue
            # in_range(Attack) is true from anywhere; trust the distance flag and
            # the client's own "out of range" error instead
            in_melee = s.t_within10
            in_bolt = lb and s.in_range(lb)
            if in_melee and s.err == 5 and s.errn != self.err5_seen and not s.t_on_me and not in_bolt:
                self.err5_seen = s.errn
                self.approach(0.8, force=True)
                continue
            ft = self.fleeing_target(s, pulled, eg.engaged, tpt, lb)
            if ft == "switch":
                self.keys.press("f11")
                if self.pick_target():
                    rearm()
                continue
            if ft == "gone":
                self.keys.press("f11")
                return "lost"
            if ft:
                continue
            swallow_call(lambda: self.pet_attack(s))
            # the class's rotation picks its spells (None: the nuke / the ranged attack / the weapon below)
            spell = self.rot.fight_spell(s, pulled=pulled, in_melee=bool(in_melee), in_bolt=bool(in_bolt),
                                         attackers=len(self.attackers(s)), target=gh)
            if spell and self.rot_cast(spell):
                if spell not in CLS.self_spells:
                    pulled = True
                continue
            if not CLS.nuke:
                # no cast filler: the ranged auto attack (Auto Shot) or the weapon carries the fight
                pulled = self.weapon_step(s, lb, melee, sw, gh, pulled, eg, last_face) or pulled
                continue
            nuke = CLS.nuke
            if in_melee:
                # swing while we cast: auto-attack runs alongside casting and adds a steady trickle of damage.
                # ensure_swing() presses Attack, checks the strip's auto-attack bit, and interacts if it stays off
                swing_ok = self.ensure_swing(s, melee, sw)
                caster = gh in self.rot.casters          # it stands 5-10 yd off and bolts: only the walk-in starts a swing
                # the weapon must show on the mob's hp: within 10 yd is not within reach (a mob 6-9 yd off, an
                # unflagged caster, a swing that the strip reads on but does not land). FINISH_STALL s without hp
                # moving: interact walks into reach; FINISH_BOLT s: bolt it as before
                still = eg.hp_still()                    # the watchdog's "its hp not dropping" clock
                finish, press = self.finish_step(s, caster, swing_ok, still, sw, time.time())
                if press:
                    log(f"  finish: mob at {s.t_hp_pct:.0f}% not taking swings for {still:.1f}s: interact")
                    now = self.interact_walk(hold=1.0)
                    sw["next"] = now + SWING_RETRY
                    sw["hold"] = now + SWING_HOLD
                if finish or time.time() < sw["hold"]:
                    # the mob is nearly dead and the weapon is on it: no 2.5 s cast (the human run finished these
                    # in melee); or an interact-walk is closing the last yards (a cast now fails while moving)
                    last_face = self.maybe_reface(s, last_face)
                    time.sleep(0.1)
                    continue
                # a caster's weapon is weak (CLS.nuke_in_melee): above MELEE_FINISH_HP keep casting point-blank,
                # keeping the heal affordable when two mobs are on us or we are hurt.
                # Bot.res.bolt_floor: a heal + 5 when hurt / 2+ on us, else 15; 0 with no heal affordable or the mob
                # at <= 25% (only for a caster, or a swing that will not start): the floor then only idles us (inst3
                # 17:46:23-26: Seer at 18%, 10% mana, 4 s of no cast and no swing; dead at 17:46:32)
                bolt_floor = self.res.bolt_floor(s, len(self.attackers(s)))
                nk = self.slot_of(nuke, s)
                if CLS.nuke_in_melee and nk and s.ready(nk) and s.mana_pct > bolt_floor and self.res.affordable(s, nuke):
                    last_face = self.maybe_reface(s, last_face)
                    self.cast(nuke)                      # the rotation's instants come first (above)
                    continue
                if not s.autoattack and s.mana_pct <= 25 and (caster or not (s.hp_pct < 60 and s.t_within10)):
                    # out of mana: melee. Not while the nuke is merely on cooldown: interact makes the
                    # character run at the mob and the next cast fails with "can't do that while moving"
                    self.interact_walk(hold=1.0)      # let the interact-walk turn us: nothing else touches the character
                    time.sleep(0.2)
                last_face = self.maybe_reface(s, last_face)
                time.sleep(0.1)
                continue
            coming = pulled and s.t_on_me          # it runs at us: the melee branch would cast at it to the same floor
            floor = self.res.bolt_floor(s, len(self.attackers(s)), zero=False)
            nk = self.slot_of(nuke, s)
            if nk and s.in_range(nk) and s.ready(nk) and (((not pulled or s.mana_pct > 45) and s.t_hp_pct > 15)
                                                          or (coming and s.mana_pct > floor)):
                tp = self.target_plate(s)
                if self.cam_off or tp is None or tp.sx < 0 or abs(self.yaw_of(tp)) > 0.6:
                    self.face_target()                 # else it is well inside the cast arc: cast straight away
                if self.cast(nuke):
                    pulled = True
                    continue
            # close in: walk towards the marker
            if pulled and s.t_on_me:
                # it is coming to us
                time.sleep(0.15)
                s = self.s()
                # within 10 yd, not the Attack slot's range bit (it reads "in range or no range": always set), so a
                # caster that stands off and bolts is walked to: inst2 01:58:47-01:59:36 stood 49 s off a Frostmane
                # Seer, w10=0, out of mana, no swing, and died; same at 01:51
                if not s.t_within10:
                    if eg.come_wait(caster=gh in self.rot.casters):   # not closing for 6 (caster) / 12 s: walk to it
                        self.approach()
                continue
            self.approach(opener=not pulled)       # not pulled yet: stop in ranged range and open from there
        log("fight timeout")
        self.blacklist.set(gh, 300)
        self.keys.press("f11")
        return "timeout"

    def outmatched(self, s, m1) -> str:
        """threat.fresh_add: a FRESH mob on us that what our hp + mana still buy cannot beat -> why (flee), else "".
        rotation.doomed() needs a measured damage rate and has none for a fresh add (inst3 10:40: kill at 44% hp / 20%
        mana, a fresh level 11 already on us, 44 -> 13 -> dead in 12 s, no flee, no heal: mana under its cost; 5 of
        inst3's last 6 deaths the same). Its 'fight it out' zone stays: 2+ on us under 70% hp with a heal's mana."""
        att = [(q.level, q.hp if q.hp > 0 else 100, q.elite, q.trivial) for q in self.attackers(s)]
        if s.t_on_me and not s.t_dead and not any(q.is_target for q in self.attackers(s)):
            att.append((s.t_level, s.t_hp_pct, False, s.t_trivial))       # the target, plate not read
        # a LONE attacker is weighed against what the weapon kills too (Bot.res.melee_kill_hp: measured, else 60% hp a
        # kill): bot 2 11:16:07 fled a single level 10 at 88% hp / 7% mana ("need 0.50 vs capacity 0.48"); and a lone
        # one is fled only when clearly outmatched with a run we survive (threat.flee_loss of the measured rate)
        why = threat.fresh_add(s.level or 1, s.hp_pct, s.mana_pct, att, m1, heal_ok=self.res.heal_ok(s),
                               hp_kill=self.res.melee_kill_hp(s.level or 1), inc=getattr(self.rot, "inc", 0.0) or 0.0,
                               out=getattr(self.rot, "out", 0.0) or 0.0)
        if why:
            key = (s.t_hash, len(att))
            if getattr(self, "_outm_log", None) != key:
                self._outm_log = key
                log(f"threat: old=fight new=flee {why}{'' if threat.THREAT_LIVE else ' [shadow]'}")
            return why if threat.THREAT_LIVE else ""
        if len(att) >= 2:
            # a fight-it-out is logged too (once per attacker count and target): silent stays read like bugs
            # (bot 4 18:18:58, jumped by 2 at 47% hp, no threat / flee line, dead 28 s later)
            key = ("stay", s.t_hash, len(att))
            if getattr(self, "_outm_log", None) != key:
                self._outm_log = key
                need, cap = threat.need_cap(s.level or 1, s.hp_pct, s.mana_pct, att, m1)
                log(f"threat: staying in: {len(att)} on us need {need:.2f} vs capacity {cap:.2f} ({s.hp_pct:.0f}% hp "
                    f"{s.mana_pct:.0f}% mana, heal {'affordable' if self.res.heal_ok(s) else 'not affordable'})")
        return ""

    FLEE_TARGET_HP = 30.0      # % mob hp: a target this low that runs off (not on us, out of melee) is fleeing
    CHASE_FAR_YD, CHASE_NEAR_YD = 35.0, 20.0   # where a runner is guessed to be: beyond / within bolt range

    def _chase_hazard(self, s, p, tpt) -> str:
        """Why chasing the runner leads somewhere we must not go, else "": a deadly area (hazards death_area) or an
        elite spawn's aggro radius (threat spawn table) where it is (plate bearing x range band; else its spawn,
        else ahead), or an awake elite plate beside it on screen."""
        try:
            if p is not None and getattr(p, "sx", -1) >= 0 and not self.cam_off:
                d = self.CHASE_NEAR_YD if (getattr(s, "t_in_ranged", False) or getattr(s, "t_within28", False)) \
                    else self.CHASE_FAR_YD
                a = s.facing + self.yaw_of(p)
                ex, ey = s.x + d * math.cos(a), s.y + d * math.sin(a)
            elif tpt is not None:
                ex, ey = tpt.x, tpt.y
            else:
                ex, ey = s.x + self.CHASE_FAR_YD * math.cos(s.facing), s.y + self.CHASE_FAR_YD * math.sin(s.facing)
        except Exception:
            return ""
        for x, y in ((ex, ey), (s.x, s.y)):
            try:
                if self.hazards.blocked(x, y, ("death_area",)):
                    return f"a deadly area at ({x:.0f},{y:.0f})"
            except Exception:
                pass
        try:
            pl = s.level or 1
            for d, sp in self.tm.sp.near(ex, ey, threat.AGGRO_MAX + 10.0):
                if sp.rank > 0 and d <= threat.aggro_radius(pl, sp.hi) + sp.wander:
                    return f"an elite's aggro (level {sp.hi} spawn {d:.0f} yd from it)"
        except Exception:
            pass
        if p is not None:
            el = threat.plates_beside(s.plates, p, self.W, FOCAL, pred=lambda q: threat.awake(q) and getattr(q, "elite", False))
            if el:
                return f"an elite beside it on screen (level {el[0].level})"
        return ""

    CHASE_STEP_YD = 8.0        # the chase's next stretch: checked for other spawns' aggro before it is walked

    def _chase_edge(self, s, p, tpt) -> str:
        """The next CHASE_STEP_YD yd towards the runner enter live spawns' aggro (other than the runner's own spawn):
        "N spawn(s)' aggro", else ""."""
        try:
            a = s.facing + (self.yaw_of(p) if p is not None and getattr(p, "sx", -1) >= 0 and not self.cam_off else 0.0)
            nx, ny = s.x + self.CHASE_STEP_YD * math.cos(a), s.y + self.CHASE_STEP_YD * math.sin(a)
            pl = s.level or 1
            here = {id(q) for q in self.tm.spot_risk(pl, s.x, s.y, pad=0.0)}   # already in reach where we stand
            pts = [q for q in self.tm.spot_risk(pl, nx, ny, pad=threat.ROUTE_PAD)
                   if id(q) not in here and not threat.is_gray(pl, q.hi)
                   and (tpt is None or math.hypot(q.x - tpt.x, q.y - tpt.y) > 1.0)]
        except Exception as e:
            swallowed("chase edge", e, log)
            return ""
        return f"{len(pts)} spawn(s)' aggro" if pts else ""

    def finisher_opts(self, s) -> list:
        """(cost, name) of the class's finishers (CLS.finishers) castable at the target now: on the bar, ready, in
        range and affordable."""
        bar = self.rot.bar(s)
        out = []
        for n in CLS.finishers:
            b = self.rot._can(s, bar, n, rng=True)
            if b and self.res.affordable(s, n):
                out.append((b[2] or 0, n))
        return out

    def fleeing_target(self, s, pulled, engaged, tpt, lb) -> bool:
        """The target runs at low hp (creatures flee at ~15-20% to the nearest friend within 30 yd). A runner that
        gets away comes back with friends (bot 2 11:15:43: let a 15% runner go, the next pull had maxatt=2), so it is
        CHASED, never let go: the cheapest affordable finisher in range and off cooldown (CLS.finishers), else an
        interact-walk (click-to-move with autoInteract; it starts the swing as it arrives) re-pressed
        every engage.CHASE_PRESS_S s. Over only when the chase makes no progress (the distance closing or its hp
        dropping: engage.Chase) for CHASE_GIVEUP_S s, or would enter a deadly area / an elite's aggro
        (_chase_hazard). Friends that join hand it back to the fight / flee rules. True: this pass is handled (the
        caller's approach() is skipped); "gone": the chase is over, the fight is left."""
        if not (pulled and engaged) or s.t_dead or s.t_on_me or s.t_within10 or not (0 < s.t_hp_pct <= self.FLEE_TARGET_HP):
            self._chase = None
            return False
        if not threat.THREAT_LIVE:
            return False                                       # the old chase: approach() follows it
        others = [q for q in self.attackers(s) if not getattr(q, "is_target", False)]
        if others:
            if getattr(self, "_chase", None) is not None:
                log(f"chase: {len(others)} joined while the target ran at {s.t_hp_pct:.0f}%; the fight rules take over")
            self._chase = None
            # the runner is not on us and nothing finishes it now: the ones hitting us first (bot 2 16:50:08-16
            # stayed 8 s on a 5% runner out of melee while 2 others took it from 69 to 26% hp; dead at 16:50:24)
            if not self.finisher_opts(s):
                log(f"fight: target runs at {s.t_hp_pct:.0f}% (not on us, no finisher castable now) while "
                    f"{len(others)} hit us: turning on them")
                return "switch"
            return False
        p = self.target_plate(s)
        ch = getattr(self, "_chase", None)
        if ch is None:
            try:
                help_n = self.tm.flee_help(tpt) if tpt is not None else 0
            except Exception:
                help_n = 0
            if not help_n and p is not None:
                help_n = len(threat.plates_beside(s.plates, p, self.W, FOCAL))
            ch = self._chase = engage.Chase(focal=FOCAL, clock=getattr(self, "clock", time.time))
            ch.help = help_n
            log(f"chase: target at {s.t_hp_pct:.0f}% runs ({help_n} friend(s) to run to): chasing")
        ch.update(s, p)
        if ch.stalled():
            log(f"chase: no progress for {ch.eg.idle():.0f}s (runner at {s.t_hp_pct:.0f}%, not closing, hp not "
                f"dropping); leaving it")
            self._chase = None
            return "gone"
        bar = self.rot.bar(s)
        opts = self.finisher_opts(s)
        if opts:
            _c, name = min(opts)
            log(f"chase: runner at {s.t_hp_pct:.0f}% in range: {name}")
            self.rot_cast(name)
            return True
        # only the WALK after it can enter a hazard: the finishers above are cast from here first (bot 4 L527:
        # 6 runners at 7-14% left to a deadly area 17:28-17:52 with Earth Shock never tried)
        why = self._chase_hazard(s, p, tpt)
        if why:
            log(f"chase: leaving the runner at {s.t_hp_pct:.0f}%: the chase would enter {why}")
            self._chase = None
            return "gone"
        edge = self._chase_edge(s, p, tpt)
        if edge:
            # the runner reached its friends' ground: walking on pulls them (bot 2 12:06:05, "3 friend(s) to run to",
            # walked after it, 3 on us at 12:06:11, dead 12:06:22). Casts from here only; out of range it is left.
            if not getattr(ch, "edge_said", False):
                ch.edge_said = True
                log(f"chase: not walking into {edge}; casting from here only")
            if not any(self.slot_of(n, s) and s.in_range(self.slot_of(n, s)) for n in CLS.finishers):
                log(f"chase: runner at {s.t_hp_pct:.0f}% out of range beside its friends; leaving it")
                self._chase = None
                return "gone"
            time.sleep(0.2)
            return True
        if getattr(ch, "help", 0):
            # nothing to finish it with and friends to run to: walking after it only takes us into them (bot 2 14:10:23
            # chased a 15% runner to 3 friends at 9% mana, 'Lightning Bolt never started', 4 on us, dead).
            # Only when the mana is not there: a finisher merely out of range or on cooldown is walked into range
            # (_chase_edge above still stops the walk at the friends' ground). Was any "no cast now": bot 4 16:23 let
            # two runners at 4% / 8% go at 48-51% mana (peer review 2026-10-15)
            # Closing in was tried (602e47c / f1e87c8) and taken out: each friend it walked to came as an add (bot 4
            # 16:32 into 5, dead; bot 3 16:58:42 one friend, then a 2nd chase, dead 16:59:32 in a known deadly area)
            mana_ok = [n for n in CLS.finishers if bar.get(n) and self.res.affordable(s, n)]
            log(f"chase: runner at {s.t_hp_pct:.0f}% heads for {ch.help} friend(s) and "
                + ("no finisher is castable now" if mana_ok else f"no finisher is affordable (mana {s.mana_pct:.0f}%)")
                + "; not walking after it")
            self._chase = None
            return "gone"
        if ch.press_due():
            if not ch.said:
                ch.said = True
                log(f"chase: interact-walk after the runner at {s.t_hp_pct:.0f}%")
            self.interact_walk()
        time.sleep(0.1)
        return True

    def _crumb(self, s):
        """Eye hook: remember where we walked out of combat (flee() runs back along it: that ground was clear)."""
        if s.combat or s.dead or s.ghost:
            return
        c = self.crumbs
        if not c or math.hypot(s.x - c[-1][0], s.y - c[-1][1]) > 5.0:
            c.append((s.x, s.y))

    FLEE_RUN_YD = 150.0        # yd a flee covers in its 25 s (scored this far along each route)
    FLEE_DIRS = 8              # straight-line alternatives to the trail / guards
    FLEE_TRAIL_BONUS, FLEE_GUARD_BONUS = 0.25, 0.5   # risk units: the way we came (walked, clear) / guards that fight
    DEATH_AREA_RISK = 3.0      # risk units per death_area a route enters
    # a straight flee line is walked without a path: it must be walkable on the mesh (bot 2 11:58:44 ran heading 45
    # into a wall 33 yd out at (-5803,-3072), wedged twice, dead at 11:59:04)
    FLEE_STEP, FLEE_MAX_DZ = 3.0, 3.6          # yd between floor samples / height change one step may have (~50 deg)
    FLEE_MIN_RUN = 40.0                        # yd: a shorter walkable line is no escape

    def _line_run(self, x0, y0, a, d) -> float:
        """Walkable yd along the straight line from (x0, y0) on bearing a (up to d): floor samples every FLEE_STEP yd,
        stopping at the first one off the mesh or too steep from the last. d when the mesh cannot tell."""
        nm = self.mv.nm
        z = nm.ground_z(x0, y0)
        if z is None:
            return d
        n = int(d / self.FLEE_STEP)
        for i in range(1, n + 1):
            zz = nm.ground_z(x0 + i * self.FLEE_STEP * math.cos(a), y0 + i * self.FLEE_STEP * math.sin(a),
                             hint=z, reach=2.0)
            if zz is None or abs(zz - z) > self.FLEE_MAX_DZ:
                return (i - 1) * self.FLEE_STEP
            z = zz
        return d

    def _trail_legs(self, x0, y0) -> list:
        """Back along our own trail (crumbs, newest first): ~15 yd legs, ending at the oldest crumb; [] without one."""
        legs, last = [], (x0, y0)
        for c in reversed(getattr(self, "crumbs", None) or ()):
            if math.hypot(c[0] - last[0], c[1] - last[1]) > 30:
                break                                              # a jump in the trail (revive, port): not walkable
            last = c
            if math.hypot(c[0] - x0, c[1] - y0) > 8:
                legs.append(c)
        return legs[2::3] + legs[-1:] if legs else []

    @staticmethod
    def _clip(x0, y0, legs, yd):
        """legs cut to the first `yd` yd of the walk from (x0, y0)."""
        out, d, last = [], 0.0, (x0, y0)
        for c in legs:
            step = math.hypot(c[0] - last[0], c[1] - last[1])
            if d + step > yd and step > 0:
                f = (yd - d) / step
                out.append((last[0] + f * (c[0] - last[0]), last[1] + f * (c[1] - last[1])))
                return out
            d += step
            out.append((c[0], c[1]))
            last = c
        return out

    def route_risk(self, pl, x0, y0, legs) -> tuple:
        """(risk, spawns) of a walk from (x0, y0) along legs, its first FLEE_RUN_YD yd: the live spawns' aggro it
        enters (threat.Model.path_risk, weighted), + DEATH_AREA_RISK per deadly area it reaches."""
        pts = [(x0, y0)] + self._clip(x0, y0, legs, self.FLEE_RUN_YD)
        sp = self.tm.path_risk(pl, pts, pad=threat.FLEE_PAD)
        risk = threat.risk_weight(pl, sp)
        try:
            areas = self.hazards.active(("death_area",))
        except Exception:
            areas = []
        for h in areas:
            if math.hypot(x0 - h.x, y0 - h.y) > h.r and any(
                    threat._seg_d(h.x, h.y, a[0], a[1], b[0], b[1]) <= h.r for a, b in zip(pts, pts[1:])):
                risk += self.DEATH_AREA_RISK
        # swimming halves the run and the chasers swim (bot 2 21:34 fled murlocs 30 yd in 15 s of water, dead; bot 4
        # 18:55 swam past a level 20 Threshadon): each yd of water on the way costs
        try:
            risk += self.FLEE_WATER_RISK * self._water_yd(pts) / 10.0
        except Exception as e:
            swallowed("bot.route_risk_water", e, log)
        return risk, sp

    FLEE_WATER_RISK = 1.0      # risk units per 10 yd of water a flee route crosses
    FLEE_WATER_STEP = 6.0      # yd between water samples

    def _water_yd(self, pts) -> float:
        """Yards of the walk along pts on water-only mesh polygons (NAV_WATER without NAV_GROUND)."""
        import navmesh
        nm = self.mv.nm
        wet = 0.0
        z = None
        for a, b in zip(pts, pts[1:]):
            d = math.hypot(b[0] - a[0], b[1] - a[1])
            n = max(1, int(d / self.FLEE_WATER_STEP))
            for i in range(1, n + 1):
                x, y = a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n
                gz = nm.ground_z(x, y, hint=z)
                if gz is None:
                    continue
                z = gz
                poly, _p = nm.nearest_poly(x, y, gz)
                if poly is None:
                    continue
                f = nm.tiles[poly[0]].pflags[poly[1]]
                if f & navmesh.NAV_WATER and not f & navmesh.NAV_GROUND:
                    wet += d / n
        return wet

    def flee_route(self, s, x0, y0) -> tuple:
        """-> (legs, kind, why) for flee(). Candidates: back along our trail (walked, clear when we came), guards within
        120 yd, and FLEE_DIRS straight lines of FLEE_RUN_YD yd; each scored by route_risk (live spawns' aggro on the
        way, deadly areas), the trail / guards with a bonus, the lines with their end's spawn density as the tie
        break. bot 2 11:37:42: the trail ran 118 yd from a 1162 camp into a level 12 1186 spawn's aggro, dead.
        Old choice (guards within 120, else the trail, else the nearest guard) when the spawn table fails."""
        pl = s.level or 1
        g = min(GUARDS, key=lambda q: math.hypot(q[0] - x0, q[1] - y0))
        trail = self._trail_legs(x0, y0)
        old = ([g], "guards") if math.hypot(g[0] - x0, g[1] - y0) <= 120 else ((trail, "trail") if trail else ([g], "guards"))
        cands = []
        if trail:
            cands.append((trail, "trail", -self.FLEE_TRAIL_BONUS))
        if math.hypot(g[0] - x0, g[1] - y0) <= 120:
            cands.append(([g], "guards", -self.FLEE_GUARD_BONUS))
        short = []                                   # (run, legs, kind): headings blocked before FLEE_MIN_RUN
        for k in range(self.FLEE_DIRS):
            a = 2 * math.pi * k / self.FLEE_DIRS
            try:
                run = self._line_run(x0, y0, a, self.FLEE_RUN_YD)
            except Exception as e:
                swallowed("flee line_run", e, log)
                run = self.FLEE_RUN_YD
            ex, ey = x0 + run * math.cos(a), y0 + run * math.sin(a)
            c = ([(ex, ey)], f"heading {math.degrees(a):.0f}" + (f" ({run:.0f} yd walkable)" if run < self.FLEE_RUN_YD else ""), 0.0)
            if run >= self.FLEE_MIN_RUN:
                cands.append(c)
            else:
                short.append((run, c))
        if short and len(short) == self.FLEE_DIRS and not cands:
            cands.append(max(short)[1])              # boxed in, no trail / guards: the longest line there is
        elif short:
            log(f"flee: {len(short)} heading(s) blocked within {self.FLEE_MIN_RUN:.0f} yd on the mesh")
        try:
            scored = []
            for legs, kind, bonus in cands:
                risk, sp = self.route_risk(pl, x0, y0, legs)
                end = self._clip(x0, y0, legs, self.FLEE_RUN_YD)[-1]
                dens = self.tm.density(pl, end[0], end[1]) if kind.startswith("heading") else 0
                scored.append((risk + bonus + 0.02 * dens, risk, len(sp), kind, legs))
        except Exception as e:
            log(f"flee: route scoring unavailable ({e!r}): {old[1]}")
            return old[0], old[1], "old rule"
        scored.sort(key=lambda r: r[0])
        best = scored[0]
        self.flee_risk = best[1]
        tr = next((r for r in scored if r[3] == "trail"), None)
        why = (f"risk {best[1]:.2f}, {best[2]} spawn(s)' aggro on it"
               + (f"; trail {tr[1]:.2f} ({tr[2]})" if tr and tr is not best else ""))
        return best[4], best[3], why

    FLEE_WEDGE_S, FLEE_WEDGE_YD = 4.0, 6.0   # a flee this close to its start after this long is wedged: fight on
    FLEE_FINISH_FIRST = (25.0, 60.0)          # target hp % at or under, our hp % at or over: kill it before a flee
    FLEE_MAX_COMBAT_S = 40.0   # s a flee may run while the combat flag stays up
    FLEE_ON_YD = 25.0          # yd per extra leg when the route ran out still chased
    FLEE_CLEAR_S = 3.0         # s of nothing on us, still running, before a flee is over
    FLEE_STAY_RISK = 1.0       # the best flee route still runs through a live spawn's aggro (risk units)
    FLEE_STAY_RATIO = 1.2      # need / capacity at or under which a risky route is not worth it: fight on
    FLEE_STAY_HP = 50.0        # % hp: with this much left an outmatched-but-close fight beats that run

    def flee_legs_clear(self, s, legs):
        """Flee legs are walked straight (no path plan mid-fight): bend them round the doodad and gameobject boxes the
        mesh lacks, as a planned path is (navmesh._avoid_doodads). Bot 4 20:44:13 fled down its trail into a wedge
        50 yd along, stuck twice, dead. Unchanged when that fails."""
        if not legs:
            return legs
        try:
            import navmesh
            z = s.z
            pts = []
            for lx, ly in legs:
                gz = self.mv.nm.ground_z(lx, ly, hint=z)
                z = gz if gz is not None else z
                pts.append((lx, ly, z))
            out = navmesh._avoid_doodads((s.x, s.y, s.z), pts)
            if len(out) != len(pts):
                log(f"flee: route bent round {len(out) - len(pts)} obstacle corner(s)")
            return [(p[0], p[1]) for p in out]
        except Exception as e:
            swallowed("bot.flee_legs_clear", e, log)
            return legs

    def flee(self, why, soft=False) -> str:
        """Break off a lost fight like a player: the class snare (CLS.snare, if on the bar) to slow them, then run back the way we
        came, to guards within 120 yd, or the straight line with the fewest live spawns' aggro on it (flee_route),
        until nothing is on us (creatures give up ~60 yd from home) or 25 s pass."""
        s = self.s()
        x0, y0, t0 = s.x, s.y, time.time()
        log(f"flee: {why}; hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}% at ({x0:.0f},{y0:.0f})")
        hp_f0, n_f0 = s.hp_pct, max(1, len(self.attackers(s)))
        if getattr(self, "_mid", None) is not None and self._mid.get("fled_at") is None:
            self._mid["fled_at"] = (time.time() - self._mid["t0"], s.hp_pct)
        self.flee_risk = 0.0
        legs, kind, rwhy = self.flee_route(s, x0, y0)
        # ... only when the fight is near even: a clearly lost one is run from even on a risky route (bot 2 18:05:24
        # "need 0.90 vs capacity 0.68", stayed for risk 2.00 at 75% hp, two fresh mobs, dead 18 s later)
        m = re.search(r"need ([\d.]+) vs capacity ([\d.]+)", why or "")
        ratio = (float(m.group(1)) / max(0.01, float(m.group(2)))) if m else 0.0
        if soft and self.flee_risk >= self.FLEE_STAY_RISK and s.hp_pct >= self.FLEE_STAY_HP                 and ratio <= self.FLEE_STAY_RATIO:
            # bot 4 12:22:37: "2 on us need 1.02 vs capacity 0.85" at 74% hp, the only way out (the trail) past a
            # spawn: ran 65 yd into more, dead at 12:22:57. A run through a camp is worse than this fight.
            log(f"flee: not running: the best way out ({kind}) still has risk {self.flee_risk:.2f} ({rwhy}); "
                f"fighting it out at {s.hp_pct:.0f}% hp")
            return "stay"
        legs = self.flee_legs_clear(s, legs)
        self.hazards.add("flee", x0, y0)          # and route around this spot afterwards (45 yd, 30 min)
        if CLS.snare and self.slot_of(CLS.snare, s):
            self.cast(CLS.snare)                  # slow the chasers
        log(f"flee: route {kind} ({rwhy})")

        clear = {"t": None}
        wedged = {"v": False}
        trail = deque(maxlen=200)                 # (t, x, y) of the run: progress over the last FLEE_WEDGE_S

        def done(st):
            # a flee that has not got FLEE_WEDGE_YD on in the last FLEE_WEDGE_S while still chased is stuck on something
            # the mesh does not know - near the start (bot 3 18:04:13-27: three stucks within 8 yd of a Westfall farm,
            # 84% -> 0 in 17 s) or anywhere on the way (Hunt 2026-10-17 23:07:26-42: ran 30 yd into a wall, two stucks
            # and a replan with the mob on it, 26% -> dead without a swing): fight
            now = time.time()
            trail.append((now, st.x, st.y))
            old = next((q for q in trail if now - q[0] <= self.FLEE_WEDGE_S), None)
            chased = st.combat or self.attackers(st)
            if now - t0 > self.FLEE_WEDGE_S and old is not None and now - old[0] >= self.FLEE_WEDGE_S - 0.5 \
                    and math.hypot(st.x - old[1], st.y - old[2]) < self.FLEE_WEDGE_YD and chased:
                wedged["v"] = True
                return True
            # "nothing on us" for FLEE_CLEAR_S running on, not one pass: while we run they are behind the camera
            # (bot 3 17:26:32 "clear after 8 s, 42 yd", npc 454 back on it 2 s later, dead 17:26:52)
            # 25 s, or 40 while the combat flag is still up (a chaser behind the camera: bot 4 20:36:45 stopped at
            # 25 s "clear" with combat 1 and no plate in view; bot 2 19:56:43 the same, dead 2 s later)
            if st.dead or time.time() - t0 > (self.FLEE_MAX_COMBAT_S if st.combat else 25):
                return True
            if st.combat or self.attackers(st):
                clear["t"] = None
                return False
            clear["t"] = clear["t"] or time.time()
            return time.time() - clear["t"] >= self.FLEE_CLEAR_S
        for lx, ly in legs:
            if done(self.s()):
                break
            self.mv.goto(lx, ly, None, tol=4, timeout=25, abort=done, direct=True)
        # the legs ran out while still chased: run on the same way (bot 3 19:40:41 "still chased after 6s, 25 yd",
        # stopped, fought at 40% against 3 seven seconds later, dead)
        px, py = (legs[-2] if len(legs) >= 2 else (x0, y0)) if legs else (x0, y0)
        for _ in range(7):
            st = self.s()
            if not legs or done(st) or wedged["v"]:
                break
            lx, ly = legs[-1]
            dx, dy = lx - px, ly - py
            n = math.hypot(dx, dy)
            if n < 1.0:
                dx, dy, n = st.x - x0, st.y - y0, max(1.0, math.hypot(st.x - x0, st.y - y0))
            nx, ny = st.x + dx / n * self.FLEE_ON_YD, st.y + dy / n * self.FLEE_ON_YD
            self.mv.goto(nx, ny, None, tol=4, timeout=10, abort=done, direct=True)
            px, py = st.x, st.y
            legs = [(nx, ny)]
        st = self.s()
        if wedged["v"] and not st.dead:
            log(f"flee: wedged {math.hypot(st.x - x0, st.y - y0):.0f} yd from the start after {time.time() - t0:.0f}s; "
                f"fighting on at {st.hp_pct:.0f}% hp")
            self.mv.stop()
            if not st.has_target or st.t_dead:
                self.pick_target()                # the one hitting us
            self.interact_walk(hold=1.0)          # the game turns us onto it and starts the swing
            return "stay"
        swallow_call(lambda: self.fbook.add(hp_f0, n_f0, "dead" if st.dead else "clear" if not (
            self.attackers(st) or st.combat) else "chased"))
        log(f"flee: {'dead' if st.dead else 'clear' if not (self.attackers(st) or st.combat) else 'still chased'} after "
            f"{time.time() - t0:.0f}s, {math.hypot(st.x - x0, st.y - y0):.0f} yd, hp {st.hp_pct:.0f}% (combat "
            f"{int(bool(st.combat))}, hostile plates {sum(1 for q in st.plates if q.hostile and not q.dead)}, "
            f"on screen {sum(1 for q in st.plates if q.hostile and not q.dead and q.sx >= 0)})")
        return "dead" if st.dead else "fled"

    def _spot_open(self, p, now):
        """Hunt point p worth walking to now: not visited <240 s ago, outside every skip zone, its spawn not dead
        for another 30+ s (tcf_characters.respawn)."""
        if self.visited.get((round(p[0]), round(p[1])), 0) >= now - 240:
            return False
        if self.hazards.blocked(p[0], p[1], HUNT_KINDS, now=now):
            return False
        if not economy.dead_known():
            return True
        g = p[3] if len(p) >= 5 else ((sp.guid if (sp := economy.at(p[0], p[1])) else None))
        return not g or economy.respawn_left(g, now) <= 30

    def packed(self, p, lvl, r=12.0):
        """Other hostile spawns (not gray to a level `lvl` character) within r yd of spawn point p. The server pulls
        them into the fight with it (worldserver.conf CreatureFamilyAssistanceRadius = 10, + 5 yd wander): inst3
        17:42:44 pulled a Seer at (-5037,-244) with crowd=2, 3 on us 7 s later, dead at 17:43:00.
        (threat.Spawns.packed: pool alternatives count once - the old copy counted each stacked pair twice)"""
        try:
            return self.tm.sp.packed(p[0], p[1], lvl, r)
        except Exception as e:
            if time.time() - getattr(self, "_packed_err_t", 0.0) > 300:
                self._packed_err_t = time.time()
                log(f"packed: spawn table unavailable: {e!r}")
            return 0

    def spawn_zones(self, s, reach=450.0, goal=None):
        """Spawn-table aggro zones (x, y, z, r) of hostile mobs above our level (or elite) within `reach`, for
        errand walks. Only the server-mesh fallback used them, so the bot-mesh route to Kharanos took Shamab (6)
        through four Snow Tracker Wolves (6-7) at (-5389,-471) (inst2 16:04:30-51). Mobs at our level stay out:
        we hunt those, and Coldridge would be one big zone."""
        lvl = s.level or 1
        if self._dz[0] != lvl:
            out, ez = [], []
            try:
                rows = [r for r in dangers._load() if r[0] == 0 and factions.is_hostile(DWARF_FACTION, r[9])]
                # camps of our own level too: a spawn with PACK_N+ non-gray neighbours within PACK_R yd pulls them
                # (threat.PACK_R / PACK_N)
                # along. The Frostmane road to the Hold trigger ran through the troll huts and 3 bots died there 8
                # times in 40 min (02:20-03:09, crowd=3, maxatt=3-4)
                grid: dict = {}
                for r in rows:
                    if r[7] > lvl - 3:
                        grid.setdefault((int(r[1] // threat.PACK_R), int(r[2] // threat.PACK_R)), []).append(r)
                for m, x, y, z, w, entry, lo, hi, rank, fac, det in rows:
                    if hi >= lvl + 1 or rank > 0:
                        out.append((x, y, z, max(det + (hi - lvl), 5.0) + w + 8.0))     # = dangers.zones() radius
                        if rank > 0 or hi >= lvl + ELITE_ZONE_LEVELS:
                            ez.append(out[-1][:3] + (out[-1][3] + (threat.ELITE_PAD if rank > 0 else 0.0),))
                    elif hi > lvl - 3:
                        gx, gy = int(x // threat.PACK_R), int(y // threat.PACK_R)
                        n = sum(1 for dx in (-1, 0, 1) for dy in (-1, 0, 1) for q in grid.get((gx + dx, gy + dy), ())
                                if 0.5 < math.hypot(q[1] - x, q[2] - y) < threat.PACK_R)
                        if n >= threat.PACK_N:
                            out.append((x, y, z, max(det, 5.0) + w + 6.0))
            except Exception as e:
                log(f"spawn zones unavailable: {e!r}")
            self._dz = (lvl, out)
            self._ez = ez
        if goal is not None:
            # the whole walk's box, not reach round the start: a 1337 yd walk to Mountaineer Barleybrew took inst4
            # and inst1 past the level 11-12 Backbreaker camp at (-5680,-2110), 730 yd from the start (05:53, 06:01)
            # padded with the walk's length: roads bend. Kharanos -> Loch Modan (2438 yd straight, 3630 walked) runs
            # through the Rockjaw quarry, 500+ yd west of the box; inst2 09:20 and inst4 09:25/09:35 died there, with
            # 1 danger zone in the plan. _path adds the zones the found path walks through from this wider set
            pad = max(150.0, 0.4 * math.hypot(goal[0] - s.x, goal[1] - s.y))
            x0, x1 = min(s.x, goal[0]) - pad, max(s.x, goal[0]) + pad
            y0, y1 = min(s.y, goal[1]) - pad, max(s.y, goal[1]) + pad
            return [z for z in self._dz[1] if x0 < z[0] < x1 and y0 < z[1] < y1]
        return [z for z in self._dz[1] if abs(z[0] - s.x) < reach and abs(z[1] - s.y) < reach]

    def elite_zones(self, s, reach=650.0):
        """The spawn_zones() of elites (every patrol point) and mobs ELITE_ZONE_LEVELS+ above us: a route cost on EVERY
        walk, the hunt walks too, not only errands. bot 2 12:49:51 roamed to its L541 grind point along Ol' Sooty's
        patrol (level 20 elite, (-5525,-3135) -> (-5588,-3155)) and died at 12:50:51, its 2nd death to it in 9 min."""
        self.spawn_zones(s)                                   # (re)builds the per-level cache
        return [z for z in getattr(self, "_ez", ()) if abs(z[0] - s.x) < reach and abs(z[1] - s.y) < reach]

    def travel_to(self, x, y, z, **kw):
        """mv.goto for errands (quest NPCs, vendor, trainer, quest items): the hazards' route costs plus the static
        spawn_zones() of the walk (one path: Hazards.cost_zones)."""
        fl = self.fly_leg(x, y, z, abort=kw.get("abort"))
        if fl == "abort":
            return "abort"
        base = self.mv.zones
        s = self.s()
        self.mv.zones = self.hazards.cost_zones(z=getattr(s, "z", 0.0) or 0.0, extra=self.spawn_zones(s, goal=(x, y)))
        try:
            return self.mv.goto(x, y, z, **kw)
        finally:
            self.mv.zones = base

    ROAM_FLY_YD = 800.0        # yd: a hunt point farther than this asks fly_leg first
    FLY_UP_S = 8.0             # s after the interact for the strip's taxi bit
    FLY_RETRY_S = 900          # s before a failed flight is tried again

    @staticmethod
    def char_name():
        """This instance's character (nb/login.py NAMES), None when unknown."""
        try:
            from nb import login
            return login.NAMES.get(inst.INST, (None,))[0]
        except Exception:
            return None

    FM_SAFE_R = 250.0          # yd round a flight master searched for spawns far above us
    FM_SAFE_LEVELS = 8         # a spawn this many levels over us there: the node is not used

    def fm_safe(self, x, y, pl) -> bool:
        """No hostile spawn FM_SAFE_LEVELS+ above us within FM_SAFE_R yd of (x, y) (a flight master's spot)."""
        try:
            return not any(p.lo >= pl + self.FM_SAFE_LEVELS for _d, p in self.tm.sp.near(x, y, self.FM_SAFE_R, live=False))
        except Exception:
            return True

    def fly_leg(self, x, y, z, abort=None, force=False):
        """A long errand leg by air (nb/flight.py plan: walk to the flight master of a known node, fly to the known
        node nearest the goal; the caller walks on from the landing). -> "ok" landed, "abort", None (walk it)."""
        if getattr(self, "_fly_busy", False):
            return None                       # the walk to the flight master itself
        s = self.s()
        if s.combat or s.dead or s.ghost or self.retry.blocked("fly"):
            return None
        try:
            fms = flight.flight_masters()
            known = flight.known_nodes(s.x, s.y, name=self.char_name())
            pl = flight.plan((s.x, s.y), (x, y), known, fms, force=force,
                             node_ok=lambda n: self.fm_safe(fms[n][2], fms[n][3], s.level or 1))
        except Exception as e:
            swallowed("bot.fly_leg plan", e, log)
            self.retry.set("fly", self.FLY_RETRY_S)
            return None
        if pl is None:
            return None
        import taxi
        a, b, save, why = pl
        entry, fm_name, fx, fy, fz = fms[a]
        dest = taxi._data().nodes[b]["name"].split(",")[0].strip()
        log(f"fly: {why}; {fm_name} -> {dest} saves {save:.0f}s")
        self._fly_busy = True
        try:
            r = self.travel_to(fx, fy, fz, tol=3, timeout=600, abort=abort)
            if r == "abort":
                return "abort"
            if not self.target_npc(entry, tries=3):
                log(f"fly: could not target {fm_name}; walking")
                self.retry.set("fly", self.FLY_RETRY_S)
                return None
            # chat lines before the window opens (never while an NPC window is open)
            self.keys.chat("/be want taxi")
            self.keys.chat(f"/be fly {dest.lower()}")
            up = False
            for _ in range(3):
                self.keys.press("m")
                dl = Deadline(self.FLY_UP_S)
                while not dl.passed():
                    time.sleep(0.3)
                    st = self.s()
                    if st.rxp is not None and st.rxp.taxi:
                        up = True
                        break
                if up:
                    break
            if not up:
                st = self.s()
                log(f"fly: not in the air after 3 talks to {fm_name} (addon: {(st.msg or '')[:80]!r}); walking")
                self.close_windows()
                self.retry.set("fly", self.FLY_RETRY_S)
                return None
            air = taxi.route_seconds(a, b, known) or 300.0
            log(f"fly: in the air to {dest} (~{air:.0f}s)")
            dl = Deadline(air * 1.5 + 120)
            while not dl.passed():
                time.sleep(1.0)
                st = self.s()
                if st.rxp is not None and not st.rxp.taxi:
                    break
            st = self.s()
            log(f"fly: landed at ({st.x:.0f},{st.y:.0f}), {math.hypot(x - st.x, y - st.y):.0f} yd from the goal")
            return "ok"
        finally:
            self._fly_busy = False
            with swallow("bot.fly_leg want", log):
                self.keys.chat("/be want")

    def weapon_step(self, s, lb, melee, sw, gh, pulled, eg, last_face) -> bool:
        """One _fight() pass for a class without a cast filler (after the rotation's instants): in range of an
        auto-repeating ranged attack (CLS.auto_ranged: Auto Shot) keep it running (the bar key toggles it, so it is
        pressed only while the strip's bit 20 reads it off); in melee the swing; else walk in to range. Stands and
        shoots - no kiting. True once the mob is engaged."""
        if lb and CLS.auto_ranged and s.in_range(lb):    # (a ranged attack reads out of range inside its minimum)
            if s.moving:
                self.mv.stop(); self.cancel_walk()
            if not s.autoshot and s.ready(lb) and time.time() >= getattr(self, "_shot_next", 0.0):
                tp = self.target_plate(s)
                if self.cam_off or tp is None or tp.sx < 0 or abs(self.yaw_of(tp)) > 0.6:
                    self.face_target()
                n0 = self.s().errn
                self.keys.press(SLOT_KEY[lb])
                self._shot_next = time.time() + 1.0
                time.sleep(0.25)
                s2 = self.s()
                if s2.errn != n0 and s2.err and not (s2.msg or "").startswith(("I:", "S:", "E:")):
                    log(f"{self.ranged} failed: {s2.msg}")
                    self.handle_err(s2.err)
                return True
            time.sleep(0.1)
            return pulled or s.autoshot
        if lb and CLS.auto_ranged and s.t_within10 and not pulled and not s.t_on_me and not s.combat \
                and time.time() - getattr(self, "_backoff_t", 0.0) > 6:
            # found inside the ranged attack's minimum range and not pulled yet: back-pedal out to range first (an
            # opener, not kiting: once it is on us we stand and melee)
            self._backoff_t = time.time()
            log(f"  opener: mob inside {self.ranged}'s minimum range, not pulled: stepping back to open from range")
            self.mv.stop(); self.cancel_walk()
            self.keys.press("s", hold=2.2)
            return False
        if s.t_within10:
            self.ensure_swing(s, melee, sw)
            self.maybe_reface(s, last_face)
            time.sleep(0.1)
            return True
        if pulled and s.t_on_me:
            time.sleep(0.15)                       # it is coming: the ranged attack's range is checked next pass
            if eg.come_wait(caster=False):
                self.approach()
            return pulled
        self.approach(opener=not pulled)
        return pulled

    def opener_hold(self, s, lb) -> bool:
        """approach(opener=True): stop in range of the ranged attack for the opener, whatever the mana (one cast's
        worth is enough) and also while it is not ready yet - wait in place instead of walking into melee (REPORT
        change 7: the human opened from range in 108 of 115 pulls; 30% of the bot's fights had the mob within 10 yd
        at the first cast). A ready flag that stays off for OPENER_WAIT s is not trusted: walk in as before."""
        if not lb or s.t_on_me or s.t_hp_pct <= 15 or not s.in_range(lb):
            return False
        b = self.rot.bar(s).get(self.ranged)
        if b and s.mana < b[2]:
            return False                        # not one cast's worth of mana: the weapon it is
        now = time.time()
        if s.ready(lb):
            self.hold_t0 = 0.0
            return True
        if not self.hold_t0:
            self.hold_t0 = now
            log(f"  approach: in {self.ranged} range, not ready: waiting here ({s.mana_pct:.0f}% mana)")
        return now - self.hold_t0 <= OPENER_WAIT

    def approach(self, max_t=1.5, force=False, opener=False, close=False):
        """Walk to the target: run with the stick and keep its nameplate in the
        middle of the screen (UnitPosition of a hostile is nil, so the plate is
        the only bearing we have). Stop in range of the class's ranged attack while
        it is still wanted, or in melee range. opener=True (a fight not pulled yet):
        always stop in its range and wait there for it (opener_hold). close=True: on to 10 yd whatever the ranged
        attack can reach (an item used on the mob: the Taming Rod's range is short of Auto Shot's)."""
        t = time.time()
        try:
            while time.time() - t < max_t:
                s = self.s()
                if not s.has_target or s.t_dead:
                    return
                lb = self.slot_of(self.ranged, s)
                if opener and self.opener_hold(s, lb):
                    self.cancel_walk()
                    time.sleep(0.1)                 # _fight() opens once it is ready
                    return
                want_ranged = lb and s.in_range(lb) and s.ready(lb) and (CLS.auto_ranged
                                                                         or s.mana_pct > 45 and not s.t_on_me)
                if close:
                    if s.t_within10:
                        self.cancel_walk()
                        return
                elif want_ranged or (not force and s.t_within10 and s.t_on_me):
                    self.cancel_walk()
                    return
                p = self.target_plate(s)
                if p is None:
                    # target beyond plate range (found by name): let the game walk us to it
                    if time.time() - self.m_t > 4:
                        self.interact_walk()
                        log("  approach: no plate, interact-walk")
                    time.sleep(0.15)
                    continue
                if p.sx < 0:
                    # plate off screen (behind us): swing the camera round to find it
                    if not self.pan_to_plate():
                        self.mv.run(False)
                        self.mv.turn(0.7)
                    continue
                err = -math.atan2(p.sx - self.W / 2, FOCAL)
                self.mv.drive(err)
        finally:
            self.mv.stop()

    def cancel_walk(self):
        """An interact-walk ('m') keeps running after mv.stop(): a tap on 's' ends it (a cast would fail while moving)."""
        if time.time() - self.m_t < 6:
            self.m_t = 0.0
            self.keys.press("s", hold=0.05)

    def _loot_score(self, s):
        """Things that change when a corpse is looted: copper, free slots, quest counters."""
        return (s.money, s.bags_free, sum(sum(q.counts) for q in s.quests))

    def target_corpse(self, c) -> bool:
        """Target the dead mob `c` (a Kill): last-target key first, then its plate."""
        for attempt in range(3):
            s = self.s()
            if s.has_target and not s.t_dead:
                self.keys.press("f11"); time.sleep(0.15)       # a live mob is not a corpse
            elif not s.has_target:
                self.keys.press("num-")                         # target last target
                time.sleep(0.35)
            s = self.s()
            if s.has_target and s.t_dead and (not c.ghash or s.t_hash == c.ghash):
                return True
            # wrong unit: try the corpse's own nameplate (hash matches the low 16 bits)
            p = next((p for p in s.plates if p.dead and c.ghash and p.ghash == (c.ghash & 0xFFFF)), None)
            if p is not None and p.token:
                self.keys.press(f"f{p.token}" if p.token <= 10 else f"num{p.token - 11}")
                time.sleep(0.35)
                s = self.s()
                if s.has_target and s.t_dead:
                    return True
            if s.has_target:
                self.keys.press("f11"); time.sleep(0.15)
        return False

    def loot_one(self, c, soft_ok=True) -> str:
        """Loot one corpse: target it, interact (walks up + opens), judge by what changed.
        soft_ok=False: an untargetable corpse is given up instead of tried with a soft interact (loot())."""
        soft = False
        c.soft = False
        if not self.target_corpse(c):
            # a corpse has no nameplate and /targetlasttarget holds one unit (after a 2-kill fight: the corpse
            # just looted). With no hard target the interact key takes the nearest still-LOOTABLE corpse
            # (SoftTargetInteract=3, range 12); looted corpses are no longer candidates
            s = self.s()
            if not c.x or math.hypot(s.x - c.x, s.y - c.y) > 12 or self.attackers(s):
                log(f"loot: corpse of npc {c.npc} not found")
                return "gone"
            if not soft_ok:
                log(f"loot: corpse of npc {c.npc} not targetable; no soft interact (fight {getattr(c, 'fight', 0)}: "
                    f"{'a corpse opened' if getattr(c, 'fight', 0) in self.fight_opened else 'soft tries used up'})")
                return "nosoft"
            if s.has_target:
                self.keys.press("f11"); time.sleep(0.15)
            t = time.time()
            while time.time() - t < 3 and math.hypot(s.x - c.x, s.y - c.y) > 3:   # back to where it died
                self.mv.drive(wrap(bearing(s.x, s.y, c.x, c.y) - s.facing))
                s = self.s()
            self.mv.stop()
            if getattr(c, "face", 0.0):
                self.mv.face_angle(c.face)          # the corpse lies ahead of where we faced when it died
            soft = c.soft = True
            log(f"loot: corpse of npc {c.npc} not targetable; soft interact")
        s = self.s()
        if s.t_tapped and not s.t_on_me:
            self.keys.press("f11")
            return "tapped"
        if not soft and s.t_dead and s.t_loot_known:
            # CanLootUnit of the targeted corpse (kills.note_loot keeps every dead-target sample): a corpse that
            # read dead for < LOOT_SETTLE s may still get its loot flag, so wait for a settled answer (<= 0.5 s)
            v, t = loot_verdict(self.loot_seen, s.t_hash), time.time()
            while v is None and time.time() - t < 0.5:
                time.sleep(0.1)
                s = self.s()
                v = loot_verdict(self.loot_seen, s.t_hash) if s.t_dead else True
            if v is False:
                self.keys.press("f11")          # the corpse holds nothing for us - no walk, no tries
                log(f"loot npc {c.npc}: nothing to loot (CanLootUnit)")
                return "nothing"
        if not soft and not s.t_within10:
            ln = self.res.lines(s)
            hp_l, mana_l = ln.pull_hp, ln.pull_mana
            far = not c.x or math.hypot(s.x - c.x, s.y - c.y) > LOOT_RISK_D
            low_res = not self.res.ready(s, ln)
            old_risky = far and low_res and self.pull_in_view(s)
            # the walk itself into a live spawn's aggro (threat.Model.loot_risk: the corpse ~20 yd along the way we
            # faced at the kill): inst3 10:12 / 10:20 walked 19-22 yd to a Stonesplinter corpse at 41-43% mana and
            # came back with the next camp member on us (att=1-2), dead both times; no plate was in view either time
            risk = []
            if low_res and c.x:
                try:
                    cx, cy = c.x + threat.LOOT_EST_D * math.cos(c.face or s.facing), \
                        c.y + threat.LOOT_EST_D * math.sin(c.face or s.facing)
                    risk = self.tm.loot_risk(s.level or 1, s.x, s.y, cx, cy, c.npc)
                except Exception:
                    risk = []
            if risk and not old_risky:
                log(f"threat: old=loot new=leave npc {c.npc}: the walk reaches {len(risk)} live spawn(s)' aggro "
                    f"at hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}%{'' if threat.THREAT_LIVE else ' [shadow]'}")
            # no bag slot: a walk only buys coins (bot 2 21:19:07 walked 31 yd at 56% hp with 0 free, pulled 2, dead);
            # and a corpse in a deadly area / by a recent death is not walked to either
            cpos = (c.x, c.y) if c.x else (s.x, s.y)
            quest_mob = c.npc in getattr(self, "wanted", ())        # its drop may be the step's: walked to anyway
            if getattr(s, "bags_free", 1) == 0 or (not quest_mob and self.hazards.blocked(cpos[0], cpos[1],
                                                                                         ("death", "death_area"))):
                self.keys.press("f11")
                log(f"loot npc {c.npc}: corpse out of reach and "
                    + ("no bag slot free" if getattr(s, "bags_free", 1) == 0 else "it lies where we died before")
                    + f" (hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}%): left")
                return "risky"
            if old_risky or (risk and threat.THREAT_LIVE):
                # a long walk into the next mob's range under the pull line: inst2 02:17:30 walked 41 yd to a
                # Seer's corpse at 31% mana, pulled the next Seer and died. The loot is worth less than a death
                self.keys.press("f11")
                log(f"loot npc {c.npc}: corpse {'%.0f+ yd off with a mob in view' % LOOT_RISK_D if old_risky else 'walk into %d spawn(s) aggro' % len(risk)} "
                    f"at hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}% (pull line {hp_l:.0f}/{mana_l:.0f}): left")
                return "risky"
            # killed from range: run up to the corpse first (interact only walks a few yards).
            # A corpse has no nameplate to steer by, so run along the way we faced when it died.
            t = time.time()
            x0, y0 = s.x, s.y
            at_spot = False
            way = "plate" if self.target_plate(s) is not None else "heading"
            while time.time() - t < 8:
                s = self.s()
                if not s.has_target or s.t_within10 or self.attackers(s):
                    break
                p = self.target_plate(s)
                if p is not None and p.sx >= 0:
                    self.mv.drive(-math.atan2(p.sx - self.W / 2, FOCAL))
                elif getattr(c, "face", 0.0) and math.hypot(s.x - x0, s.y - y0) < 40:
                    if not at_spot and math.hypot(s.x - c.x, s.y - c.y) > 3:
                        self.mv.drive(wrap(bearing(s.x, s.y, c.x, c.y) - s.facing))     # back to where it died
                    else:
                        at_spot = True
                        self.mv.drive(wrap(c.face - s.facing))
                else:
                    break
            self.mv.stop()
            s = self.s()
            log(f"loot: walked to corpse of npc {c.npc} via {way}: within10={s.t_within10} moved={math.hypot(s.x - x0, s.y - y0):.0f} yd")
        tries = getattr(c, "tries", 1)
        if s.t_within10 and tries > 1 and getattr(c, "face", 0.0):
            # within 10 yd but the loot window will not open: the loot range is shorter - step closer
            t = time.time()
            while time.time() - t < 0.45 * (tries - 1):
                s = self.s()
                if self.attackers(s):
                    break
                self.mv.drive(wrap(c.face - s.facing))
            self.mv.stop()
            s = self.s()
        # CanLootUnit of the targeted corpse: True keeps the old 1.6 s wait for the window (and allows one retry in
        # loot()); unknown / no flag: LOOT_OPEN_WAIT s standing without a window and we move on
        has_loot = loot_verdict(self.loot_seen, s.t_hash) if (not soft and s.has_target and s.t_dead) else None
        c.has_loot = has_loot
        wait_open = 1.6 if has_loot is True else LOOT_OPEN_WAIT
        before, n0 = self._loot_score(s), s.errn
        self.keys.press("m")                    # interact: walk to the corpse + open it; autoloot takes it
        t = time.time()
        opened = False
        while time.time() - t < 6:
            time.sleep(0.15)
            s = self.s()
            if s.ui_open(4):
                opened = True
            if s.errn != n0 and s.err in (6, 12, 14):
                break                            # full bags / too far / nothing to loot
            if opened and self._loot_score(s) != before:
                break                            # autoloot already took it
            el = time.time() - t
            if not s.moving and ((opened and el > 1.0) or (not opened and el > wait_open)):
                break                            # standing (the interact walk is over) and nothing more to come
        time.sleep(0.3)
        s = self.s()
        after = self._loot_score(s)
        if s.ui_open(4):
            opened = True                        # a window that came late still counts (no Esc: autoloot empties
            #                                      and closes it; an Esc closed inst4's with the loot still in it)
        self.keys.press("f11")
        res = ("looted" if after != before else "empty" if opened else
               {6: "full", 12: "toofar", 14: "gone"}.get(s.err if s.errn != n0 else 0, "noopen"))
        log(f"loot npc {c.npc}: {res} window={opened} money {before[0]}->{after[0]} bags {before[1]}->{after[1]} "
            f"quests {before[2]}->{after[2]} err={s.err if s.errn != n0 else 0}")
        return res

    def loot(self):
        """Work through the corpses we killed (newest first); stop if something attacks.
        CanLootUnit decides first: a corpse whose dead-target sample said 'nothing for us' (kills.note_loot, read
        whenever it was the target) is dropped without a walk. Corpses of one fight are judged together: once one
        opened, a neighbour that opened nothing is not tried again, and the soft interact (no hard target) gets at
        most one retry per fight (kills.soft_allowed / retry_loot: 161 attempts that opened nothing on 2026-10-14)."""
        try:
            self._loot()
        finally:
            self.loot_done_t = time.time()   # chain_ready(): the next mob in view is pulled before buffs / vendor

    def _loot(self):
        now = time.time()
        self.corpses, dup = fresh_corpses(self.corpses, now)              # Kill.t is wall-clock
        if dup:
            log(f"loot: {dup} duplicate corpse record(s) dropped")
        keep = self.fight_no - 30                                         # the fight memo only needs recent fights
        self.fight_opened = {f for f in self.fight_opened if f > keep}
        self.fight_soft_fail = {f: n for f, n in self.fight_soft_fail.items() if f > keep}
        skipped = False
        while self.corpses:
            s = self.s()
            if self.attackers(s) or os.path.exists(STOP):
                return
            c = self.corpses.pop()
            if self.noloot.count(c.npc) >= 3 and c.npc not in self.loot_ok and c.npc not in self.wanted:
                continue                     # this kind never opened a loot window: skip it
            fid = getattr(c, "fight", 0)
            if loot_verdict(self.loot_seen, c.ghash) is False:
                log(f"loot npc {c.npc}: nothing to loot (CanLootUnit when it was targeted dead)")
                skipped = True
                continue
            tries = getattr(c, "tries", 0) + 1
            c.tries = tries
            res = self.loot_one(c, soft_ok=soft_allowed(fid, self.fight_opened, self.fight_soft_fail))
            if res in ("looted", "empty"):
                self.fight_opened.add(fid)
            if res == "noopen" and getattr(c, "soft", False):
                self.fight_soft_fail[fid] = self.fight_soft_fail.get(fid, 0) + 1
            if res == "looted":
                self.loot_ok.add(c.npc)
            elif res in ("noopen", "empty") and c.npc not in self.loot_ok:
                self.noloot.bump(c.npc)
            has_loot = getattr(c, "has_loot", None)
            if retry_loot(res, tries, fid, self.fight_opened, has_loot):
                self.corpses.append(c)
            elif res == "noopen" and tries < 2:
                log(f"loot npc {c.npc}: no retry ("
                    + (f"another corpse of fight {fid} opened" if fid in self.fight_opened else
                       "CanLootUnit unknown" if has_loot is None else "CanLootUnit: nothing for us") + ")")
        if skipped:
            s = self.s()
            if s.has_target and s.t_dead and not self.attackers(s):
                self.keys.press("f11")       # a skipped corpse left targeted would bring the main loop straight back

    # ------------------------------------------------------------ rest
    LINGER_MAX_S = 20.0        # s of 'combat flag, nothing on us' before we look round for an unseen attacker

    def lingering_combat(self, s) -> bool:
        """The combat flag is up but nothing fights us - no attacker plate, the target not on us, hp not falling - and
        hp / mana are under rest_line()'s pull line: the main loop rests (rest(linger=True)) instead of picking
        whatever is in view (a mob tanking our totem, a neighbour) as a fresh pull. A falling hp means an unseen
        attacker: then this is real combat (find_attacker)."""
        now = time.time()
        prev = getattr(self, "_linger_hp", None)
        self._linger_hp = (now, s.hp_pct)
        if self.attackers(s) or (s.has_target and s.t_on_me and not s.t_dead):
            self._linger_since = 0.0
            return False
        if prev is None or now - prev[0] > 3.0:
            # not called for a while: the loop was fighting / walking, not lingering - the "nothing seen" clock starts
            # now (bot 4 16:23:49 "37 s with nothing seen" right after a fight 16:23:30-49; peer review 2026-10-15)
            self._linger_since = 0.0
        if prev and now - prev[0] < 3.0 and s.hp_pct < prev[1] - 2:
            return False
        # hp lost inside a lingering rest (rest() notes it; its heals hid the drop from the check above), or the flag
        # up LINGER_MAX_S with nothing seen: an attacker off screen - look for it (bot 2 13:44:48-13:45:47 'rested'
        # 60 s while a 1161 behind it hit it from 100 to 78% again and again, then fought it from 31% hp / 1% mana)
        if now - getattr(self, "_unseen_t", 0.0) < 15.0:
            return False
        since = getattr(self, "_linger_since", 0.0) or now
        self._linger_since = since
        if now - since > self.LINGER_MAX_S:
            self._linger_since = 0.0
            log(f"combat flag up {now - since:.0f}s with nothing seen on us: looking round for an attacker")
            return False
        ln = self.res.lines(s)
        hp, mana = ln.pull_hp, ln.pull_mana
        if self.res.ready(s, ln):
            return False
        if time.time() - getattr(self, "_linger_log_t", 0.0) > 20:
            self._linger_log_t = time.time()
            log(f"combat flag up, nothing on us, hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}% under the pull line "
                f"({hp:.0f}/{mana:.0f}): resting, no fresh pull")
        return True

    def rest(self, linger=False):
        """Heal with the class's heal (a few seconds) instead of sitting; only sit to
        drink when mana is really low, or to eat when we cannot heal. linger: the combat flag is up with nothing on us
        (lingering_combat): rest anyway, standing - the standing loop's 8 s grace decides how long."""
        s = self.s()
        if s.combat and not linger:
            return
        for _ in range(3):
            if s.hp_pct >= 75 or not CLS.heal or not self.res.rest_heal_ok(s):   # (mana for 1.5 heals)
                break
            self.keys.press("f11"); time.sleep(0.1)
            if not self.cast(CLS.heal):
                break
            s = self.s()
            if (s.combat and not linger) or any(p.on_me for p in s.plates):
                return
        ln = self.res.lines(s)
        rest_hp, rest_mana, boss = ln.pull_hp, ln.pull_mana, ln.boss
        multi = self.last_multi
        exit_hp, exit_mana = self.rest_exit(ln, multi)
        need = getattr(self, "rest_need", None)  # a look-ahead rest (forecast_walk): stand up only at what it needs
        if need:
            exit_hp, exit_mana = max(exit_hp, need[0]), max(exit_mana, need[1])
        drink = self.res.drink_line(s)          # sit to drink only under a heal's worth (20% at most; 0 without mana)
        if self.res.ready(s, ln) and not need:
            if self.walk_rest:
                log(f"rest: walked it off in {time.time() - self.walk_rest:.0f}s (hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}%)")
            self.walk_rest = 0.0
            self.last_multi = False
            return              # fights start with resources: all run-4 deaths began a chain at low hp / mana
        # between the drink line and the rest line, hp fine, nothing on us: walk on, mana comes back while moving
        # (roam stops WALK_REST_STOP yd short of the next spawn, the main loop waits when a pull is in view)
        # ... but not in a cave or beside a pack: walking on there pulls (bot 1 22:17:34 walked it off at 62% mana in
        # the Grizzled Den, a Wendigo came, two more joined, dead at 22:17:56 - its 3rd den death in 15 min)
        from nb.move import INDOOR_FLAG
        fl = getattr(s, "flags", None)
        indoors_now = isinstance(fl, int) and bool(fl & INDOOR_FLAG)
        # beside a pack (threat.REST_PLATES_N awake plates in view), or mob country by the spawn table, not only by
        # the plates in view (the camera shows one way): inst4 02:47:55 walked off 49% mana in the Frostmane camp
        # with no plate in view, a level 9 whelp came at 62% and 2 more joined; the 4th death on that road in 40 min
        stand = (len(threat.awake_plates(getattr(s, "plates", ()))) >= threat.REST_PLATES_N
                 or self.packed((s.x, s.y), s.level, r=threat.REST_DENSE_R) >= threat.REST_DENSE_N)
        # a small top-up (under REST_TOPUP % short of the pull line) is walked off even beside a pack, indoors or
        # after a 2+ mob fight: it refills in a few seconds of walking (2026-10-15 11:00-14:04: 39 / 81 / 16 / 20
        # standing rests began at most 15 points short; bot 1 11:04:15 stood for 82% -> 92% mana at full hp)
        topup = max(rest_hp - s.hp_pct, rest_mana - s.mana_pct) < self.REST_TOPUP
        held = [w for w, c in (("after a 2+ mob fight", multi), ("beside a pack", stand), ("indoors", indoors_now))
                if c]
        # (a top-up overrides only the 2+ mob fight: beside a pack or indoors walking on still pulls - the Grizzled Den
        # and Frostmane deaths above began 13 points short)
        # a big mana gap is not walked off: whatever the walk meets is fought half empty (bot 2 18:57:05 walked off
        # 38% mana vs a 90 line, a Defias came 9 s later, a second one at 8% mana, dead at 18:57:52)
        gap_ok = rest_mana - s.mana_pct <= self.WALK_REST_GAP
        if (s.mana_pct >= drink and s.hp_pct >= WALK_REST_HP and not boss and not linger and gap_ok and not need
                and not self.attackers(s) and (not held or (topup and not stand and not indoors_now))):
            if not self.walk_rest:
                self.walk_rest = time.time()
                log(f"rest: walking it off: hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}% (pull line hp {rest_hp:.0f} "
                    f"mana {rest_mana:.0f})" + (f", a small top-up ({held[0]})" if held else ""))
            return
        self.walk_rest = 0.0
        # the pending self-buff first: cast after the rest, Lightning Shield (~10% mana) put us back under the pull
        # line and a second rest followed (51 / 48 / 22 / 35 shield casts right after a rest's end, 11:00-14:04)
        if not linger and self.rest_buffs():
            s = self.s()
            if s.combat or self.attackers(s):
                return
        # spawns' aggro reach this spot: sit somewhere safer - for a real rest only (bot 2 12:41:30 walked 60 yd at
        # 100% hp / 88% mana for a 2% top-up, into a wedge and a level 20 elite)
        short = max(exit_hp - s.hp_pct, exit_mana - s.mana_pct) < self.REST_MOVE_MIN
        if not linger and not short and self.rest_move(s):
            s = self.s()
            if s.combat or self.attackers(s):
                return
        awake = sum(1 for q in s.plates if q.hostile and not q.dead and not q.player and not q.trivial and q.sx >= 0)
        why = ("after a 2+ mob fight" if multi else "boss next" if boss else "lingering combat" if linger
               else "hp under the walk line" if s.hp_pct < WALK_REST_HP else "mana under the drink line"
               if s.mana_pct < drink else held[0] if held else "standing")
        log(f"rest: hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}% hostile_plates={awake} "
            f"({why}, until hp {exit_hp:.0f} mana {exit_mana:.0f})")
        self.last_multi = False
        drinks = eats = 0
        drink_t = eat_t = 0.0
        hp0 = s.hp_pct
        dl = Deadline(45)                         # the standing rest's cap
        while not dl.passed():
            s = self.s()
            if os.path.exists(STOP):
                return
            if s.combat:
                # the combat flag outlives the kill by a few seconds: with nothing on us that is no fight, keep resting
                # (inst2 2026-10-15 09:10:20: rest at hp 36% left at once, walked on and died to the next mob)
                # (linger: the main loop found nothing on us; a mob that has us on its list while it hits our totem
                # is not a reason to stand up)
                # an hp drop under the flag is an unseen attacker: up and find it (find_attacker)
                hp0 = max(hp0, s.hp_pct)          # (a heal raises the mark)
                if (dl.elapsed() < 8 and not self.attackers(s) and s.hp_pct >= hp0 - 3
                        and (linger or not any(q.threat and not q.dead for q in s.plates))):
                    time.sleep(0.5)
                    continue
                if s.hp_pct < hp0 - 3 and not self.attackers(s):
                    self._unseen_t = time.time()          # lingering_combat: an unseen attacker, go and find it
                    log(f"rest: hp {hp0:.0f} -> {s.hp_pct:.0f}% under the combat flag with nothing seen: an attacker "
                        f"off screen")
                return
            if s.hp_pct >= exit_hp and s.mana_pct >= exit_mana:
                break
            # heal inside the loop: drinking alone left hp at 41% when the next mob came (22:00)
            if CLS.heal and s.hp_pct < 75 and self.res.rest_heal_ok(s) and self.rot.heal_next <= time.time():
                self.keys.press("f11"); time.sleep(0.1)
                if self.cast(CLS.heal):
                    drink_t = eat_t = 0.0      # the cast stood us up: allow a new drink / meal
                    continue
            # drink / eat at once, the best item in the bags (BotEye points the keys at it); again when the aura is
            # gone and we are still short (at most REST_CONSUME_N of each per rest)
            now = time.time()
            if (drinks < self.REST_CONSUME_N and now - drink_t > 3 and not getattr(s, "drinking", False)
                    and self.drink_due(s, exit_mana, drink) and self.consume("drink", s)):
                drinks, drink_t = drinks + 1, time.time()
                continue
            if (eats < self.REST_CONSUME_N and now - eat_t > 3 and not getattr(s, "eating", False)
                    and self.eat_due(s, exit_hp) and self.consume("food", s)):
                eats, eat_t = eats + 1, time.time()
                continue
            time.sleep(0.5)
        # stand up
        self.keys.press("w", 0.05)

    REST_TOPUP = 10.0                  # % short of the pull line: walked off wherever we are (no standing rest)
    DRINK_SHORT = 20.0                 # % mana short of the rest's end that is worth a drink (always under the drink line)
    EAT_SHORT = 15.0                   # % hp short of the rest's end that is worth a meal (when no heal does it)
    REST_CONSUME_N = 2                 # drinks / meals per standing rest at most

    def rest_exit(self, ln, multi=False):
        """(hp, mana) a standing rest stands up at: the pull line; after a 2+ mob fight or with a boss next the rest
        target (the pull line + EXIT_MARGIN). The margin made every rest run on 5 points past the line it needed."""
        if multi or ln.boss:
            return ln.exit_hp, ln.exit_mana
        return ln.pull_hp, ln.pull_mana

    def drink_due(self, s, exit_mana, drink_line) -> bool:
        """Sitting: drink when under the drink line, or when the mana is DRINK_SHORT short of the rest's end."""
        return s.mana_pct < exit_mana and (s.mana_pct < drink_line or exit_mana - s.mana_pct >= self.DRINK_SHORT)

    def eat_due(self, s, exit_hp) -> bool:
        """Sitting: eat when EAT_SHORT hp short of the rest's end and a heal does not do it (heal, don't eat: the
        rest heals under 75% while the mana holds 1.5 heals)."""
        return exit_hp - s.hp_pct >= self.EAT_SHORT and not (s.hp_pct < 75 and self.res.rest_heal_ok(s))

    def consume(self, kind, s) -> bool:
        """Drink / eat the best item in the bags (the F12 / num. keys: BotEye build 13 points them at it; an older
        BotEye: the fixed level-1 items, seen on the action bar only). -> pressed."""
        has = stock.have(s, kind)
        if has is None:
            has = self.has_item(159 if kind == "drink" else 4540, s)
        if not has:
            return False
        self.keys.press("f12" if kind == "drink" else "num.")
        item = getattr(s, "drink_id" if kind == "drink" else "food_id", 0)
        n = getattr(s, "drinks" if kind == "drink" else "foods", 0)
        aura = "drinking" if kind == "drink" else "eating"
        dl, on = Deadline(1.5), False
        while not dl.passed():
            time.sleep(0.25)
            st = self.s()
            if getattr(st, aura, False):
                on = True
                break
            if st.combat or self.attackers(st):
                break
        log(f"rest: {aura} item {item or '?'} ({n} in the bags) at hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}%"
            + ("" if on else f" - no {'Drink' if kind == 'drink' else 'Food'} aura seen"))
        return True

    def rest_buffs(self) -> bool:
        """Before a standing rest: cast the missing out-of-combat buff (buffs()) so the rest covers its mana."""
        try:
            return bool(self.buffs())
        except Exception as e:
            swallowed("rest buffs", e, log)
            return False

    REST_MOVE_R = (20.0, 40.0, 60.0)   # yd: rings searched for a safer rest spot
    REST_MOVE_MIN = 15.0               # % hp / mana short of the rest's end: less is topped up where we stand
    REST_MOVE_DIRS = 12
    REST_TRAIL_BONUS = 0.3             # risk units: our own trail was walked and found clear

    def rest_spot(self, s):
        """A safer place to sit: -> (x, y, kind, here, there) or None. Here = the live spawns whose aggro (+ wander +
        threat.REST_PAD) covers our spot (threat.Model.spot_risk); none: rest here. Else the candidate - our trail
        within 60 yd, rings of 20 / 40 / 60 yd - with the least weight there (x2), plus the aggro the walk there
        enters (path_risk), the trail with a bonus, distance as the tie break; not inside a death / flee / deadly
        area. bot 3 11:38:20: 'rest: hp 71% mana 15% hostile_plates=5', a kobold spawn 6 yd off, aggroed 3 s later;
        bot 4 11:10:10: 'hostile_plates=4' 12 yd from two 10 yd wanderers (1195, a level 12 1186), dead."""
        pl = s.level or 1
        try:
            here = self.tm.spot_risk(pl, s.x, s.y)
        except Exception:
            return None
        if not here:
            return None
        hw = threat.risk_weight(pl, here)
        cands = []
        for c in reversed(getattr(self, "crumbs", None) or ()):
            d = math.hypot(c[0] - s.x, c[1] - s.y)
            if d > self.REST_MOVE_R[-1]:
                break
            if d >= 8:
                cands.append((c[0], c[1], "trail"))
        for r in self.REST_MOVE_R:
            for k in range(self.REST_MOVE_DIRS):
                a = 2 * math.pi * k / self.REST_MOVE_DIRS
                cands.append((s.x + r * math.cos(a), s.y + r * math.sin(a), "ring"))
        best = None
        for x, y, kind in cands:
            if self.hazards.blocked(x, y, ("death", "flee", "death_area")):
                continue
            there = self.tm.spot_risk(pl, x, y)
            tw = threat.risk_weight(pl, there)
            if tw >= hw:
                continue
            pw = threat.risk_weight(pl, self.tm.path_risk(pl, [(s.x, s.y), (x, y)]))
            sc = 2 * tw + pw - (self.REST_TRAIL_BONUS if kind == "trail" else 0.0) + math.hypot(x - s.x, y - s.y) / 100
            if best is None or sc < best[0]:
                best = (sc, x, y, kind, here, there)
        return best[1:] if best else None

    def rest_move(self, s) -> bool:
        """Before a standing rest: walk to rest_spot() when spawns' aggro reach this one. -> walked."""
        sp = self.rest_spot(s)
        if sp is None:
            try:
                n = len(self.tm.spot_risk(s.level or 1, s.x, s.y))
            except Exception:
                n = 0
            if n:
                log(f"rest: {n} spawn(s)' aggro reach this spot, nothing better within {self.REST_MOVE_R[-1]:.0f} yd; "
                    f"resting here")
            return False
        x, y, kind, here, there = sp
        pl = s.level or 1
        log(f"rest: {len(here)} spawn(s)' aggro reach this spot (weight {threat.risk_weight(pl, here):.2f}); moving "
            f"{math.hypot(x - s.x, y - s.y):.0f} yd to ({x:.0f},{y:.0f}) ({kind}, {len(there)} there) first")
        # the walk itself, as the mesh routes it, must not cross other spawns' aggro (bot 2 17:47:54: a 60 yd "ring"
        # move, jumped on the way at 11% mana by a level 15 Harvest Watcher, dead 17:48:20): else rest here
        try:
            pts = self.mv.path(x, y, None)
            if pts:
                cross = [p for p in self.tm.path_risk(pl, [(s.x, s.y)] + [tuple(q[:2]) for q in pts])
                         if p not in here]
                if cross:
                    log(f"rest: the walk there crosses {len(cross)} other spawn(s)' aggro (weight "
                        f"{threat.risk_weight(pl, cross):.2f}); resting here")
                    return False
        except Exception as e:
            swallowed("bot.rest_move path", e, log)
        hp0 = s.hp_pct
        r = self.mv.goto(x, y, None, tol=3, timeout=20,
                         abort=lambda st: st.dead or st.combat or bool(self.attackers(st)) or st.hp_pct < hp0 - 3)
        log(f"rest: move -> {r}")
        return True

    def boss_next(self, s) -> bool:
        """A burst quest boss (BOSS_NPCS) we want is on the plates: the next pull wants full hp / mana."""
        return any(p.npc in BOSS_NPCS and p.npc in self.wanted and not p.dead for p in s.plates)

    def rest_line(self, s):
        """-> (hp, mana) a pull starts from, (hp, mana) a standing rest goes on to, and whether a boss is next: the
        lines of Bot.res (nb/resources.py: K x the p90 one-mob kill cost + a heal, the defaults until 5 fights at
        this level are measured; after a 2+ mob fight the full flat line; a boss next: BOSS_*)."""
        ln = self.res.lines(s)
        return ln.pull_hp, ln.pull_mana, ln.exit_hp, ln.exit_mana, ln.boss

    def ready_to_pull(self, s) -> bool:
        """False only during a walked rest that has not reached the pull line yet."""
        if not self.walk_rest or time.time() - self.pull_anyway_t < 60:
            return True
        return self.res.ready(s)

    def chain_ready(self, s) -> bool:
        """Pull the next mob straight after a loot, before rest / imbue / buffs / training / vendor: within
        CHAIN_PULL_S s of loot(), nothing on us, hp and mana at or above rest_line()'s pull line (the existing rest
        thresholds; after a 2+ mob fight or with a boss next that line is higher) and a mob pick_target() takes on
        screen. The human targets the next mob 2.3 s after a kill, the bot took 8 s (REPORT.md change 6)."""
        t = getattr(self, "loot_done_t", 0.0)
        if not t or time.time() - t > CHAIN_PULL_S or s.combat or self.attackers(s):
            return False
        ln = self.res.lines(s)
        if ln.boss or not self.res.ready(s, ln):
            return False
        return self.pull_in_view(s)

    def pull_in_view(self, s) -> bool:
        """A mob pick_target() would take is on screen (nameplates show to ~41 yd), or a quest mob at any sx."""
        return bool(self.wanted_plates(s)) or any(self.valid_plate(p, s) and p.sx >= 0 for p in s.plates)

    def wait_ready(self, why) -> bool:
        """The end of a walked rest: stand here, short of the next pull, until hp / mana reach the pull line
        (at most WALK_REST_WAIT s; anything attacking ends it)."""
        s = self.s()
        if self.ready_to_pull(s):
            return True
        self.mv.stop()
        ln = self.res.lines(s)
        mana = ln.pull_mana
        dl, m0 = Deadline(WALK_REST_WAIT), s.mana_pct
        ok = drank = False
        while not dl.passed() and not os.path.exists(STOP):
            s = self.s()
            if s.dead or s.combat or self.attackers(s):
                break
            if self.res.ready(s, ln):
                ok = True
                break
            # a long wait (DRINK_SHORT under the line): a drink shortens it (63 / 47 / 35 / 100 such waits, 11:00-14:04)
            if not drank and mana - s.mana_pct >= self.DRINK_SHORT and self.consume("drink", s):
                drank = True
                continue
            time.sleep(0.5)
        if drank and not (s.dead or s.combat):
            self.keys.press("w", 0.05)            # stand up from the drink
        attacked = s.combat or bool(self.attackers(s))
        log(f"rest: {why}: waited {dl.elapsed():.1f}s, mana {m0:.0f}->{s.mana_pct:.0f}% (line {mana:.0f}) "
            f"hp {s.hp_pct:.0f}% -> {'ready' if ok else 'attacked' if attacked else 'pulling anyway'}")
        if ok:
            self.walk_rest = 0.0
        elif not attacked:
            self.pull_anyway_t = time.time()      # waited long enough: no second wait before this pull
        return ok

    def has_item(self, entry, s) -> bool:
        return any(a >= 8388608 and a - 8388608 == entry for a in s.actions)

    # ------------------------------------------------------------ death
    def wait_res_sickness(self):
        """Sit out Resurrection Sickness by the healer (75% less damage and health for 1 min per level over 10): inst3
        2026-10-15 07:32:41 revived at level 11, walked out at 1% mana and died again at 07:33:21."""
        import nb.rxp_follow as rf
        time.sleep(1.0)
        s = self.s()
        if not getattr(s, "res_sick", False):
            log(f"resurrection sickness: none shown (level {s.level}, auras {s.auras & 255})")
            return
        log("resurrection sickness: sitting it out by the spirit healer")
        t0 = time.time()
        self.keys.press(rf.SIT_KEY)
        while time.time() - t0 < 660:
            time.sleep(2.0)
            s = self.s()
            if not s.res_sick or s.combat or s.dead or os.path.exists(STOP):
                break
        log(f"resurrection sickness: {'gone' if not s.res_sick else 'still on'} after {time.time() - t0:.0f}s")

    WALK_REST_GAP = 30.0       # mana points under the pull line that may still be walked off
    GHOST_SPEED = 7.0          # yd/s a ghost covers on a corpse run (run speed + the ghost bonus, detours included)
    CORPSE_OVERHEAD_S = 45.0   # s: revive-spot pick, the look for patrols, the reclaim
    CAMPED_N, CAMPED_S, CAMPED_R = 2, 900.0, 60.0    # our deaths at one spot in this long: the corpse is camped

    def revive_choice(self, s, lvl):
        """('healer' | 'corpse', why). The healer costs Resurrection Sickness from level 11 (1 min per level over 10, at
        most 10) and 25% durability; the corpse run costs the walk. Every death took the healer (it was tried first and
        is nearly always within 400 yd): bot 4 sat 6 x 4 min of sickness at Thelsamar 16:32-18:56, its corpses 100-600
        yd off. The healer stays for a camped corpse (CAMPED_N deaths within CAMPED_R yd in CAMPED_S s) and for a run
        longer than the sickness."""
        sick = 0.0 if lvl <= 10 else 60.0 * min(10, lvl - 10)
        if sick <= 0:
            return "healer", f"level {lvl}: no resurrection sickness"
        # a reclaim that already failed (the delay grows with repeated deaths): bot 4 19:44-19:58 ran the corpse loop
        # six times, "could not retrieve corpse" each 2.7 min, 22 min a ghost beside the healer
        if getattr(self, "_corpse_fails", 0) >= 1:
            return "healer", f"the corpse reclaim failed {self._corpse_fails} time(s)"
        cx, cy = self.death_pos
        run = math.hypot(s.x - cx, s.y - cy) * 1.35 / self.GHOST_SPEED + self.CORPSE_OVERHEAD_S
        now = time.time()
        recent = [h for h in self.hazards.active(("death",), mine_only=True)
                  if math.hypot(h.x - cx, h.y - cy) < self.CAMPED_R
                  and h.until - hz.KINDS["death"][1] > now - self.CAMPED_S]
        if len(recent) >= self.CAMPED_N:
            return "healer", f"{len(recent)} deaths here in {self.CAMPED_S / 60:.0f} min: the corpse is camped"
        # a corpse in a deadly area (deaths of any bot clustered there): a revive there is a fight at revive hp in the
        # same camp (bot 4 19:27-19:30 ran back into the Stonesplinter cluster, revived, died again 3 min later)
        da = [h for h in self.hazards.active(("death_area",)) if math.hypot(h.x - cx, h.y - cy) < h.r]
        if da:
            return "healer", f"the corpse lies in a deadly area ({da[0].info.get('n')} deaths round "                              f"({da[0].x:.0f},{da[0].y:.0f}))"
        if run > sick:
            return "healer", f"corpse run ~{run:.0f}s > {sick:.0f}s of sickness"
        return "corpse", f"corpse run ~{run:.0f}s vs {sick:.0f}s of sickness + 25% durability"

    HEARTH_KEY = "f9"          # BotEye 20 BotEyeHearth: /use Hearthstone
    HEARTH_CAST_S = 10.0

    def camped_deaths(self, cx, cy) -> int:
        """Our deaths within CAMPED_R yd of (cx, cy) in the last CAMPED_S s (revive_choice's camped test)."""
        now = time.time()
        return sum(1 for h in self.hazards.active(("death",), mine_only=True)
                   if math.hypot(h.x - cx, h.y - cy) < self.CAMPED_R and h.until - hz.KINDS["death"][1] > now - self.CAMPED_S)

    def hearth_out(self, why) -> bool:
        """Revived where we keep dying: hearth home before anything finds us (out of combat, the 10 s cast).
        -> True when we moved 300+ yd."""
        s = self.s()
        if s.combat or self.attackers(s):
            log(f"hearth: not now ({why}): in combat")
            return False
        x0, y0 = s.x, s.y
        log(f"hearth: {why}; casting the Hearthstone")
        self.keys.press(self.HEARTH_KEY)
        dl = Deadline(self.HEARTH_CAST_S + 6)
        while not dl.passed():
            time.sleep(0.5)
            st = self.s()
            if math.hypot(st.x - x0, st.y - y0) > 300:
                log(f"hearth: home at ({st.x:.0f},{st.y:.0f})")
                return True
            if st.dead or st.combat:
                break
        st = self.s()
        log(f"hearth: did not take us home (combat {int(bool(st.combat))}, err {st.err}: {(st.msg or '')[:40]!r})")
        return False

    CORPSE_TRUST_YD = 1500.0   # a game corpse position farther than this from our guess is a misread

    def corpse_from_game(self):
        """(x, y) of our corpse from the game (BotEye 19 /be corpse), None when it does not say. The API's world pos
        may come in either axis order: the order nearer our own guess (death_pos) is taken, within CORPSE_TRUST_YD."""
        try:
            m0 = self.s().msgn
            self._chat_quiet("/be corpse")
            dl = Deadline(2.5)
            while not dl.passed():
                time.sleep(0.25)
                st = self.s()
                if st.msgn != m0 and (st.msg or "").startswith("I:corpse"):
                    mm = re.match(r"I:corpse (-?[\d.]+) (-?[\d.]+)", st.msg)
                    if not mm:
                        log(f"ghost: the game does not say where the corpse is ({st.msg!r})")
                        return None
                    a, b = float(mm.group(1)), float(mm.group(2))
                    gx, gy = self.death_pos or (st.x, st.y)
                    c = min(((a, b), (b, a)), key=lambda p: math.hypot(p[0] - gx, p[1] - gy))
                    return c if math.hypot(c[0] - gx, c[1] - gy) <= self.CORPSE_TRUST_YD else None
        except Exception as ex:
            swallowed("bot.corpse_from_game", ex, log)
        return None

    def spirit_healer_revive(self, s) -> bool:
        """Walk to the nearest Spirit Healer and take "Return me to life" (no resurrection
        sickness below level 11): faster and safer than running back to a corpse that
        monsters may be camping."""
        import services
        sh = [n for n in services.all_npcs() if n["map"] == 0 and "spirithealer" in n["services"]]
        if not sh:
            return False
        n = min(sh, key=lambda n: math.dist(n["pos"][:2], (s.x, s.y)))
        x, y, z = n["pos"]
        log(f"ghost: spirit healer at ({x:.0f},{y:.0f}), {math.dist((x, y), (s.x, s.y)):.0f} yd")
        if math.dist((x, y), (s.x, s.y)) > 400:
            return False
        # the server takes the interact only within the healer's reach + 4 yd (Player::GetNPCIfCanInteractWith);
        # tol=6 left the ghost ~7 yd off (inst3 2026-10-14 18:03:29 stuck at (-6165.6,341.4), 7.1 yd from the healer)
        r = self.mv.goto(x, y, z, tol=2.5, timeout=120)
        for i in range(6):
            s = self.s()
            if s.ui_open(5) and not s.ui_open(0):
                self.keys.press("esc")          # a StaticPopup left open (inst4 ui=8224 17:58-18:13: no NPC window opened)
                time.sleep(0.3)
            self.keys.press("num+")             # /targetexact Spirit Healer (BotEye NUMPADPLUS)
            time.sleep(0.4)
            s = self.s()
            m0, e0 = s.msgn, s.errn
            self.keys.press("m")                # BotEye takes the gossip option; CONFIRM_XP_LOSS -> ConfirmationInteraction(18)
            t = time.time()
            while time.time() - t < 4.0:
                time.sleep(0.25)
                s = self.s()
                if not s.ghost:
                    log(f"revived at the spirit healer (press {i + 1})")
                    self._corpse_fails = 0
                    self.close_windows()
                    return True
            msg = s.msg if s.msgn != m0 else ""
            log(f"  healer press {i + 1}: walk={r} d={math.dist((x, y), (s.x, s.y)):.1f} target={s.t_npc} ui={s.ui} "
                f"err={s.err if s.errn != e0 else 0} msg={msg!r}")
            if s.ui_open(0):                    # gossip up but nothing picked: run the addon's gossip step again
                self._chat_quiet("/be gossip")
                time.sleep(2.5)
                if not self.s().ghost:
                    log("revived at the spirit healer (/be gossip)")
                    self.close_windows()
                    return True
            elif i == 1 and math.dist((x, y), (s.x, s.y)) > 3.0:
                r = self.mv.goto(x, y, z, tol=1.5, timeout=20)
        log("spirit healer did not revive us")
        return False

    def _chat_quiet(self, line):
        """A local /be command; a failure (window gone, not in world) is logged, not raised."""
        try:
            self.keys.chat(line)
        except Exception as e:
            log(f"chat {line!r} failed: {e!r}")

    def revive_spot(self, cx, cy, lvl):
        """Where to take the corpse back: a mesh point 22-34 yd from it (reclaim range 39) as far as possible from the
        hostile spawns round it, nearer to us on ties; the corpse itself when no point is clearer.
        (death-loop diagnosis 2026-10-14: inst3 revived ON the corpse at 35% hp among 3-4 whelps, 18:05:16 / 18:07:22)"""
        try:
            near = [(q.x, q.y) for _d, q in self.tm.sp.near(cx, cy, 115.0, live=False)
                    if q.hi > lvl - 5 and abs(q.x - cx) < 80 and abs(q.y - cy) < 80]
        except Exception:
            near = []
        if not near:
            return cx, cy
        me = self.s()

        def clear(px, py):
            return min(math.hypot(hx - px, hy - py) for hx, hy in near)
        best, score = (cx, cy), clear(cx, cy)
        for rr in (34.0, 28.0, 22.0):
            for k in range(16):
                a = k * math.pi / 8
                px, py = cx + rr * math.cos(a), cy + rr * math.sin(a)
                try:
                    if self.mv.nm.ground_z(px, py) is None:
                        continue
                except Exception:
                    continue
                sc = clear(px, py) - 0.02 * math.hypot(px - me.x, py - me.y)
                if sc > score:
                    best, score = (px, py), sc
        return best

    def on_death(self):
        s = self.s()
        dpath = inst.path("death.json")
        if not s.ghost:                                  # a fresh corpse: remember where it lies (survives restarts)
            self.death_pos = (s.x, s.y)
            self.hazards.add("death", s.x, s.y)          # whatever killed us lives here (shared: nb/hazards.json)
            json.dump(list(self.death_pos), open(dpath, "w"))
            log(f"died at ({s.x:.0f},{s.y:.0f})")
        elif self.death_pos is None:
            # the strip already shows a ghost: the best guess for the corpse is where the last fight ran
            # (fight() keeps last_pos / death.json fresh), not whatever death.json held from an older death
            lp = self.last_pos
            if lp and time.time() - lp[2] < 120:
                self.death_pos = (lp[0], lp[1])
            else:
                try:
                    if time.time() - os.path.getmtime(dpath) < 300:
                        self.death_pos = tuple(json.load(open(dpath)))
                    else:
                        self.death_pos = (s.x, s.y)
                except Exception:
                    self.death_pos = (s.x, s.y)
            self.hazards.add("death", self.death_pos[0], self.death_pos[1])
            log(f"ghost; corpse at ({self.death_pos[0]:.0f},{self.death_pos[1]:.0f})")
        if not s.ghost:
            time.sleep(2)
            self.keys.press("home")                      # (the addon releases by itself too)
            time.sleep(6)
            s = self.s()
            if not s.ghost:
                log("release did not happen")
                return
        gc = self.corpse_from_game()
        if gc is not None:
            if math.hypot(gc[0] - self.death_pos[0], gc[1] - self.death_pos[1]) > 5:
                log(f"ghost: the game puts the corpse at ({gc[0]:.0f},{gc[1]:.0f}), "
                    f"{math.hypot(gc[0] - self.death_pos[0], gc[1] - self.death_pos[1]):.0f} yd from our guess")
            self.death_pos = gc
            swallow_call(lambda: json.dump(list(gc), open(dpath, "w")))
        lvl = s.level or 1
        how, why = self.revive_choice(s, lvl)
        log(f"ghost: {how} ({why})")
        if how == "healer" and self.spirit_healer_revive(s):
            cx, cy = self.death_pos
            self.death_pos = None                        # next death starts from a fresh position
            n = self.camped_deaths(cx, cy)
            if n >= self.CAMPED_N:
                self.hearth_out(f"{n} deaths here in {self.CAMPED_S / 60:.0f} min")
            self.wait_res_sickness()
            return
        log("ghost: running back")
        cx, cy = self.death_pos
        # Take the corpse back like a player: from a spot inside reclaim range away from the spawns that killed us,
        # after a look for patrols. BotEye's death ticker takes the corpse as soon as we are in range and the delay is
        # over, i.e. on the corpse (inst3 18:05:16 / 18:07:22: revived on it at 35% hp, 3-4 whelps on us): hold it.
        # (An addon without /be hold answers "I:unknown": then it is the old run, only via the spot.)
        self._chat_quiet("/be hold on")
        try:
            spot = self.revive_spot(cx, cy, lvl)
            log(f"ghost: revive spot ({spot[0]:.0f},{spot[1]:.0f}), {math.hypot(spot[0] - cx, spot[1] - cy):.0f} yd "
                f"from the corpse")
            # the run from the graveyard can outlast the addon's 150 s hold: renew it on nearing reclaim range (39 yd)
            self.mv.goto(spot[0], spot[1], tol=3, timeout=400, abort=lambda st: math.hypot(st.x - cx, st.y - cy) < 55)
            self._chat_quiet("/be hold on")
            # inside reclaim range already: a wedge short of the spot is not walked for 2 min (bot 4 19:27:26-19:29,
            # 6 stucks 26 yd from its corpse, on a mesh edge)
            self.mv.goto(spot[0], spot[1], tol=3, timeout=45)
            s = self.s()
            if math.hypot(s.x - cx, s.y - cy) > 36:      # spot not reached: the corpse, as before
                self.mv.goto(cx, cy, tol=5, timeout=120)
                s = self.s()
            self.mv.face_angle(bearing(s.x, s.y, cx, cy))    # look at the camp: its plates are what we wait out
            t0, n = time.time(), 0
            while time.time() - t0 < 30:
                n = sum(1 for p in self.s().plates if p.hostile and not p.dead and not p.trivial)
                if n == 0:
                    break
                time.sleep(1.0)
            log(f"ghost: taking the corpse at ({s.x:.0f},{s.y:.0f}) after {time.time() - t0:.0f}s, hostile plates {n}")
        finally:
            self._chat_quiet("/be hold off")             # the ticker retrieves within 2 s once the delay is over
        t0 = time.time()
        while time.time() - t0 < 150:                    # the reclaim delay grows with repeated deaths
            time.sleep(1.0)
            s = self.s()
            if not s.ghost:
                log(f"revived {math.hypot(s.x - cx, s.y - cy):.0f} yd from the corpse")
                self.death_pos = None
                self._corpse_fails = 0
                time.sleep(1)
                return
            if int(time.time() - t0) % 4 == 0:
                self.keys.press("end")
            if time.time() - t0 > 40 and math.hypot(s.x - cx, s.y - cy) > 30:
                # the corpse guess may be off, or reclaim reaches less far here: close in, but only to 18 yd on our
                # side, not onto it (inst4 00:37:12-37: walked onto the corpse, revived among 3 mobs at 5% hp, died)
                d = math.hypot(s.x - cx, s.y - cy)
                k = 18.0 / d
                self.mv.goto(cx + (s.x - cx) * k, cy + (s.y - cy) * k, tol=4, timeout=60)
        self._corpse_fails = getattr(self, "_corpse_fails", 0) + 1
        log(f"could not retrieve corpse ({self._corpse_fails} in a row; the spirit healer next)")

    # ------------------------------------------------------------ roaming
    def hunt_points(self, me):
        """Grind spawns: the server's own spawn table (tcf_world, levels from creature_classic_level), mobs whose
        top level is level-GRIND_BAND .. our level, no elites / rares / quest bosses. Each point is (x, y, z, guid, xp):
        roam() scores it by expected xp per second and skips dead spawns (tcf_characters.respawn).
        Falls back to the 1.12 table (old band) when the server DB cannot be read."""
        try:
            sp = economy.spawns()
        except Exception as e:
            if not getattr(self, "_hp_warned", False):
                self._hp_warned = True
                log(f"hunt points: server spawn table unavailable ({e!r}); using the 1.12 table")
            sp = None
        if sp is not None:
            pts, far = [], []
            local = self.local_only(me)
            for p in sp:
                if p.rank or p.entry in BOSS_NPCS or p.name.startswith("[") or factions.is_friendly(DWARF_FACTION, p.faction):
                    continue
                if p.hi > me.level or p.hi < me.level - GRIND_BAND:
                    continue     # never a spawn that can roll above us (run 4: level 8s for a level 7 fed the deaths)
                if local and not self.in_home((p.x, p.y)):
                    continue
                xp = economy.spawn_xp(me.level, p.lo, p.hi)
                if xp <= 0:
                    continue
                d = math.hypot(p.x - me.x, p.y - me.y)
                (far if not local and d > 600 else pts).append((p.x, p.y, p.z, p.guid, xp, d))
            if not pts and far:
                # none within 600 yd (the end of 12-14 Loch Modan is an Ironforge vendor: inst1 stood there an hour on
                # "no hunt points; waiting"): the nearest HUNT_FAR_N of them, wherever they are
                far.sort(key=lambda p: p[5])
                pts = far[:HUNT_FAR_N]
                if getattr(self, "_far_log", None) != (round(pts[0][0]), round(pts[0][1])):
                    self._far_log = (round(pts[0][0]), round(pts[0][1]))
                    log(f"hunt points: none within 600 yd; the nearest {len(pts)} start {pts[0][5]:.0f} yd away")
            return [p[:5] for p in pts]
        pts = []
        for m, x, y, z, w, entry, lo, hi, rank, fac, det in dangers._load():
            if m != 0 or rank or factions.is_friendly(DWARF_FACTION, fac):
                continue
            if hi > me.level + (1 if me.level < 5 else 0) or hi < me.level - (1 if me.level >= 6 else 4):    # low mobs give 15 xp: skip them from level 6
                continue
            if not self.local_only(me) and math.hypot(x - me.x, y - me.y) > 600:
                continue
            if self.local_only(me) and not self.in_home((x, y)):
                continue
            pts.append((x, y, z))
        return pts

    def local_only(self, s) -> bool:
        return s.level <= LOCAL_MAX_LEVEL

    def in_home(self, p) -> bool:
        return math.hypot(p[0] - HOME[0], p[1] - HOME[1]) <= HOME_R

    def indoors(self, p) -> bool:
        """Is this spawn point inside a building or cave (vmap box test)? Cached."""
        key = (round(p[0]), round(p[1]))
        c = self._indoor_cache
        if key not in c:
            try:
                import vmap
                c[key] = bool(vmap.inside_building(p[0], p[1], p[2], map_id=0))
            except Exception:
                c[key] = False
        return c[key]

    # ------------------------------------------------------------ search mode (quest mob not in sight)
    def search_here(self, x, y):
        """Arrived at a hunt point with a quest errand and nothing to fight: look for the mob.
        Order: plates we already have (any sx), a camera sweep, then /target by name.
        Returns "plate" (a wanted plate exists: pick_target engages it), "name" (it is targeted: fight it),
        "combat", "skip" (this spot was searched <120 s ago), "far" (the named mob is beyond plate range: roam()
        moves on to another point) or None (nothing found)."""
        now = time.time()
        key = (round(x / 20), round(y / 20))
        if self.searched.get(key, 0) > now - 120:
            return "skip"
        self.searched[key] = now
        s = self.s()
        if s.combat or self.attackers(s):
            return "combat"
        self.mv.stop()
        t0 = time.time()
        log(f"arrive ({x:.0f},{y:.0f}): plates={len(s.plates)} wanted={len(self.wanted_plates(s))} "
            f"offscreen={sum(1 for p in s.plates if p.sx < 0)} pad={int(bool(self.mv.padding()))}")
        how = "plate" if self.wanted_plates(s) else None
        if not how and SEARCH_SWEEP:
            how = self.sweep()
        if not how and SEARCH_NAME and not os.path.exists(STOP):
            how = {"hit": "name", "far": "far"}.get(self.name_target() or "")
        log(f"search ({x:.0f},{y:.0f}): {how or 'nothing'} in {time.time() - t0:.1f}s")
        return how

    def sweep(self):
        """Swing the camera once round (right stick, standing still) until a wanted plate is on screen.
        Needs the pad (game window in front). Self-calibrating: the plates' own yaw shift between reads
        gives the real camera rate, which feeds pan_gain."""
        mv = self.mv
        if not mv.padding():
            return None
        need = 2 * math.pi - 2 * math.atan(self.W / 2 / FOCAL) + 0.2    # the screen already covers 2*atan(W/2/FOCAL)
        d = self.sweep_sign = -self.sweep_sign                           # alternate direction per arrival
        t0 = last = time.time()
        swept, prev, gains, found = 0.0, {}, [], None
        cap = 1.5 * need / SWEEP_RATE + 1.0
        try:
            while swept < need and time.time() - t0 < cap and not os.path.exists(STOP):
                el = time.time() - t0
                if not mv.pan(d * SWEEP_RATE / self.pan_gain * min(1.0, 0.35 + el / 0.5)):
                    break                                               # lost focus
                time.sleep(0.03)
                s = self.s()
                if s.dead or s.combat or self.attackers(s) or self.far_stronger(s):
                    break
                vis = [p for p in self.wanted_plates(s) if p.sx >= 0]
                if vis:
                    found = min(vis, key=lambda p: abs(p.sx - self.W / 2))
                    break
                now = time.time()
                dt, last = now - last, now
                cur = {p.ghash: self.yaw_of(p) for p in s.plates if p.sx >= 0}
                meas = sorted((prev[g] - yw) * d for g, yw in cur.items() if g in prev)
                step = meas[len(meas) // 2] if meas else SWEEP_RATE * dt
                if meas and dt > 0 and el > 0.6:                        # not while the stick is still ramping up
                    gains.append(step / dt)
                swept += max(0.0, step)
                prev = cur
        finally:
            mv.aim_done()
        if gains:
            g = sorted(gains)[len(gains) // 2]
            self.pan_gain = min(2.5, max(0.4, 0.5 * self.pan_gain + 0.5 * g / SWEEP_RATE * self.pan_gain))
            log(f"  sweep: measured {g:.2f} rad/s (commanded {SWEEP_RATE}), pan_gain={self.pan_gain:.2f}")
        if found:
            self.cam_off = True
            time.sleep(0.3)          # plate-key macros refresh every 0.25 s (BotEye.lua): a fresh plate's key can be stale
            return "plate"
        mv.nudge(0.12)               # realign the character with the camera
        return None

    def name_target(self):
        """/target the wanted mob by name (reaches units beyond nameplate range). The names go to the BotEyeFind
        macro with one '/be find A;B' chat line (only when they change); NUMPADMULTIPLY then runs it.
        Returns "hit" (target is a living, wanted, attackable mob), "corpse", "far" (no nameplate: beyond plate
        range), "reject" or None (no unit)."""
        if self.rxp_mode:
            # RXP mode: BotEye fills BotEyeFind from RXP's mob list itself (out of combat, RXP_PLAN.md section 4
            # "Keys"), so no '/be find' chat line; nothing to look for while the list is empty
            r = getattr(self.s(), "rxp", None)
            if r is None or not r.n_mobs:
                return None
        else:
            names = sorted({str(n) for m in self.wanted if (n := self.qdb.names.get(m))})[:3]
            if not names:
                return None
            if names != self.find_names:
                self.keys.chat("/be find " + ";".join(names))
                self.find_names = names
                time.sleep(0.4)
        self.keys.press("f11"); time.sleep(0.15)
        self.keys.press("num*"); time.sleep(0.35)
        s = self.s()
        if not s.has_target:
            log("  name-target: no unit")
            return None
        pl = self.target_plate(s)
        if s.t_dead:
            log("  name-target: nearest same-name unit is a corpse")
            self.keys.press("f11")
            return "corpse"
        now = time.time()
        top = s.level + (1 if s.hp_pct >= 95 and s.mana_pct >= 95 else 0)    # as valid_plate (wanted mobs only here)
        if pl is None:
            # no nameplate: it is beyond ~40 yd. Walking blind to it led the bot 100 yd into other mobs (died 21:50)
            log(f"  name-target: npc {s.t_npc} is out of plate range; ignoring it")
            self.keys.press("f11")
            return "far"
        if s.t_npc in BOSS_NPCS and not self.boss_ready(s, pl):
            log(f"  name-target: boss npc {s.t_npc} not yet (hp {s.hp_pct:.0f} mana {s.mana_pct:.0f}); rest / clear adds first")
            self.keys.press("f11")
            return "reject"
        if self.rxp_mode and getattr(s, "t_rxp_mob", False) and s.t_npc:
            self.rxp_ids.add(s.t_npc)                  # an RXP mob-list name: its npc id is wanted from now on
            self.wanted.add(s.t_npc)
        if (s.t_npc in self.wanted and s.t_attackable and not s.t_tapped and not s.t_player
                and s.t_level <= top and not self.blacklist.blocked(s.t_hash, now)
                and not self.blacklist.blocked(s.t_hash & 0xFFFF, now)):
            log(f"  name-target: HIT npc {s.t_npc} lvl {s.t_level} plate={'none' if pl is None else ('off' if pl.sx < 0 else 'on')}")
            return "hit"
        self.keys.press("f11")
        return "reject"

    def roam(self, pts=None, indoor_ok=False):
        s = self.s()
        if pts is None and self.rxp_mode:
            # no spawn table (rxp_follow.DB_MOBS): grinding roams the current RestedXP step's points
            r = getattr(s, "rxp", None)
            pts = self._rxp_points(r, s) if r is not None else []
            if r is not None and len([p for p in pts if not self.indoors(p)]) < self.RING_MIN:
                pts = pts + self._rxp_ring(r, s)          # too few to roam (or indoors): rings round the step's area
        pts = pts if pts is not None else self.hunt_points(s)
        if not indoor_ok:
            pts = [p for p in pts if not self.indoors(p)]    # never hunt inside caves / buildings
        now = time.time()
        pk = {}                                        # (x, y) key -> spawns packed round it (threat.Spawns.packed)
        for p in pts:
            pk[(round(p[0]), round(p[1]))] = self.packed(p, s.level)
        if pts:
            # a lone spawn is pulled alone; a packed one brings its neighbours - quest hunt or grind alike (inst3 died
            # 8 times in the Stonesplinter camps 10:05-10:55, the last ones grinding an RXP xp step). Only while a lone
            # one is open: 1 lone of 17 bear points (317) kept inst3/inst4 camping (-6070,-261) 8-14 min (01:16-01:30)
            lone = [p for p in pts if pk[(round(p[0]), round(p[1]))] == 0]
            if lone and len(lone) < len(pts) and any(self._spot_open(p, now) for p in lone):
                pts = lone
        # our avoid circles, the other bots' recent deaths (inst4 09:25 / 09:35 at the Rockjaw quarry, where inst2
        # had died at 09:20) and the deadly areas repeated deaths there make (hazards ROAM_KINDS: death_area)
        if pts:
            ok = self.hazards.outside(pts, hz.ROAM_KINDS, now=now)
            if ok:
                if len(ok) < len(pts):
                    areas = self.hazards.active(("death_area",), now=now)
                    if areas and getattr(self, "_area_log", None) != len(areas):
                        self._area_log = len(areas)
                        log(f"roam: {len(pts) - len(ok)} hunt point(s) inside avoid / death areas left out ("
                            + ", ".join(f"{h.info.get('n')} deaths round ({h.x:.0f},{h.y:.0f}) r {h.r:.0f}"
                                        for h in areas) + ")")
                pts = ok
        # hunt-point loop (REPORT.md change 2): points round a spot that just gave nothing are left alone a while
        first = self.hazards.first_end(HUNT_KINDS, now=now)
        if first is not None and pts:
            ok = self.hazards.outside(pts, HUNT_KINDS, now=now)
            if not ok:
                # every point is round a spot that gave nothing: stand and wait for the zone that ends first
                # (the old loop re-walked the point underfoot every 0.5 s)
                if getattr(self, "_skip_log", None) != first:
                    self._skip_log = first
                    log(f"roam: all {len(pts)} hunt point(s) inside a skip zone; the first ends in {first - now:.0f}s")
                t = time.time()
                while time.time() - t < min(3.0, max(0.5, first - now)) and not os.path.exists(STOP):
                    st = self.s()
                    if st.dead or st.combat or self.attackers(st) or self.pull_in_view(st):
                        break                        # the main loop takes it from here
                    time.sleep(0.5)
                return
            pts = ok
        # another bot hunts there (nb/claims.json): somewhere else while there is somewhere else. 15:53-16:01 level 7
        # inst1 grinded the whelps level 3 inst4 needed and inst4 skipped its quest. A grind keeps out of every claim,
        # a quest hunt only out of other quest hunts.
        quest = bool(self.wanted) or self.rxp_hunting
        cl = getattr(self, "claims", None)
        try:
            others = cl.others() if cl is not None else []
        except Exception:
            others = []
        if others and pts:
            free, taken = hunt_claims.split(pts, others, quest)
            if free and taken:
                pts = free
                who = sorted({(c["inst"], c.get("level", 0), bool(c.get("quest"))) for _, c in taken})
                if who != getattr(self, "_claim_log", None):
                    self._claim_log = who
                    log(f"roam: {len(taken)} of {len(taken) + len(free)} hunt points claimed by "
                        + ", ".join(f"inst {i} (level {lv}, {'quest' if q else 'grind'})" for i, lv, q in who)
                        + "; hunting elsewhere")
        # which spawns are up: tcf_characters.respawn lists the dead ones (other bots' kills too); guid None = unknown
        economy.dead()                                 # starts the (threaded) refresh when the cache is >20 s old
        known = economy.dead_known()
        info = {}                                      # (x, y) key -> (guid, xp value, s until it respawns)
        for p in pts:
            g, v = (p[3], p[4]) if len(p) >= 5 else ((sp.guid if (sp := economy.at(p[0], p[1])) else None), 1.0)
            info[(round(p[0]), round(p[1]))] = (g, v, economy.respawn_left(g, now) if (known and g) else 0.0)

        def inf(p):
            return info[(round(p[0]), round(p[1]))]
        fresh = [p for p in pts if self.visited.get((round(p[0]), round(p[1])), 0) < now - 240]
        placed = [p for p in pts if inf(p)[0]] if known else []
        if not fresh and placed:
            # was: "all hunt points visited <240s ago; waiting for respawns" (3 s sleep, then the nearest point,
            # dead or not): go to a spawn that is up, else to the one that respawns first, and wait there
            live = [p for p in placed if inf(p)[2] <= 0]
            fresh = live or [min(placed, key=lambda p: inf(p)[2])]
            log(f"all hunt points visited <240s ago; {len(live)} up again" if live else
                f"all hunt points visited <240s ago and dead; the first respawns in {inf(fresh[0])[2]:.0f}s")
            for p in fresh:
                self.visited.pop((round(p[0]), round(p[1])), None)
        elif not fresh and pts:
            for p in pts:
                self.visited.pop((round(p[0]), round(p[1])), None)
            fresh = pts
            if self.wanted:          # a quest mob that is not in sight: its spawns are on respawn timers, do not spin
                log("all hunt points visited <240s ago; waiting for respawns")
                time.sleep(3.0)
        pts = fresh
        if not pts:
            log("no hunt points; waiting")
            time.sleep(5)
            return

        # xp per second (economy.point_score): expected xp / (max(walk, respawn) + 25 s), a little more for spawns
        # that are up within 40 yd of it (a cluster). Quest points are all worth 1: nearest live cluster wins.
        # ... and less for a spawn packed with others (PACK_XP_DISCOUNT per neighbour: what it brings costs mana, rest
        # and deaths - the fights of a pull that brings 2 are not 2 x the xp for free)
        def score(p):
            d = math.hypot(p[0] - s.x, p[1] - s.y)
            _, v, left = inf(p)
            near = sum(1 for q in pts if q is not p and inf(q)[2] <= 0 and math.hypot(q[0] - p[0], q[1] - p[1]) < 40)
            n = pk.get((round(p[0]), round(p[1])), 0)
            return economy.point_score(v * (1 + 0.15 * min(near, 4)) / (1 + PACK_XP_DISCOUNT * min(n, 4)), d, left)
        p = max(pts, key=score)
        g, _, left0 = inf(p)
        d0 = math.hypot(p[0] - s.x, p[1] - s.y)     # how far the point was when picked (hunt_skip_check)
        try:                                 # the point we really hunt is ours for CLAIM_TTL s (refreshed by each pick)
            if cl is not None:
                cl.claim(p[0], p[1], s.level, quest, self.wanted)
        except Exception as e:
            log(f"claims: {e!r}")
        self.visited[(round(p[0]), round(p[1]))] = now
        for q in pts:
            if math.hypot(q[0] - p[0], q[1] - p[1]) < 15:
                self.visited[(round(q[0]), round(q[1]))] = now
        if left0 > 0:
            log(f"roam: best spawn ({p[0]:.0f},{p[1]:.0f}) is dead, up in {left0:.0f}s")
        short = [False]

        def seen(st):
            # walked rest: stop WALK_REST_STOP yd short of the spawn while hp / mana are below the pull line
            if (not short[0] and self.walk_rest and math.hypot(st.x - p[0], st.y - p[1]) < WALK_REST_STOP
                    and not self.ready_to_pull(st)):
                short[0] = True
                return True
            if self.far_stronger(st):
                return True                  # something far stronger ahead: stop, mark the area, re-plan
            # a wanted quest mob stops the walk at any sx (a plate behind us is still a plate)
            return bool(self.wanted_plates(st)) or \
                any(self.valid_plate(pl, st) and pl.sx >= 0 for pl in st.plates) or \
                any(pl.on_me for pl in st.plates) or st.combat
        if d0 > self.ROAM_FLY_YD:
            # a hunt point zones away goes by air when that saves time (fly_leg: the errands' flight plan); the walk
            # planner alone spent 4 x 40 s on "no path" from Ironforge to Westfall (bot 1 23:01-23:04)
            if self.fly_leg(p[0], p[1], p[2], abort=self.travel_abort) == "abort":
                return
        r = self.mv.goto(p[0], p[1], p[2], tol=4, timeout=60, abort=seen)
        if r == "abort" and short[0]:
            st = self.s()
            self.wait_ready(f"{math.hypot(st.x - p[0], st.y - p[1]):.0f} yd short of the spawn at ({p[0]:.0f},{p[1]:.0f})")
            st = self.s()
            if st.dead or st.combat or self.attackers(st) or self.pull_in_view(st):
                return                       # the main loop takes it from here
            r = self.mv.goto(p[0], p[1], p[2], tol=4, timeout=60, abort=seen)
        if r not in ("ok", "abort"):
            log(f"roam to ({p[0]:.0f},{p[1]:.0f}): {r}")
        if r in ("stuck", "timeout", "nopath"):
            # the walk there failed: other points first for a while, not the same mountainside again (inst2
            # 02:33-02:41, 8 min wedged on the slopes under the Seer points at (-5052,-289) / (-4984,-296))
            self.hazards.add("roam_fail", p[0], p[1], HUNT_SKIP_R, ROAM_FAIL_SKIP_S)
            log(f"roam: points within {HUNT_SKIP_R:.0f} yd of ({p[0]:.0f},{p[1]:.0f}) skipped for "
                f"{ROAM_FAIL_SKIP_S:.0f}s after walk={r}")
        if r == "ok" and g and known:
            left = economy.respawn_left(g)
            if left > 30:                    # not back within the wait (inst3 00:39:52 "up in 162s"): re-plan now
                log(f"roam: at spawn ({p[0]:.0f},{p[1]:.0f}), up in {left:.0f}s; not waiting")
            elif left > 0:                   # there just before it respawns: wait for it here, then re-plan
                log(f"roam: at spawn ({p[0]:.0f},{p[1]:.0f}), up in {left:.0f}s; waiting")
                short[0] = True              # a walked rest goes on here too: no 'stop short' abort
                t = time.time()
                while time.time() - t < min(left + 3, 30) and not os.path.exists(STOP):
                    st = self.s()
                    if st.dead or seen(st):
                        return
                    time.sleep(0.5)
        how = None
        if r == "ok" and (self.wanted or self.rxp_hunting):     # an RXP hunt searches before a mob id is known
            how = self.search_here(p[0], p[1])
            if how == "name":                                # "plate": the main loop's pick_target engages it
                self.wait_ready("quest mob targeted by name")     # a walked rest is finished first (target kept)
                res = self.fight()
                log(f"fight -> {res}")
                if res == "killed":
                    self.loot()
                return
        if r == "ok":
            self.hunt_skip_check(p, d0, how)

    def hunt_skip_check(self, p, d0, how):
        """After roam() reached point p (d0 yd from where it set off) and got search answer `how`: block every hunt
        point within HUNT_SKIP_R yd of p for HUNT_SKIP_S s when the named mob is beyond plate range ("far"), or
        when p was < HUNT_SKIP_NEAR yd away and gave nothing (searched <120 s ago / nothing found / no search).
        That is what stops the 0.5 s loop on the spawn underfoot (REPORT.md change 2)."""
        st = self.s()
        if st.dead or st.combat or self.attackers(st) or self.pull_in_view(st):
            return False                             # something to do right here
        if how == "far":
            why = "the named mob is beyond plate range"
        elif d0 < HUNT_SKIP_NEAR and how in (None, "skip"):
            why = f"{d0:.0f} yd away and {'searched <120 s ago' if how == 'skip' else 'nothing here'}"
        else:
            return False
        self.hazards.add("hunt_skip", p[0], p[1], HUNT_SKIP_R, HUNT_SKIP_S)
        log(f"roam: point ({p[0]:.0f},{p[1]:.0f}) {why}; points within {HUNT_SKIP_R:.0f} yd skipped for "
            f"{HUNT_SKIP_S:.0f}s")
        return True

    # ------------------------------------------------------------ weapon imbue
    IMBUE_MIN_SECS = 150          # refresh when less than this is left

    def imbue_due(self, s) -> bool:
        return (not s.weapon_buff or s.weapon_secs < self.IMBUE_MIN_SECS) and not self.retry.blocked("imbue")

    def imbue(self, s=None) -> bool:
        """Keep the class's weapon imbue (CLS.imbue) on the main hand."""
        if not CLS.imbue:
            return False
        s = s or self.s()
        slot = self.slot_of(CLS.imbue, s)
        if not slot or not self.imbue_due(s) or s.mana_pct < 15 or not s.ready(slot):
            return False
        self.retry.set("imbue", 20)                   # no spam when it keeps failing
        had = s.weapon_buff
        log(f"imbue: {CLS.imbue} ({'refresh, %ds left' % s.weapon_secs if had else 'missing'})")
        self.cast(CLS.imbue)
        time.sleep(0.3)
        s = self.s()
        if s.weapon_buff and s.weapon_secs >= self.IMBUE_MIN_SECS:
            self.retry.unblock("imbue")
        else:
            log(f"imbue: not applied (err {s.err}: {s.msg[:40]})")
        return True

    # ------------------------------------------------------------ the pet (CLS.pet; BotEye block 171, keys J K U Y H G)
    PET_KEYS = {"attack": "j", "call": "k", "revive": "u", "mend": "y", "feed": "h", "dismiss": "g", "assist": "b",
                "tame": "n"}
    TAME_CAST_S = 26.0        # s Tame Beast may channel (20 s, pushback from the beast's hits)
    TAME_RETRY_S = 45.0       # s between tame attempts
    TAME_ABORT_HP = 35        # % hp: a tame channel this low is cancelled and the beast fought
    PET_CALL_WAIT = 2.5       # s for a called pet to show
    PET_REVIVE_S = 12.0       # s a revive cast may take
    PET_MEND_HP = 50          # % pet hp: mend it out of combat below this
    PET_MEND_FIGHT_HP = 35    # ... in a fight (and our hp >= 50)
    PET_MEND_CD = 15.0
    PET_FEED_CD = 25.0        # s between feeds (the feed effect runs ~10 s)
    PET_ATTACK_CD = 2.0

    def no_pet_wanted(self) -> bool:
        """A step that tames a beast (an item used on a .mob: Taming the Beast) wants no pet out."""
        return time.time() < getattr(self, "_no_pet_until", 0.0)

    def _tameable(self, npc: int) -> bool:
        """A beast with a pet family (tcf_world creature_template type 1, family > 0), not elite; cached."""
        c = self.__dict__.setdefault("_tame_cache", {})
        if npc not in c:
            try:
                import worlddb
                rows = worlddb._query_db("tcf_world", "SELECT type, family, Classification FROM creature_template "
                                         f"WHERE entry={int(npc)}")
                c[npc] = bool(rows) and int(rows[0][0]) == 1 and int(rows[0][1]) > 0 and int(rows[0][2]) == 0
            except Exception as e:
                log(f"tame: creature lookup failed ({e!r})")
                c[npc] = False
        return c[npc]

    def tame_step(self) -> bool:
        """No pet owned (pet_step's call + revive found none) and the class tames one (CLS.tame): target a tameable
        beast in view at or under our level, not tapped / fighting, and channel the tame on it from range. The beast
        hits us through the channel; under TAME_ABORT_HP the channel is cancelled and the fight goes on as usual."""
        if not (CLS.pet and CLS.tame) or getattr(self, "_pet_none", 0) < 1:
            return False
        s = self.s()
        if s.pet_exists:
            self._pet_none = 0
            return False
        if s.dead or s.ghost or s.combat or self.attackers(s) or s.hp_pct < 70 or self.retry.blocked("tame") \
                or self.no_pet_wanted():
            return False
        bad = self.__dict__.setdefault("_tame_bad", set())
        cands = [p for p in s.plates if p.hostile and not p.dead and not p.tapped and not p.combat and not p.elite
                 and not p.player and p.level <= s.level and p.token and p.token <= 20 and p.sx >= 0
                 and p.npc not in bad and self._tameable(p.npc)]
        if not cands:
            self._once_log("tame_none", "tame: no tameable beast in view; looking on the way")
            return False
        p = min(cands, key=self.near_rank)
        self.mv.stop(); self.cancel_walk()
        self.keys.press(f"f{p.token}" if p.token <= 10 else f"num{p.token - 11}")
        time.sleep(0.3)
        st = self.s()
        if not (st.has_target and st.t_npc == p.npc and not st.t_dead):
            self.retry.set("tame", 5)
            return True
        log(f"tame: npc {p.npc} lvl {p.level} (a beast) - into range")
        t = time.time()
        while time.time() - t < 12 and not self.s().t_within28 and not self.s().combat:
            self.approach(1.0, force=True, close=True)
        self.mv.stop(); self.cancel_walk()
        time.sleep(0.3)
        self.keys.press(self.PET_KEYS["tame"])        # (/dismount first: "You are mounted", Hunt 13:18)
        time.sleep(0.8)
        st = self.s()
        if not st.casting_now and (st.mounted or "mounted" in (st.msg or "")):
            self.keys.press(self.PET_KEYS["tame"])    # off the mount now: the cast itself
            time.sleep(0.8)
            st = self.s()
        if not st.casting_now:
            log(f"tame: no channel ({(st.msg or '')[:50]}, err {st.err}); npc {p.npc} set aside")
            if "mounted" not in (st.msg or ""):
                bad.add(p.npc)
            self.keys.press("f11")
            self.retry.set("tame", self.TAME_RETRY_S)
            return True
        t = time.time()
        while time.time() - t < self.TAME_CAST_S:
            st = self.s()
            if not st.casting_now or st.pet_exists:
                break
            if st.hp_pct < self.TAME_ABORT_HP:
                log(f"tame: {st.hp_pct:.0f}% hp in the channel: cancelling, fighting it")
                self.keys.press("s", hold=0.2)          # a step cancels the channel
                break
            time.sleep(0.2)
        time.sleep(1.0)
        st = self.s()
        if st.pet_exists:
            log(f"pet: tamed npc {p.npc} ({st.pet_hp}% hp)")
            self._pet_none = 0
        else:
            log(f"tame: channel ended without a pet ({(st.msg or '')[:50]}); next try in {self.TAME_RETRY_S:.0f} s")
            self.retry.set("tame", self.TAME_RETRY_S)
        return True

    def _once_log(self, key, msg, every=120.0):
        seen = self.__dict__.setdefault("_once_logs", {})
        if time.time() - seen.get(key, 0) > every:
            seen[key] = time.time()
            log(msg)

    def pet_step(self) -> bool:
        """Out of combat: the pet out (call it; dead: revive it), mended when hurt, fed when not happy."""
        if not CLS.pet:
            return False
        s = self.s()
        if not getattr(s, "pet_known", False) or s.dead or s.ghost or s.combat or self.attackers(s) \
                or self.retry.blocked("pet") or self.no_pet_wanted():
            return False
        now = time.time()
        if not s.pet_exists or s.pet_dead:
            self.mv.stop(); self.cancel_walk()
            if not s.pet_dead:
                self.keys.press(self.PET_KEYS["call"])
                time.sleep(self.PET_CALL_WAIT)
                if self.s().pet_exists and not self.s().pet_dead:
                    log("pet: called")
                    return True
            log("pet: none out (dead or dismissed): reviving")
            self.keys.press(self.PET_KEYS["revive"])
            t = time.time()
            time.sleep(0.6)
            while time.time() - t < self.PET_REVIVE_S and self.s().casting_now and not self.s().combat:
                time.sleep(0.2)
            s = self.s()
            if s.pet_exists and not s.pet_dead:
                log(f"pet: revived ({s.pet_hp}% hp)")
            else:
                log(f"pet: still none after call + revive (err {s.err}: {(s.msg or '')[:40]}); next try in 60 s")
                self.retry.set("pet", 60)
                # neither a pet to call nor a dead one to revive: none is owned yet (Hunt 13:11, after Training the
                # Beast: the quests teach Tame Beast, the player tames the pet) -> tame_step
                self._pet_none = getattr(self, "_pet_none", 0) + 1
            return True
        if s.pet_hp < self.PET_MEND_HP and now >= getattr(self, "_mend_t", 0.0):
            self._mend_t = now + self.PET_MEND_CD
            log(f"pet: mending ({s.pet_hp}% hp)")
            self.keys.press(self.PET_KEYS["mend"])
            return True
        if 0 < s.pet_happy < 3 and now >= getattr(self, "_feed_t", 0.0) and getattr(s, "foods", 0):
            self._feed_t = now + self.PET_FEED_CD
            log(f"pet: feeding (happiness {s.pet_happy}/3)")
            self.mv.stop(); self.cancel_walk()
            self.keys.press(self.PET_KEYS["feed"])
            time.sleep(1.0)
            return True
        return False

    def pet_attack(self, s):
        """In a fight: the pet on our target (J: /petattack) whenever it is on something else; Mend Pet when it is low
        and we are not."""
        if not CLS.pet or not getattr(s, "pet_exists", False) or s.pet_dead:
            return
        now = time.time()
        if s.has_target and not s.t_dead and s.t_attackable and not s.pet_on_target \
                and now - getattr(self, "_petatk_t", 0.0) >= self.PET_ATTACK_CD:
            self._petatk_t = now
            self.keys.press(self.PET_KEYS["attack"])
        if s.pet_hp and s.pet_hp < self.PET_MEND_FIGHT_HP and s.hp_pct >= 50 and now >= getattr(self, "_mend_t", 0.0):
            self._mend_t = now + self.PET_MEND_CD
            log(f"pet: mending in the fight ({s.pet_hp}% hp)")
            self.keys.press(self.PET_KEYS["mend"])

    # ------------------------------------------------------------ rotation (nb/classes.py: the class's)
    def rot_cast(self, spell: str) -> bool:
        """Cast what the rotation asked for (keys only), report the outcome back to it. A spell at the mob is faced
        first when its plate is off centre (self-spells need no facing)."""
        s0 = self.s()
        if spell not in CLS.self_spells and s0.has_target:
            tp = self.target_plate(s0)
            if self.cam_off or tp is None or tp.sx < 0 or abs(self.yaw_of(tp)) > 0.6:
                self.face_target()
        ok = self.cast(spell)
        self.rot.note_cast(spell, ok, self.s())
        if ok and spell == CLS.heal:
            log(f"  healed at hp {s0.hp_pct:.0f}% mana {s0.mana_pct:.0f}% (mob {s0.t_hp_pct:.0f}%)")
        return ok

    def buffs(self) -> bool:
        """Out-of-combat buffs from the rotation (CLS.buffs); the weapon imbue is imbue()."""
        s = self.s()
        if self.attackers(s):
            return False
        spell = self.rot.buff_spell(s)
        return bool(spell and self.rot_cast(spell))

    # ------------------------------------------------------------ vendor
    VENDOR_FT = 55                  # Ironforge's faction template: a vendor friendly to it is friendly to us
    BAG_DETOUR = 250.0              # yd of extra walk a bag-selling vendor is worth while the bags are full
    # starter containers sold by general goods vendors (Small Brown Pouch, Brown Leather Satchel, Heavy Brown Bag, ...)
    BAG_ITEMS = (4496, 4497, 4498, 4499, 5571, 5572, 5573, 5574, 805, 828, 856, 857)

    def bag_vendors(self) -> set:
        """Creature entries (tcf_world npc_vendor) that sell one of BAG_ITEMS; cached."""
        if getattr(self, "_bag_vendors", None) is None:
            try:
                import worlddb
                rows = worlddb._query_db("tcf_world", "SELECT DISTINCT entry FROM npc_vendor WHERE item IN (%s)"
                                         % ",".join(str(i) for i in self.BAG_ITEMS))
                self._bag_vendors = {int(r[0]) for r in rows}
            except Exception as e:
                log(f"vendor: bag seller lookup failed ({e!r})")
                self._bag_vendors = set()
        return self._bag_vendors

    def stock_sellers(self, s, kinds) -> dict:
        """{npc entry: kinds} of the vendors selling a current-tier water / food of `kinds` (nb/stock.py); {} when
        the world DB cannot say (one log line, a 10 min pause on stock errands)."""
        if not kinds:
            return {}
        try:
            return stock.sellers(s.level, kinds, stock.item_table(), stock.vendor_items())
        except Exception as e:
            log(f"stock: seller lookup failed ({e!r})")
            self.retry.set("stock", 600)
            return {}

    def stock_text(self, s) -> str:
        """'drink 2 (best 1179), food 0' - the current-tier counts BotEye reports."""
        c = stock.counts(s)
        return ", ".join(f"{k} {c[k][1]}" + (f" (best {i})" if i else "")
                         for k, i in (("drink", getattr(s, "drink_id", 0)), ("food", getattr(s, "food_id", 0)))
                         if c[k][2])

    def stock_low(self, s) -> list:
        """The kinds of water / food under stock.LOW whose top-up the money covers over the reserve (drink first).
        Nothing while the client's BotEye is older than build 13 (no stock blocks)."""
        low = stock.low_kinds(s)
        if not low or self.retry.blocked("stock"):
            return []
        try:
            sold = set().union(*stock.vendor_items().values())
            ok = stock.affordable(s, low, stock.item_table(), sold)
        except Exception as e:
            log(f"stock: item lookup failed ({e!r})")
            self.retry.set("stock", 600)
            return []
        if len(ok) < len(low) and time.time() - getattr(self, "_stock_log_t", 0.0) > 600:
            self._stock_log_t = time.time()
            log(f"stock: {self.stock_text(s)} low, money {s.money}c: under the {stock.reserve(s.level)}c reserve + "
                f"{stock.LOW} items for {', '.join(k for k in low if k not in ok)}")
        return ok

    def vendor_order(self, s, need_repair=False, n_path=5, supplies=(), only_supplies=False):
        """Every friendly vendor on the map (server spawns; repairers only when the gear needs it), the n_path nearest
        by air re-ranked by walking distance on the mesh. (inst1 00:00:04: the 3-name list sent the bot on a
        1409 yd walk to Anvilmar from Brewnall Village, with Burdrak Harglhelm (repairs) 100 yd away.) supplies: the
        water / food kinds that are low - a vendor selling them is worth stock.DETOUR yd more walk; only_supplies (a
        stock-only errand): those vendors only. self.vendor_walks: {npc: yd of walk} of the ranked ones."""
        import services
        pool = {c["npc"]: dict(c) for c in trainers.VENDORS}
        try:
            for n in services.all_npcs():
                sv = n["services"]
                if n["map"] != 0 or "vendor" not in sv or (need_repair and "repair" not in sv):
                    continue
                if not factions.is_friendly(self.VENDOR_FT, n["faction"]):
                    continue
                pool.setdefault(n["entry"], {"name": n["name"], "npc": n["entry"], "map": 0,
                                             "pos": tuple(n["pos"])})
        except Exception as e:
            log(f"vendor: spawn list failed ({e!r}); the fixed list only")
        sellers = self.stock_sellers(s, supplies)
        if only_supplies:
            pool = {k: c for k, c in pool.items() if k in sellers}
        order = sorted(pool.values(), key=lambda c: math.hypot(c["pos"][0] - s.x, c["pos"][1] - s.y))
        head, tail = order[:n_path], order[n_path:]
        raw = {}                                        # npc -> yd of walk, before any detour bonus

        def walk_len(c):
            try:
                me = (s.x, s.y, self.mv.nm.ground_z(s.x, s.y) or s.z)
                p = self.mv.nm.find_path(me, tuple(c["pos"]), dangers=(), budget=6)
            except Exception:
                p = None
            if not p:
                return 1e9
            pts = [me[:2]] + [(float(q[0]), float(q[1])) for q in p]
            raw[c["npc"]] = d = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            return d
        lens = {c["npc"]: walk_len(c) for c in head}
        if s.bags_free <= 2:
            # bags full: a merchant with a bag (BotEye buy_bag fills an empty bag slot) is worth BAG_DETOUR yd more
            # walk than one without (inst1 00:40-00:59: 16-slot backpack only, 4 trips freeing 1 slot each)
            bag_sellers = self.bag_vendors()
            for c in order[:12]:
                if c["npc"] in bag_sellers and c["npc"] not in lens:
                    head.append(c)
                    lens[c["npc"]] = walk_len(c)
            for c in head:
                if c["npc"] in bag_sellers:
                    lens[c["npc"]] -= self.BAG_DETOUR
        self.vendor_walks = raw
        if sellers and not only_supplies:
            # water / food low: a vendor that sells the next stock beats a nearer one by stock.DETOUR yd
            for c in order[:12]:
                if c["npc"] in sellers and c["npc"] not in lens:
                    head.append(c)
                    lens[c["npc"]] = walk_len(c)
            for c in head:
                if c["npc"] in sellers:
                    lens[c["npc"]] -= stock.DETOUR
        head.sort(key=lambda c: lens[c["npc"]])
        tail = [c for c in tail if c not in head]
        if head:
            log(f"vendor: nearest by walk {head[0]['name']} {lens[head[0]['npc']]:.0f} yd"
                + "".join(f", {c['name']} {lens[c['npc']]:.0f}" for c in head[1:3]))
        return head + tail

    MAIL_R = 600.0            # yd: a mailbox this near is walked to when mail is waiting

    def mailboxes(self) -> list:
        """(x, y, z) of every mailbox on map 0 (tcf_world gameobject, type 19); cached."""
        if getattr(self, "_mailboxes", None) is None:
            try:
                import worlddb
                rows = worlddb._query_db("tcf_world", "SELECT g.position_x, g.position_y, g.position_z FROM gameobject g "
                                         "JOIN gameobject_template t ON t.entry = g.id WHERE t.type = 19 AND g.map = 0")
                self._mailboxes = [(float(a), float(b), float(c)) for a, b, c in rows]
            except Exception as e:
                log(f"mail: mailbox lookup failed ({e!r})")
                self._mailboxes = []
        return self._mailboxes

    def mail_step(self) -> bool:
        """Unread mail (BotEye bit 21): walk to the nearest mailbox within MAIL_R and open it; BotEye takes the money
        and the items (take_mail) and its gear check equips what is better."""
        s = self.s()
        if not getattr(s, "new_mail", False) or s.dead or s.ghost or s.combat or self.retry.blocked("mail"):
            return False
        boxes = [b for b in self.mailboxes() if math.hypot(b[0] - s.x, b[1] - s.y) <= self.MAIL_R]
        if not boxes:
            self.retry.set("mail", 600)
            return False
        x, y, z = min(boxes, key=lambda b: math.hypot(b[0] - s.x, b[1] - s.y))
        log(f"mail: unread mail, walking to the mailbox at ({x:.0f},{y:.0f}), {math.hypot(x - s.x, y - s.y):.0f} yd")
        r = self.mv.goto(x, y, z, tol=2.5, timeout=240, abort=lambda st: st.combat)
        if r != "ok":
            self.retry.set("mail", 120)
            log(f"mail: walk {r}")
            return True
        m0, free0 = self.s().money, self.s().bags_free
        res = self._open_object((x, y, z), lambda st: not st.new_mail, wait=5.0)
        time.sleep(6.0)                              # BotEye takes one mail per inbox update
        s = self.s()
        log(f"mail: opened ({res}); money {m0} -> {s.money}, bags free {s.bags_free}, unread {int(s.new_mail)}")
        self.keys.press("esc")
        # a visit that took nothing (still "unread": emptied mails the client keeps flagged) is not repeated for an
        # hour; Hunt 09:51-09:57 walked 526 yd back to an inbox of empty mails
        took = s.money != m0 or s.bags_free != free0
        self.retry.set("mail", 300 if took else 3600)
        return True

    def vendor_step(self) -> bool:
        """Bags nearly full, gear worn, or water / food low (stock_low): sell the gray items, repair and stock up at
        the nearest vendor (the addon sells, repairs and buys water / food when the merchant window opens). A
        stock-only errand goes to a vendor that sells the stock, within stock.WALK_MAX yd of walk."""
        s = self.s()
        if s.dead or s.ghost or self.retry.blocked("vendor"):
            return False
        worn = 0 <= s.dur <= 40
        # the last sale left the bags as full (nothing gray to sell): only a fuller bag is worth a new trip
        # (inst1 00:25:51 / 00:28:17 / 00:30:51: three trips, "sold, 2 bag slots free now" each time)
        full = s.bags_free <= 2 and (worn or s.bags_free < getattr(self, "vendor_nogray_free", 99))
        low = self.stock_low(s)
        if not (worn or full or low):
            return False
        errand = not (worn or full)                     # water / food only
        order = self.vendor_order(s, need_repair=worn, supplies=low, only_supplies=errand)
        if errand:
            walks = getattr(self, "vendor_walks", {})
            d = walks.get(order[0]["npc"], 1e9) if order else 1e9
            wmax = stock.walk_max(s, low)
            if d > wmax:
                log(f"stock: {self.stock_text(s)} low; " + (f"nearest seller {order[0]['name']} {d:.0f} yd of walk "
                    f"(over {wmax:.0f}): later" if order else "no seller of the current tier on the map"))
                self.retry.set("stock", 300)
                return False
            log(f"stock: {self.stock_text(s)} low: errand to {order[0]['name']} ({d:.0f} yd of walk)")
            # a seller that cannot be targeted falls through to the next one: only those within WALK_MAX too (the
            # unranked tail has no walk length and may be a 1 km trip for a stock-only errand)
            order = [c for c in order if walks.get(c["npc"], 1e9) <= wmax]
        s0 = s
        v = None
        for cand in order:                              # nearest first; one that cannot be targeted -> the next
            x, y, z = cand["pos"]
            log(f"vendor: {s.bags_free} bag slots free, gear {s.dur}%, walking to {cand['name']}"
                + (f" (stock: {self.stock_text(s)})" if low else ""))
            r = self.travel_to(x, y, z, tol=3, timeout=480, abort=self.travel_abort)   # (a trainer can be ~1.3 km off)
            if r == "abort":
                return True
            if self.target_npc(cand["npc"]):
                v = cand
                break
            self.approach(1.5, force=True) if self.s().has_target else None
            if self.target_npc(cand["npc"]):
                v = cand
                break
            log(f"vendor: could not target {cand['name']}")
        self.retry.set("vendor", 300)                   # one trip per 5 min at most
        self._stock_bought = False
        if v is None:
            if errand:
                self.retry.set("stock", 900)            # no seller reached: no new stock errand for 15 min
            return True
        opened = False
        # a vendor behind a gossip page (an innkeeper: quests first, Goldshire 2026-10-18 09:46 'window did not open'
        # twice with 0 food): BotEye's gossip picks "browse goods" when the bot asks (local /be command, no chat line)
        self.keys.chat("/be want vendor")
        time.sleep(0.3)
        for _ in range(3):
            self.keys.press("m")
            end = time.time() + 5
            while time.time() < end:
                time.sleep(0.25)
                if self.s().ui_open(2):
                    opened = True
                    break
            if opened:
                break
        time.sleep(1.5)                                  # the addon sells on MERCHANT_SHOW
        s = self.s()
        if opened and stock.known(s0):
            s = self.stock_wait(s0, v)
        if errand and not self._stock_bought:
            self.retry.set("stock", 900)                # the errand bought nothing: not again for 15 min
        log(f"vendor: {'sold' if opened else 'window did not open'}, {s.bags_free} bag slots free now "
            f"(addon: {(s.msg or '')[:60]!r})")
        if opened:
            self.vendor_nogray_free = s.bags_free if s.bags_free <= 2 else 99
        self.close_windows()
        self.keys.chat("/be want")                      # (windows closed: no command typed into an NPC window)
        self.keys.press("f11")
        return True

    STOCK_WAIT = 6.0                # s more with the merchant window open while BotEye buys water / food (0.5 s in,
    #                                 then up to 4 retries 1 s apart while the merchant's item data loads)

    def stock_wait(self, s0, v):
        """The merchant window is open: when this vendor sells a current tier we are short of (under the target),
        wait up to STOCK_WAIT s for BotEye's purchase to reach the bags, and log it. -> the last state."""
        target = {"drink": stock.DRINK_TARGET, "food": stock.FOOD_TARGET, "arrow": 1000, "bullet": 1000}  # (BotEye AMMO_TARGET)
        c0 = stock.counts(s0)
        short = [k for k, (_n, cur, want) in c0.items() if want and cur < target[k]]
        sells = self.stock_sellers(s0, short).get(v["npc"], set()) if short else set()
        s = self.s()
        if not sells:
            return s
        dl = Deadline(self.STOCK_WAIT)
        while not dl.passed():
            c = stock.counts(s)
            if all(c[k][1] >= target[k] for k in sells) or ("I:stock" in (s.msg or "") and all(
                    c[k][1] > c0[k][1] for k in sells)):
                break
            time.sleep(0.25)
            s = self.s()
        c = stock.counts(s)
        got = [f"{k} {c0[k][1]} -> {c[k][1]}" for k in c0 if k in sells]
        bought = self._stock_bought = any(c[k][1] > c0[k][1] for k in sells)
        log(f"stock: {', '.join(got)} at {v['name']} (money {s0.money} -> {s.money}c)" if bought else
            f"stock: bought nothing at {v['name']} ({', '.join(got)}, money {s.money}c, reserve "
            f"{stock.reserve(s.level)}c; addon: {(s.msg or '')[:60]!r})")
        return s

    # ------------------------------------------------------------ training
    def train_step(self) -> bool:
        """Every 2nd level walk to the class trainer and learn what is on offer.
        The addon picks the trainer gossip option and buys on TRAINER_SHOW."""
        s = self.s()
        if s.dead or s.ghost or not trainers.is_due(s.level) or self.retry.blocked("train"):
            return False
        if self.rxp_mode and not self.rxp_train_due(s):
            return False                        # RXP mode: the guide's trainer steps train; this only catches up
        if s.money < max(getattr(self, "train_need", 0),
                         trainers.min_cost(trainers.trained_level(), trainers.due_level(s.level))):
            return False                        # last visit we could not pay: farm copper first
        t = trainers.trainer_for(s.level, (s.x, s.y))
        if t is None:
            log(f"train: no {CLS.name} trainer known (nb/trainers.py CLASS_TRAINERS); training at the guide's steps")
            self.retry.set("train", 3600)
            return False
        x, y, z = t["pos"]
        log(f"train: level {s.level}, walking to {t['name']} ({x:.0f},{y:.0f})")
        r = self.travel_to(x, y, z, tol=3, timeout=480, abort=self.travel_abort)   # (a trainer can be ~1.3 km off)
        if r == "abort":
            return True
        s = self.s()
        log(f"train: walk={r} at ({s.x:.0f},{s.y:.0f}), {math.hypot(s.x - x, s.y - y):.0f} yd from {t['name']}")
        if not self.target_npc(t["npc"]):
            s = self.s()
            log(f"train: could not target {t['name']} (walk={r} at ({s.x:.0f},{s.y:.0f}), got {s.t_npc})")
            self.retry.set("train", 120)
            return True
        opened = False
        # BotEye's gossip() takes quest offers before the trainer option now (training first hid a trainer's class
        # quest from an untrained character); on this visit the bot asks for the trainer (local /be command, no chat
        # message), so a training trip does not pick up quests out of the route's order. Cleared after the visit
        self.keys.chat("/be want train")
        time.sleep(0.3)
        try:
            for _ in range(3):
                self.keys.press("m")
                end = time.time() + 6
                while time.time() < end:
                    time.sleep(0.25)
                    if self.s().ui_open(3):
                        opened = True
                        break
                if opened:
                    break
            if opened:
                time.sleep(4.5)                     # the addon buys on TRAINER_SHOW (an empty list is only reported after 4 s of retries)
                s = self.s()
                txt = s.msg or ""
                try:                                # the strip message is overwritten by chat lines: ask the addon for its own record
                    from nb.ev import ev
                    txt = ev("tostring(BotEyeDB.lastTrain)", self.eye, self.keys, wait=2.0)
                except Exception as e:
                    log(f"train: could not read the addon's record ({e!r})")
                m = re.search(r"bought (\d+) of (\d+) poor (\d+) need (\d+)", txt)
                if m and int(m.group(3)) > 0:       # could not afford everything: come back with the money
                    self.train_need = int(m.group(4))
                    self.retry.set("train", 60)
                    json.dump(self.train_need, open(inst.path("train_need.json"), "w"))
                    log(f"train: bought {m.group(1)}, {m.group(3)} spell(s) unaffordable - back when we have {self.train_need} copper (have {s.money})")
                elif m:
                    self.train_need = 0
                    json.dump(0, open(inst.path("train_need.json"), "w"))
                    trainers.mark_trained(s.level)
                    log(f"train: learned from {t['name']} at level {s.level} ({txt})")
                elif "empty" in txt:              # the trainer window lists nothing for this level (seen at 8): nothing to learn here
                    self.train_need = 0
                    trainers.mark_trained(s.level)
                    log(f"train: {t['name']} offers nothing at level {s.level} ({txt}); not coming back for it")
                else:
                    self.retry.set("train", 60)
                    log(f"train: no record of a purchase ({txt!r}); not marking it done")
            else:
                s = self.s()
                log(f"train: trainer window did not open (target {s.t_npc}, within10={s.t_within10}, ui={s.ui})")
                self.retry.set("train", 120)
        finally:                                    # also on an error: st.want = 'train' would keep taking
            self.close_windows()                    # the trainer before quest offers on every later talk
            self.keys.chat("/be want")              # quest talks take the quest offers again
        self.keys.press("f11")
        return True

    # ------------------------------------------------------------ quests
    def talk(self, npc, pos, why):
        name = self.qdb.names.get(npc, str(npc))
        log(f"quest: {why} -> {name}")
        r = self.travel_to(pos[0], pos[1], pos[2], tol=5, timeout=180,
                           abort=lambda st: st.combat)
        if r == "abort":
            return
        if r in ("nopath", "stuck", "timeout", "dead"):
            # never reached the spot: do not target / press interact out of range, retry soon
            if r != "dead":
                self.no_offer.set(npc, {"nopath": 600, "stuck": 300, "timeout": 120}[r])
            log(f"  walk={r} to {name}; retrying later")
            return
        s = self.s()
        before = {q.qid for q in s.quests}
        self.keys.press("f11"); time.sleep(0.15)          # clear a stale target first
        self.target_npc(npc)
        s = self.s()
        if s.t_npc != npc and name != str(npc):
            # no nameplate (the Minor Manifestation of Earth is invisible except to Sapta Sight and never gets a
            # plate, 15:00): /target it by name through the BotEyeFind macro
            if [name] != self.find_names:
                self.keys.chat("/be find " + name)
                self.find_names = [name]
                time.sleep(0.4)
            self.keys.press("num*"); time.sleep(0.4)
            s = self.s()
            if s.t_npc == npc:
                log(f"  targeted {name} by name (no nameplate)")
        if s.t_npc != npc:
            log(f"  walk={r} at ({s.x:.0f},{s.y:.0f}) npc spot ({pos[0]:.0f},{pos[1]:.0f}) "
                f"d={math.hypot(s.x - pos[0], s.y - pos[1]):.0f} plates={[(p.npc, p.friendly, p.token) for p in s.plates]}")
            log(f"could not target {name} (got {s.t_npc})")
            # unreachable (stuck / timed out on the way): shelve for an hour, else retry in 10 min
            # walked to the spot and the NPC is simply not there (event spawn / phased): the second
            # miss shelves it for an hour instead of walking back every 10 minutes
            self.no_offer.fail(npc, (600, 3600), tally=("target_miss", npc))   # (the tally never resets)
            return
        # quest givers wander off their spawn spot: close in on the targeted NPC first
        t_close = time.time()
        while time.time() - t_close < 15:
            s = self.s()
            if not s.has_target or s.t_npc != npc or s.combat or s.t_within10:
                break
            self.approach(1.5, force=True)
        presses, misses, ui_seen = 0, 0, False
        while presses < 4:
            presses += 1
            n0 = {q.qid for q in s.quests}
            m0 = s.msgn
            self.keys.press("m")                        # (keys.before['m'] runs fake_activate first)
            t = time.time()
            shut = None
            opened = False
            while time.time() - t < 8:
                time.sleep(0.2)
                s = self.s()
                open_now = any(s.ui_open(i) for i in range(4))
                opened = opened or open_now
                if open_now or s.moving or time.time() - t < 1.6:
                    shut = None
                elif shut is None:
                    shut = time.time()                  # every dialog is closed: the addon has finished its steps
                elif time.time() - shut >= 0.6:
                    break
            s = self.s()
            ui_seen = ui_seen or opened
            if s.msgn != m0 and "closer" in (s.msg or "").lower() and not any(s.ui_open(i) for i in range(4)):
                self.approach(2.0, force=True)          # "You need to be closer to interact": it wandered off
                continue
            if {q.qid for q in s.quests} == n0:
                if not opened and misses < 3:
                    # no window opened at all: the press never reached the client (an input miss), not an NPC with
                    # nothing to offer. Press again now instead of shelving it (26% of the talks changed nothing)
                    misses += 1
                    presses -= 1
                    log(f"  no window opened at {name}: input miss {misses}/3, pressing again")
                    if s.t_npc != npc:
                        self.target_npc(npc, tries=3)
                    continue
                break
            # a second press only if this NPC still has a quest to give or to take
            have = {q.qid for q in s.quests}
            self.qdb.done |= before - have            # handed in this visit: a follow-up quest is now eligible
            more = any(npc in npcs and self.qdb.eligible(qid, s.level, have)
                       for qid, npcs in self.qdb.starters.items()) or \
                any(q.complete and npc in self.qdb.enders.get(q.qid, ()) for q in s.quests)
            if not more:
                break
        self.close_windows()
        self.keys.press("f11")
        after = {q.qid for q in s.quests}
        gone, new = before - after, after - before
        if gone:
            self.qdb.done |= gone
            self.qdb.save()
        log(f"talked to {name}: handed in {sorted(gone)}, got {sorted(new)} (log {sorted(after)})")
        if ui_seen:
            self.no_offer.reset(("miss_talks", npc))
        if not gone and not new and not ui_seen and self.no_offer.count(("miss_talks", npc)) < 2:
            # every press missed (no window ever opened): not a "no offer" answer from the NPC. Short pause only -
            # twice: an NPC that never opens a window (npcflag 0, like Grund 2756) gets the 90 / 900 s below after that
            self.no_offer.fail(npc, (60,), tally=("miss_talks", npc))
            log(f"  {name}: no window opened in {misses} retries; trying again in 60 s")
        elif not gone and not new:
            # nothing changed. If the NPC still has a quest for us by the DB, the interact most likely never
            # opened (2026-10-14: WoW was in the background and Sten's first quest was missed): retry soon
            have = {q.qid for q in s.quests}
            owed = any(npc in npcs and self.qdb.eligible(qid, s.level, have) for qid, npcs in self.qdb.starters.items())
            # one quick retry only: the DB can list a quest the NPC never offers (the tally never resets)
            n_owed = self.no_offer.bump(("owed", npc))        # counted whether or not the DB says it owes one
            owed = owed and n_owed <= 1
            self.no_offer.set(npc, 90 if owed else 900)
            if owed:
                log(f"  {name} still owes a quest by the DB: retry in 90 s")

    def walk_via(self, pos, via=(), tol=4, timeout=240, abort=None):
        """Walk to pos through a route's own waypoints (use_item_at; RXP mode's use steps share it): start at the
        waypoint after the nearest one (we may be part-way along the trail), path the first leg from wherever we are,
        walk the trail legs straight (direct: the hand-checked Spirit Stone trail; a single pathfinding route looped
        at the foot of the hill, 13:32). -> the last walk's result ('abort' as soon as a leg aborts)."""
        abort = abort or (lambda st: st.combat)
        s = self.s()
        if via:
            k = min(range(len(via)), key=lambda n: math.hypot(via[n][0] - s.x, via[n][1] - s.y))
            via = list(via[k:]) if math.hypot(via[k][0] - s.x, via[k][1] - s.y) > 15 else list(via[k + 1:])
        for n, (vx, vy, vz) in enumerate(via):
            r = self.mv.goto(vx, vy, vz, tol=6, timeout=120, abort=abort, direct=n > 0)
            if r == "abort":
                return r
            if r != "ok":
                log(f"  walk={r} to route waypoint ({vx:.0f},{vy:.0f}); going on to the spot")
        return self.travel_to(pos[0], pos[1], pos[2], tol=tol, timeout=timeout, abort=abort, direct=bool(via))

    def use_item_at(self, key, pos, why, step_i, via=()):
        """A guide '.use <item>' step: walk to the spot (through the route's own waypoints), stand still and press
        the item's BotEye key (a /use macro)."""
        log(f"quest: {why} at ({pos[0]:.0f},{pos[1]:.0f}) via {len(via)} route waypoints")
        r = self.walk_via(pos, via, tol=4, timeout=240)
        if r == "abort":
            return
        if r in ("nopath", "stuck", "timeout", "dead"):
            log(f"  walk={r} to the item spot; retrying later")
            self.guide.note_used(step_i)           # do not loop on an unreachable spot: the cooldown spaces retries
            return
        self.mv.stop()
        time.sleep(0.4)
        s0 = self.s()
        self.keys.press(key)
        t = time.time()
        while time.time() - t < 6:                  # the item casts (Earth Sapta: spell 8202), then summons
            time.sleep(0.3)
            s = self.s()
            if s.combat:
                break
            if time.time() - t > 1.5 and not s.casting_now:
                break
        s = self.s()
        log(f"  used the item: msg {s.msg!r} err={s.err if s.errn != s0.errn else 0}")
        self.guide.note_used(step_i)

    def use_object(self, go, pos, why):
        """Hand a quest in at a game object: walk next to it and press interact (soft-target interact
        picks the nearest object); judged by the quest log changing."""
        log(f"quest: {why} (object {go})")
        r = self.mv.goto(pos[0], pos[1], pos[2], tol=2.5, timeout=240, abort=lambda st: st.combat)
        if r == "abort":
            return
        s = self.s()
        before = {q.qid for q in s.quests}
        if r == "ok":
            for attempt in range(2):
                res = self._open_object(pos, lambda st: {q.qid for q in st.quests} != before)
                if res in ("done", "combat") or res.startswith("err:"):
                    break
            # the hand-in at an object can open its follow-up's offer (419 at object 2059 -> 417 A Pilot's Revenge,
            # tcf gameobject_queststarter 2059): the addon accepts it, so wait as talk() does until every dialog has
            # been closed for 0.6 s. An ESC right after the hand-in closed 417's offer before it was accepted
            t, shut = time.time(), None
            while time.time() - t < 8:
                time.sleep(0.2)
                s2 = self.s()
                if s2.combat:
                    break
                if any(s2.ui_open(i) for i in range(4)) or time.time() - t < 1.6:
                    shut = None
                elif shut is None:
                    shut = time.time()
                elif time.time() - shut >= 0.6:
                    break
        self.close_windows()
        after = {q.qid for q in self.s().quests}
        gone, new = before - after, after - before
        if gone:
            self.qdb.done |= gone
            self.qdb.save()
        if gone or new:
            log(f"used object {go}: handed in {sorted(gone)}, got {sorted(new)}")
        else:
            log(f"used object {go}: nothing happened (walk={r} at ({s.x:.0f},{s.y:.0f}) spot ({pos[0]:.0f},{pos[1]:.0f}))")
            self.qdb.skip_go.add(go)                     # do not loop on it

    def _open_object(self, pos, done, wait=9.0):
        """Interact with the object at `pos`: clear the target, face it, stand still, press interact and wait.
        Opening a chest is a CAST of a few seconds - never move while it runs (that cancels it).
        Returns 'done' | 'combat' | 'err:<code>' | 'none' (the interact never started)."""
        self.keys.press("f11"); time.sleep(0.15)               # a hard target would take the interact
        self.mv.face(pos[0], pos[1], tol=0.1)
        self.mv.stop()
        time.sleep(0.4)
        s = self.s()
        n0 = s.errn
        self.keys.press("m")
        t = time.time()
        started, busy_last = False, 0.0
        while time.time() - t < wait:
            time.sleep(0.15)
            s = self.s()
            if done(s):
                return "done"
            if s.errn != n0 and s.err in (6, 12, 14, 16, 20, 21):
                return f"err:{s.err}"
            busy = s.casting_now or s.casting != 0
            if busy:
                if not started:
                    log(f"  object cast started after {time.time() - t:.1f}s")
                started, busy_last = True, time.time()
            elif started and time.time() - busy_last > 0.6:
                break
            elif not started and time.time() - t > 1.8:
                break
            if self.attackers(s) and not busy and time.time() - t > 0.6:
                return "combat"
        if started:
            log(f"  object cast ran {busy_last - t:.1f}s")
            time.sleep(0.8)                                     # the counters update a moment after the cast
            if done(self.s()):
                return "done"
        return "none"

    GATHER_DEATH_R = 30.0               # yd round a death spot whose quest chests wait 30 min
    GATHER_BACKOFF = (120, 600, 3600)   # s a chest is left after its 1st, 2nd, 3rd+ failed attempt (gather_object)

    GUARD_R = 30.0         # yd: an elite spawned this close to a chest guards it
    GUARD_STANDOFF = 40.0  # yd from the chest we wait at for the guard to walk off
    GUARD_CLEAR_S = 6.0    # s with no guard plate in view before we go in
    GUARD_CLEAR_SEEN_S = 15.0   # ... once we have seen it: it walks off, not just out of frame
    GUARD_WAIT_S = 300.0   # s we wait before leaving the chest for later

    def elite_guards(self, pos) -> set:
        """NPC ids of hostile elites spawned within GUARD_R of pos that are not gray to us."""
        try:
            sp = economy.spawns()
        except Exception:
            return set()
        lvl = self.s().level or 1
        return {p.entry for p in sp if p.rank and p.hi >= lvl - 4 and factions.is_hostile(DWARF_FACTION, p.faction)
                and math.hypot(p.x - pos[0], p.y - pos[1]) < self.GUARD_R}

    def wait_guards_away(self, pos, guards) -> str:
        """Stand GUARD_STANDOFF yd off pos (on our side) until no guard plate shows for GUARD_CLEAR_S.
        -> "clear", "seen" (still there after GUARD_WAIT_S), or "abort" (combat / walk failed / stop)."""
        s = self.s()
        d = math.hypot(s.x - pos[0], s.y - pos[1])
        if d > self.GUARD_STANDOFF + 5:
            # GUARD_STANDOFF yd back along the walk to it, not on the straight line towards us: inst1 05:02 waited at
            # (-5619,-123), 40 yd from the Meat Locker by air but 449 yd of walking, and met the guard on the way in
            sx = sy = None
            try:
                pts = self.mv.path(pos[0], pos[1], pos[2]) or []
            except Exception:
                pts = []
            if pts:
                acc, prev = 0.0, (pos[0], pos[1])
                for q in reversed(pts):
                    acc += math.hypot(q[0] - prev[0], q[1] - prev[1])
                    prev = (q[0], q[1])
                    if acc >= self.GUARD_STANDOFF:
                        sx, sy = q[0], q[1]
                        break
            if sx is not None:
                r = self.mv.goto(sx, sy, None, tol=4.0, timeout=300,
                                 abort=lambda st: st.combat or bool(self.attackers(st)))
                if r != "ok":
                    return "abort"
        log(f"gather: elite {sorted(guards)} guards it; waiting {self.GUARD_STANDOFF:.0f} yd off until it walks away")
        t0 = seen_t = time.time()
        seen = False
        face_t = 0.0
        while time.time() - t0 < self.GUARD_WAIT_S:
            if os.path.exists(STOP):
                return "abort"
            s = self.s()
            if s.combat or self.attackers(s) or s.dead:
                return "abort"
            # plates only show what the camera looks at: face the chest, else a guard beside it is "gone" when it
            # is only off screen (inst1 05:08:49-05:09:00: clear after 6 s, went in, saw it again, backed off)
            if time.time() - face_t > 5.0:
                face_t = time.time()
                turn = wrap(bearing(s.x, s.y, pos[0], pos[1]) - s.facing)
                if abs(turn) > 0.3:
                    self.mv.turn_run(turn, maxt=1.0)
                    continue
            if any(p.elite and p.npc in guards and not p.dead for p in s.plates):
                seen, seen_t = True, time.time()
            elif time.time() - seen_t >= (self.GUARD_CLEAR_SEEN_S if seen else self.GUARD_CLEAR_S):
                log(f"gather: no guard in view for {self.GUARD_CLEAR_S:.0f}s (seen={seen}); going in")
                return "clear"
            time.sleep(0.5)
        return "seen"

    def gather_object(self, go, pos, why, qid, key):
        """Loot a quest chest: walk next to it, then _open_object. Success = a free bag slot used or the
        quest's item counter moved. Failures back off 2 min -> 10 min -> 1 h."""
        log(f"quest: {why} (object {go} at {pos[0]:.0f},{pos[1]:.0f})")
        now = time.time()
        guards = self.elite_guards(pos)
        # a guard we already had to fight (it caught us waiting or walking in: Old Icebeard's patrol reaches 90 yd
        # from the Meat Locker; inst3 04:53 fled him at 78% hp and died 28 yd on) is not waited out again soon
        # (hazards kind guard_fight, persisted: survives a restart; elite_fight.json is the old code's copy, still read)
        gf = [h for h in self.hazards.active(("guard_fight",), mine_only=True) if h.info.get("npc") in guards]
        ef = (gf[-1].info["npc"], gf[-1].until - hz.KINDS["guard_fight"][1]) if gf else None
        if ef is None and guards:
            try:
                ef = tuple(json.load(open(inst.path("elite_fight.json"))))
            except Exception:
                ef = None
        if guards and ef and ef[0] in guards and now - ef[1] < 1800:
            log(f"gather: elite {ef[0]} guarding object {go} fought us {now - ef[1]:.0f}s ago; leaving it for 60 min")
            self.qdb.skip_gather[key] = now + 3600
            self.guard_skip.set(qid, 3600, now)
            return
        # only a chest at the spot we died (its camp), not the whole 150 yd death zone: inst2 00:18 one death left all
        # four Flintfire crates 41-110 yd off "in a zone we died in" and the bot hunted an empty point for 5 min.
        # A guarded chest is not left: the guard wait below handles what killed us there
        if not guards and any(math.hypot(pos[0] - h.x, pos[1] - h.y) < min(h.r, self.GATHER_DEATH_R)
                              for h in self.hazards.active(ROUTE_KINDS, now=now, mine_only=True)):
            log(f"gather: object {go} lies in a zone we died in; leaving it for 30 min")
            self.qdb.skip_gather[key] = now + 1800
            return
        def back_off(msg):
            wait = self.gfail.fail(key, self.GATHER_BACKOFF)
            log(f"gather: {msg} (object {go}); leaving it for {wait // 60} min")
            self.qdb.skip_gather[key] = self.gfail.until(key)
        # an elite guards it (Old Icebeard, 11 elite, patrols past MacGrann's Meat Locker 8 yd from his spawn; three
        # level 9-10 bots died to him at the chest, 03:18-04:31): wait out of his reach until he walks off, then go in
        if guards:
            w = self.wait_guards_away(pos, guards)
            if w != "clear":
                if w == "seen":
                    back_off(f"elite {sorted(guards)} stayed beside it")
                return
        guard_up = lambda st: bool(guards) and any(p.elite and p.npc in guards and not p.dead for p in st.plates)
        r = self.mv.goto(pos[0], pos[1], pos[2], tol=2.5, timeout=300,
                         abort=lambda st: st.combat or bool(self.attackers(st)) or guard_up(st))
        if r == "abort":
            return
        if r != "ok":
            back_off(f"could not reach it, walk={r}")
            return
        # which objective is this object for? (the counter index in the server's objective order, QuestDB.obj_idx:
        # kill-first held for 1.12 quests but not for 97277, whose item comes before its kill)
        qd = self.qdb.q.get(qid) or {"kill": [], "items": []}
        idx = None
        for it in qd["items"]:
            if any(g == go for g, _ in self.qdb.go_src.get(it, ())):
                idx = self.qdb.obj_idx(qid, "item", it)
        time.sleep(0.3)
        s = self.s()
        bags0 = s.bags_free
        cur = {q.qid: q.counts for q in s.quests}
        c0 = cur.get(qid, ())[idx] if idx is not None and idx < len(cur.get(qid, ())) else None
        def done(st):
            if st.bags_free < bags0:
                return True
            if c0 is not None:
                now_c = {q.qid: q.counts for q in st.quests}.get(qid)
                return bool(now_c) and idx < len(now_c) and now_c[idx] != c0
            return False
        res = "none"
        for attempt in range(3):
            s = self.s()
            if self.attackers(s):                              # a mob on us: kill it first, the chest stays
                log("gather: a mob is on us; fighting first")
                return
            res = self._open_object(pos, done)
            if res in ("done", "combat") or res.startswith("err:"):
                break
            if attempt < 2:                                    # the interact never started: other side, 3 yd away
                s = self.s()
                ang = math.atan2(s.y - pos[1], s.x - pos[0]) + (0.0 if attempt == 0 else math.pi)
                dist = 3.0 if attempt == 0 else 2.5
                self.mv.goto(pos[0] + dist * math.cos(ang), pos[1] + dist * math.sin(ang), pos[2], tol=0.8,
                             timeout=20, abort=lambda st: st.combat)
        time.sleep(0.5)
        if self.s().ui_open(4):
            self.keys.press("esc")                             # close_windows only knows bits 0-3
        self.close_windows()
        s = self.s()
        if res == "done":
            self.qdb.gathered[key] = time.time()
            self.gfail.ok(key)
            counts = {q.qid: q.counts for q in s.quests}.get(qid)
            log(f"gathered from object {go}: quest {qid} counts now {counts}")
        elif res == "combat":
            log("gather: a mob joined during the attempt; fighting first")
        elif res == "err:14":
            self.qdb.gathered[key] = time.time()               # already looted / gone
            log(f"gather: object {go} is empty (looted)")
        elif res in ("err:6", "err:20", "err:21"):
            log(f"gather: cannot take it (error {res[4:]}); leaving it for 1 h")
            self.qdb.skip_gather[key] = time.time() + 3600
        elif res == "err:12":
            log(f"gather: too far from object {go} at {math.hypot(s.x - pos[0], s.y - pos[1]):.1f} yd; stepping in")
            self.mv.goto(pos[0], pos[1], pos[2], tol=0.8, timeout=20, abort=lambda st: st.combat)
        else:
            back_off(f"nothing happened at ({s.x:.0f},{s.y:.0f}), spot ({pos[0]:.0f},{pos[1]:.0f})")

    # ------------------------------------------------------------ state across restarts
    def save_state(self, force=False):
        """nb/guide_state.json: passed route steps + stuck clocks (GuideDriver.export), shelved / skipped quests with
        their level, the hunt clock, visited hunt points and blacklisted mobs. A restart used to forget all of it
        and redo the same skips. Clocks are saved as ages, so the time the bot is down does not count."""
        now = time.time()
        if not force and now - self.state_t < STATE_SAVE_S:
            return
        self.state_t = now
        for bo in (self.blacklist, self.pick_npc, self.no_offer, self.gfail, self.guard_skip, self.retry):
            bo.prune(now)                    # blocks that ran out (the tallies stay: they still escalate)
        hw = self.hunt_watch
        d = {"saved_at": now,
             "guide": (self.guide.export() if self.guide and getattr(self.guide, "steps", None)
                       else getattr(self, "_saved_guide", None) or {}),     # RXP mode: keep the old driver's progress
             "qdb": {"shelved": {str(q): [u, lvl] for q, (u, lvl) in self.qdb.shelved.items() if u > now},
                     "skip": {str(q): lvl for q, lvl in self.qdb.skip_lvl.items() if q in self.qdb.skip_q}},
             "hunt": ({"q": hw.get("q"), "mobs": sorted(hw.get("mobs") or ()), "age": now - hw["t"]}
                      if hw.get("t") else {}),
             "want_seen_age": now - self.want_seen_t if self.want_seen_t else None,
             "visited": [[k[0], k[1], t] for k, t in self.visited.items() if t > now - 240],
             "fight_costs": [list(c) for c in getattr(self, "fight_costs", ())],
             "melee_costs": [list(c) for c in getattr(self, "melee_costs", None) or ()],
             # RXP mode: escaped accepts the bot goes back for (rxp_follow._rxp_note_lost); a restart kept none
             "rxp_lost": {str(q): {k: e[k] for k in ("giver", "where", "tries")}
                          for q, e in (getattr(self, "rx_lost", None) or {}).items()},
             "blacklist": self.blacklist.to_json(now)}          # [[ghash, until]]: the pre-Backoff format
        save_state(d)

    def restore_state(self, d):
        if not d:
            return
        now = time.time()
        try:
            q = d.get("qdb") or {}
            for k, (u, lvl) in (q.get("shelved") or {}).items():
                if u > now:
                    self.qdb.shelved[int(k)] = (float(u), int(lvl))
            for k, lvl in (q.get("skip") or {}).items():
                self.qdb.skip(int(k), int(lvl))
            h = d.get("hunt") or {}
            if h.get("age") is not None:
                self.hunt_watch = {"q": h.get("q"), "mobs": frozenset(h.get("mobs") or ()), "t": now - float(h["age"])}
            if d.get("want_seen_age") is not None:
                self.want_seen_t = now - float(d["want_seen_age"])
            for x, y, t in d.get("visited") or ():
                self.visited[(int(x), int(y))] = float(t)
            self.blacklist.load(d.get("blacklist"), now, key=int)
            for lv, c in d.get("fight_costs") or ():           # the measured rest line survives a restart
                self.fight_costs.append((int(lv), float(c)))
            if getattr(self, "melee_costs", None) is not None:  # ... and the melee capacity
                for lv, c in d.get("melee_costs") or ():
                    self.melee_costs.append((int(lv), float(c)))
            if getattr(self, "rx_lost", None) is not None:
                for k, e in (d.get("rxp_lost") or {}).items():
                    self.rx_lost[int(k)] = {"giver": str(e.get("giver", "?")), "where": str(e.get("where", "?")),
                                            "tries": int(e.get("tries", 0))}
            log(f"state: restored shelved {sorted(self.qdb.shelved)} skipped {sorted(self.qdb.skip_q)} "
                f"hunt {self.hunt_watch.get('q')} visited {len(self.visited)} blacklist {len(self.blacklist)}")
        except Exception as e:
            log(f"state: could not restore the saved state ({e!r})")

    def can_hunt(self, q, mobs, pts, level) -> bool:
        """The one 'can we pull it' gate for a hunt the guide is about to return: False when the quest is shelved /
        skipped, when every objective mob is a boss below BOSS_MIN_LEVEL, or when every target spawn is indoors
        (a quest not in INDOOR_QUESTS)."""
        if q is not None and self.qdb.is_shelved(q):
            return False
        mobs = set(mobs or ())
        if mobs and mobs <= BOSS_NPCS and level < BOSS_MIN_LEVEL:
            return False
        if q not in INDOOR_QUESTS and pts and not any(not self.indoors(p) for p in pts):
            return False
        return True

    def note_objectives(self, s):
        """Any objective counter of the live log moving is a sign of the hunted mobs (the HUNT_WAIT clock)."""
        if getattr(s, "nquests", 0) > len(s.quests):
            return                                   # log not fully read yet
        sig = tuple(sorted((q.qid, tuple(q.counts)) for q in s.quests))
        if self._obj_sig is not None and sig != self._obj_sig:
            old = dict(self._obj_sig)
            if any(q in old and old[q] != c for q, c in sig):
                self.want_seen_t = time.time()
        self._obj_sig = sig

    def mode_guide_on(self) -> bool:
        return bool(self.guide) and getattr(self.guide, "mode", "off") == "on" and not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "GUIDE_OFF"))

    def quest_step(self) -> bool:
        s = self.s()
        self.grind_ok = False
        if s.nquests > 0 and not s.quests:
            return False                     # quest log not fully read yet (after a restart): do not re-fetch quests
        plan = self.guide.plan(s, s.quests) if self.guide else self.qdb.plan(s, s.quests)
        # drop givers that offered nothing lately
        tries = 0
        while plan and plan[0] == "talk" and tries < 300 and (
                self.no_offer.blocked(plan[1]) or (s.level < 4 and not self.in_home(plan[2]))):
            if self.guide and self.guide.mode == "on":
                plan = None                  # guide only: no planner side-quests while its NPC is shelved (user rule)
                break
            tries += 1
            saved = self.qdb.starters, self.qdb.enders
            self.qdb.starters = {q: n - {plan[1]} for q, n in saved[0].items()}
            self.qdb.enders = {q: n - {plan[1]} for q, n in saved[1].items()}
            plan2 = self.qdb.plan(s, s.quests)
            self.qdb.starters, self.qdb.enders = saved
            plan = plan2
        if not plan:
            self.wanted = set()
            self.grind_ok = True
            return False
        if plan[0] == "use":
            self.use_object(plan[1], plan[2], plan[3])
            return True
        if plan[0] == "useitem":
            self.use_item_at(plan[1], plan[2], plan[3], plan[4], plan[5] if len(plan) > 5 else ())
            return True
        if plan[0] == "gather":
            self.gather_object(plan[1], plan[2], plan[3], plan[4], plan[5])
            return True
        if plan[0] == "talk" and (self.no_offer.blocked(plan[1])
                                  or (s.level < 4 and not self.in_home(plan[2]))):
            self.wanted = set()
            self.grind_ok = True
            return False                     # only shelved / far givers left: grind instead
        if plan[0] == "talk":
            self.talk(plan[1], plan[2], plan[3])
            return True
        if plan[0] != "hunt":
            # a plan kind this bot does not carry out (explore / visit / sit before QUEST_PLAN.md WP3a: QuestDB only
            # emits the ones in qdb.can_do): grind this tick rather than unpack it as a hunt
            if getattr(self, "_odd_plan", None) != plan[:2]:
                self._odd_plan = plan[:2]
                log(f"quest: plan kind {plan[0]!r} ({plan[3] if len(plan) > 3 else ''}) not supported; grinding")
            self.wanted = set()
            self.grind_ok = True
            return False
        _, mobs, pts, why = plan
        if mobs and set(mobs) <= BOSS_NPCS and s.level < BOSS_MIN_LEVEL:
            # the guide gates this in can_hunt, the planner (guide off / deferred) does not: a boss we may not pull
            # yet (boss_ready) kept in sight would restart the HUNT_WAIT clock forever. Shelve it, do something else
            log(f"quest: {why} - boss below level {BOSS_MIN_LEVEL}; shelving it for {HUNT_SHELVE // 60} min")
            self.wanted = set()
            if self.qdb.focus is None:
                self.grind_ok = True
                return False                 # nothing to shelve: grind this tick
            self.qdb.shelve(self.qdb.focus, HUNT_SHELVE, s.level)
            self.qdb.focus = None
            return True
        if mobs != self.wanted:
            log(f"quest: hunting {sorted(self.qdb.names.get(m, m) for m in mobs)} for {why}")
        if self.local_only(s):
            pts = [p for p in pts if self.in_home(p)] or pts       # prefer home, but the quest must be finished
        # single non-elite targets worth a trip underground (a level 5 troll with a 100% quest drop): go in
        # whenever the quest comes up in the plan, at any level; roam()'s 'something far stronger ahead'
        # abort still applies
        indoor_ok = self.qdb.focus in INDOOR_QUESTS
        if not indoor_ok and not any(not self.indoors(p) for p in pts):
            # every target is inside a cave/building (a boss in Frostmane Hold ...): skip the quest
            log(f"quest: all targets of {why} are indoors; skipping it until the next level")
            if self.qdb.focus is not None:
                self.qdb.skip(self.qdb.focus, s.level)
                self.qdb.focus = None
                self.save_state(force=True)
            self.wanted = set()
            return True
        # wait at the spawn for the target to (re)spawn, but only HUNT_WAIT seconds: a wanted mob that never
        # shows up while we stand at its spawn points means the quest is shelved for HUNT_SHELVE s.
        # The clock also restarts on want_seen_t: the main loop's pick_target() engages the mobs before
        # quest_step runs, so kills there never reached this check (nb.log 15:22:44 shelved Stocking Jetsteam
        # after 7 kills while its counter went 256->260; inst4 16:01:08 The Troll Cave after 10 whelps in 4 min)
        hw = self.hunt_watch
        q = self.qdb.focus
        now_ = time.time()
        sig = frozenset(mobs)
        if hw.get("q") != q or hw.get("mobs") != sig:           # a new quest or a new objective: the wait starts over
            hw.clear(); hw.update(q=q, t=now_, mobs=sig)
        at_spawn = any(math.hypot(p[0] - s.x, p[1] - s.y) < 35 for p in pts)
        if any(pl.npc in mobs and not pl.dead for pl in s.plates):
            self.want_seen_t = now_
        if not at_spawn or s.combat:
            hw["t"] = now_                                   # travelling or fighting: the clock restarts
        elif q is not None and now_ - max(hw["t"], self.want_seen_t) > HUNT_WAIT:
            log(f"quest: {why} - no target seen for {HUNT_WAIT // 60} min at its spawn; shelving it for {HUNT_SHELVE // 60} min")
            self.qdb.shelve(q, HUNT_SHELVE, s.level)
            self.qdb.focus = None
            self.wanted = set()
            hw.clear()
            self.save_state(force=True)
            return True
        self.wanted = set(mobs) | set(getattr(self.guide, "opt", ()) if self.mode_guide_on() else ())
        near = [p for p in pts if math.hypot(p[0] - s.x, p[1] - s.y) < 700]
        self.roam(near or pts, indoor_ok=indoor_ok)    # nothing close: travel to where the objective mobs are
        return True

    def track_handins(self, s):
        """A finished quest that left the log was handed in, whichever step did it (a trainer or vendor visit
        too): record it, or the guide asks for it again (2026-10-14: Archaic Rune went in at Teo during a
        training visit and the route then walked to Sten to 'pick it up')."""
        if getattr(s, "nquests", 0) > len(s.quests):
            return                                   # log not fully read yet
        ids = {q.qid: q.complete for q in s.quests}
        last = getattr(self, "_last_log", None)
        self._last_log = ids
        if last is None:
            return
        gone = {q for q, done in last.items() if done and q not in ids} - self.qdb.done
        if gone:
            self.qdb.done |= gone
            self.qdb.save()
            log(f"quest log: {sorted(gone)} handed in")

    def timed(self, label, fn, *a):
        """Run a step and name it in the log when it eats time (idle hunting)."""
        t = time.time()
        r = fn(*a)
        if time.time() - t > 1.5:
            log(f"slow {label}: {time.time() - t:.1f}s -> {r}")
        return r

    # ------------------------------------------------------------ main
    def run(self, goal_level=10):
        log("nb bot start")
        # the food / drink and vendor tables (one-off DB reads) before the first vendor stop needs them: built there,
        # bot 2 22:31 stood 85 s at Farmer Saldean while four bots' startup queries held the DB up
        import threading
        threading.Thread(target=lambda: swallow_call(lambda: (stock.item_table(), stock.vendor_items())),
                         daemon=True, name="stock-warm").start()
        while not os.path.exists(STOP):
            try:
                if not self.ensure_window():
                    time.sleep(5); continue
                s = self.s()
                if self.eye.gap > 20:                # back from a login/loading screen: the client may be new
                    self.pad_claimed = False
                    self.eye.gap = 0.0
                if not getattr(self, "cvars_set", False) and time.time() > getattr(self, "cvars_retry_t", 0.0):
                    # autoLootDefault: a new account (inst4 22:5x) has it off, the loot window stayed open and the
                    # bot's Esc closed it with the items still in it. autoInteract (Click-to-Move): the interact key
                    # runs to its target - a corpse killed at range lay 5-10 yd off and never opened ("noopen")
                    self.cvars_set = True
                    r = ev("(function() SetCVar('autoLootDefault',1) SetCVar('autoInteract',1) "
                           "return GetCVar('autoLootDefault')..GetCVar('autoInteract') end)()", self.eye, self.keys)
                    log(f"cvars: autoLootDefault+autoInteract -> {r!r}")
                    if "11" not in str(r):
                        self.cvars_set, self.cvars_retry_t = False, time.time() + 60   # no answer (inst3 23:27:22)
                # the claim stands still ~35 s: under attack fight first on keys (the claim-failed path), claim after
                # (inst3 00:29:26-00:30:01: a wolf took it to 1% during the claim after a restart, dead at 00:30:03)
                if not self.pad_claimed and time.time() - self.pad_claim_t > 20 \
                        and not (s.combat or self.attackers(s)):
                    self.claim_pad(); continue
                if goal_level <= s.level <= 80:
                    s2 = (time.sleep(0.5), self.s())[1]   # one torn strip frame read level 130561 (inst2 18:09:33)
                    if s2.level == s.level:
                        log(f"level {s.level} reached")
                        break
                if s.dead or s.ghost:
                    if self.rxp_mode:
                        self.rxp_note_death(s)       # deaths per RXP step (rxp_policy.DEATH_SKIP)
                    self.on_death(); continue
                if not self.rxp_mode:
                    self.rxp_flag_guard(s)           # a BotEyeDB.rxp left on would make BotEye's handlers yield
                    self.track_handins(s)            # qdb.done bookkeeping is the old driver's (RXP tracks its own)
                self.note_objectives(s)
                self.save_state()
                if s.ui_open(1) or s.ui_open(0) or s.ui_open(2) or s.ui_open(3):
                    time.sleep(0.5)
                    self.close_windows()
                if s.has_target and s.t_dead and not self.attackers(s):
                    if not self.corpses:
                        self.corpses.append(Kill(s.t, s.t_npc, s.t_hash, s.t_level, "seen", 0, 0.0, s.x, s.y, s.facing,
                                                 self.fight_no))
                    self.loot(); continue
                if (s.combat or self.attackers(s)) and self.lingering_combat(s):
                    # the combat flag outlives the fight, nothing is on us, resources under the pull line: rest (its
                    # lingering-combat grace), never a fresh pull from here (inst4 10:49:52-10:50:28: a kill at 62% hp
                    # / 15% mana, the next tick in this branch picked a boar busy with our Stoneclaw Totem and pulled
                    # it at 17% mana; dead 22 s later)
                    self.nopick_t = 0.0
                    self.timed("rest", self.rest, True)
                    time.sleep(0.2)
                    continue
                if s.combat or self.attackers(s):
                    picked = self.pick_target()
                    if not picked:
                        if not self.nopick_t:
                            self.nopick_t = time.time()
                        elif time.time() - self.nopick_t > 2.5:      # unseen / unpickable attacker: look for it
                            picked = self.find_attacker(self.nopick_t)
                    if picked:
                        self.nopick_t = 0.0
                        r = self.fight()
                        log(f"fight -> {r}")
                        if r == "killed":
                            self.loot()
                        elif r == "dead":
                            continue
                    else:
                        time.sleep(0.3)
                    continue
                self.nopick_t = 0.0
                # route costs: our death / flee / danger circles and the other bots' deaths (nb/hazards.py)
                self.mv.zones = self.hazards.cost_zones(z=getattr(s, "z", 0.0) or 0.0, extra=self.elite_zones(s))
                strong = threat.stronger(s.plates, s.level, on_screen=False)   # far stronger mobs about: stay away
                if strong and not self.hazards.covering(s.x, s.y, ROUTE_KINDS, mine_only=True):
                    p = strong[0]
                    log(f"danger: level {p.level} npc {p.npc} near ({s.x:.0f},{s.y:.0f}); avoiding the area")
                    self.hazards.add("danger", s.x, s.y)       # once, not per tick (50 yd, 20 min)
                if self.chain_ready(s):
                    # just looted, hp / mana at the pull line, a valid mob in view: pull it now (REPORT.md change 6)
                    self.loot_done_t = 0.0           # one chain pull per loot
                    if self.timed("pick", self.pick_target):
                        log("chain pull: next mob in view right after the loot")
                        r = self.fight()
                        log(f"fight -> {r}")
                        if r == "killed":
                            self.loot()
                        continue
                self.timed("rest", self.rest)
                if self.timed("imbue", self.imbue):
                    continue
                if self.timed("pet", self.pet_step):
                    continue
                if self.timed("tame", self.tame_step):
                    continue
                if self.timed("buffs", self.buffs):
                    continue
                if self.timed("train", self.train_step):
                    continue
                if self.timed("mail", self.mail_step):
                    continue
                if self.timed("vendor", self.vendor_step):
                    continue
                st = self.s()
                if self.walk_rest and not self.ready_to_pull(st) and self.pull_in_view(st):
                    self.wait_ready("a pull in view")    # walked rest: finish it here, short of the mob, then pull
                if self.timed("pick", self.pick_target):
                    r = self.fight()
                    log(f"fight -> {r}")
                    if r == "killed":
                        self.loot()
                    continue
                # RXP mode: RestedXP's current step decides (nb/rxp_follow.py); else the old guide driver / planner
                if not (self.rxp_step() if self.rxp_mode else self.quest_step()):
                    self.roam()
            except RuntimeError as e:
                log(f"error: {e}")
                self.keys.release_all()
                time.sleep(10)
        self.halt_inputs()
        self.save_state(force=True)
        log("nb bot stopped")

    def halt_inputs(self):
        """Stick centred, keys up: a bot that ends with the pad still pushed leaves the character running on its own
        (bot 3 18:34:27, killed mid-walk by a restart: ran ~760 yd to Sentinel Hill and died with no bot running)."""
        try:
            self.mv.stop()
        except Exception as e:
            swallowed("bot.halt_inputs_pad", e)
        try:
            self.keys.release_all()
        except Exception as e:
            swallowed("bot.halt_inputs_keys", e)


def swallow_call(f):
    try:
        f()
    except Exception as e:
        swallowed("bot.warm", e, log)


def single_instance():
    """OS lock on nb/bot.lock: a second bot exits instead of fighting the first
    over the keys and the pad (three copies ran in circles on 2026-10-04)."""
    import msvcrt
    f = open(inst.path("bot.lock"), "a+")
    f.seek(0)
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("another nb bot is running; exiting")
        sys.exit(1)
    return f                                    # keep open for the process lifetime


if __name__ == "__main__":
    _lock = single_instance()
    import faulthandler      # where does the main thread sit when the character stands still?
    faulthandler.dump_traceback_later(30, repeat=True, file=open(inst.path("stack.log"), "w"))
    if os.path.exists(STOP):
        os.remove(STOP)
    import nb.rxp_follow as _rf
    _rf.PERSIST = True       # step death counts to rx_deaths.json (tests leave it off: no writes into a live bot's dir)
    hz.PERSIST = True        # shared hazards (deaths) to / from nb/hazards.json (tests: memory only)
    _bot = Bot()
    try:
        _bot.run(int(inst.args()[0]) if inst.args() else 10)
    finally:
        _bot.halt_inputs()   # a crash too (a forced kill runs nothing: restarts make a STOP file and wait first)
