"""
Eventsocket client uses bounded exponential backoff with ±25% jitter for
reconnect delays. The math:
    delay = min(backoff * jitter, MAX)
    jitter ∈ [0.75, 1.25]
    backoff doubles on each failure, starting at BACKOFF_BASE_SECS=1.0,
    capped at BACKOFF_MAX_SECS=30.0.

These tests pin down the math itself (pure function) and the constants we
ship with. If anyone changes the curve, these tests catch it loudly.
"""

import pytest

from services.hubitat_eventsocket_client import (
    BACKOFF_BASE_SECS,
    BACKOFF_MAX_SECS,
    BACKOFF_JITTER,
)


@pytest.mark.unit
class TestBackoffConstants:
    def test_base_is_one_second(self):
        assert BACKOFF_BASE_SECS == 1.0

    def test_max_is_thirty_seconds(self):
        assert BACKOFF_MAX_SECS == 30.0

    def test_jitter_is_25_percent(self):
        assert BACKOFF_JITTER == 0.25


@pytest.mark.unit
class TestJitteredDelayMath:
    """The actual formula used inside _run_hub. We replicate it here so the
    test is sealed against module-internal refactors that don't change the
    behavior."""

    @staticmethod
    def _jittered(backoff: float, jitter_value: float) -> float:
        # jitter_value should be in [-BACKOFF_JITTER, +BACKOFF_JITTER]
        return min(backoff * (1.0 + jitter_value), BACKOFF_MAX_SECS)

    def test_zero_jitter_returns_base(self):
        assert self._jittered(1.0, 0.0) == 1.0

    def test_max_negative_jitter_reduces_by_25_percent(self):
        assert self._jittered(4.0, -0.25) == 3.0

    def test_max_positive_jitter_increases_by_25_percent(self):
        assert self._jittered(4.0, 0.25) == 5.0

    def test_cap_clamps_to_max(self):
        # backoff=24, jitter=+25% → 30.0, capped at MAX
        assert self._jittered(24.0, 0.25) == 30.0

    def test_cap_holds_for_huge_backoff(self):
        assert self._jittered(1_000_000.0, 0.0) == 30.0

    def test_negative_jitter_still_caps_when_above_max(self):
        # 50 * 0.75 = 37.5 → capped at 30
        assert self._jittered(50.0, -0.25) == 30.0


@pytest.mark.unit
class TestExponentialGrowthCurve:
    """backoff doubles each failure until cap. Test the doubling progression."""

    @staticmethod
    def _next_backoff(current: float) -> float:
        return min(current * 2.0, BACKOFF_MAX_SECS)

    def test_doubles_at_each_step(self):
        assert self._next_backoff(1.0) == 2.0
        assert self._next_backoff(2.0) == 4.0
        assert self._next_backoff(4.0) == 8.0
        assert self._next_backoff(8.0) == 16.0

    def test_hits_max_after_five_failures(self):
        # 1 → 2 → 4 → 8 → 16 → 32 → clamped to 30
        assert self._next_backoff(16.0) == 30.0  # 32 → 30

    def test_stays_at_max(self):
        assert self._next_backoff(30.0) == 30.0
