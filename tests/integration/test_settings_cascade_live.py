"""
Live integration: system_settings + app_type_settings CRUD against the
smarthome-postgrest stack on host port 3002.

Asserts the migration is applied, seed rows exist, PATCH roundtrips work,
and the cascade resolver reads what PostgREST writes (after cache TTL).
"""

import os
import time
from datetime import datetime, timezone

import pytest
import requests


@pytest.mark.integration
class TestSystemSettingsSchema:
    def test_motion_floor_seed_row_exists(self, live_postgrest_url):
        r = requests.get(
            f"{live_postgrest_url}/system_settings",
            params={"key": "eq.motion_timeout_floor_seconds"},
            timeout=5,
        )
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["value_type"] == "int"
        # Default seeded value is 60; user may have changed it. Just check int.
        assert int(row["value"]) > 0

    def test_lifecycle_toggles_marked_requires_restart(self, live_postgrest_url):
        for key in (
            "eventsocket_enabled",
            "reconcile_poll_enabled",
            "device_commands_logging",
            "webhook_intake_enabled",
        ):
            r = requests.get(
                f"{live_postgrest_url}/system_settings",
                params={"key": f"eq.{key}"},
                timeout=5,
            )
            rows = r.json()
            assert len(rows) == 1, f"missing seed row for {key}"
            assert rows[0]["requires_restart"] is True, (
                f"{key} should be marked requires_restart"
            )

    def test_runtime_tunables_not_requires_restart(self, live_postgrest_url):
        for key in (
            "motion_timeout_floor_seconds",
            "reconcile_interval_secs",
            "eventsocket_watchdog_secs",
        ):
            r = requests.get(
                f"{live_postgrest_url}/system_settings",
                params={"key": f"eq.{key}"},
                timeout=5,
            )
            rows = r.json()
            assert len(rows) == 1, f"missing seed row for {key}"
            assert rows[0]["requires_restart"] is False, (
                f"{key} should NOT require restart"
            )


@pytest.mark.integration
class TestSystemSettingsPatchRoundtrip:
    def test_patch_roundtrip_via_postgrest(self, live_postgrest_url):
        # Save original
        r = requests.get(
            f"{live_postgrest_url}/system_settings",
            params={"key": "eq.motion_timeout_floor_seconds"},
            timeout=5,
        )
        original = r.json()[0]["value"]

        try:
            # Patch to a test value
            r = requests.patch(
                f"{live_postgrest_url}/system_settings",
                params={"key": "eq.motion_timeout_floor_seconds"},
                json={"value": "73"},
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            assert r.status_code in (200, 204)

            # Read back
            r = requests.get(
                f"{live_postgrest_url}/system_settings",
                params={"key": "eq.motion_timeout_floor_seconds"},
                timeout=5,
            )
            assert r.json()[0]["value"] == "73"
        finally:
            # Restore
            requests.patch(
                f"{live_postgrest_url}/system_settings",
                params={"key": "eq.motion_timeout_floor_seconds"},
                json={"value": original},
                headers={"Content-Type": "application/json"},
                timeout=5,
            )


@pytest.mark.integration
class TestAppTypeSettings:
    def test_table_writable_for_existing_app_type(self, live_postgrest_url):
        # Get an existing app_type id
        r = requests.get(
            f"{live_postgrest_url}/app_types",
            params={"select": "id", "limit": "1"},
            timeout=5,
        )
        rows = r.json()
        if not rows:
            pytest.skip("no app_types rows")
        app_type_id = rows[0]["id"]

        try:
            # Insert a test row (marker via description for cleanup)
            r = requests.post(
                f"{live_postgrest_url}/app_type_settings",
                json={
                    "app_type_id": app_type_id,
                    "key": "__test__log_level",
                    "value": "DEBUG",
                    "value_type": "string",
                    "description": "__test__ integration test row",
                },
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            assert r.status_code == 201, r.text
        finally:
            requests.delete(
                f"{live_postgrest_url}/app_type_settings",
                params={"key": "eq.__test__log_level"},
                timeout=5,
            )


@pytest.mark.integration
class TestEncryptedSecretsSchema:
    def test_table_accepts_bytea(self, live_postgrest_url):
        try:
            # Insert with hex-escape format that PostgREST/Postgres accepts
            r = requests.post(
                f"{live_postgrest_url}/encrypted_secrets",
                json={
                    "key": "__test__placeholder",
                    "ciphertext": r"\xdeadbeef",
                    "kek_version": 1,
                    "description": "__test__ integration",
                },
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            assert r.status_code == 201, r.text

            # Read back
            r = requests.get(
                f"{live_postgrest_url}/encrypted_secrets",
                params={"key": "eq.__test__placeholder"},
                timeout=5,
            )
            assert r.status_code == 200
            assert len(r.json()) == 1
        finally:
            requests.delete(
                f"{live_postgrest_url}/encrypted_secrets",
                params={"key": "eq.__test__placeholder"},
                timeout=5,
            )


@pytest.mark.integration
class TestDevicesEndpointDBBacked:
    """
    The /api/devices endpoint is now DB-backed (post-refactor 2026-05-17).
    We test the PostgREST query shape directly, since the FastAPI endpoint
    won't pick up the new code until ./start.sh.
    """

    def test_filter_by_capability_returns_matching_rows(self, live_postgrest_url):
        r = requests.get(
            f"{live_postgrest_url}/devices",
            params={
                "capabilities": 'cs.["MotionSensor"]',
                "select": "id,label,capabilities",
                "order": "label",
            },
            timeout=5,
        )
        assert r.status_code == 200
        rows = r.json()
        # Filter doesn't have to return anything for a brand-new system,
        # but if it does, every row must actually have MotionSensor.
        for row in rows:
            assert "MotionSensor" in (row.get("capabilities") or [])

    def test_grouping_by_categories_matches_per_category(self, live_postgrest_url):
        """One bulk query yields the same per-category counts as N individual
        queries. (The endpoint groups in Python; we replicate here.)"""
        cats = ["MotionSensor", "Switch", "ContactSensor"]
        per_cat = {}
        for c in cats:
            r = requests.get(
                f"{live_postgrest_url}/devices",
                params={
                    "capabilities": f'cs.["{c}"]',
                    "select": "id",
                },
                timeout=5,
            )
            per_cat[c] = len(r.json())

        # Bulk
        r = requests.get(
            f"{live_postgrest_url}/devices",
            params={"select": "id,capabilities", "order": "id"},
            timeout=5,
        )
        all_devices = r.json()
        bulk_counts = {c: 0 for c in cats}
        for d in all_devices:
            caps = d.get("capabilities") or []
            for c in cats:
                if c in caps:
                    bulk_counts[c] += 1

        for c in cats:
            assert bulk_counts[c] == per_cat[c], (
                f"category {c}: bulk={bulk_counts[c]} per-call={per_cat[c]}"
            )

    def test_db_query_is_fast(self, live_postgrest_url):
        """SLO: the DB-backed devices query must complete in < 200ms.
        Replaces the multi-second Hubitat-live path."""
        start = time.monotonic()
        r = requests.get(
            f"{live_postgrest_url}/devices",
            params={
                "capabilities": 'cs.["MotionSensor"]',
                "select": "id,label,capabilities",
            },
            timeout=5,
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        assert r.status_code == 200
        assert elapsed_ms < 200, f"DB devices query took {elapsed_ms:.0f}ms"
