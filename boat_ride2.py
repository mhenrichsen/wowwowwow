"""Boat ride experiment 2 (GM account, bot stopped): board the Theramore boat
at Menethil (see boat_board.py), survive the loading screen, walk off at
Theramore, and diff the client's static data against the map-0 snapshot to
find the current-map-id global. Ends at Thelsamar with GM off.

    python boat_ride2.py
"""
import os, struct, sys, time
from wowmem import Process
from game import Game
from keys import GuardedKeyboard
from ctm import ClickToMove
import offsets as O
from boat_board import boat_pos, BOAT, STOP_EK, STOP_KAL, PIER_EK, PIER_KAL, SAFE

SNAP = r"C:\Users\MadsG\AppData\Local\Temp\claude\C--Users-MadsG-Desktop\8588e373-f424-4d6d-9024-f6f3fbafd63c\scratchpad"


def player_or_none(g):
    try:
        return g.player()
    except Exception:
        return None


def map_diff(p):
    old = open(os.path.join(SNAP, "snap_map0.bin"), "rb").read()
    idx = [int(l, 16) for l in open(os.path.join(SNAP, "snap_map0.idx"))]
    hits = []
    for k, a in enumerate(idx):
        b = p.try_read(a, 0x10000)
        if b is None:
            continue
        o = old[k * 0x10000:(k + 1) * 0x10000]
        for i in range(0, 0x10000, 4):
            if struct.unpack_from("<I", o, i)[0] == 0 and struct.unpack_from("<I", b, i)[0] == 1:
                hits.append(a + i)
    print(f"map diff: {len(hits)} dwords went 0 -> 1: {[hex(h) for h in hits[:60]]}", flush=True)


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
        print(f"boat docked at {time.strftime('%H:%M:%S')} - boarding", flush=True)
        ctm.move_to(STOP_EK[0], STOP_EK[1], 8.0)
        time.sleep(6)
        me = g.player(); print(f"   after boarding: ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) mf={me.move_flags:#x}", flush=True)
        t1 = time.time(); lost = None; diffed = False
        while time.time() - t1 < 600:
            me = player_or_none(g)
            if me is None:
                if lost is None:
                    lost = time.time(); print(f"ride {time.time() - t1:5.0f}s loading screen", flush=True)
                time.sleep(0.5); continue
            if lost is not None:
                print(f"ride {time.time() - t1:5.0f}s back in the world after {time.time() - lost:.0f}s at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)
                lost = None
                if not diffed:
                    time.sleep(3); map_diff(p); diffed = True
            bp = boat_pos(p, g, BOAT)
            print(f"ride {time.time() - t1:5.0f}s me ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) mf={me.move_flags:#x} boat="
                  + ("-" if bp is None else f"({bp[0]:.1f}, {bp[1]:.1f})"), flush=True)
            if bp is not None and ((bp[0] - STOP_KAL[0]) ** 2 + (bp[1] - STOP_KAL[1]) ** 2) ** 0.5 < 1.5:
                print(f"   docked at Theramore at {time.strftime('%H:%M:%S')} - walking off to {PIER_KAL}", flush=True)
                ctm.move_to(*PIER_KAL)
                for i in range(15):
                    time.sleep(1.0); me = g.player()
                    print(f"   off {i}: ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) mf={me.move_flags:#x}", flush=True)
                break
            time.sleep(2.0)
    finally:
        for _ in range(3):
            try:
                kb.chat(f".go xyz {SAFE[0]} {SAFE[1]} {SAFE[2]} {SAFE[3]}"); time.sleep(6)
                kb.chat(".gm off"); time.sleep(0.5)
                me = g.player(); print(f"safe at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)
                break
            except Exception as e:
                print("return failed:", e, flush=True); time.sleep(5)


if __name__ == "__main__":
    main()
