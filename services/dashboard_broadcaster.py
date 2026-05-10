"""
Dashboard WebSocket Broadcaster

Real-time event delivery to dashboard clients via WebSocket.
Replaces the 30-second polling loop with instant push updates.

When Hubitat webhooks arrive, the webhook_router calls broadcast()
to push the event to all connected dashboard clients. The frontend
uses this to update instance cards and KPI data without full-page
re-renders (eliminating flicker).

Architecture:
    WebSocket connection per browser tab
    → asyncio.Queue per connection (backpressure-safe)
    → Events tagged with instance_id for targeted updates
    → Keepalive pings every 15s to detect dead connections
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Set
from datetime import datetime

logger = logging.getLogger(__name__)


class DashboardBroadcaster:
    """
    Manages WebSocket connections for the dashboard.

    Each connected browser tab gets an asyncio.Queue. Events are
    broadcast to all connected clients. The frontend uses these
    events to patch the DOM instead of re-rendering everything.

    Event types:
        - device_event: A device changed state (motion, switch, etc.)
        - instance_update: Instance metadata changed (paused, settings, etc.)
        - instance_metrics: Periodic metrics refresh for a specific instance

    Usage:
        broadcaster = get_dashboard_broadcaster()

        # When a webhook arrives:
        await broadcaster.broadcast({
            "type": "device_event",
            "instance_ids": [1, 3],
            "device_id": "123",
            "device_name": "Office Motion",
            "event_name": "motion",
            "event_value": "active",
            "timestamp": "2026-02-26T23:15:00"
        })

        # In WebSocket endpoint:
        queue = await broadcaster.connect()
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        finally:
            await broadcaster.disconnect(queue)
    """

    def __init__(self):
        self._queues: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def connect(self) -> asyncio.Queue:
        """
        Register a new WebSocket client.

        Returns:
            asyncio.Queue that will receive broadcast events
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        async with self._lock:
            self._queues.append(q)
        logger.info(
            f"Dashboard WS client connected (total: {len(self._queues)})"
        )
        return q

    async def disconnect(self, q: asyncio.Queue):
        """
        Remove a disconnected WebSocket client.

        Args:
            q: The queue to remove
        """
        async with self._lock:
            self._queues = [sq for sq in self._queues if sq is not q]
        logger.info(
            f"Dashboard WS client disconnected (remaining: {len(self._queues)})"
        )

    async def broadcast(self, event: Dict[str, Any]):
        """
        Push an event to all connected dashboard clients.

        Events that can't be delivered (full queue) cause the
        client to be marked for removal. This prevents a slow
        client from blocking the event pipeline.

        Args:
            event: Event payload dict
        """
        if not self._queues:
            return

        event['_ts'] = datetime.now().isoformat()

        async with self._lock:
            dead: List[int] = []
            for idx, q in enumerate(self._queues):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(idx)
                    logger.warning("Dropping slow dashboard WS client")

            for idx in reversed(dead):
                self._queues.pop(idx)

    @property
    def client_count(self) -> int:
        """Number of connected WebSocket clients."""
        return len(self._queues)


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_broadcaster: Optional[DashboardBroadcaster] = None


def get_dashboard_broadcaster() -> DashboardBroadcaster:
    """Get the global dashboard broadcaster singleton."""
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = DashboardBroadcaster()
    return _broadcaster
