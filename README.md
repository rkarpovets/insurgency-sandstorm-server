# Insurgency: Sandstorm - Co-op Server Config & Manager

Configuration and tooling for a co-op (**Checkpoint**) *Insurgency: Sandstorm* dedicated
server running the **ISMC** mod. The centrepiece is `servermanager.py` - a small,
dependency-light watcher that keeps the server healthy (recovering from the end-of-match
map-vote freeze) and surfaces live player activity to in-game chat, a pinned Telegram
board, and a Prometheus/Grafana metric.

> Provisioned in production by a separate Infrastructure-as-Code repo,
> [server-infrastructure](https://github.com/rkarpovets/server-infrastructure): this repo
> holds the game-specific config and the manager, that one wires them into systemd, the
> firewall, and the monitoring stack.

## Features

### `servermanager.py`
- **Map-vote crash recovery** - when a match ends without a map vote the server logs
  `Unhandled conclusion from mapcycle map vote` / `INVALID map index` and would otherwise
  hang. The manager detects this and recovers via RCON `travelscenario` to a random map
  from the cycle (with a short cooldown to avoid double-triggering).
- **Live player tracking** - who is actually connected, kept accurate across map changes:
  - Joins are instant, from the reliable `LogNet: Join succeeded` log line.
  - Leaves are detected by reconciling the player cache against RCON `listplayers` (the
    authoritative list) - triggered by the session log line and confirmed with a second
    snapshot. The naive `is not part of session` line is **not** trusted on its own: it
    repeats for still-connected players and fires for everyone during map travel, which
    otherwise causes false "disconnected" spam and missed real departures.
  - Player **names** come from the game log's login lines (the `?Name=` field, read for
    both **Steam** and **EOS** players and immune to the `|`-in-name problem of the
    `listplayers` name column). For a Steam player not yet seen in the log (e.g. already
    connected at startup) the **Steam Web API** resolves the name from their SteamID64 as
    a fallback; **EOS** players have no such public API. Anything still unresolved shows
    the raw id. Names are kept in memory only (no database) - a steady roster makes no API
    calls.
- **Three notification channels** - all optional and independent (any can be left off):
  - **In-game chat** - `X connected` / `X disconnected` via RCON `say`.
  - **Telegram** - a single *pinned board* (server name, player count, current map, roster)
    edited in place, never spamming new messages: the board's message id is persisted
    (`BOARD_STATE_FILE`) so restarts reuse it. Plus a one-off message per join/leave. All
    Telegram I/O runs on a dedicated, debounced worker thread, so a slow or broken Telegram
    API can never stall game monitoring.
  - **Prometheus metric** - atomically writes `iss_players_online`, `iss_players_max` and
    `iss_map_info` to a node_exporter textfile (`METRICS_FILE`), which Grafana graphs
    against CPU load.
- **Robust RCON** - a single persistent connection reused for every command,
  reconnecting only when a command actually fails. ISS leaks a server-side thread for
  every RCON connection it accepts, so a fresh connection per command would pile up
  zombie threads over a few hours and destabilise the game server.
- **Survives map changes & restarts** - handles ISS's in-place log rotation (the game
  truncates `Insurgency.log` keeping the same inode) and re-seeds the player cache from
  `listplayers` on every (re)connect.

### Config (`server.sh`, `Insurgency/...`)
- `server.sh` launches the server, sourcing secrets from `.env`, and boots straight to a
  fixed Checkpoint scenario.
- Tunable: tick rate, player slots, map cycle, mutators - see **Configuration**.
- Hand-edited `Game.ini` and `Engine.ini` are version-controlled; the engine-generated
  configs around them are git-ignored.

## Requirements
- A Linux host with an Insurgency: Sandstorm **dedicated server** (SteamCMD app `581330`).
- **Python 3.10+** (uses modern type-union syntax).
- Python packages: `pip install -r requirements.txt` (`rcon`, `python-dotenv`).
- RCON enabled on the server (the launch script enables it).
- *Optional:* a Steam Web API key (`STEAM_API_KEY`) - a name fallback for Steam players
  not yet seen in the log - free from https://steamcommunity.com/dev/apikey
- *Optional:* a Telegram bot + chat for alerts; a node_exporter textfile dir for the metric.

## Setup
1. Place these files in your server directory (e.g. `/home/steam/sandstorm_server`).
2. Create your environment file from the template and fill in your values:
   ```bash
   cp .env.example /home/steam/.env     # or wherever ENV_FILE points
   # edit .env: RCON_IP / RCON_PORT / RCON_PASSWORD, GSLT & GameStats tokens,
   #            LOG_FILE_PATH, MAPCYCLE_FILE, and (optional) STEAM_API_KEY /
   #            TELEGRAM_* / METRICS_FILE
   ```
3. Review the gameplay config - `Insurgency/Saved/Config/LinuxServer/Game.ini` and
   `Engine.ini` ship with sensible defaults; edit to taste. (Secrets stay in `.env`, never
   in these files.)
4. Install Python dependencies: `pip install -r requirements.txt`
5. Make the launcher executable: `chmod +x server.sh`
6. Run both as systemd services (recommended) - see **Running with systemd**.

## Running with systemd
Two independent services: the game server and the manager. The manager is deliberately
decoupled so it survives game-server restarts on its own (reconnect-on-failure RCON +
log-rotation detection).

`/etc/systemd/system/sandstorm-server.service`:
```ini
[Unit]
Description=Sandstorm Dedicated Server
After=network.target

[Service]
Type=simple
User=steam
WorkingDirectory=/home/steam/sandstorm_server
ExecStart=/home/steam/sandstorm_server/server.sh
Restart=on-failure
RestartSec=15s
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/sandstorm-manager.service`:
```ini
[Unit]
Description=Sandstorm Manager
After=sandstorm-server.service

[Service]
Type=simple
User=steam
WorkingDirectory=/home/steam/sandstorm_server
ExecStart=/usr/bin/python3 /home/steam/sandstorm_server/servermanager.py
EnvironmentFile=/home/steam/.env
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

Enable and watch logs:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sandstorm-server.service sandstorm-manager.service

sudo systemctl restart sandstorm-server.service     # restart the game
sudo journalctl -u sandstorm-server.service -f      # live game log
sudo journalctl -u sandstorm-manager.service -f     # manager (also written to servermanager.log)
```

> Don't run `server.sh` by hand while the service is active - you'd start a second
> instance and hit port conflicts. Stop the service first if you need to run it manually.

## Configuration
| What | Where |
| --- | --- |
| Player slots | `server.sh` -> `?MaxPlayers=N` |
| Tick rate | `Insurgency/Saved/Config/LinuxServer/Engine.ini` -> `NetServerMaxTickRate` |
| Map rotation | `Insurgency/Config/Server/MapCycle.txt` |
| Starting map | `server.sh` -> `-ModDownloadTravelTo=<Map>?Scenario=<Scenario>` |
| Gameplay / bots | `Insurgency/Saved/Config/LinuxServer/Game.ini` |
| Mutators / mods | `server.sh` -> `-Mutators=...`, `-ModList=Mods.txt` |
| Admins / MOTD | `Insurgency/Config/Server/Admins.txt`, `Motd.txt` |
| Player name resolution | Log login lines (Steam + EOS); `.env` -> `STEAM_API_KEY` optional fallback for un-seen Steam players; else raw id |
| Telegram board & alerts | `.env` -> `TELEGRAM_GAME_TOKEN`, `TELEGRAM_GAME_CHAT_ID` |
| Grafana player metric | `.env` -> `METRICS_FILE` (node_exporter textfile path) |
| Manager env path | `ENV_FILE` (defaults to `/home/steam/.env`) |

**Tick rate vs. capacity.** `NetServerMaxTickRate` drives the most expensive (replication +
simulation) load. 60 is the standard tick; 120 roughly doubles CPU cost. Lower it before
raising slots. The game simulation is largely single-/few-threaded, so per-core speed -
not core count - is the real capacity limit.

**Map-name gotcha.** An ISS map's level name is **not** its scenario name and can't be
derived from it (e.g. `Scenario_Tideway_Checkpoint_Security` runs on the **Buhriz** level,
`Scenario_Crossing_*` on **Canyon**). Passing a guessed map name to `-ModDownloadTravelTo`
silently falls back to the training **Range**. Use the correct level name, or switch maps at
runtime with RCON `travelscenario <Scenario>` - that resolves the level itself.

## Notes
- `.env` and the engine-generated configs are git-ignored - **never commit secrets** (RCON
  password, GSLT/GameStats tokens, Telegram token).
- `Insurgency.log` grows very fast (multiple GB/day on a busy server) and is only rotated on
  restart - set up a size-capped cleanup (e.g. a small cron/systemd-timer that prunes old
  `Insurgency-backup-*.log` files) so it doesn't fill the disk.
- The manager needs only read access to the server log plus RCON; run it as the same `steam`
  user as the game.
