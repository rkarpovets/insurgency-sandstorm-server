import time
import os
import random
import re
import html
import json
import queue
import logging
import signal
import sys
import threading
import urllib.parse
import urllib.request
from collections import OrderedDict
from rcon.source import Client as RconClient

from dotenv import load_dotenv
# Load env vars for manual runs. Under systemd they're already provided via
# EnvironmentFile=, so override the path with ENV_FILE if yours differs.
load_dotenv(os.environ.get("ENV_FILE", "/home/steam/.env"))

LOG_FILE_PATH    = os.environ.get("LOG_FILE_PATH")
MAPCYCLE_FILE    = os.environ.get("MAPCYCLE_FILE")
RCON_IP          = os.environ.get("RCON_IP")
RCON_PORT        = int(os.environ.get("RCON_PORT"))
RCON_PASSWORD    = os.environ.get("RCON_PASSWORD")

# Steam Web API key - OPTIONAL fallback. Player names come from the game log's
# login lines (the "|"-immune "?Name=" field, for Steam AND EOS). This key only
# names a *Steam* player we have no log line for yet (e.g. one already connected
# when the manager (re)started); EOS players have no such public API. Empty ->
# such players show their raw id. Get one at https://steamcommunity.com/dev/apikey
STEAM_API_KEY    = os.environ.get("STEAM_API_KEY", "").strip()

# --- Game alerts (Telegram board + Grafana metric) - all optional ---
# Leave the Telegram vars empty to disable alerts entirely (e.g. local tests).
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_GAME_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_GAME_CHAT_ID", "").strip()
SERVER_NAME      = os.environ.get("SERVER_NAME", "Insurgency: Sandstorm")
MAX_PLAYERS      = int(os.environ.get("MAX_PLAYERS", "16"))
# Where to persist the pinned board's message_id so restarts edit it in place
# instead of spamming a new board each time.
BOARD_STATE_FILE = os.environ.get("BOARD_STATE_FILE", "board_message_id.txt")
# node_exporter textfile-collector target. Empty -> don't write the metric.
METRICS_FILE     = os.environ.get("METRICS_FILE", "").strip()

TELEGRAM_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

TRAVEL_COOLDOWN  = 30        # seconds to ignore further crash triggers after a travelscenario
MAX_CACHE_SIZE   = 5000      # max players kept in memory (big enough that long-connected
                             # players are never evicted before they disconnect)
LOG_FILE         = "servermanager.log"
RCON_TIMEOUT     = 5.0       # socket timeout in seconds
RCON_RETRIES     = 2         # connect+send attempts per command
PRELOAD_MAX_BYTES = 50 * 1024 * 1024   # only scan the tail of the log on (re)open
PRELOAD_MAX_LINES = 200_000            # secondary cap on tail lines scanned
PLAYER_POLL_INTERVAL = 20   # seconds between periodic safety-net reconciliations
TRAVEL_RECONCILE_PAUSE = 30 # seconds to pause leave reconciliation after a map change
BOARD_DEBOUNCE   = 2.0      # seconds the Telegram worker coalesces rapid roster
                            # changes into a single board edit (avoids API spam
                            # when a full server reconnects after a map change)
TG_HTTP_TIMEOUT  = 10.0     # seconds for any Telegram API call

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
player_cache: OrderedDict[str, str] = OrderedDict()
# Names learned from the log's "Login request" lines, keyed by player id (Steam
# digits or full EOS token). The primary, "|"-immune name source for BOTH Steam
# and EOS; in-memory only (the game log is the history, no database needed).
login_names: OrderedDict[str, str] = OrderedDict()
_reconcile_pending: bool = False    # a join/session-warning asked for an immediate reconcile
_last_travel_time: float = 0.0        # last crash-recovery travelscenario we issued
_last_travel_seen: float = 0.0        # last ProcessServerTravel observed in the log

# Game-alert state
_current_map: str = "-"               # human map name (Scenario token, not the level)
_current_side: str = ""               # team/side of the scenario (Security / Insurgents)
_board_msg_id: int | None = None      # pinned Telegram board message to edit in place
_board_dirty = threading.Event()      # set when the board needs a refresh
_event_queue: "queue.Queue[str]" = queue.Queue()  # join/leave messages to send


# Graceful shutdown
def _handle_signal(signum, _frame):
    log.info("Received signal %s - shutting down cleanly.", signal.Signals(signum).name)
    _rcon_close()
    sys.exit(0)

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# RCON - a SINGLE persistent connection, reused for every command.
#
# Insurgency Sandstorm leaks a server-side thread for every RCON connection it
# accepts (the FRconConnection thread is never freed on disconnect). Opening a
# fresh connection per command therefore piles up hundreds of zombie threads
# over a few hours and destabilises the game server. So we keep ONE connection
# and reuse it, reconnecting only when a command actually fails (e.g. after a
# map travel resets the listener). The earlier "chat stops after hours" symptom
# was really the in-place log-truncation bug (fixed separately), not RCON.
_rcon_client: RconClient | None = None


def _rcon_close() -> None:
    global _rcon_client
    if _rcon_client is not None:
        try:
            _rcon_client.close()
        except Exception:
            pass
        _rcon_client = None


def _rcon_connect() -> bool:
    global _rcon_client
    try:
        client = RconClient(RCON_IP, RCON_PORT, passwd=RCON_PASSWORD, timeout=RCON_TIMEOUT)
        client.connect(login=True)
        _rcon_client = client
        return True
    except Exception as e:
        log.warning("RCON connect failed: %s", e)
        _rcon_client = None
        return False


def send_rcon(command: str) -> str | None:
    global _rcon_client
    for attempt in range(1, RCON_RETRIES + 1):
        if _rcon_client is None and not _rcon_connect():
            time.sleep(0.5)
            continue
        try:
            result = _rcon_client.run(command)
            log.debug("RCON <- %r  ->  %r", command, result)
            return result
        except Exception as e:
            log.warning("RCON command %r failed (attempt %d/%d): %s",
                        command, attempt, RCON_RETRIES, e)
            _rcon_close()            # drop the dead socket; reconnect next attempt
            time.sleep(0.5)

    log.error("RCON command %r gave up after %d attempts.", command, RCON_RETRIES)
    return None


# Helpers
def sanitize_name(name: str) -> str:
    """Strip control chars that could inject a second RCON command."""
    return re.sub(r'[\x00-\x1f\x7f]', '', name).strip()[:32] or "UnknownPlayer"


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
        log.warning("No maps loaded - using fallback: %s", fallback)
        return [fallback]

    log.info("Loaded %d map(s) from MapCycle.txt.", len(maps))
    return maps


def cache_add(steam_id: str, name: str) -> None:
    """Add a player to the cache, evicting oldest entries when over the limit."""
    if steam_id in player_cache:
        del player_cache[steam_id]
    player_cache[steam_id] = name
    while len(player_cache) > MAX_CACHE_SIZE:
        evicted_id, evicted_name = player_cache.popitem(last=False)
        log.debug("Cache full - evicted %s (%s).", evicted_name, evicted_id)


def cache_pop(pid: str) -> str:
    return player_cache.pop(pid, pid)          # fall back to the raw id


# A player's NetID is "SteamNWI:<digits>" (Steam) or "EOS:<hex>|<hex>" (Epic /
# console via Epic Online Services); bots show "None:INVALID". We extract only
# the id TOKEN - unambiguous - never the "|"-delimited name column. Names are
# resolved separately (log login line, then Steam API; see _resolve_names).
_NETID = r'SteamNWI:\d+|EOS:[0-9a-fA-F]+\|[0-9a-fA-F]+'
_NETID_RE = re.compile(_NETID)
_LOGIN_ID_RE = re.compile(rf'userId:\s*({_NETID})')


def _bare_id(token: str) -> str:
    """Cache key: SteamID64 digits for a Steam token, the full 'EOS:...' token
    for EOS (Steam ids stay Steam-API-ready; EOS ids stay opaque)."""
    return token[len("SteamNWI:"):] if token.startswith("SteamNWI:") else token


def _login_name_add(pid: str, name: str) -> None:
    """Record a name learned from a log login line (LRU-bounded, in-memory)."""
    if pid in login_names:
        del login_names[pid]
    login_names[pid] = name
    while len(login_names) > MAX_CACHE_SIZE:
        login_names.popitem(last=False)


def parse_listplayers(text: str) -> list[str]:
    """Return the ids of connected humans (Steam digits / full EOS token). We scan
    the WHOLE reply for id tokens - listplayers is not reliably one row per line,
    so any per-line logic would drop players. IDs only; names are resolved
    separately (log login line, then Steam API)."""
    return [_bare_id(t) for t in _NETID_RE.findall(text or "")]


def _steam_resolve(steam_ids: list[str]) -> dict[str, str]:
    """Resolve SteamID64 to the current Steam persona name via the Steam Web API.
    Batches up to 100 ids per call. Missing/failed/empty ids are simply absent;
    EOS ids are never sent; never raises. With no STEAM_API_KEY set, returns {}."""
    names: dict[str, str] = {}
    steam_ids = [s for s in steam_ids if s.isdigit()]   # never send an EOS id
    if not STEAM_API_KEY or not steam_ids:
        return names
    for i in range(0, len(steam_ids), 100):        # API caps at 100 ids per call
        batch = ",".join(steam_ids[i:i + 100])
        url = ("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
               f"?key={STEAM_API_KEY}&steamids={batch}")
        try:
            with urllib.request.urlopen(url, timeout=TG_HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for p in data.get("response", {}).get("players", []):
                persona = p.get("personaname", "")
                if persona:
                    names[p["steamid"]] = sanitize_name(persona)
        except Exception as e:
            log.warning("Steam API resolve failed: %s", e)
    return names


def _resolve_names(ids: list[str]) -> dict[str, str]:
    """Resolve player ids to display names, in priority order:
    log login name (login_names) -> Steam Web API (Steam ids only) -> raw id.
    Only ids missing from login_names hit the Steam API."""
    steam = _steam_resolve([pid for pid in ids if pid not in login_names])
    return {pid: login_names.get(pid) or steam.get(pid) or pid for pid in ids}


def seed_cache_from_rcon() -> bool:
    """Seed the roster from RCON `listplayers` (accurate: only live players),
    unlike scanning the multi-GB log. Names come from the log login lines /
    Steam API (see _resolve_names). Returns False if RCON is unavailable.
    """
    result = send_rcon("listplayers")
    if result is None:
        return False
    ids = parse_listplayers(result)
    names = _resolve_names(ids)
    for pid in ids:
        cache_add(pid, names[pid])
    log.info("Seeded cache from RCON listplayers: %d player(s).", len(ids))
    return True


# ============================================================================
#  Game alerts: Telegram live board + Grafana metric
#
#  Design: the join/leave events already computed by handle_join /
#  reconcile_players are the single source of truth. We only add two extra
#  sinks here - never a second polling path:
#    * one NEW message per join/leave (with live count + map), AND
#    * a SINGLE pinned "board" (server name, count, map, roster) edited in
#      place - refreshed on every roster/map change;
#    * a node_exporter textfile metric (iss_players_online) for Grafana.
#  All Telegram I/O happens on a dedicated worker thread so a slow/broken API
#  call can never stall the RCON loop or log tailing.
# ============================================================================

def player_count() -> int:
    return len(player_cache)


def map_label() -> str:
    if _current_side:
        return f"{_current_map} ({_current_side})"
    return _current_map


def _tg_call(method: str, params: dict) -> dict | None:
    """POST to the Telegram Bot API. Returns the parsed response or None on
    failure. Never raises - game monitoring must survive Telegram being down."""
    if not TELEGRAM_ENABLED:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                    timeout=TG_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("Telegram %s failed: %s", method, e)
        return None


def _load_board_id() -> None:
    global _board_msg_id
    try:
        with open(BOARD_STATE_FILE, "r", encoding="utf-8") as f:
            _board_msg_id = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        _board_msg_id = None


def _save_board_id(msg_id: int) -> None:
    try:
        with open(BOARD_STATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(msg_id))
    except Exception as e:
        log.warning("Could not persist board message id: %s", e)


def build_board_text() -> str:
    """Render the pinned status board (Telegram HTML parse mode): server name,
    live count, current map, and the list of connected players."""
    count = player_count()
    roster = "\n".join(f"- {html.escape(n)}" for n in player_cache.values()) or "<i>empty</i>"
    return (
        f"🎮 <b>{html.escape(SERVER_NAME)}</b>\n"
        f"\n"
        f"👥 Players: <b>{count}/{MAX_PLAYERS}</b>\n"
        f"🗺️ Map: <b>{html.escape(map_label())}</b>\n"
        f"\n"
        f"{roster}\n"
        f"\n"
        f"<i>updated {time.strftime('%-d %b %Y %H:%M', time.gmtime())} UTC</i>"
    )


def _render_and_edit_board() -> None:
    """Edit the pinned board in place; (re)create + pin it if needed."""
    global _board_msg_id
    text = build_board_text()
    if _board_msg_id is not None:
        r = _tg_call("editMessageText", {
            "chat_id": TELEGRAM_CHAT_ID, "message_id": _board_msg_id,
            "text": text, "parse_mode": "HTML",
        })
        # "message is not modified" is a benign no-op; anything else -> recreate.
        if r and (r.get("ok") or "not modified" in str(r.get("description", ""))):
            return
        log.info("Board edit failed (%s) - creating a new board.",
                 r.get("description") if r else "no response")
        _board_msg_id = None

    r = _tg_call("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_notification": "true",
    })
    if r and r.get("ok"):
        _board_msg_id = r["result"]["message_id"]
        _save_board_id(_board_msg_id)
        _tg_call("pinChatMessage", {
            "chat_id": TELEGRAM_CHAT_ID, "message_id": _board_msg_id,
            "disable_notification": "true",
        })
    else:
        log.warning("Board sendMessage failed: %s",
                    r.get("description") if r else "no response")


def notify_event(text: str) -> None:
    """Queue a join/leave message (sent as its own message). Non-blocking."""
    if TELEGRAM_ENABLED:
        _event_queue.put(text)


def telegram_worker() -> None:
    """Single background thread: sends queued join/leave messages and refreshes
    the board at most every BOARD_DEBOUNCE seconds (coalesces bursts)."""
    while True:
        try:
            while True:
                msg = _event_queue.get_nowait()
                _tg_call("sendMessage", {
                    "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML",
                })
        except queue.Empty:
            pass
        if _board_dirty.is_set():
            _board_dirty.clear()
            try:
                _render_and_edit_board()
            except Exception as e:
                log.warning("Board update failed: %s", e)
        time.sleep(BOARD_DEBOUNCE)


def write_metrics() -> None:
    """Atomically write the node_exporter textfile metric for Grafana."""
    if not METRICS_FILE:
        return
    body = (
        "# HELP iss_players_online Players currently connected\n"
        "# TYPE iss_players_online gauge\n"
        f"iss_players_online {player_count()}\n"
        "# HELP iss_players_max Configured player slots\n"
        "# TYPE iss_players_max gauge\n"
        f"iss_players_max {MAX_PLAYERS}\n"
        "# HELP iss_map_info Current map and scenario side (value is always 1)\n"
        "# TYPE iss_map_info gauge\n"
        f'iss_map_info{{map="{_current_map}",side="{_current_side or "-"}"}} 1\n'
    )
    tmp = f"{METRICS_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, METRICS_FILE)      # atomic - node_exporter never reads a partial file
    except Exception as e:
        log.warning("Could not write metrics file %s: %s", METRICS_FILE, e)


def player_event(name: str, action: str) -> None:
    """Announce a join/leave as its OWN message, then refresh the pinned board
    and the Grafana metric. `action` is 'joined' or 'left'. The count already
    reflects this event (join: added on Login; leave: popped before this call)."""
    count = player_count()
    notify_event(
        f"<b>{html.escape(name)}</b> {action} the server\n"
        f"{count}/{MAX_PLAYERS} - {html.escape(map_label())}"
    )
    _board_dirty.set()
    write_metrics()


def roster_refresh() -> None:
    """Refresh the board + metric WITHOUT announcing - used at startup and for
    safety-net reconciliation of players we picked up implicitly."""
    _board_dirty.set()
    write_metrics()


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
    log.warning("Crash detected - travelling to: %s", chosen)
    _last_travel_time = now
    send_rcon(f"travelscenario {chosen}")
    return True


def handle_login(line: str) -> bool:
    """Learn a connecting player's name from the log into login_names - the
    primary, "|"-immune name source for BOTH Steam and EOS. Writes only the NAME
    map, never the roster, so it is safe to run live: joins/leaves are still
    announced authoritatively by reconcile_players (via listplayers)."""
    if "LogNet: Login request:" not in line:
        return False
    name_m = re.search(r'\?Name=(.+?)\s+userId:', line)
    id_m   = _LOGIN_ID_RE.search(line)
    if name_m and id_m:
        raw_name = re.sub(r'\?{2,}\w+=\S+', '', name_m.group(1)).strip()
        _login_name_add(_bare_id(id_m.group(1)), sanitize_name(raw_name))
    return True


# ProcessServerTravel: <Level>?scenario=Scenario_<Map>_<Mode>_<Side>?...
# NOTE 1: the game writes the key BOTH ways - "Scenario=" (travelscenario / on
#         boot) and "scenario=" (normal map change). Match case-insensitively
#         on the key; the value always starts with capital "Scenario_".
# NOTE 2: the human map name is the Scenario token (e.g. "Tideway"), NOT the
#         level (e.g. "Buhriz") - they differ for several maps.
# We show map + side (Security / Insurgents); the mode is always Checkpoint.
_TRAVEL_RE = re.compile(
    r'[Ss]cenario=Scenario_([A-Za-z0-9]+)_([A-Za-z0-9]+)(?:_([A-Za-z0-9]+))?')


def handle_travel(line: str) -> bool:
    """Note map changes (pause leave reconciliation during the unstable
    session-rebuild window) and capture the new map + side for the board."""
    if "ProcessServerTravel" not in line:
        return False
    global _last_travel_seen, _current_map, _current_side
    _last_travel_seen = time.monotonic()
    m = _TRAVEL_RE.search(line)
    if m:
        _current_map = m.group(1)
        _current_side = m.group(3) or ""
        log.info("[MAP] %s", map_label())
        _board_dirty.set()
        write_metrics()
    log.debug("Map travel observed - pausing leave reconciliation briefly.")
    return True


def handle_join(line: str) -> bool:
    """A successful join only TRIGGERS a prompt reconcile - the actual "joined"
    announcement comes from reconcile_players once the player is confirmed in
    listplayers. This avoids announcing arrivals (or the wrong count) before the
    player is really in-session."""
    if "LogNet: Join succeeded:" not in line:
        return False
    global _reconcile_pending
    _reconcile_pending = True
    return True


def _listplayers_online() -> dict[str, str] | None:
    """Return {player_id: name} of connected players, or None if RCON is down or
    the reply is malformed (missing the expected header)."""
    result = send_rcon("listplayers")
    if result is None or "NetID" not in result:
        return None
    ids = parse_listplayers(result)
    # Reuse cached roster names; resolve only ids we don't already have, so a
    # steady roster with known names makes zero Steam API calls.
    # ponytail: mid-game renames aren't re-fetched; add periodic re-resolve only
    # if that turns out to matter.
    resolved = _resolve_names([pid for pid in ids if pid not in player_cache])
    return {pid: player_cache.get(pid) or resolved.get(pid) or pid for pid in ids}


def reconcile_players() -> None:
    """Single source of truth for BOTH joins and leaves: diff the cache against
    RCON listplayers (the only authoritative list of who is actually in-session).

    This is why a connecting client (a log "Login request" / "Join succeeded")
    is never announced directly - a player who is mid-handshake or still loading
    the map is not yet in listplayers, and announcing off the log produced
    phantom "left" messages before the player had even arrived. Here a player is
    announced "joined" only once they appear in listplayers, and "left" only
    once they disappear from it (confirmed by a second snapshot against a
    momentarily truncated reply).

    The cache is seeded silently at startup, so an existing roster is NOT
    re-announced as a flood of joins.
    """
    # During/just after a map change the session is rebuilding and listplayers
    # can momentarily omit travelling players; don't reconcile in that window.
    if time.monotonic() - _last_travel_seen < TRAVEL_RECONCILE_PAUSE:
        return

    online = _listplayers_online()
    if online is None:
        return

    # Leaves: cached players no longer in listplayers (confirmed twice).
    suspects = [sid for sid in player_cache if sid not in online]
    if suspects:
        confirm = _listplayers_online()         # second snapshot guards a truncated reply
        if confirm is not None:
            for steam_id in suspects:
                if steam_id in confirm:         # reappeared - was a transient drop
                    continue
                name = cache_pop(steam_id)
                log.info("[LEAVE] %s (%s)", name, steam_id)
                send_rcon(f"say {name} disconnected")
                player_event(name, "left")

    # Joins: players now in listplayers that we hadn't recorded yet.
    for steam_id, name in online.items():
        if steam_id not in player_cache:
            name = sanitize_name(name)
            cache_add(steam_id, name)
            log.info("[JOIN] %s (%s)", name, steam_id)
            send_rcon(f"say {name} connected")
            player_event(name, "joined")


def handle_session_warning(line: str) -> bool:
    """Use the (unreliable) session warning only as a trigger for an immediate
    reconcile, so a real leave is announced within ~1s instead of waiting for
    the periodic poll. reconcile_players confirms via listplayers, so spurious
    warnings cost nothing."""
    if "is not part of session" not in line:
        return False
    if player_cache:                 # only worth a reconcile if someone could leave
        global _reconcile_pending
        _reconcile_pending = True
    return True


# Main loop dispatch. handle_login runs here (live) to learn player NAMES from
# the log into login_names; it never touches the roster, so joins/leaves are
# still detected authoritatively by reconcile_players (via listplayers).
# handle_crash runs first because it needs maps_pool.
def process_line(line: str, maps_pool: list[str]) -> None:
    if handle_crash(line, maps_pool):
        return
    for handler in (handle_login, handle_travel, handle_join, handle_session_warning):
        if handler(line):
            return


def backfill_names_from_tail(f) -> None:
    """Backfill login_names from recent "Login request" lines in the log tail, so
    players already connected at (re)start are named without waiting for them to
    reconnect. Bounded read (last PRELOAD_MAX_BYTES / PRELOAD_MAX_LINES); a login
    line older than that window is simply missed. Names only - the live roster
    still comes from RCON listplayers.
    """
    f.seek(0, os.SEEK_END)
    size = f.tell()
    start = max(0, size - PRELOAD_MAX_BYTES)
    f.seek(start)
    if start:
        f.readline()                       # discard the partial first line
    tail_lines = f.readlines()             # bounded by PRELOAD_MAX_BYTES above
    for line in tail_lines[-PRELOAD_MAX_LINES:]:
        handle_login(line.rstrip())


def preload_cache(f) -> None:
    """Backfill known names from the log tail, then seed the live roster from
    RCON `listplayers`. Leaves the file positioned at EOF for tailing.
    """
    backfill_names_from_tail(f)            # fill login_names from recent logins
    if seed_cache_from_rcon():
        f.seek(0, os.SEEK_END)             # roster came from RCON - tail from end
        return
    # RCON unavailable: no authoritative roster yet. Best-effort seed from the
    # names we just learned; reconcile corrects it once RCON returns.
    # ponytail: can transiently over/under-count until RCON is back.
    log.info("listplayers unavailable - seeding roster from recent log names.")
    for pid, name in login_names.items():
        cache_add(pid, name)
    f.seek(0, os.SEEK_END)


def seed_map_from_tail(f) -> None:
    """Find the most recent map from the log tail so the board shows the right
    map immediately on startup (before the next travel happens). Leaves the file
    positioned back at EOF for tailing."""
    global _current_map, _current_side
    try:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 30 * 1024 * 1024))    # last 30 MB ~ several map changes
        last = None
        for line in f:
            # keep only real travels (skip "?restart" lines without a scenario)
            if "ProcessServerTravel" in line and _TRAVEL_RE.search(line):
                last = line
        if last:
            m = _TRAVEL_RE.search(last)
            if m:
                _current_map = m.group(1)
                _current_side = m.group(3) or ""
                log.info("Seeded current map from log: %s", map_label())
    except Exception as e:
        log.debug("Map seed from tail skipped: %s", e)
    finally:
        f.seek(0, os.SEEK_END)             # always resume tailing from the end


def watch_logs() -> None:
    global _reconcile_pending
    maps_pool = load_maps()
    log.info("Monitoring: %s", LOG_FILE_PATH)

    if TELEGRAM_ENABLED:
        _load_board_id()
        threading.Thread(target=telegram_worker, daemon=True).start()
        log.info("Telegram game alerts enabled (chat %s).", TELEGRAM_CHAT_ID)
    else:
        log.info("Telegram game alerts disabled (no token/chat set).")

    while True:
        try:
            with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
                log.info("Pre-loading player cache (RCON listplayers, tail fallback)...")
                preload_cache(f)
                seed_map_from_tail(f)
                roster_refresh()
                log.info("Cache pre-loaded: %d player(s); map %s.",
                         len(player_cache), map_label())

                current_inode = os.fstat(f.fileno()).st_ino
                last_reconcile = time.monotonic()

                while True:
                    # Reconcile on a session-warning trigger (near-instant leaves)
                    # or on the periodic safety-net interval.
                    if _reconcile_pending or time.monotonic() - last_reconcile >= PLAYER_POLL_INTERVAL:
                        _reconcile_pending = False
                        reconcile_players()
                        # Heartbeat: rewrite the metric file even without changes,
                        # so its mtime staying still means the manager is dead
                        # (Grafana alerts on node_textfile_mtime_seconds).
                        write_metrics()
                        last_reconcile = time.monotonic()

                    line = f.readline()
                    if not line:
                        time.sleep(0.5)
                        try:
                            st = os.stat(LOG_FILE_PATH)
                            # Rotated by replacement: a brand-new file took the path.
                            if st.st_ino != current_inode:
                                log.info("Log rotation detected (new inode) - reopening file.")
                                break
                            # Rotated in place: Insurgency Sandstorm copies the log to a
                            # timestamped backup and TRUNCATES Insurgency.log, keeping the
                            # SAME inode. Our read offset is then stranded past EOF and we
                            # would silently stop seeing events. Detect the shrink and reopen.
                            if st.st_size < f.tell():
                                log.info("Log truncation detected (size %d < pos %d) - reopening file.",
                                         st.st_size, f.tell())
                                break
                        except FileNotFoundError:
                            log.warning("Log file disappeared - waiting...")
                            time.sleep(5)
                            break
                        continue
                    process_line(line.rstrip(), maps_pool)

        except FileNotFoundError:
            log.warning("Log file not found: %s - retrying in 10 s...", LOG_FILE_PATH)
            time.sleep(10)
        except Exception as e:
            log.exception("Unexpected error in watch loop: %s - restarting in 5 s.", e)
            time.sleep(5)


if __name__ == "__main__":
    watch_logs()
