"""
Humidifier App — MAINTAIN a room's humidity with a plug-switched humidifier
===========================================================================
Faithful port of the Hubitat Groovy app HUMIDIFIER.groovy
(``SMARTHOME_MAIN/APPS/CLIMATE/HUMIDITY/HUMIDIFIER.groovy``), which has run
reliably for years. This is deliberately a straight bang-bang controller — it
MAINTAINS humidity (turns a humidifier ON when the air is too dry), which is the
opposite of the exhaust-fan app (which REDUCES humidity, and is already ported
as ``fan_automation``). No proportional control is involved.

Control law (evaluated every 60s, and on every humidity / motion / contact
event) — identical to the Groovy ``master()``:

    Turn the humidifier plug(s) OFF if ANY of:
        - humidity >= threshold        (target reached — stop humidifying)
        - no motion                    (room empty — don't waste water/power)
        - any selected contact is open (door/window open — humidifying is futile)
        - location is in a restricted mode (and "off in restricted mode" is set)
    Otherwise turn them ON (air is dry, room occupied, closed up).

Everything is idempotent: each evaluation reads the plug's authoritative
switch state (``event_log`` source of truth, not the stale device_cache) and
only sends a command when the state actually needs to change.

Faithful-port scope notes (intentionally NOT carried over, to avoid
over-engineering a "just port the working logic" task):
    - Per-mode motion timeouts (Groovy ``modetimeout`` / ``timeoutValMode``)
      collapse to a single ``noMotionTime``.
    - The virtual-dimmer-sets-threshold convenience is dropped; the threshold
      is a plain numeric setting.
    - Battery-sensor polling (``polldevices``) is dropped; the eventsocket
      already feeds ``event_log`` and a 60s re-evaluation covers stale reads.
Each is a niche convenience; none change the core maintain-humidity behavior.

Design reuse (canonical framework idioms, not reinvented):
    - Switch state via ``get_switch_state()`` (event_log SOT — dodges the
      recurring "won't turn on" stale-cache regression class).
    - Humidity / motion / contact reads via ``get_latest_attribute()``
      (event_log SOT) with a device_cache fallback for cold devices.
    - Current location mode via the ``location_modes`` DB table (the
      DB-as-source-of-truth approach AML adopted once the Maker API was
      demoted).
    - Universal pause contract via ``UNIVERSAL_PAUSE_SETTINGS``.
    - The humidifier plug lives in an UNMAPPED device category so the
      framework never subscribes it to its own switch events (the fan-storm
      echo-loop failure mode — see instance_manager ``category_events``).
"""

from __future__ import annotations

import logging
import os
import time as _time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from apps.base_app import BaseApp
from models.event import DeviceEvent

logger = logging.getLogger(__name__)

# ANSI colors for log readability (matches the fan_automation convention).
_C = "\033[96m"   # cyan — device names
_Y = "\033[93m"   # yellow — decisions
_G = "\033[92m"   # green — on
_M = "\033[35m"   # magenta — humidity
_R = "\033[0m"    # reset


class HumidifierApp(BaseApp):
    """Bang-bang humidifier controller: keep humidity at/above a threshold."""

    TYPE_NAME = "humidifier"
    DISPLAY_NAME = "Humidifier"
    DESCRIPTION = (
        "Maintain a room's humidity: turn a humidifier plug ON when the air is "
        "dry (below the threshold) and the room is occupied, OFF once the "
        "target is reached, the room empties, or a door/window opens."
    )
    VERSION = "1.0.0"

    # Background re-evaluation cadence. The Groovy scheduled master() every
    # minute as a safety net behind the event handlers; we do the same so the
    # motion-timeout and a stale/slow sensor still get re-evaluated without an
    # event.
    _POLL_SECONDS = 60

    # When a humidity sensor has not reported yet (None/0), fall back to this
    # assumed reading so the app still behaves — mirrors the Groovy's 40%
    # fallback + warning. A real 0% is physically implausible → treated as
    # "no reading yet", same as the Groovy.
    _ASSUMED_HUMIDITY_ON_NO_READING = 40

    # Throttle the "sensor returned no value" warning to at most once per hour
    # per instance (the poll runs every 60s; we don't want 60 warnings/hour).
    _NO_READING_WARN_INTERVAL_SECS = 3600

    # =========================================================================
    # Lifecycle
    # =========================================================================
    def initialize(self) -> None:
        """Validate selections and register the 60s re-evaluation job.

        No command is issued at init: we wait for the first event or the first
        poll so the decision is made against observed device state rather than
        an empty cold cache.
        """
        self.logger.info(f"Initializing: {self.label}")
        if not self.get_devices("humidifier_switches"):
            self.logger.warning(
                f"{self.label}: no humidifier switch selected — nothing to control"
            )
        if not self.get_devices("humidity_sensors"):
            self.logger.warning(
                f"{self.label}: no humidity sensor selected — cannot decide"
            )
        self._register_jobs()

    def shutdown(self) -> None:
        """Remove our scheduler job, then run base cleanup."""
        self._clear_jobs()
        super().shutdown()

    # =========================================================================
    # Event dispatch
    # =========================================================================
    def on_event(self, event: DeviceEvent) -> None:
        """Any subscribed input event (humidity / motion / contact) re-decides.

        The humidifier plug itself is an UNMAPPED category, so its own switch
        events never reach here — there is no echo loop to guard against.
        """
        try:
            if self.is_paused:
                return
            self.logger.debug(
                f"{self.label}: event {event.event_type}={event.value} "
                f"from {event.device_name or event.device_id}"
            )
            self.master()
        except Exception as e:
            self.logger.error(f"{self.label}: on_event failed: {e}", exc_info=True)

    def on_mode_change(self, new_mode: str) -> None:
        """A location-mode change may cross the restricted-modes boundary → re-decide.

        We do not keep manual-override memo state (the humidifier has no
        user-hold convention), so there is nothing to clear — just re-evaluate.
        The restricted-mode check inside master() reads the current mode from
        the ``location_modes`` table, which the webhook router has already
        updated by the time this fires.
        """
        self.logger.info(f"{self.label}: mode → {new_mode}")
        self.master()

    def master(self, **kwargs) -> None:
        """Decide the plug state and apply it. Mirrors the Groovy ``master()``."""
        if self.is_paused:
            return
        try:
            # 1. Restricted-mode gate.
            mode = self._get_current_mode()
            restricted_modes = self.get_setting("restrictedModes", []) or []
            if mode and mode in restricted_modes:
                if self.get_setting("offInRestrictedMode", True):
                    self._apply("off", f"restricted mode ({mode})")
                else:
                    self.logger.debug(
                        f"{self.label}: restricted mode ({mode}) — leaving as-is"
                    )
                return

            # 2. Read the world.
            threshold = int(self.get_setting("humidityThreshold", 50))
            humidity = self._humidity_now()
            motion_active = self._motion_is_active()
            contact_open = self._any_contact_open()

            # 3. Bang-bang decision (identical to the Groovy).
            reasons: List[str] = []
            if humidity >= threshold:
                reasons.append(f"humidity {humidity}% ≥ {threshold}%")
            if not motion_active:
                reasons.append("no motion")
            if contact_open:
                reasons.append("a contact is open")

            if reasons:
                self._apply("off", ", ".join(reasons))
            else:
                self._apply(
                    "on",
                    f"humidity {humidity}% < {threshold}%, room occupied",
                )
        except Exception as e:
            self.logger.error(f"{self.label}: master failed: {e}", exc_info=True)

    # =========================================================================
    # Apply to the humidifier plug(s) — idempotent
    # =========================================================================
    def _apply(self, target: str, reason: str) -> None:
        """Drive every humidifier switch to ``target`` ('on'/'off').

        Reads each plug's authoritative switch state first and only sends a
        command when it actually differs, so repeated evaluations don't spam
        the hub.
        """
        for did in self.get_devices("humidifier_switches"):
            try:
                current = self.get_switch_state(did)
                if current == target:
                    continue  # already there — nothing to do
                result = self.send_command(did, target, verify=True)
                if not result.success or not result.verified:
                    self.logger.warning(
                        f"{self.label}: switch {did} → {target} not verified: "
                        f"{result.error}"
                    )
                    continue
                color = _G if target == "on" else _Y
                self.logger.info(
                    f"{self.label}: humidifier {_C}{did}{_R} → {color}{target}{_R} "
                    f"({reason})"
                )
            except Exception as e:
                self.logger.error(
                    f"{self.label}: switch {did} → {target} failed: {e}",
                    exc_info=True,
                )

    # =========================================================================
    # World-state readers (all event_log source-of-truth, cache fallback)
    # =========================================================================
    def _humidity_now(self) -> int:
        """Current humidity as an int %.

        Takes the max across selected humidity sensors (there is normally one).
        When no sensor has reported a usable value yet, falls back to the
        assumed reading with a throttled warning — same behavior as the Groovy,
        so a brand-new install still runs instead of erroring.
        """
        values: List[float] = []
        for sid in self.get_devices("humidity_sensors"):
            raw = self.get_latest_attribute(sid, "humidity")
            if raw is None:
                state = self.get_device_state(sid) or {}
                attrs = state.get("attributes") or {}
                if isinstance(attrs, dict):
                    raw = attrs.get("humidity")
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                continue

        usable = [v for v in values if v > 0]
        if not usable:
            self._warn_no_reading()
            return self._ASSUMED_HUMIDITY_ON_NO_READING
        return int(max(usable))

    def _warn_no_reading(self) -> None:
        """Emit the 'sensor returned no value' warning, throttled to hourly."""
        now = _time.monotonic()
        last = getattr(self._runtime, "last_no_reading_warn", None)
        if last is not None and (now - last) < self._NO_READING_WARN_INTERVAL_SECS:
            return
        self._runtime.last_no_reading_warn = now
        self.logger.warning(
            f"{self.label}: humidity sensor returned no value yet — running on "
            f"an assumed {self._ASSUMED_HUMIDITY_ON_NO_READING}%. If this "
            f"persists, check the device."
        )

    def _motion_is_active(self) -> bool:
        """True if motion should be considered active (Groovy ``activeMotion``).

        - No motion sensors selected → motion is not a constraint → True.
        - Any sensor currently 'active' → True.
        - All inactive → True while now − (latest motion event) < noMotionTime.
        - No motion events at all (cold device) → True (fail-safe: don't strand
          the humidifier off purely because event history is cold; the user can
          tune noMotionTime). This is the one small deviation from the Groovy's
          cold-start behavior, chosen for safety.
        """
        motion_ids = self.get_devices("motion_sensors")
        if not motion_ids:
            return True

        newest_ts: Optional[datetime] = None
        for sid in motion_ids:
            value, received_at = self._latest_motion(sid)
            if value == "active":
                return True
            ts = self._parse_iso(received_at)
            if ts is not None and (newest_ts is None or ts > newest_ts):
                newest_ts = ts

        if newest_ts is None:
            return True  # no motion history → fail-safe active

        timeout_secs = int(self.get_setting("noMotionTime", 10)) * 60
        age = (datetime.now(timezone.utc) - newest_ts).total_seconds()
        return age < timeout_secs

    def _latest_motion(self, sensor_id) -> tuple[Optional[str], Optional[str]]:
        """Return (value, received_at_iso) of a sensor's most recent motion
        event from event_log, or (None, None)."""
        pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
        try:
            r = requests.get(
                f"{pg}/event_log",
                params={
                    "canonical_device_id": f"eq.{sensor_id}",
                    "event_type": "eq.motion",
                    "select": "event_value,received_at",
                    "order": "received_at.desc",
                    "limit": "1",
                },
                timeout=3,
            )
            if r.status_code == 200 and r.json():
                row = r.json()[0]
                return (
                    (row.get("event_value") or "").lower(),
                    row.get("received_at"),
                )
        except Exception as e:
            self.logger.warning(
                f"{self.label}: motion read failed for {sensor_id}: {e}"
            )
        return (None, None)

    def _any_contact_open(self) -> bool:
        """True if any selected contact sensor is currently 'open'."""
        for cid in self.get_devices("contacts"):
            if self.get_latest_attribute(cid, "contact") == "open":
                return True
        return False

    def _get_current_mode(self) -> Optional[str]:
        """Currently-active location mode from the ``location_modes`` DB table.

        Mirrors AML's DB-as-source-of-truth reader (services.mode_poller keeps
        the table current from the primary hub). Returns None on DB error / no
        active row, in which case the restricted-mode gate simply doesn't fire.
        """
        pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
        try:
            r = requests.get(
                f"{pg}/location_modes",
                params={"is_active": "eq.true", "select": "mode_name", "limit": "1"},
                timeout=2,
            )
            if r.status_code == 200 and r.json():
                return r.json()[0].get("mode_name")
        except Exception as e:
            self.logger.warning(f"{self.label}: mode read failed: {e}")
        return None

    @staticmethod
    def _parse_iso(iso: Optional[str]) -> Optional[datetime]:
        """Parse an ISO8601 timestamp (event_log received_at) to aware UTC."""
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    # =========================================================================
    # Scheduling — a single interval poll drives the safety-net re-evaluation
    # =========================================================================
    def _register_jobs(self) -> None:
        from services.scheduler_service import get_scheduler

        sched = get_scheduler()._scheduler
        self._clear_jobs()
        try:
            sched.add_job(
                func=self.master,
                trigger="interval",
                seconds=self._POLL_SECONDS,
                id=f"humidifier_{self.instance_id}_poll",
                replace_existing=True,
            )
            self.logger.info(
                f"{self.label}: re-evaluation scheduled every {self._POLL_SECONDS}s"
            )
        except Exception as e:
            self.logger.error(
                f"{self.label}: schedule failed: {e}", exc_info=True
            )

    def _clear_jobs(self) -> None:
        try:
            from services.scheduler_service import get_scheduler

            sched = get_scheduler()._scheduler
        except Exception:
            return
        prefix = f"humidifier_{self.instance_id}_"
        for job in list(sched.get_jobs()):
            if job.id.startswith(prefix):
                try:
                    sched.remove_job(job.id)
                except Exception:
                    pass

    # =========================================================================
    # Schema + device categories (drive the generic instance-creation wizard)
    # =========================================================================
    @classmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        from apps.base.pause_settings import UNIVERSAL_PAUSE_SETTINGS

        return {
            "type": "object",
            "properties": {
                **UNIVERSAL_PAUSE_SETTINGS,
                "humidityThreshold": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 100,
                    "default": 50,
                    "title": "Humidity threshold (%)",
                    "description": (
                        "Turn the humidifier OFF at or above this humidity, ON "
                        "below it. This is the target level to maintain."
                    ),
                },
                "noMotionTime": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1440,
                    "default": 10,
                    "title": "Motion timeout (minutes)",
                    "description": (
                        "Only used if you select motion sensor(s) below. Keep "
                        "the humidifier off once the room has had no motion for "
                        "this long."
                    ),
                },
                "restrictedModes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "title": "Restricted modes",
                    "description": (
                        "Location modes in which this app should not run "
                        "(e.g. Away, Night). Leave empty to always run."
                    ),
                },
                "offInRestrictedMode": {
                    "type": "boolean",
                    "default": True,
                    "title": "Turn off in restricted modes",
                    "description": (
                        "When in a restricted mode: if ON, force the humidifier "
                        "OFF; if OFF, leave it untouched (just stop managing it)."
                    ),
                },
            },
        }

    @classmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        return [
            {
                # UNMAPPED output category (not in instance_manager
                # category_events) — the framework must NOT subscribe the plug
                # to its own switch events, or every command echoes back.
                "key": "humidifier_switches",
                "label": "Humidifier plug(s)",
                "capability": "Switch",
                "multiple": True,
                "required": True,
                "description": "The switch/plug the humidifier is plugged into.",
            },
            {
                "key": "humidity_sensors",
                "label": "Humidity sensor",
                "capability": "RelativeHumidityMeasurement",
                "multiple": False,
                "required": True,
                "description": "The sensor whose humidity drives the decision.",
            },
            {
                "key": "motion_sensors",
                "label": "Motion sensor(s)",
                "capability": "MotionSensor",
                "multiple": True,
                "required": False,
                "description": (
                    "Optional. Keep the humidifier off when the room is empty "
                    "(uses the motion timeout above)."
                ),
            },
            {
                "key": "contacts",
                "label": "Contact sensor(s)",
                "capability": "ContactSensor",
                "multiple": True,
                "required": False,
                "description": (
                    "Optional. Turn the humidifier off while any of these "
                    "doors/windows is open."
                ),
            },
        ]
