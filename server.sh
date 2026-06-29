#!/bin/bash
set -a
source "$HOME/.env"
set +a

./Insurgency/Binaries/Linux/InsurgencyServer-Linux-Shipping \
  -ModDownloadTravelTo=Buhriz?Scenario=Scenario_Tideway_Checkpoint_Security?MaxPlayers=$MAX_PLAYERS \
  -Port=$GAME_PORT \
  -QueryPort=$QUERY_PORT \
  -Rcon \
  -RconPassword=$RCON_PASSWORD \
  -RconListenPort=$RCON_PORT \
  -SecurityCode=none \
  -log \
  -hostname="$SERVER_NAME" \
  -GSLTToken=$GSLT_TOKEN \
  -GameStats \
  -GameStatsToken=$GAMESTATS_TOKEN \
  -MapCycle -AdminList -motd \
  -mods -ModList=Mods.txt \
  -Mutators=ISMCarmory_Legacy,ISMCJumpShoot
