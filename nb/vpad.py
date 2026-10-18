"""Virtual Xbox pad (ViGEmBus + vgamepad) for analog steering.

The Forever client reads the pad even with a keyboard in use: left stick
forward runs, right stick X turns the RUNNING character at a rate that grows
with deflection (measured 2026-10-04: dead below ~0.3, 0.2 rad/s at 0.4,
0.9 at 0.6, 2.0 at 0.8, 3.5 at 1.0). A standing character does not turn, so
turns in place stay on the arrow keys.
"""

from __future__ import annotations

import time

try:
    import vgamepad as vg
except Exception:                       # not installed: callers fall back to keys
    vg = None

# (deflection, rad/s) measured with the left stick held forward
CURVE = [(0.0, 0.0), (0.34, 0.0), (0.40, 0.20), (0.60, 0.885), (0.80, 1.963), (1.0, 3.475)]


def deflection_for(rate: float) -> float:
    """Right-stick |x| that gives |rate| rad/s (inverse of CURVE, clamped)."""
    r = min(abs(rate), CURVE[-1][1])
    if r < 0.05:
        return 0.0
    for (d0, r0), (d1, r1) in zip(CURVE, CURVE[1:]):
        if r <= r1 and r1 > r0:
            return d0 + (d1 - d0) * (r - r0) / (r1 - r0)
    return 1.0


class Pad:
    def __init__(self):
        if vg is None:
            raise RuntimeError("vgamepad not installed")
        self.pad = vg.VX360Gamepad()
        self.fwd = 0.0
        self.turn_x = 0.0
        self._push()
        time.sleep(1.5)                 # let the client enumerate the new device

    def _push(self):
        self.pad.left_joystick_float(0.0, self.fwd)
        self.pad.right_joystick_float(self.turn_x, 0.0)
        self.pad.update()

    def forward(self, amount: float = 1.0):
        if amount != self.fwd:
            self.fwd = amount
            self._push()

    def turn(self, rate: float):
        """Turn rate in rad/s, positive = left (the bot's facing convention)."""
        x = -deflection_for(rate) if rate > 0 else deflection_for(rate)
        if x != self.turn_x:
            self.turn_x = x
            self._push()

    def stop(self):
        self.fwd = 0.0
        self.turn_x = 0.0
        self._push()


def _press_a(self, secs: float = 0.15):
    """Tap the A button (the client's soft-target interact on a gamepad)."""
    self.pad.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
    self.pad.update()
    time.sleep(secs)
    self.pad.release_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
    self.pad.update()


Pad.press_a = _press_a
