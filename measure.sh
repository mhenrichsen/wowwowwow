#!/bin/bash
# Aggregate this session's logs. Reading bot.log alone is wrong: every
# relaunch truncates it, so four measurements tonight reported "0 fights".
# Select archived logs by modification time, not by filename pattern - the
# archive holds 178 files and a name glob pulled in a Westfall run from a
# previous day.
cd /c/Users/MadsG/Desktop/wowbot || exit 1
SINCE="${1:-2026-09-21 05:00}"
TMP=$(mktemp)
# shellcheck disable=SC2046
cat $(find logs -name "*.log" -newermt "$SINCE" -printf "%T@ %p\n" | sort -n | cut -d' ' -f2) bot.log > "$TMP" 2>/dev/null

f=$(grep -ac 'fight: ' "$TMP")
echo "window since $SINCE   (first line: $(head -n 1 "$TMP" | cut -c1-8))"
echo "  completed fights   $f"
echo "  deaths             $(grep -ac 'death #' "$TMP")"
echo "  adds joined        $(grep -ac ' joined at ' "$TMP")"
echo "  escapes            $(grep -ac ESCAPING "$TMP")"
echo "  break off early    $(grep -ac 'breaking off early' "$TMP")"
echo "  ran without a root $(grep -ac 'running without one' "$TMP")"
echo "  sheep refreshed    $(grep -ac 'refreshing' "$TMP")"
echo "  failed casts       $(grep -ac 'cast of .* failed' "$TMP")"
echo "  unknown-cost casts $(grep -ac 'learned cost ?' "$TMP")"
echo "  tick errors        $(grep -ac 'tick error' "$TMP")"
echo "  tracebacks         $(grep -ac Traceback "$TMP")"
echo "  swimming events    $(grep -ac swimming "$TMP")"
echo "  stuck              $(grep -ac 'stuck at' "$TMP")"
grep -a "fight: " "$TMP" | sed -E 's/.*hp ([0-9]+)->([0-9]+)%.*/\1 \2/' \
  | awk '{s+=$1-$2;c++} END {if(c) printf "  hp lost/fight      %.0f%%\n", s/c}'
grep -a "fight: " "$TMP" | sed -E 's/.*, ([0-9]+)s in melee.*/\1/' \
  | awk '{s+=$1;c++} END {if(c) printf "  melee/fight        %.1fs\n", s/c}'
grep -a "ghost: running toward corpse" "$TMP" | sed -E 's/.*corpse, ([0-9]+) yd.*/\1/' \
  | awk '{s+=$1;c++} END {if(c) printf "  corpse runs        %d, mean %.0f yd\n", c, s/c}'
python -c "import json;d=json.load(open('status.json'));print('  now               level',d['level'],'xp',d['xp'],'/',d['next_xp'],'state',d['state'])"
rm -f "$TMP"
