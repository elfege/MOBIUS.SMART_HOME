"""
matter_hub_port.eligibility — the preview builder (design §3 step 1).

Produces one verdict per source-hub device BEFORE any window is opened, from a
LIVE matterDetails scan of BOTH hubs (fresher than the discovery table; the
same source the verify step uses, so preview and verify can never disagree
about what "on the target" means).

Verdicts:
    eligible                    — will be attempted
    already_on_target           — MAC present in the target hub's scan (idempotency)
    source_offline              — device offline on the source hub
    no_mac_identity             — no derivable MAC; attempted, but verify falls
                                  back to name match (flagged so the operator
                                  knows the weaker identity basis)

Run-level gates (raised, not per-device):
    HardwareGateError           — target hub is not Matter-capable (C-5 fleet
                                  gate, §7-S1) or hubs invalid/identical

Thread note (§3): all fleet Matter devices are WiFi today (protocol column is
uniformly 'wifi'); matterDetails does not expose a Thread flag. A Thread device
commissioned to a TBR-less target would fail at the pair step and be classified
failed_pair_rejected — detected honestly rather than pre-guessed. Revisit if
Thread devices enter the fleet.
"""

import logging
from typing import Any, Dict, List, Optional

from services.matter_hub_port.hub_endpoints import fetch_matter_devices

logger = logging.getLogger(__name__)

# Hubs that physically cannot commission Matter devices (fleet: the C-5 at
# .71 / home_3 has no Matter radio — §7-S1 hardware gate).
NON_MATTER_HARDWARE_PREFIXES = ("C-5",)


class HardwareGateError(ValueError):
    """The requested source/target pair cannot run at all (router -> 400)."""


def check_run_gates(source_hub: Optional[Dict[str, Any]],
                    target_hub: Optional[Dict[str, Any]]) -> None:
    """Run-level gates. Raises HardwareGateError with an operator-readable
    message; returns silently when the pair is runnable."""
    if not source_hub:
        raise HardwareGateError("source hub not found in hub_config")
    if not target_hub:
        raise HardwareGateError("target hub not found in hub_config")
    if source_hub["id"] == target_hub["id"]:
        raise HardwareGateError("source and target hub are the same hub")
    hw = (target_hub.get("hardware_version") or "").strip()
    if hw.startswith(NON_MATTER_HARDWARE_PREFIXES):
        raise HardwareGateError(
            f"target hub {target_hub['hub_name']} is a {hw} — no Matter radio; "
            f"it can never be a copy target")
    if not target_hub.get("is_enabled"):
        raise HardwareGateError(
            f"target hub {target_hub['hub_name']} is disabled in hub_config")


def build_preview(source_hub: Dict[str, Any],
                  target_hub: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The eligibility table for the preview modal (and the run's work list).

    Returns one row per source-hub Matter device:
        {name, mac, node_id, device_id, online, verdict, reason}
    Ordering matches the source hub's own device order.

    Raises HubEndpointError if either hub cannot be scanned — a preview built
    from a HALF-blind scan would mislabel everything as not-on-target, so a
    scan failure is a hard error, never an empty default.
    """
    source_devices = fetch_matter_devices(source_hub["hub_ip"])
    target_devices = fetch_matter_devices(target_hub["hub_ip"])
    target_macs = {(d.get("mac") or "").strip().lower()
                   for d in target_devices if d.get("mac")}

    rows: List[Dict[str, Any]] = []
    for d in source_devices:
        mac = (d.get("mac") or "").strip().lower() or None
        verdict, reason = "eligible", ""
        if mac and mac in target_macs:
            verdict, reason = "already_on_target", "MAC already present on target hub"
        elif not d.get("online"):
            verdict, reason = "source_offline", "device offline on source hub"
        elif not mac:
            verdict, reason = ("no_mac_identity",
                               "no derivable MAC — verification falls back to name match")
        rows.append({
            "name": d["name"],
            "mac": mac,
            "node_id": d["node_id"],
            "device_id": d["device_id"],
            "online": d["online"],
            "verdict": verdict,
            "reason": reason,
        })
    return rows
