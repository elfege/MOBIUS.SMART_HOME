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
            from services.device_to_hubs_classifier import get_hub_for_device
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
            from services.device_to_hubs_classifier import get_device_by_canonical_id
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

    # =========================================================================
    # Authoritative live-state readers (event_log = source of truth)
    # =========================================================================
    #
    # WHY THESE EXIST — the recurring "lights won't turn on" regression class.
    #
    # get_device_state() above reads the `device_cache` TABLE, which is a
    # SECONDARY mirror written by webhook_router.update_device_attribute().
    # That mirror silently misses events (canon 104/290/291 froze at their
    # 2026-07-04 values while their real switch state kept changing via the
    # eventsocket). AML's on/off skip decision then trusts a stale 'on' and
    # sends no command. Same class of bug hit 2026-02-21 / 02-23 / 03-05 /
    # 06-19 / 07-05 — each patched on one path only.
    #
    # `event_log` is the eventsocket-populated SOURCE OF TRUTH (motion already
    # reads it, see MotionDetectionMixin). These readers make switch/level/etc.
    # state read the SAME source, so there is ONE source of truth for "what
    # state is this device in right now." device_cache is demoted to STATIC
    # metadata (name, label, capabilities) — which it holds reliably.

    def get_latest_attribute(self, canonical_id, attribute: str) -> Optional[str]:
        """
        Return the current value of `attribute` for a canonical device, read
        from `event_log` (the authoritative recent-events store), or None if
        the log has no such event yet.

        The most recent event_log row for (canonical_id, event_type=attribute)
        carries that attribute's CURRENT value — identical semantics to the
        per-sensor motion-state query in MotionDetectionMixin._is_motion_active.

        Args:
            canonical_id: Canonical devices.id PK.
            attribute:    Event/attribute name, e.g. 'switch', 'level'.

        Returns:
            The latest event_value as a string, or None on no-row / error.
        """
        import os
        import requests
        pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        try:
            r = requests.get(
                f"{pg}/event_log",
                params={
                    'canonical_device_id': f'eq.{canonical_id}',
                    'event_type': f'eq.{attribute}',
                    'select': 'event_value',
                    'order': 'received_at.desc',
                    'limit': '1',
                },
                timeout=3,
            )
            if r.status_code == 200 and r.json():
                return r.json()[0].get('event_value')
        except Exception as e:
            self.logger.warning(
                f"get_latest_attribute({canonical_id}, {attribute}) failed: {e}"
            )
        return None

    def get_switch_state(self, canonical_id) -> Optional[str]:
        """
        Return the authoritative current switch state ('on' / 'off' / None)
        for a canonical device, from `event_log`.

        Falls back to the device_cache mirror ONLY when event_log has no
        switch event for this device yet (cold cache / never-seen device),
        so a fresh install still behaves. Once any switch event has been
        logged, event_log is authoritative and the stale-mirror bug cannot
        recur for this device.

        Args:
            canonical_id: Canonical devices.id PK.

        Returns:
            'on', 'off', or None if unknown.
        """
        val = self.get_latest_attribute(canonical_id, 'switch')
        if val is not None:
            return val
        # Cold path: no switch event logged yet — fall back to cache metadata.
        device = self.get_device_state(canonical_id)
        if device:
            attrs = device.get('attributes') or {}
            if isinstance(attrs, dict):
                return attrs.get('switch')
        return None
# reload-hub-aware-bridge
