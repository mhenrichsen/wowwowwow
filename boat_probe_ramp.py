"""Find the boarding ramp of the Menethil -> Theramore boat (GM, bot stopped).

When the boat is docked, teleport to points along the pier edge next to the
hull and step toward the boat at each: z rises to deck height where the
ramp is, drops into the water elsewhere. Prints the ramp spot in world
coordinates and relative to the boat (position + heading at +0x50).
"""
import math, struct, time
from wowmem import Process
from game import Game
from keys import GuardedKeyboard
from ctm import ClickToMove
from boat_board import BOAT, STOP_EK, PIER_EK, SAFE
from boat_ride3 import boat_pos, boat_obj


def main():
    p = Process(writable=True); g = Game(p); kb = GuardedKeyboard(p.hwnd, g.in_world); ctm = ClickToMove(p)
    kb.chat(".gm on"); time.sleep(0.5)
    kb.chat(f".go xyz {PIER_EK[0]} {PIER_EK[1]} {PIER_EK[2]} 0"); time.sleep(5)
    try:
        t0 = time.time()
        while time.time() - t0 < 660:
            bp = boat_pos(p, g, BOAT)
            if bp is not None and math.dist(bp[:2], STOP_EK) < 1.5:
                break
            time.sleep(1.0)
        else:
            print("boat never docked"); return
        ptr = boat_obj(p, g, BOAT)
        heading = struct.unpack("<f", p.read(ptr + 0x50, 4))[0]
        print(f"docked {time.strftime('%H:%M:%S')} boat ({bp[0]:.1f}, {bp[1]:.1f}) heading {heading:.3f}", flush=True)
        # the hull lies along the pier; the pier edge runs roughly east-west
        # at y ~ -600 (the stop is at y -585.8, the pier spot at y -603)
        results = []
        for k in range(-7, 8):
            x = STOP_EK[0] + 3.0 * k
            kb.chat(f".go xyz {x:.1f} {PIER_EK[1]:.1f} {PIER_EK[2] + 1:.1f} 0"); time.sleep(2.0)
            ctm.move_to(x, STOP_EK[1], 8.0)
            time.sleep(2.8)
            me = g.player()
            state = "DECK" if me.z > 3.5 else ("water" if me.z < 1.0 else "pier")
            results.append((round(x, 1), state, round(me.x, 1), round(me.y, 1), round(me.z, 1)))
            print(f"   x {x:7.1f}: {state} at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True)
            bp2 = boat_pos(p, g, BOAT)
            if bp2 is None or math.dist(bp2[:2], STOP_EK) > 1.5:
                print("   the boat left", flush=True); break
        deck = [r for r in results if r[1] == "DECK"]
        if deck:
            for r in deck:
                dx, dy = r[0] - bp[0], PIER_EK[1] - bp[1]
                c, s = math.cos(-heading), math.sin(-heading)
                print(f"ramp: world x {r[0]} -> boat-local ({dx * c - dy * s:.1f}, {dx * s + dy * c:.1f})", flush=True)
    finally:
        for _ in range(3):
            try:
                kb.chat(f".go xyz {SAFE[0]} {SAFE[1]} {SAFE[2]} {SAFE[3]}"); time.sleep(6)
                kb.chat(".gm off"); time.sleep(0.5)
                me = g.player(); print(f"safe at ({me.x:.1f}, {me.y:.1f}, {me.z:.1f})", flush=True); break
            except Exception as e:
                print("return failed:", e, flush=True); time.sleep(5)


if __name__ == "__main__":
    main()
