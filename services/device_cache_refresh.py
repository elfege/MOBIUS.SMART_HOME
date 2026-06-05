"""
Device Cache Refresh Service

Background service that periodically polls authoritative device state
and updates the local cache. Detects and logs discrepancies between
cached state and actual device state.

State query priority:
1. Matter (local network, ~10ms) — for devices with a device_matter_map entry
2. Hubitat Maker API (~100-500ms) — fallback for all devices

Follows the same pattern as MatterDiscoveryService:
- APScheduler recurring job
- Dedicated event loop per callback (thread-safe async)
- Graceful per-device error handling
"""

import asyncio
import logging
import os
import threading
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ANSI color for device names in logs
_C = "\033[96m"   # bright cyan
_Y = "\033[93m"   # bright yellow (discrepancies)
_R = "\033[0m"    # reset

# Default refresh interval: 2 minutes
DEFAULT_REFRESH_INTERVAL = 120

# Attributes to track for discrepancy detection
TRACKED_ATTRIBUTES = ('switch', 'level', 'motion')


class DeviceCacheRefreshService:
    """
    Periodically polls Hubitat Maker API and Matter to verify and
    refresh the device cache. Logs discrepancies between cached and
    live state.

    Designed to run as a background thread managed by APScheduler.
    Thread-safe: uses its own event loop for async operations.
    """

    def __init__(self, refresh_interval: int = DEFAULT_REFRESH_INTERVAL):
        """
        Args:
            refresh_interval: Seconds between cache refresh cycles
        """
        self.refresh_interval = refresh_interval
        self._job_id = 'device_cache_refresh'
        self._running = False
        # Track discrepancy counts per device for monitoring
        self._discrepancy_counts: Dict[str, int] = {}
        logger.info(
            f"DeviceCacheRefreshService initialized "
            f"(interval={refresh_interval}s)"
        )

    def start(self) -> None:
        """Register the recurring refresh job with APScheduler."""
        if self._running:
            logger.warning("DeviceCacheRefreshService already running")
            return

        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()

        scheduler.schedule_recurring(
            job_id=self._job_id,
            interval_seconds=self.refresh_interval,
            callback=self._run_refresh,
            job_type='cache_refresh'
        )

        self._running = True
        logger.info(
            f"Device cache refresh started (every {self.refresh_interval}s)"
        )

        # Run initial refresh immediately (in a thread so we don't block)
        threading.Thread(
            target=self._run_refresh,
            kwargs={'job_id': self._job_id, 'payload': {}},
            daemon=True
        ).start()

    def stop(self) -> None:
        """Cancel the recurring refresh job."""
        if not self._running:
            return

        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()
        scheduler.cancel(self._job_id)

        self._running = False
        logger.info("Device cache refresh stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    def _run_refresh(self, job_id: str = None, payload: Dict = None) -> None:
        """
        APScheduler callback — runs in a thread pool.
        Creates a new event loop for async Matter operations.
        """
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._refresh_all_devices())
        except Exception as e:
            logger.error(
                f"[CacheRefresh] cycle failed: {e}",
                exc_info=True
            )
        finally:
            loop.close()

        # Piggyback: opportunistic device-name normalization. Strips a trailing
        # " on <hub name>" suffix from device labels. Dry-run by default; gated
        # by system_settings (device_name_normalizer_enabled / _apply). It is
        # synchronous (plain requests) and self-contained — it must never break
        # the refresh cycle, so swallow everything.
        try:
            from services.device_name_normalizer import run_normalizer_pass
            run_normalizer_pass()
        except Exception as e:
            logger.debug(f"[CacheRefresh] device-name normalizer skipped: {e}")

    async def _refresh_all_devices(self) -> None:
        """
        Main refresh logic. For each cached device:
        1. Get live state (Matter-first, Maker API fallback)
        2. Compare against cache
        3. If discrepancy → log, update cache
        """
        from services.device_cache import get_default_cache
        from services.hubitat_client import get_default_client, get_hub_client_by_ip
        from services.device_to_hubs_classifier import get_device_by_canonical_id
        from services.matter_client import get_all_matter_mappings

        cache = get_default_cache()
        # Default client is only used as a fallback for devices we can't
        # locate in the routing cache. Per-device, the actual fetch goes to
        # the hub that natively owns that device id (post-migration, ids in
        # device_cache may live on any hub, not just the default/MAIN).
        hubitat = get_default_client()

        # Load all Matter mappings upfront (one DB query). Sync PostgREST
        # call → off the loop so the cache-refresh task doesn't block on
        # slow database responses.
        matter_map = {}
        try:
            mappings = await asyncio.to_thread(get_all_matter_mappings)
            for m in mappings:
                matter_map[str(m['hubitat_device_id'])] = m
        except Exception as e:
            logger.debug(f"[CacheRefresh] No Matter mappings: {e}")

        # Get all cached devices
        cached_devices = cache._memory_cache
        if not cached_devices:
            # Memory cache empty — try loading from DB
            cache._load_from_database()
            cached_devices = cache._memory_cache

        if not cached_devices:
            logger.debug("[CacheRefresh] No devices in cache, skipping")
            return

        refreshed = 0
        discrepancies = 0

        for canonical_id, cached_data in list(cached_devices.items()):
            try:
                cached_attrs = cached_data.get('attributes', {})
                device_name = cached_data.get(
                    'device_label',
                    cached_data.get('device_name', canonical_id)
                )

                # Cache keys are CANONICAL devices.id PKs (post-Phase-5).
                # Resolve to (hub_ip, hubitat_id) — Matter uses canonical_id
                # via the matter_map, Maker API needs the per-hub id.
                row = get_device_by_canonical_id(canonical_id)
                if not row or not row.get("hub_ip"):
                    # No canonical row — stale entry. Cache write would
                    # have been blocked by the FK; this row shouldn't
                    # exist. Skip defensively.
                    continue
                hub_ip = row["hub_ip"]
                hubitat_id = str(row.get("hubitat_id") or "")
                fetch_client = get_hub_client_by_ip(hub_ip) or hubitat

                mapping = matter_map.get(str(canonical_id)) or matter_map.get(hubitat_id)

                # Get live state (Matter first, API fallback). Pass the
                # per-hub Hubitat id since that's what the Maker API needs.
                live_state = await self._get_live_state(
                    hubitat_id, mapping, fetch_client
                )

                if not live_state:
                    continue  # Both sources failed, skip

                # Detect discrepancies
                diffs = self._detect_discrepancies(
                    cached_attrs, live_state
                )

                if diffs:
                    for attr, cached_val, live_val in diffs:
                        # Pass canonical_id to the handler — the cache is
                        # keyed canonically and update_device_attribute
                        # expects canonical PKs.
                        self._handle_discrepancy(
                            canonical_id, device_name, attr,
                            cached_val, live_val, live_state.get('source', '?'),
                            cache
                        )
                        discrepancies += 1

                refreshed += 1

            except Exception as e:
                logger.debug(
                    f"[CacheRefresh] Error refreshing {canonical_id}: {e}"
                )

        if discrepancies > 0:
            logger.info(
                f"[CacheRefresh] {refreshed} devices checked, "
                f"{_Y}{discrepancies} discrepancies fixed{_R}"
            )
        else:
            logger.debug(
                f"[CacheRefresh] {refreshed} devices checked, cache is clean"
            )

    async def _get_live_state(
        self,
        device_id: str,
        matter_mapping: Optional[Dict[str, Any]],
        hubitat
    ) -> Dict[str, Any]:
        """
        Get authoritative device state. Matter first, Maker API fallback.

        Matter queries are local network (~10ms). Maker API goes through
        the hub's cloud-connected REST API (~100-500ms).

        Args:
            device_id: Hubitat device ID
            matter_mapping: Matter mapping dict or None
            hubitat: HubitatClient instance

        Returns:
            Dict with attribute values + 'source' key, or empty dict
        """
        # --- Tier 1: Matter (local network, fastest) ---
        if matter_mapping:
            try:
                from services.matter_client import (
                    get_matter_client, CLUSTER_ON_OFF, CLUSTER_LEVEL_CONTROL
                )
                client = get_matter_client()

                node_id = matter_mapping['matter_node_id']
                endpoint_id = matter_mapping.get('matter_endpoint_id', 1)

                # Read OnOff attribute (cluster 6, attribute 0)
                on_off = await client.read_attribute(
                    node_id=node_id,
                    endpoint_id=endpoint_id,
                    cluster_id=CLUSTER_ON_OFF,
                    attribute_id=0
                )

                state = {
                    'switch': 'on' if on_off else 'off',
                    'source': 'matter'
                }

                # Try reading level too (cluster 8, attribute 0)
                try:
                    level_raw = await client.read_attribute(
                        node_id=node_id,
                        endpoint_id=endpoint_id,
                        cluster_id=CLUSTER_LEVEL_CONTROL,
                        attribute_id=0
                    )
                    if level_raw is not None:
                        # Matter uses 0-254, Hubitat uses 0-100
                        state['level'] = str(round(int(level_raw) * 100 / 254))
                except Exception:
                    pass  # Level not supported on this device

                return state

            except Exception as e:
                logger.debug(
                    f"[CacheRefresh] Matter query failed for "
                    f"{device_id}, falling back to API: {e}"
                )

        # --- Tier 2: Maker API (fallback) ---
        try:
            device = hubitat.get_device(device_id)
            if device and 'attributes' in device:
                state = {'source': 'api'}
                for attr in device['attributes']:
                    name = attr.get('name')
                    if name in TRACKED_ATTRIBUTES:
                        state[name] = attr.get('currentValue')
                return state
        except Exception as e:
            logger.debug(
                f"[CacheRefresh] API query failed for {device_id}: {e}"
            )

        return {}

    def _detect_discrepancies(
        self,
        cached_attrs: Dict[str, Any],
        live_state: Dict[str, Any]
    ) -> List[tuple]:
        """
        Compare cached vs live state.

        Args:
            cached_attrs: Attributes from cache (dict format)
            live_state: Attributes from live query

        Returns:
            List of (attribute, cached_value, live_value) tuples
        """
        diffs = []
        for attr in TRACKED_ATTRIBUTES:
            cached_val = cached_attrs.get(attr)
            live_val = live_state.get(attr)
            # Only flag if live has a value and it differs from cache
            if live_val is not None and str(cached_val) != str(live_val):
                diffs.append((attr, cached_val, live_val))
        return diffs

    def _handle_discrepancy(
        self,
        device_id,
        device_name: str,
        attribute: str,
        cached_val: Any,
        live_val: Any,
        source: str,
        cache
    ) -> None:
        """
        Handle a single attribute discrepancy: log and update cache.

        Args:
            device_id: Canonical devices.id PK (post-Phase-5)
            device_name: Human-readable device name
            attribute: Attribute name (switch, level, etc.)
            cached_val: Value in cache
            live_val: Authoritative value from Matter/API
            source: Where live value came from ('matter' or 'api')
            cache: DeviceCache instance
        """
        # Track discrepancy count per device
        key = f"{device_id}:{attribute}"
        self._discrepancy_counts[key] = self._discrepancy_counts.get(key, 0) + 1

        logger.warning(
            f"[CacheRefresh] {_C}{device_name}{_R} {attribute}: "
            f"{_Y}{cached_val} → {live_val}{_R} (source={source})"
        )

        # Update cache with authoritative value
        try:
            cache.update_device_attribute(device_id, attribute, live_val)
        except Exception as e:
            logger.error(
                f"[CacheRefresh] Failed to update cache for "
                f"{device_name}/{attribute}: {e}",
                exc_info=True
            )


# =============================================================================
# Module-level singleton
# =============================================================================

_service: Optional[DeviceCacheRefreshService] = None


def get_cache_refresh_service(
    refresh_interval: int = DEFAULT_REFRESH_INTERVAL
) -> DeviceCacheRefreshService:
    """Get or create the singleton service."""
    global _service
    if _service is None:
        _service = DeviceCacheRefreshService(
            refresh_interval=refresh_interval
        )
    return _service


def start_cache_refresh(
    refresh_interval: int = DEFAULT_REFRESH_INTERVAL
) -> DeviceCacheRefreshService:
    """Start the device cache refresh background service."""
    service = get_cache_refresh_service(refresh_interval)
    service.start()
    return service


def stop_cache_refresh() -> None:
    """Stop the device cache refresh background service."""
    global _service
    if _service:
        _service.stop()
# reload-db-routing
# reload-skip-stale
