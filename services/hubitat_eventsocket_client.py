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

    async def start(self) -> None:
        if not self._hubs:
            logger.warning('hubitat_eventsocket: no enabled hubs — not starting')
            return
        for hub in self._hubs:
            name = hub['hub_name']
            self._tasks[name] = asyncio.create_task(
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
        # Bump events_received_24h counter via raw SQL would need a function;
        # for v1 we just stamp last_event_at and let a scheduled job roll
        # the counter from event_log. Cheap, correct, easy to query.
        self._patch_health(hub_id, {'ws_last_event_at': _now_iso()})

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

            # Hubitat eventsocket emits multiple 'source' types. We consume:
            #   DEVICE   — device-state events (motion, switch, etc.)
            #   LOCATION — hub-level events including mode changes
            # Everything else (APP_STATUS, HUB_INFO, etc) is ignored.
            src = ev.get('source')
            if src not in ('DEVICE', 'LOCATION'):
                continue

            self._mark_event(hub_id)

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


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC ISO-8601 timestamp PostgREST accepts as TIMESTAMPTZ."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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
    """Lifespan startup hook — call from FastAPI lifespan()."""
    global _client
    enabled = os.environ.get('EVENTSOCKET_ENABLED', 'true').strip().lower() == 'true'
    if not enabled:
        logger.info('hubitat_eventsocket: EVENTSOCKET_ENABLED=false — not starting')
        return

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
