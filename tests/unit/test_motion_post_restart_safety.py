"""
Coverage for AML's _is_motion_active() canonical state-based logic.

History note — this file was originally written 2026-05-17 to cover the
"post-restart blind window" bug where lights turned off while the user was
still in the room. That fix added a "defer to True when no data" branch in
the Tier 1/Tier 2 model. On 2026-05-19 the operator directed a canonical
rewrite to per-sensor state semantics (see motion_detection.py docstring):

  - Each sensor has a CURRENT STATE (value of its most recent event).
  - ANY sensor 'active' now → return True (keep on, no window).
  - ALL sensors 'inactive' → off-timer anchor is the transition moment
    per sensor (first 'inactive' AFTER last 'active'); use MAX of those.
  - No data at all → return False. The defer-to-True branch is GONE
    by operator directive. The first incoming 'active' event still trips
    the on-path; the post-restart race window is small in practice because
    event_log retains 24h of history that survives the container restart.

2026-06-16 added the transition-timestamp computation to prevent polled
state-confirmation events (empty src) from re-anchoring the off-timer at
"just now" (the 5 AM lights-on-with-nobody-home bug).

These tests cover the CURRENT semantics. The legacy "defer to True" tests
were removed — they tested a behavior the architecture no longer has.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.unit


def _make_instance(timeout_seconds: int = 1200):
    """Minimal stand-in instance with the real _is_motion_active method bound."""
    from apps.advanced_motion_lighting.motion_detection import (
        MotionDetectionMixin,
    )

    inst = MagicMock()
    inst._is_motion_active = (
        MotionDetectionMixin._is_motion_active.__get__(inst)
    )
    inst._functional_sensors = {"167": True, "63": True, "240": True}
    inst._runtime = SimpleNamespace(last_motion_time=None)
    inst._get_timeout_seconds = lambda: timeout_seconds
    inst.get_setting = MagicMock(return_value=False)
    inst.logger = MagicMock()
    return inst


def _row(*, received_at: str, event_value: str):
    return {"received_at": received_at, "event_value": event_value}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _patched_get(handler):
    """Patch motion_detection.requests.get with a side_effect callable.

    handler receives (url, params) and returns a MagicMock-like response.
    """
    return patch(
        "apps.advanced_motion_lighting.motion_detection.requests.get",
        side_effect=handler,
    )


def _ok_response(json_payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_payload
    return resp


# ---------------------------------------------------------------------------
# Rule 1: any sensor currently 'active' → True
# ---------------------------------------------------------------------------


def test_any_sensor_currently_active_returns_true():
    """A single sensor with a most-recent event of value='active' is enough
    to declare motion-on. The off-timer window does NOT apply when any
    sensor is currently in active state."""
    inst = _make_instance()
    now = datetime.now(timezone.utc)

    def handler(url, params=None, timeout=None):
        sid = params['canonical_device_id'].split('eq.')[-1]
        if sid == "167":
            return _ok_response([_row(received_at=_iso(now - timedelta(minutes=5)),
                                       event_value="active")])
        # The other two sensors don't matter — rule 1 short-circuits.
        return _ok_response([_row(received_at=_iso(now - timedelta(hours=2)),
                                   event_value="inactive")])

    with _patched_get(handler):
        assert inst._is_motion_active() is True


def test_any_sensor_active_event_arbitrarily_old_still_counts():
    """An 'active' state with a 12-hour-old timestamp still means the
    sensor's CURRENT STATE is active (it just hasn't transitioned since).
    The previous "events-in-window" logic would have missed this case;
    the canonical state-based logic catches it."""
    inst = _make_instance()
    long_ago = datetime.now(timezone.utc) - timedelta(hours=12)

    def handler(url, params=None, timeout=None):
        return _ok_response([_row(received_at=_iso(long_ago),
                                   event_value="active")])

    with _patched_get(handler):
        assert inst._is_motion_active() is True


# ---------------------------------------------------------------------------
# Rule 2: all sensors inactive — compute transition anchor, compare to window
# ---------------------------------------------------------------------------


def test_all_inactive_recent_transition_within_window_returns_true():
    """All sensors went inactive 60s ago and timeout is 1200s — within
    window, keep on."""
    inst = _make_instance(timeout_seconds=1200)
    now = datetime.now(timezone.utc)
    transition = now - timedelta(seconds=60)

    def handler(url, params=None, timeout=None):
        event_value_filter = params.get('event_value')
        if event_value_filter is None:
            # Tier-1 per-sensor latest event
            return _ok_response([_row(received_at=_iso(transition),
                                       event_value="inactive")])
        if event_value_filter == 'eq.active':
            # Last active was 10 minutes ago
            return _ok_response([_row(received_at=_iso(now - timedelta(minutes=10)),
                                       event_value="active")])
        if event_value_filter == 'eq.inactive':
            # First inactive after the last active = the transition
            return _ok_response([_row(received_at=_iso(transition),
                                       event_value="inactive")])
        return _ok_response([])

    with _patched_get(handler):
        assert inst._is_motion_active() is True


def test_all_inactive_transition_past_window_returns_false():
    """All sensors went inactive 30 minutes ago and timeout is 20 minutes —
    past window, turn off."""
    inst = _make_instance(timeout_seconds=1200)
    now = datetime.now(timezone.utc)
    transition = now - timedelta(minutes=30)

    def handler(url, params=None, timeout=None):
        event_value_filter = params.get('event_value')
        if event_value_filter is None:
            return _ok_response([_row(received_at=_iso(transition),
                                       event_value="inactive")])
        if event_value_filter == 'eq.active':
            return _ok_response([_row(received_at=_iso(now - timedelta(hours=1)),
                                       event_value="active")])
        if event_value_filter == 'eq.inactive':
            return _ok_response([_row(received_at=_iso(transition),
                                       event_value="inactive")])
        return _ok_response([])

    with _patched_get(handler):
        assert inst._is_motion_active() is False


def test_polled_state_confirmation_does_not_reset_anchor():
    """Regression coverage for the 5 AM lights-on bug (2026-06-16 fix).

    Hubitat emits polled 'state report' inactive events even for sensors
    that have already been inactive for hours. The transition-based query
    must IGNORE those — the off-timer anchor is the FIRST inactive AFTER
    the last active, not the latest 'inactive' row in the table.

    Setup:
      - sensor 167 last active at 22:00:00
      - first inactive after that:  22:08:35 (real transition)
      - polled re-confirmation at 04:55:00 the next morning (NOT a transition)

    Expected: anchor = 22:08:35, age > 20 min window → return False.
    The old max-of-all-inactive logic would have anchored at 04:55:00 and
    erroneously returned True at 05:00 — turning lights on with nobody home.
    """
    inst = _make_instance(timeout_seconds=1200)
    now = datetime.now(timezone.utc)
    last_active = now - timedelta(hours=7)              # 22:00 last night
    real_transition = last_active + timedelta(minutes=8, seconds=35)
    polled_confirm = now - timedelta(minutes=5)         # "just now" polled

    def handler(url, params=None, timeout=None):
        event_value_filter = params.get('event_value')
        if event_value_filter is None:
            # latest event row — could be either the real transition or
            # the polled confirm. The canonical logic relies on the
            # transition lookup, so the latest-event value just needs to
            # say 'inactive' to drop into Rule 2.
            return _ok_response([_row(received_at=_iso(polled_confirm),
                                       event_value="inactive")])
        if event_value_filter == 'eq.active':
            return _ok_response([_row(received_at=_iso(last_active),
                                       event_value="active")])
        if event_value_filter == 'eq.inactive':
            # Query is ASC, limit=1, filter received_at > last_active.
            # The earliest inactive AFTER last_active is the real transition.
            return _ok_response([_row(received_at=_iso(real_transition),
                                       event_value="inactive")])
        return _ok_response([])

    with _patched_get(handler):
        assert inst._is_motion_active() is False


# ---------------------------------------------------------------------------
# Cold cache and fail-safe paths
# ---------------------------------------------------------------------------


def test_cold_event_log_returns_false():
    """Documents the 2026-05-19 directive: when event_log has no rows for
    any subscribed sensor (cold cache / fresh install), return False.

    The earlier 'defer to True' branch was deliberately removed. The first
    incoming 'active' event still trips the on-path immediately, so the
    real-world post-restart race window depends on event_log retaining
    history across the restart (it does — 24h retention by default).
    """
    inst = _make_instance()

    def handler(url, params=None, timeout=None):
        return _ok_response([])  # nothing for anyone

    with _patched_get(handler):
        assert inst._is_motion_active() is False


def test_no_functional_sensors_with_consider_active_setting_returns_true():
    """Fail-safe path: if every sensor is marked non-functional and
    considerActiveWhenFail is on, we assume active (don't strand a
    user in the dark)."""
    inst = _make_instance()
    inst._functional_sensors = {"167": False, "63": False}
    inst.get_setting = MagicMock(side_effect=lambda k, default=None: True
                                 if k == 'considerActiveWhenFail' else default)

    assert inst._is_motion_active() is True


def test_no_functional_sensors_without_setting_returns_false():
    """Fail-safe is off by default — no functional sensors → no light."""
    inst = _make_instance()
    inst._functional_sensors = {"167": False}
    inst.get_setting = MagicMock(return_value=False)

    assert inst._is_motion_active() is False
