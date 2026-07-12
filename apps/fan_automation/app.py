"""
Fan Automation App (v2 — light-driven + humidity anti-noise)
============================================================
Simple model (operator redesign 2026-06-24):

  - The fan follows a LIGHT (the AML-managed light for the room):
      light ON  -> fan = fanWhenLightOn
      light OFF -> keep running for `runAfterLightOff`, then fan = fanWhenLightOff
  - HUMIDITY acts ONLY while the light is OFF (during a shower you're present and
    the fan sits at fanWhenLightOn — never slammed to full; extraction happens
    after you leave). It's a 4-stage controller, so above ~threshold it roars.
    State machine while light-off + humid:
        HIGH  : fan = fanWhenHumid.
                humiditySustainedMinutes == 0 -> stay HIGH until humidity clears.
                                          == N -> after N min -> QUIET.
        QUIET : fan = antiNoiseLevel (low) for antiNoiseHold.  -> RAMP.
        RAMP  : if still humid, ramp up by rampStepPercent of fanWhenHumid every
                rampIntervalMinutes (default 5), capped at fanWhenHumid; stop the
                moment humidity clears.

Memoization (AML convention): per-fan source 'app' | 'manual'. A fan event whose
value differs from what the app last commanded = USER INTERVENED -> mark 'manual'
(respected until the next mode change) AND log it to the cross-app
`dsapp.manual_overrides` table (troubleshooting + future-AI training base). Mode
change clears manual holds, exactly like advanced_motion_lighting.

State (humidity phase timers, manual marks) is DB-backed via _save_memoization.
Local UPnP-free; all device control via the hub command path (send_command).
"""

from __future__ import annotations

import logging
import time as _time
from typing import Any, Dict, List, Optional

from apps.base_app import BaseApp
from models.event import DeviceEvent

logger = logging.getLogger(__name__)

# ANSI colors for log readability
_C = "\033[96m"   # cyan — device names
_Y = "\033[93m"   # yellow — decisions
_G = "\033[92m"   # green — on/speed
_M = "\033[35m"   # magenta — humidity
_R = "\033[0m"    # reset


class FanAutomationApp(BaseApp):
    """Light-driven fan with a humidity anti-noise state machine."""

    TYPE_NAME = 'fan_automation'
    DISPLAY_NAME = 'Fan Automation'
    DESCRIPTION = 'Fan follows a light; humidity (light-off only) with quiet anti-noise control'
    VERSION = '2.0.0'

    # Skip re-sending the same target to a fan within this window (echo/poll guard).
    _APPLY_SAME_TARGET_COOLDOWN_SECS = 2.0
    # Reported-vs-commanded level tolerance before calling it a manual override
    # (4-stage controllers round set levels to stage boundaries).
    _LEVEL_MANUAL_TOLERANCE = 15
    # Background re-evaluation cadence (drives the time-based humidity + off-delay
    # transitions without waiting for a sensor event).
    _POLL_SECONDS = 30

    # =========================================================================
    # Lifecycle
    # =========================================================================
    def initialize(self) -> None:
        """Validate selections, register the poll job. No commands at init —
        wait for the first event/poll to observe the world."""
        self.logger.info(f"Initializing: {self.label}")
        if not self.get_devices('light'):
            self.logger.warning(f"{self.label}: no light selected — fan won't follow anything")
        if not self.get_devices('fans'):
            self.logger.warning(f"{self.label}: no fans selected — nothing to control")
        self._register_jobs()

    def shutdown(self) -> None:
        """Remove our scheduler jobs, then base cleanup."""
        self._clear_jobs()
        super().shutdown()

    # =========================================================================
    # Event dispatch
    # =========================================================================
    def on_event(self, event: DeviceEvent) -> None:
        """Light/humidity events re-decide. Fan events run manual-override
        detection first (a user-set value the app didn't command = intervention)."""
        try:
            if self.is_paused:
                return
            did = str(event.device_id)
            if did in {str(f) for f in self.get_devices('fans')}:
                self._on_fan_event(event)
                return
            # Light or humidity change → re-evaluate.
            self.master()
        except Exception as e:
            self.logger.error(f"{self.label}: on_event failed: {e}", exc_info=True)

    def on_mode_change(self, new_mode: str) -> None:
        """Mode change clears manual fan overrides (AML convention), then re-decide."""
        self.set_memo('last_mode', new_mode)
        memo_switch = self._memoization.setdefault('switch_state', {})
        cleared = 0
        for did, mark in list(memo_switch.items()):
            if mark.get('source') == 'manual':
                del memo_switch[did]
                cleared += 1
        self._save_memoization()
        if cleared:
            self.logger.info(f"{self.label}: mode → {new_mode}: cleared {cleared} manual override(s)")
        self.master()

    def master(self, **kwargs) -> None:
        """Evaluate the decision and apply it to the fans."""
        if self.is_paused:
            return
        decision = self._decide()
        if decision.get('action') == 'hold':
            self.logger.debug(f"{self.label}: hold ({decision.get('reason')})")
            return
        self._apply(decision)

    # =========================================================================
    # Decision (light-driven + humidity state machine)
    # =========================================================================
    def _decide(self) -> Dict[str, Any]:
        """Return {action:'on'|'off'|'hold', level, reason, expected}. Advances
        the humidity phase state (persisted) as a side effect."""
        # 1. Light ON → comfort level; humidity ignored; clear humidity phase.
        if self._light_is_on():
            self._clear_hum_phase()
            self.set_memo('offdelay_until', None)
            self._save_memoization()
            return self._level_decision(self.get_setting('fanWhenLightOn', 30), 'light on')

        # 2. Light OFF. Humidity only acts here.
        humid = self._is_humid()
        if humid and self.get_devices('humidity_sensor'):
            return self._humidity_decision()

        # 3. Light OFF + not humid → run-out then off.
        self._clear_hum_phase()
        now = _time.time()
        od_until = self.get_memo('offdelay_until')
        if od_until is None:
            # Just entered the not-humid/light-off state → start the run-out timer.
            delay = self._to_seconds(self.get_setting('runAfterLightOff', 60),
                                     self.get_setting('runAfterLightOffUnit', 'Seconds'))
            self.set_memo('offdelay_until', now + delay)
            self._save_memoization()
            return {'action': 'hold', 'reason': f'run-out {int(delay)}s after light off'}
        if now < od_until:
            return {'action': 'hold', 'reason': 'run-out timer running'}
        # Timer elapsed → apply the light-off level (0 = off).
        self.set_memo('offdelay_until', None)
        self._save_memoization()
        return self._level_decision(self.get_setting('fanWhenLightOff', 0), 'light off')

    def _humidity_decision(self) -> Dict[str, Any]:
        """The HIGH → QUIET → RAMP state machine (light-off + humid)."""
        now = _time.time()
        self.set_memo('offdelay_until', None)  # humidity supersedes the run-out
        phase = self.get_memo('hum_phase')
        high = self.get_setting('fanWhenHumid', 100)

        if phase not in ('high', 'quiet', 'ramp'):
            phase = 'high'
            self.set_memo('hum_phase', 'high')
            self.set_memo('phase_start', now)
            self._save_memoization()
            self.logger.info(f"{self.label}: {_M}humid → HIGH ({high}){_R}")

        if phase == 'high':
            sustained = int(self.get_setting('humiditySustainedMinutes', 15))
            if sustained > 0 and (now - (self.get_memo('phase_start') or now)) >= sustained * 60:
                self.set_memo('hum_phase', 'quiet')
                self.set_memo('phase_start', now)
                self._save_memoization()
                low = self.get_setting('antiNoiseLevel', 25)
                self.logger.info(f"{self.label}: {_M}humid HIGH {sustained}min → QUIET ({low}){_R}")
                return self._level_decision(low, 'humidity quiet')
            return self._level_decision(high, 'humidity high')

        if phase == 'quiet':
            hold = self._to_seconds(self.get_setting('antiNoiseHold', 30),
                                    self.get_setting('antiNoiseHoldUnit', 'Minutes'))
            if (now - (self.get_memo('phase_start') or now)) >= hold:
                low = self.get_setting('antiNoiseLevel', 25)
                self.set_memo('hum_phase', 'ramp')
                self.set_memo('ramp_level', low)
                self.set_memo('last_ramp', now)
                self._save_memoization()
                self.logger.info(f"{self.label}: {_M}quiet over → RAMP from {low}{_R}")
                return self._level_decision(low, 'humidity ramp start')
            return self._level_decision(self.get_setting('antiNoiseLevel', 25), 'humidity quiet')

        # phase == 'ramp'
        step = max(1, int(high * int(self.get_setting('rampStepPercent', 25)) / 100))
        interval = int(self.get_setting('rampIntervalMinutes', 5)) * 60
        rl = int(self.get_memo('ramp_level') or self.get_setting('antiNoiseLevel', 25))
        if rl < high and (now - (self.get_memo('last_ramp') or now)) >= interval:
            rl = min(high, rl + step)
            self.set_memo('ramp_level', rl)
            self.set_memo('last_ramp', now)
            self._save_memoization()
            self.logger.info(f"{self.label}: {_M}ramp → {rl}{_R}")
        return self._level_decision(rl, 'humidity ramp')

    def _level_decision(self, level, reason: str) -> Dict[str, Any]:
        """Build a decision dict from a 0–100 level (0 = off)."""
        try:
            level = int(level)
        except (TypeError, ValueError):
            level = 0
        if level <= 0:
            return {'action': 'off', 'level': None, 'reason': reason, 'expected': 'off'}
        return {'action': 'on', 'level': level, 'reason': reason, 'expected': f'on:{level}'}

    # =========================================================================
    # Manual-override detection + logging
    # =========================================================================
    def _on_fan_event(self, event: DeviceEvent) -> None:
        """A fan reported a new value. If it differs from what the app last
        commanded, the user intervened → mark 'manual' (respected until mode
        change) and log it. Otherwise it's our own echo → ignore."""
        fan_id = str(event.device_id)
        expected = (self._memoization.get('expected', {}) or {}).get(fan_id)
        actual = self._event_token(event)
        if expected is None:
            return  # app never commanded this fan yet — nothing to compare
        if self._tokens_match(expected, actual):
            return  # echo of our own command

        # User intervention.
        memo_switch = self._memoization.setdefault('switch_state', {})
        memo_switch[fan_id] = {'state': 'on' if actual.startswith('on') else 'off',
                               'source': 'manual'}
        self._save_memoization()
        self.logger.info(f"{self.label}: {_Y}manual override on fan {fan_id}{_R} "
                         f"(app wanted {expected}, user set {actual})")
        self._log_override(event, expected, actual)

    def _log_override(self, event: DeviceEvent, expected: str, actual: str) -> None:
        """Record the override to the cross-app log with a rich ML context."""
        try:
            from services.manual_override_log import record_override
            context = {
                'light_on': self._light_is_on(),
                'humidity': self._humidity_now(),
                'hum_phase': self.get_memo('hum_phase'),
                'fanWhenLightOn': self.get_setting('fanWhenLightOn', 30),
                'fanWhenLightOff': self.get_setting('fanWhenLightOff', 0),
                'fanWhenHumid': self.get_setting('fanWhenHumid', 100),
                'humidityThreshold': self.get_setting('humidityThreshold', 65),
            }
            record_override(
                instance_id=self.instance_id, app_type=self.TYPE_NAME,
                device_id=event.device_id, device_label=getattr(event, 'display_name', None),
                attribute=getattr(event, 'name', None),
                expected=expected, actual=actual,
                location_mode=self.get_memo('last_mode'),
                context=context,
            )
        except Exception as e:
            self.logger.warning(f"{self.label}: override-log failed: {e}")

    @staticmethod
    def _event_token(event: DeviceEvent) -> str:
        """Compact token for a fan event: 'off' or 'on:<level>'."""
        name = str(getattr(event, 'name', '') or '').lower()
        val = str(getattr(event, 'value', '') or '').lower()
        if name == 'switch':
            return 'off' if val == 'off' else 'on:?'
        if name in ('level', 'speed'):
            try:
                return f'on:{int(float(val))}'
            except (TypeError, ValueError):
                return 'on:?'
        return f'{name}:{val}'

    def _tokens_match(self, expected: str, actual: str) -> bool:
        """True if a reported token is consistent with the commanded one
        (level within tolerance; '?' level matches any on)."""
        if expected == actual:
            return True
        e_on, a_on = expected.startswith('on'), actual.startswith('on')
        if e_on != a_on:
            return False  # on vs off — definite mismatch
        if not e_on:
            return True   # both off
        # both on — compare levels with tolerance; '?' = unknown level, accept.
        try:
            el = expected.split(':', 1)[1]
            al = actual.split(':', 1)[1]
            if el == '?' or al == '?':
                return True
            return abs(int(el) - int(al)) <= self._LEVEL_MANUAL_TOLERANCE
        except (IndexError, ValueError):
            return True

    # =========================================================================
    # Apply to fans (reused from v1 — idempotent, respects manual marks)
    # =========================================================================
    def _apply(self, decision: Dict[str, Any]) -> None:
        """Send commands to fans; memoize source='app' + expected target."""
        memo_switch = self._memoization.setdefault('switch_state', {})
        expected_map = self._memoization.setdefault('expected', {})
        last_apply = getattr(self._runtime, 'last_apply_by_fan', None)
        if last_apply is None:
            last_apply = {}
            self._runtime.last_apply_by_fan = last_apply

        action = decision['action']
        level = decision.get('level')
        target = decision['expected']
        now_mono = _time.monotonic()

        for fan_id in self.get_devices('fans'):
            fid = str(fan_id)
            mark = memo_switch.get(fid, {})
            if mark.get('source') == 'manual':
                self.logger.debug(f"{self.label}: skip fan {fid} — manual override")
                continue

            if action == 'off':
                cmd, args = 'off', None
            else:
                if bool(level) and self._fan_supports_level(fan_id):
                    cmd, args = 'setLevel', [int(level)]
                else:
                    cmd, args = 'on', None

            prev = last_apply.get(fid)
            if prev and prev[0] == target and (now_mono - prev[1]) < self._APPLY_SAME_TARGET_COOLDOWN_SECS:
                continue  # idempotency: same target just applied

            try:
                result = self.send_command(fan_id, cmd, args=args, verify=True)
                if not result.success or not result.verified:
                    self.logger.warning(f"{self.label}: fan {fid} {cmd} not verified: {result.error}")
                    continue
                memo_switch[fid] = {'state': 'off' if action == 'off' else 'on', 'source': 'app'}
                expected_map[fid] = target
                last_apply[fid] = (target, now_mono)
                self.logger.info(f"{self.label}: fan {_C}{fid}{_R} → {_G}{cmd}"
                                 f"{f' {level}' if level and action == 'on' else ''}{_R}"
                                 f"  ({decision['reason']})")
            except Exception as e:
                self.logger.error(f"{self.label}: fan {fid} {cmd} failed: {e}", exc_info=True)
        self._save_memoization()

    def _fan_supports_level(self, device_id) -> bool:
        """True if the fan exposes SwitchLevel (else commands degrade to on/off)."""
        state = self.get_device_state(device_id) or {}
        for c in (state.get('capabilities', []) or []):
            if (isinstance(c, str) and c == 'SwitchLevel') or \
               (isinstance(c, dict) and c.get('name') == 'SwitchLevel'):
                return True
        return False

    # =========================================================================
    # World-state helpers
    # =========================================================================
    def _read_attr(self, device_id, attr: str):
        state = self.get_device_state(device_id) or {}
        return (state.get('attributes', {}) or {}).get(attr)

    def _light_is_on(self) -> bool:
        return any(self._read_attr(lid, 'switch') == 'on' for lid in self.get_devices('light'))

    def _humidity_now(self) -> Optional[float]:
        """Max humidity reading across humidity sensors, or None."""
        vals = []
        for sid in self.get_devices('humidity_sensor'):
            v = self._read_attr(sid, 'humidity')
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                continue
        return max(vals) if vals else None

    def _is_humid(self) -> bool:
        """Hysteresis: latch on at >= threshold, off at < (threshold − offset)."""
        h = self._humidity_now()
        if h is None:
            return False
        threshold = float(self.get_setting('humidityThreshold', 65))
        offset = float(self.get_setting('humidityOffset', 5))
        latched = bool(self.get_memo('humid_latched'))
        if h >= threshold:
            latched = True
        elif h < (threshold - offset):
            latched = False
        if latched != bool(self.get_memo('humid_latched')):
            self.set_memo('humid_latched', latched)
            self._save_memoization()
        return latched

    def _clear_hum_phase(self) -> None:
        for k in ('hum_phase', 'phase_start', 'ramp_level', 'last_ramp'):
            self.set_memo(k, None)

    @staticmethod
    def _to_seconds(value, unit: str) -> int:
        try:
            n = float(value)
        except (TypeError, ValueError):
            return 0
        u = str(unit or 'Seconds').lower()
        if u.startswith('hour'):
            return int(n * 3600)
        if u.startswith('min'):
            return int(n * 60)
        return int(n)

    # =========================================================================
    # Scheduling — a single poll drives the time-based transitions
    # =========================================================================
    def _register_jobs(self) -> None:
        from services.scheduler_service import get_scheduler
        sched = get_scheduler()._scheduler
        self._clear_jobs()
        try:
            sched.add_job(func=self.master, trigger='interval', seconds=self._POLL_SECONDS,
                          id=f"fan_{self.instance_id}_poll", replace_existing=True)
            self.logger.info(f"{self.label}: poll scheduled every {self._POLL_SECONDS}s")
        except Exception as e:
            self.logger.error(f"{self.label}: poll schedule failed: {e}", exc_info=True)

    def _clear_jobs(self) -> None:
        try:
            from services.scheduler_service import get_scheduler
            sched = get_scheduler()._scheduler
        except Exception:
            return
        prefix = f"fan_{self.instance_id}_"
        for job in list(sched.get_jobs()):
            if job.id.startswith(prefix):
                try:
                    sched.remove_job(job.id)
                except Exception:
                    pass

    # =========================================================================
    # Schema + device categories
    # =========================================================================
    @classmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        from apps.base.pause_settings import UNIVERSAL_PAUSE_SETTINGS
        return {
            "type": "object",
            "properties": {
                **UNIVERSAL_PAUSE_SETTINGS,

                "fanWhenLightOn": {
                    "type": "integer", "minimum": 0, "maximum": 100, "default": 30,
                    "title": "Fan speed while the light is ON",
                    "description": "0–100 (0 = off). Used whenever the light is on.",
                },
                "fanWhenLightOff": {
                    "type": "integer", "minimum": 0, "maximum": 100, "default": 0,
                    "title": "Fan speed after the light is OFF",
                    "description": "0 = off. Applied once the run-out time below elapses (when not humid).",
                },
                "runAfterLightOff": {
                    "type": "integer", "minimum": 0, "maximum": 3600, "default": 60,
                    "title": "Keep running after light off",
                    "description": "How long to keep the fan as-is after the light turns off "
                                   "before applying the off level (when not humid).",
                },
                "runAfterLightOffUnit": {
                    "type": "string", "enum": ["Seconds", "Minutes"], "default": "Seconds",
                    "title": "Run-out unit",
                },

                "humidityThreshold": {
                    "type": "integer", "minimum": 0, "maximum": 100, "default": 65,
                    "title": "Humidity % to run the fan high",
                    "description": "Above this (while the light is off) the fan runs high to extract moisture.",
                },
                "humidityOffset": {
                    "type": "integer", "minimum": 0, "maximum": 20, "default": 5,
                    "title": "Humidity tolerance (%)",
                    "description": "Stays 'humid' until it drops below (threshold − this), so brief dips don't reset timers.",
                },
                "fanWhenHumid": {
                    "type": "integer", "minimum": 1, "maximum": 100, "default": 100,
                    "title": "High fan speed when humid",
                },
                "humiditySustainedMinutes": {
                    "type": "integer", "minimum": 0, "maximum": 240, "default": 15,
                    "title": "Run high for (minutes) before quieting",
                    "description": "0 = run high until humidity clears (never quiet). "
                                   ">0 = after this long at high, drop to the quiet level.",
                },
                "antiNoiseLevel": {
                    "type": "integer", "minimum": 0, "maximum": 100, "default": 25,
                    "title": "Quiet (anti-noise) fan speed",
                    "description": "Low speed used during the quiet period after sustained high humidity.",
                },
                "antiNoiseHold": {
                    "type": "integer", "minimum": 1, "maximum": 240, "default": 30,
                    "title": "Quiet period length",
                },
                "antiNoiseHoldUnit": {
                    "type": "string", "enum": ["Minutes", "Hours"], "default": "Minutes",
                    "title": "Quiet period unit",
                },
                "rampStepPercent": {
                    "type": "integer", "minimum": 5, "maximum": 100, "default": 25,
                    "title": "Ramp step (% of high speed)",
                    "description": "After the quiet period, if still humid, raise the fan by this "
                                   "fraction of the high speed each step (25% ≈ one stage on a 4-stage fan).",
                },
                "rampIntervalMinutes": {
                    "type": "integer", "minimum": 1, "maximum": 60, "default": 5,
                    "title": "Ramp step interval (minutes)",
                    "description": "Increase the fan one step this often during the ramp, up to high speed.",
                },
            },
        }

    @classmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "fans", "label": "Fan(s)",
                "capability": "Switch",
                "multiple": True, "required": True,
                "description": "The fan switch(es). Variable speed via SwitchLevel preferred.",
            },
            {
                "key": "light", "label": "Light (the trigger)",
                "capability": "Switch",
                "multiple": False, "required": True,
                "description": "The room light (usually AML-managed). The fan follows its on/off.",
            },
            {
                "key": "humidity_sensor", "label": "Humidity sensor",
                "capability": "RelativeHumidityMeasurement",
                "multiple": True, "required": False,
                "description": "Optional. Enables humidity extraction (only while the light is off).",
            },
        ]
