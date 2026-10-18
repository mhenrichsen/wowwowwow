#!/bin/bash
# Stop at a calm moment, rewind the guide to step N (1-based, as dbg.py shows it), relaunch.
cd /c/Users/MadsG/Desktop/wowbot || exit 1
WANT=${1:?step number}
python - <<'EOF'
import time
from wowmem import Process
from game import Game
import factions
g = Game(Process()); t0 = time.time()
while time.time() - t0 < 600:
    try:
        me = g.player()
        near = [u for u in g.units() if not u.dead and not u.npc_flags and u.level > 1
                and me.dist(u) < 40 and not factions.is_friendly(me.faction, u.faction)]
        if not me.in_combat and not me.dead and not me.ghost and me.hp_pct >= 85 and not near:
            break
    except Exception:
        pass
    time.sleep(1.0)
open('STOP', 'w').close()
print("stop requested")
EOF
until grep -a -q "stopped\." bot.log 2>/dev/null; do sleep 3; done
sleep 2
cp bot.log "logs/bot_$(date +%H%M%S).log" 2>/dev/null
rm -f STOP bot.lock
python - "$WANT" <<'EOF'
import json, sys
name = json.load(open('status.json'))['name']
p = f'data/quest_progress_{name}.json'
d = json.load(open(p))
old = d.get('step')
d['step'] = int(sys.argv[1]) - 1        # as dbg.py / the web page number them
d['done'] = []
json.dump(d, open(p, 'w'))
print(f"guide step {old} -> {d['step']} (guide {d.get('guide')})")
EOF
python -c "import bot, bridge, taxi, combat, questing, navmesh, roads, routes" || { echo "IMPORT FAILED"; exit 1; }
(python -u bot.py $(cat launch_args.txt) > bot.log 2>&1 &)
sleep 2
echo "relaunched $(date +%T)"
