"""
services/matter_debug.py — Matter troubleshooting / diagnostics surface (OOP).

ONE entry point — `MatterDiagnostics` — that BOTH the operator (Matter debug
console UI) and agents (the /api/matter/... debug routes) use to SEE and REPAIR
Matter fabric state without shelling into containers or grepping docker logs.

Why this exists
---------------
The 2026-07-10 incident: a rolling auto-commissioner (matter_discovery Phase 3,
now removed) re-commissioned devices every 5 min, filling each device's ~5
OperationalCredentials fabric slots with orphaned MOBIUS fabrics → genuine
fabric-full → "CHIP 0x0B No memory" at SendNOC → matter-server DoS'd by the
resulting retransmission storm. Diagnosing it needed raw `docker logs` +
hand-parsed CommissionedFabrics attributes. This module makes that a
first-class, reusable capability so future diagnostics are reliable and shared.

Design
------
- OOP + modular: a single `MatterDiagnostics` class, the MatterClient injected as
  a dependency (never imported ad-hoc inside methods). A module-level
  `get_diagnostics()` returns a lazily-built singleton bound to the global client.
- Every method returns a plain JSON-able dict (no framework types) so the same
  result serves a route response AND the UI.
- A ring-buffer logging.Handler (`_OpLogHandler`) attaches to the matter_client
  logger → `op_log()` is the operator's verbose live stream of matter ops.

Matter reference (cluster 0x3E = 62, OperationalCredentials):
  attr 1  = Fabrics (list[FabricDescriptorStruct])
  attr 3  = CommissionedFabrics (uint8, the count)
  attr 5  = CurrentFabricIndex (uint8, OUR index on this device)
  FabricDescriptorStruct fields: 1=RootPublicKey 2=VendorID 3=FabricID
                                 4=NodeID 5=Label 254=FabricIndex
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services import matter_client as _mc

logger = logging.getLogger(__name__)

# OperationalCredentials cluster + attribute ids.
_OPCREDS_CLUSTER = 62
_ATTR_FABRICS = 1
_ATTR_COMMISSIONED_FABRICS = 3
_ATTR_CURRENT_FABRIC_INDEX = 5
_MAX_FABRICS = 5  # Matter spec floor; most devices support exactly 5.

# FabricDescriptorStruct field ids (matter-server serialises struct fields as
# stringified integer keys).
_F_ROOT_PUBKEY = "1"
_F_VENDOR_ID = "2"
_F_FABRIC_ID = "3"
_F_NODE_ID = "4"
_F_LABEL = "5"
_F_FABRIC_INDEX = "254"

# Our controller's Matter vendor id (docker-compose MATTER_SERVER__VENDORID).
# Fabrics on a device carrying this vendor id are OURS — the current one plus any
# orphans left by the old rolling commissioner.
OUR_VENDOR_ID = int(os.environ.get("MATTER_SERVER__VENDORID", "65521"))


class _OpLogHandler(logging.Handler):
    """A bounded in-memory ring buffer of recent matter log records — the
    operator-visible verbose stream. Attached to the matter_client logger so
    every command/response/error the client emits is captured for the UI."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__(level=logging.DEBUG)
        self._buf: "deque[Dict[str, Any]]" = deque(maxlen=capacity)
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._seq += 1
            self._buf.append({
                "seq": self._seq,
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            })
        except Exception:  # noqa: BLE001 - logging must never raise
            pass

    def tail(self, since_seq: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        """Records with seq > since_seq (for incremental polling), newest last."""
        out = [r for r in self._buf if r["seq"] > since_seq]
        return out[-limit:]


class MatterDiagnostics:
    """Read + repair Matter fabric state. Depends on an injected MatterClient."""

    def __init__(self, client: "_mc.MatterClient") -> None:
        self._client = client
        self._oplog = _OpLogHandler()
        # Capture the client's own logging as the verbose stream. The client
        # logs command sends/results at DEBUG, so raise that logger to DEBUG (and
        # the handler) or the verbose trace would be empty.
        _cl = logging.getLogger("services.matter_client")
        _cl.addHandler(self._oplog)
        _cl.setLevel(logging.DEBUG)
        self._oplog.setLevel(logging.DEBUG)

    @property
    def oplog(self) -> "_OpLogHandler":
        """Expose the ring buffer so command routes can capture a per-op trace."""
        return self._oplog

    # -- helpers ---------------------------------------------------------------

    async def _ensure(self) -> None:
        if not self._client.is_connected:
            if not await self._client.connect():
                raise ConnectionError("matter-server not reachable")

    @staticmethod
    def _attr(node: Dict[str, Any], cluster: int, attr: int) -> Any:
        """Pull ep0/cluster/attr from a node's flat attribute map (keys are
        'endpoint/cluster/attribute' strings)."""
        suffix = f"/{cluster}/{attr}"
        for k, v in (node.get("attributes") or {}).items():
            if k.endswith(suffix) and (k.startswith("0/") or k == f"0{suffix}"):
                return v
        return None

    # -- reads -----------------------------------------------------------------

    async def read_fabrics(self, node_id: int) -> Dict[str, Any]:
        """Parsed OperationalCredentials fabric table for one node.

        Returns a graceful error dict (never raises) when the node is absent
        from the current fabric: matterjs raises "Node N does not exist" for an
        unknown node rather than returning None (common after the migration
        reset the fabric to 0 nodes). Propagating that turned EVERY caller —
        read_fabrics route, node_diagnostics, decommission_node — into a 500.
        """
        await self._ensure()
        _NF = {"node_id": node_id, "fabrics": [], "commissioned_fabrics": None,
               "max_fabrics": _MAX_FABRICS, "current_fabric_index": None,
               "full": False, "our_orphan_count": 0}
        try:
            node = await self._client.get_node(node_id)
        except Exception as e:  # noqa: BLE001 - node not in this controller
            return {**_NF, "error": f"{type(e).__name__}: {e}"}
        if not node:
            return {**_NF, "error": "node not found"}
        current = self._attr(node, _OPCREDS_CLUSTER, _ATTR_CURRENT_FABRIC_INDEX)
        count = self._attr(node, _OPCREDS_CLUSTER, _ATTR_COMMISSIONED_FABRICS)
        raw = self._attr(node, _OPCREDS_CLUSTER, _ATTR_FABRICS) or []
        fabrics: List[Dict[str, Any]] = []
        for f in raw if isinstance(raw, list) else []:
            if not isinstance(f, dict):
                continue
            idx = f.get(_F_FABRIC_INDEX)
            vendor = f.get(_F_VENDOR_ID)
            fabrics.append({
                "index": idx,
                "vendor_id": vendor,
                "fabric_id": f.get(_F_FABRIC_ID),
                "node_id": f.get(_F_NODE_ID),
                "label": f.get(_F_LABEL) or "",
                "is_current": (idx == current),
                "is_ours": (vendor == OUR_VENDOR_ID),
            })
        ours_orphaned = [f for f in fabrics if f["is_ours"] and not f["is_current"]]
        return {
            "node_id": node_id,
            "commissioned_fabrics": count,
            "max_fabrics": _MAX_FABRICS,
            "current_fabric_index": current,
            "full": (count is not None and count >= _MAX_FABRICS),
            "our_orphan_count": len(ours_orphaned),
            "fabrics": fabrics,
        }

    async def node_diagnostics(self, node_id: int) -> Dict[str, Any]:
        """Everything worth seeing about one node: fabrics + availability +
        OnOff/Level state + basic identity."""
        await self._ensure()
        node = await self._client.get_node(node_id)
        if not node:
            return {"node_id": node_id, "error": "node not found"}
        fabrics = await self.read_fabrics(node_id)
        # OnOff cluster 6 attr 0 ; LevelControl cluster 8 attr 0 (CurrentLevel).
        on_off = self._attr(node, 6, 0)
        level = self._attr(node, 8, 0)
        return {
            "node_id": node_id,
            "available": node.get("available"),
            "is_bridge": node.get("is_bridge"),
            "on_off": on_off,
            "current_level": level,
            "endpoints": len(node.get("endpoints", []) or []),
            "attribute_count": len(node.get("attributes", {}) or {}),
            "fabrics": fabrics,
        }

    async def server_diagnostics(self) -> Dict[str, Any]:
        """matter-server + WS + circuit-breaker + per-node reachability."""
        breaker = None
        try:
            b = getattr(self._client, "_breaker", None)
            if b is not None and hasattr(b, "snapshot"):
                breaker = b.snapshot()
        except Exception:  # noqa: BLE001
            breaker = None
        info: Dict[str, Any] = {
            "connected": self._client.is_connected,
            "url": getattr(self._client, "url", None),
            "breaker": breaker,
        }
        try:
            await self._ensure()
            nodes = await self._client.get_nodes() or []
            avail = [n.get("node_id") or n.get("nodeId") for n in nodes if n.get("available")]
            unavail = [n.get("node_id") or n.get("nodeId") for n in nodes if not n.get("available")]
            info.update({
                "nodes_total": len(nodes),
                "nodes_available": len(avail),
                "nodes_unavailable": unavail,
            })
        except Exception as e:  # noqa: BLE001
            info["nodes_error"] = str(e)
        return info

    def op_log(self, since_seq: int = 0, limit: int = 200) -> Dict[str, Any]:
        """Verbose live stream of matter-client operations (ring buffer)."""
        records = self._oplog.tail(since_seq=since_seq, limit=limit)
        return {
            "records": records,
            "last_seq": records[-1]["seq"] if records else since_seq,
        }

    # -- repairs ---------------------------------------------------------------

    async def remove_fabric(self, node_id: int, fabric_index: int) -> Dict[str, Any]:
        """RemoveFabric by index — frees one OperationalCredentials slot. An
        administrator (our current fabric) may remove ANY fabric index, so this
        clears orphaned MOBIUS fabrics as well as its own."""
        await self._ensure()
        logger.info("[matter_debug] RemoveFabric node=%s index=%s", node_id, fabric_index)
        result = await self._client.send_command(
            node_id=node_id,
            endpoint_id=0,
            cluster_id=_OPCREDS_CLUSTER,
            command="RemoveFabric",
            payload={"fabricIndex": int(fabric_index)},
        )
        return {"node_id": node_id, "fabric_index": fabric_index, "result": result}

    async def decommission_node(self, node_id: int, keep_current: bool = False) -> Dict[str, Any]:
        """Remove OUR fabrics from a device. keep_current=True removes only the
        ORPHANS (frees slots, keeps the device controllable via our live fabric);
        False removes everything ours (fully leave the device)."""
        table = await self.read_fabrics(node_id)
        removed, errors = [], []
        targets = [
            f for f in table.get("fabrics", [])
            if f["is_ours"] and (not keep_current or not f["is_current"])
        ]
        # Remove the current fabric LAST — removing it first would drop our admin
        # rights and block removing the remaining orphans.
        targets.sort(key=lambda f: f["is_current"])
        for f in targets:
            try:
                await self.remove_fabric(node_id, f["index"])
                removed.append(f["index"])
            except Exception as e:  # noqa: BLE001
                errors.append({"index": f["index"], "error": str(e)})
        return {
            "node_id": node_id,
            "keep_current": keep_current,
            "removed_indices": removed,
            "errors": errors,
            "before": table.get("commissioned_fabrics"),
        }

    async def decommission_all(self, keep_current: bool = False) -> Dict[str, Any]:
        """Decommission our fabrics from EVERY commissioned node."""
        await self._ensure()
        nodes = await self._client.get_nodes() or []
        results = []
        for n in nodes:
            nid = n.get("node_id") or n.get("nodeId")
            if nid is None:
                continue
            try:
                results.append(await self.decommission_node(nid, keep_current=keep_current))
            except Exception as e:  # noqa: BLE001
                results.append({"node_id": nid, "error": str(e)})
        return {"count": len(results), "keep_current": keep_current, "results": results}


# --- module singleton --------------------------------------------------------

_diagnostics: Optional[MatterDiagnostics] = None


def get_diagnostics() -> MatterDiagnostics:
    """Lazily build the singleton bound to the global MatterClient."""
    global _diagnostics
    if _diagnostics is None:
        _diagnostics = MatterDiagnostics(_mc.get_matter_client())
    return _diagnostics
