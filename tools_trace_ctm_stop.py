"""Diagnostic: log who calls the client's "stop click-to-move" routine (0x60FB60).

Installs a 6-byte detour in the RUNNING process only (never on disk), issues a
click-to-move, records the return addresses of every stop call into a ring
buffer, then restores the original bytes and frees the cave.
"""

import ctypes
import ctypes.wintypes as wt
import math
import struct
import time

from game import Game
from wowmem import Process, k32

STOP_FN = 0x60FB60
ORIG = bytes.fromhex("558bec83ec18")  # push ebp; mov ebp,esp; sub esp,0x18

k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD)
k32.VirtualFreeEx.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD)
k32.VirtualProtectEx.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD,
                                 ctypes.POINTER(wt.DWORD))


def rel32(src_next: int, dst: int) -> bytes:
    return struct.pack("<i", dst - src_next)


def main():
    with Process(writable=True) as p:
        assert p.read(STOP_FN, 6) == ORIG, "unexpected prologue - not patching"
        cave = k32.VirtualAllocEx(p.handle, None, 0x1000, 0x3000, 0x40)
        assert cave, ctypes.get_last_error()
        idx, buf = cave + 0x800, cave + 0x810          # counter, 64 x u32 ring
        code = bytearray()
        code += b"\x50\x51"                             # push eax; push ecx
        code += b"\x8b\x0d" + struct.pack("<I", idx)    # mov ecx,[idx]
        code += b"\x8b\x44\x24\x08"                     # mov eax,[esp+8] (ret addr)
        code += b"\x89\x04\x8d" + struct.pack("<I", buf)  # mov [buf+ecx*4],eax
        code += b"\x41\x83\xe1\x3f"                     # inc ecx; and ecx,63
        code += b"\x89\x0d" + struct.pack("<I", idx)    # mov [idx],ecx
        code += b"\x59\x58"                             # pop ecx; pop eax
        code += ORIG
        code += b"\xe9" + rel32(cave + len(code) + 5, STOP_FN + 6)
        p.write(cave, bytes(code))
        p.write(idx, b"\0" * 4)

        old = wt.DWORD()
        k32.VirtualProtectEx(p.handle, STOP_FN, 6, 0x40, ctypes.byref(old))
        p.write(STOP_FN, b"\xe9" + rel32(STOP_FN + 5, cave) + b"\x90")
        try:
            g = Game(p)
            for trial in range(3):
                me = g.player()
                ang = trial * 2.1
                tx, ty = me.x + 9 * math.cos(ang), me.y + 9 * math.sin(ang)
                before = p.u32(idx)
                p.write(0xC4D890, struct.pack("<3f", tx, ty, me.z))
                p.write_u32(0xC4D888, 4)
                t0 = time.time()
                while time.time() - t0 < 3 and p.u32(0xC4D888) == 4:
                    time.sleep(0.002)
                me2 = g.player()
                n = (p.u32(idx) - before) % 64
                rets = [p.u32(buf + 4 * ((before + k) % 64)) for k in range(n)]
                print(f"trial {trial}: ran {time.time() - t0:.2f}s, moved "
                      f"{me.dist(me2):.2f}yd, stop called from "
                      f"{[hex(r - 5) for r in rets]}")
                time.sleep(0.4)
        finally:
            p.write(STOP_FN, ORIG)
            k32.VirtualProtectEx(p.handle, STOP_FN, 6, old.value, ctypes.byref(old))
            time.sleep(0.2)
            k32.VirtualFreeEx(p.handle, cave, 0, 0x8000)
            assert p.read(STOP_FN, 6) == ORIG
            print("hook removed, original code restored")


if __name__ == "__main__":
    main()
