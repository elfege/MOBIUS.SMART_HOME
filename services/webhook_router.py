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

        # In Flask route handler:
        @app.route('/api/webhook/event', methods=['POST'])
        def handle_webhook():
            payload = request.get_json()
            routed_count = router.route_event(payload)
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

    def route_event(self, webhook_payload: Dict[str, Any]) -> int:
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

        if not device_id or not event_name:
            self.logger.warning(f"Invalid webhook payload: {webhook_payload}")
            return 0

        # Color the value based on active/on vs inactive/off
        val_color = _GREEN if event_value in ('active', 'on', 'open') else _RED
        self.logger.info(
            f"EVENT  {_CYAN}{display_name}{_R} "
            f"[{_DIM}id:{device_id}{_R}]  "
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

        # Dispatch to each instance
        routed_to = []
        for instance_id in subscribed_ids:
            try:
                app = instance_manager.get_running_instance(instance_id)
                if app:
                    app.on_event(event)
                    routed_to.append(instance_id)
                else:
                    self.logger.warning(
                        f"Instance {instance_id} subscribed but not running"
                    )
            except Exception as e:
                self.logger.error(
                    f"Failed to dispatch event to instance {instance_id}: {e}",
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

    def route_mode_change(self, webhook_payload: Dict[str, Any]) -> int:
        """
        Route mode change event to all active instances.

        Mode changes affect all instances (unlike device events which
        are subscription-based).

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

        # Notify all running instances
        instance_manager = get_instance_manager()
        notified = 0

        for instance_id, app in instance_manager._running_instances.items():
            try:
                if hasattr(app, 'on_mode_change'):
                    app.on_mode_change(new_mode)
                    notified += 1
            except Exception as e:
                self.logger.error(
                    f"Failed to notify instance {instance_id} of mode change: {e}",
                    exc_info=True
                )

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
