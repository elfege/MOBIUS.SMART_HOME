"""
Motion activity detection — DB-backed, no Hubitat HTTP.

Groovy parity: mirrors the Active() function from the original Groovy source,
but uses our own event_log (populated by the eventsocket WS client) as the
source of truth instead of polling Hubitat over HTTP.

Two-tier check:
  Tier 1 — in-memory last_motion_time timestamp (sub-microsecond)
  Tier 2 — event_log query for active events within timeout window
           (single PostgREST GET, ~5-15ms)

The previous Tier 2 (Hubitat live API) and Tier 3 (Hubitat event history)
were removed 2026-05-17. They added 200-1000ms of HTTP latency per call,
and they queried the same data we already mirror into event_log via the
eventsocket. With the WS as sole intake (eventsocket-SOT migration on
2026-05-16), event_log IS the authoritative recent-events store.
"""

import os
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import requests


class MotionDetectionMixin:
    """Mixin: motion activity check via in-memory cache + event_log SQL."""

    def _is_motion_active(self) -> bool:
        """
        Return True if motion should be considered "currently active" for
        this instance.

        CANONICAL LOGIC (Elfege's directive 2026-05-19, after multiple
        rounds of getting this wrong by confusing events with states):

          Each sensor has a CURRENT STATE — the value of its most recent
          motion event. State is 'active' or 'inactive'.

            - ANY sensor currently 'active'  →  motion is on (keep on).
              No timeout window applies; the sensor itself is reporting
              motion right now. Lights stay on until ALL sensors flip
              to 'inactive'.

            - ALL sensors currently 'inactive'  →  start the off-timer
              from the LATEST "went inactive" timestamp (i.e., the
              moment the LAST sensor to be active finally flipped off).
              If now - that timestamp < timeout_for_this_mode →
              still in window → keep on. Else → off.

          This is NOT a "find active EVENT within last N seconds" check.
          The earlier version conflated event-in-window with current state
          and missed the case where a sensor (e.g. the GE one) stays
          IN 'active' state for 14 minutes between events — events-in-window
          said "expired" but the sensor was literally still active the
          whole time.

        Returns:
            True if motion is considered active (keep on), False otherwise.
        """
        functional = [
            sid for sid, ok in self._functional_sensors.items() if ok
        ]

        if not functional:
            if self.get_setting('considerActiveWhenFail', False):
                self.logger.warning("No functional sensors, assuming active (fail-safe)")
                return True
            return False

        timeout_seconds = self._get_timeout_seconds()

        # Query PER sensor: most recent motion event regardless of value.
        # That row's event_value IS the sensor's current state; its
        # received_at IS when that state began.
        #
        # PostgREST doesn't let us do "max per group" in one round trip
        # without RPC, so we loop. With 1-5 sensors per instance the
        # cost is negligible compared to one wide query + client-side
        # group-by, and the code stays straightforward.
        pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        per_sensor_state: Dict[str, Tuple[str, str]] = {}
        for sid in functional:
            try:
                r = requests.get(
                    f"{pg}/event_log",
                    params={
                        'canonical_device_id': f'eq.{sid}',
                        'event_type': 'eq.motion',
                        'select': 'received_at,event_value',
                        'order': 'received_at.desc',
                        'limit': '1',
                    },
                    timeout=3,
                )
                if r.status_code == 200 and r.json():
                    row = r.json()[0]
                    per_sensor_state[str(sid)] = (
                        (row.get('event_value') or '').lower(),
                        row.get('received_at') or '',
                    )
            except Exception as e:
                self.logger.warning(
                    f"motion: per-sensor state query failed for canon={sid}: {e}"
                )

        # Rule 1: ANY sensor currently 'active' → motion is on.
        currently_active = [
            sid for sid, (val, _) in per_sensor_state.items() if val == 'active'
        ]
        if currently_active:
            self.logger.debug(
                f"motion: sensors {currently_active} currently in ACTIVE state"
                f" — keep on"
            )
            return True

        # Rule 2: ALL sensors currently 'inactive'. Compute the off-timer
        # anchor as the actual TRANSITION moment per sensor (first
        # 'inactive' event after the last 'active' event), then take the
        # MAX across sensors.
        #
        # 2026-06-16 fix: previously we used max(per_sensor_state inactive
        # timestamps). That broke when the hub emitted polled "inactive"
        # state-report events for sensors that had ALREADY been inactive
        # for hours (e.g. firmware-update aftermath). Those polled events
        # have empty src and represent state CONFIRMATION, not transition.
        # The latest such event reset the off-timer anchor to 'just now,'
        # making the timeout window open again and master() turned on
        # every AML instance's lights at 5 AM with nobody home. The
        # transition-based query below ignores polled re-confirmations:
        # subsequent inactive events for an already-inactive sensor
        # don't move the anchor.
        transition_ts: List[str] = []
        for sid in functional:
            val, _ = per_sensor_state.get(str(sid), ('', ''))
            if val != 'inactive':
                continue
            try:
                # Most recent ACTIVE event for this sensor (if any).
                r_active = requests.get(
                    f"{pg}/event_log",
                    params={
                        'canonical_device_id': f'eq.{sid}',
                        'event_type': 'eq.motion',
                        'event_value': 'eq.active',
                        'select': 'received_at',
                        'order': 'received_at.desc',
                        'limit': '1',
                    },
                    timeout=3,
                )
                last_active_iso = ''
                if r_active.status_code == 200 and r_active.json():
                    last_active_iso = r_active.json()[0].get('received_at') or ''

                # FIRST inactive event AFTER the last active. If the sensor
                # has never been active in the log, the earliest inactive
                # event is treated as the transition (cold-cache case).
                params = {
                    'canonical_device_id': f'eq.{sid}',
                    'event_type': 'eq.motion',
                    'event_value': 'eq.inactive',
                    'select': 'received_at',
                    'order': 'received_at.asc',
                    'limit': '1',
                }
                if last_active_iso:
                    params['received_at'] = f'gt.{last_active_iso}'
                r_trans = requests.get(
                    f"{pg}/event_log", params=params, timeout=3,
                )
                if r_trans.status_code == 200 and r_trans.json():
                    t = r_trans.json()[0].get('received_at')
                    if t:
                        transition_ts.append(t)
            except Exception as e:
                self.logger.warning(
                    f"motion: transition query failed for canon={sid}: {e}"
                )

        if not transition_ts:
            # No motion events for any subscribed sensor at all (cold
            # cache / fresh install). Per the canonical logic, no
            # evidence of active = off. The first incoming active event
            # will trip the on path immediately.
            return False

        # All-inactive moment = the latest of the per-sensor TRANSITION
        # times (the last sensor to actually go from active -> inactive).
        # ISO-8601 strings compare lexically, so max() works directly.
        latest_inactive_iso = max(transition_ts)
        try:
            latest_inactive_at = datetime.fromisoformat(
                latest_inactive_iso.replace('Z', '+00:00')
            )
            age_seconds = (
                datetime.now(timezone.utc) - latest_inactive_at
            ).total_seconds()
        except Exception as e:
            self.logger.warning(
                f"motion: failed to parse latest_inactive {latest_inactive_iso!r}: {e}"
            )
            return False

        if age_seconds < timeout_seconds:
            self.logger.debug(
                f"motion: all sensors inactive since {latest_inactive_iso} "
                f"(age={age_seconds:.1f}s < {timeout_seconds}s) — keep on"
            )
            return True

        self.logger.debug(
            f"motion: all sensors inactive since {latest_inactive_iso} "
            f"(age={age_seconds:.1f}s ≥ {timeout_seconds}s) — turn off"
        )
        return False

        # No active event in the timeout window → motion is off.
        return False
