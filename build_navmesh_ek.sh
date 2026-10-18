#!/bin/bash
# Build every missing Detour tile of a map (default: 0, Eastern Kingdoms) in data_r3.
# Usage: build_navmesh_ek.sh [MAP] [PARALLEL]   -> log in wowbot/navbuild_<MAP>.log
MAP=${1:-0}; PAR=${2:-4}
M=$(printf "%03d" "$MAP")
cd /c/Users/MadsG/Desktop/wow-forever-server/server/data_r3
LOG=/c/Users/MadsG/Desktop/wowbot/navbuild_${M}.log
TILES=$(ls maps | grep "^${M}" | sed "s/^${M}\(..\)\(..\)\.map$/\2,\1/" | while read t; do c=${t%,*}; r=${t#*,}; [ -f mmaps/${M}${r}${c}.mmtile ] || echo "$t"; done | tr '\n' ' ')
echo "to build: $(echo $TILES | wc -w)" > "$LOG"
run() { ../MoveMapGenerator.exe "$MAP" --tile "$1" --silent >/dev/null 2>&1; echo "built $1"; }
export -f run; export MAP
echo $TILES | tr ' ' '\n' | xargs -P "$PAR" -I{} bash -c 'run {}' >> "$LOG" 2>&1
echo "ALL_DONE $(ls mmaps | grep -c "^${M}.*mmtile") tiles" >> "$LOG"
