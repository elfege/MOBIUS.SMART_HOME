-- Migration: per-field exceptions for system-enforced settings (motion floor etc)
-- Date: 2026-05-17
-- See docs/plans/comprehensive_settings_and_ui_overhaul_2026_05_17.md
--
-- POLICY: when the runtime enforces a guardrail (e.g.,
-- system_settings.motion_timeout_floor_seconds), the user opt-out is
-- per-FIELD, DB-registered (audit trail via granted_at), never a JSONB flag.
--
-- setting_path uses dotted notation. Current consumers:
--   'noMotionTime'           — AML default no-motion timeout
--   'modeTimeouts.<ModeName>' — AML per-mode timeout for the named Hubitat mode
--
-- Future consumers (different guardrails) reuse the same table with their
-- own setting_path namespace, e.g. 'maxOnSeconds', 'reconcileInterval', ...

CREATE TABLE IF NOT EXISTS instance_setting_exceptions (
    id            BIGSERIAL PRIMARY KEY,
    instance_id   BIGINT NOT NULL REFERENCES app_instances(id) ON DELETE CASCADE,
    setting_path  VARCHAR(120) NOT NULL,
    reason        TEXT,
    granted_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (instance_id, setting_path)
);

CREATE INDEX IF NOT EXISTS idx_ise_instance
  ON instance_setting_exceptions(instance_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON instance_setting_exceptions
  TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smarthome_anon;

-- Refresh PostgREST schema cache so the new table is visible via /api/*
NOTIFY pgrst, 'reload schema';
