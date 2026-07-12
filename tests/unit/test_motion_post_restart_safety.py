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
    # Bind the real stuck-sensor helpers too — _is_motion_active calls them,
    # and an unbound MagicMock would make the threshold comparison a
    # MagicMock>MagicMock TypeError.
    inst._stuck_active_seconds = (
        MotionDetectionMixin._stuck_active_seconds.__get__(inst)
    )
    inst._active_onset_age_seconds = (
        MotionDetectionMixin._active_onset_age_seconds.__get__(inst)
    )
    # _is_motion_active now delegates to these extracted helpers; bind the
    # real methods so they don't resolve to auto-MagicMocks.
    inst._gather_per_sensor_state = (
        MotionDetectionMixin._gather_per_sensor_state.__get__(inst)
    )
    inst._currently_active_nonstuck = (
        MotionDetectionMixin._currently_active_nonstuck.__get__(inst)
    )
    inst._latest_inactive_transition_iso = (
        MotionDetectionMixin._latest_inactive_transition_iso.__get__(inst)
    )
    inst.off_timer_status = (
        MotionDetectionMixin.off_timer_status.__get__(inst)
    )
    inst.DEFAULT_STUCK_ACTIVE_SECONDS = (
        MotionDetectionMixin.DEFAULT_STUCK_ACTIVE_SECONDS
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


def test_sparse_active_within_stuck_threshold_still_counts():
    """A sensor holding 'active' with sparse events (the GE sensor stays
    active for ~14 min between re-reports) still means its CURRENT STATE is
    active — as long as the continuous-active run is shorter than the stuck
    threshold. This is the original 2026-05-19 concern (events-in-window logic
    missed the still-active-between-events case).

    SUPERSEDES the earlier `test_any_sensor_active_event_arbitrarily_old_still_counts`,
    which used a 12-HOUR-old active event and asserted it still counts. Per the
    operator's stuck-sensor directive (2026-07-05), an unbroken 'active' run
    longer than DEFAULT_STUCK_ACTIVE_SECONDS is now a dead/latched sensor and is
    ignored — see test_stuck_active_sensor_is_ignored."""
    inst = _make_instance()
    now = datetime.now(timezone.utc)
    active_since = now - timedelta(minutes=14)     # well under the 4h threshold
    prior_inactive = now - timedelta(minutes=20)

    def handler(url, params=None, timeout=None):
        event_value_filter = params.get('event_value')
        if event_value_filter is None:
            return _ok_response([_row(received_at=_iso(active_since),
                                       event_value="active")])
        if event_value_filter == 'eq.inactive':
            return _ok_response([_row(received_at=_iso(prior_inactive),
                                       event_value="inactive")])
        if event_value_filter == 'eq.active':
            return _ok_response([_row(received_at=_iso(active_since),
                                       event_value="active")])
        return _ok_response([])

    with _patched_get(handler):
        assert inst._is_motion_active() is True


def test_stuck_active_sensor_is_ignored():
    """A sensor jammed 'active' with no transition for longer than the stuck
    threshold (a dead/latched PIR — canon 235 was stuck 'active' 37h) is
    treated as FAILED and excluded from Rule 1. As the sole sensor with no
    other signal, motion resolves to False so the room's lights can finally
    time out. The sensor is also flagged non-functional for _health_check."""
    inst = _make_instance()
    inst._functional_sensors = {"167": True}
    now = datetime.now(timezone.utc)
    stuck_since = now - timedelta(hours=40)        # >> 4h; never went inactive

    def handler(url, params=None, timeout=None):
        event_value_filter = params.get('event_value')
        if event_value_filter == 'eq.inactive':
            return _ok_response([])                 # never inactive → stuck
        # latest-state query and active-onset query both see the old active
        return _ok_response([_row(received_at=_iso(stuck_since),
                                   event_value="active")])

    with _patched_get(handler):
        assert inst._is_motion_active() is False
    assert inst._functional_sensors["167"] is False


def test_stuck_sensor_ignored_but_healthy_active_keeps_on():
    """One stuck sensor must not poison the decision: a co-located HEALTHY
    sensor that is genuinely active still keeps the lights on, and only the
    stuck one is flagged non-functional."""
    inst = _make_instance()
    inst._functional_sensors = {"167": True, "63": True}
    now = datetime.now(timezone.utc)
    stuck_since = now - timedelta(hours=40)
    healthy_active = now - timedelta(minutes=3)
    healthy_prior_inactive = now - timedelta(minutes=10)

    def handler(url, params=None, timeout=None):
        sid = params['canonical_device_id'].split('eq.')[-1]
        event_value_filter = params.get('event_value')
        if sid == "167":                            # stuck sensor
            if event_value_filter == 'eq.inactive':
                return _ok_response([])
            return _ok_response([_row(received_at=_iso(stuck_since),
                                       event_value="active")])
        # sid 63 — healthy, currently active since 3 min ago
        if event_value_filter == 'eq.inactive':
            return _ok_response([_row(received_at=_iso(healthy_prior_inactive),
                                       event_value="inactive")])
        return _ok_response([_row(received_at=_iso(healthy_active),
                                   event_value="active")])

    with _patched_get(handler):
        assert inst._is_motion_active() is True
    assert inst._functional_sensors["167"] is False   # stuck flagged
    assert inst._functional_sensors["63"] is True      # healthy preserved


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


# ---------------------------------------------------------------------------
# off_timer_status(): the UI countdown source — shares the anchor with
# _is_motion_active so the displayed countdown can't diverge from master().
# ---------------------------------------------------------------------------


def test_off_timer_status_active_has_no_countdown():
    """When a sensor is genuinely active, off_timer_status reports is_active
    True and no anchor — the UI shows "staying on", not a ticking countdown."""
    inst = _make_instance()
    inst._functional_sensors = {"167": True}
    now = datetime.now(timezone.utc)

    def handler(url, params=None, timeout=None):
        event_value_filter = params.get('event_value')
        if event_value_filter == 'eq.inactive':
            return _ok_response([_row(received_at=_iso(now - timedelta(minutes=20)),
                                       event_value="inactive")])
        # latest state and onset both see a recent active (not stuck)
        return _ok_response([_row(received_at=_iso(now - timedelta(minutes=5)),
                                   event_value="active")])

    with _patched_get(handler):
        status = inst.off_timer_status()
    assert status['is_active'] is True
    assert status['off_anchor_iso'] is None


def test_off_timer_status_inactive_anchors_on_transition():
    """When the room is quiet, off_timer_status reports is_active False and the
    anchor = the inactive TRANSITION (first inactive after last active) — the
    SAME anchor _is_motion_active counts the timeout from."""
    inst = _make_instance()
    inst._functional_sensors = {"167": True}
    now = datetime.now(timezone.utc)
    transition = now - timedelta(seconds=90)

    def handler(url, params=None, timeout=None):
        event_value_filter = params.get('event_value')
        if event_value_filter is None:
            return _ok_response([_row(received_at=_iso(transition),
                                       event_value="inactive")])
        if event_value_filter == 'eq.active':
            return _ok_response([_row(received_at=_iso(now - timedelta(minutes=10)),
                                       event_value="active")])
        if event_value_filter == 'eq.inactive':
            return _ok_response([_row(received_at=_iso(transition),
                                       event_value="inactive")])
        return _ok_response([])

    with _patched_get(handler):
        status = inst.off_timer_status()
    assert status['is_active'] is False
    assert status['off_anchor_iso'] == _iso(transition)
