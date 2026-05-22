import time
import os
import random
import re
import logging
import signal
import sys
import asyncio
from collections import OrderedDict
from rcon.source import rcon

from dotenv import load_dotenv
load_dotenv("/home/steam/.env") # set your path or delete this import and use your values for variables under this comment

LOG_FILE_PATH    = os.environ.get("LOG_FILE_PATH")
MAPCYCLE_FILE    = os.environ.get("MAPCYCLE_FILE")
RCON_IP          = os.environ.get("RCON_IP")
RCON_PORT        = int(os.environ.get("RCON_PORT"))
RCON_PASSWORD    = os.environ.get("RCON_PASSWORD")

TRAVEL_COOLDOWN  = 30        # seconds to ignore further crash triggers after a travelscenario
MAX_CACHE_SIZE   = 300       # max players kept in memory
LOG_FILE         = "servermanager.log"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# State
player_cache: OrderedDict[str, str] = OrderedDict()   # steam_id to player_name
_last_travel_time: float = 0.0                        # epoch seconds


# Graceful shutdown
def _handle_signal(signum, _frame):
    log.info("Received signal %s — shutting down cleanly.", signal.Signals(signum).name)
    sys.exit(0)

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# Helpers
def sanitize_name(name: str) -> str:
    """Strip characters that could be used for RCON command injection."""
    return re.sub(r'[^\w\s\-]', '', name)[:32].strip() or "UnknownPlayer"


def load_maps() -> list[str]:
    maps = []
    try:
        with open(MAPCYCLE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                m = re.search(r'Scenario="([^"]+)"', line)
                if m:
                    maps.append(m.group(1))
    except Exception as e:
        log.error("Failed to load MapCycle.txt: %s", e)

    if not maps:
        fallback = "Scenario_Crossing_Checkpoint_Insurgents"
        log.warning("No maps loaded — using fallback: %s", fallback)
        return [fallback]

    log.info("Loaded %d map(s) from MapCycle.txt.", len(maps))
    return maps


def send_rcon(command: str) -> str | None:
    try:
        result = asyncio.run(rcon(command, host=RCON_IP, port=RCON_PORT, passwd=RCON_PASSWORD))
        log.debug("RCON ← %r  →  %r", command, result)
        log.info("[RCON RESULT] %r", result)
        return result
    except Exception as e:
        log.error("RCON command %r failed: %s", command, e)
        return None

def cache_add(steam_id: str, name: str) -> None:
    """Add a player to the cache, evicting oldest entries when over the limit."""
    if steam_id in player_cache:
        del player_cache[steam_id]          # refresh position
    player_cache[steam_id] = name
    while len(player_cache) > MAX_CACHE_SIZE:
        evicted_id, evicted_name = player_cache.popitem(last=False)
        log.debug("Cache full — evicted %s (%s).", evicted_name, evicted_id)


def cache_pop(steam_id: str) -> str:
    return player_cache.pop(steam_id, f"SteamID:{steam_id}")


# Line handlers
def handle_crash(line: str, maps_pool: list[str]) -> bool:
    """Detect map-vote crash and recover via travelscenario."""
    global _last_travel_time

    if "Unhandled conclusion from mapcycle map vote" not in line and "INVALID map index" not in line:
        return False

    now = time.monotonic()
    if now - _last_travel_time < TRAVEL_COOLDOWN:
        log.debug("Crash trigger suppressed (cooldown active).")
        return True

    chosen = random.choice(maps_pool)
    log.warning("Crash detected — travelling to: %s", chosen)
    _last_travel_time = now
    send_rcon(f"travelscenario {chosen}")
    return True


def handle_login(line: str) -> bool:
    if "LogNet: Login request:" not in line:
        return False

    name_m = re.search(r'\?Name=(.+?)\s+userId:', line)
    id_m   = re.search(r'userId:\s*SteamNWI:(\d+)', line)
    if name_m and id_m:
        raw_name = re.sub(r'\?{2,}\w+=\S+', '', name_m.group(1)).strip()
        cache_add(id_m.group(1), sanitize_name(raw_name))
    return True


def handle_join(line: str) -> bool:
    if "LogNet: Join succeeded:" not in line:
        return False

    m = re.search(r'LogNet: Join succeeded:\s*(.+)', line)
    if m:
        name = sanitize_name(m.group(1))
        log.info("[JOIN] %s", name)
        send_rcon(f"say {name} connected")
    return True


def handle_disconnect(line: str) -> bool:
    if "LogOnlineSession: Warning: STEAM (NWI): Player" not in line or "is not part of session" not in line:
        return False

    m = re.search(r'Player\s+(\d+)\s+is not part of session', line)
    if m:
        steam_id = m.group(1)

        if steam_id not in player_cache:
            return True

        name = cache_pop(steam_id)
        log.info("[LEAVE] %s (%s)", name, steam_id)
        send_rcon(f"say {name} disconnected")
    return True


# Main loop
HANDLERS = [handle_crash, handle_login, handle_join, handle_disconnect]

def process_line(line: str, maps_pool: list[str]) -> None:
    for handler in HANDLERS:
        if handler is handle_crash:
            if handler(line, maps_pool):
                return
        else:
            if handler(line):
                return


def watch_logs() -> None:
    maps_pool = load_maps()
    log.info("Monitoring: %s", LOG_FILE_PATH)

    while True:
        try:
            with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
                log.info("Pre-loading player cache from existing log...")
                for line in f:
                    handle_login(line.rstrip())
                log.info("Cache pre-loaded: %d player(s).", len(player_cache))

                current_inode = os.fstat(f.fileno()).st_ino

                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.5)
                        try:
                            if os.stat(LOG_FILE_PATH).st_ino != current_inode:
                                log.info("Log rotation detected — reopening file.")
                                break
                        except FileNotFoundError:
                            log.warning("Log file disappeared — waiting...")
                            time.sleep(5)
                            break
                        continue
                    process_line(line.rstrip(), maps_pool)

        except FileNotFoundError:
            log.warning("Log file not found: %s — retrying in 10 s...", LOG_FILE_PATH)
            time.sleep(10)
        except Exception as e:
            log.exception("Unexpected error in watch loop: %s — restarting in 5 s.", e)
            time.sleep(5)


if __name__ == "__main__":
    watch_logs()