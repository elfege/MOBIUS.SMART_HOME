-- =============================================================================
-- 0_SMART_HOME Database Schema
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

    -- Device reference (Hubitat device ID as string)
    hubitat_device_id VARCHAR(50) NOT NULL,

    -- Instance reference
    instance_id BIGINT NOT NULL REFERENCES app_instances(id) ON DELETE CASCADE,

    -- Event type filter (motion, switch, contact, illuminance, mode, etc.)
    event_type VARCHAR(50) NOT NULL,

    -- Subscription metadata
    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- Unique constraint: one subscription per device/instance/event combination
    CONSTRAINT device_subscriptions_unique
        UNIQUE (hubitat_device_id, instance_id, event_type)
);

-- Primary index for event routing: device_id + event_type → instance_ids
CREATE INDEX IF NOT EXISTS idx_device_subscriptions_lookup
    ON device_subscriptions(hubitat_device_id, event_type);

-- Secondary index for cleanup when instance deleted (handled by CASCADE)
CREATE INDEX IF NOT EXISTS idx_device_subscriptions_instance
    ON device_subscriptions(instance_id);

-- =============================================================================
-- DEVICE CACHE TABLE
-- =============================================================================
-- Cache of Hubitat device states to reduce API polling
-- Refreshed on events and periodically via TTL

CREATE TABLE IF NOT EXISTS device_cache (
    -- Device identification (Hubitat device ID)
    hubitat_device_id VARCHAR(50) PRIMARY KEY,

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

    -- Tracking
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hubitat_matter_devices_hub
    ON hubitat_matter_devices(hub_ip);
CREATE INDEX IF NOT EXISTS idx_hubitat_matter_devices_commissioned
    ON hubitat_matter_devices(is_commissioned) WHERE is_commissioned = false;

-- =============================================================================
-- GRANT PERMISSIONS TO ANONYMOUS ROLE (for PostgREST)
-- =============================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smarthome_anon;

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
