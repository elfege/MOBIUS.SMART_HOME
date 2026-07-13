-- Migration: full data-oriented traceability (events, routings, commands, state changes)
-- Date: 2026-05-16
-- Why: user mandate — "this app should be fully data oriented: I want everything logged"
--      + eventsocket becomes sole intake path (Maker API webhook deprecated)
--      + many-to-many relationships should be join tables, not JSONB blobs
--
-- This file is the canonical source of the schema. The same statements are
-- duplicated in app.py::run_db_migrations() so they apply on every container
-- start (idempotent via IF NOT EXISTS / ON CONFLICT DO NOTHING).

-- ============================================================================
-- event_log: add columns for proper provenance
-- ============================================================================
-- hub_ip            — which hub's WS connection delivered the event
-- canonical_device_id — FK to devices(id); replaces ambiguous hubitat_device_id
--                    (which today holds a mix of canonical and native IDs).
--                    Old column stays for backwards-compat during transition.
-- intake_path       — 'eventsocket' | 'reconcile' (the two ways events enter)
-- processing_ms     — ms between WS frame receipt and router-done

ALTER TABLE event_log
  ADD COLUMN IF NOT EXISTS hub_ip VARCHAR(50),
  ADD COLUMN IF NOT EXISTS canonical_device_id BIGINT REFERENCES devices(id),
  ADD COLUMN IF NOT EXISTS intake_path VARCHAR(20),
  ADD COLUMN IF NOT EXISTS processing_ms INTEGER;

CREATE INDEX IF NOT EXISTS idx_event_log_canonical
  ON event_log(canonical_device_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_log_intake_time
  ON event_log(intake_path, received_at DESC);

-- ============================================================================
-- event_routings: M:N join between event_log and app_instances
-- ============================================================================
-- Replaces event_log.routed_to_instances JSONB blob with a proper join table.
-- Lets us answer "what events did instance X receive in the last hour?" with
-- a clean JOIN instead of parsing JSONB.
--
-- outcome values:
--   'routed'         — enqueued to the instance's worker
--   'dropped_mesh'   — mesh-mirror, not the origin hub
--   'dropped_unsub'  — instance not subscribed to this (device,event_type)
--   'dropped_orphan' — no canonical row for this device
--   'failed_enqueue' — exception while enqueueing
CREATE TABLE IF NOT EXISTS event_routings (
  id              BIGSERIAL PRIMARY KEY,
  event_id        BIGINT NOT NULL REFERENCES event_log(id) ON DELETE CASCADE,
  instance_id     BIGINT REFERENCES app_instances(id) ON DELETE SET NULL,
  enqueued_at     TIMESTAMPTZ DEFAULT NOW(),
  processed_at    TIMESTAMPTZ,
  outcome         VARCHAR(30) NOT NULL,
  drop_reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_routings_event ON event_routings(event_id);
CREATE INDEX IF NOT EXISTS idx_event_routings_instance
  ON event_routings(instance_id, enqueued_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_routings_outcome
  ON event_routings(outcome, enqueued_at DESC);

-- ============================================================================
-- device_commands: every command we send, two-phase (issued → confirmed/failed)
-- ============================================================================
-- Every send_command() call writes a row at issue (status='pending') and
-- updates it on completion. Causal chain via triggered_by_event_id.
-- Future: retry-on-condition-still-valid will use parent_command_id to chain
-- attempts; for now max_attempts is 1 (single fire, observe outcome).
CREATE TABLE IF NOT EXISTS device_commands (
  id                       BIGSERIAL PRIMARY KEY,
  instance_id              BIGINT REFERENCES app_instances(id) ON DELETE SET NULL,
  canonical_device_id      BIGINT REFERENCES devices(id) ON DELETE SET NULL,
  hubitat_device_id        VARCHAR(50),    -- the per-hub native id used in the HTTP call
  hub_ip                   VARCHAR(50),    -- hub that received the command
  command                  VARCHAR(50) NOT NULL,
  arguments                JSONB DEFAULT '[]'::jsonb,
  desired_attribute        VARCHAR(50),    -- 'switch' / 'level' / 'colorTemperature' etc.
  desired_value            VARCHAR(200),
  triggered_by_event_id    BIGINT REFERENCES event_log(id) ON DELETE SET NULL,
  parent_command_id        BIGINT REFERENCES device_commands(id) ON DELETE SET NULL,
  attempt                  INTEGER DEFAULT 1,
  max_attempts             INTEGER DEFAULT 1,
  issued_at                TIMESTAMPTZ DEFAULT NOW(),
  completed_at             TIMESTAMPTZ,
  outcome                  VARCHAR(30) DEFAULT 'pending',
    -- 'pending' | 'confirmed' | 'failed_verify' | 'failed_network' | 'failed_timeout'
  final_observed_value     VARCHAR(200),
  verify_retries_used      INTEGER,
  latency_ms               INTEGER,
  error                    TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_commands_device
  ON device_commands(canonical_device_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_device_commands_instance
  ON device_commands(instance_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_device_commands_outcome
  ON device_commands(outcome, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_device_commands_trig
  ON device_commands(triggered_by_event_id);
CREATE INDEX IF NOT EXISTS idx_device_commands_parent
  ON device_commands(parent_command_id);

-- ============================================================================
-- instance_state_log: pause/resume/mode/settings transitions
-- ============================================================================
-- Lets us reconstruct any instance's lifetime: when was it paused, by what,
-- what mode was it in when X happened, etc.
CREATE TABLE IF NOT EXISTS instance_state_log (
  id            BIGSERIAL PRIMARY KEY,
  instance_id   BIGINT NOT NULL REFERENCES app_instances(id) ON DELETE CASCADE,
  transition    VARCHAR(40) NOT NULL,
    -- 'started' | 'stopped' | 'paused' | 'resumed' | 'mode_changed'
    -- | 'settings_updated' | 'devices_updated'
  details       JSONB DEFAULT '{}'::jsonb,
  actor         VARCHAR(60),
    -- 'user_ui' | 'button:<canonical_id>' | 'schedule' | 'system'
  occurred_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_instance_state_log_instance
  ON instance_state_log(instance_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_instance_state_log_transition
  ON instance_state_log(transition, occurred_at DESC);

-- ============================================================================
-- mode_change_log: hub location-mode timeline
-- ============================================================================
-- Replaces the mostly-empty location_modes table for *historical* mode data.
-- location_modes still tracks "current active mode" by is_active flag.
CREATE TABLE IF NOT EXISTS mode_change_log (
  id                    BIGSERIAL PRIMARY KEY,
  mode_name             VARCHAR(60) NOT NULL,
  became_active_at      TIMESTAMPTZ DEFAULT NOW(),
  became_inactive_at    TIMESTAMPTZ,
  source                VARCHAR(40)
    -- 'hubitat_event' | 'manual_ui' | 'system'
);
CREATE INDEX IF NOT EXISTS idx_mode_change_log_active
  ON mode_change_log(became_active_at DESC);

-- ============================================================================
-- hub_health: per-hub connection & traffic health (used by reconcile poll)
-- ============================================================================
-- One row per hub_config row. Updated in place by the eventsocket client
-- on every connect / disconnect / event. The reconcile-poll service reads
-- this to decide whether to run an aggressive 10s pass (recent failure) or
-- the normal 60s pass.
CREATE TABLE IF NOT EXISTS hub_health (
  hub_id                   INTEGER PRIMARY KEY REFERENCES hub_config(id) ON DELETE CASCADE,
  ws_connected             BOOLEAN DEFAULT FALSE,
  ws_connected_since       TIMESTAMPTZ,
  ws_last_event_at         TIMESTAMPTZ,
  ws_last_failure_at       TIMESTAMPTZ,
  ws_last_failure_reason   TEXT,
  ws_consecutive_failures  INTEGER DEFAULT 0,
  ws_reconnects_24h        INTEGER DEFAULT 0,
  ws_events_received_24h   BIGINT DEFAULT 0,
  last_reconcile_at        TIMESTAMPTZ,
  last_reconcile_diffs     INTEGER DEFAULT 0,
  updated_at               TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- PostgREST permissions (anon role gets SELECT/INSERT/UPDATE/DELETE; same
-- pattern as existing tables — auth is at the network layer, not row-level).
-- ============================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON event_routings    TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON device_commands   TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON instance_state_log TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON mode_change_log   TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub_health        TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smarthome_anon;

-- Seed hub_health rows so reconcile-poll has somewhere to write from boot.
INSERT INTO hub_health (hub_id)
SELECT id FROM hub_config WHERE is_enabled = TRUE
ON CONFLICT (hub_id) DO NOTHING;
