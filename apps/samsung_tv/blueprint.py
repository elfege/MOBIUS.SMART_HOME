"""
Samsung TV FastAPI Router

REST + UI routes for Samsung TV remote control.

Endpoints:
    GET  /samsung-tv                  → UI page (Jinja2 template)
    GET  /api/samsung-tv/status       → JSON status snapshot
    POST /api/samsung-tv/on           → Turn TV on (WoL + KEY_POWER)
    POST /api/samsung-tv/off          → Turn TV off (KEY_POWER)
    POST /api/samsung-tv/key/{key}    → Send arbitrary remote key
    POST /api/samsung-tv/configure    → Update IP / MAC / token at runtime

All state (connection, retry counter, token) lives in the SamsungTVClient
singleton.  The router is purely a thin HTTP façade over that service.
"""

import asyncio
import logging
import os
import re
from typing import Dict

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router     = APIRouter(prefix="/samsung-tv", tags=["samsung-tv"])
templates  = Jinja2Templates(directory="templates")


_TOKEN_FILE = "/app/state/samsung_tv_token.txt"


def _persist_token(token: str) -> None:
    """
    Persist the Samsung TV WS auth token so it survives container restarts.

    Primary:  write to /app/logs/samsung_tv_token.txt  (mounted Docker volume,
              always writable, no credentials needed).
    Secondary: attempt to write to AWS Secrets Manager as well  (best-effort —
              requires AWS credentials to be available inside the container).

    start.sh reads _TOKEN_FILE on startup and exports SAMSUNG_TV_TOKEN so the
    container picks up the last-known token without re-pairing.
    """
    # --- Primary: volume file (always works) ---
    try:
        with open(_TOKEN_FILE, "w") as fh:
            fh.write(token)
        logger.info("Samsung TV token saved to %s", _TOKEN_FILE)
    except Exception as exc:
        logger.error("Could not write token file %s: %s", _TOKEN_FILE, exc)

    # --- Secondary: AWS Secrets Manager (best-effort) ---
    try:
        import json
        import boto3
        secret_name = os.environ.get("AWS_SECRET_NAME_SMARTHOME", "SMARTHOME")
        region      = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        client  = boto3.client("secretsmanager", region_name=region)
        current = client.get_secret_value(SecretId=secret_name)
        data    = json.loads(current["SecretString"])
        data["SAMSUNG_TV_TOKEN"] = token
        client.put_secret_value(
            SecretId     = secret_name,
            SecretString = json.dumps(data),
        )
        logger.info("Samsung TV token also persisted to AWS (%s)", secret_name)
    except Exception as exc:
        logger.debug("AWS token persist skipped (no credentials in container): %s", exc)

# ---------------------------------------------------------------------------
# Hubitat LAN-push callback registry
#
# Maps callback_url → device_id for every Hubitat hub that has registered.
# Keyed by URL (not device_id) because all hubs set the same DNI
# (hex of the server IP:port), so keying by device_id would let the last
# hub to register overwrite all the others.
# Each hub calls POST /api/register on startup (driver updated() method).
# When TV state changes, push_state_changes() fires to all registered URLs.
# ---------------------------------------------------------------------------
_hubitat_callbacks: Dict[str, str] = {}  # {callback_url: device_id}
_CALLBACKS_FILE = "/app/state/samsung_tv_callbacks.json"


def _load_callbacks() -> None:
    """Load persisted callback registry from disk on startup."""
    global _hubitat_callbacks
    import json
    try:
        with open(_CALLBACKS_FILE) as f:
            _hubitat_callbacks = json.load(f)
        logger.info(
            "Loaded %d Hubitat callback(s) from %s",
            len(_hubitat_callbacks), _CALLBACKS_FILE
        )
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Could not load callbacks from %s: %s", _CALLBACKS_FILE, exc)


def _save_callbacks() -> None:
    """Persist callback registry to disk so it survives container restarts."""
    import json
    try:
        with open(_CALLBACKS_FILE, "w") as f:
            json.dump(_hubitat_callbacks, f)
    except Exception as exc:
        logger.warning("Could not save callbacks to %s: %s", _CALLBACKS_FILE, exc)


# Load on module import.
_load_callbacks()


async def _lan_push(url: str, payload: dict) -> int:
    """
    HTTP POST to a Hubitat LAN-push URL.

    Hubitat routes incoming messages on port 39501 to a device by matching the
    source IP against the deviceNetworkId.  Docker's MASQUERADE rule rewrites
    the container source IP to the host's outbound IP automatically — no manual
    binding needed.
    """
    import json as _json
    import urllib.parse as _up
    parsed  = _up.urlparse(url)
    host    = parsed.hostname
    port    = parsed.port or 80
    body    = _json.dumps(payload).encode()
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

    Called from the on_power_change hook in app.py lifespan whenever the
    TV power state changes.  Hubitat routes the POST to the driver's parse()
    method based on the source IP matching the deviceNetworkId.

    Failures per-hub are logged and swallowed — one bad hub must not block
    the others.
    """
    if not _hubitat_callbacks:
        return

    status = client.get_status()
    payload = {
        "power_state": status["power_state"],
        "conn_state":  status["conn_state"],
        "token_set":   status["token_set"],
    }

    # Broadcast to hub drivers connected via WebSocket (primary path).
    await broadcast_state_to_hubs(client)

    # HTTP push to Hubitat port 39501.
    # Docker MASQUERADE rewrites the container source IP to the host's outbound
    # IP automatically — the DNI must match that outbound IP (C0A80A11).
    for url, device_id in list(_hubitat_callbacks.items()):
        try:
            status = await _lan_push(url, payload)
            logger.info("Pushed state to Hubitat %s → HTTP %s", url, status)
        except Exception as exc:
            logger.warning("Failed to push state to Hubitat %s (%s): %s", url, device_id, exc)


# =============================================================================
# Request models
# =============================================================================

class ConfigureRequest(BaseModel):
    """
    Body for POST /api/samsung-tv/configure.

    All fields are optional — only the fields provided are updated.
    """
    tv_ip:       str | None = None
    mac_address: str | None = None
    token:       str | None = None
    use_ssl:     bool | None = None
    name:        str | None = None


class RegisterRequest(BaseModel):
    """
    Body for POST /api/samsung-tv/register.

    Sent by the Hubitat Groovy driver on updated() to register for real-time
    LAN push notifications when TV state changes.
    """
    callback_url: str   # e.g. "http://<LAN_IP>:39501"
    device_id:    str   # Hubitat DNI hex, e.g. "C0A80A45:1389"


# =============================================================================
# Helper — get or create the singleton client
# =============================================================================

def _get_client():
    """
    Return the SamsungTVClient singleton, creating it from env vars if needed.

    Environment variables (all optional, have sensible defaults):
        SAMSUNG_TV_IP      TV LAN IP     (default: <LAN_IP>)
        SAMSUNG_TV_MAC     TV MAC, no colons, uppercase (default: D0C24EE93390)
        SAMSUNG_TV_TOKEN   Last known WS auth token (default: empty)
        SAMSUNG_TV_SSL     'true'/'false'  (default: true)
        SAMSUNG_TV_NAME    Logical name for logs (default: living_room_tv)
    """
    from services.samsung_tv_client import get_tv_client, TVPowerState
    import asyncio

    tv_ip  = os.environ.get("SAMSUNG_TV_IP",   "<LAN_IP>")
    mac    = os.environ.get("SAMSUNG_TV_MAC",   "D0C24EE93390")
    token  = os.environ.get("SAMSUNG_TV_TOKEN", "")
    use_ssl = os.environ.get("SAMSUNG_TV_SSL",  "true").lower() != "false"
    name   = os.environ.get("SAMSUNG_TV_NAME",  "living_room_tv")

    async def _save_token(new_token: str) -> None:
        """
        Token-save callback: persists the WS auth token to AWS Secrets Manager
        (SMARTHOME secret, key SAMSUNG_TV_TOKEN) so it survives container
        restarts.  Also updates the in-process env var for hot-reload survival.
        """
        os.environ["SAMSUNG_TV_TOKEN"] = new_token
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _persist_token, new_token)

    client = get_tv_client(
        tv_ip         = tv_ip,
        mac_address   = mac,
        token         = token,
        use_ssl       = use_ssl,
        name          = name,
        on_token_save = _save_token,
    )
    return client


# =============================================================================
# UI route
# =============================================================================

@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def samsung_tv_page(request: Request):
    """
    Render the Samsung TV control panel UI.

    The template polls /api/samsung-tv/status via JS and renders the
    key grid / power button / connection badge.
    """
    client = _get_client()
    return templates.TemplateResponse(
        "samsung_tv.html",
        {
            "request":      request,
            "status":       client.get_status(),
            "tv_ip":        client.config.tv_ip,
        }
    )


# =============================================================================
# API routes
# =============================================================================

@router.get("/api/status", tags=["samsung-tv"])
async def tv_status():
    """
    Return current connection and power state of the TV.

    Response fields:
        name            Logical name
        tv_ip           TV LAN IP
        mac             MAC address
        conn_state      disconnected | connecting | connected
        power_state     on | off | unknown
        use_ssl         bool — whether WSS is active
        queued_commands Number of commands waiting in the queue
        retry_count     Consecutive reconnect attempts
        last_error      Last error string or null
        token_set       Whether a WS auth token has been received
    """
    client = _get_client()
    return client.get_status()


@router.post("/api/on", tags=["samsung-tv"])
async def tv_on():
    """
    Power on the TV.

    Broadcasts a Wake-on-LAN magic packet (×3) and enqueues KEY_POWER
    over the WebSocket.  The WS command works when the TV is in network-
    standby; the WoL works when it is fully powered off.
    """
    client = _get_client()
    await client.turn_on()
    return {"ok": True, "action": "turn_on"}


@router.post("/api/off", tags=["samsung-tv"])
async def tv_off():
    """
    Power off (toggle standby) by sending KEY_POWER.

    For a hard power-down use POST /api/samsung-tv/key/POWEROFF instead.
    """
    client = _get_client()
    await client.turn_off()
    return {"ok": True, "action": "turn_off"}


@router.post("/api/key/{key}", tags=["samsung-tv"])
async def tv_key(key: str):
    """
    Send a remote-control key press to the TV.

    The key is normalised automatically (lowercase input → KEY_MUTE etc.).

    Common keys: MUTE, VOLUMEUP, VOLUMEDOWN, UP, DOWN, LEFT, RIGHT,
    ENTER, RETURN, EXIT, HOME, MENU, SOURCE, HDMI1–HDMI4, 1–9, 0,
    CHUP, CHDOWN, RED, GREEN, YELLOW, BLUE, PLAY, PAUSE, STOP,
    FF, REW, RECORD.

    Args:
        key: Key name (with or without the KEY_ prefix).
    """
    if not key or len(key) > 30:
        raise HTTPException(status_code=400, detail="Invalid key name")

    client = _get_client()
    await client.send_key(key)
    return {"ok": True, "key": key.upper()}


@router.post("/api/register", tags=["samsung-tv"])
async def tv_register(request: Request):
    """
    Register a Hubitat hub for real-time LAN push notifications.

    Called from the driver's updated() method.  When TV state changes,
    the server POSTs to callback_url and Hubitat routes it to parse().

    Accepts both snake_case and camelCase field names to be compatible with
    different Hubitat httpPost serialisation styles:
        callback_url  or  callbackUrl  or  hub_url  or  hubUrl
        device_id     or  deviceId
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    logger.debug("Register request body: %s", body)

    # Accept multiple naming conventions from Groovy drivers.
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

    # Hubitat sometimes serialises URL fields as a struct instead of a plain
    # string — e.g. {"bytes": [...], "strings": ["http://", ":39501"],
    # "values": ["<LAN_IP>"]}.  Decode it back to a plain URL string.
    if isinstance(callback_url, dict):
        if "bytes" in callback_url:
            # Full URL is in the byte array.
            callback_url = bytes(callback_url["bytes"]).decode("utf-8")
        elif "strings" in callback_url and "values" in callback_url:
            # Reconstruct from parts: strings[0] + values[0] + strings[1]
            parts   = callback_url.get("strings", [])
            vals    = callback_url.get("values", [])
            callback_url = "".join(
                s for pair in zip(parts, vals + [""]) for s in pair if s
            )

    if not callback_url or not device_id:
        logger.warning(
            "Register missing fields — received keys: %s", list(body.keys())
        )
        raise HTTPException(
            status_code=422,
            detail=f"Required: callback_url and device_id. Got: {list(body.keys())}"
        )

    _hubitat_callbacks[callback_url] = device_id
    _save_callbacks()
    logger.info(
        "Hubitat callback registered: url=%s device=%s  (total=%d)",
        callback_url, device_id, len(_hubitat_callbacks)
    )
    # Immediately push current state so the hub is in sync on registration.
    client = _get_client()
    asyncio.create_task(push_state_changes(client))
    return {"ok": True, "registered": len(_hubitat_callbacks)}


@router.delete("/api/register/{device_id}", tags=["samsung-tv"])
async def tv_unregister(device_id: str):
    """Unregister a Hubitat hub from LAN push notifications by device_id."""
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


@router.post("/api/unregister-url", tags=["samsung-tv"])
async def tv_unregister_by_url(request: Request):
    """Unregister a Hubitat hub by its callback URL."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = body.get("callback_url", "")
    if url in _hubitat_callbacks:
        removed = _hubitat_callbacks.pop(url)
        _save_callbacks()
        logger.info("Hubitat callback unregistered by URL: %s (device=%s)", url, removed)
        return {"ok": True, "unregistered": url, "remaining": len(_hubitat_callbacks)}
    return {"ok": False, "detail": f"URL not registered: {url}"}


@router.get("/api/callbacks", tags=["samsung-tv"])
async def tv_callbacks():
    """List all registered Hubitat callback URLs (debug endpoint)."""
    return {"callbacks": _hubitat_callbacks}


@router.post("/api/configure", tags=["samsung-tv"])
async def tv_configure(body: ConfigureRequest):
    """
    Update TV connection parameters without restarting the container.

    Only the fields present in the request body are applied.
    The WS background task is restarted automatically to pick up the new values.
    """
    client = _get_client()

    changed = False
    if body.tv_ip is not None:
        client.config.tv_ip = body.tv_ip;  changed = True
    if body.mac_address is not None:
        client.config.mac_address = body.mac_address.replace(":", "").upper()
        changed = True
    if body.token is not None:
        client.config.token = body.token;  changed = True
    if body.use_ssl is not None:
        client.config.use_ssl = body.use_ssl
        client._use_ssl       = body.use_ssl
        changed = True
    name_warning = None
    if body.name is not None:
        if body.name != client.config.name and client.config.token:
            # The TV issues one token per app name. Changing the name while a
            # token is set will invalidate the pairing — the TV will show the
            # authorization popup again on the next connection attempt.
            name_warning = (
                f"App name changed from '{client.config.name}' to '{body.name}' "
                f"while a pairing token exists. The current token will be "
                f"invalidated and the TV will require re-authorization. "
                f"Clear SAMSUNG_TV_TOKEN from AWS Secrets Manager (SMARTHOME) "
                f"after confirming the new pairing."
            )
            logger.warning(name_warning)
        client.config.name = body.name
        changed = True

    if changed:
        logger.info("Samsung TV config updated — restarting WS connection")
        await client.stop()
        await client.start()

    response = {"ok": True, "config": client.get_status()}
    if name_warning:
        response["warning"] = name_warning
    return response


# =============================================================================
# E2E Test Tab
# =============================================================================

@router.get("/tests", response_class=HTMLResponse, include_in_schema=False)
async def samsung_tv_tests_page(request: Request):
    """Render the Samsung TV E2E test page."""
    client = _get_client()
    return templates.TemplateResponse(
        "samsung_tv_tests.html",
        {
            "request": request,
            "status":  client.get_status(),
            "callbacks": _hubitat_callbacks,
        }
    )


@router.post("/api/test/push", tags=["samsung-tv"])
async def tv_test_push():
    """Fire a test push to all registered Hubitat callbacks."""
    client = _get_client()
    await push_state_changes(client)
    return {
        "ok": True,
        "pushed_to": len(_hubitat_callbacks),
        "callbacks": list(_hubitat_callbacks.keys()),
    }


@router.post("/api/test/wol", tags=["samsung-tv"])
async def tv_test_wol():
    """Send a single WoL packet (for connectivity testing)."""
    client = _get_client()
    await client.send_wol()
    return {"ok": True, "mac": client.config.mac_address}


@router.get("/api/test/http-poll", tags=["samsung-tv"])
async def tv_test_http_poll():
    """One-shot HTTP power poll against the TV."""
    client = _get_client()
    state = await client.poll_power()
    return {"ok": True, "power_state": state.value}


@router.get("/api/test/hub-reachable/{hub_ip}", tags=["samsung-tv"])
async def tv_test_hub_reachable(hub_ip: str):
    """Test if a Hubitat hub's port 39501 is reachable."""
    import httpx as _httpx
    url = f"http://{hub_ip}:39501"
    try:
        async with _httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.post(
                url,
                json={"power_state": "test", "conn_state": "test", "token_set": False}
            )
        return {"ok": True, "hub_ip": hub_ip, "status_code": resp.status_code}
    except Exception as exc:
        return {"ok": False, "hub_ip": hub_ip, "error": str(exc)}


# =============================================================================
# Hub state WebSocket — Hubitat driver subscription
#
# Hubitat driver connects outbound to ws://server:5001/samsung-tv/ws/state
# Server broadcasts JSON state frames whenever TV power/connection changes.
# No DNI manipulation, no port 39501, no LAN-push trickery.
#
#   Hubitat Hub ──[ws://server/samsung-tv/ws/state]──► Server
#   Server broadcasts: {"power_state": "on", "conn_state": "connected", ...}
# =============================================================================

# Active hub WebSocket connections keyed by client address string.
_hub_state_clients: Dict[str, WebSocket] = {}


async def broadcast_state_to_hubs(client) -> None:
    """
    Send current TV state to every connected Hubitat hub driver.

    Called from push_state_changes() alongside any remaining HTTP callbacks.
    Failures per-client are logged and the dead socket is pruned.
    """
    if not _hub_state_clients:
        return

    status = client.get_status()
    payload = {
        "power_state": status["power_state"],
        "conn_state":  status["conn_state"],
        "token_set":   status["token_set"],
    }
    import json as _json
    msg = _json.dumps(payload)

    dead = []
    for addr, ws in list(_hub_state_clients.items()):
        try:
            await ws.send_text(msg)
            logger.debug("State broadcast → hub WS %s", addr)
        except Exception as exc:
            logger.warning("Hub WS %s dead (%s) — pruning", addr, exc)
            dead.append(addr)
    for addr in dead:
        _hub_state_clients.pop(addr, None)


@router.websocket("/ws/state")
async def hub_state_ws(websocket: WebSocket):
    """
    Persistent WebSocket endpoint for Hubitat hub drivers.

    The Groovy driver calls:
        interfaces.webSocket.connect("ws://server:5001/samsung-tv/ws/state")

    On connect the server immediately sends the current state so the driver
    syncs without waiting for the next change event.  Thereafter the server
    broadcasts on every power/connection state change.

    The driver may send any text frame as a keepalive ping — the server
    echoes it back so the Hubitat WS interface stays happy.
    """
    await websocket.accept()
    addr = f"{websocket.client.host}:{websocket.client.port}"
    _hub_state_clients[addr] = websocket
    logger.info("Hub driver connected via WS state: %s (total=%d)", addr, len(_hub_state_clients))

    # Immediately push current state so driver syncs on connect.
    try:
        client = _get_client()
        status = client.get_status()
        import json as _json
        await websocket.send_text(_json.dumps({
            "power_state": status["power_state"],
            "conn_state":  status["conn_state"],
            "token_set":   status["token_set"],
        }))
    except Exception as exc:
        logger.warning("Could not send initial state to hub WS %s: %s", addr, exc)

    try:
        # Keep connection alive; echo any pings from the driver.
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(data)   # echo keepalive
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Hub WS %s error: %s", addr, exc)
    finally:
        _hub_state_clients.pop(addr, None)
        logger.info("Hub driver disconnected: %s (remaining=%d)", addr, len(_hub_state_clients))


# =============================================================================
# Hub log WebSocket proxy
#
# The browser page is served over HTTPS — it cannot open a plain ws:// socket
# to the Hubitat hub directly (mixed-content block).  This endpoint acts as a
# transparent server-side relay:
#
#   Browser ──[wss://server/samsung-tv/ws/hub-logs/{hub_ip}]──► Server
#   Server  ──[ws://hub_ip/logsocket]──────────────────────────► Hubitat Hub
#
# The server bridges both directions, forwarding raw text frames as-is.
# =============================================================================

# Allowlist: only our three known hub IPs are accepted as proxy targets,
# preventing SSRF abuse if someone sends an arbitrary IP.
_ALLOWED_HUB_IPS = {"<LAN_IP>", "<LAN_IP>", "<LAN_IP>", "<LAN_IP>"}


@router.websocket("/ws/hub-logs/{hub_ip}")
async def hub_log_proxy(websocket: WebSocket, hub_ip: str):
    """
    Proxy ws://hub_ip/logsocket to the browser over a secure WSS connection.

    Hubitat's logsocket sends JSON log frames:
        {"name": "...", "msg": "...", "id": 90, "time": "...",
         "type": "dev", "level": "info"}

    The proxy forwards messages as-is — no filtering or transformation.
    The browser-side JS is responsible for display/filtering.

    Security: hub_ip must be in the hardcoded allowlist to prevent SSRF.
    """
    # Validate hub IP against allowlist — reject anything else.
    if hub_ip not in _ALLOWED_HUB_IPS:
        await websocket.close(code=4003, reason="Hub IP not in allowlist")
        return

    await websocket.accept()
    hub_url = f"ws://{hub_ip}/logsocket"
    logger.info("Hub log proxy: connecting to %s", hub_url)

    try:
        async with websockets.connect(
            hub_url,
            ping_interval=None,   # Hub may not handle WS pings
            ping_timeout=None,
            open_timeout=10,
        ) as hub_ws:
            logger.info("Hub log proxy: connected to %s", hub_url)

            async def _hub_to_browser():
                """Forward every message from the hub to the browser."""
                async for message in hub_ws:
                    try:
                        await websocket.send_text(message)
                    except WebSocketDisconnect:
                        return
                    except Exception:
                        return

            async def _browser_to_hub():
                """
                Forward browser → hub (for completeness; logsocket is read-only
                in practice, but keeps the proxy symmetric).
                """
                try:
                    while True:
                        data = await websocket.receive_text()
                        await hub_ws.send(data)
                except WebSocketDisconnect:
                    return
                except Exception:
                    return

            # Run both directions concurrently until either side disconnects.
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(_hub_to_browser()),
                    asyncio.create_task(_browser_to_hub()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as exc:
        logger.warning("Hub log proxy error (%s): %s", hub_ip, exc)
        try:
            await websocket.send_text(
                f'{{"_proxy_error": true, "message": "{exc}"}}'
            )
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("Hub log proxy: closed for %s", hub_ip)
# trigger reload
 
