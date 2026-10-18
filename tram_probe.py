"""Deeprun Tram experiment (GM account): teleport into the tram map and watch
the player and the Subway gameobjects for a while, to learn whether the
animated car positions show up in the object manager.

    python tram_probe.py [seconds]

Run with the bot STOPPED. Leaves the character in the tram with .gm on.
"""
import struct, sys, time
from wowmem import Process
from game import Game
from keys import GuardedKeyboard
import worlddb

DBC = r"C:\Users\MadsG\Desktop\wow-forever-server\server\data\5875\dbc\TransportAnimation.dbc"
SUBWAY = set(range(176080, 176087))


def anim(entry):
    d = open(DBC, 'rb').read()
    magic, nrec, nfld, rsize, ssize = struct.unpack_from("<4sIIII", d, 0)
    rows = [struct.unpack_from("<IIIfffI", d, 20 + i * rsize) for i in range(nrec)]
    return sorted((r[2] / 1000, r[3], r[4], r[5]) for r in rows if r[1] == entry)


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 160
    p = Process(); g = Game(p); kb = GuardedKeyboard(p.hwnd, g.in_world)
    me = g.player()
    print(f"start at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) dead={me.dead} ghost={me.ghost}")
    kb.chat(".gm on"); time.sleep(0.5)
    if me.dead or me.ghost:
        kb.chat(".revive"); time.sleep(2)
    kb.chat(".go xyz 67.76 2490.98 -4.3 369")
    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            me = g.player()
            if abs(me.x - 67.76) < 30 and abs(me.y - 2490.98) < 30:
                break
        except Exception:
            pass
        time.sleep(0.5)
    time.sleep(2)
    print("nodes 176080 (SW track car):")
    for t, x, y, z in anim(176080):
        print(f"   t={t:6.1f}  ({x:7.1f}, {y:5.1f}, {z:6.1f})")
    t0 = time.time()
    last = {}
    while time.time() - t0 < secs:
        me = g.player()
        gos = [o for o in g.gameobjects() if o["entry"] in SUBWAY]
        line = f"{time.time() - t0:6.1f}s me ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})"
        for o in sorted(gos, key=lambda o: o["entry"]):
            line += f" | {o['entry']} ({o['x']:.1f}, {o['y']:.1f}, {o['z']:.1f}) dyn={o['dyn']:#x}"
        print(line, flush=True)
        time.sleep(2.0)


if __name__ == "__main__":
    main()
