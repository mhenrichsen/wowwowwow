"""Offline check of nb/rotation.py with fake States (no game, no keys): python -m nb.test_rotation"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb.eye import State                       # noqa: E402
from nb.shaman_rotation import Rotation, melee_finish   # noqa: E402

# bar by level: what the trainer visits (2/4/6/8/10) leave on slots 1..10
BAR = {
    1: [403, 331],
    2: [403, 331, 8017],
    4: [403, 331, 8017, 8042],
    6: [403, 331, 8017, 8042],               # Healing Wave 2 replaces rank 1 in place
    8: [403, 331, 8017, 8042, 324, 5730],
    10: [403, 331, 8017, 8042, 324, 5730, 8050],
}
RANKED = {6: {331: 332}, 8: {403: 529, 331: 332, 8042: 8044, 8017: 8018}, 10: {403: 529, 331: 332, 8042: 8044, 8017: 8018}}


def bar(level):
    key = max(k for k in BAR if k <= level)
    ids = list(BAR[key])
    for lv, m in RANKED.items():
        if level >= lv:
            ids = [m.get(i, i) for i in ids]
    return ids + [0] * (12 - len(ids))


def mk(level=1, hp=100, mana=100, t_hp=100, t_on_me=False, t_casting=False, within10=False, auras=0,
       combat=False, casting=False, has_target=True, ready=True, rng=True, manamax=None):
    manamax = manamax or 40 + 14 * level
    actions = bar(level)
    sr = 0
    for slot, sid in enumerate(actions, 1):
        if sid:
            sr |= (int(ready) | (int(rng) << 1)) << (2 * (slot - 1))
    flags = (1 if combat else 0) | (8 if casting else 0) | (512 if has_target else 0)
    tf = 2 | (16 if t_on_me else 0) | (128 if within10 else 0) | ((1 << 16) if t_casting else 0)
    return State(t=0, frame=0, x=0, y=0, z=0, facing=0, hp=hp, hpmax=100, mana=round(manamax * mana / 100),
                 manamax=manamax, level=level, inst=0, xp=0, xpmax=1, flags=flags, speed=0, casting=0,
                 t_hp=t_hp, t_hpmax=100, t_level=level, t_class=0, t_reaction=0, t_npc=1, t_flags=tf, t_hash=1,
                 t_x=None, t_y=None, bags_free=5, nquests=0, money=0, dur=100, slots_ready=sr, auras=auras,
                 err=0, errn=0, ui=0, gtime=0, actions=actions)


fails = []


def check(label, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{label}: {got!r}" + ("" if ok else f" (wanted {want!r})"))
    if not ok:
        fails.append(label)


def fresh():
    logs = []
    return Rotation(logs.append), logs


# ---- level 1-3: nothing but Lightning Bolt and Healing Wave
r, _ = fresh()
check("L1 healthy -> bolt filler (None)", r.fight_spell(mk(1), now=100.0), None)
check("L1 hp 40 -> Healing Wave", r.fight_spell(mk(1, hp=40, mana=80, t_hp=80, t_on_me=True, within10=True), attackers=1, now=101.0), "Healing Wave")
r, _ = fresh()
check("L1 hp 40 but mob nearly dead -> no heal", r.fight_spell(mk(1, hp=40, mana=80, t_hp=20, t_on_me=True), attackers=1, now=100.0), None)
r, _ = fresh()
check("L1 hp 40 mana too low for the wave", r.fight_spell(mk(1, hp=40, mana=20, t_hp=80, t_on_me=True), attackers=1, now=100.0), None)
r, _ = fresh()
check("L2 casting -> no new cast", r.fight_spell(mk(2, hp=30, t_on_me=True, casting=True), attackers=1, now=100.0), None)

# ---- level 4-7: Earth Shock
r, _ = fresh()
# the "on cooldown" filler shock (SHOCK_MANA) is for a mob in melee only: from range the bolt is better per mana
check("L4 no filler shock at range, comfy mana", r.fight_spell(mk(4, mana=90), now=100.0), None)
r.note_cast("Earth Shock", True, mk(4, mana=90), now=100.0)
check("L4 shock paced 6 s", r.fight_spell(mk(4, mana=90), now=103.0), None)
check("L4 still no filler shock after 6 s", r.fight_spell(mk(4, mana=90), now=106.5), None)
r, _ = fresh()
check("L4 low mana -> no shock", r.fight_spell(mk(4, mana=45), now=100.0), None)
r, _ = fresh()
check("L4 interrupt a caster at 45% mana", r.fight_spell(mk(4, mana=45, t_casting=True), now=100.0), "Earth Shock")
r, _ = fresh()
check("L4 not ready flag -> nothing", r.fight_spell(mk(4, mana=90, ready=False), now=100.0), None)
r, _ = fresh()
check("L5 finisher when hurting", r.fight_spell(mk(5, hp=40, mana=45, t_hp=25, t_on_me=True), attackers=1, now=100.0), "Earth Shock")
r, _ = fresh()
check("L6 hp 20 mob 25%: finisher shock instead of a 3 s wave", r.fight_spell(mk(6, hp=20, mana=70, t_hp=25, t_on_me=True), attackers=1, now=100.0), "Earth Shock")
# REPORT change 3: Earth Shock as the mob arrives (within 10 yd, <= 65%, mana for shock + heal), finisher at any hp
# (L4: 96 mana; shock 30 + Healing Wave 25 = 55 = 57%)
r, _ = fresh()
check("L4 mob arrives at 60%, mana 70 -> Earth Shock", r.fight_spell(mk(4, mana=70, t_hp=60, t_on_me=True, within10=True), in_melee=True, attackers=1, now=100.0), "Earth Shock")
check("  reason", r.reason.startswith("mob in melee"), True)
r.note_cast("Earth Shock", True, mk(4, mana=70), now=100.0)
check("L4 arrival shock paced by the 6 s cooldown", r.fight_spell(mk(4, mana=60, t_hp=40, t_on_me=True, within10=True), in_melee=True, attackers=1, now=103.0), None)
r, _ = fresh()
check("L4 mob arrives at 60%, mana 50: no heal left after a shock -> none", r.fight_spell(mk(4, mana=50, t_hp=60, t_on_me=True, within10=True), in_melee=True, attackers=1, now=100.0), None)
r, _ = fresh()
check("L4 mob at 60% but at range -> no shock (bolt)", r.fight_spell(mk(4, mana=90, t_hp=60, t_on_me=True), attackers=1, now=100.0), None)
r, _ = fresh()
check("L4 fresh mob in melee, mana 90 -> filler shock", r.fight_spell(mk(4, mana=90, t_hp=90, t_on_me=True, within10=True), in_melee=True, attackers=1, now=100.0), "Earth Shock")
check("  reason", r.reason.startswith("on cooldown in melee"), True)
r, _ = fresh()
check("L4 fresh mob in melee, mana 45 -> no filler shock", r.fight_spell(mk(4, mana=45, t_hp=90, t_on_me=True, within10=True), in_melee=True, attackers=1, now=100.0), None)
r, _ = fresh()
check("L5 finisher at full hp (no own-hp gate)", r.fight_spell(mk(5, hp=90, mana=80, t_hp=25, t_on_me=True), attackers=1, now=100.0), "Earth Shock")
r, _ = fresh()
check("L5 healthy finisher keeps the heal's mana", r.fight_spell(mk(5, hp=90, mana=45, t_hp=25, t_on_me=True), attackers=1, now=100.0), None)
r, _ = fresh()
check("L5 hurting finisher still goes at low mana", r.fight_spell(mk(5, hp=40, mana=35, t_hp=25, t_on_me=True), attackers=1, now=100.0), "Earth Shock")
r, _ = fresh()
check("L3 no shock on the bar -> bolt", r.fight_spell(mk(3, mana=90, t_hp=60, t_on_me=True, within10=True), in_melee=True, attackers=1, now=100.0), None)
# no bolts at <= 25% in melee: the weapon finishes it (not a caster, not while the swing will not start)
check("melee_finish: in melee at 20%", melee_finish(mk(4, t_hp=20, within10=True)), True)
check("melee_finish: at 30%", melee_finish(mk(4, t_hp=30, within10=True)), False)
check("melee_finish: at range", melee_finish(mk(4, t_hp=20)), False)
check("melee_finish: caster", melee_finish(mk(4, t_hp=20, within10=True), caster=True), False)
check("melee_finish: swing never started", melee_finish(mk(4, t_hp=20, within10=True), swing_ok=False), False)
r, _ = fresh()
check("L6 hp 20 mob 80%: heal",r.fight_spell(mk(6, hp=20, mana=70, t_hp=80, t_on_me=True), attackers=1, now=100.0), "Healing Wave")

# ---- level 8-9: Lightning Shield, Stoneclaw
r, _ = fresh()
check("L8 pre-fight shield (mana 90, no aura)", r.buff_spell(mk(8, mana=90, has_target=False), now=100.0), "Lightning Shield")
check("L8 shield already up", Rotation().buff_spell(mk(8, mana=90, auras=1, has_target=False), now=100.0), None)
check("L8 mana too low for the shield", Rotation().buff_spell(mk(8, mana=60, has_target=False), now=100.0), None)
check("L8 no buffs in combat", Rotation().buff_spell(mk(8, mana=90, combat=True), now=100.0), None)
check("L7 no shield learned", Rotation().buff_spell(mk(7, mana=90), now=100.0), None)
r, _ = fresh()
check("L8 shield as pull opener", r.fight_spell(mk(8, mana=70, t_hp=100), now=100.0), "Lightning Shield")
r, logs = fresh()
r.note_cast("Lightning Shield", True, mk(8, mana=70, auras=0), now=100.0)
r.note_cast("Lightning Shield", True, mk(8, mana=70, auras=0), now=130.0)
check("L8 aura never shows twice -> shield switched off", r.buff_spell(mk(8, mana=95, has_target=False), now=200.0), None)
r, _ = fresh()
check("L8 panic: Stoneclaw before the heal", r.fight_spell(mk(8, hp=30, mana=60, t_hp=80, t_on_me=True, within10=True, auras=1), attackers=1, now=100.0), "Stoneclaw Totem")
r.note_cast("Stoneclaw Totem", True, mk(8, hp=30), now=100.0)
check("L8 then the heal", r.fight_spell(mk(8, hp=30, mana=55, t_hp=80, t_on_me=True, within10=True, auras=1), attackers=1, now=101.6), "Healing Wave")
r, _ = fresh()
check("L8 stoneclaw needs the mob within 10 yd", r.fight_spell(mk(8, hp=30, mana=60, t_hp=80, t_on_me=True, within10=False, auras=1), attackers=1, now=100.0), "Healing Wave")

# ---- level 10: Flame Shock opener, still only on a fresh mob
r, _ = fresh()
check("L10 flame shock opener", r.fight_spell(mk(10, mana=95, t_hp=100, auras=1), target=7, now=100.0), "Flame Shock")
r.note_cast("Flame Shock", True, mk(10, mana=90, auras=1), now=100.0)
check("L10 no second flame shock on the same mob, no filler shock", r.fight_spell(mk(10, mana=90, t_hp=90, auras=1), target=7, now=107.0), None)
r, _ = fresh()
check("L10 no flame shock at 50% mana", r.fight_spell(mk(10, mana=50, t_hp=100, auras=1), target=7, now=100.0), None)

# ---- every level: bars and the first-use log
for lv in range(1, 11):
    r, logs = fresh()
    b = r.bar(mk(lv))
    print(f"     L{lv} bar: " + ", ".join(f"{n}@{v[0]}(id {v[1]})" for n, v in sorted(b.items(), key=lambda kv: kv[1][0])))
r, logs = fresh()
r.reason = "interrupt"
r.note_cast("Earth Shock", True, mk(4), now=1.0)
r.note_cast("Earth Shock", True, mk(4), now=9.0)
check("first-use log once, then per-cast line", [l.split(" ")[1] for l in logs], ["first", "Earth"])
print("\n".join("     log: " + l for l in logs))

print("\nFAILED: " + ", ".join(fails) if fails else "\nall passed")
if __name__ == "__main__":
    sys.exit(1 if fails else 0)
