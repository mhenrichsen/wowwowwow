"""Click-to-move by writing the client's own CTM state (1.12.1 build 5875).

Layout (verified by recording a real right-click, see README):
  0xC4D888  u32    action: 4 = walk to point, 12 = idle
  0xC4D890  3xf32  destination x, y, z

Timer fix
---------
Out of the box, CTM on this client stops after a fraction of a second. The
per-frame CTM update (around 0x610C63) has a "stuck" test: if the mover
reports a collision flag and the unit moved less than 1/360 yd this frame,
CTM is cancelled. The client times frames with timeGetTime(), which Windows 11
only advances every 15.6 ms, while the client renders frames far faster than
that (165 Hz monitor) - so many frames see zero elapsed time, zero movement,
and trip the stuck test. We flip the conditional jump in front of that test
(je -> jmp) in the running process only; our own bot does stuck detection.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import struct
import threading
import time

from wowmem import Process, k32

CTM_ACTION = 0xC4D888
CTM_DEST = 0xC4D890
CTM_GUID = 0xC4D980              # u64, object for guid-based actions
ACTION_MOVE = 4
ACTION_LOOT = 6                  # verified: opens the corpse's loot window
ACTION_IDLE = 12
STEERED_ACTIONS = (4, 5, 6, 7)
# Recorded from a real right-click on a game object: besides action, dest and
# guid, the client writes an interaction distance, an approach angle and zeroes
# a float at 0xC4D910. Without the distance an interact order is dropped
# instantly (object) or only works when a manual click left it set (NPC).
CTM_INTERACT_DIST = 0xC4D998
CTM_ANGLE = 0xC4D8C4
CTM_UNK_910 = 0xC4D910
INTERACT_DISTANCE = 4.5   # move, talk to NPC, loot, use object
_ARRIVE_IDLE = struct.pack("<f", 0.5)
UNIT_FACING = 0x9C4

STUCK_JUMP = 0x610C6A            # je 0x610c77  (skips the stop when not stuck)
STUCK_ORIG = b"\x74\x0b"
STUCK_PATCH = b"\xeb\x0b"        # jmp 0x610c77 (always skip)

k32.VirtualProtectEx.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD,
                                 ctypes.POINTER(wt.DWORD))


def _patch(p: Process, addr: int, expect: bytes, new: bytes) -> bool:
    cur = p.read(addr, len(expect))
    if cur == new:
        return True
    if cur != expect:
        raise RuntimeError(f"unexpected bytes at {addr:#x}: {cur.hex()} - not patching")
    old = wt.DWORD()
    k32.VirtualProtectEx(p.handle, addr, len(new), 0x40, ctypes.byref(old))
    p.write(addr, new)
    k32.VirtualProtectEx(p.handle, addr, len(new), old.value, ctypes.byref(old))
    return True


class ClickToMove:
    def __init__(self, proc: Process):
        self.p = proc
        self._steer = None

    def start_steering(self, player_base):
        """Point the character at the CTM destination ourselves, 20x a second.

        The client turns toward its CTM target by turn-rate x frame time, and
        frame time comes from timeGetTime(), which only ticks every 15.6 ms.
        At a high (foreground) frame rate most frames measure 0 ms, so the
        character never turns - it keeps its old facing and runs straight past
        the destination like autorun. Forward motion is unaffected. Writing the
        facing directly fixes every CTM order; the next movement packet
        carries it to the server.

        `player_base` returns the local player object's address (or None).
        """
        if self._steer:
            return
        self._steer = threading.Thread(target=self._steer_loop, args=(player_base,), daemon=True)
        self._steer.start()

    TURN_RATE = math.pi            # rad/s, the client's own keyboard turn speed
    RUN_SPEED = 7.0                # yd/s
    MAX_TURN_RATE = 4 * math.pi    # a quick mouse swing: half a turn in 0.25 s

    def _steer_loop(self, player_base):
        """Turn toward the CTM destination at a human turn rate: snapping the
        facing in one tick looks like a bot spinning on the spot, so the
        heading swings over at most TURN_RATE per second, easing in at the
        end like a real turn."""
        base, base_t = None, 0.0
        last = time.time()
        while True:
            time.sleep(0.05)
            try:
                now = time.time()
                dt, last = min(now - last, 0.2), now
                if self.p.u32(CTM_ACTION) not in STEERED_ACTIONS:
                    continue
                if base is None or now - base_t > 2.0:
                    base, base_t = player_base(), now
                if not base:
                    continue
                x, y = struct.unpack("<2f", self.p.read(base + 0x9B8, 8))
                dx, dy = struct.unpack("<2f", self.p.read(CTM_DEST, 8))
                if math.hypot(dx - x, dy - y) < 0.1:
                    continue
                want = math.atan2(dy - y, dx - x)
                have = struct.unpack("<f", self.p.read(base + UNIT_FACING, 4))[0]
                delta = (want - have + math.pi) % (2 * math.pi) - math.pi
                rate = self.TURN_RATE * max(0.35, min(1.0, abs(delta) / 0.6))
                # We only move where we face, so a turn rate is a turning
                # circle: pi rad/s at 7 yd/s run speed is 2.2 yd radius, and the
                # easing above stretched it to 6.4 yd. A mob behind us or just
                # beside us sat inside that circle and the warrior ran rings
                # round it. Reaching a point d yd away at angle delta takes a
                # circle of radius d / (2 sin delta), i.e. a rate of
                # 2 v sin(delta) / d: always turn at least that fast (with a
                # margin), the way a player swings the mouse at a close target.
                dist = math.hypot(dx - x, dy - y)
                need = 2.0 * self.RUN_SPEED * math.sin(min(abs(delta), math.pi / 2)) / max(dist, 0.5)
                rate = max(rate, 1.6 * need)
                if abs(delta) > 1.2 and dist < 20.0:
                    # well off to the side or behind, and close: swing round
                    # at once. Turning at pi rad/s while running ran a 11 yd
                    # loop to reach a mob 5 yd behind us (simulated: 4 yd now)
                    rate = self.MAX_TURN_RATE
                rate = min(rate, self.MAX_TURN_RATE)
                step = min(abs(delta), rate * dt)
                self.p.write(base + UNIT_FACING,
                             struct.pack("<f", (have + math.copysign(step, delta)) % (2 * math.pi)))
            except Exception:
                base = None

    def apply_timer_fix(self):
        _patch(self.p, STUCK_JUMP, STUCK_ORIG, STUCK_PATCH)

    def remove_timer_fix(self):
        _patch(self.p, STUCK_JUMP, STUCK_PATCH, STUCK_ORIG)

    def fix_applied(self) -> bool:
        return self.p.read(STUCK_JUMP, 2) == STUCK_PATCH

    def move_to(self, x: float, y: float, z: float):
        # 0xC4D910 is the arrival distance of a plain move (idle value 0.5).
        # A real mouse click on an NPC leaves it at the click's own value -
        # 2.78 after "talked to Myra Tyrngaarde with a click" - and from then
        # on every waypoint closer than that counts as reached before we take
        # a step: the character ran 1-2 s, stood 2.5 s, "stuck", for 14
        # minutes through Ironforge and out of the gates, and "learned"
        # unclimbable steps on a flat road. Set it on every move.
        if self.p.read(CTM_UNK_910, 4) != _ARRIVE_IDLE:
            self.p.write(CTM_UNK_910, _ARRIVE_IDLE)
        self.p.write(CTM_DEST, struct.pack("<3f", x, y, z))
        self.p.write_u32(CTM_ACTION, ACTION_MOVE)

    def move_toward(self, me, tx: float, ty: float, tz: float, stop_short: float):
        """Walk toward a point, stopping `stop_short` yards before it."""
        dx, dy = tx - me.x, ty - me.y
        d = math.hypot(dx, dy)
        if d <= stop_short:
            return
        k = (d - stop_short) / d
        self.move_to(me.x + dx * k, me.y + dy * k, tz)

    def face(self, me, tx: float, ty: float):
        """Turn toward a point by issuing a tiny CTM step toward it."""
        ang = math.atan2(ty - me.y, tx - me.x)
        # A 0.3 yd step must not count as reached before it starts: with the
        # plain-move arrival distance (0.5, see move_to) the order goes idle
        # at once, the steering thread never turns us, and a click on the NPC
        # or object in front finds nothing on screen (Milly's Harvest crates,
        # Marshal McBride). Measured: arrival 0.0 turns to 0.00 rad every time.
        self.p.write(CTM_UNK_910, struct.pack("<f", 0.0))
        self.p.write(CTM_DEST, struct.pack("<3f", me.x + 0.3 * math.cos(ang),
                                           me.y + 0.3 * math.sin(ang), me.z))
        self.p.write_u32(CTM_ACTION, ACTION_MOVE)

    def stop(self, me):
        """Stop by walking to where we already stand.

        Writing ACTION_IDLE directly abandons CTM without the client ever
        sending a stop: the character keeps running (13 yd in testing) and the
        server keeps seeing a moving player, which blocks casting and cancels
        loot. A CTM to our own position "arrives" on the next frame and makes
        the client send a proper MSG_MOVE_STOP.
        """
        self.move_to(me.x, me.y, me.z)

    def interact(self, action: int, guid: int, me, x: float, y: float, z: float):
        """Walk to a unit / object and use it (action 5 = NPC, 7 = object),
        the way a right-click does."""
        self.p.write(CTM_INTERACT_DIST, struct.pack("<f", INTERACT_DISTANCE))
        self.p.write(CTM_UNK_910, struct.pack("<f", 0.0))
        self.p.write(CTM_ANGLE, struct.pack("<f", math.atan2(y - me.y, x - me.x) % (2 * math.pi)))
        self.p.write(CTM_GUID, struct.pack("<Q", guid))
        self.p.write(CTM_DEST, struct.pack("<3f", x, y, z))
        self.p.write_u32(CTM_ACTION, action)

    def reset_fields(self):
        """Put back the idle values of the two fields an object click fills in
        (0xC4D998 = 4.5, 0xC4D910 = 0). Left as the object click set them,
        CTM-loot and NPC talk never trigger again: every loot after the first
        crate attempt failed (19/19) until this was restored."""
        self.p.write(CTM_INTERACT_DIST, struct.pack("<f", 0.0))
        self.p.write(CTM_UNK_910, struct.pack("<f", 0.5))

    def loot(self, guid: int, x: float, y: float, z: float):
        """CTM action 6: walk to a corpse and open its loot window on arrival.

        Must be issued from more than ~1 yd away (from on top of the corpse it
        does nothing) and from a standstill.
        """
        self.reset_fields()
        self.p.write(CTM_GUID, struct.pack("<Q", guid))
        self.p.write(CTM_DEST, struct.pack("<3f", x, y, z))
        self.p.write_u32(CTM_ACTION, ACTION_LOOT)

    @property
    def moving(self) -> bool:
        return self.p.u32(CTM_ACTION) == ACTION_MOVE
