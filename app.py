"""
0_SMART_HOME FastAPI Application

Main entry point for the smart home automation system.
Provides REST API for instance management, device access, and webhook handling.
Serves Jinja2 templates for the web UI.
"""

import os
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class CreateInstanceRequest(BaseModel):
    """Request body for creating a new automation instance."""
    app_type: str
    label: str
    device_selections: dict
    settings: dict = {}


class UpdateInstanceRequest(BaseModel):
    """Request body for updating an existing automation instance."""
    label: Optional[str] = None
    device_selections: Optional[dict] = None
    settings: Optional[dict] = None


class PauseInstanceRequest(BaseModel):
    """Request body for pausing an instance."""
    duration_minutes: Optional[int] = None
    reason: Optional[str] = None


class DeviceCommandRequest(BaseModel):
    """Request body for sending a command to a device."""
    command: str
    args: Optional[list] = None


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def initialize_services():
    """Initialize all services on startup."""
    logger.info("Initializing services...")

    # Initialize app registry
    from apps.app_registry import initialize_registry
    from services.instance_manager import get_instance_manager

    instance_manager = get_instance_manager()
    initialize_registry(instance_manager)

    # Initialize scheduler
    from services.scheduler_service import get_scheduler
    get_scheduler()

    # Load all instances
    instance_manager.initialize_all_instances()

    logger.info("Services initialized")


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: initialize services on startup, cleanup on shutdown."""
    initialize_services()

    # Start Matter discovery background service (scans hubs every 5 min)
    from services.matter_discovery import start_matter_discovery, stop_matter_discovery
    start_matter_discovery(scan_interval=300)

    yield

    stop_matter_discovery()
    logger.info("Shutting down...")


# ---------------------------------------------------------------------------
# Create FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MOBIUS.HOME",
    description="Hubitat home automation platform with multi-instance support",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Include routers
# ---------------------------------------------------------------------------

from apps.advanced_motion_lighting.blueprint import router as motion_router  # noqa: E402
app.include_router(motion_router)


# =============================================================================
# Health & Status
# =============================================================================


@app.get("/api/health", tags=["health"])
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/api/status", tags=["health"])
async def status():
    """Detailed status endpoint."""
    from services.instance_manager import get_instance_manager
    from services.hubitat_client import get_default_client

    manager = get_instance_manager()
    instances = manager.get_all_instances()

    # Check Hubitat connectivity
    try:
        client = get_default_client()
        hubitat_connected = client.is_connected()
    except Exception:
        hubitat_connected = False

    return {
        "status": "ok",
        "instances_count": len(instances),
        "running_instances": len(manager._running_instances),
        "hubitat_connected": hubitat_connected,
    }


# =============================================================================
# App Types
# =============================================================================


@app.get("/api/app-types", tags=["app-types"])
async def get_app_types():
    """List available app types."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    return manager.get_app_types()


@app.get("/api/app-types/{type_name}/schema", tags=["app-types"])
async def get_app_type_schema(type_name: str):
    """Get settings schema for an app type."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    schema = manager.get_app_type_schema(type_name)
    if schema:
        return schema
    raise HTTPException(status_code=404, detail="App type not found")


# =============================================================================
# Instances
# =============================================================================


@app.get("/api/instances", tags=["instances"])
async def get_instances():
    """List all instances."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    return manager.get_all_instances()


@app.post("/api/instances", status_code=201, tags=["instances"])
async def create_instance(body: CreateInstanceRequest):
    """Create a new instance."""
    from services.instance_manager import get_instance_manager

    manager = get_instance_manager()
    instance_id = manager.create_instance(
        app_type=body.app_type,
        label=body.label,
        device_selections=body.device_selections,
        settings=body.settings,
    )

    if instance_id:
        return {"id": instance_id, "message": "Instance created"}
    raise HTTPException(status_code=500, detail="Failed to create instance")


@app.get("/api/instances/{instance_id}", tags=["instances"])
async def get_instance(instance_id: int):
    """Get instance details."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    if instance:
        return instance
    raise HTTPException(status_code=404, detail="Instance not found")


@app.put("/api/instances/{instance_id}", tags=["instances"])
async def update_instance(instance_id: int, body: UpdateInstanceRequest):
    """Update instance settings."""
    from services.instance_manager import get_instance_manager

    manager = get_instance_manager()
    success = manager.update_instance(
        instance_id,
        label=body.label,
        device_selections=body.device_selections,
        settings=body.settings,
    )

    if success:
        return {"message": "Instance updated"}
    raise HTTPException(status_code=500, detail="Failed to update instance")


@app.delete("/api/instances/{instance_id}", tags=["instances"])
async def delete_instance(instance_id: int):
    """Delete an instance."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    if manager.delete_instance(instance_id):
        return {"message": "Instance deleted"}
    raise HTTPException(status_code=500, detail="Failed to delete instance")


@app.post("/api/instances/{instance_id}/pause", tags=["instances"])
async def pause_instance(instance_id: int, body: PauseInstanceRequest = PauseInstanceRequest()):
    """Pause an instance."""
    from services.instance_manager import get_instance_manager

    manager = get_instance_manager()
    if manager.pause_instance(instance_id, body.duration_minutes, body.reason):
        return {"message": "Instance paused"}
    raise HTTPException(status_code=500, detail="Failed to pause instance")


@app.post("/api/instances/{instance_id}/resume", tags=["instances"])
async def resume_instance(instance_id: int):
    """Resume a paused instance."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    if manager.resume_instance(instance_id):
        return {"message": "Instance resumed"}
    raise HTTPException(status_code=500, detail="Failed to resume instance")


@app.get("/api/instances/{instance_id}/status", tags=["instances"])
async def get_instance_status(instance_id: int):
    """Get runtime status of an instance."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    running = manager.get_running_instance(instance_id) is not None

    return {
        "id": instance_id,
        "label": instance.get("label"),
        "is_running": running,
        "is_paused": instance.get("is_paused", False),
        "is_enabled": instance.get("is_enabled", True),
        "last_activity": instance.get("last_activity_at"),
    }


@app.post("/api/instances/{instance_id}/run", tags=["instances"])
async def run_instance(instance_id: int):
    """
    Run instance: start if stopped, or re-evaluate state if already running.

    When already running, calls master() to evaluate current conditions
    (motion state, timeouts) and control lights accordingly.
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    running = manager.get_running_instance(instance_id)
    if running:
        # Already running — re-evaluate current state via master()
        running.master()
        return {"message": "Instance re-evaluated current state"}

    if manager._start_instance(instance_id, instance):
        return {"message": "Instance started"}
    raise HTTPException(status_code=500, detail="Failed to start instance")


@app.post("/api/instances/{instance_id}/update", tags=["instances"])
async def update_initialize_instance(instance_id: int):
    """
    Reload an instance (stop + start with current config).

    Also resets memoization state so the instance starts fresh
    without stale override records.
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # Reset memoization on reload (stale memo causes incorrect behavior)
    manager.update_memoization(instance_id, {})

    manager._reload_instance(instance_id)
    return {"message": "Instance reloaded with memoization reset"}


# =============================================================================
# Devices
# =============================================================================


@app.get("/api/devices", tags=["devices"])
async def get_devices(capability: Optional[str] = Query(None)):
    """
    List devices, optionally filtered by capability.

    Args:
        capability: Filter by capability (e.g., 'motionSensor', 'switch')
    """
    from services.hubitat_client import get_default_client
    from services.device_cache import get_default_cache

    try:
        client = get_default_client()
        cache = get_default_cache()

        if capability:
            devices = client.get_devices_by_capability(capability)
        else:
            devices = client.get_all_devices()

        # Update cache
        cache.update_all(devices)

        return devices

    except Exception as e:
        logger.error(f"Failed to get devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/devices/{device_id}", tags=["devices"])
async def get_device(device_id: str):
    """Get device details."""
    from services.hubitat_client import get_default_client

    try:
        client = get_default_client()
        device = client.get_device(device_id)
        if device:
            return device
        raise HTTPException(status_code=404, detail="Device not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get device: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/{device_id}/command", tags=["devices"])
async def send_device_command(device_id: str, body: DeviceCommandRequest):
    """
    Send command to a device.

    Args:
        device_id: Hubitat device ID
        body: Command name and optional arguments
    """
    from services.hubitat_client import get_default_client

    try:
        client = get_default_client()
        if client.send_command(device_id, body.command, body.args):
            return {"message": "Command sent"}
        raise HTTPException(status_code=500, detail="Command failed")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to send command: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Webhooks (from Hubitat via webhook-dispatcher)
# =============================================================================


@app.post("/api/webhook/event", tags=["webhooks"])
async def handle_event_webhook(request: Request):
    """
    Handle device event webhook from Hubitat.

    Hubitat Maker API sends events here (via webhook-dispatcher)
    when devices change state.
    """
    from services.webhook_router import get_webhook_router

    payload = await request.json()
    logger.debug(f"Webhook received: {payload}")

    router = get_webhook_router()
    routed_count = router.route_event(payload)

    return {"routed_to": routed_count}


@app.post("/api/webhook/mode", tags=["webhooks"])
async def handle_mode_webhook(request: Request):
    """Handle mode change webhook from Hubitat."""
    from services.webhook_router import get_webhook_router

    payload = await request.json()
    logger.info(f"Mode change webhook: {payload}")

    router = get_webhook_router()
    notified = router.route_mode_change(payload)

    return {"notified": notified}


# =============================================================================
# Modes
# =============================================================================


# =============================================================================
# Matter Protocol
# =============================================================================


class MatterCommissionRequest(BaseModel):
    """Request body for commissioning a new Matter device."""
    code: str  # QR code string (MT:...) or manual pairing code


class MatterMapRequest(BaseModel):
    """Request body for mapping a Hubitat device to a Matter node."""
    hubitat_device_id: str
    matter_node_id: int
    matter_endpoint_id: int = 1
    device_name: Optional[str] = None


@app.get("/api/matter/status", tags=["matter"])
async def matter_status():
    """
    Get matter-server connection status.

    Returns connection state and server info if connected.
    """
    from services.matter_client import get_matter_client

    client = get_matter_client()
    status = {"connected": client.is_connected, "url": client.url}

    if client.is_connected:
        try:
            info = await client.get_server_info()
            status["server_info"] = info
        except Exception as e:
            status["server_info_error"] = str(e)

    return status


@app.get("/api/matter/nodes", tags=["matter"])
async def matter_nodes():
    """
    List all commissioned Matter nodes.

    Connects to matter-server if not already connected.
    """
    from services.matter_client import get_matter_client

    client = get_matter_client()
    if not client.is_connected:
        connected = await client.connect()
        if not connected:
            raise HTTPException(
                status_code=503,
                detail="Cannot connect to matter-server"
            )

    try:
        nodes = await client.get_nodes()

        # Enrich nodes with friendly names from our discovered devices table.
        # Match by unique_id: Matter Basic Information cluster (40), attr 18 = UniqueID
        # Enrich with friendly names from hubitat_matter_devices.
        # Two lookups: by our_node_id (direct) and by unique_id (fallback).
        import requests as req
        postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        by_node_id = {}
        by_unique_id = {}
        try:
            disc_resp = req.get(
                f"{postgrest_url}/hubitat_matter_devices",
                headers={"Accept": "application/json"},
                timeout=5
            )
            if disc_resp.ok:
                for d in disc_resp.json():
                    if d.get('our_node_id'):
                        by_node_id[d['our_node_id']] = d
                    by_unique_id[d['unique_id']] = d
        except Exception:
            pass

        for node in nodes:
            node_id = node.get('node_id') or node.get('nodeId')

            # Primary: match by our_node_id (set during commission)
            match = by_node_id.get(node_id)

            # Fallback: match by UniqueID attribute
            if not match:
                attrs = node.get('attributes', {})
                for key, val in attrs.items():
                    if '/40/18' in key and isinstance(val, str) and val in by_unique_id:
                        match = by_unique_id[val]
                        break

            if match:
                # Prefer Hubitat friendly name over Matter product name
                node['_device_name'] = (
                    match.get('maker_api_device_name')
                    or match.get('device_name')
                )
                node['_hubitat_device_id'] = match.get('maker_api_device_id')

                # Backfill: if matched by UniqueID but our_node_id not set, update DB
                if not match.get('our_node_id') and node_id:
                    try:
                        req.patch(
                            f"{postgrest_url}/hubitat_matter_devices",
                            params={"unique_id": f"eq.{match['unique_id']}"},
                            json={"our_node_id": node_id},
                            headers={"Content-Type": "application/json"},
                            timeout=5
                        )
                        logger.info(f"Backfilled our_node_id={node_id} for {match['unique_id']}")
                    except Exception:
                        pass

        return nodes
    except Exception as e:
        logger.error(f"Failed to get Matter nodes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/matter/reconcile", tags=["matter"])
async def matter_reconcile():
    """
    Reconcile device_matter_map with commissioned nodes.
    Matches commissioned matter-server nodes to discovered Hubitat devices
    by UniqueID and creates missing mapping entries automatically.
    """
    from services.matter_discovery import get_matter_discovery_service
    service = get_matter_discovery_service()
    reconciled = await service._reconcile_mappings()
    return {"reconciled": reconciled}


@app.post("/api/matter/commission", tags=["matter"])
async def matter_commission(body: MatterCommissionRequest):
    """
    Commission a new Matter device using a pairing code.

    The code can be a QR code string (MT:...) or a manual numeric
    pairing code. A USB Bluetooth adapter is required on the server
    for BLE-based commissioning of new devices. Devices already
    paired to another controller (e.g., Hubitat) can be commissioned
    via on-network commissioning without BLE.

    Args:
        body: Contains the pairing code
    """
    from services.matter_client import get_matter_client

    client = get_matter_client()
    if not client.is_connected:
        connected = await client.connect()
        if not connected:
            raise HTTPException(
                status_code=503,
                detail="Cannot connect to matter-server"
            )

    try:
        result = await client.commission_with_code(body.code)
        return {"message": "Device commissioned", "node": result}
    except Exception as e:
        logger.error(f"Matter commissioning failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/matter/map", tags=["matter"])
async def matter_mappings():
    """Get all Hubitat-to-Matter device mappings."""
    from services.matter_client import get_all_matter_mappings
    return get_all_matter_mappings()


@app.post("/api/matter/map", tags=["matter"])
async def matter_create_mapping(body: MatterMapRequest):
    """
    Map a Hubitat device to a Matter node.

    After mapping, commands sent to this Hubitat device will also be
    sent via the Matter protocol for faster local control.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    try:
        resp = req.post(
            f"{postgrest_url}/device_matter_map",
            json={
                "hubitat_device_id": body.hubitat_device_id,
                "matter_node_id": body.matter_node_id,
                "matter_endpoint_id": body.matter_endpoint_id,
                "device_name": body.device_name
            },
            headers={
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates"
            },
            timeout=5
        )
        if resp.ok:
            return {"message": "Mapping created"}
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create Matter mapping: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/matter/map/{hubitat_device_id}", tags=["matter"])
async def matter_delete_mapping(hubitat_device_id: str):
    """Remove a Hubitat-to-Matter device mapping."""
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    try:
        resp = req.delete(
            f"{postgrest_url}/device_matter_map",
            params={"hubitat_device_id": f"eq.{hubitat_device_id}"},
            timeout=5
        )
        if resp.ok:
            return {"message": "Mapping deleted"}
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete Matter mapping: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/matter/discover", tags=["matter"])
async def matter_discover():
    """
    Discover Matter devices from all configured Hubitat hubs.

    Queries each hub's /hub/matterDetails/json endpoint, deduplicates
    by unique_id, and stores results in hubitat_matter_devices table.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Collect hub IPs to scan
    hubs = []
    main_ip = os.environ.get('HUBITAT_HUB_IP_MAIN')
    if main_ip:
        hubs.append({"ip": main_ip, "name": "main"})
    for i in range(1, 4):
        ip = os.environ.get(f'HUBITAT_HUB_IP_OTHER_HUB_{i}')
        if ip:
            hubs.append({"ip": ip, "name": f"other_hub_{i}"})

    # First, get all Maker API devices for name matching
    from services.hubitat_client import get_default_client
    maker_devices = []
    try:
        client = get_default_client()
        maker_devices = client.get_all_devices() or []
    except Exception as e:
        logger.warning(f"Could not load Maker API devices for matching: {e}")

    # Build name-lookup index (lowercase name → device)
    maker_by_name = {}
    for d in maker_devices:
        name = (d.get('label') or d.get('name') or '').strip().lower()
        if name:
            maker_by_name[name] = d

    discovered = []
    errors = []

    for hub in hubs:
        try:
            resp = req.get(
                f"http://{hub['ip']}/hub/matterDetails/json",
                timeout=10
            )
            if not resp.ok:
                errors.append(f"{hub['name']} ({hub['ip']}): HTTP {resp.status_code}")
                continue

            data = resp.json()
            if not data.get('enabled'):
                continue

            for device in data.get('devices', []):
                unique_id = device.get('uniqueId', '')
                if not unique_id:
                    continue

                matter_name = (device.get('name') or '').strip()

                # Try to match against Maker API devices by name
                maker_match = None
                match_confidence = 'none'
                name_lower = matter_name.lower()

                # Exact match
                if name_lower in maker_by_name:
                    maker_match = maker_by_name[name_lower]
                    match_confidence = 'exact'
                else:
                    # Fuzzy: check if Matter name is contained in or contains a Maker name
                    for mk_name, mk_dev in maker_by_name.items():
                        if name_lower in mk_name or mk_name in name_lower:
                            maker_match = mk_dev
                            match_confidence = 'fuzzy'
                            break

                row = {
                    "unique_id": unique_id,
                    "device_name": matter_name,
                    "manufacturer": device.get('manufacturer', ''),
                    "model": device.get('model', ''),
                    "ip_address": device.get('ipAddress', ''),
                    "is_online": device.get('online', False),
                    "hub_ip": hub['ip'],
                    "hub_name": hub['name'],
                    "hubitat_node_id": device.get('nodeId', 0),
                    "hubitat_device_id": str(device.get('id', '')),
                    "hubitat_dni": device.get('dni', ''),
                }

                if maker_match:
                    row["maker_api_device_id"] = str(maker_match.get('id', ''))
                    row["maker_api_device_name"] = maker_match.get('label') or maker_match.get('name', '')
                    row["match_confidence"] = match_confidence

                discovered.append(row)

                # Upsert into database (dedup by unique_id)
                req.post(
                    f"{postgrest_url}/hubitat_matter_devices",
                    json=row,
                    headers={
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates"
                    },
                    timeout=5
                )

        except Exception as e:
            errors.append(f"{hub['name']} ({hub['ip']}): {str(e)}")

    matched = sum(1 for d in discovered if d.get('match_confidence', 'none') != 'none')
    return {
        "discovered": len(discovered),
        "matched": matched,
        "hubs_scanned": len(hubs),
        "errors": errors
    }


@app.get("/api/matter/hubitat-devices", tags=["matter"])
async def matter_hubitat_devices():
    """Get all discovered Hubitat Matter devices from database."""
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    try:
        resp = req.get(
            f"{postgrest_url}/hubitat_matter_devices",
            params={"order": "device_name.asc"},
            headers={"Accept": "application/json"},
            timeout=5
        )
        if resp.ok:
            return resp.json()
        return []
    except Exception as e:
        logger.error(f"Failed to get hubitat matter devices: {e}")
        return []


class UpdateMatterDeviceMatchRequest(BaseModel):
    """Request body for manually correcting a Matter-to-Maker API match."""
    unique_id: str
    maker_api_device_id: str


@app.patch("/api/matter/hubitat-devices/match", tags=["matter"])
async def matter_update_match(body: UpdateMatterDeviceMatchRequest):
    """
    Manually correct the Maker API device match for a Hubitat Matter device.

    Used when auto-matching by name got it wrong. The user selects the
    correct Maker API device from a dropdown.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Get the Maker API device name for display
    maker_name = ''
    try:
        from services.hubitat_client import get_default_client
        client = get_default_client()
        device = client.get_device(body.maker_api_device_id)
        if device:
            maker_name = device.get('label') or device.get('name', '')
    except Exception:
        pass

    try:
        resp = req.patch(
            f"{postgrest_url}/hubitat_matter_devices",
            params={"unique_id": f"eq.{body.unique_id}"},
            json={
                "maker_api_device_id": body.maker_api_device_id,
                "maker_api_device_name": maker_name,
                "match_confidence": "manual"
            },
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        if resp.ok:
            return {"message": "Match updated", "maker_api_device_name": maker_name}
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AutoCommissionRequest(BaseModel):
    """Request body for auto-commissioning a Hubitat Matter device."""
    unique_id: str


@app.post("/api/matter/auto-commission", tags=["matter"])
async def matter_auto_commission(body: AutoCommissionRequest):
    """
    Auto-commission a Hubitat Matter device into our matter-server.

    Steps:
    1. Look up device in hubitat_matter_devices by unique_id
    2. Call Hubitat's openPairingWindow to get a setup code
    3. Commission into our matter-server using that code
    4. Create the device_matter_map entry
    5. Update hubitat_matter_devices with our_node_id

    This is the one-click commission flow.
    """
    import requests as req
    from services.matter_client import get_matter_client

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Step 1: Look up device
    resp = req.get(
        f"{postgrest_url}/hubitat_matter_devices",
        params={"unique_id": f"eq.{body.unique_id}"},
        headers={"Accept": "application/json"},
        timeout=5
    )
    if not resp.ok or not resp.json():
        raise HTTPException(status_code=404, detail="Device not found in discovery table")

    device = resp.json()[0]

    if not device.get('is_online'):
        raise HTTPException(status_code=400, detail=f"Device '{device['device_name']}' is offline")

    # Step 2: Open pairing window on Hubitat hub
    hub_ip = device['hub_ip']
    hubitat_node = device['hubitat_node_id']

    try:
        pair_resp = req.get(
            f"http://{hub_ip}/hub/matter/openPairingWindow",
            params={"node": hubitat_node},
            timeout=90
        )
        if not pair_resp.ok:
            raise HTTPException(
                status_code=502,
                detail=f"Hubitat returned {pair_resp.status_code} opening pairing window"
            )
        pair_data = pair_resp.json()
        setup_code = pair_data.get('setupCode') or pair_data.get('code') or pair_data.get('pairingCode')
        if not setup_code:
            # Maybe the response IS the code as a string
            if isinstance(pair_data, str):
                setup_code = pair_data
            else:
                logger.warning(f"Pairing window response: {pair_data}")
                raise HTTPException(
                    status_code=502,
                    detail=f"No setup code in Hubitat response: {pair_data}"
                )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to open pairing window on Hubitat: {e}"
        )

    # Step 3: Commission into our matter-server
    client = get_matter_client()
    if not client.is_connected:
        connected = await client.connect()
        if not connected:
            raise HTTPException(status_code=503, detail="Cannot connect to matter-server")

    try:
        result = await client.commission_with_code(str(setup_code))
        our_node_id = result.get('node_id') if isinstance(result, dict) else None
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"matter-server commission failed: {e}"
        )

    # Step 4: Create device_matter_map entry
    if our_node_id is not None:
        req.post(
            f"{postgrest_url}/device_matter_map",
            json={
                "hubitat_device_id": device['hubitat_device_id'],
                "matter_node_id": our_node_id,
                "matter_endpoint_id": 1,
                "device_name": device['device_name']
            },
            headers={
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates"
            },
            timeout=5
        )

    # Step 5: Update hubitat_matter_devices with our node ID
    req.patch(
        f"{postgrest_url}/hubitat_matter_devices",
        params={"unique_id": f"eq.{body.unique_id}"},
        json={
            "our_node_id": our_node_id,
            "is_commissioned": True
        },
        headers={"Content-Type": "application/json"},
        timeout=5
    )

    return {
        "message": f"Commissioned '{device['device_name']}'",
        "our_node_id": our_node_id,
        "hubitat_device_id": device['hubitat_device_id'],
        "setup_code_used": setup_code[:8] + "..." if setup_code else None
    }


@app.post("/api/matter/auto-commission-all", tags=["matter"])
async def matter_auto_commission_all():
    """
    Auto-commission ALL discovered, online, uncommissioned Hubitat Matter devices.

    Runs up to 3 commissions in parallel (limited by semaphore to avoid
    overwhelming the Hubitat hub or matter-server). Each device gets its
    pairing window opened and is commissioned independently.
    """
    import asyncio
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Get all online, uncommissioned devices
    resp = req.get(
        f"{postgrest_url}/hubitat_matter_devices",
        params={
            "is_online": "eq.true",
            "is_commissioned": "eq.false"
        },
        headers={"Accept": "application/json"},
        timeout=5
    )
    if not resp.ok:
        raise HTTPException(status_code=502, detail="Failed to query discovered devices")

    devices = resp.json()
    if not devices:
        return {"message": "No online uncommissioned devices found", "commissioned": 0, "failed": 0, "results": []}

    # Full parallelism — all devices commission concurrently
    sem = asyncio.Semaphore(len(devices))

    async def commission_one(device):
        """Commission a single device, respecting the semaphore."""
        unique_id = device['unique_id']
        device_name = device.get('device_name', unique_id)
        async with sem:
            try:
                body = AutoCommissionRequest(unique_id=unique_id)
                result = await matter_auto_commission(body)
                return {"device": device_name, "status": "ok", "node_id": result.get("our_node_id")}
            except HTTPException as e:
                logger.warning(f"Auto-commission failed for {device_name}: {e.detail}")
                return {"device": device_name, "status": "error", "detail": e.detail}
            except Exception as e:
                logger.warning(f"Auto-commission failed for {device_name}: {e}")
                return {"device": device_name, "status": "error", "detail": str(e)}

    # Fire all commissions concurrently (semaphore limits to 3 at a time)
    results = await asyncio.gather(*[commission_one(d) for d in devices])

    commissioned = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")

    return {
        "message": f"Commissioned {commissioned}/{len(devices)} devices",
        "commissioned": commissioned,
        "failed": failed,
        "results": results
    }


# =============================================================================
# E2E Testing
# =============================================================================


@app.get("/api/e2e/events/stream", tags=["e2e-testing"])
async def e2e_event_stream(instance_id: int = Query(...)):
    """
    SSE endpoint for E2E test events.

    Streams test execution progress (step start/complete, scenario summaries)
    for a specific instance. The frontend connects with:
        new EventSource('/api/e2e/events/stream?instance_id=2')

    Note: Live device state comes from a direct WebSocket to Hub4's
    EventSocket, not from this SSE stream. This stream is only for
    test runner progress.
    """
    from fastapi.responses import StreamingResponse
    from services.e2e_events import get_e2e_broadcaster
    import json

    broadcaster = get_e2e_broadcaster()

    async def generate():
        # Initial keepalive comment (SSE spec: lines starting with ':')
        yield ": connected\n\n"

        async for event in broadcaster.subscribe(instance_id):
            if event is None:
                yield ": keepalive\n\n"
            else:
                yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable nginx buffering for SSE
        }
    )


@app.get("/api/e2e/test/{instance_id}/scenarios", tags=["e2e-testing"])
async def get_test_scenarios(instance_id: int):
    """
    Get available test scenarios for an instance.

    Scenarios are built dynamically from the instance's device_selections
    and settings. Only scenarios relevant to the configured devices are
    returned (e.g., no dim level test if useDim is disabled).
    """
    from services.e2e_test_runner import E2ETestRunner

    runner = E2ETestRunner(instance_id)
    await runner.initialize()
    return runner.get_scenarios()


@app.get("/api/e2e/test/{instance_id}/devices", tags=["e2e-testing"])
async def get_test_devices(instance_id: int):
    """
    Get all devices for an instance with their current states.

    Returns devices grouped by category (motion_sensors, switches,
    pause_buttons, pause_switches), with live attribute data fetched
    directly from Hubitat Maker API (not from cache).

    The Maker API token is used server-side only — never exposed
    to the browser.
    """
    from services.instance_manager import get_instance_manager
    from services.hubitat_client import get_default_client

    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    client = get_default_client()
    device_selections = instance.get("device_selections", {})

    result = {}
    for category, device_ids in device_selections.items():
        devices = []
        for did in device_ids:
            device = client.get_device(str(did))
            if device:
                devices.append(device)
            else:
                devices.append({
                    "id": did,
                    "label": f"Device {did}",
                    "error": "not found in Maker API"
                })
        result[category] = devices

    return {
        "instance_id": instance_id,
        "label": instance.get("label"),
        "settings": instance.get("settings", {}),
        "device_categories": result
    }


@app.post("/api/e2e/test/{instance_id}/run/{scenario_id}", tags=["e2e-testing"])
async def run_test_scenario(instance_id: int, scenario_id: str):
    """
    Run a specific test scenario.

    Executes the scenario steps asynchronously in a background task.
    Progress is streamed via the SSE endpoint. The HTTP response
    returns immediately with a confirmation.
    """
    from services.e2e_test_runner import E2ETestRunner
    import asyncio

    runner = E2ETestRunner(instance_id)
    await runner.initialize()

    async def run_in_background():
        """Run scenario in background task so HTTP response returns fast."""
        try:
            await runner.run_scenario(scenario_id)
        except Exception as e:
            logger.error(f"E2E scenario '{scenario_id}' failed: {e}", exc_info=True)

    asyncio.create_task(run_in_background())
    return {
        "message": f"Scenario '{scenario_id}' started",
        "instance_id": instance_id
    }


@app.post("/api/e2e/test/{instance_id}/run-all", tags=["e2e-testing"])
async def run_all_test_scenarios(instance_id: int):
    """
    Run all test scenarios for an instance sequentially.

    Scenarios execute one after another in a background task.
    Progress is streamed via the SSE endpoint.
    """
    from services.e2e_test_runner import E2ETestRunner
    import asyncio

    runner = E2ETestRunner(instance_id)
    await runner.initialize()

    async def run_all():
        """Run all scenarios sequentially."""
        for scenario in runner.get_scenarios():
            try:
                await runner.run_scenario(scenario["id"])
            except Exception as e:
                logger.error(
                    f"E2E scenario '{scenario['id']}' failed: {e}",
                    exc_info=True
                )

    asyncio.create_task(run_all())
    return {"message": "All scenarios started", "instance_id": instance_id}


@app.get("/api/modes", tags=["modes"])
async def get_modes():
    """Get available location modes."""
    from services.hubitat_client import get_default_client

    try:
        client = get_default_client()
        modes = client.get_modes()
        return modes

    except Exception as e:
        logger.error(f"Failed to get modes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/modes/current", tags=["modes"])
async def get_current_mode():
    """Get current location mode."""
    from services.hubitat_client import get_default_client

    try:
        client = get_default_client()
        mode_id, mode_name = client.get_current_mode()
        return {"id": mode_id, "name": mode_name}

    except Exception as e:
        logger.error(f"Failed to get current mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Web UI
# =============================================================================


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    """Main dashboard."""
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/instance/new", response_class=HTMLResponse, include_in_schema=False)
async def new_instance(request: Request):
    """Instance creation wizard."""
    return templates.TemplateResponse(request, "instance_wizard.html")


@app.get("/api/instances/{instance_id}/events", tags=["instances"])
async def stream_instance_events(instance_id: int):
    """Recent events for an instance's subscribed devices."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # Collect all device IDs from instance's device_selections
    device_ids = []
    for ids in (instance.get('device_selections') or {}).values():
        device_ids.extend(str(d) for d in ids)

    if not device_ids:
        return []

    try:
        import requests as req
        postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        response = req.get(
            f"{postgrest_url}/event_log",
            params={
                "hubitat_device_id": f"in.({','.join(device_ids)})",
                "order": "received_at.desc",
                "limit": "50"
            },
            timeout=5
        )
        if response.status_code == 200:
            return response.json()
        return []
    except Exception as e:
        logger.error(f"Failed to get instance events: {e}")
        return []


@app.get("/matter", response_class=HTMLResponse, include_in_schema=False)
async def matter_page(request: Request):
    """Matter device management page."""
    return templates.TemplateResponse(request, "matter.html")


@app.get("/instance/{instance_id}", response_class=HTMLResponse, include_in_schema=False)
async def instance_detail(request: Request, instance_id: int):
    """Instance detail/edit page."""
    return templates.TemplateResponse(
        request, "instance_detail.html", {"instance_id": instance_id}
    )


# =============================================================================
# Error Handlers
# =============================================================================


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    """Handle 404 errors — JSON for API routes, HTML for web routes."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=404,
            content={"error": exc.detail if hasattr(exc, "detail") else "Not found"},
        )
    return templates.TemplateResponse(
        request, "error.html", {"error": "Page not found"}, status_code=404
    )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc: Exception):
    """Handle 500 errors — JSON for API routes, HTML for web routes."""
    logger.error(f"Server error: {exc}")
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"},
        )
    return templates.TemplateResponse(
        request, "error.html", {"error": "Server error"}, status_code=500
    )


# =============================================================================
# Development server
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=True)
