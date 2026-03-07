"""
Motion activity detection — three-tier check.

Groovy parity: mirrors the Active() function from the original Groovy source.

Tier 1 (fastest): in-memory timestamp from the last motion event.
Tier 2 (startup/reload): live device state query via Hubitat API.
Tier 3 (gap detection): recent event history query via Hubitat API.

Tiers 2 and 3 are critical on startup when the in-memory timestamp is None,
preventing the app from falsely assuming "no motion" and turning off lights
the moment the container restarts.
"""

from datetime import datetime
from typing import Optional


class MotionDetectionMixin:
    """Mixin: three-tier motion activity check for reliable startup and runtime behavior."""

    def _is_motion_active(self) -> bool:
        """
        Check if any configured motion sensor is currently reporting active.

        Three-tier check (fastest to slowest):
          Tier 1 — in-memory last_motion_time timestamp (normal runtime)
          Tier 2 — live Hubitat API currentValue check (startup/reload)
          Tier 3 — Hubitat event history within the timeout window (gap detection)

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
            age = (datetime.now() - self._runtime.last_motion_time).total_seconds()
            if age < timeout_seconds:
                return True

        # --- Tier 2: live device state from Hubitat (Groovy: currentValue) ---
        try:
            for sensor_id in functional:
                device = self.hubitat.get_device(sensor_id)
                if device and 'attributes' in device:
                    for attr in device['attributes']:
                        if (attr.get('name') == 'motion'
                                and attr.get('currentValue') == 'active'):
                            self.logger.debug(
                                f"Sensor {sensor_id} reports motion=active (live API)"
                            )
                            return True
        except Exception as e:
            self.logger.warning(f"Tier 2 motion check failed: {e}")

        # --- Tier 3: event history within timeout window (Groovy: eventsSince) ---
        try:
            for sensor_id in functional:
                events = self.hubitat.get_device_events(sensor_id, max_events=20)
                for event in events:
                    if event.get('name') == 'motion' and event.get('value') == 'active':
                        event_date_str = event.get('date', '')
                        if not event_date_str:
                            continue
                        try:
                            # Hubitat event dates: "2026-02-23T04:15:30+0000"
                            event_time = datetime.fromisoformat(
                                event_date_str.replace('+0000', '+00:00')
                            )
                            now = (
                                datetime.now(event_time.tzinfo)
                                if event_time.tzinfo else datetime.now()
                            )
                            age = (now - event_time).total_seconds()
                            if age < timeout_seconds:
                                self.logger.debug(
                                    f"Sensor {sensor_id} had motion=active "
                                    f"{age:.0f}s ago (within {timeout_seconds}s timeout)"
                                )
                                return True
                        except (ValueError, TypeError) as parse_err:
                            self.logger.debug(
                                f"Could not parse event date '{event_date_str}': {parse_err}"
                            )
        except Exception as e:
            self.logger.warning(f"Tier 3 motion check failed: {e}")

        return False
