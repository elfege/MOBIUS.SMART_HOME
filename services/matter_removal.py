"""
Matter device removal / re-addition flow.

Operator directive (2026-07-08): removing a device must be a SOFT delete — keep
the row + its canonical id, mark it removed, and LOG every removal and
re-addition (troubleshooting + a training substrate for future AI-driven
cleanup). A device re-added with the same identity reactivates the SAME row.

Public API:
  - remove_matter_device()      — decommission from our matter fabric
                                  (remove_node) + soft-delete the
                                  dshub.matter_devices row (active=false,
                                  removed_at=now) + log 'removed'.
  - reactivate_matter_device()  — on re-discovery of a soft-deleted device
                                  (same unique_id): clear the soft-delete + log
                                  'readded'. Called from discovery.

DB writes go through PostgREST (the api views), consistent with the rest of the
codebase.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from services import matter_client as mc

logger = logging.getLogger(__name__)


def _pg() -> str:
    return os.environ.get("POSTGREST_URL", "http://postgrest:3001")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_device_by_node(node_id: int) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(
            f"{_pg()}/matter_devices",
            params={"matter_node_id": f"eq.{node_id}", "limit": "1"},
            timeout=5,
        )
        if r.status_code == 200 and r.json():
            return r.json()[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("matter_removal: device lookup for node %s failed: %s", node_id, e)
    return None


def _log(dev: Optional[Dict[str, Any]], node_id: Optional[int], action: str,
         decommissioned: Optional[bool], reason: str, performed_by: str) -> None:
    """Insert one audit row into dshub.matter_removals (best-effort)."""
    payload = {
        "matter_device_id": (dev or {}).get("id"),
        "unique_id": (dev or {}).get("unique_id"),
        "serial_number": (dev or {}).get("serial_number"),
        "matter_node_id": node_id,
        "hubitat_device_label": (dev or {}).get("hubitat_device_label"),
        "action": action,
        "decommissioned": decommissioned,
        "reason": reason or None,
        "performed_by": performed_by,
    }
    try:
        requests.post(
            f"{_pg()}/matter_removals",
            json=payload,
            headers={"Content-Type": "application/json", "Prefer": "return=minimal"},
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("matter_removal: audit log insert failed: %s", e)


async def remove_matter_device(node_id: int, reason: str = "",
                               performed_by: str = "api") -> Dict[str, Any]:
    """
    Decommission a Matter node from OUR fabric + soft-delete its registry row.

    The row (and its canonical id) is KEPT — active=false, removed_at=now — so a
    later re-add with the same unique_id reactivates it. Every removal is logged
    to dshub.matter_removals. Safe on an already-gone node: remove_node may error
    ('not found' / unreachable); that's captured, and the row is still
    soft-deleted + logged so ghost/stale nodes can be cleaned regardless.

    Returns a dict describing what happened (decommissioned, soft_deleted, ...).
    """
    dev = _get_device_by_node(node_id)
    client = mc.get_matter_client()
    decommissioned = False
    decommission_error: Optional[str] = None
    try:
        await client.remove_node(node_id)
        decommissioned = True
    except Exception as e:  # noqa: BLE001 - node already gone/unreachable: still soft-delete
        decommission_error = str(e)
        logger.info("matter_removal: remove_node(%s) returned: %s", node_id, e)

    if dev is not None:
        try:
            requests.patch(
                f"{_pg()}/matter_devices",
                params={"id": f"eq.{dev['id']}"},
                json={"active": False, "removed_at": _now_iso()},
                headers={"Content-Type": "application/json", "Prefer": "return=minimal"},
                timeout=5,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("matter_removal: soft-delete of device %s failed: %s", dev.get("id"), e)

    _log(dev, node_id, "removed", decommissioned, reason, performed_by)
    logger.info(
        "matter_removal: removed node %s (decommissioned=%s soft_deleted=%s) by %s",
        node_id, decommissioned, dev is not None, performed_by,
    )
    return {
        "node_id": node_id,
        "decommissioned": decommissioned,
        "decommission_error": decommission_error,
        "soft_deleted": dev is not None,
        "device": {
            "id": (dev or {}).get("id"),
            "label": (dev or {}).get("hubitat_device_label"),
        },
    }


def reactivate_matter_device(unique_id: str, performed_by: str = "discovery") -> bool:
    """
    Reactivate a SOFT-DELETED device on re-discovery (same unique_id): clear
    active/removed_at and log 'readded'. Returns True if a soft-deleted row was
    reactivated, False otherwise (nothing to do). Sync — safe to call from the
    discovery upsert path.
    """
    try:
        r = requests.get(
            f"{_pg()}/matter_devices",
            params={"unique_id": f"eq.{unique_id}", "active": "eq.false", "limit": "1"},
            timeout=5,
        )
        if r.status_code != 200 or not r.json():
            return False
        dev = r.json()[0]
        requests.patch(
            f"{_pg()}/matter_devices",
            params={"id": f"eq.{dev['id']}"},
            json={"active": True, "removed_at": None},
            headers={"Content-Type": "application/json", "Prefer": "return=minimal"},
            timeout=5,
        )
        _log(dev, dev.get("matter_node_id"), "readded", None, "device re-appeared", performed_by)
        logger.info("matter_removal: reactivated device %s (unique_id %s)", dev.get("id"), unique_id)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("matter_removal: reactivate %s failed: %s", unique_id, e)
        return False
