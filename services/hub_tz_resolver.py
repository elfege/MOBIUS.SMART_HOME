"""
Hub-derived timezone resolution.

Why this exists
---------------
Hubitat hubs each carry their own location TZ (Settings → Location → Hub
Time Zone). Mobius previously had a user-settable `timezone` system
setting that drifted from the hubs when the user changed one place but
not the other. Per design decision 2026-05-17 (user directive), the
hubs are now the authoritative source: Mobius queries every enabled hub
and uses the agreed-upon TZ. Inconsistency surfaces as a warning so the
user knows which hub to reconfigure from the Hubitat UI.

Mechanics
---------
Each hub's `/location/list/data` returns a Windows-style TZ string like
"Eastern Standard Time" — used year-round regardless of DST. We map it
to an IANA name (e.g. "America/New_York") for Python's `os.environ['TZ']
+ time.tzset()` and for postgres `AT TIME ZONE` queries.

If hubs disagree, we pick the majority (with first-found as the tie-
breaker on a tie) and emit a warning. The breakdown is persisted to
`system_settings.hub_tz_inconsistency` so the dashboard can surface it.

Fallback chain (in order):
  1. Reachable hubs agree → use that TZ
  2. Reachable hubs disagree → use majority, warn, persist breakdown
  3. No hubs reachable → return None, caller falls back to whatever's
     already in `system_settings.timezone`
"""

import logging
import os
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Windows-style TZ → IANA mapping
# ---------------------------------------------------------------------------
#
# Hubitat reports Windows TZ names like "Eastern Standard Time" — these are
# DST-static (no separate "Eastern Daylight Time" string), so a single entry
# covers both halves of the year. The IANA TZ database handles the DST
# transition; we just need the right zone name.
#
# This list is the common-case North-America + Europe set Elfege uses; extend
# as needed. Unknown Windows TZ strings fall through to None and the caller
# logs a warning so the mapping can be expanded.
WINDOWS_TZ_TO_IANA: Dict[str, str] = {
    # North America
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",  # Arizona, no DST
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    # Europe
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Bucharest",
    # UTC
    "UTC": "Etc/UTC",
    "Coordinated Universal Time": "Etc/UTC",
}


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _enabled_hubs() -> List[Dict]:
    """Pull every is_enabled hub_config row. Returns [] on failure."""
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        r = requests.get(
            f"{pg}/hub_config",
            params={"is_enabled": "eq.true",
                    "select": "hub_name,hub_ip"},
            timeout=3,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug(f"hub_tz_resolver: hub_config fetch failed: {e}")
    return []


def _query_hub_tz(hub_ip: str, hub_name: str) -> Optional[str]:
    """Returns the raw Windows-style TZ string from one hub, or None."""
    try:
        # Import here so this module is safe to import early during boot
        # before the admin client's transitive deps are ready.
        from services.hubitat_admin_client import get_client
        client = get_client(hub_ip, hub_name)
        r = client._request("GET", "/location/list/data")
        if r.status_code != 200:
            return None
        rows = r.json()
        if isinstance(rows, list) and rows:
            return rows[0].get("timeZone")
    except Exception as e:
        logger.debug(
            f"hub_tz_resolver: query failed for {hub_name} ({hub_ip}): {e}"
        )
    return None


def resolve_hub_timezone() -> Tuple[Optional[str], bool, Dict[str, str]]:
    """
    Returns (iana_tz, consistent, per_hub_breakdown).

    iana_tz:
      Resolved IANA timezone string (e.g. "America/New_York"). None if
      no hub is reachable OR if every reachable hub returned an
      unmappable Windows TZ.

    consistent:
      True if every reachable hub returned the same Windows TZ string.
      False if they disagreed (in which case iana_tz is the majority
      pick and the caller should warn).

    per_hub_breakdown:
      Dict of hub_name → Windows TZ string (or "unreachable" for hubs
      that didn't respond, "unmapped:<x>" if the Windows TZ is not in
      our IANA mapping). Useful for surfacing the disagreement on a
      dashboard or in logs.
    """
    hubs = _enabled_hubs()
    if not hubs:
        return (None, True, {})

    per_hub: Dict[str, str] = {}
    win_tz_count: Counter = Counter()

    for hub in hubs:
        win_tz = _query_hub_tz(hub["hub_ip"], hub["hub_name"])
        if win_tz is None:
            per_hub[hub["hub_name"]] = "unreachable"
            continue
        per_hub[hub["hub_name"]] = win_tz
        win_tz_count[win_tz] += 1

    if not win_tz_count:
        # No hub reachable
        return (None, True, per_hub)

    consistent = (len(win_tz_count) == 1)

    # Majority pick. On a tie, Counter.most_common preserves insertion
    # order — which is the iteration order over `hubs` — so the first
    # hub's TZ wins. Stable, predictable.
    majority_win_tz, _ = win_tz_count.most_common(1)[0]
    iana = WINDOWS_TZ_TO_IANA.get(majority_win_tz)

    if iana is None:
        # The hub returned a Windows TZ we don't know how to map.
        # Mark the breakdown so the user can extend the table.
        for hub_name, val in per_hub.items():
            if val == majority_win_tz:
                per_hub[hub_name] = f"unmapped:{majority_win_tz}"
        logger.warning(
            f"hub_tz_resolver: Windows TZ {majority_win_tz!r} has no IANA "
            f"mapping. Extend services.hub_tz_resolver.WINDOWS_TZ_TO_IANA. "
            f"Breakdown: {per_hub}"
        )
        return (None, consistent, per_hub)

    return (iana, consistent, per_hub)


def apply_resolved_timezone_to_environment(iana_tz: str) -> bool:
    """Sets `os.environ['TZ']` and calls `time.tzset()`. Returns True
    if the apply was clean, False if `tzset` raised.

    Best-effort: a bad TZ string just leaves the previous setting in
    place (Python's `tzset()` silently ignores invalid TZ strings on
    glibc; we re-set os.environ defensively)."""
    import time as _t
    try:
        os.environ["TZ"] = iana_tz
        _t.tzset()
        return True
    except Exception as e:
        logger.warning(f"hub_tz_resolver: tzset({iana_tz!r}) failed: {e}")
        return False


def persist_resolved_timezone(iana_tz: Optional[str],
                              consistent: bool,
                              per_hub: Dict[str, str]) -> None:
    """Write the resolution result back to system_settings so the
    dashboard can read it without re-querying the hubs.

    Keys touched:
      timezone                  — the active IANA TZ (cache of resolver
                                  output; if hubs were unreachable, the
                                  previous value is left intact).
      hub_tz_inconsistency      — 'true' if hubs disagreed, else 'false'
      hub_tz_breakdown          — JSON string of {hub_name: tz_or_status}

    Failures are non-fatal — logged and swallowed."""
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    import json
    updates: List[Tuple[str, str]] = [
        ("hub_tz_inconsistency", "true" if not consistent else "false"),
        ("hub_tz_breakdown", json.dumps(per_hub)),
    ]
    if iana_tz:
        updates.append(("timezone", iana_tz))

    for key, value in updates:
        try:
            r = requests.patch(
                f"{pg}/system_settings",
                params={"key": f"eq.{key}"},
                json={"value": value},
                headers={"Content-Type": "application/json"},
                timeout=3,
            )
            if r.status_code not in (200, 204):
                logger.debug(
                    f"hub_tz_resolver: PATCH {key} → HTTP {r.status_code}"
                )
        except Exception as e:
            logger.debug(f"hub_tz_resolver: PATCH {key} failed: {e}")
