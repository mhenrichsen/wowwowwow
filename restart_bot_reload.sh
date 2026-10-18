#!/bin/sh
# Like restart_bot.sh, but also reinstalls the add-on and reloads the UI
# while the bot is stopped (needed whenever bridge.py's Lua changes).
cd "$(dirname "$0")"
touch STOP_SAFE
until grep -q "stopped\." bot.log; do sleep 1; done
tail -1 bot.log
mkdir -p logs && cp bot.log "logs/bot_$(date +%H%M%S).log"
rm -f STOP STOP_SAFE
python bridge.py install
python -c "
import time
from wowmem import Process; from game import Game
from keys import GuardedKeyboard; from bridge import Bridge, RELOAD_UI
p = Process(); g = Game(p)
Bridge(GuardedKeyboard(p.hwnd, g.in_world)).send(RELOAD_UI, wait=6.0)
print('ui reloaded')
"
sleep 4
start() { (python -u bot.py --minutes "${1:-240}" --guide auto > bot.log 2>&1 &); sleep 10; }
start "$1"
for i in 1 2 3; do grep -q "bot.lock is held" bot.log || break; sleep 6; start "$1"; done
tail -4 bot.log
