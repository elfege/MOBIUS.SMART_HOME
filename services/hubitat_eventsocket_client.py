"""
Hubitat eventsocket client — sole intake path for device events.

Background
----------
Hubitat exposes a raw WebSocket event stream at ``ws://<hub_ip>/eventsocket``
that emits every device event the hub processes, regardless of any per-device
Maker API "send events" opt-in. The Maker API HTTP-POST webhook path is
fragile: firmware updates and app re-saves silently de-arm event forwarding
for individual devices, producing the failure mode hit on 2026-05-16 with the
Living motion sensors (canons 55/56 on hub Home_1) where the hub kept firing
events visible in its own UI but stopped POSTing them to us.

This client replaces the Maker API webhook intake entirely. The Maker API
HTTP path is still used for two outbound purposes — sending commands and the
periodic ``/devices/all`` reconcile poll — but the smart-home app no longer
*receives* events that way. WS is the sole source of truth.

Reliability strategy (carried from OHVD MQTT client experience)
---------------------------------------------------------------
1. **Data-flow watchdog.** ``ws.open == True`` is not proof events are
   flowing. If no event arrives within ``DATA_WATCHDOG_SECS``, recycle the
   connection regardless of socket state.
2. **Own the retry.** Bounded exponential backoff with ±25% jitter so 4
   parallel hub clients don't reconnect in lockstep after a power blip.
3. **Reset backoff on data, not on connect.** A connection that opens then
   immediately drops should not be treated as healthy.
4. **Mesh filter at ingest.** The hub_mesh fans the same event out across
   every hub that has the device shared. We drop mirrors before they cost
   any DB write or routing work — only events from the device's native hub
   survive. The router applies the same filter as a safety net.
5. **hub_health table.** Every connect / disconnect / event updates a row
   so the reconcile-poll service can decide whether the hub needs an
   aggressive 10s pass.

Modes
-----
``EVENTSOCKET_ENABLED`` env var:
- ``true`` (default) — client runs.
- ``false`` — disabled entirely. Use only as a rollback escape hatch.
"""

import asyncio
import json
import logging
import os
import random
import time
from typing import Dict, List, Optional

import requests
import websockets

from services.supervised_tasks import supervised_spawn

# Maximum concurrent in-flight raw_events POSTs across all hubs. The cap
# prevents storm conditions from spawning unbounded threadpool tasks (which
# would compete with the asyncio loop for CPU and balloon memory). 32 is
# loose enough to absorb realistic bursts (a 4-hub mesh re-handshake) but
# tight enough that a runaway loop drops cleanly to zero added latency.
_RAW_EVENTS_INFLIGHT_CAP = 32

# Minimum spacing between hub_health 'ws_last_event_at' PATCHes per hub.
# The event-loop blocking from a sync requests.patch() at 6–9 Hz under DB
# load was a root cause of the 2026-06-05 Fan Bathroom storm "app
# unresponsive" symptom. 5s is dense enough for the dashboard health badge
# to show fresh data and sparse enough to never compete with the WS drain.
_HUB_HEALTH_MARK_INTERVAL_SECS = 5.0

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

DATA_WATCHDOG_SECS = float(os.environ.get('EVENTSOCKET_WATCHDOG_SECS', '120'))
BACKOFF_BASE_SECS = 1.0
BACKOFF_MAX_SECS = 30.0
BACKOFF_JITTER = 0.25
WS_PING_INTERVAL_SECS = 20
WS_PING_TIMEOUT_SECS = 10
WS_OPEN_TIMEOUT_SECS = 10

POSTGREST_URL = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class HubitatEventsocketClient:
    """Multi-hub eventsocket consumer with watchdog, backoff, hub_health writes."""

    def __init__(self, hubs: List[Dict]) -> None:
        """
        Args:
            hubs: iterable of dicts from ``hub_config`` with keys
                  ``id``, ``hub_name``, ``hub_ip``. Only ``is_enabled=true``
                  rows should be passed in.
        """
        self._hubs = list(hubs)
        self._tasks: Dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()
        self._router = None  # lazy-resolved to avoid import cycle at module load
        # PostgREST client session for hub_health writes — reused across calls
        # to amortize TCP/TLS handshake. Created lazily in _update_health.
        self._http_session: Optional[requests.Session] = None
        # Monotonic timestamp (per hub_id) of the last ws_last_event_at PATCH.
        # Used by _mark_event_throttled to coalesce writes during event storms
        # — see _HUB_HEALTH_MARK_INTERVAL_SECS.
        self._last_mark_event_at: Dict[int, float] = {}
        # In-flight counter for raw_events fire-and-forget POSTs. Capped by
        # _RAW_EVENTS_INFLIGHT_CAP so a storm can't spawn unbounded background
        # tasks; events past the cap are dropped from forensic capture only.
        # The router path is unaffected.
        self._raw_events_inflight: int = 0
        self._raw_events_dropped: int = 0

    async def start(self) -> None:
        if not self._hubs:
            logger.warning('hubitat_eventsocket: no enabled hubs — not starting')
            return
        # Idempotency: cancel any already-running tasks before scheduling new
        # ones. Belt-and-suspenders with start_eventsocket's own guard.
        for name, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        for hub in self._hubs:
            name = hub['hub_name']
            # Supervised: each per-hub eventsocket listener is a long-lived
            # task. A crash (typical cause: a websockets library exception
            # for an unhandled close code) used to silently kill the
            # listener and that hub would stop delivering events. Now
            # logs ERROR with the task name.
            self._tasks[name] = supervised_spawn(
                self._run_hub(hub), name=f'eventsocket:{name}'
            )
        logger.info(f'hubitat_eventsocket: started for hubs={list(self._tasks)}')

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks.values():
            task.cancel()
        for name, task in self._tasks.items():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f'hubitat_eventsocket: task {name} stop error: {e}')
        self._tasks.clear()
        if self._http_session is not None:
            self._http_session.close()
            self._http_session = None
        logger.info('hubitat_eventsocket: stopped')

    async def stop_hub(self, hub_name: str) -> bool:
        """Cancel and drop ONE hub's eventsocket task.

        Called from hub CRUD when a hub is deleted or disabled. Before this,
        NOTHING touched the socket lifecycle on hub CRUD (tasks were built once
        at boot), so a removed/disabled hub's reconnect loop ran forever —
        connecting the stale IP, getting HTTP 200, and PATCHing hub_health
        failures that surfaced as the phantom 'Hub N' banner (audit F3.1).

        Idempotent: returns False if no task was running for ``hub_name``.
        """
        task = self._tasks.pop(hub_name, None)
        # Keep the in-memory hub list consistent so a later full start() does
        # NOT respawn a hub we intentionally stopped.
        self._hubs = [h for h in self._hubs if h.get('hub_name') != hub_name]
        if task is None:
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f'hubitat_eventsocket: stop_hub {hub_name} error: {e}')
        logger.info(f'hubitat_eventsocket: stopped hub task {hub_name}')
        return True

    async def start_hub(self, hub: Dict) -> None:
        """Spawn (or respawn) ONE hub's eventsocket task.

        Called from hub CRUD when a hub is created, enabled, or renamed —
        makes add/enable take effect live, without the 'restart to pick it up'
        anti-pattern. Idempotent: cancels any existing task under the same
        ``hub_name`` first.
        """
        name = hub['hub_name']
        existing = self._tasks.get(name)
        if existing is not None and not existing.done():
            existing.cancel()
        self._hubs = [h for h in self._hubs if h.get('hub_name') != name]
        self._hubs.append(hub)
        self._tasks[name] = supervised_spawn(
            self._run_hub(hub), name=f'eventsocket:{name}'
        )
        logger.info(f'hubitat_eventsocket: started hub task {name}')

    # ------------------------------------------------------------------
    # hub_health writes (best-effort — never raise into the WS loop)
    # ------------------------------------------------------------------

    def _http(self) -> requests.Session:
        if self._http_session is None:
            self._http_session = requests.Session()
        return self._http_session

    def _patch_health(self, hub_id: int, fields: Dict) -> None:
        """PATCH a hub_health row. Swallows errors — health writes must
        never propagate into the WS loop."""
        try:
            fields = {**fields, 'updated_at': 'now()'}
            r = self._http().patch(
                f'{POSTGREST_URL}/hub_health',
                params={'hub_id': f'eq.{hub_id}'},
                json=fields,
                headers={'Prefer': 'return=minimal'},
                timeout=3,
            )
            if r.status_code not in (200, 204):
                logger.debug(
                    f'hub_health PATCH non-2xx: {r.status_code} {r.text[:200]}'
                )
        except Exception as e:
            logger.debug(f'hub_health PATCH error (suppressed): {e}')

    def _mark_connected(self, hub_id: int) -> None:
        now_iso = _now_iso()
        self._patch_health(hub_id, {
            'ws_connected': True,
            'ws_connected_since': now_iso,
            'ws_consecutive_failures': 0,
        })

    def _mark_event(self, hub_id: int) -> None:
        """Stamp ws_last_event_at. Synchronous — callers in the WS coroutine
        should prefer ``_mark_event_throttled`` which both rate-limits AND
        offloads the PATCH to a thread."""
        # Bump events_received_24h counter via raw SQL would need a function;
        # for v1 we just stamp last_event_at and let a scheduled job roll
        # the counter from event_log. Cheap, correct, easy to query.
        self._patch_health(hub_id, {'ws_last_event_at': _now_iso()})

    async def _mark_event_throttled(self, hub_id: int) -> None:
        """
        Async, rate-limited, off-loop variant of ``_mark_event`` for the WS
        drain loop. Two layers of defense against asyncio event-loop
        starvation during event storms:

          1. **Rate limit** — at most one health write per
             ``_HUB_HEALTH_MARK_INTERVAL_SECS`` per hub. A 9 Hz storm
             collapses to ~0.2 Hz of DB writes.

          2. **Off-loop** — the actual PATCH is wrapped in
             ``asyncio.to_thread`` and fired-and-forgotten so the WS drain
             never waits for DB latency. Without this, a slow PostgREST
             window would stall every WS frame for the duration.
        """
        now = _monotonic_now()
        last = self._last_mark_event_at.get(hub_id, 0.0)
        if (now - last) < _HUB_HEALTH_MARK_INTERVAL_SECS:
            return
        self._last_mark_event_at[hub_id] = now
        try:
            asyncio.create_task(asyncio.to_thread(
                self._patch_health, hub_id, {'ws_last_event_at': _now_iso()}
            ))
        except Exception as e:
            # No running loop or threadpool refused — non-fatal, the next
            # tick will retry. Don't ever raise into the WS drain.
            logger.debug(f'_mark_event_throttled spawn failed: {e}')

    def _mark_failure(self, hub_id: int, reason: str) -> None:
        # Increment consecutive_failures atomically via PostgREST rpc would
        # require a function; for now we accept the read-modify-write race
        # since only one task writes per hub.
        try:
            r = self._http().get(
                f'{POSTGREST_URL}/hub_health',
                params={'hub_id': f'eq.{hub_id}', 'select': 'ws_consecutive_failures'},
                timeout=3,
            )
            current = (r.json() or [{}])[0].get('ws_consecutive_failures', 0)
        except Exception:
            current = 0
        self._patch_health(hub_id, {
            'ws_connected': False,
            'ws_last_failure_at': _now_iso(),
            'ws_last_failure_reason': reason[:500],
            'ws_consecutive_failures': current + 1,
        })

    # ------------------------------------------------------------------
    # Per-hub loop
    # ------------------------------------------------------------------

    async def _run_hub(self, hub: Dict) -> None:
        name = hub['hub_name']
        ip = hub['hub_ip']
        hub_id = hub['id']
        url = f'ws://{ip}/eventsocket'

        backoff = BACKOFF_BASE_SECS

        while not self._stop_event.is_set():
            try:
                logger.info(f'[eventsocket {name}] connecting {url}')
                async with websockets.connect(
                    url,
                    open_timeout=WS_OPEN_TIMEOUT_SECS,
                    ping_interval=WS_PING_INTERVAL_SECS,
                    ping_timeout=WS_PING_TIMEOUT_SECS,
                    max_size=2 ** 20,
                ) as ws:
                    logger.info(f'[eventsocket {name}] connected')
                    self._mark_connected(hub_id)
                    got_data = await self._drain(name, ip, hub_id, ws)
                    if got_data:
                        backoff = BACKOFF_BASE_SECS
            except asyncio.CancelledError:
                raise
            except Exception as e:
                reason = f'{type(e).__name__}: {e}'
                logger.warning(f'[eventsocket {name}] connection error: {reason}')
                self._mark_failure(hub_id, reason)

            if self._stop_event.is_set():
                break

            jitter = 1.0 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER)
            delay = min(backoff * jitter, BACKOFF_MAX_SECS)
            logger.info(f'[eventsocket {name}] reconnect in {delay:.2f}s')
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                break  # stop requested during sleep
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, BACKOFF_MAX_SECS)

    async def _drain(self, name: str, ip: str, hub_id: int, ws) -> bool:
        """Read events from ws until close or watchdog. Returns True iff
        at least one event arrived (used to reset backoff)."""
        got_data = False
        router = self._get_router()
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=DATA_WATCHDOG_SECS)
            except asyncio.TimeoutError:
                logger.warning(
                    f'[eventsocket {name}] watchdog: no events in '
                    f'{DATA_WATCHDOG_SECS:.0f}s — recycling connection'
                )
                self._mark_failure(hub_id, 'watchdog_no_events')
                try:
                    await ws.close()
                except Exception:
                    pass
                return got_data

            got_data = True
            try:
                ev = json.loads(raw)
            except Exception:
                logger.debug(f'[eventsocket {name}] non-JSON frame ignored: {raw[:80]!r}')
                continue

            # 2026-05-17 — capture every frame regardless of source/mesh into
            # raw_events for forensic replay. Originally a sync requests.post
            # right here in the WS coroutine; that worked at low rate but
            # during the 2026-06-05 Fan Bathroom storm (9 Hz on this hub),
            # the 2s requests timeout × ~9 calls/s stalled the asyncio loop
            # and the whole app went unresponsive. Now offloaded via
            # asyncio.to_thread with a bounded in-flight cap so a storm can
            # drop forensic-capture frames cleanly instead of cascading.
            self._fire_raw_event_capture(ip, ev)

            # Hubitat eventsocket emits multiple 'source' types. We consume:
            #   DEVICE   — device-state events (motion, switch, etc.)
            #   LOCATION — hub-level events including mode changes
            # Everything else (APP_STATUS, HUB_INFO, etc) is ignored.
            src = ev.get('source')
            if src not in ('DEVICE', 'LOCATION'):
                continue

            # Rate-limited + off-loop: at most one PATCH per
            # _HUB_HEALTH_MARK_INTERVAL_SECS per hub, and the PATCH itself
            # runs on a thread so DB latency never blocks the WS drain.
            await self._mark_event_throttled(hub_id)

            # LOCATION → mode change goes through a dedicated router method.
            # Same shape (name='mode', value='Night') from the eventsocket
            # as the legacy Maker API webhook used to deliver. Re-using
            # route_mode_change keeps AML's on_mode_change → master()
            # path working — the failure mode user diagnosed 2026-05-17:
            # mode-driven overrides were silently broken since the webhook
            # intake was deprecated for DEVICE events.
            #
            # LOCATION events with other names (sunset, sunrise, etc.) are
            # skipped entirely — we don't currently consume them.
            if src == 'LOCATION':
                if ev.get('name') == 'mode':
                    try:
                        await self._get_router().route_mode_change({
                            'value': ev.get('value', ''),
                            'displayName': ev.get('displayName', 'Mode Changed'),
                            '_hub_ip': ip,
                            '_intake': 'eventsocket',
                        })
                    except Exception as e:
                        logger.error(
                            f'[eventsocket {name}] route_mode_change: '
                            f'{type(e).__name__}: {e}',
                            exc_info=True,
                        )
                continue  # all LOCATION events skip the DEVICE route_event path

            # Build the canonical webhook-payload dict that WebhookRouter
            # already knows how to consume. _hub_ip is what enables the
            # mesh-mirror filter inside route_event.
            payload = {
                'deviceId': str(ev.get('deviceId', '')),
                'name': ev.get('name', ''),
                'value': ev.get('value', ''),
                'displayName': ev.get('displayName', '') or '',
                'descriptionText': ev.get('descriptionText'),
                'unit': ev.get('unit'),
                'type': ev.get('type'),
                'data': ev.get('data'),
                '_hub_ip': ip,
                '_intake': 'eventsocket',
                # ms-precision intake timestamp so the router can compute
                # processing latency before writing event_log.
                '_received_at_monotonic_ms': time.monotonic() * 1000,
            }

            try:
                await router.route_event(payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f'[eventsocket {name}] router error: {type(e).__name__}: {e}',
                    exc_info=True,
                )

    def _get_router(self):
        if self._router is None:
            from services.webhook_router import get_webhook_router
            self._router = get_webhook_router()
        return self._router

    # ------------------------------------------------------------------
    # raw_events forensic capture — off-loop fire-and-forget
    # ------------------------------------------------------------------

    def _fire_raw_event_capture(self, ip: str, ev: Dict) -> None:
        """
        Schedule the raw_events POST on a thread and return immediately.

        Bounded by ``_RAW_EVENTS_INFLIGHT_CAP`` — when an event arrives and
        the cap is already reached, we DROP the capture for that frame and
        bump ``_raw_events_dropped``. The router path is unaffected; we
        just lose a forensic-log row, which is the correct trade during a
        storm (don't drag down the live system to preserve audit trail).
        """
        if self._raw_events_inflight >= _RAW_EVENTS_INFLIGHT_CAP:
            self._raw_events_dropped += 1
            # Log occasionally so a long storm is visible. Avoid one-log-
            # per-drop to prevent the storm from also storming the logger.
            if self._raw_events_dropped % 100 == 1:
                logger.warning(
                    f'raw_events capture saturated '
                    f'(in-flight={self._raw_events_inflight}, '
                    f'dropped={self._raw_events_dropped})'
                )
            return
        self._raw_events_inflight += 1
        try:
            task = asyncio.create_task(asyncio.to_thread(
                self._post_raw_event_sync, ip, ev
            ))
            task.add_done_callback(self._on_raw_event_done)
        except Exception as e:
            # No running loop, threadpool refused, etc. — release the
            # in-flight slot and continue. Best-effort capture only.
            self._raw_events_inflight -= 1
            logger.debug(f'raw_events spawn failed: {e}')

    def _on_raw_event_done(self, task: 'asyncio.Task') -> None:
        """Decrement the in-flight counter when a capture POST completes."""
        try:
            # Surface the exception (if any) so it's not silently swallowed
            # by add_done_callback — but at debug level, this is forensic
            # capture not user-facing data.
            exc = task.exception()
            if exc is not None:
                logger.debug(f'raw_events capture exc: {exc}')
        except Exception:
            pass
        finally:
            if self._raw_events_inflight > 0:
                self._raw_events_inflight -= 1

    def _post_raw_event_sync(self, ip: str, ev: Dict) -> None:
        """
        Synchronous PostgREST POST — designed to be run via asyncio.to_thread
        from the WS coroutine. NEVER call directly from the asyncio loop:
        a slow PostgREST window would block the loop for the full timeout.
        """
        try:
            self._http().post(
                f'{POSTGREST_URL}/raw_events',
                json={
                    'hub_ip': ip,
                    'source': ev.get('source', 'unknown')[:30],
                    'frame': ev,
                },
                headers={'Prefer': 'return=minimal'},
                timeout=2,
            )
        except Exception:
            # Best-effort capture — never raise. Errors are visible at
            # debug via _on_raw_event_done's task.exception() probe.
            pass


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC ISO-8601 timestamp PostgREST accepts as TIMESTAMPTZ."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _monotonic_now() -> float:
    """Monotonic seconds for rate-limit interval math. Module-level so
    tests can patch it without touching the class."""
    return time.monotonic()


def _load_hub_list() -> List[Dict]:
    """Fetch enabled hubs from hub_config via PostgREST."""
    try:
        r = requests.get(
            f'{POSTGREST_URL}/hub_config',
            params={'is_enabled': 'eq.true', 'select': 'id,hub_name,hub_ip'},
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f'hubitat_eventsocket: failed to load hub_config: {e}')
        return []


# ---------------------------------------------------------------------------
# Singleton + lifespan integration
# ---------------------------------------------------------------------------


_client: Optional[HubitatEventsocketClient] = None


async def start_eventsocket() -> None:
    """Lifespan startup hook — call from FastAPI lifespan().

    Idempotent: if a prior `_client` is still around (lifespan re-entry,
    uvicorn reload, test fixture), tear it down before creating a new one.
    Without this guard each call adds another WS task per hub; with N starts
    we end up with N parallel sockets each delivering every event, which
    duplicates event_log writes N-fold (caught live 2026-05-19: 6
    connections to hub .70, 3 to .69 → every event written 2-6x)."""
    global _client
    enabled = os.environ.get('EVENTSOCKET_ENABLED', 'true').strip().lower() == 'true'
    if not enabled:
        logger.info('hubitat_eventsocket: EVENTSOCKET_ENABLED=false — not starting')
        return

    if _client is not None:
        logger.warning(
            'hubitat_eventsocket: start called while existing client active '
            '— stopping it first to prevent duplicate WS connections'
        )
        try:
            await _client.stop()
        except Exception as e:
            logger.warning(f'hubitat_eventsocket: prior client stop failed: {e}')
        _client = None

    hubs = _load_hub_list()
    if not hubs:
        logger.warning('hubitat_eventsocket: no enabled hubs in hub_config')
        return

    _client = HubitatEventsocketClient(hubs=hubs)
    await _client.start()


async def stop_eventsocket() -> None:
    """Lifespan shutdown hook."""
    global _client
    if _client is not None:
        await _client.stop()
        _client = None


def get_client() -> Optional[HubitatEventsocketClient]:
    """Accessor for monitoring/admin endpoints."""
    return _client


async def stop_hub_socket(hub_name: str) -> bool:
    """Stop one hub's eventsocket task on the running client (hub delete/disable/
    rename-teardown). No-op (returns False) if the eventsocket client isn't up."""
    c = get_client()
    if c is None:
        return False
    return await c.stop_hub(hub_name)


async def start_hub_socket(hub: Dict) -> None:
    """Start/respawn one hub's eventsocket task on the running client (hub
    create/enable/rename). No-op if the eventsocket client isn't running."""
    c = get_client()
    if c is not None:
        await c.start_hub(hub)
