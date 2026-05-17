"""
Samsung TV Client Service

Provides async WebSocket-based control of Samsung Smart TVs (Tizen platform, 2016+)
using the SmartThings Remote Control API exposed on the TV's local network.

---

Protocol overview
-----------------
Power state detection:
    GET http://<TV_IP>:8001/api/v2/
    - HTTP 200  → TV is on (body contains device info)
    - Connection refused / timeout → TV is off

Remote control:
    WS  ws://<TV_IP>:8001/api/v2/channels/samsung.remote.control?name=<b64>&token=<tok>
    WSS wss://<TV_IP>:8002/api/v2/channels/samsung.remote.control?name=<b64>&token=<tok>
    - Send JSON key commands (ms.remote.control)
    - TV responds with ms.channel.connect containing a fresh token

Wake from standby:
    UDP broadcast magic packet (Wake-on-LAN) to 255.255.255.255:9

---

Token auth flow
---------------
1. Connect WS with last known token (or empty string on first connect).
2. TV replies immediately with {"event": "ms.channel.connect", "data": {"token": "..."}}
3. Save token; use it on all future connections.
   Token survives TV reboots — only changes when the pairing is cleared.

---

Retry / reconnect strategy
---------------------------
Connection failures use exponential backoff (1 → 2 → 4 → 8 → 16 → 30 → 60 s).
The backoff resets after a successful WS open.

Error classification:
    TV_OFF      → TV is powered down; stop reconnect, rely on WoL to wake it.
    SSL_ISSUE   → WSS failed; fall back to plain WS on port 8001.
    TRANSIENT   → Network glitch; backoff and retry.
    FATAL       → Protocol/auth error; log and stop.

Command queue
-------------
Commands issued while the WS is not open are enqueued.  On reconnect the
queue is flushed in FIFO order before new commands are accepted.

---

Usage (from FastAPI lifespan or background task)
------------------------------------------------
    from services.samsung_tv_client import get_tv_client

    client = get_tv_client()
    await client.start()           # start background tasks
    await client.turn_on()
    await client.send_key("MUTE")
    status = client.get_status()   # dict safe for JSON serialisation
    await client.stop()            # clean shutdown
"""

import asyncio
import base64
import json
import logging
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

import requests
import websockets
import websockets.exceptions

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

# App name shown in the TV's "Connected devices" list (base64-encoded).
# Read from SAMSUNG_TV_APP_NAME env var (set in .env + docker-compose), falls back to default.
_APP_NAME_B64: str = base64.b64encode(
    os.environ.get("SAMSUNG_TV_APP_NAME", "Smart Home Controller").encode()
).decode()

# WebSocket endpoints
_WS_PORT: int = 8001   # plain WS (older TVs or SSL-fallback)
_WSS_PORT: int = 8002  # WSS (Samsung tokens require this on newer models)
_WS_PATH: str = "/api/v2/channels/samsung.remote.control"

# HTTP info endpoint
_HTTP_PORT: int = 8001
_HTTP_PATH: str = "/api/v2/"

# Reconnect backoff schedule (seconds).  Last value is held indefinitely.
_BACKOFF_SCHEDULE: List[int] = [1, 2, 4, 8, 16, 30, 60]

# Power-poll interval (seconds)
_POLL_INTERVAL: int = 5

# Max queued commands before the oldest is dropped to prevent memory growth
_MAX_QUEUE_DEPTH: int = 50

# Hysteresis for ON → OFF transitions.
#
# The Samsung Tizen HTTP info endpoint at :8001/api/v2/ is intermittently
# unresponsive even when the TV is actively on — a single timeout or transient
# network blip is NOT proof that the TV powered off.  Without hysteresis, one
# bad poll flips power_state to OFF, fires the on_power_change callback, and
# pushes "off" to every Hubitat subscriber, which then unsets WatchingTV mode
# (or any other mode/automation gated on the TV switch).  Five seconds later
# the next poll succeeds and we push "on" again — but the spurious mode change
# has already happened.
#
# Strategy:
#   • Any observation reading "on" (PowerState == "on") flips state to ON
#     immediately and resets the streak.  Spurious ON-when-OFF has never been
#     reported and would only happen if the TV lied — accept it as truth.
#   • Any non-ON observation (timeout, HTTP error, PowerState=standby, missing
#     field, etc.) increments _off_streak.  Only after _OFF_STREAK_THRESHOLD
#     consecutive non-ON readings do we flip ON → OFF.
#
# With _POLL_INTERVAL = 5 s and threshold = 3, worst-case Hubitat lag when the
# TV actually turns off is ~15-25 s.  That's acceptable for a "you stopped
# watching TV" signal and eliminates the false-positive mode changes.
_OFF_STREAK_THRESHOLD: int = 3

# Per-request HTTP timeout for the TV info endpoint poll.  Raised from the
# old 4 s default because Samsung's web server can stall for 5-7 s under load
# (especially during channel changes or app launches) without actually being
# unreachable.
_HTTP_POLL_TIMEOUT: int = 8

# Quick-retry budget inside a single poll cycle.  A single missed packet
# should not count as a "TV is off" observation — we retry once with a short
# gap before declaring the poll failed for this cycle.
_HTTP_POLL_RETRIES: int = 1
_HTTP_POLL_RETRY_GAP_S: float = 0.4


# =============================================================================
# Enums
# =============================================================================

class TVConnectionState(str, Enum):
    """Current state of the WebSocket connection to the TV."""
    DISCONNECTED = "disconnected"
    CONNECTING   = "connecting"
    CONNECTED    = "connected"


class TVPowerState(str, Enum):
    """Reported power state of the TV (via HTTP poll)."""
    ON      = "on"
    OFF     = "off"
    UNKNOWN = "unknown"


class _ErrorClass(Enum):
    """Internal error classification used to decide retry behaviour."""
    TV_OFF    = "tv_off"      # TV not reachable over HTTP either → skip WS retry
    SSL_ISSUE = "ssl_issue"   # WSS failed → retry with plain WS
    TRANSIENT = "transient"   # Temporary network blip → back off and retry
    FATAL     = "fatal"       # Protocol / auth error → stop retrying


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class SamsungTVConfig:
    """
    Connection parameters for a single Samsung TV.

    Attributes:
        tv_ip:          LAN IP address of the TV (e.g. '<LAN_IP>').
        mac_address:    MAC address without colons, uppercase (e.g. 'AABBCCDDEEFF').
                        Used for Wake-on-LAN.
        token:          Last known WS auth token.  Empty string on first run;
                        updated automatically after each connect.
        use_ssl:        Start with WSS (port 8002).  Falls back to WS automatically.
        poll_interval:  Seconds between HTTP power-state polls.
        max_retries:    Max consecutive reconnect attempts before giving up.
                        0 = unlimited.
        name:           Human-readable name for log messages.
    """
    tv_ip:         str
    mac_address:   str
    token:         str  = ""
    use_ssl:       bool = True
    poll_interval: int  = _POLL_INTERVAL
    max_retries:   int  = 0          # 0 = unlimited
    name:          str  = "samsung_tv"


# =============================================================================
# Token persistence callback type
# =============================================================================
# The caller can supply a coroutine that persists the new token whenever
# the TV hands us one.  Signature: async def save_token(token: str) -> None
TokenSaveCallback = Callable[[str], Any]


# =============================================================================
# Client
# =============================================================================

class SamsungTVClient:
    """
    Async Samsung TV remote-control client.

    Maintains a persistent WebSocket connection to the TV for low-latency key
    delivery, with automatic reconnect, exponential backoff, command queuing,
    WoL support, and HTTP-based power-state polling.

    Lifecycle
    ---------
        client = SamsungTVClient(config)
        await client.start()   # launches background tasks
        ...
        await client.stop()    # graceful shutdown

    Thread safety
    -------------
    All public methods are coroutines safe to call from any asyncio task.
    The internal state is protected by an asyncio.Lock — do not call from
    synchronous code without loop.run_until_complete().
    """

    def __init__(
        self,
        config: SamsungTVConfig,
        on_power_change: Optional[Callable[[TVPowerState], Any]] = None,
        on_conn_change:  Optional[Callable[["TVConnectionState"], Any]] = None,
        on_token_save:   Optional[TokenSaveCallback]             = None,
    ):
        """
        Initialise the client (does NOT connect — call start() for that).

        Args:
            config:           SamsungTVConfig instance.
            on_power_change:  Optional async callback fired when power state changes.
                              Signature: async def cb(new_state: TVPowerState) -> None
            on_conn_change:   Optional async callback fired when WS connection state
                              changes (connected/disconnected/connecting).
                              Signature: async def cb(new_state: TVConnectionState) -> None
            on_token_save:    Optional async callback fired when the TV issues a new
                              token.  Use this to persist the token to your DB.
                              Signature: async def cb(token: str) -> None
        """
        self.config            = config
        self._on_power_change  = on_power_change
        self._on_conn_change   = on_conn_change
        self._on_token_save    = on_token_save

        # --- State ---
        self._conn_state:   TVConnectionState = TVConnectionState.DISCONNECTED
        self._power_state:  TVPowerState      = TVPowerState.UNKNOWN
        self._use_ssl:      bool              = config.use_ssl
        self._retry_count:  int               = 0
        self._last_error:   Optional[str]     = None

        # ON → OFF debounce counter.  Counts consecutive non-ON poll
        # observations; only when it reaches _OFF_STREAK_THRESHOLD do we
        # actually transition power_state to OFF.  Reset to 0 on any ON
        # observation.  Prevents spurious mode changes in Hubitat caused by
        # the Samsung HTTP info endpoint occasionally timing out while the
        # TV is in fact on.  See module-level docstring on _OFF_STREAK_THRESHOLD.
        self._off_streak:   int               = 0

        # Last raw observation (for log/debug visibility — what the poll
        # actually saw before hysteresis was applied).
        self._last_observation: TVPowerState  = TVPowerState.UNKNOWN

        # --- Command queue ---
        # asyncio.Queue is FIFO and coroutine-safe.
        self._cmd_queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE_DEPTH)

        # --- Internal coordination ---
        self._ws:              Optional[websockets.WebSocketClientProtocol] = None
        self._lock:            asyncio.Lock  = asyncio.Lock()
        self._stop_event:      asyncio.Event = asyncio.Event()
        self._connected_event: asyncio.Event = asyncio.Event()

        # Fired by the poll loop when it detects the TV coming back on.
        # Set initially so the WS loop tries to connect on startup (optimistic).
        # Cleared when HTTP poll confirms TV is unreachable (EHOSTUNREACH / off).
        # This prevents the WS loop from hammering reconnects while TV is off.
        self._tv_on_event: asyncio.Event = asyncio.Event()
        self._tv_on_event.set()

        # --- Background task handles ---
        self._connect_task: Optional[asyncio.Task] = None
        self._poll_task:    Optional[asyncio.Task] = None

        self._log = logging.getLogger(f"{__name__}.{config.name}")

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """
        Start background tasks: WS connection loop + HTTP power-state poll.

        Safe to call multiple times — subsequent calls are no-ops if already
        running.
        """
        if self._connect_task and not self._connect_task.done():
            self._log.debug("start() called but already running — ignoring")
            return

        self._stop_event.clear()
        self._log.info(
            "Starting Samsung TV client for %s (IP=%s, MAC=%s, ssl=%s)",
            self.config.name, self.config.tv_ip, self.config.mac_address, self._use_ssl
        )

        self._connect_task = asyncio.create_task(
            self._connect_loop(), name=f"tv_ws_{self.config.name}"
        )
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"tv_poll_{self.config.name}"
        )

    async def stop(self) -> None:
        """
        Gracefully shut down: close WS, cancel background tasks.
        """
        self._log.info("Stopping Samsung TV client (%s)", self.config.name)
        self._stop_event.set()

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

        for task in (self._connect_task, self._poll_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        await self._update_conn_state(TVConnectionState.DISCONNECTED)
        self._log.info("Samsung TV client stopped")

    # =========================================================================
    # Public command API
    # =========================================================================

    async def turn_on(self) -> None:
        """
        Power on the TV — DaveGut strategy.

        WoL is the primary mechanism (works from full power-off).
        KEY_POWER is a bonus for the light-standby case where the WS
        connection is already open (TV is reachable but screen is off).

        Strategy:
            1. Always send WoL × 3.
               - Full off  → wakes the TV (sole mechanism).
               - Standby   → harmless no-op (TV already has network power).

            2. If the WS is currently open → also send KEY_POWER immediately.
               - Handles light standby where the TV is still on the network.
               - KEY_POWER is NOT queued for later delivery.  Queuing it would
                 cause it to fire after a WoL boot and toggle the TV back off.

            3. Always spawn _post_wol_poll() for fast boot detection.
               - Without it the WS loop waits up to 30 s for the next regular
                 poll tick before attempting to reconnect.
        """
        ws_open = self._ws is not None and self._conn_state == TVConnectionState.CONNECTED
        self._log.info(
            "turn_on() — power_state=%s  ws_open=%s",
            self._power_state.value, ws_open
        )

        # Step 1: WoL always — primary wake mechanism.
        await self._send_wol()

        # Step 2: KEY_POWER only if WS is live right now (light standby).
        # Safe because it's delivered instantly, not queued for post-boot.
        if ws_open:
            self._log.info("WS is open — also sending KEY_POWER for light-standby case")
            await self.send_key("KEY_POWER")

        # Step 3: Fast poll to detect TV boot (every 2 s, max 30 s).
        asyncio.create_task(
            self._post_wol_poll(), name=f"tv_wol_poll_{self.config.name}"
        )

    async def _post_wol_poll(self) -> None:
        """
        Poll the TV HTTP endpoint aggressively after Wake-on-LAN.

        Runs as a fire-and-forget task from turn_on().  Polls every 2 seconds
        for up to 30 seconds.  Stops early if the TV responds (power state flips
        to ON, which sets _tv_on_event and wakes the WS connect loop) or if
        stop() has been called.

        This is the mechanism that breaks the TV-off gate in _connect_loop
        after a WoL: without it, the WS loop would wait for the next
        regular 30-second poll tick before attempting to connect.
        """
        self._log.info("Post-WoL polling started (every 2s, max 30s)")
        for attempt in range(15):   # 15 × 2s = 30s maximum
            if self._stop_event.is_set():
                break
            await asyncio.sleep(2)
            state = await self.poll_power()
            self._log.debug("Post-WoL poll attempt %d/15: TV is %s", attempt + 1, state.value)
            if state == TVPowerState.ON:
                self._log.info("TV responded after WoL on attempt %d — WS connect imminent", attempt + 1)
                return

        self._log.info("Post-WoL poll window expired — TV did not respond within 30s")

    async def turn_off(self) -> None:
        """
        Power off (or toggle to standby) by sending KEY_POWER.

        For models that use KEY_POWEROFF for a hard power-down, use
        send_key("POWEROFF") instead.
        """
        self._log.info("turn_off()")
        await self.send_key("POWER")

    async def send_key(self, key: str, cmd: str = "Click") -> None:
        """
        Send a remote-control key press to the TV.

        The key is automatically normalised to uppercase and prefixed with
        "KEY_" if not already present (so both "mute" and "KEY_MUTE" work).

        Args:
            key: Samsung remote key name.  Common values:
                 POWER, POWEROFF, MUTE, VOLUMEUP, VOLUMEDOWN,
                 UP, DOWN, LEFT, RIGHT, ENTER, RETURN, EXIT,
                 HOME, MENU, SOURCE, HDMI, HDMI1–HDMI4,
                 1–9, 0, CHUP, CHDOWN, RED, GREEN, YELLOW, BLUE,
                 PLAY, PAUSE, STOP, FF, REW, RECORD.
            cmd: Command type — 'Click' (default), 'Press', or 'Release'.
        """
        key_norm = key.upper()
        if not key_norm.startswith("KEY_"):
            key_norm = f"KEY_{key_norm}"

        payload = json.dumps({
            "method": "ms.remote.control",
            "params": {
                "Cmd":           cmd,
                "DataOfCmd":     key_norm,
                "TypeOfRemote":  "SendRemoteKey",
            }
        })

        async with self._lock:
            if self._conn_state == TVConnectionState.CONNECTED and self._ws:
                # Happy path: connection is live — send immediately.
                try:
                    await self._ws.send(payload)
                    self._log.debug("Key sent directly: %s", key_norm)
                    return
                except websockets.exceptions.ConnectionClosed:
                    self._log.warning(
                        "WS closed mid-send for %s — queueing and reconnecting",
                        key_norm
                    )
                    self._conn_state = TVConnectionState.DISCONNECTED
                    self._connected_event.clear()
        # Fire conn change callback outside the lock to avoid holding it during I/O.
        if self._conn_state == TVConnectionState.DISCONNECTED and self._on_conn_change:
            asyncio.create_task(self._fire_conn_change(TVConnectionState.DISCONNECTED))

            # WS not ready — enqueue and let the reconnect loop handle it.
            if self._cmd_queue.full():
                dropped = await self._cmd_queue.get()
                self._log.warning("Queue full — dropped oldest command: %s", dropped[:60])
            await self._cmd_queue.put(payload)
            self._log.info("Queued command (%d in queue): %s", self._cmd_queue.qsize(), key_norm)

    # =========================================================================
    # Wake-on-LAN
    # =========================================================================

    async def send_wol(self) -> None:
        """Public coroutine wrapper for Wake-on-LAN (runs sync socket in executor)."""
        await self._send_wol()

    async def _send_wol(self) -> None:
        """
        Send a WoL magic packet in a thread-pool executor so the event loop
        is not blocked by the socket calls.

        Sends to BOTH 255.255.255.255 (LAN broadcast when running on the host)
        and the TV's unicast IP (works from inside a Docker bridge network,
        where the limited broadcast is confined to the docker0 bridge and never
        reaches the physical LAN).
        """
        mac = self.config.mac_address.replace(":", "").upper()
        if not mac or len(mac) != 12:
            self._log.error("WoL skipped: invalid MAC address '%s'", mac)
            return

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, _send_wol_sync, mac, self.config.tv_ip
            )
            self._log.info("WoL magic packet sent to %s (unicast + broadcast)", mac)
        except Exception as exc:
            self._log.error("WoL failed: %s", exc)

    # =========================================================================
    # HTTP Power Polling
    # =========================================================================

    async def poll_power(self) -> TVPowerState:
        """
        Query the TV's HTTP API to determine its current power state, then
        apply ON → OFF hysteresis before pushing any state change.

        Observation rules:
            • HTTP 200 + device.PowerState == "on"     → observation = ON
            • HTTP 200 + device.PowerState == anything → observation = OFF
            • timeout / connection refused / exception → observation = OFF

        Hysteresis rules (the whole point of this method):
            • observation == ON  → _off_streak = 0, transition to ON immediately.
            • observation == OFF → _off_streak += 1.  Only transition to OFF
              after _OFF_STREAK_THRESHOLD consecutive OFF observations.
              This prevents one bad poll from pushing a spurious "switch=off"
              event to Hubitat (which would unset WatchingTV mode / similar).

        The "first OFF observation while currently ON" case is special:
        we deliberately hold _power_state at ON until the streak threshold
        is reached, so callbacks (push_state_changes → Hubitat LAN push)
        do NOT fire on the transient blip.

        Returns:
            The current debounced TVPowerState (not necessarily the raw
            observation — that's only exposed via self._last_observation).
        """
        url  = f"http://{self.config.tv_ip}:{_HTTP_PORT}{_HTTP_PATH}"
        loop = asyncio.get_running_loop()

        try:
            resp = await loop.run_in_executor(None, _http_get_sync, url)
        except Exception as exc:
            # Belt-and-braces — _http_get_sync should swallow its own
            # exceptions, but if anything leaks out, treat as a failed poll.
            self._log.debug("poll_power: executor raised %s", exc)
            resp = None

        if not resp:
            observation = TVPowerState.OFF
            obs_reason  = "no_http_response"
        else:
            # Samsung TVs with Instant On keep the network active even in
            # standby — HTTP 200 still returns but PowerState != "on".
            power_field = (resp.get("device") or {}).get("PowerState", "")
            if power_field == "on":
                observation = TVPowerState.ON
                obs_reason  = "PowerState=on"
            else:
                observation = TVPowerState.OFF
                obs_reason  = f"PowerState={power_field or 'MISSING'}"

        self._last_observation = observation

        # --- Hysteresis ---
        if observation == TVPowerState.ON:
            # Any ON reading is taken at face value and resets the streak.
            if self._off_streak:
                self._log.debug(
                    "ON observation cleared _off_streak (was %d)", self._off_streak
                )
            self._off_streak = 0
            await self._update_power_state(TVPowerState.ON)
            return TVPowerState.ON

        # observation == OFF
        self._off_streak += 1

        # Already-OFF case: nothing to debounce, just stay OFF.
        if self._power_state == TVPowerState.OFF:
            return TVPowerState.OFF

        # UNKNOWN → OFF on the first observation is fine (startup case).
        if self._power_state == TVPowerState.UNKNOWN:
            self._log.info(
                "First OFF observation from UNKNOWN startup state (%s)", obs_reason
            )
            await self._update_power_state(TVPowerState.OFF)
            return TVPowerState.OFF

        # ON → OFF transition: only fire after threshold consecutive OFFs.
        if self._off_streak < _OFF_STREAK_THRESHOLD:
            self._log.info(
                "Suppressing ON→OFF transition (streak %d/%d, reason=%s) — "
                "TV may have hiccuped; holding state ON",
                self._off_streak, _OFF_STREAK_THRESHOLD, obs_reason,
            )
            return TVPowerState.ON

        # Threshold reached — TV is genuinely off / unreachable.
        self._log.info(
            "ON→OFF transition committed after %d consecutive OFF observations "
            "(latest reason=%s)",
            self._off_streak, obs_reason,
        )
        await self._update_power_state(TVPowerState.OFF)
        return TVPowerState.OFF

    async def _update_power_state(self, new_state: TVPowerState) -> None:
        """
        Update cached power state and fire the on_power_change callback if
        the state has actually changed.

        Also manages _tv_on_event so the WS connect loop can gate on it:
        - TV OFF → clear the event (WS loop suspends)
        - TV ON  → set the event (WS loop wakes and reconnects)
        """
        if new_state == self._power_state:
            return

        self._log.info(
            "Power state: %s → %s", self._power_state.value, new_state.value
        )
        self._power_state = new_state

        # Signal / suspend the WS connect loop based on power state.
        if new_state == TVPowerState.ON:
            self._retry_count = 0   # Fresh backoff slate when TV comes back
            self._tv_on_event.set()
        elif new_state == TVPowerState.OFF:
            self._tv_on_event.clear()

        if self._on_power_change:
            try:
                result = self._on_power_change(new_state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                self._log.error("on_power_change callback raised: %s", exc)

    async def _update_conn_state(self, new_state: "TVConnectionState") -> None:
        """
        Update cached WS connection state and fire on_conn_change callback
        if the state actually changed.

        This ensures Hubitat (and any other listener) is notified when the WS
        connection is established or lost — not just when power state changes.
        """
        if new_state == self._conn_state:
            return

        self._log.info(
            "Conn state: %s → %s", self._conn_state.value, new_state.value
        )
        self._conn_state = new_state

        if new_state == TVConnectionState.CONNECTED:
            self._connected_event.set()
        else:
            self._connected_event.clear()

        if self._on_conn_change:
            try:
                result = self._on_conn_change(new_state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                self._log.error("on_conn_change callback raised: %s", exc)

    async def _fire_conn_change(self, state: "TVConnectionState") -> None:
        """Fire on_conn_change as a standalone coroutine (for create_task usage)."""
        if self._on_conn_change:
            try:
                result = self._on_conn_change(state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                self._log.error("on_conn_change callback raised: %s", exc)

    # =========================================================================
    # Status introspection
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """
        Return a JSON-serialisable status snapshot for the REST API or UI.

        Returns:
            Dict with keys: name, tv_ip, mac, conn_state, power_state,
            use_ssl, queued_commands, retry_count, last_error, token_set,
            off_streak, last_observation.
        """
        return {
            "name":             self.config.name,
            "tv_ip":            self.config.tv_ip,
            "mac":              self.config.mac_address,
            "conn_state":       self._conn_state.value,
            "power_state":      self._power_state.value,
            "use_ssl":          self._use_ssl,
            "queued_commands":  self._cmd_queue.qsize(),
            "retry_count":      self._retry_count,
            "last_error":       self._last_error,
            "token_set":        bool(self.config.token),
            # Debounce visibility: how many consecutive OFF observations
            # we've seen.  Used to debug "Hubitat thinks TV is off but it's on".
            "off_streak":         self._off_streak,
            "off_threshold":      _OFF_STREAK_THRESHOLD,
            "last_observation":   self._last_observation.value,
        }

    # =========================================================================
    # Background task: WebSocket connection loop
    # =========================================================================

    async def _connect_loop(self) -> None:
        """
        Main reconnection loop.

        Runs until stop() is called.  Each iteration:
        1. If TV is confirmed OFF, wait here (non-spinning) for the poll loop
           to fire _tv_on_event — no WS attempts while the TV is unreachable.
        2. Build the WS URL (WSS or WS depending on SSL fallback state).
        3. Try to connect and run the session.
        4. On failure, classify the error and apply backoff.
        """
        while not self._stop_event.is_set():

            # ── TV-off gate ───────────────────────────────────────────────────
            # If the HTTP poll has confirmed the TV is off, there is no point
            # attempting a WS connection — it will only produce EHOSTUNREACH.
            # Sleep here in 1-second ticks until _tv_on_event is set by the
            # poll loop (or stop() is called).
            if not self._tv_on_event.is_set():
                async with self._lock:
                    self._conn_state = TVConnectionState.DISCONNECTED
                self._log.debug(
                    "TV confirmed off — WS reconnect suspended; "
                    "waiting for HTTP poll to detect power-on"
                )
                while not self._stop_event.is_set() and not self._tv_on_event.is_set():
                    await asyncio.sleep(1)
                if self._stop_event.is_set():
                    break
                self._log.info("TV power-on detected — resuming WS connect")

            url = self._build_ws_url()
            self._log.debug("Attempting WS connect: %s", url[:80])

            await self._update_conn_state(TVConnectionState.CONNECTING)

            try:
                await self._run_ws_session(url)
                # Session ended cleanly (TV closed) — short delay then reconnect.
                self._retry_count = 0
                await asyncio.sleep(2)

            except websockets.exceptions.InvalidHandshake as exc:
                err_class = _classify_error(exc)
                self._last_error = str(exc)
                self._log.warning("WS handshake failed (%s): %s", err_class.value, exc)

                if err_class == _ErrorClass.SSL_ISSUE and self._use_ssl:
                    self._log.warning("SSL handshake failed — falling back to plain WS")
                    self._use_ssl = False
                    # Retry immediately without backoff for the fallback attempt.
                    continue

                await self._backoff_or_stop()

            except (ConnectionRefusedError, OSError) as exc:
                # TV is off or unreachable at the network level (EHOSTUNREACH,
                # ECONNREFUSED, etc.).  Mark power state OFF — this clears
                # _tv_on_event so the gate at the top of the loop will suspend
                # WS reconnect attempts until the HTTP poll detects the TV
                # coming back on.  No exponential backoff needed here.
                self._last_error = str(exc)
                self._log.info(
                    "TV unreachable (%s) — suspending WS reconnect until TV powers on",
                    exc
                )
                await self._update_power_state(TVPowerState.OFF)

            except websockets.exceptions.ConnectionClosed as exc:
                self._last_error = str(exc)
                self._log.info("WS connection closed: %s", exc)
                await self._backoff_or_stop()

            except asyncio.CancelledError:
                # stop() was called — exit cleanly.
                break

            except Exception as exc:
                self._last_error = str(exc)
                self._log.error("Unexpected WS error: %s", exc, exc_info=True)
                await self._backoff_or_stop()

            finally:
                async with self._lock:
                    self._ws = None
                await self._update_conn_state(TVConnectionState.DISCONNECTED)

        self._log.info("WS connect loop exiting")

    async def _run_ws_session(self, url: str) -> None:
        """
        Manage the lifetime of a single WebSocket session.

        Opens the connection, flushes any queued commands, then enters the
        receive loop.  Returns when the connection closes cleanly.

        Args:
            url: Full WebSocket URL including query params.
        """
        # Build SSL context that accepts the TV's self-signed cert.
        ssl_ctx: Optional[ssl.SSLContext] = None
        if url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode    = ssl.CERT_NONE

        connect_kwargs: Dict[str, Any] = {
            "open_timeout": 8,
            "close_timeout": 5,
            # Samsung TVs do NOT respond to standard WS ping frames.
            # The websockets library default (ping_interval=20) kills the
            # connection with 1002 (protocol error) after the pong timeout.
            # Disabling pings keeps the connection alive indefinitely,
            # which is critical for the initial pairing flow where the user
            # must physically hit "Allow" on the TV before a token is sent.
            "ping_interval": None,
            "ping_timeout": None,
        }
        if ssl_ctx:
            connect_kwargs["ssl"] = ssl_ctx

        async with websockets.connect(url, **connect_kwargs) as ws:
            async with self._lock:
                self._ws             = ws
                self._retry_count    = 0
                self._last_error     = None
            await self._update_conn_state(TVConnectionState.CONNECTED)
            self._log.info("WS connected: %s", url[:80])

            # Wait for the TV's initial response BEFORE flushing commands.
            # Samsung TVs send ms.channel.connect (with token) or ms.error
            # immediately after the handshake.  Flushing commands before
            # receiving the welcome message can cause a protocol error.
            try:
                first_msg = await asyncio.wait_for(ws.recv(), timeout=10)
                self._log.info("WS first message: %s", first_msg[:200])
                await self._handle_message(first_msg)
            except asyncio.TimeoutError:
                self._log.warning("No initial message from TV within 10s")

            # Flush any commands that were queued while we were disconnected.
            await self._flush_pending_commands(ws)

            # Receive loop — processes token updates and keep-alive pings.
            async for raw_message in ws:
                self._log.debug("WS recv: %s", raw_message[:200])
                await self._handle_message(raw_message)

    async def _flush_pending_commands(
        self, ws: websockets.WebSocketClientProtocol
    ) -> None:
        """
        Drain the command queue over the freshly opened WebSocket.

        Sends all pending commands in FIFO order.  Stops immediately if
        the socket closes mid-flush.

        Args:
            ws: Open WebSocket connection.
        """
        flushed = 0
        while not self._cmd_queue.empty():
            try:
                payload = self._cmd_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await ws.send(payload)
                flushed += 1
                # Small gap to avoid overwhelming the TV's receive buffer.
                await asyncio.sleep(0.15)
            except websockets.exceptions.ConnectionClosed:
                # Re-enqueue so the next session can retry.
                await self._cmd_queue.put(payload)
                self._log.warning("WS closed during queue flush — re-enqueued command")
                break

        if flushed:
            self._log.info("Flushed %d queued command(s)", flushed)

    # =========================================================================
    # Background task: HTTP power polling
    # =========================================================================

    async def _poll_loop(self) -> None:
        """
        Periodically poll the TV's HTTP endpoint to track power state.

        Runs in parallel with the WS loop.  Also triggers a WS reconnect
        attempt when the TV comes back on after being detected as off.
        """
        while not self._stop_event.is_set():
            try:
                prev_state = self._power_state
                new_state  = await self.poll_power()

                # TV just came back on — kick the WS connect loop if it's
                # currently in a long backoff sleep.
                if prev_state == TVPowerState.OFF and new_state == TVPowerState.ON:
                    self._log.info(
                        "TV came back on — resetting retry counter to trigger reconnect"
                    )
                    self._retry_count = 0

            except Exception as exc:
                self._log.error("poll_loop error: %s", exc)

            # Sleep in short increments so stop() is responsive.
            for _ in range(self.config.poll_interval):
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(1)

        self._log.info("Poll loop exiting")

    # =========================================================================
    # Message handling
    # =========================================================================

    async def _handle_message(self, raw: str) -> None:
        """
        Process an incoming WebSocket message from the TV.

        Currently handles:
        - ms.channel.connect  → extract and save auth token
        - ms.channel.ready    → TV pairing confirmed
        - error events        → log and update error state

        Args:
            raw: Raw JSON string from the TV.
        """
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            self._log.debug("Non-JSON WS message: %s", raw[:100])
            return

        event = msg.get("event", "")

        if event == "ms.channel.connect":
            token = msg.get("data", {}).get("token")
            if token:
                self._log.info("Received WS auth token from TV: %s", token)
                self.config.token = str(token)
                # Also write directly to state file as a safety net
                try:
                    with open("/app/state/samsung_tv_token.txt", "w") as _f:
                        _f.write(str(token))
                    self._log.info("Token written to /app/state/samsung_tv_token.txt")
                except Exception as _e:
                    self._log.error("Could not write token file: %s", _e)
                if self._on_token_save:
                    try:
                        result = self._on_token_save(token)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        self._log.error("on_token_save callback raised: %s", exc)

        elif event == "ms.channel.ready":
            self._log.debug("WS channel ready")

        elif event == "ms.error":
            error_msg = msg.get("data", {}).get("message", str(msg))
            self._last_error = error_msg
            self._log.warning("TV WS error event: %s", error_msg)

        else:
            self._log.debug("WS message [%s]: %s", event, str(msg)[:120])

    # =========================================================================
    # Helpers
    # =========================================================================

    def _build_ws_url(self) -> str:
        """
        Build the WebSocket URL for this TV, including auth token if available.

        Returns:
            Full URL string, e.g.:
            wss://<LAN_IP>:8002/api/v2/channels/samsung.remote.control
                ?name=U21hcnQgSG9tZSBDb250cm9sbGVy&token=12345678
        """
        if self._use_ssl:
            scheme = "wss"
            port   = _WSS_PORT
        else:
            scheme = "ws"
            port   = _WS_PORT

        token_param = f"&token={self.config.token}" if self.config.token else ""
        return (
            f"{scheme}://{self.config.tv_ip}:{port}{_WS_PATH}"
            f"?name={_APP_NAME_B64}{token_param}"
        )

    async def _backoff_or_stop(self) -> None:
        """
        Sleep for the next backoff interval, or set the stop event if the max
        retry count has been exceeded.

        Updates self._retry_count before sleeping.
        """
        max_r = self.config.max_retries
        if max_r > 0 and self._retry_count >= max_r:
            self._log.error(
                "Max retries (%d) reached — stopping reconnect for %s",
                max_r, self.config.name
            )
            self._stop_event.set()
            return

        idx     = min(self._retry_count, len(_BACKOFF_SCHEDULE) - 1)
        wait    = _BACKOFF_SCHEDULE[idx]
        self._retry_count += 1

        self._log.info(
            "Retry %d — waiting %ds before reconnect (%s)",
            self._retry_count, wait, self.config.name
        )

        # Sleep in 1-second increments so stop() can interrupt quickly.
        for _ in range(wait):
            if self._stop_event.is_set():
                return
            await asyncio.sleep(1)


# =============================================================================
# Module-level helpers (synchronous, run in executor)
# =============================================================================

def _send_wol_sync(mac: str, tv_ip: str) -> None:
    """
    Send a Wake-on-LAN magic packet to both broadcast and unicast targets.

    The magic packet is: 6 × 0xFF followed by the target MAC address
    repeated 16 times — 102 bytes total (standard WoL spec).

    Two destinations, repeated 3 × each with 200 ms gaps:
        255.255.255.255:9  — LAN broadcast (works when running on the host or
                             in a container with host networking).
        <tv_ip>:9          — Unicast to the TV's IP (works from inside a Docker
                             bridge network where the limited broadcast is
                             confined to the Docker bridge and never reaches the
                             physical LAN).  Most NIC firmware accepts unicast
                             magic packets.

    Args:
        mac:    MAC address as a 12-character hex string, no separators,
                uppercase (e.g. 'AABBCCDDEEFF').
        tv_ip:  TV's LAN IP address (e.g. '<LAN_IP>').
    """
    mac_bytes    = bytes.fromhex(mac)
    magic_packet = b"\xff" * 6 + mac_bytes * 16  # 102 bytes

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            s.sendto(magic_packet, ("255.255.255.255", 9))   # broadcast
            s.sendto(magic_packet, (tv_ip, 9))               # unicast (Docker-safe)
            time.sleep(0.2)


def _http_get_sync(
    url: str,
    timeout: int = _HTTP_POLL_TIMEOUT,
    retries: int = _HTTP_POLL_RETRIES,
) -> Optional[Dict[str, Any]]:
    """
    Synchronous HTTP GET to the TV's info endpoint, with quick retries.

    A single dropped packet should not count as "the TV is off".  Inside
    one poll cycle we attempt the request up to (1 + retries) times,
    sleeping _HTTP_POLL_RETRY_GAP_S between attempts.  Only if every
    attempt fails do we return None, which the caller then folds into
    the OFF-streak debounce.

    Returns the parsed JSON body if any attempt succeeds with HTTP 200,
    or None if all attempts fail / return a non-200 status.

    Args:
        url:     Full URL to query.
        timeout: Per-attempt request timeout in seconds.
        retries: Number of retry attempts after the first failure.

    Returns:
        Parsed JSON dict or None.
    """
    attempts = 1 + max(0, retries)
    last_exc: Optional[BaseException] = None

    for attempt_idx in range(attempts):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    # 200 but non-JSON body still means the TV is responding.
                    return {"raw": resp.text[:200]}
            # Non-200 — not retriable in any useful way for this endpoint
            # (no auth, no 429), so bail immediately.
            return None
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt_idx < attempts - 1:
                time.sleep(_HTTP_POLL_RETRY_GAP_S)
                continue

    # All attempts failed.  We deliberately swallow the exception — the
    # caller cares whether the TV responded, not why it didn't.  The
    # debug-level log here is enough for forensic analysis.
    if last_exc:
        logger.debug("HTTP GET %s failed after %d attempts: %s", url, attempts, last_exc)
    return None


def _classify_error(exc: Exception) -> _ErrorClass:
    """
    Classify a WebSocket or network exception to drive retry logic.

    Args:
        exc: The caught exception.

    Returns:
        _ErrorClass member.
    """
    msg = str(exc).lower()

    if isinstance(exc, ssl.SSLError) or "ssl" in msg or "certificate" in msg:
        return _ErrorClass.SSL_ISSUE

    if isinstance(exc, (ConnectionRefusedError, OSError)):
        return _ErrorClass.TV_OFF

    if "invalid status code" in msg or "403" in msg or "401" in msg:
        return _ErrorClass.FATAL

    return _ErrorClass.TRANSIENT


# =============================================================================
# Singleton registry — one client per TV IP
# =============================================================================

_clients: Dict[str, "SamsungTVClient"] = {}


def get_tv_client(
    tv_ip:          str  = "<LAN_IP>",
    mac_address:    str  = "AABBCCDDEEFF",
    token:          str  = "",
    use_ssl:        bool = True,
    name:           str  = "living_room_tv",
    on_power_change: Optional[Callable[[TVPowerState], Any]] = None,
    on_conn_change:  Optional[Callable[["TVConnectionState"], Any]] = None,
    on_token_save:   Optional[TokenSaveCallback]             = None,
) -> "SamsungTVClient":
    """
    Get or create a singleton SamsungTVClient for the given TV IP.

    Clients are cached by IP — calling this multiple times with the same IP
    returns the same instance.  This prevents duplicate WS connections.

    Args:
        tv_ip:          TV LAN IP address.
        mac_address:    TV MAC address (no colons, uppercase).
        token:          Persisted auth token (pass '' on first run).
        use_ssl:        Prefer WSS (will auto-fall-back to WS on SSL error).
        name:           Logical name for logs and status API.
        on_power_change: Callback fired on power state changes.
        on_conn_change:  Callback fired on WS connection state changes.
        on_token_save:   Callback fired when a new token is issued by the TV.

    Returns:
        SamsungTVClient instance.
    """
    if tv_ip not in _clients:
        cfg = SamsungTVConfig(
            tv_ip       = tv_ip,
            mac_address = mac_address,
            token       = token,
            use_ssl     = use_ssl,
            name        = name,
        )
        _clients[tv_ip] = SamsungTVClient(
            cfg,
            on_power_change = on_power_change,
            on_conn_change  = on_conn_change,
            on_token_save   = on_token_save,
        )
        logger.info("Created SamsungTVClient for %s (%s)", name, tv_ip)

    return _clients[tv_ip]
 
