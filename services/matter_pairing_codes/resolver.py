"""
matter_pairing_codes.resolver — ONE "Get Code" action, four honest sources.

The UI has a single button. This module decides where the code comes from, in
the order that yields the most useful result, and — when nothing can — explains
precisely why instead of failing silently.

RESOLUTION ORDER (and the reasoning behind it):
  1. VAULT (factory code)   — never expires, works even if the device is offline
                              or has been reset out of every fabric. Strictly the
                              best code to hold, so it is tried first.
  2. OUR FABRIC (ECM)       — a fresh window on a node we administer. Costs
                              nothing and needs no secret.
  3. HUBITAT FABRIC (ECM)   — a fresh window opened by the hub that owns it.
  4. LABEL REPAIR           — only when the operator supplied the printed code:
                              re-target it at the discriminator the device is
                              actually advertising.
  -> otherwise UnreachableCode, with the reason and the way out.

A note that matters: 2/3 produce a code for ADDING ANOTHER ADMIN (multi-admin).
1 produces the code that also works for a factory-fresh device. They are not
interchangeable, so the source is always reported to the UI.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from services.matter_pairing_codes import sources
from services.matter_pairing_codes.manual_code import InvalidPairingCode
from services.matter_pairing_codes.sources import CodeResult, UnreachableCode

logger = logging.getLogger(__name__)


async def resolve(device: Dict[str, Any],
                  label_code: Optional[str] = None,
                  window_s: int = sources.DEFAULT_WINDOW_S) -> CodeResult:
    """Get a working pairing code for `device`, or raise UnreachableCode.

    device: any subset of
        unique_id        — Matter unique id (vault key, hub scan key)
        mac              — EUI-64 (vault key; the cross-fabric identity)
        our_node_id      — set when OUR matter-server administers it
        hubitat_node_id  — set when a Hubitat hub administers it
        hub_ip, hub_name — that hub
        device_name      — for messages only
    label_code: the printed code, when the operator has it in hand.

    Every blocking call is threaded (single uvicorn worker: bare blocking I/O in
    an async route stalls the whole event loop).
    """
    name = device.get("device_name") or device.get("unique_id") or "device"

    # 1. The vault: the factory code, if we ever captured it.
    try:
        vaulted = await asyncio.to_thread(
            sources.from_vault,
            device.get("unique_id"),
            device.get("mac"),
        )
        if vaulted:
            return vaulted
    except Exception as e:  # noqa: BLE001 — a vault miss must never block the rest
        logger.warning("vault lookup failed for %s: %s", name, e)

    # 2. Our own fabric: open a fresh window on a node we administer.
    our_node = device.get("our_node_id")
    if our_node:
        try:
            return await sources.from_our_fabric(int(our_node), window_s)
        except Exception as e:  # noqa: BLE001 — fall through to the hub
            logger.warning("our-fabric window failed for %s (node %s): %s",
                           name, our_node, e)

    # 3. The Hubitat fabric that administers it.
    hub_ip, hub_node = device.get("hub_ip"), device.get("hubitat_node_id")
    if hub_ip and hub_node:
        try:
            return await asyncio.to_thread(
                sources.from_hubitat, hub_ip, int(hub_node),
                device.get("hub_name", ""))
        except Exception as e:  # noqa: BLE001
            logger.warning("hubitat window failed for %s (%s node %s): %s",
                           name, hub_ip, hub_node, e)

    # 4. The operator's label code, re-targeted at what is actually advertising.
    if label_code:
        return await sources.repair_label_code(label_code)

    # Nothing applies — case D. Say exactly why, and what to do.
    raise UnreachableCode(
        f"No pairing code can be produced for '{name}'. We do not administer it "
        f"on any fabric (so no commissioning window can be opened), its factory "
        f"code was never captured in the vault, and no label code was supplied. "
        f"A Matter passcode is a secret that exists only in the device and on "
        f"its label — it cannot be derived from what the device advertises on "
        f"the network. Scan or type the code from the device's label (use Scan), "
        f"or factory-reset the device to restore its printed code.")


__all__ = ["resolve", "CodeResult", "UnreachableCode", "InvalidPairingCode"]
