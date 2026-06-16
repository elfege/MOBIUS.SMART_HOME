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
import re
import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
import requests

# Hubitat appends " on Home N" to the LABEL of mesh-mirrored devices on
# non-native hubs. The native row keeps the clean label. To detect
# mirrors, strip this suffix before looking up the canonical row.
_MESH_SUFFIX_RE = re.compile(r" on Home \d+$")

from models.event import DeviceEvent
from services.instance_manager import get_instance_manager
from services.device_cache import DeviceCache
from services.supervised_tasks import supervised_spawn

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

        # Cache label → canonical row {id, hub_ip, hubitat_id}. Used to
        # detect mesh-mirror events (event hub_ip != canonical hub_ip → mirror).
        self._label_to_canonical: Dict[str, Optional[Dict[str, Any]]] = {}

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

    def _lookup_canonical_by_label(self, label: str) -> Optional[Dict[str, Any]]:
        """
        Find the canonical devices row for a given Hubitat label. Used to
        detect mesh-mirror events: an incoming event whose _hub_ip differs
        from the canonical row's hub_ip is firing from a mirror, not the
        native device, and should be dropped.

        Hubitat sometimes propagates labels across hubs with trailing
        whitespace differences (e.g. native='Motion Sensor Living' vs
        mirror='Motion Sensor Living '). Both ingest and lookup TRIM so
        the strings match.

        Returns dict with {id, hub_ip, hubitat_id} or None if no row exists.
        Cached by trimmed label.
        """
        if not label:
            return None
        # Try the trimmed label first (the native version has no suffix);
        # if that misses, strip a trailing ' on Home N' (mirror artifact)
        # and retry against the clean label, which is what got stored.
        key = label.strip()
        if not key:
            return None
        candidates = [key]
        base = _MESH_SUFFIX_RE.sub("", key).strip()
        if base and base != key:
            candidates.append(base)

        for candidate in candidates:
            if candidate in self._label_to_canonical:
                cached = self._label_to_canonical[candidate]
                if cached is not None:
                    return cached
                continue
            try:
                r = requests.get(
                    f"{self.postgrest_url}/devices",
                    params={
                        "select": "id,hub_ip,hubitat_id",
                        "label": f"eq.{candidate}",
                        # Labels are no longer UNIQUE (migration 008): a
                        # genuinely-distinct same-label native on another hub
                        # is kept as is_name_duplicate=true. The CANONICAL row
                        # (mirror-detection anchor) is always the winner, so
                        # restrict to is_name_duplicate=false.
                        "is_name_duplicate": "eq.false",
                    },
                    timeout=3,
                )
                if r.status_code == 200:
                    rows = r.json()
                    if rows:
                        self._label_to_canonical[candidate] = rows[0]
                        return rows[0]
                    self._label_to_canonical[candidate] = None
            except Exception as e:
                self.logger.debug(f"_lookup_canonical_by_label failed: {e}")
                self._label_to_canonical[candidate] = None
        return None

    def invalidate_device_cache(self) -> None:
        """Drop the (hub_ip, hubitat_id) → devices.id cache. Call after a
        classifier rerun that may have added/removed canonical devices."""
        self._device_id_cache.clear()
        self._label_to_canonical.clear()

    def _get_or_create_queue(self, instance_id: int) -> asyncio.Queue:
        """Lazily create the queue + worker task for an instance on first use."""
        queue = self._instance_queues.get(instance_id)
        if queue is None:
            queue = asyncio.Queue()
            self._instance_queues[instance_id] = queue
            # Supervised spawn: a crash inside _instance_worker (e.g. a bug
            # in master() of a specific app type) now surfaces as an ERROR
            # log with the task name + traceback. Without supervision the
            # worker would silently die and that instance would stop
            # processing events.
            self._instance_workers[instance_id] = supervised_spawn(
                self._instance_worker(instance_id, queue),
                name=f"instance_worker_{instance_id}",
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
        Route incoming event to relevant instances.

        Args:
            webhook_payload: Event payload, shaped identically whether it
                came from the (deprecated) Maker API webhook dispatcher or
                from the eventsocket client. Must include ``_hub_ip`` for
                mesh-mirror filtering and (optionally) ``_intake`` and
                ``_received_at_monotonic_ms`` for traceability.

        Returns:
            Number of instances that received the event
        """
        # Parse payload
        device_id = str(webhook_payload.get('deviceId', ''))
        event_name = webhook_payload.get('name', '')
        event_value = webhook_payload.get('value', '')
        display_name = webhook_payload.get('displayName', '')
        hub_ip = str(webhook_payload.get('_hub_ip', ''))
        intake_path = str(webhook_payload.get('_intake', 'eventsocket'))
        # Monotonic-ms timestamp captured at intake (eventsocket client sets
        # this). Used to compute processing_ms before event_log insert.
        recv_ms = webhook_payload.get('_received_at_monotonic_ms')

        if not device_id or not event_name:
            self.logger.warning(f"Invalid payload: {webhook_payload}")
            return 0

        # Resolve to canonical devices.id. Three possible paths, in order:
        #   1. Label match in `devices` table (real Hubitat events with
        #      meaningful displayNames).
        #   2. (mesh-mirror filter — only when both hub_ip + label hit)
        #   3. deviceId itself IS a canonical PK (e2e test injection sends
        #      synthesized displayNames; device_selections store canonical
        #      PKs, so tests author scenarios with deviceId = canonical id).
        # Sync PostgREST call on every event — off the loop so a slow lookup
        # can't hold the dispatch path. Worker thread is fine: per-event
        # latency budget is well above to_thread dispatch overhead.
        canonical_row = await asyncio.to_thread(
            self._lookup_canonical_by_label, display_name
        )

        # Mesh-mirror filter at ingest: silently drop mirrors before any
        # event_log write happens. The eventsocket fans out the same event
        # across every hub that has the device shared — only the origin
        # hub's frame survives this filter.
        if (
            hub_ip
            and canonical_row is not None
            and canonical_row.get("hub_ip") != hub_ip
        ):
            self.logger.debug(
                f"  {_DIM}drop mesh mirror: {display_name!r} from {hub_ip} "
                f"(native is {canonical_row.get('hub_ip')}){_R}"
            )
            return 0

        canonical_id = canonical_row["id"] if canonical_row else None

        # Fallback: if label lookup failed but the payload's deviceId looks
        # like an integer in range, try it as a canonical PK directly.
        # This unblocks: (a) e2e test injection that sends synthetic
        # displayNames, (b) any caller that already passes canonical ids
        # in the deviceId field.
        if canonical_id is None and device_id.isdigit():
            from services.device_to_hubs_classifier import get_device_by_canonical_id
            row = get_device_by_canonical_id(int(device_id))
            if row is not None:
                canonical_id = row["id"]

        # If still no canonical row, the event is for a device that's not
        # in our `devices` table. Log + drop — can't route under Phase 5.
        if canonical_id is None:
            self.logger.debug(
                f"No canonical row for {display_name!r} (hubitat_id={device_id}, "
                f"hub_ip={hub_ip or '?'}); event will not route"
            )

        # Color the value based on active/on vs inactive/off
        val_color = _GREEN if event_value in ('active', 'on', 'open') else _RED
        canonical_tag = f" {_DIM}canon:{canonical_id}{_R}" if canonical_id else ""
        hub_tag = f" {_DIM}hub:{hub_ip}{_R}" if hub_ip else ""
        self.logger.info(
            f"EVENT  {_CYAN}{display_name}{_R} "
            f"[{_DIM}id:{device_id}{_R}{canonical_tag}{hub_tag}]  "
            f"{_YELLOW}{event_name}{_R} = {val_color}{event_value}{_R}"
        )

        # Create event object. event.device_id is the CANONICAL devices.id;
        # the original Hubitat per-hub id is preserved as event.hubitat_id
        # for any handler that needs it (most don't — they should compare
        # against their canonical-id selections).
        event = DeviceEvent(
            device_id=str(canonical_id) if canonical_id is not None else device_id,
            device_name=display_name,
            event_type=event_name,
            value=event_value,
            unit=webhook_payload.get('unit'),
            description=webhook_payload.get('descriptionText'),
            source='hubitat_webhook',
            timestamp=datetime.now(timezone.utc),
            raw_payload=webhook_payload,
        )
        # Stash the per-hub Hubitat id for debugging / legacy lookups.
        # raw_payload also contains it under 'deviceId'.
        event.hubitat_id = device_id

        # Update device cache with new attribute value. Cache is now keyed
        # by canonical devices.id PK — pass canonical_id directly. If the
        # event has no canonical row (unclassified device), skip the cache
        # write.
        if self.device_cache and canonical_id is not None:
            self.device_cache.update_device_attribute(
                canonical_id, event_name, event_value
            )

        # Find subscribed instances by canonical id
        instance_manager = get_instance_manager()
        subscribed_ids = instance_manager.get_subscribed_instances(
            device_id=canonical_id,
            event_type=event_name
        ) if canonical_id is not None else []

        # Enqueue to each instance's worker queue. The webhook handler returns
        # immediately; workers process events in background threads so a slow
        # Hubitat command (verify retries up to 30s) cannot stall the event
        # loop or other instances.
        #
        # `routings` collects every dispatch decision for the M:N event_routings
        # table: each subscribed instance gets either an 'routed' or
        # 'failed_enqueue' entry. Drops at higher level (mesh, orphan, unsub)
        # are recorded separately below.
        routed_to: List[int] = []
        routings: List[Dict[str, Any]] = []
        for instance_id in subscribed_ids:
            try:
                app = instance_manager.get_running_instance(instance_id)
                if app is None:
                    self.logger.warning(
                        f"Instance {instance_id} subscribed but not running"
                    )
                    routings.append({
                        'instance_id': instance_id,
                        'outcome': 'dropped_unsub',
                        'drop_reason': 'instance subscribed but not running',
                    })
                    continue
                queue = self._get_or_create_queue(instance_id)
                await queue.put(event)
                routed_to.append(instance_id)
                routings.append({
                    'instance_id': instance_id,
                    'outcome': 'routed',
                })
            except Exception as e:
                self.logger.error(
                    f"Failed to enqueue event for instance {instance_id}: {e}",
                    exc_info=True
                )
                routings.append({
                    'instance_id': instance_id,
                    'outcome': 'failed_enqueue',
                    'drop_reason': f'{type(e).__name__}: {e}',
                })

        # Record "no canonical row" as orphan-routing so the event_log row
        # has at least one entry explaining why nobody received it.
        if canonical_id is None:
            routings.append({
                'instance_id': None,
                'outcome': 'dropped_orphan',
                'drop_reason': (
                    f'no canonical row for displayName={display_name!r} '
                    f'hubitat_id={device_id} hub_ip={hub_ip or "?"}'
                ),
            })

        # Compute processing latency (ms) from intake to here, then write
        # event_log with all the new columns + the event_routings rows.
        processing_ms: Optional[int] = None
        if recv_ms is not None:
            try:
                import time as _t
                processing_ms = max(0, int(_t.monotonic() * 1000 - float(recv_ms)))
            except Exception:
                processing_ms = None

        # Both writes are sync PostgREST calls — dispatch to a worker thread
        # so the per-event hot path never holds the event loop on a slow
        # database. asyncio.to_thread takes a callable and **kwargs as
        # positional only, so wrap in a lambda to keep the kwargs form.
        event_log_id = await asyncio.to_thread(
            lambda: self._log_event_v2(
                event=event,
                hub_ip=hub_ip,
                canonical_id=canonical_id,
                intake_path=intake_path,
                processing_ms=processing_ms,
                routed_to=routed_to,
                raw_payload=webhook_payload,
            )
        )
        if event_log_id is not None and routings:
            await asyncio.to_thread(self._log_routings, event_log_id, routings)

        # Broadcast to E2E test SSE subscribers (if any are listening).
        # This lets the E2E terminal log show live webhook traffic.
        # Best-effort: failures here must never affect event routing.
        try:
            from services.e2e_events import get_e2e_broadcaster
            # NOTE: asyncio is imported at module level (line 15). A second
            # function-local `import asyncio` here would shadow it and turn
            # every asyncio.* reference earlier in this function into an
            # UnboundLocalError. Removed 2026-05-28.

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
                    # supervised_spawn keeps a strong ref so these
                    # fire-and-forget broadcasts can't be GC-collected
                    # mid-flight, and any failure surfaces as an ERROR
                    # log line with the per-instance task name.
                    for inst_id in routed_to:
                        supervised_spawn(
                            broadcaster.broadcast(inst_id, e2e_event),
                            name=f"e2e-broadcast-inst{inst_id}",
                        )
                except RuntimeError:
                    pass  # No event loop (shouldn't happen in FastAPI)
        except Exception:
            pass  # E2E broadcast failure must never affect routing

        # Broadcast to dashboard WebSocket clients (real-time card updates).
        # Best-effort: failures here must never affect event routing.
        try:
            from services.dashboard_broadcaster import get_dashboard_broadcaster
            # asyncio is module-level (see note above); no local import here.

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
                    # Supervised: dashboard broadcast fire-and-forget.
                    supervised_spawn(
                        dash_broadcaster.broadcast(dash_event),
                        name="dash-broadcast",
                    )
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
                # Universal pause contract (2026-06-16): if the instance is
                # currently paused AND its settings declare
                # resumeOnModeChange=true, auto-resume it BEFORE calling
                # on_mode_change. The mode change is by definition a
                # household transition; the instance opts in to treating
                # it as a "fresh start" signal.
                if getattr(app, 'is_paused', False):
                    try:
                        resume_on_mode = bool(app.get_setting(
                            'resumeOnModeChange', False
                        ))
                    except Exception:
                        resume_on_mode = False
                    if resume_on_mode:
                        self.logger.info(
                            f"Instance {instance_id} paused + "
                            f"resumeOnModeChange=true; auto-resuming "
                            f"before notifying mode change to {new_mode!r}"
                        )
                        try:
                            await asyncio.to_thread(
                                instance_manager.resume_instance, instance_id
                            )
                        except Exception as e:
                            self.logger.warning(
                                f"resumeOnModeChange auto-resume for "
                                f"{instance_id} failed: {e}"
                            )
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

        # Update location_modes table — sync PostgREST writes, off the loop.
        await asyncio.to_thread(self._update_mode, new_mode)

        return notified

    def _log_event_v2(
        self,
        event: DeviceEvent,
        hub_ip: str,
        canonical_id: Optional[int],
        intake_path: str,
        processing_ms: Optional[int],
        routed_to: List[int],
        raw_payload: Dict[str, Any],
    ) -> Optional[int]:
        """
        Log event to ``event_log`` with full provenance.

        Returns the inserted row's id so ``_log_routings`` can FK to it.
        Returns ``None`` on failure — caller must skip the routings write.

        ``routed_to`` is kept in the legacy ``routed_to_instances`` JSONB
        column for backwards-compat with any UI that hasn't been migrated
        yet; the canonical source is the ``event_routings`` join table.
        """
        try:
            r = requests.post(
                f"{self.postgrest_url}/event_log",
                json={
                    'hubitat_device_id': event.device_id,
                    'device_name': event.device_name,
                    'event_type': event.event_type,
                    'event_value': event.value,
                    'event_unit': event.unit,
                    'hub_ip': hub_ip or None,
                    'canonical_device_id': canonical_id,
                    'intake_path': intake_path,
                    'processing_ms': processing_ms,
                    'routed_to_instances': routed_to,
                    'raw_payload': raw_payload,
                    # No `received_at`: postgres `now()` default fires
                    # as a correct UTC instant. Passing naive datetime.now()
                    # here previously stamped every row 4h off because
                    # PostgREST session TZ is UTC and the string had no
                    # timezone marker. Display is via `AT TIME ZONE
                    # <user_tz>` at read time — TZ is a user setting.
                },
                headers={
                    "Content-Type": "application/json",
                    "Prefer": "return=representation",
                },
                timeout=5,
            )
            if r.status_code in (200, 201):
                body = r.json()
                if isinstance(body, list) and body:
                    return body[0].get('id')
                if isinstance(body, dict):
                    return body.get('id')
            self.logger.warning(
                f"event_log insert non-2xx: {r.status_code} {r.text[:200]}"
            )
        except Exception as e:
            self.logger.warning(f"Failed to log event: {e}", exc_info=True)
        return None

    def _log_routings(
        self,
        event_id: int,
        routings: List[Dict[str, Any]],
    ) -> None:
        """
        Bulk-insert ``event_routings`` rows for one event.

        Each routing dict must have at least 'outcome'; 'instance_id' may
        be None for orphan drops, 'drop_reason' is optional. Failures here
        never raise — routing metadata is best-effort.
        """
        try:
            rows = [
                {
                    'event_id': event_id,
                    'instance_id': r.get('instance_id'),
                    'outcome': r['outcome'],
                    'drop_reason': r.get('drop_reason'),
                }
                for r in routings
            ]
            if not rows:
                return
            resp = requests.post(
                f"{self.postgrest_url}/event_routings",
                json=rows,
                headers={
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                timeout=5,
            )
            if resp.status_code not in (200, 201, 204):
                self.logger.debug(
                    f"event_routings insert non-2xx: "
                    f"{resp.status_code} {resp.text[:200]}"
                )
        except Exception as e:
            self.logger.debug(f"Failed to log routings: {e}", exc_info=True)

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

            # Set new mode to active (upsert).
            # `mode_id` is NOT NULL + UNIQUE — derive a stable id from
            # the name so re-emerging modes hit the existing row instead
            # of failing with a not-null violation that silently leaves
            # the table in is_active=false state (seen live 2026-05-18).
            # `updated_at` intentionally omitted — postgres default fires
            # the correct UTC instant; naive datetime.now() would store
            # 4h off because PostgREST session is UTC.
            mode_id = mode_name.lower().replace(' ', '_')[:50]
            # `on_conflict=mode_id` is REQUIRED for the upsert because
            # mode_id is a UNIQUE constraint (not the PRIMARY KEY which
            # is `id`). Without this param PostgREST does ON CONFLICT
            # against the PK only, fails the unique-violation, and
            # returns 409 — leaving is_active=false on whatever the
            # prior PATCH deactivated. Caught live 2026-05-18.
            requests.post(
                f"{self.postgrest_url}/location_modes"
                f"?on_conflict=mode_id",
                json={
                    'mode_id': mode_id,
                    'mode_name': mode_name,
                    'is_active': True,
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
# reload-phase3
# reload-mesh-suffix
