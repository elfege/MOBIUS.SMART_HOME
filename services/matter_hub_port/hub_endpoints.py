"""
matter_hub_port.hub_endpoints — the S1 Hubitat HTTP primitives.

Reverse-engineered surface (MSG-933 recon, CCE-verified, C-8 buildVersion
2.5.0.159; endpoints are firmware-pinned — re-verify on hub firmware bumps):

    PRODUCE (source hub) : GET /hub/matter/openPairingWindow?node=<id>
                           -> {"success": true, "setupCode": "<11 digits>"}
    CONSUME (target hub) : GET /hub/matter/pair?setupCode=<11 digits>
                           -> new node id as JSON; 0 = failure
    POLL     (target hub): GET /hub/matterPairDeviceStatus?nodeId=<id>
    SCAN     (either hub): GET /hub/matterDetails/json
                           -> device list (same endpoint the discovery scan uses)

All functions here are SYNCHRONOUS `requests` calls — the orchestrator wraps
every call in asyncio.to_thread (single-uvicorn-worker rule: bare blocking I/O
in an async route froze the event loop long enough for autoheal to restart the
container mid-commission; see the auto-commission route's history note).

Fleet hubs run Login Security OFF, so these GETs are unauthenticated LAN calls.
"""

import logging
from typing import Any, Dict, List, Optional

import requests

from services.matter_discovery import mac_from_ipv6_ll

logger = logging.getLogger(__name__)

# A hub that cannot open a window in 30s is down (auto-commission's lesson —
# 90s was a needlessly long freeze even threaded).
OPEN_WINDOW_TIMEOUT_S = 30
PAIR_TIMEOUT_S = 60          # target-side commissioning handshake can be slow
STATUS_TIMEOUT_S = 10
DETAILS_TIMEOUT_S = 15


class HubEndpointError(RuntimeError):
    """A hub HTTP primitive failed; `classification` feeds failed_<class> audit
    statuses (window | pair_rejected | exception)."""

    def __init__(self, message: str, classification: str = "exception"):
        super().__init__(message)
        self.classification = classification


def open_pairing_window(hub_ip: str, node_id: int) -> str:
    """Open an ECM pairing window on the SOURCE hub for one device.

    Returns the 11-digit setup code. The code is window-TTL short-lived —
    the caller must consume it promptly (the orchestrator does, sequentially).

    Raises HubEndpointError(classification='window') on any failure.
    """
    try:
        resp = requests.get(
            f"http://{hub_ip}/hub/matter/openPairingWindow",
            params={"node": node_id},
            timeout=OPEN_WINDOW_TIMEOUT_S,
        )
    except Exception as e:
        raise HubEndpointError(
            f"openPairingWindow on {hub_ip} node {node_id} failed: {e}",
            classification="window") from e
    if not resp.ok:
        raise HubEndpointError(
            f"openPairingWindow on {hub_ip} returned HTTP {resp.status_code}",
            classification="window")
    try:
        data = resp.json()
    except ValueError:
        data = resp.text.strip()
    # Same tolerant extraction the auto-commission route uses.
    setup_code = None
    if isinstance(data, dict):
        setup_code = data.get('setupCode') or data.get('code') or data.get('pairingCode')
    elif isinstance(data, str) and data:
        setup_code = data
    if not setup_code:
        raise HubEndpointError(
            f"no setup code in openPairingWindow response from {hub_ip}: {data!r}",
            classification="window")
    return str(setup_code).strip()


def consume_setup_code(hub_ip: str, setup_code: str) -> int:
    """Drive the TARGET hub to commission using a setup code.

    Returns the device's NEW node id on the target hub (> 0).
    Raises HubEndpointError(classification='pair_rejected') when the hub
    answers 0 (its own failure signal), 'exception' for transport errors.
    """
    try:
        resp = requests.get(
            f"http://{hub_ip}/hub/matter/pair",
            params={"setupCode": setup_code},
            timeout=PAIR_TIMEOUT_S,
        )
    except Exception as e:
        raise HubEndpointError(
            f"matter/pair on {hub_ip} failed: {e}") from e
    if not resp.ok:
        raise HubEndpointError(
            f"matter/pair on {hub_ip} returned HTTP {resp.status_code}",
            classification="pair_rejected")
    raw = resp.text.strip()
    try:
        node_id = int(resp.json()) if raw else 0
    except (ValueError, TypeError):
        try:
            node_id = int(raw)
        except (ValueError, TypeError):
            raise HubEndpointError(
                f"unparseable matter/pair response from {hub_ip}: {raw!r}",
                classification="pair_rejected")
    if node_id <= 0:
        # 0 is the hub's own "pairing failed" signal (S1). NOTE: HTTP 200 with
        # a non-zero node id is STILL not success — only the rescan MAC match
        # is (recycled-IP lesson) — but 0 is a definite failure.
        raise HubEndpointError(
            f"target hub {hub_ip} rejected the setup code (returned 0)",
            classification="pair_rejected")
    return node_id


def pair_device_status(hub_ip: str, node_id: int) -> Any:
    """Raw pairing status for a node on the TARGET hub (best-effort JSON).

    The exact response schema is undocumented; callers must treat this as
    advisory progress only — completion truth comes from the matterDetails
    rescan (fetch_matter_devices), never from this endpoint.
    """
    resp = requests.get(
        f"http://{hub_ip}/hub/matterPairDeviceStatus",
        params={"nodeId": node_id},
        timeout=STATUS_TIMEOUT_S,
    )
    if not resp.ok:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text.strip() or None


def fetch_matter_devices(hub_ip: str) -> List[Dict[str, Any]]:
    """Scan a hub's paired Matter devices (GET /hub/matterDetails/json).

    Normalized rows: {unique_id, name, node_id, device_id, mac, online}.
    `mac` is derived from the device's IPv6 link-local address (EUI-64),
    exactly as the discovery service does — it is the cross-fabric identity.

    Raises HubEndpointError on transport/HTTP failure (a failed VERIFY rescan
    must surface as a failure, never as an empty-list false negative).
    """
    try:
        resp = requests.get(
            f"http://{hub_ip}/hub/matterDetails/json",
            timeout=DETAILS_TIMEOUT_S,
        )
    except Exception as e:
        raise HubEndpointError(f"matterDetails on {hub_ip} failed: {e}") from e
    if not resp.ok:
        raise HubEndpointError(
            f"matterDetails on {hub_ip} returned HTTP {resp.status_code}")
    data = resp.json()
    devices = data if isinstance(data, list) else data.get('devices', [])

    out: List[Dict[str, Any]] = []
    for device in devices:
        ip_addr = device.get('ipAddress', device.get('ip', ''))
        out.append({
            "unique_id": device.get('uniqueId') or device.get('unique_id', ''),
            "name": device.get('name', device.get('label', 'Unknown')),
            "node_id": device.get('nodeId', device.get('node_id', 0)),
            "device_id": device.get('deviceId', device.get('id', '')),
            "mac": mac_from_ipv6_ll(ip_addr),
            "online": bool(device.get('online', device.get('isOnline', False))),
        })
    return out


def find_device_by_mac(devices: List[Dict[str, Any]],
                       mac: Optional[str]) -> Optional[Dict[str, Any]]:
    """The device row whose derived MAC matches (case-insensitive), or None."""
    if not mac:
        return None
    needle = mac.strip().lower()
    for d in devices:
        if (d.get('mac') or '').strip().lower() == needle:
            return d
    return None
