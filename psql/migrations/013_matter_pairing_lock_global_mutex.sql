-- =============================================================================
-- 013_matter_pairing_lock_global_mutex.sql
-- =============================================================================
-- ONE GLOBAL MATTER-PAIRING MUTEX.
--
-- WHY: a Hubitat hub can process exactly ONE Matter device at a time — both as
-- the SOURCE (holding an open pairing window) and as the TARGET (consuming a
-- code). Operator invariant, reinforced 2026-07-13 (intercom MSG-919).
--
-- Two independent features now open pairing windows:
--   * Commission All            (Architect — app.py _bulk_commission_worker)
--   * Matter hub->hub COPY      (Assistant-2 — services/matter_hub_port.py)
-- ...plus the operator pairing a device BY HAND from a hub's UI.
--
-- If any two overlap, they fight over the same hub's single pairing slot and
-- devices fail to pair (or worse, pair into the wrong fabric). A per-feature
-- in-memory "am I running?" flag CANNOT see the other feature — so the guard
-- must be GLOBAL and SHARED, and it must be DATA (a row), not process memory:
--   - it survives an app restart (an in-memory flag silently unlocks on restart
--     while a hub's pairing window is still physically open);
--   - both features and the UI can SEE who holds it and why.
--
-- Data-oriented per operator directive ("everything registered in tables").
--
-- STALE-LOCK RECLAIM: a holder that dies mid-run must not wedge the system
-- forever, so every lock carries an explicit expires_at. Acquisition may take
-- over an EXPIRED lock (and records that it did, in taken_over_from).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS dscore;

CREATE TABLE IF NOT EXISTS dscore.tbl_matter_pairing_lock (
    -- Single-row table: id is pinned to 1 by a CHECK, so the mutex is global
    -- by construction rather than by convention.
    id                INTEGER PRIMARY KEY DEFAULT 1,
    is_held           BOOLEAN     NOT NULL DEFAULT false,
    holder            TEXT,                  -- e.g. 'commission_all', 'hub_port_copy', 'manual'
    holder_detail     TEXT,                  -- free-form: which device/hubs, for the UI
    acquired_at       TIMESTAMPTZ,
    expires_at        TIMESTAMPTZ,           -- stale-lock reclaim boundary
    released_at       TIMESTAMPTZ,
    taken_over_from   TEXT,                  -- non-null if we reclaimed an EXPIRED holder
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_matter_pairing_lock_singleton CHECK (id = 1),
    CONSTRAINT chk_matter_pairing_lock_held_has_holder
        CHECK (NOT is_held OR (holder IS NOT NULL AND expires_at IS NOT NULL))
);

COMMENT ON TABLE dscore.tbl_matter_pairing_lock IS
    'Global Matter-pairing mutex. A Hubitat pairs ONE device at a time, so '
    'Commission All, hub->hub copy, and manual pairing must never overlap. '
    'Single row (id=1). Held locks carry expires_at so a dead holder cannot '
    'wedge the system.';

-- Seed the singleton row (idempotent).
INSERT INTO dscore.tbl_matter_pairing_lock (id, is_held)
VALUES (1, false)
ON CONFLICT (id) DO NOTHING;
