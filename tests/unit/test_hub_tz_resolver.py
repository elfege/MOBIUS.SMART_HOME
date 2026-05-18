"""
Unit tests for services.hub_tz_resolver.

Coverage targets:
  - Windows TZ → IANA mapping (the table we'll keep extending)
  - Majority pick on hub disagreement (with tie-breaker behavior)
  - Unreachable-hub handling
  - Unmapped Windows TZ → None + warning path
"""

from unittest.mock import patch

import pytest

from services.hub_tz_resolver import (
    WINDOWS_TZ_TO_IANA,
    resolve_hub_timezone,
)

pytestmark = pytest.mark.unit


class TestWindowsToIanaMapping:
    def test_common_north_america_zones_mapped(self):
        # Smoke-check the zones Elfege actually uses; if these flip to
        # None, every hub Elfege owns would fall back to UTC.
        assert WINDOWS_TZ_TO_IANA["Eastern Standard Time"] == "America/New_York"
        assert WINDOWS_TZ_TO_IANA["Pacific Standard Time"] == "America/Los_Angeles"
        assert WINDOWS_TZ_TO_IANA["Central Standard Time"] == "America/Chicago"

    def test_iana_names_are_well_formed(self):
        # Every mapped IANA string should look like "Region/City" with
        # at least one slash. Catches typos like "AmericaNewYork".
        for win_tz, iana in WINDOWS_TZ_TO_IANA.items():
            assert "/" in iana, (
                f"IANA name {iana!r} for {win_tz!r} is malformed"
            )


class TestResolveHubTimezone:
    """Resolver behavior across the reachable/unreachable/disagree axes.

    We patch the two boundary helpers (_enabled_hubs + _query_hub_tz)
    rather than mocking requests, so the resolver's own logic runs."""

    def _patch_hubs(self, hub_list):
        return patch("services.hub_tz_resolver._enabled_hubs",
                     return_value=hub_list)

    def _patch_query(self, side_effect):
        return patch("services.hub_tz_resolver._query_hub_tz",
                     side_effect=side_effect)

    def test_no_hubs_configured_returns_none_consistent(self):
        with self._patch_hubs([]):
            iana, consistent, breakdown = resolve_hub_timezone()
        assert iana is None
        assert consistent is True   # vacuously
        assert breakdown == {}

    def test_all_hubs_agree(self):
        hubs = [
            {"hub_name": "home_1", "hub_ip": "10.0.0.1"},
            {"hub_name": "home_2", "hub_ip": "10.0.0.2"},
        ]
        with self._patch_hubs(hubs), \
             self._patch_query(lambda ip, name: "Eastern Standard Time"):
            iana, consistent, breakdown = resolve_hub_timezone()
        assert iana == "America/New_York"
        assert consistent is True
        assert breakdown == {
            "home_1": "Eastern Standard Time",
            "home_2": "Eastern Standard Time",
        }

    def test_majority_wins_on_disagreement(self):
        hubs = [
            {"hub_name": "home_1", "hub_ip": "10.0.0.1"},
            {"hub_name": "home_2", "hub_ip": "10.0.0.2"},
            {"hub_name": "home_3", "hub_ip": "10.0.0.3"},
        ]
        # Two say Eastern, one says Pacific — Eastern should win.
        tz_by_name = {
            "home_1": "Eastern Standard Time",
            "home_2": "Eastern Standard Time",
            "home_3": "Pacific Standard Time",
        }
        with self._patch_hubs(hubs), \
             self._patch_query(lambda ip, name: tz_by_name[name]):
            iana, consistent, breakdown = resolve_hub_timezone()
        assert iana == "America/New_York"
        assert consistent is False
        assert breakdown["home_3"] == "Pacific Standard Time"

    def test_unreachable_hub_recorded_but_does_not_block_resolution(self):
        hubs = [
            {"hub_name": "home_1", "hub_ip": "10.0.0.1"},
            {"hub_name": "home_dead", "hub_ip": "10.0.0.99"},
        ]
        # home_dead returns None (unreachable)
        def fake_query(ip, name):
            return None if name == "home_dead" else "Eastern Standard Time"
        with self._patch_hubs(hubs), self._patch_query(fake_query):
            iana, consistent, breakdown = resolve_hub_timezone()
        assert iana == "America/New_York"
        assert consistent is True   # only one reachable, so 1-of-1 agrees
        assert breakdown["home_dead"] == "unreachable"

    def test_all_hubs_unreachable_returns_none(self):
        hubs = [{"hub_name": "home_1", "hub_ip": "10.0.0.1"}]
        with self._patch_hubs(hubs), self._patch_query(lambda *a: None):
            iana, consistent, breakdown = resolve_hub_timezone()
        assert iana is None
        assert breakdown == {"home_1": "unreachable"}

    def test_unmapped_windows_tz_returns_none_marks_breakdown(self):
        hubs = [{"hub_name": "home_1", "hub_ip": "10.0.0.1"}]
        # Made-up zone that won't be in the mapping table
        with self._patch_hubs(hubs), \
             self._patch_query(lambda *a: "Antarctica/McMurdo Standard Time"):
            iana, consistent, breakdown = resolve_hub_timezone()
        assert iana is None
        # The breakdown entry for the (only) hub should be flagged as
        # unmapped so the user / log reader sees what to add to the table.
        assert breakdown["home_1"].startswith("unmapped:")

    def test_tie_breaker_uses_first_hub(self):
        # 2 vs 2 split. Counter.most_common preserves insertion order,
        # so whichever Windows TZ was *seen first* wins. The order
        # `_enabled_hubs` returns drives this.
        hubs = [
            {"hub_name": "h1", "hub_ip": "1"},
            {"hub_name": "h2", "hub_ip": "2"},
            {"hub_name": "h3", "hub_ip": "3"},
            {"hub_name": "h4", "hub_ip": "4"},
        ]
        tz_by = {"h1": "Eastern Standard Time", "h2": "Pacific Standard Time",
                 "h3": "Eastern Standard Time", "h4": "Pacific Standard Time"}
        with self._patch_hubs(hubs), \
             self._patch_query(lambda ip, name: tz_by[name]):
            iana, consistent, _ = resolve_hub_timezone()
        assert iana == "America/New_York"  # Eastern was inserted first
        assert consistent is False
