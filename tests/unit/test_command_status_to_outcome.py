"""
DeviceCommander._log_command_completed maps CommandStatus → device_commands.outcome.
The mapping isn't a public method — we test the logic inline via the
documented behavior.

| CommandStatus  | When                            | outcome              |
|----------------|---------------------------------|----------------------|
| VERIFIED       | verify pass                     | confirmed            |
| TIMEOUT        | overall timeout exceeded        | failed_timeout       |
| FAILED + verified=False + expected_state set    | verify failed              | failed_verify        |
| FAILED + no expected_state (i.e. verify skipped)| network/send failed        | failed_network       |
"""

import pytest

from models.command import CommandResult, CommandStatus
from services.device_commander import DeviceCommander
from unittest.mock import MagicMock


def _commander_with_logging():
    """A DeviceCommander instance we can call _log_command_completed on
    without actually firing HTTP. We patch the HTTP session right after init."""
    client = MagicMock()
    client.config = MagicMock()
    client.config.hub_ip = "<LAN_IP>"
    dc = DeviceCommander(hubitat_client=client)
    dc._db_http = MagicMock()  # captures the .patch() call we want to inspect
    return dc


def _make_result(*, status, success=True, expected_state=None, error=None,
                 actual_state=None, elapsed_ms=42, retries_used=None):
    r = CommandResult(
        device_id="10",
        device_name="Test",
        command="on",
        args=None,
        success=success,
        expected_state=expected_state,
        actual_state=actual_state,
        elapsed_ms=elapsed_ms,
        error=error,
        status=status,
        retries_used=retries_used or {},
    )
    return r


@pytest.mark.unit
class TestCommandStatusToOutcomeMapping:
    def test_verified_maps_to_confirmed(self):
        dc = _commander_with_logging()
        dc._log_command_completed(
            command_log_id=1,
            result=_make_result(status=CommandStatus.VERIFIED, actual_state="on"),
        )
        # The PATCH body's 'outcome' must be 'confirmed'
        call = dc._db_http.patch.call_args
        assert call.kwargs["json"]["outcome"] == "confirmed"

    def test_timeout_maps_to_failed_timeout(self):
        dc = _commander_with_logging()
        dc._log_command_completed(
            command_log_id=1,
            result=_make_result(status=CommandStatus.TIMEOUT, success=False),
        )
        call = dc._db_http.patch.call_args
        assert call.kwargs["json"]["outcome"] == "failed_timeout"

    def test_failed_with_expected_state_maps_to_failed_verify(self):
        dc = _commander_with_logging()
        dc._log_command_completed(
            command_log_id=1,
            result=_make_result(
                status=CommandStatus.FAILED,
                success=False,
                expected_state="on",
                actual_state="off",
            ),
        )
        call = dc._db_http.patch.call_args
        assert call.kwargs["json"]["outcome"] == "failed_verify"

    def test_failed_without_expected_state_maps_to_failed_network(self):
        # No expected_state ⇒ verification was skipped / send failed before verify
        dc = _commander_with_logging()
        dc._log_command_completed(
            command_log_id=1,
            result=_make_result(
                status=CommandStatus.FAILED,
                success=False,
                expected_state=None,
                error="Connection refused",
            ),
        )
        call = dc._db_http.patch.call_args
        assert call.kwargs["json"]["outcome"] == "failed_network"


@pytest.mark.unit
class TestCommandCompletedBodyShape:
    def test_payload_includes_latency_and_error(self):
        dc = _commander_with_logging()
        dc._log_command_completed(
            command_log_id=42,
            result=_make_result(
                status=CommandStatus.FAILED,
                success=False,
                expected_state="on",
                actual_state="off",
                error="verify failed after 3 polls",
                elapsed_ms=2345.6,
            ),
        )
        body = dc._db_http.patch.call_args.kwargs["json"]
        assert body["latency_ms"] == 2345  # int conversion
        assert body["error"] == "verify failed after 3 polls"
        assert body["final_observed_value"] == "off"

    def test_completed_at_is_iso_string(self):
        dc = _commander_with_logging()
        dc._log_command_completed(
            command_log_id=42,
            result=_make_result(status=CommandStatus.VERIFIED),
        )
        body = dc._db_http.patch.call_args.kwargs["json"]
        # Must be parseable as ISO 8601
        from datetime import datetime
        datetime.fromisoformat(body["completed_at"])

    def test_no_db_call_when_logging_disabled(self):
        dc = _commander_with_logging()
        dc._db_logging_enabled = False
        dc._log_command_completed(
            command_log_id=42,
            result=_make_result(status=CommandStatus.VERIFIED),
        )
        dc._db_http.patch.assert_not_called()

    def test_no_db_call_when_command_log_id_is_none(self):
        # If the issued-time INSERT failed, we have no id to UPDATE.
        dc = _commander_with_logging()
        dc._log_command_completed(
            command_log_id=None,
            result=_make_result(status=CommandStatus.VERIFIED),
        )
        dc._db_http.patch.assert_not_called()
