"""Boat ride experiment 4 (GM account, bot stopped): ride to Theramore and
try to walk off the deck with the KEYBOARD - turn in 45-degree steps and
hold forward for 3 s each time, until the position turns back into world
coordinates. Screenshots go to probe_shots/.

    python boat_ride4.py
"""
import os, struct, time, math
from wowmem import Process
from game import Game
from keys import GuardedKeyboard
from ctm import ClickToMove
import screenshot
from boat_board import BOAT, STOP_EK, STOP_KAL, PIER_EK, SAFE
from boat_ride3 import boat_pos, boat_obj, player_or_none, on_boat

SHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_shots")


def shot(p, name):
    try:
        os.makedirs(SHOTS, exist_ok=True)
        screenshot.capture(os.path.join(SHOTS, name), p.hwnd, scale=0.5)
    except Exception as e:
        print("   screenshot failed:", e, flush=True)


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
        shot(p, "menethil_docked.png")
        ctm.move_to(STOP_EK[0], STOP_EK[1], 8.0)
        time.sleep(6)
        me = g.player(); print(f"   after boarding: ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) facing {me.facing:.2f}", flush=True)
        shot(p, "menethil_aboard.png")
        t1 = time.time(); lost = None
        while time.time() - t1 < 600:
            me = player_or_none(g)
            if me is None:
                lost = lost or time.time(); time.sleep(0.5); continue
            if lost:
                print(f"ride {time.time() - t1:5.0f}s back after the loading screen, map {g.map_id()}", flush=True); lost = None
            bp = boat_pos(p, g, BOAT)
            if bp is not None and ((bp[0] - STOP_KAL[0]) ** 2 + (bp[1] - STOP_KAL[1]) ** 2) ** 0.5 < 1.5:
                print(f"   docked at Theramore {time.strftime('%H:%M:%S')} me local ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) facing {me.facing:.2f}", flush=True)
                shot(p, "theramore_docked.png")
                for k in range(8):
                    kb.down("w"); time.sleep(3.0); kb.up("w"); time.sleep(0.5)
                    me = g.player()
                    print(f"   forward try {k}: ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) facing {me.facing:.2f} mf={me.move_flags:#x} "
                          f"{'OFF THE BOAT' if not on_boat(me) else 'still aboard'}", flush=True)
                    shot(p, f"theramore_try{k}.png")
                    if not on_boat(me):
                        break
                    kb.down("left"); time.sleep(0.25); kb.up("left"); time.sleep(0.3)   # ~45 degrees
                break
            time.sleep(2.0)
        else:
            print("never docked at Theramore", flush=True)
    finally:
        for _ in range(3):
            try:
                kb.chat(f".go xyz {SAFE[0]} {SAFE[1]} {SAFE[2]} {SAFE[3]}"); time.sleep(6)
                kb.chat(".gm off"); time.sleep(0.5)
                me = g.player(); print(f"safe at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) map {g.map_id()}", flush=True)
                break
            except Exception as e:
                print("return failed:", e, flush=True); time.sleep(5)


if __name__ == "__main__":
    main()
