"""Resources: one model for "enough hp / mana" (review 2026-10-15, phase 3 step 1).

bot.py kept the lines as scattered fixed percentages: rest() starting below 85 / 80 and resting "until hp 90 mana 85",
rest_line()'s p90 + 15, ready_to_pull / chain_ready / the walked rest's pull line, the drink line (20%), the rest
heal's 30% mana, cast()'s mana gate, the heal-affordable checks (outmatched, the melee bolt floor "Healing Wave ~21%
of the pool"). Here every line is a multiple of a MEASURED cost when one is known, and the old number otherwise:

  kill cost   - p90 of the bot's measured one-mob fights at this level (Bot.fight_costs: (level, mana %)); fewer than
                MIN_FIGHTS: none (the defaults below).
  spell cost  - the best rank on the bar (rotation.Rotation.bar(s)[name][2]) as % of the pool.
  pull line   - mana >= K_KILL x kill cost + one heal (the old "+ 15" margin when the bar is unknown), clamped to
                PULL_LO..PULL_HI; hp >= PULL_HP. No measurements: REST_MANA 80 / REST_HP 85. Levels 1-5: LOW_PULL_MANA 45
                (a whole level 1-5 fight costs ~65%, 12:43:16-27). After a 2+ mob fight at least the flat line; a burst
                boss next: BOSS_*.
  rest target - the pull line + EXIT_MARGIN (hp 90 / mana 85 by default, 65 at levels 1-5), at most EXIT_HI.
  drink line  - below DRINK_HEALS heals' worth of mana, never above DRINK_BELOW 20% (self-healing classes heal after
                fights and drink only under 20% mana).
  rest heal   - between fights a heal while mana >= REST_HEAL_K heals; none when the class has no heal on the bar
                (it eats). The heal is the class's (nb/classes.py heal_name()); a class without mana has no mana lines.
  melee kill  - % hp a melee-only kill of a level-equal mob costs: the measured ones (Bot.melee_costs: (level, hp %),
                one-mob kills that spent < MELEE_ONLY_MANA mana), else MELEE_KILL_HP. threat.fresh_add counts it as
                capacity for a LONE attacker: bot 2 11:16:07 fled a single level 10 at 88% hp / 7% mana ("need 0.50 vs
                capacity 0.48") that the weapon kills.
"""

from __future__ import annotations

from collections import namedtuple

from nb import threat

# fallbacks (the old bot.py numbers)
REST_HP, REST_MANA = 85.0, 80.0           # pull line without measurements
EXIT_MARGIN_HP, EXIT_MARGIN_MANA = 5.0, 5.0   # rest target = pull line + this (old REST_EXIT_* 90 / 85)
EXIT_HI = 95.0
LOW_LEVEL, LOW_PULL_MANA, LOW_EXIT_MANA = 5, 45.0, 65.0
BOSS_HP, BOSS_MANA = 95.0, 90.0
HEAL_MARGIN = 15.0                        # % mana: "one heal" when the bar does not say (economy.mana_line's +15)
K_KILL = 1.0                              # pull line = K_KILL x the median kill cost + a heal
# (was the p90: a fight spends more when it starts with more (Earth Shock on cooldown above 40%), so the p90 + a heal
# ratcheted up to 80-90% at levels 7-8, run 6 2026-10-16 - the old flat line, 23 min of extra rest. The heal is the
# margin for a dearer fight; the mid-fight forecast and flee cover the rest)
PULL_LO, PULL_HI = 40.0, 90.0             # (economy.mana_line's clamp)
MIN_FIGHTS = 5
DRINK_BELOW = 20.0                        # % mana: never drink above this
DRINK_MIN = 10.0
DRINK_HEALS = 1.0                         # drink below this many heals' worth of mana
REST_HEAL_K, REST_HEAL_MANA = 1.5, 30.0   # heal between fights while mana >= 1.5 heals (else 30%)
BOLT_FLOOR, BOLT_FLOOR_HURT = 15.0, 26.0  # melee bolt floor; hurt / 2+ on us: a heal + this pad (old 26 = ~21 + 5)
BOLT_FLOOR_PAD = 5.0


def heal_name() -> str | None:
    """The class's self-heal (nb/classes.py; None: the class has none - food does it)."""
    from nb import classes
    return classes.PROFILE.heal


def uses_mana() -> bool:
    """The class's power is mana (False for rage / energy: no mana lines, no drinking)."""
    from nb import classes
    return classes.PROFILE.mana


# melee capacity (threat.fresh_add, a lone attacker)
MELEE_KILL_HP = 60.0                      # % hp one level-equal mob takes off us killed with the weapon alone
MELEE_ONLY_MANA = 10.0                    # % mana: a kill that spent less counts as melee-only
MELEE_MIN_N = 3
MELEE_PAD = 1.2                           # x the measured median: a margin on few samples
MELEE_LO, MELEE_HI = 30.0, 100.0

Lines = namedtuple("Lines", "pull_hp pull_mana exit_hp exit_mana boss src")


# ------------------------------------------------------------ pure helpers (rotation.py uses these too)
def spell_cost(bar, name) -> float:
    """Mana cost of `name` from a rotation bar dict (name -> (slot, id, cost)), 0 when unknown."""
    try:
        b = (bar or {}).get(name)
        return float(b[2]) if b and b[2] else 0.0
    except Exception:
        return 0.0


def can_afford(s, bar, name) -> bool:
    """cast()'s gate: True unless the bar knows the cost and our mana is below it."""
    c = spell_cost(bar, name)
    return not c or getattr(s, "mana", 0) >= c


def heal_ok(s, bar) -> bool:
    """A heal is on the bar and affordable now."""
    c = spell_cost(bar, heal_name())
    return bool(c and getattr(s, "mana", 0) >= c)


def pct_of_pool(s, cost) -> float | None:
    mm = getattr(s, "manamax", 0) or 0
    return 100.0 * cost / mm if cost and mm else None


def p90(costs) -> float:
    c = sorted(float(x) for x in costs)
    return c[min(len(c) - 1, max(0, -(-9 * len(c) // 10) - 1))]


class Resources:
    """costs()  -> [(level, mana % a one-mob fight cost)]     melee() -> [(level, hp % a melee-only kill cost)]
    bar(s)   -> the rotation's bar dict                      multi() -> the last fight had 2+ attackers
    boss(s)  -> a burst boss is the next pull                log(msg)"""

    def __init__(self, costs=lambda: (), bar=lambda s: {}, multi=lambda: False, boss=lambda s: False,
                 melee=lambda: (), log=None):
        self._costs, self._bar, self._multi, self._boss, self._melee = costs, bar, multi, boss, melee
        self.log = log or (lambda m: None)
        self._line_log = None

    # ---------------------------------------------------------------- measured costs
    def bar(self, s) -> dict:
        try:
            return self._bar(s) or {}
        except Exception:
            return {}

    def kill_costs(self, level) -> list:
        """The measured one-mob fight costs used for this level: this level's, or (under MIN_FIGHTS) the ones from
        one level down and up (as the old rest_line)."""
        cs = list(self._costs() or ())
        c = [x for lv, x in cs if lv == level]
        if len(c) < MIN_FIGHTS:
            c = [x for lv, x in cs if lv >= level - 1]
        return c

    def kill_mana(self, level):
        """(median mana % of a one-mob kill, samples) - None when fewer than MIN_FIGHTS are measured."""
        c = sorted(self.kill_costs(level))
        return (c[len(c) // 2] if len(c) >= MIN_FIGHTS else None), len(c)

    def spell_cost(self, s, name) -> float:
        return spell_cost(self.bar(s), name)

    def spell_pct(self, s, name) -> float | None:
        return pct_of_pool(s, self.spell_cost(s, name))

    def heal_pct(self, s) -> float | None:
        return self.spell_pct(s, heal_name())

    def affordable(self, s, name) -> bool:
        return can_afford(s, self.bar(s), name)

    def heal_ok(self, s) -> bool:
        return heal_ok(s, self.bar(s))

    def melee_kill_hp(self, level) -> float:
        """% hp a melee-only kill of a level-equal mob costs: PAD x the median of the measured ones at this level
        (or one off), else MELEE_KILL_HP."""
        ms = list(self._melee() or ())
        c = sorted(x for lv, x in ms if abs(lv - level) <= 1)
        if len(c) >= MELEE_MIN_N:
            return max(MELEE_LO, min(MELEE_HI, MELEE_PAD * c[len(c) // 2]))
        return MELEE_KILL_HP

    # ---------------------------------------------------------------- lines
    def drink_line(self, s) -> float:
        """Drink only below DRINK_HEALS heals' worth of mana, DRINK_MIN..DRINK_BELOW % (0: the class has no mana)."""
        if not uses_mana():
            return 0.0
        h = self.heal_pct(s)
        if h is None:
            return DRINK_BELOW
        return max(DRINK_MIN, min(DRINK_BELOW, DRINK_HEALS * h))

    def rest_heal_ok(self, s) -> bool:
        """A heal between fights (rest()): mana for REST_HEAL_K heals (REST_HEAL_MANA % without a bar)."""
        if not self.spell_cost(s, heal_name()):
            return False          # no heal on the bar: eat - "a heal does it" kept a healless class from food
        h = self.heal_pct(s)
        return s.mana_pct >= (REST_HEAL_K * h if h is not None else REST_HEAL_MANA)

    def bolt_floor(self, s, attackers=0, zero=True) -> float:
        """Mana % a bolt must leave in a fight: hurt or 2+ on us, a heal + BOLT_FLOOR_PAD (BOLT_FLOOR_HURT without a
        bar), else BOLT_FLOOR. zero (the melee branch): 0 when no heal is affordable anyway or the mob is nearly dead
        (inst3 17:46:23-26)."""
        bar = self.bar(s)
        hc = spell_cost(bar, heal_name())
        if zero and ((hc and s.mana < hc) or s.t_hp_pct <= 25):
            return 0.0
        if attackers >= 2 or s.hp_pct < 60:
            h = pct_of_pool(s, hc)
            return h + BOLT_FLOOR_PAD if h is not None else BOLT_FLOOR_HURT
        return BOLT_FLOOR

    def lines(self, s) -> Lines:
        lvl = s.level or 1
        boss = bool(self._boss(s))
        multi = bool(self._multi())
        pull_hp, src = REST_HP, "default"
        if lvl <= LOW_LEVEL:
            pull_mana, exit_mana, src = LOW_PULL_MANA, LOW_EXIT_MANA, "levels 1-5"
        else:
            k, n = self.kill_mana(lvl)
            if k is None:
                pull_mana, src = REST_MANA, f"default, {n} fights measured"
            else:
                h = self.heal_pct(s)
                heal = h if h is not None else HEAL_MARGIN
                pull_mana = max(PULL_LO, min(PULL_HI, K_KILL * k + heal))
                src = f"median {k:.0f} of {n} fights + heal {heal:.0f}"
            exit_mana = min(EXIT_HI, pull_mana + EXIT_MARGIN_MANA)
            if self._line_log != (lvl, round(pull_mana)):
                self._line_log = (lvl, round(pull_mana))
                self.log(f"rest line: level {lvl} mana {pull_mana:.0f}% ({src})")
        exit_hp = min(EXIT_HI, pull_hp + EXIT_MARGIN_HP)
        if multi:
            pull_mana, exit_mana = max(pull_mana, REST_MANA), max(exit_mana, REST_MANA + EXIT_MARGIN_MANA)
        if boss:
            pull_hp, exit_hp, pull_mana, exit_mana = BOSS_HP, BOSS_HP, BOSS_MANA, BOSS_MANA + EXIT_MARGIN_MANA
        if not uses_mana():
            pull_mana = exit_mana = 0.0          # rage / energy: the power bar is no resource to rest for
        return Lines(float(pull_hp), float(pull_mana), float(exit_hp), float(exit_mana), boss, src)

    def ready(self, s, lines: Lines | None = None) -> bool:
        """hp and mana at or above the pull line."""
        ln = lines or self.lines(s)
        return s.hp_pct >= ln.pull_hp and s.mana_pct >= ln.pull_mana

    def rest_target(self, s, lines: Lines | None = None) -> tuple:
        """(hp, mana) a standing rest goes on to."""
        ln = lines or self.lines(s)
        return ln.exit_hp, ln.exit_mana

    def rested(self, s, lines: Lines | None = None) -> bool:
        hp, mana = self.rest_target(s, lines)
        return s.hp_pct >= hp and s.mana_pct >= mana


class ResourcesAttr:
    """Bot attribute holding its Resources, made on first use (tests build Bot with __new__): the measurements and
    the bar are read from the bot each call (fight_costs, melee_costs, rot.bar, last_multi, boss_next)."""

    def __set_name__(self, owner, name):
        self.slot = "_res_" + name

    def __get__(self, obj, cls=None):
        if obj is None:
            return self
        r = obj.__dict__.get(self.slot)
        if r is None:
            def bar(s):
                rot = getattr(obj, "rot", None)
                f = getattr(rot, "bar", None)
                return f(s) if f else {}

            def boss(s):
                f = getattr(obj, "boss_next", None)
                return f(s) if f else False

            def log(m):
                import sys
                mod = sys.modules.get(type(obj).__module__)
                getattr(mod, "log", lambda _m: None)(m)

            r = obj.__dict__[self.slot] = Resources(
                costs=lambda: getattr(obj, "fight_costs", ()), bar=bar,
                multi=lambda: getattr(obj, "last_multi", False), boss=boss,
                melee=lambda: getattr(obj, "melee_costs", ()), log=log)
        return r

    def __set__(self, obj, value):
        obj.__dict__[self.slot] = value
