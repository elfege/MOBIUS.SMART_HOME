"""
Matter Discovery Service

Background service that periodically scans THE single selected Matter hub
(SINGLE-HUB MATTER POLICY, 2026-07-11 — Matter-only; every other subsystem
stays multi-hub) for Matter devices and updates their online/offline status.

Commissioning is NEVER automatic here: the rolling auto-commissioner was
REMOVED 2026-07-10 (it saturated device fabric slots) and bulk commissioning
was removed entirely 2026-07-11. Commissioning is per-device, user-initiated,
via the UI only.

Runs as a recurring task via the existing APScheduler infrastructure.
Configurable scan interval (default: 5 minutes).

Architecture:
    APScheduler (interval) → _scan_and_commission()   [name kept for job-id stability]
        ├── GET /hub/matterDetails/json on THE Matter hub → discover devices
        ├── UPSERT hubitat_matter_devices via PostgREST → update status
        └── Phase 4: reconcile device_matter_map (mapping only, no commissioning)
"""

import os
import asyncio
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Commission retry policy
MAX_COMMISSION_ATTEMPTS = 5          # Stop trying after this many failures
COMMISSION_BASE_BACKOFF_SECONDS = 60 # First retry after 60s, then 120, 240, 480, 960
COMMISSION_CONCURRENCY = 1           # Only 1 PASE session at a time


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

                    # Name match to Maker API device — EXACT name.lower() ONLY
                    # (operator directive 2026-07-09). Fuzzy/partial matching was
                    # removed: any substring overlap counted, which produced
                    # nonsensical cross-matches (e.g. "Light bedroom 1" ↔ "Light
                    # Laundry"). Exact-only means a non-match stays UNMATCHED (the
                    # operator resolves it via the card's "edit" dropdown) rather
                    # than being silently mis-mapped to the wrong device.
                    match_name = device_name.strip().lower()
                    maker_match = maker_by_name.get(match_name)
                    confidence = 'exact' if maker_match else 'none'

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

        # --- Phase 3: REMOVED (2026-07-10, operator directive) ---
        # The rolling AUTO-commissioner is GONE. It re-opened pairing windows and
        # (re)added our fabric to devices on a 5-minute timer, exhausting device
        # fabric slots / CHIP session pools and DOSing matter-server (the WS
        # retransmission storm that made every command fail). Commissioning is now
        # ONLY ever operator-initiated — the Commission button ->
        # POST /api/matter/auto-commission, one device on demand. Discovery
        # (Phases 1-2) and mapping reconcile (Phase 4) still run on the timer;
        # commissioning NEVER does. Auto-discovery good, auto-commission bad.
        commissioned = 0

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
        Commission devices sequentially with a concurrency of 1.

        Matter PASE sessions cannot overlap — only one commissioning handshake
        can be active at a time. Devices are processed one-by-one.

        Each failure increments commission_attempts and records the error in the
        DB. The caller (phase 3) is responsible for filtering out devices that
        have exceeded MAX_COMMISSION_ATTEMPTS or are still in backoff cooldown.

        Returns number successfully commissioned.
        """
        import requests as req
        from services.matter_client import get_matter_client

        postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        commissioned = 0

        # Ensure matter-server connection before starting batch
        client = get_matter_client()
        if not client.is_connected:
            connected = await client.connect()
            if not connected:
                logger.error(
                    "Cannot connect to matter-server — "
                    "skipping all commissioning this cycle"
                )
                return 0

        # Pre-fetch existing nodes on our fabric to avoid re-commissioning
        try:
            existing_nodes = await client.get_nodes()
            existing_unique_ids = set()
            for node in existing_nodes:
                # python-matter-server nodes may have a unique_id attribute
                uid = node.get('unique_id') or node.get('uniqueId', '')
                if uid:
                    existing_unique_ids.add(uid)
            logger.info(
                f"Matter fabric has {len(existing_nodes)} existing nodes"
            )
        except Exception as e:
            logger.warning(f"Could not query existing Matter nodes: {e}")
            existing_unique_ids = set()

        for device in devices:
            unique_id = device['unique_id']
            device_name = device.get('device_name', unique_id)
            hub_ip = device['hub_ip']
            hubitat_node = device['hubitat_node_id']
            attempts = device.get('commission_attempts', 0)
            now_ts = datetime.now(timezone.utc).isoformat()

            # --- Guard: skip if already on our fabric ---
            if unique_id in existing_unique_ids:
                logger.info(
                    f"Skipping {device_name} — already on our Matter fabric "
                    f"(unique_id={unique_id})"
                )
                # Mark as commissioned in DB so we stop retrying
                try:
                    req.patch(
                        f"{postgrest_url}/hubitat_matter_devices",
                        params={"unique_id": f"eq.{unique_id}"},
                        json={"is_commissioned": True},
                        headers={"Content-Type": "application/json"},
                        timeout=5
                    )
                except Exception:
                    pass
                continue

            logger.info(
                f"Commissioning {device_name} "
                f"(attempt {attempts + 1}/{MAX_COMMISSION_ATTEMPTS})"
            )

            error_msg = None
            try:
                # Step 1: Open pairing window on Hubitat hub
                pair_resp = req.get(
                    f"http://{hub_ip}/hub/matter/openPairingWindow",
                    params={"node": hubitat_node},
                    timeout=90
                )
                if not pair_resp.ok:
                    error_msg = (
                        f"Pairing window HTTP {pair_resp.status_code}: "
                        f"{pair_resp.text[:200]}"
                    )
                    logger.warning(f"{device_name}: {error_msg}")
                else:
                    pair_data = pair_resp.json()
                    setup_code = (
                        pair_data.get('setupCode') or
                        pair_data.get('code') or
                        pair_data.get('pairingCode') or
                        (pair_data if isinstance(pair_data, str) else None)
                    )
                    if not setup_code:
                        error_msg = f"No setup code in response: {pair_data}"
                        logger.warning(f"{device_name}: {error_msg}")
                    else:
                        # Step 2: Commission into our matter-server
                        result = await asyncio.wait_for(
                            client.commission_with_code(str(setup_code)),
                            timeout=120  # Hard timeout for PASE + commissioning
                        )
                        our_node_id = (
                            result.get('node_id')
                            if isinstance(result, dict)
                            else None
                        )

                        # Step 3: Create device_matter_map entry
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

                        # Step 4: Mark success in DB
                        req.patch(
                            f"{postgrest_url}/hubitat_matter_devices",
                            params={"unique_id": f"eq.{unique_id}"},
                            json={
                                "our_node_id": our_node_id,
                                "is_commissioned": True,
                                "last_commission_attempt": now_ts,
                                "last_commission_error": None
                            },
                            headers={"Content-Type": "application/json"},
                            timeout=5
                        )

                        commissioned += 1
                        logger.info(
                            f"Commissioned {device_name} as node {our_node_id}"
                        )
                        continue  # Success — skip the failure update below

            except asyncio.TimeoutError:
                error_msg = "Commission timed out after 120s"
                logger.error(f"{device_name}: {error_msg}")
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"Commission failed for {device_name}: {e}")

            # --- Failure: update attempt counter and error ---
            try:
                req.patch(
                    f"{postgrest_url}/hubitat_matter_devices",
                    params={"unique_id": f"eq.{unique_id}"},
                    json={
                        "commission_attempts": attempts + 1,
                        "last_commission_attempt": now_ts,
                        "last_commission_error": (error_msg or "Unknown error")[:500]
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=5
                )
            except Exception as db_err:
                logger.warning(
                    f"Failed to update commission_attempts for "
                    f"{device_name}: {db_err}"
                )

            # Brief cooldown between sequential attempts to let the
            # Matter network settle before trying the next device
            await asyncio.sleep(5)

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
        Return the ONE hub Matter is allowed to interact with, as a one-element
        list (list shape kept so the Phase-1 scan loop is unchanged).

        SINGLE-HUB MATTER POLICY (operator directive 2026-07-11, Matter-ONLY —
        every non-Matter subsystem stays multi-hub): discovery used to scan
        EVERY hub from env vars; hubMesh mirrors then produced duplicate rows
        and commissioning could target any hub. Resolution mirrors
        app._resolve_matter_hub (sync variant for this service's thread):
        system_settings.matter_hub_id if it points at a real hub row, else the
        hub_config row marked is_primary (the "main" hub). Token comes from the
        env var NAMED by hub_config.maker_api_token_env. Returns [] when
        nothing resolves — the scan cycle no-ops with a warning.
        """
        import requests

        pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        sel = ("id,hub_name,hub_ip,is_primary,is_enabled,"
               "maker_api_app_number,maker_api_token_env")
        row = None
        try:
            r = requests.get(f"{pg}/system_settings",
                             params={"key": "eq.matter_hub_id", "select": "value"},
                             timeout=5)
            raw = (str(r.json()[0].get("value") or "").strip()
                   if r.ok and r.json() else "")
            if raw.isdigit():
                r2 = requests.get(f"{pg}/hub_config",
                                  params={"id": f"eq.{int(raw)}", "select": sel},
                                  timeout=5)
                if r2.ok and r2.json():
                    row = r2.json()[0]
        except Exception as e:
            logger.debug(f"matter_hub_id lookup failed: {e}")
        if row is None:
            try:
                r3 = requests.get(f"{pg}/hub_config",
                                  params={"is_primary": "eq.true", "select": sel,
                                          "limit": "1"},
                                  timeout=5)
                if r3.ok and r3.json():
                    row = r3.json()[0]
            except Exception as e:
                logger.warning(f"primary hub lookup failed: {e}")
        if row is None:
            logger.warning("matter_discovery: no Matter hub resolvable (no "
                           "matter_hub_id selection, no primary hub) — skipping scan")
            return []
        token_env = row.get('maker_api_token_env') or ''
        return [{
            'ip': row['hub_ip'],
            'token': os.environ.get(token_env, '') if token_env else '',
            'app_number': str(row.get('maker_api_app_number') or ''),
            'name': row['hub_name'],
        }]


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
