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
from typing import Optional

import requests


class MotionDetectionMixin:
    """Mixin: motion activity check via in-memory cache + event_log SQL."""

    def _is_motion_active(self) -> bool:
        """
        Check if any configured motion sensor is currently reporting active.

        Two-tier check (fastest to slowest):
          Tier 1 — in-memory last_motion_time timestamp
          Tier 2 — event_log: motion=active for any sensor within timeout

        Returns:
            True if motion is considered active, False otherwise
        """
        functional = [
            sid for sid, ok in self._functional_sensors.items() if ok
        ]

        if not functional:
            # No functional sensors — use fail-safe setting
            if self.get_setting('considerActiveWhenFail', False):
                self.logger.warning("No functional sensors, assuming active (fail-safe)")
                return True
            return False

        timeout_seconds = self._get_timeout_seconds()

        # --- Tier 1: in-memory timestamp (fast path for normal runtime) ---
        if self._runtime.last_motion_time:
            # Tz-aware comparison: motion.py stores last_motion_time as
            # datetime.now(timezone.utc). Mixing naive here would raise.
            age = (datetime.now(timezone.utc)
                   - self._runtime.last_motion_time).total_seconds()
            if age < timeout_seconds:
                return True

        # --- Tier 2: event_log query (DB-as-truth, no Hubitat HTTP) ---
        # ONE query that asks "is there any motion=active event for ANY of
        # these sensors within the last <timeout> seconds?" PostgREST handles
        # the FROM clause, we just filter via the in-list operator + a time
        # window. Returns at most 1 row — we only need a yes/no.
        try:
            pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
            # PostgREST in.() takes a comma-separated list inside parens
            sensor_list = '(' + ','.join(str(s) for s in functional) + ')'
            # Compute cutoff as ISO. Use UTC explicitly; event_log.received_at
            # is TIMESTAMPTZ stored in UTC.
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(seconds=timeout_seconds)).isoformat()
            r = requests.get(
                f"{pg}/event_log",
                params={
                    'canonical_device_id': f'in.{sensor_list}',
                    'event_type': 'eq.motion',
                    'event_value': 'eq.active',
                    'received_at': f'gte.{cutoff}',
                    'select': 'received_at,canonical_device_id',
                    'order': 'received_at.desc',
                    'limit': '1',
                },
                timeout=3,
            )
            if r.status_code == 200:
                rows = r.json()
                if rows:
                    row = rows[0]
                    self.logger.debug(
                        f"Sensor canon={row['canonical_device_id']} "
                        f"motion=active recorded at {row['received_at']} "
                        f"(within {timeout_seconds}s timeout) — Tier 2 hit"
                    )
                    return True
            else:
                self.logger.warning(
                    f"Tier 2 event_log query non-200: {r.status_code}"
                )
        except Exception as e:
            # Network/DB error → fail closed (no motion). Don't fail open
            # because that would leave lights on indefinitely if the DB is
            # the thing that's broken.
            self.logger.warning(f"Tier 2 motion check failed: {e}")

        return False
