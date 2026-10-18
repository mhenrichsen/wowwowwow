"""Boat ride experiment 5 (GM, bot stopped): board at Menethil from the
middle of the railing gap (pier x -3900), ride to Theramore and try
waypoint chains in the boat-local frame to reach the pier side (-y) or the
other side (+y). Logs every hop; screenshots at docking.
"""
import math, time
from wowmem import Process
from game import Game
from keys import GuardedKeyboard
from ctm import ClickToMove
from boat_board import BOAT, STOP_EK, STOP_KAL, SAFE
from boat_ride3 import boat_pos, player_or_none, on_boat
from boat_ride4 import shot

PIER_MID = (-3900.0, -603.0, 5.4)
CHAINS = [[(8, 0), (8, -10), (4, -17), (3, -24)],
          [(-8, 0), (-8, -10), (-4, -17), (-3, -24)],
          [(4, 10), (4, 17), (3, 24)],
          [(12, 12), (14, 18), (14, 24)],
          [(-12, -12), (-14, -18), (-14, -24)]]


def hop(g, ctm, x, y):
    ctm.move_to(x, y, 6.1)
    t0 = time.time(); last = None
    while time.time() - t0 < 5:
        time.sleep(0.5)
        me = g.player()
        if not on_boat(me):
            return me, True
        if math.dist((me.x, me.y), (x, y)) < 1.5:
            break
        if last and math.dist((me.x, me.y), last) < 0.1 and time.time() - t0 > 1.5:
            break
        last = (me.x, me.y)
    return g.player(), False


def main():
    p = Process(writable=True); g = Game(p); kb = GuardedKeyboard(p.hwnd, g.in_world); ctm = ClickToMove(p)
    kb.chat(".gm on"); time.sleep(0.5)
    kb.chat(f".go xyz {PIER_MID[0]} {PIER_MID[1]} {PIER_MID[2]} 0"); time.sleep(5)
    try:
        t0 = time.time()
        while time.time() - t0 < 660:
            bp = boat_pos(p, g, BOAT)
            if bp is not None and math.dist(bp[:2], STOP_EK) < 1.5:
                break
            time.sleep(1.0)
        else:
            print("boat never docked"); return
        print(f"docked at Menethil {time.strftime('%H:%M:%S')}", flush=True)
        ctm.move_to(STOP_EK[0] + 4.0, STOP_EK[1], 8.0)
        time.sleep(6)
        me = g.player(); print(f"   after boarding: ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) {'aboard' if on_boat(me) else 'NOT aboard'}", flush=True)
        if not on_boat(me):
            return
        t1 = time.time(); lost = None
        while time.time() - t1 < 600:
            me = player_or_none(g)
            if me is None:
                lost = lost or time.time(); time.sleep(0.5); continue
            if lost:
                print(f"ride {time.time() - t1:5.0f}s back after the loading screen, map {g.map_id()}", flush=True); lost = None
            bp = boat_pos(p, g, BOAT)
            if bp is not None and math.dist(bp[:2], STOP_KAL) < 1.5:
                print(f"   docked at Theramore {time.strftime('%H:%M:%S')} me local ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)
                shot(p, "theramore_docked2.png")
                for chain in CHAINS:
                    trail = []
                    for (x, y) in chain:
                        me, off = hop(g, ctm, x, y)
                        trail.append(f"({x},{y})->({me.x:.1f},{me.y:.1f},{me.z:.1f})")
                        if off:
                            break
                    print("   chain " + " ".join(trail) + ("  OFF THE BOAT" if off else ""), flush=True)
                    if off:
                        shot(p, "theramore_off.png"); break
                    bp = boat_pos(p, g, BOAT)
                    if bp is None or math.dist(bp[:2], STOP_KAL) > 1.5:
                        print("   the boat left", flush=True); break
                break
            time.sleep(2.0)
        else:
            print("never docked at Theramore", flush=True)
    finally:
        for _ in range(3):
            try:
                kb.chat(f".go xyz {SAFE[0]} {SAFE[1]} {SAFE[2]} {SAFE[3]}"); time.sleep(6)
                kb.chat(".gm off"); time.sleep(0.5)
                me = g.player(); print(f"safe at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f}) map {g.map_id()}", flush=True); break
            except Exception as e:
                print("return failed:", e, flush=True); time.sleep(5)


if __name__ == "__main__":
    main()
