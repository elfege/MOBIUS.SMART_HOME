"""
Device name and state extraction helpers for keep-switch enforcement.

These utilities handle the two formats Hubitat returns device data in:
  - Live API format:  attributes as a list of {name, currentValue} dicts
  - Cache format:     attributes as a flat {attr_name: value} dict
"""

from typing import Dict, Any, Optional


class KeepSwitchHelpersMixin:
    """Mixin: device-data extraction helpers used by keep-switch enforcement."""

    @staticmethod
    def _extract_switch_state(device_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract switch on/off state from a Hubitat device response.

        Hubitat live API returns attributes as a list:
            [{"name": "switch", "currentValue": "on"}, ...]
        Device cache stores them as a dict:
            {"switch": "on", ...}
        This handles both formats transparently.

        Args:
            device_data: Device dict from live API or local cache

        Returns:
            'on', 'off', or None if the switch attribute is absent
        """
        attrs = device_data.get('attributes', {})
        if isinstance(attrs, list):
            for attr in attrs:
                if attr.get('name') == 'switch':
                    return attr.get('currentValue')
        elif isinstance(attrs, dict):
            return attrs.get('switch')
        return None

    @staticmethod
    def _extract_device_name(
        device_data: Dict[str, Any], fallback: str = ''
    ) -> str:
        """
        Extract the most human-readable name from a device dict.

        Tries label → device_label → name → device_name → fallback.
        Works for both live API responses and cache entries.

        Args:
            device_data: Device dict from live API or local cache
            fallback: Value to return if no name field is found

        Returns:
            Human-readable device name string
        """
        return (
            device_data.get('label')
            or device_data.get('device_label')
            or device_data.get('name')
            or device_data.get('device_name')
            or fallback
        )

    def _resolve_device_name(self, device_id: str) -> str:
        """
        Get device name from the local cache without hitting the live API.

        Used for logging context when a live API call is not needed.

        Args:
            device_id: Hubitat device ID

        Returns:
            Device label, name, or raw device_id as fallback
        """
        device = self.get_device_state(device_id)
        if device:
            return device.get('device_label', device.get('device_name', device_id))
        return device_id

    def _get_current_mode(self) -> Optional[str]:
        """
        Return the currently-active location mode, read from the
        `location_modes` DB table (populated by services.mode_poller
        which pulls /location/list/data from the primary hub every 60s).

        Replaced 2026-05-18: previously called `self.hubitat.get_modes()`
        which hit the Maker API. With `maker_api_enabled=false` (admin
        API primary), the Maker call returned None → every per-mode
        timeout / exclusionMode / keepOffMode check silently fell through
        and AML used the default `noMotionTime` regardless of actual mode.
        DB-as-source-of-truth keeps reads consistent across the system.

        Returns:
            Mode name string (e.g., 'Evening', 'Night', 'Away') or None
            on DB error / no row marked active.
        """
        try:
            import os
            import requests
            pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
            r = requests.get(
                f'{pg}/location_modes',
                params={
                    'is_active': 'eq.true',
                    'select': 'mode_name',
                    'limit': '1',
                },
                timeout=2,
            )
            if r.status_code == 200 and r.json():
                return r.json()[0].get('mode_name')
        except Exception as e:
            self.logger.warning(
                f"_get_current_mode (DB read) failed: {e}", exc_info=True
            )
        return None
