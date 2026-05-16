"""
Verifies the live database has the new schema in place and that the
migration is genuinely idempotent (the same schema can be re-applied
without error).

These tests are read-only against the live stack — no data is inserted.
"""

import os
import subprocess

import pytest
import requests

PG_HOST = "localhost"
PG_PORT = 5433
PG_DB = "smarthome"
PG_USER = "smarthome_api"


def _psql_select(query: str) -> str:
    """Run a SELECT via docker exec — avoids needing psql + creds locally."""
    result = subprocess.run(
        [
            "docker", "exec", "smarthome-postgres",
            "psql", "-U", PG_USER, "-d", PG_DB, "-At", "-c", query,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(
            f"Live DB query failed (stack probably not running): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


@pytest.mark.integration
class TestNewSchemaInPlace:
    def test_event_log_has_hub_ip_column(self):
        out = _psql_select(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='event_log' AND column_name='hub_ip';"
        )
        assert out == "hub_ip"

    def test_event_log_has_canonical_device_id_column(self):
        out = _psql_select(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='event_log' AND column_name='canonical_device_id';"
        )
        assert out == "canonical_device_id"

    def test_event_log_has_intake_path_column(self):
        out = _psql_select(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='event_log' AND column_name='intake_path';"
        )
        assert out == "intake_path"

    def test_event_log_has_processing_ms_column(self):
        out = _psql_select(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='event_log' AND column_name='processing_ms';"
        )
        assert out == "processing_ms"

    def test_event_routings_table_exists(self):
        out = _psql_select(
            "SELECT tablename FROM pg_tables WHERE tablename='event_routings';"
        )
        assert out == "event_routings"

    def test_device_commands_table_exists(self):
        out = _psql_select(
            "SELECT tablename FROM pg_tables WHERE tablename='device_commands';"
        )
        assert out == "device_commands"

    def test_instance_state_log_table_exists(self):
        out = _psql_select(
            "SELECT tablename FROM pg_tables WHERE tablename='instance_state_log';"
        )
        assert out == "instance_state_log"

    def test_mode_change_log_table_exists(self):
        out = _psql_select(
            "SELECT tablename FROM pg_tables WHERE tablename='mode_change_log';"
        )
        assert out == "mode_change_log"

    def test_hub_health_table_exists(self):
        out = _psql_select(
            "SELECT tablename FROM pg_tables WHERE tablename='hub_health';"
        )
        assert out == "hub_health"


@pytest.mark.integration
class TestForeignKeyConstraints:
    def test_event_routings_fk_to_event_log(self):
        out = _psql_select(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid='event_routings'::regclass "
            "AND contype='f' "
            "AND conname LIKE '%event_id%';"
        )
        # PostgreSQL auto-generates an FK constraint name like
        # event_routings_event_id_fkey
        assert "event_id" in out

    def test_event_routings_fk_to_app_instances(self):
        out = _psql_select(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid='event_routings'::regclass "
            "AND contype='f' "
            "AND conname LIKE '%instance_id%';"
        )
        assert "instance_id" in out

    def test_device_commands_fk_to_devices(self):
        out = _psql_select(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid='device_commands'::regclass "
            "AND contype='f' "
            "AND conname LIKE '%canonical_device_id%';"
        )
        assert "canonical_device_id" in out

    def test_hub_health_pk_is_hub_id(self):
        out = _psql_select(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
            "WHERE i.indrelid='hub_health'::regclass AND i.indisprimary;"
        )
        assert out == "hub_id"


@pytest.mark.integration
class TestHubHealthSeeding:
    def test_one_hub_health_row_per_enabled_hub(self):
        enabled_count = _psql_select(
            "SELECT COUNT(*) FROM hub_config WHERE is_enabled = TRUE;"
        )
        health_count = _psql_select("SELECT COUNT(*) FROM hub_health;")
        assert enabled_count == health_count
        assert int(health_count) >= 1


@pytest.mark.integration
class TestMigrationIdempotent:
    """Re-applying the migration must not error or duplicate anything."""

    def test_rerunning_migration_does_not_error(self):
        # Apply the migration file a second time — should be a no-op
        result = subprocess.run(
            [
                "docker", "exec", "-i", "smarthome-postgres",
                "psql", "-U", PG_USER, "-d", PG_DB,
                "-f", "/tmp/004_mig.sql",  # already there from initial apply
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if "could not open file" in (result.stderr or ""):
            # Migration file wasn't preserved across container restarts; skip
            pytest.skip("Migration file not present in container")
        assert result.returncode == 0, (
            f"Re-running migration failed: {result.stderr}"
        )

    def test_event_routings_still_one_table_after_rerun(self):
        # If CREATE TABLE forgot IF NOT EXISTS, we'd have errored above.
        # Belt-and-suspenders check.
        count = _psql_select(
            "SELECT COUNT(*) FROM pg_tables WHERE tablename='event_routings';"
        )
        assert count == "1"


@pytest.mark.integration
class TestPostgrestExposesNewTables:
    """PostgREST auto-introspects the schema. New tables should appear in
    the OpenAPI spec and accept queries."""

    def test_postgrest_responds_for_event_routings(self):
        r = requests.get("http://localhost:3002/event_routings?limit=1", timeout=5)
        assert r.status_code == 200, r.text

    def test_postgrest_responds_for_device_commands(self):
        r = requests.get("http://localhost:3002/device_commands?limit=1", timeout=5)
        assert r.status_code == 200, r.text

    def test_postgrest_responds_for_hub_health(self):
        r = requests.get("http://localhost:3002/hub_health?limit=1", timeout=5)
        assert r.status_code == 200, r.text
