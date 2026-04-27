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

    def get_hubitat_for(self, device_id):
        """
        Resolve the Hubitat client for a given device_id by looking up
        which hub natively owns it in the canonical `devices` table
        (joined against `hub_config`). Falls back to the default client.
        """
        try:
            from services.hub_classifier import get_hub_for_device
            from services.hubitat_client import get_hub_client_by_ip
            row = get_hub_for_device(str(device_id))
            if row and row.get("hub_ip"):
                client = get_hub_client_by_ip(row["hub_ip"])
                if client:
                    return client
        except Exception:
            pass
        return self.hubitat

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
            return commander.send_command_sync(
                device_id=device_id,
                command=command,
                args=args,
                verify=verify,
                device_name=device_name,
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

    def get_device_state(self, device_id: str) -> Optional[Dict[str, Any]]:
        """
        Get current device state from the local device cache.

        The cache is kept fresh by DeviceCacheRefreshService (polls every ~2 min)
        and updated in real-time by verified DeviceCommander writes.

        Args:
            device_id: Hubitat device ID

        Returns:
            Device state dict or None if not cached
        """
        from services.device_cache import get_default_cache
        cache = get_default_cache()
        return cache.get_device(device_id)
# reload-hub-aware-bridge
