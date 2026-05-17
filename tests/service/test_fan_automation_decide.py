"""
FanAutomation._decide() resolves the final action+level for the fan based
on a priority list of rules:

  1. exclusionMode → off
  2. keep_off_switch on → off
  3. humidity ≥ threshold (with hysteresis) → on @ humidityFanLevel
  4. presence rule (runWhenHome / runWhenAway / runOnlyWhenHome)
  5. motion (active → motionActiveLevel; inactive → motionInactiveLevel)
  6. default → alwaysOnLevel

These tests pin down the priority order and each rule's branch logic.

Construction strategy: we instantiate FanAutomationApp directly with the
minimum required instance_data, and override the runtime helpers
(_get_current_mode, _read_switch_state, _max_humidity, _someone_home,
_motion_active_within) to control the world state per test. _decide is a
pure function over those inputs once we control them.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from apps.fan_automation.app import FanAutomationApp


def _make_app(*, settings=None, devices=None, memo=None):
    """Build a FanAutomationApp with the bare minimum to call _decide.

    We bypass the heavy mixin __init__ logic by injecting a synthetic
    instance_data and a stub instance_manager. The runtime helpers we
    override directly on the instance after construction.
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
    # Stub external IO that _decide may touch
    app._get_current_mode = MagicMock(return_value=None)
    app._read_switch_state = MagicMock(return_value="off")
    app._max_humidity = MagicMock(return_value=None)
    app._someone_home = MagicMock(return_value=False)
    app._motion_active_within = MagicMock(return_value=False)
    return app


@pytest.mark.service
class TestRulePriority:
    """Higher-priority rule wins even when lower-priority rule would trigger."""

    def test_rule1_exclusion_mode_overrides_all(self):
        app = _make_app(
            settings={
                "exclusionModes": ["Sleeping"],
                "humidityEnabled": True,
                "humidityThreshold": 60,
                "presenceEnabled": True,
                "motionEnabled": True,
                "alwaysOnLevel": 100,
            },
            devices={
                "humidity_sensors": [1],
                "presence_sensors": [2],
                "motion_sensors": [3],
            },
        )
        app._get_current_mode.return_value = "Sleeping"
        app._max_humidity.return_value = 80  # would trigger humidity rule
        app._someone_home.return_value = True
        app._motion_active_within.return_value = True

        d = app._decide()
        assert d["action"] == "off"
        assert "exclusionModes" in d["reason"]

    def test_rule2_keep_off_switch_overrides_humidity(self):
        app = _make_app(
            settings={
                "humidityEnabled": True,
                "humidityThreshold": 60,
                "humidityFanLevel": 100,
            },
            devices={
                "keep_off_switches": [99],
                "humidity_sensors": [1],
            },
        )
        # keep_off switch reports 'on'
        app._read_switch_state = MagicMock(
            side_effect=lambda did: "on" if did == 99 else "off"
        )
        app._max_humidity.return_value = 80

        d = app._decide()
        assert d["action"] == "off"
        assert "keep_off" in d["reason"]

    def test_rule3_humidity_overrides_presence_and_motion(self):
        app = _make_app(
            settings={
                "humidityEnabled": True,
                "humidityThreshold": 60,
                "humidityFanLevel": 80,
                "presenceEnabled": True,
                "presenceMode": "runWhenHome",
                "motionEnabled": True,
            },
            devices={
                "humidity_sensors": [1],
                "presence_sensors": [2],
                "motion_sensors": [3],
            },
        )
        app._max_humidity.return_value = 75
        app._someone_home.return_value = False  # would force off via presence
        app._motion_active_within.return_value = False

        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 80
        assert d["rule"] == "humidity"

    def test_rule5_motion_overrides_default(self):
        app = _make_app(
            settings={
                "motionEnabled": True,
                "motionActiveLevel": 30,
                "motionInactiveLevel": 90,
                "motionTimeoutSeconds": 300,
                "alwaysOnLevel": 100,
            },
            devices={"motion_sensors": [3]},
        )
        app._motion_active_within.return_value = True

        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 30
        assert d["rule"] == "motion"

    def test_rule6_default_when_no_other_rules_apply(self):
        app = _make_app(
            settings={"alwaysOnLevel": 75},
            devices={"fans": [10]},
        )
        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 75
        assert d["rule"] == "default"


@pytest.mark.service
class TestHumidityHysteresis:
    """Once humidity engaged the fan, fan stays on until humidity drops below
    threshold - hysteresis (default 5). Prevents oscillation at the boundary."""

    def test_engages_at_or_above_threshold(self):
        app = _make_app(
            settings={
                "humidityEnabled": True,
                "humidityThreshold": 60,
                "humidityHysteresis": 5,
                "humidityFanLevel": 100,
            },
            devices={"humidity_sensors": [1]},
        )
        app._max_humidity.return_value = 60.0

        d = app._decide()
        assert d["action"] == "on"
        assert d["rule"] == "humidity"

    def test_stays_engaged_in_hysteresis_band(self):
        # Engaged previously (memoized) → 58 still engaged (60 - 5 = 55)
        app = _make_app(
            settings={
                "humidityEnabled": True,
                "humidityThreshold": 60,
                "humidityHysteresis": 5,
                "humidityFanLevel": 100,
            },
            devices={"humidity_sensors": [1]},
            memo={"rule_in_effect": "humidity"},
        )
        app._max_humidity.return_value = 58.0

        d = app._decide()
        assert d["action"] == "on"
        assert d["rule"] == "humidity"

    def test_disengages_below_hysteresis_band(self):
        app = _make_app(
            settings={
                "humidityEnabled": True,
                "humidityThreshold": 60,
                "humidityHysteresis": 5,
                "humidityFanLevel": 100,
                "alwaysOnLevel": 50,
            },
            devices={"humidity_sensors": [1]},
            memo={"rule_in_effect": "humidity"},
        )
        app._max_humidity.return_value = 54.0  # below 55

        d = app._decide()
        # Falls through humidity → no other rule enabled → default
        assert d["rule"] == "default"
        assert d["level"] == 50

    def test_does_not_engage_below_threshold_when_never_was(self):
        app = _make_app(
            settings={
                "humidityEnabled": True,
                "humidityThreshold": 60,
                "humidityHysteresis": 5,
                "alwaysOnLevel": 50,
            },
            devices={"humidity_sensors": [1]},
            memo={"rule_in_effect": "default"},  # not currently humidity
        )
        app._max_humidity.return_value = 58.0

        d = app._decide()
        assert d["rule"] == "default"


@pytest.mark.service
class TestPresenceRule:
    def test_run_when_home_off_when_away(self):
        app = _make_app(
            settings={
                "presenceEnabled": True,
                "presenceMode": "runWhenHome",
            },
            devices={"presence_sensors": [1]},
        )
        app._someone_home.return_value = False

        d = app._decide()
        assert d["action"] == "off"

    def test_run_when_away_off_when_someone_home(self):
        app = _make_app(
            settings={
                "presenceEnabled": True,
                "presenceMode": "runWhenAway",
                "alwaysOnLevel": 50,
            },
            devices={"presence_sensors": [1]},
        )
        app._someone_home.return_value = True

        d = app._decide()
        assert d["action"] == "off"

    def test_only_when_home_falls_through_to_default_when_someone_home(self):
        # When mode = runOnlyWhenHome AND someone is home: presence rule
        # doesn't force off, so we fall through to the next rule. With no
        # motion enabled, we hit the default rule.
        app = _make_app(
            settings={
                "presenceEnabled": True,
                "presenceMode": "runOnlyWhenHome",
                "alwaysOnLevel": 75,
            },
            devices={"presence_sensors": [1]},
        )
        app._someone_home.return_value = True

        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 75


@pytest.mark.service
class TestMotionRule:
    def test_motion_active_uses_active_level(self):
        app = _make_app(
            settings={
                "motionEnabled": True,
                "motionActiveLevel": 25,
                "motionInactiveLevel": 75,
                "motionTimeoutSeconds": 300,
            },
            devices={"motion_sensors": [3]},
        )
        app._motion_active_within.return_value = True

        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 25
        assert d["rule"] == "motion"

    def test_motion_inactive_uses_inactive_level(self):
        # Inverse semantic: full speed when nobody around
        app = _make_app(
            settings={
                "motionEnabled": True,
                "motionActiveLevel": 25,
                "motionInactiveLevel": 100,
                "motionTimeoutSeconds": 300,
            },
            devices={"motion_sensors": [3]},
        )
        app._motion_active_within.return_value = False

        d = app._decide()
        assert d["action"] == "on"
        assert d["level"] == 100
        assert d["rule"] == "motion"

    def test_motion_disabled_falls_through_to_default(self):
        app = _make_app(
            settings={
                "motionEnabled": False,
                "alwaysOnLevel": 60,
            },
            devices={"motion_sensors": [3]},
        )
        d = app._decide()
        assert d["rule"] == "default"

    def test_motion_enabled_but_no_sensors_falls_through(self):
        app = _make_app(
            settings={
                "motionEnabled": True,
                "alwaysOnLevel": 60,
            },
            devices={},  # no motion sensors selected
        )
        d = app._decide()
        assert d["rule"] == "default"
