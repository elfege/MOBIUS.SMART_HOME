"""
0_MOBIUS.SMART_HOME FastAPI Application

Main entry point for the smart home automation system.
Provides REST API for instance management, device access, and webhook handling.
Serves Jinja2 templates for the web UI.
"""

import os
import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, Query, HTTPException, WebSocket, WebSocketDisconnect
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


def run_db_migrations():
    """
    Run lightweight ALTER TABLE migrations on startup via psycopg2.

    These are idempotent (IF NOT EXISTS) so safe to run every boot.
    Needed because init-db.sql only runs on first DB creation.
    """
    import psycopg2

    db_host = os.environ.get('POSTGRES_HOST', 'postgres')
    db_port = os.environ.get('POSTGRES_PORT', '5432')
    db_name = os.environ.get('POSTGRES_DB', 'smarthome')
    db_user = os.environ.get('POSTGRES_USER', 'smarthome_api')
    db_pass = os.environ.get('POSTGRES_PASSWORD', '')

    migrations = [
        # Commission retry tracking columns (added 2026-02-22)
        "ALTER TABLE hubitat_matter_devices "
        "ADD COLUMN IF NOT EXISTS commission_attempts INTEGER DEFAULT 0",
        "ALTER TABLE hubitat_matter_devices "
        "ADD COLUMN IF NOT EXISTS last_commission_attempt TIMESTAMPTZ",
        "ALTER TABLE hubitat_matter_devices "
        "ADD COLUMN IF NOT EXISTS last_commission_error TEXT",

        # Device hub mapping table for native-hub command routing (added 2026-02-28)
        """CREATE TABLE IF NOT EXISTS device_hub_mapping (
            device_label VARCHAR(200) NOT NULL,
            native_hub_name VARCHAR(100) NOT NULL,
            native_hub_ip VARCHAR(50) NOT NULL,
            native_device_id VARCHAR(50) NOT NULL,
            protocol VARCHAR(30) NOT NULL DEFAULT 'unknown',
            device_type VARCHAR(200),
            mirrors JSONB DEFAULT '{}',
            is_mesh_linked BOOLEAN DEFAULT false,
            last_classified_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (device_label, native_hub_name)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_device_hub_mapping_label "
        "ON device_hub_mapping(device_label)",
        "CREATE INDEX IF NOT EXISTS idx_device_hub_mapping_hub "
        "ON device_hub_mapping(native_hub_name)",
        "CREATE INDEX IF NOT EXISTS idx_device_hub_mapping_protocol "
        "ON device_hub_mapping(protocol)",

        # Seed all hub configs
        "INSERT INTO hub_config (hub_name, hub_ip, maker_api_app_number, "
        "maker_api_token_env, is_primary) "
        "VALUES ('home_1', '<LAN_IP>', '1717', "
        "'HUBITAT_API_TOKEN_OTHER_HUB_1', false) "
        "ON CONFLICT (hub_name) DO NOTHING",
        "INSERT INTO hub_config (hub_name, hub_ip, maker_api_app_number, "
        "maker_api_token_env, is_primary) "
        "VALUES ('home_2', '<LAN_IP>', '2151', "
        "'HUBITAT_API_TOKEN_OTHER_HUB_2', false) "
        "ON CONFLICT (hub_name) DO NOTHING",
        "INSERT INTO hub_config (hub_name, hub_ip, maker_api_app_number, "
        "maker_api_token_env, is_primary) "
        "VALUES ('home_3', '<LAN_IP>', '1269', "
        "'HUBITAT_API_TOKEN_OTHER_HUB_3', false) "
        "ON CONFLICT (hub_name) DO NOTHING",

        # Grant PostgREST access to new table
        "GRANT SELECT, INSERT, UPDATE, DELETE ON device_hub_mapping TO smarthome_anon",
    ]

    try:
        conn = psycopg2.connect(
            host=db_host, port=db_port,
            dbname=db_name, user=db_user, password=db_pass,
            connect_timeout=5
        )
        conn.autocommit = True
        cur = conn.cursor()
        for sql in migrations:
            cur.execute(sql)
        cur.close()
        conn.close()
        logger.info("DB migrations applied successfully")
    except Exception as e:
        logger.warning(f"DB migration skipped: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: initialize services on startup, cleanup on shutdown."""
    initialize_services()

    # Apply any pending schema migrations
    run_db_migrations()

    # Start Matter discovery background service (scans hubs every 5 min)
    from services.matter_discovery import start_matter_discovery, stop_matter_discovery
    start_matter_discovery(scan_interval=300)

    # Start device cache refresh (Matter-first, Maker API fallback)
    from services.device_cache_refresh import start_cache_refresh, stop_cache_refresh
    refresh_interval = int(os.environ.get('DEVICE_CACHE_REFRESH_INTERVAL', '120'))
    start_cache_refresh(refresh_interval=refresh_interval)

    # Run hub classification on startup (populates device_hub_mapping table).
    # Runs in background thread so it doesn't block app readiness.
    # TILES and DeviceCommander depend on this data for native-hub routing.
    import threading
    def _startup_classification():
        try:
            from services.hub_classifier import run_classification, invalidate_cache
            logger.info("Running startup hub classification...")
            result = run_classification()
            invalidate_cache()
            total = result.get("total_native", 0) if isinstance(result, dict) else 0
            logger.info(f"Startup hub classification complete: {total} native devices mapped")
        except Exception as e:
            logger.warning(f"Startup hub classification failed (will retry on next POST /api/hub/classify): {e}")

    threading.Thread(target=_startup_classification, name="startup-hub-classify", daemon=True).start()

    # Start Samsung TV client (WS + HTTP power-poll background tasks).
    # Config is read from env vars (set in docker-compose or start.sh).
    # on_power_change pushes state to all registered Hubitat callbacks via
    # the blueprint's push_state_changes() so Hubitat stays in sync in real-time.
    from services.samsung_tv_client import get_tv_client
    from apps.samsung_tv.blueprint import push_state_changes as _tv_push, _persist_token

    async def _on_tv_state_change(state) -> None:
        """Bridge TV power OR connection state changes → Hubitat LAN push."""
        nonlocal _tv_client
        await _tv_push(_tv_client)

    async def _on_tv_token_save(new_token: str) -> None:
        """Persist a newly-issued TV auth token so it survives container restarts."""
        os.environ["SAMSUNG_TV_TOKEN"] = new_token
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _persist_token, new_token)

    # Token loaded from env var — populated by start.sh from ./state/samsung_tv_token.txt.
    _saved_token = os.environ.get("SAMSUNG_TV_TOKEN", "")

    _tv_client = get_tv_client(
        tv_ip            = os.environ.get("SAMSUNG_TV_IP",   "<LAN_IP>"),
        mac_address      = os.environ.get("SAMSUNG_TV_MAC",  "AABBCCDDEEFF"),
        token            = _saved_token,
        use_ssl          = os.environ.get("SAMSUNG_TV_SSL",  "true").lower() == "true",
        name             = os.environ.get("SAMSUNG_TV_NAME", "living_room_tv"),
        on_power_change  = _on_tv_state_change,
        on_conn_change   = _on_tv_state_change,
        on_token_save    = _on_tv_token_save,
    )
    await _tv_client.start()

    yield

    stop_cache_refresh()
    stop_matter_discovery()

    # Stop Samsung TV client cleanly
    await _tv_client.stop()

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

from apps.samsung_tv.blueprint import router as samsung_tv_router  # noqa: E402
app.include_router(samsung_tv_router)


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


@app.post("/api/instances/{instance_id}/stop", tags=["instances"])
async def stop_instance(instance_id: int):
    """Kill a running instance (e.g. when entering edit mode).

    The instance stays in the DB but is no longer processing events.
    Call POST .../start or PUT to restart it.
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    was_running = manager.stop_instance(instance_id)
    return {"message": "Instance stopped", "was_running": was_running}


@app.post("/api/instances/{instance_id}/start", tags=["instances"])
async def start_instance(instance_id: int):
    """Start an instance from its current DB state.

    Used after cancelling an edit (instance was stopped on edit entry).
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    # Stop first in case it's somehow still running
    manager.stop_instance(instance_id)
    started = manager._start_from_db(instance_id)
    if started:
        return {"message": "Instance started"}
    raise HTTPException(status_code=500, detail="Failed to start instance")


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

    manager.stop_instance(instance_id)
    manager._rebuild_subscriptions(instance_id)
    manager._start_from_db(instance_id)
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
    from services.hub_classifier import fetch_device_live

    try:
        device = fetch_device_live(device_id)
        if device:
            return device
        raise HTTPException(status_code=404, detail="Device not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get device: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/{device_id}/command", tags=["devices"])
async def send_device_command(device_id: str, body: DeviceCommandRequest):
    """
    Send command to a device via the DeviceCommander.

    Uses threaded execution with nested retries and state verification.

    Args:
        device_id: Hubitat device ID
        body: Command name and optional arguments
    """
    from services.device_commander import get_device_commander

    try:
        commander = get_device_commander()
        result = await commander.send_command(
            device_id=device_id,
            command=body.command,
            args=body.args,
            verify=True,
        )

        if result.success:
            return {
                "message": "Command sent",
                "verified": result.verified,
                "status": result.status.value,
                "actual_state": result.actual_state,
                "expected_state": result.expected_state,
                "retries_used": result.retries_used,
                "elapsed_ms": round(result.elapsed_ms, 1),
            }
        raise HTTPException(
            status_code=500,
            detail=f"Command failed: {result.error}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to send command: {e}", exc_info=True)
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

    try:
        payload = await request.json()
        logger.debug(f"Webhook received: {payload}")

        router = get_webhook_router()
        routed_count = await router.route_event(payload)

        return {"routed_to": routed_count}
    except Exception as e:
        logger.error(f"Webhook event processing failed: {e}", exc_info=True)
        return {"routed_to": 0, "error": str(e)}


@app.post("/api/webhook/mode", tags=["webhooks"])
async def handle_mode_webhook(request: Request):
    """Handle mode change webhook from Hubitat."""
    from services.webhook_router import get_webhook_router

    try:
        payload = await request.json()
        logger.info(f"Mode change webhook: {payload}")

        router = get_webhook_router()
        notified = await router.route_mode_change(payload)

        return {"notified": notified}
    except Exception as e:
        logger.error(f"Mode webhook processing failed: {e}", exc_info=True)
        return {"notified": 0, "error": str(e)}


# =============================================================================
# Modes
# =============================================================================


# =============================================================================
# Hub Classification (native-hub device routing)
# =============================================================================


@app.post("/api/hub/classify", tags=["hub-classification"])
async def run_hub_classification():
    """
    Run device classification across all configured hubs.

    Queries each hub's Maker API, classifies devices as native vs
    mesh-linked, builds cross-reference routing table, and writes
    to the device_hub_mapping table.

    This enables the DeviceCommander to route commands directly to
    the hub that physically owns each device (bypassing Hub Mesh relay).
    """
    from services.hub_classifier import run_classification, invalidate_cache
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        # Run classification in executor to avoid blocking event loop
        # (makes HTTP requests to all 4 hubs)
        result = await loop.run_in_executor(None, run_classification)
        # Invalidate in-memory routing cache so new data is picked up
        invalidate_cache()
        return result
    except Exception as e:
        logger.error(f"Hub classification failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hub/mapping", tags=["hub-classification"])
async def get_hub_mapping(
    hub_name: Optional[str] = Query(None, description="Filter by native hub name"),
    protocol: Optional[str] = Query(None, description="Filter by protocol"),
):
    """
    Get the current device-to-hub mapping table.

    Returns all classified devices with their native hub, protocol,
    and mirror info. Optionally filter by hub or protocol.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    params = {}
    if hub_name:
        params["native_hub_name"] = f"eq.{hub_name}"
    if protocol:
        params["protocol"] = f"eq.{protocol}"
    params["order"] = "device_label.asc"

    try:
        resp = req.get(
            f"{postgrest_url}/device_hub_mapping",
            params=params,
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            entries = resp.json()
            return {
                "count": len(entries),
                "entries": entries,
            }
        return {"error": f"PostgREST returned {resp.status_code}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hub/mapping/stats", tags=["hub-classification"])
async def get_hub_mapping_stats():
    """
    Get summary statistics for the device hub mapping.

    Returns per-hub and per-protocol counts.
    """
    import requests as req
    from collections import defaultdict

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    try:
        resp = req.get(
            f"{postgrest_url}/device_hub_mapping",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"error": f"PostgREST returned {resp.status_code}"}

        entries = resp.json()
        hub_counts = defaultdict(int)
        proto_counts = defaultdict(int)
        hub_proto = defaultdict(lambda: defaultdict(int))

        for e in entries:
            hub = e.get("native_hub_name", "unknown")
            proto = e.get("protocol", "unknown")
            hub_counts[hub] += 1
            proto_counts[proto] += 1
            hub_proto[hub][proto] += 1

        return {
            "total": len(entries),
            "by_hub": dict(hub_counts),
            "by_protocol": dict(proto_counts),
            "hub_protocol_matrix": {
                hub: dict(protos) for hub, protos in hub_proto.items()
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        from services.hub_classifier import fetch_device_live
        device = fetch_device_live(body.maker_api_device_id)
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
    from services.hub_classifier import fetch_device_live

    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    device_selections = instance.get("device_selections", {})

    result = {}
    for category, device_ids in device_selections.items():
        devices = []
        for did in device_ids:
            # Selection ids are canonical PKs (Phase 5). fetch_device_live
            # resolves them to (hub, hubitat_id) and queries the right hub.
            device = fetch_device_live(did)
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


@app.post("/api/e2e/test/{instance_id}/stop", tags=["e2e-testing"])
async def stop_test(instance_id: int):
    """
    Cancel the currently-running scenario for this instance, if any.

    Sets the runner's cancel flag; the scenario loop checks it between
    steps. Returns immediately — does NOT block waiting for the in-flight
    step to actually unwind.
    """
    from services.e2e_test_runner import get_active_runner
    runner = get_active_runner(instance_id)
    if runner is None:
        return {"ok": True, "stopped": False, "reason": "no active run"}
    try:
        await runner.cancel()
        return {"ok": True, "stopped": True}
    except Exception as e:
        logger.error(f"stop_test({instance_id}) failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
        """Run all scenarios with device state save/restore."""
        try:
            # Snapshot device states before any tests run
            await runner.save_device_states()

            # Run all scenarios sequentially
            for scenario in runner.get_scenarios():
                try:
                    await runner.run_scenario(scenario["id"])
                except Exception as e:
                    logger.error(
                        f"E2E scenario '{scenario['id']}' failed: {e}",
                        exc_info=True
                    )
        finally:
            # Restore devices to their original states regardless of test outcome
            try:
                await runner.restore_device_states()
            except Exception as e:
                logger.error(
                    f"E2E device state restore failed: {e}",
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


@app.get("/hubs", response_class=HTMLResponse, include_in_schema=False)
async def hubs_page(request: Request):
    """Hub configuration page — edit hub_config rows."""
    return templates.TemplateResponse(request, "hubs.html")


# =============================================================================
# Hub config CRUD
# =============================================================================
# All routing in the app reads from hub_config (joined into devices via
# hub_id FK). Editing hub_config from the UI lets the user change a hub's
# IP / app number / token env without redeploying. After every write we
# invalidate the in-process lookup caches so changes take effect within
# a single event-loop tick.

@app.get("/api/canonical-devices", tags=["devices"])
async def list_canonical_devices():
    """
    List all rows in the canonical `devices` table.
    Used by the wizard to render chips with labels for any saved selection,
    even when the selection's device id doesn't appear in the current
    category's capability-filtered device list.
    """
    import requests as _requests
    try:
        r = _requests.get(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/devices",
            params={"select": "id,label,hub_ip,hubitat_id", "order": "label"},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"list_canonical_devices failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hubs", tags=["hubs"])
async def list_hubs():
    """List all configured Hubitat hubs (rows of hub_config)."""
    import requests as _requests
    try:
        r = _requests.get(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/hub_config",
            params={"order": "id"},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"list_hubs failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _invalidate_hub_caches():
    """Drop in-process caches that reference hub_config rows."""
    try:
        from services.hub_classifier import invalidate_device_lookup_cache
        invalidate_device_lookup_cache()
    except Exception:
        pass
    try:
        from services.webhook_router import get_webhook_router
        get_webhook_router().invalidate_device_cache()
    except Exception:
        pass


@app.patch("/api/hubs/{hub_id}", tags=["hubs"])
async def update_hub(hub_id: int, body: Dict[str, Any]):
    """
    Update a hub_config row. Accepts any subset of:
      hub_name, hub_ip, maker_api_app_number, maker_api_token_env,
      is_primary, is_enabled.
    Other fields are ignored.

    On success, invalidates the in-process device-lookup caches and
    re-syncs `devices.hub_ip` from the new `hub_config.hub_ip` for any
    rows that referenced this hub (denormalized cache stays consistent).
    """
    import requests as _requests
    allowed = {
        "hub_name", "hub_ip", "maker_api_app_number",
        "maker_api_token_env", "is_primary", "is_enabled",
    }
    patch = {k: v for k, v in body.items() if k in allowed}
    if not patch:
        raise HTTPException(status_code=400, detail="No editable fields in body")

    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")

    # If the user is setting is_primary=true, clear it on every other row
    # first — exactly one primary at a time.
    if patch.get("is_primary") is True:
        try:
            _requests.patch(
                f"{postgrest_url}/hub_config",
                params={"id": f"neq.{hub_id}"},
                json={"is_primary": False},
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"Could not clear is_primary on others: {e}")

    try:
        r = _requests.patch(
            f"{postgrest_url}/hub_config",
            params={"id": f"eq.{hub_id}"},
            json=patch,
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=5,
        )
        if r.status_code not in (200, 204):
            raise HTTPException(status_code=r.status_code, detail=r.text)

        # If hub_ip changed, mirror it into devices.hub_ip (denormalized).
        if "hub_ip" in patch:
            try:
                _requests.patch(
                    f"{postgrest_url}/devices",
                    params={"hub_id": f"eq.{hub_id}"},
                    json={"hub_ip": patch["hub_ip"]},
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
            except Exception as e:
                logger.warning(f"Could not resync devices.hub_ip: {e}")

        _invalidate_hub_caches()
        return r.json() if r.status_code == 200 else {"ok": True, "id": hub_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"update_hub({hub_id}) failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/hubs", tags=["hubs"])
async def create_hub(body: Dict[str, Any]):
    """Create a new hub_config row."""
    import requests as _requests
    required = ("hub_name", "hub_ip", "maker_api_app_number", "maker_api_token_env")
    missing = [k for k in required if not body.get(k)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {missing}")

    payload = {k: body[k] for k in required}
    payload["is_primary"] = bool(body.get("is_primary", False))
    payload["is_enabled"] = bool(body.get("is_enabled", True))

    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        r = _requests.post(
            f"{postgrest_url}/hub_config",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=5,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=r.status_code, detail=r.text)
        _invalidate_hub_caches()
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create_hub failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/hubs/{hub_id}", tags=["hubs"])
async def delete_hub(hub_id: int):
    """
    Delete a hub. Refuses if any device still references this hub via FK.
    User must move or remove those devices first (or run a fresh classifier
    cycle that lets them be re-homed).
    """
    import requests as _requests
    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        r = _requests.get(
            f"{postgrest_url}/devices",
            params={"hub_id": f"eq.{hub_id}", "select": "id", "limit": "1"},
            timeout=5,
        )
        if r.status_code == 200 and r.json():
            raise HTTPException(
                status_code=409,
                detail="Hub has devices; remove or re-classify them first",
            )
        d = _requests.delete(
            f"{postgrest_url}/hub_config",
            params={"id": f"eq.{hub_id}"},
            timeout=5,
        )
        if d.status_code not in (200, 204):
            raise HTTPException(status_code=d.status_code, detail=d.text)
        _invalidate_hub_caches()
        return {"ok": True, "id": hub_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_hub({hub_id}) failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/instance/{instance_id}", response_class=HTMLResponse, include_in_schema=False)
async def instance_detail(request: Request, instance_id: int):
    """Instance detail/edit page."""
    return templates.TemplateResponse(
        request, "instance_detail.html", {"instance_id": instance_id}
    )


# =============================================================================
# Dashboard WebSocket (real-time updates — replaces polling)
# =============================================================================


@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for real-time dashboard updates.

    Replaces the 30-second polling loop. When Hubitat webhooks arrive,
    events are pushed instantly to all connected dashboard clients.
    The frontend uses these events to patch individual cards instead
    of re-rendering the entire grid (no more flicker).

    Message types sent to client:
        - device_event: A device changed state
        - instance_update: Instance metadata changed
        - instances_snapshot: Full instance list (sent on connect)
    """
    from services.dashboard_broadcaster import get_dashboard_broadcaster
    from services.instance_manager import get_instance_manager
    import json

    await websocket.accept()
    broadcaster = get_dashboard_broadcaster()
    queue = await broadcaster.connect()

    try:
        # Send initial snapshot so the client doesn't need a separate fetch
        manager = get_instance_manager()
        instances = manager.get_all_instances()
        await websocket.send_json({
            "type": "instances_snapshot",
            "instances": instances
        })

        # Stream events to client
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                # Keepalive ping — detect dead connections
                await websocket.send_json({"type": "ping"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"Dashboard WS closed: {e}")
    finally:
        await broadcaster.disconnect(queue)


# =============================================================================
# KPI Metrics
# =============================================================================


@app.get("/api/instances/{instance_id}/metrics", tags=["instances"])
async def get_instance_metrics(
    instance_id: int,
    hours: int = Query(24, description="Lookback window in hours")
):
    """
    Aggregated KPI metrics for a single instance.

    Queries event_log for the instance's subscribed devices and computes:
    - Event counts (total, per-hour, per-device, per-type)
    - Last activity timestamps per device
    - Switch on/off operation counts
    - Motion active/inactive ratios
    - Error tracking from app_instances table

    Args:
        instance_id: Target instance
        hours: Lookback window (default 24h)
    """
    from services.instance_manager import get_instance_manager
    import requests as req
    from datetime import datetime, timedelta, timezone

    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # Collect device IDs from instance
    device_ids = []
    for ids in (instance.get('device_selections') or {}).values():
        device_ids.extend(str(d) for d in ids)

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()

    # Fetch events for this instance's devices within the time window
    events = []
    if device_ids:
        try:
            resp = req.get(
                f"{postgrest_url}/event_log",
                params={
                    "hubitat_device_id": f"in.({','.join(device_ids)})",
                    "received_at": f"gte.{since}",
                    "order": "received_at.desc",
                    "limit": "2000"
                },
                timeout=10
            )
            if resp.status_code == 200:
                events = resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch metrics events: {e}")

    # Compute aggregations
    total_events = len(events)

    # Events per hour (for chart)
    hourly_buckets = {}
    for h in range(hours):
        bucket_time = now - timedelta(hours=h)
        key = bucket_time.strftime('%Y-%m-%dT%H:00:00')
        hourly_buckets[key] = 0

    # Per-device stats
    device_stats = {}
    # Per-event-type counts
    type_counts = {}

    for evt in events:
        # Hourly bucketing
        received = evt.get('received_at', '')
        if received:
            try:
                # Truncate to hour
                hour_key = received[:13] + ':00:00'
                if hour_key in hourly_buckets:
                    hourly_buckets[hour_key] += 1
            except (IndexError, TypeError):
                pass

        # Device stats
        dev_id = evt.get('hubitat_device_id', '')
        dev_name = evt.get('device_name', dev_id)
        evt_type = evt.get('event_type', '')
        evt_value = evt.get('event_value', '')

        if dev_id not in device_stats:
            device_stats[dev_id] = {
                'device_name': dev_name,
                'event_count': 0,
                'last_event_at': received,
                'last_event_type': evt_type,
                'last_event_value': evt_value,
                'type_breakdown': {}
            }
        device_stats[dev_id]['event_count'] += 1

        # Type breakdown per device
        if evt_type not in device_stats[dev_id]['type_breakdown']:
            device_stats[dev_id]['type_breakdown'][evt_type] = {
                'count': 0,
                'last_value': evt_value,
                'last_at': received
            }
        device_stats[dev_id]['type_breakdown'][evt_type]['count'] += 1

        # Global type counts
        type_counts[evt_type] = type_counts.get(evt_type, 0) + 1

    # Sort hourly buckets chronologically
    hourly_sorted = sorted(hourly_buckets.items())

    # Instance metadata
    running = manager.get_running_instance(instance_id) is not None

    return {
        "instance_id": instance_id,
        "label": instance.get("label"),
        "is_paused": instance.get("is_paused", False),
        "is_running": running,
        "error_count": instance.get("error_count", 0),
        "last_error": instance.get("last_error"),
        "last_activity_at": instance.get("last_activity_at"),
        "created_at": instance.get("created_at"),
        "device_count": len(device_ids),
        "window_hours": hours,
        "total_events": total_events,
        "hourly_events": [
            {"hour": h, "count": c} for h, c in hourly_sorted
        ],
        "device_stats": device_stats,
        "type_counts": type_counts,
        "device_selections": instance.get("device_selections", {}),
        "settings": instance.get("settings", {}),
    }


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
