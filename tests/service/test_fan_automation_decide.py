"""
FanAutomationApp._decide() — v2 contract (light-driven + humidity anti-noise).
=============================================================================

Fan Automation was deliberately rewritten to v2 ("operator redesign
2026-06-24", commit aa7e080 "rewrite fan_automation v2 — light-driven"). The
old v1 rule list (exclusion mode / keep-off switch / presence / motion /
alwaysOn default) was REMOVED wholesale — those settings, device categories
and helpers no longer exist in apps/fan_automation/app.py. These tests were
updated to pin the v2 contract as documented in that module's docstring.

v2 model (see apps/fan_automation/app.py:1-29 and :130-221):

  1. Light ON  -> fan = fanWhenLightOn (comfort level). Humidity is IGNORED
     while the light is on; the humidity phase state is cleared.
  2. Light OFF + humid (and a humidity_sensor selected) -> the humidity state
     machine: HIGH (fanWhenHumid) -> QUIET (antiNoiseLevel) -> RAMP (steps
     back up toward fanWhenHumid).
  3. Light OFF + not humid -> keep running for `runAfterLightOff` (action
     'hold' while the run-out timer is live), then apply fanWhenLightOff
     (default 0 == off).

Decision dict shape (apps/fan_automation/app.py:213-221):
    {'action': 'on',  'level': N, 'reason': '...', 'expected': 'on:N'}
    {'action': 'off', 'level': None, 'reason': '...', 'expected': 'off'}
    {'action': 'hold', 'reason': '...'}                # no level / expected
Note: v2 decisions carry NO 'rule' key (v1 did) — assert on 'reason'.

Construction strategy: instantiate FanAutomationApp directly with a synthetic
instance_data and a stub instance_manager (so _save_memoization is a no-op
against the MagicMock — no DB). We override only the two world-sensing
boundary helpers the decision reads — _light_is_on() and _is_humid()
(plus _humidity_now() for the hysteresis tests) — and let the real decision
logic run. Time-based phase transitions are made deterministic by seeding the
memoized phase timestamp to the epoch (1.0), so the elapsed wall-clock is
always far past any threshold.
"""

from unittest.mock import MagicMock

import pytest

from apps.fan_automation.app import FanAutomationApp


def _make_app(*, settings=None, devices=None, memo=None):
    """Build a FanAutomationApp with the bare minimum to call _decide.

    Injects synthetic instance_data + a MagicMock instance_manager (which
    absorbs _save_memoization → no database), then overrides the v2
    world-sensing helpers so each test controls light/humidity state.
    """
    instance_data = {
        "id": 1,
        "label": "Test Fan",
        "settings": settings or {},
        "device_selections": devices or {},
        "memoization_state": memo or {"switch_state": {}},
        "is_paused": False,
    }
    instance_manager = MagicMock()
    app = FanAutomationApp(instance_data, instance_manager)
    # Stub the world-sensing boundary that _decide() consults. Defaults:
    # light off, not humid, no humidity reading. Real decision logic runs.
    app._light_is_on = MagicMock(return_value=False)
    app._is_humid = MagicMock(return_value=False)
    app._humidity_now = MagicMock(return_value=None)
    return app


@pytest.mark.service
class TestPriority:
    """v2 precedence: light-on beats humidity; humidity beats the run-out."""

    def test_light_on_overrides_humidity(self):
        # Light ON short-circuits (app.py:133-138): humidity is ignored even
        # when the sensor reads humid — the fan sits at the comfort level.
        app = _make_app(
            settings={"fanWhenLightOn": 30, "fanWhenHumid": 100},
            devices={"light": [2], "humidity_sensor": [1], "fans": [10]},
        )
        app._light_is_on.return_value = True
        app._is_humid.return_value = True  # would drive humidity if light were off

        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 30                  # fanWhenLightOn, NOT fanWhenHumid
        assert "light on" in d["reason"]

    def test_humidity_overrides_run_out_when_light_off(self):
        # Light OFF + humid routes to the humidity machine (app.py:141-143),
        # superseding the not-humid run-out 'hold' branch (app.py:146-161).
        app = _make_app(
            settings={"fanWhenHumid": 100},
            devices={"light": [2], "humidity_sensor": [1], "fans": [10]},
        )
        app._is_humid.return_value = True

        d = app._decide()
        assert d["action"] == "on"               # not 'hold'
        assert d["level"] == 100                  # fanWhenHumid (fresh HIGH phase)
        assert "humidity high" in d["reason"]

    def test_run_out_holds_before_applying_light_off_level(self):
        # Light OFF + not humid, first evaluation: start the run-out timer and
        # HOLD (keep the fan as-is) rather than immediately dropping to the
        # off level (app.py:146-155). 'hold' here IS the documented intent.
        app = _make_app(
            settings={"runAfterLightOff": 60, "fanWhenLightOff": 0},
            devices={"light": [2], "fans": [10]},
        )
        d = app._decide()
        assert d["action"] == "hold"
        assert "run-out" in d["reason"]


@pytest.mark.service
class TestLightDriven:
    """The fan follows the light: comfort level on, run-out then off-level."""

    def test_light_on_uses_comfort_level(self):
        app = _make_app(
            settings={"fanWhenLightOn": 40},
            devices={"light": [2], "fans": [10]},
        )
        app._light_is_on.return_value = True

        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 40
        assert d["expected"] == "on:40"
        assert "light on" in d["reason"]

    def test_light_on_zero_level_means_off(self):
        # fanWhenLightOn == 0 collapses to an 'off' action (app.py:219-220).
        app = _make_app(
            settings={"fanWhenLightOn": 0},
            devices={"light": [2], "fans": [10]},
        )
        app._light_is_on.return_value = True

        d = app._decide()
        assert d["action"] == "off"
        assert d["level"] is None
        assert "light on" in d["reason"]

    def test_light_off_not_humid_first_eval_holds_for_runout(self):
        app = _make_app(
            settings={"runAfterLightOff": 90, "runAfterLightOffUnit": "Seconds"},
            devices={"light": [2], "fans": [10]},
        )
        d = app._decide()
        assert d["action"] == "hold"
        assert "run-out" in d["reason"]
        # The run-out deadline was armed (app.py:151-154).
        assert app.get_memo("offdelay_until") is not None

    def test_light_off_applies_off_level_after_run_out_elapsed(self):
        # Run-out deadline already in the past -> apply fanWhenLightOff (0 =
        # off) (app.py:156-161). Seed the deadline at the epoch so it is
        # unambiguously elapsed regardless of wall-clock.
        app = _make_app(
            settings={"fanWhenLightOff": 0},
            devices={"light": [2], "fans": [10]},
            memo={"offdelay_until": 1.0, "switch_state": {}},
        )
        d = app._decide()
        assert d["action"] == "off"
        assert "light off" in d["reason"]


@pytest.mark.service
class TestHumidityHysteresis:
    """_is_humid() latches ON at >= threshold, OFF only below (threshold -
    humidityOffset). Prevents oscillation at the boundary (app.py:386-401)."""

    @staticmethod
    def _hyst_app(*, humidity, settings, memo=None):
        """Build an app whose REAL _is_humid() runs against a fixed reading.

        _make_app() blanket-mocks _is_humid; here we delete that instance-
        level mock so the genuine class method (the code under test) executes,
        and feed it a controlled _humidity_now().
        """
        app = _make_app(
            settings=settings,
            devices={"humidity_sensor": [1]},
            memo=memo,
        )
        del app._is_humid  # unmask the real FanAutomationApp._is_humid
        app._humidity_now = MagicMock(return_value=humidity)
        return app

    def test_latches_on_at_or_above_threshold(self):
        app = self._hyst_app(
            humidity=65.0,  # exactly at threshold
            settings={"humidityThreshold": 65, "humidityOffset": 5},
        )
        assert app._is_humid() is True
        assert app.get_memo("humid_latched") is True

    def test_stays_latched_within_offset_band(self):
        # Already latched; 62 is below threshold(65) but at/above (65-5=60),
        # so it stays engaged.
        app = self._hyst_app(
            humidity=62.0,
            settings={"humidityThreshold": 65, "humidityOffset": 5},
            memo={"humid_latched": True, "switch_state": {}},
        )
        assert app._is_humid() is True

    def test_unlatches_below_offset_band(self):
        # Latched, but 59 < (65 - 5 = 60) -> drops out.
        app = self._hyst_app(
            humidity=59.0,
            settings={"humidityThreshold": 65, "humidityOffset": 5},
            memo={"humid_latched": True, "switch_state": {}},
        )
        assert app._is_humid() is False

    def test_does_not_latch_below_threshold_when_never_was(self):
        # Not previously latched; 62 is under threshold(65) and inside the
        # band, so with no prior latch it must NOT engage.
        app = self._hyst_app(
            humidity=62.0,
            settings={"humidityThreshold": 65, "humidityOffset": 5},
            memo={"humid_latched": False, "switch_state": {}},
        )
        assert app._is_humid() is False


@pytest.mark.service
class TestHumidityStateMachine:
    """Light-off + humid drives HIGH -> QUIET -> RAMP (app.py:163-211).

    Phase timestamps are seeded to 1.0 (the epoch) so the elapsed wall-clock
    always exceeds any sustained/hold/interval threshold under test.
    """

    def _humid_app(self, *, settings, memo=None):
        app = _make_app(
            settings=settings,
            devices={"light": [2], "humidity_sensor": [1], "fans": [10]},
            memo=memo,
        )
        app._is_humid.return_value = True  # light already off by default
        return app

    def test_fresh_humid_runs_high(self):
        # No phase yet -> initialize to HIGH and run at fanWhenHumid.
        app = self._humid_app(settings={"fanWhenHumid": 100})

        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 100
        assert "humidity high" in d["reason"]
        assert app.get_memo("hum_phase") == "high"

    def test_sustained_zero_stays_high_indefinitely(self):
        # humiditySustainedMinutes == 0 means "run high until humidity clears"
        # — never quiets, even though the seeded phase_start is ancient
        # (app.py:178-186).
        app = self._humid_app(
            settings={"fanWhenHumid": 100, "humiditySustainedMinutes": 0},
            memo={"hum_phase": "high", "phase_start": 1.0, "switch_state": {}},
        )
        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 100
        assert "humidity high" in d["reason"]

    def test_high_transitions_to_quiet_after_sustained(self):
        # After humiditySustainedMinutes at HIGH -> drop to the quiet level.
        app = self._humid_app(
            settings={
                "fanWhenHumid": 100,
                "humiditySustainedMinutes": 1,
                "antiNoiseLevel": 25,
            },
            memo={"hum_phase": "high", "phase_start": 1.0, "switch_state": {}},
        )
        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 25                    # antiNoiseLevel
        assert "humidity quiet" in d["reason"]
        assert app.get_memo("hum_phase") == "quiet"

    def test_quiet_transitions_to_ramp_after_hold(self):
        # After the quiet hold elapses -> enter RAMP, starting at the quiet
        # level (app.py:188-198).
        app = self._humid_app(
            settings={
                "fanWhenHumid": 100,
                "antiNoiseLevel": 25,
                "antiNoiseHold": 1,
                "antiNoiseHoldUnit": "Minutes",
            },
            memo={"hum_phase": "quiet", "phase_start": 1.0, "switch_state": {}},
        )
        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 25
        assert "humidity ramp start" in d["reason"]
        assert app.get_memo("hum_phase") == "ramp"

    def test_ramp_steps_up_toward_high(self):
        # In RAMP, once the interval elapses, raise by rampStepPercent of
        # fanWhenHumid, capped at fanWhenHumid (app.py:201-211).
        # step = int(100 * 25 / 100) = 25 -> 25 + 25 = 50.
        app = self._humid_app(
            settings={
                "fanWhenHumid": 100,
                "antiNoiseLevel": 25,
                "rampStepPercent": 25,
                "rampIntervalMinutes": 1,
            },
            memo={
                "hum_phase": "ramp",
                "ramp_level": 25,
                "last_ramp": 1.0,
                "switch_state": {},
            },
        )
        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 50
        assert "humidity ramp" in d["reason"]
