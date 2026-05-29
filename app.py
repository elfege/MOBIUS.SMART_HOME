"""
0_MOBIUS.SMART_HOME FastAPI Application

Main entry point for the smart home automation system.
Provides REST API for instance management, device access, and webhook handling.
Serves Jinja2 templates for the web UI.
"""

import os
import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, Query, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Defensive HTTP wrapper: offloads blocking `requests.*` calls onto a worker
# thread so an unresponsive PostgREST / Hubitat hub can't hang the event loop.
# Used by every FastAPI route below that talks to PostgREST. See
# services/http_sync_offload.py for the rationale and the 2026-05-27 incident.
from services.http_sync_offload import aget, apost, apatch, adelete

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# =============================================================================
# Apply user-configured timezone BEFORE logging is configured, so every log
# line uses the user's local time. Reads system_settings.timezone via a
# direct psycopg2 connection (the resolver isn't initialized yet at this
# point in module load). Falls back to UTC silently if anything fails.
# =============================================================================
def _apply_user_timezone():
    try:
        import time as _t
        import psycopg2  # noqa: WPS433
        conn = psycopg2.connect(
            host=os.environ.get('POSTGRES_HOST', 'postgres'),
            port=os.environ.get('POSTGRES_PORT', '5432'),
            dbname=os.environ.get('POSTGRES_DB', 'smarthome'),
            user=os.environ.get('POSTGRES_USER', 'smarthome_api'),
            password=os.environ.get('POSTGRES_PASSWORD', ''),
            connect_timeout=3,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT value FROM system_settings WHERE key = %s",
                ('timezone',),
            )
            row = cur.fetchone()
            if row and row[0]:
                os.environ['TZ'] = row[0]
                _t.tzset()
            cur.close()
        finally:
            conn.close()
    except Exception:
        # Boot-time best-effort. If DB isn't reachable yet, app continues
        # in UTC and run_db_migrations will create the row on first run.
        pass


_apply_user_timezone()


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class CreateInstanceRequest(BaseModel):
    """Request body for creating a new automation instance."""
    app_type: str
    label: str
    device_selections: dict
    settings: dict = {}


class UpdateInstanceRequest(BaseModel):
    """Request body for updating an existing automation instance."""
    label: Optional[str] = None
    device_selections: Optional[dict] = None
    settings: Optional[dict] = None


class PauseInstanceRequest(BaseModel):
    """Request body for pausing an instance."""
    duration_minutes: Optional[int] = None
    reason: Optional[str] = None


class DeviceCommandRequest(BaseModel):
    """Request body for sending a command to a device."""
    command: str
    args: Optional[list] = None


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def initialize_services():
    """Initialize all services on startup."""
    logger.info("Initializing services...")

    # Initialize app registry
    from apps.app_registry import initialize_registry
    from services.instance_manager import get_instance_manager

    instance_manager = get_instance_manager()
    initialize_registry(instance_manager)

    # Initialize scheduler
    from services.scheduler_service import get_scheduler
    get_scheduler()

    # Load all instances
    instance_manager.initialize_all_instances()

    logger.info("Services initialized")


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------


def run_db_migrations():
    """
    Run lightweight ALTER TABLE migrations on startup via psycopg2.

    These are idempotent (IF NOT EXISTS) so safe to run every boot.
    Needed because init-db.sql only runs on first DB creation.
    """
    import psycopg2

    db_host = os.environ.get('POSTGRES_HOST', 'postgres')
    db_port = os.environ.get('POSTGRES_PORT', '5432')
    db_name = os.environ.get('POSTGRES_DB', 'smarthome')
    db_user = os.environ.get('POSTGRES_USER', 'smarthome_api')
    db_pass = os.environ.get('POSTGRES_PASSWORD', '')

    migrations = [
        # Commission retry tracking columns (added 2026-02-22)
        "ALTER TABLE hubitat_matter_devices "
        "ADD COLUMN IF NOT EXISTS commission_attempts INTEGER DEFAULT 0",
        "ALTER TABLE hubitat_matter_devices "
        "ADD COLUMN IF NOT EXISTS last_commission_attempt TIMESTAMPTZ",
        "ALTER TABLE hubitat_matter_devices "
        "ADD COLUMN IF NOT EXISTS last_commission_error TEXT",

        # Device hub mapping table for native-hub command routing (added 2026-02-28)
        """CREATE TABLE IF NOT EXISTS device_hub_mapping (
            device_label VARCHAR(200) NOT NULL,
            native_hub_name VARCHAR(100) NOT NULL,
            native_hub_ip VARCHAR(50) NOT NULL,
            native_device_id VARCHAR(50) NOT NULL,
            protocol VARCHAR(30) NOT NULL DEFAULT 'unknown',
            device_type VARCHAR(200),
            mirrors JSONB DEFAULT '{}',
            is_mesh_linked BOOLEAN DEFAULT false,
            last_classified_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (device_label, native_hub_name)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_device_hub_mapping_label "
        "ON device_hub_mapping(device_label)",
        "CREATE INDEX IF NOT EXISTS idx_device_hub_mapping_hub "
        "ON device_hub_mapping(native_hub_name)",
        "CREATE INDEX IF NOT EXISTS idx_device_hub_mapping_protocol "
        "ON device_hub_mapping(protocol)",

        # Seed all hub configs
        "INSERT INTO hub_config (hub_name, hub_ip, maker_api_app_number, "
        "maker_api_token_env, is_primary) "
        "VALUES ('home_1', '<LAN_IP>', '1717', "
        "'HUBITAT_API_TOKEN_OTHER_HUB_1', false) "
        "ON CONFLICT (hub_name) DO NOTHING",
        "INSERT INTO hub_config (hub_name, hub_ip, maker_api_app_number, "
        "maker_api_token_env, is_primary) "
        "VALUES ('home_2', '<LAN_IP>', '2151', "
        "'HUBITAT_API_TOKEN_OTHER_HUB_2', false) "
        "ON CONFLICT (hub_name) DO NOTHING",
        "INSERT INTO hub_config (hub_name, hub_ip, maker_api_app_number, "
        "maker_api_token_env, is_primary) "
        "VALUES ('home_3', '<LAN_IP>', '1269', "
        "'HUBITAT_API_TOKEN_OTHER_HUB_3', false) "
        "ON CONFLICT (hub_name) DO NOTHING",

        # Grant PostgREST access to new table
        "GRANT SELECT, INSERT, UPDATE, DELETE ON device_hub_mapping TO smarthome_anon",

        # ====================================================================
        # 2026-05-16 — Full data-oriented traceability (eventsocket SOT)
        # Canonical source: psql/migrations/004_full_traceability_2026_05_16.sql
        # ====================================================================

        # event_log gets proper provenance columns.
        "ALTER TABLE event_log ADD COLUMN IF NOT EXISTS hub_ip VARCHAR(50)",
        "ALTER TABLE event_log ADD COLUMN IF NOT EXISTS canonical_device_id BIGINT REFERENCES devices(id)",
        "ALTER TABLE event_log ADD COLUMN IF NOT EXISTS intake_path VARCHAR(20)",
        "ALTER TABLE event_log ADD COLUMN IF NOT EXISTS processing_ms INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_event_log_canonical ON event_log(canonical_device_id, received_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_event_log_intake_time ON event_log(intake_path, received_at DESC)",

        # event_routings: M:N join replacing event_log.routed_to_instances JSONB.
        """CREATE TABLE IF NOT EXISTS event_routings (
            id           BIGSERIAL PRIMARY KEY,
            event_id     BIGINT NOT NULL REFERENCES event_log(id) ON DELETE CASCADE,
            instance_id  BIGINT REFERENCES app_instances(id) ON DELETE SET NULL,
            enqueued_at  TIMESTAMPTZ DEFAULT NOW(),
            processed_at TIMESTAMPTZ,
            outcome      VARCHAR(30) NOT NULL,
            drop_reason  TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_event_routings_event ON event_routings(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_event_routings_instance ON event_routings(instance_id, enqueued_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_event_routings_outcome ON event_routings(outcome, enqueued_at DESC)",

        # device_commands: every outbound command, two-phase (issue + completion).
        """CREATE TABLE IF NOT EXISTS device_commands (
            id                       BIGSERIAL PRIMARY KEY,
            instance_id              BIGINT REFERENCES app_instances(id) ON DELETE SET NULL,
            canonical_device_id      BIGINT REFERENCES devices(id) ON DELETE SET NULL,
            hubitat_device_id        VARCHAR(50),
            hub_ip                   VARCHAR(50),
            command                  VARCHAR(50) NOT NULL,
            arguments                JSONB DEFAULT '[]'::jsonb,
            desired_attribute        VARCHAR(50),
            desired_value            VARCHAR(200),
            triggered_by_event_id    BIGINT REFERENCES event_log(id) ON DELETE SET NULL,
            parent_command_id        BIGINT REFERENCES device_commands(id) ON DELETE SET NULL,
            attempt                  INTEGER DEFAULT 1,
            max_attempts             INTEGER DEFAULT 1,
            issued_at                TIMESTAMPTZ DEFAULT NOW(),
            completed_at             TIMESTAMPTZ,
            outcome                  VARCHAR(30) DEFAULT 'pending',
            final_observed_value     VARCHAR(200),
            verify_retries_used      INTEGER,
            latency_ms               INTEGER,
            error                    TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_device_commands_device ON device_commands(canonical_device_id, issued_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_device_commands_instance ON device_commands(instance_id, issued_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_device_commands_outcome ON device_commands(outcome, issued_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_device_commands_trig ON device_commands(triggered_by_event_id)",
        "CREATE INDEX IF NOT EXISTS idx_device_commands_parent ON device_commands(parent_command_id)",

        # instance_state_log: pause/resume/mode/settings transitions.
        """CREATE TABLE IF NOT EXISTS instance_state_log (
            id          BIGSERIAL PRIMARY KEY,
            instance_id BIGINT NOT NULL REFERENCES app_instances(id) ON DELETE CASCADE,
            transition  VARCHAR(40) NOT NULL,
            details     JSONB DEFAULT '{}'::jsonb,
            actor       VARCHAR(60),
            occurred_at TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_instance_state_log_instance ON instance_state_log(instance_id, occurred_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_instance_state_log_transition ON instance_state_log(transition, occurred_at DESC)",

        # mode_change_log: hub location-mode timeline.
        """CREATE TABLE IF NOT EXISTS mode_change_log (
            id                 BIGSERIAL PRIMARY KEY,
            mode_name          VARCHAR(60) NOT NULL,
            became_active_at   TIMESTAMPTZ DEFAULT NOW(),
            became_inactive_at TIMESTAMPTZ,
            source             VARCHAR(40)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_mode_change_log_active ON mode_change_log(became_active_at DESC)",

        # hub_health: per-hub WS connection + traffic + reconcile heartbeat.
        """CREATE TABLE IF NOT EXISTS hub_health (
            hub_id                  INTEGER PRIMARY KEY REFERENCES hub_config(id) ON DELETE CASCADE,
            ws_connected            BOOLEAN DEFAULT FALSE,
            ws_connected_since      TIMESTAMPTZ,
            ws_last_event_at        TIMESTAMPTZ,
            ws_last_failure_at      TIMESTAMPTZ,
            ws_last_failure_reason  TEXT,
            ws_consecutive_failures INTEGER DEFAULT 0,
            ws_reconnects_24h       INTEGER DEFAULT 0,
            ws_events_received_24h  BIGINT DEFAULT 0,
            last_reconcile_at       TIMESTAMPTZ,
            last_reconcile_diffs    INTEGER DEFAULT 0,
            updated_at              TIMESTAMPTZ DEFAULT NOW()
        )""",

        # Admin-API contract-drift watch (2026-05-26). Firmware 2.5.0.143
        # changed POST /device/runmethod from form-encoded to JSON with no
        # backward compat, breaking all commands. hub_contract_watch polls
        # firmware version + a runmethod canary per hub; these columns hold
        # the per-hub result, surfaced on the Hubs settings cards.
        "ALTER TABLE hub_health ADD COLUMN IF NOT EXISTS platform_version TEXT",
        "ALTER TABLE hub_health ADD COLUMN IF NOT EXISTS platform_version_seen_at TIMESTAMPTZ",
        "ALTER TABLE hub_health ADD COLUMN IF NOT EXISTS command_path_ok BOOLEAN",
        "ALTER TABLE hub_health ADD COLUMN IF NOT EXISTS command_path_contract VARCHAR(10)",
        "ALTER TABLE hub_health ADD COLUMN IF NOT EXISTS command_path_checked_at TIMESTAMPTZ",
        "ALTER TABLE hub_health ADD COLUMN IF NOT EXISTS command_path_error TEXT",

        # Grant PostgREST access to all new tables + their sequences.
        "GRANT SELECT, INSERT, UPDATE, DELETE ON event_routings TO smarthome_anon",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON device_commands TO smarthome_anon",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON instance_state_log TO smarthome_anon",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON mode_change_log TO smarthome_anon",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON hub_health TO smarthome_anon",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smarthome_anon",

        # Seed hub_health rows for every enabled hub so the eventsocket client
        # and reconcile-poll have somewhere to UPDATE from boot.
        "INSERT INTO hub_health (hub_id) SELECT id FROM hub_config WHERE is_enabled = TRUE "
        "ON CONFLICT (hub_id) DO NOTHING",

        # ====================================================================
        # 2026-05-17 — Settings cascade (system + app-type) + encrypted secrets
        # Canonical source: psql/migrations/005_settings_cascade_and_secrets_2026_05_17.sql
        # See docs/plans/comprehensive_settings_and_ui_overhaul_2026_05_17.md
        # POLICY: a setting key MUST live at exactly one configurable layer.
        # ====================================================================
        """CREATE TABLE IF NOT EXISTS system_settings (
            key              VARCHAR(80) PRIMARY KEY,
            value            TEXT NOT NULL,
            value_type       VARCHAR(20) NOT NULL,
            description      TEXT,
            ui_exposed       BOOLEAN DEFAULT TRUE,
            requires_restart BOOLEAN DEFAULT FALSE,
            updated_at       TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS app_type_settings (
            id               BIGSERIAL PRIMARY KEY,
            app_type_id      INTEGER NOT NULL REFERENCES app_types(id) ON DELETE CASCADE,
            key              VARCHAR(80) NOT NULL,
            value            TEXT NOT NULL,
            value_type       VARCHAR(20) NOT NULL,
            description      TEXT,
            ui_exposed       BOOLEAN DEFAULT TRUE,
            requires_restart BOOLEAN DEFAULT FALSE,
            updated_at       TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (app_type_id, key)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_app_type_settings_type ON app_type_settings(app_type_id)",
        """CREATE TABLE IF NOT EXISTS encrypted_secrets (
            key          VARCHAR(80) PRIMARY KEY,
            ciphertext   BYTEA NOT NULL,
            kek_version  INTEGER NOT NULL DEFAULT 1,
            description  TEXT,
            rotated_at   TIMESTAMPTZ,
            created_at   TIMESTAMPTZ DEFAULT NOW(),
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS system_boot_log (
            id             BIGSERIAL PRIMARY KEY,
            boot_at        TIMESTAMPTZ DEFAULT NOW(),
            secrets_source VARCHAR(40),
            kek_version    INTEGER,
            notes          TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_system_boot_log_at ON system_boot_log(boot_at DESC)",

        # Seed system_settings (idempotent via ON CONFLICT DO NOTHING).
        """INSERT INTO system_settings (key, value, value_type, description, ui_exposed, requires_restart) VALUES
          ('motion_timeout_floor_seconds', '60', 'int',
           'Minimum no-motion timeout in seconds. AML/Fan clamp computed timeouts to this floor unless the instance has bypassTimeoutFloor=true.',
           TRUE, FALSE),
          ('reconcile_interval_secs', '60', 'int', 'Normal reconcile-poll cadence.', TRUE, FALSE),
          ('reconcile_aggressive_secs', '10', 'int', 'Aggressive reconcile cadence after recent hub WS failure.', TRUE, FALSE),
          ('reconcile_aggressive_window_secs', '300', 'int', 'How recently a hub WS failure must have occurred to engage aggressive reconcile.', TRUE, FALSE),
          ('eventsocket_watchdog_secs', '120', 'int', 'Recycle WS connection if no events arrive within this window.', TRUE, FALSE),
          ('device_cmd_verify_retries', '3', 'int', 'Polls per command-send attempt to verify state.', TRUE, FALSE),
          ('device_cmd_verify_delay', '1.0', 'float', 'Seconds between verify polls.', TRUE, FALSE),
          ('device_cmd_operation_retries', '2', 'int', 'Full send+verify cycles before giving up.', TRUE, FALSE),
          ('aml_init_master_delay_seconds', '5', 'int', 'AML initialize() schedules its first master() run after this many seconds. Short delay lets in-flight motion events arrive first.', TRUE, FALSE),
          ('aml_periodic_eval_interval_seconds', '60', 'int', 'Defensive: every AML instance runs master() at this cadence regardless of events. Minimum 10s.', TRUE, FALSE),
          ('timezone', 'America/New_York', 'string', 'IANA timezone name. Hub-derived: refreshed hourly from /location/list/data on every enabled hub. UI editing is advisory — the next refresh cycle overrides. DB stays in UTC; this is applied to the app container at boot for log timestamps.', TRUE, FALSE),
          ('hub_tz_inconsistency', 'false', 'bool', 'Set to TRUE by the hub-TZ refresher when enabled hubs report disagreeing time zones. Dashboard surfaces this as a warning so the user can fix the outlier from the Hubitat UI.', TRUE, FALSE),
          ('hub_tz_breakdown', '{}', 'string', 'JSON object {hub_name: tz_or_status} from the most recent hub-TZ refresh. Populated by services.hub_tz_resolver. Values are Windows-style TZ strings, "unreachable", or "unmapped:<tz>".', TRUE, FALSE),
          ('colorblind_mode', 'false', 'bool', 'Use a colorblind-safe (Okabe-Ito) palette in charts and accent colors. Designed for protanopia / deuteranopia / tritanopia.', TRUE, FALSE),
          ('eventsocket_enabled', 'true', 'bool', 'Master switch for Hubitat eventsocket WS intake. Requires app restart.', TRUE, TRUE),
          ('reconcile_poll_enabled', 'true', 'bool', 'Reconcile poll on/off. Requires app restart.', TRUE, TRUE),
          ('device_commands_logging', 'true', 'bool', 'Two-phase device_commands logging. Requires app restart.', TRUE, TRUE),
          ('webhook_intake_enabled', 'false', 'bool', 'Legacy webhook intake — rollback escape hatch.', TRUE, TRUE),
          ('maker_api_enabled', 'false', 'bool', 'When TRUE: reconcile poll + commands + verify use Maker API (legacy path). When FALSE (default 2026-05-17): all three use the Hubitat admin API directly — bypasses Maker entirely. Toggle on /hubs page. Eventsocket WS handles inbound events regardless.', TRUE, FALSE)
        ON CONFLICT (key) DO NOTHING""",

        # instance_setting_exceptions — per-FIELD bypass of system-enforced
        # validation (e.g., motion_timeout_floor_seconds). Per the DB-SOT
        # policy, each exception is its own row (audit-friendly) rather
        # than a JSONB flag on app_instances.settings.
        """CREATE TABLE IF NOT EXISTS instance_setting_exceptions (
            id           BIGSERIAL PRIMARY KEY,
            instance_id  BIGINT NOT NULL REFERENCES app_instances(id) ON DELETE CASCADE,
            setting_path VARCHAR(120) NOT NULL,
            reason       TEXT,
            granted_at   TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (instance_id, setting_path)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ise_instance ON instance_setting_exceptions(instance_id)",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON instance_setting_exceptions TO smarthome_anon",

        # Per-hub admin credentials (2026-05-17). Optional — populated only
        # when the user enables Hubitat Hub Login Security. Plaintext for
        # now; KEK-encryption layer coming. AWS Secrets Manager fallback
        # via admin_creds_index → HUBITAT_ADMIN_USER_<n>/PASSWORD_<n>.
        "ALTER TABLE hub_config ADD COLUMN IF NOT EXISTS admin_username VARCHAR(80)",
        "ALTER TABLE hub_config ADD COLUMN IF NOT EXISTS admin_password VARCHAR(200)",
        "ALTER TABLE hub_config ADD COLUMN IF NOT EXISTS admin_creds_index INTEGER",
        # 2026-05-18: mode poller reads currentMode from the hub flagged
        # is_primary=TRUE. No new column needed — is_primary already
        # designates the authoritative hub for location-level concerns.

        # Grants
        "GRANT SELECT, INSERT, UPDATE, DELETE ON system_settings TO smarthome_anon",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON app_type_settings TO smarthome_anon",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON encrypted_secrets TO smarthome_anon",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON system_boot_log TO smarthome_anon",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smarthome_anon",

        # Tell PostgREST to reload its schema cache. Without this, columns
        # added by ALTER TABLE above are invisible to PostgREST's OpenAPI
        # and POST/PATCH requests fail with PGRST204 "column does not exist".
        # Surfaced by tests/integration/test_live_crud_and_cascades.py on 2026-05-16.
        "NOTIFY pgrst, 'reload schema'",
    ]

    try:
        conn = psycopg2.connect(
            host=db_host, port=db_port,
            dbname=db_name, user=db_user, password=db_pass,
            connect_timeout=5
        )
        conn.autocommit = True
        cur = conn.cursor()
        # Schema split (migration 007): tables live in dshub/dsapp/dscore, not
        # public. Ensure those schemas exist and resolve unqualified names to
        # them, so the idempotent CREATE TABLE IF NOT EXISTS / ALTER statements
        # below find the real (moved) tables instead of recreating empty
        # shadows in public. The live split is performed by 007 + init-db.sql;
        # this only keeps the catch-up migrations pointing at the right place.
        cur.execute("CREATE SCHEMA IF NOT EXISTS dshub")
        cur.execute("CREATE SCHEMA IF NOT EXISTS dsapp")
        cur.execute("CREATE SCHEMA IF NOT EXISTS dscore")
        cur.execute("CREATE SCHEMA IF NOT EXISTS api")
        cur.execute("SET search_path = dshub, dsapp, dscore, public")
        for sql in migrations:
            cur.execute(sql)
        # Tell PostgREST to reload its schema cache so columns added by the
        # ALTERs above (e.g. hub_health contract-watch columns) are visible
        # to the REST API immediately, not just after the next restart.
        cur.execute("NOTIFY pgrst, 'reload schema'")
        cur.close()
        conn.close()
        logger.info("DB migrations applied successfully")
    except Exception as e:
        logger.warning(f"DB migration skipped: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: initialize services on startup, cleanup on shutdown."""
    initialize_services()

    # Apply any pending schema migrations
    run_db_migrations()

    # Start Matter discovery background service (scans hubs every 5 min)
    from services.matter_discovery import start_matter_discovery, stop_matter_discovery
    start_matter_discovery(scan_interval=300)

    # Start device cache refresh (Matter-first, Maker API fallback)
    from services.device_cache_refresh import start_cache_refresh, stop_cache_refresh
    refresh_interval = int(os.environ.get('DEVICE_CACHE_REFRESH_INTERVAL', '120'))
    start_cache_refresh(refresh_interval=refresh_interval)

    # Start Hubitat eventsocket client (raw WS event stream per hub).
    # Default mode is 'shadow' — connect and log events without routing them,
    # so cutover can compare against the Maker API webhook path before flipping.
    # Set EVENTSOCKET_INTAKE_MODE=primary in env to dispatch through WebhookRouter.
    from services.hubitat_eventsocket_client import (
        start_eventsocket, stop_eventsocket
    )
    await start_eventsocket()

    # Reconcile poll — safety net for the WS-only intake. Polls /devices/all
    # per hub every 60s (10s in aggressive mode after a recent WS failure),
    # synthesizes events for cache↔hub divergences through WebhookRouter.
    from services.reconcile_poll import (
        start_reconcile_poll, stop_reconcile_poll
    )
    await start_reconcile_poll()

    # DB size-cap auto-prune (2026-05-17). Per-table max bytes; oldest rows
    # dropped + VACUUM ANALYZE every hour. Targets ~100MB total budget across
    # event_log / raw_events / event_routings / device_commands / etc.
    # See services/db_size_cap.py for the policy.
    try:
        from services.db_size_cap import schedule_prune_job, run_prune_pass
        from services.scheduler_service import get_scheduler
        schedule_prune_job(get_scheduler(), interval_seconds=3600)
        # Also run one pass synchronously at boot so we never start over budget.
        await asyncio.to_thread(run_prune_pass)
    except Exception as e:
        logger.warning(f"db_size_cap startup failed (non-fatal): {e}")

    # Run hub classification on startup (populates device_hub_mapping table).
    # Runs in background thread so it doesn't block app readiness.
    # TILES and DeviceCommander depend on this data for native-hub routing.
    import threading
    def _startup_classification():
        try:
            from services.device_to_hubs_classifier import run_classification, invalidate_cache
            logger.info("Running startup hub classification...")
            result = run_classification()
            invalidate_cache()
            total = result.get("total_native", 0) if isinstance(result, dict) else 0
            logger.info(f"Startup hub classification complete: {total} native devices mapped")
        except Exception as e:
            logger.warning(f"Startup hub classification failed (will retry on next POST /api/hub/classify): {e}")

    threading.Thread(target=_startup_classification, name="startup-hub-classify", daemon=True).start()

    # Hub-derived timezone refresh (2026-05-17 user directive).
    # Each hub carries its own location TZ; Mobius queries every enabled
    # hub on a schedule and caches the agreed-upon TZ to system_settings.
    # On disagreement, picks majority, warns, and persists the breakdown
    # so the dashboard can surface which hub to reconfigure.
    #
    # First refresh runs in a background thread so it doesn't block app
    # readiness (network query × N hubs would). Subsequent refreshes run
    # hourly via APScheduler.
    def _refresh_hub_timezone():
        try:
            from services.hub_tz_resolver import (
                resolve_hub_timezone,
                apply_resolved_timezone_to_environment,
                persist_resolved_timezone,
            )
            iana_tz, consistent, per_hub = resolve_hub_timezone()
            persist_resolved_timezone(iana_tz, consistent, per_hub)
            if iana_tz:
                apply_resolved_timezone_to_environment(iana_tz)
                if consistent:
                    logger.info(
                        f"Hub-derived TZ resolved: {iana_tz} (all hubs agree)"
                    )
                else:
                    logger.warning(
                        f"Hub-derived TZ resolved to {iana_tz} but hubs "
                        f"disagree. Breakdown: {per_hub}. Fix the outlier "
                        f"from its Hubitat UI → Settings → Location → "
                        f"Hub Time Zone."
                    )
            else:
                logger.info(
                    "Hub-derived TZ unavailable (no hubs reachable or "
                    "unmapped Windows TZ). Falling back to whatever's in "
                    "system_settings.timezone."
                )
        except Exception as e:
            logger.warning(
                f"Hub TZ refresh failed (non-fatal): {e}", exc_info=True
            )

    threading.Thread(
        target=_refresh_hub_timezone,
        name="startup-hub-tz-refresh",
        daemon=True,
    ).start()
    try:
        from services.scheduler_service import get_scheduler
        get_scheduler()._scheduler.add_job(
            func=_refresh_hub_timezone,
            trigger='interval',
            seconds=3600,
            id='hub_tz_refresh',
            replace_existing=True,
        )
    except Exception as e:
        logger.warning(f"Hub TZ periodic refresh schedule failed: {e}")

    # Hub-derived location-mode polling (2026-05-18).
    # The eventsocket WS doesn't deliver LOCATION frames on Elfege's
    # firmware (zero captured in raw_events across ~2.5k DEVICE frames).
    # Pull `currentMode` from the authoritative hub every 60s and write
    # location_modes + mode_change_log on transitions; route_mode_change
    # fires so running AML instances see on_mode_change() the same way
    # the WS path would have delivered it.
    def _mode_poll_first_pass():
        try:
            from services.mode_poller import run_poll_pass
            result = run_poll_pass()
            logger.info(f"mode_poller startup pass: {result}")
        except Exception as e:
            logger.warning(f"mode_poller startup pass failed: {e}")

    threading.Thread(
        target=_mode_poll_first_pass,
        name="startup-mode-poll",
        daemon=True,
    ).start()
    try:
        from services.mode_poller import schedule_poll_job
        from services.scheduler_service import get_scheduler
        schedule_poll_job(get_scheduler()._scheduler, interval_seconds=60)
    except Exception as e:
        logger.warning(f"mode_poller schedule failed: {e}")

    # Hubitat admin-API contract-drift watch (2026-05-26).
    # Firmware 2.5.0.143 changed POST /device/runmethod from form-encoded to
    # JSON with no backward compat, silently breaking every command. This
    # watcher polls each hub's firmware version + a harmless runmethod canary
    # and records the result in hub_health (surfaced on the Hubs cards), so the
    # next contract flip announces itself instead of being diagnosed by hand.
    # First pass in a background thread (network × N hubs); 6h recurring.
    def _contract_watch_first_pass():
        try:
            from services.hub_contract_watch import run_watch_pass
            result = run_watch_pass()
            logger.info(f"contract_watch startup pass: {result}")
        except Exception as e:
            logger.warning(f"contract_watch startup pass failed: {e}")
    threading.Thread(
        target=_contract_watch_first_pass,
        name="startup-contract-watch",
        daemon=True,
    ).start()
    try:
        from services.hub_contract_watch import schedule_watch_job
        from services.scheduler_service import get_scheduler
        schedule_watch_job(get_scheduler()._scheduler, interval_seconds=21600)
    except Exception as e:
        logger.warning(f"contract_watch schedule failed: {e}")

    # Start Samsung TV client (WS + HTTP power-poll background tasks).
    # Config is read from env vars (set in docker-compose or start.sh).
    # on_power_change pushes state to all registered Hubitat callbacks via
    # the blueprint's push_state_changes() so Hubitat stays in sync in real-time.
    from services.samsung_tv_client import get_tv_client
    from apps.samsung_tv.blueprint import push_state_changes as _tv_push, _persist_token

    async def _on_tv_state_change(state) -> None:
        """Bridge TV power OR connection state changes → Hubitat LAN push."""
        nonlocal _tv_client
        await _tv_push(_tv_client)

    async def _on_tv_token_save(new_token: str) -> None:
        """Persist a newly-issued TV auth token so it survives container restarts."""
        os.environ["SAMSUNG_TV_TOKEN"] = new_token
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _persist_token, new_token)

    # Token loaded from env var — populated by start.sh from ./state/samsung_tv_token.txt.
    _saved_token = os.environ.get("SAMSUNG_TV_TOKEN", "")

    _tv_client = get_tv_client(
        tv_ip            = os.environ.get("SAMSUNG_TV_IP",   "<LAN_IP>"),
        mac_address      = os.environ.get("SAMSUNG_TV_MAC",  "D0C24EE93390"),
        token            = _saved_token,
        use_ssl          = os.environ.get("SAMSUNG_TV_SSL",  "true").lower() == "true",
        name             = os.environ.get("SAMSUNG_TV_NAME", "living_room_tv"),
        on_power_change  = _on_tv_state_change,
        on_conn_change   = _on_tv_state_change,
        on_token_save    = _on_tv_token_save,
    )
    await _tv_client.start()

    # Phase C defensive layer: in-process event-loop watchdog. Ticks every
    # 2s; /api/health returns 503 if no tick within 10s. Docker healthcheck
    # surfaces that as unhealthy, and the autoheal sidecar restarts the
    # container. This narrows the stall-detection window from "loop fully
    # wedged, no HTTP at all" to "loop degraded for >10s" — well before a
    # stall becomes visible in the UI.
    from services.loop_watchdog import start_watchdog, stop_watchdog
    start_watchdog()

    yield

    stop_watchdog()
    stop_cache_refresh()
    stop_matter_discovery()
    await stop_eventsocket()
    await stop_reconcile_poll()

    # Stop Samsung TV client cleanly
    await _tv_client.stop()

    logger.info("Shutting down...")


# ---------------------------------------------------------------------------
# Create FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MOBIUS.HOME",
    description="Hubitat home automation platform with multi-instance support",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Include routers
# ---------------------------------------------------------------------------

from apps.advanced_motion_lighting.blueprint import router as motion_router  # noqa: E402
app.include_router(motion_router)

from apps.samsung_tv.blueprint import router as samsung_tv_router  # noqa: E402
app.include_router(samsung_tv_router)


# =============================================================================
# Health & Status
# =============================================================================


@app.get("/api/health", tags=["health"])
async def health():
    """
    Liveness probe. Returns 200 only if the event-loop watchdog has
    ticked within the alive threshold (10s by default). Returns 503 when
    the loop is wedged or degraded — Docker healthcheck picks that up
    and the autoheal sidecar restarts the container. See
    services/loop_watchdog.py for the rationale.
    """
    from services.loop_watchdog import (
        is_loop_alive,
        last_tick_age_seconds,
        ALIVE_THRESHOLD_SECS,
    )
    age = last_tick_age_seconds()
    if not is_loop_alive():
        # 503 fails the Docker healthcheck → autoheal restarts. Body is
        # informational; the status code is what the orchestrator reads.
        raise HTTPException(
            status_code=503,
            detail={
                "status": "degraded",
                "reason": "event loop watchdog stale",
                "loop_lag_seconds": age,
                "alive_threshold_seconds": ALIVE_THRESHOLD_SECS,
            },
        )
    return {"status": "ok", "loop_lag_seconds": age}


@app.get("/api/status", tags=["health"])
async def status():
    """Detailed status endpoint."""
    from services.instance_manager import get_instance_manager
    from services.hubitat_client import get_default_client

    manager = get_instance_manager()
    instances = manager.get_all_instances()

    # Check Hubitat connectivity
    try:
        client = get_default_client()
        hubitat_connected = client.is_connected()
    except Exception:
        hubitat_connected = False

    return {
        "status": "ok",
        "instances_count": len(instances),
        "running_instances": len(manager._running_instances),
        "hubitat_connected": hubitat_connected,
    }


# =============================================================================
# App Types
# =============================================================================


@app.get("/api/app-types", tags=["app-types"])
async def get_app_types():
    """List available app types."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    return manager.get_app_types()


@app.get("/api/app-types/{type_name}/schema", tags=["app-types"])
async def get_app_type_schema(type_name: str):
    """Get settings schema for an app type."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    schema = manager.get_app_type_schema(type_name)
    if schema:
        return schema
    raise HTTPException(status_code=404, detail="App type not found")


# =============================================================================
# Instances
# =============================================================================


@app.get("/api/instances", tags=["instances"])
async def get_instances():
    """List all instances."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    return manager.get_all_instances()


@app.post("/api/instances", status_code=201, tags=["instances"])
async def create_instance(body: CreateInstanceRequest):
    """Create a new instance."""
    from services.instance_manager import get_instance_manager

    manager = get_instance_manager()
    instance_id = manager.create_instance(
        app_type=body.app_type,
        label=body.label,
        device_selections=body.device_selections,
        settings=body.settings,
    )

    if instance_id:
        return {"id": instance_id, "message": "Instance created"}
    raise HTTPException(status_code=500, detail="Failed to create instance")


@app.get("/api/instances/{instance_id}", tags=["instances"])
async def get_instance(instance_id: int):
    """Get instance details."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    if instance:
        return instance
    raise HTTPException(status_code=404, detail="Instance not found")


@app.put("/api/instances/{instance_id}", tags=["instances"])
async def update_instance(instance_id: int, body: UpdateInstanceRequest):
    """Update instance settings."""
    from services.instance_manager import get_instance_manager

    manager = get_instance_manager()
    success = manager.update_instance(
        instance_id,
        label=body.label,
        device_selections=body.device_selections,
        settings=body.settings,
    )

    if success:
        return {"message": "Instance updated"}
    raise HTTPException(status_code=500, detail="Failed to update instance")


@app.delete("/api/instances/{instance_id}", tags=["instances"])
async def delete_instance(instance_id: int):
    """Delete an instance."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    if manager.delete_instance(instance_id):
        return {"message": "Instance deleted"}
    raise HTTPException(status_code=500, detail="Failed to delete instance")


@app.post("/api/instances/{instance_id}/stop", tags=["instances"])
async def stop_instance(instance_id: int):
    """Kill a running instance (e.g. when entering edit mode).

    The instance stays in the DB but is no longer processing events.
    Call POST .../start or PUT to restart it.
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    was_running = manager.stop_instance(instance_id)
    return {"message": "Instance stopped", "was_running": was_running}


@app.post("/api/instances/{instance_id}/start", tags=["instances"])
async def start_instance(instance_id: int):
    """Start an instance from its current DB state.

    Used after cancelling an edit (instance was stopped on edit entry).
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    # Stop first in case it's somehow still running
    manager.stop_instance(instance_id)
    started = manager._start_from_db(instance_id)
    if started:
        return {"message": "Instance started"}
    raise HTTPException(status_code=500, detail="Failed to start instance")


@app.post("/api/instances/{instance_id}/pause", tags=["instances"])
async def pause_instance(instance_id: int, body: PauseInstanceRequest = PauseInstanceRequest()):
    """Pause an instance."""
    from services.instance_manager import get_instance_manager

    manager = get_instance_manager()
    if manager.pause_instance(instance_id, body.duration_minutes, body.reason):
        return {"message": "Instance paused"}
    raise HTTPException(status_code=500, detail="Failed to pause instance")


@app.post("/api/instances/{instance_id}/resume", tags=["instances"])
async def resume_instance(instance_id: int):
    """Resume a paused instance."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    if manager.resume_instance(instance_id):
        return {"message": "Instance resumed"}
    raise HTTPException(status_code=500, detail="Failed to resume instance")


@app.get("/api/instances/{instance_id}/status", tags=["instances"])
async def get_instance_status(instance_id: int):
    """Get runtime status of an instance."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    running = manager.get_running_instance(instance_id) is not None

    return {
        "id": instance_id,
        "label": instance.get("label"),
        "is_running": running,
        "is_paused": instance.get("is_paused", False),
        "is_enabled": instance.get("is_enabled", True),
        "last_activity": instance.get("last_activity_at"),
    }


@app.get("/api/instances/{instance_id}/runtime-status", tags=["instances"])
async def get_instance_runtime_status(instance_id: int):
    """
    Live runtime state of a running instance — what the debug-panel
    countdown reads to show "time until next turn-off".

    Returns a small JSON with:
      - last_motion_time:  ISO UTC of the most recent motion=active event
                           seen by this instance (None if never)
      - timeout_seconds:   current effective no-motion timeout, after
                           per-mode lookup + system-floor clamp
      - timeout_at:        ISO UTC of when AML will next decide off
                           (= last_motion_time + timeout_seconds);
                           None if last_motion_time is None
      - remaining_seconds: float — positive means "off pending in N s",
                           zero/negative means "should have fired by now"
                           (master() picks it up on the next tick)
      - current_mode:      string from location_modes (DB-read, not Maker)
      - is_motion_active:  current Tier-1+Tier-2 verdict
      - is_paused:         persisted pause state
      - is_running:        whether the instance is loaded in-memory

    404 if the instance row doesn't exist. 200 with is_running=false
    and null runtime fields when the instance is stopped.
    """
    from datetime import datetime, timezone
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    running = manager.get_running_instance(instance_id)
    out = {
        "id": instance_id,
        "label": instance.get("label"),
        "is_running": running is not None,
        "is_paused": instance.get("is_paused", False),
        "last_motion_time": None,
        "timeout_seconds": None,
        # User's configured display unit ('seconds' or 'minutes'). The
        # UI formats the countdown accordingly so a 5-min timeout reads
        # "off in 4m 23s" and a 30-sec timeout reads "off in 18s".
        "time_unit": (instance.get("settings") or {}).get("timeUnit", "seconds"),
        "timeout_at": None,
        "remaining_seconds": None,
        "current_mode": None,
        "is_motion_active": None,
    }
    if running is None:
        return out

    # Read in-memory runtime state. These are AML-specific attributes;
    # other app types may not have them — defend with getattr.
    rt = getattr(running, "_runtime", None)
    last_motion = getattr(rt, "last_motion_time", None) if rt else None

    timeout_seconds = None
    try:
        # _get_timeout_seconds is on the TimeoutMixin (AML); FanAutomation
        # has a different path. Guard so we don't crash on non-AML types.
        if hasattr(running, "_get_timeout_seconds"):
            timeout_seconds = int(running._get_timeout_seconds())
    except Exception as e:
        logger.debug(f"runtime-status: _get_timeout_seconds failed: {e}")

    try:
        if hasattr(running, "_get_current_mode"):
            out["current_mode"] = running._get_current_mode()
    except Exception:
        pass

    try:
        if hasattr(running, "_is_motion_active"):
            out["is_motion_active"] = bool(running._is_motion_active())
    except Exception:
        pass

    if last_motion is not None:
        # last_motion_time is set tz-aware (datetime.now(timezone.utc));
        # serialize to ISO and compute remaining if we have a timeout.
        out["last_motion_time"] = last_motion.isoformat()
        if timeout_seconds is not None:
            from datetime import timedelta
            timeout_at = last_motion + timedelta(seconds=timeout_seconds)
            out["timeout_at"] = timeout_at.isoformat()
            out["timeout_seconds"] = timeout_seconds
            out["remaining_seconds"] = (
                timeout_at - datetime.now(timezone.utc)
            ).total_seconds()
    elif timeout_seconds is not None:
        # No motion observed yet this process lifetime — surface the
        # configured timeout for the user but no countdown to anchor it to.
        out["timeout_seconds"] = timeout_seconds

    return out


@app.post("/api/instances/{instance_id}/run", tags=["instances"])
async def run_instance(instance_id: int):
    """
    Run instance: start if stopped, or re-evaluate state if already running.

    When already running, calls master() to evaluate current conditions
    (motion state, timeouts) and control lights accordingly.
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    running = manager.get_running_instance(instance_id)
    if running:
        # Already running — re-evaluate current state via master()
        running.master()
        return {"message": "Instance re-evaluated current state"}

    if manager._start_instance(instance_id, instance):
        return {"message": "Instance started"}
    raise HTTPException(status_code=500, detail="Failed to start instance")


@app.post("/api/instances/{instance_id}/update", tags=["instances"])
async def update_initialize_instance(instance_id: int):
    """
    Reload an instance (stop + start with current config).

    Also resets memoization state so the instance starts fresh
    without stale override records.
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # Reset memoization on reload (stale memo causes incorrect behavior)
    manager.update_memoization(instance_id, {})

    manager.stop_instance(instance_id)
    manager._rebuild_subscriptions(instance_id)
    manager._start_from_db(instance_id)
    return {"message": "Instance reloaded with memoization reset"}


# =============================================================================
# Devices
# =============================================================================


@app.get("/api/devices", tags=["devices"])
async def get_devices(capability: Optional[str] = Query(None)):
    """
    List devices, optionally filtered by capability.

    Reads from the canonical `devices` table (DB-cached) — NOT a live Maker
    API call. The canonical table is kept fresh by hub_classifier on startup
    and by the reconcile poll. This eliminates the multi-second "Loading
    devices..." wait in the wizard that the per-category live-Maker pattern
    used to cause (one HTTP roundtrip to Hubitat per category × 6 categories).

    For a forced live refresh from Hubitat, see /api/devices/refresh.

    Args:
        capability: Filter by capability (e.g., 'motionSensor', 'switch').
                    PostgREST JSONB containment: capabilities ? capability.
    """
    try:
        pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        params = {
            'select': 'id,hub_ip,hubitat_id,label,name,device_type,'
                      'protocol,capabilities,attributes',
            'order': 'label',
        }
        if capability:
            # PostgREST JSONB array-contains: cs.["value"] for JSONB array
            # of strings. NOT cs.{"value"} — that's PG-array literal syntax
            # and PostgREST rejects it on JSONB columns (PGRST 22P02).
            params['capabilities'] = f'cs.["{capability}"]'
        r = await aget(f"{pg}/devices", params=params, timeout=5)
        r.raise_for_status()
        rows = r.json()
        # Shape compatibility: legacy callers expect each device to have
        # `id` (the integer canonical PK is fine) and a `label`. Already do.
        return rows
    except Exception as e:
        logger.error(f"Failed to get devices: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/devices/by-categories", tags=["devices"])
async def get_devices_by_categories(categories: str = Query(...)):
    """
    Bulk endpoint: returns devices grouped by capability in ONE roundtrip.

    `categories` is a comma-separated list of capability names matching
    what the wizard's device_categories return. Example:
        GET /api/devices/by-categories?categories=motionSensor,switch,contact
        →  {
              "motionSensor": [...],
              "switch":       [...],
              "contact":      [...]
           }

    Internally one PostgREST call fetches all devices, then we group in
    memory. Faster than N round-trips and trivial to add new categories.
    """
    cats = [c.strip() for c in categories.split(',') if c.strip()]
    if not cats:
        return {}
    try:
        pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        r = await aget(
            f"{pg}/devices",
            params={
                'select': 'id,hub_ip,hubitat_id,label,name,device_type,'
                          'protocol,capabilities,attributes',
                'order': 'label',
            },
            timeout=5,
        )
        r.raise_for_status()
        all_devices = r.json()
        # Case-insensitive capability matching. Hubitat ships capabilities
        # as PascalCase ("MotionSensor", "Switch") but wizard configs use
        # camelCase ("motionSensor", "switch") in places. The naive `c in
        # caps` would silently return empty for every category. Build a
        # lowercase capability set per device once and compare lowered
        # category names against it.
        out = {c: [] for c in cats}
        cats_lower = {c.lower(): c for c in cats}
        for d in all_devices:
            caps = d.get('capabilities') or []
            caps_lower = {str(c).lower() for c in caps}
            for cat_lower, cat_orig in cats_lower.items():
                if cat_lower in caps_lower:
                    out[cat_orig].append(d)
        return out
    except Exception as e:
        logger.error(f"get_devices_by_categories: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/refresh", tags=["devices"])
async def refresh_devices_from_hubitat():
    """
    Force a fresh pull of all devices from Hubitat via Maker API → canonical
    `devices` table. Use sparingly — the reconcile poll already keeps this
    fresh in the background. Returns the count of devices refreshed.
    """
    from services.device_to_hubs_classifier import run_classification, invalidate_cache
    try:
        result = run_classification()
        invalidate_cache()
        return {
            "ok": True,
            "total_native": (result or {}).get("total_native", 0),
        }
    except Exception as e:
        logger.error(f"refresh_devices_from_hubitat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/devices/{device_id}", tags=["devices"])
async def get_device(device_id: str):
    """Get device details."""
    from services.device_to_hubs_classifier import fetch_device_live

    try:
        device = fetch_device_live(device_id)
        if device:
            return device
        raise HTTPException(status_code=404, detail="Device not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get device: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/{device_id}/command", tags=["devices"])
async def send_device_command(device_id: str, body: DeviceCommandRequest):
    """
    Send command to a device via the DeviceCommander.

    Uses threaded execution with nested retries and state verification.

    Args:
        device_id: Hubitat device ID
        body: Command name and optional arguments
    """
    from services.device_commander import get_device_commander

    try:
        commander = get_device_commander()
        result = await commander.send_command(
            device_id=device_id,
            command=body.command,
            args=body.args,
            verify=True,
        )

        if result.success:
            return {
                "message": "Command sent",
                "verified": result.verified,
                "status": result.status.value,
                "actual_state": result.actual_state,
                "expected_state": result.expected_state,
                "retries_used": result.retries_used,
                "elapsed_ms": round(result.elapsed_ms, 1),
            }
        raise HTTPException(
            status_code=500,
            detail=f"Command failed: {result.error}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to send command: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Webhooks (from Hubitat via webhook-dispatcher)
# =============================================================================


@app.post("/api/webhook/event", tags=["webhooks"])
async def handle_event_webhook(request: Request):
    """
    Deprecated — device event intake moved to the Hubitat eventsocket.

    Reason: the Maker API webhook path was fragile — firmware updates and
    re-saving the Maker API app silently de-armed per-device event forwarding,
    producing the 2026-05-16 Living-room failure (canons 55/56 stopped
    delivering events while the hub itself kept firing them). The eventsocket
    bypasses per-device opt-in entirely. See services/hubitat_eventsocket_client.py.

    Set WEBHOOK_INTAKE_ENABLED=true to re-open this endpoint as a temporary
    rollback during a problem with the WS intake.
    """
    if os.environ.get('WEBHOOK_INTAKE_ENABLED', 'false').strip().lower() == 'true':
        from services.webhook_router import get_webhook_router
        try:
            payload = await request.json()
            router = get_webhook_router()
            routed_count = await router.route_event(payload)
            return {"routed_to": routed_count}
        except Exception as e:
            logger.error(f"Webhook event processing failed: {e}", exc_info=True)
            return {"routed_to": 0, "error": str(e)}

    # Closed path — explicit 410 so the dispatcher's failed-POST logs are
    # not misread as a transient error.
    raise HTTPException(
        status_code=410,
        detail=(
            "Webhook event intake is deprecated; events now arrive via the "
            "Hubitat eventsocket. Set WEBHOOK_INTAKE_ENABLED=true to "
            "temporarily re-open this endpoint."
        ),
    )


@app.post("/api/webhook/mode", tags=["webhooks"])
async def handle_mode_webhook(request: Request):
    """Handle mode change webhook from Hubitat."""
    from services.webhook_router import get_webhook_router

    try:
        payload = await request.json()
        logger.info(f"Mode change webhook: {payload}")

        router = get_webhook_router()
        notified = await router.route_mode_change(payload)

        return {"notified": notified}
    except Exception as e:
        logger.error(f"Mode webhook processing failed: {e}", exc_info=True)
        return {"notified": 0, "error": str(e)}


# =============================================================================
# Modes
# =============================================================================


# =============================================================================
# Settings (cascade: system → app-type → instance)
# =============================================================================


@app.get("/api/system_settings", tags=["settings"])
async def list_system_settings(ui_only: bool = Query(True)):
    """
    List system-wide settings. Default: only UI-exposed ones.
    Set ui_only=false to include internal knobs.
    """
    params = {"order": "key"}
    if ui_only:
        params["ui_exposed"] = "eq.true"
    try:
        r = await aget(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/system_settings",
            params=params,
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"list_system_settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/system_settings/{key}", tags=["settings"])
async def get_system_setting(key: str):
    """Get a single system setting by key. Returns coerced value."""
    from services.settings_resolver import get_resolver, _coerce
    resolver = get_resolver()
    # Force a fresh fetch so the caller sees the latest value
    resolver._sys_cache.pop(key, None)
    row = resolver._fetch_system_row(key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"setting {key!r} not found")
    return {
        "key": row["key"],
        "value": _coerce(row["value"], row["value_type"]),
        "value_type": row["value_type"],
    }


class SystemSettingPatch(BaseModel):
    """Body for PATCH /api/system_settings/{key}."""
    value: Any


@app.patch("/api/system_settings/{key}", tags=["settings"])
async def patch_system_setting(key: str, body: SystemSettingPatch):
    """Update a system setting. Type-coerces against the stored value_type."""
    from services.settings_resolver import get_resolver
    resolver = get_resolver()
    ok = resolver.set_system(key, body.value)
    if not ok:
        raise HTTPException(status_code=400, detail=f"could not set {key!r}")
    return {"key": key, "value": body.value}


@app.get("/api/app_types/{type_name}/settings", tags=["settings"])
async def list_app_type_settings(type_name: str):
    """
    List per-app-type global settings for the named app type.
    """
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    # Look up app_type id from name
    r = await aget(f"{pg}/app_types",
                      params={"type_name": f"eq.{type_name}",
                              "select": "id"},
                      timeout=5)
    rows = r.json() if r.status_code == 200 else []
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"app_type {type_name!r} not found")
    app_type_id = rows[0]["id"]
    r = await aget(f"{pg}/app_type_settings",
                      params={"app_type_id": f"eq.{app_type_id}",
                              "ui_exposed": "eq.true",
                              "order": "key"},
                      timeout=5)
    r.raise_for_status()
    return r.json()


class AppTypeSettingPatch(BaseModel):
    """Body for PATCH /api/app_types/{type_name}/settings/{key}."""
    value: Any


@app.get("/api/instances/{instance_id}/setting-exceptions", tags=["settings"])
async def list_instance_setting_exceptions(instance_id: int):
    """List all per-field exceptions granted to this instance."""
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    r = await aget(
        f"{pg}/instance_setting_exceptions",
        params={
            "instance_id": f"eq.{instance_id}",
            "select": "id,setting_path,reason,granted_at",
        },
        timeout=5,
    )
    r.raise_for_status()
    return r.json()


class SettingExceptionGrant(BaseModel):
    """Body for POST /api/instances/{id}/setting-exceptions."""
    setting_path: str
    reason: Optional[str] = None


@app.post("/api/instances/{instance_id}/setting-exceptions", tags=["settings"])
async def grant_instance_setting_exception(
    instance_id: int, body: SettingExceptionGrant,
):
    """
    Grant this instance an exception for `setting_path` — bypasses
    system-enforced validation (e.g., motion_timeout_floor_seconds) for that
    field only. Audit record kept (granted_at).
    """
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    r = await apost(
        f"{pg}/instance_setting_exceptions",
        json={
            "instance_id": instance_id,
            "setting_path": body.setting_path,
            "reason": body.reason,
        },
        headers={
            "Content-Type": "application/json",
            "Prefer": "return=representation,resolution=merge-duplicates",
        },
        timeout=5,
    )
    if r.status_code in (200, 201):
        body_json = r.json()
        return body_json[0] if isinstance(body_json, list) and body_json else body_json
    raise HTTPException(status_code=r.status_code, detail=r.text)


@app.delete(
    "/api/instances/{instance_id}/setting-exceptions/{setting_path:path}",
    tags=["settings"],
)
async def revoke_instance_setting_exception(
    instance_id: int, setting_path: str,
):
    """Revoke a per-field exception. setting_path uses path-style routing so
    nested keys like `modeTimeouts.Night` work without URL encoding."""
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    r = await adelete(
        f"{pg}/instance_setting_exceptions",
        params={
            "instance_id": f"eq.{instance_id}",
            "setting_path": f"eq.{setting_path}",
        },
        timeout=5,
    )
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return {"ok": True, "setting_path": setting_path}


@app.patch(
    "/api/app_types/{type_name}/settings/{key}",
    tags=["settings"],
)
async def patch_app_type_setting(
    type_name: str, key: str, body: AppTypeSettingPatch,
):
    """Update a per-app-type setting."""
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    r = await aget(f"{pg}/app_types",
                      params={"type_name": f"eq.{type_name}",
                              "select": "id"},
                      timeout=5)
    rows = r.json() if r.status_code == 200 else []
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"app_type {type_name!r} not found")
    app_type_id = rows[0]["id"]
    from services.settings_resolver import get_resolver
    ok = get_resolver().set_app_type(app_type_id, key, body.value)
    if not ok:
        raise HTTPException(status_code=400,
                            detail=f"could not set ({type_name}, {key})")
    return {"app_type": type_name, "key": key, "value": body.value}


# =============================================================================
# Hub Classification (native-hub device routing)
# =============================================================================


@app.post("/api/hub/classify", tags=["hub-classification"])
async def run_hub_classification():
    """
    Run device classification across all configured hubs.

    Queries each hub's Maker API, classifies devices as native vs
    mesh-linked, builds cross-reference routing table, and writes
    to the device_hub_mapping table.

    This enables the DeviceCommander to route commands directly to
    the hub that physically owns each device (bypassing Hub Mesh relay).
    """
    from services.device_to_hubs_classifier import run_classification, invalidate_cache
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        # Run classification in executor to avoid blocking event loop
        # (makes HTTP requests to all 4 hubs)
        result = await loop.run_in_executor(None, run_classification)
        # Invalidate in-memory routing cache so new data is picked up
        invalidate_cache()
        return result
    except Exception as e:
        logger.error(f"Hub classification failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hub/mapping", tags=["hub-classification"])
async def get_hub_mapping(
    hub_name: Optional[str] = Query(None, description="Filter by native hub name"),
    protocol: Optional[str] = Query(None, description="Filter by protocol"),
):
    """
    Get the current device-to-hub mapping table.

    Returns all classified devices with their native hub, protocol,
    and mirror info. Optionally filter by hub or protocol.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    params = {}
    if hub_name:
        params["native_hub_name"] = f"eq.{hub_name}"
    if protocol:
        params["protocol"] = f"eq.{protocol}"
    params["order"] = "device_label.asc"

    try:
        resp = req.get(
            f"{postgrest_url}/device_hub_mapping",
            params=params,
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            entries = resp.json()
            return {
                "count": len(entries),
                "entries": entries,
            }
        return {"error": f"PostgREST returned {resp.status_code}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hub/mapping/stats", tags=["hub-classification"])
async def get_hub_mapping_stats():
    """
    Get summary statistics for the device hub mapping.

    Returns per-hub and per-protocol counts.
    """
    import requests as req
    from collections import defaultdict

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    try:
        resp = req.get(
            f"{postgrest_url}/device_hub_mapping",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"error": f"PostgREST returned {resp.status_code}"}

        entries = resp.json()
        hub_counts = defaultdict(int)
        proto_counts = defaultdict(int)
        hub_proto = defaultdict(lambda: defaultdict(int))

        for e in entries:
            hub = e.get("native_hub_name", "unknown")
            proto = e.get("protocol", "unknown")
            hub_counts[hub] += 1
            proto_counts[proto] += 1
            hub_proto[hub][proto] += 1

        return {
            "total": len(entries),
            "by_hub": dict(hub_counts),
            "by_protocol": dict(proto_counts),
            "hub_protocol_matrix": {
                hub: dict(protos) for hub, protos in hub_proto.items()
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Matter Protocol
# =============================================================================


class MatterCommissionRequest(BaseModel):
    """Request body for commissioning a new Matter device."""
    code: str  # QR code string (MT:...) or manual pairing code


class MatterMapRequest(BaseModel):
    """Request body for mapping a Hubitat device to a Matter node."""
    hubitat_device_id: str
    matter_node_id: int
    matter_endpoint_id: int = 1
    device_name: Optional[str] = None


@app.get("/api/matter/status", tags=["matter"])
async def matter_status():
    """
    Get matter-server connection status.

    Returns connection state and server info if connected.
    """
    from services.matter_client import get_matter_client

    client = get_matter_client()
    status = {"connected": client.is_connected, "url": client.url}

    if client.is_connected:
        try:
            info = await client.get_server_info()
            status["server_info"] = info
        except Exception as e:
            status["server_info_error"] = str(e)

    return status


@app.get("/api/matter/nodes", tags=["matter"])
async def matter_nodes():
    """
    List all commissioned Matter nodes.

    Connects to matter-server if not already connected.
    """
    from services.matter_client import get_matter_client

    client = get_matter_client()
    if not client.is_connected:
        connected = await client.connect()
        if not connected:
            raise HTTPException(
                status_code=503,
                detail="Cannot connect to matter-server"
            )

    try:
        nodes = await client.get_nodes()

        # Enrich nodes with friendly names from our discovered devices table.
        # Match by unique_id: Matter Basic Information cluster (40), attr 18 = UniqueID
        # Enrich with friendly names from hubitat_matter_devices.
        # Two lookups: by our_node_id (direct) and by unique_id (fallback).
        import requests as req
        postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        by_node_id = {}
        by_unique_id = {}
        try:
            disc_resp = req.get(
                f"{postgrest_url}/hubitat_matter_devices",
                headers={"Accept": "application/json"},
                timeout=5
            )
            if disc_resp.ok:
                for d in disc_resp.json():
                    if d.get('our_node_id'):
                        by_node_id[d['our_node_id']] = d
                    by_unique_id[d['unique_id']] = d
        except Exception:
            pass

        for node in nodes:
            node_id = node.get('node_id') or node.get('nodeId')

            # Primary: match by our_node_id (set during commission)
            match = by_node_id.get(node_id)

            # Fallback: match by UniqueID attribute
            if not match:
                attrs = node.get('attributes', {})
                for key, val in attrs.items():
                    if '/40/18' in key and isinstance(val, str) and val in by_unique_id:
                        match = by_unique_id[val]
                        break

            if match:
                # Prefer Hubitat friendly name over Matter product name
                node['_device_name'] = (
                    match.get('maker_api_device_name')
                    or match.get('device_name')
                )
                node['_hubitat_device_id'] = match.get('maker_api_device_id')

                # Backfill: if matched by UniqueID but our_node_id not set, update DB
                if not match.get('our_node_id') and node_id:
                    try:
                        req.patch(
                            f"{postgrest_url}/hubitat_matter_devices",
                            params={"unique_id": f"eq.{match['unique_id']}"},
                            json={"our_node_id": node_id},
                            headers={"Content-Type": "application/json"},
                            timeout=5
                        )
                        logger.info(f"Backfilled our_node_id={node_id} for {match['unique_id']}")
                    except Exception:
                        pass

        return nodes
    except Exception as e:
        logger.error(f"Failed to get Matter nodes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/matter/reconcile", tags=["matter"])
async def matter_reconcile():
    """
    Reconcile device_matter_map with commissioned nodes.
    Matches commissioned matter-server nodes to discovered Hubitat devices
    by UniqueID and creates missing mapping entries automatically.
    """
    from services.matter_discovery import get_matter_discovery_service
    service = get_matter_discovery_service()
    reconciled = await service._reconcile_mappings()
    return {"reconciled": reconciled}


@app.post("/api/matter/commission", tags=["matter"])
async def matter_commission(body: MatterCommissionRequest):
    """
    Commission a new Matter device using a pairing code.

    The code can be a QR code string (MT:...) or a manual numeric
    pairing code. A USB Bluetooth adapter is required on the server
    for BLE-based commissioning of new devices. Devices already
    paired to another controller (e.g., Hubitat) can be commissioned
    via on-network commissioning without BLE.

    Args:
        body: Contains the pairing code
    """
    from services.matter_client import get_matter_client

    client = get_matter_client()
    if not client.is_connected:
        connected = await client.connect()
        if not connected:
            raise HTTPException(
                status_code=503,
                detail="Cannot connect to matter-server"
            )

    try:
        result = await client.commission_with_code(body.code)
        return {"message": "Device commissioned", "node": result}
    except Exception as e:
        logger.error(f"Matter commissioning failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/matter/map", tags=["matter"])
async def matter_mappings():
    """Get all Hubitat-to-Matter device mappings."""
    from services.matter_client import get_all_matter_mappings
    # get_all_matter_mappings() is sync (blocking requests.get on PostgREST);
    # offload to a worker thread so a slow lookup can't hold the event loop.
    return await asyncio.to_thread(get_all_matter_mappings)


@app.post("/api/matter/map", tags=["matter"])
async def matter_create_mapping(body: MatterMapRequest):
    """
    Map a Hubitat device to a Matter node.

    After mapping, commands sent to this Hubitat device will also be
    sent via the Matter protocol for faster local control.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    try:
        resp = req.post(
            f"{postgrest_url}/device_matter_map",
            json={
                "hubitat_device_id": body.hubitat_device_id,
                "matter_node_id": body.matter_node_id,
                "matter_endpoint_id": body.matter_endpoint_id,
                "device_name": body.device_name
            },
            headers={
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates"
            },
            timeout=5
        )
        if resp.ok:
            return {"message": "Mapping created"}
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create Matter mapping: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/matter/map/{hubitat_device_id}", tags=["matter"])
async def matter_delete_mapping(hubitat_device_id: str):
    """Remove a Hubitat-to-Matter device mapping."""
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    try:
        resp = req.delete(
            f"{postgrest_url}/device_matter_map",
            params={"hubitat_device_id": f"eq.{hubitat_device_id}"},
            timeout=5
        )
        if resp.ok:
            return {"message": "Mapping deleted"}
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete Matter mapping: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/matter/discover", tags=["matter"])
async def matter_discover():
    """
    Discover Matter devices from all configured Hubitat hubs.

    Queries each hub's /hub/matterDetails/json endpoint, deduplicates
    by unique_id, and stores results in hubitat_matter_devices table.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Collect hub IPs to scan
    hubs = []
    main_ip = os.environ.get('HUBITAT_HUB_IP_MAIN')
    if main_ip:
        hubs.append({"ip": main_ip, "name": "main"})
    for i in range(1, 4):
        ip = os.environ.get(f'HUBITAT_HUB_IP_OTHER_HUB_{i}')
        if ip:
            hubs.append({"ip": ip, "name": f"other_hub_{i}"})

    # First, get all Maker API devices for name matching
    from services.hubitat_client import get_default_client
    maker_devices = []
    try:
        client = get_default_client()
        maker_devices = client.get_all_devices() or []
    except Exception as e:
        logger.warning(f"Could not load Maker API devices for matching: {e}")

    # Build name-lookup index (lowercase name → device)
    maker_by_name = {}
    for d in maker_devices:
        name = (d.get('label') or d.get('name') or '').strip().lower()
        if name:
            maker_by_name[name] = d

    discovered = []
    errors = []

    for hub in hubs:
        try:
            resp = req.get(
                f"http://{hub['ip']}/hub/matterDetails/json",
                timeout=10
            )
            if not resp.ok:
                errors.append(f"{hub['name']} ({hub['ip']}): HTTP {resp.status_code}")
                continue

            data = resp.json()
            if not data.get('enabled'):
                continue

            for device in data.get('devices', []):
                unique_id = device.get('uniqueId', '')
                if not unique_id:
                    continue

                matter_name = (device.get('name') or '').strip()

                # Try to match against Maker API devices by name
                maker_match = None
                match_confidence = 'none'
                name_lower = matter_name.lower()

                # Exact match
                if name_lower in maker_by_name:
                    maker_match = maker_by_name[name_lower]
                    match_confidence = 'exact'
                else:
                    # Fuzzy: check if Matter name is contained in or contains a Maker name
                    for mk_name, mk_dev in maker_by_name.items():
                        if name_lower in mk_name or mk_name in name_lower:
                            maker_match = mk_dev
                            match_confidence = 'fuzzy'
                            break

                row = {
                    "unique_id": unique_id,
                    "device_name": matter_name,
                    "manufacturer": device.get('manufacturer', ''),
                    "model": device.get('model', ''),
                    "ip_address": device.get('ipAddress', ''),
                    "is_online": device.get('online', False),
                    "hub_ip": hub['ip'],
                    "hub_name": hub['name'],
                    "hubitat_node_id": device.get('nodeId', 0),
                    "hubitat_device_id": str(device.get('id', '')),
                    "hubitat_dni": device.get('dni', ''),
                }

                if maker_match:
                    row["maker_api_device_id"] = str(maker_match.get('id', ''))
                    row["maker_api_device_name"] = maker_match.get('label') or maker_match.get('name', '')
                    row["match_confidence"] = match_confidence

                discovered.append(row)

                # Upsert into database (dedup by unique_id)
                req.post(
                    f"{postgrest_url}/hubitat_matter_devices",
                    json=row,
                    headers={
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates"
                    },
                    timeout=5
                )

        except Exception as e:
            errors.append(f"{hub['name']} ({hub['ip']}): {str(e)}")

    matched = sum(1 for d in discovered if d.get('match_confidence', 'none') != 'none')
    return {
        "discovered": len(discovered),
        "matched": matched,
        "hubs_scanned": len(hubs),
        "errors": errors
    }


@app.get("/api/matter/hubitat-devices", tags=["matter"])
async def matter_hubitat_devices():
    """Get all discovered Hubitat Matter devices from database."""
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    try:
        resp = req.get(
            f"{postgrest_url}/hubitat_matter_devices",
            params={"order": "device_name.asc"},
            headers={"Accept": "application/json"},
            timeout=5
        )
        if resp.ok:
            return resp.json()
        return []
    except Exception as e:
        logger.error(f"Failed to get hubitat matter devices: {e}")
        return []


class UpdateMatterDeviceMatchRequest(BaseModel):
    """Request body for manually correcting a Matter-to-Maker API match."""
    unique_id: str
    maker_api_device_id: str


@app.patch("/api/matter/hubitat-devices/match", tags=["matter"])
async def matter_update_match(body: UpdateMatterDeviceMatchRequest):
    """
    Manually correct the Maker API device match for a Hubitat Matter device.

    Used when auto-matching by name got it wrong. The user selects the
    correct Maker API device from a dropdown.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Get the Maker API device name for display
    maker_name = ''
    try:
        from services.device_to_hubs_classifier import fetch_device_live
        device = fetch_device_live(body.maker_api_device_id)
        if device:
            maker_name = device.get('label') or device.get('name', '')
    except Exception:
        pass

    try:
        resp = req.patch(
            f"{postgrest_url}/hubitat_matter_devices",
            params={"unique_id": f"eq.{body.unique_id}"},
            json={
                "maker_api_device_id": body.maker_api_device_id,
                "maker_api_device_name": maker_name,
                "match_confidence": "manual"
            },
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        if resp.ok:
            return {"message": "Match updated", "maker_api_device_name": maker_name}
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AutoCommissionRequest(BaseModel):
    """Request body for auto-commissioning a Hubitat Matter device."""
    unique_id: str


@app.post("/api/matter/auto-commission", tags=["matter"])
async def matter_auto_commission(body: AutoCommissionRequest):
    """
    Auto-commission a Hubitat Matter device into our matter-server.

    Steps:
    1. Look up device in hubitat_matter_devices by unique_id
    2. Call Hubitat's openPairingWindow to get a setup code
    3. Commission into our matter-server using that code
    4. Create the device_matter_map entry
    5. Update hubitat_matter_devices with our_node_id

    This is the one-click commission flow.
    """
    import requests as req
    from services.matter_client import get_matter_client

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Step 1: Look up device
    resp = req.get(
        f"{postgrest_url}/hubitat_matter_devices",
        params={"unique_id": f"eq.{body.unique_id}"},
        headers={"Accept": "application/json"},
        timeout=5
    )
    if not resp.ok or not resp.json():
        raise HTTPException(status_code=404, detail="Device not found in discovery table")

    device = resp.json()[0]

    if not device.get('is_online'):
        raise HTTPException(status_code=400, detail=f"Device '{device['device_name']}' is offline")

    # Step 2: Open pairing window on Hubitat hub
    hub_ip = device['hub_ip']
    hubitat_node = device['hubitat_node_id']

    try:
        pair_resp = req.get(
            f"http://{hub_ip}/hub/matter/openPairingWindow",
            params={"node": hubitat_node},
            timeout=90
        )
        if not pair_resp.ok:
            raise HTTPException(
                status_code=502,
                detail=f"Hubitat returned {pair_resp.status_code} opening pairing window"
            )
        pair_data = pair_resp.json()
        setup_code = pair_data.get('setupCode') or pair_data.get('code') or pair_data.get('pairingCode')
        if not setup_code:
            # Maybe the response IS the code as a string
            if isinstance(pair_data, str):
                setup_code = pair_data
            else:
                logger.warning(f"Pairing window response: {pair_data}")
                raise HTTPException(
                    status_code=502,
                    detail=f"No setup code in Hubitat response: {pair_data}"
                )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to open pairing window on Hubitat: {e}"
        )

    # Step 3: Commission into our matter-server
    client = get_matter_client()
    if not client.is_connected:
        connected = await client.connect()
        if not connected:
            raise HTTPException(status_code=503, detail="Cannot connect to matter-server")

    try:
        result = await client.commission_with_code(str(setup_code))
        our_node_id = result.get('node_id') if isinstance(result, dict) else None
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"matter-server commission failed: {e}"
        )

    # Step 4: Create device_matter_map entry
    if our_node_id is not None:
        req.post(
            f"{postgrest_url}/device_matter_map",
            json={
                "hubitat_device_id": device['hubitat_device_id'],
                "matter_node_id": our_node_id,
                "matter_endpoint_id": 1,
                "device_name": device['device_name']
            },
            headers={
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates"
            },
            timeout=5
        )

    # Step 5: Update hubitat_matter_devices with our node ID
    req.patch(
        f"{postgrest_url}/hubitat_matter_devices",
        params={"unique_id": f"eq.{body.unique_id}"},
        json={
            "our_node_id": our_node_id,
            "is_commissioned": True
        },
        headers={"Content-Type": "application/json"},
        timeout=5
    )

    return {
        "message": f"Commissioned '{device['device_name']}'",
        "our_node_id": our_node_id,
        "hubitat_device_id": device['hubitat_device_id'],
        "setup_code_used": setup_code[:8] + "..." if setup_code else None
    }


@app.post("/api/matter/auto-commission-all", tags=["matter"])
async def matter_auto_commission_all():
    """
    Auto-commission ALL discovered, online, uncommissioned Hubitat Matter devices.

    Runs up to 3 commissions in parallel (limited by semaphore to avoid
    overwhelming the Hubitat hub or matter-server). Each device gets its
    pairing window opened and is commissioned independently.
    """
    import asyncio
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Get all online, uncommissioned devices
    resp = req.get(
        f"{postgrest_url}/hubitat_matter_devices",
        params={
            "is_online": "eq.true",
            "is_commissioned": "eq.false"
        },
        headers={"Accept": "application/json"},
        timeout=5
    )
    if not resp.ok:
        raise HTTPException(status_code=502, detail="Failed to query discovered devices")

    devices = resp.json()
    if not devices:
        return {"message": "No online uncommissioned devices found", "commissioned": 0, "failed": 0, "results": []}

    # Full parallelism — all devices commission concurrently
    sem = asyncio.Semaphore(len(devices))

    async def commission_one(device):
        """Commission a single device, respecting the semaphore."""
        unique_id = device['unique_id']
        device_name = device.get('device_name', unique_id)
        async with sem:
            try:
                body = AutoCommissionRequest(unique_id=unique_id)
                result = await matter_auto_commission(body)
                return {"device": device_name, "status": "ok", "node_id": result.get("our_node_id")}
            except HTTPException as e:
                logger.warning(f"Auto-commission failed for {device_name}: {e.detail}")
                return {"device": device_name, "status": "error", "detail": e.detail}
            except Exception as e:
                logger.warning(f"Auto-commission failed for {device_name}: {e}")
                return {"device": device_name, "status": "error", "detail": str(e)}

    # Fire all commissions concurrently (semaphore limits to 3 at a time)
    results = await asyncio.gather(*[commission_one(d) for d in devices])

    commissioned = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")

    return {
        "message": f"Commissioned {commissioned}/{len(devices)} devices",
        "commissioned": commissioned,
        "failed": failed,
        "results": results
    }


# =============================================================================
# E2E Testing
# =============================================================================


@app.get("/api/e2e/events/stream", tags=["e2e-testing"])
async def e2e_event_stream(instance_id: int = Query(...)):
    """
    SSE endpoint for E2E test events.

    Streams test execution progress (step start/complete, scenario summaries)
    for a specific instance. The frontend connects with:
        new EventSource('/api/e2e/events/stream?instance_id=2')

    Note: Live device state comes from a direct WebSocket to Hub4's
    EventSocket, not from this SSE stream. This stream is only for
    test runner progress.
    """
    from fastapi.responses import StreamingResponse
    from services.e2e_events import get_e2e_broadcaster
    import json

    broadcaster = get_e2e_broadcaster()

    async def generate():
        # Initial keepalive comment (SSE spec: lines starting with ':')
        yield ": connected\n\n"

        async for event in broadcaster.subscribe(instance_id):
            if event is None:
                yield ": keepalive\n\n"
            else:
                yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable nginx buffering for SSE
        }
    )


@app.get("/api/e2e/test/{instance_id}/scenarios", tags=["e2e-testing"])
async def get_test_scenarios(instance_id: int):
    """
    Get available test scenarios for an instance.

    Scenarios are built dynamically from the instance's device_selections
    and settings. Only scenarios relevant to the configured devices are
    returned (e.g., no dim level test if useDim is disabled).
    """
    from services.e2e_test_runner import E2ETestRunner

    runner = E2ETestRunner(instance_id)
    await runner.initialize()
    return runner.get_scenarios()


@app.get("/api/e2e/test/{instance_id}/devices", tags=["e2e-testing"])
async def get_test_devices(instance_id: int):
    """
    Get all devices for an instance with their current states.

    Returns devices grouped by category (motion_sensors, switches,
    pause_buttons, pause_switches), with live attribute data fetched
    directly from Hubitat Maker API (not from cache).

    The Maker API token is used server-side only — never exposed
    to the browser.
    """
    from services.instance_manager import get_instance_manager
    from services.device_to_hubs_classifier import fetch_device_live

    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    device_selections = instance.get("device_selections", {})

    # Pre-resolve every selected canonical id to its (hub_ip, hubitat_id)
    # via the canonical devices row joined with hub_config — used to enrich
    # the response so the frontend can render hub-specific links and open
    # one EventSocket per distinct hub without an extra roundtrip.
    from services.device_to_hubs_classifier import get_device_by_canonical_id

    result = {}
    for category, device_ids in device_selections.items():
        devices = []
        for did in device_ids:
            # Selection ids are canonical PKs (Phase 5). fetch_device_live
            # resolves them to (hub, hubitat_id) and queries the right hub.
            device = fetch_device_live(did)
            row = get_device_by_canonical_id(did)
            extras = {}
            if row:
                extras = {
                    "_canonical_id": row["id"],
                    "_hub_ip":       row.get("hub_ip"),
                    "_hub_name":     row.get("hub_name"),
                    "_hubitat_id":   row.get("hubitat_id"),
                }
            if device:
                # Carry canonical/hub metadata alongside the Maker API
                # device dict. Underscore-prefixed so they don't collide
                # with any future Hubitat field names.
                device.update(extras)
                devices.append(device)
            else:
                devices.append({
                    "id": did,
                    "label": (row.get("label") if row else f"Device {did}"),
                    "error": "not found in Maker API",
                    **extras,
                })
        result[category] = devices

    return {
        "instance_id": instance_id,
        "label": instance.get("label"),
        "settings": instance.get("settings", {}),
        "device_categories": result
    }


@app.post("/api/e2e/test/{instance_id}/run/{scenario_id}", tags=["e2e-testing"])
async def run_test_scenario(instance_id: int, scenario_id: str):
    """
    Run a specific test scenario.

    Executes the scenario steps asynchronously in a background task.
    Progress is streamed via the SSE endpoint. The HTTP response
    returns immediately with a confirmation.
    """
    from services.e2e_test_runner import E2ETestRunner
    import asyncio

    runner = E2ETestRunner(instance_id)
    await runner.initialize()

    async def run_in_background():
        """Run scenario in background task so HTTP response returns fast."""
        try:
            await runner.run_scenario(scenario_id)
        except Exception as e:
            logger.error(f"E2E scenario '{scenario_id}' failed: {e}", exc_info=True)

    asyncio.create_task(run_in_background())
    return {
        "message": f"Scenario '{scenario_id}' started",
        "instance_id": instance_id
    }


@app.post("/api/e2e/test/{instance_id}/stop", tags=["e2e-testing"])
async def stop_test(instance_id: int):
    """
    Cancel the currently-running scenario for this instance, if any.

    Sets the runner's cancel flag; the scenario loop checks it between
    steps. Returns immediately — does NOT block waiting for the in-flight
    step to actually unwind.
    """
    from services.e2e_test_runner import get_active_runner
    runner = get_active_runner(instance_id)
    if runner is None:
        return {"ok": True, "stopped": False, "reason": "no active run"}
    try:
        await runner.cancel()
        return {"ok": True, "stopped": True}
    except Exception as e:
        logger.error(f"stop_test({instance_id}) failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/e2e/test/{instance_id}/run-all", tags=["e2e-testing"])
async def run_all_test_scenarios(instance_id: int):
    """
    Run all test scenarios for an instance sequentially.

    Scenarios execute one after another in a background task.
    Progress is streamed via the SSE endpoint.
    """
    from services.e2e_test_runner import E2ETestRunner
    import asyncio

    runner = E2ETestRunner(instance_id)
    await runner.initialize()

    async def run_all():
        """Run all scenarios with device state save/restore."""
        try:
            # Snapshot device states before any tests run
            await runner.save_device_states()

            # Run all scenarios sequentially
            for scenario in runner.get_scenarios():
                try:
                    await runner.run_scenario(scenario["id"])
                except Exception as e:
                    logger.error(
                        f"E2E scenario '{scenario['id']}' failed: {e}",
                        exc_info=True
                    )
        finally:
            # Restore devices to their original states regardless of test outcome
            try:
                await runner.restore_device_states()
            except Exception as e:
                logger.error(
                    f"E2E device state restore failed: {e}",
                    exc_info=True
                )

    asyncio.create_task(run_all())
    return {"message": "All scenarios started", "instance_id": instance_id}


@app.get("/api/modes", tags=["modes"])
async def get_modes():
    """Get available location modes."""
    from services.hubitat_client import get_default_client

    try:
        client = get_default_client()
        modes = client.get_modes()
        return modes

    except Exception as e:
        logger.error(f"Failed to get modes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/modes/current", tags=["modes"])
async def get_current_mode():
    """Get current location mode."""
    from services.hubitat_client import get_default_client

    try:
        client = get_default_client()
        mode_id, mode_name = client.get_current_mode()
        return {"id": mode_id, "name": mode_name}

    except Exception as e:
        logger.error(f"Failed to get current mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Web UI
# =============================================================================


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    """Main dashboard."""
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/instance/new", response_class=HTMLResponse, include_in_schema=False)
async def new_instance(request: Request):
    """Instance creation wizard."""
    return templates.TemplateResponse(request, "instance_wizard.html")


@app.get("/api/instances/{instance_id}/events", tags=["instances"])
async def stream_instance_events(instance_id: int):
    """Recent events for an instance's subscribed devices."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # Collect all device IDs from instance's device_selections
    device_ids = []
    for ids in (instance.get('device_selections') or {}).values():
        device_ids.extend(str(d) for d in ids)

    if not device_ids:
        return []

    try:
        import requests as req
        postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        response = req.get(
            f"{postgrest_url}/event_log",
            params={
                "hubitat_device_id": f"in.({','.join(device_ids)})",
                "order": "received_at.desc",
                "limit": "50"
            },
            timeout=5
        )
        if response.status_code == 200:
            return response.json()
        return []
    except Exception as e:
        logger.error(f"Failed to get instance events: {e}")
        return []


@app.get("/matter", response_class=HTMLResponse, include_in_schema=False)
async def matter_page(request: Request):
    """Matter device management page."""
    return templates.TemplateResponse(request, "matter.html")


@app.get("/hubs", response_class=HTMLResponse, include_in_schema=False)
async def hubs_page(request: Request):
    """Hub configuration page — edit hub_config rows."""
    return templates.TemplateResponse(request, "hubs.html")


@app.get("/admin/settings", response_class=HTMLResponse, include_in_schema=False)
async def admin_settings_page(request: Request):
    """System settings page — edit rows in system_settings table.
    Reached via the gear icon in the navbar."""
    return templates.TemplateResponse(request, "admin_settings.html")


# =============================================================================
# Hub config CRUD
# =============================================================================
# All routing in the app reads from hub_config (joined into devices via
# hub_id FK). Editing hub_config from the UI lets the user change a hub's
# IP / app number / token env without redeploying. After every write we
# invalidate the in-process lookup caches so changes take effect within
# a single event-loop tick.

@app.get("/api/canonical-devices/{canonical_id}/recent-events", tags=["devices"])
async def get_recent_events_for_device(
    canonical_id: int,
    event_type: Optional[str] = None,
    limit: int = 20,
):
    """
    Return the most recent N events for a canonical device, optionally
    filtered by event_type. Used by the KPI modal's per-chip popover to
    show the last raw values that produced the breakdown count.

    event_log.hubitat_device_id contains the canonical PK post-Phase-5
    (the column name is legacy).
    """
    import requests as _req
    if limit <= 0 or limit > 200:
        limit = 20
    params = {
        "hubitat_device_id": f"eq.{canonical_id}",
        "order": "received_at.desc",
        "limit": str(limit),
        "select": "event_type,event_value,event_unit,received_at",
    }
    if event_type:
        params["event_type"] = f"eq.{event_type}"
    try:
        r = _req.get(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/event_log",
            params=params,
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"get_recent_events_for_device({canonical_id}, {event_type}) failed: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/canonical-devices", tags=["devices"])
async def list_canonical_devices():
    """
    List all rows in the canonical `devices` table.
    Used by the wizard to render chips with labels for any saved selection,
    even when the selection's device id doesn't appear in the current
    category's capability-filtered device list.
    """
    try:
        r = await aget(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/devices",
            params={"select": "id,label,hub_ip,hubitat_id", "order": "label"},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"list_canonical_devices failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hubs/health", tags=["hubs"])
async def list_hub_health():
    """
    Per-hub WS + reconcile health. Used by the dashboard alert banner to
    decide whether to surface a 'recommend Maker API as fallback' warning.
    """
    r = await aget(
        f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/hub_health",
        params={"order": "hub_id"},
        timeout=5,
    )
    if r.status_code == 200:
        return r.json()
    raise HTTPException(status_code=r.status_code, detail=r.text)


@app.get("/api/hubs", tags=["hubs"])
async def list_hubs():
    """List all configured Hubitat hubs (rows of hub_config)."""
    try:
        r = await aget(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/hub_config",
            params={"order": "id"},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"list_hubs failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _invalidate_hub_caches():
    """Drop in-process caches that reference hub_config rows."""
    try:
        from services.device_to_hubs_classifier import invalidate_device_lookup_cache
        invalidate_device_lookup_cache()
    except Exception:
        pass
    try:
        from services.webhook_router import get_webhook_router
        get_webhook_router().invalidate_device_cache()
    except Exception:
        pass


@app.patch("/api/hubs/{hub_id}", tags=["hubs"])
async def update_hub(hub_id: int, body: Dict[str, Any]):
    """
    Update a hub_config row. Accepts any subset of:
      hub_name, hub_ip, maker_api_app_number, maker_api_token_env,
      is_primary, is_enabled.
    Other fields are ignored.

    On success, invalidates the in-process device-lookup caches and
    re-syncs `devices.hub_ip` from the new `hub_config.hub_ip` for any
    rows that referenced this hub (denormalized cache stays consistent).
    """
    allowed = {
        "hub_name", "hub_ip", "maker_api_app_number",
        "maker_api_token_env", "is_primary", "is_enabled",
    }
    patch = {k: v for k, v in body.items() if k in allowed}
    if not patch:
        raise HTTPException(status_code=400, detail="No editable fields in body")

    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")

    # If the user is setting is_primary=true, clear it on every other row
    # first — exactly one primary at a time.
    if patch.get("is_primary") is True:
        try:
            await apatch(
                f"{postgrest_url}/hub_config",
                params={"id": f"neq.{hub_id}"},
                json={"is_primary": False},
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"Could not clear is_primary on others: {e}")

    try:
        r = await apatch(
            f"{postgrest_url}/hub_config",
            params={"id": f"eq.{hub_id}"},
            json=patch,
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=5,
        )
        if r.status_code not in (200, 204):
            raise HTTPException(status_code=r.status_code, detail=r.text)

        # If hub_ip changed, mirror it into devices.hub_ip (denormalized).
        if "hub_ip" in patch:
            try:
                await apatch(
                    f"{postgrest_url}/devices",
                    params={"hub_id": f"eq.{hub_id}"},
                    json={"hub_ip": patch["hub_ip"]},
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
            except Exception as e:
                logger.warning(f"Could not resync devices.hub_ip: {e}")

        _invalidate_hub_caches()
        return r.json() if r.status_code == 200 else {"ok": True, "id": hub_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"update_hub({hub_id}) failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/hubs", tags=["hubs"])
async def create_hub(body: Dict[str, Any]):
    """Create a new hub_config row."""
    required = ("hub_name", "hub_ip", "maker_api_app_number", "maker_api_token_env")
    missing = [k for k in required if not body.get(k)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {missing}")

    payload = {k: body[k] for k in required}
    payload["is_primary"] = bool(body.get("is_primary", False))
    payload["is_enabled"] = bool(body.get("is_enabled", True))

    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        r = await apost(
            f"{postgrest_url}/hub_config",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=5,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=r.status_code, detail=r.text)
        _invalidate_hub_caches()
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create_hub failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/hubs/{hub_id}", tags=["hubs"])
async def delete_hub(hub_id: int):
    """
    Delete a hub. Refuses if any device still references this hub via FK.
    User must move or remove those devices first (or run a fresh classifier
    cycle that lets them be re-homed).
    """
    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        r = await aget(
            f"{postgrest_url}/devices",
            params={"hub_id": f"eq.{hub_id}", "select": "id", "limit": "1"},
            timeout=5,
        )
        if r.status_code == 200 and r.json():
            raise HTTPException(
                status_code=409,
                detail="Hub has devices; remove or re-classify them first",
            )
        d = await adelete(
            f"{postgrest_url}/hub_config",
            params={"id": f"eq.{hub_id}"},
            timeout=5,
        )
        if d.status_code not in (200, 204):
            raise HTTPException(status_code=d.status_code, detail=d.text)
        _invalidate_hub_caches()
        return {"ok": True, "id": hub_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_hub({hub_id}) failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/instance/{instance_id}", response_class=HTMLResponse, include_in_schema=False)
async def instance_detail(request: Request, instance_id: int):
    """Instance detail/edit page."""
    return templates.TemplateResponse(
        request, "instance_detail.html", {"instance_id": instance_id}
    )


# =============================================================================
# Dashboard WebSocket (real-time updates — replaces polling)
# =============================================================================


@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for real-time dashboard updates.

    Replaces the 30-second polling loop. When Hubitat webhooks arrive,
    events are pushed instantly to all connected dashboard clients.
    The frontend uses these events to patch individual cards instead
    of re-rendering the entire grid (no more flicker).

    Message types sent to client:
        - device_event: A device changed state
        - instance_update: Instance metadata changed
        - instances_snapshot: Full instance list (sent on connect)
    """
    from services.dashboard_broadcaster import get_dashboard_broadcaster
    from services.instance_manager import get_instance_manager
    import json

    await websocket.accept()
    broadcaster = get_dashboard_broadcaster()
    queue = await broadcaster.connect()

    try:
        # Send initial snapshot so the client doesn't need a separate fetch
        manager = get_instance_manager()
        instances = manager.get_all_instances()
        await websocket.send_json({
            "type": "instances_snapshot",
            "instances": instances
        })

        # Stream events to client
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                # Keepalive ping — detect dead connections
                await websocket.send_json({"type": "ping"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"Dashboard WS closed: {e}")
    finally:
        await broadcaster.disconnect(queue)


# =============================================================================
# KPI Metrics
# =============================================================================


@app.get("/api/instances/{instance_id}/metrics", tags=["instances"])
async def get_instance_metrics(
    instance_id: int,
    hours: int = Query(24, description="Lookback window in hours")
):
    """
    Aggregated KPI metrics for a single instance.

    Queries event_log for the instance's subscribed devices and computes:
    - Event counts (total, per-hour, per-device, per-type)
    - Last activity timestamps per device
    - Switch on/off operation counts
    - Motion active/inactive ratios
    - Error tracking from app_instances table

    Args:
        instance_id: Target instance
        hours: Lookback window (default 24h)
    """
    from services.instance_manager import get_instance_manager
    import requests as req
    from datetime import datetime, timedelta, timezone

    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # Collect device IDs from instance
    device_ids = []
    for ids in (instance.get('device_selections') or {}).values():
        device_ids.extend(str(d) for d in ids)

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()

    # Fetch events for this instance's devices within the time window
    events = []
    if device_ids:
        try:
            resp = req.get(
                f"{postgrest_url}/event_log",
                params={
                    "hubitat_device_id": f"in.({','.join(device_ids)})",
                    "received_at": f"gte.{since}",
                    "order": "received_at.desc",
                    "limit": "2000"
                },
                timeout=10
            )
            if resp.status_code == 200:
                events = resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch metrics events: {e}")

    # Compute aggregations
    total_events = len(events)

    # Events per hour (for chart)
    hourly_buckets = {}
    for h in range(hours):
        bucket_time = now - timedelta(hours=h)
        key = bucket_time.strftime('%Y-%m-%dT%H:00:00')
        hourly_buckets[key] = 0

    # Per-device stats
    device_stats = {}
    # Per-event-type counts
    type_counts = {}

    for evt in events:
        # Hourly bucketing
        received = evt.get('received_at', '')
        if received:
            try:
                # Truncate to hour
                hour_key = received[:13] + ':00:00'
                if hour_key in hourly_buckets:
                    hourly_buckets[hour_key] += 1
            except (IndexError, TypeError):
                pass

        # Device stats
        dev_id = evt.get('hubitat_device_id', '')
        dev_name = evt.get('device_name', dev_id)
        evt_type = evt.get('event_type', '')
        evt_value = evt.get('event_value', '')

        if dev_id not in device_stats:
            device_stats[dev_id] = {
                'device_name': dev_name,
                'event_count': 0,
                'last_event_at': received,
                'last_event_type': evt_type,
                'last_event_value': evt_value,
                'type_breakdown': {}
            }
        device_stats[dev_id]['event_count'] += 1

        # Type breakdown per device
        if evt_type not in device_stats[dev_id]['type_breakdown']:
            device_stats[dev_id]['type_breakdown'][evt_type] = {
                'count': 0,
                'last_value': evt_value,
                'last_at': received
            }
        device_stats[dev_id]['type_breakdown'][evt_type]['count'] += 1

        # Global type counts
        type_counts[evt_type] = type_counts.get(evt_type, 0) + 1

    # Sort hourly buckets chronologically
    hourly_sorted = sorted(hourly_buckets.items())

    # Enrich device_stats with canonical-→hub mapping so the UI can render
    # a hyperlink to the device's edit page on the hub that natively owns
    # it. event_log.hubitat_device_id contains the CANONICAL devices.id PK
    # post-Phase-5 (the column name is legacy). For each id we look up the
    # canonical row + its hub_config join in one batch.
    if device_stats:
        try:
            import requests as _req
            postgrest_url = os.environ.get(
                "POSTGREST_URL", "http://postgrest:3001"
            )
            ids_csv = ",".join(str(k) for k in device_stats.keys())
            resp = _req.get(
                f"{postgrest_url}/devices",
                params={
                    "select": "id,hubitat_id,label,hub_config(hub_name,hub_ip)",
                    "id": f"in.({ids_csv})",
                },
                timeout=5,
            )
            if resp.status_code == 200:
                for row in resp.json():
                    cid = str(row["id"])
                    if cid in device_stats:
                        hub = row.get("hub_config") or {}
                        device_stats[cid]["canonical_id"]  = row["id"]
                        device_stats[cid]["hubitat_id"]    = row.get("hubitat_id")
                        device_stats[cid]["hub_ip"]        = hub.get("hub_ip")
                        device_stats[cid]["hub_name"]      = hub.get("hub_name")
                        # Prefer the canonical label when we have it — the
                        # event_log device_name may carry the mirror's
                        # ' on Home N' suffix.
                        if row.get("label"):
                            device_stats[cid]["device_name"] = row["label"]
        except Exception as e:
            logger.warning(f"KPI device enrichment failed: {e}")

    # Instance metadata
    running = manager.get_running_instance(instance_id) is not None

    return {
        "instance_id": instance_id,
        "label": instance.get("label"),
        "is_paused": instance.get("is_paused", False),
        "is_running": running,
        "error_count": instance.get("error_count", 0),
        "last_error": instance.get("last_error"),
        "last_activity_at": instance.get("last_activity_at"),
        "created_at": instance.get("created_at"),
        "device_count": len(device_ids),
        "window_hours": hours,
        "total_events": total_events,
        "hourly_events": [
            {"hour": h, "count": c} for h, c in hourly_sorted
        ],
        "device_stats": device_stats,
        "type_counts": type_counts,
        "device_selections": instance.get("device_selections", {}),
        "settings": instance.get("settings", {}),
    }


# =============================================================================
# Error Handlers
# =============================================================================


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    """Handle 404 errors — JSON for API routes, HTML for web routes."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=404,
            content={"error": exc.detail if hasattr(exc, "detail") else "Not found"},
        )
    return templates.TemplateResponse(
        request, "error.html", {"error": "Page not found"}, status_code=404
    )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc: Exception):
    """Handle 500 errors — JSON for API routes, HTML for web routes."""
    logger.error(f"Server error: {exc}")
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"},
        )
    return templates.TemplateResponse(
        request, "error.html", {"error": "Server error"}, status_code=500
    )


# =============================================================================
# Development server
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=True)
