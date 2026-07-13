"""
matter_hub_port.router — the HTTP surface (design §5).

    POST /api/matter/port-devices           start a run (confirmed-gate + 409s)
    GET  /api/matter/port-devices/status    live run state (UI polls this)
    GET  /api/matter/port-devices/preview   eligibility dry-run table

Wiring (Architect's lane): app.include_router(router) — one line in app.py.

The confirmed-gate clones Commission All's: bulk pairing is a USER action,
never automatic; any caller without {"confirmed": true} gets 409 by
construction, so the scan-chain class of bug cannot be reintroduced here.
"""

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.matter_hub_port import orchestrator
from services.matter_hub_port.db import fetch_hub
from services.matter_hub_port.eligibility import (
    HardwareGateError,
    build_preview,
    check_run_gates,
)
from services.matter_hub_port.hub_endpoints import HubEndpointError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["matter"])


class PortDevicesBody(BaseModel):
    """Body for POST /api/matter/port-devices."""
    source_hub_id: int
    target_hub_id: int
    # Preview subset (§9: checkboxes, default all) — lowercase MACs. None/omit
    # = every eligible device on the source hub.
    device_macs: Optional[List[str]] = None
    # Explicit-user gate (same contract as Commission All).
    confirmed: bool = False


async def _load_and_gate(source_hub_id: int, target_hub_id: int):
    """Fetch both hub rows (threaded — psycopg2 is synchronous and this app
    runs a single uvicorn worker; bare blocking I/O in an async route blocks
    the WHOLE event loop, the exact failure that let autoheal restart the
    container mid-commission) and apply the run-level gates.
    Raises HTTPException 400 with the gate's operator-readable reason."""
    source_hub = await asyncio.to_thread(fetch_hub, source_hub_id)
    target_hub = await asyncio.to_thread(fetch_hub, target_hub_id)
    try:
        check_run_gates(source_hub, target_hub)
    except HardwareGateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return source_hub, target_hub


@router.get("/api/matter/port-devices/preview")
async def port_devices_preview(source_hub_id: int, target_hub_id: int):
    """Eligibility dry-run: what WOULD a copy source->target do, per device.

    Scans both hubs live (threaded — blocking I/O never runs on the loop)."""
    source_hub, target_hub = await _load_and_gate(source_hub_id, target_hub_id)
    try:
        rows = await asyncio.to_thread(build_preview, source_hub, target_hub)
    except HubEndpointError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "source_hub": source_hub["hub_name"],
        "target_hub": target_hub["hub_name"],
        "devices": rows,
        "eligible": sum(1 for r in rows if r["verdict"] in ("eligible", "no_mac_identity")),
    }


@router.post("/api/matter/port-devices")
async def port_devices_start(body: PortDevicesBody):
    """Start a hub->hub COPY run (background; poll .../status).

    409 without {"confirmed": true} — a bulk pairing run is a user action,
    never automatic. 409 when a run is already in flight. 400 on gate
    failures (C-5 target, same hub, unknown/disabled hub). The global
    Matter-pairing mutex is acquired inside the worker; contention with
    Commission All or manual pairing aborts the run with an explanatory
    message in status (same holder-visibility contract as the lock)."""
    if not body.confirmed:
        raise HTTPException(
            status_code=409,
            detail="Hub->hub copy requires explicit confirmation "
                   "({\"confirmed\": true}) — it is a user action, never automatic.")

    if orchestrator.run_state().get("running"):
        raise HTTPException(
            status_code=409,
            detail="A hub->hub copy run is already in progress — poll "
                   "/api/matter/port-devices/status.")

    source_hub, target_hub = await _load_and_gate(body.source_hub_id, body.target_hub_id)

    # Flip the running flag SYNCHRONOUSLY (single-worker loop -> atomic w.r.t.
    # a racing second POST), then hand off to the background task.
    run_id = orchestrator.init_run_state(source_hub, target_hub)
    asyncio.create_task(orchestrator.run_port(source_hub, target_hub, body.device_macs))
    logger.info(f"hub-port copy {run_id} started: "
                f"{source_hub['hub_name']} -> {target_hub['hub_name']}")
    return {"started": True, "run_id": run_id,
            "source_hub": source_hub["hub_name"], "target_hub": target_hub["hub_name"]}


@router.get("/api/matter/port-devices/status")
async def port_devices_status():
    """The live run state — per-device results, counters, circuit-breaker
    status, and the final summary message once finished."""
    st = orchestrator.run_state()
    if not st or "run_id" not in st:
        return {"running": False, "message": "no run has been started"}
    return st
