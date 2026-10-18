"""In-combat footwork: small human strafing, and backing a mob away from an add.

Three kinds of burst, all driven by the keyboard ("q" strafe left, "e" strafe
right, "s" backpedal, arrow "left"/"right" turn) and never by click-to-move:

  IDLE     short side-to-side bursts while fighting, so the character does not
           stand frozen between presses the way nothing alive does.
  RESQUARE a short arrow-key turn back onto the target. See "WHY WE TURN".
  DRAG     when a second mob is closing on us and is not yet on top of us,
           back away along a direction that opens the gap, taking the current
           mob with us (it follows faster than we retreat, so it stays in
           reach).

THE ONE RULE THAT MAKES THIS SAFE. A burst begins and ends inside a single
call to tick(): key down, sleep, key up in a finally, settle, return. No key
is ever held when tick() returns.

That is not a style choice. combat.fight() runs

    if me.moving or self.ctm.moving:
        self.stop()

at the top of every steady-state tick, before the rotation, and Bot.stop() is
a click-to-move order plus wait_until_still(1.0). A key still held at that
point makes the fight loop fire a CTM order underneath our own keyboard move
and then block for a full second, because our key is what keeps us moving.
Holding a key across a loop iteration therefore costs about a second per
burst. Finishing inside one call also means every return, break and exception
in the 600-line fight loop is already safe: there is nothing held to leak.

THE ONE RULE THAT MAKES IT FREE. Footwork only spends time the fight loop has
already decided to waste. The caller passes free_for: how many seconds it
promises it has nothing to press. In combat.fight() that is the branch where
pick() returned None and the loop is about to time.sleep(0.15). Because
Combat.pick() returns None for the whole global cooldown, and because cast()
sleeps out a real cast internally and never sets _gcd_until for a cast longer
than the GCD, this single rule generalises across classes with no class table:
a rage-starved warrior is in that branch most of the fight and fidgets; a mage
is in it after an instant and not at all between Frostbolts, which is how a
real mage stands. It also makes "movement cancels a cast" impossible by
construction rather than by a checked condition.

THE RETURN CONTRACT. tick() returns "off" or "still" when it pressed nothing
and "<kind>:<keys>" when it pressed something. The caller can test for a burst
with `":" in r` and nothing else; no return value ever costs the caller time
without having spent time itself, so there is no zero-cost value that a
`continue` can spin on.

WHY WE TURN. A strafe translates without rotating, so each burst moves the
target off the centre of our view by dist/lateral radians, and nothing puts it
back: a mob that follows us walks straight at us, which leaves its bearing
FROM us exactly where it was, and Bot.face(tolerance=0.35) is a no-op below
0.35 rad by design. So the bearing error ratchets out to the budget and stays
there, at which point the cap in that direction is zero and the opposite one
is double - and the character is forced to alternate left, right, left, right
however the dice fell. Measured over 12 x 600 s melee fights that produced a
lag-1 autocorrelation of -0.91 in the EMITTED key sequence, which is the
metronome this module exists to avoid, while the intended directions out of
_draw_dir sat at a healthy +0.07. The fix is the one a player uses without
thinking: turn back onto the mob. A re-square is one short arrow-key press,
it never moves us (no terrain risk), it only ever reduces the error it is
correcting, and with the error back near zero both strafe directions are open
again and the dice decide. Bot.face already turns with exactly these keys at
exactly this rate (bot.py: kb.press("left" if err > 0 else "right",
hold=|err| / pi)), so this is the codebase's own turn primitive, not a new
one. Turning never cancels a cast in 1.12 - only translation does - but a
re-square is gated on free_for like every other burst anyway.

Facing is only ever turned TOWARD the target, by the measured error, so an
ability can never be lost to footwork pointing us away. An idle strafe is
capped so the target's bearing cannot swing past Bot.face()'s own 0.35 rad
tolerance, which keeps face() a no-op instead of something we fight. A DRAG
step is allowed 0.60 rad instead, because _do() snaps the facing back with one
arrow press inside the same tick, before the fight loop's next face() can see
it: capping a drag at 0.32 rad deletes the lateral candidates outright at a
mob's 2 yd follow distance, and those are the only candidates that gain
anything when the add is coming from behind a shoulder.

Nothing here reaches into combat.py or bot.py. Every game-facing operation is
an injected callable, so the whole module runs against stubs with a fake clock
and zero real elapsed time.
"""

from __future__ import annotations

import atexit
import math
import random
import time
import weakref
from dataclasses import dataclass, field, replace

# ---------------------------------------------------------------- the keys
# The keys bot.py already uses for these moves. No other key is ever touched;
# in particular never "w" (walking into the mob is the documented cause of a
# 25 s stall at 1.2 yd where the auto-attack click missed) and never "a"/"d"
# (those are also turn keys, but Bot.face uses the arrows and so do we, so
# there is exactly one turn primitive in the codebase).
STRAFE_LEFT = "q"
STRAFE_RIGHT = "e"
BACK = "s"
TURN_LEFT = "left"
TURN_RIGHT = "right"
MOVE_KEYS = (STRAFE_LEFT, STRAFE_RIGHT, BACK)
KEYS = MOVE_KEYS + (TURN_LEFT, TURN_RIGHT)

# ---------------------------------------------------------------- timing
# Lower bound: below ~0.1 s the client may not register a discernible move.
# Upper bound: 0.45 s x 7 yd/s = 3.1 yd of motion between safety re-checks.
# combat.backpedal's docstring records that a blind 2.2 s hold was already
# marginal and a blind 3.2 s hold put the mage in the water; 3.1 yd is under a
# third of what drowned it, and every burst re-validates terrain first.
MIN_HOLD = 0.10
MAX_HOLD = 0.45

# Bot.face(target, tolerance=0.35) returns immediately while the facing error
# is inside 0.35. A burst is only allowed if the CURRENT facing error plus the
# bearing swing the burst would add stays under this, so combat's own per-tick
# face() stays a no-op and footwork never provokes the 0.3 yd CTM nudge plus
# wait_until_still(1.0) that face() falls back to. This is the cap that
# actually binds in melee, and it is why melee footwork is naturally small and
# caster footwork naturally larger with no class constant.
BEARING_BUDGET = 0.32

# ---------------------------------------------------------------- the turn
# bot.py's own number: Bot.face presses an arrow for min(|err| / pi, 1.2) s,
# i.e. it believes the client turns at pi rad/s. We start from that and then
# measure it, so a different client or a different turn-speed setting is
# learned rather than assumed.
TURN_RATE = math.pi
# Aim at the whole error. This is not a free parameter: in melee the cone is
# 2 d tan(BEARING_BUDGET) yards wide and the shortest burst the client
# reliably registers is MIN_STRAFE_LAT, so at a mob's 2.5 yd follow distance a
# residual facing error of more than 0.05 rad already shuts one direction and
# forces the next burst. Aiming at 80% left exactly that residual and measured
# no better than not turning at all (-0.909 vs -0.911). Overshoot is bounded:
# turning toward the target by up to 2x the error leaves the error no larger
# than it was, and the rate is measured rather than trusted.
RESQUARE_FRAC = 1.0
# Below this there is nothing worth correcting, and the press would be under
# MIN_TURN_HOLD anyway.
RESQUARE_MIN = 0.08
MIN_TURN_HOLD = 0.04
MAX_TURN_HOLD = 0.25
# A DRAG step may spend more bearing than an idle strafe, because _do() snaps
# the facing back inside the same tick() (see _snap_back) while an idle strafe
# has to leave the error standing until the next re-square is due. 0.60 rad is
# what one MAX_TURN_HOLD press recovers with margin (0.25 s x pi = 0.785 rad),
# which matters because the snap-back aims at the target's PRE-STEP position -
# the mob moves while we do - so the correction is good to a few hundredths,
# not exact. It is not simply unbounded: at 0.81 rad (an uncapped 0.45 s
# strafe at a mob's 2.0 yd follow distance) the residual after the snap-back
# ran to 0.47 rad, past Bot.face's 0.35 tolerance, on 5% of steps.
DRAG_BEARING_MAX = 0.60
# A turn burst must rotate us at least this much to count as having arrived.
TURN_MIN_RAD = 0.02
TURN_FAIL_LIMIT = 3

# Post-burst distance to the target must stay <= melee_range - 1.0 (i.e. 5.0
# against combat.MELEE_RANGE = 6.0). Every warrior melee ability is gated on
# {"target_within": 6.0} in classes/warrior.py, so drifting past 6.0 does not
# merely cost a white swing, it makes Rend, Thunder Clap and Heroic Strike
# unusable. One yard of margin absorbs the mob's own motion during the burst.
MELEE_CAP_MARGIN = 1.0
# Post-burst distance <= profile.engage_range - 3.0 for a ranged profile.
# Purely lateral motion at 25 yd changes range by centimetres, so this cap
# almost never binds; it exists so a caster already at the edge of its range
# cannot strafe out of it.
RANGED_CAP_MARGIN = 3.0

# Diagonal composed speed in the 1.12 client is not documented and is not
# assumed. Endpoints are ALWAYS projected at SAFE_SPEED, an over-estimate, so
# the terrain we check is always at least as far as the terrain we reach.
# Gains are ALWAYS estimated at BACK_SPEED, an under-estimate derived
# conservatively from combat.backpedal's measured 1.1 s -> 5 yd and
# 2.2 s -> ~10 yd. Being wrong about diagonal speed can therefore make a burst
# smaller than planned, never less safe.
SAFE_SPEED = 7.5           # endpoint projection only
STRAFE_SPEED = 7.0         # ctm.ClickToMove.RUN_SPEED, the client's run speed
BACK_SPEED = 4.0           # gain estimation only

# Key-down seconds this fight / wall-clock seconds this fight. A hard bound
# that survives any bug in the distributions: whatever the random draws do, at
# most a quarter of the fight has a key down. (Idle only - the drag has its
# own explicit episode and per-fight budgets below, a safety behaviour is not
# throttled by a cosmetic budget, and a re-square does not translate us at all
# so it cannot walk us anywhere.)
DUTY_CAP = 0.25

# Footwork refuses below emergency_hp + 10 (i.e. 55% against combat's
# EMERGENCY_HP = 45.0). Below that the engine's emergency, bail and escape
# ladder owns movement, and a losing fight is not the time to fidget.
HP_MARGIN = 10.0

# Net displacement FOOTWORK ITSELF has caused this fight, past which all
# footwork stops until the next fight. The server evades a creature once its
# victim is past ThreatRadius (50 yd per combat.escape's comment) from where
# the fight started; 15 yd leaves a 3.3x margin and, more importantly, leaves
# the full escape distance available to combat.escape() afterwards.
#
# This is measured as the vector sum of the displacements our own bursts
# produced, NOT as distance from a position recorded at begin(). That is the
# whole point: combat.fight() calls begin() before self.approach(me, t, reach)
# has closed the gap, and real pulls are 20-60 yd (questing.SEARCH_RADIUS 60,
# bot.PULL_RADIUS 24, combat re-targets within 45), so an anchor taken at
# begin() is 20-60 yd behind where the fight is actually fought and latches
# off instantly. Measured: a fight pulled from 20 yd produced 0 bursts in 60 s
# against 6 for the identical fight pulled from 14. Bounding our own travel is
# also simply the correct quantity - it is what the comment above always said,
# and it is immune to combat chasing a fleeing mob mid-fight.
TRAVEL_CAP = 15.0

# No endpoint may land within 8 yd of an unengaged threat. This is the same
# "the move itself must not pass next to another mob" rule retreat_point
# already encodes with its `run < 12` penalty; 8 yd is the tighter number
# appropriate to a 1-3 yd move rather than a 60 yd run.
CLEAR_OF_OTHERS = 8.0

# |nav.ground_z(x, y) - me.z| must be <= 1.5. line_walkable does NOT do this:
# it tracks a poly's height range but never queries terrain height, so a
# strafe off a dock edge or a mine ledge can be nominally walkable. This is
# the one filter added beyond the primitives combat.py already has.
LEDGE_DZ = 1.5

# An add counts as closing only if its distance to us fell by >= 1.5 yd over a
# ~1.2 s ring buffer. Without this the bot backs away from any mob it merely
# stands near.
CLOSING_YD = 1.5
CLOSING_WINDOW = 1.2

# A mob is "inbound" - something to back away from - while it is further than
# melee_range + this, whether or not it has already aggroed. Inside it, it is
# on us: that is a 2v1, which combat's own join and bail logic owns, and
# footwork hands it back.
#
# The aggroed case is deliberately included, and it is the ordinary case: the
# user asked for "backpedal away with the mob in combat if another mob is
# getting closer", and a mob that is getting closer has usually noticed us.
# Combat.enemies() returns anything with `u.target == me.guid and u.in_combat`,
# so such a mob is already in ctx.active; excluding active units (or units
# whose target is us) therefore meant the behaviour could only ever fire at
# mobs that had NOT noticed us. Measured: identical scenario, add at 24 yd
# closing at 7 yd/s, 0 drags when the add targeted us and 6 when it did not.
ENGAGED_ADD_PAD = 4.0

# Add detection radius is add_radius(me.level) + 8, capped at 30. add_radius
# is combat.py's own level-scaled function (8 / 16 / 22 yd by level band), so
# the notice distance scales 1-60 with no level constant of our own.
NOTICE_PAD = 8.0
NOTICE_CAP = 30.0

# A drag is a sequence of short re-planned steps, never one long hold; 2.5 s
# per episode and 6 s per fight bound how much DPS the behaviour can cost.
DRAG_STEP_MAX = 0.45
DRAG_EPISODE_S = 2.5
DRAG_BUDGET_S = 6.0
DRAG_GAIN_MIN = 0.30       # a drag that buys nothing is lost DPS
DRAG_REARM_S = 2.0         # a gap this long ends the episode; also the
                           # stand-down after an episode is spent

# After key-up, poll the raw move-flags word until it clears, up to 0.4 s, so
# the fight loop's own moving-check normally finds us already still.
SETTLE_MAX = 0.4

# A burst must displace us at least 0.5 yd to teach the strafe sign; three
# consecutive bursts under that means the keys are not reaching the client and
# footwork disables itself for the session. This machine has a recorded
# history of OEM-key presses never arriving (see the Danish-scancode notes).
CALIB_MIN_YD = 0.5
CALIB_FAIL_LIMIT = 3

# Bot.threats() walks the whole object manager and does a worlddb lookup, so
# it must not be called at the loop rate.
THREAT_PERIOD = 0.5        # 2 Hz

# The impatient shuffle. A burst has a FLURRY_CHANCE of being the start of a
# 2-4 tap shuffle whose taps are FLURRY_GAP apart, instead of a lone slide.
# The gap is the number that carries the look: much under 0.15 s and the taps
# merge into a vibration, much over 0.5 s and they read as separate decisions
# rather than one impatient shuffle.
FLURRY_CHANCE = 0.55
FLURRY_TAPS = (1, 4)       # rng.randrange -> 1..3 further taps, i.e. 2-4 total
FLURRY_GAP = (0.18, 0.45)  # seconds between taps within one shuffle

MIN_ROOM = 0.4             # less lateral room than this is not worth a press
# The shortest burst the client reliably registers, in yards. A direction with
# less room than this than is not a direction we can go, whatever MIN_ROOM
# says - it is the number the re-square has to keep both sides above.
MIN_STRAFE_LAT = STRAFE_SPEED * MIN_HOLD
FAULT_STANDDOWN = 5.0      # after any injected callable raises
BLOCK_COOLDOWN = 1.5       # after every direction failed the filter stack
STEERED_ACTIONS = (4, 5, 6, 7)   # ctm.STEERED_ACTIONS, inlined: no import


def human_gap(rng, fidget: float) -> float:
    """Seconds until the next idle event.

    Lognormal, not uniform and certainly not constant: measured over 20k draws
    this has mean 2.62 s and coefficient of variation 0.64, where a fixed
    shuffle has a CV of exactly 0. The CV is the whole point - see the module
    docstring and test 36, which runs a 1 Hz alternator through the same
    assertions and fails all of them.
    """
    v = rng.lognormvariate(math.log(2.2), 0.6)
    return min(max(v, 0.6), 12.0) / max(fidget, 0.05)


def human_hold(rng) -> float:
    """How long one burst holds its key. Mean 0.238 s, CV 0.41 over 20k draws."""
    v = rng.lognormvariate(math.log(0.22), 0.45)
    return min(max(v, MIN_HOLD), MAX_HOLD)


def turn_gap(rng) -> float:
    """Seconds until the next chance to re-square onto the target.

    Faster than the strafe cadence on purpose: a re-square that arrives after
    the next strafe leaves that strafe forced into one direction, which is the
    metronome. Still a distribution, not a period - the re-square is itself a
    visible motion.
    """
    v = rng.lognormvariate(math.log(0.9), 0.5)
    return min(max(v, 0.3), 4.0)


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def _bearing_err(me, t) -> float:
    """Signed radians we would have to turn to face `t`.

    Computed here rather than through game.Unit.facing_error so that this
    module imports nothing from the game side (test 39).
    """
    return _wrap(math.atan2(t.y - me.y, t.x - me.x) - me.facing)


def _hdist(a, b) -> float:
    """Horizontal distance. The motion is horizontal, so the caps are too."""
    return math.hypot(a.x - b.x, a.y - b.y)


def _dist3(a, b) -> float:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))


def _is_not_in_world(exc: BaseException) -> bool:
    """keys.NotInWorld, recognised by name so keys.py is never imported."""
    return type(exc).__name__ == "NotInWorld"


@dataclass
class FootCtx:
    """Everything tick() is allowed to know, all of it already read by the
    caller in the same loop iteration - except `me`, which tick() re-reads for
    itself (see Footwork.tick) because the caller's copy is a snapshot taken
    before stop() and face() ran."""
    me: object                  # game.Player, already read by the caller
    target: object | None       # game.Unit, the primary
    active: list = field(default_factory=list)   # uncontrolled enemies
    ranged: bool = False        # profile.ranged
    engage_range: float = 5.0   # profile.engage_range
    melee_range: float = 6.0    # combat.MELEE_RANGE
    emergency_hp: float = 45.0  # combat.EMERGENCY_HP
    free_for: float = 0.0       # seconds the caller promises it will not press
    casting: bool = False       # UNIT_CASTING_SPELL != 0, read by the caller
    escaped: bool = False       # caller's `escaped or bail_pending`


_LIVE: "weakref.WeakSet" = weakref.WeakSet()


def release_all() -> None:
    """Up every key every live Footwork holds. Registered with atexit: a
    character strafing into the loch because the process died mid-burst is the
    one failure this module must not have."""
    for fw in list(_LIVE):
        try:
            fw.release()
        except Exception:
            pass


atexit.register(release_all)


class Footwork:
    def __init__(self, kb, *,
                 line_walkable,   # (me, (x,y,z), step=float) -> bool   Combat.line_walkable
                 ground_z,        # (x, y) -> float | None              navmesh.ground_z
                 move_flags,      # () -> int    one u32 read of UNIT_MOVE_FLAGS
                 threats,         # () -> list[Unit]                    Bot.threats
                 add_radius,      # (level) -> float                    combat.add_radius
                 ctm_action,      # () -> int    one u32 read of CTM_ACTION
                 player,          # () -> Player  a fresh read, every tick
                 log=None, rng=None,
                 clock=time.monotonic, sleep=time.sleep,
                 enabled=True):
        self.kb = kb
        self.line_walkable = line_walkable
        self.ground_z = ground_z
        self.move_flags = move_flags
        self.threats = threats
        self.add_radius = add_radius
        self.ctm_action = ctm_action
        self.player = player
        self.log = log
        self.rng = rng if rng is not None else random.Random()
        self.clock = clock
        self.sleep = sleep
        # No navmesh means no ledge guard and no walkability test, and a burst
        # we cannot validate is a burst we do not take: a disabled footwork
        # costs exactly what the bot does today.
        self._enabled = bool(enabled and line_walkable is not None and ground_z is not None)
        self._dead = False                 # session-fatal (keys never arrive)
        self._disabled_until = 0.0

        self._held: set[str] = set()
        self._sign = 0                     # +1 once "q" is known to be left
        self._strafe_speed = STRAFE_SPEED  # measured, for logging and gains only
        self._calib_fails = 0
        self._turn_rate = TURN_RATE        # measured
        self._turn_fails = 0
        self._turn_dead = False            # arrows do not turn us: strafe only

        self._threats_list: list = []
        self._threats_t = None
        self._hist: dict[int, list] = {}

        self._counts = {"idle": 0, "drag": 0, "turn": 0, "blocked": 0}
        self._begin()
        _LIVE.add(self)

    # ------------------------------------------------------------ lifecycle
    def _begin(self) -> None:
        now = self.clock()
        self._fight_t0 = now
        # Net displacement our own bursts have produced this fight. NOT a
        # position: see TRAVEL_CAP for why an anchor taken at begin() is the
        # wrong quantity and was measurably fatal.
        self._travel = (0.0, 0.0)
        self._travel_done = False
        self._idle_key_down = 0.0
        self._turn_key_down = 0.0
        self._drag_key_down = 0.0
        self._episode_key_down = 0.0
        self._drag_guid = None
        self._last_drag_t = -1e9
        self._episode_cooldown = 0.0
        self._blocked_until = 0.0
        # Some fights the character barely moves. Drawn per fight so that
        # "how twitchy is this person today" is not a constant either.
        self._fidget = min(max(self.rng.betavariate(2, 3) * 2.0, 0.35), 1.6)
        self._last_dir = 1 if self.rng.random() < 0.5 else -1
        self._flurry = 0
        self._next_event = now + human_gap(self.rng, self._fidget)
        self._next_turn = now + turn_gap(self.rng)
        self._hist.clear()

    def begin(self, me=None) -> None:
        """Called once per fight, from combat.fight() right after t_start.

        `me` is accepted and ignored: combat.fight() has it to hand at that
        point, but it is the PRE-APPROACH read and recording it as an anchor
        is exactly the bug TRAVEL_CAP documents.
        """
        self.release()
        self._begin()

    def end(self) -> None:
        self.release()
        self._begin()

    # ------------------------------------------------------------ keys
    def release(self) -> None:
        """Up every key we hold. Idempotent, and always legal: keys.py:100-101
        deliberately lets GuardedKeyboard.up bypass the in-world check
        ("always allowed: never leave a key held"), so this is safe at the
        character screen and mid-disconnect."""
        for k in list(self._held):
            self._held.discard(k)          # discard first: a raising up()
            try:                           # must not leave us double-upping
                self.kb.up(k)
            except Exception:
                pass

    # ------------------------------------------------------------ the loop
    def tick(self, ctx: FootCtx) -> str:
        """Returns "off", "still", or "<kind>:<keys>" when a burst went out.

        A return containing ":" is the only one that spent time; every other
        return pressed nothing AND took no measurable time, so a caller that
        does `if ":" in r: continue` can never build a sleepless hot loop out
        of it. There is deliberately no third zero-cost value (the old
        "blocked" was one, and under the documented call site it spun the
        fight loop at full speed with the rotation skipped - measured at
        ~40,000 iterations in 0.0 s of simulated time with an add inbound).
        """
        if not self._enabled or self._dead:
            return "off"
        now = self.clock()
        if now < self._disabled_until:
            return "off"
        try:
            # `ctx.me` is the fight loop's top-of-tick snapshot (game.Unit is
            # a struct.unpack, not a live view). Between that read and here,
            # combat.fight() has run self.stop() - a CTM order plus up to 1.0 s
            # of wait_until_still, during which we were still travelling - and
            # self.face(t), which turns us with a CTM nudge and up to two 1.2 s
            # arrow presses. Planning a strafe off that facing checks terrain
            # up to 90 degrees away from the ground we actually enter, and
            # checks the ledge from an origin yards behind where we stand.
            # So: one extra u32-cluster read per tick, and every piece of
            # geometry below is current.
            ctx = self._refresh(ctx)
            add = self.approaching_add(ctx)
            if self._stand_down_if_engaged(ctx, add, now):
                return "off"
            if add is not None and now >= self._blocked_until and self._drag_allowed(ctx):
                plan = self.plan_drag(ctx, add)
                if plan is None:
                    # Every direction failed the filter stack. Do NOT hand the
                    # caller a free return: sit out the cooldown as "still" so
                    # the fight loop sleeps and, more importantly, presses its
                    # rotation instead of standing there doing nothing with a
                    # mob inbound.
                    self._counts["blocked"] += 1
                    self._blocked_until = now + BLOCK_COOLDOWN
                    return "still"
                return self._do(ctx, plan, "drag")
            if not self._move_gates(ctx):
                return "off"
            # Re-square first and on its own clock: a turn is cheap, moves us
            # nowhere, and is what keeps the strafe sequence from degenerating
            # into a left-right metronome. See "WHY WE TURN".
            # ... on its own clock, or at once when the geometry has shut one
            # side and a strafe is due: squaring up before stepping the other
            # way is what a player does, and it is what stops the shut side
            # from dictating the direction.
            if now >= self._next_turn or (now >= self._next_event
                                          and self._one_sided(ctx)):
                self._next_turn = now + turn_gap(self.rng)
                plan = self.plan_turn(ctx)
                if plan is not None:
                    return self._do(ctx, plan, "turn")
            if not self._room_gates(ctx):
                return "off"
            if now < self._next_event or now < self._blocked_until:
                return "still"
            self._schedule_next()          # draw the next gap regardless
            plan = self.plan_idle(ctx)
            if plan is None:
                return "still"
            return self._do(ctx, plan, "idle")
        except BaseException as e:          # noqa: BLE001 - see below
            # Footwork failing must never kill a fight, and must never leave a
            # key down while it fails. NotInWorld is the expected one (the
            # client dropped to the character screen mid-fight); anything else
            # is a bug in an injected callable and gets the same treatment.
            self.release()
            self._disabled_until = self.clock() + FAULT_STANDDOWN
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            self._say(f"    footwork off for {FAULT_STANDDOWN:.0f}s: "
                      f"{'not in world' if _is_not_in_world(e) else repr(e)}")
            return "off"

    def _refresh(self, ctx: FootCtx) -> FootCtx:
        """Replace the caller's stale player snapshot with a fresh read.

        A raise here is a real one (NotInWorld, a moved object manager) and
        goes to tick()'s handler, which stands footwork down - the correct
        answer when we cannot see where we are.
        """
        me = self.player()
        return ctx if me is None else replace(ctx, me=me)

    def _schedule_next(self) -> None:
        """When to fidget again.

        Impatience is a RHYTHM, not a rate. A player waiting on a swing timer
        does not slide one yard every few seconds - they shuffle: two or three
        quick taps that end up roughly back where they started, then a pause
        while they watch the mob, then another shuffle. A single isolated
        burst per free window reads as drifting, which is what the first live
        run looked like.

        So a burst either continues the current shuffle (a short FLURRY_GAP,
        and _draw_dir is biased to come back the other way) or ends it and
        draws the ordinary long gap. Nothing else changes: every tap in a
        flurry is still an ordinary burst that re-validates terrain, is still
        bounded by the bearing cap, and still counts against the duty cap -
        which is what keeps a flurry from becoming a vibration.
        """
        if self._flurry > 0:
            self._flurry -= 1
            self._next_event = self.clock() + self.rng.uniform(*FLURRY_GAP)
            return
        self._flurry = (self.rng.randrange(*FLURRY_TAPS)
                        if self.rng.random() < FLURRY_CHANCE else 0)
        self._next_event = self.clock() + human_gap(self.rng, self._fidget)

    # ------------------------------------------------------------ gates
    def _duty(self) -> float:
        el = self.clock() - self._fight_t0
        return 0.0 if el <= 0 else self._idle_key_down / el

    def _travel_ok(self, ctx) -> bool:
        if self._travel_done:
            return False
        if math.hypot(*self._travel) >= TRAVEL_CAP:
            self._travel_done = True
            self._say(f"    footwork has moved us {TRAVEL_CAP:.0f} yd this fight "
                      "- no more until the next one")
            return False
        return True

    def _hp_ok(self, ctx) -> bool:
        return (not ctx.escaped) and ctx.me.hp_pct >= ctx.emergency_hp + HP_MARGIN

    def _move_gates(self, ctx) -> bool:
        """May we press ANY movement key right now? (Turn or strafe.)"""
        if ctx.target is None or getattr(ctx.target, "dead", False):
            return False
        if ctx.casting:
            return False
        if self.ctm_action() in STEERED_ACTIONS:
            # The click-to-move steering thread rewrites UNIT_FACING at 20 Hz
            # while CTM_ACTION is one of those. Bot.walk_to's own loop proves
            # the client clears the action when an order arrives, so after the
            # fight loop's self.stop() the thread is already inert - but if
            # some other code has an order in flight we simply do not move.
            # Zero writes to any CTM global, ever.
            return False
        if self.move_flags() != 0:
            return False
        if not self._hp_ok(ctx):
            return False
        if len(ctx.active) > 2:
            return False
        if not self._travel_ok(ctx):
            return False
        return True

    def _room_gates(self, ctx) -> bool:
        """May we STRAFE right now? (Everything a turn does not care about.)"""
        if self._duty() >= DUTY_CAP:
            return False
        if self.caps(ctx) < MIN_ROOM:
            return False
        return True

    def _idle_gates(self, ctx) -> bool:
        return self._move_gates(ctx) and self._room_gates(ctx)

    def _one_sided(self, ctx) -> bool:
        """One strafe direction has room for a real burst and the other does
        not - i.e. the dice are about to be overruled by the geometry."""
        a, b = self._lat_cap(ctx, 1), self._lat_cap(ctx, -1)
        return min(a, b) < MIN_STRAFE_LAT <= max(a, b)

    # ------------------------------------------------------------ geometry
    def _bearing_cap(self, ctx, cand: int, budget: float = BEARING_BUDGET) -> float:
        """Lateral yards this direction may spend before the target's bearing
        leaves BEARING_BUDGET (+1 = to the left of our facing).

        Per direction, not per position, and that is not a refinement: with a
        single symmetric cap the first burst pushes the bearing error out to
        the budget and every later burst is then refused in BOTH directions
        for the rest of the fight - the character moves once and freezes. In
        the offline run that was 1 burst per 600 s.

        The target sits at body coordinates (d cos e, d sin e). A strafe of L
        to our left moves it to (d cos e, d sin e - L), so the direction that
        REDUCES the error may swing the whole cone across and the direction
        that increases it may only spend what is left:

            L <= d (cand sin e + cos e tan BEARING_BUDGET)
        """
        t = ctx.target
        if t is None:
            return 0.0
        d = _hdist(ctx.me, t)
        e = _bearing_err(ctx.me, t)
        return max(0.0, d * (cand * math.sin(e) + math.cos(e) * math.tan(budget)))

    def _lat_cap(self, ctx, cand: int) -> float:
        """The bearing cap, further limited by the range band and MAX_HOLD.

        Two caps and a clock, and still no class or level value anywhere in
        the sum. Squared up on the target a melee profile at 3.5 yd gets
        1.16 yd (0.166 s) and at 5.0 yd and beyond exactly nothing - it is at
        the edge of its swing band and does not move. A ranged profile at
        15 yd is bound only by MAX_HOLD, 3.15 yd. Melee footwork is small and
        caster footwork larger, and neither number is written down: both fall
        out of the geometry.
        """
        bearing = self._bearing_cap(ctx, cand)
        if bearing <= 0.0:
            return 0.0                     # target behind us, or the cone is
                                           # already spent on this side
        d = _hdist(ctx.me, ctx.target)
        cap_r = (ctx.engage_range - RANGED_CAP_MARGIN) if ctx.ranged \
            else (ctx.melee_range - MELEE_CAP_MARGIN)
        if ctx.ranged and d < cap_r - 1.0:
            rng_cap = float("inf")         # lateral motion at 25 yd changes
        elif d < cap_r:                    # range by centimetres
            rng_cap = math.sqrt(max(cap_r * cap_r - d * d, 0.0))
        else:
            rng_cap = 0.0
        return max(0.0, min(bearing, rng_cap, STRAFE_SPEED * MAX_HOLD))

    def caps(self, ctx) -> float:
        """Maximum lateral yards allowed right now, in the better direction."""
        return max(self._lat_cap(ctx, 1), self._lat_cap(ctx, -1))

    def safe_endpoint(self, me, ang: float, dist: float, others: list) -> bool:
        """The filter stack, cheapest first."""
        x = me.x + dist * math.cos(ang)
        y = me.y + dist * math.sin(ang)
        for u in others:
            if math.dist((x, y), (u.x, u.y)) < CLEAR_OF_OTHERS:
                return False
        gz = self.ground_z(x, y)
        if gz is None or abs(gz - me.z) > LEDGE_DZ:
            return False                   # the ledge guard; see LEDGE_DZ
        # step 1.5 and not the default 4.0: a 1.2 yd burst sampled every 4 yd
        # checks a single point. This is the primitive combat already trusts
        # for 60 yd escapes, used here over 1-3 yd, and it is what rejects
        # NAV_STEEP and deep NAV_WATER (a ford keeps NAV_GROUND and is fine).
        return bool(self.line_walkable(me, (x, y, gz), step=1.5))

    def _others(self, ctx, exclude=()) -> list:
        engaged = {getattr(u, "guid", None) for u in ctx.active}
        if ctx.target is not None:
            engaged.add(ctx.target.guid)
        engaged.update(exclude)
        return [u for u in self._threats_cached(ctx)
                if u.guid not in engaged and not getattr(u, "dead", False)]

    # ------------------------------------------------------------ the turn
    def plan_turn(self, ctx):
        """One short arrow press back onto the target, or None.

        Only ever toward the target, and only ever by a fraction of the
        measured error, so this cannot point us away from what we are hitting.
        """
        if self._turn_dead or ctx.target is None:
            return None
        err = _bearing_err(ctx.me, ctx.target)
        if abs(err) < RESQUARE_MIN:
            return None
        hold = abs(err) * RESQUARE_FRAC / max(self._turn_rate, 0.5)
        hold = min(hold, MAX_TURN_HOLD, ctx.free_for - 0.05)
        if hold < MIN_TURN_HOLD:
            return None
        return ((TURN_LEFT if err > 0 else TURN_RIGHT,), hold)

    # ------------------------------------------------------------ idle
    def _draw_dir(self) -> int:
        """3-state Markov chain over {same 0.45, opposite 0.30, none 0.25}.

        Never strict alternation: the signed sequence has a measured lag-1
        autocorrelation of about +0.15, where a left-right shuffle has exactly
        -1.0. A metronome is a positive fingerprint; standing still is merely
        an absence of one, so a periodic shuffle would be strictly worse than
        today's behaviour.

        What comes out of here is only worth anything if the geometry lets it
        through - see "WHY WE TURN" in the module docstring, and test 34,
        which measures the EMITTED keys rather than this draw.
        """
        if self._flurry > 0:
            # mid-shuffle: mostly come back the other way, so a flurry ends up
            # near where it started rather than walking sideways. Not strict
            # alternation - a real shuffle double-taps one way often enough.
            return -self._last_dir if self.rng.random() < 0.72 else self._last_dir
        u = self.rng.random()
        if u < 0.45:
            return self._last_dir
        if u < 0.75:
            self._last_dir = -self._last_dir
            return self._last_dir
        return 0

    def plan_idle(self, ctx):
        if self.caps(ctx) < MIN_ROOM:
            return None
        draw = human_hold(self.rng)
        # the duty accumulator is a hard bound that survives any bug in the
        # distributions above
        el = max(self.clock() - self._fight_t0, 0.0)
        room = DUTY_CAP * el - self._idle_key_down
        d = self._draw_dir()
        if d == 0:
            return None                    # standing still is a legal answer
        others = self._others(ctx)
        tried = False
        for cand in (d, -d):
            cap = self._lat_cap(ctx, cand)
            if self._sign == 0:
                # Until the strafe sign is measured we do not know which world
                # direction "q" produces, so a burst may only go out if it is
                # legal BOTH ways: a mirrored sign cannot then put us anywhere
                # the intended direction would not also have been safe.
                cap = min(cap, self._lat_cap(ctx, -cand))
            if cap < MIN_ROOM:
                continue
            hold = min(draw, cap / STRAFE_SPEED, MAX_HOLD, ctx.free_for - 0.15, room)
            if hold < MIN_HOLD:
                continue
            tried = True
            reach = SAFE_SPEED * hold
            ang = ctx.me.facing + cand * math.pi / 2
            if not self.safe_endpoint(ctx.me, ang, reach, others):
                continue
            if self._sign == 0 and not self.safe_endpoint(
                    ctx.me, ctx.me.facing - cand * math.pi / 2, reach, others):
                continue
            self._last_dir = cand
            sign = self._sign or 1
            key = STRAFE_LEFT if cand * sign > 0 else STRAFE_RIGHT
            return ((key,), hold)
        if tried:
            # terrain, not geometry or the clock: do not re-plan every tick
            self._blocked_until = self.clock() + BLOCK_COOLDOWN
        return None

    # ------------------------------------------------------------ adds
    def _threats_cached(self, ctx) -> list:
        now = self.clock()
        if self._threats_t is not None and now - self._threats_t < THREAT_PERIOD:
            return self._threats_list
        self._threats_t = now
        self._threats_list = list(self.threats())
        me = ctx.me
        for u in self._threats_list:
            h = self._hist.setdefault(u.guid, [])
            h.append((now, math.dist((me.x, me.y, me.z), (u.x, u.y, u.z))))
            while h and now - h[0][0] > CLOSING_WINDOW:
                h.pop(0)
        for guid in [g for g, h in self._hist.items()
                     if not h or now - h[-1][0] > 3 * CLOSING_WINDOW]:
            self._hist.pop(guid, None)
        return self._threats_list

    def approaching_add(self, ctx):
        """The nearest mob that is closing on us and is not yet on top of us.

        Deliberately includes mobs that have already aggroed - see
        ENGAGED_ADD_PAD. What disqualifies a mob here is being CLOSE, not
        having noticed us.
        """
        me = ctx.me
        if me is None or ctx.target is None:
            return None
        pool = self._threats_cached(ctx)
        notice = min(self.add_radius(me.level) + NOTICE_PAD, NOTICE_CAP)
        reach = ctx.melee_range + ENGAGED_ADD_PAD
        best, best_d = None, 1e9
        for u in pool:
            if u.guid == ctx.target.guid or getattr(u, "dead", False):
                continue
            d = math.dist((me.x, me.y, me.z), (u.x, u.y, u.z))
            if d > notice or d <= reach:
                continue          # already on us: combat's join/bail owns it
            h = self._hist.get(u.guid) or []
            if len(h) < 2:
                continue
            if max(s for _, s in h) - h[-1][1] < CLOSING_YD:
                continue          # standing there is not approaching
            if d < best_d:
                best, best_d = u, d
        return best

    def _unit(self, ctx, guid):
        for u in list(ctx.active) + self._threats_cached(ctx):
            if getattr(u, "guid", None) == guid:
                return u
        return None

    def _stand_down_if_engaged(self, ctx, add, now) -> bool:
        """A drag whose add has reached us is not a drag any more: it is a
        2v1, which combat's own join and bail logic already owns. Hand it back
        and stand down rather than fidget through it."""
        if self._drag_guid is None:
            return False
        if add is not None and add.guid == self._drag_guid:
            return False
        u = self._unit(ctx, self._drag_guid)
        if u is None or ctx.me is None:
            return False
        if _dist3(ctx.me, u) > ctx.melee_range + ENGAGED_ADD_PAD:
            return False
        self._say(f"    add {self._drag_guid:#x} reached us mid-drag - footwork stands down")
        self._drag_guid = None
        self._episode_key_down = 0.0
        self._disabled_until = now + DRAG_REARM_S
        return True

    def _drag_allowed(self, ctx) -> bool:
        if self._sign == 0:
            return False                   # a mirrored drag is the wrong way
        if ctx.casting or not self._hp_ok(ctx):
            return False
        if not self._travel_ok(ctx):
            return False
        # Anything engaged with us other than the primary is fine ONLY while
        # it is still inbound; the moment one is on us this is a 2v1.
        reach = ctx.melee_range + ENGAGED_ADD_PAD
        seen, extra = set(), []
        for u in ctx.active:
            g = getattr(u, "guid", None)
            if ctx.target is not None and g == ctx.target.guid:
                continue
            if g in seen:
                continue
            seen.add(g)
            extra.append(u)
        if len(extra) > 1:
            return False                   # two adds: combat's bail owns it
        if any(_dist3(ctx.me, u) <= reach for u in extra):
            return False
        if self._drag_key_down >= DRAG_BUDGET_S - MIN_HOLD:
            return False
        now = self.clock()
        if now < self._episode_cooldown:
            return False
        if self._episode_key_down >= DRAG_EPISODE_S - MIN_HOLD:
            self._episode_cooldown = now + DRAG_REARM_S
            self._episode_key_down = 0.0
            self._drag_guid = None
            return False
        if self.ctm_action() in STEERED_ACTIONS:
            return False
        if self.move_flags() != 0:
            return False
        return True

    # With facing locked on the target, keyboard motion has exactly five
    # reachable body-frame directions. Forward is deliberately unreachable:
    # footwork never presses "w", so a drag can never turn into a chase and
    # can never walk us onto the mob.
    _DRAG_CANDIDATES = (
        ((BACK,), math.pi, BACK_SPEED),
        ((BACK, STRAFE_LEFT), 3 * math.pi / 4, BACK_SPEED),
        ((BACK, STRAFE_RIGHT), -3 * math.pi / 4, BACK_SPEED),
        ((STRAFE_LEFT,), math.pi / 2, STRAFE_SPEED),
        ((STRAFE_RIGHT,), -math.pi / 2, STRAFE_SPEED),
    )

    def plan_drag(self, ctx, add):
        if self._sign == 0:
            return None      # a mirrored sign mirrors every drag: until the
                             # strafe direction is measured there is no drag
        now = self.clock()
        if add.guid != self._drag_guid or now - self._last_drag_t > DRAG_REARM_S:
            self._drag_guid = add.guid
            self._episode_key_down = 0.0
        hold0 = min(DRAG_STEP_MAX,
                    DRAG_EPISODE_S - self._episode_key_down,
                    DRAG_BUDGET_S - self._drag_key_down)
        if hold0 < MIN_HOLD:
            return None
        me, t = ctx.me, ctx.target
        away = math.atan2(me.y - add.y, me.x - add.x)
        others = self._others(ctx, exclude=(add.guid,))
        keep_max = (ctx.engage_range - RANGED_CAP_MARGIN) if ctx.ranged \
            else (ctx.melee_range + 2.0)
        # Melee deliberately accepts overshoot to melee_range + 2: the mob
        # follows at ~7-8 yd/s against our 4.0-7.0, which is exactly why
        # backing up DRAGS it rather than dropping it, and combat's own
        # approach re-closes anything that slips.
        best, best_score = None, -1e9
        for keys, offset, speed in self._DRAG_CANDIDATES:
            ang = me.facing + self._sign * offset
            gain = math.cos(_wrap(ang - away))
            if gain <= DRAG_GAIN_MIN:
                continue
            # An uncapped pure strafe swings the target's bearing to 0.81
            # rad, 2.3x Bot.face's 0.35 tolerance, so the next tick's face()
            # fires a CTM order and blocks for wait_until_still(1.0) - about a
            # second of standing still per flank step, while the add closes at
            # run speed. The answer is NOT to shorten the step: at a mob's
            # 2.0 yd follow distance the bearing cone is 0.66 yd, i.e. under
            # MIN_HOLD's 0.7, so capping deletes the lateral candidates
            # outright - and on 5 of 16 add bearings (the ones behind a
            # shoulder) a pure strafe is the ONLY candidate that gains
            # anything. Measured: capping took the drag from 45/48 scenarios
            # that opened the gap to 33/48. So the step runs at full length
            # and _do() snaps the facing back with the same arrow-key
            # re-square the idle path uses, inside the same tick(), before the
            # fight loop's next face() ever sees the error. Only when the
            # arrows are known not to work (self._turn_dead) is there nothing
            # to snap back with, and then the cap is the honest answer.
            hold = hold0
            lat = math.sin(self._sign * offset)
            if abs(lat) > 1e-9:
                cap = self._bearing_cap(
                    ctx, 1 if lat > 0 else -1,
                    BEARING_BUDGET if self._turn_dead else DRAG_BEARING_MAX)
                hold = min(hold, cap / (speed * abs(lat)))
            if hold < MIN_HOLD:
                continue
            if not self.safe_endpoint(me, ang, SAFE_SPEED * hold, others):
                continue
            ex = me.x + speed * hold * math.cos(ang)
            ey = me.y + speed * hold * math.sin(ang)
            if t is not None and math.dist((ex, ey), (t.x, t.y)) > keep_max:
                continue
            room = min((math.dist((ex, ey), (u.x, u.y)) for u in others), default=20.0)
            score = 2.0 * gain + 0.08 * min(room, 20.0)
            if score > best_score:
                best, best_score = (keys, hold), score
        return best

    # ------------------------------------------------------------ the burst
    def _burst(self, keys, hold: float) -> float:
        """Press, sleep, release in a finally. Returns the real key-down time.

        Nothing above this line may leave a key held: the release is in the
        finally, and _held is only ever added to AFTER the press went out
        (every real raise from Keyboard.down happens before PostMessage).
        """
        t0 = self.clock()
        try:
            for k in keys:
                self.kb.down(k)
                self._held.add(k)
            self.sleep(hold)
        finally:
            held_for = max(self.clock() - t0, 0.0)
            self.release()
        return held_for

    def _do(self, ctx, plan, kind: str) -> str:
        keys, hold = plan
        me = ctx.me
        m0 = (me.x, me.y, me.facing)
        held_for = self._burst(keys, hold)
        self._settle()
        if kind == "drag":
            self._drag_key_down += held_for
            self._episode_key_down += held_for
            self._last_drag_t = self.clock()
        elif kind == "turn":
            self._turn_key_down += held_for
        else:
            self._idle_key_down += held_for
        self._counts[kind] += 1
        self._note(m0, keys, held_for, kind)
        tail = self._snap_back(ctx) if kind == "drag" else ""
        return f"{kind}:{'+'.join(keys)}{tail}"

    def _snap_back(self, ctx) -> str:
        """Immediately after a drag step, put the target back in the front arc.

        A drag step is allowed to spend more bearing than an idle strafe (see
        plan_drag), so this is what keeps the promise the budget makes: the
        fight loop's own face() runs at the top of the NEXT iteration, and it
        must still find the error inside its 0.35 tolerance or it pays a CTM
        nudge plus wait_until_still(1.0). Turning translates us nowhere, so
        there is no terrain to re-check.
        """
        if self._turn_dead or ctx.target is None:
            return ""
        try:
            p = self.player()
        except Exception:
            return ""
        if p is None:
            return ""
        err = _bearing_err(p, ctx.target)
        if abs(err) <= BEARING_BUDGET:
            return ""
        hold = min(abs(err) * RESQUARE_FRAC / max(self._turn_rate, 0.5),
                   MAX_TURN_HOLD)
        if hold < MIN_TURN_HOLD:
            return ""
        key = TURN_LEFT if err > 0 else TURN_RIGHT
        held_for = self._burst((key,), hold)
        self._settle()
        self._turn_key_down += held_for
        self._counts["turn"] += 1
        self._note((p.x, p.y, p.facing), (key,), held_for, "turn")
        return "+" + key

    def _settle(self) -> None:
        # The deadline is computed once and the last poll is shortened to land
        # on it. Re-testing `clock() - t0 < SETTLE_MAX` and then sleeping a
        # fixed 0.05 lets accumulated float error buy one extra poll, and the
        # offline harness caught exactly that: a 0.45 s burst blocked tick()
        # for 0.90 s against a documented 0.85 s worst case.
        end = self.clock() + SETTLE_MAX
        while True:
            try:
                if self.move_flags() == 0:
                    return
            except Exception:
                return
            left = end - self.clock()
            if left <= 1e-6:
                return         # not "<= 0": a sleep of a femtosecond may not
                               # advance an injected clock at all, and this
                               # loop then never ends
            self.sleep(min(0.05, left))

    def _note(self, m0, keys, hold: float, kind: str) -> None:
        """Measure what the burst actually did: the strafe sign, the speed,
        the turn rate, and whether the keys reach the client at all."""
        try:
            p = self.player()
        except Exception:
            return
        if p is None:
            return
        if kind == "turn":
            self._note_turn(m0[2], p.facing, keys[0], hold)
            return
        dx, dy, f0 = p.x - m0[0], p.y - m0[1], m0[2]
        # Our own contribution to where we stand, which is the only travel
        # TRAVEL_CAP is entitled to bound.
        self._travel = (self._travel[0] + dx, self._travel[1] + dy)
        moved = math.hypot(dx, dy)
        if moved < CALIB_MIN_YD:
            self._calib_fails += 1
            if self._calib_fails >= CALIB_FAIL_LIMIT:
                self._dead = True
                self.release()
                self._say(f"    {CALIB_FAIL_LIMIT} footwork bursts moved us nothing - "
                          "the keys are not reaching the client; footwork off for the session")
            return
        self._calib_fails = 0
        if len(keys) != 1 or keys[0] not in (STRAFE_LEFT, STRAFE_RIGHT):
            return
        lat = -dx * math.sin(f0) + dy * math.cos(f0)
        s = 1 if lat > 0 else -1
        if keys[0] == STRAFE_RIGHT:
            s = -s
        if self._sign != s:
            self._say(f"    strafe sign measured: \"{STRAFE_LEFT}\" moves "
                      f"{'left' if s > 0 else 'right'} ({moved:.1f} yd in {hold:.2f}s)")
        self._sign = s
        if hold > 0:
            self._strafe_speed = 0.7 * self._strafe_speed + 0.3 * (moved / hold)

    def _note_turn(self, f0: float, f1: float, key: str, hold: float) -> None:
        """Learn the client's turn rate, and give up on turning (only on
        turning) if the arrows do nothing or go the wrong way."""
        dz = _wrap(f1 - f0)
        want = 1 if key == TURN_LEFT else -1
        if abs(dz) < TURN_MIN_RAD:
            self._turn_fails += 1
            if self._turn_fails >= TURN_FAIL_LIMIT:
                self._turn_dead = True
                self._say(f"    {TURN_FAIL_LIMIT} re-square presses turned us nothing "
                          "- footwork will strafe but not re-square")
            return
        if (dz > 0) != (want > 0):
            # Turning AWAY from the target would diverge if we kept doing it.
            self._turn_dead = True
            self._say("    arrow keys turn the wrong way here "
                      "- footwork will strafe but not re-square")
            return
        self._turn_fails = 0
        if hold > 0:
            self._turn_rate = 0.7 * self._turn_rate + 0.3 * (abs(dz) / hold)

    # ------------------------------------------------------------ misc
    def _say(self, msg: str) -> None:
        if self.log is not None:
            try:
                self.log(msg)
            except Exception:
                pass

    def stats(self) -> dict:
        return {
            "enabled": self._enabled and not self._dead,
            "sign": self._sign,
            "strafe_speed": round(self._strafe_speed, 2),
            "turn_rate": round(self._turn_rate, 2),
            "turning": not self._turn_dead,
            "idle": self._counts["idle"],
            "drag": self._counts["drag"],
            "turn": self._counts["turn"],
            "blocked": self._counts["blocked"],
            "idle_key_down": round(self._idle_key_down, 2),
            "turn_key_down": round(self._turn_key_down, 2),
            "drag_key_down": round(self._drag_key_down, 2),
            "travel": round(math.hypot(*self._travel), 1),
            "duty": round(self._duty(), 3),
            "fidget": round(self._fidget, 2),
            "held": sorted(self._held),
        }
