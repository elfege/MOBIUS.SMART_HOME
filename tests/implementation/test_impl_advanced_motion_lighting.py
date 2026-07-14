"""
Advanced Motion Lighting — per-instance implementation suite (Tier 1, read-only).

APP_TYPE binds this module to the real advanced_motion_lighting instances:
conftest.pytest_generate_tests runs every `instance` test once per live AML
instance (the "per app suite, per instance" of R1). All checks hit the REAL app.
"""
import pytest

from tests.implementation import checks

APP_TYPE = "advanced_motion_lighting"
pytestmark = [pytest.mark.implementation, pytest.mark.observational]


def test_status_reflects_instance(client, instance):
    checks.assert_status_reflects_instance(client, instance)


def test_runtime_alive(client, instance):
    checks.assert_runtime_alive(client, instance)


def test_selected_devices_well_formed(instance):
    checks.assert_selection_well_formed(instance)


def test_no_persistent_error(instance):
    checks.assert_no_persistent_error(instance)


def test_has_motion_and_switch_wiring(instance):
    """An AML instance that controls no switch, or watches no motion sensor, is
    misconfigured — it cannot do the one thing it exists to do."""
    sel = instance.get("device_selections") or {}
    assert sel.get("switches"), (
        f"AML instance {instance['id']} ({instance.get('label')!r}) has NO switches selected"
    )
    assert sel.get("motion_sensors"), (
        f"AML instance {instance['id']} ({instance.get('label')!r}) has NO motion sensors selected"
    )
