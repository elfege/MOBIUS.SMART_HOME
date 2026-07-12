"""
Matter legacy-fabric orphan purge — frees device fabric slots WITHOUT resets.

WHY THIS EXISTS (2026-07-11/12)
-------------------------------
The 2026-02..07 auto-commissioner era + several controller-storage generations
(0_smart_home_matter_data, tiles_matter_data, 0_mobiussmart_home_matter_data)
left devices' OperationalCredentials tables saturated with orphaned MOBIUS
fabrics (vendor 65521). Consequence: Hubitat pairing fails with
"No memory at SendNOC" (CHIP 0x0B) — the device has no free fabric slot.

The LIVE controller (matterjs on :5580) runs a FRESH fabric the devices don't
know, so it cannot remove anything. The purge therefore runs through a
SIDE-BOOTED controller on :5581 that adopts a COPY of the old
python-matter-server storage — inheriting the old 65521 fabric identity, which
IS in the devices' tables and has admin rights (and matterjs invokes actually
land, unlike the archived python-matter-server's).

OPERATOR-EXPLICIT ONLY. Never scheduled, never chained. One run per side-boot.

SETUP (what this script expects to already exist)
-------------------------------------------------
    docker volume create matter_legacy_purge_copy
    docker run --rm -v 0_mobiussmart_home_matter_data:/from:ro \
        -v matter_legacy_purge_copy:/to alpine cp -a /from/. /to/
    docker run -d --name matter-legacy-purge --network=host -u 0:0 \
        -e PORT=5581 -v matter_legacy_purge_copy:/data \
        ghcr.io/matter-js/matterjs-server:stable

RUN (from the repo root; executes inside the app container for the venv):
    docker exec -i -w /app smarthome-app \
        python3 scripts/matter_legacy_orphan_purge.py [--minutes 15] [--dry-run]

SAFETY INVARIANTS
-----------------
- ONLY fabric entries with vendor id 65521 (the Matter test vendor every MOBIUS
  controller generation used) are EVER removed. Hubitat (4996), Apple/HomeKit
  (4937) and anything else are structurally untouchable.
- Our OWN entry (the side-boot identity) is removed LAST on each node —
  removing it first would drop admin rights mid-purge.
- Every removal is verified by an unfiltered re-read of the fabric table.
- Nodes that never come available within the deadline are reported, not forced.

After a successful run, tear the side controller down:
    docker rm -f matter-legacy-purge && docker volume rm matter_legacy_purge_copy
(The ORIGINAL 0_mobiussmart_home_matter_data volume is never modified.)
"""

import argparse
import asyncio
import json
import sys
import time

sys.path.insert(0, "/app")

from services.matter_client import MatterClient  # noqa: E402

LEGACY_WS = "ws://<LAN_IP>:5581/ws"
OUR_VENDOR = 65521          # ONLY entries with this vendor are ever removed
OPCREDS = 62                # OperationalCredentials cluster
ATTR_FABRICS = 1
F_VENDOR, F_LABEL, F_INDEX, F_NODEID = "2", "5", "254", "4"


async def read_fabric_entries(client: MatterClient, node_id: int):
    """Unfiltered OperationalCredentials.Fabrics read → list of raw entries.
    A LIVE read (not the cached/filtered node dump) — returns every fabric on
    the device, which is what makes cross-generation orphans visible."""
    r = await client._send_command(
        "read_attribute",
        {"node_id": node_id, "attribute_path": f"0/{OPCREDS}/{ATTR_FABRICS}",
         "fabric_filtered": False},
    )
    for v in (r or {}).values():
        if isinstance(v, list):
            return v
    return []


async def purge_node(client: MatterClient, node_id: int, our_ctrl_node: int,
                     dry_run: bool) -> dict:
    """Remove every vendor-65521 fabric entry from one node (ours LAST).
    Returns a per-node report dict; never raises.

    ORDERING (fixed after the gen-2 run): "ours last" is decided by the
    device's CurrentFabricIndex attribute (0/62/5) — the fabric index our
    session is running on. The first version compared FabricDescriptor.NodeID
    against the CONTROLLER's node id, but that field holds the DEVICE's node id
    within the fabric, so the sort never matched, our own entry could go first,
    the session died, and remaining orphans errored 'Node does not exist'."""
    rep = {"node_id": node_id, "removed": [], "kept": [], "errors": []}
    try:
        entries = await read_fabric_entries(client, node_id)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"read failed: {e}")
        return rep

    current_idx = None
    try:
        r = await client._send_command(
            "read_attribute", {"node_id": node_id, "attribute_path": "0/62/5"})
        for v in (r or {}).values():
            if isinstance(v, int):
                current_idx = v
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"CurrentFabricIndex read failed: {e}")

    ours, foreign = [], []
    for f in entries:
        (ours if f.get(F_VENDOR) == OUR_VENDOR else foreign).append(f)
    rep["kept"] = [
        {"index": f.get(F_INDEX), "vendor": f.get(F_VENDOR), "label": f.get(F_LABEL)}
        for f in foreign
    ]
    rep["current_fabric_index"] = current_idx
    # Non-current orphans FIRST; the fabric our session runs on LAST.
    ours.sort(key=lambda f: 1 if f.get(F_INDEX) == current_idx else 0)

    for f in ours:
        idx = f.get(F_INDEX)
        tag = f"idx={idx} label={f.get(F_LABEL)!r} ctrl_node={f.get(F_NODEID)}"
        if dry_run:
            rep["removed"].append(f"DRY-RUN would remove {tag}")
            continue
        try:
            await client._send_command(
                "device_command",
                {"node_id": node_id, "endpoint_id": 0, "cluster_id": OPCREDS,
                 "command_name": "RemoveFabric", "payload": {"fabricIndex": int(idx)}},
            )
            rep["removed"].append(tag)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"RemoveFabric {tag}: {e}")

    # Verify (only meaningful while our entry still exists; after removing
    # ourselves the session may drop — treat verify failure then as expected).
    if not dry_run and rep["removed"]:
        try:
            after = await read_fabric_entries(client, node_id)
            rep["after_count"] = len(after)
            rep["after_ours"] = sum(1 for f in after if f.get(F_VENDOR) == OUR_VENDOR)
        except Exception:
            rep["after_count"] = "session dropped (expected after self-removal)"
    return rep


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=15.0,
                    help="how long to keep waiting for nodes to come available")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    client = MatterClient(url=LEGACY_WS)
    if not await client.connect():
        print(json.dumps({"fatal": f"cannot connect {LEGACY_WS}"})); return
    info = await client.get_server_info()
    our_ctrl_node = info.get("controller_node_id")
    print(f"# legacy fabric {info.get('compressed_fabric_id')} "
          f"ctrl_node={our_ctrl_node} dry_run={args.dry_run}", flush=True)

    deadline = time.monotonic() + args.minutes * 60
    done, reports = set(), []
    while time.monotonic() < deadline:
        nodes = await client.get_nodes()
        pending = [n for n in nodes if n["node_id"] not in done]
        if not pending:
            break
        for n in pending:
            if not n.get("available"):
                continue
            rep = await purge_node(client, n["node_id"], our_ctrl_node, args.dry_run)
            reports.append(rep)
            done.add(n["node_id"])
            print(json.dumps(rep), flush=True)
        if len(done) < len(nodes):
            await asyncio.sleep(20)   # wait for more sessions to establish

    nodes = await client.get_nodes()
    unreached = [n["node_id"] for n in nodes if n["node_id"] not in done]
    print(json.dumps({
        "summary": {
            "purged_nodes": sorted(done),
            "never_available": unreached,
            "note": ("nodes never_available keep their orphans — re-run after "
                     "power-cycling those devices, or accept factory reset for "
                     "genuinely dead ones"),
        }
    }), flush=True)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
