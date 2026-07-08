#!/bin/bash
set -a
source "$HOME/.env"
set +a

# Never launch with an empty hostname: the engine's command-line parser then
# takes the NEXT argument as the server name - which once put the private token
# on public server trackers. An empty name means .env is broken; refuse to start.
if [ -z "$SERVER_NAME" ]; then
    echo "FATAL: SERVER_NAME is empty - check $HOME/.env quoting" >&2
    exit 1
fi

./Insurgency/Binaries/Linux/InsurgencyServer-Linux-Shipping \
  -ModDownloadTravelTo=Buhriz?Scenario=Scenario_Tideway_Checkpoint_Security?MaxPlayers=$MAX_PLAYERS \
  -hostname="$SERVER_NAME" \
  -Port=$GAME_PORT \
  -QueryPort=$QUERY_PORT \
  -Rcon \
  -RconPassword=$RCON_PASSWORD \
  -RconListenPort=$RCON_PORT \
  -SecurityCode=none \
  -log \
  -GSLTToken=$GSLT_TOKEN \
  -GameStats \
  -GameStatsToken=$GAMESTATS_TOKEN \
  -MapCycle -AdminList -motd \
  -mods -ModList=Mods.txt \
  -Mutators=ISMCarmory_Legacy,ISMCJumpShoot
