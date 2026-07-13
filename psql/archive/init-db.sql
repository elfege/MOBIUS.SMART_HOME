-- =============================================================================
-- 0_MOBIUS.SMART_HOME Database Schema
-- Multi-instance automation system for Hubitat integration
-- =============================================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================================================
-- ROLES AND PERMISSIONS
-- =============================================================================

-- Create anonymous role for PostgREST (unauthenticated access)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'smarthome_anon') THEN
        CREATE ROLE smarthome_anon NOLOGIN;
    END IF;
END
$$;

-- Grant usage on schema
GRANT USAGE ON SCHEMA public TO smarthome_anon;

-- =============================================================================
-- APP TYPES TABLE
-- =============================================================================
-- Metadata about available app types (populated at startup from Python code)
-- Each app type is a blueprint that users can instantiate multiple times

CREATE TABLE IF NOT EXISTS app_types (
    id SERIAL PRIMARY KEY,

    -- Type identification
    type_name VARCHAR(100) NOT NULL UNIQUE,       -- 'advanced_motion_lighting'
    display_name VARCHAR(200) NOT NULL,            -- 'Advanced Motion Lighting'
    description TEXT,
    version VARCHAR(20) DEFAULT '1.0.0',

    -- JSON Schema for settings validation (enables dynamic form generation)
    settings_schema JSONB NOT NULL DEFAULT '{}',

    -- Device categories this app type needs (for wizard device picker)
    -- Example: [{"key": "motion_sensors", "capability": "motionSensor", "multiple": true}]
    device_categories JSONB NOT NULL DEFAULT '[]',

    -- Metadata
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- APP INSTANCES TABLE
-- =============================================================================
-- THE CORE OF MULTI-INSTANCE ARCHITECTURE
-- Each row = one user-created instance of an app type
-- Example: "Advanced Lights - Office", "Advanced Lights - Bedroom"

CREATE TABLE IF NOT EXISTS app_instances (
    id BIGSERIAL PRIMARY KEY,

    -- Instance identification
    instance_uuid UUID DEFAULT uuid_generate_v4() UNIQUE,

    -- Type reference
    app_type_id INTEGER NOT NULL REFERENCES app_types(id) ON DELETE RESTRICT,

    -- User-defined label (unique per app type)
    label VARCHAR(200) NOT NULL,

    -- Configuration (all settings as JSON)
    -- Example: {"noMotionTime": 5, "useDim": true, "defaultDimLevel": 75}
    settings JSONB NOT NULL DEFAULT '{}',

    -- Selected devices by category
    -- Example: {"motion_sensors": ["123", "456"], "switches": ["789"]}
    device_selections JSONB NOT NULL DEFAULT '{}',

    -- Runtime state
    is_paused BOOLEAN DEFAULT false,
    pause_expires_at TIMESTAMPTZ,
    pause_reason VARCHAR(200),

    -- Memoization state (remembers which devices app controlled vs user)
    -- Example: {"switch_state": {"Office Light": "on"}, "dim_level": {"Office Light": 75}}
    memoization_state JSONB DEFAULT '{}',

    -- Health monitoring
    last_activity_at TIMESTAMPTZ,
    error_count INTEGER DEFAULT 0,
    last_error TEXT,

    -- Lifecycle
    is_enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- Unique constraint: label must be unique per app type
    CONSTRAINT app_instances_label_unique UNIQUE (app_type_id, label)
);

-- Index for fast lookup by type and enabled status
CREATE INDEX IF NOT EXISTS idx_app_instances_type ON app_instances(app_type_id);
CREATE INDEX IF NOT EXISTS idx_app_instances_enabled ON app_instances(is_enabled) WHERE is_enabled = true;
CREATE INDEX IF NOT EXISTS idx_app_instances_paused ON app_instances(is_paused) WHERE is_paused = true;

-- =============================================================================
-- DEVICE SUBSCRIPTIONS TABLE
-- =============================================================================
-- Maps Hubitat device IDs to app instances for event routing
-- When Hubitat sends a webhook, we query this table to find which instances care

CREATE TABLE IF NOT EXISTS device_subscriptions (
    id BIGSERIAL PRIMARY KEY,

    -- Canonical device reference. Hubitat per-hub ids are NOT unique across
    -- a multi-hub setup (the same id can identify different physical devices
    -- on different hubs), so the routing key is our own devices.id PK.
    device_id BIGINT NOT NULL,

    -- Instance reference
    instance_id BIGINT NOT NULL REFERENCES app_instances(id) ON DELETE CASCADE,

    -- Event type filter (motion, switch, contact, illuminance, mode, etc.)
    event_type VARCHAR(50) NOT NULL,

    -- Subscription metadata
    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- Unique constraint: one subscription per (canonical device, instance, event)
    CONSTRAINT device_subscriptions_unique
        UNIQUE (device_id, instance_id, event_type)
);

-- Primary index for event routing: device_id + event_type → instance_ids
CREATE INDEX IF NOT EXISTS idx_device_subscriptions_canonical
    ON device_subscriptions(device_id, event_type);

-- Secondary index for cleanup when instance deleted (handled by CASCADE)
CREATE INDEX IF NOT EXISTS idx_device_subscriptions_instance
    ON device_subscriptions(instance_id);

-- Add the FK to devices(id) only after `devices` is created (later in this file).
-- Idempotent: skipped on existing installs that already have it.
DO $devsubsfk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'device_subscriptions_device_id_fkey'
    ) THEN
        BEGIN
            ALTER TABLE device_subscriptions
              ADD CONSTRAINT device_subscriptions_device_id_fkey
              FOREIGN KEY (device_id) REFERENCES devices(id);
        EXCEPTION WHEN undefined_table THEN
            -- `devices` not yet created in this run; the constraint will be
            -- added at the bottom of this file in a follow-up DO block.
            NULL;
        END;
    END IF;
END$devsubsfk$;

-- =============================================================================
-- DEVICE CACHE TABLE
-- =============================================================================
-- Cache of Hubitat device states to reduce API polling
-- Refreshed on events and periodically via TTL

CREATE TABLE IF NOT EXISTS device_cache (
    -- Canonical devices.id PK (post-Phase-5). Hubitat per-hub ids are NOT
    -- unique across hubs, so the cache primary key must be our own PK.
    -- See `devices` table for the canonical hub_ip + hubitat_id pair.
    device_id BIGINT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,

    -- Device metadata
    device_name VARCHAR(200),
    device_label VARCHAR(200),
    device_type VARCHAR(100),

    -- Capabilities list (for filtering in device picker)
    -- Example: ["motionSensor", "temperatureMeasurement", "battery"]
    capabilities JSONB DEFAULT '[]',

    -- Current attribute values
    -- Example: {"motion": "inactive", "temperature": 72, "battery": 85}
    attributes JSONB DEFAULT '{}',

    -- Cache metadata
    last_synced_at TIMESTAMPTZ DEFAULT NOW(),
    sync_source VARCHAR(50) DEFAULT 'api'  -- 'api' or 'webhook'
);

CREATE INDEX IF NOT EXISTS idx_device_cache_synced ON device_cache(last_synced_at);

-- =============================================================================
-- EVENT LOG TABLE
-- =============================================================================
-- Log of all processed events (for debugging, analytics, and audit)
-- Consider partitioning by time for large deployments

CREATE TABLE IF NOT EXISTS event_log (
    id BIGSERIAL PRIMARY KEY,

    -- Source device
    hubitat_device_id VARCHAR(50) NOT NULL,
    device_name VARCHAR(200),

    -- Event data
    event_type VARCHAR(50) NOT NULL,              -- motion, switch, mode, etc.
    event_value VARCHAR(200),                     -- active, inactive, on, off
    event_unit VARCHAR(50),                       -- %, lux, F, etc.

    -- Routing info (which instances received this event)
    routed_to_instances JSONB DEFAULT '[]',

    -- Raw webhook payload for debugging
    raw_payload JSONB,

    -- Timestamp
    received_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for querying recent events by device
CREATE INDEX IF NOT EXISTS idx_event_log_time ON event_log(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_log_device ON event_log(hubitat_device_id, received_at DESC);

-- =============================================================================
-- SCHEDULED JOBS TABLE
-- =============================================================================
-- Persistent storage for scheduled tasks (timeouts, health checks, pause expiry)
-- APScheduler can use this for durability across restarts

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    -- Job identification
    job_id VARCHAR(100) PRIMARY KEY,              -- 'timeout_12345_instance_1'

    -- Association to instance (optional - some jobs are global)
    instance_id BIGINT REFERENCES app_instances(id) ON DELETE CASCADE,

    -- Job details
    job_type VARCHAR(50) NOT NULL,                -- 'turn_off', 'health_check', 'pause_expire'
    execute_at TIMESTAMPTZ NOT NULL,

    -- Job payload (parameters for the job)
    payload JSONB DEFAULT '{}',

    -- State
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),

    -- Retry tracking
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    last_error TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_execute ON scheduled_jobs(execute_at, status)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_instance ON scheduled_jobs(instance_id);

-- =============================================================================
-- HUB CONFIGURATION TABLE
-- =============================================================================
-- Hubitat hub connection info (supports multiple hubs)

CREATE TABLE IF NOT EXISTS hub_config (
    id SERIAL PRIMARY KEY,

    -- Hub identification
    hub_name VARCHAR(100) NOT NULL UNIQUE,
    hub_ip VARCHAR(50) NOT NULL,

    -- Maker API credentials
    maker_api_app_number VARCHAR(20) NOT NULL,
    -- Token stored in env var, reference here
    maker_api_token_env VARCHAR(50) NOT NULL,     -- e.g., 'HUBITAT_API_TOKEN_MAIN'

    -- Status
    is_primary BOOLEAN DEFAULT false,
    is_enabled BOOLEAN DEFAULT true,
    last_seen_at TIMESTAMPTZ,
    last_error TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- LOCATION MODES TABLE
-- =============================================================================
-- Cache of Hubitat location modes

CREATE TABLE IF NOT EXISTS location_modes (
    id SERIAL PRIMARY KEY,

    mode_id VARCHAR(50) NOT NULL UNIQUE,
    mode_name VARCHAR(100) NOT NULL,
    is_active BOOLEAN DEFAULT false,

    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- DEVICE MATTER MAP TABLE
-- =============================================================================
-- Maps Hubitat devices to their Matter protocol counterparts.
-- When a Hubitat device has a row here, commands are sent to BOTH
-- Hubitat (via Maker API) and Matter (via matter-server WebSocket).

CREATE TABLE IF NOT EXISTS device_matter_map (
    -- Hubitat device ID (foreign reference, not enforced since device_cache may not have all devices)
    hubitat_device_id VARCHAR(50) NOT NULL,

    -- Matter node ID assigned by matter-server during commissioning
    matter_node_id INTEGER NOT NULL,

    -- Matter endpoint (usually 1 for single-endpoint devices like bulbs)
    matter_endpoint_id INTEGER NOT NULL DEFAULT 1,

    -- Human-readable label for identification
    device_name VARCHAR(255),

    -- Tracking
    commissioned_at TIMESTAMPTZ DEFAULT NOW(),

    -- One Hubitat device maps to one Matter node
    PRIMARY KEY (hubitat_device_id)
);

-- Index for reverse lookup (Matter node → Hubitat device)
CREATE INDEX IF NOT EXISTS idx_device_matter_map_node
    ON device_matter_map(matter_node_id);

-- =============================================================================
-- HUBITAT MATTER DEVICES TABLE
-- =============================================================================
-- Discovered Matter devices from Hubitat hubs.
-- Populated by scanning all hubs via /hub/matterDetails/json.
-- Deduplicated by unique_id (same physical device may appear on multiple hubs).

CREATE TABLE IF NOT EXISTS hubitat_matter_devices (
    -- Matter unique ID from the device (globally unique across hubs)
    unique_id VARCHAR(100) PRIMARY KEY,

    -- Device info from Hubitat
    device_name VARCHAR(255),
    manufacturer VARCHAR(100),
    model VARCHAR(100),
    ip_address VARCHAR(100),
    is_online BOOLEAN DEFAULT false,

    -- Hubitat hub info (which hub reported this device)
    hub_ip VARCHAR(50) NOT NULL,
    hub_name VARCHAR(100),
    hubitat_node_id INTEGER NOT NULL,
    hubitat_device_id VARCHAR(50),          -- Hubitat internal device ID (the 'id' field)
    hubitat_dni VARCHAR(50),                -- Hubitat Device Network ID (e.g., 'M3052')

    -- Matched Maker API device (by name matching)
    -- This is the device ID from Hubitat's Maker API, NOT the internal Matter node ID.
    -- Used for dual-command: Maker API device ID → device_matter_map → our Matter node
    maker_api_device_id VARCHAR(50),        -- NULL until matched
    maker_api_device_name VARCHAR(255),     -- Name from Maker API (for verification)
    match_confidence VARCHAR(20) DEFAULT 'none',  -- 'exact', 'fuzzy', 'manual', 'none'

    -- Commissioning state in OUR matter-server
    our_node_id INTEGER,                    -- NULL until commissioned into our fabric
    is_commissioned BOOLEAN DEFAULT false,

    -- Commission retry tracking (exponential backoff + circuit breaker)
    commission_attempts INTEGER DEFAULT 0,          -- Total attempts made
    last_commission_attempt TIMESTAMPTZ,             -- When last attempt was made
    last_commission_error TEXT,                       -- Error from last failed attempt

    -- Tracking
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hubitat_matter_devices_hub
    ON hubitat_matter_devices(hub_ip);
CREATE INDEX IF NOT EXISTS idx_hubitat_matter_devices_commissioned
    ON hubitat_matter_devices(is_commissioned) WHERE is_commissioned = false;

-- =============================================================================
-- DEVICE HUB MAPPING TABLE
-- =============================================================================
-- Maps each device to its native (physically paired) Hubitat hub.
-- Populated by the hub_classifier service which queries all hubs' Maker APIs
-- and uses the hubMeshDisabled attribute to distinguish native vs linked devices.
--
-- This table enables:
-- 1. Command routing: send commands to the hub that owns the radio (no mesh relay)
-- 2. Event source identification: know which hub's event stream is authoritative
-- 3. Protocol awareness: know if a device is Z-Wave, Zigbee, Matter, LAN, etc.

CREATE TABLE IF NOT EXISTS device_hub_mapping (
    -- Composite key: device label + native hub (a device has one native hub)
    device_label VARCHAR(200) NOT NULL,
    native_hub_name VARCHAR(100) NOT NULL,

    -- Native hub connection info (denormalized from hub_config for fast lookups)
    native_hub_ip VARCHAR(50) NOT NULL,
    native_device_id VARCHAR(50) NOT NULL,

    -- Radio protocol (zwave, zigbee, matter, lan, virtual, cloud, unknown)
    protocol VARCHAR(30) NOT NULL DEFAULT 'unknown',

    -- Driver type from Hubitat (e.g., 'Generic Z-Wave Smart Switch')
    device_type VARCHAR(200),

    -- Mirror device IDs on other hubs (for cross-reference)
    -- Format: {"hub_name": {"id": "device_id", "hub_ip": "ip"}, ...}
    mirrors JSONB DEFAULT '{}',

    -- Classification metadata
    is_mesh_linked BOOLEAN DEFAULT false,
    last_classified_at TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (device_label, native_hub_name)
);

-- Index for fast lookups by label (most common query: "which hub owns this device?")
CREATE INDEX IF NOT EXISTS idx_device_hub_mapping_label
    ON device_hub_mapping(device_label);

-- Index for per-hub queries ("list all native devices on Home 2")
CREATE INDEX IF NOT EXISTS idx_device_hub_mapping_hub
    ON device_hub_mapping(native_hub_name);

-- Index for protocol queries ("list all Z-Wave devices")
CREATE INDEX IF NOT EXISTS idx_device_hub_mapping_protocol
    ON device_hub_mapping(protocol);

-- =============================================================================
-- DEVICES TABLE (canonical, mesh-phobic)
-- =============================================================================
-- Single source of truth for physical devices across all hubs.
--
-- Hub Mesh exposes the same physical sensor under DIFFERENT hubitat IDs on
-- each hub (e.g. one motion sensor → ids 112 on Main, 456 on Home1, 1461 on
-- Home2). The legacy device_cache used hubitat_device_id as PK, so cross-hub
-- collisions silently overwrote each other. This table fixes that:
--   - id: our own stable PK (used by subscriptions and selections)
--   - (hub_ip, hubitat_id): per-hub identity, UNIQUE so we never insert
--     the same hub-id pair twice
--   - label: Hubitat user-assigned label, UNIQUE because this IS one-per-
--     physical-device. (The Hubitat 'name' field is the driver type and
--     IS NOT unique — many devices share the same driver name.) A second
--     hub trying to register the same label hits the upsert_device()
--     SKIP_MESH branch and the first row wins.
--
-- Ingestion is double-defended:
--   1. hub_classifier._is_mesh_linked() filters mirrors at fetch time
--      (presence of hubMeshDisabled attribute = mirror, skip)
--   2. UNIQUE (label) + upsert_device() function enforces it at the DB

CREATE TABLE IF NOT EXISTS devices (
    -- Our own auto-incrementing primary key
    id BIGSERIAL PRIMARY KEY,

    -- Hub the device lives on (commands route here, events expected from here)
    hub_ip VARCHAR(50) NOT NULL,

    -- Device's id on that hub (Hubitat's internal id field)
    hubitat_id VARCHAR(50) NOT NULL,

    -- Hubitat 'name' field — driver type (e.g. 'Generic Zigbee Motion
    -- Sensor', 'Aeon Multisensor 6'). NOT unique per physical device —
    -- many devices share the same driver. Stored for diagnostics only.
    name VARCHAR(255) NOT NULL,

    -- Hubitat 'label' — user-assigned device identity (e.g. 'Motion
    -- Sensor Living Bookshelves'). UNIQUE: this IS one-per-physical-
    -- device, so it's the right key for mesh-duplicate rejection.
    -- A second hub trying to register the same label gets SKIP_MESH.
    label VARCHAR(255) NOT NULL UNIQUE,

    -- Driver type (e.g. 'Generic Z-Wave Smart Switch')
    device_type VARCHAR(200),

    -- Radio protocol (zwave, zigbee, matter, lan, virtual, cloud, unknown)
    protocol VARCHAR(30) DEFAULT 'unknown',

    -- Capability list from Maker API (for filtering in device pickers)
    capabilities JSONB DEFAULT '[]',

    -- Current attribute snapshot (motion, switch, level, etc.)
    attributes JSONB DEFAULT '{}',

    -- When this row was last refreshed from the hub
    last_synced_at TIMESTAMPTZ DEFAULT NOW(),

    -- Belt + suspenders: a (hub_ip, hubitat_id) pair must also be unique.
    UNIQUE (hub_ip, hubitat_id)
);

CREATE INDEX IF NOT EXISTS idx_devices_hub_hubitat
    ON devices(hub_ip, hubitat_id);
CREATE INDEX IF NOT EXISTS idx_devices_label
    ON devices(label);
CREATE INDEX IF NOT EXISTS idx_devices_capabilities
    ON devices USING GIN (capabilities);


-- =============================================================================
-- DEVICES UPSERT FUNCTION (mesh-phobic)
-- =============================================================================
-- Single entry point for ingesting devices. Atomically:
--   - INSERT a never-before-seen name
--   - UPDATE attributes if the same (hub_ip, hubitat_id) reappears
--   - SKIP_MESH if a different hub tries to claim the same name (mirror)
--
-- Returns (device_id, action) so the caller can log INSERT / UPDATE / SKIP_MESH.

CREATE OR REPLACE FUNCTION upsert_device(
    p_hub_ip       TEXT,
    p_hubitat_id   TEXT,
    p_name         TEXT,
    p_label        TEXT,
    p_device_type  TEXT,
    p_protocol     TEXT,
    p_capabilities JSONB,
    p_attributes   JSONB
) RETURNS TABLE(device_id BIGINT, action TEXT) AS $$
DECLARE
    v_existing_id         BIGINT;
    v_existing_hub_ip     TEXT;
    v_existing_hubitat_id TEXT;
BEGIN
    -- Lookup by label (canonical user-facing identity, unique per device)
    SELECT d.id, d.hub_ip, d.hubitat_id
      INTO v_existing_id, v_existing_hub_ip, v_existing_hubitat_id
      FROM devices d
     WHERE d.label = p_label
     LIMIT 1;

    IF v_existing_id IS NULL THEN
        INSERT INTO devices
            (hub_ip, hubitat_id, name, label, device_type, protocol,
             capabilities, attributes, last_synced_at)
        VALUES
            (p_hub_ip, p_hubitat_id, p_name, p_label, p_device_type,
             p_protocol, p_capabilities, p_attributes, NOW())
        RETURNING id INTO v_existing_id;
        device_id := v_existing_id;
        action    := 'INSERT';
        RETURN NEXT;

    ELSIF v_existing_hub_ip = p_hub_ip AND v_existing_hubitat_id = p_hubitat_id THEN
        UPDATE devices SET
            name           = p_name,
            device_type    = p_device_type,
            protocol       = p_protocol,
            capabilities   = p_capabilities,
            attributes     = p_attributes,
            last_synced_at = NOW()
        WHERE id = v_existing_id;
        device_id := v_existing_id;
        action    := 'UPDATE';
        RETURN NEXT;

    ELSE
        device_id := v_existing_id;
        action    := 'SKIP_MESH';
        RETURN NEXT;
    END IF;
END;
$$ LANGUAGE plpgsql;


-- =============================================================================
-- GRANT PERMISSIONS TO ANONYMOUS ROLE (for PostgREST)
-- =============================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smarthome_anon;
GRANT EXECUTE ON FUNCTION upsert_device TO smarthome_anon;

-- =============================================================================
-- TRIGGER: Update updated_at timestamp
-- =============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply to tables with updated_at column
DROP TRIGGER IF EXISTS update_app_types_updated_at ON app_types;
CREATE TRIGGER update_app_types_updated_at
    BEFORE UPDATE ON app_types
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_app_instances_updated_at ON app_instances;
CREATE TRIGGER update_app_instances_updated_at
    BEFORE UPDATE ON app_instances
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- =============================================================================
-- INITIAL DATA: Insert primary hub configuration
-- =============================================================================

INSERT INTO hub_config (hub_name, hub_ip, maker_api_app_number, maker_api_token_env, is_primary)
VALUES ('main', '<LAN_IP>', '268', 'HUBITAT_API_TOKEN_MAIN', true)
ON CONFLICT (hub_name) DO NOTHING;

INSERT INTO hub_config (hub_name, hub_ip, maker_api_app_number, maker_api_token_env, is_primary)
VALUES ('home_1', '<LAN_IP>', '1717', 'HUBITAT_API_TOKEN_OTHER_HUB_1', false)
ON CONFLICT (hub_name) DO NOTHING;

INSERT INTO hub_config (hub_name, hub_ip, maker_api_app_number, maker_api_token_env, is_primary)
VALUES ('home_2', '<LAN_IP>', '2151', 'HUBITAT_API_TOKEN_OTHER_HUB_2', false)
ON CONFLICT (hub_name) DO NOTHING;

INSERT INTO hub_config (hub_name, hub_ip, maker_api_app_number, maker_api_token_env, is_primary)
VALUES ('home_3', '<LAN_IP>', '1269', 'HUBITAT_API_TOKEN_OTHER_HUB_3', false)
ON CONFLICT (hub_name) DO NOTHING;

-- =============================================================================
-- COMMENTS
-- =============================================================================

COMMENT ON TABLE app_types IS 'Available app blueprints that users can instantiate';
COMMENT ON TABLE app_instances IS 'User-created automation instances with their own devices and settings';
COMMENT ON TABLE device_subscriptions IS 'Maps Hubitat devices to instances for webhook event routing';
COMMENT ON TABLE device_cache IS 'Cached device states to reduce Hubitat API polling';
COMMENT ON TABLE event_log IS 'Audit log of all processed events';
COMMENT ON TABLE scheduled_jobs IS 'Persistent scheduled tasks (timeouts, health checks)';
COMMENT ON TABLE hub_config IS 'Hubitat hub connection configuration';
COMMENT ON TABLE location_modes IS 'Cached Hubitat location modes';
COMMENT ON TABLE device_matter_map IS 'Maps Hubitat devices to Matter protocol nodes for dual-command control';
COMMENT ON TABLE hubitat_matter_devices IS 'Discovered Matter devices from Hubitat hubs, deduplicated by unique_id';
COMMENT ON TABLE device_hub_mapping IS 'Maps devices to their native hub for direct command routing and parallel event processing';

-- =============================================================================
-- SCHEMA SPLIT (2026-05-26) — see migration 007 + docs/plans/postgres_schema_split_*.md
-- Reorganize the public tables created above into storage schemas and expose
-- them as 1:1 views in the single PostgREST-facing schema `api` (Option B).
-- Runs at the END of fresh-DB init so the CREATE TABLE bodies above stay simple.
-- =============================================================================
BEGIN;

CREATE SCHEMA IF NOT EXISTS dshub;
CREATE SCHEMA IF NOT EXISTS dsapp;
CREATE SCHEMA IF NOT EXISTS dscore;
CREATE SCHEMA IF NOT EXISTS api;

DO $$
DECLARE
  hub  text[] := ARRAY['devices','device_cache','device_hub_mapping',
                       'device_matter_map','hubitat_matter_devices','hub_config',
                       'hub_health','raw_events','event_log','location_modes',
                       'mode_change_log'];
  app  text[] := ARRAY['app_types','app_type_settings','app_instances',
                       'device_subscriptions','event_routings','device_commands',
                       'instance_state_log','instance_setting_exceptions',
                       'scheduled_jobs'];
  core text[] := ARRAY['system_settings','encrypted_secrets','system_boot_log'];
  t text;
BEGIN
  FOREACH t IN ARRAY hub LOOP
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relname=t AND c.relkind='r') THEN
      EXECUTE format('ALTER TABLE public.%I SET SCHEMA dshub', t);
    END IF;
  END LOOP;
  FOREACH t IN ARRAY app LOOP
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relname=t AND c.relkind='r') THEN
      EXECUTE format('ALTER TABLE public.%I SET SCHEMA dsapp', t);
    END IF;
  END LOOP;
  FOREACH t IN ARRAY core LOOP
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relname=t AND c.relkind='r') THEN
      EXECUTE format('ALTER TABLE public.%I SET SCHEMA dscore', t);
    END IF;
  END LOOP;
END $$;

DO $$
DECLARE rec record;
BEGIN
  FOR rec IN
    SELECT s.relname AS seq, n.nspname AS tbl_schema
    FROM pg_class s
    JOIN pg_depend d  ON d.objid = s.oid AND d.deptype = 'a'
    JOIN pg_class tbl ON tbl.oid = d.refobjid
    JOIN pg_namespace n ON n.oid = tbl.relnamespace
    WHERE s.relkind = 'S'
      AND n.nspname IN ('dshub','dsapp','dscore')
      AND s.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
  LOOP
    EXECUTE format('ALTER SEQUENCE public.%I SET SCHEMA %I', rec.seq, rec.tbl_schema);
  END LOOP;
END $$;

DROP FUNCTION IF EXISTS public.upsert_device(text,text,text,text,text,text,jsonb,jsonb);
CREATE OR REPLACE FUNCTION api.upsert_device(
    p_hub_ip TEXT, p_hubitat_id TEXT, p_name TEXT, p_label TEXT,
    p_device_type TEXT, p_protocol TEXT, p_capabilities JSONB, p_attributes JSONB
) RETURNS TABLE(device_id BIGINT, action TEXT)
LANGUAGE plpgsql SET search_path = dshub, public AS $$
DECLARE
    v_existing_id BIGINT; v_existing_hub_ip TEXT; v_existing_hubitat_id TEXT;
BEGIN
    SELECT d.id, d.hub_ip, d.hubitat_id
      INTO v_existing_id, v_existing_hub_ip, v_existing_hubitat_id
      FROM devices d WHERE d.label = p_label LIMIT 1;
    IF v_existing_id IS NULL THEN
        INSERT INTO devices (hub_ip, hubitat_id, name, label, device_type, protocol,
             capabilities, attributes, last_synced_at)
        VALUES (p_hub_ip, p_hubitat_id, p_name, p_label, p_device_type, p_protocol,
             p_capabilities, p_attributes, NOW())
        RETURNING id INTO v_existing_id;
        device_id := v_existing_id; action := 'INSERT'; RETURN NEXT;
    ELSIF v_existing_hub_ip = p_hub_ip AND v_existing_hubitat_id = p_hubitat_id THEN
        UPDATE devices SET name=p_name, device_type=p_device_type, protocol=p_protocol,
            capabilities=p_capabilities, attributes=p_attributes, last_synced_at=NOW()
        WHERE id = v_existing_id;
        device_id := v_existing_id; action := 'UPDATE'; RETURN NEXT;
    ELSE
        device_id := v_existing_id; action := 'SKIP_MESH'; RETURN NEXT;
    END IF;
END; $$;

DO $$
DECLARE rec record;
BEGIN
  FOR rec IN
    SELECT n.nspname AS sch, c.relname AS t
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname IN ('dshub','dsapp','dscore') AND c.relkind='r'
  LOOP
    EXECUTE format('CREATE OR REPLACE VIEW api.%I AS SELECT * FROM %I.%I',
                   rec.t, rec.sch, rec.t);
  END LOOP;
END $$;

GRANT USAGE ON SCHEMA dshub, dsapp, dscore, api TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA dshub  TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA dsapp  TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA dscore TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA api    TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dshub  TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dsapp  TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dscore TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smarthome_anon;
GRANT EXECUTE ON FUNCTION api.upsert_device(text,text,text,text,text,text,jsonb,jsonb) TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dshub  GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dsapp  GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dscore GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA api    GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dshub  GRANT USAGE,SELECT ON SEQUENCES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dsapp  GRANT USAGE,SELECT ON SEQUENCES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dscore GRANT USAGE,SELECT ON SEQUENCES TO smarthome_anon;

NOTIFY pgrst, 'reload schema';
COMMIT;

-- =============================================================================
-- MIGRATION 008 (2026-05-26) — classifier dedup by (hub_ip,hubitat_id) +
-- linkedDevice/primary rule. Retires UNIQUE(label)+SKIP_MESH; adds
-- is_name_duplicate; upsert_device keys on identity and sets hub_id.
-- See psql/migrations/008_classifier_dedup_linkeddevice_primary_2026_05_26.sql
-- =============================================================================
BEGIN;
ALTER TABLE dshub.devices DROP CONSTRAINT IF EXISTS devices_label_key;
ALTER TABLE dshub.devices ADD COLUMN IF NOT EXISTS is_name_duplicate BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS idx_devices_label ON dshub.devices(label);

DROP FUNCTION IF EXISTS api.upsert_device(text,text,text,text,text,text,jsonb,jsonb);
DROP FUNCTION IF EXISTS api.upsert_device(text,text,integer,text,text,text,text,jsonb,jsonb,boolean);
CREATE OR REPLACE FUNCTION api.upsert_device(
    p_hub_ip TEXT, p_hubitat_id TEXT, p_hub_id INTEGER, p_name TEXT, p_label TEXT,
    p_device_type TEXT, p_protocol TEXT, p_capabilities JSONB, p_attributes JSONB,
    p_is_name_duplicate BOOLEAN
) RETURNS TABLE(device_id BIGINT, action TEXT)
LANGUAGE plpgsql SET search_path = dshub, public AS $$
DECLARE v_id BIGINT;
BEGIN
    SELECT id INTO v_id FROM devices WHERE hub_ip = p_hub_ip AND hubitat_id = p_hubitat_id;
    IF v_id IS NULL THEN
        INSERT INTO devices (hub_ip, hubitat_id, hub_id, name, label, device_type, protocol,
             capabilities, attributes, is_name_duplicate, last_synced_at)
        VALUES (p_hub_ip, p_hubitat_id, p_hub_id, p_name, p_label, p_device_type, p_protocol,
             p_capabilities, p_attributes, p_is_name_duplicate, NOW())
        RETURNING id INTO v_id;
        device_id := v_id; action := 'INSERT'; RETURN NEXT;
    ELSE
        UPDATE devices SET hub_id=p_hub_id, name=p_name, label=p_label, device_type=p_device_type,
            protocol=p_protocol, capabilities=p_capabilities, attributes=p_attributes,
            is_name_duplicate=p_is_name_duplicate, last_synced_at=NOW()
        WHERE id = v_id;
        device_id := v_id; action := 'UPDATE'; RETURN NEXT;
    END IF;
END; $$;
GRANT EXECUTE ON FUNCTION api.upsert_device(text,text,integer,text,text,text,text,jsonb,jsonb,boolean) TO smarthome_anon;
CREATE OR REPLACE VIEW api.devices AS SELECT * FROM dshub.devices;
NOTIFY pgrst, 'reload schema';
COMMIT;
