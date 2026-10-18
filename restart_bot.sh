#!/bin/sh
# Stop the bot at a calm moment (out of combat, HP >= 80%, mana >= 50%, nothing hostile within 35 yd), keep its log, start it again.
cd "$(dirname "$0")"
python -c "
import time
from wowmem import Process; from game import Game
import factions
g = Game(Process())
while True:
    me = g.player()
    # the 15 s gap between stop and start is unguarded: two golems 5 yd
    # away at the restart meant an escape at 70% hp before the first cast
    near = [u for u in g.units() if not u.dead and not u.npc_flags and u.level > 1 and me.dist(u) < 35
            and not factions.is_friendly(me.faction, u.faction)]
    if (not me.in_combat and not me.dead and not me.ghost and me.hp_pct >= 80
            and me.mana_pct >= 50 and not near):
        break
    time.sleep(0.5)
open('STOP', 'w').close()
"
until grep -q "stopped\." bot.log; do sleep 1; done
tail -1 bot.log
mkdir -p logs && cp bot.log "logs/bot_$(date +%H%M%S).log"
rm -f STOP
sleep 5
# Arguments come from launch_args.txt. This used to hardcode the human
# Elwynn guide, which is exactly how a dwarf ended up on it once.
start() { (python -u bot.py $(cat launch_args.txt 2>/dev/null || echo "--minutes ${1:-240}") > bot.log 2>&1 &); sleep 10; }
start "$1"
# the old process can still hold bot.lock for a few seconds after "stopped."
for i in 1 2 3; do grep -q "bot.lock is held" bot.log || break; sleep 6; start "$1"; done
tail -3 bot.log
