"""
Coverage for the Rules-app declarative interpreter (Phase 1 of the
agentic-rule-authoring pivot — see
``docs/plans/agentic_rule_authoring_via_user_local_claude_code_cli_and_mobius_mcp_server.md``).

What is pinned here
===================
The pool_button case re-expressed as a ``RuleSpec`` must produce *exactly*
the same observable behaviour as the legacy hand-coded case. These tests
drive the interpreter directly (no Flask, no DB) using a small ``StubHost``
that records every ``send_command`` call and exposes mutable cached device
state.

Scenarios:
    - single tap (pushed)       → both pool-water switches → ``on`` when
                                  starting all-off
    - single tap (pushed)       → both pool-water switches → ``off`` when
                                  starting all-on (no-asymmetry invariant)
    - double tap (doubleTapped) → pump switch independently toggled
    - hold (held)               → set_state off on BOTH categories (pool
                                  water + pump), in declared order
    - button-number filter       → events for button "2" never fire any rule
    - debounce                   → second event of same type within the
                                  window does not produce a second command
    - pause guard                → host.is_paused short-circuits at top of
                                  execute_event and also mid-rule between
                                  primitives
    - empty device category      → trigger never matches; no commands sent
                                  and no exception raised
"""

from __future__ import annotations

import logging
import time as _real_time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Stub host implementing the RuleHost protocol surface
# ---------------------------------------------------------------------------


@dataclass
class _CommandResult:
    success: bool = True
    verified: bool = True
    actual_state: str | None = None
    error: str | None = None


@dataclass
class StubHost:
    """
    Records ``send_command`` invocations and lets the test mutate cached
    device states between events to model "the switch is now on" after a
    successful command.
    """

    categories: Dict[str, List[int]] = field(default_factory=dict)
    states: Dict[int, str] = field(default_factory=dict)   # device_id → 'on'/'off'
    paused: bool = False
    sent: List[Tuple[int, str]] = field(default_factory=list)
    activity_pings: int = 0
    fail_for: set = field(default_factory=set)             # device_ids whose send fails

    # ---- RuleHost protocol ------------------------------------------------
    @property
    def is_paused(self) -> bool:
        return self.paused

    @property
    def logger(self):
        # Real logger so error/info/debug calls work; quiet by default
        return logging.getLogger("test.rules.interpreter")

    def update_last_activity(self) -> None:
        self.activity_pings += 1

    def get_devices(self, category: str) -> List[int]:
        return list(self.categories.get(category, []))

    def get_device_state(self, device_id: int) -> Dict[str, Any] | None:
        state = self.states.get(device_id)
        if state is None:
            return None
        return {"attributes": {"switch": state}}

    def send_command(self, device_id: int, command: str, **kwargs):
        if device_id in self.fail_for:
            return _CommandResult(success=False, error="injected")
        self.sent.append((device_id, command))
        # Reflect the command in the cached state — subsequent toggles see
        # the post-command value, which matches production behaviour.
        self.states[device_id] = command
        return _CommandResult(success=True, verified=True)


def _evt(event_type: str, device_id: int, value: Any | None = None):
    """Construct a minimal DeviceEvent-shaped object."""
    obj = MagicMock()
    obj.event_type = event_type
    obj.device_id = device_id
    obj.value = value
    return obj


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pool_host():
    """Stub host pre-loaded with the pool_button case's three categories."""
    return StubHost(
        categories={
            "trigger_button":      [100],          # one button, canonical id 100
            "pool_water_switches": [200, 201],     # two pool-water switches
            "pump_switch":         [300],          # one pump
        },
        states={
            100: "off",   # button state irrelevant; just present
            200: "off",
            201: "off",
            300: "off",
        },
    )


@pytest.fixture
def pool_spec():
    """The pool_button preset built from schema (button '1', debounce 3s)."""
    from apps.rules.schema import pool_button_preset
    return pool_button_preset(button_number="1", debounce_seconds=3)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_single_tap_off_to_on_drives_both_pool_water_switches(pool_host, pool_spec):
    from apps.rules.interpreter import execute_event
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("pushed", 100, "1"), state)
    # both pool-water switches → 'on'; pump untouched
    assert sorted(pool_host.sent) == [(200, "on"), (201, "on")]


def test_single_tap_all_on_to_off_enforces_no_asymmetry(pool_host, pool_spec):
    from apps.rules.interpreter import execute_event
    pool_host.states[200] = "on"
    pool_host.states[201] = "on"
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("pushed", 100, "1"), state)
    assert sorted(pool_host.sent) == [(200, "off"), (201, "off")]


def test_single_tap_mixed_resolves_to_on(pool_host, pool_spec):
    """toggle_uniform's 'fail safe toward ON' branch when state is asymmetric."""
    from apps.rules.interpreter import execute_event
    pool_host.states[200] = "on"
    pool_host.states[201] = "off"
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("pushed", 100, "1"), state)
    assert sorted(pool_host.sent) == [(200, "on"), (201, "on")]


def test_double_tap_toggles_pump_only(pool_host, pool_spec):
    from apps.rules.interpreter import execute_event
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("doubleTapped", 100, "1"), state)
    assert pool_host.sent == [(300, "on")]


def test_hold_sets_all_off_in_declared_order(pool_host, pool_spec):
    from apps.rules.interpreter import execute_event
    # Pre-set everything ON so the off commands are observable.
    for did in (200, 201, 300):
        pool_host.states[did] = "on"
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("held", 100, "1"), state)
    # Order: pool_water_switches first, then pump_switch, matching the
    # preset's declared action sequence.
    assert pool_host.sent == [(200, "off"), (201, "off"), (300, "off")]


def test_button_number_filter_blocks_phantom_button_2(pool_host, pool_spec):
    """Events for the fabricated phantom button 2 must never fire any rule."""
    from apps.rules.interpreter import execute_event
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("pushed", 100, "2"), state)
    execute_event(pool_host, pool_spec, _evt("doubleTapped", 100, "2"), state)
    execute_event(pool_host, pool_spec, _evt("held", 100, "2"), state)
    assert pool_host.sent == []


def test_debounce_swallows_immediate_duplicate(pool_host, pool_spec):
    from apps.rules.interpreter import execute_event
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("pushed", 100, "1"), state)
    execute_event(pool_host, pool_spec, _evt("pushed", 100, "1"), state)
    # Only the first 'pushed' fires; second one is within the 3s window.
    assert sorted(pool_host.sent) == [(200, "on"), (201, "on")]


def test_debounce_is_per_event_type_not_global(pool_host, pool_spec):
    """A pushed shouldn't debounce a subsequent doubleTapped."""
    from apps.rules.interpreter import execute_event
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("pushed", 100, "1"), state)
    execute_event(pool_host, pool_spec, _evt("doubleTapped", 100, "1"), state)
    assert (300, "on") in pool_host.sent           # pump fired
    assert (200, "on") in pool_host.sent           # pool-water fired too


def test_paused_host_short_circuits_at_entry(pool_host, pool_spec):
    from apps.rules.interpreter import execute_event
    pool_host.paused = True
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("held", 100, "1"), state)
    assert pool_host.sent == []


def test_paused_host_mid_rule_aborts_remaining_actions(pool_host, pool_spec):
    """
    Pause flips between the two set_state primitives of the 'held' rule.
    The first action must NOT have already sent, because the interpreter
    re-checks pause inside _exec_set_state before commanding devices.
    """
    from apps.rules.interpreter import execute_event

    # Wrap get_devices so pause flips on AFTER the first action's device
    # resolution but BEFORE its send_command. Easiest: subclass StubHost.
    class FlipHost(StubHost):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._calls = 0

        def get_devices(self, category):
            ids = super().get_devices(category)
            # The 'held' rule resolves pool_water_switches first, then pump_switch.
            # After pool_water_switches has been resolved AND its commands sent,
            # flip pause on; pump_switch's _exec_set_state must abort.
            return ids

        def send_command(self, device_id, command, **kwargs):
            res = super().send_command(device_id, command, **kwargs)
            self._calls += 1
            if self._calls == 2:    # after the two pool-water sends
                self.paused = True
            return res

    flip = FlipHost(
        categories=pool_host.categories,
        states={k: "on" for k in (200, 201, 300)},  # so off commands are visible
    )
    state: Dict[str, float] = {}
    execute_event(flip, pool_spec, _evt("held", 100, "1"), state)
    # The two pool_water_switches were turned off; pump never reached.
    assert sorted(flip.sent) == [(200, "off"), (201, "off")]


def test_empty_category_makes_trigger_silently_miss(pool_host, pool_spec):
    from apps.rules.interpreter import execute_event
    pool_host.categories["trigger_button"] = []      # idle instance
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("pushed", 100, "1"), state)
    assert pool_host.sent == []


def test_event_device_id_not_in_category_skips_silently(pool_host, pool_spec):
    """A pushed event from a button that ISN'T this instance's trigger_button."""
    from apps.rules.interpreter import execute_event
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("pushed", 999, "1"), state)
    assert pool_host.sent == []


def test_activity_ping_fires_once_per_event(pool_host, pool_spec):
    from apps.rules.interpreter import execute_event
    state: Dict[str, float] = {}
    execute_event(pool_host, pool_spec, _evt("pushed", 100, "1"), state)
    execute_event(pool_host, pool_spec, _evt("doubleTapped", 100, "1"), state)
    execute_event(pool_host, pool_spec, _evt("held", 100, "1"), state)
    assert pool_host.activity_pings == 3
