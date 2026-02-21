"""
Matter Discovery Service

Background service that periodically scans Hubitat hubs for Matter devices,
updates their online/offline status, and auto-commissions new devices into
our matter-server fabric.

Runs as a recurring task via the existing APScheduler infrastructure.
Configurable scan interval (default: 5 minutes).

Architecture:
    APScheduler (interval) → scan_and_commission()
        ├── POST /hub/matterDetails/json per hub → discover devices
        ├── UPSERT hubitat_matter_devices via PostgREST → update status
        └── commission_with_code per new online device → join our fabric
"""

import os
import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def mac_from_ipv6_ll(ipv6: str) -> Optional[str]:
    """
    Extract MAC address from an IPv6 link-local address (EUI-64 encoded).

    fe80::66e8:33ff:fe91:8978 → 64:e8:33:91:89:78

    Returns None if the address isn't a valid link-local with embedded MAC.
    """
    if not ipv6 or not ipv6.startswith('fe80::'):
        return None
    try:
        suffix = ipv6.split('::')[1]
        parts = suffix.split(':')
        if len(parts) != 4:
            return None
        eui = ''.join(p.zfill(4) for p in parts)
        b = [int(eui[i:i+2], 16) for i in range(0, len(eui), 2)]
        # Verify ff:fe in the middle (EUI-64 marker)
        if b[3] != 0xff or b[4] != 0xfe:
            return None
        b[0] ^= 0x02  # flip universal/local bit
        mac = b[:3] + b[5:]
        return ':'.join(f'{x:02x}' for x in mac)
    except Exception:
        return None

# Default interval: 5 minutes
DEFAULT_SCAN_INTERVAL = 300


class MatterDiscoveryService:
    """
    Periodically scans Hubitat hubs for Matter devices and auto-commissions
    newly discovered online devices into our matter-server.

    Designed to run as a background thread managed by APScheduler.
    Thread-safe: uses its own event loop for async operations.
    """

    def __init__(self, scan_interval: int = DEFAULT_SCAN_INTERVAL):
        """
        Args:
            scan_interval: Seconds between discovery scans
        """
        self.scan_interval = scan_interval
        self._job_id = 'matter_discovery_scan'
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        logger.info(f"MatterDiscoveryService initialized (interval={scan_interval}s)")

    def start(self) -> None:
        """Register the recurring scan job with APScheduler."""
        if self._running:
            logger.warning("MatterDiscoveryService already running")
            return

        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()

        scheduler.schedule_recurring(
            job_id=self._job_id,
            interval_seconds=self.scan_interval,
            callback=self._run_scan,
            job_type='matter_discovery'
        )

        self._running = True
        logger.info(f"Matter discovery started (every {self.scan_interval}s)")

        # Run an initial scan immediately (in a thread so we don't block)
        threading.Thread(target=self._run_scan, kwargs={'job_id': self._job_id, 'payload': {}}, daemon=True).start()

    def stop(self) -> None:
        """Cancel the recurring scan job."""
        if not self._running:
            return

        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()
        scheduler.cancel(self._job_id)

        self._running = False
        logger.info("Matter discovery stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    def _run_scan(self, job_id: str = None, payload: Dict = None) -> None:
        """
        APScheduler callback — runs in a thread pool.
        Creates a new event loop for async operations.
        """
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._scan_and_commission())
        except Exception as e:
            logger.error(f"Matter discovery scan failed: {e}")
        finally:
            loop.close()

    async def _scan_and_commission(self) -> Dict[str, Any]:
        """
        Core scan logic:
        1. Query each Hubitat hub for Matter device details
        2. Upsert discovered devices into hubitat_matter_devices
        3. Auto-commission any new online, uncommissioned devices
        """
        import requests as req

        postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        hubs = self._get_hub_configs()

        if not hubs:
            logger.warning("No Hubitat hubs configured — skipping Matter scan")
            return {"discovered": 0}

        all_discovered = []
        errors = []

        # --- Phase 1: Scan all hubs ---
        for hub in hubs:
            hub_ip = hub['ip']
            hub_token = hub['token']
            hub_app = hub['app_number']
            hub_name = hub.get('name', hub_ip)

            try:
                # Get Matter device details from hub
                matter_resp = req.get(
                    f"http://{hub_ip}/hub/matterDetails/json",
                    timeout=15
                )
                if not matter_resp.ok:
                    errors.append(f"{hub_name}: HTTP {matter_resp.status_code}")
                    continue

                matter_data = matter_resp.json()
                devices = matter_data if isinstance(matter_data, list) else matter_data.get('devices', [])

                # Get Maker API devices for name matching
                maker_resp = req.get(
                    f"http://{hub_ip}/apps/api/{hub_app}/devices/all",
                    params={"access_token": hub_token},
                    timeout=15
                )
                maker_devices = maker_resp.json() if maker_resp.ok else []

                # Build lookup by name for matching
                maker_by_name = {}
                for md in maker_devices:
                    name = md.get('label', md.get('name', '')).strip().lower()
                    maker_by_name[name] = md

                for device in devices:
                    unique_id = device.get('uniqueId') or device.get('unique_id', '')
                    if not unique_id:
                        continue

                    device_name = device.get('name', device.get('label', 'Unknown'))
                    is_online = device.get('online', device.get('isOnline', False))

                    # Name match to Maker API device
                    match_name = device_name.strip().lower()
                    maker_match = maker_by_name.get(match_name)
                    confidence = 'exact' if maker_match else 'none'

                    # Fuzzy match: try partial
                    if not maker_match:
                        for mk_name, mk_dev in maker_by_name.items():
                            if match_name in mk_name or mk_name in match_name:
                                maker_match = mk_dev
                                confidence = 'fuzzy'
                                break

                    ip_addr = device.get('ipAddress', device.get('ip', ''))
                    derived_mac = mac_from_ipv6_ll(ip_addr)

                    record = {
                        "unique_id": unique_id,
                        "device_name": device_name,
                        "manufacturer": device.get('manufacturer', ''),
                        "model": device.get('model', ''),
                        "ip_address": ip_addr,
                        "mac_address": derived_mac,
                        "is_online": is_online,
                        "hub_ip": hub_ip,
                        "hub_name": hub_name,
                        "hubitat_node_id": device.get('nodeId', device.get('node_id', 0)),
                        "hubitat_device_id": device.get('deviceId', device.get('id', '')),
                        "hubitat_dni": device.get('dni', device.get('deviceNetworkId', '')),
                        "firmware_version": device.get('firmwareVersion', device.get('softwareVersion', '')),
                        "hardware_version": device.get('hardwareVersion', ''),
                        "serial_number": device.get('serialNumber', ''),
                        "product_id": str(device.get('productId', device.get('productID', ''))) if device.get('productId') or device.get('productID') else '',
                        "vendor_id": str(device.get('vendorId', device.get('vendorID', ''))) if device.get('vendorId') or device.get('vendorID') else '',
                        "last_seen_at": datetime.now(timezone.utc).isoformat() if is_online else None,
                    }
                    # Only set maker_api fields if we found a match.
                    # Never overwrite an existing match with null.
                    if maker_match:
                        record["maker_api_device_id"] = str(maker_match['id'])
                        record["maker_api_device_name"] = maker_match.get('label', maker_match.get('name', ''))
                        record["match_confidence"] = confidence
                    all_discovered.append(record)

            except Exception as e:
                errors.append(f"{hub_name}: {e}")
                logger.warning(f"Matter scan failed for hub {hub_name}: {e}")

        # --- Phase 2: Upsert to database ---
        # Use upsert for new records. For existing records, PATCH only status
        # fields to avoid overwriting manually-curated maker_api matches.
        upserted = 0
        for record in all_discovered:
            uid = record['unique_id']
            try:
                # Check if record already exists
                check = req.get(
                    f"{postgrest_url}/hubitat_matter_devices",
                    params={"unique_id": f"eq.{uid}", "select": "unique_id,maker_api_device_id"},
                    headers={"Accept": "application/json"},
                    timeout=5
                )
                existing = check.json() if check.ok else []

                if existing:
                    # Existing record: update status + info fields, preserve matches
                    patch_data = {
                        "is_online": record["is_online"],
                        "ip_address": record.get("ip_address", ""),
                        "device_name": record["device_name"],
                    }
                    # Update optional fields only if we have new data
                    for field in ("mac_address", "firmware_version", "hardware_version",
                                  "serial_number", "product_id", "vendor_id"):
                        val = record.get(field)
                        if val:
                            patch_data[field] = val
                    if record.get("last_seen_at"):
                        patch_data["last_seen_at"] = record["last_seen_at"]
                    # Only update maker fields if we have a new match AND existing has none
                    if record.get("maker_api_device_id") and not existing[0].get("maker_api_device_id"):
                        patch_data["maker_api_device_id"] = record["maker_api_device_id"]
                        patch_data["maker_api_device_name"] = record.get("maker_api_device_name")
                        patch_data["match_confidence"] = record.get("match_confidence", "none")

                    req.patch(
                        f"{postgrest_url}/hubitat_matter_devices",
                        params={"unique_id": f"eq.{uid}"},
                        json=patch_data,
                        headers={"Content-Type": "application/json"},
                        timeout=5
                    )
                else:
                    # New record: full insert
                    req.post(
                        f"{postgrest_url}/hubitat_matter_devices",
                        json=record,
                        headers={
                            "Content-Type": "application/json",
                            "Prefer": "resolution=merge-duplicates"
                        },
                        timeout=5
                    )
                upserted += 1
            except Exception as e:
                logger.warning(f"Failed to upsert device {uid}: {e}")

        logger.info(f"Matter scan: {upserted} devices upserted, {len(errors)} hub errors")

        # --- Phase 3: Auto-commission new online devices ---
        commissioned = 0
        try:
            # Get uncommissioned, online devices
            resp = req.get(
                f"{postgrest_url}/hubitat_matter_devices",
                params={
                    "is_online": "eq.true",
                    "is_commissioned": "eq.false"
                },
                headers={"Accept": "application/json"},
                timeout=5
            )
            if resp.ok:
                to_commission = resp.json()
                if to_commission:
                    commissioned = await self._commission_devices(to_commission)
        except Exception as e:
            logger.warning(f"Auto-commission phase failed: {e}")

        # --- Phase 4: Reconcile mappings ---
        # Match commissioned nodes to discovered devices and auto-create
        # device_matter_map entries for any that are missing.
        reconciled = await self._reconcile_mappings()

        result = {
            "discovered": len(all_discovered),
            "upserted": upserted,
            "commissioned": commissioned,
            "reconciled": reconciled,
            "errors": errors
        }
        logger.info(f"Matter discovery complete: {result}")
        return result

    async def _commission_devices(self, devices: list) -> int:
        """
        Commission a list of devices with concurrency limit.
        Returns number successfully commissioned.
        """
        import requests as req
        from services.matter_client import get_matter_client

        postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        sem = asyncio.Semaphore(len(devices))  # Full parallelism
        commissioned = 0

        async def commission_one(device):
            nonlocal commissioned
            unique_id = device['unique_id']
            device_name = device.get('device_name', unique_id)
            hub_ip = device['hub_ip']
            hubitat_node = device['hubitat_node_id']

            async with sem:
                try:
                    # Open pairing window on Hubitat
                    pair_resp = req.get(
                        f"http://{hub_ip}/hub/matter/openPairingWindow",
                        params={"node": hubitat_node},
                        timeout=90
                    )
                    if not pair_resp.ok:
                        logger.warning(f"Pairing window failed for {device_name}: HTTP {pair_resp.status_code}")
                        return

                    pair_data = pair_resp.json()
                    setup_code = (
                        pair_data.get('setupCode') or
                        pair_data.get('code') or
                        pair_data.get('pairingCode') or
                        (pair_data if isinstance(pair_data, str) else None)
                    )
                    if not setup_code:
                        logger.warning(f"No setup code for {device_name}: {pair_data}")
                        return

                    # Commission into our matter-server
                    client = get_matter_client()
                    if not client.is_connected:
                        connected = await client.connect()
                        if not connected:
                            logger.error("Cannot connect to matter-server")
                            return

                    result = await client.commission_with_code(str(setup_code))
                    our_node_id = result.get('node_id') if isinstance(result, dict) else None

                    # Create device_matter_map entry
                    if our_node_id is not None and device.get('maker_api_device_id'):
                        req.post(
                            f"{postgrest_url}/device_matter_map",
                            json={
                                "hubitat_device_id": device['maker_api_device_id'],
                                "matter_node_id": our_node_id,
                                "matter_endpoint_id": 1,
                                "device_name": device_name
                            },
                            headers={
                                "Content-Type": "application/json",
                                "Prefer": "resolution=merge-duplicates"
                            },
                            timeout=5
                        )

                    # Update hubitat_matter_devices
                    req.patch(
                        f"{postgrest_url}/hubitat_matter_devices",
                        params={"unique_id": f"eq.{unique_id}"},
                        json={
                            "our_node_id": our_node_id,
                            "is_commissioned": True
                        },
                        headers={"Content-Type": "application/json"},
                        timeout=5
                    )

                    commissioned += 1
                    logger.info(f"Commissioned {device_name} as node {our_node_id}")

                except Exception as e:
                    logger.warning(f"Commission failed for {device_name}: {e}")

        await asyncio.gather(*[commission_one(d) for d in devices])
        return commissioned

    async def _reconcile_mappings(self) -> int:
        """
        Reconcile device_matter_map from hubitat_matter_devices.

        Simple: query hubitat_matter_devices where our_node_id IS NOT NULL
        AND maker_api_device_id IS NOT NULL. For each, create a device_matter_map
        entry if one doesn't already exist. No UniqueID gymnastics needed —
        the data is already in the table.

        Returns number of new mappings created.
        """
        import requests as req

        postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        reconciled = 0

        try:
            # Get devices that are commissioned AND have a Maker API match
            resp = req.get(
                f"{postgrest_url}/hubitat_matter_devices",
                params={
                    "our_node_id": "not.is.null",
                    "maker_api_device_id": "not.is.null"
                },
                headers={"Accept": "application/json"},
                timeout=5
            )
            if not resp.ok or not resp.json():
                return 0

            candidates = resp.json()

            # Get existing mappings to avoid duplicates
            map_resp = req.get(
                f"{postgrest_url}/device_matter_map",
                headers={"Accept": "application/json"},
                timeout=5
            )
            existing_maps = set()
            if map_resp.ok:
                for m in map_resp.json():
                    existing_maps.add(str(m.get('matter_node_id')))

            for device in candidates:
                node_id = device['our_node_id']
                if str(node_id) in existing_maps:
                    continue

                maker_id = device['maker_api_device_id']
                device_name = device.get('device_name', '')

                # Create device_matter_map entry
                try:
                    resp = req.post(
                        f"{postgrest_url}/device_matter_map",
                        json={
                            "hubitat_device_id": maker_id,
                            "matter_node_id": node_id,
                            "matter_endpoint_id": 1,
                            "device_name": device_name
                        },
                        headers={
                            "Content-Type": "application/json",
                            "Prefer": "resolution=merge-duplicates"
                        },
                        timeout=5
                    )
                    if resp.ok:
                        reconciled += 1
                        logger.info(f"Reconciled mapping: {device_name} (Hubitat #{maker_id}) → Matter node {node_id}")
                except Exception as e:
                    logger.warning(f"Failed to create mapping for node {node_id}: {e}")

        except Exception as e:
            logger.error(f"Reconciliation failed: {e}")

        if reconciled > 0:
            logger.info(f"Reconciled {reconciled} new device mappings")
        return reconciled

    def _get_hub_configs(self) -> list:
        """
        Build list of hub configs from environment variables.
        Returns list of dicts with ip, token, app_number, name.
        """
        hubs = []

        # Main hub
        main_ip = os.environ.get('HUBITAT_HUB_IP_MAIN')
        main_token = os.environ.get('HUBITAT_API_TOKEN_MAIN')
        main_app = os.environ.get('HUBITAT_API_NUMBER_MAIN')
        if main_ip and main_token and main_app:
            hubs.append({
                'ip': main_ip,
                'token': main_token,
                'app_number': main_app,
                'name': 'main_hub'
            })

        # Other hubs (1-3)
        for i in range(1, 4):
            ip = os.environ.get(f'HUBITAT_HUB_IP_OTHER_HUB_{i}')
            token = os.environ.get(f'HUBITAT_API_TOKEN_OTHER_HUB_{i}')
            app_num = os.environ.get(f'HUBITAT_API_NUMBER_OTHER_HUB_{i}')
            if ip and token and app_num:
                hubs.append({
                    'ip': ip,
                    'token': token,
                    'app_number': app_num,
                    'name': f'other_hub_{i}'
                })

        return hubs


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: Optional[MatterDiscoveryService] = None


def get_matter_discovery_service(
    scan_interval: int = DEFAULT_SCAN_INTERVAL
) -> MatterDiscoveryService:
    """Get the global MatterDiscoveryService instance, creating if needed."""
    global _service
    if _service is None:
        _service = MatterDiscoveryService(scan_interval=scan_interval)
    return _service


def start_matter_discovery(scan_interval: int = DEFAULT_SCAN_INTERVAL) -> MatterDiscoveryService:
    """Start the Matter discovery background service."""
    service = get_matter_discovery_service(scan_interval)
    service.start()
    return service


def stop_matter_discovery() -> None:
    """Stop the Matter discovery background service."""
    global _service
    if _service:
        _service.stop()
