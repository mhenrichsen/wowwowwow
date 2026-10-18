"""Threat: how many mobs a pull / fight brings and whether our hp + mana beat them (review 2026-10-15, phase 2 step 2).

bot.py judged pull risk from ON-SCREEN plates only, in six places with six thresholds (pick_target's crowd_of /
crowd_n + PACK_PULL_N, _fight's crowd log, boss_ready's adds, rest()'s packed_here / dense, far_stronger + run()'s
danger loop, travel_abort), with a 450 px window that meant 0.5 rad in a 1080 px window and 0.82 rad in the 657 px
ones the bots run in. The deaths of 2026-10-15 are what that misses:

  inst3 10:26  pulled Tunnel Rat Kobold 1202 lvl 11 (us 12) with crowd=1 on screen; 4 on us 8 s later. Its spawn
               (-5296,-2717) is pooled with 1176 in a 4-spawn camp within 6 yd + (-5282,-2719): the assist radius.
  inst3 10:05 / 10:13 / 10:20  clean 1v1 vs lvl 12 Stonesplinters, then "loot: walked to corpse ... moved=19-22 yd"
               into the next camp member's aggro: att=1-2 at 66-70% hp / 41-43% mana, a third came, dead three
               times round (-5394..-5546, -2710..-2747) - camps of 6-7 spawns within 25 yd.
  inst3 10:05  the target fled at 14% hp into its friends: 5 attackers (TrinityCore: a fleeing creature runs to
               the nearest friend within CreatureFamilyFleeAssistanceRadius = 30 yd).
  inst3 10:40  1v1 lvl 12, an add at mob 37%, kill at 44% hp / 20% mana, then a FRESH lvl 11 already on us; 44 -> 13
               -> dead in 12 s, no flee (doomed() has no damage rate for a fresh mob) and no heal (mana < its cost).
               5 of inst3's last 6 deaths: a 2nd / 3rd fresh mob arriving at < 50% resources.
  inst4 10:06  pulled with crowd=1 at 100/100; the kill took 63% mana, the neighbour (an Elder Black Bear 13 yd
               from the target, its aggro covering our spot) came at once, dead with it at 59%.
  inst4 09:35  Rockjaw quarry: maxatt=5 with crowd=2.

So the estimate comes from the SERVER SPAWN TABLE (dangers._load(): positions, levels, faction, wander) plus the
plates, and is weighed against our resources:

  joiners   - ASSIST: live spawns of the target's own faction within ASSIST_R (worldserver.conf
              CreatureFamilyAssistanceRadius = 10) + their wander of the target's spawn come with it, and their own
              neighbours (each creature calls assistance once as it attacks: Unit::Attack) count at CHAIN_W.
            - AGGRO: live hostile spawns whose aggro radius (+ wander) covers where we will stand to pull.
            - PLATES: awake, not gray hostile plates beside the target on screen (same bearing within BESIDE_YAW
              rad, same depth within BESIDE_DSY x FOCAL px). est = target + max(spawn joiners, plate joiners).
  weight    - a mob of our level is 1.0, +LEVEL_W per level above (2.5 at most), gray GRAY_W, elite x ELITE_W.
  capacity  - CAP_FULL level-equal mobs at full hp, none at CAP_HP_FLOOR % (hp only: the same for every class).
              A pull is ok while weight <= PULL_MARGIN x capacity.
  in fight  - a FRESH mob (>= FRESH_HP) on us that our capacity cannot beat: flee (inst3 10:40, 10:13, 10:20,
              inst4 10:05:53). With hp + mana still there it is no verdict at all: rotation.doomed() decides.

Pool alternatives: dangers._load() lists both spawns of a pool ("VMaNGOS inline main / alternative", 5844 stacked
pairs on map 0) - only one is ever up, so spawns are merged by position here (bot.py's old packed() / spawn_zones
camp counts counted each neighbour twice).

Gray: one formula for the whole bot, the classic / TrinityCore Trinity::XP::GetGrayLevel:
    level <= 5: 0;  6-39: level - 5 - level // 10;  40-59: level - 1 - level // 5;  60+: level - 9
(level 12: mobs <= 6 are gray). The bot's own "not worth a stop" band (LOW_LEVELS = 3 below us: valid_plate,
travel_abort's outrun) is an xp choice, kept apart from gray on purpose: a level 9 mob still hits a level 12.

THREAT_LIVE: the new verdicts drive pick / pull, the loot walk, the fleeing-mob chase and the fresh-add flee. Either
way every place where the old rule and the new one disagree logs "threat: old=... new=... est=..." (bot.py), so the
shadow comparison runs live too. Set it False (or env NB_THREAT=0) to go back to the old rules with the logging.
"""

from __future__ import annotations

import math
import os
from collections import namedtuple

THREAT_LIVE = os.environ.get("NB_THREAT", "1") != "0"

try:
    from nb import route as _route
    FACTION = _route.FACTION   # the player's faction template (dwarf 3, human 1: nb/route.py)
except Exception:
    FACTION = 3
AGGRO_BASE, AGGRO_MIN, AGGRO_MAX = 20.0, 5.0, 45.0   # Creature::GetAggroRange: 20 + (mob - our level), 5..45 yd
ASSIST_R = 10.0          # CreatureFamilyAssistanceRadius
FLEE_ASSIST_R = 30.0     # CreatureFamilyFleeAssistanceRadius: where a fleeing mob finds its help
DB_JOINERS = False       # spawn-table assist / chain / aggro joiners (the user, 2026-10-17: the DB is not relied on)
CHAIN_W = 0.5            # weight of an assist-of-an-assist (it may come)
PLATE_RANGE = 45.0       # yd: plates show to ~41 yd; a target spawn is looked for this far + its wander
LOCATE_ARC = 0.6         # rad: a spawn this far off the plate's bearing is not the target
BESIDE_YAW = 0.30        # rad: plates beside the target (~10 yd at 30 yd)
BESIDE_DSY = 0.16        # x FOCAL px: same depth band (2 x bot.PICK_SY_BAND)
PULL_STAND = 28.0        # yd: where the pull is cast from (the bot sets its class's: nb/classes.py pull_stand)
CASTER = True            # the class's damage is a cast filler (Profile.nuke): low mana means only white hits are left
GRAY_W, LEVEL_W, ELITE_W = 0.25, 0.25, 2.5
LEVEL_W_DOWN = 0.12       # per level BELOW us: a level-2-lower mob still hits ~3/4 as hard (bot 2 11:58: a pair of
#                           level 11s at level 13 weighed 1.00 in all, cost 95% mana + 32% hp)
PACK_W = 0.5              # a pull of n mobs costs x (1 + PACK_W (n-1)): the later ones hit while the first dies
LOW_LEVELS = 3           # the bot's "below us" band: not worth a stop (valid_plate), outrun on a service walk
STRONGER_LEVELS = 2      # far_stronger / the run() danger mark: elite, or this many levels up
#                          (12:43 level 1-5: ~65% a WHOLE fight of 2; inst3 10:23-10:25 level 12: 55-65%; inst4 10:05 63%)
LONE_RESTED = 80.0       # % hp and mana: a lone target with nothing near it may use the whole capacity
PULL_MARGIN = 0.75       # a pull's weight may use this share of the capacity (a death costs minutes, + res sickness)
FRESH_HP = 85.0          # % mob hp: an attacker this fresh is a whole fight still to pay for
FIGHT_MARGIN = 1.0       # in a fight: flee when what is on us needs more than this x the capacity left
FRESH_FLEE_MIN_HP = 15.0 # % hp: below this a run is no better than the fight
# moved from rotation.py (doomed()'s static rules; travel_abort uses the same ones)
FLEE_PACK = 3            # this many (not gray) on us: break off at once, while there is hp to run on (inst3 17:42:51)
FLEE_PACK_HP = 85.0      # % hp: the pack break-off only from here up (all 3 such flees got away, 2026-10-14 logs)
ELITE_LEVELS = 3         # an elite fights like a mob this many levels higher
FLEE_LEVEL_GAP = 3       # a mob this many levels over us at 50%+ hp: flee (2 fled a level 12 Rockjaw, inst4 05:53: died)
TRAVEL_FIGHT_HP = 55.0   # % hp: on a service walk, below this anything on us is fought, not outrun
# walked rest (rest()): stand instead of walking on beside a pack or in mob country
REST_PLATES_N = 2        # awake hostile plates in view
REST_DENSE_R, REST_DENSE_N = 40.0, 3   # this many hostile (not low) spawns this close
# spawn_zones camps (bot.py): a same-level spawn with PACK_N+ hostile neighbours within PACK_R yd
PACK_R, PACK_N = 15.0, 2
LOOT_AGGRO_PAD = 3.0     # yd added to an aggro radius on the loot walk (we stop 0-5 yd short of / past the corpse)
ROUTE_PAD = 3.0          # yd added to an aggro radius along a flee route / the walk to a rest spot
FLEE_PAD = 8.0           # yd added along a FLEE route: the run wobbles, chasers drag us, and a near-miss costs a death
ELITE_PAD = 10.0         # yd more round an elite's spawn / patrol point (the patrol walks BETWEEN the points; bot 2
#                          12:41:38 wedged 31 yd from Ol' Sooty's nearest point, 1 yd inside 27 + 5, dead at 12:42:11)
REST_PAD = 5.0           # yd added round a rest spot (we sit for up to 45 s; a wanderer's path is not a circle)


def risk_weight(pl: int, pts) -> float:
    """The strength of spawn points (path_risk / spot_risk) in 'one mob of our level' units."""
    return sum(weight(pl, p.hi, p.rank > 0) for p in pts)
LOOT_EST_D = 20.0        # yd: a corpse that needs a walk lies this far along our facing (10:12-10:20: moved 19-22 yd)


# ------------------------------------------------------------ levels
def gray_level(pl: int) -> int:
    """Highest mob level that is gray to a level `pl` character (Trinity::XP::GetGrayLevel, classic table)."""
    if pl <= 5:
        return 0
    if pl <= 39:
        return pl - 5 - pl // 10
    if pl <= 59:
        return pl - 1 - pl // 5
    return pl - 9


def is_gray(pl: int, ml: int) -> bool:
    return ml > 0 and ml <= gray_level(pl)


def low(pl: int, ml: int) -> bool:
    """LOW_LEVELS or more below us: not worth a stop (an xp choice, not a threat one)."""
    return ml > 0 and ml <= pl - LOW_LEVELS


def aggro_radius(pl: int, ml: int) -> float:
    return max(AGGRO_MIN, min(AGGRO_MAX, AGGRO_BASE + (ml - pl)))


def weight(pl: int, ml: int, elite: bool = False, trivial: bool = False, down: float = LEVEL_W) -> float:
    """A mob's strength in 'one mob of our level' units (down: the slope per level below us; pull() uses LEVEL_W_DOWN)."""
    if trivial or is_gray(pl, ml):
        w = GRAY_W
    else:
        dl = (ml or pl) - pl
        w = max(0.4, min(2.5, 1.0 + (LEVEL_W if dl >= 0 else down) * dl))
    return w * (ELITE_W if elite else 1.0)


CAP_FULL, CAP_HP_FLOOR = 4.0, 20.0   # level-equal mobs a full-hp character beats; none at CAP_HP_FLOOR % hp


def capacity(hp: float, mana: float = 0.0, m1: float = 0.0) -> float:
    """How many level-equal mobs we still beat: from hp alone, the same for every class (the old mana / 'mana per
    mob' term was a shaman fit; the user, 2026-10-17: the bot is general). Mana is the rest / drink lines' job: a
    pull starts above them. mana and m1 are ignored (kept for the callers' signatures)."""
    return CAP_FULL * max(0.0, hp - CAP_HP_FLOOR) / (100.0 - CAP_HP_FLOOR)


# ------------------------------------------------------------ plates
def _g(o, k, d=False):
    return getattr(o, k, d)


_npc_fac: dict | None = None


def neutral_npc(npc) -> bool:
    """A creature that is attackable but not hostile to us (Young Wolf, Timber Wolf: faction 32): it never aggroes
    and - the user's call from Northshire, 2026-10-17 - a pull of one brings no others. False when the DB cannot say."""
    global _npc_fac
    if not npc:
        return False
    if _npc_fac is None:
        try:
            import worlddb
            _npc_fac = {int(e): int(f) for e, f in worlddb._query_db("tcf_world",
                                                                     "SELECT entry, faction FROM creature_template")}
        except Exception:
            _npc_fac = {}
    fac = _npc_fac.get(int(npc))
    if fac is None:
        return False
    import factions
    return not factions.is_hostile(FACTION, fac) and not factions.is_friendly(FACTION, fac)


def awake(q) -> bool:
    """A hostile plate that would fight: alive, not a player, not gray (UnitIsTrivial). ('hostile' is the strip's
    UnitCanAttack bit: a neutral wolf has it too - awake_hostile leaves those out.)"""
    return bool(_g(q, "hostile") and not _g(q, "dead") and not _g(q, "player") and not _g(q, "trivial"))


def awake_hostile(q) -> bool:
    """awake() and not neutral (neutral_npc): what a pull can bring along (a fleeing mob still fetches neutrals)."""
    return awake(q) and not neutral_npc(_g(q, "npc", 0))


def plate_yaw(p, W, focal) -> float:
    return -math.atan2(_g(p, "sx", 0) - W / 2, focal)


def beside(p, q, W, focal) -> bool:
    """q stands beside p on screen: same bearing (BESIDE_YAW) and depth band (BESIDE_DSY x FOCAL px; the camera looks
    down on the ground, a lower plate stands closer)."""
    if _g(p, "sx", -1) < 0 or _g(q, "sx", -1) < 0:
        return False
    if abs(plate_yaw(p, W, focal) - plate_yaw(q, W, focal)) > BESIDE_YAW:
        return False
    psy, qsy = _g(p, "sy", -1), _g(q, "sy", -1)
    return psy < 0 or qsy < 0 or abs(psy - qsy) <= BESIDE_DSY * focal


def plates_beside(plates, p, W, focal, pred=awake) -> list:
    """Plates (pred: awake by default) beside p, p itself (or its guid) left out."""
    gp = _g(p, "ghash", None)
    return [q for q in plates if q is not p and (gp is None or _g(q, "ghash", None) != gp) and pred(q)
            and beside(p, q, W, focal)]


def awake_plates(plates, on_screen=False) -> list:
    return [q for q in plates if awake(q) and (not on_screen or _g(q, "sx", -1) >= 0)]


def stronger(plates, pl: int, on_screen=True) -> list:
    """Hostile plates far stronger than us (elite, or STRONGER_LEVELS+ up): far_stronger / run()'s danger mark."""
    return [q for q in plates if _g(q, "hostile") and not _g(q, "dead") and not _g(q, "player")
            and (_g(q, "elite") or _g(q, "level", 0) >= pl + STRONGER_LEVELS) and (not on_screen or _g(q, "sx", -1) >= 0)]


# ------------------------------------------------------------ fight-time rules
def pack_flee(pack: int, hp: float, t_hp: float) -> bool:
    """rotation.doomed()'s pack break-off: FLEE_PACK+ not gray on us, before anything is invested."""
    return pack >= FLEE_PACK and hp >= FLEE_PACK_HP and t_hp >= 60


def level_flee(pl: int, t_level: int, t_hp: float, elite=False, trivial=False) -> bool:
    """rotation.doomed()'s level gap: a mob FLEE_LEVEL_GAP+ over us (an elite ELITE_LEVELS more) with 50%+ hp."""
    el = (t_level or 0) + (ELITE_LEVELS if elite else 0)
    return el >= (pl or 1) + FLEE_LEVEL_GAP and t_hp >= 50 and not trivial


FLEE_MULTI_HP = 70.0     # rotation.doomed()'s 'fight it out' line: 2+ on us below it, with a heal's mana (1 of 7 such
#                          flees got away, 2026-10-14)


MELEE_HP_FLOOR = 30.0    # % hp: a melee-only fight is not planned below this (phase 3: melee_capacity)


def melee_capacity(hp: float, mana: float, m1: float, hp_kill: float) -> float:
    """Capacity against ONE mob when the weapon can carry the fight: mana's share as capacity() plus the level-equal
    mobs our hp over MELEE_HP_FLOOR kills in melee at hp_kill % hp each (nb/resources.py: measured, else 60). Bot 2
    11:16:07 fled a lone level 10 at 88% hp / 7% mana, capacity 0.48 by the hp-over-50 rule; this one says 1.09."""
    melee = max(0.0, hp - MELEE_HP_FLOOR) / max(1.0, hp_kill)
    return max(capacity(hp), melee)


PACK_W = 0.5             # per extra attacker: need x (1 + PACK_W x (n - 1))


def pack_need(pl: int, att) -> float:
    """What the mobs on us need of our capacity. 2+ on us: lower levels count with pull()'s slope (two level 11s on a
    level 14 weighed 0.4 each and "won"; bot 4 18:18:58 died to them), and the sum is scaled for the pack: they hit
    together while we kill them one by one, so the first one's fight is paid at n times the rate - n equal mobs cost
    ~ n (n + 1) / 2 lone fights of hp (bot 4 18:42:47 "need 1.40 vs capacity 2.14", 2 level 12 troggs at 79% hp,
    dead in 18 s; bot 2 18:38:41 1.68 vs 1.81, 3 level 10 vultures, dead). A lone one keeps LEVEL_W (the lone
    near-miss rules were measured on it)."""
    att = list(att)
    down = LEVEL_W_DOWN if len(att) >= 2 else LEVEL_W
    need = sum(weight(pl, a[0], a[2], a[3], down=down) * max(0.1, min(100.0, a[1] or 100.0)) / 100.0 for a in att)
    live = sum(1 for a in att if not a[3] and (a[1] or 100.0) > 0)
    return need * (1.0 + PACK_W * max(0, live - 1))


def need_cap(pl: int, hp: float, mana: float, attackers, m1: float, hp_kill: float | None = None):
    """(need, capacity) as fresh_add weighs them (for the logs of a fight-it-out)."""
    att = list(attackers)
    need = pack_need(pl, att)
    cap = (melee_capacity(hp, mana, m1, hp_kill) if hp_kill and len(att) == 1 else capacity(hp, mana, m1))
    return need, cap


def fresh_add(pl: int, hp: float, mana: float, attackers, m1: float, heal_ok: bool = False,
              hp_kill: float | None = None, inc: float = 0.0, out: float = 0.0) -> str:
    """A fresh mob on us that what we have left cannot beat -> why (flee), else "". attackers: [(level, hp %, elite,
    trivial)] of what is on us. The need is each one's weight x the share of its hp still up; one of them must be
    fresh (>= FRESH_HP), else the fight is invested and rotation.doomed() judges it from the measured rates.
    doomed()'s 'fight it out' zone is kept: 2+ on us under FLEE_MULTI_HP with a heal affordable (heal_ok).
    hp_kill (% hp a melee-only kill costs): a LONE attacker is weighed against melee_capacity (2+ keep capacity()),
    and fled only when clearly outmatched (need over (1 + NEAR_MISS) x capacity) with a survivable run (flee_loss of
    the measured incoming rate `inc`, %/s, leaves FLEE_SURVIVE_HP). 2+ on us: the old rule."""
    att = list(attackers)
    if not att or hp < FRESH_FLEE_MIN_HP:
        return ""
    need = pack_need(pl, att)
    cap = (melee_capacity(hp, mana, m1, hp_kill) if hp_kill and len(att) == 1 else capacity(hp, mana, m1))
    # a hopeless fight is run from whatever the run costs: staying is sure death, a run has variance (leashes, a
    # pessimistic cost: ~32% costed, 10% paid at 17:26). Bot 2 19:08-19:10 stayed in at need/cap 5.4, 7.6, 21 ("3 on
    # us need 3.82 vs capacity 0.18") and died twice in 2 min
    # (2+ only: a lone chaser runs at our speed and takes its hits the whole way - bot 4 13:29:41 fled one at 56%, dead)
    # no heal affordable and little mana: capacity is white hits only, the 3x margin is for a bot that still casts
    # (bot 2 19:39:50 "2 on us need 1.46 vs capacity 0.50 (71% hp 13% mana, heal not affordable)" stayed at 2.9x,
    # ran at 28% hp with 4 on it, dead)
    gate = HOPELESS_DRY_RATIO if (CASTER and not heal_ok and mana < HOPELESS_DRY_MANA) else HOPELESS_RATIO
    # dying fast: the measured loss rate leaves under TTD_FLEE_S s and the fight is not near even - a heal (~30%) buys
    # 2-3 s (bot 3 21:03:31 "3 on us need 3.65 vs capacity 1.46 (40% hp 84% mana, heal affordable)", 12%/s, dead 4 s
    # later: the run at 40% / 84% mana was its best chance)
    # losing the race: at the measured rates we die in under RACE_SHARE of the time killing all of them takes
    # (bot 4 21:30:52, 44% hp at ~6%/s = 7 s left, a 37% and a full mob at ~2.5%/s = ~50 s; the 6 s rule ran at 17%)
    if len(att) >= 2 and inc and inc > 0 and out and out > 0 and hp >= RACE_MIN_HP             and need > MULTI_STAY_RATIO * max(cap, 0.05):
        ttd, ttk = hp / inc, sum(max(1.0, min(100.0, a[1] or 100.0)) for a in att if not a[3]) / out
        if ttd < RACE_SHARE * ttk:
            return (f"losing the race: {len(att)} on us need {need:.2f} vs capacity {cap:.2f} ({hp:.0f}% hp "
                    f"{mana:.0f}% mana): ~{ttd:.0f}s to live at {inc:.1f}%/s, ~{ttk:.0f}s to kill them at {out:.1f}%/s")
    if len(att) >= 2 and inc and inc > 0 and hp / inc < TTD_FLEE_S and need > MULTI_STAY_RATIO * max(cap, 0.05):
        return (f"dying fast: {len(att)} on us need {need:.2f} vs capacity {cap:.2f} ({hp:.0f}% hp {mana:.0f}% mana), "
                f"losing {inc:.1f}%/s: ~{hp / inc:.0f}s left")
    if len(att) >= 2 and need > gate * max(cap, 0.05):
        return (f"hopeless: {len(att)} on us need {need:.2f} vs capacity {cap:.2f} ({hp:.0f}% hp {mana:.0f}% mana): "
                f"running whatever it costs")
    if not any(a[1] >= FRESH_HP and not a[3] for a in att):
        return ""
    # 2+ on us under FLEE_MULTI_HP with a heal: fight it out - when the fight is near even. Two fresh level 11s on
    # a bot at 47% hp (bot 4 18:18:58, two heals, dead 28 s later) are not: then the flee rules below decide
    if len(att) >= 2 and hp < FLEE_MULTI_HP and heal_ok and need <= MULTI_STAY_RATIO * cap:
        return ""
    if need <= FIGHT_MARGIN * cap:
        return ""
    if len(att) == 1:
        # a lone melee chaser runs at our speed: a flee takes its hits until it gives up. Flee only when the fight is
        # CLEARLY lost (not a near-miss) and the run itself is survivable (bot 3 11:30:24: "need 0.75 vs capacity
        # 0.73 (58% hp 38% mana)", fled one level 12, 33 yd in 19 s, dead; fighting with a heal would have won)
        if need <= (1.0 + NEAR_MISS) * FIGHT_MARGIN * cap:
            return ""
        loss = flee_loss(need, inc)
        if hp - loss < FLEE_SURVIVE_HP:
            return ""
        return (f"outmatched: 1 on us need {need:.2f} vs capacity {cap:.2f} ({hp:.0f}% hp {mana:.0f}% mana), "
                f"the run costs ~{loss:.0f}% hp")
    # 2+ on us: the run must be survivable too (bot 4 14:14:13 fled two level 14s at 93% hp - a 15 s run costed at
    # ~131% hp - and died after 79 yd; a fight with heals was the better bet). A pack's chase is costed shorter:
    # clean runs from packs paid ~0.6 of the 15 s estimate (2026-10-15: 64->19, 32->10, 30->35, 42->21, 26->21)
    loss = flee_loss(need, inc, FLEE_LEASH_PACK_S)
    if hp - loss < FLEE_SURVIVE_HP:
        return ""
    return (f"outmatched: {len(att)} on us need {need:.2f} vs capacity {cap:.2f} ({hp:.0f}% hp {mana:.0f}% mana), "
            f"the run costs ~{loss:.0f}% hp")


RACE_SHARE, RACE_MIN_HP = 0.5, 25.0   # fresh_add: time to die under this share of the time to kill all -> run
TTD_FLEE_S = 6.0         # s left at the measured loss rate under which 2+ on us is run from (fresh_add)
HOPELESS_DRY_RATIO, HOPELESS_DRY_MANA = 1.5, 20.0   # ... with no heal affordable under this mana
HOPELESS_RATIO = 3.0     # fresh_add: need over this x capacity is fled with no run-cost veto
MULTI_STAY_RATIO = 1.2   # fresh_add: 2+ on us with a heal stay in only up to this need / capacity (Bot.FLEE_STAY_RATIO)
NEAR_MISS = 0.15         # a lone mob: need within 15% over the capacity is a fight (heal first), not a flee
FLEE_LEASH_S = 15.0      # s a melee chaser at our speed keeps hitting a runner (no leash measurement yet: bot 3 11:30
#                          ran 19 s / 33 yd dazed and died; bot 2 11:16 got away after 127 yd)
FLEE_LEASH_PACK_S = 10.0 # s of chase costed for a run from 2+ (the measured runs above)
FLEE_DPS_W = 3.5         # % of our hp per s per unit of weight, when the fight has no measured damage rate yet
FLEE_SURVIVE_HP = 15.0   # % hp a flee must leave us


def flee_loss(need: float, inc: float = 0.0, leash_s: float = FLEE_LEASH_S) -> float:
    """% hp a flee on foot costs: the measured incoming rate (rotation inc, %/s) or FLEE_DPS_W per unit of weight
    still on us, for FLEE_LEASH_S s of chase."""
    # at least the static rate: a fight's first seconds measure too little (bot 4 13:29:41 'losing 1.0%/s', costed
    # the run ~15%, lost 56% hp in 16 s / 16 yd, dazed; bot 3 11:30 58% in 19 s: ~3-3.5% per s per mob)
    rate = max(inc or 0.0, FLEE_DPS_W * need)
    return rate * leash_s


def travel_fight(pl: int, hp: float, attackers) -> bool:
    """A service walk breaks off (the main loop then fights, or fight() flees by the rules above) for a real threat;
    low / gray mobs at our heels are outrun. attackers: [(level, trivial)]. One rule set with doomed(): a pack is
    fought or fled by fight(), never walked on with (the old travel_abort aborted at 3 of ANY level; 3 gray ones at
    55%+ hp are now outrun like one)."""
    if not attackers:
        return False
    if hp < TRAVEL_FIGHT_HP:
        return True
    # 2+ not gray on us: fought now, at full hp. Low ones still hit: bot 2 18:37-18:38 "outran" three level 10
    # Fleshrippers (level 14) for 70 s on a vendor walk, broke off at 53% hp with no run left, and died
    if sum(1 for _, tv in attackers if not tv) >= 2:
        return True
    return any(not tv and (lv <= 0 or not low(pl, lv)) for lv, tv in attackers)


# ------------------------------------------------------------ spawn table
Point = namedtuple("Point", "x y z wander entries lo hi rank fac")


class Spawns:
    """The hostile (to FACTION) spawns of one map from dangers._load() rows, pool alternatives merged, on a grid.
    alive(point) -> bool: is it up now (default: every point; the bot passes economy's respawn table)."""
    CELL = 20.0

    def __init__(self, rows=None, map_id=0, faction=FACTION, alive=None, hostile=None):
        if rows is None:
            import dangers
            rows = dangers._load()
        if hostile is None:
            import factions
            hostile = lambda fac: factions.is_hostile(faction, fac)
        by: dict = {}
        hcache: dict = {}
        for r in rows:
            m, x, y, z, w, e, lo, hi, rank, fac = r[:10]
            if m != map_id:
                continue
            if fac not in hcache:
                hcache[fac] = bool(hostile(fac))
            if not hcache[fac]:
                continue
            k = (round(x * 2), round(y * 2))
            o = by.get(k)
            if o is None:
                by[k] = [x, y, z, w, {e}, lo, hi, rank, fac]
            else:                                    # a pool alternative: one spawn, the worse case of the two
                o[3], o[4] = max(o[3], w), o[4] | {e}
                o[5], o[6], o[7] = min(o[5], lo), max(o[6], hi), max(o[7], rank)
        self.points = [Point(a[0], a[1], a[2], a[3], frozenset(a[4]), a[5], a[6], a[7], a[8]) for a in by.values()]
        self.grid: dict = {}
        for p in self.points:
            self.grid.setdefault((int(p.x // self.CELL), int(p.y // self.CELL)), []).append(p)
        self.alive = alive or (lambda p: True)

    def near(self, x, y, r, live=True) -> list:
        """(distance, point) within r yd of (x, y), nearest first."""
        c, n = self.CELL, int(r // self.CELL) + 1
        gx, gy = int(x // c), int(y // c)
        out = []
        for dx in range(-n, n + 1):
            for dy in range(-n, n + 1):
                for p in self.grid.get((gx + dx, gy + dy), ()):
                    d = math.hypot(p.x - x, p.y - y)
                    if d <= r and (not live or self.alive(p)):
                        out.append((d, p))
        out.sort(key=lambda t: t[0])
        return out

    def packed(self, x, y, pl, r=12.0) -> int:
        """Bot.packed: other hostile spawns, not LOW_LEVELS+ below us (hi > pl - 3), within r yd of (x, y). Static
        (dead ones count: a camp is a camp), as the old one; pool alternatives once."""
        return sum(1 for d, p in self.near(x, y, r, live=False) if d > 0.5 and p.hi > pl - LOW_LEVELS)


_spawns = None
_spawns_fail_t = -1e18


def spawns() -> Spawns:
    """The shared Spawns of map 0 (dangers._load(), loaded on first use; alive from economy's respawn table). A failed
    load raises at once for a minute after (no DB query per call)."""
    global _spawns, _spawns_fail_t
    if _spawns is None:
        import time
        if time.time() - _spawns_fail_t < 60:
            raise RuntimeError("spawn table load failed <60 s ago")
        try:
            _spawns = Spawns(alive=_economy_alive)
        except Exception:
            _spawns_fail_t = time.time()
            raise
    return _spawns


def _economy_alive(p) -> bool:
    try:
        from nb import economy
        if not economy.dead_known():
            return True
        sp = economy.at(p.x, p.y)
        return not sp or economy.respawn_left(sp.guid) <= 0
    except Exception:
        return True


# ------------------------------------------------------------ estimates
Estimate = namedtuple("Estimate", "est weight cap verdict why target joiners plate_n src")


def _ahead(x, y, ang, d):
    return x + d * math.cos(ang), y + d * math.sin(ang)


def _seg_d(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    n2 = dx * dx + dy * dy
    t = 0.0 if n2 <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / n2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class Model:
    def __init__(self, sp: Spawns | None = None):
        self._sp = sp

    @property
    def sp(self) -> Spawns:
        if self._sp is None:
            self._sp = spawns()
        return self._sp

    def locate(self, npc, x, y, ang, dist=None):
        """The target's spawn point: a live spawn of its npc id within PLATE_RANGE (+ wander) nearest the plate's
        bearing `ang` (world angle, as move.bearing); any hostile spawn when no spawn of that id is there (patrols,
        an id the table lists under its pool partner). dist: a known distance (ranged range: <= 30). -> point or None."""
        best, bs = None, 1e9
        for d, p in self.sp.near(x, y, PLATE_RANGE + 10.0):
            if d < 1.0:
                continue
            off = abs((math.atan2(p.y - y, p.x - x) - ang + math.pi) % (2 * math.pi) - math.pi)
            if off > LOCATE_ARC + math.atan2(p.wander + 3.0, max(d, 1.0)):
                continue
            sc = off * 30.0 + (0.0 if npc in p.entries else 25.0) + (abs(d - dist) * 0.5 if dist else d * 0.05)
            if sc < bs:
                best, bs = p, sc
        return best

    def joiners(self, pl, tx, ty, tfac, sx, sy, me=None, exclude=()):
        """Spawn points that come with a target at (tx, ty) of faction tfac pulled from stand point (sx, sy), walked
        to from `me`: [(point, weight factor, why)]."""
        out, seen = [], set(exclude)
        first = []
        if not DB_JOINERS:
            return out           # the plates beside the target are the estimate (pull(): est = target + plates)
        for d, p in self.sp.near(tx, ty, ASSIST_R + 10.0):
            if p in seen or (tfac is not None and p.fac != tfac):
                continue
            if d <= ASSIST_R + p.wander:
                seen.add(p)
                first.append(p)
                out.append((p, 1.0, "assist"))
        for f in first:                                  # its own call for help (each attacker calls once)
            for d, p in self.sp.near(f.x, f.y, ASSIST_R + 10.0):
                if p not in seen and p.fac == f.fac and d <= ASSIST_R + p.wander:
                    seen.add(p)
                    out.append((p, CHAIN_W, "chain"))
        ax, ay = me if me else (sx, sy)
        for d, p in self.sp.near(sx, sy, AGGRO_MAX + 10.0 + math.hypot(sx - ax, sy - ay)):
            if p in seen:
                continue
            if p.hi <= pl and not p.rank:
                # a spawn table aggro guess for a mob at or below our level: not counted (Northshire 2026-10-17:
                # kobold pulls estimated 2-9 from 'aggro' spawns, maxatt 1 every time; the user: "we cannot rely on the
                # database"). Higher-level and elite ones still count (the Elder Black Bear, inst4 10:06)
                continue
            reach = aggro_radius(pl, p.hi) + p.wander
            if _seg_d(p.x, p.y, ax, ay, sx, sy) <= reach:
                seen.add(p)
                out.append((p, 1.0, "aggro"))
        return out

    def pull(self, pl, x, y, ang, npc, t_level, hp, mana, m1, elite=False, trivial=False, dist=None, plates=()):
        """Estimate of a pull: the target (npc, t_level, on bearing ang from us at (x, y)) and `plates`, the awake
        plates beside it [(level, elite, trivial)]. ang None (no bearing: camera off the heading, plate off screen):
        the plates alone. -> Estimate (verdict "pull" / "skip")."""
        tp = self.locate(npc, x, y, ang, dist) if ang is not None else None
        if ang is None:
            tx = ty = tfac = None
            src = "plates"
        elif tp is not None:
            tx, ty, tfac, src = tp.x, tp.y, tp.fac, "spawn"
            td = math.hypot(tx - x, ty - y)
        else:
            td = dist or 25.0
            (tx, ty), tfac, src = _ahead(x, y, ang, td), None, "guess"
        if ang is None:
            js = []
        else:
            if td > PULL_STAND + 2:
                sx, sy = _ahead(tx, ty, math.atan2(y - ty, x - tx), PULL_STAND)
            else:
                sx, sy = x, y
            js = self.joiners(pl, tx, ty, tfac, sx, sy, me=(x, y), exclude=(tp,) if tp else ())
            if tfac is None:                             # no spawn found: only what is certainly in assist range
                js = [j for j in js if j[2] != "chain"]
            if neutral_npc(npc):                         # a neutral target calls no one: only aggro on the way in
                js = [j for j in js if j[2] == "aggro"]
        dn = LEVEL_W_DOWN
        w0 = weight(pl, t_level, elite, trivial, down=dn)
        ws = sum(weight(pl, p.hi, p.rank > 0, down=dn) * f for p, f, _ in js)
        wp = sum(weight(pl, lv, el, tv, down=dn) for lv, el, tv in plates)
        n_s = sum(1 for _p, f, _w in js if f >= 1.0)
        est = 1 + max(n_s, len(plates))
        # the pack factor counts real joiners only: a gray one beside the target adds its weight, not a second fight
        n_eff = 1.0 + max(sum(f for p, f, _w in js if not is_gray(pl, p.hi)),
                          float(sum(1 for lv, _el, tv in plates if not (tv or is_gray(pl, lv)))))
        wt = (w0 + max(ws, wp)) * (1.0 + PACK_W * (n_eff - 1.0))
        cap = capacity(hp, mana, m1)
        # the margin is for adds we did not see coming; a lone target with nothing near it only has to fit (bot 1 run 8
        # 12:2x-12:42: level 2, fights measured at ~92% mana -> capacity 1.59 at full; every lone level 3 (1.25) was
        # "skip" against 0.75 x 1.59, 79 skips, level 2 while the others were 3)
        margin = 1.0 if (not js and not plates and hp >= LONE_RESTED and (mana >= LONE_RESTED or not CASTER))             else PULL_MARGIN
        ok = wt <= margin * cap
        kinds = {}
        for _p, _f, why in js:
            kinds[why] = kinds.get(why, 0) + 1
        why = (f"{'+'.join(f'{n} {k}' for k, n in sorted(kinds.items())) or 'alone'}"
               f"{f', {len(plates)} beside on screen' if plates else ''}; weight {wt:.2f} vs {margin} x "
               f"capacity {cap:.2f}")
        return Estimate(est, round(wt, 2), round(cap, 2), "pull" if ok else "skip", why, tp, js, len(plates), src)

    def flee_help(self, tp, exclude=()) -> int:
        """Live spawns of the target's faction a fleeing target can run to (FLEE_ASSIST_R of its spawn)."""
        if tp is None:
            return 0
        ex = set(exclude) | {tp}
        return sum(1 for d, p in self.sp.near(tp.x, tp.y, FLEE_ASSIST_R) if p not in ex and p.fac == tp.fac)

    def path_risk(self, pl, pts, pad=ROUTE_PAD) -> list:
        """Live hostile spawns whose aggro radius (+ wander + pad) reaches the walk along pts [(x, y, ...)] - not the
        ones that already reach where it starts (pts[0]: they are in it, or aware, whichever way we go). The flee
        route and the walk to a rest spot are scored with it (bot 2 11:37:42: fled 118 yd from a 1162 camp into a
        level 12 1186's aggro at (-5478,-2837), dead)."""
        if len(pts) < 2:
            return []
        x0, y0 = pts[0][0], pts[0][1]
        seen, out = set(), []
        for a, b in zip(pts, pts[1:]):
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            half = math.hypot(b[0] - a[0], b[1] - a[1]) / 2
            for d, p in self.sp.near(mx, my, half + AGGRO_MAX + 10.0 + pad + ELITE_PAD):
                if p in seen:
                    continue
                reach = aggro_radius(pl, p.hi) + p.wander + (ELITE_PAD if p.rank > 0 else 0.0)
                if math.hypot(p.x - x0, p.y - y0) <= reach:   # (the pad only widens the way, not the start)
                    continue
                if _seg_d(p.x, p.y, a[0], a[1], b[0], b[1]) <= reach + pad:
                    seen.add(p)
                    out.append(p)
        return out

    def spot_risk(self, pl, x, y, pad=REST_PAD) -> list:
        """Live hostile spawns whose aggro radius (+ wander + pad) covers (x, y): a mob that comes while we sit
        (bot 3 11:38:20 rested at 15% mana 6 yd from a kobold spawn, aggroed 3 s later; bot 4 11:10:10 12 yd from a
        level 12 1186 with a 10 yd wander)."""
        return [p for d, p in self.sp.near(x, y, AGGRO_MAX + 10.0 + pad + ELITE_PAD)
                if d <= aggro_radius(pl, p.hi) + p.wander + pad + (ELITE_PAD if p.rank > 0 else 0.0)]

    def density(self, pl, x, y, r=REST_DENSE_R) -> int:
        """Live hostile spawns, not LOW_LEVELS+ below us, within r yd."""
        return sum(1 for d, p in self.sp.near(x, y, r) if p.hi > pl - LOW_LEVELS)

    def loot_risk(self, pl, x, y, cx, cy, npc=None) -> list:
        """Live spawns whose aggro (+ wander + LOOT_AGGRO_PAD) reaches the walk from (x, y) to the corpse at (cx, cy)
        (the bot puts it LOOT_EST_D yd along the way it faced at the kill). The corpse's own spawn (npc's, nearest
        the corpse) does not count - it is the one we killed - nor does a spawn that reaches us where we stand."""
        near = self.sp.near(cx, cy, AGGRO_MAX + 15.0)
        own = next((p for dd, p in near if npc in p.entries and dd < 20.0), None) if npc else None
        out = []
        for dd, p in near:
            if p is own:
                continue
            reach = aggro_radius(pl, p.hi) + p.wander + LOOT_AGGRO_PAD
            # what does not reach us where we stand now is what the walk adds
            if _seg_d(p.x, p.y, x, y, cx, cy) <= reach and math.hypot(p.x - x, p.y - y) > reach - LOOT_AGGRO_PAD:
                out.append(p)
        return out
