"""Idle-time probe for the nb bot: where does the character just stand there?

Samples the BotEye strip ~10 Hz (read-only, sends no input) and puts every
moment in one bucket:

    move   running / walking
    cast   casting
    melee  auto-attack on
    loot   loot window up
    rest   not moving, hp or mana below max and rising (eating / drinking / regen)
    fight  has a target, in a fight, but not casting or swinging (waiting for the mob, GCD)
    IDLE   none of the above: wasted time

Prints a summary every --every seconds and appends every IDLE stretch longer
than --min seconds to nb/idle.log together with the bot log lines around it.

    python -m nb.idle_probe [--every 60] [--min 0.8]
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb.eye import Eye    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# --inst N: one probe per client window (inst 1 logs in nb/, the others in nb/instN/)
INST = int(sys.argv[sys.argv.index("--inst") + 1]) if "--inst" in sys.argv else 1
IDIR = HERE if INST == 1 else os.path.join(HERE, f"inst{INST}")
BOTLOG = os.path.join(IDIR, "nb.log")
OUT = os.path.join(IDIR, "idle.log")


def arg(name, default):
    return float(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def tail_lines(n=3):
    try:
        with open(BOTLOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4000))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except Exception:
        return []


def classify(s, prev):
    if s.moving:
        return "move"
    if s.casting_now:
        return "cast"
    if s.ui_open(4):
        return "loot"
    if s.autoattack:
        return "melee"
    regen = prev is not None and (s.hp > prev.hp or s.mana > prev.mana or s.hp < s.hpmax or s.mana < s.manamax) \
        and not s.has_target and not s.combat
    if regen and (s.hp < s.hpmax or s.mana < s.manamax):
        return "rest"
    if s.has_target and (s.t_on_me or s.t_hp_pct < 100 or s.combat or any(p.on_me for p in s.plates)):
        return "fight"
    return "IDLE"


def main():
    every, min_idle = arg("--every", 60.0), arg("--min", 0.8)
    from nb.multi import find_instance_window
    eye = Eye(find_instance_window(INST))
    tot: dict[str, float] = {}
    t_last, prev, cur, since, before = time.time(), None, None, time.time(), []
    t_sum = time.time()
    print("idle probe running (read-only)", flush=True)
    while not os.path.exists(os.path.join(HERE, "STOP_PROBE")):
        s = eye.read(retries=2)
        now = time.time()
        if s is None:
            time.sleep(0.2)
            continue
        dt = now - t_last
        t_last = now
        k = classify(s, prev)
        tot[cur or k] = tot.get(cur or k, 0.0) + dt
        if k != cur:
            if cur == "IDLE" and now - since >= min_idle:
                after = tail_lines(2)
                with open(OUT, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%H:%M:%S ", time.localtime(since)) +
                            f"IDLE {now - since:4.1f}s at ({s.x:.0f},{s.y:.0f}) hp {s.hp_pct:.0f}% mana {s.mana_pct:.0f}% "
                            f"tgt={int(s.has_target)} | before: {before[-1] if before else ''} | after: {after[-1] if after else ''}\n")
            cur, since = k, now
            if k == "IDLE":
                before = tail_lines(1)
        prev = s
        if now - t_sum >= every:
            span = sum(tot.values()) or 1.0
            line = " ".join(f"{b} {100 * v / span:4.1f}%" for b, v in sorted(tot.items(), key=lambda kv: -kv[1]))
            print(f"{time.strftime('%H:%M:%S')} [{INST}] lvl {s.level} last {span:.0f}s: {line}", flush=True)
            tot, t_sum = {}, now
        time.sleep(0.08)


if __name__ == "__main__":
    main()
