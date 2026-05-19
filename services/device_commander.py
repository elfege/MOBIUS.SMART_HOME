"""
Device Commander Service

Centralized module for ALL device command execution. Every command to a
Hubitat (or Matter) device goes through this module — nothing else should
call hubitat_client.send_command() directly.

Key features:
- Threaded execution: every command runs in a ThreadPoolExecutor thread
- Nested retries:
    INNER  (network):    Handled by hubitat_client._make_request() (3 retries)
    MIDDLE (verify):     Send command → poll device state → retry if mismatch
    OUTER  (operation):  If verify fails after all middle retries, re-send + re-verify
- State verification: polls the Hubitat Maker API after command to confirm
  the device actually changed state (not just that HTTP 200 was returned)
- "Updating" status: tracked in-memory (thread-safe) and in PostgREST
  so the UI can show device status and app logic can avoid duplicate commands
- Configurable timeouts via environment variables

Environment variables:
    DEVICE_CMD_TIMEOUT          Overall timeout per command (default: 30s)
    DEVICE_CMD_VERIFY_RETRIES   Verification polls per send attempt (default: 3)
    DEVICE_CMD_VERIFY_DELAY     Seconds between verification polls (default: 1.0)
    DEVICE_CMD_OPERATION_RETRIES Full send+verify cycles (default: 2)
    DEVICE_CMD_OPERATION_DELAY  Seconds between operation retries (default: 2.0)
"""

import os
import time
import asyncio
import logging
import traceback
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    """UTC ISO-8601 timestamp PostgREST accepts as TIMESTAMPTZ."""
    return datetime.now(timezone.utc).isoformat()

from models.command import (
    CommandResult,
    CommandStatus,
    resolve_expected_state,
)
from services.hubitat_client import HubitatClient


# Module-level logger
logger = logging.getLogger(__name__)

# ANSI color for device names — bright cyan stands out in terminal/docker logs
_C = "\033[96m"   # bright cyan
_R = "\033[0m"    # reset


def extract_attribute(
    device_data: Dict[str, Any],
    attribute_name: str
) -> Optional[str]:
    """
    Extract an attribute value from device data.

    Handles both Hubitat Maker API list format and cache dict format:
      - API:   {"attributes": [{"name": "switch", "currentValue": "on"}, ...]}
      - Cache: {"attributes": {"switch": "on", ...}}

    Args:
        device_data: Device response from get_device() or cache
        attribute_name: Attribute to extract (e.g., 'switch', 'level')

    Returns:
        String value of the attribute, or None if not found
    """
    attrs = device_data.get("attributes", [])
    if isinstance(attrs, list):
        # Hubitat Maker API list format
        for attr in attrs:
            if attr.get("name") == attribute_name:
                val = attr.get("currentValue")
                return str(val) if val is not None else None
    elif isinstance(attrs, dict):
        # Cache dict format
        val = attrs.get(attribute_name)
        return str(val) if val is not None else None
    return None


class DeviceCommander:
    """
    Centralized device command execution with threaded dispatch,
    nested retries, and state verification.

    All device commands in the system go through this module.
    Nothing else should call hubitat_client.send_command() directly.

    Threading model:
        Every command execution runs in a ThreadPoolExecutor thread.
        This keeps the asyncio event loop (FastAPI/uvicorn) unblocked
        during blocking requests.get() calls and verification polling.

    Retry levels:
        INNER (network):    Handled by hubitat_client._make_request() — 3 retries
        MIDDLE (verify):    Send command, poll device state, retry if mismatch
        OUTER (operation):  If middle fails after all attempts, retry full send+verify
    """

    def __init__(
        self,
        hubitat_client: HubitatClient,
        executor: Optional[ThreadPoolExecutor] = None,
        verify_retries: Optional[int] = None,
        verify_delay: Optional[float] = None,
        operation_retries: Optional[int] = None,
        operation_delay: Optional[float] = None,
        command_timeout: Optional[float] = None,
    ):
        """
        Initialize the DeviceCommander.

        All retry/timeout parameters fall back to environment variables,
        then to hardcoded defaults.

        Args:
            hubitat_client: Low-level Hubitat API client
            executor: Optional ThreadPoolExecutor (created if not provided)
            verify_retries: Polls per send attempt (env: DEVICE_CMD_VERIFY_RETRIES, default: 3)
            verify_delay: Seconds between polls (env: DEVICE_CMD_VERIFY_DELAY, default: 1.0)
            operation_retries: Full send+verify cycles (env: DEVICE_CMD_OPERATION_RETRIES, default: 2)
            operation_delay: Seconds between cycles (env: DEVICE_CMD_OPERATION_DELAY, default: 2.0)
            command_timeout: Overall timeout (env: DEVICE_CMD_TIMEOUT, default: 30)
        """
        self._client = hubitat_client
        self._executor = executor or ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="device_cmd"
        )

        # Retry configuration — env vars override constructor args override defaults
        self.verify_retries = verify_retries or int(
            os.environ.get('DEVICE_CMD_VERIFY_RETRIES', '3')
        )
        self.verify_delay = verify_delay or float(
            os.environ.get('DEVICE_CMD_VERIFY_DELAY', '1.0')
        )
        self.operation_retries = operation_retries or int(
            os.environ.get('DEVICE_CMD_OPERATION_RETRIES', '2')
        )
        self.operation_delay = operation_delay or float(
            os.environ.get('DEVICE_CMD_OPERATION_DELAY', '2.0')
        )
        self.command_timeout = command_timeout or float(
            os.environ.get('DEVICE_CMD_TIMEOUT', '30')
        )

        # Thread-safe device status tracking
        self._device_status: Dict[str, CommandStatus] = {}
        self._status_lock = threading.Lock()

        # Whether to log every command to the `device_commands` table.
        # Default on; set DEVICE_COMMANDS_LOGGING=false to disable if writes
        # become a bottleneck or PostgREST is misbehaving.
        self._db_logging_enabled = (
            os.environ.get('DEVICE_COMMANDS_LOGGING', 'true').strip().lower()
            == 'true'
        )
        self._postgrest_url = os.environ.get(
            'POSTGREST_URL', 'http://postgrest:3001'
        )
        # Reusable session so DB writes don't pay TCP handshake per command.
        # (requests.Session is thread-safe for separate request() calls.)
        import requests as _req
        self._db_http = _req.Session()

        logger.info(
            f"DeviceCommander initialized: "
            f"timeout={self.command_timeout}s, "
            f"verify_retries={self.verify_retries}, "
            f"verify_delay={self.verify_delay}s, "
            f"operation_retries={self.operation_retries}, "
            f"operation_delay={self.operation_delay}s"
        )

    # =========================================================================
    # Public API
    # =========================================================================

    async def send_command(
        self,
        device_id: str,
        command: str,
        args: Optional[List] = None,
        verify: bool = True,
        device_name: str = "",
        instance_id: Optional[int] = None,
    ) -> CommandResult:
        """
        Send a command to a device asynchronously (dispatched to thread).

        Dual-command strategy (Matter-first when commissioned):
        1. If device has a Matter mapping → fire Matter immediately (async,
           non-blocking). Matter is lower-latency than Hubitat cloud relay.
        2. Fire Hubitat command (full retry + verify cycle in thread).
           Hubitat verification sees the actual device state — whether
           it was changed by Matter or by the Hubitat command itself.
        3. Memo updates are gated on Hubitat verification result, so
           the memoization system stays consistent regardless of which
           protocol changed the device.

        If the device is NOT commissioned to Matter, only Hubitat fires.

        Args:
            device_id: Hubitat device ID
            command: Command name (on, off, setLevel, setColorTemperature, etc.)
            args: Optional command arguments (e.g., [75] for setLevel)
            verify: Whether to poll device state after command (default: True)
            device_name: Human-readable name for logging context

        Returns:
            CommandResult with success, verified, actual_state, timing, etc.
        """
        # 1. Fire Matter first (async, non-blocking) — fastest path
        #    Set UPDATING status early so _handle_switch() knows a command
        #    is in-flight and won't interpret the state change as a manual
        #    override. See docs/dual_command_flow.html for full flow diagram.
        try:
            self._fire_matter_command(device_id, command, args)
        except Exception as e:
            logger.debug(
                f"Matter pre-dispatch failed for device {device_id}: {e}"
            )

        # 2. Fire Hubitat command (full retry + verify cycle)
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._execute_command_sync,
                device_id, command, args, verify, device_name, instance_id,
            )
        except Exception as e:
            logger.error(
                f"send_command async dispatch failed: "
                f"device={device_id}, cmd={command}: {e}",
                exc_info=True
            )
            result = CommandResult(
                device_id=device_id,
                device_name=device_name,
                command=command,
                args=args,
                error=str(e),
                traceback_str=traceback.format_exc(),
                status=CommandStatus.FAILED,
            )

        return result

    def send_command_sync(
        self,
        device_id: str,
        command: str,
        args: Optional[List] = None,
        verify: bool = True,
        device_name: str = "",
        instance_id: Optional[int] = None,
    ) -> CommandResult:
        """
        Send a command to a device synchronously.

        Dual-command strategy (Matter-first when commissioned):
        1. If device has a Matter mapping → fire Matter immediately (async,
           non-blocking). Matter is lower-latency than Hubitat cloud relay.
        2. Fire Hubitat command (full retry + verify cycle in thread).
           Hubitat verification sees the actual device state — whether
           it was changed by Matter or by the Hubitat command itself.
        3. Memo updates are gated on Hubitat verification result, so
           the memoization system stays consistent regardless of which
           protocol changed the device.

        If the device is NOT commissioned to Matter, only Hubitat fires.

        For callers not in an async context (scheduler callbacks, BaseApp
        methods). Submits to the ThreadPoolExecutor and blocks on the result.

        Args:
            device_id: Hubitat device ID
            command: Command name
            args: Optional command arguments
            verify: Whether to verify device state after command
            device_name: Human-readable name for logging

        Returns:
            CommandResult with full execution details
        """
        # 1. Fire Matter first (best-effort, non-blocking) — fastest path
        #    _fire_matter_command uses create_task internally; if no event
        #    loop is available (pure-thread context), it logs and skips.
        try:
            self._fire_matter_command(device_id, command, args)
        except Exception as e:
            logger.debug(
                f"Matter pre-dispatch failed for device {device_id}: {e}"
            )

        # 2. Fire Hubitat command (full retry + verify cycle)
        try:
            future = self._executor.submit(
                self._execute_command_sync,
                device_id, command, args, verify, device_name, instance_id,
            )
            result = future.result(timeout=self.command_timeout)
        except Exception as e:
            logger.error(
                f"send_command_sync failed: "
                f"device={device_id}, cmd={command}: {e}",
                exc_info=True
            )
            result = CommandResult(
                device_id=device_id,
                device_name=device_name,
                command=command,
                args=args,
                error=str(e),
                traceback_str=traceback.format_exc(),
                status=CommandStatus.TIMEOUT
                if "TimeoutError" in str(type(e).__name__)
                else CommandStatus.FAILED,
            )

        return result

    def get_device_status(self, device_id: str) -> CommandStatus:
        """
        Get the current command status of a device.

        Thread-safe read from the in-memory status dict.

        Args:
            device_id: Hubitat device ID

        Returns:
            Current CommandStatus (IDLE if no command has been sent)
        """
        with self._status_lock:
            return self._device_status.get(device_id, CommandStatus.IDLE)

    # =========================================================================
    # Two-phase command logging (device_commands table)
    # =========================================================================

    def _log_command_issued(
        self,
        canonical_device_id: str,
        hubitat_device_id: str,
        hub_name: str,
        command: str,
        args: Optional[List],
        instance_id: Optional[int] = None,
    ) -> Optional[int]:
        """
        Insert a 'pending' device_commands row at command issue time.

        Returns the inserted row id (used by _log_command_completed to
        UPDATE the same row), or None if logging is disabled or failed.
        Errors are swallowed — DB logging must never block command execution.
        """
        if not self._db_logging_enabled:
            return None
        try:
            # canonical_device_id may be a stringified int (post-Phase-5)
            # or a Hubitat per-hub id (transitional fallback). Only insert
            # canonical_device_id into the FK column if it parses cleanly
            # AND looks like a small int (canonical PKs in this DB are 1-1000ish).
            canonical_fk = None
            try:
                cid = int(canonical_device_id)
                if 0 < cid < 100000:
                    canonical_fk = cid
            except (ValueError, TypeError):
                pass

            # Look up hub_ip from hub_name (cheap; one-off per command).
            hub_ip = self._hub_name_to_ip(hub_name)

            payload = {
                'canonical_device_id': canonical_fk,
                'hubitat_device_id': str(hubitat_device_id),
                'hub_ip': hub_ip,
                'command': command,
                'arguments': args or [],
                'attempt': 1,
                'max_attempts': 1,
                'outcome': 'pending',
            }
            # Thread the instance_id through so "who issued this command?"
            # is answerable from the DB. NULL means "didn't pass one"
            # (e.g., a direct curl /send-command call without context).
            if instance_id is not None:
                payload['instance_id'] = int(instance_id)
            r = self._db_http.post(
                f'{self._postgrest_url}/device_commands',
                json=payload,
                headers={
                    'Content-Type': 'application/json',
                    'Prefer': 'return=representation',
                },
                timeout=3,
            )
            if r.status_code in (200, 201):
                body = r.json()
                if isinstance(body, list) and body:
                    return body[0].get('id')
                if isinstance(body, dict):
                    return body.get('id')
        except Exception as e:
            logger.debug(f'device_commands insert failed: {e}')
        return None

    def _log_command_completed(
        self,
        command_log_id: Optional[int],
        result: CommandResult,
    ) -> None:
        """UPDATE the 'pending' row with the final outcome from CommandResult."""
        if not self._db_logging_enabled or command_log_id is None:
            return
        try:
            # Map CommandStatus → device_commands.outcome enum.
            status = result.status
            if status == CommandStatus.VERIFIED:
                outcome = 'confirmed'
            elif status == CommandStatus.TIMEOUT:
                outcome = 'failed_timeout'
            elif status == CommandStatus.FAILED:
                # If verification was attempted (we have expected_state),
                # it's a verify failure; otherwise a network/send failure.
                outcome = (
                    'failed_verify' if result.expected_state is not None
                    else 'failed_network'
                )
            else:
                # IDLE/UPDATING shouldn't appear here, but cover them.
                outcome = 'confirmed' if result.success else 'failed_network'

            payload = {
                'outcome': outcome,
                'completed_at': _now_iso(),
                'final_observed_value': (
                    str(result.actual_state) if result.actual_state is not None
                    else None
                ),
                'verify_retries_used': result.retries_used.get('verify')
                    if isinstance(result.retries_used, dict) else None,
                'latency_ms': int(result.elapsed_ms or 0),
                'error': result.error,
            }
            self._db_http.patch(
                f'{self._postgrest_url}/device_commands',
                params={'id': f'eq.{command_log_id}'},
                json=payload,
                headers={
                    'Content-Type': 'application/json',
                    'Prefer': 'return=minimal',
                },
                timeout=3,
            )
        except Exception as e:
            logger.debug(f'device_commands update failed: {e}')

    def _hub_name_to_ip(self, hub_name: str) -> Optional[str]:
        """Resolve hub_name → hub_ip via hub_config. Cached per process."""
        if not hub_name or hub_name == 'default':
            return None
        if not hasattr(self, '_hub_ip_cache'):
            self._hub_ip_cache: Dict[str, str] = {}
        if hub_name in self._hub_ip_cache:
            return self._hub_ip_cache[hub_name]
        try:
            r = self._db_http.get(
                f'{self._postgrest_url}/hub_config',
                params={
                    'hub_name': f'eq.{hub_name}',
                    'select': 'hub_ip',
                },
                timeout=3,
            )
            rows = r.json() if r.status_code == 200 else []
            ip = rows[0]['hub_ip'] if rows else None
            if ip:
                self._hub_ip_cache[hub_name] = ip
            return ip
        except Exception:
            return None

    # =========================================================================
    # Core Execution (runs in thread)
    # =========================================================================

    def _resolve_native_hub(
        self,
        device_id: str,
        device_name: str,
    ) -> tuple:
        """
        Resolve a CANONICAL devices.id to (client, hubitat_id, hub_name).

        Post-Phase-5: app code passes canonical PKs. We look up the row in
        the canonical `devices` table (joined against editable `hub_config`
        for IP/name) and translate to the per-hub Hubitat id we need to
        talk to that hub's Maker API. As a safety net for any caller that
        still passes a Hubitat id, we also try get_hub_for_device(hubitat_id).

        Args:
            device_id: Canonical devices.id PK (preferred) or, transitionally,
                       a Hubitat per-hub id.
            device_name: Human-readable label for log context

        Returns:
            (HubitatClient, hubitat_id_to_send, hub_name)
            Falls back to (default_client, device_id, 'default') if unresolvable.
        """
        try:
            from services.hub_classifier import (
                get_device_by_canonical_id, get_hub_for_device,
            )
            from services.hubitat_client import get_hub_client_by_ip

            # Preferred path: canonical id lookup
            row = get_device_by_canonical_id(device_id)
            if not row or not row.get("hub_ip"):
                # Transitional fallback: maybe caller still hands a hubitat id
                row = get_hub_for_device(device_id)

            if row and row.get("hub_ip"):
                hub_ip = row["hub_ip"]
                client = get_hub_client_by_ip(hub_ip)
                if client:
                    hubitat_id = str(row.get("hubitat_id") or device_id)
                    if client is not self._client:
                        logger.info(
                            f"[Route] {_C}{device_name}{_R} → "
                            f"hub {row.get('hub_name') or hub_ip} "
                            f"({hub_ip}) canon={row.get('id')} "
                            f"hubitat_id={hubitat_id} "
                            f"label={row.get('label')!r}"
                        )
                    return (client, hubitat_id, row.get("hub_name") or hub_ip)
        except Exception as e:
            logger.debug(f"DB-backed hub lookup failed for {device_id}: {e}")

        # Fallback: default client with original id (Hubitat will 404 loudly
        # if it doesn't recognize the id — that's the correct failure mode).
        return (self._client, device_id, "default")

    def _execute_command_sync(
        self,
        device_id: str,
        command: str,
        args: Optional[List],
        verify: bool,
        device_name: str,
        instance_id: Optional[int] = None,
    ) -> CommandResult:
        """
        Synchronous command execution with nested retries and native-hub routing.

        Runs in a ThreadPoolExecutor thread. This is the heart of the
        command execution pipeline:

        NATIVE HUB ROUTING:
            Before sending, resolves which hub physically owns the device
            via device_hub_mapping. Sends command to the native hub's
            Maker API directly (bypasses Hub Mesh relay for lower latency).
            Falls back to MAIN hub if no mapping exists.

        OUTER LOOP (operation retries):
            Send command to hub (INNER retries in _make_request)
            MIDDLE LOOP (verification retries):
                Poll device state from hub (live, not cached)
                If state matches expected → return VERIFIED
                Sleep verify_delay
            Sleep operation_delay, re-send

        Args:
            device_id: Hubitat device ID (as known on MAIN hub)
            command: Command name
            args: Optional arguments
            verify: Whether to verify state after command
            device_name: For logging

        Returns:
            CommandResult with full execution details
        """
        # Resolve native hub — may remap device_id and client
        effective_client, effective_device_id, hub_name = (
            self._resolve_native_hub(device_id, device_name)
        )

        result = CommandResult(
            device_id=device_id,
            device_name=device_name or device_id,
            command=command,
            args=args,
        )
        start_time = time.monotonic()

        # Two-phase logging — record the intent before firing so even a
        # crash mid-execution leaves a 'pending' row that the watchdog
        # can flag later. canonical_device_id == device_id (post-Phase-5
        # device_selections store canonical PKs); effective_device_id is
        # the per-hub native id used in the actual Hubitat HTTP call.
        command_log_id = self._log_command_issued(
            canonical_device_id=device_id,
            hubitat_device_id=effective_device_id,
            hub_name=hub_name,
            command=command,
            args=args,
            instance_id=instance_id,
        )

        # Resolve expected state for verification
        expected = resolve_expected_state(command, args) if verify else None

        # Set device status to UPDATING
        self._set_device_status(device_id, CommandStatus.UPDATING)

        hub_tag = f" @{hub_name}" if hub_name and hub_name != "default" else ""
        log_prefix = (
            f"[Cmd] {_C}{device_name or device_id}{_R} "
            f"({effective_device_id}/{command}"
            f"{'/' + str(args) if args else ''}{hub_tag})"
        )

        try:
            for outer in range(self.operation_retries):
                result.retries_used['outer'] = outer

                # ----- SEND COMMAND -----
                # Backend selection:
                #   maker_api_enabled=True  → Maker API via hubitat_client
                #   maker_api_enabled=False → admin API via hubitat_admin_client
                # No silent fallback. If the user disabled Maker, they're
                # testing the new path; failures surface as failures.
                use_admin_for_commands = False
                try:
                    from services.settings_resolver import get_resolver
                    maker_on = get_resolver().get_system('maker_api_enabled', True)
                    use_admin_for_commands = (maker_on is False)
                except Exception:
                    pass

                try:
                    if use_admin_for_commands:
                        # Pick the right hub for this device. hub_name comes
                        # from _resolve_native_hub above; admin client is
                        # per-hub-IP. Look up hub_ip from hub_config.
                        hub_ip = self._hub_name_to_ip(hub_name) or '<LAN_IP>'
                        from services.hubitat_admin_client import get_client
                        admin = get_client(hub_ip, hub_name or 'default')
                        # admin.send_command expects (device_id, command, argument)
                        # where argument is a single value. args is a list;
                        # if non-empty, use the first element.
                        arg = args[0] if args else None
                        arg_str = str(arg) if arg is not None else None
                        send_ok = admin.send_command(
                            int(effective_device_id), command, arg_str,
                        )
                        if send_ok:
                            logger.info(
                                f"{log_prefix} sent via ADMIN API"
                            )
                    else:
                        send_ok = effective_client.send_command(
                            effective_device_id, command, args
                        )
                except Exception as e:
                    logger.error(
                        f"{log_prefix} send_command exception on attempt "
                        f"{outer + 1}/{self.operation_retries}: {e}",
                        exc_info=True
                    )
                    result.error = str(e)
                    result.traceback_str = traceback.format_exc()
                    if outer < self.operation_retries - 1:
                        time.sleep(self.operation_delay)
                    continue

                if not send_ok:
                    logger.warning(
                        f"{log_prefix} send_command returned False on attempt "
                        f"{outer + 1}/{self.operation_retries}"
                    )
                    result.error = (
                        f"send_command returned False for "
                        f"{device_id}/{command} on attempt {outer + 1}"
                    )
                    if outer < self.operation_retries - 1:
                        time.sleep(self.operation_delay)
                    continue

                # Command was accepted by the hub
                result.success = True

                # ----- VERIFY STATE -----
                if not verify or not expected:
                    # No verification requested or no expected state mapping
                    logger.info(
                        f"{log_prefix} sent OK (no verification)"
                    )
                    self._set_device_status(device_id, CommandStatus.IDLE)
                    break

                verified = False
                for mid in range(self.verify_retries):
                    result.retries_used['verify'] = mid

                    # Check overall timeout
                    elapsed = time.monotonic() - start_time
                    if elapsed >= self.command_timeout:
                        logger.warning(
                            f"{log_prefix} overall timeout "
                            f"({self.command_timeout}s) exceeded"
                        )
                        result.status = CommandStatus.TIMEOUT
                        result.error = (
                            f"Overall timeout ({self.command_timeout}s) "
                            f"exceeded after {elapsed:.1f}s"
                        )
                        self._set_device_status(
                            device_id, CommandStatus.TIMEOUT
                        )
                        result.elapsed_ms = elapsed * 1000
                        return result

                    try:
                        # LIVE API call — bypasses cache. Backend mirrors
                        # the send path: admin API when Maker is disabled,
                        # else Maker. Without this, 'disable Maker API'
                        # was a lie — sends went admin but verify-poll
                        # still hit Maker.
                        if use_admin_for_commands:
                            from services.hubitat_admin_client import (
                                get_client, to_maker_shape,
                            )
                            hub_ip = self._hub_name_to_ip(hub_name) or '<LAN_IP>'
                            admin = get_client(hub_ip, hub_name or 'default')
                            raw = admin.get_device(int(effective_device_id))
                            # /device/fullJson nests state under
                            # device.currentStates (dict, not list).
                            # to_maker_shape() handles the conversion;
                            # the previous inline shim read the wrong
                            # path and silently produced empty attrs.
                            device_data = to_maker_shape(raw)
                        else:
                            device_data = effective_client.get_device(
                                effective_device_id
                            )
                        if device_data:
                            actual = extract_attribute(
                                device_data, expected['attribute']
                            )
                            result.actual_state = actual
                            result.expected_state = expected['expected']

                            if actual == expected['expected']:
                                verified = True
                                logger.info(
                                    f"{log_prefix} VERIFIED: "
                                    f"{expected['attribute']}="
                                    f"{actual} (attempt "
                                    f"{outer + 1}.{mid + 1})"
                                )
                                break
                            else:
                                logger.debug(
                                    f"{log_prefix} verify poll "
                                    f"{mid + 1}/{self.verify_retries}: "
                                    f"expected={expected['expected']}, "
                                    f"actual={actual}"
                                )
                        else:
                            logger.warning(
                                f"{log_prefix} get_device returned None "
                                f"during verification poll {mid + 1}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"{log_prefix} verify poll exception: {e}",
                            exc_info=True
                        )

                    if mid < self.verify_retries - 1:
                        time.sleep(self.verify_delay)

                if verified:
                    result.verified = True
                    result.status = CommandStatus.VERIFIED
                    self._set_device_status(device_id, CommandStatus.VERIFIED)
                    # Sync verified state back to device cache so subsequent
                    # reads (e.g., _turn_on_switch skip check) see the real state
                    self._update_cache_after_verify(
                        device_id, expected['attribute'], result.actual_state
                    )
                    break
                else:
                    logger.warning(
                        f"{log_prefix} verification FAILED after "
                        f"{self.verify_retries} polls on operation attempt "
                        f"{outer + 1}/{self.operation_retries}: "
                        f"expected={expected['expected']}, "
                        f"actual={result.actual_state}"
                    )
                    if outer < self.operation_retries - 1:
                        logger.info(
                            f"{log_prefix} retrying full operation "
                            f"in {self.operation_delay}s..."
                        )
                        time.sleep(self.operation_delay)

            # After all outer retries exhausted
            if not result.verified and verify and expected:
                result.status = CommandStatus.FAILED
                result.error = (
                    f"Verification failed after "
                    f"{self.operation_retries} operation attempts: "
                    f"expected {expected['attribute']}="
                    f"{expected['expected']}, "
                    f"got {result.actual_state}"
                )
                self._set_device_status(device_id, CommandStatus.FAILED)
                logger.error(f"{log_prefix} {result.error}")

        except Exception as e:
            # Catch-all for unexpected errors in the execution pipeline
            logger.error(
                f"{log_prefix} unexpected error in execution pipeline: {e}",
                exc_info=True
            )
            result.error = str(e)
            result.traceback_str = traceback.format_exc()
            result.status = CommandStatus.FAILED
            self._set_device_status(device_id, CommandStatus.FAILED)

        result.elapsed_ms = (time.monotonic() - start_time) * 1000

        # Two-phase logging — completion update. Maps CommandStatus to the
        # device_commands.outcome enum:
        #   VERIFIED → confirmed
        #   TIMEOUT  → failed_timeout
        #   FAILED   → failed_verify (if verification ran) or failed_network
        self._log_command_completed(command_log_id, result)

        logger.debug(f"{log_prefix} completed: {result}")
        return result

    # =========================================================================
    # Status Management (thread-safe)
    # =========================================================================

    def _set_device_status(
        self,
        device_id: str,
        status: CommandStatus
    ) -> None:
        """
        Update device command status in-memory and in PostgREST.

        Both writes are wrapped in try/except — the in-memory update
        is authoritative; the database write is best-effort for UI visibility.

        Args:
            device_id: Hubitat device ID
            status: New CommandStatus
        """
        # In-memory (thread-safe)
        with self._status_lock:
            self._device_status[device_id] = status

        # Database (best-effort via PostgREST)
        try:
            from services.device_cache import get_default_cache
            cache = get_default_cache()
            cache.update_device_attribute(
                device_id, "command_status", status.value
            )
        except Exception as e:
            logger.warning(
                f"Failed to update command_status in DB for "
                f"device {device_id}: {e}",
                exc_info=True
            )

    def _update_cache_after_verify(
        self,
        device_id: str,
        attribute: str,
        value: str,
    ) -> None:
        """
        Write verified device state back to cache.

        After the commander confirms a device is in the expected state
        (e.g., switch=off), update the cache so that subsequent reads
        (like _turn_on_switch's skip-if-already-on check) see the real
        state instead of a stale value.

        Args:
            device_id: Hubitat device ID
            attribute: Attribute name (e.g., 'switch', 'level')
            value: Verified attribute value
        """
        try:
            from services.device_cache import get_default_cache
            cache = get_default_cache()
            cache.update_device_attribute(device_id, attribute, value)
        except Exception as e:
            logger.debug(
                f"Cache update after verify failed for "
                f"device {device_id}/{attribute}={value}: {e}"
            )

    # =========================================================================
    # Matter Integration
    # =========================================================================

    def _fire_matter_command(
        self,
        device_id: str,
        command: str,
        args: Optional[List]
    ) -> None:
        """
        Fire-and-forget Matter command for devices with a Matter mapping.

        Checks the device_matter_map table via PostgREST. If a mapping exists,
        translates the Hubitat command and sends via Matter WebSocket.

        This is best-effort: failures are logged with full traceback but never
        propagated to the caller.

        Args:
            device_id: Hubitat device ID
            command: Hubitat command name
            args: Hubitat command arguments
        """
        try:
            from services.matter_client import (
                get_matter_mapping,
                get_matter_client,
            )
            mapping = get_matter_mapping(device_id)
            if not mapping:
                return

            client = get_matter_client()
            try:
                loop = asyncio.get_running_loop()

                async def _matter_fire_and_forget():
                    """Wrapper that catches Matter exceptions so asyncio
                    doesn't log 'Task exception was never retrieved'."""
                    try:
                        await client.send_hubitat_command(
                            node_id=mapping['matter_node_id'],
                            endpoint_id=mapping['matter_endpoint_id'],
                            hubitat_command=command,
                            hubitat_args=args,
                        )
                    except Exception as exc:
                        logger.debug(
                            f"Matter dual-command failed for device "
                            f"{device_id} (node {mapping['matter_node_id']}): "
                            f"{exc}"
                        )

                loop.create_task(_matter_fire_and_forget())
                logger.debug(
                    f"Matter dual-command dispatched for device {device_id}: "
                    f"node={mapping['matter_node_id']}, cmd={command}"
                )
            except RuntimeError:
                # No running event loop (e.g., called from scheduler thread)
                logger.debug(
                    f"No event loop for Matter command on device {device_id}, "
                    f"skipping Matter dispatch"
                )
        except ImportError:
            # matter_client module not available — ignore silently
            pass
        except Exception as e:
            logger.warning(
                f"Matter dual-command failed for device {device_id}: {e}",
                exc_info=True
            )


# =========================================================================
# Global Singleton
# =========================================================================

_commander: Optional[DeviceCommander] = None
_commander_lock = threading.Lock()


def get_device_commander() -> DeviceCommander:
    """
    Get the global DeviceCommander singleton.

    Thread-safe lazy initialization. Creates a DeviceCommander with
    the default HubitatClient and a dedicated ThreadPoolExecutor.

    Returns:
        The global DeviceCommander instance
    """
    global _commander
    if _commander is None:
        with _commander_lock:
            # Double-check after acquiring lock
            if _commander is None:
                from services.hubitat_client import get_default_client
                client = get_default_client()
                _commander = DeviceCommander(hubitat_client=client)
                logger.info("Global DeviceCommander singleton created")
    return _commander
