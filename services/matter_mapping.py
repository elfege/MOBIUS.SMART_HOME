"""
Matter <-> Hubitat mapping resolution — single source of truth.

Background (the bug this fixes, 2026-06-19)
-------------------------------------------
The Matter mapping was an island: ``device_matter_map`` keyed on a bare
``hubitat_device_id`` *frozen at commission time* — and worse, it froze the
Maker-API id, not the admin id. After a device re-pair (new Hubitat id) the
mapping pointed at a dead id (the operator-reported "Mapped to Hubitat #660"
for a device that is now admin id 3871 / canonical 170). Test ON/OFF and the
staleness check both used that dead id, against the wrong id space.

The sound anchor
----------------
``hubitat_matter_devices`` is refreshed by discovery and already carries the
CURRENT admin identity of each commissioned node:

    our_node_id  ->  (hub_ip, hubitat_device_id)        [current admin id]

and ``devices`` (the canonical registry) is uniquely keyed on
``(hub_ip, hubitat_id)``. So a Matter node resolves to its current canonical
device by an EXACT composite-key match — no fuzzy name matching:

    matter node_id
        -> hubitat_matter_devices.our_node_id
        -> (hub_ip, hubitat_device_id)
        -> devices WHERE hub_ip = ? AND hubitat_id = ? AND is_present
        -> canonical devices.id

This disambiguates dual-hub Matter duplicates (hub_ip is part of the key),
fixes the TV-POWER-vs-TV mis-match, survives re-pair (discovery refreshes the
id), and treats canonical devices.id as the currency the rest of the system
uses. A node that no longer resolves to a present device is genuinely STALE.

This module owns ALL mapping resolution so routes / discovery / the client /
the UI stop each re-deriving it with a different key (the redundancy that
caused the inconsistency).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


def _pg() -> str:
    return os.environ.get("POSTGREST_URL", "http://postgrest:3001")


def resolve_node_to_device(node_id: int) -> Optional[Dict[str, Any]]:
    """
    Resolve a Matter node id to its CURRENT canonical device row, or None
    if it can't be resolved (uncommissioned / re-paired-away / removed —
    i.e. a stale mapping).

    Resolution is the exact (hub_ip, hubitat_id) composite match described
    in the module docstring — never the frozen ``device_matter_map`` id.

    Args:
        node_id: Matter node id (our_node_id in hubitat_matter_devices).

    Returns:
        Canonical device dict (id, label, hub_ip, hubitat_id, is_present)
        or None.
    """
    if node_id is None:
        return None
    pg = _pg()
    try:
        # 1) node -> current admin identity
        rh = requests.get(
            f"{pg}/hubitat_matter_devices",
            params={
                "our_node_id": f"eq.{node_id}",
                "select": "hub_ip,hubitat_device_id,device_name,unique_id",
                "limit": "1",
            },
            timeout=5,
        )
        if rh.status_code != 200 or not rh.json():
            return None
        hmd = rh.json()[0]
        hub_ip = hmd.get("hub_ip")
        hub_dev_id = hmd.get("hubitat_device_id")
        if not hub_ip or not hub_dev_id:
            return None

        # 2) exact composite-key match into the canonical registry
        rd = requests.get(
            f"{pg}/devices",
            params={
                "hub_ip": f"eq.{hub_ip}",
                "hubitat_id": f"eq.{hub_dev_id}",
                "is_present": "eq.true",
                "select": "id,label,hub_ip,hubitat_id,is_present",
                "limit": "1",
            },
            timeout=5,
        )
        if rd.status_code != 200 or not rd.json():
            return None
        return rd.json()[0]
    except Exception as e:
        logger.warning(f"resolve_node_to_device({node_id}) failed: {e}")
        return None


def resolve_device_to_node(canonical_id) -> Optional[Dict[str, Any]]:
    """
    Reverse of :func:`resolve_node_to_device`: given a canonical
    ``devices.id``, return the Matter node that controls it (if Mobius's
    matter-server has it commissioned), or None.

    Chain (the same exact composite key, in reverse):
        canonical devices.id
            -> devices(id) -> (hub_ip, hubitat_id)
            -> hubitat_matter_devices[(hub_ip, hubitat_device_id),
                                       our_node_id IS NOT NULL]
            -> {node_id, endpoint_id, is_online, ...}

    This is what device_commander uses to decide whether a command can go
    over Matter directly. It deliberately requires ``our_node_id`` (the
    node is commissioned to US) — a device merely paired to Hubitat's
    Matter bridge but not to our server is NOT Matter-controllable by us
    and must use the Hubitat path.

    Args:
        canonical_id: canonical devices.id (what apps/device_commander use).

    Returns:
        {node_id, endpoint_id, is_online, unique_id, device_name} or None.
    """
    if canonical_id is None:
        return None
    pg = _pg()
    try:
        rd = requests.get(
            f"{pg}/devices",
            params={
                "id": f"eq.{canonical_id}",
                "select": "hub_ip,hubitat_id",
                "limit": "1",
            },
            timeout=5,
        )
        if rd.status_code != 200 or not rd.json():
            return None
        dev = rd.json()[0]

        rh = requests.get(
            f"{pg}/hubitat_matter_devices",
            params={
                "hub_ip": f"eq.{dev['hub_ip']}",
                "hubitat_device_id": f"eq.{dev['hubitat_id']}",
                "our_node_id": "not.is.null",
                "select": "our_node_id,is_online,unique_id,device_name",
                "limit": "1",
            },
            timeout=5,
        )
        if rh.status_code != 200 or not rh.json():
            return None
        hmd = rh.json()[0]

        # matter_endpoint_id lives in device_matter_map (default 1); most
        # single-endpoint devices are endpoint 1. Look it up best-effort.
        endpoint_id = 1
        try:
            rm = requests.get(
                f"{pg}/device_matter_map",
                params={
                    "matter_node_id": f"eq.{hmd['our_node_id']}",
                    "select": "matter_endpoint_id",
                    "limit": "1",
                },
                timeout=5,
            )
            if rm.status_code == 200 and rm.json():
                endpoint_id = rm.json()[0].get("matter_endpoint_id") or 1
        except Exception:
            pass

        return {
            "node_id": hmd["our_node_id"],
            "endpoint_id": endpoint_id,
            "is_online": hmd.get("is_online"),
            "unique_id": hmd.get("unique_id"),
            "device_name": hmd.get("device_name"),
        }
    except Exception as e:
        logger.warning(f"resolve_device_to_node({canonical_id}) failed: {e}")
        return None


def list_mappings() -> List[Dict[str, Any]]:
    """
    Authoritative mapping list, derived from CURRENT discovery state rather
    than the frozen ``device_matter_map``. One row per commissioned node
    (our_node_id set), each resolved to its canonical device (or flagged
    stale).

    Returns a list of dicts:
        {
          matter_node_id, device_name, unique_id, hub_ip, is_online,
          canonical_id,            # None when stale
          canonical_label,         # None when stale
          hubitat_id,              # current admin id (None when stale)
          stale: bool,
        }
    """
    pg = _pg()
    out: List[Dict[str, Any]] = []
    try:
        r = requests.get(
            f"{pg}/hubitat_matter_devices",
            params={
                "our_node_id": "not.is.null",
                "select": "our_node_id,device_name,maker_api_device_name,"
                          "unique_id,hub_ip,is_online",
                "order": "device_name",
            },
            timeout=5,
        )
        if r.status_code != 200:
            logger.warning(f"list_mappings: HTTP {r.status_code} {r.text[:120]}")
            return out
        for hmd in r.json():
            node_id = hmd.get("our_node_id")
            dev = resolve_node_to_device(node_id)
            out.append({
                "matter_node_id": node_id,
                "device_name": (hmd.get("maker_api_device_name")
                                or hmd.get("device_name")),
                "unique_id": hmd.get("unique_id"),
                "hub_ip": hmd.get("hub_ip"),
                "is_online": hmd.get("is_online"),
                "canonical_id": dev.get("id") if dev else None,
                "canonical_label": dev.get("label") if dev else None,
                "hubitat_id": dev.get("hubitat_id") if dev else None,
                "stale": dev is None,
            })
    except Exception as e:
        logger.warning(f"list_mappings failed: {e}")
    return out


def get_device_matter_map_enriched() -> List[Dict[str, Any]]:
    """
    Return the rows of the legacy ``device_matter_map`` table, each ENRICHED
    with the resolved CURRENT canonical device (via ``matter_node_id`` →
    :func:`resolve_node_to_device`).

    Keeps the original ``hubitat_device_id`` key (the delete endpoint still
    keys on it) while surfacing correct current-device info + a ``stale``
    flag, so the mappings table stops displaying the frozen #660-style id.
    Single home for the PostgREST read so the route stops re-deriving it.
    """
    pg = _pg()
    rows: List[Dict[str, Any]] = []
    try:
        r = requests.get(
            f"{pg}/device_matter_map",
            params={"select": "*", "order": "device_name"},
            timeout=5,
        )
        if r.status_code != 200:
            logger.warning(
                f"get_device_matter_map_enriched: HTTP {r.status_code} "
                f"{r.text[:120]}"
            )
            return rows
        for m in r.json():
            dev = resolve_node_to_device(m.get("matter_node_id"))
            m["canonical_id"] = dev.get("id") if dev else None
            m["canonical_label"] = dev.get("label") if dev else None
            m["canonical_hubitat_id"] = dev.get("hubitat_id") if dev else None
            m["canonical_hub_ip"] = dev.get("hub_ip") if dev else None
            m["stale"] = dev is None
            rows.append(m)
    except Exception as e:
        logger.warning(f"get_device_matter_map_enriched failed: {e}")
    return rows
