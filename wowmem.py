"""Read (and, for click-to-move only, write) the memory of a running WoW 1.12.1 client.

WoW.exe 1.12.1 is a 32-bit process with a fixed image base (0x400000, no ASLR),
so static addresses from offsets.py can be used directly. A 64-bit Python can
read a 32-bit process with ReadProcessMemory; no injection, no admin rights.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)

PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
k32.ReadProcessMemory.argtypes = (wt.HANDLE, wt.LPCVOID, wt.LPVOID,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
k32.WriteProcessMemory.argtypes = (wt.HANDLE, wt.LPVOID, wt.LPCVOID,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
k32.CloseHandle.argtypes = (wt.HANDLE,)
u32.FindWindowW.restype = wt.HWND
u32.FindWindowW.argtypes = (wt.LPCWSTR, wt.LPCWSTR)
u32.GetWindowThreadProcessId.argtypes = (wt.HWND, ctypes.POINTER(wt.DWORD))

# The 1.12.1 client registers its main window under one of these classes.
WINDOW_CLASSES = ("GxWindowClassD3d", "GxWindowClass", "GxWindowClassOpenGl")


class MemoryError_(Exception):
    pass


def find_wow_window() -> int:
    for cls in WINDOW_CLASSES:
        hwnd = u32.FindWindowW(cls, None)
        if hwnd:
            return hwnd
    hwnd = u32.FindWindowW(None, "World of Warcraft")
    if hwnd:
        return hwnd
    raise MemoryError_("WoW window not found - is the game running?")


adv = ctypes.WinDLL("advapi32", use_last_error=True)
WRITE_DAC = 0x00040000
SE_KERNEL_OBJECT = 6
DACL_SECURITY_INFORMATION = 0x4
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
TOKEN_QUERY = 0x8
TokenUser = 1

adv.OpenProcessToken.argtypes = (wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE))
adv.GetTokenInformation.argtypes = (wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                    wt.DWORD, ctypes.POINTER(wt.DWORD))
adv.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
    wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
adv.GetSecurityDescriptorDacl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wt.BOOL),
                                          ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(wt.BOOL))
adv.SetSecurityInfo.argtypes = (wt.HANDLE, ctypes.c_int, wt.DWORD, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
k32.GetCurrentProcess.restype = wt.HANDLE


def current_user_sid() -> str:
    tok = wt.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(tok)):
        raise MemoryError_(f"OpenProcessToken failed: {ctypes.get_last_error()}")
    try:
        buf = ctypes.create_string_buffer(256)
        n = wt.DWORD()
        if not adv.GetTokenInformation(tok, TokenUser, buf, 256, ctypes.byref(n)):
            raise MemoryError_(f"GetTokenInformation failed: {ctypes.get_last_error()}")
        psid = ctypes.c_void_p.from_buffer(buf).value   # TOKEN_USER.User.Sid
        s = ctypes.c_wchar_p()
        adv.ConvertSidToStringSidW(psid, ctypes.byref(s))
        return s.value
    finally:
        k32.CloseHandle(tok)


def unlock_process(pid: int) -> None:
    """Let the current user read/write the memory of `pid`.

    The RetroWoW build of WoW.exe installs a protected DACL on its own process
    that denies VM_READ / VM_WRITE / VM_OPERATION to Everyone. As the process
    owner we implicitly hold WRITE_DAC, so we replace it with one that grants
    our own SID memory access, while keeping the client's deny on
    CREATE_THREAD, DUP_HANDLE and CREATE_PROCESS (nothing here needs those).
    The change lives only in the running process and is gone when WoW exits.
    """
    sddl = (f"D:P(D;;DCDTLO;;;WD)"                 # deny Everyone: thread/dup/proc
            f"(A;;0x101739;;;{current_user_sid()})")  # us: query, VM r/w/op, sync
    sd = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(sd), None):
        raise MemoryError_(f"bad SDDL: {ctypes.get_last_error()}")
    present, defaulted = wt.BOOL(), wt.BOOL()
    dacl = ctypes.c_void_p()
    adv.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl),
                                  ctypes.byref(defaulted))
    h = k32.OpenProcess(WRITE_DAC, False, pid)
    if not h:
        raise MemoryError_(f"OpenProcess(WRITE_DAC) failed: {ctypes.get_last_error()}")
    try:
        rc = adv.SetSecurityInfo(h, SE_KERNEL_OBJECT,
                                 DACL_SECURITY_INFORMATION |
                                 PROTECTED_DACL_SECURITY_INFORMATION,
                                 None, None, dacl, None)
        if rc != 0:
            raise MemoryError_(f"SetSecurityInfo failed: {rc}")
    finally:
        k32.CloseHandle(h)
        k32.LocalFree.argtypes = (ctypes.c_void_p,)
        k32.LocalFree(sd)


class Process:
    def __init__(self, hwnd: int | None = None, writable: bool = False):
        self.hwnd = hwnd or find_wow_window()
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
        self.pid = pid.value
        access = PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION
        if writable:
            access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
        self.handle = k32.OpenProcess(access, False, self.pid)
        if not self.handle and ctypes.get_last_error() == 5:
            unlock_process(self.pid)
            self.handle = k32.OpenProcess(access, False, self.pid)
        if not self.handle:
            raise MemoryError_(f"OpenProcess({self.pid}) failed: "
                               f"error {ctypes.get_last_error()}")

    def close(self):
        if self.handle:
            k32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ reading
    def read(self, addr: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), buf,
                                     size, ctypes.byref(n)) or n.value != size:
            raise MemoryError_(f"read {size} bytes at {addr:#x} failed")
        return buf.raw

    def try_read(self, addr: int, size: int) -> bytes | None:
        try:
            return self.read(addr, size)
        except MemoryError_:
            return None

    def u32(self, addr: int) -> int:
        return struct.unpack("<I", self.read(addr, 4))[0]

    def i32(self, addr: int) -> int:
        return struct.unpack("<i", self.read(addr, 4))[0]

    def u64(self, addr: int) -> int:
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def f32(self, addr: int) -> float:
        return struct.unpack("<f", self.read(addr, 4))[0]

    def cstr(self, addr: int, maxlen: int = 64) -> str:
        raw = self.read(addr, maxlen)
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")

    # ------------------------------------------------------------ writing
    def write(self, addr: int, data: bytes) -> None:
        n = ctypes.c_size_t()
        if not k32.WriteProcessMemory(self.handle, ctypes.c_void_p(addr), data,
                                      len(data), ctypes.byref(n)) or n.value != len(data):
            raise MemoryError_(f"write {len(data)} bytes at {addr:#x} failed: "
                               f"error {ctypes.get_last_error()}")

    def write_u32(self, addr: int, v: int) -> None:
        self.write(addr, struct.pack("<I", v))

    def write_u64(self, addr: int, v: int) -> None:
        self.write(addr, struct.pack("<Q", v))

    def write_f32(self, addr: int, v: float) -> None:
        self.write(addr, struct.pack("<f", v))
