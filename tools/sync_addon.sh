#!/bin/bash
# The live BotEye addon runs from the game folder; this repo keeps a tracked copy in addon/BotEye.
#   tools/sync_addon.sh pull   game -> repo (after editing in the game folder; then commit)
#   tools/sync_addon.sh push   repo -> game (after a checkout / revert; then /reload the clients)
A="/c/Program Files (x86)/World of Warcraft/_classic_beta_/Interface/AddOns/BotEye"
R="$(dirname "$0")/../addon/BotEye"
case "$1" in
  pull) cp "$A"/BotEye.lua "$A"/*.toc "$R"/ && echo "pulled into $R";;
  push) cp "$R"/BotEye.lua "$R"/*.toc "$A"/ && echo "pushed to $A";;
  *) echo "usage: $0 pull|push"; exit 1;;
esac
