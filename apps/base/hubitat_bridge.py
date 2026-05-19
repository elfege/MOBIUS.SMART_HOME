"""
Hubitat API integration bridge.

Lazy-loads HubitatClient on first use. Routes all device commands through
DeviceCommander, which handles:
  - Threaded execution (never blocks the asyncio event loop)
  - Nested retries with state verification
  - Matter dual-command dispatch (fire-and-forget)
  - Full traceback logging on errors
"""

import traceback
from typing import List, Optional, Dict, Any

from models.command import CommandResult


class HubitatMixin:
    """Mixin: Hubitat device queries and command dispatch."""

    @property
    def hubitat(self):
        """
        Get the DEFAULT Hubitat client (lazy). Use this only when you don't
        have a specific device id — for per-device queries use
        get_hubitat_for(device_id) so the call hits the hub that natively
        owns the id (the default client only knows MAIN-hub ids).
        """
        if self._hubitat is None:
            from services.hubitat_client import get_default_client
            self._hubitat = get_default_client()
        return self._hubitat

    def get_hubitat_for(self, hubitat_id):
        """
        DEPRECATED post-Phase-5: takes a Hubitat per-hub id and returns
        the right client. Kept only for paths that still hand around
        Hubitat ids. New code should use get_device_state_live() /
        get_device_events_live() with a canonical devices.id.
        """
        try:
            from services.hub_classifier import get_hub_for_device
            from services.hubitat_client import get_hub_client_by_ip
            row = get_hub_for_device(str(hubitat_id))
            if row and row.get("hub_ip"):
                client = get_hub_client_by_ip(row["hub_ip"])
                if client:
                    return client
        except Exception:
            pass
        return self.hubitat

    def _resolve_canonical(self, canonical_id):
        """
        Translate a canonical devices.id PK into (client, hubitat_id, hub_name).
        Returns (None, None, None) if the canonical id has no row or no
        reachable client.
        """
        try:
            from services.hub_classifier import get_device_by_canonical_id
            from services.hubitat_client import get_hub_client_by_ip
            row = get_device_by_canonical_id(canonical_id)
            if not row or not row.get("hub_ip"):
                return (None, None, None)
            client = get_hub_client_by_ip(row["hub_ip"])
            if not client:
                return (None, None, None)
            return (client, row["hubitat_id"], row.get("hub_name"))
        except Exception:
            return (None, None, None)

    def get_device_state_live(self, canonical_id):
        """
        Fetch live device state from the hub that natively owns this
        canonical device. Returns the Maker API device dict or None.

        Replaces the old `self.hubitat.get_device(hubitat_id)` pattern
        for app handlers that iterate device_selections (which now store
        canonical PKs).
        """
        client, hubitat_id, _ = self._resolve_canonical(canonical_id)
        if client is None:
            return None
        try:
            return client.get_device(hubitat_id)
        except Exception as e:
            self.logger.debug(
                f"get_device_state_live({canonical_id}) failed: {e}"
            )
            return None

    def get_device_events_live(self, canonical_id, max_events=20):
        """
        Fetch device event history for a canonical device id. Returns
        the Maker API events list or [].
        """
        client, hubitat_id, _ = self._resolve_canonical(canonical_id)
        if client is None:
            return []
        try:
            return client.get_device_events(hubitat_id, max_events=max_events)
        except Exception as e:
            self.logger.debug(
                f"get_device_events_live({canonical_id}) failed: {e}"
            )
            return []

    def send_command(
        self,
        device_id: str,
        command: str,
        args: List = None,
        verify: bool = True,
    ) -> CommandResult:
        """
        Send a command to a Hubitat device via DeviceCommander.

        Args:
            device_id: Hubitat device ID
            command: Command name (e.g., 'on', 'off', 'setLevel')
            args: Optional positional arguments for the command
            verify: Whether to verify device state after command (default: True)

        Returns:
            CommandResult with success, verified, actual_state, timing, etc.
        """
        from services.device_commander import get_device_commander
        try:
            commander = get_device_commander()
            device_name = self._get_device_display_name(device_id)
            # Thread the instance_id through so device_commands.instance_id
            # is populated. Without this, every command row is NULL in that
            # column and post-hoc "who turned the light off?" debugging is
            # impossible. self.instance_id is set on every running instance
            # by InstanceManager.
            return commander.send_command_sync(
                device_id=device_id,
                command=command,
                args=args,
                verify=verify,
                device_name=device_name,
                instance_id=getattr(self, 'instance_id', None),
            )
        except Exception as e:
            self.logger.error(
                f"send_command failed for device {device_id}, cmd={command}: {e}",
                exc_info=True
            )
            return CommandResult(
                device_id=device_id,
                command=command,
                args=args,
                error=str(e),
                traceback_str=traceback.format_exc(),
            )

    def _get_device_display_name(self, device_id: str) -> str:
        """
        Get human-readable device name for logging context.

        Tries cache first. Falls back to raw device_id if cache misses.
        """
        try:
            device = self.get_device_state(device_id)
            if device:
                return device.get('device_label', device.get('device_name', device_id))
        except Exception:
            pass
        return device_id

    def get_device_state(self, device_id) -> Optional[Dict[str, Any]]:
        """
        Get current device state from the local cache.

        Args:
            device_id: Canonical devices.id PK (post-Phase-5 the cache is
                       keyed on this exclusively — no translation needed)

        Returns:
            Device state dict or None if not cached
        """
        from services.device_cache import get_default_cache
        return get_default_cache().get_device(device_id)
# reload-hub-aware-bridge
