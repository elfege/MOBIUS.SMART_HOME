-- =============================================================================
-- 015_matter_hub_ports_audit_table.sql
-- =============================================================================
-- Append-only audit of Matter hub->hub COPY ("port") attempts.
--
-- One row per device per run. The orchestrator (services/matter_hub_port/)
-- writes EVERY state transition as it happens, so a container restart mid-run
-- leaves an honest partial trail and the run is resumable by re-running
-- (idempotency comes from the already_on_target eligibility check, not from
-- run-state persistence).
--
-- Shape per the design doc §6 (docs/plans/matter_port_copy_..._no_transfer_
-- semantics.md). POLICY (P5 / operator 2026-07-11): NO foreign keys, NO
-- CASCADE — hub_config rows referenced by plain integer id; audit rows must
-- survive any hub reconfiguration untouched.
--
-- status values (§6):
--   pending | window_open | pairing | verified | renamed | done
--   | skipped_<reason>   (already_on_target, source_offline,
--                         thread_target_incompatible, mac_duplicate)
--   | failed_<class>     (window, pair_rejected, verify_timeout,
--                         fabric_full, exception)
--
-- Reached ONLY through the authenticated FastAPI routes (same convention as
-- migration 014's panel tables): no api.* view, no smarthome_anon grant.
-- =============================================================================

CREATE TABLE IF NOT EXISTS dshub.matter_hub_ports (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID        NOT NULL,
    source_hub_id   INTEGER     NOT NULL,   -- dshub.hub_config.id (no FK by policy)
    target_hub_id   INTEGER     NOT NULL,
    mac_address     TEXT,                   -- EUI-64 identity (cross-fabric key)
    serial_number   TEXT,
    device_name     TEXT,
    status          TEXT        NOT NULL,
    detail          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Run-scoped and device-scoped lookups (status endpoint + history view later).
CREATE INDEX IF NOT EXISTS idx_matter_hub_ports_run_id
    ON dshub.matter_hub_ports (run_id);
CREATE INDEX IF NOT EXISTS idx_matter_hub_ports_mac
    ON dshub.matter_hub_ports (mac_address);

-- The app writes through its own role (same one matter_pairing_lock uses).
GRANT INSERT, SELECT, UPDATE ON dshub.matter_hub_ports TO smarthome_api;
GRANT USAGE, SELECT ON SEQUENCE dshub.matter_hub_ports_id_seq TO smarthome_api;
