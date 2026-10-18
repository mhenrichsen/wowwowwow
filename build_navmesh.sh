#!/bin/bash
# Build Detour navmesh tiles for Northshire + surrounding Elwynn (map 0).
# Usage: build_navmesh.sh "X,Y X,Y ..."  (generator order: column,row)
cd /c/Users/MadsG/Desktop/wow-forever-server/server/data
TILES=${1:-"31,48 33,48 30,48 31,49 32,49 33,49 30,49 34,48 34,49"}
run() { [ -f "mmaps/000$(echo $1 | awk -F, '{printf "%02d%02d",$2,$1}').mmtile" ] && return; ../MoveMapGenerator.exe 0 --tile "$1" --silent >/dev/null 2>&1; echo "built $1"; }
export -f run
echo $TILES | tr ' ' '\n' | xargs -P 3 -I{} bash -c 'run {}'
echo ALL_DONE
