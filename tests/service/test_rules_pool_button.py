"""
Service-layer coverage for the Rules app, pool_button case, driven through the
REAL current code path (declarative rule interpreter, Phase 1 pivot):

    DeviceEvent → RulesApp.on_event → apps.rules.interpreter.execute_event
                → _dispatch_action → _exec_toggle_uniform / _exec_toggle_independent
                                    / _exec_set_state → host.send_command

Why this file exists alongside tests/unit/test_rules_interpreter.py
===================================================================
The unit suite drives ``interpreter.execute_event`` DIRECTLY with a hand-built
``RuleSpec`` and a ``StubHost`` — it never touches the app shell. This service
suite exercises the shell that wraps the interpreter, which the unit suite does
not cover:

  - ``RulesApp._resolve_spec``  — the LEGACY synthesis path the live pool button
    actually runs. That instance predates Phase 1: it carries no ``rule_spec``,
    only the legacy ``triggerButtonNumber`` / ``debounceSeconds`` settings, so
    the spec is reconstructed on the fly via ``schema.pool_button_preset``.
  - ``RulesApp.initialize``     — seeds ``_runtime.rule_spec`` and
    ``_runtime.debounce_state`` (the state the interpreter reads/writes).
  - ``RulesApp.on_event``       — the BaseApp ``on_event`` bridge + per-instance
    exception boundary.

The real ``initialize`` and ``on_event`` methods are bound onto a mock host and
invoked; assertions check the ACTUAL ``send_command`` calls the interpreter
emits. Nothing here reimplements the toggle/debounce logic — it calls the live
code and inspects its outputs.

Operator spec 2026-06-19 (the contract this file protects)
==========================================================
  - pushed (single tap)       → toggle BOTH pool-water switches to the SAME
                                state: all-on → off; otherwise → on.
  - doubleTapped (double tap) → toggle the pump (verified BOTH directions).
  - held (hold)               → everything off (pool water + pump).
  - only the configured button number is acted on (phantom button 2 ignored).
  - duplicate same-type events within debounceSeconds are dropped.
  - a paused instance ignores the button entirely.

Strictness (operator directive R2 — "test the app AS IT IS; is-not = FAIL"):
each test asserts the EXACT (canonical_id, command) sequence sent against the
operator spec. Any deviation by the app — wrong switch, wrong target state,
missing debounce, a firing paused instance — turns this suite red.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apps.rules.app import RulesApp
from models.event import DeviceEvent


pytestmark = pytest.mark.service


# Canonical device ids for the pool_button slots (strings, matching how the
# device roster resolves them). Module-level so assertions read by name.
BUTTON_ID = "171"
POOL_WATER_IDS = ["95", "106"]
PUMP_ID = "135"


def _make_app(*, switch_states=None, paused=False, button_number="1", debounce=3):
    """
    Build a RulesApp-shaped host that runs the REAL app methods.

    The methods that make up the live pool_button path (``_resolve_spec`` →
    ``initialize`` → ``on_event``) are bound, unbound, onto a MagicMock host,
    then ``initialize()`` is called so the ``RuleSpec`` is synthesised exactly
    as it is for the live legacy instance: no ``rule_spec`` setting → the
    ``pool_button_preset`` reconstructed from the legacy settings.

    Parameters
    ----------
    switch_states : dict[str, 'on'|'off']
        Seed for ``get_device_state``; a missing id reads as unknown (→ off).
    paused : bool
        Value of ``host.is_paused``.
    button_number : str
        Legacy ``triggerButtonNumber`` setting → the value filter on all three
        triggers (phantom-button-2 events are dropped by mismatch).
    debounce : int
        Legacy ``debounceSeconds`` setting → per-event-type debounce window.
    """
    states = {str(k): v for k, v in (switch_states or {}).items()}

    app = MagicMock()
    # Bind the REAL methods — the whole point is to drive current code, not a
    # stub. If any of these is renamed/removed, this test fails loudly (as it
    # should) rather than silently testing nothing.
    for name in ("_resolve_spec", "initialize", "on_event"):
        setattr(app, name, getattr(RulesApp, name).__get__(app))

    app.logger = MagicMock()
    app.label = "Pool Button test"
    app.is_paused = paused
    app.update_last_activity = MagicMock()
    app._runtime = SimpleNamespace()

    # LEGACY settings only (rule_spec absent) → forces the _resolve_spec
    # synthesis path the live pool button runs.
    settings = {
        "rule_spec": None,
        "triggerButtonNumber": button_number,
        "debounceSeconds": debounce,
    }
    app.get_setting = MagicMock(side_effect=lambda k, d=None: settings.get(k, d))

    devices = {
        "pool_water_switches": list(POOL_WATER_IDS),
        "pump_switch": [PUMP_ID],
        "trigger_button": [BUTTON_ID],
    }
    app.get_devices = MagicMock(side_effect=lambda c: devices.get(c, []))

    def _state(cid):
        v = states.get(str(cid))
        return {"attributes": {"switch": v}} if v is not None else None
    app.get_device_state = MagicMock(side_effect=_state)

    # send_command returns a verified-success result by default.
    app.send_command = MagicMock(return_value=SimpleNamespace(
        success=True, verified=True, error=None, actual_state="ok"))

    # Seed _runtime.rule_spec + _runtime.debounce_state exactly as the
    # framework does at instance startup (real code path).
    app.initialize()
    return app


def _evt(event_type, value="1", device_id=BUTTON_ID):
    """Construct a real DeviceEvent for the trigger button."""
    return DeviceEvent(device_id=device_id, event_type=event_type, value=value,
                       device_name="Button Pool")


def _targets(app):
    """List of (canonical_id, command) actually sent, in call order."""
    return [(c.args[0], c.args[1]) for c in app.send_command.call_args_list]


# ---------------------------------------------------------------------------
# Single tap → pool water toggled together, same state (no-asymmetry invariant)
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
    assert all(cid != PUMP_ID for cid, _ in _targets(app))


# ---------------------------------------------------------------------------
# Double tap → pump toggled (both directions — full "toggle" semantics)
# ---------------------------------------------------------------------------


def test_double_tap_pump_off_turns_on():
    app = _make_app(switch_states={"135": "off"})
    app.on_event(_evt("doubleTapped"))
    assert _targets(app) == [("135", "on")]


def test_double_tap_pump_on_turns_off():
    app = _make_app(switch_states={"135": "on"})
    app.on_event(_evt("doubleTapped"))
    assert _targets(app) == [("135", "off")]


def test_double_tap_does_not_touch_pool_water():
    app = _make_app(switch_states={"135": "off"})
    app.on_event(_evt("doubleTapped"))
    assert all(cid not in POOL_WATER_IDS for cid, _ in _targets(app))


# ---------------------------------------------------------------------------
# Hold → everything off (pool water then pump, in declared order)
# ---------------------------------------------------------------------------


def test_hold_turns_everything_off():
    app = _make_app(switch_states={"95": "on", "106": "on", "135": "on"})
    app.on_event(_evt("held"))
    assert _targets(app) == [("95", "off"), ("106", "off"), ("135", "off")]


# ---------------------------------------------------------------------------
# Button-number filter, debounce, pause
# ---------------------------------------------------------------------------


def test_phantom_button_two_ignored():
    """Post-2.5.0.x firmware fabricates a phantom button 2; it must be inert."""
    app = _make_app(switch_states={"95": "off", "106": "off"})
    app.on_event(_evt("pushed", value="2"))
    assert _targets(app) == []


def test_debounce_drops_rapid_duplicate():
    app = _make_app(switch_states={"95": "off", "106": "off"}, debounce=3)
    app.on_event(_evt("pushed"))
    app.on_event(_evt("pushed"))          # within 3s window → dropped
    assert _targets(app) == [("95", "on"), ("106", "on")]


def test_distinct_event_types_not_cross_debounced():
    """A pushed must not debounce a subsequent doubleTapped (per-type window)."""
    app = _make_app(switch_states={"95": "off", "106": "off", "135": "off"})
    app.on_event(_evt("pushed"))
    app.on_event(_evt("doubleTapped"))    # different type → not debounced
    assert ("135", "on") in _targets(app)
    assert ("95", "on") in _targets(app)


def test_paused_instance_ignores_button():
    app = _make_app(switch_states={"95": "off", "106": "off"}, paused=True)
    app.on_event(_evt("pushed"))
    assert _targets(app) == []


def test_non_button_event_ignored():
    """An unrelated event type (no matching trigger) fires nothing."""
    app = _make_app(switch_states={"95": "off", "106": "off"})
    app.on_event(_evt("switch", value="on"))
    assert _targets(app) == []


def test_event_from_other_device_ignored():
    """A pushed from a button that is NOT this instance's trigger is inert."""
    app = _make_app(switch_states={"95": "off", "106": "off"})
    app.on_event(_evt("pushed", device_id="999"))
    assert _targets(app) == []
