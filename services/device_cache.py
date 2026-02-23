"""
Device Cache Service

Caches Hubitat device states to reduce API polling. The cache is backed by
PostgreSQL (via PostgREST) for persistence across restarts.

Key features:
- TTL-based cache invalidation
- Event-driven cache updates (when webhooks arrive)
- Capability filtering for device picker UI
- Bulk operations for efficiency

The cache serves two purposes:
1. Reduce load on Hubitat hub by avoiding repeated API calls
2. Provide fast device lookups for the UI device picker
"""

import os
import logging
import traceback
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional


class DeviceCache:
    """
    Cache for Hubitat device states.

    Uses PostgREST as the backend storage, which provides:
    - Persistent storage across container restarts
    - SQL query capabilities for filtering
    - Automatic REST API from PostgreSQL schema

    Example usage:
        cache = DeviceCache(postgrest_url='http://postgrest:3001')

        # Update cache from Hubitat API response
        cache.update_all(devices_from_hubitat)

        # Get devices by capability
        motion_sensors = cache.get_devices_by_capability('motionSensor')

        # Update single device on webhook event
        cache.update_device_attribute('123', 'motion', 'active')
    """

    # Default cache TTL in seconds (5 minutes)
    DEFAULT_TTL = 300

    def __init__(
        self,
        postgrest_url: str = None,
        ttl_seconds: int = None
    ):
        """
        Initialize the device cache.

        Args:
            postgrest_url: URL to PostgREST service (defaults to env var)
            ttl_seconds: Cache TTL in seconds (defaults to 300)
        """
        self.postgrest_url = postgrest_url or os.environ.get(
            'POSTGREST_URL', 'http://postgrest:3001'
        )
        self.ttl_seconds = ttl_seconds or self.DEFAULT_TTL
        self.logger = logging.getLogger(__name__)

        # In-memory cache for fast reads (synced with database)
        self._memory_cache: Dict[str, Dict[str, Any]] = {}
        self._last_full_sync: Optional[datetime] = None

    # =========================================================================
    # Read Operations
    # =========================================================================

    def get_all(self) -> List[Dict[str, Any]]:
        """
        Get all cached devices.

        Returns cached data if TTL hasn't expired, otherwise returns empty
        list (caller should refresh from Hubitat).

        Returns:
            List of device dictionaries or empty list if cache expired
        """
        if self._is_cache_valid():
            return list(self._memory_cache.values())

        # Try to load from database
        devices = self._load_from_database()
        if devices:
            self._memory_cache = {d['hubitat_device_id']: d for d in devices}
            self._last_full_sync = datetime.now()
            return devices

        return []

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a single cached device.

        Args:
            device_id: Hubitat device ID

        Returns:
            Device dictionary or None if not cached
        """
        # Check memory cache first
        if device_id in self._memory_cache:
            return self._memory_cache[device_id]

        # Try database
        try:
            response = requests.get(
                f"{self.postgrest_url}/device_cache",
                params={"hubitat_device_id": f"eq.{device_id}"},
                timeout=5
            )
            if response.status_code == 200:
                devices = response.json()
                if devices:
                    device = devices[0]
                    self._memory_cache[device_id] = device
                    return device
        except Exception as e:
            self.logger.error(f"Failed to get device from cache: {e}", exc_info=True)

        return None

    def get_devices_by_capability(self, capability: str) -> List[Dict[str, Any]]:
        """
        Get devices with a specific capability.

        Uses PostgreSQL JSONB containment operator for efficient filtering.

        Args:
            capability: Capability name (e.g., 'motionSensor', 'switch')

        Returns:
            List of devices with the specified capability
        """
        # Try memory cache first
        if self._is_cache_valid():
            return [
                device for device in self._memory_cache.values()
                if capability in device.get('capabilities', [])
            ]

        # Query database with JSONB filter
        try:
            response = requests.get(
                f"{self.postgrest_url}/device_cache",
                params={"capabilities": f"cs.[\"{capability}\"]"},
                timeout=5
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            self.logger.error(f"Failed to filter devices by capability: {e}", exc_info=True)

        return []

    # =========================================================================
    # Write Operations
    # =========================================================================

    def update_all(self, devices: List[Dict[str, Any]]) -> bool:
        """
        Update cache with full device list from Hubitat.

        This is typically called after get_all_devices() from HubitatClient.
        Performs an upsert (insert or update) for each device.

        Args:
            devices: List of device dictionaries from Hubitat API

        Returns:
            True if update succeeded
        """
        if not devices:
            return True

        # Transform to database format
        cache_entries = []
        for device in devices:
            entry = {
                "hubitat_device_id": str(device.get('id')),
                "device_name": device.get('name'),
                "device_label": device.get('label'),
                "device_type": device.get('type'),
                "capabilities": device.get('capabilities', []),
                "attributes": self._extract_attributes(device),
                "last_synced_at": datetime.now().isoformat(),
                "sync_source": "api"
            }
            cache_entries.append(entry)

            # Update memory cache
            self._memory_cache[entry['hubitat_device_id']] = entry

        # Bulk upsert to database
        try:
            response = requests.post(
                f"{self.postgrest_url}/device_cache",
                json=cache_entries,
                headers={
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates"
                },
                timeout=30
            )

            if response.status_code in (200, 201):
                self._last_full_sync = datetime.now()
                self.logger.info(f"Updated cache with {len(devices)} devices")
                return True
            else:
                self.logger.error(f"Cache update failed: {response.text}")
                return False

        except Exception as e:
            self.logger.error(f"Failed to update device cache: {e}", exc_info=True)
            return False

    def update_device(self, device_id: str, device_data: Dict[str, Any]) -> bool:
        """
        Update a single device in the cache.

        Args:
            device_id: Hubitat device ID
            device_data: Full device data from Hubitat API

        Returns:
            True if update succeeded
        """
        entry = {
            "hubitat_device_id": str(device_id),
            "device_name": device_data.get('name'),
            "device_label": device_data.get('label'),
            "device_type": device_data.get('type'),
            "capabilities": device_data.get('capabilities', []),
            "attributes": self._extract_attributes(device_data),
            "last_synced_at": datetime.now().isoformat(),
            "sync_source": "api"
        }

        # Update memory cache
        self._memory_cache[str(device_id)] = entry

        # Update database
        try:
            response = requests.post(
                f"{self.postgrest_url}/device_cache",
                json=entry,
                headers={
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates"
                },
                timeout=5
            )
            return response.status_code in (200, 201)
        except Exception as e:
            self.logger.error(f"Failed to update device in cache: {e}", exc_info=True)
            return False

    def update_device_attribute(
        self,
        device_id: str,
        attribute_name: str,
        attribute_value: Any
    ) -> bool:
        """
        Update a single attribute for a device (from webhook event).

        This is more efficient than updating the entire device when only
        one attribute changed (e.g., motion sensor state).

        Args:
            device_id: Hubitat device ID
            attribute_name: Attribute name (e.g., 'motion', 'switch', 'level')
            attribute_value: New attribute value

        Returns:
            True if update succeeded
        """
        device_id = str(device_id)

        # Update memory cache
        if device_id in self._memory_cache:
            attrs = self._memory_cache[device_id].get('attributes', {})
            attrs[attribute_name] = attribute_value
            self._memory_cache[device_id]['attributes'] = attrs
            self._memory_cache[device_id]['last_synced_at'] = datetime.now().isoformat()
            self._memory_cache[device_id]['sync_source'] = 'webhook'

        # Update database using PATCH
        try:
            # First get current device to merge attributes
            current = self.get_device(device_id)
            if current:
                attrs = current.get('attributes', {})
                attrs[attribute_name] = attribute_value

                response = requests.patch(
                    f"{self.postgrest_url}/device_cache",
                    params={"hubitat_device_id": f"eq.{device_id}"},
                    json={
                        "attributes": attrs,
                        "last_synced_at": datetime.now().isoformat(),
                        "sync_source": "webhook"
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=5
                )
                return response.status_code in (200, 204)
        except Exception as e:
            self.logger.error(f"Failed to update device attribute: {e}", exc_info=True)

        return False

    # =========================================================================
    # Cache Management
    # =========================================================================

    def invalidate(self, device_id: str = None) -> None:
        """
        Invalidate cached data.

        Args:
            device_id: Specific device to invalidate, or None for all
        """
        if device_id:
            self._memory_cache.pop(str(device_id), None)
        else:
            self._memory_cache.clear()
            self._last_full_sync = None

    def clear(self) -> bool:
        """
        Clear all cached data from memory and database.

        Returns:
            True if clear succeeded
        """
        self._memory_cache.clear()
        self._last_full_sync = None

        try:
            response = requests.delete(
                f"{self.postgrest_url}/device_cache",
                params={"hubitat_device_id": "neq."},  # Match all
                timeout=10
            )
            return response.status_code in (200, 204)
        except Exception as e:
            self.logger.error(f"Failed to clear device cache: {e}", exc_info=True)
            return False

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _is_cache_valid(self) -> bool:
        """Check if the memory cache is still valid based on TTL."""
        if not self._last_full_sync:
            return False

        age = datetime.now() - self._last_full_sync
        return age.total_seconds() < self.ttl_seconds

    def _load_from_database(self) -> List[Dict[str, Any]]:
        """Load all devices from database."""
        try:
            response = requests.get(
                f"{self.postgrest_url}/device_cache",
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            self.logger.error(f"Failed to load devices from database: {e}", exc_info=True)
        return []

    def _extract_attributes(self, device_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract current attribute values from Hubitat device data.

        Hubitat returns attributes in a list format:
        [{"name": "motion", "currentValue": "active"}, ...]

        We convert to a dict for easier access:
        {"motion": "active", ...}
        """
        attributes = {}

        # Handle list format from detailed device query
        if 'attributes' in device_data and isinstance(device_data['attributes'], list):
            for attr in device_data['attributes']:
                name = attr.get('name')
                value = attr.get('currentValue')
                if name:
                    attributes[name] = value

        # Handle dict format (already converted)
        elif 'attributes' in device_data and isinstance(device_data['attributes'], dict):
            attributes = device_data['attributes']

        return attributes


def get_default_cache() -> DeviceCache:
    """
    Create a DeviceCache with configuration from environment variables.

    Returns:
        Configured DeviceCache instance
    """
    return DeviceCache(
        postgrest_url=os.environ.get('POSTGREST_URL', 'http://postgrest:3001'),
        ttl_seconds=int(os.environ.get('DEVICE_CACHE_TTL', '300'))
    )
