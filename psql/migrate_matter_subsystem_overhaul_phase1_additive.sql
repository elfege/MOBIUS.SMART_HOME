-- ============================================================================
-- Matter subsystem overhaul — Phase 1 (ADDITIVE, non-destructive)
-- Branch: matter_subsystem_overhaul_hub_exclusive_mapping_and_normalized_sot_JUL_08_2026_a
-- Date:   2026-07-08
--
-- Creates the hub-exclusive mapping setting + the normalized Matter SOT tables
-- (matter_devices + matter_device_events), backfills from the legacy
-- dshub.hubitat_matter_devices, and exposes them via the api schema.
-- Idempotent + transactional. NO drops/deletes here — the destructive dedupe/
-- drop phase runs later with the operator watching + a fresh pg_dump.
--
-- Design:
--   * Field names align with TILES device_matter_map (matter_node_id,
--     matter_endpoint_id, device_type, active) for cross-project consistency.
--   * Mapping is INFORMATIONAL ONLY — nothing routes commands off it.
--   * "Exclusive to a user-selected hub" = dscore.system_settings.matter_mapping_hub
--     holds ONE hub_ip (default <LAN_IP> = hub index 0 = home_1). Reads
--     filter WHERE hub_ip = <that>; all hubs' data is retained, only the view is
--     scoped, so switching hubs is non-destructive. This alone resolves the
--     cross-hub duplicate-row mess (bedroom-1<->Laundry etc. were on .69 vs .70).
-- ============================================================================

BEGIN;

-- 1) Hub-exclusive mapping setting (default = first hub / index 0 = home_1/.69)
INSERT INTO dscore.system_settings
    (key, value, value_type, description, ui_exposed, requires_restart)
SELECT 'matter_mapping_hub', '<LAN_IP>', 'string',
       'IP of the single hub the Matter device mapping is scoped to (info-only). Default = hub index 0.',
       true, false
WHERE NOT EXISTS (SELECT 1 FROM dscore.system_settings WHERE key = 'matter_mapping_hub');

-- 2) Normalized Matter device registry (identity + static + dynamic state)
CREATE TABLE IF NOT EXISTS dshub.matter_devices (
    id                   BIGSERIAL PRIMARY KEY,
    unique_id            VARCHAR(64) NOT NULL UNIQUE,   -- Matter UniqueID (per-fabric stable)
    serial_number        VARCHAR(100),                  -- cross-fabric identity (dedup key)
    hub_ip               VARCHAR(45),                   -- owning hub (hub-exclusive scope)
    -- Hubitat mapping — INFORMATIONAL ONLY (never gate/route a command on it)
    hubitat_device_id    VARCHAR(50),
    hubitat_device_label VARCHAR(200),
    matter_node_id       INTEGER,                       -- our commissioned node id (legacy our_node_id)
    matter_endpoint_id   INTEGER NOT NULL DEFAULT 1,
    device_type          VARCHAR(50) DEFAULT 'light',
    -- Static identity
    manufacturer         VARCHAR(100),
    model                VARCHAR(100),
    vendor_id            VARCHAR(20),
    product_id           VARCHAR(20),
    mac_address          VARCHAR(32),
    firmware_version     VARCHAR(50),
    hardware_version     VARCHAR(50),
    protocol             VARCHAR(20),
    -- Dynamic state (fed by the matter-server subscription stream)
    is_online            BOOLEAN,
    switch_state         VARCHAR(10),
    level                INTEGER,
    color_hue            INTEGER,
    color_saturation     INTEGER,
    color_temp           INTEGER,
    power_w              NUMERIC,
    energy_kwh           NUMERIC,
    last_state_at        TIMESTAMPTZ,
    -- Bookkeeping
    is_commissioned      BOOLEAN DEFAULT false,
    active               BOOLEAN DEFAULT true,
    discovered_at        TIMESTAMPTZ DEFAULT now(),
    updated_at           TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_matter_devices_hub_ip  ON dshub.matter_devices(hub_ip);
CREATE INDEX IF NOT EXISTS idx_matter_devices_hubitat ON dshub.matter_devices(hubitat_device_id);
CREATE INDEX IF NOT EXISTS idx_matter_devices_node    ON dshub.matter_devices(matter_node_id);
CREATE INDEX IF NOT EXISTS idx_matter_devices_serial  ON dshub.matter_devices(serial_number);

-- 3) Matter event stream (dynamic attribute reports; retention-pruned later)
CREATE TABLE IF NOT EXISTS dshub.matter_device_events (
    id               BIGSERIAL PRIMARY KEY,
    matter_device_id BIGINT REFERENCES dshub.matter_devices(id) ON DELETE CASCADE,
    node_id          INTEGER,
    endpoint_id      INTEGER,
    cluster          VARCHAR(60),
    attribute        VARCHAR(60),
    value            TEXT,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_matter_events_device ON dshub.matter_device_events(matter_device_id);
CREATE INDEX IF NOT EXISTS idx_matter_events_ts     ON dshub.matter_device_events(received_at);

-- 4) Backfill from the legacy wide table (one row per Matter UniqueID)
INSERT INTO dshub.matter_devices
    (unique_id, serial_number, hub_ip, hubitat_device_id, hubitat_device_label,
     matter_node_id, manufacturer, model, vendor_id, product_id, mac_address,
     firmware_version, hardware_version, protocol, is_online, is_commissioned,
     discovered_at, updated_at)
SELECT
     h.unique_id, h.serial_number, h.hub_ip, h.hubitat_device_id, h.device_name,
     h.our_node_id, h.manufacturer, h.model, h.vendor_id, h.product_id, h.mac_address,
     h.firmware_version, h.hardware_version, h.protocol, h.is_online, h.is_commissioned,
     h.discovered_at, h.updated_at
FROM dshub.hubitat_matter_devices h
WHERE h.unique_id IS NOT NULL
ON CONFLICT (unique_id) DO NOTHING;

-- 5) Expose via the api schema (PostgREST). Restart smarthome-postgrest after.
CREATE OR REPLACE VIEW api.matter_devices        AS SELECT * FROM dshub.matter_devices;
CREATE OR REPLACE VIEW api.matter_device_events  AS SELECT * FROM dshub.matter_device_events;
GRANT SELECT, INSERT, UPDATE, DELETE ON api.matter_devices        TO smarthome_api, smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON api.matter_device_events  TO smarthome_api, smarthome_anon;
GRANT USAGE, SELECT ON SEQUENCE dshub.matter_devices_id_seq        TO smarthome_api, smarthome_anon;
GRANT USAGE, SELECT ON SEQUENCE dshub.matter_device_events_id_seq  TO smarthome_api, smarthome_anon;

COMMIT;
