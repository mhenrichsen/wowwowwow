"""Boat experiment 2 (GM account, bot stopped): find where the client keeps
the animated position of a boat.

Stands on the Menethil pier, and whenever a boat object is in the object
manager reads its struct (0x800 bytes) plus every block a pointer in it
leads to (depth 1, 0x400 bytes each), and reports float pairs that look like
a Menethil-area coordinate and keep changing between samples.

    python boat_probe2.py [seconds]
"""
import struct, sys, time
from wowmem import Process
from game import Game
from keys import GuardedKeyboard
import offsets as O

BOATS = {176231: "Theramore boat", 176310: "Auberdine boat"}
PIER = (-3895.0, -600.0, 6.0)
ROOT, BLK = 0x800, 0x400
XR, YR = (-4400.0, -3400.0), (-1100.0, -100.0)


def looks_ptr(v):
    return 0x00400000 <= v < 0x7FFF0000 and v % 4 == 0


def scan(p, base):
    """{(root_off, ptr_off): (x, y, z)} for every plausible coordinate triple."""
    out = {}
    root = p.try_read(base, ROOT)
    if root is None:
        return out
    blocks = [(-1, base, root)]
    for i in range(0, ROOT - 4, 4):
        v = struct.unpack_from("<I", root, i)[0]
        if looks_ptr(v):
            b = p.try_read(v, BLK)
            if b is not None:
                blocks.append((i, v, b))
    for off, addr, b in blocks:
        n = len(b) // 4 - 2
        fl = struct.unpack(f"<{len(b) // 4}f", b)
        for i in range(n):
            x, y, z = fl[i], fl[i + 1], fl[i + 2]
            if XR[0] <= x <= XR[1] and YR[0] <= y <= YR[1] and -20 <= z <= 60:
                out[(off, i * 4)] = (x, y, z)
    return out


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 400
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
            line = f"{time.time() - t0:6.1f}s me ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})"
            for base, guid, typ in g.iter_objects():
                if typ != O.TYPE_GAMEOBJECT:
                    continue
                d = p.u32(base + O.OBJ_DESCRIPTORS)
                entry = p.u32(d + O.OBJECT_FIELD_ENTRY)
                if entry not in BOATS:
                    continue
                cur = scan(p, base)
                old = prev.get(guid, {})
                moving = {k: v for k, v in cur.items()
                          if k in old and max(abs(a - b) for a, b in zip(v, old[k])) > 0.05}
                line += f" | {BOATS[entry]} base={base:#x} triples={len(cur)} moving={len(moving)}"
                for k, v in list(moving.items())[:6]:
                    line += f" {k}->({v[0]:.1f},{v[1]:.1f},{v[2]:.1f})"
                if not old and cur:
                    line += f" first={[(k, tuple(round(c, 1) for c in v)) for k, v in list(cur.items())[:8]]}"
                prev[guid] = cur
            print(line, flush=True)
            time.sleep(2.0)
    finally:
        kb.chat(f".go xyz {home[0]:.1f} {home[1]:.1f} {home[2]:.1f} 0"); time.sleep(4)
        kb.chat(".gm off"); time.sleep(0.5)
        me = g.player(); print(f"back at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)


if __name__ == "__main__":
    main()
