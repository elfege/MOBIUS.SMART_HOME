"""
Coverage for device_commander Matter-primary fall-through safety (Phase 2).

The cardinal rule: _try_matter_primary returns None (→ "use Hubitat") for
ANY miss, so the Hubitat fallback is never silently skipped. These tests pin
the fall-through branches without needing a live matter-server.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.device_commander import DeviceCommander


pytestmark = pytest.mark.service


def _commander():
    # Build without running __init__ (which constructs clients/executors).
    return DeviceCommander.__new__(DeviceCommander)


def test_not_commissioned_falls_through_to_hubitat():
    c = _commander()
    with patch("services.matter_mapping.resolve_device_to_node",
               return_value=None):
        out = c._try_matter_primary(71, "on", None, True, "Living")
    assert out is None  # None => Hubitat path


def test_offline_node_falls_through():
    c = _commander()
    with patch("services.matter_mapping.resolve_device_to_node",
               return_value={"node_id": 2, "endpoint_id": 1, "is_online": False}):
        out = c._try_matter_primary(170, "on", None, True, "Terrace")
    assert out is None


def test_matter_server_disconnected_falls_through():
    c = _commander()
    fake_client = MagicMock()
    fake_client.is_connected = False
    with patch("services.matter_mapping.resolve_device_to_node",
               return_value={"node_id": 2, "endpoint_id": 1, "is_online": True}), \
         patch("services.matter_client.get_matter_client", return_value=fake_client):
        out = c._try_matter_primary(170, "on", None, True, "Terrace")
    assert out is None


def test_verified_matter_command_returns_success_result():
    c = _commander()
    c._set_device_status = MagicMock()   # avoid DB write
    fake_client = MagicMock()
    fake_client.is_connected = True

    # run_on_loop is called twice: send (returns truthy), then read_attribute
    # (returns True == 'on'). Sequence the return values.
    calls = {"n": 0}

    def fake_run_on_loop(coro, timeout=6.0):
        # close the un-awaited coroutine to avoid 'never awaited' warnings
        try:
            coro.close()
        except Exception:
            pass
        calls["n"] += 1
        return {"ok": True} if calls["n"] == 1 else True  # send, then OnOff=True

    with patch("services.matter_mapping.resolve_device_to_node",
               return_value={"node_id": 2, "endpoint_id": 1, "is_online": True}), \
         patch("services.matter_client.get_matter_client", return_value=fake_client), \
         patch("services.matter_client.run_on_loop", side_effect=fake_run_on_loop), \
         patch("services.device_cache.get_default_cache", return_value=MagicMock()):
        out = c._try_matter_primary(170, "on", None, True, "Terrace")

    assert out is not None
    assert out.success is True
    assert out.verified is True
    assert out.matter_sent is True
    assert out.actual_state == "on"


def test_matter_verify_mismatch_falls_through():
    c = _commander()
    c._set_device_status = MagicMock()
    fake_client = MagicMock()
    fake_client.is_connected = True

    calls = {"n": 0}

    def fake_run_on_loop(coro, timeout=6.0):
        try:
            coro.close()
        except Exception:
            pass
        calls["n"] += 1
        # send ok, but OnOff reads False ('off') while we asked for 'on'
        return {"ok": True} if calls["n"] == 1 else False

    with patch("services.matter_mapping.resolve_device_to_node",
               return_value={"node_id": 2, "endpoint_id": 1, "is_online": True}), \
         patch("services.matter_client.get_matter_client", return_value=fake_client), \
         patch("services.matter_client.run_on_loop", side_effect=fake_run_on_loop):
        out = c._try_matter_primary(170, "on", None, True, "Terrace")

    assert out is None  # mismatch => fall through to Hubitat
