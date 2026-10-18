"""Boat boarding experiment (GM account, bot stopped).

Waits on the Menethil pier until the Theramore boat's live position (the
client keeps it at *(object+0x528)+0x44) sits at its stop, walks onto the
deck, and logs the ride to Theramore. Ends by teleporting to Thelsamar
(a safe town spot) and turning GM off.

    python boat_board.py [deck_z]
"""
import struct, sys, time
from wowmem import Process
from game import Game
from keys import GuardedKeyboard
from ctm import ClickToMove
import offsets as O

BOAT = 176231                                  # Menethil <-> Theramore
STOP_EK = (-3905.2, -585.8)                    # boat origin when docked at Menethil
STOP_KAL = (-4016.4, -4740.6)                  # ... and at Theramore
PIER_EK = (-3905.0, -603.0, 5.4)               # land spot next to the Menethil stop
PIER_KAL = (-4008.3, -4726.7, 5.3)             # land spot next to the Theramore stop
SAFE = (-5424.0, -2929.0, 348.0, 0)            # Thelsamar flight master
BOAT_POS_PTR, BOAT_POS_OFF = 0x210, 0x44        # decimal 528 and 68 in the scan output


def boat_pos(p, g, entry):
    for base, guid, typ in g.iter_objects():
        if typ != O.TYPE_GAMEOBJECT:
            continue
        d = p.u32(base + O.OBJ_DESCRIPTORS)
        if p.u32(d + O.OBJECT_FIELD_ENTRY) != entry:
            continue
        ptr = p.u32(base + BOAT_POS_PTR)
        if not (0x00400000 <= ptr < 0x7FFF0000) or ptr % 4:
            print(f"   boat object {base:#x} present, slot 0x528 = {ptr:#x} (no model yet)", flush=True)
            return None                 # the model is not loaded yet (the slot held 1.0f once)
        raw = p.try_read(ptr + BOAT_POS_OFF, 12)
        return None if raw is None else struct.unpack("<3f", raw)
    return None


def main():
    deck_z = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    p = Process(writable=True); g = Game(p); kb = GuardedKeyboard(p.hwnd, g.in_world); ctm = ClickToMove(p)
    me = g.player(); print(f"start at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)
    kb.chat(".gm on"); time.sleep(0.5)
    kb.chat(f".go xyz {PIER_EK[0]} {PIER_EK[1]} {PIER_EK[2]} 0"); time.sleep(5)
    try:
        t0 = time.time(); docked = None
        while time.time() - t0 < 660:
            bp = boat_pos(p, g, BOAT)
            me = g.player()
            d = None if bp is None else ((bp[0] - STOP_EK[0]) ** 2 + (bp[1] - STOP_EK[1]) ** 2) ** 0.5
            print(f"{time.time() - t0:6.1f}s me ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) mf={me.move_flags:#x} boat="
                  + ("-" if bp is None else f"({bp[0]:.1f}, {bp[1]:.1f}) {d:.1f} yd from the stop"), flush=True)
            if d is not None and d < 1.5:
                docked = time.time(); break
            time.sleep(1.0)
        if docked is None:
            print("boat never docked"); return
        print(f"boat docked at {time.strftime('%H:%M:%S')} - stepping onto the deck at z {deck_z}", flush=True)
        ctm.move_to(STOP_EK[0], STOP_EK[1], deck_z)
        for i in range(12):
            time.sleep(1.0)
            me = g.player()
            print(f"   step {i}: ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) mf={me.move_flags:#x}", flush=True)
            if me.z < 1.0:
                print("   fell in the water"); break
        # the ride
        t1 = time.time(); last = None
        while time.time() - t1 < 400:
            me = g.player(); bp = boat_pos(p, g, BOAT)
            print(f"ride {time.time() - t1:5.0f}s me ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) mf={me.move_flags:#x} boat="
                  + ("-" if bp is None else f"({bp[0]:.1f}, {bp[1]:.1f}, {bp[2]:.1f})"), flush=True)
            if bp is not None and ((bp[0] - STOP_KAL[0]) ** 2 + (bp[1] - STOP_KAL[1]) ** 2) ** 0.5 < 1.5 \
                    and abs(me.x - STOP_KAL[0]) < 60:
                print(f"   docked at Theramore at {time.strftime('%H:%M:%S')} - walking off", flush=True)
                ctm.move_to(*PIER_KAL)
                for i in range(15):
                    time.sleep(1.0); me = g.player()
                    print(f"   off {i}: ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) mf={me.move_flags:#x}", flush=True)
                break
            time.sleep(2.0)
    finally:
        kb.chat(f".go xyz {SAFE[0]} {SAFE[1]} {SAFE[2]} {SAFE[3]}"); time.sleep(5)
        kb.chat(".gm off"); time.sleep(0.5)
        me = g.player(); print(f"safe at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)


if __name__ == "__main__":
    main()
