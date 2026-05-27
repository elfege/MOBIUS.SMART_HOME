"""
Reconcile-poll service — the safety net for WS-only intake.

What it does
------------
Every ``RECONCILE_INTERVAL_SECS`` (default 60) — or every
``RECONCILE_AGGRESSIVE_SECS`` (default 10) when any hub has had a WS failure
in the last ``RECONCILE_AGGRESSIVE_WINDOW_SECS`` (default 300) — this service:

1. Pulls ``/devices/all`` from each enabled hub via Maker API HTTP.
2. For every device on each hub, looks up the canonical row by
   ``(hub_ip, hubitat_id)``. Skips mesh-mirrors (canonical.hub_ip != polled hub).
3. For every event type the device is subscribed to in ``device_subscriptions``,
   compares the hub-reported current value against ``device_cache.attributes``.
4. On divergence, synthesizes a payload identical in shape to an eventsocket
   frame and pushes it through ``WebhookRouter.route_event`` with
   ``_intake='reconcile'``. The router does its usual work — event_log write,
   event_routings, instance dispatch — and the divergence is healed.
5. Updates ``hub_health.last_reconcile_at`` + ``last_reconcile_diffs``.

Why this is necessary
---------------------
The WS eventsocket is the sole intake. While connected, TCP guarantees in-order
delivery; while disconnected (reconnect gap, hub reboot, firmware bump), events
fire on the hub but never reach us. Without reconcile, a missed switch=off
during a 30s reconnect window means the cached state stays "on" forever and
the next motion-active won't trigger anything (since the app already thinks
the light is on). Reconcile closes that gap by polling absolute state every
minute and replaying any event we missed.

Cost
----
GET /devices/all is one HTTP call per hub per pass. Each hub returns ~50-200
devices. We then make zero further calls — comparison is in-process against
the cache. So this is ~4 HTTP calls per 60s, negligible.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (env-overridable)
# ---------------------------------------------------------------------------

RECONCILE_INTERVAL_SECS = float(os.environ.get('RECONCILE_INTERVAL_SECS', '60'))
RECONCILE_AGGRESSIVE_SECS = float(os.environ.get('RECONCILE_AGGRESSIVE_SECS', '10'))
RECONCILE_AGGRESSIVE_WINDOW_SECS = float(
    os.environ.get('RECONCILE_AGGRESSIVE_WINDOW_SECS', '300')
)
RECONCILE_HUB_TIMEOUT_SECS = float(os.environ.get('RECONCILE_HUB_TIMEOUT_SECS', '15'))

POSTGREST_URL = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

# Only reconcile these attribute kinds — the ones that drive automations.
# We intentionally skip the firehose (power, energy, amperage, etc.) since
# divergence there isn't actionable and would flood event_log.
RECONCILE_ATTRIBUTES: Set[str] = {
    'motion', 'switch', 'contact', 'presence', 'illuminance',
    'humidity', 'temperature', 'lock', 'water',
}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ReconcilePoll:
    """Single background asyncio task — runs forever until stop()."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._router = None
        self._device_cache = None
        # Per-(canonical_id, attr) memory of the last hub-reported value
        # we observed. Used to suppress re-synthesizing the SAME state
        # every cycle when device_cache hasn't caught up yet — divergence
        # alone isn't enough; we want hub-value-CHANGE since last poll.
        # Otherwise: 16:21:33 hub=active → synthesize 'active', cache
        # gets the update, but if the cache write fails silently or has
        # latency, next poll still sees cache=None hub=active and
        # synthesizes again. Caught 2026-05-19: 11 synthesized 'active'
        # events for canon 244 in a 12-minute window where Hubitat only
        # reported one real active.
        self._last_seen: Dict[tuple, str] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name='reconcile_poll')
        logger.info(
            f'reconcile_poll: started normal={RECONCILE_INTERVAL_SECS}s '
            f'aggressive={RECONCILE_AGGRESSIVE_SECS}s '
            f'after-failure-window={RECONCILE_AGGRESSIVE_WINDOW_SECS}s'
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info('reconcile_poll: stopped')

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        # Wait a bit on startup so the eventsocket client has time to
        # connect and the device cache to populate — otherwise the first
        # pass divergence-storms because cache is empty.
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=30)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop_event.is_set():
            interval = self._pick_interval()
            try:
                await self._one_pass()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f'reconcile_poll pass failed: {e}', exc_info=True)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break  # stop requested
            except asyncio.TimeoutError:
                pass

    def _pick_interval(self) -> float:
        """Aggressive cadence if any hub has had a recent failure."""
        try:
            r = requests.get(
                f'{POSTGREST_URL}/hub_health',
                params={'select': 'ws_last_failure_at'},
                timeout=3,
            )
            r.raise_for_status()
            rows = r.json()
        except Exception:
            return RECONCILE_INTERVAL_SECS

        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=RECONCILE_AGGRESSIVE_WINDOW_SECS
        )
        for row in rows:
            ts = row.get('ws_last_failure_at')
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                if dt > cutoff:
                    return RECONCILE_AGGRESSIVE_SECS
            except Exception:
                continue
        return RECONCILE_INTERVAL_SECS

    # ------------------------------------------------------------------
    # One reconcile pass
    # ------------------------------------------------------------------

    async def _one_pass(self) -> None:
        hubs = self._load_hubs()
        if not hubs:
            return

        # Build (canonical_id → set of subscribed event_types). One query,
        # used across all hubs.
        sub_map = self._load_subscriptions()
        if not sub_map:
            return

        # Iterate hubs concurrently — each pulls its own /devices/all and
        # synthesizes divergence events for its devices. _process_hub never
        # raises into here.
        results = await asyncio.gather(
            *(self._process_hub(h, sub_map) for h in hubs),
            return_exceptions=True,
        )
        total_diffs = sum(r for r in results if isinstance(r, int))
        logger.info(f'reconcile_poll: pass complete diffs={total_diffs}')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_hubs(self) -> List[Dict]:
        try:
            r = requests.get(
                f'{POSTGREST_URL}/hub_config',
                params={
                    'is_enabled': 'eq.true',
                    'select': 'id,hub_name,hub_ip,maker_api_app_number,maker_api_token_env',
                },
                timeout=5,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning(f'reconcile_poll: load hubs failed: {e}')
            return []

    def _load_subscriptions(self) -> Dict[int, Set[str]]:
        """canonical_device_id → set of event_types we care about."""
        try:
            r = requests.get(
                f'{POSTGREST_URL}/device_subscriptions',
                params={'select': 'device_id,event_type'},
                timeout=5,
            )
            r.raise_for_status()
            rows = r.json()
        except Exception as e:
            logger.warning(f'reconcile_poll: load subscriptions failed: {e}')
            return {}
        out: Dict[int, Set[str]] = {}
        for row in rows:
            d = row.get('device_id')
            t = row.get('event_type')
            if d is None or not t:
                continue
            out.setdefault(int(d), set()).add(t)
        return out

    def _load_canonical_by_hub_native(
        self, hub_ip: str
    ) -> Dict[str, Dict]:
        """{ hubitat_id (str) → canonical row dict } for one hub."""
        try:
            r = requests.get(
                f'{POSTGREST_URL}/devices',
                params={
                    'hub_ip': f'eq.{hub_ip}',
                    'select': 'id,hub_ip,hubitat_id,label',
                },
                timeout=5,
            )
            r.raise_for_status()
            return {str(row['hubitat_id']): row for row in r.json()}
        except Exception as e:
            logger.warning(
                f'reconcile_poll: load devices for hub {hub_ip} failed: {e}'
            )
            return {}

    async def _process_hub(
        self,
        hub: Dict,
        sub_map: Dict[int, Set[str]],
    ) -> int:
        """Pull /devices/all from one hub, emit synthesized events for
        divergences. Returns count of synthesized events. Never raises."""
        hub_id = hub['id']
        hub_ip = hub['hub_ip']
        app_num = hub['maker_api_app_number']
        token = os.environ.get(hub['maker_api_token_env'], '')
        # Token only required for Maker API path. If Maker is disabled,
        # admin API doesn't need a token (cookie auth handles that case
        # via HubitatAdminClient credentials lookup).
        try:
            from services.settings_resolver import get_resolver
            maker_required = get_resolver().get_system('maker_api_enabled', True)
        except Exception:
            maker_required = True
        if maker_required and not token:
            logger.warning(
                f'reconcile_poll [{hub["hub_name"]}]: '
                f'token env {hub["maker_api_token_env"]} not set'
            )
            return 0

        try:
            devices = await asyncio.to_thread(
                self._http_get_devices_all,
                hub_ip, app_num, token, hub['hub_name'],
            )
        except Exception as e:
            logger.warning(
                f'reconcile_poll [{hub["hub_name"]}]: /devices/all failed: {e}'
            )
            return 0

        canonical_by_native = self._load_canonical_by_hub_native(hub_ip)
        # Inverse lookup: native id list for THIS hub limited to canonical
        # ids that anyone is actually subscribed to. Used by the admin API
        # path to skip the metadata-only bulk endpoint and pull state only
        # for devices we care about.
        subscribed_canonical = set(sub_map.keys())
        self._subscribed_native_ids_for_hub = [
            native for native, row in canonical_by_native.items()
            if int(row['id']) in subscribed_canonical
        ]
        cache = self._get_cache()
        router = self._get_router()

        diffs = 0
        for d in devices:
            native_id = str(d.get('id', ''))
            canonical = canonical_by_native.get(native_id)
            if canonical is None:
                continue  # device not in our canonical table
            canonical_id = int(canonical['id'])
            # Mesh-mirror guard: skip if our canonical row's hub_ip differs
            # from the hub we're currently polling.
            if canonical.get('hub_ip') != hub_ip:
                continue
            subscribed = sub_map.get(canonical_id, set())
            if not subscribed:
                continue
            # Compare each subscribed attribute.
            hub_attrs = {a['name']: a.get('currentValue')
                         for a in d.get('attributes', [])}
            cache_state = cache.get_device(canonical_id) if cache else None
            cache_attrs = (
                cache_state.get('attributes', {}) or {}
                if isinstance(cache_state, dict) else {}
            )
            for attr in subscribed & RECONCILE_ATTRIBUTES:
                hub_val = hub_attrs.get(attr)
                if hub_val is None:
                    continue
                cached_val = cache_attrs.get(attr)
                if str(cached_val) == str(hub_val):
                    # Cache agrees with hub — nothing to do.
                    self._last_seen[(canonical_id, attr)] = str(hub_val)
                    continue
                # Cache disagrees with hub. Only synthesize on a NEW
                # hub state — i.e., the hub value changed since our
                # last observation. If we've seen this exact hub value
                # before on this attr, the divergence is just cache
                # lag and we shouldn't replay the same "event" every
                # poll. The cache will reconverge naturally as real
                # eventsocket frames arrive.
                key = (canonical_id, attr)
                if self._last_seen.get(key) == str(hub_val):
                    continue
                self._last_seen[key] = str(hub_val)
                # Divergence — synthesize event.
                payload = {
                    'deviceId': native_id,
                    'name': attr,
                    'value': str(hub_val),
                    'displayName': canonical.get('label', d.get('label', '')),
                    'descriptionText': (
                        f'reconcile divergence: hub={hub_val} '
                        f'cache={cached_val}'
                    ),
                    'unit': None,
                    'type': None,
                    'data': None,
                    '_hub_ip': hub_ip,
                    '_intake': 'reconcile',
                    '_received_at_monotonic_ms': time.monotonic() * 1000,
                }
                try:
                    await router.route_event(payload)
                    diffs += 1
                    logger.info(
                        f'reconcile [{hub["hub_name"]}] canon={canonical_id} '
                        f'{attr}: cache={cached_val} hub={hub_val} → synthesized'
                    )
                except Exception as e:
                    logger.warning(
                        f'reconcile route_event failed canon={canonical_id} '
                        f'attr={attr}: {e}'
                    )

        self._update_hub_health(hub_id, diffs)
        return diffs

    def _http_get_devices_all(
        self, hub_ip: str, app_num: str, token: str, hub_name: str = '',
    ) -> List[Dict]:
        """
        Pull devices+state for one hub. Picks Maker API or admin API based
        on the system_setting `maker_api_enabled`.

        Both paths return enough info for divergence detection
        (id, attributes / currentStates). We normalize to the shape the
        rest of _process_hub expects: each device dict has
        ``{'id': str, 'label': str, 'attributes': [{'name', 'currentValue'}]}``.
        """
        # Decide backend
        use_admin = False
        try:
            from services.settings_resolver import get_resolver
            maker_on = get_resolver().get_system('maker_api_enabled', True)
            use_admin = (maker_on is False)
        except Exception:
            pass  # fall through to Maker API on resolver error

        if use_admin:
            try:
                from services.hubitat_admin_client import (
                    get_client, to_maker_shape,
                )
                client = get_client(hub_ip, hub_name or hub_ip)
                # Admin API's /device/list/data is metadata-only (no state).
                # Pull state per-subscribed-device via /device/fullJson/<id>.
                # `subscribed_native_ids` is set on the instance before each
                # _process_hub call — see _process_hub() below.
                native_ids = getattr(self, '_subscribed_native_ids_for_hub', [])
                if not native_ids:
                    # Nothing subscribed on this hub — nothing to reconcile.
                    return []
                devices = client.get_devices_with_state(
                    [int(i) for i in native_ids if str(i).isdigit()]
                )
                # /device/fullJson nests state under device.currentStates
                # as a *dict*. to_maker_shape() handles that conversion;
                # the prior inline reader treated currentStates as a list
                # at top level, which produced empty attributes for
                # every device — same bug as in device_commander.
                normalized = []
                for d in devices:
                    shaped = to_maker_shape(d)
                    if shaped:
                        normalized.append(shaped)
                return normalized
            except Exception as e:
                # No silent fallback to Maker when Maker is explicitly
                # disabled. User wants to test the architecture without
                # Maker; failures need to surface, not hide.
                logger.error(
                    f'reconcile_poll [{hub_name or hub_ip}]: admin API '
                    f'failed: {e}. NOT falling back to Maker because '
                    f'maker_api_enabled=false. Re-enable Maker on /hubs '
                    f'page if this becomes blocking.'
                )
                return []

        # Maker API path
        r = requests.get(
            f'http://{hub_ip}/apps/api/{app_num}/devices/all',
            params={'access_token': token},
            timeout=RECONCILE_HUB_TIMEOUT_SECS,
        )
        r.raise_for_status()
        return r.json()

    def _update_hub_health(self, hub_id: int, diffs: int) -> None:
        try:
            from datetime import datetime, timezone
            requests.patch(
                f'{POSTGREST_URL}/hub_health',
                params={'hub_id': f'eq.{hub_id}'},
                json={
                    'last_reconcile_at': datetime.now(timezone.utc).isoformat(),
                    'last_reconcile_diffs': diffs,
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                },
                headers={'Prefer': 'return=minimal'},
                timeout=3,
            )
        except Exception as e:
            logger.debug(f'reconcile hub_health update failed: {e}')

    def _get_router(self):
        if self._router is None:
            from services.webhook_router import get_webhook_router
            self._router = get_webhook_router()
        return self._router

    def _get_cache(self):
        if self._device_cache is None:
            try:
                from services.device_cache import get_default_cache
                self._device_cache = get_default_cache()
            except Exception as e:
                logger.warning(f'reconcile: cache unavailable: {e}')
                self._device_cache = False  # mark as tried
        return self._device_cache if self._device_cache else None


# ---------------------------------------------------------------------------
# Singleton + lifespan integration
# ---------------------------------------------------------------------------


_service: Optional[ReconcilePoll] = None


async def start_reconcile_poll() -> None:
    """Lifespan startup hook. Idempotent — if a prior _service is still
    running (lifespan re-entry, uvicorn reload), stop it before
    starting a new one. Same singleton-leak class of bug as the
    eventsocket multi-start, caught 2026-05-19."""
    global _service
    if os.environ.get('RECONCILE_POLL_ENABLED', 'true').strip().lower() != 'true':
        logger.info('reconcile_poll: RECONCILE_POLL_ENABLED=false — not starting')
        return
    if _service is not None:
        logger.warning(
            'reconcile_poll: start called while a prior service is active — '
            'stopping it first to prevent duplicate polling loops'
        )
        try:
            await _service.stop()
        except Exception as e:
            logger.warning(f'reconcile_poll: prior stop failed: {e}')
        _service = None
    _service = ReconcilePoll()
    await _service.start()


async def stop_reconcile_poll() -> None:
    """Lifespan shutdown hook."""
    global _service
    if _service is not None:
        await _service.stop()
        _service = None
