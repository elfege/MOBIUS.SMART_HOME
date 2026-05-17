"""
Motion timeout floor enforcement in AML and FanAutomation.

System-level `motion_timeout_floor_seconds` (default 60s) clamps the
computed timeout. Per-instance `bypassTimeoutFloor=true` opts out.

Surfaced during the 2026-05-17 Kitchen Night=5s flicker incident.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestAMLTimeoutFloor:
    """The AML TimeoutMixin._get_timeout_seconds clamps below the floor."""

    def _make_host(self, *, settings, mode=None, floor=60, bypass=False):
        """Build a minimal host class with TimeoutMixin methods."""
        from apps.advanced_motion_lighting.timeout import TimeoutMixin

        class Host(TimeoutMixin):
            def __init__(self):
                self.logger = MagicMock()

            def get_setting(self, k, default=None):
                return settings.get(k, default)

            def _get_current_mode(self):
                return mode

        h = Host()
        return h, floor

    def test_below_floor_clamps_to_floor(self, mocker):
        h, floor = self._make_host(
            settings={
                "noMotionTime": 5,
                "timeUnit": "seconds",
            },
            floor=60,
        )
        mock_resolver = MagicMock()
        mock_resolver.get_system.return_value = floor
        mocker.patch(
            "services.settings_resolver.get_resolver",
            return_value=mock_resolver,
        )
        assert h._get_timeout_seconds() == 60

    def test_at_floor_stays_at_floor(self, mocker):
        h, _ = self._make_host(
            settings={"noMotionTime": 60, "timeUnit": "seconds"},
            floor=60,
        )
        mock_resolver = MagicMock()
        mock_resolver.get_system.return_value = 60
        mocker.patch("services.settings_resolver.get_resolver", return_value=mock_resolver)
        assert h._get_timeout_seconds() == 60

    def test_above_floor_unchanged(self, mocker):
        h, _ = self._make_host(
            settings={"noMotionTime": 300, "timeUnit": "seconds"},
            floor=60,
        )
        mock_resolver = MagicMock()
        mock_resolver.get_system.return_value = 60
        mocker.patch("services.settings_resolver.get_resolver", return_value=mock_resolver)
        assert h._get_timeout_seconds() == 300

    def test_minutes_unit_converted_then_compared_to_floor(self, mocker):
        # noMotionTime=5, timeUnit=minutes → 300 seconds. Floor 60 doesn't apply.
        h, _ = self._make_host(
            settings={"noMotionTime": 5, "timeUnit": "minutes"},
            floor=60,
        )
        mock_resolver = MagicMock()
        mock_resolver.get_system.return_value = 60
        mocker.patch("services.settings_resolver.get_resolver", return_value=mock_resolver)
        assert h._get_timeout_seconds() == 300

    def test_per_mode_short_value_is_clamped(self, mocker):
        # The exact Kitchen Night=5s scenario
        h, _ = self._make_host(
            settings={
                "noMotionTime": 300,
                "timeUnit": "seconds",
                "timeWithMode": True,
                "modeTimeouts": {"Night": 5, "Day": 300},
            },
            mode="Night",
            floor=60,
        )
        mock_resolver = MagicMock()
        mock_resolver.get_system.return_value = 60
        mocker.patch("services.settings_resolver.get_resolver", return_value=mock_resolver)
        assert h._get_timeout_seconds() == 60

    def test_bypass_disables_floor(self, mocker):
        h, _ = self._make_host(
            settings={
                "noMotionTime": 5,
                "timeUnit": "seconds",
                "bypassTimeoutFloor": True,
            },
            floor=60,
        )
        mock_resolver = MagicMock()
        mock_resolver.get_system.return_value = 60
        mocker.patch("services.settings_resolver.get_resolver", return_value=mock_resolver)
        # Bypassed → no clamp
        assert h._get_timeout_seconds() == 5

    def test_resolver_failure_falls_back_to_raw_value(self, mocker):
        h, _ = self._make_host(
            settings={"noMotionTime": 5, "timeUnit": "seconds"},
            floor=60,
        )
        mocker.patch(
            "services.settings_resolver.get_resolver",
            side_effect=Exception("DB down"),
        )
        # Floor lookup failed; raw value passes through
        assert h._get_timeout_seconds() == 5

    def test_zero_floor_is_a_no_op(self, mocker):
        h, _ = self._make_host(
            settings={"noMotionTime": 5, "timeUnit": "seconds"},
            floor=0,
        )
        mock_resolver = MagicMock()
        mock_resolver.get_system.return_value = 0
        mocker.patch("services.settings_resolver.get_resolver", return_value=mock_resolver)
        assert h._get_timeout_seconds() == 5
