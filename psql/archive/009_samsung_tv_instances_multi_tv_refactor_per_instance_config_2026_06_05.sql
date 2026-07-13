-- Migration 009: samsung_tv_instances — multi-TV refactor, per-instance config in DB.
--
-- Replaces the single-tenant env-var-driven Samsung TV "router" with a
-- multi-instance registry whose source of truth is this table.
--
-- Before: ONE TV per process. Config (IP, MAC, token, SSL, name) lives in
-- env vars (`SAMSUNG_TV_IP`, `SAMSUNG_TV_MAC`, `SAMSUNG_TV_TOKEN`,
-- `SAMSUNG_TV_SSL`, `SAMSUNG_TV_NAME`) with personal defaults baked into
-- `apps/samsung_tv/blueprint.py:244-248`. Token persisted to a single file
-- (`/app/state/samsung_tv_token.txt`); callbacks to a single file
-- (`/app/state/samsung_tv_callbacks.json`).
--
-- After: N TVs, each a row here. Config / token / callbacks live in this
-- row. `services/samsung_tv_registry.py` (next commit) owns one
-- `SamsungTVClient` per enabled row; the router accepts an `{id}` URL
-- segment to dispatch.
--
-- Plan: docs/plans/samsung_tv_multi_instance_refactor_per_instance_ip_mac_token_in_database.md
-- Driver: user request 2026-06-05 — IP per-instance in DB, not env-based.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The table itself (dsapp — automation/instance domain).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dsapp.samsung_tv_instances (
    id              SERIAL PRIMARY KEY,
    -- Human-readable label shown in the UI ("Living Room TV", "Bedroom TV").
    -- Distinct from samsung_name (which is the slug the Samsung WS API
    -- echoes back to identify *this controller*, not the TV itself).
    label           VARCHAR(120) NOT NULL,
    -- TV LAN address (IPv4 or IPv6 string form).
    tv_ip           VARCHAR(45)  NOT NULL,
    -- Optional MAC for Wake-on-LAN (canonical "XX:XX:XX:XX:XX:XX"). Some
    -- non-Samsung Wake methods (e.g. a smart-plug) make this unnecessary,
    -- so it's nullable rather than required.
    mac_address     VARCHAR(17),
    -- Most modern Samsungs use wss://. The toggle exists for older sets
    -- that only accept ws:// and for diagnostic fallback.
    use_ssl         BOOLEAN      NOT NULL DEFAULT TRUE,
    -- The slug the WS handshake includes as the controller identity. Once
    -- a token is paired against a specific samsung_name, changing the
    -- samsung_name invalidates the token (and the TV will prompt the user
    -- to re-authorize). The default mirrors the old SAMSUNG_TV_NAME env.
    samsung_name    VARCHAR(120) NOT NULL DEFAULT 'mobius_smart_home',
    -- The application name shown on the TV's "Allowed Controllers" list.
    -- Independent from samsung_name (the technical id) so the operator
    -- can rename the displayed controller without invalidating the token.
    app_name        VARCHAR(120) NOT NULL DEFAULT 'Smart Home Controller',
    -- Long-lived auth token returned by the TV after the user accepts
    -- the SmartView prompt. Persisted here so it survives container
    -- restarts (replaces the on-disk /app/state/samsung_tv_token.txt).
    -- 64-char wide for headroom (real tokens are typically 8-32 chars).
    token           VARCHAR(64),
    -- Hubitat-device → external-URL callbacks the TV controller should
    -- POST to on state change. Same shape as the old on-disk
    -- /app/state/samsung_tv_callbacks.json, just scoped per TV:
    --   { "<hub_device_id>": { "url": "...", "events": ["power_state"] }, ... }
    callbacks       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- Soft-disable: keeps the row + token + callbacks but stops the
    -- registry from spinning up a client for it. Useful for "I unplugged
    -- this TV but I'll plug it back in next week."
    is_enabled      BOOLEAN      NOT NULL DEFAULT TRUE,
    -- Runtime pause flag (separate from is_enabled — pause is a temporary
    -- runtime state, enable is a deliberate configuration choice). Same
    -- two-flag pattern as dsapp.app_instances.
    is_paused       BOOLEAN      NOT NULL DEFAULT FALSE,
    pause_reason    VARCHAR(50),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- Same (tv_ip, samsung_name) shouldn't be registered twice. Two
    -- different controllers targeting the same TV from the same Mobius
    -- instance is always a configuration bug; surface it loudly via the
    -- unique constraint instead of silently double-controlling.
    CONSTRAINT samsung_tv_instances_tv_ip_samsung_name_key UNIQUE (tv_ip, samsung_name)
);

CREATE INDEX IF NOT EXISTS idx_samsung_tv_instances_enabled
    ON dsapp.samsung_tv_instances (is_enabled)
    WHERE is_enabled;

COMMENT ON TABLE dsapp.samsung_tv_instances IS
    'Per-TV configuration for the multi-instance Samsung router. One row per '
    'TV the system controls. Replaces the single-tenant env-var config from '
    'before migration 009.';

-- ---------------------------------------------------------------------------
-- 2. updated_at trigger — uses the same generic function as app_instances.
--    The trigger function is defined in init-db.sql; idempotent
--    DROP/CREATE keeps reruns safe.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS update_samsung_tv_instances_updated_at
    ON dsapp.samsung_tv_instances;
CREATE TRIGGER update_samsung_tv_instances_updated_at
    BEFORE UPDATE ON dsapp.samsung_tv_instances
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ---------------------------------------------------------------------------
-- 3. PostgREST surface: 1:1 view in `api` schema (consistent with the
--    schema-split pattern from migration 007).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW api.samsung_tv_instances AS
    SELECT * FROM dsapp.samsung_tv_instances;

-- ---------------------------------------------------------------------------
-- 4. Grants. Migration 007's ALTER DEFAULT PRIVILEGES on dsapp already
--    covers the new table; this explicit grant is belt-and-braces in case
--    the migration is run out-of-order on a snapshot that pre-dates 007.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON dsapp.samsung_tv_instances TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON api.samsung_tv_instances   TO smarthome_anon;
GRANT USAGE, SELECT ON SEQUENCE dsapp.samsung_tv_instances_id_seq  TO smarthome_anon;

COMMIT;
