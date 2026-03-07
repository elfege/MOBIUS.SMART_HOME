"""
Settings and device selection accessors.

Read-only access to the instance's settings dict and device_selections.
Both are loaded from the database row at __init__ time.
"""

from typing import Any, List


class SettingsMixin:
    """Mixin: typed accessors for instance settings and device categories."""

    def get_setting(self, key: str, default: Any = None) -> Any:
        """
        Get a setting value.

        Args:
            key: Setting key as defined in the app's settings_schema
            default: Value to return if key is not set

        Returns:
            Setting value or default
        """
        return self.settings.get(key, default)

    def get_devices(self, category: str) -> List[str]:
        """
        Get device IDs for a category.

        Args:
            category: Device category key (e.g., 'motion_sensors', 'switches')

        Returns:
            List of Hubitat device ID strings
        """
        return self.device_selections.get(category, [])
