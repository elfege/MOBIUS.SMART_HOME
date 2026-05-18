"""
Regression coverage for AML's post-restart "blind window" — the case
that caused lights to turn off at 22:28:37 EDT on 2026-05-17 while the
user was actively in the office.

Sequence the bug requires:
  1. Container restarts (in-memory `last_motion_time` reset to None).
  2. AML's `aml_init_master_delay_seconds` (default 5s) fires first
     master() before any new motion event has arrived.
  3. `_is_motion_active()` runs:
     - Tier 1 (in-memory last_motion_time) → None, skipped.
     - Tier 2 (event_log query) → no rows in window (either because
       the table is empty or because the prune just ran).
  4. Previous code returned False → master() decided "no motion" →
     turned off lights while user was in the room.

The defensive fix: when Tier 1 has *no data at all* (last_motion_time
is None) AND Tier 2 returns nothing, return True. We have no evidence
of motion-inactive, only absence of data — and absence of evidence is
not evidence of absence. Real motion-inactive transitions arrive via
the event handler which arms `schedule_timeout()`; that path remains
the authoritative "no motion → turn off" signal.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.unit


# Minimal stand-in instance with just what _is_motion_active needs.
def _make_instance():
    from apps.advanced_motion_lighting.motion_detection import (
        MotionDetectionMixin,
    )

    inst = MagicMock()
    # Apply the real mixin so we test actual logic, not mock side-effects
    inst._is_motion_active = (
        MotionDetectionMixin._is_motion_active.__get__(inst)
    )
    inst._functional_sensors = {"167": True, "63": True, "240": True}
    inst._runtime = SimpleNamespace(last_motion_time=None)
    inst._get_timeout_seconds = lambda: 1200  # 20 min
    inst.get_setting = MagicMock(return_value=False)  # default off
    inst.logger = MagicMock()
    return inst


def test_no_data_at_all_defers_to_active(monkeypatch):
    """The exact bug: Tier 1 is None, Tier 2 returns no rows. Old code
    returned False (no motion → turn off). New code returns True
    (defer; not enough info to act)."""
    inst = _make_instance()

    # Patch the Tier 2 HTTP call to return no rows.
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = []  # no events in window

    with patch(
        "apps.advanced_motion_lighting.motion_detection.requests.get",
        return_value=fake_response,
    ):
        result = inst._is_motion_active()

    assert result is True, (
        "Should defer (return True) when no motion data exists at all. "
        "Returning False here causes lights to turn off after every "
        "restart while user is still in the room."
    )
    # And confirm a log line was emitted so the operator can correlate
    # mystery "no off" decisions to this branch.
    info_calls = [c for c in inst.logger.info.call_args_list
                  if "Deferring" in str(c) or "No motion data yet" in str(c)]
    assert info_calls, "Defer branch should emit a log line"


def test_tier1_within_window_short_circuits(monkeypatch):
    """When Tier 1 has a recent timestamp, return True without calling
    out to Tier 2. (Sanity check that we didn't break the fast path.)"""
    from datetime import datetime, timedelta, timezone
    inst = _make_instance()
    inst._runtime.last_motion_time = datetime.now(timezone.utc) - timedelta(seconds=30)

    # No HTTP patch needed — Tier 2 should not be reached
    with patch(
        "apps.advanced_motion_lighting.motion_detection.requests.get",
        side_effect=AssertionError("Tier 2 should not be called"),
    ):
        result = inst._is_motion_active()

    assert result is True


def test_tier1_expired_and_tier2_empty_with_prior_motion_returns_false():
    """When Tier 1 *has* observed motion but it's outside the timeout
    window, AND Tier 2 confirms no recent active events, returning False
    is the correct behavior (positive evidence of no motion). We are
    NOT regressing this case."""
    from datetime import datetime, timedelta, timezone
    inst = _make_instance()
    # 30 min ago — well past the 20-min timeout
    inst._runtime.last_motion_time = datetime.now(timezone.utc) - timedelta(minutes=30)

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = []

    with patch(
        "apps.advanced_motion_lighting.motion_detection.requests.get",
        return_value=fake_response,
    ):
        result = inst._is_motion_active()

    assert result is False, (
        "When last_motion_time exists but is outside the timeout window "
        "AND Tier 2 confirms no recent active events, the right answer "
        "is False (positive evidence of inactivity)."
    )


def test_tier2_finds_recent_event_returns_true():
    """Standard happy path: Tier 1 is None (fresh start), Tier 2 finds
    a recent motion=active row, return True."""
    inst = _make_instance()
    inst._runtime.last_motion_time = None

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = [
        {"received_at": "2026-05-17T22:30:00+00:00", "canonical_device_id": 167},
    ]

    with patch(
        "apps.advanced_motion_lighting.motion_detection.requests.get",
        return_value=fake_response,
    ):
        result = inst._is_motion_active()

    assert result is True
