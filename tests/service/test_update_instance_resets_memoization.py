"""
Coverage for instance_manager.update_instance() always clearing the
memoization_state column when settings or device_selections change.

The 2026-06-17 fix: when a user changes per-mode dim levels or swaps a
device, any stale manual overrides previously memoized must NOT survive
the update. Otherwise the cascade returns the old override forever and
the new settings appear to be ignored. Label-only updates leave memo
untouched (no field the memo depends on has moved).

These tests mock the HTTP layer (PostgREST) so the patch body we send is
the only thing under test. The full restart pipeline (stop_instance,
_rebuild_subscriptions, _start_from_db) is exercised via mocks.
"""

from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.service


def _make_manager(*, instance_exists=True, patch_status=204):
    """Build an InstanceManager with the HTTP boundary mocked."""
    from services.instance_manager import InstanceManager

    mgr = InstanceManager.__new__(InstanceManager)
    mgr.postgrest_url = "http://postgrest:3001"
    mgr.logger = MagicMock()
    mgr._http = MagicMock()

    # PATCH response
    patch_resp = MagicMock()
    patch_resp.status_code = patch_status
    mgr._http.patch.return_value = patch_resp

    # GET response for get_instance() (used by settings-merge path)
    if instance_exists:
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = [{
            "id": 8, "label": "Hallway",
            "settings": {"defaultDimLevel": 50, "useDim": True},
            "device_selections": {"switches": ["100"]},
        }]
        mgr._http.get.return_value = get_resp

    # Plumbing methods we don't want to actually fire
    mgr.stop_instance = MagicMock(return_value=True)
    mgr._rebuild_subscriptions = MagicMock()
    mgr._start_from_db = MagicMock(return_value=True)
    return mgr


def _patch_body(mgr) -> dict:
    """Extract the JSON body that update_instance PATCH'd to PostgREST."""
    assert mgr._http.patch.called, "PATCH was not called"
    return mgr._http.patch.call_args.kwargs["json"]


# ---------------------------------------------------------------------------
# Settings-change branch
# ---------------------------------------------------------------------------


def test_settings_change_clears_memoization_state():
    mgr = _make_manager()

    ok = mgr.update_instance(
        8,
        settings={"modeDimLevels": {"WatchingTV": 10}},
    )

    assert ok is True
    body = _patch_body(mgr)
    assert "memoization_state" in body
    assert body["memoization_state"] == {}
    # Settings were merged with existing
    assert "settings" in body
    assert body["settings"]["modeDimLevels"] == {"WatchingTV": 10}
    assert body["settings"]["defaultDimLevel"] == 50  # preserved


def test_device_selections_change_clears_memoization_state():
    mgr = _make_manager()

    ok = mgr.update_instance(
        8,
        device_selections={"switches": ["100", "101"]},
    )

    assert ok is True
    body = _patch_body(mgr)
    assert body["memoization_state"] == {}
    assert body["device_selections"] == {"switches": ["100", "101"]}


def test_combined_settings_and_devices_change_clears_memoization_state_once():
    mgr = _make_manager()

    ok = mgr.update_instance(
        8,
        settings={"defaultDimLevel": 30},
        device_selections={"switches": ["100"]},
    )

    assert ok is True
    body = _patch_body(mgr)
    assert body["memoization_state"] == {}


# ---------------------------------------------------------------------------
# Label-only branch — memo is preserved
# ---------------------------------------------------------------------------


def test_label_only_change_does_NOT_clear_memoization_state():
    """No field the memoization depends on (device_name, settings keys) has
    changed — clearing would be unnecessarily disruptive."""
    mgr = _make_manager()

    ok = mgr.update_instance(8, label="New Hallway Label")

    assert ok is True
    body = _patch_body(mgr)
    assert "memoization_state" not in body
    assert body["label"] == "New Hallway Label"


# ---------------------------------------------------------------------------
# No-op update
# ---------------------------------------------------------------------------


def test_no_change_returns_true_without_patch():
    mgr = _make_manager()

    ok = mgr.update_instance(8)

    assert ok is True
    mgr._http.patch.assert_not_called()
    mgr.stop_instance.assert_not_called()


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


def test_patch_failure_returns_false_and_restarts_from_old_state():
    """When the PATCH HTTP call fails, update_instance must restart the
    instance from the (unchanged) DB state so it's not left dead."""
    mgr = _make_manager(patch_status=500)
    mgr._http.patch.return_value.text = "DB error"

    ok = mgr.update_instance(8, settings={"defaultDimLevel": 30})

    assert ok is False
    mgr._start_from_db.assert_called_once_with(8)
