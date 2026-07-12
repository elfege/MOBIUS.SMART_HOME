"""
Coverage for the Rules app, pool_button case (apps/rules/app.py).

Behavior under test (operator spec 2026-06-19):
  - pushed (single tap)       → toggle BOTH pool-water switches to the
                                SAME state: all-on → off; otherwise → on.
  - doubleTapped (double tap) → toggle the pump.
  - held (hold)               → everything off.
  - only the configured button number is acted on (phantom button 2 ignored).
  - duplicate same-type events within debounceSeconds are dropped.
  - a paused instance ignores the button entirely.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apps.rules.app import RulesApp
from models.event import DeviceEvent


pytestmark = pytest.mark.service


def _make_app(*, switch_states=None, paused=False, button_number="1",
              debounce=3):
    """Bind the real RulesApp methods onto a MagicMock host.

    switch_states: {canonical_id(str): 'on'|'off'} seed for get_device_state.
    """
    states = {str(k): v for k, v in (switch_states or {}).items()}

    app = MagicMock()
    for name in ('on_event', '_toggle_pool_water', '_toggle_pump', '_all_off',
                 '_switch_is_on', '_set_switches', '_debounced', 'master'):
        setattr(app, name, getattr(RulesApp, name).__get__(app))

    app.logger = MagicMock()
    app.label = "Pool Button test"
    app.is_paused = paused
    app.update_last_activity = MagicMock()
    app._runtime = SimpleNamespace(last_action_monotonic={})

    settings = {"triggerButtonNumber": button_number, "debounceSeconds": debounce}
    app.get_setting = MagicMock(side_effect=lambda k, d=None: settings.get(k, d))

    devices = {
        "pool_water_switches": ["95", "106"],
        "pump_switch": ["135"],
        "trigger_button": ["171"],
    }
    app.get_devices = MagicMock(side_effect=lambda c: devices.get(c, []))

    def _state(cid):
        v = states.get(str(cid))
        return {"attributes": {"switch": v}} if v is not None else None
    app.get_device_state = MagicMock(side_effect=_state)

    # send_command returns a verified-success result by default.
    app.send_command = MagicMock(return_value=SimpleNamespace(
        success=True, verified=True, error=None, actual_state="ok"))
    app._sent = app.send_command
    return app


def _evt(event_type, value="1"):
    return DeviceEvent(device_id="171", event_type=event_type, value=value,
                       device_name="Button Pool")


def _targets(app):
    """List of (canonical_id, command) sent."""
    return [(c.args[0], c.args[1]) for c in app.send_command.call_args_list]


# ---------------------------------------------------------------------------
# Single tap → pool water toggled together, same state
# ---------------------------------------------------------------------------


def test_single_tap_both_off_turns_both_on():
    app = _make_app(switch_states={"95": "off", "106": "off"})
    app.on_event(_evt("pushed"))
    assert _targets(app) == [("95", "on"), ("106", "on")]


def test_single_tap_both_on_turns_both_off():
    app = _make_app(switch_states={"95": "on", "106": "on"})
    app.on_event(_evt("pushed"))
    assert _targets(app) == [("95", "off"), ("106", "off")]


def test_single_tap_asymmetric_resolves_to_both_on():
    """No-asymmetry invariant: one on + one off → both ON (since not ALL on)."""
    app = _make_app(switch_states={"95": "on", "106": "off"})
    app.on_event(_evt("pushed"))
    assert _targets(app) == [("95", "on"), ("106", "on")]


def test_single_tap_does_not_touch_pump():
    app = _make_app(switch_states={"95": "off", "106": "off"})
    app.on_event(_evt("pushed"))
    assert all(cid != "135" for cid, _ in _targets(app))


# ---------------------------------------------------------------------------
# Double tap → pump toggled
# ---------------------------------------------------------------------------


def test_double_tap_pump_off_turns_on():
    app = _make_app(switch_states={"135": "off"})
    app.on_event(_evt("doubleTapped"))
    assert _targets(app) == [("135", "on")]


def test_double_tap_pump_on_turns_off():
    app = _make_app(switch_states={"135": "on"})
    app.on_event(_evt("doubleTapped"))
    assert _targets(app) == [("135", "off")]


# ---------------------------------------------------------------------------
# Hold → everything off
# ---------------------------------------------------------------------------


def test_hold_turns_everything_off():
    app = _make_app(switch_states={"95": "on", "106": "on", "135": "on"})
    app.on_event(_evt("held"))
    assert _targets(app) == [("95", "off"), ("106", "off"), ("135", "off")]


# ---------------------------------------------------------------------------
# Button-number filter, debounce, pause
# ---------------------------------------------------------------------------


def test_phantom_button_two_ignored():
    app = _make_app(switch_states={"95": "off", "106": "off"})
    app.on_event(_evt("pushed", value="2"))
    assert _targets(app) == []


def test_debounce_drops_rapid_duplicate():
    app = _make_app(switch_states={"95": "off", "106": "off"}, debounce=3)
    app.on_event(_evt("pushed"))
    app.on_event(_evt("pushed"))          # within window → dropped
    assert _targets(app) == [("95", "on"), ("106", "on")]


def test_distinct_event_types_not_cross_debounced():
    app = _make_app(switch_states={"95": "off", "106": "off", "135": "off"})
    app.on_event(_evt("pushed"))
    app.on_event(_evt("doubleTapped"))    # different type → not debounced
    assert ("135", "on") in _targets(app)


def test_paused_instance_ignores_button():
    app = _make_app(switch_states={"95": "off", "106": "off"}, paused=True)
    app.on_event(_evt("pushed"))
    assert _targets(app) == []


def test_non_button_event_ignored():
    app = _make_app(switch_states={"95": "off", "106": "off"})
    app.on_event(_evt("switch", value="on"))
    assert _targets(app) == []
