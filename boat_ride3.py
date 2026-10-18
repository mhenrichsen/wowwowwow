"""Boat ride experiment 3 (GM account, bot stopped).

Rides Menethil -> Theramore like boat_ride2 and then tries to walk off the
deck with click-to-move targets in the BOAT-LOCAL frame (on a transport the
client reports and takes coordinates relative to the boat), one direction
at a time, until the position turns back into world coordinates. Dumps the
boat model struct at both docks (to find its rotation) and finally reads
the map-id candidates from map 369 before returning to Thelsamar.

    python boat_ride3.py
"""
import struct, time
from wowmem import Process
from game import Game
from keys import GuardedKeyboard
from ctm import ClickToMove
import offsets as O
from boat_board import BOAT, STOP_EK, STOP_KAL, PIER_EK, SAFE

CANDS = [0x868604, 0x86a2cc, 0x86f694, 0x88262c, 0xb01ed4, 0xb23128, 0xb4e378, 0xb71e84, 0xc4c498, 0xc7cd84, 0xc81b70]


def boat_obj(p, g, entry):
    for base, guid, typ in g.iter_objects():
        if typ != O.TYPE_GAMEOBJECT:
            continue
        d = p.u32(base + O.OBJ_DESCRIPTORS)
        if p.u32(d + O.OBJECT_FIELD_ENTRY) == entry:
            ptr = p.u32(base + 0x210)
            if 0x00400000 <= ptr < 0x7FFF0000 and ptr % 4 == 0:
                return ptr
    return None


def boat_pos(p, g, entry):
    ptr = boat_obj(p, g, entry)
    if ptr is None:
        return None
    raw = p.try_read(ptr + 0x44, 12)
    return None if raw is None else struct.unpack("<3f", raw)


def dump(p, g, entry, label):
    ptr = boat_obj(p, g, entry)
    raw = None if ptr is None else p.try_read(ptr, 0x120)
    if raw is None:
        print(f"{label}: no boat struct", flush=True); return
    fl = struct.unpack("<72f", raw)
    print(f"{label}: " + " ".join(f"+{i*4:x}={v:.3f}" for i, v in enumerate(fl) if v == v and 1e-4 < abs(v) < 1e5), flush=True)


def player_or_none(g):
    try:
        return g.player()
    except Exception:
        return None


def on_boat(me):
    return abs(me.x) < 60 and abs(me.y) < 60


def main():
    p = Process(writable=True); g = Game(p); kb = GuardedKeyboard(p.hwnd, g.in_world); ctm = ClickToMove(p)
    kb.chat(".gm on"); time.sleep(0.5)
    kb.chat(f".go xyz {PIER_EK[0]} {PIER_EK[1]} {PIER_EK[2]} 0"); time.sleep(5)
    try:
        t0 = time.time()
        while time.time() - t0 < 660:
            bp = boat_pos(p, g, BOAT)
            if bp is not None and ((bp[0] - STOP_EK[0]) ** 2 + (bp[1] - STOP_EK[1]) ** 2) ** 0.5 < 1.5:
                break
            time.sleep(1.0)
        else:
            print("boat never docked"); return
        print(f"boat docked at Menethil {time.strftime('%H:%M:%S')}", flush=True)
        dump(p, g, BOAT, "struct at Menethil")
        ctm.move_to(STOP_EK[0], STOP_EK[1], 8.0)
        time.sleep(6)
        me = g.player(); print(f"   after boarding: ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)
        t1 = time.time(); lost = None
        while time.time() - t1 < 600:
            me = player_or_none(g)
            if me is None:
                lost = lost or time.time(); time.sleep(0.5); continue
            if lost:
                print(f"ride {time.time() - t1:5.0f}s back after the loading screen", flush=True); lost = None
            bp = boat_pos(p, g, BOAT)
            if bp is not None and ((bp[0] - STOP_KAL[0]) ** 2 + (bp[1] - STOP_KAL[1]) ** 2) ** 0.5 < 1.5:
                print(f"   docked at Theramore {time.strftime('%H:%M:%S')} me local ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)
                dump(p, g, BOAT, "struct at Theramore")
                for dx, dy in ((0, -20), (0, 20), (20, 0), (-20, 0), (14, -14), (-14, -14), (14, 14), (-14, 14)):
                    ctm.move_to(dx, dy, 6.1)
                    for i in range(8):
                        time.sleep(1.0); me = g.player()
                        if not on_boat(me):
                            break
                    print(f"   local target ({dx},{dy}) -> ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) mf={me.move_flags:#x} {'OFF THE BOAT' if not on_boat(me) else 'still aboard'}", flush=True)
                    if not on_boat(me):
                        break
                break
            time.sleep(2.0)
        else:
            print("never docked at Theramore", flush=True)
        # map-id candidates on the tram map
        kb.chat(".go xyz 67.76 2490.98 -4.3 369"); time.sleep(8)
        print("candidates on map 369: " + " ".join(f"{a:#x}={p.u32(a)}" for a in CANDS), flush=True)
    finally:
        for _ in range(3):
            try:
                kb.chat(f".go xyz {SAFE[0]} {SAFE[1]} {SAFE[2]} {SAFE[3]}"); time.sleep(6)
                kb.chat(".gm off"); time.sleep(0.5)
                me = g.player(); print(f"safe at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) candidates on map 0: "
                                       + " ".join(f"{a:#x}={p.u32(a)}" for a in CANDS), flush=True)
                break
            except Exception as e:
                print("return failed:", e, flush=True); time.sleep(5)


if __name__ == "__main__":
    main()
