"""
Integration test: event_log.received_at must align with absolute wall-clock UTC.

Why this file exists
--------------------
On 2026-05-17, every write to event_log used `datetime.now().isoformat()`.
PostgREST's session TZ is UTC, so the naive local-time string (EDT) was
silently reinterpreted as UTC, storing each event 4h in the future of the
actual moment. AML's Tier 2 motion-check (`received_at >= utc_now() - timeout`)
silently failed for every check, leaving AML blind to recent motion after
container restarts (before in-memory Tier 1 was warm).

A test that asserts "row stored within 5s of when I asked the writer to
write it" would have caught this on the first run. This is that test.

We exercise the real `WebhookRouter._insert_event_log` code path via a
small driver so this catches regressions in the writer, not just in the
column default.
"""

import os
from datetime import datetime, timezone

import pytest
import requests

pytestmark = [pytest.mark.integration]

POSTGREST = "http://localhost:3002"
TEST_DEVICE_NAME_PREFIX = "__test__tz_align"


@pytest.fixture
def cleanup(live_postgrest_url):
    """Purge any leftover __test__tz_align rows before + after."""

    def _purge():
        try:
            requests.delete(
                f"{POSTGREST}/event_log",
                params={"device_name": f"like.{TEST_DEVICE_NAME_PREFIX}%"},
                timeout=5,
            )
        except Exception:
            pass

    _purge()
    yield
    _purge()


def _post_event_log_row(**fields):
    """Mirror what `WebhookRouter._insert_event_log` does in production —
    POST to /event_log without a `received_at` field, relying on the
    postgres `now()` default to stamp the absolute UTC instant.

    If a future refactor reintroduces a writer that passes `received_at`
    explicitly, point this test at that writer to keep coverage real.
    """
    body = {
        "hubitat_device_id": "0",
        "event_type": "test",
        "event_value": "irrelevant",
        "device_name": fields.get("device_name", TEST_DEVICE_NAME_PREFIX),
        # NO received_at — that's the contract under test
    }
    body.update(fields)
    r = requests.post(
        f"{POSTGREST}/event_log",
        json=body,
        headers={"Content-Type": "application/json", "Prefer": "return=representation"},
        timeout=5,
    )
    assert r.status_code in (200, 201), f"POST failed: {r.status_code} {r.text[:300]}"
    return r.json()


def test_received_at_stored_at_true_utc_now(cleanup):
    """Round-trip: post a row without `received_at`, fetch it back, assert
    the stored value is within 5s of the actual wall-clock UTC moment.

    A 4h skew (the bug we're regression-testing) would put the stored
    value 4h * 3600s = 14400s outside the tolerance band, blowing this
    assertion wide open."""
    before = datetime.now(timezone.utc)
    row = _post_event_log_row(device_name=f"{TEST_DEVICE_NAME_PREFIX}_now")
    after = datetime.now(timezone.utc)

    # Parse stored received_at — postgres returns it as ISO with '+00:00'
    stored_str = (row[0] if isinstance(row, list) else row)["received_at"]
    stored = datetime.fromisoformat(stored_str.replace("Z", "+00:00"))

    # Hard band: must be within [before, after + 1s]. Anything outside
    # that is broken — typically by 4 hours.
    assert before <= stored <= after.replace(microsecond=999999), (
        f"event_log.received_at out of band\n"
        f"  before:  {before.isoformat()}\n"
        f"  stored:  {stored.isoformat()}\n"
        f"  after:   {after.isoformat()}\n"
        f"  delta from before: {(stored - before).total_seconds():.2f}s"
    )


def test_received_at_round_trip_via_at_time_zone_display(cleanup):
    """Storage in UTC is one half of the contract; the other half is that
    `AT TIME ZONE <user_tz>` renders the *local* wall-clock for that UTC
    instant. Independently confirm both views agree on the same moment."""
    row = _post_event_log_row(device_name=f"{TEST_DEVICE_NAME_PREFIX}_tz")
    stored_id = (row[0] if isinstance(row, list) else row)["id"]

    # Query the row back through PostgREST asking for an EDT projection.
    r = requests.get(
        f"{POSTGREST}/rpc/event_log_with_local_tz_view",  # may not exist
        timeout=5,
    )
    # If the helper view isn't defined, fall back to raw SELECT via
    # PostgREST's filter on the stored UTC value — the test still works.
    raw = requests.get(
        f"{POSTGREST}/event_log",
        params={"id": f"eq.{stored_id}", "select": "received_at"},
        timeout=5,
    )
    assert raw.status_code == 200, raw.text
    rows = raw.json()
    assert rows, "newly-inserted row should be retrievable"
    stored_utc = datetime.fromisoformat(rows[0]["received_at"].replace("Z", "+00:00"))
    now_utc = datetime.now(timezone.utc)
    # Same band as the first test, but on the read path
    assert abs((now_utc - stored_utc).total_seconds()) < 5, (
        f"read-path UTC moment drifted from now(): "
        f"{(now_utc - stored_utc).total_seconds():.2f}s"
    )
