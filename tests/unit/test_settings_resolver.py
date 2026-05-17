"""
SettingsResolver — cascade logic (instance → app-type → system → default),
type coercion, and cache behavior.

We mock PostgREST at the requests boundary; everything else is real code.
"""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("POSTGREST_URL", "http://postgrest:3001")

from services.settings_resolver import (
    SettingsResolver, _coerce, _serialize,
)


@pytest.mark.unit
class TestCoercion:
    def test_int(self):
        assert _coerce("42", "int") == 42

    def test_float(self):
        assert _coerce("3.14", "float") == 3.14

    def test_bool_true(self):
        for v in ("true", "True", "TRUE", "1", "yes", "on"):
            assert _coerce(v, "bool") is True

    def test_bool_false(self):
        for v in ("false", "False", "0", "no", "anything-else"):
            assert _coerce(v, "bool") is False

    def test_json(self):
        assert _coerce('{"a": 1, "b": [2, 3]}', "json") == {"a": 1, "b": [2, 3]}

    def test_string_default(self):
        assert _coerce("hello", "string") == "hello"

    def test_unknown_type_treated_as_string(self):
        assert _coerce("hello", "weird") == "hello"

    def test_none_returns_none(self):
        assert _coerce(None, "int") is None


@pytest.mark.unit
class TestSerialize:
    def test_int_roundtrip(self):
        assert _coerce(_serialize(42, "int"), "int") == 42

    def test_bool_roundtrip(self):
        assert _coerce(_serialize(True, "bool"), "bool") is True
        assert _coerce(_serialize(False, "bool"), "bool") is False

    def test_json_roundtrip(self):
        v = {"nested": [1, 2, 3]}
        assert _coerce(_serialize(v, "json"), "json") == v


@pytest.mark.unit
class TestCascadeResolution:
    """The four-tier resolution order is the critical contract."""

    def _patch_fetch(self, resolver, sys_rows=None, at_rows=None):
        """Stub _fetch_system_row + _fetch_app_type_row to controlled rows."""
        sys_rows = sys_rows or {}
        at_rows = at_rows or {}

        def fake_sys(key):
            return sys_rows.get(key)

        def fake_at(app_type_id, key):
            return at_rows.get((app_type_id, key))

        resolver._fetch_system_row = fake_sys
        resolver._fetch_app_type_row = fake_at

    def test_instance_setting_wins_over_app_type_and_system(self):
        r = SettingsResolver()
        self._patch_fetch(
            r,
            sys_rows={"k": {"value": "100", "value_type": "int"}},
            at_rows={(1, "k"): {"value": "50", "value_type": "int"}},
        )
        v = r.get(
            "k",
            instance_settings={"k": 5},
            instance_schema_properties={"k": {}},
            app_type_id=1,
        )
        assert v == 5

    def test_app_type_setting_wins_over_system(self):
        r = SettingsResolver()
        self._patch_fetch(
            r,
            sys_rows={"k": {"value": "100", "value_type": "int"}},
            at_rows={(1, "k"): {"value": "50", "value_type": "int"}},
        )
        v = r.get("k", app_type_id=1)
        assert v == 50

    def test_system_setting_when_no_app_type(self):
        r = SettingsResolver()
        self._patch_fetch(
            r,
            sys_rows={"k": {"value": "100", "value_type": "int"}},
        )
        v = r.get("k")
        assert v == 100

    def test_caller_default_when_no_tier_has_key(self):
        r = SettingsResolver()
        self._patch_fetch(r)
        v = r.get("missing", default=999)
        assert v == 999

    def test_instance_setting_ignored_if_not_in_schema(self):
        """Key in instance settings JSONB but NOT in settings_schema —
        treat as not-exposed, fall through to lower tiers."""
        r = SettingsResolver()
        self._patch_fetch(
            r,
            sys_rows={"k": {"value": "100", "value_type": "int"}},
        )
        v = r.get(
            "k",
            instance_settings={"k": 5},
            instance_schema_properties={},  # k NOT exposed
        )
        assert v == 100  # fell through to system

    def test_lenient_when_no_schema_provided(self):
        """If schema is None, any key in instance_settings counts as exposed."""
        r = SettingsResolver()
        self._patch_fetch(
            r,
            sys_rows={"k": {"value": "100", "value_type": "int"}},
        )
        v = r.get(
            "k",
            instance_settings={"k": 5},
            instance_schema_properties=None,
        )
        assert v == 5


@pytest.mark.unit
class TestCacheBehavior:
    def test_system_cache_hits_avoid_postgrest_call(self):
        r = SettingsResolver()
        fetch_mock = MagicMock(return_value={"value": "60", "value_type": "int"})
        r._fetch_system_row = fetch_mock

        r._get_system("motion_timeout_floor_seconds")
        r._get_system("motion_timeout_floor_seconds")
        r._get_system("motion_timeout_floor_seconds")

        assert fetch_mock.call_count == 1

    def test_cache_expires_after_ttl(self, mocker):
        # Force a very short TTL for the test
        mocker.patch(
            "services.settings_resolver.CACHE_TTL_SECS", 0.05
        )
        r = SettingsResolver()
        fetch_mock = MagicMock(return_value={"value": "60", "value_type": "int"})
        r._fetch_system_row = fetch_mock

        r._get_system("k")
        time.sleep(0.1)
        r._get_system("k")

        assert fetch_mock.call_count == 2

    def test_invalidate_all_clears_cache(self):
        r = SettingsResolver()
        fetch_mock = MagicMock(return_value={"value": "60", "value_type": "int"})
        r._fetch_system_row = fetch_mock

        r._get_system("k")
        r.invalidate_all()
        r._get_system("k")

        assert fetch_mock.call_count == 2


@pytest.mark.unit
class TestSetters:
    def test_set_system_invalidates_cache(self, mocker):
        r = SettingsResolver()
        r._fetch_system_row = MagicMock(
            return_value={"value": "60", "value_type": "int"}
        )
        mocker.patch(
            "services.settings_resolver.requests.patch",
            return_value=MagicMock(status_code=204),
        )

        r._get_system("k")  # populate cache
        assert ("k" in r._sys_cache)

        ok = r.set_system("k", 99)
        assert ok
        # Cache entry purged
        assert "k" not in r._sys_cache

    def test_set_system_unknown_key_returns_false(self):
        r = SettingsResolver()
        r._fetch_system_row = MagicMock(return_value=None)  # row doesn't exist

        ok = r.set_system("nonexistent", 99)
        assert ok is False

    def test_set_system_postgrest_failure_returns_false(self, mocker):
        r = SettingsResolver()
        r._fetch_system_row = MagicMock(
            return_value={"value": "60", "value_type": "int"}
        )
        mocker.patch(
            "services.settings_resolver.requests.patch",
            return_value=MagicMock(status_code=500, text="boom"),
        )
        assert r.set_system("k", 99) is False
