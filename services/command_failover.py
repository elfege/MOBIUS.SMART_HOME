"""
Sibling failover targets for device commands — the ITEM2 ruling (2026-07-14).

A physical device often exists on SEVERAL hubs (LAN/ESP drivers installed per
hub; Matter devices multi-admitted or copied hub->hub). The classifier elects
one canonical WINNER row and flags the rest `is_name_duplicate` (mirrors). The
commander routes to the winner — but when the winner's hub copy is deaf (the
2026-07-14 home_2 None-storm: copied outlets HTTP-200 but never actuate), the
command must FALL OVER to a sibling copy instead of failing the operator.

Chain policy (ruled, strict): SEQUENTIAL, never racing — two hubs driving one
radio/actuator concurrently is the storm this codebase keeps refusing to allow.
Order: primary hub's copy first, then by hub id (stable). One tight attempt per
sibling, all inside the commander's existing overall command timeout.

This module is deliberately split from the commander (fanatic modularization):
`order_targets` is PURE (unit-testable rows -> ordered targets);
`fetch_sibling_rows` is the one PostgREST read.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


def fetch_sibling_rows(canonical_id: Any, label: str) -> List[Dict[str, Any]]:
    """Present sibling copies of a canonical device: same label
    (case-insensitive), OTHER rows (the canonical row itself is excluded),
    joined against hub_config for routing. Mirrors ARE wanted here — they are
    exactly the alternates the chain exists for. Returns [] on any failure:
    failover is an enhancement, never a new failure mode."""
    if not label:
        return []
    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        resp = requests.get(
            f"{postgrest_url}/devices",
            params={
                "select": "id,hubitat_id,label,hub_id,"
                          "hub_config(hub_name,hub_ip,is_enabled,is_primary)",
                "label": f"ilike.{label}",
                "is_present": "is.true",
                "id": f"neq.{int(canonical_id)}",
            },
            timeout=3,
        )
        if resp.status_code != 200:
            return []
        return resp.json()
    except Exception as e:  # noqa: BLE001 — degrade to no-failover, log why
        logger.debug(f"fetch_sibling_rows({canonical_id}, {label!r}) failed: {e}")
        return []


def order_targets(sibling_rows: List[Dict[str, Any]],
                  exclude_hub_ip: Optional[str] = None) -> List[Dict[str, Any]]:
    """PURE: sibling rows -> ordered failover targets.

    - drops rows without routing info or on disabled hubs;
    - drops the hub already tried (`exclude_hub_ip` — the winner's hub);
    - dedups per hub (one copy per hub is enough — a second copy on the same
      deaf hub would fail identically);
    - orders: primary hub first, then hub_id ASC (stable, deterministic).

    Returns [{'id', 'hubitat_id', 'hub_ip', 'hub_name'}, ...]
    """
    seen_hubs = set()
    if exclude_hub_ip:
        seen_hubs.add(exclude_hub_ip)
    candidates = []
    for row in sibling_rows:
        hub = row.get("hub_config") or {}
        hub_ip = hub.get("hub_ip")
        if not hub_ip or not row.get("hubitat_id"):
            continue
        if hub.get("is_enabled") is False:
            continue
        if hub_ip in seen_hubs:
            continue
        seen_hubs.add(hub_ip)
        candidates.append({
            "id": row.get("id"),
            "hubitat_id": str(row["hubitat_id"]),
            "hub_ip": hub_ip,
            "hub_name": hub.get("hub_name") or hub_ip,
            "_is_primary": bool(hub.get("is_primary")),
            "_hub_id": row.get("hub_id") or 0,
        })
    candidates.sort(key=lambda t: (not t["_is_primary"], t["_hub_id"]))
    for t in candidates:
        t.pop("_is_primary", None)
        t.pop("_hub_id", None)
    return candidates
