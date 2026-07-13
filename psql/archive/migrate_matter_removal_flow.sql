-- ============================================================================
-- Matter device removal / re-addition flow (operator directive 2026-07-08):
--   "When I remove a device, keep its canonical id and mark it REMOVED in the
--    DB. When re-added with the same name/identity, mark it ACTIVE again. Plus
--    a table to LOG matter removals and re-additions (troubleshooting + a
--    training substrate for future AI-driven cleanup)."
--
-- Additive + idempotent. NO hard deletes — a removed device is soft-deleted
-- (active=false, removed_at set) so its row + canonical id survive and a
-- re-add reactivates the SAME row.
-- ============================================================================

BEGIN;

-- Soft-delete marker on the normalized registry (active already exists).
ALTER TABLE dshub.matter_devices ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ;

-- Removal / re-addition audit log.
CREATE TABLE IF NOT EXISTS dshub.matter_removals (
    id                   BIGSERIAL PRIMARY KEY,
    matter_device_id     BIGINT,                 -- dshub.matter_devices.id (survives soft-delete)
    unique_id            VARCHAR(64),            -- stable re-add key
    serial_number        VARCHAR(100),
    matter_node_id       INTEGER,
    hubitat_device_label VARCHAR(200),
    action               VARCHAR(20) NOT NULL,   -- 'removed' | 'readded'
    decommissioned       BOOLEAN,                -- did the matter-server remove_node succeed?
    reason               TEXT,
    performed_by         VARCHAR(80),            -- operator | watchdog | api | discovery
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_matter_removals_unique ON dshub.matter_removals(unique_id);
CREATE INDEX IF NOT EXISTS idx_matter_removals_node   ON dshub.matter_removals(matter_node_id);
CREATE INDEX IF NOT EXISTS idx_matter_removals_ts     ON dshub.matter_removals(created_at);

-- Expose via the api schema (PostgREST). Restart smarthome-postgrest after.
CREATE OR REPLACE VIEW api.matter_removals AS SELECT * FROM dshub.matter_removals;
GRANT SELECT, INSERT, UPDATE, DELETE ON api.matter_removals TO smarthome_api, smarthome_anon;
GRANT USAGE, SELECT ON SEQUENCE dshub.matter_removals_id_seq TO smarthome_api, smarthome_anon;

COMMIT;
