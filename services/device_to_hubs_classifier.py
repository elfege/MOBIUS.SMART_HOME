"""
Hub Device Classifier Service

Queries all configured Hubitat hubs via Maker API and classifies devices as
native (physically paired to that hub's radio) or mesh-linked (mirrored from
another hub via Hub Mesh).

Classification uses the 'hubMeshDisabled' attribute: present = mesh-linked,
absent = native. Additionally parses the 'name' field suffix ' on Home N'
to identify the source hub for linked devices.

The resulting device_hub_mapping table enables:
- Command routing to native hub (bypasses mesh relay for lower latency)
- Per-hub event stream routing (future: parallel WebSocket per hub)
- Protocol awareness (Z-Wave / Zigbee / Matter / LAN / Virtual)

Usage:
    from services.device_to_hubs_classifier import run_classification
    results = run_classification()   # queries all hubs, populates DB table
"""

import os
import re
import logging
import requests
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ANSI colors for log output
_C = "\033[96m"   # cyan
_G = "\033[92m"   # green
_Y = "\033[93m"   # yellow
_R = "\033[0m"    # reset


# =========================================================================
# Protocol Detection
# =========================================================================

# Map known driver name patterns to radio protocol.
# Order matters: first match wins. Patterns are checked case-insensitively
# against the Hubitat 'type' field.
DRIVER_PROTOCOL_PATTERNS: List[Tuple[str, str]] = [
    # Z-Wave
    ("z-wave", "zwave"),
    ("zwave", "zwave"),
    ("aeotec", "zwave"),
    ("aeon", "zwave"),
    ("homeseer", "zwave"),
    ("dome", "zwave"),
    ("inovelli", "zwave"),
    ("zooz", "zwave"),
    ("ge enbrighten", "zwave"),
    ("ge 40 amp", "zwave"),
    ("ge smart fan", "zwave"),
    ("ge portable smart", "zwave"),
    ("jasco", "zwave"),
    ("eaton", "zwave"),
    ("leviton z", "zwave"),
    ("fibaro", "zwave"),
    ("qubino", "zwave"),
    ("monoprice", "zwave"),
    ("gocontrol", "zwave"),
    ("honeywell", "zwave"),
    ("kwikset", "zwave"),
    ("schlage", "zwave"),
    ("yale", "zwave"),
    ("ring", "zwave"),
    ("thermostat dimmer", "zwave"),
    # Zigbee
    ("zigbee", "zigbee"),
    ("sengled", "zigbee"),
    ("hue", "zigbee"),
    ("ikea", "zigbee"),
    ("sonoff", "zigbee"),
    ("samsung", "zigbee"),
    ("centralite", "zigbee"),
    ("smartthings", "zigbee"),
    ("tuya", "zigbee"),
    ("xiaomi", "zigbee"),
    ("aqara", "zigbee"),
    ("zemismart", "zigbee"),
    ("innr", "zigbee"),
    ("heiman", "zigbee"),
    ("linptech", "zigbee"),
    # Matter
    ("matter", "matter"),
    # LAN / WiFi
    ("esp", "lan"),
    ("midea", "lan"),
    ("tasmota", "lan"),
    ("shelly", "lan"),
    ("kasa", "lan"),
    ("wled", "lan"),
    ("bond", "lan"),
    ("switchbot", "lan"),
    ("sonos", "lan"),
    ("airplay", "lan"),
    ("chromecast", "lan"),
    ("vesync", "lan"),
    ("levoit", "lan"),
    ("cat feeder", "lan"),
    ("catswatert", "lan"),
    # Virtual / Cloud
    ("virtual", "virtual"),
    ("mobile app", "cloud"),
    ("openweathermap", "cloud"),
    ("hub controller", "virtual"),
    ("application refresh", "virtual"),
    ("momentary", "virtual"),
]


def detect_protocol(device_type: str) -> str:
    """
    Detect radio protocol from Hubitat driver type name.

    Args:
        device_type: The 'type' field from Hubitat Maker API (driver name)

    Returns:
        Protocol string: zwave, zigbee, matter, lan, virtual, cloud, or unknown
    """
    if not device_type:
        return "unknown"
    type_lower = device_type.lower()
    for pattern, protocol in DRIVER_PROTOCOL_PATTERNS:
        if pattern in type_lower:
            return protocol
    return "unknown"


# =========================================================================
# Hub Classification
# =========================================================================

def _fetch_hub_devices(
    hub_ip: str,
    hub_name: str,
) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch all devices from a single hub via the ADMIN API (/device/list/data).

    Migrated off Maker /devices/all (2026-05-26): the admin roster carries the
    authoritative `linkedDevice` boolean (mesh mirror vs native), plus
    `deviceNetworkId`, `type`, and `deviceTypeName`, and includes admin-only
    devices the Maker app never exposed. Capabilities/attributes are NOT in this
    endpoint — they are filled per native device later (reuse-or-fullJson).

    Returns the raw roster list, or None on failure.
    """
    try:
        from services.hubitat_admin_client import get_client
        client = get_client(hub_ip, hub_name)
        return client.get_all_devices()
    except Exception as e:
        logger.error(f"Failed to fetch devices from {hub_ip} (admin API): {e}")
        return None


def _fetch_caps_attrs(
    hub_ip: str, hub_name: str, device_id: str,
) -> tuple:
    """Fetch (capabilities, attributes) for one device via admin fullJson.

    Only called for devices we don't already have capabilities cached for
    (new / admin-only devices). Returns ([], {}) on any failure.
    """
    try:
        from services.hubitat_admin_client import get_client, to_maker_shape
        raw = get_client(hub_ip, hub_name).get_device(int(device_id))
        if not raw:
            return [], {}
        # Capabilities live at device.capabilities in the admin fullJson
        # (to_maker_shape only normalizes attributes/currentStates, not caps).
        dev = raw.get("device", raw) if isinstance(raw, dict) else {}
        caps = dev.get("capabilities") or []
        shaped = to_maker_shape(raw)
        attrs = {
            a["name"]: a.get("currentValue")
            for a in (shaped or {}).get("attributes", []) if a.get("name")
        }
        return caps, attrs
    except Exception as e:
        logger.debug(f"_fetch_caps_attrs {hub_ip}/{device_id}: {e}")
    return [], {}


def _is_mesh_linked(device: Dict[str, Any]) -> bool:
    """
    Check if a device is a Hub Mesh linked device.

    Hub Mesh linked devices have a 'hubMeshDisabled' attribute.
    This is a firmware-level indicator with 100% reliability.

    Args:
        device: Device dict from Maker API

    Returns:
        True if device is a mesh-linked mirror
    """
    attrs = device.get("attributes", {})
    if isinstance(attrs, dict):
        return "hubMeshDisabled" in attrs
    elif isinstance(attrs, list):
        return any(
            (a.get("name") == "hubMeshDisabled" if isinstance(a, dict)
             else a == "hubMeshDisabled")
            for a in attrs
        )
    return False


def _parse_source_hub(device_name: str) -> Optional[str]:
    """
    Parse the source hub from a mesh-linked device's name field.

    Hubitat appends ' on Home N' to the name (not label) of linked devices.

    Args:
        device_name: The 'name' field from Maker API

    Returns:
        Source hub identifier (e.g., 'Home 1') or None
    """
    match = re.search(r' on Home (\d+)$', device_name)
    if match:
        return f"Home {match.group(1)}"
    return None


def _ingest_into_devices(natives: List[Dict[str, Any]]) -> None:
    """
    Upsert the GLOBALLY-deduped native devices into the canonical `devices`
    table, keyed on (hub_ip, hubitat_id) — the true device identity.

    `natives` is the full cross-hub native list AFTER dedup: each entry carries
    hub_ip, id (hubitat_id), hub_id, label, name, type, protocol, and the
    classifier's `is_name_duplicate` verdict (a native that lost a same-label
    conflict to the primary hub — stored hidden, kept as a failover candidate).

    Capabilities/attributes (not in the admin roster) are REUSED from the
    existing row by (hub_ip, hubitat_id); only devices without cached
    capabilities (new / admin-only) trigger a per-device fullJson fetch.

    Errors are swallowed per-device so one bad row can't poison the rest.
    """
    if not natives:
        return

    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")

    # Preload existing capabilities/attributes for reuse (one query).
    existing: Dict[tuple, Dict[str, Any]] = {}
    try:
        r = requests.get(
            f"{postgrest_url}/devices",
            params={"select": "hub_ip,hubitat_id,capabilities,attributes"},
            timeout=15,
        )
        if r.status_code == 200:
            for row in r.json():
                existing[(row["hub_ip"], str(row["hubitat_id"]))] = row
    except Exception as e:
        logger.debug(f"ingest: existing-caps preload failed: {e}")

    counts = {"INSERT": 0, "UPDATE": 0, "ERROR": 0, "FULLJSON": 0}

    for e in natives:
        key = (e["hub_ip"], str(e.get("id", "")))
        ex = existing.get(key)
        caps = ex.get("capabilities") if ex else None
        attrs = (ex.get("attributes") if ex else None) or {}
        if caps in (None, [], {}):
            # New / uncached device — fetch capabilities via admin fullJson.
            caps, attrs = _fetch_caps_attrs(e["hub_ip"], e.get("hub_name", ""), e.get("id", ""))
            counts["FULLJSON"] += 1

        try:
            r = requests.post(
                f"{postgrest_url}/rpc/upsert_device",
                json={
                    "p_hub_ip":            e["hub_ip"],
                    "p_hubitat_id":        str(e.get("id", "")),
                    "p_hub_id":            e.get("hub_id"),
                    "p_name":              e.get("name", ""),
                    "p_label":             e.get("label", ""),
                    "p_device_type":       e.get("type", ""),
                    "p_protocol":          e.get("protocol", "unknown"),
                    "p_capabilities":      caps or [],
                    "p_attributes":        attrs or {},
                    "p_is_name_duplicate": bool(e.get("is_name_duplicate", False)),
                },
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if r.status_code in (200, 201):
                payload = r.json()
                if isinstance(payload, list) and payload:
                    payload = payload[0]
                action = (payload or {}).get("action", "ERROR")
                counts[action] = counts.get(action, 0) + 1
            else:
                counts["ERROR"] += 1
                logger.warning(
                    f"upsert_device failed for {e.get('name')!r} "
                    f"on hub {e.get('hub_ip')}: HTTP {r.status_code} {r.text[:200]}"
                )
        except Exception as ex2:
            counts["ERROR"] += 1
            logger.warning(
                f"upsert_device exception for {e.get('name')!r} "
                f"on hub {e.get('hub_ip')}: {ex2}"
            )

    logger.info(
        f"devices ingest: INSERT={counts['INSERT']} UPDATE={counts['UPDATE']} "
        f"ERROR={counts['ERROR']} (fullJson caps fetches={counts['FULLJSON']})"
    )


def classify_hub(
    hub_name: str,
    hub_ip: str,
    hub_id: Optional[int] = None,
    is_primary: bool = False,
    **_ignored,
) -> Dict[str, Any]:
    """
    Classify all devices on a single hub as native or mesh-linked, using the
    admin roster's authoritative `linkedDevice` boolean.

    Does NOT ingest here — ingest is global (run_classification), so the
    same-label primary-hub-wins dedup can see every hub's natives at once.

    Returns a dict with 'native' and 'linked' entry lists + hub metadata.
    Each native entry carries the fields the global dedup + ingester need:
    id (hubitat_id), label, name, type, protocol, dni, hub_ip, hub_name,
    hub_id, is_primary.
    """
    devices = _fetch_hub_devices(hub_ip, hub_name)
    if devices is None:
        return {"error": f"Failed to fetch from {hub_ip}", "native": [], "linked": []}

    native = []
    linked = []

    for d in devices:
        # AUTHORITATIVE mesh signal — the admin roster boolean (not the old
        # Maker hubMeshDisabled attribute, not the name suffix).
        is_linked = bool(d.get("linkedDevice"))
        name = d.get("name", "")
        # Strip trailing whitespace (mesh artifact) so one normalized label
        # is stored per device and label lookups match across hubs.
        label = (d.get("label") or name).strip()
        device_type = d.get("deviceTypeName") or d.get("type") or ""
        protocol = detect_protocol(device_type)
        source_hub = _parse_source_hub(name) if is_linked else None

        entry = {
            "id": str(d.get("id", "")),
            "label": label,
            "name": name,
            "type": device_type,
            "protocol": protocol,
            "is_linked": is_linked,
            "source_hub": source_hub,
            "dni": d.get("deviceNetworkId"),
            "hub_ip": hub_ip,
            "hub_name": hub_name,
            "hub_id": hub_id,
            "is_primary": is_primary,
        }

        if is_linked:
            linked.append(entry)
        else:
            native.append(entry)

    logger.info(
        f"{_C}Hub {hub_name}{_R} ({hub_ip}): "
        f"{_G}{len(native)} native{_R}, "
        f"{_Y}{len(linked)} linked{_R}, "
        f"{len(devices)} total"
    )

    return {
        "hub_name": hub_name,
        "hub_ip": hub_ip,
        "hub_id": hub_id,
        "is_primary": is_primary,
        "total": len(devices),
        "native": native,
        "linked": linked,
    }


def _get_hub_configs() -> List[Dict[str, Any]]:
    """
    Load enabled hubs from the `hub_config` table (the UI-editable source of
    truth), ordered PRIMARY hub first. Replaces the old hardcoded env-var /
    Maker-creds loader — the classifier now honors `hub_config.is_primary`
    (the same flag mode_poller and device_commander use), so same-label
    conflicts resolve to the user-designated primary, not a hardcoded ".72".

    Returns [{hub_name, hub_ip, hub_id, is_primary}], primary first then by id.
    """
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        r = requests.get(
            f"{pg}/hub_config",
            params={
                "is_enabled": "eq.true",
                "select": "id,hub_name,hub_ip,is_primary",
                "order": "is_primary.desc,id.asc",
            },
            timeout=5,
        )
        if r.status_code == 200:
            return [
                {
                    "hub_name": h["hub_name"],
                    "hub_ip": h["hub_ip"],
                    "hub_id": h["id"],
                    "is_primary": bool(h.get("is_primary")),
                }
                for h in r.json()
            ]
        logger.error(f"_get_hub_configs: hub_config load HTTP {r.status_code}")
    except Exception as e:
        logger.error(f"_get_hub_configs: hub_config load failed: {e}")
    return []


def run_classification() -> Dict[str, Any]:
    """
    Run full device classification across all configured hubs.

    Queries each hub's Maker API, classifies native vs linked,
    builds cross-reference routing table, and writes to the
    device_hub_mapping table via PostgREST.

    Returns:
        Summary dict with per-hub counts and total classified
    """
    hubs = _get_hub_configs()
    if not hubs:
        return {"error": "No hub configurations found in environment"}

    # Step 1: Classify each hub
    hub_results = {}
    for cfg in hubs:
        result = classify_hub(**cfg)
        hub_results[cfg["hub_name"]] = result

    # Step 2: GLOBAL native dedup — the PRIMARY hub wins a same-label conflict.
    # Mesh mirrors are already excluded (linkedDevice), so only genuinely-
    # distinct natives are compared. The loser(s) are flagged is_name_duplicate
    # (hidden in the picker, but KEPT as failover candidates — Matter/LAN
    # devices bindable to multiple hubs). No primary in the group → earliest
    # hub_id wins (deterministic).
    all_natives: List[Dict[str, Any]] = []
    for result in hub_results.values():
        all_natives.extend(result.get("native", []))

    def _rank(e: Dict[str, Any]):
        return (0 if e.get("is_primary") else 1, e.get("hub_id") or 1_000_000)

    by_label: Dict[str, List[Dict[str, Any]]] = {}
    for e in all_natives:
        key = (e.get("label") or "").strip().lower()
        if key:
            by_label.setdefault(key, []).append(e)

    name_dups = 0
    for group in by_label.values():
        winner = min(group, key=_rank)
        for e in group:
            e["is_name_duplicate"] = (len(group) > 1 and e is not winner)
            if e["is_name_duplicate"]:
                name_dups += 1
    if name_dups:
        logger.info(f"classifier dedup: {name_dups} same-label native(s) flagged is_name_duplicate (primary wins)")

    # Ingest ALL natives (winners + flagged losers) into `devices`, keyed on
    # (hub_ip, hubitat_id). hub_id is set here — fixing the old INSERT that
    # silently failed on hub_id NOT NULL.
    try:
        _ingest_into_devices(all_natives)
    except Exception as e:
        logger.warning(f"_ingest_into_devices (global) failed: {e}")

    # Step 3: device_hub_mapping cross-reference (command routing). Native
    # registry = WINNERS only (is_name_duplicate=false); mirrors come from
    # linked devices on other hubs sharing the label.
    native_registry: Dict[str, Dict[str, Any]] = {}
    all_devices_by_hub: Dict[str, Dict[str, Dict]] = {}

    for hub_name, result in hub_results.items():
        if "error" in result and not result.get("native"):
            continue
        all_devices_by_hub[hub_name] = {}
        for d in result.get("native", []):
            if not d.get("is_name_duplicate"):
                native_registry[d["label"]] = {
                    "hub_name": hub_name,
                    "hub_ip": result["hub_ip"],
                    "id": d["id"],
                    "protocol": d["protocol"],
                    "type": d["type"],
                }
            all_devices_by_hub[hub_name][d["label"]] = d
        for d in result.get("linked", []):
            all_devices_by_hub[hub_name][d["label"]] = d

    routing_entries = []
    for label, native_info in native_registry.items():
        mirrors = {}
        for hub_name, hub_devices in all_devices_by_hub.items():
            if hub_name == native_info["hub_name"]:
                continue
            if label in hub_devices:
                d = hub_devices[label]
                mirrors[hub_name] = {
                    "id": d["id"],
                    "hub_ip": hub_results[hub_name].get("hub_ip", ""),
                }

        routing_entries.append({
            "device_label": label,
            "native_hub_name": native_info["hub_name"],
            "native_hub_ip": native_info["hub_ip"],
            "native_device_id": native_info["id"],
            "protocol": native_info["protocol"],
            "device_type": native_info["type"],
            "mirrors": mirrors,
            "is_mesh_linked": False,
            "last_classified_at": datetime.now(timezone.utc).isoformat(),
        })

    # Step 4: Write to database via PostgREST
    written = _write_to_database(routing_entries)

    # Step 5: Summary
    proto_counts = defaultdict(int)
    for entry in routing_entries:
        proto_counts[entry["protocol"]] += 1

    summary = {
        "total_native_devices": len(routing_entries),
        "total_with_mirrors": sum(1 for e in routing_entries if e["mirrors"]),
        "written_to_db": written,
        "protocols": dict(proto_counts),
        "per_hub": {
            hub_name: {
                "native": len(r.get("native", [])),
                "linked": len(r.get("linked", [])),
                "total": r.get("total", 0),
            }
            for hub_name, r in hub_results.items()
        },
        "classified_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        f"Classification complete: {len(routing_entries)} native devices, "
        f"{summary['total_with_mirrors']} with mirrors, "
        f"{written} written to DB"
    )

    return summary


def _write_to_database(entries: List[Dict[str, Any]]) -> int:
    """
    Write classification entries to device_hub_mapping via PostgREST.

    Uses upsert (merge-duplicates) to handle re-classification without
    losing data on devices that haven't changed.

    Args:
        entries: List of routing entry dicts

    Returns:
        Number of entries written
    """
    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    written = 0

    # PostgREST upsert: batch insert with merge-duplicates
    # Send in batches of 50 to avoid payload size issues
    batch_size = 50
    for i in range(0, len(entries), batch_size):
        batch = entries[i:i + batch_size]
        try:
            resp = requests.post(
                f"{postgrest_url}/device_hub_mapping",
                json=batch,
                headers={
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                timeout=10,
            )
            if resp.status_code in (200, 201):
                written += len(batch)
            else:
                logger.error(
                    f"PostgREST upsert failed (status {resp.status_code}): "
                    f"{resp.text[:200]}"
                )
        except Exception as e:
            logger.error(f"Failed to write classification batch to DB: {e}")

    return written


# =========================================================================
# Lookup Functions (used by device_commander for command routing)
# =========================================================================

# In-memory cache of the routing table (refreshed on classification)
_routing_cache: Dict[str, Dict[str, Any]] = {}
_cache_loaded = False


def _load_routing_cache() -> None:
    """Load device_hub_mapping from PostgREST into memory."""
    global _routing_cache, _cache_loaded
    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    try:
        resp = requests.get(
            f"{postgrest_url}/device_hub_mapping",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            entries = resp.json()
            _routing_cache = {}
            for entry in entries:
                label = entry.get("device_label", "")
                _routing_cache[label] = entry
            _cache_loaded = True
            logger.info(f"Loaded {len(_routing_cache)} device routing entries")
        else:
            logger.warning(
                f"Failed to load routing cache: HTTP {resp.status_code}"
            )
    except Exception as e:
        logger.warning(f"Failed to load routing cache: {e}")


def get_native_hub(device_label: str) -> Optional[Dict[str, str]]:
    """
    Look up the native hub for a device by its label.

    Returns hub connection info needed to send commands directly
    to the device's native hub (bypassing mesh relay).

    Args:
        device_label: Device label as shown in Hubitat

    Returns:
        Dict with 'hub_name', 'hub_ip', 'native_device_id', 'protocol'
        or None if device is not in the routing table
    """
    global _cache_loaded
    if not _cache_loaded:
        _load_routing_cache()

    entry = _routing_cache.get(device_label)
    if entry:
        return {
            "hub_name": entry.get("native_hub_name"),
            "hub_ip": entry.get("native_hub_ip"),
            "native_device_id": entry.get("native_device_id"),
            "protocol": entry.get("protocol"),
        }
    return None


def get_native_hub_by_device_id(
    device_id: str,
    hub_name: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """
    DEPRECATED — use get_device_by_canonical_id() or get_hub_for_device()
    instead. Kept for backwards compatibility with the old in-memory
    routing-cache path.

    Look up the native hub for a device given a Hubitat per-hub id.

    If `hub_name` is provided, the function does a fast first-pass match
    against that hub. Otherwise (or if the fast pass misses) it falls back
    to a hub-agnostic search across the routing cache. This avoids the
    legacy implicit assumption that ids without context belong to MAIN.

    Args:
        device_id: Hubitat per-hub device ID
        hub_name: Optional hint about which hub this id came from. None
                  means "search all hubs".

    Returns:
        Dict with 'hub_name', 'hub_ip', 'native_device_id', 'protocol',
        'device_label' — or None if not found.
    """
    global _cache_loaded
    if not _cache_loaded:
        _load_routing_cache()

    target = str(device_id)

    # PASS 1: privileged lookup on the named hub (fast path for callers that
    # really do know which hub the id came from).
    for label, entry in _routing_cache.items():
        if (entry.get("native_hub_name") == hub_name
                and entry.get("native_device_id") == target):
            return {
                "hub_name": entry.get("native_hub_name"),
                "hub_ip": entry.get("native_hub_ip"),
                "native_device_id": entry.get("native_device_id"),
                "protocol": entry.get("protocol"),
                "device_label": label,
            }
        mirrors = entry.get("mirrors", {})
        if isinstance(mirrors, dict):
            hub_mirror = mirrors.get(hub_name, {})
            if isinstance(hub_mirror, dict) and hub_mirror.get("id") == target:
                return {
                    "hub_name": entry.get("native_hub_name"),
                    "hub_ip": entry.get("native_hub_ip"),
                    "native_device_id": entry.get("native_device_id"),
                    "protocol": entry.get("protocol"),
                    "device_label": label,
                }

    # PASS 2: fall back to a hub-agnostic search. After the device-selections
    # migration to canonical native IDs, the ID a caller hands us is the
    # NATIVE id on whichever hub physically owns the device — not necessarily
    # the hub named in `hub_name` (which is a legacy default of 'MAIN').
    # Match the first entry whose native_device_id equals our id; that IS
    # the native hub by definition.
    for label, entry in _routing_cache.items():
        if entry.get("native_device_id") == target:
            return {
                "hub_name": entry.get("native_hub_name"),
                "hub_ip": entry.get("native_hub_ip"),
                "native_device_id": entry.get("native_device_id"),
                "protocol": entry.get("protocol"),
                "device_label": label,
            }
        # Last resort: search all hubs' mirror IDs (handles legacy subs that
        # were never migrated, where the id is a mirror of some native).
        mirrors = entry.get("mirrors", {})
        if isinstance(mirrors, dict):
            for m in mirrors.values():
                if isinstance(m, dict) and m.get("id") == target:
                    return {
                        "hub_name": entry.get("native_hub_name"),
                        "hub_ip": entry.get("native_hub_ip"),
                        "native_device_id": entry.get("native_device_id"),
                        "protocol": entry.get("protocol"),
                        "device_label": label,
                    }

    return None


def invalidate_cache() -> None:
    """Force reload of the routing cache on next lookup."""
    global _cache_loaded
    _cache_loaded = False


# In-process cache of (hubitat_id) → {hub_ip, hub_name, hubitat_id, label, id}
# resolved against the `devices` table. Cleared on classifier reruns.
_device_lookup_cache: Dict[str, Optional[Dict[str, Any]]] = {}


def get_hub_for_device(hubitat_id: str) -> Optional[Dict[str, Any]]:
    """
    Resolve a Hubitat device id to its native hub via the canonical `devices`
    table. THIS IS THE PREFERRED LOOKUP — it asks Postgres directly, with no
    hardcoded hub IPs or assumptions about which hub a device 'should' be on.

    Returns:
        {
          'id':         <canonical devices.id>,
          'hub_ip':     <ip of the hub that natively owns this device>,
          'hub_name':   <hub name from _HUB_ENV_MAP, or None if no env match>,
          'hubitat_id': <same id passed in, echoed for symmetry with caller>,
          'label':      <devices.label>,
        }
        or None if the id is not in the `devices` table.

    Note: hubitat_id is not globally unique across hubs in raw Hubitat
    data. If multiple rows match (collision across hubs), this returns the
    first row and emits a warning. Post-migration, device_selections store
    only native ids so collisions should be rare.
    """
    if not hubitat_id:
        return None
    key = str(hubitat_id)
    if key in _device_lookup_cache:
        return _device_lookup_cache[key]

    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")

    # Resolve hub_ip / hub_name from the editable hub_config (source of truth;
    # devices.hub_ip is denormalized cache) via the hub_id FK. Two explicit
    # queries rather than PostgREST resource-embedding: post the 2026-05-26
    # schema split, both tables are dshub storage exposed as `api` VIEWS, and
    # PostgREST cannot infer FK embedding through a view.
    try:
        resp = requests.get(
            f"{postgrest_url}/devices",
            params={
                "select": "id,hubitat_id,label,hub_id",
                "hubitat_id": f"eq.{key}",
            },
            timeout=3,
        )
        if resp.status_code == 200:
            rows = resp.json()
            if not rows:
                _device_lookup_cache[key] = None
                return None
            if len(rows) > 1:
                logger.warning(
                    f"get_hub_for_device({key}): {len(rows)} matching rows "
                    f"in `devices` table — picking first ({rows[0].get('label')!r})"
                )
            row = rows[0]
            hub = {}
            hub_id = row.get("hub_id")
            if hub_id is not None:
                hresp = requests.get(
                    f"{postgrest_url}/hub_config",
                    params={
                        "select": "hub_name,hub_ip,is_enabled",
                        "id": f"eq.{hub_id}",
                    },
                    timeout=3,
                )
                if hresp.status_code == 200 and hresp.json():
                    hub = hresp.json()[0]
            result = {
                "id":         row["id"],
                "hub_id":     row.get("hub_id"),
                "hub_ip":     hub.get("hub_ip"),
                "hub_name":   hub.get("hub_name"),
                "hub_enabled": hub.get("is_enabled", True),
                "hubitat_id": row["hubitat_id"],
                "label":      row.get("label"),
            }
            _device_lookup_cache[key] = result
            return result
    except Exception as e:
        logger.debug(f"get_hub_for_device({key}) failed: {e}")

    _device_lookup_cache[key] = None
    return None


def invalidate_device_lookup_cache() -> None:
    """Drop the in-process devices-table lookup cache. Call after a
    re-classification or any UPDATE/INSERT into the `devices` table."""
    _device_lookup_cache.clear()
    _canonical_id_cache.clear()


def fetch_device_live(device_id: Any) -> Optional[Dict[str, Any]]:
    """
    Fetch a device's live state from the hub that natively owns it.

    Accepts either a canonical devices.id PK (preferred) or a Hubitat
    per-hub id (legacy). Resolves to (hub_ip, hubitat_id) via the
    canonical `devices` table, picks the right HubitatClient, and
    returns the Maker API device dict (or None if unresolvable).

    This is the unified replacement for the legacy pattern of
    `get_default_client().get_device(device_id)` everywhere — it
    eliminates the implicit assumption that device_id lives on MAIN.
    """
    if device_id is None:
        return None
    from services.hubitat_client import get_hub_client_by_ip, get_default_client

    row = get_device_by_canonical_id(device_id) or get_hub_for_device(device_id)
    if row and row.get("hub_ip"):
        client = get_hub_client_by_ip(row["hub_ip"])
        if client is not None:
            try:
                return client.get_device(str(row.get("hubitat_id") or device_id))
            except Exception as e:
                logger.debug(
                    f"fetch_device_live({device_id}) failed on hub "
                    f"{row.get('hub_ip')}: {e}"
                )
                return None

    # Last-resort fallback: default client with the raw id. Surfaces a
    # 404 if the id doesn't exist there (loud failure, easy to debug).
    try:
        return get_default_client().get_device(str(device_id))
    except Exception:
        return None


# Cache canonical_id → row {hub_ip, hub_name, hubitat_id, label}.
# Populated alongside the hubitat-id cache so canonical-id lookups
# don't always need a separate roundtrip.
_canonical_id_cache: Dict[int, Optional[Dict[str, Any]]] = {}


def get_device_by_canonical_id(canonical_id: Any) -> Optional[Dict[str, Any]]:
    """
    Resolve a canonical devices.id PK to its hub + Hubitat id.

    This is the post-Phase-5 inverse of get_hub_for_device(): selections
    and subscriptions store canonical ids, but Hubitat APIs need the
    per-hub hubitat_id. JOIN against hub_config so callers get hub IP/name
    from the editable hubs table.

    Returns:
        {
          'id':         <canonical devices.id>,
          'hub_id':     <hub_config.id>,
          'hub_ip':     <ip from hub_config>,
          'hub_name':   <hub_config.hub_name>,
          'hubitat_id': <Hubitat per-hub id>,
          'label':      <devices.label>,
        }
        or None if no row matches.
    """
    if canonical_id is None:
        return None
    try:
        key = int(canonical_id)
    except (TypeError, ValueError):
        return None
    if key in _canonical_id_cache:
        return _canonical_id_cache[key]

    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        resp = requests.get(
            f"{postgrest_url}/devices",
            params={
                "select": "id,hubitat_id,label,hub_id,hub_config(hub_name,hub_ip,is_enabled)",
                "id": f"eq.{key}",
            },
            timeout=3,
        )
        if resp.status_code == 200:
            rows = resp.json()
            if rows:
                row = rows[0]
                hub = row.get("hub_config") or {}
                result = {
                    "id":          row["id"],
                    "hub_id":      row.get("hub_id"),
                    "hub_ip":      hub.get("hub_ip"),
                    "hub_name":    hub.get("hub_name"),
                    "hub_enabled": hub.get("is_enabled", True),
                    "hubitat_id":  row["hubitat_id"],
                    "label":       row.get("label"),
                }
                _canonical_id_cache[key] = result
                return result
    except Exception as e:
        logger.debug(f"get_device_by_canonical_id({key}) failed: {e}")

    _canonical_id_cache[key] = None
    return None


def refresh_single_device(device_id: Any) -> Dict[str, Any]:
    """
    Force a fresh pull of ONE device from the hub that natively owns it,
    re-fetching its capabilities + attributes via admin fullJson and
    upserting the result into the canonical `devices` table.

    Accepts either a canonical devices.id PK or a Hubitat per-hub id —
    canonical is tried first via get_device_by_canonical_id; if that
    misses, falls back to get_hub_for_device (per-hub id lookup).

    Use after changing a driver on the hub side (the cached caps/attrs
    are now stale and the lazy reuse path in _ingest_into_devices would
    keep the old caps). This forces a fullJson roundtrip.

    Returns:
        {
            "ok":         bool,
            "device_id":  <as passed in>,
            "resolved": {
                "canonical_id": int,
                "hub_ip":       str,
                "hubitat_id":   str,
                "label":        str,
            } or None,
            "caps_count": int,
            "reason":     str  # only set on failure paths
        }

    Errors are surfaced in the return dict (ok=False + reason) rather
    than raised — the route handler can map to HTTP status codes from
    the result.
    """
    if device_id is None or device_id == 0 or device_id == "0":
        return {"ok": False, "device_id": device_id,
                "reason": "device_id must be non-zero (0 = refresh all is handled by run_classification)"}

    # Try canonical id first, then per-hub id. fetch_device_live uses the
    # same two-step resolution; we mirror it so the upsert has the right
    # (hub_ip, hubitat_id) tuple to write against.
    row = get_device_by_canonical_id(device_id) or get_hub_for_device(device_id)
    if not row or not row.get("hub_ip"):
        return {"ok": False, "device_id": device_id, "resolved": None,
                "reason": "no devices row matches that id (try the canonical # or the per-hub Hubitat id)"}

    hub_ip      = row["hub_ip"]
    hub_name    = row.get("hub_name") or ""
    hub_id      = row.get("hub_id")
    hubitat_id  = str(row.get("hubitat_id") or "")
    canonical_id = row.get("id")

    # 1. Pull live admin-side metadata. _fetch_caps_attrs hits fullJson and
    #    returns ([], {}) on any error so we don't half-update with junk.
    caps, attrs = _fetch_caps_attrs(hub_ip, hub_name, hubitat_id)
    if not caps:
        # No caps from the admin path — either the hub is unreachable, the
        # device was removed, or the admin client isn't configured. Surface
        # the failure rather than silently wiping the cached caps to [].
        return {
            "ok": False, "device_id": device_id,
            "resolved": {"canonical_id": canonical_id, "hub_ip": hub_ip,
                         "hubitat_id": hubitat_id, "label": row.get("label")},
            "reason": "fullJson fetch returned empty capabilities — hub unreachable or device missing",
        }

    # 2. We also want the latest label / name / device_type / protocol from
    #    the hub roster (a driver change usually doesn't move these but the
    #    operator may have renamed at the same time). One roster pull per
    #    refresh is acceptable — same cost as the existing reconcile poll
    #    does every minute, but scoped to one device's hub.
    try:
        from services.hubitat_admin_client import get_client, to_maker_shape
        raw = get_client(hub_ip, hub_name).get_device(int(hubitat_id))
    except Exception as e:
        logger.debug(f"refresh_single_device: roster fetch failed: {e}")
        raw = None
    shaped = to_maker_shape(raw) if raw else {}
    inner  = (raw or {}).get("device", raw or {}) if isinstance(raw, dict) else {}
    label   = (shaped or {}).get("label") or row.get("label") or ""
    name    = (shaped or {}).get("name") or inner.get("name") or label or ""
    dtype   = inner.get("type") or ""
    # Protocol is "zwave" / "zigbee" / "lan" / "unknown" — keep whatever the
    # admin shape exposes (the classifier sets it from the same field).
    protocol = inner.get("deviceTypeName") or "unknown"

    # 3. Upsert directly via the canonical RPC (the same one
    #    _ingest_into_devices uses). p_is_name_duplicate left None so the
    #    DB function preserves whatever the classifier last set — we're not
    #    re-running the cross-hub dedup, just refreshing one row's data.
    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        # RPC signature matches _ingest_into_devices' canonical call: 10 args,
        # no p_linked_device. p_is_name_duplicate left as False so a single-
        # device refresh never accidentally flips the dedup verdict — that's
        # the classifier's job during a full sweep.
        r = requests.post(
            f"{postgrest_url}/rpc/upsert_device",
            json={
                "p_hub_ip":            hub_ip,
                "p_hubitat_id":        hubitat_id,
                "p_hub_id":            hub_id,
                "p_name":              name,
                "p_label":             label,
                "p_device_type":       dtype,
                "p_protocol":          protocol,
                "p_capabilities":      caps,
                "p_attributes":        attrs or {},
                "p_is_name_duplicate": False,
            },
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        ok = r.status_code in (200, 201, 204)
        if not ok:
            return {"ok": False, "device_id": device_id,
                    "resolved": {"canonical_id": canonical_id, "hub_ip": hub_ip,
                                 "hubitat_id": hubitat_id, "label": label},
                    "reason": f"upsert RPC returned {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "device_id": device_id,
                "resolved": {"canonical_id": canonical_id, "hub_ip": hub_ip,
                             "hubitat_id": hubitat_id, "label": label},
                "reason": f"upsert failed: {e}"}

    # 4. Invalidate the in-process resolution caches for this device so a
    #    subsequent fetch sees the new row.
    try:
        if canonical_id is not None and int(canonical_id) in _canonical_id_cache:
            _canonical_id_cache.pop(int(canonical_id), None)
        if hubitat_id in _device_lookup_cache:
            _device_lookup_cache.pop(hubitat_id, None)
    except Exception:
        pass

    logger.info(
        f"refresh_single_device: canonical={canonical_id} hub={hub_ip} "
        f"hubitat_id={hubitat_id} label={label!r} caps={len(caps)}"
    )
    return {
        "ok": True,
        "device_id": device_id,
        "resolved": {
            "canonical_id": canonical_id,
            "hub_ip":       hub_ip,
            "hubitat_id":   hubitat_id,
            "label":        label,
        },
        "caps_count": len(caps),
    }
# reload-canonical-devices
# reload-resolve-fix
# reload-main-sweep
# reload-single-device-refresh
