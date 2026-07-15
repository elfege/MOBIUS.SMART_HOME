"""
Screen Time Planner — per-instance implementation suite (Tier 1, read-only).
Runs once per live `screen_time_planner` instance (e.g. TV Allowed Time).
"""
import pytest

from tests.implementation import checks

APP_TYPE = "screen_time_planner"
pytestmark = [pytest.mark.implementation, pytest.mark.observational]


def test_status_reflects_instance(client, instance):
    checks.assert_status_reflects_instance(client, instance)


def test_runtime_alive(client, instance):
    checks.assert_runtime_alive(client, instance)


def test_selected_devices_well_formed(instance):
    checks.assert_selection_well_formed(instance)


def test_no_persistent_error(instance):
    checks.assert_no_persistent_error(instance)
