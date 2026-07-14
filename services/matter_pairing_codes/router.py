"""
matter_pairing_codes.router — the "Get Code" HTTP surface.

    POST /api/matter/pairing-code            get a code for a device (4 sources)
    POST /api/matter/pairing-code/repair     fix a label code whose discriminator drifted
    GET  /api/matter/pairing-code/advertising  what is in pairing mode on the LAN right now

Wiring (Architect's lane, one line in app.py):
    from services.matter_pairing_codes.router import router as matter_codes_router
    app.include_router(matter_codes_router)

HTTP semantics:
    200  a code (with its source + provenance detail)
    409  UnreachableCode — no source applies; the detail explains WHY and the way
         out. This is a legitimate, expected answer, not a server fault: it is
         what "the passcode is a secret we do not hold" looks like over HTTP.
    400  InvalidPairingCode — the supplied code is mistyped (bad check digit).
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.matter_pairing_codes import sources
from services.matter_pairing_codes.manual_code import InvalidPairingCode
from services.matter_pairing_codes.resolver import resolve
from services.matter_pairing_codes.sources import UnreachableCode
from services.matter_pairing_lock import PairingLockBusy

logger = logging.getLogger(__name__)

router = APIRouter(tags=["matter"])


class PairingCodeBody(BaseModel):
    """Body for POST /api/matter/pairing-code.

    Supply whatever the UI knows about the device; the resolver picks the best
    available source. `unique_id` alone is enough for a device in the discovery
    table — the route hydrates the rest from the DB.
    """
    unique_id: Optional[str] = None
    mac: Optional[str] = None
    our_node_id: Optional[int] = None
    hubitat_node_id: Optional[int] = None
    hub_ip: Optional[str] = None
    hub_name: Optional[str] = None
    device_name: Optional[str] = None
    # The printed code, when the operator has it (enables the repair path).
    label_code: Optional[str] = None
    window_seconds: int = sources.DEFAULT_WINDOW_S


class RepairBody(BaseModel):
    """Body for POST /api/matter/pairing-code/repair."""
    code: str


def _hydrate(unique_id: str) -> Dict[str, Any]:
    """Fill in a device's identifiers from the discovery table.

    Returns {} when the id is unknown — the caller still has whatever the UI
    passed, so an unknown id degrades to a normal resolution attempt.
    """
    from services.matter_hub_port.db import connect

    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT unique_id, mac_address, our_node_id, hubitat_node_id,
                          hub_ip, hub_name, device_name
                     FROM dshub.hubitat_matter_devices
                    WHERE unique_id = %s
                    LIMIT 1""",
                (unique_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    return {
        "unique_id": row[0], "mac": row[1], "our_node_id": row[2],
        "hubitat_node_id": row[3], "hub_ip": row[4], "hub_name": row[5],
        "device_name": row[6],
    }


@router.post("/api/matter/pairing-code")
async def get_pairing_code(body: PairingCodeBody):
    """Produce a working pairing code for a device — or explain why none exists.

    Tries, in order: the vault's factory code · a fresh window on our fabric ·
    a fresh window on the device's Hubitat hub · repairing a supplied label code.
    """
    device: Dict[str, Any] = body.model_dump(exclude_none=True)
    # Hydrate missing identifiers from the discovery table, so the UI can pass
    # just a unique_id. Anything the UI DID supply wins over the DB.
    if body.unique_id:
        hydrated = await asyncio.to_thread(_hydrate, body.unique_id)
        for key, value in hydrated.items():
            if value is not None and device.get(key) is None:
                device[key] = value

    try:
        result = await resolve(device, body.label_code, body.window_seconds)
    except InvalidPairingCode as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PairingLockBusy as e:
        # Another Matter pairing is in flight. Opening a window now would storm
        # the radio — the same 409 contract every other pairing path uses.
        raise HTTPException(status_code=409, detail=str(e))
    except UnreachableCode as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error("pairing-code resolution failed: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not get a code: {e}")

    return {
        "manual_code": result.manual,
        "qr_code": result.qr,
        "source": result.source,
        "detail": result.detail,
        "expires_in_s": result.expires_in_s,
        "device_name": device.get("device_name"),
    }


@router.post("/api/matter/pairing-code/repair")
async def repair_pairing_code(body: RepairBody):
    """Re-target a printed code at the discriminator the device ACTUALLY
    advertises (the fix for 'no commissionable device was discovered' when the
    device is plainly sitting there in pairing mode)."""
    try:
        result = await sources.repair_label_code(body.code)
    except InvalidPairingCode as e:
        raise HTTPException(status_code=400, detail=str(e))
    except UnreachableCode as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {
        "manual_code": result.manual,
        "qr_code": result.qr,
        "source": result.source,
        "detail": result.detail,
        "changed": result.manual != body.code.strip(),
    }


@router.get("/api/matter/pairing-code/advertising")
async def advertising_devices():
    """Devices currently in pairing mode on the LAN (mDNS _matterc._udp).

    Each entry reports the FULL 12-bit discriminator and its 4-bit short form —
    the short form is what a manual code must match, and comparing the two is
    how a drifted discriminator is spotted.
    """
    found = await sources.discover_commissionable()
    return {"count": len(found), "devices": found}
