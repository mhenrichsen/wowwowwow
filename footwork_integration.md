# Wiring `footwork.py` into `combat.py` — the patch, NOT applied

This is the file the specification calls `footwork_patch.md`. Nothing in
`combat.py`, `bot.py`, `questing.py`, `gear.py`, `taxi.py`, `rxp.py` or
`dangers.py` has been edited. Four hunks; three are required, the fourth is
optional but **its condition matters** — see HUNK 4.

## Why this is anchored on text, not on line numbers

`combat.py` is being edited by another workflow as this is written. It was
1196 lines when the design was drafted, 1221 during review and **1330 when
this file was written**; `MELEE_RANGE` moved from line 46 to 71. Every hunk
below therefore quotes **unique source text**. The line numbers are a hint to
re-grep, not a location:

```
$ grep -n "a = self.pick(prof.rotation, me, t, active)" combat.py   # 1 hit, 1275
$ grep -n "^        t_start = time.time()" combat.py                # 1 hit, 741
```

Confirm each anchor is still a single hit before pasting.

---

## HUNK 1 — lazy construction

New property on `Combat`. It touches nothing existing; paste it anywhere in
the class (next to `retreat_point` is the natural home, since that is the
other movement code). The import is inside the property so that importing
`combat` never pulls in `footwork`, and so that a `footwork.py` with a syntax
error can never stop the bot from starting.

```python
    @property
    def footwork(self):
        """Keyboard footwork (footwork.py). Built on first use, per bot."""
        fw = self.__dict__.get("_footwork")
        if fw is None:
            import footwork
            import offsets as O
            from ctm import CTM_ACTION
            fw = self.__dict__["_footwork"] = footwork.Footwork(
                self.kb,
                line_walkable=self.line_walkable,
                ground_z=(self.nav.ground_z if self.nav else None),
                move_flags=lambda: self.p.u32(self.g.player().base + O.UNIT_MOVE_FLAGS),
                threats=self.threats,
                add_radius=add_radius,
                ctm_action=lambda: self.p.u32(CTM_ACTION),
                player=self.g.player,
                log=_log,
                enabled=bool(self.nav))
        return fw
```

Everything it needs already exists on the instance: `MageBot(Combat,
QuestRunner)` owns `kb`, `nav`, `threats`, `p` and `g`, and `line_walkable`
and `add_radius` are `combat.py`'s own. `enabled=bool(self.nav)` is the
no-navmesh case: with no mesh there is no ledge guard and no walkability
test, and footwork then costs exactly what the bot does today (it returns
`"off"` and presses nothing).

`move_flags` deliberately re-reads `self.g.player().base` rather than caching
it: the object manager moves the player object on zone changes, and this
probe runs a few times a second at most.

`player=self.g.player` is not optional and is not only used after a burst:
`tick()` calls it once per tick for its own geometry (see HUNK 3).

---

## HUNK 2 — per-fight reset

**ANCHOR** (inside `_fight_one`, one occurrence, currently line 741):

```python
        t_start = time.time()
```

**AFTER:**

```python
        t_start = time.time()
        self.footwork.begin()
```

`begin()` releases any key (there should be none), resets the per-fight duty,
travel and drag budgets and draws this fight's *fidget* — how twitchy the
character is today. The strafe-sign calibration, the turn-rate calibration
and the "the keys are not reaching the client" latch are **not** reset: those
are properties of the session, not of the fight.

### Why `begin()` takes no position

An earlier draft was `self.footwork.begin(me)`, and `me` here is the read at
the top of `_fight_one`, i.e. **before** the fight loop's
`self.approach(me, t, reach)` has closed the gap. Real pulls are 20–60 yd
(`questing.SEARCH_RADIUS = 60`, `bot.PULL_RADIUS = 24`, `combat` re-targets
within 45 yd, and a warrior's `engage_range = 3.5` means it closes almost the
whole gap). Recording that position as the anchor of a 15 yd travel cap
latched the cap on tick one and disabled **both** behaviours for the entire
fight. Measured offline: a fight pulled from 20 yd produced 0 bursts and 0
key events in 60 s of combat; the identical fight pulled from 14 yd produced
6. `footwork.py` now bounds the vector sum of the displacements *its own
bursts* produced (`TRAVEL_CAP`), which is both the quantity the cap was
always described as bounding and immune to where the pull started — and to
`combat` chasing a fleeing mob mid-fight. `begin(me)` still works: the
argument is accepted and ignored.

---

## HUNK 3 — the single call site

**ANCHOR** (inside `_fight_one`, one occurrence, currently line 1275, at 12
spaces of indent):

```python
            a = self.pick(prof.rotation, me, t, active)
```

**REPLACE WITH:**

```python
            a = self.pick(prof.rotation, me, t, active)
            import offsets as _O, footwork as _fw
            _gl = getattr(self, "_gcd_until", 0.0) - time.time()
            _r = self.footwork.tick(_fw.FootCtx(
                me=me, target=t, active=active,
                ranged=prof.ranged, engage_range=prof.engage_range,
                melee_range=MELEE_RANGE, emergency_hp=EMERGENCY_HP,
                free_for=(0.0 if a is not None else (_gl if _gl > 0 else 0.45)),
                casting=bool(self.p.u32(me.base + _O.UNIT_CASTING_SPELL)),
                escaped=bool(escaped or bail_pending)))
            if ":" in _r:
                continue                 # we moved: re-read the world next tick
```

### The return contract — why `":" in _r` and not a tuple of names

`tick()` returns `"off"`, `"still"`, or `"<kind>:<keys>"` (`idle:q`,
`turn:left`, `drag:s+e`). **Every return containing `":"` spent time; no other
return spends any.** So the predicate above cannot build a sleepless loop out
of a return value, and it does not have to be revised when a new kind of
burst is added.

The earlier draft tested `if _r not in ("still", "off")`, and `tick()` had a
third return, `"blocked"`, which pressed no key and slept no time. That
combination is a hot loop *with the rotation skipped*, and it is not exotic:
every drag candidate sits at ≥ 90° off forward (footwork never presses `"w"`),
so when an unengaged mob closes from the ~20° cone dead astern, no candidate
gains ground and the plan is `None` on every tick. Measured in a sim of this
exact control flow: **39,996 laps in 0.0000 s of simulated time with zero
abilities pressed**, against 6 drag steps and +5 to +8 yd of gap on the other
seven of eight bearings. `tick()` no longer has a zero-cost non-`"still"`
return: a blocked plan now sets a 1.5 s cooldown, returns `"still"` (so the
loop sleeps *and presses its rotation*) and is counted in
`footwork.stats()["blocked"]`.

### Why this call site, and why `free_for` is computed rather than promised

Placing the call immediately after `pick()` means the free-time promise is
derived from the same decision the loop is about to act on, instead of being
asserted by whoever applies the patch. A hand-placed `free_for` degrades the
"movement never cancels a cast" property from structural to merely checked.

* `a is not None` → `free_for = 0`, and **no idle burst can start** (the hold
  plus a 0.15 s settle margin can never fit in 0 s). Only a drag, which is
  supposed to pre-empt, can run.
* `a is None` because the GCD is running → `free_for` is the exact remaining
  GCD, so a burst can never delay the cast that follows it.
* `a is None` with the GCD already expired (a rage-starved warrior) → 0.45 s
  is free by definition: there was nothing to press.

This is the whole class-generalisation mechanism. Offline, driving the same
`Footwork` object with a warrior's free-tick profile and a mage's produced
28.4 s and 3.5 s of key-down over 600 simulated seconds each — the mage
fidgets an eighth as much as the warrior, with no class branch anywhere in
`footwork.py`.

### `me` is passed but re-read

`FootCtx.me` is filled in from the loop's snapshot for convenience, and
`tick()` **replaces it with a fresh `player()` read** before it computes any
geometry. It has to: `game.Unit` is a `struct.unpack` of a position, not a
live view, and between the loop's `me = self.g.player()` and this anchor sit

```python
            if me.moving or self.ctm.moving:
                self.stop()
            self.face(t)
```

— `Bot.stop()` is a CTM order plus `wait_until_still(1.0)`, i.e. up to a
second of *continued travel* before we halt, and `Bot.face()` turns us with a
CTM nudge and up to two 1.2 s arrow presses. Measured: a 0.5 rad stale facing
aims the terrain check 0.39 yd away from the real motion on a 0.79 yd move;
2.5 rad aims it 1.49 yd away, i.e. more than the move is long. The ledge
guard compares `ground_z` against `me.z` and the walkability line starts at
`me.x, me.y`, so a stale snapshot means checking ground the character never
enters. The extra cost is one position read per tick, which the loop above
already does twice.

### Why nothing else is needed

* No `try`/`finally` around the 600-line `while` loop (which would be a
  re-indentation of a file another workflow is editing).
* No `release()` sprinkled before `cast` / `approach` / `stop` / `escape`.
* No `busy()` contract, no watchdog thread.

All of those exist only to contain a key held across a `return`, and **no key
is ever held across a return**: `tick()` presses, sleeps, releases in a
`finally`, waits for the move flag to clear and only then comes back. That
also matters for the `self.stop()` above, which a still-held key would turn
into a CTM order fired underneath our own keyboard move plus a full second of
blocking.

---

## HUNK 4 — OPTIONAL, and safe to skip

In the `if a is None:` branch (currently line 1286) the trailing

```python
                time.sleep(0.15)
```

may become

```python
                time.sleep(0.15 if ":" not in _r else 0.0)
```

so the loop does not sleep twice in a tick that already spent time moving.
Skipping this hunk costs 0.15 s per burst and nothing else.

**Do not write `time.sleep(0.15 if _r == "still" else 0.0)`.** `"off"`, not
`"still"`, is the ordinary no-burst return: `tick()` returns `"off"` from
every gate (`caps(ctx) < MIN_ROOM`, which a melee profile hits at ≥ 5.0 yd; a
CTM order in flight; casting; below 55% hp; and the whole no-navmesh case,
where `enabled=bool(self.nav)` makes *every* return `"off"`). Testing only
for `"still"` turns this branch into a busy spin that re-runs `enemies()`,
`threats()`, `g.player()` and the rest of the 500-line body at full speed
instead of ~6 Hz, for a rage-starved warrior at melee edge or for any bot run
without a mesh. The `":" not in _r` form is the same predicate as HUNK 3 and
cannot get this wrong.

---

## Behaviour notes the caller should know

### Footwork turns the character

Earlier drafts claimed "facing is never touched". That is no longer true, and
the reason is in `footwork.py`'s docstring under **WHY WE TURN**: a strafe
translates without rotating, a mob that follows us walks straight at us
(which leaves its bearing *from* us unchanged), and `Bot.face(tolerance=0.35)`
is a no-op below 0.35 rad by design — so the bearing error ratchets out to the
budget and stays there, pinning one strafe direction's cap at zero and
forcing the character to alternate left, right, left, right whatever the dice
say. Measured over 12 × 600 s melee fights: lag-1 autocorrelation **−0.91 in
the emitted key sequence** while the intended directions sat at +0.07. That is
the metronome the module exists to avoid.

So footwork now also presses the arrow keys, **only ever toward the target**
and only ever by `0.8 ×` the measured error, capped at 0.25 s. This is
`Bot.face`'s own primitive (`kb.press("left" if err > 0 else "right",
hold=min(abs(err) / math.pi, 1.2))`) at `Bot.face`'s own rate, and footwork
measures the real rate from the first presses rather than trusting π. Turning
never cancels a cast in 1.12 (only translation does), and a re-square is
gated on `free_for` like every other burst anyway. If the arrows turn out not
to reach the client, or to be wired backwards, footwork disables *turning*
after at most three presses and keeps strafing.

Consequences for the caller: `_r` can be `"turn:left"` / `"turn:right"`, which
the `":" in _r` predicate handles; and `Bot.face(t)` remains a no-op, because
a re-square only ever reduces the error it is correcting (worst error left
behind, measured over 4000 ticks with the facing randomly jostled: 0.35 rad,
i.e. exactly at the tolerance and never past it).

### What counts as "another mob getting closer"

The user's request was "backpedal away with the mob in combat if another mob
is getting closer". The **aggroed** case is included and is the ordinary one.
An earlier draft skipped any mob whose `target` was us — but
`Combat.enemies()` returns exactly `u.target == me.guid and u.in_combat`, so
such a mob is also in `active`, and a `len(active) != 1` gate refused it
twice over. The behaviour could then only ever fire at mobs that had *not*
noticed us. Measured: add at 24 yd closing at 7 yd/s, 0 drag steps when it
targeted us, 6 when it did not.

What disqualifies a mob now is being **close**, not having noticed us: inside
`melee_range + 4` it is on us, that is a 2v1, and `combat`'s own join and
bail logic owns it — footwork stands down for 2 s and hands it back. Outside
that, a mob whose distance to us fell by ≥ 1.5 yd over the last ~1.2 s is
something to back away from, aggroed or not. At most one such add: two is
also `combat`'s problem, not footwork's.

### Drag steps are bearing-bounded, and snap back inside the tick

An uncapped pure-strafe drag step swung the target's bearing to 0.81 rad —
2.3× `Bot.face`'s tolerance — so every flank step bought ~0.45 s of motion and
then paid up to 1 s of `wait_until_still(1.0)` on the next tick while the add
closed at run speed.

Simply applying the idle strafe's 0.32 rad cap is **not** the fix, and this is
worth knowing before someone "simplifies" it back: at a mob's 2.0 yd follow
distance that cone is 0.66 yd wide, under the 0.7 yd a `MIN_HOLD` press
covers, so the cap deletes the lateral candidates outright — and on 5 of 16
add bearings (the ones coming from behind a shoulder) a pure strafe is the
*only* candidate that gains anything, because footwork never presses `"w"`.
Measured: capping took the drag from **45/48 scenarios that opened the gap to
33/48**, i.e. it silently removed the behaviour the user asked for on a third
of approach angles.

What is applied instead: a drag step may spend up to `DRAG_BEARING_MAX`
(0.60 rad), and `_do()` immediately follows it with the same arrow-key
re-square the idle path uses, inside the same `tick()`, so the fight loop's
next `face()` never sees the error. 0.60 is what one `MAX_TURN_HOLD` press
recovers with margin; the snap-back aims at the target's *pre-step* position
(the mob moves while we do), so it is good to a few hundredths rather than
exact, which is why the allowance is 0.60 and not 0.81. Measured over 122 drag
steps: worst post-step bearing error **0.000 rad** with the bound, 0.47 with
the snap-back but no bound (5% of steps past 0.35), 1.07 with neither.

The cost is that a drag tick can block for `MAX_HOLD + MAX_TURN_HOLD +
2 × SETTLE_MAX` = 1.5 s rather than 0.85 s. That is still less than the
`wait_until_still(1.0)` plus two 1.2 s arrow presses `Bot.face` falls back to,
it only happens on a drag step that actually swung the bearing, and the drag
is bounded at 2.5 s of key-down per episode regardless.

The range cap is *not* applied to a drag (a drag deliberately overshoots to
`melee_range + 2`, because the mob follows faster than we retreat); only the
bearing half is.

---

## Documented, deliberately NOT wired

`footwork.stats()["blocked"]` counts the ticks where a drag was armed — a
second mob closing and not yet on us — and **no direction was both safe and
useful**: the terrain has boxed us in with a mob inbound. That is a
meaningful escalation hint, and `combat` could reasonably set `bail_pending`
on it:

```python
            if self.footwork.stats()["blocked"] > _blocked_seen:   # NOT in the patch
                bail_pending = True
```

It is described and not applied because it changes survival logic, and
survival logic is not footwork's to own. It is a counter rather than a return
value on purpose — see the return contract above.

---

## What to expect in the log

`footwork.py` logs through the `log=_log` callable, only on events that mean
something:

```
    strafe sign measured: "q" moves left (1.2 yd in 0.17s)
    footwork has moved us 15 yd this fight - no more until the next one
    add 0x...  reached us mid-drag - footwork stands down
    3 re-square presses turned us nothing - footwork will strafe but not re-square
    arrow keys turn the wrong way here - footwork will strafe but not re-square
    3 footwork bursts moved us nothing - the keys are not reaching the client;
      footwork off for the session
    footwork off for 5s: <the exception>
```

The last three are the ones to watch. This PC has a recorded history of key
presses not reaching the client (the Danish-scancode note); the third-from-last
line is exactly that signature, and footwork disables itself instead of
pretending.

## What to expect in the fight summary

No new instrumentation is needed. The existing per-fight line already prints
seconds elapsed and time in melee, so a DPS regression is visible. The bounds
that make it small are: a burst only runs where the loop was about to
`sleep(0.15)` anyway; the facing error the next tick's `face()` sees stays
inside its own 0.35 tolerance — for idle strafes by the 0.32 rad cap, for
re-squares because a re-square only ever reduces the error, and for drag steps
because `_do()` snaps the facing back before returning — so `face()` stays a
no-op; the range cap keeps a melee
profile inside 5.0 yd against a 6.0 yd `MELEE_RANGE`, so every
`{"target_within": 6.0}` condition in `classes/warrior.py` still holds; and
the duty accumulator caps idle key-down at 25% of fight wall-clock. A
re-square is not counted against the duty cap because it does not translate
us at all; it is reported separately as `turn_key_down` in `stats()`.

A drag is genuinely expensive and meant to be: it pre-empts the rotation, and
is bounded at 2.5 s per episode and 6.0 s per fight.

## Rolling it back

Delete hunks 2, 3 and 4. Hunk 1 is inert on its own. Or leave the patch in
and pass `enabled=False` in hunk 1 — every `tick()` then returns `"off"` and
presses nothing.

## Tests

`scratchpad/test_footwork.py`, run with `py -3`: **47/47 passing**, 34.0
simulated hours of combat in 20 s of real time, entirely offline against stubs
with a fake clock. Nine of the checks are regressions for the problems fixed in
this revision — 16b (pull distance), 29b (an aggroed add), 34 (the *emitted*
key autocorrelation, not the RNG draw), 34b and 34c (the re-square and its
failure modes), 41 (the stale snapshot), 42 (the drag bearing bound and the
drag tick's blocking time), 43 (the return contract and caller progress), and
44, which asserts the three lines of *this file* the review found wrong.
Several of them carry their own control, so they fail if the fix is reverted:
34b runs with the re-square disabled (94% of bursts forced into one direction,
against 0% with it), 41 runs with the fresh read stubbed out (9 of 38 bursts
in the loch, against 0 of 59), and 42 prices the same drag steps uncapped
(1.07 rad of bearing swing, against 0.00).
