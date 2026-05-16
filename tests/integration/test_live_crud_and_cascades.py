"""
Live PostgREST CRUD round-trip tests. Inserts rows tagged with __test__
so cleanup is easy and unambiguous.

What's verified:
  - INSERT into event_log with all new columns roundtrips correctly
  - event_routings FK constraint to event_log enforces referential integrity
  - event_routings rows cascade-delete when their event_log row is deleted
  - device_commands two-phase: INSERT pending → PATCH outcome
  - hub_health UPDATE roundtrips

Cleanup contract: every test inserts rows whose distinguishing column starts
with __test__ , and tests/integration/conftest.py purges those before AND
after each test.
"""

import json
from datetime import datetime, timezone

import pytest
import requests

TEST_PREFIX = "__test__"


@pytest.mark.integration
class TestEventLogInsertRoundtrip:
    def test_insert_with_all_new_columns(self, live_postgrest_url, cleanup_test_rows):
        device_name = f"{TEST_PREFIX}round_trip_motion"
        r = requests.post(
            f"{live_postgrest_url}/event_log",
            json={
                "hubitat_device_id": "99999",
                "device_name": device_name,
                "event_type": "motion",
                "event_value": "active",
                "hub_ip": "<LAN_IP>",
                "canonical_device_id": None,  # avoid FK; just verify columns
                "intake_path": "eventsocket",
                "processing_ms": 42,
            },
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=5,
        )
        assert r.status_code == 201, r.text
        rows = r.json()
        assert isinstance(rows, list) and len(rows) == 1
        row = rows[0]

        assert row["hub_ip"] == "<LAN_IP>"
        assert row["intake_path"] == "eventsocket"
        assert row["processing_ms"] == 42
        assert row["event_type"] == "motion"
        assert row["event_value"] == "active"

        # Roundtrip the row by id
        rid = row["id"]
        r2 = requests.get(
            f"{live_postgrest_url}/event_log",
            params={"id": f"eq.{rid}"},
            timeout=5,
        )
        assert r2.status_code == 200
        assert r2.json()[0]["device_name"] == device_name


@pytest.mark.integration
class TestEventRoutingsFK:
    def test_routing_with_nonexistent_event_id_is_rejected(
        self, live_postgrest_url, cleanup_test_rows,
    ):
        r = requests.post(
            f"{live_postgrest_url}/event_routings",
            json=[{
                "event_id": 999_999_999,  # doesn't exist
                "instance_id": None,
                "outcome": "dropped_orphan",
                "drop_reason": f"{TEST_PREFIX}fk-test",
            }],
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        # FK violation → 409 or 400 family
        assert r.status_code >= 400, f"Expected FK rejection, got {r.status_code}: {r.text}"

    def test_routings_cascade_delete_with_event_log(
        self, live_postgrest_url, cleanup_test_rows,
    ):
        device_name = f"{TEST_PREFIX}cascade_test"
        # Insert event_log
        r = requests.post(
            f"{live_postgrest_url}/event_log",
            json={
                "hubitat_device_id": "99999",
                "device_name": device_name,
                "event_type": "motion",
                "event_value": "active",
                "intake_path": "eventsocket",
            },
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=5,
        )
        assert r.status_code == 201
        event_id = r.json()[0]["id"]

        # Insert event_routings linked to it
        r = requests.post(
            f"{live_postgrest_url}/event_routings",
            json=[{
                "event_id": event_id,
                "instance_id": None,
                "outcome": "dropped_orphan",
                "drop_reason": f"{TEST_PREFIX}cascade",
            }],
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert r.status_code == 201, r.text

        # Verify the routing exists
        r = requests.get(
            f"{live_postgrest_url}/event_routings",
            params={"event_id": f"eq.{event_id}"},
            timeout=5,
        )
        assert r.status_code == 200
        assert len(r.json()) == 1

        # Delete the event_log row
        r = requests.delete(
            f"{live_postgrest_url}/event_log",
            params={"id": f"eq.{event_id}"},
            timeout=5,
        )
        assert r.status_code in (200, 204)

        # The routing should be gone (CASCADE)
        r = requests.get(
            f"{live_postgrest_url}/event_routings",
            params={"event_id": f"eq.{event_id}"},
            timeout=5,
        )
        assert r.status_code == 200
        assert r.json() == [], (
            "event_routings should have cascade-deleted with event_log"
        )


@pytest.mark.integration
class TestDeviceCommandsTwoPhaseLive:
    def test_insert_pending_then_patch_to_confirmed(
        self, live_postgrest_url, cleanup_test_rows,
    ):
        # Phase 1: INSERT 'pending'
        r = requests.post(
            f"{live_postgrest_url}/device_commands",
            json={
                "hubitat_device_id": f"{TEST_PREFIX}999",
                "hub_ip": "<LAN_IP>",
                "command": "on",
                "arguments": [],
                "outcome": "pending",
                "attempt": 1,
                "max_attempts": 1,
            },
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=5,
        )
        assert r.status_code == 201, r.text
        cmd_id = r.json()[0]["id"]

        # Phase 2: PATCH outcome → confirmed
        now_iso = datetime.now(timezone.utc).isoformat()
        r = requests.patch(
            f"{live_postgrest_url}/device_commands",
            params={"id": f"eq.{cmd_id}"},
            json={
                "outcome": "confirmed",
                "completed_at": now_iso,
                "final_observed_value": "on",
                "latency_ms": 250,
            },
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert r.status_code in (200, 204)

        # Verify final state
        r = requests.get(
            f"{live_postgrest_url}/device_commands",
            params={"id": f"eq.{cmd_id}"},
            timeout=5,
        )
        row = r.json()[0]
        assert row["outcome"] == "confirmed"
        assert row["final_observed_value"] == "on"
        assert row["latency_ms"] == 250
        assert row["completed_at"] is not None


@pytest.mark.integration
class TestHubHealthLiveUpdates:
    def test_can_patch_hub_health_row(self, live_postgrest_url):
        # Pick the first hub_health row that exists
        r = requests.get(
            f"{live_postgrest_url}/hub_health",
            params={"select": "hub_id", "limit": "1"},
            timeout=5,
        )
        rows = r.json()
        if not rows:
            pytest.skip("hub_health empty — migration seeding didn't run")
        hub_id = rows[0]["hub_id"]

        now_iso = datetime.now(timezone.utc).isoformat()
        r = requests.patch(
            f"{live_postgrest_url}/hub_health",
            params={"hub_id": f"eq.{hub_id}"},
            json={
                "ws_last_event_at": now_iso,
                "updated_at": now_iso,
            },
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert r.status_code in (200, 204)

        # Verify the update took effect
        r = requests.get(
            f"{live_postgrest_url}/hub_health",
            params={"hub_id": f"eq.{hub_id}"},
            timeout=5,
        )
        row = r.json()[0]
        assert row["ws_last_event_at"] is not None
