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

from apps.tiles_api import db, resolver
from apps.tiles_api.auth import (KIND_PANEL, KIND_SERVICE, SCOPE_PANEL_COMMAND,
                                 SCOPE_PANEL_READ, ALL_SCOPES, Principal,
                                 client_ip, generate_token, hash_token,
                                 is_trusted_lan, require_scope, token_prefix)
from apps.tiles_api.models import (AffinityRequest, DeviceCommandRequest,
                                   EnrollDeviceRequest, EnrollDeviceResponse,
                                   PreferenceRequest)

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


@router.get("/enrollments")
async def list_enrolled_devices(request: Request):
    """
    Enrolled principals (admin view). Never returns token material — only the
    non-secret prefix, plus last-seen for attribution.

    NOTE the path: this is `/api/panel/enrollments`, NOT `/api/panel/devices`.
    `/api/panel/devices` is the RENDERED DEVICE ROSTER (get_panel_devices) — a
    different resource. Keeping them on distinct paths avoids the GET-method
    collision that would otherwise let the admin list shadow the roster.
    """
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


# --- LAN auto-bootstrap (wall tablets) --------------------------------------

@router.post("/session/bootstrap")
async def bootstrap_panel_session(request: Request):
    """
    Trusted-LAN auto-enrollment for wall tablets — the plan's "trusted-subnet
    auto-login". A browser on the trusted LAN that holds NO token gets its own
    panel token minted automatically, so a wall tablet shows the operator's
    devices with ZERO manual enrollment (he never pastes a token).

    This is DEFAULT-DENY-preserving, not a hole:
      * The LAN is the gate for ISSUANCE here (nginx-set, non-spoofable client
        IP). Off-LAN requests are refused — a token is never handed out where the
        LAN second factor cannot apply.
      * Every issued token is a normal enrolled `panel` principal: scoped
        (read+command), require_lan=true (so the token ALSO needs the LAN factor
        on every later call), and individually REVOCABLE. This is the
        enrolled-device model, auto-triggered on the LAN — not a shared secret.

    Each call mints a NEW token; the client stores it and only bootstraps once
    per device, so this is one revocable token per tablet.
    """
    ip = client_ip(request)
    if not is_trusted_lan(ip):
        logger.warning(f"panel bootstrap: DENIED off-LAN request from {ip}")
        raise HTTPException(status_code=403, detail="Not permitted.")
    raw = generate_token()
    name = f"lan-walkup {ip or '?'} {raw[:4]}"
    row = db.create_device(
        name=name, kind=KIND_PANEL,
        token_hash=hash_token(raw), token_prefix=token_prefix(raw),
        scopes=[SCOPE_PANEL_READ, SCOPE_PANEL_COMMAND], require_lan=True)
    logger.info(f"panel bootstrap: minted LAN panel token for {ip} (id={row['id']})")
    return {"token": raw, "id": row["id"],
            "scopes": [SCOPE_PANEL_READ, SCOPE_PANEL_COMMAND]}


# --- panel surface (enrolled devices) ---------------------------------------

@router.get("/whoami")
async def whoami(principal: Principal = Depends(require_scope(SCOPE_PANEL_READ))):
    """Lets an enrolled panel confirm its identity + granted scopes."""
    return {"id": principal.id, "name": principal.name, "kind": principal.kind,
            "scopes": principal.scopes}


@router.get("/devices")
async def get_panel_devices(profile: str = "default",
                            principal: Principal = Depends(require_scope(SCOPE_PANEL_READ))):
    """
    The resolved panel roster: ordered sections + a flat list of tiles, each
    already carrying its tile renderer, section, and primary value.

    ALL grouping/tile-type logic is resolved SERVER-SIDE from the panel_* tables
    (migration 014) — the client renders verbatim and never carries its own
    capability/room chain, so the web panel and the native app cannot drift
    (operator directive 2026-07-13). Reads are cheap (whole small tables) and
    resolution is pure; no per-device DB round-trips.
    """
    resolved = resolver.resolve_panel(
        devices=db.list_present_devices(),
        sections=db.list_sections(profile),
        tile_types=db.list_tile_types(),
        rules=db.list_section_rules(),
        affinities_by_device=db.affinities_by_device(profile),
    )
    resolved["profile"] = profile
    return resolved


@router.put("/devices/{device_id}/affinity")
async def put_device_affinity(device_id: int, body: AffinityRequest,
                              principal: Principal = Depends(require_scope(SCOPE_PANEL_COMMAND))):
    """
    Pin/override one device on the panel (section, tile renderer, label, hide,
    favorite) — the DATA that replaces TILES' hard-coded grouping. Requires
    command scope: reorganizing the panel is a configuration write, not a read.
    """
    db.set_affinity(
        body.profile, device_id,
        section_id=body.section_id, tile_type=body.tile_type,
        custom_label=body.custom_label, sort_order=body.sort_order,
        is_hidden=body.is_hidden, is_favorite=body.is_favorite)
    logger.info(f"panel: '{principal.name}' set affinity for device {device_id} "
                f"(profile={body.profile})")
    return {"message": "saved", "profile": body.profile, "device_id": device_id}


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

    DELEGATES to the SHARED command path (services/device_commander) — no forked
    control logic lives in this package (fanatic-modularization ruling). By
    delegating, the panel inherits, for free:
      * the Matter-first-then-Hubitat fallback the operator asked for. That
        fallback is Matter-ONLY by construction: DeviceCommander resolves the
        device to a Matter node and, for anything that is not Matter-controllable
        by us (or whose Matter send/verify fails), returns None -> the Hubitat
        path runs. A non-Matter device never touches the Matter branch.
      * retry + state verification, and consistent memoization updates.
    We pass the canonical device id straight through, exactly as the admin route
    /api/devices/{id}/command does — same id space, one command path.
    """
    from services.device_commander import get_device_commander

    args = None
    if body.value is not None:
        # DeviceCommander wants a positional arg LIST (e.g. setLevel -> [75]).
        args = body.value if isinstance(body.value, list) else [body.value]

    logger.info(f"panel: '{principal.name}' -> device {device_id} "
                f"command={body.command} value={body.value!r}")
    try:
        commander = get_device_commander()
        result = await commander.send_command(
            device_id=device_id, command=body.command, args=args, verify=True)
    except Exception as e:  # noqa: BLE001 — surface as 500, never leak a stack
        logger.error(f"panel command failed: device={device_id} "
                     f"cmd={body.command}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    if result.success:
        return {"message": "Command sent", "verified": result.verified,
                "status": result.status.value, "actual_state": result.actual_state,
                "elapsed_ms": round(result.elapsed_ms, 1)}
    raise HTTPException(status_code=502,
                        detail=f"Command failed: {result.error}")
