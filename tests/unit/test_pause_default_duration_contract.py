"""
Universal pause contract at the pause_instance choke point.

The 2026-07-15 TAT incident: the RN pause button POSTs an empty body ->
duration None -> the old `if duration_minutes:` wrote NO expiry -> instance 13
sat paused FOREVER while the operator expected his configured 300-minute
auto-resume. Contract now enforced server-side:

    None       -> the instance's OWN settings.pauseDuration/-Unit
    explicit 0 -> indefinite (unchanged)
    explicit N -> N minutes (unchanged)
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


def make_manager(settings):
    from services.instance_manager import InstanceManager
    m = InstanceManager.__new__(InstanceManager)
    m.postgrest_url = "http://postgrest:3001"
    m._running_instances = {}
    m.logger = MagicMock()
    m._http = MagicMock()
    m._http.patch.return_value = MagicMock(status_code=204)
    m.get_instance = MagicMock(return_value={"id": 13, "settings": settings})
    return m


def patched_expiry(m):
    body = m._http.patch.call_args.kwargs.get("json") or m._http.patch.call_args[1]["json"]
    return body.get("pause_expires_at")


class TestPauseDefaultDuration:
    def test_none_duration_inherits_configured_minutes(self):
        m = make_manager({"pauseDuration": 300, "pauseDurationUnit": "Minutes"})
        assert m.pause_instance(13, None) is True
        exp = datetime.fromisoformat(patched_expiry(m))
        expected = datetime.now(timezone.utc) + timedelta(minutes=300)
        assert abs((exp - expected).total_seconds()) < 5

    def test_none_duration_inherits_configured_seconds_unit(self):
        m = make_manager({"pauseDuration": 90, "pauseDurationUnit": "Seconds"})
        m.pause_instance(13, None)
        exp = datetime.fromisoformat(patched_expiry(m))
        expected = datetime.now(timezone.utc) + timedelta(seconds=90)
        assert abs((exp - expected).total_seconds()) < 5

    def test_explicit_zero_stays_indefinite(self):
        m = make_manager({"pauseDuration": 300, "pauseDurationUnit": "Minutes"})
        m.pause_instance(13, 0)
        assert patched_expiry(m) is None

    def test_none_with_no_configured_default_is_indefinite(self):
        m = make_manager({})
        m.pause_instance(13, None)
        assert patched_expiry(m) is None

    def test_explicit_duration_wins_over_settings(self):
        m = make_manager({"pauseDuration": 300, "pauseDurationUnit": "Minutes"})
        m.pause_instance(13, 10)
        exp = datetime.fromisoformat(patched_expiry(m))
        expected = datetime.now(timezone.utc) + timedelta(minutes=10)
        assert abs((exp - expected).total_seconds()) < 5

    def test_settings_lookup_failure_degrades_to_indefinite(self):
        m = make_manager({})
        m.get_instance = MagicMock(side_effect=RuntimeError("db down"))
        assert m.pause_instance(13, None) is True
        assert patched_expiry(m) is None
