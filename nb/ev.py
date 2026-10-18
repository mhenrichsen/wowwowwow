"""Evaluate a Lua expression in the client via /be eval and print the answer."""
import sys, time
from nb.win import Keys, find_window
from nb.eye import Eye

def ev(expr: str, eye=None, keys=None, wait=1.5) -> str:
    eye = eye or Eye(); keys = keys or Keys(eye.cap.hwnd)
    s0 = eye.read(); n0 = s0.msgn if s0 else -1
    keys.chat("/be eval " + expr)
    t = time.time()
    while time.time() - t < wait:
        s = eye.read()
        if s and s.msgn != n0:
            return s.msg
        time.sleep(0.1)
    return "(no answer)"

if __name__ == "__main__":
    e = Eye(); k = Keys(e.cap.hwnd)
    for expr in sys.argv[1:]:
        print(expr, "=>", ev(expr, e, k))
