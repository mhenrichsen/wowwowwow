#!/bin/sh
# Stop the bot at a calm moment, run an experiment script, start the bot again.
#   sh pause_probe.sh "python boat_probe.py 360" 720
cd "$(dirname "$0")"
python -c "
import time
from wowmem import Process; from game import Game
import factions
g = Game(Process())
while True:
    me = g.player()
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
sleep 6
echo "=== experiment: $1"
$1 > probe.log 2>&1
tail -3 probe.log
sleep 3
start() { (python -u bot.py --minutes "${2:-720}" --guide auto > bot.log 2>&1 &); sleep 10; }
start
for i in 1 2 3; do grep -q "bot.lock is held" bot.log || break; sleep 6; start; done
tail -3 bot.log
