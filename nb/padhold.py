"""Keep N virtual pads plugged in (idle) so clients that start now enumerate them: python -m nb.padhold 4"""
import os, sys, time
from nb.vpad import Pad

pads = [Pad() for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 1)]
print(f"{len(pads)} pads up", flush=True)
STOP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "STOP_PADHOLD")
while not os.path.exists(STOP):
    time.sleep(1)
os.remove(STOP)
