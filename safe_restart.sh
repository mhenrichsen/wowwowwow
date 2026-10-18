#!/bin/bash
# Stop the bot and start it again only while the character is safe.
#
# A raw "touch STOP; relaunch" leaves the mage standing still for the ~60 s
# the navmesh takes to load. It was killed by mobs in that gap. Wait for a
# calm moment before stopping, and refuse to stop at all while in combat.
cd /c/Users/MadsG/Desktop/wowbot || exit 1

python - <<'EOF'
import time
from wowmem import Process
from game import Game
import factions

g = Game(Process())
t0 = time.time()
while time.time() - t0 < 600:
    try:
        me = g.player()
        near = [u for u in g.units()
                if not u.dead and not u.npc_flags and u.level > 1 and me.dist(u) < 40
                and not factions.is_friendly(me.faction, u.faction)]
        if (not me.in_combat and not me.dead and not me.ghost
                and me.hp_pct >= 85 and not near):
            break
    except Exception:
        pass
    time.sleep(1.0)
else:
    print("no calm moment in 10 min - stopping anyway")
open('STOP', 'w').close()
print("stop requested")
EOF

until grep -a -q "stopped\." bot.log 2>/dev/null; do sleep 3; done
sleep 2
# Keep the run's log: launching with "> bot.log" truncates it, so every
# restart tonight destroyed the evidence and cut short whatever measurement
# was reading it. Archive before relaunching.
cp bot.log "logs/bot_$(date +%H%M%S).log" 2>/dev/null
rm -f STOP bot.lock
python -c "import bot, combat, questing, navmesh, roads" || { echo "IMPORT FAILED - not starting"; exit 1; }
# The launch arguments live in launch_args.txt so a restart cannot put a
# different character on the wrong guide: this script used to hardcode
# the human Elwynn guide, which a dwarf must never be given.
ARGS=$(cat launch_args.txt 2>/dev/null || echo "--minutes 720")
echo "launching: bot.py $ARGS"
(python -u bot.py $ARGS > bot.log 2>&1 &)
sleep 2
echo "relaunched $(date +%T)"
