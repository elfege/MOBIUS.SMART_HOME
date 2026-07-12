"""
Matter self-healing watchdog.

A background task (spawned in the app lifespan) that keeps the ``matter_client``
connection alive and re-establishes stale per-node sessions, so direct-Matter
control (``matter_primary_enabled``) is reliable instead of silently degrading
to the (broken) Hubitat bridge.

Fixes two failure modes found live on 2026-07-08:

1. **Lazy connection.** The app never proactively ``connect()``s the
   matter_client — it connected only when some caller happened to invoke a
   matter op. An idle-dropped WebSocket then pinned EVERY command to Hubitat
   forever, because nothing re-connected it. The watchdog calls
   ``_ensure_connected()`` on a fixed cadence.

2. **Stale node sessions.** The matter-server reports a node ``"not (yet)
   available"`` (its operational CASE session went stale) and nothing
   re-establishes it — even though the device is reachable (HomeKit controls
   it fine). The watchdog re-interviews unavailable commissioned nodes,
   rate-limited per node and capped per sweep so it never hammers the fabric.

It also publishes a health snapshot (connection state, per-node reachability,
last error, last check time) via :func:`get_health` for the Matter UI's status
panel + failure reports.

This is ordinary app code (not a Workflow script), so ``time``/``datetime`` are
used normally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services import matter_client as mc

logger = logging.getLogger(__name__)

# --- tunables -------------------------------------------------------------
_CHECK_INTERVAL_S = 30          # seconds between watchdog sweeps
_REINTERVIEW_COOLDOWN_S = 300   # per-node: at most one re-interview / 5 min
_MAX_REINTERVIEWS_PER_SWEEP = 2  # gentleness: never re-interview more than N/sweep
_STALE_AFTER_FAILS = 3          # consecutive failed heals -> stop retrying, flag removal

# --- shared health snapshot (read by the /api/matter/watchdog route) ------
_state: Dict[str, Any] = {
    "running": False,
    "connected": False,
    "last_check": None,          # ISO8601
    "last_error": None,
    "nodes_total": 0,
    "nodes_available": 0,
    "nodes_unavailable": [],     # list[node_id]
    "removal_candidates": [],    # node_ids that failed >= _STALE_AFTER_FAILS heals (dead/replaced)
    "last_reinterview": {},      # node_id -> ISO8601 of last heal attempt
}
_reinterview_ts: Dict[int, float] = {}    # node_id -> monotonic ts (rate limiting)
_reinterview_fails: Dict[int, int] = {}   # node_id -> consecutive failed heals
_task: Optional[asyncio.Task] = None


def get_health() -> Dict[str, Any]:
    """Return the latest watchdog health snapshot (for the Matter UI)."""
    return dict(_state)


async def _sweep() -> None:
    """One watchdog pass: keep the connection up, then heal stale node sessions."""
    client = mc.get_matter_client()

    # 1) connection maintenance — the fix for the lazy-connection bug.
    try:
        ok = await client._ensure_connected()
    except Exception as e:  # noqa: BLE001 - watchdog must never crash on this
        _state["connected"] = False
        _state["last_error"] = f"connect: {e}"
        return
    _state["connected"] = bool(ok)
    if not ok:
        _state["last_error"] = "matter-server unreachable"
        return

    # 2) per-node availability + session healing.
    try:
        nodes: List[Dict[str, Any]] = await asyncio.wait_for(
            client.get_nodes(), timeout=8.0
        )
    except Exception as e:  # noqa: BLE001
        _state["last_error"] = f"get_nodes: {e}"
        return

    def _available(n: Dict[str, Any]) -> Optional[bool]:
        # python-matter-server exposes `available`; fall back to `is_online`.
        a = n.get("available")
        return n.get("is_online") if a is None else a

    unavailable: List[int] = [
        n.get("node_id") for n in nodes if _available(n) is False
    ]
    # Nodes that recovered since the last sweep: clear their stale/fail tracking
    # so a device that comes back online is no longer flagged for removal.
    for nid in list(_state["removal_candidates"]):
        if nid not in unavailable:
            _state["removal_candidates"].remove(nid)
    for nid in list(_reinterview_fails):
        if nid not in unavailable:
            _reinterview_fails.pop(nid, None)
    # Publish the scan result FIRST, so the health snapshot is accurate even if
    # the (bounded) re-interviews below are slow.
    _state["nodes_total"] = len(nodes)
    _state["nodes_available"] = len(nodes) - len(unavailable)
    _state["nodes_unavailable"] = unavailable
    _state["last_error"] = None

    # Heal stale sessions — rate-limited per node, capped per sweep, and each
    # call time-bounded so a hung interview can NEVER stall the watchdog. A node
    # that fails _STALE_AFTER_FAILS heals in a row is a dead/replaced commission
    # (device gone, not merely asleep) -> stop retrying and flag it as a removal
    # candidate so the UI surfaces it for decommission instead of retrying forever.
    now = time.monotonic()
    healed = 0
    for nid in unavailable:
        if nid in _state["removal_candidates"]:
            continue  # already given up on -> don't burn heals on a dead node
        if healed >= _MAX_REINTERVIEWS_PER_SWEEP:
            break
        if (now - _reinterview_ts.get(nid, 0.0)) < _REINTERVIEW_COOLDOWN_S:
            continue
        _reinterview_ts[nid] = now
        healed += 1
        try:
            await asyncio.wait_for(client.interview_node(nid), timeout=8.0)
            _state["last_reinterview"][str(nid)] = _iso_now()
            _reinterview_fails.pop(nid, None)
            logger.info("[matter-watchdog] re-interviewed stale node %s", nid)
        except Exception as e:  # noqa: BLE001
            fails = _reinterview_fails.get(nid, 0) + 1
            _reinterview_fails[nid] = fails
            if fails >= _STALE_AFTER_FAILS and nid not in _state["removal_candidates"]:
                _state["removal_candidates"].append(nid)
                logger.warning(
                    "[matter-watchdog] node %s persistently unreachable "
                    "(%d failed heals) -> flagged as removal candidate", nid, fails,
                )
            else:
                logger.warning(
                    "[matter-watchdog] re-interview node %s failed (%d/%d): %s",
                    nid, fails, _STALE_AFTER_FAILS, e,
                )


async def _loop() -> None:
    _state["running"] = True
    logger.info("[matter-watchdog] started (interval=%ss)", _CHECK_INTERVAL_S)
    try:
        while True:
            try:
                await _sweep()
            except Exception as e:  # noqa: BLE001 - defensive: keep the loop alive
                logger.error("[matter-watchdog] sweep error: %s", e, exc_info=True)
            finally:
                _state["last_check"] = _iso_now()
            await asyncio.sleep(_CHECK_INTERVAL_S)
    finally:
        _state["running"] = False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_matter_watchdog() -> Optional[asyncio.Task]:
    """Spawn the supervised watchdog loop. Idempotent."""
    global _task
    if _task is not None and not _task.done():
        return _task
    from services.supervised_tasks import supervised_spawn
    _task = supervised_spawn(_loop(), name="matter_watchdog")
    return _task


def stop_matter_watchdog() -> None:
    """Cancel the watchdog loop (called on app shutdown)."""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
    _state["running"] = False
