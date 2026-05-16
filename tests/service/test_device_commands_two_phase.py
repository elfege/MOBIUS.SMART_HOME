"""
DeviceCommander writes to device_commands twice per command:
  1. INSERT a 'pending' row at issue (POST /device_commands)
  2. UPDATE that row at completion (PATCH /device_commands?id=eq.X) with:
     - outcome (confirmed/failed_verify/failed_network/failed_timeout)
     - completed_at
     - final_observed_value
     - verify_retries_used
     - latency_ms
     - error

These tests exercise _log_command_issued and _log_command_completed via
the public API where possible, and the failure paths directly.
"""

import json
import os
from unittest.mock import MagicMock

import pytest

os.environ["POSTGREST_URL"] = "http://postgrest:3001"

from models.command import CommandResult, CommandStatus
from services.device_commander import DeviceCommander


def _make_commander():
    client = MagicMock()
    client.config = MagicMock()
    client.config.hub_ip = "<LAN_IP>"
    dc = DeviceCommander(hubitat_client=client)
    dc._db_http = MagicMock()
    return dc


@pytest.mark.service
class TestLogCommandIssued:
    def test_inserts_pending_row_with_canonical_fk(self):
        dc = _make_commander()
        # POST returns the inserted row with id
        dc._db_http.post.return_value = MagicMock(
            status_code=201,
            json=lambda: [{"id": 7}],
        )
        # Stub hub lookup
        dc._db_http.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"hub_ip": "<LAN_IP>"}],
        )

        log_id = dc._log_command_issued(
            canonical_device_id="10",  # canonical (small int)
            hubitat_device_id="100",
            hub_name="hub4",
            command="on",
            args=None,
        )

        assert log_id == 7
        # POST body shape
        call = dc._db_http.post.call_args
        body = call.kwargs["json"]
        assert body["canonical_device_id"] == 10  # parsed to int
        assert body["hubitat_device_id"] == "100"
        assert body["hub_ip"] == "<LAN_IP>"
        assert body["command"] == "on"
        assert body["arguments"] == []
        assert body["outcome"] == "pending"
        assert body["attempt"] == 1
        assert body["max_attempts"] == 1

    def test_setlevel_with_arg_records_arguments_list(self):
        dc = _make_commander()
        dc._db_http.post.return_value = MagicMock(
            status_code=201, json=lambda: [{"id": 8}]
        )
        dc._db_http.get.return_value = MagicMock(
            status_code=200, json=lambda: []
        )

        dc._log_command_issued(
            canonical_device_id="10",
            hubitat_device_id="100",
            hub_name="hub4",
            command="setLevel",
            args=[75],
        )

        body = dc._db_http.post.call_args.kwargs["json"]
        assert body["arguments"] == [75]
        assert body["command"] == "setLevel"

    def test_too_large_canonical_id_skips_fk(self):
        # 5-digit "canonical_device_id" is suspicious — could be a Hubitat
        # native id leaked through. We refuse to FK it.
        dc = _make_commander()
        dc._db_http.post.return_value = MagicMock(
            status_code=201, json=lambda: [{"id": 9}]
        )
        dc._db_http.get.return_value = MagicMock(
            status_code=200, json=lambda: []
        )

        dc._log_command_issued(
            canonical_device_id="123456",  # too large
            hubitat_device_id="123456",
            hub_name="hub4",
            command="on",
            args=None,
        )

        body = dc._db_http.post.call_args.kwargs["json"]
        assert body["canonical_device_id"] is None
        # but hubitat_device_id retains the raw id
        assert body["hubitat_device_id"] == "123456"

    def test_non_numeric_canonical_id_skips_fk(self):
        dc = _make_commander()
        dc._db_http.post.return_value = MagicMock(
            status_code=201, json=lambda: [{"id": 10}]
        )
        dc._db_http.get.return_value = MagicMock(
            status_code=200, json=lambda: []
        )

        dc._log_command_issued(
            canonical_device_id="not-a-number",
            hubitat_device_id="100",
            hub_name="hub4",
            command="on",
            args=None,
        )

        body = dc._db_http.post.call_args.kwargs["json"]
        assert body["canonical_device_id"] is None

    def test_logging_disabled_returns_none_and_no_http_call(self):
        dc = _make_commander()
        dc._db_logging_enabled = False

        result = dc._log_command_issued(
            canonical_device_id="10",
            hubitat_device_id="100",
            hub_name="hub4",
            command="on",
            args=None,
        )

        assert result is None
        dc._db_http.post.assert_not_called()

    def test_postgrest_failure_returns_none_silently(self):
        dc = _make_commander()
        dc._db_http.post.side_effect = Exception("postgrest down")
        dc._db_http.get.return_value = MagicMock(
            status_code=200, json=lambda: []
        )

        result = dc._log_command_issued(
            canonical_device_id="10",
            hubitat_device_id="100",
            hub_name="hub4",
            command="on",
            args=None,
        )

        # Failure is swallowed — command must keep executing
        assert result is None


@pytest.mark.service
class TestHubNameToIpCache:
    def test_caches_after_first_lookup(self):
        dc = _make_commander()
        dc._db_http.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"hub_ip": "<LAN_IP>"}],
        )

        # First call hits HTTP
        ip1 = dc._hub_name_to_ip("home_1")
        # Second call should be cached
        ip2 = dc._hub_name_to_ip("home_1")

        assert ip1 == "<LAN_IP>"
        assert ip2 == "<LAN_IP>"
        # Only one HTTP call total
        assert dc._db_http.get.call_count == 1

    def test_default_hub_name_returns_none(self):
        dc = _make_commander()

        assert dc._hub_name_to_ip("default") is None
        assert dc._hub_name_to_ip("") is None
        # No HTTP call
        dc._db_http.get.assert_not_called()

    def test_unknown_hub_returns_none(self):
        dc = _make_commander()
        dc._db_http.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [],  # no row
        )

        assert dc._hub_name_to_ip("nonexistent") is None
