"""
Fan Automation App
==================

Multi-rule fan speed/on-off control. Each rule is independently toggleable
in the instance settings; conflicts are resolved by a fixed priority
(highest wins) so two enabled rules can't fight each other:

    1. Mode in `exclusionModes` ........................ fans OFF (hard override)
    2. Manual fan-level override switch currently ON ... fan @ manualOverrideOnLevel
       (replaces the old "keep_off" rule; the override switch is a two-state
       device the user toggles directly — see rule 6 for the OFF behavior.)
    3. Humidity over threshold (with hysteresis) ....... fans ON @ humidityFanLevel
       — INTENTIONALLY above presence/motion: humidity is a moisture-damage /
         mold concern that should run regardless of who's home.
       — SKIPPED during the humidity-suppress window opened by the override
         switch transitioning ON→OFF (see rule 6).
    4. Presence rule (runWhenHome / runWhenAway):
         - If the rule says don't run, fans OFF.
    5. Motion rule:
         - Motion in last `motionTimeoutSeconds`  → fans ON @ motionActiveLevel
         - Otherwise                              → fans ON @ motionInactiveLevel
       Inverse of motion lighting: higher fan when room empty (less noise
       concern), quieter when someone is present.
    6. Default:
         - Inside the humidity-suppress window (the override switch just
           went OFF) → fan @ manualOverrideOffBurstLevel.
         - Otherwise → fan @ alwaysOnLevel.

The master() decision is re-evaluated on:
    - every device webhook for a subscribed device (humidity / motion /
      presence / fan switch / manual override switch),
    - mode changes,
    - timeout expiry (motion-inactive timer),
    - resume from pause.

Foolproofing:
    - If a rule is enabled but its sensor category is empty, the rule
      is silently skipped (logged once) — instance still runs the
      remaining rules.
    - Manual switch overrides on fans are detected via memoization
      (source='manual' vs 'app') and respected until mode change.
    - useDim=true requires the fan switches to advertise `SwitchLevel`;
      if a fan only supports `Switch`, level commands degrade to plain
      on/off (the level is ignored at the device).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from apps.base_app import BaseApp
from models.event import DeviceEvent

logger = logging.getLogger(__name__)


# ANSI colors for log readability (mirrors advanced_motion_lighting/constants.py)
_C = "\033[96m"   # cyan — device names
_Y = "\033[93m"   # yellow — warnings / decisions
_G = "\033[92m"   # green — speed-up / on
_M = "\033[35m"   # magenta — humidity
_R = "\033[0m"    # reset


class FanAutomationApp(BaseApp):
    """Multi-rule fan automation. See module docstring for priority order."""

    TYPE_NAME    = 'fan_automation'
    DISPLAY_NAME = 'Fan Automation'
    DESCRIPTION  = 'Speed control by humidity, motion, presence; rule-priority foolproofing'
    VERSION      = '1.0.0'

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def initialize(self) -> None:
        """Validate settings → log skips → no device commands at init."""
        self.logger.info(f"Initializing: {self.label}")

        # Sanity-check toggleable rules vs available sensor categories.
        if self.get_setting('humidityEnabled', False):
            if not self.get_devices('humidity_sensors'):
                self.logger.warning(
                    "humidityEnabled=true but no humidity_sensors selected "
                    "— humidity rule will be skipped"
                )
        if self.get_setting('motionEnabled', False):
            if not self.get_devices('motion_sensors'):
                self.logger.warning(
                    "motionEnabled=true but no motion_sensors selected "
                    "— motion rule will be skipped"
                )
        if self.get_setting('presenceEnabled', False):
            if not self.get_devices('presence_sensors'):
                self.logger.warning(
                    "presenceEnabled=true but no presence_sensors selected "
                    "— presence rule will be skipped"
                )

        # No master() at init: deciding fan state before observing the
        # current world state (humidity reading, motion-active timestamp,
        # presence) would just do something arbitrary. Wait for the
        # first event or scheduled poll.

    # =========================================================================
    # Event dispatch
    # =========================================================================

    def on_event(self, event: DeviceEvent) -> None:
        """
        Route the incoming event by type. After any state-changing event,
        re-evaluate master() so the fan reflects current world state.
        """
        try:
            if self.is_paused:
                self.logger.debug(f"Paused, ignoring event: {event}")
                return

            self.update_last_activity()

            etype = event.event_type
            if etype == 'humidity':
                self._on_humidity(event)
            elif etype == 'motion':
                self._on_motion(event)
            elif etype == 'presence':
                self._on_presence(event)
            elif etype == 'switch':
                self._on_switch(event)
            else:
                # Unknown / unsubscribed event_type — should not happen
                # if subscriptions are correct, but log + ignore is safe.
                self.logger.debug(f"Ignoring unhandled event type: {etype}")
                return

            self.master()
        except Exception as e:
            self.logger.error(
                f"on_event failed: {event}: {e}", exc_info=True
            )

    # ------------------------------------------------------------------ events

    def _on_humidity(self, event: DeviceEvent) -> None:
        """Cache the latest humidity reading per device for the rule check."""
        try:
            value = float(event.value)
        except (TypeError, ValueError):
            return
        self._runtime.humidity_by_device = getattr(
            self._runtime, 'humidity_by_device', {}
        )
        self._runtime.humidity_by_device[event.device_id] = value
        self.logger.debug(
            f"Humidity {_M}{event.device_name}{_R}: {value}%"
        )

    def _on_motion(self, event: DeviceEvent) -> None:
        """Cache last-active time + reschedule timeout on inactive."""
        if event.is_motion_active:
            self._runtime.last_motion_time = datetime.now(timezone.utc)
            self.logger.debug(f"Motion active: {_C}{event.device_name}{_R}")
        else:
            timeout = int(self.get_setting('motionTimeoutSeconds', 300))
            self.schedule_timeout(timeout)
            self.logger.debug(
                f"Motion inactive: {_C}{event.device_name}{_R} "
                f"(timeout {timeout}s)"
            )

    def _on_presence(self, event: DeviceEvent) -> None:
        """Cache presence per device; master() reads `someone_home`."""
        self._runtime.presence_by_device = getattr(
            self._runtime, 'presence_by_device', {}
        )
        # Hubitat presence values: 'present' / 'not present'
        self._runtime.presence_by_device[event.device_id] = (event.value == 'present')
        self.logger.debug(
            f"Presence {_C}{event.device_name}{_R}: {event.value}"
        )

    def _on_switch(self, event: DeviceEvent) -> None:
        """
        Two paths:

        1. Event from a manual-fan-level-override switch: track the previous
           state; on an ON→OFF transition open a humidity-suppress window
           (master() will see the timer and skip rule 3 + use the burst
           level as the default for the duration). master() then re-runs
           naturally and the cascade picks up the new state.

        2. Event from one of the controlled fans: detect manual-vs-app
           changes and update the memoization source — master() respects
           'manual' overrides until a mode change clears them.
        """
        fan_ids  = set(self.get_devices('fans'))
        override = set(self.get_devices('manual_fan_level_override_switches'))
        if event.device_id in override:
            # Track previous state per device so we can detect a real ON→OFF
            # edge. Stored on memoization (survives within an instance run).
            last_states = self._memoization.setdefault(
                'override_switch_last_state', {}
            )
            prev = last_states.get(str(event.device_id))
            last_states[str(event.device_id)] = event.value
            if prev == 'on' and event.value == 'off':
                n = int(self.get_setting(
                    'manualOverrideHumiditySuppressSeconds', 60
                ))
                self._runtime.humidity_suppress_until = (
                    datetime.now(timezone.utc) + timedelta(seconds=n)
                )
                self.logger.info(
                    f"Manual override {_C}{event.device_name}{_R} ON→OFF: "
                    f"opening humidity-suppress window for {_Y}{n}s{_R} "
                    f"(default level becomes manualOverrideOffBurstLevel)"
                )
            # Don't touch fan memo — master() reads override state live.
            return
        if event.device_id not in fan_ids:
            return
        # If the value matches what we last set, it's our own command echo.
        memo = self._memoization.setdefault('switch_state', {})
        last = memo.get(str(event.device_id))
        if last and last.get('state') == event.value and last.get('source') == 'app':
            # Echo of our last command — don't flip to manual.
            return
        memo[str(event.device_id)] = {'state': event.value, 'source': 'manual'}
        self.logger.info(
            f"Manual override on fan {_C}{event.device_name}{_R}: {event.value}"
        )

    # =========================================================================
    # Master decision
    # =========================================================================

    def master(self, **kwargs) -> None:
        """
        Decide and execute the fan state. See module docstring for priority.
        """
        try:
            if self.is_paused:
                return

            decision = self._decide()
            self.logger.debug(
                f"master decision: {decision['action']} "
                f"(level={decision.get('level')}, reason={decision['reason']})"
            )
            self._apply(decision)
        except Exception as e:
            self.logger.error(f"master() failed: {e}", exc_info=True)

    def _decide(self) -> Dict[str, Any]:
        """
        Return one of:
            {action: 'off',          reason: '...'}
            {action: 'on', level: N, reason: '...'}
        """
        # -- Rule 1: exclusion mode --
        excl = self.get_setting('exclusionModes', []) or []
        current_mode = self._get_current_mode()
        if current_mode and current_mode in excl:
            return {'action': 'off', 'reason': f'mode={current_mode} in exclusionModes'}

        # -- Rule 2: manual fan-level override switch currently ON --
        # ON dictates a specific level (above humidity / motion / presence).
        # OFF is handled in rule 6 via the humidity-suppress window timer
        # — there is no "off action" path on this rule.
        for sw in self.get_devices('manual_fan_level_override_switches'):
            if self._read_switch_state(sw) == 'on':
                level = int(self.get_setting('manualOverrideOnLevel', 50))
                return {
                    'action': 'on',
                    'level': level,
                    'reason': f'manual override switch {sw} is ON',
                    'rule': 'manual_override_on',
                }

        # -- Rule 3: humidity safety override (skipped during the suppress window) --
        # The override switch ON→OFF transition opens an N-second window
        # during which we skip humidity (see _on_switch). After the window,
        # humidity is re-eligible.
        humidity_suppressed = self._humidity_suppressed_now()
        if (
            self.get_setting('humidityEnabled', False)
            and not humidity_suppressed
            and self.get_devices('humidity_sensors')
        ):
            humidity = self._max_humidity()
            threshold = float(self.get_setting('humidityThreshold', 60))
            hysteresis = float(self.get_setting('humidityHysteresis', 5))
            currently_running_humidity = (
                self._memoization.get('rule_in_effect') == 'humidity'
            )
            # Hysteresis: once we cross threshold UP, stay engaged until we
            # drop below (threshold - hysteresis). Prevents oscillation
            # when the reading hovers around the threshold.
            engage = (humidity is not None) and (
                humidity >= threshold
                or (currently_running_humidity and humidity >= (threshold - hysteresis))
            )
            if engage:
                level = int(self.get_setting('humidityFanLevel', 100))
                return {
                    'action': 'on',
                    'level': level,
                    'reason': f'humidity={humidity:.1f} >= {threshold}',
                    'rule': 'humidity',
                }

        # -- Rule 4: presence --
        if self.get_setting('presenceEnabled', False) and self.get_devices('presence_sensors'):
            mode = self.get_setting('presenceMode', 'runWhenHome')
            someone_home = self._someone_home()
            if mode == 'runWhenHome' and not someone_home:
                return {'action': 'off', 'reason': 'runWhenHome + nobody home'}
            if mode == 'runWhenAway' and someone_home:
                return {'action': 'off', 'reason': 'runWhenAway + someone home'}
            # mode == 'runOnlyWhenHome' is a stricter alias of runWhenHome
            if mode == 'runOnlyWhenHome' and not someone_home:
                return {'action': 'off', 'reason': 'runOnlyWhenHome + nobody home'}

        # -- Rule 5: motion --
        if self.get_setting('motionEnabled', False) and self.get_devices('motion_sensors'):
            timeout = int(self.get_setting('motionTimeoutSeconds', 300))
            # System-level floor enforcement (cascade tier 3). PIR re-trigger
            # cooldown is typically 10-60s; values below this cause oscillation
            # of the fan rule. bypassTimeoutFloor=true on the instance skips it.
            if not self.get_setting('bypassTimeoutFloor', False):
                try:
                    from services.settings_resolver import get_resolver
                    floor = get_resolver().get_system(
                        'motion_timeout_floor_seconds', 60
                    )
                    if isinstance(floor, (int, float)) and floor > 0 and timeout < floor:
                        self.logger.info(
                            f"FanAutomation motionTimeoutSeconds clamped "
                            f"{timeout}s → {int(floor)}s (system floor)"
                        )
                        timeout = int(floor)
                except Exception as e:
                    self.logger.warning(
                        f"FanAutomation floor lookup failed, using raw "
                        f"{timeout}s: {e}"
                    )
            if self._motion_active_within(timeout):
                level = int(self.get_setting('motionActiveLevel', 30))
                return {
                    'action': 'on', 'level': level,
                    'reason': f'motion within {timeout}s', 'rule': 'motion',
                }
            level = int(self.get_setting('motionInactiveLevel', 100))
            return {
                'action': 'on', 'level': level,
                'reason': f'no motion in {timeout}s', 'rule': 'motion',
            }

        # -- Rule 6: default --
        # Inside the humidity-suppress window (override switch just went
        # OFF), the burst level replaces alwaysOnLevel as the default. The
        # window expires after manualOverrideHumiditySuppressSeconds and
        # the regular alwaysOnLevel resumes.
        if self._humidity_suppressed_now():
            burst = int(self.get_setting('manualOverrideOffBurstLevel', 100))
            return {
                'action': 'on',
                'level': burst,
                'reason': 'manual override OFF burst (humidity suppress window)',
                'rule': 'manual_override_off_burst',
            }
        return {
            'action': 'on',
            'level': int(self.get_setting('alwaysOnLevel', 100)),
            'reason': 'default (no rule override)',
            'rule': 'default',
        }

    # =========================================================================
    # Decision helpers
    # =========================================================================

    def _max_humidity(self) -> Optional[float]:
        """Return highest current humidity reading across cached sensors,
        falling back to a live cache read for sensors we haven't seen yet."""
        cache = getattr(self._runtime, 'humidity_by_device', {}) or {}
        readings = list(cache.values())
        if not readings:
            # First-time fall-through: try the device cache for whatever
            # we have, even if the value is stale.
            for did in self.get_devices('humidity_sensors'):
                state = self.get_device_state(did)
                if not state:
                    continue
                attrs = state.get('attributes', {}) or {}
                v = attrs.get('humidity')
                try:
                    readings.append(float(v))
                except (TypeError, ValueError):
                    continue
        return max(readings) if readings else None

    def _someone_home(self) -> bool:
        """Any presence sensor reporting 'present' counts as someone home."""
        cache = getattr(self._runtime, 'presence_by_device', {}) or {}
        if cache:
            return any(cache.values())
        # Fall through: read from device cache.
        for did in self.get_devices('presence_sensors'):
            state = self.get_device_state(did)
            if not state:
                continue
            attrs = state.get('attributes', {}) or {}
            if attrs.get('presence') == 'present':
                return True
        return False

    def _motion_active_within(self, seconds: int) -> bool:
        """True if any motion sensor reports / has reported active recently."""
        last = self._runtime.last_motion_time
        # last_motion_time is tz-aware (set via datetime.now(timezone.utc)
        # in _on_motion). Mixing naive datetime.now() here would TypeError.
        if last and (datetime.now(timezone.utc) - last).total_seconds() < seconds:
            return True
        # Live fallback: any sensor currently reporting active?
        for did in self.get_devices('motion_sensors'):
            state = self.get_device_state(did)
            if not state:
                continue
            attrs = state.get('attributes', {}) or {}
            if attrs.get('motion') == 'active':
                return True
        return False

    def _get_current_mode(self) -> Optional[str]:
        """Best-effort current Hubitat location mode."""
        try:
            import requests, os
            r = requests.get(
                f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/location_modes",
                params={"is_active": "eq.true", "select": "mode_name", "limit": "1"},
                timeout=3,
            )
            if r.status_code == 200 and r.json():
                return r.json()[0].get("mode_name")
        except Exception:
            pass
        return None

    def _humidity_suppressed_now(self) -> bool:
        """
        True while the post-override-OFF humidity-suppress window is open.
        The window is opened by _on_switch on every ON→OFF transition of
        any manual_fan_level_override switch (timestamp on _runtime).
        """
        until = getattr(self._runtime, 'humidity_suppress_until', None)
        if until is None:
            return False
        return datetime.now(timezone.utc) < until

    def _read_switch_state(self, device_id) -> Optional[str]:
        """Read 'on'/'off' state for a switch — cache first, attribute extract."""
        state = self.get_device_state(device_id)
        if not state:
            return None
        attrs = state.get('attributes', {}) or {}
        v = attrs.get('switch')
        return v if v in ('on', 'off') else None

    # =========================================================================
    # Apply
    # =========================================================================

    def _apply(self, decision: Dict[str, Any]) -> None:
        """Send commands to fans. Memoize source='app' on success."""
        rule = decision.get('rule', 'default')
        self._memoization['rule_in_effect'] = rule
        memo_switch = self._memoization.setdefault('switch_state', {})

        action = decision['action']
        level = decision.get('level')

        for fan_id in self.get_devices('fans'):
            # Respect manual override until next mode change clears memo.
            mark = memo_switch.get(str(fan_id), {})
            if mark.get('source') == 'manual':
                self.logger.debug(
                    f"Skip fan {fan_id} — manual override "
                    f"({mark.get('state')})"
                )
                continue

            if action == 'off':
                cmd = 'off'
                args = None
            else:
                # Variable speed: prefer setLevel when supported, else 'on'.
                use_level = bool(level) and self._fan_supports_level(fan_id)
                if use_level:
                    cmd, args = 'setLevel', [int(level)]
                else:
                    cmd, args = 'on', None

            try:
                result = self.send_command(fan_id, cmd, args=args, verify=True)
                if not result.success or not result.verified:
                    self.logger.warning(
                        f"Fan {fan_id} {cmd} not verified: "
                        f"err={result.error} expected={result.expected_state} "
                        f"actual={result.actual_state}"
                    )
                    continue
                memo_switch[str(fan_id)] = {
                    'state': 'off' if action == 'off' else 'on',
                    'source': 'app',
                }
                self.logger.info(
                    f"Fan {_C}{fan_id}{_R} → {_G}{cmd}{_R}"
                    f"{f' lvl={level}' if level and action == 'on' else ''}"
                    f"  ({decision['reason']})"
                )
            except Exception as e:
                self.logger.error(
                    f"Fan {fan_id} {cmd} failed: {e}", exc_info=True
                )

    def _fan_supports_level(self, device_id) -> bool:
        """True if the fan exposes the SwitchLevel capability."""
        state = self.get_device_state(device_id)
        if not state:
            return False
        caps = state.get('capabilities', []) or []
        # capabilities may be list of strings or list of {name: ...}
        for c in caps:
            if isinstance(c, str) and c == 'SwitchLevel':
                return True
            if isinstance(c, dict) and c.get('name') == 'SwitchLevel':
                return True
        return False

    # =========================================================================
    # Mode change override clear
    # =========================================================================

    def on_mode_change(self, new_mode: str) -> None:
        """Mode change clears manual overrides — same convention as
        advanced_motion_lighting. Then re-decide."""
        memo_switch = self._memoization.setdefault('switch_state', {})
        cleared = 0
        for did, mark in list(memo_switch.items()):
            if mark.get('source') == 'manual':
                del memo_switch[did]
                cleared += 1
        if cleared:
            self.logger.info(
                f"Mode → {new_mode}: cleared {cleared} manual fan override(s)"
            )
        self.master()

    # =========================================================================
    # Schema
    # =========================================================================

    @classmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # Humidity
                "humidityEnabled": {
                    "type": "boolean", "default": False,
                    "title": "Use humidity rule",
                    "description": "Run fans at humidityFanLevel when any humidity sensor exceeds the threshold",
                },
                "humidityThreshold": {
                    "type": "integer", "minimum": 0, "maximum": 100, "default": 60,
                    "title": "Humidity threshold (%)",
                },
                "humidityHysteresis": {
                    "type": "integer", "minimum": 0, "maximum": 20, "default": 5,
                    "title": "Hysteresis (%)",
                    "description": "Don't disengage until humidity drops below (threshold − hysteresis)",
                },
                "humidityFanLevel": {
                    "type": "integer", "minimum": 1, "maximum": 100, "default": 100,
                    "title": "Fan level when humidity over",
                },

                # Motion
                "motionEnabled": {
                    "type": "boolean", "default": False,
                    "title": "Use motion rule",
                    "description": "Lower fan speed when motion present, raise when no motion",
                },
                "motionTimeoutSeconds": {
                    "type": "integer", "minimum": 30, "maximum": 7200, "default": 300,
                    "title": "Motion-inactive timeout (s)",
                },
                "motionActiveLevel": {
                    "type": "integer", "minimum": 1, "maximum": 100, "default": 30,
                    "title": "Speed when motion present",
                },
                "motionInactiveLevel": {
                    "type": "integer", "minimum": 1, "maximum": 100, "default": 100,
                    "title": "Speed when no motion",
                },

                # Presence
                "presenceEnabled": {
                    "type": "boolean", "default": False,
                    "title": "Use presence rule",
                },
                "presenceMode": {
                    "type": "string",
                    "enum": ["runWhenHome", "runWhenAway", "runOnlyWhenHome"],
                    "default": "runWhenHome",
                    "title": "Presence mode",
                },

                # Mode exclusion
                "exclusionModes": {
                    "type": "array", "items": {"type": "string"}, "default": [],
                    "title": "Exclusion modes",
                    "description": "Hubitat location modes in which fans must stay OFF",
                },

                # Default
                "alwaysOnLevel": {
                    "type": "integer", "minimum": 1, "maximum": 100, "default": 100,
                    "title": "Default fan speed",
                    "description": "Used when no other rule applies",
                },

                # Manual fan-level override (the device category formerly known
                # as "keep_off_switches"). The override switch is a two-state
                # device: ON dictates the active level (rule 2); ON→OFF opens
                # a humidity-suppress window and the burst level becomes the
                # default for that window's duration.
                "manualOverrideOnLevel": {
                    "type": "integer", "minimum": 1, "maximum": 100, "default": 50,
                    "title": "Manual override ON → fan level",
                    "description": (
                        "Fan level forced when any manual override switch is ON. "
                        "Translates to fan speed on FanControl devices, level on "
                        "SwitchLevel devices, or plain ON for switch-only devices."
                    ),
                },
                "manualOverrideOffBurstLevel": {
                    "type": "integer", "minimum": 1, "maximum": 100, "default": 100,
                    "title": "Manual override OFF burst → fan level",
                    "description": (
                        "Fan level used as the default during the humidity-suppress "
                        "window that opens when an override switch transitions "
                        "ON→OFF. Other rules (motion / presence) still apply if "
                        "their triggers fire during the window."
                    ),
                },
                "manualOverrideHumiditySuppressSeconds": {
                    "type": "integer", "minimum": 1, "maximum": 3600, "default": 60,
                    "title": "Manual override OFF → humidity-suppress window (s)",
                    "description": (
                        "Duration of the humidity-suppress window opened by an "
                        "override switch transitioning ON→OFF. The humidity rule "
                        "is skipped for this many seconds; after the window the "
                        "regular cascade resumes including humidity."
                    ),
                },
            },
        }

    @classmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "fans", "label": "Fans",
                "capability": "Switch",
                "multiple": True, "required": True,
                "description": "Fan switches (variable speed via SwitchLevel preferred)",
            },
            {
                "key": "humidity_sensors", "label": "Humidity Sensors",
                "capability": "RelativeHumidityMeasurement",
                "multiple": True, "required": False,
                "description": "Optional — used by the humidity rule",
            },
            {
                "key": "motion_sensors", "label": "Motion Sensors",
                "capability": "MotionSensor",
                "multiple": True, "required": False,
                "description": "Optional — used by the motion rule",
            },
            {
                "key": "presence_sensors", "label": "Presence Sensors",
                "capability": "PresenceSensor",
                "multiple": True, "required": False,
                "description": "Optional — used by the presence rule",
            },
            {
                "key": "manual_fan_level_override_switches",
                "label": "Manual Fan Level Override Switches (two-state)",
                "capability": "Switch",
                "multiple": True, "required": False,
                "description": (
                    "Optional. Two-state switches the user toggles directly. "
                    "When ANY is ON → fan is forced to manualOverrideOnLevel "
                    "(above humidity / motion / presence). When ANY transitions "
                    "ON→OFF → opens a humidity-suppress window for "
                    "manualOverrideHumiditySuppressSeconds, during which the "
                    "burst level (manualOverrideOffBurstLevel) is the default "
                    "and the humidity rule is skipped."
                ),
            },
        ]
