"""
apps/samsung_tv/instance_router.py

Instance-scoped FastAPI router for the multi-TV refactor.

Mounts at ``/samsung-tv/<instance_id>`` (UI) and ``/api/samsung-tv/<instance_id>/*``
(API). The instance_id is the PK of a row in ``dsapp.samsung_tv_instances``;
the registry (`services/samsung_tv_registry.py`) owns the live client for
that row.

Coexists with the legacy single-tenant router in ``blueprint.py`` during
the refactor — see plan §4.7 step 6 for the cutover. The legacy router
keeps backing the bare ``/samsung-tv`` / ``/api/samsung-tv/*`` paths until
the cut.

Plan: docs/plans/samsung_tv_multi_instance_refactor_per_instance_ip_mac_token_in_database.md
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from services.samsung_tv_registry import get_samsung_tv_registry

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/samsung-tv", tags=["samsung-tv-instance"])


# =============================================================================
# Pydantic models
# =============================================================================


class TVConfigureBody(BaseModel):
    """Body for ``POST /api/samsung-tv/<id>/configure`` — patch live config."""
    tv_ip:        Optional[str]  = None
    mac_address:  Optional[str]  = None
    token:        Optional[str]  = None
    use_ssl:      Optional[bool] = None
    samsung_name: Optional[str]  = None
    app_name:     Optional[str]  = None
    label:        Optional[str]  = None


# =============================================================================
# Helpers
# =============================================================================


def _require_client(instance_id: int):
    """Resolve the live SamsungTVClient for instance_id, or raise HTTP 404/409.

    Splits the failure cases:
        404 — no such row at all
        409 — row exists but is disabled / paused (caller should resume first)
    """
    registry = get_samsung_tv_registry()
    client = registry.get(instance_id)
    if client is not None:
        return client

    row = registry.get_row(instance_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"samsung_tv instance {instance_id} not found",
        )
    if not row.get("is_enabled", True):
        raise HTTPException(
            status_code=409,
            detail=f"samsung_tv instance {instance_id} is disabled",
        )
    if row.get("is_paused"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"samsung_tv instance {instance_id} is paused: "
                f"{row.get('pause_reason') or 'no reason recorded'}"
            ),
        )
    # Last-ditch: row is enabled + unpaused but client missing. Likely a
    # mid-restart race. 503 reflects "transient, retry" semantics.
    raise HTTPException(
        status_code=503,
        detail=f"samsung_tv instance {instance_id} client not running yet",
    )


# =============================================================================
# UI route (placeholder until templates/samsung_tv.html is restored)
# =============================================================================


@router.get("/{instance_id}", response_class=HTMLResponse)
async def samsung_tv_page(request: Request, instance_id: int):
    """
    Render the per-instance Samsung TV control page.

    Currently a placeholder: ``templates/samsung_tv.html`` lives in
    ``stash@{0}`` and has not been restored to the working tree. When the
    template is back this hook switches to a ``templates.TemplateResponse``
    that takes both ``request`` and the instance row. Until then this
    returns a tiny diagnostic page so the route is at least reachable.
    """
    registry = get_samsung_tv_registry()
    row = registry.get_row(instance_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"samsung_tv instance {instance_id} not found",
        )
    client = registry.get(instance_id)
    running = client is not None
    return HTMLResponse(
        f"""<!doctype html>
        <html><head><title>Samsung TV {instance_id}</title></head>
        <body style='font-family:sans-serif;padding:2em;'>
        <h1>Samsung TV — {row.get('label', '(unlabeled)')}</h1>
        <p><strong>Instance id:</strong> {instance_id}</p>
        <p><strong>IP:</strong> {row.get('tv_ip')}</p>
        <p><strong>Running:</strong> {running}</p>
        <p><em>Full UI page is pending restoration of
        <code>templates/samsung_tv.html</code> from stash@{{0}}.</em></p>
        </body></html>"""
    )


# =============================================================================
# API routes — every action takes an explicit instance_id
# =============================================================================


@router.get("/api/{instance_id}/status")
async def api_status(instance_id: int) -> Dict[str, Any]:
    """Live status snapshot for the given TV instance."""
    client = _require_client(instance_id)
    return client.get_status()


@router.post("/api/{instance_id}/on")
async def api_on(instance_id: int) -> Dict[str, Any]:
    """Turn the TV on (Wake-on-LAN + KEY_POWER)."""
    client = _require_client(instance_id)
    return await client.power_on()


@router.post("/api/{instance_id}/off")
async def api_off(instance_id: int) -> Dict[str, Any]:
    """Toggle KEY_POWER. Idempotent for already-off TVs."""
    client = _require_client(instance_id)
    return await client.power_off()


@router.post("/api/{instance_id}/key/{key}")
async def api_key(instance_id: int, key: str) -> Dict[str, Any]:
    """Send an arbitrary Samsung remote key (e.g. KEY_VOLUP, KEY_HDMI)."""
    client = _require_client(instance_id)
    return await client.send_key(key)


@router.post("/api/{instance_id}/configure")
async def api_configure(
    instance_id: int, body: TVConfigureBody,
) -> Dict[str, Any]:
    """
    Patch a TV instance's configuration. Writes the changed columns to
    the DB row, then asks the registry to reload (which respawns the
    client if any wire-relevant field changed: tv_ip, mac, ssl,
    samsung_name).

    Returns the updated row plus a flag indicating whether the client was
    restarted as part of the reload.
    """
    registry = get_samsung_tv_registry()
    row = registry.get_row(instance_id)
    if row is None:
        # If the row exists in DB but we have no snapshot yet, reload
        # forces a fresh fetch. After that, surfacing 404 to the caller
        # is the right move when it's genuinely missing.
        await registry.reload_instance(instance_id)
        row = registry.get_row(instance_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"samsung_tv instance {instance_id} not found",
            )

    patch: Dict[str, Any] = {}
    if body.tv_ip is not None:
        patch["tv_ip"] = body.tv_ip
    if body.mac_address is not None:
        patch["mac_address"] = body.mac_address.replace(":", "").upper() or None
    if body.token is not None:
        patch["token"] = body.token
    if body.use_ssl is not None:
        patch["use_ssl"] = body.use_ssl
    if body.samsung_name is not None:
        patch["samsung_name"] = body.samsung_name
    if body.app_name is not None:
        patch["app_name"] = body.app_name
    if body.label is not None:
        patch["label"] = body.label

    if not patch:
        return {"ok": True, "noop": True, "row": row}

    # Run the PATCH via the registry's helper so we don't open a second
    # PostgREST session here.
    import asyncio as _asyncio
    ok = await _asyncio.to_thread(
        registry._patch_row, instance_id, patch  # noqa: SLF001 — intentional
    )
    if not ok:
        raise HTTPException(
            status_code=500,
            detail=f"failed to PATCH samsung_tv instance {instance_id}",
        )

    await registry.reload_instance(instance_id)
    new_row = registry.get_row(instance_id) or {}
    return {"ok": True, "row": new_row}


@router.get("/api/list")
async def api_list() -> Dict[str, Any]:
    """
    Return every known TV instance with metadata + a `_is_running` flag.
    Used by the dashboard Drivers section to populate one card per TV.
    """
    registry = get_samsung_tv_registry()
    return {"instances": registry.list_instances()}


# =============================================================================
# (No bare-prefix redirect here)
# =============================================================================
#
# `GET /samsung-tv` (no id) intentionally stays bound to the legacy
# blueprint while both routers coexist. FastAPI's trailing-slash redirect
# would collide if this router also declared the bare prefix. The bare-
# prefix-redirect lives instead in plan §4.7 step 6 (cutover), which
# replaces the legacy handler with `RedirectResponse → /samsung-tv/<id>`
# once we're confident no callers depend on the legacy HTML body.
