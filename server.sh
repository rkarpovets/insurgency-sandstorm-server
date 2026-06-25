#!/bin/bash
set -a
source "$HOME/.env"
set +a

# Pick a random scenario from MapCycle.txt for this launch, so each (re)start
# begins on a different map. The map name is the 2nd token of the scenario name
# (Scenario_<Map>_<Mode>_<Side>).
MAPCYCLE="${MAPCYCLE_FILE:-./Insurgency/Config/Server/MapCycle.txt}"
SCENARIO=$(sed -nE 's/.*Scenario="([^"]+)".*/\1/p' "$MAPCYCLE" | shuf -n1)
if [ -z "$SCENARIO" ]; then
  SCENARIO="Scenario_Tideway_Checkpoint_Security"   # fallback if MapCycle is unreadable
fi
MAP=$(printf '%s' "$SCENARIO" | cut -d_ -f2)
echo "server.sh: starting on random scenario $SCENARIO (map $MAP)"

./Insurgency/Binaries/Linux/InsurgencyServer-Linux-Shipping \
  -ModDownloadTravelTo=Buhriz?Scenario=Scenario_Tideway_Checkpoint_Security?MaxPlayers=12 \
  -Port=27000 \
  -QueryPort=27010 \
  -Rcon \
  -RconPassword=$RCON_PASSWORD \
  -RconListenPort=$RCON_PORT \
  -SecurityCode=none \
  -log \
  -hostname="[DE] CrazyStorm / ISMC / Balanced AI" \
  -GSLTToken=$GSLT_TOKEN \
  -GameStats \
  -GameStatsToken=$GAMESTATS_TOKEN \
  -MapCycle -AdminList -motd \
  -mods -ModList=Mods.txt \
  -Mutators=ISMCarmory_Legacy,ISMCJumpShoot
