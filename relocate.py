"""Rescue a character stuck in terrain: log out, move it in the character DB,
log back in (character-select click only - no password needed).

    python relocate.py <name> <x> <y> <z>
"""
import sys
import time

import mouse
import worlddb
from game import Game
from keys import Keyboard
from wowmem import Process, find_wow_window


def relocate(name, x, y, z):
    with Process() as p:
        g = Game(p)
        if g.in_world():
            Keyboard(p.hwnd).chat("/script Logout()")
            t0 = time.time()
            while g.in_world() and time.time() - t0 < 40:
                time.sleep(1)
        time.sleep(3)
        worlddb._query_db("characters",
                          f"UPDATE characters SET position_x={x}, position_y={y}, position_z={z} "
                          f"WHERE name='{name}'")
        mouse.click(find_wow_window(), 956, 990)            # Enter World
        t0 = time.time()
        while not g.in_world() and time.time() - t0 < 60:
            time.sleep(1)
        time.sleep(4)
        me = g.player()
        return round(me.x, 1), round(me.y, 1)


if __name__ == "__main__":
    n, x, y, z = sys.argv[1], *map(float, sys.argv[2:5])
    print("now at", relocate(n, x, y, z))
