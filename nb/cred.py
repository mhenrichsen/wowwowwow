"""The WoW login password lives in Windows Credential Manager (generic credential "nb-wow-login"), put there
by the user with `python -m nb.cred set`; the login watchdog reads it from there. Nothing else stores it."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import getpass
import sys

TARGET = "nb-wow-login"
adv = ctypes.WinDLL("advapi32", use_last_error=True)


class CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wt.DWORD),
                ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
                ("UserName", wt.LPWSTR)]


def save(user: str, password: str):
    blob = password.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    c = CREDENTIAL(Type=1, TargetName=TARGET, CredentialBlobSize=len(blob),
                   CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), Persist=2, UserName=user)
    if not adv.CredWriteW(ctypes.byref(c), 0):
        raise OSError(ctypes.get_last_error())


def load() -> tuple[str, str] | None:
    p = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(TARGET, 1, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        pw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
        return c.UserName, pw
    finally:
        adv.CredFree(p)


if __name__ == "__main__":
    if sys.argv[1:2] == ["set"]:
        user = input("account e-mail [admin@admin.com]: ").strip() or "admin@admin.com"
        save(user, getpass.getpass("WoW password (not shown): "))
        print("saved to Windows Credential Manager as", TARGET)
    else:
        print("stored for", load()[0] if load() else "nobody - run: python -m nb.cred set")
