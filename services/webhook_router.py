"""
Webhook Router Service

Routes incoming Hubitat webhooks to the correct app instances.
Uses device_subscriptions table to determine which instances
should receive each event.

The Maker API can be configured to POST events to our webhook endpoint
when device attributes change. This service parses those events and
dispatches them to all subscribed instances.
"""

import os
import asyncio
import logging
import traceback
from datetime import datetime
from typing import Dict, List, Any, Optional
import requests

from models.event import DeviceEvent
from services.instance_manager import get_instance_manager
from services.device_cache import DeviceCache

# ANSI colors for log output (matches Hubitat event log style)
_CYAN = "\033[96m"     # device name
_GREEN = "\033[92m"    # active/on values
_RED = "\033[91m"      # inactive/off values
_YELLOW = "\033[93m"   # event type
_MAGENTA = "\033[95m"  # routing info
_DIM = "\033[2m"       # dim/secondary info
_BOLD = "\033[1m"      # emphasis
_R = "\033[0m"         # reset


class WebhookRouter:
    """
    Routes Hubitat webhook events to subscribed app instances.

    Flow:
    1. Hubitat sends POST to /api/webhook/event
    2. Router extracts device_id and event_type
    3. Queries device_subscriptions for matching instance_ids
    4. Dispatches event to each matching instance's on_event() method
    5. Logs event to event_log table for audit/debugging

    Webhook payload format from Hubitat:
    {
        "deviceId": "123",
        "name": "motion",
        "value": "active",
        "displayName": "Office Motion Sensor",
        "descriptionText": "Office Motion Sensor motion is active",
        "unit": null,
        "type": null,
        "data": null
    }

    Example usage:
        router = WebhookRouter()

        # In FastAPI route handler:
        @app.post('/api/webhook/event')
        async def handle_webhook(request: Request):
            payload = await request.json()
            routed_count = await router.route_event(payload)
            return {'routed_to': routed_count}
    """

    def __init__(
        self,
        postgrest_url: str = None,
        device_cache: DeviceCache = None
    ):
        """
        Initialize the webhook router.

        Args:
            postgrest_url: URL to PostgREST service
            device_cache: Optional DeviceCache for updating device state
        """
        self.postgrest_url = postgrest_url or os.environ.get(
            'POSTGREST_URL', 'http://postgrest:3001'
        )
        self.device_cache = device_cache
        self.logger = logging.getLogger(__name__)

        # Per-instance event queues + worker tasks. Events for the same
        # instance are serialized through its queue (no races in master());
        # different instances' workers run concurrently. Workers offload
        # the synchronous on_event() to a thread so the asyncio event loop
        # is never blocked by Hubitat command verification cycles.
        self._instance_queues: Dict[int, asyncio.Queue] = {}
        self._instance_workers: Dict[int, asyncio.Task] = {}

        # Cache (hub_ip, hubitat_id) → devices.id so webhook routing doesn't
        # query PostgREST on every event. Misses are negative-cached as None.
        self._device_id_cache: Dict[tuple, Optional[int]] = {}

    def _lookup_canonical_id(self, hub_ip: str, hubitat_id: str) -> Optional[int]:
        """
        Translate (hub_ip, hubitat_id) → devices.id.

        Cached in-memory because the mapping is stable per restart and we
        hit it on every webhook. Cache invalidates only on classifier reruns
        which are rare.
        """
        if not hub_ip or not hubitat_id:
            return None
        cache_key = (hub_ip, hubitat_id)
        if cache_key in self._device_id_cache:
            return self._device_id_cache[cache_key]
        try:
            r = requests.get(
                f"{self.postgrest_url}/devices",
                params={
                    "select": "id",
                    "hub_ip": f"eq.{hub_ip}",
                    "hubitat_id": f"eq.{hubitat_id}",
                },
                timeout=3,
            )
            if r.status_code == 200:
                rows = r.json()
                if rows:
                    canon_id = rows[0]["id"]
                    self._device_id_cache[cache_key] = canon_id
                    return canon_id
        except Exception as e:
            self.logger.debug(f"_lookup_canonical_id failed: {e}")
        # Negative-cache misses too, otherwise meshed mirror events with no
        # canonical row would re-query the DB on every webhook.
        self._device_id_cache[cache_key] = None
        return None

    def invalidate_device_cache(self) -> None:
        """Drop the (hub_ip, hubitat_id) → devices.id cache. Call after a
        classifier rerun that may have added/removed canonical devices."""
        self._device_id_cache.clear()

    def _get_or_create_queue(self, instance_id: int) -> asyncio.Queue:
        """Lazily create the queue + worker task for an instance on first use."""
        queue = self._instance_queues.get(instance_id)
        if queue is None:
            queue = asyncio.Queue()
            self._instance_queues[instance_id] = queue
            self._instance_workers[instance_id] = asyncio.create_task(
                self._instance_worker(instance_id, queue),
                name=f"instance_worker_{instance_id}"
            )
        return queue

    async def _instance_worker(self, instance_id: int, queue: asyncio.Queue) -> None:
        """
        Background worker: drains events for one instance and dispatches
        them to its on_event() in a thread. Serial per instance, so master()
        cannot race against itself; concurrent across instances.
        """
        instance_manager = get_instance_manager()
        while True:
            event = await queue.get()
            try:
                app = instance_manager.get_running_instance(instance_id)
                if app is not None:
                    await asyncio.to_thread(app.on_event, event)
            except Exception as e:
                self.logger.error(
                    f"Worker for instance {instance_id} failed on event {event}: {e}",
                    exc_info=True
                )
            finally:
                queue.task_done()

    def stop_instance_worker(self, instance_id: int) -> None:
        """
        Cancel and discard the worker + queue for an instance. Called by
        InstanceManager.stop_instance() so removed instances don't keep
        consuming or holding queued events.
        """
        task = self._instance_workers.pop(instance_id, None)
        if task is not None:
            task.cancel()
        self._instance_queues.pop(instance_id, None)

    async def route_event(self, webhook_payload: Dict[str, Any]) -> int:
        """
        Route incoming webhook to relevant instances.

        Args:
            webhook_payload: Raw webhook payload from Hubitat

        Returns:
            Number of instances that received the event
        """
        # Parse webhook
        device_id = str(webhook_payload.get('deviceId', ''))
        event_name = webhook_payload.get('name', '')
        event_value = webhook_payload.get('value', '')
        display_name = webhook_payload.get('displayName', '')
        # Hub IP is injected by webhook_dispatcher.py from request.remote_addr.
        # Empty for direct-Hubitat or test callers; that's fine, callers that
        # need hub-disambiguated lookup must send through the dispatcher.
        hub_ip = str(webhook_payload.get('_hub_ip', ''))

        if not device_id or not event_name:
            self.logger.warning(f"Invalid webhook payload: {webhook_payload}")
            return 0

        # Resolve to canonical devices.id when hub_ip is known. Phase 2 only
        # logs the resolution; the actual routing still uses hubitat_device_id
        # via device_subscriptions. Phase 3 will switch routing to canonical id.
        canonical_id = self._lookup_canonical_id(hub_ip, device_id) if hub_ip else None

        # Color the value based on active/on vs inactive/off
        val_color = _GREEN if event_value in ('active', 'on', 'open') else _RED
        canonical_tag = f" {_DIM}canon:{canonical_id}{_R}" if canonical_id else ""
        hub_tag = f" {_DIM}hub:{hub_ip}{_R}" if hub_ip else ""
        self.logger.info(
            f"EVENT  {_CYAN}{display_name}{_R} "
            f"[{_DIM}id:{device_id}{_R}{canonical_tag}{hub_tag}]  "
            f"{_YELLOW}{event_name}{_R} = {val_color}{event_value}{_R}"
        )

        # Create event object
        event = DeviceEvent(
            device_id=device_id,
            device_name=display_name,
            event_type=event_name,
            value=event_value,
            unit=webhook_payload.get('unit'),
            description=webhook_payload.get('descriptionText'),
            source='hubitat_webhook',
            timestamp=datetime.now(),
            raw_payload=webhook_payload
        )

        # Update device cache with new attribute value
        if self.device_cache:
            self.device_cache.update_device_attribute(
                device_id, event_name, event_value
            )

        # Find subscribed instances
        instance_manager = get_instance_manager()
        subscribed_ids = instance_manager.get_subscribed_instances(
            device_id=device_id,
            event_type=event_name
        )

        # Enqueue to each instance's worker queue. The webhook handler returns
        # immediately; workers process events in background threads so a slow
        # Hubitat command (verify retries up to 30s) cannot stall the event
        # loop or other instances.
        routed_to = []
        for instance_id in subscribed_ids:
            try:
                app = instance_manager.get_running_instance(instance_id)
                if app is None:
                    self.logger.warning(
                        f"Instance {instance_id} subscribed but not running"
                    )
                    continue
                queue = self._get_or_create_queue(instance_id)
                await queue.put(event)
                routed_to.append(instance_id)
            except Exception as e:
                self.logger.error(
                    f"Failed to enqueue event for instance {instance_id}: {e}",
                    exc_info=True
                )

        # Log event
        self._log_event(event, routed_to, webhook_payload)

        # Broadcast to E2E test SSE subscribers (if any are listening).
        # This lets the E2E terminal log show live webhook traffic.
        # Best-effort: failures here must never affect event routing.
        try:
            from services.e2e_events import get_e2e_broadcaster
            import asyncio

            broadcaster = get_e2e_broadcaster()
            if broadcaster.subscriber_count > 0:
                e2e_event = {
                    "type": "device_event",
                    "device_id": device_id,
                    "device_name": display_name,
                    "event_name": event_name,
                    "event_value": event_value
                }
                try:
                    loop = asyncio.get_running_loop()
                    for inst_id in routed_to:
                        loop.create_task(
                            broadcaster.broadcast(inst_id, e2e_event)
                        )
                except RuntimeError:
                    pass  # No event loop (shouldn't happen in FastAPI)
        except Exception:
            pass  # E2E broadcast failure must never affect routing

        # Broadcast to dashboard WebSocket clients (real-time card updates).
        # Best-effort: failures here must never affect event routing.
        try:
            from services.dashboard_broadcaster import get_dashboard_broadcaster
            import asyncio

            dash_broadcaster = get_dashboard_broadcaster()
            if dash_broadcaster.client_count > 0:
                dash_event = {
                    "type": "device_event",
                    "instance_ids": routed_to,
                    "device_id": device_id,
                    "device_name": display_name,
                    "event_name": event_name,
                    "event_value": event_value
                }
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(dash_broadcaster.broadcast(dash_event))
                except RuntimeError:
                    pass
        except Exception:
            pass  # Dashboard broadcast failure must never affect routing

        if routed_to:
            self.logger.info(
                f"  {_MAGENTA}→ routed to {len(routed_to)} instance(s):{_R}"
                f" {routed_to}"
            )
        else:
            self.logger.debug(
                f"  {_DIM}→ no subscriptions for device {device_id}"
                f" ({display_name}) event_type={event_name}{_R}"
            )

        return len(routed_to)

    async def route_mode_change(self, webhook_payload: Dict[str, Any]) -> int:
        """
        Route mode change event to all active instances.

        Mode changes affect all instances (unlike device events which
        are subscription-based). on_mode_change() is offloaded to a thread
        per instance so a slow handler does not block the event loop.

        Args:
            webhook_payload: Mode change webhook payload

        Returns:
            Number of instances notified
        """
        new_mode = webhook_payload.get('value', '')

        if not new_mode:
            self.logger.warning(f"Invalid mode change payload: {webhook_payload}")
            return 0

        self.logger.info(f"Mode changed to: {new_mode}")

        # Notify all running instances concurrently in threads
        instance_manager = get_instance_manager()
        targets = [
            (iid, app)
            for iid, app in instance_manager._running_instances.items()
            if hasattr(app, 'on_mode_change')
        ]

        async def _notify(instance_id: int, app: Any) -> bool:
            try:
                await asyncio.to_thread(app.on_mode_change, new_mode)
                return True
            except Exception as e:
                self.logger.error(
                    f"Failed to notify instance {instance_id} of mode change: {e}",
                    exc_info=True
                )
                return False

        results = await asyncio.gather(
            *(_notify(iid, app) for iid, app in targets),
            return_exceptions=False
        )
        notified = sum(1 for ok in results if ok)

        # Update location_modes table
        self._update_mode(new_mode)

        return notified

    def _log_event(
        self,
        event: DeviceEvent,
        routed_to: List[int],
        raw_payload: Dict[str, Any]
    ) -> None:
        """Log event to database for audit/debugging."""
        try:
            requests.post(
                f"{self.postgrest_url}/event_log",
                json={
                    'hubitat_device_id': event.device_id,
                    'device_name': event.device_name,
                    'event_type': event.event_type,
                    'event_value': event.value,
                    'event_unit': event.unit,
                    'routed_to_instances': routed_to,
                    'raw_payload': raw_payload,
                    'received_at': datetime.now().isoformat()
                },
                headers={"Content-Type": "application/json"},
                timeout=5
            )
        except Exception as e:
            self.logger.warning(f"Failed to log event: {e}", exc_info=True)

    def _update_mode(self, mode_name: str) -> None:
        """Update location_modes table with new active mode."""
        try:
            # Set all modes to inactive
            requests.patch(
                f"{self.postgrest_url}/location_modes",
                json={'is_active': False},
                headers={"Content-Type": "application/json"},
                timeout=5
            )

            # Set new mode to active (upsert)
            requests.post(
                f"{self.postgrest_url}/location_modes",
                json={
                    'mode_name': mode_name,
                    'is_active': True,
                    'updated_at': datetime.now().isoformat()
                },
                headers={
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates"
                },
                timeout=5
            )
        except Exception as e:
            self.logger.warning(f"Failed to update mode in database: {e}", exc_info=True)


# Global router instance
_webhook_router: Optional[WebhookRouter] = None


def get_webhook_router() -> WebhookRouter:
    """Get the global webhook router instance."""
    global _webhook_router
    if _webhook_router is None:
        _webhook_router = WebhookRouter()
    return _webhook_router
# reload-phase2
# reload-phase2-cache-flush
