#!/bin/bash
set -a
source "$HOME/.env"
set +a

./Insurgency/Binaries/Linux/InsurgencyServer-Linux-Shipping \
  -ModDownloadTravelTo=Farmhouse?Scenario=Scenario_Farmhouse_Checkpoint_Insurgents?MaxPlayers=12 \
  -Port=27000 \
  -QueryPort=27010 \
  -Rcon \
  -RconPassword=$RCON_PASSWORD \
  -RconListenPort=$RCON_PORT \
  -SecurityCode=none \
  -log \
  -hostname="[DE] CrazyStorm / ISMC / Insurgents only" \
  -GSLTToken=$GSLT_TOKEN \
  -GameStats \
  -GameStatsToken=$GAMESTATS_TOKEN \
  -MapCycle -AdminList -motd \
  -mods -ModList=Mods.txt \
  -Mutators=ISMCarmory_Legacy,ISMCJumpShoot