"""Class-agnostic combat engine, driven by a ClassProfile (classes/).

The engine owns everything classes share: target selection and switching,
counting attackers, range and line of sight, crowd control of extra
attackers, emergencies and escaping, melee auto-attack. The profile says
which abilities exist and when each is worth using; every tick the engine
casts the first ability in the profile's rotation whose conditions all hold,
that is on the bar, off cooldown and affordable.
"""

from __future__ import annotations

import math
import time

import bridge as br
import events
import factions
import mobstats
import spells
import worlddb
import offsets as O

ADD_RADIUS = 22.0                # a hostile this close to the target = an add


def band_radius(level: int) -> float:
    """The level-band constant this rule used to be. Kept as the CEILING.

    22 yd was measured on a level-18 mage that died to pairs. In Coldridge
    Valley every wolf has a friend inside 22 yd, so a level-1 warrior
    rejected every target it picked, blacklisted both mobs for 90 s, walked
    on and picked the next one - one kill in two minutes and a log full of
    "leaving it for a while". The bands were the patch for that.
    """
    if level < 10:
        return 8.0
    if level < 15:
        return 16.0
    return ADD_RADIUS        # 22 yd from level 15: the mage died at 18


def add_radius(level: int, target=None, me=None, ranged: bool = False) -> float:
    """How close a second mob must be before a pull counts as a pair.

    The bands above freeze at 22 yd from level 15 onward, so a level-40
    warrior in mail with 1500 hp applies a number measured on a level-18
    mage, and in a dense 30-60 zone that rejects nearly every spawn - the
    Coldridge failure again, thirty levels later. Refusing a pull is not
    free: it is a blacklist, a walk and another pull somewhere else, which
    is most of what "too much travelling" is made of.

    The general law: a neighbour only matters if it would JOIN (the server's
    own aggro radius, which scales with the level gap) and if we cannot
    AFFORD it (mobstats, which measures what this character's kills really
    cost). Both scale by themselves from level 1 to 60. Until an entry has
    been measured, and for callers that cannot pass a target, the band is
    the answer - and the measured radius may only ever be SMALLER than the
    band, so this can widen what we accept and never narrow it.
    """
    ceiling = band_radius(level)
    st = mobstats.current()
    if st is None or target is None or me is None:
        return ceiling
    r = st.add_radius(me, target, ranged)
    return ceiling if r is None else max(3.0, min(ceiling, r))
from classes import Ability
from game import Unit

GCD = 1.5
MELEE_RANGE = 6.0
EMERGENCY_HP = 45.0
# Below this, being low is reason enough on its own. The relative-health
# clauses above let a level-11 bear walk us from 54% to 1% because its
# own health fell in step with ours (73% vs our 54%: never +20).
DESPERATE_HP = 30.0
# Above this, a pack on us is a fight, not a rout - see the branch in
# _fight_one that used to run from three mobs at any health at all.
ROUT_HP = 60.0
# Drink a healing potion below this (the user's rule), if we carry one.
POTION_HP = 20.0
POTION_SLOT = 13          # action page 2: UseAction reaches it, the visible bar is untouched
POTION_COOLDOWN = 120.0   # shared by every potion in 1.12
PULL_DISTANCE = 26.0      # default engage range for ranged classes


def _bot():
    """The running bot module. `from bot import log` loaded bot.py a SECOND
    time (the process runs it as __main__), with its own STATE and LOG_TAIL:
    every "fighting" state after the first went unrecorded (dbg.py report
    showed 1 fight for 124 kills, and their time booked as "looting") and
    combat lines never reached the status file's log tail."""
    import sys
    m = sys.modules.get("__main__")
    if m is not None and hasattr(m, "set_state") and hasattr(m, "log"):
        return m
    import bot
    return bot


def _log(msg):
    _bot().log(msg)


def _state(state, **kw):
    _bot().set_state(state, **kw)


class Combat:
    # ------------------------------------------------------------ auras
    def aura_ids(self, name: str) -> set[int]:
        cache = self.__dict__.setdefault("_aura_cache", {})
        if name not in cache:
            cache[name] = spells.rank_ids(name)
        return cache[name]

    def has_aura(self, u: Unit, name: str) -> bool:
        return bool(set(u.auras) & self.aura_ids(name))

    def controlled(self, u: Unit) -> bool:
        """Crowd-controlled or rooted: not a threat right now."""
        names = ["Frost Nova"]
        if self.profile.crowd_control:
            names.append(self.profile.crowd_control.name)
        sent = getattr(self, "_cc_sent", None)
        if sent and sent[0] == u.guid and time.time() - sent[1] < 3.0:
            return True
        return any(self.has_aura(u, n) for n in names)

    # ------------------------------------------------------------ perception
    def enemies(self, me, primary_guid=None) -> list[Unit]:
        """Everything alive that is fighting us, plus the chosen target."""
        return [u for u in self.g.units() if not u.dead and
                (u.guid == primary_guid or (u.target == me.guid and u.in_combat))]

    # ------------------------------------------------------------ measurements
    @property
    def mobstats(self):
        """This character's measured mob costs (None until we have a name)."""
        st = self.__dict__.get("_mobstats")
        if st is None:
            try:
                st = self.__dict__["_mobstats"] = mobstats.for_char(self.g.player_name())
            except Exception:
                return None
        return st

    def pair_mates(self, me, t: Unit, threats=None) -> list[Unit]:
        """The friends that make `t` a pull fight() refuses ([] = pull it).

        Whether a pair is a problem is a question about COST, not about
        levels: two level-1 wolves cost a level-2 warrior ~15% of its health
        and two level-15 crocolisks killed a level-17 mage in 24 s. The
        level branches below were three attempts to say that in levels. Now
        they are the cold start: when mobstats has measured every mob in the
        fight, the answer is whether their predicted health cost fits in the
        health we are holding, and that law reads the same at level 50.

        Bounded on purpose: the measurement may only ever turn a refusal
        into a pull, never the other way round. A model that under-predicts
        can then only cost us a fight the old rule would have taken anyway,
        and a death immediately drops the budget it is measured against.

        Target choice uses the same rule: it used to only add a distance
        penalty for a friend, so the bot walked up to a wolf that fight()
        then refused - back off 20 yd, stand 3 s, walk back, three times in
        30 s at 15:57 with every wolf in the den paired.
        """
        threats = self.threats() if threats is None else threats
        others = self._pair_mates_bands(me, t, threats)
        if not others:
            return []                     # today's rule already pulls it
        st = self.mobstats
        if st is not None and st.confident([t] + others) and st.affordable_pull(me, t, others):
            # one line per entry per minute: pair_mates runs once per
            # candidate per tick and this would otherwise be the whole log
            said = self.__dict__.setdefault("_pair_said", {})
            if time.time() - said.get(t.entry, 0) > 60:
                said[t.entry] = time.time()
                cost = sum(st.cost(u.entry, me, u.level)[0] for u in [t] + others)
                _log(f"  {worlddb.name(t.entry)} + {len(others)} friend(s) measure at "
                     f"{cost:.0f}% of our health, and we can spend {st.budget(me):.0f}% "
                     "- pulling it anyway")
            return []
        return others

    def _pair_mates_bands(self, me, t: Unit, threats) -> list[Unit]:
        """The old level-band rule, kept as the cold start (see pair_mates)."""
        others = [u for u in threats if u.guid != t.guid and not u.dead
                  and u.dist(t) < add_radius(me.level, t, me, self.profile.ranged)]
        if me.level >= 5 and not others:
            # standing at t, a mob our level or above aggroes from
            # 11-12 yd: a level-6 warrior charged a Wendigo, its mate
            # 11 yd off joined, twice, and the second time killed us.
            # ... and a HOSTILE one from its full aggro radius (20 yd +1 per
            # level above us): at 17:38 and 17:40 a level-7 Wendigo 13 and
            # 20 yd from the target joined and killed a level-6 warrior twice
            others = [u for u in threats if u.guid != t.guid and not u.dead
                      and u.level >= me.level
                      and (u.dist(t) < 14.0
                           or (factions.is_hostile(me.faction, u.faction)
                               and u.dist(t) < min(25.0, 20.0 + (u.level - me.level)) - 1.0))]
        if not others:
            return []
        # rusty golems two-three levels below us can come in pairs
        if all(u.level <= me.level - 2 for u in others + [t]):
            return []
        # ... and so can anything at levels 1-3: two level-1 wolves on a
        # level-2 warrior at full health cost ~15%
        if (me.level <= 3 and len(others) == 1 and me.hp_pct >= 70
                and all(u.level <= me.level for u in others + [t])):
            return []
        return others

    # ------------------------------------------------------------ rules
    def conditions_hold(self, a: Ability, me, t: Unit, active: list[Unit]) -> bool:
        d = me.dist(t)
        for k, v in a.when.items():
            if k == "target_hp_below" and not t.hp_pct < v: return False
            if k == "target_hp_above" and not t.hp_pct > v: return False
            if k == "my_hp_below" and not me.hp_pct < v: return False
            if k == "my_hp_above" and not me.hp_pct > v: return False
            if k == "power_at_least" and not me.power >= v: return False
            if k == "power_pct_at_least" and not me.mana_pct >= v: return False
            if k == "enemies_at_least" and not len(active) >= v: return False
            if k == "enemy_in_melee":
                # Frost Nova reaches 10 yd: root a mob as it closes instead of
                # starting a 2.5 s Fireball that its first hits push back
                if any(me.dist(e) <= max(MELEE_RANGE, 8.5) for e in active) != bool(v): return False
            if k == "no_sheep_near":
                # Frost Nova damages everything around: it would break our
                # own Polymorph and hand us the second mob back
                if any(self.controlled(e) and me.dist(e) < 12.5
                       for e in getattr(self, "_all_enemies", [])): return False
            if k == "target_within" and not d <= v: return False
            if k == "level_below" and not me.level < v: return False
            if k == "level_at_least" and not me.level >= v: return False
            if k == "target_beyond" and not d > v: return False
            if k == "target_missing_aura" and self.has_aura(t, v): return False
            if k == "target_has_aura" and not self.has_aura(t, v): return False
            if k == "self_missing_aura" and self.has_aura(me, v): return False
            if k == "self_has_aura" and not self.has_aura(me, v): return False
        return True

    def usable(self, a: Ability) -> bool:
        key = f"{a.name}"
        return self.has(a.name) and time.time() >= self.cd.get(key, 0)

    # A failure above this much mana was not about mana, so do not let it
    # teach us a cost. No vanilla spell at these levels costs near it.
    COST_CEILING = 200.0
    # A crowd control older than this may not outlast a 60 yd run.
    CC_FRESH = 5.0

    def affordable(self, a: Ability, me) -> bool:
        """Can we pay for this spell right now?

        A level-11 bear killed us twice because at 2% mana the rotation kept
        picking Frostbolt: the press does nothing and each dead attempt is
        ~4.7 s of standing still being hit.

        "Unknown cost gets a free try" then killed us a third time, at 04:43.
        Fireball sits in the rotation but never lands at this level, so its
        cost was never learned, so it was retried forever - three presses at
        18, 48 and 63 mana while two Forest Lurkers chewed through us. An
        unknown cost therefore needs the most expensive cost we DO know, and
        every failure teaches a lower bound."""
        if self.profile.power != "mana":
            return True
        costs = self.__dict__.get("_spell_cost", {})
        need = costs.get(a.name)
        if need is None:
            floor = self.__dict__.get("_spell_cost_min", {}).get(a.name, 0.0)
            # the dearest spell we have actually paid for is a fair prior
            need = max(floor + 1.0, max(costs.values(), default=0.0))
        return me.power >= need

    def note_cast_failed(self, a: Ability, mana_at_press: float) -> None:
        """A cast that did nothing at low mana tells us the cost is higher
        than that. Above COST_CEILING the failure had another cause (one
        Frostbolt failed at 625 mana with a learned cost of 50), so ignore
        it rather than learn a bound that disables the spell."""
        if self.profile.power != "mana" or mana_at_press >= self.COST_CEILING:
            return
        d = self.__dict__.setdefault("_spell_cost_min", {})
        if mana_at_press > d.get(a.name, 0.0):
            d[a.name] = mana_at_press

    def pick(self, abilities, me, t, active) -> Ability | None:
        # the global cooldown is a timestamp, not a sleep: the fight loop keeps
        # reading, moving and facing while it runs, and off-GCD abilities
        # (Heroic Strike) go out inside it
        gcd_on = time.time() < getattr(self, "_gcd_until", 0.0)
        for a in abilities:
            if gcd_on and a.gcd:
                continue
            if (self.usable(a) and self.affordable(a, me)
                    and self.conditions_hold(a, me, t, active)):
                return a
        return None

    # ------------------------------------------------------------ casting
    def click_unit(self, t: Unit, right: bool = True) -> bool:
        """A genuine mouse click on a mob, like a player: left selects it,
        right selects it and starts auto-attack. Unlike writing the target
        into client memory, the click tells the server - measured: a Snow
        Leopard took no white damage for minutes, one right-click took it
        from 61% to 21% in 6 s."""
        import mouse
        import screenpos
        fix = getattr(self, "fix_camera", None)
        if fix:
            fix()
        try:
            u = self.g.unit(t.guid) or t
            pos = screenpos.find_on_screen(self.p, u.guid, u.x, u.y, u.z, heights=(0.5, 0.3, 0.8, 1.0))
            if pos is None:
                return False
            click = mouse.real_right_click if right else mouse.real_left_click
            return bool(click(self.p.hwnd, *pos))
        except Exception as e:
            _log(f"    click on {worlddb.name(t.entry)} failed: {e}")
            return False

    def server_target(self) -> int:
        """Our own UNIT_FIELD_TARGET: what the SERVER has us targeting. The
        TARGET_GUID global we write is client-side only - at 16:03 it said
        wolf while the target frame (and so Lua's AttackTarget) still said a
        Rockjaw Trogg out of reach, and the warrior stood at a wolf's side
        for 25 s swinging at nothing. This field follows a real selection."""
        try:
            return self.g.player().target
        except Exception:
            return 0

    def start_attack(self, t: Unit, force: bool = False) -> bool:
        """Select the mob for real and switch auto-attack on at it. True once
        the server has it as our target. force: cycle it off and on even if
        the field says we are on it - at 16:38 the field named the Troll
        Whelp for 50 s while not one swing landed, and the whelp killed us."""
        self._attack_try = time.time()
        if self.server_target() == t.guid and not force:
            # Selected is not swinging: the field names what we have selected,
            # and a mob that jumps us is often selected already. Returning
            # here left auto-attack off - half the fights a mob started
            # (15 of 30, Dsa's run) stood 6-11 s at melee range with no swing
            # until the no-damage watchdog forced it. ATTACK_ON (53) never
            # toggles: it only switches auto-attack on when it is off.
            self.bridge.send(br.ATTACK_ON, wait=0.05)
            return True

        def wait_field(want, secs):
            t0 = time.time()
            while time.time() - t0 < secs:
                if self.server_target() == want:
                    return True
                time.sleep(0.05)
            return False
        if self.server_target() or force:
            # swinging at something else: off first, in its own frame
            self.bridge.send(br.ATTACK_OFF, wait=0.1)
            wait_field(0, 0.5)
        # a genuine selection makes it the UI target that the attack uses;
        # off screen, the target global we write has to do
        self.set_target(t.guid)
        self.click_unit(t, right=False)
        self.bridge.send(br.ATTACK_ON, wait=0.1)
        if wait_field(t.guid, 0.6):
            return True
        return self._start_attack_click(t)

    def _start_attack_click(self, t: Unit) -> bool:
        """The old way: a right-click on the mob, the bridge if off screen."""
        if self.click_unit(t, right=True):
            return self.server_target() == t.guid
        me = self.g.player()
        u = self.g.unit(t.guid) or t
        if me.dist(u) < 3.5 and time.time() - getattr(self, "_click_backoff", 0) > 4:
            # Right on top of it the mob is under the camera and cannot be
            # clicked, and the bridge fallback only writes a target the server
            # never hears of: two Wendigos stood at 2 yd for 16 s, 0 swings
            # from us, and killed us. Step back so it is in view, click again.
            self._click_backoff = time.time()
            p = self.retreat_point(me, [u], 3.5)
            if p:
                self.walk_to(*p, timeout=1.5)
                self.stop()
                self.face(self.g.unit(t.guid) or u)
                time.sleep(0.2)
                if self.click_unit(self.g.unit(t.guid) or u, right=True):
                    _log(f"    stepped back to click {worlddb.name(t.entry)} - it was under the camera")
                    return self.server_target() == t.guid
        return False

    def cast(self, a: Ability, target: Unit | None) -> bool:
        """Cast an ability. True if it went off (resource spent / effect seen).

        A self spell (Seal, Blessing, Holy Light, Divine Protection) points
        the target at us, and a friendly target stops auto-attack: the Ice
        Claw Bear stood at 70% for 10 s after a Seal (08:57), and most of the
        "slow starts" had a cast in them. In a melee fight, put the mob back
        as the target and switch auto-attack on again straight away."""
        ok = self._cast(a, target)
        if (a.target == "self" and target is not None and self.profile.melee_auto_attack):
            try:
                me = self.g.player()
                # self spells go out with onSelf now and leave the target
                # alone; recover only if the selection did move after all
                if (target.guid != me.guid and me.in_combat
                        and self.p.u64(O.TARGET_GUID) != target.guid):
                    # the client adopts a written target with a lag (0.4 s
                    # was not enough for a Polymorph, see _cast): at 0.1 s the
                    # attack still went to us, and the Elder Crag Boar sat at
                    # 23% for 12 s after Divine Protection + Holy Light (09:06).
                    # Not a forced off-and-on: that reset the swing timer after
                    # every opening Seal (median first hit 0.9 s -> 3.0 s).
                    # ATTACK_ON only switches auto-attack on when it is off.
                    self.set_target(target.guid)
                    time.sleep(0.5)
                    self.start_attack(self.g.unit(target.guid) or target)
            except Exception:
                pass
        return ok

    def _cast(self, a: Ability, target: Unit | None) -> bool:
        me = self.g.player()
        # a self spell is cast with the onSelf flag: no target switch, so the
        # mob stays selected and auto-attack keeps swinging
        on_self = a.target == "self"
        want = None if on_self else (target.guid if target is not None else None)
        if want is not None:
            # the client needs a few frames to adopt a target written into
            # memory: a Frostbolt pressed right after the sheep still flew at
            # the sheep (memory already said "primary", the client did not)
            switched = getattr(self, "_last_cast_target", None) != want
            self.set_target(want)
            self._last_cast_target = want
            if switched:
                # the client adopts the written target global with a lag: at
                # 0.4 s a Polymorph still hit the old target four times today
                time.sleep(1.0)
                self.set_target(want)
                time.sleep(0.1)
                cur = self.p.u64(O.TARGET_GUID)
                if cur != want:
                    # diagnostics: a Polymorph once landed on the trogg we
                    # were fighting instead of the scout it was aimed at
                    # (our own UNIT_FIELD_TARGET is always 0: no selection
                    # packet is ever sent, so read the client global instead)
                    _log(f"    target global reverted: {cur:#x} vs {want:#x}")
        power, thp = me.power, target.health if target else None
        start_hp = me.hp_pct
        was_fighting = me.in_combat
        self._press_state = (me.moving, hex(me.move_flags), self.ctm.moving)
        t_press = time.time()
        due = self.cd.get(a.name, 0)
        if a.cooldown and due and 0 <= t_press - due < 6 and me.in_combat:
            # how long a cooldown spell waited after it came up: the user saw
            # Judgement go out 0.5-1 s late, and nothing measured it
            late = self.__dict__.setdefault("_cd_late", {})
            late.setdefault(a.name, []).append(t_press - due)
            events.emit("cd_late", spell=a.name, secs=round(t_press - due, 2))
        self.press_spell(a.name, on_self=on_self)
        t_sent = time.time()
        self.__dict__.setdefault("_send_s", []).append(t_sent - t_press)
        # an instant is confirmed by polling below (mana drop); a flat 0.3 s
        # here held the fight loop past the next cooldown
        time.sleep(a.cast_time if a.cast_time > 0 else 0.1)
        after = self.g.player()
        # Charge GAINS rage: "power went down" judged every Charge a failure
        gains = a.name in ("Charge", "Bloodrage")
        def went_off(after):
            if gains:
                return after.power > power or (target is not None and a.name == "Charge"
                                                and (self.g.unit(target.guid) or target).dist(after) < 5.0)
            return after.power < power
        ok = went_off(after)
        if a.queued and self.profile.power != "mana":
            # Heroic Strike pays at the next white hit: the rage is still
            # there at press time, and the 1 s wait below found nothing
            ok = True
        # Melee hits push a cast back (0.5-1 s each). Judging it "failed" at
        # the nominal cast time made us step away - which cancels the cast -
        # so a mage caught in melee never finished a spell. Wait it out.
        # an instant's mana drop still needs a server round trip: judged at
        # 0.3 s a Frost Nova that did go off counted as failed, so no
        # step-back followed and its cooldown was never recorded
        # A rage ability that did not go off cost nothing and is simply
        # pressed again next tick: 0.4 s is plenty for the local server
        deadline = time.time() + (2.5 if a.cast_time > 0 else
                                  1.0 if self.profile.power == "mana" else 0.4)
        hard = time.time() + 7.0
        while not ok and (time.time() < deadline or
                          (time.time() < hard and self.p.u32(after.base + O.UNIT_CASTING_SPELL))):
            # still casting (three melee pushbacks on one Frostbolt): a fight
            # with a Forest Lurker had 6 of 15 casts "fail" while in progress
            time.sleep(0.1)
            after = self.g.player()
            ok = went_off(after)
            if after.dead:
                break
            if after.hp_pct < EMERGENCY_HP and after.hp_pct < start_hp - 12:
                # a level-11 bear took us 54% -> 1% while two failed casts
                # held this loop for 9 s: hand control back to the fight loop
                # the moment we are both low and dropping fast.
                break
            if was_fighting and not after.in_combat:
                break              # the fight ended under us (the bolt in flight killed it)
            if target is not None:
                # the mob died to the previous bolt (its health in memory
                # lagged): 4 s of waiting per fight for a spell never cast
                t2 = self.g.unit(target.guid)
                if t2 is None or t2.dead:
                    break
        if not ok and target is not None and self.profile.power != "mana":
            t2 = self.g.unit(target.guid)
            ok = t2 is not None and t2.health < thp
        if ok and self.profile.power == "mana":
            # Nothing in the client tells us a spell's mana cost, so learn it:
            # the drop on a cast that went off. There is no regen during a
            # cast (the 5-second rule), so the drop is the cost. Take the
            # largest seen - a cast cut short by the mob dying can read low.
            spent = power - after.power
            if 0 < spent < 2000:
                d = self.__dict__.setdefault("_spell_cost", {})
                if spent > d.get(a.name, 0):
                    d[a.name] = spent
        if a.cooldown and ok:
            # from the press, not from when we saw the mana go: judged up to
            # 1 s later, every Judgement came due a second late
            self.cd[a.name] = t_press + a.cooldown
        if ok and a.cast_time > 0:
            # the mana drop shows a few frames before the client finishes the
            # cast: a key pressed in that window is "another action is in
            # progress" and cost 4 s of waiting (one failed Fireball per fight
            # right after a good one)
            time.sleep(0.35)
        if a.cast_time < GCD and a.gcd:
            # used to sleep the rest of the GCD here, which froze the whole
            # fight loop for 1.2 s per instant (no reads, no facing, no
            # Heroic Strike); pick() now waits it out instead. A press that
            # did not go off triggered no GCD, so only a short guard then.
            self._gcd_until = (t_press + GCD) if ok else time.time() + 0.2
        return ok

    # ------------------------------------------------------------ movement
    def retreat_point(self, me, enemies, dist=12.0, avoid_dir=None):
        """Reachable spot `dist` yd away, far from every enemy and not next
        to another mob. `avoid_dir` (radians) skips a 120 degree cone: the
        direction a step back just got stuck in."""
        engaged = {e.guid for e in enemies}
        others = [u for u in self.threats() if u.guid not in engaged]
        best, best_score = None, -1e9
        for k in range(16):
            a = 2 * math.pi * k / 16
            if avoid_dir is not None and abs((a - avoid_dir + math.pi) % (2 * math.pi) - math.pi) < math.pi / 3:
                continue
            x, y = me.x + dist * math.cos(a), me.y + dist * math.sin(a)
            z = me.z
            if self.nav:
                poly, _ = self.nav.nearest_poly(x, y, me.z + 2, max_dist=1.5)
                if poly is None:
                    continue
                import navmesh
                f = self.nav.tiles[poly[0]].pflags[poly[1]]
                if f & navmesh.NAV_WATER and not f & navmesh.NAV_GROUND:
                    continue           # deep water: escaping 60 yd into the
                                       # loch drowned us at 20% hp with the
                                       # crocolisk 55 yd behind
                z = float(self.nav.tiles[poly[0]].center[poly[1]][1])
            if self.nav and not self.line_walkable(me, (x, y, z)):
                continue
            gap = min((math.dist((x, y), (e.x, e.y)) for e in enemies), default=99)
            danger = min((math.dist((x, y), (u.x, u.y)) for u in others), default=99)
            # the run itself must not pass next to another mob: a 60 yd
            # escape from a sheeped smuggler ran straight past a level-15
            # watcher at 10 yd and died at 24% hp
            def seg_d(u):
                dx, dy = x - me.x, y - me.y
                L2 = dx * dx + dy * dy
                t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((u.x - me.x) * dx + (u.y - me.y) * dy) / L2))
                return math.dist((me.x + t * dx, me.y + t * dy), (u.x, u.y))
            run = min((seg_d(u) for u in others), default=99)
            score = gap + min(danger, 25) * 0.8 - (40 if danger < 15 else 0) - (60 if run < 12 else 0)
            if score > best_score:
                best, best_score = (x, y, z), score
        return best

    def line_walkable(self, me, p, step=4.0) -> bool:
        """The straight run from here to p stays on the navmesh, on dry land
        and off any ledge: a 60 yd escape once ran straight off the bank
        into the Duskwood river, where the bot could not climb out."""
        import navmesh
        n = max(int(math.dist((me.x, me.y), p[:2]) / step), 1)
        z_prev = me.z
        for k in range(1, n + 1):
            f = k / n
            x, y = me.x + (p[0] - me.x) * f, me.y + (p[1] - me.y) * f
            poly, pt = self.nav.nearest_poly(x, y, z_prev + 2, max_dist=1.5)
            if poly is None:
                return False
            flags = self.nav.tiles[poly[0]].pflags[poly[1]]
            if flags & navmesh.NAV_WATER and not flags & navmesh.NAV_GROUND:
                return False
            if flags & navmesh.NAV_STEEP:
                return False       # a coastal slope: the crab caught us halfway up
            if flags & navmesh.NAV_WATER and not flags & navmesh.NAV_GROUND:
                return False       # deep water (a ford keeps both flags and is fine)
            # track the terrain height through the poly's height range: a
            # 10 yd drop into a river leaves the +-6 yd window of nearest_poly
            t = self.nav.tiles[poly[0]]
            z_prev = min(max(z_prev, float(t.pmin[poly[1], 1])), float(t.pmax[poly[1], 1]))
        return True

    def backpedal(self, enemies, hold=2.2):
        """After Frost Nova: walk backwards, still facing the target, and
        cast from there. The 12 yd walk to a computed spot was 3-4 s of not
        casting and sometimes a wall.

        1.1 s only made 5 yd, so the mob was in melee the moment the 8 s
        root broke and every even-level fight cost ~50% hp. 2.2 s makes
        ~10 yd, which the mob has to run back, for one extra cast and
        less time being hit.

        3.2 s was tried and reverted: it did make more ground (12.4 yd mean
        over 21 retreats) but cost slightly MORE health per fight (18% vs
        16% over 18 fights), and walking backwards blind for another second
        beside Loch Modan put the mage in the water.
        """
        if self.ctm.moving or self.g.player().moving:
            self.stop()
        m0 = self.g.player()
        self.kb.down("s")
        time.sleep(hold)
        self.kb.up("s")
        self.wait_until_still(0.6)
        m2 = self.g.player()
        _log(f"    backpedalled {math.dist((m0.x, m0.y), (m2.x, m2.y)):.0f} yd, nearest enemy "
             f"{min((m2.dist(e) for e in enemies), default=0):.0f} yd")

    def step_back(self, me, enemies, dist=12.0):
        p = self.retreat_point(me, enemies, dist)
        if p:
            self.walk_to(*p, timeout=3)
            self.wait_until_still(0.8)      # a cast pressed while still rolling fails
            m2 = self.g.player()
            moved = math.dist((me.x, me.y), (m2.x, m2.y))
            if moved < 5 and dist >= 8:
                # a fence or haystack the navmesh does not know: "stepped
                # back 2 yd" and 10 s in melee. Try the other way while
                # the root still holds.
                bad = math.atan2(p[1] - me.y, p[0] - me.x)
                p2 = self.retreat_point(m2, enemies, dist, avoid_dir=bad)
                if p2:
                    self.walk_to(*p2, timeout=3)
                    self.wait_until_still(0.8)
                    m2 = self.g.player()
                    moved = math.dist((me.x, me.y), (m2.x, m2.y))
            _log(f"    stepped back {moved:.0f} yd, nearest enemy "
                 f"{min((m2.dist(e) for e in enemies), default=0):.0f} yd")
        else:
            _log("    no spot to step back to")

    def sheepable(self, u: Unit) -> bool:
        """Polymorph only takes beasts and humanoids."""
        ci = worlddb.creatures({u.entry}).get(u.entry)
        return ci is None or ci.type in (1, 7)

    def run_until_clear(self, enemies, limit=40.0):
        """After an escape: keep moving away from whatever still chases until
        the combat flag drops (or a sheep breaks and catches us anyway)."""
        guids = {e.guid for e in enemies}
        t0 = time.time()
        while time.time() - t0 < limit and not self.stopped():
            me = self.g.player()
            if me.dead or not me.in_combat:
                break
            foes = [u for u in self.g.units() if u.guid in guids and not u.dead and me.dist(u) < 80]
            if not foes:
                break
            p = self.retreat_point(me, foes, 30.0)
            if not p:
                break
            self.navigate(*p, stop_short=2.0, timeout=10, combat_ok=True)
        me = self.g.player()
        _log(f"  {'clear of combat' if not me.in_combat else 'still in combat'} at {me.hp_pct:.0f}% hp - resting")

    def escape(self, me, enemies) -> bool:
        # the server lets a creature evade once its victim is more than
        # ThreatRadius (50 yd in mangosd.conf) from where the fight started:
        # a rooted or sheeped mob gives us the 8-20 s to get 60 yd away
        p = None
        for dist in (60.0, 48.0, 36.0):
            p = self.retreat_point(me, enemies, dist)
            if p:
                break
        if not p:
            return False
        _log(f"  ESCAPING at {me.hp_pct:.0f}% hp from {len(enemies)} enemies, {dist:.0f} yd")
        _state("escaping")
        t0 = time.time()
        start = (me.x, me.y)
        self.ctm.move_to(*p)                     # straight line first: no re-planning delays
        while time.time() - t0 < 14 and not self.stopped():
            time.sleep(0.3)
            me = self.g.player()
            if me.dead or math.dist((me.x, me.y), p[:2]) < 3:
                break
            if time.time() - t0 > 2.5 and math.dist((me.x, me.y), start) < 3:
                # not moving (a river bank): let the navmesh find the way
                self.navigate(*p, stop_short=2.0, timeout=10, combat_ok=True)
                break
        me = self.g.player()
        chasing = [e for e in self.enemies(me) if not self.controlled(e) and me.dist(e) < 10]
        return not chasing

    # ------------------------------------------------------------ potions
    def heal_potions(self) -> list[tuple[int, int, str]]:
        """(entry, required level, name) of every healing potion and healthstone,
        best first. One query per run."""
        if "_heal_pots" not in self.__dict__:
            rows = worlddb._query(
                "SELECT entry, MAX(required_level), MAX(name) FROM item_template WHERE class = 0 "
                "AND (name LIKE '%Healing Potion%' OR name LIKE '%Healthstone%') "
                "GROUP BY entry")
            self._heal_pots = sorted(((int(e), int(l), n) for e, l, n in rows),
                                     key=lambda r: -r[1])
        return self._heal_pots

    def drink_potion(self, me) -> bool:
        """Drink the best healing potion we carry and may use. True if one
        was used."""
        if time.time() - getattr(self, "_potion_at", 0.0) < POTION_COOLDOWN:
            return False
        bag = self.g.bag_items()
        best = next(((e, n) for e, lvl, n in self.heal_potions()
                     if bag.get(e, 0) > 0 and lvl <= me.level), None)
        if best is None:
            return False
        pot, pot_name = best
        v = spells.action_bar(self.p)[POTION_SLOT - 1]
        if not (v & 0x80000000 and v & 0x7FFFFFFF == pot):
            self.bridge.send(br.PLACE_ITEM, pot * 100 + POTION_SLOT, wait=0.5)
        self.bridge.send(br.USE_SLOT, POTION_SLOT, wait=0.3)
        self._potion_at = time.time()
        after = self.g.bag_items().get(pot, 0)
        _log(f"  {me.hp_pct:.0f}% hp - drinking {pot_name}"
             + ("" if after < bag[pot] else " (the count did not drop - it may not have gone off)"))
        return True

    # ------------------------------------------------------------ the fight
    PARTY_LEVELS = 3                 # a mob this far above us turns any fight into a death
    ELITE_LEVELS = 4                 # what an elite is worth on top of its own level
    PARTY_RADIUS = 30.0
    NOVA_RADIUS = 10.0        # Frost Nova is a PBAoE: it roots nothing further out

    def roaming_party(self, me):
        """Hostiles far above our level standing close by, not yet on us.

        A level 19-21 hunting party wanders south Loch Modan. Stopping to
        fight a level 13 chaser 8 yd from it killed a level 16 mage in 3 s:
        all three joined the moment the first bolt landed.
        """
        out = []
        for u in self.g.units():
            if u.dead or u.npc_flags or u.level < me.level + self.PARTY_LEVELS:
                continue
            if me.dist(u) > self.PARTY_RADIUS:
                continue
            if factions.is_friendly(me.faction, u.faction):
                continue
            out.append(u)
        return out

    # ------------------------------------------------------ cost measurement
    def _sample_begin(self, t: Unit) -> None:
        """Start measuring what this mob costs us (mobstats)."""
        self._fight_mob = (getattr(t, "entry", 0), getattr(t, "level", 0))
        self._fight_t0 = time.time()
        self._fight_sampled = False
        self._fight_engaged = False
        try:
            self._fight_hp0 = self.g.player().hp_pct
        except Exception:
            self._fight_hp0 = 100.0

    def _sample_end(self) -> None:
        """Record a fight that did NOT end in a kill.

        An escape or a death is a measurement, not a missing one: the mob
        that takes 90% of our health and then kills us never writes a kill
        record, so pricing mobs from wins alone makes every mob look cheap
        and the pull rules built on those prices reckless.
        """
        if self.__dict__.get("_fight_sampled") or not self.__dict__.get("_fight_engaged"):
            return                       # killed it (already recorded), or never engaged
        st = self.mobstats
        mob = self.__dict__.get("_fight_mob")
        if st is None or not mob or not mob[0]:
            return
        try:
            me = self.g.player()
            st.record(mob[0], mob[1], me.level,
                      max(0.0, self._fight_hp0 - me.hp_pct),
                      time.time() - self._fight_t0,
                      "death" if (me.dead or me.ghost) else "escape")
        except Exception:
            pass
        self._fight_sampled = True

    def fight(self, target: Unit, reason: str = "") -> None:
        """Fight one mob, and record what it cost however it ends."""
        self._sample_begin(target)
        try:
            self._fight_one(target, reason)
        finally:
            self._sample_end()

    def _fight_one(self, target: Unit, reason: str = "") -> None:
        """Fight one mob. "reason" is what the kill is FOR, and it goes in
        the log line: "attacking Ragged Young Wolf" reads as aimless
        grinding even when the wolf is eight-eighths of a quest objective,
        and that is exactly how it was read."""
        prof = self.profile
        self._fight_reason = reason
        me = self.g.player()
        party = self.roaming_party(me)
        if party and target.level < me.level + self.PARTY_LEVELS and prof.can_escape:
            names = ", ".join(f"{worlddb.name(u.entry)} {u.level}" for u in party[:3])
            _log(f"  not fighting here - {names} within {self.PARTY_RADIUS:.0f} yd; running instead")
            self.escape(me, party + [target])
            return
        # An elite is worth roughly four levels of its own. Hogger (level 11,
        # ELITE) took this level-12 paladin from 100% to dead in 83 s and
        # never dropped below 36% himself, and nothing in the engine noticed,
        # because on level alone he reads as an easy fight - one BELOW us, so
        # even the "3 levels above us - running" gate stayed silent. The prey
        # filter in bot.py already skips elites, but the quest and item
        # farming paths call fight() directly and bypass it entirely; this
        # one was walked to from 50 yd away ("attacking Hogger (lvl 11) at
        # 50.1 yd - for item 2589"). Refuse it here, where every caller goes.
        _ci = worlddb.creatures({target.entry}).get(target.entry)
        if _ci is not None and _ci.elite and target.level + self.ELITE_LEVELS > me.level:
            _log(f"  {worlddb.name(target.entry)} is elite (lvl {target.level}) - "
                 f"worth ~{self.ELITE_LEVELS} levels more than it reads; not soloing it")
            self.blacklist[target.guid] = time.time() + 1800
            return
        _log(f"attacking {worlddb.name(target.entry)} (lvl {target.level}) "
             f"at {me.dist(target):.1f} yd"
             + (f" - for {reason}" if reason else ""))
        _state("fighting", detail=worlddb.name(target.entry))
        primary = target.guid
        self.set_target(primary)
        t_start = time.time()
        # a closing-in record left from an earlier fight is not this one's:
        # "not getting any closer to Wendigo (8 yd for 213 s)" dropped a mob
        # at 8 yd the moment we engaged it, and it joined the next pull (09:56)
        self._approach = None
        last_hp, last_progress = target.health, time.time()
        # Latency to the first hit, from the moment we stand in melee range.
        # It should be about one swing; a long one is a bug (auto-attack
        # left off, a target the server never heard of) that no other
        # number shows: fights still end in a kill, just slower.
        t_melee = first_hit = None
        failed = 0
        cc_tried: dict[int, float] = {}
        escaped = False
        escapes = 0
        bail_pending = False
        sheep_escaped = False
        seen = {primary}
        # How many were already on us when the fight began. A pack is learned
        # from a PULL that brought friends, not from a fight we walked into:
        # three Defias Thugs met on the vineyard road were "packs - not
        # pulling them for a day", and the thugs were the Brotherhood of
        # Thieves objective, so that chain went too.
        started_with = sum(1 for u in self.threats()
                           if not u.dead and u.target == me.guid and u.guid != primary) + 1
        opened = False                  # the opener fires once per fight
        auto_attack_on = None
        hp0, mana0, casts, misses, melee_s, t_prev = me.hp_pct, me.power, 0, 0, 0.0, time.time()

        while not self.stopped():
            me = self.g.player()
            if me.dead or me.ghost:
                return
            if me.in_combat:
                # blows were exchanged: from here an escape or a death is a
                # real cost sample, where a fight called off before contact
                # (the roaming-party check above) is not one at all
                self._fight_engaged = True
            t = self.g.unit(primary)
            if t is not None and not t.dead and not prof.ranged and me.dist(t) > MELEE_RANGE + 2:
                # our target is out of reach and something else is hitting
                # us: fight that one. The warrior chased a Crag Boar 14-19 yd
                # off while a Juvenile Snow Leopard clawed its back (15:26)
                on_us = [e for e in self.enemies(me) if e.guid != primary and not e.dead
                         and e.target == me.guid and me.dist(e) <= MELEE_RANGE]
                if on_us:
                    t = min(on_us, key=me.dist)
                    primary = t.guid
                    seen.add(primary)
                    last_hp, last_progress = t.health, time.time()
                    auto_attack_on = None
                    self._fight_mob = (t.entry, t.level)      # cost it against the mob we end on
                    self.set_target(primary)
                    _log(f"  switching to {worlddb.name(t.entry)} - it is on us, the target is not")
            if t is None or t.dead:
                if t is not None:
                    self.kills += 1
                    self.last_fight_end = time.time()
                    events.emit("kill", mob=worlddb.name(t.entry), entry=t.entry, mlvl=t.level,
                                secs=round(time.time() - t_start, 1), hp0=round(hp0),
                                hp1=round(me.hp_pct), lvl=me.level, xp=me.xp, why=reason)
                    # ... and into mobstats, which is what actually reads it
                    st = self.mobstats
                    if st is not None:
                        st.record(t.entry, t.level, me.level, max(0.0, hp0 - me.hp_pct),
                                  time.time() - t_start, "kill", adds=max(0, len(seen) - 1))
                    self._fight_sampled = True
                    _log(f"  {worlddb.name(t.entry)} died ({time.time() - t_start:.0f}s). "
                         f"kills={self.kills} xp {me.xp}/{me.next_xp}")
                    _log(f"    fight: {casts} casts ({misses} failed), {melee_s:.0f}s in melee, "
                         f"hp {hp0:.0f}->{me.hp_pct:.0f}%, mana {mana0}->{me.power}")
                    _cl = self.__dict__.pop("_cd_late", {})
                    _ss = self.__dict__.pop("_send_s", [])
                    if _ss:
                        _cl["(key send)"] = _ss
                    if _cl:
                        _log("    cooldowns pressed late: " + ", ".join(
                            f"{k} {sum(v) / len(v):.2f}s avg over {len(v)}" for k, v in _cl.items()))
                rest = [e for e in self.enemies(me) if e.guid != primary]
                # a sheeped add wanders off with no target, so enemies() misses
                # it: the fight returned at 30% hp, we could not rest (still
                # in combat) and re-engaged it at 4 yd when the sheep broke
                have = {e.guid for e in rest}
                rest += [u for u in self.g.units() if u.guid in seen and u.guid != primary
                         and u.guid not in have and not u.dead and me.dist(u) < 45]
                if not rest:
                    return
                rest.sort(key=lambda e: (self.controlled(e), e.health))
                primary, t = rest[0].guid, rest[0]
                last_hp, last_progress = t.health, time.time()
                # a new mob, a new cost record: what the next one costs is
                # measured from here, not from the start of the first fight
                # (the kill record used to charge mob two for mob one's
                # health as well, which priced every add as lethal)
                hp0, t_start = me.hp_pct, time.time()
                self._fight_mob, self._fight_hp0 = (t.entry, t.level), me.hp_pct
                self._fight_t0, self._fight_sampled = time.time(), False
                _log(f"  next: {worlddb.name(t.entry)}"
                     + (" (crowd-controlled)" if self.controlled(t) else ""))
                nova_up = any(r.name == "Frost Nova" and self.usable(r) for r in prof.rotation)
                strong = t.level >= me.level + 3 and not self.sheepable(t) and t.hp_pct > 60
                # Chaining into a FRESH mob on the fumes of the last fight is
                # certain death, and at this health the "only with a head
                # start" rule below stops protecting us and starts killing us:
                # staying loses with probability 1, running merely usually
                # fails. Deaths 13 and 14 were the same five seconds - the
                # first fight won down to 37% and to 13% hp, then "next:
                # <mob>" with Divine Protection and Lay on Hands both already
                # spent, then a corpse. `pick() is None` is the honest test
                # for "nothing left": it is what the emergency list itself
                # says when no entry can fire.
                if (not self.controlled(t) and prof.can_escape and not escaped
                        and me.hp_pct < EMERGENCY_HP
                        and self.pick(prof.emergency, me, t, active) is None):
                    _log(f"  chaining into {worlddb.name(t.entry)} at {me.hp_pct:.0f}% hp "
                         "with no emergency left - breaking off instead")
                    escaped = True
                    escapes += 1
                    if self.escape(me, [t]):
                        self.blacklist[t.guid] = time.time() + 120
                    self.run_until_clear([t])
                    self.need_full_rest = True
                    return
                if (not self.controlled(t) and prof.can_escape and not escaped and me.dist(t) > 9
                        and ((not nova_up and not self.sheepable(t) and me.hp_pct < 50 and t.hp_pct > 50)
                             or strong)):
                    # (only with a head start: with the mob already on us the
                    # 60 yd run has ended in death every time today)
                    # the second harvest golem at 87% with us at 39% and no
                    # root for 11 s: trading in melee was a death - run now,
                    # the mob gives up 50 yd from where the fight started
                    _log(f"  not fit to fight it (hp {me.hp_pct:.0f}%, it has {t.hp_pct:.0f}%, no root)"
                         " - running now")
                    escaped = True
                    escapes += 1
                    if self.escape(me, [t]):
                        self.blacklist[t.guid] = time.time() + 120
                        self.run_until_clear([t])
                        self.need_full_rest = True
                        return
                if self.controlled(t) and prof.can_escape and not escaped                         and (me.hp_pct < 60 or me.mana_pct < 45):
                    # a sheeped goretusk at 91% with 33% mana left is not a
                    # fight we win: leave while the sheep still holds.
                    #
                    # But the sheep has to outlast the run. At 04:57 the mage
                    # left at 34% hp on a 16 s old Polymorph (rank 1 lasts
                    # ~20 s); it broke mid-flight and the crocolisk killed it
                    # from behind - 34% -> 0% in 18 s, "clear of combat at 0%".
                    # Top the sheep up first when there is time to.
                    sent = getattr(self, "_cc_sent", None)
                    age = (time.time() - sent[1]) if (sent and sent[0] == t.guid) else 99.0
                    cc = prof.crowd_control
                    if (age > self.CC_FRESH and cc and self.usable(cc)
                            and self.affordable(cc, me) and me.dist(t) > 9
                            and self.sheepable(t)):
                        _log(f"  refreshing {cc.name} before running "
                             f"(the sheep is {age:.0f}s old)")
                        self.cast(cc, t)
                        me = self.g.player()
                    _log(f"  not fit to fight it (hp {me.hp_pct:.0f}%, mana {me.mana_pct:.0f}%)"
                         " - leaving while it is sheeped")
                    escaped = True
                    escapes += 1
                    if self.escape(me, [t]):
                        self.blacklist[t.guid] = time.time() + 120
                        self.run_until_clear([t])
                        self.need_full_rest = True
                        return
                continue
            if t.tapped_by_other and t.target != me.guid:
                _log("  someone else tagged it - giving up")
                self.blacklist[t.guid] = time.time() + 300
                return
            if (not prof.ranged and t_melee is None and t.guid == primary
                    and me.dist(t) <= 5.0):
                t_melee = time.time()
            if t.health < last_hp:
                if first_hit is None and t_melee is not None and t.guid == primary:
                    first_hit = time.time() - t_melee
                    events.emit("first_hit", mob=worlddb.name(t.entry), secs=round(first_hit, 1),
                                engaged_by=("us" if reason else "them"))
                    if first_hit > 4.0:
                        _log(f"  SLOW START: first hit {first_hit:.1f} s after reaching melee range")
                last_hp, last_progress = t.health, time.time()
            # 6 s with no hit yet is a start that did not take; once hits
            # land, a 3.6 s two-hander plus two misses is a normal 7-8 s gap,
            # and cycling auto-attack off and on then only costs the swing
            # not while still walking in: a 55 yd pull tripped it at 21 yd,
            # "swinging at 0x0" (10:22) - the clock starts at melee range
            if (not prof.ranged and (t_melee is not None or me.dist(t) <= 8.0)
                    and time.time() - max(last_progress, t_melee or 0) > (6 if first_hit is None else 10)
                    and time.time() - getattr(self, "_closed_in", 0) > 5):
                # also at melee range: standing on the mob it can be under the
                # camera, the click misses and nothing swings (start_attack
                # steps back for that); this watchdog used to skip <= 2.5 yd
                # swinging at air: a Snow Leopard 4.5 yd away (a step up the
                # slope) took no damage for minutes while it clawed us, and
                # "blacklisting" it only re-engaged it. Step right onto it.
                self._closed_in = time.time()
                _log(f"  no damage landing at {me.dist(t):.1f} yd - closing in and attacking again "
                     f"(swinging at {'it' if self.server_target() == t.guid else hex(self.server_target())}, "
                     f"facing off {abs(me.facing_error(t)):.2f}, dz {t.z - me.z:+.1f}, "
                     f"flat {math.dist((me.x, me.y), (t.x, t.y)):.1f} yd, it targets "
                     f"{'us' if t.target == me.guid else hex(t.target)})")
                if me.dist(t) > 4.0:
                    # to 3 yd, not onto it: standing on the mob is exactly
                    # when the click misses (a 25 s stall at 1.2 yd, 15:59)
                    self.approach(me, t, 3.5)
                elif me.dist(t) < 2.0:
                    p = self.retreat_point(me, [t], 3.0)
                    if p:
                        self.walk_to(*p, timeout=1.5)
                        self.face(self.g.unit(t.guid) or t)
                if prof.melee_auto_attack:
                    self.face(self.g.unit(t.guid) or t, tolerance=0.2)
                    self.start_attack(self.g.unit(t.guid) or t, force=True)
                continue
            if time.time() - last_progress > 25:
                np = self.__dict__.setdefault("_no_progress", {})
                n_np = np.get(t.guid, (0, 0))
                n_np = (n_np[0] + 1 if time.time() - n_np[1] < 180 else 1, time.time())
                np[t.guid] = n_np
                if n_np[0] >= 2 and me.in_combat and t.target == me.guid:
                    # it hits us, we cannot hit it (at 1 hp on this server our
                    # swings stopped landing): re-engaging every 25 s went on
                    # for 8 minutes. Run until it leashes home (50 yd).
                    _log(f"  {worlddb.name(t.entry)}: no damage twice running - running until it leashes")
                    self.blacklist[t.guid] = time.time() + 600
                    self.escape(me, [t])
                    self.run_until_clear([t], limit=60.0)
                    self.need_full_rest = True
                    return
                _log("  no progress for 25 s - blacklisting target")
                self.blacklist[t.guid] = time.time() + 300
                self.stop()
                if me.hp_pct < 50 and self.profile.can_escape:
                    # an unreachable (evading) scout while its friends hit us:
                    # looting and pulling again at 24% hp was a death
                    _log(f"  ... and at {me.hp_pct:.0f}% hp - getting clear first")
                    self.escape(me, [e for e in self.enemies(me, primary) if e.guid != t.guid] or [t])
                    self.need_full_rest = True
                return

            enemies = self.enemies(me, primary)
            self._all_enemies = enemies
            active = [e for e in enemies if not self.controlled(e)]
            now = time.time()
            if any(me.dist(e) < 5 for e in active):
                melee_s += now - t_prev
            t_prev = now
            if now - t_start > 8 and now - getattr(self, "_fight_log_t", 0) > 8:
                # a death leaves no fight summary: two scouts took a level-15
                # mage from 100% to 41% in 22 s with nothing in the log
                self._fight_log_t = now
                _log(f"    fight status: {worlddb.name(t.entry)} {t.hp_pct:.0f}% at {me.dist(t):.0f} yd"
                     f"{' (controlled)' if self.controlled(t) else ''}, us {me.hp_pct:.0f}% hp {me.mana_pct:.0f}% mana, "
                     f"{casts} casts ({misses} failed), {len(active)} active of {len(enemies)}")
            # diagnostics: how long does a sheep hold, and what breaks it?
            ctrl = self.__dict__.setdefault("_ctrl_since", {})
            for e in enemies:
                if self.controlled(e):
                    ctrl.setdefault(e.guid, (now, e.hp_pct))
                elif e.guid in ctrl:
                    t_c, hp_c = ctrl.pop(e.guid)
                    _log(f"    control on {worlddb.name(e.entry)} ended after {now - t_c:.0f}s "
                         f"(hp {hp_c:.0f}->{e.hp_pct:.0f}%, {me.dist(e):.0f} yd, client target "
                         f"{'it' if self.g.target_guid() == e.guid else 'other'})")
            bail = False
            for e in enemies:
                if e.guid not in seen:
                    seen.add(e.guid)
                    _log(f"  + {worlddb.name(e.entry)} (lvl {e.level}) joined at {me.dist(e):.0f} yd"
                         f" - hp {me.hp_pct:.0f}%")
                    # two mobs of our level is a lost fight for this mage
                    # unless one can be sheeped: leave while the first is
                    # still healthy instead of dying at 20% of its health
                    # ... but two rusty golems three levels below us are fine.
                    # The code said two levels and the comment said three, and
                    # the code was wrong: two level-15 Loch Crocolisks took a
                    # level-17 mage from 100% to dead in 24 s at 04:30, with
                    # this rule disabled because 15 <= 17 - 2. Match the
                    # comment - a 2-level gap is not a weak pair.
                    weak = all(x.level <= me.level - 3 for x in active)
                    # The sheepable exemption assumes we can Polymorph our
                    # way out. You cannot sheep three: at 05:20 three Loch
                    # Crocolisks took a level-17 mage from 94% to dead in
                    # 21 s while this rule sat out because crocolisks are
                    # beasts. Two is still a sheep problem; three is a rout.
                    if (len(active) >= 2 and t.hp_pct > 60 and not escaped
                            and not weak and prof.can_escape
                            and (len(active) >= 3 or not self.sheepable(e))):
                        bail = True
            if bail_pending and not escaped:
                bail = True
            if bail:
                a = self.pick(prof.emergency, me, t, active)
                in_melee_now = any(me.dist(e) < 9 for e in active)
                if in_melee_now and a is None and len(active) < 3:
                    # running from two golems already hitting us without a
                    # root was a death at 70% hp: fight on until Frost Nova
                    # is ready, then go
                    if not bail_pending:
                        _log("  two unsheepable mobs on us - no root ready, fighting until it is")
                    bail_pending, bail = True, False
                elif in_melee_now and a is None and me.hp_pct > ROUT_HP:
                    # 97% hp is not a rout. `a is None` here means no
                    # emergency ability's CONDITION fired, which at full
                    # health means nothing is wrong yet - not that we are out
                    # of tools. The branch below reads it as the latter and
                    # runs. The rule it encodes was learned on a mage (cloth,
                    # no heals), where running is right; for a plate class
                    # with self-heals it throws away a fight it would likely
                    # win, because running hands three melee mobs free swings
                    # while we deal nothing back. Four deaths this run -
                    # 15:58, 16:15, 16:45 and 17:09 - all read "ESCAPING from
                    # 3 enemies" at 51-97% hp and then a corpse 13-20 s later,
                    # and not one of those escapes broke away. Fight now and
                    # keep bail_pending armed, so we still leave the moment we
                    # are genuinely losing.
                    if not bail_pending:
                        _log(f"  {len(active)} mobs on us at {me.hp_pct:.0f}% hp - "
                             "fighting rather than handing them a free run")
                    bail_pending, bail = True, False
                elif in_melee_now and a is None:
                    # ...but waiting only works if we outlive the cooldown.
                    # At 05:44 three Loch Crocolisks took us 93% -> 1% in
                    # 17 s while we waited on a 25 s Frost Nova. Against
                    # three, running hurt beats standing dead.
                    _log(f"  {len(active)} mobs on us and no root for "
                         "another cooldown - running without one")
                    bail_pending = False
                    escaped = True
                    escapes += 1
                else:
                    _log("  two unsheepable mobs on us - breaking off early")
                    bail_pending = False
                    if a and in_melee_now:
                        _log(f"  emergency: {a.name}" + ("" if self.cast(a, t) else " (did not go off)"))
                    me = self.g.player()
                    if (prof.melee_auto_attack and not self.has("Frost Nova")
                            and (me.hp_pct > ROUT_HP
                                 or (prof.rest_heal is not None and me.mana_pct >= 25))):
                        # a melee class runs no faster than what is hitting
                        # it, and nothing roots them: the run is free swings
                        # at our back. Ddqr ran at 71% hp with 96% mana
                        # (18:03), met a fourth Wendigo and died at 24% hp
                        # with the mana for four Holy Lights unspent. While
                        # the heals last, heal and fight; bail_pending still
                        # lets us go once we are genuinely out of tools.
                        if time.time() - self.__dict__.get("_fight_on_log", 0) > 10:
                            self._fight_on_log = time.time()
                            _log(f"  {len(active)} on us, {me.hp_pct:.0f}% hp {me.mana_pct:.0f}% mana"
                                 " - nothing to break away with, fighting on")
                        bail_pending, bail = True, False
                    # Frost Nova only roots what stands inside 10 yd. Running
                    # from a mob it never touched is how 25 of 90 deaths
                    # happened: at 08:47 the nova held a Dire Condor while a
                    # Black Dragon Whelp 16 yd out chased us down in 4 s.
                    # Escaping one enemy above 40% never killed us; escaping
                    # with something loose is where it goes wrong.
                    me = self.g.player()
                    loose = [e for e in active if me.dist(e) > self.NOVA_RADIUS]
                    if not bail:
                        pass
                    elif loose and 20 <= me.hp_pct < 50:
                        _log(f"  {len(loose)} of {len(active)} still loose outside the "
                             f"root at {me.hp_pct:.0f}% hp - fighting instead of running")
                        bail = False
                    else:
                        escaped = True
                        escapes += 1
            if bail:
                if self.escape(me, enemies):
                    for e in enemies:
                        self.blacklist[e.guid] = time.time() + 120
                    self.run_until_clear(enemies)
                    self.need_full_rest = True
                    return
            d = me.dist(t)
            # Without Frost Nova or Polymorph a pair is already the pack that
            # kills us: three murloc pairs killed the level-7 mage in 25 min
            # (05:19-05:43) and none reached the 3-at-once bar
            kinds = {}
            for e in active:
                kinds[e.entry] = kinds.get(e.entry, 0) + 1
            pair_pack = (self.profile.ranged and len(active) >= 2 and max(kinds.values()) >= 2
                         and not any(self.has(sp) for sp in ("Frost Nova", "Polymorph")))
            if (len(active) >= 3 or pair_pack) and not escaped and started_with <= 1:
                # a kind that comes in packs (murlocs): pulling one brings
                # three - leave that kind alone for a while unless it comes for us.
                # Not after an escape that ran into more mobs, and not the
                # level-9 golems of the pumpkin patch (a day without the
                # grind stage's own mobs)
                for e in active:
                    if e.level < me.level - 1:
                        continue
                    if self.named_spawn({e.entry}):
                        continue           # Grik'nir joining a whelp pull is not a pack of Grik'nirs
                    if e.entry not in self.pack_mobs:
                        _log(f"  {worlddb.name(e.entry)}s come in packs - not pulling them for a day")
                    self.pack_mobs[e.entry] = time.time() + 86400   # a day, or 2 levels
                    self.pack_level[e.entry] = me.level
                self.save_packs()

            # A healing potion below 20% hp, if we carry one. Potions are off
            # the global cooldown, so this never costs a rotation cast.
            if me.hp_pct < POTION_HP and self.drink_potion(me):
                continue

            # 0. a mob far above us (a level-14 harvest watcher at 11): do
            # not even try - root it and get away at once
            if not escaped and prof.can_escape and t.level >= me.level + 3                     and all(e.level >= me.level + 3 for e in active):
                _log(f"  {worlddb.name(t.entry)} is {t.level - me.level} levels above us - running")
                a = self.pick(prof.emergency, me, t, active)
                if a and any(me.dist(e) < 9 for e in active):
                    self.cast(a, t)
                escaped = True
                escapes += 1
                # A failed escape used to fall straight back into the fight
                # loop - which means trading blows with a mob 3+ levels up,
                # the one fight we have already decided we cannot win. That
                # was death #1 on the paladin: escape() returned False from a
                # level-16 Tarantula at 11 (a paladin has no root to buy the
                # run, so the spider simply followed and `chasing` was never
                # empty), the loop carried on swinging, and 100% hp became a
                # corpse in 19 s. Keep running instead. run_until_clear waits
                # out the leash, which is the only way this ends well.
                for _ in range(3):
                    if self.escape(self.g.player(), enemies):
                        break
                    if self.stopped() or self.g.player().dead:
                        break
                for e in enemies:
                    self.blacklist[e.guid] = time.time() + 600
                self.run_until_clear(enemies)
                self.need_full_rest = True
                return
            # 1. emergency: losing, and escape can actually work.
            # Not from a single mob well below us with mana in the tank: a
            # level-10 coyote at 22% hp is three casts inside one Frost Nova,
            # while a 60 yd run from it was death #2 (it outran the root).
            weak_single = (len(enemies) == 1 and t.level <= me.level - 2
                           and me.mana_pct >= 35 and me.hp_pct >= 12
                           and (t.hp_pct <= 2.2 * me.hp_pct      # 87% golem vs our 39%: not "weak"
                                or t.level <= me.level - 3))     # a scout 4 levels down: running from
                                                                 # it at 40% was death 2 of 4 today
            if me.hp_pct < EMERGENCY_HP and not weak_single                     and (not escaped or (escapes < 2 and len(active) >= 2)) and (
                    len(active) >= 2 or t.hp_pct > me.hp_pct + 20
                    or me.hp_pct < DESPERATE_HP
                    or (t.level > me.level and t.hp_pct > 35)):
                # last clause: a level-15 harvest watcher at 14 traded blows
                # from 67% and won - a higher-level mob with a third of its
                # health left out-damages us: sheep it and break off
                a = self.pick(prof.emergency, me, t, active)
                if a:
                    _log(f"  emergency: {a.name}")
                    self.cast(a, t)
                    continue
                close = [e for e in active if me.dist(e) < 9]
                cc = prof.crowd_control
                if close and cc and len(active) == 1 and not sheep_escaped                         and self.usable(cc) and self.sheepable(close[0]):
                    # Frost Nova is down and one mob is eating us: sheep it
                    # (it heals up, we live) and get away from it
                    sheep_escaped = True
                    _log(f"  emergency: {cc.name} on {worlddb.name(close[0].entry)} to break off")
                    if self.cast(cc, close[0]):
                        close = []
                if prof.can_escape and close:
                    # nothing to root or sheep them with and they are on us:
                    # running was tried 7 times today (troggs, buzzards, a
                    # crocolisk) and ended in death every single time - the
                    # mob keeps hitting us in the back for 60 yd. Fighting on
                    # at least lands a few more Fireballs.
                    if time.time() - getattr(self, "_no_root_said", 0) > 20:
                        self._no_root_said = time.time()
                        _log("  no root or sheep available - fighting on")
                if prof.can_escape and not close:
                    escaped = True
                    escapes += 1
                    if self.escape(me, enemies):
                        for e in enemies:                 # do not walk back into them
                            self.blacklist[e.guid] = time.time() + 90
                        self.run_until_clear(enemies)
                        self.need_full_rest = True
                        return

            # 2. crowd control for extra attackers - but not while something
            # is hitting us: every melee hit pushes the 1.6 s cast back and
            # six sheep attempts in a row at 4 yd was a death at the murlocs.
            # Root them first (Frost Nova in the rotation), sheep from range.
            cc = prof.crowd_control
            in_melee = [e for e in active if me.dist(e) < 6]
            nova_ready = any(a.name == "Frost Nova" and self.usable(a) for a in prof.rotation)
            # ... and not while the primary is nearly dead: a goretusk at 27%
            # got a 4 s pushed-back sheep on its gnoll friend instead of the
            # one Fireball that would have ended it - we died at 0 escapes
            if cc and len(active) >= 2 and not (in_melee and nova_ready) and t.hp_pct >= 35                     and self.usable(cc) and self.conditions_hold(cc, me, t, active):
                # Polymorph only takes beasts (1) and humanoids (7): a Harvest
                # Golem shrugged off two sheep casts while a goretusk ate us
                types = worlddb.creatures({e.entry for e in active})
                extra = [e for e in active if e.guid != primary
                         and time.time() - cc_tried.get(e.guid, 0) > 3
                         and (e.entry not in types or types[e.entry].type in (1, 7))]
                if extra:
                    s = max(extra, key=lambda e: e.health)
                    self.face(s)
                    _log(f"  {len(active)} attackers - {cc.name} on {worlddb.name(s.entry)}")
                    cc_tried[s.guid] = time.time()
                    if self.cast(cc, s):
                        self._cc_sent = (s.guid, time.time())
                    else:
                        cc_tried[s.guid] = time.time() + 9      # pushed back: not again at once
                    if prof.melee_auto_attack:
                        # a written target does not move the swing: after a
                        # Hammer of Justice on the add, auto-attack stayed on
                        # the stunned mob (100 -> 63%) and we stood facing it
                        # 1.7 rad off the primary, which sat at 100% for 8 s
                        # while three hit us - death at 10:16. Turn back and
                        # switch the attack itself.
                        pu = self.g.unit(primary) or t
                        self.face(pu)
                        # forced: the target field names the primary again at
                        # once, so a plain start_attack only sent ATTACK_ON -
                        # which leaves the swing where it is (Dark Iron Spy
                        # still 88% 9 s after the stun, 10:23). Off, then on.
                        self.start_attack(pu, force=True)
                    else:
                        self.set_target(primary)
                    continue

            if not t.in_combat and t.target != me.guid and len(enemies) == 1:
                # about to open on a mob nobody has pulled yet: check its friends
                # BEFORE walking over - the pair mate aggroed on the approach
                gap = add_radius(me.level, t, me, prof.ranged)
                others = self.pair_mates(me, t)
                refused = self.__dict__.setdefault("_pair_refused", {})
                n_ref, t_ref = refused.get(t.guid, (0, 0.0))
                if time.time() - t_ref > 600:
                    n_ref = 0
                # two levels under us, a pair is cheaper than the wait: at the
                # Stolen Journal camp a level-6 paladin refused level-3/4
                # whelp pairs for three minutes (12:13-12:16), standing at
                # the edge - each one of them was a 10 s kill
                weak = (len(others) == 1 and t.level <= me.level - 2
                        and all(o.level <= me.level - 2 for o in others)
                        and me.hp_pct >= 80 and (prof.power != "mana" or me.mana_pct >= 40))
                if (others and (n_ref >= 1 or weak) and len(others) == 1
                        and all(o.level <= me.level + 1 for o in others)
                        and me.hp_pct >= (80 if weak else 90)
                        and (prof.power != "mana" or me.mana_pct >= (40 if weak else 50))):
                    # refused this pull once already: a quest mob that only
                    # ever stands with one friend (Grik'nir) left the bot at
                    # the camp edge for minutes, re-refusing every 90 s. One
                    # extra mob our level, taken at full health, is a fight
                    # every class wins; waiting for it to walk off is not.
                    _log(f"  {worlddb.name(t.entry)} still has a friend after {n_ref} waits "
                         "- taking both" if not weak else
                         f"  {worlddb.name(t.entry)} has a friend, both 2+ levels under us - taking both")
                    refused.pop(t.guid, None)
                    others = []
                if others:
                    refused[t.guid] = (n_ref + 1, time.time())
                    _log(f"  {worlddb.name(t.entry)} has {len(others)} friend(s) within "
                         f"{gap:.0f} yd now - leaving it for a while")
                    self.blacklist[t.guid] = time.time() + 90
                    for u in others:
                        # a pair is a pair from either side: the hunt picked
                        # the partner one second later and both came
                        self.blacklist[u.guid] = time.time() + 90
                    self.stop()
                    if d < 28:
                        # it was about to notice us anyway (a coyote "pulled
                        # before it reaches us" then reached us with its
                        # goretusk friend): get out of its aggro range first
                        _log("  backing away from the pair")
                        self.step_back(me, [t] + others, dist=20.0)
                        # it comes for us anyway (a coyote caught us at 4.5 yd
                        # six seconds later): open on it from here instead of
                        # walking on with our back to it
                        t_w = time.time()
                        while time.time() - t_w < 3.0:
                            time.sleep(0.2)
                            t2 = self.g.unit(t.guid)
                            if t2 is not None and not t2.dead and t2.target == self.g.player().guid:
                                _log("  it comes anyway - fighting it at range")
                                self.blacklist.pop(t.guid, None)
                                break
                        else:
                            return
                        continue
                    return
            # opener: Charge closes the gap itself and pays for the first
            # ability. It has to go BEFORE the approach - it only works out
            # of combat and only from 8-25 yd, and the approach walks us
            # straight through that band into melee.
            op = prof.opener
            if (op is not None and not opened and self.usable(op)
                    and not me.in_combat and not t.in_combat
                    and getattr(self, "_engaged", (None, 0))[0] != primary):
                # engage first, like a player: select the mob and turn to it
                # the moment we commit, then Charge when the range is right.
                # Selecting it only at Charge time cost the client's 1 s
                # target lag, and we walked inside 8 yd meanwhile. (No
                # auto-attack yet: AttackTarget() toggles in 1.12, and
                # Charge is refused once we count as in combat.)
                self.set_target(primary)
                self._engaged = (primary, time.time())
                self.face(t)
                # ... with a real left-click: a target written into client
                # memory is never told to the server, and auto-attack then
                # swung at nothing while the mob clawed us to 1 hp
                self.click_unit(t, right=False)
            if (op is not None and not opened and self.usable(op)
                    and not me.in_combat and not t.in_combat
                    and self.pick([op], me, t, active) is not None):
                opened = True
                eng, t_eng = getattr(self, "_engaged", (None, 0))
                if eng == primary:
                    # the client has had the target since we engaged: no
                    # second target-switch pause inside cast()
                    time.sleep(max(0.0, 1.0 - (time.time() - t_eng)))
                    self._last_cast_target = primary
                self.face(t)
                self.set_target(primary)
                _log(f"  {op.name} from {d:.0f} yd")
                went = self.cast(op, t)
                _log(f"    {op.name} " + ("landed" if went else
                     f"did NOT go off ({self.g.player().dist(self.g.unit(primary) or t):.0f} yd now)"))
                if went:
                    # Charge lands us in melee with the target stunned for a
                    # second - the best opening a warrior gets. Waiting for
                    # the next loop to notice we had arrived spent it
                    # standing still, so swing the moment we land: poll for
                    # the landing rather than sleep a fixed 0.45 s (an 8 yd
                    # Charge lands in 0.2 s, a 25 yd one in 0.8 s)
                    t_land = time.time()
                    while time.time() - t_land < 1.5:
                        me = self.g.player()
                        t2 = self.g.unit(primary) or t
                        if me.dist(t2) < 5.0 and not me.moving:
                            break
                        time.sleep(0.05)
                    if prof.melee_auto_attack:
                        self.start_attack(t)
                        auto_attack_on = primary
                    me = self.g.player()
                    continue
            # range / line of sight
            reach = prof.engage_range
            # melee swings land to ~5 yd; the approach parks 1-2 yd short of
            # its goal, so "not in combat yet" must accept 5 yd, not 3.5, or
            # it re-approaches forever and never swings (16:01-16:03)
            gate = reach + 1.5 if not prof.ranged else reach
            too_far = d > reach + 3 or (d > gate and not t.in_combat)
            if too_far:
                # Closing in that gains no ground is not a fight: at 15:59 the
                # warrior stood 20.8 s "fighting" a wolf 27 yd away (the mob
                # stood where the walk could not reach), waiting out the 25 s
                # no-progress timer. 4 s without getting 1 yd closer to a mob
                # that has not noticed us: leave it and take the next one.
                ap = getattr(self, "_approach", None)
                if ap is None or ap[0] != primary or d < ap[1] - 1.0:
                    self._approach = (primary, d, time.time())
                elif (time.time() - ap[2] > 4.0 and not t.in_combat
                      and t.target != me.guid and not me.in_combat):
                    _log(f"  not getting any closer to {worlddb.name(t.entry)} "
                         f"({d:.0f} yd for {time.time() - ap[2]:.0f} s) - leaving it")
                    self.blacklist[t.guid] = time.time() + 120
                    self._approach = None
                    self.stop()
                    return
                self.approach(me, t, reach)
                continue
            if me.moving or self.ctm.moving:
                self.stop()
            self.face(t)
            self.set_target(primary)
            if prof.melee_auto_attack and (
                    auto_attack_on != primary
                    or (self.server_target() != primary
                        and time.time() - getattr(self, "_attack_try", 0) > 1.5)):
                # re-checked every tick: the server not having the mob as our
                # target means no swings, and waiting 6 s for "no damage
                # landing" to notice was most of a level-1 fight
                if auto_attack_on == primary:
                    _log(f"    auto-attack not on {worlddb.name(t.entry)} - selecting it again")
                self.start_attack(t)
                auto_attack_on = primary

            # 3. rotation
            # A Fireball started at 12-15 yd on a mob running at us never
            # finishes: it arrives mid-cast and its hits push the cast past
            # the window. With the root ready, wait the second it takes to
            # get into Frost Nova range instead.
            nova = next((r for r in prof.rotation if r.name == "Frost Nova"), None)
            if (nova and self.usable(nova) and me.power >= 55 and not self.controlled(t)
                    and 8.5 < d < 16 and t.target == me.guid and t.moving
                    and len(active) == 1):
                time.sleep(0.15)
                continue
            a = self.pick(prof.rotation, me, t, active)
            if a is None:
                broke = (prof.power == "mana" and d <= 7.0
                         and not any(self.affordable(x, me) for x in prof.rotation))
                if prof.power == "mana" and (me.power < 25 or broke) and auto_attack_on != primary:
                    # out of mana: melee with whatever we hold
                    self.stand_up()
                    self.bridge.send(br.ATTACK)
                    auto_attack_on = primary
                # nothing to press (rage short, or inside the GCD): a short
                # look round - a tick is ~30 ms of reads, and a cooldown that
                # comes up is pressed on the next one
                time.sleep(0.1)
                continue
            casts += 1
            if self.cast(a, t):
                failed = 0
                if a.then == "step_back":
                    self.backpedal(enemies)
            elif prof.ranged and a.cast_time > 0:
                misses += 1
                m2 = self.g.player()
                _log(f"    cast of {a.name} failed: {d:.1f} yd, facing off {m2.facing_error(t):.2f}, "
                     f"mana {m2.power:.0f} (learned cost "
                     f"{self.__dict__.get('_spell_cost', {}).get(a.name, '?')}), "
                     f"at press moving/flags/ctm={getattr(self, '_press_state', None)} "
                     f"moving={m2.moving} ctm={self.ctm.moving} sitting={m2.sitting} "
                     f"target hp {t.hp_pct:.0f}% dead={t.dead} in_combat={m2.in_combat}")
                # No effect. Far away: line of sight or range -> get closer
                # around the obstacle. Close: the mob moved round us (facing)
                # or hits pushed the cast back -> face it squarely, then make
                # room instead of walking into it.
                failed += 1
                self.note_cast_failed(a, m2.power)
                if failed >= 2 and t.target != me.guid and not self.enemies(me):
                    # nothing is on us and it cannot be hit from here (a mob on
                    # the other mine level): walking over pulls its neighbours
                    _log(f"  no line of sight to {worlddb.name(t.entry)} and it has not "
                         "noticed us - leaving it")
                    self.blacklist[t.guid] = time.time() + 120
                    self.stop()
                    return
                if failed >= 2:
                    if d > 12:
                        # a thrower at 20 yd pushes every 2.5 s cast back for
                        # good: get inside 7 yd so it draws its knife, then
                        # Frost Nova and step back work as usual
                        _log(f"  casts failing at {d:.0f} yd - closing in")
                        self.navigate(t.x, t.y, t.z, stop_short=7.0, timeout=15,
                                      combat_ok=True)
                    else:
                        _log(f"  casts failing at {d:.0f} yd - facing and making room")
                        self.face(t, tolerance=0.15)
                        if failed >= 3:
                            self.step_back(me, enemies)
                    if failed >= 3:
                        failed = 0
