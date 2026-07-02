"""
Hisense TV Client Service (Fire TV / Android TV via ADB-over-TCP)
==================================================================

Async client for controlling Hisense TVs running Fire TV (Amazon's Android
TV fork) — primary target model: 75U7SF, 2026 ULED-X mini-LED. Mirrors the
shape of ``services/samsung_tv_client.py`` so the FastAPI / Hubitat-push
integration layer is the same; differs in the transport (ADB-over-TCP
instead of Tizen WebSocket+SSL).

Protocol overview
-----------------
Fire TV (and Android TV in general) doesn't expose a native REST or
WebSocket API. The standard local-control surface is **ADB** (Android
Debug Bridge) running over TCP on the TV's port 5555 once "ADB Debugging"
is enabled in the TV's developer settings.

  Commands:
    adb connect <TV_IP>:5555
    adb -s <TV_IP>:5555 shell input keyevent <KEYCODE>
    adb -s <TV_IP>:5555 shell am start -n <package>/<activity>
    adb -s <TV_IP>:5555 shell dumpsys power | grep "Display Power"

  Pairing:
    On first ``adb connect`` from a new host, the TV displays a PIN /
    "Allow USB debugging?" prompt. The operator must accept it on the TV
    using the physical remote. After acceptance, this host's RSA public
    key (from ``~/.android/adbkey.pub`` inside the container) is
    persisted on the TV and connection is silent thereafter.

  Wake / power state:
    - Full off: TV is not on the network at all. ADB connection fails
      with ``failed to connect``. Wake via Wake-on-LAN UDP magic packet
      (TV's "Quick Start" / "Always-on standby" / "Wake on LAN" setting
      must be enabled — usually under Network / Standby settings).
    - Network standby / display-off: ADB connection succeeds but
      ``dumpsys power`` reports ``Display Power: state=OFF``. Wake via
      ``input keyevent 26`` (POWER) or ``input keyevent 224`` (WAKEUP).
    - Display-on: ``dumpsys power`` reports ``state=ON`` (or ``DOZE``
      when in screen-saver / ambient mode).

Why shell out to the system ``adb`` binary
------------------------------------------
The Python ADB client libraries (``adb-shell``, ``python-adb``) have a
poor compatibility track record with Fire TV firmware revisions —
Google rotates protocol details and the system ``adb`` binary catches
up first. Shelling out via ``asyncio.create_subprocess_exec`` keeps
this driver immune to those churn cycles and matches the canonical
debugging workflow (any command we issue, the operator can run by hand
to reproduce). Cost: ``adb`` binary must be installed in the
smarthome-app container (see Dockerfile note below).

  Dockerfile required addition:
      RUN apt-get update && apt-get install -y --no-install-recommends \\
          android-tools-adb && rm -rf /var/lib/apt/lists/*

Hysteresis-debounced power polling
----------------------------------
Direct lesson from the 2026-05-17 Samsung TV state-machine fix
(``services/samsung_tv_client.py``): a single failed power poll must
NOT flip the cached ``power_state`` to OFF and fire a push to Hubitat
that unsets a mode like "WatchingTV." Required at least 3 consecutive
non-ON observations before committing the ON → OFF transition. The
same hysteresis is implemented here verbatim because Fire TV's ADB
connection is at least as flaky as the Samsung HTTP info endpoint —
the TV can fail to answer a single ``dumpsys`` invocation under load
without actually being off.

Lifecycle (matches Samsung client API)
--------------------------------------
    from services.hisense_tv_client import get_tv_client

    client = get_tv_client(tv_ip="<LAN_IP>", mac_address="AABBCCDDEEFF")
    await client.start()                # launches background tasks
    await client.turn_on()              # WoL + (if connected) KEYCODE_POWER
    await client.send_key("VOLUME_UP")  # any Android KEYCODE name
    status = client.get_status()        # JSON-serialisable snapshot
    await client.stop()                 # graceful shutdown
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Port the TV listens on for ADB-over-TCP (standard Android default).
_ADB_TCP_PORT: int = 5555

# Reconnect backoff schedule (seconds). Last value held indefinitely.
_BACKOFF_SCHEDULE: List[int] = [1, 2, 4, 8, 16, 30, 60]

# Power-poll interval. Same value as the Samsung client for consistency.
_POLL_INTERVAL: int = 5

# Hysteresis for ON → OFF transitions — see module docstring.
# Three consecutive non-ON observations before committing the OFF flip.
_OFF_STREAK_THRESHOLD: int = 3

# Per-attempt ADB subprocess timeout. Fire TV's adb daemon under load can
# briefly stall — 8s gives it room without burning a whole poll cycle.
_ADB_TIMEOUT_S: float = 8.0

# Quick in-cycle retry on a single failed ADB invocation (mirrors the
# Samsung HTTP poll's _HTTP_POLL_RETRIES). One retry catches a single
# dropped packet without counting toward the OFF streak.
_ADB_POLL_RETRIES: int = 1
_ADB_POLL_RETRY_GAP_S: float = 0.4

# Max queued commands before the oldest is dropped.
_MAX_QUEUE_DEPTH: int = 50

# Android KEYCODE table — names map to their numeric codes for
# ``input keyevent <CODE>``. Subset relevant to TV remote control;
# full list at https://developer.android.com/reference/android/view/KeyEvent
# (constants prefixed ``KEYCODE_``). The map is the closed vocabulary
# this driver accepts; arbitrary numeric codes are not exposed to the
# blueprint or the agent (Phase 2+) to keep the surface auditable.
_KEYCODES: Dict[str, int] = {
    # Power
    "POWER":             26,
    "WAKEUP":            224,
    "SLEEP":             223,
    # Volume / mute
    "VOLUME_UP":         24,
    "VOLUME_DOWN":       25,
    "MUTE":              164,
    "VOLUME_MUTE":       164,
    # Channel
    "CHANNEL_UP":        166,
    "CHANNEL_DOWN":      167,
    # Navigation
    "DPAD_UP":           19,
    "DPAD_DOWN":         20,
    "DPAD_LEFT":         21,
    "DPAD_RIGHT":        22,
    "DPAD_CENTER":       23,
    "ENTER":             66,
    "BACK":              4,
    "HOME":              3,
    "MENU":              82,
    # Media transport
    "MEDIA_PLAY_PAUSE":  85,
    "MEDIA_PLAY":        126,
    "MEDIA_PAUSE":       127,
    "MEDIA_STOP":        86,
    "MEDIA_NEXT":        87,
    "MEDIA_PREVIOUS":    88,
    "MEDIA_REWIND":      89,
    "MEDIA_FAST_FORWARD": 90,
    # Source / input
    "TV_INPUT":          178,
    "TV_INPUT_HDMI_1":   243,
    "TV_INPUT_HDMI_2":   244,
    "TV_INPUT_HDMI_3":   245,
    "TV_INPUT_HDMI_4":   246,
    # Misc
    "INFO":              165,
    "GUIDE":             172,
    "SETTINGS":          176,
    "CAPTIONS":          175,
    "SEARCH":            84,
    "NOTIFICATION":      83,
}


# =============================================================================
# Enums
# =============================================================================


class TVConnectionState(str, Enum):
    """Current state of the ADB connection to the TV."""
    DISCONNECTED = "disconnected"
    CONNECTING   = "connecting"
    CONNECTED    = "connected"


class TVPowerState(str, Enum):
    """Reported display power state of the TV (via ``dumpsys power``)."""
    ON      = "on"
    OFF     = "off"
    UNKNOWN = "unknown"


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class HisenseTVConfig:
    """
    Connection parameters for a single Hisense / Fire TV.

    Attributes
    ----------
    tv_ip :
        LAN IP address of the TV (e.g. ``"<LAN_IP>"``).
    mac_address :
        MAC address without colons, uppercase (e.g. ``"AABBCCDDEEFF"``).
        Used for Wake-on-LAN to bring the TV out of full-off.
    name :
        Human-readable name for log messages.
    poll_interval :
        Seconds between dumpsys-power polls.
    adb_path :
        Path to the ``adb`` binary. Default ``"adb"`` (PATH lookup).
    """
    tv_ip:         str
    mac_address:   str
    name:          str  = "hisense_tv"
    poll_interval: int  = _POLL_INTERVAL
    adb_path:      str  = "adb"


# Token-style callback typedef kept for API parity with the Samsung client
# even though ADB has no token concept — lets the lifespan code in app.py
# wire both clients identically.
StateChangeCallback = Callable[[Any], Any]


# =============================================================================
# Client
# =============================================================================


class HisenseTVClient:
    """
    Async ADB-over-TCP client for Fire TV / Android TV devices.

    Maintains a "connected" ADB session by re-issuing ``adb connect`` as
    needed before each command (ADB's TCP session can be reaped by the
    TV's adbd between commands; the cheapest way to be correct is to
    always make sure the connection is live before sending). A background
    power-polling loop tracks display state and fires the
    ``on_power_change`` callback whenever the hysteresis-debounced state
    transitions.

    Thread safety: all public methods are coroutines safe to call from
    any asyncio task; an asyncio.Lock serialises ADB subprocess
    invocations because concurrent ``adb shell`` calls on the same
    serial occasionally race the daemon.
    """

    def __init__(
        self,
        config: HisenseTVConfig,
        on_power_change: Optional[StateChangeCallback] = None,
        on_conn_change:  Optional[StateChangeCallback] = None,
    ):
        """
        Initialise the client (does NOT connect — call ``start()`` for that).

        Args:
            config:           ``HisenseTVConfig`` instance.
            on_power_change:  Optional async callback fired when display power
                              state transitions. Signature:
                              ``async def cb(new_state: TVPowerState) -> None``.
            on_conn_change:   Optional async callback fired when the ADB
                              connection state changes.
        """
        self.config            = config
        self._on_power_change  = on_power_change
        self._on_conn_change   = on_conn_change

        # --- State ---
        self._conn_state:  TVConnectionState = TVConnectionState.DISCONNECTED
        self._power_state: TVPowerState      = TVPowerState.UNKNOWN
        self._last_error:  Optional[str]     = None
        self._retry_count: int               = 0

        # ON → OFF debounce counter. Reset on any ON observation.
        # See module docstring on _OFF_STREAK_THRESHOLD.
        self._off_streak:        int          = 0
        self._last_observation:  TVPowerState = TVPowerState.UNKNOWN

        # --- Internal coordination ---
        self._adb_lock:    asyncio.Lock  = asyncio.Lock()
        self._stop_event:  asyncio.Event = asyncio.Event()
        self._poll_task:   Optional[asyncio.Task] = None

        self._log = logging.getLogger(f"{__name__}.{config.name}")

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """
        Start the background power-poll task. Safe to call multiple times.
        """
        if self._poll_task and not self._poll_task.done():
            self._log.debug("start() called but already running — ignoring")
            return

        self._stop_event.clear()
        self._log.info(
            "Starting Hisense TV client for %s (IP=%s, MAC=%s)",
            self.config.name, self.config.tv_ip, self.config.mac_address,
        )

        # Best-effort initial connect — failures here are normal if the TV
        # is currently off; the poll loop will retry as needed.
        await self._try_connect()

        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"hisense_poll_{self.config.name}"
        )

    async def stop(self) -> None:
        """Gracefully stop the poll loop and disconnect."""
        self._log.info("Stopping Hisense TV client (%s)", self.config.name)
        self._stop_event.set()

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass

        # Best-effort adb disconnect. Failures swallowed — we're going away.
        try:
            await self._run_adb(["disconnect", f"{self.config.tv_ip}:{_ADB_TCP_PORT}"], timeout=3.0)
        except Exception:
            pass

        await self._update_conn_state(TVConnectionState.DISCONNECTED)
        self._log.info("Hisense TV client stopped")

    # =========================================================================
    # Public command API (mirrors Samsung client)
    # =========================================================================

    async def turn_on(self) -> None:
        """
        Power on the TV.

        Strategy mirrors the Samsung client:
          1. WoL × 3 always — the only reliable wake from full-off.
          2. If ADB is currently connected (TV in network standby) →
             also fire KEYCODE_POWER for the light-standby case.
          3. Spawn post-WoL fast poll to detect boot quickly without
             waiting a full ``poll_interval`` tick.
        """
        adb_open = self._conn_state == TVConnectionState.CONNECTED
        self._log.info(
            "turn_on() — power_state=%s adb_open=%s",
            self._power_state.value, adb_open,
        )

        await self._send_wol()

        if adb_open:
            self._log.info("ADB open — also sending KEYCODE_POWER for light-standby")
            await self.send_key("POWER")

        asyncio.create_task(
            self._post_wol_poll(), name=f"hisense_wol_poll_{self.config.name}"
        )

    async def turn_off(self) -> None:
        """
        Power off (to network standby) by sending KEYCODE_POWER.

        Note: Fire TV's KEYCODE_POWER toggles standby, not full power-off.
        A full power-off requires the user to physically unplug the TV or
        use the TV's settings menu — there is no remote command for it on
        Android TV. This matches the Samsung Tizen behaviour.
        """
        self._log.info("turn_off()")
        await self.send_key("POWER")

    async def send_key(self, key: str) -> None:
        """
        Send a remote-control key press to the TV.

        ``key`` is looked up in ``_KEYCODES`` (case-insensitive, with or
        without the ``KEYCODE_`` prefix). Unknown keys raise ValueError —
        the closed vocabulary is intentional (auditable surface).

        If ADB is not currently connected, attempts a fresh connect first
        and then sends. If the connect fails, the call logs and returns
        without erroring — the next post-WoL or scheduled poll will reopen
        the connection.

        Args:
            key: Android KEYCODE name without prefix (e.g. ``"VOLUME_UP"``,
                 ``"DPAD_UP"``, ``"POWER"``).
        """
        keyname = key.upper().removeprefix("KEYCODE_")
        code = _KEYCODES.get(keyname)
        if code is None:
            raise ValueError(f"Unknown key {key!r} — not in _KEYCODES vocabulary")

        if self._conn_state != TVConnectionState.CONNECTED:
            if not await self._try_connect():
                self._log.warning(
                    "send_key %s: ADB not connected and reconnect failed — dropping",
                    keyname,
                )
                return

        result = await self._run_adb(
            ["-s", self._serial(), "shell", "input", "keyevent", str(code)],
            timeout=_ADB_TIMEOUT_S,
        )
        if result is None:
            # Connection died mid-send. Mark disconnected and let the poll
            # loop reopen.
            await self._update_conn_state(TVConnectionState.DISCONNECTED)
            self._log.warning(
                "send_key %s: ADB invocation failed — marked disconnected",
                keyname,
            )
            return
        self._log.debug("Key sent: %s (code=%d)", keyname, code)

    # =========================================================================
    # Status snapshot
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """
        Return a JSON-serialisable status snapshot. Same shape as the
        Samsung client's ``get_status()`` so dashboard/UI code is
        identical across the two TV types.
        """
        return {
            "name":              self.config.name,
            "tv_ip":             self.config.tv_ip,
            "mac":               self.config.mac_address,
            "conn_state":        self._conn_state.value,
            "power_state":       self._power_state.value,
            "retry_count":       self._retry_count,
            "last_error":        self._last_error,
            "off_streak":        self._off_streak,
            "off_threshold":     _OFF_STREAK_THRESHOLD,
            "last_observation":  self._last_observation.value,
            "transport":         "adb_tcp",
        }

    # =========================================================================
    # Wake-on-LAN
    # =========================================================================

    async def send_wol(self) -> None:
        """Public wrapper for Wake-on-LAN (runs sync socket in executor)."""
        await self._send_wol()

    async def _send_wol(self) -> None:
        """Send WoL to broadcast + unicast (Docker-bridge safe)."""
        mac = self.config.mac_address.replace(":", "").upper()
        if not mac or len(mac) != 12:
            self._log.error("WoL skipped: invalid MAC %r", mac)
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _send_wol_sync, mac, self.config.tv_ip)
            self._log.info("WoL magic packet sent to %s (unicast + broadcast)", mac)
        except Exception as exc:
            self._log.error("WoL failed: %s", exc)

    # =========================================================================
    # ADB connection management
    # =========================================================================

    def _serial(self) -> str:
        """The ``adb -s <serial>`` value for this TV: ``IP:port``."""
        return f"{self.config.tv_ip}:{_ADB_TCP_PORT}"

    async def _try_connect(self) -> bool:
        """
        Idempotent ``adb connect <ip>:5555`` attempt. Returns True iff
        the connection is live (verified by a trivial ``adb get-state``).

        Fires ``on_conn_change`` if the state actually transitions.
        """
        async with self._adb_lock:
            await self._update_conn_state(TVConnectionState.CONNECTING)
            connect_result = await self._run_adb(
                ["connect", self._serial()], timeout=_ADB_TIMEOUT_S, _locked=True,
            )
            if connect_result is None:
                self._last_error = "adb connect timeout / not found"
                await self._update_conn_state(TVConnectionState.DISCONNECTED)
                return False
            # ``adb connect`` returns 0 with stdout containing either
            # "connected to" / "already connected to" on success, or
            # "failed to connect" / "cannot connect" on failure.
            if "connected" not in connect_result.lower():
                self._last_error = connect_result.strip()[:200]
                self._log.info("ADB connect failed: %s", self._last_error)
                await self._update_conn_state(TVConnectionState.DISCONNECTED)
                return False

            # Verify with get-state — confirms the daemon accepts us.
            state_result = await self._run_adb(
                ["-s", self._serial(), "get-state"],
                timeout=3.0, _locked=True,
            )
            if state_result is None or state_result.strip() != "device":
                self._last_error = (
                    f"adb get-state returned {state_result!r}"
                    if state_result is not None
                    else "adb get-state timeout"
                )
                self._log.info("ADB get-state failed: %s", self._last_error)
                await self._update_conn_state(TVConnectionState.DISCONNECTED)
                return False

            self._last_error = None
            self._retry_count = 0
            await self._update_conn_state(TVConnectionState.CONNECTED)
            return True

    # =========================================================================
    # Power polling — hysteresis-debounced (Samsung-client pattern verbatim)
    # =========================================================================

    async def poll_power(self) -> TVPowerState:
        """
        Query the TV's display power state, then apply ON → OFF hysteresis.

        Observation rules:
            - ADB connected AND dumpsys reports Display Power state=ON
              → observation = ON
            - All other cases (not connected, dumpsys returns OFF/DOZE,
              dumpsys timeout, unparseable output) → observation = OFF

        Hysteresis rules (the whole point of this method):
            - Observation == ON  → reset _off_streak; transition to ON
              immediately.
            - Observation == OFF → _off_streak += 1. Only transition to
              OFF after _OFF_STREAK_THRESHOLD consecutive non-ON
              observations.

        Returns the debounced state (not the raw observation; raw is
        exposed via ``self._last_observation`` for debugging).
        """
        observation, reason = await self._observe_power()
        self._last_observation = observation

        if observation == TVPowerState.ON:
            if self._off_streak:
                self._log.debug(
                    "ON observation cleared _off_streak (was %d)", self._off_streak
                )
            self._off_streak = 0
            await self._update_power_state(TVPowerState.ON)
            return TVPowerState.ON

        # observation == OFF
        self._off_streak += 1

        if self._power_state == TVPowerState.OFF:
            return TVPowerState.OFF

        if self._power_state == TVPowerState.UNKNOWN:
            self._log.info(
                "First OFF observation from UNKNOWN startup state (%s)", reason
            )
            await self._update_power_state(TVPowerState.OFF)
            return TVPowerState.OFF

        if self._off_streak < _OFF_STREAK_THRESHOLD:
            self._log.info(
                "Suppressing ON→OFF transition (streak %d/%d, reason=%s) — "
                "TV may have hiccuped; holding state ON",
                self._off_streak, _OFF_STREAK_THRESHOLD, reason,
            )
            return TVPowerState.ON

        self._log.info(
            "ON→OFF committed after %d consecutive OFF observations (latest=%s)",
            self._off_streak, reason,
        )
        await self._update_power_state(TVPowerState.OFF)
        return TVPowerState.OFF

    async def _observe_power(self) -> tuple[TVPowerState, str]:
        """
        Single raw observation. Returns ``(observation, reason)``.

        Tries ``dumpsys power | grep "Display Power"`` once; retries
        once on subprocess failure before declaring OFF (mirrors the
        Samsung ``_HTTP_POLL_RETRIES`` pattern).
        """
        if self._conn_state != TVConnectionState.CONNECTED:
            if not await self._try_connect():
                return TVPowerState.OFF, "adb_not_connected"

        for attempt in range(1 + _ADB_POLL_RETRIES):
            output = await self._run_adb(
                ["-s", self._serial(), "shell",
                 "dumpsys power | grep 'Display Power'"],
                timeout=_ADB_TIMEOUT_S,
            )
            if output is not None:
                # Expected line shape: ``  Display Power: state=ON``
                # Possible values: ON, OFF, DOZE, DOZE_SUSPEND, VR
                upper = output.upper()
                if "STATE=ON" in upper:
                    return TVPowerState.ON, "state=ON"
                if "STATE=OFF" in upper:
                    return TVPowerState.OFF, "state=OFF"
                if "STATE=DOZE" in upper:
                    # Doze = screen off but device awake (ambient mode).
                    # Treat as OFF for the "is the user watching TV" signal.
                    return TVPowerState.OFF, "state=DOZE"
                # Output present but unparseable — count as observation OFF.
                return TVPowerState.OFF, f"unparseable:{output.strip()[:60]!r}"

            if attempt < _ADB_POLL_RETRIES:
                await asyncio.sleep(_ADB_POLL_RETRY_GAP_S)

        # All attempts failed → mark disconnected so next cycle reconnects.
        await self._update_conn_state(TVConnectionState.DISCONNECTED)
        return TVPowerState.OFF, "dumpsys_failed"

    async def _update_power_state(self, new_state: TVPowerState) -> None:
        """
        Set cached power state; fire ``on_power_change`` only on actual change.
        """
        if new_state == self._power_state:
            return
        self._log.info(
            "Power state: %s → %s", self._power_state.value, new_state.value
        )
        self._power_state = new_state
        if self._on_power_change:
            try:
                result = self._on_power_change(new_state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                self._log.error("on_power_change callback raised: %s", exc)

    async def _update_conn_state(self, new_state: TVConnectionState) -> None:
        """Set cached conn state; fire ``on_conn_change`` only on change."""
        if new_state == self._conn_state:
            return
        self._log.info(
            "Conn state: %s → %s", self._conn_state.value, new_state.value
        )
        self._conn_state = new_state
        if self._on_conn_change:
            try:
                result = self._on_conn_change(new_state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                self._log.error("on_conn_change callback raised: %s", exc)

    # =========================================================================
    # Background tasks
    # =========================================================================

    async def _poll_loop(self) -> None:
        """Periodically poll display power state. Quiet on stop()."""
        while not self._stop_event.is_set():
            try:
                await self.poll_power()
            except Exception as exc:
                self._log.error("poll_loop error: %s", exc, exc_info=True)
            for _ in range(self.config.poll_interval):
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(1)
        self._log.info("Poll loop exiting")

    async def _post_wol_poll(self) -> None:
        """
        Fast post-WoL poll (every 2 s, max 30 s) to detect TV boot quickly
        without waiting for the next regular ``poll_interval`` tick. Stops
        as soon as the TV responds with ON.
        """
        self._log.info("Post-WoL polling started (every 2s, max 30s)")
        for attempt in range(15):
            if self._stop_event.is_set():
                break
            await asyncio.sleep(2)
            state = await self.poll_power()
            self._log.debug("Post-WoL poll %d/15: TV is %s", attempt + 1, state.value)
            if state == TVPowerState.ON:
                self._log.info(
                    "TV responded after WoL on attempt %d", attempt + 1
                )
                return
        self._log.info("Post-WoL poll window expired (30s)")

    # =========================================================================
    # ADB subprocess helper
    # =========================================================================

    async def _run_adb(
        self,
        argv: List[str],
        *,
        timeout: float = _ADB_TIMEOUT_S,
        _locked: bool = False,
    ) -> Optional[str]:
        """
        Run ``adb <argv...>`` and return stdout (decoded, stripped) on
        success, or None on any failure (timeout, nonzero exit, subprocess
        error). All exceptions are caught — the caller treats None as
        "this didn't work, decide what to do."

        ``_locked``: internal flag. When True, the caller already holds
        ``self._adb_lock`` and we skip re-acquiring it (used by
        ``_try_connect`` which holds the lock across the connect+verify
        sequence).
        """
        cmd = [self.config.adb_path, *argv]
        try:
            async def _go():
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise
                if proc.returncode != 0:
                    self._log.debug(
                        "adb %s exited %d: stderr=%s",
                        " ".join(argv), proc.returncode,
                        stderr.decode("utf-8", errors="replace").strip()[:200],
                    )
                    return None
                return stdout.decode("utf-8", errors="replace").strip()

            if _locked:
                return await _go()
            async with self._adb_lock:
                return await _go()
        except asyncio.TimeoutError:
            self._log.debug("adb %s timed out after %.1fs", " ".join(argv), timeout)
            return None
        except FileNotFoundError:
            self._log.error("adb binary not found at %r — install android-tools-adb in container", self.config.adb_path)
            return None
        except Exception as exc:
            self._log.debug("adb %s failed: %s", " ".join(argv), exc)
            return None


# =============================================================================
# Module-level helpers
# =============================================================================


def _send_wol_sync(mac: str, tv_ip: str) -> None:
    """
    Send a Wake-on-LAN magic packet. Same shape as the Samsung helper.

    Two destinations × 3 repeats with 200 ms gaps:
        255.255.255.255:9  — LAN broadcast
        <tv_ip>:9          — unicast (Docker-bridge safe — limited
                             broadcast is confined to docker0 otherwise)
    """
    mac_bytes    = bytes.fromhex(mac)
    magic_packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            s.sendto(magic_packet, ("255.255.255.255", 9))
            s.sendto(magic_packet, (tv_ip, 9))
            time.sleep(0.2)


# =============================================================================
# Singleton registry — one client per TV IP (matches Samsung client)
# =============================================================================

_clients: Dict[str, "HisenseTVClient"] = {}


def get_tv_client(
    tv_ip:           str  = "<LAN_IP>",
    mac_address:     str  = "",
    name:            str  = "hisense_tv",
    on_power_change: Optional[StateChangeCallback] = None,
    on_conn_change:  Optional[StateChangeCallback] = None,
) -> "HisenseTVClient":
    """
    Get or create a singleton ``HisenseTVClient`` keyed by ``tv_ip``.

    Multiple calls with the same ``tv_ip`` return the same instance —
    prevents duplicate ADB connections and duplicate poll loops.
    """
    if tv_ip not in _clients:
        cfg = HisenseTVConfig(tv_ip=tv_ip, mac_address=mac_address, name=name)
        _clients[tv_ip] = HisenseTVClient(
            cfg,
            on_power_change=on_power_change,
            on_conn_change=on_conn_change,
        )
        logger.info("Created HisenseTVClient for %s (%s)", name, tv_ip)
    return _clients[tv_ip]
