#!/bin/bash
# Reinstall the add-on and reload the client UI. The bot must be stopped:
# the bridge talks over the F-keys and two senders would interleave.
cd /c/Users/MadsG/Desktop/wowbot || exit 1
python -c "import bridge; print('installed:', bridge.install())" || exit 1
python - <<'PY'
import time, bridge, keys
from wowmem import Process
from game import Game
proc = Process()
g = Game(proc)
kb = keys.GuardedKeyboard(proc.hwnd, g.in_world)
b = bridge.Bridge(kb)
b.send(bridge.RELOAD_UI, wait=6.0)
print("reloaded; in world:", g.in_world())
PY
