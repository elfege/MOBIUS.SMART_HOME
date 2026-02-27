"""
E2E Test SSE Event Broadcaster

Manages Server-Sent Events (SSE) for the E2E test UI.
Broadcasts test execution progress (step results, scenario summaries)
to connected test modal clients.

Uses asyncio.Queue because FastAPI runs on uvicorn's async event loop.
Each connected browser (EventSource) gets its own queue, tagged with
an instance_id so events are filtered per-modal.

Adapted from the 0_MOBIUS.TILES SSE pattern (which uses threading.Queue for Flask).
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class E2EEventBroadcaster:
    """
    SSE event broadcaster for E2E test sessions.

    Each connected browser tab (EventSource) gets an asyncio.Queue.
    When events are broadcast, they are pushed to all queues matching
    the target instance_id.

    Usage:
        broadcaster = get_e2e_broadcaster()

        # In SSE endpoint:
        async for event in broadcaster.subscribe(instance_id=2):
            yield f"data: {json.dumps(event)}\\n\\n"

        # When test progresses:
        await broadcaster.broadcast(instance_id=2, event_data={...})
    """

    def __init__(self):
        # List of (instance_id, asyncio.Queue) tuples
        self._subscribers: List[Tuple[int, asyncio.Queue]] = []
        self._lock = asyncio.Lock()

    async def subscribe(self, instance_id: int):
        """
        Async generator that yields events for a specific instance.

        Each call creates a new queue for this subscriber. Events are
        yielded as they arrive. A keepalive (None) is yielded every
        15 seconds if no events arrive.

        Args:
            instance_id: Only receive events for this instance

        Yields:
            Dict event data, or None for keepalive
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers.append((instance_id, q))
        logger.info(
            f"E2E SSE subscriber connected for instance {instance_id} "
            f"(total: {len(self._subscribers)})"
        )

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield event
                except asyncio.TimeoutError:
                    # Send keepalive so the connection doesn't drop
                    yield None
        finally:
            async with self._lock:
                self._subscribers = [
                    (iid, sq) for iid, sq in self._subscribers
                    if sq is not q
                ]
            logger.info(
                f"E2E SSE subscriber disconnected for instance {instance_id} "
                f"(remaining: {len(self._subscribers)})"
            )

    async def broadcast(self, instance_id: int, event_data: Dict[str, Any]):
        """
        Broadcast event to all subscribers watching this instance.

        Args:
            instance_id: Target instance ID
            event_data: Event payload dict (will be JSON-serialized)
        """
        event_data['timestamp'] = datetime.now().isoformat()
        event_data['instance_id'] = instance_id

        async with self._lock:
            dead: List[int] = []
            for idx, (sub_iid, q) in enumerate(self._subscribers):
                if sub_iid == instance_id:
                    try:
                        q.put_nowait(event_data)
                    except asyncio.QueueFull:
                        dead.append(idx)
                        logger.warning(
                            f"Dropping slow E2E SSE subscriber for instance {sub_iid}"
                        )

            # Remove dead subscribers (reverse order to preserve indices)
            for idx in reversed(dead):
                self._subscribers.pop(idx)

    @property
    def subscriber_count(self) -> int:
        """Number of active SSE subscribers."""
        return len(self._subscribers)


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_broadcaster: Optional[E2EEventBroadcaster] = None


def get_e2e_broadcaster() -> E2EEventBroadcaster:
    """Get the global E2E event broadcaster singleton."""
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = E2EEventBroadcaster()
    return _broadcaster
