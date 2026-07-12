"""
Coverage for AML's dim-level cascade: _get_current_dim_level().

Cascade order (added 2026-06-12, refined 2026-06-17):
  1. Memoized manual override for this device (source='manual')
  2. modeDimLevels[current_mode]   — only when dimWithMode is enabled
                                     case-insensitive mode-key lookup
  3. defaultDimLevel from settings

These tests pin every tier and the case-insensitive defense added
2026-06-17 in response to the instance-8 / WatchingTV bug. Reference:
TILES _mapModeToBackground case-sensitivity bug 2026-03-18 was the
same class of issue.
"""

from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.unit


def _make_instance(*, settings=None, memo=None, current_mode="Day"):
    """Minimal harness with the real _get_current_dim_level bound."""
    from apps.advanced_motion_lighting.light_control.color_and_dim import (
        ColorAndDimMixin,
    )

    inst = MagicMock()
    inst._get_current_dim_level = (
        ColorAndDimMixin._get_current_dim_level.__get__(inst)
    )
    inst.logger = MagicMock()
    inst._memoization = {'dim_level': memo or {}}

    settings = settings or {}
    inst.get_setting = MagicMock(side_effect=lambda k, default=None:
                                 settings.get(k, default))
    inst._get_current_mode = MagicMock(return_value=current_mode)
    return inst


# ---------------------------------------------------------------------------
# Tier 1 — manual override wins everything
# ---------------------------------------------------------------------------


def test_manual_memo_override_wins_over_per_mode_and_default():
    inst = _make_instance(
        settings={
            'dimWithMode': True,
            'modeDimLevels': {'Day': 100, 'WatchingTV': 10},
            'defaultDimLevel': 50,
        },
        memo={'Living Lamp': {'level': 80, 'source': 'manual'}},
        current_mode='WatchingTV',
    )

    assert inst._get_current_dim_level('Living Lamp') == 80


def test_manual_memo_with_app_source_is_ignored():
    """Only source='manual' wins. source='app' memos are our own previous
    writes and should NOT shadow new settings."""
    inst = _make_instance(
        settings={
            'dimWithMode': True,
            'modeDimLevels': {'WatchingTV': 10},
            'defaultDimLevel': 50,
        },
        memo={'Living Lamp': {'level': 80, 'source': 'app'}},
        current_mode='WatchingTV',
    )

    assert inst._get_current_dim_level('Living Lamp') == 10  # per-mode wins


# ---------------------------------------------------------------------------
# Tier 2 — per-mode (dimWithMode + modeDimLevels)
# ---------------------------------------------------------------------------


def test_per_mode_level_applied_when_dimWithMode_enabled():
    inst = _make_instance(
        settings={
            'dimWithMode': True,
            'modeDimLevels': {'Day': 100, 'WatchingTV': 10, 'Night': 30},
            'defaultDimLevel': 50,
        },
        current_mode='WatchingTV',
    )

    assert inst._get_current_dim_level('Hallway') == 10


def test_per_mode_ignored_when_dimWithMode_disabled():
    """Even if modeDimLevels has the current mode, the toggle off means
    fall back to defaultDimLevel."""
    inst = _make_instance(
        settings={
            'dimWithMode': False,
            'modeDimLevels': {'WatchingTV': 10},
            'defaultDimLevel': 50,
        },
        current_mode='WatchingTV',
    )

    assert inst._get_current_dim_level('Hallway') == 50


def test_per_mode_falls_through_when_mode_not_in_map():
    inst = _make_instance(
        settings={
            'dimWithMode': True,
            'modeDimLevels': {'Day': 100},  # no 'Evening' entry
            'defaultDimLevel': 50,
        },
        current_mode='Evening',
    )

    assert inst._get_current_dim_level('Hallway') == 50


# ---------------------------------------------------------------------------
# Tier 2 — case-insensitive mode-key lookup (defense added 2026-06-17)
# ---------------------------------------------------------------------------


def test_case_insensitive_mode_match_lowercase_key():
    """Hubitat reports 'WatchingTV'; UI may store key as 'watchingtv'.
    Defensive fallback must still match."""
    inst = _make_instance(
        settings={
            'dimWithMode': True,
            'modeDimLevels': {'watchingtv': 15, 'day': 100},
            'defaultDimLevel': 50,
        },
        current_mode='WatchingTV',
    )

    assert inst._get_current_dim_level('Hallway') == 15


def test_case_insensitive_mode_match_uppercase_key():
    inst = _make_instance(
        settings={
            'dimWithMode': True,
            'modeDimLevels': {'WATCHINGTV': 20},
            'defaultDimLevel': 50,
        },
        current_mode='WatchingTV',
    )

    assert inst._get_current_dim_level('Hallway') == 20


def test_exact_match_preferred_over_case_insensitive():
    """When BOTH cases exist in the dict, the exact match wins
    (lookup order: dict.get first, then case-insensitive scan)."""
    inst = _make_instance(
        settings={
            'dimWithMode': True,
            'modeDimLevels': {'WatchingTV': 10, 'watchingtv': 99},
            'defaultDimLevel': 50,
        },
        current_mode='WatchingTV',
    )

    assert inst._get_current_dim_level('Hallway') == 10


# ---------------------------------------------------------------------------
# Tier 3 — defaultDimLevel fallback
# ---------------------------------------------------------------------------


def test_default_dim_level_when_no_memo_no_per_mode():
    inst = _make_instance(
        settings={'defaultDimLevel': 42},
        current_mode='Day',
    )

    assert inst._get_current_dim_level('Hallway') == 42


def test_default_dim_level_fallback_when_setting_missing():
    """If defaultDimLevel isn't in settings, the hardcoded default of 50
    is returned."""
    inst = _make_instance(settings={}, current_mode='Day')

    assert inst._get_current_dim_level('Hallway') == 50
