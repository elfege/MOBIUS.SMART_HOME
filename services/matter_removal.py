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
                               performed_by: str = "api", force: bool = False) -> Dict[str, Any]:
    """
    Decommission a Matter node from OUR fabric + soft-delete its registry row.

    The row (and its canonical id) is KEPT — active=false, removed_at=now — so a
    later re-add with the same unique_id reactivates it. Every removal is logged
    to dshub.matter_removals. Safe on an already-gone node: remove_node may error
    ('not found' / unreachable); that's captured, and the row is still
    soft-deleted + logged so ghost/stale nodes can be cleaned regardless.

    force=True skips the fabric decommission entirely (DB-only soft-delete) — for
    dead/ghost nodes where remove_node would hang or is pointless.

    Returns a dict describing what happened (decommissioned, soft_deleted, ...).
    """
    dev = _get_device_by_node(node_id)
    client = mc.get_matter_client()
    decommissioned = False
    decommission_error: Optional[str] = None
    if force:
        decommission_error = "skipped (force)"
    else:
        try:
            await client.remove_node(node_id)
            decommissioned = True
        except Exception as e:  # noqa: BLE001 - node already gone/unreachable: still soft-delete
            decommission_error = str(e)
            logger.info("matter_removal: remove_node(%s) returned: %s", node_id, e)

    # Purge the row if the backing Hubitat device is gone (else soft-delete).
    action = _purge_or_soft_delete(dev) if dev is not None else None

    _log(dev, node_id, action or "removed", decommissioned, reason, performed_by)
    logger.info(
        "matter_removal: %s node %s (decommissioned=%s) by %s",
        action or "removed", node_id, decommissioned, performed_by,
    )
    return {
        "node_id": node_id,
        "decommissioned": decommissioned,
        "decommission_error": decommission_error,
        "soft_deleted": action == "removed",
        "purged": action == "purged",
        "device": {
            "id": (dev or {}).get("id"),
            "label": (dev or {}).get("hubitat_device_label"),
        },
    }


def _get_device_by_uid(unique_id: str) -> Optional[Dict[str, Any]]:
    """Fetch an ACTIVE matter_devices row by unique_id (the card key)."""
    try:
        r = requests.get(
            f"{_pg()}/matter_devices",
            params={"unique_id": f"eq.{unique_id}", "active": "eq.true", "limit": "1"},
            timeout=5,
        )
        if r.status_code == 200 and r.json():
            return r.json()[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("matter_removal: uid lookup for %s failed: %s", unique_id, e)
    return None


def _hubitat_device_gone(hub_ip: Optional[str], device_id, hub_name: str = "") -> bool:
    """
    True ONLY when the Hubitat admin API confirms the device is gone.

    Uses the STRUCTURED, AUTH-AWARE admin client (`get_device` → None on a real
    404), not HTML scraping — so it survives Hubitat UI-copy changes (the "does
    not exist on this hub" wording can change on a platform update) AND handles
    login-secured hubs (a raw GET would get a login page and could FALSELY
    purge). Best-effort: any error / uncertainty returns False, so we never
    hard-delete a device that might still be there.
    """
    if not hub_ip or not device_id:
        return False
    try:
        did = int(device_id)
    except (TypeError, ValueError):
        return False
    try:
        from services.hubitat_admin_client import get_client
        client = get_client(str(hub_ip), hub_name or "matter-removal-check")
        return client.get_device(did) is None   # None == 404 == gone
    except Exception as e:  # noqa: BLE001 - uncertainty -> treat as present (safe)
        logger.debug("matter_removal: admin existence check %s/%s failed: %s", hub_ip, device_id, e)
        return False


def _purge_or_soft_delete(dev: Dict[str, Any]) -> str:
    """
    Hard-DELETE the matter_devices row when its backing Hubitat device is
    confirmed GONE (404 / 'does not exist') — otherwise soft-delete it
    (active=false, removed_at, rediscoverable). Soft-deleting a device that was
    actually deleted from Hubitat just resurrects it on the next scan, so those
    get purged. Returns 'purged' or 'removed'.
    """
    hub_ip = dev.get("hub_ip")
    hub_name = dev.get("hub_name") or ""
    hub_dev_id = dev.get("hubitat_device_id") or dev.get("maker_api_device_id")
    if _hubitat_device_gone(hub_ip, hub_dev_id, hub_name):
        try:
            requests.delete(
                f"{_pg()}/matter_devices",
                params={"id": f"eq.{dev['id']}"},
                headers={"Prefer": "return=minimal"},
                timeout=5,
            )
            logger.info("matter_removal: PURGED device %s (Hubitat %s/%s gone)",
                        dev.get("id"), hub_ip, hub_dev_id)
            return "purged"
        except Exception as e:  # noqa: BLE001
            logger.warning("matter_removal: purge of %s failed, soft-deleting: %s", dev.get("id"), e)
    try:
        requests.patch(
            f"{_pg()}/matter_devices",
            params={"id": f"eq.{dev['id']}"},
            json={"active": False, "removed_at": _now_iso()},
            headers={"Content-Type": "application/json", "Prefer": "return=minimal"},
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("matter_removal: soft-delete of %s failed: %s", dev.get("id"), e)
    return "removed"


def _soft_remove_discovered_row(unique_id: str) -> bool:
    """Mark the hubitat_matter_devices row (THE table the UI's discovered cards
    read) removed. This was THE missing half of removal (audit F5 fallout,
    caught live 2026-07-11 — 'remove all doesn't remove shit'): the service
    only wrote matter_devices.active, a frozen backfill table the UI never
    displays, so cards never disappeared. Returns True if a row was marked."""
    try:
        r = requests.patch(
            f"{_pg()}/hubitat_matter_devices",
            params={"unique_id": f"eq.{unique_id}"},
            json={"is_removed": True, "removed_at": _now_iso()},
            headers={"Content-Type": "application/json",
                     "Prefer": "return=representation"},
            timeout=5,
        )
        return r.status_code in (200, 204) and bool(r.text and r.json())
    except Exception as e:  # noqa: BLE001
        logger.warning("matter_removal: discovered-row soft-remove %s failed: %s",
                       unique_id, e)
        return False


async def remove_matter_device_by_uid(unique_id: str, reason: str = "",
                                       performed_by: str = "api", force: bool = False) -> Dict[str, Any]:
    """
    Soft-remove a DISCOVERED device by unique_id (works for cards that aren't
    commissioned and have no node_id). Decommissions its Matter node first if it
    HAS one and not force. Marks BOTH stores: hubitat_matter_devices.is_removed
    (what the UI cards read — the previously missing half) AND the canonical
    matter_devices row when one exists (post-migration discoveries have none;
    that must NOT abort the removal). A MANUAL re-scan restores the row; the
    periodic discovery timer does NOT resurrect removed rows.
    """
    dev = _get_device_by_uid(unique_id)   # canonical row — may be None (post-migration)
    node_id = (dev or {}).get("matter_node_id")
    decommissioned = False
    decommission_error: Optional[str] = None
    if node_id and not force:
        try:
            await mc.get_matter_client().remove_node(node_id)
            decommissioned = True
        except Exception as e:  # noqa: BLE001 - still soft-delete
            decommission_error = str(e)
            logger.info("matter_removal: remove_node(%s) returned: %s", node_id, e)
    elif force:
        decommission_error = "skipped (force)"

    # THE user-visible half: hide the discovered card.
    ui_removed = _soft_remove_discovered_row(unique_id)

    # Canonical-store half (when a canonical row exists).
    action = "removed"
    if dev is not None:
        action = _purge_or_soft_delete(dev)   # 'purged' if Hubitat device gone
    _log(dev if dev is not None else {"unique_id": unique_id},
         node_id, action, decommissioned, reason, performed_by)
    return {
        "unique_id": unique_id, "node_id": node_id,
        "decommissioned": decommissioned, "decommission_error": decommission_error,
        "soft_deleted": ui_removed or (dev is not None and action == "removed"),
        "purged": action == "purged",
    }


async def remove_all_discovered(force: bool = False, reason: str = "bulk remove",
                                performed_by: str = "api") -> Dict[str, Any]:
    """
    Soft-remove ALL active discovered devices (decommissioning each node unless
    force). Rows are kept + marked removed, so a re-scan brings them all back.
    Returns {total, removed, force}.
    """
    try:
        # Iterate the UI's OWN table (hubitat_matter_devices), not the frozen
        # matter_devices backfill — the old source only knew 8 backfill-era
        # rows, so 'Remove all' skipped everything discovered since (the other
        # half of the 2026-07-11 'removes nothing' bug).
        r = requests.get(
            f"{_pg()}/hubitat_matter_devices",
            params={"is_removed": "not.is.true", "select": "unique_id,our_node_id"},
            timeout=10,
        )
        devices = r.json() if r.status_code == 200 else []
    except Exception as e:  # noqa: BLE001
        logger.warning("matter_removal: bulk list failed: %s", e)
        devices = []
    removed = 0
    for dev in devices:
        uid = dev.get("unique_id")
        if not uid:
            continue
        try:
            res = await remove_matter_device_by_uid(
                uid, reason=reason, performed_by=performed_by, force=force)
            if res.get("soft_deleted"):
                removed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("matter_removal: bulk remove of %s failed: %s", uid, e)
    logger.info("matter_removal: bulk removed %d/%d discovered devices (force=%s)",
                removed, len(devices), force)
    return {"total": len(devices), "removed": removed, "force": force}


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
