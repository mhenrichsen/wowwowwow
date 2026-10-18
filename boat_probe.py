"""Boat experiment (GM account, bot stopped): teleport to the Menethil pier
and watch the Theramore / Auberdine boats for a few minutes.

Logs the player's position, each boat's descriptor position (expected
static, like the tram cars) and every float inside the boat's client object
struct that keeps changing - the client-animated position, if it is kept
there. Ends by teleporting back to where we started and turning GM off.

    python boat_probe.py [seconds]
"""
import struct, sys, time
from wowmem import Process
from game import Game
from keys import GuardedKeyboard
import offsets as O

BOATS = {176231: "Theramore boat", 176310: "Auberdine boat"}
PIER = (-3895.0, -600.0, 6.0)      # end of the Menethil pier, next to the Theramore boat's stop
SCAN = 0x400


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 360
    p = Process(writable=True); g = Game(p); kb = GuardedKeyboard(p.hwnd, g.in_world)
    me = g.player(); home = (me.x, me.y, me.z)
    print(f"start at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)
    kb.chat(".gm on"); time.sleep(0.5)
    kb.chat(f".go xyz {PIER[0]} {PIER[1]} {PIER[2]} 0")
    t0 = time.time()
    while time.time() - t0 < 25:
        try:
            me = g.player()
            if abs(me.x - PIER[0]) < 40 and abs(me.y - PIER[1]) < 40:
                break
        except Exception:
            pass
        time.sleep(0.5)
    time.sleep(3)
    t0 = time.time(); prev = {}
    try:
        while time.time() - t0 < secs:
            me = g.player()
            line = f"{time.time() - t0:6.1f}s me ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) mf={me.move_flags:#x}"
            for base, guid, typ in g.iter_objects():
                if typ != O.TYPE_GAMEOBJECT:
                    continue
                d = p.u32(base + O.OBJ_DESCRIPTORS)
                entry = p.u32(d + O.OBJECT_FIELD_ENTRY)
                if entry not in BOATS:
                    continue
                x, y, z = struct.unpack("<3f", p.read(d + O.GAMEOBJECT_POS_X, 12))
                line += f" | {BOATS[entry]} desc ({x:.1f}, {y:.1f}, {z:.1f})"
                blk = p.try_read(base, SCAN)
                if blk is None:
                    continue
                fl = struct.unpack(f"<{SCAN // 4}f", blk)
                old = prev.get(guid)
                if old:
                    ch = [(i * 4, round(v, 2)) for i, (v, o) in enumerate(zip(fl, old))
                          if abs(v) < 20000 and abs(v - o) > 0.05 and v == v and abs(v) > 1e-3]
                    if ch:
                        line += f" changed@{ch[:12]}"
                prev[guid] = fl
            print(line, flush=True)
            time.sleep(2.0)
    finally:
        kb.chat(f".go xyz {home[0]:.1f} {home[1]:.1f} {home[2]:.1f} 0"); time.sleep(4)
        kb.chat(".gm off"); time.sleep(0.5)
        me = g.player(); print(f"back at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)


if __name__ == "__main__":
    main()
