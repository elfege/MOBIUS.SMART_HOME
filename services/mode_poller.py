"""
Hub-derived location-mode polling.

Why this exists
---------------
Mobius assumed mode changes would flow through the eventsocket WS (source
"LOCATION"). Diagnosis 2026-05-18: zero LOCATION frames have ever been
captured to raw_events on Elfege's hubs. The eventsocket on this firmware
delivers DEVICE-source events only; mode changes never arrive that way.

Without a push notification path, the mode subsystem was effectively dead:
  - `location_modes` table never populated  →  empty
  - AML's `_get_current_mode()` called Maker API which is now disabled
    (admin-API-primary policy) → returned None
  - Per-mode timeouts (`modeTimeouts.Night`), `exclusionModes`,
    `keepOffModes` all silently bypassed because no mode ever matched

This module replaces the push assumption with a pull poller:
  - Reads `/location/list/data` from the *authoritative* hub (user
    designation; defaults to whichever hub_config row has
    `is_authoritative_for_mode = TRUE`)
  - Extracts `currentMode` (admin API exposes just the active one;
    there's no observed admin endpoint that lists all available modes
    as of 2026-05-18 — see probe results in commit message)
  - Diff-checks against last known mode in `location_modes`; on change:
      - UPDATE the prior active row to is_active=FALSE
      - UPSERT the new row with is_active=TRUE
      - APPEND to mode_change_log for forensic timeline
      - Fire-and-forget call to webhook_router.route_mode_change(...)
        so running AML instances get an immediate `on_mode_change()`
        callback (same code path the eventsocket WS would have used)

Cadence: 60s default. Modes change rarely; this gives ~1 min worst-case
latency between hub-side change and Mobius-side awareness. For
forensic-grade tracking the cadence can be tightened via
system_settings.mode_poll_interval_seconds.

The list of available modes is built up lazily — each new mode_name we
see for the first time gets a new row in location_modes. After a few
days of typical activity, every mode the user actually cycles through
will be represented.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def _postgrest_url() -> str:
    return os.environ.get('POSTGREST_URL', 'http://postgrest:3001')


def _authoritative_hub() -> Optional[Tuple[str, str]]:
    """Return (hub_name, hub_ip) of the primary hub. Mode polling targets
    the hub_config row flagged `is_primary = TRUE` — that flag already
    designates the authoritative hub for location-level concerns
    (location modes, sunrise/sunset, etc.), no second flag needed.

    Returns None if no primary hub is configured/enabled; caller logs
    and skips the poll pass.
    """
    pg = _postgrest_url()
    try:
        r = requests.get(
            f'{pg}/hub_config',
            params={
                'is_primary': 'eq.true',
                'is_enabled': 'eq.true',
                'select': 'hub_name,hub_ip',
                'limit': '1',
            },
            timeout=3,
        )
        if r.status_code == 200 and r.json():
            row = r.json()[0]
            return (row['hub_name'], row['hub_ip'])
    except Exception as e:
        logger.debug(f"mode_poller: hub_config lookup failed: {e}")
    return None


def _query_current_mode(hub_ip: str, hub_name: str) -> Optional[str]:
    """Returns the hub's `currentMode` string, or None on failure."""
    try:
        from services.hubitat_admin_client import get_client
        client = get_client(hub_ip, hub_name)
        r = client._request('GET', '/location/list/data')
        if r.status_code != 200:
            return None
        rows = r.json()
        if isinstance(rows, list) and rows:
            return rows[0].get('currentMode')
    except Exception as e:
        logger.debug(
            f"mode_poller: /location/list/data on {hub_name}: {e}"
        )
    return None


def _current_active_mode_in_db() -> Optional[str]:
    """Returns mode_name of the currently-active row in location_modes, or
    None if no row is active."""
    pg = _postgrest_url()
    try:
        r = requests.get(
            f'{pg}/location_modes',
            params={
                'is_active': 'eq.true',
                'select': 'mode_name',
                'limit': '1',
            },
            timeout=3,
        )
        if r.status_code == 200 and r.json():
            return r.json()[0].get('mode_name')
    except Exception as e:
        logger.debug(f"mode_poller: active-mode lookup failed: {e}")
    return None


def _apply_mode_change(new_mode: str, previous_mode: Optional[str]) -> None:
    """Persist the mode transition: flip is_active on prior row, upsert
    the new row, append a mode_change_log entry. All best-effort —
    failures are logged but never raised."""
    pg = _postgrest_url()

    # 1. Deactivate any currently-active rows.
    try:
        requests.patch(
            f'{pg}/location_modes',
            params={'is_active': 'eq.true'},
            json={'is_active': False},
            headers={'Content-Type': 'application/json'},
            timeout=3,
        )
    except Exception as e:
        logger.warning(f"mode_poller: deactivate failed: {e}")

    # 2. Upsert the new active row. Use a deterministic mode_id derived
    #    from the name so re-emerging modes hit the existing row.
    #    `on_conflict=mode_id` is REQUIRED: mode_id is UNIQUE but not
    #    the PRIMARY KEY, so without this param PostgREST uses the PK
    #    for its ON CONFLICT clause and the unique-violation surfaces
    #    as 409 — silently leaving is_active=false.
    mode_id = new_mode.lower().replace(' ', '_')[:50]
    try:
        requests.post(
            f'{pg}/location_modes?on_conflict=mode_id',
            json={
                'mode_id': mode_id,
                'mode_name': new_mode,
                'is_active': True,
            },
            headers={
                'Content-Type': 'application/json',
                'Prefer': 'resolution=merge-duplicates',
            },
            timeout=3,
        )
    except Exception as e:
        logger.warning(f"mode_poller: upsert failed: {e}")

    # 3. Append to mode_change_log. Schema (from migration 004):
    #    id, mode_name, became_active_at (default now()), became_inactive_at, source
    try:
        requests.post(
            f'{pg}/mode_change_log',
            json={
                'mode_name': new_mode,
                'source': 'mode_poller',
            },
            headers={'Content-Type': 'application/json'},
            timeout=3,
        )
    except Exception as e:
        logger.warning(f"mode_poller: change_log append failed: {e}")


def _notify_running_instances(new_mode: str) -> None:
    """Trigger on_mode_change() on every running instance via webhook_router.

    Re-uses the same dispatcher the eventsocket would have used had the
    hub sent a LOCATION/mode frame. Runs in an asyncio task; we are in
    the APScheduler thread context so we have to bridge.
    """
    try:
        import asyncio
        from services.webhook_router import get_webhook_router
        router = get_webhook_router()
        # Build a minimal payload matching the LOCATION/mode shape
        payload = {'name': 'mode', 'value': new_mode, 'source': 'LOCATION'}
        # Try the running loop; fall back to creating a one-shot loop.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    router.route_mode_change(payload), loop
                )
                return
        except RuntimeError:
            pass
        asyncio.run(router.route_mode_change(payload))
    except Exception as e:
        logger.debug(f"mode_poller: instance notify failed: {e}")


def run_poll_pass() -> dict:
    """One iteration of the poll. Returns a small report dict for logging.

    No side effects when the mode hasn't changed; the only state mutation
    is the very first pass after fresh boot (when location_modes is empty)
    or after a hub-side mode change.
    """
    hub = _authoritative_hub()
    if hub is None:
        return {'status': 'no_authoritative_hub'}
    hub_name, hub_ip = hub

    current_mode = _query_current_mode(hub_ip, hub_name)
    if current_mode is None:
        return {'status': 'hub_unreachable', 'hub': hub_name}

    previous = _current_active_mode_in_db()
    if previous == current_mode:
        return {'status': 'unchanged', 'mode': current_mode}

    logger.info(
        f"mode_poller: mode change detected on {hub_name}: "
        f"{previous!r} → {current_mode!r}"
    )
    _apply_mode_change(current_mode, previous)
    _notify_running_instances(current_mode)
    return {
        'status': 'changed',
        'previous': previous,
        'current': current_mode,
        'hub': hub_name,
    }


def schedule_poll_job(scheduler, interval_seconds: int = 60) -> str:
    """Register the recurring poll with APScheduler.

    Returns the job id. Default cadence 60s; tune via
    system_settings.mode_poll_interval_seconds at a later layer.
    """
    job_id = 'mode_poller_pull'
    scheduler.add_job(
        func=run_poll_pass,
        trigger='interval',
        seconds=interval_seconds,
        id=job_id,
        replace_existing=True,
    )
    logger.info(f"mode_poller: scheduled poll every {interval_seconds}s")
    return job_id
