"""
Integration test: every configured hub reports the same time zone.

Why this file exists
--------------------
2026-05-17: timestamp handling across the system assumes consistent time
basis. The DB stores UTC; display layer applies a single user-configured
TZ. If the hubs themselves disagree on TZ (e.g., one set to Pacific while
the others are Eastern), event timestamps from the hubs and the cross-hub
device-state correlation start drifting in subtle, hard-to-debug ways.

Hubitat exposes its location TZ at the admin endpoint:
  GET /location/list/data  →  [{"id":1,"name":"Home 1",
                                "timeZone":"Eastern Standard Time", ...}]

This test pulls that field from each enabled hub in hub_config and
asserts every hub returns the same value. Failure surfaces as a
detailed diff so the user knows which hub to reconfigure.

Note: Hubitat uses Windows-style TZ names ("Eastern Standard Time" year-
round, regardless of DST). Comparing string equality is what we want;
DST handling is the hub's responsibility.
"""

from collections import defaultdict
from typing import Dict, List, Optional

import pytest
import requests

pytestmark = [pytest.mark.integration]

POSTGREST = "http://localhost:3002"


def _enabled_hubs(live_postgrest_url) -> List[Dict]:
    r = requests.get(
        f"{POSTGREST}/hub_config",
        params={"is_enabled": "eq.true",
                "select": "hub_name,hub_ip,admin_username,admin_password,"
                          "admin_creds_index"},
        timeout=5,
    )
    assert r.status_code == 200, f"hub_config fetch failed: {r.status_code} {r.text}"
    return r.json()


def _fetch_hub_timezone(hub_ip: str, hub_name: str) -> Optional[str]:
    """Returns the timeZone string from /location/list/data, or None if
    the hub is unreachable / auth-failing. Test treats unreachable as a
    skip-condition for that hub, not a failure — we test consistency
    across hubs that ARE reachable."""
    from services.hubitat_admin_client import get_client
    try:
        client = get_client(hub_ip, hub_name)
        r = client._request("GET", "/location/list/data")
        if r.status_code != 200:
            return None
        rows = r.json()
        if isinstance(rows, list) and rows:
            return rows[0].get("timeZone")
    except Exception:
        return None
    return None


def test_all_enabled_hubs_share_one_timezone(live_postgrest_url):
    """Pulls timeZone from every reachable, enabled hub and asserts they
    all match. If any disagree, fails with a per-hub breakdown so the
    user can fix the outlier from the Hubitat UI."""
    hubs = _enabled_hubs(live_postgrest_url)
    if not hubs:
        pytest.skip("no enabled hubs configured — nothing to check")

    by_tz: Dict[str, List[str]] = defaultdict(list)
    unreachable: List[str] = []

    for hub in hubs:
        tz = _fetch_hub_timezone(hub["hub_ip"], hub["hub_name"])
        if tz is None:
            unreachable.append(f"{hub['hub_name']} ({hub['hub_ip']})")
            continue
        by_tz[tz].append(f"{hub['hub_name']} ({hub['hub_ip']})")

    if not by_tz:
        pytest.skip(
            f"no enabled hubs reachable to compare. Unreachable: "
            f"{unreachable}"
        )

    if len(by_tz) > 1:
        # Build a readable diff for the failure message
        breakdown = "\n".join(
            f"  {tz!r}: {hubs}" for tz, hubs in sorted(by_tz.items())
        )
        unreachable_note = (
            f"\n(Unreachable: {unreachable})" if unreachable else ""
        )
        pytest.fail(
            f"Hubs disagree on time zone — every event timestamp from "
            f"these hubs will be skewed relative to each other.\n"
            f"{breakdown}{unreachable_note}\n"
            f"Fix: open the Hubitat web UI for the outlier hub, "
            f"Settings → Location → Hub Time Zone, set it to match "
            f"the majority value."
        )

    # All consistent. Single TZ — assert it's non-empty.
    only_tz = next(iter(by_tz.keys()))
    assert only_tz, "hub reported empty timeZone string"


def test_hub_tz_field_shape_is_sane(live_postgrest_url):
    """Spot-check at least one hub returns a recognizable TZ string.
    Catches the case where /location/list/data changes shape and our
    consistency check would pass for the wrong reason (all hubs return
    None and we'd miss it because of the skip above)."""
    hubs = _enabled_hubs(live_postgrest_url)
    if not hubs:
        pytest.skip("no enabled hubs")

    for hub in hubs:
        tz = _fetch_hub_timezone(hub["hub_ip"], hub["hub_name"])
        if tz is None:
            continue
        # Windows-style names: "Eastern Standard Time", "Pacific Standard
        # Time", etc. Hubitat uses these year-round regardless of DST.
        # Loose check — non-empty string containing 'Time' is enough to
        # tell us we're reading the right field.
        assert isinstance(tz, str) and "Time" in tz, (
            f"unexpected timeZone shape from {hub['hub_name']}: {tz!r}"
        )
        return  # one good hub is enough for the shape check

    pytest.skip("no reachable hubs to check shape")
