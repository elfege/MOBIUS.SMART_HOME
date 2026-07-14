"""
matter_hub_port.orchestrator — the strictly-sequential hub->hub COPY worker.

HARD INVARIANT (design §4, operator 2026-07-13): a Hubitat hub processes ONE
Matter device at a time — as source (open window) AND as target (commissioning).
The run is therefore serialized on the DEVICE, end-to-end:

    open window (A) -> pair (B) -> verify by rescan (B) -> rename (B)
    -> settle -> next device

No interleaving, no pipelining, no overlap — not conditionally, not later.
The WHOLE run holds the global Matter-pairing mutex (dscore, migration 013),
shared with Commission All and manual pairing, so "sequential" is true ACROSS
features, not just within this one.

COPY semantics: nothing in this module (or package) ever removes a fabric.

Guards cloned from _bulk_commission_worker (the verified-17/17 skeleton):
per-run MAC dedup · per-device hard ceiling · 3-consecutive-failure circuit
breaker · settle pause between devices · background task + status polling
(a sync response would die at the proxy read-timeout).

Single uvicorn worker -> the module-level state dict is race-free enough,
same as Commission All's.
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.matter_hub_port import audit
from services.matter_hub_port.eligibility import build_preview
from services.matter_hub_port.hub_endpoints import (
    HubEndpointError,
    consume_setup_code,
    fetch_matter_devices,
    open_pairing_window,
)
from services.matter_pairing_lock import PairingLockBusy, matter_pairing_lock

logger = logging.getLogger(__name__)

SETTLE_S = 8.0                 # hub "breath" between devices (Commission All value)
DEVICE_CEILING_S = 180.0       # hard per-device ceiling — two-hub round trip,
                               # so higher than Commission All's single-hub 120s
VERIFY_TIMEOUT_S = 90.0        # rescan window for the MAC to appear on target
VERIFY_POLL_INTERVAL_S = 5.0
MAX_CONSECUTIVE_FAILURES = 3   # circuit breaker: a wedged hub must not be ground through

_run_state: Dict[str, Any] = {"running": False}


def run_state() -> Dict[str, Any]:
    """The live status dict (returned verbatim by the status endpoint)."""
    return _run_state


def _record(status_key: str, name: str, status: str, detail: str = "") -> None:
    """Append one per-device outcome to the state dict and bump its counter."""
    _run_state["results"].append({"device": name, "status": status, "detail": detail})
    _run_state[status_key] += 1


async def _port_one_device(device: Dict[str, Any],
                           source_hub: Dict[str, Any],
                           target_hub: Dict[str, Any],
                           row_id: Optional[int]) -> None:
    """The per-device atom: window -> pair -> verify -> rename.

    Raises HubEndpointError (classified) or asyncio.TimeoutError upward; the
    caller owns counters, audit terminal states and the circuit breaker.
    """
    name = device["name"]

    # 1. Open the ECM window on the SOURCE hub (device's own hub).
    await asyncio.to_thread(audit.update_row, row_id, "window_open",
                            f"opening window on {source_hub['hub_name']}")
    setup_code = await asyncio.to_thread(
        open_pairing_window, source_hub["hub_ip"], device["node_id"])

    # 2. Consume the code on the TARGET hub — promptly (window is TTL-bound).
    await asyncio.to_thread(audit.update_row, row_id, "pairing",
                            f"target {target_hub['hub_name']} consuming code")
    target_node_id = await asyncio.to_thread(
        consume_setup_code, target_hub["hub_ip"], setup_code)
    _run_state["current"] = f"{name}: pairing on {target_hub['hub_name']} (node {target_node_id})"

    # 3. VERIFY: poll the target's matterDetails until the device's MAC appears.
    #    A pairing endpoint's HTTP 200 is NOT success (recycled-IP lesson) —
    #    only the rescan match is. MAC-less devices fall back to exact-name
    #    match (flagged in the preview as the weaker identity basis).
    mac = (device.get("mac") or "").strip().lower() or None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + VERIFY_TIMEOUT_S
    verified_row: Optional[Dict[str, Any]] = None
    while loop.time() < deadline:
        scan = await asyncio.to_thread(fetch_matter_devices, target_hub["hub_ip"])
        if mac:
            verified_row = next(
                (d for d in scan if (d.get("mac") or "").strip().lower() == mac), None)
        else:
            verified_row = next(
                (d for d in scan
                 if d["name"].strip().lower() == name.strip().lower()), None)
        if verified_row:
            break
        await asyncio.sleep(VERIFY_POLL_INTERVAL_S)
    if not verified_row:
        raise HubEndpointError(
            f"device did not appear in {target_hub['hub_name']}'s matterDetails "
            f"within {VERIFY_TIMEOUT_S:.0f}s (pair reported node {target_node_id})",
            classification="verify_timeout")
    await asyncio.to_thread(audit.update_row, row_id, "verified",
                            f"MAC confirmed on target (device id {verified_row['device_id']})")

    # 4. NAME PARITY: source label -> target device, via the admin client.
    #    A rename failure must NOT fail the copy (the fabric exists) — it is
    #    recorded honestly instead.
    try:
        from services.hubitat_admin_client import get_client
        client = get_client(target_hub["hub_ip"], target_hub["hub_name"])
        renamed = await asyncio.to_thread(
            client.set_device_label, int(verified_row["device_id"]), name)
        if renamed:
            await asyncio.to_thread(audit.update_row, row_id, "renamed",
                                    f"label '{name}' applied on target")
        else:
            await asyncio.to_thread(
                audit.update_row, row_id, "done",
                f"copied OK; rename returned false — set the label by hand")
            return
    except Exception as e:  # noqa: BLE001 — copy succeeded; rename is best-effort
        await asyncio.to_thread(
            audit.update_row, row_id, "done",
            f"copied OK; rename failed: {e} — set the label by hand")
        return

    await asyncio.to_thread(audit.update_row, row_id, "done", "copied + renamed")


async def run_port(source_hub: Dict[str, Any],
                   target_hub: Dict[str, Any],
                   device_macs: Optional[List[str]] = None) -> None:
    """The background run (design §4). Assumes the router already ran
    check_run_gates and set _run_state['running'] atomically.

    device_macs: operator's preview subset (lowercase MACs); None = all eligible.
    """
    st = _run_state
    run_id = st["run_id"]
    seen_macs: set = set()
    consecutive_failures = 0
    aborted = False

    # Build the work list from a fresh preview (idempotency lives here: rows
    # already_on_target are skipped, so re-running a partial run resumes
    # naturally with no run-state persistence).
    try:
        preview = await asyncio.to_thread(build_preview, source_hub, target_hub)
    except Exception as e:  # noqa: BLE001
        st.update(running=False, aborted=True,
                  message=f"preview scan failed, nothing attempted: {e}")
        return

    wanted = ({m.strip().lower() for m in device_macs} if device_macs else None)
    work: List[Dict[str, Any]] = []
    for row in preview:
        if wanted is not None and (row["mac"] or "") not in wanted:
            continue
        if row["verdict"] in ("eligible", "no_mac_identity"):
            work.append(row)
        else:
            _record("skipped", row["name"], f"skipped_{row['verdict']}", row["reason"])
            rid = await asyncio.to_thread(
                audit.open_row, run_id, source_hub["id"], target_hub["id"],
                row["mac"], None, row["name"])
            await asyncio.to_thread(audit.update_row, rid,
                                    f"skipped_{row['verdict']}", row["reason"])

    st["total"] = len(work) + st["skipped"]

    # GLOBAL MATTER-PAIRING MUTEX — held for the WHOLE run, shared with
    # Commission All and manual pairing (§4 / §7-S4: one pairing slot fleet-wide).
    try:
        lock_cm = matter_pairing_lock(
            "hub_port_copy",
            f"{len(work)} device(s) {source_hub['hub_name']} -> {target_hub['hub_name']}",
            ttl_s=int(len(work) * (DEVICE_CEILING_S + SETTLE_S)) + 300,
        )
        await lock_cm.__aenter__()
    except PairingLockBusy as e:
        st.update(running=False, aborted=True, message=str(e))
        logger.warning(f"hub-port copy refused to start: {e}")
        return

    try:
        for n, device in enumerate(work):
            # CANCEL CHECK — between devices ONLY (operator UX contract,
            # 2026-07-14): the in-flight device always completes or hits its
            # ceiling first, because aborting mid-handshake leaves a device
            # half-paired. request_cancel() sets the flag; we honor it at the
            # only clean cancellation point there is.
            if st.get("cancel_requested"):
                st["cancelled"] = True
                logger.info("hub-port copy CANCELLED by operator after "
                            f"{len(st['results'])} device(s)")
                break

            name = device["name"]
            mac = (device["mac"] or "").strip().lower()
            st["done"] = len(st["results"])
            st["current"] = name

            # Per-run MAC dedup — one copy per PHYSICAL device.
            if mac and mac in seen_macs:
                _record("skipped", name, "skipped_mac_duplicate",
                        "same physical device (MAC) already copied this run")
                continue

            row_id = await asyncio.to_thread(
                audit.open_row, run_id, source_hub["id"], target_hub["id"],
                device["mac"], None, name)
            try:
                await asyncio.wait_for(
                    _port_one_device(device, source_hub, target_hub, row_id),
                    timeout=DEVICE_CEILING_S,
                )
                _record("ok", name, "done")
                consecutive_failures = 0
                if mac:
                    seen_macs.add(mac)
            except asyncio.TimeoutError:
                detail = f"exceeded {DEVICE_CEILING_S:.0f}s per-device ceiling"
                await asyncio.to_thread(audit.update_row, row_id,
                                        "failed_timeout", detail)
                _record("failed", name, "failed_timeout", detail)
                consecutive_failures += 1
            except HubEndpointError as e:
                status = f"failed_{e.classification}"
                await asyncio.to_thread(audit.update_row, row_id, status, str(e))
                _record("failed", name, status, str(e))
                consecutive_failures += 1
            except Exception as e:  # noqa: BLE001
                await asyncio.to_thread(audit.update_row, row_id,
                                        "failed_exception", str(e))
                _record("failed", name, "failed_exception", str(e))
                consecutive_failures += 1

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                aborted = True
                logger.error(f"hub-port copy ABORTED: {consecutive_failures} "
                             f"consecutive failures — pairing flow looks wedged")
                break

            # Settle pause between devices (not after the last): both hubs
            # recover before the next window.
            if n < len(work) - 1:
                st["current"] = f"settling {int(SETTLE_S)}s (hub recovery)"
                await asyncio.sleep(SETTLE_S)
    finally:
        # Release the shared mutex FIRST — a wedged run must never keep Matter
        # pairing locked for other features (it also self-expires; don't rely on it).
        try:
            await lock_cm.__aexit__(None, None, None)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"pairing-lock release failed (it will expire): {e}")
        st["done"] = len(st["results"])
        st["current"] = None
        st["running"] = False
        st["finished_at"] = datetime.now().isoformat(timespec="seconds")
        st["aborted"] = aborted
        st["message"] = (
            f"Copied {st['ok']}/{st['total']} "
            f"{source_hub['hub_name']} -> {target_hub['hub_name']}"
            + (f", {st['failed']} failed" if st['failed'] else "")
            + (f", {st['skipped']} skipped" if st['skipped'] else "")
            + (" — ABORTED (consecutive failures)" if aborted else "")
            + (" — CANCELLED by operator (remaining devices untouched; "
               "re-run to resume, already-copied devices are skipped)"
               if st.get("cancelled") else ""))
        logger.info(f"hub-port copy finished: {st['message']}")

        # RUN-END RECONCILE (design §3.6 — was missing; caught during the maiden
        # production run 2026-07-14): the copies are real on the hubs the moment
        # they verify, but hubitat_matter_devices (and the UI cards) lag until a
        # discovery scan. Trigger one now so the operator sees the new sibling
        # reality without pressing Scan Hub. Self-call to our own endpoint —
        # the scan logic lives inline in app.py's route (the other lane's file),
        # so an HTTP self-call is the decoupled path. Best-effort: a reconcile
        # hiccup must never turn a successful run into a failure.
        if st["ok"]:
            def _reconcile():
                import requests as req
                req.post("http://127.0.0.1:5000/api/matter/discover", timeout=120)
            try:
                await asyncio.to_thread(_reconcile)
                logger.info("hub-port copy: post-run discovery reconcile done")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"post-run discovery reconcile failed "
                               f"(UI lags until next scan): {e}")


def request_cancel() -> bool:
    """Ask the running copy to stop at the next between-devices checkpoint.

    Returns True when a run was live and the flag was set; False when nothing
    is running (the router maps that to 409). The in-flight device ALWAYS
    finishes (or times out) first — aborting mid-handshake would leave it
    half-paired; the checkpoint in run_port is the only clean stop.
    Cancellation loses nothing: re-running the same source→target resumes,
    because already-copied devices skip as already_on_target."""
    if not _run_state.get("running"):
        return False
    _run_state["cancel_requested"] = True
    _run_state["current"] = ((_run_state.get("current") or "")
                             + "  (cancelling after this device…)")
    return True


def init_run_state(source_hub: Dict[str, Any], target_hub: Dict[str, Any]) -> str:
    """Reset the state dict for a new run and return its run_id.

    The router calls this synchronously BEFORE spawning the task, so the
    running flag flips atomically within the single-worker event loop (no
    double-start window)."""
    run_id = str(uuid.uuid4())
    _run_state.clear()
    _run_state.update({
        "running": True, "run_id": run_id, "aborted": False,
        "cancel_requested": False, "cancelled": False,
        "source_hub": source_hub["hub_name"], "target_hub": target_hub["hub_name"],
        "total": 0, "done": 0, "ok": 0, "failed": 0, "skipped": 0,
        "results": [], "current": "starting (preview scan)…", "message": None,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
    })
    return run_id
