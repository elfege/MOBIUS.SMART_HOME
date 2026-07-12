"""
Hisense TV FastAPI Router
=========================

REST + LAN-push routes for Hisense / Fire TV remote control via ADB-over-TCP.
Mirrors ``apps/samsung_tv/blueprint.py`` in shape so the dashboard, the
Hubitat driver, and the Mobius MCP server (Phase 2 of the agentic-rule
plan) treat Samsung and Hisense identically at the HTTP layer.

Endpoints
---------
    GET   /hisense-tv/api/status              JSON status snapshot
    POST  /hisense-tv/api/on                  WoL + KEYCODE_POWER (if connected)
    POST  /hisense-tv/api/off                 KEYCODE_POWER (toggle standby)
    POST  /hisense-tv/api/key/{key}           Send arbitrary Android KEYCODE
    POST  /hisense-tv/api/register            Register Hubitat hub for LAN push
    DELETE /hisense-tv/api/register/{dev_id}  Unregister by device id
    GET   /hisense-tv/api/callbacks           List registered callbacks (debug)
    POST  /hisense-tv/api/configure           Update IP / MAC at runtime
    POST  /hisense-tv/api/test/push           Fire a test push to all callbacks
    POST  /hisense-tv/api/test/wol            Send a WoL packet (test)
    GET   /hisense-tv/api/test/adb-poll       One-shot dumpsys power poll

State (singleton ADB connection, retry counter, callback registry) lives in
``services/hisense_tv_client.py``. This router is a thin HTTP façade.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hisense-tv", tags=["hisense-tv"])


# ---------------------------------------------------------------------------
# Hubitat LAN-push callback registry
#
# Mirrors the Samsung blueprint exactly — keyed by callback_url (not
# device_id) because all hubs that register tend to share the same DNI
# (hex of the server IP), so device_id keying would let the last
# registrant clobber the others.
# ---------------------------------------------------------------------------
_hubitat_callbacks: Dict[str, str] = {}   # {callback_url: device_id}
_CALLBACKS_FILE = "/app/state/hisense_tv_callbacks.json"


def _load_callbacks() -> None:
    """Load persisted callback registry from disk on startup."""
    global _hubitat_callbacks
    import json
    try:
        with open(_CALLBACKS_FILE) as f:
            _hubitat_callbacks = json.load(f)
        logger.info(
            "Loaded %d Hubitat callback(s) from %s",
            len(_hubitat_callbacks), _CALLBACKS_FILE,
        )
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Could not load callbacks from %s: %s", _CALLBACKS_FILE, exc)


def _save_callbacks() -> None:
    """Persist callback registry so it survives container restarts."""
    import json
    try:
        os.makedirs(os.path.dirname(_CALLBACKS_FILE), exist_ok=True)
        with open(_CALLBACKS_FILE, "w") as f:
            json.dump(_hubitat_callbacks, f)
    except Exception as exc:
        logger.warning("Could not save callbacks to %s: %s", _CALLBACKS_FILE, exc)


_load_callbacks()


async def _lan_push(url: str, payload: dict) -> int:
    """
    Plain-socket HTTP POST to a Hubitat LAN-push URL on port 39501.

    Same shape as the Samsung blueprint helper — Docker's MASQUERADE
    rewrites the container's source IP to the host's outbound IP, so
    the DNI on the Hubitat driver must match that outbound IP for
    Hubitat to route the incoming POST to the right device's parse().
    """
    import json as _json
    import urllib.parse as _up
    parsed = _up.urlparse(url)
    host = parsed.hostname
    port = parsed.port or 80
    body = _json.dumps(payload).encode()
    request = (
        f"POST / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(request)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(256), timeout=5.0)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    status_line = response.split(b"\r\n")[0]
    return int(status_line.split()[1])


async def push_state_changes(client) -> None:
    """
    POST current TV state to every registered Hubitat callback URL.

    Called from the on_power_change hook in app.py lifespan when the TV
    state transitions. The push payload's shape matches the Samsung
    blueprint's so the Hubitat driver's parse() can be a copy/adapt of
    the existing Samsung_TV_Remote_Driver.groovy.

    Failures per-hub are logged and swallowed — one bad hub must not
    block the others.
    """
    if not _hubitat_callbacks:
        return

    status = client.get_status()
    payload = {
        "power_state":      status["power_state"],
        "conn_state":       status["conn_state"],
        "transport":        status.get("transport", "adb_tcp"),
        "last_observation": status.get("last_observation"),
    }

    for url, device_id in list(_hubitat_callbacks.items()):
        try:
            http_status = await _lan_push(url, payload)
            logger.info("Pushed state to Hubitat %s → HTTP %s", url, http_status)
        except Exception as exc:
            logger.warning(
                "Failed to push state to Hubitat %s (%s): %s",
                url, device_id, exc,
            )


# =============================================================================
# Request models
# =============================================================================


class ConfigureRequest(BaseModel):
    """
    Body for POST /hisense-tv/api/configure.

    All fields optional — only the provided ones are applied. Triggers
    a client stop/start cycle so the new TV IP / MAC takes effect.
    """
    tv_ip:       str | None = None
    mac_address: str | None = None
    name:        str | None = None


class RegisterRequest(BaseModel):
    """
    Body for POST /hisense-tv/api/register. Sent by the Hubitat driver's
    updated() to enroll for real-time LAN push.
    """
    callback_url: str
    device_id:    str


# =============================================================================
# Helper — get or create the singleton client
# =============================================================================


def _get_client():
    """
    Return the HisenseTVClient singleton, creating it from env vars
    if needed.

    Environment variables (all optional, sensible defaults):
        HISENSE_TV_IP    TV LAN IP    (default: <LAN_IP>)
        HISENSE_TV_MAC   MAC, no colons, uppercase (default: empty — WoL no-op)
        HISENSE_TV_NAME  Logical name (default: living_room_tv)
    """
    from services.hisense_tv_client import get_tv_client

    tv_ip = os.environ.get("HISENSE_TV_IP",   "<LAN_IP>")
    mac   = os.environ.get("HISENSE_TV_MAC",   "")
    name  = os.environ.get("HISENSE_TV_NAME",  "living_room_tv")
    return get_tv_client(tv_ip=tv_ip, mac_address=mac, name=name)


# =============================================================================
# API routes
# =============================================================================


@router.get("/api/status")
async def tv_status():
    """
    Return current connection + power state.

    Fields: name, tv_ip, mac, conn_state, power_state, retry_count,
    last_error, off_streak, off_threshold, last_observation, transport.
    """
    client = _get_client()
    return client.get_status()


@router.post("/api/on")
async def tv_on():
    """
    Power on the TV (WoL × 3 + KEYCODE_POWER if ADB already connected).
    """
    client = _get_client()
    await client.turn_on()
    return {"ok": True, "action": "turn_on"}


@router.post("/api/off")
async def tv_off():
    """Toggle standby (sends KEYCODE_POWER over ADB)."""
    client = _get_client()
    await client.turn_off()
    return {"ok": True, "action": "turn_off"}


@router.post("/api/key/{key}")
async def tv_key(key: str):
    """
    Send any supported Android KEYCODE by name.

    Supported names come from the closed vocabulary in
    ``hisense_tv_client._KEYCODES``. Unknown names return 400.
    """
    if not key or len(key) > 40:
        raise HTTPException(status_code=400, detail="Invalid key name")
    client = _get_client()
    try:
        await client.send_key(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "key": key.upper()}


@router.post("/api/register")
async def tv_register(request: Request):
    """
    Register a Hubitat hub for LAN-push state notifications.

    Accepts both snake_case and camelCase field names to be compatible
    with the variations Groovy's httpPost serialisation can produce.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    callback_url = (
        body.get("callback_url") or
        body.get("callbackUrl") or
        body.get("hub_url") or
        body.get("hubUrl")
    )
    device_id = (
        body.get("device_id") or
        body.get("deviceId") or
        body.get("dni")
    )

    # Hubitat sometimes serialises URL fields as a struct {bytes, strings,
    # values} — same defensive decode as the Samsung blueprint.
    if isinstance(callback_url, dict):
        if "bytes" in callback_url:
            callback_url = bytes(callback_url["bytes"]).decode("utf-8")
        elif "strings" in callback_url and "values" in callback_url:
            parts = callback_url.get("strings", [])
            vals  = callback_url.get("values", [])
            callback_url = "".join(
                s for pair in zip(parts, vals + [""]) for s in pair if s
            )

    if not callback_url or not device_id:
        raise HTTPException(
            status_code=422,
            detail=f"Required: callback_url and device_id. Got: {list(body.keys())}",
        )

    _hubitat_callbacks[callback_url] = device_id
    _save_callbacks()
    logger.info(
        "Hubitat callback registered: url=%s device=%s  (total=%d)",
        callback_url, device_id, len(_hubitat_callbacks),
    )
    # Push current state immediately so the hub syncs on registration.
    client = _get_client()
    asyncio.create_task(push_state_changes(client))
    return {"ok": True, "registered": len(_hubitat_callbacks)}


@router.delete("/api/register/{device_id}")
async def tv_unregister(device_id: str):
    """Unregister a Hubitat hub by device_id."""
    url_to_remove = None
    for url, did in _hubitat_callbacks.items():
        if did == device_id:
            url_to_remove = url
            break
    if url_to_remove:
        _hubitat_callbacks.pop(url_to_remove)
        _save_callbacks()
        logger.info("Hubitat callback unregistered: device=%s url=%s", device_id, url_to_remove)
        return {"ok": True, "unregistered": device_id}
    return {"ok": False, "detail": "device_id not found"}


@router.get("/api/callbacks")
async def tv_callbacks():
    """Debug — list all registered callback URLs."""
    return {"callbacks": _hubitat_callbacks}


@router.post("/api/configure")
async def tv_configure(body: ConfigureRequest):
    """
    Update TV connection parameters without restarting the container.
    Triggers a client stop/start cycle so the new values take effect.
    """
    client = _get_client()
    changed = False
    if body.tv_ip is not None:
        client.config.tv_ip = body.tv_ip
        changed = True
    if body.mac_address is not None:
        client.config.mac_address = body.mac_address.replace(":", "").upper()
        changed = True
    if body.name is not None:
        client.config.name = body.name
        changed = True
    if changed:
        logger.info("Hisense TV config updated — restarting ADB session")
        await client.stop()
        await client.start()
    return {"ok": True, "config": client.get_status()}


# =============================================================================
# Test endpoints (mirror Samsung blueprint's /api/test/* shape)
# =============================================================================


@router.post("/api/test/push")
async def tv_test_push():
    """Fire a test push to every registered Hubitat callback."""
    client = _get_client()
    await push_state_changes(client)
    return {
        "ok": True,
        "pushed_to": len(_hubitat_callbacks),
        "callbacks": list(_hubitat_callbacks.keys()),
    }


@router.post("/api/test/wol")
async def tv_test_wol():
    """Send a single WoL packet (for connectivity testing)."""
    client = _get_client()
    await client.send_wol()
    return {"ok": True, "mac": client.config.mac_address}


@router.get("/api/test/adb-poll")
async def tv_test_adb_poll():
    """One-shot dumpsys-power poll against the TV."""
    client = _get_client()
    state = await client.poll_power()
    return {"ok": True, "power_state": state.value}
