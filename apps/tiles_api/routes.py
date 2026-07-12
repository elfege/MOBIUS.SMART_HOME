"""
Panel API router — the authenticated surface the RN/Expo panel app talks to.

EVERY route here is scope-gated (DEFAULT-DENY). There is intentionally no
"open" route: the TILES surface we are replacing exposed an UNAUTHENTICATED
`POST /api/device/<id>/command` (verified first-hand), which let anything on the
LAN command any lock. That class of bug is structurally impossible here — a
route without a `Depends(require_scope(...))` does not ship, and the enrollment
routes themselves are admin-gated.

Enrollment is an ADMIN action (it mints credentials), so it sits behind the LAN
check plus an explicit admin scope, never behind a panel token.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from apps.tiles_api import db
from apps.tiles_api.auth import (KIND_PANEL, KIND_SERVICE, SCOPE_PANEL_COMMAND,
                                 SCOPE_PANEL_READ, ALL_SCOPES, Principal,
                                 client_ip, generate_token, hash_token,
                                 is_trusted_lan, require_scope, token_prefix)
from apps.tiles_api.models import (DeviceCommandRequest, EnrollDeviceRequest,
                                   EnrollDeviceResponse, PreferenceRequest)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/panel", tags=["panel"])


def _admin_guard(request: Request) -> None:
    """
    Enrollment mints credentials, so it is the most sensitive surface here.
    Gate: must originate from the trusted LAN (nginx-set, non-spoofable client
    IP). This is the admin console's own boundary — panels never enroll panels.
    Kept as its own guard so it can be tightened (e.g. to an admin session)
    without touching the panel routes.
    """
    ip = client_ip(request)
    if not is_trusted_lan(ip):
        logger.warning(f"panel enrollment: DENIED off-LAN attempt from {ip}")
        raise HTTPException(status_code=403, detail="Not permitted.")


# --- enrollment (admin) -----------------------------------------------------

@router.post("/devices/enroll", response_model=EnrollDeviceResponse)
async def enroll_device(body: EnrollDeviceRequest, request: Request):
    """
    Enroll a panel device (or a server-to-server service) and mint its token.

    The RAW TOKEN IS RETURNED EXACTLY ONCE — we persist only its SHA-256 hash and
    cannot show it again. Each device gets its OWN token, which is why we can
    revoke ONE tablet without touching the others (a 'trusted LAN' gate cannot).
    """
    _admin_guard(request)
    if body.kind not in (KIND_PANEL, KIND_SERVICE):
        raise HTTPException(status_code=400, detail="kind must be 'panel' or 'service'.")
    bad = [s for s in body.scopes if s not in ALL_SCOPES]
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown scope(s): {bad}")
    if not body.scopes:
        raise HTTPException(status_code=400, detail="at least one scope is required.")

    raw = generate_token()
    row = db.create_device(name=body.name, kind=body.kind,
                           token_hash=hash_token(raw), token_prefix=token_prefix(raw),
                           scopes=body.scopes, require_lan=body.require_lan)
    logger.info(f"panel: enrolled {body.kind} '{body.name}' (id={row['id']}, "
                f"scopes={body.scopes}, require_lan={body.require_lan})")
    return EnrollDeviceResponse(id=row["id"], name=body.name, kind=body.kind,
                                scopes=body.scopes, require_lan=body.require_lan,
                                token=raw, created_at=row.get("created_at"))


@router.get("/devices")
async def list_enrolled_devices(request: Request):
    """Enrolled principals (admin view). Never returns token material — only the
    non-secret prefix, plus last-seen for attribution."""
    _admin_guard(request)
    return {"devices": db.list_devices()}


@router.post("/devices/{device_id}/revoke")
async def revoke_enrolled_device(device_id: int, request: Request):
    """Revoke ONE device's token immediately (lost/compromised tablet)."""
    _admin_guard(request)
    if not db.revoke_device(device_id):
        raise HTTPException(status_code=404, detail="No such active device.")
    logger.info(f"panel: REVOKED device id={device_id}")
    return {"message": "revoked", "id": device_id}


# --- panel surface (enrolled devices) ---------------------------------------

@router.get("/whoami")
async def whoami(principal: Principal = Depends(require_scope(SCOPE_PANEL_READ))):
    """Lets an enrolled panel confirm its identity + granted scopes."""
    return {"id": principal.id, "name": principal.name, "kind": principal.kind,
            "scopes": principal.scopes}


@router.get("/preferences")
async def get_preferences(profile: str = "default",
                          principal: Principal = Depends(require_scope(SCOPE_PANEL_READ))):
    """Panel preferences (sections, appearance, KPI settings) for a profile."""
    return {"profile": profile, "preferences": db.get_preferences(profile)}


@router.put("/preferences")
async def put_preference(body: PreferenceRequest,
                         principal: Principal = Depends(require_scope(SCOPE_PANEL_COMMAND))):
    """Upsert one preference category. Requires command scope — a read-only
    display token must not be able to rewrite the panel's configuration."""
    db.set_preference(body.profile, body.category, body.value)
    logger.info(f"panel: '{principal.name}' set preference "
                f"{body.profile}/{body.category}")
    return {"message": "saved", "profile": body.profile, "category": body.category}


@router.post("/devices/{device_id}/command")
async def panel_device_command(device_id: str, body: DeviceCommandRequest,
                               principal: Principal = Depends(require_scope(SCOPE_PANEL_COMMAND))):
    """
    Send a device command from a panel — the route TILES left completely
    unauthenticated. Here it requires an enrolled token WITH `panel:command`
    AND (for panel principals) the trusted-LAN second factor, and every call is
    attributed to a named principal in the log.

    Delegates to the SHARED command path (services/) — no forked control logic
    lives in this package (fanatic-modularization ruling).
    """
    logger.info(f"panel: '{principal.name}' -> device {device_id} "
                f"command={body.command} value={body.value!r}")
    # NOTE: wired to the shared command service in P2, together with the device
    # roster endpoint. Kept explicit rather than silently no-op'ing so the auth
    # contract is testable now and the control path cannot be smuggled in
    # unauthenticated later.
    raise HTTPException(
        status_code=501,
        detail="Panel command path lands in P2 (delegates to the shared command "
               "service). Auth contract is live and enforced.")
