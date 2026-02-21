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
    yield
    logger.info("Shutting down...")


# ---------------------------------------------------------------------------
# Create FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="0_SMART_HOME",
    description="Hubitat automation system with multi-instance support",
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
        return nodes
    except Exception as e:
        logger.error(f"Failed to get Matter nodes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
