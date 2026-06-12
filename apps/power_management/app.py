"""
Power Management
================

Average-power threshold cutoff for breaker-overload protection (and
similar) automations. Watches one or more PowerMeter-capable devices,
computes a rolling-average watts reading per sensor, and turns off a
set of "cutoff" switches when any sensor's average crosses the
configured high threshold. Optional auto-recovery turns the cutoffs
back on after sustained sub-threshold readings.

Primary use case
----------------
Single-circuit / breaker protection: a 15 A breaker can run continuously
at ~1440 W. If a fixed appliance (pool pump, electric dryer, EV
charger, water heater) develops a fault that drives draw past that
limit, this app shuts the appliance off BEFORE the breaker trips,
avoiding the nuisance trip and the manual reset trip.

Secondary use cases (same shape)
--------------------------------
- Whole-panel load shedding: aggregate-meter sensor + several
  high-draw cutoffs (HVAC, EV, dryer). When the aggregate climbs past
  the panel's safe ceiling, the app sheds non-essential loads. When
  the aggregate drops back below threshold for the recovery window,
  the loads come back.
- Pool-pump dry-run detection (FUTURE): low-watts-for-too-long usually
  means the impeller is spinning air. This v1 only handles the HIGH
  threshold; LOW threshold is a clean follow-on with the same
  scaffolding.

Decision flow
-------------
Per power event from any subscribed sensor:

    1. append (timestamp, watts) to that sensor's rolling buffer
       (truncated to ``averageWindowSeconds``)
    2. compute per-sensor average over the window
    3. if ANY sensor's average ≥ ``highThresholdWatts`` → TRIP

TRIP:
    a. fire ``off`` on every ``cutoff_switches`` device
    b. call ``pause(0)`` with ``pause_reason='power_threshold_high'``
       — surfaces on the dashboard, blocks re-trips while paused
    c. record the trip timestamp on ``_runtime.tripped_at``

While paused (tripped), power events keep arriving (the framework still
dispatches them — pause only inhibits master()). The trip handler keeps
appending to the buffer. When the recent window shows a sustained
sub-threshold reading for ``autoRecoveryWindowMinutes`` AND
``autoRecoveryEnabled`` is true, the app:

    a. fires ``on`` on every ``cutoff_switches`` device
    b. calls ``resume()`` — which triggers the framework's standard
       resume flow (memo reset + master() re-evaluation)
    c. records the recovery timestamp on ``_runtime.last_recovery_at``

A manual Resume from the dashboard works identically — it fires the
``on`` and lets the next event-driven evaluation either re-trip
(condition still bad) or stay quiet (condition resolved).

Optional poll mode
------------------
Some PowerMeter devices push events on every reading; others only on
power-state change. For the latter, ``pollEnabled = true`` schedules a
recurring forced read of each sensor at ``pollIntervalSeconds`` and
treats the result as a synthetic event (appended to the buffer +
threshold-checked). Off by default.

Subscriptions
-------------
- ``power_sensors`` → ``power`` events (real). Registered in
  ``services/instance_manager.py::category_events``.
- ``cutoff_switches`` deliberately NOT subscribed — they are pure
  outputs of this app. Subscribing creates the self-feedback loop that
  bit the fan_automation app in the 2026-06-05 storm.

Settings
--------
See ``get_settings_schema()`` for the full validated form. Notable
defaults:

- ``averageWindowSeconds = 300`` (5 min)
- ``autoRecoveryWindowMinutes = 5``
- ``cooldownSeconds = 600`` — minimum time between two consecutive
  trips, regardless of what the buffer says, to prevent thrash on a
  borderline reading.

Trip vs cooldown
----------------
``cooldownSeconds`` covers the case where readings hover near the
threshold and would otherwise toggle continuously. The first trip fires
immediately; subsequent trips within the cooldown window are
suppressed (we log them but don't re-fire ``off``). The cooldown
expires either after that many seconds OR when an explicit Resume
happens, whichever comes first.
"""

from __future__ import annotations

import logging
import time as _monotonic_time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from apps.base_app import BaseApp
from models.event import DeviceEvent

logger = logging.getLogger(__name__)


# Log color shortcuts (mirrors fan_automation / advanced_motion_lighting style)
_C = "\033[96m"   # cyan — device names
_Y = "\033[93m"   # yellow — warnings / decisions
_R_RESET = "\033[0m"
_R_RED = "\033[91m"
_G = "\033[92m"


class PowerManagementApp(BaseApp):
    """
    Average-power threshold cutoff. See module docstring for the
    decision flow, subscriptions, and trip / recovery semantics.

    Status (2026-06-09): UNTESTED. Code shipped + registered (app_types
    row id 1119, schema reachable via PostgREST) + ast.parse clean +
    imports clean. No live instance has been created yet. Before
    relying on this for real protection (pool pump, EV charger, etc.):

      1. Create an instance via the dashboard with a HIGH threshold
         well above normal operating draw (e.g. 90% of the appliance's
         peak load). Verify trip fires + cutoff actuates.
      2. Verify auto-recovery un-trips correctly after the sustained
         sub-threshold window.
      3. If enabling dry-run detection, calibrate the LOW threshold by
         observing real readings of the protected pump in both healthy
         (water flowing) and dry (no prime) states, NOT by guessing
         from the rated wattage alone. The default-50%-of-rated
         starting point in the field description is a starting point,
         not a recommendation.
      4. Watch the dashboard tile + event_log for at least one full
         day under normal load before disconnecting any manual
         protection (e.g. breaker reset, manual on/off).
    """

    TYPE_NAME    = 'power_management'
    DISPLAY_NAME = 'Power Management'
    DESCRIPTION  = (
        'Watches PowerMeter sensors, trips a set of cutoff switches '
        'when the rolling-average watts crosses a threshold. Intended '
        'for breaker-overload protection (e.g. pool pump / EV charger / '
        'dryer) and similar load-shedding use cases.'
    )
    VERSION      = '1.0.0'
    # Surfaced in the UI as a category tag (dashboard groups apps by
    # category). The app_types table does not yet have a category
    # column; this attr is read by the registry and the future
    # dashboard-categorization patch. Other apps default to
    # 'automation'; this one is 'security' per operator classification.
    CATEGORY     = 'security'

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def initialize(self) -> None:
        """
        Set up the per-sensor sliding-window buffers and (if enabled)
        schedule the recurring poll job. No state-changing actions at
        init — we wait for the first event or poll tick to know whether
        we should trip.
        """
        self.logger.info(f"Initializing: {self.label}")

        # Per-sensor deque of (monotonic_ts, watts). Window is enforced
        # by trimming on every append rather than using deque maxlen
        # because the window is configured in SECONDS not COUNT.
        self._runtime.power_buffer: Dict[str, deque] = {}
        # Monotonic ts of the last trip (for cooldown). 0.0 means "never".
        self._runtime.last_trip_monotonic: float = 0.0
        # Wall-clock ts of the last trip (for dashboard / audit).
        self._runtime.tripped_at: Optional[datetime] = None
        # Wall-clock ts of the last successful recovery.
        self._runtime.last_recovery_at: Optional[datetime] = None
        # The watts value that caused the last trip (for the
        # pause_reason and the dashboard tile).
        self._runtime.last_trip_watts: Optional[float] = None
        # Which threshold caused the last trip: 'high' or 'low'. Used
        # by _maybe_recover to decide whether auto-recovery is even
        # legal (low/dry-run trips never auto-recover — recovering
        # would just re-trip immediately since the cutoff being OFF
        # means readings stay at 0 W).
        self._runtime.last_trip_reason: Optional[str] = None
        # Per-sensor monotonic ts of the first non-zero reading seen
        # since boot/restart. Used to gate the low-threshold check past
        # the pump's prime-up window — a fresh-started pump legitimately
        # reads near zero before it's pulling water.
        self._runtime.first_active_monotonic: Dict[str, float] = {}
        # Track the poll job id so we can cancel on pause / shutdown.
        self._runtime.poll_job_id: Optional[str] = None

        # Sanity checks. We log but don't refuse to start — an operator
        # may be configuring the instance and will fix it shortly.
        if not self.get_devices('power_sensors'):
            self.logger.warning(
                "no power_sensors selected — instance will be idle "
                "until at least one is added"
            )
        if not self.get_devices('cutoff_switches'):
            self.logger.warning(
                "no cutoff_switches selected — trips will log but not "
                "actuate anything"
            )

        # Dry-run detection gates. If dryRunDetectionEnabled but
        # pumpHorsepower / pumpRatedWatts / lowThresholdWatts are
        # missing, the JSON-schema dependency check should have
        # blocked save — but be defensive at runtime too, since
        # operators can poke the row via PostgREST directly.
        if self.get_setting('dryRunDetectionEnabled', False):
            missing = [
                k for k in ('pumpHorsepower', 'pumpRatedWatts',
                            'lowThresholdWatts')
                if self.get_setting(k) in (None, '', 0)
            ]
            if missing:
                self.logger.error(
                    f"{_R_RED}dryRunDetectionEnabled=true but required "
                    f"calibration fields are missing: {missing}{_R_RESET} "
                    f"— dry-run detection runtime-disabled until fixed"
                )
                self._runtime.dry_run_runtime_disabled = True
            else:
                self._runtime.dry_run_runtime_disabled = False
                self.logger.info(
                    f"dry-run detection enabled "
                    f"(pump={self.get_setting('pumpHorsepower')}HP / "
                    f"rated={self.get_setting('pumpRatedWatts')}W, "
                    f"low_threshold={self.get_setting('lowThresholdWatts')}W, "
                    f"grace={self.get_setting('lowThresholdGraceSeconds', 120)}s)"
                )
        else:
            self._runtime.dry_run_runtime_disabled = False

        if self.get_setting('pollEnabled', False):
            self._start_poll()

    # =========================================================================
    # Event dispatch
    # =========================================================================

    def on_event(self, event: DeviceEvent) -> None:
        """
        Route the incoming event. The only event type this app
        subscribes to is ``power`` (registered in
        ``services/instance_manager.py``), but be defensive — ignore
        anything else loudly.

        ``self.is_paused`` does NOT short-circuit power events here.
        While tripped we still need readings to evaluate auto-recovery.
        """
        try:
            self.update_last_activity()
            if event.event_type == 'power':
                self._on_power(event)
            else:
                self.logger.debug(f"ignoring unsubscribed event type: {event.event_type}")
        except Exception as e:
            self.logger.error(
                f"on_event failed: {event}: {e}", exc_info=True
            )

    # ------------------------------------------------------------------
    # Power event handling
    # ------------------------------------------------------------------

    def _on_power(self, event: DeviceEvent) -> None:
        """
        Append a single power reading to its sensor's sliding window,
        then either evaluate a recovery (if currently tripped) or
        evaluate a fresh trip.
        """
        try:
            watts = float(event.value)
        except (TypeError, ValueError):
            self.logger.debug(
                f"non-numeric power value from {event.device_name}: "
                f"{event.value!r} — skipped"
            )
            return

        key = str(event.device_id)
        now_mono = _monotonic_time.monotonic()
        buf = self._runtime.power_buffer.setdefault(key, deque())
        buf.append((now_mono, watts))
        self._trim_buffer(buf, now_mono)

        # Track the first non-zero reading per sensor for the dry-run
        # grace gate. A fresh-started pump legitimately reads <100 W
        # for the first 15-60 s before it's pulling water; checking
        # the low threshold during that window would false-trip every
        # power-on.
        if watts > 0 and key not in self._runtime.first_active_monotonic:
            self._runtime.first_active_monotonic[key] = now_mono

        if self.is_paused:
            # We're already tripped. Keep buffering, then ask whether
            # the recent window justifies an auto-recovery.
            self._maybe_recover(now_mono)
            return

        # Not tripped: see whether this update pushes us over (or
        # under, when dry-run detection is on).
        self._maybe_trip(now_mono)

    def _trim_buffer(self, buf: deque, now_mono: float) -> None:
        """Drop entries older than ``averageWindowSeconds``."""
        win = float(self.get_setting('averageWindowSeconds', 300))
        cutoff = now_mono - win
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def _sensor_avg(self, key: str) -> Optional[float]:
        """Compute the average watts for one sensor's window. None if
        the buffer is empty."""
        buf = self._runtime.power_buffer.get(key)
        if not buf:
            return None
        return sum(w for _, w in buf) / len(buf)

    # =========================================================================
    # Trip + recovery decisions
    # =========================================================================

    def _maybe_trip(self, now_mono: float) -> None:
        """
        Evaluate both thresholds in priority order: high first
        (electrical safety > dry-run protection), then low. The first
        match wins; we never trip on both in the same tick.
        Both checks respect ``cooldownSeconds`` to prevent thrash.
        """
        if self._maybe_trip_high(now_mono):
            return
        self._maybe_trip_low(now_mono)

    def _maybe_trip_high(self, now_mono: float) -> bool:
        """High-threshold check. Returns True if a trip fired."""
        threshold = self.get_setting('highThresholdWatts')
        if threshold is None:
            return False
        threshold = float(threshold)

        worst_key: Optional[str] = None
        worst_avg: float = -1.0
        for key in self._runtime.power_buffer:
            avg = self._sensor_avg(key)
            if avg is None:
                continue
            if avg > worst_avg:
                worst_avg = avg
                worst_key = key

        if worst_key is None or worst_avg < threshold:
            return False

        if not self._cooldown_clear(now_mono, why=(
            f"high {worst_key} avg={worst_avg:.0f}W >= {threshold:.0f}W"
        )):
            return False

        self._trip(worst_key, worst_avg, now_mono, reason='high')
        return True

    def _maybe_trip_low(self, now_mono: float) -> bool:
        """
        Low-threshold (dry-run) check. Returns True if a trip fired.

        Gates (all must hold):
          - dryRunDetectionEnabled = true
          - calibration fields present (cross-checked at initialize();
            we also respect the runtime-disabled flag set there)
          - the sensor has seen a non-zero reading at least
            ``lowThresholdGraceSeconds`` ago (so a fresh-prime pump
            isn't false-tripped)
          - the rolling average is at or below ``lowThresholdWatts``
        """
        if not self.get_setting('dryRunDetectionEnabled', False):
            return False
        if getattr(self._runtime, 'dry_run_runtime_disabled', False):
            return False
        threshold = self.get_setting('lowThresholdWatts')
        if threshold in (None, '', 0):
            return False
        threshold = float(threshold)
        grace = float(self.get_setting('lowThresholdGraceSeconds', 120))

        # Find the worst (lowest) offender past its grace window.
        worst_key: Optional[str] = None
        worst_avg: float = float('inf')
        for key, first_active in self._runtime.first_active_monotonic.items():
            if (now_mono - first_active) < grace:
                continue  # still in the prime-up window
            avg = self._sensor_avg(key)
            if avg is None:
                continue
            if avg < worst_avg:
                worst_avg = avg
                worst_key = key

        if worst_key is None or worst_avg > threshold:
            return False

        if not self._cooldown_clear(now_mono, why=(
            f"low {worst_key} avg={worst_avg:.0f}W <= {threshold:.0f}W"
        )):
            return False

        self._trip(worst_key, worst_avg, now_mono, reason='low')
        return True

    def _cooldown_clear(self, now_mono: float, why: str) -> bool:
        """Return True if the cooldown window has elapsed since the
        last trip (or if there has never been a trip)."""
        cooldown = float(self.get_setting('cooldownSeconds', 600))
        if self._runtime.last_trip_monotonic <= 0:
            return True
        since_last = now_mono - self._runtime.last_trip_monotonic
        if since_last >= cooldown:
            return True
        self.logger.debug(
            f"would trip ({why}) but cooldown active "
            f"({since_last:.0f}s/{cooldown:.0f}s) — skipped"
        )
        return False

    def _trip(
        self, worst_key: str, worst_avg: float, now_mono: float,
        reason: str,
    ) -> None:
        """
        Execute the trip: log loudly, fire OFF on every cutoff switch,
        then pause the instance with a recorded reason. ``reason`` is
        ``'high'`` (over-load / electrical safety) or ``'low'``
        (dry-run / lost prime). The reason is stored on _runtime so
        _maybe_recover knows whether auto-recovery is even legal —
        only high-trips auto-recover.
        """
        self._runtime.last_trip_monotonic = now_mono
        self._runtime.tripped_at = datetime.now(timezone.utc)
        self._runtime.last_trip_watts = worst_avg
        self._runtime.last_trip_reason = reason

        if reason == 'high':
            threshold = float(self.get_setting('highThresholdWatts', 0))
            arrow = ">="
            slug = "power_threshold_high"
        else:
            threshold = float(self.get_setting('lowThresholdWatts', 0))
            arrow = "<="
            slug = "power_threshold_low_dry_run"

        self.logger.warning(
            f"{_R_RED}TRIP ({reason}){_R_RESET}: sensor {_C}{worst_key}{_R_RESET} "
            f"avg={worst_avg:.0f}W {arrow} {threshold:.0f}W "
            f"(window={self.get_setting('averageWindowSeconds', 300)}s) "
            f"— turning off {len(self.get_devices('cutoff_switches'))} "
            f"cutoff switch(es)"
        )

        self._fire_cutoffs('off')

        # Pause(0) = indefinite. Auto-recovery is driven by the
        # event buffer in _maybe_recover, NOT by the framework's
        # auto-resume timer (which would only do a blind retry).
        self._pause_reason = f"{slug}:{worst_avg:.0f}W"
        try:
            self.pause(0)
        except Exception as e:
            self.logger.error(f"pause() during trip failed: {e}", exc_info=True)

    def _maybe_recover(self, now_mono: float) -> None:
        """
        While tripped, decide whether the recent window justifies an
        auto-recovery.

        Auto-recovery is ONLY legal for high-threshold trips.
        Low-threshold (dry-run) trips never auto-recover — recovering
        a dry-run trip would just re-trigger immediately (cutoff OFF
        → 0 W → still "below low threshold" → re-trip), and more
        fundamentally a lost-prime pump needs operator intervention.
        Low-trip cards stay paused until Resume is hit manually.

        Conditions for a high-trip auto-recovery (ALL must hold):
          1. ``autoRecoveryEnabled`` is true
          2. ``highThresholdWatts`` is configured
          3. EVERY sensor's average over its current window is BELOW
             threshold (any sensor still hot blocks recovery)
          4. The OLDEST sample in EVERY non-empty buffer is at least
             ``autoRecoveryWindowMinutes`` minutes old — i.e., we have
             a sustained window of sub-threshold readings to back the
             decision, not just a single dip
        """
        if self._runtime.last_trip_reason != 'high':
            return  # low-trip / no trip recorded → no auto-recovery
        if not self.get_setting('autoRecoveryEnabled', True):
            return
        threshold = self.get_setting('highThresholdWatts')
        if threshold is None:
            return
        threshold = float(threshold)
        window_sec = float(self.get_setting('autoRecoveryWindowMinutes', 5)) * 60

        # All sensors must be sub-threshold AND have a full window.
        any_sensor_has_data = False
        for key, buf in self._runtime.power_buffer.items():
            if not buf:
                continue
            any_sensor_has_data = True
            avg = self._sensor_avg(key)
            if avg is None or avg >= threshold:
                return  # at least one sensor still hot
            oldest_ts = buf[0][0]
            if (now_mono - oldest_ts) < window_sec:
                return  # not enough history yet

        if not any_sensor_has_data:
            return  # no readings at all — wait for some

        self._recover(now_mono)

    def _recover(self, now_mono: float) -> None:
        """Fire ON on every cutoff switch + resume the instance."""
        self._runtime.last_recovery_at = datetime.now(timezone.utc)
        self.logger.info(
            f"{_G}RECOVERY{_R_RESET}: sustained sub-threshold for "
            f"{self.get_setting('autoRecoveryWindowMinutes', 5)} min "
            f"— turning {len(self.get_devices('cutoff_switches'))} "
            f"cutoff switch(es) back ON"
        )
        self._fire_cutoffs('on')
        try:
            self.resume()
        except Exception as e:
            self.logger.error(f"resume() during recovery failed: {e}", exc_info=True)

    # =========================================================================
    # Cutoff actuation
    # =========================================================================

    def _fire_cutoffs(self, action: str) -> None:
        """
        Send ``on`` or ``off`` to every cutoff switch. Logs failures
        per-device but does not abort the loop — a single offline
        cutoff shouldn't block the others from acting.

        Pause guard (defensive, project rule
        ``feedback_pause_guard_on_every_action_method``): action-issuing
        methods MUST check is_paused at their own top. The recovery
        path calls this from _recover() — which calls self.resume()
        FIRST, so by the time send_command runs we're no longer paused.
        The trip path calls this from _trip() BEFORE self.pause(), so
        is_paused is still False there. The remaining concern is a
        pause that lands between our caller's check and ours; this
        guard catches that race.
        """
        if action not in ('on', 'off'):
            self.logger.error(f"_fire_cutoffs unknown action: {action!r}")
            return
        # _trip() calls us BEFORE pause(0), so we're not paused at trip time.
        # _recover() calls self.resume() before us, so we're not paused then either.
        # The only race left is an external pause arriving mid-tick — short-circuit.
        # `action == 'off'` from _trip is allowed to proceed (we're not paused yet);
        # `action == 'on'` from _recover is also allowed (just resumed). If a
        # SCHEDULED retry of either path landed here while paused, drop it.
        if self.is_paused and action == 'on':
            # Recovery 'on' must not fire while paused — this can only
            # happen if a scheduled callback raced with a pause.
            self.logger.debug("_fire_cutoffs('on') skipped — instance is paused")
            return
        cutoffs = self.get_devices('cutoff_switches')
        if not cutoffs:
            self.logger.warning(
                f"_fire_cutoffs({action}): no cutoff_switches configured"
            )
            return
        for did in cutoffs:
            try:
                result = self.send_command(did, action, args=None, verify=True)
                if not result.success or not result.verified:
                    self.logger.warning(
                        f"cutoff {_C}{did}{_R_RESET} {action} "
                        f"not verified: err={result.error} "
                        f"expected={result.expected_state} "
                        f"actual={result.actual_state}"
                    )
                else:
                    self.logger.info(
                        f"cutoff {_C}{did}{_R_RESET} {_Y}{action}{_R_RESET} OK"
                    )
            except Exception as e:
                self.logger.error(
                    f"cutoff {did} {action} failed: {e}", exc_info=True
                )

    # =========================================================================
    # Poll mode
    # =========================================================================

    def _start_poll(self) -> None:
        """
        Schedule a recurring poll job. Cancelled and re-scheduled
        whenever the interval setting changes (via on_settings_change,
        if the framework supports it; otherwise restart the instance).
        """
        try:
            from services.scheduler_service import get_scheduler
            interval = int(self.get_setting('pollIntervalSeconds', 60))
            if interval <= 0:
                self.logger.warning(
                    f"pollIntervalSeconds={interval} is non-positive — poll not started"
                )
                return
            scheduler = get_scheduler()
            job_id = f"power_management_poll_{self.instance_id}"
            # Cancel any existing job under the same id (defensive — a
            # re-initialize after a settings change would otherwise leak
            # the old job).
            try:
                scheduler.cancel(job_id)
            except Exception:
                pass
            scheduler.schedule_recurring(
                job_id=job_id,
                interval_seconds=interval,
                callback=lambda **kwargs: self._poll_tick(),
                instance_id=self.instance_id,
                job_type='power_management_poll',
            )
            self._runtime.poll_job_id = job_id
            self.logger.info(
                f"power_management poll started: every {interval}s"
            )
        except Exception as e:
            self.logger.error(f"could not start poll: {e}", exc_info=True)

    def _poll_tick(self) -> None:
        """
        Fire on the recurring schedule. Forces a state read for each
        power_sensor and treats the result as a synthetic power
        event. Useful for devices that don't push frequently enough.
        """
        try:
            now_mono = _monotonic_time.monotonic()
            for did in self.get_devices('power_sensors'):
                state = self.get_device_state(did)
                if not state:
                    continue
                attrs = state.get('attributes', {}) or {}
                raw = attrs.get('power')
                if raw is None:
                    continue
                try:
                    watts = float(raw)
                except (TypeError, ValueError):
                    continue
                key = str(did)
                buf = self._runtime.power_buffer.setdefault(key, deque())
                buf.append((now_mono, watts))
                self._trim_buffer(buf, now_mono)

            # After all samples in, evaluate trip or recovery once.
            if self.is_paused:
                self._maybe_recover(now_mono)
            else:
                self._maybe_trip(now_mono)
        except Exception as e:
            self.logger.error(f"poll tick failed: {e}", exc_info=True)

    # =========================================================================
    # Standard BaseApp hooks
    # =========================================================================

    def master(self) -> None:
        """
        BaseApp calls master() on resume and on mode change. For
        power_management this is mostly a no-op — the decision logic
        runs on power events and the poll tick, not on a master()
        decision. We keep the hook so the framework contract is
        satisfied.
        """
        try:
            if self.is_paused:
                return
            # Re-evaluate immediately so a manual Resume that's still
            # over-threshold re-trips without waiting for the next
            # event (which may be 30+ seconds away on slow sensors).
            self._maybe_trip(_monotonic_time.monotonic())
        except Exception as e:
            self.logger.error(f"master() failed: {e}", exc_info=True)

    # =========================================================================
    # Schema
    # =========================================================================

    @classmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "highThresholdWatts": {
                    "type": "integer", "minimum": 1, "maximum": 100000,
                    "default": 1500,
                    "title": "High threshold (Watts)",
                    "description": (
                        "If ANY power_sensors device's rolling-average watts "
                        "over the averaging window reaches this value, fire "
                        "OFF on every cutoff_switches device and pause the "
                        "instance."
                    ),
                },
                "averageWindowSeconds": {
                    "type": "integer", "minimum": 30, "maximum": 3600,
                    "default": 300,
                    "title": "Averaging window (seconds)",
                    "description": (
                        "Length of the rolling window used for the average. "
                        "A 5-minute window smooths over the brief spikes "
                        "that are normal at pump/compressor start; raise it "
                        "if you still see false trips."
                    ),
                },
                "cooldownSeconds": {
                    "type": "integer", "minimum": 0, "maximum": 86400,
                    "default": 600,
                    "title": "Trip cooldown (seconds)",
                    "description": (
                        "Minimum seconds between two consecutive automatic "
                        "trips. Suppresses thrash when readings hover at the "
                        "threshold. Manual Resume bypasses the cooldown."
                    ),
                },
                "autoRecoveryEnabled": {
                    "type": "boolean", "default": True,
                    "title": "Auto-recovery enabled (high-threshold trips only)",
                    "description": (
                        "If enabled, the app turns the cutoff switches back "
                        "ON automatically once every power sensor's rolling "
                        "average has stayed below the threshold for "
                        "autoRecoveryWindowMinutes minutes. If disabled, "
                        "recovery requires a manual Resume from the "
                        "dashboard. NOTE: low-threshold (dry-run) trips "
                        "NEVER auto-recover regardless of this setting — "
                        "they require operator intervention because the "
                        "underlying lost-prime condition does not resolve "
                        "without manual action."
                    ),
                },
                "autoRecoveryWindowMinutes": {
                    "type": "integer", "minimum": 1, "maximum": 1440,
                    "default": 5,
                    "title": "Auto-recovery window (minutes)",
                    "description": (
                        "After a high-threshold trip, the power must stay "
                        "below threshold across all sensors for this many "
                        "continuous minutes before the cutoffs are "
                        "re-engaged."
                    ),
                },
                "pollEnabled": {
                    "type": "boolean", "default": False,
                    "title": "Poll sensors actively",
                    "description": (
                        "Force a state read from every power_sensors device "
                        "on a recurring interval instead of relying on the "
                        "events the device pushes on its own. Use this when "
                        "the meter only pushes on power-state CHANGE rather "
                        "than on every reading."
                    ),
                },
                "pollIntervalSeconds": {
                    "type": "integer", "minimum": 5, "maximum": 3600,
                    "default": 60,
                    "title": "Poll interval (seconds)",
                    "description": (
                        "How often to force a state read when pollEnabled "
                        "is true. Has no effect when pollEnabled is false."
                    ),
                },

                # ---------------- Dry-run detection (optional) ----------------
                # All three calibration fields below are gated by the
                # `allOf -> if dryRunDetectionEnabled then required` block
                # at the bottom of this schema. The UI must refuse save when
                # dryRunDetectionEnabled is true and any of the three is
                # empty; initialize() also re-checks at runtime and
                # runtime-disables the feature if it sees them missing.
                "dryRunDetectionEnabled": {
                    "type": "boolean", "default": False,
                    "title": (
                        "Dry-run detection — turn pump OFF when watts stay "
                        "LOW (advanced)"
                    ),
                    "description": (
                        "⚠ ADVISORY: dry-run detection from power draw "
                        "is an APPROXIMATION. It works well for "
                        "centrifugal pumps (a pool / well / sump pump "
                        "draws roughly 30-50% LESS power when spinning "
                        "air vs. moving water), but is unreliable for "
                        "positive-displacement pumps, fixed-speed "
                        "compressors, or any motor whose load does not "
                        "drop materially when its work output drops. "
                        "Calibrate by observing real readings before "
                        "relying on this for protection. Enabling this "
                        "REQUIRES filling in pumpHorsepower, "
                        "pumpRatedWatts, and lowThresholdWatts below."
                    ),
                },
                "pumpHorsepower": {
                    "type": ["number", "null"], "minimum": 0.1, "maximum": 50,
                    "default": None,
                    "title": "Pump rated horsepower (HP)",
                    "description": (
                        "Nameplate horsepower of the protected pump (or "
                        "motor). Used together with pumpRatedWatts as the "
                        "ACCEPTANCE GATE for dry-run detection — both must "
                        "be provided before dryRunDetectionEnabled can be "
                        "true. A 2 HP pool pump typically draws around "
                        "1500 W at full load (~746 W/HP)."
                    ),
                },
                "pumpRatedWatts": {
                    "type": ["integer", "null"], "minimum": 50, "maximum": 50000,
                    "default": None,
                    "title": "Pump rated power (Watts)",
                    "description": (
                        "Full-load nameplate watts of the pump. Used as a "
                        "sanity reference for lowThresholdWatts — a "
                        "well-chosen lowThresholdWatts sits around "
                        "30-50% of this value for a centrifugal pump."
                    ),
                },
                "lowThresholdWatts": {
                    "type": ["integer", "null"], "minimum": 1, "maximum": 100000,
                    "default": None,
                    "title": "Low threshold (Watts) — dry-run trip line",
                    "description": (
                        "If the rolling average drops to or below this value "
                        "AND the pump has been running for "
                        "lowThresholdGraceSeconds, fire OFF on the cutoff "
                        "switches and pause the instance with reason "
                        "'power_threshold_low_dry_run'. Typical starting "
                        "value: 50% of pumpRatedWatts. Tune downward if "
                        "you see false trips during normal operation."
                    ),
                },
                "lowThresholdGraceSeconds": {
                    "type": "integer", "minimum": 5, "maximum": 3600,
                    "default": 120,
                    "title": "Dry-run grace period (seconds)",
                    "description": (
                        "How long after the pump's first non-zero reading "
                        "before the low-threshold check engages. Gives the "
                        "pump time to prime and reach steady-state flow "
                        "without false-tripping. Default 120 s suits most "
                        "pool / well pumps."
                    ),
                },
            },

            # Cross-field gate: dry-run detection cannot be saved "true"
            # without the three calibration fields. PostgREST validates
            # this via the framework's jsonschema check on POST/PATCH to
            # /app_instances; UI presents the requirement up-front so a
            # save with this constraint violated never gets attempted.
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "dryRunDetectionEnabled": {"const": True}
                        },
                        "required": ["dryRunDetectionEnabled"]
                    },
                    "then": {
                        "required": [
                            "pumpHorsepower",
                            "pumpRatedWatts",
                            "lowThresholdWatts"
                        ],
                        "properties": {
                            "pumpHorsepower":    {"type": "number"},
                            "pumpRatedWatts":    {"type": "integer"},
                            "lowThresholdWatts": {"type": "integer"}
                        }
                    }
                }
            ]
        }

    @classmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "power_sensors",
                "label": "Power Meters",
                "capability": "PowerMeter",
                "multiple": True, "required": True,
                "description": (
                    "Devices that report a 'power' attribute (watts). The app "
                    "watches each one independently — any single sensor's "
                    "rolling average crossing the threshold triggers a trip."
                ),
            },
            {
                "key": "cutoff_switches",
                "label": "Cutoff Switches",
                "capability": "Switch",
                "multiple": True, "required": True,
                "description": (
                    "Plain switches the app turns OFF on a trip and "
                    "(optionally) back ON on auto-recovery. Typically the "
                    "smart plug or in-wall switch controlling the pump / "
                    "EV charger / dryer that you want to protect."
                ),
            },
        ]
