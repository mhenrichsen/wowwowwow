"""Deeprun Tram boarding experiment (GM account, bot stopped).

Stands at the Stormwind-side platform edge of the -45 track and keeps
stepping onto the car's docking spot. When the car is not there the
character falls into the pit (z -13.8) and is teleported back up; when it
is there z stays at platform level - that moment gives the cycle phase.
Then the ride is logged until the car stops at the far end.

    python tram_board.py
"""
import time
from wowmem import Process
from game import Game
from keys import GuardedKeyboard
from ctm import ClickToMove

EDGE = (-38.7, 2492.7, -4.0)      # platform edge poly next to the -45 track (SW station)
CAR = (-45.5, 2492.9, -4.3)       # floor of the middle car when docked


def main():
    p = Process(writable=True); g = Game(p); kb = GuardedKeyboard(p.hwnd, g.in_world); ctm = ClickToMove(p)
    kb.chat(".gm on"); time.sleep(0.3)

    def goto(x, y, z):
        kb.chat(f".go xyz {x} {y} {z} 369"); time.sleep(3.0)

    def pos():
        me = g.player(); return me.x, me.y, me.z

    goto(*EDGE)
    t0 = time.time()
    boarded = None
    while time.time() - t0 < 200 and boarded is None:
        ctm.move_to(*CAR)
        time.sleep(1.8)
        x, y, z = pos()
        print(f"{time.time() - t0:6.1f}s after step: ({x:.1f}, {y:.1f}, {z:.1f})", flush=True)
        if z < -8:
            print("   fell into the pit - back up", flush=True)
            goto(*EDGE)
            continue
        if abs(x - CAR[0]) < 3.5 and z > -7:
            boarded = time.time()
            print(f"   ON THE CAR at {time.strftime('%H:%M:%S')} (unix {boarded:.1f})", flush=True)
            break
    if boarded is None:
        print("never boarded"); return
    last = pos(); still = 0; moved = False
    while time.time() - boarded < 150:
        time.sleep(1.0)
        x, y, z = pos()
        if abs(y - last[1]) > 1.0:
            moved = True; still = 0
        else:
            still += 1
        print(f"ride {time.time() - boarded:5.1f}s ({x:.1f}, {y:.1f}, {z:.1f}) moved={moved} still={still}", flush=True)
        last = (x, y, z)
        if moved and still >= 3:
            print(f"   car stopped at {time.strftime('%H:%M:%S')} (unix {time.time():.1f})", flush=True)
            break


if __name__ == "__main__":
    main()
