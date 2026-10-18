"""Level-1+ mage grinding bot for WoW 1.12.1 on the local VMaNGOS server.

Perception is pure memory reading (object manager, descriptors). Actions are
background key presses (spells on the action bar) plus click-to-move writes.

Default Human Mage action bar:  1 Attack  2 Fireball  3 Frost Armor
                                - water   = food

Stop it with Ctrl+C, or by creating a file named STOP next to this script.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import struct
import sys
import time

import dangers
import gear
import classes
import consumables
import factions
import offsets as O
import routes
import services
import events
import spells
import worlddb
from combat import Combat, add_radius
from questing import QuestRunner
from ctm import (ClickToMove, CTM_ACTION, CTM_DEST, CTM_GUID)

CTM_INTERACT_NPC = 5          # verified: opens gossip/trainer/vendor windows
from game import Game, Unit
import bridge as br
from bridge import Bridge
from keys import GuardedKeyboard, NotInWorld
from wowmem import MemoryError_, Process


def seg_dist(p, a, c) -> float:
    """Distance from point p to the segment a-c (2-D)."""
    ax, ay = a; cx, cy = c; px, py = p
    dx, dy = cx - ax, cy - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-9:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.dist(p, (ax + t * dx, ay + t * dy))

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_FILE = os.path.join(HERE, "STOP")
STOP_SAFE_FILE = os.path.join(HERE, "STOP_SAFE")

KEY_WATER, KEY_FOOD = "-", "="
MAP_EASTERN_KINGDOMS = 0
SPIRIT_HEALER_BELOW_LEVEL = 10  # no resurrection sickness below this level
PULL_RANGE = 45.0             # do not start on something further off than this: a crocolisk
                              # pulled from 76 yd collected its pair on the walk over
CONJURED_WATER = {5350, 2288, 2136, 3772, 8077, 8078, 8079}
CONJURED_FOOD = {5349, 1113, 1114, 1487, 8075, 8076, 22895}

# Vendor food and drink, cheapest ladder that 46-188 vendors each carry
# (checked against item_template: required_level, buy_price in copper).
# A warrior has no Conjure Food, so without this it can only sit and wait for
# natural regen - the slowest thing the bot does.
VENDOR_FOOD = [(1, 4540, 25), (5, 4541, 125), (15, 4542, 500),
               (25, 4544, 1000), (35, 4599, 2000), (45, 8950, 4000)]
VENDOR_DRINK = [(1, 159, 25), (5, 1179, 125), (15, 1205, 500), (25, 1708, 1000)]
FOOD_STOCK = 20              # buy up to this many; one rest eats 1-2
FOOD_LOW = 5                 # below this, make a trip for more
FOOD_CRITICAL = 3            # this close to empty, food outranks every other claim
                             # on the purse: a level-8 warrior with 3.18s against a
                             # 3.20s training floor walked 250 yd to Innkeeper Belm,
                             # bought nothing, and rested on natural regen until it
                             # died. Nothing we are saving for is worth that.


_LADDER_CACHE: dict[str, list] = {}


def vendor_ladder(kind: str) -> list[tuple[int, int, int]]:
    """The food or drink ladder [(required_level, entry, price)], derived from
    the world DB rather than typed out here.

    VENDOR_FOOD/VENDOR_DRINK above are the hand-written lists this replaces,
    kept only as the fallback for a run with no world DB. The food one happened
    to be complete; the drink one stopped at required_level 25, so every mana
    class drank the rank-3 tier from level 26 to 60 against a mana pool that
    grows about sixfold on the way - Moonberry Juice (35) and Morning Glory Dew
    (45) are on this server and were simply never typed in. consumables.py
    derives both and caches them for the session (about two seconds, once).
    """
    key = kind.title()
    if key not in _LADDER_CACHE:
        got = []
        try:
            got = consumables.vendor_ladder(key)
        except Exception as exc:
            log(f"  (vendor ladder lookup failed: {exc}) - using the built-in list")
        _LADDER_CACHE[key] = list(got) or (VENDOR_FOOD if key == "Food" else VENDOR_DRINK)
    return _LADDER_CACHE[key]


def edible(entries, level: int, avoid=()) -> tuple[set, set]:
    """Bag entries we may consume, as (food, drink) sets of item entries.

    The world DB answers this now - see consumables.py. What used to live
    here was a join that de-duplicated neither item_template's per-patch
    rows nor spell_template's per-build ones, so an item could come back
    with another patch's required_level attached to it.
    """
    lvl = int(level)
    return ({c.entry for c in consumables.best(entries, lvl, "Food", avoid)},
            {c.entry for c in consumables.best(entries, lvl, "Drink", avoid)})


def best_consumable(ladder, level: int):
    """(entry, unit price) of the best item on the ladder we can use."""
    ok = [x for x in ladder if x[0] <= level]
    return (ok[-1][1], ok[-1][2]) if ok else (None, 0)


def affordable_consumable(ladder, level: int, budget: int):
    """(entry, price) of the best ladder item we can use AND pay for.

    The price is per vendor purchase - a stack of 5, not one item: the
    12:26 buy went "4541 x10 (wanted 2), money 305 -> 55". Picking by level
    alone left a level-9 warrior at 55c looking only at the 125c bread,
    so it never restocked and rested on natural regen after every kill,
    while two stacks of the 25c bread were in reach. Beasts drop no coin,
    so waiting to afford the top tier meant waiting forever.
    """
    ok = [x for x in ladder if x[0] <= level and x[2] <= budget]
    return (ok[-1][1], ok[-1][2]) if ok else (None, 0)


# ------------------------------------------------------ what a trainer still owes us
# Weapon and armour proficiencies (Dual Wield at 20, Mail at 40 for a warrior,
# Plate Mail at 40) are bought from the class trainer like any other spell, but
# they never appear in the client's SPELLBOOK array - they land on a skill line
# instead, which is why gear.known_skills exists at all. Measured on the live
# level-7 dwarf: the spellbook holds Parry/Dodge/Block and not one proficiency,
# while the skill lines already hold 44/54/172/413/414/415/433. Counting "not in
# the spellbook" as "not learned" therefore makes the trainer check permanently
# true from level 20 onwards, so every town trip becomes a trainer walk that
# teaches nothing and repeats every ten minutes for the rest of the run.
PROFICIENCY_SKILLS = (set(gear.WEAPON_SKILL.values()) | set(gear.ARMOR_SKILL.values())
                      | {118})            # 118 Dual Wield has no item class of its own
# Class lines (26 Arms, 256 Fury, 6 Frost...) are deliberately NOT in this set:
# knowing the Arms line says nothing about knowing Rend.


def entry_learned(x, known_spells, known_skills, prof_skill, ledger) -> bool:
    """Is this trainer-plan entry already on the character, by any evidence?

    Three layers, deliberately redundant because each one alone has been
    wrong: the spellbook (what the client lists), the skill lines (where
    proficiencies actually live), and a ledger of entries a real visit to
    this trainer failed to teach.
    """
    sid = int(x["learns"])
    if sid in known_spells:
        return True
    sk = prof_skill.get(sid)
    if sk is not None and sk in known_skills:
        return True
    at = ledger.get(str(sid))
    # A ledger row is only trusted while it still describes the same character:
    # an entry recorded as unlearnable below its own required level was recorded
    # against a plan/server disagreement, and levelling clears it.
    return at is not None and int(at) >= int(x["level"])


def due_training(plan, level, money, known_spells, known_skills, prof_skill, ledger) -> list:
    """Plan entries we could walk in and learn right now, cheapest first."""
    return sorted((x for x in plan
                   if x["level"] <= level and x["cost"] <= money
                   and not entry_learned(x, known_spells, known_skills, prof_skill, ledger)),
                  key=lambda x: (x["level"], x["cost"]))


# ------------------------------------------------------------- how fast we travel
class SpeedModel:
    """Measured ground speed in yards per second, per (continent, mounted).

    taxi.RUN_SPEED 7.0 divided by a 1.25 "detour" factor was the project's
    entire notion of how long a walk takes - one number, guessed once, for
    every zone, every level and every class. Replaying 556 clean legs out of
    data/events.jsonl puts the truth at 5.26 yd/s against the 5.6 the constants
    imply, and at 4.81 on the 600-1500 yd legs where a flight starts to
    compete: the constant is optimistic exactly where the decision is close.
    A mount at 40 then makes it ~8.5 and at 60 ~10.6, which no constant can
    express at all. So measure it. Every finished navigate is one sample of
    (yards actually covered) / (seconds it took), and legs that FAILED are
    samples too - a leg that ran into three Rockjaw really did cost that.

    The average is exponential with a half life counted in seconds of travel,
    not wall clock, so an hour of standing in a town does not age it and a
    single leg can only move it by a few percent.
    """

    SEED = 5.6                  # cold start = the old RUN_SPEED / GROUND_DETOUR
    LO, HI = 2.5, 20.0          # a sample outside this is a stuck leg or a teleport
    HALF_LIFE = 1800.0          # seconds of travel
    MIN_SECS, MIN_YARDS = 5.0, 25.0   # shorter legs are all overhead, not speed
    MAX_WEIGHT = 0.15           # no single leg may rewrite the estimate: one
                                # half-hour trip would otherwise carry half the
                                # weight of everything before it

    def __init__(self):
        self.v: dict = {}
        self.n: dict = {}

    def speed(self, key) -> float:
        return self.v.get(key, self.SEED)

    def sample(self, key, yards: float, secs: float) -> float | None:
        """Fold one finished leg in. Returns the sample, or None if ignored."""
        if secs < self.MIN_SECS or yards < self.MIN_YARDS:
            return None
        v = min(max(yards / secs, self.LO), self.HI)
        cur = self.v.get(key, self.SEED)
        w = min(1.0 - 0.5 ** (min(secs, self.HALF_LIFE) / self.HALF_LIFE), self.MAX_WEIGHT)
        self.v[key] = cur * (1.0 - w) + v * w
        self.n[key] = self.n.get(key, 0) + 1
        return v


def best_flight(sources, dests, known_ids, eff_speed, on_foot, route_seconds,
                takeoff: float = 6.0):
    """Cheapest (seconds, source, destination, air time) over flight masters we
    could walk to crossed with flight points we know - or None when walking wins.

    sources: [(node id, yards to walk there, label)]
    dests:   [(node id, label, yards from its landing pad to the destination)]

    try_flight used to evaluate exactly ONE source, nearest_service's first
    result, although it asked for twelve - so one blacklisted or awkwardly
    placed flight master took flying away from a whole area. And it refused
    any source further than a flat 1500 yd before computing anything, which
    rejects an 1800 yd walk to Gadgetzan that saves a 12000 yd run: a 6:1 win
    thrown away by a constant. The correct bound is not a yardage, it is the
    trip itself - a source cannot help once walking to it already costs more
    than walking the whole way. Sources are tried nearest first so that bound
    tightens as we go and the loop stays cheap.
    """
    best = None
    for snid, s_walk, s_name in sorted(sources, key=lambda s: s[1]):
        if snid is None:
            continue
        s_secs = s_walk / eff_speed + takeoff
        if s_secs >= on_foot or (best is not None and s_secs >= best[0]):
            break                       # nearest-first: no later source can win either
        for dnid, d_name, d_walk in dests:
            if dnid is None or dnid == snid:
                continue
            air = route_seconds(snid, dnid, known_ids)
            if air is None:
                continue
            total = s_secs + air + d_walk / eff_speed
            if total < on_foot and (best is None or total < best[0]):
                best = (total, s_name, d_name, air)
    return best


def should_mount(level, mount_level, trip_secs, *, mounted, in_combat, ghost,
                 hostiles_near, min_secs=20.0) -> bool:
    """Summon the mount for this leg? Pure so it can be asserted over every
    state the bot can be in - below the mount level it must be False for all
    of them, because none of this can be tried in game until 40."""
    return (level >= mount_level and trip_secs >= min_secs and not mounted
            and not in_combat and not ghost and not hostiles_near)


BAG_SLOTS = 16


STATUS_FILE = os.path.join(HERE, "status.json")
DEATH_FILE = os.path.join(HERE, "last_death.json")
DEATH_TRAP_MINUTES = 20
REZ_SICKNESS = 15007        # -75% to every stat for up to 10 min
LOG_TAIL: list[str] = []
STATE = {"state": "starting"}


def log(msg: str):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG_TAIL.append(line)
    del LOG_TAIL[:-14]


def set_state(state: str, **extra):
    if STATE.get("state") != state:
        events.emit("state", s=state)
    STATE.clear()
    STATE.update(state=state, **extra)


class MageBot(Combat, QuestRunner):
    def __init__(self, args):
        self.args = args
        self.p = Process(writable=True)
        self.g = Game(self.p)
        if not self.g.in_world():
            log("waiting for the character to enter the world (log in first)")
            while not self.g.in_world() and not os.path.exists(STOP_FILE):
                time.sleep(3)
            if not self.g.in_world():
                log("stopped before entering the world")
                raise SystemExit(0)
            time.sleep(5)
        self.ctm = ClickToMove(self.p)
        self.kb = GuardedKeyboard(self.p.hwnd, self.g.in_world)
        self.bridge = Bridge(self.kb)
        self.ctm.apply_timer_fix()
        steer_game = Game(self.p)                  # own instance: used from the steering thread

        def _player_base():
            try:
                return steer_game.player().base if steer_game.in_world() else None
            except Exception:
                return None
        self.ctm.start_steering(_player_base)
        try:
            from navmesh import NavMesh
            self.nav = NavMesh()
            log(f"navmesh: {len(self.nav.tiles)} tiles loaded")
            self.navs = {0: self.nav}
            self.map_id = 0
        except Exception as e:                      # no tiles built yet
            self.nav = None
            log(f"navmesh unavailable ({e}) - straight-line movement only")
        me = self.g.player()
        self.profile = classes.load(me.class_id)
        self.route = None if args.here else routes.load(args.route)
        self.stage = None
        self.home = (me.x, me.y, me.z)
        self.radius = args.radius
        self.stage_mobs: set[int] = set()
        self.cd: dict[str, float] = {}
        self._path, self._path_t = [], 0.0
        self.next_trip = 0.0
        self.load_obstacles()
        self.load_npc_bans()
        self.install_lua_helpers()
        # The bot drives raw action ids (UseAction), which are page 1. The
        # keys are not: with the bar on another page the rotation fires
        # correctly while "3" gives the user Stoneform and the bar on
        # screen shows the racials. Put the visible bar back on page 1.
        self.bridge.send(br.BAR_PAGE_1, wait=0.4)
        # spells learned in an earlier session may never have been placed
        # (Frostbolt sat in the spellbook while the bot cast Fireball)
        known_now = spells.known_by_name(self.p)
        bar_now = spells.action_bar(self.p)
        want_bonus = any(bar_now[self.BONUS_BAR_OFFSET:self.BONUS_BAR_OFFSET + 12])
        missing = [n for n in self.profile.bar()
                   if not self.has(n) and n in known_now]
        # also re-place when the stance bar has drifted: that is the bar the
        # player actually sees and types on
        if not missing and want_bonus:
            ids = {v for v in bar_now[self.BONUS_BAR_OFFSET:self.BONUS_BAR_OFFSET + 12] if v}
            missing = [n for n, s in self.profile.bar().items()
                       if n in known_now and br.spell_id(n) not in ids]
        if missing:
            log(f"action bar incomplete ({', '.join(missing)}) - placing known spells")
            self.setup_action_bar()
        if self.equip_spare_bags():
            log(f"bags in the backpack equipped: {self.g.bag_capacity()} slots, "
                f"{self.g.free_bag_slots()} bag slots free")
        self.guide_active = bool(getattr(args, "guide", None))
        if self.guide_active:
            self.setup_guide(args.guide)
        self.blacklist: dict[int, float] = {}   # guid -> until
        self.pack_mobs: dict[int, float] = {}   # entry -> until: kinds that swarm us
        self.pack_level: dict[int, int] = {}    # entry -> our level when it swarmed us
        # per character (a pack at level 3 is no pack for another char at 8);
        # survives restarts (murlocs, three times)
        self.pack_file = os.path.join(HERE, "data", f"pack_mobs_{self.g.player_name()}.json")
        try:
            for k, v in json.load(open(self.pack_file)).items():
                until, lvl = (v, 0) if isinstance(v, (int, float)) else v
                if float(until) > time.time():
                    self.pack_mobs[int(k)] = float(until)
                    self.pack_level[int(k)] = int(lvl)
        except Exception:
            pass
        self.kills = 0
        self.looted = 0
        self.deaths = 0
        self.start_xp, self.start_level = me.xp, me.level
        if not self.profile.ranged and self.args.pull_hp == 65:
            # the 65% default is the mage's: a warrior loses 30-45% to one
            # mob of its level and has no root to leave with, so a pull at
            # 72% that brought an add was death at 12:16 (Leper Gnomes)
            self.args.pull_hp = 80
        log(f"class profile: {self.profile.name} ({self.profile.power}, "
            f"{'ranged' if self.profile.ranged else 'melee'})")
        events.emit("start", char=self.g.player_name(), lvl=me.level, xp=me.xp,
                    x=round(me.x, 1), y=round(me.y, 1), args=sys.argv[1:])
        log(f"started at ({me.x:.1f}, {me.y:.1f})"
            + (f"; route: {self.route['name']}" if self.route else f"; farming here, {args.radius} yd"))

    # ------------------------------------------------------------ helpers

    def save_packs(self):
        try:
            json.dump({k: [v, self.pack_level.get(k, 0)] for k, v in self.pack_mobs.items()},
                      open(self.pack_file, "w"))
        except Exception:
            pass

    def stopped(self) -> bool:
        if os.path.exists(STOP_FILE):
            return True
        # STOP_SAFE: stop at the next calm moment (for restarts - never mid-fight)
        if os.path.exists(STOP_SAFE_FILE):
            try:
                me = self.g.player()
                calm = not me.in_combat and not me.dead and not me.ghost                     and me.hp_pct >= 85 and me.mana_pct >= 60                     and not any(me.dist(t) < 30 for t in self.aggressors())
                if calm:
                    os.remove(STOP_SAFE_FILE)
                    open(STOP_FILE, "w").close()
                    return True
            except Exception:
                pass
        return False

    def set_target(self, guid: int):
        self.p.write_u64(O.TARGET_GUID, guid)

    def wait_until_still(self, timeout=2.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.ctm.moving and not self.g.player().moving:
                return True
            time.sleep(0.03)
        return False

    def stop(self):
        self.ctm.stop(self.g.player())
        self.wait_until_still(1.0)

    def halt(self):
        """Hard stop: a glitched interaction leaves the client running forward
        like autorun, which click-to-move orders do not cancel - a tap on a
        movement key does."""
        self.p.write_u32(CTM_ACTION, 12)
        self.kb.press("s", hold=0.1)
        self.ctm.stop(self.g.player())
        self.wait_until_still(1.0)

    def walk_to(self, x, y, z, stop_short=0.0, timeout=20.0) -> bool:
        """Walk until CTM arrives on its own (clean stop packet)."""
        t0 = time.time()
        self.ctm.move_toward(self.g.player(), x, y, z, stop_short)
        while time.time() - t0 < timeout and not self.stopped():
            if not self.ctm.moving and not self.g.player().moving:
                return True
            time.sleep(0.05)
        self.stop()
        return False

    # --- keeping a goal we cannot reach from coming back every few minutes ---

    def _goal_key(self, x, y, z) -> tuple[int, int, int]:
        """One 5-yd cell: the same errand asked for twice is never the same
        float twice (a spawn point wanders, stop_short moves the target)."""
        return (int(x // 5), int(y // 5), int(z // 5))

    # A leg that ends without arriving is usually not the terrain's fault. Of
    # 2,853 recorded outer legs, counting every failure refuses 1,485 of them
    # (52%) and 50 of those refusals are legs the character ACTUALLY WALKED to
    # the end - because most failures are a fight on the way, a timeout, a
    # stop request or a tram that did not come. Only these reasons are
    # evidence that the place cannot be walked to.
    ROUTE_FAILS = ("no_route", "no_mesh_path", "too_far_to_walk_blind", "wedged")
    GOAL_WINDOW = 1800.0   # a refusal decays: the same shape as _bad_goals
                           # (5-20 min), not the rest of the run
    GOAL_SPREAD = 120.0    # three failures inside one minute is one bad minute,
                           # not a place we cannot reach

    def note_goal_failed(self, x, y, z, reason=None) -> None:
        """One more leg that ended without arriving *for a routing reason*.

        `reason` must name one of ROUTE_FAILS; anything else (combat, timeout,
        a stop request, a boat that did not come) is not evidence about the
        ground and is dropped on the floor. In memory only: a fresh run
        deserves a fresh try."""
        if reason not in self.ROUTE_FAILS:
            return
        d = self.__dict__.setdefault("_dead_goals", {})
        t = d.setdefault(self._goal_key(x, y, z), [])
        t.append((time.time(), float(x), float(y), float(z)))
        del t[:-8]

    def goal_fail_times(self, x, y, z, window=None, near=5.0) -> list:
        """When the legs to (about) this point failed, inside `window`.

        The cell is an index, not the test: two asks for the same errand 2.3 yd
        apart fall either side of a cell edge (-6539.5 and -6541 do), so the
        neighbouring cells are scanned and the real distance decides."""
        d = self.__dict__.setdefault("_dead_goals", {})
        if not d:
            return []
        if window is None:
            window = self.GOAL_WINDOW
        kx, ky, kz = self._goal_key(x, y, z)
        now, hits = time.time(), []
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                for c in (-1, 0, 1):
                    for ts, fx, fy, fz in d.get((kx + a, ky + b, kz + c), ()):
                        if now - ts < window and math.dist((fx, fy, fz), (x, y, z)) <= near:
                            hits.append(ts)
        return hits

    def goal_fails(self, x, y, z, window=None, near=5.0) -> int:
        """How many legs to (about) this point failed inside `window`."""
        return len(self.goal_fail_times(x, y, z, window, near))

    def goal_is_dead(self, x, y, z, n=3, window=None) -> bool:
        """True once this spot has refused us `n` times for a routing reason,
        inside `window`, and the failures are spread over GOAL_SPREAD seconds.

        The spread matters: three refusals in the same fifteen seconds are one
        bad moment (a mob parked on the route, a re-plan storm). Three spread
        over two minutes is a place."""
        t = self.goal_fail_times(x, y, z, window)
        return len(t) >= n and (max(t) - min(t)) >= self.GOAL_SPREAD

    def flight_answers(self, x, y, z) -> bool:
        """Is there a flight landing we already know within 300 yd of the goal?
        Arithmetic only - no navmesh search - because this runs before every
        leg. A dead goal with a flight beside it is still worth one more try:
        the walk that failed was from the wrong side."""
        try:
            known = set(self.flights_known())
            if not known:
                return False
            for name, (_code, land) in self.taxi_nodes_here().items():
                if name in known and math.dist(land[:2], (x, y)) < 300.0:
                    return True
        except Exception:
            return False
        return False

    # Group A adds proven=/relax_soft=/dz= to these navmesh methods. Until it
    # lands (or if it never does) the older signatures must keep working: the
    # hint is dropped and the call repeated. Nothing has happened yet when a
    # call fails to bind its arguments, so the retry is safe.
    NAV_HINTS = ("proven", "relax_soft", "dz")

    def nav_call(self, name, *a, **kw):
        fn = getattr(self.nav, name)
        try:
            return fn(*a, **kw)
        except TypeError as e:
            msg = str(e)
            drop = [k for k in self.NAV_HINTS if k in kw and f"'{k}'" in msg]
            if not drop:
                raise
            return fn(*a, **{k: v for k, v in kw.items() if k not in drop})

    def navigate(self, x, y, z, stop_short=0.0, timeout=180.0, combat_ok=False,
                 careful=False, via_portal=False, dest_map=None) -> bool:
        """Walk to a point along a navmesh path (see _navigate). Publishes the
        goal and the current path for the overlay while walking."""
        outer = getattr(self, "_nav_goal", None) is None
        if outer and self.goal_is_dead(x, y, z) and not self.flight_answers(x, y, z):
            # Three legs that ended with NO ROUTE (not "a mob interrupted us"),
            # spread over minutes, and no flight lands near it: the errand is
            # not walkable from here just now. Refuse it until the refusal
            # decays - GOAL_WINDOW, not the rest of the run.
            t = self.goal_fail_times(x, y, z)
            log(f"  ({x:.0f}, {y:.0f}, z{z:.0f}) found no route {len(t)} times in the last "
                f"{(max(t) - min(t)) / 60:.0f} min and no flight lands near it - "
                f"not trying again for a while")
            events.emit("goal_dead", x=round(x, 1), y=round(y, 1), z=round(z, 1),
                        fails=len(t), spread=round(max(t) - min(t)))
            return False
        ok = False
        if outer:
            self._nav_goal = (x, y, z)
            self._nav_fail = None          # why this leg failed, if it does:
                                           # only a routing reason is evidence
                                           # about the ground (see ROUTE_FAILS)
            self._detours = 0
            t_nav = time.time()
            me0 = self.g.player()
            d0 = math.dist((me0.x, me0.y), (x, y))
            events.emit("nav", to=[round(x, 1), round(y, 1), round(z, 1)],
                        at=[round(me0.x, 1), round(me0.y, 1), round(me0.z, 1)],
                        dist=round(d0), stop_short=stop_short,
                        careful=careful)
            self.mount_up(me0, self.ground_seconds(d0))
            key0 = self.speed_key()
            self._leg_teleported = False       # a flight, tram or portal on this leg
                                               # covers ground at no walking speed
        try:
            if dest_map is not None and dest_map in (0, 1) and dest_map != self.continent():
                if not self.cross_sea(x, y, z, dest_map, timeout):
                    return False
                timeout = max(timeout, 600.0)
            ok = self._navigate(x, y, z, stop_short, timeout, combat_ok, careful, via_portal)
            return ok
        finally:
            if outer:
                left = None
                try:
                    me1 = self.g.player()
                    secs = time.time() - t_nav
                    left = math.dist((me1.x, me1.y), (x, y))
                    events.emit("nav_end", ok=bool(ok), secs=round(secs, 1),
                                left=round(left),
                                at=[round(me1.x, 1), round(me1.y, 1), round(me1.z, 1)])
                    # One more measurement of how fast this character actually
                    # covers ground. A leg that failed counts: the time was
                    # spent either way, and pricing a trip as if fights never
                    # happen is how travel came to be half the wall clock.
                    if not getattr(self, "_leg_teleported", False):
                        self.speeds().sample(key0, d0 - left, secs)
                except Exception:
                    pass
                # A leg only counts against the goal when it failed for a
                # routing reason AND ended somewhere that is not already at
                # the goal: stopping 1 yd short of a stop_short=0 errand is a
                # rounding problem, not an unreachable place.
                why = getattr(self, "_nav_fail", None)
                # os.path.exists, not stopped(): stopped() can promote
                # STOP_SAFE to STOP, and bookkeeping must not have side effects
                if (not ok and not os.path.exists(STOP_FILE)
                        and (left is None or left > max(2.0 * stop_short, 5.0))):
                    self.note_goal_failed(x, y, z, why)
                self._nav_goal = None
                self._nav_fail = None
                self._nav_path = None

    def _no_way_round(self, me, goal, path, n, nt, avoid, dz, skw):
        """The re-plan loop could not clean this route up. Decide what it means.

        Returns (path, walk_carefully, no_honest_route). `path` is None only
        when the leg has no route at all - the caller must then fail the leg
        honestly rather than fall through to a blind straight walk.

        Three cases, and only one of them is a cliff:

        * trunks only (n == 0). A tree is a cost, not a cliff: check_trees
          prices it and the route round it may simply be longer. Keep the
          route - this is what the code did before the steep work, and
          calling it a "proven climb" made both the log line and the
          telemetry wrong.
        * every climb block on it is a PREDICTION. Nothing ever failed to walk
          these faces; STEEP_GROUND is a threshold with real overlap on either
          side (see the note on STEEP_GROUND). Refusing the leg outright would
          strand the bot wherever the only route is a guess, so it is walked -
          carefully - and the log says so honestly, including the common case
          where relaxing the predictions hands back the very route that was
          rejected.
        * one of them is PROVEN: this character stood there and could not get
          up it. There is no honest route; say so and let the caller answer
          with a flight, another approach point, or skipping the step.
        """
        x, y, z = goal
        if not n:
            log(f"  route runs through {nt} tree trunk(s) and there is no way round - "
                f"keeping it (a trunk is a cost, not a cliff)")
            events.emit("trees_kept", x=round(x, 1), y=round(y, 1), trunks=nt)
            return path, False, False
        if not hasattr(self.nav, "soft_climbs"):
            # an older navmesh with no predicted/proven split: there is nothing
            # to relax and no way to tell a guess from proof. Old behaviour.
            log("  route climbs a face we could not route round - keeping it "
                "(this navmesh cannot tell a guess from proof)")
            return path, True, False
        relaxed = None
        if self.nav.soft_climbs:
            try:
                relaxed = self.nav_call("find_path", (me.x, me.y, me.z), (x, y, z),
                                        avoid=avoid, dangers=dz, holes=self.obstacles,
                                        relax_soft=True, **skw)
            except Exception as e:
                log(f"  (the relaxed re-plan failed: {e})")
        if relaxed:
            same = (path is not None and len(relaxed) == len(path)
                    and all(math.dist(p[:3], q[:3]) < 0.01 for p, q in zip(relaxed, path)))
            if same:
                # Be honest about this: it is the SAME route, and the only
                # difference from the old "no way round, keeping it" is that
                # it is now walked carefully.
                log("  every steep step on this route is a guess, and setting them aside "
                    "gives the same route back - walking it carefully")
            else:
                log("  route climbs a face we could not route round - walking a relaxed "
                    "route (predicted steep, never proved)")
            events.emit("relax_soft", x=round(x, 1), y=round(y, 1),
                        same=bool(same), pts=len(relaxed), blocked=n)
            return relaxed, True, False
        log("  the only route climbs a face we already failed to walk - no honest route")
        events.emit("no_route", x=round(x, 1), y=round(y, 1), reason="proven_climb",
                    blocked=n)
        return None, False, True

    def _navigate(self, x, y, z, stop_short=0.0, timeout=180.0, combat_ok=False,
                  careful=False, via_portal=False) -> bool:
        """Walk to a point along a navmesh path, working around obstacles.

        The navmesh only knows terrain and buildings; small doodads (crates,
        book piles, fences) can still block us. When progress stalls we:
          1. remember the blocked spot and re-plan with a heavy cost there,
          2. back off diagonally before following the new path,
          3. on repeated stalls at the same place, also try jumping.
        """
        if not self.nav:
            return self.walk_to(x, y, z, stop_short, timeout)
        # Predicted (never proved) climb blocks are session-only and time out:
        # without this an hour of cautious guesses walls the bot into a maze of
        # faces nothing ever tried to walk. A no-op on an older navmesh.
        exp = getattr(self.nav, "expire_soft", None)
        if exp is not None:
            try:
                exp(1800.0)
            except Exception:
                pass
        bad = self.__dict__.get("_bad_goals") or {}
        n, until = bad.get((round(x / 5), round(y / 5)), (0, 0.0))
        if n >= 2 and time.time() < until:
            log(f"  ({x:.0f}, {y:.0f}) wedged us {n} times before - not walking there again "
                f"for {(until - time.time()) / 60:.0f} min")
            return False
        t0 = time.time()
        avoid: list[tuple[float, float, float, float]] = []
        attempts = 0
        me = self.g.player()
        self.sync_map(me)
        if not via_portal and self.map_id == 0 and self.prefer_tram(me, (x, y, z)):
            # Walking this is slower than the tram (or there is no walk at all):
            # the tram is a candidate, but only a candidate. Price the flight
            # first - try_flight prices walk, tram and taxi in the same seconds
            # comparison, so this is no longer a mandatory branch.
            if (not combat_ok and not me.in_combat
                    and self.try_flight(me, x, y, z)):
                me = self.g.player()
                if not self.prefer_tram(me, (x, y, z)):
                    return self.navigate(x, y, z, stop_short, timeout, combat_ok, careful)
            leg = self.tram_leg(me)
            if not self.ride_tram(leg):
                return False
            return self.navigate(x, y, z, stop_short, max(timeout - (time.time() - t0), 120),
                                 combat_ok, careful, via_portal=True)
        splice_ahead = 0.0
        while time.time() - t0 < timeout and not self.stopped():
            me = self.g.player()
            if me.dead:
                # a fight inside this walk killed us: the death handler is in
                # the main loop, and the walk went on for 3 more minutes
                # (rest at 0% hp, a loot, a camera aim - 09:25-09:28)
                self._nav_fail = "dead"
                return False
            self.sync_map(me)
            if math.dist((me.x, me.y), (x, y)) <= max(stop_short, 1.0) and self._same_floor(me, z):
                self.stop()
                return True
            # No yard gate here any more: try_flight's own floor is the fixed
            # cost of flying (talk + take-off + one hop delay), which is the
            # only distance-independent reason a flight cannot win.
            if (not combat_ok and self.map_id in (0, 1) and not me.in_combat
                    and self.try_flight(me, x, y, z)):
                me = self.g.player()
                continue
            dz = self.danger_zones(me, (x, y), careful)
            if self.nav.nearest_poly(me.x, me.y, me.z)[0] is None:
                # standing off the mesh (wading in Loch Modan next to a Loch
                # Frenzy: every path failed for ten minutes and each hunting
                # ground got marked unreachable): walk back onto it first
                back = self.nav.reachable_near((me.x, me.y, me.z), (me.x, me.y, me.z), radius=60.0, tries=0)
                if back is not None and time.time() - getattr(self, "_remesh", 0) > 30:
                    self._remesh = time.time()
                    log(f"  off the navmesh - walking {math.dist(back[:2], (me.x, me.y)):.0f} yd back onto it")
                    self.walk_to(*back, 1.5, 40)
                    continue
            # a long trip (Thelsamar -> Ironforge, 2000 yd) needs more than
            # 200k nodes: scale the search with the distance
            far = math.dist((me.x, me.y), (x, y))
            skw = {"max_nodes": 1500000, "budget": 150.0} if far > 700 else {}
            # every fight on the way threw the route away and planned the
            # whole trip again: splice back onto the remembered one instead
            # Each stuck used to re-plan the whole trip (85 s on a 1800 yd
            # walk, five times in a row at Gol'Bolar). Reuse the remembered
            # route and skip further past the obstacle each time; only give
            # up on it after six tries.
            path = (self.resume_path(me, x, y, z, avoid, dz, ahead=max(30.0 * attempts, splice_ahead))
                    if attempts < 6 else None)
            splice_ahead = 0.0
            # set when the re-plan loop below declares the leg impassable: the
            # fallbacks further down must not answer that by walking blind.
            no_honest_route = False
            if path is None:
                path = self.nav.find_path((me.x, me.y, me.z), (x, y, z), avoid=avoid,
                                          dangers=dz, holes=self.obstacles, **skw)
                if not path and self.obstacles:
                    path = self.nav.find_path((me.x, me.y, me.z), (x, y, z), avoid=avoid,
                                              dangers=dz, **skw)
                n = nt = 0
                ROUNDS = 4
                for _round in range(ROUNDS):
                    last_round = _round == ROUNDS - 1
                    # a face too steep to walk up: block it and plan round it;
                    # a tree trunk on the line: plan round that too
                    try:
                        n = self.check_climbs(path) if path and self.map_on_terrain() else 0
                        nt = self.check_trees(path, avoid) if path else 0
                    except Exception as e:
                        log(f"  (checking the route failed: {e})")
                        n = nt = 0
                    if not (n or nt):
                        break                    # the route we hold is approved
                    alt = self.nav.find_path((me.x, me.y, me.z), (x, y, z), avoid=avoid,
                                             dangers=dz, holes=self.obstacles, **skw)
                    what = []
                    if n:
                        what.append(f"climbs a face too steep to walk (blocked {n} step(s))")
                    if nt:
                        what.append(f"runs through {nt} tree trunk(s)")
                    if alt and not last_round:
                        log(f"  route {' and '.join(what)} - planned round it")
                        path = alt
                        continue
                    if alt:
                        # Four rounds of re-planning and every one of them came
                        # back with new steep steps. Falling out of the loop
                        # here used to walk `path` - a route check_climbs has
                        # never approved - and print nothing at all.
                        log(f"  route {' and '.join(what)} after {ROUNDS} re-plans - "
                            f"out of re-plans")
                    else:
                        log(f"  route {' and '.join(what)} - no alternative route exists")
                    path, careful_now, dead = self._no_way_round(
                        me, (x, y, z), path, n, nt, avoid, dz, skw)
                    careful = careful or careful_now
                    no_honest_route = dead
                    break
                if path and far > 150:
                    self._path_cache = ((x, y, z), list(path), time.time())
            if not path and not via_portal:
                for trig, radius, land in self.PORTALS:
                    # a quick look only: with the 30 s budget each portal probe
                    # added a minute to every failed search (Bink: 3 min x 3)
                    if self.nav.find_path((me.x, me.y, me.z), trig, budget=4.0) and                             self.nav.find_path(land, (x, y, z), budget=4.0):
                        log(f"  taking the portal at ({trig[0]:.0f}, {trig[1]:.0f})")
                        if not self.navigate(*trig, stop_short=max(radius - 3.0, 1.0),
                                             timeout=timeout, combat_ok=combat_ok, via_portal=True):
                            return False
                        t1 = time.time()
                        while time.time() - t1 < 8:
                            me = self.g.player()
                            if math.dist((me.x, me.y), land[:2]) < 15 and abs(me.z - land[2]) < 10:
                                self._leg_teleported = True   # not walked ground
                                break
                            time.sleep(0.3)
                        else:
                            log("  the portal did not take us through")
                            return False
                        time.sleep(1.0)
                        return self.navigate(x, y, z, stop_short, max(timeout - (time.time() - t0), 30),
                                             combat_ok, careful, via_portal=True)
            if not path and not via_portal:
                # the spot itself is off the mesh (an NPC in a house whose
                # doorway the mesh does not connect - Jern Hornhelm in
                # Thelsamar): settle for the nearest reachable spot near it
                # and, last, a spot that may sit well below the goal: the shelf
                # under a cliff top is the only honest way to stand near a
                # place we cannot climb to (44 yd down, 24 yd away, on the real
                # ground). dz= is new in navmesh; older ones ignore it and this
                # is then just a wider radius.
                alt = (self.nav.reachable_near((me.x, me.y, me.z), (x, y, z), radius=12.0)
                       or self.nav.reachable_near((me.x, me.y, me.z), (x, y, z), radius=25.0)
                       or self.nav_call("reachable_near", (me.x, me.y, me.z), (x, y, z),
                                        radius=40.0, dz=45.0))
                if alt is not None:
                    gap = math.dist(alt[:2], (x, y))
                    if abs(alt[2] - z) >= 6:
                        log(f"  no route to ({x:.0f}, {y:.0f}) - nearest honest approach is "
                            f"{gap:.1f} yd away at z{alt[2]:.0f}")
                    else:
                        log(f"  no path to ({x:.0f}, {y:.0f}) - going to the reachable spot "
                            f"{gap:.1f} yd from it")
                    # this inner leg is not the outer one: it does not reset
                    # _nav_fail, so a reason it sets on the way must not be
                    # left lying around for the outer leg to pick up
                    prev_why = getattr(self, "_nav_fail", None)
                    self._nav_fail = None
                    ok = self.navigate(*alt, stop_short=1.0, timeout=max(timeout - (time.time() - t0), 60),
                                       combat_ok=combat_ok, careful=careful, via_portal=True)
                    if ok:
                        self._nav_fail = prev_why
                    if ok and gap > max(stop_short, 3.0) and abs(alt[2] - z) < 6:
                        # the rest is an unlinked floor (Steelgrill's Depot):
                        # from the doorway a straight walk gets there
                        log(f"  walking the last {gap:.0f} yd straight in")
                        return self.walk_to(x, y, z, stop_short, 25)
                    if ok and (abs(alt[2] - z) >= 6 or gap > max(stop_short, 3.0)):
                        # We are standing NEAR it, not AT it - a shelf under a
                        # cliff top, tens of yards off in z. Reporting success
                        # from here makes the caller re-issue the same errand
                        # for ever and hides the one case the dead-goal rule
                        # exists for. Fail honestly instead.
                        log(f"  stood {gap:.0f} yd from ({x:.0f}, {y:.0f}) at z{alt[2]:.0f} "
                            f"(goal z{z:.0f}) - near it, not at it")
                        events.emit("no_route", x=round(x, 1), y=round(y, 1),
                                    reason="approach_only", gap=round(gap, 1),
                                    dz=round(alt[2] - z, 1))
                        self._nav_fail = "no_route"
                        return False
                    if not ok:
                        self._nav_fail = getattr(self, "_nav_fail", None) or "no_route"
                    return ok
            if not path and time.time() - getattr(self, "_island_t", 0) > 20:
                # stuck on an unlinked patch (walked straight onto the depot
                # floor to reach Razzle): every search from here fails.
                # Walk straight back to the real mesh and plan from there.
                ex = self.nav.island_exit((me.x, me.y, me.z))
                if ex is not None:
                    self._island_t = time.time()
                    log(f"  on an unlinked patch of mesh - walking {math.dist(ex[:2], (me.x, me.y)):.0f} yd "
                        f"straight back onto the main mesh")
                    self.walk_to(*ex, 1.5, 20)
                    continue
            if not path:
                if no_honest_route:
                    # The route was just declared impassable. Answering that
                    # with a raw click-to-move straight at the goal walks the
                    # same cliff off the mesh entirely - a worse way into it
                    # than the mesh route the old code kept.
                    log("  no honest route and nothing to fall back on - failing this leg")
                    self._nav_fail = "no_route"
                    return False
                if math.dist((me.x, me.y), (x, y)) > 300:
                    # a trainer in Kharanos (3900 yd, no mmaps there) and a
                    # murloc in the Wetlands were "walked straight" toward
                    log(f"  no navmesh path and {math.dist((me.x, me.y), (x, y)):.0f} yd away - not walking that blind")
                    self._nav_fail = "too_far_to_walk_blind"
                    return False
                if careful:
                    # a quest walk with no mesh route went straight into the
                    # Bingles trogg camp (dead in 8 s): let the caller pick
                    # another spawn point instead
                    log("  no navmesh path - not walking blind on a quest walk")
                    self._nav_fail = "no_mesh_path"
                    return False
                log("  no navmesh path - walking straight")
                return self.walk_to(x, y, z, stop_short, timeout)
            path = self.densify_slopes(me, self.densify_indoors(me, path))
            self._nav_path = path
            result = self._follow(path, stop_short, t0 + timeout, combat_ok, travel=careful)
            if result == "arrived":
                return True
            if result == "detour":
                # Re-plan with the mob's circle as a real danger zone (set by
                # _follow above). Re-joining the remembered route at our feet
                # kept the stretch through the mob; splice in past it so the
                # head is planned round it.
                zone = self.__dict__.pop("_detour_zone", None)
                splice_ahead = 35.0
                if zone:
                    zx, zy, zz, zr, far = zone
                    avoid.append((zx, zy, zz, zr))
                    if far:
                        # Splicing back onto a route that runs through a mob
                        # this far above us just re-walks the death. Throw the
                        # remembered route away and plan the whole leg fresh,
                        # now that `avoid` carries the mob.
                        self._path_cache = None
                        splice_ahead = 0.0
                continue
            if result != "stuck":
                return False
            attempts += 1
            me = self.g.player()
            # the obstacle is just ahead of us, toward the waypoint we wanted
            ang = self._heading
            bx, by = me.x + 1.0 * math.cos(ang), me.y + 1.0 * math.sin(ang)
            # stuck again at the same place: the blocked zone grows, forcing
            # the next route to swing wider round the obstacle
            again = sum(1 for a in avoid if math.dist(a[:2], (bx, by)) < 5)
            avoid.append((bx, by, me.z, 4.0 + 3.0 * again))
            if not (abs(me.z - z) > 3 and math.dist((me.x, me.y), (x, y)) < stop_short + 2):
                # stuck under an upstairs target is not a hole in the floor:
                # 13 such "holes" walled off the Lion's Pride taproom
                self.remember_obstacle(bx, by, me.z)
            if self.learn_blocked_climb(me):
                self._path_cache = None      # the remembered route goes up it too
            log(f"  stuck at ({me.x:.1f}, {me.y:.1f}) - unstuck attempt {attempts}")
            events.emit("stuck", x=round(me.x, 1), y=round(me.y, 1), attempt=attempts)
            if attempts == 1:
                stuck_at = [(me.x, me.y)]
            else:
                stuck_at.append((me.x, me.y))
            spread = max(math.dist(p, q) for p in stuck_at for q in stuck_at)
            if attempts >= 5 and spread < 0.6:
                # not one step in any direction for 5 rounds: wedged inside
                # geometry (a shop counter in Stormwind). Only a teleport helps.
                log("  wedged in geometry - using the hearthstone to get out")
                self._wedged_at = time.time()
                self.remember_obstacle(me.x, me.y, me.z)      # never path through here again
                if getattr(self, "use_hearth", None) and self.use_hearth() == "done":
                    return False
            # Five, not nine. Replaying every leg in data/events.jsonl: a leg
            # that has been stuck four times reaches its goal 11% of the
            # time, after five 7%, after six never - and the attempts after
            # the fifth cost ~16 min of pure retrying across the log. The
            # callers pick another spawn point or come back later; that is
            # cheaper than grinding a slope we already failed five times.
            if attempts >= 5:
                log("  giving up on this destination")
                # Twice wedged on the way to the same place is a place we
                # cannot walk to: Tundra MacGrann sits 73 yd up a cliff and
                # the bot re-walked to the same spot for 26 minutes (18:09 to
                # 18:35) for 113 xp. Refuse the destination for a while
                # instead of letting every caller try again.
                bad = self.__dict__.setdefault("_bad_goals", {})
                key = (round(x / 5), round(y / 5))
                nbad, _ = bad.get(key, (0, 0.0))
                bad[key] = (nbad + 1, time.time() + (1200 if nbad else 300))
                self._nav_fail = "wedged"     # nine stucks is about the ground
                return False
            side = 1 if attempts % 2 else -1
            trail = [p for p in getattr(self, "_trail", []) if math.dist(p[:2], (me.x, me.y)) > 2.0]
            if attempts in (1, 3) and trail:
                # back up along the ground we came over (4 yd, then 8 yd):
                # a click-to-move order there is the one move that always
                # works, keys into a scarecrow's collision did nothing
                want = 4.0 if attempts == 1 else 8.0
                p = min(trail, key=lambda p: abs(math.dist(p[:2], (me.x, me.y)) - want))
                self.ctm.p.write_u32(0xC4D888, 12)
                self.walk_to(*p, timeout=4)
            elif attempts == 1:
                back = ang + math.pi + side * math.pi / 4
                self.walk_to(me.x + 2.5 * math.cos(back), me.y + 2.5 * math.sin(back), me.z,
                             timeout=3)
            else:
                # Wedged against geometry the navmesh doesn't know (a mine
                # frame, a fence): click-to-move keeps pushing into it, the
                # keyboard gets out - back up, jump, strafe.
                self.keyboard_escape(side)
        self.stop()
        return False

    OBSTACLE_FILE = os.path.join(HERE, "data", "obstacles.json")

    def load_obstacles(self):
        try:
            self.obstacles = [tuple(o) for o in json.load(open(self.OBSTACLE_FILE))]
        except Exception:
            self.obstacles = []

    NPC_BAN_FILE = os.path.join(HERE, "data", "npc_bans.json")

    def load_npc_bans(self):
        """Unreachable NPCs survive a restart. In memory only, a restart
        walked straight back up to Tundra MacGrann's cliff, the place the
        last run had just given up on. It is a fact about the terrain, so it
        is shared by every character; bans still expire."""
        self.npc_bad, self._npc_fails = {}, {}
        try:
            raw = json.load(open(self.NPC_BAN_FILE, encoding="utf-8"))
        except Exception:
            return
        now = time.time()
        for k, (until, fails) in raw.items():
            key = tuple(int(v) for v in k.split(","))
            self._npc_fails[key] = int(fails)
            if until > now:
                self.npc_bad[key] = float(until)

    def save_npc_bans(self):
        try:
            now = time.time()
            keys = set(self.npc_bad) | set(getattr(self, "_npc_fails", {}))
            out = {",".join(map(str, k)): [min(self.npc_bad.get(k, 0.0), now + 86400),
                                           self._npc_fails.get(k, 0)] for k in keys}
            os.makedirs(os.path.dirname(self.NPC_BAN_FILE), exist_ok=True)
            json.dump(out, open(self.NPC_BAN_FILE, "w", encoding="utf-8"))
        except Exception:
            pass

    def remember_obstacle(self, x, y, z):
        """Furniture the mesh calls walkable: learned once, kept for good.

        Stuck again next to one we already know means the thing is bigger
        than we thought: grow that one (to 3 yd at most, so a doorway is
        never walled off) instead of dropping a new 1.5 yd dot beside it.
        Dots only ever 1.5 yd across let the planner thread a route between
        them - the approach to Tundra MacGrann got stuck at the same spot 13
        times across three trips with seven dots side by side."""
        for i, o in enumerate(self.obstacles):
            d = math.dist(o[:2], (x, y))
            if d < 2.5 and abs(o[2] - z) < 3:
                if d < 1.0 and o[3] >= 3.0:
                    return
                self.obstacles[i] = (o[0], o[1], o[2], min(3.0, max(o[3], d + 1.0) + 0.5))
                break
        else:
            self.obstacles.append((round(x, 1), round(y, 1), round(z, 1), 1.5))
        os.makedirs(os.path.dirname(self.OBSTACLE_FILE), exist_ok=True)
        json.dump(self.obstacles, open(self.OBSTACLE_FILE, "w"))

    def danger_zones(self, me, dest, careful=False):
        """Aggro zones of hostile mobs we can't handle, between here and dest.

        Short hops (fighting, looting) skip this: the stage's own mobs are
        what we came for. Quest walks pass careful=True: three short hops
        between sacks of oats led straight into the harvest watchers.
        """
        if me.ghost or (not careful and math.dist((me.x, me.y), dest) < 80):
            return ()                  # ghosts can't be attacked
        if getattr(self, "map_id", 0) not in (0, 1):
            return ()                  # the tram: nothing lives there
        pad = 300
        bbox = (min(me.x, dest[0]) - pad, min(me.y, dest[1]) - pad,
                max(me.x, dest[0]) + pad, max(me.y, dest[1]) + pad)
        zs = list(dangers.zones(self.continent(), me.level, me.faction, bbox))
        if careful:
            # mobs actually standing about right now: a travel route used to
            # run straight through the golems on the road and then "turned
            # to fight" every one of them
            zs += self.live_zones(me)
        # never treat the destination itself as forbidden
        return [z for z in zs if math.dist(z[:2], dest) > z[3]]

    def aggro_radius(self, me, u) -> float:
        # vanilla: 20 yd at equal level, +/-1 per level, never under 5.
        #
        # The upper clamp used to be min(20.0, ...), which is the equal-level
        # value - so every mob ABOVE our level got the same circle as one of
        # our own level and the whole +1-per-level term only ever shrank it.
        # That is the caller that matters: path_blocker steers round a mob
        # using this radius, and a level-16 Tarantula on a level-11 paladin
        # was steered round on a 20 yd circle instead of its real 25, walked
        # past at 43 yd, and the re-joined route ran straight back into it -
        # three deaths on the same vendor walk (13:03, 13:05, 13:12). The
        # server's own cap is 45.
        return max(5.0, min(45.0, 20.0 + (u.level - me.level)))

    def live_zones(self, me, within=150.0):
        """(x, y, z, r) aggro circles of living hostiles near us."""
        out = []
        for u in self.aggressors():
            # (below our level: walk through, see path_blocker)
            if u.level < me.level or me.dist(u) > within:
                continue
            r = self.aggro_radius(me, u) + 2.0
            if u.level >= me.level + 3:
                # a level 19-21 hunting party in Loch Modan killed a level-15
                # mage three times in 20 min when the quest walk brushed its
                # aggro circle: give such mobs a wide berth
                r += 15.0
            out.append((u.x, u.y, u.z, r))
        return out

    def keyboard_escape(self, side: int = 1):
        self.ctm.p.write_u32(0xC4D888, 12)       # drop the CTM order that pushes us in
        strafe = "q" if side > 0 else "e"
        for keys, hold in ((["s"], 1.0), (["s", "space"], 0.3), ([strafe], 0.8)):
            for k in keys:
                self.kb.down(k)
            time.sleep(hold)
            for k in keys:
                self.kb.up(k)
            time.sleep(0.2)

    def _travel_ok(self, me) -> bool:
        """Travelling mode: a mob that noticed us on a long walk is left to
        chase - it gives up 50 yd from where it aggroed (ThreatRadius) and at
        equal speed never closes a 20 yd gap. Only something already on us
        (dazes from behind let it catch up) or low health ends the run."""
        att = self.attackers(me)
        if not att:
            self._travel_gap = None
            return True
        near = min(me.dist(u) for u in att)
        # a chaser that gains on us (a thrown knife dazes from behind) will
        # catch us anyway: better to turn and fight at 90% than at 40%
        prev = getattr(self, "_travel_gap", None)
        closing = prev is not None and near < prev[0] - 8 and time.time() - prev[1] < 6
        if prev is None or time.time() - prev[1] >= 3:
            self._travel_gap = (near, time.time())
        # only ever wave along a SINGLE chaser: sheeping one of two and
        # running left the other free, which is how 08:47 ended
        # A mob we have never beaten counts as "two levels up" however its
        # level reads: the Dire Condor is only level 18 to our 17, but the
        # record is 2 fights, 2 escapes, 2 deaths and no wins. Sheep it and
        # keep going rather than turning to fight it.
        lone_nemesis = len(att) == 1 and att[0].entry in dangers.NEMESIS
        if (closing or lone_nemesis) and near >= 5 and len(att) == 1:
            # a bear two levels up that catches us is a fight we lose (a
            # level-17 patriarch: sheep, run, stuck, dead): sheep it from
            # range and keep running - 8 s of sheep is 50 yd of road
            u = min(att, key=me.dist)
            cc = getattr(self.profile, "crowd_control", None)
            if ((u.level >= me.level + 2 or u.entry in dangers.NEMESIS)
                    and cc and self.usable(cc) and self.sheepable(u)
                    and near < 28 and time.time() - getattr(self, "_travel_sheep", 0) > 20):
                self._travel_sheep = time.time()
                log(f"  travelling: {worlddb.name(u.entry)} (lvl {u.level}) gaining - sheeping it and running on")
                self.stop()
                self.face(u)
                if self.cast(cc, u):
                    self._cc_sent = (u.guid, time.time())
                    self._travel_gap = None
                    return True
        # Running does not work for this character: equal run speed, dazes
        # from behind, and ranged attackers that do not care about the gap.
        # Across 178 logs, 32% of travel chases ended in a death inside 60 s
        # (20 of 90 deaths) - the bot arrived at the fight already damaged
        # and facing two mobs. Turn while we are still healthy enough to win
        # it, and never let two chasers ride along.
        crowd = len(att) >= 2
        # A warrior has no root, no sheep and no ranged spell: running on only
        # collects a second chaser (a Winter Wolf, then two Snow Tracker
        # Wolves on the walk back to Frostmane Hold: dead at 11:58). Fight a
        # lone chaser of our level the moment it is close, while we are fresh.
        # ... but never on a camp's outrider. At 15:58:06 the engine refused
        # this exact pull on its own - "Stonesplinter Trogg has 3 friend(s)
        # within 16 yd now - leaving it for a while" - and six seconds later
        # melee_turn accepted a Scout 13 yd from the same camp at 100% hp.
        # Two more troggs joined within three seconds, the escape failed with
        # three on us and no root, and that was death #7. The two decisions
        # disagreed about the same camp; this makes the turn ask the question
        # the pull already asks.
        camp = 0
        if att and not self.profile.ranged:
            r = add_radius(me.level, att[0], me, False)
            camp = sum(1 for h in self.threats()
                       if h.guid != att[0].guid and h.dist(att[0]) < r)
        melee_turn = (not self.profile.ranged and len(att) == 1 and near < 20
                      and att[0].level <= me.level + 1 and camp < 2)
        # The one chase we must never turn on: a lone mob far above our
        # level. The combat engine refuses that fight outright the moment it
        # starts it (t.level >= me.level + 3 -> "do not even try - running"),
        # so turning only hands the mob a stationary target while escape()
        # tries to break away from zero metres with nothing bought by the
        # stop. Both paladin deaths at 13:03 and 13:05 were this exact shape:
        # a level-16 Tarantula on a level-11 paladin, turned on at 100% hp
        # because it was "closing", dead in 20 s without landing a swing.
        # Keep running: leaving the mob's territory is what makes it leash,
        # and that is the one thing we cannot do standing still.
        far_above = len(att) == 1 and att[0].level >= me.level + 3
        if not far_above and (near < 7 or me.hp_pct < 85 or closing or crowd or melee_turn):
            why = ("chaser gaining ({:.0f} -> {:.0f} yd)".format(prev[0], near) if closing
                   else f"{len(att)} chasers" if crowd
                   else f"{near:.0f} yd away" if near < 7 or melee_turn else f"{me.hp_pct:.0f}% hp")
            log(f"  travelling: turning to fight at {me.hp_pct:.0f}% hp - {why}")
            self._travel_gap = None
            return False
        if time.time() - getattr(self, "_travel_note", 0) > 5:
            self._travel_note = time.time()
            u = min(att, key=me.dist)
            rel = (math.atan2(u.y - me.y, u.x - me.x) - me.facing + math.pi) % (2 * math.pi) - math.pi
            side = "ahead" if abs(rel) < 1.0 else ("behind" if abs(rel) > 2.1 else "beside")
            log(f"  travelling: {len(att)} mob(s) chasing, nearest {near:.0f} yd {side} - running on")
        return True

    def path_blocker(self, me, path, i):
        """A living hostile whose aggro circle covers the next ~30 yd of the
        path, not yet fighting us, and not so close that it has us anyway."""
        pts = [(me.x, me.y)] + [p[:2] for p in path[i:i + 12]]
        segs = []
        run = 0.0
        for a, c in zip(pts, pts[1:]):
            segs.append((a, c))
            run += math.dist(a, c)
            if run > 30:
                break
        best = None
        done = getattr(self, "_detoured", {})
        # the quest's own target is where we are going, not something in the
        # way: the walk to Grik'nir the Cold "steered round" Grik'nir twice
        hunt, hunt_t = getattr(self, "_hunt_mobs", (set(), 0.0))
        if time.time() - hunt_t > 60:
            hunt = set()
        for u in self.aggressors():
            if u.entry in hunt:
                continue
            # below our level it is a kill, not a danger: steering round every
            # level-2 trogg took a level-3 warrior 49 s over 190 yd at 16:21
            # (four re-plans in 16 s)
            if u.level < me.level or u.target == me.guid or done.get(u.guid, 0) > time.time():
                continue
            d = me.dist(u)
            if d < 8 or d > 45:
                continue
            r = self.aggro_radius(me, u)
            if any(seg_dist((u.x, u.y), a, c) < r for a, c in segs):
                if best is None or d < me.dist(best):
                    best = u
        return best

    def maybe_jump(self, closing: bool = False) -> None:
        """Hop now and then while running.

        Vanilla has no bunny-hop speed bonus, so this buys nothing
        mechanically - it is simply how a person moves, and a character
        that never once leaves the ground reads as scripted. Only while
        actually moving, and never while seated, dead or mid-cast: a jump
        does not cancel a 1.12 cast, but a caster standing still to cast
        is not running anyway.
        """
        now = time.time()
        if now < getattr(self, "_next_jump", 0.0):
            return
        self._next_jump = now + random.uniform(2.5, 7.0)
        me = self.g.player()
        if me.dead or me.ghost or me.sitting:
            return
        # "closing" is the run at a target we have already engaged: keep
        # hopping while there is ground to cover, but not once we are in
        # melee, where it just looks twitchy.
        if me.in_combat and not closing:
            return
        if not (me.moving or self.ctm.moving):
            return
        try:
            self.kb.press("space", hold=0.06)
            # A jump cancels the client's click-to-move, but the CTM action
            # field still reads "moving" - so the follow loop saw no reason
            # to re-issue the order, the character stood still, and 2.5 s
            # later the stuck detector fired on a character that was fine.
            # Force one re-issue on the next tick.
            self._just_jumped = True
        except NotInWorld:
            pass

    def _follow(self, path, stop_short, deadline, combat_ok=False, travel=False) -> str:
        i = 0
        me = self.g.player()
        last_pos, last_move = (me.x, me.y), time.time()
        last_scan = 0.0
        self._heading = me.facing
        while i < len(path) and time.time() < deadline and not self.stopped():
            wx, wy, wz = path[i]
            final = i == len(path) - 1
            self.maybe_jump()
            me = self.g.player()
            if me.dead or (me.in_combat and not me.ghost and not combat_ok
                           and not (travel and self._travel_ok(me))):
                self.stop()
                return "interrupted"
            d = math.dist((me.x, me.y), (wx, wy))
            if d <= (max(stop_short, 1.0) if final else 2.0) and (not final or self._same_floor(me, wz)):
                i += 1
                continue
            # the client stops on its own ~2 yd short on slopes and ledges;
            # re-issuing the same order then looks like being stuck forever
            if final and d <= 3.0 and not self.ctm.moving and not me.moving                     and time.time() - last_move > 0.8:
                if self._same_floor(me, wz):
                    i += 1
                    continue
                return "stuck"          # under (or above) the target, not at it
            # Steep slopes and stairs: waypoints come thick and the client
            # does not hit each one exactly. Once we are already closer to the
            # next waypoint than this one is, this one is behind us - skip it.
            if not final:
                nx, ny, _ = path[i + 1]
                if math.dist((me.x, me.y), (nx, ny)) < math.dist((wx, wy), (nx, ny)) - 0.5:
                    i += 1
                    continue
            # travelling: a mob standing on the path ahead would notice us
            # and be "on us" before we passed it - re-plan round it now
            if travel and time.time() - last_scan > 1.0 and getattr(self, "_detours", 0) < 8:
                last_scan = time.time()
                blocker = self.path_blocker(me, path, i)
                if blocker is not None:
                    # no stop here: the re-plan takes ~0.1 s and the character
                    # keeps running to the current waypoint meanwhile. Three
                    # stop-replan-restart cycles in three seconds on the road
                    # to Kharanos looked like a stutter, and the re-plan
                    # then re-joined the very same route ("back on the
                    # remembered route") - see splice_ahead in _navigate
                    self._detours = getattr(self, "_detours", 0) + 1
                    # A mob far above us is the one we must not go blind to.
                    # The 20 s suppression below is anti-thrash for ordinary
                    # mobs, but it also meant that after ONE steer we stopped
                    # seeing the mob entirely, re-joined the remembered route
                    # straight back through it, and had no eyes left for the
                    # 20 s it took to die. Four deaths on one level-16
                    # Tarantula (13:03-13:19) all read: "steering round it",
                    # then 3 s later "1 mob(s) chasing".
                    far = blocker.level >= me.level + 3
                    self.__dict__.setdefault("_detoured", {})[blocker.guid] = (
                        time.time() + (4 if far else 20))
                    # Hand the re-plan the circle to route around. The branch
                    # in _navigate claimed to "re-plan with the mob's circle
                    # as a danger zone" but never actually passed one: it only
                    # moved the splice point 35 yd along the SAME remembered
                    # route, which on this road put it at the spider's feet.
                    self._detour_zone = (blocker.x, blocker.y, blocker.z,
                                         self.aggro_radius(me, blocker) + (20.0 if far else 8.0),
                                         far)
                    log(f"  travelling: {worlddb.name(blocker.entry)} (lvl {blocker.level}) "
                        f"{me.dist(blocker):.0f} yd ahead on the path - steering round it")
                    return "detour"
            # Something hostile is about to notice us: open on it from range
            # instead of letting it run up and start the fight in melee.
            if not combat_ok and not travel and time.time() - last_scan > 0.5:
                last_scan = time.time()
                mob = self.pull_candidate(me)
                if mob is not None:
                    self.stop()
                    log(f"  pulling {worlddb.name(mob.entry)} at {me.dist(mob):.0f} yd "
                        "before it reaches us")
                    self.fight(mob)
                    me = self.g.player()
                    if me.dead or me.ghost:
                        return "interrupted"
                    # the combat flag lingers a few seconds after the kill;
                    # let it clear or the walk is "interrupted" at once
                    t_cc = time.time()
                    while self.g.player().in_combat and time.time() - t_cc < 6:
                        time.sleep(0.3)
                    if not self.args.no_loot and self.g.player().hp_pct >= 35:
                        self.loot_nearby(self.g.player())
                    self.buff_if_needed(self.g.player())
                    self.rest_if_needed(self.g.player())
                    if not self.args.no_loot:
                        self.loot_nearby(self.g.player())
                    me = self.g.player()
                    last_pos, last_move = (me.x, me.y), time.time()
                    continue
            self._heading = math.atan2(wy - me.y, wx - me.x)
            if (self.__dict__.pop("_just_jumped", False) or not self.ctm.moving
                    or math.dist(self.ctm_dest(), (wx, wy)) > 0.1):
                self.ctm.move_to(wx, wy, wz)
            if math.dist(last_pos, (me.x, me.y)) > 0.3:
                last_pos, last_move = (me.x, me.y), time.time()
                # ground we actually walked: the safest place to back up to
                trail = self.__dict__.setdefault("_trail", [])
                if not trail or math.dist(trail[-1][:2], (me.x, me.y)) > 1.5:
                    trail.append((me.x, me.y, me.z))
                    del trail[:-40]
            elif time.time() - last_move > 2.5:      # climbing is slow, not stuck
                return "stuck"
            time.sleep(0.1)
        if i >= len(path):
            self.stop()
            return "arrived"
        return "timeout"

    def interact(self, unit: Unit) -> bool:
        """Talk to an NPC (gossip / trainer / vendor window) via CTM action 5.

        Like CTM-loot, the interaction fires while *approaching*, so start
        from ~6 yd away rather than from on top of the NPC.
        """
        # Must start OUTSIDE interaction range: issued from within ~5 yd the
        # client never talks, and instead keeps the order active and walks
        # straight ahead along its facing until something blocks it (this
        # ran a fresh character from Northshire into Echo Ridge Mine).
        # A failed try is retried from another side (a wall, a counter or the
        # NPC's own model can be in the way of the first approach).
        for attempt, turn in enumerate((0.0, 1.2, -1.2)):
            me = self.g.player()
            unit = self.g.unit(unit.guid) or unit          # NPCs wander a little
            d = me.dist(unit)
            if attempt == 0 and d <= 5.0:
                # already next to them (the previous talk, a hand-in that
                # continues with an accept): a click from here beats walking
                # out to an approach spot through the furniture
                if self.click_npc(unit):
                    return True
                me = self.g.player()
                unit = self.g.unit(unit.guid) or unit
                d = me.dist(unit)
            if attempt >= 1:
                # The order fails for NPCs behind counters or in nooks (the
                # client walks into the furniture and never "arrives"). A real
                # right-click on the NPC talks from anywhere within ~5 yd.
                if self.click_npc(unit):
                    return True
                # no click possible (camera pushed into a roof, NPC off
                # screen): fall through to the order from another side
                me = self.g.player()
                unit = self.g.unit(unit.guid) or unit
                d = me.dist(unit)
            # Standing right next to the NPC (after a previous talk) the order
            # rarely triggers: back off to a fresh approach point every time.
            if abs(me.z - unit.z) > 3.0:
                # wrong floor (the taproom under an upstairs trainer): an
                # order from here aims at the ceiling - get onto their floor
                pt = self.approach_point(me, unit, turn) or (unit.x, unit.y, unit.z)
                self.navigate(*pt, stop_short=0.5 if pt[0] != unit.x else 6.0, timeout=90)
                self.wait_until_still(1.5)
                me = self.g.player()
                if abs(me.z - unit.z) > 3.0 and math.dist((me.x, me.y), (unit.x, unit.y)) < 15:
                    # the mesh stops at the doorway: Jern Hornhelm's house in
                    # Thelsamar is a 37-polygon island whose door has no
                    # link, so the path ends on the threshold 7 yd above him.
                    # From that close a straight walk gets through the door
                    # and down the ramp inside.
                    log(f"  {abs(me.z - unit.z):.0f} yd above/below {worlddb.name(unit.entry)} at the doorway - walking straight in")
                    self.walk_to(unit.x, unit.y, unit.z, 2.5, 25)
                    self.wait_until_still(1.0)
                    me = self.g.player()
                if abs(me.z - unit.z) > 3.0:
                    log(f"  still {abs(me.z - unit.z):.0f} yd below/above {worlddb.name(unit.entry)} - no order from here")
                    if abs(me.z - unit.z) > 20.0 or attempt >= 1:
                        # 73 yd under Tundra MacGrann on his cliff: no walk
                        # from here gets up there, and trying twice more is
                        # another 8 minutes of the same stuck spot.
                        #
                        # The attempt>=1 half is the same lesson at 5 yd: the
                        # other two approach angles turn the character round
                        # the OUTSIDE of the building, and a z gap is never an
                        # angle problem - the mesh simply stops at the door.
                        # Honni Goldenoat in Thelsamar span this loop for
                        # minutes at 16:19 ("walking the last 8 yd straight
                        # in" over and over), which is what the user saw.
                        return False
                    continue
                d = me.dist(unit)
            if d > 8.5 or d < 6.5 or turn:
                # First choice: a spot we actually walked through on the way
                # in, 6.5-9 yd out. It is reachable by definition and usually
                # in the open; a computed spot indoors can sit behind a pillar
                # (Marshal McBride in Northshire Abbey: two "stuck" backing
                # off to it, the click fallback could not see him, the quest
                # and its follow-up were skipped).
                trail = [t for t in getattr(self, "_trail", [])
                         if 6.5 <= math.dist(t[:2], (unit.x, unit.y)) <= 9.0
                         and abs(t[2] - unit.z) < 3.0]
                pt = (max(trail, key=lambda t: -abs(math.dist(t[:2], (unit.x, unit.y)) - 7.5))
                      if trail and attempt < 2 else None)
                if pt is None:
                    pt = self.approach_point(me, unit, turn)
                if pt is None and d > 8.5:
                    pt = (unit.x, unit.y, unit.z)      # no better spot: walk up to them
                if pt is not None:
                    self.navigate(*pt, stop_short=0.5 if pt[0] != unit.x else 6.0, timeout=90)
                    self.wait_until_still(1.5)
            time.sleep(0.4)
            me = self.g.player()
            d = me.dist(unit)
            if d > 10 or self.stopped():
                continue                       # navigation cut short: no order from afar
            if d < 6.0:
                # Could not get out to a proper approach spot (furniture,
                # porch, counter): from inside ~5.5 yd the order never fires
                # and the client just walks into the scenery - click instead.
                if self.click_npc(unit):
                    return True
                continue
            # NPC talk works with the client's idle defaults in the fields an
            # object click fills in (distance 0, 0xC4D910 = 0.5) - restore them
            self.ctm.reset_fields()
            self.p.write(0xC4D8C4, struct.pack("<f", me.angle_to(unit) % (2 * math.pi)))
            self.p.write(CTM_GUID, struct.pack("<Q", unit.guid))
            self.p.write(CTM_DEST, struct.pack("<3f", unit.x, unit.y, unit.z))
            self.p.write_u32(CTM_ACTION, CTM_INTERACT_NPC)
            t0 = time.time()
            while time.time() - t0 < 2.5 and self.p.u32(CTM_ACTION) == CTM_INTERACT_NPC:
                time.sleep(0.05)
            if self.p.u32(CTM_ACTION) != CTM_INTERACT_NPC:
                time.sleep(0.3)
                return self.g.player().dist(unit) < 5.5
            now = self.g.player()
            self.halt()                            # never let it wander off
            log(f"  interaction did not trigger from {d:.1f} yd (try {attempt + 1}) - cancelled "
                f"(moved {me.dist(now):.1f} yd, now {now.dist(unit):.1f} yd, "
                f"facing off by {abs(now.facing_error(unit)):.2f})")
        return False

    def _timed(self, name, fn, *args):
        """Run a between-fights check; log it when it did nothing but took
        over a second (a check that acts - rests, loots - is allowed time)."""
        t0 = time.time()
        r = fn(*args)
        dt = time.time() - t0
        if dt > 1.0 and not r:
            log(f"  SLOW CHECK: {name} took {dt:.1f} s and did nothing")
        return r

    def fix_camera(self) -> bool:
        """Reset the view when the camera is inside the character or looking
        up/down so steeply that nothing around us projects on screen. True if
        it was reset."""
        import screenpos
        try:
            pos, (fwd, _l, _u), _f, _a = screenpos.camera(self.p)
            me = self.g.player()
            close = math.dist(pos, (me.x, me.y, me.z + 1.0)) < 2.0
            steep = abs(fwd[2]) > 0.8
            if not (close or steep):
                return False
            if time.time() - getattr(self, "_cam_reset_t", 0) < 5:
                return False
            self._cam_reset_t = time.time()
            log(f"  camera {'inside the character' if close else 'pitched off'} "
                f"(fwd z {fwd[2]:+.2f}) - resetting the view")
            self.bridge.send(br.CAMERA_RESET, wait=0.5)
            return True
        except Exception:
            return False

    def aim_camera(self, x: float, y: float):
        """Swing the camera toward a point before a mouse click there.

        The character's facing is written to memory, and standing still that
        does not drag the camera round: at 05:01 the camera looked 0.84 rad
        away from where we faced, the view is only +-0.6 rad wide, and Corina
        Steele (William Pestle, Marshal McBride before her) "projected None"
        at 3 yd. FlipCameraYaw(deg) turns the camera alone, positive = toward
        a positive (counter-clockwise) yaw error: 2.71 rad -> 155 deg -> 0.01.
        Right after we stop, the camera is often still swinging after us on
        its own; measure only once it holds still, or the correction is
        aimed at a moving camera (05:28: three swings, ended 3.08 rad off)."""
        import screenpos
        self.fix_camera()
        try:
            def err():
                # the camera orbits us: aim it along our bearing to the point
                # (then it sits behind us and looks through us at the point);
                # the bearing from the camera itself moves as it swings
                me = self.g.player()
                _pos, (fwd, _l, _u), _f, _a = screenpos.camera(self.p)
                want = math.atan2(y - me.y, x - me.x)
                return (want - math.atan2(fwd[1], fwd[0]) + math.pi) % (2 * math.pi) - math.pi

            def settled(limit=2.0):
                # held still over two consecutive reads, 0.15 s apart: a
                # single quiet read mid-swing judged 86 deg as 42 (07:38)
                e, t_s, calm = err(), time.time(), 0
                while time.time() - t_s < limit:
                    time.sleep(0.15)
                    e2 = err()
                    calm = calm + 1 if abs(e2 - e) < 0.01 else 0
                    e = e2
                    if calm >= 2:
                        break
                return e
            e = settled()
            trace = []
            for _ in range(3):
                if abs(e) < 0.25:
                    if trace:
                        log(f"  camera aimed: {', '.join(trace)}")
                    return
                deg = int(round(math.degrees(e)))
                self.bridge.send(br.CAMERA_YAW, max(1, min(360, deg + 180)), wait=0.15)
                e2 = settled()
                trace.append(f"{e:.2f}->{deg}deg->{e2:.2f}")
                e = e2
            if abs(e) >= 0.25:
                log(f"  camera still {e:.2f} rad off the click target ({', '.join(trace)})")
        except Exception as ex:
            log(f"  camera aim failed: {ex}")

    def click_npc(self, unit: Unit) -> bool:
        """Talk to an NPC with a genuine right-click (see mouse.py)."""
        import mouse
        import screenpos
        me = self.g.player()
        if me.dist(unit) > 4.5:
            self.navigate(unit.x, unit.y, unit.z, stop_short=3.5, timeout=60)
            self.wait_until_still(1.0)
        elif me.dist(unit) < 2.0:
            # on top of them the camera sits inside the model and the
            # projection fails: a step back first
            a = math.atan2(me.y - unit.y, me.x - unit.x)
            self.walk_to(unit.x + 3.2 * math.cos(a), unit.y + 3.2 * math.sin(a), me.z, timeout=3)
            self.wait_until_still(1.0)
        me = self.g.player()
        # NPCs walk their rounds (Rybrad Coldbank): aim at where they are
        # now, not where they stood when we set off - a stale position
        # projected off screen and the NPC was written off for 30 minutes
        unit = self.g.unit(unit.guid) or unit
        if me.dist(unit) > 6:
            return False
        # Turn to them like a player would, but do not wait out the whole
        # turn: aim_camera puts the NPC on screen, which is what the click
        # needs, and the old 0.15 rad wait was up to 2.5 s of every talk
        # (Senir Whitebeard: 5 s from arriving at 2 yd to the click).
        self.ctm.face(me, unit.x, unit.y)
        t0 = time.time()
        while time.time() - t0 < 0.8:
            unit = self.g.unit(unit.guid) or unit
            if abs(self.g.player().facing_error(unit)) <= 0.5:
                break
            time.sleep(0.1)
        unit = self.g.unit(unit.guid) or unit
        self.aim_camera(unit.x, unit.y)
        pos = screenpos.find_on_screen(self.p, unit.guid, unit.x, unit.y, unit.z,
                                       heights=(1.0, 0.5, 1.5, 0.2))
        if pos is None:
            # diagnostics: is the projection off screen (camera pushed in by a
            # roof / wall) or does the mouse-over just not register?
            try:
                cam = screenpos.camera(self.p)[0]
                proj = screenpos.world_to_screen(self.p, unit.x, unit.y, unit.z + 1.0)
                log(f"  {worlddb.name(unit.entry)} not found on screen for a click "
                    f"(projected {proj}, camera {math.dist(cam, (me.x, me.y, me.z)):.1f} yd "
                    f"from us, npc {me.dist(unit):.1f} yd)")
            except Exception:
                log(f"  {worlddb.name(unit.entry)} not found on screen for a click")
            return False
        if not mouse.real_right_click(self.p.hwnd, *pos):
            log("  could not focus the game window for the click")
            return False
        t_c = time.time()
        while time.time() - t_c < 1.0 and self.g.target_guid() != unit.guid:
            time.sleep(0.05)
        time.sleep(0.25)                   # the gossip frame opens a frame or two later
        ok = self.g.target_guid() == unit.guid and self.g.player().dist(unit) < 6
        log(f"  talked to {worlddb.name(unit.entry)} with a click" if ok else "  click on the NPC did nothing")
        return ok

    def approach_point(self, me, unit, turn=0.0):
        """A spot 6-7.5 yd from the NPC to start the talk order from, on the
        navmesh, on the NPC's floor, and with the SHORTEST walk from where we
        stand. Indoors (an upstairs room, a bar counter) most directions end
        in a wall, and the nearest-looking spot can lie behind the counter
        through the kitchen: candidates are ranked by real path length instead
        of walking into furniture and getting "stuck"."""
        base = math.atan2(me.y - unit.y, me.x - unit.x) + turn
        best, best_len = None, 1e9
        for r in (7.0, 6.2):
            for da in (0.0, 0.6, -0.6, 1.2, -1.2, 2.0, -2.0, 3.1):
                a = base + da
                x, y, z = unit.x + r * math.cos(a), unit.y + r * math.sin(a), unit.z
                if not self.nav:
                    return (x, y, z)
                path = self.nav.find_path((me.x, me.y, me.z), (x, y, z))
                if not path:
                    continue
                end = path[-1]
                # the funnel always ends at the requested point, so test the
                # candidate itself: it must lie on a poly of the NPC's floor
                n, _ = self.nav.nearest_poly(x, y, z, max_dist=0.5)
                if n is None:
                    continue
                tt = self.nav.tiles[n[0]]
                if not (tt.pmin[n[1], 1] - 1.5 <= z <= tt.pmax[n[1], 1] + 1.5):
                    continue
                if abs(end[2] - unit.z) >= 2.5 or math.dist(end[:2], (unit.x, unit.y)) < 5.5:
                    continue
                pts = [(me.x, me.y)] + [p[:2] for p in path]
                length = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
                if length < best_len:
                    best, best_len = (float(end[0]), float(end[1]), float(end[2])), length
        return best

    def _same_floor(self, me, z) -> bool:
        """Target height reached - or the mesh has no floor at the target's
        height (a bogus z on a slope), in which case height is ignored. An
        upstairs trainer is 7 yd above the taproom: 2D arrival said "there"
        while we stood under him."""
        if abs(me.z - z) < 3.0 or not self.nav:
            return True
        # the poly under us vs the poly at the target height under us: on a
        # hillside both are the same big sloped poly (its height range spans
        # 10 yd - a range test called Sentinel Hill "another floor" and
        # stuck-cycled); under an upstairs room they differ
        n_me, _ = self.nav.nearest_poly(me.x, me.y, me.z, max_dist=2.0)
        n_t, _ = self.nav.nearest_poly(me.x, me.y, z, max_dist=2.0)
        if n_me is None or n_t is None:
            return True
        return n_me == n_t

    def ctm_dest(self):
        return struct.unpack("<2f", self.p.read(0xC4D890, 8))

    def looting_flag(self) -> bool:
        me = self.g.player()
        d = self.p.u32(me.base + O.OBJ_DESCRIPTORS)
        return bool(self.p.u32(d + O.UNIT_FIELD_FLAGS) & 0x400)   # UNIT_FLAG_LOOTING

    def face(self, target: Unit, tolerance=0.35):
        me = self.g.player()
        err = me.facing_error(target)
        if abs(err) <= tolerance:
            return
        self.ctm.face(me, target.x, target.y)
        self.wait_until_still(1.0)
        err = self.g.player().facing_error(target)
        if abs(err) <= tolerance:
            return
        # the 0.3 yd click-to-move nudge does not always turn us: after the
        # Frost Nova step-back the first bolt went out with our back to the
        # mob (facing off 1.8 rad) and half the root was lost. The keyboard
        # turns at pi rad/s.
        for _ in range(2):
            self.kb.press("left" if err > 0 else "right", hold=min(abs(err) / math.pi, 1.2))
            time.sleep(0.15)
            err = self.g.player().facing_error(target)
            if abs(err) <= tolerance:
                return

    def attackers(self, me) -> list[Unit]:
        # a mob we just ran from (blacklisted by escape) only counts again
        # once it has actually caught up: walking back to a sheep 80 yd
        # away undid the escape
        # ... but a blacklisted smuggler throwing at us from 20 yd while we
        # sat at 31% hp is an attacker: the combat flag on us settles it
        return [u for u in self.g.units()
                if not u.dead and u.target == me.guid and u.in_combat
                and (self.blacklist.get(u.guid, 0) < time.time() or me.dist(u) < 8
                     or (me.in_combat and me.dist(u) < 30))]     # a thrower's reach, not a sheep 88 yd off

    PULL_RADIUS = 24.0            # a same-level mob aggroes at ~20 yd

    def reachable(self, me, u: Unit) -> bool:
        """Is the mob where it looks? In a mine a mob on the level above is a
        few yards away in a straight line but a long tunnel walk with no line
        of sight: casting fails and walking to it pulls the whole level."""
        w = self.walk_dist(me, u)
        return w is not None and w < 1.6 * me.dist(u) + 8

    def walk_dist(self, me, u: Unit) -> float | None:
        """Yards on foot to the mob along the navmesh, None if no way there.
        Target choice ranks by this, not the straight line: a mob 30 yd away
        across a gully is a 90 yd walk while one 40 yd off on our side is 40."""
        d = me.dist(u)
        if not self.nav or d < 4:
            return d
        cache = self.__dict__.setdefault("_walk_cache", {})
        hit = cache.get(u.guid)
        if (hit and time.time() - hit[0] < 2.0 and math.dist(hit[1], (u.x, u.y)) < 3.0
                and math.dist(hit[2], (me.x, me.y)) < 3.0):
            return hit[3]
        path = self.nav.find_path((me.x, me.y, me.z), (u.x, u.y, u.z), max_nodes=4000)
        if path:
            pts = [(me.x, me.y, me.z)] + list(path)
            walk = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        else:
            walk = None
        if len(cache) > 300:
            cache.clear()
        cache[u.guid] = (time.time(), (u.x, u.y), (me.x, me.y), walk)
        return walk

    def wait_out_sickness(self, me) -> bool:
        """Sit still until Resurrection Sickness goes. True while waiting.

        Refusing to *pull* while sick was not enough: at 10:54, still sick
        from a rez six minutes earlier, the bot was chased on the way to the
        hunting ground, turned to fight at 78% and lost to a level-15
        Tarantula. At -75% stats every fight is a coin flip, and losing it
        means another spirit healer and another ten minutes of sickness.
        Waiting costs minutes; the spiral costs the morning.
        """
        if not self.rez_sick(me) or me.in_combat:
            return False
        if not getattr(self, "_sick_since", 0.0):
            self._sick_since = time.time()
            log("  resurrection sickness (-75% stats) - sitting it out rather than "
                "feeding the death spiral")
        set_state("waiting", detail="resurrection sickness")
        time.sleep(5.0)
        me = self.g.player()
        if not self.rez_sick(me):
            log(f"  sickness gone after {(time.time() - self._sick_since) / 60:.1f} min - back to work")
            self._sick_since = 0.0
        return True

    def rez_sick(self, me) -> bool:
        """Resurrection Sickness: -75% to every stat, up to 10 minutes.

        The spirit healer costs this and the bot never knew. At 10:47 it
        rezzed sick with 229 max hp instead of 342 and 323 mana instead of
        790, walked straight back out and lost to a level-15 Redridge
        Mongrel - its second death in three minutes, in a spot where it had
        just ground for 30 minutes without dying once. Dying while sick
        sends us back to the spirit healer, which renews the sickness, so
        this compounds. Do not start fights until it wears off.
        """
        return REZ_SICKNESS in me.auras

    def pull_candidate(self, me) -> Unit | None:
        """A lone hostile close enough to aggro on us soon - worth pulling
        from range now, while we still have the distance."""
        if me.hp_pct < 60 or (self.profile.power == "mana" and me.mana_pct < 40):
            return None
        if self.rez_sick(me):
            if time.time() - getattr(self, "_sick_note", 0) > 60:
                self._sick_note = time.time()
                log("  resurrection sickness up (-75% stats) - not pulling until it goes")
            return None
        units = [u for u in self.g.units() if not u.dead and not u.npc_flags
                 and me.dist(u) < self.PULL_RADIUS + add_radius(me.level)]
        if not units:
            return None
        info = worlddb.creatures({u.entry for u in units})
        hostile = [u for u in units if factions.is_hostile(me.faction, u.faction)
                   and not (info.get(u.entry) and info[u.entry].critter)]
        # a mob aggroes at 20 yd +/- 1 per level of difference (5..25):
        # pull only what is about to notice us, not every grey wolf we pass
        def aggro(u):
            return max(5.0, min(25.0, 20.0 + (u.level - me.level)))
        cands = [u for u in hostile if me.dist(u) < min(self.PULL_RADIUS, aggro(u) + 3)
                 and info.get(u.entry) and not info[u.entry].elite
                 and not u.tapped_by_other
                 and not (u.flags & 0x2 or u.flags & 0x02000000)
                 and u.level <= me.level + self.args.max_level_diff
                 and self.blacklist.get(u.guid, 0) < time.time()
                 and self.pack_mobs.get(u.entry, 0) < time.time()
                 and not any(h.guid != u.guid and h.dist(u) < add_radius(me.level)
                             for h in hostile)]
        for u in sorted(cands, key=me.dist):
            if self.reachable(me, u):
                return u
        return None

    @staticmethod
    def grey_level(level: int) -> int:
        """Highest mob level that awards no xp (vanilla 1.12 formula)."""
        if level <= 5:
            return 0
        if level <= 39:
            return level - 5 - level // 5
        if level <= 59:
            return level - 1 - level // 5
        return level - 9

    def valid_prey(self, me, u: Unit, info, reach: float = PULL_RANGE) -> bool:
        ci = info.get(u.entry)
        if u.dead or u.tapped_by_other or u.npc_flags or ci is None:
            return False
        if ci.critter or ci.elite:
            return False
        if u.flags & 0x2 or u.flags & 0x02000000:          # not attackable / not selectable
            return False
        if factions.is_friendly(me.faction, u.faction):
            return False
        if u.level > me.level + self.args.max_level_diff:
            return False
        if me.dist(u) > reach:
            return False
        if u.level < getattr(self, "prey_min_level", 0):
            return False          # worth too little for the grind that asked (grind_to)
        if u.level <= self.grey_level(me.level):
            return False          # grey: 0 xp. Four Rockjaw Skullthumpers (lvl 8)
                                  # at level 16 cost 2 minutes and 600 mana for nothing
        if self.blacklist.get(u.guid, 0) > time.time():
            return False
        if self.pack_mobs.get(u.entry, 0) > time.time():
            return False
        if math.dist(self.home[:2], (u.x, u.y)) > self.radius:
            return False
        if self.stage_mobs and u.entry not in self.stage_mobs:
            return False
        return True

    # ------------------------------------------------------------ behaviours
    SWIM_FLAG = 0x00200000

    def swim_out(self, target, timeout: float = 25.0) -> bool:
        """Swim to a point on the keyboard. True once we are out of the water.

        Click-to-move does not get the character out of Lake Everstill: the
        bot logged "heading 37 yd to dry land" three times, moved nowhere and
        eventually drowned. keyboard_escape() already proves the keys work
        when CTM is dropped, so swim on the keys instead.
        """
        from types import SimpleNamespace
        self.ctm.p.write_u32(0xC4D888, 12)        # drop the CTM order
        mark = SimpleNamespace(x=target[0], y=target[1], z=target[2])
        t0 = time.time()
        while time.time() - t0 < timeout and not self.stopped():
            me = self.g.player()
            if not self.swimming():
                log(f"  out of the water after {time.time() - t0:.0f}s of swimming")
                return True
            if me.dead:
                return False
            self.face(mark)
            # forward ALONE moves 0.4 yd: the character catches on the lake
            # bed and only a jump breaks it loose. Measured in Lake Everstill
            # with the bot stopped: "w" 0.4 yd, "w+space" 12.6 yd and the
            # swim flag cleared.
            for k in ("w", "space"):
                self.kb.down(k)
            time.sleep(1.2)
            for k in ("space", "w"):
                self.kb.up(k)
            time.sleep(0.4)
        return not self.swimming()

    def swimming(self) -> bool:
        """The swim flag, sampled twice: a single read flickers as we bob."""
        a = bool(self.g.player().move_flags & self.SWIM_FLAG)
        time.sleep(0.25)
        return a and bool(self.g.player().move_flags & self.SWIM_FLAG)

    def dry_land_near(self, here, min_yards: float = 10.0):
        """A spot on dry land far enough away to actually be out of the water.

        reachable_near() kept handing back a ground poly 2 yd away: the bot
        logged "heading 2 yd to dry land" three times running, moved 2 yd,
        was still swimming, and gave up - which is how it spent 30 minutes in
        Lake Everstill earning 0 xp. A point we can stand in has to be a
        point we are not already floating over, so probe outward and ignore
        anything nearer than min_yards.
        """
        import navmesh
        best = None
        for reach in (18.0, 35.0, 60.0, 100.0):
            for k in range(8):
                a = k * math.pi / 4
                probe = (here[0] + reach * math.cos(a),
                         here[1] + reach * math.sin(a), here[2])
                p = self.nav.reachable_near(here, probe, radius=12.0, tries=0,
                                            require=navmesh.NAV_GROUND,
                                            exclude=navmesh.NAV_WATER)
                if p is None:
                    continue
                d = math.dist(p[:2], here[:2])
                if d < min_yards:
                    continue
                if best is None or d < best[1]:
                    best = (p, d)
            if best is not None:
                return best[0]
        return None

    def leave_water(self, me) -> bool:
        """Swimming out of combat: get to dry land. True if we moved.

        Drowning killed the mage twice in ten minutes tonight with no
        attacker and no combat line in the log - once while stuck on the
        steep Loch Modan bank at 06:00, once at 03:00. Breath is not
        exposed anywhere we can read, so treat "swimming and nobody
        fighting us" as a reason to get out on its own, not only when the
        rest logic happens to run."""
        import navmesh
        if not (me.move_flags & self.SWIM_FLAG) or me.in_combat or not self.nav:
            self._swim_since = 0.0
            self._swim_tries = 0
            return False
        now = time.time()
        # Swimming is not by itself an emergency. Acting on it every tick
        # turned this into a livelock at 06:21: wedged on the bank, the walk
        # moved 0 yd, the guard fired again a second later and the bot did
        # nothing else at all. Only step in once the water has actually been
        # a problem, and stop trying if we cannot get out.
        if not getattr(self, "_swim_since", 0.0):
            self._swim_since = now
            self._swim_tries = 0
        drowning = me.hp_pct < 90 or now - self._swim_since > 15.0
        if not drowning:
            return False
        if getattr(self, "_swim_tries", 0) >= 3:
            if now - self._swim_since < 120.0:
                return False        # let the rest of the loop have a turn
            self._swim_since, self._swim_tries = now, 0
        self._swim_tries = getattr(self, "_swim_tries", 0) + 1
        here = (me.x, me.y, me.z)
        back = self.dry_land_near(here)
        if back is None:
            back = self.nav.reachable_near(here, here, radius=200.0, tries=0,
                                           require=navmesh.NAV_GROUND,
                                           exclude=navmesh.NAV_WATER)
        if back is None or math.dist(back[:2], here[:2]) > 60.0:
            shore = self.nav.reachable_near(here, here, radius=200.0, tries=0,
                                            require=navmesh.NAV_GROUND)
            if shore is not None and (back is None
                                      or math.dist(shore[:2], here[:2]) + 25.0
                                      < math.dist(back[:2], here[:2])):
                back = shore
        if back is None:
            log("  swimming and no dry land within 200 yd")
            return False
        log(f"  swimming out of combat - {math.dist(back[:2], here[:2]):.0f} yd to dry land")
        set_state("travelling", detail="out of the water")
        # navigate(), not walk_to(): the mage gets WEDGED against the steep
        # Loch Modan bank while swimming, and a plain walk cannot free it -
        # the rest-time shore walk logged "still swimming after 3 tries" at
        # 06:06 and then drowned. navigate() brings the unstuck machinery
        # (jumps, backing along the walked trail) with it.
        if not self.swim_out(back):
            self.navigate(*back, stop_short=1.5, timeout=45, combat_ok=True)
        after = self.g.player()
        if after.move_flags & self.SWIM_FLAG:
            log(f"  still in the water after the attempt "
                f"({math.dist((after.x, after.y), here[:2]):.0f} yd moved)")
        return True

    def use_slot(self, slot: int, key: str, on_self: bool = False):
        """Use an action slot. Lua first, the key only as a fallback.

        The client binds ACTIONBUTTON11/12 to "-" and "=", but those are
        US-layout keys and this PC is Danish: the "=" we post arrives as a
        different key entirely, so the food press was silently dropped.
        """
        try:
            # 0.5 s here plus cast()'s own 0.3 s wait was 0.8 s of standing
            # still per ability; cast() does the waiting
            # no wait of its own: cast() polls for the result straight after
            self.bridge.send(br.USE_SLOT_SELF if on_self else br.USE_SLOT, slot, wait=0.0)
        except Exception:
            self.kb.press(key)

    HEAL_TO = 90.0

    def heal_after_fight(self, me, full=False):
        """Top up with the class's own heal instead of eating. Casts until
        HEAL_TO (95 for a full rest) or until one more cast would take mana
        under the pull threshold. Returns the fresh player, or None."""
        heal = self.profile.rest_heal
        if heal is None or not self.has(heal.name):
            return None
        # not 95 for a full rest: a Holy Light for the last 6% (94 -> 100 at
        # 17:57) spent a quarter of the mana and the rest then sat 6 s to
        # drink it back
        goal = self.HEAL_TO
        floor = self.profile.pull_mana if self.profile.pull_mana is not None else self.args.pull_mana
        hp0, casts = me.hp_pct, 0
        while (me.hp_pct < goal and not me.in_combat and not me.dead
               and not self.attackers(me) and casts < 6):
            cost = self.__dict__.get("_spell_cost", {}).get(heal.name, 0)
            cost_pct = 100.0 * cost / max(me.max_power, 1) if cost else 15.0
            if me.mana_pct - cost_pct < floor:
                break
            if casts == 0:
                set_state("resting", detail="healing")
                self.stop()
                self.stand_up()
            if not self.cast(heal, me):
                # the first press often lands in the global cooldown of the
                # buff cast just before (Blessing of Might after the loot):
                # Ddqr sat down to eat at 48% hp / 68% mana at 17:41 instead
                # of one Holy Light. Wait the GCD out and try once more.
                time.sleep(1.6)
                me = self.g.player()
                if me.in_combat or self.attackers(me) or not self.cast(heal, me):
                    if casts == 0:
                        log(f"  {heal.name} did not go off (hp {me.hp_pct:.0f}%, "
                            f"mana {me.mana_pct:.0f}%) - resting instead")
                    break
            casts += 1
            me = self.g.player()
        if casts:
            log(f"healed after the fight: hp {hp0:.0f}% -> {me.hp_pct:.0f}% "
                f"with {casts} {heal.name}, mana {me.mana_pct:.0f}%")
        return me

    def rest_if_needed(self, me) -> bool:
        if me.in_combat:
            return False
        if self.g.player().dead:
            return False
        if self.attackers(me):
            # something is coming for us: sitting down to eat at 2% with a
            # Young Black Bear on the way was death at 15:15 - fight it
            return False
        # Rest below the pull thresholds, not just when nearly empty: the next
        # pull may bring an add, and a half-empty mage loses that fight.
        full = getattr(self, "need_full_rest", False)
        hp_before = me.hp_pct
        me = self.heal_after_fight(me, full) or me
        healed = me.hp_pct > hp_before + 1
        low_hp = me.hp_pct < ((self.HEAL_TO if self.profile.rest_heal else 95)
                              if full else self.args.pull_hp)
        # Rage and energy are not resources you rest to refill: a warrior
        # sits at 0 rage out of combat, so "low_mana" was true on every tick
        # and a full-health warrior walked away from nearby mobs to sit down
        # for a drink it cannot take. Only mana users rest for power.
        pull_mana = self.profile.pull_mana if self.profile.pull_mana is not None else self.args.pull_mana
        # a self-healer's mana is its health bar too, but sitting from 91% to
        # 95% after every town visit (Ddqr, 17:47) buys one tick of regen
        full_mana = 80 if self.profile.rest_heal else 95
        low_mana = (self.profile.drinks
                    and me.mana_pct < (full_mana if full else pull_mana))
        # Once we are sitting down anyway, EAT. The thresholds above decide
        # whether a rest is worth starting; they were also deciding whether
        # to touch the food, so a rest triggered by mana at 55% with health
        # at 78% drank to full and then waited on natural regeneration to
        # crawl to 92%. Conjured food is free and eating stacks with
        # drinking, so the only cost of eating is a keypress.
        # (a class that heals itself stopped at HEAL_TO on purpose: no bread
        # for the last few percent)
        if (low_hp or low_mana) and me.hp_pct < (self.HEAL_TO if self.profile.rest_heal else 95):
            low_hp = True
        self.need_full_rest = False
        if not (low_hp or low_mana):
            return healed            # a heal was the rest: not "did nothing"
        log(f"resting: hp {me.hp_pct:.0f}%  mana {me.mana_pct:.0f}%")
        import navmesh
        for attempt in range(3):
            if not (me.move_flags & 0x00200000 and self.nav):
                break
            # Swimming: you cannot sit in the loch, and drowning damage does
            # not stop. The mage drowned at 03:00 because this asked for the
            # nearest reachable poly and got the lake bed it was floating
            # over - water polys are walkable by design. Require NAV_GROUND.
            here = (me.x, me.y, me.z)
            # Fully dry first, then a ground-and-water shore poly (a ford is
            # standable, so it is good enough to sit on, but 20 yd of real
            # land beats it).
            # far enough out to actually be on land: a 2 yd "shore" left us
            # swimming three tries running in Lake Everstill
            back = self.dry_land_near(here)
            if back is None:
                back = self.nav.reachable_near(here, here, radius=200.0, tries=0,
                                               require=navmesh.NAV_GROUND,
                                               exclude=navmesh.NAV_WATER)
            if back is None or math.dist(back[:2], here[:2]) > 60.0:
                shore = self.nav.reachable_near(here, here, radius=200.0, tries=0,
                                                require=navmesh.NAV_GROUND)
                if shore is not None and (back is None
                                          or math.dist(shore[:2], here[:2]) + 25.0
                                          < math.dist(back[:2], here[:2])):
                    back = shore
            if back is None:
                log("  swimming and no dry poly within 200 yd - resting where we are")
                break
            log(f"  swimming - heading {math.dist(back[:2], (me.x, me.y)):.0f} yd "
                f"to dry land (try {attempt + 1})")
            set_state("travelling", detail="to the shore")
            if not self.swim_out(back):
                self.walk_to(*back, 1.5, 60)
            me = self.g.player()
            if not me.move_flags & 0x00200000:
                log("  on dry land")
                break
        else:
            log("  still swimming after 3 tries - resting anyway")
        # next to mobs above our level a partial rest is a gamble - fill up
        near_strong = any(u.level > me.level and me.dist(u) < 40 and not u.dead
                          for u in self.aggressors())
        set_state("resting")
        self.stop()
        # sitting down 10 yd from the next wolf of the pack is how a chain of
        # three fights ends: step out of their aggro range first
        # at 20% hp in Saldean's fields a wanderer 25 yd away is the next
        # death: the lower we are, the wider the berth, and look twice
        for _ in range(2):
            gap = 35.0 if me.hp_pct < 50 else 22.0
            near = [u for u in self.aggressors() if me.dist(u) < gap and u.target != me.guid]
            if not near:
                break
            p = self.retreat_point(me, near, 30.0 if me.hp_pct < 50 else 20.0)
            if not p:
                break
            log(f"  {len(near)} mobs within {gap:.0f} yd - moving away before sitting down")
            self.navigate(*p, stop_short=2.0, timeout=20)
            me = self.g.player()
            if me.in_combat:
                return False
        if not self.profile.drinks:
            low_mana = False
            if not low_hp:
                return False
        self.fix_consumable_slots()
        # Only press a slot we have something for. At 12:39, after the Elder
        # Crag Boar kill, the warrior had no food: it pressed the empty slot
        # 12 twice, logged "not sitting after the food/water key" and stood
        # regenerating from 75% to 92%. With nothing to eat, sit down anyway:
        # VMaNGOS gives 1.5x out-of-combat health regen when not standing.
        food, drink = self.pick_consumables()
        eat = low_hp and food is not None
        sip = low_mana and drink is not None
        if sip:
            self.use_slot(11, KEY_WATER)
            time.sleep(1.6)                  # the drink triggers the global cooldown: a
                                             # food press 0.4 s later was ignored
        if eat:
            self.use_slot(12, KEY_FOOD)
        if not (eat or sip):
            self.bridge.send(br.SIT)
            log("  nothing to eat or drink - sitting down for regen")
        t0 = time.time()
        bite = t0
        # did we actually sit down to eat/drink? if not, the slot is wrong:
        # place the items again and press once more
        time.sleep(1.5)
        if not (eat or sip):
            if not self.g.player().sitting and not self.g.player().in_combat:
                self.bridge.send(br.SIT)
        elif not self.g.player().sitting and not self.g.player().in_combat:
            # This used to send the conjured-only place commands (62/63).
            # They find an item by the word "Conjured" in its tooltip, so
            # for a warrior carrying bread they placed nothing at all and
            # the retry pressed the same empty slot. Go through the slot
            # fixer, which places by entry.
            log(f"  not sitting after the food/water key - re-placing "
                f"({food and food.name}, {drink and drink.name}) and pressing again")
            self.fix_consumable_slots()
            if sip:
                self.use_slot(11, KEY_WATER)
                time.sleep(0.4)
            if eat:
                self.use_slot(12, KEY_FOOD)
            time.sleep(1.5)
            fails = self.__dict__.setdefault("_eat_fails", {})
            if (not self.g.player().sitting and not self.g.player().in_combat
                    and not self.g.player().dead
                    and not self.attackers(self.g.player())):
                # (not while dead: at 0% hp nothing goes down, and 09:25
                # counted the run's best food as a failure)
                # believe the client over the database: whatever is on the
                # bar will not go down, so stop offering it and let the
                # next rest pick the next best thing. Twice, in separate
                # rests: a bear arriving mid-bite made the warrior drop its
                # only food (Freshly Baked Bread) for the whole run
                for pick in (food if eat else None, drink if sip else None):
                    if pick is not None:
                        fails[pick.entry] = fails.get(pick.entry, 0) + 1
                        if fails[pick.entry] < 2:
                            log(f"  {pick.name} did not go down - trying it again next rest")
                            continue
                        consumables.reject(pick.entry, "pressed twice, never sat")
                        log(f"  {pick.name} will not go down - dropping it "
                            f"from the menu for this run")
        while time.time() - t0 < 75 and not self.stopped():
            time.sleep(0.5)
            me = self.g.player()
            if me.dead:
                return False             # 72 s "resting" at 0% hp (09:25)
            if me.in_combat:
                log("attacked while resting")
                return False
            # rage sits at 0 out of combat: for a warrior "mana" never fills,
            # and every rest ran the whole 75 s timer at 100% hp
            mana = me.mana_pct if self.profile.drinks else 100.0
            if me.hp_pct >= 99 and mana >= 99:
                break                       # full: up at once, not on a timer
            if not full and me.hp_pct >= 92 and mana >= 90 and not near_strong:
                # Standing up at 82% was too thin: an even-level mob costs
                # this mage ~50% hp, so the next pull plus one add is a
                # death. Two deaths in 20 minutes in Loch Modan, each a
                # 1300 yd corpse run - far dearer than a few more seconds
                # of drinking.
                break
            if time.time() - bite > 27:
                # one bite of bread runs out at 84% hp: standing up there
                # and pulling the next goretusk ended at 24%. Eat again.
                bite = time.time()
                if sip and me.mana_pct < 95:
                    self.use_slot(11, KEY_WATER)
                    time.sleep(1.6)
                if eat and me.hp_pct < 95:
                    self.use_slot(12, KEY_FOOD)
                if not (eat or sip) and not me.sitting:
                    self.bridge.send(br.SIT)
        self.stand_up()
        log(f"rested: hp {me.hp_pct:.0f}%  mana {me.mana_pct:.0f}%")
        return True

    def has(self, name: str) -> bool:
        """Is this spell on its action bar slot (i.e. learned and placed)?"""
        slot = self.profile.bar().get(name)
        if slot is None:
            return False
        # the slot must hold a rank of THIS spell, not whatever was there before
        return spells.action_bar(self.p)[slot - 1] in self.aura_ids(name)

    def press_spell(self, name: str, on_self: bool = False):
        """Fire an ability by its action slot, through Lua when we can.

        The digit keys are as unreliable as "-" and "=" were: posting "4"
        at 29 rage moved nothing three times running, while UseAction(4)
        cast Battle Shout and took the 10 rage. The warrior had been
        auto-attacking its way through Coldridge with the whole rotation
        silently dropped, so slots go through the bridge now and the key
        press is only the fallback.
        """
        self.stand_up()
        slot = self.profile.bar()[name]
        self.use_slot(slot, spells.KEY_FOR_SLOT[slot], on_self=on_self)

    def stand_up(self):
        """Eating/drinking leaves us seated, and nothing can be cast seated."""
        if not self.g.player().sitting:
            return
        self.bridge.send(br.STAND)
        t0 = time.time()
        while time.time() - t0 < 2 and self.g.player().sitting:
            time.sleep(0.1)

    def ready(self, name: str) -> bool:
        return time.time() >= self.cd.get(name, 0)

    def buff_if_needed(self, me) -> bool:
        if me.in_combat:
            return False
        cast_at = self.__dict__.setdefault("_buff_cast_at", {})
        for buff in self.profile.buffs:
            name = buff.name
            # A timed buff is refreshed with 75 s left, so it does not run
            # out in the middle of the next fight (we only know its age if we
            # cast it this run; otherwise wait for it to fall off).
            fading = (buff.duration > 0 and name in cast_at
                      and time.time() - cast_at[name] > buff.duration - 75)
            # a weapon enchant shows no aura: cast it once per run, then on the timer
            missing = (name not in cast_at) if buff.weapon else not self.has_aura(me, name)
            if (self.has(name) and (fading or missing)
                    and me.mana_pct >= buff.min_power_pct):
                if not self.ready(name):
                    continue
                log(f"casting {name}")
                set_state("buffing")
                self.stop()
                # Friendly buffs fail on a hostile target (we usually still
                # have the last kill selected), so cast explicitly on self.
                # onSelf: no target write, and success is the aura itself -
                # an aura with no mana cost (Devotion Aura) always read as
                # "did not go off" when judged by mana
                mana = self.g.player().power
                self.press_spell(name, on_self=True)
                t_b = time.time()
                went = False
                while time.time() - t_b < 1.6 and not went:
                    time.sleep(0.15)
                    p_ = self.g.player()
                    went = p_.power < mana or (not fading and not buff.weapon and self.has_aura(p_, name))
                if went:
                    # the global cooldown: a second buff pressed inside it
                    # fails and waits 60 s
                    time.sleep(max(0.0, 1.55 - (time.time() - t_b)))
                if not went:
                    log(f"  {name} did not go off - retrying in 60 s")
                    self.cd[name] = time.time() + 60
                else:
                    cast_at[name] = time.time()
                return True
        return False

    def food_stock(self, items=None):
        """(food, drink) we carry, conjured or bought."""
        items = items if items is not None else self.g.bag_items()
        me = self.g.player()
        # what we would actually eat, not what is merely food-shaped: a
        # quest turn-in we are carrying does not count as a meal, and
        # neither does anything the client has already refused
        vf, vd = edible(items, me.level, self.quest_reserved())
        food = sum(c for e, c in items.items() if e in vf)
        drink = sum(c for e, c in items.items() if e in vd)
        return food, drink

    def food_floor(self, me, have) -> int:
        """Money to keep back when buying food. Nearly out, food beats every
        other claim on the purse: a level-8 warrior with 3.18s against a 3.20s
        training floor walked 250 yd to Innkeeper Belm, bought nothing, and
        rested on natural regen (40 s a rest) until it died. Above that the
        floor is the rest of the ladder - the food rung itself excluded, or
        the money set aside to buy food would forbid buying food."""
        if have < FOOD_CRITICAL:
            return 0
        # Training is NOT a claim that outranks eating. reserve_parts prices
        # the trainer at money=10**9 - every spell the character is owed at
        # this level, whether or not it could ever pay today - so an untrained
        # level-10 paladin reserved ~1200c and turned this into "cannot afford
        # food (0g3s)" with 300c in the bags. With nothing to eat it cannot
        # rest, so it walked 12 minutes from 82% hp down to 7% and was killed
        # at 5 yd. A spell we cannot afford is a missed upgrade; food we
        # cannot buy ends the run.
        return self.reserve(me, exclude=("food", "train"))

    def drink_floor(self, me, have) -> int:
        """Money to keep back when buying drink - the same rule as food_floor.

        Drink had no critical-stock escape at all, only a flat "is anything
        spendable?" test, and from level 38 the mount rung alone (33g, 67g,
        100g) is more than a levelling character's whole purse. So a drinking
        class simply stopped restocking water at 38 and rested on natural
        regen until it was richer than the mount. A mount you cannot reach
        because you are sitting down is not worth saving for.
        """
        if have < FOOD_CRITICAL:
            return 0
        # Training is NOT a claim that outranks eating. reserve_parts prices
        # the trainer at money=10**9 - every spell the character is owed at
        # this level, whether or not it could ever pay today - so an untrained
        # level-10 paladin reserved ~1200c and turned this into "cannot afford
        # food (0g3s)" with 300c in the bags. With nothing to eat it cannot
        # rest, so it walked 12 minutes from 82% hp down to 7% and was killed
        # at 5 yd. A spell we cannot afford is a missed upgrade; food we
        # cannot buy ends the run.
        return self.reserve(me, exclude=("food", "train"))    # food covers the drink rung too

    def needs_shopping(self, me) -> str | None:
        """Why we should visit a vendor for consumables, or None.

        A mage conjures its own and never needs this; a warrior cannot, and
        sitting for natural regen is the slowest thing the bot does.
        """
        food, drink = self.food_stock()
        wants_drink = self.profile.drinks and self.profile.conjure_water is None
        # A dedicated trip only when it is cheap or we are nearly out. With a
        # few rests left, the next vendor we pass (sell, train, repair, a
        # hand-in in town) tops us up anyway: Ugibd left a quest area with 4
        # food for a 978 yd walk to Kharanos, ~6 minutes there and back.
        near = self.vendor_near(me, drink=False) if food < FOOD_LOW else True
        if self.profile.conjure_food is None and food < FOOD_LOW and (food <= 1 or near):
            # the cheapest tier we can pay for, not just the best one: see
            # affordable_consumable (55c never reached the 125c bread)
            entry, _ = affordable_consumable(vendor_ladder("Food"), me.level,
                                             me.money - self.food_floor(me, food))
            if entry is not None:
                return f"food ({food} left)"
        if wants_drink and drink < FOOD_LOW and (drink <= 1 or self.vendor_near(me, drink=True)):
            # exactly the food test, against the drink ladder and drink_floor:
            # can we pay for a rung at all? (It used to be "is anything at all
            # spendable after the whole reserve?", which the mount rung alone
            # answers with no from level 38.)
            entry, _ = affordable_consumable(vendor_ladder("Drink"), me.level,
                                             me.money - self.drink_floor(me, drink))
            if entry is not None:
                return f"drink ({drink} left)"
        return None

    FOOD_TRIP_NEAR = 300.0   # yd: a seller this close is worth a detour at any stock

    def vendor_near(self, me, drink=False) -> bool:
        """Is a seller of our food (drink) within FOOD_TRIP_NEAR? Cached 60 s:
        food_vendor queries the world DB."""
        key = ("drink" if drink else "food")
        cache = self.__dict__.setdefault("_vendor_near", {})
        hit = cache.get(key)
        if hit and time.time() - hit[0] < 60 and math.dist(hit[1], (me.x, me.y)) < 50:
            return hit[2]
        v = self.food_vendor(me, drink=drink, quiet=True)
        near = bool(v) and math.dist(v["pos"][:2], (me.x, me.y)) <= self.FOOD_TRIP_NEAR
        cache[key] = (time.time(), (me.x, me.y), near)
        return near

    def buy_consumables(self, me) -> int:
        """At an open merchant: top food (and drink) up to FOOD_STOCK."""
        bought = 0
        wants = [(vendor_ladder("Food"), 12, "food")]
        if self.profile.drinks and self.profile.conjure_water is None:
            wants.append((vendor_ladder("Drink"), 11, "drink"))
        # only what this merchant stocks: a general-supplies vendor with water
        # and no bread got "bought food: 4541 x0 (wanted 1)", 5.3 s of
        # waiting for bread that was never on sale (10:06)
        stock = self.merchant_stock()
        for ladder, slot, what in wants:
            if stock is not None:
                ladder = [x for x in ladder if x[1] in stock]
                if not ladder:
                    continue
            have = self.food_stock()[0 if what == "food" else 1]
            if FOOD_STOCK - have <= 0:
                continue
            # live read: the greys were sold a moment ago, the only income
            money = self.g.player().money
            floor = (self.food_floor(me, have) if what == "food"
                     else self.drink_floor(me, have))
            if have >= 2:
                # keep what the guide is about to spend at a vendor: 4 food
                # left bought 25 more and left 8c, and the Thunder Ale (50c)
                # two steps later could not be paid - Bitter Rivals and its
                # two follow-ups were skipped (09:19)
                floor += self.guide_shopping_reserve()
            entry, price = affordable_consumable(ladder, me.level, money - floor)
            if entry is None:
                log(f"  cannot afford {what} ({money // 10000}g{money % 10000 // 100}s)")
                continue
            # BUY_ITEM n loops BuyMerchantItem(i, 1), and each of those buys a
            # whole vendor stack of 5: "4541 x10 (wanted 2)" was 2 stacks.
            # Count stacks, not items, or a rich bot buys 5x FOOD_STOCK.
            want = -(-(FOOD_STOCK - have) // 5)
            afford = max(0, (money - floor) // max(price, 1))
            n = int(min(want, afford, 20))
            if have < FOOD_CRITICAL and self.reserve_parts(me).get("weapon", 0) > 0:
                # nearly out, so the floor is 0 - but a full 20 spends the
                # weapon money again: one stack gets us to the next vendor
                n = min(n, 1)
            if n <= 0:
                log(f"  cannot afford {what} ({money // 10000}g{money % 10000 // 100}s)")
                continue
            if self.g.bag_item_count() >= self.g.bag_capacity() and not self.g.bag_items().get(entry):
                log(f"  no free bag slot for {what}")
                break
            before = self.g.bag_items().get(entry, 0)
            self.bridge.send(br.BUY_ITEM, entry * 100 + n, wait=0.2)
            # poll rather than a flat 2 s: the stacks land in ~0.3 s here
            t_buy = time.time()
            while time.time() - t_buy < 2.0:
                after = self.g.bag_items().get(entry, 0)
                if after >= before + 5 * n:
                    break
                time.sleep(0.15)
            after = self.g.bag_items().get(entry, 0)
            log(f"  bought {what}: {entry} x{after - before} (wanted {n}), "
                f"money {money} -> {self.g.player().money}")
            if after > before:
                bought += after - before
                self.bridge.send(br.PLACE_ITEM, entry * 100 + slot, wait=0.8)
            else:
                # this merchant is not selling to us (wrong window, sold out,
                # no room): the drink attempt after a failed food buy failed
                # the same way every time - 2-3 s each (17:16, 18:27)
                break
        return bought

    _stock_cache: dict = {}

    def merchant_stock(self):
        """Item entries the targeted merchant sells, or None if unknown."""
        try:
            u = self.g.unit(self.g.target_guid())
            if u is None or not u.entry:
                return None
            if u.entry not in self._stock_cache:
                rows = worlddb._query(
                    f"SELECT DISTINCT item FROM npc_vendor WHERE entry = {int(u.entry)}")
                self._stock_cache[u.entry] = {int(r[0]) for r in rows}
            return self._stock_cache[u.entry] or None
        except Exception:
            return None

    def quest_reserved(self) -> set[int]:
        """Items a quest in our log wants handed in - do not eat those.

        Eighteen ordinary foods double as quest objectives. Refreshing
        Spring Water is one of them and is also the drink every vendor
        sells, so the ban has to be about the quest we are on, not the
        item.
        """
        try:
            return consumables.quest_items(self.g.quest_log().keys())
        except Exception:
            return set()

    def pick_consumables(self, items=None, level=None):
        """The best food and drink in the bags, as (food, drink) or None.

        "Best" is the biggest restore the character is high enough to use.
        Picking the biggest STACK instead, which is what this did, put a
        level-1 ration on the bar while better food sat unused two slots
        over.
        """
        items = items if items is not None else self.g.bag_items()
        lvl = level if level is not None else self.g.player().level
        avoid = self.quest_reserved()
        food = consumables.best(items, lvl, "Food", avoid)
        drink = consumables.best(items, lvl, "Drink", avoid)
        return (food[0] if food else None), (drink[0] if drink else None)

    def fix_consumable_slots(self, items=None) -> bool:
        """Slots 11/12 must hold the best food/drink we actually carry.

        Two separate failures lived here. The slot was only re-placed when
        it held nothing at all, so a worse food stayed on the bar for the
        whole run; and the conjured-only bridge commands (62/63) match an
        item by the word "Conjured" in its tooltip, which no looted haunch
        and no vendor bread has ever had. Place by entry and check the
        entry. True if anything was re-placed.
        """
        items = items if items is not None else self.g.bag_items()
        bar = spells.action_bar(self.p)
        lvl = self.g.player().level
        food, drink = self.pick_consumables(items, lvl)
        want = [(12, food, "food")]
        # a warrior has no mana: managing slot 11 only spent a bridge round
        # trip every 20 s and filled the log with "re-placing water"
        if self.profile.drinks:
            want.insert(0, (11, drink, "water"))
        fixed = False
        for slot, pick, what in want:
            if pick is None:
                continue
            v = bar[slot - 1]
            held = v & 0x7FFFFFFF if v & 0x80000000 else None
            if held == pick.entry and items.get(held, 0) > 0:
                continue
            # an equal food already on the bar stays: ties go to the bigger
            # stack, eating shrinks it, and Tough Jerky and Tough Hunk of
            # Bread (61 hp each) swapped places 7 times in an hour, 1.3 s each
            cur = consumables.table().get(held) if held else None
            if (cur is not None and items.get(held, 0) > 0 and cur.kind == pick.kind
                    and cur.restore >= pick.restore and cur.req_level <= lvl):
                continue
            log(f"  slot {slot}: putting {pick.name} x{items.get(pick.entry, 0)} "
                f"on the bar (held {held or 'nothing'})")
            self.bridge.send(br.PLACE_ITEM, pick.entry * 100 + slot, wait=0.8)
            fixed = True
        return fixed

    def ensure_water(self, me) -> bool:
        """Conjure water / food when we run low; keep them on '-' and '='."""
        cw, cf = self.profile.conjure_water, self.profile.conjure_food
        items = self.g.bag_items()
        # only when the bags or the level changed: the check reads the bar and
        # queries the DB for every food and drink we carry - 1.4 s after
        # every kill for a paladin whose bags had not changed (08:49)
        sig = (me.level, tuple(sorted(items.items())))
        if (not me.in_combat and time.time() - getattr(self, "_slot_check", 0) > 20
                and sig != getattr(self, "_slot_sig", None)):
            self._slot_check = time.time()
            self._slot_sig = sig
            self.fix_consumable_slots(items)
        water = sum(c for e, c in items.items() if e in CONJURED_WATER)
        food = sum(c for e, c in items.items() if e in CONJURED_FOOD)
        # keep a small stock: two waters is one rest, and a rest with none
        # is natural regen only
        if water < 5 and self._conjure(me, cw, f"water ({water} left)", br.PLACE_WATER, 11):
            return True
        return food < 5 and self._conjure(me, cf, f"food ({food} left)", br.PLACE_FOOD, 12)

    def _conjure(self, me, ab, what, place_cmd, slot) -> bool:
        if me.in_combat or ab is None or not self.has(ab.name) or not self.ready(ab.name):
            return False
        if me.mana_pct < 60:
            return False
        log(f"conjuring {what}")
        set_state("conjuring")
        self.stop()
        mana = self.g.player().power
        self.press_spell(ab.name)
        time.sleep(3.6)
        if self.g.player().power >= mana:
            log("  conjure did not go off - retrying in 60 s")
            self.cd[ab.name] = time.time() + 60
            return True
        self.bridge.send(place_cmd, slot)
        # Conjuring spends the mana the rest just bought: rested to 92%,
        # conjured water and food, pulled at ~62% (over the 60% floor), an
        # add joined and the mage died at 1% mana (04:45). Top up again - a
        # sip with the fresh water - before the next pull.
        self.need_full_rest = True
        return True

    # ------------------------------------------------------------ town trips
    # ------------------------------------------------------------ route
    def update_stage(self, me) -> bool:
        """Move to the route stage for our level. True if we travelled."""
        if not self.route:
            return False
        st = routes.stage_for(self.route, me.level, (me.x, me.y))
        if self.stage is st:
            return False
        self.stage = st
        self.home = tuple(st["center"])
        self.radius = st["radius"]
        self.stage_mobs = set(st.get("mobs") or [])
        d = math.dist(self.home[:2], (me.x, me.y))
        log(f"level {me.level}: stage '{st['name']}' ({st['min_level']}-{st['max_level']}), "
            f"{d:.0f} yd away")
        if d > st["radius"]:
            # arrive fit: travel may run into a fight on the way
            self.buff_if_needed(self.g.player())
            self.need_full_rest = True
            self.rest_if_needed(self.g.player())
            set_state("travelling", detail=st["name"])
            # Westfall -> Redridge / Duskwood is 3000+ yd: give long moves time
            self.navigate(*self.home, stop_short=10.0, timeout=self.travel_timeout(d, 900.0),
                          careful=True)
        return True

    # ------------------------------------------------------------ NPC services
    def wanders(self, entry: int) -> bool:
        """Waypoint-walking NPCs (Antonio Perelli the travelling salesman) are
        never where the spawn table says."""
        cache = getattr(self, "_wander_cache", {})
        if entry not in cache:
            rows = worlddb._query(f"SELECT MAX(movement_type) FROM creature WHERE id={entry}")
            cache[entry] = bool(rows and rows[0][0] not in (None, "NULL", "0"))
            self._wander_cache = cache
        return cache[entry]

    UNREACHABLE_NPCS: set[int] = {
    279,        # Morgan Pestle, Stormwind: the shop doorway wedged the character 150+ times
}

    # Teleport area triggers (areatrigger_teleport): (trigger x, y, z), radius,
    # (landing x, y, z). Stormwind's Wizard's Sanctum floor is on the navmesh
    # but joined to the city only through these.
    PORTALS = [((-9020.09, 880.50, 151.58), 7.0, (-9017.46, 885.90, 29.62)),   # Sanctum entrance
               ((-9023.26, 892.40, 33.51), 7.0, (-9014.94, 873.33, 148.62))]   # Sanctum exit
    # The Deeprun Tram (map 369): an areatrigger at each end teleports us in,
    # the track bed is walked end to end (the cars are client-side animations
    # the bot cannot see; 2000 yd, five minutes) and the trigger at the far
    # end teleports us out. (entry trigger, radius, landing, exit trigger,
    # radius, landing) - triggers from AreaTrigger.dbc, landings from
    # areatrigger_teleport.
    # The entry/exit targets are mesh polygons inside each trigger's radius
    # that the street side actually reaches (the trigger centres sit on the
    # ramps beyond a mesh gap): SW entrance 2173 r10, IF exit 2166 (box),
    # IF entrance 2175 r10, SW exit 2171 r10.
    TRAM = [("Stormwind -> Ironforge", (-8350.1, 517.9, 91.8), 1.0, (67.76, 2490.98, -4.3),
             (76.03, 10.5, -4.3), 2.0, (-4839.51, -1317.74, 501.87)),
            ("Ironforge -> Stormwind", (-4839.5, -1326.1, 502.0), 1.0, (69.23, 10.39, -4.3),
             (75.5, 2490.8, -4.2), 1.0, (-8354.23, 524.07, 91.8))]
    TRAM_BBOX = (-250.0, -80.0, 250.0, 2650.0)     # x0, y0, x1, y1 of the tram map
    TRAM_SECONDS = 400.0            # the cars are client-side animations the bot cannot
                                    # board, so ride_tram WALKS the 2500 yd track bed
    # Whether two points are joined by land is a question the navmesh already
    # answers, and it used to be answered by a line on the map instead:
    # NORTH_X = -7300 split Eastern Kingdoms in two and anything crossing it
    # rode the tram. The original observation was true - Ironforge is reached
    # from Stormwind by tram, because the land route is a level-50 zone and a
    # locked gate - but the coordinate generalises to nothing. WorldMapArea.dbc
    # puts Searing Gorge at x=-6844 and its neighbour Burning Steppes at
    # x=-8007, so a thirty-second walk between two adjacent zones became a full
    # Stormwind<->Ironforge tram crossing, twice, about fifteen minutes; and the
    # whole level 30-60 eastern block (Hillsbrad, Arathi, Alterac, the
    # Hinterlands, both Plaguelands, Tirisfal, Silverpine, the Badlands) sits
    # "north", so every trip from there to Duskwood or Stranglethorn was priced
    # and walked as a tram ride. Ask the mesh instead, and cache the answer per
    # corridor so the extra pathfind is paid once.
    LAND_MIN_TRIP = 600.0   # below this no tram approach can ever pay: skip the pathfind
    LAND_FILE = os.path.join(HERE, "data", "land_reach.json")
    LAND_FAIL_TTL = 6 * 3600.0   # a remembered "no route" is only believed this long:
                                 # find_path also comes back empty when it runs out of
                                 # its time or node budget, and one unlucky search must
                                 # not teach the bot for ever that a corridor has no
                                 # walking route (data/land_reach.json held exactly one
                                 # such row, and it took the tram across the continent)

    def _land_cell(self, p) -> tuple:
        return (int(round(p[0] / 500.0)), int(round(p[1] / 500.0)))

    # Rings searched around an unreachable goal for a spot on our own patch of
    # mesh. Wide enough to step out of a building (Ironforge's bank floor is
    # 12 yd across, the Stormwind mage tower's upper floor sits 30 yd above the
    # street): over the 838 map-0 service NPCs these settings leave exactly one
    # unresolved, against 36 for a plain nearest_poly test.
    LAND_NEAR_RADII = (6.0, 12.0, 20.0, 30.0, 45.0, 60.0, 90.0, 130.0)
    LAND_NEAR_DZ = (0.0, -5.0, 5.0, -10.0, 10.0, -20.0, 20.0, -35.0, 35.0)

    def mesh_near(self, goal, mine=None):
        """The nearest spot to `goal` that sits on the mesh - on OUR patch of
        it when `mine` is given - or None.

        nearest_poly finding *a* polygon at the goal is not the same as that
        polygon being walkable to. 23 of the 838 service NPCs on map 0 stand on
        a patch of mesh the streets do not join - Bretta Goldfury on the
        Ironforge bank floor (component 11558), Honni Goldenoat in a Thelsamar
        house (12371) - and find_path to them fails instantly, exactly as it
        does for the 13 who have no polygon at all. Neither is "there is no
        land route to that city", so resolve to the doorway instead, which is
        the spot _navigate walks to anyway.

        The height ladder matters as much as the rings: reachable_near only
        looks 6 yd up or down, so in Ironforge it answers with the ledge over
        the lava rather than the Great Forge floor 45 yd above it.
        """
        comp = self.nav.components() if mine is not None else None
        for r in self.LAND_NEAR_RADII:
            for k in range(12):
                th = math.pi * k / 6.0
                x, y = goal[0] + r * math.cos(th), goal[1] + r * math.sin(th)
                for dz in self.LAND_NEAR_DZ:
                    p, _ = self.nav.nearest_poly(x, y, goal[2] + dz)
                    if p is not None and (comp is None or comp.get(p) == mine):
                        return (x, y, goal[2] + dz)
        return None

    def land_goal(self, a, b):
        """The point we would really walk to for `b`, and the yards left over.

        The mesh has no polygon within 8 yd of 13 of the 838 service NPCs on
        map 0 - an NPC standing on an inn floor the mesh does not join to the
        street (Karm Ironquill and Brock Stoneseeker in Thelsamar, Lyesa
        Steelbrow in Ironforge...). find_path to such a point fails instantly,
        which is NOT "there is no land route": _navigate already handles it by
        walking to the nearest reachable spot instead (see its reachable_near
        branch), so price the trip the same way rather than concluding that
        walking is impossible and riding the tram across the continent.

        A polygon that is there but is NOT joined to ours fails the same way
        and used to be handed to find_path unchanged, so the route length came
        back unknown and the caller priced the walk at the straight line - a
        lower bound that shrinks with the mount while the tram's 400 s ride
        does not, which flipped Stormwind <-> Ironforge to walking over the
        mountain wall above 8.76 yd/s. Ask which patch the goal is on, not
        merely whether a polygon exists.
        """
        goal = tuple(b[:3])
        try:
            comp = self.nav.components()
            s_poly, _ = self.nav.nearest_poly(*tuple(a[:3]))
            mine = comp.get(s_poly) if s_poly is not None else None
            g_poly, _ = self.nav.nearest_poly(*goal)
            if g_poly is not None and (mine is None or comp.get(g_poly) == mine):
                return goal, 0.0
            alt = (self.mesh_near(goal, mine)
                   or self.nav.reachable_near(tuple(a[:3]), goal, radius=12.0, tries=0)
                   or self.nav.reachable_near(tuple(a[:3]), goal, radius=25.0, tries=0))
        except Exception:
            return goal, 0.0
        if alt is None:
            return goal, 0.0
        return tuple(alt[:3]), math.dist(alt[:2], goal[:2])

    def land_start(self, a):
        """Where the pathfind should start: us, or the nearest mesh if we are
        standing off it. find_path snaps the start by 8 yd only, so a character
        in a spot the mesh does not cover makes every route come back unknown
        and the walk gets priced at the straight line again. The skipped yards
        are not lost: land_yards measures the leg from our real position to the
        first path point."""
        start = tuple(a[:3])
        try:
            if self.nav.nearest_poly(*start)[0] is not None:
                return start
            alt = self.mesh_near(start)
        except Exception:
            return start
        return tuple(alt[:3]) if alt else start

    def land_yards(self, a, b) -> float | None:
        """Length of the walking route from a to b, or None if there is none.

        Route length, not "is there a route": the mesh is built from terrain
        and happily reports a 6081 yd scramble over the mountain wall between
        Stormwind and Ironforge, which is a shorter LINE than the tram but a
        longer TRIP. A boolean cannot express that; seconds can, so this
        returns the raw number and the callers compare it with the tram.

        Route length is symmetric, so the cache is keyed on the unordered pair
        of 500-yd cells: one pathfind per corridor, remembered across runs.
        """
        if not self.nav:
            return None
        straight = math.dist(a[:2], b[:2])
        if straight <= self.LAND_MIN_TRIP:
            return straight
        ca, cb = sorted((self._land_cell(a), self._land_cell(b)))
        key = f"{self.map_id}:{ca[0]},{ca[1]}>{cb[0]},{cb[1]}"
        cache = self.__dict__.get("_land_reach")
        if cache is None:
            try:
                cache = json.load(open(self.LAND_FILE, encoding="utf-8"))
            except Exception:
                cache = {}
            self._land_reach = cache
        v = cache.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)             # a remembered route length
        if (isinstance(v, dict) and time.time() - v.get("fail", 0) < self.LAND_FAIL_TTL):
            return None                 # a remembered failure, still fresh
        # anything else - a bare null from the old format of this file, a bool,
        # or a failure older than LAND_FAIL_TTL - is not evidence: look again.
        start = self.land_start(a)
        goal, left = self.land_goal(start, b)
        try:
            skw = {"max_nodes": 1500000, "budget": 150.0} if straight > 700 else {}
            path = self.nav.find_path(start, goal, **skw)
        except Exception as exc:
            log(f"  (land-route check failed: {exc}) - assuming the straight line")
            return straight
        length = None
        if path:
            pts = [tuple(a[:2])] + [tuple(p[:2]) for p in path]
            length = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) + left
            log(f"  land route {a[0]:.0f},{a[1]:.0f} -> {b[0]:.0f},{b[1]:.0f}: "
                f"{length:.0f} yd ({straight:.0f} straight)")
        else:
            log(f"  no land route {a[0]:.0f},{a[1]:.0f} -> {b[0]:.0f},{b[1]:.0f} "
                f"({straight:.0f} yd straight)")
        cache[key] = length if length is not None else {"fail": int(time.time())}
        try:
            os.makedirs(os.path.dirname(self.LAND_FILE), exist_ok=True)
            json.dump(cache, open(self.LAND_FILE, "w", encoding="utf-8"))
        except Exception:
            pass
        return length

    def tram_leg(self, me):
        """The Deeprun Tram leg whose entrance is nearer to us."""
        return min(self.TRAM, key=lambda L: math.dist(L[1][:2], (me.x, me.y)))

    def tram_seconds(self, me, dest) -> float | None:
        """Door to door seconds of riding the tram to `dest`, or None when the
        tram is not on this map at all."""
        if self.map_id != 0:
            return None
        leg = self.tram_leg(me)
        entrance, exit_at = leg[1], leg[6]
        return (self.ground_seconds(math.dist(entrance[:2], (me.x, me.y)))
                + self.TRAM_SECONDS
                + self.ground_seconds(math.dist(exit_at[:2], dest[:2])))

    def walk_seconds_to(self, me, dest) -> float:
        """Seconds to WALK there: the route the mesh offers, or - when the mesh
        cannot answer - the straight line.

        A mesh that returns nothing has not said "walking is impossible", only
        "I could not find it": the destination may sit off the mesh (13 of the
        838 service NPCs on map 0 do), or the search may have run out of its
        budget. The straight line is a lower bound on the walk, which is the
        honest thing to compare against and keeps the answer on the side of
        walking rather than committing to a tram ride across the continent.
        """
        yards = self.land_yards((me.x, me.y, me.z), tuple(dest[:3]))
        if yards is None:
            yards = math.dist((me.x, me.y), dest[:2])
        return self.ground_seconds(yards)

    def ground_seconds_to(self, me, dest) -> float:
        """The best NON-flying way to get there, in seconds: walk the route the
        mesh actually offers, or ride the tram, whichever is quicker. This is
        the number a flight has to beat."""
        walk = self.walk_seconds_to(me, dest)
        tram = self.tram_seconds(me, dest)
        return walk if tram is None else min(walk, tram)

    def prefer_tram(self, me, dest) -> bool:
        """Ride the tram rather than walk? Only when walking is genuinely
        slower. This replaces a coordinate test (NORTH_X) that answered the
        question without looking at either.

        "The mesh gave me no route" is NOT "there is no land route" - it is
        also what an off-mesh doorway and an exhausted search budget look
        like - so an unknown walk is priced at the straight line and loses to
        the tram only when even that lower bound is slower. Stormwind <->
        Ironforge still takes the tram: the mesh really does offer a 6500 yd
        scramble over the mountains there, and 6500 yd is slower than the ride.

        That lower bound shrinks with the mount while the tram's 400 s ride
        does not, so it must stay a last resort: land_goal/land_start resolve
        an off-mesh or unjoined endpoint onto our own patch of mesh first, and
        the straight line is only reached when there is no polygon at all
        within 130 yd of the place we are going.
        """
        if self.map_id != 0:
            return False
        if math.dist((me.x, me.y), dest[:2]) <= self.LAND_MIN_TRIP:
            return False
        tram = self.tram_seconds(me, dest)
        if tram is None:
            return False
        return self.walk_seconds_to(me, dest) > tram

    def map_on_terrain(self) -> bool:
        return self.continent() in (0, 1)

    def continent(self) -> int:
        """0 = Eastern Kingdoms (also inside the Deeprun Tram), 1 = Kalimdor:
        the map whose creature spawns, flight points and services apply."""
        return 1 if getattr(self, "map_id", 0) == 1 else 0

    def nav_for(self, mid):
        if mid not in self.navs:
            try:
                from navmesh import NavMesh
                self.navs[mid] = NavMesh(map_id=mid)
            except Exception as e:
                log(f"  no navmesh for map {mid} ({e})")
                self.navs[mid] = None
        return self.navs[mid]

    def sync_map(self, me) -> int:
        """Pick the navmesh for where we are. The tram map's coordinates fall
        in open sea on the continent, so the position alone tells them apart."""
        mid = self.g.map_id()
        if mid not in (0, 1, 369):
            bx = self.TRAM_BBOX
            mid = 369 if (bx[0] < me.x < bx[2] and bx[1] < me.y < bx[3]) else 0
        if mid != self.map_id:
            nav = self.nav_for(mid)
            if nav is not None:
                self.map_id, self.nav = mid, nav
                log(f"  on map {mid}: {len(nav.tiles)} navmesh tiles")
        return self.map_id

    PATH_CACHE_AGE = 180.0           # seconds a remembered route stays usable

    # ------------------------------------------------------------- flights
    # Travel never considered the taxi: fly_to was only reachable from a
    # guide [F] step, so a level-16 trip to the Ironforge trainer was a
    # 2000 yd walk past Rockjaw camps instead of a 30 s flight from
    # Thelsamar, whose flight master stands 100 yd from the road.
    # Three yard thresholds used to decide this and all three were wrong in a
    # different way. FLIGHT_MIN_GAIN was dead (defined, never read).
    # FLIGHT_MIN_TRIP 800 yd is a distance where the real question is a fixed
    # cost, not a distance: flying cannot win a trip shorter than talking to
    # the flight master and taking off. FLIGHT_MAX_WALK 1500 yd refuses an
    # 1800 yd walk to Gadgetzan that saves a 12000 yd run - a 6:1 win rejected
    # before the arithmetic that would have found it. The arithmetic is the
    # rule; every bound below is derived from the trip being priced.
    FLIGHT_RETRY = 600.0             # after a failed attempt, walk for a while
    MOUNT_LEVEL = 40                 # riding is trained at 40 in 1.12
    MOUNT_SAVE_FROM = 38             # start putting the mount money aside two levels out

    def speeds(self) -> SpeedModel:
        m = self.__dict__.get("_speeds")
        if m is None:
            m = self._speeds = SpeedModel()
        return m

    def speed_key(self, mounted=None):
        return (self.continent(), bool(self.mounted() if mounted is None else mounted))

    def eff_speed(self, mounted=None) -> float:
        return self.speeds().speed(self.speed_key(mounted))

    def taxi_nodes_here(self) -> dict:
        """Known flight points that sit on the continent we are on."""
        cache = self.__dict__.setdefault("_taxi_here", {})
        key = self.continent()
        if key not in cache:
            out = {}
            for name, (code, land) in br.TAXI_NODES.items():
                try:
                    if self.nav and self.nav.nearest_poly(*land, max_dist=40.0)[0] is not None:
                        out[name] = (code, land)
                except Exception:
                    pass
            cache[key] = out
        return cache[key]

    def ground_seconds(self, yards: float, mounted=None) -> float:
        """Seconds to cover this much ground, at the speed we actually make."""
        return yards / max(self.eff_speed(mounted), SpeedModel.LO)

    def travel_timeout(self, yards: float, floor: float = 120.0) -> float:
        """How long to allow a trip before calling it stuck. This was a
        scattering of `d / 3.0` (3 yd/s, guessed) with a `max(900, ...)` floor;
        it is the same measured speed as everything else now, with a generous
        multiple so an ordinary fight on the way does not abort the walk."""
        return max(floor, 3.0 * self.ground_seconds(yards))

    def try_flight(self, me, x, y, z) -> bool:
        """Fly when flying is actually faster, in seconds. True if we flew.

        This used to be a yardage rule of thumb ("flight master within 400 yd,
        hop must save 600 yd"), which walked the mage 4540 yd across Loch Modan
        and killed it twice. The client ships the real numbers: TaxiPathNode.dbc
        has every flight path as a polyline and the taxi flies it at a fixed
        32 yd/s, so the comparison is arithmetic. Checked against vanilla:
        Stormwind->Ironforge computes 4.0 min, Ironforge->Stormwind 3.3 min,
        Stormwind->Lakeshire 1.8 min.

        The arithmetic was right and the gates around it were not: three yard
        thresholds decided which trips were allowed to be priced at all, and
        only one source flight master was ever considered. Now the trip itself
        is the bound - every flight master we could reach is crossed with every
        flight point we know, and the ground leg is priced at the speed we are
        measured to make (see SpeedModel), not at a guessed constant."""
        if time.time() < getattr(self, "_no_flight_until", 0) or getattr(self, "_flying", False):
            return False
        # _navigate asks on every pass of its walking loop. Without the old
        # 800 yd pre-filter that is several Dijkstras a second, so remember a
        # "no" for this destination for a few seconds; the answer cannot change
        # faster than we can walk.
        seen = self.__dict__.setdefault("_flight_no", {})
        key = (round(x / 50), round(y / 50))
        if time.time() < seen.get(key, 0):
            return False
        seen[key] = time.time() + 15.0
        if len(seen) > 200:
            seen.clear()
        known = self.flights_known()
        if not known:
            return False
        import taxi
        # The straight line is not the alternative: the ground option is the
        # route the mesh offers or the tram, whichever is quicker. Price that,
        # or flying loses to a line the character cannot actually walk.
        on_foot = self.ground_seconds_to(me, (x, y, z))
        # A trip shorter than the fixed cost of flying cannot be won by flying,
        # whatever the distance: that, not FLIGHT_MIN_TRIP's 800 yd, is the floor.
        if on_foot < taxi.TAKEOFF + taxi.HOP_DELAY:
            return False
        known_ids = {taxi.node_by_name(n) for n in known}
        known_ids.discard(None)
        # Every flight master we could reach, not just the first one
        # nearest_service happened to return out of the twelve it was asked for.
        sources = []
        for fm in self.nearest_services(me, "flightmaster"):
            nid = taxi.nearest_node(fm["pos"], max_yards=150.0, map_id=self.map_id)
            if nid is not None:
                sources.append((nid, math.dist(fm["pos"][:2], (me.x, me.y)), fm))
        dests = [(taxi.node_by_name(name), name, math.dist(land[:2], (x, y)))
                 for name, (code, land) in self.taxi_nodes_here().items() if name in known]
        eff = self.eff_speed()
        best = best_flight([(nid, d, fm) for nid, d, fm in sources], dests, known_ids,
                           eff, on_foot, taxi.route_seconds, takeoff=taxi.TAKEOFF)
        if best is None:
            return False
        total, fm, dest, air = best
        fm_walk = math.dist(fm["pos"][:2], (me.x, me.y))
        log(f"  flying to {dest} from {fm['name']} ({fm_walk:.0f} yd away): "
            f"{total/60:.1f} min door to door ({air/60:.1f} in the air) "
            f"against {on_foot/60:.1f} min on foot at {eff:.1f} yd/s")
        self._flying = True
        # fly_to finds its own flight master; make sure it finds the one this
        # arithmetic was done for, not whichever is nearest.
        self._prefer_fm = (fm["entry"], round(fm["pos"][0]), round(fm["pos"][1]))
        try:
            r = self.fly_to(dest, max_walk=max(fm_walk + 50.0, 150.0))
        except Exception as exc:
            log(f"  flight attempt failed: {exc}")
            r = "failed"
        finally:
            self._flying = False
            self._prefer_fm = None
        if r != "done":
            self._no_flight_until = time.time() + self.FLIGHT_RETRY
            return False
        self._path_cache = None                # the remembered route starts elsewhere now
        self._leg_teleported = True            # do not price this leg as walking
        return True

    def resume_path(self, me, x, y, z, avoid=(), dangers=(), ahead=0.0):
        """The remembered route to this destination, re-joined from where we
        are now, or None. A trip interrupted by a fight used to re-plan the
        whole way (10 s of A* per interruption on a 2000 yd walk); a stuck
        spot only needs a detour back onto it, not a new route to Ironforge.

        ahead: rejoin at least this far along the route from the nearest
        point, so a detour aims past whatever we are wedged on.
        """
        c = getattr(self, "_path_cache", None)
        if not c or not self.nav:
            return None
        goal, path, ts = c
        if math.dist(goal[:2], (x, y)) > 2.0 or time.time() - ts > self.PATH_CACHE_AGE:
            return None
        here = (me.x, me.y)
        if not path:
            return None
        # nearest point ON the route (not the nearest waypoint): a waypoint
        # we had already passed was "nearest", and re-joining there turned
        # the character round to run back and touch it - very bot-like
        seg, frac, d = 0, 0.0, math.dist(path[0][:2], here)
        for i in range(len(path) - 1):
            ax, ay = path[i][:2]
            bx, by = path[i + 1][:2]
            L2 = (bx - ax) ** 2 + (by - ay) ** 2
            f = 0.0 if L2 < 1e-6 else max(0.0, min(1.0, ((here[0] - ax) * (bx - ax) + (here[1] - ay) * (by - ay)) / L2))
            dd = math.dist((ax + (bx - ax) * f, ay + (by - ay) * f), here)
            if dd < d:
                seg, frac, d = i, f, dd
        if d > 80.0:
            return None                      # too far off the route to splice
        if not ahead and d < 4.0 and abs(path[min(seg + 1, len(path) - 1)][2] - me.z) < 5:
            rest = path[seg + 1:] or path[-1:]
            log(f"  back on the remembered route, {len(rest)} waypoints left")
            return rest
        # merge in ahead of us, further ahead the further off we are, so the
        # way back is a shallow diagonal rather than a right angle
        want = max(ahead, min(25.0, 8.0 + 1.5 * d))
        a, b = path[seg], path[min(seg + 1, len(path) - 1)]
        k, left = seg, math.dist(a[:2], b[:2]) * (1.0 - frac)
        pt = tuple(a[c] + (b[c] - a[c]) * frac for c in range(3))
        while True:
            nxt = path[k + 1] if k + 1 < len(path) else None
            if nxt is None:
                pt = path[-1]
                break
            if left >= want:
                f = want / max(left, 1e-6)
                pt = tuple(pt[c] + (nxt[c] - pt[c]) * f for c in range(3))
                break
            want -= left
            pt = nxt
            k += 1
            left = math.dist(path[k][:2], path[k + 1][:2]) if k + 1 < len(path) else 0.0
        rest = [pt] + list(path[k + 1:])
        head = self.nav.find_path((me.x, me.y, me.z), pt, avoid=avoid, dangers=dangers,
                                  holes=self.obstacles, budget=8.0)
        if not head:
            return None
        log(f"  re-joining the remembered route {math.dist(pt[:2], here):.0f} yd on, "
            f"{len(rest)} waypoints left")
        return list(head) + rest[1:]

    def check_trees(self, path, avoid) -> int:
        """Add the trunks of trees standing on the planned line to `avoid`.
        The navmesh was built without them: in the pine grove south-east of
        Kharanos the bot wedged 22 times in 2 minutes against a row of
        Ironforgesnowtree/Wintertree trunks on flat ground. The model box
        includes the canopy, so use the spawn point (the trunk) and a
        radius scaled from the model width."""
        import vmap
        m = self.continent()
        if m not in (0, 1):
            return 0
        added = 0
        seen = set()
        for a, b in zip(path, path[1:]):
            d = math.dist(a[:2], b[:2])
            n = max(1, int(d / 4.0))
            for i in range(n + 1):
                px, py = a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n
                pz = a[2] + (b[2] - a[2]) * i / n
                for sp in vmap.spawns_near(px, py, 8.0, map_id=m):
                    if not sp.m2 or "tree" not in sp.name.lower() or id(sp) in seen:
                        continue
                    seen.add(id(sp))
                    if abs(sp.z - pz) > 4.0:
                        continue
                    # big snow trees block far more than a trunk: the bot
                    # wedged 9 yd from an 18 yd-wide Wintertree04 near
                    # Amberstill and 6-10 yd from a 13 yd one in the grove
                    r = (0.45 * sp.width if sp.width > 6.0
                         else min(1.8, max(0.4, 0.08 * sp.width))) + 1.0
                    # distance from the trunk to this segment
                    L = d * d or 1.0
                    f = max(0.0, min(1.0, ((sp.x - a[0]) * (b[0] - a[0]) + (sp.y - a[1]) * (b[1] - a[1])) / L))
                    if math.dist((sp.x, sp.y), (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))) > r:
                        continue
                    if any(math.dist(v[:2], (sp.x, sp.y)) < 1.0 for v in avoid):
                        continue
                    avoid.append((sp.x, sp.y, sp.z, r))
                    added += 1
        return added

    STEEP_GROUND = 1.3     # rise/run over 3 yd of real terrain. Measured over 204 real
                           # stuck-learned faces the median is 1.10 (p90 1.38, max 2.02);
                           # 99.5% of 1,837 successful uphill legs are under 1.3 (max
                           # 1.63), which is why the threshold sits here. It is a
                           # prediction either way: the two overlap, so what this test
                           # produces is a soft block, not a permanent one.

    def check_climbs(self, path) -> int:
        """Block the steep faces a planned path would walk up, before we walk
        into them. The mesh smooths a hillside into big polygons whose edges
        look walkable; the terrain inside is not (the ridge north of Kharanos,
        the hill below MacGrann's: minutes of stuck-learn-replan, one face at
        a time). Only segments that run on the terrain count - bridges and
        buildings are not in the height grid.

        Everything blocked here is a PREDICTION (proven=False): nothing has
        tried to walk it. Predictions are session-only, they expire, and a
        search may relax them when they are the only thing in the way; only a
        face the character actually failed on becomes permanent (that is
        learn_blocked_climb's job).

        No terrain reading is ever read as "steep". A segment off the height
        grid - a bridge, a building floor, Ironforge, the Deeprun Tram (map 369
        has a navmesh and no .map files at all) - is skipped, not blocked.
        Unknown means allow, or this would wall the bot out of the cities."""
        import terrain
        m = self.continent()
        blocked = 0
        for a, b in zip(path, path[1:]):
            if b[2] <= a[2] + 1.0:
                continue
            ha, hb = terrain.height(m, a[0], a[1]), terrain.height(m, b[0], b[1])
            if ha is None or hb is None or abs(ha - a[2]) > 2.0 or abs(hb - b[2]) > 2.0:
                continue
            g, where = terrain.steepest_climb(m, a, b, window=3.0)
            if g <= self.STEEP_GROUND or where is None:
                continue
            d = math.dist(a[:2], b[:2]) or 1.0
            back = (where[0] - (b[0] - a[0]) / d * 3.0, where[1] - (b[1] - a[1]) / d * 3.0)
            zb, zw = terrain.height(m, *back), terrain.height(m, *where)
            p0, _ = self.nav.nearest_poly(back[0], back[1], zb, max_dist=4.0) if zb is not None else (None, None)
            p1, _ = self.nav.nearest_poly(where[0], where[1], zw, max_dist=4.0) if zw is not None else (None, None)
            if p1 is None:
                continue
            n = (int(self.nav_call("block_climb", p0, p1, proven=False))
                 if p0 is not None and p0 != p1 else 0)
            n += self.nav_call("block_uphill_into", p1, proven=False)
            # the whole face, not one step of it: blocked one step at a time
            # the next plan simply went up the polygon beside it
            n += self.block_steep_ground(m, p1, where, proven=False)
            if n:
                blocked += n
                events.emit("climb_blocked", x=round(where[0], 1), y=round(where[1], 1),
                            dz=round(g, 2), pre=True)
        return blocked

    def block_steep_ground(self, m, start, where, radius=10.0, proven: bool = False) -> int:
        """Block the way up into every polygon within `radius` yd of `where`
        whose terrain is steeper than STEEP_GROUND.

        This is the blanket rule, and it is guesswork by construction: it
        blocks a whole face because one step of it measured steep. Hence
        proven=False by default - session-only, relaxable, expiring."""
        import terrain

        def grade(x, y, r=1.5):
            try:
                return math.hypot(terrain.height(m, x + r, y) - terrain.height(m, x - r, y),
                                  terrain.height(m, x, y + r) - terrain.height(m, x, y - r)) / (2 * r)
            except TypeError:
                return 0.0
        seen, todo, n = {start}, [start], 0
        while todo:
            a = todo.pop()
            c = self.nav.tiles[a[0]].center[a[1]]
            wx, wy = float(c[2]), float(c[0])          # recast (wowY, wowZ, wowX)
            if grade(wx, wy) > self.STEEP_GROUND:
                n += self.nav_call("block_uphill_into", a, proven=proven)
            for b, _, _ in self.nav.neighbours(a):
                cb = self.nav.tiles[b[0]].center[b[1]]
                if b not in seen and math.dist((float(cb[2]), float(cb[0])), where) < radius:
                    seen.add(b)
                    todo.append(b)
        return n

    def learn_blocked_climb(self, me) -> bool:
        """Stuck facing uphill: remember the polygon step we could not climb."""
        if not self.nav:
            return False
        try:
            cur, _ = self.nav.nearest_poly(me.x, me.y, me.z)
            if cur is None:
                return False
            zc = float(self.nav.tiles[cur[0]].center[cur[1]][1])
            ang = self._heading
            for step in (1.5, 3.0, 4.5, 6.0):
                px, py = me.x + step * math.cos(ang), me.y + step * math.sin(ang)
                z = self.nav.ground_z(px, py, hint=me.z + step)
                if z is None:
                    continue
                nb, _ = self.nav.nearest_poly(px, py, z, max_dist=2.0)
                if nb is None or nb == cur:
                    continue
                zn = float(self.nav.tiles[nb[0]].center[nb[1]][1])
                if zn > zc + 8.0:
                    continue       # the floor above a cave roof, not the climb in front
                if self.nav.in_building(cur) or self.nav.in_building(nb):
                    # indoors a big step up is the next floor, not a slope:
                    # stuck against the Northshire Abbey library wall, the bot
                    # "learned" the upper floor (+7.2 yd) as unclimbable, twice,
                    # and that permanent block sits on the stairs to the trainer
                    return False
                # this one is proof, not a guess: the character stood here and
                # could not get up it. It is the only kind of block that is
                # permanent (block_climb's proven=True default).
                if zn > zc + 0.5 and self.nav.block_climb(cur, nb):
                    more = (self.nav_call("block_steep_patch", cur, proven=False)
                            if self.nav.max_edge_slope(nb) > 0.7 else 0)
                    log(f"  learned: the climb ahead ({zn - zc:+.1f} yd) is not walkable - "
                        f"never routing up it again" + (f" (nor {more} more steep steps around it)" if more else ""))
                    events.emit("climb_blocked", x=round(me.x, 1), y=round(me.y, 1), dz=round(zn - zc, 1))
                    return True
                return False
        except Exception as e:
            log(f"  (learning the climb failed: {e})")
        return False

    def densify_slopes(self, me, path):
        """Click-to-move walks straight between waypoints; across a hillside
        that straight line leaves the mesh's line and runs into the steep
        bit. Steep pieces get a waypoint every 3 yd so it follows the plan."""
        if not path:
            return path
        out, prev = [], (me.x, me.y, me.z)
        for p in path:
            run = math.dist(prev[:2], p[:2])
            if run > 4.0 and abs(p[2] - prev[2]) / run > 0.35:
                n = int(run / 3.0)
                for k in range(1, n):
                    f = k / n
                    out.append((prev[0] + (p[0] - prev[0]) * f, prev[1] + (p[1] - prev[1]) * f,
                                prev[2] + (p[2] - prev[2]) * f))
            out.append(p)
            prev = p
        return out

    def densify_indoors(self, me, path):
        """Click-to-move walks straight between waypoints and happily crosses a
        roof or a wall the mesh path went round (Jern Hornhelm's house, dug
        into a slope: the character arrived on the roof, 7 yd above him).
        Inside or next to a building the waypoints are spaced 2 yd apart."""
        if not path or not getattr(self.nav, "in_building", None):
            return path
        try:
            ends = [self.nav.nearest_poly(*p, max_dist=3.0)[0] for p in (path[-1], (me.x, me.y, me.z))]
            indoors = any(n is not None and self.nav.in_building(n) for n in ends)
        except Exception:
            indoors = False
        if not indoors:
            return path
        out = [path[0]]
        for a, c in zip(path, path[1:]):
            d = math.dist(a[:2], c[:2])
            n = max(1, int(d / 2.0))
            for k in range(1, n + 1):
                f = k / n
                out.append((a[0] + (c[0] - a[0]) * f, a[1] + (c[1] - a[1]) * f, a[2] + (c[2] - a[2]) * f))
        return out

    def _wait_teleport(self, land, secs) -> bool:
        t1 = time.time()
        while time.time() - t1 < secs:
            try:
                me = self.g.player()
            except Exception:
                time.sleep(0.5)                  # loading screen
                continue
            if math.dist((me.x, me.y), land[:2]) < 40 and abs(me.z - land[2]) < 25:
                self._leg_teleported = True   # ground covered at no walking speed
                return True
            time.sleep(0.3)
        return False

    def ride_tram(self, leg) -> bool:
        name, trig, r, land369, exit369, r2, land0 = leg
        log(f"  taking the Deeprun Tram ({name})")
        set_state("travelling", detail=f"Deeprun Tram {name}")
        try:
            self.navigate(*trig, stop_short=r, timeout=1200, careful=True, via_portal=True)
        except (RuntimeError, MemoryError_, NotInWorld):
            pass                                  # the loading screen hit mid-walk
        if not self._wait_teleport(land369, 15):
            log("  the tram entrance did not take us through")
            return False
        time.sleep(1.5)
        self.sync_map(self.g.player())
        set_state("travelling", detail="walking the Deeprun Tram tunnel")
        try:
            ok = self.navigate(*exit369, stop_short=r2, timeout=900, via_portal=True)
        except (RuntimeError, MemoryError_, NotInWorld):
            ok = True
        if not self._wait_teleport(land0, 15):
            if ok:
                self.walk_to(*exit369, timeout=6)          # a nudge into the trigger box
            if not self._wait_teleport(land0, 10):
                log("  the tram exit did not take us through")
                return False
        time.sleep(1.5)
        self.sync_map(self.g.player())
        log(f"  off the tram at ({land0[0]:.0f}, {land0[1]:.0f})")
        self._leg_teleported = True            # do not price this leg as walking
        if hasattr(self, "learn_nearby_flight"):
            self.learn_nearby_flight(300.0)
        return True

    # ------------------------------------------------------------- boats
    # Transports (gameobject type 15) are client-animated; the live position
    # and heading sit in the model struct at *(object+0x210) (+0x44 xyz,
    # +0x50 heading). A docked boat waits 60 s at its stop (TaxiPathNode.dbc,
    # delay 60). On deck the client reports and takes boat-LOCAL coordinates.
    # Boarding: the railing has a gap at boat-local y ~ +16..19, x ~ -1..7,
    # reached from the pier by walking at the boat's origin + 4 yd along x.
    # Leaving: (-8,0) -> (-8,+-10) -> (-4,+-17) -> (-3,+-24) toward the pier
    # side. Verified live on the Menethil -> Theramore boat (2026-09-20).
    BOATS = [
        {"name": "Menethil Harbor -> Theramore", "entry": 176231, "from_map": 0, "to_map": 1,
         "stop": (-3905.2, -585.8), "pier": (-3900.0, -603.0, 5.4),
         "far_stop": (-4016.4, -4740.6), "far_pier": (-4008.3, -4726.7, 5.3)},
        {"name": "Theramore -> Menethil Harbor", "entry": 176231, "from_map": 1, "to_map": 0,
         "stop": (-4016.4, -4740.6), "pier": (-4008.3, -4726.7, 5.3),
         "far_stop": (-3905.2, -585.8), "far_pier": (-3900.0, -603.0, 5.4)},
        {"name": "Menethil Harbor -> Auberdine", "entry": 176310, "from_map": 0, "to_map": 1,
         "stop": (-3709.5, -575.1), "pier": (-3737.4, -574.3, 6.0),
         "far_stop": (6406.2, 823.1), "far_pier": (6421.7, 814.9, 3.8)},
        {"name": "Auberdine -> Menethil Harbor", "entry": 176310, "from_map": 1, "to_map": 0,
         "stop": (6406.2, 823.1), "pier": (6421.7, 814.9, 3.8),
         "far_stop": (-3709.5, -575.1), "far_pier": (-3737.4, -574.3, 6.0)},
    ]
    BOAT_POS_PTR, BOAT_POS_OFF = 0x210, 0x44

    def boat_state(self, entry):
        """(x, y, z, heading) of a boat the client knows, or None."""
        import struct as _st
        for base, guid, typ in self.g.iter_objects():
            if typ != O.TYPE_GAMEOBJECT:
                continue
            d = self.p.u32(base + O.OBJ_DESCRIPTORS)
            if self.p.u32(d + O.OBJECT_FIELD_ENTRY) != entry:
                continue
            ptr = self.p.u32(base + self.BOAT_POS_PTR)
            if not (0x00400000 <= ptr < 0x7FFF0000) or ptr % 4:
                return None
            raw = self.p.try_read(ptr + self.BOAT_POS_OFF, 16)
            return None if raw is None else _st.unpack("<4f", raw)
        return None

    def on_boat(self, me) -> bool:
        return abs(me.x) < 60 and abs(me.y) < 60

    def pick_boat(self, me, x, y, dest_map):
        """The crossing with the least walking on both shores."""
        best = None
        for bt in self.BOATS:
            if bt["from_map"] != self.continent() or bt["to_map"] != dest_map:
                continue
            cost = math.dist((me.x, me.y), bt["pier"][:2]) + math.dist(bt["far_pier"][:2], (x, y))
            if best is None or cost < best[0]:
                best = (cost, bt)
        return best[1] if best else None

    def cross_sea(self, x, y, z, dest_map, timeout) -> bool:
        me = self.g.player()
        bt = self.pick_boat(me, x, y, dest_map)
        if bt is None:
            log(f"  no boat from map {self.continent()} to map {dest_map}")
            return False
        for attempt in range(3):
            if self.ride_boat(bt):
                self._leg_teleported = True    # a sea crossing is not walking speed
                return True
            log(f"  boat ride failed (attempt {attempt + 1})")
            if self.continent() == dest_map:
                return True
        return False

    def ride_boat(self, bt) -> bool:
        dest = bt["name"].split("-> ")[-1]
        log(f"  taking the boat {bt['name']}")
        set_state("travelling", detail=f"boat {bt['name']}")
        if not self.navigate(*bt["pier"], stop_short=1.0, timeout=1500, careful=True, via_portal=True):
            return False
        # wait for the boat to dock (up to a full round trip)
        t0 = time.time()
        while time.time() - t0 < 720 and not self.stopped():
            st = self.boat_state(bt["entry"])
            if st is not None and math.dist(st[:2], bt["stop"]) < 1.5:
                break
            time.sleep(1.0)
        else:
            log("  the boat never came")
            return False
        log("  the boat is in - boarding")
        self.ctm.move_to(bt["stop"][0] + 4.0, bt["stop"][1], 8.0)
        t1 = time.time()
        me = self.g.player()
        while time.time() - t1 < 8:
            time.sleep(0.5)
            me = self.g.player()
            if self.on_boat(me):
                break
        if not self.on_boat(me):
            if me.z < bt["pier"][2] - 3:
                log("  fell in the water next to the boat - swimming back to the pier")
                self.navigate(*bt["pier"], stop_short=1.0, timeout=120, via_portal=True)
            return False
        log(f"  aboard at boat-local ({me.x:.1f}, {me.y:.1f}) - sailing to {dest}")
        set_state("travelling", detail=f"on the boat to {dest}")
        # the crossing: the loading screen empties the object manager for a second
        t2 = time.time()
        lost = None
        while time.time() - t2 < 600 and not self.stopped():
            try:
                me = self.g.player()
            except Exception:
                lost = lost or time.time()
                time.sleep(0.5)
                continue
            if lost:
                lost = None
                time.sleep(1.0)
            st = self.boat_state(bt["entry"])
            if st is not None and math.dist(st[:2], bt["far_stop"]) < 1.5 and self.on_boat(me):
                break
            time.sleep(1.0)
        else:
            log("  the boat never reached the far dock")
            return False
        # walk off toward the pier side, in boat-local coordinates
        st = self.boat_state(bt["entry"])
        heading = st[3]
        dx, dy = bt["far_pier"][0] - st[0], bt["far_pier"][1] - st[1]
        c, s = math.cos(-heading), math.sin(-heading)
        side = 1.0 if (dx * s + dy * c) > 0 else -1.0
        for (lx, ly) in ((-8.0, 0.0), (-8.0, 10.0 * side), (-4.0, 17.0 * side), (-3.0, 24.0 * side)):
            self.ctm.move_to(lx, ly, 6.1)
            t3 = time.time()
            last = None
            while time.time() - t3 < 6:
                time.sleep(0.5)
                me = self.g.player()
                if not self.on_boat(me):
                    break
                if math.dist((me.x, me.y), (lx, ly)) < 1.5:
                    break
                if last and math.dist((me.x, me.y), last) < 0.1 and time.time() - t3 > 1.5:
                    break
                last = (me.x, me.y)
            if not self.on_boat(me):
                break
        me = self.g.player()
        if self.on_boat(me):
            log("  could not get off the deck")
            return False
        time.sleep(1.0)
        self.sync_map(me)
        log(f"  off the boat at ({me.x:.0f}, {me.y:.0f}) on map {self.map_id}")
        self.navigate(*bt["far_pier"], stop_short=1.0, timeout=60, via_portal=True)
        if hasattr(self, "learn_nearby_flight"):
            self.learn_nearby_flight(300.0)
        return True

    def nearest_services(self, me, service, **kw) -> list:
        """Every usable NPC offering this service, nearest first.

        try_flight used to take found[0] alone although it asked for twelve,
        so one blacklisted or badly placed flight master removed flying from a
        whole area. Callers that only want the first still get it from
        nearest_service; callers comparing options get the list.
        """
        bad = getattr(self, "npc_bad", {})
        found = [n for n in services.find(service, self.continent(), (me.x, me.y), me.faction,
                                          limit=12, **kw)
                 if bad.get((n["entry"], round(n["pos"][0]), round(n["pos"][1])), 0) < time.time()
                 and n["entry"] not in self.UNREACHABLE_NPCS
                 and not self.wanders(n["entry"])]
        if service in ("vendor", "repair"):
            # trainers that also sell (pet trainers, demon trainers...) often
            # stand in awkward places; prefer a proper merchant within +150 yd
            found.sort(key=lambda n: math.dist(n["pos"][:2], (me.x, me.y))
                       + (150 if "trainer" in (n.get("title") or "").lower() else 0))
        want = self.__dict__.get("_prefer_fm")
        if service == "flightmaster" and want:
            # try_flight priced a specific flight master; fly_to must walk to
            # that one, not to whichever happens to be nearest.
            found.sort(key=lambda n: (n["entry"], round(n["pos"][0]), round(n["pos"][1])) != want)
        return found

    def nearest_service(self, me, service, **kw):
        found = self.nearest_services(me, service, **kw)
        return found[0] if found else None

    # ------------------------------------------------- what the trainer still owes us
    def prof_skill(self, plan) -> dict[int, int]:
        """Trainer-plan spell -> the proficiency skill line it grants, for the
        entries that grant one. Cached per plan like spells._plans, because it
        is one query per trainer template and the answer never changes."""
        cache = self.__dict__.setdefault("_prof_skill", {})
        ids = tuple(sorted({int(x["learns"]) for x in plan}))
        if ids not in cache:
            out: dict[int, int] = {}
            if ids:
                try:
                    rows = worlddb._query(
                        "SELECT spell_id, skill_id FROM skill_line_ability "
                        f"WHERE build<={spells.BUILD} AND spell_id IN ({','.join(map(str, ids))})")
                    for sid, sk in rows:
                        sid, sk = int(sid), int(sk)
                        if sk in PROFICIENCY_SKILLS:
                            out[sid] = sk
                except Exception as exc:
                    log(f"  (proficiency lookup failed: {exc})")
            cache[ids] = out
        return cache[ids]

    def ledger_file(self) -> str:
        return os.path.join(HERE, "data", f"trained_{self.g.player_name()}.json")

    def trained_ledger(self, template) -> dict:
        """Per character, per trainer template: entries a real visit failed to
        teach. Without it a trainer entry the server will never hand over keeps
        the trainer check true for ever, and every town trip becomes a walk to
        a trainer that teaches nothing - repeating on the 600 s back-off for
        the rest of the run while bags, repairs and restocks queue behind it."""
        led = self.__dict__.get("_trained")
        if led is None:
            try:
                led = json.load(open(self.ledger_file(), encoding="utf-8"))
            except Exception:
                led = {}
            self._trained = led
        return led.setdefault(str(template), {})

    def note_unlearnable(self, template, entries, level, money):
        """After a visit that bought everything it could: whatever was due,
        affordable at the time, and still not learned is not learnable here."""
        led = self.trained_ledger(template)
        for x in entries:
            if x["cost"] > money:
                continue          # it was only ever too expensive - do not blame the trainer
            led[str(int(x["learns"]))] = int(level)
            log(f"  {x['name']} was not taught and was affordable: "
                f"not asking {template} for it again")
        try:
            os.makedirs(os.path.dirname(self.ledger_file()), exist_ok=True)
            json.dump(self._trained, open(self.ledger_file(), "w", encoding="utf-8"))
        except Exception:
            pass

    def due_at(self, me, template, money=None) -> list:
        """Plan entries this trainer could still teach us right now."""
        plan = spells.trainer_plan(template)
        if not plan:
            return []
        try:
            skills = gear.known_skills(self.g)
        except Exception:
            skills = set()
        due = due_training(plan, me.level,
                           me.money if money is None else money,
                           spells.known_spells(self.p), skills,
                           self.prof_skill(plan), self.trained_ledger(template))
        if due and money is None:
            # saving for a weapon: only the spells we cast are worth a trip
            # (and the money) before it is bought - see WEAPON_GOAL_GAIN
            try:
                wg = self.weapon_goal(me)
                if wg and me.money < wg[1] + 600:
                    p = self.profile
                    cast = {a.name for a in [*p.rotation, *p.emergency,
                                             *([p.crowd_control] if p.crowd_control else [])]}
                    names = self.__dict__.setdefault("_learn_names", {})
                    miss = [str(int(x["learns"])) for x in due if int(x["learns"]) not in names]
                    if miss:
                        for e, n in worlddb._query(
                                f"SELECT entry, name FROM spell_template WHERE entry IN ({','.join(miss)})"):
                            names[int(e)] = n
                    due = [x for x in due if names.get(int(x["learns"])) in cast]
            except Exception:
                pass
        return due

    def useful_trainer(self, me):
        """Nearest class trainer that teaches something we can learn and afford.

        "Learned" used to mean "in the client's SPELLBOOK array" alone, which
        is false for every proficiency: Dual Wield at 20, and Mail/Plate Mail
        at 40, are bought here and land on a skill line instead. From level 20
        on, that made this check permanently true. See entry_learned.
        """
        return self.nearest_service(me, "trainer", trainer_class=self.profile.trainer_class,
                                    trainer_type="class",
                                    where=lambda n: bool(self.due_at(me, n["trainer"]["template"])))

    def training_overdue(self, me, trainer) -> bool:
        """With a guide, the guide says when to train: its .train steps come
        when the route passes a trainer. Training on every level-up instead
        walked the dwarf off mid-quest and left those steps nothing to do.
        Still go on our own once a spell is two levels overdue - a skipped
        or filtered step must not leave the character behind for good."""
        plan = self.due_at(me, trainer["trainer"]["template"])
        due = [x["level"] for x in plan]
        if bool(due) and min(due) <= me.level - 2:
            return True
        # ... except the spells that feed resting: a mage without Conjure
        # Water sits on natural regen after every fight from level 4 until the
        # two-level rule fires at 6 ("nothing to eat or drink - sitting down
        # for regen" at 34% mana). Those are worth the walk the moment they
        # are due.
        sustain = {a.name for a in (self.profile.conjure_water, self.profile.conjure_food) if a}
        if sustain and plan:
            ids = ",".join(str(x["learns"]) for x in plan)
            try:
                names = {r[0] for r in worlddb._query(
                    f"SELECT DISTINCT name FROM spell_template WHERE entry IN ({ids})")}
            except Exception:
                names = set()
            if names & sustain:
                return True
        return False

    def find_npc(self, npc):
        near = [u for u in self.g.units() if u.entry == npc["entry"]]
        return min(near, key=lambda u: math.dist((u.x, u.y), npc["pos"][:2])) if near else None

    def visit_npc(self, npc):
        me = self.g.player()
        log(f"  going to {npc['name']} <{npc['title']}>, "
            f"{math.dist(npc['pos'][:2], (me.x, me.y)):.0f} yd")
        set_state("travelling", detail=f"to {npc['name']}")
        if math.dist(npc["pos"][:2], (me.x, me.y)) > 8:
            unit = self.find_npc(npc)
            pt = self.approach_point(me, unit) if unit else None
            # a vendor run through the Defias camp north of Sentinel Hill
            # (trapper 13 + smuggler 12) was a death: route round danger zones
            tmo = self.travel_timeout(math.dist(npc["pos"][:2], (me.x, me.y)), 600.0)
            if pt:
                self.navigate(*pt, stop_short=0.8, timeout=tmo, careful=True)
            else:
                self.navigate(*npc["pos"], stop_short=6.0, timeout=tmo, careful=True, dest_map=npc.get("map"))
        if self.g.player().in_combat:
            log(f"  attacked on the way to {npc['name']} - will come back")
            return None                        # not the NPC's fault: no blacklist
        unit = self.find_npc(npc)
        # Standing where the NPC should be but seeing nothing usually means the
        # object manager has not caught up yet, not that the NPC is missing.
        # It costs a rescan to find out and a 30 min blacklist to be wrong: a
        # spirit healer 8 yd away was declared unreachable four seconds after a
        # release teleport, twice, and each miss turned a 30 s graveyard revive
        # into a ~1200 yd corpse run. The same sequence succeeded on the death
        # before it, which is what a race looks like.
        for _ in range(4):
            if unit is not None or self.stopped() or self.g.player().in_combat:
                break
            m = self.g.player()
            if math.dist(npc["pos"][:2], (m.x, m.y)) > 12:
                break                          # genuinely not here; walk failed
            time.sleep(1.0)
            unit = self.find_npc(npc)
        ok = unit is not None and self.interact(unit)
        for _ in range(2):
            if ok or unit is None or self.stopped() or self.g.player().in_combat:
                break
            # a walking NPC: wait for them to stop at a waypoint of their
            # round, then try again from where they are
            if not self.wait_npc_still(unit):
                break
            ok = self.interact(self.g.unit(unit.guid) or unit)
        if not ok:
            if self.stopped():
                return None                    # a stop request cut the walk short
            # Escalate. A flat 30 min ban means an NPC we can NEVER reach is
            # walked to again three times an hour, for the whole run: Honni
            # Goldenoat was declared unreachable at 11:29 and again at 15:40,
            # and was being walked to a third time at 16:19. The first miss
            # can be a race or a wandering NPC and deserves a short ban; the
            # third is a fact about the world.
            self.npc_bad = getattr(self, "npc_bad", {})
            key = (npc["entry"], round(npc["pos"][0]), round(npc["pos"][1]))
            n = self.__dict__.setdefault("_npc_fails", {})
            n[key] = n.get(key, 0) + 1
            ban = (1800, 21600)[min(n[key], 3) - 1] if n[key] < 3 else 10 ** 9
            self.npc_bad[key] = time.time() + ban
            self.save_npc_bans()
            log(f"  could not reach {npc['name']} - avoiding them "
                + ("for the rest of the run" if n[key] >= 3
                   else f"for {ban // 60} min (miss {n[key]})"))
            return None
        return unit

    def wait_npc_still(self, unit, secs=15.0) -> bool:
        """Follow a wandering NPC until they stand still (or give up).
        True when they are standing within reach of a talk order."""
        t0, last = time.time(), None
        while time.time() - t0 < secs and not self.stopped():
            u = self.g.unit(unit.guid)
            if u is None:
                return False
            me = self.g.player()
            if last and math.dist((u.x, u.y), last) < 0.2:
                if me.dist(u) > 8:
                    self.navigate(u.x, u.y, u.z, stop_short=6.0, timeout=20)
                return True
            last = (u.x, u.y)
            if me.dist(u) > 12:
                self.navigate(u.x, u.y, u.z, stop_short=7.0, timeout=10)
            time.sleep(0.6)
        return False

    KEEP_ITEMS = {6948}                      # hearthstone

    def sell_list(self) -> list[int]:
        """Everything below green quality that is not food or drink, a quest
        item, unsellable, or the hearthstone."""
        bag = self.g.bag_items()
        if not bag:
            return []
        rows = worlddb._query(
            "SELECT entry, quality, class, sell_price, name FROM item_template WHERE entry IN (%s)"
            % ",".join(map(str, bag)))
        # ingredients of quests in the log are trade goods too: the stew's
        # vulture meat and goretusk snout went to Farmer Saldean for 27 copper
        keep = set(self.KEEP_ITEMS)
        qids = list(self.g.quest_log())
        if qids:
            for r in worlddb._query("SELECT ReqItemId1, ReqItemId2, ReqItemId3, ReqItemId4, SrcItemId "
                                    f"FROM quest_template WHERE entry IN ({','.join(map(str, qids))})"):
                keep |= {int(v) for v in r if v and v != "NULL" and int(v)}
        # ...and whatever the guide still has to collect or hand in.
        if getattr(self, "guide_active", False):
            try:
                keep |= self.guide_items()
            except Exception as exc:
                log(f"  (guide item list failed: {exc})")
        # The user's rule: keep green, rare and epic items, food + drink and
        # healing potions; sell everything else. White and grey gear, white potions, cloth,
        # meat and the rest all go. What still stays is what the bot cannot
        # sell or cannot do without: quest items and ingredients, unsellable
        # things (price 0), the hearthstone, and a spare bag while a bag slot
        # is free to put it in.
        import consumables
        eats = consumables.table()
        heal_pots = {e for e, _, _ in self.heal_potions()}
        out = []
        for entry, quality, cls, price, name in rows:
            entry, quality, cls, price = int(entry), int(quality), int(cls), int(price)
            if entry in keep or price == 0 or cls == 12:
                continue
            if cls == 1:
                # A bag is sold only when it can never be used: every bag
                # slot is taken AND it is no bigger than the smallest bag we
                # wear. A bigger one is kept - manage_bags swaps it in. (The
                # BAG_ITEMS test used to come first and skipped every bag
                # outright, so five 6-slot pouches sat in the backpack with
                # all four bag slots taken.)
                if (self.g.free_bag_slots() == 0
                        and self.bag_size(entry) <= min(n for n, _ in self.bag_layout())):
                    out.append(entry)
                continue
            if quality >= 2:                                   # green, blue, purple
                continue
            if entry in eats:                                  # food and drink
                continue
            if entry in heal_pots:                             # drunk below 20% hp in a fight
                continue
            out.append(entry)
        return out

    # Bags used to be a two-entry table - Brown Leather Satchel (8) and Small
    # Brown Pouch (6) - so the character was locked at 48 slots from about
    # level 12 to 60 however rich it got, and a Heavy Brown Bag (10) or Huge
    # Brown Sack (16) it could easily afford at 30 was never even considered.
    # Bag size sets the interval between vendor trips (16 backpack slots meant
    # a vendor run every 8 minutes), so this is travel time, not tidiness.
    # Ask the vendor tables instead.
    BAG_ITEMS = {4496, 4497, 4498, 4499, 828, 805, 5571, 5572, 5573}

    def bag_catalogue(self) -> list[tuple[int, int, int]]:
        """(entry, price, slots) for every container a vendor sells anywhere,
        biggest first then cheapest. Cached: it is one query per run."""
        if "_bag_cat" not in self.__dict__:
            cat: list[tuple[int, int, int]] = []
            try:
                # newest patch row per entry (item_template has one per patch
                # and they disagree on price), the way gear.items does it
                rows = worlddb._query(
                    "SELECT t.entry, t.buy_price, t.container_slots FROM npc_vendor v "
                    "JOIN item_template t ON t.entry = v.item "
                    "JOIN (SELECT entry, MAX(patch) p FROM item_template GROUP BY entry) mx "
                    "ON mx.entry = t.entry AND mx.p = t.patch "
                    "WHERE t.class = 1 AND t.buy_price > 0 AND t.container_slots > 0 "
                    "AND t.bag_family = 0 GROUP BY t.entry, t.buy_price, t.container_slots")
                cat = sorted(((int(e), int(p), int(s)) for e, p, s in rows),
                             key=lambda b: (-b[2], b[1]))
            except Exception as exc:
                log(f"  (bag catalogue lookup failed: {exc})")
            self._bag_cat = cat
            self.BAG_ITEMS = set(self.BAG_ITEMS) | {b[0] for b in cat}
        return self._bag_cat

    def best_bag(self, budget: int):
        """The largest container this budget buys, or None."""
        return next((b for b in self.bag_catalogue() if b[1] <= budget), None)

    def bag_size(self, entry: int) -> int:
        """Slots of a plain bag (0 for anything else, including quivers and
        soul bags). Cached per entry."""
        cache = self.__dict__.setdefault("_bag_size", {})
        if entry not in cache:
            rows = worlddb._query(
                "SELECT MAX(class), MAX(container_slots), MAX(bag_family) "
                f"FROM item_template WHERE entry = {int(entry)}")
            c, n, fam = (rows[0] if rows and rows[0][0] not in (None, "NULL") else (0, 0, 0))
            cache[entry] = int(n) if int(c) == 1 and int(fam) == 0 else 0
        return cache[entry]

    def bag_layout(self) -> list[tuple[int, int]]:
        """(slots, used) of the bag in each of the four bag slots; (0, 0)
        for an empty one."""
        d = self.p.u32(self.g.player().base + O.OBJ_DESCRIPTORS)
        guids = struct.unpack("<4Q", self.p.read(d + O.PLAYER_FIELD_BAG_SLOT_1, 32))
        info = {g: (n, len(items)) for g, n, items in self.g.containers()}
        return [info.get(g, (0, 0)) if g else (0, 0) for g in guids]

    def manage_bags(self) -> bool:
        """Put looted (or bought) bags to use: equip them into empty bag
        slots, and once all four are taken, swap a bigger one in for the
        smallest bag we wear. Checked every 15 s out of combat; before this
        a looted bag was only equipped at start-up or after buying bags, and
        with the slots full it was sold. True if the bags changed."""
        if time.time() - getattr(self, "_bag_check", 0) < 15:
            return False
        self._bag_check = time.time()
        spare = sorted(((e, self.bag_size(e)) for e in self.g.bag_items()),
                       key=lambda b: -b[1])
        spare = [b for b in spare if b[1] > 0]
        if not spare:
            return False
        before = self.g.bag_capacity()
        if self.g.free_bag_slots():
            if self.equip_spare_bags():
                log(f"  equipped a bag from the loot: {before} -> {self.g.bag_capacity()} slots")
                return True
            return False
        layout = self.bag_layout()
        k = min(range(4), key=lambda i: layout[i][0])
        old_n, old_used = layout[k]
        entry, n = spare[0]
        if n <= old_n:
            return False
        # the old bag's contents must fit in the rest of the bags
        free_elsewhere = (before - self.g.bag_item_count()) - (old_n - old_used)
        if free_elsewhere < old_used:
            log(f"  a {n}-slot bag could replace the {old_n}-slot one, but its "
                f"{old_used} items do not fit elsewhere yet")
            return False
        log(f"  swapping a {n}-slot bag in for the {old_n}-slot one "
            f"(moving {old_used} items out first)")
        for _ in range(old_used + 4):
            self.bridge.send(br.UPGRADE_BAG, entry * 10 + k, wait=0.8)
            if self.g.bag_capacity() > before:
                break
        self.bridge.send(br.UPGRADE_BAG, entry * 10 + 9, wait=0.8)   # put the old bag away
        after = self.g.bag_capacity()
        log(f"  bags: {before} -> {after} slots"
            + ("" if after > before else " - the swap did not happen"))
        return after > before

    def equip_spare_bags(self) -> bool:
        """Bags lying in the bags go into free bag slots (one per add-on
        call: the pickup needs a frame to settle). Any plain bag counts, not
        just the ones vendors sell: a looted bag is not in BAG_ITEMS."""
        done = False
        for _ in range(4):
            if not self.g.free_bag_slots() or not any(self.bag_size(e) > 0 for e in self.g.bag_items()):
                break
            before = self.g.bag_capacity()
            self.bridge.send(br.EQUIP_BAGS, wait=2.5)
            if self.g.bag_capacity() == before:
                break
            done = True
        return done

    def buy_bags(self, vendor=None):
        """Fill empty bag slots: 16 backpack slots meant a vendor run every
        8 minutes. Buys from the vendor we are at, or the nearest bag seller
        within 400 yd, while money allows."""
        import guide as guides
        free = self.g.free_bag_slots()
        me = self.g.player()
        if not free:
            if self.best_bag(self.spendable(me)):
                # Replacing a smaller equipped bag with a bigger one needs a
                # bag-swap the add-on bridge does not have (EQUIP_BAGS only
                # fills empty slots), so this is a note, not an action.
                log("  all bag slots full - a bigger bag is affordable but "
                    "swapping one out needs a bridge command we do not have")
            return
        bag = self.best_bag(self.spendable(me))
        if bag is None:
            return
        sellers = guides.item_vendors(bag[0])
        unit = None
        if vendor and vendor["entry"] in sellers:
            unit = self.find_npc(vendor)
        else:
            near = self.nearest_service(me, "vendor", where=lambda n: n["entry"] in sellers)
            # three more bags save a vendor run every ten minutes all night:
            # worth a long walk once
            if near and math.dist(near["pos"][:2], (me.x, me.y)) < (1000 if free >= 2 else 400):
                log(f"  bag shopping at {near['name']}, {math.dist(near['pos'][:2], (me.x, me.y)):.0f} yd")
                unit = self.visit_npc(near)
        if unit is None or self.g.player().dist(unit) > 6:
            return
        if self.g.target_guid() != unit.guid or vendor is None or vendor["entry"] not in sellers:
            if not self.interact(unit):
                return
        self.bridge.send(br.OPEN_MERCHANT, wait=1.0)
        self.buy_consumables(self.g.player())
        bought = 0
        for _ in range(free):
            me_now = self.g.player()
            money = me_now.money
            # the purse shrinks with every bag: stop when the reserve no longer
            # covers another one. (The bag itself stays the one we came here
            # for - this vendor stocks that one, not necessarily a cheaper one.)
            if self.spendable(me_now) < bag[1]:
                break
            have = self.g.bag_items().get(bag[0], 0)
            self.bridge.send(br.BUY_ITEM, bag[0] * 100 + 1, wait=2.0)
            now = self.g.bag_items().get(bag[0], 0)
            log(f"  bought bag {bag[0]} ({bag[2]} slots): in bags {have} -> {now}, "
                f"money {money} -> {self.g.player().money}")
            if now <= have:
                log("  the bag did not show up in the bags - not buying more")
                break
            bought += 1
        # close the merchant BEFORE touching the bags: "use" with the
        # window open sells the item straight back (lost 2 satchels that way)
        self.bridge.send(br.CLOSE_ALL, wait=1.0)
        self.equip_spare_bags()
        log(f"  bags: {self.g.bag_capacity()} slots now ({self.g.free_bag_slots()} bag slots free)")

    def town_trip_needed(self, me):
        if me.in_combat or time.time() < self.next_trip:
            return None
        used = self.g.bag_item_count()
        trainer = self.useful_trainer(me)
        if trainer and (not getattr(self, "guide_active", False)
                        or self.training_overdue(me, trainer)):
            return f"train at {trainer['name']}"
        cap = self.g.bag_capacity()
        if used >= cap - 2:
            return f"bags full ({used}/{cap})"
        try:
            low = gear.durability(self.g)[2]
        except Exception:
            low = 100.0
        # 25% was too late to be useful: a death costs 10 points outright,
        # so four deaths in 13 min took the mage from 94% to 5% - and armour
        # at 5% is most of why the fights after the first death went badly.
        # Repair with enough margin to absorb a death without spiralling.
        if low < 40 and me.money >= self.repair_floor(me, low):
            return f"repair (worst item at {low:.0f}%)"
        shop = self.needs_shopping(me)
        if shop:
            return f"restock {shop}"
        mount = self.mount_errand(me)
        if mount:
            return mount
        # the weapon we saved for, once the money is there and a seller is a
        # short walk away - the next ordinary vendor visit may be 30 min off
        # and sell no weapons (Dsa's four visits were to food and supply men)
        wg = self.weapon_goal(me)
        if wg and me.money - wg[1] >= self.reserve(me, exclude=("weapon", "train", "food")):
            near = self.nearest_service(me, "vendor", where=lambda n: n["entry"] in wg[3])
            if near and math.dist(near["pos"][:2], (me.x, me.y)) < self.WEAPON_TRIP_YD:
                return f"weapon: {wg[2]} at {near['name']}"
        # Under-geared: only ever a reason to go to the vendor we were going to
        # anyway, never a reason to travel further than the errands above
        # already justify. It needs gear.staleness/vendor_items to price it; if
        # that is not there yet, there is simply no such reason.
        if (getattr(self.args, "buy_gear", "off") != "off" and hasattr(gear, "staleness")
                and hasattr(gear, "vendor_purchases")):
            try:
                _per, gap = gear.staleness(self.g)      # (per-slot dict, gap)
                if gap >= self.GEAR_GAP_TRIP and self.spendable(me) > 0:
                    # only when a vendor within reach sells something we can
                    # buy NOW: Dsaa made 26 of these trips in 77 min (13:23-
                    # 14:40), one every ~4 min, and bought one pair of bracers -
                    # the gap was real but the training reserve blocked every
                    # purchase and half the trips went to an innkeeper
                    errand = self.gear_errand(me)
                    if errand:
                        return f"under-geared (gap {gap:.2f}): {errand[1]} at {errand[0]['name']}"
            except Exception:
                pass
        return None

    # gear.staleness_of normalises to short/total_w, so the gap is in [0, 1]
    # and reaches exactly 1.0 only for a character wearing literally nothing.
    # 1.0 was therefore a gate nothing could pass. 0.55 is inside the scale the
    # measure actually produces: a level-7 warrior in real starter gear (worn
    # shortsword, wooden shield, recruit's pants and boots, every other slot
    # empty) comes out at 0.94 and trips it, while a character at about half
    # the item level it is entitled to in every slot does not.
    GEAR_GAP_TRIP = 0.55     # staleness units; gear.staleness defines the scale

    # --------------------------------------------------------------- one purse
    # There used to be five independent answers to "how much may we spend?":
    # MONEY_FLOOR 1000 with min(1000, 40*level), food_floor's "0 if have < 3",
    # repair_floor's min(2000, 50*level) with a "low < 20" override, buy_bags'
    # "money < 3500" plus "bag price + 1000", and the two-entry BAGS table.
    # Each was written after its own incident and none knew about the others,
    # so a level-8 warrior could starve its food budget to protect a training
    # reserve for a spell it had already learned, and a level-50 character was
    # still governed by numbers tuned at level 8. One ladder now, and every rung
    # is a price the world DB quotes rather than a constant: what it costs to
    # keep this gear working, what the trainer is owed at this level, what a
    # full larder costs at today's ladder price, and from level 38 the mount.
    REPAIR_SEED_PER_LEVEL = 0.35     # copper per durability point, before we have
                                     # watched a repair: vanilla repair cost tracks
                                     # item level, and item level tracks character
                                     # level. Replaced by measurement at the first
                                     # repair we can see the bill for.

    def repair_copper_per_point(self, level: int) -> float:
        v = self.__dict__.get("_repair_cpp")
        return v if v else max(1.0, self.REPAIR_SEED_PER_LEVEL * level)

    def note_repair(self, points: int, copper: int):
        """One observed repair bill. Durability wear is the one running cost
        the character cannot avoid, so its price is worth measuring rather
        than guessing once at level 8."""
        if points <= 0 or copper <= 0:
            return
        v = copper / points
        cur = self.__dict__.get("_repair_cpp")
        self._repair_cpp = v if not cur else 0.7 * cur + 0.3 * v
        log(f"  repair cost measured: {v:.2f} copper per durability point")

    def class_template(self) -> int | None:
        """This character's class trainer template (any spawn will do: the
        template is per class, not per trainer)."""
        if "_class_tmpl" not in self.__dict__:
            t = None
            try:
                for n in services.all_npcs():
                    tr = n.get("trainer") or {}
                    if tr.get("type") == "class" and tr.get("class") == self.profile.trainer_class:
                        t = int(tr["template"])
                        break
            except Exception:
                pass
            self._class_tmpl = t
        return self._class_tmpl

    # SPELL_AURA_MOUNTED. "A mount" is not an item class (they are class 15
    # Miscellaneous, alongside wrapping paper and quest junk) and not a
    # subclass either; what makes an item a mount is that using it applies
    # this aura. That is the one test that works for every race and both
    # factions without a table of item ids in the code.
    AURA_MOUNTED = 78

    def riding_plans(self) -> list[tuple[int, int]]:
        """(trainer template, cost) for the riding skill, cheapest first.

        creature_template carries one row per content patch and they disagree
        about which template a mount trainer uses: older rows point at the
        per-race templates (109-112, 193-195, 413) at 20 gold, the current one
        at template 440 at 90. Do not try to settle that from the tables -
        take the templates of the trainers the bot would actually WALK to,
        which is what services indexed into data/npcs.json (MAX(patch), the
        same rule gear.items and worlddb.creatures use). The item side agrees
        with that choice: at the same patch a Brown Ram drops from 80 gold to
        10, so the pairs are 20 + 80 and 90 + 10 - a hundred gold either way.
        """
        if "_riding_plans" not in self.__dict__:
            out = []
            try:
                tmpls = sorted({int(n["trainer"]["template"]) for n in services.all_npcs()
                                if (n.get("trainer") or {}).get("type") == "mount"})
                self._riding = set()
                for t in tmpls:
                    plan = spells.trainer_plan(t)
                    due = [x for x in plan if x["level"] <= self.MOUNT_LEVEL]
                    if not due:
                        continue
                    out.append((t, min(x["cost"] for x in due)))
                    self._riding |= {int(x["learns"]) for x in due}
                out.sort(key=lambda p: p[1])
            except Exception as exc:
                log(f"  (riding trainer lookup failed: {exc})")
                self._riding = set()
            self._riding_plans = out
        return self._riding_plans

    def riding_spells(self) -> set[int]:
        self.riding_plans()
        return self.__dict__.get("_riding") or set()

    def mount_price(self) -> int:
        """What a mount costs at the level it becomes available: the most
        common price among the mounts on sale at that level, not the cheapest.
        The cheapest is a seasonal Reindeer Reins at 12 silver and the next a
        PvP-rank Knight-Lieutenant's Steed; the price the character will
        actually pay is the one almost every mount shares."""
        if "_mount_price" not in self.__dict__:
            prices: dict[int, int] = {}
            for _vendor, items in self.mount_stock().items():
                for _e, p in items:
                    prices[p] = prices.get(p, 0) + 1
            self._mount_price = max(prices, key=prices.get) if prices else 0
        return self._mount_price

    def mount_cost(self, me) -> int:
        """Riding skill plus a mount, in copper, or 0 once we own one.

        Ramped from MOUNT_SAVE_FROM to MOUNT_LEVEL rather than switched on:
        a hundred gold appearing in the reserve overnight would stop the
        character buying food, bags or repairs for two whole levels.
        """
        if me.level < self.MOUNT_SAVE_FROM:
            return 0
        if self.mount_item(me):
            return 0                     # the mount is in the bags: nothing left to buy
        # Riding TRAINED is not "bought". buy_mount visits the trainer first and
        # only then walks to a mount vendor, and everything after the trainer can
        # fail (no friendly mount vendor indexed, the visit fails, the bags are
        # full, the purse is short). Treating the trained spell as "done" released
        # the whole reserve and silenced mount_errand, so the character walked at
        # 5 yd/s for the remaining twenty levels. Drop only the half really paid.
        plans = self.riding_plans()
        riding = (min(c for _t, c in plans) if plans else 0)
        if spells.known_spells(self.p) & self.riding_spells():
            riding = 0
        total = riding + self.mount_price()
        if not total:
            return 0
        span = max(1, self.MOUNT_LEVEL - self.MOUNT_SAVE_FROM + 1)
        return int(total * min(1.0, (me.level - self.MOUNT_SAVE_FROM + 1) / span))

    def reserve_parts(self, me) -> dict[str, int]:
        """Copper this character must not spend, by what it is owed to."""
        cached = self.__dict__.get("_reserve")
        if cached and cached[0] == me.level and time.time() - cached[1] < 30:
            return dict(cached[2])
        parts = {"repair": 0, "train": 0, "food": 0, "mount": 0, "weapon": 0}
        try:
            cur, mx, _low = gear.durability(self.g)
            parts["repair"] = int(max(0, mx - cur) * self.repair_copper_per_point(me.level))
        except Exception:
            pass
        t = self.class_template()
        if t:
            try:
                # money=huge: what the trainer is owed at this level, not what
                # we can pay for today - that is the point of a reserve.
                parts["train"] = sum(x["cost"] for x in self.due_at(me, t, money=10 ** 9))
            except Exception:
                pass
        try:
            food, drink = self.food_stock()
            for ladder, have, wanted in ((vendor_ladder("Food"), food,
                                          self.profile.conjure_food is None),
                                         (vendor_ladder("Drink"), drink,
                                          self.profile.drinks and self.profile.conjure_water is None)):
                if not wanted:
                    continue
                _e, price = best_consumable(ladder, me.level)
                parts["food"] += int(price * max(0, -(-(FOOD_STOCK - have) // 5)))
        except Exception:
            pass
        try:
            parts["mount"] = self.mount_cost(me)
        except Exception:
            pass
        try:
            wg = self.weapon_goal(me)
            parts["weapon"] = wg[1] if wg else 0
        except Exception:
            pass
        self._reserve = (me.level, time.time(), dict(parts))
        return parts

    def reserve(self, me, exclude=()) -> int:
        return sum(v for k, v in self.reserve_parts(me).items() if k not in exclude)

    def spendable(self, me, exclude=()) -> int:
        """Money we may actually spend on `exclude`'s errand. A buyer excludes
        its own rung of the ladder, or it could never afford the thing the
        reserve was set aside for."""
        return max(0, me.money - self.reserve(me, exclude))

    def repair_cost(self, me) -> int:
        return self.reserve_parts(me).get("repair", 0)

    def repair_floor(self, me, low):
        """Money we need before repairing. It was a flat 20 silver so a
        repair would not starve training, but a level-7 warrior with 7
        silver then fought on at 0% durability - no armour, no weapon
        damage, three deaths in 15 min. Broken gear costs more than a late
        spell: below 20% repair with whatever we have. Above that the right
        threshold is not min(2000, 50*level) - a guess that stops scaling at
        level 40 - but what this repair will actually cost, which is the
        shortfall times the price per point we have watched vendors charge.
        """
        if low < 20:
            return 1
        return max(1, self.repair_cost(me))

    # ----------------------------------------------------------------- mounts
    # Until now the project had no notion of a mount at all: grep for "mount"
    # across every .py found Blackrock Mountain, Stonetalon Mountains and one
    # unused TRAINER_TYPES entry. At 40 a 60% mount takes effective speed from
    # ~5.3 to ~8.5 yd/s and at 60 a 100% one to ~10.6; with travel measured at
    # 10-34% of wall clock that is the largest piece of missing coverage above
    # level 20. It is also a 100 gold purchase, so it saves through the same
    # reserve as everything else rather than growing a second gold rule.
    # Everything here is hard-gated on level so that below 38 the bot behaves
    # exactly as it did before: none of it can be tried in game until then.
    def mount_item(self, me) -> int | None:
        """A mount in the bags this character may use, or None."""
        if me.level < self.MOUNT_LEVEL:
            return None
        bag = self.g.bag_items()
        if not bag:
            return None
        cache = self.__dict__.setdefault("_mount_items", {})
        key = tuple(sorted(bag))
        if key not in cache:
            hit = None
            try:
                rows = worlddb._query(
                    "SELECT t.entry, t.spellid_1 FROM item_template t "
                    f"JOIN spell_template s ON s.entry = t.spellid_1 AND s.build <= {spells.BUILD} "
                    f"WHERE t.entry IN ({','.join(map(str, key))}) "
                    f"AND t.required_level <= {me.level} "
                    f"AND (t.allowable_race = -1 OR t.allowable_race & {1 << (me.race - 1)}) "
                    f"AND {self.AURA_MOUNTED} IN (s.effectApplyAuraName1, s.effectApplyAuraName2, "
                    "s.effectApplyAuraName3) ORDER BY t.required_level DESC LIMIT 1")
                if rows:
                    hit = (int(rows[0][0]), int(rows[0][1]))
            except Exception:
                hit = None
            cache.clear()
            cache[key] = hit
        hit = cache[key]
        if not hit:
            return None
        self._mount_spell = hit[1]
        return hit[0]

    def mounted(self) -> bool:
        sid = self.__dict__.get("_mount_spell")
        if not sid:
            return False                 # never summoned one: cannot be mounted
        cached = self.__dict__.get("_mounted_at")
        if cached and time.time() - cached[0] < 1.0:
            return cached[1]             # ground_seconds asks this in tight loops
        name = spells.spell_names([sid]).get(sid)
        try:
            up = bool(name) and self.has_aura(self.g.player(), name)
        except Exception:
            up = False
        self._mounted_at = (time.time(), up)
        return up

    def mount_up(self, me, trip_secs: float) -> bool:
        """Summon the mount before a long leg. The mount breaks on damage, so
        there is no upkeep to do: the next leg simply summons again. Never in
        combat, never as a ghost, never with something already in aggro range -
        a summon mid-pull is a wasted global cooldown and a dead character."""
        if me.level < self.MOUNT_LEVEL:
            return False                 # nothing below 40 pays for the unit scan below
        try:
            hostiles = sum(1 for u in self.aggressors() if me.dist(u) < add_radius(me.level))
        except Exception:
            hostiles = 0
        if not should_mount(me.level, self.MOUNT_LEVEL, trip_secs, mounted=self.mounted(),
                            in_combat=me.in_combat, ghost=me.ghost or me.dead,
                            hostiles_near=hostiles):
            return False
        item = self.mount_item(me)
        if not item or time.time() < getattr(self, "_no_mount_until", 0):
            return False
        log(f"  mounting up for a {trip_secs / 60:.1f} min ride")
        self.stop()
        self.bridge.send(br.EQUIP_ITEM, item, wait=2.0)
        if not self.mounted():
            # indoors, moving, or the item was on cooldown: a no-op, never a
            # retry loop - the leg is walked instead and we try again next time
            self._no_mount_until = time.time() + 60.0
            return False
        return True

    def train_order(self) -> list[int]:
        """Spell ids to buy first at the class trainer: the rotation in its
        own order, then emergencies and crowd control."""
        if "_train_order" not in self.__dict__:
            p = self.profile
            names = [a.name for a in [*p.rotation, *p.emergency,
                                      *([p.crowd_control] if p.crowd_control else [])]]
            ids = []
            for n in dict.fromkeys(names):
                try:
                    sid = br.spell_id(n)
                except Exception:
                    sid = None
                if sid:
                    ids.append(sid)
            self._train_order = ids
        return self._train_order

    def mount_errand(self, me) -> str | None:
        """Reason for a town trip to buy the mount, or None. Level 40 exactly:
        below it this returns None for every state the bot can be in."""
        if me.level < self.MOUNT_LEVEL or self.mount_cost(me) <= 0:
            return None
        if me.money < self.mount_cost(me):
            return None
        return "buy a mount"

    def buy_mount(self, me) -> bool:
        """Train riding, then buy the cheapest mount we may ride, using exactly
        the visit machinery a class trainer already uses."""
        if me.level < self.MOUNT_LEVEL:
            return False
        trainer = self.nearest_service(me, "trainer", trainer_type="mount")
        if trainer and not (spells.known_spells(self.p) & self.riding_spells()):
            if self.visit_npc(trainer):
                self.bridge.send(br.TRAIN_ALL, wait=1.5)
                self.bridge.send(br.TRAIN_ALL, wait=1.5)
                self.bridge.send(br.CLOSE_ALL)
                log("  riding: " + ("trained" if spells.known_spells(self.p) & self.riding_spells()
                                    else "the trainer taught nothing"))
        me = self.g.player()
        if not (spells.known_spells(self.p) & self.riding_spells()):
            return False
        stock = self.mount_stock()
        if not stock:
            return False
        near = self.nearest_service(me, "vendor", where=lambda n: n["entry"] in stock)
        if not near or not self.visit_npc(near):
            return False
        me = self.g.player()
        entry, price = min(stock[near["entry"]], key=lambda p: p[1])
        self.bridge.send(br.OPEN_MERCHANT, wait=1.0)
        before = self.g.bag_items().get(entry, 0)
        if me.money >= price:
            self.bridge.send(br.BUY_ITEM, entry * 100 + 1, wait=3.0)
        else:
            log(f"  mount costs {price} and we have {me.money} - coming back later")
        self.bridge.send(br.CLOSE_ALL)
        got = self.g.bag_items().get(entry, 0) > before
        log(f"  mount {worlddb.name(entry)}: {'bought' if got else 'not bought'}, "
            f"money now {self.g.player().money}")
        self.__dict__.pop("_mount_items", None)
        return got

    def mount_stock(self) -> dict[int, list[tuple[int, int]]]:
        """vendor entry -> [(item, price)] for every vendor selling a mount of
        the tier that unlocks at MOUNT_LEVEL. Which mounts a character may buy
        is a reputation and race question the tables answer badly (the race
        mask on a Pinto Bridle lets a dwarf buy a horse it cannot ride), so do
        not decide it here: go to the nearest mount vendor our faction is
        friendly with and buy what THAT vendor stocks. A dwarf's nearest
        friendly mount vendor sells rams."""
        if "_mount_stock" not in self.__dict__:
            stock: dict[int, list[tuple[int, int]]] = {}
            try:
                # item_template carries one row per content patch and they
                # disagree on price (a ram is 100000 in one and 800000 in
                # another), so take the newest row per entry the way
                # gear.items does, or the "most common price" below is the
                # most common PATCH.
                rows = worlddb._query(
                    "SELECT v.entry, t.entry, t.buy_price FROM npc_vendor v "
                    "JOIN item_template t ON t.entry = v.item "
                    "JOIN (SELECT entry, MAX(patch) p FROM item_template GROUP BY entry) mx "
                    "ON mx.entry = t.entry AND mx.p = t.patch "
                    f"JOIN spell_template s ON s.entry = t.spellid_1 AND s.build <= {spells.BUILD} "
                    f"WHERE t.required_level = {self.MOUNT_LEVEL} AND t.buy_price > 0 "
                    f"AND {self.AURA_MOUNTED} IN (s.effectApplyAuraName1, s.effectApplyAuraName2, "
                    "s.effectApplyAuraName3) GROUP BY v.entry, t.entry, t.buy_price")
                for ve, ie, pr in rows:
                    stock.setdefault(int(ve), []).append((int(ie), int(pr)))
            except Exception as exc:
                log(f"  (mount stock lookup failed: {exc})")
            self._mount_stock = stock
        return self._mount_stock

    def food_vendor(self, me, drink=False, quiet=False):
        """The nearest NPC that actually sells our food (or drink): "the
        nearest vendor" was a fisherman's supplier, then a weaponsmith -
        two trips that bought nothing, and the warrior rested on regen."""
        try:
            import guide as guides
            ladder = vendor_ladder("Drink" if drink else "Food")
            # walk to a seller of what buy_consumables will actually buy: a
            # broke warrior buys the cheap bread, not the 125c tier
            entry, _ = affordable_consumable(ladder, me.level, me.money)
            if entry is None:
                entry, _ = best_consumable(ladder, me.level)
            if entry is None:
                return None
            # one query for every seller's spawns: spawn_of() per vendor was
            # 55 bread sellers x 2-3 queries = 15 s standing still before
            # every restock trip (16:56)
            ids = sorted(guides.item_vendors(entry))
            if not ids:
                return None
            rows = worlddb._query(
                "SELECT id, position_x, position_y, position_z FROM creature "
                f"WHERE map={self.continent()} AND id IN ({','.join(map(str, ids))})")
            if not rows:
                return None
            # Honour the "could not reach them" blacklist the way
            # nearest_service does. Without this the nearest seller is picked
            # on distance alone, so a vendor we have just failed to reach is
            # chosen again the moment the next restock is due: Greishan
            # Ironstove was walked to four times in sixteen minutes, ~830 yd
            # and three minutes each, blacklisted every time. Fall back to the
            # unfiltered list if every seller is blacklisted - a far vendor we
            # once failed to reach still beats having no food at all.
            bad = getattr(self, "npc_bad", {})
            ok_rows = [r for r in rows
                       if bad.get((int(r[0]), round(float(r[1])), round(float(r[2]))), 0)
                       <= time.time()]
            best = min(ok_rows or rows,
                       key=lambda r: math.dist((float(r[1]), float(r[2])), (me.x, me.y)))
            e = int(best[0])
            v = {"entry": e, "name": worlddb.name(e), "title": "",
                 "pos": [float(best[1]), float(best[2]), float(best[3])],
                 "kind": "creature", "map": self.continent()}
            if not quiet:
                log(f"  {'drink' if drink else 'food'} is sold by {v['name']}, "
                    f"{math.dist(v['pos'][:2], (me.x, me.y)):.0f} yd")
            return dict(v, title="Vendor")
        except Exception as e:
            log(f"  (finding a food vendor failed: {e})")
            return None

    # A vendor weapon worth saving for: the score it must beat the worn one by.
    WEAPON_GOAL_GAIN = 1.4
    WEAPON_TRIP_YD = 900.0

    def weapon_goal(self, me):
        """(entry, price, name, seller entries) of the vendor weapon worth
        saving for, or None.

        Dsa levelled 1-9 with the Anvilmar Sledge (2.6 dps) while the Wooden
        Mallet (5.0 dps, 7s) sat on the shelf of a vendor it visited four
        times. It never had 7s: every visit spent 5s of 6-7s on twenty
        loaves, and fighting was 43% of the three hours. The weapon is the
        one purchase that shortens every fight, so the best one a vendor
        sells for our level (gain per copper, at least WEAPON_GOAL_GAIN times
        the worn score) is a rung of the reserve until it is bought.
        Cached per level and worn weapon."""
        if getattr(self.args, "buy_gear", "off") == "off":
            return None
        try:
            worn = gear.worn_and_bag(self.g)[0]
        except Exception:
            return None
        mh = worn[gear.MAIN_HAND] if gear.MAIN_HAND < len(worn) else 0
        key = (me.level, mh)
        cached = self.__dict__.get("_wgoal")
        if cached and cached[0] == key:
            return cached[1]
        goal = None
        try:
            rows = worlddb._query(
                "SELECT DISTINCT v.entry, v.item FROM npc_vendor v "
                "JOIN item_template t ON t.entry = v.item AND t.class = 2 "
                f"WHERE t.required_level <= {int(me.level)} AND t.buy_price > 0")
            sellers: dict[int, set] = {}
            for ve, it in rows:
                sellers.setdefault(int(it), set()).add(int(ve))
            info = gear.items(list(sellers))
            known = gear.known_skills(self.g)
            ws = gear.worn_scores(worn, me.class_id, me.level)
            best = None
            for e, it in info.items():
                price = it.get("buy_price", 0)
                if price <= 0 or not gear.can_use(it, me.class_id, me.level, known):
                    continue
                slots, old = gear.slots_for(it, worn, ws, known)
                if not slots or gear.MAIN_HAND not in slots:
                    continue
                new = gear.score(it, me.class_id, me.level)
                if new < self.WEAPON_GOAL_GAIN * max(old, 0.1):
                    continue
                rate = (new - old) / price
                if best is None or rate > best[0]:
                    best = (rate, e, price, it["name"], sellers[e], old, new)
            if best:
                goal = best[1:5]
                log(f"  weapon goal: {best[3]} for {best[2]}c ({best[5]:.1f} -> {best[6]:.1f})")
        except Exception as exc:
            log(f"  (weapon goal lookup failed: {exc})")
        self._wgoal = (key, goal)
        return goal

    GEAR_TRIP_YD = 600

    def gear_picks(self, me, vendor_entry, gap=None):
        """What buy_gear_here would buy from this vendor right now: the same
        budget, share and reserve rules, so a trip is only planned for a
        purchase that will actually happen."""
        if self.reserve_parts(me).get("train", 0) > 0:
            return []
        budget = self.spendable(me)
        if budget <= 0:
            return []
        if gap is None:
            gap = gear.staleness(self.g)[1]
        share = min(1.0, 0.25 + 0.25 * max(0.0, gap))
        stock = self.__dict__.setdefault("_vstock", {})
        if vendor_entry not in stock:           # world DB: never changes in a run
            stock[vendor_entry] = gear.vendor_items([vendor_entry])
        if not stock[vendor_entry]:
            return []
        picks = gear.vendor_purchases(stock[vendor_entry], me.class_id, me.level,
                                      gear.known_skills(self.g),
                                      gear.worn_and_bag(self.g)[0], int(budget * share))
        return [p for p in picks or () if me.money - p[1] >= self.reserve(me)]

    def gear_errand(self, me):
        """(vendor, what) for the nearest vendor within GEAR_TRIP_YD that sells
        an upgrade we can afford now, or None. Cached per level, 50c of money
        and worn set, so it costs nothing on the ticks in between."""
        # one dedicated gear walk per level: Dsaa walked 230 yd for pants at
        # 15:06, then again at 15:13 for boots worth +0.3 - every other
        # vendor visit (bags, food, training) still buys gear on the spot
        if self.__dict__.get("_gear_trip_level") == me.level:
            return None
        try:
            worn = tuple(gear.worn_and_bag(self.g)[0])
            key = (me.level, worn)
            cached = self.__dict__.get("_gerrand")
            # the money changes with every loot: re-price at most once a
            # minute (town_trip_needed runs every tick and was already
            # logging 3.4 s SLOW CHECKs)
            if cached and cached[0] == key and time.time() - cached[2] < 60:
                return cached[1]
            gap = gear.staleness(self.g)[1]
            out = None
            for v in self.nearest_services(me, "vendor"):
                if math.dist(v["pos"][:2], (me.x, me.y)) > self.GEAR_TRIP_YD:
                    break
                picks = self.gear_picks(me, v["entry"], gap)
                if picks:
                    out = (v, picks[0][3])
                    break
        except Exception as exc:
            log(f"  (gear errand lookup failed: {exc})")
            out = None
        self._gerrand = (key, out, time.time())
        return out

    def buy_gear_here(self, vendor) -> int:
        """At a merchant window that is already open, spend what is free on
        the worst-equipped slots. Copper spent.

        The kill log is the argument: 2% health and 12 s per kill at level 3
        against 36% and 23.6 s at level 7-10, which is what "fighting is a
        third of the wall clock and resting a fifth" is made of. Quest rewards
        alone do not keep up, and the window is already open here, so the
        travel cost of this is zero.

        It spends the character's money live, so it is bounded three ways:
        off by default (--buy-gear), a dry run that only logs, and a hard
        invariant that money may never fall below the reserve.
        """
        mode = getattr(self.args, "buy_gear", "off")
        if mode == "off" or not vendor:
            return 0
        if not (hasattr(gear, "vendor_items") and hasattr(gear, "vendor_purchases")):
            return 0                    # the pricing half of this lives in gear.py
        me = self.g.player()
        try:
            low = gear.durability(self.g)[2]
        except Exception:
            low = 100.0
        if low < 40:
            log("  not buying gear: repairs come first")
            return 0
        spent = 0
        # the weapon we saved for, ahead of training: the trainer's reserve is
        # every spell owed at this level, which is nearly always > 0, and a
        # doubled weapon shortens every fight where a spell rank changes few
        goal = self.weapon_goal(me)
        if goal and vendor.get("entry") in goal[3]:
            entry, price, name, _s = goal
            if me.money - price >= self.reserve(me, exclude=("weapon", "train", "food")):
                if mode == "dry":
                    log(f"  [dry run] would buy the weapon goal {name} for {price}")
                else:
                    before = self.g.bag_items().get(entry, 0)
                    self.bridge.send(br.BUY_ITEM, entry * 100 + 1, wait=0.3)
                    t0 = time.time()
                    while time.time() - t0 < 2.0 and self.g.bag_items().get(entry, 0) <= before:
                        time.sleep(0.15)
                    if self.g.bag_items().get(entry, 0) > before:
                        spent += price
                        log(f"  bought the weapon goal {name} for {price}")
                        self.__dict__.pop("_reserve", None)
                        self.__dict__.pop("_wgoal", None)
                    else:
                        log(f"  weapon goal {name}: the item did not arrive")
            else:
                log(f"  saving for {name} ({price}c): have {me.money}c")
        if self.reserve_parts(me).get("train", 0) > 0:
            return spent
        me = self.g.player()
        budget = self.spendable(me)
        if budget <= 0:
            return spent
        try:
            # gear.py's signatures are the contract; read them there, not here.
            # vendor_items takes an ITERABLE of vendor entries, staleness
            # returns a (per-slot dict, gap) tuple, and vendor_purchases takes
            # (offers, class_id, level, known, worn, budget) and yields
            # 4-tuples (entry, price, gain, why).
            stock = gear.vendor_items([vendor["entry"]])
            _per, gap = gear.staleness(self.g)
            # an under-geared character spends a larger share of what is free
            share = min(1.0, 0.25 + 0.25 * max(0.0, gap))
            picks = gear.vendor_purchases(stock, me.class_id, me.level,
                                          gear.known_skills(self.g),
                                          gear.worn_and_bag(self.g)[0],
                                          int(budget * share))
        except Exception as exc:
            log(f"  (vendor gear pricing failed: {exc})")
            return spent
        for entry, price, _gain, why in picks or ():
            me = self.g.player()
            if me.money - price < self.reserve(me):
                log(f"  stopping gear purchases: {price} would break the reserve")
                break
            if mode == "dry":
                log(f"  [dry run] would buy {why} for {price}")
                continue
            before = self.g.bag_items().get(entry, 0)
            self.bridge.send(br.BUY_ITEM, entry * 100 + 1, wait=2.0)
            if self.g.bag_items().get(entry, 0) <= before:
                log(f"  {why}: the item did not arrive - stopping")
                break
            spent += price
            log(f"  bought {why} for {price}")
        # gear_check's equip loop puts whatever is an upgrade on the character
        return spent

    def town_trip(self, reason: str):
        self.last_trip_done = time.time()      # (re)set at the start too: a trip in progress
        log(f"town trip: {reason}")
        set_state("town trip", detail=reason)
        me = self.g.player()
        money0 = me.money
        # every piece was broken while the junk vendors (a quartermaster, a
        # trade goods seller) could not repair: below 60% go to a repairer
        try:
            low = gear.durability(self.g)[2]
        except Exception:
            low = 100.0
        vendor = self.nearest_service(me, "repair") if low < 60 else None
        if vendor:
            log(f"  worst item at {low:.0f}% durability - using a repairer")
        elif reason.startswith("restock"):
            vendor = self.food_vendor(me, drink="drink" in reason)
        elif reason.startswith("weapon"):
            wg = self.weapon_goal(me)
            if wg:
                vendor = self.nearest_service(me, "vendor", where=lambda n: n["entry"] in wg[3])
        elif reason.startswith("under-geared"):
            errand = self.gear_errand(me)
            if errand:
                vendor = errand[0]
        if not vendor:
            vendor = self.nearest_service(me, "vendor")
        if reason.startswith("train") and self.g.bag_item_count() < 0.7 * self.g.bag_capacity():
            vendor = None                       # no 350 yd detour to a butcher for nothing
        unit = self.visit_npc(vendor) if vendor else None
        if unit is None and self.g.player().in_combat:
            log("  attacked on the way - fighting first, trip continues later")
            self.next_trip = 0
            return
        if unit:
            if reason.startswith("under-geared"):
                self._gear_trip_level = me.level    # reached: that was this level's gear walk
            self.bridge.send(br.OPEN_MERCHANT, wait=1.0)   # gossip vendors (Farmer Saldean)
            before = self.g.bag_item_count()
            # Two items per command, then a pause: selling a whole bag in one
            # frame trips the server's anti-flood (CMSG_SELL_ITEM) and kicks us.
            t_vend = time.time()
            # a visit is ~10-20 s of selling with nothing else to say: say it,
            # or the hang watchdog reads a vendor visit as a freeze
            log(f"  selling at {vendor.get('name', 'the vendor')}: {before} items in the bags")
            for _ in range(10):
                n0 = self.g.bag_item_count()
                self.bridge.send(br.SELL_GREYS)
                # at least 1 s between commands (the anti-flood), but not a
                # flat 2 s: the round that finds nothing left to sell cost
                # the full wait every visit
                t_sell = time.time()
                while time.time() - t_sell < 1.2 and self.g.bag_item_count() >= n0:
                    time.sleep(0.1)
                if self.g.bag_item_count() >= n0:
                    break
                time.sleep(max(0.0, 1.0 - (time.time() - t_sell)))
            t_greys = time.time()
            # one stack per 0.5 s: the greys' own rate (2 per ~1 s), which the
            # server's anti-flood accepts - a flat 1.2 s per stack was 10 s
            # of the 22 s a visit took (Hegnar Rumbleshot, 08:45)
            misses = 0
            for item in self.sell_list():
                n0 = self.g.bag_item_count()
                self.bridge.send(br.SELL_ITEM, item, wait=0.1)
                t_s = time.time()
                while time.time() - t_s < 1.0 and self.g.bag_item_count() >= n0:
                    time.sleep(0.05)
                if self.g.bag_item_count() >= n0:
                    misses += 1
                    if misses >= 2:
                        # the window is not a shop (Milli Featherwhistle sold
                        # nothing, 15 items x 1 s tried at 13:13) - stop here
                        break
                    continue
                misses = 0
                time.sleep(max(0.0, 0.5 - (time.time() - t_s)))
            t_sold = time.time()
            # measured here, before repairs and shopping spend it again: the
            # old line read "+-325 copper" for a run of sales
            sold_for = self.g.player().money - money0
            sold_n = before - self.g.bag_item_count()
            if sold_n <= 0 and before > 0 and self.sell_list():
                # sold nothing with junk in the bags: this vendor does not buy
                # from us through the bot (a gossip-only merchant). Use
                # another one, and go now - the bags are still full.
                key = (vendor["entry"], round(vendor["pos"][0]), round(vendor["pos"][1]))
                self.__dict__.setdefault("npc_bad", {})[key] = time.time() + 1800
                try:
                    self.save_npc_bans()
                except Exception:
                    pass
                log(f"  {vendor.get('name')} bought nothing - trying another vendor")
                self.next_trip = 0
            # repairs before level ~10 cost more than the few copper we own
            # and starve training (a repair once left 38 copper for spells)
            if ("repair" in vendor.get("services", ())
                    and self.g.player().money >= self.repair_floor(self.g.player(), low)):
                try:
                    dur0, money_pre = gear.durability(self.g)[:2], self.g.player().money
                except Exception:
                    dur0, money_pre = None, None
                self.bridge.send(br.REPAIR, wait=1.0)
                try:
                    dur1 = gear.durability(self.g)
                    log(f"  repaired: worst item {low:.0f}% -> {dur1[2]:.0f}%, "
                        f"{money0 - self.g.player().money} copper spent so far")
                    if dur0 is not None:
                        # the one running cost the character cannot avoid:
                        # price it from the bill rather than guessing once
                        self.note_repair(dur1[0] - dur0[0],
                                         money_pre - self.g.player().money)
                except Exception:
                    pass
            # stock up while the window is open: selling greys first means we
            # are buying with the money we just made
            t_buy = time.time()
            got = self.buy_consumables(self.g.player())
            t_gear = time.time()
            self.buy_gear_here(vendor)
            self.bridge.send(br.CLOSE_ALL)
            log(f"  sold {sold_n} stack(s) of junk: +{sold_for} copper "
                f"(greys {t_greys - t_vend:.1f}s, other {t_sold - t_greys:.1f}s, repair "
                f"{t_buy - t_sold:.1f}s, food {t_gear - t_buy:.1f}s, gear {time.time() - t_gear:.1f}s)")
            self.buy_bags(vendor)
            if reason.startswith("restock") and not got:
                # the repairer came first (a blacksmith, a leather merchant)
                # and sells no food: three "restock" trips in a row bought
                # nothing and the warrior rested on natural regen. Go on to
                # a vendor that has it.
                fv = self.food_vendor(self.g.player(), drink="drink" in reason)
                if fv and fv.get("name") != vendor.get("name"):
                    if self.visit_npc(fv):
                        self.bridge.send(br.OPEN_MERCHANT, wait=1.0)
                        self.buy_consumables(self.g.player())
                        self.bridge.send(br.CLOSE_ALL)
        me = self.g.player()
        # in town anyway: pick up its flight point. Only boat rides did this,
        # so Thelsamar was never learned and every Ironforge trip was walked.
        try:
            # 250 yd was not enough: Thelsamar's flight master stands ~600 yd
            # from its vendor, so the point was never picked up. One short
            # detour buys every later trip out of this zone.
            self.learn_nearby_flight(700.0)
        except Exception as exc:
            log(f"  could not check the flight master: {exc}")
        me = self.g.player()
        trainer = self.useful_trainer(me)
        if trainer:
            tmpl = trainer["trainer"]["template"]
            before = spells.known_spells(self.p)
            try:
                skills0 = gear.known_skills(self.g)
            except Exception:
                skills0 = set()
            due0, money0_train = self.due_at(me, tmpl), me.money
            for attempt in range(3):
                if not self.visit_npc(trainer):
                    if self.g.player().in_combat:
                        log("  attacked on the way - fighting first, trip continues later")
                        self.next_trip = 0
                        return
                    key = (trainer["entry"], round(trainer["pos"][0]), round(trainer["pos"][1]))
                    if getattr(self, "npc_bad", {}).get(key, 0) > time.time():
                        break            # unreachable: three 3-minute walks to Bink were the same walk
                    continue
                # first press opens the trainer page from gossip, then the
                # rotation's spells in rotation order (what we swing with
                # beats a buff when the money is short), then the rest
                self.bridge.send(br.TRAIN_ALL if not self.train_order() else br.TRAIN_SPELL,
                                 self.train_order()[0] if self.train_order() else 0, wait=1.5)
                for sid in self.train_order():
                    self.bridge.send(br.TRAIN_SPELL, sid, wait=0.4)
                # the rest (Purify, a profession-style extra) only once the
                # weapon we are saving for is bought: 600c of level-8 spells
                # would push a doubled weapon back by a level
                wg = self.weapon_goal(self.g.player())
                if not wg or self.g.player().money >= wg[1] + 600:
                    self.bridge.send(br.TRAIN_ALL, wait=1.5)
                else:
                    log(f"  leaving the other spells until {wg[2]} is bought")
                self.bridge.send(br.CLOSE_ALL)
                if spells.known_spells(self.p) - before:
                    break
                log(f"  nothing learned on attempt {attempt + 1}")
            new = spells.spell_names(spells.known_spells(self.p) - before)
            try:
                skills1 = gear.known_skills(self.g)
            except Exception:
                skills1 = skills0
            if skills1 - skills0:
                log(f"  new skill lines: {sorted(skills1 - skills0)}")
            if new or (skills1 - skills0):
                if new:
                    log("  learned: " + ", ".join(new.values()))
                    self.setup_action_bar()
            else:
                log("  training failed - not retrying for 10 minutes")
                self.next_trip = time.time() + 600
            # Whatever was due, was affordable when we arrived, and is still
            # not on the character after a real visit, this trainer will never
            # teach: write it down. Without this the check stays true for ever
            # and every town trip from level 20 on becomes a trainer walk that
            # teaches nothing (see trained_ledger).
            still = self.due_at(self.g.player(), tmpl, money=10 ** 9)
            stuck = [x for x in due0 if any(y["learns"] == x["learns"] for y in still)]
            if stuck:
                # Judge by the money LEFT, not the money we walked in with: a
                # visit that runs out of copper buys the first spells and
                # leaves the rest - those were never refused. Judging by the
                # arrival purse banned Seal of the Crusader and Divine
                # Protection at Kharanos for a level-6 paladin with 119 copper,
                # and five 600-1000 yd walks to the Coldridge trainer followed.
                self.note_unlearnable(tmpl, stuck, me.level, self.g.player().money)
        if self.mount_errand(self.g.player()):
            self.buy_mount(self.g.player())
        self.next_trip = max(self.next_trip, time.time() + 180)
        if getattr(self, "guide_active", False):
            return                   # the guide says where to go next, often right here in town
        log("  heading back to the hunting ground")
        self.navigate(*self.home, stop_short=5.0, timeout=600, careful=True)

    def install_lua_helpers(self):
        """Close stray windows / the game menu / error popups (via the bridge)."""
        self.bridge.send(br.CLOSE_ALL)

    def fix_action_bar(self, me) -> bool:
        """Re-place profile spells that are known but not on the bar.

        Placing only once at startup was not enough twice over. A brand-new
        character has no saved key bindings, so the add-on sets them when it
        loads - and the bot fired its placement two seconds after entering
        the world, before F1-F12 existed, leaving Dvaerg swinging with an
        empty bar. It also meant every spell learned later (Rend at 4,
        Thunder Clap at 6, Battle Shout at 12) would never reach the bar at
        all. Check now and then instead.
        """
        if me.in_combat or time.time() - getattr(self, "_bar_check", 0) < 60:
            return False
        self._bar_check = time.time()
        known = spells.known_by_name(self.p)
        missing = [n for n in self.profile.bar() if n in known and not self.has(n)]
        if not missing:
            return False
        log(f"action bar missing {', '.join(missing)} - placing")
        self.setup_action_bar()
        return True

    # A warrior in a stance (and a druid in form, a rogue in stealth) has
    # the visible main bar swapped for the bonus bar: action ids 73-84
    # instead of 1-12. UseAction ignores that and always means 1-12, so the
    # bot's rotation fires correctly while the player sees Stoneform and
    # Find Treasure and gets them from the 3 and 4 keys. Mirror the abilities
    # onto the bonus bar so the bar on screen is the one the bot is using.
    BONUS_BAR_OFFSET = 72

    def setup_action_bar(self):
        """Put the highest rank of every combat spell on its fixed key."""
        known = spells.known_by_name(self.p)
        cur = spells.action_bar(self.p)
        bonus = any(v for v in cur[self.BONUS_BAR_OFFSET:self.BONUS_BAR_OFFSET + 12])
        for name, slot in self.profile.bar().items():
            sid = br.spell_id(name) if name in known else None
            if not sid:
                continue
            # already there (same rank): each placement is ~10 key presses,
            # and re-placing the whole bar after every training was 6 s of
            # standing at the trainer (Azar Stronghammer, 08:47)
            top = max(known[name]) if isinstance(known.get(name), (set, frozenset)) and known[name] else None
            if top and cur[slot - 1] == top and (
                    not bonus or cur[slot - 1 + self.BONUS_BAR_OFFSET] == top):
                continue
            self.bridge.send(br.PLACE_SPELL, sid * 100 + slot, wait=0.1)
            if bonus:
                self.bridge.send(br.PLACE_SPELL,
                                 sid * 100 + slot + self.BONUS_BAR_OFFSET)
        time.sleep(0.5)
        bar = spells.action_bar(self.p)
        names = spells.spell_names([v for v in bar[:10] if v and not v & 0x80000000])
        log("  action bar: " + ", ".join(f"{spells.KEY_FOR_SLOT[i + 1]}={names.get(v, v)}"
                                         for i, v in enumerate(bar[:10]) if v))

    def straight_clear(self, me, p) -> bool:
        """No known doodad (scarecrow, outhouse, fence) or remembered wedge
        spot on the straight line from here to p."""
        try:
            import navmesh
            boxes = list(navmesh._doodad_boxes(me.x, me.y, p[0], p[1], min(me.z, p[2])))
            boxes += [(o[0] - o[3] - 0.7, o[1] - o[3] - 0.7, o[0] + o[3] + 0.7, o[1] + o[3] + 0.7)
                      for o in self.obstacles if abs(o[2] - me.z) < 3
                      and math.dist(o[:2], (me.x, me.y)) < math.dist(p[:2], (me.x, me.y)) + 5]
            return not any(navmesh._seg_hits_rect((me.x, me.y), p[:2], r) for r in boxes)
        except Exception:
            return True

    def step_off_corpse(self, c) -> None:
        """Back away to ~4.8 yd so a loot order has an approach to make.

        In a mine tunnel the spot straight behind us is often a wall - the
        order then went out from 1.2 yd and did nothing - so try around the
        corpse for a spot that is actually on the mesh before settling for
        a plain step back.
        """
        if self.g.player().dist(c) >= 3.8:
            return
        me = self.g.player()
        base = math.atan2(me.y - c.y, me.x - c.x)
        for checked in (True, False):
            for da in (0.0, 0.8, -0.8, 1.6, -1.6, 2.4, -2.4, 3.1):
                a = base + da
                x, y, z = c.x + 4.8 * math.cos(a), c.y + 4.8 * math.sin(a), me.z
                if checked and self.nav:
                    node, _ = self.nav.nearest_poly(x, y, z, max_dist=1.5)
                    if node is None:
                        continue
                self.walk_to(x, y, z, timeout=4)
                if self.g.player().dist(c) >= 3.8:
                    break
            if self.g.player().dist(c) >= 3.8:
                break
        self.wait_until_still(1.0)

    def corpse_at_feet(self, me, reach=8.0) -> bool:
        return any(u.dead and u.lootable and me.dist(u) < reach for u in self.g.units())

    def loot_nearby(self, me) -> bool:
        if self.g.player().dead:
            return False           # 95 s spent "looting" as a corpse (09:26)
        if self.attackers(me):
            return False           # a golem hit us while resting: fight, loot later
        # Corpses have their own blacklist. The target blacklist ("do not
        # attack this for a while") used to veto the loot as well, so a mob
        # we once backed off from and then killed was never looted:
        # Grik'nir the Cold died with The Stolen Journal on him and the
        # corpse was "skipped: blacklisted 64s more" - five minutes of the
        # quest wasted and the step given up.
        lbl = self.__dict__.setdefault("loot_bl", {})
        corpses = [u for u in self.g.units()
                   if u.dead and u.lootable and me.dist(u) < 40
                   and lbl.get(u.guid, 0) < time.time()]
        if not corpses:
            # diagnostics: 66 of 71 goretusk kills went unlooted (no livers,
            # no quest progress) - say why the nearest corpse was skipped
            seen = self.__dict__.setdefault("_loot_diag", {})
            dead = [u for u in self.g.units() if u.dead and me.dist(u) < 40]
            for u in dead:
                if u.guid in seen:
                    continue
                seen[u.guid] = time.time()
                bl = lbl.get(u.guid, 0) - time.time()
                log(f"  corpse {worlddb.name(u.entry)} at {me.dist(u):.1f} yd skipped: "
                    f"lootable={u.lootable} dynflags=0x{u.dyn_flags:x}"
                    + (f" blacklisted {bl:.0f}s more" if bl > 0 else ""))
            if len(seen) > 200:
                for k in list(seen)[:100]:
                    del seen[k]
            return False
        # A corpse out of reach of a live hostile only: walking to one in its
        # aggro circle is a pull we did not choose, taken wherever our hp
        # happens to be. Ugibd walked 36 yd at 56% hp to loot a Dark Iron Spy
        # beside a level-10 Rockjaw Bonesnapper, got both, and died (02:03).
        # Mobs below our level only count when we are hurt.
        if len(corpses) > 0:
            aggro = [u for u in self.aggressors() if not u.dead and u.target != me.guid
                     and (u.level >= me.level or me.hp_pct < 70)]
            safe = []
            for u in corpses:
                if me.dist(u) > 8 and any(
                        math.dist((u.x, u.y), (a.x, a.y)) < self.aggro_radius(me, a) + 3
                        for a in aggro):
                    if lbl.get(u.guid, 0) < time.time():
                        log(f"  corpse {worlddb.name(u.entry)} at {me.dist(u):.0f} yd lies in "
                            f"a live mob's aggro range - leaving it")
                    lbl[u.guid] = time.time() + 30
                    continue
                safe.append(u)
            corpses = safe
            if not corpses:
                return False
        c = min(corpses, key=me.dist)
        log(f"looting {worlddb.name(c.entry)} at {me.dist(c):.1f} yd")
        set_state("looting")
        # The server cancels a loot session on any movement packet that still
        # carries a moving flag (MovementHandler.cpp:1106), and CTM-loot does
        # nothing when issued from on top of the corpse. So: walk to ~3 yd and
        # let CTM arrive on its own, then CTM-loot the last steps.
        # Like NPC talk, the loot order only fires while approaching: from a
        # standstill next to the corpse it does nothing (Garrick's head was
        # skipped twice from 2.4 yd). Always start from ~4.5 yd out.
        if me.dist(c) > 6:
            # corpses lie where we just fought: straight there, the navmesh
            # detours between two corpses looked like running in circles
            if me.dist(c) < 18 and self.straight_clear(me, (c.x, c.y, c.z)):
                self.walk_to(c.x, c.y, c.z, stop_short=4.5, timeout=12)
            else:
                self.navigate(c.x, c.y, c.z, stop_short=4.5, timeout=30)
        # still rolling from the fight's last step: the order went out from
        # 1.9 yd although we measured 4.5 a second earlier
        if self.ctm.moving or self.g.player().moving:
            self.stop()
        self.wait_until_still(1.5)
        time.sleep(0.2)
        me = self.g.player()
        money_before = me.money
        # Arm the client's own auto-loot (same flag as shift-clicking): when
        # the loot window opens it takes every item and the coins itself.
        self.p.write(me.base + O.PLAYER_AUTOLOOT, b"\x01")
        # A loot order issued while the previous loot window is still open is
        # dropped by the client at once (idle after 0.1 s, no window): wait for
        # it to close, and give the order a second chance.
        loot_guid_addr = self.g.player().base + O.PLAYER_LOOT_GUID
        for attempt in range(2):
            t = time.time()
            while self.p.u64(loot_guid_addr) and time.time() - t < 2:
                time.sleep(0.1)
            if self.p.u64(loot_guid_addr):
                self.bridge.send(br.CLOSE_ALL)
            if attempt:
                # Second go: the loot order only fires while approaching, so
                # give it something to approach. This used to run before
                # EVERY loot, and a melee class finishes every fight standing
                # on the corpse - so the warrior backed off and circled the
                # body once per kill. Try it from where we stand first; the
                # dance is only worth it when that failed.
                self.step_off_corpse(c)
            d_order = self.g.player().dist(c)
            self.ctm.loot(c.guid, c.x, c.y, c.z)
            t0 = time.time()
            after = c
            acts, saw_window = [], False
            while time.time() - t0 < 4:
                time.sleep(0.1)
                a = self.p.u32(CTM_ACTION)
                if not acts or acts[-1][1] != a:
                    acts.append((round(time.time() - t0, 1), a, round(self.g.player().dist(c), 1)))
                saw_window = saw_window or bool(self.p.u64(loot_guid_addr))
                after = self.g.unit(c.guid)
                if after is None or not after.lootable:
                    break
            if after is None or not after.lootable or saw_window:
                break
            if attempt == 0 and self.g.player().dist(c) < 5.5:
                # within reach: a right-click opens it now; the second order
                # and its step-off dance cost ~6 s and failed as often
                # (12:07, 16 s on one whelp)
                break
            time.sleep(0.8)
        if after is not None and after.lootable and not saw_window:
            # Both orders went idle without a window (09:02: the order ended
            # 2.6 yd short; 7 of 115 loots failed this way, 10-15 s each).
            # A real right-click on the corpse opens it from anywhere within
            # reach, now that aim_camera puts it on screen.
            try:
                u = self.g.unit(c.guid) or c
                if self.g.player().dist(u) < 5.5:
                    self.aim_camera(u.x, u.y)
                    if self.click_unit(u, right=True):
                        t_c = time.time()
                        while time.time() - t_c < 2.5:
                            time.sleep(0.1)
                            saw_window = saw_window or bool(self.p.u64(loot_guid_addr))
                            after = self.g.unit(c.guid)
                            if after is None or not after.lootable:
                                break
                        if after is None or not after.lootable:
                            log("  looted it with a click after the loot order failed")
            except Exception as exc:
                log(f"  loot click failed: {exc}")
        self.p.write(self.g.player().base + O.PLAYER_AUTOLOOT, b"\x00")
        got_money = self.g.player().money - money_before
        if after is not None and after.lootable:
            log(f"  loot left on the corpse - retrying later (order from {d_order:.1f} yd, "
                f"ctm {acts}, loot window {'seen' if saw_window else 'not seen'}, "
                f"combat={self.g.player().in_combat})")
            lbl[c.guid] = time.time() + 45
            self.bridge.send(br.CLOSE_ALL)
        else:
            self.looted += 1
            log("  looted" + (f" +{got_money} copper" if got_money else ""))
        return True

    def approach(self, me, t: Unit, reach: float):
        """Step toward a target along the navmesh (walls, fences, the abbey)."""
        # melee (reach 3.5) stops 2.5 yd out, not 1.5: from 1.2 yd the mob is
        # under the camera, the right-click that starts auto-attack misses,
        # and at 15:59 a wolf stood untouched at our feet for 25 s
        goal_short = 2.5 if reach <= 5.0 else max(reach - 2, 1.5)
        path = None
        # The cached path must be for THIS mob where it stands NOW. It was
        # kept 1.5 s whatever happened, with no note of whose it was: the
        # warrior ran to the spot a mob had engaged from (or to the previous
        # mob's corpse) while the mob itself came at it from the side.
        key = getattr(self, "_path_for", None)
        stale = (key is None or key[0] != t.guid
                 or math.dist(key[1], (t.x, t.y)) > 2.0
                 or time.time() - getattr(self, "_path_t", 0) > 1.5)
        if self.nav and (stale or not self._path):
            path = self.nav.find_path((me.x, me.y, me.z), (t.x, t.y, t.z))
            self._path, self._path_t = path or [], time.time()
            self._path_for = (t.guid, (t.x, t.y))
        path = self._path
        if path and math.dist(path[-1][:2], (t.x, t.y)) > 3.0:
            # the mesh end snaps off the mob (it stands on a doodad): the last
            # leg is always the mob's live position
            path = path[:-1] + [(t.x, t.y, t.z)]
        if me.dist(t) < 12.0 and abs(t.z - me.z) < 4.0 and self.straight_clear(me, (t.x, t.y, t.z)):
            # close and nothing in between: head straight at it, re-aimed every
            # tick, rather than walking out a path planned 0.2-1.5 s ago
            path = []
        if not path:
            self.ctm.move_toward(me, t.x, t.y, t.z, goal_short)
            if me.dist(t) > 8.0:
                self.maybe_jump(closing=True)
            time.sleep(0.2)
            return
        # next waypoint we have not reached yet; stop short near the target
        while len(path) > 1 and math.dist((me.x, me.y), path[0][:2]) < 1.5:
            path.pop(0)
        wx, wy, wz = path[0]
        if len(path) == 1:
            self.ctm.move_toward(me, wx, wy, wz, goal_short)
        else:
            self.ctm.move_to(wx, wy, wz)
        if me.dist(t) > 8.0:
            self.maybe_jump(closing=True)
        time.sleep(0.2)

    CORPSE_RECLAIM_RADIUS = 36.0       # server allows 39 yd; keep a margin
    SAFE_DISTANCE = 30.0               # desired gap to the nearest hostile mob

    def threats(self) -> list[Unit]:
        """Living mobs that could attack us (not friendly, not critters)."""
        me = self.g.player()
        units = [u for u in self.g.units() if not u.dead and not u.npc_flags]
        info = worlddb.creatures({u.entry for u in units})
        return [u for u in units
                if not factions.is_friendly(me.faction, u.faction)
                and not (info.get(u.entry) and info[u.entry].critter)]

    def aggressors(self) -> list[Unit]:
        """Living mobs that attack us UNPROVOKED: hostile, not merely
        "not friendly". The wolves, troggs and boars of Coldridge are
        neutral (yellow names - they never start a fight), yet travel
        steered round every one of them and resting walked away from them.
        Use this for "will it notice us"; threats() for "can it join in"."""
        me = self.g.player()
        return [u for u in self.threats() if factions.is_hostile(me.faction, u.faction)]

    def safe_revive_spot(self, corpse, exclude=()):
        """Point within reclaim range of the corpse, as far from mobs as possible."""
        threats = self.threats()
        cx, cy, cz = corpse
        best, best_score = None, -1e9
        for r in (0.0, 12.0, 20.0, 28.0, self.CORPSE_RECLAIM_RADIUS):
            for k in range(1 if r == 0 else 24):
                a = 2 * math.pi * k / 24
                x, y = cx + r * math.cos(a), cy + r * math.sin(a)
                if any(math.dist((x, y), e) < 4 for e in exclude):
                    continue
                z = cz
                if self.nav:
                    poly, _ = self.nav.nearest_poly(x, y, cz + 2, max_dist=1.0)
                    if poly is None:
                        continue                 # not walkable ground
                    t = self.nav.tiles[poly[0]]
                    fl = int(t.pflags[poly[1]])
                    if fl & 0x10 or (fl & 0x08 and not fl & 0x01):      # NAV_STEEP / water-only
                        continue                 # a coastal slope: two minutes of "unstuck" as a ghost
                    z = float(t.center[poly[1]][1])
                gap = min((math.dist((x, y), (u.x, u.y)) for u in threats), default=99.0)
                # prefer clear space, then a short walk from the corpse
                score = min(gap, 45.0) - 0.05 * r
                if score > best_score:
                    best, best_score = (x, y, z, gap), score
        return best

    def revive_at_spirit_healer(self, me) -> bool:
        """Below level 10 there is no resurrection sickness, so reviving at the
        graveyard costs only durability - and the graveyard is a safe spot,
        unlike a corpse lying among the mobs that killed us."""
        healer = self.nearest_service(me, "spirithealer")
        if healer is None:
            return False
        if math.dist(healer["pos"][:2], (me.x, me.y)) > 120:
            # already ran to the corpse and gave up on it (death trap):
            # back to the graveyard as a ghost
            log(f"ghost: heading back to the spirit healer, "
                f"{math.dist(healer['pos'][:2], (me.x, me.y)):.0f} yd")
            if not self.navigate(*healer["pos"], stop_short=6.0, timeout=400):
                return False
        log(f"ghost: level {me.level}, reviving at {healer['name']} (no penalty below 10)")
        unit = self.visit_npc(healer)
        if unit is None:
            return False
        # gossip "Return me to life." -> confirmation popup -> accept
        self.bridge.send(br.SPIRIT_HEALER, wait=1.2)      # "Return me to life."
        self.bridge.send(br.SPIRIT_HEALER, wait=2.0)      # accept the confirmation
        me = self.g.player()
        if not me.ghost and me.health > 0:
            log(f"resurrected at the graveyard with {me.hp_pct:.0f}% hp - resting to full")
            self.need_full_rest = True
            return True
        return False

    def handle_death(self, me) -> None:
        """Release, run back as a ghost, resurrect at a spot away from mobs."""
        set_state("dead" if me.dead else "corpse run")
        if me.dead:
            self.corpse_pos = (me.x, me.y, me.z)
            # remember it across bot restarts, with the recent death spots:
            # dying repeatedly in one place (Fargodeep's lower level at 7)
            # means the place is beyond us for now, not that we were unlucky
            spots = self.death_spots()
            spots.append([me.x, me.y, time.time()])
            json.dump({"corpse": self.corpse_pos, "time": time.time(), "spots": spots},
                      open(DEATH_FILE, "w"))
            self.revive_spot = None
            self.bad_spots = []
            self.deaths += 1
            log(f"died (death #{self.deaths}) at ({me.x:.1f}, {me.y:.1f}) - releasing spirit")
            events.emit("death", n=self.deaths, lvl=me.level, x=round(me.x, 1), y=round(me.y, 1),
                        step=getattr(self, "prog", {}).get("step"))
            trap = self.death_trap()
            if trap:
                log(f"  second death here in {DEATH_TRAP_MINUTES} min - the area is a death trap: "
                    "quest steps there are skipped for a while, spirit healer this time")
                self._corpse_runs_failed = 1
            time.sleep(1.5)
            self.bridge.send(br.REPOP)
            t0 = time.time()
            while time.time() - t0 < 15 and not self.g.player().ghost:
                time.sleep(0.5)
            time.sleep(2.0)                       # loading into the graveyard
            return
        corpse = self.g.my_corpse() or getattr(self, "corpse_pos", None)
        # Fall back to the position we recorded when we died BEFORE deciding
        # corpse-vs-healer, not after. my_corpse() only sees a corpse that is
        # in range, and corpse_pos is in-process memory, so after a restart
        # mid-ghost-run both are empty and `far` came out True however close
        # the body actually was. Measured: restarted 85 yd from the corpse and
        # the bot set off for a spirit healer 923 yd away, over a hillside it
        # got stuck on six times, having already walked AWAY from the body.
        if corpse is None and os.path.exists(DEATH_FILE):
            try:
                corpse = tuple(json.load(open(DEATH_FILE))["corpse"])
                self.corpse_pos = corpse
                log(f"ghost: corpse position from last run ({corpse[0]:.0f}, {corpse[1]:.0f})")
            except Exception:
                corpse = None
        # A player runs back to the corpse; the spirit healer (25% durability
        # damage, and a bot tell) is for a corpse far away or one we could not
        # reach - resurrection sickness only starts at level 10 anyway.
        far = corpse is None or math.dist((me.x, me.y), corpse[:2]) > 700
        # above level 10 the healer costs a few minutes of sickness: still
        # better than reviving into the camp that killed us twice
        if ((me.level < SPIRIT_HEALER_BELOW_LEVEL and far)
                or getattr(self, "_corpse_runs_failed", 0) >= 1 or self.death_trap()) \
                and self.revive_at_spirit_healer(me):
            self._corpse_runs_failed = 0
            return
        if corpse is None:
            log("ghost, but corpse position unknown - waiting")
            time.sleep(3)
            return
        d_corpse = math.dist((me.x, me.y), corpse[:2])
        if d_corpse > 60:
            # far away (graveyard): head to the corpse area first; mobs near
            # the corpse only become visible once we get close.
            log(f"ghost: running toward corpse, {d_corpse:.0f} yd")
            if not self.navigate(*corpse, stop_short=45.0) and not self.stopped():
                self._corpse_runs_failed = getattr(self, "_corpse_runs_failed", 0) + 1
                log("  could not get to the corpse - the spirit healer it is")
            return
        # Choose a spot once and stick to it; only re-pick if a mob wanders
        # close to it or it turns out to be unreachable. (Re-picking every
        # tick made the ghost zig-zag between near-equal spots.)
        spot = getattr(self, "revive_spot", None)
        if spot is not None:
            gap = min((math.dist(spot[:2], (u.x, u.y)) for u in self.aggressors()), default=99.0)
            spot = (*spot[:3], gap)
            # only drop the spot when a mob is really on it: with a 20 yd
            # bar the ghost zig-zagged between two spots for two minutes in
            # the mine while patrols wandered 16-19 yd past each of them
            if gap < 10:
                spot = None
        if spot is None:
            spot = self.safe_revive_spot(corpse, getattr(self, "bad_spots", [])) or (*corpse, 0.0)
            self.revive_spot = spot
        x, y, z, gap = spot
        # a crowd (ten kobolds in the mine) needs more room than a patrol
        crowd = sum(1 for u in self.aggressors() if math.dist((x, y), (u.x, u.y)) < 30)
        need_gap = 22 if crowd >= 4 else 12
        # already in reach of the corpse with nothing close: revive where we
        # stand. A ghost at 37 yd from its corpse spent two minutes stuck on
        # a rock walking to a "better" spot 3 yd further on
        here_gap = min((math.dist((me.x, me.y), (u.x, u.y)) for u in self.aggressors()), default=99.0)
        if (math.dist((me.x, me.y), (x, y)) > 3 and math.dist(corpse[:2], (me.x, me.y)) < 34
                and here_gap >= max(need_gap, gap)):
            x, y, z, gap = me.x, me.y, me.z, here_gap
        if math.dist((me.x, me.y), (x, y)) > 3:
            log(f"ghost: moving to revive spot {math.dist(corpse[:2], (x, y)):.0f} yd "
                f"from corpse, nearest mob {gap:.0f} yd")
            if not self.navigate(x, y, z, stop_short=1.5, timeout=45):
                me = self.g.player()
                if math.dist((me.x, me.y), (x, y)) > 5:
                    log("  revive spot unreachable - picking another")
                    self.bad_spots = getattr(self, "bad_spots", []) + [(x, y)]
                    self.revive_spot = None
            return
        if gap < need_gap:
            self._revive_waits = getattr(self, "_revive_waits", 0) + 1
            if self._revive_waits > 20:              # a minute: a camp, not a patrol
                log("  no safe spot near the corpse - the spirit healer it is")
                self._revive_waits = 0
                self._corpse_runs_failed = getattr(self, "_corpse_runs_failed", 0) + 1
                return
            log(f"  mob within {gap:.0f} yd of the best spot - waiting for it to move")
            time.sleep(3)
            return
        self._revive_waits = 0
        # Reclaiming has a delay after death; RetrieveCorpse() simply fails
        # until it expires, so keep asking.
        self.bridge.send(br.RETRIEVE_CORPSE)
        time.sleep(2.0)
        me = self.g.player()
        if not me.ghost and me.health > 0:
            log(f"resurrected with {me.hp_pct:.0f}% hp, nearest mob {gap:.0f} yd - resting to full")
            self.need_full_rest = True
            # Back in a camp at half health: get clear of it before sitting
            # down (resting 20 yd from a kobold patrol was death #3).
            near = [u for u in self.aggressors() if me.dist(u) < 40]
            trap = self.death_trap()
            if trap and math.dist(trap, (me.x, me.y)) < 120:
                self.leave_death_trap(me, trap)
            elif near:
                p = self.retreat_point(me, near, 30.0)
                if p:
                    log(f"  {len(near)} mobs within 30 yd - moving away before resting")
                    self.navigate(*p, stop_short=2.0, timeout=30)
            self.rest_if_needed(self.g.player())

    def leave_death_trap(self, me, trap):
        """Resurrected inside a place that killed us twice: walk 140 yd out of
        it toward the side with the fewest mobs of our level before resting."""
        import dangers
        spawns = [(x, y, hi) for m, x, y, z, w, e, lo, hi, rank, fac, det in dangers._load()
                  if m == 0 and hi >= me.level - 2 and factions.is_hostile(me.faction, fac)
                  and math.dist((x, y), (me.x, me.y)) < 260]
        best, best_n = None, 1e9
        for k in range(12):
            a = k * math.pi / 6
            px, py = trap[0] + 140 * math.cos(a), trap[1] + 140 * math.sin(a)
            n = sum(1 for x, y, hi in spawns if math.dist((x, y), (px, py)) < 45)
            n += math.dist((px, py), (me.x, me.y)) / 100          # prefer the near side a little
            if n < best_n and self.nav and self.nav.find_path((me.x, me.y, me.z), (px, py, me.z)):
                best, best_n = (px, py), n
        if best is None:
            return
        log(f"  leaving the death trap: {math.dist(best, (me.x, me.y)):.0f} yd to a quieter spot")
        self.navigate(best[0], best[1], me.z, stop_short=5.0, timeout=90)

    def death_spots(self, max_age=3600) -> list:
        try:
            spots = json.load(open(DEATH_FILE)).get("spots", [])
        except Exception:
            spots = []
        return [sp for sp in spots if time.time() - sp[2] < max_age]

    def death_trap(self):
        """(x, y) of a place we died at twice recently, else None (murloc camps)."""
        spots = self.death_spots()
        if not spots:
            return None
        x, y, t = spots[-1]
        near = [sp for sp in spots if math.dist(sp[:2], (x, y)) < 80
                and time.time() - sp[2] < DEATH_TRAP_MINUTES * 60]
        return (x, y) if len(near) >= 2 else None

    def find_prey(self, me) -> Unit | None:
        units = self.g.units()
        info = worlddb.creatures({u.entry for u in units})
        prey = [u for u in units if self.valid_prey(me, u, info)]
        # Avoid pulls that bring friends: skip a target with another hostile
        # mob close to it (mages die to two mobs at once at low level).
        hostile = [u for u in units if not u.dead and not u.npc_flags
                   and factions.is_hostile(me.faction, u.faction)
                   and not (info.get(u.entry) and info[u.entry].critter)]
        prey = [p for p in prey
                if not any(h.guid != p.guid and h.dist(p) < add_radius(me.level)
                           for h in hostile)]
        # a wandering mob three+ levels up that Polymorph cannot take (Dust
        # Devil 19 in the Alexston farm) joins every nearby fight: no pulls
        # within 45 yd of one, it moves on
        strong = [h for h in hostile if h.level >= me.level + 3
                  and (info.get(h.entry) is None or info[h.entry].type not in (1, 7))]
        if strong:
            before = len(prey)
            prey = [p for p in prey if not any(s.dist(p) < 45 or me.dist(s) < 30 for s in strong)]
            s = min(strong, key=me.dist)
            if me.dist(s) < 30 and not me.in_combat:
                # it wanders: it aggroed us while resting twice - get 40 yd away
                log(f"  {worlddb.name(s.entry)} (lvl {s.level}) {me.dist(s):.0f} yd away - moving off")
                ang = math.atan2(me.y - s.y, me.x - s.x)
                self.navigate(me.x + 40 * math.cos(ang), me.y + 40 * math.sin(ang), me.z,
                              stop_short=5.0, timeout=30, careful=True)
                return None
            if before and not prey and time.time() - getattr(self, "_strong_note", 0) > 30:
                self._strong_note = time.time()
                log(f"  {worlddb.name(s.entry)} (lvl {s.level}) {me.dist(s):.0f} yd away - "
                    "waiting for it to move on")
        # the closest by the walk, not the crow: check the nearest few
        best, best_w = None, 1e9
        for u in sorted(prey, key=me.dist)[:6]:
            if me.dist(u) >= best_w:
                break                          # a straight line can only be shorter
            w = self.walk_dist(me, u)
            if w is not None and w < 1.6 * me.dist(u) + 8 and w < best_w:
                best, best_w = u, w
        return best

    def walk_to_prey(self, me) -> bool:
        """Nothing pullable within PULL_RANGE: walk toward the nearest valid
        target further out. Standing at the spot until one wandered in was
        "searching" 53 times for 13 s each in one Coldridge run (11 of 75
        minutes); a player walks to the next mob. Combat on the way is the
        point: pull_candidate opens on it as it comes into range."""
        units = self.g.units()
        info = worlddb.creatures({u.entry for u in units})
        far = [u for u in units if me.dist(u) > PULL_RANGE
               and self.valid_prey(me, u, info, reach=140.0)]
        if not far:
            return False
        hostile = [u for u in units if not u.dead and not u.npc_flags
                   and factions.is_hostile(me.faction, u.faction)
                   and not (info.get(u.entry) and info[u.entry].critter)]
        far = [p for p in far
               if not any(h.guid != p.guid and h.dist(p) < add_radius(me.level) for h in hostile)]
        if not far:
            return False
        u = min(far, key=me.dist)
        if time.time() - getattr(self, "_walk_prey_t", 0) > 20:
            self._walk_prey_t = time.time()
            log(f"  nothing within {PULL_RANGE:.0f} yd - walking toward "
                f"{worlddb.name(u.entry)} (lvl {u.level}), {me.dist(u):.0f} yd")
        set_state("searching", detail=f"toward {worlddb.name(u.entry)}")
        self.navigate(u.x, u.y, u.z, stop_short=PULL_RANGE - 15.0, timeout=30)
        return True

    def roam_home(self, me):
        """No prey in reach: (re)join the hunting ground, e.g. after a fight
        interrupted the trip there. Uses the navmesh and avoids danger."""
        d = math.dist(self.home[:2], (me.x, me.y))
        if d > 15:
            if d > self.radius:
                if self.buff_if_needed(me):
                    return
                self.need_full_rest = True
                if self.rest_if_needed(me):
                    return
                log(f"heading to the hunting ground, {d:.0f} yd")
                set_state("travelling", detail=self.stage["name"] if self.stage else "")
            self.navigate(*self.home, stop_short=10.0, timeout=self.travel_timeout(d, 900.0))

    # ------------------------------------------------------------ status
    HANG_S = 15.0

    def _hang_watch(self):
        """Dump every thread's stack to hang.log when the bot goes silent:
        no log line and the character not moving for HANG_S while it is
        neither sitting nor dead. At 11:01:51 the paladin logged a kill, then
        nothing for 38 s standing still in state "fighting", and was killed -
        with no way to tell which call it was stuck in."""
        import sys, traceback
        last_line, last_pos, since, dumped = None, None, time.time(), False
        while not self._stop_publish.is_set():
            time.sleep(1.0)
            try:
                me = self.g.player()
                pos = (round(me.x, 1), round(me.y, 1))
                busy = me.sitting or me.dead or me.ghost
            except Exception:
                continue
            line = LOG_TAIL[-1] if LOG_TAIL else None
            if line != last_line or pos != last_pos or busy:
                last_line, last_pos, since, dumped = line, pos, time.time(), False
                continue
            if not dumped and time.time() - since >= self.HANG_S:
                dumped = True
                try:
                    with open("hang.log", "a", encoding="utf-8") as f:
                        f.write(f"==== {time.strftime('%H:%M:%S')} silent {time.time() - since:.0f}s, "
                                f"state {STATE.get('state')}, in combat {me.in_combat}, "
                                f"hp {me.hp_pct:.0f}%\n")
                        for tid, frame in sys._current_frames().items():
                            f.write(f"-- thread {tid}\n")
                            f.write("".join(traceback.format_stack(frame)))
                    log(f"HANG: silent and still for {self.HANG_S:.0f}s "
                        f"(state {STATE.get('state')}) - stacks in hang.log")
                except Exception:
                    pass

    def _publish_loop(self):
        import json
        self.started = time.time()
        xp_hist = []                                   # (t, total_xp_ever)
        while not self._stop_publish.is_set():
            try:
                me = self.g.player()
                total = self.xp_total(me)
                xp_hist.append((time.time(), total))
                xp_hist[:] = [h for h in xp_hist if time.time() - h[0] < 900]
                span = xp_hist[-1][0] - xp_hist[0][0]
                xph = (xp_hist[-1][1] - xp_hist[0][1]) / span * 3600 if span > 60 else 0
                units = []
                for u in self.g.units():
                    if me.dist(u) > 60:
                        continue
                    kind = ("dead" if u.dead else "friend" if factions.is_friendly(me.faction, u.faction)
                            else "target" if u.guid == self.g.target_guid() else "mob")
                    units.append((round(u.x - me.x, 1), round(u.y - me.y, 1), kind))
                tgt = self.g.unit(self.g.target_guid()) if self.g.target_guid() else None
                # what comes next, for the overlay: the guide step in hand and
                # the one after it, or the pending town trip
                plan, used, cap, dur = [], 0, 0, 100.0
                try:
                    used, cap = self.g.bag_item_count(), self.g.bag_capacity()
                    dur = gear.durability(self.g)[2]
                    if self.guide_active and getattr(self, "gsteps", None):
                        i = self.prog["step"]
                        for k in (i, i + 1):
                            if k < len(self.gsteps):
                                stp = self.gsteps[k]
                                acts = [f"{tg}{ar}" for j, (tg, ar) in enumerate(stp.actions)
                                        if k != i or j not in self.prog["done"]]
                                import re as _re
                                txt = _re.sub(r"#(\d+)", lambda m: worlddb.name(int(m.group(1))) or m.group(0),
                                              stp.text)
                                plan.append(f"step {k + 1}/{len(self.gsteps)}: {txt[:80]}"
                                            + (f"  [{' '.join(acts)[:60]}]" if acts else ""))
                    elif self.stage:
                        plan.append(f"grind stage {self.stage['name']}")
                    if used >= cap - 2:
                        plan.insert(0, f"vendor trip (bags {used}/{cap})")
                    elif dur < 40:
                        plan.insert(0, f"repair trip (worst item {dur:.0f}%)")
                except Exception:
                    pass
                npath = getattr(self, "_nav_path", None) or []
                goal = getattr(self, "_nav_goal", None)
                try:
                    guide_st = self.guide_status()
                except Exception:
                    guide_st = None          # never let the page break the status file
                st = {
                    "guide": guide_st,
                    "plan": plan, "bags": f"{used}/{cap}", "durability": round(dur),
                    "path": [[round(p[0] - me.x, 1), round(p[1] - me.y, 1)] for p in npath[:80]],
                    "goal": [round(goal[0] - me.x, 1), round(goal[1] - me.y, 1)] if goal else None,
                    "ts": time.time(), "running": True, **STATE,
                    "name": self.g.player_name(), "level": me.level,
                    "hp": me.health, "max_hp": me.max_health,
                    "mana": me.power, "max_mana": me.max_power,
                    "xp": me.xp, "next_xp": me.next_xp, "xp_per_hour": round(xph),
                    "kills": self.kills, "deaths": self.deaths, "looted": self.looted,
                    "money": me.money,
                    "uptime": round(time.time() - self.started),
                    "ghost": me.ghost, "in_combat": me.in_combat,
                    "facing": me.facing, "pos": [me.x, me.y, me.z],
                    "home": [self.home[0] - me.x, self.home[1] - me.y], "radius": self.radius,
                    "stage": self.stage["name"] if self.stage else None,
                    "target": None if tgt is None else {
                        "name": worlddb.name(tgt.entry), "level": tgt.level,
                        "hp_pct": round(tgt.hp_pct), "dist": round(me.dist(tgt), 1)},
                    "units": units, "log": LOG_TAIL[-14:],
                }
                if time.time() - getattr(self, "_pos_t", 0) >= 2.0:
                    # the track navdbg.py draws: where we actually walked
                    self._pos_t = time.time()
                    events.emit("pos", x=round(me.x, 1), y=round(me.y, 1), z=round(me.z, 1),
                                s=STATE.get("state"))
                tmp = STATUS_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(st, f)
                os.replace(tmp, STATUS_FILE)
            except Exception:
                pass
            self._stop_publish.wait(0.5)

    def xp_total(self, me) -> int:
        """XP accumulated since start, carried across level-ups."""
        if me.level != self._lvl:
            self._xp_base += self._lvl_next           # finished a level
            self._lvl, self._lvl_next = me.level, me.next_xp
        self._lvl_next = me.next_xp
        return self._xp_base + me.xp

    # ------------------------------------------------------------ main loop
    def gear_check(self, force=False) -> bool:
        """Equip bag items that beat what is worn (at most one per call)."""
        if not force and time.time() < getattr(self, "_next_gear", 0.0):
            return False
        self._next_gear = time.time() + 20.0
        try:
            ups = gear.upgrades(self.g, self.p)
        except Exception as e:
            log(f"gear check failed: {e}")
            return False
        tried = getattr(self, "_gear_tried", {})
        self._gear_tried = tried
        for entry, gain, desc in ups:
            if tried.get(entry, 0) >= 2:           # refused twice (bound, unique...)
                continue
            tried[entry] = tried.get(entry, 0) + 1
            log(f"gear: equipping {desc}")
            self.bridge.send(br.EQUIP_ITEM, entry, wait=1.2)
            self._next_gear = 0.0                  # look again right away for more
            return True
        return False

    def run(self):
        import threading
        me = self.g.player()
        self._lvl, self._lvl_next, self._xp_base = me.level, me.next_xp, 0
        self._stop_publish = threading.Event()
        threading.Thread(target=self._publish_loop, daemon=True).start()
        threading.Thread(target=self._hang_watch, daemon=True).start()
        self.t_start = time.time()
        end = time.time() + self.args.minutes * 60 if self.args.minutes else None
        try:
            while not self.stopped() and (end is None or time.time() < end):
                try:
                    if not self.g.in_world():
                        log("not in world - waiting")
                        time.sleep(3)
                        continue
                    me = self.g.player()
                    if me.level != getattr(self, "_lvl_seen", me.level):
                        log(f"LEVEL {me.level} reached ({(time.time() - self.t_start) / 60:.0f} min in)")
                        events.emit("level", lvl=me.level)
                        self._next_gear = 0.0          # new level: maybe new gear fits
                        # two levels on, a kind that swarmed us is worth another try
                        for k in [k for k, l in self.pack_level.items() if me.level >= l + 2]:
                            self.pack_mobs.pop(k, None)
                            self.pack_level.pop(k, None)
                            log(f"  {worlddb.name(k)}s: 2 levels since they swarmed us - pulling them again")
                        self.save_packs()
                    self._lvl_seen = me.level
                    if me.dead or me.ghost:
                        self.handle_death(me)
                        continue
                    attackers = self.attackers(me)
                    if attackers:
                        self.fight(min(attackers, key=me.dist))
                        continue
                    if me.in_combat:
                        # flagged but nobody on us (a fleeing mob, a lingering
                        # flag): NPC visits refuse to start - wait it out
                        # ... unless something IS hitting us that attackers()
                        # cannot see (no target field on us: a stunned mob, a
                        # mob between targets). At 11:01 the paladin stood 38 s
                        # here in silence, 60% -> dead, looping on this sleep.
                        t0, hp0 = self.__dict__.get("_flag_only", (None, None))
                        if t0 is None:
                            self._flag_only = t0, hp0 = time.time(), me.health
                        if me.health < hp0 or time.time() - t0 > 2.0:
                            near = [u for u in self.g.units()
                                    if not u.dead and u.in_combat and me.dist(u) < 10
                                    and not u.summoned_by
                                    and factions.is_hostile(me.faction, u.faction)]
                            if near:
                                u = min(near, key=me.dist)
                                log(f"  in combat with nobody on us, but {worlddb.name(u.entry)} "
                                    f"is {me.dist(u):.0f} yd away (its target {u.target:#x}, "
                                    f"hp {me.hp_pct:.0f}%) - fighting it")
                                self._flag_only = (None, None)
                                self.fight(u)
                                continue
                        time.sleep(0.5)
                        continue
                    self._flag_only = (None, None)
                    if self.leave_water(me):
                        continue
                    if self.wait_out_sickness(me):
                        continue
                    # loot first when it is safe: a corpse here does not keep
                    # through a 35 s rest (66 of 71 goretusks went unlooted -
                    # no livers, no quest), and the loot is the reason we fought
                    # ... and at any hp when the corpse is at our feet and nothing is
                    # on us: resting at 1% hp first walked away from Grik'nir's
                    # corpse and lost The Stolen Journal's only drop
                    if (not self.args.no_loot and (me.hp_pct >= 35 or self.corpse_at_feet(me))
                            and self.loot_nearby(me)):
                        continue
                    # each check between fights is timed: one that answers
                    # "nothing to do" slowly stands the character still after
                    # every kill, and no other log line would say so
                    tm = self._timed
                    if tm("fix_action_bar", self.fix_action_bar, me):
                        continue
                    if (tm("buff", self.buff_if_needed, me) or tm("rest", self.rest_if_needed, me)
                            or tm("ensure_water", self.ensure_water, me)):
                        continue
                    if not self.args.no_loot and tm("loot", self.loot_nearby, me):
                        continue
                    if tm("manage_bags", self.manage_bags):
                        continue
                    if tm("gear_check", self.gear_check):
                        continue
                    # train BEFORE moving up a stage: the next area assumes the
                    # new spells (Fire Blast at 6, Polymorph at 8, ...)
                    trip = tm("town_trip_needed", self.town_trip_needed, me)
                    if trip:
                        self.town_trip(trip)
                        continue
                    need = self.guide_min_level() if self.guide_active else 0
                    if need and me.level < need:
                        # a level-12 guide at level 11 was four deaths in 15
                        # minutes: grind the route stage for this level first
                        if getattr(self, "_underlevel_note", None) != need:
                            log(f"guide '{self.guide.name}' wants level {need} - "
                                f"grinding the route until then")
                            self._underlevel_note = need
                    elif self.guide_active:
                        if self.quest_tick(me):
                            continue
                        nxt = self.guide.next_guide      # the guide's own #next
                        if nxt:
                            log(f"guide finished - continuing with {nxt}")
                            try:
                                self.setup_guide(nxt, force=True)   # not the saved (finished) one
                                continue
                            except KeyError as e:
                                log(f"  {e}")
                        log("guide finished - continuing with the grinding route")
                        self.guide_active = False
                    if self.update_stage(me):
                        continue
                    set_state("searching")
                    # Do not pull while hurt. Twice tonight a retreat-to-rest
                    # was abandoned among Loch Crocolisks and the bot pulled
                    # one at 36-41% hp; both fights drew the pair and killed
                    # it. Rest wins over the next kill.
                    _pm = self.profile.pull_mana if self.profile.pull_mana is not None else self.args.pull_mana
                    if me.hp_pct < self.args.pull_hp or me.mana_pct < _pm:
                        self.need_full_rest = True
                        if self.rest_if_needed(me):
                            continue
                        # Rest was needed and did NOT happen - nearly always
                        # because there are mobs too close to sit among, and
                        # the retreat before sitting found nowhere to go. The
                        # old code fell straight through to find_prey here,
                        # which pulls at exactly the health this floor exists
                        # to prevent: at 16:45 the paladin logged "resting: hp
                        # 70% mana 63%", then "4 mobs within 22 yd - moving
                        # away before sitting down", then two seconds later
                        # walked 52 yd to the next Tunnel Rat with the floor
                        # at 80. Two of the den joined and it died. Back out
                        # of the crowd and come round again instead of buying
                        # a fight we cannot pay for.
                        crowd = [u for u in self.aggressors() if me.dist(u) < 40]
                        spot = self.retreat_point(me, crowd, 40.0) if crowd else None
                        if spot:
                            log(f"  {me.hp_pct:.0f}% hp, {me.mana_pct:.0f}% mana and nowhere "
                                f"to rest among {len(crowd)} mobs - backing off before pulling")
                            self.navigate(*spot, stop_short=2.0, timeout=30)
                            continue
                    prey = self.find_prey(me)
                    if prey:
                        self.fight(prey)
                    elif self.walk_to_prey(me):
                        continue
                    else:
                        self.roam_home(me)
                        time.sleep(0.5)
                except (NotInWorld, MemoryError_, RuntimeError) as e:
                    if isinstance(e, RuntimeError) and self.g.in_world():
                        # a DB query or logic error, not a loading screen:
                        # say what it was and carry on rather than spin
                        import traceback
                        log(f"error: {str(e).splitlines()[-1][:160]}")
                        log("  " + traceback.format_exc().strip().splitlines()[-3].strip()[:160])
                        time.sleep(3)
                        continue
                    log(f"not in world ({type(e).__name__}) - waiting; no input is sent")
                    set_state("not in world")
                    while not self.stopped() and not self.g.in_world():
                        time.sleep(3)
                    if not self.stopped():
                        log("back in world - resuming in 5 s")
                        time.sleep(5)
                        self.install_lua_helpers()
                except Exception:
                    # A bug in one tick must not kill the run. My own
                    # NameError in retreat_point ended the process mid-fight
                    # at 03:53 and the mage stood still for 97 s (including
                    # the 38 s navmesh reload) while three crocolisks killed
                    # it. Staying alive in-process also keeps the navmesh
                    # loaded, which no external relauncher can do.
                    import traceback
                    tb = traceback.format_exc()
                    self._tick_errors = getattr(self, "_tick_errors", 0) + 1
                    last = tb.strip().splitlines()[-1][:160]
                    log(f"tick error #{self._tick_errors}: {last}")
                    try:
                        path = os.path.join(os.path.dirname(__file__), "data",
                                            "tick_errors.log")
                        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                        with open(path, "a", encoding="utf-8") as fh:
                            fh.write(os.linesep + "===== " + stamp + os.linesep + tb)
                    except Exception:
                        pass
                    if self._tick_errors >= 20:
                        log("20 tick errors - the build is broken, stopping")
                        raise
                    try:
                        me = self.g.player()
                        if me.in_combat and me.hp_pct < 60:
                            self.ctm.stop(me)   # do not stand and spin
                    except Exception:
                        pass
                    time.sleep(min(2.0 * self._tick_errors, 10.0))
        except KeyboardInterrupt:
            pass
        finally:
            try:
                me = self.g.player()
                self.ctm.stop(me)
            except Exception:
                me = None
            log(f"stopped. kills={self.kills} deaths={self.deaths} looted={self.looted}"
                + (f" level {self.start_level}->{me.level} xp {me.xp}/{me.next_xp}" if me else ""))
            self._stop_publish.set()
            time.sleep(0.6)
            try:
                import json
                st = json.load(open(STATUS_FILE, encoding="utf-8"))
                st.update(running=False, state="stopped", log=LOG_TAIL[-14:])
                json.dump(st, open(STATUS_FILE, "w", encoding="utf-8"))
            except Exception:
                pass
            self.p.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--radius", type=float, default=90, help="leash radius around start (yd)")
    ap.add_argument("--minutes", type=float, default=0, help="stop after N minutes (0 = never)")
    ap.add_argument("--pull-hp", type=float, default=65,
                    help="rest before pulling unless HP is at least this %%")
    ap.add_argument("--pull-mana", type=float, default=60,
                    help="rest before pulling unless mana is at least this %%")
    ap.add_argument("--max-level-diff", type=int, default=0)
    ap.add_argument("--no-loot", action="store_true", help="do not loot corpses")
    ap.add_argument("--guide", default=None,
                    help="quest with a RestedXP Survival guide: 'auto' (where this "
                         "character's level sits on its race's chain) or a #name "
                         "with _ for spaces, e.g. 6-10_Dun_Morogh")
    ap.add_argument("--route", default="human_mage_elwynn",
                    help="levelling route in routes/ (name or path)")
    ap.add_argument("--here", action="store_true",
                    help="ignore the route and farm around the current position (--radius)")
    ap.add_argument("--goto", default=None, metavar="X,Y,Z",
                    help="test mode: navigate to a point (tram, portals and all) and exit")
    ap.add_argument("--buy-gear", choices=("off", "dry", "on"), default="on",
                    help="buy armour and weapons at the merchant we are already at: "
                         "'dry' logs what it would buy and spends nothing, 'on' buys. "
                         "On by default: a vendor weapon twice the starter's was never bought "
                         "in a 3 h run with it off (Dsa, 2026-09-24).")
    args = ap.parse_args()
    lock = single_instance()
    if lock is None:
        print("another bot is already driving the client (bot.lock is held) - not starting")
        sys.exit(1)
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    bot = MageBot(args)
    if args.goto:
        import threading
        x, y, z = (float(v) for v in args.goto.split(","))
        me = bot.g.player()
        bot._lvl, bot._lvl_next, bot._xp_base = me.level, me.next_xp, 0
        bot._stop_publish = threading.Event()
        bot.t_start = time.time()
        threading.Thread(target=bot._publish_loop, daemon=True).start()
        me = bot.g.player()
        log(f"goto ({x:.0f}, {y:.0f}, {z:.0f}) from ({me.x:.0f}, {me.y:.0f}, {me.z:.0f})")
        ok = bot.navigate(x, y, z, stop_short=3.0, timeout=3600, careful=True)
        me = bot.g.player()
        log(f"goto {'arrived' if ok else 'failed'} at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})")
        return
    bot.run()


def single_instance():
    """Hold an exclusive OS lock on bot.lock for the life of the process, so two
    bots can never drive one client at once. Windows drops the lock when the
    process dies, even if it is killed."""
    import msvcrt
    f = open(os.path.join(HERE, "bot.lock"), "a+")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    f.truncate(0)
    f.write(str(os.getpid()))
    f.flush()
    return f


if __name__ == "__main__":
    main()
