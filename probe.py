"""Print what the bot can see. Use this to verify offsets against the screen."""

import math
import sys
from collections import Counter

from game import Game
from wowmem import Process


def main():
    with Process() as proc:
        g = Game(proc)
        print(f"pid {proc.pid}, in world: {g.in_world()}")
        print(f"name       : {g.player_name()!r}")
        print(f"local guid : {g.local_guid():#x}")
        types = Counter(t for _, _, t in g.iter_objects())
        print(f"objects    : {dict(sorted(types.items()))}")
        me = g.player()
        print(f"player     : lvl {me.level}  hp {me.health}/{me.max_health}  "
              f"mana {me.power}/{me.max_power}  xp {me.xp}/{me.next_xp}  "
              f"money {me.money}c")
        print(f"position   : x={me.x:.2f} y={me.y:.2f} z={me.z:.2f} "
              f"facing={math.degrees(me.facing):.0f} deg")
        print(f"flags      : {me.flags:#x}  combat={me.in_combat}  auras={me.auras}")
        tg = g.target_guid()
        print(f"target     : {tg:#x}")
        units = sorted(g.units(), key=me.dist)
        print(f"\nnearest units ({len(units)} total):")
        for u in units[:int(sys.argv[1]) if len(sys.argv) > 1 else 15]:
            mark = "<-- TARGET" if u.guid == tg else ""
            print(f"  entry {u.entry:>6} lvl {u.level:>2} hp {u.health:>4}/{u.max_health:<4} "
                  f"dist {me.dist(u):5.1f}  faction {u.faction:>4} flags {u.flags:#010x} "
                  f"npc {u.npc_flags:#x} dyn {u.dyn_flags:#x} {mark}")


if __name__ == "__main__":
    main()
