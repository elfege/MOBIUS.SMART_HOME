"""
Device Cache Service

Caches device states to reduce Hubitat API polling. Backed by PostgreSQL
(via PostgREST) for persistence.

Post-Phase-5 the cache is keyed by the canonical `devices.id` PK, NOT
the per-hub Hubitat id. This matches the rest of the system:
device_selections, device_subscriptions, and event.device_id all use
canonical ids. Callers that have a Hubitat per-hub id must translate
via services.device_to_hubs_classifier (get_device_by_canonical_id /
get_hub_for_device) before hitting the cache.

Key features:
- TTL-based cache invalidation
- Event-driven cache updates (when webhooks arrive — canonical id is
  already resolved by the webhook router)
- Capability filtering for device picker UI
- Bulk operations for efficiency
"""

import os
import logging
import traceback
import requests
from datetime import datetime, timedelta, timezone
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
            self._memory_cache = {str(d['device_id']): d for d in devices}
            self._last_full_sync = datetime.now(timezone.utc)
            return devices

        return []

    def get_device(self, device_id) -> Optional[Dict[str, Any]]:
        """
        Get a single cached device.

        Args:
            device_id: Canonical devices.id PK (int or stringified int)

        Returns:
            Device dictionary or None if not cached
        """
        key = str(device_id)
        # Check memory cache first
        if key in self._memory_cache:
            return self._memory_cache[key]

        # Try database
        try:
            response = requests.get(
                f"{self.postgrest_url}/device_cache",
                params={"device_id": f"eq.{key}"},
                timeout=5
            )
            if response.status_code == 200:
                devices = response.json()
                if devices:
                    device = devices[0]
                    self._memory_cache[key] = device
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

    def update_all(self, devices: List[Dict[str, Any]], hub_ip: str = None) -> bool:
        """
        Update cache with full device list from one hub's Maker API.

        This is typically called after get_all_devices() from a HubitatClient.
        Each device dict carries a per-hub Hubitat id; we translate to
        the canonical devices.id PK via the (hub_ip, hubitat_id) lookup.

        Args:
            devices: List of device dictionaries from Hubitat Maker API
            hub_ip: IP of the hub the devices came from (required for
                    canonical-id resolution; devices from different hubs
                    can share the same Hubitat per-hub id)

        Returns:
            True if update succeeded
        """
        if not devices:
            return True

        if not hub_ip:
            self.logger.warning(
                "update_all called without hub_ip — cannot resolve "
                "canonical ids; skipping cache update"
            )
            return False

        # Resolve (hub_ip, hubitat_id) → canonical id for each device.
        # Single batch query against the canonical devices table.
        from services.device_to_hubs_classifier import get_hub_for_device  # late import

        cache_entries = []
        for device in devices:
            hubitat_id = str(device.get('id') or '')
            if not hubitat_id:
                continue
            row = get_hub_for_device(hubitat_id)
            # The lookup matches the FIRST row with this hubitat_id; for
            # devices on multiple hubs that share an id we'd need
            # disambiguation, but `devices` already drops mesh duplicates
            # so by construction each canonical row maps to one (hub_ip,
            # hubitat_id) pair.
            if not row or row.get("hub_ip") != hub_ip:
                # Different hub or unknown device — likely a mesh mirror
                # we don't track. Skip.
                continue
            entry = {
                "device_id": int(row["id"]),
                "device_name": device.get('name'),
                "device_label": device.get('label'),
                "device_type": device.get('type'),
                "capabilities": device.get('capabilities', []),
                "attributes": self._extract_attributes(device),
                "last_synced_at": datetime.now(timezone.utc).isoformat(),
                "sync_source": "api"
            }
            cache_entries.append(entry)
            # Memory cache keyed by canonical PK as a string
            self._memory_cache[str(entry['device_id'])] = entry

        if not cache_entries:
            return True

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
                self._last_full_sync = datetime.now(timezone.utc)
                self.logger.info(
                    f"Updated cache with {len(cache_entries)} devices from {hub_ip}"
                )
                return True
            else:
                self.logger.error(f"Cache update failed: {response.text}")
                return False

        except Exception as e:
            self.logger.error(f"Failed to update device cache: {e}", exc_info=True)
            return False

    def update_device(self, device_id, device_data: Dict[str, Any]) -> bool:
        """
        Update a single cached device by canonical PK.

        Args:
            device_id: Canonical devices.id PK
            device_data: Full device data from a Hubitat Maker API call.
                The 'id' field on device_data (Hubitat per-hub id) is
                preserved in attributes only; the cache row is keyed
                solely by the canonical id passed in.

        Returns:
            True if update succeeded
        """
        canonical_id = int(device_id)
        entry = {
            "device_id": canonical_id,
            "device_name": device_data.get('name'),
            "device_label": device_data.get('label'),
            "device_type": device_data.get('type'),
            "capabilities": device_data.get('capabilities', []),
            "attributes": self._extract_attributes(device_data),
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
            "sync_source": "api"
        }

        self._memory_cache[str(canonical_id)] = entry

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
        device_id,
        attribute_name: str,
        attribute_value: Any
    ) -> bool:
        """
        Update a single attribute for a device (from webhook event).

        This is more efficient than updating the entire device when only
        one attribute changed (e.g., motion sensor state).

        Args:
            device_id: Canonical devices.id PK
            attribute_name: Attribute name (e.g., 'motion', 'switch', 'level')
            attribute_value: New attribute value

        Returns:
            True if update succeeded
        """
        key = str(device_id)

        # Memory cache — UPSERT (create entry if not present).
        # Previously this was guard-gated on `key in self._memory_cache`,
        # so devices that never went through the bulk-import path got no
        # cache entry, ever. That broke the on-already-on / off-already-off
        # dedup downstream: AML's `_turn_off_switch` returns early only if
        # cache says switch=='off', and an absent device means dedup is
        # skipped — every periodic master() tick fires a redundant command.
        # Caught live 2026-05-18 on canon 104 (Light Kitchen): 1 row in
        # device_cache for the whole system.
        if key not in self._memory_cache:
            try:
                cid_int = int(device_id)
            except (TypeError, ValueError):
                cid_int = device_id
            self._memory_cache[key] = {
                "device_id": cid_int,
                "attributes": {},
            }
        attrs = self._memory_cache[key].setdefault('attributes', {}) or {}
        attrs[attribute_name] = attribute_value
        self._memory_cache[key]['attributes'] = attrs
        self._memory_cache[key]['last_synced_at'] = (
            datetime.now(timezone.utc).isoformat()
        )
        self._memory_cache[key]['sync_source'] = 'webhook'

        # DB upsert. We can't PATCH for missing rows, so use POST with
        # `on_conflict=device_id` + `resolution=merge-duplicates`. The PK
        # is `device_id` (BIGINT not null) so the upsert is well-defined.
        # We send the full attribute dict merged with the new value;
        # PostgREST replaces the JSONB column (not deep-merge), so we
        # need the merged dict, not just the new key.
        try:
            try:
                cid_int = int(device_id)
            except (TypeError, ValueError):
                cid_int = device_id
            response = requests.post(
                f"{self.postgrest_url}/device_cache"
                f"?on_conflict=device_id",
                json={
                    "device_id": cid_int,
                    "attributes": attrs,
                    "last_synced_at":
                        datetime.now(timezone.utc).isoformat(),
                    "sync_source": "webhook",
                },
                headers={
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                timeout=5,
            )
            return response.status_code in (200, 201, 204)
        except Exception as e:
            self.logger.error(f"Failed to update device attribute: {e}", exc_info=True)

        return False

    # =========================================================================
    # Cache Management
    # =========================================================================

    def invalidate(self, device_id=None) -> None:
        """
        Invalidate cached data.

        Args:
            device_id: Specific canonical PK to invalidate, or None for all
        """
        if device_id is not None:
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
                params={"device_id": "gt.0"},  # Match all
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

        # Tz-aware: matches the tz-aware value stored in _last_full_sync.
        # Mixing naive and aware here would raise TypeError.
        age = datetime.now(timezone.utc) - self._last_full_sync
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
