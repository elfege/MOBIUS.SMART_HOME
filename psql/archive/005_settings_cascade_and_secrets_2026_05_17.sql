-- Migration: settings cascade (system + app-type), encrypted secrets, boot log
-- Date: 2026-05-17
-- See docs/plans/comprehensive_settings_and_ui_overhaul_2026_05_17.md
--
-- POLICY (enforced by unit test in tests/unit/test_settings_cascade_disjoint.py):
--   A setting key MUST live at exactly ONE configurable layer.
--   If exposed at instance UI level (app_types.settings_schema), it must NOT
--   also appear in app_type_settings for the same app_type_id. The cascade
--   resolves: instance → app_type → system → code-default.

-- ============================================================================
-- system_settings: cross-cutting platform knobs
-- ============================================================================
CREATE TABLE IF NOT EXISTS system_settings (
  key              VARCHAR(80) PRIMARY KEY,
  value            TEXT NOT NULL,
  value_type       VARCHAR(20) NOT NULL,  -- 'int' | 'bool' | 'string' | 'json' | 'float'
  description      TEXT,
  ui_exposed       BOOLEAN DEFAULT TRUE,
  requires_restart BOOLEAN DEFAULT FALSE,
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- app_type_settings: per-app-type globals (one row per (app_type, key))
-- ============================================================================
CREATE TABLE IF NOT EXISTS app_type_settings (
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
);
CREATE INDEX IF NOT EXISTS idx_app_type_settings_type ON app_type_settings(app_type_id);

-- ============================================================================
-- encrypted_secrets: tokens, encrypted with KEK (KEK from AWS SM at boot)
-- ============================================================================
CREATE TABLE IF NOT EXISTS encrypted_secrets (
  key          VARCHAR(80) PRIMARY KEY,
  ciphertext   BYTEA NOT NULL,
  kek_version  INTEGER NOT NULL DEFAULT 1,
  description  TEXT,
  rotated_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  updated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- system_boot_log: audit which tier loaded secrets at each boot
-- ============================================================================
CREATE TABLE IF NOT EXISTS system_boot_log (
  id             BIGSERIAL PRIMARY KEY,
  boot_at        TIMESTAMPTZ DEFAULT NOW(),
  secrets_source VARCHAR(40),  -- 'aws-sm' | 'env-cleartext' | 'passphrase-prompt' | 'cached-tmpfs' | 'unknown'
  kek_version    INTEGER,
  notes          TEXT
);
CREATE INDEX IF NOT EXISTS idx_system_boot_log_at ON system_boot_log(boot_at DESC);

-- ============================================================================
-- Seed system_settings with the knobs we currently read from env vars.
-- All idempotent via ON CONFLICT DO NOTHING — re-running won't clobber edits.
-- ============================================================================
INSERT INTO system_settings (key, value, value_type, description, ui_exposed, requires_restart) VALUES
  ('motion_timeout_floor_seconds', '60', 'int',
   'Minimum no-motion timeout in seconds. AML/Fan clamp computed timeouts to this floor unless the instance has bypassTimeoutFloor=true. PIR sensors typically have 10-60s re-trigger cooldown; setting below this causes off/on flicker.',
   TRUE, FALSE),
  ('reconcile_interval_secs', '60', 'int',
   'Normal cadence for the device-state reconcile poll. Aggressive cadence kicks in after a recent hub WS failure.',
   TRUE, FALSE),
  ('reconcile_aggressive_secs', '10', 'int',
   'Aggressive reconcile cadence after a recent hub WS failure (within reconcile_aggressive_window_secs).',
   TRUE, FALSE),
  ('reconcile_aggressive_window_secs', '300', 'int',
   'How recently a hub WS failure must have occurred to count as "recent" and engage aggressive reconcile.',
   TRUE, FALSE),
  ('eventsocket_watchdog_secs', '120', 'int',
   'Recycle the WS connection if no events arrive within this window. Catches zombie sockets.',
   TRUE, FALSE),
  ('device_cmd_verify_retries', '3', 'int',
   'Polls per command-send attempt to verify the device reached desired state.',
   TRUE, FALSE),
  ('device_cmd_verify_delay', '1.0', 'float',
   'Seconds between verify polls.', TRUE, FALSE),
  ('device_cmd_operation_retries', '2', 'int',
   'Full send+verify cycles before giving up on a command.', TRUE, FALSE)
ON CONFLICT (key) DO NOTHING;

-- Lifecycle toggles — DB but require_restart=TRUE (yellow badge in UI).
INSERT INTO system_settings (key, value, value_type, description, ui_exposed, requires_restart) VALUES
  ('eventsocket_enabled', 'true', 'bool',
   'Master switch for the Hubitat eventsocket WS intake. Disabling makes the app blind to all device events. Requires app restart to take effect.',
   TRUE, TRUE),
  ('reconcile_poll_enabled', 'true', 'bool',
   'Periodic state-reconcile poll on/off. Requires app restart.', TRUE, TRUE),
  ('device_commands_logging', 'true', 'bool',
   'Two-phase device_commands logging (pending → confirmed/failed). Requires app restart.',
   TRUE, TRUE),
  ('webhook_intake_enabled', 'false', 'bool',
   'Legacy Maker API webhook intake — disabled by default since eventsocket-SOT migration on 2026-05-16. Enable only for rollback. Requires app restart.',
   TRUE, TRUE)
ON CONFLICT (key) DO NOTHING;

-- ============================================================================
-- Permissions
-- ============================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON system_settings    TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_type_settings  TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON encrypted_secrets  TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON system_boot_log    TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smarthome_anon;

-- Reload PostgREST schema cache (otherwise new tables are invisible to /api/* until next restart).
NOTIFY pgrst, 'reload schema';
